# cable-outage-evidence

繁體中文 | English below

> **v0.1.3。** 監控、數據機讀取、事件分類與報告都可以試用；Windows 已實機測試；macOS / Linux 通過自動化測試，還需要實機使用回報。

## 這是什麼

你是不是遇過這種情況：網路突然斷了幾秒到幾十秒，數據機的燈號卻一切正常；打給客服，
對方說「線路正常，請檢查您的路由器」。

這類瞬斷常常太短，ISP 的監控看不到：數據機一直保持上線，ISP 每幾分鐘做一次的檢查
都顯示正常。用戶手上又只有幾張測速截圖，很難要求 ISP 處理。

這個工具在你的電腦（Windows、macOS、Linux）上持續記錄：

- 每 0.5 秒探測家用路由器、數據機、ISP 第一跳和公共目標，找出斷在哪一層；
- 讀取數據機的 DOCSIS 日誌和上下行頻道狀態，斷線時改成每秒讀一次；
- 產生給 ISP 的報告：每次斷線的時間、長度、各層的狀態，以及數據機日誌的原文。

報告只寫觀察到的事實，不替 ISP 下結論。這樣的報告比較難被反駁，也讓 ISP 的工程師
能直接判斷該查哪一段。

這類「數據機在線、下行鎖定，卻整段斷網」的現象，符合上行雜訊干擾（例如 burst
ingress）的特徵。工具量不到 ingress 本身，但能把證據整理好，請 ISP 用他們的儀器
檢查上行。

支援自動辨識 Hitron CODA 與 CBN `/Request` API（依 CG8673V2 介面實作），可記錄 DOCSIS 日誌與
SC-QAM、OFDM、OFDMA 頻道狀態。CBN 韌體介面不同時仍可能不支援；其他型號仍能記錄斷線。
歡迎到 Issues 回報型號，或提供 API 資料、PR。

詳細規則見 [docs/spec.md](docs/spec.md)。

## 下載與使用

**Windows：** 到 [Releases](../../releases) 下載 `cable-outage-evidence-<版本>-windows-x64.zip`，解壓縮後雙擊
`cable-outage-evidence.exe`。第一次執行會詢問數據機位址與帳密，直接按 Enter 使用原廠預設
（`admin` / `password`），並當場登入數據機確認；帳密錯誤時可以重新輸入、選擇不讀數據機，
或先照這樣存。之後每次啟動都會檢查，帳密不對會直接問你要不要重新輸入。確認後視窗會持續
監控，要保持開著。exe 沒有數位簽章，第一次執行若被
SmartScreen 擋下，點「其他資訊」→「仍要執行」。

**macOS / Linux：** 需要 Python 3.9 以上。下載原始碼後執行 `python3 -m cable_outage_evidence`。

## 目前可以試用的部分

```
py -3 -m cable_outage_evidence init            # 建立設定檔（會詢問數據機帳密）
py -3 -m cable_outage_evidence modem-check     # 確認讀得到數據機
py -3 -m cable_outage_evidence discover-path   # 找出預設閘道與 ISP 第一跳
py -3 -m cable_outage_evidence once            # 取樣一次並印出
py -3 -m cable_outage_evidence run             # 持續監控，Ctrl+C 停止
py -3 -m cable_outage_evidence report --days 30 --out report.txt   # 給 ISP 的報告
py -3 -m cable_outage_evidence evidence        # 最近一次事件的完整證據（或加上時間，例如 "2026-09-03 23:26"）
py -3 -m cable_outage_evidence timeline        # 事件時間軸
py -3 -m cable_outage_evidence docsis-log      # 收集到的數據機日誌
py -3 -m cable_outage_evidence ucid-report 7   # 上行頻道 7 的 T4 / CM-STATUS / 頻道掉線串連
py -3 -m unittest discover -s tests -t .       # 測試
```

macOS / Linux 用 `python3` 取代 `py -3`。

數據機帳密預設為 `admin` / `password`。如果你的 ISP 改過，執行 `init` 重新設定
（密碼只存在本機的設定檔），再用 `modem-check` 確認連得上。數據機連不上或帳密錯誤時，
監控會印出通知與處理建議，探測和斷線偵測照常進行。

`modem_type` 預設為 `auto`；可指定 `cbn`，或用 `none` 關閉數據機讀取。舊設定裡的
`hitron-coda` 也會自動辨識 CBN。若先前已選擇「不讀數據機」，請執行 `init` 重新啟用。

## English

> **v0.1.3.** Monitoring, modem reading, event classes and reports work. Tested on real hardware on Windows; macOS and Linux pass automated tests and need real-world reports.

Short cable (DOCSIS) dropouts of a few seconds often never show up in ISP
monitoring: the modem stays registered and the ISP's periodic checks see nothing.
This tool runs on a PC (Windows, macOS, Linux). It probes every layer of the path
every 0.5 s, records the cable modem's DOCSIS event log and channel state, and
builds an evidence report that states what was observed without guessing a cause.

Supports automatic detection of Hitron CODA and CBN's `/Request` API (based on
the CG8673V2 interface). CBN firmware variants may expose different APIs.
Legacy `hitron-coda` configurations also detect CBN; use `init` to re-enable
modem reading if it was previously disabled. See [docs/spec.md](docs/spec.md)
(Traditional Chinese).

## License

MIT
