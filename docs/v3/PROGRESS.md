# Plan v3 進度與驗收帳本（團隊維護）

## 2026-10-26 接續開發

0.11.0 的八階段與公開交付追蹤 #118 已完成：完整單輪 010 為 3,877 passed／8 skipped；公開 App 010-001 為 82 passed／0 skipped，原媒體與後補驗證 ZIP 均已核驗下載及清理。媒體與 stable 保持 F `40fdc65035ba082e5c875b739d34a3a7ee76d7c7`，tag 保持 M `df25dae051e2d067da97eb5b60852b2f7d37ea88`；[交付摘要](../../evidence/laptop-40fdc65/summary.json) 保留各自來源，不能套用到後續候選。

後續未發行增量沿 #14 的家庭體驗繼續：管家優先首次設定、IPP／Shelly 1／Synology 的受限聊天接入，以及主機聊天內家庭網路接入與原工作續查。接入後仍需明確選擇探索與分享；HA 側欄保留權限邊界。功能與驗證範圍見 [設備探索](../DEVICE_DISCOVERY.md) 及 [驗證紀錄](../VERIFICATION.md)。

全品牌設備、完整安裝器體驗、實體 Dell、真帳號／家庭、72 小時與離機復原仍待後續開發或現場驗收；32 案維持 2 passed／30 not_run，不因工程測試升級。以下 0.10.0 與更早記錄為當時歷史。

## 0.10.0 專用筆電工程預覽

0.10.0 整合多來源設備探索、接入追蹤、原生 HA 示範、安裝前 KVM／記憶體檢查，以及可選的有線 HAOS 家庭區網。Host 保留管理 NAT；HAOS 的 LAN 位址、探索介面與內部網址由原生 API 讀回。切換接線需正常關機，並核對操作紀錄、VM／網路身分與自動啟動狀態。

工程驗證與公開交付結果：同一候選本機八階段（2+6 接續）已通過；公開 App：逐階段接續通過（enable 42、bridge-haos 40，清理完成）；匿名下載：Linux 完整下載與校驗通過，暫存清理完成。見 [本機八階段與公開兩階段摘要](../../evidence/laptop-d0089a8/summary.json)、[CI／下載驗證範圍](../VERIFICATION.md) 及 [v0.10.0 發布頁](https://github.com/TED-H-com/agentic-smart-home/releases/tag/v0.10.0)。媒體／本次 stable 測試來源固定為 `d0089a80e358cbd776a89327f0e58bfba68c671b`；Home coding 來源為 `0886a5459984e916e8dc6a881a1bd6d4249934fd`，後續文件更新不改這些身分。

實體 Dell、真雲端帳號、家庭設備與 72 小時運作仍待現場驗收；Windows 原生下載工具及 Grok 實際執行均為 `not_run`。32 案維持 **2 passed、30 not_run**，E1／E2 不因工程案例通過而升級。此檔是 companion 帳本，六份原稿與歷史 0.9.0 證據保持原樣。

本輪優先完成 0.10.0 同版媒體交付；Owner 現場依 [第一次實測順序](../../INSTALL.md#第一次實測順序) 驗收有線／USB Ethernet、真 Hermes 登入、Host 小修改、Home 調查、單一低風險燈的實體讀回、瀏覽器保持開啟的整機重開，以及恢復後第二個工作。這些項目尚無現場通過紀錄；重開與斷線均沿用原 job ID 核對，保留未確認停止的 lease fence，不以新工作繞過。

## 0.9.0 與早期開發紀錄（歷史）

以下各段保留當時的版本與驗證狀態；其中「未發布」「目前」指記錄當時。0.10.0 的交付狀態以上節及其精確證據為準。

2026-10-16 接續開發：**0.9.0 已公開發布為工程預覽**，52 份附件與完整匿名下載均已核驗，見 [發布紀錄](../reviews/2026-10-16-RELEASE-PUBLISHED-53c9e79.md)。以下通知、接手與更新器修正屬後續未發布增量；`main` 開發不移動既有 tag、`stable`、發行分支或受測媒體。真帳號、Owner laptop 與 E1／E2 的限制保留；公開 `#stable` 工程安裝已由 [第二輪公開交付](../reviews/2026-10-16-PUBLIC-DELIVERY-53c9e79.md) 驗證。

**目前：0.9.0 完整產品工程預覽。精確 ISO 在一次性 VM 完整安裝通過；真 HAOS 側欄 App、家庭現場開發助手、一鍵啟用家庭現場（含 cold 備份與整台重開）、HAOS 備份、真 Supervisor 建置家庭現場 App，以及公開 `#stable` 安裝的工程案例通過。模型 transport 是固定 fixture；真雲端帳號、實體筆電及 E1／E2 尚未驗收，32 案維持 2 passed、30 not_run。** 詳見 [版本驗證](../VERIFICATION.md#090)；0.8.0 的 Home coding 增量見 [下方](#home-coding-080-工程增量)。

更新：2026-10-15。Owner 原稿基準為 0.3.1／`3ad476a712009469521c3f9838a44ef35ba29f76`。本檔獨立記錄實作狀態，不修改六份原稿；早期交付為 **0.4.0 v3 技術預覽，CI 145 passed／2 skipped，精確 ISO 完整 VM 測試 1 passed；完成 E0 工程媒體與獨立救援範圍，Owner laptop／真帳號及 E1、E2 尚未完成**。


0.5.0 開發新增 CLI 先行的自動 onboarding：持久九步流程、桌面單次登入、官方最新 Hermes 相容性檢查、Hermes 原生 Codex OAuth、LINE／Telegram 配對碼、自動 HAOS 啟動及 HA 官方瀏覽器授權。這是 Owner 的新流程，不改六份原稿。當版CI為210 passed／3 skipped；精確0.5.0 ISO在一次性VM為1 passed／1310.79秒。T3-01與T3-05的E0工程範圍已重驗，其他30項不批次升級，詳見 [版本驗證](../VERIFICATION.md)。通用 Host／Home coding、真雲端家庭與固定產品更新／退版仍未完成。

0.5.1 本輪補上跨服務 E2E 與恢復修正，本機完整回歸 **271 passed／2 skipped**；真 HAOS 另為 **1 passed**。測試含實際服務／CLI／MCP／Core 瀏覽器授權，但模型及 Host 副作用為 fixture，見 [審查](../reviews/2026-10-12-E2E-ARCHITECTURE.md)。最終 CI **271 passed／3 skipped**；修正版精確 ISO **1 passed／1493.94 秒**，完成最新版 Hermes、HAOS 冷還原與重啟。下列兩案僅更新 E0 工程證據；其餘 30 案不批次升級。該版缺口由下列 0.6.0 工程交付接續。

0.6.0 本輪新增真正 tools-enabled Codex Host coding、獨立角色登入／受控出站、可信停止恢復、全鏈保存期限、成果與隔離截圖預覽、同 case 工作追蹤、家庭精確提案橋接、簽名候選 Controller 更新／退版，以及 Tailscale 選項。UI 已按 1440／768／390 px 截圖調整。這是程式交付；真帳號、Owner laptop、Home App coding 與完整 E1／E2 仍需要各自驗收。技術邊界見 [Workers](../WORKERS.md) 與 [Updates](../UPDATES.md)，本輪結果見 [驗證](../VERIFICATION.md)。

0.6.0 最終 CI **469 passed／4 skipped**；精確 ISO／harness `6847a03` 完整安裝 **1 passed／1789.412 秒**，含真 Codex 工具／固定模型 transport、最新版 Hermes、官方 HAOS、同 schema Controller 更新／退版、快照恢復與外層重啟。只更新 T3-01／T3-05 的 E0 工程證據，其餘 30 案仍為 `not_run`；真家庭與雲端整案不得以 fixture 代替。後續原生首頁與上游 adapter 整合見 [入口流程](../ONBOARDING.md#原生家庭首頁與智能管家)及 [相容邊界](../ADAPTERS.md)；當時 HA 側欄外層 bridge 尚未整合，本輪接續見下方 0.7.0。

Home 隔離基礎已有 [真 HAOS 18.3 App 證據](../../evidence/haos-app-boundary.json)：預設設定無法建立 mount namespace；增加 `SYS_ADMIN` 與限定 cgroup2 掛載位置的 AppArmor 診斷設定後，可在獨立 App cgroup namespace 委派資源限制並實際觸發記憶體 OOM kill。這修正了「平台無可行隔離路徑」的假設；當時尚未實作 Home coding launcher、模型憑證／出站／工作區完整隔離。最新實作與第十二輪通過的平台測試見下方 Home 增量，32 個案例的狀態不變。

0.6.1 接續原生 HA 日常入口、返回救援／管家、Hermes 私有接點 facade、候選 contract gate 與 Linux 固定動作能力檢查。真 Core 導向與對話接續已在隔離 E2E 通過，最終 CI **598 passed／5 skipped**，同來源 `083b80e` 精確 ISO 完整安裝 **1 passed／1568.186 秒**，包含最新 Hermes、HAOS、Controller 更新／退版及重啟，見 [最新紀錄](../VERIFICATION.md)；這些工程能力不新增真家庭驗收 passed 案例。

0.7.0 本輪實作 HA 智能管家薄 App 整合：官方 Ingress → 專用 TLS pairing → 外層即時 Owner 驗證，重用同一對話／case／成果與精確批准；側欄不呈現外層設定、不建立第二份 Controller DB。Web 協定 fixture 涵蓋相對路徑、草稿／撤權、PNG-only 與錯 digest；真 App／Supervisor 結果另記 [驗證](../VERIFICATION.md)，不能用瀏覽器 fixture 代替。Host gate 前本機完整回歸814 passed／4 skipped、CI812 passed／6 skipped；當時真正 HAOS 上公開來源安裝17 passed，原生側欄與同case續聊通過（模型仍fixture）。後續 gate、工作續查與媒體狀態見下方最新紀錄。本輪 Hermes 首次準備已改用安裝包受測 digest／bundled commit，加離線 probe，不在家庭端追 HEAD／build；精確媒體已完成獨立驗證，結果見下方。既有 32 案維持 2 項 E0 工程 passed、30 項 not_run。

最新 Owner 方向採 **HA 官方自管更新、外層只維護自家 App／adapter**；先前將 HAOS／Core 納入全產品釘版的政策已由 [發行邊界](../RELEASE_POLICY.md) 取代。新 onboarding 告知側欄安裝與有限維護；舊家庭需另行啟用，人工停止／移除／改設定不被自動抵銷。未知 Core 寫入仍拒絕受影響操作，不能宣稱所有最新版皆已支援。

## 公開示範的完整家庭流程（2026-10-16）

公開示範新增 Home Assistant 外觀的家庭總覽、設備控制、裝置與服務設定及自動化檢視；側欄載入原有智能管家產品畫面，與管理頁共用虛構家庭資料。這是示範頁交付，不增加原生 HA／實體設備驗收、不更換 0.9.0 媒體；詳見 [實作與驗證](../reviews/2026-10-16-NATIVE-HOME-DEMO.md)。

## 記錄規則

2026-10-12 較早一輪新增 [統一發行規格](../RELEASE_POLICY.md)：產品版本矩陣、改用受測 Hermes 初始化、版本偏移偵測、維護者候選流程及 HA／Linux 分層更新。該次僅同步方向文件；runtime、0.6.1 ISO／TAR 與 32 案狀態未變。其 HA 更新安排後由上方最新政策取代。當時的首次 latest／未完全鎖定 apt／Supervisor 支援例外均列為缺口；0.7.0 已改用隨附 Hermes digest，其餘限制仍按最新發行邊界記錄。

開發狀態 `not_run / in_progress / implemented` 只描述程式交付；案例結果使用 `not_run / blocked / passed / failed`。新增案例預設 `not_run`，不得因舊測試或相關 unit test 通過而批次改為 passed。具體功能因隔離不足 disabled，可在開發備註記錄原因；未實際執行整個案例仍保留 not_run。

每次驗收另附 test/case/job IDs、時間、執行者、source commit、candidate digest、安裝版本、runtime_location、model_backend、ha_backend、device_kind、CLI/Hermes/Core/Supervisor/OS 版本、觀測位置／方法、evidence refs、限制與批准的 Live 副作用。不得用單一 real=true 取代多維環境。

0.7.0 更新審查另補 Host 固定更新器能力握手；舊 Host 只換 Controller 不能啟用側欄。新 gate 定向 190 項、本機產品 790 項、安裝腳本 74 項均通過；來源 `9146b38` 的 CI 為 862 passed／6 skipped，真公開 App 在 HAOS 為 18 passed。後者使用真 Supervisor／Ingress 與 Owner 瀏覽器，Host 能力回應和模型仍為 fixture，不能代替已安裝 Host 或真帳號驗收。

舊媒體 `9146b38`／ISO `44aaa2ba…` 的首輪為 **1 failed／1116.214 秒**：安裝、登入／救援、sandbox、真 Codex 工具及隨附 Hermes 通過，HAOS provision 失敗；原診斷不足，根因仍未確定，見 [首輪封存](../../evidence/installed-0.7.0-run1-prior-stages/manifest.json)。同 ISO、診斷 harness `2f838451` 第二輪為 **1 failed／1511.092 秒**：HAOS／真 Core、Controller 更新／保留新 DB 退版及三次 bridge 固定 probe 均通過；snapshot 在回傳工作 ID 後，CLI 讀取狀態逾時。當時同一工作仍 `running`、HAOS `shut off`、ownership 相符，快照完成未確認，還原／重啟未驗；見 [第二輪封存](../../evidence/installed-0.7.0-run2-prior-stages/manifest.json)。兩輪 VM 與磁碟暫存均已清除，分段通過不等於完整媒體通過。

Host 已新增輕量唯讀 `GET /jobs/{id}`、`job --id`／`wait --id`；沿原 ID 續查，暫時讀取失敗有界重試，POST 不重送，也不放寬單次 socket 期限。**54 項定向測試通過**（含真 Unix socket／UID 邊界），[證據](../../evidence/host-job-polling-unix.xml)；Host／更新器／診斷回歸 **215 passed／4 skipped**，[證據](../../evidence/host-job-polling-regression.xml)。查無 ID、結果未確認或 `needs_reconcile` 均不能當作未執行而重新提交，見 [續查說明](../UPDATES.md#主機工作續查)。

最新來源 `cd0fe57725138da00bf67537f68f3fb9a0d7b150` 的 GitHub CI **942 passed／6 skipped／413.130 秒**，見 [CI 記錄](../../evidence/github-ci-0.7.0.json)與 [JUnit](../../evidence/pytest-0.7.0-github.xml)。同來源及 harness 的第三輪精確 ISO 完整安裝 **1 passed／2288.830 秒**；ISO SHA-256 為 `9c6f6d3e54a89454137a8db4760208d856cdfbb07d8bc7322d18bac5b2928c94`，見 [安裝結果](../../evidence/linux-install-0.7.0.json)、[JUnit](../../evidence/linux-install-0.7.0.xml)與 [交付記錄](../../evidence/release-0.7.0.json)。一次性 VM、容器與磁碟暫存已確認清除。CI 與獨立 ISO／HAOS VM 證據分開記錄；本輪只重驗 T3-01／T3-05 的 E0 工程範圍，不代表 Owner laptop 或真帳號已驗收。

最終安裝已完成 Ubuntu／outer-only firstboot、五映像核對、單次登入、Controller 停止救援、隔離 sandbox、真 Codex 工具／獨立驗證、隨附受測 Hermes 準備、HAOS 啟動、Controller 更新／保留新 DB 退版、冷快照還原及外層重啟。真 installed updater 的三次固定 probe 已驗支援映像通過、帶 revoked bridge 歷史時缺契約映像被拒、無該歷史時同衍生映像通過；另驗 pending App 操作阻擋及資源清理，見 [安裝後更新證據](../../evidence/installed-update-laptop-amd64-0.7.0.json)。缺契約映像、publisher／registry delivery 與模型 transport 均為明示 fixture；沒有將舊版 standalone Host compatibility 觀測算入本輪。

剩餘架構缺口包括 Home App 真雲端自主研究與現場驗收、同一 CLI／BAT live session 接手、雙 worker 真互補研究、原生 Assist、App 跨版本 migration／退版及舊 Host 的受控升級。App 更新已有 [下一增量設計](../reviews/2026-10-12-APP-UPDATE-NEXT.md)，U1–U8 未實作／待驗；目前 updater 仍只處理同 schema Controller。Owner laptop、真雲端／bot、實體家庭、相鄰 Core 與 72 小時試跑均未驗，E1／E2 未完成。原有 32 案維持 2 項 E0 工程 passed（本輪重驗）、30 項 not_run，不因本輪 CI 或相關工程能力批次升級。

## Home coding 0.8.0 工程增量

Owner 要求實作真正內層 worker 後，已新增 HomeService／HomeSandbox，沿用現有配對 TLS、持久 jobs、同 case evidence／artifacts、獨立 Codex coding 登入與固定 verifier。來源使用 App 內受固定 commit 核對的 bare repo，每件工作另建 clone；Home 專屬觀測只讀 App 事實與無帳號 Core API 連通性，標記 `haos_app`。`home_operator` 不解除既有限制。本輪為 **0.8.0 來源／App 工程預覽**，第十二輪完整平台案例已通過；不是 0.7.0 已發行媒體的新能力，也沒有重驗新 ISO。最新完整 ISO 證據仍為上方 0.7.0。

**當前工程結果：第十二輪 passed，receipt 324.151 秒**，來源／harness `7b2c4bd29d78f7f2f61e4e52d16569b8b08b20a0`，產品 runtime／映像沿用 `c19610ae`，封裝研究來源固定為公開 `bf831fc8e6082c886b988f4e1cbdfecab5238380`；[完整證據](../../evidence/home-worker-haos.json)。真 HAOS App 內 Codex 工具／patch、38 項獨立測試、同 case 內容及 digest、App 對 Core 80 的額外觀測、30 項 worker 邊界、硬磁碟／CPU／PID／記憶體限制、TLS 取消與正常重啟均通過。突然停止再啟動後仍保留 `stop_unconfirmed` 與原 lease，新工作未啟動、模型請求未重播；App 新程序及 Docker 啟動紀錄已核對，即使 namespace 編號重用也不誤當停止證明。VM／容器／磁碟暫存均已清理。這是固定模型 transport 與文件註解 patch 的工程測試，真雲端自主研究及 E2 仍未驗收。

真 HAOS harness 透過官方 Supervisor 安裝衍生 Runner App，明示只增加固定模型 transport／診斷 helper；保留產品 base layers、config 和 AppArmor。第六輪隔離啟動失敗仍保留 [原始結果](../../evidence/home-worker-haos-run6-isolation-failure.json)，後續修正不回改歷史。第八輪已通過硬 ext4 quota、CPU throttling、PID／記憶體耗盡、descendant 取消與資源回收，以及 enforcing profile、單一 `/dev/fuse` 規則與真 TLS 配對；其後因 coding 登入目錄權限失敗，整案仍為 failed，見 [第八輪](../../evidence/home-worker-haos-run8-role-login-failure.json)。

第九輪真 Codex 工具程序退出碼 0、產生來源 patch，30 項 namespace／秘密／FD／唯讀目錄／seccomp／出站邊界檢查全數 true；同一工作回報 6 份產物與 3 筆證據。獨立 verifier **37 passed／1 failed**，因固定測試搬移後找不到基準 `scripts/restore_controller.py` 而讓工作維持 failed，見 [第九輪](../../evidence/home-worker-haos-run9-verifier-layout-failure.json)。上述各輪 VM／容器／磁碟暫存均已清除；不得將 CLI 的成功升格為 verifier 或整案成功。

第十輪來源工作成功，獨立 verifier **38 passed**、30 項 worker 邊界檢查通過，case／觀測來源及內容 digest 均相符。整案仍失敗：probe 固定 8123，但真 Core 使用 80；現場唯讀讀回確認 80 回應 401、8123 拒絕連線。見 [第十輪原始失敗](../../evidence/home-worker-haos-run10-observation-failure.json)與 [獨立去密診斷](../../evidence/home-worker-haos-run10-observation-diagnostic.json)。VM／容器／磁碟暫存均已清除，沒有回改 failed。修正為固定 `homeassistant` 的 80 → 8123 有界無帳號探測。第十一輪已通過來源修改／獨立測試、同 case 內容／指紋、Core 80 回應 401、TLS 取消與正常重啟；最後因測試要求 PID namespace 編號必須改變而失敗，見 [原始結果](../../evidence/home-worker-haos-run11-restart-observer-failure.json)。[現場去密診斷](../../evidence/home-worker-haos-run11-restart-observer-diagnostic.json)證實 namespace 編號可重用，產品仍保留未知工作與原 lease；後續新工作阻擋／不重播斷言尚未執行。資源已清理；測試改用新程序 start ticks 加 Docker StartedAt 排除舊快取與 namespace 編號重用，後續第十二輪完整重跑通過，原第十一輪仍為 failed。固定模型回應和文件註解 patch 只驗工程連線，不證明真雲端自主研究或真實問題修復。V3-10 更新為工程實作 `implemented`，T3-13／T3-15、E2 及其餘 30 案維持 `not_run`；詳見 [Home worker 範圍](../WORKERS.md#home-coding-080-工程預覽)。

## 0.9.0 發布後修復（未發布工程增量）

- 停止後的接手檢查點與成果清單統一為最多 32 件；拒絕畸形、重複或其他工作的成果 ID。請求途中換綁 worker 或內容到期也會拒絕，不會釋放不確定的 lease；仍不是同一個 live BAT／CLI session 接手。
- 固定 Host 更新器在切換與失敗回復前，同時核對側欄與 Home App 的持久操作；停用不抵銷已送出的操作，畸形設定亦阻擋。此修正需要後續 Host 發行，已安裝的 0.9.0 不會因 Controller image 更新而取得新 Host 程式。
- 工作通知連結與選單同步精確工作 ID，支援同頁切換、返回鍵與最近清單外的工作；不存在、到期與暫時失敗分別提示。晚到回覆不能蓋掉新選取，連結導覽不重送工作，未送出的草稿保留。

本機整合定向 **422 passed、2 skipped**：工作／通知／App 維護 290 項、更新器 97 項及編譯介面瀏覽器 35 項通過。新增 47 項接手與 14 項通知連結 fixtures；三項變更均通過獨立來源審查。Ruff、22 份公開 contracts、鎖定 web toolchain、23 份共用 runtime、套件與文件連結檢查通過；Controller／Bridge 使用同一份重新編譯介面，1440／768／390 px 合成 API 截圖已檢視。pytest 均使用磁碟暫存並清理。

接續修正成果下載：GET 回應後再核對原 worker 身分，途中重新配對或取消配對均拒絕交付。新增 20 項固定 transport 案例，涵蓋 Host／Home、無關 worker 配對變動、畸形回應、大小／SHA-256 與保存期限；修正前 4 個途中換綁／解綁案例失敗，修正後連同工作、接手、通知及保存期限回歸為 **162 passed**。此組與上方測試部分重疊，不相加為獨立案例總數；公開契約與 DB schema 不變。

兩次 CI 在下載鎖定 Hermes 來源時收到 HTTP 429，均未進入 pytest。固定下載器因此補上最多三次的暫時 HTTP 重試、請求逾時及 `Retry-After` 秒數／日期處理；畸形或超出等待上限會拒絕，永久錯誤、TLS、雜湊及解壓縮錯誤不重試。原 URL、commit、SHA-256 與安全解壓邏輯不變。新增 49 項磁碟／網路模擬案例，連同 23 項候選工具回歸為 **72 passed**；沒有實際網路下載或等待，也沒有重新建置本機映像。

上述本機檢查使用 transport／Supervisor／模型 fixtures 與真 Chromium；本機未建置新映像或安裝媒體，也沒有新增雲端帳號、真 HAOS／家庭或 E1／E2 驗收。既有 0.9.0 發布證據保留原樣，32 案仍為 2 passed、30 not_run。

## 多來源設備盤點（2026-10-16，main 工程增量）

已接通 Host 實體 IPv4 介面的鄰居表、mDNS、SSDP 收集與 JSON／CSV／dnsmasq 路由器清單匯入。使用者可查看來源與辨識理由、保留未知項目、依建議建立接入追蹤、到原生 HA 完成設定，再讀回整合／設備／狀態；管家分享保留選取與預覽。流程已納入產品與公開 HA 介面示範，詳見 [實作與證據](../reviews/2026-10-16-MULTISOURCE-DISCOVERY.md)。

本機後端 373 項通過，含新 Host 47、Controller 61 和真 Core 1；介面／原生示範回歸 72、最後授權修正回歸 14、網站 8 項通過（批次重疊不相加）。公開示範本機整條流程預檢及 11 張截圖／9 個資源雜湊核對通過。真Core證據只涵蓋實際授權／登錄讀取與無副作用交接；外層收集器的loopback協定測試不當成現場LAN驗收。路由器來源目前採匯出檔案，不假裝登入未配置的防火牆API。現場品牌帳號與設備、VLAN／虛擬bridge／IPv6、72h觀測及最新媒體依各自證據處理，32案維持2passed／30not_run。

## 通用設備探索 D1（2026-10-16，未發布工程增量）

Owner 將家庭案例提升為通用產品概念：[設備探索與接入](../DEVICE_DISCOVERY.md)。本輪新增 Owner 唯讀 `GET /api/discovery`、contract 1 及設備頁探索區；讀取 HA 待設定探索流程、設備／實體 registry、整合設定與 API 狀態，保留各來源狀態、時間、版本和摘要。相同名稱不合併身份，Hub／子設備關係分開；缺資料或來源失敗不變成零設備／已接通。

本輪不新增 MCP 讀取範圍、背景掃描或家庭權限；路由器清單、直接 LAN 探索與品牌帳號收集器明示未實作。D2 多來源盤點、D3 對話引導、D4 逐能力與持續驗證各自交付。真實設備與網路覆蓋仍按 E3 驗收，32 案維持 2 passed、30 not_run，既有 0.9.0 媒體不變。

最終定向測試 **61 passed**（46 項探索後端、13 項探索瀏覽器、2 項既有設備頁回歸），隔離真 Core 2026.10.1 **1 passed**；建置、23 份契約、Ruff 與套件檢查通過。較廣回歸的先前失敗與磁碟延遲造成的既有 fixture 到期，完整保留於 [本輪驗證紀錄](../reviews/2026-10-16-DEVICE-DISCOVERY.md)，不宣稱本機完整套件全綠。

## 可接續的接入追蹤 D3b（2026-10-16，main 工程增量）

外層設備頁與 HA 側欄的分享視窗共用「接入進度」：Owner 對候選建立本地追蹤，經原生設定登入／配對，返回後明確選定精確整合，再分開讀回整合、關聯登錄設備與設備 API 狀態。候選消失不代表接通；同品牌實例不自動選取。Controller 保存精簡追蹤，支援重新整理／重啟接續、去重、revision 衝突與保存期限；新私有 record 不改 SQL schema 3，舊 binary 的清理限制已記錄。

這是 D3 的接續／讀回切片，沿用現有 HA 唯讀來源。路由器／LAN／帳號獨立來源、真品牌登入、實體動作與長期觀測仍待各自驗收；32 案維持 2 passed、30 not_run，既有 0.9.0 ISO／stable／tag 不變。公開示範以同一虛構記憶體家庭串接流程；產品持久化與身份邊界另以 Controller 測試驗證。驗證結果見 [本輪審查](../reviews/2026-10-16-DISCOVERY-SETUP.md)。

## 探索快照帶入管家對話 D3a（2026-10-16，未發布工程增量）

正式管家新增選取當次探索結果、預覽完整文字、帶入可編輯草稿的流程，外層 Web 與 HA 側欄共用。最多八項、整則 5,000 字，預設不選、不自動送出；名稱與來源文字只作資料。側欄僅新增已驗證 Owner 的探索 GET，讀取前後核對身份與家庭憑證。詳見 [實作與驗證](../reviews/2026-10-16-DISCOVERY-CONTEXT.md)。

本機後端 162、新流程瀏覽器 14、既有 UI 回歸 43 項通過；390／768／1440 px 完成視覺與鍵盤檢查。公開 demo 的探索答覆明示為固定示範，不建立提案或附上舊工作成果。正式 UI、側欄資源與公開截圖同步重建。

這是使用者主動分享歷史快照，不增加 MCP 或持續讀權；原生配對、模型資料分享與家庭操作權限維持各自邊界。D2、完整 D3／D4、真帳號與家庭驗收仍待執行，32 案維持 2 passed、30 not_run。既有 0.9.0 媒體與發布分支不變。

## V3-11 非同步工作完成通知（未發布工程增量）

在獨立 `codex/worker-completion-notifications` 分支，補上 coding 工作結束後回到原 LINE／Telegram／Discord 對話的固定狀態通知。只處理原 case 授權、仍有效的對話與已確認停止的終態；Web 手動工作不猜聊天室。通知只含工作狀態與既有 Web 工作路徑，不含原始成果、模型摘要，也不建立新的 Hermes 回合或家庭操作。這不改寫 `53c9e79` 媒體或發布來源。

工作終態與以 job ID 去重的通知意圖在同一交易保存；先確認原 bot 回覆已送達，再保存 `sending` 並等待磁碟持久化，之後重驗原對話、case、scope、帳號／provider／政策、收件範圍、暫停、更新 gate 與授權期限。尚未開始的離線通知可在同一授權仍有效時續送。送達不明、程序中斷或 adapter 非預期失敗保留 `unconfirmed`，不自動重送；一件 adapter 失敗不阻止另一件工作通知。開始外部傳送後的撤權無法收回已在途的訊息。

通知採私有 `worker_completion` record，不改公開 worker job、Runner contract 或 SQLite schema 3；不存 token、chat／sender 或訊息本文，按 context 保存期限和原對話存在性清理。同 schema 退版保留現有 DB，舊 binary 忽略此 kind 且不發送通知；重新使用本實作時只處理仍有效的 `pending`，`sending` 一律轉為 `unconfirmed`，不回填舊工作。舊 binary 不認得新增 kind 的清理，因此精簡收據 metadata 可能保留到再次使用本實作清理；這不是完整跨版本保存政策一致性的聲明。

本機 transport／Runner fixtures 定向 **115 passed／10.06 秒**，包含 41 項新增通知案例與既有 worker／Gateway 回歸；受影響檔案的 Ruff 檢查通過。涵蓋三通道、終態去重、撤權／跨 scope、未知 writer fence、交易回滾、持久化前失敗、重啟／取消不重送與 adapter 失敗隔離。沒有 bot 帳號、真模型、HAOS 或家庭副作用，未建置映像；真 bot 接收、雙 worker 互補研究及 E1／E2 仍待各自驗收，32 案不批次升級。

## 16 項開發工作

本輪分 A Hermes/Controller、B worker sandbox、C docs/support/交付，整合者管理共用契約。下表的 in_progress 並非可用能力或完成聲明；完整成果與相依仍見 [Backlog](BACKLOG_V3.md)。

| ID | 優先／階段／工作線 | 開發狀態 | 目前證據／缺口 |
|---|---|---|---|
| V3-01 | P0 / E0 / 整合 | implemented | 六原稿 hash 核對、history 與根文件同步；見 IMPORT.json |
| V3-02 | P0 / E0 / A | in_progress | 真 pinned AIAgent／官方 MCP 及獨立 bridge 已實作、真容器 needs_auth 啟動已測；模型端點 fixture，Owner 真帳號未驗 |
| V3-03 | P0 / E0→E1 / A | in_progress | Hermes Gateway→case/session/run、scope續接、撤權及UI已實作／測試；真 bot／現場身份重送未驗 |
| V3-04 | P0 / E0 / B | implemented | E0 獨立診斷／support；0.6.0 增加 root console publisher 登錄及固定 updater。0.7.0 精確媒體已驗 Controller 停止救援／固定 updater，並交付原工作 ID 唯讀續查；真家庭試跑另驗 |
| V3-05 | P0 / E0 / C | implemented | 0.7.0 source/harness cd0fe577，五映像、固定 Tailscale 套件及 public source bundle；精確 ISO 完整安裝 1 passed／2288.830s，VM／磁碟暫存已清理；實體 laptop 尚未執行 |
| V3-06 | P0 / E1 / A | implemented | MCP → Controller → TLS Runner → broker 持久派工、同 case handles／ACL／去重；Codex tools 真程序＋模型 transport fixture，真模型帳號未驗 |
| V3-07 | P0 / E1 / B | implemented | 真 Codex 工具執行、私有 clone、patch、獨立 verifier；bearer 留 relay、封鎖管理／私網出站、ext4／cgroup／watchdog。本項記錄 Host Codex coding；Home 見 V3-10 |
| V3-08 | P0 / E1 / A+B | in_progress | lease／實際 writer 停止與重啟 reconcile 已交付；可匯出停止後 checkpoint。live_session:false，尚非直接加入同一 BAT／CLI session |
| V3-09 | P0 / E1 / C | implemented | Web 問題派工、差異／SHA-256 成果、network-none renderer 三尺寸 PNG、獨立短效 origin／撤銷；真 Owner UI 問題與雲端整案未驗 |
| V3-10 | P1 / E2 / A+B | implemented | 0.8.0 HomeService／HomeSandbox 真 HAOS App 第十二輪完整工程案例 passed：Codex／38 項獨立 verifier／30 項邊界／同 case／Core 觀測／TLS 取消／正常重啟／突然中斷 fencing 與無重播，見 [證據](../../evidence/home-worker-haos.json)。固定模型 transport，真雲端自主研究與 E2 待驗；沒有新 ISO 聲明 |
| V3-11 | P1 / E2 / A+B | in_progress | 同 case 工作者狀態、下一輪 Hermes 證據 handle、觀測位置與 ACL 已接通；獨立分支新增 bot 固定完成通知／持久去重與撤權、重啟負向 fixtures，見上方工程增量。真 bot 接收與雙層互補研究仍未驗 |
| V3-12 | P1 / E2 / A+C | implemented | home.observe／propose／proposal_status 接回既有 Engine，精確 Owner 批准、撤權／暫停競態保護；真模型 helpers 整案待驗 |
| V3-13 | P1 / E2 / C | implemented | schema 3 私有遷移 snapshot；Ed25519 publisher／digest、獨立 probe、精確批准、quiesce／backup／health／保留新 DB 退版。真 installed probe 已驗 revoked bridge 契約及 pending App 操作阻擋；缺契約映像是明示衍生 fixture。僅同 schema 外層 Controller，不是全產品／HAOS 更新器 |
| V3-14 | P1 / E2 / 全組 | not_run | 尚無Owner laptop研究→修正→preview→批准更新→現場驗證→退版或雙worker真執行 |
| V3-15 | P2 / E3 / B+C | not_run | 未接入新的真實設備；LAN／USB及integration依現場授權逐項驗證 |
| V3-16 | P2 / E3 / 全組 | not_run | 72h、相鄰Core、離機恢復與三個單／雙worker對照均未執行 |

## 32 個產品驗收

以下每項仍需要 [Pilot Acceptance](PILOT_ACCEPTANCE.md) 的完整通過條件和對應環境證據。一次性 VM 與 Owner laptop、離線 sandbox 與真 cloud coding 分開記錄；相關工程測試不能代替尚未執行的整案。

| ID | 案例 | 階段 | 結果 | 驗收證據 |
|---|---|---|---|---|
| T3-01 | 一次性 VM 安裝待交付媒體 | E0 交付前 | passed | 0.7.0 source/harness cd0fe577／ISO 9c6f6d3e…完整 VM 1 passed／2288.830s，版本／五 images／outer-only firstboot／單次登入／Web／診斷／HAOS 冷還原與重啟；一次性磁碟與 key，已清理。見 [結果](../../evidence/linux-install-0.7.0.json)及 [JUnit](../../evidence/linux-install-0.7.0.xml)；真帳號與實體 laptop 未驗 |
| T3-02 | Owner laptop 真安裝與只選外層 | E0 現場 | not_run | — |
| T3-03 | Hermes 真實登入並回答一個工具查詢 | E1 | not_run | — |
| T3-04 | 保持 HAOS 未啟動，問主機狀態 | E1 | not_run | — |
| T3-05 | 關閉或故障 Controller 的測試實例 | E0 救援，E2 擴充 | passed | 0.7.0 E0 獨立救援：Controller 停止時非 sudo diagnostics／support 可用，安裝版本讀回正確，見 [rescue](../../evidence/installed-rescue-laptop-amd64-0.7.0.json)。另驗合成候選 Controller 更新／保留新資料退版及 bridge 固定契約阻擋；不代表 E2 真家庭整案 |
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

下一個產品驗收切片是在 Owner laptop 用真帳號，透過智能管家完成一件 Host 研究／修正／獨立測試／預覽的真實問題；相關受控出站、帳號隔離與 PNG 預覽已交付工程實作。Home coding 的真 App 隔離與完整工程鏈已通過，接著驗真雲端自主研究、雙 worker 互補調查、家庭現場驗證及受控更新／退版整案。失敗的邊界不能以 fixture 宣稱可用；離線 Python、固定模型 transport 與 HAOS lifecycle 不能代替 E1／E2。
