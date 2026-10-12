# Plan v3 進度與驗收帳本（團隊維護）

更新：2026-10-12。Owner 原稿基準為 0.3.1／`3ad476a712009469521c3f9838a44ef35ba29f76`。本檔獨立記錄實作狀態，不修改六份原稿；目前為 **0.4.0 v3 技術預覽，前輪 CI 141 passed／2 skipped；ISO 驗證發現產品缺陷，修正後須重建媒體及重驗，尚未宣稱 E0、E1 或 E2 整階段完成**。

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
| V3-04 | P0 / E0 / B | in_progress | 前輪ISO已實測Controller停止時diagnostics及support格式；manifest為root0600導致版本null，installer改root:1000/0640，須非sudo重驗 |
| V3-05 | P0 / E0 / C | in_progress | 前輪ISO已測外層firstboot不建HAOS、Owner HTTP與四映像ID；sandbox/installer修正後須重建bundle/ISO，新digest待產生，四images不變 |
| V3-06 | P0 / E1 / A | in_progress | 固定MCP七tools、case ACL、短期能力、blocked job handle／去重已實作；沒有接上可執行cloud worker |
| V3-07 | P0 / E1 / B | in_progress | 真Docker containment negatives已測；VM發現preflight誤讀CpuCfsQuota欄位，執行前拒絕，正修產品；完整ext4 lifecycle／provider egress／真CLI仍待驗，能力blocked |
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

以下每項仍需要 [Pilot Acceptance](PILOT_ACCEPTANCE.md) 的完整通過條件和對應環境證據。一次性 VM 與 Owner laptop、離線 sandbox 與真 cloud coding 分開記錄；相關工程測試不能代替尚未執行的整案。

| ID | 案例 | 階段 | 結果 | 驗收證據 |
|---|---|---|---|---|
| T3-01 | 一次性 VM 安裝待交付媒體 | E0 交付前 | failed | 第一輪support harness錯字；第二輪sandbox產品欄位錯誤，manifest版本權限另需修正。見[第二輪失敗](../../evidence/linux-install-0.4.0-sandbox-preflight-failure.json)；待新媒體重驗 |
| T3-02 | Owner laptop 真安裝與只選外層 | E0 現場 | not_run | — |
| T3-03 | Hermes 真實登入並回答一個工具查詢 | E1 | not_run | — |
| T3-04 | 保持 HAOS 未啟動，問主機狀態 | E1 | not_run | — |
| T3-05 | 關閉或故障 Controller 的測試實例 | E0 救援，E2 擴充 | not_run | 前輪ISO已取得診斷／support格式證據，但版本metadata不可讀；待修正後完整重驗 |
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

**最近完成的 GitHub CI：141 passed、2 skipped，152.74 秒**，受測 commit `02e7e00cffe9309657ac723bf28a0f435cdb3a3b`；[CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38200135512)、[JUnit](../../evidence/pytest-0.4.0-github.xml)及[詳細環境／限制](../VERIFICATION.md)。這早於本輪sandbox／installer產品修正。包含 `--real-ha --containers`；兩個 skip 是獨立大型 HAOS KVM 與 Linux ISO 安裝。另行ISO測試目前失敗，不能將CI或0.2.x歷史結果寫成新版安裝通過。

前輪媒體 source commit 為 `69a1a2470988a47f3520cf7081a3435b3e19fdae`，以 `02e7e00` 的測試harness驗證。ISO SHA-256：`21b4d9a0b9caaeb9bf8840602f2858a1843ad347f3b0e13c5dfdddda035a24c3`；Linux TAR SHA-256：`17d5933fdba0499ac46d19168d1cdfb3c5eb0b315230d0689c87ff2e732a1b48`。見[封存release](../../evidence/release-0.4.0-before-sandbox-fix.json)。這些失敗媒體已移至本機 `build/before-sandbox-fix-*`；修正後新bundle／ISO的digest尚未產生，四服務images不變。

同ISO第二輪 **1 failed／1764.31秒**：安裝、外層firstboot、Owner HTTP與Controller停止時support格式均已觀測，但Docker preflight誤用`CPUCfsQuota`，正確JSON欄位為`CpuCfsQuota`，因此在任何sandbox工作前拒絕；[失敗摘要](../../evidence/linux-install-0.4.0-sandbox-preflight-failure.json)與[JUnit](../../evidence/linux-install-0.4.0-sandbox-preflight-failure.xml)已封存。另修非秘密installed.json讀取權限，新增非sudo診斷版本斷言；不放寬sandbox限制或秘密檔案權限。

| 工程檢查 | 實際執行與證據範圍 |
|---|---|
| 文件匯入 | IMPORT.json的6份SHA-256與上傳／private parent比對；3份pre-v3根文件原封封存；不是功能驗收 |
| Hermes upstream／MCP | 真pinned AIAgent與官方HTTP MCP client；兩輪模型protocol fixture＋固定工具／provider401；另接實際Controller MCP handler，host觀測仍fixture |
| 私有 bridge | 真container UID10003無帳號啟動；service scope、去重、認證／重啟／retention、真subprocess與descendant停止／stop_unconfirmed fencing |
| 相依安全更新 | PyJWT2.15.0／httpx2與httpcore2 2.12.0；真wheel pip check與JWT負向回歸、16項runtime/container通過；公開後重查open Dependabot alerts=0，見[記錄](../../evidence/hermes-security-update.json) |
| Controller權威 | 固定工具allowlist，拒Live／偽造權限；case evidence ACL、scope/audience撤回、串流中重驗權、取消後拒晚到成功；worker始終blocked且execution_started=false |
| 資料格式 | 2個schema migration tests；Controller格式2，先私有一致snapshot再向前遷移，保留settings/audit/去重；舊binary拒讀2，無原地降版承諾 |
| Sandbox foundation | 真Docker containment／timeout／取消與artifact／lease／去重fixtures已測；前輪VM因Docker info欄位錯誤在preflight拒絕，完整ext4 quota生命週期仍未跑，cloud egress／HAOS App未驗 |
| 固定support | 8個去密／限額／console tests與唯讀smoke；前輪ISO實際停止Controller後diagnostics與support格式通過，但manifest权限導致版本null；新媒體需重驗 |
| Browser | 真Chromium＋API fixtures，1440/768/390無水平overflow；Hermes五種状态、Enter表單、credential不入storage、scope續接／刷新／新case與direct Grok/HAOS切換；九張hermes截圖，桌機／手機已檢視 |
| 既有路徑 | 真Core原生helper／觸發／重啟；真受控CLI／LINE.js／Supervisor package，模型及部分平台端點仍fixture；不是Owner家庭或雲端帳號試跑 |

所有本輪測試位於磁碟basetemp／可清理容器；產品安裝僅發生在一次性 VM，沒有在開發host執行installer、沒有真家庭副作用或公開家庭資料。失敗媒體與證據綁確切commit／digest；新媒體尚待建置與驗證，不預填通過。

目前先修正sandbox前置檢查與安裝metadata權限，重建bundle／ISO後再於一次性VM驗證外層先行、獨立救援、離線sandbox與HAOS lifecycle，再讓Owner完成真帳號／實機設定。E1仍需完整隔離與真cloud coding CLI、diff/tests/preview；E2仍需真正HAOS App研究與受控家庭／更新／退版。尚未通過的coding模式保持blocked，不以基礎containment或已配對受控Runner代替。
