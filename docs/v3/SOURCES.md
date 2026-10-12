# Plan v3 來源索引（團隊補建）

本檔由開發團隊在 2026-10-11 補建，並非六份上傳原稿之一。原稿引用的 `r01`–`r07` 依其描述對應至原基準 commit；`u01`–`u07` 對應查核過的官方文件。這是可追溯的替代索引，不聲稱重建了未收到的 SOURCES 原件。

所有 `r` 引用固定 `3ad476a712009469521c3f9838a44ef35ba29f76`（應用 0.3.1），以免後續 v3 程式反過來被當成文件發出時的基準。上游文件會變動，runtime 的實際 lock 和測試才是候選版本依據。

<a id="r01"></a>
## r01 — 原始產品基準

[Agentic Smart Home 0.3.1 source tree](https://github.com/teddashh/agentic-smart-home/tree/3ad476a712009469521c3f9838a44ef35ba29f76)。原六份文件明列的基準，不是新增 v3 能力證據。

<a id="r02"></a>
## r02 — Linux／家庭架構

[基準 Project Spec](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/PROJECT_SPEC.md)、[Linux product](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/docs/LINUX_PRODUCT.md)。外層 Linux、KVM HAOS、Controller/Runner、固定 broker 與家庭 Engine 是重用起點。

<a id="r03"></a>
## r03 — 既有 Runner 分流與配對傳輸

[Controller Runner client](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/controller/agentic_controller/runner.py)、[Runner broker](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/runner/agentic_runner/app.py)。沿用既有 TLS fingerprint/pairing/jobs；worker contract 與 Runner protocol 分別協商。

<a id="r04"></a>
## r04 — 受限 CLI 啟動基準

[Provider 命令](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/runner/agentic_runner/providers.py)、[provider locks](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/providers.lock.json)。舊命令只證明受限家庭 intent 路徑，不表示已開啟自主 coding。

<a id="r05"></a>
## r05 — 單一 Gateway 與續接

[Gateway 實作](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/controller/agentic_controller/gateway.py)、[Gateway 文件](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/docs/GATEWAY.md)。0.3.1 已有通訊與對話隔離基礎；Hermes mapping 是後續新增。

<a id="r06"></a>
## r06 — 既有驗證及限制

[0.3.1 Verification](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/docs/VERIFICATION.md)。使用者帳號、實際通道、硬體證據的限制不能由基準的自動測試取代。

<a id="r07"></a>
## r07 — 備份、安裝保護與更新缺口

[0.3.1 Operations](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/docs/OPERATIONS.md)、[installer](https://github.com/teddashh/agentic-smart-home/blob/3ad476a712009469521c3f9838a44ef35ba29f76/host/install.py)。既有安裝器拒絕覆蓋 `/opt/agentic-home`；v3 固定 updater 是新增交付。

<a id="u01"></a>
## u01 — Hermes MCP

[Hermes 官方 MCP 文件](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)。外部 MCP 可以提供本產品受限工具；Controller 的授權、持久 job 和遠端 Runner 路由仍由產品實作。

<a id="u02"></a>
## u02 — Hermes 原生子代理

[Hermes 官方 Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation/)。`delegate_task` 的子代理環境不等於本產品已配對的 HAOS coding CLI；不能藉工具名稱推定內層執行。

<a id="u03"></a>
## u03 — Hermes API 與實際 runtime

[Hermes 官方 API Server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)、[查核的上游 source commit](https://github.com/NousResearch/hermes-agent/tree/835c3dd038d903182a66ecc45e34bf5e0de65f00)。開發 adapter 依此固定來源查核 `run_agent.py` 的 `AIAgent` 和 `agent/turn_facade.py` 的 `run_conversation`。本產品 create/continue/status/cancel 是自己的服務契約；不宣稱上游介面同名，也不啟動第二套上游 bot gateway。

<a id="u04"></a>
## u04 — HAOS App 設定與視角

[Home Assistant App configuration](https://developers.home-assistant.io/docs/apps/configuration/)。App 的 API、namespace、mount、`full_access`、`host_pid` 與 AppArmor 都需明示設定；App 內的觀測不能直接當成 Core／整台主機觀測。

<a id="u05"></a>
## u05 — HAOS App 安全

[Home Assistant App security](https://developers.home-assistant.io/docs/apps/security/)。維持保護模式與適當 AppArmor；runtime capability 仍需在實際 App 內驗證，文件存在不代表 coding sandbox 已通過。

<a id="u06"></a>
## u06 — HAOS App 通訊

[Home Assistant App communication](https://developers.home-assistant.io/docs/apps/communication/)。Controller 既有 HA API 路徑與 App 本地 probe 是不同通訊／觀測位置，報告應分開標記。

<a id="u07"></a>
## u07 — 資源限制的 OS 基礎

[Linux kernel cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)。CPU、memory 和 pids controller 提供 OS 層機制；必須檢查實際 delegation 和 enforceability，不能由 App 安裝位置或 RSS 取樣推論 hard limit 已生效。工作區容量與 process/網路隔離另需實作和測試。
