"""MSC 地中海郵輪美國站 www.msccruisesusa.com 擷取器（船公司官網，報價為美元）。

網站是 Adobe AEM + React，結果頁的 HTML 只有骨架，航次由前端打自家代理的
Algolia 搜尋 API 取得：

  POST https://www.msccruisesusa.com/api-search/1/indexes/*/queries
  {"requests": [{"indexName": "prod-en-us-cruises", "filters": "…", …}]}

實測（2026-10-20）**不需要 cookie 也不需要 Algolia 金鑰**（代理自己補，
前端送的 x-algolia-* 標頭本來就是空字串），httpx 直接 POST 就有資料。
但前面有 Akamai：User-Agent 只送 "Mozilla/5.0" 會回 401，要送完整的瀏覽器 UA。

`prod-en-us-cruises` 的每筆紀錄是「航次 × 艙等 × 票價方案」，一個航次約 18 筆：

  cruiseID                  "BE20261212KEEKEE"（船代碼＋出發日＋上下船港）
  departureStartDate        "2026-12-12"
  numberOfNights            4（已經是夜數，不必減 1）
  embkPort / disembkPort    {"key": "KEE", "value": "Keelung (Taipei), Taiwan (China)"}
  shipCd                    {"key": "BE", "value": "MSC Bellissima"}
  visitingPorts[]           停靠港，含 "SEADAY"（海上航行日）
  prices.adultPrice         2 位成人同房時的每人價（約 cabinPrice / 2，取整數）
  prices.portCharges        每人港務費，另外加

實測踩到的幾件事：

  - **`discountedPricesBySpecificPax` 是限定身分的折扣價**（SENIOR25 長者、
    MSVG 會員），`paxType` 標了 SENIOR25 的紀錄 `adultPrice` 仍是一般價。
    只用 `adultPrice`，否則會把長者價拿去跟其他來源的一般價比。
  - **有單程航次**：`BE20261121TYOKEE` 是東京上、基隆下，到達港要看 `disembkPort`。
  - **網頁「Last Minute Deals」頁用的 deduplication 索引一條航線只給一筆**
    （所有出發日的最低價），不能拿來逐日比價，要查逐航次的 `prod-en-us-cruises`。
  - `hasAir:true` 是機＋船套裝（價格含機票），跟 icruise 的 cruiseOnly 同一個理由不收。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from .. import config, normalize
from ..models import Deal, utcnow
from .base import BlockedError, ParseError, keep_cheapest

log = logging.getLogger(__name__)

SOURCE = "msc"

CRUISE_LINE = "MSC Cruises"  # 與 cruisedirect.CRUISELINE_NAMES["msc"] 一致

# 停靠港清單裡的「海上航行日」
SEA_DAY = "SEADAY"

# 翻頁上限：一頁 1000 筆，一個月的亞洲航次正常一頁就夠；這是防成環的保險
MAX_PAGES = 5

# Akamai 擋下時回的狀態碼
_BLOCKED_STATUS = (401, 403)

# 兩位成人、無兒童，與網站預設的 passengers=2|0|0|0 相同
_PAX_FILTER = "(noofAdults=2) AND (noofMinors=0) AND (noofJuniors=0)"


def _epoch_ms(day: date, end_of_day: bool = False) -> int:
  """出發日的 Algolia 時間戳（UTC 毫秒）。紀錄裡存的是出發日的 UTC 午夜。"""
  moment = datetime.combine(
    day, dt_time.max if end_of_day else dt_time.min, tzinfo=timezone.utc
  )
  return int(moment.timestamp() * 1000)


def search_filters(start: date | None = None, end: date | None = None) -> str:
  """組出 Algolia filters 字串：亞洲區、兩位成人、純船票、有艙位，可選出發日窗口。"""
  parts = [
    f"(commArea.key:{config.MSC_AREA})",
    _PAX_FILTER,
    "(hasAir:false)",
    "(prices.availability:true)",
  ]
  if start and end:
    parts.append(
      f"(departureStartDateTimestamp >= {_epoch_ms(start)}"
      f" AND departureStartDateTimestamp <= {_epoch_ms(end, end_of_day=True)})"
    )
  return " AND ".join(parts)


def search_body(
  filters: str, page: int, hits_per_page: int = config.MSC_HITS_PER_PAGE
) -> dict[str, Any]:
  """組出跟網站前端同形狀的查詢 body（只查一個索引）。"""
  return {
    "requests": [
      {
        "indexName": config.MSC_INDEX,
        "filters": filters,
        "hitsPerPage": hits_per_page,
        "page": page,
        # 不讓 Algolia 依 distinct 鍵收斂：它留的是排名最前的那筆，不一定最便宜
        "distinct": 0,
        "attributesToHighlight": [""],
        "facets": [],
      }
    ]
  }


def extract_result(payload: Any) -> dict[str, Any]:
  """從回應取出唯一一個查詢結果。形狀不對就大聲失敗。"""
  results = payload.get("results") if isinstance(payload, dict) else None
  if not isinstance(results, list) or not results:
    raise ParseError("MSC 搜尋 API 回應缺少 results——API 可能已改版")
  result = results[0]
  if not isinstance(result, dict) or "hits" not in result or "nbHits" not in result:
    raise ParseError("MSC 搜尋 API 回應缺少 hits／nbHits——API 可能已改版")
  return result


def _headers() -> dict[str, str]:
  return {
    "User-Agent": config.USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "MSC-Locale": "en_US",
    "Origin": config.MSC_BASE,
    "Referer": config.MSC_BASE + "/deals/last-minute-cruise-deals",
  }


def _post(client: httpx.Client, body: dict[str, Any], page: int) -> dict[str, Any]:
  """送一次查詢並取出結果；被擋、看不懂的回應都轉成對應的例外。"""
  response = client.post(config.MSC_SEARCH_URL, json=body, headers=_headers())
  if response.status_code in _BLOCKED_STATUS:
    raise BlockedError(
      f"MSC 搜尋 API 回 {response.status_code}——被 Akamai 機器人防護擋下"
      "（User-Agent 不像瀏覽器、或 IP 信譽不足）"
    )
  response.raise_for_status()
  try:
    payload = response.json()
  except ValueError as exc:
    raise ParseError(
      f"MSC 搜尋 API 第 {page + 1} 頁回應不是 JSON（{exc}）——可能被擋或正在維護"
    ) from exc
  return extract_result(payload)


def fetch_hits(
  start: date,
  end: date,
  client: httpx.Client | None = None,
  delay_s: float = config.REQUEST_DELAY_S,
  timeout_s: float = 60.0,
) -> list[dict[str, Any]]:
  """查出窗口內亞洲區的所有紀錄（翻完所有頁）。

  窗口內 0 筆時再查一次不限日期的同條件筆數：亞洲區連一筆未來航次都沒有，
  代表篩選欄位被改名了（Algolia 對不存在的欄位是安靜地回 0 筆，不會報錯），
  要大聲失敗，而不是讓下游以為「今天真的沒船」。
  """
  own_client = client is None
  client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)

  filters = search_filters(start, end)
  hits: list[dict[str, Any]] = []
  try:
    claimed = 0
    for page in range(MAX_PAGES):
      if page:
        time.sleep(delay_s)  # 禮貌延遲
      result = _post(client, search_body(filters, page), page)
      hits.extend(result.get("hits") or [])
      claimed = int(result.get("nbHits") or 0)
      if page + 1 >= int(result.get("nbPages") or 1):
        break

    if claimed > len(hits):
      raise ParseError(
        f"MSC 搜尋 API 宣稱 {claimed} 筆，只拿到 {len(hits)} 筆——結果被截斷"
      )

    if not hits:
      overall = _post(client, search_body(search_filters(), 0, hits_per_page=0), 0)
      if not int(overall.get("nbHits") or 0):
        raise ParseError(
          "MSC 亞洲區連一筆未來航次都查不到——篩選欄位可能已改名（Algolia 對不存在的欄位不報錯）"
        )
  finally:
    if own_client:
      client.close()
  return hits


def _value(field: Any) -> str:
  """{"key": …, "value": …} 形狀的欄位取 value。"""
  return normalize.clean_text(field.get("value")) if isinstance(field, dict) else ""


def per_person_price(hit: dict[str, Any]) -> Decimal | None:
  """兩人一室的每人價，含港務費。

  港務費另外列（每人 7～10 美元），加進去才能跟 cruisedirect 的「含稅費」價對齊。
  不用 discountedPricesBySpecificPax：那是長者／會員限定的折扣價。
  """
  prices = hit.get("prices")
  if not isinstance(prices, dict):
    return None
  fare = normalize.parse_price(prices.get("adultPrice"))
  if fare is None:
    return None
  port_charges = normalize.parse_price(prices.get("portCharges")) or Decimal(0)
  return fare + port_charges


def itinerary_ports(hit: dict[str, Any]) -> tuple[str, ...]:
  """停靠港（不含海上航行日）。"""
  return tuple(
    _value(port)
    for port in hit.get("visitingPorts") or []
    if isinstance(port, dict) and port.get("key") != SEA_DAY and _value(port)
  )


def _parse_hit(hit: dict[str, Any], scraped_at: datetime) -> Deal | None:
  """解析單筆紀錄。非目標港回 None；欄位缺漏時拋 ValueError（交給呼叫端統計）。"""
  depart_raw = _value(hit.get("embkPort"))
  depart_port = normalize.match_port(depart_raw)
  if depart_port is None:
    return None

  sail_date = date.fromisoformat(str(hit["departureStartDate"]))
  nights = int(hit["numberOfNights"])
  ship_name = _value(hit.get("shipCd"))
  if nights <= 0 or not ship_name:
    raise ValueError(f"夜數或船名缺漏：{hit.get('objectID')}")

  arrive_raw = _value(hit.get("disembkPort"))
  return Deal(
    source=SOURCE,
    sail_date=sail_date,
    depart_port=depart_port,
    depart_port_raw=depart_raw,
    arrive_port=normalize.match_port(arrive_raw) or arrive_raw or depart_port,
    ports_of_call=itinerary_ports(hit),
    ship_name=ship_name,
    cruise_line=CRUISE_LINE,
    nights=nights,
    price=per_person_price(hit),
    currency="USD",
    price_note="各艙等最低價（每人，2 人一室，含港務費）",
    detail_url=config.MSC_DETAIL_URL.format(
      cruise_id=hit.get("cruiseID") or "", itin_cd=hit.get("itinCd") or ""
    ),
    scraped_at=scraped_at,
  )


def parse_hits(hits: list[dict[str, Any]], start: date, end: date) -> list[Deal]:
  """把紀錄湊成 Deal 清單（尚未去重）。只收目標港出發、窗口內、有價格的紀錄。

  有紀錄卻每一筆都解析失敗 -> 欄位改名了，大聲失敗。
  """
  scraped_at = utcnow()
  deals: list[Deal] = []
  malformed = 0
  for hit in hits:
    try:
      deal = _parse_hit(hit, scraped_at)
    except (KeyError, TypeError, ValueError) as exc:
      malformed += 1
      log.debug("MSC 略過無法解析的紀錄：%s", exc)
      continue
    if deal is None or deal.price is None:
      continue
    if start <= deal.sail_date <= end:
      deals.append(deal)

  if hits and malformed == len(hits):
    raise ParseError(
      f"MSC 搜尋 API 回了 {len(hits)} 筆紀錄卻一筆都解析不出來——欄位可能已改版"
    )
  return deals


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
) -> list[Deal]:
  """擷取 MSC 美國站上基隆／東京／橫濱出發、指定日期窗口內的航次（每航次留最便宜的艙等）。"""
  start = start or date.today()
  end = start + timedelta(days=lookahead_days)

  hits = fetch_hits(start, end)
  deals = keep_cheapest(parse_hits(hits, start, end))

  if not deals:
    # MSC 在亞洲只有一艘船，基隆／東京航次有季節性，窗口內沒有是常態，不要拋錯
    log.info(
      "MSC：亞洲區 %d 筆紀錄中沒有 %s~%s 由基隆／東京／橫濱出發的航次", len(hits), start, end
    )
  else:
    log.info("MSC：%d 筆", len(deals))
  return deals
