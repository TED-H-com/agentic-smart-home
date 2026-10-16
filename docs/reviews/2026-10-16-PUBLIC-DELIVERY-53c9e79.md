# 0.9.0 第二次公開交付驗證

固定產品及測試來源為 `53c9e790cccf9d22842ec358b9f4f7160e1d448b`。Owner 在第一次公開驗證失敗並停止發布後，允許繼續發布及必要重試，不需逐次確認。本輪沒有修改產品、凍結 harness、四份原始媒體或六個映像。

2026-10-16 00:36:04–00:56:47 UTC，同一次 `verify_public_only` 呼叫依序完成兩個公開階段：

| 階段 | JUnit | 實際整合範圍 |
|---|---|---|
| enable | 42 passed、0 failed／error／skipped | 真 Supervisor 從公開來源建置、安裝及啟用 Home App，16 項檢查通過，含 cold 部分備份、整台 HAOS／Controller 重開及配對／憑證身分恢復。從按下啟用到可登入為 255.963 秒。 |
| bridge-haos | 40 passed、0 failed／error／skipped | 8 項公開整合檢查及原生 Chromium HA 側欄操作通過，涵蓋原生身分、偽造標頭拒絕、同一對話、跨來源與主機管理拒絕、App 重啟及手動停止。 |

每個階段只有一個真 VM 整合案例，其餘為 harness 單元測試。兩個 VM 都是 HAOS 18.3、Core 2026.10.1、Supervisor 2026.10.2。兩階段實際使用 `https://github.com/TED-H-com/agentic-smart-home#stable`；各階段前後均讀回精確產品 commit。Supervisor 接受 branch 而非 commit digest pin，因此只宣稱上述前後讀回範圍。側欄 receipt 的 `published_release_branch` 是舊欄位名稱，不能解讀為使用另一條 branch。

錯誤 TLS pin 注入位於凍結 `tests/test_bridge_haos.py` 的 `if not public` 區塊，本公開輪未執行；它在先前本機八階段的側欄測試中已通過。文件匯入工具曾誤要求本機專用的第九項檢查，已以另份工具修正為精確八項公開檢查，明列本輪未執行的項目，並驗證缺少、false 或多列該檢查都會被拒絕。沒有修改測試或把未執行改成通過。

兩份 primary、JUnit、命令、來源、前後 ref 與清理收據均經 root 及獨立核對。測試 VM、外層容器、測試網路及磁碟暫存均已移除；只對已知測試名稱範圍作資源不存在的宣稱。既有 184 份證據 digest 與第一次公開失敗區塊完整保留，另新增本輪八份清理後證據。原核准筆記及第一次公開失敗筆記的 bytes 未改動。

第一次公開 enable 的 41 passed、1 failed 仍保留：Core WebSocket HTTP 握手在送出 Supervisor `GET /info` 前逾時，備份最終結果未觀測，原因未確立。本輪通過不證明舊失敗的原因，也不把舊備份或舊重開機改為通過。

這是既有完整本機八階段之外的兩階段公開驗證，不能另宣稱一次新的八階段。模型推論與 Host contract 仍為明示 fixture，沒有真雲端登入或模型／coding 工作；實體筆電及家庭 E1/E2 尚待驗收。32 個產品案例仍為 2 passed、30 not_run。工程驗證文件不等於實際發布證明；tag、Release 與公開下載校驗會在實際完成後另存收據。

原 ISO 與 TAR 分別切成 7 及 3 份原始分塊，未重新壓縮。加上原始 source ZIP、SHA256SUMS、分塊 manifest 與下載工具，共 14 份媒體／工具資產。已用實際隨附工具離線重組並核對四份完整原檔的固定 SHA-256；新建的兩份重組驗證副本已移除，原始媒體保持不變。驗證文件 ZIP 是另附的文件增補，保留本機八階段、歷史失敗及本輪公開結果，不改寫原始 source ZIP。見 [重組校驗](../../evidence/owner-trial-53c9e79/release-transport/offline-reassembly.json)及 [實際分塊 manifest](../../evidence/owner-trial-53c9e79/release-transport/release-parts.json)。此處僅證明離線重組，不宣稱已完成上傳或公開下載。

證據：[本輪摘要](../../evidence/owner-trial-53c9e79/public-approved-2/summary.json)、[來源](../../evidence/owner-trial-53c9e79/public-approved-2/publication-provenance.json)、[獨立核對](../../evidence/owner-trial-53c9e79/public-approved-2-independent-review.json)、[第一次失敗](../../evidence/owner-trial-53c9e79/public-approved-1-failed/summary.json)、[本機八階段](../../evidence/owner-trial-53c9e79/summary.json)。
