# 維護與復原

## Plan v3：不依賴 Web 的固定支援入口

包含新 support module 的 v3 技術預覽提供以下本機 console 命令。它們直接讀取外層的固定 metadata、systemd 狀態和 libvirt 的固定 VM 狀態；Controller Web、Host broker daemon、Hermes、provider 帳號和 HAOS 都不是執行前提。尚未安裝該候選的 0.3.1 主機不會自動得到新命令。

```bash
sudo agentic-home diagnostics
sudo agentic-home support-bundle --output ./agentic-support.json
```

`diagnostics` 在 terminal 顯示 JSON；`support-bundle` 建立新的 JSON 檔、權限 0600，拒絕覆蓋現有檔案或 symlink。使用 `sudo` 建立的檔案由 root 擁有。報告含 payload SHA-256 供檔案核對；它不是 publisher 簽章，也不會自動上傳。無足夠權限或工具未安裝時明確標示 `not_available`，不要求先把故障服務重啟。

每筆 observation 都有來源位置 `outer_host_console`、目標、時間、方法、版本和 limitations。內容只包含允許的數字、版本及有限狀態：installed/profile、host OS、產品資料所在 filesystem 空間、固定服務 states、HAOS VM state，以及最多 200 筆產品 service journal 的 priority 計數。每個 probe 限 3 秒／64 KiB，沒有任意 command、URL、路徑或 VM 選項。

一般 support bundle 不讀取或匯出 log messages、環境變數、cookies、provider profiles、Controller DB、用戶對話、Owner ID、IP、URL/query string 或任意私人檔案。journal 只取 PRIORITY 欄位並聚合；無紀錄不代表沒有錯誤。systemd active 不代表 container、Hermes 或模型可用，libvirt running 也不代表 Core/Supervisor 正常。真實家庭診斷仍需要各自授權的資料來源。

如果安裝後 CLI wrapper 損壞，但版本目錄與 Python 還可讀，可在 local console 使用同一固定 module：

```bash
sudo env PYTHONPATH=/opt/agentic-home/host /usr/bin/python3 -m agentic_host.support status
```

這些命令只做診斷和指定的新 support 檔案輸出，不停止或重啟服務、不修改 VM、不執行模型程式。既有 `agentic-home status` 仍是 broker lifecycle status；Web 或 broker 故障時改用 `diagnostics`。安裝、固定 updater／退版、實際 Owner laptop 救援的 v3 驗收狀態分開列在 [PROGRESS.md](v3/PROGRESS.md)；新增 unit tests 不代表整段 E0/E2 已完成。

## 停止與錯誤

「暫停 AI」停止新需求。取消規劃中的任務會停止 Runner process group；取消已部署規則的追蹤不會停用 HA 規則。停用規則請用「家庭自動化 → 預覽停用 → 批准」，緊急時直接在 HA 原生自動化頁停用對應 `agentic_` 規則。

寫入逾時／回應遺失時，任務會進入「需要核對」。點「讀回並核對現況」只查 exact ID、設定 hash 與執行狀態。完整最終設定可恢復追蹤；停用草稿可重新預覽啟用；外部修改則保留現況。不要為同一失敗操作換一個新 ID 重送。

## 備份

自動化部署使用 Supervisor partial backup：HA data，`addons: []`／`folders: []`，等待完成並讀回 slug；不把正在執行的 Controller 放入該次 cold backup。Controller 另用 SQLite 原生 backup API 留一致快照。

原生 Apps 備份採 cold 模式。Runner 排除 `profiles/*`、`work/*`、`private/*`；已用 Supervisor 2026.09.3 真正的 archive/filter 實作確認巢狀登入資訊不進 archive。完整 HAOS 現場備份／還原仍需在你的 appliance 執行確認。

Controller 的「下載加密備份」只含 Controller DB，含配對資料、policy、任務與 ledger，不含 provider profile。保存到另一台設備；密碼另存。日常也使用 HA 原生加密備份涵蓋 Core 與 Apps，保留 HA emergency kit。

## Controller 獨立快照還原

通常優先使用 HA 原生 App backup restore。獨立的加密 DB 快照適合離線維護及災難取回資料：

1. 停止 Controller。保留現有 `/data/controller.sqlite3`、`-wal`、`-shm` 整組檔案；不要覆寫在線 DB。
2. 在可取得 App 離線資料目錄的維護環境，用 Python 3.13 與此專案依賴執行：

   ```bash
   uv run --no-project --python 3.13 --with cryptography==50.0.2 python scripts/restore_controller.py agentic-home-backup.enc /offline-app-data/controller.sqlite3
   ```

3. 程式互動讀取密碼、驗證 AES-GCM、SQLite integrity 與 schema，再建立不存在的目標檔案。目前接受 Controller schema 1 或 2，保留備份本來的版本，不把 2 轉回 1。目標已存在或解密失敗會拒絕，不覆蓋原 DB。
4. 將已還原資料放回該 App 的離線 data 目錄，使用相容版本啟動。0.4.0 開啟 schema 1 會依下方流程遷移至 2；0.3.x 不能開啟 schema 2。App 啟動會修正其 DB 的 UID，未完成部署轉入核對，不自動重播。

這個路徑不需要把主機 shell 開給 Agent。只有 Samba 一般分享時，通常無法直接取得 App 私有 `/data`，請採 HA 原生備份還原。不要把加密 Controller 快照當成包含 Core 的全機備份。

## Runner 還原與配對

原生 Runner 備份不含 CLI credentials／TLS private key。還原時產生新憑證，broker 會清除舊配對綁定；在記錄複製新的 JSON，從 Controller 重新配對，再完成官方登入。若只有 Controller 遺失、Runner 仍綁定舊實例，先使用對應 Controller 備份恢復；確定無法恢復時，重新安裝 Runner 後配對並登入。

還原同一家庭的 HA backup 會恢復原有 owner 身份；只把 Controller DB 搬到另一個全新家庭不是支援的跨家庭遷移流程。

## 更新

- **0.4.0 Controller 資料格式為 schema 2。** 雖沿用現有 SQL tables，Gateway 的 `coordinator` 等新欄位與 v3 records 語意已超出 0.3.x 的讀取契約，不能宣稱同格式相容。新建 DB 直接使用 `PRAGMA user_version=2`。
- 開啟 schema 1 時，先用 SQLite backup API 在相同私有目錄建立唯一的 `controller.sqlite3.before-v2-<uuid>.sqlite3`，權限 0600，再於交易中將版本 marker 更新為 2；原 settings、records、audit 與去重紀錄保留。舊版 binary 會拒絕 schema 2，不能藉手動修改 `user_version` 繞過。
- 遷移前快照含 Controller 私有資料，沒有因 0600 就加密；不可放進一般 support bundle 或公開 evidence，離機保存必須加密。這份快照不含遷移後的新操作，也不是自動退版機制。不要為了換回舊 binary，直接把它覆蓋到已有新家庭操作的 DB。
- 更新 Apps：先保留原生 App backup／加密 Controller 快照，再由 HA 原生管理頁更新。0.3.x → 0.4.0 的資料遷移與版本部署是不同步驟；目前沒有固定 in-place updater 或可聲稱安全的原地降版／rollback。需要恢復時先停止相關服務、保留現況、核對家庭副作用與 ledger，再採對應版本的離線恢復流程。
- 更新 Core／Supervisor／HAOS：使用 HA 原生 UI。App 只允許已通過真實自動測試的 Core 版本使用自動化寫入 adapter；版本未涵蓋時更新 Agentic Home，既有 HA 規則仍由 HA 執行。
- OS A/B、HA data restore、App DB restore 是不同操作。不要用舊 Core binary 直接開新版本資料來當還原。
- 移除兩個 Apps 不會刪除其已建立的 HA 自動化。若要完整退出產品，先在 HA 檢查並停用／刪除需要移除的規則。

## 保存與觀測

預設 audit 30 天、Agent 觀測資料 7 天，可在設定縮短。活躍任務、當前 managed automation 的操作依據與 idempotency tombstones 保留以支援安全恢復。到期 Controller snapshots 清除；HA 原生 backups 的保留／離機同步由 HA 備份管理處理。

外層模式更新 `context_days`（1–7 天）前，Controller 先呼叫 Hermes 私有 `POST /v1/retention` 同步保存上限。縮短期限會讓 runtime 停止進行中的 turn，再清除過期 session／context，避免完成中的舊 turn 重新寫回已刪前文；調長不恢復已刪資料。若同步失敗，API 回 503，Controller 的原設定保留，不會只更新 Web 顯示就宣稱所有上下文已同步清除。恢復 Hermes 服務後可重試；最小去重 tombstones 不含原始對話。

Runner job 留經過時間、CPU 與取樣 RSS；超時、記憶體或輸出上限會停止 job。原始 CLI stdout 不保存。雲端用量／金額沒有可信數據時不顯示推算數字。

從「任務紀錄」匯出的證據含需求文字與授權 entities；可能是家庭敏感資料，分享前確認。App 不匯出 provider secrets。
# Linux product 0.2 deployment

The primary deployment runs Controller and Runner on the outer Linux host. Use [INSTALL.md](../INSTALL.md) and [LINUX_PRODUCT.md](LINUX_PRODUCT.md) for host lifecycle and snapshots. The App-specific Supervisor restore instructions above apply only to the retained Ingress deployment mode. Plan v3 adds the Hermes coordinator and separate coding profiles progressively; see [the authoritative plan](v3/PROJECT_PLAN_V3.md) and [current progress](v3/PROGRESS.md).

Outer Linux data is in `/var/lib/agentic-home/`. To back it up consistently, stop `agentic-home.service` before copying Controller/Runner files, protect the copy with encryption, and restart the service afterwards; HAOS continues to run independently. Runner profiles contain provider credentials. A cold backup of that directory is not a full HAOS disk backup; use `agentic-home snapshot` for HAOS, and copy completed snapshot directories off-machine with encryption.

Do not restore the HAOS disk while it is running. After a whole-guest restore, reconcile Controller's automation ledger with actual HA config before further writes. The current installer refuses to overwrite `/opt/agentic-home` with a new release; an in-place distribution upgrade is not supplied as an unattended operation.

## 0.5.0 首次設定恢復

`bootstrap_v1` 和短效通道配對是 schema 2 的新增記錄，沒有移動既有家庭 journal；這不构成 0.5.0 → 0.4.0 原地退版保证。引導 GET 只讀，啟動／重試會保存固定 job ID；回應遺失先核對原工作，不重新派相同副作用。

初次 Hermes 安裝使用 `/var/lib/agentic-home/host/runtime.env` 保存通過檢查的 image digest，systemd 啟停都讀取它。手動使用 Compose 時也必須傳入 `--env-file /var/lib/agentic-home/host/runtime.env`，否則會回到安裝包 image。已有認證／session 的 runtime 不允許走首次更新。`needs_reconcile` 必須核對原 host 操作與容器，不能刪記錄強迫重播。

桌面票據失效時重開 Agentic Home 捷徑即可；key 登入仍是備援。HA 官方授權失敗會顯示固定錯誤碼；中斷的 exchanging 不重用 auth code，可重新開啟新的授權流程。tokens 留在私有 storage，不放入支援報告。
