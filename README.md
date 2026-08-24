# 郵輪 Last Minute Deals 自動擷取

每天自動擷取**基隆、東京、橫濱**出發、**未來一個月內**的郵輪航次，
整理成統一表格並產生可瀏覽的網頁。

同一個航次同時出現在多個平台時會合併成一列並列出各家報價；
外國站報美元、台灣站報台幣，全部依當天匯率換算成**台幣**再比較。

| 輸出 | 位置 | 用途 |
|---|---|---|
| 表格網頁 | `docs/index.html`（GitHub Pages） | 日常查看，可排序／篩選／搜尋 |
| CSV | `data/deals.csv` | 用 Excel 開（UTF-8 BOM，不會亂碼） |
| JSON | `data/deals.json` | 程式讀取，含各來源執行狀態 |
| 每日快照 | `data/history/YYYY-MM-DD.json` | 比對價格變化 |

資料每天 commit 回 repo，所以用 `git log -p data/deals.csv` 就能看出
哪些航次是新開的、哪一班降價了——不需要額外的資料庫。

## 資料來源現況

| 來源 | 幣別 | 狀態 | 說明 |
|---|---|---|---|
| **icruise.com** | USD | ✅ 正常 | Server-rendered HTML，`httpx` + `selectolax` 直接解析 |
| **expediacruises.com** | USD | ✅ 正常 | Odysseus Swift API，用瀏覽器取得授權標頭後呼叫 JSON API |
| **cruisedirect.com** | USD | ✅ 正常 | Cloudflare 保護，用 SeleniumBase CDP Mode 通過 |
| **asiayo.com** | TWD | ✅ 正常 | Next.js 伺服器渲染，`httpx` 讀 RSC payload，不需瀏覽器 |
| **bwt.com.tw**（百威旅遊） | TWD | ✅ 正常 | SSE JSON API，`httpx` 直接串流，不需瀏覽器 |

> 百威旅遊的郵輪團期最早在**三個月後**，所以在預設的一個月窗口下它常態回 0 筆。
> 這是正常狀態，不是壞掉；想看得更遠可以加 `--lookahead-days 180`。

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
其餘來源照舊直連，不佔用家用頻寬。
沒設定 `ROUTER_*` secrets 時整個步驟會跳過，cruisedirect 直連並如常降級。

##### 路由器端設定

已在真實環境驗證通過：Netgear R7000 刷 **Asuswrt-Merlin** 韌體 + Entware
（Linux 4.19 armv7l，SSH server 為 **dropbear**）。

###### 1. 產生 CI 專用金鑰（在你自己的電腦上）

**不要沿用你平常登入用的那把**——這把要放進 GitHub secret，權限也要縮到最小：

```powershell
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\cruise_tunnel -C "cruise-deals-ci" -N '""'
```

會產生兩個檔案：`cruise_tunnel`（**私鑰**，只留在電腦與 GitHub secret）
與 `cruise_tunnel.pub`（**公鑰**，等一下放上路由器）。

###### 2. 把公鑰裝上路由器，而且要撐得過重開機

Merlin 的 `/` 是每次開機從韌體重建的 ramdisk，root 的家目錄
（`/root` → `/tmp/home/root`）躺在 tmpfs 上，**所以直接寫進
`~/.ssh/authorized_keys` 的公鑰一重開機就沒了**。持久的地方是 `/jffs`。

`scripts/merlin-authorized-keys.sh` 把這件事包起來：公鑰存進 `/jffs`，
再掛一支 `/jffs/scripts/services-start` 鉤子，開機後合併回 root 的
`authorized_keys`。

先確認 WebUI 的 **Administration → System → Enable JFFS custom scripts and
configs** 是開的（否則 `/jffs/scripts/*` 開機不會被執行），然後：

```powershell
# 把腳本送上路由器（不需要先 clone，直接把本機檔案灌過去）
ssh -p 2222 你的SSH使用者@你的DDNS網域 'cat > /tmp/merlin-authorized-keys.sh' `
  < scripts\merlin-authorized-keys.sh

# 安裝（公鑰不是機密，直接當參數貼上沒關係）
$pub = Get-Content -Raw $env:USERPROFILE\.ssh\cruise_tunnel.pub
ssh -p 2222 你的SSH使用者@你的DDNS網域 "sh /tmp/merlin-authorized-keys.sh install '$pub'"
```

它會自動補上 dropbear 的限制選項，存成這樣：

```
no-pty,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA...你的公鑰... cruise-deals-ci
```

> dropbear 不支援 OpenSSH 的 `restrict` 關鍵字，上面這組是 dropbear 認得的等效寫法：
> 禁止配置終端機、禁止 agent 與 X11 轉發，只留下建立通道所需的埠轉發能力。
> 這把金鑰即使外洩，也開不了互動 shell。

重開機後這樣驗收：

```bash
sh /jffs/scripts/cruise-authorized-keys.sh status
```

腳本的三個子命令：`install`（安裝並立即生效）、`apply`（手動重跑合併，
開機時會自動執行）、`status`（檢查現況）。它只接受公鑰——誤貼私鑰會被擋下來。
合併是逐行比對後補上，不會洗掉你從 WebUI 或 NVRAM 灌進去的其他金鑰。

> **更穩的做法**：Merlin 的 WebUI 有內建的 SSH 公鑰欄位
> （Administration → System → Authorized Keys），那是存在 NVRAM 裡的，
> 由韌體自己在開機時寫進 `authorized_keys`，比外掛鉤子更不容易被蓋掉。
> 已知的邊界情況：中途重啟 sshd（例如在 WebUI 改設定）時，韌體可能會
> 依 NVRAM 重寫 `authorized_keys`，把鉤子補上的那行沖掉，要到下次開機
> 或手動跑 `apply` 才會回來。兩邊都放最保險。

###### 3. 取得主機金鑰指紋（給下面的 `ROUTER_KNOWN_HOSTS` 用）

```powershell
ssh-keyscan -p 2222 你的DDNS網域
```

###### 4. GitHub Secrets

| Secret | 內容 | 必要 |
|---|---|---|
| `ROUTER_HOST` | 你的 DDNS 網域 | ✅ |
| `ROUTER_SSH_USER` | SSH 使用者名稱 | ✅ |
| `ROUTER_SSH_KEY` | **私鑰**全文 | ✅ |
| `ROUTER_SSH_PORT` | 非預設埠（未設則用 22） | 選用 |
| `ROUTER_KNOWN_HOSTS` | `ssh-keyscan` 的輸出 | 建議 |

設定方式（私鑰直接從檔案讀入，不會經過剪貼簿或終端機畫面）：

```powershell
gh secret set ROUTER_HOST        --body "你的DDNS網域"
gh secret set ROUTER_SSH_USER    --body "你的SSH使用者"
gh secret set ROUTER_SSH_PORT    --body "2222"
Get-Content -Raw $env:USERPROFILE\.ssh\cruise_tunnel | gh secret set ROUTER_SSH_KEY
ssh-keyscan -p 2222 你的DDNS網域 2>$null | gh secret set ROUTER_KNOWN_HOSTS
```

沒有 `ROUTER_KNOWN_HOSTS` 時會退回 TOFU 模式並發出警告——補上它才能防中間人攻擊。

> ⚠️ 把 SSH 開到公網有風險。務必：關閉密碼登入、換掉預設埠、
> 用專用金鑰並加上 `no-pty` 等限制。

### asiayo 的存取方式

伺服器渲染，`httpx` 直接抓即可。資料在 Next.js App Router 的 RSC flight
payload（`self.__next_f.push([1,"…"])`）裡，串接後可以切出乾淨的 JSON 物件。

**價格是「查詢區間內所有出發日的最低價」，不是逐日價格**——同一筆行程查
一個月的窗口顯示 18,000，把窗口縮到只含 8/23 那一天卻是 21,583。
所以要跟 icruise 一樣把窗口切成 5 天一段查詢，逐段的價格才對得上出發日。

⚠️ 不能用「一天一查」（`startDate == endDate`）來取得更精確的價格：
該站在這種情況下會忽略上界，回傳往後好幾個月的出發日。

另外，使用者從網站分享出來的網址會帶 `cruiseIds` / `companyIds` 篩選，
沿用會少抓資料，所以只帶日期與分頁。

### 百威旅遊的存取方式

`/destination/…` 頁面本身不含任何商品，資料由前端再打 API 取得。
可用的是 SSE 端點（一般的 JSON 端點只回骨架，價格全是 `99999999`）：

```
GET https://ncapi.bwt.com.tw/Shop/Present/GetMainGroupInfoByWebSiteSSE/5
Accept: text/event-stream
```

依序推 `step1`（主行程）、`step2`（團期與價格）、`step3`（完成標記）。
**沒收到 `step3` 就視為失敗**，否則會拿到不完整的資料卻以為「今天就這麼少」。

只收 `departure == "基隆港"` 且 `hasCruisePrice` 的商品：
桃園機場出發的是「機票＋郵輪」套裝，價格含機票，跟外國站的每人船票價不能比；
`hasCruisePrice` 則剛好濾掉「單訂船票」之類的渡輪商品。

#### 憑證的坑

該站憑證鏈缺 Subject Key Identifier，而 Python 3.13 起
`ssl.create_default_context()` 預設開啟 `VERIFY_X509_STRICT`，會直接拒絕連線：

```
CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier
```

解法是**只**清掉那個嚴格旗標，憑證鏈仍然完整驗證——不要退化成 `verify=False`：

```python
context = ssl.create_default_context(cafile=certifi.where())
context.verify_flags &= ~ssl.VERIFY_X509_STRICT
```

## 匯率與台幣比價

台灣站報台幣、外國站報美元，不換算就比大小的話 379 美元會被判定比
18,000 台幣便宜——**排序、最低價統計、跨來源比價一律以台幣為準**。
原始報價與所用匯率都保留在 `price` / `currency` / `fx_rate` 欄位裡。

匯率來源（皆免金鑰，主要來源失敗會自動換備援）：

| 順位 | 來源 |
|---|---|
| 1 | `https://open.er-api.com/v6/latest/USD` |
| 2 | `https://tw.rter.info/capi.php` |

> 沒有用台灣銀行牌告匯率：`rate.bot.com.tw` 已經上了機器人挑戰頁，
> `httpx` 拿到的是 `Challenge Validation` 的 HTML，在 GitHub Actions 上過不了。

兩個來源都失敗時會**沿用上一次的匯率**並標記為舊資料（網頁上會醒目顯示），
與「來源擷取失敗就沿用舊資料」同一個原則——沒有匯率會讓整張表失去台幣價，
比用昨天的匯率糟糕得多。除錯時可以用 `--fx-rate 31.97` 直接指定，不連網。

## 中文船名怎麼跟英文船名合併

台灣站給的是「鑽石公主號」，外國站給的是 `Diamond Princess`，
不轉換就永遠是兩列、比不了價。對照表在 `config.SHIP_ALIASES`
（船公司在 `config.CRUISE_LINE_ALIASES`），比對方式是對整段商品名稱做
**最長子字串比對**——各站把船名塞進標題的寫法差太多，逐一寫 parser 會很脆：

```
【麗星郵輪探索星號】…                     船公司與船名黏在一起
【MSC郵輪．榮耀號】…                      中間有分隔符號
【公主遊輪】鑽石公主號～…                  括號裡只有船公司
【名人遊輪千禧號】CELEBRITY MILLENNIUM～…  中英並陳
```

對照表上沒有的新船名會**照原樣輸出並記警告**（終端機、`run_report.warnings`
與網頁上都看得到），不會讓整個來源失敗——但那一列不會跟外國站合併，
看到警告就去補 `SHIP_ALIASES`。

另外，**東京與橫濱在去重時視為同一個港**（`config.PORT_GROUPS`）。
同一班船各站寫法不一（icruise 寫 Yokohama、cruisedirect 寫 Tokyo、
asiayo 兩個一起寫成「東京（東京/橫濱）」），分開看會讓同一航次合併不起來。

## 這個系統怎麼避免「安靜地壞掉」

爬蟲最危險的失效不是崩潰，而是**安靜地回傳空清單**，讓你以為今天真的沒有 deal。
因此有三道防線：

1. **解析器健全性檢查** — 頁面若宣稱有 N 筆結果卻解析出 0 筆，直接拋錯，
   而不是回傳空清單（icruise、cruisedirect、asiayo 都有這一關；
   百威則是檢查 SSE 有沒有收到 `step3`）。
2. **失敗不覆蓋好資料** — 某來源擷取失敗時，沿用它上一次的資料並標記 `stale_since`，
   網頁上會明確顯示「這是 X 日抓的資料」。匯率抓不到時同樣沿用上一次的。
3. **狀態全都攤在明處** — `deals.json` 的 `run_report` 與網頁頂端的狀態橫幅
   都會列出每個來源的成功／失敗與原因，對不到英文名的船名也會列成警告。

## 本機使用

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev,browser]"
patchright install chromium       # Expedia 用
# cruisedirect 用 SeleniumBase，會自動下載 uc_driver；Linux 另需 apt install xvfb

python -m cruise_deals                                   # 全部來源
python -m cruise_deals --sources icruise,asiayo,bwt      # 只跑免瀏覽器的來源（最快）
python -m cruise_deals --dry-run                         # 不寫檔，只印表格
python -m cruise_deals --sources expedia --headed        # 有頭模式觀察瀏覽器
python -m cruise_deals --lookahead-days 60               # 改成看兩個月
python -m cruise_deals --fx-rate 31.97                   # 指定匯率，不連網查
```

`icruise`、`asiayo`、`bwt` 都是純 `httpx`，不需要 Chromium 也不需要 xvfb。

離開碼：**所有**來源都失敗時為 1，否則為 0（部分失敗仍算成功）。

## 測試

```bash
pytest -q          # 360 個測試，約 2 秒
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
├── config.py            # 目標港口、日期窗口、各站網址、中英船名對照表
├── models.py            # Deal 資料模型、跨語言去重鍵、以台幣排序
├── normalize.py         # 港口比對、船名對照、日期／價格／天數解析（純函式）
├── fx.py                # USD→TWD 匯率取得與換算
├── scrapers/
│   ├── base.py          # ScrapeResult、ParseError／BlockedError、優雅降級
│   ├── icruise.py       # httpx + selectolax
│   ├── expedia.py       # patchright 取得授權標頭 + JSON API
│   ├── cruisedirect.py  # SeleniumBase CDP Mode 穿過 Cloudflare
│   ├── asiayo.py        # httpx + Next.js RSC payload
│   └── bwt.py           # httpx + SSE JSON API
├── outputs/
│   ├── tabular.py       # 合併邏輯、台幣換算、CSV／JSON、歷史快照
│   └── page.py          # 自足的 GitHub Pages 表格網頁
└── cli.py

scripts/
└── merlin-authorized-keys.sh   # 路由器端：讓 CI 專用公鑰撐過重開機（Asuswrt-Merlin）
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
- **asiayo 的價格是「查詢區間內最低價」**，不是逐日價。不切段查詢的話，
  9/13 那班的低價會被套到 8/23 那班上，比價就是錯的。
- **asiayo 的 `startDate == endDate` 會忽略上界**，回傳往後好幾個月的出發日。
  想用單日查詢取得精確價格是行不通的。
- **asiayo 的 TYO 涵蓋東京與橫濱兩個港**，要看行程第一天才分得出來
  （橫濱出發寫「日本 東京 (橫濱) 登船」，東京出發只寫「日本東京出發」）。
  所以 `TARGET_PORTS` 裡 Yokohama 必須排在 Tokyo 前面。
- **百威的憑證缺 Subject Key Identifier**，Python 3.13 預設的
  `VERIFY_X509_STRICT` 會擋下來。只清那個旗標，不要用 `verify=False`。
- **百威 30 天內常態 0 筆**是正常的（團期最早在三個月後），
  所以它過濾後沒有結果時不拋錯——但 SSE 沒收到 `step3` 就一定要拋。
- **跨幣別一定要換算後才能比**：`_price_rank` 若比 `price` 而不是 `price_twd`，
  379 USD 會勝過 18,000 TWD，整個比價與排序都會反過來。
- **Windows 終端機預設 cp950**，印 `✓` 會拋 `UnicodeEncodeError` 讓程式在
  最後一步掛掉。CLI 啟動時會把 stdout／stderr 切成 UTF-8。
