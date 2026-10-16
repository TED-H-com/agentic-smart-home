# 0.9.0 工程預覽發布與匿名下載核驗 — 2026-10-16

固定產品與 harness 來源為 `53c9e790cccf9d22842ec358b9f4f7160e1d448b`；公開 [v0.9.0 工程預覽](https://github.com/TED-H-com/agentic-smart-home/releases/tag/v0.9.0) 已於 `2026-10-16T03:22:04Z` 發布，Release ID `414429247`，prerelease，`make_latest=false`。

43 份新增 ISO 小分塊的上傳已完成。草稿最終化、發布前完整資產端點與內嵌 ID 核對、發布後伺服器核對均通過；52 份附件的名稱、大小、SHA-256 與 canonical tag URL 精確符合已審核清單。原始四份媒體的位元組身分不變；ISO 保留原第二份 1 GiB 分塊，其餘為 43 份不超過 128 MiB 的小分塊，合計 44 份 ISO 分塊；TAR 維持三份原分塊。

匿名核驗實際下載 SHA-256 `8c462b1a7b4bc7bf667afe75722dfaf8e6cb76a0499379d6df17fc2e24ecf73d` 的公開工具與 schema-2 manifest，在全新磁碟目錄執行網路下載。這次同一呼叫的紀錄包含全部 49 份傳輸物件；工具核對四份完整原檔成功，也實際下載並核對驗證 ZIP。公開 Release/API/HTML 可匿名讀取，附件精確為 52 份，stable、release branch、tag 與 A2 文件參照均吻合，v0.9.0 未成為 latest。54 份這次擁有的驗證副本與暫存目錄已移除，原始媒體、全部本機交付檔及舊失敗紀錄均保留。

驗證 ZIP `agentic-smart-home-0.9.0-verification-bd0b00617fe8.zip` 是 A2 `bd0b00617fe831dc44853d6149d67b7107567d3f` 的凍結快照，包含當時已審核的文件與證據，不包含本次發布後的 docsB。此文與新收據補記實際發布結果；不修改原先 2026-10-15 核准及失敗紀錄，也不把 A2 離線重組改稱網路下載。原本機八階段與額外兩個公開階段的驗證範圍維持原樣，沒有另跑一輪完整八階段。真帳號、實體筆電及家庭 E1/E2 尚未驗收；32 個產品案例仍為 **2 passed、30 not_run**。

新收據均在 [`release-publication-v2`](../../evidence/owner-trial-53c9e79/release-publication-v2/)：[43 份上傳完成](../../evidence/owner-trial-53c9e79/release-publication-v2/upload43-completion.json)、[草稿最終化](../../evidence/owner-trial-53c9e79/release-publication-v2/draft-finalization.json)、[發布前核對](../../evidence/owner-trial-53c9e79/release-publication-v2/draft-assets-reviewed.json)、[發布完成](../../evidence/owner-trial-53c9e79/release-publication-v2/published-command.json)、[匿名下載](../../evidence/owner-trial-53c9e79/release-publication-v2/anonymous-download.json)、[實際工具呼叫](../../evidence/owner-trial-53c9e79/release-publication-v2/anonymous-command.json)、[副本清理](../../evidence/owner-trial-53c9e79/release-publication-v2/anonymous-cleanup.json)。私有 inventory、啟動收據與原始日誌不納入公開文件。
