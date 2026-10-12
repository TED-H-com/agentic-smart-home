# 智能管家 App 跨版本更新／退版：下一增量設計

**設計審查，未實作、未執行新增測試。** 基準為 `44e18299ea5969a154e2fe3d6e4c40f6a6a01f80` 的 0.7.0。本文不改凍結產品、原始 v3 或現有驗收狀態；0.7.0 的真 App 安裝／側欄測試不能算跨版本驗收。

建議第一個增量只處理**同一產品、同一 App options／bridge protocol 的相鄰版本**。沿用 Controller 的 Supervisor adapter 與維護 journal；外層 Controller、Hermes、HAOS／Core／Supervisor 不在同一工作中升降版。Owner 從外層發起，側欄暫停期間仍可用原生 HA 和外層救援。

## 先解決 App 身份，不能只加 update 按鈕

目前 [BridgeRelease](../../controller/agentic_controller/bridge_maintenance.py) 固定 repo source `https://github.com/teddashh/agentic-smart-home#release/0.7.0`。官方 Supervisor 2026.09.3 對完整 source 字串取小寫 SHA-1 前八碼作 repository ID，再組成 App slug；`#ref` 也在字串內。因此換成下一個 release ref 會變成另一個 App，不是原 slug 的原地更新。[官方 repository 建立程式](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/store/repository.py)、[hash 函式](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/store/utils.py)。

官方 `/store/addons/{slug}/update` 只接受 `backup`／`background`，不接受指定 version、commit 或 image digest。產品必須在呼叫前後核對 catalog／installed version；這仍是信任 repo/ref 的 source build，不能稱原子 digest 安裝。[官方 API 實作](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/api/store.py)。

| 變更 | 本增量處理 |
|---|---|
| 同 source/ref、同 slug，catalog 提供新版本 | 原地 update；先取得只含該 App 的備份。這不是目前逐版本 release ref 的主要路徑 |
| 同 Git repo／app 名稱，指定 release ref 改變 | **主要路徑：新 slug 安裝、保留舊 App、單一配對世代切換** |
| repo 主體、app 名稱、權限或 options schema 改變 | 拒絕自動遷移；另做明確遷移設計，不憑相同顯示名稱認領 |
| 使用者已停止／移除／改設定，或目標 slug 已被其他流程安裝 | 保留人工選擇，進 `needs_reconcile`；不重裝、不接管 |

讀回 Supervisor 實際 repository ID／slug；hash 算式只解釋身份變化，不作瀏覽器提供任意 slug 的理由。保留舊 repo/ref 和 App 到復原期限結束，不能刪 repo 影響其中其他 Apps。

## 可重用的接點與真正缺口

| 現有實作 | 直接沿用 | 必須另補 |
|---|---|---|
| `BridgeMaintenance.owner/guard/effect_gate` | fresh HA Owner、token／generation 變更前後檢查、Pause／Controller updater gate | 執行中 release 需綁工作內的 source／target；不能覆寫現有 `release` 來繞過 `bridge_release_changed` |
| `effect/job/waiting`、`operation` | 先存意圖再 POST、job ID／deadline、未知結果只核對 | 每個 effect 綁自己的 App slug 與後置條件；更新／stop／備份／還原不能套用 install 的預設選項判斷 |
| `validate_app/configured/settings_digest` | 固定 repo 身份、版本、安全旗標、protected／options／boot／panel 讀回 | 顯式傳 source 或 target release；切換中同時核對兩個 App，拒絕 catalog 漂移 |
| `IngressBridge.prepare_pairing/current/authorize/revoke` | 私有憑證、TLS pin、單一有效世代、fresh Owner／CSRF | 可中斷恢復的切換 journal；新舊 App 不共用有效 bearer，不能把 `repair=True` 当作無條件遷移 |
| `host/.../updater.py` 的 pending App operation 檢查 | App 操作未核對前禁止 Controller 切版 | 覆蓋整段 transition，包括兩個 POST 之間；持久更新語意另設 compatibility marker，不能只靠既有 `BRIDGE_CONTRACT=1` |
| `ha.request(..., supervisor=True)` | 固定官方端點與 HA Owner 路徑 | App-only backup/restore adapter；现有 `ha.backup()` 備份的是 Core，**不可直接用作 App 退版** |
| `test_bridge_haos.py`／`bridge_native_browser.py` | 真 Supervisor 安裝、Owner 原生登入、實際 Ingress、同 case 續接 | 兩版本切換／故障注入／退版後的同一組讀回；保留模型 fixture 與真雲端的界線 |

## Owner 授權與最小持久資料

新增單一 `bridge_release_transition` 工作。目標 descriptor 第一版由受測外層產品提供，僅含固定 repo/ref、version、source commit、App／options／bridge contract 與對應測試證據。瀏覽器只選目標 ID，不傳 repo URL、shell、Docker 參數或權限。現有 Controller-only 簽名 manifest 不自動授權 App 更新；完整可信發行索引仍是獨立缺口。

外層先產生 review：source／target 身份、預期暫停、需改的 boot／panel、備份或保留舊 App 的復原方式，以及 App 退版不還原家庭資料。Owner 的一次批准綁 review digest、當前 owner／authorization epoch、maintenance revision、兩端 contract、設定 hash 與期限；沿用 host-only session／CSRF／Idempotency-Key。自動維護開關和最初 onboarding 安裝同意**不包含跨版本升級**；Hermes／MCP／App 本身沒有批准接點。

最少保存：`id`、`contract`、`direction`、`state/stage`、source／target descriptor、Owner scope、review digest／消耗紀錄、原設定 hash、App backup ID 或保留實例、目前 effect 的 slug／job／deadline／後置條件、切換前後 pairing IDs、failure code。明文 bearer 仍只在原私有 credential storage；backup、API options、raw log 不進一般證據。

批准只消耗一次；重啟續查同一工作不再消耗或重新送 POST。每個後續 effect 仍需 fresh Owner、未撤銷的批准 scope、固定來源及設定讀回。換 Owner、取消維護、撤銷或來源漂移後停止後續效果，不能借自動退版復活權限。GET 保持純讀。

## 最少狀態機

| 狀態／stage | 允許動作與離開條件 |
|---|---|
| `offered` | 只讀出精確差異；過期或基準改變即失效 |
| `preparing` | 批准已保存；驗來源／空間／Supervisor、建立復原材料。跨 ref 安裝新 App，但保持 stopped／無配對／無側欄；原地路徑完成 App-only backup |
| `quiescing` | 阻止舊側欄新 mutations；舊 bridge authority 的活動推論／coding／家庭效果須已停止或完成。未知效果先核對，不默默 cancel。跨 ref 固定 stop 舊 App，讀回 stopped，再設 boot manual／隱藏舊 panel |
| `switching` | 保存切換意圖，再撤銷舊世代並建立新世代。跨 ref 設定／啟動新 slug；原地則執行 update、核對新版本後設定新世代。每一步獨立 journal／讀回 |
| `verifying` | 精確 App 版本／來源／安全及設定、切換後的新 heartbeat、fresh Owner 與 bridge contract 都通過；heartbeat 單獨不能完成 |
| `completed` | 將 target 發布為唯一 managed App、解除側欄 transition fence。保留有期限的復原材料與歷史；Owner 開原生側欄仍走正常登入 |
| `rollback_offered` → 同一套 preparing／quiescing／switching／verifying | 另一次綁精確 source/target 的 Owner 批准；方向相反，不把失敗一概當成可以退版 |
| `needs_reconcile` | 保存原 effect／世代／未知結果與 fence；只讀核對原工作。不能因 timeout、job 404 或按重試就重送 |
| `cancelled` | 只在尚無外部 effect 時可直接結束；已有 effect 先核對，不宣稱已取消 Supervisor 工作 |

`operation` 或等價既有 updater 可見 sentinel 必須涵蓋**整個非終態 transition**，不能在中間清空讓舊 Controller updater 切版。需新增例如 `BRIDGE_UPDATE_CONTRACT=1` 的固定候選 probe／持久 required marker，包含歷史工作；僅 SQLite schema 3 與 bridge transport contract 1 不證明舊 binary 了解兩實例切換。**這個 marker 必須由已安裝的 Host updater 執行，不能只由新 Controller 寫入。**

### 啟用前置：先交付能執行門檻的 Host

已核對 0.7.0 [Host updater](../../host/agentic_host/updater.py)：`offline_check()` 只因 `bridge_maintenance.operation` 非空而拒絕切版；`probe()` 只把 `ingress_bridge` 的存在轉為 `required_ingress_bridge_contract=1`。隨 Host 安裝、唯讀掛入候選的 [fixed probe](../../host/agentic_host/update_probe.py) 也只檢查 `BRIDGE_CONTRACT`。目前 `/updates` 的 `contract:1`／`scope:controller_only_same_schema` 不是 App transition 能力聲明。

因此新 Controller 即使寫入 `required_bridge_update_contract`，舊 Host 也不會複製或執行它；工作完成、operation 清空後，仍可能接受不理解 transition 歷史的舊 Controller。暫留 operation 可重用為活動工作 fence，但不能冒充完成後的永久相容性保護。

最小交付順序必須是：

1. **先提供新版 Host bundle／明確的 Host 元件交付。** 同時更新 root-owned updater 與 fixed probe，使它們讀回非終態工作及歷史 required marker，並在部署、Owner 退版與健康失敗自動退回三條路徑執行同一門檻。未知 required contract 拒絕候選。現有 publisher／manifest 只切換 Controller image，不能更新這些 Host 檔案；在另有受控 Host 更新方式前，本功能明確要求新版 bundle 的受測安裝，不宣稱舊 0.7.0 可只升 Controller 就開啟。
2. **再從固定 Unix broker 取得實際 Host capability。** 例如新增 `app_release_transition_contract:1`，並綁受測 updater/probe 的 build 識別；這是待實作契約，不是目前 `/updates` 已有欄位。來源必須是正在執行的 Host 服務，不能用 Controller 自報版本、媒體版本或 DB marker 代替。讀取失敗、舊 Host 缺欄位或契約不符時，App 更新保持不可啟用；尚不得接受批准或建立新語意工作。跨 await／重新啟動後，在後續 effect 前重驗此門檻。
3. **用精確安裝證據證明它真的拒絕舊候選，再啟用功能。** 在一次性安裝機，以實際 Host binary＋固定 probe 測「相同 schema 3、`BRIDGE_CONTRACT=1`、缺 App update contract」的舊 Controller。非終態工作、已完成歷史及已撤銷 pairing 三種資料都必須拒絕；支援契約的候選才可通過。同測手動退版與失敗回復，不以 mocked capability JSON 或常數存在作為通過。記錄實際 Host／probe hash 與拒絕原因。

這是有限 Host 相容門檻的先行交付，不要求重寫 Linux updater。舊 Host 保留既有首次安裝／同版本維護；新 Controller 可先提供其原有相容功能，但在握手和已驗執行門檻缺失時不能寫入新的 App transition 語意。人工替换 Host 檔案或降版 bundle 也不能被宣稱為受支援的退版路徑。

Supervisor 沒有 options compare-and-swap；現有「await 後再檢查」保留，但最後一瞬間原生手動操作仍可能競爭。讀回不符即停止並記錄，不以覆寫讓狀態看似一致。

## 配對、連續性與退版

跨 ref 不複製整個 App `/data`，薄 App 的對話／case／proposal 都在外層。切換前確認舊實例停止，新 App 才取得新的 pairing ID／token／pin；同一時刻只接受一個世代。記錄意圖後若憑證寫入或 Controller 中斷，核對私有檔與 ledger；不因重試再生成下一組。這段原子性目前只有配對失敗拒絕，尚不是完整更新恢復機制。

新世代令舊 CSRF／舊 iframe／舊 service bearer 失效。case／thread 與已完成成果保留，由 server resume 找回；不把舊 authority marker 改綁新世代。未完成的家庭批准保留內容及歷史，但需 fresh Owner 重新核對並產生當前 authority 的提案修訂，不能沿用旧批准來執行。使用者必須事先知道切換可能中斷未送出草稿；不能藉把 prompt 寫入 browser storage 假裝保存。

跨 ref 退版先證明新 App 已停止，之後才用**另一個新世代**重新設定／啟動保留的舊 App；不復活備份裡的舊 token。原地退版只恢復該 App 的 partial backup，再輪換配對；不得還原 Core、Controller DB 或已發生的家庭操作。官方 partial backup/restore 可限定 `addons:[slug]`、`homeassistant:false`、`folders:[]`；需要讀回 backup 內容和實际還原結果，不能只信 HTTP receipt。[Supervisor 備份 API](https://developers.home-assistant.io/docs/api/supervisor/endpoints/#backup)。

若新版本變更 App 資料格式、舊 App 不支援目前 Controller 或 HA 已升到舊 App 不支援的介面，退版先拒絕。首次增量不做通用資料 down-migration、source rebuild 或 HA 降版。復原材料含配對秘密，需私有權限／明確期限；active／needs_reconcile 工作保留所需材料。舊 App／repo 刪除是後續明確清理，不在更新成功後立即 uninstall。

## 必須補的相鄰版本證據

下列全部 **not_run**。現有 public App 證據是 Core `2026.10.1`／Supervisor `2026.09.3`；測試時記錄實際 HAOS／Core／Supervisor／source ref／commit／App version，不能只寫「latest」。先以固定基準驗證 A→B→A，再使用維護端取得的相鄰受測 Core B；若當下没有下一版可測，記 unavailable，不能以改版本字串代替。

| 案例 | 最小實測判據 |
|---|---|
| U1 公開 ref A→B→A | 真 Supervisor 判定不同 slug；舊實例停止／新世代接管；真原生側欄送訊、刷新、同 case 續接；退版後保留更新後新增的外層訊息 |
| U2 同 source 原地更新／App-only 還原 | 真 backup 內容只含該 App；還原後 Core／Controller 新資料不倒退，舊 token 拒絕；未驗此路徑則不對外提供 |
| U3 各 POST 回覆遺失／Controller 重啟 | 安裝、stop、options、update、restore、start 分別中斷；原 job 已清除也只按精確後置條件核對；不重送不明效果 |
| U4 配對切換中斷 | 在私有檔／ledger／App options 三邊落盤邊界注入中斷；舊與新 token 不同時有效，成功退版也不復活任何舊世代 |
| U5 人工與批准競爭 | approve 後改 Owner／設定／repo catalog／手動 stop、uninstall、disable；原工作停止且不覆寫；同 idempotency key 不開始第二份工作 |
| U6 Core 相鄰版本／重啟 | Owner 用 HA 官方流程更新 Core；App A、App B 分別測 Ingress 身份、同 case、PNG、精確批准與撤權。家庭写入另需受測版本 gate；不能由聊天通過推定 |
| U7 Supervisor／HAOS 自管更新與斷線 | 在一次性 VM 依官方路徑更新或重啟，再觀察 pending job／Ingress／options；失聯不當作 App 被移除，未知欄位可接受，缺必要語意只阻止此能力 |
| U8 復原與 Host／Controller 相容 | 舊 Host＋新 Controller 不啟用 App 更新；新版精確安裝的 Host/probe 在活動、completed 歷史及 revoked 三種狀態都拒絕缺契約的舊 Controller，涵蓋部署／手動退版／失敗回復。備份遺失、build 失敗或舊 App 不相容時保留外層救援，不自動降 HA 或擴權 |

可先用可控 transport／故障 fixture 驗 journal 和負向路徑，但 U1／U2 的安裝、停止、備份、還原與 native browser 必須在真一次性 HAOS 完成。固定模型回覆可驗身份與 case 延續；真雲端帳號、家庭設備、Home coding 與 E1／E2 仍各自驗收。

建議開發順序是：先交付並實測 Host updater／fixed probe 的新門檻與 capability 握手，再交可信 target descriptor／精確批准與持久 transition，完成現有逐 release ref 的跨 slug 路徑及 U1／U3–U5／U8；之後擴展原地 backup/update 路徑與相鄰 HA 矩陣。沒有通過的路徑維持不可選，既有 0.7.0 維護功能保持原限制。
