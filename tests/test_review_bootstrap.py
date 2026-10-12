"""Review regressions: local protocol fixtures, without cloud or household effects."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from agentic_controller import bootstrap as bootstrap_module
from agentic_controller import host
from agentic_controller.app import create_app
from agentic_controller.bootstrap import STEPS, Bootstrap
from agentic_controller.models import BootstrapStart
from agentic_controller.runner import Runner, RunnerError
from agentic_runner.app import Broker, create_api
from fastapi import HTTPException
from test_bootstrap import GatewayFixture, HermesFixture, HostFixture, RunnerFixture, start_at_install


@pytest.fixture
def review_setup(store, ha, monkeypatch):
    backend, runner, hermes, gateway = HostFixture(), RunnerFixture(), HermesFixture(), GatewayFixture(store)
    store.set_setting("runner", {"fixture": True})
    runner.connected = True
    hermes.runtime.update(state="ready", auth_verified_at="fixture-observed-time")
    bootstrap = Bootstrap(store, runner, hermes, gateway, ha, "a" * 32, host_request=backend)
    monkeypatch.setattr(bootstrap, "resume", lambda: None)
    return bootstrap, backend, runner, hermes, gateway


@pytest.fixture
def review_clock(monkeypatch):
    now = [2_000_000_000.0]
    monkeypatch.setattr(bootstrap_module.time, "time", lambda: now[0])
    return now


async def checkpoint_at(bootstrap, step):
    await bootstrap.start(BootstrapStart(provider="codex", channel="web"), "review-start")
    record = bootstrap.record()
    for item in record["steps"]:
        if STEPS.index(item["id"]) < STEPS.index(step):
            item.update(state="completed", verified_at="fixture-observed-time")
    record["current_step"] = step
    bootstrap.save(record)


@pytest.mark.parametrize("step,kind", [("cli_auth", "cli_login"), ("hermes_auth", "hermes_login")])
async def test_review_login_action_stays_visible_during_reobservation(review_setup, monkeypatch, step, kind):
    bootstrap, _, runner, hermes, _ = review_setup
    runner.connected = False
    hermes.runtime.update(state="needs_auth", auth_verified_at=None)
    await checkpoint_at(bootstrap, step)
    await bootstrap.advance()
    waiting = bootstrap.public()
    assert waiting["state"] == "waiting_input" and waiting["human_action"]["kind"] == kind
    entered, release = asyncio.Event(), asyncio.Event()
    target, method = (runner, "call") if step == "cli_auth" else (hermes, "capabilities")
    original = getattr(target, method)

    async def observe(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(target, method, observe)
    task = asyncio.create_task(bootstrap.advance())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        during = bootstrap.public()
        assert during["state"] == "waiting_input"
        assert during["human_action"] == waiting["human_action"]
        assert during["steps"] == waiting["steps"]
        release.set()
        await asyncio.wait_for(task, 1)
        assert bootstrap.public()["human_action"] == waiting["human_action"]
        runner.connected = True
        hermes.runtime.update(state="ready", auth_verified_at="fixture-observed-time")
        await bootstrap.advance()
        assert bootstrap.public()["current_step"] != step
        assert bootstrap.public()["human_action"] is None
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_review_pause_during_status_prevents_new_ha_provision(review_setup, store):
    bootstrap, backend, _, _, _ = review_setup
    await checkpoint_at(bootstrap, "ha_provision")
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_status(method="GET", body=None):
        if method == "GET":
            entered.set()
            await release.wait()
        return await backend(method, body)

    bootstrap.host_request = gated_status
    task = asyncio.create_task(bootstrap.advance())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        store.set_setting("paused", True)
        release.set()
        await asyncio.wait_for(task, 1)
        assert not [body for method, body in backend.calls if method == "POST"], \
            "A new Host provision action was issued after AI pause took effect"
        assert bootstrap.public()["state"] == "blocked"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("changed_gate", ["paused", "hermes_auth_epoch"])
async def test_review_last_ha_readiness_await_rechecks_authority(review_setup, store, monkeypatch, changed_gate):
    bootstrap, backend, _, hermes, _ = review_setup
    await checkpoint_at(bootstrap, "ha_provision")
    entered, release = asyncio.Event(), asyncio.Event()
    original = hermes.capabilities

    async def capabilities():
        snapshot = await original()
        entered.set()
        await release.wait()
        return snapshot

    monkeypatch.setattr(hermes, "capabilities", capabilities)
    task = asyncio.create_task(bootstrap.advance())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        store.set_setting(changed_gate, True if changed_gate == "paused" else "rotated-auth-epoch")
        release.set()
        await asyncio.wait_for(task, 1)
        assert not [body for method, body in backend.calls if method == "POST"]
        assert bootstrap.public()["state"] == "blocked"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_review_pause_during_cli_job_lookup_prevents_inference_submission(review_setup, store, monkeypatch):
    bootstrap, backend, runner, _, _ = review_setup
    await checkpoint_at(bootstrap, "cli_assist")
    entered, release = asyncio.Event(), asyncio.Event()
    original = runner.call

    async def call(method, path, body=None):
        if method == "GET" and path.startswith("/bootstrap/jobs/"):
            entered.set()
            await release.wait()
        return await original(method, path, body)

    monkeypatch.setattr(runner, "call", call)
    task = asyncio.create_task(bootstrap.advance())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        store.set_setting("paused", True)
        release.set()
        await asyncio.wait_for(task, 1)
        assert not [item for item in runner.calls if item[0] == "POST"]
        assert not [body for method, body in backend.calls if method == "POST"]
        assert bootstrap.public()["state"] == "blocked"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_review_native_completion_rechecks_bound_owner_after_readback(review_setup, store, ha, monkeypatch):
    bootstrap, backend, _, _, _ = review_setup
    await checkpoint_at(bootstrap, "ha_native")
    backend.value["bootstrap"]["ha"].update(reachable=True, onboarding_complete=True)
    store.set_setting("owner", "owner")
    entered, release = asyncio.Event(), asyncio.Event()

    async def readback(method, path):
        assert (method, path) == ("GET", "/config")
        entered.set()
        await release.wait()
        return {"version": "fixture-version"}

    monkeypatch.setattr(ha, "request", readback)
    task = asyncio.create_task(bootstrap.advance())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        store.set_setting("owner", None)
        release.set()
        await asyncio.wait_for(task, 1)
        assert bootstrap.public()["state"] != "completed", \
            "Onboarding became complete even though the owner binding was revoked during readback"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_review_concurrent_manual_connect_cannot_replace_first_household(store, tmp_path, monkeypatch):
    entered = {owner: asyncio.Event() for owner in ("first-owner", "second-owner")}
    release = {owner: asyncio.Event() for owner in entered}

    class Candidate:
        def __init__(self, token, base, websocket, external):
            self.owner_id = token

        async def start(self):
            pass

        async def ws(self, operation):
            assert operation == "auth/current_user"
            return {"id": self.owner_id}

        async def owner(self, owner):
            return {"id": owner, "is_owner": True, "is_active": True}

        async def close(self):
            entered[self.owner_id].set()
            await release[self.owner_id].wait()

    monkeypatch.setattr(host, "HomeAssistant", Candidate)
    auth = SimpleNamespace(root=tmp_path)
    ha = SimpleNamespace(base="http://fixture/api", websocket="ws://fixture/api/websocket", session=None, token="")
    first = asyncio.create_task(host.connect_home_owner(auth, ha, store, "first-owner"))
    second = asyncio.create_task(host.connect_home_owner(auth, ha, store, "second-owner"))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 1)
        release["first-owner"].set()
        assert (await asyncio.wait_for(first, 1))["principal"] == "first-owner"
        release["second-owner"].set()
        with pytest.raises(HTTPException) as rejected:
            await asyncio.wait_for(second, 1)
        assert rejected.value.status_code == 409
        assert store.setting("owner") == "first-owner"
        assert (tmp_path / "ha-token").read_text() == "first-owner"
        assert ha.token == "first-owner"
    finally:
        for event in release.values():
            event.set()
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_review_manual_connect_logout_before_commit_keeps_ha_unbound(store, ha, tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    class Candidate:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            pass

        async def ws(self, operation):
            return {"id": "owner"}

        async def owner(self, owner):
            return {"id": owner, "is_owner": True, "is_active": True}

        async def close(self):
            entered.set()
            await release.wait()

    monkeypatch.setattr(host, "HomeAssistant", Candidate)
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("review-fixture-access-key-only")
    ha.base, ha.websocket, ha.session, ha.token = "http://fixture/api", "ws://fixture/api/websocket", None, ""
    app = create_app(store=store, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                 headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "review-fixture-access-key-only"})).status_code == 200
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        task = asyncio.create_task(client.post("/api/host/connect", json={"token": "review-fixture-token-not-live"}))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert (await client.post("/api/host/logout")).status_code == 200
            release.set()
            assert (await asyncio.wait_for(task, 1)).status_code == 403
            assert store.setting("owner") is None and ha.token == ""
            assert not (tmp_path / "ha-token").exists()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_review_revision_deadline_survives_restart_and_retry_does_not_reinstall(
        review_setup, review_clock, store, ha, monkeypatch):
    bootstrap, backend, runner, hermes, gateway = review_setup
    await start_at_install(review_setup)
    backend.finish("bootstrap_hermes")
    hermes.runtime["upstream_commit"] = "c" * 40
    await bootstrap.advance()
    deadline = bootstrap.record()["observation_waits"]["hermes_revision"]["deadline"]
    assert deadline == review_clock[0] + 120
    jobs = bootstrap.record()["jobs"]
    restarted = Bootstrap(store, runner, hermes, gateway, ha, "a" * 32, host_request=backend)
    monkeypatch.setattr(restarted, "resume", lambda: None)
    review_clock[0] += 119
    await restarted.advance()
    assert restarted.public()["state"] == "running"
    review_clock[0] += 1
    await restarted.advance()
    assert restarted.public()["human_action"]["code"] == "hermes_revision_timeout"
    await restarted.retry("revision-observation-retry")
    await restarted.advance()
    new_deadline = restarted.record()["observation_waits"]["hermes_revision"]["deadline"]
    assert new_deadline == review_clock[0] + 120
    assert restarted.record()["jobs"] == jobs
    assert len([body for method, body in backend.calls if method == "POST"]) == 1
    review_clock[0] += 60
    await restarted.retry("revision-observation-retry")
    await restarted.advance()
    assert restarted.record()["observation_waits"]["hermes_revision"]["deadline"] == new_deadline


async def test_review_unavailable_hermes_has_bounded_wait_but_human_auth_does_not(review_setup, review_clock):
    bootstrap, backend, _, hermes, _ = review_setup
    await checkpoint_at(bootstrap, "hermes_auth")
    hermes.runtime.update(state="unavailable", auth_verified_at=None)
    await bootstrap.advance()
    review_clock[0] += 120
    await bootstrap.advance()
    assert bootstrap.public()["human_action"]["code"] == "hermes_runtime_timeout"
    await bootstrap.retry("runtime-observation-retry")
    hermes.runtime["state"] = "needs_auth"
    await bootstrap.advance()
    review_clock[0] += 86400
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "waiting_input"
    assert bootstrap.public()["human_action"]["kind"] == "hermes_login"
    assert not [body for method, body in backend.calls if method == "POST"]


async def test_review_successful_verification_without_readback_times_out_without_new_inference(review_setup, review_clock):
    bootstrap, backend, _, hermes, gateway = review_setup
    await checkpoint_at(bootstrap, "hermes_auth")
    hermes.runtime.update(state="configured", auth_verified_at=None)
    await bootstrap.advance()
    verification = bootstrap.record()["verification"]
    gateway.messages[verification["event"]]["state"] = "succeeded"
    await bootstrap.advance()
    review_clock[0] += 120
    await bootstrap.advance()
    assert bootstrap.public()["human_action"]["code"] == "hermes_readback_timeout"
    await bootstrap.retry("readback-observation-retry")
    await bootstrap.advance()
    assert bootstrap.record()["verification"] == verification
    assert len(gateway.messages) == 1
    assert bootstrap.public()["state"] == "running"
    assert not [body for method, body in backend.calls if method == "POST"]


@pytest.mark.parametrize("transport_error", [False, True])
async def test_review_unconfirmed_runner_preserves_job_on_retry(review_setup, monkeypatch, transport_error):
    bootstrap, backend, runner, _, _ = review_setup
    await checkpoint_at(bootstrap, "cli_assist")
    calls = []

    async def call(method, path, body=None):
        calls.append((method, path))
        if transport_error:
            raise RunnerError("stop_unconfirmed", 409)
        return {"state": "stop_unconfirmed", "error": "stop_unconfirmed"}

    monkeypatch.setattr(runner, "call", call)
    await bootstrap.advance()
    original = bootstrap.record()["assist_request"]
    assert bootstrap.public()["human_action"]["code"] == "runner_needs_reconcile"
    await bootstrap.retry("unconfirmed-runner-review")
    await bootstrap.advance()
    assert bootstrap.record()["assist_request"] == original
    assert bootstrap.public()["state"] == "blocked"
    assert all(method == "GET" for method, _ in calls)
    assert not [body for method, body in backend.calls if method == "POST"]


async def test_review_unconfirmed_hermes_never_submits_verification_or_ha_action(review_setup):
    bootstrap, backend, _, hermes, gateway = review_setup
    await checkpoint_at(bootstrap, "hermes_auth")
    hermes.runtime["execution_state"] = "stop_unconfirmed"
    await bootstrap.advance()
    assert bootstrap.public()["human_action"]["code"] == "hermes_needs_reconcile"
    await bootstrap.retry("unconfirmed-hermes-review")
    await bootstrap.advance()
    assert bootstrap.public()["state"] == "blocked"
    assert not gateway.messages and not [body for method, body in backend.calls if method == "POST"]


async def test_review_runner_client_returns_unconfirmed_stop_without_polling(store):
    class UnconfirmedRunner(Runner):
        calls = []

        async def call(self, method, path, data=None, pairing=None):
            self.calls.append((method, path))
            return {"state": "stop_unconfirmed", "error": "stop_unconfirmed"}

    runner = UnconfirmedRunner(store)
    with pytest.raises(RunnerError, match="stop_unconfirmed") as result:
        await asyncio.wait_for(runner.run({"id": "a" * 32}), 1)
    assert result.value.status_code == 409
    assert runner.calls == [("POST", "/jobs"), ("GET", "/jobs/" + "a" * 32)]


async def test_review_lost_pair_reply_recovers_same_runner_identity(store, tmp_path):
    """Real Runner pairing route; simulate only transport loss after its committed response."""
    broker = Broker(tmp_path / "review-runner")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)),
                                     base_url="https://runner") as client:
            class LocalRunner(Runner):
                lose_once = True

                async def call(self, method, path, data=None, pairing=None):
                    response = await client.request(method, path, json=data)
                    if response.status_code >= 300:
                        raise RunnerError(response.json()["detail"], response.status_code)
                    if path == "/pair" and self.lose_once:
                        self.lose_once = False
                        raise RunnerError("fixture: response lost after Runner committed pairing", 503)
                    return response.json()

            runner = LocalRunner(store)
            pairing = {"host": "runner", "port": 8443, "fingerprint": broker.fingerprint, "code": broker.code}
            with pytest.raises(RunnerError, match="response lost"):
                await runner.pair(pairing, "host-owner", "a" * 32)
            assert broker.setting("token_hash") and store.setting("runner") is None
            assert await runner.pair(pairing, "host-owner", "a" * 32) == {"paired": True}
            assert store.setting("runner")["fingerprint"] == broker.fingerprint
    finally:
        broker.db.close()
