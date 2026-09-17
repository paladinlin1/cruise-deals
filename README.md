# 郵輪 Last Minute Deals 自動擷取

每天自動擷取**基隆、東京、橫濱**出發、**未來一個月內**的郵輪航次，
整理成統一表格並產生可瀏覽的網頁。

同一個航次同時出現在多個平台時會合併成一列並列出各家報價；
外國站報美元、台灣站報台幣，全部依當天匯率換算成**台幣**再比較。

| 輸出 | 位置 | 用途 |
|---|---|---|
| 表格網頁 | `docs/index.html`（GitHub Pages） | 日常查看，可排序／篩選／搜尋；滑鼠停在列上會顯示停靠港、航程、價格說明等 |
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
| **liontravel.com**（雄獅旅遊） | TWD | ✅ 正常 | 搜尋 JSON API，`httpx` 直接 POST，不需 cookie 也不需瀏覽器 |
| **eztravel.com.tw**（易遊網） | TWD | ✅ 正常 | Incapsula 保護，patchright 載入第一頁後其餘請求走 `page.request`，讀 `__NEXT_DATA__` |

> 百威旅遊的郵輪團期最早在**三個月後**，所以在預設的一個月窗口下它常態回 0 筆。
> 雄獅則是**一個月內的基隆／東京團期幾乎都「暫時額滿」**，額滿的不收，所以也常只有零星幾筆。
> 這些都是正常狀態，不是壞掉；想看得更遠可以加 `--lookahead-days 180`。

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

只有 cruisedirect（`CRUISEDIRECT_PROXY`）、易遊網（`EZTRAVEL_PROXY`）與
icruise（`ICRUISE_PROXY`）走這條通道，其餘來源照舊直連，不佔用家用頻寬。
沒設定 `ROUTER_*` secrets 時整個步驟會跳過，三者直連並如常降級。

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
payload（`self.__next_f.push([1,"…"])`）裡，串接後可以切出 JSON 物件。

但**不是每個欄位都是本體**：React Flight 會把同一份 payload 裡重複出現的物件
去重成 `"$5f:props:events:1:properties:bnbs:0:port"` 這種參照字串
（`$列號:路徑`），哪一份是本體取決於元件輸出順序——8/17 是卡片先出、GA 追蹤
事件放參照，9/16 對調過來，卡片上的 `port`／`journey`／`availableDates`
全變成參照。所以抽出物件後要先解參照，兩種順序才都吃得下。

解參照時列的切法要照 React Flight 的框架走，**不能用「每行一列」**：
`T` 文字列是 `T<十六進位位元組數>,<原文>`，靠長度收尾、原文可含換行、
結尾沒有換行，下一列會直接黏在原文後面（列 62 就黏在「…住宿稅」後面）。

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

### 雄獅旅遊的存取方式

搜尋頁是 React SPA，商品清單由前端打這支 API 取得，實測**不需要 cookie 或授權標頭**：

```
POST https://travel.liontravel.com/search/grouplistinfojson
Content-Type: application/json
{"TripTypes": "01", "GoDatestart": "2026-09-16", "GoDateEnd": "2026-10-16",
 "Page": 1, "PageSize": 100, … 其餘欄位照網站原樣送（多為 null）}
```

`TripTypes: "01"` 是「交通型態＝郵輪」（`02` 巴士、`04` 航空），
比關鍵字搜尋可靠——關鍵字「郵輪」反而回 0 筆。
**查詢 body 要照網站原樣送完整欄位**，只送幾個欄位時同樣的條件會回 0 筆。

回應是 `NormGroupList[]`（商品）底下掛 `GroupList[]`（團期），
每個團期都有自己的 `GoDate` 與 `StraightLowestPrice`（直客價），
所以不必像 asiayo 那樣把日期窗口切段查。

出發港藏在商品名稱裡，只認三種寫法：`基隆出發`、`東京上下`（原港來回）、
`東京上首爾下`（單程）。**不能整段字串比對**——「神戶上基隆下」含「基隆」但是神戶出發。
回應裡的 `StartFromCityList`／`IsCruise` 欄位不能用：前者是集合城市不是登船港
（東京上下的鑽石公主號標「台北」），後者全站都是 `False`。
標題另有「台北出發」之類非目標港「X出發」的是機＋船套裝，不收。

東京出發的商品是**純船票**（詳情頁寫明「不含國際段機票」「售價兩人一室每人艙房費用」），
可以跟外國站的 Tokyo 航次直接比價。

團期狀態 `full`（暫時額滿）**不收**：額滿的價格拿去跨來源比價會誤導最低價。
實測（2026-09-16）一個月內全站 76 個團期有 72 個額滿，基隆／東京出發的 26 個**全部**額滿，
所以這一站在預設窗口下常態只有零星幾筆。

同一航次會有兩個供應商——雄獅自家（`TourSource=Lion`，探索星號 3 日 12,300）與
「【主題旅遊】」合作商品（`GoUni`，同航次 8,000）。來源內去重只留最便宜的，
與百威「同航次多個 groupCode 留最便宜」同一原則，但兩者的價格定義可能不同
（艙等、幾人一室），看到雄獅的價格特別低時要點進 `detail_url` 確認。

### 易遊網的存取方式

Next.js 網站，資料在 `<script id="__NEXT_DATA__">` 裡，是台灣站裡結構最乾淨的
（出發港、停靠城市、天數、逐艙等價格都是欄位，不用猜標題）。但整站有 **Incapsula**：
`httpx` 直接請求只會拿到 212 bytes 的挑戰頁。實測（2026-09-17）：

| 方式 | 結果 |
|---|---|
| `httpx` 直接請求 | ❌ 挑戰頁（`_Incapsula_Resource`） |
| patchright 無頭載入第一頁 | ✅ 2～12 秒後拿到 `__NEXT_DATA__` |
| 之後改用 `page.request.get()` | ✅ 共用 cookie，每次 0.2 秒，不必渲染 |

所以瀏覽器只用來載入第一頁，之後的列表與商品頁全走 `page.request`（同 expedia 的做法）。

⚠️ **正常頁面也會嵌 `/_Incapsula_Resource?…` 監控腳本**，不能拿「HTML 含這個字串」
當被擋的判斷——要先找 `__NEXT_DATA__`，找不到才看是不是挑戰頁。

列表網址 `/pkgfrn/results/KEE/{航線代碼}?depDateFrom=…&depDateTo=…&pageSize=100`。
日期參數有效，但站方會把 `depDateFrom` 往後推到約今天＋2（要 0917 回 0919），
最近兩天的出發日本來就不會出現。**預設一頁只回 12 筆而且 `page` 參數無效**，
`pageSize` 才有效，所以一次要完；回應的 `pageConfig.total` 仍比拿到的多就視為截斷、拋錯。
**沒有「全部航線」的查詢**（父代碼 331 會回「系統升級中」），要逐個亞洲葉節點航線查
（`config.EZTRAVEL_ROUTE_CODES`：沖繩、九州、韓國、日本環遊、日韓、亞洲多國、海上巡遊）。

**列表的 `minPrice1` 不能直接用**：那是「所有艙等 × 佔床人數」的最低價，
實際是 3／4 人房的每人價（探索星號 3 日 6,325），其他來源都是 2 人一室。
所以每個出發日都再進商品頁 `/pkgfrn/introduction/{prodNo}/{saleDt}`，
從 `pfProPrice4Introductions` 取「雙人房 × 成人」各艙等的最低價（同一航次 8,000，
與雄獅對得上）；商品頁拿不到時退回列表價並在 `price_note` 註明。
但**商品頁若回的是 Incapsula 挑戰頁就整個來源失敗**——那代表 cookie 已失效，
若安靜退回列表價會讓整站報價系統性偏低卻回報成功。

**列表的 `tourCitysNm` 是商品群組的標籤，不是該航次的行程**（富士號 5 天的商品標
「與那國島、石垣島、沖繩」，實際只停那霸），停靠港與到達港要用商品頁的 `routeInfo.routes`。

出發地分類 `departArea` 只有基隆港／高雄港／桃園機場（機＋船）／海外登船，
東京／橫濱出發的藏在「海外登船」裡且登船港要從標題猜，目前只收基隆。
但**「蘇澳出發，基隆返回」也被歸在基隆港分類下**，標題有「(X出發，Y返回)」時
要再確認 X 是基隆。`fullStatus == "END"`（關團）的出發日不收，與雄獅的額滿同一原則。

**GitHub Actions 的資料中心 IP 過得了第一頁，第二個 `page.request` 就被掛斷**
（`socket hang up`，實測 2026-09-17；住宅 IP 完全正常）。所以比照 cruisedirect，
`EZTRAVEL_PROXY` 有設定時整個瀏覽器走家用路由器的 SOCKS5 通道。

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
   百威則是檢查 SSE 有沒有收到 `step3`）。反過來說，**頁面自己宣稱 0 筆就照實回空清單**
   ——「今天真的沒船」與「對方改版了」必須分得開，否則前者會被誤報成後者。
2. **失敗不覆蓋好資料** — 某來源擷取失敗時，沿用它上一次的資料並標記 `stale_since`，
   網頁上會明確顯示「這是 X 日抓的資料」。匯率抓不到時同樣沿用上一次的。
3. **狀態全都攤在明處** — `deals.json` 的 `run_report` 與網頁頂端的狀態橫幅
   都會列出每個來源的成功／失敗與原因，對不到英文名的船名也會列成警告。

## 本機使用

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev,browser]"
patchright install chromium       # Expedia、易遊網 用
# cruisedirect 用 SeleniumBase，會自動下載 uc_driver；Linux 另需 apt install xvfb

python -m cruise_deals                                   # 全部來源
python -m cruise_deals --sources icruise,asiayo,bwt,lion # 只跑免瀏覽器的來源（最快）
python -m cruise_deals --dry-run                         # 不寫檔，只印表格
python -m cruise_deals --sources expedia,eztravel --headed # 有頭模式觀察瀏覽器
python -m cruise_deals --lookahead-days 60               # 改成看兩個月
python -m cruise_deals --fx-rate 31.97                   # 指定匯率，不連網查
```

`icruise`、`asiayo`、`bwt`、`lion` 都是純 `httpx`，不需要 Chromium 也不需要 xvfb。

離開碼：**所有**來源都失敗時為 1，否則為 0（部分失敗仍算成功）。

## 測試

```bash
pytest -q          # 406 個測試，約 5 秒
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
│   ├── bwt.py           # httpx + SSE JSON API
│   ├── lion.py          # httpx + 搜尋 JSON API
│   └── eztravel.py      # patchright 過 Incapsula + page.request 讀 __NEXT_DATA__
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
- **icruise 在 GitHub Actions 上會間歇性拿到 HTTP 200 的自家錯誤頁**
  （「Oh no! There seems to be a problem… creating your account」，2026-09-11 起
  隔三差五 0 筆，本機同一時間 30 筆）。頁面要分三種：有結果表、真正的
  「No results found」、兩者都不是——第三種以前被當成 0 筆，把前一天的資料整批
  洗掉。現在第三種先重試，重試用完才拋 ParseError（沿用前次資料）並把現場存進
  `debug/` 供 artifact 診斷。實測（2026-09-17）CI 上三次都拿到同一頁、本機正常，
  是對方拒絕資料中心 IP 而不是暫時性錯誤，所以 icruise 也走 `ICRUISE_PROXY` 的通道
  （httpx 走 SOCKS 需要 `httpx[socks]`）。
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
- **雄獅 30 天內也常態只有零星幾筆**（基隆團期幾乎都「暫時額滿」，額滿的不收），
  同樣過濾後 0 筆不拋錯——但 API 整批回 0 個郵輪商品就一定要拋，
  全球一個月內不可能沒有郵輪團。
- **易遊網的正常頁面也含 `_Incapsula_Resource`**，判斷被擋要看「沒有 `__NEXT_DATA__`
  且有 Incapsula 腳本」，只看後者會把每一頁都當成被擋。
- **易遊網列表價是 3／4 人房的每人價**，要進商品頁取雙人房成人價才能跟其他來源比。
- **「航行天數」顯示的是天，資料存的是夜**（`Deal.nights`；`days` 是 +1 的換算）。
  外國站原生報「N Nights」、台灣站報「N 天」，去重鍵與各來源都以夜為準，
  只有網頁／CSV／CLI 給人看的地方換成天。JSON 兩個都有。
- **跨幣別一定要換算後才能比**：`_price_rank` 若比 `price` 而不是 `price_twd`，
  379 USD 會勝過 18,000 TWD，整個比價與排序都會反過來。
- **Windows 終端機預設 cp950**，印 `✓` 會拋 `UnicodeEncodeError` 讓程式在
  最後一步掛掉。CLI 啟動時會把 stdout／stderr 切成 UTF-8。
- **GitHub UI 的「Re-run」會 checkout 當初那次 run 釘住的 commit**，不是分支最新狀態。
  這個 workflow 每次都會 commit 資料回 repo，所以重跑一次舊執行時，
  基準永遠是過期的，`git push` 必被拒（`! [rejected] main -> main (fetch first)`），
  整個工作以 exit 1 收場——看起來像擷取壞掉，其實只是基準過期。
  解法是 `actions/checkout` 加上 `ref: ${{ github.head_ref || github.ref_name }}`。
  要重驗擷取流程請用 **Run workflow**（workflow_dispatch），不要用 Re-run。
- **錯誤訊息不可以宣稱自己做了沒做的事**。cruisedirect 的 ParseError 原本寫著
  「HTML 已存到 debug/cruisedirect.html 供比對」，但存檔只發生在「被 Cloudflare
  擋下」那條路徑上；真的遇到改版時 CI 什麼都沒留下，執行報告卻叫你去比對一個
  不存在的檔案。現在解析失敗也會存現場，路徑由 `parse_or_save()` 補進訊息裡。
- **「沒有結果」不等於「改版」**。cruisedirect 原本只要抓不到航程卡片就拋 ParseError，
  所以 2026-08-23 基隆那個窗口剛好掛零時整個來源就陣亡了。更慘的是當時
  `except ParseError: raise` 會直接往上拋，而基隆排在迴圈第一個——**東京與橫濱
  連抓都沒被抓到**。現在改讀頁面自己的 `<h2 class="view-header">N Cruises</h2>`：
  宣稱 0 筆就回空清單，宣稱 N 筆卻解析不出來才算改版；單一城市失敗也只記進
  `failures`，不再中斷其他城市。
- **正常頁面裡也會有 Cloudflare 的字串**。2026-09-16 起 cruisedirect 在結果頁自己嵌了
  Turnstile（`#turnstile-analytics-container`，載入 `challenges.cloudflare.com/turnstile/…`，
  基隆頁連 `cf-chl-widget-*` iframe 都渲染出來了）。原本 `is_blocked()` 把
  `challenges.cloudflare.com` 與 `cf-chl` 當攔截標記，於是三個城市明明都通過了挑戰、
  標題也是「Cruise Search Results」，卻全被判成「挑戰未解除」。攔截標記現在只留
  挑戰頁本身才有的 `_cf_chl_opt` 與「Performing security verification」，加上標題判斷。
- **cruisedirect 一頁只放 5 張卡片**，超過就有 `li.pager__item--next`（`&page=1`）。
  舊 fixture 最多 3 筆，分頁從沒被走到；東京宣稱 9 筆時只拿到 5 筆，剩下 4 筆
  **安靜地**漏掉。現在沿著 pager 翻到底（`MAX_PAGES` 防成環）。
- **同一航次可能有兩張卡片**（東京 9/23 Celebrity Millennium：4,687 與 7,661 USD），
  去重鍵相同時「後者蓋前者」會留下貴的那筆。cruisedirect 現在跟 asiayo 一樣只留最便宜的。
- **asiayo 的 RSC 參照字串**（見〈asiayo 的存取方式〉）：卡片上的 `port` 從物件變成
  `"$5f:props:…"`，解析器在 `_resolve_port` 用 `'str' object has no attribute 'get'`
  炸掉。`page_meta` 仍正常，「宣稱 N 筆卻解析 0 筆」那道防線抓不到這種壞法——
  它數的是物件數，物件有、只是欄位是字串。
