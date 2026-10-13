# Agentic Smart Home Runner

家庭現場的開發助手與受控 CLI。由主機的「設定 → 開發助手 → 啟用家庭現場」安裝並自動配對，不需要手動操作。

- 開發助手使用獨立的 Codex 登入，在固定公開 commit 的私有 clone 調查、修改和跑測試；成果回到主機的同一份工作，部署仍需你批准。
- 受控家庭 CLI：Codex 0.157.1、Claude Code 2.1.296、Antigravity 1.3.3、Grok Build 1.0.50，各自用非 root UID 與私有 profile。
- App 宣告 `SYS_ADMIN`、`/dev/fuse` 與自訂 AppArmor 來建立隔離環境；模型程序沒有這些能力，也沒有 HA／Supervisor token 或直接 LAN 存取。隔離不可用時不派工。
- TLS 8443 只給主機連線，以憑證指紋和私有 bearer 驗證，不需要轉發到公網。需要 amd64。

來源與套件版本分別鎖在 `coding-source.lock.json` 和 `worker-dependencies.lock.json`。範圍與平台驗證見 [Workers](../docs/WORKERS.md)。
