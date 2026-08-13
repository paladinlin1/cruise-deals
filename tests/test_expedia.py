"""Expedia Cruises 解析器測試：跑在真實 API 回應的 fixture 上，不需要網路。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cruise_deals.scrapers import expedia
from cruise_deals.scrapers.base import ParseError

FIXTURES = Path(__file__).parent / "fixtures"

# fixture 擷取當下的查詢窗口
START = date(2026, 8, 13)
END = date(2026, 9, 12)


@pytest.fixture(scope="module")
def items() -> list[dict]:
  return json.loads((FIXTURES / "expedia_items.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def master() -> dict:
  return json.loads((FIXTURES / "expedia_master.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def deals(items, master):
  return expedia.parse_items(items, master, START, END)


class TestLowestPrice:
  """使用者要的是「數種房型取最低者」。"""

  def test_picks_cheapest_cabin_type(self):
    package = {
      "prices": [
        {
          "items": [
            {"name": "Inside", "value": 379.25},
            {"name": "Outside", "value": 519.25},
            {"name": "Balcony", "value": 619.25},
            {"name": "Suite", "value": 964.0},
          ]
        }
      ]
    }
    assert expedia.lowest_price(package) == Decimal("379.25")

  def test_ignores_tax_and_port_charge_entries(self):
    # 這些項目用 "code" 而非 "name"，金額很小，誤收會算出荒謬的低價
    package = {
      "prices": [
        {
          "items": [
            {"name": "Balcony", "value": 619.25},
            {"code": "CruiseTax", "value": 4.35},
            {"code": "PortCharge", "value": 66.0},
            {"code": "InsidePortCharge", "value": 66.0},
          ]
        }
      ]
    }
    assert expedia.lowest_price(package) == Decimal("619.25")

  def test_no_cabin_prices_returns_none(self):
    assert expedia.lowest_price({"prices": [{"items": [{"code": "CruiseTax", "value": 4.35}]}]}) is None

  def test_missing_prices_key_returns_none(self):
    assert expedia.lowest_price({}) is None

  def test_zero_value_is_not_treated_as_a_price(self):
    package = {"prices": [{"items": [{"name": "Inside", "value": 0}]}]}
    assert expedia.lowest_price(package) is None

  def test_reads_currency_code(self):
    package = {"prices": [{"items": [{"name": "Inside", "value": 10}], "currencyCode": "CAD"}]}
    assert expedia.currency_of(package) == "CAD"

  def test_currency_defaults_to_usd(self):
    assert expedia.currency_of({"prices": [{"items": []}]}) == "USD"


class TestParseItems:
  def test_returns_only_sailings_inside_the_window(self, deals):
    # fixture 裡有遠至 2029 年的航次，必須被排除
    assert len(deals) == 3
    assert all(START <= d.sail_date <= END for d in deals)

  def test_all_deals_come_from_expedia(self, deals):
    assert {d.source for d in deals} == {"expedia"}

  def test_first_deal_fields(self, deals):
    deal = sorted(deals, key=lambda d: d.sail_date)[0]
    assert deal.sail_date == date(2026, 8, 16)
    assert deal.depart_port == "Keelung"
    assert deal.ship_name == "Costa Serena"
    assert deal.cruise_line == "Costa Cruises"
    assert deal.nights == 3
    assert deal.price == Decimal("379.25")
    assert deal.currency == "USD"

  def test_departure_port_codes_are_resolved_to_names(self, deals):
    # 原始資料只有 "KEEL"，必須透過主檔轉成可讀名稱再正規化
    assert all(d.depart_port in ("Keelung", "Tokyo", "Yokohama") for d in deals)
    assert all("KEEL" not in d.depart_port_raw or "Keelung" in d.depart_port_raw for d in deals)

  def test_arrival_port_is_resolved(self, deals):
    deal = sorted(deals, key=lambda d: d.sail_date)[0]
    assert deal.arrive_port == "Keelung"

  def test_each_package_becomes_its_own_deal(self, deals):
    # 同一個 itinerary 有多個出發日期，每個都要獨立成一筆
    dates = sorted(d.sail_date for d in deals)
    assert dates == [date(2026, 8, 16), date(2026, 8, 19), date(2026, 8, 23)]

  def test_nights_come_from_itinerary_duration(self, deals):
    by_date = {d.sail_date: d for d in deals}
    assert by_date[date(2026, 8, 19)].nights == 4

  def test_detail_url_points_at_package_page(self, deals):
    deal = sorted(deals, key=lambda d: d.sail_date)[0]
    assert deal.detail_url.startswith("https://bookus.expediacruises.com/swift/cruise/package/")

  def test_scraped_at_is_timezone_aware(self, deals):
    assert all(d.scraped_at.tzinfo is not None for d in deals)

  def test_price_note_mentions_cabin_basis(self, deals):
    assert all("房型" in d.price_note or "cabin" in d.price_note.lower() for d in deals)


class TestParseItemsEdgeCases:
  def test_unknown_ship_id_does_not_crash(self, master):
    item = {
      "itinerary": {"duration": 3, "departure": {"code": "KEEL"}, "arrival": {"code": "KEEL"}},
      "ship": {"id": 999999},
      "packages": [{"id": 1, "startDateTime": "16-Aug-2026", "prices": [{"items": [{"name": "Inside", "value": 100}]}]}],
    }
    result = expedia.parse_items([item], master, START, END)
    assert len(result) == 1
    assert result[0].ship_name == ""

  def test_unknown_port_code_is_skipped(self, master):
    # 非目標出發港（API 已篩過，這是防禦性檢查）
    item = {
      "itinerary": {"duration": 3, "departure": {"code": "SIN"}, "arrival": {"code": "SIN"}},
      "ship": {"id": 1095},
      "packages": [{"id": 1, "startDateTime": "16-Aug-2026", "prices": []}],
    }
    assert expedia.parse_items([item], master, START, END) == []

  def test_package_without_date_is_skipped(self, master):
    item = {
      "itinerary": {"duration": 3, "departure": {"code": "KEEL"}, "arrival": {"code": "KEEL"}},
      "ship": {"id": 1095},
      "packages": [{"id": 1, "prices": []}, {"id": 2, "startDateTime": "16-Aug-2026", "prices": []}],
    }
    assert len(expedia.parse_items([item], master, START, END)) == 1

  def test_sailing_without_price_still_produces_a_deal(self, master):
    item = {
      "itinerary": {"duration": 3, "departure": {"code": "KEEL"}, "arrival": {"code": "KEEL"}},
      "ship": {"id": 1095},
      "packages": [{"id": 1, "startDateTime": "16-Aug-2026", "prices": []}],
    }
    result = expedia.parse_items([item], master, START, END)
    assert len(result) == 1
    assert result[0].price is None

  def test_empty_item_list_returns_empty(self, master):
    assert expedia.parse_items([], master, START, END) == []

  def test_master_without_ports_raises(self, items):
    # 主檔抓失敗時不能安靜地產出一堆沒有港口的資料
    with pytest.raises(ParseError):
      expedia.parse_items(items, {"ship": {}, "cruiseline": {}, "port": {}}, START, END)


class TestBuildRequest:
  def test_filters_target_departure_ports(self):
    body = expedia.build_search_body()
    port_filter = [f for f in body["filters"] if f["key"] == "departurePortCode"][0]
    assert set(port_filter["values"]) >= {"KEEL", "TYO", "YOK"}

  def test_page_size_respects_api_limit(self):
    # API 明確回報 "Maximum Page Size Limit 50 is Exceeded"
    assert expedia.PAGE_SIZE <= 50

  def test_search_url_uses_valid_sort_column(self):
    # "departureDate" 會被 API 拒絕（Invalid Value for sortColumn）
    url = expedia.build_search_url("https://x/nitroapi/v2/cruise", page_index=1)
    assert "sortColumn=departureDateTime" in url
    assert "pageStart=1" in url
