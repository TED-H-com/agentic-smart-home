# Agentic Smart Home 0.4.0 技術預覽驗證

2026-10-12，修正 sandbox／manifest 權限後的 [GitHub CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38202961564) 通過 **145 passed、2 skipped，152.424 秒**；受測與媒體 source commit 均為 `cb11ffd8c0d3eb48a9854fd51add030a1df936e9`，見 [CI 記錄](../evidence/github-ci.json)及 [JUnit](../evidence/pytest-0.4.0-github.xml)。CI 包含 `--real-ha --containers`，跳過獨立大型 HAOS KVM 與完整 Linux ISO 安裝；後者已另行通過 **1 passed，1566.81 秒**，見 [ISO JUnit](../evidence/linux-install-0.4.0.xml)。本輪完成 E0 工程媒體及獨立救援範圍；Owner laptop／真帳號與 E1、E2 尚未完成。

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
| 離線 coding containment 基礎 | 19 項 sandbox 回歸檢查 Docker 限制與 fencing；另在安裝後一次性 VM 完成 root broker 掛載獨立 ext4、clone／修改／測試／patch、總容量耗盡、去重、程序樹取消及清理，見 [installed sandbox](../evidence/installed-sandbox-laptop-amd64.json) | 程式為離線 Python fixture，沒有 provider egress、cloud CLI 或 HAOS App coding；兩個 coding workers 仍 blocked |
| 固定 console 支援 | Host／support 20 項回歸通過；精確 ISO 安裝後停止 Controller，一般 Owner 以非 sudo diagnostics／support-bundle 完成救援，每筆版本均為 0.4.0，見 [installed rescue](../evidence/installed-rescue-laptop-amd64.json) | 通過 E0 獨立救援範圍；E2 updater／退版擴充與 Owner laptop 現場仍未驗 |
| 實際 Chromium UI | `tests/test_ui.py`、`test_gateway_ui.py`、`test_hermes_ui.py`：1440／768／390 px；Hermes 真實狀態語意、keyboard login form、key 清空且不進 storage／URL、續接／刷新／新 case、進階 Grok/HAOS 與 Hermes scope 隔離。API 為 fixtures，畫面見 `evidence/ui/hermes-*` | 沒有登入真 OpenRouter／LINE／CLI 帳號；UI fixture 不證明真家庭語意 |
| 保留家庭／CLI／通道回歸 | 真 Core 2026.10.1 的原生驗證／helpers／讀回／觸發／重啟；真受控 Runner、LINE.js 與 Supervisor package 測試。模型、部分 backup/平台端點依既有案例使用 fixtures | 不代表真 model→家庭完整流程、LINE／Discord／Telegram 實際收發或新 HAOS App coding |
| 精確 ISO／HAOS lifecycle | 一次性 VM 的 OS 安裝、外層 firstboot、四映像 ID、Owner HTTP、可選 HAOS provision、冷快照／restore、外層 reboot／HAOS autostart；三次讀到真 Core onboarding JSON list，見 [linux-install](../evidence/linux-install.json) | 是基礎安裝與生命週期驗證；没有 Home Worker App coding、真帳號或 T3-27 的完整 re-pair／helpers 驗收 |

Hermes 的七工具為 `host.status`、`worker.capabilities`、`worker.submit/status/cancel/result` 和 `case.evidence.read`。沒有原生 terminal/filesystem/browser/delegate 或 Live approve/apply。真上游執行與模型 fixture 分開標記；沒有把檔案名稱或模型輸出 passed 當作測試平台結果。

Controller 使用 `user_version=2`，0.3.x binary 不能讀取新格式。Hermes 的獨立保存上限透過私有 `/v1/retention` 同步，縮短時停止進行中的 turn 再刪逾期內容；同步失败不先改 Controller 設定。操作與敏感遷移快照的保護見 [OPERATIONS](OPERATIONS.md)。

Ruff、產生契約與文件完整性檢查通過。Git 發佈 allowlist 已做 Gitleaks 8.30.1 掃描，0 findings，見 [source-audit.json](../evidence/source-audit.json)；最終檔案數以該記錄為準。掃描範圍排除私有 parent／ignored runtime 資料，不能當成真家庭資料已獲公開授權。

## 0.4.0 工程媒體（一次性 VM 驗證通過）

媒體來自已凍結的 source commit `cb11ffd8c0d3eb48a9854fd51add030a1df936e9`，見 [release](../evidence/release.json)。下列精確 laptop ISO 已通過一次性 VM 全流程；測試核對安裝版本與四個實際 image ID。Linux TAR 是封裝產物，沒有另外執行既有主機就地安裝驗收。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.4.0-laptop-amd64.iso` | 5387124736 | `44d163dba7ff629cc287213cbbb9a4836381d24e45531533efa0051ca9625868` |
| `agentic-home-0.4.0-linux-amd64.tar` | 1307658240 | `12d4426978674706810dcc4be73de40ea26542a15f869e032b864d3879f04229` |

完整 pytest 耗時 1566.81 秒，內部安裝流程計時 1564 秒。`runtime_location=disposable_outer_linux_vm`、`model_backend=none`、`ha_backend=nested_official_haos`、`device_kind=none`；Ubuntu 24.04.5 上以一次性磁碟 serial 與臨時 SSH key 安裝，未碰其他磁碟。結束後 VM、測試容器與磁碟 basetemp 已清除。證據見 [linux-install](../evidence/linux-install.json)及 [JUnit](../evidence/linux-install-0.4.0.xml)。

firstboot 僅啟外層，HAOS 尚未建立；[真 Owner HTTP 登入](../evidence/installed-auth-laptop-amd64.json)與受控 Runner 配對成功。四個 CLI 均為 `needs_reauth`、Hermes 為 `needs_auth`，coding workers 為 blocked，這是未登入帳號的真實狀態。停止 Controller 後，[非 sudo 診斷與 support bundle](../evidence/installed-rescue-laptop-amd64.json)仍可用，版本保持 0.4.0；無模型參與。

[離線 sandbox probe](../evidence/installed-sandbox-laptop-amd64.json)共耗時 2.1 秒：獨立 clone 產出 patch、重現程式與成功測試；原始 checkout／Git metadata 不變。容量 62869504 bytes 的 ext4 在累計寫入 60252160 bytes 時回 ENOSPC，單檔只有 1048576 bytes，低於 16777216 bytes 的檔案限制，證明測到總檔案系統容量。取消後程序樹停止、writer fencing 與清理通過。隨後真正 provision HAOS，首次啟動、冷快照 restore、外層 reboot／HAOS autostart 後均讀到 Core onboarding API JSON list。

前輪 source `69a1a24`／ISO `21b4d9a0…` 已失敗，媒體移至本機 `build/before-sandbox-fix-*`；其精確 digest 與 `02e7e00` 測試紀錄保留於 [封存 release](../evidence/release-0.4.0-before-sandbox-fix.json)，不能當作本轮媒體驗證。

## 0.4.0 工程追蹤

- 首次 GitHub CI（`a973a078`）為 **140 passed、1 failed、2 skipped**。失敗來自 PID fixture 檔案已存在但內容尚未寫完的競態，完整記錄保留於 [initial-failure](../evidence/github-ci-0.4.0-initial-failure.json)。`d3ad83b` 改為原子發布 fixture，沒有修改 runtime 或放寬產品斷言；該案例單跑與 runtime 16 項回歸通過。[第二次 CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38196533079) 已通過 **141 passed、2 skipped／160.94 秒**，為相依安全更新前的結果。
- 發佈後 Dependabot 列出 Hermes 三項相依共 20 則公告（PyJWT 14、httpx2 5、httpcore2 1）。目前候選改為 PyJWT 2.15.0、httpx2／httpcore2 2.12.0；依據包含上游 [PyJWT 公告](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-ffc3-869f-jxw9)、[HTTPX2 解壓公告](https://github.com/pydantic/httpx2/security/advisories/GHSA-8xx6-hgc6-gc2m)及 [HTTPCore2 TLS 公告](https://github.com/pydantic/httpx2/security/advisories/GHSA-7mj9-2mp8-4m2p)。更新後映像已重建，runtime／容器 **16 passed／58.08 秒**，含實際 wheel 的 pip check 與 PyJWT 驗證選項／過期 token／OKP keys 回歸；見 [安全更新證據](../evidence/hermes-security-update.json)。修補版 `69a1a24` 的 [GitHub CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38197039535) 已通過 **141 passed、2 skipped／164.44 秒**；發佈後重新查詢未處理 Dependabot 公告為 **0**；依賴說明見 [Hermes README](../hermes/README.md)。
- 初始候選在 OS 安裝完成、firstboot 安裝套件期間因安全更新而中止，未通過驗收；清除 VM／磁碟暫存後改測修補媒體。見 [中止紀錄](../evidence/linux-install-0.4.0-initial-interrupted.json)；不能沿用初始候選作為最終媒體證據。
- `21b4d9a0…` ISO 第一輪完成 OS／外層 firstboot、四映像 ID 核對、真 Owner HTTP 登入、Hermes needs_auth、Controller 停止時的診斷與 support 檔案建立，但測試把 bundle 格式誤寫成 collector 格式而失敗；見 [失敗紀錄](../evidence/linux-install-0.4.0-support-fixture-failure.json)。修正為既有 `agentic-support-bundle-v1` 契約後，以相同媒體重跑。
- 同 ISO 第二輪為 **1 failed／1764.31 秒**：上述安裝與救援步驟完成，但 sandbox 在 `launch_quota` 前置檢查即拒絕執行。產品誤讀 Docker JSON 欄位 `CPUCfsQuota`，實際為 `CpuCfsQuota`；這是產品缺陷，修正保留 CPU 限制要求。見 [失敗摘要](../evidence/linux-install-0.4.0-sandbox-preflight-failure.json)、[JUnit](../evidence/linux-install-0.4.0-sandbox-preflight-failure.xml)及 [sandbox 記錄](../evidence/installed-sandbox-preflight-failure.json)。quota／取消程式與 HAOS provision／restore／reboot 均未執行。
- 該輪 rescue 同時發現 `installed.json` 由 root 以 0600 建立，一般 Owner 的 diagnostics 無法讀取，造成版本為 null。installer 已將此非秘密 manifest 改為 root:1000／0640；private host state 與憑證權限不變，collector 不提升權限。新媒體的非 sudo diagnostics 與每筆 evidence 安裝版本斷言均已通過。
- 首版候選本機回歸 **141 passed、2 skipped／107.87 秒** 保留於 [pytest-0.4.0.xml](../evidence/pytest-0.4.0.xml)；最新安全更新及 harness 修正以本頁開頭的 CI 結果為準。

修正後 Host／support 回歸 **20 passed／39.55 秒**，sandbox 全套 **19 passed／31.95 秒**。新增真 Docker JSON 欄位驗證及固定錯誤／逾時診斷，沒有調高限額。sandbox 首次補測曾為 17 passed／1 failed；事件顯示 kill→die 17.657 秒，超過 15 秒停止確認期限，但舊版沒有保存操作細節，不能確定是哪個呼叫逾時。新 deterministic 案例確認停止未確認時仍禁止釋放、下一 writer 與匯出；[失敗與事件](../evidence/coding-sandbox-cancel-pressure.json)、[完整重測](../evidence/coding-sandbox-foundation.xml)。新媒體安裝後的真 ext4 配額驗證亦已通過。

## 0.4.0 仍待取得的證據

- Owner laptop 的實體安裝、硬體與真帳號仍未驗收；一次性 VM 通過不能代替這些現場案例。Owner 可先使用外層，再選擇固定 Web／CLI provision HAOS。
- 真 OpenRouter 登入／雲端工具查詢、Host cloud coding CLI、UI 問題回報／獨立 preview 與一次實際修正、Home Worker 真 HAOS App 自主研究、互補／獨立驗證均未完成產品驗收。
- 真模型→helpers 提案／Owner 批准／原生觸發、可信候選 updater／失敗退版、Owner/BAT writer 接手、實體 laptop／設備／USB／LAN、相鄰 Core、離機恢復與 72h 仍需逐項執行。
- 四家受控 CLI 的真帳號、LINE／Discord／Telegram 真收發、Google 訂閱登入没有新增現場證據。未使用開發主機做產品安裝，也未替 Owner 執行家庭副作用。

[Plan v3 進度帳本](v3/PROGRESS.md) 的 T3-01 已通過，T3-05 通過 E0 獨立救援範圍（E2 updater 擴充未做）；其餘 30 案維持 `not_run`。下一個開發切片是 Host cloud CLI 的受控 egress／帳號隔離與獨立 preview，再接真正 HAOS App coding worker。離線 Python 與 HAOS lifecycle 證據不等於 E1／E2 coding 完成。

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
