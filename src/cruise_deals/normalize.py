"""把各站格式不一的原始字串正規化成統一型別。

本模組刻意只放純函式（不碰網路、不碰檔案），方便離線測試。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import config

# 港口欄位常見的前綴標籤，取 HTML text 後會混進來
# 有些站的標籤後面沒有冒號（cruisedirect 是 "Port of Call Tokyo, Japan - ..."），
# 故冒號設為可選。
_LABEL_RE = re.compile(
  r"^\s*(?:Starts|Ends|Ports? of Call|Departs|Departing From|Returns|Duration|Ship)\s*:?\s+",
  re.I,
)

_WS_RE = re.compile(r"\s+")

# 抓第一個數字（允許千分位逗號與小數）
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# "3 Nights" / "3 Night Keelung to ..." 都要抓到 3
_NIGHTS_RE = re.compile(r"(\d+)\s*Night", re.I)

# parse_sail_date 依序嘗試的格式
#   "Aug 16, 2026"  icruise
#   "16-Aug-2026"   Expedia（packages[].startDateTime）
#   "08/16/2026"    icruise 查詢參數
_DATE_FORMATS = (
  "%b %d, %Y",
  "%B %d, %Y",
  "%d-%b-%Y",
  "%d-%B-%Y",
  "%m/%d/%Y",
  "%Y/%m/%d",
  "%d %b %Y",
)


def clean_text(raw: str | None) -> str:
  """去除前後空白並把連續空白（含換行）壓成單一空格。"""
  if not raw:
    return ""
  return _WS_RE.sub(" ", raw).strip()


def strip_prefix_label(raw: str | None) -> str:
  """去除 "Starts: " 之類的標籤前綴並正規化空白。"""
  return clean_text(_LABEL_RE.sub("", raw or ""))


def match_port(raw: str | None) -> str | None:
  """把原始港口字串對映到目標港口的正規化名稱，非目標港則回傳 None。

  各站寫法差異大（"Keelung (Taipei), Taiwan" vs "Keelung"），
  故採小寫子字串比對。
  """
  text = strip_prefix_label(raw).lower()
  if not text:
    return None
  for canonical, keywords in config.TARGET_PORTS.items():
    if any(kw in text for kw in keywords):
      return canonical
  return None


def parse_sail_date(raw: str | date | datetime | None) -> date:
  """解析出發日期。無法解析時拋 ValueError（不要安靜地回 None）。"""
  if isinstance(raw, datetime):
    return raw.date()
  if isinstance(raw, date):
    return raw

  text = clean_text(raw)
  if not text:
    raise ValueError("空的日期字串")

  # ISO 格式（含帶時間的 "2026-08-16T00:00:00"）
  try:
    return datetime.fromisoformat(text).date()
  except ValueError:
    pass

  for fmt in _DATE_FORMATS:
    try:
      return datetime.strptime(text, fmt).date()
    except ValueError:
      continue

  raise ValueError(f"無法解析日期: {text!r}")


def parse_price(raw: str | int | float | Decimal | None) -> Decimal | None:
  """解析最低價格。無報價（"Pricing On Request"、空值、0）一律回 None。

  回傳 None 而非 0，是為了讓排序時能把「洽詢報價」排到最後，
  而不是誤判成最便宜。
  """
  if raw is None:
    return None

  if isinstance(raw, (int, float, Decimal)):
    value = Decimal(str(raw))
    return value if value > 0 else None

  match = _NUMBER_RE.search(raw)
  if not match:
    return None
  try:
    value = Decimal(match.group().replace(",", ""))
  except InvalidOperation:
    return None
  return value if value > 0 else None


def parse_nights(raw: str | int | None) -> int:
  """解析航行天數（夜數）。無法解析時拋 ValueError。"""
  if isinstance(raw, int):
    return raw

  text = clean_text(raw)
  match = _NIGHTS_RE.search(text)
  if match:
    return int(match.group(1))

  # 沒有 "Night" 字樣時退而求其次抓第一個整數
  fallback = re.search(r"\d+", text)
  if fallback:
    return int(fallback.group())

  raise ValueError(f"無法解析航行天數: {text!r}")


def port_group(port: str | None) -> str:
  """去重用的港口分組鍵。東京與橫濱視為同一個港（見 config.PORT_GROUPS）。"""
  name = clean_text(port)
  return config.PORT_GROUPS.get(name, name.lower())


def match_alias(text: str | None, aliases: dict[str, str]) -> str | None:
  """在整段文字裡找別名表的鍵，回傳對應值；找不到回 None。

  刻意採「最長子字串比對」而非逐一寫 parser——各站把船名與船公司塞在
  商品名稱裡的寫法差太多（【麗星郵輪探索星號】／【公主遊輪】鑽石公主號～／
  【MSC郵輪．榮耀號】），寫規則會很脆。最長優先是為了讓
  「藍寶石公主號」不會被「公主號」之類較短的鍵先搶走。
  """
  haystack = clean_text(text)
  if not haystack:
    return None
  best: str | None = None
  best_len = 0
  for key, value in aliases.items():
    if len(key) > best_len and key in haystack:
      best, best_len = value, len(key)
  return best


# 中英並陳時用來撈出英文船名，例如
# "【名人遊輪千禧號】CELEBRITY MILLENNIUM～ 12 晚日本精選" -> "CELEBRITY MILLENNIUM"
_ASCII_NAME_RE = re.compile(r"[A-Za-z][A-Za-z'.\-]*(?:\s+[A-Za-z][A-Za-z'.\-]*)+")

# 平假名／片假名、CJK 擴充 A、CJK 基本區
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


def _has_cjk(text: str) -> bool:
  """字串裡是否含中日文字元。"""
  return bool(_CJK_RE.search(text))


def canonical_ship(name: str | None) -> str:
  """把船名正規化成跨來源一致的寫法（英文正式船名）。

  這是台灣站能與外國站合併比價的關鍵：asiayo 給「鑽石公主號」、
  icruise 給 "Diamond Princess"，不轉換就永遠是兩列。

  依序嘗試：別名表 -> 字串裡的英文船名 -> 原樣（清理空白後）。
  對不到時回傳原字串而不是拋錯——一艘新船不該讓整個來源掛掉。
  """
  text = clean_text(name)
  if not text:
    return ""

  alias = match_alias(text, config.SHIP_ALIASES)
  if alias:
    return alias

  # 沒有中日文字元代表本來就是英文船名，直接用
  if not _has_cjk(text):
    return text

  embedded = _ASCII_NAME_RE.search(text)
  if embedded:
    return clean_text(embedded.group())

  return text


def is_unmapped_ship(name: str | None) -> bool:
  """判斷這個船名是否沒對應到英文正式名（含中日文字元即視為未對照）。

  給呼叫端拿去記警告用——安靜地漏掉一艘船不會報錯，
  只會讓那一列永遠無法跨來源合併，很難察覺。
  """
  return _has_cjk(canonical_ship(name))


def canonical_cruise_line(name: str | None) -> str:
  """把船公司名正規化成與其他來源一致的英文寫法。對不到就回原字串。"""
  text = clean_text(name)
  if not text:
    return ""
  return match_alias(text, config.CRUISE_LINE_ALIASES) or text


# 台灣站的商品名稱都把船公司與船名包在【】裡，但船名不一定在括號內：
#   【麗星郵輪探索星號】…      船公司與船名黏在一起
#   【MSC郵輪．榮耀號】…       中間用分隔符號
#   【公主遊輪】鑽石公主號～…   括號裡只有船公司，船名在括號後
_BRACKET_RE = re.compile(r"【([^】]+)】(.*)$")

# 船名之後的行程敘述分隔符號
_NAME_TAIL_RE = re.compile(r"[～~｜|、，,].*$")

# 【】裡只放了船公司（沒有船名）的判斷依據
_LINE_ONLY_RE = re.compile(r"(?:郵輪|遊輪|郵轮|Cruises?)\s*$", re.I)


def _strip_line_aliases(text: str) -> str:
  """從字串裡拿掉船公司名，剩下的通常就是船名。"""
  for alias in config.CRUISE_LINE_ALIASES:
    text = text.replace(alias, "")
  return clean_text(text).strip("．・.-　 ")


def split_ship_and_line(name: str | None) -> tuple[str, str, str]:
  """從台灣站的商品名稱解析出 (正規化船名, 原始船名, 船公司)。

  刻意不針對每種寫法寫 parser——三種寫法都見過，規則會很脆。
  改成用別名表對整段字串做最長子字串比對，對不到時才退回【】附近的字樣
  （扣掉船公司），讓資料至少還看得懂。
  """
  text = clean_text(name)
  if not text:
    return "", "", ""

  line = match_alias(text, config.CRUISE_LINE_ALIASES) or ""

  match = _BRACKET_RE.search(text)
  bracket = match.group(1) if match else ""
  # 括號內容以「郵輪／遊輪」收尾代表裡面只有船公司，船名在括號後面
  inside = "" if _LINE_ONLY_RE.search(bracket) else _strip_line_aliases(bracket)
  after = (
    _strip_line_aliases(_NAME_TAIL_RE.sub("", match.group(2))) if match else ""
  )
  raw = inside or after or _strip_line_aliases(_NAME_TAIL_RE.sub("", text))

  ship = match_alias(text, config.SHIP_ALIASES) or canonical_ship(raw)
  return ship, raw, line


def split_ports(raw: str | None, separator: str = ",") -> tuple[str, ...]:
  """把停靠港字串切成 tuple。

  分隔符號依來源而異：icruise 用逗號，cruisedirect 用 " - "
  （因為它的港名本身就含逗號，例如 "Tokyo, Japan"）。
  """
  text = strip_prefix_label(raw)
  if not text:
    return ()
  return tuple(part.strip() for part in text.split(separator) if part.strip())
