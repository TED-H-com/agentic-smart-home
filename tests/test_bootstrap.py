"""Protocol fixtures for resumable setup; no cloud, installed OS, or real HA claims."""
import asyncio
import copy
import json

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.bootstrap import Bootstrap
from agentic_controller.models import BootstrapPreferences, BootstrapStart, GatewayPreferences
from agentic_controller.runner import RunnerError
from agentic_controller.store import Conflict

REVISION = "a" * 40


class HostFixture:
    def __init__(self):
        self.calls, self.lost = [], False
        self.value = {"kvm": True, "provisioned": False, "vm": "not_created", "restore_pending": None,
            "jobs": [], "bootstrap": {"network": {"ready": True},
            "hermes": {"state": "bundled", "source": "bundled", "commit": "b" * 40},
            "ha": {"reachable": False, "onboarding_available": False, "onboarding_complete": False, "steps": []}}}

    async def __call__(self, method="GET", body=None):
        self.calls.append((method, copy.deepcopy(body)))
        if method == "GET":
            return copy.deepcopy(self.value)
        assert set(body) == {"id", "action"}
        assert body["action"] in {"bootstrap_hermes", "provision", "start"}
        previous = next((job for job in self.value["jobs"] if job["id"] == body["id"]), None)
        if previous:
            assert previous["request"] == body
            return previous
        job = {"id": body["id"], "request": copy.deepcopy(body), "state": "running"}
        self.value["jobs"].append(job)
        if self.lost:
            self.lost = False
            raise OSError("private daemon output must not leak")
        return job

    def finish(self, action):
        job = next(job for job in reversed(self.value["jobs"]) if job["request"]["action"] == action)
        job["state"] = "succeeded"
        if action == "bootstrap_hermes":
            self.value["bootstrap"]["hermes"] = {"state": "ready", "source": "release", "commit": REVISION}
        elif action == "provision":
            self.value.update(provisioned=True, vm="shut off")
        elif action == "start":
            self.value["vm"] = "running"


class RunnerFixture:
    def __init__(self):
        self.connected, self.lost, self.calls, self.jobs = False, False, [], {}
        self.reason = None
        self.assist_error = None

    async def call(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/status":
            item = {"provider": "codex", "version_matches": True,
                    "status": "connected" if self.connected else "needs_reauth"}
            if self.reason:
                item["reason"] = self.reason
            return {"protocol": 1, "identity": "agentic-home-runner", "providers": [item]}
        if method == "GET":
            if path.rsplit("/", 1)[-1] not in self.jobs:
                raise RunnerError("missing", 404)
            return self.jobs[path.rsplit("/", 1)[-1]]
        assert path == "/bootstrap/jobs"
        assert body["provider"] == "codex" and body["stage"] == "hermes_setup"
        if self.assist_error:
            result = {"id": body["id"], "state": "failed", "error": self.assist_error}
        else:
            result = {"id": body["id"], "state": "succeeded", "evidence": {
                "inference_completed": True, "observed_from": "host_runner", "live_mutations": False},
                "result": {"decision": "continue", "next_step": "install_hermes", "reason_codes": []}}
        self.jobs[body["id"]] = result
        if self.lost:
            self.lost = False
            raise RunnerError("reply lost", 503)
        return result


class HermesFixture:
    def __init__(self):
        self.runtime = {"state": "needs_auth", "upstream_commit": REVISION, "auth_verified_at": None}

    async def capabilities(self):
        return {"runtime": copy.deepcopy(self.runtime)}


class GatewayFixture:
    def __init__(self, store):
        self.store, self.states, self.configs, self.messages = store, {}, {}, {}

    def preferences(self):
        return GatewayPreferences.model_validate(self.store.setting("gateway", {}))

    def config(self, channel):
        return self.configs.get(channel, {"enabled": False, "chats": [], "senders": []})

    async def cancel_pairing(self, channel):
        return {"state": "cancelled"}

    def submit(self, body, source, chat, sender, event):
        assert body.coordinator == "hermes" and "不要呼叫工具" in body.message
        assert source == "web" and chat == "owner"
        return self.messages.setdefault(event, {"id": event, "state": "planning"})


@pytest.fixture
def setup(store, ha, monkeypatch):
    backend, runner, hermes, gateway = HostFixture(), RunnerFixture(), HermesFixture(), GatewayFixture(store)
    store.set_setting("runner", {"fixture": True})
    bootstrap = Bootstrap(store, runner, hermes, gateway, ha, "fixture-controller", host_request=backend)
    monkeypatch.setattr(bootstrap, "resume", lambda: None)
    return bootstrap, backend, runner, hermes, gateway


async def start_at_install(setup, channel="web"):
    bootstrap, _, runner, _, _ = setup
    runner.connected = True
    await bootstrap.start(BootstrapStart(provider="codex", channel=channel), "start")
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "hermes_install"


async def ready_for_channel(setup, channel="line"):
    bootstrap, backend, _, hermes, _ = setup
    await start_at_install(setup, channel)
    backend.finish("bootstrap_hermes")
    hermes.runtime.update(state="ready", auth_verified_at="fixture-observed-time")
    await bootstrap.advance()


async def test_start_pairs_automatically_waits_for_actual_auth_and_resumes(setup):
    bootstrap, backend, runner, _, _ = setup
    first = await bootstrap.start(BootstrapStart(provider="codex"), "same-key")
    assert first["state"] == "running"
    assert bootstrap.record()["bridge_auto_enroll"] is True
    await bootstrap.advance()
    waiting = bootstrap.public()
    assert waiting["current_step"] == "cli_auth"
    assert waiting["human_action"]["kind"] == "cli_login"
    assert not any(method == "POST" for method, _ in backend.calls)
    assert await bootstrap.start(BootstrapStart(provider="codex"), "same-key") == waiting
    with pytest.raises(Conflict):
        await bootstrap.start(BootstrapStart(provider="grok"), "same-key")
    runner.connected = True
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "hermes_install"
    assert len(runner.jobs) == 1


async def test_account_error_categories_reach_login_and_assist(setup):
    bootstrap, _, runner, _, _ = setup
    await bootstrap.start(BootstrapStart(provider="codex"), "start-plain")
    await bootstrap.advance()
    plain = bootstrap.public()
    assert plain["human_action"]["message"] == "請完成所選 CLI 的官方登入；完成後會自動繼續。"
    assert plain["human_action"]["code"] is None
    assert next(step["code"] for step in plain["steps"] if step["id"] == "cli_auth") == "cli_login_required"
    runner.reason = "rate_limited"
    await bootstrap.advance()
    limited = bootstrap.public()
    assert limited["current_step"] == "cli_auth" and limited["human_action"]["kind"] == "cli_login"
    assert limited["human_action"]["code"] == "rate_limited"
    assert limited["human_action"]["message"] == "已達用量上限，請稍後再試。"
    assert next(step["code"] for step in limited["steps"] if step["id"] == "cli_auth") == "cli_login_required"
    runner.connected, runner.reason, runner.assist_error = True, None, "needs_reauth"
    await bootstrap.advance()
    failed = bootstrap.public()
    assert failed["state"] == "blocked" and failed["current_step"] == "cli_assist"
    assert next(step["code"] for step in failed["steps"] if step["id"] == "cli_assist") == "needs_reauth"
    assert failed["human_action"]["message"] == "登入已失效，請重新登入。"
    assert bootstrap.record().get("assist_request")
    await bootstrap.retry("retry-account")
    assert "assist_request" not in bootstrap.record()


async def test_no_network_prevents_pairing_auth_and_any_host_action(setup):
    bootstrap, backend, runner, _, _ = setup
    backend.value["bootstrap"]["network"]["ready"] = False
    await bootstrap.start(BootstrapStart(provider="codex"), "start")
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "network"
    assert not runner.calls and all(method == "GET" for method, _ in backend.calls)
    backend.value["bootstrap"]["network"]["ready"] = True
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "cli_auth"


async def test_lost_runner_reply_uses_exact_persisted_handle_without_second_inference(setup):
    bootstrap, backend, runner, _, _ = setup
    runner.connected, runner.lost = True, True
    await bootstrap.start(BootstrapStart(provider="codex"), "start")
    await bootstrap.advance()
    original = copy.deepcopy(bootstrap.record()["assist_request"])
    assert bootstrap.public()["current_step"] == "cli_assist"
    assert all(method == "GET" for method, _ in backend.calls)
    await bootstrap.advance()
    assert bootstrap.record()["assist_request"] == original
    assert len([call for call in runner.calls if call[:2] == ("POST", "/bootstrap/jobs")]) == 1
    assert bootstrap.public()["current_step"] == "hermes_install"


async def test_restart_after_lost_host_reply_reconciles_original_job(setup, store, ha, monkeypatch):
    bootstrap, backend, runner, hermes, gateway = setup
    backend.lost = True
    await start_at_install(setup)
    identifier = backend.value["jobs"][0]["id"]
    restarted = Bootstrap(store, runner, hermes, gateway, ha, "fixture-controller", host_request=backend)
    monkeypatch.setattr(restarted, "resume", lambda: None)
    await restarted.advance()
    assert restarted.record()["jobs"]["hermes_install:bootstrap_hermes"]["request"]["id"] == identifier
    assert len([call for call in backend.calls if call[0] == "POST"]) == 1
    backend.finish("bootstrap_hermes")
    await restarted.advance()
    assert restarted.public()["human_action"]["kind"] == "hermes_login"
    assert "private" not in json.dumps(restarted.public())


async def test_configured_or_model_success_never_substitutes_for_hermes_readiness(setup):
    bootstrap, backend, _, hermes, gateway = setup
    await start_at_install(setup)
    backend.finish("bootstrap_hermes")
    hermes.runtime["state"] = "configured"
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "hermes_auth"
    event = next(iter(gateway.messages))
    gateway.messages[event]["state"] = "succeeded"
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "hermes_auth" and len(gateway.messages) == 1
    assert not any(body and body["action"] == "provision" for _, body in backend.calls)
    hermes.runtime.update(state="ready", auth_verified_at="fixture-observed-time")
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "ha_provision"


@pytest.mark.parametrize("error,code,state", [
    ("timeout", "hermes_verification_timeout", "blocked"),
    ("rate_limited", "hermes_rate_limited", "blocked"),
    ("provider_unavailable", "hermes_provider_unavailable", "blocked"),
    ("needs_reauth", "hermes_login_required", "waiting_input"),
    ("mystery", "hermes_verification_failed", "blocked"),
])
async def test_hermes_verification_failure_codes_stay_distinct(setup, error, code, state):
    bootstrap, backend, _, hermes, gateway = setup
    await start_at_install(setup)
    backend.finish("bootstrap_hermes")
    hermes.runtime["state"] = "configured"
    await bootstrap.advance()
    event = next(iter(gateway.messages))
    gateway.messages[event].update(state="failed", error_code=error)
    await bootstrap.advance()
    step = next(item for item in bootstrap.public()["steps"] if item["id"] == "hermes_auth")
    assert step["code"] == code and step["state"] == state
    assert bootstrap.public()["current_step"] == "hermes_auth"
    if state == "blocked":
        await bootstrap.retry("retry-" + error)
        assert "verification" not in bootstrap.record()
        assert bootstrap.record()["attempts"]["hermes_auth"] >= 1


async def test_channel_must_be_live_or_explicitly_skipped_before_auto_ha(setup):
    bootstrap, backend, _, _, gateway = setup
    await ready_for_channel(setup)
    assert bootstrap.public()["human_action"]["kind"] == "channel_setup"
    gateway.configs["line"] = {"enabled": True, "chats": ["fixture"], "senders": ["fixture"]}
    gateway.states["line"] = "connecting"
    await bootstrap.advance()
    assert bootstrap.public()["current_step"] == "channel"
    await bootstrap.preferences(BootstrapPreferences(channel="web"), "skip-explicitly")
    await bootstrap.advance()
    assert backend.value["jobs"][-1]["request"]["action"] == "provision"
    assert next(step for step in bootstrap.public()["steps"] if step["id"] == "channel")["state"] == "skipped"


async def test_native_ha_requires_native_completion_and_live_owner_not_browser_checkpoint(setup, store):
    bootstrap, backend, _, _, _ = setup
    await ready_for_channel(setup, "web")
    backend.finish("provision")
    await bootstrap.advance()
    assert backend.value["jobs"][-1]["request"]["action"] == "start"
    backend.finish("start")
    backend.value["bootstrap"]["ha"]["reachable"] = True
    await bootstrap.advance()
    assert bootstrap.public()["human_action"]["code"] == "ha_native_onboarding_required"
    backend.value["bootstrap"]["ha"]["onboarding_complete"] = True
    await bootstrap.advance()
    assert bootstrap.public()["human_action"]["code"] == "ha_owner_required"
    store.set_setting("owner", "revoked-or-wrong-owner")
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "waiting_input"
    store.set_setting("owner", "owner")
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "completed"


async def test_restarted_core_missing_onboarding_route_requires_live_owner_readback(setup, store, ha, monkeypatch):
    import urllib.error
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    from agentic_host.bootstrap import native_ha_status

    def absent(url, **kwargs):
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)
    bootstrap, backend, _, _, _ = setup
    await ready_for_channel(setup, "web")
    backend.finish("provision")
    await bootstrap.advance()
    backend.finish("start")
    actual_observation = native_ha_status(opener=absent)
    assert actual_observation["reachable"] is False and actual_observation["http_status"] == 404
    backend.value["bootstrap"]["ha"] = actual_observation
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "waiting_input"
    store.set_setting("owner", "owner")
    ha.revoked = True
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "waiting_input"
    ha.revoked = False
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "completed"
    assert bootstrap.public()["steps"][-1]["code"] == "ha_owner_and_route_absence_verified"


async def test_retry_never_replays_unconfirmed_host_operation(setup):
    bootstrap, backend, _, _, _ = setup
    await start_at_install(setup)
    backend.value["jobs"][0]["state"] = "needs_reconcile"
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "blocked"
    original = copy.deepcopy(bootstrap.record()["jobs"])
    await bootstrap.retry("retry-once")
    await bootstrap.advance()
    assert bootstrap.record()["jobs"] == original
    assert bootstrap.public()["state"] == "blocked"
    assert len([call for call in backend.calls if call[0] == "POST"]) == 1


async def test_failed_host_job_needs_explicit_idempotent_retry_with_new_handle(setup):
    bootstrap, backend, _, _, _ = setup
    await start_at_install(setup)
    first = backend.value["jobs"][0]["id"]
    backend.value["jobs"][0]["state"] = "failed"
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "blocked"
    await bootstrap.advance()
    assert len(backend.value["jobs"]) == 1
    await bootstrap.retry("retry-once")
    await bootstrap.advance()
    assert len(backend.value["jobs"]) == 2 and backend.value["jobs"][-1]["id"] != first
    await bootstrap.retry("retry-once")
    await bootstrap.advance()
    assert len(backend.value["jobs"]) == 2


async def test_lifespan_resume_keeps_background_progress_without_page(setup, store, ha):
    bootstrap, backend, runner, hermes, gateway = setup
    await bootstrap.start(BootstrapStart(provider="codex"), "start")
    resumed = Bootstrap(store, runner, hermes, gateway, ha, "fixture-controller", host_request=backend)
    resumed.resume()
    try:
        async with asyncio.timeout(2):
            while resumed.public()["current_step"] != "cli_auth":
                await asyncio.sleep(.01)
        assert resumed.public()["state"] == "waiting_input"
    finally:
        await resumed.close()


async def test_http_get_is_pure_and_browser_cannot_report_success(store, ha, tmp_path, monkeypatch):
    from agentic_controller import host
    backend = HostFixture()
    monkeypatch.setattr(host, "host_call", backend)
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("fixture-machine-access-key")
    app = create_app(store=store, ha=ha, host_mode=True, runner=RunnerFixture())
    monkeypatch.setattr(app.state.bootstrap, "resume", lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                 headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.get("/api/bootstrap")).status_code == 401
        await client.post("/api/host/login", json={"key": "fixture-machine-access-key"})
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        before = store.db.total_changes
        assert (await client.get("/api/bootstrap")).json()["state"] == "idle"
        assert store.db.total_changes == before and not backend.calls
        assert (await client.post("/api/bootstrap/start", json={"provider": "codex"})).status_code == 400
        client.headers["Idempotency-Key"] = "start"
        assert (await client.post("/api/bootstrap/start", json={"provider": "codex", "completed": True})).status_code == 422
        assert (await client.post("/api/bootstrap/start", json={"provider": "codex"})).status_code == 202
        before = store.db.total_changes
        assert (await client.get("/api/bootstrap")).json()["state"] == "running"
        assert store.db.total_changes == before and not backend.calls
        client.headers["X-Agentic-CSRF"] = "invalid"
        assert (await client.post("/api/bootstrap/retry", json={})).status_code == 403


async def test_controller_request_passes_real_runner_bootstrap_contract(setup, tmp_path, monkeypatch):
    import hashlib

    from agentic_runner.app import Broker, create_api
    bootstrap, _, _, _, _ = setup
    monkeypatch.setenv("AGENTIC_HOST_MODE", "1")
    broker = Broker(tmp_path / "runner")
    broker.settings({"token_hash": hashlib.sha256(b"fixture-token").hexdigest()})
    async def health(provider):
        return {"provider": provider, "version": "fixture", "version_matches": True, "status": "connected"}
    async def run(*args):
        return {"decision": "continue", "next_step": "install_hermes", "reason_codes": ["ready_for_next_stage"]}
    monkeypatch.setattr(broker.providers, "health", health)
    monkeypatch.setattr(broker.providers, "run", run)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                     headers={"Authorization": "Bearer fixture-token"}) as client:
            class Adapter:
                async def call(self, method, path, body=None):
                    response = await client.request(method, path, json=body)
                    if response.status_code >= 300:
                        raise RunnerError("fixture HTTP rejection", response.status_code)
                    return response.json()
            bootstrap.runner = Adapter()
            await bootstrap.start(BootstrapStart(provider="codex", channel="web"), "start")
            await bootstrap.advance()
            await asyncio.gather(*list(broker.tasks.values()))
            await bootstrap.advance()
            assert bootstrap.public()["current_step"] == "hermes_install"
            assert len(broker.db.execute("SELECT * FROM jobs").fetchall()) == 1
    finally:
        for task in list(broker.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(broker.tasks.values()), return_exceptions=True)
        broker.db.close()


async def test_desktop_ticket_is_single_use_origin_bound_and_not_a_general_auth_bypass(store, ha, tmp_path, monkeypatch):
    import hashlib
    import secrets
    import time

    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    # host/install.py enrolls this private key before exposing desktop login.
    access = tmp_path / "access-key"
    access.write_text(secrets.token_urlsafe(32) + "\n")
    access.chmod(0o600)
    app = create_app(store=store, ha=ha, host_mode=True)
    ticket = secrets.token_urlsafe(32)
    path = tmp_path / "desktop-ticket.json"
    path.write_text(json.dumps({"ticket_hash": hashlib.sha256(ticket.encode()).hexdigest(), "expires": time.time() + 119}))
    path.chmod(0o600)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099") as client:
        assert (await client.post("/api/host/desktop-login", json={"ticket": ticket})).status_code == 403
        client.headers["Origin"] = "http://localhost:8099"
        assert (await client.post("/api/bootstrap/start", json={"provider": "codex"})).status_code == 401
        assert (await client.post("/api/host/desktop-login", json={"ticket": "wrong"})).status_code == 422
        assert (await client.post("/api/host/desktop-login", json={"ticket": "x" * 43})).status_code == 403
        assert path.read_text()
        result = await client.post("/api/host/desktop-login", json={"ticket": ticket})
        assert result.status_code == 200 and "httponly" in result.headers["set-cookie"].lower()
        assert not path.read_text() and ticket not in result.text
        assert (await client.get("/api/session")).status_code == 200
        assert (await client.post("/api/host/desktop-login", json={"ticket": ticket})).status_code == 403
        path.unlink()
        assert (await client.post("/api/host/desktop-login", json={"ticket": ticket})).status_code == 403
        path.write_text(json.dumps({"ticket_hash": hashlib.sha256(ticket.encode()).hexdigest(), "expires": time.time() - 1}))
        path.chmod(0o600)
        assert (await client.post("/api/host/desktop-login", json={"ticket": ticket})).status_code == 403
        path.unlink()
        target = tmp_path / "outside.json"
        target.write_text("must remain untouched")
        path.symlink_to(target)
        assert (await client.post("/api/host/desktop-login", json={"ticket": ticket})).status_code == 403
        assert target.read_text() == "must remain untouched"
