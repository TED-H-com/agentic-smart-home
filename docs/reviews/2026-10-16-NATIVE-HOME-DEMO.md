# 公開示範：家庭總覽到智能管家

Owner 要求示範包含 Home Assistant 的樣子與介面。原本只有外層管理畫面及獨立的管家側欄，無法看出日常家庭入口。本輪 `/demo/` 預設打開家庭總覽，提供房間、燈光開關與亮度、冷氣溫度、設備詳情、裝置與服務設定、自動化檢視，再從同一側欄進入智能管家。

## 介面與資料邊界

HA 部分是本專案撰寫、明示標記的互動介面模擬，並未執行官方 HA frontend；管家／管理 frame 直接載入未修改的 `web/src/main.tsx`。三個畫面共用瀏覽器記憶體中的虛構家庭模型，沒有 Controller、HA、品牌帳號或實體設備連線。示範可讓待設定橋接器加入一盞書桌燈，更新設備、整合、發現來源摘要及 digest；不自動授予 AI 讀寫權限。既有提案批准、取消、開發工作與維護示範保留。

切換頁面保留已掛載 frame，因此未送出對話與設備權限草稿得以保留。產品的原生 HA 連結在示範內轉往對應畫面；內網／localhost／授權連結不送到使用者機器。對話框與手機抽屜提供鍵盤焦點限制及 Escape 返回。舊外層 hash 深連結及 `?surface=bridge` 保持可用。

正式部署仍使用官方 HA frontend 與薄 Ingress App。本輪不修改上游 DOM、不加入正式產品 fixture，也不變更控制權、安裝版號、stable、release tag 或既有安裝媒體。32 項現場驗收狀態不變。

## 官方外觀參照

2026-10-16 檢視官方實際 demo 與 frontend `20260930.3`；參照側欄、房間 section／tile、整合設定的層次。本實作沒有複製上游執行碼、品牌圖檔或圖示套件。

- [官方 demo：家庭總覽](https://demo.home-assistant.io/#/lovelace/home)、[整合](https://demo.home-assistant.io/#/config/integrations)。
- [官方 frontend demo 說明](https://contributors.home-assistant.io/frontend/development/gallery-and-demo/)、[Demo integration](https://www.home-assistant.io/integrations/demo/)。
- [側欄原始碼](https://github.com/home-assistant/frontend/blob/20260930.3/src/components/ha-sidebar.ts)、[sections demo](https://github.com/home-assistant/frontend/blob/20260930.3/demo/src/configs/sections/lovelace.ts)、[整合頁](https://github.com/home-assistant/frontend/blob/20260930.3/src/panels/config/integrations/ha-config-integrations-dashboard.ts)。

官方 demo 本身能以 hash 路由與 browser mock 運作，但完整 frontend 建置、資產及家庭資料適配需要另一套整合工作。本輪採獨立模擬，以維持現有 Pages 子路徑、產品畫面及共用示範資料；畫面標示與截圖 manifest 明確記錄此差異。

## 驗證

驗證範圍為真 Chromium 與虛構瀏覽器資料；所有暫存使用磁碟。`tests/test_site.py` 保留原有公開頁、提案批准／取消、開發工作、維護與封裝邊界；`tests/test_native_demo.py` 覆蓋家庭控制、設備設定、不自動分享、跨介面規則同步、草稿／瀏覽器歷史、私網連結及三尺寸操作。本機穩定整套 **29 passed／178.78 秒**，涵蓋最後的亮度 0 後重新開燈、新對話／工作／通訊深連結與重新整理回歸；測試與示範來源已提交為 `8e78018d3d9d948fd8dfa2c19139fc535d4c223b`。

示範 TypeScript／Vite、正式產品重建、Ruff、23 份契約、Web toolchain、kit 與文件連結檢查均通過；獨立 Chromium 審查確認三尺寸輸入區、對話框／抽屜、草稿與導覽。完整平台 CI 另以本次 PR 的檢查及交付紀錄為準。正式產品來源與重建資產相對基準沒有差異；已重建來源封裝。

`scripts/site_screenshots.py` 由正常互動狀態生成十張公開 PNG（含 OG）。[截圖 manifest](../../site/assets/screens/manifest.json) 記錄確切來源 commit／tree、來源雜湊與每個畫面的 surface 類型；`native_or_live_acceptance=false`。截圖不注入 DOM、不以歷史原生 HA 截圖拼接成新功能證據。
