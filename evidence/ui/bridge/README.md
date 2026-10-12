# 智能管家側欄畫面 · 0.7.0

本目錄是 Chromium 搭配合成 API／家庭資料的 UI 證據，由 [瀏覽器測試](../../../tests/test_bridge_ui.py) 產生；不代表真 HAOS、雲端帳號或家庭驗收。

- `before-host-*`／`before-ingress-*`：整合前的 1440／390 px 畫面。
- `conversation-*`、`results-*`、`approval-*`：同一對話、成果與精確家庭批准，1440／768／390 px。
- `offline-*`、`unavailable-*`、`owner_required-*`、`revoked-*`、`unpaired-*`：連線與身份邊界，同樣三種尺寸。

另有 [真 HAOS 原生側欄全頁截圖](../bridge-native-haos-local.png) 與 [實測記錄](../../bridge-haos-local.json)。該次使用一次性官方 HAOS、真 Owner 瀏覽器登入、Supervisor Ingress、實際薄 App／外層 Controller；没有攔截瀏覽器 API 或注入身份 header。它核對已存在的 case，送出訊息、刷新並接續同一對話。模型回覆為明示 transport fixture，App 來源為本機交付，不能當成公開 release branch、真雲端或實體家庭驗收。

公開來源另已通過 [真 HAOS 安裝](../../bridge-haos-public.json)，見 [GitHub source delivery 的原生側欄截圖](../bridge-native-haos-public.png)。它同樣使用明示推理 fixture，不代表真雲端或家庭驗收。
