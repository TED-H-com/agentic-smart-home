# 原生家庭入口與上游相容邊界

Home Assistant 是家庭日常介面；智能管家提供研究、對話、提案與成果；外層 Linux Web 保留初始化及救援。這項分工不改變外層單一 Hermes、Host／Home workers、Controller 權威或原有家庭批准流程。

目標是讓上游變動可被辨識、隔離與驗證，不保證任何未來版本都相容。產品不修改 HA frontend DOM、不 fork HAOS、不把模型生成的程式寫入 Linux 系統服務。上游內部 Python 接點也不會因為包成 adapter 就變成穩定官方 API。

## 已實作的邊界

| 接點 | 產品控制的介面 | 相容失敗時 |
|---|---|---|
| Web → 原生 HA | `GET /api/host/home-entry`，contract 1；只回固定 `/`，不接受任意 redirect URL | 外層入口仍可用；不重設 onboarding、不重送工作 |
| Controller → Hermes bridge | `/v1` HTTP、既有 case/session/run 與 MCP 工具契約 | 保留 case 與錯誤狀態；不隱式換 provider |
| bridge → Hermes 上游 | `agentic_hermes/upstream.py` 集中 AIAgent、MCP 與 Codex auth 相依，adapter contract 1 | 候選 probe 失敗便不接受；既有版本不由候選測試更換 |
| Controller → HA Core | 官方 REST／WebSocket；讀取與家庭寫入分開驗證 | 未測 Core 可繼續原生操作；Agent 自動化寫入沿用受測版本限制 |
| App → Supervisor Ingress | 只信任官方 ingress 來源、再驗有效 Owner | 不把任意 header 或一般 HA 使用者當外層 Owner |
| Host broker → Linux／libvirt／Docker | 固定指令、JSON／XML 與實际功能探針；每次 mutation 重驗必要契約 | 只拒絕受影響操作，保留狀態與獨立救援 |
| Host → HA onboarding | 集中既有原生步驟探針，明示 `upstream_private_volatile` | 404、未知必需步驟或壞格式不推論設定完成 |

官方介面依據：[HA REST](https://developers.home-assistant.io/docs/api/rest/)、[HA WebSocket](https://developers.home-assistant.io/docs/api/websocket/)、[Ingress](https://developers.home-assistant.io/docs/apps/presentation/#ingress)、[Hermes MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)、[systemd 穩定性範圍](https://systemd.io/PORTABILITY_AND_STABILITY/)。Hermes AIAgent、部分 OAuth helper 與 HA onboarding 不是本產品能保證穩定的接點，因此另有集中檢查與候選行為測試。

## 原生入口

只有無明確頁面的日常入口，才會在本產品初始化已完成、綁定 Owner 仍有效、Core 的配置及 frontend 能力讀回成功後進入 HA。新 Core 版本號或額外欄位本身不會阻止瀏覽。必要欄位遺失、服務未就緒或認證失效時，保留外層入口及手動開啟原生 HA 的路徑。此檢查不替代瀏覽器真正載入前端的驗證。

`#gateway`、`#host`、提案等明確連結不自動跳走。離開外層前把原本入口記成 `#entry`，瀏覽器返回與重新整理不會形成跳轉迴圈。HA 登入維持原生流程；不把 Host cookie 或 HA bearer 搬進另一個 origin。只使用同一 loopback hostname 的既有 8099／8123 路徑；遠端仍需既有 SSH forwarding，同時連通兩個端口。

HA 側欄的完整外層 Hermes bridge 尚未交付。目前 Controller Ingress App 有另一份 DB／Engine，不能把它當成已連通的單一管家。下一個整合需使用薄型 Ingress adapter、經驗證的 Owner 身份及同一外層 case／proposal store；身份、撤權、斷線恢復與家庭批准需完整測試。HA Assist／語音接入也另行驗收。

## 候選與發行

1. 保留產品及各上游鎖定來源。新候選解析一次 revision／digest，測試過程不修改正式 lock、帳號或已安裝版本。
2. 先驗產品 protocol/schema 與實際使用的能力，再跑隔離行為測試；不以版本字串或 import 成功取代完整握手。
3. Hermes 必須通過真上游模型迴圈、MCP、登入 helper 與負向路徑的離線 transport 測試。真 OAuth 同意與雲端研究另列，不能由 fixture 宣稱完成。
4. HA Core／Supervisor 變更跑真 Core／App、身份、事件重連、提案／批准、備份及讀回測試；HAOS／Linux 基礎映像變更還需精確媒體的一次性 KVM 安裝、還原與重啟。
5. 一次只更換一個上游組件，記錄確切版本矩陣。沒有證據的組合不能自動升級為受支援；安全更新仍需及時測試與修補，不以永久鎖版取代維護。

可在開發機執行只測試的 Hermes 候選：

```bash
python3 scripts/check_upstream.py --hermes locked --report evidence/upstream-locked.json
python3 scripts/check_upstream.py --hermes latest --report evidence/upstream-candidate.json
```

`Upstream compatibility candidate` GitHub workflow 提供相同的手動入口。它使用固定 repo、產品 adapter 與鎖定 dependencies，在獨立無網路容器驗證，限制時間／記憶體／程序與輸出，最後移除測試容器、volume、tag 及 scratch；不部署。結果分別保存輸入 source hashes 與實際候選 context hashes，另含候選 commit／image ID、契約版本及清理狀態；失敗和相容性未知都不當成 passed。

現有固定 updater 仍只處理 **同 schema 3 的 Controller image**。Hermes 首次最新來源準備有自己的候選 gate 與初次安裝 fallback；不是使用中 Hermes 通用更新器。Linux、HAOS、Core 的完整升級／退版還需要各自的機制及驗證；目前不能宣稱任意 OS 更新失敗都會自動退回。外層救援不依賴 HA 可用，HA 原生規則也不依賴 Hermes 持續運作。
