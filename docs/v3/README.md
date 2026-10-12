# Plan v3 閱讀與實作入口

本頁是開發團隊補建的閱讀說明，並非上傳原稿。Owner 這次實際提供 **6 份 Markdown**；提及的完整 ZIP、原始 README 與 SOURCES 未隨這六個檔案提供，因此沒有把它們算成已收到或還原的原件。

## 閱讀順序

1. [DEV_TEAM_BRIEF.md](DEV_TEAM_BRIEF.md)：固定一個外層 Hermes、Host/Home 各一個真正 coding worker。
2. [PROJECT_PLAN_V3.md](PROJECT_PLAN_V3.md)：E0 → E1 → E2 → E3 產品與啟用順序。
3. [PROJECT_SPEC_V3.md](PROJECT_SPEC_V3.md)：身份、隔離、觀測來源、部署與恢复不變條件。
4. [WORKER_PROTOCOL_V1.md](WORKER_PROTOCOL_V1.md)：case/job、能力、去重、lease、取消與人工接手。
5. [BACKLOG_V3.md](BACKLOG_V3.md)：16 項開發工作、相依與三條並行工作線。
6. [PILOT_ACCEPTANCE.md](PILOT_ACCEPTANCE.md)：32 個分階段現場驗收。

六份原稿逐 byte 保留，上傳檔名、SHA-256 與大小記於 [IMPORT.json](IMPORT.json)。匯入時已與上傳檔及私有 parent repo 的 `docs/v3/` 逐檔比對相同。不要直接改原稿來表示實作進度；修訂 steering 時另存新版，日常結果寫入 [PROGRESS.md](PROGRESS.md)。

[根 Plan](../../PROJECT_PLAN.md)、[根 Spec](../../PROJECT_SPEC.md)、[AGENTS.md](../../AGENTS.md) 是現行 repo 入口；[pre-v3 0.3.1 history](../history/pre-v3-0.3.1/README.md) 封存變更前原件。沒有另外收到 v2 套件，不將這些 0.3.1 文件假稱為完整 v2 原稿。

## 團隊補充文件

- [SOURCES.md](SOURCES.md)：補足原文中的 r01–r07、u01–u07 引用，使用原基準 commit 與查核過的官方上游；不是聲稱拿到了缺失的原始來源索引。
- [PROGRESS.md](PROGRESS.md)：開發狀態與 32 項驗收分開追蹤。`not_run` 表示整個驗收案例尚未執行，fixture 測試不能自動升為 `passed`。
- [OPERATIONS.md](../OPERATIONS.md)：固定 console 診斷／support bundle 與現有備份恢復方法。

此目錄中的 v3 是方向版本。實際程式版本見根 `pyproject.toml`、候選版本 manifest 和測試證據；`0.3.1` 舊測試不代表 Hermes、隔離 coding 或 HAOS App 研究已完成。E0 工程預覽也不能冒稱 E1 真實助手或 E2 雙層驗收。
