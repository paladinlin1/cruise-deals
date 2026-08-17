"""百威旅遊 bwt.com.tw 擷取器（台灣旅行社，報價為新台幣）。

`/destination/5/SAREA00721` 那個頁面本身**不含任何商品**，商品是前端再打 API 拿的。
從 `/Scripts/dist/destination/index.bundle.js` 挖出來的端點有兩個：

  - `…/Shop/Present/GetMainGroupInfoByWebSite/5`     只有骨架，price 全是 99999999，**不可用**
  - `…/Shop/Present/GetMainGroupInfoByWebSiteSSE/5`  SSE 串流，才有真正的團期與價格

SSE 依序推三個步驟：
  step1  主行程清單（含 subArea、hasCruisePrice）
  step2  每個 mainGroupCode 底下的團期（leaveDate／groupDay／price／groupCode）
  step3  `{"message": "資料傳輸完成", "totalGroups": N}`

`5` 是「遊輪．河輪」這個 shop 的 sn，一次涵蓋全站郵輪商品，
比逐個 SAREA 抓省事，也不會漏掉分類調整。

實測踩到的兩個坑：

  - **TLS**：該站憑證鏈缺 Subject Key Identifier，Python 3.13 起
    `ssl.create_default_context()` 預設開啟 `VERIFY_X509_STRICT`，會直接拒絕連線
    （`CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier`）。
    解法是只清掉那個嚴格旗標，**不要用 `verify=False`**（那會連憑證鏈都不驗）。
  - **30 天內常態 0 筆**：該站郵輪團期最早是三個月後，多數落在明年。
    這是正常狀態不是壞掉，所以過濾後 0 筆時不拋錯。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import certifi
import httpx

from .. import config, normalize
from ..models import Deal, utcnow
from .base import ParseError

log = logging.getLogger(__name__)

SOURCE = "bwt"

# 骨架回應用這個數字當「沒有價格」的哨兵值
NO_PRICE = 99999999

# 商品名稱：【船公司．船名】航線敘述～停靠港、停靠港(加購說明)
_TITLE_BRACKET_RE = re.compile(r"^\s*【[^】]*】\s*")
_TRAILING_NOTE_RE = re.compile(r"[（(][^）)]*[）)]\s*$")
_DAYS_TAIL_RE = re.compile(r"[0-9０-９一二三四五六七八九十]+\s*天.*$")
_TOUR_WORDS_RE = re.compile(r"自主遊|自由行|之旅|巡航|巡遊|遊$")


def ssl_context() -> ssl.SSLContext:
  """建立可連上 bwt 的 SSL context。

  只放寬 RFC 5280 的嚴格檢查（對方憑證缺 Subject Key Identifier），
  憑證鏈本身仍然完整驗證——所以不是 `verify=False`。
  """
  context = ssl.create_default_context(cafile=certifi.where())
  context.verify_flags &= ~ssl.VERIFY_X509_STRICT
  return context


def read_sse(lines: Iterable[str]) -> dict[str, Any]:
  """把 SSE 串流的逐行文字整理成 {"step1": …, "step2": …, "step3": …}。

  純函式，方便直接餵存下來的串流做離線測試。
  """
  steps: dict[str, Any] = {}
  for line in lines:
    if not line.startswith("data: "):
      continue
    payload = line[6:].strip()
    if not payload or payload == "[DONE]":
      continue
    try:
      obj = json.loads(payload)
    except ValueError:
      log.warning("略過無法解析的 SSE 片段: %.80s", payload)
      continue
    step = obj.get("step") if isinstance(obj, dict) else None
    if step:
      steps[step] = obj.get("data")
  return steps


def itinerary_ports(group_name: str | None) -> tuple[str, ...]:
  """從商品名稱取出停靠港。

  "【MSC郵輪．榮耀號】日韓自主遊６天～鹿兒島、濟州"      -> ("鹿兒島", "濟州")
  "【MSC郵輪．榮耀號】宮古島、沖繩、石垣島自主遊５天"   -> ("宮古島", "沖繩", "石垣島")
  """
  text = normalize.clean_text(group_name)
  if not text:
    return ()
  text = _TRAILING_NOTE_RE.sub("", _TITLE_BRACKET_RE.sub("", text))

  # 有「～」時港口在後面，沒有的話在「N天」之前
  head, sep, tail = text.partition("～")
  text = tail if sep else _DAYS_TAIL_RE.sub("", head)
  text = _TOUR_WORDS_RE.sub("", normalize.clean_text(text))

  return tuple(part.strip() for part in text.split("、") if part.strip())


def parse_steps(steps: dict[str, Any], start: date, end: date) -> list[Deal]:
  """把 SSE 的三個步驟湊成 Deal 清單。

  只收基隆港出發的純郵輪：桃園機場出發的是「機票＋郵輪」套裝，
  價格含機票，跟外國站的每人船票價不能比。
  """
  if steps.get("step3") is None:
    raise ParseError(
      "百威 SSE 串流沒有收到 step3（資料傳輸完成）——串流中斷或 API 已改版。"
      "此時的資料是不完整的，不能當成「今天就這麼少」。"
    )

  mains = {
    m["mainGroupCode"]: m
    for m in steps.get("step1") or []
    if m.get("mainGroupCode")
  }
  if not mains:
    raise ParseError("百威 step1 沒有任何主行程——API 可能已改版")

  scraped_at = utcnow()
  deals: list[Deal] = []

  for detail in steps.get("step2") or []:
    code = detail.get("mainGroupCode")
    main = mains.get(code)
    if main is None:
      continue
    if normalize.clean_text(detail.get("departure")) != config.BWT_DEPARTURE:
      continue
    if not main.get("hasCruisePrice"):
      continue  # 濾掉「單訂船票」之類的渡輪商品

    depart_port = normalize.match_port(detail.get("departure"))
    if depart_port is None:
      continue

    for group in detail.get("groups") or []:
      deal = _parse_group(group, main, depart_port, detail, scraped_at)
      if deal is not None and start <= deal.sail_date <= end:
        deals.append(deal)

  return deals


def _parse_group(
  group: dict[str, Any],
  main: dict[str, Any],
  depart_port: str,
  detail: dict[str, Any],
  scraped_at,
) -> Deal | None:
  """解析單一團期。日期或天數不合理時回 None（不讓一團壞掉整批）。"""
  try:
    sail_date = normalize.parse_sail_date(group.get("leaveDate"))
  except ValueError:
    return None

  nights = max(int(group.get("groupDay") or 0) - 1, 0)
  if nights == 0:
    return None

  raw_price = group.get("price")
  price = None if raw_price in (None, NO_PRICE) else normalize.parse_price(raw_price)

  group_name = group.get("groupName") or main.get("groupName") or ""
  ship_name, ship_raw, cruise_line = normalize.split_ship_and_line(group_name)
  ports = itinerary_ports(group_name)
  group_code = group.get("groupCode") or ""

  return Deal(
    source=SOURCE,
    sail_date=sail_date,
    depart_port=depart_port,
    depart_port_raw=normalize.clean_text(detail.get("departure")),
    arrive_port=depart_port,  # 基隆港出發的都是原港來回
    ports_of_call=ports,
    ship_name=ship_name,
    cruise_line=cruise_line,
    nights=nights,
    price=price,
    currency="TWD",
    price_note="團費（每人，2 人一室）",
    detail_url=(
      config.BWT_TOUR_URL.format(group_code=group_code) if group_code else ""
    ),
    scraped_at=scraped_at,
    ship_name_raw=ship_raw,
  )


def _keep_cheaper(collected: dict[tuple, Deal], deal: Deal) -> None:
  """來源內先去重：同一航次會有多個 groupCode（不同團位、加購方案），只留最便宜的。"""
  existing = collected.get(deal.dedup_key)
  if existing is None:
    collected[deal.dedup_key] = deal
    return
  if deal.price is not None and (existing.price is None or deal.price < existing.price):
    collected[deal.dedup_key] = deal


def fetch_steps(timeout_s: float = 120.0) -> dict[str, Any]:
  """打 SSE 端點並收完整個串流。"""
  url = config.BWT_SSE_URL.format(shop_sn=config.BWT_SHOP_SN)
  headers = {
    "User-Agent": config.USER_AGENT,
    "Accept": "text/event-stream",
    "Accept-Language": "zh-TW,zh;q=0.9",
  }
  with httpx.Client(
    headers=headers, timeout=timeout_s, verify=ssl_context(), follow_redirects=True
  ) as client:
    with client.stream("GET", url) as response:
      response.raise_for_status()
      return read_sse(response.iter_lines())


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
) -> list[Deal]:
  """擷取百威旅遊上基隆港出發、指定日期窗口內的郵輪航次。"""
  start = start or date.today()
  end = start + timedelta(days=lookahead_days)

  steps = fetch_steps()
  deals = parse_steps(steps, start, end)

  collected: dict[tuple, Deal] = {}
  unmapped: set[str] = set()
  for deal in deals:
    if normalize.is_unmapped_ship(deal.ship_name):
      unmapped.add(deal.ship_name)
    _keep_cheaper(collected, deal)

  if unmapped:
    log.warning(
      "百威有 %d 個船名沒對應到英文正式名（無法跨來源比價）：%s",
      len(unmapped),
      "、".join(sorted(unmapped)),
    )

  if not collected:
    # 已確認這是常態：該站郵輪團期最早在三個月後。不是壞掉，不要拋錯。
    log.info(
      "百威：%d 個主行程中沒有 %s~%s 由基隆港出發的郵輪團期（該站團期多在三個月後）",
      len(steps.get("step1") or []),
      start,
      end,
    )
  else:
    log.info("百威：%d 筆", len(collected))

  return list(collected.values())
