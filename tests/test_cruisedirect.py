"""cruisedirect.com 擷取器測試。

該站以 Cloudflare 全面封鎖自動化存取（連 robots.txt 都回 403），
目前無法取得結果頁。本模組的職責因此是：
  1. 正確辨識出「被挑戰擋下」而不是誤判成「今天沒有 deal」
  2. 失敗訊息要具體可行動
  3. 一旦真的通過，把 HTML 留存下來，好據以補上解析器
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruise_deals.scrapers import cruisedirect
from cruise_deals.scrapers.base import BlockedError, ParseError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def challenge_html() -> str:
  # 用真實瀏覽器實際遇到的挑戰頁存下來的
  return (FIXTURES / "cruisedirect_challenge.html").read_text(encoding="utf-8")


class TestChallengeDetection:
  def test_detects_real_cloudflare_challenge_page(self, challenge_html):
    assert cruisedirect.is_blocked(challenge_html, "Just a moment...") is True

  def test_detects_by_title_alone(self):
    assert cruisedirect.is_blocked("<html></html>", "Just a moment...") is True

  def test_detects_attention_required_variant(self):
    assert cruisedirect.is_blocked("<html></html>", "Attention Required! | Cloudflare") is True

  def test_normal_page_is_not_flagged(self):
    html = (
      "<html><body><div class='cruise-card'>7 Night Caribbean</div></body></html>"
    )
    assert cruisedirect.is_blocked(html, "Last Minute Cruises | CruiseDirect") is False

  def test_empty_page_is_not_flagged_as_blocked(self):
    # 空頁面是別的問題（版面改版），不該誤報成被擋
    assert cruisedirect.is_blocked("", "CruiseDirect") is False


class TestParseSearchPage:
  def test_blocked_page_raises_blocked_error_not_parse_error(self, challenge_html):
    # 這個區別很重要：BlockedError 代表「進不去」，
    # ParseError 代表「進去了但看不懂」，兩者要分開回報
    with pytest.raises(BlockedError) as exc:
      cruisedirect.parse_search_page(challenge_html, title="Just a moment...")
    assert "Cloudflare" in str(exc.value)

  def test_blocked_error_is_a_parse_error(self):
    # 讓既有的優雅降級流程不必特別處理就能接住
    assert issubclass(BlockedError, ParseError)

  def test_unrecognised_page_raises_parse_error_with_guidance(self):
    html = "<html><body><h1>Last Minute Cruises</h1></body></html>"
    with pytest.raises(ParseError) as exc:
      cruisedirect.parse_search_page(html, title="Last Minute Cruises")
    # 訊息要指名是哪個選擇器不見了，修解析器的人才知道從哪查起。
    # 現場檔案的路徑由呼叫端 parse_or_save() 補上——這支純函式沒有做 I/O，
    # 不該宣稱自己存了檔（見 TestErrorMessagesDoNotLie）。
    assert "article.node--type-sailing" in str(exc.value)


@pytest.fixture(scope="module")
def tokyo_html() -> str:
  # 以 facet 篩選出發城市=Tokyo、日期=2026-08-13~09-12 後抓下來的真實頁面
  return (FIXTURES / "cruisedirect_tokyo.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def yokohama_html() -> str:
  return (FIXTURES / "cruisedirect_yokohama.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tokyo(tokyo_html):
  return cruisedirect.parse_search_page(tokyo_html, title="Last Minute Cruise Deals")


@pytest.fixture(scope="module")
def yokohama(yokohama_html):
  return cruisedirect.parse_search_page(yokohama_html, title="Last Minute Cruise Deals")


class TestParseTokyoPage:
  def test_finds_every_sailing(self, tokyo):
    # 一張 article 卡片可能含多個出發日期（各自一個 price-table）
    assert len(tokyo) == 3

  def test_sail_dates(self, tokyo):
    assert sorted(d.sail_date for d in tokyo) == [
      date(2026, 8, 18), date(2026, 8, 30), date(2026, 9, 11),
    ]

  def test_common_fields(self, tokyo):
    deal = min(tokyo, key=lambda d: d.sail_date)
    assert deal.source == "cruisedirect"
    assert deal.depart_port == "Tokyo"
    assert deal.ship_name == "Celebrity Millennium"
    assert deal.nights == 12
    assert deal.currency == "USD"

  def test_lowest_of_all_cabin_types(self, tokyo):
    by_date = {d.sail_date: d for d in tokyo}
    # Aug 18: Interior $2,772 / Balcony $3,001 -> 取 2772
    assert by_date[date(2026, 8, 18)].price == Decimal("2772")
    # Sep 11: Interior $1,991 / Balcony $2,712 / Suite $15,714 -> 取 1991
    assert by_date[date(2026, 9, 11)].price == Decimal("1991")

  def test_sailing_with_only_suite_available(self, tokyo):
    by_date = {d.sail_date: d for d in tokyo}
    # Aug 30 只有 Suite 有價
    assert by_date[date(2026, 8, 30)].price == Decimal("11343")

  def test_cruise_line_resolved_from_logo(self, tokyo):
    # logo 的 alt 只有 "celebrity"，要還原成完整名稱才對得上其他來源
    assert all(d.cruise_line == "Celebrity Cruises" for d in tokyo)

  def test_ports_of_call_split_on_dash_not_comma(self, tokyo):
    # 港名本身含逗號（"Tokyo, Japan"），用逗號切會切壞
    deal = min(tokyo, key=lambda d: d.sail_date)
    assert deal.ports_of_call[0] == "Tokyo, Japan"
    assert all(", " in p or len(p.split()) <= 3 for p in deal.ports_of_call)

  def test_detail_url_points_at_the_specific_sailing(self, tokyo):
    # 每個 price-table 的「Select」按鈕帶該航次專屬的 package id，
    # 比整張卡片共用的行程連結精確
    deal = min(tokyo, key=lambda d: d.sail_date)
    assert deal.detail_url.startswith("https://book.cruisedirect.com/swift/cruise/package/")

  def test_each_sailing_gets_its_own_url(self, tokyo):
    assert len({d.detail_url for d in tokyo}) == 3

  def test_scraped_at_is_timezone_aware(self, tokyo):
    assert all(d.scraped_at.tzinfo is not None for d in tokyo)


class TestParseYokohamaPage:
  def test_finds_every_sailing(self, yokohama):
    assert len(yokohama) == 3

  def test_fields(self, yokohama):
    assert {d.depart_port for d in yokohama} == {"Yokohama"}
    assert {d.ship_name for d in yokohama} == {"Diamond Princess"}
    assert {d.cruise_line for d in yokohama} == {"Princess Cruises"}
    assert {d.nights for d in yokohama} == {10}

  def test_prices(self, yokohama):
    by_date = {d.sail_date: d for d in yokohama}
    assert by_date[date(2026, 8, 26)].price == Decimal("1962")
    assert by_date[date(2026, 9, 5)].price == Decimal("2417")

  def test_sailing_with_no_available_cabins_has_no_price(self, yokohama):
    # Aug 16 四種房型都是 "-"
    by_date = {d.sail_date: d for d in yokohama}
    assert by_date[date(2026, 8, 16)].price is None


@pytest.fixture(scope="module")
def keelung_html() -> str:
  # 由 /search-results?f[0]=departure_city:743704 抓下來的真實頁面
  return (FIXTURES / "cruisedirect_keelung.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def keelung(keelung_html):
  return cruisedirect.parse_search_page(keelung_html, title="Cruise Search Results")


class TestParseKeelungPage:
  """基隆頁面的出發城市欄位是空的，必須用停靠港第一站補上。

  這是真實資料裡的陷阱：欄位存在但沒有內容，若不處理會安靜地漏掉整頁資料。
  """

  def test_finds_sailings_despite_empty_departure_city_field(self, keelung):
    assert len(keelung) == 3

  def test_departure_port_falls_back_to_first_port_of_call(self, keelung):
    assert {d.depart_port for d in keelung} == {"Keelung"}

  def test_ship_and_line(self, keelung):
    assert {d.ship_name for d in keelung} == {"Costa Serena"}
    assert {d.cruise_line for d in keelung} == {"Costa Cruises"}

  def test_nights_parsed(self, keelung):
    assert {d.nights for d in keelung} <= {3, 4}

  def test_ports_of_call_without_country_suffix(self, keelung):
    # 這一頁的港名沒有國名（"Keelung (taipei)" 而非 "Keelung, Taiwan"）
    deal = keelung[0]
    assert deal.ports_of_call[0] == "Keelung (taipei)"


class TestLoudFailureOnUnparseableArticles:
  """有卡片卻一筆都解析不出來 -> 版面改了，要大聲失敗而不是回空清單。"""

  def test_raises_when_articles_exist_but_none_parse(self):
    html = (
      "<html><body>"
      '<article class="node node--type-sailing">'
      '<div class="nothing-we-recognise">x</div>'
      "</article>"
      "</body></html>"
    )
    with pytest.raises(ParseError):
      cruisedirect.parse_search_page(html, title="Cruise Search Results")


class TestDateWindowFilter:
  def test_filters_out_sailings_beyond_window(self, tokyo_html):
    deals = cruisedirect.parse_search_page(
      tokyo_html, title="ok", start=date(2026, 8, 13), end=date(2026, 9, 1)
    )
    # Sep 11 應被排除
    assert sorted(d.sail_date for d in deals) == [date(2026, 8, 18), date(2026, 8, 30)]

  def test_no_window_returns_everything(self, tokyo_html):
    assert len(cruisedirect.parse_search_page(tokyo_html, title="ok")) == 3


class TestPriceCells:
  def test_dash_means_cabin_unavailable(self):
    assert cruisedirect.lowest_of_cabins(["-", "-", "-", "-"]) is None

  def test_picks_minimum(self):
    assert cruisedirect.lowest_of_cabins(
      ["$2,772 USD", "-", "$3,001 USD", "-"]
    ) == Decimal("2772")

  def test_ignores_non_price_text(self):
    assert cruisedirect.lowest_of_cabins(["Select", "-", "$500 USD"]) == Decimal("500")

  def test_empty_list(self):
    assert cruisedirect.lowest_of_cabins([]) is None


class TestSailDateParsing:
  def test_takes_departure_date_from_range(self):
    raw = "Aug 30, 2026 - Sep 11, 2026 Sun - Fri Bonus Details"
    assert cruisedirect.parse_sail_date_cell(raw) == date(2026, 8, 30)

  def test_handles_missing_return_date(self):
    assert cruisedirect.parse_sail_date_cell("Aug 30, 2026") == date(2026, 8, 30)

  def test_unparseable_returns_none(self):
    assert cruisedirect.parse_sail_date_cell("Bonus Details") is None


class TestBuildSearchUrl:
  def test_includes_departure_city_and_date_facets(self):
    url = cruisedirect.build_search_url(2604, date(2026, 8, 13), date(2026, 9, 12))
    assert "departure_city%3A2604" in url
    assert "departure_date" in url
    assert url.startswith("https://www.cruisedirect.com/search-results?")

  def test_date_range_uses_unix_timestamps(self):
    url = cruisedirect.build_search_url(2604, date(2026, 8, 13), date(2026, 9, 12))
    # 2026-08-13T00:00:00Z = 1786579200
    assert "1786579200" in url

  def test_covers_all_three_target_ports(self):
    assert set(cruisedirect.DEPARTURE_CITY_IDS) == {"Keelung", "Tokyo", "Yokohama"}

  def test_uses_general_search_endpoint_not_the_curated_list(self):
    """/cruises/last-minute-cruises 是策展子集合，其 facet 清單裡沒有基隆。
    改用 /search-results 這個完整搜尋端點，日期自己篩。"""
    url = cruisedirect.build_search_url(743704, date(2026, 8, 13), date(2026, 9, 12))
    assert "/search-results?" in url
    assert "last-minute-cruises" not in url


class TestProxySetting:
  """CI 的資料中心 IP 會被 Cloudflare 升級成人工勾選框。
  透過家用路由器的 SSH SOCKS5 通道，讓流量從住宅 IP 出去即可。"""

  def test_none_when_env_not_set(self, monkeypatch):
    monkeypatch.delenv("CRUISEDIRECT_PROXY", raising=False)
    assert cruisedirect.proxy_setting() is None

  def test_none_when_env_is_blank(self, monkeypatch):
    monkeypatch.setenv("CRUISEDIRECT_PROXY", "   ")
    assert cruisedirect.proxy_setting() is None

  def test_bare_host_port_defaults_to_socks5(self, monkeypatch):
    monkeypatch.setenv("CRUISEDIRECT_PROXY", "127.0.0.1:1080")
    assert cruisedirect.proxy_setting() == "socks5://127.0.0.1:1080"

  def test_socks5h_is_rewritten_to_socks5(self, monkeypatch):
    """socks5h 是 curl 的寫法，Chrome 不認得會**安靜地忽略整個代理設定**
    改走直連。實測就是這個原因讓通道看起來完全沒生效。
    Chrome 的 socks5 本來就會遠端解析 DNS，語意相同。"""
    monkeypatch.setenv("CRUISEDIRECT_PROXY", "socks5h://127.0.0.1:1080")
    assert cruisedirect.proxy_setting() == "socks5://127.0.0.1:1080"

  def test_http_scheme_preserved(self, monkeypatch):
    monkeypatch.setenv("CRUISEDIRECT_PROXY", "http://10.0.0.1:3128")
    assert cruisedirect.proxy_setting() == "http://10.0.0.1:3128"

  def test_strips_surrounding_whitespace(self, monkeypatch):
    monkeypatch.setenv("CRUISEDIRECT_PROXY", "  socks5://127.0.0.1:1080\n")
    assert cruisedirect.proxy_setting() == "socks5://127.0.0.1:1080"

  def test_rejects_obviously_invalid_value(self, monkeypatch):
    # 設錯值時寧可不用代理直連，也不要讓瀏覽器啟動失敗
    monkeypatch.setenv("CRUISEDIRECT_PROXY", "not a proxy")
    assert cruisedirect.proxy_setting() is None


class TestFailureIsGraceful:
  """整體流程不能因為這一站掛掉而中斷。"""

  def test_scrape_failure_becomes_a_failed_result_not_an_exception(self):
    from cruise_deals.scrapers.base import run_scraper

    def boom():
      raise BlockedError("Cloudflare 挑戰未通過")

    result = run_scraper("cruisedirect", boom)
    assert result.ok is False
    assert "Cloudflare" in (result.error or "")
    assert result.deals == []


class FakeSb:
  """假的 SeleniumBase，只需要有 save_screenshot。"""

  def __init__(self, screenshot_fails: bool = False):
    self.screenshot_fails = screenshot_fails
    self.screenshots: list[str] = []

  def save_screenshot(self, path: str) -> None:
    if self.screenshot_fails:
      raise RuntimeError("瀏覽器已經關掉了")
    self.screenshots.append(path)


class TestDebugArtifactOnParseFailure:
  """解析失敗時要把現場存下來。

  這是實際踩到的坑：原本只有「被 Cloudflare 擋下」那條路徑會存檔，
  但錯誤訊息卻宣稱 HTML 已存到 debug/。結果對方改版那天 CI 上什麼都沒留下，
  執行報告叫你去比對一個根本不存在的檔案。
  """

  @pytest.fixture(autouse=True)
  def debug_dir(self, tmp_path, monkeypatch):
    from cruise_deals import config

    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "debug")
    return tmp_path / "debug"

  def test_successful_parse_writes_nothing(self, debug_dir):
    html = (FIXTURES / "cruisedirect_keelung.html").read_text(encoding="utf-8")

    deals = cruisedirect.parse_or_save(FakeSb(), "Keelung", html, "CruiseDirect")

    assert deals
    assert not debug_dir.exists()

  def test_parse_failure_saves_the_page(self, debug_dir):
    html = "<html><body>對方改版了</body></html>"

    with pytest.raises(ParseError):
      cruisedirect.parse_or_save(FakeSb(), "Keelung", html, "CruiseDirect")

    saved = debug_dir / "cruisedirect_Keelung.html"
    assert saved.read_text(encoding="utf-8") == html

  def test_error_message_points_at_the_saved_file(self, debug_dir):
    with pytest.raises(ParseError) as excinfo:
      cruisedirect.parse_or_save(FakeSb(), "Tokyo", "<html></html>", "CruiseDirect")

    assert str(debug_dir / "cruisedirect_Tokyo.html") in str(excinfo.value)

  def test_screenshot_is_also_taken(self, debug_dir):
    sb = FakeSb()

    with pytest.raises(ParseError):
      cruisedirect.parse_or_save(sb, "Keelung", "<html></html>", "CruiseDirect")

    assert sb.screenshots == [str(debug_dir / "cruisedirect_Keelung.png")]

  def test_failed_screenshot_does_not_lose_the_html(self, debug_dir):
    # 截圖失敗（瀏覽器掛了）不該讓已經存下來的 HTML 白費
    with pytest.raises(ParseError) as excinfo:
      cruisedirect.parse_or_save(
        FakeSb(screenshot_fails=True), "Keelung", "<html></html>", "CruiseDirect"
      )

    assert (debug_dir / "cruisedirect_Keelung.html").exists()
    assert "現場已存到" in str(excinfo.value)

  def test_blocked_error_is_not_downgraded(self, debug_dir):
    # BlockedError 與 ParseError 的處置方式不同，重建例外時不能弄丟型別
    html = (FIXTURES / "cruisedirect_challenge.html").read_text(encoding="utf-8")

    with pytest.raises(BlockedError):
      cruisedirect.parse_or_save(FakeSb(), "Keelung", html, "Just a moment...")

  def test_unwritable_debug_dir_still_raises_the_original_error(
    self, monkeypatch, debug_dir
  ):
    from cruise_deals import config

    # 存不下來（例如唯讀檔案系統）也不該把原本的錯誤吃掉
    monkeypatch.setattr(config, "DEBUG_DIR", debug_dir / "nope\0invalid")

    with pytest.raises(ParseError) as excinfo:
      cruisedirect.parse_or_save(FakeSb(), "Keelung", "<html></html>", "CruiseDirect")

    assert "現場已存到" not in str(excinfo.value)


class TestErrorMessagesDoNotLie:
  """純函式沒有做 I/O，訊息就不該宣稱檔案已經存好了。"""

  def test_missing_articles_message_makes_no_file_claim(self):
    with pytest.raises(ParseError) as excinfo:
      cruisedirect.parse_search_page("<html><body>改版</body></html>", "CruiseDirect")

    assert "已存到" not in str(excinfo.value)

  def test_unparsable_cards_message_makes_no_file_claim(self):
    html = '<article class="node--type-sailing"></article>'

    with pytest.raises(ParseError) as excinfo:
      cruisedirect.parse_search_page(html, "CruiseDirect")

    assert "已存到" not in str(excinfo.value)


@pytest.fixture(scope="module")
def zero_results_html() -> str:
  """真實抓到的「0 Cruises」頁面。

  2026-08-23 起基隆在那個日期窗口內真的一班都沒有。這份是 CI 存下來的
  現場（run 32710731522），頁面本身完全正常，不是 Cloudflare 挑戰頁。
  """
  return (FIXTURES / "cruisedirect_zero_results.html").read_text(encoding="utf-8")


class TestMatchedCount:
  """頁面宣稱的筆數是「真的沒船」與「改版了」之間唯一的分界線。"""

  def test_reads_zero_from_an_empty_result_page(self, zero_results_html):
    assert cruisedirect.matched_count(zero_results_html) == 0

  def test_reads_the_count_from_a_page_with_results(self, keelung_html):
    assert cruisedirect.matched_count(keelung_html) == 3

  def test_claimed_count_matches_what_we_actually_parse(self, keelung_html):
    # 兩者對不上就代表解析漏了東西
    deals = cruisedirect.parse_search_page(keelung_html)
    assert len(deals) == cruisedirect.matched_count(keelung_html)

  def test_unknown_layout_yields_none(self):
    assert cruisedirect.matched_count("<html><body>改版了</body></html>") is None

  def test_challenge_page_has_no_count(self, challenge_html):
    assert cruisedirect.matched_count(challenge_html) is None


class TestZeroResultsIsNotAFailure:
  """某個港口當天沒航次，不該被誤判成對方改版。

  實際踩到（2026-08-23）：基隆掛零 -> 拋 ParseError -> 因為當時是直接
  往上拋，東京與橫濱連抓都沒抓，整個 cruisedirect 來源歸零。
  """

  def test_zero_results_page_parses_to_an_empty_list(self, zero_results_html):
    assert cruisedirect.parse_search_page(zero_results_html, title="ok") == []

  def test_zero_results_page_does_not_raise(self, zero_results_html):
    # 沒有 pytest.raises，這行本身就是斷言
    cruisedirect.parse_search_page(
      zero_results_html, title="ok", start=date(2026, 8, 13), end=date(2026, 9, 12)
    )

  def test_claiming_results_but_finding_no_cards_still_raises(self):
    # 宣稱有 24 筆卻一張卡片都沒有 -> 這才是改版
    html = '<html><body><h2 class="view-header">24 Cruises</h2></body></html>'

    with pytest.raises(ParseError, match="24"):
      cruisedirect.parse_search_page(html, title="ok")

  def test_unknown_count_with_no_cards_raises(self):
    # 連筆數都讀不到 -> 版面真的變了，要大聲失敗
    with pytest.raises(ParseError, match="未知"):
      cruisedirect.parse_search_page("<html><body></body></html>", title="ok")

  def test_zero_results_leaves_no_debug_artifact(self, zero_results_html, tmp_path,
                                                 monkeypatch):
    from cruise_deals import config

    monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "debug")
    cruisedirect.parse_or_save(FakeSb(), "Keelung", zero_results_html, "ok")

    assert not (tmp_path / "debug").exists()


class FakeBrowser:
  """假的 SeleniumBase，依網址裡的 city_id 回傳對應的頁面。"""

  def __init__(self, pages: dict[str, str], title: str = "Cruise Search Results"):
    self.pages = pages
    self.title = title
    self.url = ""
    self.screenshots: list[str] = []
    self.cdp = SimpleNamespace(click_captcha=lambda: None)

  def __enter__(self):
    return self

  def __exit__(self, *exc_info):
    return False

  def activate_cdp_mode(self, url: str) -> None:
    self.url = url

  def sleep(self, _seconds) -> None:
    pass

  def get_page_source(self) -> str:
    for city, city_id in cruisedirect.DEPARTURE_CITY_IDS.items():
      if str(city_id) in self.url:
        return self.pages[city]
    raise AssertionError(f"沒有為這個網址準備頁面：{self.url}")

  def get_title(self) -> str:
    return self.title

  def save_screenshot(self, path: str) -> None:
    self.screenshots.append(path)


@pytest.fixture
def fake_browser(monkeypatch, tmp_path):
  """把 seleniumbase.SB 換成假的，讓 scrape() 的整個迴圈跑得起來。"""
  import seleniumbase

  from cruise_deals import config

  monkeypatch.setattr(config, "DEBUG_DIR", tmp_path / "debug")
  made: list[FakeBrowser] = []

  def install(pages, title="Cruise Search Results"):
    browser = FakeBrowser(pages, title)
    made.append(browser)
    monkeypatch.setattr(seleniumbase, "SB", lambda **_kw: browser)
    return browser

  return install


class TestScrapeIsResilientPerCity:
  """一個城市出事不該把另外兩個城市一起拖下水。"""

  def _scrape(self):
    return cruisedirect.scrape(
      start=date(2026, 8, 13), lookahead_days=30, wait_s=0
    )

  def test_empty_city_does_not_block_the_others(
    self, fake_browser, zero_results_html, tokyo_html, yokohama_html
  ):
    fake_browser({
      "Keelung": zero_results_html,  # 這個港口當天沒航次
      "Tokyo": tokyo_html,
      "Yokohama": yokohama_html,
    })

    deals = self._scrape()

    assert {d.depart_port for d in deals} == {"Tokyo", "Yokohama"}

  def test_one_broken_city_does_not_block_the_others(
    self, fake_browser, tokyo_html, yokohama_html
  ):
    # 只有基隆那頁改版，另外兩個港口照樣要拿得到
    fake_browser({
      "Keelung": "<html><body>改版了</body></html>",
      "Tokyo": tokyo_html,
      "Yokohama": yokohama_html,
    })

    deals = self._scrape()

    assert {d.depart_port for d in deals} == {"Tokyo", "Yokohama"}

  def test_broken_city_still_leaves_a_debug_artifact(
    self, fake_browser, tmp_path, tokyo_html, yokohama_html
  ):
    fake_browser({
      "Keelung": "<html><body>改版了</body></html>",
      "Tokyo": tokyo_html,
      "Yokohama": yokohama_html,
    })

    self._scrape()

    assert (tmp_path / "debug" / "cruisedirect_Keelung.html").exists()

  def test_every_city_empty_is_a_success_with_no_deals(
    self, fake_browser, zero_results_html
  ):
    # 全部港口都沒航次是合法狀態，不該讓來源失敗
    fake_browser({c: zero_results_html for c in cruisedirect.DEPARTURE_CITY_IDS})

    assert self._scrape() == []

  def test_every_city_broken_fails_as_a_parse_error(self, fake_browser):
    fake_browser({c: "<html>改版</html>" for c in cruisedirect.DEPARTURE_CITY_IDS})

    with pytest.raises(ParseError) as excinfo:
      self._scrape()

    # 不能報成 BlockedError——那會讓人以為是被擋，跑去調整代理而不是修解析器
    assert not isinstance(excinfo.value, BlockedError)

  def test_every_city_blocked_still_fails_as_blocked(self, fake_browser, challenge_html):
    fake_browser(
      {c: challenge_html for c in cruisedirect.DEPARTURE_CITY_IDS},
      title="Just a moment...",
    )

    with pytest.raises(BlockedError):
      self._scrape()
