"""Offline fixtures for official CLI login. No cloud account and no real credential."""
import asyncio
import hashlib
import json
import sys
import time
import uuid

import aiohttp
import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.runner import Runner, RunnerError
from agentic_runner.app import Broker, create_api
from agentic_runner.providers import (
    ProviderError,
    Providers,
    StopUnconfirmed,
    auth_error,
    classify_cli_output,
)


def script_process(code):
    return asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)


def matched_probe(provider, version="1.0.50"):
    return {"provider": provider, "version": version, "version_matches": True,
            "billing": "由所選官方登入方式與帳號方案決定", "hardware_verified": False}


async def close_broker(broker):
    for state in list(broker.providers.auth_sessions.values()):
        task = state.get("_task")
        if task and not task.done() and state.get("status") in {"pending", "verifying"} and not broker.providers.unconfirmed:
            await broker.providers.cancel_auth(state["provider"], state["id"])
    for task in list(broker.tasks.values()):
        if not task.done():
            task.cancel()
    if broker.tasks:
        await asyncio.gather(*broker.tasks.values(), return_exceptions=True)
    broker.db.close()


@pytest.fixture
async def paired(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_HOST_MODE", "1")
    broker = Broker(tmp_path)
    broker.settings({"token_hash": hashlib.sha256(b"fixture-pairing").hexdigest()})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                headers={"Authorization": "Bearer fixture-pairing"}) as client:
        yield broker, client
    await close_broker(broker)


async def test_concurrent_device_start_spawns_one_process(tmp_path, monkeypatch):
    providers = Providers(tmp_path)
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def spawn(provider, args, cwd=None):
        calls.append(provider)
        entered.set()
        await release.wait()
        return await script_process("import time; time.sleep(30)")

    monkeypatch.setattr(providers, "spawn", spawn)
    first = asyncio.create_task(providers.begin_device_auth("a" * 32, "codex"))
    await entered.wait()
    second = asyncio.create_task(providers.begin_device_auth("b" * 32, "codex"))
    await asyncio.sleep(0.05)
    assert calls == ["codex"]
    release.set()
    started = await first
    with pytest.raises(ProviderError, match="已有登入流程正在進行"):
        await second
    assert len(calls) == 1 and started["status"] == "pending"
    await providers.cancel_auth("codex", started["id"])


async def test_pending_login_blocks_only_that_provider(tmp_path, monkeypatch):
    providers = Providers(tmp_path)
    calls = []

    async def spawn(provider, args, cwd=None):
        calls.append((provider, list(args)))
        return await script_process("import time; time.sleep(30)")

    monkeypatch.setattr(providers, "spawn", spawn)
    try:
        codex = await providers.begin_device_auth("c" * 32, "codex")
        grok = await providers.begin_device_auth("d" * 32, "grok")
        assert codex["status"] == grok["status"] == "pending"
        assert [item[0] for item in calls] == ["codex", "grok"]
        assert calls[0][1][:2] == ["login", "--device-auth"]
        assert calls[1][1] == ["login", "--device-auth"]
        with pytest.raises(ProviderError, match="已有登入流程正在進行"):
            await providers.begin_device_auth("e" * 32, "codex")
        assert len(calls) == 2
    finally:
        for state in list(providers.auth_sessions.values()):
            if state["status"] in {"pending", "verifying"}:
                await providers.cancel_auth(state["provider"], state["id"])


async def test_cancel_stops_the_device_process(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def spawn(provider, args, cwd=None):
        return await script_process("import time; time.sleep(30)")

    monkeypatch.setattr(providers, "spawn", spawn)
    started = await providers.begin_device_auth("f" * 32, "codex")
    proc = providers.auth_sessions[started["id"]]["_proc"]
    cancelled = await providers.cancel_auth("codex", started["id"])
    assert cancelled["status"] == "cancelled" and cancelled["code"] is None
    assert proc.returncode is not None and not providers.unconfirmed
    again = await providers.begin_device_auth("g" * 32, "codex")
    await providers.cancel_auth("codex", again["id"])


async def test_unconfirmed_stop_keeps_blocking_new_logins(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def spawn(provider, args, cwd=None):
        return await script_process("import time; time.sleep(30)")

    async def stop(process):
        if process.returncode is None:
            process.kill()
            await process.wait()
        providers.quarantine("fixture_uncertain", getattr(process, "pid", None))
        raise StopUnconfirmed()

    monkeypatch.setattr(providers, "spawn", spawn)
    monkeypatch.setattr(providers, "stop", stop)
    started = await providers.begin_device_auth("h" * 32, "codex")
    view = await providers.cancel_auth("codex", started["id"])
    assert view["status"] == "stop_unconfirmed" and providers.unconfirmed
    with pytest.raises(StopUnconfirmed):
        await providers.begin_device_auth("i" * 32, "grok")


async def test_new_client_sees_the_pending_session(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def probe(provider):
        return matched_probe(provider)

    async def spawn(provider, args, cwd=None):
        assert args == ["login", "--device-auth"]
        return await script_process(
            "import sys,time; print('https://accounts.x.ai/device?user_code=ABCD-EFGH', file=sys.stderr, flush=True);"
            "print('ABCD-EFGH', file=sys.stderr, flush=True); time.sleep(30)")

    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setattr(providers, "spawn", spawn)
    try:
        started = await providers.begin_device_auth("j" * 32, "grok")
        pending = None
        for _ in range(50):
            health = await providers.health("grok")
            pending = health.get("pending_session")
            if pending and pending.get("code"):
                break
            await asyncio.sleep(0.01)
        assert health["status"] == "needs_reauth" and "reason" not in health
        assert pending == {
            "id": started["id"], "provider": "grok", "status": "pending",
            "url": "https://accounts.x.ai/device?user_code=ABCD-EFGH", "code": "ABCD-EFGH",
            "expires_in": pending["expires_in"]}
        assert 0 < pending["expires_in"] <= 1800
    finally:
        await providers.cancel_auth("grok", "j" * 32)


async def test_status_and_cancel_routes_expose_one_pending_session(paired, monkeypatch):
    broker, client = paired

    async def probe(provider):
        return matched_probe(provider)

    async def spawn(provider, args, cwd=None):
        if args[:2] == ["login", "--device-auth"]:
            return await script_process(
                "import sys,time; print('https://accounts.x.ai/device?user_code=ABCD-EFGH', file=sys.stderr, flush=True);"
                "print('ABCD-EFGH', file=sys.stderr, flush=True); time.sleep(30)")
        return await script_process("import time; print('not logged in'); time.sleep(0.3); raise SystemExit(1)")

    monkeypatch.setattr(broker.providers, "probe", probe)
    monkeypatch.setattr(broker.providers, "spawn", spawn)
    started = await client.post("/providers/grok/auth", json={"method": "device_code", "secret": ""})
    assert started.status_code == 200 and started.json()["status"] == "pending"
    pending = None
    for _ in range(50):
        status = await client.get("/status")
        pending = next(item for item in status.json()["providers"] if item["provider"] == "grok").get("pending_session")
        if pending and pending.get("code"):
            break
        await asyncio.sleep(0.01)
    assert pending["id"] == started.json()["id"] and pending["code"] == "ABCD-EFGH"
    fetched = await client.get("/auth/" + started.json()["id"])
    assert fetched.json()["url"].startswith("https://accounts.x.ai/")
    cancelled = await client.post("/providers/grok/auth/" + started.json()["id"] + "/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    missing = await client.post("/providers/grok/auth/" + "z" * 32 + "/cancel")
    assert missing.status_code == 404


async def test_secret_login_returns_202_and_completes(paired, monkeypatch):
    broker, client = paired
    secret = "fixture-agy-key"

    async def probe(provider):
        return matched_probe(provider, "1.3.3")

    async def spawn(provider, args, cwd=None):
        return await script_process(
            "import sys,json; sys.stdin.read(); print(json.dumps({'event':'result','result':"
            "{'status':'SUCCESS','structured_output':{'status':'ok'}}}))")

    monkeypatch.setattr(broker.providers, "probe", probe)
    monkeypatch.setattr(broker.providers, "spawn", spawn)
    response = await client.post("/providers/agy/auth", json={"method": "api_key", "secret": secret})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "verifying" and secret not in response.text
    await broker.providers.auth_sessions[body["id"]]["_task"]
    done = (await client.get("/auth/" + body["id"])).json()
    assert done["status"] == "connected" and secret not in json.dumps(done)
    saved = json.loads((broker.providers.profile("agy") / "credential.json").read_text())
    assert saved["value"] == secret
    assert (broker.providers.profile("agy") / "verified").exists()


async def test_inline_secret_login_stays_synchronous(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def probe(provider):
        return matched_probe(provider, "1.3.3")

    async def spawn(provider, args, cwd=None):
        return await script_process(
            "import sys,json; sys.stdin.read(); print(json.dumps({'event':'result','result':"
            "{'status':'SUCCESS','structured_output':{'status':'ok'}}}))")

    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setattr(providers, "spawn", spawn)
    result = await providers.secret_login("agy", "api_key", "fixture-inline-key")
    assert result["status"] == "connected" and not providers.auth_sessions
    assert (providers.profile("agy") / "verified").exists()


async def test_slow_verification_keeps_the_key_and_reports_unverified(tmp_path, monkeypatch):
    providers = Providers(tmp_path)
    monkeypatch.setattr("agentic_runner.providers.SECRET_VERIFY_TIMEOUT", 0.2)
    secret = "fixture-slow-key"

    async def probe(provider):
        return matched_probe(provider)

    async def spawn(provider, args, cwd=None):
        return await script_process("import time; time.sleep(30)")

    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setattr(providers, "spawn", spawn)
    view = await providers.begin_secret_auth("k" * 32, "grok", "api_key", secret)
    await providers.auth_sessions[view["id"]]["_task"]
    state = providers.auth_sessions[view["id"]]
    assert state["status"] == "unverified" and state["reason"] == "timeout"
    assert json.loads((providers.profile("grok") / "credential.json").read_text())["value"] == secret
    assert not (providers.profile("grok") / "verified").exists() and not providers.unconfirmed
    public = providers.public_auth(state)
    assert public["message"] == "AI 帳號回應逾時，請稍後再試。" and secret not in json.dumps(public)


async def test_invalid_key_is_deleted_and_other_failures_are_kept(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def probe(provider):
        return matched_probe(provider, "1.3.3")

    outputs = {"invalid api key": 1}

    async def spawn(provider, args, cwd=None):
        text = next(iter(outputs))
        return await script_process(
            "import sys,time; print(" + repr(text) + ", file=sys.stderr, flush=True); time.sleep(0.2); raise SystemExit(1)")

    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setattr(providers, "spawn", spawn)
    secret = "bad-fixture-key"
    view = await providers.begin_secret_auth("m" * 32, "agy", "api_key", secret)
    await providers.auth_sessions[view["id"]]["_task"]
    state = providers.auth_sessions[view["id"]]
    assert state["status"] == "failed" and state["reason"] == "invalid_credentials"
    assert not (providers.profile("agy") / "credential.json").exists()
    assert secret not in json.dumps(providers.public_auth(state))

    outputs.clear()
    outputs["connection refused"] = 1
    kept = "fixture-network-key"
    view = await providers.begin_secret_auth("n" * 32, "agy", "api_key", kept)
    await providers.auth_sessions[view["id"]]["_task"]
    assert providers.auth_sessions[view["id"]]["reason"] == "network"
    assert json.loads((providers.profile("agy") / "credential.json").read_text())["value"] == kept

    outputs.clear()
    outputs["token revoked"] = 1
    token = "fixture-setup-token"
    view = await providers.begin_secret_auth("o" * 32, "claude", "oauth_token", token)
    await providers.auth_sessions[view["id"]]["_task"]
    assert providers.auth_sessions[view["id"]]["status"] == "failed"
    assert providers.auth_sessions[view["id"]]["reason"] == "needs_reauth"
    assert not (providers.profile("claude") / "credential.json").exists()
    assert token not in json.dumps(providers.public_auth(providers.auth_sessions[view["id"]]))


async def test_bootstrap_preserves_account_categories(paired, monkeypatch):
    broker, client = paired

    async def health(provider):
        return {"status": "connected", "version": "0.157.1"}

    async def limited(*args, **kwargs):
        raise auth_error("rate_limited")

    monkeypatch.setattr(broker.providers, "health", health)
    monkeypatch.setattr(broker.providers, "run", limited)
    body = {"id": uuid.uuid4().hex, "provider": "codex", "stage": "hermes_setup", "expires": time.time() + 300,
            "observations": {"network_ready": True, "hermes_installed": False, "hermes_auth_state": "needs_auth",
                             "channel": "line", "ha_state": "not_installed"}}
    assert (await client.post("/bootstrap/jobs", json=body)).status_code == 202
    await asyncio.gather(*list(broker.tasks.values()))
    assert (await client.get("/bootstrap/jobs/" + body["id"])).json()["error"] == "rate_limited"

    async def timed_out(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(broker.providers, "run", timed_out)
    body["id"] = uuid.uuid4().hex
    await client.post("/bootstrap/jobs", json=body)
    await asyncio.gather(*list(broker.tasks.values()))
    assert (await client.get("/bootstrap/jobs/" + body["id"])).json()["error"] == "timeout"

    async def logged_out(provider):
        return {"status": "needs_reauth", "version": "0.157.1"}

    monkeypatch.setattr(broker.providers, "health", logged_out)
    body["id"] = uuid.uuid4().hex
    await client.post("/bootstrap/jobs", json=body)
    await asyncio.gather(*list(broker.tasks.values()))
    assert (await client.get("/bootstrap/jobs/" + body["id"])).json()["error"] == "needs_reauth"


class Memory:
    def setting(self, key):
        return {"host": "runner", "fingerprint": "ab" * 32, "port": 8443, "token": "t"}


class Session:
    def __init__(self, *args, **kwargs):
        self.error = TimeoutError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, *args, **kwargs):
        raise self.error()


async def test_timeout_is_distinct_from_the_fingerprint_error(monkeypatch):
    runner = Runner(Memory())
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    with pytest.raises(RunnerError, match="AI 帳號回應逾時，請稍後再試。") as account:
        await runner.call("POST", "/providers/grok/auth", {})
    assert "指紋" not in str(account.value)
    with pytest.raises(RunnerError, match="Runner 回應逾時，請稍後再試。") as other:
        await runner.call("GET", "/jobs/" + "a" * 32)
    assert "指紋" not in str(other.value) and "AI 帳號" not in str(other.value)

    class Broken(Session):
        def __init__(self, *args, **kwargs):
            self.error = aiohttp.ClientError

    monkeypatch.setattr(aiohttp, "ClientSession", Broken)
    with pytest.raises(RunnerError, match="Runner 無法連線或憑證指紋不符，請檢查配對與 App 狀態"):
        await runner.call("POST", "/providers/grok/auth", {})

    class Slow(Session):
        def __init__(self, *args, **kwargs):
            self.error = aiohttp.ServerTimeoutError

    monkeypatch.setattr(aiohttp, "ClientSession", Slow)
    with pytest.raises(RunnerError, match="AI 帳號回應逾時，請稍後再試。"):
        await runner.call("GET", "/auth/" + "a" * 32)


async def test_controller_secret_login_is_202_and_cancel_keeps_owner_guards(store, ha):
    class Fake:
        def __init__(self):
            self.calls = []

        async def call(self, method, path, data=None, pairing=None):
            self.calls.append((method, path, data))
            provider = path.split("/")[2]
            if path.endswith("/cancel"):
                return {"id": path.split("/")[4], "provider": provider, "status": "cancelled",
                        "url": None, "code": None, "expires_in": 0}
            status = "pending" if data and data.get("method") == "device_code" else "verifying"
            return {"id": "abc123", "provider": provider, "status": status, "url": None, "code": None, "expires_in": 120}

    outer, inner = Fake(), Fake()
    app = create_app(store=store, ha=ha, runner=outer, inner_runner=inner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.30.32.2", 1234)),
                                 base_url="http://home.test", headers={"X-Remote-User-Id": "owner"}) as client:
        token = (await client.get("/api/session")).json()["csrf"]
        headers = {"X-Agentic-CSRF": token, "Origin": "http://home.test"}
        secret = "fixture-controller-key"
        denied = await client.post("/api/providers/agy/auth", json={"method": "api_key", "secret": secret})
        assert denied.status_code == 403 and secret not in denied.text
        device = await client.post("/api/providers/grok/auth", json={"method": "device_code", "secret": ""}, headers=headers)
        assert device.status_code == 200 and device.json()["status"] == "pending"
        accepted = await client.post("/api/providers/agy/auth", json={"method": "api_key", "secret": secret}, headers=headers)
        assert accepted.status_code == 202 and accepted.json()["status"] == "verifying"
        assert secret not in accepted.text and outer.calls[-1][2]["secret"] == secret
        cancelled = await client.post("/api/providers/agy/auth/abc123/cancel", headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        runtime = await client.post("/api/runtimes/haos/providers/claude/auth/abc123/cancel", headers=headers)
        assert runtime.status_code == 200
        assert inner.calls[-1] == ("POST", "/providers/claude/auth/abc123/cancel", None)
        assert outer.calls[-1][1] == "/providers/agy/auth/abc123/cancel"
        assert (await client.post("/api/providers/agy/auth/not-valid/cancel", headers=headers)).status_code == 422


@pytest.mark.parametrize(("text", "category"), [
    ("HTTP 401 from provider", "needs_reauth"),
    ('{"status": 429}', "rate_limited"),
    ("context used 140100 tokens; output did not match the schema", "cli_output_unrecognized"),
    ("trace 0a4012c9 request req_8a429b failed", "cli_output_unrecognized"),
    ("version 1.401.2 exited", "cli_output_unrecognized"),
])
def test_status_numbers_must_stand_alone(text, category):
    assert classify_cli_output("", text) == category


async def test_unrelated_run_failure_is_not_an_account_reason(tmp_path, monkeypatch):
    providers = Providers(tmp_path)

    async def probe(provider):
        return matched_probe(provider, "1.3.3")

    async def spawn(provider, args, cwd=None):
        return await script_process(
            "import sys; print('model returned 140100 tokens without a final answer', file=sys.stderr); raise SystemExit(1)")

    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setattr(providers, "spawn", spawn)
    schema = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}
    with pytest.raises(ProviderError) as failed:
        await providers.run("p" * 32, "agy", "Reply with status ok.", schema)
    assert str(failed.value) == "CLI 執行失敗；請檢查登入狀態、所選模型與網路"
    assert "agy" not in providers.reasons
