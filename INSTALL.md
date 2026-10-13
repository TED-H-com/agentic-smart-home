# 安裝 Agentic Smart Home：0.7.0 媒體與 0.8.0 App 預覽

產品從外層 Linux 開始；Web 引導先完成 CLI、Hermes 與通訊，再自動建立／啟動 HAOS。firstboot 只啟動外層服務，因此家庭尚未準備好仍能先登入助手。完整順序見 [自動 onboarding](docs/ONBOARDING.md)。

以下 ISO／TAR 步驟描述最近完成精確媒體驗證的 0.7.0。0.8.0 是原始碼／App 預覽，尚未完成新版 ISO 驗收；不能將以下舊媒體視為已含 Home coding。新候選媒體依 [README](README.md) 建置，Git repository 不包含大型 TAR／ISO。候選 hash、一次性 VM 與硬體測試的實際結果以 [驗證紀錄](docs/VERIFICATION.md) 為準；歷史安裝結果不自動算成本候選驗收。這是 v3 技術預覽，Host Codex coding 已接通；Home coding 須通過真 HAOS App 隔離驗證。

## A. 整台重灌

1. 取得 `agentic-home-0.7.0-laptop-amd64.iso` 與 `SHA256SUMS`，核對 SHA-256。
2. 備份目標電腦資料，用 Rufus／Etcher 將 ISO 寫入 USB。這會清除選中的 USB；後續選中的系統磁碟也會重新分割。
3. Intel／AMD x86-64 使用 UEFI，開啟 VT-x／AMD-V，從 USB 開機。建議 16 GB RAM／128 GB SSD；laptop profile 檢查約 8 GB RAM、KVM 和可用空間，安裝需要網路。暫不建立 HAOS 不會取消這些主機規格檢查。
4. 選 **Install Agentic Home Linux**，在官方 Ubuntu 安裝器設定網路、目標磁碟與 Linux 帳號。沒有預設密碼或預選磁碟。
5. 移除 USB 並重啟。firstboot 安裝套件、載入五個產品 images（含隔離預覽）、啟動外層服務並配對受控 Runner。此時 HAOS 尚未建立。進度可在 local console 查看：

   ```bash
   journalctl -fu agentic-home-firstboot
   ```

6. 登入安裝器建立的主要 Linux 帳號（UID 1000），桌面自動開啟 **Agentic Home**。程式使用有效 120 秒、僅能兌換一次的本機票據登入；票據不存於瀏覽器 storage，網址 fragment 立即清除。若開啟失敗，可再次點桌面捷徑。手動金鑰只作備援：`sudo agentic-home access-key`，家目錄的 `Agentic-Home.txt` 亦有此主機金鑰。
7. 選一個 coding CLI，完成它支援的官方登入。Codex／Grok 提供 device code；Claude 支援 API key／setup token，Antigravity 目前支援 Gemini API key。沒有把未支援的 OAuth 顯示成可用。
8. 開始後 CLI 執行固定設定 prompt，程式檢查隨產品版本交付的 Hermes 相容性，再按固定映像 digest 啟動。完成 Hermes 自己的 Codex OAuth 或 OpenRouter 設定；此認證與 CLI 各自保存，不共用 refresh token。流程自動做一次實際推論驗證，可能使用供應商額度。
9. LINE 是預設入口：掃 QR、選聊天室、從預定操作帳號傳送畫面的短效配對碼。Telegram 使用 BotFather token 加配對碼；Discord 保留頻道／使用者設定；也可選 Web。配對完成後才允許該 sender／聊天室使用助手。
10. 通道就緒後自動建立／啟動 HAOS，等待真 Core onboarding API。進入 HA 原生設定頁，建立家庭與管理者帳號，再回到引導按 **授權連接家庭**；官方 HA 授權頁同意後自動驗證 active owner 並保存專用授權，不需貼長效權杖。
11. 新設定完成後自動加入 **智能管家** 側欄，日常使用原生 HA。側欄沿用外層對話，桌面入口保留設定及救援。系統會檢查 Host 的更新保護；舊 Host 只更新 Controller 不會取得這項能力，不能當成已完成整套升級。

流程保存進度；關閉頁面不取消工作，重新開啟會接續。需要真人授權時等待，不把僅保存金鑰當作推論成功。等待通道或家庭授權時可以先開外層對話；離開頁面不會停止已開始的自動安裝。預設使用 DHCP 與本機 Web；「Linux 主機 → 網路與系統維護」可選路由器 DHCP reservation 偏好或完成 Tailscale 官方登入，詳見 [網路與更新](docs/UPDATES.md)。

## B. 家庭與內層 Runner

若先離開設定畫面，日後可從引導查看並繼續；固定主機入口也保留 `sudo agentic-home provision`。broker 只管理產品自己的 VM；首次 HAOS 啟動可能下載元件，可以離開頁面等待。

原生 HA 設定與家庭授權完成後：

1. 在 HA App 商店加入 `https://github.com/teddashh/agentic-smart-home`，安裝 **Agentic Home Runner**、保留 8443。將記錄中的一次性配對 JSON 貼至外層 Web 的 HAOS 內層登入區，再獨立登入 CLI。本機來源可用 Web 提供的 Runner ZIP，放入 Samba／SSH App 提供的本機 Apps 目錄後重新載入。新版文件使用 `/local_apps/agentic_runner`，部分映射仍為 `/addons/agentic_runner`；依所用 App 的實際掛載目錄操作，參考 [官方本機測試](https://developers.home-assistant.io/docs/apps/testing/)。
2. 到「設備與權限」選擇分享與操作範圍。一般對話仍是 Hermes；目前家庭提案走「受控 Agent（進階）」→「HAOS 內層」，或通道的 `/home 需求`。精確提案留在 Web 等待 Owner 批准。

0.8.0 Runner App 保留受控 `home_operator`，另有獨立 Home Codex coding。完成 TLS 配對後，從「設定 → 進階工具」選「家庭現場助手」，再完成獨立 coding 登入；不共用外層或家庭 CLI profile。App 需要宣告的 `SYS_ADMIN`、`/dev/fuse` 和隨附 AppArmor，邊界不可用時只停用 coding。詳細能力、平台證據與重啟限制見 [Workers](docs/WORKERS.md)。內層 App 自動安裝／登入尚未交付，配對成功不代表 E2 完成。HA 手動貼 token 保留為進階備援。

## C. 無桌面或已有相符 Ubuntu

既有 server profile 可建置 `agentic-home-0.7.0-server-amd64.iso`，省略桌面套件。另一台電腦使用 SSH tunnel：

```bash
ssh -L 8099:127.0.0.1:8099 -L 8123:127.0.0.1:8123 your-user@your-host
sudo agentic-home access-key
```

瀏覽器開啟本機 `http://localhost:8099`。HAOS 建立後才使用 `http://localhost:8123`。兩個 listener 都只綁 loopback；不轉送 Hermes 私有 8096 或 MCP 8097 給瀏覽器。

已有相符的 Ubuntu，可保留分割並安裝產品。在**選定的目標 Linux**執行，不能在開發主機套用：

```bash
tar -xf agentic-home-0.7.0-linux-amd64.tar
cd agentic-home
python3 host/install.py --profile laptop-amd64
sudo python3 host/install.py --profile laptop-amd64 --apply
```

不含 `--apply` 的命令只顯示安裝內容。Ubuntu 24.04 可選 laptop-amd64／server-amd64；26.04 的 laptop-new-amd64 仍是擴充 recipe。安裝檢查版本、架構、KVM、RAM、空間與檔案 hash；既有 `/opt/agentic-home` 不會被另一份安裝包直接覆蓋。日常相同 schema 的 Controller 更新另走 [固定 updater](docs/UPDATES.md)；這個安裝命令不是原地升級器。

Controller 0.7.0 使用 schema 3。開啟 schema 1／2 既有資料時，先建立 mode-0600 `.before-v3-<uuid>.sqlite3` 一致快照，再向前遷移；保留 settings、records、audit、去重紀錄。較舊 binary 拒絕 schema 3；不能手改 marker 或用舊 DB 覆蓋後續家庭操作。詳見 [OPERATIONS](docs/OPERATIONS.md)。

## 第一條家庭自動化

1. 在 HA 人工建立專用 Toggle helpers：`input_boolean.agentic_motion_test`、`input_boolean.agentic_lamp_test`，確認不被其他規則使用。
2. 在「設備與權限」允許讀取並選「專用測試 helper」，儲存授權。
3. 已完成受控 CLI 登入後，在家庭工作台要求：「motion_test 由 off 變 on 時，開啟 lamp_test，先顯示提案。」
4. 查看精確變更再批准；系統執行備份、寫入、啟用及讀回。於已批准的測試範圍切換 motion，觀察 lamp 的真實結果。
5. 日常規則由 HA 執行，停止 Hermes／Runner 不會停止已部署規則。

此流程的真模型＋真家庭試跑需另記驗收；容器 fixture 通過不等於已替你的家庭完成。

## 診斷、主機管理與恢復

沒有模型或 Web 時先用固定診斷：

```bash
sudo agentic-home diagnostics
sudo agentic-home support-bundle --output ./agentic-support.json
```

它們不需要 Controller 或 broker 可用。bundle 建立新的 0600 JSON，只含允許的 metadata／狀態和 journal priority 計數，不含原始 log、對話或 credentials，也不自動上傳。若需要原始 journal，僅在 local console 查看；不要把未檢查的整份記錄當作可分享 support bundle。

已建立 HAOS 後的固定操作：

```bash
agentic-home status
agentic-home start
agentic-home shutdown
agentic-home snapshot
agentic-home restore --snapshot <完整快照ID>
```

冷快照會正常關閉 guest、複製磁碟及 UEFI，完成後恢復原開機狀態；期間家庭規則暫停。restore 只接受已關機的 VM，完成後維持關機。快照含家庭資料，離機保存需要加密；Controller、Hermes、Runner 資料須各自備份。外層 Web 損壞時的固定 console 路徑見 [OPERATIONS](docs/OPERATIONS.md)。

firstboot 套件或 image 載入失敗由 service 重試，可在 console 檢查後重新啟動 `agentic-home-firstboot`；它不會因此建立 HAOS。HAOS provisioning 失敗則查看固定主機操作紀錄，再對同一受管理 VM 處理，不能用重新安裝覆蓋現場。

## 網路與硬體範圍

- HAOS 使用專用 NAT `192.168.177.0/24`；不自動提供 LAN mDNS／SSDP discovery 或實體網卡 bridge。
- Matter、反向連線、USB dongle 等需要現場確認；不自動認領未知 USB 或修改主機網路。
- laptop profile 設定合蓋行為；散熱、網卡、UEFI 與實際硬體仍需 Owner 現場驗證。
- VM 快照、Core backup、Controller、Hermes memory／認證、Runner profiles 是不同備份層。還原舊 guest 後核對較新的 Controller ledger。
- 頁面、任務與對話可刷新續接；browser storage 只保存 IDs。主機登入到期保留記憶體內未送出草稿，明確登出清除本次畫面／對話 IDs。

參阅 [Linux 架構](docs/LINUX_PRODUCT.md)、[Gateway](docs/GATEWAY.md)、[版本驗證](docs/VERIFICATION.md)、[v3 驗收](docs/v3/PROGRESS.md)。
