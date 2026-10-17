# Home Assistant 家庭區網接入 · 0.10.0 候選

本頁描述 0.10.0 候選的有線家庭接入流程與網路契約。精確 ISO、巢狀 VM 的 LAN 通路與公開 App 組合仍待本輪驗證，結果以 [驗證紀錄](VERIFICATION.md) 為準；不能據此宣稱實體網卡、Matter 或全屋設備已通過。

外層「探索更多設備」可以取得 LAN 觀測，但 HAOS 也需要自己的家庭網路通路，才能使用本機探索、設備回呼及手機的 HA 原生入口。候選保留原管理網路，讓使用者另外選擇有線接入；未選擇時維持基本 NAT。

## 第一次設定

1. 筆電插 AC 電源，Ethernet 接家庭主網路。可使用系統辨識為實體有線介面的 USB Ethernet；路由器需為 HAOS 的另一個 MAC 提供 DHCP。
2. 在首次設定按「開始設定」之前，展開 **先設定家庭區網（有線設備探索）**，明確選擇正在使用的有線介面，按 **保存家庭接線**。HAOS 尚未建立時可以先保存，稍後自動建立家庭時套用。
3. 照原流程完成 CLI、管家、通訊、HA 原生家庭設定與 Owner 授權。保存接線只表示主機配置完成，不能當作 HA 已取得位址。
4. 家庭授權完成後，查看實際讀回的 HAOS 介面、LAN 位址與 HA 網路選擇。需要時依畫面到 **HA 設定 → 系統 → 網路** 選擇家庭介面或調整內部網址，再返回讀回。
5. 用同一家庭網路的手機開啟讀回的 HA LAN 網址，以 HA 自己的帳號登入。先核對一個設備的探索、狀態及指定功能，再擴大接入。

列表只接受目前啟用且有實體證明的 Ethernet／USB Ethernet，排除 Wi-Fi、VLAN、bridge、bond、tunnel，以及已被其他介面接管的網卡。沒有候選時先檢查實體接線；不要把 Wi-Fi 名稱填成 Ethernet，也不要讓模型提供任意網卡或命令。

## 已建立家庭時切換或復原

先結束相關開發工作，在 **主機維護** 正常關閉 Home Assistant，等狀態明確為已關機。展開 **家庭區網接入**，選介面並按 **保存家庭接線**，完成後再啟動 Home Assistant。切換會暫停 HAOS 及其自動化，不能在執行中的 VM 熱改網卡。

要回復基本網路，同樣正常關機，改選 **暫不接入，使用基本網路**，保存後再啟動。這會移除產品增加的第二張網卡並恢復 NAT DHCP 預設 gateway；家庭資料與整合仍保留，但依賴 LAN 通路的設備需要重新核對。若曾在 HA 手動指定家庭介面或 LAN 內部網址，也需在原生網路頁改回適合基本網路的設定。

配置中斷時，主機保留操作紀錄並阻擋不確定狀態下的啟動／建立。先查原工作；按照畫面重試同一選擇或明確移除。不要手改 libvirt XML、刪除工作紀錄或反覆重建 VM 來繞過核對。外層 localhost 管理與固定 console 救援維持獨立，見 [操作與復原](OPERATIONS.md)。

## 兩張網卡的分工

| 路徑 | 配置與用途 |
|---|---|
| 產品管理網路 | 保留 `192.168.177.2`、原 MAC 與 NAT NIC；外層 Controller、側欄配對及 localhost 8123 代理繼續走這條路。 |
| 家庭 LAN | 第二張 virtio NIC 透過 libvirt direct／macvtap `bridge` mode 接到所選有線介面；由家庭 DHCP 提供獨立位址，承接家庭設備通訊。 |
| 預設路由 | 有線模式取消產品管理網路 DHCP 所發送的預設 gateway，讓家庭 LAN DHCP 提供 guest 預設路由；回復基本網路時恢復原設定。 |
| 外層 Linux | 不重寫主機 IP、預設路由或 NetworkManager／Netplan，也不建立實體 Linux bridge。外層管理中心 8099 繼續只聽 localhost。 |

macvtap 本身不讓 guest 經同一實體網卡直接回到所在主機；保留管理 NIC 是為了讓外層與 HAOS 仍能互通。這是 libvirt 記錄的行為，不能拿外層無法連 HA 的 LAN IP 判定整個接入失敗。[libvirt direct interface](https://libvirt.org/formatdomain.html#direct-attachment-to-physical-interface)、[macvtap 的主機通訊限制](https://wiki.libvirt.org/TroubleshootMacvtapHostFail.html)。

受信任 Host 使用 `GET /network/ha-lan` 列出選擇；既有固定 action `configure_lan` 只接收當下有效的 opaque `interface_id`，null 表示移除。它核對產品 VM／網路身份，先保存操作意圖，再修改並讀回。Controller 在家庭 Owner 授權後，透過原生 Supervisor／Core API，依第二張網卡的 MAC 核對 guest 介面、位址、HA adapter 選擇與內部網址。它不登入路由器、不自動改 HA 網路偏好，也不把保存的選擇當作設備已在線。

## 接入範圍與驗證

有線接入讓 HAOS 成為家庭 LAN 上的一台主機。HA 原生 HTTP 與已發布的 App 服務可能被同網段存取，例如家庭現場 Runner 的 TLS 8443；它們維持各自的 HA 登入或 App TLS／配對認證。外層管理中心 8099 沒有因此公開到 LAN。只接入你信任的家庭網路，不把此模式當作公開網際網路代理或遠端存取方案。

- **HA 原生探索**：仍受 HA 當前 adapter 選擇、路由器 multicast 與設備行為影響。HA 的自動選擇與路由有關；曾手動選過介面時需讀回核對。[HA Network Configuration](https://www.home-assistant.io/integrations/network/)。
- **Google Cast／TTS／本機媒體**：設備必須能存取 HA 提供的媒體網址。若內部網址仍是 localhost 或管理 NAT 位址，依畫面到原生 HA 改成設備可達的 LAN 網址；儲存介面不能取代一次真正播放測試。[HA Cast](https://www.home-assistant.io/integrations/cast/)。
- **Matter／Thread**：需要本地 IPv6、mDNS、合適的 Thread border router，以及必要的手機 Companion 配網流程。有線網卡只是通路的一部分；不能由 IPv4 位址、一次 mDNS 回應或 VM 測試推成 Matter 已驗收。[HA Matter](https://www.home-assistant.io/integrations/matter/)。
- **其他網路**：Wi-Fi-only、訪客隔離、VLAN、企業交換器多 MAC 限制、USB dongle 與實體 NIC 驅動仍需各自的接入方案或現場驗收。基本 NAT 保留外層與雲端／可路由 IP 整合的使用路徑。

驗證依序區分「選擇已保存」「libvirt 配置已讀回」「guest 取得家庭位址／路由」「HA 選擇正確介面」「來源觀測成功」與「指定設備功能／實體動作通過」。工程 fixture、巢狀 VM、實體 Dell 與家庭帳號各自留證據；72 小時持續運作仍另行記錄。
