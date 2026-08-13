# 郵輪 Last Minute Deals 自動擷取

每天自動擷取**基隆、東京、橫濱**出發、**未來一個月內**的郵輪航次，
整理成統一表格並產生可瀏覽的網頁。

| 輸出 | 位置 | 用途 |
|---|---|---|
| 表格網頁 | `docs/index.html`（GitHub Pages） | 日常查看，可排序／篩選／搜尋 |
| CSV | `data/deals.csv` | 用 Excel 開（UTF-8 BOM，不會亂碼） |
| JSON | `data/deals.json` | 程式讀取，含各來源執行狀態 |
| 每日快照 | `data/history/YYYY-MM-DD.json` | 比對價格變化 |

資料每天 commit 回 repo，所以用 `git log -p data/deals.csv` 就能看出
哪些航次是新開的、哪一班降價了——不需要額外的資料庫。

## 資料來源現況

| 來源 | 狀態 | 說明 |
|---|---|---|
| **icruise.com** | ✅ 正常 | Server-rendered HTML，`httpx` + `selectolax` 直接解析 |
| **expediacruises.com** | ✅ 正常 | Odysseus Swift API，用瀏覽器取得授權標頭後呼叫 JSON API |
| **cruisedirect.com** | ✅ 正常 | Cloudflare 保護，用 SeleniumBase CDP Mode 通過 |

### cruisedirect 的存取方式

該站以 Cloudflare 阻擋一般自動化存取。實測（2026-08-13）：

| 方式 | 結果 |
|---|---|
| `httpx` 直接請求 | ❌ 403，`Cf-Mitigated: challenge` |
| 換瀏覽器 User-Agent | ❌ 403 |
| patchright 無頭 | ❌ 挑戰頁 30 秒未解除 |
| patchright 有頭 | ❌ 同樣未解除 |
| **SeleniumBase CDP Mode** | ✅ **首次嘗試即通過** |

因此這一站用 `seleniumbase` 的 `sb.activate_cdp_mode()`。
在 Linux（GitHub Actions）需要 `xvfb` 提供虛擬顯示，因為 UC 模式在無頭下過不了挑戰。

**要用 `/search-results` 而不是 `/cruises/last-minute-cruises`。**
後者是策展子集合，其 facet 清單裡查不到基隆，會整個港口漏掉。
日期改由我們自己用 Unix 時間戳篩，不依賴對方對「last minute」的定義（他們設 3 個月）。

#### 資料中心 IP 的問題與解法

同一份程式碼在不同網路環境下行為不同（實測）：

| 環境 | Cloudflare 反應 |
|---|---|
| 家用住宅 IP | 自動放行，不出現任何互動 |
| GitHub Actions（Azure 資料中心 IP） | 升級成「Verify you are human」勾選框，**程式點了也不通過** |

這是 IP 信譽評分造成的，不是程式寫法問題。解法是讓流量從住宅 IP 出去：
在 GitHub Actions 裡透過 SSH 連到家用路由器開一條 SOCKS5 通道。

只有 cruisedirect 走這條通道（`CRUISEDIRECT_PROXY` 環境變數），
icruise 與 expedia 照舊直連，不佔用家用頻寬。
沒設定 `ROUTER_*` secrets 時整個步驟會跳過，cruisedirect 直連並如常降級。

##### 路由器端設定（Entware）

```sh
# 1. 安裝 OpenSSH server（比內建 Dropbear 好，支援完整的金鑰限制語法）
opkg install openssh-server

# 2. 建立專用使用者（不要用 root 開通道）
adduser -D tunnel

# 3. 停用密碼登入，只允許金鑰
#    編輯 /opt/etc/ssh/sshd_config：
#      PasswordAuthentication no
#      PermitRootLogin no
#      Port 22022            # 換掉預設埠可大幅減少掃描
#      AllowUsers tunnel
```

在**你自己的電腦**上產生專用金鑰（私鑰不要離開你的機器以外的地方，
只有公鑰放路由器、私鑰放 GitHub Secrets）：

```powershell
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\cruise_tunnel -C "cruise-deals-ci" -N '""'
```

把**公鑰**加到路由器的 `/home/tunnel/.ssh/authorized_keys`，並限制這把金鑰只能開通道：

```
restrict,port-forwarding ssh-ed25519 AAAA...你的公鑰... cruise-deals-ci
```

`restrict` 會關掉 shell、pty、agent forwarding、X11 forwarding，
只留下 `port-forwarding`——這把金鑰即使外洩也開不了 shell。

最後在路由器上做 DDNS 與埠轉發（你已有 DDNS），確認從外部連得進來。

##### GitHub Secrets

| Secret | 內容 | 必要 |
|---|---|---|
| `ROUTER_HOST` | 你的 DDNS 網域 | ✅ |
| `ROUTER_SSH_USER` | `tunnel` | ✅ |
| `ROUTER_SSH_KEY` | **私鑰**全文（`cruise_tunnel` 檔案內容） | ✅ |
| `ROUTER_SSH_PORT` | 例如 `22022`（未設則用 22） | 選用 |
| `ROUTER_KNOWN_HOSTS` | `ssh-keyscan -p 22022 你的DDNS` 的輸出 | 建議 |

沒有 `ROUTER_KNOWN_HOSTS` 時會退回 TOFU 模式並發出警告——
補上它才能防中間人攻擊。

> ⚠️ 把 SSH 開到公網有風險。務必：關閉密碼登入、換非預設埠、
> 用專用非 root 帳號、金鑰加 `restrict,port-forwarding` 限制。

## 這個系統怎麼避免「安靜地壞掉」

爬蟲最危險的失效不是崩潰，而是**安靜地回傳空清單**，讓你以為今天真的沒有 deal。
因此有三道防線：

1. **解析器健全性檢查** — icruise 頁面若宣稱有 N 筆結果卻解析出 0 筆，直接拋錯，
   而不是回傳空清單。
2. **失敗不覆蓋好資料** — 某來源擷取失敗時，沿用它上一次的資料並標記 `stale_since`，
   網頁上會明確顯示「這是 X 日抓的資料」。
3. **狀態全都攤在明處** — `deals.json` 的 `run_report` 與網頁頂端的狀態橫幅
   都會列出每個來源的成功／失敗與原因。

## 本機使用

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev,browser]"
patchright install chromium       # Expedia 用
# cruisedirect 用 SeleniumBase，會自動下載 uc_driver；Linux 另需 apt install xvfb

python -m cruise_deals                              # 全部來源
python -m cruise_deals --sources icruise            # 只跑輕量來源（最快）
python -m cruise_deals --dry-run                    # 不寫檔，只印表格
python -m cruise_deals --sources expedia --headed   # 有頭模式觀察瀏覽器
python -m cruise_deals --lookahead-days 60          # 改成看兩個月
```

離開碼：**所有**來源都失敗時為 1，否則為 0（部分失敗仍算成功）。

## 測試

```bash
pytest -q          # 220 個測試，約 2 秒
```

測試全部跑在存下來的**真實**回應上（`tests/fixtures/`），不需要網路。
瀏覽器測試（`test_page_browser.py`）在真實 Chromium 裡驗證排序／篩選／搜尋／手機版面，
沒安裝 patchright 時會自動跳過。

## 部署到 GitHub Actions

1. 把這個目錄推到一個 GitHub repo
2. **Settings → Pages** → Source 選 `main` 分支的 `/docs` 目錄
3. **Settings → Actions → General** → Workflow permissions 選 **Read and write**
4. 到 Actions 頁籤手動觸發一次 `擷取郵輪 Last Minute Deals` 確認正常

排程為每天 UTC 21:00（台北 05:00）。

> ⚠️ GitHub 會在 repo 連續 60 天無提交活動時停用排程 workflow 並寄信通知。
> 本專案每天 commit 資料，正常情況不會觸發。

## 專案結構

```
src/cruise_deals/
├── config.py            # 目標港口、日期窗口、各站網址
├── models.py            # Deal 資料模型、去重鍵、排序
├── normalize.py         # 港口比對、日期／價格／天數解析（純函式）
├── scrapers/
│   ├── base.py          # ScrapeResult、ParseError／BlockedError、優雅降級
│   ├── icruise.py       # httpx + selectolax
│   ├── expedia.py       # patchright 取得授權標頭 + JSON API
│   └── cruisedirect.py  # 封鎖偵測（解析器待對方開放後補上）
├── outputs/
│   ├── tabular.py       # 合併邏輯、CSV／JSON、歷史快照
│   └── page.py          # 自足的 GitHub Pages 表格網頁
└── cli.py
```

## 實作上踩過的坑（都已處理）

這些是實際打過真實請求才發現的，記錄下來免得日後重踩：

- **icruise 每頁固定 25 筆**，且 `PageNo` / `strPage` / `page` / `CurrentPage` /
  `strResultsPerPage` 等分頁參數由 GET 傳入**全部無效**。
  解法：把日期窗口切成 5 天一段，讓每段結果自然低於上限。
- **icruise 的日期參數不能做 URL 編碼**。`08/13/2026` 被編成 `08%2F13%2F2026`
  時會**間歇性**回 404。解法：自行組 query string 保留字面斜線。
- **icruise 會間歇性回 404／逾時**，與參數無關。解法：三次遞增延遲重試。
- **Expedia 那個網址不回 JSON**，只是 12KB 的 SPA 空殼。真資料在
  `POST /nitroapi/v2/cruise`，需要 `uniquetid` 授權標頭（由頁面 JS 動態產生）。
  解法：用瀏覽器載入頁面、攔下 SPA 自己的請求標頭再沿用。
- **Expedia API 限制**（都是它自己回報的錯誤訊息）：`pageSize` 上限 50；
  `pageStart` 是頁碼不是筆數位移（`from = (pageStart-1) * pageSize`）；
  `sortColumn` 只接受 `departureDateTime`；`departureDate` 區間篩選無效，
  日期只能在本地過濾。
- **Expedia 的價格陣列混著稅金與港務費**（用 `code` 而非 `name` 標示，
  金額只有幾塊錢）。只認 Inside／Outside／Balcony／Suite 四種房型名稱，
  否則會把 4.35 元的稅金算成「最低價」。
- **網頁排序的 `dataset.sort || textContent` 是陷阱**：無報價時 `data-sort` 是
  空字串（falsy），會被誤退回讀「洽詢報價」文字，`parseFloat` 得到 NaN，
  導致「空值排最後」完全失效。這個 bug 是瀏覽器測試抓到的。
- **cruisedirect 基隆頁面的出發城市欄位是空的**（東京／橫濱的有值）。
  只靠該欄位判斷出發港會**安靜地漏掉整個港口**的航次。
  解法：欄位為空時改用停靠港第一站，並在「有卡片卻解析出 0 筆」時拋 ParseError。
- **cruisedirect 的港名含逗號**（"Tokyo, Japan"），停靠港要用 `" - "` 分隔，
  用逗號切會把國名切成獨立港口。
- **去重鍵不含船公司**：各站寫法差異太大（icruise `Celebrity Cruises`
  vs cruisedirect logo 只給 `celebrity`），納入會讓同一航次無法跨來源合併。
  船名在郵輪業是唯一的，加上出發日、夜數、出發港已足以識別。
