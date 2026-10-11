# Agentic Smart Home · 0.3.1

從外層 Linux 開始的家庭 Agent。先啟動 Web onboarding、登入 Agent，再選擇 LINE、Discord 或 Telegram 作為通訊入口。Home Assistant OS 在受管理的 KVM 虛擬機裡執行，內外兩層各自擁有獨立的 Agent 登入。

**HAOS 尚未就緒或關機時，外層 Web、Agent 對話與主機管理仍可使用。** 所有訊息經過同一個外層 Controller，家庭變更會先產生具體提案，再由家庭 owner 在 Web 批准。

```text
Web（固定入口）＋ LINE（預設選項）／Discord／Telegram
                         │
外層 Ubuntu Linux        ▼
├── Controller：onboarding、統一對話、授權與操作紀錄
├── 外層 Runner：Codex / Claude Code / Antigravity / Grok Build
├── LINE.js：本機通訊服務，登入與聊天室白名單
├── Host broker：HAOS 建立、啟停、冷快照與還原
└── KVM → Home Assistant OS
          ├── 內層 Runner：四種 CLI，各自重新登入
          └── Supervisor / Core → 家庭设备與原生自動化
```

[安裝說明](INSTALL.md) · [通訊與雙層 Agent](docs/GATEWAY.md) · [Linux 架構](docs/LINUX_PRODUCT.md) · [驗證紀錄](docs/VERIFICATION.md)

## 開始使用

1. 建置下方 Linux bundle 或 ISO，依安裝說明部署到目標電腦。
2. 開啟 `http://localhost:8099`，使用該主機專用金鑰登入。
3. 跟隨 onboarding：Web → 外層 Agent → 通訊入口 → HAOS 與內層 Agent → 對話。
4. LINE 掃碼登入後，選擇聊天室並填入允許使用者；也可先只使用 Web。
5. 內層 Runner 從本 repository 安裝為 HAOS App，與外層 Controller 配對，再獨立登入 CLI。

Web 可選擇執行環境與 CLI；外部通道可用 `/host 需求` 或 `/home 需求` 指定外層／內層。不帶指令時使用 Web 設定的預設環境與 CLI。對話可接續前文；Web 按「開始新對話」，外部通道傳 `/new 新需求` 即可重新開始。

## Agent 與通訊支援

| Agent | 鎖定版本 | 登入方式 |
|---|---|---|
| Codex | 0.157.1 | 官方 device code、API key |
| Claude Code | 2.1.296 | API key、官方 setup token |
| Antigravity (`agy`) | 1.3.3 | Gemini API key；Google 訂閱登入尚未整合 |
| Grok Build (`grok`) | 1.0.50 | 官方 device code、xAI API key |

CLI 在不同 UID 與私有 profile 執行，沒有 HA 管理權杖。Controller 驗證結構化意圖後才產生家庭提案；模型不能直接执行 shell 或任意設備指令。

LINE 使用鎖定版本的 [JS-PACKAGE/LINE.js](https://github.com/JS-PACKAGE/LINE.js)，屬於非官方 LINE 協定。Discord 使用專用 bot 與 REST polling；Telegram 使用專用 bot 與 long polling。聊天室和使用者必須同時符合白名單。重新啟動不重播舊對話，也不盲目重送未確認的回覆。

## 建置與測試

需要 Linux amd64、Python 3.13、Node 24 與 Docker。主產品為 Ubuntu 24.04 profiles；Ubuntu 26.04 是尚未完成整機驗證的擴充 recipe。

```bash
uv sync --frozen
npm ci --prefix web
npm run build --prefix web
uv run python scripts/contracts.py --check
uv run python -c "import sys; sys.path.insert(0,'scripts'); from package import package_runner; package_runner()"
docker build -t agentic-home-controller:0.3.1 controller
docker build -t agentic-home-runner:0.3.1 runner
docker build -t agentic-home-linejs:0.3.1 host/linejs
python3 scripts/build_host.py
uv run python scripts/package.py
```

`dist/` 產生 `agentic-home-0.3.1-linux-amd64.tar`、source ZIP 與 SHA256SUMS。內部服務名稱保留 `agentic-home`，避免改變既有資料路徑。原始碼公開於本 repo；大型安裝媒體由上述命令建置，不存入 Git。

需要可開機安裝媒體時：

```bash
docker build -f host/Builder.Dockerfile -t agentic-home-builder:0.2.0 host
python3 scripts/build_host.py --profile laptop-amd64 --iso --reuse-bundle
python3 scripts/build_host.py --profile server-amd64 --iso --reuse-bundle
```

建置只寫入 `build/` 與 `dist/`。安裝器的 `--apply` 僅應在要部署的主機執行。

測試使用磁碟暫存目錄，不能使用 `/dev/shm`。有 wrapper 時執行 `pytest-disktmp uv run pytest -q --real-ha --containers`；一般開發環境可用：

```bash
mkdir -p "$HOME/agent-work/tmp/pytest"
agentic_test_tmp=$(mktemp -d "$HOME/agent-work/tmp/pytest/agentic-home-XXXXXX")
trap 'rm -rf -- "$agentic_test_tmp"' EXIT
uv run pytest -q --basetemp "$agentic_test_tmp"
```

這個版本的自動測試使用本機協定 fixtures 與隔離容器。真實雲端帳號、LINE／Discord／Telegram 收發及新版實機安裝尚未驗證；歷史 VM 安裝證據與本版結果分開記錄。
