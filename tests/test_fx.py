"""匯率取得與換算測試。

跑在真實存下來的 API 回應上（`tests/fixtures/fx_*.json`），不需要網路。

換算的正確性直接決定比價對不對：台灣站報台幣、外國站報美元，
沒換算就比大小的話 379 美元會被當成比 18,000 台幣便宜。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cruise_deals import fx

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
  return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestParsingErApi:
  def test_reads_the_twd_rate(self):
    rate = fx.parse_er_api(load("fx_er_api.json"))
    assert rate.usd_twd == Decimal("31.966724")

  def test_reads_the_rate_date(self):
    rate = fx.parse_er_api(load("fx_er_api.json"))
    assert rate.as_of == date(2026, 8, 17)
    assert rate.source == "open.er-api.com"

  def test_reported_failure_raises(self):
    with pytest.raises(fx.FxError):
      fx.parse_er_api({"result": "error", "error-type": "unsupported-code"})

  def test_missing_twd_raises(self):
    with pytest.raises(fx.FxError):
      fx.parse_er_api({"result": "success", "rates": {"JPY": 159}})

  def test_unparseable_date_falls_back_to_today_rather_than_failing(self):
    rate = fx.parse_er_api(
      {"result": "success", "rates": {"TWD": 32}, "time_last_update_utc": "亂碼"}
    )
    assert rate.usd_twd == Decimal("32")


class TestParsingRter:
  def test_reads_the_twd_rate(self):
    rate = fx.parse_rter(load("fx_rter.json"))
    assert rate.usd_twd == Decimal("31.875")
    assert rate.as_of == date(2026, 8, 17)
    assert rate.source == "tw.rter.info"

  def test_missing_pair_raises(self):
    with pytest.raises(fx.FxError):
      fx.parse_rter({"USDJPY": {"Exrate": 159}})

  def test_nonsense_rate_raises(self):
    with pytest.raises(fx.FxError):
      fx.parse_rter({"USDTWD": {"Exrate": 0}})


class TestFetchFallback:
  """主要來源掛掉時要自動換備援，不是直接放棄。"""

  def _client(self, handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))

  def test_uses_the_primary_source_when_it_works(self):
    def handler(request):
      assert "open.er-api.com" in str(request.url)
      return httpx.Response(200, json=load("fx_er_api.json"))

    with self._client(handler) as client:
      assert fx.fetch(client).source == "open.er-api.com"

  def test_falls_back_when_the_primary_fails(self):
    def handler(request):
      if "open.er-api.com" in str(request.url):
        return httpx.Response(503)
      return httpx.Response(200, json=load("fx_rter.json"))

    with self._client(handler) as client:
      rate = fx.fetch(client)

    assert rate.source == "tw.rter.info"
    assert rate.usd_twd == Decimal("31.875")

  def test_all_sources_failing_raises(self):
    with self._client(lambda request: httpx.Response(500)) as client:
      with pytest.raises(fx.FxError):
        fx.fetch(client)


RATE = fx.Rate(usd_twd=Decimal("32"), as_of=date(2026, 8, 17), source="test")


class TestConversion:
  def test_usd_is_multiplied_by_the_rate(self):
    assert fx.convert(Decimal("2812"), "USD", RATE) == Decimal("89984")

  def test_result_is_rounded_to_whole_dollars(self):
    assert fx.convert(Decimal("1742.5"), "USD", RATE) == Decimal("55760")

  def test_twd_passes_through_untouched(self):
    assert fx.convert(Decimal("18000"), "TWD", RATE) == Decimal("18000")

  def test_twd_works_even_without_a_rate(self):
    # 台灣站的報價本來就是台幣，抓不到匯率也不該失去價格
    assert fx.convert(Decimal("18000"), "TWD", None) == Decimal("18000")

  def test_usd_without_a_rate_yields_none(self):
    # 回 None 而不是 0——0 會被排序誤判成最便宜
    assert fx.convert(Decimal("2812"), "USD", None) is None

  def test_unknown_currency_yields_none(self):
    assert fx.convert(Decimal("50000"), "JPY", RATE) is None

  def test_no_quote_stays_no_quote(self):
    assert fx.convert(None, "USD", RATE) is None


class TestRoundTrip:
  def test_rate_survives_json(self):
    restored = fx.Rate.from_dict(RATE.to_dict())
    assert restored == RATE

  def test_stale_flag_survives_json(self):
    stale = fx.Rate(
      usd_twd=Decimal("32"), as_of=date(2026, 8, 16), source="test", stale=True
    )
    assert fx.Rate.from_dict(stale.to_dict()).stale is True

  def test_missing_data_reads_as_none(self):
    assert fx.Rate.from_dict(None) is None

  def test_corrupt_data_reads_as_none(self):
    assert fx.Rate.from_dict({"usd_twd": "不是數字", "as_of": "2026-08-17"}) is None
