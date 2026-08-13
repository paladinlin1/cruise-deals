"""測試用的 Deal 建構工廠。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from cruise_deals.models import Deal


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
