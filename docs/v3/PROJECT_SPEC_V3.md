# Agentic Smart Home — Project Specification v3
## Hermes 協調、雙層研究開發、受控家庭執行

**Steering v3.0｜2026-10-11｜設計基線，非已實作聲明**  
Repo：`teddashh/agentic-smart-home`，`3ad476a712009469521c3f9838a44ef35ba29f76`。[來源](SOURCES.md#r01)

MUST＝相關能力啟用前必要；SHOULD＝預設做法，例外需記錄；LATER＝不阻擋第一版。本文與 [Plan](PROJECT_PLAN_V3.md)、[Worker Protocol](WORKER_PROTOCOL_V1.md) 一起取代 v2 的角色／開發模式描述；原生 HA 家庭政策、批准、備份及驗證不因換 runtime 而移除。

## 1. 角色與系統權威

| 系統 | 唯一負責的權威 | 不可越界 |
|---|---|---|
| Hermes | 推理、調查分工、任務背景與人機互動 | 不生成有效人類批准，不持有 HA owner／host root 憑證 |
| Controller | principal、capability、case/job 記錄、家庭 policy／proposal／approval／operations | 不在自己的高權限程序執行模型產生的 code |
| Host／Home Worker | 在指定環境自主研究、workspace 編輯及測試 | 不改自己的權限，不遞迴建立新控制者，不直接部署 |
| Worker broker | 固定 CLI 版本、job 接收、執行身份、資源限制、取消、產物匯出 | 不接受 model 指定任意 host URL、UID、mount、shell hook |
| Host broker／Updater | 固定主機操作、可信發布套用與恢復 | 不變成通用遠端 root exec |
| HA Core／Supervisor／HAOS | 家庭狀態、規則與原生系統生命週期 | 不因 Agent 記憶與意見而更改真實觀測 |

一個主 Hermes，兩個 worker 身份（`host-coder`、`home-coder`）；工作模式不是新增三種 bot。MUST 用 server-side identity 解析角色，不能採信模型在 JSON 自報 admin。

## 2. 可部署的元件邊界

外層沿用 Controller／Gateway、Host broker 和 Runner 基礎。新增獨立、非 root 的 Hermes runtime；Controller 的對話 adapter 呼叫它，Hermes 透過受限 MCP 工具回到 Controller 的業務 handler。沒有第二套家庭 policy，也沒有兩套相同 bot 的 polling。

Host Worker 由可信 launcher 建立任務專用沙盒。Home Worker 是由 Supervisor 管理的 HAOS App 內的實際 CLI，不在 HAOS 唯讀主機檔案系統安裝開發工具，也不把 host 容器冒稱 inner。官方 App 具有獨立的 API／namespace／mount 設定，不能由安裝位置推定權限。[來源](SOURCES.md#u04)

SHOULD 保留現有受限的家庭操作 Runner 配置；research-capable worker 使用獨立部署配置。可共用程式碼與映像，不能共用寬權限程序。若現有 App 無法把通用開發 code 與 broker 秘密／家庭權限隔離，使用單獨的低權限 research worker App，而非放寬所有舊 Runner。這是隔離元件，不是多一個管家或多一套 LLM 框架。

## 3. Hermes adapter 與登入

Hermes 的 MCP、API／session 接點可作整合基礎，但具體版本和啟動參數需由開發團隊用實際 binary 驗證與鎖定。[MCP](SOURCES.md#u01) [API](SOURCES.md#u03)

Adapter MUST 實作 create/continue conversation、run/status/events、cancel、capabilities 和錯誤正規化；對應方法名是本產品契約，不假定 Hermes 任一版本都有同名接口。長任務提交回 handle，由 Controller 保存狀態；不得靠 Web request 一直開著才不會丟任務。

Identity mapping：`owner_id + channel_binding + conversation_id → hermes_session_id`，case 可連多個 jobs。不同使用者／provider／scope 的 context 不自動混用；跨通道延續同 case 必須由已綁定 Owner 明確選擇。Gateway 保存必要訊息／連結，Hermes 負責模型上下文；家庭操作狀態仍由 Controller 權威保存。

Hermes API key 只由服務端 adapter 使用；瀏覽器不直接得到可讀全部 Hermes sessions 的 key。Hermes MCP service identity 與 Owner cookie／批准權分開。API／MCP 僅在受控網路可達、使用驗證的服務身份，不直接公開到 Internet。

Hermes 主程序不在 host 上提供無限制 terminal／filesystem／browser。需要這些工具時必須指向與 worker 同級隔離的 scratch／dev backend；不得利用原生工具繞過 Controller。小型低風險工具查詢可以直接完成，不為每個問題再派 worker。

## 4. Provider 與 account 隔離

Hermes、Host Worker、Home Worker 各有私有認證狀態。MUST 在每個實際環境驗證登入、一次真實推理、失效／重登與計費來源；不假設同一訂閱可無限制並行或所有 token 可互換。

先支持一條已驗證的 CLI provider 路徑，不要求四家都完成。不得抄另一個 app 的私有 OAuth client、不繞過官方限制。不把使用者 MFA、refresh token 或 setup token 寫进 evidence／prompt／Git。

不共享可寫 `~/.hermes`、CLI profiles 或 SQLite；每個角色的檔案與 Unix 身份隔離。既有 Runner pairing token 留 broker，CLI 子程序只得到自己的 provider 必需認證與本 job 短期能力。清除 Supervisor 自身 token、其他 provider env、未知 MCP 和個人 Git credential helper。

**剩餘風險需誠實說明：** coding CLI 若能以同一身份讀自己的 credential，任意 workspace code 也可能接觸該 credential；非 root／容器不能把這個風險變零。優先獨立、限額的帳號／key，適用時用可信 proxy，限制 egress 與額度。不得宣稱「模型永遠看不到任何自己的登入資料」。

## 5. Execution profiles

| Profile | workspace | 外部研究 | 現場資訊 | 正式寫入 |
|---|---|---|---|---|
| `orchestrator` | 無 live checkout；僅必要記憶／scratch | 受控研究工具 | Controller 去密查詢 | 只能提出提案 |
| `research` | scratch 可寫；指定 source checkout 唯讀或可生成分析副本 | 已授權來源／代理 | case-scoped read-only 診斷工具 | 不可 |
| `development` | 自己的 clone／worktree 可寫；tests 可執行 | 已授權依賴與來源 | 以去密資料重現 | 不可 |
| `home_operator` | 保留現有受限 intent 模式 | 不新增通用工具 | 已授權 HA 狀態 | 仍由 Controller 批准後執行 |
| `live_verification` | 無任意 live shell | 非必要關閉 | 固定驗收 runbook | 只執行事先批准的具體測試／helpers |

Research 是「對正式系統唯讀」，不是禁止產生程式。兩個 workers 都 MUST 有在自己的 workspace 寫小型分析／測試程式的能力；內層不必有外層完整 build toolchain 或 browser。

Profile 的差異必須落到 process、credential、mount、網路和 broker API；同一高權限程序的 prompt／UI 開關不算隔離。現有 provider 命令限制不可全域撤除，要為新 profile 選擇實測過的工具模式。[基準](SOURCES.md#r04)

## 6. Workspace、程序、檔案及網路隔離

### 6.1 工作區

可信 launcher 依 repo allowlist 和 commit 建立 dev clone；其內可建 worktree。不得掛正式安裝目錄或 Live checkout 的共用 `.git` 管理目錄。不同 jobs 不共用可寫 Git metadata、env、artifact 目錄或 provider profiles。

Worker 可以執行完整的開發命令，不靠列出幾個命令假裝安全。候選 tests、依賴安裝腳本和下載的原始碼都是不可信程式。取消時停止整個 job 的子程序與測試 server，保留受限的工作區快照供繼續；不把 worktree 刪除等同於已停止程序。

Artifact 匯出經可信 broker：限制檔案數與大小，拒絕路徑穿越、指向外部的 symlink、特殊檔案與未知封裝；檢查 secret。來源 HTML、測試報告與模型摘要以不可信內容顯示，不能在管理 origin 當作可執行 HTML。

### 6.2 網路

Development／research 不能任意連 Live HA、Controller 管理端點、Supervisor、其他 Apps、私有 LAN 或雲端 metadata。網路控制要在沙盒／可信代理／網路層強制，不只設定 `HTTP_PROXY` 或提示詞。

允許官方來源、核准的依賴來源及 provider egress。需要擴大研究網站時可在 case 範圍授權，不為每一個公開頁面逐次打擾；禁止把家庭識別資訊或 secrets 放在公開搜尋 query、URL 或上傳內容。

Live 診斷透過限定的 read-only tools。需測試某個現場 endpoint 時使用事先定義的 probe（明確服務、方法、超時、目標和回應上限）；不能讓 coding shell 自由探測家中所有位址。HTTP GET 不是通用無副作用證明。

### 6.3 HAOS 內層可行性驗證

不給 Home Worker Docker socket、`full_access`、host PID、整份 HA config 或管理 token；保留 AppArmor。不能假定在 App 裡可巢狀啟動 Docker、任意建立 namespace 或寫 cgroup。[來源](SOURCES.md#u04) [來源](SOURCES.md#u05)

首個 Home Worker 任務應輕量：讀固定 source、取得受控診斷、產生並執行小型測試。實測 broker 與子程序 credential 邊界、直接 LAN／Supervisor 存取阻擋、取消与限額；無法隔離時，只禁用該通用 coding profile，不取消整個外層試跑。E2 完成前仍必須解決並取得 inner coding 的真實證據，不能永久用 dummy／host executor 代替。

## 7. 診斷的資料路徑與「看見更多」

一般 HA states、registry、actions 和 events：走既有 Controller→HA API。特定 host 資訊：走固定 Host broker。Home Worker 的當地測試：在 App 可見的範圍內執行，或呼叫受授權且標明來源的診斷 collector。[通訊基礎](SOURCES.md#u06)

每份觀測 MUST 有 `observed_from`、`target_component`、`observed_at`、`version_refs`、`collection_method`、`evidence_id` 及 limitations。Home Worker 透過 Controller 讀到 Core 資料時，不得把 `observed_from` 填成「我直接讀 Core 檔案」。

應区分 host、guest-network、HAOS App、Core API、Supervisor API、preview 和物理設備。App 的 DNS／TCP 測試不能直接代表 Core 的 DNS／TCP 測試。無法看到的資訊回 `not_available`，不能為方便診斷就讀所有 `.storage` 或秘密設定。

可以「以受控 API 把更多資料送給一個 Agent」作 baseline；增加 Home Worker 的成效以自主重現與可反證的證據量測，不以物理位置推論一定較準。

## 8. 工作提交、回報與主動事件

MCP 提供 `worker.submit/status/cancel/result` 等自家工具；Controller 沿用既有 TLS jobs 傳輸，增加工作契約和能力握手。既有 `protocol:1` 不被新 `work_contract_major:1` 偷換語意；相容性不符時只拒絕新 research 功能，保留舊有路徑。[基準](SOURCES.md#r03)

提交先持久化 case/job 與 request digest，再發送；回傳 job id。Worker 完成可由 polling 或受認證事件通報，Controller 記錄 event id／sequence 與去重狀態，再將經過濾結果交給 Hermes。SSE 可後做，MCP 不需要長時間佔用一個 tool call 等待整個研究完成。

CLI 能透過短期 job 能力請求更多去密觀測，不能用 pairing bearer 呼叫其他 jobs、UI approval 或通用 HA API。回呼路徑僅作受控工具與產物；不要反向暴露目前只應給人類用的管理 listener。

內層主動事件先由一般程式篩選、限流、合併，綁 case 才喚醒 Hermes；不直接把每個 state_changed 傳 LLM。由我們的 job 造成的事件保留 correlation，避免自觸發循環。首版無授權 cron／無限自動 research。

## 9. 單一寫入者、重試與人工接手

每個 job 綁固定 `worker_id + workspace_id + lease_epoch`；broker 管控單一 workspace writer。Owner／BAT／其他人要接手時，撤銷或暫停自動 writer，確認舊程序停止；恢復时發新 lease。**worktree 本身不是鎖，process 還能写磁碟時只換 lease 字串也不算 fencing。**

重複 submit 相同 key/digest 回同 job；同 key/different digest 拒絕。斷線先查 job，不用新 id 重做。Research 有限重試可以重跑；套用 patch 先查 base/tree hash；正式副作用由 Controller 的 operation journal 核對。沒有跨所有程序的 exactly-once 保證。

Worker heartbeat 失聯後不立即另派第二個寫入者。先停止／隔離舊 job 或保持 `interrupted`；可保留 findings 與部分產物。Cancel 狀態需区分「已要求」與「程序已停止」，已完成的物理操作不能撤回。

## 10. 開發產物與獨立驗證

每次開發回傳 base commit、changed files、patch/hash、測試及 preview refs、限制與後續驗證。兩個 worker 不同時寫同一分支；跨層交換 evidence 與 patch，不互掛 workspace。

Verifier 接收問題、候選版本和固定驗收條件，不先收到要求它認同的結論。需要時讓兩者先獨立調查同一症狀，之後再比較假設；二者用同一模型仍不能把一致意見當獨立證明。

Agent 可以增加或修改測試；但刪除断言、改成 skip、替換 fixture 或修改安全基準需顯示。發布必跑由可信來源鎖定、不能被候選 workspace 直接改掉的關鍵 smoke／負向測試。最終 evidence 連到真正 exit code、環境與 artifact；模型自己打印的 passed 不是測試平台證明。

## 11. UI／preview／資料分享

沿用既有 Web，增加同 case 的角色狀態、工作取消／接手、觀測位置、facts/inferences/unknowns、候選差異與發布狀態。Owner 正常對話不必每次手選 `/host`／`/home`；保留進階選項，Hermes 路由仍受後端授權。

「回報這個畫面」只收 Owner 選擇的 route、版本、任務、預期／實際行為和去密附件。截圖先預覽遮罩；不常駐擷取全桌面、相機、麥克風或任意私人瀏覽器。自動去密不保證零外洩，公開 issue／跨 provider 分享另有明確 scope。

Preview 與管理 UI 使用隔離來源、cookie 與資料；**僅換 localhost port 不等於 cookie 隔離**。測試 browser 不帶正式 Owner cookie，也不能導航到正式批准入口。候選前端或報告不得在 trusted admin origin 執行。

UI 修改交前後畫面和互動測試：viewport、字型環境、鍵盤／表單、錯誤／斷線、刷新續接与後端結果。遮罩不能蓋住待測控制項。一般 UI 用 fixture；現場資料語意由受控 Home 驗證補足。

## 12. 家庭寫入沿用既有 Engine

保留明確 entity/action allowlist、固定 intent 編譯、原生驗證、資源 ownership ledger、base hash、提案 digest／expiry、Owner 批准、備份、寫入讀回、真正觸發與 `needs_reconcile`。

Home Worker 自主研究的結果可成為 proposal 輸入，不能成為 bypass。Hermes 的工具批准、Worker 的 `ready`、CI 綠燈不等於家庭 Owner 批准。改提案要明確 proposal id／revision／base hash，使舊批准失效，不憑聊天「改剛剛那個」猜資源。

首版 Live 測試從專用 helpers 開始，再逐台開放低風險設備。門鎖、車庫門、警報解除、加熱／烹調設備、未知用途插座、對外分享影像不納入早期自主測試。停止 Agent 不等於停止它以前部署的原生規則。

## 13. 同 schema 更新、資料與退版

不熱改 `/opt/agentic-home`，不讓 candidate 安裝腳本取得 Controller／host 權限。用受信任 repo/CI 的 artifact manifest，包含 commit、digest、runtime versions、DB schema、work contract／Runner protocol、HA 相容範圍和測試證據；由固定 updater 驗證來源後套用。manifest 不允許任意 shell hooks／mount／compose overrides。[既有更新缺口](SOURCES.md#r07)

最早只支援資料相容的外層應用更新；內層 App 走原生 Supervisor 更新並另驗版本相容。一次不混升 Ubuntu、HAOS、Core、CLI、網路與 schema。Hermes 本身更新也走版本鎖定與回歸，不在模型任務中自我 `update`。

套用前暫停新 live mutations、結清或標記不確定工作、保存 Controller 一致快照和舊 artifact。更新後先做登入／配對／資料讀取／HA 可達的無副作用 smoke；已批准的 helpers 測試另做。失敗可依此次預授權退回舊 artifact，不依賴壞掉的 Web 再點批准。

加 job/case 表需要 migration 設計：新增 schema 在第一個 v3 候選鎖定；不得仍聲稱是無 schema 改變。E2 的快速退版需兩個版本都能讀同一 schema，或有測過的 backward-compatible migration；不以強行回舊 DB 蓋掉新操作解決。

HAOS 磁碟快照、Core backup、Controller DB、Hermes memory、worker profiles 各自備份與說明。默認診斷／普通產物不含任何 credential；加密離機備份與解密材料分開。恢復舊 guest 時必須核對較新的 Controller ledger。

## 14. 資源、可用性與留存

初始建議（設計預設，非已量測效能）：每個 worker 同時一個 job；總 coding 並行一個，通過實機負載後最多兩個；light research wall timeout 300 秒、outer dev 900 秒，可由 Owner 在固定上限內調整；最多兩輪跨 worker 補查，不允許 worker 再生成整棵 agent tree。

Broker MUST 限制總輸出、artifacts、磁碟容量與執行生命週期；CPU/memory/pids 以實際可用的 OS/container 機制強制。既有 CLI RSS 取樣不是完整子程序／browser 的 hard limit；單設 VM RAM 也不足以防 Home Worker 擠垮同 guest 的 Core。[來源](SOURCES.md#u07)

背景接單不代表持續推理。UI 顯示每角色用量的可得數據、不可得標 unknown；等待 jobs 不啟動無限模型輪詢。高負载先暫停／停止開發，保留 HA 與救援。

預設案例內容／細節 evidence 七天、必要 audit 三十天；Owner 可縮短。活躍 case、已部署產物的執行依據及去重 tombstones 另保留最小必要記錄。刪除／權限收回要同步處理 Gateway、Hermes context、worker checkpoints 和 artifact ACL；不能只刪一份 DB。

## 15. 現有 repo 的變更位置

| 位置 | 工作 |
|---|---|
| `PROJECT_PLAN.md`、`PROJECT_SPEC.md`、`AGENTS.md` | 更新 v3 角色與邊界；保留舊版於 history，記錄資料／執行權限不變事項 |
| `controller/agentic_controller/gateway.py` | 維持入口與續接，映射 Hermes session/case；不再另造通用推理 loop |
| `controller/agentic_controller/runner.py` | 保留 TLS／pairing／jobs，增加能力握手、async submit/status/cancel/result 語意 |
| `controller/agentic_controller/` | 建議新增 `hermes_adapter`、`worker_tools`、`cases`；仍是同一 Controller，不開新平台 |
| `runner/agentic_runner/providers.py` 與部署配置 | 保留 home_operator 命令；為 research/development 使用實測隔離 launcher/profile |
| `host/` 與 HAOS worker App | Host／Home 各一個 job 沙盒、固定診斷與正確觀測位置；內層無 Docker socket |
| `web/` | 案例、角色進度、來源證據、接手／取消、分享預覽、candidate/live 分離 |
| `scripts/`、CI、`docs/OPERATIONS.md` | 固定候選 artifact、可信 smoke、同 schema updater／獨立恢復與實測記錄 |

新模組名稱是建議，不代表已存在。團隊可調整工程實作，但不能把本輪核心降成「只有外層 coding，內層永遠是固定執行器」。

## 16. 能力啟用與交付真實性

按照 [Pilot Acceptance](PILOT_ACCEPTANCE.md)逐能力啟用。Sandbox 越權阻擋該 coding mode，不阻擋安全的 Support；未接 Nest 不阻擋 Hermes＋Host Worker；但 E2 雙層宣稱必須有實際內層 code 執行／研究證據。

採 `not_run / blocked / passed / failed` 與多維環境記錄；不是文件存在就完成，也不是 0.3.1 原有測試通過就證明 v3。這份文件沒有替產品執行任何新功能測試。
