# Agentic Smart Home 驗證

## 0.9.0

本版重做介面，開發助手可在網頁啟用、登入，主機重開後自動復原，內容見 [CHANGELOG](../CHANGELOG.md)。受測來源、媒體與 harness 都是同一個 commit [`13ad697a`](https://github.com/TED-H-com/agentic-smart-home/commit/13ad697aca3e61fa6c409ee08ffa177d3c297842)。家庭現場映像內的開發來源固定為 [`0eb569e4`](https://github.com/TED-H-com/agentic-smart-home/commit/0eb569e42ed2661e3e3bd4c7835b3715d57e3da0)。來源、映像與限制的完整紀錄見 [0.9.0 交付紀錄](../evidence/release-0.9.0.json)。

| 項目 | 結果 | 證據 |
|---|---|---|
| 本機回歸（`--real-ha --containers`） | 1809 passed、7 skipped／884.252 秒 | [JUnit](../evidence/pytest-0.9.0.xml) |
| [GitHub CI 38692753833](https://github.com/TED-H-com/agentic-smart-home/actions/runs/38692753833) | 1807 passed、9 skipped／1117.76 秒 | [核對](../evidence/github-ci-0.9.0.json)、[JUnit](../evidence/pytest-0.9.0-github.xml) |
| ISO bundle 對 CI 來源 | 190 份執行／設定檔、50 份文件逐檔吻合 | [比對](../evidence/ci-media-match-0.9.0.json)、[建置](../evidence/build-0.9.0.json) |
| HAOS 備份 | 2 passed | [結果](../evidence/haos-vm-0.9.0.json)、[JUnit](../evidence/haos-vm-0.9.0.xml) |
| HAOS App 邊界 | 2 passed | [結果](../evidence/haos-app-boundary-0.9.0.json)、[JUnit](../evidence/haos-app-boundary-0.9.0.xml) |
| 智能管家側欄 App（本機來源交付） | 19 passed | [結果](../evidence/bridge-haos-local-0.9.0.json)、[JUnit](../evidence/bridge-haos-local-0.9.0.xml)、[截圖](../evidence/ui/bridge-native-haos-0.9.0.png) |
| 家庭現場開發助手（真 HAOS App） | 78 passed／746.859 秒 | [結果](../evidence/home-worker-haos-0.9.0.json)、[JUnit](../evidence/home-worker-haos-0.9.0.xml) |
| 一鍵啟用家庭現場、cold 備份與整台重開（真 HAOS） | 42 passed／1384.502 秒 | [結果](../evidence/home-worker-enable-haos-0.9.0.json)、[JUnit](../evidence/home-worker-enable-haos-0.9.0.xml) |
| 家庭現場 App 由真 Supervisor 建置（本機來源交付） | 72 passed／732.65 秒 | [結果](../evidence/runner-supervisor-build-0.9.0.json)、[JUnit](../evidence/runner-supervisor-build-0.9.0.xml) |
| 精確 ISO 完整安裝 | 1 passed／2021.549 秒 | [結果](../evidence/linux-install-0.9.0.json)、[JUnit](../evidence/linux-install-0.9.0.xml) |

本機的 7 個跳過項就是上表另跑的 VM 案例，沒有算成通過。CI 另跳過 UID 1000 桌面專項及本機 QEMU builder 探針；CI 映像由 CI 獨立建置，不宣稱 ID 與本機相同。每個 VM 項目都只有一個真 VM 案例，其餘是 harness 單元測試。

HAOS 備份測試建立一次性擁有者後，等 Core 回報 `RUNNING`、Supervisor 回報 running 且 healthy 才備份，第一次就完成。

家庭現場開發助手的真 VM 整合案例通過 15 項整合檢查及 38 項獨立基準測試；outer、VM、network 已移除，磁碟 ownership 已恢復。

一鍵啟用由測試自己的 git daemon 把受測 commit 當成 `stable` 提供，只換掉信任的 repository 網址。VM 大小同出貨的 laptop profile（4096 MiB、2 vCPU、64 GiB）。依序通過：

- 側欄自動安裝；按「啟用家庭現場」後，真 Supervisor 建置、安裝、啟動家庭現場 App，Controller 配對到可登入；從按下到可登入共 367 秒。
- 一次 cold 部分備份：Supervisor 進入 `freeze`，期間看到 App 被停止，之後自動恢復。
- 整台重開 HAOS 與 Controller。

全程只出現執行中、等待與可登入狀態，沒有顯示失敗，也不需要修復；16 項檢查通過。沒有登入、模型或開發工作；inference 與 Host 能力是 fixture。

家庭現場 App 建置測試交付受測 commit 的 `runner/`（git tree `cdf5d405`）。VM 大小同出貨的 laptop profile（4096 MiB、2 vCPU、64 GiB）。真 Supervisor 用 262.8 秒建置 `runner/Dockerfile`，安裝、啟動後通過 18 項檢查：

- App 合約與容器邊界：自訂 AppArmor（enforce）、只有 SYS_ADMIN 與 `/dev/fuse`、沒有 Docker socket。
- `/health` 200；未配對的 App 把一次性配對寫入自己的 options，指紋等於 8443 實際提供的憑證。
- Codex 0.157.1 等於 lock；映像內開發來源 `0eb569e4` 通過產品自己的驗證。

Supervisor builder 與兩個 base image 先載入 HAOS，但 HAOS 無法用 digest 在本機找到 base image，所以 build 時從 registry 依同一 digest 取得。apt、npm、PyPI、agy 與開發來源都是即時下載。沒有配對後的登入、模型或開發工作。

ISO 在一次性 VM 依序通過下列項目：

- Ubuntu 安裝與 firstboot。
- [角色登入](../evidence/installed-auth-laptop-amd64-0.9.0.json)與 [Controller 停止救援](../evidence/installed-rescue-laptop-amd64-0.9.0.json)。
- [sandbox](../evidence/installed-sandbox-laptop-amd64-0.9.0.json)。
- [真 Codex 工具與獨立 verifier](../evidence/installed-coding-laptop-amd64-0.9.0.json)。
- [隨附 Hermes 啟用](../evidence/installed-hermes-laptop-amd64-0.9.0.json)。
- 建立 HAOS 虛擬機，真 Core 就緒。
- [Controller 更新／退版](../evidence/installed-update-laptop-amd64-0.9.0.json)，含三次 bridge 固定 probe。
- 冷快照還原。
- 整機重啟後 HAOS 自動啟動。

VM 與磁碟目錄已移除，沒有在開發主機安裝產品。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.9.0-laptop-amd64.iso` | 6725500928 | `ed9482bb8be7faa17eb30c38c62ddb3370eafd9e8bb95a41be8f8de3369fe969` |
| `agentic-home-0.9.0-linux-amd64.tar` | 2646005760 | `3afe0ee51e6ed07063a33320178622e99de52036fbb79f59f6d7d499560c4da8` |
| `agentic-home-0.9.0-source.zip` | 31063612 | `51ad7e21c652623299fa997e58abe1301ac2f95e5819931116ca8043e152793e` |

沒有發布的候選如下。每個都換成下一個候選重跑全部驗收；只保留失敗的紀錄。

| 候選 | 原因 | 修正 | 失敗紀錄 |
|---|---|---|---|
| `4412d33` | 一鍵啟用家庭現場會停在商店檢查；HAOS 重開或備份會被當成你停止了 App（#62）。 | [#63](https://github.com/TED-H-com/agentic-smart-home/pull/63)、[#64](https://github.com/TED-H-com/agentic-smart-home/pull/64)、[#65](https://github.com/TED-H-com/agentic-smart-home/pull/65) | 家庭現場開發助手（[結果](../evidence/home-worker-haos-0.9.0-run1-failure.json)、[JUnit](../evidence/home-worker-haos-0.9.0-run1-failure.xml)） |
| `03fa99f` | 整台重開時兩張 App 卡片先顯示「暫時無法連線」；另有四個 VM 階段停在 harness 的時限。 | [#67](https://github.com/TED-H-com/agentic-smart-home/pull/67) | 家庭現場開發助手（[結果](../evidence/home-worker-haos-0.9.0-rc2-failure.json)、[JUnit](../evidence/home-worker-haos-0.9.0-rc2-failure.xml)）；App 邊界（[JUnit](../evidence/haos-app-boundary-0.9.0-rc2-failure.xml)）；一鍵啟用（[結果](../evidence/home-worker-enable-haos-0.9.0-rc2-failure.json)、[JUnit](../evidence/home-worker-enable-haos-0.9.0-rc2-failure.xml)）；安裝（[結果](../evidence/linux-install-0.9.0-rc2-failure.json)、[JUnit](../evidence/linux-install-0.9.0-rc2-failure.xml)） |
| `c9fb98a` | 三個 VM 階段停在 harness；其中一個找到側欄和家庭現場偶爾重用剛被關閉的連線。 | [#69](https://github.com/TED-H-com/agentic-smart-home/pull/69)、[#70](https://github.com/TED-H-com/agentic-smart-home/pull/70)、[#71](https://github.com/TED-H-com/agentic-smart-home/pull/71) | 安裝（[結果](../evidence/linux-install-0.9.0-rc3-failure.json)、[sandbox](../evidence/installed-sandbox-laptop-amd64-0.9.0-rc3-failure.json)、[JUnit](../evidence/linux-install-0.9.0-rc3-failure.xml)）；家庭現場開發助手（[結果](../evidence/home-worker-haos-0.9.0-rc3-failure.json)、[JUnit](../evidence/home-worker-haos-0.9.0-rc3-failure.xml)）；App 邊界（[JUnit](../evidence/haos-app-boundary-0.9.0-rc3-failure.xml)） |
| `281c1b5` | 一鍵啟用的 cold 備份期間，慢磁碟讓 Controller 卡在 SQLite commit，側欄顯示無法連線（#68）；另有三個 VM 階段停在 harness。 | [#77](https://github.com/TED-H-com/agentic-smart-home/pull/77)、[#80](https://github.com/TED-H-com/agentic-smart-home/pull/80)、[#76](https://github.com/TED-H-com/agentic-smart-home/pull/76)、[#78](https://github.com/TED-H-com/agentic-smart-home/pull/78)、[#79](https://github.com/TED-H-com/agentic-smart-home/pull/79) | 一鍵啟用（[結果](../evidence/home-worker-enable-haos-0.9.0-rc4-failure.json)、[JUnit](../evidence/home-worker-enable-haos-0.9.0-rc4-failure.xml)）；家庭現場開發助手（[結果](../evidence/home-worker-haos-0.9.0-rc4-failure.json)、[JUnit](../evidence/home-worker-haos-0.9.0-rc4-failure.xml)）；App 邊界（[JUnit](../evidence/haos-app-boundary-0.9.0-rc4-failure.xml)）；HAOS 備份（[JUnit](../evidence/haos-vm-0.9.0-rc4-failure.xml)） |
| `7fe911e` | 全套回歸有兩個 Docker 指令逾時；安裝後 Controller 讀不到 Runner 配對；HAOS 備份測試在 onboarding 後馬上要求備份，Supervisor 的備份工作失敗（重跑時看到 Core 還在啟動）。 | [#85](https://github.com/TED-H-com/agentic-smart-home/pull/85)、[#87](https://github.com/TED-H-com/agentic-smart-home/pull/87)、[#89](https://github.com/TED-H-com/agentic-smart-home/pull/89) | 全套回歸（[JUnit](../evidence/pytest-0.9.0-rc5-failure.xml)）；安裝（[結果](../evidence/linux-install-0.9.0-rc5-failure.json)、[登入](../evidence/installed-auth-laptop-amd64-0.9.0-rc5-failure.json)、[JUnit](../evidence/linux-install-0.9.0-rc5-failure.xml)）；HAOS 備份（[JUnit](../evidence/haos-vm-0.9.0-rc5-failure.xml)） |
| `6dd8b9a` | 建立 HAOS 虛擬機時，正常的映像被拒絕：映像檢查只給 `qemu-img` 3 秒，測試機磁碟很慢。 | [#90](https://github.com/TED-H-com/agentic-smart-home/pull/90) | 安裝（[結果](../evidence/linux-install-0.9.0-rc6-failure.json)、[JUnit](../evidence/linux-install-0.9.0-rc6-failure.xml)） |
| `90614e5` | 全部驗收通過（安裝與家庭現場開發助手第一次沒過，同一個 commit 重跑通過）。之後 repo 移到 TED-H-com，產品信任的 App repository 網址要跟著改。 | [#93](https://github.com/TED-H-com/agentic-smart-home/pull/93) | 安裝（[結果](../evidence/linux-install-0.9.0-rc7-run1-failure.json)、[JUnit](../evidence/linux-install-0.9.0-rc7-run1-failure.xml)）；家庭現場開發助手（[結果](../evidence/home-worker-haos-0.9.0-rc7-run1-failure.json)、[JUnit](../evidence/home-worker-haos-0.9.0-rc7-run1-failure.xml)） |
| `9d1d564` | 全部驗收第一次就通過。實測前最後一次檢查找到介面問題：家庭現場還沒啟用時，AI 帳號頁要你手動加入沒有 `#stable` 的 App 來源或下載安裝檔，裝到的 App 一鍵啟用接手不了；主機重開時，AI 帳號頁要你重新配對或啟用已配對的開發助手，每個帳號都顯示需要登入；主機重開後錯誤提示不會自己消失；畫面預覽關閉後不能再產生；修改內容因疑似含密碼只留在主機時，顯示成「沒有修改檔案」。 | [#94](https://github.com/TED-H-com/agentic-smart-home/pull/94) | 無 |
| `0015dd4` | 建置完成後、第一個驗收階段開始前停止。檢查 #94 的斷線恢復時發現：斷線時按過「重新連線」，連線恢復後錯誤提示仍不消失；第一次開啟就連不上時，要手動按才會打開；開著已經不存在的工作時，錯誤提示每 10 秒閃一次。主機重開正好會碰到這三種情況。之後檢查整條重開機路線，又找到幾個頁面在服務回來後仍停在錯誤，或誤報尚未設定、尚未登入，同一個 PR 一起修正。 | [#95](https://github.com/TED-H-com/agentic-smart-home/pull/95) | 無 |

家庭現場 App 建置測試在合併前第一次實跑時，建置、安裝、啟動都成功，但 harness 沿用 fixture App「options 必須為空」的條件而失敗，見 [失敗 receipt](../evidence/runner-supervisor-build-0.9.0-run1-failure.json)。該次 JUnit 印出了已刪除 VM 的一次性配對碼，所以不公開；harness 已改成任何失敗訊息都不會印出這類值。

`90614e5` 第一次執行時，安裝和家庭現場開發助手兩個階段沒有通過，同一個 commit 與媒體重跑後通過。當時測試機磁碟同時被其他專案的測試佔用，非常忙。

- 安裝：裝好的系統在測試等待的一小時內沒有完成首次開機。SSH 探測一直沒看到安裝完成，最後讀 journal 的 SSH 也逾時，所以 VM 內的原因沒有紀錄。首次開機會從 Ubuntu 套件庫和 snap store 安裝桌面套件與 Firefox，再載入產品映像，產品本身不設時限。重跑時在 harness 外每 5 分鐘用 SSH 問 VM：每次都在 7 秒內回應，首次開機約半小時完成，安裝驗收通過。
- 家庭現場開發助手：要突然停止 App 之前，VM 內的一個 fixture 指令以 1 結束。harness 只保留結束碼，看不出是哪一個指令；同一次的取消和正常重開都通過。

本候選第一次執行時，有三個階段沒有通過：家庭現場開發助手、家庭現場 App 建置、智能管家側欄 App。同一個 commit 與媒體重跑後都通過。

- 家庭現場開發助手（[結果](../evidence/home-worker-haos-0.9.0-rc10-run1-failure.json)、[JUnit](../evidence/home-worker-haos-0.9.0-rc10-run1-failure.xml)）：開發工作在開始執行前停在「停止未確認」，harness 等確認停止 280 秒後判定失敗。家庭現場的 broker 會先記下要啟動 sandbox；啟動失敗又無法證明沒有程序留下時，會照設計擋住這個工作，不會回報已停止。harness 只保留白名單內的原因，所以看不出是哪一步失敗，#97 追蹤保留這個原因。這段期間沒有取消或授權變動；上次通過後唯一的產品改動（#95）在一鍵啟用的維護流程，這個測試不會用到。這個階段等磁碟 45 分鐘逾時後才開始，開發工作期間 IO 壓力（full avg60）在 51 到 81 之間，其他專案正在跑測試。Docker 刪容器也超過 harness 的清理時限：外層容器和網路由 Docker 自己刪除，停在 dead 的 VM 容器由整合者刪除。
- 家庭現場 App 建置（[結果](../evidence/runner-supervisor-build-0.9.0-rc10-run1-failure.json)、[JUnit](../evidence/runner-supervisor-build-0.9.0-rc10-run1-failure.xml)）：真 Supervisor 用 73 秒建好 App；harness 觀察安裝時讀 App 清單，Supervisor 回了 `unknown_error`。harness 把 Supervisor 的錯誤當成答案，不會重讀。之後讀 App 資訊和用 SSH 讀 Supervisor log 也都失敗，所以 Supervisor 的訊息沒有紀錄，#98 追蹤重讀與保留訊息分類。App 原始碼（git tree `cdf5d405`）和上一個候選相同，那次這個階段通過。IO 壓力（full avg60）在階段開始時約 7，安裝期間升到 49 到 82，其他專案正在跑測試。清理全部完成。
- 智能管家側欄 App（[進度](../evidence/bridge-haos-local-0.9.0-rc10-run1-failure-progress.json)、[JUnit](../evidence/bridge-haos-local-0.9.0-rc10-run1-failure.xml)）：測試本體在 770 秒進入最後一步（手動停止後維持停止），JUnit 只有收尾的錯誤，本體沒有出錯。收尾時刪外層容器的 `docker rm` 超過 harness 的 30 秒；harness 沒有接住這個逾時，所以後面的清理和寫結果都沒做，只留下進度檔。外層容器之後由 Docker 自己刪除，VM 容器和網路由整合者刪除。#99 追蹤比照其他 VM 測試，逾時後照樣清完並寫下結果。IO 壓力（full avg60）在階段開始時約 9，執行期間在 20 到 78 之間，其他專案正在跑測試。

固定模型 transport、發行者／registry 與 Unix Host 能力回應都是明示的 fixture。下列項目尚未驗收：

- 真雲端帳號與實體筆電。
- 側欄與家庭現場 App 經 `#stable` 公開安裝，以及 Supervisor 從公開來源建置家庭現場 App。
- Claude／Antigravity／Grok coding。
- 家庭 E1／E2。

32 個產品驗收案例仍為 2 passed、30 not_run。

發布前的 Gitleaks [來源稽核](../evidence/source-audit-0.9.0.json)通過。1290 份公開檔案的 415 個 raw findings 都已逐一核對：413 個是可對回公開 Git blob 或封存檔的 SHA-256，2 個是測試裡的合成 fixture 值。沒有找到憑證。

## 0.8.0 Home coding 原始碼／App 預覽

本輪加入真正 HAOS App 內的 Home coding worker，沿用 Controller／TLS Runner 派工、獨立登入、同 case 證據和工作區交接。首個 coding provider 為固定版本 Codex；模型程序使用私有 namespace、cgroup、磁碟配額及受控 relay，正式家庭操作仍由既有受控流程執行。實作與平台測試進度見 [Workers](WORKERS.md)及 [Plan v3 帳本](v3/PROGRESS.md)。

候選來源 [`c19610ae`](https://github.com/teddashh/agentic-smart-home/commit/c19610ae52322066f6d25e2d88c57f040de29641) 的本機完整 `--real-ha --containers` 回歸為 **1116 passed、5 skipped／385.46 秒**，見 [JUnit](../evidence/pytest-0.8.0.xml)。五個跳過項是獨立 HAOS 邊界、備份、側欄 App、Home worker 及完整 Linux 安裝案例；Home worker 接著另跑真平台測試，沒有把跳過算成通過。Home 映像內的研究來源固定為 [`bf831fc8`](https://github.com/teddashh/agentic-smart-home/commit/bf831fc8e6082c886b988f4e1cbdfecab5238380)，由獨立 lock 記錄，與 App 執行碼來源分開追溯。

同一來源的 [GitHub CI 38330431861](https://github.com/teddashh/agentic-smart-home/actions/runs/38330431861) 為 **1114 passed、7 skipped／435.462 秒**，見 [CI 與來源核對](../evidence/github-ci-0.8.0-runtime.json)及 [JUnit](../evidence/pytest-0.8.0-runtime-github.xml)。GitHub artifact digest、source ZIP 的 1073 個 manifest 項目，以及 203 份執行碼／配置／編譯資產均核對成功；Bridge 與 Controller UI 相同。CI 額外跳過 UID 1000 桌面專項及本機 QEMU builder 探針；獨立建置的映像 ID 不宣稱與本機相同。

最終 harness 來源 `7b2c4bd29d78f7f2f61e4e52d16569b8b08b20a0` 的 [GitHub CI 38332485177](https://github.com/teddashh/agentic-smart-home/actions/runs/38332485177) 為 **1119 passed、7 skipped／468.929 秒**，見 [最終 CI 核對](../evidence/github-ci-0.8.0.json)及 [JUnit](../evidence/pytest-0.8.0-github.xml)。Artifact digest、1078 項 source ZIP manifest 與 203 份 runtime／配置／編譯資產均核對成功，且與本機來源相同。相較上輪只增加重啟觀測的 harness 測試，產品執行碼未改。收尾文件與平台證據另行提交；精確受測來源、映像及限制見 [0.8.0 交付紀錄](../evidence/release-0.8.0.json)。

本版沒有重建或驗收整套 Linux ISO。下方 0.7.0 媒體保留原始來源與 hash，不包含本次 Home worker。真 HAOS 工程測試使用產品映像衍生 App，另加明示的模型 transport／診斷 fixture；不能代替真帳號、自主研究品質、家庭 helpers 或更新／退版的 E2 產品驗收。

**真 HAOS Home worker 第十二輪完整通過，324.151 秒**，見 [平台結果](../evidence/home-worker-haos.json)及 [JUnit](../evidence/home-worker-haos.xml)。平台為 HAOS 18.3／Core 2026.10.1／Supervisor 2026.09.3；來源／harness 為 `7b2c4bd29d78f7f2f61e4e52d16569b8b08b20a0`，runtime／映像沿用 `c19610ae`，無未提交執行碼。JUnit 的 **54 passed** 是一個真 VM 整合案例加 53 個 harness 單元測試。整合包含真 Codex 工具修改來源、38 項獨立基準測試、30 項 worker 邊界檢查及 15 項整合檢查；Controller 經真正配對 TLS 取得同 case 產物與內容指紋。

App 內實際觸發磁碟 ENOSPC、CPU throttling、PID 上限及 OOM；TLS 取消 1.349 秒後確認子程序、FUSE、cgroup 與工作磁碟已回收。正常停止／重啟不重播；突然終止／重啟以新的程序 start ticks、Docker StartedAt 及核對過的 App 身份證明新程序已啟動，namespace 編號可重用。原工作仍為 `stop_unconfirmed`、保留 lease，新工作未啟動、模型請求未重播。此限制不能靠重新開頁或重啟 App 繞過。outer／VM／network 移除及磁碟 ownership 恢復四項清理均成功。

第八至十一輪的登入目錄、verifier 路徑、Core 連接埠及重啟觀測失敗均保留原始 receipt，沒有回填成功。詳細原因見 [Workers](WORKERS.md)；接續位置、來源與交付步驟見 [開發交接](HANDOFF_HOME_WORKER_0.8.md)。32 個產品驗收案例仍為 2 passed、30 not_run。

## 0.7.0 側欄整合候選

本輪新增薄 HAOS Ingress App、同一外層對話／case、即時 Owner 驗證、有限維護與配對恢復；首次 Hermes 改用隨附 digest。工程與產品範圍見 [整合審查](reviews/2026-10-12-INGRESS-BRIDGE.md)。HA 保留官方更新，Home coding、原生 Assist、App 跨版本 migration 與真帳號仍需另驗。

目前受測產品來源為 `cd0fe57725138da00bf67537f68f3fb9a0d7b150`。GitHub [CI 38304072611](https://github.com/teddashh/agentic-smart-home/actions/runs/38304072611) **942 passed、6 skipped／413.130 秒**，見 [CI 記錄](../evidence/github-ci-0.7.0.json)及 [JUnit](../evidence/pytest-0.7.0-github.xml)。Artifact digest 與 source ZIP 的 990 個 manifest 項目均已核對；新 ISO bundle 的 **149 份執行／設定檔及 49 份文件**逐檔吻合 CI，六份生成資產另由 build manifest 核對，見 [來源比對](../evidence/ci-media-match-0.7.0.json)。獨立 CI 的 image IDs 不宣稱與本機相同，六個跳過項仍需各自實測。

**精確 0.7.0 ISO 完整安裝：1 passed／2288.830 秒**。媒體、安裝 harness 與上述 CI 的產品來源均為 `cd0fe57725138da00bf67537f68f3fb9a0d7b150`。一次性 VM 完成 Ubuntu 安裝、outer-only firstboot、五映像與來源核對、桌面單次登入／角色權限、Controller 停止救援、sandbox、真 Codex 工具與獨立 verifier、隨附 Hermes 相容性檢查／啟用、HAOS 真 Core 就緒、Controller 更新／保留新資料退版、冷快照還原與整機重啟。見 [安裝結果](../evidence/linux-install-0.7.0.json)、[JUnit](../evidence/linux-install-0.7.0.xml)及 [交付記錄](../evidence/release-0.7.0.json)。VM 容器及磁碟 basetemp 已確認移除，未在開發主機安裝產品。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.7.0-laptop-amd64.iso` | 6568214528 | `9c6f6d3e54a89454137a8db4760208d856cdfbb07d8bc7322d18bac5b2928c94` |
| `agentic-home-0.7.0-linux-amd64.tar` | 2488709120 | `c91cc1cda95b3257bd88c94abd49ac5208c40d72dca9a4149fc3194755ac31bd` |

[已安裝 Host 的角色驗證](../evidence/installed-auth-laptop-amd64-0.7.0.json)讀回真正固定更新契約 1；[更新／退版](../evidence/installed-update-laptop-amd64-0.7.0.json)在真安裝環境執行三次固定 bridge probe，支援候選通過、缺契約候選拒絕、無 bridge 歷史控制組通過，清理均完成。缺契約候選是當前映像的測試衍生版，資料是隔離的合成 revoked bridge 歷史；不是歷史發行版。另有實際 0.6.1 映像的拒絕測試，見下文。Controller 更新使用合成發行者／registry delivery，不能當成全產品或 Linux／App 跨版本更新驗收。

[安裝 Hermes](../evidence/installed-hermes-laptop-amd64-0.7.0.json)以隨附 digest 啟用並在重啟後維持；[Codex coding](../evidence/installed-coding-laptop-amd64-0.7.0.json)使用真 CLI／工具與固定模型 transport，沒有真雲端帳號。[公共 ISO seed](../evidence/public-seed-0.7.0.json)保留網路、磁碟和 Linux 身份選擇，沒有內嵌密碼／SSH key。TAR 未另驗既有主機就地安裝，大型媒體保留於本機 `dist/`。T3-01／T3-05 只更新 E0 工程安裝／救援證據，另外 30 個產品案例維持 `not_run`；實體 laptop、真 OAuth／bot、Home App coding、原生 Assist、App 跨版本切換和受控舊 Host 就地更新仍未完成。

最終更新審查另發現 **舊 Host＋新 Controller** 的能力落差：舊固定更新器不知道 bridge 授權，因此 Controller 必須先驗證真正 Host 的更新契約。下文標示 gate 前的結果不能視為新 gate 已驗證。第一份 0.7.0 ISO 在 Ubuntu 安裝完成、firstboot 階段由整合者主動中止，準備以修正後媒體重驗；[中止記錄](../evidence/linux-install-0.7.0-before-host-gate.json)保存原因、原媒體 hash 與清理結果，沒有標記 passed。

Host gate 修正後，本機產品回歸 **790 passed、3 skipped／346.04 秒**，見 [JUnit](../evidence/pytest-0.7.0-host-gate.xml)，包含 `--real-ha --containers`。這輪排除同步補強的兩個安裝測試檔；兩檔另行 **74 passed、1 skipped／1.02 秒**，見 [安裝驗證腳本](../evidence/installed-bridge-contract-unit.xml)。合計範圍涵蓋全部本機回歸；四個 VM 案例需另行執行。

來源 `9146b38`、ISO `44aaa2ba…` 的首輪完整安裝為 **1 failed／1116.21 秒**，失敗於建立 HAOS。Ubuntu、真正 Host 能力握手、角色登入、Controller 停止救援、sandbox、真 Codex 工具與隨附 Hermes 準備已通過；更新／退版與後續 HAOS 檢查尚未執行。見 [失敗 JUnit](../evidence/linux-install-0.7.0-provision-failure.xml)及 [保留階段](../evidence/installed-0.7.0-run1-prior-stages/manifest.json)。原診斷未辨識固定 Host 錯誤、也未保存最後相容性檢查，不能據此判定原因。一次性 VM 與磁碟目錄已清除；補強診斷後沿用相同媒體重驗，不放寬產品或測試期限。

補強後的 [安裝診斷回歸](../evidence/installed-failure-diagnostics-unit.xml)為 **101 passed、1 skipped／1.178 秒**。保留固定 Host 錯誤、各相容性檢查的原因與最後 action，唯讀核對產品 VM／網路 ownership 和固定磁碟狀態；不公開原始日誌、XML、路徑或帳號資料。最後 action 可能為 `start`，因為 provision 後段會執行啟動檢查；診斷不重跑檢查覆蓋原證據。這輪只改 harness，不改 ISO 或產品程式。

同一 ISO、harness `2f838451` 的第二輪為 **1 failed／1511.09 秒**。這次 HAOS provision／真 Core 就緒、Controller 更新／同 DB 退版及三次安裝後 bridge 固定 probe 均通過，接著 `agentic-home snapshot` 在輸出工作 ID 後，讀取狀態逾時而退出。新診斷確認 exact snapshot job 仍為 `running`、HAOS `shut off`、domain／network／disk ownership 相符，不能宣稱快照失敗或完成；尚未還原／重啟。見 [JUnit](../evidence/linux-install-0.7.0-snapshot-read-failure.xml)與 [保留階段](../evidence/installed-0.7.0-run2-prior-stages/manifest.json)。這揭露 CLI 用整份 `/status` 追蹤工作、暫時讀取失敗未續查的產品缺口；原 job 不重送，接著補精確工作唯讀接點與有界續查。測試 VM、容器及磁碟目錄已清理。

已新增 Host `GET /jobs/{id}` 與 CLI `job --id`／`wait --id`。輕量查詢沿用 UID 1000 邊界，不執行整份系統狀態探測；POST 僅一次，回應遺失仍保留 ID，GET 暫時失敗最多嘗試三次，單次 socket 仍為 30 秒。舊碼的 [獨立重現](../evidence/host-job-polling-before.xml)為 1 failed；修後 [54 項定向驗證](../evidence/host-job-polling-unix.xml)通過（含真正 Unix socket／UID 邊界、完整 status 故障時仍可查原 job、重啟 needs_reconcile、錯 ID／request／格式拒絕），測試容器已清理。既有 [Host／更新器／診斷回歸](../evidence/host-job-polling-regression.xml)為 **215 passed、4 skipped／5.60 秒**。這項產品修正需要新 Host bundle／ISO；先前媒體的測試不能當作新版完整安裝已通過。

Host gate 前的完整回歸為 **814 passed、4 skipped／424.07 秒**，見 [封存 JUnit](../evidence/pytest-0.7.0-before-host-gate.xml)。四個 skipped 是需另行啟用的 HAOS App boundary、HAOS 備份、薄 App、Linux ISO VM 案例。協定 fixture 與真服務分別標記；本節不沿用下方 0.6.1 的 passed 或 ISO 證據充當新版結果。

第一輪為 **802 passed、4 skipped、1 failed／497.01 秒**；sandbox deadline 案例等待 Docker 結束逾時，見 [原始結果](../evidence/pytest-0.7.0-deadline-failure.xml)。原次沒有保存容器停止狀態，原因未確定。新增有界診斷後定向測試 **23 passed**，最終全套也通過；[deadline](../evidence/coding-boundary-deadline.json) 實測約 10 秒退出、所有子程序消失、清理完成，沒有放寬 10 秒 watchdog 或 20 秒控制等待限制。

定向證據另有 [薄 App](../evidence/bridge-app.xml)、[Controller／修復](../evidence/bridge-controller-repair.xml)、[維護](../evidence/bridge-maintenance.xml)、[UI](../evidence/bridge-ui.xml)、[官方 Supervisor schema](../evidence/bridge-supervisor-schema.xml)及 [App 容器生命週期](../evidence/bridge-container.xml)。

GitHub 首次乾淨安裝揭露 setuptools 將 `static` 目錄誤認成 namespace package。已限制為三個真正的 `agentic_*` 模組；兩份獨立 git archive 的 build／frozen sync 均通過，見 [失敗與修正](../evidence/github-ci-0.7.0-packaging-failure.json)。gate 前的修正版來源為 `5bf6babab1b9f23923c8352bd3d497288dd5c3fe`，GitHub [CI 38289833372](https://github.com/teddashh/agentic-smart-home/actions/runs/38289833372) **812 passed、6 skipped／486.541 秒**，見 [封存記錄](../evidence/github-ci-0.7.0-candidate.json)與 [JUnit](../evidence/pytest-0.7.0-github-candidate.xml)。CI 另跳過 UID 1000 專項桌面測試及需本機 builder 的 QEMU 磁碟探針。來源 ZIP 的 918 個 manifest 檔案均核對通過；映像由 CI 獨立建置，不宣稱與本機 digest 相同。

Host 工作續查修正前的媒體來源 `9146b38f6cbe04c0d3cfdc221b7b397a4f40261f` 的 GitHub [CI 38295508318](https://github.com/teddashh/agentic-smart-home/actions/runs/38295508318) **862 passed、6 skipped／486.682 秒**，見 [封存記錄](../evidence/github-ci-0.7.0-before-job-polling.json)與 [JUnit](../evidence/pytest-0.7.0-github-before-job-polling.xml)。947 個 source ZIP manifest 項目與 artifact digest 全部核對。該份 ISO bundle 中全部可與來源對應的 **149 份執行／設定檔與 49 份文件**也逐檔吻合 CI，見 [封存來源比對](../evidence/ci-media-match-0.7.0-before-job-polling.json)；六份生成資產另由 build manifest 核對，CI 映像 ID 不宣稱與本機相同。

診斷 harness `2f83845117cd1ebd90273c67e0e75fb3a10241d0` 的 [CI 38299730431](https://github.com/teddashh/agentic-smart-home/actions/runs/38299730431) 為 **889 passed、6 skipped／538.456 秒**，見 [獨立記錄](../evidence/github-ci-0.7.0-diagnostics.json)及 [JUnit](../evidence/pytest-0.7.0-diagnostics-github.xml)。Artifact digest 與 source ZIP 的 965 個 manifest 項目均吻合。產品程式／前端與 `9146b38` 媒體一致，差異只在測試、文件與證據；六個跳過項仍需各自實測。

真正 **HAOS 18.3／Core 2026.10.1／Supervisor 2026.09.3** 第三輪本地來源安裝 **7 passed／295.10 秒**，見 [結果](../evidence/bridge-haos-local.json)、[JUnit](../evidence/bridge-haos-local.xml)及 [原生側欄截圖](../evidence/ui/bridge-native-haos-local.png)。包含真 Supervisor 安裝、TLS 指紋錯誤拒絕、偽造身份拒絕、Owner 瀏覽器登入、CSRF、重送去重、同 case 續聊、App 重啟保留與人工停止不復活。模型使用明示協定 fixture，沒有雲端帳號；公開 GitHub source delivery 見下方；新版完整 ISO 安裝結果見本節開頭。

Host gate 修正後的公開 GitHub source delivery 在全新 HAOS 為 **18 passed／291.89 秒**，見 [結果](../evidence/bridge-haos-public.json)、[JUnit](../evidence/bridge-haos-public.xml)及 [原生側欄截圖](../evidence/ui/bridge-native-haos-public.png)。來源 `9146b38f6cbe04c0d3cfdc221b7b397a4f40261f` 無未提交執行碼，Controller 映像為 `sha256:e15c6ca3af58e123b346dc19daad1a493e68076f8186809cefe35a178cd1ed60`。正式 `host_call` 經真 Unix socket 讀取能力 34 次；Host 回應與模型為明示 fixture，並非已安裝 Host 或雲端驗收。公開 App 安裝、真 Owner／Ingress、CSRF、同 case、原生 browser reload、重啟及人工停止均通過，測試資源已清理。

Host gate 前的公開 GitHub source delivery 以全新 HAOS 完成 **17 passed／253.98 秒**，見 [封存結果](../evidence/bridge-haos-public-before-host-gate.json)、[JUnit](../evidence/bridge-haos-public-before-host-gate.xml)與 [原生側欄截圖](../evidence/ui/bridge-native-haos-public-before-host-gate.png)。正式管理器從 `release/0.7.0` 安裝 `0.7.0` 薄 App，讀回實際 repo／ref／版本及權限；未拷入本機 App source 或預載 App 映像。真 Owner／Ingress／CSRF／對話接續／App 重啟／人工停止通過；模型仍為 fixture，外層使用乾淨 `5bf6bab` 來源及本機候選映像。Supervisor 信任來源分支，不提供原子 commit/digest 安裝保證。

公開模式第一次在 Docker 建立 VM 的 120 秒控制等待逾時，尚未進入 App 安裝；[事件診斷](../evidence/bridge-haos-public-vm-start-diagnostics.json)顯示容器接近期限才建立，逾時後立即被清理。當時 IO pressure 高，沒有證據指出特定來源。I/O 等待回落後 [獨立容器生命週期](../evidence/bridge-haos-builder-control-cycle.json)正常，再以原期限重驗通過；[原始失敗](../evidence/bridge-haos-public-vm-start-failure.xml)保留，沒有修改產品時限或重啟共用 Docker daemon。

前兩輪 HAOS 測試卡在背景 job 讀回：[首次失敗](../evidence/bridge-haos-install-job-failure.json)、[有界診斷](../evidence/bridge-haos-job-readback-failure.json)。第二輪已觀測 App 安裝成功、Supervisor healthy，但 job 被清理；修正改以原安裝意圖與完整 App postcondition 核對，不重送 install。這兩筆失敗原樣保留。

同 schema 的舊 Controller 不理解新配對授權，因此固定 probe 另驗 bridge contract 1。[真映像測試](../evidence/bridge-legacy-candidate.json)確認 0.6.1 被拒絕，新候選通過；只使用去密結構 DB，未改家庭資料。

## 0.6.1 歷史驗證

本輪交付原生 HA 日常入口與上游 adapter 邊界，詳細見 [整合審查](reviews/2026-10-12-NATIVE-HOME-ADAPTERS.md)。資料格式仍為 schema 3；HA 側欄外層 bridge、Home coding、真帳號與完整 OS 更新／退版未由本輪測試建立。

**GitHub [CI 38275228393](https://github.com/teddashh/agentic-smart-home/actions/runs/38275228393)：598 passed、5 skipped／446.91 秒**，來源 `083b80eaa64510a6882b6c33096b0efcb852ba65`，見 [CI 記錄](../evidence/github-ci-0.6.1.json)及 [JUnit](../evidence/pytest-0.6.1-github.xml)。五項跳過為 UID 1000 專項桌面單元、兩個獨立 HAOS VM 案例、需 builder image 的 QEMU 磁碟探針，以及完整安裝 VM；本機 QEMU 探針已通過，完整媒體另驗。CI ZIP 的 800 檔 manifest 與 ISO 內 135 份執行來源已逐檔比對，全數相符，操作文件也一致，見 [CI／媒體核對](../evidence/ci-media-match-0.6.1.json)。

本機全套首次為 **599 passed、1 failed、3 skipped／297.07 秒**；失敗為新增原生頁面測試漏走 HA 自己的瀏覽器登入。補上登入後另發現最小 Core fixture 未啟用 Lovelace，且有 loopback HTTP 設定待確認；測試現依原生畫面完成確認並等待真正 dashboard。修正僅在 harness，未搬移 token 或修改 HA 前端。保留 [初次結果](../evidence/pytest-0.6.1-native-login-initial.xml)及 [迭代範圍](../evidence/native-entry-initial-checks.json)。

修正後真服務 composed E2E **1 passed／43.05 秒**，完成原生登入、真 Core dashboard、裸入口導向、Back／reload 外層返回、明確管家入口及同 case MCP 對話接續，見 [JUnit](../evidence/onboarding-e2e-native-0.6.1.xml)、[服務證據](../evidence/onboarding-e2e.json)及 [真 Core 截圖](../evidence/ui/e2e-native-dashboard.png)。模型與 Host 副作用仍為合成 fixture；不是實體家庭或雲端帳號驗收。

Hermes 的封裝基準 `835c3dd…` 和當時官方最新 `7cc3fb8…` 均完成獨立候選建置與 contract 1 測試，沒有啟用候選，清理已確認。見 [locked](../evidence/upstream-locked-0.6.1.json)、[latest](../evidence/upstream-candidate-0.6.1.json)。負向測試涵蓋上游符號缺失、必要參數改變、壞回應、契約版本不合與下載失敗。UI 的 [三尺寸截圖](../evidence/ui/native-home/README.md) 使用 Chromium／合成 API 狀態，與真 Core 截圖分開標示。

[獨立上游候選 CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38275258431) 也已通過，精確來源與清理結果見 [報告](../evidence/upstream-candidate-0.6.1-github.json)。**精確 0.6.1 ISO 完整安裝：1 passed／1568.186 秒**，媒體、installer harness 與 CI 執行來源均為 `083b80eaa64510a6882b6c33096b0efcb852ba65`。一次性 VM 完成 Ubuntu 安裝、outer-only firstboot、五映像／來源核對、一次性桌面登入／重播拒絕、角色權限、Controller 停止救援、sandbox、真 Codex 工具／獨立 verifier、最新 Hermes、HAOS 啟動、Controller 更新／同 DB 退版、冷快照還原及整機重啟。見 [安裝結果](../evidence/linux-install-0.6.1.json)、[JUnit](../evidence/linux-install-0.6.1.xml)與 [交付記錄](../evidence/release-0.6.1.json)。測試容器、VM 及磁碟 basetemp 已清理，未在開發 host 安裝產品。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.6.1-laptop-amd64.iso` | 6561529856 | `ac434f55bfa66be57fd86dbd52772894189b0e0a2778dd93e5ec8c9d461e6a60` |
| `agentic-home-0.6.1-linux-amd64.tar` | 2482012160 | `da0a76ef596f239bcdfb6c98bb8f927880a2c01e3ad86a9f1a05f461ca514ab8` |

安裝內的最新 Hermes 為 `7cc3fb8cb55898747dd1dfa56fda3e16ebba028e`，實際通過 adapter contract 1 gate、啟用、版本讀回與重啟持續性，見 [Hermes 記錄](../evidence/installed-hermes-laptop-amd64-0.6.1.json)。[Linux 能力觀測](../evidence/installed-host-compatibility-laptop-amd64-0.6.1.json)另外保存還原執行期間的唯讀 preflight 結果；該筆觀測的 restore 仍為 running，最終成功由完整安裝結果證明。

[Controller 更新／退版](../evidence/installed-update-laptop-amd64-0.6.1.json)實際驗證簽章、受限 probe、Compose 切換、Owner smoke、schema 3 snapshot 及保留新資料退版。發行者、registry delivery、只改 image label 的候選與新增資料是合成 fixture；不是全產品或 OS 升級驗收。Codex 使用真 CLI／工具與固定模型 transport，沒有真雲端帳號。

[公共 ISO seed](../evidence/public-seed-0.6.1.json)保留網路、磁碟與 Linux 身份選擇，沒有預選清除磁碟或內嵌密碼／SSH key。TAR 未另測既有主機就地安裝；大型媒體留在本機 `dist/`。T3-01／T3-05 僅更新 E0 工程證據，另外 30 案維持 `not_run`。公開檔案與候選來源核對見 [source audit](../evidence/source-audit-0.6.1.json)。下列 0.6.0 成果保留為歷史。

## 0.6.0 歷史驗證

使用者要求對話優先後，主選單已改為五项，進度與成果留在智能管家同一段對話，技術設定收起。[新版24張截圖](../evidence/ui/chat-first/README.md) 已檢视桌面／平板／手機。
**最新 GitHub [CI 38266420366](https://github.com/teddashh/agentic-smart-home/actions/runs/38266420366)：469 passed、4 skipped／392.548 秒**，測試來源 `c6cff8fbebf087665a592484dd5587227a3e62f4`，見 [CI 記錄](../evidence/github-ci-0.6.0.json)及 [JUnit](../evidence/pytest-0.6.0-github.xml)。包含真 Core、CLI 容器與瀏覽器回歸；四項跳過為 UID1000 桌面單元案例和三個獨立大型 VM 案例。媒體來源為 `6847a03c8f5d1ad2a100df76eaaf8f3eca323794`；兩者差異只有測試 fixture、文件與證據。CI ZIP 的 680 檔 manifest 全部核對，精確媒體內 131 份執行來源／編譯資產完全一致，僅驗證文件新增後續紀錄，見 [來源核對](../evidence/ci-media-match-0.6.0.json)。

**精確 0.6.0 ISO 完整安裝：1 passed／1789.412 秒**，媒體與 installer harness 均為 `6847a03c8f5d1ad2a100df76eaaf8f3eca323794`。一次性 VM 完成 Ubuntu 24.04.5、firstboot、五映像與來源核對、單次桌面登入／重播拒絕、角色權限、Controller 停止救援、ext4 quota／停止隔離、真 Codex 工具修改與兩項獨立 verifier、最新版 Hermes、官方 HAOS、Controller 更新／同 DB 退版、冷快照還原及外層重啟自動啟動。見 [安裝結果](../evidence/linux-install-0.6.0.json)、[JUnit](../evidence/linux-install-0.6.0.xml)、[交付記錄](../evidence/release-0.6.0.json)。測試 VM、容器與唯一磁碟 basetemp 已刪除，未在開發 host 安裝產品。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.6.0-laptop-amd64.iso` | 6553468928 | `ed0686dabba1e267a4bddbbf1f22d62397484b95ac00679868716c26bf3856ce` |
| `agentic-home-0.6.0-linux-amd64.tar` | 2474004480 | `b0b087533c1299e2f169165bb0ca80a0d098e4ce14ee6fdc82c3e45b17808be5` |

官方最新 Hermes 在這次安裝解析為 `7cc3fb8cb55898747dd1dfa56fda3e16ebba028e`；建置、啟用、runtime revision 讀回及重啟持續性通過，見 [Hermes](../evidence/installed-hermes-laptop-amd64-0.6.0.json)。[更新測試](../evidence/installed-update-laptop-amd64-0.6.0.json)實際執行簽章驗證、受限 compatibility probe、compose activation、Owner smoke、schema 3 私有 snapshot 與保留更新後資料的退版；durable systemd 工作 exit 0、cleanup 完成。發行者是臨時 fixture、候選只改 image label、registry 預載及新增資料為合成案例，不宣稱公開發行或跨 schema／全產品更新。

[Codex coding](../evidence/installed-coding-laptop-amd64-0.6.0.json)使用真正 CLI／工具與固定模型 transport，沒有真雲端登入。三次 HA 就緒以原生 onboarding API JSON 核對；未自動代填原生家庭帳號，亦未實作 Home App coding。公共 ISO 的 [seed](../evidence/public-seed-0.6.0.json)保留網路、磁碟及身份選擇，沒有內嵌密碼或 SSH key。TAR 未另做既有主機就地安裝；Owner laptop、真 bot／OAuth／Tailscale 帳號、實體設備仍待驗。大型媒體留在本機 `dist/`，不存入 Git。

使用者後續提出以 HA 原生 Web 作為日常首頁；[介面方向評估](ONBOARDING.md#原生家庭首頁與智能管家2026-10-12-介面方向評估)已記錄，尚未改動受測產品。聊天版截圖是目前介面，不代表已交付原生首頁導向或 HA 側欄 Hermes bridge。


**對話版完整本機回歸：409 passed、3 skipped／248.34 秒**，見 [完整 JUnit](../evidence/pytest-chat-first-final.xml)。三個 skipped 都是需另行啟動的 HAOS App 探針、HAOS owner／Supervisor 備份，以及 Linux 安裝 VM。後續安裝發現的可信測試目錄權限問題已修正，定向 **41 passed／27.58 秒**，見 [JUnit](../evidence/coding-prepare-permissions.xml)；修正後 CI 如上，精確媒體另行完整安裝。

最新瀏覽器與 composed E2E 回歸 **13 passed／134.90 秒**，見 [JUnit](../evidence/assistant-browser-final.xml)。保留登入、精確家庭批准、草稿與重試、provider／runtime／Hermes scope，另覆蓋無 thread 舊紀錄唯讀、送出中禁止切換，以及成果 exact case 核對。前一輪完整 suite 為407 passed、3 skipped、1項 Chromium capture失敗（[原始紀錄](../evidence/pytest-chat-first.xml)）；已加入 resize 後 layout frame 等待，最終完整回歸通過。另一次與 image 匯出並行的回歸因兩個 Docker create 超時而中止，保留 [原始紀錄](../evidence/pytest-chat-first-build-contention.xml)；清理晚到容器並依序執行後，全套通過，未放寬產品限制。

權限修正前的 GitHub [CI 38243649110](https://github.com/teddashh/agentic-smart-home/actions/runs/38243649110) 已通過 **414 passed、4 skipped／359.433 秒**，來源為 `09acfb757a4e370ed4456d672434f21494b6bff9`，含新增六項安裝 probe 契約檢查；見 [CI 記錄](../evidence/github-ci-0.6.0-before-trusted-test-permissions.json)、[JUnit](../evidence/pytest-0.6.0-github-before-trusted-test-permissions.xml)。CI 比本機多跳過一個限定 UID 1000 的桌面單元案例；其餘三項為獨立大型 VM 測試。

舊媒體來源為 `acc44fc78b51c01416e83f40163a415ffae3ef19`；當時較新的 CI commit 僅修正安裝測試與保存證據。CI ZIP 的 596 檔 manifest 全數核對，媒體內 131 份非 Markdown 執行來源／編譯資產均與當時 CI 產物相同，隨附操作文件也一致，見 [歷史核對](../evidence/ci-media-match-0.6.0-before-trusted-test-permissions.json)。這份舊媒體未通過下述獨立 verifier 安裝測試，不能作為最終交付。兩地 Docker image 是獨立建置，來源核對不宣稱 image IDs 相同。

本輪將 0.5.1 審查的工程缺口接入產品，並依實際 Chromium 截圖改善 onboarding 與成果畫面。模型 transport fixtures 與真帳號驗收分開記錄。

精確 ISO 首次安裝測試在 **1152.82 秒**後於安裝後狀態斷言失敗，見 [失敗摘要](../evidence/linux-install-0.6.0-initial-failure.json)及 [原始 JUnit](../evidence/linux-install-0.6.0-initial-auth-failure.xml)。Linux／firstboot、五個 image IDs、來源 bundle、Tailscale、單次登入與重播拒絕、Owner 登入和 Runner 配對均已通過；coding、HAOS 與更新／退版尚未開始。舊 probe 仍要求所有 workers 都不可用，與 0.6 Host 的「隔離環境已就緒、角色尚未登入」契約衝突。原紀錄未包含確切斷言位置，不能回溯排除同段 Hermes 狀態曾異常。新版 probe 增加安全階段診斷、精確 Host／Home 契約，以及未登入派工必須 blocked、未執行、無 lease 和重送不重複的檢查；[六項回歸](../evidence/installed-probe-contract-0.6.0.xml)通過。產品與 ISO 均未修改；完整安裝結果另記。

同 ISO 第二輪為 **1 failed／1099.57 秒**：上述狀態與未登入拒絕派工檢查通過，Controller 停止救援、離線 sandbox、真 ext4 ENOSPC 與 Codex 工具修改／自身測試也通過，但獨立 verifier exit 1。正式 broker 的 `umask 0077` 使 `mkdir(mode=0755)` 實際建立 root-only 0700 目錄，UID 10000 無法讀取可信測試。已在固定 Runner 重現；修正只於複製完成後明確設定目錄 0555，檔案仍為 0444。新增回歸呼叫真正的 prepare 方法（只替代掛載 plumbing），確認 UID 10000 能讀、不能寫，並完成兩項獨立測試，見 [權限證據](../evidence/coding-prepare-permissions.json)。原安裝 probe 沒有保存 verifier log，這個原因是後續獨立重現；新版另補僅含布林值與完成計數的診斷。保留 [第二輪摘要](../evidence/linux-install-0.6.0-coding-verifier-failure.json)、[JUnit](../evidence/linux-install-0.6.0-coding-verifier-failure.xml)與 [舊媒體](../evidence/build-0.6.0-before-trusted-test-permissions.json)，修正後須重新封裝並驗證。

第三輪使用 `ead5a3051a9b87f0e81295c8d3ca1e79455883c2` 新媒體，**1 failed／1082.78 秒**。實際 Codex 修改、兩項獨立 verifier 測試、停止／接手和清理已通過，但初始化最新 Hermes 立即失敗，狀態保持 bundled。安裝器產生的 `runtime.env` 第一行註解被共用環境檔 writer 拒絕，且發生於 bootstrap 狀態建立之前；已用安裝器原始 literal 獨立重現。修正接受空白與獨立註解行，仍拒絕未知欄位、不合法 digest、重複欄位；bootstrap／updater／診斷回歸 **56 passed／8.57 秒**。見 [第三輪摘要](../evidence/linux-install-0.6.0-hermes-bootstrap-failure.json)、[真 coding 成功](../evidence/installed-coding-0.6.0-before-bootstrap-env.json)、[重現](../evidence/bootstrap-env-reproduction.json)及 [修正測試](../evidence/bootstrap-initial-environment.xml)。原始安裝 CLI 輸出未匯出，新 harness 已加固定階段與允許清單錯誤碼，不輸出帳號或任意 log。HAOS 與 updater 尚未執行，完整安裝須以修正後媒體重跑。

這次環境檔修正前，同一 `ead5a30` 來源的 [GitHub CI 38246275498](https://github.com/teddashh/agentic-smart-home/actions/runs/38246275498) **415 passed、4 skipped／364.354 秒**；[來源核對](../evidence/ci-media-match-0.6.0-before-bootstrap-env.json)確認 607 檔 ZIP manifest、131 份媒體執行來源與操作文件均一致。此 CI 及 [媒體](../evidence/build-0.6.0-before-bootstrap-env.json) 保留為修正前證據，不能替代下一版完整安裝。

第四輪使用修正版 `a67fab69…`／ISO `9e4b4824…`，**1 failed／1605.11 秒**。Ubuntu、真 Codex 與兩項獨立 verifier、官方最新 Hermes `3f2abce7f468cf194bb7f9ff9c6a033b075d834c`、HAOS 原生 API／冷快照還原／外層重啟／Hermes 版本保留均已通過；最後更新探針 stdout 為空，harness 在解析 JSON 時失敗。原退出碼與 stderr 未保存，不能判定產品故障或假定更新／退版成功。精確 probe 在 Python 3.12 的匯入與隔離環境 guard 可執行，VM 簡短 console 不能排除程序或 SSH 中斷。見 [第四輪摘要](../evidence/linux-install-0.6.0-update-probe-failure.json)、[JUnit](../evidence/linux-install-0.6.0-update-probe-failure.xml)、[階段紀錄](../evidence/installed-progress-0.6.0-update-probe-failure.json)及 [已通過階段](../evidence/installed-0.6.0-run4-prior-stages/manifest.json)。已清除 VM 與磁碟；接著只修測試結果保存／續讀，產品來源與 ISO 不變。測試端改為具 root／一次性 hostname／磁碟 serial／虛擬化 guard 的單次 systemd 工作，600 秒 watchdog，私有階段與結果檔案；SSH 後續只讀取結果，不重送更新。完成需要原有 updater 斷言、清理證據及程序正常退出，逾時或不完整結果仍失敗。[78 項定向回歸](../evidence/installed-update-harness.xml)通過（含遺失回覆、重複啟動、guard、遮蔽與結果契約），原第四輪原因仍未分類。

第五輪沿用相同 `a67fab69…`／ISO `9e4b4824…` 及上述新 harness，**1 failed／1620.75 秒**。真 Codex 與獨立測試、最新 Hermes `eefdfe0692207a7520b3a8c764fe510024f552a1`、首次 HAOS 原生 API 及關機後外層 HTTP 都已通過；`agentic-home snapshot` 回傳 exit 1，更新探針尚未執行。generic command 的 stdout／stderr 和 broker job receipt 未於 teardown 前寫入證據，因此原因仍未分類；上一輪相同 ISO 曾通過快照，不能據此認定隨機失敗。見 [第五輪摘要](../evidence/linux-install-0.6.0-run5-snapshot-failure.json)、[JUnit](../evidence/linux-install-0.6.0-run5-snapshot-failure.xml)及 [已通過階段](../evidence/installed-0.6.0-run5-prior-stages/manifest.json)。接著補所有階段的有界診斷，並將更新／退版檢查移到首次 HAOS 就緒後；仍完整保留快照、還原、重啟與版本持續性檢查，不將 CLI 失敗視為成功，也不重送不確定的操作。 新增固定工作狀態、程序退出碼、磁碟容量與有界錯誤特徵；auth／sandbox／coding 先保存程序診斷再解析結果，診斷寫入失敗也不能阻止清理或蓋掉原錯誤。[95 項定向回歸](../evidence/installed-global-diagnostics.xml)通過（6.70 秒），含真測試流程的清理故障路徑；所有外部副作用在該定向案例中均被替代。

第六輪同 ISO、harness `53260e1…` 為 **1 failed／1466.27 秒**。Linux、角色權限、救援、真 Codex 與兩項獨立測試、最新 Hermes `f5a6651231cf67031d9e458e45328efe239c628b`、首次 HAOS 原生 API 均通過。持久更新探針已真正執行，簽名與候選 staging 完成，但在 `approved_candidate_activation` 步驟回報 updater failed；systemd 證明 exit 1，cleanup started／completed 皆為 true。該步驟名稱不證明候選已啟用，內部 phase 未保留；一般錯誤碼又被 probe allowlist 遮成未分類。快照和重啟尚未執行。見 [第六輪摘要](../evidence/linux-install-0.6.0-run6-update-activation-failure.json)、[持久結果](../evidence/installed-update-0.6.0-run6-failure.json)、[安全診斷](../evidence/installed-failure-0.6.0-run6.json)及 [JUnit](../evidence/linux-install-0.6.0-run6-update-activation-failure.xml)。後續以同一實際 Controller 映像、UID 1000、umask 0077 與產品 probe 命令，獨立重現兩個缺陷：`python /fixed-probe.py` 沒有 `/app` 匯入路徑；SQLite 交易 context 結束後，source／target 檔案描述符仍存活到 volume.close。這項局部重現只替代 root 磁碟配置／掛載 plumbing，沒有在開發主機掛載，不能冒稱實際 ext4 卸載重現。修正固定 `/app` 路徑，並明確關閉連線且保留 target 交易提交；同兩項測試由 [2 failed](../evidence/updater-probe-prefixed-failure.xml) 變成 [2 passed](../evidence/updater-probe-fixed.xml)。原第六輪沒有保存內部 phase／原始例外，這是獨立重現的產品缺陷，不是回溯取得原錯誤堆疊。另有 [101 項定向回歸](../evidence/updater-probe-regression.xml)通過（9.15 秒），[重現範圍](../evidence/updater-probe-reproduction.json)記錄映像與隔離條件；harness 也保留內部 phase、命令與 probe 原例外的固定診斷，不重試效果。需重新封裝媒體並完成安裝驗證。

新版 Host probe 來源 `6847a03…` 的 [CI 38264318494](https://github.com/teddashh/agentic-smart-home/actions/runs/38264318494) 為 **467 passed、1 failed、4 skipped／380.69 秒**；只有新增的實際 Controller probe fixture 失敗。GitHub 使用 UID 1001，fixture 原本以 0666／0777 模擬 root chown；UID 1000 雖可寫入，仍不能執行 Store 要求的 chmod 0600。已用真映像及 UID 1001 擁有的合成資料獨立重現相同 PermissionError（483 bytes stderr），匯入與描述符清理皆正常，見 [CI 摘要](../evidence/github-ci-0.6.0-probe-fixture-failure.json)與 [重現](../evidence/updater-probe-ci-owner-before.xml)。已只修測試資料的真實所有權與 finally 回復，加入強制外來 UID 案例；[更新器全套 36 項](../evidence/updater-probe-ci-owner-fixed.xml)通過（5.68 秒），[修正範圍](../evidence/updater-probe-ci-owner.json)記錄實際容器及擁有權回復。產品的 root chown、ISO 與進行中的完整安裝不變；修正後完整 CI 如上，469 passed、4 skipped。

較早的分項紀錄（不是額外加總）：Controller／Engine／Hermes 管理層 **106 passed、1 skipped**（另待 real Core）；真 pinned Hermes 容器 **22 passed**；Web 三尺寸瀏覽器回歸 **11 passed**。對應 [Controller](../evidence/worker-controller.xml)、[Hermes runtime](../evidence/worker-hermes-runtime.xml)。先前本機完整 `--real-ha --containers` 為 **385 passed、3 skipped／378.44 秒**，見 [完整 JUnit](../evidence/pytest-0.6.0.xml)；另補遷移快照、updater 快照與 verifier shadowing 的定向回歸，分別 35、34、35 項通過。三個跳過項為兩個 HAOS VM 案例與 Linux 安裝案例，另行執行；真帳號不在上述數字內。

[30 張三尺寸截圖](../evidence/ui/0.6.0/README.md) 已逐類檢視；調整見 [本輪審查](reviews/2026-10-12-UI-WORKER-DELIVERY.md)。

另已在一次性 **HAOS 18.3 的真正 App** 執行隔離邊界探測，見 [JSON](../evidence/haos-app-boundary.json)與 [JUnit](../evidence/haos-app-boundary.xml)。預設 protected App 建立 mount namespace 被拒；增加 `SYS_ADMIN`，配合啟用且限定 cgroup2 掛載位置的 AppArmor 診斷設定後，可在與 HAOS host 不同的 cgroup namespace 設定子群組 CPU／PID／記憶體限制，並真實觸發記憶體 OOM kill。沒有 Docker socket，沒有真雲端帳號，也沒有 coding CLI 執行。

這是 Home launcher 的平台可行性證據，不能稱為完整 sandbox 或 E2 驗收。CPU／PID 耗盡、AppArmor 禁止路徑、工作區 quota、出站／憑證隔離和取消接手仍待整合驗證；可信 App 程序可見 Supervisor token，模型子程序的去憑證邊界尚未交付。Home coding 仍未啟用，T3-13／T3-15 維持 `not_run`。

能力與限制見 [Worker 說明](WORKERS.md)、[更新／網路說明](UPDATES.md)、[v3 帳本](v3/PROGRESS.md)。歷史紀錄保留如下，不能用於宣稱新版 ISO 已通過。

# 0.5.1 E2E 與恢復驗證（歷史）

2026-10-12，本機完整 `pytest-disktmp uv run pytest -q --real-ha --containers` 回歸 **271 passed、2 skipped，194.16 秒**，見 [JUnit](../evidence/pytest-0.5.1.xml)。兩個獨立大型案例另外執行：真 HAOS **1 passed／84.78 秒**；修正版 0.5.1 精確 ISO 安裝 **1 passed／1493.94 秒**。首輪媒體版本標記失敗保留於本頁末；修正後使用新 digest 完整重驗。

GitHub [CI 38223849292](https://github.com/teddashh/agentic-smart-home/actions/runs/38223849292) 在同一 source commit `c6735fe79c80a669f55e3f119c6f48d5e1e6fed7` 通過 **271 passed／3 skipped／319.254 秒**，見 [CI 記錄](../evidence/github-ci-0.5.1.json) 和 [JUnit](../evidence/pytest-0.5.1-github.xml)。CI 另外跳過 UID1000 專用桌面單元案例；實際跨程序票據及新增 composed E2E 均通過，後者 **60.44 秒**，見 [CI E2E](../evidence/onboarding-e2e-0.5.1-github.json)。CI ZIP 的 405 檔 manifest 全數驗證，與媒體來源的 141 份檔案全部相同，見 [核對](../evidence/ci-media-match-0.5.1.json)。

新增的 [跨服務 E2E](../evidence/onboarding-e2e.json) 使用真正 Chromium、Controller HTTP、TLS Runner、真 Codex、Hermes AIAgent／MCP、隔離 Core 與原生瀏覽器 OAuth；涵蓋 CLI 選擇／登入、Hermes 獨立驗證、Web 通道、HA 引導、Controller 重啟不重派、家庭授權，以及同 case 的 MCP 證據／對話刷新。最後將 Controller／Runner／Hermes 資源限制對齊正式 compose 後，該條 E2E 另跑 **1 passed／45.37 秒**，並記錄實際 image IDs、記憶體／CPU／PID 限制及 OOM 狀態。沒有 browser API interception。模型回覆、Host 安裝／VM effects 是明示 fixture，原生 HA wizard 的初始化透過真 Core API 完成；真雲端同意、實體 laptop／設備、真 bot 仍未執行。

修正涵蓋整組 CLI 停止確認、重複取消／斷線、停止未確認的 Controller／Gateway 呈現、配對回覆遺失、異常 FIFO、pause／HA owner 競爭、登入表單、Hermes readback 期限、HA callback 與 Telegram 初始配對。完整缺陷與未完成功能見 [E2E／架構審查](reviews/2026-10-12-E2E-ARCHITECTURE.md)。Runner idle retention 與可信 quarantine 解除仍是已知缺口；Host／Home coding、preview、固定更新／退版尚未完成，E1／E2 未升級。

## 0.5.1 精確媒體與安裝

媒體與最終 CI 來源均為 `c6735fe79c80a669f55e3f119c6f48d5e1e6fed7`。後續文件／證據提交不重建已驗證媒體；[build](../evidence/build-0.5.1.json) 與 [release](../evidence/release-0.5.1.json) 保存 digest、四個 image IDs 與驗收限制。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.5.1-laptop-amd64.iso` | 5387517952 | `5363f9e9b96dcfe7f7e3516b7533a2f70d18b8e63f5102a1569b01de070a7477` |
| `agentic-home-0.5.1-linux-amd64.tar` | 1308037120 | `89be112be7bbfd5cfb9857783cc5d70eec7abce35e9f2519832b25d49cbb6560` |

一次性 VM 實際完成 Ubuntu 24.04.5 安裝、外層 firstboot、四映像核對、桌面單次登入／重播拒絕、Controller 停止時的非 sudo 救援、離線 sandbox、最新版 Hermes 安裝、HAOS 啟動、冷快照還原與外層重啟。三次都讀到真 Core onboarding JSON list；重開後 Hermes image 不變。見 [安裝結果](../evidence/linux-install-0.5.1.json)、[JUnit](../evidence/linux-install-0.5.1.xml)、[救援](../evidence/installed-rescue-laptop-amd64-0.5.1.json)。

官方來源在本次安裝解析為 Hermes `3569e8461ea721623fa16e6f01fb636157989fe6`，完成下載、建置、相容性檢查、啟用與 runtime 版本讀回，見 [安裝 Hermes](../evidence/installed-hermes-laptop-amd64-0.5.1.json)。同一上游 commit 獨立建置後，原始 runtime／MCP／OAuth helper suite **21 passed／29.805 秒**，見 [候選記錄](../evidence/latest-hermes-3569.json) 及 [JUnit](../evidence/latest-hermes-3569.xml)；兩次 image IDs 各自記錄，模型／OAuth HTTP 為 fixture。獨立驗證首次改測試埠被 Controller 固定 Host allowlist 拒絕，原失敗保留；同 image 改回未修改的測試即通過，未修改產品。這是初次安裝流程，尚非使用中產品的通用候選更新／退版。

安裝測試無真帳號，直接呼叫固定 Host 動作；跨服務 Web 測試另外驗證自動串接，不能合稱真雲端端到端 onboarding。公共 ISO 保留網路、磁碟與 Linux 身份選擇，沒有預選清除磁碟或內嵌測試 key，見 [seed](../evidence/public-seed-0.5.1.json)。TAR 未另測既有主機就地安裝；大型媒體位於本機 `dist/`，不存入 Git。測試 VM、臨時 key 與磁碟 basetemp 已移除。

T3-01 與 T3-05 僅更新 E0 工程媒體／救援證據；其餘 30 案仍為 `not_run`，見 [Plan v3 帳本](v3/PROGRESS.md)。Owner laptop、真雲端 OAuth／bot、HAOS App coding、實體設備與 72 小時觀察未執行。

# 0.5.0 自動 onboarding 驗證（歷史）

2026-10-12，0.5.0 精確 laptop ISO 在一次性 VM 完成安裝與生命週期測試，**1 passed／1310.79秒**。最新 [GitHub CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38210802532) **210 passed／3 skipped／245.250秒**，見 [CI記錄](../evidence/github-ci-0.5.0-final.json)及 [JUnit](../evidence/pytest-0.5.0-github-final.xml)。本機完整 `--real-ha --containers` 回歸 **210 passed／2 skipped／188.68秒**，見 [本機JUnit](../evidence/pytest-0.5.0.xml)。CI跳過獨立大型HAOS VM、完整ISO安裝與UID1000專用本機桌面單元測試；後兩項另有安裝及本機證據，跨程序桌面票據測試已在CI通過。

媒體來源固定為 `1bc2ed0a27f8f0d1e9c03fc5c5f591aed89a4172`；最終CI／安裝harness來源為 `5d99ea80a04ff19a530d438f68e64e83716df71a`。兩者差異只有測試和文件，93份runtime／契約／配置檔案與CI來源產物、媒體hash完全一致，見 [核對](../evidence/ci-media-match-0.5.0-final.json)。本頁後續證據更新不重建已驗證媒體；[release](../evidence/release-0.5.0.json)記錄精確版本與限制。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.5.0-laptop-amd64.iso` | 5387452416 | `5d79886885253a0799d2d21a244ba79648e4c4a570fe2b7e701c2a07057d6430` |
| `agentic-home-0.5.0-linux-amd64.tar` | 1307934720 | `3cca4f664e6dc55549956981071aec434b70f400ccf4127d8aa0f52e6a7eea75` |

TAR是封裝產物，未另外測既有主機就地安裝。公共ISO保留網路、磁碟、Linux身份選擇，沒有預選清除磁碟或內嵌測試key，見 [seed檢查](../evidence/public-seed-0.5.0.json)。大型媒體保留在本機dist，不存入Git。

| 新版範圍 | 實際證據與限制 |
|---|---|
| 精確ISO／HAOS | Ubuntu24.04.5安裝、outer-only firstboot、四image IDs、真桌面單次登入與拒絕重播、停止Controller後非sudo救援、離線sandbox、HAOS啟動／冷快照還原／外層重開自動啟動；三次讀到真Core onboarding JSON list。[安裝](../evidence/linux-install-0.5.0.json)、[JUnit](../evidence/linux-install-0.5.0.xml) |
| 官方最新Hermes | 安裝當下解析到 `41cd7b649d0980c57620035af736f91be10dae79`，真下載／建置／離線相容性gate／按digest啟用，runtime回讀版本正確，重開後維持相同image。獨立候選另有21項runtime／OAuth helper／MCP測試通過，[候選證據](../evidence/latest-hermes-41cd.json)、[JUnit](../evidence/latest-hermes-41cd.xml)。兩次獨立建置image ID不同，各自記錄；沒有真雲端登入 |
| CLI bootstrap | 真封裝Codex binary、tools-disabled與本機模型fixture；typed觀測、持久去重與取消。這是受限設定助手，通用Host／Home research/development仍blocked |
| HA官方授權 | 真隔離Core2026.10.1 code交換→owner核對→專用LLT→撤銷臨時grant；21項授權測試包含登出／替代授權／等待中權限失效。見 [Core記錄](../evidence/ha-onboarding-oauth.json)與完整回歸；未執行Owner家庭瀏覽器授權 |
| 設定恢復／通道 | Controller協定fixtures驗證九步排程、重試、回應遺失、HA404獨立owner證據、配對到期及sender綁定。真實LINE／Telegram外部收發仍not_run |
| 桌面／Web | 真Host ticket writer→封裝Controller跨程序單次登入通過；私有launcher不洩漏程序參數。真Chromium＋API fixtures測引導、刷新、斷線、1440／768／390px，[畫面](../evidence/ui/bootstrap-choose-1440.png) |

安裝測試沒有provider／通道帳號，透過固定Host CLI直接觸發Hermes安裝及HAOS建立；Web自動串接則由Controller與瀏覽器fixture驗證。不能把這兩類證據合併稱為真雲端端到端onboarding。OAuth同意、LINE QR／配對、Telegram bot token及HA管理者帳號仍需使用者操作。Owner laptop、真帳號、E1/E2 coding與固定產品更新／退版仍未完成。

初次ISO測試在Linux／firstboot／映像核對後，因驗證腳本誤把重播拒絕403預期為401而停止。原harness未保存HTTP狀態，[失敗紀錄](../evidence/linux-install-0.5.0-auth-fixture-failure.json)原樣保留。修正harness後重跑相同ISO通過，產品拒絕重播行為沒有修改。先前 `8a7a5a9…` 的 [21項相容性測試](../evidence/latest-hermes.json)保留為前一上游候選，實際安裝使用上述41cd版本。

測試VM、容器、臨時SSH key及磁碟basetemp均已清理。T3-01與T3-05的E0工程範圍已在本版重驗，其他30項產品案例維持not_run，見 [Plan v3帳本](v3/PROGRESS.md)。公開檔案掃描與封裝檢查見 [source audit](../evidence/source-audit-0.5.0-final.json)及 [build](../evidence/build-0.5.0.json)。

## 0.4.0 技術預覽驗證（歷史）

2026-10-12，修正 sandbox／manifest 權限後的 [GitHub CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38202961564) 通過 **145 passed、2 skipped，152.424 秒**；受測與媒體 source commit 均為 `cb11ffd8c0d3eb48a9854fd51add030a1df936e9`，見 [CI 記錄](../evidence/github-ci-0.4.0-final.json)及 [JUnit](../evidence/pytest-0.4.0-github.xml)。CI 包含 `--real-ha --containers`，跳過獨立大型 HAOS KVM 與完整 Linux ISO 安裝；後者已另行通過 **1 passed，1566.81 秒**，見 [ISO JUnit](../evidence/linux-install-0.4.0.xml)。本輪完成 E0 工程媒體及獨立救援範圍；Owner laptop／真帳號與 E1、E2 尚未完成。

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
| 離線 coding containment 基礎 | 19 項 sandbox 回歸檢查 Docker 限制與 fencing；另在安裝後一次性 VM 完成 root broker 掛載獨立 ext4、clone／修改／測試／patch、總容量耗盡、去重、程序樹取消及清理，見 [installed sandbox](../evidence/installed-sandbox-laptop-amd64-0.4.0.json) | 程式為離線 Python fixture，沒有 provider egress、cloud CLI 或 HAOS App coding；兩個 coding workers 仍 blocked |
| 固定 console 支援 | Host／support 20 項回歸通過；精確 ISO 安裝後停止 Controller，一般 Owner 以非 sudo diagnostics／support-bundle 完成救援，每筆版本均為 0.4.0，見 [installed rescue](../evidence/installed-rescue-laptop-amd64-0.4.0.json) | 通過 E0 獨立救援範圍；E2 updater／退版擴充與 Owner laptop 現場仍未驗 |
| 實際 Chromium UI | `tests/test_ui.py`、`test_gateway_ui.py`、`test_hermes_ui.py`：1440／768／390 px；Hermes 真實狀態語意、keyboard login form、key 清空且不進 storage／URL、續接／刷新／新 case、進階 Grok/HAOS 與 Hermes scope 隔離。API 為 fixtures，畫面見 `evidence/ui/hermes-*` | 沒有登入真 OpenRouter／LINE／CLI 帳號；UI fixture 不證明真家庭語意 |
| 保留家庭／CLI／通道回歸 | 真 Core 2026.10.1 的原生驗證／helpers／讀回／觸發／重啟；真受控 Runner、LINE.js 與 Supervisor package 測試。模型、部分 backup/平台端點依既有案例使用 fixtures | 不代表真 model→家庭完整流程、LINE／Discord／Telegram 實際收發或新 HAOS App coding |
| 精確 ISO／HAOS lifecycle | 一次性 VM 的 OS 安裝、外層 firstboot、四映像 ID、Owner HTTP、可選 HAOS provision、冷快照／restore、外層 reboot／HAOS autostart；三次讀到真 Core onboarding JSON list，見 [linux-install](../evidence/linux-install-0.4.0-final.json) | 是基礎安裝與生命週期驗證；没有 Home Worker App coding、真帳號或 T3-27 的完整 re-pair／helpers 驗收 |

Hermes 的七工具為 `host.status`、`worker.capabilities`、`worker.submit/status/cancel/result` 和 `case.evidence.read`。沒有原生 terminal/filesystem/browser/delegate 或 Live approve/apply。真上游執行與模型 fixture 分開標記；沒有把檔案名稱或模型輸出 passed 當作測試平台結果。

Controller 使用 `user_version=2`，0.3.x binary 不能讀取新格式。Hermes 的獨立保存上限透過私有 `/v1/retention` 同步，縮短時停止進行中的 turn 再刪逾期內容；同步失败不先改 Controller 設定。操作與敏感遷移快照的保護見 [OPERATIONS](OPERATIONS.md)。

Ruff、產生契約與文件完整性檢查通過。Git 發佈 allowlist 已做 Gitleaks 8.30.1 掃描，0 findings，見 [source-audit.json](../evidence/source-audit-0.4.0-final.json)；最終檔案數以該記錄為準。掃描範圍排除私有 parent／ignored runtime 資料，不能當成真家庭資料已獲公開授權。

## 0.4.0 工程媒體（一次性 VM 驗證通過）

媒體來自已凍結的 source commit `cb11ffd8c0d3eb48a9854fd51add030a1df936e9`，見 [release](../evidence/release-0.4.0-final.json)。下列精確 laptop ISO 已通過一次性 VM 全流程；測試核對安裝版本與四個實際 image ID。Linux TAR 是封裝產物，沒有另外執行既有主機就地安裝驗收。

| 媒體 | Bytes | SHA-256 |
|---|---|---|
| `agentic-home-0.4.0-laptop-amd64.iso` | 5387124736 | `44d163dba7ff629cc287213cbbb9a4836381d24e45531533efa0051ca9625868` |
| `agentic-home-0.4.0-linux-amd64.tar` | 1307658240 | `12d4426978674706810dcc4be73de40ea26542a15f869e032b864d3879f04229` |

完整 pytest 耗時 1566.81 秒，內部安裝流程計時 1564 秒。`runtime_location=disposable_outer_linux_vm`、`model_backend=none`、`ha_backend=nested_official_haos`、`device_kind=none`；Ubuntu 24.04.5 上以一次性磁碟 serial 與臨時 SSH key 安裝，未碰其他磁碟。結束後 VM、測試容器與磁碟 basetemp 已清除。證據見 [linux-install](../evidence/linux-install-0.4.0-final.json)及 [JUnit](../evidence/linux-install-0.4.0.xml)。

firstboot 僅啟外層，HAOS 尚未建立；[真 Owner HTTP 登入](../evidence/installed-auth-laptop-amd64-0.4.0.json)與受控 Runner 配對成功。四個 CLI 均為 `needs_reauth`、Hermes 為 `needs_auth`，coding workers 為 blocked，這是未登入帳號的真實狀態。停止 Controller 後，[非 sudo 診斷與 support bundle](../evidence/installed-rescue-laptop-amd64-0.4.0.json)仍可用，版本保持 0.4.0；無模型參與。

[離線 sandbox probe](../evidence/installed-sandbox-laptop-amd64-0.4.0.json)共耗時 2.1 秒：獨立 clone 產出 patch、重現程式與成功測試；原始 checkout／Git metadata 不變。容量 62869504 bytes 的 ext4 在累計寫入 60252160 bytes 時回 ENOSPC，單檔只有 1048576 bytes，低於 16777216 bytes 的檔案限制，證明測到總檔案系統容量。取消後程序樹停止、writer fencing 與清理通過。隨後真正 provision HAOS，首次啟動、冷快照 restore、外層 reboot／HAOS autostart 後均讀到 Core onboarding API JSON list。

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

0.3.1 當時的映像與媒體記錄固定參照 [build](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/evidence/build-0.4.0-final.json)、[release](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/evidence/release-0.4.0-final.json)。這些是歷史資料；GitHub CI 記錄以 `headSha` 明確指出受測 commit。

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

所有暫存磁碟、SQLite、SSH key 都使用磁碟上的 pytest wrapper 目錄；測試容器、虛擬磁碟和憑證結束後刪除。JUnit 檔寫入後，`hostname` 記為 `redacted`，家目錄路徑記為 `~`；舊版本已提交的證據保持原樣，以免破壞既有封存雜湊。

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

## 0.5.1 首輪媒體版本紀錄失敗

首輪 ISO `1b8ddb6c…` 在 Ubuntu 安裝及 firstboot 完成後，installed.json 回報0.5.0；**1 failed／659.01 秒**，[原始 JUnit](../evidence/linux-install-0.5.1-version-failure.xml)、[失敗 build](../evidence/build-0.5.1-before-version-fix.json)。服務 image tags 為0.5.1，但版本 manifest 不能宣稱成功。Installer 已改用 Host VERSION，ISO label 由版本導出，kit check 核對 Python／App／Web／compose／packager 版本；30 項 Host／bootstrap／support 回歸通過。修正版新 ISO `5363f9e9…` 隨後完整重跑通過 **1 passed／1493.94 秒**，未沿用失敗媒體的驗收。
