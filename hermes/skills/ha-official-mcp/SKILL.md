# 使用官方 Home Assistant MCP

連線與權限說明在 `/ha-mcp/README.md`。

管家應使用 Home Assistant 內建的 [Model Context Protocol 整合](https://www.home-assistant.io/integrations/mcp_server/)，而不是社群第三方 MCP 套件當預設。

- 建議先用 Device control 的個別 URL，或把主 URL 設成唯讀，在測試機驗證。
- 官方整合走 OAuth 2.0 + PKCE。不要把長期權杖寫進這個 repo。
- 工具集合與暴露的實體由主人在 HA 裡設定；你只能用被打開的那些。
