# 開發與自動化測試 · 0.9.0

先讀根 [AGENTS](../AGENTS.md)、[Plan](../PROJECT_PLAN.md) 和 [v3 原稿／進度](v3/README.md)。A Hermes/Controller、B worker isolation、C Web/preview/delivery 三線可並行，共用契約由整合者維護，同一檔案／workspace 維持一個 writer。工程測試和 32 個產品驗收分開記。

## 重現環境與建置

Linux amd64、Python 3.13、Node 24、Docker。Controller／受控 Runner 使用既有依賴；Hermes image 自帶 Python 3.14 和独立 lock，不將上游 Agent framework 裝入 Controller。來源 commit/archive SHA、base digest 及依賴見 [hermes.lock.json](../hermes/hermes.lock.json) 和 [Hermes runtime](../hermes/README.md)。

```bash
uv sync --frozen
npm ci --prefix web
npm run build --prefix web
uv run playwright install --with-deps chromium
uv run python -c "import sys; sys.path.insert(0,'scripts'); from package import package_runner, package_bridge; package_runner(); package_bridge()"
uv run python scripts/controller_source.py
docker build -t agentic-home-controller:0.9.0 controller
docker build -t agentic-home-runner:0.9.0 runner
docker build -t agentic-home-linejs:0.9.0 host/linejs
docker build -t agentic-home-hermes:0.9.0 hermes
docker build -t agentic-home-preview:0.9.0 host/preview
docker build -t agentic-home-bridge:0.9.0 bridge
docker pull ghcr.io/home-assistant/amd64-hassio-supervisor:2026.09.3
```

`python3 scripts/worker_runtime.py --check` 核對 Runner 隨附的共用 Host 程式；修改 Host 原件後用不含 `--check` 的命令更新，避免維護第二份邏輯。Home App 的研究來源由 `runner/coding-source.lock.json` 固定公開 commit，在 Docker build 時取得並驗證。

`npm run build` 會重建 static 目錄；建 Controller image 前重新執行 `package_runner()`，保留 UI 的 Runner ZIP。完整 build/package 順序見 [README](../README.md)。

## 磁碟暫存與測試範圍

所有 pytest 經 `pytest-disktmp`，禁止 `/dev/shm`／其他 tmpfs。未安裝 wrapper 時使用等效唯一磁碟目錄並清理：

```bash
mkdir -p "$HOME/agent-work/tmp/pytest"
agentic_test_tmp=$(mktemp -d "$HOME/agent-work/tmp/pytest/agentic-home-XXXXXX")
trap 'rm -rf -- "$agentic_test_tmp"' EXIT
uv run pytest -q --basetemp "$agentic_test_tmp"
```

| 範圍 | 命令（均加 `pytest-disktmp uv run pytest -q` 前綴） | 實際環境與限制 |
|---|---|---|
| 家庭 Engine/API/Runner | `tests/test_engine.py tests/test_api.py tests/test_runner.py` | HA/provider 協定 fixtures |
| Hermes Controller/MCP | `tests/test_hermes_controller.py` | 身份、scope、blocked jobs、固定工具、去重／撤權；runtime fixture |
| Hermes 私有 runtime | `tests/test_hermes_runtime.py --containers` | service tests＋真 pinned AIAgent／官方 MCP client；模型及 MCP endpoints 為本機 fixture |
| Web | `tests/test_ui.py tests/test_bootstrap_ui.py tests/test_gateway_ui.py tests/test_hermes_ui.py tests/test_worker_ui.py tests/test_home_worker_ui.py tests/test_bridge_ui.py tests/test_recovery_ui.py tests/test_review_ui.py tests/test_provider_auth_ui.py tests/test_interactive_preview_ui.py` | 真 Chromium、API fixtures，1440／768／390 px |
| 首次設定／登入 | `tests/test_bootstrap.py tests/test_bootstrap_host.py tests/test_bootstrap_runner.py tests/test_desktop_login.py tests/test_ha_oauth.py --containers --real-ha` | 持久步驟／負向恢復、真封裝CLI本機推論fixture、真Core OAuth；不登入雲端 |
| 跨服務 onboarding E2E | `tests/test_onboarding_e2e.py --containers --real-ha` | 真 Chromium／Controller／TLS Runner／Codex／Hermes AIAgent／MCP／Core OAuth；模型與 Host 安裝副作用為 fixture，無瀏覽器 API 攔截；精確限制見測試與審查報告 |
| 競爭與恢復回歸 | `tests/test_review_bootstrap.py tests/test_review_runtime.py tests/test_review_ui.py` | 真程序樹取消、配對回覆遺失、授權／pause race、登入表單狀態、Telegram 首次配對；外部帳號端點 fixture |
| 固定救援 | `tests/test_support.py tests/test_host.py` | 去密／固定 probe fixtures、真子程序 timeout/output bounds，無家庭寫入 |
| Coding 隔離基礎 | `tests/test_coding_sandbox.py --containers` | 真 Docker 的 network／process／resource negatives；不等於真 cloud CLI、完整 ext4 lifecycle 或 HAOS App |
| 智能管家 App | `tests/test_bridge_app.py tests/test_bridge_controller.py tests/test_bridge_maintenance.py tests/test_bridge_ui.py` | transport／Owner／CSRF／同 case 與復原，API／模型 fixtures；真 HAOS 另跑 `tests/test_bridge_haos.py --haos-vm` |
| Home coding App | `tests/test_home_coding_service.py tests/test_home_sandbox.py tests/test_home_worker_haos.py`；真平台另加 `--haos-vm` | 單元／故障恢復與一次性官方 HAOS；真 Codex 工具、磁碟配額、cgroup、固定模型 transport，不含真雲端帳號 |
| Core | `tests/test_real_ha.py --real-ha` | 真官方 Core；Supervisor backup 與 Agent intent 仍有 fixture |
| 受控 Runner／Supervisor | `tests/test_container.py --containers` | 真 CLI、TLS、UID、schema／backup filter；模型為本機 fixture |

先完成 image 建置與媒體匯出，再跑容器回歸；不要讓 `docker save`／ISO 打包與限時 Docker create 測試並行。所有測試結束後，再執行一次性 Linux 安裝案例。

完整候選回歸用 `pytest-disktmp uv run pytest -q --real-ha --containers`。真 Core 測試使用 2026.10.1；測試容器用唯一名稱和磁碟 temp，必須在 finally 停止／移除。不得讀開發機 credentials 或偷偷呼叫付費 inference。

[Worker 說明](WORKERS.md) 列出 Host 實際 CLI、role credentials、egress、固定 verifier、預覽與 HAOS App gate。真 ext4 quota／完整安裝生命週期只在一次性 VM 驗證，不在開發 host 掛載測試 ext4 或執行 installer。

## 契約、來源與包裝

```bash
uv run python scripts/contracts.py
uv run python scripts/contracts.py --check
uv run ruff check controller runner hermes bridge host scripts tests
uv run python scripts/validate_kit.py
npm run build --prefix web
python3 scripts/build_host.py
uv run python scripts/package.py
```

先改 Pydantic models 再產生 contracts。Runner `protocol=1` 與 `work_contract_major=1` 分開。Controller 使用 schema 3，因舊 worker cancel 語意不兼容新 lease；從 1／2 開啟先留 0600 SQLite 一致快照，交易更新 marker，保留 audit／去重。restore 接受 1／2／3 並保持格式。固定 updater 只允許同 schema 3 Controller image；不盲目回復舊 Live DB。

Hermes 工具 allowlist 由 runtime 和 Controller 同時限制，沒有 Live approval、arbitrary shell 或原生 delegate；新的 research/development launcher 不可修改受控 `home_operator` 的工具限制。模型回傳 passed／approved 沒有測試或人類批准效力。

更新 provider 須同步 locks、runtime versions、probe；更新 Hermes 須同步 commit、archive hash、依賴 lock、真 AIAgent/MCP 協定測試及權限負向測試。不要使用未審核的 provider fallback 或未知工具集合。

source ZIP 明列允许目錄；不含 `_upstream`、`.venv`、node_modules、credentials、私人家庭資料或 live workspaces。每 ZIP 有 `MANIFEST.sha256.json`，外部有 `dist/SHA256SUMS`；編譯 static 刻意隨產品打包。Linux bundle 含五 images 及 `images.lock.json`／`BUNDLE.sha256.json`。六份 v3 steering 原稿保持 byte-for-byte，工程進度写到 companion 文件。

## 媒體、相容與真實驗收

每個版本的媒體、一次性 VM 安裝與平台測試結果記在 [VERIFICATION](VERIFICATION.md)。

本輪首要 profile 是 Ubuntu 24.04 `laptop-amd64`。firstboot 只安裝外層和配對既有 Runner，不再自動 provision HAOS；測試應明確驗證這個行為。媒體建置和一次性 VM 安裝是不同證據；Owner 實體 laptop、真帳號、真 HAOS App coding、家庭 helpers、更新／退版及長時間運作各自記錄。

`HomeAssistant.ensure_write_compatibility()` 限制未測 Core 的寫入 adapter。擴充 Core 版本需實跑原生部署／觸發／重啟和 regression，再更新 lock；沒有 Owner 逐 PR 人工簽核 gate。任何正式家庭／部署副作用仍走產品的精確 Owner 批准。

在開發機只做建置及隔離測試。`host/install.py --apply` 僅可在已選定的目標或一次性 VM 執行；不得把開發 host 當作安裝測試環境。實際候選結果見 [VERIFICATION](VERIFICATION.md) 和 [v3 帳本](v3/PROGRESS.md)。
