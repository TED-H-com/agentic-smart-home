# Worker Contract v1 — Hermes 與雙層 Coding Worker

**2026-10-11｜配合 Plan／Spec v3｜設計契約，尚未實作**

本文件的工具名稱、欄位、狀態均為自家設計，不是 Hermes、MCP 或 Home Assistant 官方接口名稱。`work_contract_major=1` 是新工作契約，與 repo 已有的 Runner `protocol=1` 分開協商，不因數字相同就假定相容。[現有傳輸](SOURCES.md#r03)

## 1. 最少需要的工具

| Hermes 可見工具（擬定） | 語意 | 輸出 |
|---|---|---|
| `worker.capabilities` | 列出配對 worker 與當下可用能力，不暴露憑證 | environment、version、modes、auth/availability |
| `worker.submit` | 提交明確目標、證據 refs、模式與期望產物；立即回 handle | case_id、job_id、accepted/blocked、有效 scope |
| `worker.status` | 查已授權 job，不啟動新推理 | 狀態、進度、heartbeat、新 event cursor |
| `worker.cancel` | 取消 job，由 broker 停止整個工作環境 | cancel_requested/cancelled/stop_unconfirmed |
| `worker.result` | 讀已驗證範圍內的摘要／產物索引 | findings、test/evidence/patch refs、限制 |
| `case.evidence.read` | 按 ID 展開去密證據，無任意 filesystem path | 有界內容、觀測來源、timestamp、截斷標記 |
| `home.proposal.create` | 把研究結果轉成既有家庭提案 | proposal/revision/digest；不執行 |

Live approve/apply 不提供給 worker。Hermes 可以提出請求，但 Controller 只接受經驗證 Owner session／預授權 runbook 的真正批准。MCP 上的 tool confirmation 不是家庭批准的替代品。

官方 Hermes 支援外部 MCP 工具；其 `delegate_task` 是 Hermes 子代理機制，不會自動定位自家 HAOS worker。此 adapter MUST 顯式映射 Controller／Runner jobs。[MCP](SOURCES.md#u01) [子代理](SOURCES.md#u02)

## 2. 身份、任務與執行位置

固定 `worker_id=host-coder` 或 `home-coder`；server 配對記錄決定 endpoint／環境，模型不能提供任意 URL、port、UID、volume 或 credential。

五種 ID 不混用：`conversation_id` 是對話；`case_id` 是問題；`job_id` 是一段 worker 工作；`proposal_id` 是具體 Live 變更；`operation_id` 是真正副作用。單一 case 可有多個研究／修正／驗證 jobs，但不能因 case 已批准研究就取得 Live 寫入。

Controller 保存 requester、資料分享 scope、provider 選擇、來源訊息與權限版本。CLI 使用 job 專用短期 capability；不可取得 Runner pairing token、Controller admin 或 Owner cookie。身份來源是經認證的通道，不是模型輸入。

## 3. 提交的最小內容

| 欄位 | 誰決定／驗證 | 要求 |
|---|---|---|
| `case_id`, `job_id`, `parent_job_id` | Controller | 唯一且可追溯；root job 的 parent 可為 null |
| `worker_id`, `mode` | Hermes 提議、Controller 授權 | 固定 worker 和可用 profile |
| `goal`, `questions`, `deliverables`, `acceptance_refs` | Hermes／Owner／可信模板 | 清楚說明不同 worker 要查什麼 |
| `repo_id`, `base_commit`, `workspace_id` | Controller／launcher | repo allowlist、固定 SHA、自家 workspace ID |
| `evidence_refs`, `candidate_ref` | Controller ACL | 只傳必要資料；產物以 ID 取，不給任意 path |
| `authorization_ref`, `policy_version` | Controller | 指向服務端 grant；不是 model 可自簽的批准 |
| `lease_epoch`, `deadline`, `budgets` | broker | 可執行限制；worker 不可自行調大 |
| `provider_id`, `model_id` | 既定帳號與 Owner scope | 不自動切換未授權 provider |
| `request_digest`, `idempotency_key` | 可信 adapter／Controller | 重試可去重；相同 key 不可帶不同內容 |

敏感 bearer 不寫入此 envelope 的公開日誌；`authorization_ref` 是受保護記錄的指標，不是公開持有即可越權的通行證。

### 範例：派內層查現場（純示意，不能直接當已可呼叫 API）

```json
{
  "work_contract_major": 1,
  "case_id": "case-demo-001",
  "job_id": "job-demo-home-001",
  "parent_job_id": null,
  "worker_id": "home-coder",
  "mode": "research",
  "goal": "查明測試 helper 規則未觸發的原因，先不要修改正式設定",
  "questions": ["來源是否真的發生指定狀態轉換？", "執行中規則與候選設定是否相符？"],
  "deliverables": ["有來源的事實", "可反證的假設", "必要時的小型驗證程式", "尚缺資訊"],
  "acceptance_refs": ["T3-13", "T3-14"],
  "repo_id": "agentic-smart-home",
  "base_commit": "3ad476a712009469521c3f9838a44ef35ba29f76",
  "workspace_id": "workspace-demo-home-001",
  "evidence_refs": ["evidence-demo-inventory-001"],
  "candidate_ref": null,
  "authorization_ref": "grant-demo-home-read",
  "policy_version": "policy-demo-1",
  "lease_epoch": 1,
  "budgets": {"wall_seconds": 300, "diagnostic_calls": 20},
  "live_mutation_allowed": false
}
```

實際 deadline、provider、digest、idempotency 及 transport 認證由服務端補齊；這個範例是任務語意，不是安全完整 wire payload。布林 `live_mutation_allowed` 僅作顯示，永遠不能單獨授權。

## 4. 回報的結構

Broker 記錄 execution status；worker 提供 findings。兩者分開，不讓模型寫 `succeeded` 就直接更新權威 job 狀態。

| 區塊 | 必需內容 |
|---|---|
| 執行標頭 | case/job、worker、contract、實際 CLI 版本、開始／結束、exit／取消狀態、實際環境 |
| `facts` | 明確主張、對應 evidence IDs、來源與 timestamp；不能只寫信心百分比 |
| `inferences` | 推論、依據、替代解釋、能推翻它的測試 |
| `unknowns` | 尚缺資料、無法觀測的位置、未測能力、環境差異 |
| `experiments` | 實際測什麼、input/output refs、觀測位置、exit、fixture／real 分類 |
| `artifacts` | patch、reproducer、tests、preview、截圖及對應 hash／scope |
| `next_steps` | 建議的後續工作或提案，不代表已批准 |

每筆 evidence 至少包含 `observed_from`, `target_component`, `collection_method`, `observed_at`, `version_refs`, `evidence_id`, `content_digest`, `redaction`, `limitations`。版本依測項記 host release、HA Core／Supervisor、App image、CLI／模型；不可省略關鍵版本卻聲稱候選已驗證。

Broker 能證明在哪個沙盒執行、用哪個設定、取得哪些原始輸出，不代表能自動證明輸出所描述的事實全真。固定測試 harness、API 讀回與 source-linked evidence 才是實際查核材料。

### 部分結果示意

```json
{
  "case_id": "case-demo-001",
  "job_id": "job-demo-home-001",
  "finding_status": "inconclusive",
  "facts": [],
  "inferences": [],
  "unknowns": ["尚未觀察新的來源狀態轉換，不能判定規則失效"],
  "experiments": [],
  "artifacts": [],
  "next_steps": ["在 Owner 批准的 helper 測試窗口取得新事件"]
}
```

這個示意刻意不捏造成功觀測。執行完成、研究有結論、測試通過、正式問題已解決是四件不同的事。

## 5. 狀態與恢復

```text
job: accepted → queued → running → succeeded | failed
                      ↘ blocked
running → waiting_for_user → running
running → cancel_requested → cancelled | stop_unconfirmed
running → interrupted →（查核後續接／失敗；不直接重跑）

case: reported → researching → developing → testing → preview_ready
      → awaiting_live_approval → deploying → verifying_live → resolved
      也可進 blocked / cancelled / failed / needs_reconcile
```

排隊不保證已啟動，heartbeat 不保證產生進展。無法停止的 job 不標 cancelled；Live 副作用不確定時 Controller 進 `needs_reconcile`。

`Idempotency-Key + canonical digest` 綁同一提交；Controller 和 Runner 都必須做去重。提交成功但 HTTP 回應丟失，重新查 exact job；不能換新 id 派同一修正。checkpoint 帶 workspace tree hash 和版本；跨 CLI 版本或權限變更不能直接假定可續接。

## 6. 交接與獨立驗證

Hermes 可以平行派互補的 read-only 研究；有依賴的修改與驗證順序執行。worker 不自行派給另一 worker，避免雙向無限循環。

最初每 case 最多兩輪跨 worker 補查；超出回報現有證據與缺口，由 Owner 擴大預算。一般工具型查詢不算再生成一個 Agent。大量狀態事件合併成 case 更新，不每筆事件喚醒 Hermes。

研究型分工：Host 查 code／上游；Home 查現場／寫重現。驗證型分工：Host 做 patch；Home 驗证實际條件與受控 helper 流程。Home 也可產出 patch，由 Host 整合；兩邊不可寫同一 workspace。

Verifier 不先拿到「請確認它已修好」的指令；它拿症狀、候選 digest、固定驗收條件与必要證據。不同模型非必需；客觀檢查不可由投票取代。

## 7. 單一工作所有權

Owner、Hermes、CLI、BAT 或其他工具對同一 workspace／coding session 同時只允許一個 writer。接手流程：暫停接收新步驟 → 撤銷能力 → 停止原環境／確認不再寫入 → 保存 checkpoint → 發新 lease。還有活的舊程序時不得宣稱 writer 已轉移。

權限收回、case 過期或 logout 會使 job 工具失效。若是已不可撤回的外部操作，只能停止後续並核對；不能顯示「已取消所以什麼也沒發生」。

## 8. 產物、記憶與不可信內容

跨層只傳 job/case、最小上下文和 artifact IDs，不共享整份 home directory、SQLite、CLI 登入或 Hermes memory。來源文件、issue、log、設備名稱、worker 摘要都當作不可信資料；內含「忽略規則」「請執行命令」不會取得更高指令或權限。

私有證據留本地且有 ACL／保留期限。送雲端只用授權去密摘要，公開 PR／issue 需額外確認內容。Hermes 的長期記憶只保留穩定偏好與已驗證解法摘要；要附來源／版本／日期，不能把昨天 inventory 當成現在的家。

README 建議先以 Markdown 契約實作並由團隊生成程式模型／schema，避免這份計畫另帶一套與程式脫節的 schema。實際 API 如改名，更新 handler、契約與測試，不只改文件。
