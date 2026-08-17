"""asiayo.com 擷取器測試。

跑在真實存下來的頁面上（`tests/fixtures/asiayo_*.html`），不需要網路。

這一站最容易踩的兩個地雷，都各有專屬測試：
  1. 同一筆商品有多個出發日，價格是「區間最低價」不是逐日價
  2. TYO 把東京與橫濱併成一個港，要靠行程第一天的敘述才分得出來
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cruise_deals.scrapers import asiayo
from cruise_deals.scrapers.base import ParseError

FIXTURES = Path(__file__).parent / "fixtures"

# fixture 是用這個窗口抓下來的
WINDOW = (date(2026, 8, 17), date(2026, 9, 16))


def load(name: str) -> str:
  return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def keelung_html() -> str:
  return load("asiayo_keelung.html")


@pytest.fixture(scope="module")
def tokyo_html() -> str:
  return load("asiayo_tokyo.html")


@pytest.fixture(scope="module")
def empty_html() -> str:
  return load("asiayo_empty.html")


def deals_of(html: str):
  return asiayo.parse_items(asiayo.extract_items(html), *WINDOW)


class TestSearchUrl:
  def test_only_carries_dates_and_page(self):
    url = asiayo.build_search_url("KEE", date(2026, 8, 17), date(2026, 9, 16))
    assert "/cruise/list/port/KEE/route/all/" in url
    assert "startDate=2026-08-17" in url
    assert "endDate=2026-09-16" in url
    assert "page=1" in url

  def test_does_not_carry_cruise_or_company_filters(self):
    # 使用者分享的網址會帶這兩個參數，沿用會少抓資料
    url = asiayo.build_search_url("TYO", date(2026, 8, 17), date(2026, 9, 16))
    assert "cruiseIds" not in url
    assert "companyIds" not in url


class TestPayloadExtraction:
  def test_reads_items_from_the_rsc_payload(self, keelung_html):
    assert len(asiayo.extract_items(keelung_html)) == 6

  def test_reads_pagination_metadata(self, keelung_html):
    assert asiayo.page_meta(keelung_html) == (6, 20)

  def test_empty_result_has_no_metadata(self, empty_html):
    # 沒有結果時該站不輸出 total，這是「真的沒有」而不是改版
    assert asiayo.page_meta(empty_html) is None
    assert asiayo.extract_items(empty_html) == []

  def test_missing_payload_yields_nothing_rather_than_crashing(self):
    assert asiayo.extract_items("<html><body>nothing here</body></html>") == []


class TestParsing:
  def test_days_are_converted_to_nights(self, keelung_html):
    # 「6 日」的行程是 5 夜，外國站都以夜數計
    deal = next(d for d in deals_of(keelung_html) if d.sail_date == date(2026, 8, 23))
    assert deal.nights == 5

  def test_prices_are_new_taiwan_dollars(self, keelung_html):
    deal = next(d for d in deals_of(keelung_html) if d.sail_date == date(2026, 8, 23))
    assert deal.currency == "TWD"
    assert deal.price == Decimal("18000")

  def test_chinese_ship_name_is_mapped_to_english(self, keelung_html):
    deal = deals_of(keelung_html)[0]
    assert deal.ship_name == "Star Voyager"
    assert deal.ship_name_raw == "探索星號"
    assert deal.cruise_line == "Star Cruises"

  def test_ship_name_outside_the_brackets_is_still_found(self, tokyo_html):
    # 「【公主遊輪】鑽石公主號～…」——括號裡只有船公司
    deal = next(d for d in deals_of(tokyo_html) if d.ship_name == "Diamond Princess")
    assert deal.ship_name_raw == "鑽石公主號"
    assert deal.cruise_line == "Princess Cruises"

  def test_itinerary_comes_from_the_daily_journey(self, keelung_html):
    deal = next(d for d in deals_of(keelung_html) if d.sail_date == date(2026, 8, 23))
    assert deal.ports_of_call == ("基隆港", "鹿兒島", "熊本", "沖繩・那霸 NCT", "基隆港")

  def test_round_trip_ends_at_the_home_port(self, keelung_html):
    # 母港在第一天與最後一天都出現，全域去重會把回程那筆吃掉
    deal = next(d for d in deals_of(keelung_html) if d.sail_date == date(2026, 8, 23))
    assert deal.arrive_port == "基隆港"

  def test_sea_days_are_not_ports(self, keelung_html):
    for deal in deals_of(keelung_html):
      assert not any("海上" in port for port in deal.ports_of_call)

  def test_detail_url_points_at_the_chosen_departure_date(self, keelung_html):
    deal = next(d for d in deals_of(keelung_html) if d.sail_date == date(2026, 9, 13))
    assert deal.detail_url.endswith("/cruise/item/51219/?activityStartDate=2026-09-13")


class TestMultipleDepartureDates:
  """一筆商品含多個出發日時要展開成多筆 Deal。"""

  def test_each_available_date_becomes_its_own_row(self, keelung_html):
    dates = [d.sail_date for d in deals_of(keelung_html) if d.ship_name_raw == "探索星號"]
    assert date(2026, 8, 23) in dates
    assert date(2026, 9, 13) in dates

  def test_dates_outside_the_window_are_dropped(self, keelung_html):
    narrow = asiayo.parse_items(
      asiayo.extract_items(keelung_html), date(2026, 8, 17), date(2026, 8, 25)
    )
    assert [d.sail_date for d in narrow] == [date(2026, 8, 23)]


class TestTokyoYokohamaDisambiguation:
  """TYO 一個代碼涵蓋兩個港，判錯就跟外國站對不上。"""

  def test_diamond_princess_is_yokohama(self, tokyo_html):
    # 行程第一天寫「日本 東京 (橫濱) 登船」，外國站也都寫 Yokohama
    deal = next(d for d in deals_of(tokyo_html) if d.ship_name == "Diamond Princess")
    assert deal.depart_port == "Yokohama"

  def test_celebrity_millennium_is_tokyo(self, tokyo_html):
    # 行程第一天只寫「日本東京出發」，沒有橫濱
    deal = next(
      d for d in deals_of(tokyo_html) if d.ship_name == "Celebrity Millennium"
    )
    assert deal.depart_port == "Tokyo"

  def test_source_wording_is_kept_for_debugging(self, tokyo_html):
    assert deals_of(tokyo_html)[0].depart_port_raw == "東京（東京/橫濱）"


class TestParserHealthCheck:
  """安靜地回空清單會讓下游誤刪好資料，所以要大聲失敗。"""

  def test_claiming_results_but_parsing_none_raises(self, monkeypatch):
    html = "<html>版面改了</html>"
    monkeypatch.setattr(asiayo, "extract_items", lambda _: [])
    monkeypatch.setattr(asiayo, "page_meta", lambda _: (12, 20))
    monkeypatch.setattr(asiayo, "fetch_page", lambda *a, **k: html)

    with pytest.raises(ParseError, match="12"):
      asiayo.fetch_chunk(None, "KEE", *WINDOW)

  def test_genuinely_empty_result_is_not_an_error(self, monkeypatch, empty_html):
    monkeypatch.setattr(asiayo, "fetch_page", lambda *a, **k: empty_html)
    assert asiayo.fetch_chunk(None, "KEE", *WINDOW) == []


class TestWithinSourceDedup:
  """同一航次拆成多個商品時只留最便宜的，不要變成自己跟自己比價。"""

  def test_cheaper_row_wins(self):
    from factories import make_deal

    collected: dict = {}
    asiayo._keep_cheaper(collected, make_deal(source="asiayo", price=Decimal("30000")))
    asiayo._keep_cheaper(collected, make_deal(source="asiayo", price=Decimal("24000")))

    assert len(collected) == 1
    assert next(iter(collected.values())).price == Decimal("24000")

  def test_priced_row_beats_unpriced_one(self):
    from factories import make_deal

    collected: dict = {}
    asiayo._keep_cheaper(collected, make_deal(source="asiayo", price=None))
    asiayo._keep_cheaper(collected, make_deal(source="asiayo", price=Decimal("24000")))

    assert next(iter(collected.values())).price == Decimal("24000")
