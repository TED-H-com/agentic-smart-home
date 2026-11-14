# 舊線封存

2026-11-14 起，這個 repo 的主線改為「官方 HAOS + 旁邊的 Hermes 管家 VM」。

舊產品（USB 安裝自製主機 OS、Controller／Bridge／Runner、內嵌 HAOS VM、0.11.1 候選）完整保留在：

- 分支 `archive/agentic-host-os`（與封存當日的 `main` 相同，commit `b96fe757895080fafdc7b900fb833643ad57d591`）
- 標籤 `archive/os-appliance-v0.11.1`

不要刪這條分支或這個標籤。要查舊安裝器、合約測試或 ISO 建置流程，請 checkout 那條線，不要把它們再合併回新主線。
