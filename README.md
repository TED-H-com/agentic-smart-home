# Agentic Smart Home · 0.7.0 技術預覽

從開機一路引導到家庭設定：**Linux／網路 → 選 coding CLI 並登入 → 準備隨版本交付的 Hermes → LINE 或其他通道 → 自動啟動 HAOS → HA 原生設定**。程式保存進度並自動執行固定步驟，遇到帳號授權才等待使用者；可選擇先用 Web／外層助手。

本版沿用 [Plan v3](docs/v3/README.md) 的一個外層 Hermes、Host／Home 各一個 coding worker。已接通獨立 Codex Host coding、檔案差異、獨立 verifier、隔離 PNG 預覽與停止後的接手檢查點；Hermes 可提出精確家庭提案，批准仍由 Owner 完成。Home coding 尚未啟用，真 HAOS App 資源隔離已有平台證據，完整執行器仍待整合；真帳號與家庭完整 E1／E2 驗收不由工程 fixtures 代替。

完成設定後，日常入口進 **Home Assistant 原生家庭畫面**；需要協助時開啟 **智能管家**，安裝及救援留在外層 Web。明確對話／提案連結不跳走，HA 斷線仍可回到外層。[上游相容邊界](docs/ADAPTERS.md) 說明各 adapter、候選測試與更新限制；0.7.0 新增薄 HAOS Ingress App，在 HA 側欄延續同一外層對話；不修改 HA／Supervisor 原始介面。

```text
Web ＋ 可選 LINE／Discord／Telegram（同一個 Gateway）
                        │
外層 Ubuntu Linux       ▼
├── Controller：onboarding、對話身份、case/run、家庭授權與操作
├── Hermes：獨立模型登入、真上游 AIAgent、固定 MCP 工具
├── Runner：進階受控 Codex／Claude／Antigravity／Grok
├── LINE.js：可選的本機 LINE 通訊服務
├── Host broker：隔離 coding、固定 HAOS VM／快照、預覽與更新
└── 可稍後建立的 KVM → 官方 HAOS
                         ├── 智能管家 Ingress App → 同一外層 Controller／Hermes
                         ├── 受控 Runner App（另行配對／登入）
                         └── Supervisor／Core → 原生家庭自動化

Host coding：私有 Codex → diff → 獨立 verifier → PNG 預覽／停止後檢查點
Home coding：App 資源隔離已探測，完整 coding 執行器尚未啟用
```

[安裝](INSTALL.md) · [通訊與登入](docs/GATEWAY.md) · [Linux 架構](docs/LINUX_PRODUCT.md) · [Hermes runtime](hermes/README.md) · [驗證](docs/VERIFICATION.md) · [原生入口／相容審查](docs/reviews/2026-10-12-NATIVE-HOME-ADAPTERS.md) · [v3 進度](docs/v3/PROGRESS.md)

0.7.0 的 Controller 資料格式為 **schema 3**；從 1／2 開啟時先保存私有一致快照，再向前遷移。固定 updater 僅允許可信簽章、相同 schema 3 的 Controller image 更新／退版，保留現有 Live DB。不能直接退回 schema 2 的 0.5.x；詳見 [更新](docs/UPDATES.md) 與 [恢復](docs/OPERATIONS.md)。

## 開始使用

1. 依 [安裝說明](INSTALL.md) 從 USB 安裝 Linux，選擇網路、磁碟與 Linux 帳號。
2. 登入桌面，**Agentic Home** 自動開啟並使用短效一次性本機授權進入 Web。
3. 選 Codex／Claude／Antigravity／Grok 與通訊入口，開始設定；完成所選 CLI 支援的官方登入。
4. CLI 讀取固定設定觀測，程式驗證隨本產品版本交付的 Hermes，按鎖定 digest 啟動。完成 Hermes 的獨立 Codex OAuth 或 OpenRouter 設定後，自動驗證一次模型回覆。
5. LINE 掃碼後選聊天室並輸入配對碼；Telegram 貼上 BotFather token 並傳送配對碼。也可明確選 Web 略過外部通道。
6. 流程自動建立／啟動 HAOS。完成原生家庭帳號設定後，按官方授權連接家庭，不需手動複製 HA 長效權杖。
7. 新設定完成後自動安裝智能管家側欄，進入原生 Home Assistant；已有新版 Host 的安裝可在外層設定啟用。桌面的 Agentic Home 保留救援與維護返回路徑。

詳細行為、仍需人的步驟與中斷恢復見 [自動 onboarding](docs/ONBOARDING.md)。
一般對話預設 Hermes；「受控 Agent（進階）」保留原來的 runtime／CLI 選擇與家庭提案流程。Web 可接續或開始新對話；外部通道的 `/new 新需求` 開始新對話，`/host`／`/home` 明確切至受控 Runner。家庭變更仍需 Owner 在 Web 批准，Hermes 沒有 Live approve/apply 工具。

## 現在可用的範圍

依 [發行與上游追版](docs/RELEASE_POLICY.md)，HA 保留官方更新；外層只檢查並有限次修復已授權的智能管家 App。手動停止、移除或改設定會停止自動修復。外層 CLI／Hermes 隨產品釘版，固定在線 updater 目前仍限 Controller；原生 Assist／語音尚未整合。

側欄會確認 Host 已具備相應更新保護；舊 0.6.x Host 只升 Controller 不能啟用它。0.7.0 新安裝媒體包含完整保護，舊 Host 的受控就地升級、App 跨版本更新與退版仍待交付，詳見 [更新邊界](docs/UPDATES.md)。

安裝包的 Hermes 使用上游 commit [`835c3dd…`](https://github.com/NousResearch/hermes-agent/tree/835c3dd038d903182a66ecc45e34bf5e0de65f00)，來源 archive hash、Python 3.14 與依賴見 [lock](hermes/hermes.lock.json)。首次設定直接驗證與啟用隨附 image digest，不在家庭機器追 HEAD 或重新建置；開發端另有候選追版工具。實際 commit／digest 可追蹤，之後重啟維持同一映像。它在獨立服務中執行真實 `AIAgent`，不啟動第二套 Hermes Gateway。

MCP 可查 Host、worker capabilities、派工／取消／讀結果與 case 證據，也可使用 `home.observe`、`home.propose`、`home.proposal_status` 走既有家庭 Engine。沒有任意主機 shell 或 approve/apply 工具。隔離 coding 目前明確支援 Codex；其他 CLI 的家庭模式與初始化支援保持不變。使用與邊界見 [Workers](docs/WORKERS.md)，固定更新與網路見 [Updates](docs/UPDATES.md)。

進階受控 Runner 維持以下鎖定 CLI，各自用私有 profile；這些受限家庭模式不是已完成的通用 coding worker。

| CLI | 版本 | 登入 |
|---|---|---|
| Codex | 0.157.1 | 官方 device code／API key |
| Claude Code | 2.1.296 | API key／官方 setup token |
| Antigravity (`agy`) | 1.3.3 | Gemini API key；Google 訂閱登入未整合 |
| Grok Build (`grok`) | 1.0.50 | 官方 device code／xAI API key |

通訊仍沿用鎖定的 [LINE.js](https://github.com/JS-PACKAGE/LINE.js)、Discord REST polling 與 Telegram long polling；聊天室及使用者需同時在白名單。LINE.js 使用非官方 LINE 協定，帳號與真實收發結果需另驗。

## 建置與測試

建置環境為 Linux amd64、Python 3.13、Node 24、Docker。Hermes 的 Python 3.14 runtime 與依賴在自己的 Docker image 內，無需改 Controller／Runner 的 Python。

```bash
uv sync --frozen
npm ci --prefix web
npm run build --prefix web
uv run python scripts/contracts.py --check
uv run python -c "import sys; sys.path.insert(0,'scripts'); from package import package_runner, package_bridge; package_runner(); package_bridge()"
docker build -t agentic-home-controller:0.7.0 controller
docker build -t agentic-home-runner:0.7.0 runner
docker build -t agentic-home-linejs:0.7.0 host/linejs
docker build -t agentic-home-hermes:0.7.0 hermes
docker build -t agentic-home-preview:0.7.0 host/preview
docker build -t agentic-home-bridge:0.7.0 bridge
python3 scripts/build_host.py
uv run python scripts/package.py
```

產品來源須先提交到本 public repo；建置會將精確 HEAD 的 Git bundle 放入安裝包，拒絕未提交的產品程式，以免 worker 研究到另一個版本。

`dist/` 產生 `agentic-home-0.7.0-linux-amd64.tar`、source ZIP 與 `SHA256SUMS`。內部服務名稱保留 `agentic-home`；大型 TAR／ISO 不存入 Git。產生 laptop 安裝媒體：

```bash
docker build -f host/Builder.Dockerfile -t agentic-home-builder:0.2.0 host
python3 scripts/build_host.py --profile laptop-amd64 --iso --reuse-bundle
```

既有 server profile 可用同一命令的 `--profile server-amd64`。首條驗收路徑仍是 Ubuntu 24.04 的 laptop-amd64；26.04 recipe 與其他 profile 不因可建置就視為已驗證。建置只写入 `build/`／`dist/`，不在開發機執行 installer `--apply`。

pytest 需磁碟暫存，禁止 `/dev/shm` 或任何 tmpfs：

```bash
pytest-disktmp uv run pytest -q --real-ha --containers
```

依賴／測試容器準備及無 wrapper 環境的等效命令見 [開發說明](docs/DEVELOPMENT.md)。本版測試把真上游 binary、模型 protocol fixture、真 Core、Docker 與 browser 分開記；真帳號、Owner laptop、Home coding 與更新／退版的產品验收見 [32 項帳本](docs/v3/PROGRESS.md)，不能由既有測試自動推定完成。

Web 或模型無法使用時，local console 的 `sudo agentic-home diagnostics` 和 `sudo agentic-home support-bundle --output ./agentic-support.json` 仍能執行固定去密診斷，詳見 [恢復說明](docs/OPERATIONS.md)。
