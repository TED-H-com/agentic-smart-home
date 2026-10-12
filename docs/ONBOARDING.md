# 自動 onboarding：CLI 先行

依 2026-10-12 Owner 最新指示，首次使用從選擇熟悉的 coding CLI 開始。預設區域網路自動取得位址；固定 IP／Tailscale 的選擇保留稍後處理，不是登入助手的前提。

```text
USB 開機 → Linux 安裝／連網 → Web
  → 選一個 CLI → 官方登入
  → CLI 檢查設定需求 → 程式準備最新版 Hermes
  → Hermes 帳號驗證 → LINE／Telegram／Discord／Web
  → 自動建立或啟動 HAOS → HA 原生 onboarding → 綁定家庭 Owner
```

使用者選擇一次 CLI 和主要通道、啟動設定後，Controller 的持久工作會自動執行已授權步驟。關閉 Web 不取消工作；重新開啟讀取原進度。讀取進度本身不建立工作。帳號授權、LINE 掃碼、建立 Telegram bot、家庭密碼和設備權限仍需真正的 Owner 操作；無法登入或服務中斷時顯示實際等待原因。

## 安裝與網路

公共 ISO 預設啟動安裝選項，Ubuntu 安裝器處理網路、磁碟及 Linux 身份；選定目標後自動完成套件、桌面、產品服務與外層配對。公共媒體不預選要清除的磁碟、不內嵌通用密碼。Linux 桌面登入後自動開啟 Web，使用本機 Owner 的 120 秒一次性票據登入；Controller 的私有票據檔只保存 hash；家目錄0700目錄中的0600暫時 HTML 傳遞票據並在120秒後清除，程序參數不含 bearer，浏览器立即清除 fragment，票據不進 storage。其他 Linux 帳號或遠端瀏覽器仍使用明確的金鑰登入。初次 firstboot 只啟動外層；HAOS 由後續 onboarding 自動啟動，故內層未準備好不阻擋外層登入。

主機的 network.ready 是外層預設路由觀測，並不宣稱雲端一定可用。真正下載、OAuth 和推論各自驗證對外連線。固定 IP、Tailscale 或公開網路位址尚不自動設定；現有 Web 維持 loopback／SSH tunnel 的可用路徑，不自動把管理介面曝露到網際網路。

## Agent 與程式的分工

選定的 CLI 實際執行固定 bootstrap prompt，只取得必要且無秘密的觀測，產出經 schema 驗證的建議。它不能提供任意 URL、shell、套件名或磁碟路徑給 root broker。安裝、設定與狀態核對由固定程式執行；CLI 回覆「成功」不算完成證據。這個受限設定助手不是 v3 的通用 coding worker，不解除既有 home_operator 限制。

Hermes 的 OAuth 與 CLI 登入由各自的受信任接點管理，不複製可輪換 refresh token、不共用可寫 HOME。Codex 可使用官方 device-code 流程；其他 CLI 依實際 adapter 支援呈現，未支援的訂閱登入不冒充可用。Hermes 仍須真實推論成功才標示 ready，僅保存憑證為 configured。相關官方文件：[Codex 登入](https://learn.chatgpt.com/docs/auth)、[Hermes 認證池](https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools)。

## 首次準備最新版 Hermes

固定 `bootstrap_hermes` 操作只用於從未配置或使用的 Hermes 安裝：

1. 從 `NousResearch/hermes-agent` 官方預設分支解析當時最新 commit，下載對應 archive，記錄 SHA-256；拒絕跨來源重新導向、異常路徑及過量內容。
2. 在獨立建置目錄使用產品自己的 Dockerfile、adapter 與 hash-locked 相依。最新來源若不相容則失敗，不自動放寬介面或任意改相依。
3. 在無網路、無憑證、唯讀 root、限資源的容器執行相容性 probe。
4. 再次檢查未登入／未使用，停止舊 Hermes，按 image digest 啟動候選並等 healthcheck。成功保存精確 revision；啟動失敗恢復安裝包附帶的 image digest。
5. 程序中斷或退回未確認會保留 `needs_reconcile`，不盲目重播。已經登入／有 session 的 Hermes 不走這條首次設定更新路徑。

安裝時固定的 digest 會跨重新開機保存；正常啟動不追逐 upstream HEAD。這不是正式家庭產品更新／資料格式退版器。產品更新與家庭變更仍走獨立受控流程。上游 Docker 安裝也由其封裝管理者負責更新，見 [Hermes 安裝說明](https://hermes-agent.nousresearch.com/docs/getting-started/installation)。

## 通訊與家庭

LINE 為預設選项；Web 一直可用，選 Web 是明確略過外部通道。沿用唯一 Gateway，不同時啟動另一個 Hermes bot receiver 消費相同 token。LINE 掃碼後選定聊天室，再傳送配對碼；Telegram 建立 BotFather bot、貼 token 並傳送 `/pair CODE`。配對碼短效、可取消且只綁定真實 sender／chat；配對之前的訊息不交給模型。Discord 保留原本明確頻道／使用者設定。通道必須實際 connected 且建立允許的聊天室／使用者範圍才繼續；帳號保存或 QR 畫面不能當成已連線。

完成外層與所選通道後，固定 Host broker 自動 provision／start 產品管理的 HAOS。只核對固定內部 Core `/api/onboarding`，逐項回報真實步驟；VM running 不等於 Core 就緒，API 斷線或 401 也不等於 onboarding 完成。原生 HA 頁面由 Owner 設定家庭與帳號，之後透過 HA 官方瀏覽器授權返回 Controller，驗證 active owner 後建立專用長效授權，保存成功再撤銷臨時 OAuth refresh grant。code／token 不回傳 Web 或模型；state 綁本機 session、origin、時限與授權世代，重播／舊流程回呼拒絕。手動 token 僅保留進階備援。已完成設定的 Core 重啟後可能移除 onboarding 路由；404 必須再有獨立的有效 owner 與 `/config` 證據才能標示完成。模型不讀取家庭密碼或管理 bearer，不替使用者猜地理位置、同意遙測或授權設備。

## 驗證界線

程式測試、官方最新來源下載、真封裝 CLI／Hermes 與本機協定 fixture、一次性 VM 安裝、真雲端登入和 Owner 家庭分開記錄。0.5.1 精確ISO已在一次性VM驗證最新版Hermes安裝、HAOS啟動／還原／重啟；無帳號的安裝案例直接呼叫固定Host動作，自動Web流程另有真服務／CLI／MCP／Core瀏覽器整合，但模型及Host副作用為fixture，不能合稱真雲端端到端onboarding。最新結果見 [驗證紀錄](VERIFICATION.md)，Plan v3 原有 32 案不因新增 onboarding 測試自動改為 passed。


## 原生家庭首頁與智能管家：2026-10-12 介面方向評估

Owner 指出聊天首頁的「今天想處理什麼」不自然，並要求評估完成建置後銜接 Home Assistant 原生 Web。這是下一輪介面方向評估；目前 0.6.0 已交付的聊天入口、截圖及受測 ISO 尚未實作下述切換，不能將提案寫成已完成。

Home Assistant 已有家庭儀表板、設備控制、自動化與設定介面；原生 onboarding 結束後即進入其 dashboard。產品應優先使用這個日常家庭入口。外層 Web 負責安裝、AI／通道連接及故障恢復；智能管家負責對話、研究、提案和結果。這三個用途可以有清楚的入口，不必複製一套家庭控制介面。[原生 dashboards](https://www.home-assistant.io/dashboards/)、[原生 onboarding](https://www.home-assistant.io/getting-started/onboarding)。

建議流程如下，尚待實作與專項驗證：

| 狀態 | 預設體驗 |
|---|---|
| 外層尚未設定／主動選擇只用外層 | 繼續安裝引導，或使用外層助手；HA 尚未建立不阻擋使用 |
| HA 正在準備或原生設定未完成 | 顯示真實準備進度；就緒後進原生設定，必要的 Owner 帳號與授權仍由本人完成 |
| HA 已完成且可用 | 日常啟動進 Home Assistant 原生 dashboard；聊天成為可隨時開啟的智能管家入口 |
| 有提案、進行中的對話或尚未儲存的修改 | 保留明確深連結與當前工作，不因預設首頁變更強制跳走 |
| HA 無法開啟 | 外層獨立入口仍可診斷與修復；避免自動跳轉迴圈，也不把故障誤判為需要重新初始化 |

第一個切片應先完成正常頁面導向、原生登入及返回智能管家的可用路徑。第二個切片再將管家入口整合到 HA 側欄；官方 App Ingress 可承接 HA 登入，但現有 Controller App 是另一個部署模式，不能直接宣稱它已連到外層 Hermes。目前 `create_app` 僅在 host mode 建立外層 Coordinator 與 gateway／Hermes／worker API；Ingress 模式是自己的 DB／Engine，不能以側欄可見推論同一個 Hermes 已接通。需要薄型、經驗證的外層 bridge、使用者身份對應、家庭授權、登出／撤權、對話 scope 與故障 fallback，並沿用同一外層 case／proposal store；不得把 HA 的所有登入者都當成外層 Owner，也不直接轉送瀏覽器提供的身份 header 或共享 Host cookie。[官方 Ingress](https://developers.home-assistant.io/docs/apps/presentation/#ingress)。

原生設備控制維持 HA 自己的身份及權限。由 Agent 提出的家庭變更仍走既有精確提案／Owner 批准；HA 側欄入口不能繞過該流程。HA Assist／Conversation 可另作後續接點評估，並非已完成的 Hermes adapter。

實作前須核对本機、LAN 與遠端的受信任 URL／登入路徑。既有 Host Web 使用 localhost:8099 與本機 HA :8123，不能只改首頁字串就宣稱遠端銜接也可用。專項驗收至少包含首次設定、後續啟動、HA 不可用、外層救援、非 Owner、登出撤權、待批准提案／草稿保留，以及返回管家的完整瀏覽器流程。
