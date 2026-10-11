# 維護與復原

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
   uv run --no-project --python 3.13 --with cryptography==48.0.1 python scripts/restore_controller.py agentic-home-backup.enc /offline-app-data/controller.sqlite3
   ```

3. 程式互動讀取密碼、驗證 AES-GCM、SQLite integrity 與 schema，再建立不存在的目標檔案。目標已存在或解密失敗會拒絕，不覆蓋原 DB。
4. 將已還原資料放回該 App 的離線 data 目錄後啟動。App 啟動會修正其 DB 的 UID，未完成部署轉入核對，不自動重播。

這個路徑不需要把主機 shell 開給 Agent。只有 Samba 一般分享時，通常無法直接取得 App 私有 `/data`，請採 HA 原生備份還原。不要把加密 Controller 快照當成包含 Core 的全機備份。

## Runner 還原與配對

原生 Runner 備份不含 CLI credentials／TLS private key。還原時產生新憑證，broker 會清除舊配對綁定；在記錄複製新的 JSON，從 Controller 重新配對，再完成官方登入。若只有 Controller 遺失、Runner 仍綁定舊實例，先使用對應 Controller 備份恢復；確定無法恢復時，重新安裝 Runner 後配對並登入。

還原同一家庭的 HA backup 會恢復原有 owner 身份；只把 Controller DB 搬到另一個全新家庭不是支援的跨家庭遷移流程。

## 更新

- 更新 Apps：先保留原生 App backup／加密 Controller 快照，再由 HA 原生管理頁更新。這版只使用 schema 1，較新的不相容 DB 不會自動降級。
- 更新 Core／Supervisor／HAOS：使用 HA 原生 UI。App 只允許已通過真實自動測試的 Core 版本使用自動化寫入 adapter；版本未涵蓋時更新 Agentic Home，既有 HA 規則仍由 HA 執行。
- OS A/B、HA data restore、App DB restore 是不同操作。不要用舊 Core binary 直接開新版本資料來當還原。
- 移除兩個 Apps 不會刪除其已建立的 HA 自動化。若要完整退出產品，先在 HA 檢查並停用／刪除需要移除的規則。

## 保存與觀測

預設 audit 30 天、Agent 觀測資料 7 天，可在設定縮短。活躍任務、當前 managed automation 的操作依據與 idempotency tombstones 保留以支援安全恢復。到期 Controller snapshots 清除；HA 原生 backups 的保留／離機同步由 HA 備份管理處理。

Runner job 留經過時間、CPU 與取樣 RSS；超時、記憶體或輸出上限會停止 job。原始 CLI stdout 不保存。雲端用量／金額沒有可信數據時不顯示推算數字。

從「任務紀錄」匯出的證據含需求文字與授權 entities；可能是家庭敏感資料，分享前確認。App 不匯出 provider secrets。
# Linux product 0.2 deployment

The primary deployment runs Controller and Runner on the outer Linux host. Use [INSTALL.md](../INSTALL.md) and [LINUX_PRODUCT.md](LINUX_PRODUCT.md) for host lifecycle and snapshots. The App-specific Supervisor restore instructions below apply only to the retained Ingress deployment mode.

Outer Linux data is in `/var/lib/agentic-home/`. To back it up consistently, stop `agentic-home.service` before copying Controller/Runner files, protect the copy with encryption, and restart the service afterwards; HAOS continues to run independently. Runner profiles contain provider credentials. A cold backup of that directory is not a full HAOS disk backup; use `agentic-home snapshot` for HAOS, and copy completed snapshot directories off-machine with encryption.

Do not restore the HAOS disk while it is running. After a whole-guest restore, reconcile Controller's automation ledger with actual HA config before further writes. The current installer refuses to overwrite `/opt/agentic-home` with a new release; an in-place distribution upgrade is not supplied as an unattended operation.
