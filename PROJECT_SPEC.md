# Agentic Smart Home — 現行 v3 規格入口

Steering v3.0；匯入基準是應用 `0.3.1`、commit `3ad476a712009469521c3f9838a44ef35ba29f76`。完整規格以 [Owner Project Spec v3](docs/v3/PROJECT_SPEC_V3.md) 和 [Worker Contract v1](docs/v3/WORKER_PROTOCOL_V1.md) 為準；以下是工程落地的共同不變條件，不是所有設計均已實作的聲明。當前證據見 [進度表](docs/v3/PROGRESS.md)。

首次使用順序依 Owner 2026-10-12 指示改為 CLI 先行，見 [自動 onboarding](docs/ONBOARDING.md)。0.5.0 新增持久 `bootstrap_v1` 流程、短效本機桌面登入、首次 Hermes 最新來源相容性檢查、LINE／Telegram 配對碼與 HA 官方瀏覽器授權。仍使用 Controller schema 2；新增記錄與既有家庭 journal 分開，不宣稱能原地退回 0.4.0。

## 路徑與權威

```text
Owner → 既有 Web/Gateway → conversation adapter → 外層 Hermes
                                                ↓ MCP 工具
                                            Controller
                          ┌─────────────────────┼──────────────────┐
                    Host Runner           Home Runner        家庭 Engine
                    隔離 coding           真 HAOS App         精確提案／批准
                          ↓                     ↓                   ↓
                   source/tests/preview   source/scratch/tests     HA APIs

主機操作／版本切換：Controller → 固定 Host broker／獨立 updater
證據：兩個 workers → 同一 Controller case → Hermes／Owner
```

Hermes 不持有家庭 Owner／host root 權威。Controller 驗證身份、scope、capability、job、proposal 與執行；不在自身高權限 process 執行模型程式。Runner broker 由 server 配對決定目標／身份／限制，不接受模型指定 URL、UID、mount 或 shell hook。單一 bot 維持一個 receiver。Hermes 原生 `delegate_task` 不等於我們的遠端 worker adapter。

## 執行模式

| Profile | 可做 | 不可取得 |
|---|---|---|
| `orchestrator` | 記憶、受控查詢、case 分工與提案 | 任意 host shell、Owner cookie、正式批准 |
| `research` | 允許來源研究、case-scoped 診斷、可寫 scratch、小型重現及測試 | Live 寫入、管理 token、任意 LAN 探測 |
| `development` | 任務獨立 clone/workspace、修改、tests、patch／preview | Live checkout／DB、共用 Git metadata、部署權 |
| `home_operator` | 現有受限結構化 intent 與有限 observations | 新增的一般 filesystem、terminal、browser 工具 |
| `live_verification` | 固定並已授權的驗收 runbook／helpers | 任意 live shell、超出批准的設備動作 |

保留原家庭 CLI 啟動限制。Research／development 以獨立 process、credential、mount、network 與 hard resource limits 實作；prompt 或 UI 開關不算 sandbox。Home Worker 必須在 App 內實測可用邊界，不給 Docker socket、`full_access`、host PID、整份 HA config、Core／Supervisor 管理 token。不可隔離時禁用該 coding mode，保留固定診斷並記錄缺口。

## 工作、身份與恢復

對話依 Owner、channel binding、conversation 映射 Hermes session；provider/scope 變更不得沿用未授權 context。case、job、proposal、operation 各自有不同 ID。job 綁 worker/workspace/lease，server 補上實際 authorization、policy、deadline、budget、digest 和 provider；模型自報 `approved=true` 或 `live_mutation_allowed` 不授權。

既有 Runner `protocol=1` 與新 `work_contract_major=1` 分別握手。提交先持久化再發送並立即回 handle；Controller 和 Runner 都做 key/digest 去重。斷線／重啟先查 exact job，不直接重派。取消需分 `cancel_requested`、`cancelled`、`stop_unconfirmed`；舊程序仍能寫入時不能釋出 writer lease。初期每 worker 一 job、總 coding 並行一個、最多兩輪跨 worker 補查；不允許 worker 遞迴派工。

## 正式家庭保護

既有 policy、entity/action allowlist、typed intent、canonical digest、10 分鐘提案有效期、revision/base hash、ownership ledger、live Owner 身份、備份、journal、staging/readback、activation 與事件核對仍有效。只有 Owner 的正式批准或明確預授權 runbook 能形成執行權。新修訂使舊批准失效；寫入結果不確定時進 `needs_reconcile`。停止 Hermes／workers 不會停止已部署的 HA 原生規則。

Ingress／host 管理身份、Controller／Runner 私有 bearer 及 credentials 分離沿用 [API contract](docs/API_CONTRACT.md)、[Linux 部署](docs/LINUX_PRODUCT.md) 和 [Gateway](docs/GATEWAY.md)。舊 0.3.1 的詳細家庭執行規格保存於 [封存規格](docs/history/pre-v3-0.3.1/PROJECT_SPEC.md)，其中「所有 Agent 都無一般工具」僅適用保留的 `home_operator`，不再限制隔離的 research/development。

## 證據、preview、更新

每份 observation 帶 `observed_from`、`target_component`、`observed_at`、`version_refs`、`collection_method`、`evidence_id`、digest、redaction 及 limitations。App probe 與透過 Controller 的 Core API 是不同來源，不能由 App 連線正常推論整個 Core／HAOS 正常。Fixture、真 CLI、real cloud、Core、HAOS、helper 和 physical device 分開記。

Artifact 經 broker 檢查大小、路徑、symlink、特殊檔案及 secrets；HTML 和 findings 當作不可信內容。Preview 不能使用正式 cookies、管理 origin、HA 登入備份或連正式設備；只換 localhost port 不等於隔離。UI 依 `DESIGN.md` 與既有元件演進。

固定 updater 只接受可信 publisher、固定配置和版本 manifest；hash 不是發布身份。第一版只做相容 schema/protocol 的應用更新，保留舊 artifact、一致資料快照及不靠 Web 的退版工具。新增 case/job schema 必須明示 migration；不能盲目恢復舊 DB 蓋過新操作。不在同次混升 OS、Core、CLI 或網路。

**0.4.0 實作註記：Controller 使用 schema 2。** SQL tables 沿用，但新增 Gateway `coordinator` 與 v3 records 的資料語意不相容於 0.3.x。開啟 schema 1 時，先產生 mode-0600、SQLite 一致性的 `.before-v2-<uuid>.sqlite3` 私有快照，再於交易中更新版本 marker；settings、audit、records 和去重紀錄保留。新 DB 為 2，舊 binary 拒絕 2。這是明確的向前資料遷移，不代表已提供同格式 updater／原地降版；restore 工具接受 1 或 2，保留原格式，不把 2 降為 1。

外層 Controller 的 context 保存設定同步至 Hermes 私有 `/v1/retention`，範圍 1–7 天。同步失敗回 503 並保留 Controller 原設定，不能只清一份 DB 就宣稱收回所有前文；Hermes 縮短期限時先停止 turn，再清除逾期資料。具體操作和敏感遷移快照處理見 [OPERATIONS](docs/OPERATIONS.md)。固定 updater／rollback 和完整現場恢復仍待另行交付與驗收。

每個能力按 [Pilot Acceptance](docs/v3/PILOT_ACCEPTANCE.md) 分階段啟用。硬體／帳號／內層執行的未測項保持 `not_run` 或具體 `blocked`；不得用既有測試、模型自己的 passed 或三個 Agent 意見一致取代驗收。
