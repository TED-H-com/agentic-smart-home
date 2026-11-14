# 管家 VM 部署檔

這些檔案對應 2026-11-14 在 castle 叢集建好的 **VM 102 / pve2 / 192.168.103.105**。

| 檔案 | 用途 |
|---|---|
| `create-butler-vm.sh` | 在目標節點建立並啟動 VM（不碰 VM 100、101） |
| `butler-user-data.yaml` | cloud-init：hostname butler、僅金鑰、安裝 Tailscale 但不登入 |
| `hosts.pin` | /etc/hosts 釘選（請一併寫入 cloud-init 的 hosts template） |
| `ssh-config.snippet` | box 上的 `butler-lan` 別名 |
| `setup-replication-ha.sh` | 每 15 分鐘複製到 pve1、pve4，以及 HA 規則 `butler-nodes` |

授權用的是公鑰（box `box-mgmt`、Castle4 `castle4-pve-admin`）。私鑰不進 git。

快照慣例：`base`（系統就緒）→ `hermes`（軟體裝好）→ `shim-ok`（Google 帳號池已授權且閘道在跑）。
