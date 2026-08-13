"""GitHub Pages 表格網頁產生器的測試。"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

from factories import make_deal

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
