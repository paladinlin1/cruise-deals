"""normalize.py 的單元測試：全部使用真實網站上出現過的字串。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cruise_deals import normalize


class TestMatchPort:
  """出發港比對：各站對同一港口的寫法不同，需容錯。"""

  def test_icruise_keelung_format(self):
    # icruise 實際輸出格式
    assert normalize.match_port("Keelung (Taipei), Taiwan") == "Keelung"

  def test_tokyo(self):
    assert normalize.match_port("Tokyo, Japan") == "Tokyo"

  def test_yokohama(self):
    assert normalize.match_port("Yokohama, Japan") == "Yokohama"

  def test_bare_port_name_without_country(self):
    # 其他站可能只寫港名
    assert normalize.match_port("Keelung") == "Keelung"

  def test_case_insensitive(self):
    assert normalize.match_port("KEELUNG (TAIPEI), TAIWAN") == "Keelung"

  def test_non_target_port_returns_none(self):
    assert normalize.match_port("Busan (Pusan), South Korea") is None

  def test_empty_returns_none(self):
    assert normalize.match_port("") is None

  def test_tokyo_must_not_match_yokohama(self):
    # 「Yokohama」不含「tokyo」子字串，但這個斷言防止未來加入過寬的別名
    assert normalize.match_port("Yokohama, Japan") != "Tokyo"


class TestStripPrefixLabel:
  """icruise 的港口欄位含 <b>Starts: </b> 標籤，取 text 後需去除。"""

  def test_strips_starts_label(self):
    assert normalize.strip_prefix_label(
      "Starts: Keelung (Taipei), Taiwan"
    ) == "Keelung (Taipei), Taiwan"

  def test_strips_ends_label(self):
    assert normalize.strip_prefix_label(
      "Ends: Busan (Pusan), South Korea"
    ) == "Busan (Pusan), South Korea"

  def test_collapses_whitespace(self):
    assert normalize.strip_prefix_label(
      "Starts:   Tokyo,\n  Japan  "
    ) == "Tokyo, Japan"

  def test_leaves_unlabelled_text_alone(self):
    assert normalize.strip_prefix_label("Tokyo, Japan") == "Tokyo, Japan"


class TestParseSailDate:
  def test_icruise_format(self):
    assert normalize.parse_sail_date("Aug 16, 2026") == date(2026, 8, 16)

  def test_iso_format(self):
    assert normalize.parse_sail_date("2026-08-16") == date(2026, 8, 16)

  def test_slash_format(self):
    assert normalize.parse_sail_date("08/16/2026") == date(2026, 8, 16)

  def test_expedia_format(self):
    # Expedia 的 packages[].startDateTime 格式
    assert normalize.parse_sail_date("16-Aug-2026") == date(2026, 8, 16)

  def test_iso_datetime_format(self):
    # Expedia JSON 可能回傳帶時間的 ISO 字串
    assert normalize.parse_sail_date("2026-08-16T00:00:00") == date(2026, 8, 16)

  def test_surrounding_whitespace_tolerated(self):
    assert normalize.parse_sail_date("  Aug 16, 2026 \n") == date(2026, 8, 16)

  def test_unparseable_raises(self):
    with pytest.raises(ValueError):
      normalize.parse_sail_date("not a date")


class TestParsePrice:
  def test_plain_dollar_amount(self):
    assert normalize.parse_price("$479") == Decimal("479")

  def test_thousands_separator(self):
    assert normalize.parse_price("$1,299") == Decimal("1299")

  def test_decimal_amount(self):
    assert normalize.parse_price("$1,299.50") == Decimal("1299.50")

  def test_pricing_on_request_returns_none(self):
    # icruise 的 <h2 class="noprice">Pricing On<br>Request</h2>
    # 取 text 後可能是 "Pricing OnRequest"（無空格），兩種都要處理
    assert normalize.parse_price("Pricing On Request") is None
    assert normalize.parse_price("Pricing OnRequest") is None

  def test_empty_returns_none(self):
    assert normalize.parse_price("") is None
    assert normalize.parse_price("   ") is None

  def test_numeric_input(self):
    # Expedia JSON 會直接給數字
    assert normalize.parse_price(479) == Decimal("479")
    assert normalize.parse_price(479.5) == Decimal("479.5")

  def test_none_input_returns_none(self):
    assert normalize.parse_price(None) is None

  def test_zero_price_treated_as_no_price(self):
    # 0 元不是真實報價，視為無報價以免排序時排到最前面
    assert normalize.parse_price("$0") is None
    assert normalize.parse_price(0) is None


class TestParseNights:
  def test_plural(self):
    assert normalize.parse_nights("3 Nights") == 3

  def test_singular(self):
    assert normalize.parse_nights("1 Night") == 1

  def test_from_itinerary_title(self):
    assert normalize.parse_nights("3 Night Keelung to Keelung Cruise") == 3

  def test_two_digit(self):
    assert normalize.parse_nights("14 Nights") == 14

  def test_integer_input(self):
    assert normalize.parse_nights(7) == 7

  def test_unparseable_raises(self):
    with pytest.raises(ValueError):
      normalize.parse_nights("no number here")


class TestSplitPorts:
  def test_splits_comma_separated_port_list(self):
    raw = "Keelung (Taipei), Naha, Ishigaki, Keelung (Taipei)"
    assert normalize.split_ports(raw) == (
      "Keelung (Taipei)", "Naha", "Ishigaki", "Keelung (Taipei)",
    )

  def test_empty_returns_empty_tuple(self):
    assert normalize.split_ports("") == ()

  def test_strips_ports_of_call_label(self):
    raw = "Ports of Call: Tokyo, Osaka, Kochi"
    assert normalize.split_ports(raw) == ("Tokyo", "Osaka", "Kochi")
