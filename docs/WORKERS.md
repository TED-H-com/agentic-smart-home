# Coding workers、成果與接手

0.6.0 把 Host coding 接到現有 Controller → TLS Runner → 固定 Host broker。`home_operator` 仍使用原本受限的結構化命令；新增的 `research`／`development` 使用另一份登入與真正 tools-enabled Codex。Hermes 依每個 worker 的 coding 設定派工，不繼承 onboarding 或 Gateway 所選的家庭 CLI。

## 使用

日常在「智能管家」描述需求，由 Hermes 依能力派工，成果回到同一段對話。第一次 coding 登入或手動派工可從「設定 → 進階工具」選外層助手，完成獨立 Codex device code 或 API key 登入。Host 工作者只讀封裝內的產品來源，在私有 clone 調查、修改和跑測試。CLI 登入資料不複製到 Hermes 或模型的工作區。尚未登入時不送出工作；`configured` 表示資料保存，成功模型工作才記為 `inference_verified`。

已發行版本的 coding provider 為 **Codex 0.157.1**；Claude、Antigravity、Grok 的家庭模式／初始化能力繼續保留，但不冒充已完成的 coding adapter。**0.7.0 的已驗證交付仍是 Host coding。** 本輪目標為 0.8.0 來源／App 預覽，實作同樣以 Codex 為首個 provider 的 Home coding；真 HAOS 整條流程尚未通過，不能將開發映像當成已交付的內層助手。

Web 會保留調查、差異、檔案指紋與實際程序觀測。worker 自己的結論仍是不可信內容。修改成功後，另一份無帳號、無網路的乾淨 clone 套用同一 patch，執行封裝基準提供的固定測試；原 worker 不能修改 verifier 的測試／設定。驗證器先載入封裝內的 pytest，再加入候選程式的 import 路徑；除了退出碼，還要求測試有收集、有執行且完整結束，避免尚未測試就退出的假成功。候選 Python 仍在白箱測試程序內執行，因此這是有限的測試完成紀錄，不是抵抗任意惡意候選的密碼學證明。這個有限測試集合不代表任意修改已通過完整 CI，也不形成部署批准。

## 執行邊界

- Host 安裝包含只針對本 public repo `HEAD` 的 Git bundle，以及 commit／bundle SHA-256；建置拒絕未提交的產品來源。installer 驗證後放到 root-owned `/var/lib/agentic-home/coding/source` bare repo。每個 worker 再建獨立 clone，不共用可写 Git metadata。
- 固定 Host broker 持有 Docker 與 ext4 volume。Runner 只有固定 Unix broker 接點；模型程序無 Docker socket、Controller／HA／Supervisor bearer、Owner cookies 或 provider bearer。
- Host worker UID 10000、network none、read-only root、capabilities 限制、cgroups CPU／memory／PID 配额與固定 watchdog。所有可寫工作目錄在有界 ext4 磁碟；不能以輪詢資料夾大小代替 quota。
- provider／公開文件出站經受控 Unix relay。上游 HTTPS 主機、IP、方法與路徑受限；拒私網 DNS 與任意 redirect／遠端 MCP，provider bearer 在 broker 加入。模型與公開來源的請求數、內容大小與期限受限。
- 任務初期最多 300 秒、同時一個 coding job。writer 和 verifier 都必須證明停止，才釋出全域 lease。關頁或 HTTP 回覆遺失不表示取消。
- 成果在確認停止後匯出，拒 symlink／hardlink／特殊檔案、越界、超量、非允許類型及可辨識的憑證格式。去密模式不能證明所有內容都適合公開；成果留在本機。

### Home coding 開發增量（尚未交付）

HomeService 已接現有 Controller → 配對 TLS Runner 的 jobs／auth／artifacts 契約，worker ID 為 `home-coder`。它在真正 HAOS Runner App 內執行；來源為 App 映像內的唯讀 bare repo `/opt/agentic-worker/source` 及固定 commit 記錄，每件工作另建 clone。coding 帳號獨立保存在 App 的 `/data/coding-auth`，不借用 Host、Hermes 或 `home_operator` 登入。既有家庭 Runner 的結構化限制不變。

新增 HomeSandbox 的 FUSE ext4 工作磁碟、程序 namespace、App 內 cgroup 資源限制與停止紀錄正在真平台驗證。App 只申請 `SYS_ADMIN`、自訂 enforcing AppArmor 與 `/dev/fuse` 裝置規則；沒有 host network／PID、full access 或 Docker socket。模型 worker 必須降為 UID 10000、唯讀 root、無有效 capabilities、有 seccomp／no-new-privileges；私有 staging、管理憑證及父程序 workspace FD 都不能傳入。這些是本輪必須實測的啟用條件，不能以啟動參數或單元測試代替。

Home 專屬觀測只讀 App namespace／資源事實，以及固定 `homeassistant` 的 `/api/` 無帳號連通性（80 優先，未取得 HTTP 回應時只再試 8123）；不回傳內容、不追 redirect，不提供 HA／Supervisor bearer 或任意 LAN 掃描。每筆標示 `observed_from=haos_app` 並回到同一 case。獨立 verifier 使用另一個無 relay、無帳號的 clone，執行封裝基準的固定測試。真 App 已執行 Codex 工具並產生 patch；第十輪獨立 verifier 的 38 項測試通過，同 case 來源及內容指紋相符，但固定 Core 觀測埠與真 HAOS 設定不符，完整整案仍失敗。

正常停止需先取消並證明 worker／FUSE 已退出，再回收工作區與 lease。同一 HAOS boot 中 App namespace 改變，不能單憑「App 已重啟」斷言舊 writer 停止；未知結果保持 `stop_unconfirmed` 並阻擋下一個 writer。這項 crash／restart 行為也仍待完整平台測試。

### Home App 的實測範圍

一次性 **HAOS 18.3** 的真正診斷 App 已比較兩種設定。預設 protected App 無法建立新的 mount namespace；增加 `SYS_ADMIN`，並啟用限定 cgroup2 掛載位置的 AppArmor 診斷設定後，可在自己的 cgroup namespace 建立子群組、設定 CPU／PID／記憶體限制，且實際觸發記憶體 OOM kill。兩種設定都沒有 Docker socket；這是 App 內的觀測，不能推論 Core 或整台 HAOS 的狀態。見 [實測資料](../evidence/haos-app-boundary.json)與 [JUnit](../evidence/haos-app-boundary.xml)。

這份早期證據沒有執行 Home coding CLI 或真雲端帳號，不等於新的 HomeSandbox 已通過。新 harness 使用官方 Supervisor 安裝以真 Runner 映像衍生的測試 App：保留產品 layers、config 與 AppArmor，另加明示的固定模型 transport／診斷 helper，不冒充原封 release 映像。

真 HAOS 18.3／Core 2026.10.1／Supervisor 2026.09.3 的第八輪已通過實際 ext4 ENOSPC、CPU throttling、PID 上限、記憶體 OOM、descendant 取消與 cgroup／工作磁碟回收；另核對 App 身份、enforcing profile、單一 FUSE 裝置規則與配對 TLS。該輪其後因獨立 coding 登入目錄權限失敗，整案仍為 failed，見 [第八輪原始結果](../evidence/home-worker-haos-run8-role-login-failure.json)。

修正登入目錄後，第九輪真 Codex 程序退出碼為 0，產出來源 patch；30 項 worker 邊界檢查全數為 true，包含 App／worker namespace 區分、管理秘密與私有 FD 隔離、唯讀 root、seccomp／no-new-privileges、直接 LAN／公網出站拒絕。工作取得 6 份產物及 3 筆證據，但獨立 verifier 為 **37 passed／1 failed**：固定測試移至另一目錄後找不到基準 `scripts/restore_controller.py`，因此整個工作正確維持 failed，未把 CLI 成功當作驗證成功。見 [第九輪原始結果](../evidence/home-worker-haos-run9-verifier-layout-failure.json)。兩輪 VM／容器與磁碟暫存均已清除。

第十輪來源工作成功，獨立 verifier **38 passed**，30 項 worker 邊界檢查通過；case／觀測來源／內容指紋均相符。完整測試仍於 Core 連通性檢查失敗：原固定 probe 使用 8123，但該 HAOS 的 Core HTTP 設定及實際 listener 為 80；現場唯讀核對為 80 回應 401、8123 拒絕連線。見 [第十輪原始失敗](../evidence/home-worker-haos-run10-observation-failure.json)與 [獨立去密診斷](../evidence/home-worker-haos-run10-observation-diagnostic.json)。原失敗未改寫，資源已清理。

修正採固定 80 → 8123 有界探測，每次保留安全結果，不能接受模型指定主機／埠。第十一輪待執行，尚須通過實際 App 連通性核對、TLS 取消、正常 App 停止／重啟，以及非正常重啟的未知 writer fencing；分段證據不等於整案通過。

測試的模型回應是固定 fixture，source task 只是封裝真原始碼的一筆文件註解修改；即使工程鏈通過，也不是自主修復真問題、真雲端登入或 E2 驗收。T3-13／T3-15 與其他產品案例仍分別保持待驗。

## 重送、取消與接手

工作 envelope 使用 canonical digest。Controller 先保存 handle，再 dispatch；相同 Idempotency-Key／payload 取得原工作，不產生第二 writer。重新啟動或回覆遺失先查 exact job；未能確認停止時維持 `stop_unconfirmed`／`needs_reconcile`，新工作被擋住。

Host 的「核對停止與工作狀態」讀取固定 runner／broker 的容器 ID、boot／process 身份與停止證據。Home 開發路徑沒有可任意採用的 Host Docker 停止證明。不能只刪 marker 或把狀態改 cancelled。確認停止後可下載接手檢查點及 SHA-256 綁定的 diff，在新的私有 checkout 接手；此版的 `live_session:false` **不是**直接加入同一個 BAT／CLI 可寫 session。

## 畫面預覽

工作產物含 `index.html` 時，可產生 1440／768／390 三個寬度的截圖。固定 renderer 使用獨立、無 credentials、network none 的瀏覽器容器。候選 HTML 不在管理中心 origin 執行。

PNG 經 `127.0.0.2:8100` 的獨立 host origin 與隨機短效連結查看；管理頁只連到 PNG，30 分鐘失效，可提前關閉。遠端 SSH 指令包含這個 listener 的 forwarding，沒有把主機管理埠公開到 LAN。此版交付靜態畫面預覽，尚非互動候選站台。

## 內容期限

Controller 會將保存期限傳給 Hermes 和所有已配對 Runners。縮短期限先生效；離線服務留下同步 barrier，未完成同步不派新工作。各服務在 idle 時也清除內容，延長保存不會復活已刪資料。有界去重 tombstone 保留 ID／digest 與狀態，不保留原 prompt／artifact，也不能重跑舊請求。

## 驗證範圍

`test_worker_controller.py`、`test_worker_execution.py`、`test_runner_lifecycle.py`、`test_worker_ui.py` 測派工、實際 CLI 工具、恢復與介面；`installed_coding_probe.py` 僅可在 installer 的一次性 VM 執行，用真磁碟 quota、真 Codex、獨立 verifier 與固定模型 transport fixture 驗證整條 Host 路徑。

`test_home_worker_haos.py --haos-vm` 是獨立、需序列執行的真 Home App 測試；一般單元測試會略過 VM。它要求實際磁碟／資源耗盡、worker 與 App／HAOS namespace 對照、秘密／出站負向檢查、同 case 產物與證據、取消及正常／非正常重啟。當前平台結果仍失敗，沒有新增 ISO 安裝或真帳號通過聲明。

真實模型帳號、研究品質、Owner laptop、Home App coding 和家庭副作用仍各自記在 [v3 驗收帳本](v3/PROGRESS.md)。工程測試不批次標記 E1／E2 或 32 案通過。
