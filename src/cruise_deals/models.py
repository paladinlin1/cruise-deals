"""統一的 Deal 資料模型。

三個來源的原始欄位長得完全不一樣，各 scraper 的職責就是把自己的格式
轉成這裡的 Deal；下游的合併、輸出、網頁都只認識 Deal。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from .normalize import canonical_ship, clean_text, port_group

# 去重鍵的型別：(郵輪名, 出發日期, 夜數, 出發港分組)
DedupKey = tuple[str, date, int, str]

# 同一航次在其他來源的報價
OtherQuote = dict[str, str | None]


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
  ship_name: str  # 郵輪名（正規化後的英文正式船名）
  cruise_line: str  # 船公司
  nights: int  # 航行天數（夜）
  price: Decimal | None  # 最低價格；None 代表洽詢報價
  currency: str  # 幣別：外國站 USD、台灣站 TWD
  price_note: str  # 價格的但書，例如 per person / double occupancy
  detail_url: str  # 該航次的詳情頁
  scraped_at: datetime  # 擷取時間（UTC）

  # 該筆資料所屬來源最後一次擷取成功的日期；
  # 來源本次失敗、沿用舊資料時才會有值。
  stale_since: date | None = None

  # 同一航次在其他來源的報價，格式為
  # {來源名: {"price": "2812", "currency": "USD", "price_twd": "89876"}}
  other_sources: dict[str, OtherQuote] = field(default_factory=dict)

  # 換算成台幣的價格。跨幣別比價、排序、最低價統計全部以這個欄位為準——
  # 直接比 price 會讓 379 USD 看起來比 18,000 TWD 便宜。
  price_twd: Decimal | None = None
  fx_rate: Decimal | None = None  # 換算當下使用的 USD→TWD 匯率；台幣原生報價為 None
  ship_name_raw: str = ""  # 來源站的原始船名（台灣站是中文），供除錯與網頁 tooltip

  @property
  def dedup_key(self) -> DedupKey:
    """跨來源辨識「同一個航次」的鍵。刻意不含 source 與 price。

    也刻意**不含船公司**：各站寫法差異太大
    （icruise "Celebrity Cruises" vs cruisedirect logo "celebrity"），
    納入會讓同一航次無法合併。船名在郵輪業是唯一的，
    加上出發日、夜數、出發港已足以識別。

    船名走 canonical_ship()、出發港走 port_group()，這兩層轉換是
    台灣站（中文船名、東京／橫濱不分）能與外國站合併比價的關鍵。
    """
    return (
      _key_part(canonical_ship(self.ship_name)),
      self.sail_date,
      self.nights,
      port_group(self.depart_port),
    )

  def to_dict(self) -> dict[str, Any]:
    """轉成可直接 json.dumps 的 dict。價格用字串保存以免 float 精度失真。"""
    data = asdict(self)
    data["sail_date"] = self.sail_date.isoformat()
    data["scraped_at"] = self.scraped_at.isoformat()
    data["ports_of_call"] = list(self.ports_of_call)
    data["price"] = str(self.price) if self.price is not None else None
    data["price_twd"] = str(self.price_twd) if self.price_twd is not None else None
    data["fx_rate"] = str(self.fx_rate) if self.fx_rate is not None else None
    data["stale_since"] = self.stale_since.isoformat() if self.stale_since else None
    return data

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> Deal:
    """to_dict() 的反向操作。"""
    price = data.get("price")
    price_twd = data.get("price_twd")
    fx_rate = data.get("fx_rate")
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
      other_sources=upgrade_other_sources(data.get("other_sources")),
      price_twd=Decimal(price_twd) if price_twd is not None else None,
      fx_rate=Decimal(fx_rate) if fx_rate is not None else None,
      ship_name_raw=data.get("ship_name_raw", ""),
    )


def upgrade_other_sources(raw: Any) -> dict[str, OtherQuote]:
  """把 other_sources 讀成現行格式，同時接受加入台幣前的舊格式。

  舊格式是 {來源名: 價格字串或 None}（當時全站都是美元）。
  歷史快照與既有的 deals.json 仍是那個形狀，不能讀不進來。
  """
  result: dict[str, OtherQuote] = {}
  for source, value in (raw or {}).items():
    if isinstance(value, dict):
      result[source] = {
        "price": value.get("price"),
        "currency": value.get("currency") or "USD",
        "price_twd": value.get("price_twd"),
      }
    else:
      result[source] = {"price": value, "currency": "USD", "price_twd": None}
  return result


def sort_key(deal: Deal) -> tuple[date, int, Decimal]:
  """排序用：出發日期遞增，同日期則價格遞增，無報價者排最後。

  比的是台幣價，因為各來源幣別不同（見 Deal.price_twd）。
  """
  amount = deal.price_twd if deal.price_twd is not None else deal.price
  return (deal.sail_date, int(amount is None), amount or Decimal(0))


def utcnow() -> datetime:
  """目前 UTC 時間（帶時區），統一由此取得方便測試替換。"""
  return datetime.now(timezone.utc)
