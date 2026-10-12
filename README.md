# Agentic Smart Home · 0.4.0 技術預覽

外層 Linux 先提供 Web、Hermes 協調助手與固定主機診斷；需要時再建立官方 Home Assistant OS 的 KVM 虛擬機。**首次開機不自動建立 HAOS**，沒有家庭帳號、通訊 bot 或內層 worker，也可以先設定外層。

本版開始落實 [Plan v3](docs/v3/README.md)：一個外層 Hermes，Host／Home 各一個 coding worker 的方向不變。目前可使用 Hermes 對話與固定工具；兩個 research／development worker **尚未啟用**。Host 隔離原型、工作契約與 blocked handle 是開發基礎，不能當成 E1 真實修正或 E2 HAOS App 研究已完成。

```text
Web ＋ 可選 LINE／Discord／Telegram（同一個 Gateway）
                        │
外層 Ubuntu Linux       ▼
├── Controller：onboarding、對話身份、case/run、家庭授權與操作
├── Hermes：獨立模型登入、真上游 AIAgent、固定 MCP 工具
├── Runner：進階受控 Codex／Claude／Antigravity／Grok
├── LINE.js：可選的本機 LINE 通訊服務
├── Host broker：Owner 操作固定 HAOS VM／快照
└── 可稍後建立的 KVM → 官方 HAOS
                         ├── 受控 Runner App（另行配對／登入）
                         └── Supervisor／Core → 原生家庭自動化

待完成：隔離 Host coding CLI → 真 HAOS App Home coding → 更新／退版
```

[安裝](INSTALL.md) · [通訊與登入](docs/GATEWAY.md) · [Linux 架構](docs/LINUX_PRODUCT.md) · [Hermes runtime](hermes/README.md) · [驗證](docs/VERIFICATION.md) · [v3 進度](docs/v3/PROGRESS.md)

0.4.0 的 Controller 資料格式為 **schema 2**。開啟 0.3.x 的 schema 1 DB 時會先留私有一致快照，再遷移；舊 binary 不能讀取 schema 2。此功能不是原地部署／降版工具，固定 updater 與 rollback 仍待完成；請依 [恢復說明](docs/OPERATIONS.md) 保留現況，不能用舊 DB 覆蓋新家庭操作。

## 開始使用

1. 建置 Linux bundle／laptop ISO，依 [安裝說明](INSTALL.md) 部署到選定的目標電腦。
2. 開啟 `http://localhost:8099`，使用此主機的專用金鑰登入。
3. 在 onboarding 的「Hermes 登入」填入 OpenRouter 模型 ID 與自己的 API key。Hermes 與既有 coding CLI 分別認證，不自動共用訂閱或切換 provider。
4. 儲存後顯示「已儲存，待實際回覆驗證」。到對話窗口詢問主機狀態；只有實際推論成功才成為「已完成推論驗證」。HAOS、通道和內層登入都可跳過。
5. 日後可設定 LINE／Discord／Telegram；需要家庭功能時，由「Linux 主機」的「建立 HAOS」或 `sudo agentic-home provision` 建立 VM，再完成 HA owner、受控 Runner 與設備授權。

一般對話預設 Hermes；「受控 Agent（進階）」保留原來的 runtime／CLI 選擇與家庭提案流程。Web 可接續或開始新對話；外部通道的 `/new 新需求` 開始新對話，`/host`／`/home` 明確切至受控 Runner。家庭變更仍需 Owner 在 Web 批准，Hermes 沒有 Live approve/apply 工具。

## 現在可用的範圍

Hermes 固定使用上游 commit [`835c3dd…`](https://github.com/NousResearch/hermes-agent/tree/835c3dd038d903182a66ecc45e34bf5e0de65f00)，來源 archive hash、Python 3.14 與依賴見 [lock](hermes/hermes.lock.json)。它在獨立服務中執行真實 `AIAgent`，不啟動第二套 Hermes Gateway。

可見 MCP 工具為 `host.status`、`worker.capabilities`、`worker.submit/status/cancel/result`、`case.evidence.read`。目前 worker 提交持久化為 `blocked`，不執行 code；沒有任意 shell、filesystem、browser、內建 delegate 或家庭批准工具。Host coding 的 provider egress／完整 workspace lifecycle、Home coding 的真 HAOS App 邊界仍需驗證，見 [sandbox foundation](host/worker/README.md)。

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
uv run python -c "import sys; sys.path.insert(0,'scripts'); from package import package_runner; package_runner()"
docker build -t agentic-home-controller:0.4.0 controller
docker build -t agentic-home-runner:0.4.0 runner
docker build -t agentic-home-linejs:0.4.0 host/linejs
docker build -t agentic-home-hermes:0.4.0 hermes
python3 scripts/build_host.py
uv run python scripts/package.py
```

`dist/` 產生 `agentic-home-0.4.0-linux-amd64.tar`、source ZIP 與 `SHA256SUMS`。內部服務名稱保留 `agentic-home`；大型 TAR／ISO 不存入 Git。產生 laptop 安裝媒體：

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
