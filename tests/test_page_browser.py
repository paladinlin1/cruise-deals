"""在真實瀏覽器裡驗證產生的頁面：排序、篩選、搜尋、手機版面。

這些是 JS 行為，純字串比對的測試看不出來——實際上就是這支測試抓到了
「無報價的列沒有排到最後」的排序 bug。

沒安裝 patchright／chromium 時會自動跳過。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest
from factories import make_deal

from cruise_deals.fx import Rate
from cruise_deals.outputs import page, tabular
from cruise_deals.scrapers.base import ScrapeResult

sync_api = pytest.importorskip(
  "patchright.sync_api", reason="需要 patchright 才能跑瀏覽器測試"
)


RATE = Rate(usd_twd=Decimal("32"), as_of=date(2026, 8, 17), source="test")


def usd(amount: str) -> dict:
  """外國站報價：保留美元原價，另附換算後的台幣。"""
  return {
    "price": Decimal(amount),
    "currency": "USD",
    "price_twd": Decimal(amount) * RATE.usd_twd,
    "fx_rate": RATE.usd_twd,
  }


DEALS = [
  make_deal(
    sail_date=date(2026, 8, 16), depart_port="Yokohama",
    ship_name="Diamond Princess", cruise_line="Princess Cruises",
    nights=10, **usd("2812"),
  ),
  make_deal(
    sail_date=date(2026, 8, 16), depart_port="Keelung",
    ship_name="Costa Serena", cruise_line="Costa Cruises",
    nights=3, price=None,
  ),
  make_deal(
    sail_date=date(2026, 8, 18), depart_port="Tokyo",
    ship_name="Celebrity Millennium", cruise_line="Celebrity Cruises",
    nights=12, **usd("3001"),
  ),
  make_deal(
    sail_date=date(2026, 8, 26), depart_port="Yokohama",
    ship_name="Diamond Princess", cruise_line="Princess Cruises",
    nights=20, price=None,
  ),
  make_deal(
    sail_date=date(2026, 9, 5), depart_port="Yokohama",
    ship_name="Diamond Princess", cruise_line="Princess Cruises",
    nights=17, **usd("1742"),
  ),
  # 台幣原生報價：金額數字比美元那幾筆大得多，但實際上最便宜。
  # 排序若沒有換算就會把它擺在最貴的一端。
  make_deal(
    source="asiayo", sail_date=date(2026, 9, 13), depart_port="Keelung",
    ship_name="Star Voyager", ship_name_raw="探索星號", cruise_line="Star Cruises",
    nights=5, price=Decimal("18000"), currency="TWD", price_twd=Decimal("18000"),
  ),
]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
  """產生頁面並回傳 file:// 網址。"""
  report = tabular.build_report(
    [ScrapeResult(source="icruise", deals=list(DEALS), ok=True, duration_s=1.0)],
    fx=RATE,
  )
  path = tmp_path_factory.mktemp("docs") / "index.html"
  page.write(path, DEALS, report)
  return path.resolve().as_uri()


@pytest.fixture(scope="module")
def browser_page(rendered):
  with sync_api.sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    tab = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    tab.goto(rendered)
    tab.wait_for_selector("#deals")
    tab.errors = errors  # type: ignore[attr-defined]
    yield tab
    browser.close()


def visible_rows(tab) -> int:
  return tab.eval_on_selector_all(
    "#deals tbody tr", "rows => rows.filter(r => !r.hidden).length"
  )


def column(tab, index: int) -> list[str]:
  return tab.eval_on_selector_all(
    "#deals tbody tr",
    f"rows => rows.filter(r => !r.hidden).map(r => r.cells[{index}].textContent.trim())",
  )


def reset(tab) -> None:
  tab.fill("#q", "")
  tab.select_option("#port", "")
  tab.select_option("#line", "")


# 價格欄現在是 "NT$89,984"（台幣為主），無報價則是「洽詢報價」
_PRICE_RE = re.compile(r"^NT\$([\d,]+(?:\.\d+)?)")


def price_of(cell: str) -> float | None:
  """把價格欄的文字轉成數字；無報價回 None。"""
  match = _PRICE_RE.match(cell)
  return float(match.group(1).replace(",", "")) if match else None


def prices(tab) -> list[float | None]:
  return [price_of(c) for c in column(tab, 6)]


def sort_by(tab, nth: int, ascending: bool = True) -> None:
  """把某一欄排成指定方向。

  整個模組共用同一個瀏覽器分頁，排序狀態會跨測試留著，
  所以不能假設「點一下就是升冪」——點到方向對了為止。
  """
  selector = f"#deals thead th:nth-child({nth})"
  want = "asc" if ascending else "desc"
  for _ in range(3):
    if tab.get_attribute(selector, "data-dir") == want:
      return
    tab.click(selector)
  raise AssertionError(f"第 {nth} 欄排不成 {want}")


class TestInitialRender:
  def test_shows_every_deal(self, browser_page):
    reset(browser_page)
    assert visible_rows(browser_page) == len(DEALS)

  def test_counter_matches_visible_rows(self, browser_page):
    reset(browser_page)
    assert browser_page.inner_text("#shown") == str(len(DEALS))

  def test_no_javascript_errors(self, browser_page):
    assert browser_page.errors == []


class TestSorting:
  def test_price_column_sorts_ascending(self, browser_page):
    reset(browser_page)
    sort_by(browser_page, 7)
    priced = [p for p in prices(browser_page) if p is not None]
    assert priced == sorted(priced)

  def test_twd_and_usd_rows_sort_together(self, browser_page):
    """跨幣別排序：台幣那筆的數字最大卻最便宜，必須排在最前面。

    這正是「直接比 price 就會錯」的情境——不換算的話 18,000 會被
    當成比 1,742 貴，排到最後去。
    """
    reset(browser_page)
    sort_by(browser_page, 7)
    priced = [p for p in prices(browser_page) if p is not None]
    assert priced[0] == 18000  # asiayo 的台幣原生報價
    assert priced == sorted(priced)

  def test_deals_without_price_sort_last(self, browser_page):
    """「洽詢報價」不能因為沒有數值就排到最前面。"""
    reset(browser_page)
    sort_by(browser_page, 7)
    cells = column(browser_page, 6)
    priced_count = sum(1 for c in cells if price_of(c) is not None)
    assert all("洽詢報價" in c for c in cells[priced_count:])

  def test_deals_without_price_stay_last_when_descending(self, browser_page):
    """降冪時「洽詢報價」一樣要在最後，不能翻到最前面。"""
    reset(browser_page)
    sort_by(browser_page, 7, ascending=False)
    cells = column(browser_page, 6)
    priced = [price_of(c) for c in cells if price_of(c) is not None]
    assert priced == sorted(priced, reverse=True)
    assert all("洽詢報價" in c for c in cells[len(priced):])

  def test_nights_column_sorts_numerically(self, browser_page):
    reset(browser_page)
    sort_by(browser_page, 6)
    nights = [int(n) for n in column(browser_page, 5)]
    assert nights == sorted(nights)  # 字串排序會把 3 排在 20 後面

  def test_date_column_sorts_chronologically(self, browser_page):
    reset(browser_page)
    sort_by(browser_page, 1)
    dates = column(browser_page, 0)
    assert dates == sorted(dates)


class TestFiltering:
  def test_filter_by_departure_port(self, browser_page):
    reset(browser_page)
    browser_page.select_option("#port", "Keelung")
    assert set(column(browser_page, 1)) == {"Keelung"}

  def test_filter_by_cruise_line(self, browser_page):
    reset(browser_page)
    browser_page.select_option("#line", "Princess Cruises")
    assert set(column(browser_page, 4)) == {"Princess Cruises"}

  def test_clearing_filter_restores_all_rows(self, browser_page):
    reset(browser_page)
    browser_page.select_option("#port", "Keelung")
    reset(browser_page)
    assert visible_rows(browser_page) == len(DEALS)

  def test_counter_tracks_filtered_rows(self, browser_page):
    reset(browser_page)
    browser_page.select_option("#port", "Yokohama")
    assert browser_page.inner_text("#shown") == str(visible_rows(browser_page))


class TestSearch:
  def test_matches_ship_name_case_insensitively(self, browser_page):
    reset(browser_page)
    browser_page.fill("#q", "diamond")
    assert set(column(browser_page, 3)) == {"Diamond Princess"}

  def test_matches_port_of_call(self, browser_page):
    reset(browser_page)
    browser_page.fill("#q", "ishigaki")  # 只出現在停靠港，不在表格欄位裡
    assert visible_rows(browser_page) > 0

  def test_no_match_shows_empty_message(self, browser_page):
    reset(browser_page)
    browser_page.fill("#q", "zzzznotexist")
    assert visible_rows(browser_page) == 0
    assert browser_page.is_visible("#empty")

  def test_empty_message_hidden_when_results_exist(self, browser_page):
    reset(browser_page)
    assert not browser_page.is_visible("#empty")


class TestResponsive:
  def test_page_does_not_scroll_horizontally_on_mobile(self, browser_page):
    browser_page.set_viewport_size({"width": 390, "height": 844})
    try:
      assert browser_page.evaluate(
        "() => document.documentElement.scrollWidth"
        " <= document.documentElement.clientWidth"
      )
    finally:
      browser_page.set_viewport_size({"width": 1280, "height": 900})

  def test_wide_table_scrolls_inside_its_own_container(self, browser_page):
    browser_page.set_viewport_size({"width": 390, "height": 844})
    try:
      assert browser_page.evaluate(
        "() => { const w = document.querySelector('.tablewrap');"
        " return w.scrollWidth > w.clientWidth; }"
      )
    finally:
      browser_page.set_viewport_size({"width": 1280, "height": 900})
