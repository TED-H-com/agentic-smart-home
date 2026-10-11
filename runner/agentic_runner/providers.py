from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import resource
import shutil
import signal
import time
from pathlib import Path

VERSIONS = {"codex": "0.157.1", "claude": "2.1.296", "agy": "1.3.3", "grok": "1.0.50"}
DISABLED_CODEX_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "hooks", "multi_agent",
    "multi_agent_v2", "browser_use", "browser_use_external", "computer_use", "image_generation",
    "view_image", "code_mode", "code_mode_host", "skill_search", "skill_mcp_dependency_install",
    "workspace_dependencies", "memories", "goals", "sleep_tool", "tool_suggest",
)
MAX_OUTPUT = 1024 * 1024


class ProviderError(Exception):
    pass


def limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CPU, (300, 310))


class Providers:
    def __init__(self, root: Path):
        self.root = root
        self.processes = {}
        self.auth_sessions = {}
        self.metrics = {}
        for provider in VERSIONS:
            profile = self.profile(provider)
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.getuid() == 0:
                os.chown(profile, self.uid(provider), self.uid(provider))
            profile.chmod(0o700)

    def uid(self, provider):
        return {"codex": 1001, "claude": 1002, "agy": 1003, "grok": 1004}[provider]

    def profile(self, provider):
        if provider not in VERSIONS:
            raise ProviderError("未知 provider")
        return self.root / "profiles" / provider

    def environment(self, provider):
        profile = self.profile(provider)
        # Deliberate allowlist: never copy os.environ, SUPERVISOR_TOKEN or peer secrets.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(profile), "LANG": "C.UTF-8",
               "TERM": "dumb", "NO_COLOR": "1", "DISABLE_AUTOUPDATER": "1"}
        if provider == "codex":
            env["CODEX_HOME"] = str(profile)
        elif provider in {"agy", "grok"}:
            secret = profile / "credential.json"
            if secret.exists():
                env["GEMINI_API_KEY" if provider == "agy" else "XAI_API_KEY"] = json.loads(secret.read_text())["value"]
        else:
            env["CLAUDE_CONFIG_DIR"] = str(profile)
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
            secret = profile / "credential.json"
            if secret.exists():
                credential = json.loads(secret.read_text())
                key = "ANTHROPIC_API_KEY" if credential["method"] == "api_key" else "CLAUDE_CODE_OAUTH_TOKEN"
                env[key] = credential["value"]
        return env

    async def spawn(self, provider, args, cwd=None):
        if not shutil.which(provider):
            raise ProviderError(f"{provider} CLI 未安裝")
        privileges = {"user": self.uid(provider), "group": self.uid(provider), "extra_groups": []} if os.getuid() == 0 else {}
        return await asyncio.create_subprocess_exec(provider, *args, env=self.environment(provider),
            cwd=cwd or self.profile(provider), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, preexec_fn=limits, **privileges)

    async def stop(self, process):
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            except ProcessLookupError:
                pass

    async def capture(self, process, input=b"", timeout=300):
        async def read(stream):
            chunks, total = [], 0
            while data := await stream.read(4096):
                total += len(data)
                if total > MAX_OUTPUT:
                    raise ProviderError("CLI 輸出超過上限")
                chunks.append(data)
            return b"".join(chunks).decode(errors="replace")

        readers = []
        try:
            if input:
                process.stdin.write(input)
                await process.stdin.drain()
            process.stdin.close()
            async with asyncio.timeout(timeout):
                readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
            return stdout, stderr
        finally:
            await self.stop(process)
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    def private_files(self, provider):
        for path in self.profile(provider).rglob("*"):
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)

    async def probe(self, provider):
        proc = await self.spawn(provider, ["--version"])
        out, _ = await self.capture(proc, timeout=15)
        version = re.search(r"\d+\.\d+\.\d+", out)
        return {"provider": provider, "version": version[0] if version else "unknown",
                "version_matches": bool(version and version[0] == VERSIONS[provider]),
                "billing": "由所選官方登入方式與帳號方案決定", "hardware_verified": False}

    async def health(self, provider):
        probe = await self.probe(provider)
        if provider in {"agy", "grok"}:
            return {**probe, "status": "connected" if (self.profile(provider) / "verified").exists()
                    else "needs_reauth", "auth_method": "api_key" if (self.profile(provider) / "credential.json").exists()
                    else "subscription" if provider == "grok" else "none"}
        proc = await self.spawn(provider, ["login", "status"] if provider == "codex" else ["auth", "status"])
        try:
            out, err = await self.capture(proc, timeout=20)
        except (TimeoutError, ProviderError):
            return {**probe, "status": "unavailable", "auth_method": "unknown"}
        connected = proc.returncode == 0
        method = "subscription" if provider == "codex" else "unknown"
        if provider == "codex" and "API key" in out + err:
            method = "api_key"
        if provider == "claude":
            try:
                data = json.loads(out)
                connected = connected and data.get("loggedIn", False)
                method = data.get("authMethod", "unknown")
            except ValueError:
                connected = False
        return {**probe, "status": "connected" if connected else "needs_reauth", "auth_method": method}

    async def secret_login(self, provider, method, secret):
        if not secret or len(secret) > 16384 or "\n" in secret or "\r" in secret:
            raise ProviderError("登入資訊格式無效")
        if provider in {"agy", "grok"}:
            if method != "api_key":
                raise ProviderError("請使用 API key，或此 provider 提供的 device code 登入")
            profile = self.profile(provider)
            path = profile / "credential.json"
            path.write_text(json.dumps({"method": method, "value": secret}))
            if os.getuid() == 0:
                os.chown(path, self.uid(provider), self.uid(provider))
            self.private_files(provider)
            try:
                # This checks the actual account through the official CLI, not file existence.
                await self.run("auth-" + str(time.time_ns()), provider, "Reply with status ok.", {
                    "type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
                    "required": ["status"], "additionalProperties": False})
                (profile / "verified").touch(mode=0o600)
            except BaseException:
                path.unlink(missing_ok=True)
                (profile / "verified").unlink(missing_ok=True)
                raise
            return await self.health(provider)
        if provider == "codex":
            if method != "api_key":
                raise ProviderError("Codex 請使用 device code 或 API key")
            proc = await self.spawn(provider, ["login", "--with-api-key"])
            await self.capture(proc, (secret + "\n").encode(), timeout=30)
            if proc.returncode:
                raise ProviderError("Codex 官方登入失敗，請確認 API key")
        else:
            if method not in {"api_key", "oauth_token"}:
                raise ProviderError("Claude 支援 API key 或官方 setup-token")
            path = self.profile(provider) / "credential.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump({"method": method, "value": secret}, file)
            if os.getuid() == 0:
                os.chown(path, self.uid(provider), self.uid(provider))
        self.private_files(provider)
        return await self.health(provider)

    async def begin_device_auth(self, id, provider="codex"):
        if any(a["status"] == "pending" for a in self.auth_sessions.values()):
            raise ProviderError("已有登入流程正在進行")
        if provider not in {"codex", "grok"}:
            raise ProviderError("此 provider 未提供 device code 登入")
        args = ["login", "--device-auth"]
        if provider == "codex":
            args += ["-c", 'cli_auth_credentials_store="file"']
        else:
            (self.profile(provider) / "credential.json").unlink(missing_ok=True)
            (self.profile(provider) / "verified").unlink(missing_ok=True)
        proc = await self.spawn(provider, args)
        self.processes[id] = proc
        duration = 1800 if provider == "grok" else 900
        state = {"id": id, "provider": provider, "status": "pending", "url": None, "code": None, "expires_in": duration,
                 "_expires": time.time() + duration}
        self.auth_sessions[id] = state

        async def consume(stream):
            text = ""
            while chunk := await stream.read(256):
                text = (text + chunk.decode(errors="replace"))[-8192:]
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
                url = re.search(r"https://auth\.openai\.com/[A-Za-z0-9/_-]+" if provider == "codex" else
                                r"https://(?:auth|accounts)\.x\.ai/[^\s\x1b]+", clean)
                code = re.search(r"\b[A-Z0-9]{4,5}-[A-Z0-9]{4,5}\b", clean)
                if url:
                    state["url"] = url[0]
                if code:
                    state["code"] = code[0]

        async def complete():
            try:
                async with asyncio.timeout(duration):
                    await asyncio.gather(consume(proc.stdout), consume(proc.stderr))
                    await proc.wait()
                state["status"] = "connected" if proc.returncode == 0 else "failed"
                if provider == "grok" and proc.returncode == 0:
                    (self.profile(provider) / "verified").touch(mode=0o600)
                self.private_files(provider)
            except TimeoutError:
                state["status"] = "expired"
            finally:
                await self.stop(proc)
                self.processes.pop(id, None)
                if state["status"] != "pending":
                    state["code"] = None

        state["_task"] = asyncio.create_task(complete())
        return {k: v for k, v in state.items() if not k.startswith("_")}

    async def logout(self, provider):
        for proc in list(self.processes.values()):
            await self.stop(proc)
        if provider != "agy":
            proc = await self.spawn(provider, ["logout"] if provider in {"codex", "grok"} else ["auth", "logout"])
            await self.capture(proc, timeout=20)
        profile = self.profile(provider)
        # Fixed, app-owned path only. Erase local auth even if remote revocation was unavailable.
        for path in profile.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        return {"status": "needs_reauth", "remote_revocation": "unknown"}

    def command(self, provider, schema_path, output_path, model):
        if provider == "codex":
            catalog = Path(__file__).with_name("codex-models.json")
            model = model or "gpt-6-astra"
            if model not in {entry["slug"] for entry in json.loads(catalog.read_text())["models"]}:
                raise ProviderError("此 Codex 模型尚未包含於隔離工具設定，請使用打包模型清單中的名稱")
            args = ["exec", "--ignore-user-config", "--ignore-rules", "--strict-config", "--ephemeral",
                    "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                    "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                    "-c", 'web_search="disabled"', "-c", 'approval_policy="never"',
                    "-c", "model_catalog_json=" + json.dumps(str(catalog)),
                    "-c", "tools.experimental_request_user_input.enabled=false",
                    "-c", "tools.update_plan.enabled=false", "-c", "include_environment_context=false",
                    "-c", "project_doc_max_bytes=0"]
            for feature in DISABLED_CODEX_FEATURES:
                args += ["--disable", feature]
        elif provider == "agy":
            args = ["--input-format", "stream-json", "--output-format", "stream-json", "--disable-slash-commands",
                    "--json-schema", schema_path.read_text(), "--print-timeout", "300s"]
        elif provider == "grok":
            args = ["--no-leader", "--no-auto-update", "--no-memory", "--no-plan", "--no-subagents",
                    "--disable-web-search", "--tools", "", "--permission-mode", "dontAsk", "--max-turns", "3",
                    "--prompt-file", "/dev/stdin", "--output-format", "json", "--json-schema", schema_path.read_text()]
        else:
            args = ["-p", "--bare", "--restricted", "--tools", "", "--disallowedTools", "mcp__*",
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--permission-mode", "dontAsk", "--no-session-persistence", "--max-turns", "3",
                    "--output-format", "json", "--json-schema", schema_path.read_text()]
        if model:
            args += ["--model", model]
        if provider == "codex":
            args.append("-")
        return args

    async def run(self, id, provider, prompt, schema, model=""):
        probe = await self.probe(provider)
        if not probe["version_matches"]:
            raise ProviderError("CLI 版本與打包版本不符，請重新安裝 Runner")
        workspace = self.root / "work" / id
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        schema_path, output_path = workspace / "schema.json", workspace / "answer.json"
        schema_path.write_text(json.dumps(schema))
        if os.getuid() == 0:
            os.chown(workspace, self.uid(provider), self.uid(provider))
            os.chown(schema_path, self.uid(provider), self.uid(provider))
        started = time.monotonic()
        metrics = {"peak_rss_bytes": 0, "cpu_seconds": 0.0, "memory_limit_bytes": 1024 ** 3}
        monitor = None
        try:
            if provider == "grok":
                config = self.profile(provider) / ".grok/config.toml"
                config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                config.write_text('[cli]\nauto_update = false\n[memory]\nenabled = false\n'
                                  '[features]\ntelemetry = false\n[telemetry]\ntrace_upload = false\n')
                if os.getuid() == 0:
                    for path in (config, config.parent):
                        os.chown(path, self.uid(provider), self.uid(provider))
                self.private_files(provider)
            if provider == "agy":
                config = self.profile(provider) / ".gemini/antigravity-cli/settings.json"
                config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                config.write_text(json.dumps({"modelProvider": "gemini", "allowNonWorkspaceAccess": False,
                    "permissions": {"allow": [], "ask": [], "deny": [
                        action + "(*)" for action in ("read_file", "write_file", "read_url", "execute_url",
                                                       "command", "unsandboxed", "mcp")]}}))
                if os.getuid() == 0:
                    for path in (config, config.parent, config.parent.parent):
                        os.chown(path, self.uid(provider), self.uid(provider))
                self.private_files(provider)
            proc = await self.spawn(provider, self.command(provider, schema_path, output_path, model), workspace)
            self.processes[id] = proc

            async def watch_resources():
                while proc.returncode is None:
                    rss, cpu = 0, 0
                    for path in Path("/proc").iterdir():
                        if not path.name.isdecimal():
                            continue
                        with contextlib.suppress(OSError, ValueError, IndexError):
                            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                            if int(fields[2]) == proc.pid:
                                rss += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
                                cpu += (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
                    metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], rss)
                    metrics["cpu_seconds"] = max(metrics["cpu_seconds"], cpu)
                    if rss > metrics["memory_limit_bytes"]:
                        metrics["memory_limit_exceeded"] = True
                        await self.stop(proc)
                        return
                    await asyncio.sleep(0.5)

            monitor = asyncio.create_task(watch_resources())
            payload = (json.dumps({"event": "user", "message": {"content": prompt}}) + "\n").encode() if provider == "agy" else prompt.encode()
            out, err = await self.capture(proc, payload, timeout=300)
            if metrics.get("memory_limit_exceeded"):
                raise ProviderError("Agent 已達到 1 GiB 記憶體上限，任務已停止")
            if proc.returncode != 0:
                lower = (out + err).lower()
                if any(s in lower for s in ("401", "unauthorized", "not logged", "authentication")):
                    raise ProviderError("needs_reauth：官方登入已失效")
                if "rate" in lower or "quota" in lower or "limit" in lower:
                    raise ProviderError("rate_limited：供應商額度或速率限制")
                raise ProviderError("CLI 執行失敗；請檢查登入狀態、所選模型與網路")
            if provider == "codex":
                # Only normalized final structured output crosses the broker boundary.
                for line in out.splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("item", {}).get("type") in {
                        "command_execution", "file_change", "mcp_tool_call", "web_search"}:
                        raise ProviderError("CLI 嘗試未授權工具，已拒絕結果")
                if not output_path.is_file() or output_path.stat().st_size > 256 * 1024:
                    raise ProviderError("CLI 未回傳有效結構化結果")
                return json.loads(output_path.read_text())
            if provider == "agy":
                result = None
                for line in out.splitlines():
                    event = json.loads(line)
                    if event.get("step_update", {}).get("step_type") in {"tool", "subagent"}:
                        raise ProviderError("Antigravity 嘗試工具操作，已拒絕此回覆")
                    if event.get("event") == "result":
                        result = event["result"]
                if not result or result.get("status") != "SUCCESS" or not isinstance(result.get("structured_output"), dict):
                    raise ProviderError("Antigravity 未回傳有效結構化結果")
                return result["structured_output"]
            result = json.loads(out)
            if provider == "grok":
                if result.get("structuredOutputError") or not isinstance(result.get("structuredOutput"), dict):
                    raise ProviderError("Grok 未回傳有效結構化結果")
                return result["structuredOutput"]
            if result.get("is_error") or not isinstance(result.get("structured_output"), dict):
                raise ProviderError("CLI 未回傳有效結構化結果")
            return result["structured_output"]
        finally:
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            self.metrics[id] = {**metrics, "elapsed_seconds": round(time.monotonic() - started, 3)}
            self.processes.pop(id, None)
            self.private_files(provider)
            shutil.rmtree(workspace, ignore_errors=True)
