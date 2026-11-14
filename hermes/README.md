# Hermes（管家）

管家 VM 上跑 [Hermes Agent](https://github.com/NousResearch/hermes-agent) **v0.22.7**（與 Oracle4 相同 commit `b200f5bc`），推論走本機 `agy-openai-shim`（`http://127.0.0.1:18795/v1`），不要用 Hermes 內建的 antigravity plugin。

這個目錄只放技能、提示詞、SOUL 和**沒有密鑰**的設定樣板。舊產品裡把 Hermes 包成 Docker／OAuth 子行程的程式碼還在 `archive/agentic-host-os` 的 `hermes/`，不搬進新主線。

## 安裝要點（已在 VM 102 做過）

- 系統帳號 `hermes`，官方 `install.sh --commit b200f5bc60e31588ba2cb60e288029c4a54ba5fa --non-interactive --skip-browser`
- systemd 單元 `hermes-hgw.service`（`hermes gateway run --replace --external-supervisor`）
- 官方 `agy` CLI，shim 程式放在管家家目錄；Google 帳號目錄權限 0700／檔案 0600
- 未授權前不要啟動 shim 與閘道。權杖用既有部署方式拷貝，不要寫進 git
- 在接上 Home Assistant 之前，閘道可以跑，但不會有家庭控制

設定樣板見 `config.template.yaml`。把真正的 MCP URL 或權杖放到客體上的設定檔，不要 commit。
