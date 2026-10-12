# Linux 產品架構 · 0.4.0 技術預覽

外層 Linux 先啟動 Web／Hermes／固定診斷，HAOS VM 由 Owner 稍後建立。Plan v3 的完整 Host/Home coding 仍待啟用；本頁描述當前部署，完整設計與缺口見 [v3 文件](v3/README.md) 和 [進度](v3/PROGRESS.md)。

## 建置與首次開機

`host/bases.lock.json` 固定官方 Ubuntu ISO／HAOS 映像 URL 和 SHA-256。`host/profiles/` 選 base、桌面、VM 資源與合蓋行為。首條交付採 `laptop-amd64`；既有其他 profile 不因 recipe 存在就當作已通過整機驗收。

`scripts/build_host.py` 打包四個 runtime images（Controller、Runner、LINE.js、Hermes）、產品檔案與 HAOS 官方映像，再按需加入 NoCloud seed／GRUB 入口。xorriso boot replay 保留原映像 boot records。網路、磁碟與 Linux 帳號仍由官方安裝畫面選擇，公開 ISO 不內建預選磁碟、固定密碼或 SSH key。

firstboot 安裝套件、載入已驗 hash 的 images、啟動外層服務、產生每台主機自己的登入／服務秘密並配對受控 Runner。**它不執行 HAOS provision**；沒有家庭 VM 或雲端帳號也可開 Web，Hermes 初始為 `needs_auth`。需要家庭功能時，Owner 在 Web 按「建立 HAOS」，或 local console 執行 `sudo agentic-home provision`。

`BUNDLE.sha256.json` 驗證檔案，`images.lock.json` 核對 Docker image ID，外部 `SHA256SUMS` 記錄媒體 digest。Hash 本身不是 publisher 身份。Ubuntu apt 取得安裝當時套件，因此整個 rootfs 並非位元可重現或完全離線；媒體建置、一次性 VM 安裝與 Owner laptop 是不同證據，見 [VERIFICATION](VERIFICATION.md)。

## 外層程序與身份

| 元件 | 位置／權限 | 當前工作 |
|---|---|---|
| Controller/Gateway | Docker UID 1000，loopback Web 8099、私有 MCP 8097 | UI、通道 receiver、Owner／scope、case/run、HA policy／approval、固定 host API |
| Hermes | 獨立 Docker UID 10003，loopback 8096；read-only root、drop capabilities、CPU/memory/PID limits | 真 pinned AIAgent、OpenRouter 私有認證、session history、固定 Controller MCP 工具 |
| 受控 Runner | Docker broker＋各 provider UID，loopback TLS 8443 | 舊 `home_operator` 結構化 CLI 問答／登入，不含通用 coding 權限 |
| LINE.js | 獨立本機 sidecar，loopback 3789 | 可選 LINE 登入及 typed 通道連線 |
| Host broker | systemd root，固定 Unix socket | 指定 ownership VM 的 provision／start／shutdown／snapshot／restore |
| Local support CLI | Owner console 執行，無需 Web／模型 | 固定狀態與去密 bundle，無模型任意程式 |

Hermes 使用 [commit 835c3dd…](https://github.com/NousResearch/hermes-agent/tree/835c3dd038d903182a66ecc45e34bf5e0de65f00)，版本／來源／Python 3.14／依賴各自 lock，見 [Hermes README](../hermes/README.md)。它不啟動第二個 bot Gateway，也不開內建 terminal、filesystem、browser、delegate 或 Live approve/apply。新 cloud coding 任務仍 blocked，不能把 coordinator 容器當成執行任意候選程式的 sandbox。

MCP 使用專用短期 case/run capability。固定工具僅 `host.status`、`worker.capabilities`、`worker.submit/status/cancel/result`、`case.evidence.read`；submit 現在記錄 blocked handle 而不啟動 worker。Controller 不執行 CLI／shell。產品容器不掛 Docker socket；Host coding 的 trusted launcher prototype 另見 [隔離基礎](../host/worker/README.md)，尚未接入可用派工。

主機 access-key 換取 12 小時 HttpOnly／SameSite cookie；mutation 驗 Origin／CSRF，Host allowlist 防 DNS rebinding，遠端管理用 SSH tunnel。Hermes service token 是每台安裝生成的独立秘密；8096／8097 拒絕 browser Origin，瀏覽器不能直接取得服務 bearer。

家庭授權仍使用 HA active owner 長效權杖，保存在 Controller 的 mode-0600 檔案。Core WebSocket `supervisor/api` 以 owner 代理 Supervisor backup，管理 token 不進 Hermes／CLI。主機登入不會冒充家庭 owner，每次家庭 mutation 重新查核。

## 可選 HAOS VM 與恢復

Owner 呼叫 provision 後，broker 建立並啟動固定 VM `agentic-home-haos`、network `agentic-home`、bridge `virbr-ah`、NAT `192.168.177.0/24`、guest DHCP `.2`。路由重疊或同名外部資源會拒絕；UUID、XML metadata、ownership journal 和磁碟路徑必須匹配。

guest 使用 non-secure OVMF、qcow2／virtio-scsi／virtio-net，預設 2 vCPU／4 GiB RAM／64 GiB sparse disk。成功 provision 後才有 libvirt autostart；未建立 HAOS 的外層重啟不會突然建立 guest。ACPI shutdown 等待 180 秒，逾時不強制斷電。

loopback proxy 8123 → guest:80 保留 WebSocket，不偽造 HA 身份。NAT 不提供 LAN multicast discovery；profile manager 不自動建立實體 Ethernet bridge 或認領 USB。Home Coding Worker 必須在真 HAOS App 內另做 namespace／credential／LAN／管理隔離驗證；`host_network:false` 或未給 HA API token 不能直接證明 App 無法連其他內部服務。

冷快照正常關機後複製磁碟＋UEFI，計算 SHA-256 後寫 manifest。restore 先驗 ownership／digest／關機狀態，記錄 `restore_pending`；中断可對同一 snapshot 核對／續作，不啟動半還原 VM。還原較舊 guest 後需 reconcile 較新的 Controller ledger，資料還原不能撤回物理動作。

## 資料與救援

| 路徑 | 內容 |
|---|---|
| `/opt/agentic-home` | 產品、鎖定來源及 assets |
| `/etc/agentic-home/profile.json` | 固定主機 profile |
| `/var/lib/agentic-home/controller` | Controller DB、HA token、主機 access-key |
| `/var/lib/agentic-home/runner` | 受控 CLI profiles、Runner DB |
| `/var/lib/agentic-home/hermes` | Hermes 私有認證、adapter DB、限定 session/run history |
| `/var/lib/agentic-home/hermes-link` | 每台主機的私有 service token，read-only 掛載 |
| `/var/lib/agentic-home/linejs`／`line-config` | LINE 私有 session／typed 設定 |
| `/var/lib/agentic-home/host` | VM ownership、jobs、cold snapshots |
| `/var/lib/agentic-home/pairing` | 受控 Runner 初始配對資料 |
| `/var/lib/libvirt/images/agentic-home` | provision 後的 HAOS disk／NVRAM |
| `/run/agentic-home/host.sock` | 固定主機操作 socket |

Controller case/run/grant/evidence 使用既有 records table 的 `data_contract=1`，整體持久格式是 **schema 2**。新增 `coordinator` 等欄位不相容於 0.3.x，因此從 schema 1 開啟時先建立 mode-0600 的 SQLite 一致私有快照，再交易更新 marker；既有資料保留，新 DB 直接為 2，舊 binary 拒絕 2。相同 SQL tables 不代表舊應用能理解新資料。Hermes adapter 有獨立 DB／留存規則，不與 Runner 或 Controller 共用 home、profiles 或 writable DB。固定 updater／原地降版仍待實作，不用遷移前 DB 覆蓋新操作；操作與快照保護見 [OPERATIONS](OPERATIONS.md)。

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
