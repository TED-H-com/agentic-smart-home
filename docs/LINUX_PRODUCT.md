# Linux 產品架構

## 建置模型

`host/bases.lock.json` 固定官方 ISO／HAOS URL 和 SHA-256。`host/profiles/` 選擇 base、桌面、VM 資源與合蓋行為。共用產品只維護一份；scripts/build_host.py 加入產品 TAR、NoCloud seed、GRUB 入口，xorriso boot replay 保留原映像 boot records。

網路、磁碟及身份使用原生安裝畫面。公開 ISO 沒有預選磁碟、固定密碼或 SSH key。late-command 驗證產品 TAR 後放入目標 Linux，firstboot systemd unit 自動完成部署。

BUNDLE.sha256.json 驗證產品檔案，images.lock.json 核對 Docker image ID。Ubuntu apt 套件取得安裝當時的安全更新；因此整個 Linux rootfs 並非位元完全可重現，也不是全離線安裝。官方 ISO/kernel 與產品 runtime 則由 release artifact 固定。

## 外層程序

| 元件 | 位置／權限 | 工作 |
|---|---|---|
| Controller | Linux Docker UID 1000，loopback 8099 | UI、HA owner bearer、家庭政策、調用固定主機 API |
| Runner | Linux Docker broker／各 provider UID，loopback TLS 8443 | 官方登入、受限 CLI 結構化輸出；沒有主機管理 socket |
| Host broker | systemd root，Unix socket | 固定 ownership HAOS VM／網路／磁碟／快照 |

容器不掛 Docker socket。Controller 不執行 shell／CLI。root broker 不執行模型，只接受 provision、start、shutdown、snapshot、restore；不接受任意 command、path、URL、VM 名稱或實體磁碟。

主機 access-key 換取 12 小時 HttpOnly／SameSite cookie；mutation 另驗證 Origin／CSRF。listener 綁 loopback、Host allowlist 防 DNS rebinding；遠端用 SSH tunnel。

家庭授權使用 HA active owner 長效權杖，保存在獨立 mode-0600 檔案。Core WebSocket supervisor/api 以 owner 身份代理 Supervisor 備份，無 Supervisor token 離開 HAOS。主機登入不會冒充 HA owner，每次家庭 mutation 重新查核。

## VM 與恢復

固定 VM agentic-home-haos、network agentic-home、bridge virbr-ah、NAT 192.168.177.0/24、DHCP .2。路由重疊或已有同名外部資源時拒絕認領。UUID、XML metadata、local ownership journal 和 disk path 都要相符。

UEFI 使用 non-secure OVMF、qcow2／virtio-scsi／virtio-net，預設 2 vCPU／4 GiB guest RAM／64 GiB sparse disk。libvirt autostart 在 Linux 重開後啟動。ACPI shutdown 等待 180 秒，逾時不強制斷電。

loopback TCP proxy 8123 → guest:80 保留 WebSocket，不偽造 HA 身份。NAT 不提供 LAN multicast discovery；本版 profile manager 不會自動建立實體 Ethernet bridge。

冷快照正常關機後複製磁碟＋UEFI，算 SHA-256，最後才產生 manifest。restore 先驗證 ownership／雜湊／關機狀態並取消 autostart，保存 restore_pending；全部替換完成才清除記錄並恢復 autostart。中斷時可對相同 snapshot 重跑，不能啟動半還原 VM。

HAOS snapshot 不包含外層 Controller／Runner。還原較舊的 guest 後，Controller ownership ledger 可能較新，需 reconcile。物理動作不能靠資料還原撤回。

## 安裝目錄

- /opt/agentic-home：產品及 assets
- /etc/agentic-home/profile.json：主機設定
- /var/lib/agentic-home/controller：DB、HA token、access-key
- /var/lib/agentic-home/runner：私有 provider profiles、DB
- /var/lib/agentic-home/host：ownership、jobs、冷快照
- /var/lib/agentic-home/pairing：初始配對
- /var/lib/libvirt/images/agentic-home：HAOS 磁碟及 NVRAM
- /run/agentic-home/host.sock：Controller 專用主機 API

## 新增 Linux／硬體

1. 新增官方 base URL、SHA-256、os_id、os_version、arch。
2. profile 定義 desktop、memory_mib、vcpus、disk_gib、appliance_lid、minimum_host_memory_mib。
3. 核對 apt 名稱、OVMF 路徑、libvirt service、Docker Compose。非 Ubuntu 需新 installer adapter；不能忽略版本檢查硬裝。
4. 用 build_host.py --profile ID --iso 建置並執行隔離 VM 測試。
5. 新架構另外提供 boot chain、HAOS image 與 CLI binary port，不能只改版本字串。

## 來源

- [HA Linux／KVM](https://www.home-assistant.io/installation/linux/)
- [Ubuntu 24.04 映像](https://releases.ubuntu.com/24.04/)
- [Ubuntu 26.04 映像](https://releases.ubuntu.com/26.04/)
- [Autoinstall reference](https://canonical-subiquity.readthedocs-hosted.com/en/latest/reference/autoinstall-reference.html)
- [NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html)
- [Core Supervisor WebSocket proxy](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hassio/websocket_api.py)
