# 統一發行與上游追版

2026-10-12 Owner 方向：一般使用者跟隨 Agentic Smart Home 發行的受測版本組合；開發端負責追蹤 Hermes、Home Assistant 與 Linux 上游。這是後續實作規格，**沒有將 0.6.1 改成全相依釘版或全產品更新器**。現有能力見 [Updates](UPDATES.md) 與 [驗證](VERIFICATION.md)。

## 使用者體驗

提供一個產品版本、一個更新入口與 Stable／明確選入的 Preview 通道。畫面只需說明改善內容、服務中斷、重啟與可用的恢復方式，讓使用者選維護時間。背景可檢查及下載；正式切換仍需精確批准或事先設定的更新政策。

建議一般使用者不要另跑 `hermes update`、自行更換產品 image 或在 HA／Linux 個別升級受管理元件。進階 Owner 保有機器管理權；偵測手動變動後，標示「版本組合尚未驗證」、保留原生 HA 與救援，重新檢查並只暫停受影響的 Agent 能力。不能偷偷降版、覆蓋家庭資料或修改 HA 前端來隱藏原生更新入口。

**首次安裝應使用本次發行已驗證的 Hermes image digest。** 這取代先前「使用者首次設定時追 upstream HEAD」的產品目標；「最新」改指最新受測產品發行。上游最新 commit 的解析、建置與測試留在開發／候選環境。0.6.1 bootstrap 尚未切換，不能宣稱已交付。

## 鎖定範圍與例外

| 元件 | 目標鎖定方式 | 邊界 |
|---|---|---|
| 自家 Controller／Runner／adapter／Web | 不可變 image digest、來源 commit、schema／protocol | Preview 通過後將同一產物升為 Stable，不重新建置 |
| Hermes、CLI、LINE.js 與語言相依 | commit／archive hash、base image digest、完整 lock、預建 image | 由我們封裝更新，不在使用者機器拉 main 或自由升級套件 |
| HAOS、Core、自家 Apps | HAOS image／版本、Core／App 精確版本、實際讀回 inventory | HAOS 磁碟 hash 不會釘住首次開機下載的 Core／Supervisor／plugins；每層另驗 |
| Supervisor 與其管理的元件 | 官方介面、最新 stable 快速相容驗證、實際版本讀回 | 官方只支援最新 Supervisor，不能承諾長期凍結或任意降版 |
| 外層 Ubuntu 與系統套件 | LTS、官方 archive snapshot、套件版本／hash inventory、簽章來源 | ISO hash 不等於 apt 套件固定；Snap、韌體與第三方 repo 也需盤點 |
| 雲端模型／OAuth／通訊服務 | 可指定的模型版本、API contract、行為測試 | 遠端服務可能變更或退役，本機 lock 無法凍結 |

Supervisor 可經官方選項調整自動更新，但非最新版不在官方支援範圍；指定 Supervisor 更新版本的 API 亦有限制。因此採快速驗證／修補的例外策略，不用封網、改內部檔案或 fork 假裝完全釘版。[官方支援範圍](https://www.home-assistant.io/more-info/unsupported/supervisor_version/)、[Supervisor API](https://developers.home-assistant.io/docs/api/supervisor/endpoints/)。

Ubuntu archive snapshot 可固定套件來源；第三方來源另需不可變產物。預設安全更新及服務重啟可能改變實際執行版本，必須一併管理、讀回及驗證。[Ubuntu Snapshot](https://snapshot.ubuntu.com/)、[自動更新](https://ubuntu.com/server/docs/how-to/software/automatic-updates/)。

## 開發端的候選流程

建立單一 release manifest 作為版本矩陣權威來源，現有各 component lock 由它校驗或產生。記錄 release ID、channel、組件版本／digest、預期及實際 inventory、Supervisor 支援條件、schema／protocol、升級起點、固定步驟相依、證據、簽章及到期／撤銷資訊。這是新契約設計，不改寫目前 Controller-only manifest 的語意。

維護者操作目標是「選組件／版本 → 建候選 → 看結果 → 發布」：

1. 從允許的官方來源發現更新，固定 revision／digest，自動產生 lock diff、上游變更與安全公告摘要。未在本輪建立任何背景排程。
2. 一次改一個上游組件；確有最低版本相依時明列最小必要組合。Agent 可研究、修 adapter、補測試及準備 PR，不能替自己簽發版本。
3. 自動執行 contract／行為測試及相應整合／VM 測試；除了新安裝，必須測受支援舊版原地升級、中斷續作、失敗恢復與資料保留。
4. 保存不可變候選、SBOM／來源與證據。Hermes 沿用 `scripts/check_upstream.py`；HA 與 Linux 接相同報告格式、各自執行測試矩陣。測試環境不帶正式發布私鑰。
5. Preview 試跑後，對同一批受測 digest 簽署 Stable 發行；可撤回問題 manifest，保留已知可用版本。產品只解析可信發行索引，不追上游浮動 HEAD。
6. updater 驗證發行者、digest、有效期／版本序列與適用起點，再執行已批准計畫。簽章不等於部署授權；明確退版需新批准，避免舊發行 metadata 重播。

正式媒體需內建經核對的公開信任根及輪替／離線驗證路徑；一般使用者不應手動完成工程版 publisher 登錄。私鑰不放 repo、產品 image、Agent prompt 或一般 PR CI。

## 更新與恢復分層執行

一個更新入口可以協調多個元件，但它們不是一筆可原子回復的交易。每步保存前後版本、資料格式、備份、批准、健康讀回及中斷狀態；不確定時先 reconcile。

- Hermes／自家 images：切換不可變候選，驗證設定及 session 格式；格式不兼容時不能只換回 image。
- HAOS／Core：使用官方更新及備份介面。HAOS 雙 boot slot 保護 OS 開機，不等於 Core DB、App 資料或整個家庭回復；Core 降版的備份恢復條件另驗。[官方更新與恢復](https://www.home-assistant.io/common-tasks/os/)。
- Linux：先確定 package snapshot、bootloader／kernel、網路、持久資料及磁碟布局的恢復方案。apt 降版或保留舊 kernel 不是整機可退版的證據；不能盲目還原舊家庭 DB。

手動變動、Supervisor 必須追新及雲端失效各有狀態；保留家庭原生操作與外層救援，只停止受影響的 Agent 寫入。

## 安全修補與交付順序

釘版代表我們承擔維護責任。正常 Stable 發行之外需有緊急修補路徑；建議初始作業目標為重大公告 24 小時內判讀、可受測修補優先在 72 小時內提供，未達成時說明狀態與緩解方式。這是待建立的團隊目標，不是已有監控、值班或 SLA。

**在安全修補供應、發布與恢復流程可用前，不全面關閉 Linux 安全更新。** 先完成套件／自動更新來源盤點與偏移偵測，再驗證維護時段內的受管理更新；Supervisor 例外獨立處理。不能讓使用者無限期等待我們發版。

| 次序 | 可交付項目 | 必要驗收 |
|---|---|---|
| 1 | 發行 manifest、首次安裝受測 Hermes、唯讀版本偏移偵測 | 同發行重裝不追 HEAD；錯簽章／digest／起點拒絕；既有帳號及 session 不重跑 bootstrap |
| 2 | 自動候選準備與 Hermes 更新 adapter | lock diff、報告、設定／session 遷移、停止接手、失敗恢復；不動正式帳號 |
| 3 | HAOS／Core／自家 App 受控更新 | Supervisor 支援條件、真 HAOS App／Core、資料保留、中斷續作及恢復 |
| 4 | Linux 套件快照、受管理維護與恢復 | 新裝及升級到同一矩陣；精確媒體 VM、斷網／重啟失敗及救援，再驗 laptop |

0.6.1 已有 Hermes 候選測試、Linux 能力檢查及同 schema Controller updater；上表仍是待實作增量。32 項產品驗收不因政策更新而改變。
