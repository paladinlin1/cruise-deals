"""CLI 的測試：來源選擇、離開碼、dry-run、匯率取得。

用假的 scraper registry 取代真實網路呼叫。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from factories import make_deal

from cruise_deals import cli, fx

RATE = fx.Rate(usd_twd=Decimal("32"), as_of=date(2026, 8, 17), source="test")


@pytest.fixture(autouse=True)
def offline_fx(monkeypatch):
  """整份測試不准連網。

  cli.main() 每次都會去查匯率，沒擋掉的話測試就會偷偷依賴網路，
  斷線時整批紅燈卻看不出原因。
  """
  monkeypatch.setattr(fx, "fetch", lambda *a, **k: RATE)


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


class TestNewSourcesAreRegistered:
  def test_taiwanese_sources_are_selectable(self):
    assert cli.parse_sources("asiayo,bwt") == ["asiayo", "bwt"]

  def test_default_run_includes_them(self):
    assert "asiayo" in cli.parse_sources(None)
    assert "bwt" in cli.parse_sources(None)

  def test_real_registry_has_a_function_for_every_source(self):
    assert set(cli.default_scrapers()) == set(cli.config.ALL_SOURCES)


class TestExchangeRate:
  def test_manual_rate_skips_the_online_lookup(self, monkeypatch, tmp_path):
    def boom(*_a, **_k):
      raise AssertionError("指定了 --fx-rate 就不該再連網查匯率")

    monkeypatch.setattr(fx, "fetch", boom)
    rate = cli.resolve_fx("31.5", tmp_path / "deals.json")

    assert rate is not None
    assert rate.usd_twd == Decimal("31.5")

  def test_invalid_manual_rate_falls_back_to_lookup(self, tmp_path):
    assert cli.resolve_fx("不是數字", tmp_path / "deals.json") == RATE

  def test_previous_rate_is_reused_when_the_lookup_fails(self, monkeypatch, tmp_path):
    path = tmp_path / "deals.json"
    path.write_text(
      json.dumps({"fx": {"usd_twd": "30.5", "as_of": "2026-08-10", "source": "舊的"}}),
      encoding="utf-8",
    )
    monkeypatch.setattr(fx, "fetch", _raise_fx_error)

    rate = cli.resolve_fx(None, path)

    assert rate is not None
    assert rate.usd_twd == Decimal("30.5")
    assert rate.stale is True  # 網頁要看得出這是舊匯率

  def test_no_rate_at_all_is_survivable(self, monkeypatch, tmp_path):
    monkeypatch.setattr(fx, "fetch", _raise_fx_error)
    assert cli.resolve_fx(None, tmp_path / "nope.json") is None

  def test_rate_is_written_into_the_json(self, tmp_path):
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=[make_deal()]),
    )
    payload = json.loads((tmp_path / "data" / "deals.json").read_text(encoding="utf-8"))

    assert payload["fx"]["usd_twd"] == "32"

  def test_usd_deals_get_a_twd_price(self, tmp_path):
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise"],
      scrapers=registry(icruise=[make_deal(price=Decimal("479"))]),
    )
    payload = json.loads((tmp_path / "data" / "deals.json").read_text(encoding="utf-8"))

    assert payload["deals"][0]["price_twd"] == "15328"

  def test_manual_rate_flag_reaches_the_output(self, tmp_path):
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "icruise", "--fx-rate", "30"],
      scrapers=registry(icruise=[make_deal(price=Decimal("100"))]),
    )
    payload = json.loads((tmp_path / "data" / "deals.json").read_text(encoding="utf-8"))

    assert payload["deals"][0]["price_twd"] == "3000"


class TestUnmappedShipReporting:
  def test_warning_reaches_the_run_report(self, tmp_path):
    cli.main(
      ["--output-dir", str(tmp_path), "--sources", "asiayo"],
      scrapers=registry(asiayo=[make_deal(source="asiayo", ship_name="愛達魔都號")]),
    )
    payload = json.loads((tmp_path / "data" / "deals.json").read_text(encoding="utf-8"))

    assert any("愛達魔都號" in w for w in payload["run_report"]["warnings"])


def _raise_fx_error(*_a, **_k):
  raise fx.FxError("兩個來源都掛了")
