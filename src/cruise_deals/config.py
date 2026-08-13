"""設定常數：目標港口、日期窗口、各來源網址。"""

from __future__ import annotations

from pathlib import Path

# 專案根目錄（本檔位於 <root>/src/cruise_deals/config.py）
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
DOCS_DIR = ROOT / "docs"
DEBUG_DIR = ROOT / "debug"

# 出發日期窗口：今天起算一個月內
LOOKAHEAD_DAYS = 30

# icruise 每頁固定 25 筆且分頁參數無法由 GET 控制（已實測），
# 因此把日期窗口切成小段查詢，讓每段結果自然低於上限。
CHUNK_DAYS = 5

# 目標出發港：正規化名稱 -> 用於比對的小寫關鍵字
# 各站對同一港口的寫法不同（例：icruise 寫 "Keelung (Taipei), Taiwan"），
# 故採小寫子字串比對而非完全相等。
TARGET_PORTS: dict[str, tuple[str, ...]] = {
  "Keelung": ("keelung", "chilung", "jilong"),
  "Tokyo": ("tokyo",),
  "Yokohama": ("yokohama",),
}

# icruise 搜尋參數
ICRUISE_SEARCH_URL = "https://www.icruise.com/c/src.php"
ICRUISE_BASE = "https://www.icruise.com"
ICRUISE_DESTINATION_ASIA = 7  # WMPHDestinationCodeSub=7 為亞洲
ICRUISE_VACATION_TYPE = 1

# cruisedirect（Cloudflare 保護，需真實瀏覽器）
CRUISEDIRECT_URL = "https://www.cruisedirect.com/cruises/last-minute-cruises"

# expediacruises（Odysseus Swift SPA，需真實瀏覽器讓其自行通過 reCAPTCHA）
EXPEDIA_URL = (
  "https://bookus.expediacruises.com/swift/cruise"
  "?siid=1095905&lang=1&destinations=19"
)

# 禮貌延遲（秒），避免對來源站造成負擔並降低被封機率
REQUEST_DELAY_S = 1.5

USER_AGENT = (
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
  "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# 所有可用來源名稱（CLI --sources 用）
ALL_SOURCES = ("icruise", "expedia", "cruisedirect")
