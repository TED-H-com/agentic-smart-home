# 統一通訊、Hermes 與受控 Agent

外層 Controller/Gateway 維持唯一通訊 receiver。Web 一直可用，onboarding 預設 LINE，亦可選 Discord／Telegram／Web；Hermes 不啟動自己的 bot Gateway，也不取得通道 token。首次開機不建立 HAOS，不影響外層 Web、Hermes 登入或固定主機查詢。

## Hermes 是一般對話的協調助手

新的一般 Web／通道對話預設交給 Hermes。它執行真上游 AIAgent：首次設定使用安裝包的 [受測版本](../hermes/hermes.lock.json)，驗證隨附 digest 與離線相容性，不在家庭主機追 HEAD 或重新建置。它透過私有 adapter 的 session/run/cancel 介面與 Controller 整合，詳見 [runtime 契約](../hermes/README.md)。沒有用自製推理 loop 假稱 Hermes，也沒有把上游 `delegate_task` 當作已完成的 HAOS worker。

自動 onboarding 先登入選定的 CLI，再準備 Hermes。Hermes 提供 **原生 Codex device OAuth** 或 **OpenRouter API key＋模型 ID**；模型預設值來自 runtime。它與 Host／HAOS CLI 各自保存認證，不複製 refresh token。儲存狀態為 configured，引導隨後自動執行一個不操作家庭的驗證提問，只有推論成功才為 ready。

| 顯示狀態 | 意義 |
|---|---|
| 尚待登入（needs_auth） | 尚無有效設定；仍可使用 Web 和固定 console 診斷 |
| 已儲存，待實際回覆驗證（configured） | 金鑰已保存，未藉儲存動作呼叫模型或宣稱登入成功 |
| 已完成推論驗證（ready） | 實際模型呼叫已成功；不代表 worker／家庭／設備驗收完成 |
| 需要重新登入（needs_reauth） | 認證失效，需處理原帳號 |
| 服務尚未連線（unavailable） | 私有 runtime 不可用；與 HAOS 未建立分開呈現 |

瀏覽器只與 Controller 互動；key 表單儲存後清空，不放入 localStorage/sessionStorage、URL、對話 prompt 或一般 evidence。runtime service bearer 不交給瀏覽器。Hermes 私有服務在 loopback 8096，Controller 的獨立 MCP listener 在 8097，兩者都不是使用者或 worker 的通用管理 API。

## 固定工具與 worker 的實際狀態

Hermes 可見工具固定為 `host.status`、`worker.capabilities`、`worker.submit`、`worker.status`、`worker.cancel`、`worker.result`、`case.evidence.read`、`home.observe`、`home.propose`、`home.proposal_status`。MCP 每次呼叫驗證短期 case/run capability；server 決定身份、資料 scope 與工具限制。沒有任意 shell、filesystem、browser、原生 delegate、HA API、Live approve/apply 或模型自簽批准。

本版 `host.status` 提供去密且標註觀測位置的固定主機證據，不能由 libvirt／host 狀態推論 Core、App 或設備正常。`case.evidence.read` 只接受同一 case 的 evidence IDs，不能指定任意路徑／URL。

Host research/development 已接通獨立登入的 Codex、私有 clone、受限 relay、程式差異、固定 verifier 與隔離預覽。能力由實際 Runtime 回報，未登入或環境未就緒不能派工；同時最多一份 coding 工作。Home coding 仍 blocked：真正 HAOS App 的 cgroup 可行性已有證據，完整 launcher 與憑證／出站／磁碟隔離仍待整合。詳見 [Workers](WORKERS.md)。受控 Runner 或工程 fixture 通過均不代表 E1/E2 家庭驗收完成。

## 對話、case 與重試

「智能管家」直接呈現對話框，預設交給 Hermes。帳號與通訊入口移至「設定」，模型／CLI 選擇收在對話下方的「進階」。對話方式選「直接交給開發助手」才顯示執行位置與 AI；切換進階 Grok／HAOS 後回 Hermes，仍回到自己的 Hermes 對話 scope，不讓隱藏的 CLI 選擇改變 Hermes case。Web 可按「開始新對話」，或從「對話紀錄」選擇可續接的 Web thread；無 thread 的舊紀錄、外部通道和已結束紀錄只能查看。送出時停用歷史切換；閱讀外部紀錄不清除原 Web 續接位置。sessionStorage 只保存各 scope 的訊息 IDs。

同 case 的工作進度與成果在原對話展開，讀取成果不派新工作。成果必須符合 exact job 與 case；checkpoint 和觀測位置保留在技術資料，檔案仍驗 SHA，候選 HTML 只產生隔離 PNG。

外部通道依設定的協調方式處理；`/new 新需求` 開始新對話。`/host 需求`／`/home 需求` 明確選擇既有受控 Runner；可組合 `/home /new 新需求`。不同通道、聊天室、sender 及設定保持隔離，不會自動跨通道共用 Hermes session。更換 bot token／白名單、Owner／政策、Hermes 模型或認證會使不再授權的舊 context／能力失效。明確主機登出也切斷 Web Hermes scope。

Controller 保存 conversation→case→Hermes session/run mapping；提交取得 handle，頁面離開不必維持原 HTTP 請求。相同 key/digest 的重試指向同一工作，換 payload 則拒絕。Controller 和 Hermes 重啟都會查核／標示中斷，不直接重播未完成推論或家庭副作用。短期 capability 可因取消、暫停、登出或 scope 變更撤銷。

Hermes runtime 一次一個 run，5 分鐘 deadline、最多 12 次模型 iterations；history 與回覆有大小／期限上限，詳細見 runtime 契約。這套有限 session history 與原受控 Runner 的前文編譯分開。輪詢狀態不會呼叫模型；無可信供應商用量時不捏造費用。

保存期限 `context_days` 可設 1–7 天。Controller 同步 Hermes 與所有已配對 Runner／coding broker；同步失敗回 503，保留 pending barrier，完成前不派新工作。較短期限先套用，不能因某個服務離線就繼續讀取應過期內容。各 runtime 在 idle 時也清除，停止未確認時保留隔離；調長不復活已刪內容。一般證據／內容、去重 tombstones 與稽核備份的期限分開，詳見 [Workers](WORKERS.md) 與 [Updates](UPDATES.md)。

聊天室及 sender 必須同時在白名單，bot／自身訊息忽略。來源事件持久化去重，每位來源使用者每分鐘最多 10 條，Gateway 最多 10 個處理中工作；runtime 自己的並行限制另行生效。回覆傳送不確定時保留 `unconfirmed`，不盲目重送；更換收件範圍後，舊工作不發送到新對象。

## 進階受控 Runner 與家庭操作

既有 `home_operator` 限制保留，沒有為新增 coding 全域打開工具。Controller 分別保存 Host／HAOS Runner 的 TLS 指紋與配對 bearer，provider 認證留在各自的私有 profile。內層是實際 HAOS Runner App，不是在 HAOS 唯讀根目錄安裝 npm，也不是 Home Coding Worker 驗收的替代品。

內層位址固定為產品 VM 的 `192.168.177.2:8443`。完成首次設定後，Owner 可明確啟動家庭現場助手維護：沿用側欄同一條發行 repository，安裝商店裡的 `agentic_runner`，保留 8443，並以 App options 中的一次性指紋完成配對。配對碼不進入維護紀錄或瀏覽器回應。商店沒有此 App 時不改裝本機 ZIP。App 記錄仍印出配對 JSON，供手動恢復。`hassio_api: false` 下的 self-options 尚未在真 HAOS VM 驗證。詳見 [Onboarding](ONBOARDING.md) 與 [Workers](WORKERS.md)。

Codex 支援 device code／API key；Claude 支援 API key／setup token；Grok 支援官方 device code／xAI API key；Antigravity 目前只整合 Gemini API key。CLI 的 API-key 登入檢查會執行一次簡短官方推論、可能使用額度；Hermes 引導亦在保存 configured 後自動提交驗證提問。

受控模式的前文最多六回合、8 KiB，每則需求與答覆最多 2 KiB；逾限明示省略，保留期 1–7 天。HAOS 問答重新取得授權觀測，不把舊記憶當即時狀態。補充需求會建立後續家庭任務並連回先前等待補充的任務；等待批准的提案仍是獨立的確切資源。

Hermes 的 `home.observe`／`home.propose` 和受控 HAOS 問答都接到原家庭 Engine，保留 policy、live Owner、具體提案、Web 批准、備份、journal 及讀回核對。對話中的「查看提案」開啟確切提案。Hermes 沒有把研究結果直接部署的工具；通道訊息與模型 `approved=true` 均不能代替批准。已部署的日常規則由 HA 執行，停止 Hermes／Runner 不會停用規則。

## LINE.js

依 [固定上游 commit](https://github.com/JS-PACKAGE/LINE.js/tree/9670589e4df3096a06c812dc4c1298140c736172) 建置，來源 SHA-256 記於 `channels.lock.json`。管理連線使用首頁 HttpOnly cookie 與同源 `/ws`；掃碼 QR／PIN 只留在記憶體。啟用流程先寫入聊天室設定，等 sidecar 重新載入後才建立一次性 bot bearer；token 存於私有 Controller DB。外部 bot socket `/api/ws` 不帶 Origin，只帶 bearer，只收即時訊息。

原始 LINE.js 容許任意 Host。本產品的 `host/linejs/patch-local-host.mjs` 在建置時限制 HTTP、admin socket、bot socket 的 Host 為 `127.0.0.1:3789`，防止 DNS rebinding 繞過入口。上游的 cookie／Origin／bearer 驗證仍保留。設定、LINE session 與訊息快取位於 `/var/lib/agentic-home/linejs`，0700 目錄，不納入 source archive。Controller 只交換 typed 白名單設定，不接受任意程式參數。

LINE.js 是非官方 LINE 客戶端；帳號登入與平台相容性需現場驗證。可先用次要帳號。引導先讓使用者選擇真實聊天室，再從目標帳號傳送短效配對碼；Controller 綁定訊息中實際的聊天室 ID 與 sender MID，不必手動複製 IDs。配對期間未授權的訊息不送入模型。手動 ID 白名單仍在進階設定。

## Discord 與 Telegram

Discord 使用專用 bot token，開啟 Message Content Intent，對指定頻道授予 View Channel、Read Message History、Send Messages。以 REST 每 3 秒查詢指定頻道；忽略 bot 與 webhook，回覆停用 mentions。第一次只設定游標，不處理舊訊息。

Telegram 使用 BotFather 建立的專用 bot；輸入 token 後，在預定對話傳送 `/pair 畫面的配對碼`，自動綁定實際 chat／sender。取消或過期可以重新產生配對碼，已保存 token 不需重貼。long polling 讀取 message updates；若 bot 已有 webhook 則拒絕接管。群組的 privacy mode 會影響 bot 可收到的內容。初次略過舊 updates，游標以 token hash 分開持久化；回覆停用 link preview。更換 token 不繼承另一個 bot 的游標。

HTTP 通道禁止重新導向、限制回覆大小；錯誤不回傳含 bot token 的原始 URL。所有外部通道皆由主機向外連接，不需公開 Controller HTTP port。

## 上游調研與範圍

讀取 [JS-PACKAGE repositories](https://github.com/orgs/JS-PACKAGE/repositories) 後，這次實際整合 LINE.js。Gauge.js 的帳號額度查詢可另外整合；WebChatMCP.js 是瀏覽器聊天連線；Beacon.js、Stage.js 等各有遊戲或語音用途，本次不加入家庭 gateway 的執行依賴。

協定與 CLI 依據：[Antigravity 安裝](https://antigravity.google/docs/cli/install/)、[headless](https://antigravity.google/docs/cli/headless/)、[permissions](https://antigravity.google/docs/permissions?tab=cli)、[Grok CLI reference](https://docs.x.ai/build/cli/reference)、[headless](https://docs.x.ai/build/cli/headless-scripting)、[Discord messages](https://docs.discord.com/developers/resources/message)、[Telegram Bot API](https://core.telegram.org/bots/api)。

`evidence/runner-container.json` 區分官方 binary 執行與本機推理 fixture；`evidence/linejs-container.json` 僅驗證沒有網路／帳號的本機 HTTP／WebSocket 邊界。真實四家雲端帳號與外部訊息收發尚未執行，不能從 fixture 通過推定。
