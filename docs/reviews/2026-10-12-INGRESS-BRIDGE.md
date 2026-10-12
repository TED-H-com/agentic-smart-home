# HA 智能管家側欄與恢復審查 · 0.7.0

日常使用原生 Home Assistant；智能管家以獨立薄 App 放進官方 Ingress，連回原有外層 Controller／Hermes。沒有修改 HA frontend、Supervisor 或 upstream DOM，沒有新增第二份對話資料庫。此 App 是對話入口，不是 Home Coding Worker；原生 Assist／語音也需要另一個 Conversation adapter。

## 連線與權限

```text
HA Owner → 原生側欄 → Supervisor Ingress → 智能管家 App
                                              │ 專用 bearer＋TLS 指紋
                                              ▼
                                   外層 Controller :8444
                                      fresh HA Owner
                                              │
                                 同一 Gateway／case／Hermes
```

App 只接受官方 Ingress peer 與 user ID，不映射公開端口，不持有 Supervisor／Core 管理 bearer、Docker socket 或主機掛載。它以非 root 身份執行，只能把固定 JSON 契約送到管理網路上的固定 TLS 端點。外層再核對配對世代、當前 HA Owner、路徑 allowlist、CSRF 與 case 範圍；管理設定與任意 URL proxy 不在這個入口。

服務 heartbeat 不含 Owner、對話或批准能力。前端初次載入仍要實際驗證 Owner；斷線、未配對與非 Owner 有各自狀態。背景研究由短效身份租約持續核對，關閉頁面不等於撤權；撤銷配對則會封鎖舊世代並要求停止其工作，不宣稱已送出的家庭動作可以被倒轉。

外層和側欄重用同一個 case。跨入口返回時，同一對話接到最新訊息；使用者明確選擇的其他對話仍保留。回應遺失後重送原請求識別，不建立第二份工作。成果只顯示經雜湊核對的文字與固定 PNG，不在 HA origin 執行候選 HTML。

## 首次安裝與有限維護

新 onboarding 在開始頁告知側欄安裝，完成有效 Owner 連接後，由外層固定流程加入本產品來源、安裝 App、寫入配對設定並啟動。舊 setup 記錄不會被補成已授權。停止自動維護、撤銷連接與明確重新連接是分開的操作。

Supervisor 的背景 job 完成後可能已自動刪除。第一、二輪真 HAOS 測試揭露原本要求 job 持續可查會卡住，即使 App 已成功安裝。修正保留原安裝意圖，以來源、版本、完整權限及初始設定讀回核對；單獨的 missing job 不代表成功，也不觸發第二次 install。若仍有明確 running／error 的 job，不能繞過。

維護只針對本產品 App；同版本重啟最多每 24 小時兩次。讀取失敗不代表 App 被移除；手動停止、移除或改設定都不被自動復原。程序 health、傳輸 heartbeat 與 Owner 驗證是不同證據。無法區分手動停止和 crash 時維持保守處理，不宣稱任何 HA 更新後都能自動修復。

撤銷後的重新連接需 Owner 明確操作，核對原本受管 App 後換發配對，再等新世代 heartbeat。首次配對若中斷在私有檔案寫入期間，可修復尚未使用的身份；已有操作、已連線／撤銷的身份或不安全檔案不走這個初始化修復入口。

App 跨版本 migration、重建及重新部署尚未交付。固定 release branch 或版本改變時會要求核對，不自動採用另一個來源；這是下一階段需另驗的更新流程。

## 外層更新與恢復

首次 Hermes 設定改用安裝包 `images.lock.json` 的映像 digest，執行離線 adapter probe 後啟用，不在家庭主機追 HEAD 或重新 build。維護者的 `scripts/check_upstream.py` 仍可建立最新版候選；已有帳號／session 的安裝不藉 bootstrap 更換。

Controller 仍使用 schema 3，但有任何側欄歷史的資料庫要求候選支援 bridge contract 1。實際 0.6.1 映像因缺少契約而被固定 probe 拒絕，新版候選可通過；見 [映像測試](../../evidence/bridge-legacy-candidate.json)。尚未確認的 Supervisor 操作也會阻止 Controller 切版。退版保留當前 DB，不恢復舊快照蓋掉新家庭操作。

## 證據範圍

- [薄 App 測試](../../evidence/bridge-app.xml)：固定目的地、Ingress 身份、跨來源請求、部署狀態與斷線。
- [Controller／恢復測試](../../evidence/bridge-controller-repair.xml)、[維護測試](../../evidence/bridge-maintenance.xml)：身份、世代、精確派工、恢復及 Supervisor 協定 fixtures。
- [UI 測試](../../evidence/bridge-ui.xml)與 [截圖](../../evidence/ui/bridge/README.md)：真 Chromium、協定 fixture；不代替真 HAOS。
- 真 HAOS、完整容器回歸、公開來源安裝與媒體的最終結果統一記在 [驗證紀錄](../VERIFICATION.md)。失敗紀錄保留，不因後續通過而刪除。

真雲端 OAuth／收發、Owner laptop、Home Worker coding、跨版本 App migration、實體設備與完整 E1／E2 家庭驗收仍各自需要證據。已授權的工程自動測試不替使用者批准正式家庭變更。
