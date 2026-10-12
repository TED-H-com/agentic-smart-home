# Agentic Smart Home — Project Plan v3
## 一個 Hermes，兩個 Coding Worker，邊用邊開發

**Steering v3.0｜2026-10-11｜Owner：Ted Huang**  
Repo 基準：`main@3ad476a712009469521c3f9838a44ef35ba29f76`（應用 0.3.1）。[來源](SOURCES.md#r01)

本文取代 v2 的角色安排與開發優先順序；不推翻既有 HA 執行／授權機制。所有 E0–E3 新能力是計畫，除非另有對應證據，狀態均為未驗證。文件版本 v3 不代表應用要跳成 3.0.0。

## 1. 產品目標

**先交付一台 Owner 在家能使用、並能靠 Agent 持續改善的家庭開發主機，再逐步提高它承擔家庭任務的程度。**

外層採現有 Linux 主線，內層保留官方 Home Assistant OS。外層 Hermes 與兩層 coding workers 幫忙安裝、research、debug、程式開發、UI/UX、測試及升級驗證。推理使用獲授權的雲端模型；本地跑 runtime、CLI、工具、資料與介面，不增加本地大模型的前置要求。

Home Assistant 負責日常確定性的家庭自動化；不是每次感測器變化都呼叫 LLM。正式家庭状态由 HA 提供；聊天記憶不能當作即時狀態。

## 2. 本輪已決定的架構

| 元件 | 定位 | 初期運作方式 |
|---|---|---|
| Hermes | 唯一主要任務協調者與使用者助手 | 常駐服務，按需推理；簡單查詢可直接用工具 |
| Host Coding Worker | 外層產品／系統工程師 | 常駐接單服務，按任務啟動 CLI 與隔離 workspace |
| Home Coding Worker | HAOS 內層研究／驗證工程師 | 在 HAOS App 執行 CLI，自主追查、寫小型重現及測試 |
| Controller／Gateway | 身份、工作記錄、工具授權、家庭提案與執行、UI | 沿用，增加 Hermes adapter 與少量 worker 記錄 |
| Host broker／Updater | 固定主機操作與可退版發布 | 不執行模型任意程式；有独立 Owner 恢復入口 |
| HAOS／Core | 家庭狀態、integrations 與已部署規則 | 沿用官方上游；不 fork Core 或 OS |

「三個 AI 角色」不等於要三家 provider、三套聊天平台或三個一直燃燒 token 的程序。兩個 workers 可以使用同一家 coding CLI；Hermes 自己的模型認證與 worker CLI 認證分別驗證，不預設訂閱／token 能互換。

### 相對 v2 的差異

| v2 或前次簡化 | v3 決策 |
|---|---|
| 外層泛稱 System／Development Agent | 明確採 Hermes，盡量沿用其通用 Agent 能力 |
| 外層 Agent 自己承擔所有 coding | Hermes 可以做小任務，主要 repo 修改可派 Host Worker |
| 內層只做 typed 家庭操作或固定 Bridge | 保留操作邊界，另外給 Home Worker 自主研究與 workspace coding |
| 外層才有隔離 Dev Session | 兩層都有受隔離的 workspace；內層以輕量研究／重現為主 |
| 雙 Agent 只交換一段摘要 | 同一 case、分工、證據來源與觀測位置、候選版本、獨立驗證 |
| 多做入口和 provider 才像完整產品 | 先一個 Web、一家可用模型／CLI 路徑與一個 laptop profile |

本輪不重新比較 Hermes／OpenClaw；先把已同意的 Hermes 路線實作、量測，不同時導入兩套協調 runtime。

## 3. 為什麼要內外各一個 coding worker

這是產品假設，需要實測，而不是「Agent 越多必然越準」的承諾。

Host Worker 擅長查產品原始碼、上游文件與主機診斷，修改跨模組程式並建立 preview。Home Worker 的價值是結合授權的 HA 現場資訊，在內層自己進行下一步調查、寫驗證程式與比對候選結果。

二者資訊若完全相同、都只重述 Hermes 的假設，新增 worker 沒有價值。每次派工要明確寫出不同問題與產物，例如「查相容性變更」對「查現場 entity 的真實能力」。

**觀測位置不能誇大。** App、Core、Supervisor、HAOS host、外層 host 是不同視角。Runner 連線成功不等於 Core 連線成功；inner 執行不等於取得整個 HAOS 的任意權限。每份 evidence 都要記觀測位置和來源。[上游 App 權限](SOURCES.md#u04)

## 4. 現況可沿用什麼

已查核的基準有外層 Linux／HAOS VM 主線、Controller／Runner、家庭提案與執行；Gateway 已有對話續接。Runner 客戶端區分 `host` 與 `haos`，使用配對、TLS 指紋與 jobs 路徑。這些不是本輪重造對象。[架構](SOURCES.md#r02) [Runner](SOURCES.md#r03) [Gateway](SOURCES.md#r05)

目前 CLI 的啟動命令仍以限制工具的結構化輸出為主，不能當作已支援通用 research／coding。新版必須增加獨立 execution profiles 與實際隔離驗證，而不是改 README 宣稱可以。[來源](SOURCES.md#r04)

0.3.1 驗證文件把真實雲端登入、外部通道收發和實體硬體列為未完成證據。這份 plan 也沒有替 Owner 登入、執行 laptop 安裝或重跑產品測試。[來源](SOURCES.md#r06)

## 5. 目標通訊與權威

```text
Owner ── Web／既有通訊 Gateway ── 對話 adapter ── Hermes
                                                    │
                                        MCP 工具：派工、查詢、提案
                                                    │
                                             Controller
                         ┌──────────────────────────┼─────────────────────┐
                         │                          │                     │
                  Host Worker Broker         Home Worker Broker     家庭 Engine
                         │                   （HAOS App）                  │
                  外層隔離 coding CLI                │                 HA APIs
                         │                    內層 coding CLI              │
                   repo／preview              scratch／研究／測試       原生規則

          主機操作／版本切換：Controller → Host broker／獨立 Updater
          證據：兩個 worker → Controller case → Hermes／Owner
```

Hermes 決定工作策略；Controller 決定工具與工作是否在授權範圍內；Runner broker 決定實際執行目標和隔離環境。MCP 只是工具接口，不自帶任務持久化、身份或家庭批准。官方 Hermes 有 MCP 接點，但自家遠端 worker adapter 仍需實作。[來源](SOURCES.md#u01)

單一對話入口，不等於所有人的記憶混在一起。Controller 映射 owner、conversation、case、Hermes session、worker job；bot token 不同時交给兩套 gateway 接收。內層可回報事件與工作完成，但不能自己再派工給外層或批准正式變更。

## 6. 首版範圍

必須包含：一戶、一位 Owner、一台相容 amd64 laptop；現有 Linux／官方 HAOS；一個外層 Hermes；內外各一個可用 coding worker；Web；真實登入與連線狀態；去密診斷；可恢復工作記錄；兩層隔離工作區；UI 問題回報／preview；helpers 家庭流程；同 schema 應用更新與退版。

不必同時啟用所有能力。E0／E1 先使用外層；Home Worker 完成後進 E2。**E2 不能用「另一個 host worker」或只有固定命令的 inner executor 冒充。**

首輪不增加：新 Linux profile、新 provider、新通訊平台、第二套 Hermes、通用 fleet、Kubernetes／Kafka、向量資料庫、內層完整桌面瀏覽器、整屋自動探索、無限制自我改寫與自動上線。

## 7. 交付順序：逐段可以使用

### E0 — 帶 Hermes 接點的工程預覽包

交付可在既有 laptop profile 安裝的 ISO 或完整安裝包、SHA-256、對應 commit／manifest、可跳過 HA／通道的首次設定，以及獨立 console／救援说明。重用現有 installer，不重做品牌動畫或桌面。

先在一次性 VM 確認候選媒體可啟動、服務可用、没有共用預設秘密或誤選實體磁碟。Owner 在 laptop 完成雲端登入；無帳號者交付 `needs_auth` 的預覽包，不以 fixture 冒充真登入，也不因此一直不交付。

E0 的價值是看得出 HAOS 初始化卡在哪裡，即使 AI 登入失敗也有固定診斷與支援資料匯出。Nest／Alexa／LINE／Home Worker 尚未就緒均不阻擋 E0。

### E1 — Hermes＋Host Worker 能真的開發

至少一條真實模型登入與 Host CLI 工作路徑。Hermes 在同一 case 派 Host Worker：查來源、重現一個問題、改 code、跑測試、提供 patch 和 preview。Owner 可以接手 workspace；接手前由系統確保原 worker 停寫。

必須驗證 workspace、網路、credential 和資源隔離。HAOS 關機時外層仍能對話與診斷；停止開發工作不應停止既有家庭規則。

第一個 patch 可以先由 dev team 放回原發布流程，不等完美 updater UI 才讓 Owner 使用開發助手。單純提供文字修法不算 E1。

### E2 — 真正的雙層研究與可回復迭代

Home Worker 在 HAOS App 內完成獨立 CLI 登入、經授權的現場查詢和輕量程式執行。至少一個研究任務包含「第一輪觀測 → 自主追加查證／測試 → 有證據的結論」，不是只回答傳入摘要。

用同一 case 演示外層查 code／上游、內層查現場，至少發現或排除一項不同的假設。外層做修正，內層或可信測試 harness 以獨立驗收條件檢查。二者的主張與證據分開顯示。

家庭端先完成兩個 helpers 的提案、批准、原生觸發與整機重啟回歸。同時提供同 schema 候選應用更新／退版，不每次重灌 OS。外層與內層 worker 可以有不同應用版本，但工作契約能力握手需相容；不要求兩層一起滾動更新。

### E3 — 家用中逐步穩定與擴大

逐台增加真實低風險設備，解決實際需要的 discovery／USB／網路路徑；按需要啟用既有通道，累積 72h 與更長實測、斷網與重新登入、相鄰 HA 穩定版相容和離機還原。

72h 是觀察長度，不是開發工期，也不是第一天使用外層助手的先決條件。完成 E2 不代表全家關鍵安全設備可無人看管地交給此系統。

## 8. Research／Development／Live 三種範圍

| 範圍 | 自主程度 | 必須限制的部分 |
|---|---|---|
| Research | 自行讀原始碼、查允許來源、透過診斷工具調查、寫暫存驗證程式 | 對正式設定唯讀；查詢與網路探測有明確目標及限額 |
| Development | 在自己的 clone/workspace 修改、跑測試、建立候選產物 | 不掛正式資料、HA 管理 token、主機 socket；沒有發布權 |
| Live | 提交具體操作／部署提案，回報結果 | 由既有 Controller／可信更新器執行，不能由 worker shell 代替 |

Research 的「唯讀」是對 Live 唯讀，不是禁止寫 scratch 或執行驗證程式。內外 worker 的 coding 自由由隔離範圍保護；不是對每一行命令詢問 Owner。

「查網路」不是允許全網掃描；「GET」也不自動等於無副作用。正式端點只走已審查的固定診斷工具；一般程式、第三方套件腳本和候選測試都視為不可信 code。

## 9. UI／家庭／開發三者要連在一起

使用者回報「這裡不對」時，綁定 route、case、任務、版本與他同意分享的畫面／日誌。Host Worker 可以修 UI、開 preview、以測試 browser 比較前後；Home Worker 可確認 UI 對設備狀態的解釋是否符合現場。

Preview 使用獨立 DB、測試身份與 seeded Core／fixture；真實 HA 行為的驗證另走授權流程。**不把正式 HA 備份帶著所有登入還原到上網的 preview**，避免兩套系統同時控制家中設備。

畫面需顯示哪個 worker 正在做什麼、真正的觀測位置、候選產物、證據種類、等待批准與部署狀態。fixture 画面可測 UI，但不能當現場成功證據。

## 10. 更新、恢复與故障獨立性

現有 installer 拒絕直接覆蓋 Live 的保護保留，另加固定 updater。基準運維文件尚未提供無人值守原地更新；此輪將最小可退版路徑提前。[來源](SOURCES.md#r07)

第一版只做明確相容 DB schema／protocol 的應用更新；Ubuntu、HAOS、Core、provider CLI、網路配置分開維護。候選 manifest 來自可信 publisher，Owner 批准確切 digest；SHA-256 單獨不能證明發布來源。

套用時短暫停止需要替換的服務、保存一致資料快照、檢查健康，失敗由不依賴 Controller Web 的工具退回上一版。不盲目恢復舊 DB 蓋掉新操作；有不確定副作用就核對現況。

外層 Agent 能在 HAOS 故障時協助診斷，但同一台 laptop 不是高可用集群。host、磁碟、Controller、供電或雲端全壞時，不承諾三個 AI 可以互救。保留原生 HA、console、停止與離機備份。

## 11. 資源與成本

常駐接單，按需啟 CLI；Hermes 等 worker 時不不停輪詢模型。Controller 可輪詢工作狀態並以去重事件通知 Hermes，基礎狀態更新不消耗推理。

初期每個 worker 最多一個 job；在未知 laptop 上，總 coding 並行預設一個，量測後才可開兩個。大型 Docker builds／browser suites 走外層隔離 build 或現有 CI；不把主機 Docker socket 給模型。

CPU、記憶體、pids、workspace 容量及時間要有實際可執行的限制。HAOS 內層必須查證可用隔離與限額機制，不能假定 App 內能使用 Docker-in-Docker 或任意 cgroup。沒有合格邊界時暫停該 coding 能力，保留外層功能與內層受控診斷。[資源基礎](SOURCES.md#u07)

用量可追溯到 Hermes、Host Worker、Home Worker；供應商沒有可信數據時顯示 unknown。切換 provider 需原有資料分享授權涵蓋，不能暗中把家庭資訊送往另一家。

## 12. 成功指標與較多視角的驗證

不用 PR 數或 Agent 數當完成度。驗收的五件事是：外層真能陪安裝；Host Worker 真能改碼；Home Worker 真能自主研究／執行小型驗證；跨層證據能支持一次修正；不重灌就能更新與退版，既有家庭規則仍正常。

E3 加一個輕量對照：選三個已知真實案例，比較 Hermes 單獨使用工具與 Hermes＋雙 worker 的證據完整度、錯誤假設、重現率、人工補資料次數、模型成本及時間。這是檢驗增益，不是 E0/E1 的阻擋項；結果沒有增益就減少無效派工，而不是追加更多 Agent。

證據分 `not_run / blocked / passed / failed`；環境分開記，不用單一 `real=true`。worker 完成不等於問題解決，Agent 意見一致不等於獨立測試通過。

## 13. 對團隊的實施要求

採三條工作線：A Hermes／派工／身份與工作持久化，B 兩層隔離 worker／診斷，C UI／preview／updater 與驗證。指定一個契約整合負責人；可並行，不新增逐 PR 人工簽核制度。

先交 [Backlog](BACKLOG_V3.md) 的第一切片，再立即接第二、第三切片。現有功能保留，新增能力用明確開關漸進啟用；範圍無關的未完成項不阻擋已安全可用的功能。

真正的現場登入、磁碟選擇、敏感資料分享與 Live 授權由 Owner 完成。其餘研究、編輯、測試和證據整理盡量自動化；不要把「安全」做成永遠只有唯讀聊天，也不要把「自主」做成無限制 root。

技術基線見 [Project Spec](PROJECT_SPEC_V3.md)，派工與結果見 [Worker Protocol](WORKER_PROTOCOL_V1.md)，現場啟用見 [Pilot Acceptance](PILOT_ACCEPTANCE.md)。
