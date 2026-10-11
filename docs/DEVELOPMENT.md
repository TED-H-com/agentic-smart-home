# 開發與自動化測試

## 重現

使用 Python 3.13、Node 24、Docker amd64。`uv.lock`、npm locks、requirements hashes、base image digests 都已提交到交付包。先在 repo 根目錄執行 README 的安裝／建置命令。

所有 pytest 都經 `pytest-disktmp`。未安裝 wrapper 時，下列等效命令建立唯一磁碟暫存並在退出清理：

```bash
mkdir -p "$HOME/agent-work/tmp/pytest"
agentic_test_tmp=$(mktemp -d "$HOME/agent-work/tmp/pytest/agentic-home-XXXXXX")
trap 'rm -rf -- "$agentic_test_tmp"' EXIT
uv run pytest -q --basetemp "$agentic_test_tmp"
```

不可使用 `/dev/shm` 或 tmpfs 作為測試資料庫／fixtures。真實 Core 測試要求 basetemp 位於 `~/agent-work/tmp/pytest/`，以唯一名稱建立容器，完成時修正該測試目錄的擁有者並移除容器。容器 probe 的資料在自身可丟棄的磁碟 overlay 中。

## 測試分類

| 類型 | 命令 | 真實／fixture |
|---|---|---|
| Controller／Runner 行為 | `pytest-disktmp uv run pytest -q tests/test_engine.py tests/test_api.py tests/test_runner.py` | HA／provider 契約 fixtures |
| UI | `pytest-disktmp uv run pytest -q tests/test_ui.py` | 真 Chromium，API fixtures；三個寬度 |
| Core | `pytest-disktmp uv run pytest -q tests/test_real_ha.py --real-ha` | 真官方 Core；只有 Supervisor backup、Agent intent 使用 fixture |
| Runner／Supervisor package | `pytest-disktmp uv run pytest -q tests/test_container.py --containers` | 真 CLI、TLS、Unix UID、官方 schema／backup filter；模型回應為本機協定 fixture |

容器測試先建置 `agentic-home-controller:0.3.0`、`agentic-home-runner:0.3.0`、`agentic-home-linejs:0.3.0`；需要官方 `ghcr.io/home-assistant/amd64-hassio-supervisor:2026.09.3` image。Core 測試自行取得 2026.10.1 image。不讀開發機的 provider credentials，不呼叫付費 inference。

`tests/container_probe.py` 僅由測試容器執行，正式映像沒有它。它將官方 CLI 連到本機 fixture，查驗 Codex 工具清單為空、Claude 只提供 StructuredOutput，並檢查真實 normalized output。

## 契約與包裝

```bash
uv run python scripts/contracts.py       # 有意變更模型後重新產生
uv run python scripts/contracts.py --check
uv run ruff check controller runner scripts tests
npm run build --prefix web
uv run python scripts/package.py
```

打包明列允許的目錄，不包含 `_upstream`、`.venv`、node_modules、cache 或本機 credentials。每個 ZIP 內有 `MANIFEST.sha256.json`；ZIP 外有 `dist/SHA256SUMS`。編譯後 `controller/static/` 刻意包含在來源包中，讓 HA Supervisor 可直接建置。

更新 provider 時需同時更新 npm lock、`VERSIONS`、`providers.lock.json` 與容器 probe。Codex 靜態 catalog 來自同一 CLI tag；保留官方文字與 Apache 授權，明列修改的工具 metadata。不要直接使用新的未驗證模型 fallback，因其預設工具集合可能不同。

## 相容性

`HomeAssistant.ensure_write_compatibility()` 以自動測試通過的 Core 版本限制非公開寫入 adapter。要擴充版本，修改 real Core test 的 image、實跑部署／觸發／重啟及相關 regression，再更新 `TESTED_CORE_VERSIONS` 和 lock。這是程式相容性檢查，不需要 Owner 逐階段簽核開發。

## 0.2 Linux 媒體

主線建置與 profiles 見 [LINUX_PRODUCT.md](LINUX_PRODUCT.md) 和根目錄 [INSTALL.md](../INSTALL.md)。在開發機只建置與測試；`host/install.py --apply` 僅用於已選定的目標 Linux，不能直接在開發主機執行。
