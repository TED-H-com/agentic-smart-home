# 給 Dev Team：Hermes 統籌，內外雙 Coding Worker

**Steering v3.0｜2026-10-11｜Owner：Ted Huang**  
**適用專案：`teddashh/agentic-smart-home`**  
**文件版本不是產品版本。** 本次查核 `main@3ad476a712009469521c3f9838a44ef35ba29f76`；repo 應用基準為 0.3.1。本文件是下一輪開發方向，不是 Hermes／雙 worker 已整合完成的聲明。[來源](SOURCES.md#r01)

## 請採用這個產品定義

我要在家裡實際使用這台 OS，並靠它的 Agent 協助後續 research、開發、debug、UI/UX 與驗收。**外層常駐一個 Hermes 作為唯一主要協調者；外層與 HAOS 內層各有一個可自主研究、寫程式、跑測試的 coding CLI worker。** Hermes 隨時能在已授權範圍內調動他們，不是每次都由我複製貼上工作。

不是內外各一套完整 Hermes，也不是內層只剩固定命令執行器。目標是「一個協調者、兩個具備不同現場視角的工程師」。兩個 worker 可以用同一家 CLI／模型，但工作區、session、認證與能力範圍各自隔離。

## 三個角色，不造三套控制平台

| 角色 | 必須真正做到 | 不應承擔 |
|---|---|---|
| 外層 Hermes | 理解需求、保留 case 背景、派工、追問、比較證據、彙整進度 | HA 管理權威、root daemon、自己批准自己的正式部署 |
| Host Coding Worker | 查 repo／上游、查主機證據、在隔離 workspace 改 code、跑 tests、開 UI preview | 熱改正式服務、任意存取全家設備 |
| Home Coding Worker | 在 HAOS App 內自主查證、讀對應版本原始碼、寫重現程式／測試、提出 patch、核對現場結果 | 任意改 Core／HAOS、把 App 視角冒充所有容器或主機視角 |

簡單查詢由 Hermes 直接使用工具；複雜調查才派工。常駐的是 runtime／接單服務，不要求三個模型持續同時推理。初期每個 worker 最多一個工作，資源不足就順序執行。

## 保留既有成果，新增薄薄的接點

保留 Ubuntu＋KVM＋官方 HAOS、Controller、Host broker、家庭 Engine、Web／Gateway、提案批准、ownership ledger、備份與讀回驗證。既有 Runner 已有 `host/haos` 分流、TLS 指紋與 jobs 路徑，應擴充它，不重造遠端派工平台。[來源](SOURCES.md#r03)

新增 Hermes adapter／MCP 工具，讓 Hermes 經 Controller 提交兩層工作。**只有 Controller 發正式執行憑證；模型發來的 `approved=true` 沒有效力。** 單一通訊 bot 只讓一套 Gateway 接收訊息。Hermes 子代理功能不自動等於我們的遠端 CLI；要完成明確的 Runner adapter。[來源](SOURCES.md#u01) [來源](SOURCES.md#u02)

## 本次相對 v2 的關鍵修正

v2 的隔離 Dev Session 只在外層；**v3 把 research／workspace coding 能力也列為內層必要交付。** Home Worker 可以自己決定下一個診斷步驟、讀程式、寫小型重現與測試，不應被限制成只能回 JSON 意圖。現有家庭操作路徑仍然限制嚴格；新增的自由只存在於隔離工作區與明確授權的診斷工具中。[目前受限命令基準](SOURCES.md#r04)

不能把舊 Runner 全部改成不檢查權限的模式，也不能為了滿足「inner」字樣把另一個 host 容器假稱 HAOS 內層 worker。內層需要真實的 HAOS App 執行證據。

## 請照這個次序交付

**E0：工程包先到 laptop。** 有 Hermes 登入／接點、外層診斷、可跳過 HA／通道的首次設定，以及不依賴 Web 的救援路徑。帳號登入在 Owner 現場完成；未登入就顯示未登入。

**E1：外層真的幫忙開發。** Hermes 派 Host Worker 修一個真實問題；有 diff、測試、preview／前後畫面；正式家庭環境未被修改。

**E2：內外真的合作。** Home Worker 在 HAOS App 內做一次真實自主研究／小型重現；Host Worker 做對應修正；Hermes 彙整並安排獨立驗證。再完成同 schema 的候選應用更新／退版，及兩個 helpers 的原生觸發流程。

**E3：在使用中穩定。** 逐台接入低風險設備、累積 72h、相鄰 HA 穩定版相容與離機恢复。這些不是 E0 之前的所有前置工作。

## Owner 的日常體驗

> 「這個設備狀態不對，畫面也看不懂，幫我查清楚並修一版。」
>
> Hermes 建立同一 case：派外層查產品／上游，派內層查現場。兩邊各自提交有來源、版本、時間與觀測位置的證據。外層完成候選修改，內層以獨立檢查驗證適用性。我先看 preview，確認後才更新正式版；失敗能退回，不重灌整台。

一次批准一個開發範圍，範圍內讀寫 workspace、執行測試不逐命令打擾。正式家庭變更、程式部署、主機網路／USB、公開家庭資料仍需相應授權。兩個 Agent 都說可以，不等於測試通過，也不等於 Owner 已批准。

## 下一輪不要做

不增加第五種 provider、第二套 Hermes、OpenClaw 常駐協調層、更多 distro、另一套 Gateway／記憶平台、通用 fleet 或新的 UI framework。暫不開 worker 遞迴派工、永久自動掃描或自我更新。不要因為重型 build 無法在 HAOS 跑，就將內層研究功能整個刪掉；內層先做好小型分析與現場驗證。

**第一個成功標準：Owner 靠這個系統的 Hermes 和 coding worker 完成一次真實修正。第二個成功標準：內外 worker 帶回互補現場證據，使下一次修正能被獨立驗證。**

執行依據：[Project Plan](PROJECT_PLAN_V3.md) → [Backlog](BACKLOG_V3.md)。技術契約：[Project Spec](PROJECT_SPEC_V3.md)、[Worker Protocol](WORKER_PROTOCOL_V1.md)。現場驗收：[Pilot Acceptance](PILOT_ACCEPTANCE.md)。
