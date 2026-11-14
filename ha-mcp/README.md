# Home Assistant 官方 MCP

本主線只使用 Home Assistant 官方整合：[Model Context Protocol](https://www.home-assistant.io/integrations/mcp_server/)（HA 當 MCP **伺服器**，讓管家當客戶端）。那一頁會更新得比這份說明快，有出入以官網為準。

不要把權杖、OAuth 碼或完整連線 URL 裡的密鑰部分 commit 進來。樣板裡只放佔位符。

## 在 HA 裡開啟

1. 先在**測試用 HAOS** 做（見 `deploy/haos-test/`），不要先動正式 VM 100
2. **設定 → 系統 → AI**。若 Model Context Protocol 卡片有「啟用」，先啟用
3. 複製 URL。建議先用 **Device control** 的個別 URL（`/api/mcp/assist`），它只看得到已在「暴露的實體」裡交給 Assist 的裝置
4. **主 URL**（`/api/mcp`）預設會暴露全部工具，而且需要系統管理員帳號。完整「Home Assistant」工具組可以讀所有實體、紀錄，並執行任何動作
5. 可在整合的設定裡改：是否暴露全部工具、是否要求管理員、是否 **唯讀**

## 驗證方式

官方整合實作標準 MCP，含 OAuth 2.0 與 PKCE。雲端客戶端需要 HA 能從外網連到；管家 VM 在同一區網時，可用內網 URL（測試機的 IP，或之後才用正式的 `http://192.168.103.103/api/mcp/...`）。

首次連線通常要在瀏覽器登入 Home Assistant（主人本人操作）。本 repo 不存放這次授權。

若某個客戶端改走長期權杖，請用安全表單或客體上權限 0600 的檔案保存，路徑不要進 git。

## 權限建議

| 階段 | 建議 |
|---|---|
| 測試機第一次接通 | `/api/mcp/assist`，或主 URL 打開唯讀 |
| 測試機要練習寫入 | Device control，只暴露必要實體 |
| 正式家庭 | 同樣由小到大；沒有需要就不要開完整「Home Assistant」工具組 |

管家的 Hermes 設定樣板見 `hermes/config.template.yaml`。把真正的 URL 寫在客體上。
