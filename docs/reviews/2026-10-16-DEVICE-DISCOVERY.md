# 通用設備探索 D1 驗證

2026-10-16；基準 `366d0a29b728417800ebb74484e8babf346ca9af`。本輪是未發布開發增量，不修改 0.9.0 tag、stable、媒體或原發布證據。方向見 [設備探索與接入](../DEVICE_DISCOVERY.md)。

## 交付

- Owner 唯讀探索快照與生成契約；六個 HA 來源各自回報狀態、時間、版本及經欄位投影後的摘要。
- 待設定候選、設備／子設備、整合設定及功能可用性計數；未知來源與不完整依賴不當作完整空結果。
- 設備頁固定原生設定交接、來源範圍、登錄清單、失敗與刷新；探索不覆寫未儲存權限。
- 讀取前後重新確認 HA Owner，回應前確認 Host session／登入世代；不新增模型、bridge 或設備權限。

## 本機結果

| 檢查 | 實際結果 | 範圍 |
|---|---|---|
| `tests/test_discovery.py`、`tests/test_discovery_ui.py`、兩項既有設備頁測試 | 61 passed／191.06 秒 | 最終來源；合成多來源資料、真 Chromium、身份競態、欄位投影、部分失敗、上限、原生連結與權限草稿 |
| `tests/test_real_ha.py --real-ha` | 1 passed／307.61 秒 | 一次性 Core 2026.10.1；六個實際讀取介面皆成功；原有 helper 部署、事件與重啟回歸 |
| TypeScript／Vite、Ruff、23 份契約、web toolchain、`validate_kit.py` | passed | 編譯 UI、App 靜態資產一致、契約及文件連結 |
| `scripts/package.py` | passed | 本地來源封裝，沒有製作或發布新 ISO |
| 390／768／1440 畫面 | 已目視檢查 | 合成資料；無橫向溢出，手機保留原設備權限操作 |

真 Core 使用獨立 Docker 容器與磁碟 config，沒有啟用 LAN 探索，所以空候選只驗證 upstream read contract，不是全屋掃描。原有備份接點是明示 fixture，沒有新 Supervisor／HAOS／帳號或實體設備驗收。所有 pytest 經 `pytest-disktmp` 使用磁碟暫存；Core 容器由 fixture 清除。

初次較廣的 208 項回歸為 **205 passed、1 failed、2 errors**。失敗是新增測試的本機登入字串短於既有 20 字元規則，已修正並在上述 61 項最終套件通過。兩個 error 發生於既有 bridge fixture 建立私有 TLS 檔案時，Host proof 的 15 秒期限已到期；當時共用主機 IO pressure full avg10 約 81%。獨立重跑兩案為 **1 passed、1 error**，剩下的仍是 fixture 的相同到期。保留這些結果，不延長產品期限、不停用 fsync，也不將本機完整套件標成通過；乾淨 CI 的完整結果由對應 PR 顯示。

初輪完整 CI（`38937220332`／`38937429511`）皆為 **2333 passed、3 failed、9 skipped**。三個失敗來自既有示範網站、隔離預覽與重啟測試缺少新的探索回應；已補上共用的合成探索資料與重啟來源回應，保留未知 API、網路外連及錯誤檢查。示範觀測保留固定時間與匹配摘要，沒有連接真實網路；重啟回歸另外確認探索失敗清除舊資料、返回頁面可重讀，而且未儲存設備授權保留。修正後仍需對應新提交的完整 CI 通過才合併。

## 限制

路由器／防火牆、直接 LAN 及品牌帳號的獨立收集器尚未實作；目前受管 HAOS 的 NAT multicast 限制仍在。對話式探索資料分享、可接續設定工作、能力操作驗證與全屋實測按 D2–D4 交付。32 項產品驗收維持 2 passed、30 not_run。
