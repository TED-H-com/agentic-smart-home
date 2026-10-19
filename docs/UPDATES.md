# 管理中心更新、退版與遠端連線

HA 保留官方更新；外層負責本產品的受測發行與已授權智能管家 App 的有限維護，見 [發行策略](RELEASE_POLICY.md)。不攔截 HA 更新，也不停止 Linux 安全更新。下文描述 **固定 Controller updater**（0.7.0 起），不是全系統更新／退版聲明。

Host 的 Controller 更新不會自動更新側欄 App 或 Home worker App。兩者的信任 repository 都是 `https://github.com/TED-H-com/agentic-smart-home#stable`（發布時 `stable` 指到與 `release/X.Y.Z` 相同的 commit），版本都來自 `agentic_controller.VERSION`。同一 repository 上較舊的安裝進入 `update_available`，Owner 以無 body 的 `POST /api/host/bridge/maintenance/update` 或 `POST /api/host/home-worker/maintenance/update` 更新到恰好該版本。商店 `version_latest` 較高時是 `*_release_ahead`，不安裝也不更新；較低時依保存的重試次數與退避重新載入商店（最多三次），仍較低則 `waiting`／`*_release_not_published`。舊的 `#release/X.Y.Z` 是另一個 App slug，進入 `needs_reconcile`／`*_release_channel_changed`，須在 HA 手動移除舊 App 再從管理中心啟用。不會自動解除安裝、加入 repository 或重裝，也不改權限或 options，沒有自動退版。真實 Supervisor 從 0.9.0 更新到下一版仍是 `not_run`。

0.10.0 包含新的 Host 收集器及安裝前檢查，需要同版完整媒體；Controller-only 更新不能把舊 0.9.0 主機升成完整 0.10.0。當 `#stable` 推進到 0.10.0，舊 0.9.0 ISO 的新安裝可能遇到 `*_release_ahead` 而無法首次安裝 Apps。新主機須使用發布頁上已通過驗證的同版媒體；維護者必須先完成媒體與公開來源配套核對，再把它交給使用者。已使用的主機不得以再次 `host/install.py --apply` 代替升級。

公開來源 `53c9e79` 的 `#stable` 商店 slug、側欄 App 與 Home worker 共用 repository，已在真 HAOS 的 [第二輪公開交付](reviews/2026-10-16-PUBLIC-DELIVERY-53c9e79.md) 核對通過。該工程案例預載受測 base 映像，Host 能力與模型是 fixture，沒有登入真雲端帳號；不是跨版本更新或 Owner 家庭驗收。

更新失敗但舊 App 仍完好時，紀錄回到 `update_available`，只能由 Owner 再按一次更新。不會重送已經送出的安裝、設定、啟動或更新。

| 代碼 | 意義 |
|---|---|
| `bridge_update_failed` | 側欄更新沒有套上；舊 App 未改，Owner 可以再更新 |
| `home_worker_update_failed` | Home worker 更新沒有套上；舊 App 仍是有效設定（含新的配對碼），Owner 可以再更新 |

停用的側欄重新啟用時，先在目前已安裝的版本重新連線，健康後才提供更新。停用期間若已在 Home Assistant 更新到這一版，重新連線直接採用這一版。

設定或重新連線尚未完成時：已送出的安裝、設定、啟動與重新連線在釘選的舊版做完，進入 monitor 後才提供更新。還沒安裝任何舊版（`repository`，或 `install` 尚未送出）則改釘這一版並繼續安裝。Home worker 還沒找到 App，或 `repository`／`install` 階段 App 不在時，同樣改釘這一版後繼續安裝；已完成的舊版 install 先記下安裝，再提供更新。

側欄與 Home worker 都在本機建置。讀回寬限從 Supervisor 工作完成，或第一次看到目標版本的 `done_at` 起算，不從送出更新的時間起算。工作仍在跑會延長期限，到最多一小時，不重送。

上游 adapter 與產品 image 更新分開處理，見 [相容邊界與候選測試](ADAPTERS.md)。候選測試不會自動部署；目前也沒有 Linux／HAOS／Core／Hermes 全套自動退版承諾。

固定 updater 只更新 **外層 Controller image**，相同 Controller schema 3、Runner protocol 1、worker contract 1。manifest 另綁目前 CLI／Hermes／Core／網路 runtime 指紋；不在同次升級 OS、HAOS、Core、Runner、Hermes 或網路。較舊 schema 的向前遷移與同 schema 的 image 退版分開處理。有智能管家 bridge 歷史時（即使已撤銷），候選仍必須支援 bridge contract 1；0.6.1 缺少此契約，不能僅憑 schema 3 就直接退回。

**舊 Host 只換 Controller image，不會取得新版固定更新器。** 側欄首次配對、管理及使用前，Controller 必須從固定 Unix Host broker 取得 `controller_update.ingress_bridge_contract: 1`；值由 Host updater 實作提供，資料庫設定或產品版本號不能代替。證明只保存在程序記憶體並定期重驗，Host 過舊、失聯或證明失效時停止側欄連接與其工作，外層救援仍可用。0.7.0 媒體包含這項 Host 保護；既有 0.6.x Host 的就地升級流程尚未交付，不能用 Controller-only 更新宣稱整套升級完成。

## 官方更新來源與 Owner 批准

一般使用者從「設定 → 主機維護 → 版本」完成，不需要終端機或 sudo。這個路徑只允許信任**受信任安裝媒體已附帶的官方公鑰**，不能從瀏覽器、聊天、worker 或更新檔加入任意公鑰／repository。

Ted 已於 **2026-10-19** 核准正式 Ed25519 **公鑰**及其 DER SHA-256 fingerprint；`host/official-update-channel.json` 已釘選兩者與精確 Controller image repository `ghcr.io/ted-h-com/agentic-smart-home-controller`，啟用原始碼中的官方更新來源。公鑰為：

```pem
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA1fUHnQTX7LEJxfdrkmuvpdPw7r8uBwozTfqns+na97c=
-----END PUBLIC KEY-----
```

DER SHA-256 fingerprint：`18dd73f8b96d115d076be6b362cd62ed011c4be2b6dfdaf808e9b3e5a6a53d39`。正式私鑰由發行端持有，保存在本 repo 之外，不加入原始碼、產物內的來源或日誌。

這項釘選不代表已有簽署產物可下載：**尚未發布以此正式金鑰簽署的 Controller image 或 Host bundle**。既有工程預覽媒體也不會因此改變。`SHA256SUMS` 與 bundle hashes 只能檢查檔案完整性，不是發行者身份簽章；保留 Ubuntu 的 signed boot chain 也不等於重新製作的產品 ISO 已簽章。官方 pin 仍須透過已驗證身份的發行媒體或受控 Host 遷移交付，不把 GitHub 帳號、`stable` branch 或現場下載的 key 當作信任根。

新安裝只有在媒體帶有有效、相符的官方 pin，且尚未存在本機信任設定時，才使用該 pin 建立預設來源。已存在的設定（包含明確清空／撤銷）不會被安裝預設值覆寫。既有安裝不因 Controller 或 Host 程式升級就自動加入信任。

既有安裝的 Owner 必須分別批准：

1. **信任官方更新來源**：在版本頁檢查 `TED-H-com/agentic-smart-home`、頻道、完整金鑰指紋及「只更新管理中心」範圍，按確認。這只保存媒體已附帶的 pin，不下載、不部署任何版本。確認綁定當前媒體 pin、本機信任版本與短效一次性憑據；重新登入、逾期或 pin 改變時必須重新檢查。
2. **更新指定版本**：選取發行者提供的簽署更新說明檔。產品驗簽、核對不可變 image digest、相容性與隔離檢查後，再確認這個精確候選版本。信任來源不代表批准未來所有更新。
3. **需要時退版**：另行檢查前一版本並確認新的退版批准；更新批准不兼作退版批准。

舊 Host 不認得這些固定端點時，版本頁會說明需要受信任的主機更新。現有 Controller-only updater 無法交付 Host broker、安裝 pin 或整機遷移；不能在已使用的 Dell 上重跑 installer 代替遷移。正式公鑰已由 Ted 核准並釘選，但既有安裝仍需經身份驗證的 Host 遷移交付 pin，並由 Owner 在產品內明確信任來源；本變更不宣稱已交付簽署媒體或完成舊裝置遷移。

本機 root 的進階救援工具仍保留 `update-publisher-inspect`、`update-publisher-add`、`update-publisher-revoke`；它們能管理自訂發行者，要求精確 review digest。這不是一般使用者的操作步驟。Web 的新端點只能批准固定官方 pin，沒有通用 key 匯入能力。信任檔仍是 root-only `/etc/agentic-home/update-publishers.json`。

發行者提供 JSON `{manifest,signature}`，signature 為 canonical JSON 的 Ed25519 簽章。manifest 必須有 publisher、version、source_commit、不可變 repository digest、實際 image ID、相容矩陣、測試名稱／證據 SHA-256、發行／到期時間。它不接受 shell、hooks、Docker mounts 或任意 compose。詳見產生的 [Owner OpenAPI](../contracts/host-controller.openapi.json)。

在「設定 → 主機維護 → 版本」選取說明檔。主機驗簽、核對 repository、下載精確 digest、核對 image ID，保存候選。Owner 查看後取得綁 candidate digest、10 分鐘有效的批准；按下確認後才部署。

## 部署與退版

固定 updater 停止 Controller，核對沒有活動／未確認的 household、Hermes、worker 工作、lease 或尚未核對的 Supervisor App 操作，保存 SQLite 一致快照。候選在無家庭資料的結構性 DB 與隔離 probe 中檢查；正式切換受 updater gate 保護，不能在健康檢查途中透過 Web 恢復 AI 或派新工作。

0.10.0 的固定 updater 同時核對側欄與 Home worker 的持久維護紀錄；即使 App 已停用，尚未清除的操作仍阻擋切換與失敗回復。畸形紀錄也會阻擋，不會當成閒置。已安裝的 0.9.0 Host 更新器沒有因此自動升級；這項修正由新 Host 媒體交付，精確媒體的實際驗證見 [驗證紀錄](VERIFICATION.md)；單換 Controller image 不會套用。

啟動後實際驗證 HTTP、Owner session、版本、HA／Runner 與 pause 狀態。失敗時切回前 image；較晚發現問題時，「退回前一版本」產生新的精確批准。退版保留目前 Live DB，不能拿部署前備份蓋掉新家庭操作。schema 或其他 runtime 已改變時拒絕同格式退版；狀態不確定時保留 `needs_reconcile`。

image 含 `/app/source/agentic-home.bundle` 時，staging 只用 `docker create`／`docker cp` 取出 bundle，不啟動容器。唯一 head 必須等於 manifest `source_commit`，才匯入 Host coding bare repo 的 `refs/agentic/candidate/<image digest>`；此時 HEAD 不動。bundle 由 `scripts/controller_source.py` 在建置時寫入，image digest 已涵蓋它，manifest 格式不變。沒有 bundle 的 image、或主機沒有 coding source 時，照舊部署，該筆 `source_sync` 為 `unavailable`。來源只從已驗證的本機 image 取出，不從網路 fetch。

候選通過 smoke、Controller 再次停止並確認沒有工作、lease 或 `stop_unconfirmed` 後，HEAD 才以 compare-and-swap 移到 `source_commit`，`coding/source.json` 以原子寫入改寫，然後才正式啟動候選。之後任何失敗退回前 image 時，來源一起退回。退版用同樣方式把 HEAD 與 `source.json` 換回更新前。每一步都可重做：中途斷電後，退版會完成同一次移動。HEAD 或 `source.json` 不是這筆更新的前後狀態時，updater 不改動它，前一個 Controller 照常恢復，紀錄留 `needs_reconcile`，需要人工核對 `coding/source.json` 與 bare repo HEAD。

`GET /updates` 增加 `deployed_source_commit`（目前 `source.json` 的 `commit`；檔案缺失或無效時為 null）。每筆記錄增加 `source_sync`：`pending`、`moved`、`unavailable`、`reverted`、`needs_reconcile`。既有欄位保留。已安裝 VM 的更新探測用一個一次性 commit 驗證實際移動與退版；該探測實跑前，這只是離線測試結果。

固定 updater 的 mode-0600 SQLite 快照包含部署時的完整 Controller DB，按目前 `audit_days`（預設 30 天；待同步的較短設定也生效）於啟動、維護狀態讀取及每 15 秒背景檢查清除。`deploying`、`rolling_back` 或 `needs_reconcile` 的復原快照保留到狀態解除；無法安全讀取政策時先保留並重試。資料庫遷移快照也採稽核保存期限。此清理不刪 image，不以舊快照覆蓋 Live DB；自行匯出的備份需另行管理。

Web 不可用時的固定命令：

```bash
agentic-home updates
agentic-home update-stage --manifest signed-update.json
agentic-home update-apply --candidate <candidate_digest> --approval <approval_id>
agentic-home update-rollback-offer --candidate <candidate_digest>
agentic-home update-rollback --candidate <candidate_digest> --approval <新rollback_approval_id>
```

命令參數以 `agentic-home --help` 為準。正常操作用安裝時的主要 Linux Owner 帳號；本機 root 救援可切至該 UID 1000 帳號呼叫 broker。Controller 更新批准不等於家庭設備批准。

## 主機工作續查

逾時或連線中斷不代表操作未執行。保留 CLI 印出的 `Operation <id>`，以**同一個 ID**查詢或繼續等待：

```bash
agentic-home job --id <原工作id>
agentic-home wait --id <原工作id>
```

`job` 只讀一次；exit 0 表示查詢成功，仍須看 `state`。`wait` 每 2 秒查同一工作，每次讀取遇到暫時性錯誤時最多嘗試 3 次，不重送操作；工作仍為 `running` 就繼續等，目前沒有 `--timeout` 選項。`wait` 的 exit 0 表示 `succeeded`，exit 1 表示 `failed` 或 `needs_reconcile`，exit 2 表示結果無法確認，包括查無 ID。讀取失敗、查無工作或 `needs_reconcile` 都不能當作未執行而重新提交；先核對實際狀態。一般 `failed` 也可能已有部分效果。

這兩個命令適用於 `provision`、`start`、`shutdown`、`snapshot`、`restore`、`bootstrap_hermes`，只讀已保存的工作，不依賴完整主機狀態探測。Controller 更新／退版使用獨立的 candidate 記錄；以 `agentic-home updates` 查對原 `candidate_digest`，不要拿主機 job ID 代替。

## 網路選擇

「網路與遠端」分開選擇位址與遠端身份：

- DHCP 保留既有自動網路。選「在路由器保留固定 IP」只保存偏好，需在路由器做 DHCP reservation；產品不假裝已代設路由器。
- Tailscale 使用打包的鎖定官方 deb；以官方登入確認此節點。產品不接受未知 auth URL，也不啟用 exit node、subnet route 或 Tailscale SSH。
- 另一台已登入 tailnet 的電腦使用畫面提供的標準 OpenSSH forwarding，仍須原 Linux SSH 身份。主機的 localhost Web／HA OAuth origin 維持原樣；這項遠端功能不公開管理 listener。可選有線接入是另一條 HAOS 家庭 LAN 路徑，其 HA／App 服務的可見範圍見 [家庭區網接入](HA_LAN.md)。
- 選擇停用或「登出此主機」会撤銷本產品管理的節點登入，不能只是隱藏設定。既有非本產品管理的 Tailscale 身份不被接管。
- SSH forwarding 同時包含管理中心 8099、HA 8123、PNG 預覽 127.0.0.2:8100。本機 console 与 localhost Web 保留救援用途。

真 tailnet 帳號、遠端 ACL 與失網現場驗收仍須另測；官方套件 hash／簽署來源驗證不能取代真帳號連線證據。0.10.0 的有線 HAOS 接入有獨立媒體／平台工程紀錄，見 [驗證紀錄](VERIFICATION.md)；mDNS、Matter、USB 與實體網卡仍按個別家庭場景驗收。


商店重載成功但仍回舊版時，也會使用同一個有限重試預算；重啟不重置次數或退避期限。用完三次後停止自動重載，Owner 可按「重新檢查版本」授權新的一輪商店查詢。這個動作只重置商店重載預算，保留原本階段與操作狀態；已有未確認的 App 操作時不重置，也不重送 install、start 或 update。商店恢復正確版本後，既有較舊 App 仍等待 Owner 明確同意更新。
