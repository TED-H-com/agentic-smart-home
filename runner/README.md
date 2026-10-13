# Agentic Smart Home Runner

隔離執行 Codex 0.157.1、Claude Code 2.1.296、Antigravity 1.3.3 與 Grok Build 1.0.50。第一次啟動的 App 記錄會列出 10 分鐘有效的配對 JSON，在外層 Web 的「HAOS 內層 Agent」完成一次性配對。

Runner 不持有 HA 家庭管理 bearer。四種 CLI 分別使用非 root UID 與私有 profile；內外層也各自登入。登入、任務及取消請從 Controller 操作。需要 amd64。

TLS 8443 映射至 HAOS，讓外層 Controller 連線；透過憑證 pin 與私有 bearer 驗證。主產品的 HAOS VM 位於專用 NAT 子網，不需將 8443 轉發至公網。原 Ingress 模式仍可在 HA 內部配對。

Antigravity 目前支援 Gemini API key，Google 訂閱登入尚未整合。Grok 支援官方 device code 或 xAI API key。雲端帳號仍需使用者登入，測試 fixture 不代表實際雲端驗證。

0.8.0 另提供 Home Coding Worker 的獨立 Codex research／development，從外層「設定 → 進階工具」選家庭現場助手並另外登入。每次工作使用固定來源 commit 的私有 clone，可寫 scratch、改程式及跑測試；成果回到同一 case，部署仍需受控批准。

Home coding 使用 App 宣告的 `SYS_ADMIN`、`/dev/fuse` 與自訂 AppArmor 建立隔離環境；模型程序沒有這些管理能力、HA／Supervisor token 或直接 LAN 存取。來源與套件版本分別鎖在 `coding-source.lock.json` 和 `worker-dependencies.lock.json`；隔離不可用時不派 coding。範圍與平台驗證見 [Workers](../docs/WORKERS.md)。不正常 App 重啟後若無法證明舊 writer 停止，保留工作鎖，不能只清 marker 繼續。
