"""合併各來源結果並輸出 CSV／JSON。

本模組的核心規則（也是這類每日排程最容易翻車的地方）：

    某個來源擷取失敗時，絕不能用空資料蓋掉上一次的好資料。

失敗的來源會沿用前次結果並標記 stale_since，讓網頁能誠實顯示
「這是幾號抓的資料」，而不是假裝一切正常。
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .. import normalize
from ..fx import Rate, convert
from ..models import Deal, sort_key, utcnow
from ..scrapers.base import ScrapeResult

log = logging.getLogger(__name__)

CSV_HEADERS = [
  "出發日期",
  "出發港口",
  "目的港口",
  "郵輪名",
  "船公司",
  "航行天數",
  "最低價格",
  "幣別",
  "台幣價格",
  "匯率",
  "價格說明",
  "停靠港",
  "來源",
  "其他來源報價",
  "資料狀態",
  "連結",
]


@dataclass
class SourceStatus:
  """單一來源在這次執行的狀態，會寫進 JSON 也會顯示在網頁上。"""

  source: str
  ok: bool
  count: int
  duration_s: float
  error: str | None = None

  def to_dict(self) -> dict:
    return {
      "source": self.source,
      "ok": self.ok,
      "count": self.count,
      "duration_s": round(self.duration_s, 2),
      "error": self.error,
    }


@dataclass
class RunReport:
  """一次執行的總結。"""

  generated_at: datetime
  sources: list[SourceStatus] = field(default_factory=list)
  fx: Rate | None = None
  # 不足以讓執行失敗、但需要人看一眼的事（目前是對不到英文名的中文船名）
  warnings: list[str] = field(default_factory=list)

  @property
  def any_ok(self) -> bool:
    return any(s.ok for s in self.sources)

  @property
  def all_failed(self) -> bool:
    return bool(self.sources) and not self.any_ok

  def to_dict(self) -> dict:
    # fx 不寫在這裡，而是放在 deals.json 的頂層（見 write_json），
    # 免得同一份匯率在檔案裡出現兩次
    return {
      "generated_at": self.generated_at.isoformat(),
      "sources": [s.to_dict() for s in self.sources],
      "warnings": list(self.warnings),
    }


def build_report(results: list[ScrapeResult], fx: Rate | None = None) -> RunReport:
  """把各 scraper 的結果整理成執行報告。"""
  return RunReport(
    generated_at=utcnow(),
    fx=fx,
    sources=[
      SourceStatus(
        source=r.source,
        ok=r.ok,
        count=r.count,
        duration_s=r.duration_s,
        error=r.error,
      )
      for r in results
    ],
  )


def unmapped_ship_warnings(deals: list[Deal]) -> list[str]:
  """列出沒對應到英文正式船名的航次。

  這種列不會與外國站合併比價，但也不會消失——正是那種「安靜地少了一半功能」
  的失效方式，所以要主動攤在報告與網頁上，提醒補 config.SHIP_ALIASES。
  """
  unmapped = sorted(
    {d.ship_name for d in deals if normalize.is_unmapped_ship(d.ship_name)}
  )
  if not unmapped:
    return []
  return [f"這些船名沒有英文對照，無法跨來源比價：{'、'.join(unmapped)}"]


def _mark_stale(deal: Deal) -> Deal:
  """標記為沿用的舊資料。已標記過的不覆蓋，讓日期停在最後一次成功擷取。"""
  if deal.stale_since is not None:
    return deal
  return replace(deal, stale_since=deal.scraped_at.date())


def _mark_fresh(deal: Deal) -> Deal:
  """來源本次成功時，清掉可能殘留的過期標記。"""
  return deal if deal.stale_since is None else replace(deal, stale_since=None)


def _price_rank(deal: Deal) -> tuple[int, Decimal]:
  """比價用：有報價的一律勝過「洽詢報價」。

  比的是**台幣**價。台灣站報 TWD、外國站報 USD，直接比 price 的話
  379 USD 會被判定比 18,000 TWD 便宜，整個比價就是錯的。
  """
  amount = deal.price_twd if deal.price_twd is not None else deal.price
  return (1 if amount is None else 0, amount or Decimal(0))


def _requote(quote: dict[str, str | None], rate: Rate | None) -> dict[str, str | None]:
  """用今天的匯率重算一筆 other_sources 報價的台幣價。

  這些報價可能是從上一次的 deals.json 帶過來的（該來源本次沒跑到，
  它的 Deal 已在上次去重時被收進 other_sources），沒有重算的話
  網頁上會出現「NT$None」。
  """
  price = quote.get("price")
  if price is None:
    return quote
  try:
    amount = Decimal(price)
  except InvalidOperation:
    log.warning("other_sources 裡的價格無法解析（%r），不做台幣換算", price)
    return {**quote, "price_twd": None}
  twd = convert(amount, quote.get("currency") or "USD", rate)
  return {**quote, "price_twd": str(twd) if twd is not None else None}


def apply_fx(deal: Deal, rate: Rate | None) -> Deal:
  """把一筆 Deal 補上台幣價與所用匯率（含它記著的其他來源報價）。"""
  twd = convert(deal.price, deal.currency, rate)
  fx_rate = rate.usd_twd if (rate and deal.currency.upper() == "USD") else None
  others = {
    source: _requote(quote, rate) for source, quote in deal.other_sources.items()
  }
  if deal.price_twd == twd and deal.fx_rate == fx_rate and others == deal.other_sources:
    return deal
  return replace(deal, price_twd=twd, fx_rate=fx_rate, other_sources=others)


def merge_results(
  previous: list[Deal], results: list[ScrapeResult], rate: Rate | None = None
) -> list[Deal]:
  """把本次結果與前次資料合併成最終清單。

  規則：
    - 來源成功 -> 用新資料完整取代該來源的舊資料（賣完的航次因此會消失）
    - 來源失敗 -> 沿用該來源的舊資料並標記 stale_since
    - 來源本次未執行（例如 --sources 只指定一部分）-> 同樣沿用並標記
    - 跨來源相同航次 -> 只留報價最低的一筆，其餘價格記進 other_sources

  台幣換算在去重**之前**完成，因為去重就是靠台幣價挑出最便宜的那一筆。
  沿用的舊資料也會用今天的匯率重算，讓整張表的幣別基準一致。
  """
  previous_by_source: dict[str, list[Deal]] = {}
  for deal in previous:
    previous_by_source.setdefault(deal.source, []).append(deal)

  rows: list[Deal] = []
  attempted: set[str] = set()

  for result in results:
    attempted.add(result.source)
    if result.ok:
      rows.extend(_mark_fresh(d) for d in result.deals)
    else:
      retained = previous_by_source.get(result.source, [])
      if retained:
        log.warning(
          "%s 擷取失敗（%s），沿用前次 %d 筆資料",
          result.source,
          result.error,
          len(retained),
        )
      rows.extend(_mark_stale(d) for d in retained)

  # 本次沒跑到的來源，資料照樣保留（但標記為舊資料）
  for source, deals in previous_by_source.items():
    if source not in attempted:
      rows.extend(_mark_stale(d) for d in deals)

  return _dedupe([apply_fx(d, rate) for d in rows])


def _quote(deal: Deal) -> dict[str, str | None]:
  """把一筆報價壓成 other_sources 用的小記錄（原幣 + 幣別 + 台幣）。"""
  return {
    "price": str(deal.price) if deal.price is not None else None,
    "currency": deal.currency,
    "price_twd": str(deal.price_twd) if deal.price_twd is not None else None,
  }


def _dedupe(rows: list[Deal]) -> list[Deal]:
  """同一航次跨來源只留最便宜的一筆，其他來源的報價記在 other_sources。"""
  grouped: dict[tuple, list[Deal]] = {}
  for deal in rows:
    grouped.setdefault(deal.dedup_key, []).append(deal)

  merged: list[Deal] = []
  for group in grouped.values():
    winner = min(group, key=_price_rank)
    others = {d.source: _quote(d) for d in group if d is not winner}
    merged.append(replace(winner, other_sources=others) if others else winner)

  return sorted(merged, key=sort_key)


def read_previous(path: Path) -> list[Deal]:
  """讀取上一次的 deals.json。檔案不存在或壞掉時回空清單（不要讓整個流程掛掉）。"""
  path = Path(path)
  if not path.exists():
    return []
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Deal.from_dict(item) for item in payload.get("deals", [])]
  except (
    json.JSONDecodeError,
    KeyError,
    ValueError,
    TypeError,
    # Decimal("亂碼") 丟的是 InvalidOperation，它繼承 ArithmeticError 而非
    # ValueError，不列在這裡的話一份壞掉的 deals.json 會讓整趟排程崩掉
    InvalidOperation,
  ) as exc:
    log.warning("讀取 %s 失敗（%s），視為沒有前次資料", path, exc)
    return []


def read_previous_fx(path: Path) -> Rate | None:
  """從上一次的 deals.json 讀回匯率。

  今天抓不到匯率時沿用它（標成 stale），與「來源失敗就沿用舊資料」同一個原則——
  沒有匯率會讓整張表失去台幣價，比用昨天的匯率糟糕得多。
  """
  path = Path(path)
  if not path.exists():
    return None
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (json.JSONDecodeError, ValueError) as exc:
    log.warning("讀取 %s 的匯率失敗（%s）", path, exc)
    return None
  return Rate.from_dict(payload.get("fx"))


def write_json(path: Path, deals: list[Deal], report: RunReport) -> None:
  """寫出 deals.json（含執行報告與當日匯率）。"""
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = {
    "generated_at": report.generated_at.isoformat(),
    "run_report": report.to_dict(),
    "fx": report.fx.to_dict() if report.fx else None,
    "count": len(deals),
    "deals": [d.to_dict() for d in deals],
  }
  path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
  )


def _format_quote(quote: dict[str, str | None]) -> str:
  """把 other_sources 的一筆報價寫成人看得懂的字串。"""
  price = quote.get("price")
  if price is None:
    return "洽詢報價"
  twd = quote.get("price_twd")
  currency = quote.get("currency") or ""
  if twd and currency != "TWD":
    return f"NT${twd}（{currency} {price}）"
  return f"NT${twd}" if twd else f"{currency} {price}".strip()


def _csv_row(deal: Deal) -> dict[str, str]:
  others = "; ".join(
    f"{src}: {_format_quote(quote)}"
    for src, quote in sorted(deal.other_sources.items())
  )
  return {
    "出發日期": deal.sail_date.isoformat(),
    "出發港口": deal.depart_port,
    "目的港口": deal.arrive_port,
    "郵輪名": deal.ship_name,
    "船公司": deal.cruise_line,
    "航行天數": str(deal.nights),
    "最低價格": str(deal.price) if deal.price is not None else "",
    "幣別": deal.currency,
    "台幣價格": str(deal.price_twd) if deal.price_twd is not None else "",
    "匯率": str(deal.fx_rate) if deal.fx_rate is not None else "",
    "價格說明": deal.price_note,
    "停靠港": " → ".join(deal.ports_of_call),
    "來源": deal.source,
    "其他來源報價": others,
    "資料狀態": f"沿用 {deal.stale_since} 資料" if deal.stale_since else "最新",
    "連結": deal.detail_url,
  }


def write_csv(path: Path, deals: list[Deal]) -> None:
  """寫出 deals.csv。用 utf-8-sig 讓 Excel 直接開不會亂碼。"""
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS)
    writer.writeheader()
    for deal in deals:
      writer.writerow(_csv_row(deal))


def write_history_snapshot(
  directory: Path, deals: list[Deal], report: RunReport, today: date | None = None
) -> Path:
  """另存一份當日快照，方便日後比對價格變化。"""
  directory = Path(directory)
  directory.mkdir(parents=True, exist_ok=True)
  stamp = (today or date.today()).isoformat()
  path = directory / f"{stamp}.json"
  write_json(path, deals, report)
  return path
