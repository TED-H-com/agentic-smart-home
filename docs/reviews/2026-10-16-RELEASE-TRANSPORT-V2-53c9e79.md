# 0.9.0 下載分塊與離線核驗

來源固定為 `53c9e790cccf9d22842ec358b9f4f7160e1d448b`。本次只更新 GitHub Release 的下載分塊、下載工具與驗證文件；ISO、TAR、source ZIP、SHA256SUMS 沿用原始媒體。

第一次並行上傳與第二次逐檔上傳皆收到 HTTP 408／upload body timed out。這是實際傳輸失敗，根因未確立；兩輪 [失敗一](../../evidence/owner-trial-53c9e79/release-transport-v2/upload-attempt1-failed.json)、[失敗二](../../evidence/owner-trial-53c9e79/release-transport-v2/upload-attempt2-failed.json)完整保留。舊版七份 ISO／三份 TAR 分塊的離線核驗仍是歷史證據，不作新版附件的驗收。

新版保留已上傳且 GitHub SHA-256 相符的 ISO `.part002`（1 GiB）、TAR 三份分塊、原始碼 ZIP 與 SHA256SUMS。其餘六個舊 ISO 分塊只讀取、不改寫，轉為 43 個最多 128 MiB 的原始小分塊；最後一份 94,699,520 bytes。新版 ISO 共 44 份，TAR 共 3 份；加上 source ZIP、SHA256SUMS、工具、manifest 與獨立驗證文件 ZIP，預定共 52 個 Release 附件。

[產生收據](../../evidence/owner-trial-53c9e79/release-transport-v2/preparation.json)核對舊七個 ISO 分塊各自 SHA-256 及串接後完整 ISO SHA-256，舊檔 inode／大小／mtime／ctime 均未改變。新版 manifest 為 schema 2；工具固定來源、四個原檔大小與 SHA-256、44 份 ISO 的名稱／順序／大小及既有 TAR 三份與 ISO 第二份的 SHA-256。拒絕多餘、缺少、重複、路徑或錯誤大小的 manifest；重組後仍必須核對完整原檔 SHA-256。

已使用實際新版工具及 manifest 完成 [四原檔離線重組](../../evidence/owner-trial-53c9e79/release-transport-v2/offline-reassembly.json)：49 份 regular-file 輸入複製到新的磁碟暫存，執行 `--media all --join-only`，四原檔全部通過。沒有網路下載；舊 15 份本機上傳檔、凍結來源與原始四媒體身分前後相同，這輪重組副本已移除。

工具及產生器的 [獨立審查](../../evidence/owner-trial-53c9e79/release-transport-v2/helper-generator-independent-review.json)完成 11 個 fixture 加 6 個下載負例；[離線 verifier 獨立審查](../../evidence/owner-trial-53c9e79/release-transport-v2/offline-verifier-independent-review.json)已核對成功前的舊 15 檔重查。這些小型測試沒有替代上述實際大型重組。

本文件只證明新版傳輸的離線可重組性。Release 尚未由本收據發布；全部附件的遠端 SHA-256、公開狀態、匿名完整下載與重組須另行驗收。產品既有本機八階段、公開兩階段及 CI 計數不變；真帳號、實體筆電、家庭 E1/E2 與其餘 30 個產品案例仍待驗收。
