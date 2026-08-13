"""Deal 資料模型的測試：去重鍵、排序、JSON 往返。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from cruise_deals.models import Deal, sort_key


def make_deal(**overrides) -> Deal:
  """建立測試用 Deal，只覆寫關心的欄位。"""
  base = dict(
    source="icruise",
    sail_date=date(2026, 8, 16),
    depart_port="Keelung",
    depart_port_raw="Keelung (Taipei), Taiwan",
    arrive_port="Keelung (Taipei), Taiwan",
    ports_of_call=("Keelung (Taipei)", "Naha", "Ishigaki"),
    ship_name="Costa Serena",
    cruise_line="Costa Cruises",
    nights=3,
    price=Decimal("479"),
    currency="USD",
    price_note="per person, double occupancy",
    detail_url="https://www.icruise.com/itineraries/x.html",
    scraped_at=datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc),
  )
  base.update(overrides)
  return Deal(**base)


class TestDedupKey:
  def test_identical_sailings_share_a_key(self):
    assert make_deal().dedup_key == make_deal().dedup_key

  def test_key_ignores_source(self):
    # 同一航次出現在不同來源時必須被視為同一筆
    a = make_deal(source="icruise")
    b = make_deal(source="expedia")
    assert a.dedup_key == b.dedup_key

  def test_key_ignores_price(self):
    a = make_deal(price=Decimal("479"))
    b = make_deal(price=Decimal("999"))
    assert a.dedup_key == b.dedup_key

  def test_key_ignores_case_and_whitespace(self):
    # 各站對船名／船公司的大小寫寫法可能不同
    a = make_deal(ship_name="Costa Serena", cruise_line="Costa Cruises")
    b = make_deal(ship_name="  COSTA   SERENA ", cruise_line="costa cruises")
    assert a.dedup_key == b.dedup_key

  def test_key_ignores_cruise_line_naming_differences(self):
    """各站對船公司的寫法不同，不能因此把同一航次拆成兩筆。

    icruise 寫 "Celebrity Cruises"，cruisedirect 的 logo 只給 "celebrity"。
    船名（Celebrity Millennium）在全球郵輪業是唯一的，
    加上出發日、夜數、出發港已足以識別同一航次。
    """
    a = make_deal(ship_name="Celebrity Millennium", cruise_line="Celebrity Cruises")
    b = make_deal(ship_name="Celebrity Millennium", cruise_line="Celebrity")
    assert a.dedup_key == b.dedup_key

  def test_different_sail_date_differs(self):
    assert make_deal().dedup_key != make_deal(sail_date=date(2026, 8, 17)).dedup_key

  def test_different_ship_differs(self):
    assert make_deal().dedup_key != make_deal(ship_name="Costa Fortuna").dedup_key

  def test_different_nights_differs(self):
    assert make_deal().dedup_key != make_deal(nights=4).dedup_key

  def test_different_depart_port_differs(self):
    assert make_deal().dedup_key != make_deal(depart_port="Tokyo").dedup_key


class TestSortKey:
  def test_sorts_by_sail_date_ascending(self):
    early = make_deal(sail_date=date(2026, 8, 10))
    late = make_deal(sail_date=date(2026, 8, 20))
    assert sorted([late, early], key=sort_key) == [early, late]

  def test_same_date_sorts_by_price_ascending(self):
    cheap = make_deal(price=Decimal("100"))
    pricey = make_deal(price=Decimal("900"))
    assert sorted([pricey, cheap], key=sort_key) == [cheap, pricey]

  def test_no_price_sorts_last_within_same_date(self):
    # 「洽詢報價」不該被當成最便宜排在最前面
    priced = make_deal(price=Decimal("900"))
    unpriced = make_deal(price=None)
    assert sorted([unpriced, priced], key=sort_key) == [priced, unpriced]


class TestJsonRoundTrip:
  def test_round_trip_preserves_all_fields(self):
    original = make_deal()
    restored = Deal.from_dict(original.to_dict())
    assert restored == original

  def test_round_trip_preserves_none_price(self):
    original = make_deal(price=None)
    restored = Deal.from_dict(original.to_dict())
    assert restored.price is None
    assert restored == original

  def test_to_dict_is_json_serialisable(self):
    import json

    # Decimal / date / tuple 都必須先轉成 JSON 可接受的型別
    json.dumps(make_deal().to_dict())

  def test_price_survives_as_decimal_not_float(self):
    # 用 float 存錢會有精度問題，必須是 Decimal
    restored = Deal.from_dict(make_deal(price=Decimal("1299.50")).to_dict())
    assert restored.price == Decimal("1299.50")
    assert isinstance(restored.price, Decimal)
