# 接到家裡的正式 Home Assistant

只有測試機上的官方 MCP 流程走通以後才做這一步。

1. 在**測試 HAOS** 安裝並設定官方 [Model Context Protocol](https://www.home-assistant.io/integrations/mcp_server/) 整合，見 [`ha-mcp/README.md`](../ha-mcp/README.md)
2. 把管家 Hermes 的 MCP 設定指向測試機 URL，確認只能碰到你允許的工具／實體
3. 主人檢查行為無誤後，再在**正式 HA**（VM 100，`http://192.168.103.103`）開同樣的官方 MCP
4. 授權用官方 OAuth，或若某客戶端需要長期權杖，由主人用安全表單提供。**不要貼在聊天，不要 commit**
5. 先用較小的權限（例如只開 Device control、或主 URL 設唯讀）觀察一段時間，再考慮打開完整「Home Assistant」工具組

接上正式 HA 之前，管家不應對 VM 100 發出控制。
