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
from pathlib import Path

from . import config
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

  return {
    "icruise": run_icruise,
    "expedia": run_expedia,
    "cruisedirect": run_cruisedirect,
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
  parser.add_argument("-v", "--verbose", action="store_true", help="顯示詳細日誌")
  return parser


def print_table(deals: list[Deal]) -> None:
  """把結果印成終端機表格（--dry-run 用）。"""
  if not deals:
    print("（沒有符合條件的航次）")
    return
  header = f"{'出發日期':<12}{'出發港':<10}{'目的港':<28}{'郵輪':<24}{'船公司':<20}{'夜':>3}  {'最低價':>10}"
  print(header)
  print("-" * len(header))
  for d in deals:
    price = f"${d.price:,.0f}" if d.price is not None else "洽詢報價"
    print(
      f"{d.sail_date.isoformat():<12}{d.depart_port:<10}{d.arrive_port[:26]:<28}"
      f"{d.ship_name[:22]:<24}{d.cruise_line[:18]:<20}{d.nights:>3}  {price:>10}"
    )


def main(argv: list[str] | None = None, scrapers: dict[str, ScraperFn] | None = None) -> int:
  """執行一次擷取。回傳離開碼：全部來源都失敗時為 1，否則為 0。"""
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

  results: list[ScrapeResult] = []
  for name in selected:
    scrape_fn = registry[name]
    log.info("開始擷取 %s …", name)
    result = run_scraper(name, lambda fn=scrape_fn: fn(opts))
    log.info("%s", result.summary())
    results.append(result)

  data_dir = Path(opts.output_dir) / "data"
  docs_dir = Path(opts.output_dir) / "docs"
  json_path = data_dir / "deals.json"

  previous = tabular.read_previous(json_path)
  report = tabular.build_report(results)
  deals = tabular.merge_results(previous, results)

  if opts.dry_run:
    print_table(deals)
    print(f"\n（dry-run：未寫入任何檔案）共 {len(deals)} 筆")
  else:
    tabular.write_json(json_path, deals, report)
    tabular.write_csv(data_dir / "deals.csv", deals)
    tabular.write_history_snapshot(data_dir / "history", deals, report, date.today())
    page.write(docs_dir / "index.html", deals, report)
    log.info("已寫出 %d 筆到 %s 與 %s", len(deals), data_dir, docs_dir)

  for status in report.sources:
    marker = "✓" if status.ok else "✕"
    detail = f"{status.count} 筆" if status.ok else f"失敗 — {status.error}"
    print(f"{marker} {status.source}: {detail}")

  if report.all_failed:
    print("所有來源都擷取失敗。", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":  # pragma: no cover
  raise SystemExit(main())
