# 自家發行與 Home Assistant 更新邊界

0.11.1 候選採同版主機媒體與公開 App，並提供 #123 的正式 Host bootstrap／更新路徑交付 #122 的 G15／G16 政策；簽署產物與家庭遷移仍待實際驗證、發布。新的 Host 提供輕量側欄能力證明；Controller 僅在該固定 Unix 端點明確回覆 404 時相容舊 Host 的 `/status`，整段共用原本的證明期限。格式不符、認證失敗或逾時不走替代證明。這項相容讀取不代表 Controller-only updater 能升級 Host、Runner、Hermes 或網路實作；既有資料主機仍不可直接重跑 installer。

2026-10-12 Owner 最新方向：**Home Assistant 自行透過官方流程更新；外層負責自家產品與智能管家 App 的相容及修復。** 這取代先前要求把 HAOS／Core 一起納入產品釘版的政策。0.6.1 媒體與歷史驗證不因文件修改而改變；目前更新器範圍仍見 [Updates](UPDATES.md)。

## 使用者日常

家庭首頁與設備操作使用原生 HA。HAOS、Core、Supervisor、官方與第三方 Apps 的更新及備份沿用 HA 官方介面，外層不遮蔽更新入口、不攔截上游更新、不偷偷降版，也不修改 HA frontend。HAOS 雙 boot slot 不等於整個家庭資料可退版，Core 與 App 恢復仍依各自備份條件。[官方更新與恢復](https://www.home-assistant.io/common-tasks/os/)。

智能管家透過獨立薄型 Ingress App 連回外層單一 Hermes／Controller。HA 的側欄是對話、成果與精確批准入口，不是第二份 Controller DB，也不是另一個家庭執行引擎。HA 或管家不可用時，原生操作與外層救援依各自服務的可用性保持獨立。

新的 Core 版本號本身不阻止原生 HA 瀏覽。Agent 家庭寫入仍按已驗證的能力與版本檢查；遇到缺少必要語意、未知寫入相容性或失效 Owner 時，只停止受影響的能力。不能宣稱所有最新 Core 都已支援，也不能以修復管家為由更換 Core。

## 我們維護哪些元件

| 元件 | 發行及維護責任 | 限制 |
|---|---|---|
| 外層 Controller／Runner／Web／adapter | 產品來源 commit、不可變 image、schema／protocol 與相容測試 | 現有固定 updater 只支援同 schema Controller；不是全產品原子更新器 |
| Hermes、CLI、LINE.js 與封裝相依 | 產品受測版本及 lock；上游候選在維護端驗證 | 本輪首次安裝按 images.lock 取 Hermes digest 並離線 probe，不追 HEAD；使用中憑證／session 不能藉 bootstrap 重置 |
| 自家智能管家薄 App | 官方 Supervisor App API、產品發行 repo／版本與實際設定讀回 | Supervisor 信任 repo／branch，並非對整個 build 原子 digest pin；不把成功 heartbeat 當成 Owner 或模型驗證 |
| HAOS／Core／Supervisor／其他 Apps | 使用者與 Home Assistant 官方更新流程 | 我們觀測相容性、修自家 adapter；不自動接管或降版 |
| 外層 Ubuntu | 現有受測 LTS／媒體及官方安全更新 | 尚無完整 OS 受管理更新及資料退版；不全面關閉安全更新 |
| 雲端模型、OAuth、通訊服務 | 固定本產品使用的接點及行為契約 | 遠端服務會變更，不能由本機 lock 凍結 |

官方只支援最新 Supervisor，這也不能用產品 policy 規避。[Supervisor 支援條件](https://www.home-assistant.io/more-info/unsupported/supervisor_version/)。外層安全更新維持官方路徑；在可用的修補、發布與恢復流程建立前，不為版本一致性停止安全維護。[Ubuntu 安全更新](https://ubuntu.com/server/docs/how-to/software/automatic-updates/)。

## 側欄的事先授權與有限修復

新 onboarding 在「開始設定」附近說明完成後加入 HA 智能管家側欄，並可展開查看安裝及自動維護範圍。既有已完成設定的家庭不因升級便自動被接管；Owner 在外層設定明確啟用。

外層設定區分三個操作：啟用側欄（同時開啟自動維護）、停止自動維護（保留連接）、撤銷連接。撤銷會停用維護及 App bearer，不卸載 App、不停止家庭規則。讀取狀態不建立派工或安装。

Manager 只處理本產品指定 App。使用者手動停止、移除或改變 App 設定時，保留該選擇；不能循環重裝或重新啟動來抵銷人工操作。有限重啟最多兩次／24 小時，超限及效果不明時需核對；按「檢查側欄連線」不重設次數。相同版本的來源重建尚未自動化，不能把「自動維護」解讀為任意修 code 或任意更新。[Supervisor 官方 API](https://developers.home-assistant.io/docs/api/supervisor/endpoints/)。

目前維護範圍限首次安裝、同版本有限重啟，以及 Owner 明確要求的重新連接。同一 `#stable` 頻道上較舊的側欄或 Home worker App 由 Owner 更新到恰好這一版。舊的 `#release/X.Y.Z` 進入 `*_release_channel_changed`，須在 Home Assistant 手動移除。Home Assistant 本身仍透過官方流程更新，見 [Updates](UPDATES.md)。因此不能宣稱 HA 上游更新後都能自動恢復側欄，仍需查證實際相容性。

## 維護端候選與發布

0.10.0 工程預覽的發布入口為 [v0.10.0](https://github.com/TED-H-com/agentic-smart-home/releases/tag/v0.10.0)，工程驗證與公開下載結果：同一候選本機八階段（2+6 接續）已通過；公開 App：逐階段接續通過（enable 42、bridge-haos 40，清理完成）；匿名下載：Linux 完整下載與校驗通過，暫存清理完成。媒體及本次公開 `#stable` 測試來源固定為 `d0089a80e358cbd776a89327f0e58bfba68c671b`，Home coding 來源為 `0886a5459984e916e8dc6a881a1bd6d4249934fd`；八階段、兩個公開 App 階段及清理結果見 [companion 摘要](../evidence/laptop-d0089a8/summary.json)。CI、傳輸與匿名下載各自留證據，見 [驗證紀錄](VERIFICATION.md)。發布後 main 的文件／證據更新不重建媒體、不重指既有 tag 或本次 stable 身分；source ZIP 內的建置時文件保持原樣。0.9.0 媒體與收據亦保持不變。

`#stable` 是單一移動頻道。推進後，新商店版本可能高於舊 ISO 的 Controller，首次 App 安裝會以 `*_release_ahead` 停止；不得移除版本檢查來掩蓋差異。新使用者取得的媒體、公開 App 版本和安裝說明必須配套。這也不授權自動重裝既有家庭；完整 Host 更新／遷移仍須另外交付。

維護者追上游、建立候選、驗證接點後發布自家版本。`scripts/check_upstream.py` 可測鎖定或最新 Hermes；它不批准部署。HA 變更需真 Core／App、Owner、事件、批准、備份與讀回測試；Linux／HAOS 媒體另需相應一次性 VM，真 Owner 帳號及實體 laptop 另驗。

保留每份產物的來源、lock diff、SBOM／依賴、實際版本、契約、資料格式與恢復證據；Preview 與 Stable 應指向同一受測產物。簽章與綠色 CI 不是家庭寫入或部署授權。正式產品更新仍需精確批准，或符合事先明確設定的政策。

Controller 的 SQL `user_version` 仍為 3，但相同 schema 不代表舊 binary 理解側欄授權與停止語意。資料庫只要有任何 `ingress_bridge` 歷史（包含已 revoked），固定 candidate probe 就要求 `BRIDGE_CONTRACT=1`；不能把 0.6.1 當成可直接切回的同 schema 候選。Supervisor 安裝／設定／啟動等尚有未完成或效果未確認的操作時，也必須先核對，不能切換 Controller 來遺失持久管理狀態。這是能力及 quiescence 檢查，不是以舊 DB 覆蓋新 Live 資料的退版捷徑。

上述保護需要新版 **Host 固定更新器**。只更新 Controller 的舊 Host 不具備它；側欄必須先通過固定 Unix broker 的能力握手，不能信任 DB marker 或僅比較版本字串。首次連接及運作中的證明都受期限與身份範圍限制，失聯時停用受影響的側欄並停止其工作。現有 Controller-only 發布工具不更新 Host，0.6.x Host 的受控就地升級仍需另交付；不得以新 Controller 能啟動作為已完成遷移的證據。

Ted 已於 **2026-10-19** 核准正式官方更新 Ed25519 公鑰及其 DER SHA-256 fingerprint `18dd73f8b96d115d076be6b362cd62ed011c4be2b6dfdaf808e9b3e5a6a53d39`；原始碼的 `host/official-update-channel.json` 已釘選兩者與 `ghcr.io/ted-h-com/agentic-smart-home-controller`，啟用官方更新來源。私鑰由發行端保存在本 repo 之外，公鑰詳見 [Updates](UPDATES.md#官方更新來源與-owner-批准)。**尚未發布以此正式金鑰簽署的 Controller image、Host bundle、bootstrap 套件或產品媒體**；既有媒體的完整性雜湊也不因此成為身份簽章。可信發行索引、實際遷移交付與完整 Linux 恢復仍待交付。既有 0.11.0 家庭主機須先透過經獨立身份驗證的圖形 bootstrap 配合 **AH-UPDATE** USB，由 Owner 明確批准一次信任與遷移，不重跑 installer；程式升級本身不建立信任。新安裝從經獨立身份驗證且帶有效官方 pin 的媒體安裝啟用中的不可變 Host 更新執行器，有效且明確設定 `pin: null`、`image_repository: null` 的工程 fixture 則延後安裝啟用中的執行器。[Host bundle 更新](HOST_UPDATES.md) 的權限、還原點與回復範圍另有固定契約，不能把現有 Controller-only manifest 重新解讀為能更新 HAOS／Core／Linux。安全公告的追蹤與緊急發版仍需持續建立實際證據，不宣稱已有值班或 SLA。

各版 App、橋接身份、UI 與固定維護流程的工程結果由 [驗證紀錄](VERIFICATION.md) 記錄；工程測試通過不等於真家庭驗收。

## 接入追蹤資料相容性（D3b）

`discovery_setup` 是 schema 3 下的私有 v1 metadata record，只保存有界候選、建立時同類整合 ID、Owner 選取及時間／revision。HMAC 綁定 Owner 與家庭連線，原始 token 不存入 record；沒有可重播的 HA 操作、家庭授權或模型工具。到期以建立時間和目前／待套用 context 保存期限的較短者計算，讀取即過濾，prune 實際刪除。忘記／到期後，舊去重收據不能重建追蹤。

舊 binary 忽略此未知 kind，不會執行任何接入副作用，但它的 prune 不認得這個 kind；metadata 可能保留到再次使用本實作清理。因此 SQL 格式不變只表示這項 effect-free metadata 可被忽略，不保證跨版本保存政策完全相同，也不取代既有 bridge／updater 的能力與靜止檢查。既有 0.9.0 ISO 不包含本增量。


## 網路盤點資料相容性

`network_inventory` 為 schema 3 的私有 v1 record，保存投影後的來源與觀測，不保存匯入原文或路由器密碼。ID 是 Owner／家庭來源／授權的 HMAC；原始 token 不入紀錄。內容按每來源的收集時間套用現行／待套用 context 保存期限，讀取立即過濾，prune 清除內容。24 小時 freshness 和內容保存期限分開；過期觀測不再產生當前接入建議。清除保留不含設備內容的 revision tombstone，避免舊操作重放或重設 revision。

舊 binary 忽略此 kind，不會依觀測執行設備動作，但不認得它的清理政策；回退時資料可能留存到再次使用此版清理。`discovery_setup` v1 的候選來源新增 `network_inventory`，舊 binary 的嚴格 parser 會略過這些追蹤，HA 原有來源仍可讀。既有 HA create 的去重指紋保留舊格式；新網路候選另外包含明確來源，不能跨命名空間選取。這是 metadata 相容性決策，並非系統退版或硬體驗收。

主機收集器需要同步交付新的 Host broker；只更新 Controller 不會使舊 Host 憑空具備掃描功能。舊 Host 回覆不支援時，介面保留路由器匯入與 HA 探索，說明主機網路暫不可用。完整打包會包含 Host、Controller 和側欄同版程式；既有 0.9.0 安裝媒體維持其原有驗證來源。
