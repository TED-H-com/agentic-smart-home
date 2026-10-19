# Coding workers、成果與接手

0.6.0 把 Host coding 接到現有 Controller → TLS Runner → 固定 Host broker。`home_operator` 仍使用原本受限的結構化命令；新增的 `research`／`development` 使用另一份登入與真正 tools-enabled Codex。Hermes 依每個 worker 的 coding 設定派工，不繼承 onboarding 或 Gateway 所選的家庭 CLI。

## 使用

日常在「智能管家」描述需求，由 Hermes 依能力派工，成果回到同一段對話。第一次 coding 登入或手動派工可從「設定 → 開發助手」選主機或家庭現場，完成獨立 Codex device code 或 API key 登入。Host 工作者只讀封裝內的產品來源，在私有 clone 調查、修改和跑測試。CLI 登入資料不複製到 Hermes 或模型的工作區。尚未登入時不送出工作；`configured` 表示資料保存，成功模型工作才記為 `inference_verified`。

目前 coding adapter 的 provider 為 **Codex 0.157.1**；Claude、Antigravity、Grok 的家庭模式／初始化能力繼續保留，但不冒充已完成的 coding adapter。**0.9.0 交付 Host coding 與家庭現場的 Home coding**，兩者都以 Codex 為首個 provider，家庭現場可從網頁一鍵啟用。工程測試的模型 transport 是固定 fixture；真雲端自主研究、Owner 家庭試跑與 E2 仍待 Owner 實測。各版本的媒體與平台證據見 [驗證紀錄](VERIFICATION.md)。

Web 會保留調查、差異、檔案指紋與實際程序觀測。worker 自己的結論仍是不可信內容。修改成功後，另一份無帳號、無網路的乾淨 clone 套用同一 patch，執行封裝基準提供的固定測試；原 worker 不能修改 verifier 的測試／設定。驗證器先載入封裝內的 pytest，再加入候選程式的 import 路徑；除了退出碼，還要求測試有收集、有執行且完整結束，避免尚未測試就退出的假成功。候選 Python 仍在白箱測試程序內執行，因此這是有限的測試完成紀錄，不是抵抗任意惡意候選的密碼學證明。這個有限測試集合不代表任意修改已通過完整 CI，也不形成部署批准。

## 執行邊界

- Host 安裝包含只針對本 public repo `HEAD` 的 Git bundle，以及 commit／bundle SHA-256；建置拒絕未提交的產品來源。installer 驗證後放到 root-owned `/var/lib/agentic-home/coding/source` bare repo。每個 worker 再建獨立 clone，不共用可写 Git metadata。簽署的 Controller 更新成功後，這個 bare repo 才前進到該 image 的 `source_commit`；退版把它和 image 一起退回。`source.json` 的 `commit` 仍是 worker 研究的 HEAD，`install_commit` 記住安裝時的 commit。再次執行安裝不會用安裝 bundle 覆蓋已移動且 `install_commit` 仍相符的 repo。提交時 `base_commit` 為 null，Host 綁定當時的 HEAD；明確傳入過期 SHA 仍拒絕為 `coding_source_revision_mismatch`。`GET /updates` 的 `deployed_source_commit` 與每筆 `source_sync` 給畫面顯示目前研究的來源。
- 固定 Host broker 持有 Docker 與 ext4 volume。Runner 只有固定 Unix broker 接點；模型程序無 Docker socket、Controller／HA／Supervisor bearer、Owner cookies 或 provider bearer。
- Host worker UID 10000、network none、read-only root、capabilities 限制、cgroups CPU／memory／PID 配额與固定 watchdog。所有可寫工作目錄在有界 ext4 磁碟；不能以輪詢資料夾大小代替 quota。
- provider／公開文件出站經受控 Unix relay。上游 HTTPS 主機、IP、方法與路徑受限；拒私網 DNS 與任意 redirect／遠端 MCP，provider bearer 在 broker 加入。模型與公開來源的請求數、內容大小與期限受限。
- 中繼把 provider 回應逐段轉給 sandbox（chunked，每段 flush）。位元組之間的閒置上限是 300 秒；工作 deadline 仍限制總時間。回應累計超過 8 MiB 就中止，不先緩衝整份本文。ChatGPT OAuth 依固定版 Codex 的規則向固定的 `https://auth.openai.com/oauth/token` 更新：JWT 有到期時間時，只在 5 分鐘內到期才更新；沒有時，`last_refresh` 早於 8 天才更新。provider 在尚未送出任何回應位元組前回 401 時，只更新並重試該請求一次。更新端點回 401、`invalid_grant` 或 `refresh_token_expired`／`refresh_token_reused`／`refresh_token_invalidated`，或第二次 401／403，才記 `auth_failed` 並以 `needs_reauth` 回報。連線失敗、逾時或 5xx 是暫時性的，不要求重新登入。refresh token 與 id token 只留在憑證檔。API key 不呼叫該端點。中繼與 sandbox proxy 只雙向轉送固定的 Codex Responses 標頭（lite、originator、turn-state、beta、session／thread／window id、turn metadata；routing hint 只在 ChatGPT 路徑），其餘含憑證、attestation 與 Set-Cookie 一律丟棄。這是程式行為，不是真帳號驗收。
- 同時一個 coding job。準備（clone 與映像掛載）與驗證各有 300 秒，執行有 840 秒，用 `time.monotonic()` 計時。開發容器自己的上限是 900 秒，比 broker 的期限晚 60 秒，所以逾時一定由 broker 收尾並保留成果。執行預算從容器實際跑起來才開始，不從送出開始。Owner 授權的 `expires` 仍是原本的授權截止，不會被預算拉長；逾時原因分別是 `setup_timeout`、`execution_timeout`、`verification_timeout`、`authorization_expired`。Owner 按取消是 `owner_cancelled`。取消不會清掉已寫下的 `error_code`。writer 和 verifier 都必須證明停止，才釋出全域 lease。關頁或 HTTP 回覆遺失不表示取消。
- Broker 收到 SIGTERM／SIGINT 後，沿原本的 fenced stop 停掉 sandbox，在有限時間內 `collect()` 再結束。下次啟動先從 workspace 收集上次還在跑的 job，標成 `interrupted`（`broker_restarted`），保留 diff／findings／artifacts，不重跑。還沒進 sandbox 的排隊 job 直接標 `interrupted`（`broker_restarted`）。結果還沒 collect 的執行 workspace 不會被 prune 刪掉。
- `stop_unconfirmed` 的工作在每次清理時重試停止（每分鐘一次，以及每個寫入請求之前）；Owner 按停止或「確認已停止」也會重試。確認停止後只會成為 `cancelled`（`owner_cancelled`）、`interrupted`（`broker_restarted`）或 `failed`（沒有其他原因時記 `writer_stop_unconfirmed`），絕不升級為成功，也不跑驗證；驗證被中斷時記 `not_run`。重試失敗只略過那一筆，下次再試，不影響其他工作與請求；Owner 明確停止時仍回報失敗。Controller 的核對路由對 `stop_unconfirmed` 的工作送出取消。在確認之前，新工作仍被擋住。
- 失敗且沒有其他原因的工作，依序記錄 relay 觀察到的第一個原因：`coding_quota_reached`（最後一次模型回應為 429）、`coding_turn_limit`（超過每份工作 200 次模型呼叫）、`coding_provider_rejected`（其他 4xx，401／403 除外）、`coding_provider_unavailable`（5xx）。401／403 照舊走 `coding_needs_reauth`。成功的工作不記錄這些原因。
- 狀態讀取不跟 launch、git、collect 共用鎖。`phase` 含 `queued`、`launching`、`running`、`verifying`、`collecting`，並帶 `heartbeat_at`。Controller 在 launching／running／verifying／collecting 且 heartbeat 仍新時不會標 `needs_reconcile`；job 真的消失、回執不合或 heartbeat 過期才會。
- 完成、逾時或中斷的 job 都留結果。沒有自動重試。「再試一次」是新的一次送出：Owner 用新的 `Idempotency-Key` 呼叫 `POST /api/worker/jobs`；Hermes 用新的 `idempotency_key` 呼叫 MCP `worker.submit`。同一個 key 只會取回舊 job。
- 成果在確認停止後匯出，拒 symlink／hardlink／特殊檔案、越界、超量、非允許類型及可辨識的憑證格式。不是 UTF-8 或含 NUL 的產出檔標成 `not_text` 略過，其他檔案照常匯出。修改本身不是文字時記 `patch_not_text`，不保留修改也不跑驗證；`changes.patch` 是驗證端收到的同一份位元組。驗證端回報的 `tests_executed`（0 到 1,000,000 的整數）只用來顯示，不影響通過與否。去密模式不能證明所有內容都適合公開；成果留在本機。

### Home coding（家庭現場）

HomeService 已接現有 Controller → 配對 TLS Runner 的 jobs／auth／artifacts 契約，worker ID 為 `home-coder`。它在真正 HAOS Runner App 內執行；來源為 App 映像內的唯讀 bare repo `/opt/agentic-worker/source` 及固定 commit 記錄，每件工作另建 clone。coding 帳號獨立保存在 App 的 `/data/coding-auth`，不借用 Host、Hermes 或 `home_operator` 登入。既有家庭 Runner 的結構化限制不變。

Owner 可在首次設定完成後明確啟用這個 App。狀態由 `GET /api/host/home-worker/maintenance` 讀取：`not_authorized`、`running`／`waiting`（階段 `repository` → `install` → `start` → `pair` → `login` → `monitor`）、`login_ready`、`ready`、`degraded`、`user_disabled`、`user_removed`、`user_modified`、`needs_reconcile`、`unavailable`。`login.show` 為真時，登入仍走 `POST /api/workers/home-coder/providers/codex/auth`；維護流程自己不呼叫它。`credential_state` 只反映能力輪詢，`configured` 不會被標成 `inference_verified`。停用維護不刪 `/data/coding-auth`，也不解除安裝或取消 `runner_haos` 配對；`POST .../repair` 是 Owner 明確重試，最多重新安裝或啟動一次，不改權限或 options。當機後的明確重啟在 coding lease 或登入 barrier 存在時拒絕。同一 `#stable` 渠道、較舊的已安裝版本會成為 `update_available`（`status.update`），已安裝的 App 繼續跑；在 Owner 呼叫 `POST /api/host/home-worker/maintenance/update` 之前，不重啟、不修復、不配對、不登入，也只讀 App 版本與狀態。更新只裝到與 Controller `VERSION` 相同的版本；商店較新是 `home_worker_release_ahead`，較舊會有界重試重新載入商店（最多三次），仍較舊則 `waiting`／`home_worker_release_not_published`。舊的 `#release/X.Y.Z` 紀錄是另一個 App slug，狀態 `needs_reconcile`／`home_worker_release_channel_changed`，不會自動解除安裝或加入新 repository；要在 HA 移除舊 App 後，再從管理中心重新啟用。`user_disabled`、`user_removed`、`user_modified` 不會被主機更新改寫。解除安裝會刪除 `/data`，包括 `/data/coding-auth`。公開來源 `53c9e79` 的真 HAOS 工程測試已完成 `hassio_api: false` App 的一鍵啟用與自動配對，並經配對 TLS 讀到 `home-coder`／`runtime_location=haos_app`；self-options 可用性由固定啟用流程成功推得，沒有另存原始 HTTP 回應。這不是雲端登入或 coding 工作；從 0.9.0 到下一版的真實 Supervisor 更新仍為 `not_run`。

公開來源 `53c9e79` 的 `#stable` 商店 slug 與一鍵啟用已在真 HAOS 的 [第二輪公開交付](reviews/2026-10-16-PUBLIC-DELIVERY-53c9e79.md) 通過，包含兩個 App 共用 repository、cold 備份與整台重開。測試預載受測 base 映像，Host 能力／模型回應仍是 fixture，未登入真雲端或執行 coding；不能代替上方尚未驗證的項目。

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

Host 的「確認已停止」讀取固定 runner／broker 的容器 ID、boot／process 身份與停止證據。Home 路徑沒有可任意採用的 Host Docker 停止證明。不能只刪 marker 或把狀態改 cancelled。確認停止後可下載接手檢查點及 SHA-256 綁定的 diff，在新的私有 checkout 接手；此版的 `live_session:false` **不是**直接加入同一個 BAT／CLI 可寫 session。

未發布的後續修正允許接手檢查點列出最多 32 件已接受的成果，與工作成果上限一致。重複、畸形或其他工作的成果 ID 均拒絕；請求途中更換配對 worker，或內容保存期限到期，也不能回傳接手成功。

成果下載也會在取得回應後再次核對原工作的配對 worker 身分；途中重新配對或取消配對會拒絕交付，另一個 worker 的配對變動不影響下載。檔案仍須符合原成果的工作 ID、大小與 SHA-256，回傳前也會重查保存期限。

## 畫面預覽

工作產物含 `index.html` 時，可產生 1440／768／390 三個寬度的截圖。固定 renderer 使用獨立、無 credentials、network none 的瀏覽器容器。候選 HTML 不在管理中心 origin 執行。

PNG 經 `127.0.0.2:8100` 的獨立 host origin 與隨機短效連結查看；管理頁只連到 PNG，30 分鐘失效，可提前關閉。遠端 SSH 指令包含這個 listener 的 forwarding，沒有把主機管理埠公開到 LAN。

Host 獨立驗證器在基準測試通過、且候選差異碰到 `web/` 時，用映像內釘選的工具鏈離線建置候選介面。建置失敗只記入 `verification.web_preview`，不改變測試結果，也不新增工作階段。Home 驗證器不建置（`unavailable`／`host_only`）。依賴鎖與 `web/package.json` 必須與映像內複本逐位元組相同。

擁有者在 Host 管理頁可開互動預覽：同一個 `127.0.0.2:8100` 的 `/i/<capability>/`，只提供虛構示範資料，30 分鐘失效，可提前關閉，最多同時保留 8 筆（含截圖）。候選 JavaScript 不在 `localhost:8099`、`127.0.0.1:8099` 或 HA origin 執行，也不讀真實家庭資料。Bridge 與 Ingress 拒絕互動預覽，也不轉送互動網址。每個回應帶嚴格 CSP，並拒絕帶 `Service-Worker` 標頭的請求。示範資料不是驗收。

## 內容期限

Controller 會將保存期限傳給 Hermes 和所有已配對 Runners。縮短期限先生效；離線服務留下同步 barrier，未完成同步不派新工作。各服務在 idle 時也清除內容，延長保存不會復活已刪資料。有界去重 tombstone 保留 ID／digest 與狀態，不保留原 prompt／artifact，也不能重跑舊請求。

## 完成通知（未發布增量）

已確認停止的工作，可向原 LINE／Telegram／Discord 對話發送固定狀態與 Web 工作路徑；授權、對話、保存期限或原 bot 回覆不符合時不送出。通知不包含成果本文，不建立新的 Hermes 回合。送達不明不自動重送，Web 手動工作也不猜測收件人。

通知路徑只查指定工作，不因它缺席於最近工作清單而改顯示其他成果。切換通知或瀏覽器上一頁會同步選取；找不到、期限已過或暫時讀取失敗各自提示，不會重新派工。

## 驗證範圍

`test_worker_controller.py`、`test_worker_execution.py`、`test_runner_lifecycle.py`、`test_worker_ui.py` 測派工、實際 CLI 工具、恢復與介面；`installed_coding_probe.py` 僅可在 installer 的一次性 VM 執行，用真磁碟 quota、真 Codex、獨立 verifier 與固定模型 transport fixture 驗證整條 Host 路徑。

`test_home_worker_haos.py --haos-vm` 是獨立、需序列執行的真 Home App 測試；一般單元測試會略過 VM。它要求實際磁碟／資源耗盡、worker 與 App／HAOS namespace 對照、秘密／出站負向檢查、同 case 產物與證據、取消及正常／非正常重啟。第十二輪完整平台案例已通過；沒有新增 ISO 安裝或真帳號通過聲明。

真實模型帳號、研究品質、Owner laptop、Home App 的自主研究與家庭副作用仍各自記在 [v3 驗收帳本](v3/PROGRESS.md)。工程測試不批次標記 E1／E2 或 32 案通過。
