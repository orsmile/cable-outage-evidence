# 規格：cable-outage-evidence

這份文件定義偵測、分類與報告的規則。它是 Python 版實作與測試的對照標準。
規則來自原本在 OpenWrt 路由器上執行的版本，那個版本在一條 Hitron CODA-5370 的
cable 線路上跑了約兩個月，記錄到多次 7–10 秒的瞬斷。本文的「原版」都是指它。

## 1. 目標與範圍

- **要解決的問題：** cable（DOCSIS）用戶遇到幾秒到幾十秒的瞬斷。數據機燈號正常、
  一直保持註冊、下行一直鎖定，ISP 的監控（數分鐘一次的上線檢查）看不到。用戶報修
  時拿不出證據，客服常回答「線路正常，請檢查您的路由器」。
- **工具做的事：** 持續、密集地探測網路的每一層，同時讀取數據機的 DOCSIS 日誌與
  頻道狀態，在斷線時留下完整紀錄，並產生 ISP 看得懂、難以反駁的報告。
- **平台：** Windows 10/11、macOS、Linux。Python 3.9 以上，只用標準函式庫。
  Windows 另提供打包好的 exe。
- **網路：** 單一線路。原版的「第二條 DHCP 租約交叉比對」不移植（見 §11）。
- **數據機：** 支援 Hitron CODA 系列的 `/1/Device/CM/*` API 與 CBN `/Request` API
  （依 CG8673V2 介面實作）。其他型號由社群補上。沒有可讀的數據機時，探測與報告仍然可用，只是少了
  DOCSIS 證據。
- **使用者：** 以台灣用戶為主。介面與報告提供繁體中文和英文。

## 2. 原則

這幾條優先於本文其他所有內容。

1. **只寫觀察到的事實，不寫原因。** 任何輸出（分類、理由、時間軸、報告）都不得把
   事件標成 RF、ingress、雜訊、排程器、RPD 或任何成因。寫「有 T4 + CM-STATUS 6」
   「頻道狀態不變仍斷線」，不寫「上行雜訊造成」。
2. **沒有日誌紀錄，不代表問題在 DOCSIS 之上。** 數據機看不到上行訊號品質（CMTS 端
   的 RxMER），上行資料封包遺失也不會在數據機日誌留下紀錄。原版文件曾寫「沒有日誌
   就是 CMTS 轉送層的問題」，那是推論過頭，本專案不沿用。
3. **證據強弱分開記錄。** 強證據才能決定分類；弱證據要列出，但不能升級分類（§7）。
4. **雜訊門檻與事件分開。** 太短、或 TCP 仍然連得通的事件照樣記錄，但標成低等級，
   在報告裡與強事件分開呈現，不隱藏也不混在一起（§6）。
5. **報告預設遮蔽家裡設備的資訊**（§10）；ISP 本來就知道的資訊（數據機 MAC 等）照常列出。
6. **探測不能影響使用者的網路。** 每秒的流量要小，數據機 API 同時只能有一個連線。

### 報告中提到可能原因的寫法

報告可以附一段固定的「背景說明」，內容是：這類「數據機在線、下行鎖定，卻整段
斷網」的現象，符合上行雜訊干擾（例如 burst ingress）的特徵，請 ISP 檢查該節點的
上行品質。這段文字與事件資料分開，不寫進任何事件的分類或理由。

## 3. 路徑與探測目標

```
電腦 ──> 家用路由器 ──> 數據機 ──(同軸)──> ISP 第一跳（CMTS） ──> 公共目標
        lan_gateway     modem              isp_first_hop             public
```

| 層 | 目標 | 怎麼取得 |
|---|---|---|
| `lan_gateway` | 電腦的預設閘道 | 從系統路由表讀取，不寫死。電腦直接接數據機（橋接模式）時，這一層就是 ISP 閘道，`isp_first_hop` 會跟它相同 |
| `modem` | 數據機的管理位址 | 設定值，預設 `192.168.0.1`；常見的另一個是 `192.168.100.1` |
| `isp_first_hop` | 家用路由器之後的第一個 ISP 路由器 | traceroute 自動找出（§3.1） |
| `public` | `1.1.1.1`、`8.8.8.8`、`101.101.101.101` | 預設值，可以修改 |

**只探測回 ping 的近端目標。** 家用路由器和數據機在啟動時、每 300 s、以及預設閘道改變時檢查一次，
沒有回應就不探測（欄位留空、不算失敗）。有些路由器設定成不回 LAN 的 ping；若照樣探測，每次斷線都會
被誤判成「家用路由器斷線」和 `L`。已經在探測的目標若之後不回應，只有在網路正常（`1.1.1.1` 有回應）時
才停止探測：斷線期間它不回應正是證據，不能因此丟掉。CI 的 macOS 與 Linux 雲端機器就是預設閘道不回
ping 的例子。

**數據機兼路由器。** ISP 提供的數據機常常同時當路由器用（預設閘道就是數據機位址），這時只有一台
設備，探測一次並標成 `modem`，不標成家用路由器。選擇「不讀數據機」只關掉 API 讀取，數據機
仍然以 ping 探測；只有在位址輸入 `none`（沒有數據機）時才完全不探測。

`modem` 是整條路徑上唯一在同軸這一側的目標，所以由它來區分「數據機或家裡的問題」
和「同軸之後的問題」。

### 3.1 找出 ISP 第一跳

- 在啟動時、預設閘道改變後，以及每 300 秒，對 `1.1.1.1` 做數字模式的 traceroute，
  取跳過 `lan_gateway` 之後第一個有回應的位址。
- 逾時或失敗時，保留上一次有效的位址。預設閘道改變時先清除舊位址再重新找，避免
  回報過期的拓撲。
- 如果第一跳本身不回 ping（有些 ISP 設備只回 TTL 逾時），就不探測這一層，欄位留空，
  不算失敗。

### 3.2 每次取樣

- 每 500 ms 取樣一次，所有目標同時探測。
- ICMP 每個目標最多試 `ping_retries`（預設 2）次，每次逾時 `ping_timeout_sec`
  （預設 1 s）。**所有嘗試都沒有回應才算失敗。** 單一封包遺失無法和探測程式本身的
  延遲分開，原版在加上重試前，事件紀錄大多是這種雜訊。
- ICMP 的實作順序：系統允許時用免管理員權限的 ICMP socket（macOS；Linux 需要
  `net.ipv4.ping_group_range` 包含使用者），否則呼叫系統的 `ping` 指令並依平台解析
  輸出。Windows 一律用 `ping` 指令。
- 每個取樣要記錄：時間、序號、網路介面是否在線、每個目標的成功與否和 RTT、該次
  取樣的分類（§4）。
- 排程以單調時鐘計算，下一次取樣從上一次的開始時間算起；延遲要記錄，不要吸收掉。
- **取樣可以重疊。** 斷線時一個取樣要等「次數 × 逾時」才放棄（預設 2 s），如果等它結束
  才開始下一個，斷線期間的取樣會變成 2 s 一次，正好在最需要密度的時候變稀。所以每個
  取樣都準時開始，最多同時進行「次數 × 逾時 ÷ 間隔 + 2」個，完成後依序號處理。
  取樣的時間是它開始的時間。
- 預設閘道每 5 s 檢查一次（Windows 和 macOS 要呼叫外部指令，太慢不適合每次取樣），
  所以 `LINK_DOWN` 最多晚 5 s 才出現。

## 4. 取樣分類

`public_failures` 為三個公共目標中失敗的數量。依序判斷，第一個符合的就是結果：

| 分類 | 條件 | 是否算斷線 |
|---|---|---|
| `LINK_DOWN` | 網路介面離線或沒有預設路由 | 是 |
| `LAN_GATEWAY_UNREACHABLE` | `public_failures >= 2` 且 `lan_gateway` 失敗 | 是 |
| `MODEM_UNREACHABLE` | `public_failures >= 2` 且 `modem` 失敗 | 是 |
| `ISP_ACCESS_OUTAGE` | `public_failures >= 2` 且 `isp_first_hop` 失敗 | 是 |
| `ISP_UPSTREAM_OUTAGE` | `public_failures >= 2` 且 `isp_first_hop` 正常 | 是 |
| `ISP_UPSTREAM_OUTAGE_NO_HOP` | `public_failures >= 2`，沒有第一跳可以探測 | 是 |
| `LAN_GATEWAY_ICMP_ONLY` | 只有 `lan_gateway` 失敗 | 否 |
| `MODEM_ICMP_ONLY` | 只有 `modem` 失敗 | 否 |
| `FIRST_HOP_ICMP_ONLY` | 只有 `isp_first_hop` 失敗 | 否 |
| `<TARGET>_PATH_ONLY` | 只有一個公共目標失敗 | 否 |
| `OK` | 全部正常 | 否 |

「是否算斷線」為「否」的分類只是診斷資料，不會開始事件。

## 5. 事件

- **開始：** 一個取樣的分類算斷線。
- **事件開始時，立刻：**
  - 對三個公共目標測 TCP 443 連線（逾時 2 s），並記錄結果；
  - 要求數據機模組進入快速模式（§8.3）。
- **結束：** 連續 `recovery_rounds`（預設 2）個正常取樣。結束時間是第一個恢復的取樣。
- **事件內統計：**
  - `bad_samples`：算斷線的取樣數；
  - `modem_down_samples`：`modem` 失敗的取樣數；
  - `lan_down_samples`：`lan_gateway` 失敗的取樣數。
- **前後資料：** 記憶體中保留最近 120 s 的取樣（`pre_event_seconds`）。事件開始時
  把它寫進事件檔，之後持續寫入，直到恢復後 120 s（`post_recovery_seconds`）為止。
- 開始與結束都要寫進持久化的事件日誌（JSON Lines），程式中途被關掉也能還原事件。

## 6. 等級

分類說的是「斷在哪裡」，不能證明「是不是真的斷了」。等級是另一個軸，依證據強度
判定：

| 等級 | 條件 | 通知 |
|---|---|---|
| `P1` | 網路介面離線，或三個公共目標中至少兩個 TCP 443 失敗 | 立即 |
| `P2` | `bad_samples >= sustained_samples`，且整段期間 `modem` 都失敗 | 立即 |
| `P3` | `bad_samples >= sustained_samples`，但 TCP 443 仍然連得通 | 恢復時一次 |
| `P4` | TCP 443 連得通，且 `bad_samples < sustained_samples` | 只記錄，不通知 |

- `sustained_samples` 預設 4（約 2 s）。
- TCP 443 連得通是「資料面還能用」的正面證據，所以會降低等級，不只是不升級。
- **同一取樣中，`lan_gateway` 或 `modem` 和公共目標一起失敗**，代表很可能是探測本身
  卡住或家裡的網路有問題：一個直接接在網路線上的設備，不會因為同軸或 ISP 的問題而
  沒有回應。這種情況在 §7 標為 `L`。只有在「整段期間 `modem` 都失敗」時，才把它視為
  真的數據機問題而升到 P2。
- 等級在事件進行中只會上升，所以第一次到達 P2 時通知一次即可。

## 7. 事件分類（觀察型）

事件結束、恢復後的 120 s 也收集完成後，自動產生事件分類和時間軸，也可以手動重跑。

### 7.1 數據機日誌的證據強度

| 強度 | 條目 |
|---|---|
| 強 | T4（type `82000400`）；CM-STATUS（`74010100`）且訊息含 `Event Type Code: 6;`；觀察到的頻道狀態變化 |
| 弱 | T3（`82000200`）；其他 type code 的 CM-STATUS |
| 不算證據 | 上行 profile 變更（`67061600`、`67061601`）、DHCP 通知等其他條目 |

數據機日誌的時間，以「電腦時間 − 數據機時間」的時差中位數換算到電腦的時間軸上
（§8.4）。沒有時差資料時用 0，並在輸出中註明。

### 7.2 分類

視窗是 [事件開始 − 60 s, 事件結束 + 60 s]。依序判斷，第一個符合的就是主分類：

| 分類 | 觀察到的情況 |
|---|---|
| `L` | 事件期間 `lan_gateway` 或 `modem` 的探測失敗，或 RTT 超過 100 ms（本地卡住） |
| `A` | 視窗內有強證據 |
| `CP-weak` | 沒有強證據，但視窗內有弱證據 |
| `B-verified` | 沒有任何日誌條目、沒有頻道變化，而且事件期間的頻道狀態都有觀察到，間隔不超過 2 s（快速模式開始前允許 6 s） |
| `B-unverified` | 同 B，但觀察不夠密；要寫出取樣了幾次 |

- **觀察密度（`B-verified` 的條件）**：上行頻道狀態，以及每秒讀取（含下行 OFDM 鎖定）
  要**分別**符合間隔規則。每秒讀取中只要有一個端點沒讀到，那一秒就不算數：上行有讀到
  也一樣，因為下行 OFDM 鎖定只在每秒讀取裡。快速模式在斷線開始後才啟動，所以第一個
  一秒讀取之前的那段，以斷線前最後一次任何觀察為起點，最多 6 s。
- **頻道狀態以種類分開比較**（上行 SC-QAM、上行 OFDMA），同一次讀取內依頻道 index 合併；
  `State` 為空的列是沒讀到值，不算狀態。否則「只讀到 OFDMA」會被誤判成 SC-QAM 頻道消失。
- 原版的 `S`（只有一條租約斷線）不移植，因為沒有第二條線路。
- 主分類只是方便閱讀的標籤，**不能遮蓋其他觀察。**
- 以下旗標欄位一律填寫：`LocalStall`、`StrongControlPlane`、`ChannelTransition`、
  `WeakControlPlane`、`ChannelStateVerified`。
- `ClassReason` 列出每個旗標背後的每一項觀察。例如本地卡住又同時有 T4 的事件，
  分類是 `L`，兩個旗標都是 yes，理由兩項都列。
- `FirstFailingLayer`：第一個失敗的取樣中，最靠近電腦的失敗層
  （`lan_gateway` / `modem` / `isp_first_hop` / `public`）。

### 7.3 時間軸

範圍是 [開始 − 120 s, 結束 + 120 s]，每行格式為「相對秒數　時間　來源　內容」：

- `PROBE`：每個取樣，所有目標的 RTT。
- `MODEM`：頻道狀態變化時一定列出，其餘每 10 s 一行，例如
  `SC 3/3 OPERATE | OFDMA 2/2 OPERATE | DS 32/32 OFDM 1/2`。
- `LOG`：數據機日誌，同時列出數據機時間和換算後的時間。
- `EVENT`：開始、恢復、結束。

開頭一段寫出分類、理由、觀察密度和使用的時差。

## 8. 數據機模組（Hitron CODA）

### 8.1 API

- 登入：`POST {modem_url}/1/Device/Users/Login`，表單欄位
  `model={"username":..,"password":..}`。回應含 `"result":"success"` 即成功，取得
  session cookie。
- 讀取：`GET {modem_url}/1/Device/CM/{SysInfo|DsInfo|UsInfo|DsOfdm|UsOfdm|Log}?_=<epoch>`。
  回應 `errCode` 為 `"000"` 才算成功；其他值或逾時就重新登入。
- 登出：`POST {modem_url}/1/Device/Users/Logout`，表單 `model={}`。
- 一個 cookie 可以連續使用，另外登入（包括使用者打開網頁管理介面）不會讓它失效。
- 數值欄位前面常有空白，要先去掉。

用到的欄位：

| 端點 | 列表 | 欄位 |
|---|---|---|
| `UsInfo` | `Freq_List` | `portId`、`channelId`、`lockStatus`（`OPERATE`）、`frequency`、`bandwidth`、`modulationType`、`signalStrength` |
| `UsOfdm` | `Freq_List` | `uschindex`、`state`（含 `OPERATE`）、`channelBw`、`fftVal`、`repPower`、`repPower1_6`、`digAtten`、`digAttenBo` |
| `DsInfo` | `Freq_List` | `portId`、`channelId`、`lockStatus`（`Locked`）、`frequency`、`modulation`、`signalStrength`、`snr`、`correcteds`、`uncorrect`、`dsoctets` |
| `DsOfdm` | `Freq_List` | `receive`、`plclock`、`ncplock`、`mdc1lock`（`YES`）、`Subcarr0freqFreq`、`plcpower`、`ffttype` |
| `SysInfo` | — | `ntAccess`、`gw` |
| `Log` | `Log_List` | `time`（數據機時鐘，MM/DD/YYYY）、`priority`、`type`、`event` |

`Log` 的 `event` 結尾帶有 `;CM-MAC=..;CMTS-MAC=..;CM-QOS=..;CM-VER=..`。訊息只取
`;CM-MAC=` 之前的部分；`CMTS-MAC` 和 `CM-MAC` 各存一欄：ISP 用前者找節點、用後者找用戶。

### 8.2 只有一個連線

監控執行期間，數據機模組是唯一連到數據機 API 的程式。其他指令要讀數據機時，只把
請求交給它；只有監控沒在跑時才直接連線。

每個 HTTP 請求都有硬性逾時：連線 1 s；`UsInfo`、`UsOfdm`、`DsOfdm` 2 s；`DsInfo`、
`SysInfo`、`Log`、登入登出 4 s。數據機慢或卡住時，只會拖慢數據機模組，不能拖慢探測。

### 8.2.1 讀取狀態與通知

數據機讀取的狀態分成五種：`ok`、`unreachable`（沒有 HTTP 回應）、`auth_failed`（帳密被拒）、
`unsupported`（不是支援的 Hitron CODA / CBN 網頁 API）、`bad_response`（有回應但 API 資料無法解析）。

- **`unsupported` 和 `auth_failed` 要分清楚。** 登入回應若不是帶 `result` 欄位的 JSON（例如其他廠牌的
  HTML 登入頁），或是 HTTP 錯誤頁，先嘗試 CBN 介面；兩者都不支援才是 `unsupported`，不是帳密錯誤。
  Hitron 明確拒絕帳密時不嘗試其他介面，避免重複登入。
- `unsupported` 時讀取數據機首頁，找出廠牌字樣與網頁標題放進通知，方便使用者回報型號；
  監控期間每小時才重試一次；設定對話裡預設選「不讀數據機」；之後啟動會問一次要不要改成不讀。

- 帳密錯誤立即通知；其他兩種要連續失敗 3 次才通知，因為逾時和 session 過期偶爾會發生。
- 狀態改變時（包括恢復）印出通知，內容含建議的處理方式，並寫進 `events-journal.jsonl`
  （`type: modem_status`），報告用它說明哪段時間沒有數據機資料、原因是什麼。
- 帳密錯誤後每 300 s 才重試登入一次，避免數據機因多次失敗而鎖住帳號。
- 數據機讀取失敗**不影響**探測、斷線偵測和等級判斷；數據機的 ping 仍是獨立的一層。
- 通知內容不含密碼。

### 8.3 讀取頻率

原版實測這型數據機的回應時間：`UsInfo`、`UsOfdm`、`DsOfdm` 約 0.1 s，`DsInfo` 0.7 s，
`SysInfo` 0.9 s，`Log` 1.2 s。

- **每 5 s：** 讀 `UsInfo` 和 `UsOfdm`，比較每個上行頻道的狀態組合（是否存在、狀態、
  頻道 ID、頻率、寬度、調變）。功率和衰減一直在小幅變動，不算狀態。
  - 狀態沒變的結果不寫檔，但保留最後一次觀察（含時間）。
  - **狀態一變，先寫出保留的上一次觀察（原本的時間，原因 `PrevObserved`），再寫
    新的（原因 `StateChange`），然後進入快速模式。** 這樣頻道變化的時間一定落在兩次
    真實觀察之間，最多 5 s 的區間。
- **每 30 s：** 讀全部端點，寫一列彙總，加上每個上行頻道一列。
- **每 300 s：** 每個下行頻道一列。
- **日誌：** 每 300 s 讀一次；事件快照時一定讀。
- **快速模式（每 1 s）：**
  - 觸發：事件開始（至少 60 s）、事件恢復（再延長 60 s）、數據機模組自己偵測到狀態
    變化（至少 90 s）。單次最長 300 s。
  - 每一秒同時讀 `UsInfo`、`UsOfdm`、`DsOfdm`，期限是這一秒的 85%。超過期限的這一秒
    標為不完整，直接進下一秒。
  - `DsInfo`（每 5 s）、`Log`（每 15 s，進入和結束時各一次）、`SysInfo`（進入和結束時）
    在背景讀，忙的時候跳過，不能拖慢每秒的讀取。最近三秒中有任一秒超過 600 ms 或
    遲到，背景讀取暫停 5 秒。

### 8.4 時差

數據機 HTTP 回應的 `Date` 標頭，用來估計每次的時差（電腦時間 − 數據機時間，
解析度 1 s）。數據機日誌的時間用附近時差的中位數換算。

### 8.5 日誌去重

數據機日誌是 40 筆的環狀緩衝區，大約四天就會被覆蓋，所以要定期複製出來。

- 以條目內容識別（`time`、`priority`、`type`、訊息、CMTS MAC），**不用 `index`**：
  `index` 是條目在環中的位置，每次有新條目都會往下移。
- 記錄時間不放進識別值，否則同一筆會在每次讀取時重複出現。
- 保存最近 240 筆的識別值即可（環的深度的數倍）。

### 8.6 已知限制

- `dsoctets` 不是即時值：原版實測 32 個頻道在 10 s 內都沒變，而 WAN 埠同時收到
  6 MB。不能用它判斷下行有沒有在收資料。
- `correcteds`、`uncorrect` 在這型數據機上一直是 0。
- 數據機看不到上行訊號品質，上行封包遺失也不會留下日誌（見 §2）。
- `UsOfdm` 的 `repPower` 是整個 OFDMA 頻道的總功率，頻道越寬數值越高。要看的是
  `repPower1_6`（換算到 1.6 MHz），它才能和 SC-QAM 的功率比較：
  `repPower = repPower1_6 + 10·log10(channelBw / 1.6)`。

## 9. 儲存

每天一個資料夾，CSV 一律 UTF-8、含標題列。程式更新後如果某個 CSV 的欄位變了，
舊的當日檔改名保留（`.schema-<時間>.csv`），不覆蓋。

| 檔案 | 內容 |
|---|---|
| `events.csv` | 每個事件：時間、長度、等級、分類、各層失敗與否、TCP 443 結果、`bad_samples` 等統計 |
| `events-journal.jsonl` | 事件開始與恢復的紀錄 |
| `minute-summary.csv` | 每分鐘每個目標的遺失率與 RTT 最小／平均／最大 |
| `incidents/` | 每個事件前後 120 s 的所有取樣 |
| `modem-signals.csv` | 數據機彙總：頻道數、鎖定數、功率與 SNR 範圍、OFDMA 寬度與 `repPower1_6` |
| `modem-channels.csv` | 每次讀取每個頻道一列，長格式，含原因（`Periodic`、`PrevObserved`、`StateChange`、`Fast` 等） |
| `fast-poll.csv` | 快速模式每秒一列：遲到時間、讀取耗時、頻道計數、時差 |
| `docsis-log.csv` | 從數據機複製出的日誌：記錄時間、數據機時間、等級、type、訊息、CMTS MAC、數據機 MAC |
| `event-classes.csv`、`timelines/` | 觀察型分類與時間軸 |
| `sessions.jsonl` | 程式啟動與停止的紀錄，用來看出電腦睡眠或關機的空白 |

- 保留 `retention_days`（預設 180）天。報告有說服力的部分，是事件和平常基準的對比，
  所以保留期要長。原版每天約 3.6 MB。
- 資料夾位置：Windows `%LOCALAPPDATA%\cable-outage-evidence\`，macOS
  `~/Library/Application Support/cable-outage-evidence/`，Linux
  `~/.local/share/cable-outage-evidence/`，都可以改。
- **電腦睡眠、關機或程式沒在跑的期間沒有資料。** 報告要寫出監控的涵蓋時間，避免把
  空白誤讀成沒有斷線。

## 10. 報告

| 指令 | 內容 |
|---|---|
| `report [DAYS]` | 給 ISP 的摘要（預設 30 天）：監控涵蓋時間；事件數依等級與分類統計；時段分布；P1–P3 事件逐筆列出（時間、長度、斷在哪一層、數據機頻道狀態、日誌條目原文）；P4 只計數。最後是 §2 的固定背景說明 |
| `evidence [TIME]` | 單一事件的完整證據：前後的取樣、數據機頻道狀態、前後半小時的日誌、訊號值有沒有變動、和長期基準的比較 |
| `timeline [TIME]` | 印出事件時間軸 |
| `docsis-log [DAYS]` | 依時間列出收集到的日誌，並依 type 統計 |
| `ucid-report UCID [DAYS]` | 把每筆指名該 UCID 的 CM-STATUS type 6，串連到之前的 T4、OFDMA 頻道離開與恢復 `OPERATE`、以及同時段的斷線；頻道變化以區間呈現並註明解析度。UCID 沒有預設值 |

`ucid-report` 的頻道資料依序使用：每秒讀取、5 s 狀態變化的前後兩次觀察、30 s 完整讀取的
頻道數（只用 `Periodic` / `Startup`，因為快速模式進出時寫的彙總是快取資料）。後者只能給出
約 30 s 寬的範圍。2026-10-10 以原版歷史資料驗證，UCID 7 與 8 的區間都和原版一致。

### 遮蔽

只遮蔽**家裡設備**的資訊：家用網路的私有位址（`192.168.x.x`、`172.16–31.x.x`）改成
`<home-network>`。

不遮蔽、而且要列出的：

- 數據機自己的 MAC（從日誌的 `CM-MAC` 取得）：ISP 本來就知道，而且要用它找到用戶；
- CMTS MAC、ISP 第一跳、數據機回報的 CM 閘道：都是 ISP 自己的設備；
- 數據機的管理位址（`192.168.0.1`、`192.168.100.1`）：每台同型數據機都一樣。

報告不會出現公網 IP：程式沒有收集它。加 `--no-redact` 可以輸出完整資料。

## 11. 不移植的功能

| 原版功能 | 原因 |
|---|---|
| 第二條 DHCP 租約交叉比對、`isp-report` 的分歧分析 | 需要能在 WAN 埠開第二個介面的路由器；一般電腦做不到 |
| WAN 埠流量計數器 | 電腦看不到 WAN 埠，只看得到自己的網卡 |
| ISP 閘道 MAC 追蹤 | 電腦在家用路由器後面，看到的是家用路由器的 MAC |
| tcpdump 計算來自 CMTS 的封包 | 同上，而且需要管理員權限 |
| procd 服務、flock、BusyBox 相關的處理 | 改用 Python 迴圈；常駐方式各平台自行設定（工作排程器、launchd、systemd） |

## 12. 設定

| 選項 | 預設 | 說明 |
|---|---|---|
| `interval_ms` | 500 | 取樣間隔 |
| `ping_timeout_sec` | 1 | 每次 ping 的逾時 |
| `ping_retries` | 2 | 每個目標的嘗試次數 |
| `sustained_samples` | 4 | 事件成為 P2/P3 所需的斷線取樣數 |
| `recovery_rounds` | 2 | 確認恢復所需的正常取樣數 |
| `pre_event_seconds` / `post_recovery_seconds` | 120 / 120 | 事件前後保留的取樣 |
| `public_targets` | `Cloudflare:1.1.1.1 Google:8.8.8.8 TWNIC:101.101.101.101` | 公共目標，`名稱:位址`；名稱用在 CSV 欄位 |
| `path_discovery_interval_sec` | 300 | 重新找 ISP 第一跳的間隔 |
| `modem_type` | `auto` | 自動辨識；`cbn` 直接指定 CBN；舊 `hitron-coda` 也自動辨識；`none` 表示不讀數據機 |
| `modem_url` | `http://192.168.0.1` | |
| `modem_username` / `modem_password` | `admin` / `password` | Hitron 的原廠預設；`init` 會詢問，直接按 Enter 沿用。存在只有使用者可讀的設定檔 |
| `modem_state_poll_interval_sec` | 5 | |
| `modem_poll_interval_sec` | 30 | |
| `modem_channel_ds_interval_sec` | 300 | |
| `modem_log_interval_sec` | 300 | |
| `modem_fast_min_sec` / `_post_sec` / `_statechange_sec` / `_max_sec` | 60 / 60 / 90 / 300 | 快速模式 |
| `modem_fast_dsinfo_interval_sec` / `_log_interval_sec` | 5 / 15 | |
| `retention_days` | 180 | |
| `data_dir` | 依平台（§9） | |
| `telegram_enabled` | 0 | 選用的通知 |

密碼和 Telegram token 不寫進日誌、報告或錯誤訊息。

## 13. 驗證

- **單元測試：** 從原版自我測試移植：
  - traceroute 輸出的解析；
  - 取樣分類的每個分支；
  - 等級的每個分支；
  - 數據機 JSON 的解析，包括有空白的數值；
  - 日誌去重，包括 `index` 改變時不重複。
- **分類測試：** 以合成的事件視窗驗證：
  - 只有 T3 → `CP-weak`；
  - 只有 CM-STATUS type 16 → `CP-weak`；
  - 只有 T4 → `A`；
  - 只有 CM-STATUS type 6 → `A`；
  - 只有頻道變化 → `A`；
  - 什麼都沒有且每秒都有觀察 → `B-verified`；
  - 數據機探測超過 100 ms 又有 T4 → `L`，兩個旗標都有。
- **回放測試：** 用原版的歷史資料（匿名化後）回放，事件、等級與分類要和原版一致；
  原版的雙線路欄位忽略。2026-10-10 以原版 2026-08-26 ~ 10-08 的 26 個事件（排除 `S`）回放，分類 26/26 一致。
- **輸出檢查：** 所有報告和時間軸都不得含有成因用詞（RF、ingress、雜訊、scheduler、
  RPD 等），§2 的固定背景說明除外。
- **平台：** 三個平台都要跑過探測、ICMP 降級為系統 `ping` 的路徑、睡眠後恢復。

### CBN `/Request` API

預設 `modem_type = auto`，舊 `hitron-coda` 設定也自動辨識；`cbn` 可直接指定 CBN。
依 CG8673V2 的介面實作，其他 CBN 韌體須以 `modem-check` 確認。

- 先 GET `/common_page/login.php` 保留 cookie，再讀頁面引用的 `/js/CBN.js.php`（含版本 query），取得 `CSRF_Token`。
- POST `/Request/Set.php`，`fun = 7` 與 `Username` / `Password` 登入；`fun = 8` 登出。
- POST `/Request/Get.php`：`fun = 2` 系統、`108` DOCSIS、`11` 日誌。
- 表單 `Data` 為 base64(encodeURIComponent(JSON))；每次請求帶 Referer 與 CSRF header，接受回應更新 token。
- CBN 請求序列化，避免並行請求使用過期 token；等候鎖也算入讀取逾時。DOCSIS 四類頻道共用最多 0.5 s 的快取。
- 上行 `LockStatus = Locked` 映射為 `OPERATE`；OFDMA `STATE = 2` 為 `OPERATE`。保留未鎖定 OFDM / 未運作 OFDMA，不能把消失的狀態隱藏掉。
- CBN 日誌沒有數值事件碼，只將描述中明確的 T3、T4、CM-STATUS 對應到既有證據規則；其他描述不升級證據強度。
- CBN `Time` 的實測格式為 `YYYY-MM-DD HH:MM:SS`，在介面層轉成下游共同的 `MM/DD/YYYY HH:MM:SS`；不改變時區或重複套用時差。未知時間仍保留原文，不猜測日期。
- CM-STATUS 只有描述含精確的 `Event Type Code: 6;` 才是強證據，其他格式維持弱證據。現有 CBN 實機日誌沒有 CM-STATUS，type 6 的端對端測試使用合成字串，尚未確認其他韌體的描述格式。
- 無效編碼、缺少頻道群組或 session 過期記為讀取失敗，清除登入與快取，讓 worker 下次重新登入。錯誤訊息不回印伺服器原始內容或帳密。
- 單次連線失敗或逾時只清除 DOCSIS 快取，保留 session 與已收到的 token；若遺失的回應已輪替 token，下次被拒絕後才重新登入。收到 headers 即更新 token，避免讀取 body 逾時後無謂地重新登入。
