"""expediacruises.com 擷取器（Odysseus Swift v2 平台）。

那個 /swift/cruise 網址回傳的只是 12KB 的 SPA 空殼，真正的資料來自
POST /nitroapi/v2/cruise。該 API 需要 `uniquetid` 授權標頭，而這個值
是由頁面 JS 逐次產生的——所以我們不自己偽造，而是：

  1. 用真實瀏覽器載入頁面，讓 SPA 自行完成 reCAPTCHA 驗證
  2. 攔截它發出的第一個搜尋請求，取得其網址與標頭
  3. 沿用那組標頭，送出我們自己的篩選條件（出發港、排序、分頁）

實測得到的 API 限制（都是 API 自己回報的錯誤訊息）：
  - pageSize 上限 50（超過回 "Maximum Page Size Limit 50 is Exceeded"）
  - pageStart 是「頁碼」而非筆數位移，from = (pageStart - 1) * pageSize
  - sortColumn 只接受 departureDateTime（departureDate 會被拒絕）
  - departurePortCode 篩選有效；departureDate 區間篩選無效，故日期在本地過濾
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .. import config, normalize
from ..models import Deal, utcnow
from .base import ParseError

log = logging.getLogger(__name__)

SOURCE = "expedia"

# 目標出發港在 Expedia 主檔中的代碼
# （KEEL/TPE 都是基隆；TYO/NRT 都對應東京；YOK 是橫濱）
DEPARTURE_PORT_CODES = ["KEEL", "TPE", "TYO", "NRT", "YOK"]

# 亞洲目的地
DESTINATION_ID = "19"

# API 明確回報的上限
PAGE_SIZE = 50
MAX_PAGES = 20

# prices[].items[] 裡屬於「房型」的項目；其餘是稅金與港務費（用 code 而非 name）
CABIN_TYPES = ("Inside", "Outside", "Balcony", "Suite")

BASE = "https://bookus.expediacruises.com"
PACKAGE_URL = BASE + "/swift/cruise/package/{package_id}?siid=1095905&lang=1"


def build_search_body() -> dict[str, Any]:
  """搜尋條件。出發港在伺服器端篩，日期在本地篩（API 的日期區間篩選無效）。"""
  return {
    "filters": [
      {"key": "destinationId", "values": [DESTINATION_ID]},
      {"key": "destinationType", "value": "All"},
      {"key": "departurePortCode", "values": list(DEPARTURE_PORT_CODES)},
    ]
  }


def build_search_url(endpoint: str, page_index: int) -> str:
  """組出搜尋網址。page_index 為 1 起算的頁碼。"""
  return (
    f"{endpoint}?sortColumn=departureDateTime&sortOrder=asc"
    f"&pageSize={PAGE_SIZE}&pageStart={page_index}"
    f"&groupByItineraryId=true&applyExchangeRates=true&requestSource=1"
  )


def lowest_price(package: dict[str, Any]) -> Decimal | None:
  """從各房型報價中取最低者。

  同一份 items 裡混著房型價與稅金／港務費（後者用 "code" 而非 "name"），
  只認 CABIN_TYPES 裡的名稱，否則會把 4.35 元的稅金當成最低價。
  """
  values: list[Decimal] = []
  for price_block in package.get("prices") or []:
    for item in price_block.get("items") or []:
      if item.get("name") not in CABIN_TYPES:
        continue
      value = normalize.parse_price(item.get("value"))
      if value is not None:
        values.append(value)
  return min(values) if values else None


def currency_of(package: dict[str, Any]) -> str:
  """讀取報價幣別，預設 USD。"""
  for price_block in package.get("prices") or []:
    code = price_block.get("currencyCode")
    if code:
      return str(code)
  return "USD"


def _port_name(master: dict[str, Any], code: str | None) -> str:
  """把港口代碼轉成可讀名稱；查不到就退回代碼本身。"""
  if not code:
    return ""
  return (master.get("port") or {}).get(code) or code


def _ship_and_line(master: dict[str, Any], ship_id: Any) -> tuple[str, str]:
  """由 shipId 查出船名與船公司名。查不到時回空字串，不讓整批解析失敗。"""
  ships = master.get("ship") or {}
  entry = ships.get(str(ship_id)) or {}
  ship_name = entry.get("name") or ""
  lines = master.get("cruiseline") or {}
  line_name = lines.get(str(entry.get("parentId"))) or ""
  return ship_name, line_name


def parse_items(
  items: list[dict[str, Any]],
  master: dict[str, Any],
  start: date,
  end: date,
) -> list[Deal]:
  """把 API 的航程清單轉成 Deal。

  一個 item 是一條航線，底下的每個 package 是一個實際出發日期，
  因此會展開成多筆 Deal。
  """
  if items and not (master.get("port") or {}):
    raise ParseError("Expedia 主檔沒有港口資料，無法把港口代碼轉成名稱")

  scraped_at = utcnow()
  deals: list[Deal] = []

  for item in items:
    itinerary = item.get("itinerary") or {}
    depart_code = (itinerary.get("departure") or {}).get("code")
    depart_raw = _port_name(master, depart_code)
    depart_port = normalize.match_port(depart_raw)
    if depart_port is None:
      continue  # 非目標出發港（防禦性檢查，伺服器端理應已篩過）

    arrive_raw = _port_name(master, (itinerary.get("arrival") or {}).get("code"))
    ship_name, cruise_line = _ship_and_line(master, (item.get("ship") or {}).get("id"))
    duration = itinerary.get("duration")

    for package in item.get("packages") or []:
      raw_date = package.get("startDateTime")
      if not raw_date:
        continue
      try:
        sail_date = normalize.parse_sail_date(raw_date)
      except ValueError:
        log.warning("略過無法解析的出發日期: %r", raw_date)
        continue
      if not (start <= sail_date <= end):
        continue

      try:
        nights = normalize.parse_nights(duration)
      except (ValueError, TypeError):
        nights = 0

      package_id = package.get("id")
      deals.append(
        Deal(
          source=SOURCE,
          sail_date=sail_date,
          depart_port=depart_port,
          depart_port_raw=depart_raw,
          arrive_port=arrive_raw,
          ports_of_call=(),  # 清單 API 不含逐站停靠港
          ship_name=ship_name,
          cruise_line=cruise_line,
          nights=nights,
          price=lowest_price(package),
          currency=currency_of(package),
          price_note="各房型最低價（每人）",
          detail_url=PACKAGE_URL.format(package_id=package_id) if package_id else "",
          scraped_at=scraped_at,
        )
      )

  return deals


def _capture_session(page, timeout_s: int = 60) -> tuple[str, dict[str, str]]:
  """載入 SPA 並攔下它自己的搜尋請求，取得可重複使用的網址與標頭。"""
  captured: dict[str, Any] = {}

  def on_request(request):
    if "/nitroapi/v2/cruise?" in request.url and request.method == "POST":
      captured.setdefault("url", request.url)
      captured.setdefault("headers", dict(request.headers))

  page.on("request", on_request)
  page.goto(config.EXPEDIA_URL, wait_until="domcontentloaded", timeout=90_000)

  for _ in range(timeout_s // 2):
    page.wait_for_timeout(2_000)
    if "headers" in captured:
      break

  if "headers" not in captured:
    raise ParseError(
      "等不到 Expedia SPA 發出搜尋請求（可能是 reCAPTCHA 未通過或版面改版）"
    )

  endpoint = captured["url"].split("?")[0]
  return endpoint, captured["headers"]


def _fetch_all(page, endpoint: str, headers: dict[str, str]) -> list[dict[str, Any]]:
  """沿用 SPA 的標頭分頁抓取全部結果。"""
  body = json.dumps(build_search_body())
  items: list[dict[str, Any]] = []
  total: int | None = None

  for page_index in range(1, MAX_PAGES + 1):
    response = page.request.post(
      build_search_url(endpoint, page_index), headers=headers, data=body
    )
    payload = response.json()
    if not payload.get("isSucceed"):
      advisories = json.dumps(payload.get("advisories"), ensure_ascii=False)
      if page_index == 1:
        raise ParseError(f"Expedia 搜尋失敗: {advisories[:200]}")
      log.warning("第 %d 頁停止：%s", page_index, advisories[:200])
      break

    data = payload.get("data") or {}
    total = data.get("total")
    batch = data.get("list") or []
    items.extend(batch)
    log.info("Expedia 第 %d 頁：+%d，累計 %d/%s", page_index, len(batch), len(items), total)
    if not batch or (total is not None and len(items) >= total):
      break

  return items


def _fetch_master(page, endpoint: str, headers: dict[str, str]) -> dict[str, Any]:
  """抓取主檔並精簡成 id -> 名稱 的對映。"""
  root = endpoint.rsplit("/nitroapi", 1)[0]
  response = page.request.get(
    f"{root}/nitroapi/v2/master/allswift?requestSource=1", headers=headers
  )
  payload = response.json()
  if not payload.get("isSucceed"):
    raise ParseError("Expedia 主檔抓取失敗")
  data = payload["data"]
  return {
    "ship": {
      str(s["id"]): {"name": s.get("name"), "parentId": s.get("parentId")}
      for s in data.get("ship") or []
    },
    "cruiseline": {str(c["id"]): c.get("name") for c in data.get("cruiseline") or []},
    "port": {p["code"]: p.get("name") for p in data.get("port") or []},
  }


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
  headless: bool = True,
) -> list[Deal]:
  """擷取 Expedia 上目標港口、指定日期窗口內的航次。"""
  # 延遲匯入：沒裝瀏覽器套件時仍可只跑 icruise
  from patchright.sync_api import sync_playwright

  start = start or date.today()
  end = start + timedelta(days=lookahead_days)

  with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=headless)
    try:
      page = browser.new_page(
        viewport={"width": 1400, "height": 950}, locale="en-US"
      )
      endpoint, headers = _capture_session(page)
      items = _fetch_all(page, endpoint, headers)
      master = _fetch_master(page, endpoint, headers)
    finally:
      browser.close()

  deals = parse_items(items, master, start, end)
  log.info("Expedia：%d 個航程中有 %d 筆落在窗口內", len(items), len(deals))
  return deals
