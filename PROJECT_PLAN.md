# Agentic Home Linux 產品計畫 — 0.3.1

Owner：Ted Huang。2026-10-11 的外層 Linux 架構修正優先於先前「只交付 HAOS Apps」方案。

產品從外層 Linux 安裝開始；Agent、管理介面與主機服務在 Linux 上，產品建立並管理內層官方 HAOS。HAOS 保留 Supervisor、Core、integrations、日常自動化。

## 交付範圍

- 可開機 Linux ISO、完整產品 TAR、來源 ZIP。
- 官方映像 lock、多主機 profile、可恢復 firstboot 安裝。
- 外層 Controller／Runner、主機登入、HA owner 連接、provider 官方登入。
- KVM provisioning、VM ownership、正常啟停、冷快照／還原、操作紀錄。
- HAOS 停機時仍可用的主機介面與 Agent 診斷。
- 既有家庭設備盤點、提案、批准、備份、原生自動化及真實觸發流程。

首條部署路徑：amd64 Ubuntu 24.04.5 + KVM + HAOS 18.3，有桌面／無桌面兩種 profile。Ubuntu 26.04.1 有鎖定 recipe，驗證狀態另列。ARM／其他 Linux 要實作對應 installer、firmware、HAOS image 及 provider binary adapter。

不安排開發人工 gate。自動測試由開發 Agent 執行；使用者登入、硬體及長時間運作如實記錄，不拿 fixture 代替。既有 credential 分離、家庭寫入政策、分層备份與 owner 身份要求继续有效。

## 目前開發順序

- 已完成：外層 Web onboarding、四種 CLI 的雙層登入、LINE／Discord／Telegram 統一入口與提案批准流程。
- 0.3.1：可接續的對話、明確的新對話、隔離與限量前文、快速追問排序，以及瀏覽器重試去重。驗證結果記於 `docs/VERIFICATION.md`。
- 下一階段：通道連線與送達問題的診斷、容易取得聊天室／使用者 ID 的設定流程。
- 後續：已安装系統的版本升級／回復流程、更多 Linux profile，以及實機與長時間運作證據。

真實雲端帳號與 LINE／Discord／Telegram 收發需在已授權帳號上實測；協定 fixture 與本機容器證據不代表平台登入成功。
