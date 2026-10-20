"""MSC 地中海郵輪美國站 www.msccruisesusa.com 擷取器測試。

跑在真實存下來的搜尋 API 回應上（`tests/fixtures/msc_cruises.json`），不需要網路。
fixture 是 2026-10-20 抓的亞洲區（commArea FAE）**60 天**窗口（10/20～12/19），
160 筆紀錄：那霸 98、基隆 60、東京 2。用 60 天而不是預設的 30 天，是因為抓的當下
一個月內一班基隆／東京航次都沒有（最早是 11/21），30 天的回應測不到任何東西。

這一站的重點：
  1. 每筆紀錄是「航次 × 艙等 × 票價方案」，同一航次只留最便宜的
  2. 價格用一般的每人價＋港務費，不用長者／會員限定的折扣價
  3. 單程航次（東京上、基隆下）的到達港要看 disembkPort
  4. 窗口內 0 筆是常態，但亞洲區連未來航次都查不到是欄位改名
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from cruise_deals import config
from cruise_deals.scrapers import msc
from cruise_deals.scrapers.base import BlockedError, ParseError, keep_cheapest

FIXTURES = Path(__file__).parent / "fixtures"

# fixture 抓取當下查的窗口
WINDOW = (date(2026, 10, 20), date(2026, 12, 19))


@pytest.fixture(scope="module")
def payload() -> dict:
  return json.loads((FIXTURES / "msc_cruises.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def hits(payload) -> list[dict]:
  return payload["results"][0]["hits"]


@pytest.fixture(scope="module")
def deals(hits) -> list:
  return sorted(keep_cheapest(msc.parse_hits(hits, *WINDOW)), key=lambda d: d.sail_date)


def by_cruise(hits: list[dict], cruise_id: str) -> list[dict]:
  return [h for h in hits if h["cruiseID"] == cruise_id]


class TestFixtureParsing:
  def test_only_keelung_and_tokyo_departures_are_kept(self, deals):
    # 那霸出發的 98 筆不是目標港
    assert {d.depart_port for d in deals} == {"Keelung", "Tokyo"}

  def test_one_deal_per_sailing(self, deals, hits):
    target = {h["cruiseID"] for h in hits if h["embkPort"]["key"] in ("KEE", "TYO")}
    assert len(deals) == len(target) == 6

  def test_sailings_are_the_ones_on_the_site(self, deals):
    assert [(d.sail_date, d.depart_port, d.nights) for d in deals] == [
      (date(2026, 11, 21), "Tokyo", 4),
      (date(2026, 11, 29), "Keelung", 5),
      (date(2026, 12, 4), "Keelung", 4),
      (date(2026, 12, 8), "Keelung", 4),
      (date(2026, 12, 12), "Keelung", 4),
      (date(2026, 12, 16), "Keelung", 4),
    ]

  def test_ship_and_line_match_the_other_sources(self, deals):
    # 「MSC Bellissima」與 SHIP_ALIASES["榮耀號"] 相同，才能跟 asiayo 合併比價
    assert {d.ship_name for d in deals} == {config.SHIP_ALIASES["榮耀號"]}
    assert {d.cruise_line for d in deals} == {"MSC Cruises"}

  def test_one_way_sailing_arrives_at_keelung(self, deals):
    tokyo = next(d for d in deals if d.depart_port == "Tokyo")
    assert tokyo.depart_port_raw == "Tokyo, Japan"
    assert tokyo.arrive_port == "Keelung"

  def test_round_trip_arrives_where_it_departed(self, deals):
    assert all(d.arrive_port == "Keelung" for d in deals if d.depart_port == "Keelung")

  def test_sea_days_are_not_ports_of_call(self, deals):
    tokyo = next(d for d in deals if d.depart_port == "Tokyo")
    assert tokyo.ports_of_call == ("Ishigaki, Japan",)
    assert all("Day at Sea" not in d.ports_of_call for d in deals)

  def test_detail_url_points_at_the_sailing(self, deals):
    assert deals[0].detail_url == (
      "https://www.msccruisesusa.com/search/itinerary?passengers=2%7C0%7C0%7C0"
      "&cruiseId=BE20261121TYOKEE&itineraryCode=UZJ9&viewPrice=person"
    )

  def test_price_is_usd(self, deals):
    assert all(d.currency == "USD" and d.source == "msc" for d in deals)


class TestPrice:
  def test_cheapest_cabin_plus_port_charges(self, deals, hits):
    # 12/12 最便宜的是內艙 IR1 的 432，加每人港務費 7
    records = by_cruise(hits, "BE20261212KEEKEE")
    assert min(h["prices"]["adultPrice"] for h in records) == 432
    assert {h["prices"]["portCharges"] for h in records} == {7}

    (deal,) = [d for d in deals if d.sail_date == date(2026, 12, 12)]
    assert deal.price == Decimal("439")

  def test_senior_only_discount_is_not_used(self, hits):
    # 這筆的長者價是 411，一般價是 432；拿長者價去跟其他來源的一般價比會誤導
    hit = next(
      h
      for h in by_cruise(hits, "BE20261212KEEKEE")
      if h["category"]["key"] == "IR1" and h["priceType"] == "EZAT"
    )
    assert hit["discountedPricesBySpecificPax"]["priceAfterDiscountSENIOR25PerAdult"] == 411
    assert msc.per_person_price(hit) == Decimal("439")

  def test_adult_price_is_half_the_cabin_price(self, hits):
    # adultPrice 是兩人同房時的每人價，不是整間艙房的價（取整數，所以會差 1）
    assert all(abs(h["prices"]["cabinPrice"] - 2 * h["prices"]["adultPrice"]) <= 1 for h in hits)

  def test_missing_port_charges_still_gives_the_fare(self):
    assert msc.per_person_price({"prices": {"adultPrice": 409}}) == Decimal("409")

  def test_no_fare_means_no_price(self):
    assert msc.per_person_price({"prices": {"adultPrice": 0, "portCharges": 10}}) is None
    assert msc.per_person_price({}) is None


class TestFiltering:
  def test_sailings_outside_the_window_are_dropped(self, hits):
    deals = msc.parse_hits(hits, date(2026, 12, 1), date(2026, 12, 10))
    assert {d.sail_date for d in deals} == {date(2026, 12, 4), date(2026, 12, 8)}

  def test_window_with_no_target_sailings_is_empty_not_an_error(self, hits):
    # 一個月內沒有基隆／東京航次是常態（抓的當下最早是 11/21）
    assert msc.parse_hits(hits, date(2026, 10, 20), date(2026, 11, 19)) == []

  def test_zero_hits_is_empty(self):
    assert msc.parse_hits([], *WINDOW) == []

  def test_all_records_malformed_is_a_site_change(self, hits):
    broken = deepcopy(hits[:5])
    for hit in broken:
      del hit["departureStartDate"]
    with pytest.raises(ParseError, match="解析不出來"):
      msc.parse_hits(broken, *WINDOW)

  def test_one_malformed_record_does_not_sink_the_rest(self, hits):
    records = deepcopy(by_cruise(hits, "BE20261212KEEKEE"))
    del records[0]["numberOfNights"]
    assert len(keep_cheapest(msc.parse_hits(records, *WINDOW))) == 1


class TestQuery:
  def test_filters_cover_area_pax_cruise_only_and_window(self):
    filters = msc.search_filters(date(2026, 10, 20), date(2026, 11, 19))
    assert "(commArea.key:FAE)" in filters
    assert "(noofAdults=2) AND (noofMinors=0) AND (noofJuniors=0)" in filters
    assert "(hasAir:false)" in filters  # 機＋船套裝價格含機票，不能比
    assert "(prices.availability:true)" in filters
    # 紀錄存的是出發日的 UTC 午夜（毫秒）；上界要含到結束日當天
    assert "departureStartDateTimestamp >= 1792454400000" in filters
    assert "departureStartDateTimestamp <= 1795132799999" in filters

  def test_filters_without_window_have_no_date_clause(self):
    assert "departureStartDateTimestamp" not in msc.search_filters()

  def test_body_queries_the_per_sailing_index_without_distinct(self):
    (request,) = msc.search_body("x", 2)["requests"]
    assert request["indexName"] == "prod-en-us-cruises"
    assert request["page"] == 2
    # distinct 留的是排名最前的那筆，不一定最便宜，要自己收斂
    assert request["distinct"] == 0


def _result(hits: list[dict], nb_hits: int | None = None, nb_pages: int = 1) -> dict:
  return {
    "results": [
      {"hits": hits, "nbHits": len(hits) if nb_hits is None else nb_hits, "nbPages": nb_pages}
    ]
  }


class TestFetching:
  def test_follows_pagination(self, hits):
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
      (body,) = json.loads(request.content)["requests"]
      pages.append(body["page"])
      return httpx.Response(200, json=_result([hits[body["page"]]], nb_hits=2, nb_pages=2))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    got = msc.fetch_hits(*WINDOW, client=client, delay_s=0)

    assert pages == [0, 1]
    assert got == hits[:2]

  def test_sends_a_browser_user_agent(self, hits):
    # Akamai 對 "Mozilla/5.0" 這種短 UA 回 401
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
      seen.append(request.headers["User-Agent"])
      return httpx.Response(200, json=_result(hits[:1]))

    msc.fetch_hits(*WINDOW, client=httpx.Client(transport=httpx.MockTransport(handler)), delay_s=0)
    assert seen == [config.USER_AGENT]

  def test_truncated_results_are_an_error(self, hits):
    client = httpx.Client(
      transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_result(hits[:3], nb_hits=5)))
    )
    with pytest.raises(ParseError, match="截斷"):
      msc.fetch_hits(*WINDOW, client=client, delay_s=0)

  def test_pagination_stops_at_the_page_cap(self, hits):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
      nonlocal calls
      calls += 1
      return httpx.Response(200, json=_result(hits[:1], nb_hits=1, nb_pages=999))

    msc.fetch_hits(*WINDOW, client=httpx.Client(transport=httpx.MockTransport(handler)), delay_s=0)
    assert calls == msc.MAX_PAGES

  def test_empty_window_with_future_sailings_is_fine(self):
    filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
      (body,) = json.loads(request.content)["requests"]
      filters.append(body["filters"])
      # 窗口查詢 0 筆，不限日期的查詢還有 1766 筆
      return httpx.Response(200, json=_result([], nb_hits=1766 if len(filters) == 2 else 0))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert msc.fetch_hits(*WINDOW, client=client, delay_s=0) == []
    assert "departureStartDateTimestamp" in filters[0]
    assert "departureStartDateTimestamp" not in filters[1]

  def test_no_future_sailings_at_all_is_a_site_change(self):
    # Algolia 對不存在的欄位是安靜地回 0 筆；亞洲區連未來航次都沒有就是欄位改名了
    client = httpx.Client(
      transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_result([])))
    )
    with pytest.raises(ParseError, match="改名"):
      msc.fetch_hits(*WINDOW, client=client, delay_s=0)

  @pytest.mark.parametrize("status", [401, 403])
  def test_akamai_rejection_is_reported_as_blocked(self, status):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    with pytest.raises(BlockedError, match="Akamai"):
      msc.fetch_hits(*WINDOW, client=client, delay_s=0)

  def test_non_json_response_is_a_parse_error(self):
    client = httpx.Client(
      transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>維護中</html>"))
    )
    with pytest.raises(ParseError, match="JSON"):
      msc.fetch_hits(*WINDOW, client=client, delay_s=0)

  @pytest.mark.parametrize(
    "payload", [{}, {"results": []}, {"results": [{"hits": []}]}, ["not", "an", "object"]]
  )
  def test_unexpected_shape_is_a_parse_error(self, payload):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    with pytest.raises(ParseError):
      msc.fetch_hits(*WINDOW, client=client, delay_s=0)

  def test_server_error_propagates(self):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
      msc.fetch_hits(*WINDOW, client=client, delay_s=0)
