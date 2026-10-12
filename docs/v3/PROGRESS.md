# Plan v3 進度與驗收帳本（團隊維護）

更新：2026-10-12。Owner 原稿基準為 0.3.1／`3ad476a712009469521c3f9838a44ef35ba29f76`。本檔獨立記錄實作狀態，不修改六份原稿；前版為 **0.4.0 v3 技術預覽，CI 145 passed／2 skipped，精確 ISO 完整 VM 測試 1 passed；完成 E0 工程媒體與獨立救援範圍，Owner laptop／真帳號及 E1、E2 尚未完成**。


0.5.0 開發新增 CLI 先行的自動 onboarding：持久九步流程、桌面單次登入、官方最新 Hermes 相容性檢查、Hermes 原生 Codex OAuth、LINE／Telegram 配對碼、自動 HAOS 啟動及 HA 官方瀏覽器授權。這是 Owner 的新流程，不改六份原稿。當版CI為210 passed／3 skipped；精確0.5.0 ISO在一次性VM為1 passed／1310.79秒。T3-01與T3-05的E0工程範圍已重驗，其他30項不批次升級，詳見 [版本驗證](../VERIFICATION.md)。通用 Host／Home coding、真雲端家庭與固定產品更新／退版仍未完成。

0.5.1 本輪補上跨服務 E2E 與恢復修正，本機完整回歸 **271 passed／2 skipped**；真 HAOS 另為 **1 passed**。測試含實際服務／CLI／MCP／Core 瀏覽器授權，但模型及 Host 副作用為 fixture，見 [審查](../reviews/2026-10-12-E2E-ARCHITECTURE.md)。最終 CI **271 passed／3 skipped**；修正版精確 ISO **1 passed／1493.94 秒**，完成最新版 Hermes、HAOS 冷還原與重啟。下列兩案僅更新 E0 工程證據；其餘 30 案不批次升級。該版缺口由下列 0.6.0 工程交付接續。

0.6.0 本輪新增真正 tools-enabled Codex Host coding、獨立角色登入／受控出站、可信停止恢復、全鏈保存期限、成果與隔離截圖預覽、同 case 工作追蹤、家庭精確提案橋接、簽名候選 Controller 更新／退版，以及 Tailscale 選項。UI 已按 1440／768／390 px 截圖調整。這是程式交付；真帳號、Owner laptop、Home App coding 與完整 E1／E2 仍需要各自驗收。技術邊界見 [Workers](../WORKERS.md) 與 [Updates](../UPDATES.md)，本輪結果見 [驗證](../VERIFICATION.md)。

0.6.0 最終 CI **469 passed／4 skipped**；精確 ISO／harness `6847a03` 完整安裝 **1 passed／1789.412 秒**，含真 Codex 工具／固定模型 transport、最新版 Hermes、官方 HAOS、同 schema Controller 更新／退版、快照恢復與外層重啟。只更新 T3-01／T3-05 的 E0 工程證據，其餘 30 案仍為 `not_run`；真家庭與雲端整案不得以 fixture 代替。後續原生首頁與上游 adapter 整合見 [入口流程](../ONBOARDING.md#原生家庭首頁與智能管家)及 [相容邊界](../ADAPTERS.md)；HA 側欄外層 bridge 仍待整合。

Home 隔離基礎已有 [真 HAOS 18.3 App 證據](../../evidence/haos-app-boundary.json)：預設設定無法建立 mount namespace；增加 `SYS_ADMIN` 與限定 cgroup2 掛載位置的 AppArmor 診斷設定後，可在獨立 App cgroup namespace 委派資源限制並實際觸發記憶體 OOM kill。這修正了「平台無可行隔離路徑」的假設，尚未交付 Home coding launcher、模型憑證／出站／工作區完整隔離或真雲端執行。32 個案例的狀態不變。

0.6.1 接續原生 HA 日常入口、返回救援／管家、Hermes 私有接點 facade、候選 contract gate 與 Linux 固定動作能力檢查。真 Core 導向與對話接續已在隔離 E2E 通過，最終 CI **598 passed／5 skipped**，同來源 `083b80e` 精確 ISO 完整安裝 **1 passed／1568.186 秒**，包含最新 Hermes、HAOS、Controller 更新／退版及重啟，見 [最新紀錄](../VERIFICATION.md)；這些工程能力不新增真家庭驗收 passed 案例。

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
| V3-04 | P0 / E0 / B | implemented | E0 獨立診斷／support；0.6.0 增加 root console publisher 登錄及固定 updater。新版精確 ISO 的 Controller 停止救援及固定 updater 已實際通過；真家庭試跑另驗 |
| V3-05 | P0 / E0 / C | implemented | 0.6.1 媒體包含五個映像、固定 Tailscale 套件、只含 public repo HEAD 的來源 bundle；精確新 ISO 完整安裝 1 passed／1568.186s；實體 laptop 尚未執行 |
| V3-06 | P0 / E1 / A | implemented | MCP → Controller → TLS Runner → broker 持久派工、同 case handles／ACL／去重；Codex tools 真程序＋模型 transport fixture，真模型帳號未驗 |
| V3-07 | P0 / E1 / B | implemented | 真 Codex 工具執行、私有 clone、patch、獨立 verifier；bearer 留 relay、封鎖管理／私網出站、ext4／cgroup／watchdog。僅 Host Codex coding enabled |
| V3-08 | P0 / E1 / A+B | in_progress | lease／實際 writer 停止與重啟 reconcile 已交付；可匯出停止後 checkpoint。live_session:false，尚非直接加入同一 BAT／CLI session |
| V3-09 | P0 / E1 / C | implemented | Web 問題派工、差異／SHA-256 成果、network-none renderer 三尺寸 PNG、獨立短效 origin／撤銷；真 Owner UI 問題與雲端整案未驗 |
| V3-10 | P1 / E2 / B | in_progress | 真 HAOS 18.3 App 已證明 SYS_ADMIN＋AppArmor 診斷設定可委派 App 內 cgroup 限制及 memory OOM kill；[證據](../../evidence/haos-app-boundary.json)。Home launcher／雲端 coding／完整 secret、network、quota、fencing 尚未交付，保持未啟用 |
| V3-11 | P1 / E2 / A+B | in_progress | 同 case 工作者狀態、下一輪 Hermes 證據 handle、觀測位置與 ACL 已接通；雙層互補研究與 bot 主動完成通知未驗／未交付 |
| V3-12 | P1 / E2 / A+C | implemented | home.observe／propose／proposal_status 接回既有 Engine，精確 Owner 批准、撤權／暫停競態保護；真模型 helpers 整案待驗 |
| V3-13 | P1 / E2 / C | implemented | schema 3 私有遷移 snapshot；Ed25519 publisher／digest、獨立 probe、精確批准、quiesce／backup／health／保留新 DB 退版。僅同 schema 外層 Controller，不是全產品／HAOS 更新器 |
| V3-14 | P1 / E2 / 全組 | not_run | 尚無Owner laptop研究→修正→preview→批准更新→現場驗證→退版或雙worker真執行 |
| V3-15 | P2 / E3 / B+C | not_run | 未接入新的真實設備；LAN／USB及integration依現場授權逐項驗證 |
| V3-16 | P2 / E3 / 全組 | not_run | 72h、相鄰Core、離機恢復與三個單／雙worker對照均未執行 |

## 32 個產品驗收

以下每項仍需要 [Pilot Acceptance](PILOT_ACCEPTANCE.md) 的完整通過條件和對應環境證據。一次性 VM 與 Owner laptop、離線 sandbox 與真 cloud coding 分開記錄；相關工程測試不能代替尚未執行的整案。

| ID | 案例 | 階段 | 結果 | 驗收證據 |
|---|---|---|---|---|
| T3-01 | 一次性 VM 安裝待交付媒體 | E0 交付前 | passed | 0.6.1 source/harness 083b80e／ISO ac434f55…完整 VM 1 passed／1568.186s，版本／五 images／outer-only firstboot／單次登入／Web／診斷核對；一次性磁碟與 key，已清理。見 [結果](../../evidence/linux-install-0.6.1.json)及 [JUnit](../../evidence/linux-install-0.6.1.xml)，真帳號與 laptop 未驗 |
| T3-02 | Owner laptop 真安裝與只選外層 | E0 現場 | not_run | — |
| T3-03 | Hermes 真實登入並回答一個工具查詢 | E1 | not_run | — |
| T3-04 | 保持 HAOS 未啟動，問主機狀態 | E1 | not_run | — |
| T3-05 | 關閉或故障 Controller 的測試實例 | E0 救援，E2 擴充 | passed | 0.6.1 E0 獨立救援：Controller 停止時非 sudo diagnostics／support 可用，見 [rescue](../../evidence/installed-rescue-laptop-amd64-0.6.1.json)。另完成合成候選的安裝後 Controller 更新／保留新資料退版；不代表 E2 真家庭整案 |
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

## 0.4.0 工程證據（歷史）

**GitHub CI：145 passed、2 skipped，152.424 秒**，受測與媒體 source commit 均為 `cb11ffd8c0d3eb48a9854fd51add030a1df936e9`；[CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38202961564)、[JUnit](../../evidence/pytest-0.4.0-github.xml)及[詳細環境／限制](../VERIFICATION.md)。包含 `--real-ha --containers`；CI跳過獨立大型HAOS KVM與Linux ISO安裝，後者已另行通過 **1 passed／1566.81秒**，见[ISO JUnit](../../evidence/linux-install-0.4.0.xml)。CI與安裝證據分開記錄。

ISO SHA-256：`44d163dba7ff629cc287213cbbb9a4836381d24e45531533efa0051ca9625868`（5387124736 bytes）；Linux TAR SHA-256：`12d4426978674706810dcc4be73de40ea26542a15f869e032b864d3879f04229`（1307658240 bytes）。媒體 source為 `cb11ffd`，见 [release](../../evidence/release-0.4.0-final.json)；精確ISO完整VM通過、四個安裝後image IDs完全符合。TAR沒有另做既有主機就地安裝驗收。前輪 `69a1a24`／ISO `21b4d9a0…` 失敗媒體及[封存release](../../evidence/release-0.4.0-before-sandbox-fix.json)仍保留。

最終VM為Ubuntu24.04.5、`runtime_location=disposable_outer_linux_vm`、`model_backend=none`、`ha_backend=nested_official_haos`、`device_kind=none`。外層firstboot時HAOS未建立，真[Owner HTTP與Runner配對](../../evidence/installed-auth-laptop-amd64-0.4.0.json)成功；四CLI均needs_reauth、Hermes needs_auth，未登入雲端。Controller停止仍有版本0.4.0的獨立救援。可選HAOS首次啟動、冷快照restore、外層reboot／autostart後均讀到真Core onboarding JSON list，見[安裝結果](../../evidence/linux-install-0.4.0-final.json)。這不是Home App coding或T3-27完整re-pair／helpers驗收。

舊ISO第二輪曾為 **1 failed／1764.31秒**：Docker preflight誤用`CPUCfsQuota`，正確JSON欄位為`CpuCfsQuota`，因此在任何sandbox工作前拒絕；[失敗摘要](../../evidence/linux-install-0.4.0-sandbox-preflight-failure.json)與[JUnit](../../evidence/linux-install-0.4.0-sandbox-preflight-failure.xml)已封存。另修非秘密installed.json讀取權限，新增非sudo診斷版本斷言；新媒體已驗證兩項修正，未放寬sandbox限制或秘密檔案權限。

| 工程檢查 | 實際執行與證據範圍 |
|---|---|
| 文件匯入 | IMPORT.json的6份SHA-256與上傳／private parent比對；3份pre-v3根文件原封封存；不是功能驗收 |
| Hermes upstream／MCP | 真pinned AIAgent與官方HTTP MCP client；兩輪模型protocol fixture＋固定工具／provider401；另接實際Controller MCP handler，host觀測仍fixture |
| 私有 bridge | 真container UID10003無帳號啟動；service scope、去重、認證／重啟／retention、真subprocess與descendant停止／stop_unconfirmed fencing |
| 相依安全更新 | PyJWT2.15.0／httpx2與httpcore2 2.12.0；真wheel pip check與JWT負向回歸、16項runtime/container通過；公開後重查open Dependabot alerts=0，見[記錄](../../evidence/hermes-security-update.json) |
| Controller權威 | 固定工具allowlist，拒Live／偽造權限；case evidence ACL、scope/audience撤回、串流中重驗權、取消後拒晚到成功；worker始終blocked且execution_started=false |
| 資料格式 | 2個schema migration tests；Controller格式2，先私有一致snapshot再向前遷移，保留settings/audit/去重；舊binary拒讀2，無原地降版承諾 |
| Sandbox foundation | 19項回歸＋[安裝後離線probe](../../evidence/installed-sandbox-laptop-amd64-0.4.0.json)通過；獨立clone產出patch／成功測試、原Git不變；ext4容量62869504 bytes，總寫入60252160 bytes得到ENOSPC；取消／程序樹停止／fencing／清理通過，probe共2.1s。沒有cloud CLI或HAOS App |
| 固定support | Host／support20項回歸＋精確ISO[Controller停止救援](../../evidence/installed-rescue-laptop-amd64-0.4.0.json)通過；非sudo diagnostic及support bundle可用、每筆installed_application為0.4.0 |
| Browser | 真Chromium＋API fixtures，1440/768/390無水平overflow；Hermes五種状态、Enter表單、credential不入storage、scope續接／刷新／新case與direct Grok/HAOS切換；九張hermes截圖，桌機／手機已檢視 |
| 既有路徑 | 真Core原生helper／觸發／重啟；真受控CLI／LINE.js／Supervisor package，模型及部分平台端點仍fixture；不是Owner家庭或雲端帳號試跑 |

所有本輪測試位於磁碟basetemp／可清理容器；產品安裝僅發生在一次性VM，結束後VM／容器／磁碟暫存均已清除。沒有在開發host執行installer、沒有真家庭副作用或公開家庭資料。失敗與成功證據皆綁確切commit／digest；32案中2案通過上述範圍，其餘30案仍為not_run。

下一個產品驗收切片是在 Owner laptop 用真帳號，透過智能管家完成一件 Host 研究／修正／獨立測試／預覽的真實問題；相關受控出站、帳號隔離與 PNG 預覽已交付工程實作。接著交付真正 HAOS App coding launcher、自主研究與家庭現場驗證，再完成受控家庭更新／退版整案。尚未通過的 coding 模式保持 blocked，離線 Python、固定模型 transport 與 HAOS lifecycle 不能代替 E1／E2。
