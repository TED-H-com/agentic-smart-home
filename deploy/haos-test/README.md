# 可拋棄的 HAOS 測試機

用途：在接到家裡正式 Home Assistant（VM 100）之前，用一台乾淨的官方 HAOS 練習官方 MCP 與管家連線。

## 硬性限制

- 不要建在 **pve3**
- 不要使用 VMID **100**，不要讀寫、快照、遷移或刪除 VM 100
- 建議：**pve1**、VMID **110**、名稱 `haos-test`
- 測完可以刪除這台測試機；不要把它排進與正式 HA 相同的 HA 規則

## 做法概要

1. 從 [Home Assistant OS 發布頁](https://github.com/home-assistant/operating-system/releases) 下載官方 qcow2／ova，在 pve1 匯入
2. 給 2 核、2–4 GB 記憶體、32 GB 碟即可，網卡接 `vmbr0`
3. 區網 IP 不要用 `192.168.103.103`（那是正式 HA）。選一個確認空閒的位址，或先 DHCP 再釘選
4. 開機後完成 HA 新手設定，再依 [`ha-mcp/README.md`](../../ha-mcp/README.md) 安裝官方 MCP
5. 只有測試機驗證通過，才考慮把管家指到正式 HA

`create-haos-test-vm.sh` 只是檢查與記錄用的骨架，預設不會啟動匯入；請先讀完再改參數執行。
