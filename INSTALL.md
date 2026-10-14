# 安裝 Agentic Smart Home 0.9.0

## 需要

- Intel／AMD x86-64 電腦，UEFI，開啟 VT-x／AMD-V。建議 16 GB RAM、128 GB SSD；少於約 8 GB RAM 或沒有 KVM 時，安裝會停下並說明原因。
- 網路（安裝和第一次設定都要下載元件）。
- 一支可以清除的 USB。
- 一個 AI 帳號，例如 ChatGPT 訂閱或 API 金鑰。

## 安裝

1. 用 `SHA256SUMS` 核對 `agentic-home-0.9.0-laptop-amd64.iso`。
2. 用 Rufus 或 balenaEtcher 把 ISO 寫入 USB。這會清除 USB。
3. 從 USB 開機，選 **Install Agentic Home Linux**，在 Ubuntu 安裝器選網路、磁碟和帳號。選中的磁碟會被清除；沒有預設密碼，也不會預先選磁碟。
4. 移除 USB 並重新開機。第一次開機會安裝套件並啟動主機服務，需要幾分鐘。想看進度：`journalctl -fu agentic-home-firstboot`。
5. 用剛建立的帳號登入桌面，**Agentic Home** 會自己打開。沒打開時再點一次桌面捷徑。

桌面登入用的是 120 秒、只能用一次的本機票據。其他帳號或遠端瀏覽器用主機金鑰：`sudo agentic-home access-key`。

## 第一次設定

照畫面一步步完成。關掉頁面不會取消；再打開會從原處繼續。

1. **AI 帳號**：選一個（Codex、Claude Code、Antigravity 或 Grok Build），完成官方登入。
2. **智能管家**：用 ChatGPT 訂閱或 OpenRouter 金鑰登入。系統會實際問一次，確認能回覆；這可能用到一點額度。
3. **通訊**：LINE 掃描後選聊天室並傳送配對碼；Telegram 貼上機器人權杖並傳送配對碼；也可以選 Discord，或只用網頁。
4. **家庭**：系統自動建立 Home Assistant。在 Home Assistant 建立家庭和擁有者帳號，回來按 **授權連接家庭**，再在家庭確認授權。
5. 完成後，Home Assistant 側欄會出現 **智能管家**，日常直接用 Home Assistant。桌面的 Agentic Home 保留設定與救援。

智能管家、AI 帳號和開發助手的登入各自保存，互不共用。

## 開發助手

- **主機**：設定 → 開發助手 → 登入開發助手（Codex 裝置碼或 API 金鑰），描述問題後按 **開始調查** 或 **開始修改並測試**。成果依序是發現、獨立基準測試結果和修改內容；工作執行中可以停止，失敗時可以再試一次。
- **家庭現場**：同一頁按 **啟用家庭現場**。系統會把 App 裝進 Home Assistant 並完成配對，之後另外登入一次。你在 Home Assistant 停止或移除它，系統不會自動裝回。

開發助手只修改自己的私有副本，沒有家庭權限，成果不會自動部署。

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

瀏覽器開 `http://localhost:8099`；家庭是 `http://localhost:8123`。兩者都只聽本機。也可以在 **設定 → 主機維護** 登入 Tailscale。

## 已有 Ubuntu 24.04

在**要安裝的那台電腦**執行，不要在開發機執行：

```bash
tar -xf agentic-home-0.9.0-linux-amd64.tar
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

- Home Assistant 用專用 NAT `192.168.177.0/24`，不自動做區網探索或網卡橋接；Matter、USB dongle 需要現場確認。
- 實體筆電的散熱、網卡和 UEFI 需要現場驗證。
- VM 快照、Home Assistant 備份、主機 Controller、智能管家和各 AI 登入是不同的備份層。

架構與驗證：[Linux 架構](docs/LINUX_PRODUCT.md) · [通訊](docs/GATEWAY.md) · [驗證紀錄](docs/VERIFICATION.md)。
