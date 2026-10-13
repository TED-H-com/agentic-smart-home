# Home coding worker 0.8.0 開發交接

更新：2026-10-13 UTC（紐約 2026-10-12 晚間）。交接給下一個開發 session。

**內層 Home coding worker 已實作，最後一輪真 HAOS 工程測試與 GitHub CI 已通過，測試 VM 與資源已清理。此紀錄隨 0.8.0 收尾文件與證據提交；不要重新開始上一輪測試。** 這是 0.8.0 原始碼／App 工程預覽，最新完整安裝 ISO 仍為 0.7.0。真雲端帳號、Owner 家庭與完整 E1／E2 尚未驗收。

## 使用者方向

- 外層 Hermes 是主要協調者；Host 與 HAOS App 內的 Home worker 都能研究、寫程式、跑測試。沿用 Controller／TLS Runner 派工，同一 case 收回結果，不另造 Agent 框架。
- 初始化盡量自動：Linux／網路 → 選 coding CLI 並官方登入 → 隨附且受測的 Hermes → LINE 為預設通訊，可選 Telegram／Discord／Web → HAOS → 原生 HA 設定。
- 完成初始化後，原生 HA 是日常首頁，智能管家透過薄 Ingress App 接外層；不修改 upstream 前端或 Supervisor。HA 保留官方更新，外層維護自家 adapter 與受測依賴。
- 使用者已授權開發及提交此 public repo；最新另要求留下可接續的交接紀錄。正式家庭變更仍需受控提案與批准。

## Repo 與精確來源

公開 repo：[teddashh/agentic-smart-home](https://github.com/teddashh/agentic-smart-home)。本機工作目錄是私人父 repo 內的 `agentic-home/`；外層父目錄不得打包或發布。

| 用途 | 來源 |
| --- | --- |
| 開發分支 | `feat/home-coding-worker-0.8` |
| 本次收尾前 HEAD，已推送 | `7b2c4bd29d78f7f2f61e4e52d16569b8b08b20a0` |
| 受測 runtime／配置／編譯 UI | `c19610ae52322066f6d25e2d88c57f040de29641` |
| Home 映像內的研究原始碼 | `bf831fc8e6082c886b988f4e1cbdfecab5238380` |
| 收尾前 main，0.7.0 | `5a2f2b2ad7b52660395b0562456556f379b65ad4` |

`7b2c4bd` 只補測試、文件與證據；runtime 與 `c19610ae` 相同。研究來源由 `runner/coding-source.lock.json` 獨立固定，不能誤當作 App 執行碼來源。發布目標為 `main` 與 `release/0.8.0`；本文件的收尾提交只改文件／證據，精確 runtime 仍是上表來源。既有 `release/0.7.0` 不移動。接手時以下方 refs 命令核對實際遠端狀態，不把這份文件內的歷史 HEAD 當成最新 HEAD。

## 已完成的實作

- `runner/agentic_runner/home_service.py`、`home_sandbox.py`、`home_launcher.py`：真正 App 內的 Home worker，worker ID `home-coder`，沿用共用 coding 契約與全域單一工作 lease。
- 首個 coding provider 是 Codex 0.157.1。Claude／Antigravity／Grok 的既有家庭／初始化模式保留，尚未成為 coding adapter。Home 登入獨立放在 `/data/coding-auth`，不複製 Host／Hermes／家庭操作帳號。
- 每個 job 使用私有來源 clone、FUSE ext4 配額、namespace、cgroup、seccomp、唯讀 root、UID 10000 與受控 relay。App 只申請必要的 `SYS_ADMIN`、自訂 enforcing AppArmor 與 `/dev/fuse`；無 Docker socket、host PID／network 或 full access。
- 固定觀測只查 App 視角及 `homeassistant /api/`，先 80、必要時 8123；無帳號、不取 body、不追 redirect、不開任意 LAN 掃描。證據明列 `observed_from=haos_app`。
- 修改後以另一份無帳號、無網路的 clone 跑固定基準測試；成果與觀測經 TLS 回到原 case，包含內容 SHA-256。UI 已補 Home verifier 結果，三尺寸截圖在 `evidence/ui/home-verifier/`。
- 取消／交接須證明舊 writer 停止。**同一 HAOS boot 的異常 App 重啟會保留 `stop_unconfirmed` 與 lease，阻擋新 writer，不重播模型工作。** App 重啟或 namespace 編號改變本身不能解除此鎖；不可偷偷重啟家庭 HAOS 來解鎖。
- `scripts/worker_runtime.py` 生成並核對 23 個共用 Host 模組。映像、provider 與 sandbox 套件有 lock；尚不能宣稱所有 Linux apt 套件完全釘版。

## 已驗證結果

| 驗證 | 結果與證據 |
| --- | --- |
| 本機完整回歸，來源 `c19610ae` | **1116 passed／5 skipped**；[JUnit](../evidence/pytest-0.8.0.xml) |
| 同來源 GitHub CI 38330431861 | **1114 passed／7 skipped**；[CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38330431861)、[artifact 核對](../evidence/github-ci-0.8.0-runtime.json)、[JUnit](../evidence/pytest-0.8.0-runtime-github.xml) |
| 最新 harness GitHub CI 38332485177 | **1119 passed／7 skipped**；[CI](https://github.com/teddashh/agentic-smart-home/actions/runs/38332485177)、[artifact 核對](../evidence/github-ci-0.8.0.json)、[JUnit](../evidence/pytest-0.8.0-github.xml)，來源 `7b2c4bd` |
| 真 HAOS 第十二輪 | **passed，324.151 秒**；[完整 receipt](../evidence/home-worker-haos.json)、[JUnit](../evidence/home-worker-haos.xml) |

真平台為 HAOS 18.3／Core 2026.10.1／Supervisor 2026.09.3。JUnit 的 **54 passed** 是一個真 VM 整合案例加 53 個 harness 單元測試，不是 54 個平台案例。整合內含 38 項獨立基準測試、30 項 worker 邊界檢查及 15 項整合檢查；TLS 取消 1.349 秒。實際驗證磁碟 ENOSPC、CPU throttling、PID 上限、OOM、停止後回收，以及正常／異常重啟後不重播。

Supervisor 安裝的是保留產品 layers／配置／AppArmor 的衍生測試 App，明示加入模型 transport 與診斷 helper。真 Codex 工具確實改了封裝來源並跑測試，模型回答由 fixture 提供；不代表真雲端自主研究品質已驗收。原始失敗各輪保留在 `evidence/home-worker-haos-run*.json`，不得覆寫成成功。

最後 receipt 的四項清理均為 true：outer、VM、network 已移除，disk fixture ownership 已恢復；pytest wrapper 也已清除 basetemp。沒有待續查的本次 VM 工作或 shell session，不要把其他專案的容器當作本次資源刪除。

32 個 Plan v3 產品案例仍是 **2 passed／30 not_run**；詳見 [進度帳本](v3/PROGRESS.md)、[Workers](WORKERS.md)、[版本驗證](VERIFICATION.md)。

## 本輪收尾與接手入口

Workers、Plan v3 帳本、版本驗證與本交接已同步，原三條工作線均已凍結。最新 CI 的 artifact digest、1078 項 ZIP manifest 與 203 份 runtime／配置／編譯資產已核對；上一輪 CI receipt 已另存，無須重送 CI。精確映像與驗證結果集中在 [0.8.0 交付紀錄](../evidence/release-0.8.0.json)。

公開提交前執行來源打包、runtime mirror／contracts／Ruff／文件連結檢查及 gitleaks 審查；掃描結果在 [公開來源審查](../evidence/source-audit-home-worker.json)，其 `scanned_files_sha256` 可核對本次文件和證據。打包位於本機 `dist/agentic-home-0.8.0-source.zip`，大型 ISO 未重建。收尾提交不更動受測產品執行碼，也不部署到使用者家庭。

接手先執行以下唯讀命令。若三個遠端分支均已包含本交接提交、working tree 乾淨，直接處理下一節的產品缺口。若最後 Git push 被中斷，只補齊尚未完成的 fast-forward 發布，不重跑已通過的 VM／CI；先確認遠端沒有其他人的新提交。

```bash
cd agentic-home   # 私人父 repo 內的產品 repo
git status --short
git log -4 --oneline
git ls-remote origin refs/heads/main refs/heads/feat/home-coding-worker-0.8 refs/heads/release/0.8.0
gh run view 38332485177 --json status,conclusion,headSha,url
```

本機另有兩支整合輔助腳本（不在公開 repo）：`~/agent-work/tmp/verify-agentic-home-ci-0.8.py` 接收 run ID 與完整 source SHA，核對 artifact 並寫入 CI receipt；`~/agent-work/tmp/audit-agentic-home-worker.py` 對允許發布的來源跑 gitleaks，逐筆核對公開來源／證據 hash 偽陽性。先讀腳本再用；前者會寫入通用 0.8.0 receipt 檔名，需先保留原紀錄。來源打包命令為 `uv run python scripts/package.py`。只有新程式變更、失敗或未解問題才擴大重驗。

## 下一階段實際缺口

- 真帳號首次 Home coding 登入與自主研究，Host／Home 互補調查的完整家庭案例。
- Home App 的自動安裝／配對／角色登入體驗，目前不能宣稱一鍵內層 onboarding 已交付。
- Owner 的精確提案批准、家庭 helpers、候選版本更新與退版串成 E2；不可拿本輪 fixture 當作已通過。
- 0.8.0 新安裝媒體的建置與精確 ISO 安裝驗收，以及實體 laptop／真通訊帳號驗收。
- 其他 coding providers、更多固定觀測能力、異常 writer 的受控恢復流程；維持現有隔離和真停止證明。

## 接手規則

- 先讀根目錄 `AGENTS.md`；六份 `docs/v3/` Owner 原稿保持原樣，進度寫在 companion 文件。
- pytest 一律 `pytest-disktmp uv run pytest -q ...`，暫存只能在 `~/agent-work/tmp/pytest/` 等磁碟目錄，不能用 `/dev/shm` 或任何 tmpfs。
- Docker 重建、全套測試、HAOS VM 串行執行。不可在開發主機執行 `host/install.py --apply`，不可改共用 Docker／namespace 設定。
- 原三條工作線均已完成：A Hermes／Controller／HomeService，B Home sandbox／launcher，C 測試 harness／Workers／進度與 UI；整合者負責打包、CI、審查及 Git 發布。
- 後續 session 先核對實際 Git 與 receipt，再接續尚未完成的步驟。舊工具 session 已結束時，不反覆等待或重送原工作。
