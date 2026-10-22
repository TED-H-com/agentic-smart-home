# 家庭網路自動接入

「連上家庭網路」是一個產品操作：先保存還原點，正常關閉家庭系統，加入有線網卡，再啟動並讀回家庭位址。期間家庭自動化會暫停。失敗時產品嘗試自動恢復基本 NAT 網路；如果外部修改或關機失敗使恢復無法安全完成，保留還原點與管理入口，明確顯示尚待復原，不強制關機或覆寫不明設定。

## 使用方式

在主機維護展開「家庭區網接入」，按 **連上家庭網路**。只有一張可用有線網卡時不用選擇；多張時選擇接到家庭路由器的那張。產品自己完成關機及開機，不需要終端機、SSH、sudo 或 HA 網路設定頁。

第一次建立家庭時，若只有一張實體有線網卡，且主機已從 DHCP 取得位址，預設嘗試接入家庭網路。既有安裝、已保存的基本網路選擇及未完成的操作不會被這個預設值覆寫。首次建立帳號之前只核對 guest 的實際位址；帳號及 Owner 授權完成後，再自動核對 HA 的探索介面及本機網址。兩種證據分開呈現，不能把 IP 位址當成設備探索已驗證。

驗證成功顯示 **HA 已連上家庭網路：&lt;ip&gt;**。這表示 HA 的 LAN 位址、探索介面與本機網址已讀回；不表示每個設備或 Matter／Thread 已通過實際控制測試。

## 工程及救援介面

```console
agentic-home configure_lan
agentic-home configure_lan --interface-id <目前有效的介面識別碼>
agentic-home configure_lan --disable-lan
agentic-home ha-lan
agentic-home status
agentic-home configure_lan --help
agentic-home network-select --help
```

省略 `interface_id` 表示自動選擇唯一候選；明確的 JSON `null` 或 CLI `--disable-lan` 表示回復基本 NAT。多張候選不能猜測。CLI 選項依子命令分開；`--lan {dhcp,reservation}` 只屬於 `network-select`，設定的是 Linux 主機位址政策。

Controller 經既有受保護 Host action API 提交一個持久工作；回應遺失時用原去重鍵取回同一工作，不重送不同操作。Host 仍核對產品 VM、磁碟、網路所有權、獨佔使用與每一步 XML 讀回。配置中的 journal 保留復原圍欄，不能藉刪紀錄繞過。每次 Core 網路寫入（含回復）在 WebSocket 連線及落盤等待之後、送出前再次核對 Owner、登入世代、家庭連線身份與 Host 操作；任一變動即停止該次寫入，保留固定原因代碼。

## 兩張網卡

| 路徑 | 用途 |
|---|---|
| 管理 NIC | 保留 `192.168.177.2` 與既有 MAC，Controller、固定 8123 代理及救援繼續使用。 |
| 家庭 NIC | 第二張 virtio NIC 透過 macvtap bridge 接到實體 Ethernet，由路由器 DHCP 配址。 |
| 預設路由 | 有線模式取消管理 DHCP 的預設 gateway；回到 NAT 時恢復。 |
| Linux 主機 | 不重寫主機 IP、路由、NetworkManager／Netplan，也不把外層管理中心公開到 LAN。 |

macvtap 不允許所在主機經同一實體介面直接連 guest 的 LAN IP；這是正常限制，不能用 ping 或 `ip neigh` 的缺席判定失敗。產品透過管理通路向 HA 的 Supervisor／Core 核對 enrolled MAC、位址、探索 adapter 與 internal URL；首次建立前可經產品 VM 的固定 guest-agent 命令讀回位址。診斷會說明此限制。[libvirt direct interface](https://libvirt.org/formatdomain.html#direct-attachment-to-physical-interface)、[macvtap 主機限制](https://wiki.libvirt.org/TroubleshootMacvtapHostFail.html)。

回復基準（先前與產品目標介面、網址）綁定 HA Owner 與家庭連線身份。一般登出只撤銷進行中的操作，不刪除基準；重新登入後，新接入操作可接續欄位所有權，或在 Host 回到 NAT 時以當下授權回復仍完全等於產品目標的欄位。使用者後續修改永遠保留；更換 Owner 或家庭連線不會把舊基準套到另一個家庭。既有含登入世代的紀錄沿用基準，世代不作為持久基準身份；每次操作仍擷取並核對當下世代。

Core 本機網址沿用已驗證的 Host 管理端點之通訊協定與監聽埠；預設 guest 的 80 埠寫成 `http://<家庭位址>`，不使用 Host loopback 的 8123 代理。僅 Host 模式且管理端點位於產品管理網段才可寫入，Supervisor proxy 不適用。IPv6 使用方括號，非預設埠保留。既有有效家庭網址不覆寫；產品寫入後須由 Core `/config` 讀回完全相同的值，才可回報完成。

新的家庭 MAC 由產品安裝身份穩定衍生，使用 locally administered unicast 位址；既有安裝保留已分配 MAC，停用再啟用仍保留，避免破壞 DHCP reservation。產品不登入路由器，不宣稱已替使用者建立 DHCP reservation。

## 還原點容量

Host 持續觀測根 LVM 的 classic CoW snapshots。使用率達 60% 即顯示白話警告；有可用 VG 空間時自動擴大並讀回。沒有足夠容量時阻擋新的大型 snapshot／restore 寫入；危急或失效時仍讓家庭繼續運作，顯示警告；只有 Owner 按「暫停 HA 以保護還原點」才正常關機。已失效的還原點不提供這個保護動作。失效、無法讀取及非 LVM 根磁碟各有獨立狀態。任何自動路徑都不刪除使用者既有快照。

背景約每 60 秒輪詢，主機狀態沿用同一份短期快取，並在大型複製前後立即讀回。輪詢及 Owner 選擇停止 HA 都不能保證其他 Linux 程式完全停止寫入；根檔案系統仍在使用，容量不足不等於整機恢復已有保證。產品不凍結根磁碟、不強制刪除快照，狀態保留限制與處理結果。

## 範圍、遷移與驗證

只接受目前啟用且實體證明成立的 Ethernet／USB Ethernet，排除 Wi-Fi、bridge、VLAN、bond、tunnel 及已被其他介面接管的 NIC。Wi-Fi-only multicast relay 屬另一項交付；此變更保留 NAT fallback，不宣稱解決所有網路拓撲。

家庭 LAN 會讓 HA 原生服務與已開放連接埠的 Apps 能從所選家庭網路連線；各自的 HA 登入、TLS 及配對保護維持。只接入信任的家庭網路。

這項交付包含 Host 程式及 Controller。現有 Controller-only updater 無法升級舊 Host；既有裝置需受測的 Host 遷移流程，不能重跑 installer 覆蓋使用中的家庭。舊 LAN choice 記錄與 MAC 會保留；新增觀測欄位不更改 Controller SQL schema。舊程式不理解新的單步流程與容量保護，不能宣稱跨版本功能退版已驗證。

單元、瀏覽器及隔離 libvirt container 測試是工程證據，不能取代實體 NIC、DHCP、HAOS／Core、路由器 multicast 或家庭設備控制驗收。本次不連線或修改 Dell-agentic-home、Castle1。
