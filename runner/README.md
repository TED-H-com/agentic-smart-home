# Agentic Smart Home Runner

隔離執行 Codex 0.157.1、Claude Code 2.1.296、Antigravity 1.3.3 與 Grok Build 1.0.50。第一次啟動的 App 記錄會列出 10 分鐘有效的配對 JSON，在外層 Web 的「HAOS 內層 Agent」完成一次性配對。

Runner 不持有 HA 家庭管理 bearer。四種 CLI 分別使用非 root UID 與私有 profile；內外層也各自登入。登入、任務及取消請從 Controller 操作。需要 amd64。

TLS 8443 映射至 HAOS，讓外層 Controller 連線；透過憑證 pin 與私有 bearer 驗證。主產品的 HAOS VM 位於專用 NAT 子網，不需將 8443 轉發至公網。原 Ingress 模式仍可在 HA 內部配對。

Antigravity 目前支援 Gemini API key，Google 訂閱登入尚未整合。Grok 支援官方 device code 或 xAI API key。雲端帳號仍需使用者登入，測試 fixture 不代表實際雲端驗證。
