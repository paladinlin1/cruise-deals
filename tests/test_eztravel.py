"""易遊網 vacation.eztravel.com.tw 擷取器測試。

跑在真實存下來的 `__NEXT_DATA__` 上（2026-09-17 抓的），不需要網路也不需要瀏覽器：
  eztravel_results_kee_332.json   基隆港／沖繩航線、一個月窗口的列表（7 個商品，2 個關團）
  eztravel_intro_navigator.json   探索星號 9/23 商品頁（內側／海景／露台 × 雙人／3 人／4 人房）
  eztravel_intro_fuji.json        三井富士號 10/11 商品頁（只有陽台套房雙人房、沒有行程表）
  eztravel_incapsula.html         被 Incapsula 擋下時回的 212 bytes 挑戰頁

這一站的重點是三個判斷：
  1. 列表的 minPrice1 是「所有艙等×佔床人數」的最低價（3／4 人房），
     要進商品頁取雙人房成人價才能跟其他來源的 2 人一室比
  2. 關團（fullStatus=END）的出發日不收，與雄獅的額滿同一原則
  3. 拿不到 __NEXT_DATA__ 要分清楚是被擋（BlockedError）還是改版（ParseError）
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cruise_deals import config
from cruise_deals.scrapers import eztravel
from cruise_deals.scrapers.base import BlockedError, ParseError

FIXTURES = Path(__file__).parent / "fixtures"

# fixture 抓取當下查的窗口
WINDOW = (date(2026, 9, 17), date(2026, 10, 17))


def load_state(name: str) -> dict:
  data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
  return data["props"]["pageProps"]["initialState"]


def wrap_html(next_data: dict) -> str:
  """把 __NEXT_DATA__ 包回頁面的樣子。

  正常頁面**也會**嵌一支 `/_Incapsula_Resource?…` 監控腳本（實測 2026-09-17），
  所以這裡照樣放進去——「有 Incapsula 腳本」不等於被擋。
  """
  return (
    '<html><body><script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(next_data, ensure_ascii=False)
    + '</script><script type="text/javascript" '
    'src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3&amp;ns=4&amp;cb=12">'
    "</script></body></html>"
  )


@pytest.fixture(scope="module")
def results() -> list[dict]:
  return eztravel.extract_results(load_state("eztravel_results_kee_332.json"))


@pytest.fixture(scope="module")
def navigator_intro() -> dict:
  return eztravel.extract_intro(load_state("eztravel_intro_navigator.json"))


@pytest.fixture(scope="module")
def fuji_intro() -> dict:
  return eztravel.extract_intro(load_state("eztravel_intro_fuji.json"))


def product(**overrides) -> dict:
  """建一個最小的列表商品，只覆寫關心的欄位。"""
  base = {
    "prodNm": "【麗星郵輪探索星號】～日本沖繩自由行 3 天 (週三出發)",
    "tourCitysNm": ["沖繩(那霸)"],
    "travelDay": 3,
    "minPrice1": 6325,
    "pfProdNo": "FRN0000020376",
    "departArea": "基隆港出發",
    "otherSaleDts": [
      {
        "saleDt": "20260923",
        "prodUrl": "https://vacation.eztravel.com.tw/pkgfrn/introduction/FRN0000020376/20260923",
        "tripStatus": "",
        "fullStatus": "NONE",
      }
    ],
  }
  base.update(overrides)
  return base


class TestPageShape:
  def test_incapsula_challenge_is_blocked_error(self):
    html = (FIXTURES / "eztravel_incapsula.html").read_text(encoding="utf-8")
    with pytest.raises(BlockedError, match="Incapsula"):
      eztravel.extract_state(html)

  def test_page_without_next_data_is_parse_error(self):
    with pytest.raises(ParseError, match="__NEXT_DATA__"):
      eztravel.extract_state("<html><body>找不到這個網頁</body></html>")

  def test_real_page_with_the_incapsula_script_is_not_blocked(self):
    next_data = json.loads(
      (FIXTURES / "eztravel_results_kee_332.json").read_text(encoding="utf-8")
    )
    html = wrap_html(next_data)
    assert "_Incapsula_Resource" in html  # 正常頁面本來就有這支腳本

    state = eztravel.extract_state(html)

    assert state["search"]["searchStatus"] == "SUCCESS"

  def test_state_without_search_results_is_parse_error(self):
    with pytest.raises(ParseError, match="searchResults"):
      eztravel.extract_results({"search": {"searchStatus": "SUCCESS"}})

  def test_failed_search_status_is_parse_error(self):
    with pytest.raises(ParseError, match="FAIL"):
      eztravel.extract_results({"search": {"searchStatus": "FAIL", "searchResults": []}})

  def test_truncated_list_is_parse_error(self):
    # pageConfig.total 比實際回的多代表被分頁截斷——安靜漏航次違反「改版要大聲」
    state = {
      "search": {
        "searchStatus": "SUCCESS",
        "searchInfo": {"pageConfig": {"pageSize": 10, "total": 19}},
        "searchResults": [product()] * 12,
      }
    }
    with pytest.raises(ParseError, match="19"):
      eztravel.extract_results(state)

  def test_null_page_props_is_parse_error(self):
    html = wrap_html({"props": {"pageProps": None}})
    with pytest.raises(ParseError):
      eztravel.extract_results(eztravel.extract_state(html))

  def test_intro_page_without_product_yields_none(self):
    # 商品頁 404 時 __NEXT_DATA__ 還在，只是 introduction 是空的
    assert eztravel.extract_intro({"introduction": {"server": None}}) is None
    assert eztravel.extract_intro({}) is None


class TestSailingsInWindow:
  def test_closed_sailings_are_excluded(self, results):
    closed = {
      (r["pfProdNo"], d["saleDt"])
      for r in results
      for d in r["otherSaleDts"]
      if d["fullStatus"] == "END"
    }
    assert len(closed) == 2  # fixture 裡 10/11 與 10/16 關團

    kept = {
      (s.product["pfProdNo"], s.sail_date)
      for s in eztravel.sailings_in_window(results, *WINDOW)
    }

    assert not any((p, date.fromisoformat(f"{d[:4]}-{d[4:6]}-{d[6:]}")) in kept for p, d in closed)
    # 7 個商品 − 2 個關團 − 1 個「蘇澳出發，基隆返回」（不是基隆登船）
    assert len(kept) == 4

  def test_only_keelung_departures_are_kept(self):
    taoyuan = product(departArea="桃園機場出發")  # 機票＋郵輪套裝
    assert eztravel.sailings_in_window([taoyuan], *WINDOW) == []

  def test_boarding_elsewhere_and_returning_to_keelung_is_not_a_keelung_departure(self):
    # 該站把「蘇澳出發，基隆返回」也歸在「基隆港出發」分類下，要看標題才分得出來
    suao = product(
      prodNm="【三井海洋郵輪富士號船票】蘇澳・石垣・那霸・基隆 7天(蘇澳出發，基隆返回)"
    )
    assert eztravel.sailings_in_window([suao], *WINDOW) == []

  def test_boarding_at_keelung_and_returning_elsewhere_is_kept(self):
    keelung = product(
      prodNm="【三井海洋郵輪富士號船票】基隆・那霸・蘇澳5天 (基隆出發，蘇澳返回)"
    )
    assert len(eztravel.sailings_in_window([keelung], *WINDOW)) == 1

  def test_sailings_outside_the_window_are_dropped(self):
    assert eztravel.sailings_in_window([product()], date(2026, 11, 1), date(2026, 11, 30)) == []

  def test_each_sale_date_becomes_its_own_sailing(self):
    two_dates = product(
      otherSaleDts=[
        {"saleDt": "20260923", "prodUrl": "https://x/FRN0000020376/20260923", "fullStatus": "NONE"},
        {"saleDt": "20261007", "prodUrl": "https://x/FRN0000020376/20261007", "fullStatus": "NONE"},
      ]
    )
    sailings = eztravel.sailings_in_window([two_dates], *WINDOW)
    assert [(s.sail_date, s.detail_url) for s in sailings] == [
      (date(2026, 9, 23), "https://x/FRN0000020376/20260923"),
      (date(2026, 10, 7), "https://x/FRN0000020376/20261007"),
    ]

  def test_products_without_a_usable_day_count_are_skipped(self):
    assert eztravel.sailings_in_window([product(travelDay=None)], *WINDOW) == []
    assert eztravel.sailings_in_window([product(travelDay=1)], *WINDOW) == []

  def test_unparseable_date_skips_that_sailing_only(self):
    broken = product(
      otherSaleDts=[
        {"saleDt": "not-a-date", "prodUrl": "https://x/a", "fullStatus": "NONE"},
        {"saleDt": "20260923", "prodUrl": "https://x/b", "fullStatus": "NONE"},
      ]
    )
    assert [s.detail_url for s in eztravel.sailings_in_window([broken], *WINDOW)] == ["https://x/b"]


class TestDoubleOccupancyPrice:
  def test_takes_the_cheapest_cabin_for_two_adults(self, navigator_intro):
    # 內側 8000 / 海景 10000 / 露台 12000 的雙人房成人價 → 8000；
    # 3 人房 6883、4 人房 6325 雖然更便宜但不是 2 人一室
    assert eztravel.double_occupancy_price(navigator_intro) == Decimal("8000")

  def test_single_cabin_type(self, fuji_intro):
    assert eztravel.double_occupancy_price(fuji_intro) == Decimal("62000")

  def test_no_double_room_row_yields_none(self):
    only_triples = {
      "pfProPrice4Introductions": [
        {"htlNum": "3", "cond2Type": "1", "price": 6883},
        {"htlNum": "2", "cond2Type": "3", "price": 8000},  # 雙人房但是孩童
      ]
    }
    assert eztravel.double_occupancy_price(only_triples) is None

  def test_zero_price_is_not_a_quote(self):
    assert eztravel.double_occupancy_price(
      {"pfProPrice4Introductions": [{"htlNum": "2", "cond2Type": "1", "price": 0}]}
    ) is None


class TestRoute:
  """商品頁的 routeInfo.routes 才是該航次真正的航線；列表的 tourCitysNm 是商品群組的標籤。"""

  def test_round_trip_route(self, navigator_intro):
    assert eztravel.route_ports(navigator_intro) == ("基隆", "那霸", "基隆")

  def test_sea_days_are_dropped_and_notes_stripped(self, fuji_intro):
    # 基隆 → 海上巡航 → 那霸 (沖繩) → 海上巡航 → 蘇澳
    assert eztravel.route_ports(fuji_intro) == ("基隆", "那霸", "蘇澳")

  def test_no_route_info_yields_nothing(self):
    assert eztravel.route_ports({}) == ()
    assert eztravel.route_ports(None) == ()

  def test_arrival_port_is_the_last_stop(self, navigator_intro, fuji_intro):
    assert eztravel.arrive_port(navigator_intro, "", "Keelung") == "Keelung"
    assert eztravel.arrive_port(fuji_intro, "", "Keelung") == "蘇澳"

  def test_one_way_trip_in_the_title_when_there_is_no_route_info(self):
    name = "【三井海洋郵輪富士號船票】2026年台灣國慶之旅 - 基隆・那霸・蘇澳5天 (基隆出發，蘇澳返回)"
    assert eztravel.arrive_port(None, name, "Keelung") == "蘇澳"

  def test_falls_back_to_the_departure_port(self):
    assert eztravel.arrive_port(None, "MSC地中海郵輪．榮耀號 5 天 4 晚", "Keelung") == "Keelung"


class TestBuildDeal:
  def test_price_comes_from_the_intro_page(self, navigator_intro):
    (sailing,) = eztravel.sailings_in_window([product()], *WINDOW)
    deal = eztravel.build_deal(sailing, navigator_intro, None)
    assert deal.price == Decimal("8000")
    assert deal.currency == "TWD"
    assert deal.price_note == "雙人房成人（每人）"

  def test_falls_back_to_the_list_price_without_an_intro_page(self):
    (sailing,) = eztravel.sailings_in_window([product()], *WINDOW)
    deal = eztravel.build_deal(sailing, None, None)
    assert deal.price == Decimal("6325")
    assert "列表最低價" in deal.price_note

  def test_travel_days_are_converted_to_nights(self, navigator_intro):
    (sailing,) = eztravel.sailings_in_window([product(travelDay=3)], *WINDOW)
    assert eztravel.build_deal(sailing, navigator_intro, None).nights == 2

  def test_star_navigator_is_mapped(self, navigator_intro):
    (sailing,) = eztravel.sailings_in_window([product()], *WINDOW)
    deal = eztravel.build_deal(sailing, navigator_intro, None)
    assert (deal.ship_name, deal.ship_name_raw, deal.cruise_line) == (
      "Star Voyager",
      "探索星號",
      "Star Cruises",
    )

  def test_mitsui_fuji_is_mapped_despite_the_promo_bracket(self, fuji_intro):
    name = (
      "【三大好禮全含｜小費・Wi-Fi・船上消費金】【三井海洋郵輪富士號船票】"
      "2026年台灣國慶之旅 - 基隆・那霸・蘇澳5天 (基隆出發，蘇澳返回)"
    )
    (sailing,) = eztravel.sailings_in_window([product(prodNm=name, travelDay=5)], *WINDOW)
    deal = eztravel.build_deal(sailing, fuji_intro, None)
    assert (deal.ship_name, deal.ship_name_raw, deal.cruise_line) == (
      "Mitsui Ocean Fuji",
      "富士號",
      "Mitsui Ocean Cruises",
    )
    assert deal.arrive_port == "蘇澳"

  def test_msc_bellissima_line_is_mapped_from_the_full_chinese_name(self):
    name = "MSC地中海郵輪．榮耀號 5 天 4 晚．基隆（台灣） - 宮古島 - 那霸（沖繩市） - 基隆（台灣）"
    (sailing,) = eztravel.sailings_in_window([product(prodNm=name, travelDay=5)], *WINDOW)
    deal = eztravel.build_deal(sailing, None, None)
    assert (deal.ship_name, deal.ship_name_raw, deal.cruise_line) == (
      "MSC Bellissima",
      "榮耀號",
      "MSC Cruises",
    )

  def test_ports_come_from_the_intro_route_not_the_list_tags(self, fuji_intro):
    # 列表把這個商品標成「與那國島、石垣島、沖繩」，但這班其實只停那霸
    (sailing,) = eztravel.sailings_in_window(
      [product(tourCitysNm=["與那國島", "石垣島", "沖繩(那霸)"], travelDay=5)], *WINDOW
    )
    deal = eztravel.build_deal(sailing, fuji_intro, None)
    assert deal.ports_of_call == ("那霸",)
    assert deal.arrive_port == "蘇澳"

  def test_ports_fall_back_to_the_list_tags_without_an_intro_page(self):
    (sailing,) = eztravel.sailings_in_window(
      [product(tourCitysNm=["石垣島", "宮古島", "沖繩(那霸)"])], *WINDOW
    )
    deal = eztravel.build_deal(sailing, None, None)
    assert deal.ports_of_call == ("石垣島", "宮古島", "沖繩(那霸)")

  def test_promo_bracket_before_an_unmapped_ship_does_not_become_the_ship_name(self):
    name = "【三大好禮全含｜小費・Wi-Fi・船上消費金】【挪威郵輪暢悅號船票】2026年沖繩之旅 5天"
    (sailing,) = eztravel.sailings_in_window([product(prodNm=name, travelDay=5)], *WINDOW)
    deal = eztravel.build_deal(sailing, None, None)
    assert (deal.ship_name, deal.ship_name_raw, deal.cruise_line) == (
      "暢悅號",
      "暢悅號",
      "Norwegian Cruise Line",
    )

  def test_source_and_urls_are_filled_in(self):
    (sailing,) = eztravel.sailings_in_window([product()], *WINDOW)
    deal = eztravel.build_deal(sailing, None, None)
    assert deal.source == "eztravel"
    assert deal.sail_date == date(2026, 9, 23)
    assert deal.depart_port == "Keelung"
    assert deal.depart_port_raw == "基隆港出發"
    assert deal.detail_url.endswith("/FRN0000020376/20260923")


class TestCollect:
  """用假的 fetch 函式驗證整個流程：查所有航線 → 每個出發日進商品頁 → 商品頁失敗退回列表價。"""

  def test_queries_every_route_code_and_visits_each_sailing(self, results):
    state = load_state("eztravel_results_kee_332.json")
    intro_state = load_state("eztravel_intro_navigator.json")
    empty = {"search": {"searchStatus": "SUCCESS", "searchResults": []}}
    fetched: list[str] = []

    def fetch(url: str) -> dict:
      fetched.append(url)
      if "/introduction/" in url:
        return intro_state
      return state if "/KEE/332?" in url else empty

    deals = eztravel.collect(fetch, *WINDOW)

    list_urls = [u for u in fetched if "/results/" in u]
    assert len(list_urls) == len(config.EZTRAVEL_ROUTE_CODES)
    assert all("depDateFrom=20260917&depDateTo=20261017" in u for u in list_urls)
    assert sum("/introduction/" in u for u in fetched) == 4  # 4 個基隆登船、可報名的出發日
    assert len(deals) == 4
    assert all(d.price_note == "雙人房成人（每人）" for d in deals)

  def test_intro_failure_falls_back_to_the_list_price(self, results):
    state = load_state("eztravel_results_kee_332.json")
    empty = {"search": {"searchStatus": "SUCCESS", "searchResults": []}}

    def fetch(url: str) -> dict:
      if "/introduction/" in url:
        raise RuntimeError("商品頁掛了")
      return state if "/KEE/332?" in url else empty

    deals = eztravel.collect(fetch, *WINDOW)

    assert len(deals) == 4
    assert all("列表最低價" in d.price_note for d in deals)

  def test_blocked_intro_page_aborts_the_source(self, results):
    # 中途被 Incapsula 擋代表 cookie 已失效；退回列表價會讓整站報價系統性偏低卻回報成功
    state = load_state("eztravel_results_kee_332.json")
    empty = {"search": {"searchStatus": "SUCCESS", "searchResults": []}}

    def fetch(url: str) -> dict:
      if "/introduction/" in url:
        raise BlockedError("挑戰頁")
      return state if "/KEE/332?" in url else empty

    with pytest.raises(BlockedError):
      eztravel.collect(fetch, *WINDOW)

  def test_same_product_under_two_routes_visits_the_intro_page_once(self):
    state = {"search": {"searchStatus": "SUCCESS", "searchResults": [product()]}}
    intro_urls: list[str] = []

    def fetch(url: str) -> dict:
      if "/introduction/" in url:
        intro_urls.append(url)
        return {}
      return state  # 每條航線都回同一個商品

    deals = eztravel.collect(fetch, *WINDOW)

    assert len(intro_urls) == 1
    assert len(deals) == 1

  def test_sailings_without_a_prod_url_are_not_collapsed_into_each_other(self):
    a = product(pfProdNo="A", otherSaleDts=[{"saleDt": "20260923", "fullStatus": "NONE"}])
    b = product(pfProdNo="B", otherSaleDts=[{"saleDt": "20261001", "fullStatus": "NONE"}])
    state = {"search": {"searchStatus": "SUCCESS", "searchResults": [a, b]}}

    deals = eztravel.collect(lambda url: state if "/results/" in url else {}, *WINDOW)

    assert sorted(d.sail_date for d in deals) == [date(2026, 9, 23), date(2026, 10, 1)]

  def test_same_sailing_from_two_products_keeps_the_cheaper(self):
    # 「週三出發」「週日出發」是不同商品編號，但同一天同一艘船就是同一航次
    sale = {"saleDt": "20260923", "fullStatus": "NONE"}
    a = product(pfProdNo="A", minPrice1=9000, otherSaleDts=[{**sale, "prodUrl": "https://x/A"}])
    b = product(pfProdNo="B", minPrice1=7000, otherSaleDts=[{**sale, "prodUrl": "https://x/B"}])
    state = {"search": {"searchStatus": "SUCCESS", "searchResults": [a, b]}}

    deals = eztravel.collect(lambda url: state if "/results/" in url else None, *WINDOW)

    assert [d.price for d in deals] == [Decimal("7000")]


class TestWithinSourceDedup:
  def test_uses_the_shared_helper(self):
    from factories import make_deal

    from cruise_deals.scrapers.base import keep_cheapest

    priced = make_deal(source="eztravel", price=Decimal("8000"))
    cheaper = make_deal(source="eztravel", price=Decimal("7000"))
    unpriced = make_deal(source="eztravel", price=None)

    assert [d.price for d in keep_cheapest([priced, cheaper, unpriced])] == [Decimal("7000")]
    assert keep_cheapest([unpriced, priced])[0].price == Decimal("8000")


class TestWaitForNextData:
  class FakePage:
    """挑戰頁自我重載時 page.content() 會拋例外，之後才拿得到真正的頁面。"""

    def __init__(self, contents):
      self.contents = list(contents)

    def content(self):
      item = self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]
      if isinstance(item, Exception):
        raise item
      return item

    def wait_for_timeout(self, ms):
      pass

  def test_keeps_waiting_through_a_navigation_error(self):
    page = self.FakePage([
      "<html>challenge</html>",
      RuntimeError("Page.content: Unable to retrieve content because the page is navigating"),
      '<html><script id="__NEXT_DATA__">{}</script></html>',
    ])
    assert "__NEXT_DATA__" in eztravel._wait_for_next_data(page, timeout_s=5)

  def test_returns_the_last_html_on_timeout(self):
    page = self.FakePage(["<html>challenge</html>"])
    assert eztravel._wait_for_next_data(page, timeout_s=0) == "<html>challenge</html>"


class TestUrls:
  def test_results_url_carries_route_window_and_page_size(self):
    # 預設一頁只回 12 筆；實測 pageSize 參數有效，一次要完
    assert eztravel.results_url(332, *WINDOW) == (
      "https://vacation.eztravel.com.tw/pkgfrn/results/KEE/332"
      f"?depDateFrom=20260917&depDateTo=20261017&pageSize={config.EZTRAVEL_PAGE_SIZE}"
    )

  def test_detail_url_is_rebuilt_when_the_list_omits_it(self):
    (sailing,) = eztravel.sailings_in_window(
      [product(otherSaleDts=[{"saleDt": "20260923", "fullStatus": "NONE"}])], *WINDOW
    )
    assert sailing.detail_url == (
      "https://vacation.eztravel.com.tw/pkgfrn/introduction/FRN0000020376/20260923"
    )
