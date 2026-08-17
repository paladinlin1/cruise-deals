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

  def test_custom_separator_for_ports_containing_commas(self):
    """cruisedirect 用 " - " 分隔，而港名本身含逗號（"Tokyo, Japan"），
    用逗號切會把國名切成獨立的港口。"""
    raw = "Tokyo, Japan - Kochi, Japan - Busan, South Korea"
    assert normalize.split_ports(raw, separator=" - ") == (
      "Tokyo, Japan", "Kochi, Japan", "Busan, South Korea",
    )

  def test_strips_singular_port_of_call_label(self):
    # cruisedirect 用單數 "Port of Call"
    raw = "Port of Call Tokyo, Japan - Kochi, Japan"
    assert normalize.split_ports(raw, separator=" - ") == ("Tokyo, Japan", "Kochi, Japan")


class TestChinesePortMatching:
  """台灣站的港名是中文，要對映到與外國站相同的正規化名稱。"""

  def test_keelung_in_chinese(self):
    assert normalize.match_port("基隆") == "Keelung"

  def test_keelung_port_suffix(self):
    assert normalize.match_port("基隆港") == "Keelung"

  def test_tokyo_in_chinese(self):
    assert normalize.match_port("日本東京出發：下午 4:30") == "Tokyo"

  def test_yokohama_in_chinese(self):
    assert normalize.match_port("日本 橫濱 登船") == "Yokohama"

  def test_japanese_yokohama_spelling(self):
    assert normalize.match_port("横浜港") == "Yokohama"

  def test_yokohama_wins_when_both_appear(self):
    """asiayo 把兩個港併寫成「東京（東京/橫濱）」，行程第一天寫
    「日本 東京 (橫濱) 登船」的其實是橫濱出發，外國站也都寫 Yokohama。
    Tokyo 若先比對就會判錯，兩邊就永遠合併不起來。"""
    assert normalize.match_port("日本 東京 (橫濱) 登船") == "Yokohama"

  def test_non_target_chinese_port_is_ignored(self):
    assert normalize.match_port("高雄") is None


class TestPortGroup:
  """東京與橫濱在去重時視為同一個港。"""

  def test_tokyo_and_yokohama_share_a_group(self):
    assert normalize.port_group("Tokyo") == normalize.port_group("Yokohama")

  def test_keelung_has_its_own_group(self):
    assert normalize.port_group("Keelung") != normalize.port_group("Tokyo")

  def test_unknown_port_falls_back_to_itself(self):
    assert normalize.port_group("Kobe") == "kobe"


class TestCanonicalShip:
  def test_chinese_name_maps_to_english(self):
    assert normalize.canonical_ship("鑽石公主號") == "Diamond Princess"

  def test_english_name_is_left_alone(self):
    assert normalize.canonical_ship("Diamond Princess") == "Diamond Princess"

  def test_longest_alias_wins(self):
    # 「藍寶石公主號」不可以被較短的鍵先搶走
    assert normalize.canonical_ship("藍寶石公主號") == "Sapphire Princess"

  def test_english_name_embedded_in_chinese_text_is_extracted(self):
    name = "【名人遊輪凱旋號】CELEBRITY ASCENT～ 12 晚"
    assert normalize.canonical_ship(name) == "CELEBRITY ASCENT"

  def test_unknown_chinese_name_is_kept_as_is(self):
    # 一艘沒登記的新船不該讓整批擷取失敗
    assert normalize.canonical_ship("愛達魔都號") == "愛達魔都號"

  def test_empty_stays_empty(self):
    assert normalize.canonical_ship(None) == ""


class TestUnmappedShipDetection:
  def test_chinese_name_without_mapping_is_flagged(self):
    assert normalize.is_unmapped_ship("愛達魔都號") is True

  def test_mapped_chinese_name_is_not_flagged(self):
    assert normalize.is_unmapped_ship("鑽石公主號") is False

  def test_english_name_is_not_flagged(self):
    assert normalize.is_unmapped_ship("Costa Serena") is False


class TestSplitShipAndLine:
  """台灣站把船公司與船名塞在商品名稱裡，三種寫法都要吃。"""

  def test_line_and_ship_glued_together(self):
    ship, raw, line = normalize.split_ship_and_line(
      "【麗星郵輪探索星號】鹿兒島、熊本、那霸6天-週日出發"
    )
    assert (ship, raw, line) == ("Star Voyager", "探索星號", "Star Cruises")

  def test_line_and_ship_separated_by_a_dot(self):
    ship, raw, line = normalize.split_ship_and_line("【MSC郵輪．榮耀號】沖繩自主遊４天")
    assert (ship, raw, line) == ("MSC Bellissima", "榮耀號", "MSC Cruises")

  def test_ship_name_outside_the_brackets(self):
    ship, raw, line = normalize.split_ship_and_line(
      "【公主遊輪】鑽石公主號～日本探險家之旅 11天｜可加購橫濱飯店"
    )
    assert (ship, raw, line) == ("Diamond Princess", "鑽石公主號", "Princess Cruises")

  def test_unknown_ship_keeps_its_chinese_name(self):
    ship, raw, line = normalize.split_ship_and_line("【愛達郵輪】魔都號～上海３天")
    assert ship == raw == "魔都號"

  def test_empty_name_yields_empties(self):
    assert normalize.split_ship_and_line("") == ("", "", "")


class TestMatchAlias:
  def test_returns_none_when_nothing_matches(self):
    assert normalize.match_alias("完全無關的字串", {"探索星號": "Star Voyager"}) is None

  def test_returns_none_for_empty_text(self):
    assert normalize.match_alias("", {"探索星號": "Star Voyager"}) is None
