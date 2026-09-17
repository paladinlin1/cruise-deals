"""雄獅旅遊 travel.liontravel.com 擷取器（台灣旅行社，報價為新台幣）。

搜尋頁是 React SPA，商品清單由前端打這支 JSON API 取得：

  POST https://travel.liontravel.com/search/grouplistinfojson

實測（2026-09-16）**不需要 cookie 或授權標頭**，httpx 直接 POST 就有資料。
用 `TripTypes:"01"`（交通型態＝郵輪）加日期窗口篩選，回傳格式是

  NormGroupList[]            商品（同一條航線的所有團期）
    .TourName                「麗星郵輪｜探索星號｜基隆出發｜那霸．石垣島｜自由行4日」
    .TourDays                天數（4日＝3夜）
    .GroupList[]             團期
      .GoDate                出發日 "2026/10/13"
      .Status                selling／ensure／full
      .StraightLowestPrice   直客價（每人最低價，"18,950"）

實測踩到的幾件事：

  - **一個月內的基隆／東京團期全部「暫時額滿」**（2026-09-16：全站 76 個團期
    有 72 個 Status=full，目標港的 26 個全部額滿）。額滿的價格拿去跨來源比價
    會誤導最低價，所以只收 selling／ensure。這代表這一站在近期窗口常態
    只有零星幾筆，跟百威一樣不是壞掉。
  - **出發港藏在商品名稱裡**，只有「X出發」「X上下」「X上Y下」三種寫法。
    不能整段字串比對——「神戶上基隆下」含「基隆」但是神戶出發。
    回應裡雖然有 `StartFromCityList` 與 `IsCruise` 兩個結構化欄位，但都不能用：
    `IsCruise` 全站 44 個商品都是 False；`StartFromCityList` 是集合城市不是登船港
    （新加坡出發的迪士尼探險號標基隆、東京上下的鑽石公主號標台北）。
  - **東京出發的商品是純船票**：詳情頁寫明「不含國際段機票」「售價兩人一室
    每人艙房費用」，可以跟外國站的 Tokyo 航次直接比價。
    若標題另有「台北出發」之類非目標港的「X出發」，則是機＋船套裝，不收。
  - **同一航次會有兩個供應商**：雄獅自家（TourSource=Lion）與「【主題旅遊】」
    合作商品（GoUni）價格差很多。來源內去重只留最便宜的，與百威同一原則。
  - 查詢 body 要照網站原樣送完整欄位（含一堆 null）；只送幾個欄位時
    同樣的關鍵字會回 0 筆。
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
from .base import ParseError, keep_cheapest

log = logging.getLogger(__name__)

SOURCE = "lion"

# 團期狀態：暫時額滿。其餘（selling 熱銷中、ensure 成團可報名）都收。
FULL_STATUS = "full"

# 翻頁上限：正常一個月窗口約 50 個商品、PageSize 100 只需一頁；
# 這是防對方 TotalPage 回錯值時無限翻下去的保險。
MAX_PAGES = 20

# 商品名稱用全形直線分段
_SEGMENT_SEP = "｜"

# 「X出發」：X 是分隔符號／】／～之後到「出發」之前的字
_DEPART_RE = re.compile(r"(?:^|[｜】～\s])([^｜】～\s]+?)出發")
# 「X上下」（原港來回）或「X上Y下」（單程）：整段就是這個寫法
_BOARD_RE = re.compile(r"^(?P<board>[^上]+?)上(?:下|(?P<leave>[^下]+?)下)$")

# 停靠港那一段以外的段落特徵
_NOTE_RE = re.compile(r"[（(][^）)]*[）)]")
_DAYS_RE = re.compile(r"\d+\s*(?:日|天)")
_TRIP_TYPE_RE = re.compile(r"自由行|自主遊|半自助|團體")
_MARKETING_RE = re.compile(r"航次|航程|出遊|連假|假期|優惠|折\$?\d|艙|禮遇")
_SEA_RE = re.compile(r"海上|公海")
_ROUTE_RE = re.compile(r"^環")  # 「環日本」是航線描述不是停靠港
_PORT_SEP_RE = re.compile(r"[．.、・]")


def search_body(start: date, end: date, page: int) -> dict[str, Any]:
  """組出跟網站前端一模一樣的查詢 body。

  欄位刻意全部照送（含 null）：實測只送幾個欄位時，同樣的條件會回 0 筆。
  """
  return {
    "ArriveID": None,
    "GoDatestart": start.isoformat(),
    "GroupID": None,
    "Keywords": None,
    "IsEnsureGroup": None,
    "IsSold": None,
    "ThemeID": None,
    "TravelPavilionGroupID": None,
    "KeywordsCity": None,
    "TravelType": 0,
    "BuIDs": "",
    "PreferAirlines": None,
    "GoDateEnd": end.isoformat(),
    "DepartureID": "",
    "WeekDay": "",
    "PriceList": None,
    "AirlineIDs": "",
    "TripTypes": config.LION_TRIP_TYPE_CRUISE,
    "Tags": "",
    "SortType": None,
    "Days": "",
    "Page": page,
    "PageSize": config.LION_PAGE_SIZE,
  }


def extract_norm_groups(payload: Any) -> list[dict[str, Any]]:
  """從單頁回應取出商品清單。形狀不對就大聲失敗。"""
  if not isinstance(payload, dict) or "NormGroupList" not in payload:
    raise ParseError("雄獅搜尋 API 回應缺少 NormGroupList——API 可能已改版")
  return list(payload.get("NormGroupList") or [])


def fetch_norm_groups(
  start: date,
  end: date,
  client: httpx.Client | None = None,
  delay_s: float = config.REQUEST_DELAY_S,
  timeout_s: float = 60.0,
) -> list[dict[str, Any]]:
  """打搜尋 API 並翻完所有頁，回傳合併後的商品清單。"""
  headers = {
    "User-Agent": config.USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Origin": "https://travel.liontravel.com",
    "Referer": "https://travel.liontravel.com/search",
  }
  own_client = client is None
  client = client or httpx.Client(timeout=timeout_s, follow_redirects=True)

  groups: list[dict[str, Any]] = []
  try:
    for page in range(1, MAX_PAGES + 1):
      if page > 1:
        time.sleep(delay_s)  # 禮貌延遲
      response = client.post(
        config.LION_SEARCH_URL, json=search_body(start, end, page), headers=headers
      )
      response.raise_for_status()
      try:
        payload = response.json()
      except ValueError as exc:
        # WAF 或維護頁會回 200 的 HTML
        raise ParseError(
          f"雄獅搜尋 API 第 {page} 頁回應不是 JSON（{exc}）——可能被擋或正在維護"
        ) from exc
      groups.extend(extract_norm_groups(payload))

      if page >= int(payload.get("TotalPage") or 1):
        break
  finally:
    if own_client:
      client.close()
  return groups


def parse_route(tour_name: str | None) -> tuple[str, str, str] | None:
  """從商品名稱解析 (出發港正規化名, 出發港原文, 到達港)。

  只認三種寫法：
    「X出發」      -> X 出發、原港來回
    「X上下」      -> X 出發、原港來回
    「X上Y下」     -> X 出發、Y 下船（Y 對不到目標港就照原文放）
  認不出寫法、或 X 不是目標港（基隆／東京／橫濱）時回 None。

  「X出發」優先於「X上Y下」，而且只看第一個：標題同時有「台北出發」與
  「東京上下」的是機＋船套裝（價格含機票），刻意讓它因「台北」不是目標港
  而被丟掉——與百威排除桃園機場出發的商品同一個理由。
  """
  text = _NOTE_RE.sub("", normalize.clean_text(tour_name))
  if not text:
    return None

  board_raw: str | None = None
  leave_raw: str | None = None
  match = _DEPART_RE.search(text)
  if match:
    board_raw = match.group(1)
  else:
    for segment in text.split(_SEGMENT_SEP):
      match = _BOARD_RE.match(segment.strip())
      if match:
        board_raw = match.group("board")
        leave_raw = match.group("leave")
        break
  if board_raw is None:
    return None

  depart = normalize.match_port(board_raw)
  if depart is None:
    return None
  arrive = (normalize.match_port(leave_raw) or leave_raw) if leave_raw else depart
  return depart, board_raw, arrive


def itinerary_ports(tour_name: str | None) -> tuple[str, ...]:
  """從商品名稱取出停靠港。

  「麗星郵輪｜探索星號｜基隆出發｜那霸．石垣島｜自由行4日」 -> ("那霸", "石垣島")
  「…｜海上遊｜…」                                        -> ()

  做法是把明顯不是停靠港的段落（出發港、天數、行程型態、行銷詞、船名）
  排除掉，剩下的當停靠港。純海上航程回空 tuple。
  """
  candidates: list[str] = []
  for segment in _segments(tour_name):
    if "出發" in segment or _BOARD_RE.match(segment):
      continue
    if _DAYS_RE.search(segment) or _TRIP_TYPE_RE.search(segment):
      continue
    if _MARKETING_RE.search(segment) or _SEA_RE.search(segment):
      continue
    if _ROUTE_RE.match(segment) or _is_ship_segment(segment):
      continue
    candidates.append(segment)

  if not candidates:
    return ()
  # 停靠港是分隔符號最多的那一段（「賣點．賣點」這種行銷段也會有分隔符號，
  # 但港口清單通常更長）；平手時取後面那段，因為雄獅把行銷詞放最前面
  chosen = max(
    enumerate(candidates), key=lambda item: (len(_PORT_SEP_RE.findall(item[1])), item[0])
  )[1]
  return tuple(part.strip() for part in _PORT_SEP_RE.split(chosen) if part.strip())


def _segments(tour_name: str | None) -> list[str]:
  """把商品名稱切成去掉括號註記、去掉空白的段落。"""
  text = normalize.clean_text(tour_name)
  if not text:
    return []
  segments = (normalize.clean_text(_NOTE_RE.sub("", s)) for s in text.split(_SEGMENT_SEP))
  return [s for s in segments if s]


def _is_ship_segment(segment: str) -> bool:
  """這一段是不是船名／船公司（「探索星號」「挪威郵輪暢悅號」「麗星郵輪」）。"""
  return (
    segment.endswith("號")
    or normalize.match_alias(segment, config.SHIP_ALIASES) is not None
    or normalize.match_alias(segment, config.CRUISE_LINE_ALIASES) is not None
  )


def ship_segment(tour_name: str | None) -> str:
  """找出商品名稱裡帶船名的那一段；找不到回空字串。

  別名表沒有的新船（「25．26年航程｜挪威郵輪暢悅號｜…」）若拿整段標題去
  `split_ship_and_line`，會在第一個「｜」截斷而把「25．26年航程」當船名——
  dedup_key 與「沒對應到英文名」的警告都會跟著壞掉，維護者看不出該補哪個別名。
  """
  for segment in _segments(tour_name):
    if normalize.match_alias(segment, config.SHIP_ALIASES) or segment.endswith("號"):
      return segment
  return ""


def parse_norm_groups(
  groups: list[dict[str, Any]], start: date, end: date
) -> list[Deal]:
  """把商品清單湊成 Deal 清單（尚未去重）。

  只收目標港出發、可報名、出發日在窗口內的團期。
  """
  if not groups:
    raise ParseError(
      "雄獅搜尋 API 回傳 0 個郵輪商品——全球一個月內不可能沒有郵輪團，API 可能已改版"
    )

  scraped_at = utcnow()
  deals: list[Deal] = []
  for group in groups:
    tour_name = group.get("TourName") or ""
    route = parse_route(tour_name)
    if route is None:
      continue
    for sailing in group.get("GroupList") or []:
      if sailing.get("Status") == FULL_STATUS:
        continue
      deal = _parse_sailing(group, sailing, route, scraped_at)
      if deal is not None and start <= deal.sail_date <= end:
        deals.append(deal)
  return deals


def _parse_sailing(
  group: dict[str, Any],
  sailing: dict[str, Any],
  route: tuple[str, str, str],
  scraped_at: datetime,
) -> Deal | None:
  """解析單一團期。日期或天數不合理時回 None（不讓一團壞掉整批）。"""
  try:
    sail_date = normalize.parse_sail_date(sailing.get("GoDate"))
  except ValueError:
    return None

  nights = int(group.get("TourDays") or 0) - 1
  if nights <= 0:
    return None

  tour_name = group.get("TourName") or ""
  depart_port, depart_raw, arrive_port = route
  # 船名只從帶船名的那一段解析（見 ship_segment）；船公司可能在別段，看整段標題
  ship_name, fallback_raw, _ = normalize.split_ship_and_line(
    ship_segment(tour_name) or tour_name
  )
  ship_raw = normalize.ship_alias_key(tour_name) or fallback_raw
  cruise_line = normalize.match_alias(tour_name, config.CRUISE_LINE_ALIASES) or ""

  return Deal(
    source=SOURCE,
    sail_date=sail_date,
    depart_port=depart_port,
    depart_port_raw=depart_raw,
    arrive_port=arrive_port,
    ports_of_call=itinerary_ports(tour_name),
    ship_name=ship_name,
    cruise_line=cruise_line,
    nights=nights,
    price=normalize.parse_price(sailing.get("StraightLowestPrice")),
    currency="TWD",
    price_note="每人最低價（直客價）",
    detail_url=config.LION_DETAIL_URL.format(
      norm_group_id=group.get("NormGroupID") or "",
      group_id=sailing.get("GroupID") or "",
    ),
    scraped_at=scraped_at,
    ship_name_raw=ship_raw,
  )


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
) -> list[Deal]:
  """擷取雄獅旅遊上基隆／東京／橫濱出發、指定日期窗口內、可報名的郵輪團期。"""
  start = start or date.today()
  end = start + timedelta(days=lookahead_days)

  groups = fetch_norm_groups(start, end)
  deals = keep_cheapest(parse_norm_groups(groups, start, end))

  unmapped = sorted({d.ship_name for d in deals if normalize.is_unmapped_ship(d.ship_name)})
  if unmapped:
    log.warning(
      "雄獅有 %d 個船名沒對應到英文正式名（無法跨來源比價）：%s",
      len(unmapped),
      "、".join(unmapped),
    )

  if not deals:
    # 已確認這是常態：近期團期多為「暫時額滿」，額滿的不收。不是壞掉，不要拋錯。
    log.info(
      "雄獅：%d 個郵輪商品中沒有 %s~%s 由基隆／東京／橫濱出發且可報名的團期"
      "（近期團期多為暫時額滿）",
      len(groups),
      start,
      end,
    )
  else:
    log.info("雄獅：%d 筆", len(deals))
  return deals
