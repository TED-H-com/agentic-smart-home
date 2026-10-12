# Backlog v3 — 最短可用切片與實作相依

**2026-10-11｜計畫 ID，尚未建立 GitHub Issues／PR，也未指派實際人員。**

工作線 A：Hermes／Controller／派工。B：兩層 worker／隔離／診斷。C：Web／preview／交付恢復。團隊可調整工程分組；不增加新的人工開發 gate。Owner 在帳號登入、資料分享、真實設備／Live 授權與接手時參與。

## 1. 排序

| ID | 優先／階段／工作線 | 成果与完成標準 | 必要相依 |
|---|---|---|---|
| V3-01 | P0 / E0 / 整合 | 更新 root Plan/Spec/AGENTS；封存 v2；確認一個 laptop profile、來源基準與能力旗標 | 無 |
| V3-02 | P0 / E0 / A | 外層 Hermes 安裝／啟動／重啟與真登入狀態；鎖版本；HAOS 不可用時仍可進入 | V3-01；真帳號在現場驗 |
| V3-03 | P0 / E0→E1 / A | 沿用 Web/Gateway，映射 Hermes session/run；同一 bot 單一 receiver；不漏送或混用身份 | V3-02；UI 可先並行 |
| V3-04 | P0 / E0 / B | 固定 host/VM/log 診斷＋去密 support bundle＋不靠 Web 的救援入口 | V3-01 |
| V3-05 | P0 / E0 / C | 一份真正可安裝 bundle/ISO、hash、version manifest、最短 setup；非只有 source zip | V3-02、V3-03 基本登入、V3-04 |
| V3-06 | P0 / E1 / A | 薄 MCP worker adapter、case/job metadata、能力握手；保留現有 TLS jobs；提交立即回 handle | V3-03；contract v1 |
| V3-07 | P0 / E1 / B | Host Worker research/development sandbox，真 CLI、獨立 clone、quota、cancel、artifact 匯出 | V3-01；對接 V3-06 |
| V3-08 | P0 / E1 / A+B | lease／去重／重啟查核／人工接手；停止舊 writer 後才交接；無遞迴派工 | V3-06、V3-07 |
| V3-09 | P0 / E1 / C | case 問題回報、分享預覽、隔離 UI preview/browser；Host Worker 修一個真 bug | V3-04、V3-07、V3-08 |
| V3-10 | P1 / E2 / B | Home Worker 在 HAOS App 內真登入和自主研究；source/scratch coding、輕量測試、權限／網路／資源負向驗證 | V3-06；可與 V3-07 並行 |
| V3-11 | P1 / E2 / A+B | 正確觀測位置的 HA 診斷；job read-only callbacks；來源事件去重；內外同 case 互補研究與獨立驗證 | V3-08、V3-10、V3-04 |
| V3-12 | P1 / E2 / A+C | 原家庭 Engine 接 research 結果；確切 proposal/revision；真模型→helpers→批准→觸發→重啟 | V3-06、V3-11；沿用 Engine |
| V3-13 | P1 / E2 / C | 固定同 schema updater／外部 rescue；可信 artifact、舊版保存、失敗退回；CLI 可先於完整 UI | V3-04、V3-05；schema 決策需先做 |
| V3-14 | P1 / E2 / 全組 | Owner laptop 完整「研究→修正→preview→批准更新→現場驗證→退版」及雙 worker 真執行證據 | V3-09、V3-11、V3-12、V3-13 |
| V3-15 | P2 / E3 / B+C | 一個真實低風險 integration／設備的接入；必要的 LAN／USB/bridge 維護；額度／事件診斷 | V3-12；依設備需要調整 |
| V3-16 | P2 / E3 / 全組 | 72h 在使用中累積；相鄰 Core 版本；離機還原；三個案例比單 Agent／雙 worker 的實際增益 | V3-14 之後逐項 |

## 2. 三個必須拿得到的切片

### 切片 A：可安裝、Hermes 能陪上線

V3-01–05。一個 Web、至少一條可登入的模型來源、固定診斷和救援。讓 Owner 先在 laptop 登入、記錄硬體和查安裝問題。不等 Home Worker、Nest／Alexa／LINE、全部 provider 或 72h。

E0 交付前測一次性 VM 安裝；實體硬體結果在 Owner 現場記錄。無帳號時可交工程預覽，但不能將它標成 E1 真實助手。

### 切片 B：Hermes 派外層 coding，真正改一個問題

V3-06–09。從同一 case 提交研究／開發工作；Host Worker 改碼、跑測試與 preview；Owner 能取消或接手；Live 檔案與 DB 未被候選修改。此階段可以由 dev team 手動處理 patch 合併，不等自動升級中心。

### 切片 C：內外互補研究，能在家持續迭代

V3-10–14。內層必須是 HAOS App 裡的真實 CLI，會自主讀取下一份資料並寫／執行小型重現；不是只接受固定 JSON 意圖。兩邊各交不同來源的證據，Hermes 合併；候選可由固定更新器套用與退回。

Home Worker 的輕量原型與 updater 可早於 E1 完成並行开发；不必等整個外層介面美化完再動手。

## 3. 關鍵任務的實作邊界

**V3-06：不是重做 workflow engine。** 在既有 Controller／Runner 擴充 job contract，MCP handler 複用同一授權與業務邏輯。需要有限的持久化／通知，不需要 Kafka、另一套 PM 或通用 fleet registry。

**V3-07／10：不是解除全部 CLI 限制。** 新 profile 真能查來源與寫 workspace，正式家庭角色照舊。以負向測試證明 secrets／網路／主機隔離。若 App 平台限制導致新能力不成立，先回報具體技術阻礙，保留可用診斷；不得以 host 容器假裝通過 inner。

**V3-11：不是 Agent 自問自答。** 每份觀測標來源和時間；模擬資料保留標記。把原始結論藏掉做 verifier，不以「兩個 Agent 同意」代替查核。

**V3-13：不是開放任意升級腳本。** 標準 manifest、可信 publisher、固定服務配置、同 schema相容測試。退版在 Web 壞掉時仍可用；不回寫舊 DB 蓋過新操作。

## 4. 與 v2 工作的對照

| 舊任務 | v3 去向 |
|---|---|
| V2-01 計畫調整 | V3-01，新增 Hermes／兩 coding worker 決策 |
| V2-02／03／07 安裝、診斷、救援 | V3-02–05；補 Hermes run/session 接點 |
| V2-04 外層 Dev Session | V3-07；加 V3-10 內層自主 research/coding |
| V2-05／06 問題回報與 UI preview | V3-09，保留，不重寫一套 UI framework |
| V2-08 雙 Agent 交接 | V3-06／08／11，明確 job 身份、lease、產物與觀測來源 |
| V2-09／10 更新與完整試跑 | V3-13／14 |
| V2-11／12 設備與長期驗證 | V3-15／16 |

舊版已做完的程式應按現行成果重用，不因文件版本變更又建立同功能新模組。新任務 ID 不是把過去全部進度歸零。

## 5. 每個 PR 最少說明

支援哪個 Owner 流程；改到什麼權限／資料／網路；跑了哪些測試且在什麼環境；有哪些限制；怎样取消／停用／退版。PR 修改測試断言、skip、baseline、權限或 auth adapter，需列出變更，不允許只為 CI 綠燈調弱驗收。

小型 PR 跑受影響測試，不每次 CSS 改動都重建 OS。發布候選固定跑核心 smoke；Hermes adapter、執行器、身份、資料 schema、網路／host 變動必須跑對應負向與恢復測試。整機媒體變更才重跑相關 installer／VM 安裝案例。

## 6. 回報模板

```text
文件/應用版本與 source commit：
本次交付 artifact ID、digest 與來源：
可啟用能力（E0/E1/E2/E3）：
Hermes：版本、模型/認證來源、真實或 fixture：
Host Worker：CLI、mode、執行環境、真實或 fixture：
Home Worker：HAOS App 識別、CLI、mode、真實或 fixture：
本次 Owner 可以完成的工作：
互補證據及真正觀測位置：
測試的 model/backend/runtime/device 各自環境：
未測／失敗／阻擋事項：
Live 副作用與批准範圍：
取消／接手／恢復／退版方法：
下一個最短切片：
```

完整驗收 ID 與必要性見 [PILOT_ACCEPTANCE.md](PILOT_ACCEPTANCE.md)。本文件沒有替團隊建立遠端 Issues，也沒有更動任何現有 PR。
