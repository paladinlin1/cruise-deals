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
from decimal import Decimal
from pathlib import Path

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

  @property
  def any_ok(self) -> bool:
    return any(s.ok for s in self.sources)

  @property
  def all_failed(self) -> bool:
    return bool(self.sources) and not self.any_ok

  def to_dict(self) -> dict:
    return {
      "generated_at": self.generated_at.isoformat(),
      "sources": [s.to_dict() for s in self.sources],
    }


def build_report(results: list[ScrapeResult]) -> RunReport:
  """把各 scraper 的結果整理成執行報告。"""
  return RunReport(
    generated_at=utcnow(),
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


def _mark_stale(deal: Deal) -> Deal:
  """標記為沿用的舊資料。已標記過的不覆蓋，讓日期停在最後一次成功擷取。"""
  if deal.stale_since is not None:
    return deal
  return replace(deal, stale_since=deal.scraped_at.date())


def _mark_fresh(deal: Deal) -> Deal:
  """來源本次成功時，清掉可能殘留的過期標記。"""
  return deal if deal.stale_since is None else replace(deal, stale_since=None)


def _price_rank(deal: Deal) -> tuple[int, Decimal]:
  """比價用：有報價的一律勝過「洽詢報價」。"""
  return (1 if deal.price is None else 0, deal.price or Decimal(0))


def merge_results(previous: list[Deal], results: list[ScrapeResult]) -> list[Deal]:
  """把本次結果與前次資料合併成最終清單。

  規則：
    - 來源成功 -> 用新資料完整取代該來源的舊資料（賣完的航次因此會消失）
    - 來源失敗 -> 沿用該來源的舊資料並標記 stale_since
    - 來源本次未執行（例如 --sources 只指定一部分）-> 同樣沿用並標記
    - 跨來源相同航次 -> 只留報價最低的一筆，其餘價格記進 other_sources
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

  return _dedupe(rows)


def _dedupe(rows: list[Deal]) -> list[Deal]:
  """同一航次跨來源只留最便宜的一筆，其他來源的報價記在 other_sources。"""
  grouped: dict[tuple, list[Deal]] = {}
  for deal in rows:
    grouped.setdefault(deal.dedup_key, []).append(deal)

  merged: list[Deal] = []
  for group in grouped.values():
    winner = min(group, key=_price_rank)
    others = {
      d.source: (str(d.price) if d.price is not None else None)
      for d in group
      if d is not winner
    }
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
  except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
    log.warning("讀取 %s 失敗（%s），視為沒有前次資料", path, exc)
    return []


def write_json(path: Path, deals: list[Deal], report: RunReport) -> None:
  """寫出 deals.json（含執行報告）。"""
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = {
    "generated_at": report.generated_at.isoformat(),
    "run_report": report.to_dict(),
    "count": len(deals),
    "deals": [d.to_dict() for d in deals],
  }
  path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
  )


def _csv_row(deal: Deal) -> dict[str, str]:
  others = "; ".join(
    f"{src}: {price or '洽詢報價'}" for src, price in sorted(deal.other_sources.items())
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
