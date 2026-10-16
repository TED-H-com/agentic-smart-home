# Agentic Smart Home — Plan v3

Owner：Ted Huang。方向版本：Steering v3.0，2026-10-11。匯入時應用基準為 `0.3.1`、commit `3ad476a712009469521c3f9838a44ef35ba29f76`；文件 v3 不代表應用版本 3.0 或已完成驗收。

完整權威文件為 [Owner 的 Project Plan v3](docs/v3/PROJECT_PLAN_V3.md)，先讀 [Dev Team Brief](docs/v3/DEV_TEAM_BRIEF.md)。本頁是 repo 的現行入口；舊根目錄文件保存在 [history](docs/history/pre-v3-0.3.1/README.md)，其角色安排與執行模式已由 v3 取代。

2026-10-12 Owner 補充的首次使用順序優先於原稿的 Hermes-first 登入安排：Linux／網路 → 選一個 coding CLI 並官方登入 → CLI 協助準備隨產品交付的受測 Hermes → Hermes 驗證 → LINE／可選 Telegram 等通訊 → 自動建立或啟動 HA → 原生家庭設定及助手引導。後端保存工作進度，僅在必要的帳號、掃碼及家庭授權等待使用者；詳見 [自動 onboarding](docs/ONBOARDING.md)。這是新增產品流程，不代表兩個 coding worker 或 E1／E2 已完成。

0.5.1 的 [E2E／架構審查](docs/reviews/2026-10-12-E2E-ARCHITECTURE.md) 是本輪基準。0.6.0 補上 Host 實際 coding 派工／role auth／egress、獨立測試、PNG 預覽、可信停止核對、全鏈 retention、Hermes 家庭提案 adapter 和固定同 schema Controller updater；使用及精確限制見 [Workers](docs/WORKERS.md)、[Updates](docs/UPDATES.md)。真帳號、Home App research 與家庭產品驗收仍分開記錄。

0.8.0 增加 HAOS App 內的 Home Codex research／development、唯讀現場觀測、獨立 verifier 與停止後檢查點。先交付原始碼／App；0.7.0 安裝媒體證據不沿用成 0.8.0 ISO 驗收。真雲端研究品質、家庭 helpers 與更新／退版仍分別驗收，詳見 [Workers](docs/WORKERS.md)。

0.9.0 讓主機與家庭現場的開發助手都能在網頁啟用、登入及使用（家庭現場自動裝進 HA），補上真帳號可靠性與停止重試，並把介面整理成每個狀態一句說明、一個主要動作。變更見 [更新紀錄](CHANGELOG.md)；真帳號與真家庭驗收仍分開記錄。

## 固定角色

2026-10-16 Owner 補充：[通用設備探索與接入](docs/DEVICE_DISCOVERY.md) 納入產品主線。家庭案例用來驗證多來源盤點、辨識、整合引導及逐能力驗證，不把特定品牌或路由器寫成使用前提。先交付 HA 唯讀探索快照與原生交接，再分段加入其他來源與對話引導；實體設備、帳號與網路覆蓋仍逐項驗收。

最新方向採原生 HA 日常介面＋「智能管家」Ingress App，連回同一外層 Hermes／Controller／case。HA 保留官方 Core／Supervisor／HAOS 更新路徑；外層只核對並有限次修復本產品 App，不復原使用者手動停止、移除或改動的設定。原生 Assist／語音的 Conversation adapter 另行開發，不能把 MCP 工具接點當作已完成的對話整合。

0.7.0 首次設定使用發行隨附的 Hermes image digest，保留離線相容性檢查；追上游由開發端產生候選。外層 CLI／Hermes 相依維持釘版，不以完成全 Linux 更新平台為本輪前置條件。HA 更新後未知 Core 的家庭寫入仍可能暫停，須另取得相容證據。具體範圍見 [發行策略](docs/RELEASE_POLICY.md)。

2026-10-12 後續介面方向：完成家庭建置後以原生 Home Assistant 作為日常首頁，智能管家作為協助入口，外層保留初始化與救援。Hermes／HAOS／Linux 的相依集中在獨立 adapter，以固定契約、能力探針及候選驗證處理上游變動；不保證任意未來版本相容。實作與限制見 [adapter 邊界](docs/ADAPTERS.md)。

外層 Linux 上的一個 Hermes 是唯一主要協調者，管理需求、case、分工與證據。Host Coding Worker 研究產品／上游、修改隔離 checkout、跑 tests 與 preview；Home Coding Worker 在真正的 HAOS App 內自主追加現場查證、寫 scratch／重現程式、跑輕量測試並提出 patch。兩者可使用同一 provider，認證、session、workspace 和能力分開。

保留 Ubuntu＋KVM＋官方 HAOS、現有 Web/Gateway、Controller、Runner 的 `host/haos` 路由、TLS/pairing/jobs、家庭 Engine 和 Host broker。新增薄 Hermes/MCP adapter 與工作契約，不建立第二套通訊 receiver、家庭政策或遠端派工平台。簡單查詢由 Hermes 使用固定工具；有互補研究價值時才派 worker。

## 交付順序

| 階段 | 可使用的成果 | 必須另外取得的證據 |
|---|---|---|
| E0 | 既有 laptop profile 的工程 bundle/ISO；Hermes 接點／明確登入狀態；可跳過 HA/通道；固定診斷及不靠 Web 的救援 | 0.5.1 精確媒體在一次性 VM 安裝／還原／重啟已通過；Owner laptop 現場硬體／磁碟選擇仍待驗 |
| E1 | Hermes 派 Host Worker 真正修一個問題，交 diff、測試及隔離 preview；可取消／接手 | 真實模型與 CLI、工具執行、隔離負向測試、舊 writer 確實停止 |
| E2 | HAOS App 內真實研究／小型程式驗證；內外互補證據；helpers 提案→批准→觸發；固定同 schema 更新／退版 | 真正 inner runtime、家庭流程、候選故障回復與整機重啟 |
| E3 | 逐台實體設備、相鄰 Core 版本、72h 運作、離機恢復和三個單／雙 worker 對照案例 | 逐項現場紀錄；不阻擋先用 E0/E1 |

第一條 profile 沿用 `host/profiles/laptop-amd64.json`。本輪不加 provider、distro、第二套 Hermes 或新的 UI framework。內層未準備好不阻擋外層 Support，但另一個外層容器或固定 JSON executor 不能算完成 E2。

## 自主開發與正式執行

Research 可在隔離工作區寫分析程式；Development 可修改自己的 clone、跑 tests 與產出候選。它們對正式安裝、家庭設定與設備保持唯讀。原 `home_operator` 限制繼續有效，新增 coding profile 的自由必須由實際 sandbox 隔離，不能全域開啟舊 Runner 工具。

正式家庭變更與部署仍經 Controller／Host broker／固定 updater：精確提案、Owner 批准、備份、journal、讀回及核對。公開家庭資料、真實帳號、磁碟選擇與 Live 授權需要對應 Owner 操作；隔離範圍內開發與自動驗證不增加逐 PR 人工 gate。

## 三條開發工作線

A 負責 Hermes／Controller／身份與持久派工；B 負責兩層 worker／sandbox／診斷；C 負責 Web／preview／交付恢復。Owner 授權三條工作線並行，指定一位整合者管理共同契約；同一檔案、workspace 或 session 維持單一 writer。產品 workers 不因此取得遞迴派工權。

16 項工作及相依以 [Backlog](docs/v3/BACKLOG_V3.md) 為準；32 個分階段驗收以 [Pilot Acceptance](docs/v3/PILOT_ACCEPTANCE.md) 為準。最新開發／驗收紀錄放 [PROGRESS.md](docs/v3/PROGRESS.md)，與六份原稿分離。既有 `0.3.1` 測試只是基準，不會自動算成新增 v3 功能已通過。
