# 管理中心更新、退版與遠端連線

0.6.0 的固定 updater 只更新 **外層 Controller image**，相同 Controller schema 3、Runner protocol 1、worker contract 1。manifest 另綁目前 CLI／Hermes／Core／網路 runtime 指紋；不在同次升級 OS、HAOS、Core、Runner、Hermes 或網路。較舊 schema 的向前遷移與同 schema 的 image 退版分開處理。

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

固定 updater 停止 Controller，核對沒有活動／未確認的 household、Hermes、worker 工作或 lease，保存 SQLite 一致快照。候選在無家庭資料的結構性 DB 與隔離 probe 中檢查；正式切換受 updater gate 保護，不能在健康檢查途中透過 Web 恢復 AI 或派新工作。

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

## 網路選擇

「網路與遠端連線」分開選擇位址與遠端身份：

- DHCP 保留既有自動網路。選「在路由器保留固定 IP」只保存偏好，需在路由器做 DHCP reservation；產品不假裝已代設路由器。
- Tailscale 使用打包的鎖定官方 deb；以官方登入確認此節點。產品不接受未知 auth URL，也不啟用 exit node、subnet route 或 Tailscale SSH。
- 另一台已登入 tailnet 的電腦使用畫面提供的標準 OpenSSH forwarding，仍須原 Linux SSH 身份。localhost Web／HA OAuth origin 維持原樣；管理與 HA listeners 不直接對 LAN 或 tailnet 開放。
- 選擇停用或「登出此主機」会撤銷本產品管理的節點登入，不能只是隱藏設定。既有非本產品管理的 Tailscale 身份不被接管。
- SSH forwarding 同時包含管理中心 8099、HA 8123、PNG 預覽 127.0.0.2:8100。本機 console 与 localhost Web 保留救援用途。

真 tailnet 帳號、遠端 ACL 與失網現場驗收仍須另測；官方套件 hash／簽署來源驗證不能取代真帳號連線證據。mDNS、Matter、USB 和實體網卡 bridge 仍按個別家庭場景驗證。
