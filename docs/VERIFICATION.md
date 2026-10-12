# Agentic Smart Home 0.4.0 技術預覽驗證

2026-10-11，凍結本輪程式後的完整自動回歸：**141 passed、2 skipped，107.87 秒**，見 [pytest-0.4.0.xml](../evidence/pytest-0.4.0.xml)。執行包含 `--real-ha --containers`；跳過的是大型 HAOS KVM 與完整 Linux ISO 安裝案例，並非略過失敗的單元測試。本節不宣稱 E0、E1 或 E2 的產品驗收完成。

```bash
pytest-disktmp uv run pytest -q --real-ha --containers --junitxml=evidence/pytest-0.4.0.xml
```

## 0.4.0 實際測到的範圍

| 範圍 | 證據與環境 | 不代表什麼 |
|---|---|---|
| 真 Hermes／官方 MCP client | `tests/test_hermes_runtime.py` 執行 pinned upstream `AIAgent`（commit `835c3dd038d903182a66ecc45e34bf5e0de65f00`）及官方 HTTP MCP client；包含工具 discovery→固定 `host.status`→第二次模型回應、provider 401，及實際 Controller MCP handler 往返。模型端點與主機觀測是本機 protocol fixtures | 沒有真雲端帳號或現場主機健康證據；模型與 MCP 路徑不能合稱全套 real |
| 私有 Hermes bridge | 真 packaged service 以 UID 10003 啟動、無帳號顯示 needs_auth；service tests 檢查 scope/session/run 去重、認證撤銷、重啟不重播、期限與真本機子程序停止，包括 leader 已退出的 descendant／stop_unconfirmed fencing | configured 只表示保存，不是驗證登入；未通過 E1 coding |
| Controller／MCP | `tests/test_hermes_controller.py` 檢查固定七工具、拒絕 Live／偽造權限、blocked job 去重、case ACL、credential/audience 變更、串流期間重新驗權／budget、取消後不接受晚到成功、Web 登出撤權及 retention 同步失败回 503 | worker submit 只建立 `execution_started=false` 的 blocked handle；沒有發起 cloud coding 或家庭寫入 |
| Schema 2 遷移 | `tests/test_store_migration.py` 兩項測試：schema 1 先留私有一致 snapshot，再升到 2 並保留 settings／audit／去重；新 DB 為 2，未知新版拒絕。SQL tables 沿用，但資料語意明確不相容於 0.3.x | 遷移不是安全降版；固定 updater／rollback 尚未實作 |
| 離線 coding containment 基礎 | `tests/test_coding_sandbox.py` 的真 Docker negatives 檢查無網路／管理存取、非 root、read-only root、PID／memory limits、程序樹 timeout／取消；fixture 檢查 writer fencing、去重、artifact 限额／秘密／link 拒絕。另在普通磁碟檔執行 ext4 格式檢查，沒有在開發 host 掛載 | 不證明 root-only ext4 mount／quota 全流程、provider egress、cloud CLI 或 HAOS App sandbox；兩個 coding workers 仍 blocked |
| 固定 console 支援 | `tests/test_support.py` 的 redaction／固定 probe fixtures，加真子程序 timeout/output/environment tests；本機 collector 另做唯讀 development-host smoke，無模型／HA API | 不是候選安裝機故障救援或 Owner laptop 驗收 |
| 實際 Chromium UI | `tests/test_ui.py`、`test_gateway_ui.py`、`test_hermes_ui.py`：1440／768／390 px；Hermes 真實狀態語意、keyboard login form、key 清空且不進 storage／URL、續接／刷新／新 case、進階 Grok/HAOS 與 Hermes scope 隔離。API 為 fixtures，畫面見 `evidence/ui/hermes-*` | 沒有登入真 OpenRouter／LINE／CLI 帳號；UI fixture 不證明真家庭語意 |
| 保留家庭／CLI／通道回歸 | 真 Core 2026.10.1 的原生驗證／helpers／讀回／觸發／重啟；真受控 Runner、LINE.js 與 Supervisor package 測試。模型、部分 backup/平台端點依既有案例使用 fixtures | 不代表真 model→家庭完整流程、LINE／Discord／Telegram 實際收發或新 HAOS App coding |

Hermes 的七工具為 `host.status`、`worker.capabilities`、`worker.submit/status/cancel/result` 和 `case.evidence.read`。沒有原生 terminal/filesystem/browser/delegate 或 Live approve/apply。真上游執行與模型 fixture 分開標記；沒有把檔案名稱或模型輸出 passed 當作測試平台結果。

Controller 使用 `user_version=2`，0.3.x binary 不能讀取新格式。Hermes 的獨立保存上限透過私有 `/v1/retention` 同步，縮短時停止進行中的 turn 再刪逾期內容；同步失败不先改 Controller 設定。操作與敏感遷移快照的保護見 [OPERATIONS](OPERATIONS.md)。

Ruff、產生契約與文件完整性檢查通過。Git 發佈 allowlist 的 237 份檔案經 Gitleaks 8.30.1 掃描，0 findings，見 [source-audit.json](../evidence/source-audit.json)；掃描範圍排除私有 parent／ignored runtime 資料，不能當成真家庭資料已獲公開授權。四個 0.4.0 image 的建置記錄與待建置媒體的安裝驗收分開。

## 0.4.0 仍待取得的證據

- 新候選 Linux bundle／laptop ISO 的精確 digest、一次性 VM 安裝／firstboot／救援／重啟尚未記為完成。本次原始碼提交之後才建置對應媒體；下面 0.2.x 的歷史安裝不能替代本版。
- firstboot 已改為只啟外層服務，不自動建立 HAOS；該程式行為有自動回歸，待候選媒體安裝驗證。Owner 可稍後用固定 Web／CLI provision HAOS。
- 真 OpenRouter 登入／雲端工具查詢、Host cloud coding CLI、UI 問題回報／獨立 preview 與一次實際修正、Home Worker 真 HAOS App 自主研究、互補／獨立驗證均未完成產品驗收。
- 真模型→helpers 提案／Owner 批准／原生觸發、可信候選 updater／失敗退版、Owner/BAT writer 接手、實體 laptop／設備／USB／LAN、相鄰 Core、離機恢復與 72h 仍需逐項執行。
- 四家受控 CLI 的真帳號、LINE／Discord／Telegram 真收發、Google 訂閱登入没有新增現場證據。未使用開發主機做產品安裝，也未替 Owner 執行家庭副作用。

[Plan v3 進度帳本](v3/PROGRESS.md) 保留 16 項工作與 32 個分階段驗收；本次全部新增产品驗收仍為 `not_run`。工程回歸支持相關程式邊界，不能批次換成現場 passed。媒體建置、雲端帳號與可用能力分開發佈，不把未完成項隱藏成已通過。

以下保留舊版歷史結果。未帶版本的歷史 evidence 路徑可能由後續回歸更新，閱讀時需核對檔案內版本／hash；它們不是 0.4.0 媒體驗收。

## 0.3.1 歷史驗證紀錄

2026-10-11 完整回歸 **89 passed、2 skipped**，見 `evidence/pytest-0.3.1.xml`。包含實際 Core、三個服務映像與 Chromium；兩项大型 HAOS VM／Linux 重灌案例未重跑。

- 新增九個 Gateway 案例：追問排序、失敗與取消、重啟續接、跨 Agent／通道／聊天室／使用者隔離、權限變更、限量前文、保存期限與去重。
- 瀏覽器協定 fixture：續接、新對話、切換 runtime、重新整理保留 ID、送出回覆遺失後重試不重複；1440／768／390 px 均檢查無水平溢出。
- Runner ZIP 固定封裝時間戳；連續建置雜湊相同，16 份檔案的 manifest 全數核對。此項不代表 Linux rootfs 可位元重現。
- 版本、契約、production UI 與 Linux 封裝同步為 0.3.1；CLI 固定版本維持原設定。

真實雲端登入、LINE／Discord／Telegram 收發、實機硬體與 Google 訂閱登入不在本次已完成證據之內。家庭寫入仍須具體提案與 owner 批准。

0.3.1 當時的映像與媒體記錄固定參照 [build](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/evidence/build.json)、[release](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/evidence/release.json)。這些是歷史資料；GitHub CI 記錄以 `headSha` 明確指出受測 commit。

## 0.3.0 歷史驗證紀錄

2026-10-11 完整回歸 **80 passed、2 skipped**，見 `evidence/pytest-0.3.0.xml`。跳過的兩項為大型 HAOS VM 與 Linux 整機重灌案例；本版沒有重跑，以下歷史結果不能視為 0.3.0 的新硬體證據。

- 真實 Core 2026.10.1：原生設定驗證、helper 操作、部署／讀回／觸發與重啟。
- 真實 Runner image：四種官方 CLI 的版本與獨立 Unix UID／profile，TLS 配對；Codex／Claude 的推理使用本機協定端點。Antigravity／Grok 的新增輸出與登入 adapter 使用 subprocess 協定 fixture。
- 真實 LINE.js image：隔離 `--network none`，HTTP Host 限制、cookie／Origin 管理連線、白名單重載、一次性 bot token、bot socket 不接受 Origin 或管理指令。未登入 LINE、未發送外部訊息。
- Gateway fixtures：HA 離線 Web 對話、內層 Runner 路由、Web 批准不被通訊訊息取代、聊天室＋使用者白名單、去重、憑證隔離、設定變更後不誤送、重啟中斷與內容清理。
- Chromium：首次 Web onboarding、LINE QR／PIN fixture、Grok device-code、兩層登入、進度續接、統一對話及既有家庭工作台，1440／768／390 px。
- Ruff、產生契約與文件連結檢查、TypeScript／Vite production build 通過；另外以 `npm ci` 驗證乾淨安裝。
- 首次公開後的 Dependabot 三則 cryptography 公告已透過升級 50.0.2 修正（在三份依賴清單重複顯示為九筆）。[官方 changelog](https://cryptography.io/en/latest/changelog/) 說明修正；原應用沒有使用涉及的 PKCS#7／憑證鏈驗證 API。

雲端帳號、LINE／Discord／Telegram 真實收發、Google 訂閱登入及實機筆電仍沒有本版驗證證據。沒有將開發主機當成安裝目標，沒有執行 `host/install.py --apply`。

0.3.0 封裝記錄見 `evidence/build-0.3.0.json`、`evidence/release-0.3.0.json`；大型媒體留在本機 `dist/`，Git 只包含產品來源與驗證摘要。

## 0.2.1 歷史體驗更新驗證

本次更新工作台、首次設定、主機頁、登入續接、任務導覽與原生 HA 跳轉。測試結果見 `evidence/pytest-0.2.1.xml`，新媒體與映像記錄見 `evidence/release.json`、`evidence/build.json`。

本次功能／真實 Core／CLI 容器／瀏覽器回歸：**63 passed、2 skipped**（两個大型 VM 案例）。最後的舊連線回應處理另跑 **4 個瀏覽器案例**，不是另外 4 個獨立案例。1440／768／390 px 無水平溢出，原生 HA 跳轉、跨頁登入、設備未儲存處理、登入到期草稿保留都有互動驗證。

本次沒有重跑完整 Linux／HAOS VM 安裝；下方是 **0.2.0 的歷史平台證據**，不能當成新版 ISO 已重灌完成。舊映像與媒體記錄保存在 `evidence/build-0.2.0.json`、`evidence/release-0.2.0.json`。

## 0.2.0 歷史驗證紀錄

執行日期：2026-10-11。產品主線為外層 Ubuntu Linux＋獨立 Agent runtime＋HAOS KVM 虛擬機。

## 軟體與真實平台

| 範圍 | 已執行結果 |
|---|---|
| 功能、容器與 Chromium | 59 passed、2 skipped；兩個大型 VM 案例另行執行。`evidence/pytest.xml` |
| 真實 Core 2026.10.1 | 原生 validate_config、automation 寫入／讀回／reload、helper off→on、context 關聯、Core 重啟後再次觸發。`evidence/real-core-integration.json` |
| 真實 HAOS KVM | 官方 HAOS 18.3 開機，Core 2026.10.1 owner 身份、Supervisor 2026.09.3 代理與真正備份完成／讀回。`evidence/haos-vm.json` |
| CLI 容器 | 官方 Codex 0.157.1／Claude Code 2.1.296，非 root、profile 隔離、環境去密、TLS 配對、結構化回覆。`evidence/runner-container.json` |
| 外層管理 | localhost 身份／CSRF、HA 斷線時可用、主機工作狀態、限定 VM ownership、重啟中斷／restore journal、profile 驗證；Chromium 1440／768／390 px。`tests/test_host.py`、`evidence/ui/` |
| 完整 Linux ISO | 由 `tests/test_linux_install.py --linux-install` 在一次性磁碟執行。安裝演練所用 ISO hash、安裝／firstboot／內層 HAOS 結果記於隨 source ZIP 交付的 `evidence/linux-install.json`；不能由 unit tests 推定 |

TypeScript／Vite production build、Ruff、契約檢查及 Docker build 已執行。ISO 保留上游 UEFI／BIOS boot records；實際安裝演練使用 UEFI／nested KVM。

## 執行方式

```bash
pytest-disktmp uv run pytest -q --real-ha --containers --junitxml=evidence/pytest.xml
pytest-disktmp uv run pytest -q tests/test_haos_vm.py --haos-vm
pytest-disktmp uv run pytest -q tests/test_linux_install.py --linux-install
```

所有暫存磁碟、SQLite、SSH key 都使用磁碟上的 pytest wrapper 目錄；測試容器、虛擬磁碟和憑證結束後刪除。

## Fixtures 與限制

- Core 容器測試的 Supervisor backup 是 fixture；另有上述真實 HAOS 備份案例。
- Runner 執行官方 CLI，但推理端點是本機 OpenAI／Anthropic 協定 fixture。沒有使用真實帳號或雲端模型呼叫。
- 瀏覽器 API 是 fixture；完整 ISO 案例另外檢查實際外層 Controller HTTP。
- 全自動 ISO 測試只在 fixture 中指定一次性虛擬磁碟 serial 與臨時 SSH key；公開 ISO 仍由使用者選磁碟、網路及帳號。
- 物理 laptop、合蓋／網卡／USB、Alexa／Nest 帳號及 72 小時 soak 尚未執行。
- Ubuntu 26.04 profile 是擴充配方，尚未建置或實測；目前交付媒體使用 Ubuntu 24.04。
- 目前採 NAT，沒有 LAN multicast discovery 或自動實體 bridge 配置。

上述缺少現場資料不形成開發簽核流程；成品依實際連線／登入狀態顯示結果，不假裝已連接。

## 完整性與來源

`evidence/build-0.2.0.json` 保存映像 ID 與 runtime hashes；`host/bases.lock.json` 保存 Linux／HAOS 來源。TAR 的 `BUNDLE.sha256.json`、source ZIP 的 `MANIFEST.sha256.json` 與 `dist/SHA256SUMS` 分別核對內部檔案與交付媒體。

上游 apt 安全更新仍在安裝時下載，因此這不是完全離線或位元可重現的 Linux rootfs。

安裝演練後的修改及最終 artifact hashes 見 `evidence/release-0.2.0.json`；`evidence/linux-tested-build.json` 保留演練時的 runtime hashes，避免把後續修改冒充已跑過相同 ISO。

額外主機權限回歸：`tests/test_host.py` 12 passed，包含多個 Linux 帳號時只有 UID 1000 收到 mode-0600 主機金鑰。

## Linux 安裝結果

- **筆電版：通過。** 約 28 分鐘，Ubuntu GNOME 登入畫面、首次部署、HAOS 啟停、關機時外層 HTTP、冷快照／還原及 Linux reboot 後自動啟動均已觀察。當時的 HA readiness 判準是 HTTP 200；不是雲端 Agent 或完整 Core onboarding 的證據。
- **伺服器版：通過。** 最終 ISO 的精確 SHA-256 與測試映像一致。Core onboarding API 的真實 JSON 回應在首次啟動、還原及外層 Linux reboot 後都驗證通過。見 `evidence/linux-install-server.json`。
- **實際主機登入／配對：通過。** 真正的 Controller cookie session、主機 Unix socket API 與 Runner TLS 配對可用；兩 provider 正確顯示 needs_reauth。見 `evidence/installed-auth-server-amd64.json`。
- 筆電測試映像與最後交付版只差 `host/install.py` 的桌面金鑰帳號範圍修正；該修改已通過多帳號檔案權限回歸。最終筆電 ISO 沒有再重跑整機安裝。完整差異與兩份 ISO hash 見 `evidence/release-0.2.0.json`。

測試 VM／磁碟／臨時登入資料均已移除。第一次實機部署完成後，建議再重開一次 Linux，載入合蓋設定；雲端帳號與家庭 onboarding 由使用者在成品中登入。
