# 原生家庭入口與上游相容邊界

Home Assistant 是家庭日常介面；智能管家提供研究、對話、提案與成果；外層 Linux Web 保留初始化及救援。這項分工不改變外層單一 Hermes、Host／Home workers、Controller 權威或原有家庭批准流程。

目標是讓上游變動可被辨識、隔離與驗證，不保證任何未來版本都相容。產品不修改 HA frontend DOM、不 fork HAOS、不把模型生成的程式寫入 Linux 系統服務。上游內部 Python 接點也不會因為包成 adapter 就變成穩定官方 API。

## 已實作的邊界

| 接點 | 產品控制的介面 | 相容失敗時 |
|---|---|---|
| Web → 原生 HA | `GET /api/host/home-entry`，contract 1；只回固定 `/`，不接受任意 redirect URL | 外層入口仍可用；不重設 onboarding、不重送工作 |
| Controller → Hermes bridge | `/v1` HTTP、既有 case/session/run 與 MCP 工具契約 | 保留 case 與錯誤狀態；不隱式換 provider |
| bridge → Hermes 上游 | `agentic_hermes/upstream.py` 集中 AIAgent、MCP 與 Codex auth 相依，adapter contract 1 | 候選 probe 失敗便不接受；既有版本不由候選測試更換 |
| Controller → HA Core | 官方 REST／WebSocket；讀取與家庭寫入分開驗證 | 未測 Core 可繼續原生操作；Agent 自動化寫入沿用受測版本限制 |
| App → Supervisor Ingress | 只信任官方 ingress 來源、再驗有效 Owner | 不把任意 header 或一般 HA 使用者當外層 Owner |
| Host broker → Linux／libvirt／Docker | 固定指令、JSON／XML 與實际功能探針；每次 mutation 重驗必要契約 | 只拒絕受影響操作，保留狀態與獨立救援 |
| Host → HA onboarding | 集中既有原生步驟探針，明示 `upstream_private_volatile` | 404、未知必需步驟或壞格式不推論設定完成 |

官方介面依據：[HA REST](https://developers.home-assistant.io/docs/api/rest/)、[HA WebSocket](https://developers.home-assistant.io/docs/api/websocket/)、[Ingress](https://developers.home-assistant.io/docs/apps/presentation/#ingress)、[Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)、[systemd 穩定性範圍](https://systemd.io/PORTABILITY_AND_STABILITY/)。Hermes AIAgent、部分 OAuth helper 與 HA onboarding 不是本產品能保證穩定的接點，因此另有集中檢查與候選行為測試。

## 設備探索

多來源盤點的 Controller adapter 為 `network_inventory.py`，Host adapter 為 `agentic_host/discovery.py`，契約各為 v1。`GET /api/discovery/inventory` 讀保存結果，`GET .../scopes` 讀主機當前範圍；`POST .../scan|import|clear` 是經 Owner 明確操作的收集／本地資料管理，使用去重鍵和回應落盤後身份檢查。Ingress 僅精確允許這五個路徑，不開放任意 Host namespace。模型沒有新工具或任意網段參數。

Host 僅接受固定 Unix `GET /discovery/scopes` 與 `POST /discovery/scan`，UID 邊界沿用 daemon。實體 IPv4 介面重新確認後，使用固定 `ip -j` 讀取鄰居與介面，以及 [RFC 6762 mDNS](https://www.rfc-editor.org/rfc/rfc6762.html)／[UPnP SSDP](https://upnp.org/specs/arch/UPnP-arch-DeviceArchitecture-v2.0.pdf) 的有界查詢。契約版本 1 不下載服務 URL、不做連接埠掃描，也不推斷虛擬網路拓撲。目前僅從同封包、精確 HTTP PTR 實例的唯一 TXT 廠商宣告投影固定 `vendor=synology` 分類，不保存原始 TXT 值或其他欄位；畸形或重複宣告不分類。mDNS 位址是回應者，服務代理可能代答。固定服務查詢、辨識規則摘要版本 2 及舊 Host 限制見 [設備探索](DEVICE_DISCOVERY.md#常見網路服務帶入聊天)。

辨識依據固定於 Core 2026.10.1 的官方 manifests：[Hue](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hue/manifest.json)、[Cast](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/cast/manifest.json)、[ESPHome](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/esphome/manifest.json)、[HomeKit Device](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/homekit_controller/manifest.json)、[Shelly](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/shelly/manifest.json)。規則只產生建議；服務宣告仍可能偽造，名稱／廠商／匯入服務欄位只作 hint。[Tuya 官方設定](https://www.home-assistant.io/integrations/tuya/) 的帳號、User Code 與 QR 由 HA 原生流程處理，盤點不索取憑證或宣稱讀到完整 Google Home／Alexa 帳號。

通用路由器匯入只讀 JSON／CSV／dnsmasq 文件，格式與欄位白名單見 [匯入格式](DEVICE_DISCOVERY.md#匯入格式)。這與連到廠商管理 API 的 adapter 分開；產品不假裝已登入未配置的防火牆。HTTP／檔案輸入、網路宣告、HA 登錄資料的來源身份保持各自命名空間，沒有猜測合併。


設備探索新增產品契約 `GET /api/discovery`／`device-discovery-v1`，實作集中於 `agentic_controller/discovery.py`。只對已綁定且讀取前後仍有效的 HA Owner 回傳有界快照，不擴大 MCP。D3a 精確開放 Ingress `GET /api/discovery`；D3b 再精確開放 `GET/POST /api/discovery/setups`、`GET /api/discovery/setups/{32hex}` 及 `POST .../{32hex}/select|forget`，供 Owner 管理本地追蹤。這些路徑不代理任意 HA 請求。各來源失敗獨立標示，欄位白名單之外的設定、flow context、任意 URL 與錯誤本文不回傳。

D3b 的 `discovery_setup.py` 重用同一組唯讀 HA adapter，沒有新增上游設定命令。每次請求捕捉 Owner、家庭連線、Web 世代及 Host／bridge 身份，在 HA 讀取後與回應落盤後再核對。追蹤綁定另排除短期 Web 世代，使同一授權範圍可以重新登入接續；家庭 token 或來源位址改變則隔離。來源失敗、候選消失與 entry loaded 都不形成實體成功聲明。

HA 探索 adapter 的來源基準為 [Core 2026.10.1 flow progress](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/config/config_entries.py) 及 [frontend 20260930.3 discovery 分類](https://github.com/home-assistant/frontend/blob/20260930.3/src/data/config_flow.ts)。使用唯讀 WS `config_entries/flow/progress`；registry/config-entry 命令與此 frontend 接點視為 `upstream_private_volatile`，不因 transport 是官方 WebSocket 就保證語意穩定。`reauth`、user/import/system 不列為新設備探索；重授權仍由原生設定處理。未知來源標示不完整。設備優先讀取 `config_entry_id`，相容舊 `config_entries`；`via_device_id` 與 `parent_device_id` 分別保留。完整方向與未實作來源見 [設備探索與接入](DEVICE_DISCOVERY.md)。

## 原生入口

只有無明確頁面的日常入口，才會在本產品初始化已完成、綁定 Owner 仍有效、Core 的配置及 frontend 能力讀回成功後進入 HA。新 Core 版本號或額外欄位本身不會阻止瀏覽。必要欄位遺失、服務未就緒或認證失效時，保留外層入口及手動開啟原生 HA 的路徑。此檢查不替代瀏覽器真正載入前端的驗證。

`#gateway`、`#host`、提案等明確連結不自動跳走。離開外層前把原本入口記成 `#entry`，瀏覽器返回與重新整理不會形成跳轉迴圈。HA 登入維持原生流程；不把 Host cookie 或 HA bearer 搬進另一個 origin。只使用同一 loopback hostname 的既有 8099／8123 路徑；遠端仍需既有 SSH forwarding，同時連通兩個端口。

## HA 側欄橋接（0.7.0 起）

獨立薄 App 經官方 Ingress 收取身分，再以專用 TLS／配對憑證連外層 Controller。外層重新驗證綁定的 active HA Owner；沿用 `source=web / chat=owner / sender=HA Owner`、同一 case／proposal／worker store。舊 Controller Ingress App 的第二份 DB 不能替代這個橋接。HA Assist／語音仍是不同接點。

Web 新增 `mode=bridge`，先確認橋接與 Owner，再掛載聊天；不顯示主機、provider、通道或更新設定。session 的 opaque `storage_scope` 隔離瀏覽器中的訊息 ID，server `resume` 提供同一外層對話。草稿留在記憶體；斷線先隱藏內容並停止操作，同一身份重新驗證後可續用，身份世代改變則不重用舊內容。提案深連結仍按原 digest／到期／Owner 取得精確批准；失去回應後使用同一請求識別，不建立第二份批准。

所有 API 與靜態資產保留 Ingress 相對路徑。成果文字按大小及 SHA-256 驗證，只作文字顯示；普通 LAN HTTP 缺少 SubtleCrypto 時使用鎖定的 `@noble/hashes` SHA-256。畫面只能由固定 preview ID 與 390／768／1440 尺寸取得 PNG，不能把任意 URL 或 HTML 代理到 HA origin。外層設定的固定維護操作不在橋接 allowlist。

[瀏覽器測試](../tests/test_bridge_ui.py) 使用協定 fixture，涵蓋相對路徑、同 case、精確批准、撤權、斷線、錯 digest 與三尺寸；它不是實際 Supervisor 或真雲端帳號證據。實际 App／Controller／HAOS 結果分別記在 [驗證](VERIFICATION.md)。

## 候選與發行

0.11.1 候選保留 Core **2026.10.1**，並將 **2026.10.2** 納入精確的自動化寫入相容範圍。[隔離真 Core 矩陣](../evidence/core-patch-matrix.json) 的八項實測涵蓋兩版的提案／批准／寫入與事件重連、Owner OAuth、原生通知及唯讀盤點；[Core .2 官方來源](../evidence/core-2026.10.2-source.json) 另記 tag、不可變 commit 與受測 image digest。Supervisor 備份回應仍是 fixture；HAOS／Supervisor 真備份、實體家庭及 72 小時驗收仍為 `not_run`。這項結果不授權更新家庭的 HA，也不變更 `upstream.lock.json` 的既有參考基準或其他依賴。

尚未發行的聊天接入 adapter 另外以 [IPP 原生設定矩陣](../evidence/device-setup-real-core-matrix-20261019.json) 核對這兩個精確版本的 `zeroconf_confirm` 空表單、設備身分與讀回。它不從自動化 adapter 的成功推定配對相容；未知 patch／預覽版不接受新的接入。真 Core 的 flow、aioipp 與登錄資料路徑保留，探索事件、印表機及 Supervisor 備份使用明示 fixture。聊天的 `home.device_setup_status` 只讀 Owner 當前訊息選定的工作，不能取得原生寫入或重送權限。

Shelly 接入使用獨立的固定 adapter：同兩個 Core patch、已存在的 `zeroconf`／`confirm_discovery` 空表單，以及原生回報型號恰為 `Shelly 1`。穩定身分以種類與正規化 MAC 綁定，不沿用 IPP 身分或名稱比對。這個回報欄位不是實體世代鑑定，原生初始化可能調整連線設定，須在批准前說明；原生 Gen1 協定測試也不代表所有 Shelly 或真實繼電器已驗證。支援範圍與 v1→v2 私有紀錄遷移、降版寫入停止規則見 [設備接入](DEVICE_DISCOVERY.md#聊天內確認接入-shelly-1)。

Synology NAS 接入使用獨立的多步 adapter：同兩個 Core patch、SSDP／zeroconf 發現的 `link` 表單與 12 位序號身分。登入、兩步驟驗證與備份資料夾三種步驟回應都須符合已審閱的精確形狀；其他步驟、自訂欄位或含帳密預設值的表單改由原生設定入口處理。完成判定以 NAS 網卡 MAC 比對原生設備登錄，帳密不進入接入紀錄或請求收據。範圍見 [設備接入](DEVICE_DISCOVERY.md#聊天內輸入帳號接入-synology-nas)。

### HA 原生通知 adapter（主線開發中）

`custom_components/agentic_home_notifications` 是獨立版本的原生 HA 整合，產品 protocol 為 1。HA 自動化呼叫 `agentic_home_notifications.send_text`，外層 Controller 透過既有、經 Owner 認證的 HA WebSocket 消費有界佇列，再由原有 Gateway 傳送。沒有第二個 bot receiver、公開 listener、任意 webhook URL 或新的管理 bearer；整合不持有 LINE／Telegram／Discord 憑證。安裝與範例見 [原生通知](HA_NOTIFICATIONS.md)。

收件者別名只能指向已配置的通道聊天室。每則請求固定來源、綁定世代、路由版本、內容摘要及到期時間；更換收件者或通道憑證不能讓舊通知改送新目的地。HA 的持久化 claim 只授予一次，Controller 先持久化送出狀態，再呼叫 transport。程序中斷或外部結果不明時保留 `unknown`，不自動重送；`submitted` 僅代表 transport 接受，不代表對方已收到或已讀。這不構成跨平台 exactly-once 保證，也不能把任意備份回復視為未曾送出。

通知授權獨立於 AI 暫停、模型登入及家庭 AI 政策；停用通知使用自己的設定。HA Owner／連線憑證、收件人、通道憑證與 LINE 帳號世代的變更仍會使舊路由失效，更新維護期間亦停止新提交。某個通道離線時，其待送通知保留在有期限的佇列；已就緒的其他通道仍能處理。

這輪借鏡 [LINE OA Add-on](https://github.com/xangin/line-oa-ha-addon/tree/3729faa66788748c17345215cc027db67dd7c443) 與 [HACS integration](https://github.com/xangin/line-oa-hacs/tree/3254f4d67b3684a9a9a40849ccc59d98313caec3) 的原生 action、別名與分層狀態設計。沿用本產品既有帳號與 durable outbox；沒有移植 OA 私有管理端點或把其 session cookie 帶入 HA。鎖定的 LINE bot API 只支援文字，本輪不提供圖片或攝影機 action。新整合與 Controller 必須一起部署到受測候選；0.10.0 已發布媒體、tag 與 `stable` 保持原身份。

最新 [發行策略](RELEASE_POLICY.md) 明確讓 HA 走官方自管更新；維護者追上游並修補自家 App／adapter。Hermes 與外層封裝採受測產品版本；0.6.1 首次準備 latest 是歷史行為，新路徑另列驗證。未知 Core 家庭寫入仍 fail closed，原生瀏覽不因版本字串被阻止。

1. 保留產品及各上游鎖定來源。新候選解析一次 revision／digest，測試過程不修改正式 lock、帳號或已安裝版本。
2. 先驗產品 protocol/schema 與實際使用的能力，再跑隔離行為測試；不以版本字串或 import 成功取代完整握手。
3. Hermes 必須通過真上游模型迴圈、MCP、登入 helper 與負向路徑的離線 transport 測試。真 OAuth 同意與雲端研究另列，不能由 fixture 宣稱完成。
4. HA Core／Supervisor 變更跑真 Core／App、身份、事件重連、提案／批准、備份及讀回測試；HAOS／Linux 基礎映像變更還需精確媒體的一次性 KVM 安裝、還原與重啟。
5. 一次只更換一個上游組件，記錄確切版本矩陣。沒有證據的組合不能自動升級為受支援；安全更新仍需及時測試與修補，不以永久鎖版取代維護。

可在開發機執行只測試的 Hermes 候選：

```bash
python3 scripts/check_upstream.py --hermes locked --report evidence/upstream-locked.json
python3 scripts/check_upstream.py --hermes latest --report evidence/upstream-candidate.json
```

`Upstream compatibility candidate` GitHub workflow 提供相同的手動入口。它使用固定 repo、產品 adapter 與鎖定 dependencies，在獨立無網路容器驗證，限制時間／記憶體／程序與輸出，最後移除測試容器、volume、tag 及 scratch；不部署。結果分別保存輸入 source hashes 與實際候選 context hashes，另含候選 commit／image ID、契約版本及清理狀態；失敗和相容性未知都不當成 passed。

現有固定 updater 仍只處理 **同 schema 3 的 Controller image**；SQL schema 相同只是必要條件。任何 `ingress_bridge` 設定歷史，即使已 revoked，都要求候選固定 probe 證明 `BRIDGE_CONTRACT=1`，因此 0.6.1 不能直接按同 schema 退回。尚有 pending／未確認的 Supervisor operation 時阻止 Controller 切版，先核對持久狀態；不盲目回復舊 DB。Hermes 的上游候選 gate 不是使用中 Hermes 通用更新器。Linux 完整產品升級／退版尚需機制及驗證；HAOS／Core 沿用官方更新恢復流程，我們不接管。不能宣稱任意 OS 更新失敗都會自動退回。外層救援不依賴 HA 可用，HA 原生規則也不依賴 Hermes 持續運作。
