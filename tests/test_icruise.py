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
  """版面改版或被擋時要大聲失敗，不能安靜地回傳空清單。

  2026-09-11 起 GitHub Actions 上隔三差五回 0 筆（本機同一時間有 30 筆），
  就是因為「拿到的根本不是搜尋結果頁」被當成「今天真的沒有航次」，
  把 icruise 前一天的 20 多筆整批洗掉。
  """

  def test_raises_when_page_claims_results_but_none_parsed(self):
    html = (
      '<html><body><span class="matched-text">42 Matched Sailings</span>'
      '<table id="results_table"></table></body></html>'
    )
    with pytest.raises(ParseError):
      icruise.parse_search_page(html)

  def test_page_that_says_zero_matched_is_empty(self):
    html = (
      '<html><body><span class="matched-text">0 Matched Sailings</span>'
      "</body></html>"
    )
    assert icruise.parse_search_page(html) == []

  def test_real_no_results_page_is_empty(self):
    # 查 2030 年的區間，該站回「No results found」——這才是真正的 0 筆
    html = load("icruise_no_results.html")
    assert icruise.page_state(html) == "empty"
    assert icruise.parse_search_page(html) == []

  def test_page_without_results_or_no_results_marker_is_unrecognised(self):
    # 沒有結果表、沒有筆數、也沒有「No results found」——這不是搜尋結果頁，
    # 可能是被擋、錯誤頁或改版，不能當成 0 筆
    html = "<html><body><h1>Access Denied</h1></body></html>"
    assert icruise.page_state(html) == "unknown"
    with pytest.raises(ParseError, match="不是搜尋結果頁"):
      icruise.parse_search_page(html)

  def test_results_page_state(self, keelung_html):
    assert icruise.page_state(keelung_html) == "results"


class TestErrorPageRetry:
  """CI 上實際拿到的是該站自己的錯誤頁（「Oh no! There seems to be a problem…
  There was a problem when creating your account」，HTTP 200、有導覽列、沒有結果區塊）。
  這種頁面重送一次通常就好了，所以先重試，重試用完才失敗並存現場。"""

  ERROR_PAGE = load("icruise_error_page.html")

  def test_real_ci_error_page_is_unrecognised(self):
    assert icruise.page_state(self.ERROR_PAGE) == "unknown"

  def test_error_page_is_retried_until_a_results_page_comes_back(self, keelung_html):
    import httpx

    responses = [self.ERROR_PAGE, self.ERROR_PAGE, keelung_html]
    calls = []

    def handler(request):
      calls.append(request.url)
      return httpx.Response(200, text=responses[len(calls) - 1])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    html = icruise.fetch_page(client, date(2026, 9, 17), date(2026, 9, 21), delay_s=0)

    assert len(calls) == 3
    assert icruise.page_state(html) == "results"

  def test_error_page_on_every_attempt_fails_loudly_and_saves_the_page(
    self, tmp_path, monkeypatch
  ):
    import httpx

    from cruise_deals import config

    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "debug")
    client = httpx.Client(
      transport=httpx.MockTransport(lambda r: httpx.Response(200, text=self.ERROR_PAGE))
    )

    with pytest.raises(ParseError, match="不是搜尋結果頁") as info:
      icruise.fetch_page(client, date(2026, 9, 17), date(2026, 9, 21), delay_s=0)

    saved = tmp_path / "debug" / "icruise_2026-09-17_2026-09-21.html"
    assert saved.exists()
    assert str(saved) in str(info.value)


class TestDebugSnapshot:
  def test_unrecognised_page_is_saved_for_diagnosis(self, tmp_path, monkeypatch):
    # CI 會把 debug/ 當成 artifact 上傳；沒有現場就永遠不知道對方回了什麼
    from cruise_deals import config

    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "debug")
    path = icruise.save_debug(date(2026, 9, 17), date(2026, 9, 21), "<html>Access Denied</html>")
    assert path == tmp_path / "debug" / "icruise_2026-09-17_2026-09-21.html"
    assert path.read_text(encoding="utf-8") == "<html>Access Denied</html>"


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
