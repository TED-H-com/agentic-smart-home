"""Controller/MCP protocol fixtures; no cloud login or E1 coding acceptance."""
import asyncio
import json
import time
import uuid

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.hermes import HermesError
from agentic_controller.mcp import TOOLS, create_mcp
from agentic_controller.models import ChatInput
from fastapi import HTTPException


class Bridge:
    def __init__(self):
        self.calls, self.runs = [], {}
        self.lost = False

    async def call(self, method, path, body=None, key=None):
        self.calls.append((method, path, body, key))
        if path == "/v1/capabilities":
            return {"state": "needs_auth", "auth_verified_at": None}
        if path == "/v1/sessions":
            return {"session_id": "a" * 32, **body}
        if path.endswith("/runs"):
            result = {"run_id": body["run_id"], "state": "succeeded", "answer": "fixture answer"}
            self.runs[body["run_id"]] = result
            if self.lost:
                raise HermesError()
            return result
        if path.startswith("/v1/runs/"):
            return self.runs.get(path.split("/")[3].split("?")[0], {"state": "cancelled"})
        return {"state": "configured", "auth_verified_at": None}


@pytest.fixture
async def system(store, ha, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("fixture-machine-access-key")
    bridge = Bridge()
    app = create_app(store=store, ha=ha, host_mode=True, hermes_client=bridge)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                 headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "fixture-machine-access-key"})).status_code == 200
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        yield app, client, bridge
    for task in list(app.state.running.values()):
        task.cancel()
    await asyncio.gather(*list(app.state.running.values()), return_exceptions=True)


async def send(app, client, message, key, **extra):
    response = await client.post("/api/gateway/messages", json={"message": message, "coordinator": "hermes", **extra},
                                 headers={"Idempotency-Key": key})
    await asyncio.gather(*list(app.state.running.values()))
    return response


async def test_hermes_default_channel_route_auth_separation_and_durable_retry(system, store, ha):
    app, client, bridge = system
    bridge.lost = True
    assert (await client.get("/api/gateway")).json()["preferences"]["coordinator"] == "hermes"
    first = await send(app, client, "diagnose host", "once")
    assert first.status_code == 202
    item = store.get("conversation", first.json()["id"])
    assert item["state"] == "succeeded" and item["case_id"] and item["hermes_run_id"]
    assert (await send(app, client, "diagnose host", "once")).json()["id"] == item["id"]
    assert (await send(app, client, "changed", "once")).status_code == 409
    assert len([c for c in bridge.calls if c[1].endswith("/runs")]) == 1
    assert any(c[0] == "GET" and c[1].startswith("/v1/runs/") for c in bridge.calls)
    assert not ha.writes and not ha.calls and not store.list("approval")
    token = next(c[2]["mcp_token"] for c in bridge.calls if c[1].endswith("/runs"))
    with pytest.raises(HTTPException):
        app.state.coordinator.authorize(token)
    assert token not in json.dumps(store.list("mcp_grant"))
    public = (await client.get("/api/hermes/cases")).text
    assert item["scope"] not in public and token not in public


async def test_hermes_auth_works_without_ha_and_does_not_verify_configuration(system, store, ha):
    app, client, bridge = system
    ha.revoked = True
    assert (await client.get("/api/hermes")).json()["runtime"]["state"] == "needs_auth"
    response = await client.post("/api/hermes/auth", json={"model": "provider/model", "api_key": "fixture-key-never-real"})
    assert response.status_code == 200 and response.json()["state"] == "configured"
    assert response.json()["auth_verified_at"] is None
    assert "fixture-key-never-real" not in app.state.store.path.read_bytes().decode(errors="ignore")
    client.headers["X-Agentic-CSRF"] = "invalid"
    assert (await client.post("/api/hermes/auth/logout", json={})).status_code == 403
    assert (await client.post("/api/hermes/auth", json={"api_key": "secret"})).status_code == 403


async def test_context_retention_requires_bridge_acknowledgement(system, store):
    _, client, bridge = system
    response = await client.post("/api/retention", json={"audit_days": 30, "context_days": 1})
    assert response.status_code == 200
    assert bridge.calls[-1][:3] == ("POST", "/v1/retention", {"context_days": 1})
    assert store.setting("retention")["context_days"] == 1
    async def offline(*args):
        raise HermesError()
    bridge.call = offline
    assert (await client.post("/api/retention", json={"audit_days": 30, "context_days": 2})).status_code == 503
    assert store.setting("retention")["context_days"] == 1


async def prepare_grant(app, store, key="one", source="web"):
    gateway = app.state.gateway
    item = gateway.submit(ChatInput(message="question", coordinator="hermes"), source, "room", "owner", key,
                          config=None if source == "web" else gateway.config(source))
    await asyncio.gather(*list(app.state.running.values()))
    # Test a currently active case independently of the completed bridge fixture.
    case_id, run_id = uuid.uuid4().hex, uuid.uuid4().hex
    token = app.state.coordinator.grant(item, case_id, run_id)
    return token, item


async def test_mcp_catalog_rejects_live_calls_and_forged_authority(system, store):
    app, _, _ = system
    token, item = await prepare_grant(app, store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mcp(app.state.coordinator),
        client=("127.0.0.1", 40000)), base_url="http://127.0.0.1:8097",
        headers={"Authorization": "Bearer " + token}) as client:
        async def rpc(method, params=None):
            return await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        catalog = (await rpc("tools/list")).json()["result"]["tools"]
        assert {t["name"] for t in catalog} == set(TOOLS)
        for name in ("home.approve", "host.shell", "delegate_task", "filesystem.read"):
            assert (await rpc("tools/call", {"name": name})).json()["result"]["isError"]
        args = {"worker_id": "host-coder", "mode": "research", "idempotency_key": "fixture-1", "goal": "fix bug"}
        for field in ("approved", "endpoint", "credentials", "command", "lease_epoch", "live_mutation"):
            bad = await rpc("tools/call", {"name": "worker.submit", "arguments": {**args, field: "untrusted"}})
            assert bad.json()["result"]["isError"]
        first = json.loads((await rpc("tools/call", {"name": "worker.submit", "arguments": args})).json()["result"]["content"][0]["text"])
        second = json.loads((await rpc("tools/call", {"name": "worker.submit", "arguments": args})).json()["result"]["content"][0]["text"])
        assert first == second and first["state"] == "blocked" and first["execution_started"] is False
        assert (await rpc("tools/call", {"name": "worker.submit", "arguments": {**args, "goal": "different"}})).json()["result"]["isError"]
        assert not store.list("task") and not store.list("approval")
        assert (await client.post("/mcp", headers={"Origin": "http://localhost:8099"}, json={})).status_code == 403
        assert (await client.post("/mcp", headers={"Authorization": "Bearer owner-cookie"}, json={})).status_code == 403
        store.set_setting("paused", True)
        assert (await rpc("tools/list")).status_code == 403


async def test_mcp_evidence_observation_scope_revocation_and_size(system, store, monkeypatch):
    app, _, _ = system
    token, item = await prepare_grant(app, store)
    async def observed():
        return {"vm": "running", "kvm": True, "storage_free_bytes": 123, "private_token": "must-not-leak"}
    monkeypatch.setattr("agentic_controller.hermes.host_call", observed)
    transport = httpx.ASGITransport(app=create_mcp(app.state.coordinator), client=("127.0.0.1", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8097",
        headers={"Authorization": "Bearer " + token}) as client:
        body = {"jsonrpc": "2.0", "id": "id", "method": "tools/call", "params": {"name": "host.status"}}
        response = await client.post("/mcp", json=body)
        evidence = json.loads(response.json()["result"]["content"][0]["text"])
        assert evidence["observed_from"] == "host_broker" and evidence["content_digest"]
        assert "must-not-leak" not in response.text and evidence["limitations"]
        assert (await client.post("/mcp", content=b"x" * 65537)).status_code == 413
        other, _ = await prepare_grant(app, store, "other")
        body["params"] = {"name": "case.evidence.read", "arguments": {"evidence_ids": [evidence["id"]]}}
        assert (await client.post("/mcp", json=body)).json()["result"]["isError"] is False
        assert (await client.post("/mcp", json=body, headers={"Authorization": "Bearer " + other})).json()["result"]["isError"]
        store.set_setting("hermes_auth_epoch", "rotated")
        assert (await client.post("/mcp", json=body)).status_code == 403


async def test_restart_revokes_capabilities_and_cancels_exact_run_without_replay(system, store):
    app, _, bridge = system
    token, item = await prepare_grant(app, store)
    grant = app.state.coordinator.authorize(token)
    store.put("hermes_run", {"id": grant["run_id"], "scope": item["scope"], "state": "running"})
    bridge.calls.clear()
    await app.state.coordinator.recover()
    assert [c[1] for c in bridge.calls] == [f'/v1/runs/{grant["run_id"]}/cancel']
    with pytest.raises(HTTPException):
        app.state.coordinator.authorize(token)
    assert store.get("hermes_run", grant["run_id"])["state"] == "cancelled"
    store.put("hermes_run", {"id": grant["run_id"], "scope": item["scope"], "state": "running"})
    async def offline(*args):
        raise HermesError()
    bridge.call = offline
    await app.state.coordinator.recover()
    assert store.get("hermes_run", grant["run_id"])["state"] == "stop_unconfirmed"


async def test_grant_expiry_and_channel_audience_rotation(system, store):
    app, _, _ = system
    token, item = await prepare_grant(app, store, source="telegram")
    grant = app.state.coordinator.authorize(token)
    store.put("mcp_grant", {**grant, "expires": time.time() - 1})
    with pytest.raises(HTTPException):
        app.state.coordinator.authorize(token)
    store.put("mcp_grant", grant)
    store.set_setting("channel_telegram", {"enabled": False, "chats": [], "senders": [], "secret": "rotated"})
    with pytest.raises(HTTPException):
        app.state.coordinator.authorize(token)


async def test_web_logout_revokes_web_grants_without_revoking_external_audience(system, store):
    app, client, _ = system
    web, _ = await prepare_grant(app, store, key="web")
    external, _ = await prepare_grant(app, store, key="external", source="telegram")
    assert app.state.coordinator.authorize(web)
    assert (await client.post("/api/host/logout", json={})).status_code == 200
    with pytest.raises(HTTPException):
        app.state.coordinator.authorize(web)
    assert app.state.coordinator.authorize(external)


async def test_streamed_mcp_request_rechecks_revocation_and_budget(system, store):
    app, _, _ = system
    token, _ = await prepare_grant(app, store)
    transport = httpx.ASGITransport(app=create_mcp(app.state.coordinator), client=("127.0.0.1", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8097",
        headers={"Authorization": "Bearer " + token}) as client:
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "worker.capabilities"}}).encode()
        started, release = asyncio.Event(), asyncio.Event()
        async def chunks():
            yield raw[:20]
            started.set()
            await release.wait()
            yield raw[20:]
        task = asyncio.create_task(client.post("/mcp", content=chunks()))
        await started.wait()
        grant = app.state.coordinator.authorize(token)
        app.state.coordinator.revoke(grant["run_id"])
        release.set()
        assert (await task).status_code == 403
        store.put("mcp_grant", {**grant, "calls": 63})
        arrived, release, both = 0, asyncio.Event(), asyncio.Event()
        async def concurrent_chunks():
            nonlocal arrived
            yield raw[:20]
            arrived += 1
            if arrived == 2:
                both.set()
            await release.wait()
            yield raw[20:]
        tasks = [asyncio.create_task(client.post("/mcp", content=concurrent_chunks())) for _ in range(2)]
        await both.wait()
        release.set()
        responses = await asyncio.gather(*tasks)
        assert sorted(r.json()["result"]["isError"] for r in responses) == [False, True]
        assert store.get("mcp_grant", grant["id"])["calls"] == 64


async def test_cancel_during_dispatch_does_not_accept_late_success(system, store):
    app, client, bridge = system
    started, release = asyncio.Event(), asyncio.Event()
    original = bridge.call
    async def delayed(method, path, body=None, key=None):
        if path.endswith("/runs"):
            started.set()
            await release.wait()
        return await original(method, path, body, key)
    bridge.call = delayed
    response = await client.post("/api/gateway/messages", json={"message": "delayed", "coordinator": "hermes"},
                                 headers={"Idempotency-Key": "delayed"})
    await started.wait()
    item = store.get("conversation", response.json()["id"])
    assert item["hermes_run_id"]
    assert (await client.post(f'/api/hermes/runs/{item["hermes_run_id"]}/cancel', json={})).status_code == 200
    release.set()
    await asyncio.gather(*list(app.state.running.values()))
    assert store.get("conversation", item["id"])["state"] == "failed"
