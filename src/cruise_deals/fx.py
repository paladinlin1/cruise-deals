"""匯率取得與換算。

台灣站（asiayo／百威）報台幣、外國站報美元，要比價就得換成同一個幣別。
統一換成**台幣**，因為使用者是在台灣付錢。

為什麼不用台灣銀行牌告匯率：`rate.bot.com.tw` 已經上了機器人挑戰頁
（`httpx` 拿到的是 `Challenge Validation` 的 HTML），在 GitHub Actions 上過不了。
改用兩個免金鑰的公開 API，主要來源掛掉就換備援。

抓不到匯率時**不要讓整個流程失敗**——沿用上一次的匯率並標記 stale，
與「來源擷取失敗就沿用舊資料」的原則一致。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)

TWD = "TWD"
USD = "USD"


class FxError(RuntimeError):
  """匯率取不到。呼叫端應改為沿用上一次的匯率，而不是放棄換算。"""


@dataclass(frozen=True)
class Rate:
  """一筆 USD -> TWD 的匯率。"""

  usd_twd: Decimal
  as_of: date  # 這個匯率的日期
  source: str  # 來源網址主機名，供除錯
  stale: bool = False  # True 代表本次沒抓到、沿用上一次的值

  def to_dict(self) -> dict[str, Any]:
    return {
      "usd_twd": str(self.usd_twd),
      "as_of": self.as_of.isoformat(),
      "source": self.source,
      "stale": self.stale,
    }

  @classmethod
  def from_dict(cls, data: dict[str, Any] | None) -> Rate | None:
    """to_dict() 的反向操作。資料缺漏或壞掉時回 None（視為沒有前次匯率）。"""
    if not data:
      return None
    try:
      return cls(
        usd_twd=Decimal(str(data["usd_twd"])),
        as_of=date.fromisoformat(data["as_of"]),
        source=data.get("source", ""),
        stale=bool(data.get("stale", False)),
      )
    except (KeyError, ValueError, TypeError, InvalidOperation):
      log.warning("前次匯率資料無法解析，視為沒有前次匯率")
      return None


def _positive(value: Any) -> Decimal:
  """把 API 回傳的數字轉成正的 Decimal，否則拋 FxError。"""
  try:
    rate = Decimal(str(value))
  except (InvalidOperation, TypeError):
    raise FxError(f"匯率不是數字: {value!r}") from None
  if rate <= 0:
    raise FxError(f"匯率不合理: {rate}")
  return rate


def parse_er_api(payload: dict[str, Any]) -> Rate:
  """解析 open.er-api.com 的回應。"""
  if payload.get("result") != "success":
    raise FxError(f"open.er-api 回報失敗: {payload.get('error-type')}")
  rate = _positive((payload.get("rates") or {}).get(TWD))

  # "Mon, 17 Aug 2026 00:02:31 +0000"；解析不了就用今天，不要因為日期而整個失敗
  stamp = payload.get("time_last_update_utc") or ""
  try:
    as_of = datetime.strptime(stamp, "%a, %d %b %Y %H:%M:%S %z").date()
  except ValueError:
    as_of = datetime.now(timezone.utc).date()
  return Rate(usd_twd=rate, as_of=as_of, source="open.er-api.com")


def parse_rter(payload: dict[str, Any]) -> Rate:
  """解析 tw.rter.info 的回應（{"USDTWD": {"Exrate": 31.882, "UTC": "..."}}）。"""
  entry = payload.get("USDTWD") or {}
  rate = _positive(entry.get("Exrate"))
  try:
    as_of = datetime.fromisoformat(str(entry.get("UTC"))).date()
  except (ValueError, TypeError):
    as_of = datetime.now(timezone.utc).date()
  return Rate(usd_twd=rate, as_of=as_of, source="tw.rter.info")


# (網址, 解析函式) 依序嘗試
_PROVIDERS = (
  (config.FX_PRIMARY_URL, parse_er_api),
  (config.FX_FALLBACK_URL, parse_rter),
)


def fetch(client: httpx.Client | None = None) -> Rate:
  """取得今天的 USD -> TWD 匯率。所有來源都失敗時拋 FxError。"""
  owns_client = client is None
  client = client or httpx.Client(
    headers={"User-Agent": config.USER_AGENT}, timeout=30.0, follow_redirects=True
  )
  failures: list[str] = []
  try:
    for url, parse in _PROVIDERS:
      try:
        response = client.get(url)
        response.raise_for_status()
        rate = parse(response.json())
        log.info("匯率 1 USD = %s TWD（%s，%s）", rate.usd_twd, rate.as_of, rate.source)
        return rate
      except Exception as exc:  # noqa: BLE001 - 換下一個來源就好
        failures.append(f"{url}: {type(exc).__name__}: {exc}")
        log.warning("匯率來源失敗（%s），嘗試下一個", failures[-1])
  finally:
    if owns_client:
      client.close()

  raise FxError("；".join(failures) or "沒有可用的匯率來源")


def convert(price: Decimal | None, currency: str, rate: Rate | None) -> Decimal | None:
  """把報價換算成台幣。無法換算時回 None（不要回 0，會被誤當成最便宜）。"""
  if price is None:
    return None
  code = (currency or "").strip().upper()
  if code == TWD:
    return price.quantize(Decimal("1"))
  if code == USD and rate is not None:
    return (price * rate.usd_twd).quantize(Decimal("1"))
  if code and code != USD:
    log.warning("沒有 %s 的匯率，該筆報價不做台幣換算", code)
  return None
