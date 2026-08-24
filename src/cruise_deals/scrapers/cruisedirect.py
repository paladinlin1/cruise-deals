"""cruisedirect.com 擷取器。

該站以 Cloudflare 阻擋一般 HTTP client（httpx、換 UA、甚至 patchright
無頭／有頭都過不了，連 /robots.txt 都回 403）。
唯一實測可通過的是 **SeleniumBase 的 CDP Mode**（`sb.activate_cdp_mode`）。

網站本身是 Drupal，篩選走 facet query string：

    /search-results
      ?f[0]=departure_city:743704
      &f[1]=departure_date:(min:<unix>,max:<unix>)

實測發現：
  - 要用 **/search-results**（完整搜尋）而不是 /cruises/last-minute-cruises。
    後者是策展子集合，其 facet 清單裡查不到基隆，會漏掉整個港口的航次。
    日期由我們自己用時間戳篩，不依賴對方對「last minute」的定義。
  - 一張 article 卡片可含多個 price-table，每個 price-table 是一個出發日期
  - 每個 price-table 有四種房型（Interior/Oceanview/Balcony/Suite），
    "-" 代表該房型無艙位
  - **基隆頁面的出發城市欄位是空的**，必須改用停靠港第一站判斷出發港，
    否則會安靜地漏掉整頁資料
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from selectolax.parser import HTMLParser, Node

from .. import config, normalize
from ..models import Deal, utcnow
from .base import BlockedError, ParseError

log = logging.getLogger(__name__)

SOURCE = "cruisedirect"

BASE = "https://www.cruisedirect.com"
# 用完整搜尋端點，不要用 /cruises/last-minute-cruises（策展子集合，查不到基隆）
SEARCH_PATH = "/search-results"

# 實測取得的 facet id
DEPARTURE_CITY_IDS: dict[str, int] = {
  "Keelung": 743704,
  "Tokyo": 2604,
  "Yokohama": 2752,
}

# logo 的 alt 只給小寫代號，還原成與其他來源一致的完整名稱
CRUISELINE_NAMES: dict[str, str] = {
  "celebrity": "Celebrity Cruises",
  "princess": "Princess Cruises",
  "royal": "Royal Caribbean International",
  "royalcaribbean": "Royal Caribbean International",
  "carnival": "Carnival Cruise Line",
  "norwegian": "Norwegian Cruise Line",
  "msc": "MSC Cruises",
  "costa": "Costa Cruises",
  "holland": "Holland America Line",
  "hollandamerica": "Holland America Line",
  "cunard": "Cunard",
  "disney": "Disney Cruise Line",
  "oceania": "Oceania Cruises",
  "regent": "Regent Seven Seas Cruises",
  "seabourn": "Seabourn",
  "silversea": "Silversea",
  "viking": "Viking",
  "windstar": "Windstar Cruises",
  "azamara": "Azamara",
  "explora": "Explora Journeys",
}

# 挑戰頁的特徵：標題與 DOM 標記各自都足以判定
_BLOCKED_TITLES = ("just a moment", "attention required", "access denied")
_BLOCKED_MARKERS = (
  "challenges.cloudflare.com",
  "cf-chl",
  "_cf_chl_opt",
  "Performing security verification",
)

# "Aug 30, 2026 - Sep 11, 2026 Sun - Fri Bonus Details" -> 取前面的出發日
_DATE_CELL_RE = re.compile(r"([A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})")

# 結果頁標題列的筆數："0 Cruises" / "24 Cruises"
_COUNT_RE = re.compile(r"([\d,]+)\s+Cruises?", re.I)


def matched_count(html: str) -> int | None:
  """讀出頁面宣稱的航次數。找不到這個欄位時回 None。

  這是「這個港口今天真的沒船」與「對方改版了」之間唯一的分界線。
  少了它，某個港口剛好掛零就會被誤判成改版——而且因為解析失敗會中斷
  整個來源，另外兩個港口連抓都不會被抓到。實際發生過（2026-08-23）。
  """
  for node in HTMLParser(html).css("h2.view-header"):
    match = _COUNT_RE.search(node.text())
    if match:
      return int(match.group(1).replace(",", ""))
  return None


def is_blocked(html: str, title: str = "") -> bool:
  """判斷這一頁是不是機器人防護的攔截頁。"""
  if any(marker in (title or "").lower() for marker in _BLOCKED_TITLES):
    return True
  return any(marker in (html or "") for marker in _BLOCKED_MARKERS)


def _unix(day: date, end_of_day: bool = False) -> int:
  moment = datetime.combine(
    day, time.max if end_of_day else time.min, tzinfo=timezone.utc
  )
  return int(moment.timestamp())


def build_search_url(city_id: int, start: date, end: date) -> str:
  """組出 Drupal facet 篩選網址（出發城市 + 出發日期區間）。"""
  city = quote(f"departure_city:{city_id}", safe="")
  dates = quote(f"departure_date:(min:{_unix(start)},max:{_unix(end, True)})", safe="")
  return f"{BASE}{SEARCH_PATH}?f%5B0%5D={city}&f%5B1%5D={dates}"


def parse_sail_date_cell(raw: str) -> date | None:
  """從 "Aug 30, 2026 - Sep 11, 2026 Sun - Fri Bonus Details" 取出發日。"""
  match = _DATE_CELL_RE.search(raw or "")
  if not match:
    return None
  try:
    return normalize.parse_sail_date(match.group(1))
  except ValueError:
    return None


def lowest_of_cabins(cells: list[str]) -> Decimal | None:
  """從各房型的價格儲存格取最低者。

  "-" 代表該房型無艙位，"Select" 之類的按鈕文字也要略過——
  parse_price 對沒有數字的字串一律回 None，剛好都能濾掉。
  """
  prices = [p for p in (normalize.parse_price(c) for c in cells) if p is not None]
  return min(prices) if prices else None


def _text(node: Node | None) -> str:
  return normalize.clean_text(node.text(separator=" ")) if node else ""


def _cruise_line(article: Node) -> str:
  """由 cruiseline logo 的 alt 還原船公司名稱。"""
  logo = article.css_first("[class*=cruiseline-logo] img")
  slug = (logo.attributes.get("alt") or "").strip().lower() if logo else ""
  if not slug:
    return ""
  return CRUISELINE_NAMES.get(slug.replace(" ", ""), slug.title())


def _parse_article(article: Node, scraped_at) -> list[Deal]:
  """解析一張航程卡片，展開成每個出發日期一筆 Deal。"""
  # "Port of Call Tokyo, Japan - Kochi, Japan - ..."
  # 港名本身含逗號，必須用 " - " 分隔而非逗號
  itinerary_raw = _text(article.css_first(".field--name-field-itinerary-id"))
  itinerary_raw = re.sub(r"\s*Itinerary Details\s*$", "", itinerary_raw)
  ports = normalize.split_ports(itinerary_raw, separator=" - ")

  depart_raw = normalize.strip_prefix_label(
    _text(article.css_first(".field--name-field-sailing-departure-city-id"))
  )
  depart_port = normalize.match_port(depart_raw)

  # 基隆的頁面這個欄位是空的，改用停靠港第一站判斷。
  # 沒有這個備援會安靜地漏掉整頁資料。
  if depart_port is None and ports:
    depart_raw = ports[0]
    depart_port = normalize.match_port(depart_raw)

  if depart_port is None:
    return []  # 非目標出發港（防禦性檢查，facet 理應已篩過）

  ship_name = normalize.strip_prefix_label(
    _text(article.css_first(".field--name-field-sailing-ship-id"))
  )
  cruise_line = _cruise_line(article)

  try:
    nights = normalize.parse_nights(
      _text(article.css_first(".field--name-field-sailing-duration"))
    )
  except ValueError:
    nights = 0

  deals: list[Deal] = []
  for table in article.css(".price-table"):
    body_cells = [_text(c) for c in table.css(".price-table-body .price-table-cell")]
    if not body_cells:
      continue
    sail_date = parse_sail_date_cell(body_cells[0])
    if sail_date is None:
      continue

    link = table.css_first("a[href]")
    detail_url = link.attributes.get("href", "") if link else ""

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
        price=lowest_of_cabins(body_cells[1:]),
        currency="USD",
        price_note="各房型最低價（每人，含稅費）",
        detail_url=detail_url,
        scraped_at=scraped_at,
      )
    )
  return deals


def parse_search_page(
  html: str,
  title: str = "",
  start: date | None = None,
  end: date | None = None,
) -> list[Deal]:
  """解析結果頁。

  被擋時拋 BlockedError（進不去），版面看不懂時拋 ParseError（進去了但解析不出來）——
  兩者的處置方式不同，故分開。
  """
  if is_blocked(html, title):
    raise BlockedError(
      "被 Cloudflare 機器人防護擋下（挑戰頁未解除）。其他來源不受影響。"
    )

  tree = HTMLParser(html)
  articles = tree.css("article.node--type-sailing")
  claimed = matched_count(html)

  if not articles:
    if claimed == 0:
      return []  # 這個港口在這個窗口內真的沒有航次，不是改版
    raise ParseError(
      "頁面裡找不到 article.node--type-sailing（頁面宣稱 "
      f"{'未知' if claimed is None else claimed} 筆）——cruisedirect 版面可能已改版"
    )

  scraped_at = utcnow()
  deals: list[Deal] = []
  for article in articles:
    deals.extend(_parse_article(article, scraped_at))

  # 有卡片卻一筆都解析不出來 -> 版面改了。安靜回空清單會讓下游
  # 誤以為「今天真的沒有 deal」，進而把好資料蓋掉。
  if articles and not deals:
    raise ParseError(
      f"頁面有 {len(articles)} 張航程卡片，卻解析出 0 筆——"
      "cruisedirect 版面可能已改版"
    )

  if start and end:
    deals = [d for d in deals if start <= d.sail_date <= end]
  return deals


# "host:port" 或 "scheme://host:port"（host 可為網域或 IP）
_PROXY_RE = re.compile(
  r"^(?:(?P<scheme>socks5h|socks5|socks4|https?)://)?"
  r"(?P<host>[\w.\-]+):(?P<port>\d{1,5})$"
)


def proxy_setting() -> str | None:
  """從 CRUISEDIRECT_PROXY 環境變數讀取代理設定。

  GitHub Actions 的資料中心 IP 會被 Cloudflare 升級成人工勾選框（實測點了也不過），
  但家用住宅 IP 可以自動放行。把流量導過家用路由器的 SSH SOCKS5 通道即可。

  未設定或格式不對時回 None（直連），不讓瀏覽器因設定錯誤而啟動失敗。
  """
  raw = os.environ.get("CRUISEDIRECT_PROXY", "").strip()
  if not raw:
    return None
  match = _PROXY_RE.match(raw)
  if not match:
    log.warning("CRUISEDIRECT_PROXY 格式無法辨識（%r），改用直連", raw)
    return None

  scheme = match.group("scheme") or "socks5"
  # socks5h 是 curl 的寫法，Chrome 不認得，會**安靜地忽略整個代理設定**改走直連。
  # Chrome 的 socks5 本來就會把網域交給代理端解析，語意相同。
  if scheme == "socks5h":
    scheme = "socks5"
  return f"{scheme}://{match.group('host')}:{match.group('port')}"


def _save_debug(sb, city_name: str, html: str) -> Path | None:
  """把失敗當下的現場存下來，CI 上會當成 artifact 上傳供診斷。

  回傳 HTML 的存檔路徑，讓呼叫端可以把它寫進錯誤訊息——否則執行報告上
  只看得到「版面可能已改版」，卻不知道要去哪裡看現場。
  """
  try:
    config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DEBUG_DIR / f"cruisedirect_{city_name}.html"
    path.write_text(html, encoding="utf-8")
  except Exception as exc:  # noqa: BLE001 - 存不下來也不該影響主流程
    log.debug("儲存除錯 HTML 失敗：%s", exc)
    return None

  # 截圖另外處理：截不到不影響 HTML 已經存下來這件事
  try:
    sb.save_screenshot(str(config.DEBUG_DIR / f"cruisedirect_{city_name}.png"))
  except Exception as exc:  # noqa: BLE001
    log.debug("儲存除錯截圖失敗：%s", exc)

  return path


def parse_or_save(
  sb,
  city_name: str,
  html: str,
  title: str,
  start: date | None = None,
  end: date | None = None,
) -> list[Deal]:
  """解析結果頁；解析不出來時先把現場存下來再把例外往上拋。

  解析失敗才是最需要現場的時候——原本只有「被擋下」那條路徑會存檔，
  導致對方改版時 CI 上什麼都沒留下，事後完全無從比對。

  重建例外時用 type(exc)，才不會把 BlockedError 降級成 ParseError；
  這兩者的處置方式不同（見 base.py）。
  """
  try:
    return parse_search_page(html, title, start, end)
  except ParseError as exc:
    saved = _save_debug(sb, city_name, html)
    if saved is None:
      raise
    raise type(exc)(f"{exc}（現場已存到 {saved}）") from exc


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
  headless: bool = True,
  wait_s: int = 12,
) -> list[Deal]:
  """擷取 cruisedirect 上東京／橫濱出發、指定窗口內的航次。

  headless 參數保留是為了與其他 scraper 介面一致，但 UC 模式在無頭下
  過不了挑戰，故實際一律以 xvfb（Linux）或真實視窗（Windows）執行。
  """
  # 延遲匯入：沒裝 seleniumbase 時仍可只跑其他來源
  from seleniumbase import SB

  start = start or date.today()
  end = start + timedelta(days=lookahead_days)

  collected: dict[tuple, Deal] = {}
  failures: list[str] = []
  parse_errors: list[ParseError] = []

  # Linux 上以 xvfb 提供虛擬顯示（GitHub Actions 需要）；Windows 直接開視窗
  import sys

  use_xvfb = sys.platform.startswith("linux")

  proxy = proxy_setting()
  if proxy:
    log.info("cruisedirect 透過代理連線：%s", proxy)

  with SB(uc=True, xvfb=use_xvfb, locale="en", proxy=proxy) as sb:
    for city_name, city_id in DEPARTURE_CITY_IDS.items():
      url = build_search_url(city_id, start, end)
      try:
        sb.activate_cdp_mode(url)
        sb.sleep(wait_s)
        html = sb.get_page_source()
        title = sb.get_title()

        # 資料中心 IP（如 GitHub Actions）上，Cloudflare 常從自動放行
        # 升級成需要點擊的 Turnstile 核取方塊。試著點掉它再等一次。
        if is_blocked(html, title):
          # CDP 模式下要用 sb.cdp.click_captcha()；
          # sb.uc_gui_click_captcha() 是 UC 模式的 API，在這裡會 AttributeError。
          log.info("cruisedirect %s 仍在挑戰頁，嘗試點擊 Turnstile", city_name)
          try:
            sb.cdp.click_captcha()
          except Exception as exc:  # noqa: BLE001 - 沒有可點的元素也算正常
            log.info("點擊 Turnstile 未成功（%s: %s）", type(exc).__name__, exc)
          sb.sleep(wait_s)
          html = sb.get_page_source()
          title = sb.get_title()

        if is_blocked(html, title):
          _save_debug(sb, city_name, html)
          failures.append(f"{city_name}: 挑戰未解除")
          continue

        page_deals = parse_or_save(sb, city_name, html, title, start, end)
        for deal in page_deals:
          collected[deal.dedup_key] = deal
        log.info("cruisedirect %s：%d 筆", city_name, len(page_deals))
      except ParseError as exc:
        # 單一城市解析失敗不該中斷其他城市：只有那一頁改版、或那個港口
        # 當天掛零，不代表另外兩個港口也拿不到資料。
        parse_errors.append(exc)
        failures.append(f"{city_name}: {exc}")
        log.warning("cruisedirect %s 解析失敗：%s", city_name, exc)
      except Exception as exc:  # noqa: BLE001 - 單一城市失敗不該中斷其他城市
        failures.append(f"{city_name}: {type(exc).__name__}: {exc}")
        log.warning("cruisedirect %s 擷取失敗：%s", city_name, exc)

  if failures and not collected:
    # 每個城市都失敗了才讓整個來源失敗。型別要選對，因為處置方式不同：
    # 全部都是被擋下 -> BlockedError（改解析器沒有用）
    # 只要有一個是解析失敗 -> ParseError（去看 debug/ 裡存下來的現場）
    raise (ParseError if parse_errors else BlockedError)("；".join(failures))
  if failures:
    log.warning("cruisedirect 部分城市失敗：%s", "；".join(failures))

  return list(collected.values())
