"""把各站格式不一的原始字串正規化成統一型別。

本模組刻意只放純函式（不碰網路、不碰檔案），方便離線測試。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import config

# 港口欄位常見的前綴標籤，取 HTML text 後會混進來
_LABEL_RE = re.compile(r"^\s*(?:Starts|Ends|Ports of Call|Departs|Returns)\s*:\s*", re.I)

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


def split_ports(raw: str | None) -> tuple[str, ...]:
  """把逗號分隔的停靠港字串切成 tuple。"""
  text = strip_prefix_label(raw)
  if not text:
    return ()
  return tuple(part.strip() for part in text.split(",") if part.strip())
