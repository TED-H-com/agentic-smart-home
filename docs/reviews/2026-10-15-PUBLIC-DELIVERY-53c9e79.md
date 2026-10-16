# 0.9.0 公開交付驗證後記 — 53c9e79

2026-10-15：Owner 以「發布, 繼續推」核准 [原發布提案](2026-10-15-OWNER-TRIAL-53c9e79.md)。本頁記錄之後的實際結果；原核准提案及本機驗證紀錄保持原樣。

公開 `stable` 與 `release/0.9.0` 已建立，均指向固定產品來源 `53c9e790cccf9d22842ec358b9f4f7160e1d448b`。額外公開驗證沒有通過，因此尚未建立 `v0.9.0` tag、GitHub Release 或上傳媒體。依原提案「任一驗證失敗則保留失敗證據，停止後續發布動作」，未自動重試；本次驗證文件先保存於獨立本機文件分支。

## 本次公開驗證

| 項目 | 實際結果 |
|---|---|
| 執行 | `public-candidate-53c9e79-approved-1`，一次 `verify_public_only` |
| enable | 2026-10-15 22:35:08–23:26:17 UTC；41 passed、1 failed、0 errors、0 skipped |
| 失敗案例 | `test_real_home_worker_enable_backup_reboot` |
| 失敗階段 | `cold_backup` |
| bridge-haos | `not_run`；沒有沿用本機成功當作公開驗收 |
| 公開兩階段 | 尚未通過；不是新的完整八階段驗收 |

測試先完成 13 項檢查，包括公開來源身分、側欄自動安裝、Home worker 建置／啟用、可登入狀態、TLS 指紋及容器合約。`cold_backup` 期間觀測到 Supervisor `freeze` 與 App 停止；之後 `tests/test_home_worker_enable_haos.py:780` 準備查詢 Supervisor `GET /info` 時，連線 Core WebSocket 的 HTTP upgrade 回應等待 30 秒後出現 `TimeoutError`。此時尚未送出該查詢。

備份最終狀態沒有被觀測，不能判定備份本身失敗或完成；cold 備份沒有驗收，後續整台重開與身分恢復檢查未執行。根因尚未建立。主機執行期間的 I/O 壓力只是相關觀測，不能當成已證實的原因。本機成功與本次失敗的實際 primary 均為 HAOS 18.3／Core 2026.10.1／Supervisor 2026.10.2。

本次保留的 [摘要](../../evidence/owner-trial-53c9e79/public-approved-1-failed/summary.json)、[enable 階段](../../evidence/owner-trial-53c9e79/public-approved-1-failed/enable/stage.json)、[JUnit](../../evidence/owner-trial-53c9e79/public-approved-1-failed/enable/junit.xml)及 [整合結果](../../evidence/owner-trial-53c9e79/public-approved-1-failed/enable/home-worker-enable-haos.json)是經審查的公開欄位投影。失敗本文、原始日誌、私人路徑與 fixture 帳號資料不公開；原始檔案另以 SHA-256 綁定保存。

## 清理、來源與限制

原始整合結果的五個清理欄位均為 true；wrapper 記錄測試名稱範圍內沒有新增容器或網路，磁碟 basetemp parent 已移除。獨立審查及整合者直接觀測亦確認此範圍的容器／網路不存在；pytest 暫存位於 ext4。執行服務已結束、PID 為 0，`Restart=no`。其他工作資源未清除。

公開 `stable` 在 enable 前後均為指定 commit；失敗後的 wrapper 身分檢查通過，來源工作目錄乾淨，六個映像 ID、manifest 與媒體大小仍吻合。最初啟動前已完整核對媒體 SHA-256；此次失敗後沒有另重讀大型媒體，也未重建或改寫它們。

[先前本機八階段](../VERIFICATION.md)仍是獨立有效的本機工程結果，不補足本次公開失敗。模型傳輸、帳號及 Host 能力仍含明示 fixture；沒有真雲端登入、實體筆電或家庭設備驗收。32 個產品案例維持 2 passed、30 not_run。

## 後續開發

另一條開發線已提交 [草稿 PR #102](https://github.com/TED-H-com/agentic-smart-home/pull/102)，實作 worker 完成後回到原 bot／同一 case 的固定狀態通知。來源為 `10794887e08cfaed2dc4dba7f57560ecc6676075`，未合併，未納入本次 53c9e79 媒體。獨立審查與 115 項針對測試通過；[push CI](https://github.com/TED-H-com/agentic-smart-home/actions/runs/38820493448)及 [PR CI](https://github.com/TED-H-com/agentic-smart-home/actions/runs/38820534886)各為 2100 passed、9 skipped、0 failures／errors，其中 41 項是新增通知測試。傳輸使用 fixture，沒有向真實帳號發訊。
