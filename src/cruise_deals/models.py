"""統一的 Deal 資料模型。

三個來源的原始欄位長得完全不一樣，各 scraper 的職責就是把自己的格式
轉成這裡的 Deal；下游的合併、輸出、網頁都只認識 Deal。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .normalize import clean_text

# 去重鍵的型別：(船公司, 郵輪名, 出發日期, 夜數, 出發港)
DedupKey = tuple[str, str, date, int, str]


def _key_part(value: str) -> str:
  """把字串正規化成去重鍵的一部分（忽略大小寫與多餘空白）。"""
  return clean_text(value).lower()


@dataclass(frozen=True)
class Deal:
  """一筆郵輪航次報價。"""

  source: str  # 來源站名：icruise / cruisedirect / expedia
  sail_date: date  # 出發日期
  depart_port: str  # 出發港口（正規化後：Keelung / Tokyo / Yokohama）
  depart_port_raw: str  # 來源站的原始寫法，供除錯比對
  arrive_port: str  # 目的港口（航程結束港）
  ports_of_call: tuple[str, ...]  # 沿途停靠港
  ship_name: str  # 郵輪名
  cruise_line: str  # 船公司
  nights: int  # 航行天數（夜）
  price: Decimal | None  # 最低價格；None 代表洽詢報價
  currency: str  # 幣別，預設 USD
  price_note: str  # 價格的但書，例如 per person / double occupancy
  detail_url: str  # 該航次的詳情頁
  scraped_at: datetime  # 擷取時間（UTC）

  # 該筆資料所屬來源最後一次擷取成功的日期；
  # 來源本次失敗、沿用舊資料時才會有值。
  stale_since: date | None = None

  # 同一航次在其他來源的報價，格式為 {來源名: 價格字串或 None}
  other_sources: dict[str, str | None] = field(default_factory=dict)

  @property
  def dedup_key(self) -> DedupKey:
    """跨來源辨識「同一個航次」的鍵。刻意不含 source 與 price。"""
    return (
      _key_part(self.cruise_line),
      _key_part(self.ship_name),
      self.sail_date,
      self.nights,
      _key_part(self.depart_port),
    )

  def to_dict(self) -> dict[str, Any]:
    """轉成可直接 json.dumps 的 dict。價格用字串保存以免 float 精度失真。"""
    data = asdict(self)
    data["sail_date"] = self.sail_date.isoformat()
    data["scraped_at"] = self.scraped_at.isoformat()
    data["ports_of_call"] = list(self.ports_of_call)
    data["price"] = str(self.price) if self.price is not None else None
    data["stale_since"] = self.stale_since.isoformat() if self.stale_since else None
    return data

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> Deal:
    """to_dict() 的反向操作。"""
    price = data.get("price")
    stale = data.get("stale_since")
    return cls(
      source=data["source"],
      sail_date=date.fromisoformat(data["sail_date"]),
      depart_port=data["depart_port"],
      depart_port_raw=data.get("depart_port_raw", ""),
      arrive_port=data.get("arrive_port", ""),
      ports_of_call=tuple(data.get("ports_of_call") or ()),
      ship_name=data["ship_name"],
      cruise_line=data["cruise_line"],
      nights=int(data["nights"]),
      price=Decimal(price) if price is not None else None,
      currency=data.get("currency", "USD"),
      price_note=data.get("price_note", ""),
      detail_url=data.get("detail_url", ""),
      scraped_at=datetime.fromisoformat(data["scraped_at"]),
      stale_since=date.fromisoformat(stale) if stale else None,
      other_sources=dict(data.get("other_sources") or {}),
    )


def sort_key(deal: Deal) -> tuple[date, int, Decimal]:
  """排序用：出發日期遞增，同日期則價格遞增，無報價者排最後。"""
  has_no_price = deal.price is None
  return (deal.sail_date, int(has_no_price), deal.price or Decimal(0))


def utcnow() -> datetime:
  """目前 UTC 時間（帶時區），統一由此取得方便測試替換。"""
  return datetime.now(timezone.utc)
