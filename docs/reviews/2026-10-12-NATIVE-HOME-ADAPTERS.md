# 原生家庭入口與上游 adapter 審查

完成安裝後，Home Assistant 原生介面承接日常家庭使用；外層 Web 保留智能管家、初始化與救援。此版保留既有角色、Gateway、家庭批准與 schema 3，沒有另建 HA 前端或修改上游程式。

## 實作與檢查重點

- 原生入口採 contract 1，讀回有效 Owner 和 Core frontend 能力；接受新版本字串及額外欄位。過期身份、缺少必要語意、維護與斷線會停止自動導向。
- 只有裸入口或成功桌面一次性登入才可能自動進 HA。明確提案／對話／維護連結、未送出草稿與返回點保留；已失效 session 或過期非同步回應不能觸發跳轉。
- Hermes 私有符號集中在 facade；候選驗證真實 AIAgent、官方 MCP、對話歷史、工具拒絕、401、Codex OAuth helper 和 Responses transport。這些是離線 fixture，未完成真雲端授權。
- Linux 固定動作每次檢查各自需要的 libvirt、QEMU、Docker、Compose 和路由語意；版本字串不當作能力證據。缺少單一介面時拒絕相關操作，保留讀取狀態與獨立救援。
- 候選測試分離輸入來源 hash 與實際建置 context hash；下載／archive 失敗也輸出固定錯誤碼並清理。安裝器驗證 probe 的契約內容後才啟用，exit 0 本身不足。

## 畫面檢視

[桌面／平板／手機截圖](../../evidence/ui/native-home/README.md) 使用真正 Chromium 和編譯資產，API 狀態及 HA 目的頁是 fixture。原生入口、完成頁與失敗頁改為短文和清楚動作；管家移除「今天想處理什麼」大標。即時入口狀態取代可能過時的全域 HA 徽章。主按鈕沿用既有深綠設計 token，截圖等待 150 ms 色彩 transition 完成。

[真 Core dashboard 截圖](../../evidence/ui/e2e-native-dashboard.png)及原生導向／返回由 composed E2E 另行記錄；瀏覽器 fixture 截圖不冒充實際家庭。測試與精確媒體結果集中於 [驗證紀錄](../VERIFICATION.md)。

## 保留的缺口

HA 側欄接外層 Hermes 尚未完成，需薄型 Ingress bridge、經驗證的身份對應，以及同一外層 case／proposal store；不能把另一個 Controller App 的側欄當成已接通。Home App coding、真帳號、實體 laptop／设备及 E1／E2 整案仍按原帳本驗收。

目前 updater 只支援同 schema 的 Controller image。Linux、HAOS、Core 和使用中的 Hermes 更新／退版仍需各自設計及證據。契約檢查的目的是在上游破壞相容性時提早辨識及隔離影響，不保證任何未來版本都能直接使用。詳細策略見 [ADAPTERS](../ADAPTERS.md)。
