# 配對與帳號

保持保護模式。未配對時，記錄中的 JSON 包含 App hostname、port、SHA-256 certificate pin 及隨機一次性 code。只把這份資料輸入自己的 Controller；過期後重啟 Runner 取得新的 code。

Controller 提供 Codex 官方 device code／API key、Claude Code API key／官方 setup-token、Antigravity Gemini API key、Grok 官方 device code／xAI API key。內外兩層需要各自登入，Antigravity 的 Google 訂閱登入尚未整合。MFA 在自己的瀏覽器完成。

一般 Runner backup 排除登入 profiles、任務工作目錄與 TLS private key。還原後需要重新配對與登入；已部署的 HA 規則繼續由 HA 執行。
