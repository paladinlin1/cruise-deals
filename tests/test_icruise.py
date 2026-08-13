"""icruise 解析器測試：全部跑在存下來的真實 HTML 上，不需要網路。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cruise_deals.scrapers import icruise
from cruise_deals.scrapers.base import ParseError

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
  return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def keelung_html() -> str:
  # 基隆單港查詢：5 筆，全部是「洽詢報價」
  return load("icruise_keelung.html")


@pytest.fixture(scope="module")
def asia_html() -> str:
  # 亞洲全區查詢：25 筆（分頁上限），含真實價格
  return load("icruise_asia.html")


class TestMatchedCount:
  def test_reads_total_from_matched_text(self, asia_html):
    assert icruise.matched_count(asia_html) == 119

  def test_keelung_page_total(self, keelung_html):
    assert icruise.matched_count(keelung_html) == 5

  def test_missing_matched_text_returns_zero(self):
    assert icruise.matched_count("<html><body>nothing</body></html>") == 0


class TestParseSearchPage:
  def test_parses_every_row(self, keelung_html):
    assert len(icruise.parse_search_page(keelung_html)) == 5

  def test_first_row_fields(self, keelung_html):
    deal = icruise.parse_search_page(keelung_html)[0]
    assert deal.source == "icruise"
    assert deal.sail_date == date(2026, 8, 16)
    assert deal.depart_port == "Keelung"
    assert deal.depart_port_raw == "Keelung (Taipei), Taiwan"
    assert deal.arrive_port == "Keelung (Taipei), Taiwan"
    assert deal.ship_name == "Costa Serena"
    assert deal.cruise_line == "Costa Cruises"
    assert deal.nights == 3
    assert deal.price is None  # <h2 class="noprice">Pricing On<br>Request</h2>
    assert deal.currency == "USD"

  def test_ports_of_call(self, keelung_html):
    deal = icruise.parse_search_page(keelung_html)[0]
    assert deal.ports_of_call == (
      "Keelung (Taipei)", "Naha", "Ishigaki", "Keelung (Taipei)",
    )

  def test_one_way_sailing_has_different_arrive_port(self, keelung_html):
    # 第 4 筆是基隆到釜山的單程航次
    deal = icruise.parse_search_page(keelung_html)[3]
    assert deal.depart_port == "Keelung"
    assert deal.arrive_port == "Busan (Pusan), South Korea"

  def test_detail_url_is_absolute(self, keelung_html):
    deal = icruise.parse_search_page(keelung_html)[0]
    assert deal.detail_url.startswith("https://www.icruise.com/itineraries/")
    assert "3-night-keelung-to-keelung-cruise_costa-serena_8-16-2026" in deal.detail_url

  def test_all_keelung_rows_have_no_price(self, keelung_html):
    deals = icruise.parse_search_page(keelung_html)
    assert all(d.price is None for d in deals)

  def test_asia_page_parses_full_page_of_rows(self, asia_html):
    # 每頁上限 25 筆
    assert len(icruise.parse_search_page(asia_html)) == 25

  def test_parses_price_with_thousands_separator(self, asia_html):
    # 真實資料中的 "$1,742"
    yokohama = [
      d for d in icruise.parse_search_page(asia_html) if d.depart_port == "Yokohama"
    ]
    assert len(yokohama) == 1
    assert yokohama[0].price == Decimal("1742")
    assert yokohama[0].ship_name == "Diamond Princess"
    assert yokohama[0].sail_date == date(2026, 9, 5)

  def test_scraped_at_is_timezone_aware(self, keelung_html):
    deal = icruise.parse_search_page(keelung_html)[0]
    assert deal.scraped_at.tzinfo is not None


class TestFilterTargetPorts:
  def test_keeps_only_target_departure_ports(self, asia_html):
    deals = icruise.parse_search_page(asia_html)
    filtered = icruise.filter_target_ports(deals)
    assert len(filtered) == 1
    assert filtered[0].depart_port == "Yokohama"

  def test_keeps_all_when_every_row_matches(self, keelung_html):
    deals = icruise.parse_search_page(keelung_html)
    assert len(icruise.filter_target_ports(deals)) == 5


class TestSanityCheck:
  """版面改版時要大聲失敗，不能安靜地回傳空清單。"""

  def test_raises_when_page_claims_results_but_none_parsed(self):
    html = (
      '<html><body><span class="matched-text">42 Matched Sailings</span>'
      '<table id="results_table"></table></body></html>'
    )
    with pytest.raises(ParseError):
      icruise.parse_search_page(html)

  def test_empty_result_page_returns_empty_list(self):
    html = (
      '<html><body><span class="matched-text">0 Matched Sailings</span>'
      "</body></html>"
    )
    assert icruise.parse_search_page(html) == []


class TestBuildSearchUrl:
  def test_formats_dates_as_month_day_year(self):
    params = icruise.build_search_params(date(2026, 8, 13), date(2026, 9, 12))
    assert params["Sail_DateFrom"] == "08/13/2026"
    assert params["Sail_DateTo"] == "09/12/2026"

  def test_includes_asia_destination_and_vacation_type(self):
    params = icruise.build_search_params(date(2026, 8, 13), date(2026, 9, 12))
    assert params["WMPHDestinationCodeSub"] == 7
    assert params["VacationType"] == 1


class TestBuildSearchUrl2:
  """實測發現：日期中的斜線被編碼成 %2F 時該站會間歇性回 404，
  故 URL 必須保留字面斜線（與瀏覽器送出的形式一致）。"""

  def test_query_keeps_literal_slashes(self):
    url = icruise.build_search_url(date(2026, 8, 13), date(2026, 9, 12))
    assert "Sail_DateFrom=08/13/2026" in url
    assert "Sail_DateTo=09/12/2026" in url
    assert "%2F" not in url

  def test_url_points_at_search_endpoint(self):
    url = icruise.build_search_url(date(2026, 8, 13), date(2026, 9, 12))
    assert url.startswith("https://www.icruise.com/c/src.php?")


class TestFetchWithRetry:
  """該站會間歇性回 404／5xx，無人值守的每日排程必須能自行重試。"""

  def test_returns_result_after_transient_failures(self):
    attempts = []

    def flaky():
      attempts.append(1)
      if len(attempts) < 3:
        raise RuntimeError("transient")
      return "ok"

    assert icruise.with_retry(flaky, attempts=3, delay_s=0) == "ok"
    assert len(attempts) == 3

  def test_reraises_after_exhausting_attempts(self):
    calls = []

    def always_fails():
      calls.append(1)
      raise RuntimeError("permanent")

    with pytest.raises(RuntimeError, match="permanent"):
      icruise.with_retry(always_fails, attempts=3, delay_s=0)
    assert len(calls) == 3

  def test_succeeds_first_try_without_extra_calls(self):
    calls = []

    def fine():
      calls.append(1)
      return "ok"

    assert icruise.with_retry(fine, attempts=3, delay_s=0) == "ok"
    assert len(calls) == 1


class TestDateChunks:
  """每頁 25 筆上限無法用參數放寬，故切分日期窗口。"""

  def test_covers_whole_window_without_gaps(self):
    chunks = icruise.date_chunks(date(2026, 8, 13), date(2026, 9, 12), chunk_days=5)
    assert chunks[0][0] == date(2026, 8, 13)
    assert chunks[-1][1] == date(2026, 9, 12)
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
      # 下一段必須緊接前一段，中間不能漏日期
      assert (next_start - prev_end).days == 1

  def test_chunk_size_respected(self):
    chunks = icruise.date_chunks(date(2026, 8, 1), date(2026, 8, 30), chunk_days=5)
    assert all((end - start).days + 1 <= 5 for start, end in chunks)

  def test_single_day_window(self):
    assert icruise.date_chunks(date(2026, 8, 1), date(2026, 8, 1), chunk_days=5) == [
      (date(2026, 8, 1), date(2026, 8, 1))
    ]
