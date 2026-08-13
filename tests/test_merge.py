"""合併與輸出的測試。

最重要的規則：**某個來源擷取失敗時，絕不能用空資料蓋掉上一次的好資料。**
這是這類每日排程最容易翻車的地方，故測得最密。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from factories import make_deal

from cruise_deals.outputs import tabular
from cruise_deals.scrapers.base import ScrapeResult


def ok(source: str, deals) -> ScrapeResult:
  return ScrapeResult(source=source, deals=list(deals), ok=True, duration_s=1.0)


def failed(source: str, error: str = "Cloudflare 挑戰逾時") -> ScrapeResult:
  return ScrapeResult(source=source, deals=[], ok=False, error=error, duration_s=1.0)


class TestSuccessfulSourceReplacesItsOwnRows:
  def test_new_deals_replace_old_ones_from_same_source(self):
    previous = [make_deal(source="icruise", sail_date=date(2026, 8, 1))]
    fresh = make_deal(source="icruise", sail_date=date(2026, 8, 20))

    merged = tabular.merge_results(previous, [ok("icruise", [fresh])])

    assert len(merged) == 1
    assert merged[0].sail_date == date(2026, 8, 20)

  def test_sold_out_sailings_disappear(self):
    # 來源成功但少回了一筆 -> 那筆應該消失（已售完／已開船）
    previous = [
      make_deal(source="icruise", ship_name="Costa Serena"),
      make_deal(source="icruise", ship_name="Costa Fortuna"),
    ]
    kept = make_deal(source="icruise", ship_name="Costa Serena")

    merged = tabular.merge_results(previous, [ok("icruise", [kept])])

    assert [d.ship_name for d in merged] == ["Costa Serena"]

  def test_fresh_deals_have_no_stale_marker(self):
    merged = tabular.merge_results([], [ok("icruise", [make_deal()])])
    assert merged[0].stale_since is None


class TestFailedSourceKeepsPreviousData:
  """核心規則：失敗不等於沒有 deal。"""

  def test_previous_deals_are_retained(self):
    previous = [make_deal(source="cruisedirect")]

    merged = tabular.merge_results(previous, [failed("cruisedirect")])

    assert len(merged) == 1
    assert merged[0].source == "cruisedirect"

  def test_retained_deals_are_marked_stale(self):
    previous = [
      make_deal(
        source="cruisedirect",
        scraped_at=datetime(2026, 8, 10, 5, 0, tzinfo=timezone.utc),
      )
    ]

    merged = tabular.merge_results(previous, [failed("cruisedirect")])

    assert merged[0].stale_since == date(2026, 8, 10)

  def test_stale_marker_does_not_drift_on_repeated_failures(self):
    # 連續失敗多天時，標記應維持「最後一次成功擷取」的日期
    already_stale = make_deal(
      source="cruisedirect",
      scraped_at=datetime(2026, 8, 10, 5, 0, tzinfo=timezone.utc),
      stale_since=date(2026, 8, 10),
    )

    merged = tabular.merge_results([already_stale], [failed("cruisedirect")])

    assert merged[0].stale_since == date(2026, 8, 10)

  def test_failure_with_no_previous_data_yields_nothing(self):
    assert tabular.merge_results([], [failed("cruisedirect")]) == []

  def test_one_source_failing_does_not_affect_another(self):
    previous = [make_deal(source="cruisedirect", ship_name="Old Ship")]
    fresh = make_deal(source="icruise", ship_name="New Ship")

    merged = tabular.merge_results(
      previous, [ok("icruise", [fresh]), failed("cruisedirect")]
    )

    assert {d.ship_name for d in merged} == {"Old Ship", "New Ship"}

  def test_recovered_source_clears_stale_marker(self):
    stale = make_deal(source="cruisedirect", stale_since=date(2026, 8, 10))
    fresh = make_deal(source="cruisedirect", stale_since=None)

    merged = tabular.merge_results([stale], [ok("cruisedirect", [fresh])])

    assert merged[0].stale_since is None


class TestUnattemptedSourceKeepsPreviousData:
  """只跑 --sources icruise 時，不該把其他來源的資料清掉。"""

  def test_unattempted_source_rows_survive(self):
    previous = [make_deal(source="expedia", ship_name="Expedia Ship")]
    fresh = make_deal(source="icruise", ship_name="Icruise Ship")

    merged = tabular.merge_results(previous, [ok("icruise", [fresh])])

    assert {d.ship_name for d in merged} == {"Expedia Ship", "Icruise Ship"}

  def test_unattempted_source_rows_are_marked_stale(self):
    previous = [
      make_deal(
        source="expedia",
        ship_name="Expedia Only Ship",  # 與本次的 icruise 資料不是同一航次
        scraped_at=datetime(2026, 8, 11, 5, 0, tzinfo=timezone.utc),
      )
    ]

    merged = tabular.merge_results(previous, [ok("icruise", [make_deal()])])

    expedia = [d for d in merged if d.source == "expedia"][0]
    assert expedia.stale_since == date(2026, 8, 11)


class TestCrossSourceDedup:
  """同一航次出現在多個來源時只留一筆，並記下各站報價方便比價。"""

  def test_same_sailing_from_two_sources_collapses_to_one_row(self):
    a = make_deal(source="icruise", price=Decimal("999"))
    b = make_deal(source="expedia", price=Decimal("888"))

    merged = tabular.merge_results([], [ok("icruise", [a]), ok("expedia", [b])])

    assert len(merged) == 1

  def test_cheapest_source_wins(self):
    a = make_deal(source="icruise", price=Decimal("999"))
    b = make_deal(source="expedia", price=Decimal("888"))

    merged = tabular.merge_results([], [ok("icruise", [a]), ok("expedia", [b])])

    assert merged[0].source == "expedia"
    assert merged[0].price == Decimal("888")

  def test_other_source_prices_are_recorded(self):
    a = make_deal(source="icruise", price=Decimal("999"))
    b = make_deal(source="expedia", price=Decimal("888"))

    merged = tabular.merge_results([], [ok("icruise", [a]), ok("expedia", [b])])

    assert merged[0].other_sources == {"icruise": "999"}

  def test_real_price_beats_pricing_on_request(self):
    # 「洽詢報價」不該贏過真實報價
    no_price = make_deal(source="icruise", price=None)
    priced = make_deal(source="expedia", price=Decimal("1200"))

    merged = tabular.merge_results(
      [], [ok("icruise", [no_price]), ok("expedia", [priced])]
    )

    assert merged[0].price == Decimal("1200")
    assert merged[0].other_sources == {"icruise": None}

  def test_different_sailings_are_not_merged(self):
    a = make_deal(source="icruise", sail_date=date(2026, 8, 16))
    b = make_deal(source="expedia", sail_date=date(2026, 8, 17))

    merged = tabular.merge_results([], [ok("icruise", [a]), ok("expedia", [b])])

    assert len(merged) == 2


class TestOutputSorting:
  def test_sorted_by_date_then_price(self):
    late = make_deal(sail_date=date(2026, 8, 20), price=Decimal("100"))
    early_cheap = make_deal(
      sail_date=date(2026, 8, 10), price=Decimal("100"), ship_name="A"
    )
    early_pricey = make_deal(
      sail_date=date(2026, 8, 10), price=Decimal("900"), ship_name="B"
    )

    merged = tabular.merge_results(
      [], [ok("icruise", [late, early_pricey, early_cheap])]
    )

    assert [d.ship_name for d in merged] == ["A", "B", "Costa Serena"]


class TestPersistence:
  def test_json_round_trip_preserves_deals(self, tmp_path):
    deals = [make_deal(), make_deal(sail_date=date(2026, 9, 1), price=None)]
    path = tmp_path / "deals.json"

    tabular.write_json(path, deals, report=tabular.build_report([]))
    restored = tabular.read_previous(path)

    assert restored == deals

  def test_missing_file_reads_as_empty(self, tmp_path):
    assert tabular.read_previous(tmp_path / "nope.json") == []

  def test_corrupt_file_reads_as_empty(self, tmp_path):
    path = tmp_path / "deals.json"
    path.write_text("{ not json", encoding="utf-8")
    assert tabular.read_previous(path) == []

  def test_json_includes_run_report(self, tmp_path):
    path = tmp_path / "deals.json"
    report = tabular.build_report([ok("icruise", [make_deal()]), failed("expedia")])

    tabular.write_json(path, [make_deal()], report=report)
    payload = json.loads(path.read_text(encoding="utf-8"))

    sources = {s["source"]: s for s in payload["run_report"]["sources"]}
    assert sources["icruise"]["ok"] is True
    assert sources["expedia"]["ok"] is False
    assert "Cloudflare" in sources["expedia"]["error"]

  def test_csv_has_header_and_one_row_per_deal(self, tmp_path):
    path = tmp_path / "deals.csv"
    tabular.write_csv(path, [make_deal(), make_deal(sail_date=date(2026, 9, 1))])

    lines = path.read_text(encoding="utf-8-sig").strip().splitlines()
    assert len(lines) == 3  # 表頭 + 2 筆
    assert "出發日期" in lines[0]

  def test_csv_writes_empty_string_for_missing_price(self, tmp_path):
    path = tmp_path / "deals.csv"
    tabular.write_csv(path, [make_deal(price=None)])

    import csv

    with path.open(encoding="utf-8-sig", newline="") as handle:
      row = next(iter(csv.DictReader(handle)))
    assert row["最低價格"] == ""
