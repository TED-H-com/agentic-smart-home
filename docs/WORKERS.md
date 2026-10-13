# Coding workers、成果與接手

0.6.0 把 Host coding 接到現有 Controller → TLS Runner → 固定 Host broker。`home_operator` 仍使用原本受限的結構化命令；新增的 `research`／`development` 使用另一份登入與真正 tools-enabled Codex。Hermes 依每個 worker 的 coding 設定派工，不繼承 onboarding 或 Gateway 所選的家庭 CLI。

## 使用

日常在「智能管家」描述需求，由 Hermes 依能力派工，成果回到同一段對話。第一次 coding 登入或手動派工可從「設定 → 進階工具」選外層或家庭現場助手，完成獨立 Codex device code 或 API key 登入。Host 工作者只讀封裝內的產品來源，在私有 clone 調查、修改和跑測試。CLI 登入資料不複製到 Hermes 或模型的工作區。尚未登入時不送出工作；`configured` 表示資料保存，成功模型工作才記為 `inference_verified`。

目前 coding adapter 的 provider 為 **Codex 0.157.1**；Claude、Antigravity、Grok 的家庭模式／初始化能力繼續保留，但不冒充已完成的 coding adapter。**0.7.0 的已驗證交付仍是 Host coding。** **0.8.0 來源／App 工程預覽已實作 Home coding，並完成真 HAOS 的整條工程測試**，同樣以 Codex 為首個 provider。模型 transport 仍是固定 fixture；真雲端自主研究、Owner 家庭試跑與 E2 尚未驗收。最新完整 ISO 證據仍為 0.7.0，本輪不宣稱新 ISO 已驗。

Web 會保留調查、差異、檔案指紋與實際程序觀測。worker 自己的結論仍是不可信內容。修改成功後，另一份無帳號、無網路的乾淨 clone 套用同一 patch，執行封裝基準提供的固定測試；原 worker 不能修改 verifier 的測試／設定。驗證器先載入封裝內的 pytest，再加入候選程式的 import 路徑；除了退出碼，還要求測試有收集、有執行且完整結束，避免尚未測試就退出的假成功。候選 Python 仍在白箱測試程序內執行，因此這是有限的測試完成紀錄，不是抵抗任意惡意候選的密碼學證明。這個有限測試集合不代表任意修改已通過完整 CI，也不形成部署批准。

## 執行邊界

- Host 安裝包含只針對本 public repo `HEAD` 的 Git bundle，以及 commit／bundle SHA-256；建置拒絕未提交的產品來源。installer 驗證後放到 root-owned `/var/lib/agentic-home/coding/source` bare repo。每個 worker 再建獨立 clone，不共用可写 Git metadata。
- 固定 Host broker 持有 Docker 與 ext4 volume。Runner 只有固定 Unix broker 接點；模型程序無 Docker socket、Controller／HA／Supervisor bearer、Owner cookies 或 provider bearer。
- Host worker UID 10000、network none、read-only root、capabilities 限制、cgroups CPU／memory／PID 配额與固定 watchdog。所有可寫工作目錄在有界 ext4 磁碟；不能以輪詢資料夾大小代替 quota。
- provider／公開文件出站經受控 Unix relay。上游 HTTPS 主機、IP、方法與路徑受限；拒私網 DNS 與任意 redirect／遠端 MCP，provider bearer 在 broker 加入。模型與公開來源的請求數、內容大小與期限受限。
- 中繼把 provider 回應逐段轉給 sandbox（chunked，每段 flush）。位元組之間的閒置上限是 300 秒；工作 deadline 仍限制總時間。回應累計超過 8 MiB 就中止，不先緩衝整份本文。ChatGPT OAuth 依固定版 Codex 的規則向固定的 `https://auth.openai.com/oauth/token` 更新：JWT 有到期時間時，只在 5 分鐘內到期才更新；沒有時，`last_refresh` 早於 8 天才更新。provider 在尚未送出任何回應位元組前回 401 時，只更新並重試該請求一次。更新端點回 401、`invalid_grant` 或 `refresh_token_expired`／`refresh_token_reused`／`refresh_token_invalidated`，或第二次 401／403，才記 `auth_failed` 並以 `needs_reauth` 回報。連線失敗、逾時或 5xx 是暫時性的，不要求重新登入。refresh token 與 id token 只留在憑證檔。API key 不呼叫該端點。這是程式行為，不是真帳號驗收。
- 任務初期最多 300 秒、同時一個 coding job。writer 和 verifier 都必須證明停止，才釋出全域 lease。關頁或 HTTP 回覆遺失不表示取消。
- 成果在確認停止後匯出，拒 symlink／hardlink／特殊檔案、越界、超量、非允許類型及可辨識的憑證格式。去密模式不能證明所有內容都適合公開；成果留在本機。

### Home coding 0.8.0 工程預覽

HomeService 已接現有 Controller → 配對 TLS Runner 的 jobs／auth／artifacts 契約，worker ID 為 `home-coder`。它在真正 HAOS Runner App 內執行；來源為 App 映像內的唯讀 bare repo `/opt/agentic-worker/source` 及固定 commit 記錄，每件工作另建 clone。coding 帳號獨立保存在 App 的 `/data/coding-auth`，不借用 Host、Hermes 或 `home_operator` 登入。既有家庭 Runner 的結構化限制不變。

Owner 可在首次設定完成後明確啟用這個 App。狀態由 `GET /api/host/home-worker/maintenance` 讀取：`not_authorized`、`running`／`waiting`（階段 `repository` → `install` → `start` → `pair` → `login` → `monitor`）、`login_ready`、`ready`、`degraded`、`user_disabled`、`user_removed`、`user_modified`、`needs_reconcile`、`unavailable`。`login.show` 為真時，登入仍走 `POST /api/workers/home-coder/providers/codex/auth`；維護流程自己不呼叫它。`credential_state` 只反映能力輪詢，`configured` 不會被標成 `inference_verified`。停用維護不刪 `/data/coding-auth`，也不解除安裝或取消 `runner_haos` 配對；`POST .../repair` 是 Owner 明確重試，最多重新安裝或啟動一次，不改權限或 options。當機後的明確重啟在 coding lease 或登入 barrier 存在時拒絕。真 HAOS 上 `hassio_api: false` 的 self-options、`release/0.8.0` 商店 slug，以及能力回報 `haos_app`，仍是 `not_run`。

HomeSandbox 的 FUSE ext4 工作磁碟、程序 namespace、App 內 cgroup 資源限制與停止紀錄已在第十二輪真 HAOS App 測試通過。App 只申請 `SYS_ADMIN`、自訂 enforcing AppArmor 與 `/dev/fuse` 裝置規則；沒有 host network／PID、full access 或 Docker socket。模型 worker 必須降為 UID 10000、唯讀 root、無有效 capabilities、有 seccomp／no-new-privileges；私有 staging、管理憑證及父程序 workspace FD 都不能傳入。這些啟用條件有真程序及資源耗盡證據，不能只以啟動參數或單元測試代替。

Home 專屬觀測只讀 App namespace／資源事實，以及固定 `homeassistant` 的 `/api/` 無帳號連通性（80 優先，未取得 HTTP 回應時只再試 8123）；不回傳內容、不追 redirect，不提供 HA／Supervisor bearer 或任意 LAN 掃描。每筆標示 `observed_from=haos_app` 並回到同一 case。獨立 verifier 使用另一個無 relay、無帳號的 clone，執行封裝基準的固定測試。第十二輪真 App 已執行 Codex 工具並產生 patch，另一 clone 的固定 verifier 通過 38 項測試；Controller 經配對 TLS 取得同 case 產物與觀測，逐筆核對來源、內容及 SHA-256。

正常停止需先取消並證明 worker／FUSE 已退出，再回收工作區與 lease。同一 HAOS boot 中 App namespace 改變，不能單憑「App 已重啟」斷言舊 writer 停止；未知結果保持 `stop_unconfirmed` 並阻擋下一個 writer。第十二輪已驗真 TLS 取消、Supervisor 正常停止／重啟，以及突然終止後保留未知工作與 lease、阻擋新 writer 且不重播模型請求。程序 start ticks 與 Docker StartedAt 證明 App 已重新啟動；namespace 編號可重用，不能拿它單獨當停止或重啟證明。

### Home App 的實測範圍

第十二輪完整工程案例 **passed，receipt 324.151 秒**；來源／harness `7b2c4bd29d78f7f2f61e4e52d16569b8b08b20a0`，產品 runtime 與映像沿用 `c19610ae`，封裝研究來源固定為公開 commit `bf831fc8e6082c886b988f4e1cbdfecab5238380`。見 [真 Home App 結果](../evidence/home-worker-haos.json)。15 個整合檢查、30 個 worker 邊界檢查及全部清理檢查通過；實際觸發 ext4 ENOSPC、CPU throttling、PID 上限、記憶體 OOM，取消後回收 worker／FUSE cgroup 與磁碟。App 對 Core 80 的無帳號查詢取得 401，而外層容器無法解析 App 內部名稱；這是額外的 App 視角，不是 Core 全面健康證明。TLS 取消耗時 1.349 秒。

一次性 **HAOS 18.3** 的真正診斷 App 已比較兩種設定。預設 protected App 無法建立新的 mount namespace；增加 `SYS_ADMIN`，並啟用限定 cgroup2 掛載位置的 AppArmor 診斷設定後，可在自己的 cgroup namespace 建立子群組、設定 CPU／PID／記憶體限制，且實際觸發記憶體 OOM kill。兩種設定都沒有 Docker socket；這是 App 內的觀測，不能推論 Core 或整台 HAOS 的狀態。見 [實測資料](../evidence/haos-app-boundary.json)與 [JUnit](../evidence/haos-app-boundary.xml)。

這份早期證據沒有執行 Home coding CLI 或真雲端帳號；新的 HomeSandbox 由上述第十二輪另行驗證。新 harness 使用官方 Supervisor 安裝以真 Runner 映像衍生的測試 App：保留產品 layers、config 與 AppArmor，另加明示的固定模型 transport／診斷 helper，不冒充原封 release 映像。

真 HAOS 18.3／Core 2026.10.1／Supervisor 2026.09.3 的第八輪已通過實際 ext4 ENOSPC、CPU throttling、PID 上限、記憶體 OOM、descendant 取消與 cgroup／工作磁碟回收；另核對 App 身份、enforcing profile、單一 FUSE 裝置規則與配對 TLS。該輪其後因獨立 coding 登入目錄權限失敗，整案仍為 failed，見 [第八輪原始結果](../evidence/home-worker-haos-run8-role-login-failure.json)。

修正登入目錄後，第九輪真 Codex 程序退出碼為 0，產出來源 patch；30 項 worker 邊界檢查全數為 true，包含 App／worker namespace 區分、管理秘密與私有 FD 隔離、唯讀 root、seccomp／no-new-privileges、直接 LAN／公網出站拒絕。工作取得 6 份產物及 3 筆證據，但獨立 verifier 為 **37 passed／1 failed**：固定測試移至另一目錄後找不到基準 `scripts/restore_controller.py`，因此整個工作正確維持 failed，未把 CLI 成功當作驗證成功。見 [第九輪原始結果](../evidence/home-worker-haos-run9-verifier-layout-failure.json)。兩輪 VM／容器與磁碟暫存均已清除。

第十輪來源工作成功，獨立 verifier **38 passed**，30 項 worker 邊界檢查通過；case／觀測來源／內容指紋均相符。完整測試仍於 Core 連通性檢查失敗：原固定 probe 使用 8123，但該 HAOS 的 Core HTTP 設定及實際 listener 為 80；現場唯讀核對為 80 回應 401、8123 拒絕連線。見 [第十輪原始失敗](../evidence/home-worker-haos-run10-observation-failure.json)與 [獨立去密診斷](../evidence/home-worker-haos-run10-observation-diagnostic.json)。原失敗未改寫，資源已清理。

修正採固定 80 → 8123 有界探測，每次保留安全結果，不能接受模型指定主機／埠。第十一輪使用已提交的 0.8.0 候選，來源修改、獨立測試、同 case 內容／指紋、Core 80 回應 401、TLS 取消及正常 App 停止／重啟已通過順序核對；最後因測試誤要求重啟後 PID namespace 編號必須改變而失敗。現場讀回顯示新 App 確實重用 namespace 編號，產品仍保留 `stop_unconfirmed` 與原 lease。見 [第十一輪原始失敗](../evidence/home-worker-haos-run11-restart-observer-failure.json)與 [去密診斷](../evidence/home-worker-haos-run11-restart-observer-diagnostic.json)。資源已清理；該輪尚未跑到阻擋新工作／不重播模型請求的最後斷言。測試改核對程序 start ticks 與 Docker StartedAt，避免舊快取或重用編號誤判；後續第十二輪重新跑完整案例通過，未回改第十一輪失敗。

測試的模型回應是固定 fixture，source task 只是封裝真原始碼的一筆文件註解修改；工程鏈通過仍不是自主修復真問題、真雲端登入或 E2 驗收。T3-13／T3-15 與其他產品案例仍分別保持待驗。

## 重送、取消與接手

工作 envelope 使用 canonical digest。Controller 先保存 handle，再 dispatch；相同 Idempotency-Key／payload 取得原工作，不產生第二 writer。重新啟動或回覆遺失先查 exact job；未能確認停止時維持 `stop_unconfirmed`／`needs_reconcile`，新工作被擋住。

Host 的「核對停止與工作狀態」讀取固定 runner／broker 的容器 ID、boot／process 身份與停止證據。Home 路徑沒有可任意採用的 Host Docker 停止證明。不能只刪 marker 或把狀態改 cancelled。確認停止後可下載接手檢查點及 SHA-256 綁定的 diff，在新的私有 checkout 接手；此版的 `live_session:false` **不是**直接加入同一個 BAT／CLI 可寫 session。

## 畫面預覽

工作產物含 `index.html` 時，可產生 1440／768／390 三個寬度的截圖。固定 renderer 使用獨立、無 credentials、network none 的瀏覽器容器。候選 HTML 不在管理中心 origin 執行。

PNG 經 `127.0.0.2:8100` 的獨立 host origin 與隨機短效連結查看；管理頁只連到 PNG，30 分鐘失效，可提前關閉。遠端 SSH 指令包含這個 listener 的 forwarding，沒有把主機管理埠公開到 LAN。此版交付靜態畫面預覽，尚非互動候選站台。

## 內容期限

Controller 會將保存期限傳給 Hermes 和所有已配對 Runners。縮短期限先生效；離線服務留下同步 barrier，未完成同步不派新工作。各服務在 idle 時也清除內容，延長保存不會復活已刪資料。有界去重 tombstone 保留 ID／digest 與狀態，不保留原 prompt／artifact，也不能重跑舊請求。

## 驗證範圍

`test_worker_controller.py`、`test_worker_execution.py`、`test_runner_lifecycle.py`、`test_worker_ui.py` 測派工、實際 CLI 工具、恢復與介面；`installed_coding_probe.py` 僅可在 installer 的一次性 VM 執行，用真磁碟 quota、真 Codex、獨立 verifier 與固定模型 transport fixture 驗證整條 Host 路徑。

`test_home_worker_haos.py --haos-vm` 是獨立、需序列執行的真 Home App 測試；一般單元測試會略過 VM。它要求實際磁碟／資源耗盡、worker 與 App／HAOS namespace 對照、秘密／出站負向檢查、同 case 產物與證據、取消及正常／非正常重啟。第十二輪完整平台案例已通過；沒有新增 ISO 安裝或真帳號通過聲明。

真實模型帳號、研究品質、Owner laptop、Home App 的自主研究與家庭副作用仍各自記在 [v3 驗收帳本](v3/PROGRESS.md)。工程測試不批次標記 E1／E2 或 32 案通過。
