# Coding workers、成果與接手

0.6.0 把 Host coding 接到現有 Controller → TLS Runner → 固定 Host broker。`home_operator` 仍使用原本受限的結構化命令；新增的 `research`／`development` 使用另一份登入與真正 tools-enabled Codex。Hermes 依每個 worker 的 coding 設定派工，不繼承 onboarding 或 Gateway 所選的家庭 CLI。

## 使用

在「開發工作」選外層助手，完成獨立 Codex device code 或 API key 登入，描述問題。Host 工作者只讀封裝內的產品來源，在私有 clone 調查、修改和跑測試。CLI 登入資料不複製到 Hermes 或模型的工作區。尚未登入時不送出工作；`configured` 表示資料保存，成功模型工作才記為 `inference_verified`。

目前 enabled provider 為 **Codex 0.157.1**；Claude、Antigravity、Grok 的家庭模式／初始化能力繼續保留，但不冒充已完成的 coding adapter。Home worker 只有在真正 HAOS App 的硬邊界可用時才可啟用；外層容器不能充當 Home 證據。

Web 會保留調查、差異、檔案指紋與實際程序觀測。worker 自己的結論仍是不可信內容。修改成功後，另一份無帳號、無網路的乾淨 clone 套用同一 patch，執行封裝基準提供的固定測試；原 worker 不能修改 verifier 的測試／設定。驗證器先載入封裝內的 pytest，再加入候選程式的 import 路徑；除了退出碼，還要求測試有收集、有執行且完整結束，避免尚未測試就退出的假成功。候選 Python 仍在白箱測試程序內執行，因此這是有限的測試完成紀錄，不是抵抗任意惡意候選的密碼學證明。這個有限測試集合不代表任意修改已通過完整 CI，也不形成部署批准。

## 執行邊界

- 安裝包含只針對本 public repo `HEAD` 的 Git bundle，以及 commit／bundle SHA-256；建置拒絕未提交的產品來源。installer 驗證後放到 root-owned `/var/lib/agentic-home/coding/source` bare repo。每個 worker 再建獨立 clone，不共用可写 Git metadata。
- 固定 Host broker 持有 Docker 與 ext4 volume。Runner 只有固定 Unix broker 接點；模型程序無 Docker socket、Controller／HA／Supervisor bearer、Owner cookies 或 provider bearer。
- worker UID 10000、network none、read-only root、capabilities 限制、cgroups CPU／memory／PID 配额與固定 watchdog。所有可寫工作目錄在有界 ext4 磁碟；不能以輪詢資料夾大小代替 quota。
- provider／公開文件出站經受控 Unix relay。上游 HTTPS 主機、IP、方法與路徑受限；拒私網 DNS 與任意 redirect／遠端 MCP，provider bearer 在 broker 加入。模型與公開來源的請求數、內容大小與期限受限。
- 任務初期最多 300 秒、同時一個 coding job。writer 和 verifier 都必須證明停止，才釋出全域 lease。關頁或 HTTP 回覆遺失不表示取消。
- 成果在確認停止後匯出，拒 symlink／hardlink／特殊檔案、越界、超量、非允許類型及可辨識的憑證格式。去密模式不能證明所有內容都適合公開；成果留在本機。

## 重送、取消與接手

工作 envelope 使用 canonical digest。Controller 先保存 handle，再 dispatch；相同 Idempotency-Key／payload 取得原工作，不產生第二 writer。重新啟動或回覆遺失先查 exact job；未能確認停止時維持 `stop_unconfirmed`／`needs_reconcile`，新工作被擋住。

「核對停止與工作狀態」讀取固定 runner／broker 的容器 ID、boot／process 身份與停止證據。不能只刪 marker 或把狀態改 cancelled。確認停止後可下載接手檢查點及 SHA-256 綁定的 diff，在新的私有 checkout 接手；此版的 `live_session:false` **不是**直接加入同一個 BAT／CLI 可寫 session。

## 畫面預覽

工作產物含 `index.html` 時，可產生 1440／768／390 三個寬度的截圖。固定 renderer 使用獨立、無 credentials、network none 的瀏覽器容器。候選 HTML 不在管理中心 origin 執行。

PNG 經 `127.0.0.2:8100` 的獨立 host origin 與隨機短效連結查看；管理頁只連到 PNG，30 分鐘失效，可提前關閉。遠端 SSH 指令包含這個 listener 的 forwarding，沒有把主機管理埠公開到 LAN。此版交付靜態畫面預覽，尚非互動候選站台。

## 內容期限

Controller 會將保存期限傳給 Hermes 和所有已配對 Runners。縮短期限先生效；離線服務留下同步 barrier，未完成同步不派新工作。各服務在 idle 時也清除內容，延長保存不會復活已刪資料。有界去重 tombstone 保留 ID／digest 與狀態，不保留原 prompt／artifact，也不能重跑舊請求。

## 驗證範圍

`test_worker_controller.py`、`test_worker_execution.py`、`test_runner_lifecycle.py`、`test_worker_ui.py` 測派工、實際 CLI 工具、恢復與介面；`installed_coding_probe.py` 僅可在 installer 的一次性 VM 執行，用真磁碟 quota、真 Codex、獨立 verifier 與固定模型 transport fixture 驗證整條 Host 路徑。

真實模型帳號、研究品質、Owner laptop、Home App coding 和家庭副作用仍各自記在 [v3 驗收帳本](v3/PROGRESS.md)。工程測試不批次標記 E1／E2 或 32 案通過。
