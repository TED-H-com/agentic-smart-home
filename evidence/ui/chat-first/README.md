# 智能管家：對話優先版

使用者要求移除不易理解的「開發工作」主入口，改成直接對話、減少說明。
這24張圖片來自編譯後產品的 Chromium 瀏覽器，使用合成 API fixtures；不是 mockup，也不是真雲端／家庭驗收。

主要改動：五項主選單、同一段對話呈現進度與成果、設定與初次登入減字；模型、手動派工、工作識別與 checkpoint 收在進階區。未修改原廠 HA 或 OAuth 頁面。

| 畫面 | 桌面 | 平板 | 手機 |
|---|---|---|---|
| 智能管家：直接開始對話 | [1440](assistant-welcome-1440.png) | [768](assistant-welcome-768.png) | [390](assistant-welcome-390.png) |
| 對話中的處理進度 | [1440](conversation-work-1440.png) | [768](conversation-work-768.png) | [390](conversation-work-390.png) |
| 在同一段對話展開成果 | [1440](assistant-results-1440.png) | [768](assistant-results-768.png) | [390](assistant-results-390.png) |
| 簡化設定入口 | [1440](settings-host-1440.png) | [768](settings-host-768.png) | [390](settings-host-390.png) |
| 初次設定：選擇助手 | [1440](bootstrap-choose-1440.png) | [768](bootstrap-choose-768.png) | [390](bootstrap-choose-390.png) |
| 官方登入 | [1440](bootstrap-cli-auth-1440.png) | [768](bootstrap-cli-auth-768.png) | [390](bootstrap-cli-auth-390.png) |
| 連接 Home Assistant | [1440](bootstrap-native-home-1440.png) | [768](bootstrap-native-home-768.png) | [390](bootstrap-native-home-390.png) |
| 設定完成 | [1440](bootstrap-complete-1440.png) | [768](bootstrap-complete-768.png) | [390](bootstrap-complete-390.png) |

已實際檢視三種寬度，檢查溢出、主要操作可見性、長篇成果與輸入框位置。
原先工程導向版本保留在 [0.6.0](../0.6.0/README.md) 供比較。檔案與編譯資產 SHA 記在 [screenshots.json](screenshots.json)。
