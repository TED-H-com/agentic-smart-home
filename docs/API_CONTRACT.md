# API 契約

Runner protocol major：1；Worker work contract major：1（两者獨立）。JSON Schema 的唯一來源為 Pydantic models；`uv run python scripts/contracts.py --check` 檢查漂移，完整 Controller OpenAPI 在 `contracts/controller.openapi.json` 與 `contracts/host-controller.openapi.json`。

## 身份

外層 Linux 主線使用 loopback Web 與主機 session；主機首次設定不依賴 HA。家庭操作另外驗證 active HA owner。次要 HA App 模式經 Ingress 接收 active owner。先 `GET api/session` 取得短期 CSRF；mutation 使用相同 origin，帶 `X-Agentic-CSRF`。新任務、批准、控制提案、helper 測試及補充需求另帶 `Idempotency-Key`。Body 不接受 `approved_by`、`approved:true` 或任意 HA endpoint。

## Controller 路由

路徑相對於管理頁根目錄。Host mode 的管理 Web 固定在 loopback 8099；HA App 模式經 Ingress，不提供直接 LAN 管理 port。

| 方法／路徑 | 契約 |
|---|---|
| GET `api/session` | principal、CSRF、App version |
| GET `api/status` | HA／Runner／provider／onboarding／pause 狀態 |
| POST `api/pair` | host、固定 port 8443、fingerprint、一次性 code |
| GET `api/inventory` | 真實 states／registry 摘要與本地 policy |
| POST `api/policy` | `policy.schema.json`，重新驗證 owner 與 entities |
| POST `api/pause` | `{ "paused": true }` |
| POST `api/providers/{codex,claude,agy,grok}/auth` | method、secret；device-code 省略 secret |
| GET `api/auth/{id}` | pending／connected／expired／failed 及安全授權指示 |
| POST `api/providers/{provider}/logout` | 官方登出及清除本地 profile |
| POST `api/tasks` | message、provider、可選 model；202，沒有家庭副作用 |
| GET `api/tasks`／`api/tasks/{id}` | 任務列表／詳情、proposal、events、evidence |
| POST `api/tasks/{id}/reply` | awaiting_user 的補充 message，同任務重新分析 |
| POST `api/tasks/{id}/cancel` | 停止 Agent 或追蹤；副作用不確定時要求核對 |
| POST `api/proposals/{id}/approve` | `{ "digest": "完整 SHA256" }`，202 |
| POST `api/proposals/{id}/reconcile` | 只讀 exact ID／hash，恢復可證明的狀態；不重送 |
| POST `api/proposals/{id}/test` | owner 明確要求兩個專用 helpers 的狀態轉換 |
| GET `api/automations` | ownership ledger |
| POST `api/automations/{id}/propose` | action=enable／disable／rollback，產生待批准提案 |
| GET `api/services` | 三條服務設定路徑、checkpoint、觀測 |
| POST `api/services/{id}` | start／verify／skip／confirm_voice_test |
| GET／POST `api/retention` | audit_days 1–30，context_days 1–7 且不大於 audit_days |
| POST `api/backups/export` | passphrase 至少 16 字元，回傳加密 binary |
| GET `api/evidence` | 真實 runtime 任務／operation 摘要；可能含家庭名稱，分享前確認 |

## Runner 私有通道

TLS 8443，Controller 驗證 pin。`POST /pair` 首次接受未配對且未過期的高熵 code，綁定 controller UUID／principal／protocol。首次回應遺失時，在原 code 的原期限內，完全相同的 code、Controller、principal、protocol 可取回同一 bearer；不延長期限、不旋轉身份。其他重配要求拒絕。Host pairing 檔僅保留原 code hash 與 fingerprint 相符的資料到原期限。其餘 API 需私有 bearer，沒有 owner 批准端點。

| 方法／路徑 | 內容 |
|---|---|
| GET `/status` | protocol、identity、CLI probe／auth 狀態 |
| POST `/providers/{provider}/auth` | device code／官方 credential 導入 |
| GET `/auth/{id}` | 登入進度 |
| POST `/providers/{provider}/logout` | 登出 |
| POST `/jobs` | `runner-job.schema.json`，精確 job ID／prompt／schema／期限 |
| GET `/jobs/{id}` | queued／running／succeeded／failed／cancelled／stop_unconfirmed、normalized result、resource metrics |
| POST `/jobs/{id}/cancel` | 核對 owned process 身份後 TERM／KILL，確認整組無存活 writer 才回 cancelled；無法證明則持久化 stop_unconfirmed 並阻止後續 job／auth；已完成的 job 不改寫結果 |

Runner 沒有回呼家庭寫入的 API。模型的 `inspect_entities`、`automation`、`action` 欄位只是意圖；Controller 過濾／驗證後才繼續下一個 job 或產生提案。

## 狀態與錯誤

`queued → planning → awaiting_user / awaiting_approval → executing → verifying → succeeded`，另有 `failed`、`cancelled`、`needs_reconcile`。只讀答案可由 planning 完成；停用規則由執行後讀回完成。

403：身份／來源／CSRF；409：相同 key 不同內容或衝突；422：輸入／policy／adapter 拒絕；429：佇列滿。失去 HA owner 查核能力時拒絕 mutation。發生副作用後的連線不確定性不使用新 resource ID 重試。

## 0.2 外層 Linux

主線使用 `contracts/host-controller.openapi.json`。主機登入、HAOS lifecycle 與外層 Agent 狀態不依賴 HA 連線；家庭操作仍另外驗證 HA owner。部署、loopback、Unix socket、配對與 VM ownership 見 [LINUX_PRODUCT.md](LINUX_PRODUCT.md)。原 Ingress 契約保留為次要 HA App 模式。

## 0.3 統一入口（host mode）

`GET /api/gateway` 顯示通道狀態，不回傳秘密；`POST /api/gateway/preferences` 選擇 primary／runtime／provider；`POST /api/gateway/onboarding` 保存步驟 0–4。`POST /api/gateway/channels/{channel}` 設定 enabled、secret、chats、senders；秘密留空保留原值。

`POST /api/gateway/line/login` 開始本機掃碼工作；GET 同路徑查看進度。`GET /api/gateway/messages` 顯示最近 100 筆，POST 同路徑需要 Idempotency-Key 及 message／runtime／provider。已清理內容的去重紀錄回 410；重用 ID 修改內容回 409。

`GET /api/runtimes` 分別顯示 host／haos；`POST /api/runtimes/haos/pair` 只接受 fingerprint／code，固定 VM 位址。`POST /api/runtimes/{runtime}/providers/{provider}/auth` 與 `/logout` 分層登入；`GET /api/runtimes/{runtime}/auth/{id}` 查詢進度。

所有入口均使用外層主機 session；mutation 另需 CSRF 與同源檢查。家庭部署仍需 HA owner 驗證。完整 enum／模型見重新產生的 host-controller OpenAPI。

## 自動 onboarding 與原生 HA 授權（0.5.1）

| 方法／路徑 | 契約 |
|---|---|
| POST `api/host/login`／`api/host/desktop-login` | 主機私有 key／一次性桌面 ticket 換取 HttpOnly session；ticket 在 React 啟動前自 URL 清除 |
| POST `api/host/logout` | 撤銷 session 與相關授權 scope |
| GET `api/bootstrap` | `contract=1` 的持久九步快照；GET 不派工 |
| POST `api/bootstrap/start` | 選定 CLI 與通道，Idempotency-Key 綁定固定流程；立即回 202 與 checkpoint |
| POST `api/bootstrap/preferences`／`api/bootstrap/retry` | 更改通道／明確重試；未知副作用保留同一 handle，不能當作已失敗重做 |
| POST `api/host/authorize` | 主機 session＋CSRF 開始固定 localhost HA OAuth；600 秒、綁定 session／owner／epoch／origin |
| GET `api/host/authorization` | 安全的授權狀態，無 token／code |
| GET `api/host/authorized` | 一次性交換＋active owner 驗證；成功或拒絕均 303 回 `/#onboarding`、no-store/no-referrer；未登入不交換 code |

順序為 network → runner_pairing → cli_auth → cli_assist → hermes_install → hermes_auth → channel → ha_provision → ha_native。等待人類登入保留表單；Hermes 安裝後的 revision／runtime／inference readback 等待有 120 秒持久期限。逾時重試只核對原工作；不因重開頁面、服務重啟而再安裝或重新推論。Pause 阻止下一筆派工，不撤回已被 Host 接受的工作。

Runner 私有 `POST /bootstrap/jobs`（202）、GET／cancel 使用獨立的固定 setup schema；CLI 確實執行，但沒有 arbitrary shell／root 安裝權限。這不是 research/development worker API。

## Hermes 與 worker adapter

`GET api/hermes` 顯示獨立 runtime／auth／worker capabilities；`POST api/hermes/auth`、`auth/device`、GET `auth/device/{id}`、POST `auth/device/{id}/cancel` 與 `auth/logout` 使用獨立帳號資料。`GET api/hermes/cases` 和 `POST api/hermes/runs/{run_id}/cancel` 受原 case scope 限制；conversation 經既有 Gateway。官方 MCP client 連接 Controller 私有 `/mcp`，每次工具呼叫檢查短期 grant、scope、期限及撤權，不可代替 Owner 批准。

目前 `worker.submit` 只回持久化 blocked handle、`execution_started=false`；Host／Home coding 尚未啟用。取消 blocked handle 不等於證明 coding workspace 已可交接。Runner `stop_unconfirmed` 同樣不能靠服務重啟自行清除；可信的 reconciliation 尚未提供。

資料保留限制：Controller `context_days` 的設定目前尚未同步成 Runner 清理契約；Runner 的 7 天清理只在新工作時觸發，idle GET 可能返回過期結果。這是已重現的未修缺陷，後續需內容清除、期限同步及不重執行的去重 tombstone，見 [Runner 審查](reviews/2026-10-12-runtime.md)。
