# 部署管家 VM

目標叢集是家裡的 Proxmox `castle`（pve1–4）。管家 VM 不要跟正式 HAOS 擠在 pve3。

已建好的實例見根目錄 README：VM 102、pve2、`192.168.103.105`。

## 建立（若要重做）

在選定節點（建議 pve2）放好 Ubuntu 26.04 cloud image，校驗 SHA-256，再跑 [`deploy/butler-vm/create-butler-vm.sh`](../deploy/butler-vm/create-butler-vm.sh)。腳本會寫入 [`butler-user-data.yaml`](../deploy/butler-vm/butler-user-data.yaml)（僅公鑰、禁用密碼登入）。

建立並完成 cloud-init 後：

1. 釘選 [`hosts`](../deploy/butler-vm/hosts.pin) 到客體與（如需）mgmt
2. 加上 [`ssh-config.snippet`](../deploy/butler-vm/ssh-config.snippet)（box 走 `butler-lan`，經 Castle4 跳板）
3. 拍快照 `base`
4. 跑 [`setup-replication-ha.sh`](../deploy/butler-vm/setup-replication-ha.sh)
5. 安裝 Hermes 與 shim，見 [`hermes/README.md`](../hermes/README.md)，再拍 `hermes`、授權後拍 `shim-ok`
6. Tailscale：官方腳本安裝後，由主人本人打開 `tailscale up` 的登入網址。尚未加入前用 `butler-lan`

不要把私鑰、Tailscale auth key、Google 權杖或 HA 權杖寫進這個 repo。

## 備援注意

pve4 已跑 mgmt（8 GB）。管家也是 8 GB 時，pve4 多半無法同時跑兩者。HA 規則裡雖有 pve4，真正用得上的備援是 pve1。
