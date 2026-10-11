# Agentic Home Linux 產品計畫 — 0.2

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
