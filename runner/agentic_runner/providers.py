from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import re
import resource
import shutil
import signal
import time
from pathlib import Path

from .reconcile import local_proof, runtime_identity

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


class StopUnconfirmed(ProviderError):
    def __init__(self):
        super().__init__("stop_unconfirmed")


# Fixed user-facing text only. Never attach CLI stdout, secrets, or non-official URLs.
AUTH_MESSAGES = {
    "needs_reauth": "登入已失效，請重新登入。",
    "rate_limited": "已達用量上限，請稍後再試。",
    "invalid_credentials": "登入資訊無效，請重新輸入。",
    "network": "無法連線到 AI 服務，請檢查網路後再試。",
    "cli_output_unrecognized": "無法確認登入結果，請稍後再試。",
    "timeout": "AI 帳號回應逾時，請稍後再試。",
}
SECRET_VERIFY_TIMEOUT = 120
AUTH_CODE = re.compile(r"[A-Z0-9]{4,8}-[A-Z0-9]{4,8}")
OFFICIAL_AUTH_URL = re.compile(
    r"https://auth\.openai\.com/[A-Za-z0-9/_-]+|https://(?:auth|accounts)\.x\.ai/[A-Za-z0-9._~/?#&=%+-]+")
ACTIVE_AUTH = {"pending", "verifying", "stop_unconfirmed"}
REJECTED_SECRET = {"invalid_credentials", "needs_reauth"}


def auth_error(category):
    return ProviderError(f"{category}：{AUTH_MESSAGES[category]}")


def auth_category(exc):
    text = str(exc)
    for name in AUTH_MESSAGES:
        if text.startswith(name + "："):
            return name
    if isinstance(exc, TimeoutError):
        return "timeout"
    return None


class AuthCancelled(Exception):
    """The owner cancelled verification. This must not quarantine the process."""


# A status must stand alone: "14012 tokens" or a hex request id is not HTTP 401.
CLI_STATUS = re.compile(r"(?<![A-Za-z0-9_.])(401|429)(?![A-Za-z0-9_.])")


def classify_cli_output(out, err):
    """Map CLI diagnostics to a category. The raw text is never returned."""
    lower = (out + "\n" + err).lower()
    statuses = set(CLI_STATUS.findall(lower))
    if any(token in lower for token in ("invalid api key", "invalid_api_key", "incorrect api key", "invalid key")):
        return "invalid_credentials"
    if "401" in statuses or any(token in lower for token in ("unauthorized", "not logged", "authentication", "revoked")):
        return "needs_reauth"
    if "429" in statuses or any(token in lower for token in (
            "rate limit", "rate_limit", "ratelimit", "too many requests", "quota")):
        return "rate_limited"
    if any(token in lower for token in (
            "timed out", "timeout", "connection reset", "connection refused",
            "network", "unreachable", "name or service")):
        return "network"
    return "cli_output_unrecognized"


def process_info(pid, uid=None):
    try:
        path = Path(f"/proc/{pid}")
        actual_uid = path.stat().st_uid
        if uid is not None and actual_uid != uid:
            return None
        fields = (path / "stat").read_text().rsplit(") ", 1)[1].split()
        return {"uid": actual_uid, "state": fields[0], "parent": int(fields[1]),
                "group": int(fields[2]), "session": int(fields[3]), "start": int(fields[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def group_members(group, uid):
    result = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            info = process_info(int(path.name), uid)
            if info and info["group"] == group and info["session"] == group and info["state"] not in {"Z", "X"}:
                result[int(path.name)] = info["start"]
    return result


def signal_members(members, uid, value):
    """Check UID/start identity and signal kernel handles, never recycled PIDs."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes, libc.pidfd_open.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    libc.pidfd_send_signal.restype = ctypes.c_int
    for pid, started in members.items():
        fd = libc.pidfd_open(pid, 0)
        if fd < 0:
            error = ctypes.get_errno()
            if error == 3:  # ESRCH: this exact process has already exited.
                continue
            raise OSError(error, os.strerror(error))
        try:
            info = process_info(pid, uid)
            if info and info["start"] == started:
                if libc.pidfd_send_signal(fd, value, None, 0) != 0:
                    error = ctypes.get_errno()
                    if error != 3:
                        raise OSError(error, os.strerror(error))
        finally:
            os.close(fd)


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
        self.auth_lock = asyncio.Lock()
        self.probes = {}
        self.reasons = {}
        self.metrics = {}
        private = root / "private"
        private.mkdir(mode=0o700, exist_ok=True)
        self.quarantine_path = private / "stop-unconfirmed.json"
        self.unconfirmed = json.loads(self.quarantine_path.read_text()) if self.quarantine_path.exists() else {}
        self.runtime_path = private / "runtime.json"
        self.previous_runtime = json.loads(self.runtime_path.read_text()) if self.runtime_path.exists() else None
        self.runtime = runtime_identity()
        self.write_runtime()
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

    def ensure_available(self):
        if self.unconfirmed:
            raise StopUnconfirmed()

    def quarantine(self, reason, pid=None):
        self.unconfirmed.setdefault(str(pid) if pid is not None else "restart", {
            "reason": reason, "runtime": self.runtime if pid is not None else self.previous_runtime or self.runtime})
        temporary = self.quarantine_path.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.unconfirmed, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.quarantine_path)
        directory = os.open(self.quarantine_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def write_runtime(self):
        temporary = self.runtime_path.with_suffix(".new")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.runtime, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.runtime_path)

    async def reconcile(self, external_proof=None):
        if self.processes or any(s.get("status") in {"pending", "verifying"} for s in self.auth_sessions.values()):
            return False
        uids = {self.uid(provider) for provider in VERSIONS} if os.getuid() == 0 else {os.getuid()}
        for value in self.unconfirmed.values():
            previous = value.get("runtime")
            if local_proof(previous, self.runtime, uids):
                continue
            if external_proof is None or not isinstance(previous, dict) or not await external_proof(previous):
                return False
        self.quarantine_path.unlink(missing_ok=True)
        self.unconfirmed.clear()
        return True

    def track_process(self, proc, provider):
        if hasattr(proc, "_agentic_fence"):
            return
        try:
            uid = self.uid(provider) if os.getuid() == 0 else os.getuid()
            leader = process_info(proc.pid, uid)
            owned = leader and leader["parent"] == os.getpid() and leader["group"] == proc.pid and leader["session"] == proc.pid
            proc._agentic_fence = {"uid": uid, "members": group_members(proc.pid, uid) if owned else {},
                                   "lock": asyncio.Lock(), "stopped": False}
        except (OSError, ValueError, AttributeError):
            self.quarantine("process_identity_unconfirmed", proc.pid)
            raise StopUnconfirmed() from None

    async def spawn(self, provider, args, cwd=None):
        self.ensure_available()
        if not shutil.which(provider):
            raise ProviderError(f"{provider} CLI 未安裝")
        privileges = {"user": self.uid(provider), "group": self.uid(provider), "extra_groups": []} if os.getuid() == 0 else {}
        pending = asyncio.create_task(asyncio.create_subprocess_exec(provider, *args, env=self.environment(provider),
            cwd=cwd or self.profile(provider), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, preexec_fn=limits, **privileges))
        try:
            proc = await asyncio.shield(pending)
        except asyncio.CancelledError:
            proc = await pending
            self.track_process(proc, provider)
            await self.stop(proc)
            raise
        self.track_process(proc, provider)
        proc._agentic_provider = provider
        return proc

    async def stop(self, process):
        try:
            fence = process._agentic_fence
            async with fence["lock"]:
                if fence["stopped"]:
                    return
                current = group_members(process.pid, fence["uid"])
                if current:
                    if not any(current.get(pid) == start for pid, start in fence["members"].items()):
                        raise StopUnconfirmed()
                    fence["members"].update(current)
                    signal_members(current, fence["uid"], signal.SIGTERM)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(process.wait()), 2)
                # Leader exit never transfers ownership. Freeze the remaining
                # group and terminate each anchored member through its pidfd.
                for _ in range(100):
                    current = group_members(process.pid, fence["uid"])
                    if not current:
                        await asyncio.wait_for(asyncio.shield(process.wait()), 1)
                        fence["stopped"] = True
                        return
                    if not any(current.get(pid) == start for pid, start in fence["members"].items()):
                        raise StopUnconfirmed()
                    for _ in range(8):
                        signal_members(current, fence["uid"], signal.SIGSTOP)
                        fence["members"].update(current)
                        frozen = group_members(process.pid, fence["uid"])
                        stopped = all((info := process_info(pid, fence["uid"])) is None or
                                      info["state"] in {"T", "t", "Z", "X"} for pid in frozen)
                        if current == frozen and stopped:
                            signal_members(frozen, fence["uid"], signal.SIGKILL)
                            break
                        current = frozen
                    await asyncio.sleep(0.02)
                raise StopUnconfirmed()
        except (OSError, ValueError, KeyError, AttributeError, TimeoutError, StopUnconfirmed, asyncio.CancelledError):
            self.quarantine("process_group_stop_unconfirmed", process.pid)
            raise StopUnconfirmed() from None

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
            try:
                await self.stop(process)
            finally:
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
        self.track_process(proc, provider)
        out, _ = await self.capture(proc, timeout=15)
        version = re.search(r"\d+\.\d+\.\d+", out)
        result = {"provider": provider, "version": version[0] if version else "unknown",
                  "version_matches": bool(version and version[0] == VERSIONS[provider]),
                  "billing": "由所選官方登入方式與帳號方案決定", "hardware_verified": False}
        self.probes[provider] = dict(result)
        return result

    def active_auth(self, provider):
        for state in self.auth_sessions.values():
            if state.get("provider") == provider and state.get("status") in ACTIVE_AUTH:
                return state
        return None

    def auth_in_progress(self, provider):
        # Each provider has its own profile and UID. Nothing in spawn shares a
        # process slot across providers, so only this profile is blocked.
        # stop_unconfirmed stays global through ensure_available.
        return self.active_auth(provider) is not None

    def public_auth(self, state):
        url, code = state.get("url"), state.get("code")
        if not (isinstance(url, str) and OFFICIAL_AUTH_URL.fullmatch(url)):
            url = None
        if not (isinstance(code, str) and AUTH_CODE.fullmatch(code)):
            code = None
        active = state.get("status") in {"pending", "verifying"}
        body = {"id": state["id"], "provider": state["provider"], "status": state["status"],
                "url": url if active else None, "code": code if active else None,
                "expires_in": max(0, int(state.get("_expires", 0) - time.time())) if active else 0}
        reason = state.get("reason")
        if reason in AUTH_MESSAGES:
            body["reason"] = reason
            body["message"] = AUTH_MESSAGES[reason]
        return body

    def _with_reason(self, result, provider, reason=None):
        reason = reason or self.reasons.get(provider)
        if reason in AUTH_MESSAGES:
            result["reason"] = reason
            result["message"] = AUTH_MESSAGES[reason]
        return result

    async def _probe_or_cached(self, provider, active):
        if active and provider in self.probes:
            return dict(self.probes[provider])
        try:
            return await self.probe(provider)
        except (ProviderError, OSError, TimeoutError):
            return {"provider": provider, "version": "unknown", "version_matches": False,
                    "billing": "由所選官方登入方式與帳號方案決定", "hardware_verified": False}

    async def health(self, provider):
        active = self.active_auth(provider)
        probe = await self._probe_or_cached(provider, active)
        if active:
            # Do not spawn a second CLI against a profile that is logging in.
            return self._with_reason({**probe, "status": "needs_reauth", "auth_method": self._stored_method(provider),
                                      "pending_session": self.public_auth(active)}, provider, active.get("reason"))
        if not probe.get("version_matches") and probe.get("version") == "unknown" and provider not in self.probes:
            return self._with_reason({**probe, "status": "unavailable", "auth_method": "unknown"}, provider)
        if provider in {"agy", "grok"}:
            connected = (self.profile(provider) / "verified").exists()
            result = {**probe, "status": "connected" if connected else "needs_reauth",
                      "auth_method": "api_key" if (self.profile(provider) / "credential.json").exists()
                      else "subscription" if provider == "grok" else "none"}
            if connected and self.reasons.get(provider) in REJECTED_SECRET | {"timeout"}:
                self.reasons.pop(provider, None)
            return self._with_reason(result, provider)
        proc = await self.spawn(provider, ["login", "status"] if provider == "codex" else ["auth", "status"])
        self.track_process(proc, provider)
        try:
            out, err = await self.capture(proc, timeout=20)
        except (TimeoutError, ProviderError):
            return self._with_reason({**probe, "status": "unavailable", "auth_method": "unknown"}, provider)
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
                if proc.returncode == 0:
                    self.reasons[provider] = "cli_output_unrecognized"
        if connected and self.reasons.get(provider) in REJECTED_SECRET | {"timeout"}:
            self.reasons.pop(provider, None)
        return self._with_reason(
            {**probe, "status": "connected" if connected else "needs_reauth", "auth_method": method}, provider)

    def _stored_method(self, provider):
        path = self.profile(provider) / "credential.json"
        if provider in {"agy", "grok"}:
            return "api_key" if path.exists() else "subscription" if provider == "grok" else "none"
        if not path.exists():
            return "unknown"
        try:
            method = json.loads(path.read_text()).get("method", "unknown")
        except (OSError, ValueError):
            return "unknown"
        return method if method in {"api_key", "oauth_token"} else "unknown"

    def _validate_secret(self, provider, method, secret):
        if not secret or len(secret) > 16384 or "\n" in secret or "\r" in secret:
            raise ProviderError("登入資訊格式無效")
        if provider in {"agy", "grok"} and method != "api_key":
            raise ProviderError("請使用 API key，或此 provider 提供的 device code 登入")
        if provider == "codex" and method != "api_key":
            raise ProviderError("Codex 請使用 device code 或 API key")
        if provider == "claude" and method not in {"api_key", "oauth_token"}:
            raise ProviderError("Claude 支援 API key 或官方 setup-token")

    def _drop_secret(self, provider):
        profile = self.profile(provider)
        for name in ("credential.json", "verified", "auth.json"):
            (profile / name).unlink(missing_ok=True)

    def _note_secret_failure(self, provider, exc):
        if isinstance(exc, StopUnconfirmed):
            return
        category = auth_category(exc)
        # A timeout or cancellation did not prove the key invalid, so keep it.
        if category in REJECTED_SECRET:
            self._drop_secret(provider)
        if category:
            self.reasons[provider] = category

    def _write_secret(self, provider, method, secret):
        path = self.profile(provider) / "credential.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump({"method": method, "value": secret}, file)
            file.flush()
            os.fsync(file.fileno())
        if os.getuid() == 0:
            os.chown(path, self.uid(provider), self.uid(provider))
        self.private_files(provider)

    async def _perform_secret(self, provider, method, secret, auth_id=None):
        self._validate_secret(provider, method, secret)
        state = self.auth_sessions.get(auth_id) if auth_id else None
        if state and state.get("_cancel"):
            raise AuthCancelled()
        if provider == "codex":
            proc = await self.spawn(provider, ["login", "--with-api-key"])
            proc._agentic_auth = auth_id
            if state is not None:
                state["_proc"] = proc
                if state.get("_cancel"):
                    await self.stop(proc)
                    raise AuthCancelled()
            if auth_id:
                self.processes[auth_id] = proc
            try:
                out, err = await self.capture(proc, (secret + "\n").encode(), timeout=SECRET_VERIFY_TIMEOUT)
            finally:
                if auth_id and not self.unconfirmed:
                    self.processes.pop(auth_id, None)
            if state and state.get("_cancel"):
                raise AuthCancelled()
            if proc.returncode:
                category = classify_cli_output(out, err)
                raise auth_error("invalid_credentials" if category == "cli_output_unrecognized" else category)
            self.private_files(provider)
            return
        self._write_secret(provider, method, secret)
        # The previous marker must not outlive an unfinished replacement.
        (self.profile(provider) / "verified").unlink(missing_ok=True)
        if state and state.get("_cancel"):
            raise AuthCancelled()
        # Claude setup-token and API keys are checked with one real completion.
        # `auth status` alone can still report a revoked token as logged in.
        await self.run(auth_id or f"auth{time.time_ns():x}", provider, "Reply with status ok.", {
            "type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"], "additionalProperties": False}, timeout=SECRET_VERIFY_TIMEOUT, auth_id=auth_id)
        if state and state.get("_cancel"):
            raise AuthCancelled()
        if provider in {"agy", "grok"}:
            verified = self.profile(provider) / "verified"
            verified.touch(mode=0o600)
            if os.getuid() == 0:
                os.chown(verified, self.uid(provider), self.uid(provider))
        self.private_files(provider)

    async def secret_login(self, provider, method, secret):
        """Synchronous verification for in-process callers. HTTP login uses begin_secret_auth."""
        async with self.auth_lock:
            self.ensure_available()
            self._validate_secret(provider, method, secret)
            if self.auth_in_progress(provider):
                raise ProviderError("已有登入流程正在進行")
        try:
            await self._perform_secret(provider, method, secret)
        except AuthCancelled:
            raise ProviderError("登入已取消") from None
        except (ProviderError, TimeoutError, OSError, ValueError) as exc:
            self._note_secret_failure(provider, exc)
            raise
        self.reasons.pop(provider, None)
        return await self.health(provider)

    async def begin_secret_auth(self, id, provider, method, secret):
        async with self.auth_lock:
            self.ensure_available()
            self._validate_secret(provider, method, secret)
            if self.auth_in_progress(provider):
                raise ProviderError("已有登入流程正在進行")
            state = {"id": id, "provider": provider, "status": "verifying", "url": None, "code": None,
                     "expires_in": SECRET_VERIFY_TIMEOUT, "_expires": time.time() + SECRET_VERIFY_TIMEOUT, "reason": None}
            self.auth_sessions[id] = state
            state["_task"] = asyncio.create_task(self._finish_secret(state, provider, method, secret))
            return self.public_auth(state)

    async def _finish_secret(self, state, provider, method, secret):
        outcome, reason = "failed", "cli_output_unrecognized"
        try:
            await self._perform_secret(provider, method, secret, auth_id=state["id"])
            outcome, reason = "connected", None
        except asyncio.CancelledError:
            outcome, reason = "unverified", None
            raise
        except (AuthCancelled, StopUnconfirmed) as exc:
            outcome, reason = ("stop_unconfirmed", None) if isinstance(exc, StopUnconfirmed) else ("unverified", None)
        except (ProviderError, TimeoutError, OSError, ValueError) as exc:
            category = auth_category(exc) or "cli_output_unrecognized"
            if category in REJECTED_SECRET and not state.get("_cancel"):
                self._drop_secret(provider)
            if state.get("_cancel") or category == "timeout":
                outcome, reason = "unverified", None if state.get("_cancel") else "timeout"
            else:
                outcome, reason = "failed", category
            if reason:
                self.reasons[provider] = reason
        finally:
            if self.unconfirmed or outcome == "stop_unconfirmed":
                state["status"] = "stop_unconfirmed"
            elif state.get("_cancel") and outcome != "connected":
                state["status"] = "unverified"
                state["reason"] = None
            else:
                state["status"] = outcome
                state["reason"] = reason
                if outcome == "connected":
                    self.reasons.pop(provider, None)
            state["code"] = None
            if not self.unconfirmed:
                self.processes.pop(state["id"], None)

    async def begin_device_auth(self, id, provider="codex"):
        async with self.auth_lock:
            self.ensure_available()
            if provider not in {"codex", "grok"}:
                raise ProviderError("此 provider 未提供 device code 登入")
            if self.auth_in_progress(provider):
                raise ProviderError("已有登入流程正在進行")
            duration = 1800 if provider == "grok" else 900
            state = {"id": id, "provider": provider, "status": "pending", "url": None, "code": None,
                     "expires_in": duration, "_expires": time.time() + duration, "reason": None}
            self.auth_sessions[id] = state
            args = ["login", "--device-auth"]
            if provider == "codex":
                args += ["-c", 'cli_auth_credentials_store="file"']
            else:
                # Pinned Grok package: `login --device-auth` is the adapter argv.
                # providers.lock.json cloud_login is not_run; this is not a live check.
                (self.profile(provider) / "credential.json").unlink(missing_ok=True)
                (self.profile(provider) / "verified").unlink(missing_ok=True)
            try:
                proc = await self.spawn(provider, args)
            except StopUnconfirmed:
                state["status"] = "stop_unconfirmed"
                raise
            except (ProviderError, OSError) as exc:
                state["status"] = "failed"
                state["reason"] = auth_category(exc) or "cli_output_unrecognized"
                raise
            proc._agentic_auth = id
            proc._agentic_provider = provider
            state["_proc"] = proc
            try:
                self.track_process(proc, provider)
            except StopUnconfirmed:
                state["status"] = "stop_unconfirmed"
                raise
            self.processes[id] = proc
            state["_task"] = asyncio.create_task(self._finish_device(state, proc, duration))
            return self.public_auth(state)

    async def _finish_device(self, state, proc, duration):
        provider = state["provider"]

        async def consume(stream):
            text = ""
            while chunk := await stream.read(256):
                text = (text + chunk.decode(errors="replace"))[-8192:]
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
                url = re.search(r"https://auth\.openai\.com/[A-Za-z0-9/_-]+" if provider == "codex" else
                                r"https://(?:auth|accounts)\.x\.ai/[^\s\x1b]+", clean)
                code = re.search(rf"\b{AUTH_CODE.pattern}\b", clean)
                if url and OFFICIAL_AUTH_URL.fullmatch(url[0]):
                    state["url"] = url[0]
                if code:
                    state["code"] = code[0]

        outcome = "failed"
        try:
            async with asyncio.timeout(duration):
                await asyncio.gather(consume(proc.stdout), consume(proc.stderr))
                await proc.wait()
            outcome = "connected" if proc.returncode == 0 else "failed"
        except TimeoutError:
            outcome = "expired"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            try:
                await self.stop(proc)
            except StopUnconfirmed:
                state["status"] = "stop_unconfirmed"
            else:
                if state.get("_cancel") and outcome != "connected":
                    outcome = "cancelled"
                if state["status"] != "stop_unconfirmed":
                    state["status"] = outcome
                    if provider == "grok" and outcome == "connected":
                        (self.profile(provider) / "verified").touch(mode=0o600)
                    self.private_files(provider)
                    self.processes.pop(state["id"], None)
            state["code"] = None

    async def _stop_session(self, state):
        seen = []
        proc = state.get("_proc") or self.processes.get(state["id"])
        if proc is not None:
            seen.append(proc)
        for candidate in list(self.processes.values()):
            if getattr(candidate, "_agentic_auth", None) == state["id"] and candidate not in seen:
                seen.append(candidate)
        for candidate in seen:
            await self.stop(candidate)

    async def cancel_auth(self, provider, id):
        async with self.auth_lock:
            state = self.auth_sessions.get(id)
            if not state or state.get("provider") != provider:
                return None
            if state["status"] not in {"pending", "verifying"}:
                return self.public_auth(state)
            state["_cancel"] = True
            task = state.get("_task")
        for _ in range(200):
            if state.get("_proc") or self.processes.get(id) or task is None or task.done():
                break
            await asyncio.sleep(0.01)
        try:
            await self._stop_session(state)
        except StopUnconfirmed:
            # Keep the fence. A later successful stop must not look like a clean cancel.
            state["status"] = "stop_unconfirmed"
            state["code"] = None
        if task and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        if self.unconfirmed or state["status"] == "stop_unconfirmed":
            state["status"] = "stop_unconfirmed"
            state["code"] = None
        elif state["status"] in {"pending", "verifying"}:
            state["status"] = "cancelled" if state["status"] == "pending" else "unverified"
            state["code"] = None
            state["reason"] = None
            self.processes.pop(id, None)
        return self.public_auth(state)

    async def logout(self, provider):
        async with self.auth_lock:
            self.ensure_available()
            tasks = []
            for state in self.auth_sessions.values():
                if state.get("provider") != provider:
                    continue
                if state.get("status") in {"pending", "verifying"}:
                    state["_cancel"] = True
                task = state.get("_task")
                if task and not task.done():
                    tasks.append(task)
            for proc in list(self.processes.values()):
                if getattr(proc, "_agentic_provider", None) == provider:
                    await self.stop(proc)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.reasons.pop(provider, None)
            if provider != "agy":
                proc = await self.spawn(provider, ["logout"] if provider in {"codex", "grok"} else ["auth", "logout"])
                self.track_process(proc, provider)
                await self.capture(proc, timeout=20)
            profile = self.profile(provider)
            # Fixed, app-owned path only. Erase local auth even if remote revocation was unavailable.
            for path in profile.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            for state in self.auth_sessions.values():
                if state.get("provider") == provider and state.get("status") in {"pending", "verifying"}:
                    state["status"] = "cancelled"
                    state["code"] = None
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

    async def run(self, id, provider, prompt, schema, model="", timeout=300, auth_id=None):
        session = self.auth_sessions.get(auth_id) if auth_id else None
        if session and session.get("_cancel"):
            raise AuthCancelled()
        self.ensure_available()
        probe = await self.probe(provider)
        if session and session.get("_cancel"):
            raise AuthCancelled()
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
            self.track_process(proc, provider)
            if auth_id:
                proc._agentic_auth = auth_id
            if session is not None:
                session["_proc"] = proc
                if session.get("_cancel"):
                    await self.stop(proc)
                    raise AuthCancelled()
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
            out, err = await self.capture(proc, payload, timeout=timeout)
            if session and session.get("_cancel"):
                raise AuthCancelled()
            if metrics.get("memory_limit_exceeded"):
                raise ProviderError("Agent 已達到 1 GiB 記憶體上限，任務已停止")
            if proc.returncode != 0:
                category = classify_cli_output(out, err)
                if category == "cli_output_unrecognized":
                    # An unrelated job failure says nothing about the account.
                    raise ProviderError("CLI 執行失敗；請檢查登入狀態、所選模型與網路")
                self.reasons[provider] = category
                raise auth_error(category)
            self.reasons.pop(provider, None)
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
            if not self.unconfirmed:
                self.processes.pop(id, None)
                self.private_files(provider)
                shutil.rmtree(workspace, ignore_errors=True)
