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
#
# **順序有意義，Yokohama 必須排在 Tokyo 前面。**
# asiayo 把兩個港併成一個，寫法是「東京（東京/橫濱）」；
# 行程第一天寫「日本 東京 (橫濱) 登船」的其實是橫濱出發（外國站也都寫 Yokohama）。
# 若 Tokyo 先比對就會把這些航次誤判成東京。
TARGET_PORTS: dict[str, tuple[str, ...]] = {
  "Keelung": ("keelung", "chilung", "jilong", "基隆"),
  "Yokohama": ("yokohama", "橫濱", "横浜"),
  "Tokyo": ("tokyo", "東京"),
}

# 去重時視為同一個港的分組。
# 東京與橫濱在郵輪業是同一個母港區，同一班船各站寫法不一
# （icruise 寫 Yokohama、cruisedirect 寫 Tokyo、asiayo 兩個一起寫），
# 分開看會讓同一航次無法跨來源合併比價。
PORT_GROUPS: dict[str, str] = {
  "Tokyo": "tokyo/yokohama",
  "Yokohama": "tokyo/yokohama",
}

# 中文船名 -> 各外國站使用的英文正式船名。
# 台灣站（asiayo／百威）只給中文，不對照就無法與外國站合併比價。
# 比對方式是對整段商品名稱做「最長子字串」比對，因為各站的寫法差異很大：
#   【麗星郵輪探索星號】…  /  【公主遊輪】鑽石公主號～…
#   【MSC郵輪．榮耀號】…   /  【名人遊輪千禧號】CELEBRITY MILLENNIUM～…
# 對不到的船名會照原樣輸出並記警告（見 normalize.canonical_ship）。
SHIP_ALIASES: dict[str, str] = {
  # 星夢／麗星
  "探索星號": "Star Voyager",
  "雲頂夢號": "Genting Dream",
  "世界夢號": "World Dream",
  # 公主遊輪
  "鑽石公主號": "Diamond Princess",
  "藍寶石公主號": "Sapphire Princess",
  "太陽公主號": "Sun Princess",
  "星辰公主號": "Star Princess",
  "皇家公主號": "Royal Princess",
  "珊瑚公主號": "Coral Princess",
  "島嶼公主號": "Island Princess",
  "紅寶石公主號": "Ruby Princess",
  "翡翠公主號": "Emerald Princess",
  # 名人遊輪
  "千禧號": "Celebrity Millennium",
  "日蝕號": "Celebrity Eclipse",
  "無限號": "Celebrity Infinity",
  "頂峰號": "Celebrity Summit",
  "星宿號": "Celebrity Constellation",
  "極致號": "Celebrity Edge",
  # 歌詩達
  "莎倫娜號": "Costa Serena",
  "賽琳娜號": "Costa Serena",
  "幸運號": "Costa Fortuna",
  "大西洋號": "Costa Atlantica",
  # MSC
  "榮耀號": "MSC Bellissima",
  "華麗號": "MSC Splendida",
  "珍愛號": "MSC Preziosa",
  # 皇家加勒比
  "海洋光譜號": "Spectrum of the Seas",
  "海洋聖歌號": "Ovation of the Seas",
  "海洋量子號": "Quantum of the Seas",
  "海洋航行者號": "Voyager of the Seas",
  "海洋贊禮號": "Anthem of the Seas",
  "海洋水手號": "Mariner of the Seas",
  "海洋光輝號": "Radiance of the Seas",
  "海洋璀璨號": "Brilliance of the Seas",
  # 挪威
  "喜悅號": "Norwegian Joy",
  "暢意號": "Norwegian Spirit",
  # 日系
  "富士號": "Mitsui Ocean Fuji",
  "飛鳥Ⅲ": "Asuka III",
  "飛鳥三號": "Asuka III",
  "日本丸": "Nippon Maru",
  "太平洋世界號": "Pacific World",
}

# 中文船公司 -> 英文名稱。
# 英文寫法刻意對齊 scrapers/cruisedirect.py 的 CRUISELINE_NAMES，
# 否則網頁的「船公司」篩選會出現同一家公司兩個選項。
CRUISE_LINE_ALIASES: dict[str, str] = {
  "麗星郵輪": "Star Cruises",
  "麗星遊輪": "Star Cruises",
  "星夢郵輪": "StarDream Cruises",
  "公主郵輪": "Princess Cruises",
  "公主遊輪": "Princess Cruises",
  "名人郵輪": "Celebrity Cruises",
  "名人遊輪": "Celebrity Cruises",
  "MSC郵輪": "MSC Cruises",
  "MSC遊輪": "MSC Cruises",
  "歌詩達": "Costa Cruises",
  "皇家加勒比": "Royal Caribbean International",
  "挪威郵輪": "Norwegian Cruise Line",
  "諾唯真": "Norwegian Cruise Line",
  "維京遊輪": "Viking",
  "維京郵輪": "Viking",
  "三井海洋郵輪": "Mitsui Ocean Cruises",
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

# asiayo（Next.js App Router，伺服器渲染，httpx 直接可用）
# 港口代碼 -> 我們的正規化港名。該站只有 KEE／KHH／SIN／TYO 四個郵輪母港，
# 沒有獨立的橫濱（TYO 寫成「東京（東京/橫濱）」，實際是哪個港要看行程第一天）。
ASIAYO_BASE = "https://asiayo.com"
ASIAYO_LIST_PATH = "/zh-tw/cruise/list/port/{port_id}/route/all/"
ASIAYO_ITEM_URL = "https://asiayo.com/zh-tw/cruise/item/{item_id}/?activityStartDate={date}"
ASIAYO_PORT_IDS: dict[str, str] = {"KEE": "Keelung", "TYO": "Tokyo"}

# 百威旅遊（頁面本身不含商品，資料走 SSE API）
# 5 是「遊輪．河輪」這個 shop 的 sn，涵蓋全站郵輪商品。
BWT_SHOP_SN = 5
BWT_SSE_URL = (
  "https://ncapi.bwt.com.tw/Shop/Present/GetMainGroupInfoByWebSiteSSE/{shop_sn}"
)
BWT_TOUR_URL = "https://www.bwt.com.tw/Tour/{group_code}"
# 只收這個出發地的商品：純郵輪、價格是每人船票，可與外國站直接比價。
# 桃園國際機場出發的是「機票＋郵輪」套裝，價格含機票，比了會失真。
BWT_DEPARTURE = "基隆港"

# 匯率來源（皆免金鑰）。台銀 rate.bot.com.tw 已上機器人挑戰頁，CI 不可用。
FX_PRIMARY_URL = "https://open.er-api.com/v6/latest/USD"
FX_FALLBACK_URL = "https://tw.rter.info/capi.php"

# 禮貌延遲（秒），避免對來源站造成負擔並降低被封機率
REQUEST_DELAY_S = 1.5

USER_AGENT = (
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
  "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# 所有可用來源名稱（CLI --sources 用）
ALL_SOURCES = ("icruise", "expedia", "cruisedirect", "asiayo", "bwt")
