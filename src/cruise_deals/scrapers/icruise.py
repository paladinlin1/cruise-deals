"""icruise.com 擷取器。

該站是 server-rendered HTML，不需要瀏覽器，用 httpx + selectolax 即可。

已實測的兩個限制：
  1. 每頁固定 25 筆，且 PageNo / strPage / page / CurrentPage /
     strResultsPerPage 等分頁參數由 GET 傳入全部無效。
  2. Sail_DateFrom / Sail_DateTo 接受 MM/DD/YYYY 且確實生效。
所以改用「切分日期窗口」讓每段結果自然低於 25 筆上限。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, TypeVar

import httpx
from selectolax.parser import HTMLParser, Node

from .. import config, normalize
from ..models import Deal, utcnow
from .base import ParseError

if TYPE_CHECKING:  # pragma: no cover
  from collections.abc import Callable

log = logging.getLogger(__name__)

T = TypeVar("T")

SOURCE = "icruise"

_MATCHED_RE = re.compile(r"([\d,]+)\s+Matched\s+Sailing", re.I)


def build_search_params(start: date, end: date) -> dict[str, str | int]:
  """組出 icruise 搜尋參數（格式已實測有效）。"""
  return {
    "VacationType": config.ICRUISE_VACATION_TYPE,
    "WMPHDestinationCodeSub": config.ICRUISE_DESTINATION_ASIA,
    "Sail_DateFrom": start.strftime("%m/%d/%Y"),
    "Sail_DateTo": end.strftime("%m/%d/%Y"),
  }


def build_search_url(start: date, end: date) -> str:
  """組出完整查詢網址，**保留日期中的字面斜線**。

  實測：把 "08/13/2026" 編碼成 "08%2F13%2F2026" 時該站會間歇性回 404，
  用字面斜線（與瀏覽器送出的形式相同）則穩定回 200。
  因此這裡自行組 query string，不交給 httpx 的 params 編碼。
  """
  params = build_search_params(start, end)
  query = "&".join(f"{key}={value}" for key, value in params.items())
  return f"{config.ICRUISE_SEARCH_URL}?{query}"


def with_retry(fn: Callable[[], T], attempts: int = 3, delay_s: float = 2.0) -> T:
  """重試包裝：該站會間歇性回 404／5xx，無人值守排程需自行重試。

  用遞增延遲，最後一次仍失敗才把例外往上拋。
  """
  last_exc: Exception | None = None
  for attempt in range(1, attempts + 1):
    try:
      return fn()
    except Exception as exc:  # noqa: BLE001 - 由呼叫端決定如何處理
      last_exc = exc
      if attempt < attempts:
        log.warning("第 %d/%d 次嘗試失敗（%s），稍後重試", attempt, attempts, exc)
        if delay_s:
          time.sleep(delay_s * attempt)
  assert last_exc is not None
  raise last_exc


def date_chunks(
  start: date, end: date, chunk_days: int = config.CHUNK_DAYS
) -> list[tuple[date, date]]:
  """把日期窗口切成不重疊、不遺漏的連續小段。"""
  if chunk_days < 1:
    raise ValueError("chunk_days 必須至少為 1")
  chunks: list[tuple[date, date]] = []
  current = start
  while current <= end:
    chunk_end = min(current + timedelta(days=chunk_days - 1), end)
    chunks.append((current, chunk_end))
    current = chunk_end + timedelta(days=1)
  return chunks


def matched_count(html: str) -> int:
  """讀出頁面宣稱的總筆數（"119 Matched Sailings"）。找不到時回 0。"""
  match = _MATCHED_RE.search(html)
  if not match:
    return 0
  return int(match.group(1).replace(",", ""))


def _text(row: Node, selector: str) -> str:
  """取節點文字並正規化空白；找不到節點回空字串。"""
  node = row.css_first(selector)
  return normalize.clean_text(node.text(separator=" ")) if node else ""


def _parse_row(row: Node, scraped_at) -> Deal | None:
  """解析單一 <tr>。不是結果列（例如表頭）時回 None。"""
  raw_date = _text(row, "div.celldata.depart h2")
  if not raw_date:
    return None  # 表頭或廣告列

  title = _text(row, "div.celldata.itin2 h2")
  depart_raw = normalize.strip_prefix_label(_text(row, "div.route div.start"))
  arrive_raw = normalize.strip_prefix_label(_text(row, "div.route div.end"))

  # 夜數優先取「Length」欄，取不到再退回行程標題（"3 Night ... Cruise"）
  try:
    nights = normalize.parse_nights(_text(row, "div.celldata.length h2"))
  except ValueError:
    nights = normalize.parse_nights(title)

  price_cell = row.css_first("div.celldata.price")
  price_text = _text(row, "div.celldata.price h2")
  price = normalize.parse_price(price_text)

  # 價格但書：把 h2 與按鈕文字剔除後剩下的說明（per person / 含稅費…）
  price_note = ""
  if price_cell:
    note = price_cell.text(separator=" ")
    for junk in (price_text, "Learn More"):
      if junk:
        note = note.replace(junk, " ")
    price_note = normalize.clean_text(note)

  link = row.css_first("div.celldata.price a[href]")
  href = link.attributes.get("href", "") if link else ""
  detail_url = f"{config.ICRUISE_BASE}{href.split('?')[0]}" if href else ""

  return Deal(
    source=SOURCE,
    sail_date=normalize.parse_sail_date(raw_date),
    depart_port=normalize.match_port(depart_raw) or depart_raw,
    depart_port_raw=depart_raw,
    arrive_port=arrive_raw,
    ports_of_call=normalize.split_ports(_text(row, "div.ports div.port_list")),
    ship_name=_text(row, "div.celldata.itin2 div.ship"),
    cruise_line=_text(row, "div.celldata.itin2 div.line"),
    nights=nights,
    price=price,
    currency="USD",
    price_note=price_note,
    detail_url=detail_url,
    scraped_at=scraped_at,
  )


def parse_search_page(html: str) -> list[Deal]:
  """解析一頁搜尋結果。

  若頁面宣稱有結果卻一筆都解析不出來，拋 ParseError——
  那代表版面改版了，安靜回傳空清單會讓下游誤刪好資料。
  """
  scraped_at = utcnow()
  tree = HTMLParser(html)
  table = tree.css_first("table#results_table")

  deals: list[Deal] = []
  if table is not None:
    for row in table.css("tr"):
      deal = _parse_row(row, scraped_at)
      if deal is not None:
        deals.append(deal)

  claimed = matched_count(html)
  if claimed > 0 and not deals:
    raise ParseError(
      f"頁面宣稱有 {claimed} 筆結果，卻解析出 0 筆——icruise 版面可能已改版"
    )
  return deals


def filter_target_ports(deals: list[Deal]) -> list[Deal]:
  """只留下出發港為目標港口（基隆／東京／橫濱）的航次。"""
  return [d for d in deals if d.depart_port in config.TARGET_PORTS]


def fetch_page(client: httpx.Client, start: date, end: date) -> str:
  """送出一段日期區間的查詢並回傳 HTML（含重試）。"""

  def once() -> str:
    response = client.get(build_search_url(start, end))
    response.raise_for_status()
    return response.text

  return with_retry(once, attempts=3, delay_s=config.REQUEST_DELAY_S)


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
  chunk_days: int = config.CHUNK_DAYS,
) -> list[Deal]:
  """擷取未來 lookahead_days 天內、由目標港口出發的所有航次。"""
  start = start or date.today()
  end = start + timedelta(days=lookahead_days)
  chunks = date_chunks(start, end, chunk_days)

  collected: dict[tuple, Deal] = {}
  headers = {
    "User-Agent": config.USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
  }

  with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as client:
    for index, (chunk_start, chunk_end) in enumerate(chunks):
      if index:
        time.sleep(config.REQUEST_DELAY_S)  # 禮貌延遲
      html = fetch_page(client, chunk_start, chunk_end)
      page_deals = parse_search_page(html)
      claimed = matched_count(html)
      if claimed > 25:
        # 切段後仍撞到 25 筆上限代表會漏資料，記下來以便調小 chunk_days
        log.warning(
          "%s ~ %s 有 %d 筆結果，超過單頁 25 筆上限，可能遺漏；建議調小 CHUNK_DAYS",
          chunk_start,
          chunk_end,
          claimed,
        )
      for deal in filter_target_ports(page_deals):
        collected[deal.dedup_key] = deal

  return list(collected.values())
