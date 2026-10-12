# Plan v3 進度與驗收帳本（團隊維護）

更新：2026-10-11。Owner 原稿基準為 0.3.1／`3ad476a712009469521c3f9838a44ef35ba29f76`。本檔獨立記錄實作狀態，不修改六份原稿；目前交付 **0.4.0 v3 技術預覽程式，完整自動回歸 141 passed／2 skipped；尚未宣稱 E0、E1 或 E2 驗收完成**。

## 記錄規則

開發狀態 `not_run / in_progress / implemented` 只描述程式交付；案例結果使用 `not_run / blocked / passed / failed`。新增案例預設 `not_run`，不得因舊測試或相關 unit test 通過而批次改為 passed。具體功能因隔離不足 disabled，可在開發備註記錄原因；未實際執行整個案例仍保留 not_run。

每次驗收另附 test/case/job IDs、時間、執行者、source commit、candidate digest、安裝版本、runtime_location、model_backend、ha_backend、device_kind、CLI/Hermes/Core/Supervisor/OS 版本、觀測位置／方法、evidence refs、限制與批准的 Live 副作用。不得用單一 real=true 取代多維環境。

## 16 項開發工作

本輪分 A Hermes/Controller、B worker sandbox、C docs/support/交付，整合者管理共用契約。下表的 in_progress 並非可用能力或完成聲明；完整成果與相依仍見 [Backlog](BACKLOG_V3.md)。

| ID | 優先／階段／工作線 | 開發狀態 | 目前證據／缺口 |
|---|---|---|---|
| V3-01 | P0 / E0 / 整合 | implemented | 六原稿 hash 核對、history 與根文件同步；見 IMPORT.json |
| V3-02 | P0 / E0 / A | in_progress | 真 pinned AIAgent／官方 MCP 及獨立 bridge 已實作、真容器 needs_auth 啟動已測；模型端點 fixture，Owner 真帳號未驗 |
| V3-03 | P0 / E0→E1 / A | in_progress | Hermes Gateway→case/session/run、scope續接、撤權及UI已實作／測試；真 bot／現場身份重送未驗 |
| V3-04 | P0 / E0 / B | in_progress | 獨立 console／去密 support 已實作，8個測試＋development-host唯讀smoke；候選安裝機故障救援未驗 |
| V3-05 | P0 / E0 / C | in_progress | 四images與installer/build已納入Hermes；firstboot不自動建HAOS；0.4.0精確媒體／一次性VM安裝待此程式提交後建置驗證 |
| V3-06 | P0 / E1 / A | in_progress | 固定MCP七tools、case ACL、短期能力、blocked job handle／去重已實作；沒有接上可執行cloud worker |
| V3-07 | P0 / E1 / B | in_progress | 離線sandbox primitive＋真Docker network/process/memory/PID negatives通過；完整ext4 lifecycle／provider egress／真CLI仍待驗，能力blocked |
| V3-08 | P0 / E1 / A+B | in_progress | bridge真程序停止、stop_unconfirmed、重啟／dispatch撤權、sandbox writer fencing及去重有測試；Owner/BAT真接手未做 |
| V3-09 | P0 / E1 / C | not_run | Hermes設定／對話UI已測，但問題回報／隔離candidate preview／Host Worker真bug修正尚未交付 |
| V3-10 | P1 / E2 / B | not_run | Home coding能力blocked；尚無真HAOS App隔離、認證、研究或程式執行證據 |
| V3-11 | P1 / E2 / A+B | in_progress | host.status已產出有位置／時間／版本的case evidence並驗ACL；Home callbacks／跨層事件與互補研究未做 |
| V3-12 | P1 / E2 / A+C | not_run | 保留既有家庭Engine回歸；未完成Hermes research→精確提案修訂／真模型helpers流程 |
| V3-13 | P1 / E2 / C | in_progress | 已明確schema2向前遷移與私有一致快照，2項migration tests；固定可信updater／相容候選rollback仍未實作 |
| V3-14 | P1 / E2 / 全組 | not_run | 尚無Owner laptop研究→修正→preview→批准更新→現場驗證→退版或雙worker真執行 |
| V3-15 | P2 / E3 / B+C | not_run | 未接入新的真實設備；LAN／USB及integration依現場授權逐項驗證 |
| V3-16 | P2 / E3 / 全組 | not_run | 72h、相鄰Core、離機恢復與三個單／雙worker對照均未執行 |

## 32 個產品驗收

以下每項仍需要 [Pilot Acceptance](PILOT_ACCEPTANCE.md) 的完整通過條件和對應環境證據。這次的固定診斷單元測試不是 laptop 安裝、Controller 故障實機救援或真實 Hermes 的驗收。

| ID | 案例 | 階段 | 結果 | 驗收證據 |
|---|---|---|---|---|
| T3-01 | 一次性 VM 安裝待交付媒體 | E0 交付前 | not_run | — |
| T3-02 | Owner laptop 真安裝與只選外層 | E0 現場 | not_run | — |
| T3-03 | Hermes 真實登入並回答一個工具查詢 | E1 | not_run | — |
| T3-04 | 保持 HAOS 未啟動，問主機狀態 | E1 | not_run | — |
| T3-05 | 關閉或故障 Controller 的測試實例 | E0 救援，E2 擴充 | not_run | — |
| T3-06 | 同一 Web／bot 的身份與訊息重送 | E1 | not_run | — |
| T3-07 | Hermes 派 Host Worker 研究 | E1 | not_run | — |
| T3-08 | Host Worker 修實際 UI 問題 | E1 | not_run | — |
| T3-09 | Host／Hermes 工具嘗試越權 | 各開發能力啟用前 | not_run | — |
| T3-10 | Preview 導航／連線與 cookie 測試 | E1 preview | not_run | — |
| T3-11 | 同工作重複提交與回應遺失 | E1 | not_run | — |
| T3-12 | Owner／BAT 接手工作區 | E1 | not_run | — |
| T3-13 | Home Worker 在 HAOS App 真正研究與 coding | E2 核心必要 | not_run | — |
| T3-14 | 不同元件視角的對照 | E2 | not_run | — |
| T3-15 | Home Worker secret／網路／管理負向測試 | E2 coding 啟用前 | not_run | — |
| T3-16 | Worker 要求超範圍診斷／過期能力 | E2 | not_run | — |
| T3-17 | 同 case 互補研究 | E2 | not_run | — |
| T3-18 | 獨立 verifier 測候選 | E2 | not_run | — |
| T3-19 | 日誌／文件含指令式惡意內容 | E1/E2 | not_run | — |
| T3-20 | 內層事件重送、斷線、Controller 重啟 | E2 | not_run | — |
| T3-21 | 真模型產生 helpers 提案再批准 | E2 家庭寫入 | not_run | — |
| T3-22 | 追問修改同一提案 | E2 | not_run | — |
| T3-23 | 停掉 Hermes 和兩個 workers | E2 | not_run | — |
| T3-24 | 套用同 schema 受測候選版本 | E2 迭代 | not_run | — |
| T3-25 | 候選 Web／Controller 無法啟動 | E2 迭代 | not_run | — |
| T3-26 | 加入一個低風險實體設備 | E3 逐項 | not_run | — |
| T3-27 | 整台 Linux 重啟與內層 re-pair | E2 | not_run | — |
| T3-28 | 資源耗盡、超時與取消 | 各 coding mode 啟用前 | not_run | — |
| T3-29 | provider 失效、切換與資料 scope 收回 | E1/E2 | not_run | — |
| T3-30 | 相邻 Core 版本／App 更新與離機恢復 | E3 | not_run | — |
| T3-31 | 在使用中觀察 72h | E3 | not_run | — |
| T3-32 | 三個案例對照單協調者與雙 workers | E3，非早期阻擋 | not_run | — |

## 本輪工程證據

**完整凍結程式回歸：141 passed、2 skipped，107.87 秒。** 命令：`pytest-disktmp uv run pytest -q --real-ha --containers --junitxml=evidence/pytest-0.4.0.xml`；[結果](../../evidence/pytest-0.4.0.xml)與[詳細環境／限制](../VERIFICATION.md)。兩個 skip 是大型 HAOS KVM 與 Linux ISO 安裝，尚未對本候選執行，不能以 0.2.x 歷史結果補上。

| 工程檢查 | 實際執行與證據範圍 |
|---|---|
| 文件匯入 | IMPORT.json的6份SHA-256與上傳／private parent比對；3份pre-v3根文件原封封存；不是功能驗收 |
| Hermes upstream／MCP | 真pinned AIAgent與官方HTTP MCP client；兩輪模型protocol fixture＋固定工具／provider401；另接實際Controller MCP handler，host觀測仍fixture |
| 私有 bridge | 真container UID10003無帳號啟動；service scope、去重、認證／重啟／retention、真subprocess與descendant停止／stop_unconfirmed fencing |
| Controller權威 | 固定工具allowlist，拒Live／偽造權限；case evidence ACL、scope/audience撤回、串流中重驗權、取消後拒晚到成功；worker始終blocked且execution_started=false |
| 資料格式 | 2個schema migration tests；Controller格式2，先私有一致snapshot再向前遷移，保留settings/audit/去重；舊binary拒讀2，無原地降版承諾 |
| Sandbox foundation | 真Docker network／management denial、nonroot、read-only root、CPU/PID/memory limits、timeout／程序樹取消；artifact／lease／去重fixtures。普通檔案mkfs已測，未在開發host掛載；完整ext4 quota生命週期／cloud egress／HAOS App未驗 |
| 固定support | 8個去密／限額／console tests；development-host唯讀collector smoke有11筆觀測（8collected、3not_available），model/HA/device均none，無Live副作用 |
| Browser | 真Chromium＋API fixtures，1440/768/390無水平overflow；Hermes五種状态、Enter表單、credential不入storage、scope續接／刷新／新case與direct Grok/HAOS切換；九張hermes截圖，桌機／手機已檢視 |
| 既有路徑 | 真Core原生helper／觸發／重啟；真受控CLI／LINE.js／Supervisor package，模型及部分平台端點仍fixture；不是Owner家庭或雲端帳號試跑 |

所有本輪測試位於磁碟basetemp／可清理容器；沒有在開發host執行產品installer、沒有真家庭副作用或公開家庭資料。完整候選媒體與VM測試將綁確切commit／digest另記，不預填尚未產生的結果。

下一個最短切片是將這個0.4.0變更集提交、建置對應bundle／laptop ISO，在一次性VM驗證外層先行與獨立救援，再讓Owner完成真帳號／實機設定。E1仍需完整隔離與真cloud coding CLI、diff/tests/preview；E2仍需真正HAOS App研究與受控家庭／更新／退版。尚未通過的coding模式保持blocked，不以基礎containment或已配對受控Runner代替。
