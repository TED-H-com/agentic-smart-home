# Home Assistant 原生文字通知

此整合讓 HA 自動化透過既有外層 Agentic Home 通訊連線傳送文字。HA 只保存收件人別名和有期限的通知請求；LINE、Telegram、Discord 憑證與實際聊天室 ID 留在外層 Controller。整合不新增 HTTP listener、不要求複製 API key，也不經過 AI 或開發 worker。

這是獨立版本 `0.1.0` 的整合，使用協定 1。隨 0.11.1 候選 Controller 提供，既有 0.10.0 媒體不會因此改變。這一版只提供文字，不註冊圖片或攝影機動作；也不宣稱已具備 LINE OA 整合。

安裝前必須先使用 0.11.0 或包含通知協定 1 的新版 Controller。已發布的 0.10.0 Controller／ISO 不含這項功能，僅安裝整合 ZIP 無法啟用通知。

## 安裝及啟用

1. 從外層 Agentic Home 下載 `agentic-home-notifications.zip`。ZIP 中的 `custom_components/agentic_home_notifications` 必須放在 HA 設定目錄的同名位置，例如 `/config/custom_components/agentic_home_notifications`。保留其他整合，不要覆寫整個 `custom_components`。
2. 重新啟動 HA Core，在「設定 → 裝置與服務 → 新增整合」選擇 **Agentic Home notifications**。
3. 在外層管理頁面完成既有 HA 擁有者授權，啟用通知，並從已設定且允許的通訊目的地建立別名，例如 `household`。HA 不接受原始聊天室 ID。
4. 等待 HA 的 **Controller connection** 顯示連線，再由原生「開發者工具 → 動作」或自動化呼叫服務。

這是可下載的自訂整合安裝包，不是 HACS 收錄或自動安裝宣告。首次連線會將這一組整合綁定到目前 HA 擁有者和 Controller。若要改綁另一個 Controller／擁有者，須明確移除並重新加入整合；新來源 ID 和 epoch 使舊請求失效。

## 原生動作

```yaml
action: agentic_home_notifications.send_text
data:
  recipient: household
  message: 洗衣機已完成。
response_variable: notification_request
```

`recipient` 為外層設定的別名，格式為小寫英文字母開頭，後接小寫英文字母、數字或底線，最多 32 字元。`message` 接受 1 至 1900 個字元；過長訊息會拒絕，不會截斷。可選的回應為 `{request_id, state: queued}`。直接呼叫需要 HA 管理員；HA 自動化／系統內部 context 也可呼叫，但仍須通過整合綁定、啟用、有效 heartbeat、收件人和儲存檢查。

未啟用、收件人未授權、Controller 超過 60 秒沒有更新、佇列已滿或儲存異常時，動作會回報錯誤。各通道是否已登入是獨立狀態；建立佇列請求並不代表通道已提交訊息。

## 狀態與失敗恢復

原生實體分別顯示 Controller heartbeat、等待處理數量、各通道登入狀態與上次傳送結果。實體不包含訊息內容、聊天室 ID、權杖或認領憑證。

- `queued`：HA 已確認請求寫入磁碟，等待 Controller 處理。
- `submitted`：通訊 adapter 已確認接受此次提交；不代表收件人已收到或已讀。
- `unknown`：認領或提交結果無法確認，或認領後程序／整合重新啟動。系統不會自動重送。
- `not_sent`：未傳送，例如請求到期或收件人授權版本已變更。

佇列最多 32 筆，包含已認領但尚未確認的請求；每筆有效期為一小時。最多保留 64 筆不含訊息內容的近期結果。重新啟動不會延長期限。已認領請求在重新啟動後顯示 `unknown`，保留原認領且不再次授權；到期後移入近期結果，逾時確認回傳 false。期限內同一認領可補送實際終態確認，但不能藉此再次提交訊息。

變更別名目的地、授權或通道設定會更新 routing revision。舊的未認領請求會失效，不能悄悄轉送到新的目的地。已認領請求保留原 routing，Controller 在外部效果前仍須重查目前授權。移除／重新加入整合不會重播舊佇列。

儲存資料損毀、格式不符或無法確認寫入時，整合停止授權新請求及認領，不會清空資料後假裝正常。請先保留備份並檢查儲存問題；移除整合會刪除其專用佇列，須由使用者明確操作。

## 實作及驗證範圍

整合透過既有 owner-authenticated HA WebSocket 提供 `status`、`update_status`、`pull`、`claim`、`ack`。每次 claim 只可取得一次 true，即使重送相同 claim ID 也不會再次授權。Controller 先持久化 claiming／sending，再進行下一步；遺失回應或重啟後結果不明時不自動重試。這是保守的單次提交策略，不是外部通道 exactly-once 或已讀保證。

HA 服務註冊於 `async_setup`，運行狀態放在 config entry `runtime_data`；使用 HA 原生 config flow、實體平台和 WebSocket 擴充。[HA 動作文件](https://developers.home-assistant.io/docs/dev_101_services/)、[config entry 生命週期](https://contributors.home-assistant.io/core/integrations/config-entries/)、[WebSocket 擴充](https://contributors.home-assistant.io/frontend/extending/websocket-api/)。

儲存使用私有 atomic HA Store。Core 2026.10.1 的 `async_save` 會記錄並吞掉部分寫入錯誤，因此整合在回覆前會額外核對完整磁碟 revision/data，並同步檔案與目錄；無法確認則停止授權。[固定版 Store 原始碼](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/helpers/storage.py)。

自動測試分為純佇列負面／恢復案例，以及使用實際整合 ZIP、隔離 Core 2026.10.1、真正 owner／non-owner WebSocket、HA 重啟的整合案例。請使用 `pytest-disktmp uv run --frozen pytest -q tests/test_ha_notifications*.py --real-ha`；容器與測試磁碟由 fixture 清理。這些測試不使用真實通訊帳號，也不證明家庭設備、實體電腦或長時間運作已驗收。
