# 配對與帳號

保持保護模式。主機會自動完成配對；自動配對沒有成功時，App 記錄會列出 10 分鐘有效的一次性配對資料（App hostname、port、憑證 SHA-256 與隨機 code），可以在主機的 AI 帳號頁貼上。只把它交給自己的主機；過期後重啟 App 取得新的 code。

開發助手另外登入 Codex（裝置碼或 API 金鑰）。受控 CLI 提供 Codex 裝置碼／API 金鑰、Claude Code API 金鑰／setup-token、Antigravity Gemini API 金鑰、Grok 裝置碼／xAI API 金鑰。主機和家庭現場各自登入；MFA 在自己的瀏覽器完成。

一般 Runner 備份不含登入資料、工作目錄與 TLS 私鑰。還原後需要重新配對與登入；已部署的 HA 規則繼續由 HA 執行。解除安裝會刪除 App 的 `/data`，包括開發助手的登入。
