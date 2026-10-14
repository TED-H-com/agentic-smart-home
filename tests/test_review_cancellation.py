"""Controller cancellation acknowledgements use real routes and fixture Runner replies."""

import asyncio

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.runner import RunnerError


class CancellationRunner:
    def __init__(self, acknowledgement, *, fail_run=False):
        self.acknowledgement, self.fail_run = acknowledgement, fail_run
        self.started = asyncio.Event()
        self.jobs, self.cancellations = [], []

    async def run(self, job):
        self.jobs.append(job)
        self.started.set()
        if self.fail_run:
            raise RunnerError("stop_unconfirmed", 409)
        await asyncio.Future()

    async def call(self, method, path, body=None):
        assert method == "POST" and path.endswith("/cancel")
        self.cancellations.append(path)
        if self.acknowledgement == "lost_reply":
            raise RunnerError("synthetic lost cancellation reply", 503)
        return {"state": self.acknowledgement}


@pytest.mark.parametrize("surface", ["household", "host", "chat_host", "chat_home"])
@pytest.mark.parametrize("acknowledgement", ["cancelled", "stop_unconfirmed", "lost_reply"])
async def test_controller_preserves_unknown_stop_across_cancel_retry_and_recovery(
        store, ha, tmp_path, monkeypatch, surface, acknowledgement):
    await exercise_surface(store, ha, tmp_path, monkeypatch, surface, acknowledgement)


@pytest.mark.parametrize("surface", ["household", "host", "chat_host", "chat_home"])
async def test_runner_stop_unconfirmed_failure_is_not_an_ordinary_failed_task(
        store, ha, tmp_path, monkeypatch, surface):
    await exercise_surface(store, ha, tmp_path, monkeypatch, surface, "stop_unconfirmed", fail_run=True)


async def exercise_surface(store, ha, tmp_path, monkeypatch, surface, acknowledgement, *, fail_run=False):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("review-cancellation-fixture-key")
    store.set_setting("owner", "owner")

    async def host_status(*, store=None):
        return {"vm": "not_created", "kvm": True, "provisioned": False,
                "network": {}, "storage_free_bytes": 123456789}

    monkeypatch.setattr("agentic_controller.host.host_call", host_status)
    monkeypatch.setattr("agentic_controller.gateway.host_call", host_status)
    runner = CancellationRunner(acknowledgement, fail_run=fail_run)
    app = create_app(store=store, ha=ha, runner=runner, inner_runner=runner, host_mode=True)
    if surface == "household":
        route, kind = "/api/tasks", "task"
        body = {"message": "synthetic household cancellation", "provider": "codex"}
    elif surface == "host":
        route, kind = "/api/host/ask", "host_task"
        body = {"message": "synthetic host cancellation", "provider": "codex"}
    else:
        route, kind = "/api/gateway/messages", "conversation"
        body = {"message": "synthetic chat cancellation", "provider": "codex", "coordinator": "direct",
                "runtime": "haos" if surface == "chat_home" else "host"}
    tasks = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                    headers={"Origin": "http://localhost:8099"}) as client:
            assert (await client.post("/api/host/login", json={"key": "review-cancellation-fixture-key"})).status_code == 200
            client.headers.update({"X-Agentic-CSRF": (await client.get("/api/session")).json()["csrf"],
                                   "Idempotency-Key": "review-stop-request"})
            created = await client.post(route, json=body)
            assert created.status_code == 202, created.text
            identifier = created.json()["id"]
            await asyncio.wait_for(runner.started.wait(), 1)
            tasks = list(app.state.running.values())
            if not fail_run:
                if surface == "household":
                    assert (await client.post(f"/api/tasks/{identifier}/cancel")).status_code == 200
                elif surface == "host":
                    assert (await client.post(f"/api/host/tasks/{identifier}/cancel")).status_code == 200
                else:
                    # A service shutdown or scope cancellation enters the same
                    # Gateway coroutine cleanup; there is no chat cancel route.
                    app.state.running["chat-" + identifier].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            item = store.get(kind, identifier)
            expected = ("interrupted" if surface.startswith("chat_") else "cancelled") if acknowledgement == "cancelled" else "needs_reconcile"
            assert item["state"] == expected
            if expected == "needs_reconcile":
                assert item["error_code"] == "stop_unconfirmed"
            assert runner.cancellations == [f"/jobs/{runner.jobs[0]['id']}/cancel"]
            store.recover()
            assert store.get(kind, identifier)["state"] == expected
            retried = await client.post(route, json=body)
            assert retried.status_code == 202 and retried.json()["id"] == identifier
            assert retried.json()["state"] == expected
            assert len(runner.jobs) == 1
    finally:
        for task in list(app.state.running.values()):
            task.cancel()
        await asyncio.gather(*list(app.state.running.values()), return_exceptions=True)
        await app.state.gateway.close()


@pytest.mark.parametrize("acknowledgement", ["cancelled", "stop_unconfirmed"])
async def test_hermes_gateway_cancellation_preserves_coordinator_stop_result(
        store, ha, tmp_path, monkeypatch, acknowledgement):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("review-cancellation-fixture-key")
    entered = asyncio.Event()
    stops = []

    class Bridge:
        async def call(self, method, path, body=None, key=None):
            if path == "/v1/sessions":
                return {"session_id": "a" * 32}
            if path.endswith("/runs"):
                entered.set()
                await asyncio.Future()
            assert path.endswith("/cancel")
            stops.append(path)
            return {"state": acknowledgement}

    app = create_app(store=store, ha=ha, host_mode=True, hermes_client=Bridge())
    tasks = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                    headers={"Origin": "http://localhost:8099"}) as client:
            await client.post("/api/host/login", json={"key": "review-cancellation-fixture-key"})
            client.headers.update({"X-Agentic-CSRF": (await client.get("/api/session")).json()["csrf"],
                                   "Idempotency-Key": "review-hermes-stop"})
            created = await client.post("/api/gateway/messages", json={"message": "synthetic", "coordinator": "hermes"})
            assert created.status_code == 202
            await asyncio.wait_for(entered.wait(), 1)
            tasks = list(app.state.running.values())
            tasks[0].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            item = store.get("conversation", created.json()["id"])
            assert item["state"] == ("interrupted" if acknowledgement == "cancelled" else "needs_reconcile")
            assert store.get("hermes_run", item["hermes_run_id"])["state"] == acknowledgement
            assert stops == [f"/v1/runs/{item['hermes_run_id']}/cancel"]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.gateway.close()
