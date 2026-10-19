"""Selected setup sharing through real Gateway/Hermes/MCP fixture boundaries.

No physical printer, cloud account, or household mutation is exercised here.
"""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.device_setup import KIND
from agentic_controller.ha import HAUnreachable
from agentic_controller.mcp import TOOLS, create_mcp
from agentic_controller.models import ChatInput, ConversationDeviceSetup, digest
from agentic_hermes.models import TOOLS as RUNTIME_TOOLS
from fastapi import HTTPException
from test_device_setup import IPPHA
from test_hermes_controller import Bridge, send


def mcp_client(app, token):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mcp(app.state.coordinator),
        client=("127.0.0.1", 40000)), base_url="http://127.0.0.1:8097",
        headers={"Authorization": "Bearer " + token})


async def rpc(client, arguments=None):
    return await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "home.device_setup_status", "arguments": arguments or {}}})


@pytest.fixture
async def setup_chat(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("setup-chat-fixture-key")
    store.set_setting("owner", "owner")
    store.set_setting("policy", {"entities": []})
    ha, bridge = IPPHA(), Bridge()
    app = create_app(store=store, ha=ha, host_mode=True, hermes_client=bridge)
    results, calls = [], []
    original_call, original_ws, original_request = bridge.call, ha.ws, ha.request

    async def call(method, path, body=None, key=None):
        if path == "/v1/capabilities":
            return {"state": "ready", "tools": list(RUNTIME_TOOLS)}
        if path.endswith("/runs"):
            async with mcp_client(app, body["mcp_token"]) as mcp:
                response = await rpc(mcp)
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert not result["isError"], result
            results.append(json.loads(result["content"][0]["text"]))
        return await original_call(method, path, body, key)

    async def ws(target):
        calls.append(("ws", target))
        return await original_ws(target)

    async def request(method, target, *args, **kwargs):
        calls.append((method, target))
        return await original_request(method, target, *args, **kwargs)

    bridge.call, ha.ws, ha.request = call, ws, request
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
            headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "setup-chat-fixture-key"})).status_code == 200
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        proposal = await client.post("/api/device-setups", json={"candidate_id": "pending"},
                                    headers={"Idempotency-Key": "fixture-proposal"})
        assert proposal.status_code == 200, proposal.text
        proposal = proposal.json()
        response = await client.post("/api/device-setups/" + proposal["id"] + "/approve",
            json={"expected_revision": proposal["revision"], "proposal_digest": proposal["proposal_digest"]},
            headers={"Idempotency-Key": "fixture-approval"})
        assert response.status_code == 202, response.text
        await asyncio.gather(*list(app.state.device_setups.running.values()))
        assert store.get(KIND, proposal["id"])["state"] == "configured"
        calls.clear()
        ha.posts.clear()
        ha.backups.clear()
        yield app, client, ha, store, results, proposal["id"], calls
    await app.state.gateway.close()


async def share(system, key=None, **extra):
    app, client, _, store, _, selected, _ = system
    response = await send(app, client, "請幫我解讀這筆接入結果", key or uuid.uuid4().hex,
                          device_setup_id=selected, **extra)
    assert response.status_code == 202, response.text
    return store.conversation(id=response.json()["id"])


async def active_grant(system):
    item = await share(system)
    token = system[0].state.coordinator.grant(item, uuid.uuid4().hex, uuid.uuid4().hex)
    return token, item


async def test_no_selection_requests_exact_ui_action_without_reading_home(setup_chat):
    app, client, ha, store, results, _, calls = setup_chat
    response = await send(app, client, "幫我看看設備裝好了沒", "no-selection")
    item = store.conversation(id=response.json()["id"])
    result = ConversationDeviceSetup.model_validate(item["device_setup_status"])
    assert result.state == "consent_required" and not result.sources and result.setup_id is None
    assert "設備接入進度" in result.limitations[0] and "請管家解讀結果" in result.limitations[0]
    assert not calls and not ha.posts and not ha.backups
    assert results == [item["device_setup_status"]]
    assert "home.device_setup_status" in TOOLS and set(TOOLS) == set(RUNTIME_TOOLS)


async def test_only_selected_entry_is_shared_and_survives_final_hermes_update(setup_chat):
    _, _, ha, store, results, selected, calls = setup_chat
    ha.entries.append({"entry_id": "private_other_entry", "domain": "hue", "state": "loaded", "title": "Other room"})
    ha.devices.append({"id": "private_other_device", "name": "Other room", "config_entries": ["private_other_entry"]})
    ha.entities.append({"entity_id": "light.private", "device_id": "printer", "config_entry_id": "private_other_entry"})
    ha.states.append({"entity_id": "light.private", "state": "on"})
    ha.devices[0].update(config_entries=["newentry", "private_other_entry"], via_device_id="private_parent")
    before = store.get(KIND, selected)
    item = await share(setup_chat)
    result = ConversationDeviceSetup.model_validate(item["device_setup_status"])
    assert item["state"] == "succeeded" and result == ConversationDeviceSetup.model_validate(results[-1])
    assert result.state == "ready" and result.current_state == "present" and result.setup_state == "configured"
    assert result.integration.id == "newentry" and result.integration.state == "loaded"
    assert (result.device_count, result.entity_count, result.available_entity_count) == (1, 1, 1)
    assert result.devices[0].entity_count == 1 and result.physical_verification == "not_performed"
    assert result.content_digest == digest(result.model_dump(exclude={"content_digest"}))
    assert {source.item_count for source in result.sources} == {1}
    raw = result.model_dump_json()
    for private in ("private_other", "Other room", "private_parent", "private-serial", "private-token",
                    "sensor.ink", "light.private", "fixture-backup", "pending", "proposal_digest"):
        assert private not in raw
    assert calls and not ha.posts and not ha.backups and store.get(KIND, selected) == before
    assert store.setting("policy") == {"entities": []}


async def test_labels_redacted_and_output_bounds_do_not_expose_other_record(setup_chat):
    _, _, ha, store, _, selected, _ = setup_chat
    unsafe = "Printer 192.0.2.25 02:00:00:00:00:25 fe80::beef https://private.invalid/?token=SECRET private-token"
    ha.entries[0]["title"] = unsafe
    ha.devices[0].update(name=unsafe, manufacturer="password=PRIVATE", model="private-token")
    for index in range(20):
        ha.devices.append({"id": f"printer_{index}", "name": f"Printer {index}", "config_entries": ["newentry"]})
    other = {**store.get(KIND, selected), "id": "b" * 32}
    other["candidate"] = {**other["candidate"], "title": "PRIVATE OTHER SETUP"}
    store.put(KIND, other)
    result = (await share(setup_chat))["device_setup_status"]
    assert result["device_count"] == 21 and len(result["devices"]) == 16 and result["truncated"]
    raw = json.dumps(result)
    for private in ("192.0.2.25", "02:00:00:00:00:25", "fe80::beef", "private.invalid", "SECRET",
                    "private-token", "PRIVATE", "b" * 32):
        assert private not in raw


async def test_legacy_idempotency_and_exact_selection_and_reply_noninheritance(setup_chat):
    app, client, _, store, results, selected, calls = setup_chat
    response = await send(app, client, "ordinary", "legacy-key")
    legacy = ChatInput(message="ordinary", coordinator="hermes").model_dump(
        exclude={"reply_to", "new_conversation", "discover_devices", "discover_environment", "device_setup_id"})
    assert store.conversation(id=response.json()["id"])["request_digest"] == digest(legacy)
    first = await share(setup_chat, "same-key")
    again = await share(setup_chat, "same-key")
    assert first["id"] == again["id"]
    assert (await send(app, client, "請幫我解讀這筆接入結果", "same-key")).status_code == 409
    assert (await send(app, client, "請幫我解讀這筆接入結果", "same-key", device_setup_id="a" * 32)).status_code == 409
    reads, tools = len(calls), len(results)
    follow = await send(app, client, "再查一次", "follow", reply_to=first["id"])
    item = store.conversation(id=follow.json()["id"])
    assert item["device_setup_id"] is None and item["device_setup_status"]["state"] == "consent_required"
    assert len(calls) == reads and len(results) == tools + 1 and selected not in json.dumps(item["device_setup_status"])


@pytest.mark.parametrize("value", ["A" * 32, "a" * 31, "a" * 33, "http://private.invalid", 123])
async def test_selection_is_strict_opaque_id(setup_chat, value):
    app, client, _, _, _, _, calls = setup_chat
    response = await send(app, client, "status", "invalid", device_setup_id=value)
    assert response.status_code == 422 and not calls


@pytest.mark.parametrize("source,coordinator", [("web", "direct"), ("telegram", "hermes"), ("line", "direct")])
async def test_selection_rejected_outside_web_hermes(setup_chat, source, coordinator):
    app, _, _, _, _, selected, calls = setup_chat
    with pytest.raises(HTTPException) as exc:
        app.state.gateway.submit(ChatInput(message="status", coordinator=coordinator, device_setup_id=selected),
                                 source, "chat", "owner", "event")
    assert exc.value.status_code == 422 and not calls


@pytest.mark.parametrize("arguments", [{"setup_id": "a" * 32}, {"device_setup_id": "a" * 32}, {"approved": True},
    {"reconcile": True}, {"retry": True}, {"url": "http://private.invalid"}])
async def test_model_cannot_choose_another_operation_or_grant_write(setup_chat, arguments):
    app, _, ha, _, _, _, calls = setup_chat
    token, _ = await active_grant(setup_chat)
    reads = len(calls)
    async with mcp_client(app, token) as client:
        result = (await rpc(client, arguments)).json()["result"]
    assert result["isError"] and len(calls) == reads and not ha.posts and not ha.backups


@pytest.mark.parametrize("fault", ["missing", "unavailable", "partial", "truncated", "entity_failure"])
async def test_historical_success_never_substitutes_for_failed_current_readback(setup_chat, fault):
    _, _, ha, store, _, selected, _ = setup_chat
    original = ha.ws
    if fault == "missing":
        ha.entries.clear()
    elif fault == "partial":
        ha.entries[:] = [{"invalid": True}]
    elif fault == "truncated":
        ha.devices.extend({"id": f"unrelated_{index}", "name": "Other"} for index in range(1000))

    async def ws(target):
        if target == ("config/entity_registry/list" if fault == "entity_failure" else "config_entries/get") and fault in {"unavailable", "entity_failure"}:
            raise HAUnreachable("private-upstream-detail")
        return await original(target)

    ha.ws = ws
    result = (await share(setup_chat))["device_setup_status"]
    assert result["setup_state"] == "configured" and store.get(KIND, selected)["state"] == "configured"
    if fault == "missing":
        assert result["current_state"] == "not_seen" and result["integration"] is None
    elif fault in {"unavailable", "partial"}:
        assert result["state"] == "unavailable" and result["current_state"] == "unknown" and result["integration"] is None
        assert result["device_count"] is None and result["devices"] == []
    elif fault == "truncated":
        assert result["current_state"] == "present" and result["truncated"] and result["device_count"] is None
        assert result["entity_count"] is None
    else:
        assert result["current_state"] == "present" and result["entity_count"] is None
        assert result["available_entity_count"] is None
    assert "private-upstream-detail" not in json.dumps(result) and not ha.posts and not ha.backups


async def test_same_house_credential_rotation_reads_history_but_never_reconciles_or_writes(setup_chat):
    _, _, ha, store, _, selected, _ = setup_chat
    before = store.get(KIND, selected)
    ha.token = "replacement-token"
    result = (await share(setup_chat))["device_setup_status"]
    assert result["current_state"] == "present" and result["setup_state"] == "configured"
    assert store.get(KIND, selected) == before and not ha.posts and not ha.backups


@pytest.mark.parametrize("state,phase", [("proposed", "proposal"), ("needs_reconcile", "confirmation_prepared")])
async def test_no_recorded_entry_is_not_a_claim_that_confirmation_was_unsent(setup_chat, state, phase):
    _, _, ha, store, _, selected, calls = setup_chat
    record = {**store.get(KIND, selected), "state": state, "phase": phase, "entry_id": None}
    store.put(KIND, record)
    result = (await share(setup_chat))["device_setup_status"]
    assert result["setup_state"] == state and result["current_state"] == "not_recorded"
    assert not result["sources"] and result["integration"] is None and result["device_count"] is None
    assert any("不代表請求未送出" in value for value in result["limitations"])
    assert not calls and not ha.posts and not ha.backups and store.get(KIND, selected) == record


@pytest.mark.parametrize("failure", [HAUnreachable("private-owner-outage"), TimeoutError()])
async def test_unreachable_owner_returns_no_historical_or_current_household_data(setup_chat, failure):
    _, _, ha, _, _, selected, calls = setup_chat
    async def owner(_):
        raise failure
    ha.owner = owner
    result = (await share(setup_chat))["device_setup_status"]
    assert result["state"] == "unavailable" and result["setup_id"] == selected
    assert result["setup_state"] is None and result["title"] is None and not result["sources"]
    assert result["integration"] is None and not calls and "private-owner-outage" not in json.dumps(result)


async def test_revoked_bridge_scope_cannot_read_selected_operation(setup_chat, monkeypatch):
    app, _, ha, store, _, _, calls = setup_chat
    token, item = await active_grant(setup_chat)
    reads = len(calls)
    before = store.conversation(id=item["id"])["device_setup_status"]
    monkeypatch.setattr(app.state.gateway, "bridge_valid", lambda _: False)
    async with mcp_client(app, token) as client:
        response = await rpc(client)
    assert response.status_code == 403 and len(calls) == reads and not ha.posts
    assert store.conversation(id=item["id"])["device_setup_status"] == before


@pytest.mark.parametrize("change", ["owner", "base", "unknown_id"])
async def test_selection_cannot_read_other_home_or_owner_history(setup_chat, change):
    app, client, ha, store, _, selected, _ = setup_chat
    if change == "owner":
        store.set_setting("owner", "new-owner")
        async def owner(value):
            return {"id": value, "is_owner": True}
        ha.owner = owner
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
    elif change == "base":
        ha.base = "http://different-house/api"
    else:
        selected = "f" * 32
    response = await send(app, client, "status", "different", device_setup_id=selected)
    assert response.status_code == 202, response.text
    result = store.conversation(id=response.json()["id"])["device_setup_status"]
    assert result["state"] == "unavailable" and result["setup_state"] is None and result["title"] is None
    assert not result["devices"] and not result["sources"]


@pytest.mark.parametrize("change", ["owner", "token", "base", "websocket", "epoch", "policy", "revoked", "selection", "revision", "grant"])
async def test_changes_during_read_block_late_observations(setup_chat, change):
    app, _, ha, store, _, selected, _ = setup_chat
    token, item = await active_grant(setup_chat)
    before = store.conversation(id=item["id"])["device_setup_status"]
    original = ha.ws

    async def ws(target):
        value = await original(target)
        if change in {"token", "base", "websocket"}:
            setattr(ha, change, "changed")
        elif change == "owner":
            store.set_setting("owner", "different")
        elif change == "epoch":
            store.set_setting("hermes_web_epoch", "changed")
        elif change == "policy":
            store.set_setting("policy", {"entities": [{"entity_id": "light.changed", "read": True}]})
        elif change == "revoked":
            ha.revoked = True
        elif change == "selection":
            store.put("conversation", {**store.conversation(id=item["id"]), "device_setup_id": "a" * 32})
        elif change == "revision":
            record = store.get(KIND, selected)
            store.put(KIND, {**record, "revision": record["revision"] + 1})
        else:
            app.state.coordinator.revoke(app.state.coordinator.authorize(token)["run_id"])
        return value

    ha.ws = ws
    async with mcp_client(app, token) as client:
        response = await rpc(client)
    assert response.status_code == (409 if change == "revision" else 403), response.text
    assert "Office printer" not in response.text
    assert store.conversation(id=item["id"])["device_setup_status"] == before


@pytest.mark.parametrize("change", ["owner", "token", "revoked", "revision", "epoch"])
async def test_durable_response_boundary_rechecks_current_authority_and_revision(setup_chat, monkeypatch, change):
    app, _, ha, store, _, selected, _ = setup_chat
    token, _ = await active_grant(setup_chat)
    original = store.durable

    async def durable():
        await original()
        if change == "owner":
            store.set_setting("owner", "different")
        elif change == "token":
            ha.token = "replacement"
        elif change == "revoked":
            ha.revoked = True
        elif change == "epoch":
            store.set_setting("hermes_web_epoch", "changed")
        else:
            record = store.get(KIND, selected)
            store.put(KIND, {**record, "revision": record["revision"] + 1})

    monkeypatch.setattr(store, "durable", durable)
    async with mcp_client(app, token) as client:
        response = await rpc(client)
    assert response.status_code == (409 if change == "revision" else 403), response.text
    assert "Office printer" not in response.text


async def test_expired_context_label_is_not_reshared_and_setup_journal_is_unchanged(setup_chat):
    _, _, _, store, _, selected, _ = setup_chat
    record = store.get(KIND, selected)
    record["created_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store.put(KIND, record)
    result = (await share(setup_chat))["device_setup_status"]
    assert result["title"] == "設備接入" and result["current_state"] == "present"
    assert store.get(KIND, selected) == record


@pytest.mark.parametrize("tools", [None, [], "home.device_setup_status", {"home.device_setup_status": True}])
async def test_older_runtime_keeps_history_and_normal_chat_available_without_model_dispatch(setup_chat, tools):
    app, client, ha, store, results, selected, calls = setup_chat
    runtime = app.state.coordinator.client
    original, requests = runtime.call, []

    async def old_runtime(method, path, body=None, key=None):
        requests.append((method, path))
        if path == "/v1/capabilities":
            return {"state": "ready", "tools": tools}
        return await original(method, path, body, key)

    runtime.call = old_runtime
    before = store.get(KIND, selected)
    item = await share(setup_chat, "older-runtime")
    assert item["state"] == "awaiting_user" and item["error_code"] == "device_setup_status_unavailable"
    assert "設備接入進度" in item["answer"] and "登入" not in item["answer"]
    assert requests == [("GET", "/v1/capabilities")] and not results and not calls
    assert not store.list("hermes_run") and not store.list("mcp_grant")
    assert (await share(setup_chat, "older-runtime"))["id"] == item["id"]
    assert requests == [("GET", "/v1/capabilities")]
    assert (await client.get("/api/device-setups/" + selected)).status_code == 200
    follow = await send(app, client, "先聊別的", "ordinary-follow", reply_to=item["id"])
    assert store.conversation(id=follow.json()["id"])["state"] == "succeeded"
    assert len([path for _, path in requests if path.endswith("/runs")]) == 1
    assert store.get(KIND, selected) == before and not ha.posts and not ha.backups


async def test_scope_changed_during_runtime_capability_check_never_starts_a_session(setup_chat):
    app, client, _, store, _, selected, calls = setup_chat
    requests = []

    async def changed_runtime(method, path, body=None, key=None):
        requests.append((method, path))
        store.set_setting("hermes_web_epoch", "changed")
        return {"state": "ready", "tools": list(RUNTIME_TOOLS)}

    app.state.coordinator.client.call = changed_runtime
    response = await send(app, client, "解讀結果", "changed-capability", device_setup_id=selected)
    item = store.conversation(id=response.json()["id"])
    assert item["state"] == "failed" and item["error_code"] == "scope_mismatch"
    assert requests == [("GET", "/v1/capabilities")] and not calls
    assert not store.list("hermes_case") and not store.list("hermes_run") and not store.list("mcp_grant")
