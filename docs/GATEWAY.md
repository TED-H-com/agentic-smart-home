# 統一通訊與兩層 Agent · 0.3.1

外層 Linux 的 Controller 是唯一通訊入口。Web 永遠可用，外部通道由使用者在 onboarding 啟用；預設呈現 LINE，也可選 Discord、Telegram 或只使用 Web。HAOS 停機不會阻止外層登入或對話。

## 執行與登入

Controller 分別保存外層與內層 Runner 的 TLS 指紋、配對 bearer；provider 認證留在各自 Runner 的私有 profile。每個環境都有 Codex、Claude Code、Antigravity 與 Grok Build，不共用登入檔案。HAOS Runner 是該 OS 的 App 容器，透過 Supervisor 管理；不是在 HAOS 唯讀主機根目錄安裝 npm。

內層位址固定為受管理 VM 的 `192.168.177.2:8443`，使用者不能將登入 API 改成任意遠端 URL。HA App 商店加入 `https://github.com/teddashh/agentic-smart-home`，安裝 Runner、保留 8443 映射，再將一次性配對 JSON 貼到外層 Web。Runner ZIP 可作為本機 App 來源替代方案。

Codex 支援 device code／API key；Claude 支援 API key／setup token；Grok 支援官方 device code／xAI API key；Antigravity 目前只整合 Gemini API key，尚未整合 Google 訂閱登入。API key 直接送到指定 Runner，不進 Controller 對話紀錄或模型 prompt。API key 登入檢查會執行一次簡短的官方 CLI 推理，可能使用帳號額度。

## 訊息與家庭操作

Web 明確選擇 runtime 與 provider。外部通道依預設設定處理，`/host `、`/home ` 前綴可分別選擇外層或內層。Web 送出後會接續目前對話，可按「開始新對話」清除前文選擇，也可從舊訊息選「接續這段對話」。切換環境或 provider 時各自保留選擇；瀏覽器 sessionStorage 僅保存訊息 ID，不保存需求或憑證。網路回覆遺失時，同一份尚未成功確認的表單重試會沿用 Idempotency-Key。

外部訊息自動接續同一通道、聊天室、使用者、runtime、provider 的最近對話。`/new 新需求` 開始新對話；`/home /new 新需求` 則同時指定 HAOS。變更 bot token、白名單、HA owner 或設備政策後不繼承原上下文。連續送出的訊息會等待前一則完成；中斷或已過期的前文不會重播。

Controller 最多提供六回合、8 KiB 的前文，每則需求與答覆各最多 2 KiB；超限或已清理時明確標記省略，不把舊觀測當作即時狀態。每次 HAOS 問答重新讀取已授權設備。前文依設定保留 1–7 天，查詢也立即套用期限，不依賴定期清理。補充家庭需求會建立後續任務，原本等待補充的任務標示已接續並記錄後續任務 ID；等待批准的提案保持獨立。

外層只接受文字答覆與有限主機觀測。內層需求進入既有 Engine，保留設備政策、HA owner 即時驗證、具體提案、Web 批准、備份、讀回及日誌。LINE／Discord／Telegram 的訊息不能充當批准。已啟用的日常自動化由 Home Assistant 執行，不依賴 Agent 常駐推理。

聊天室與使用者白名單必須同時符合；bot／自身訊息忽略。外部通道以來源＋聊天室＋使用者＋事件 ID＋通道設定持久化去重，重複送出不重跑。每位來源使用者每分鐘最多 10 條，總共最多 10 個處理中工作。Controller 重啟後標示中斷，不重播未完成需求；回覆傳送不確定時記為 `unconfirmed`，不盲目重送。變更聊天室或 token 後，舊工作不會將內容送給新設定的對象。

## LINE.js

依 [固定上游 commit](https://github.com/JS-PACKAGE/LINE.js/tree/9670589e4df3096a06c812dc4c1298140c736172) 建置，來源 SHA-256 記於 `channels.lock.json`。管理連線使用首頁 HttpOnly cookie 與同源 `/ws`；掃碼 QR／PIN 只留在記憶體。啟用流程先寫入聊天室設定，等 sidecar 重新載入後才建立一次性 bot bearer；token 存於私有 Controller DB。外部 bot socket `/api/ws` 不帶 Origin，只帶 bearer，只收即時訊息。

原始 LINE.js 容許任意 Host。本產品的 `host/linejs/patch-local-host.mjs` 在建置時限制 HTTP、admin socket、bot socket 的 Host 為 `127.0.0.1:3789`，防止 DNS rebinding 繞過入口。上游的 cookie／Origin／bearer 驗證仍保留。設定、LINE session 與訊息快取位於 `/var/lib/agentic-home/linejs`，0700 目錄，不納入 source archive。Controller 只交換 typed 白名單設定，不接受任意程式參數。

LINE.js 是非官方 LINE 客戶端；帳號登入與平台相容性需現場驗證。可先用次要帳號。通道啟用需要聊天室 ID 以及允許使用者的 LINE MID；從 LINE.js 本機聊天室／成員資料取得，不能以顯示名稱代替。

## Discord 與 Telegram

Discord 使用專用 bot token，開啟 Message Content Intent，對指定頻道授予 View Channel、Read Message History、Send Messages。以 REST 每 3 秒查詢指定頻道；忽略 bot 與 webhook，回覆停用 mentions。第一次只設定游標，不處理舊訊息。

Telegram 使用 BotFather 建立的專用 bot，long polling 讀取 message updates；若 bot 已有 webhook 則拒絕接管。群組的 privacy mode 會影響 bot 可收到的內容。初次略過舊 updates，游標以 token hash 分開持久化；回覆停用 link preview。更換 token 不繼承另一個 bot 的游標。

HTTP 通道禁止重新導向、限制回覆大小；錯誤不回傳含 bot token 的原始 URL。所有外部通道皆由主機向外連接，不需公開 Controller HTTP port。

## 上游調研與範圍

讀取 [JS-PACKAGE repositories](https://github.com/orgs/JS-PACKAGE/repositories) 後，這次實際整合 LINE.js。Gauge.js 的帳號額度查詢可另外整合；WebChatMCP.js 是瀏覽器聊天連線；Beacon.js、Stage.js 等各有遊戲或語音用途，本次不加入家庭 gateway 的執行依賴。

協定與 CLI 依據：[Antigravity 安裝](https://antigravity.google/docs/cli/install/)、[headless](https://antigravity.google/docs/cli/headless/)、[permissions](https://antigravity.google/docs/permissions?tab=cli)、[Grok CLI reference](https://docs.x.ai/build/cli/reference)、[headless](https://docs.x.ai/build/cli/headless-scripting)、[Discord messages](https://docs.discord.com/developers/resources/message)、[Telegram Bot API](https://core.telegram.org/bots/api)。

`evidence/runner-container.json` 區分官方 binary 執行與本機推理 fixture；`evidence/linejs-container.json` 僅驗證沒有網路／帳號的本機 HTTP／WebSocket 邊界。真實四家雲端帳號與外部訊息收發尚未執行，不能從 fixture 通過推定。
