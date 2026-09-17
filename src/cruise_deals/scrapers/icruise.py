"""icruise.com 擷取器（走新版搜尋頁背後的 JSON API）。

該站 2026-09 起把搜尋頁分批換成 Arrivia 的 Angular SPA：同一個網址，有的連線
拿到舊的 server-rendered 結果表、有的拿到只剩空殼的新模板（結果由前端打 API 渲染）。
GitHub Actions 隔三差五拿到新模板，舊的 HTML 解析就「成功地」回 0 筆，把前一天的
資料整批洗掉。舊模板遲早會消失，所以改直接打新模板用的 API：

  POST https://shared-components-api-wa-prod-usc.azurewebsites.net
       /api/cruise/search/get-search-results
  {"destinations": "7", "date": "2026-10", "numberOfRecords": 100, "page": 1,
   "brand": "IC", "vacationType": "1,2", 其餘欄位空字串}

實測（2026-09-17）**不需要授權**，回 `{"searchResults": [...]}`：

  .sailingDate          "Oct 20, 2026"
  .numberOfDaysOrNights / .dayOrNight   夜數（"N"）或天數（"D"）
  .shipName / .ports / .departurePort / .returnPort
  .price                每人最低價；**-99 代表洽詢報價**
  .metaName             最低價的艙等（Interior／Balcony…，可能缺）
  .cruiseOnly           False 是含機票／陸上行程的套裝，不收
  .cruiseLineLogo       只給船公司 logo 圖檔，沒有名稱——用 CRUISE_LINE_BY_LOGO_ID 對照
  .id                   詳情頁 /c/itinDetail.php?CruiseItineraryID={id}（會轉到原本的 /itineraries/…）

篩選只認 `destinations`（7＝亞洲）與 `date`（月份），`ports` 要另一套代碼且
篩選矩陣端點對零售站回 500，所以出發港與日期窗口在本地過濾。
`numberOfRecords` 上限在 100～500 之間（500 回空），用 100 翻頁到不足一頁為止。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .. import config, normalize
from ..models import Deal, utcnow
from .base import ParseError, keep_cheapest, with_retry

log = logging.getLogger(__name__)

SOURCE = "icruise"

# 翻頁上限：亞洲一個月約 150 筆、每頁 100，兩頁就夠；這是防 API 回錯值時無限翻下去
MAX_PAGES = 20

# 洽詢報價的哨兵值
PRICE_ON_REQUEST = -99

# 船公司 logo 圖檔 id -> 名稱。API 只給圖檔，這張表是從 2026-09-17 亞洲全部結果整理的，
# 寫法對齊 scrapers/cruisedirect.py 的 CRUISELINE_NAMES（網頁的「船公司」篩選照字串分組）。
# 對不到的 id 會留空並記警告，看到警告就來補。
CRUISE_LINE_BY_LOGO_ID: dict[str, str] = {
  "10": "Carnival Cruise Line",
  "11": "Celebrity Cruises",
  "13": "Holland America Line",
  "14": "Norwegian Cruise Line",
  "15": "Princess Cruises",
  "16": "Regent Seven Seas Cruises",
  "17": "Royal Caribbean International",
  "18": "Windstar Cruises",
  "19": "Costa Cruises",
  "21": "Seabourn",
  "22": "Silversea",
  "23": "Disney Cruise Line",
  "27": "Viking",  # 維京河輪
  "28": "MSC Cruises",
  "29": "Oceania Cruises",
  "36": "AmaWaterways",
  "42": "Uniworld",
  "52": "Azamara",
  "53": "Avalon Waterways",
  "54": "Ponant",
  "56": "Lindblad Expeditions",
  "62": "Viking",  # 維京海洋
  "67": "Scenic",
  "75": "Emerald Cruises",
  "76": "The Ritz-Carlton Yacht Collection",
  "127": "Aurora Expeditions",
}

_LOGO_ID_RE = re.compile(r"/(\d+)_\d+\.\w+$")


def build_search_body(month: str, page: int) -> dict[str, Any]:
  """組出跟新版搜尋頁一模一樣的查詢 body（欄位照送，空的給空字串）。"""
  return {
    "destinations": config.ICRUISE_DESTINATION_ASIA,
    "date": month,
    "ships": "",
    "numberOfRecords": config.ICRUISE_PAGE_SIZE,
    "page": page,
    "ports": "",
    "cruiseLines": "",
    "duration": "",
    "sortBy": "",
    "brand": config.ICRUISE_BRAND,
    "vacationType": "1,2",
  }


def months_covering(start: date, end: date) -> list[str]:
  """日期窗口涵蓋的月份，API 的 date 參數格式（"2026-10"）。"""
  months: list[str] = []
  current = start.replace(day=1)
  while current <= end:
    months.append(current.strftime("%Y-%m"))
    current = (current + timedelta(days=32)).replace(day=1)
  return months


def extract_results(payload: Any) -> list[dict[str, Any]]:
  """從單頁回應取出結果清單。形狀不對就大聲失敗。"""
  if not isinstance(payload, dict) or "searchResults" not in payload:
    raise ParseError("icruise 搜尋 API 回應缺少 searchResults——API 可能已改版")
  return list(payload.get("searchResults") or [])


def logo_id(url: str | None) -> str | None:
  """從 logo 圖檔網址取出船公司 id：".../logos/120w/new/28_120.gif" -> "28"。"""
  match = _LOGO_ID_RE.search(url or "")
  return match.group(1) if match else None


def cruise_line(url: str | None) -> str:
  """logo 圖檔 -> 船公司名稱；對不到留空（呼叫端會記警告）。"""
  return CRUISE_LINE_BY_LOGO_ID.get(logo_id(url) or "", "")


def parse_item(item: dict[str, Any], scraped_at: datetime | None) -> Deal | None:
  """把一筆 API 結果轉成 Deal。不能用的（套裝、日期壞掉）回 None，不讓一筆拖垮整批。"""
  if not item.get("cruiseOnly", True):
    return None  # 含機票／陸上行程的套裝，價格跟其他來源的船票價不能比

  try:
    sail_date = datetime.strptime(
      normalize.clean_text(item.get("sailingDate")), "%b %d, %Y"
    ).date()
  except ValueError:
    return None

  count = int(item.get("numberOfDaysOrNights") or 0)
  nights = count - 1 if item.get("dayOrNight") == "D" else count

  depart_raw = normalize.clean_text(item.get("departurePort"))
  arrive_raw = normalize.clean_text(item.get("returnPort"))
  raw_price = item.get("price")
  price = None if raw_price == PRICE_ON_REQUEST else normalize.parse_price(raw_price)
  cabin = normalize.clean_text(item.get("metaName"))

  return Deal(
    source=SOURCE,
    sail_date=sail_date,
    depart_port=normalize.match_port(depart_raw) or depart_raw,
    depart_port_raw=depart_raw,
    arrive_port=normalize.match_port(arrive_raw) or arrive_raw,
    ports_of_call=tuple(
      normalize.clean_text(p) for p in item.get("ports") or [] if normalize.clean_text(p)
    ),
    ship_name=normalize.clean_text(item.get("shipName")),
    cruise_line=cruise_line(item.get("cruiseLineLogo")),
    nights=nights,
    price=price,
    currency="USD",
    price_note=f"每人最低價（{cabin}）" if cabin else "每人最低價",
    detail_url=config.ICRUISE_DETAIL_URL.format(itinerary_id=item.get("id") or ""),
    scraped_at=scraped_at or utcnow(),
  )


def deals_in_window(items: list[dict[str, Any]], start: date, end: date) -> list[Deal]:
  """整批結果 -> 目標港、窗口內的 Deal，同航次只留最便宜。

  整批 0 筆是改版（亞洲一整個月不可能沒有航次）；過濾後 0 筆才是正常。
  """
  if not items:
    raise ParseError(
      "icruise 搜尋 API 回傳 0 筆亞洲航次——亞洲整個月不可能沒航次，API 可能已改版"
    )

  scraped_at = utcnow()
  deals: list[Deal] = []
  for item in items:
    deal = parse_item(item, scraped_at)
    if deal is None or deal.depart_port not in config.TARGET_PORTS:
      continue
    if start <= deal.sail_date <= end:
      deals.append(deal)
  return keep_cheapest(deals)


def fetch_month(
  client: httpx.Client, month: str, delay_s: float = config.REQUEST_DELAY_S
) -> list[dict[str, Any]]:
  """查一個月的亞洲航次，翻頁到不足一頁為止；每頁失敗會重試。"""
  items: list[dict[str, Any]] = []
  for page in range(1, MAX_PAGES + 1):
    if page > 1:
      time.sleep(delay_s)  # 禮貌延遲

    def once() -> list[dict[str, Any]]:
      response = client.post(config.ICRUISE_SEARCH_API, json=build_search_body(month, page))
      response.raise_for_status()
      try:
        payload = response.json()
      except ValueError as exc:
        raise ParseError(f"icruise 搜尋 API 回應不是 JSON（{exc}）——可能正在維護") from exc
      return extract_results(payload)

    batch = with_retry(once, attempts=3, delay_s=delay_s)
    items.extend(batch)
    if len(batch) < config.ICRUISE_PAGE_SIZE:
      break
  return items


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
) -> list[Deal]:
  """擷取未來 lookahead_days 天內、由目標港口出發的所有航次。"""
  start = start or date.today()
  end = start + timedelta(days=lookahead_days)

  headers = {
    "User-Agent": config.USER_AGENT,
    "Origin": config.ICRUISE_BASE,
    "Referer": config.ICRUISE_BASE + "/",
  }
  items: list[dict[str, Any]] = []
  with httpx.Client(headers=headers, timeout=60.0) as client:
    for index, month in enumerate(months_covering(start, end)):
      if index:
        time.sleep(config.REQUEST_DELAY_S)
      items.extend(fetch_month(client, month))

  deals = deals_in_window(items, start, end)

  unknown = sorted(
    {
      logo_id(x.get("cruiseLineLogo")) or "?"
      for x in items
      if not cruise_line(x.get("cruiseLineLogo"))
    }
  )
  if unknown:
    log.warning(
      "icruise 有 %d 個船公司 logo id 沒對照到名稱：%s（見 CRUISE_LINE_BY_LOGO_ID）",
      len(unknown),
      "、".join(unknown),
    )
  log.info("icruise：%d 筆亞洲航次中有 %d 筆落在窗口內", len(items), len(deals))
  return deals
