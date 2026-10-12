# E2E 與 Plan v3 架構審查 · 2026-10-12

本輪從 `90d305462897d4c727fba73684f43623a3be4a87`（0.5.0）開始，目標是驗證跨服務完整路徑、重現失敗／恢復邊界，以及核對 Plan v3。修正版為 0.5.1。六份 Owner 原稿保持不變；工程結果不會批次升級 32 個產品驗收。

**主要結論：外層安裝、設定與 Hermes 工具查詢已有可測的實作；Plan v3 的真正 Host／Home coding 尚未接通，E1／E2 尚未完成。** 缺少的不是更多 prompt，而是 worker 派工與隔離執行、產物／預覽、人工交接，以及批准後的固定更新／退版鏈。下一個交付應集中完成一條 Host 修正流程。

## 測試範圍與證據

- 跨服務瀏覽器測試：[test_onboarding_e2e.py](../../tests/test_onboarding_e2e.py)。真 Chromium → 編譯 Web → Controller session／CSRF → TLS Runner／真 Codex → 真 Hermes AIAgent／官方 MCP → 隔離 Core／原生 OAuth。沒有攔截瀏覽器 API；模型回覆與 Host 安裝／VM effects 是明確 fixture。真 Core 初始化使用其 onboarding API，不能稱為逐畫面操作了所有 HA native wizard。
- 獨立實際 HAOS：本輪 **1 passed，84.78 秒**，官方 HAOS 18.3、Core 2026.10.1、Supervisor 2026.09.3，在一次性 KVM guest 完成 owner 驗證及 Supervisor backup 讀回。[JUnit](../../evidence/review-haos.xml)、[平台記錄](../../evidence/haos-vm.json)。沒有 Home Worker 或實體設備。
- 取消／配對、Controller 時序及通道邊界的原始失敗與修正後記錄，分別見 [Runtime](2026-10-12-runtime.md)、[Bootstrap](2026-10-12-bootstrap.md)、[Web／Gateway](2026-10-12-ui.md)。部分失敗由 deterministic asyncio barrier 控制，避免只重跑到偶然成功。
- GitHub CI **270 passed／3 skipped**，包含真服務 E2E；[CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38222178550)／[JUnit](../../evidence/pytest-0.5.1-github.xml)。服務資源限制與正式 compose 相符，image IDs 與限制另記在 [CI E2E](../../evidence/onboarding-e2e-0.5.1-github.json)。
- 本機完整回歸 **271 passed、2 skipped／194.16 秒**，見 [JUnit](../../evidence/pytest-0.5.1.xml)。最終修正版安裝媒體結果記錄在 [版本驗證](../VERIFICATION.md)。0.5.0 的精確媒體證據保留原版本，不代表已驗證 0.5.1。

模型 fixture 驗證的是協定、資料流與權限邊界；不證明 OAuth 供應商實際同意、模型推論品質、真 bot 收發或家庭現場可用。真帳號、Owner laptop、HAOS App coding、設備、長期觀察皆需各自的證據。

## 本輪重現的產品缺陷

| 缺陷 | 修正及驗證重點 |
|---|---|
| 0.5.1 發行版本紀錄漂移 | 第一份新版 ISO 安裝後仍寫舊的 0.5.0；已改為讀 Host VERSION，補上真 manifest 寫入／Owner 權限測試和 kit 版本一致性檢查；失敗媒體與 JUnit 保留 |
| 取消只等 CLI leader，仍有子程序寫入 | 以 UID／PID start time／pidfd 核對身份並停止整組；無法確認時持久隔離，不開下一個 writer；真正忽略 TERM 的子程序回歸 |
| 重複取消中斷自己的停止清理 | 同一 task 只要求取消一次；重複 HTTP 請求等待同一清理，client 斷線不打斷；真正 TERM-ignoring 子程序及斷線案例 |
| Controller／Gateway 誤報停止成功 | 一般家庭、Host 查詢、Gateway 都核對 Runner 停止回覆；回覆遺失／stop_unconfirmed 保存 needs_reconcile，不引導立即重送 |
| 配對已成功但回覆遺失 | 原 code 期限內，完全相同的身份重試取回同一 bearer；重啟可恢復，不延長有效期、不輪替權限 |
| 等待 Host 回應時暫停仍派新工作 | 每次關鍵 await 返回後、下一筆 dispatch／checkpoint 前重新檢查 pause |
| 並行 HA 綁定或完成讀回覆蓋新身份 | token 寫入及完成 checkpoint 前再次核對 owner、session／epoch；race 測試驗證 |
| Hermes 安裝後 readback 無期限等待 | 持久 120 秒 observation deadline，重試核對原 handle，不重新安裝／推理 |
| 背景登入觀測清空 human_action | 同一步驟等待登入時保留表單；只有新的觀測結果切換狀態 |
| HA OAuth 失敗停在含 code 的 URL | 成功／拒絕都回乾淨 onboarding URL；失效 session 不交換 code；no-store／no-referrer |
| Telegram 首次同步吞掉剛送的配對碼 | 有界首批只驗證當次有效 `/pair`；一般歷史訊息不進模型，失效碼／bot sender 拒絕 |

瀏覽器整合另外揭露測試本身的 label 精確比對問題：巢狀 select 的 options 會參與 accessible label；這是 harness 修正，不當成產品缺陷計數。

## 尚未完成：按交付風險排序

| 優先／項目 | 現有實作與缺口 | 下一個可驗收結果 |
|---|---|---|
| P1 資料保留正確性 | Runner 僅新 job 觸發固定 7 天 prune；8 天前的結果在 idle GET 仍可讀。Controller 較短期限未同步 | 持久期限、idle 清除、下調立即失效、升高不復活；保留有界去重 tombstone，舊請求不能重執行 |
| P1 取消後可信恢復 | stop_unconfirmed 正確阻止新工作，但沒有可信 reconciliation／clear；服務重啟遇到 running job 也保守隔離 | 以不可重用的容器／boot／process 身份證明舊 writer 已不存在，再發新 lease；不能靠刪 marker 或按「重試」解除 |
| P1 / E1 真 Host Coding Worker | MCP worker.submit 目前只保存 blocked handle。離線 Python sandbox 能力測試不等於 Codex／Claude／AGY／Grok development | 一個 provider 在独立 workspace 查來源、寫重現程式、修一個真問題，交 base commit／diff／tests；單一 coding concurrency 與取消負向測試 |
| P1 / E1 egress／帳號邊界 | 未接可用的 provider／研究來源網路政策；任意程式可能讀自己的 CLI credential | 明確支援的登入方式、role 分離、依來源核准出站、限額與期限、私有 LAN／管理服務阻擋；無暗中 provider fallback |
| P1 / E1 產物、預覽、接手 | 尚無完整 artifact 驗證／獨立 preview origin／workspace lease／BAT handoff | 拒 symlink／越界／超量／secret 產物；preview 無正式 cookies／設備網路；確認舊 writer 停止才交人類 |
| P1 / E2 真 Home Coding Worker | 已有真 HAOS VM 及受限 Runner，仍無 HAOS App 內 research/development | App 內自主補查、可寫 scratch、小程式與 tests，證據標記 App／Core／Supervisor 不同視角；不取得管理 bearer／Docker socket |
| P1 / E2 提案至部署／退版 | 家庭 Engine 與初次 Hermes installer 已存在；尚無 Hermes research → proposal adapter 或通用候選 updater | candidate digest／trusted publisher／獨立 verifier／精確批准／一致快照／健康核對／獨立 rollback；不得用舊 DB 覆蓋新 Live 操作 |
| P2 网络與設備接入 | default route 不等於外網可用；Web loopback／SSH tunnel，未交付 Tailscale／固定 IP 選擇；VM NAT 不證明 mDNS／USB | 保留本機救援入口，定義遠端身份／撤權與 callback origin；逐項驗證 discovery／USB／設備，不因開放管理埠解決連線 |
| P2 真帳號及通道 | LINE.js／Telegram／Discord adapter 可測，但此輪無真帳號或雲端 consent | 至少一條真 CLI＋Hermes 登入、失效／重登、真 bot allowlist／去重；各 provider 分開記錄 |
| P2 / E3 運作與恢復 | 沒有 72h、相鄰 Core、離機恢復或單／雙 worker 成本比較 | 保留 Plan v3 T3-26、T3-30～32 各自證據；不阻擋先交 E1 |

P1 是下一輪工程優先級，不是本輪聲稱全部已修。尤其 retention 與 quarantine recovery 是已知產品缺陷／缺失，不能包裝成「等真帳號才能驗」。

## Plan 需要再寫細的地方

1. **能力啟用 gate。** 每個 mode 的實作、測試與帳號限制需可讀 capability；先啟用一條真正 Host coding，再逐 provider 擴大。已安裝 CLI／可輸出 JSON 不算 coding ready。
2. **跨服務停止與重啟契約。** 統一 requested、confirmed、unconfirmed 的語意，以及 Controller/Gateway 的使用者狀態；持久 job、lease、boot 身份與人工接手必须一起設計。
3. **全鏈資料生命週期。** Controller、Runner、Hermes、artifact、preview、migration snapshots 的內容期限與去重期限分開；縮短保存不能只清 Controller，離線服務重連後也需套用。
4. **網路選擇與來源身份。** 固定 IP 解決位址穩定，Tailscale 解決認證後遠端連線，是不同決策；目前 localhost OAuth 不能直接搬到任意遠端網址。必須定義 redirect、origin、裝置撤权和失網救援。
5. **「最新版」與正常更新分開。** 首次官方 HEAD 解析／鎖定／compatibility probe 已有；使用中產品需可信發布身份、相容矩陣與 rollback，不應重新跑初始安裝器。
6. **E2E 分層，但追同一成果。** 實際安裝媒體、跨服務瀏覽器、真雲端／家庭三組各記 commit、digest、runtime_location、fixture 邊界。工程 CI 綠不等於 32 案全過；不得把最新分支證據歸給較早 ISO。

## 建議下一個開發切片

先完成 **Hermes → 真 Host Worker → 隔離修正 → 獨立測試與 preview → 安全接手**，對應 V3-06～09／T3-07～12；並處理 retention 與停止恢復。一次只選一個已驗證的 provider，保留其他選項的正確 unavailable 狀態。完成後再接真正 Home App worker 及固定 updater，讓同一 case 走完家庭試跑與退版。

所有本輪測試使用磁碟 basetemp 及自有隔離容器。產品安裝只允許一次性 VM；不在開發主機執行 installer、不使用或發布真實家庭資料。
