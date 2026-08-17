"""asiayo.com 擷取器（台灣 OTA，報價為新台幣）。

該站是 Next.js App Router 的伺服器渲染頁，`httpx` 直接就能拿到完整資料，
不需要瀏覽器。資料躺在 RSC flight payload（`self.__next_f.push([1,"…"])`）裡，
形狀很乾淨：

    {"id":51219,"name":"【麗星郵輪探索星號】鹿兒島、熊本、那霸6天-週日出發",
     "activityDays":6,"availableDates":["2026-08-23","2026-09-13"],
     "price":18000,"port":{"id":"KEE","name":"基隆"},
     "journey":{"daily":[{"description":"第一天：基隆港【郵輪20:00啟航】"}, …]}}

實測發現（都是打真實請求才知道的）：

  - **`price` 是「查詢區間內所有出發日的最低價」，不是某一天的價格。**
    同一筆 51219 查 08/17–09/16 顯示 18,000，查 08/21–08/25（只含 08/23）
    卻是 21,583。所以必須**切段查詢**才有正確的逐日價格，
    這裡沿用 icruise 那套 date_chunks（CHUNK_DAYS=5）。
  - **`startDate == endDate` 時該站會忽略上界**，回傳往後好幾個月的出發日，
    所以不能用「一天一查」來取得精確價格。
  - 使用者常見的分享網址會帶 `cruiseIds` / `companyIds` 篩選，
    那會少抓資料，這裡只帶日期與分頁。
  - 港口代碼只有 KEE／KHH／SIN／TYO，**沒有獨立的橫濱**；
    TYO 寫成「東京（東京/橫濱）」，實際是哪個港要看行程第一天的敘述。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx

from .. import config, normalize
from ..models import Deal, utcnow
from .base import ParseError

# 這兩個是通用的日期切段與重試工具，先在 icruise 寫出來的，直接沿用不重寫
from .icruise import date_chunks, with_retry

log = logging.getLogger(__name__)

SOURCE = "asiayo"

# 該站每頁 20 筆；設個上限避免分頁邏輯出錯時無限打下去
MAX_PAGES = 20

_DECODER = json.JSONDecoder()

# flight payload 的每個片段：self.__next_f.push([1,"…"])
_FLIGHT_RE = re.compile(r"self\.__next_f\.push\(\[1,\s*")

# 商品物件的起點
_ITEM_RE = re.compile(r'"item":\s*\{"id":\s*\d+,\s*"name":')

# 分頁資訊
_META_RE = re.compile(r'\{"total":\s*(\d+),\s*"currentPage":\s*\d+,\s*"limit":\s*(\d+)')

# 行程敘述的「第N天」前綴（阿拉伯數字、全形數字、國字都要吃）
_DAY_PREFIX_RE = re.compile(
  r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十]+\s*天|Day\s*\d+)\s*[:：]?\s*"
)

# 只有「第N天」自成一行的情況（地名在下一行）
_DAY_ONLY_RE = re.compile(
  r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十]+\s*天|Day\s*\d+)\s*[:：]?\s*$"
)

# 地名之後的動詞、時刻與說明，切掉只留地名
_DAY_TRAILER_RE = re.compile(
  r"\s*(?:【|抵達|啟航|登船|下船|出發|停靠|Arrival|Departs?).*$", re.I
)

# 海上巡航日不是港口
_SEA_DAY_RE = re.compile(r"海上|巡遊|巡航|at\s*sea", re.I)


def build_search_url(port_id: str, start: date, end: date, page: int = 1) -> str:
  """組出搜尋網址。只帶日期與分頁，不帶 cruiseIds／companyIds（會少抓資料）。"""
  path = config.ASIAYO_LIST_PATH.format(port_id=port_id)
  return (
    f"{config.ASIAYO_BASE}{path}"
    f"?startDate={start.isoformat()}&endDate={end.isoformat()}&page={page}"
  )


def flight_payload(html: str) -> str:
  """把散在多個 <script> 裡的 RSC flight 片段還原成一整段文字。

  用 json 的 raw_decode 而非正則抓字串內容——payload 裡到處是跳脫引號，
  正則很容易在 `\\"])` 這種地方提早斷掉。
  """
  parts: list[str] = []
  for match in _FLIGHT_RE.finditer(html):
    index = match.end()
    if index >= len(html) or html[index] != '"':
      continue  # 不是字串片段（例如 push([1,null])）
    try:
      value, _ = _DECODER.raw_decode(html, index)
    except ValueError:
      continue
    if isinstance(value, str):
      parts.append(value)
  return "".join(parts)


def extract_items(html: str) -> list[dict[str, Any]]:
  """從頁面抽出所有航程商品物件。"""
  flight = flight_payload(html)
  items: list[dict[str, Any]] = []
  for match in _ITEM_RE.finditer(flight):
    start = flight.index("{", match.start())
    try:
      obj, _ = _DECODER.raw_decode(flight, start)
    except ValueError:
      continue
    if isinstance(obj, dict) and obj.get("id") is not None:
      items.append(obj)
  return items


def page_meta(html: str) -> tuple[int, int] | None:
  """讀出 (總筆數, 每頁筆數)。沒有結果時該站不會輸出這段，回 None。"""
  match = _META_RE.search(flight_payload(html))
  if not match:
    return None
  return int(match.group(1)), int(match.group(2))


def _day_place(description: str | None) -> str:
  """從行程某一天的敘述取出地名。取不到或是海上巡航日回空字串。

  兩種排版都要吃：
    "第三天：鹿兒島【郵輪7:00抵達、19:00啟航】"   地名跟日次同一行
    "第1天\\n日本 東京 (橫濱) 登船\\n15:00 啟航"    日次自成一行、地名在下一行
  """
  for line in (description or "").split("\n"):
    text = normalize.clean_text(line)
    if not text or _DAY_ONLY_RE.match(text):
      continue
    place = _DAY_TRAILER_RE.sub("", _DAY_PREFIX_RE.sub("", text))
    place = normalize.clean_text(place).strip("．・、,-　 ")
    if not place:
      continue
    return "" if _SEA_DAY_RE.search(place) else place
  return ""


def journey_ports(journey: dict[str, Any] | None) -> tuple[str, ...]:
  """把 journey.daily 轉成停靠港序列（去掉海上巡航日）。

  只收斂**連續**重複（同一個港停兩天會有抵達日與離開日兩筆），
  不做全域去重——來回航次的第一天與最後一天都是母港，
  全域去重會把回程那筆吃掉，arrive_port 就會變成中間的某個停靠港。
  """
  ports: list[str] = []
  for day in (journey or {}).get("daily") or []:
    place = _day_place(day.get("description"))
    if place and (not ports or ports[-1] != place):
      ports.append(place)
  return tuple(ports)


def _resolve_port(item: dict[str, Any]) -> tuple[str | None, str]:
  """判斷出發港，回傳 (正規化港名, 來源原始寫法)。

  該站的 TYO 把東京與橫濱併成「東京（東京/橫濱）」，光看這個欄位分不出來。
  行程第一天的敘述才分得出：橫濱出發寫「日本 東京 (橫濱) 登船」，
  東京出發寫「日本東京出發」。判不出來時退回港口欄位。
  """
  port_raw = normalize.clean_text((item.get("port") or {}).get("name"))
  daily = (item.get("journey") or {}).get("daily") or []
  first_day = daily[0].get("description") if daily else ""

  return normalize.match_port(first_day) or normalize.match_port(port_raw), port_raw


def parse_items(
  items: list[dict[str, Any]], start: date, end: date
) -> list[Deal]:
  """把商品物件展開成 Deal（一個出發日一筆）。"""
  scraped_at = utcnow()
  deals: list[Deal] = []

  for item in items:
    depart_port, depart_raw = _resolve_port(item)
    if depart_port is None:
      continue  # 非目標出發港（防禦性檢查，查詢時已用 port_id 篩過）

    ship_name, ship_raw, cruise_line = normalize.split_ship_and_line(item.get("name"))
    ports = journey_ports(item.get("journey"))

    # asiayo 講「幾天」，其他來源講「幾夜」
    days = item.get("activityDays") or 0
    nights = max(int(days) - 1, 0)

    price = normalize.parse_price(item.get("price"))
    if price is not None:
      price = price.quantize(Decimal("1"))

    for raw_date in item.get("availableDates") or []:
      try:
        sail_date = normalize.parse_sail_date(raw_date)
      except ValueError:
        log.warning("略過無法解析的出發日期: %r", raw_date)
        continue
      if not (start <= sail_date <= end):
        continue

      deals.append(
        Deal(
          source=SOURCE,
          sail_date=sail_date,
          depart_port=depart_port,
          depart_port_raw=depart_raw,
          arrive_port=ports[-1] if ports else "",
          ports_of_call=ports,
          ship_name=ship_name,
          cruise_line=cruise_line,
          nights=nights,
          price=price,
          currency="TWD",
          price_note="行程總價（每人，2 人一室起）",
          detail_url=config.ASIAYO_ITEM_URL.format(
            item_id=item.get("id"), date=sail_date.isoformat()
          ),
          scraped_at=scraped_at,
          ship_name_raw=ship_raw,
        )
      )

  return deals


def fetch_page(client: httpx.Client, port_id: str, start: date, end: date, page: int) -> str:
  """取得一頁搜尋結果的 HTML（含重試）。"""

  def once() -> str:
    response = client.get(build_search_url(port_id, start, end, page))
    response.raise_for_status()
    return response.text

  return with_retry(once, attempts=3, delay_s=config.REQUEST_DELAY_S)


def fetch_chunk(
  client: httpx.Client, port_id: str, start: date, end: date
) -> list[dict[str, Any]]:
  """抓完某個港口、某一段日期的所有頁面，回傳原始商品物件。

  頁面宣稱有結果卻一個商品都抽不出來時拋 ParseError——
  安靜回空清單會讓下游誤以為「今天真的沒有 deal」而蓋掉好資料。
  """
  items: list[dict[str, Any]] = []
  total: int | None = None

  for page in range(1, MAX_PAGES + 1):
    if page > 1:
      time.sleep(config.REQUEST_DELAY_S)  # 禮貌延遲
    html = fetch_page(client, port_id, start, end, page)
    batch = extract_items(html)
    meta = page_meta(html)

    if page == 1:
      total = meta[0] if meta else 0
      if total and not batch:
        raise ParseError(
          f"asiayo {port_id} {start}~{end} 宣稱有 {total} 筆結果卻解析出 0 筆"
          "——版面可能已改版"
        )
    items.extend(batch)
    if not batch or total is None or len(items) >= total:
      break

  return items


def _keep_cheaper(collected: dict[tuple, Deal], deal: Deal) -> None:
  """來源內先去重：同一航次（同船同日同夜數）只留最便宜的一筆。

  asiayo 會把同一航次拆成多個商品（例如加購岸上觀光），
  不先收斂的話會在合併階段變成「asiayo 跟 asiayo 自己比價」。
  """
  existing = collected.get(deal.dedup_key)
  if existing is None:
    collected[deal.dedup_key] = deal
    return
  if deal.price is not None and (existing.price is None or deal.price < existing.price):
    collected[deal.dedup_key] = deal


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
  chunk_days: int = config.CHUNK_DAYS,
) -> list[Deal]:
  """擷取 asiayo 上目標港口、指定日期窗口內的航次。"""
  start = start or date.today()
  end = start + timedelta(days=lookahead_days)
  chunks = date_chunks(start, end, chunk_days)

  collected: dict[tuple, Deal] = {}
  unmapped: set[str] = set()
  headers = {
    "User-Agent": config.USER_AGENT,
    "Accept-Language": "zh-TW,zh;q=0.9",
  }

  with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as client:
    first = True
    for port_id in config.ASIAYO_PORT_IDS:
      for chunk_start, chunk_end in chunks:
        if not first:
          time.sleep(config.REQUEST_DELAY_S)  # 禮貌延遲
        first = False

        items = fetch_chunk(client, port_id, chunk_start, chunk_end)
        for deal in parse_items(items, chunk_start, chunk_end):
          if normalize.is_unmapped_ship(deal.ship_name):
            unmapped.add(deal.ship_name)
          _keep_cheaper(collected, deal)

  if unmapped:
    # 對不到英文船名的航次不會跟外國站合併比價，但也不該讓整批失敗。
    # 記下來提醒補 config.SHIP_ALIASES。
    log.warning(
      "asiayo 有 %d 個船名沒對應到英文正式名（無法跨來源比價）：%s",
      len(unmapped),
      "、".join(sorted(unmapped)),
    )

  log.info("asiayo：%d 筆", len(collected))
  return list(collected.values())
