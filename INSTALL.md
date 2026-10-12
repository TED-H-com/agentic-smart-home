# 安裝 Agentic Smart Home 0.4.0 技術預覽

產品從外層 Linux 開始，先提供 Web、Hermes 和固定診斷。**本版 firstboot 不自動建立或啟動 HAOS**；完成外層設定後，你可隨時透過固定主機操作建立家庭 VM。沒有 Home Assistant、LINE 或內層 worker，不阻擋外層登入與使用。

本版媒體依 [README](README.md) 建置，Git repository 不包含大型 TAR／ISO。候選 hash、一次性 VM 與硬體測試的實際結果以 [驗證紀錄](docs/VERIFICATION.md) 為準；歷史安裝結果不自動算成本候選驗收。這是 v3 技術預覽，兩個 coding worker 仍未啟用。

## A. 整台重灌

1. 取得 `agentic-home-0.4.0-laptop-amd64.iso` 與 `SHA256SUMS`，核對 SHA-256。
2. 備份目標電腦資料，用 Rufus／Etcher 將 ISO 寫入 USB。這會清除選中的 USB；後續選中的系統磁碟也會重新分割。
3. Intel／AMD x86-64 使用 UEFI，開啟 VT-x／AMD-V，從 USB 開機。建議 16 GB RAM／128 GB SSD；laptop profile 檢查約 8 GB RAM、KVM 和可用空間，安裝需要網路。暫不建立 HAOS 不會取消這些主機規格檢查。
4. 選 **Install Agentic Home Linux**，在官方 Ubuntu 安裝器設定網路、目標磁碟與 Linux 帳號。沒有預設密碼或預選磁碟。
5. 移除 USB 並重啟。firstboot 安裝套件、載入四個產品 images、啟動外層服務並配對受控 Runner。此時 HAOS 尚未建立。進度可在 local console 查看：

   ```bash
   journalctl -fu agentic-home-firstboot
   ```

6. 預設主機擁有者是安裝器建立的 UID 1000；其他一般帳號不收到管理金鑰。登入 Linux 桌面，開啟 **Agentic Home**：`http://localhost:8099`。家目錄的 `Agentic-Home.txt` 有此主機專用登入金鑰，也可執行：

   ```bash
   sudo agentic-home access-key
   ```

7. Web onboarding 第二步是 **Hermes 登入**：填入 OpenRouter 模型 ID 與自己的 API key。儲存只驗證表單並保存設定，狀態為「已儲存，待實際回覆驗證」；第一次實際模型推論成功後才顯示已驗證。提問可能使用供應商額度。Hermes 不共用 Codex／Claude／Antigravity／Grok 的登入。
8. 可直接按「保留進度，先開啟 Web 對話」，問「請讀取這台主機的狀態」。初次未登入顯示 `needs_auth`；服務與 HA 狀態分別呈現，不把 HA 未建立當成 Hermes 故障。
9. 通訊入口可稍後設定：Web 一直可用，LINE 是預設呈現選項。啟用 LINE／Discord／Telegram 之前需要自己的帳號／bot、聊天室及允許使用者的雙重白名單；沒有這些資料可保留 Web。

需要既有受控 CLI 時，展開 onboarding 的「進階：受控 CLI 登入」，或到「Agent 環境」。這條路徑提供原有結構化家庭／主機問答；不是通用 research/development worker。

## B. 稍後加入 Home Assistant

1. 在「連接 HAOS」或「Linux 主機」按 **建立 HAOS**；也可在 local console 執行：

   ```bash
   sudo agentic-home provision
   ```

   固定 broker 建立並啟動產品管理的 VM。它不認領同名的外部 VM／磁碟；重疊路由會拒絕建立。首次 guest 啟動可能下載元件，可以離開頁面稍後查看操作紀錄。
2. 開啟 `http://localhost:8123`，完成 HA 原生家庭與管理者帳號。在 HA 個人資料 → 安全性建立長效權杖，貼進「連接你的家庭」。Controller 驗證 active owner 後保存，不交給 Hermes 或 Runner。
3. 在 HA App 商店加入 `https://github.com/teddashh/agentic-smart-home`，安裝 **Agentic Home Runner**、保留 8443。將記錄中的一次性配對 JSON 貼至外層 Web 的 HAOS 內層登入區，再獨立登入 CLI。本機來源可用 Web 提供的 Runner ZIP，解壓到 `/addons/agentic_runner` 後重新載入本機 Apps。
4. 到「設備與權限」選擇分享與操作範圍。一般對話仍是 Hermes；目前家庭提案走「受控 Agent（進階）」→「HAOS 內層」，或通道的 `/home 需求`。精確提案留在 Web 等待 Owner 批准。

上述 App 是保留的受控 `home_operator`；v3 的 Home Coding Worker 還需要獨立 HAOS App sandbox 與真實研究／測試證據，不能把配對成功算成完成 E2。

## C. 無桌面或已有相符 Ubuntu

既有 server profile 可建置 `agentic-home-0.4.0-server-amd64.iso`，省略桌面套件。另一台電腦使用 SSH tunnel：

```bash
ssh -L 8099:127.0.0.1:8099 -L 8123:127.0.0.1:8123 your-user@your-host
sudo agentic-home access-key
```

瀏覽器開啟本機 `http://localhost:8099`。HAOS 建立後才使用 `http://localhost:8123`。兩個 listener 都只綁 loopback；不轉送 Hermes 私有 8096 或 MCP 8097 給瀏覽器。

已有相符的 Ubuntu，可保留分割並安裝產品。在**選定的目標 Linux**執行，不能在開發主機套用：

```bash
tar -xf agentic-home-0.4.0-linux-amd64.tar
cd agentic-home
python3 host/install.py --profile laptop-amd64
sudo python3 host/install.py --profile laptop-amd64 --apply
```

不含 `--apply` 的命令只顯示安裝內容。Ubuntu 24.04 可選 laptop-amd64／server-amd64；26.04 的 laptop-new-amd64 仍是擴充 recipe。安裝檢查版本、架構、KVM、RAM、空間與檔案 hash；既有 `/opt/agentic-home` 不會被另一份安裝包直接覆蓋。v3 固定更新／退版是後續交付，這個安裝命令不是原地升級器。

Controller 0.4.0 新建 DB 使用 schema 2。若開啟 0.3.x schema 1 的既有資料，會先建立私有 `.before-v2-<uuid>.sqlite3` 一致快照，再向前遷移；雖保留既有 tables／紀錄，新資料語意仍不相容於舊版，0.3.x binary 會拒絕 schema 2。沒有提供原地降版保證，不能手改版本 marker 或拿遷移前 DB 蓋掉後續家庭操作。部署變更與資料遷移須分開處理，詳見 [OPERATIONS](docs/OPERATIONS.md)。

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
sudo agentic-home status
sudo agentic-home start
sudo agentic-home shutdown
sudo agentic-home snapshot
sudo agentic-home restore --snapshot <完整快照ID>
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
