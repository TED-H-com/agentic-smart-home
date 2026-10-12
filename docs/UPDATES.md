# 管理中心更新、退版與遠端連線

HA 保留官方更新；外層負責本產品的受測發行與已授權智能管家 App 的有限維護，見 [發行策略](RELEASE_POLICY.md)。不攔截 HA 更新，也不停止 Linux 安全更新。下文描述 **0.7.0 固定 Controller updater**，不是全系統更新／退版聲明。

上游 adapter 與產品 image 更新分開處理，見 [相容邊界與候選測試](ADAPTERS.md)。候選測試不會自動部署；目前也沒有 Linux／HAOS／Core／Hermes 全套自動退版承諾。

0.7.0 的固定 updater 只更新 **外層 Controller image**，相同 Controller schema 3、Runner protocol 1、worker contract 1。manifest 另綁目前 CLI／Hermes／Core／網路 runtime 指紋；不在同次升級 OS、HAOS、Core、Runner、Hermes 或網路。較舊 schema 的向前遷移與同 schema 的 image 退版分開處理。有智能管家 bridge 歷史時（即使已撤銷），候選仍必須支援 bridge contract 1；0.6.1 缺少此契約，不能僅憑 schema 3 就直接退回。

**舊 Host 只換 Controller image，不會取得新版固定更新器。** 側欄首次配對、管理及使用前，Controller 必須從固定 Unix Host broker 取得 `controller_update.ingress_bridge_contract: 1`；值由 Host updater 實作提供，資料庫設定或產品版本號不能代替。證明只保存在程序記憶體並定期重驗，Host 過舊、失聯或證明失效時停止側欄連接與其工作，外層救援仍可用。0.7.0 媒體包含這項 Host 保護；既有 0.6.x Host 的就地升級流程尚未交付，不能用 Controller-only 更新宣稱整套升級完成。

## 發行者與候選

產品預設不附虛造的可信發行者。Owner 在本機核對發行者 Ed25519 公鑰與 image repository 後，以固定 CLI 設定信任：

```bash
agentic-home update-publisher-inspect --publisher example --repository ghcr.io/example/controller --public-key publisher.pem
sudo agentic-home update-publisher-add --publisher example --repository ghcr.io/example/controller --public-key publisher.pem --approval <上一步的完整review_digest>
```

這是管理者動作；Web、Hermes、worker 和 Unix broker 都沒有安裝信任公鑰的 API。只保存公開 key，私鑰由發行者離機保管。`update-publisher-revoke` 使用相同參數與精確 review digest 撤銷；換 key 需先撤銷舊 key。主機信任檔為 root-only `/etc/agentic-home/update-publishers.json`。

發行者提供 JSON `{manifest,signature}`，signature 為 canonical JSON 的 Ed25519 簽章。manifest 必須有 publisher、version、source_commit、不可變 repository digest、實際 image ID、相容矩陣、測試名稱／證據 SHA-256、發行／到期時間。它不接受 shell、hooks、Docker mounts 或任意 compose。詳見產生的 [Owner OpenAPI](../contracts/host-controller.openapi.json)。

在「Linux 主機 → 網路與系統維護 → 版本更新與退版」選取說明檔。主機驗簽、核對 repository、下載精確 digest、核對 image ID，保存候選。Owner 查看後取得綁 candidate digest、10 分鐘有效的批准；按下確認後才部署。

## 部署與退版

固定 updater 停止 Controller，核對沒有活動／未確認的 household、Hermes、worker 工作、lease 或尚未核對的 Supervisor App 操作，保存 SQLite 一致快照。候選在無家庭資料的結構性 DB 與隔離 probe 中檢查；正式切換受 updater gate 保護，不能在健康檢查途中透過 Web 恢復 AI 或派新工作。

啟動後實際驗證 HTTP、Owner session、版本、HA／Runner 與 pause 狀態。失敗時切回前 image；較晚發現問題時，「選擇退回前一版本」產生新的精確批准。退版保留目前 Live DB，不能拿部署前備份蓋掉新家庭操作。schema 或其他 runtime 已改變時拒絕同格式退版；狀態不確定時保留 `needs_reconcile`。

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

「網路與遠端連線」分開選擇位址與遠端身份：

- DHCP 保留既有自動網路。選「在路由器保留固定 IP」只保存偏好，需在路由器做 DHCP reservation；產品不假裝已代設路由器。
- Tailscale 使用打包的鎖定官方 deb；以官方登入確認此節點。產品不接受未知 auth URL，也不啟用 exit node、subnet route 或 Tailscale SSH。
- 另一台已登入 tailnet 的電腦使用畫面提供的標準 OpenSSH forwarding，仍須原 Linux SSH 身份。localhost Web／HA OAuth origin 維持原樣；管理與 HA listeners 不直接對 LAN 或 tailnet 開放。
- 選擇停用或「登出此主機」会撤銷本產品管理的節點登入，不能只是隱藏設定。既有非本產品管理的 Tailscale 身份不被接管。
- SSH forwarding 同時包含管理中心 8099、HA 8123、PNG 預覽 127.0.0.2:8100。本機 console 与 localhost Web 保留救援用途。

真 tailnet 帳號、遠端 ACL 與失網現場驗收仍須另測；官方套件 hash／簽署來源驗證不能取代真帳號連線證據。mDNS、Matter、USB 和實體網卡 bridge 仍按個別家庭場景驗證。
