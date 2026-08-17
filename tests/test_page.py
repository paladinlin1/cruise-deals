"""GitHub Pages 表格網頁產生器的測試。"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

from factories import make_deal

from cruise_deals.fx import Rate
from cruise_deals.outputs import page, tabular
from cruise_deals.scrapers.base import ScrapeResult


def report_with(*results: ScrapeResult) -> tabular.RunReport:
  return tabular.build_report(list(results))


def ok(source: str, count: int = 1) -> ScrapeResult:
  return ScrapeResult(source=source, deals=[make_deal()] * count, ok=True)


def failed(source: str, error: str) -> ScrapeResult:
  return ScrapeResult(source=source, deals=[], ok=False, error=error)


class TestContent:
  def test_renders_a_row_per_deal(self):
    deals = [
      make_deal(ship_name="Costa Serena"),
      make_deal(ship_name="Diamond Princess", sail_date=date(2026, 9, 5)),
    ]
    html = page.render(deals, report_with(ok("icruise")))

    assert html.count("<tr") == len(deals) + 1  # 資料列 + 表頭

  def test_shows_every_required_column(self):
    html = page.render([make_deal()], report_with(ok("icruise")))
    for header in ("出發日期", "出發港口", "目的港口", "郵輪", "船公司", "航行天數", "最低價格"):
      assert header in html

  def test_shows_deal_values(self):
    deal = make_deal(
      ship_name="Diamond Princess",
      cruise_line="Princess Cruises",
      depart_port="Yokohama",
      nights=10,
      price=Decimal("1742"),
    )
    html = page.render([deal], report_with(ok("icruise")))

    assert "Diamond Princess" in html
    assert "Princess Cruises" in html
    assert "Yokohama" in html
    assert "1,742" in html  # 價格加上千分位比較好讀

  def test_shows_pricing_on_request_for_missing_price(self):
    html = page.render([make_deal(price=None)], report_with(ok("icruise")))
    assert "洽詢報價" in html

  def test_links_to_detail_page(self):
    deal = make_deal(detail_url="https://www.icruise.com/itineraries/abc.html")
    html = page.render([deal], report_with(ok("icruise")))
    assert 'href="https://www.icruise.com/itineraries/abc.html"' in html

  def test_empty_deal_list_renders_a_friendly_message(self):
    html = page.render([], report_with(ok("icruise", 0)))
    assert "沒有" in html or "查無" in html


class TestEscaping:
  def test_escapes_html_in_text_fields(self):
    deal = make_deal(ship_name='<script>alert("x")</script>')
    html = page.render([deal], report_with(ok("icruise")))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html

  def test_escapes_ampersand_in_ship_name(self):
    # 真實資料裡確實有 "Includes taxes & fees" 這類字串
    deal = make_deal(price_note="Includes taxes & fees")
    html = page.render([deal], report_with(ok("icruise")))
    assert "taxes &amp; fees" in html

  def test_escapes_quotes_in_urls(self):
    deal = make_deal(detail_url='https://x.com/a"onmouseover="evil()')
    html = page.render([deal], report_with(ok("icruise")))
    assert 'onmouseover="evil()' not in html


class TestSourceStatusBanner:
  def test_failed_source_is_reported(self):
    html = page.render(
      [make_deal()], report_with(ok("icruise"), failed("cruisedirect", "CF 挑戰逾時"))
    )
    assert "cruisedirect" in html
    assert "CF 挑戰逾時" in html

  def test_successful_sources_are_listed(self):
    html = page.render([make_deal()], report_with(ok("icruise")))
    assert "icruise" in html

  def test_stale_rows_are_visibly_marked(self):
    deal = make_deal(source="cruisedirect", stale_since=date(2026, 8, 10))
    html = page.render([deal], report_with(failed("cruisedirect", "擋掉了")))
    assert "2026-08-10" in html


class TestSelfContained:
  """GitHub Pages 上沒有建置流程，頁面必須自足。"""

  def test_no_external_scripts_or_stylesheets(self):
    html = page.render([make_deal()], report_with(ok("icruise")))
    assert not re.search(r'<script[^>]+src=["\']https?://', html)
    assert not re.search(r'<link[^>]+href=["\']https?://', html)

  def test_has_doctype_and_utf8_charset(self):
    html = page.render([make_deal()], report_with(ok("icruise")))
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert 'charset="utf-8"' in html.lower()

  def test_has_viewport_for_mobile(self):
    html = page.render([make_deal()], report_with(ok("icruise")))
    assert "viewport" in html


class TestTimestamp:
  def test_shows_last_updated_in_taipei_time(self):
    report = tabular.RunReport(
      generated_at=datetime(2026, 8, 13, 21, 30, tzinfo=timezone.utc), sources=[]
    )
    html = page.render([make_deal()], report)
    # UTC 21:30 -> 台北 05:30（隔日）
    assert "2026-08-14 05:30" in html


class TestWriteFile:
  def test_writes_index_html(self, tmp_path):
    path = tmp_path / "index.html"
    page.write(path, [make_deal()], report_with(ok("icruise")))
    assert path.exists()
    assert "Costa Serena" in path.read_text(encoding="utf-8")

  def test_creates_parent_directory(self, tmp_path):
    path = tmp_path / "docs" / "index.html"
    page.write(path, [make_deal()], report_with(ok("icruise")))
    assert path.exists()


RATE = Rate(usd_twd=Decimal("32"), as_of=date(2026, 8, 17), source="test")


def usd_deal(**overrides):
  """已換算過的美元報價（模擬 merge_results 走完之後的狀態）。"""
  price = overrides.pop("price", Decimal("2812"))
  return make_deal(
    price=price,
    currency="USD",
    price_twd=price * RATE.usd_twd,
    fx_rate=RATE.usd_twd,
    **overrides,
  )


class TestCurrencyDisplay:
  """台幣為主、原幣為輔。"""

  def test_shows_twd_as_the_headline_price(self):
    html = page.render([usd_deal()], report_with(ok("icruise")))
    assert "NT$89,984" in html

  def test_still_shows_the_original_quote(self):
    html = page.render([usd_deal()], report_with(ok("icruise")))
    assert "US$2,812" in html

  def test_twd_native_source_has_no_redundant_second_price(self):
    deal = make_deal(
      source="asiayo", price=Decimal("18000"), currency="TWD",
      price_twd=Decimal("18000"),
    )
    html = page.render([deal], report_with(ok("asiayo")))
    assert "NT$18,000" in html
    assert 'class="orig"' not in html

  def test_sorts_on_the_twd_amount(self):
    html = page.render([usd_deal()], report_with(ok("icruise")))
    # 排序值必須是台幣，否則跨幣別排序會錯
    assert 'data-sort="89984"' in html

  def test_unconverted_price_falls_back_to_the_original_currency(self):
    # 匯率抓不到時仍要看得到價格，只是沒有台幣
    html = page.render([make_deal(price=Decimal("2812"))], report_with(ok("icruise")))
    assert "US$2,812" in html

  def test_cheapest_stat_is_in_twd(self):
    deals = [usd_deal(price=Decimal("2812")), usd_deal(price=Decimal("1000"),
                                                       sail_date=date(2026, 9, 5))]
    html = page.render(deals, report_with(ok("icruise")))
    assert "NT$32,000" in html


class TestPriceComparison:
  def test_other_source_quotes_are_shown_in_twd(self):
    deal = usd_deal(
      other_sources={
        "asiayo": {"price": "68232", "currency": "TWD", "price_twd": "68232"}
      }
    )
    html = page.render([deal], report_with(ok("icruise")))
    assert "asiayo NT$68,232" in html

  def test_original_currency_is_kept_in_the_tooltip(self):
    deal = make_deal(
      source="asiayo", price=Decimal("67896"), currency="TWD",
      price_twd=Decimal("67896"),
      other_sources={
        "icruise": {"price": "3371", "currency": "USD", "price_twd": "107872"}
      },
    )
    html = page.render([deal], report_with(ok("asiayo")))
    assert "icruise NT$107,872" in html
    assert "icruise：US$3,371" in html

  def test_no_quote_from_another_source_is_labelled(self):
    deal = usd_deal(
      other_sources={"icruise": {"price": None, "currency": "USD", "price_twd": None}}
    )
    html = page.render([deal], report_with(ok("cruisedirect")))
    assert "icruise 洽詢報價" in html


class TestFxBanner:
  def test_shows_the_rate_in_use(self):
    report = tabular.build_report([ok("icruise")], fx=RATE)
    html = page.render([usd_deal()], report)
    assert "1 USD = 32 TWD" in html
    assert "2026-08-17" in html

  def test_stale_rate_is_flagged(self):
    stale = Rate(
      usd_twd=Decimal("32"), as_of=date(2026, 8, 10), source="test", stale=True
    )
    html = page.render([usd_deal()], tabular.build_report([ok("icruise")], fx=stale))
    assert "沿用" in html
    assert "note warn" in html

  def test_missing_rate_is_flagged(self):
    html = page.render([make_deal()], tabular.build_report([ok("icruise")]))
    assert "沒有取得匯率" in html


class TestShipNameTooltip:
  def test_chinese_original_name_is_available_as_a_tooltip(self):
    deal = make_deal(
      source="asiayo", ship_name="Star Voyager", ship_name_raw="探索星號"
    )
    html = page.render([deal], report_with(ok("asiayo")))
    assert 'title="探索星號"' in html

  def test_chinese_name_is_searchable(self):
    deal = make_deal(
      source="asiayo", ship_name="Star Voyager", ship_name_raw="探索星號"
    )
    html = page.render([deal], report_with(ok("asiayo")))
    row = re.search(r'data-search="([^"]*)"', html).group(1)
    assert "探索星號" in row


class TestWarnings:
  def test_unmapped_ship_warning_is_shown(self):
    report = report_with(ok("asiayo"))
    report.warnings = ["這些船名沒有英文對照，無法跨來源比價：愛達魔都號"]
    html = page.render([make_deal()], report)
    assert "愛達魔都號" in html

  def test_no_warning_block_when_there_is_nothing_to_report(self):
    html = page.render([make_deal()], report_with(ok("icruise")))
    assert "無法跨來源比價" not in html
