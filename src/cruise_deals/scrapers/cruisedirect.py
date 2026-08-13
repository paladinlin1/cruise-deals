"""cruisedirect.com 擷取器。

## 目前狀態：無法存取

該站以 Cloudflare 全面封鎖自動化存取。實測結果（2026-08-13）：

  - httpx 直接請求        -> 403，`Cf-Mitigated: challenge`
  - 換成瀏覽器 User-Agent -> 403
  - patchright 無頭瀏覽器 -> 挑戰頁 30 秒內未解除
  - patchright 有頭瀏覽器 -> 同樣未解除
  - 連 /robots.txt 都回 403

連 robots.txt 都擋，代表站方是刻意且全面地拒絕自動化存取。
要通過就必須主動破解其機器人防護，本專案不做這件事。

## 本模組因此的職責

1. 正常嘗試存取（若對方哪天放寬，就會自動開始運作）
2. 明確辨識「被擋下」與「版面看不懂」——絕不把被擋誤判成「今天沒有 deal」
3. 一旦真的通過挑戰，把 HTML 存到 debug/ 供撰寫解析器之用

解析器尚未實作，因為我們從未取得過真實的結果頁——
憑空猜 DOM 結構寫出來的解析器是無法驗證的假程式碼。
"""

from __future__ import annotations

import logging
from datetime import date

from .. import config
from ..models import Deal
from .base import BlockedError, ParseError

log = logging.getLogger(__name__)

SOURCE = "cruisedirect"

# 挑戰頁的特徵：標題與 DOM 標記各自都足以判定
_BLOCKED_TITLES = ("just a moment", "attention required", "access denied")
_BLOCKED_MARKERS = (
  "challenges.cloudflare.com",
  "cf-chl",
  "_cf_chl_opt",
  "Performing security verification",
)


def is_blocked(html: str, title: str = "") -> bool:
  """判斷這一頁是不是機器人防護的攔截頁。"""
  if any(marker in (title or "").lower() for marker in _BLOCKED_TITLES):
    return True
  return any(marker in (html or "") for marker in _BLOCKED_MARKERS)


def parse_search_page(html: str, title: str = "") -> list[Deal]:
  """解析結果頁。

  目前一定會拋例外，但兩種例外的意義不同，呼叫端與網頁上會顯示不同訊息。
  """
  if is_blocked(html, title):
    raise BlockedError(
      "被 Cloudflare 機器人防護擋下（挑戰頁未解除）。"
      "此站目前無法自動擷取，其他來源不受影響。"
    )

  raise ParseError(
    "取得了非挑戰頁的內容，但尚未實作解析器——"
    "這代表封鎖可能已解除。HTML 已存到 debug/cruisedirect.html，"
    "請依該檔的實際結構補上解析邏輯。"
  )


def scrape(
  start: date | None = None,
  lookahead_days: int = config.LOOKAHEAD_DAYS,
  headless: bool = True,
  timeout_s: int = 25,
) -> list[Deal]:
  """嘗試擷取 cruisedirect。

  目前預期會拋 BlockedError。逾時刻意設短（25 秒），
  因為每天為一個已知被擋的站空等 60 秒沒有意義。
  """
  from patchright.sync_api import sync_playwright

  with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=headless)
    try:
      page = browser.new_page(
        viewport={"width": 1400, "height": 950}, locale="en-US"
      )
      page.goto(config.CRUISEDIRECT_URL, wait_until="domcontentloaded", timeout=60_000)

      # 給 Cloudflare 的 managed challenge 一點自動解除的時間
      for _ in range(timeout_s // 2):
        page.wait_for_timeout(2_000)
        if not is_blocked("", page.title()):
          break

      html = page.content()
      title = page.title()

      if not is_blocked(html, title):
        # 封鎖解除了——把現場存下來，好據以補上解析器
        config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = config.DEBUG_DIR / "cruisedirect.html"
        debug_path.write_text(html, encoding="utf-8")
        page.screenshot(path=str(config.DEBUG_DIR / "cruisedirect.png"), full_page=True)
        log.warning("cruisedirect 似乎已可存取，HTML 已存到 %s", debug_path)

      return parse_search_page(html, title)
    finally:
      browser.close()
