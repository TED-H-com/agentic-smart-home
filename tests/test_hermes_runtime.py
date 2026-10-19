"""Service-boundary tests plus real pinned Hermes with protocol fixtures.

No cloud account, physical device or real HAOS is used by these tests.
"""

import asyncio
import base64
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from agentic_hermes.app import Runtime, create_app, process_info, signal_members
from agentic_hermes.child import guarded_dispatch
from agentic_hermes.models import TOOLS, RetentionInput

TOKEN = "runtime-service-" + "a" * 40
SCOPE = "a" * 64
OTHER_SCOPE = "b" * 64
MCP_TOKEN = "fixture-capability-" + "c" * 40
API_KEY = "fixture-api-key-" + "d" * 40
CODEX_HEADER = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
CODEX_TOKEN = CODEX_HEADER + "." + base64.urlsafe_b64encode(json.dumps({
    "exp": 4102444800, "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account"},
}).encode()).rstrip(b"=").decode() + ".AA"


def source_mounts():
    """Opt-in development evidence; default container tests inspect packaged code."""
    if os.environ.get("AGENTIC_HERMES_TEST_SOURCE") != "1":
        return []
    root = Path(__file__).parents[1] / "hermes"
    return ["-v", f"{root / 'agentic_hermes'}:/app/agentic_hermes:ro",
            "-v", f"{root / 'compatibility_probe.py'}:/app/compatibility_probe.py:ro"]


@pytest.fixture
async def runtime(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "run_agent.py").write_text("# mock process host, never upstream evidence\n")
    service = Runtime(tmp_path / "data", TOKEN, upstream=upstream)
    yield service
    await service.close()


@pytest.fixture
async def client(runtime):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://runtime", headers={"Authorization": "Bearer " + TOKEN}) as value:
        yield value


async def configure(client):
    response = await client.post("/v1/auth", json={"provider": "openrouter", "model": "fixture/model", "api_key": API_KEY})
    assert response.status_code == 200
    assert response.json()["state"] == "configured"
    assert API_KEY not in response.text


async def session(client, key="session1", scope=SCOPE):
    response = await client.post("/v1/sessions", json={"scope_id": scope}, headers={"Idempotency-Key": key})
    assert response.status_code == 200
    return response.json()["session_id"]


def request_body(**values):
    return {"run_id": uuid.uuid4().hex, "scope_id": SCOPE, "case_id": "fixture-case", "message": "查主機狀態", "mcp_token": MCP_TOKEN, **values}


async def drain(runtime):
    await asyncio.gather(*list(runtime.tasks.values()), return_exceptions=True)
    await asyncio.sleep(0)


async def test_native_oauth_is_separate_idempotent_and_requires_real_run(client, runtime, monkeypatch):
    await configure(client)
    old_session = await session(client)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def oauth_fixture(item, home):
        calls.append(item["auth_id"])
        runtime.device_login.update(item, "pending", verification_uri="https://auth.openai.com/codex/device", user_code="ABCD-EFGH")
        entered.set()
        await release.wait()
        (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}}))
        return "configured"

    monkeypatch.setattr(runtime.device_login, "invoke", oauth_fixture)
    body = {"provider": "openai-codex", "model": "gpt-6-astra"}
    response = await client.post("/v1/auth/device", json=body, headers={"Idempotency-Key": "native-login"})
    assert response.status_code == 202
    identifier = response.json()["auth_id"]
    await entered.wait()
    retry = await client.post("/v1/auth/device", json=body, headers={"Idempotency-Key": "native-login"})
    assert retry.json()["auth_id"] == identifier and len(calls) == 1
    status = await client.get("/v1/auth/device/" + identifier)
    assert status.json()["user_code"] == "ABCD-EFGH"
    assert runtime.auth["state"] == "needs_auth" and "api_key" not in runtime.auth
    assert runtime.get("session", old_session) is None
    assert "CODEX_HOME" not in runtime.child_environment(runtime.directory / "fixture")
    release.set()
    await drain(runtime)
    status = await client.get("/v1/auth/device/" + identifier)
    assert status.json()["state"] == "configured" and status.json()["user_code"] is None
    assert runtime.capabilities()["auth_verified_at"] is None
    assert "synthetic-access" not in status.text + runtime.auth_path.read_text()
    assert "synthetic-refresh" not in runtime.db.execute("SELECT group_concat(body) FROM records").fetchone()[0]

    async def inference_fixture(payload, scratch, identifier):
        assert payload["provider"] == "openai-codex" and payload["api_key"] == ""
        return {"answer": "synthetic inference", "history": []}

    monkeypatch.setattr(runtime, "invoke", inference_fixture)
    sid = await session(client, key="new-native-session")
    await client.post(f"/v1/sessions/{sid}/runs", json=request_body(), headers={"Idempotency-Key": "native-run"})
    await drain(runtime)
    assert runtime.auth["state"] == "ready" and runtime.auth["verified_at"]
    await client.post("/v1/auth/logout")
    assert not (runtime.directory / "oauth").exists()
    assert (await client.get("/v1/auth/device/" + identifier)).json()["state"] == "interrupted"


async def test_native_oauth_cancel_fences_before_erasing_store(client, runtime, monkeypatch):
    entered = asyncio.Event()

    async def fixture(item, home):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.device_login, "invoke", fixture)
    response = await client.post("/v1/auth/device", json={"provider": "openai-codex", "model": "gpt-6-astra"}, headers={"Idempotency-Key": "cancel-login"})
    identifier = response.json()["auth_id"]
    await entered.wait()
    response = await client.post(f"/v1/auth/device/{identifier}/cancel")
    assert response.json()["state"] == "cancelled"
    assert not list((runtime.directory / "oauth").iterdir())
    assert runtime.auth["state"] == "needs_auth"


async def test_native_oauth_uncertain_stop_quarantines_login_and_store(client, runtime, monkeypatch):
    entered = asyncio.Event()

    async def fixture(item, home):
        entered.set()
        await asyncio.Event().wait()

    async def unknown_stop(identifier):
        raise TimeoutError("fixture unknown stop")

    monkeypatch.setattr(runtime.device_login, "invoke", fixture)
    monkeypatch.setattr(runtime, "stop_process", unknown_stop)
    response = await client.post("/v1/auth/device", json={"provider": "openai-codex", "model": "gpt-6-astra"}, headers={"Idempotency-Key": "unknown-login"})
    identifier = response.json()["auth_id"]
    await entered.wait()
    assert (await client.post(f"/v1/auth/device/{identifier}/cancel")).json()["state"] == "stop_unconfirmed"
    assert runtime.capabilities()["execution_state"] == "stop_unconfirmed"
    assert (await client.post("/v1/auth/logout")).status_code == 409
    assert (runtime.directory / "oauth" / runtime.auth["epoch"]).is_dir()
    await runtime.close()
    restarted = Runtime(runtime.directory, TOKEN, upstream=runtime.upstream)
    try:
        assert identifier in restarted.unconfirmed
    finally:
        await restarted.close()
    runtime.unconfirmed.clear()


async def test_runtime_auth_and_no_secret_echo(client, runtime):
    response = await client.get("/v1/capabilities", headers={"Authorization": "Bearer incorrect"})
    assert response.status_code == 401
    assert (await client.get("/v1/capabilities", headers={"Origin": "http://evil"})).status_code == 401
    assert (await client.get("/v1/capabilities")).json()["state"] == "needs_auth"
    assert (await client.get("/v1/capabilities")).json()["execution_state"] == "idle"
    sid = await session(client)
    response = await client.post(f"/v1/sessions/{sid}/runs", json=request_body(), headers={"Idempotency-Key": "first"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "needs_auth"
    malformed = await client.post("/v1/auth", json={"provider": "root", "model": "x", "api_key": API_KEY})
    assert malformed.status_code == 422 and API_KEY not in malformed.text
    await configure(client)
    assert runtime.auth_path.stat().st_mode & 0o777 == 0o600
    assert (await client.get("/v1/capabilities")).json()["auth_verified_at"] is None


async def test_runtime_session_idempotency_scope_and_history(client, runtime, monkeypatch):
    await configure(client)
    sid = await session(client)
    assert await session(client) == sid
    seen = []

    async def fixture(payload, scratch, identifier):
        seen.append(payload)
        return {"answer": "fixture answer", "history": payload["history"] + [{"role": "user", "content": payload["message"]}, {"role": "assistant", "content": "fixture answer"}], "usage": None}

    monkeypatch.setattr(runtime, "invoke", fixture)
    body = request_body()
    response = await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "first"})
    assert response.status_code == 202 and response.json()["state"] == "queued"
    await drain(runtime)
    same = await client.post(f"/v1/sessions/{sid}/runs", json={**body, "mcp_token": "rotated-" + "t" * 40}, headers={"Idempotency-Key": "first"})
    assert same.json()["run_id"] == body["run_id"] and len(seen) == 1
    conflict = await client.post(f"/v1/sessions/{sid}/runs", json={**body, "message": "different"}, headers={"Idempotency-Key": "first"})
    assert conflict.status_code == 409
    assert (await client.get(f"/v1/runs/{body['run_id']}?scope_id={OTHER_SCOPE}")).status_code == 403
    cross = await client.post(f"/v1/sessions/{sid}/runs", json=request_body(scope_id=OTHER_SCOPE), headers={"Idempotency-Key": "cross"})
    assert cross.status_code == 403
    follow = request_body(message="接著查")
    assert (await client.post(f"/v1/sessions/{sid}/runs", json=follow, headers={"Idempotency-Key": "follow"})).status_code == 202
    await drain(runtime)
    assert len(seen[-1]["history"]) == 2
    events = (await client.get(f"/v1/runs/{body['run_id']}/events?scope_id={SCOPE}")).json()
    assert [row["state"] for row in events["events"]] == ["queued", "running", "succeeded"]
    encoded = json.dumps(runtime.db.execute("SELECT * FROM records").fetchall())
    assert TOKEN not in encoded and MCP_TOKEN not in encoded and API_KEY not in encoded
    assert (await client.get("/v1/capabilities")).json()["state"] == "ready"


@pytest.mark.parametrize("revoke", ["logout", "retention"])
async def test_runtime_cancel_fences_before_account_change(client, runtime, monkeypatch, revoke):
    await configure(client)
    sid = await session(client)
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def fixture(payload, scratch, identifier):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(runtime, "invoke", fixture)
    body = request_body()
    await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "first"})
    await entered.wait()
    busy = await client.post(f"/v1/sessions/{sid}/runs", json=request_body(), headers={"Idempotency-Key": "second"})
    assert busy.status_code == 409
    if revoke == "retention":
        record = runtime.get("session", sid)
        record["created_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        runtime.put("session", sid, record)
        assert (await client.post("/v1/retention", json={"context_days": 1})).status_code == 200
    else:
        await client.post("/v1/auth/logout", json={})
    assert stopped.is_set()
    result = (await client.get(f"/v1/runs/{body['run_id']}?scope_id={SCOPE}")).json()
    assert result["state"] == "cancelled"
    assert runtime.get("session", sid) is None
    assert runtime.auth == {} if revoke == "logout" else runtime.context_days == 1
    assert not list((runtime.directory / "turns").iterdir())


async def test_runtime_restart_never_replays_and_environment_has_no_ambient_secrets(tmp_path, monkeypatch):
    directory = tmp_path / "runtime"
    first = Runtime(directory, TOKEN)
    await first.set_retention(RetentionInput(context_days=1))
    run = {"run_id": uuid.uuid4().hex, "scope_id": SCOPE, "state": "running", "event_cursor": 0}
    first.put("run", run["run_id"], run)
    await first.close()
    second = Runtime(directory, TOKEN)
    try:
        assert second.context_days == 1
        assert second.get("run", run["run_id"])["state"] == "interrupted"
        assert not second.tasks
        monkeypatch.setenv("SUPERVISOR_TOKEN", "must-not-inherit")
        monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
        env = second.child_environment(tmp_path / "scratch")
        assert "SUPERVISOR_TOKEN" not in env and "OPENAI_API_KEY" not in env
        assert "HTTP_PROXY" not in env and "SSH_AUTH_SOCK" not in env
    finally:
        await second.close()


async def test_runtime_context_expires_even_when_recently_used(client, runtime):
    await configure(client)
    sid = await session(client)
    record = runtime.get("session", sid)
    record.update(created_at="2000-01-01T00:00:00+00:00", updated_at="2099-01-01T00:00:00+00:00", history=[{"role": "user", "content": "expired private details"}])
    runtime.put("session", sid, record)
    # Existing tombstone stops automatic recreation of the same conversation.
    response = await client.post("/v1/sessions", json={"scope_id": SCOPE}, headers={"Idempotency-Key": "session1"})
    assert response.status_code == 410
    assert runtime.get("session", sid) is None


async def test_runtime_reducing_retention_deletes_history_and_preserves_tombstones(client, runtime, monkeypatch):
    await configure(client)
    sid = await session(client)

    async def fixture(payload, scratch, identifier):
        return {"answer": "private prior answer", "history": [{"role": "user", "content": payload["message"]}], "usage": None}

    monkeypatch.setattr(runtime, "invoke", fixture)
    body = request_body()
    await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "shorten"})
    await drain(runtime)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    for kind, identifier in (("session", sid), ("run", body["run_id"])):
        record = runtime.get(kind, identifier)
        runtime.put(kind, identifier, {**record, "created_at": old})
    assert (await client.get(f"/v1/runs/{body['run_id']}?scope_id={SCOPE}")).status_code == 200
    for value in (0, 8, True, "1"):
        assert (await client.post("/v1/retention", json={"context_days": value})).status_code == 422
    response = await client.post("/v1/retention", json={"context_days": 1})
    assert response.status_code == 200 and response.json() == {"context_days": 1}
    assert runtime.get("session", sid) is None and runtime.get("run", body["run_id"]) is None
    assert not runtime.db.execute("SELECT body FROM events WHERE run=?", (body["run_id"],)).fetchall()
    assert runtime.get("setting", "retention") == {"context_days": 1}
    assert (await client.get("/v1/capabilities")).json()["context_days"] == 1
    assert (await client.post("/v1/retention", json={"context_days": 7})).status_code == 200
    retry = await client.post("/v1/sessions", json={"scope_id": SCOPE}, headers={"Idempotency-Key": "session1"})
    assert retry.status_code == 410
    assert runtime.db.execute("SELECT id FROM requests WHERE kind='run' AND key='shorten'").fetchone()[0] == body["run_id"]
    assert (await client.get(f"/v1/runs/{body['run_id']}?scope_id={SCOPE}")).status_code == 404


async def test_runtime_stops_real_subprocess_before_cancelled(client, runtime, monkeypatch):
    await configure(client)
    sid = await session(client)
    started = asyncio.Event()
    processes = []

    async def sleeper(payload, scratch, identifier):
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
        processes.append(proc)
        runtime.track_process(identifier, proc)
        started.set()
        await proc.wait()
        return {"answer": "must not finish", "history": []}

    monkeypatch.setattr(runtime, "invoke", sleeper)
    body = request_body()
    await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "real-cancel"})
    await started.wait()
    response = await client.post(f"/v1/runs/{body['run_id']}/cancel", json={"scope_id": SCOPE})
    assert response.json()["state"] == "cancelled"
    assert processes[0].returncode is not None
    assert not runtime.processes


async def test_runtime_kills_term_ignoring_descendant_after_leader_exit(runtime, tmp_path):
    child_path = tmp_path / "descendant.pid"
    source = (
        "import os,signal,time\n"
        "from pathlib import Path\n"
        "if os.fork() == 0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f" child_path = Path({str(child_path)!r})\n"
        " pending_path = child_path.with_suffix('.pending')\n"
        " pending_path.write_text(str(os.getpid()))\n"
        " pending_path.replace(child_path)\n"
        "while True: time.sleep(1)\n"
    )
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", source, start_new_session=True)
    identifier = uuid.uuid4().hex
    runtime.track_process(identifier, proc)
    child_pid, child_started = None, None
    try:
        for _ in range(100):
            if child_path.exists():
                break
            await asyncio.sleep(0.01)
        child_pid = int(child_path.read_text())
        child_started = process_info(child_pid)["start"]
        await runtime.stop_process(identifier)
        assert proc.returncode is not None
        info = process_info(child_pid)
        assert info is None or info["state"] in {"Z", "X"}
        assert identifier not in runtime.processes and identifier not in runtime.groups
    finally:
        if child_pid and child_started:
            signal_members({child_pid: child_started}, signal.SIGKILL)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_unconfirmed_stop_blocks_replacement_and_survives_restart(client, runtime, monkeypatch):
    await configure(client)
    sid = await session(client)
    entered = asyncio.Event()

    async def fixture(payload, scratch, identifier):
        entered.set()
        await asyncio.Event().wait()

    async def cannot_confirm(identifier):
        raise TimeoutError("stop outcome intentionally unknown")

    monkeypatch.setattr(runtime, "invoke", fixture)
    monkeypatch.setattr(runtime, "stop_process", cannot_confirm)
    body = request_body()
    await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "unknown-stop"})
    await entered.wait()
    assert (await client.get("/v1/capabilities")).json()["execution_state"] == "busy"
    response = await client.post(f"/v1/runs/{body['run_id']}/cancel", json={"scope_id": SCOPE})
    assert response.json()["state"] == "stop_unconfirmed"
    assert body["run_id"] in runtime.unconfirmed
    await drain(runtime)
    assert not runtime.tasks
    capabilities = (await client.get("/v1/capabilities")).json()
    assert capabilities["execution_state"] == "stop_unconfirmed"
    assert capabilities["state"] == "configured"
    assert (await client.post(f"/v1/sessions/{sid}/runs", json=request_body(), headers={"Idempotency-Key": "replacement"})).status_code == 409
    for path, payload in (("/v1/auth/logout", {}), ("/v1/auth", {"provider": "openrouter", "model": "other", "api_key": API_KEY}), ("/v1/retention", {"context_days": 1})):
        blocked = await client.post(path, json=payload)
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "stop_unconfirmed"
    assert runtime.auth["model"] == "fixture/model" and runtime.context_days == 7
    await runtime.close()
    restarted = Runtime(runtime.directory, TOKEN, upstream=runtime.upstream)
    try:
        assert body["run_id"] in restarted.unconfirmed
        assert restarted.get("run", body["run_id"])["state"] == "stop_unconfirmed"
        assert restarted.capabilities()["execution_state"] == "stop_unconfirmed"
        assert (restarted.directory / "turns" / body["run_id"]).is_dir()
    finally:
        await restarted.close()
    # The shared fixture closes this Runtime once more; make that harmless.
    runtime.unconfirmed.clear()


async def _stamp_unconfirmed(client, runtime, monkeypatch, identity):
    """Leave one auth item and one run at stop_unconfirmed, each stamped with identity."""
    monkeypatch.setattr("agentic_hermes.app.current_instance", lambda: dict(identity))
    entered = asyncio.Event()

    async def oauth_fixture(item, home):
        entered.set()
        await asyncio.Event().wait()

    async def unknown_stop(identifier):
        raise TimeoutError("fixture unknown stop")

    monkeypatch.setattr(runtime.device_login, "invoke", oauth_fixture)
    monkeypatch.setattr(runtime, "stop_process", unknown_stop)
    response = await client.post(
        "/v1/auth/device", json={"provider": "openai-codex", "model": "gpt-6-astra"},
        headers={"Idempotency-Key": "restart-login"})
    assert response.status_code == 202, response.text
    auth_id = response.json()["auth_id"]
    await entered.wait()
    cancelled = await client.post(f"/v1/auth/device/{auth_id}/cancel")
    assert cancelled.json()["state"] == "stop_unconfirmed" and "instance" not in cancelled.json()
    assert runtime.get("auth", auth_id)["instance"] == identity
    (runtime.directory / "turns" / auth_id).mkdir(exist_ok=True)
    run_id = uuid.uuid4().hex
    runtime.put("run", run_id, {
        "run_id": run_id, "session_id": "s" * 32, "case_id": "fixture-case", "scope_id": SCOPE,
        "state": "stop_unconfirmed", "error": "stop_unconfirmed", "answer": "",
        "created_at": datetime.now(UTC).isoformat(), "updated_at": datetime.now(UTC).isoformat(),
        "event_cursor": 0, "usage": None, "instance": dict(identity),
    })
    (runtime.directory / "turns" / run_id).mkdir()
    return auth_id, run_id


def _restart_client(restarted):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(restarted)), base_url="http://runtime",
        headers={"Authorization": "Bearer " + TOKEN})


async def test_new_namespace_interrupts_unconfirmed_stops_and_accepts_a_run(client, runtime, monkeypatch):
    identity = {"boot_id": "boot-old", "pid_namespace": "pid:[111]"}
    auth_id, run_id = await _stamp_unconfirmed(client, runtime, monkeypatch, identity)
    # This run differs only by boot id once the namespace moves to pid:[222].
    runtime.put("run", run_id, {**runtime.get("run", run_id), "instance": {
        "boot_id": "boot-other", "pid_namespace": "pid:[222]"}})
    assert runtime._instance_held
    assert fcntl.fcntl(runtime._instance_fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    await runtime.close()
    await runtime.close()
    identity["pid_namespace"] = "pid:[222]"
    restarted = Runtime(runtime.directory, TOKEN, upstream=runtime.upstream)
    try:
        assert not restarted.unconfirmed
        auth, run = restarted.get("auth", auth_id), restarted.get("run", run_id)
        assert auth["state"] == "interrupted" and auth["error"] == "runtime_restarted"
        assert run["state"] == "interrupted" and run["error"] == "runtime_restarted"
        assert auth["instance"]["pid_namespace"] == "pid:[111]"
        assert run["instance"]["boot_id"] == "boot-other"
        assert not (restarted.directory / "turns" / auth_id).exists()
        assert not (restarted.directory / "turns" / run_id).exists()
        async with _restart_client(restarted) as restarted_client:
            auth_view = await restarted_client.get("/v1/auth/device/" + auth_id)
            run_view = await restarted_client.get(f"/v1/runs/{run_id}?scope_id={SCOPE}")
            assert auth_view.json()["state"] == "interrupted" and "instance" not in auth_view.json()
            assert run_view.json()["state"] == "interrupted" and "instance" not in run_view.json()
            assert restarted.capabilities()["execution_state"] == "idle"

            async def answer(payload, scratch, identifier):
                return {"answer": "已恢復", "history": []}

            monkeypatch.setattr(restarted, "invoke", answer)
            await configure(restarted_client)
            sid = await session(restarted_client, key="after-restart")
            accepted = await restarted_client.post(
                f"/v1/sessions/{sid}/runs", json=request_body(), headers={"Idempotency-Key": "resumed"})
            assert accepted.status_code == 202, accepted.text
            await drain(restarted)
            assert restarted.get("run", accepted.json()["run_id"])["state"] == "succeeded"
            assert not restarted.unconfirmed
    finally:
        await restarted.close()
        await restarted.close()
    runtime.unconfirmed.clear()


async def test_same_namespace_keeps_unconfirmed_stops(client, runtime, monkeypatch):
    identity = {"boot_id": "boot-same", "pid_namespace": "pid:[333]"}
    auth_id, run_id = await _stamp_unconfirmed(client, runtime, monkeypatch, identity)
    await runtime.close()
    restarted = Runtime(runtime.directory, TOKEN, upstream=runtime.upstream)
    try:
        assert restarted.unconfirmed == {auth_id, run_id}
        assert restarted.get("auth", auth_id)["state"] == "stop_unconfirmed"
        assert restarted.get("run", run_id)["state"] == "stop_unconfirmed"
        assert (restarted.directory / "turns" / auth_id).is_dir()
        assert (restarted.directory / "turns" / run_id).is_dir()
        async with _restart_client(restarted) as restarted_client:
            sid = await session(restarted_client, key="kept-session")
            blocked = await restarted_client.post(
                f"/v1/sessions/{sid}/runs", json=request_body(), headers={"Idempotency-Key": "kept-run"})
            assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "stop_unconfirmed"
    finally:
        await restarted.close()
    runtime.unconfirmed.clear()


async def test_busy_instance_lock_keeps_unconfirmed_stops(client, runtime, monkeypatch, caplog):
    identity = {"boot_id": "boot-busy", "pid_namespace": "pid:[444]"}
    auth_id, run_id = await _stamp_unconfirmed(client, runtime, monkeypatch, identity)
    await runtime.close()
    held = os.open(runtime.directory / "instance.lock", os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity["pid_namespace"] = "pid:[555]"
        with caplog.at_level(logging.WARNING, logger="agentic_hermes.app"):
            restarted = Runtime(runtime.directory, TOKEN, upstream=runtime.upstream)
        try:
            assert "hermes instance lock is busy; unconfirmed stops stay unconfirmed" in caplog.text
            assert "instance.lock" not in caplog.text and str(runtime.directory) not in caplog.text
            assert restarted.unconfirmed == {auth_id, run_id}
            assert restarted.get("auth", auth_id)["state"] == "stop_unconfirmed"
            assert restarted.get("run", run_id)["state"] == "stop_unconfirmed"
            assert not restarted._instance_held
        finally:
            await restarted.close()
            await restarted.close()
    finally:
        os.close(held)
    runtime.unconfirmed.clear()


async def test_stop_without_instance_survives_a_new_namespace(tmp_path, monkeypatch):
    identity = {"boot_id": "boot-old", "pid_namespace": "pid:[111]"}
    monkeypatch.setattr("agentic_hermes.app.current_instance", lambda: dict(identity))
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "run_agent.py").write_text("# fixture\n")
    service = Runtime(tmp_path / "data", TOKEN, upstream=upstream)
    run_id = uuid.uuid4().hex
    service.put("run", run_id, {
        "run_id": run_id, "session_id": "s" * 32, "case_id": "fixture-case", "scope_id": SCOPE,
        "state": "stop_unconfirmed", "error": "stop_unconfirmed", "answer": "",
        "created_at": datetime.now(UTC).isoformat(), "updated_at": datetime.now(UTC).isoformat(),
        "event_cursor": 0, "usage": None,
    })
    (service.directory / "turns" / run_id).mkdir()
    await service.close()
    await service.close()
    identity["boot_id"] = "boot-new"
    identity["pid_namespace"] = "pid:[222]"
    restarted = Runtime(tmp_path / "data", TOKEN, upstream=upstream)
    try:
        assert run_id in restarted.unconfirmed
        assert restarted.get("run", run_id)["state"] == "stop_unconfirmed"
        assert "instance" not in restarted.get("run", run_id)
        assert (restarted.directory / "turns" / run_id).is_dir()
    finally:
        await restarted.close()
        await restarted.close()


async def test_runtime_revoked_credentials_cannot_resume_or_leak(client, runtime, monkeypatch):
    await configure(client)
    sid = await session(client)

    async def rejected(payload, scratch, identifier):
        return {"error": "needs_reauth", "answer": API_KEY, "history": []}

    monkeypatch.setattr(runtime, "invoke", rejected)
    body = request_body()
    await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "revoked"})
    await drain(runtime)
    result = await client.get(f"/v1/runs/{body['run_id']}?scope_id={SCOPE}")
    assert result.json()["state"] == "failed" and result.json()["error"] == "needs_reauth"
    assert API_KEY not in result.text
    assert (await client.get("/v1/capabilities")).json()["state"] == "needs_reauth"
    await configure(client)
    response = await client.post(f"/v1/sessions/{sid}/runs", json=request_body(), headers={"Idempotency-Key": "after-rotation"})
    assert response.status_code == 404


def test_hermes_builtin_dispatch_is_refused_even_if_model_invents_tools():
    executed = []
    call = guarded_dispatch(lambda name, *args, **kwargs: executed.append(name), {"mcp__agentic__host_status"})
    for name in ("terminal", "read_file", "execute_code", "delegate_task", "tool_call", "browser_navigate", "mcp__agentic__home_apply"):
        assert json.loads(call(name, {}))["error"] == "tool_not_authorized"
    assert executed == []
    call("mcp__agentic__host_status", {})
    assert executed == ["mcp__agentic__host_status"]


@contextmanager
def protocol_server(auth_error=False, controller=False, native=False):
    observed = {"model_calls": [], "tool_calls": [], "egress": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_CONNECT(self):
            # The child's proxy points here: any HTTPS call beyond loopback is egress.
            observed["egress"].append(self.path)
            self.send_response(403)
            self.end_headers()
            self.close_connection = True

        def do_GET(self):
            if self.path.startswith("http"):
                observed["egress"].append(self.path.split("/")[2])
            self.send_response(405)
            self.end_headers()

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/mcp":
                assert self.headers["Authorization"] == "Bearer " + MCP_TOKEN
                method = body["method"]
                if method == "initialize":
                    result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "product-fixture", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "host.status", "description": "Read fixed host status", "inputSchema": {"type": "object", "properties": {}}}, {"name": "terminal", "description": "must be filtered", "inputSchema": {"type": "object", "properties": {}}}]}
                elif method == "tools/call":
                    observed["tool_calls"].append(body["params"])
                    assert body["params"]["name"] == "host.status"
                    result = {"content": [{"type": "text", "text": '{"status":"ok","observed_from":"protocol_fixture"}'}], "isError": False}
                elif method.startswith("notifications/"):
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.reply({"jsonrpc": "2.0", "id": body["id"], "result": result})
                return
            if self.path == "/api/show":
                self.reply({"model_info": {"general.architecture": "fixture", "fixture.context_length": 65536}})
                return
            if native and self.path.endswith("/responses"):
                assert self.headers["Authorization"] == "Bearer " + CODEX_TOKEN
                observed["model_calls"].append(body)
                assert {tool["name"] for tool in body["tools"]} == {"mcp__agentic__host_status"}
                if len(observed["model_calls"]) == 1:
                    item = {"id": "fc_fixture", "type": "function_call", "status": "completed", "call_id": "call_fixture", "name": "mcp__agentic__host_status", "arguments": "{}"}
                else:
                    item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Fixture host diagnosis received; no real household tested.", "annotations": []}]}
                response = {"id": "resp_fixture", "object": "response", "created_at": 1, "model": "gpt-6-astra", "status": "completed", "output": [item], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                          {"type": "response.output_item.added", "output_index": 0, "item": item},
                          {"type": "response.output_item.done", "output_index": 0, "item": item},
                          {"type": "response.completed", "response": response}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write("".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode())
                return
            assert self.path.endswith("/chat/completions"), self.path
            assert self.headers["Authorization"] == "Bearer " + API_KEY
            observed["model_calls"].append(body)
            names = {t["function"]["name"] for t in body.get("tools", [])}
            expected = {"mcp__agentic__" + name.replace(".", "_") for name in TOOLS} if controller else {"mcp__agentic__host_status"}
            assert names == expected, names
            if auth_error:
                self.reply({"error": {"message": "Invalid API key", "type": "authentication_error"}}, status=401)
                return
            if any(m["role"] == "tool" for m in body["messages"]):
                message = {"role": "assistant", "content": "Fixture host diagnosis received; no real household tested."}
                finish = "stop"
            else:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_fixture_1", "type": "function", "function": {"name": "mcp__agentic__host_status", "arguments": "{}"}}]}
                finish = "tool_calls"
            reply = {"id": "fixture-completion", "object": "chat.completion", "created": 1, "model": "fixture/model", "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": 0, **delta["tool_calls"][0]}]
                chunk = {**reply, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\ndata: [DONE]\n\n").encode())
            else:
                self.reply(reply)

        def reply(self, value, status=200):
            encoded = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@contextmanager
def controller_mcp_server(tmp_path, monkeypatch):
    """Actual Controller adapter and Store; only the host broker is a fixture."""
    import uvicorn
    from agentic_controller.hermes import Coordinator
    from agentic_controller.mcp import create_mcp
    from agentic_controller.store import Store

    state, errors = {"evidence": [], "url": "http://127.0.0.1:8097/mcp"}, []

    async def host_fixture(*, store=None):
        return {"vm": "shut off", "kvm": True, "storage_free_bytes": 123456, "secret": "broker-must-not-leak"}

    monkeypatch.setattr("agentic_controller.hermes.host_call", host_fixture)

    def serve():
        store = Store(tmp_path / "controller.sqlite3")
        try:
            coordinator = Coordinator(store)
            coordinator.scope_valid = lambda item: item["scope"] == SCOPE
            message = {"id": uuid.uuid4().hex, "scope": SCOPE, "source": "web"}
            store.put("conversation", message)
            state["token"] = coordinator.grant(message, "fixture-case", uuid.uuid4().hex)
            evidence = coordinator.host_evidence

            async def record_evidence(grant):
                result = await evidence(grant)
                state["evidence"].append(result)
                return result

            coordinator.host_evidence = record_evidence
            server = uvicorn.Server(uvicorn.Config(create_mcp(coordinator), host="127.0.0.1", port=8097, log_level="critical", access_log=False, loop="asyncio"))
            state["server"] = server
            server.run()
        except BaseException as exc:
            errors.append(type(exc).__name__)
        finally:
            store.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if errors or (state.get("server") and state["server"].started):
                break
            time.sleep(0.02)
        assert not errors and state.get("server") and state["server"].started, errors
        yield state
    finally:
        if state.get("server"):
            state["server"].should_exit = True
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("auth_error,controller,native", [(False, False, False), (True, False, False), (False, True, False), (False, False, True)], ids=["mcp_roundtrip", "provider_401", "actual_controller_mcp", "native_codex_oauth"])
def test_real_pinned_hermes_uses_only_controller_mcp(tmp_path, request, monkeypatch, auth_error, controller, native):
    if not request.config.getoption("--containers") and not os.environ.get("AGENTIC_HERMES_TEST_PYTHON"):
        pytest.skip("Real pinned Hermes requires --containers or explicit isolated test interpreter/source")
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / ".hermes").mkdir()
    (home / "tmp").mkdir()
    payload = {"run_id": uuid.uuid4().hex, "session_id": uuid.uuid4().hex, "message": "Use host.status to diagnose the host", "history": [], "model": "fixture/model", "api_key": API_KEY, "mcp_token": MCP_TOKEN}
    with protocol_server(auth_error, controller, native) as (url, observed), controller_mcp_server(tmp_path, monkeypatch) if controller else nullcontext({"url": url + "/mcp", "token": MCP_TOKEN}) as mcp:
        payload["mcp_token"] = mcp["token"]
        name = "agentic-hermes-test-" + uuid.uuid4().hex[:12]
        if python := os.environ.get("AGENTIC_HERMES_TEST_PYTHON"):
            command = [python, "-m", "agentic_hermes.child"]
            env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "HERMES_HOME": str(home / ".hermes"), "TMPDIR": str(home / "tmp"), "PYTHONPATH": str(Path(__file__).parents[1] / "hermes") + ":" + os.environ["AGENTIC_HERMES_TEST_UPSTREAM"], "AGENTIC_MCP_URL": mcp["url"], "AGENTIC_PROVIDER_URL": url + "/v1",
                   "HTTPS_PROXY": url, "HTTP_PROXY": url, "NO_PROXY": "127.0.0.1,localhost"}
        else:
            command = ["docker", "run", *source_mounts(), "--rm", "--name", name, "-i", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=512m", "--pids-limit=64", "--network=host", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{home}:/scratch", "-e", "HOME=/scratch", "-e", "HERMES_HOME=/scratch/.hermes", "-e", "TMPDIR=/scratch/tmp", "-e", "PYTHONPATH=/app:/opt/hermes", "-e", "AGENTIC_MCP_URL=" + mcp["url"], "-e", "AGENTIC_PROVIDER_URL=" + url + "/v1", "-e", "HTTPS_PROXY=" + url, "-e", "HTTP_PROXY=" + url, "-e", "NO_PROXY=127.0.0.1,localhost", "--entrypoint=python", os.environ.get("AGENTIC_HERMES_IMAGE", "agentic-home-hermes:0.11.1"), "-m", "agentic_hermes.child"]
            env = None
        if native:
            payload.update(provider="openai-codex", model="gpt-6-astra", api_key="")
            command[-2:] = ["-c", "from agentic_hermes.upstream import HermesAdapter; "
                f"HermesAdapter().save_tokens({{'access_token':{CODEX_TOKEN!r},'refresh_token':'synthetic-native-refresh'}}); "
                "from agentic_hermes.child import main; main()"]
        try:
            result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=90)
        finally:
            if not python:
                subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)
        assert result.returncode == 0, result.stderr[-3000:]
        body = json.loads(result.stdout)
        # Only the provider and the Controller: no model catalog or other download.
        assert observed["egress"] == []
        if auth_error:
            assert body["error"] == "needs_reauth", (body, result.stderr[-2000:])
            assert body["answer"] == "" and body["history"] == []
            assert observed["model_calls"] and not observed["tool_calls"]
            assert API_KEY not in json.dumps(body)
            return
        assert body["error"] is None, (body, result.stderr[-3000:])
        assert "Fixture host diagnosis" in body["answer"]
        assert len(observed["model_calls"]) == 2
        if controller:
            assert len(mcp["evidence"]) == 1
            evidence = mcp["evidence"][0]
            assert evidence["case_id"] == "fixture-case" and evidence["observed_from"] == "host_broker"
            assert evidence["facts"] == {"vm": "shut off", "kvm": True, "storage_free_bytes": 123456}
            assert "broker-must-not-leak" not in json.dumps(observed)
            assert set(body["tools"]) == {"mcp__agentic__" + name.replace(".", "_") for name in TOOLS}
        else:
            assert [{k: call[k] for k in ("name", "arguments")} for call in observed["tool_calls"]] == [{"name": "host.status", "arguments": {}}]
            assert body["tools"] == ["mcp__agentic__host_status"]
        assert len(body["history"]) >= 4
        assert MCP_TOKEN not in json.dumps(observed["model_calls"])
        assert mcp["token"] not in json.dumps(observed["model_calls"])
        assert API_KEY not in json.dumps(body)
        assert CODEX_TOKEN not in json.dumps(body) and "synthetic-native-refresh" not in json.dumps(body)
        assert set(TOOLS) >= {"host.status", "worker.submit", "home.observe", "home.propose", "home.proposal_status"}


def test_packaged_runtime_starts_nonroot_without_cloud_credentials(tmp_path, request):
    if not request.config.getoption("--containers"):
        pytest.skip("Packaged runtime startup requires --containers")
    name = "agentic-hermes-service-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    token_path = tmp_path / "service-token"
    token_path.write_text(TOKEN)
    token_path.chmod(0o644)  # Dedicated synthetic credential, mounted as a file.
    image = os.environ.get("AGENTIC_HERMES_IMAGE", "agentic-home-hermes:0.11.1")
    probe = (
        "import json,os,urllib.request; "
        "r=urllib.request.Request('http://127.0.0.1:8096/v1/capabilities',"
        f"headers={{'Authorization':'Bearer {TOKEN}'}}); "
        "print(json.dumps({'uid':os.getuid(),'capabilities':json.load(urllib.request.urlopen(r,timeout=2))}))"
    )
    try:
        subprocess.run(["docker", "run", *source_mounts(), "-d", "--name", name, "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=512m", "-v", volume + ":/data", "-v", str(token_path) + ":/hermes-link/service-token:ro", image], check=True, capture_output=True, text=True)
        result = None
        for _ in range(40):
            result = subprocess.run(["docker", "exec", name, "python", "-c", probe], capture_output=True, text=True)
            if result.returncode == 0:
                break
            time.sleep(0.1)
        assert result and result.returncode == 0, result.stderr if result else "runtime did not start"
        value = json.loads(result.stdout)
        assert value["uid"] == 10003
        assert value["capabilities"]["state"] == "needs_auth"
        assert value["capabilities"]["auth_verified_at"] is None
        assert TOKEN not in result.stdout
        # Exercise the same pidfd boundary under the packaged UID/seccomp profile,
        # not only under the developer interpreter used by service unit tests.
        fence_probe = """
import asyncio, sys
from agentic_hermes.app import Runtime
async def main():
    runtime = Runtime('/data/fence-probe', 'fixture-service-' + 'f' * 40)
    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)', start_new_session=True)
    runtime.track_process('fixture', proc)
    await runtime.stop_process('fixture')
    assert proc.returncode is not None and not runtime.processes
    await runtime.close()
asyncio.run(main())
"""
        stopped = subprocess.run(["docker", "exec", name, "python", "-c", fence_probe], capture_output=True, text=True, timeout=120)
        assert stopped.returncode == 0, stopped.stderr[-2000:]
        # Exercise security fixes in installed wheels, including the advisory
        # whose first-patched-version metadata is still empty on GitHub.
        security_probe = """
import base64
from importlib.metadata import version
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
assert version('PyJWT') == '2.15.0'
assert version('httpx2') == version('httpcore2') == '2.12.0'
secret = 'synthetic-security-probe-key-00000000'
token = jwt.encode({'exp': 1}, secret, algorithm='HS256')
options = {'verify_signature': False}
jwt.decode(token, options=options)
assert options == {'verify_signature': False}, 'decoder mutated caller options'
options['verify_signature'] = True
try:
    jwt.decode(token, secret, algorithms=['HS256'], options=options)
except jwt.ExpiredSignatureError:
    pass
else:
    raise AssertionError('reused options skipped expiry verification')
private = Ed25519PrivateKey.generate()
other = Ed25519PrivateKey.generate()
def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
jwk = {'kty': 'OKP', 'crv': 'Ed25519',
       'x': b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
       'd': b64(private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))}
jwt.PyJWK.from_dict(jwk)
jwk['x'] = b64(other.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
try:
    jwt.PyJWK.from_dict(jwk)
except jwt.InvalidKeyError:
    pass
else:
    raise AssertionError('mismatched OKP key components accepted')
"""
        secured = subprocess.run(["docker", "exec", name, "python", "-c", security_probe], capture_output=True, text=True, timeout=120)
        assert secured.returncode == 0, secured.stderr[-2000:]
        consistent = subprocess.run(["docker", "exec", name, "python", "-m", "pip", "check"], capture_output=True, text=True, timeout=120)
        assert consistent.returncode == 0, consistent.stdout[-2000:] + consistent.stderr[-2000:]
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)


def test_offline_candidate_gate_and_real_upstream_device_flow(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Candidate and native device-flow probes require --containers")
    name = "agentic-hermes-oauth-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    image = os.environ.get("AGENTIC_HERMES_IMAGE", "agentic-home-hermes:0.11.1")
    common = ["docker", "run", *source_mounts(), "--rm", "--name", name, "--network=none", "--read-only", "--cap-drop=ALL",
              "--security-opt=no-new-privileges", "--memory=512m", "--pids-limit=64", "-v", volume + ":/data",
              "-e", "HOME=/data", "-e", "HERMES_HOME=/data", "-e", "TMPDIR=/data",
              "-e", "PYTHONPATH=/app:/opt/hermes", "--entrypoint=python", image]
    try:
        gate = subprocess.run(common + ["/app/compatibility_probe.py"], capture_output=True, text=True, timeout=90)
        assert gate.returncode == 0, gate.stdout[-3000:] + gate.stderr[-3000:]
        receipt = json.loads(gate.stdout)
        assert receipt["compatible"] is True and receipt["adapter_contract"] == 1
        assert receipt["cloud_account_verified"] is False
        assert set(receipt["checks"]) >= {
            "consumed_signatures", "continued_history", "builtin_tool_refused", "provider_401",
            "native_device_oauth", "private_token_store", "native_responses_tool_roundtrip",
        }
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)


async def test_idle_runtime_prunes_without_new_request(runtime):
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    runtime.put("session", "e" * 32, {"session_id": "e" * 32, "created_at": old, "scope_id": SCOPE})
    runtime.remember("session", "idle-once", "f" * 64, "e" * 32)
    maintenance = asyncio.create_task(runtime.maintain(interval=0.01))
    try:
        async with asyncio.timeout(2):
            while runtime.get("session", "e" * 32):
                await asyncio.sleep(0.01)
        assert runtime.db.execute("SELECT count(*) FROM requests WHERE id=?", ("e" * 32,)).fetchone()[0] == 1
    finally:
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)


def test_device_login_and_provider_failures_stay_distinct_codes():
    from agentic_hermes.oauth import reason_from_stderr
    from agentic_hermes.oauth_child import classify_login
    from agentic_hermes.upstream import classify_provider_failure

    class Failure(Exception):
        def __init__(self, **fields):
            self.__dict__.update(fields)

    assert classify_login(Failure(code="access_denied")) == ("failed", "access_denied")
    assert classify_login(Failure(error="slow_down")) == ("failed", "slow_down")
    assert classify_login(Failure(status_code=429)) == ("failed", "slow_down")
    assert classify_login(Failure(code="expired_token")) == ("expired", "expired_token")
    assert classify_login(Failure(code="device_code_timeout")) == ("expired", "expired_token")
    assert classify_login(Failure(code="provider blew up sk-live")) == ("failed", "oauth_failed")
    denied = Failure(code="access_denied")
    denied.args = ("secret provider body sk-live",)
    assert "sk-live" not in "".join(classify_login(denied))
    assert reason_from_stderr(b"access_denied by the user") == "access_denied"
    assert reason_from_stderr(b"HTTP 429 slow down") == "slow_down"
    assert reason_from_stderr(b"device_code_timeout") == "expired_token"
    assert reason_from_stderr(b"totally unknown sk-live") is None
    assert classify_provider_failure("401 invalid api key sk-live") == "needs_reauth"
    assert classify_provider_failure("429 insufficient_quota") == "rate_limited"
    assert classify_provider_failure("limit") == "inference_failed"
    assert classify_provider_failure("504 deadline exceeded") == "timeout"
    assert classify_provider_failure("502 bad gateway") == "provider_unavailable"
    assert classify_provider_failure("connection reset") == "provider_unavailable"
    assert classify_provider_failure("model refused the schema") == "inference_failed"


async def test_pending_login_resumes_without_clearing_the_account(client, runtime, monkeypatch):
    cleared = []
    original = runtime.clear_account

    def spy():
        cleared.append("clear")
        return original()

    entered = asyncio.Event()

    async def oauth_fixture(item, home):
        runtime.device_login.update(item, "pending", verification_uri="https://auth.openai.com/codex/device",
                                    user_code="ABCD-EFGH")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "clear_account", spy)
    monkeypatch.setattr(runtime.device_login, "invoke", oauth_fixture)
    body = {"provider": "openai-codex", "model": "gpt-6-astra"}
    first = await client.post("/v1/auth/device", json=body, headers={"Idempotency-Key": "login-a"})
    assert first.status_code == 202
    await entered.wait()
    second = await client.post("/v1/auth/device", json={**body, "model": "other-model"}, headers={"Idempotency-Key": "login-b"})
    assert second.status_code == 202
    assert second.json()["auth_id"] == first.json()["auth_id"]
    assert second.json()["user_code"] == "ABCD-EFGH"
    assert second.json()["verification_uri"] == "https://auth.openai.com/codex/device"
    assert "reason" in second.json()
    assert len(cleared) == 1
    pending = (await client.get("/v1/capabilities")).json()["pending_auth"]
    assert pending["auth_id"] == first.json()["auth_id"] and pending["state"] == "pending"
    assert pending["user_code"] == "ABCD-EFGH"
    await client.post("/v1/auth/device/" + first.json()["auth_id"] + "/cancel")


@pytest.mark.parametrize("outcome,reason,state", [
    (("failed", "access_denied"), "access_denied", "failed"),
    (("failed", "slow_down"), "slow_down", "failed"),
    (("expired", "expired_token"), "expired_token", "expired"),
])
async def test_oauth_child_reason_is_stored(client, runtime, monkeypatch, outcome, reason, state):
    async def oauth_fixture(item, home):
        return outcome

    monkeypatch.setattr(runtime.device_login, "invoke", oauth_fixture)
    started = await client.post("/v1/auth/device", json={"provider": "openai-codex", "model": "gpt-6-astra"},
                                headers={"Idempotency-Key": "reason-" + reason})
    await drain(runtime)
    status = await client.get("/v1/auth/device/" + started.json()["auth_id"])
    assert status.json()["state"] == state
    assert status.json()["error"] == reason and status.json()["reason"] == reason
    assert "sk-" not in status.text


async def test_oauth_parent_timeout_is_expired_token(client, runtime, monkeypatch):
    async def oauth_fixture(item, home):
        raise TimeoutError

    monkeypatch.setattr(runtime.device_login, "invoke", oauth_fixture)
    started = await client.post("/v1/auth/device", json={"provider": "openai-codex", "model": "gpt-6-astra"},
                                headers={"Idempotency-Key": "parent-timeout"})
    await drain(runtime)
    status = (await client.get("/v1/auth/device/" + started.json()["auth_id"])).json()
    assert status["state"] == "expired" and status["reason"] == "expired_token"


@pytest.mark.parametrize("error", ["rate_limited", "provider_unavailable", "timeout", "needs_reauth"])
async def test_turn_error_codes_pass_through_without_provider_text(client, runtime, monkeypatch, error):
    await configure(client)
    sid = await session(client, key="turn-" + error)

    async def inference(payload, scratch, identifier):
        return {"error": error, "answer": "provider said sk-live-secret", "history": [{"secret": "sk-live-secret"}]}

    monkeypatch.setattr(runtime, "invoke", inference)
    body = request_body()
    await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "turn-" + error})
    await drain(runtime)
    result = await client.get(f"/v1/runs/{body['run_id']}?scope_id={SCOPE}")
    assert result.json()["state"] == "failed" and result.json()["error"] == error
    assert "sk-live" not in result.text
    auth = (await client.get("/v1/capabilities")).json()
    assert auth["state"] == ("needs_reauth" if error == "needs_reauth" else "configured")


async def test_turn_timeout_does_not_become_runtime_unavailable(client, runtime, monkeypatch):
    await configure(client)
    sid = await session(client, key="turn-timeout")

    async def inference(payload, scratch, identifier):
        raise TimeoutError

    monkeypatch.setattr(runtime, "invoke", inference)
    body = request_body()
    await client.post(f"/v1/sessions/{sid}/runs", json=body, headers={"Idempotency-Key": "turn-timeout"})
    await drain(runtime)
    result = (await client.get(f"/v1/runs/{body['run_id']}?scope_id={SCOPE}")).json()
    assert result["error"] == "timeout"
    assert (await client.get("/v1/capabilities")).json()["state"] == "configured"


@pytest.mark.parametrize("text,code", [
    ("This model's maximum context length is 128000 tokens; your messages resulted in 140350 tokens", "inference_failed"),
    ("request used 14013 tokens of 40100", "inference_failed"),
    ("Error code: 401 - unauthorized", "needs_reauth"),
    ("HTTP 403", "needs_reauth"),
    ("status 429: slow down", "rate_limited"),
    ("upstream returned 503", "provider_unavailable"),
    ("gateway 504", "timeout"),
    ("version 1.403.2", "inference_failed"),
    ("model error, request req_8a401b trace 0a5030c", "inference_failed"),
])
def test_provider_failure_status_numbers_must_stand_alone(text, code):
    from agentic_hermes.upstream import classify_provider_failure
    assert classify_provider_failure(text) == code


def test_stderr_429_needs_a_standalone_status():
    from agentic_hermes.oauth import reason_from_stderr
    assert reason_from_stderr(b"HTTP 429 Too Many Requests") == "slow_down"
    assert reason_from_stderr(b"started at 1714290001") is None
    assert reason_from_stderr(b"device session a429f1 closed") is None
