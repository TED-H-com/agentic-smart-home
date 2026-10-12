# 自動 onboarding：CLI 先行

依 2026-10-12 Owner 指示，首次使用從選擇熟悉的 coding CLI 開始。0.7.0 本輪接續加入薄型 HA 側欄與自家 App 維護；HA 系統更新由 HA 官方流程管理。預設區域網路自動取得位址；固定 IP／Tailscale 的選擇保留稍後處理，不是登入助手的前提。

```text
USB 開機 → Linux 安裝／連網 → Web
  → 選一個 CLI → 官方登入
  → CLI 檢查設定需求 → 準備發行受測的 Hermes
  → Hermes 帳號驗證 → LINE／Telegram／Discord／Web
  → 自動建立或啟動 HAOS → HA 原生 onboarding → 綁定家庭 Owner
  → 加入智能管家側欄 → 日常使用原生 HA
```

使用者選擇一次 CLI 和主要通道；「開始設定」旁說明完成後加入側欄，展開可查看只維護本產品 App 的授權範圍。啟動設定後，Controller 的持久工作會自動執行已授權步驟。關閉 Web 不取消工作；重新開啟讀取原進度。讀取進度本身不建立工作。帳號授權、LINE 掃碼、建立 Telegram bot、家庭密碼和設備權限仍需真正的 Owner 操作；無法登入或服務中斷時顯示實際等待原因。

## 安裝與網路

公共 ISO 預設啟動安裝選項，Ubuntu 安裝器處理網路、磁碟及 Linux 身份；選定目標後自動完成套件、桌面、產品服務與外層配對。公共媒體不預選要清除的磁碟、不內嵌通用密碼。Linux 桌面登入後自動開啟 Web，使用本機 Owner 的 120 秒一次性票據登入；Controller 的私有票據檔只保存 hash；家目錄0700目錄中的0600暫時 HTML 傳遞票據並在120秒後清除，程序參數不含 bearer，浏览器立即清除 fragment，票據不進 storage。其他 Linux 帳號或遠端瀏覽器仍使用明確的金鑰登入。初次 firstboot 只啟動外層；HAOS 由後續 onboarding 自動啟動，故內層未準備好不阻擋外層登入。

主機的 network.ready 是外層預設路由觀測，並不宣稱雲端一定可用。真正下載、OAuth 和推論各自驗證對外連線。固定 IP、Tailscale 或公開網路位址尚不自動設定；現有 Web 維持 loopback／SSH tunnel 的可用路徑，不自動把管理介面曝露到網際網路。

## Agent 與程式的分工

選定的 CLI 實際執行固定 bootstrap prompt，只取得必要且無秘密的觀測，產出經 schema 驗證的建議。它不能提供任意 URL、shell、套件名或磁碟路徑給 root broker。安裝、設定與狀態核對由固定程式執行；CLI 回覆「成功」不算完成證據。這個受限設定助手不是 v3 的通用 coding worker，不解除既有 home_operator 限制。

Hermes 的 OAuth 與 CLI 登入由各自的受信任接點管理，不複製可輪換 refresh token、不共用可寫 HOME。Codex 可使用官方 device-code 流程；其他 CLI 依實際 adapter 支援呈現，未支援的訂閱登入不冒充可用。Hermes 仍須真實推論成功才標示 ready，僅保存憑證為 configured。相關官方文件：[Codex 登入](https://learn.chatgpt.com/docs/auth)、[Hermes 認證池](https://hermes-agent.nousresearch.com/docs/user-guide/features/credential-pools)。

## 0.7.0 發行受測 Hermes

本輪固定 `bootstrap_hermes` 預設改用安裝包 `images.lock` 的 Hermes image digest 與 bundled source commit；不在家庭主機解析 HEAD、下載上游來源或現場 build。仍執行無憑證的離線相容性 probe，錯 digest 不接受。已 ready／已有設定的服務保持原狀，不把新 bootstrap 當作使用中升級或重登入。維護者仍可用 `scripts/check_upstream.py` 測最新候選，候選測試不改正式安裝。本輪精確媒體及真 App 的平台結果另記 [驗證](VERIFICATION.md)。

## 0.6.1 歷史：首次準備最新版 Hermes

Owner 後續改採 [自家發行策略](RELEASE_POLICY.md)：新版本安裝產品發行已驗證的 Hermes image，追上游 HEAD 留在開發端。以下只描述已發行 0.6.1 的歷史行為；本輪切換與工程驗證另記，不把旧媒體結果當成新路徑已驗證。

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


## 原生家庭首頁與智能管家

完成設定後，日常啟動進 Home Assistant 原生 dashboard；外層 Web 保留安裝、智能管家與維護入口。產品不複製家庭儀表板，也不修改上游前端。原生設備控制維持 HA 自己的身份與權限。[原生 dashboards](https://www.home-assistant.io/dashboards/)、[原生 onboarding](https://www.home-assistant.io/getting-started/onboarding)。

| 狀態 | 預設體驗 |
|---|---|
| 尚未完成外層設定 | 接續原進度，亦可先用外層助手 |
| HA 正在準備或原生設定未完成 | 顯示實際進度，Owner 完成原生帳號與授權 |
| 本產品設定完成、Owner 有效且 HA 可用 | 無明確頁面的日常入口進原生 HA；完成頁亦提供「進入家庭」 |
| 明確開啟對話、提案、設定或維護 | 留在指定頁面，保留工作與草稿 |
| HA 斷線、身份失效或介面不相容 | 留在外層，提供智能管家、主機維護與手動開啟 HA |

跳轉之前使用獨立的 `home-entry` 契約，即時讀回 Owner 及 Core 配置；不能只看 VM running 或舊的已連線徽章。離開前保存 `#entry` 返回點，Back／reload 不再自動前進。新的 Core 版本字串與額外欄位不阻擋原生使用；Agent 家庭寫入仍有自己的相容性與批准檢查。詳見 [adapter 邊界及候選驗證](ADAPTERS.md)。

目前使用本機 8099／8123 的正常頁面導向。完成頁手動開啟原生 HA 使用新分頁，外層管家入口保留；日常自動進入則在同一分頁，可返回外層。遠端仍須同時轉送兩個端口，不宣稱已完成公開網路／單一網域代理。HA cookie／bearer 不搬到 Host origin。首次開啟原生 Web 若尚無原生登入狀態，仍依 HA 畫面登入；Controller 的家庭連接授權不等於另一個瀏覽器 client 已登入。

0.7.0 以獨立薄型 Ingress App 連接外層 Hermes，驗證有效 Owner 並沿用同一外層 case／proposal store。舊 Controller App 的另一份 DB／Engine 不屬於這條橋接；瀏覽器不能自填身份 header，也不共用 Host cookie。撤權、斷線與批准的驗證範圍見 [Adapters](ADAPTERS.md)；真 HAOS 原生側欄的對話接續證據見 [瀏覽器記錄](../evidence/bridge-haos-local.json)。[官方 Ingress](https://developers.home-assistant.io/docs/apps/presentation/#ingress)。

HA Assist／語音接入另行驗收。真家庭、雲端帳號與實體 laptop 的驗收狀態見 [v3 帳本](v3/PROGRESS.md)，不由瀏覽器 fixture 自動推定完成。

## HA 智能管家側欄與外層救援

0.7.0 本輪薄 App 只顯示智能管家對話、同 case 成果及待批准的家庭提案，重用外層服務，不建立第二份家庭 DB。只有當前有效的家庭 Owner 可以讀取或批准；一般 HA 管理員或 App transport 憑證不足以取得家庭身份。連接失效顯示重連，沒有暗中建立另一個本地助手。

新 onboarding 預授權在家庭連接完成後安裝及維護本產品 App。已完成舊版設定者需在外層「設定 → HA 智能管家側欄」明確啟用。可停止自動維護而保留側欄，也可撤銷側欄連接；手動停止、移除或修改 App 不會被自動抵銷。維護狀態的「側欄服務可用」只表示傳輸與 App 觀測，不代表模型已驗證或當前瀏覽器 Owner 已授權。

HAOS／Core／Supervisor 與其他 Apps 仍由 HA 原生更新管理；外層只修自家 App／adapter。HA 內的「回到家庭」回原生頁面；Linux 桌面入口與明確外層連結仍可取得設定、登入及獨立救援，不把這些控制項放入側欄。相對路徑、撤權及 PNG 預覽邊界見 [Adapters](ADAPTERS.md)。
