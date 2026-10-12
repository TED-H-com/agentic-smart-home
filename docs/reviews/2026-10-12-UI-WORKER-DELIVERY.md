# 0.6.0 畫面、開發工作與交付審查

接續 [0.5.1 審查](2026-10-12-E2E-ARCHITECTURE.md)。這次直接檢視真 Chromium 產生的 1440、768、390 px 頁面，修正自家 Web 的操作順序，並接上審查中缺少的 Host coding、成果與固定交付路徑。API／模型 fixtures 和真平台的範圍各自記錄，沒有修改原廠 OAuth 或 HA wizard。

## 截圖後實際調整

| 原問題 | 調整與核對 |
|---|---|
| 手機常駐十項導覽，開始設定的主按鈕被推到第一屏之外 | 收合導覽、短版五步進度、縮短開場文字；390 px 下可直接選 CLI 並繼續，點導覽後自動收起 |
| 尚未綁家庭時先顯示錯誤色，容易誤認安裝失敗 | 設定期間使用中性的「家庭尚未連接」；執行錯誤仍保留真正錯誤狀態 |
| OAuth 等待中仍重複顯示可提交的登入表單 | 專注顯示官方入口、可複製代碼和等待狀態；刷新接回同一認證 handle，拒未知網址 |
| LINE 掃碼、手動 ID、家庭配對同時搶注意力 | 首先顯示 QR 與手機確認碼，連線後再配對聊天室；手動設定放在進階區 |
| HA 的原廠初始化和授權順序不清楚 | 自家引導明列先完成 HA、再授權連接；原廠頁面保持原樣 |
| 開發工作只剩 handle，不知道在哪看結果 | 新增開發頁，展示證據、patch、檔案指紋、獨立驗證與三尺寸預覽；同 case 對話直接連到該份成果 |
| 手機先看到新工作表單，完成的結果在很下方 | 新工作／查看成果分頁；已有工作預設成果，保留 44 px 控制與 16 px 輸入文字 |
| 主機更新和遠端選項會擠滿初始設定 | 收在主機的「網路與系統維護」；批准時才展示精確版本／digest 與獨立退版操作 |

截圖是編譯後產品介面，頁面狀態使用明示 API fixtures。QR、登入碼、案例與家庭名稱不是可用的真帳號資料。這些截圖證明介面呈現與瀏覽器操作，不證明雲端同意或家庭連線。

## 使用者回饋：改以智能管家對話為主

使用者指出「開發工作」不易理解，且整體說明過多。後續版已將 Host 主選單縮為五項，智能管家直接呈現對話框；Worker 手動表單移至設定的進階工具。對話記錄可接續，成果在原 case 展開，技術識別與 checkpoint 預設收起。已完成設定的主入口直接回到智能管家。

初次設定刪去重複文案，仍保留官方登入、授權期限與撤銷、斷線和重試。設定改為簡短入口與折疊項目。截圖檢視另修正長篇成果被底部輸入框遮住的情形。前一版30張截圖仍留在 `evidence/ui/0.6.0/` 供比較，最新版另存 `evidence/ui/chat-first/`。

## 工程缺口的處理

| 0.5.1 缺口 | 0.6.0 交付與邊界 |
|---|---|
| Runner idle retention／下調不同步 | Controller、全部配對 Runner、Hermes、coding 成果的期限同步與 idle 清除；離線 ACK 保留 barrier；縮短立即失效，去重 tombstone 不重啟工作 |
| stop_unconfirmed 無可信解除 | exact job／配對身份／程序停止核對；重啟和回覆遺失先 reconcile，未能證明停止不釋出新 lease |
| Host coding 只有 blocked handle | 真 Codex tools、角色登入、來源 bundle、私有 clone、diff、測試及無網路獨立 verifier；第一版只啟用 Host Codex coding |
| provider／研究來源出站缺失 | model bearer 留受控 relay，固定 HTTPS 來源、拒私網／管理端點／任意 redirect，有請求量、大小與時間上限 |
| 產物與預覽缺失 | 停止後收集 bounded artifact，拒越界／symlink／hardlink；固定無網路 renderer 產三種 PNG，獨立 origin、短效連結與撤銷 |
| 人工接手只有概念 | 提供停止證明與 checkpoint／patch 匯出；不宣稱能加入同一個互動 BAT session |
| Hermes 研究與家庭操作斷開 | household MCP adapter 走原 Engine、現行 policy、精確 proposal 與 Owner 批准；worker 的文字不能自行批准 |
| 沒有固定更新／退版 | Ed25519 發行者、同 schema Controller digest、probe、quiesce、backup、health、精確批准；退版保留當前 DB，不覆蓋新操作 |
| 網路選擇不明 | DHCP／路由器 reservation 偏好與 Tailscale 身份分開；標準 SSH forwarding 保持 localhost origin，能撤銷產品管理的登入 |

較低層的 JSON fixture、真 CLI、本機容器、安裝後 VM 各自回答不同問題。固定模型 transport 可要求 Codex 真正呼叫工具、修改檔案並跑測試，但不能證明真模型對家庭問題的研究品質。簽名 fixture 候選的安裝／退版也不代表公開發行者已經發布受測版本。

最後交叉檢查另實際重現候選 `pytest.py` 遮蔽測試工具，以及候選匯入時 `os._exit(0)` 導致未測先綠。驗證器固定先載入工具、要求完整測試完成紀錄；零測試、跳過或未完成不能當成功。這種白箱驗證仍有候選程式在同一 Python interpreter 執行的限制，不能單憑它批准部署。舊媒體未做安裝驗收，保留其來源與 digest 後重建。

精確安裝又發現正式 broker 的 `umask 0077` 使可信測試目錄變成 root-only，獨立 verifier 因無法讀取而失敗。修正只將複製完成的目錄明確設為 root-owned 0555，測試檔案仍為 0444；真正的 prepare／UID 10000／固定 Runner 回歸確認可讀、不可寫並完成兩項測試。安裝 probe 另改為區分 Host 隔離環境就緒與角色未登入，要求未登入派工拒絕執行、無 lease 且重送不重複。兩次失敗和修正證據保留於 [版本驗證](../VERIFICATION.md)，修正後媒體須另行完整安裝。

重新安裝後，真 Codex 與兩項獨立測試已通過；接著發現安裝器的初始環境檔含註解，而 Hermes／updater 共用 writer 原本拒絕註解。修正只接受空白與獨立註解，保持固定欄位和 digest 驗證並拒絕重複欄位。直接取安裝器 literal 的契約回歸與 bootstrap／updater suite 共 56 項通過；最新媒體仍須完整安裝。

更新器的實際映像回歸又重現固定 probe 的兩個問題：腳本掛載在根目錄，沒有將 `/app` 套件加入匯入路徑；SQLite 交易 context 不會關閉連線，描述符仍存活到測試磁碟釋放。修正固定匯入位置、保留交易提交並明確關閉連線，同兩項回歸由失敗變為通過。磁碟配置／掛載在局部重現中是替代實作，完整 ext4 和更新／退版仍以新 ISO 安裝結果為準；各轮失败记录未移除。

## 仍需獨立完成

- 真 HAOS App 診斷已證明受限 SYS_ADMIN 與自訂 AppArmor 設定可建立 App 專屬 cgroup，並實際觸發記憶體限制；完整 Home launcher、出站／憑證／磁碟界線與真 Codex tools 流程仍未交付，維持 blocked。App、Core 容器和整個 HAOS 的觀測不可混稱。
- Claude／Antigravity／Grok 的既有家庭與初始化 adapter 保留；各自 tools-enabled coding 邊界尚未交付。不能因 CLI 存在就顯示 coding ready。
- 目前預覽是 PNG；互動候選站台、同一 BAT live session 接手、bot 主動完成推送未交付。
- updater 首版只處理相同 schema 的外層 Controller。跨 schema／OS／HAOS／Core／Runner／Hermes 全套升級，需要各自相容與恢復契約。
- 真 OAuth、LINE／Telegram／Discord、Tailscale 帳號、Owner laptop、實體設備、72 小時及雙 worker 對照，仍按 [32 案](../v3/PROGRESS.md) 逐項執行。

上列未完成事項保留可讀的能力與限制，不因本輪工程測試通過批次宣稱 E1／E2 或全部產品驗收完成。最終數字與來源媒體以 [版本驗證](../VERIFICATION.md) 為準。
