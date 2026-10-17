# 安裝 Agentic Smart Home 0.10.0 候選版

本頁是正在準備的 0.10.0 安裝流程。新 ISO 尚待完整媒體驗證與公開發布；請以 [驗證紀錄](docs/VERIFICATION.md) 和同版 Release 的檔名／校驗碼為交付依據。既有 0.9.0 ISO 不包含多來源盤點與接入追蹤，不適合拿來驗收本頁的新功能。

## 需要

- Intel／AMD x86-64 電腦、UEFI，開啟 Intel VT-x／AMD-V。建議 **16 GB RAM、128 GB SSD**；硬體檢查以系統實際可用記憶體至少 **7,680 MiB** 與可建立 VM 的 KVM 為準。約五年前的 Dell 仍需確認實際規格，年份本身不是相容證明。
- 安裝時插上 AC 電源，優先用 **Ethernet 網路線接家庭主網路**；沒有網路孔時可使用 Linux 支援的 USB Ethernet。安裝和第一次設定都需要下載元件。
- 一支可以清除的 **32 GB USB**，以及可以製作 USB 的電腦。
- 筆電準備作專用主機；安裝時選中的內部磁碟會清除。先把要保留的資料搬走。
- 手機保留 Smart Life／Tuya Smart、Hue 等原有 App，後續需要你本人登入、掃碼或按配對鍵。
- 一個 AI 帳號，例如 ChatGPT 訂閱或 API 金鑰。

## 安裝

1. 用 `SHA256SUMS` 核對 `agentic-home-0.10.0-laptop-amd64.iso`。Linux 可用 `sha256sum agentic-home-0.10.0-laptop-amd64.iso`；Windows PowerShell 可用 `Get-FileHash .\agentic-home-0.10.0-laptop-amd64.iso -Algorithm SHA256`。結果須與清單中 ISO 那一行完全相同。
2. 用 Rufus 或 balenaEtcher 把 ISO 寫入 USB。這會清除 USB。
3. 從 USB 開機，選 **Install Agentic Home Linux**。先檢查 CPU 架構、記憶體與 KVM；不符合時會在磁碟設定前停止，顯示需增加記憶體或開啟 BIOS 虛擬化等原因。通過後，在 Ubuntu 安裝器選有線網路、要安裝的內部磁碟和 Linux 帳號。專用筆電可選使用整顆磁碟，務必核對容量與型號，避免選到外接資料碟。沒有預設密碼，也不會預先選磁碟。
4. 移除 USB 並重新開機。第一次開機會下載桌面套件、Firefox 並載入產品映像；請預留至少 30 分鐘，網路或磁碟較慢時可能更久。想看進度：`journalctl -fu agentic-home-firstboot`。仍有進度時讓它繼續，完成前不要反覆重開機。
5. 用剛建立的帳號登入桌面，**Agentic Home** 會自己打開。沒打開時再點一次桌面捷徑。

桌面登入用的是 120 秒、只能用一次的本機票據。其他帳號或遠端瀏覽器用主機金鑰：`sudo agentic-home access-key`。

## 第一次設定

照畫面一步步完成。關掉頁面不會取消；再打開會從原處繼續。

1. **家庭接線**：在按「開始設定」之前，展開 **先設定家庭區網（有線設備探索）**，明確選 Ethernet 並按 **保存家庭接線**；未選擇則維持基本 NAT。接法與候選驗證範圍見 [家庭區網接入](docs/HA_LAN.md)。
2. **AI 帳號**：選一個畫面顯示可用的 provider，例如 Codex，完成官方登入。
3. **智能管家**：用 ChatGPT 訂閱或 OpenRouter 金鑰登入。系統會實際問一次，確認能回覆；這可能用到一點額度。
4. **通訊**：第一次實測可先選 **只用網頁**。需要時再接 LINE（掃描、選聊天室、傳配對碼）、Telegram（機器人權杖與配對碼）或 Discord。
5. **家庭**：系統自動建立 Home Assistant。在 Home Assistant 建立家庭和擁有者帳號，回來按 **授權連接家庭**，再在家庭確認授權。
6. 完成後，Home Assistant 側欄會出現 **智能管家**，日常直接用 Home Assistant。桌面的 Agentic Home 保留設定與救援。

智能管家、AI 帳號和開發助手的登入各自保存，互不共用。

## 開發助手

- **主機**：設定 → 開發助手 → 登入開發助手（Codex 裝置碼或 API 金鑰），描述問題後按 **開始調查** 或 **開始修改並測試**。成果依序是發現、獨立基準測試結果和修改內容；工作執行中可以停止，失敗時可以再試一次。
- **家庭現場**：同一頁按 **啟用家庭現場**。系統會把 App 裝進 Home Assistant 並完成配對，之後另外登入一次。你在 Home Assistant 停止或移除它，系統不會自動裝回。

開發助手只修改自己的私有副本，沒有家庭權限，成果不會自動部署。

## 第一次實測順序

這是工程預覽版。媒體安裝與隔離平台驗證由開發端完成；真帳號、這台筆電與實體設備由現場驗收。先完成一個可核對的流程，再擴大到全屋：

1. 完成首次設定，確認智能管家能回覆，Home Assistant 的 **智能管家** 側欄能打開同一段對話。
2. 登入 **主機** 開發助手，要求一個小修改，例如「只改善 README 的安裝說明，保留行為不變，完成後執行測試」。核對發現、獨立基準測試結果和實際差異；「未執行」不算通過。
3. 等工作結束，記下已登入的角色、你手動停止的項目和對話。保持頁面開啟，正常重開整台筆電；服務啟動時可顯示等待，回來後確認登入、停止狀態、Home Assistant、側欄與對話保留。不需要在等待時重新登入或配對。
4. 按 **啟用家庭現場**，等安裝和配對完成，再登入它自己的 Codex。首次需要從公開 `#stable` 下載並建置 App，可能花數分鐘；頁面顯示安裝或等待時讓原流程繼續。先做一次 **開始調查**，確認成果的觀測位置為 `haos_app`。
5. 在 **設備 → 探索更多設備** 選家庭網路並開始探索；有路由器清單時，再匯入 JSON、CSV 或 dnsmasq leases。核對來源、時間與建議，不能把觀測筆數當作實體設備數。
6. 先選一組 Hue 或 Tuya／Smart Life，按 **追蹤接入**，在 HA 原生頁面完成配對／帳號授權，再回來明確選擇整合並讀取結果。先確認一盞燈的狀態與實體動作，再擴大設備。Google Home／Alexa 的全部帳號設備不會因 LAN 探索自動匯入。
7. 做下方的測試開關自動化；每次部署仍需看過提案並批准。暫停 AI 後，確認已部署的 HA 規則仍可觸發。最後再次重開筆電，確認盤點、接入追蹤、對話與登入保留。
8. 持續觀察 72 小時，記錄斷線、重登、合蓋／散熱與設備失聯恢復。做一份加密離機備份；長時間測試未完成前，保持這些項目為未驗收。

若家庭現場暫時不能工作，記下它的狀態，仍可繼續驗收智能管家與主機開發助手；不要把未完成的家庭現場項目記為通過。公開 `#stable` 必須提供與主機相同的版本，側欄安裝與家庭現場啟用才能完成。當頻道升到 0.10.0 後，舊 0.9.0 媒體的新安裝可能停在「商店版本較新」；新主機應使用已發布且驗證的同版媒體。不要解除版本檢查，也不要在已用的主機直接重跑 installer 覆蓋資料。

### 工作顯示「停止尚未確認」

按該工作上的 **確認已停止**，讓系統核對並重試停止原工作。這個按鈕不代表由你保證程序已停止；只有程式確認後才會釋出工作區。系統也會在背景重試。

如果仍未確認，保留原工作，不要刪除記錄、解除安裝 App 或反覆重啟來繞過。展開 **技術資料** 記下工作、原因代碼、觀測位置與發生時間，再依下方救援步驟產生本機支援檔。一般支援檔不包含這份工作的詳細啟動原因。確認停止後的失敗／中斷工作不會自動重做；需要時再明確開始新工作。

家庭現場在工作尚未確認停止時異常重啟 App，同一次 Home Assistant OS 開機中的核對仍可能無法證明舊程序已停止。此時保留原工作並交由維護者核對；「確認已停止」不是強制解除保護的重設按鈕。

## 第一條家庭自動化

1. 在 Home Assistant 建立兩個測試用開關：`input_boolean.agentic_motion_test`、`input_boolean.agentic_lamp_test`。
2. 到 **設備** 允許讀取這兩個開關，用途選「測試用開關」，儲存。
3. 對智能管家說：「motion_test 從關閉變開啟時，打開 lamp_test，先讓我看規則。」
4. 看過提案再批准。系統會備份、寫入、啟用，再確認狀態。切換 motion_test，看 lamp_test 是否跟著開啟。

規則由 Home Assistant 執行；停止 AI 不會停止已部署的規則。

## 從另一台電腦使用

```bash
ssh -L 8099:127.0.0.1:8099 -L 8123:127.0.0.1:8123 your-user@your-host
sudo agentic-home access-key
```

瀏覽器開 `http://localhost:8099`；家庭是 `http://localhost:8123`。這兩個主機入口只聽本機，也可以在 **設定 → 主機維護** 登入 Tailscale 後使用相同 SSH tunnel。若已啟用有線家庭接入且讀回 HAOS 的 LAN 位址，同網段手機可另外開啟 HA 原生 LAN 網址並登入；外層管理中心不會因此公開到 LAN。接法與回復步驟見 [家庭區網接入](docs/HA_LAN.md)。

## 已有 Ubuntu 24.04

在**要安裝的那台電腦**執行，不要在開發機執行：

```bash
tar -xf agentic-home-0.10.0-linux-amd64.tar
cd agentic-home
python3 host/install.py --profile laptop-amd64
sudo python3 host/install.py --profile laptop-amd64 --apply
```

不加 `--apply` 只列出會做的事。安裝會檢查版本、架構、KVM、記憶體、空間和檔案 hash，不會覆蓋已存在的 `/opt/agentic-home`。沒有桌面的主機用 `--profile server-amd64`。

## 救援

Web 或 AI 不能用時，在主機的本機終端：

```bash
sudo agentic-home diagnostics
sudo agentic-home support-bundle --output ./agentic-support.json
```

support bundle 只含固定、去密的狀態，不含記錄原文、對話或憑證，也不會自動上傳。

Home Assistant 的固定操作：

```bash
agentic-home status
agentic-home start
agentic-home shutdown
agentic-home snapshot
agentic-home restore --snapshot <完整快照ID>
```

冷快照會先正常關閉家庭系統，期間家庭自動化暫停。快照含家庭資料，離機保存請加密。更多情況見 [救援](docs/OPERATIONS.md)。

## 範圍

- 外層主機可在選定的實體 IPv4 網路收集鄰居表、mDNS／SSDP。HAOS 預設基本 NAT，0.10.0 候選另提供明確選擇的有線接入；保留原管理網卡，不改主機 IP。保存接線、guest 位址、HA adapter 和實體設備動作分別讀回／驗證。
- 有線接入的精確媒體與巢狀 VM 測試仍待本輪驗證；Matter／Thread、Cast 播放和手機入口不能僅憑配置保存或探索成功判定可用。
- USB dongle、Wi-Fi 驅動及實體設備的每項功能仍需現場確認；[設備探索](docs/DEVICE_DISCOVERY.md) 說明來源與接入範圍。
- 實體筆電的散熱、網卡和 UEFI 需要現場驗證。
- VM 快照、Home Assistant 備份、主機 Controller、智能管家和各 AI 登入是不同的備份層。

架構與驗證：[Linux 架構](docs/LINUX_PRODUCT.md) · [通訊](docs/GATEWAY.md) · [驗證紀錄](docs/VERIFICATION.md)。
