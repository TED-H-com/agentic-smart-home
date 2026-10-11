# 安裝 Agentic Smart Home

產品從外層 Linux 開始。你可以使用 Linux 桌面、瀏覽器與終端；Agentic Home 在外層運行，自動建立內層 HAOS，不必先手動安裝 HAOS 或兩個 HA Apps。

本版安裝媒體可依 [README 建置步驟](README.md) 產生；Git repository 不包含大型 TAR／ISO。0.3.1 沒有重跑完整 Linux 重灌驗證，歷史平台證據見 [驗證紀錄](docs/VERIFICATION.md)。

## A. 整台重灌

1. 取得 `agentic-home-0.3.1-laptop-amd64.iso` 與 `SHA256SUMS`，比對 SHA-256。
2. 備份筆電資料，用 Rufus／Etcher 將 ISO 寫入 USB。這會清除選中的 USB；後續安裝器選中的系統磁碟也會重新分割。
3. Intel／AMD x86-64 電腦使用 UEFI，開啟 VT-x／AMD-V，從 USB 開機。建議 16 GB RAM／128 GB SSD；profile 要求約 8 GB RAM，安裝時需要網路。
4. 選 **Install Agentic Home Linux**，在官方 Ubuntu 安裝器設定網路、目標磁碟與 Linux 帳號。沒有預設密碼；請確認選中要重灌的磁碟。
5. 移除 USB 並重啟。首次開機自動安裝桌面與主機套件、載入包內 Agent images、建立並啟動 HAOS。可在終端查看進度：

   ```bash
   journalctl -fu agentic-home-firstboot
   ```

6. 預設主機擁有者是安裝器建立的 UID 1000；其他一般 Linux 帳號不會收到管理金鑰。登入 Linux 桌面，開啟 **Agentic Home**：`http://localhost:8099`。家目錄的 `Agentic-Home.txt` 有此主機專用登入金鑰；也可執行：

   ```bash
   sudo agentic-home access-key
   ```

7. 登入後即進入「首次設定」。先在「外層 Agent」登入 Codex、Claude、Antigravity 或 Grok；這一步不需要 HAOS 已啟動。安裝器會配對外層 Runner，若尚未配對可按「連接外層 Runner」。
8. 選擇通訊入口。Web 一直可用；預設提供 LINE 掃碼登入與聊天室選擇。填入聊天室和允許使用者 ID 才啟用接收。Discord／Telegram 改用專用 bot token 與相同的雙重白名單。
9. 在「連接 HAOS」開啟 `http://localhost:8123`，完成 HA 家庭帳號。到 HA 個人資料 → 安全性建立長效存取權杖，貼進「連接你的家庭」。Controller 驗證 active owner 後保存，不交給模型。
10. 在 HA App 商店加入 `https://github.com/teddashh/agentic-smart-home`，安裝並啟動 **Agentic Home Runner**，保留網路連接埠 8443。複製記錄中的一次性配對 JSON，在外層 onboarding 的「HAOS 內層 Agent」貼上配對，再獨立登入 CLI。離線來源安裝可改用介面提供的 Runner ZIP，解壓至 `/addons/agentic_runner` 後重新載入本機 Apps。
11. 到「設備與權限」選擇分享與操作範圍並儲存。統一對話選「HAOS 內層」，或外部通訊輸入 `/home 需求`，家庭提案會留在 Web 等待 owner 批准。詳見 [通訊與双層登入](docs/GATEWAY.md)。

## B. 無桌面主機

使用 `agentic-home-0.3.1-server-amd64.iso`；步驟相同，省略桌面套件。從另一台電腦連線：

```bash
ssh -L 8099:127.0.0.1:8099 -L 8123:127.0.0.1:8123 your-user@your-host
sudo agentic-home access-key
```

在操作電腦開啟 `http://localhost:8099`、`http://localhost:8123`。兩個 listener 預設只綁 loopback；操作電腦上的相同 ports 需可用。SSH 使用安裝時建立的 Linux 帳號。

## C. 已有相符的 Ubuntu

保留既有磁碟分割，安裝系統套件、服務、產品資料及 KVM VM。在要部署的主機執行：

```bash
tar -xf agentic-home-0.3.1-linux-amd64.tar
cd agentic-home
python3 host/install.py --profile laptop-amd64
sudo python3 host/install.py --profile laptop-amd64 --apply
```

第一條命令只顯示安裝內容。Ubuntu 24.04 可選 laptop-amd64／server-amd64；Ubuntu 26.04 對應 laptop-new-amd64，後者尚無整機驗證證據。OS version、架構、KVM、RAM、空間及包內雜湊均會檢查；既有 /opt/agentic-home 不會被另一份新安裝包直接覆蓋。

## 第一條家庭自動化

1. 在 HA 建立專用 Toggle helpers：`input_boolean.agentic_motion_test`、`input_boolean.agentic_lamp_test`。
2. 「設備與權限」允許讀取，選「專用測試 helper」，並儲存授權。
3. 工作台輸入：「motion_test 由 off 變 on 時，開啟 lamp_test，先顯示提案。」
4. 查看並批准，系統完成備份、寫入、啟用及讀回；切換 motion，確認 lamp 真正變成 on。
5. 日常規則由 HA 執行；外層 Agent 停機也不需要重新推理。

## 主機管理

```bash
sudo agentic-home status
sudo agentic-home start
sudo agentic-home shutdown
sudo agentic-home snapshot
sudo agentic-home restore --snapshot <完整快照ID>
```

UI 提供相同操作。冷快照正常關閉 HAOS 後複製磁碟與 UEFI，再恢復原開機狀態；期間家庭自動化暫停。restore 只接受已關機 VM，還原後保持關機。快照含家庭資料，離機保存時要加密；外層 Agent 另行備份。

下載失敗會由 firstboot service 重試，既有受管理 VM 不會重新格式化：

```bash
journalctl -u agentic-home-firstboot -u agentic-home-host
sudo systemctl restart agentic-home-firstboot
```

## 網路與硬體範圍

- 專用 NAT 子網 192.168.177.0/24；HAOS 可連網及依 IP 存取設備。既有 route 重疊時不建立 VM。
- NAT 不提供 LAN 的 mDNS／SSDP discovery；Matter 與部分反向連線整合需額外橋接設定。本版不自動改實體網卡設定。
- USB dongle passthrough 可由管理者透過原生 libvirt 指定；不自動認領未知 USB。
- laptop profile 讓 logind 忽略合蓋；GNOME 電源管理和實際網卡／散熱仍依你的硬體設定。
- VM 冷快照、Core 備份、外層 Agent 資料是三個層次。HAOS 還原後，Controller 較新的紀錄必須 reconcile。

參閱 [架構](docs/LINUX_PRODUCT.md)、[驗證](docs/VERIFICATION.md)、[恢復](docs/OPERATIONS.md)。

## 使用中的連續性

- 各頁與家庭任務都有網址，重新整理仍回到同一頁或任務。
- 主機登入到期會顯示登入頁；重新登入後保留本頁尚未送出的家庭需求。手動登出會清除本次工作畫面。
- 官方 device-code 登入跨頁會續接相同登入工作；不用再建立一組代碼。
- 設備授權尚未儲存時，離開頁面會提示；重新盤點會等修改儲存後才開放。
- 主機啟停、快照、還原採背景工作。可以離開頁面，稍後從「主機操作紀錄」查看真實結果。
- 外層模式的 HA 設定連結會開啟 localhost:8123 的新分頁，管理中心留在原頁。
