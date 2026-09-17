"""易遊網 vacation.eztravel.com.tw 擷取器（台灣旅行社，報價為新台幣）。

Next.js 網站，資料在頁面的 `<script id="__NEXT_DATA__">` 裡，結構化程度是
台灣站裡最好的。但整站有 Incapsula 保護：httpx 直接請求只會拿到 212 bytes 的
挑戰頁（含 `_Incapsula_Resource`）。實測（2026-09-17）patchright 無頭可過，
而且**只要第一頁用瀏覽器載入過，之後的請求用 `page.request.get()` 就行**
（共用 cookie、走瀏覽器的網路堆疊，每次 0.2 秒，不必再渲染頁面）。

列表頁：`/pkgfrn/results/KEE/{航線代碼}?depDateFrom=YYYYMMDD&depDateTo=YYYYMMDD&pageSize=100`

  initialState.search.searchResults[]     商品
    .prodNm / .travelDay / .tourCitysNm    名稱、天數、停靠城市（結構化）
    .minPrice1                            **所有艙等×佔床人數的最低價**（3／4 人房）
    .otherSaleDts[]                       窗口內的出發日
      .saleDt / .prodUrl / .fullStatus    "END" 是關團

商品頁：`/pkgfrn/introduction/{prodNo}/{saleDt}`

  initialState.introduction.server.introData
    .pfProPrice4Introductions[]           逐艙等×佔床人數×成人／孩童 的價格
    .routeInfo.routes[]                   逐日停靠城市（{day, city}，海上日寫「海上巡航」）

實測踩到的幾件事：

  - **列表的 minPrice1 不能直接用**：那是 4 人房的每人價（探索星號 3 日 6,325），
    其他來源都是 2 人一室；同一航次商品頁的雙人房成人是 8,000，才跟雄獅對得上。
    所以每個出發日都進一次商品頁取「雙人房 × 成人」各艙等的最低價，
    商品頁拿不到時才退回列表價並在 price_note 註明。
  - **沒有「全部航線」的查詢**：父代碼（331 亞洲航線）會回「系統升級中」，
    要逐個葉節點航線查。基隆出發散在沖繩、九州、日韓、亞洲多國、海上巡遊等航線。
  - **出發港是結構化欄位**（departArea），不用猜標題；桃園機場出發的是機＋船套裝，
    東京／橫濱出發的藏在「海外登船」裡且登船港要從標題猜，目前都不收。
    但「蘇澳出發，基隆返回」也被歸在基隆港分類下，標題有單程註記時要再看登船港。
  - **列表的 tourCitysNm 是商品群組的標籤，不是該航次的行程**：富士號 5 天的商品
    標「與那國島、石垣島、沖繩」，實際只停那霸。停靠港與到達港要用商品頁的
    routeInfo.routes。
  - 列表預設一頁只回 12 筆而且 page 參數無效；`pageSize` 參數有效，一次要完，
    回應的 pageConfig.total 仍比拿到的多就視為截斷、大聲失敗。
  - 站方會把 depDateFrom 往後推到約今天＋2（要 0917 回 0919），最近兩天的出發日
    本來就不會出現在列表上。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .. import config, normalize
from ..models import Deal, utcnow
from .base import BlockedError, ParseError, keep_cheapest

log = logging.getLogger(__name__)

SOURCE = "eztravel"

# 出發日狀態：關團。其餘（"NONE"）都收。
CLOSED_STATUS = "END"

# 商品頁價格列：雙人房（htlNum）× 成人（cond2Type）× 佔床（cond3Type）
DOUBLE_ROOM = "2"
ADULT = "1"
OCCUPYING_BED = "1"

PRICE_NOTE_INTRO = "雙人房成人（每人）"
PRICE_NOTE_LIST = "列表最低價（每人，可能為 3／4 人房）"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# 航線城市後面的括號註記：「那霸 (沖繩)」
_NOTE_RE = re.compile(r"[（(][^）)]*[）)]")
# routeInfo.routes 裡不是港口的天：「海上巡航」
_SEA_DAY_RE = re.compile(r"海上|公海")
# 標題開頭的【】群：促銷文案「【三大好禮全含｜…】」會排在船名「【三井海洋郵輪富士號船票】」前面
_BRACKET_RE = re.compile(r"【([^】]*)】")
# 標題裡的單程說明：「(基隆出發，蘇澳返回)」→ (登船港, 下船港)
_ONE_WAY_RE = re.compile(
  r"[（(]\s*(?P<board>[^（()）,，]+?)出發\s*[，,]\s*(?P<leave>[^（()）]+?)返回\s*[）)]"
)

# 取得單一網址 __NEXT_DATA__ 狀態的函式；真實版本走瀏覽器，測試時用假的
Fetch = Callable[[str], Any]


@dataclass(frozen=True)
class Sailing:
  """列表上一個「商品 × 出發日」。"""

  product: dict[str, Any]
  sail_date: date
  detail_url: str


def results_url(route_code: int, start: date, end: date) -> str:
  return config.EZTRAVEL_RESULTS_URL.format(
    departure=config.EZTRAVEL_DEPARTURE,
    route_code=route_code,
    start=start.strftime("%Y%m%d"),
    end=end.strftime("%Y%m%d"),
    page_size=config.EZTRAVEL_PAGE_SIZE,
  )


def intro_url(prod_no: str, sail_date: date) -> str:
  return config.EZTRAVEL_INTRO_URL.format(prod_no=prod_no, sale_dt=sail_date.strftime("%Y%m%d"))


def extract_state(html: str) -> dict[str, Any]:
  """從頁面 HTML 取出 __NEXT_DATA__ 的 initialState。

  分清楚兩種拿不到的情況：被 Incapsula 擋（BlockedError，改解析器沒用）
  與版面改了（ParseError）。**正常頁面也會嵌 `/_Incapsula_Resource` 監控腳本**，
  所以判斷順序是先找 __NEXT_DATA__，找不到才看是不是挑戰頁。
  """
  match = _NEXT_DATA_RE.search(html)
  if not match:
    if "_Incapsula_Resource" in html:
      raise BlockedError("易遊網回的是 Incapsula 挑戰頁——被機器人防護擋下")
    raise ParseError("易遊網頁面裡沒有 __NEXT_DATA__——版面可能已改版")
  try:
    data = json.loads(match.group(1))
  except ValueError as exc:
    raise ParseError(f"易遊網 __NEXT_DATA__ 不是合法 JSON：{exc}") from exc
  page_props = (data.get("props") or {}).get("pageProps") or {}
  return page_props.get("initialState") or {}


def extract_results(state: Any) -> list[dict[str, Any]]:
  """從列表頁狀態取出商品清單。形狀不對或搜尋失敗就大聲失敗。"""
  search = state.get("search") if isinstance(state, dict) else None
  if not isinstance(search, dict) or "searchResults" not in search:
    raise ParseError("易遊網列表狀態缺少 search.searchResults——版面可能已改版")
  status = search.get("searchStatus")
  if status != "SUCCESS":
    raise ParseError(f"易遊網搜尋狀態為 {status!r}（不是 SUCCESS）")
  results = list(search.get("searchResults") or [])

  # 網址已帶 pageSize 一次要完；宣稱的 total 還是比拿到的多就是被分頁截斷了，
  # 安靜漏掉第二頁的航次會讓下游以為「今天就這麼少」
  total = ((search.get("searchInfo") or {}).get("pageConfig") or {}).get("total")
  try:
    total = int(total)
  except (TypeError, ValueError):
    total = None
  if total is not None and total > len(results):
    raise ParseError(f"易遊網列表宣稱有 {total} 個商品卻只回 {len(results)} 個——分頁被截斷")
  return results


def extract_intro(state: Any) -> dict[str, Any] | None:
  """從商品頁狀態取出 introData；商品頁 404 時狀態是空的，回 None 讓呼叫端退回列表價。"""
  if not isinstance(state, dict):
    return None
  server = (state.get("introduction") or {}).get("server")
  if not isinstance(server, dict):
    return None
  intro = server.get("introData")
  return intro if isinstance(intro, dict) else None


def one_way_ports(prod_name: str | None) -> tuple[str, str] | None:
  """標題裡「(X出發，Y返回)」的 (X, Y)；沒有這種寫法回 None。"""
  match = _ONE_WAY_RE.search(normalize.clean_text(prod_name))
  if not match:
    return None
  return match.group("board").strip(), match.group("leave").strip()


def is_keelung_departure(product: dict[str, Any]) -> bool:
  """這個商品真的從基隆港登船嗎？

  departArea 是該站的「出發地分類」：桃園機場出發的是機＋船套裝、海外登船的
  登船港無法確定，都不收。但**「蘇澳出發，基隆返回」也被歸在基隆港分類下**，
  所以標題有單程說明時要再看登船港是不是基隆。
  """
  if normalize.clean_text(product.get("departArea")) != config.EZTRAVEL_DEPARTURE_NAME:
    return False
  one_way = one_way_ports(product.get("prodNm"))
  return one_way is None or normalize.match_port(one_way[0]) == "Keelung"


def sailings_in_window(
  results: list[dict[str, Any]], start: date, end: date
) -> list[Sailing]:
  """把列表商品攤成「商品 × 出發日」，只留基隆港登船、未關團、日期在窗口內的。"""
  sailings: list[Sailing] = []
  for product in results:
    if not is_keelung_departure(product):
      continue
    if not product.get("pfProdNo"):
      log.warning("易遊網有商品沒有編號（%s），略過", (product.get("prodNm") or "")[:40])
      continue
    if _nights(product) <= 0:
      log.warning("易遊網商品 %s 的天數不合理（%r），略過", product.get("pfProdNo"), product.get("travelDay"))
      continue
    for entry in product.get("otherSaleDts") or []:
      if entry.get("fullStatus") == CLOSED_STATUS:
        continue
      try:
        sail_date = normalize.parse_sail_date(entry.get("saleDt"))
      except (ValueError, TypeError):
        continue
      if not start <= sail_date <= end:
        continue
      detail_url = entry.get("prodUrl") or intro_url(product.get("pfProdNo") or "", sail_date)
      sailings.append(Sailing(product, sail_date, detail_url))
  return sailings


def _nights(product: dict[str, Any]) -> int:
  """travelDay（天）換成夜數；缺漏或不是數字時回 0 以下，讓呼叫端略過。"""
  try:
    return int(product.get("travelDay") or 0) - 1
  except (TypeError, ValueError):
    return 0


def double_occupancy_price(intro: dict[str, Any]) -> Decimal | None:
  """商品頁裡「雙人房 × 成人 × 佔床」各艙等的最低每人價；沒有這種列時回 None。

  欄位用 str() 比對：站方目前給字串，哪天改成整數也不該讓整站安靜退回 3／4 人房價。
  """
  rows = intro.get("pfProPrice4Introductions") or []
  prices = [
    normalize.parse_price(row.get("price"))
    for row in rows
    if str(row.get("htlNum")) == DOUBLE_ROOM
    and str(row.get("cond2Type")) == ADULT
    and str(row.get("cond3Type", OCCUPYING_BED)) == OCCUPYING_BED
  ]
  prices = [p for p in prices if p is not None]
  if not prices and rows:
    log.warning("易遊網商品頁有 %d 列價格卻沒有「雙人房×成人」列——欄位格式可能改了", len(rows))
  return min(prices) if prices else None


def route_ports(intro: dict[str, Any] | None) -> tuple[str, ...]:
  """商品頁 routeInfo.routes 的逐日城市，去掉海上日與括號註記。

  「基隆 → 海上巡航 → 那霸 (沖繩) → 海上巡航 → 蘇澳」 -> ("基隆", "那霸", "蘇澳")
  第一個是登船港、最後一個是下船港、中間是停靠港。沒有 routeInfo 回空 tuple。
  """
  ports: list[str] = []
  for stop in ((intro or {}).get("routeInfo") or {}).get("routes") or []:
    city = normalize.clean_text(_NOTE_RE.sub("", stop.get("city") or ""))
    if city and not _SEA_DAY_RE.search(city):
      ports.append(city)
  return tuple(ports)


def arrive_port(intro: dict[str, Any] | None, prod_name: str, depart_port: str) -> str:
  """航程結束港：依序看商品頁航線的最後一站、標題的「（X出發，Y返回）」，都沒有就是原港來回。"""
  route = route_ports(intro)
  if route:
    return normalize.match_port(route[-1]) or route[-1]

  one_way = one_way_ports(prod_name)
  if one_way:
    return normalize.match_port(one_way[1]) or one_way[1]

  return depart_port


def ship_title(prod_name: str) -> str:
  """把促銷括號從標題前面拿掉，只留從船名那個【】開始的部分。

  「【三大好禮全含｜小費・Wi-Fi・船上消費金】【挪威郵輪暢悅號船票】…」若整段拿去
  split_ship_and_line，別名表沒有的船會把第一個【】的促銷文案當船名。
  「船票」也一併去掉，它不是船名的一部分。
  """
  text = prod_name.replace("船票", "")
  for match in _BRACKET_RE.finditer(text):
    inside = match.group(1)
    if (
      inside.endswith("號")
      or normalize.match_alias(inside, config.SHIP_ALIASES)
      or normalize.match_alias(inside, config.CRUISE_LINE_ALIASES)
    ):
      return text[match.start() :]
  return text


def build_deal(
  sailing: Sailing, intro: dict[str, Any] | None, scraped_at: datetime | None
) -> Deal:
  """把一個出發日湊成 Deal。有商品頁就用雙人房成人價，沒有就退回列表價。"""
  product = sailing.product
  prod_name = normalize.clean_text(product.get("prodNm"))

  price = double_occupancy_price(intro) if intro else None
  if price is not None:
    price_note = PRICE_NOTE_INTRO
  else:
    price = normalize.parse_price(product.get("minPrice1"))
    price_note = PRICE_NOTE_LIST

  depart_raw = normalize.clean_text(product.get("departArea"))
  depart_port = normalize.match_port(depart_raw) or "Keelung"
  ship_name, fallback_raw, _ = normalize.split_ship_and_line(ship_title(prod_name))
  cruise_line = normalize.match_alias(prod_name, config.CRUISE_LINE_ALIASES) or ""

  # 停靠港以商品頁的航線為準（列表的 tourCitysNm 是商品群組標籤，會多列沒停的港）
  route = route_ports(intro)
  ports = route[1:-1] if len(route) >= 2 else tuple(
    normalize.clean_text(city) for city in product.get("tourCitysNm") or [] if city
  )

  return Deal(
    source=SOURCE,
    sail_date=sailing.sail_date,
    depart_port=depart_port,
    depart_port_raw=depart_raw,
    arrive_port=arrive_port(intro, prod_name, depart_port),
    ports_of_call=ports,
    ship_name=ship_name,
    cruise_line=cruise_line,
    nights=_nights(product),
    price=price,
    currency="TWD",
    price_note=price_note,
    detail_url=sailing.detail_url,
    scraped_at=scraped_at or utcnow(),
    ship_name_raw=normalize.ship_alias_key(prod_name) or fallback_raw,
  )


def collect(fetch: Fetch, start: date, end: date) -> list[Deal]:
  """整個流程：查每條航線的列表 → 每個出發日進商品頁 → 去重。

  `fetch(url)` 回該網址的 initialState。商品頁失敗只記警告並退回列表價，
  不讓一筆拖垮整批；列表失敗則往上拋（那代表整個來源有問題）。
  """
  sailings: list[Sailing] = []
  seen: set[tuple[str, date]] = set()
  for code in config.EZTRAVEL_ROUTE_CODES:
    results = extract_results(fetch(results_url(code, start, end)))
    for sailing in sailings_in_window(results, start, end):
      key = (sailing.product.get("pfProdNo") or "", sailing.sail_date)
      if key in seen:
        continue  # 同一商品可能同時掛在兩條航線下
      seen.add(key)
      sailings.append(sailing)

  scraped_at = utcnow()
  deals: list[Deal] = []
  intros_with_prices = 0
  for sailing in sailings:
    try:
      intro = extract_intro(fetch(sailing.detail_url))
    except BlockedError:
      # 中途被擋代表 cookie 已失效；退回列表價會讓整站報價系統性偏低卻回報成功
      raise
    except Exception as exc:  # noqa: BLE001 - 單一商品頁壞掉只影響那一筆的價格精度
      log.warning("易遊網商品頁 %s 讀取失敗（退回列表價）：%s", sailing.detail_url, exc)
      intro = None
    else:
      if intro is None:
        log.warning("易遊網商品頁 %s 沒有價格表，退回列表價", sailing.detail_url)
      elif intro.get("pfProPrice4Introductions"):
        intros_with_prices += 1
    deals.append(build_deal(sailing, intro, scraped_at))

  priced = [d for d in deals if d.price_note == PRICE_NOTE_INTRO]
  if intros_with_prices and not priced:
    raise ParseError(
      f"易遊網 {intros_with_prices} 個商品頁都有價格表卻一筆「雙人房×成人」都對不到"
      "——價格表欄位可能已改版，不能整站退回 3／4 人房價"
    )
  return _prefer_intro_prices(priced, [d for d in deals if d.price_note != PRICE_NOTE_INTRO])


def _prefer_intro_prices(priced: list[Deal], fallback: list[Deal]) -> list[Deal]:
  """來源內去重，但雙人房價永遠優先：退回的列表價是 3／4 人房價，數字小不代表便宜。

  同一航次拆成兩個商品時，若 A 的商品頁成功、B 的商品頁暫時失敗，
  直接 keep_cheapest 會讓 B 的 6,325 壓過 A 的 8,000——那正是進商品頁要避免的失真。
  """
  result = keep_cheapest(priced)
  taken = {d.dedup_key for d in result}
  result.extend(keep_cheapest([d for d in fallback if d.dedup_key not in taken]))
  return result


def _wait_for_next_data(page, timeout_s: float = 45.0) -> str:
  """等頁面帶著 __NEXT_DATA__ 載入完成（Incapsula 挑戰頁會自己重新導向）。"""
  deadline = time.monotonic() + timeout_s
  html = ""
  while True:
    try:
      html = page.content()
    except Exception:  # noqa: BLE001 - 頁面正在導向時 Playwright 會拒絕給內容，等下一輪
      pass
    if "__NEXT_DATA__" in html or time.monotonic() >= deadline:
      return html
    page.wait_for_timeout(1000)


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
  headless: bool = True,
) -> list[Deal]:
  """擷取易遊網上基隆港出發、指定日期窗口內、可報名的郵輪航次。"""
  # 延遲匯入：沒裝瀏覽器套件時仍可只跑純 httpx 的來源
  from patchright.sync_api import sync_playwright

  start = start or date.today()
  end = start + timedelta(days=lookahead_days)

  with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=headless)
    try:
      page = browser.new_page(viewport={"width": 1400, "height": 950}, locale="zh-TW")
      # 第一頁用真實瀏覽器載入，讓 Incapsula 發 cookie；之後全走 page.request
      first_url = results_url(config.EZTRAVEL_ROUTE_CODES[0], start, end)
      page.goto(first_url, wait_until="domcontentloaded", timeout=60_000)
      cache = {first_url: extract_state(_wait_for_next_data(page))}

      def fetch(url: str) -> dict[str, Any]:
        if url in cache:
          return cache.pop(url)
        time.sleep(config.REQUEST_DELAY_S)  # 禮貌延遲
        response = page.request.get(url)
        if response.status in (403, 429):
          raise BlockedError(f"易遊網回 HTTP {response.status}（{url}）——被機器人防護擋下")
        return extract_state(response.text())

      deals = collect(fetch, start, end)
    finally:
      browser.close()

  unmapped = sorted({d.ship_name for d in deals if normalize.is_unmapped_ship(d.ship_name)})
  if unmapped:
    log.warning(
      "易遊網有 %d 個船名沒對應到英文正式名（無法跨來源比價）：%s",
      len(unmapped),
      "、".join(unmapped),
    )
  log.info("易遊網：%d 筆", len(deals))
  return deals
