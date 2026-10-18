# Agentic Smart Home 0.11.0 工程預覽候選

[專案介紹與互動示範](https://ted-h-com.github.io/agentic-smart-home/) · HA 介面模擬與產品管家介面，使用虛構資料。

一台 Linux 主機，裝好就有 Home Assistant 家庭、一個會用工具的智能管家，以及能自己調查與修改程式的開發助手。家裡的設備照 Home Assistant 原生方式運作；AI 只提出變更，由你批准後才寫入。

**0.11.0 候選**將 HA 原生文字通知、重開機恢復、App 更新重試與診斷補強整合進同版主機媒體。延續 0.10.0 的設備盤點、有線家庭接入與內外層開發助手。發布後可從 [v0.11.0 發布頁](https://github.com/TED-H-com/agentic-smart-home/releases/tag/v0.11.0) 取得下載工具。候選的精確來源、媒體與測試結果見 [驗證紀錄](docs/VERIFICATION.md)；完成驗證與公開頻道銜接前，不將已合併功能視為可下載交付。

上一個已發布版本仍可在 [v0.10.0](https://github.com/TED-H-com/agentic-smart-home/releases/tag/v0.10.0) 查閱；既有媒體與驗證紀錄保留。實體筆電、真帳號、家庭設備及 72 小時運作由 Owner 現場驗收。

## 安裝

準備一支可清除的 **32 GB USB**。可以直接在這台筆電的現有系統下載並製作 USB，再重新開機安裝；先備份要保留的資料，安裝會清除選定的內部磁碟。

1. 從發布頁下載 `download-0.11.0.py`，依 [安裝步驟](INSTALL.md#下載與製作-usb) 下載、校驗並合併 ISO，寫入 USB。接上電源與有線網路，從 USB 開機；Dell 通常使用 F12 一次性開機選單。硬體檢查通過後，在 Ubuntu 安裝器選網路、磁碟和帳號。
2. 重開機後登入桌面，**Agentic Home** 會自己打開。
3. 按「開始設定」前先選家庭有線接入；接著照畫面完成：AI 帳號 → 智能管家 → 通訊（LINE、Telegram、Discord，或只用網頁）→ 家庭。

需求與詳細步驟見 [安裝](INSTALL.md)。

## 每天使用

| 想做的事 | 去哪裡 |
|---|---|
| 開燈、看設備、日常自動化 | Home Assistant 原生畫面 |
| 請管家幫忙、看提案並批准 | **智能管家**：HA 側欄或主機網頁；LINE 等通訊可以對話，批准一律在網頁 |
| 找出可接入的設備 | **設備 → 探索更多設備**：選網路探索，或匯入路由器清單；追蹤原生 HA 設定與讀回結果 |
| 修程式、查問題 | **設定 → 開發助手**：主機或家庭現場，成果依序是發現、測試、變更 |
| 從 HA 自動化發送文字通知 | [原生文字通知](docs/HA_NOTIFICATIONS.md)：安裝整合、設定收件人別名，再呼叫原生動作 |
| 更新、備份、救援 | **設定 → 主機維護**；Web 打不開時用 `sudo agentic-home diagnostics` |

家庭變更一律先顯示精確內容，由你批准；批准後系統備份、寫入，再確認狀態。Home Assistant、HAOS 與其他 App 照官方流程更新。

## 架構

```text
主機 Ubuntu Linux
├── Controller：網頁、設定進度、對話、家庭提案與批准
├── Hermes：智能管家，獨立登入，只用固定工具
├── Host broker：隔離的開發助手、獨立驗證、預覽、HAOS VM 與更新
├── Runner、LINE.js：受控 CLI 與可選的 LINE 連線
└── KVM → 官方 HAOS
              ├── 智能管家側欄 App → 同一個外層對話
              └── 家庭現場 App → 家裡的開發助手（獨立登入）
```

開發助手在沒有家庭權限的沙箱裡修改私有 clone；另一份沒有帳號、沒有網路的 clone 套用同一份修改，跑固定的基準測試。結果只是成果，不會自動部署。

## 建置與測試

需要 Linux amd64、Python 3.13、Node 24、Docker。

```bash
uv sync --frozen
npm ci --prefix web
npm run build --prefix web
uv run python scripts/contracts.py --check
uv run python -c "import sys; sys.path.insert(0,'scripts'); from package import package_runner, package_bridge, package_notifications; package_runner(); package_bridge(); package_notifications()"
uv run python scripts/controller_source.py
for app in controller runner bridge hermes; do docker build -t agentic-home-$app:0.11.0 $app; done
docker build -t agentic-home-linejs:0.11.0 host/linejs
docker build -t agentic-home-preview:0.11.0 host/preview
python3 scripts/build_host.py
uv run python scripts/package.py
```

`dist/` 會有 `agentic-home-0.11.0-linux-amd64.tar`、source ZIP 和 `SHA256SUMS`。安裝媒體：

```bash
docker build -f host/Builder.Dockerfile -t agentic-home-builder:0.2.0 host
python3 scripts/build_host.py --profile laptop-amd64 --iso --reuse-bundle
```

產品來源必須先提交；安裝包內含精確 HEAD 的 Git bundle，開發助手研究的就是這個版本。測試一律用磁碟暫存，不用 `/dev/shm` 或 tmpfs：

```bash
pytest-disktmp uv run pytest -q --real-ha --containers
```

測試範圍、VM 案例與無 wrapper 的等效命令見 [開發說明](docs/DEVELOPMENT.md)。

## 文件

[安裝](INSTALL.md) · [更新紀錄](CHANGELOG.md) · [首次設定](docs/ONBOARDING.md) · [開發助手](docs/WORKERS.md) · [通訊與登入](docs/GATEWAY.md) · [更新](docs/UPDATES.md) · [救援](docs/OPERATIONS.md) · [Linux 架構](docs/LINUX_PRODUCT.md) · [家庭區網接入](docs/HA_LAN.md) · [驗證紀錄](docs/VERIFICATION.md) · [v3 驗收帳本](docs/v3/PROGRESS.md)

工程測試和真帳號、真家庭的驗收分開記錄；fixture 通過不等於你的家庭已驗收。
