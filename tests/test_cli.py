"""CLI 的測試：來源選擇、離開碼、dry-run。

用假的 scraper registry 取代真實網路呼叫。
"""

from __future__ import annotations

import json

import pytest
from factories import make_deal

from cruise_deals import cli


def registry(**sources):
  """把 {來源名: 回傳值或例外} 轉成 scraper registry。"""

  def make(value):
    def scrape(_opts):
      if isinstance(value, Exception):
        raise value
      return value

    return scrape

  return {name: make(value) for name, value in sources.items()}


class TestParseSources:
  def test_defaults_to_all_sources(self):
    assert cli.parse_sources(None) == list(cli.config.ALL_SOURCES)

  def test_single_source(self):
    assert cli.parse_sources("icruise") == ["icruise"]

  def test_comma_separated(self):
    assert cli.parse_sources("icruise,expedia") == ["icruise", "expedia"]

  def test_tolerates_whitespace(self):
    assert cli.parse_sources(" icruise , expedia ") == ["icruise", "expedia"]

  def test_unknown_source_raises(self):
    with pytest.raises(ValueError, match="未知的來源"):
      cli.parse_sources("nosuchsite")


class TestExitCode:
  def test_zero_when_a_source_succeeds(self, tmp_path):
    code = cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=[make_deal()]),
    )
    assert code == 0

  def test_zero_when_only_some_sources_fail(self, tmp_path):
    code = cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise,expedia"],
      scrapers=registry(icruise=[make_deal()], expedia=RuntimeError("擋掉了")),
    )
    assert code == 0

  def test_one_when_every_source_fails(self, tmp_path):
    # 全部失敗代表這次執行沒有任何價值，應該讓 workflow 亮紅燈
    code = cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise,expedia"],
      scrapers=registry(
        icruise=RuntimeError("逾時"), expedia=RuntimeError("擋掉了")
      ),
    )
    assert code == 1

  def test_zero_when_source_succeeds_with_no_deals(self, tmp_path):
    # 成功但當下真的沒有 last minute deal，不算失敗
    code = cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=[]),
    )
    assert code == 0


class TestOutputs:
  def test_writes_csv_json_and_page(self, tmp_path):
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=[make_deal()]),
    )
    assert (tmp_path / "data" / "deals.csv").exists()
    assert (tmp_path / "data" / "deals.json").exists()
    assert (tmp_path / "docs" / "index.html").exists()

  def test_writes_history_snapshot(self, tmp_path):
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=[make_deal()]),
    )
    snapshots = list((tmp_path / "data" / "history").glob("*.json"))
    assert len(snapshots) == 1

  def test_dry_run_writes_nothing(self, tmp_path):
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise", "--dry-run"],
      scrapers=registry(icruise=[make_deal()]),
    )
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "docs").exists()


class TestPreviousDataIsRespected:
  def test_failed_source_keeps_previous_rows(self, tmp_path):
    # 第一次成功，寫入資料
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=[make_deal(ship_name="Costa Serena")]),
    )
    # 第二次失敗，舊資料必須還在
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=RuntimeError("逾時")),
    )

    payload = json.loads((tmp_path / "data" / "deals.json").read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["deals"][0]["ship_name"] == "Costa Serena"
    assert payload["deals"][0]["stale_since"] is not None
