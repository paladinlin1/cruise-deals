"""asiayo.com 擷取器測試。

跑在真實存下來的頁面上（`tests/fixtures/asiayo_*.html`），不需要網路。

這一站最容易踩的三個地雷，都各有專屬測試：
  1. 同一筆商品有多個出發日，價格是「區間最低價」不是逐日價
  2. TYO 把東京與橫濱併成一個港，要靠行程第一天的敘述才分得出來
  3. RSC payload 會把重複物件寫成 `$5f:props:…` 參照字串，
     哪一份是本體、哪一份是參照，順序會變（2026-09-16 實際翻過一次）
"""

from __future__ import annotations

import json
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


@pytest.fixture(scope="module")
def refs_html() -> str:
  """2026-09-16 的基隆頁：卡片上的 port／journey／availableDates 全是參照字串。"""
  return load("asiayo_keelung_refs.html")


@pytest.fixture(scope="module")
def textrow_html() -> str:
  """2026-09-16 的東京頁：被參照的列 62 黏在一個沒有換行結尾的 T 文字列後面。"""
  return load("asiayo_tokyo_textrow.html")


def flight_html(*rows: str) -> str:
  """把幾列 RSC flight 包成最小可解析的頁面，用來做合成案例。"""
  payload = json.dumps("\n".join(rows) + "\n", ensure_ascii=False)
  return f"<html><body><script>self.__next_f.push([1,{payload}])</script></body></html>"


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


class TestFlightReferences:
  """React Flight 去重：同一物件第二次出現只剩 `$<列>:<路徑>` 字串。

  8/17 的頁面是卡片先輸出本體、GA 追蹤事件裡放參照；9/16 對調過來，
  卡片上的 port／journey／availableDates 變成指向 GA 事件那一列的參照，
  直接讀就會 `'str' object has no attribute 'get'`。
  """

  def test_card_fields_are_resolved_to_the_referenced_objects(self, refs_html):
    (item,) = asiayo.extract_items(refs_html)
    assert item["port"] == {"id": "KEE", "name": "基隆"}
    assert item["availableDates"] == ["2026-09-20"]
    assert item["journey"]["daily"][0]["description"].startswith("第一天：基隆港")

  def test_resolved_page_parses_into_deals(self, refs_html):
    (deal,) = asiayo.parse_items(
      asiayo.extract_items(refs_html), date(2026, 9, 16), date(2026, 9, 20)
    )
    assert deal.sail_date == date(2026, 9, 20)
    assert deal.depart_port == "Keelung"
    assert deal.ship_name == "Star Voyager"
    assert deal.nights == 3
    assert deal.price == Decimal("14000")
    assert deal.ports_of_call == ("基隆港", "沖繩・那霸 NCT", "石垣島", "基隆港")

  def test_props_segment_maps_to_the_react_element_tuple(self):
    # 元件在 payload 裡是 ["$", type, key, props]，路徑裡的 "props" 對應索引 3；
    # 參照可以指到另一列的任意深度，含陣列索引
    html = flight_html(
      '5d:["$","$L68","cruise-1",{"item":{"id":1,"name":"【麗星郵輪探索星號】測試",'
      '"port":"$5f:props:events:1:properties:bnbs:0:port"}}]',
      '5f:["$","$L67",null,{"events":[{"type":"ga-custom"},'
      '{"properties":{"bnbs":[{"id":1,"port":{"id":"KEE","name":"基隆"}}]}}]}]',
    )
    (item,) = asiayo.extract_items(html)
    assert item["port"] == {"id": "KEE", "name": "基隆"}

  def test_references_inside_a_resolved_object_are_resolved_too(self):
    # 本體裡的欄位也可能再指向第三列
    html = flight_html(
      '5d:["$","$L68","cruise-1",{"item":{"id":1,"name":"測試",'
      '"journey":"$5f:props:bnbs:0:journey"}}]',
      '5f:["$","$L67",null,{"bnbs":[{"id":1,"journey":{"daily":"$60:props:daily"}}]}]',
      '60:["$","$L69",null,{"daily":[{"description":"第一天：基隆港"}]}]',
    )
    (item,) = asiayo.extract_items(html)
    assert item["journey"] == {"daily": [{"description": "第一天：基隆港"}]}

  def test_row_glued_to_a_text_row_is_still_found(self, textrow_html):
    # T 文字列是「T<十六進位位元組長度>,<原文>」，靠長度而不是換行收尾，
    # 所以下一列會緊接在原文後面、不在行首。用 ^列號: 去找就找不到。
    items = asiayo.extract_items(textrow_html)
    assert len(items) == 7
    assert all(isinstance(item["port"], dict) for item in items)

  def test_text_row_length_is_counted_in_bytes(self):
    text = "⑤ 需於飯店現場支付住宿稅\n第二行"
    html = flight_html(
      '5d:["$","$L68","cruise-1",{"item":{"id":1,"name":"測試","port":"$62:props:port"}}]',
      f"61:T{len(text.encode('utf-8')):x},{text}" '62:["$","$L6a",null,{"port":{"id":"TYO"}}]',
    )
    (item,) = asiayo.extract_items(html)
    assert item["port"] == {"id": "TYO"}

  def test_reference_to_a_text_row_yields_the_text(self):
    # 長字串會被拉出去變成 T 列，原地只留 "$61"
    text = "第一天：基隆港【郵輪20:00啟航】"
    html = flight_html(
      '5d:["$","$L68","cruise-1",{"item":{"id":1,"name":"測試","note":"$61"}}]',
      f"61:T{len(text.encode('utf-8')):x},{text}",
    )
    (item,) = asiayo.extract_items(html)
    assert item["note"] == text

  def test_non_reference_dollar_strings_are_left_alone(self):
    # "$undefined"、"$L…" 之類是 Flight 的其他型別標記，不是列參照
    html = flight_html(
      '5d:["$","$L68","cruise-1",{"item":{"id":1,"name":"測試",'
      '"route":"$undefined","lazy":"$L5"}}]',
    )
    (item,) = asiayo.extract_items(html)
    assert item["route"] == "$undefined"
    assert item["lazy"] == "$L5"

  def test_dangling_reference_fails_loudly(self):
    # 指到不存在的列＝格式變了；安靜留著字串會讓下游用奇怪的錯誤炸掉
    html = flight_html(
      '5d:["$","$L68","cruise-1",{"item":{"id":1,"name":"測試",'
      '"port":"$7a:props:port"}}]',
    )
    with pytest.raises(ParseError, match=r"\$7a:props:port"):
      asiayo.extract_items(html)


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
