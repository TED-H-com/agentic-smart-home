# 使用與維護

先完成外層 Agentic Smart Home 的家庭 Owner 連接，再由外層啟用智能管家側欄。
App 設定由受信任的固定流程寫入；不要將其中的 token 或設定內容貼到對話、截圖或公開 issue。

介面只允許 Supervisor Ingress 流量，沒有公開映射端口、Supervisor／Core API token、Docker socket 或主機目錄掛載。
App 以固定內部位址及 TLS 憑證指紋連接外層專用端點，外層再次驗證有效 Owner。
登入外層維護頁的權限不會經由此 App 傳遞。

HA 更新後可重新連線。若啟用自動維護，外層只在限定次數內處理我們自己的 App；使用者停用、移除或改設定時不自動覆蓋。
本 App 的 health endpoint 只代表程序存活，不能代替完整身份／對話驗證。
