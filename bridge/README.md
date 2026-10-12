# 智能管家

在 Home Assistant 側欄開啟同一個外層 Hermes 對話，查看工作成果與家庭提案。
透過官方 Ingress 登入，只有已綁定且仍有效的家庭 Owner 可以操作。

本 App 是介面及連線橋接；不啟動另一個 Controller、Hermes 或 coding CLI，也不保存對話資料庫。
外層的「啟用側欄並自動維護」負責安裝、配對與有限度修復。配對資料不顯示於聊天或瀏覽器。
外層暫時離線時，對話仍保存在原處；恢復後可接續。

HA／Supervisor 沿用官方更新機制。原生 Assist／語音與真正 Home coding worker 另行整合。
