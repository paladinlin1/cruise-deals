"""百威旅遊 bwt.com.tw 擷取器測試。

跑在真實存下來的 SSE 串流上（`tests/fixtures/bwt_sse.txt`），不需要網路。

這一站的重點不在解析難度，而在兩個判斷：
  1. 什麼該收（基隆港出發的純郵輪）、什麼不該收（機＋船套裝、渡輪船票）
  2. 「未來 30 天 0 筆」是正常的，「串流沒收完」才是壞掉
"""

from __future__ import annotations

import ssl
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cruise_deals.scrapers import bwt
from cruise_deals.scrapers.base import ParseError

FIXTURES = Path(__file__).parent / "fixtures"

# fixture 抓取當下的資料涵蓋到 2027 年底
WIDE = (date(2026, 8, 17), date(2027, 12, 31))


@pytest.fixture(scope="module")
def steps() -> dict:
  raw = (FIXTURES / "bwt_sse.txt").read_text(encoding="utf-8")
  return bwt.read_sse(raw.splitlines())


class TestSseParsing:
  def test_reads_all_three_steps(self, steps):
    assert len(steps["step1"]) == 125
    assert len(steps["step2"]) == 125
    assert steps["step3"]["totalGroups"] == 125

  def test_ignores_heartbeats_and_done_marker(self):
    lines = [
      "",
      ": keep-alive",
      'data: {"step":"step1","data":[]}',
      "data: [DONE]",
      "event: complete",
    ]
    assert bwt.read_sse(lines) == {"step1": []}

  def test_skips_unparseable_fragments(self):
    lines = ["data: {broken", 'data: {"step":"step2","data":[1]}']
    assert bwt.read_sse(lines) == {"step2": [1]}


class TestIncompleteStream:
  """串流中斷等於安靜漏資料，必須大聲失敗而不是回少少幾筆。"""

  def test_missing_step3_raises(self, steps):
    truncated = {"step1": steps["step1"], "step2": steps["step2"]}
    with pytest.raises(ParseError, match="step3"):
      bwt.parse_steps(truncated, *WIDE)

  def test_missing_step1_raises(self, steps):
    with pytest.raises(ParseError, match="step1"):
      bwt.parse_steps({"step1": [], "step2": [], "step3": {}}, *WIDE)


class TestProductFiltering:
  def test_only_keelung_departures_are_kept(self, steps):
    deals = bwt.parse_steps(steps, *WIDE)
    assert deals
    assert {d.depart_port for d in deals} == {"Keelung"}

  def test_flight_packages_are_excluded(self, steps):
    # 桃園機場出發的是機票＋郵輪套裝，價格含機票不能跟外國站比
    taoyuan = [
      d["mainGroupCode"]
      for d in steps["step2"]
      if d.get("departure") == "桃園國際機場"
    ]
    assert taoyuan  # fixture 裡確實有這種商品
    codes = {d.detail_url for d in bwt.parse_steps(steps, *WIDE)}
    assert not any(code in url for code in taoyuan for url in codes)

  def test_ferry_tickets_are_excluded(self, steps):
    # 「【單訂船票】八重山丸過夜渡輪」也從基隆港出發，但不是郵輪航次；
    # 靠 hasCruisePrice 濾掉
    assert any(
      "單訂船票" in m["groupName"] and not m["hasCruisePrice"] for m in steps["step1"]
    )
    assert not any("八重山丸" in d.ship_name for d in bwt.parse_steps(steps, *WIDE))


class TestParsing:
  def test_group_days_are_converted_to_nights(self, steps):
    deal = min(bwt.parse_steps(steps, *WIDE), key=lambda d: d.sail_date)
    assert deal.sail_date == date(2026, 11, 29)
    assert deal.nights == 5  # 「６天」的行程是 5 夜

  def test_chinese_ship_name_is_mapped_to_english(self, steps):
    deal = min(bwt.parse_steps(steps, *WIDE), key=lambda d: d.sail_date)
    assert deal.ship_name == "MSC Bellissima"
    assert deal.ship_name_raw == "榮耀號"
    assert deal.cruise_line == "MSC Cruises"

  def test_prices_are_new_taiwan_dollars(self, steps):
    deal = min(bwt.parse_steps(steps, *WIDE), key=lambda d: d.sail_date)
    assert deal.currency == "TWD"
    assert deal.price == Decimal("9900")

  def test_detail_url_uses_the_group_code(self, steps):
    for deal in bwt.parse_steps(steps, *WIDE):
      assert deal.detail_url.startswith("https://www.bwt.com.tw/Tour/")

  def test_round_trip_returns_to_the_departure_port(self, steps):
    for deal in bwt.parse_steps(steps, *WIDE):
      assert deal.arrive_port == deal.depart_port

  def test_placeholder_price_is_treated_as_no_quote(self, steps):
    main = {"groupName": "【MSC郵輪．榮耀號】沖繩自主遊４天", "hasCruisePrice": True}
    group = {"leaveDate": "2026/12/04", "groupDay": 4, "price": bwt.NO_PRICE}

    deal = bwt._parse_group(group, main, "Keelung", {"departure": "基隆港"}, None)

    assert deal is not None
    assert deal.price is None


class TestItineraryPorts:
  def test_ports_after_the_wave_dash(self):
    assert bwt.itinerary_ports("【MSC郵輪．榮耀號】日韓自主遊６天～鹿兒島、濟州") == (
      "鹿兒島",
      "濟州",
    )

  def test_ports_before_the_day_count(self):
    assert bwt.itinerary_ports("【MSC郵輪．榮耀號】宮古島、沖繩、石垣島自主遊５天") == (
      "宮古島",
      "沖繩",
      "石垣島",
    )

  def test_trailing_note_is_dropped(self):
    assert bwt.itinerary_ports(
      "【MSC郵輪．榮耀號】日韓自主遊６天～佐世保、釜山(加購岸上觀光)"
    ) == ("佐世保", "釜山")

  def test_empty_name_yields_nothing(self):
    assert bwt.itinerary_ports("") == ()


class TestEmptyWindowIsNormal:
  """該站郵輪團期最早在三個月後，30 天內 0 筆是常態不是壞掉。"""

  def test_next_thirty_days_yields_nothing_without_raising(self, steps):
    assert bwt.parse_steps(steps, date(2026, 8, 17), date(2026, 9, 16)) == []


class TestTlsWorkaround:
  def test_strict_x509_check_is_relaxed_but_chain_is_still_verified(self):
    # 對方憑證缺 Subject Key Identifier，Python 3.13 的嚴格檢查會擋下來。
    # 只清掉那個旗標，不可以退化成 verify=False。
    context = bwt.ssl_context()
    assert not (context.verify_flags & ssl.VERIFY_X509_STRICT)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


class TestWithinSourceDedup:
  def test_same_sailing_with_two_group_codes_keeps_the_cheaper(self):
    from factories import make_deal

    collected: dict = {}
    bwt._keep_cheaper(collected, make_deal(source="bwt", price=Decimal("12900")))
    bwt._keep_cheaper(collected, make_deal(source="bwt", price=Decimal("11900")))

    assert len(collected) == 1
    assert next(iter(collected.values())).price == Decimal("11900")
