# Linux 產品架構

外層 Linux 先啟動 Web／Hermes／固定診斷，HAOS VM 由持久 onboarding 在 CLI／Hermes／通道就緒後自動建立。Host Codex coding 與真正運行於 HAOS App 的 Home coding 已交付，家庭現場可從網頁一鍵啟用及獨立登入。真帳號及完整 E1／E2 仍待現場驗收。本頁描述當前部署，完整設計與缺口見 [v3 文件](v3/README.md) 和 [進度](v3/PROGRESS.md)。

0.10.0 工程預覽的來源、精確媒體與公開 App 結果見 [驗證紀錄](VERIFICATION.md)；既有 0.9.0 證據保留原版本，真帳號及實體筆電另驗。

## 建置與首次開機

`host/bases.lock.json` 固定官方 Ubuntu ISO／HAOS 映像 URL 和 SHA-256。`host/profiles/` 選 base、桌面、VM 資源與合蓋行為。首條交付採 `laptop-amd64`；既有其他 profile 不因 recipe 存在就當作已通過整機驗收。

`scripts/build_host.py` 打包五個 images（Controller、Runner、LINE.js、Hermes，以及按需啟動的 preview renderer）、產品檔案與 HAOS 官方映像，另含固定官方 Tailscale deb 和本 public repo 精確 commit 的 coding source bundle，再按需加入 NoCloud seed／GRUB 入口。xorriso boot replay 保留原映像 boot records。網路、磁碟與 Linux 帳號仍由官方安裝畫面選擇，公開 ISO 不內建預選磁碟、固定密碼或 SSH key。

0.10.0 的 USB `early-commands` 先執行獨立標準函式庫 helper，檢查 x86-64、profile 最低實際 RAM 和 KVM API／暫時 VM fd。live kernel 尚未載入 KVM 時，最多以固定 Intel／AMD 模組執行一次有時限的 modprobe，再讀回能力；失敗在磁碟設定前停止並提供修正指引。它不選磁碟、不建立 guest 磁碟、不修改網路。安裝後仍檢查 Linux 版本、KVM、RAM 與 `/var/lib` 至少 40 GiB 可用空間。

firstboot 安裝套件、載入已驗 hash 的 images、啟動外層服務、產生每台主機自己的登入／服務秘密並配對受控 Runner。**它不執行 HAOS provision**；沒有家庭 VM 或雲端帳號也可開 Web，Hermes 初始為 `needs_auth`。Web 設定流程在 CLI／Hermes／通道就緒後自動 provision；固定手動入口仍保留。未完成授權前可先使用外層。

`BUNDLE.sha256.json` 驗證檔案，`images.lock.json` 核對 Docker image ID，外部 `SHA256SUMS` 記錄媒體 digest。Hash 本身不是 publisher 身份。Ubuntu apt 取得安裝當時套件，因此整個 rootfs 並非位元可重現或完全離線；媒體建置、一次性 VM 安裝與 Owner laptop 是不同證據，見 [VERIFICATION](VERIFICATION.md)。

## 外層程序與身份

| 元件 | 位置／權限 | 當前工作 |
|---|---|---|
| Controller/Gateway | Docker UID 1000，loopback Web 8099、私有 MCP 8097 | UI、通道 receiver、Owner／scope、case/run、HA policy／approval、固定 host API |
| Hermes | 獨立 Docker UID 10003，loopback 8096；read-only root、drop capabilities、CPU/memory/PID limits | 真 AIAgent、獨立 Codex OAuth／OpenRouter 私有認證、session history、固定 Controller MCP 工具 |
| 受控 Runner | Docker broker＋各 provider UID，loopback TLS 8443 | 保留 `home_operator` 問答／登入；獨立 coding 登入與固定 Host broker 派工 adapter |
| LINE.js | 獨立本機 sidecar，loopback 3789 | 可選 LINE 登入及 typed 通道連線 |
| Host broker | systemd root，固定 Unix socket | Hermes 首次安裝、受控 VM lifecycle、隔離 Host coding、PNG preview、同 schema Controller 更新／退版、網路設定 |
| Preview renderer | 按需 Docker UID 10004，network none、read-only root、有界 ext4／cgroups | 不可信 HTML 轉三尺寸 PNG；獨立 `127.0.0.2:8100` 短效連結，不在管理 origin 執行候選 |
| Local support CLI | Owner console 執行，無需 Web／模型 | 固定狀態與去密 bundle，無模型任意程式 |

Hermes 安裝包回復基線使用 [commit 835c3dd…](https://github.com/NousResearch/hermes-agent/tree/835c3dd038d903182a66ecc45e34bf5e0de65f00)，版本／來源／Python 3.14／依賴各自 lock，見 [Hermes README](../hermes/README.md)。首次引導使用隨附 Hermes 映像，通過離線相容性 probe 後按 digest 啟動；結果寫入 host/runtime.env，重啟不自行追更新。它不啟動第二個 bot Gateway，也不開內建 terminal、filesystem、browser、delegate 或 Live approve/apply。Host coding 派至獨立 sandbox，不能把 coordinator 容器當成執行候選程式的環境；真雲端帳號仍須另驗。

MCP 使用專用短期 case/run capability。固定工具為 `host.status`、`worker.capabilities`、`worker.submit/status/cancel/result`、`case.evidence.read`、`home.observe`、`home.propose`、`home.proposal_status`、`home.discover`；設備狀態與操作仍受既有 policy／精確 Owner 批准控制，沒有 approve／apply 工具。`home.discover` 另由 Owner 對這則訊息明確分享唯讀探索，最多提供八個 HA 候選及來源狀態；不增加控制權限、不掃描 LAN、不建立整合。worker submit 依實際能力與獨立登入派至 Host coding，或已明確啟用並配對的 Home coding App；未啟用的能力維持 unavailable。Controller 不執行 CLI／shell，产品服務容器不掛 Docker socket；root Host broker 持有 launcher，見 [Workers](WORKERS.md)。Unix peer 身份將 UID 0 Runner 限於 coding 接點，UID 1000 Owner／Controller 使用管理接點。

桌面主要 Owner 可透過 120 秒單次票據換取 session；票據經家目錄0700私有目錄的0600暫時 HTML 導向傳遞，xdg-open 程序參數不含 bearer，120 秒後清除該暫存檔。Web 立即移除 fragment 再兌換，server 只保存 hash 並先消耗票據。手動主機 access-key 亦可換取 12 小時 HttpOnly／SameSite cookie；mutation 驗 Origin／CSRF，Host allowlist 防 DNS rebinding，遠端管理用 SSH tunnel。Hermes service token 是每台安裝生成的独立秘密；8096／8097 拒絕 browser Origin，瀏覽器不能直接取得服務 bearer。

家庭授權透過官方 HA 瀏覽器 OAuth 回呼，自動核對 active owner、建立專用長效授權並撤銷臨時 grant，保存在 Controller 的 mode-0600 檔案；手動 token 是進階備援。Core WebSocket `supervisor/api` 以 owner 代理 Supervisor backup，管理 token 不進 Hermes／CLI。主機登入不會冒充家庭 owner，每次家庭 mutation 重新查核。

## 可選 HAOS VM 與恢復

首次設定排程或 Owner 明確呼叫 provision 後，broker 建立並啟動固定 VM `agentic-home-haos`、network `agentic-home`、bridge `virbr-ah`、NAT `192.168.177.0/24`、guest DHCP `.2`。路由重疊或同名外部資源會拒絕；UUID、XML metadata、ownership journal 和磁碟路徑必須匹配。

guest 使用 non-secure OVMF、qcow2／virtio-scsi／virtio-net，預設 2 vCPU／4 GiB RAM／64 GiB sparse disk。成功 provision 後才有 libvirt autostart；未建立 HAOS 的外層重啟不會突然建立 guest。ACPI shutdown 等待 180 秒，逾時不強制斷電。

loopback proxy 8123 → guest:80 保留 WebSocket，不偽造 HA 身份。基本 NAT 不提供 LAN multicast discovery；外層 broker 的鄰居表、mDNS／SSDP 與路由器清單只是獨立觀測來源，見 [設備探索](DEVICE_DISCOVERY.md)。0.10.0 另提供明確選擇的實體有線接入：保留 NAT 管理 NIC，增加一張 direct／macvtap 家庭 NIC；有線模式取消管理 DHCP 的預設 gateway，讓家庭 DHCP 提供 guest 預設路由，不重寫 host IP／路由。只在未建立或已正常關機的 HAOS 上配置，並保留中斷核對紀錄。Controller 在 Owner 授權後讀回 Supervisor／Core 的位址、adapter 與內部網址；原生 HA 的手動選擇由使用者處理。完整操作、LAN 可見服務與待驗證限制見 [家庭區網接入](HA_LAN.md)。產品不自動認領 USB dongle。

既有 0.9.0 真 HAOS 工程驗證已涵蓋 Home launcher、自訂 AppArmor、cgroup／磁碟配額、credential／LAN／管理隔離與停止核對；模型使用固定測試 transport，不能當作真帳號或實體設備驗收。0.10.0 的精確來源另有工程驗證紀錄，見 [驗證紀錄](VERIFICATION.md)。`host_network:false` 或未給 HA API token 不能直接證明 App 無法連其他內部服務，詳見 [Home 邊界證據](WORKERS.md#home-app-的實測範圍)。

冷快照正常關機後複製磁碟＋UEFI，計算 SHA-256 後寫 manifest。restore 先驗 ownership／digest／關機狀態，記錄 `restore_pending`；中断可對同一 snapshot 核對／續作，不啟動半還原 VM。還原較舊 guest 後需 reconcile 較新的 Controller ledger，資料還原不能撤回物理動作。

## 資料與救援

| 路徑 | 內容 |
|---|---|
| `/opt/agentic-home` | 產品、鎖定來源及 assets |
| `/etc/agentic-home/profile.json` | 固定主機 profile |
| `/var/lib/agentic-home/controller` | Controller DB、HA token、主機 access-key |
| `/var/lib/agentic-home/runner` | 受控 CLI profiles、獨立 coding-auth profiles、Runner DB |
| `/var/lib/agentic-home/coding` | 已驗證的只讀 source bare repo、私有工作磁碟、派工與成果 |
| `/var/lib/agentic-home/hermes` | Hermes 私有認證、adapter DB、限定 session/run history |
| `/var/lib/agentic-home/hermes-link` | 每台主機的私有 service token，read-only 掛載 |
| `/var/lib/agentic-home/linejs`／`line-config` | LINE 私有 session／typed 設定 |
| `/var/lib/agentic-home/host` | VM ownership、jobs、cold snapshots、更新與預覽紀錄、runtime image overrides |
| `/etc/agentic-home/update-publishers.json` | Owner 本機審查後登錄的公開簽章信任，不含私鑰 |
| `/var/lib/agentic-home/pairing` | 受控 Runner 初始配對資料 |
| `/var/lib/libvirt/images/agentic-home` | provision 後的 HAOS disk／NVRAM |
| `/run/agentic-home/host.sock` | 固定主機操作 socket |

Controller case/run/grant/evidence 沿用既有 records table，整體持久格式是 **schema 3**，新增 coding job／lease 的停止與恢復語意。從 schema 1／2 開啟時先建立 mode-0600 的 SQLite 一致私有快照，再交易更新 marker；既有資料保留，新 DB 直接為 3，舊 binary 拒絕 3。相同 SQL tables 不代表舊應用能理解新資料。Hermes adapter 有獨立 DB／留存規則，不與 Runner 或 Controller 共用 home、profiles 或 writable DB。固定 updater 只更新相容 schema 3 的 Controller image：可信 publisher 簽章、精確批准、停止／快照／獨立 probe／健康檢查，退版保留目前 Live DB，不覆蓋新操作。不能將它當作跨 schema 降版或全產品更新器；見 [UPDATES](UPDATES.md) 和 [OPERATIONS](OPERATIONS.md)。

`agentic-home diagnostics` 和 `support-bundle --output NEW.json` 直接在 outer host 取固定 metadata，Web、broker、AI 不可用也能執行。只輸出 allowlisted 欄位與 log priority 計數、標明實際觀測位置；不匯出原始 log／憑證／家庭對話。具體操作見 [OPERATIONS](OPERATIONS.md)。

HAOS disk、Core backup、Controller、Hermes、Runner profiles 是不同備份層；一般 support bundle 不含上述私有資料，完整離機備份需加密及相應恢復步驟。現有 installer 保留拒絕覆蓋 Live `/opt/agentic-home` 的保護，不能以再次 `--apply` 當作 v3 updater。

## Profile 擴充規則

本輪優先完成既有 laptop 路徑，不增加 distro。日後擴充需新增官方 base URL／hash／OS／arch，驗證 apt、OVMF、libvirt、Compose 和相應 HAOS/provider binary；不能只改 version 字串。新 profile 必須建置並在隔離 VM 驗證，實體硬體結果另記。

## 官方來源

- [HA Linux／KVM](https://www.home-assistant.io/installation/linux/)
- [Ubuntu 24.04](https://releases.ubuntu.com/24.04/)
- [Autoinstall reference](https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html)
- [NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
- [Core Supervisor proxy](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hassio/websocket_api.py)
- [Hermes／App／資源基礎的 v3 來源索引](v3/SOURCES.md)
