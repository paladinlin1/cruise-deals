"""icruise 擷取器測試：跑在存下來的真實 API 回應上，不需要網路。

fixture 是 2026-09-17 對 `get-search-results` 查亞洲（destinations=7）的原始回應：
  icruise_api_2026-09_p1.json   9 月，55 筆（不足一頁，最後一頁）
  icruise_api_2026-10_p1.json   10 月第 1 頁，100 筆（滿頁，還有下一頁）
  icruise_api_2026-10_p2.json   10 月第 2 頁，52 筆
只把 specialPromos 裡的長篇文案精簡掉，其餘欄位原樣。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cruise_deals import config
from cruise_deals.scrapers import icruise
from cruise_deals.scrapers.base import ParseError, with_retry

FIXTURES = Path(__file__).parent / "fixtures"

# fixture 抓取當下查的窗口
WINDOW = (date(2026, 9, 17), date(2026, 10, 17))


def load(name: str) -> dict:
  return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def september() -> list[dict]:
  return icruise.extract_results(load("icruise_api_2026-09_p1.json"))


@pytest.fixture(scope="module")
def october() -> list[dict]:
  return icruise.extract_results(load("icruise_api_2026-10_p1.json")) + icruise.extract_results(
    load("icruise_api_2026-10_p2.json")
  )


def item(**overrides) -> dict:
  """建一筆最小的 API 結果（以真實的鑽石公主號 9/22 為底），只覆寫關心的欄位。"""
  base = {
    "id": 14279539,
    "cruiseLineLogo": "https://d23n7ahjfnjotp.cloudfront.net/imgs/client/logos/120w/new/15_120.gif",
    "itineraryName": "9 Night Okinawa and Taiwan Cruise",
    "numberOfDaysOrNights": 9,
    "dayOrNight": "N",
    "shipName": "Diamond Princess",
    "ports": ["Yokohama", " Keelung (Taipei)", " Ishigaki", " Okinawa", " Yokohama"],
    "departurePort": "Yokohama",
    "returnPort": " Yokohama",
    "sailingDate": "Sep 22, 2026",
    "metaName": "Balcony",
    "price": 1299,
    "totalPerPerson": 1299,
    "cruiseOnly": True,
    "canBook": True,
  }
  base.update(overrides)
  return base


class TestSearchBody:
  def test_queries_asia_for_one_month_at_a_time(self):
    body = icruise.build_search_body("2026-10", page=2)
    assert body["destinations"] == config.ICRUISE_DESTINATION_ASIA == "7"
    assert body["date"] == "2026-10"
    assert body["page"] == 2
    assert body["numberOfRecords"] == config.ICRUISE_PAGE_SIZE
    assert body["brand"] == "IC"

  def test_months_covering_the_window(self):
    assert icruise.months_covering(date(2026, 9, 17), date(2026, 10, 17)) == ["2026-09", "2026-10"]
    assert icruise.months_covering(date(2026, 10, 1), date(2026, 10, 31)) == ["2026-10"]
    assert icruise.months_covering(date(2026, 12, 20), date(2027, 1, 19)) == ["2026-12", "2027-01"]


class TestResponseShape:
  def test_missing_search_results_is_parse_error(self):
    with pytest.raises(ParseError, match="searchResults"):
      icruise.extract_results({"message": "oops"})

  def test_non_object_response_is_parse_error(self):
    with pytest.raises(ParseError):
      icruise.extract_results("<html>maintenance</html>")

  def test_real_pages_load(self, september, october):
    assert len(september) == 55
    assert len(october) == 152


class TestParseItem:
  def test_maps_every_field(self):
    deal = icruise.parse_item(item(), None)
    assert deal is not None
    assert deal.source == "icruise"
    assert deal.sail_date == date(2026, 9, 22)
    assert deal.nights == 9
    assert deal.ship_name == "Diamond Princess"
    assert deal.cruise_line == "Princess Cruises"  # logo 15
    assert deal.depart_port == "Yokohama"
    assert deal.depart_port_raw == "Yokohama"
    assert deal.arrive_port == "Yokohama"
    assert deal.ports_of_call == (
      "Yokohama", "Keelung (Taipei)", "Ishigaki", "Okinawa", "Yokohama"
    )
    assert deal.price == Decimal("1299")
    assert deal.currency == "USD"
    assert deal.price_note == "每人最低價（Balcony）"
    assert deal.detail_url == "https://www.icruise.com/c/itinDetail.php?CruiseItineraryID=14279539"

  def test_keelung_departure_is_recognised(self):
    deal = icruise.parse_item(item(departurePort="Keelung (Taipei)"), None)
    assert deal.depart_port == "Keelung"
    assert deal.depart_port_raw == "Keelung (Taipei)"

  def test_one_way_keeps_the_raw_return_port(self):
    deal = icruise.parse_item(
      item(departurePort="Tokyo", returnPort=" Seoul (Incheon)"), None
    )
    assert (deal.depart_port, deal.arrive_port) == ("Tokyo", "Seoul (Incheon)")

  def test_minus_99_means_price_on_request(self):
    deal = icruise.parse_item(item(price=-99, totalPerPerson=-99), None)
    assert deal.price is None

  def test_day_count_is_converted_to_nights(self):
    assert icruise.parse_item(item(numberOfDaysOrNights=10, dayOrNight="D"), None).nights == 9

  def test_missing_cabin_name_still_has_a_note(self):
    deal = icruise.parse_item(item(metaName=None), None)
    assert deal.price_note == "每人最低價"

  def test_unknown_cruise_line_logo_leaves_the_line_blank(self):
    deal = icruise.parse_item(item(cruiseLineLogo=".../logos/120w/new/999_120.gif"), None)
    assert deal.cruise_line == ""

  def test_unparseable_date_yields_none(self):
    assert icruise.parse_item(item(sailingDate="soon"), None) is None

  def test_package_with_air_is_skipped(self):
    # cruiseOnly=False 是含機票／陸上行程的套裝，價格跟其他來源的船票價不能比
    assert icruise.parse_item(item(cruiseOnly=False), None) is None


class TestCruiseLineLogos:
  def test_logo_id_is_read_from_the_image_url(self):
    assert icruise.logo_id("https://x/imgs/client/logos/120w/new/28_120.gif") == "28"
    assert icruise.logo_id("") is None
    assert icruise.logo_id(None) is None

  def test_every_line_seen_in_asia_is_mapped(self, september, october):
    # 對照表是從真實回應整理的；有新船公司時這個測試會告訴你該補哪個 id
    ids = {icruise.logo_id(x.get("cruiseLineLogo")) for x in september + october}
    assert ids <= set(icruise.CRUISE_LINE_BY_LOGO_ID)

  def test_names_align_with_cruisedirect(self):
    from cruise_deals.scrapers.cruisedirect import CRUISELINE_NAMES

    # 網頁的「船公司」篩選是照字串分組的，同一家公司兩種寫法會變成兩個選項
    shared = set(CRUISELINE_NAMES.values()) & set(icruise.CRUISE_LINE_BY_LOGO_ID.values())
    expected = {"Princess Cruises", "MSC Cruises", "Celebrity Cruises", "Norwegian Cruise Line"}
    assert expected <= shared


class TestWindowFilter:
  def test_only_target_ports_inside_the_window_are_kept(self, september, october):
    deals = icruise.deals_in_window(september + october, *WINDOW)

    assert deals
    assert {d.depart_port for d in deals} <= {"Keelung", "Tokyo", "Yokohama"}
    assert all(WINDOW[0] <= d.sail_date <= WINDOW[1] for d in deals)
    # 10/17 之後的 10 月航次要被窗口切掉（fixture 裡確實有這種目標港航次）
    parsed = [icruise.parse_item(x, None) for x in october]
    late = [
      d for d in parsed
      if d and d.depart_port in ("Tokyo", "Yokohama") and d.sail_date > WINDOW[1]
    ]
    assert late
    assert not any(d.sail_date > WINDOW[1] for d in deals)

  def test_same_sailing_on_two_pages_is_deduplicated(self):
    deals = icruise.deals_in_window([item(), item()], *WINDOW)
    assert len(deals) == 1

  def test_empty_asia_is_a_site_change(self):
    # 亞洲一整個月不可能 0 筆；整批空代表 API 改了（過濾後 0 筆才是正常）
    with pytest.raises(ParseError, match="0"):
      icruise.deals_in_window([], *WINDOW)

  def test_no_target_port_sailings_is_not_an_error(self):
    singapore = item(departurePort="Singapore", returnPort=" Singapore")
    assert icruise.deals_in_window([singapore], *WINDOW) == []


class TestFetchMonth:
  """一個月一次查詢，翻頁到不足一頁為止；壞回應要重試。"""

  def pages(self, *names: str) -> list[dict]:
    return [load(n) for n in names]

  def test_follows_pagination_until_a_short_page(self):
    responses = self.pages("icruise_api_2026-10_p1.json", "icruise_api_2026-10_p2.json")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
      bodies.append(json.loads(request.content))
      return httpx.Response(200, json=responses[len(bodies) - 1])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = icruise.fetch_month(client, "2026-10", delay_s=0)

    assert len(items) == 152
    assert [b["page"] for b in bodies] == [1, 2]
    assert all(b["date"] == "2026-10" for b in bodies)

  def test_short_first_page_stops_immediately(self):
    (payload,) = self.pages("icruise_api_2026-09_p1.json")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
      nonlocal calls
      calls += 1
      return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(icruise.fetch_month(client, "2026-09", delay_s=0)) == 55
    assert calls == 1

  def test_stops_at_the_page_cap(self):
    full = load("icruise_api_2026-10_p1.json")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
      nonlocal calls
      calls += 1
      return httpx.Response(200, json=full)  # 每頁都滿頁

    client = httpx.Client(transport=httpx.MockTransport(handler))
    icruise.fetch_month(client, "2026-10", delay_s=0)
    assert calls == icruise.MAX_PAGES

  def test_transient_5xx_is_retried(self):
    (payload,) = self.pages("icruise_api_2026-09_p1.json")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
      nonlocal calls
      calls += 1
      return httpx.Response(503) if calls == 1 else httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(icruise.fetch_month(client, "2026-09", delay_s=0)) == 55
    assert calls == 2

  def test_non_json_response_is_a_parse_error_after_retries(self):
    client = httpx.Client(
      transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>維護中</html>"))
    )
    with pytest.raises(ParseError, match="JSON"):
      icruise.fetch_month(client, "2026-09", delay_s=0)


class TestFetchWithRetry:
  def test_returns_result_after_transient_failures(self):
    attempts = []

    def flaky():
      attempts.append(1)
      if len(attempts) < 3:
        raise RuntimeError("transient")
      return "ok"

    assert with_retry(flaky, attempts=3, delay_s=0) == "ok"
    assert len(attempts) == 3

  def test_reraises_after_exhausting_attempts(self):
    calls = []

    def always_fails():
      calls.append(1)
      raise RuntimeError("permanent")

    with pytest.raises(RuntimeError, match="permanent"):
      with_retry(always_fails, attempts=3, delay_s=0)
    assert len(calls) == 3
