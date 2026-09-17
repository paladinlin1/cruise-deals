"""雄獅旅遊 travel.liontravel.com 擷取器測試。

跑在真實存下來的搜尋 API 回應上（`tests/fixtures/lion_cruise.json`，
2026-09-16 抓的 TripTypes=01 一個月窗口，44 個商品），不需要網路。

這一站的重點是三個判斷：
  1. 「暫時額滿」的團期不收——額滿的價格拿去比價會誤導最低價
  2. 出發港只認「X出發」「X上下」「X上Y下」三種寫法，
     不能整段字串比對（「神戶上基隆下」不是基隆出發）
  3. 整批 0 筆是改版，港口過濾後 0 筆才是常態
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
from cruise_deals.scrapers import lion
from cruise_deals.scrapers.base import ParseError, keep_cheapest

FIXTURES = Path(__file__).parent / "fixtures"

# fixture 抓取當下查的窗口
WINDOW = (date(2026, 9, 16), date(2026, 10, 16))


@pytest.fixture(scope="module")
def payload() -> dict:
  return json.loads((FIXTURES / "lion_cruise.json").read_text(encoding="utf-8"))


def sailing(group_id: str, go_date: str, status: str, price: str = "18,950") -> dict:
  """建一個最小的團期（GroupList 裡的一筆）。"""
  return {
    "GroupID": group_id,
    "GoDate": go_date,
    "Status": status,
    "StraightLowestPrice": price,
  }


def norm_group(**overrides) -> dict:
  """建一個最小的雄獅商品（NormGroup），只覆寫關心的欄位。"""
  base = {
    "NormGroupID": "3eb9d401-a8d5-4459-94ee-701407c2e3e2",
    "TourName": "麗星郵輪｜探索星號｜基隆出發｜那霸．石垣島｜自由行4日",
    "TourDays": 4,
    "TourSource": "Lion",
    "GroupList": [
      {
        "GroupID": "26JCO13SN-T",
        "GoDate": "2026/10/13",
        "Status": "selling",
        "StatusText": "熱銷中",
        "StraightLowestPrice": "18,950",
        "IndustryLowestPrice": "17,300",
      }
    ],
  }
  base.update(overrides)
  return base


class TestResponseShape:
  def test_missing_norm_group_list_raises(self):
    with pytest.raises(ParseError, match="NormGroupList"):
      lion.extract_norm_groups({"TotalCount": 3})

  def test_non_object_response_raises(self):
    with pytest.raises(ParseError):
      lion.extract_norm_groups(["not", "an", "object"])

  def test_zero_products_overall_is_treated_as_a_site_change(self):
    # 全世界一個月內不可能 0 個郵輪商品，這是改版不是沒貨
    with pytest.raises(ParseError, match="0"):
      lion.parse_norm_groups([], *WINDOW)


class TestSailingStatus:
  def test_temporarily_full_sailings_are_excluded(self, payload):
    # fixture 全站 76 個團期有 72 個「暫時額滿」，
    # 而基隆／東京出發的 26 個團期全部額滿——所以結果必須是空的
    groups = payload["NormGroupList"]
    full_ids = {
      entry["GroupID"]
      for group in groups
      for entry in group["GroupList"]
      if entry["Status"] == "full"
    }
    target = [
      entry
      for group in groups
      if lion.parse_route(group["TourName"])
      for entry in group["GroupList"]
    ]
    assert len(full_ids) == 72
    assert len(target) == 26 and all(e["Status"] == "full" for e in target)

    assert lion.parse_norm_groups(groups, *WINDOW) == []

  def test_selling_and_ensure_sailings_are_kept(self):
    group = norm_group(
      GroupList=[
        sailing("A", "2026/10/13", "selling"),
        sailing("B", "2026/10/20", "ensure"),
        sailing("C", "2026/10/27", "full"),
      ]
    )

    deals = lion.parse_norm_groups([group], date(2026, 10, 1), date(2026, 10, 31))

    assert [d.sail_date for d in deals] == [date(2026, 10, 13), date(2026, 10, 20)]


class TestDeparturePort:
  """只認三種寫法；認不出或非目標港一律回 None。"""

  def test_keelung_departure_in_pipe_format(self):
    assert lion.parse_route("麗星郵輪｜探索星號｜基隆出發｜那霸｜自由行3日") == (
      "Keelung",
      "基隆",
      "Keelung",
    )

  def test_keelung_departure_in_bracket_format(self):
    assert lion.parse_route(
      "【主題旅遊】基隆出發～麗星郵輪探索星號～2026年航次｜那霸｜3天2夜｜自由行"
    ) == ("Keelung", "基隆", "Keelung")

  def test_round_trip_boarding_port(self):
    assert lion.parse_route(
      "公主遊輪鑽石公主號｜關門海峽．廣島．釜山｜東京上下｜自由行10日"
    ) == ("Tokyo", "東京", "Tokyo")

  def test_one_way_boarding_port_keeps_the_other_end_as_arrival(self):
    assert lion.parse_route("挪威郵輪翡翠號｜環日本｜東京上首爾下｜自由行12日") == (
      "Tokyo",
      "東京",
      "首爾",
    )

  def test_yokohama_is_recognised(self):
    assert lion.parse_route("飛鳥Ⅲ｜熊野｜橫濱上下｜自由行5日") == (
      "Yokohama",
      "橫濱",
      "Yokohama",
    )

  def test_arriving_at_keelung_is_not_a_keelung_departure(self):
    assert lion.parse_route("三井海洋郵輪富士號｜沖繩｜神戶上基隆下｜自由行7日") is None

  def test_parenthesised_note_after_the_port_is_ignored(self):
    assert lion.parse_route(
      "公主遊輪鑽石公主號｜關門海峽．廣島．釜山｜東京上下（橫濱港）｜自由行10日"
    ) == ("Tokyo", "東京", "Tokyo")

  def test_flight_package_marked_by_a_city_departure_is_dropped(self):
    # 標題同時有「台北出發」與「東京上下」的是機＋船套裝，價格含機票，不能跟外國站比
    assert lion.parse_route("台北出發｜公主遊輪鑽石公主號｜青森．函館｜東京上下｜自由行9日") is None

  def test_non_target_departure_is_dropped(self):
    assert lion.parse_route("26年航程｜迪士尼探險號｜新加坡出發｜海上巡航｜自由行4日") is None

  def test_title_without_a_departure_is_dropped(self):
    # TripTypes=01 也會夾雜這種國內團（小琉球渡輪），沒有出發港寫法就不收
    assert lion.parse_route("高屏3日遊｜最高折$2000｜小琉球湛藍環島.鹿港懷舊巡禮三日") is None


class TestItineraryPorts:
  def test_ports_separated_by_fullwidth_dot(self):
    assert lion.itinerary_ports("麗星郵輪｜探索星號｜基隆出發｜那霸．石垣島｜自由行4日") == (
      "那霸",
      "石垣島",
    )

  def test_ports_separated_by_ascii_dot_in_bracket_format(self):
    assert lion.itinerary_ports(
      "【主題旅遊】基隆出發～麗星郵輪探索星號～2026年航次｜那霸.石垣島｜4天3夜｜自由行"
    ) == ("那霸", "石垣島")

  def test_sea_days_only_yield_no_ports(self):
    assert lion.itinerary_ports("麗星郵輪｜探索星號｜基隆出發｜海上遊｜自由行3日") == ()
    assert lion.itinerary_ports(
      "【主題旅遊】基隆出發～麗星郵輪探索星號～2026年航次｜公海巡遊｜3天2夜｜自由行"
    ) == ()

  def test_parenthesised_note_is_dropped(self):
    assert lion.itinerary_ports(
      "春夏出遊｜MSC郵輪歐羅巴號｜馬賽．熱那亞．那不勒斯（龐貝）｜巴塞隆納上下｜自由行8日"
    ) == ("馬賽", "熱那亞", "那不勒斯")

  def test_ship_and_marketing_segments_are_not_ports(self):
    assert lion.itinerary_ports(
      "26航程｜公主遊輪尋夢公主號｜凱契根．安提卡灣．朱諾｜溫哥華上下｜自由行8日"
    ) == ("凱契根", "安提卡灣", "朱諾")

  def test_marketing_segment_with_dots_does_not_win_over_the_port_list(self):
    # 「艙等．賣點」放第一段是雄獅常見寫法；停靠港是分隔符號最多的那一段
    assert lion.itinerary_ports(
      "豪華陽台艙．含岸上觀光｜探索星號｜基隆出發｜那霸．石垣島｜自由行4日"
    ) == ("那霸", "石垣島")

  def test_route_description_is_not_a_port(self):
    assert lion.itinerary_ports("挪威郵輪翡翠號｜環日本｜東京上首爾下｜自由行12日") == ()


class TestParsing:
  def test_tour_days_are_converted_to_nights(self):
    (deal,) = lion.parse_norm_groups([norm_group(TourDays=4)], *WINDOW)
    assert deal.nights == 3  # 「4日」的行程是 3 夜

  def test_price_is_the_straight_lowest_price_in_twd(self):
    (deal,) = lion.parse_norm_groups([norm_group()], *WINDOW)
    assert deal.price == Decimal("18950")
    assert deal.currency == "TWD"

  def test_chinese_ship_name_is_mapped_to_english(self):
    (deal,) = lion.parse_norm_groups([norm_group()], *WINDOW)
    assert deal.ship_name == "Star Voyager"
    assert deal.ship_name_raw == "探索星號"
    assert deal.cruise_line == "Star Cruises"

  def test_norwegian_jade_from_tokyo_is_mapped(self):
    # 雄獅東京出發的資料裡有這艘，別名表原本沒有
    group = norm_group(
      TourName="挪威郵輪翡翠號｜環日本｜東京上首爾下｜自由行12日",
      TourDays=12,
      GroupList=[
        sailing("26JCO07NCL-T", "2026/10/07", "selling", "79,400"),
      ],
    )
    (deal,) = lion.parse_norm_groups([group], *WINDOW)
    assert deal.ship_name == "Norwegian Jade"
    assert deal.ship_name_raw == "翡翠號"
    assert deal.cruise_line == "Norwegian Cruise Line"
    assert deal.depart_port == "Tokyo"
    assert deal.arrive_port == "首爾"
    assert deal.ports_of_call == ()

  def test_unmapped_ship_falls_back_to_the_ship_segment_not_the_first_segment(self):
    # 別名表沒有的船，船名要從「…號」那一段取，而不是第一段的行銷詞——
    # 否則 dedup_key 與「沒對應到英文名」的警告都會變成「25．26年航程」
    group = norm_group(
      TourName="25．26年航程｜挪威郵輪暢悅號｜那霸．石垣島｜基隆上下｜自由行8日",
      TourDays=8,
    )
    (deal,) = lion.parse_norm_groups([group], *WINDOW)
    assert deal.ship_name == "暢悅號"
    assert deal.ship_name_raw == "暢悅號"
    assert deal.cruise_line == "Norwegian Cruise Line"

  def test_detail_url_points_at_the_sailing(self):
    (deal,) = lion.parse_norm_groups([norm_group()], *WINDOW)
    assert deal.detail_url == (
      "https://travel.liontravel.com/detail"
      "?NormGroupID=3eb9d401-a8d5-4459-94ee-701407c2e3e2&GroupID=26JCO13SN-T"
    )

  def test_source_and_ports_are_filled_in(self):
    (deal,) = lion.parse_norm_groups([norm_group()], *WINDOW)
    assert deal.source == "lion"
    assert deal.depart_port == "Keelung"
    assert deal.depart_port_raw == "基隆"
    assert deal.arrive_port == "Keelung"
    assert deal.ports_of_call == ("那霸", "石垣島")

  def test_sailings_outside_the_window_are_dropped(self):
    assert lion.parse_norm_groups([norm_group()], date(2026, 11, 1), date(2026, 11, 30)) == []

  def test_non_target_departures_are_dropped(self):
    group = norm_group(TourName="26年航程｜迪士尼探險號｜新加坡出發｜海上巡航｜自由行4日")
    assert lion.parse_norm_groups([group], *WINDOW) == []

  def test_unparseable_date_skips_that_sailing_only(self):
    group = norm_group(
      GroupList=[
        sailing("A", "not a date", "selling"),
        sailing("B", "2026/10/13", "selling"),
      ]
    )
    deals = lion.parse_norm_groups([group], *WINDOW)
    assert [d.detail_url.rsplit("=", 1)[1] for d in deals] == ["B"]

  def test_fixture_yields_deals_once_a_keelung_sailing_opens(self, payload):
    # 把 fixture 裡一筆基隆團期從額滿改成熱銷，就該出現在結果裡
    groups = deepcopy(payload["NormGroupList"])
    target = next(
      g for g in groups if g["TourName"].startswith("麗星郵輪｜探索星號｜基隆出發｜石垣島")
    )
    target["GroupList"][0]["Status"] = "selling"

    deals = lion.parse_norm_groups(groups, *WINDOW)

    assert [(d.sail_date, d.ship_name, d.price) for d in deals] == [
      (date(2026, 9, 18), "Star Voyager", Decimal("16300"))
    ]


class TestWithinSourceDedup:
  def test_same_sailing_from_two_suppliers_keeps_the_cheaper(self):
    # 雄獅自家（TourSource=Lion）與「【主題旅遊】」（GoUni）會對同一航次各開一個商品
    own = norm_group()
    partner = norm_group(
      NormGroupID="0df3b568-e5e4-4623-b95e-ccb92a9aebd7",
      TourName="【主題旅遊】基隆出發～麗星郵輪探索星號～2026年航次｜那霸.石垣島｜4天3夜｜自由行",
      TourSource="GoUni",
      GroupList=[
        sailing("26JCO13UNS4-ZP", "2026/10/13", "selling", "11,000"),
      ],
    )

    deals = keep_cheapest(lion.parse_norm_groups([own, partner], *WINDOW))

    assert len(deals) == 1
    assert deals[0].price == Decimal("11000")

  def test_quote_on_request_never_replaces_a_real_price(self):
    from factories import make_deal

    priced = make_deal(source="lion", price=Decimal("12900"))
    unpriced = make_deal(source="lion", price=None)

    assert keep_cheapest([unpriced, priced])[0].price == Decimal("12900")
    assert keep_cheapest([priced, unpriced])[0].price == Decimal("12900")


class TestFetching:
  """翻頁要走完 TotalPage，而且每頁都用同一份查詢條件。"""

  def test_follows_pagination_and_concatenates_pages(self):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
      body = json.loads(request.content)
      bodies.append(body)
      page = body["Page"]
      return httpx.Response(
        200,
        json={
          "TotalCount": 2,
          "TotalPage": 2,
          "NormGroupList": [norm_group(NormGroupID=f"page-{page}")],
        },
      )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    groups = lion.fetch_norm_groups(*WINDOW, client=client, delay_s=0)

    assert [g["NormGroupID"] for g in groups] == ["page-1", "page-2"]
    assert [b["Page"] for b in bodies] == [1, 2]
    assert all(b["TripTypes"] == config.LION_TRIP_TYPE_CRUISE for b in bodies)
    assert all(b["GoDatestart"] == "2026-09-16" for b in bodies)
    assert all(b["GoDateEnd"] == "2026-10-16" for b in bodies)

  def test_body_keeps_every_field_the_site_sends(self):
    # 實測只送幾個欄位時同樣的條件會回 0 筆，所以那堆 null 不能被「清理」掉
    assert set(lion.search_body(*WINDOW, 1)) == {
      "ArriveID", "GoDatestart", "GroupID", "Keywords", "IsEnsureGroup", "IsSold",
      "ThemeID", "TravelPavilionGroupID", "KeywordsCity", "TravelType", "BuIDs",
      "PreferAirlines", "GoDateEnd", "DepartureID", "WeekDay", "PriceList",
      "AirlineIDs", "TripTypes", "Tags", "SortType", "Days", "Page", "PageSize",
    }

  def test_pagination_stops_at_the_page_cap(self):
    # 對方 TotalPage 回錯值時不能無限翻下去
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
      nonlocal calls
      calls += 1
      return httpx.Response(
        200, json={"TotalPage": 999, "NormGroupList": [norm_group(NormGroupID=str(calls))]}
      )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    groups = lion.fetch_norm_groups(*WINDOW, client=client, delay_s=0)

    assert calls == lion.MAX_PAGES
    assert len(groups) == lion.MAX_PAGES

  def test_non_json_response_is_a_parse_error(self):
    # WAF 或維護頁會回 200 的 HTML；要說清楚是「看不懂回應」而不是 JSONDecodeError
    client = httpx.Client(
      transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>維護中</html>"))
    )
    with pytest.raises(ParseError, match="JSON"):
      lion.fetch_norm_groups(*WINDOW, client=client, delay_s=0)

  def test_http_error_propagates(self):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
      lion.fetch_norm_groups(*WINDOW, client=client, delay_s=0)
