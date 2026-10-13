# 0.9.0 家庭現場開發助手

- 由主機一鍵安裝並自動配對，之後另外登入 Codex。你停止或移除 App 後，主機不會自動裝回。
- 開發工作最多執行 14 分鐘、200 次模型呼叫；用量上限、步驟太多、模型拒絕或服務故障時會記錄原因。
- 非文字的產出檔會略過，其他成果照常保留；修改含非文字內容時不保留修改，也不跑驗證。
- 改由 `#stable` 頻道發行；主機更新後，在你同意時更新到同一版本。

# 0.8.0 Home coding preview

- Add the Home coding worker: separate Codex login, a private clone of a pinned public commit, bounded FUSE ext4 workspace, cgroup limits and an independent verifier.
- The App declares only `SYS_ADMIN`, `/dev/fuse` and its own AppArmor profile; the model process gets none of them and no HA or Supervisor token.

# 0.7.0

- Align the version with the HA sidebar release; Runner protocol 1 and worker contract 1 are unchanged.

# 0.6.1 native home and upstream boundaries

- Align the distribution version with native HA entry and upstream adapter checks; retain Runner protocol 1 and worker contract 1.

# 0.6.0 isolated coding and delivery

- Add persistent Host coding dispatch, independent role login, bounded provider/source relay, exact source commit and stopped-writer reconciliation; retained household mode stays restricted.
- Export verified local artifacts, run a separate fixed verifier, produce isolated PNG previews, and export fenced handoff checkpoints.
- Propagate retention to all paired runtimes, expire idle content, preserve bounded non-replayable tombstones, and gate new work while a shorter limit is pending.
- Add Hermes household observation/proposal tools through the existing policy and exact Owner approval flow.
- Add signed same-schema Controller updates/rollback and local trusted-publisher enrollment; introduce schema 3 with private pre-migration snapshots.
- Improve mobile onboarding, device-code copying, LINE pairing flow and same-case work results. Add network preferences and optional Tailscale login with localhost SSH forwarding.
- Home coding, real cloud accounts and household acceptance require their own evidence; engineering fixtures do not establish E1/E2 completion.

# 0.5.1 recovery and E2E fixes

- Coalesce repeated cancellation and protect ongoing cleanup from client disconnects.
- Confirm whole process-group termination using PID identities before cancellation completes.
- Persist uncertain-stop quarantine across restart and refuse new jobs or credential rotation.
- Recover exact lost pairing replies within the original pairing window without storing bearer plaintext.

# 0.5.0 technical preview

Add a separate host-only, tools-disabled bootstrap advisor using the selected official CLI, typed observations and durable idempotent jobs. Retain the existing household restrictions and pinned provider versions; this does not enable the v3 general coding workers.

# 0.4.0 technical preview

Package with the v3 outer Controller and Hermes coordinator. The existing restricted home_operator execution and four pinned CLI versions remain in place. The offline coding sandbox prototype is separate and is not enabled as an HAOS coding worker.

# 0.3.1

與 Controller 0.3.1 同版封裝；CLI 版本與憑證隔離維持原設定。

# 0.3.0

外層 Web-first onboarding、統一 LINE／Discord／Telegram 通訊、獨立內外層 Runner 登入。新增 Antigravity 1.3.3 與 Grok Build 1.0.50；保留家庭提案批准與執行證據。

# 0.1.0

- Codex 0.157.1 與 Claude Code 2.1.296，正式 CLI 登入及結構化輸出。
- 一次性配對、固定 TLS 指紋、私有 bearer、不同 UID／profile。
- 排隊、取消、時間／記憶體／輸出限制、重啟不重播。
- 一般備份排除登入資訊、工作目錄與 TLS private key。
