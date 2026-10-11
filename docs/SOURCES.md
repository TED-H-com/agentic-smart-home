# 上游與第三方來源

查核日期：2026-10-11。確切 commit 見 `upstream.lock.json`，官方 stable channel 仍為 HAOS 18.3、Core 2026.10.1、Supervisor 2026.09.3。本次已實際抓取四個來源工作樹至 `_upstream/sources/`；發行 ZIP 不複製整份上游原始碼。

## Home Assistant

- [Stable channel](https://raw.githubusercontent.com/home-assistant/version/master/stable.json)
- [官方 x86-64 安裝](https://www.home-assistant.io/installation/generic-x86-64/)
- [Apps 設定](https://developers.home-assistant.io/docs/apps/configuration/)、[本地安裝](https://developers.home-assistant.io/docs/apps/tutorial/)、[通訊](https://developers.home-assistant.io/docs/apps/communication/)、[Ingress](https://developers.home-assistant.io/docs/apps/presentation/)
- [Core 2026.10.1 automation config](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/config/automation.py)、[config writer](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/config/view.py)
- [原生 WebSocket commands](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/websocket_api/commands.py)、[auth adapter](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/config/auth.py)
- [Supervisor 2026.09.3](https://github.com/home-assistant/supervisor/tree/2026.09.3)：`supervisor/api/ingress.py`、`api/backups.py`、`api/middleware/security.py`、`apps/validate.py`、`apps/app.py`
- [Alexa Smart Home](https://www.home-assistant.io/integrations/alexa.smart_home/)、[Alexa Devices](https://www.home-assistant.io/integrations/alexa_devices/)、[Nest](https://www.home-assistant.io/integrations/nest/)

Core 的非公開 config adapter 在此版本會轉換部分舊欄位名稱；本產品直接編譯成讀回一致的形式。`validate_config` 的此版本實作採複數 `triggers`／`conditions`／`actions`，因此版本固定的原始碼及真實測試是相容性依據。

## Providers

- [Codex 官方認證](https://developers.openai.com/codex/auth)、[CLI 參考](https://developers.openai.com/codex/cli/reference)
- [Codex 0.157.1 tag](https://github.com/openai/codex/tree/rust-v0.157.1)、[config schema](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/core/config.schema.json)、[官方 model catalog](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/models-manager/models.json)
- [Claude Code CLI 參考](https://code.claude.com/docs/en/cli-reference)
- npm：`@openai/codex@0.157.1`、`@anthropic-ai/claude-code@2.1.296`，完整 transitive lock 在 `runner/package-lock.json`。

## 第三方檔案

`runner/agentic_runner/codex-models.json` 衍生自官方 Codex 的固定 tag，Apache-2.0，授權原文隨附 `CODEX-LICENSE`。工具 metadata 的修改記錄在 `providers.lock.json`；官方模型訊息保留。沒有修改 provider binary；Docker build 從官方 npm package 或 Antigravity 官方 checksum 固定的 archive 安裝鎖定版本，相關產品登入、帳號、使用條款及額度仍依官方服務。

其餘 Python、React、Vite、Node、Debian 套件的版本與完整性雜湊見對應 lock／Dockerfile，授權依各套件隨附文件。

## 0.3 通訊與新增 CLI

LINE.js 固定來源與本機 Host patch、新增 Antigravity／Grok 的官方來源和協定限制見 [GATEWAY.md](GATEWAY.md)。版本／checksum 見 `channels.lock.json`、`providers.lock.json`、`runner/agy.lock.json`；不隨來源庫附帶任何使用者 credential。
