"""所有 scraper 共用的介面與優雅降級機制。"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import Deal


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


def format_exception(exc: BaseException) -> str:
  """給除錯輸出用的完整 traceback。"""
  return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
