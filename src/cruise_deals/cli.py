"""命令列進入點。

用法範例：
    python -m cruise_deals                        # 全部來源
    python -m cruise_deals --sources icruise      # 只跑輕量來源
    python -m cruise_deals --dry-run              # 不寫檔，只印出表格
    python -m cruise_deals --sources expedia --headed   # 有頭瀏覽器觀察
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import config, fx
from .models import Deal
from .outputs import page, tabular
from .scrapers.base import ScrapeResult, run_scraper

log = logging.getLogger("cruise_deals")

# scraper registry 的型別：來源名 -> 接受 options 並回傳 Deal 清單的函式
ScraperFn = Callable[[argparse.Namespace], list[Deal]]


def parse_sources(raw: str | None) -> list[str]:
  """把 --sources 的字串轉成來源清單，未指定時回傳全部。"""
  if not raw or raw.strip().lower() == "all":
    return list(config.ALL_SOURCES)
  names = [part.strip() for part in raw.split(",") if part.strip()]
  unknown = [n for n in names if n not in config.ALL_SOURCES]
  if unknown:
    raise ValueError(
      f"未知的來源: {', '.join(unknown)}（可用：{', '.join(config.ALL_SOURCES)}）"
    )
  return names


def default_scrapers() -> dict[str, ScraperFn]:
  """真實的 scraper registry。

  瀏覽器型 scraper 採延遲匯入，這樣沒裝 playwright／patchright 時
  仍然可以只跑 icruise。
  """

  def run_icruise(opts: argparse.Namespace) -> list[Deal]:
    from .scrapers import icruise

    return icruise.scrape(lookahead_days=opts.lookahead_days)

  def run_expedia(opts: argparse.Namespace) -> list[Deal]:
    from .scrapers import expedia

    return expedia.scrape(
      lookahead_days=opts.lookahead_days, headless=not opts.headed
    )

  def run_cruisedirect(opts: argparse.Namespace) -> list[Deal]:
    from .scrapers import cruisedirect

    return cruisedirect.scrape(
      lookahead_days=opts.lookahead_days, headless=not opts.headed
    )

  def run_asiayo(opts: argparse.Namespace) -> list[Deal]:
    from .scrapers import asiayo

    return asiayo.scrape(lookahead_days=opts.lookahead_days)

  def run_bwt(opts: argparse.Namespace) -> list[Deal]:
    from .scrapers import bwt

    return bwt.scrape(lookahead_days=opts.lookahead_days)

  return {
    "icruise": run_icruise,
    "expedia": run_expedia,
    "cruisedirect": run_cruisedirect,
    "asiayo": run_asiayo,
    "bwt": run_bwt,
  }


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    prog="cruise-deals",
    description="擷取基隆／東京／橫濱出發、一個月內的郵輪 last minute deals",
  )
  parser.add_argument(
    "--sources",
    help=f"以逗號分隔的來源（預設全部）：{', '.join(config.ALL_SOURCES)}",
  )
  parser.add_argument(
    "--lookahead-days",
    type=int,
    default=config.LOOKAHEAD_DAYS,
    help=f"往後看幾天的出發日期（預設 {config.LOOKAHEAD_DAYS}）",
  )
  parser.add_argument(
    "--output-dir",
    type=Path,
    default=config.ROOT,
    help="輸出根目錄（其下會產生 data/ 與 docs/）",
  )
  parser.add_argument(
    "--dry-run", action="store_true", help="不寫任何檔案，只把結果印到終端機"
  )
  parser.add_argument(
    "--headed", action="store_true", help="瀏覽器型 scraper 改用有頭模式（除錯用）"
  )
  parser.add_argument(
    "--fx-rate",
    help="手動指定 USD→TWD 匯率（例如 31.97），跳過線上查詢。離線除錯用。",
  )
  parser.add_argument("-v", "--verbose", action="store_true", help="顯示詳細日誌")
  return parser


def print_table(deals: list[Deal]) -> None:
  """把結果印成終端機表格（--dry-run 用）。"""
  if not deals:
    print("（沒有符合條件的航次）")
    return
  header = (
    f"{'出發日期':<12}{'出發港':<10}{'目的港':<28}{'郵輪':<24}{'船公司':<20}"
    f"{'夜':>3}  {'最低價(TWD)':>13}  來源"
  )
  print(header)
  print("-" * len(header))
  for d in deals:
    amount = d.price_twd if d.price_twd is not None else d.price
    price = f"{amount:,.0f}" if amount is not None else "洽詢報價"
    print(
      f"{d.sail_date.isoformat():<12}{d.depart_port:<10}{d.arrive_port[:26]:<28}"
      f"{d.ship_name[:22]:<24}{d.cruise_line[:18]:<20}{d.nights:>3}  {price:>13}  "
      f"{d.source}"
    )


def resolve_fx(raw: str | None, json_path: Path) -> fx.Rate | None:
  """決定這次要用的匯率。

  順序：--fx-rate 指定值 -> 線上查詢 -> 沿用上一次的匯率（標記 stale）。
  三個都沒有才回 None——那時美元報價換不成台幣，網頁會明確標示。
  """
  if raw:
    try:
      return fx.Rate(
        usd_twd=Decimal(raw), as_of=date.today(), source="--fx-rate（手動指定）"
      )
    except InvalidOperation:
      log.warning("--fx-rate 不是數字（%r），改為線上查詢", raw)

  try:
    return fx.fetch()
  except fx.FxError as exc:
    log.warning("取得匯率失敗（%s），嘗試沿用上一次的匯率", exc)

  previous = tabular.read_previous_fx(json_path)
  if previous is None:
    log.error("沒有可用的匯率，美元報價這次無法換算成台幣")
    return None
  log.warning("沿用 %s 的匯率 1 USD = %s TWD", previous.as_of, previous.usd_twd)
  return fx.Rate(
    usd_twd=previous.usd_twd,
    as_of=previous.as_of,
    source=previous.source,
    stale=True,
  )


def _use_utf8_output() -> None:
  """讓終端機輸出走 UTF-8。

  Windows 的預設是 cp950，中文會變亂碼，而 ✓／⚠ 這些符號更會直接
  拋 UnicodeEncodeError 讓整個程式在最後一步掛掉。
  """
  for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
      try:
        reconfigure(encoding="utf-8", errors="replace")
      except (ValueError, OSError):  # pragma: no cover - 已被重導向的串流
        pass


def main(argv: list[str] | None = None, scrapers: dict[str, ScraperFn] | None = None) -> int:
  """執行一次擷取。回傳離開碼：全部來源都失敗時為 1，否則為 0。"""
  _use_utf8_output()
  opts = build_parser().parse_args(argv)
  logging.basicConfig(
    level=logging.DEBUG if opts.verbose else logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
  )

  try:
    selected = parse_sources(opts.sources)
  except ValueError as exc:
    print(f"錯誤：{exc}", file=sys.stderr)
    return 2

  registry = scrapers if scrapers is not None else default_scrapers()

  json_path = Path(opts.output_dir) / "data" / "deals.json"
  rate = resolve_fx(opts.fx_rate, json_path)

  results: list[ScrapeResult] = []
  for name in selected:
    scrape_fn = registry[name]
    log.info("開始擷取 %s …", name)
    result = run_scraper(name, lambda fn=scrape_fn: fn(opts))
    log.info("%s", result.summary())
    results.append(result)

  data_dir = Path(opts.output_dir) / "data"
  docs_dir = Path(opts.output_dir) / "docs"

  previous = tabular.read_previous(json_path)
  report = tabular.build_report(results, fx=rate)
  deals = tabular.merge_results(previous, results, rate=rate)
  report.warnings = tabular.unmapped_ship_warnings(deals)

  if opts.dry_run:
    print_table(deals)
    print(f"\n（dry-run：未寫入任何檔案）共 {len(deals)} 筆")
  else:
    tabular.write_json(json_path, deals, report)
    tabular.write_csv(data_dir / "deals.csv", deals)
    tabular.write_history_snapshot(data_dir / "history", deals, report, date.today())
    page.write(docs_dir / "index.html", deals, report)
    log.info("已寫出 %d 筆到 %s 與 %s", len(deals), data_dir, docs_dir)

  if report.fx is not None:
    suffix = "（沿用舊匯率）" if report.fx.stale else ""
    print(f"匯率 1 USD = {report.fx.usd_twd} TWD（{report.fx.as_of}）{suffix}")
  for status in report.sources:
    marker = "✓" if status.ok else "✕"
    detail = f"{status.count} 筆" if status.ok else f"失敗 — {status.error}"
    print(f"{marker} {status.source}: {detail}")
  for warning in report.warnings:
    print(f"⚠ {warning}")

  if report.all_failed:
    print("所有來源都擷取失敗。", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":  # pragma: no cover
  raise SystemExit(main())
