# 家用管家（Home butler）

這個 repo 的主線已經改了。Home Assistant 用官方原版 HAOS，旁邊另開一台小 VM 跑 Hermes，透過 [Home Assistant 官方 MCP](https://www.home-assistant.io/integrations/mcp_server/) 讀狀態、控制設備。兩邊互不干擾：HA 升級不會弄壞管家，管家出問題也拖不垮 HA。

舊的「自製 Linux 主機 OS + 內嵌 HAOS VM」那一條線沒有刪，收在：

- 分支 [`archive/agentic-host-os`](https://github.com/TED-H-com/agentic-smart-home/tree/archive/agentic-host-os)
- 標籤 [`archive/os-appliance-v0.11.1`](https://github.com/TED-H-com/agentic-smart-home/releases/tag/archive/os-appliance-v0.11.1)

詳見 [docs/ARCHIVE.md](docs/ARCHIVE.md)。

## 現況（castle 叢集，2026-11-14）

| 項目 | 內容 |
|---|---|
| 管家 VM | **butler**，VMID **102**，在 **pve2** |
| 規格 | 4 核、8 GB（無 balloon）、64 GB 碟、Ubuntu 26.04 |
| 區網 IP | `192.168.103.105/23`，閘道 `192.168.103.1` |
| 正式 HAOS | VM **100**，在 **pve3**，`http://192.168.103.103`。主線腳本不會動它 |
| 複製 / HA | 每 15 分鐘複製到 pve1、pve4；優先 pve2，備援 pve1（pve4 記憶體可能不夠同時跑 mgmt 8G + butler 8G） |
| Hermes | v0.22.7 + 官方 `agy` CLI + `agy-openai-shim`（127.0.0.1:18795） |
| 還沒做 | 管家尚未加入 Tailscale；尚未接正式 HA（沒有把權杖或 OAuth 寫進 git） |

## 目錄

- [`deploy/butler-vm/`](deploy/butler-vm/)：建立管家 VM 的 cloud-init、qm 腳本、複製與 HA、hosts、SSH 別名
- [`hermes/`](hermes/)：技能、提示詞、SOUL、最小設定樣板（不含密鑰）
- [`ha-mcp/`](ha-mcp/)：官方 MCP 的連線與權限說明（權杖與 OAuth 不進 git）
- [`deploy/haos-test/`](deploy/haos-test/)：可拋棄的乾淨 HAOS 測試機（不要放 pve3，不要碰 VM 100）
- [`docs/`](docs/)：部署 → 測試機 → 再接到家裡正式 HA

## 建議順序

1. 部署或還原管家 VM（已開好的話只需維持快照與複製）
2. 先在測試用 HAOS 上開官方 MCP，讓 Hermes 連測試機
3. 確認沒問題，再由主人用安全方式把正式 HA 的授權交給管家（不要貼在聊天或 commit）

細節見 [docs/DEPLOY.md](docs/DEPLOY.md)、[docs/TEST-HA.md](docs/TEST-HA.md)、[docs/CONNECT-REAL-HA.md](docs/CONNECT-REAL-HA.md)。
