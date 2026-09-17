"""所有 scraper 共用的介面與優雅降級機制。"""

from __future__ import annotations

import logging
import os
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

from datetime import date, timedelta
from typing import TypeVar

from ..models import Deal

log = logging.getLogger(__name__)

T = TypeVar("T")

# 代理設定的寫法：[scheme://]host:port
_PROXY_RE = re.compile(
  r"^(?:(?P<scheme>socks5h|socks5|socks4|https?)://)?"
  r"(?P<host>[\w.\-]+):(?P<port>\d{1,5})$"
)


def proxy_from_env(var_name: str) -> str | None:
  """從環境變數讀取瀏覽器要走的代理，回 Chrome 認得的 "scheme://host:port"。

  GitHub Actions 的資料中心 IP 會被機器人防護擋（Cloudflare 出人工勾選框、
  Incapsula 直接掛斷連線），家用住宅 IP 則自動放行；workflow 會開一條
  SSH SOCKS5 通道到家用路由器，各來源用自己的環境變數決定要不要走。

  未設定或格式不對時回 None（直連），不讓瀏覽器因設定錯誤而啟動失敗。
  """
  raw = os.environ.get(var_name, "").strip()
  if not raw:
    return None
  match = _PROXY_RE.match(raw)
  if not match:
    log.warning("%s 格式無法辨識（%r），改用直連", var_name, raw)
    return None

  scheme = match.group("scheme") or "socks5"
  # socks5h 是 curl 的寫法，Chrome 不認得，會**安靜地忽略整個代理設定**改走直連。
  # Chrome 的 socks5 本來就會把網域交給代理端解析，語意相同。
  if scheme == "socks5h":
    scheme = "socks5"
  return f"{scheme}://{match.group('host')}:{match.group('port')}"


class ParseError(RuntimeError):
  """版面結構與預期不符。

  刻意設計成「大聲失敗」：來源站改版時若安靜地回傳空清單，
  合併階段會誤以為「今天真的沒有 deal」而清空資料。
  """


class BlockedError(ParseError):
  """連頁面都拿不到（機器人防護擋下）。

  與 ParseError 分開是因為兩者的處理方式不同：
    ParseError   -> 進得去但看不懂，通常是對方改版，要更新解析器
    BlockedError -> 根本進不去，改解析器沒有用
  繼承 ParseError 是為了讓既有的降級流程不必特別處理就能接住。
  """


@dataclass
class ScrapeResult:
  """單一來源的擷取結果，成功失敗都用同一個型別表達。"""

  source: str
  deals: list[Deal] = field(default_factory=list)
  ok: bool = True
  error: str | None = None
  duration_s: float = 0.0

  @property
  def count(self) -> int:
    return len(self.deals)

  def summary(self) -> str:
    if self.ok:
      return f"{self.source}: {self.count} 筆（{self.duration_s:.1f}s）"
    return f"{self.source}: 失敗 — {self.error}"


def run_scraper(source: str, fn: Callable[[], list[Deal]]) -> ScrapeResult:
  """執行單一 scraper，任何例外都轉成失敗的 ScrapeResult 而非往上拋。

  這是「一個來源掛掉不影響其他來源」的關鍵。
  """
  started = time.monotonic()
  try:
    deals = fn()
  except Exception as exc:  # noqa: BLE001 - 這裡刻意攔截所有例外
    return ScrapeResult(
      source=source,
      deals=[],
      ok=False,
      error=f"{type(exc).__name__}: {exc}".strip(),
      duration_s=time.monotonic() - started,
    )
  return ScrapeResult(
    source=source,
    deals=deals,
    ok=True,
    duration_s=time.monotonic() - started,
  )


def with_retry(fn: Callable[[], T], attempts: int = 3, delay_s: float = 2.0) -> T:
  """重試包裝：來源站會間歇性回 404／5xx，無人值守排程需自行重試。

  用遞增延遲，最後一次仍失敗才把例外往上拋。
  """
  last_exc: Exception | None = None
  for attempt in range(1, attempts + 1):
    try:
      return fn()
    except Exception as exc:  # noqa: BLE001 - 由呼叫端決定如何處理
      last_exc = exc
      if attempt < attempts:
        log.warning("第 %d/%d 次嘗試失敗（%s），稍後重試", attempt, attempts, exc)
        if delay_s:
          time.sleep(delay_s * attempt)
  assert last_exc is not None
  raise last_exc


def date_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
  """把日期窗口切成不重疊、不遺漏的連續小段（asiayo 的價格是區間最低價，要切段查）。"""
  if chunk_days < 1:
    raise ValueError("chunk_days 必須至少為 1")
  chunks: list[tuple[date, date]] = []
  current = start
  while current <= end:
    chunk_end = min(current + timedelta(days=chunk_days - 1), end)
    chunks.append((current, chunk_end))
    current = chunk_end + timedelta(days=1)
  return chunks


def keep_cheapest(deals: list[Deal]) -> list[Deal]:
  """來源內去重：同一航次（dedup_key 相同）只留最便宜的一筆，洽詢報價永遠輸給有價格的。

  台灣站常把同一航次拆成多個商品（不同供應商、加購方案、「週三出發」「週日出發」），
  不先收斂會在合併階段變成「自己跟自己比價」。
  """
  collected: dict[tuple, Deal] = {}
  for deal in deals:
    existing = collected.get(deal.dedup_key)
    if existing is None or (
      deal.price is not None and (existing.price is None or deal.price < existing.price)
    ):
      collected[deal.dedup_key] = deal
  return list(collected.values())


def format_exception(exc: BaseException) -> str:
  """給除錯輸出用的完整 traceback。"""
  return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
