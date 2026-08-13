"""cruisedirect.com 擷取器測試。

該站以 Cloudflare 全面封鎖自動化存取（連 robots.txt 都回 403），
目前無法取得結果頁。本模組的職責因此是：
  1. 正確辨識出「被挑戰擋下」而不是誤判成「今天沒有 deal」
  2. 失敗訊息要具體可行動
  3. 一旦真的通過，把 HTML 留存下來，好據以補上解析器
"""

from __future__ import annotations

from pathlib import Path

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
    # 訊息要告訴維護者下一步怎麼做
    assert "debug" in str(exc.value).lower()


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
