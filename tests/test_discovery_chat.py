"""HTTP/Hermes/MCP discovery fixtures; no cloud, physical devices or HA writes."""
import json
import uuid

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.ha import HAUnreachable
from agentic_controller.mcp import TOOLS, create_mcp
from agentic_controller.models import ChatInput, ConversationDiscovery, digest
from agentic_hermes.models import TOOLS as RUNTIME_TOOLS
from fastapi import HTTPException
from test_discovery import DiscoveryHA
from test_hermes_controller import Bridge, send


@pytest.fixture
async def discovery_chat(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("discovery-chat-fixture-key")
    store.set_setting("owner", "owner")
    store.set_setting("policy", {"entities": []})
    ha, bridge = DiscoveryHA(), Bridge()
    ha.token, ha.base, ha.websocket = "fixture-private-token", "http://fixture/api", "ws://fixture/websocket"
    app = create_app(store=store, ha=ha, host_mode=True, hermes_client=bridge)
    tool_results = []
    original_call = bridge.call

    async def call(method, path, body=None, key=None):
        if path.endswith("/runs"):
            async with mcp_client(app, body["mcp_token"]) as mcp:
                response = await rpc(mcp)
            assert response.status_code == 200
            result = response.json()["result"]
            assert not result["isError"]
            tool_results.append(json.loads(result["content"][0]["text"]))
        return await original_call(method, path, body, key)

    bridge.call = call
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
            headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "discovery-chat-fixture-key"})).status_code == 200
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        yield app, client, ha, store, tool_results
    await app.state.gateway.close()


def mcp_client(app, token):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_mcp(app.state.coordinator),
        client=("127.0.0.1", 40000)), base_url="http://127.0.0.1:8097",
        headers={"Authorization": "Bearer " + token})


async def rpc(client, arguments=None, method="tools/call"):
    return await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
        "params": {"name": "home.discover", "arguments": arguments or {}} if method == "tools/call" else {}})


async def active_grant(system, *, consent=True):
    app, client, _, store, _ = system
    response = await send(app, client, "找找可以加入的裝置", uuid.uuid4().hex, discover_devices=consent)
    item = store.conversation(id=response.json()["id"])
    token = app.state.coordinator.grant(item, uuid.uuid4().hex, uuid.uuid4().hex)
    return token, item


async def test_natural_request_returns_action_card_without_household_read(discovery_chat):
    app, client, ha, store, tool_results = discovery_chat
    response = await send(app, client, "找看看有什麼裝置還沒裝，把它們都裝一裝", "natural-request")
    assert response.status_code == 202
    item = store.conversation(id=response.json()["id"])
    result = ConversationDiscovery.model_validate(item["discovery"])
    assert result.state == "consent_required" and result.candidates == result.sources == []
    assert result.content_digest is None and not ha.calls
    assert tool_results == [item["discovery"]]
    assert "home.discover" in TOOLS and set(TOOLS) == set(RUNTIME_TOOLS)
    assert not store.list("task") and not store.list("approval") and not store.list("discovery_setup")


async def test_explicit_turn_shares_only_bounded_ha_candidates_with_no_control_grant(discovery_chat, monkeypatch):
    app, client, ha, store, tool_results = discovery_chat
    flow = ha.results["config_entries/flow/progress"][0]
    flow.update(handler="ipp")
    flow["context"]["title_placeholders"]["name"] = "Study printer"
    flow["context"]["title_placeholders"].update(ip="192.0.2.25", mac="02:00:00:00:00:25")
    before = store.setting("policy")

    def forbidden(*_):
        raise AssertionError("Saved network observations have no chat sharing grant")

    monkeypatch.setattr(app.state.network_inventory, "view", forbidden)
    response = await send(app, client, "請探索家庭設備", "explicit-request", discover_devices=True)
    item = store.conversation(id=response.json()["id"])
    result = ConversationDiscovery.model_validate(item["discovery"])
    assert result.state == "ready" and result.candidate_count == 1
    candidate = result.candidates[0]
    assert candidate.title == "Study printer" and candidate.integration_name == "網路印表機"
    assert candidate.source_id == "ha_discovery" and candidate.native_path == "/config/integrations"
    assert candidate.next_step and len(ha.calls) == 6
    assert result.content_digest == digest(result.model_dump(exclude={"content_digest"}))
    assert tool_results[-1] == item["discovery"]
    raw = json.dumps(item["discovery"])
    for private in ("192.0.2.25", "02:00:00:00:00:25", "hidden-secret", "fixture-private-token",
                    "private-serial", "untrusted.invalid", "light.one", "Same name"):
        assert private not in raw
    assert store.setting("policy") == before == {"entities": []}
    assert not store.list("proposal") and not store.list("approval") and not store.list("discovery_setup")


async def test_sharing_is_current_message_only_and_idempotency_binds_choice(discovery_chat):
    app, client, ha, store, tool_results = discovery_chat
    first = await send(app, client, "探索設備", "same-key", discover_devices=True)
    assert (await send(app, client, "探索設備", "same-key", discover_devices=True)).json()["id"] == first.json()["id"]
    assert (await send(app, client, "探索設備", "same-key")).status_code == 409
    reads = len(ha.calls)
    second = await send(app, client, "再幫我看看", "follow-up", reply_to=first.json()["id"])
    assert store.conversation(id=second.json()["id"])["discovery"]["state"] == "consent_required"
    assert len(ha.calls) == reads and len(tool_results) == 2


async def test_shared_candidate_continues_owner_tracking_with_fresh_identity_check(discovery_chat):
    app, client, ha, store, _ = discovery_chat
    response = await send(app, client, "請探索家庭設備", "setup-handoff", discover_devices=True)
    candidate = store.conversation(id=response.json()["id"])["discovery"]["candidates"][0]
    body = {"candidate_id": candidate["id"], "integration_domain": candidate["integration_domain"],
            "source_id": candidate["source_id"]}
    created = await client.post("/api/discovery/setups", json=body, headers={"Idempotency-Key": "owner-choice"})
    assert created.status_code == 200 and created.json()["revision"] == 1
    view = (await client.get("/api/discovery/setups/" + created.json()["id"])).json()
    assert view["integration_state"] == "unselected" and view["physical_verification"] == "not_performed"
    ha.results["config_entries/flow/progress"] = []
    stale = await client.post("/api/discovery/setups", json=body, headers={"Idempotency-Key": "stale-choice"})
    assert stale.status_code == 409
    assert len(store.list("discovery_setup")) == 1 and store.setting("policy") == {"entities": []}
    assert not store.list("approval") and not store.list("proposal")


async def test_default_false_retains_legacy_request_digest(discovery_chat):
    app, client, _, store, _ = discovery_chat
    response = await send(app, client, "ordinary question", "legacy-key")
    item = store.conversation(id=response.json()["id"])
    legacy = ChatInput(message="ordinary question", coordinator="hermes").model_dump(
        exclude={"reply_to", "new_conversation", "discover_devices"})
    assert item["request_digest"] == digest(legacy)
    assert (await send(app, client, "ordinary question", "legacy-key", discover_devices=False)).status_code == 202


@pytest.mark.parametrize("arguments", [{"discover_devices": True}, {"consent": True},
    {"scope_id": "unreviewed-network"}, {"approved": True}, {"candidate_id": "pending"}])
async def test_model_cannot_supply_sharing_scanning_or_setup_authority(discovery_chat, arguments):
    app, _, ha, _, _ = discovery_chat
    token, _ = await active_grant(discovery_chat, consent=False)
    async with mcp_client(app, token) as client:
        result = (await rpc(client, arguments)).json()["result"]
    assert result["isError"] and not ha.calls


@pytest.mark.parametrize("fault", ["partial", "unavailable", "unsupported", "empty", "bounded"])
async def test_coverage_never_turns_unknown_into_empty_home(discovery_chat, fault):
    app, client, ha, store, _ = discovery_chat
    rows = ha.results["config_entries/flow/progress"]
    if fault == "partial":
        rows.append({"invalid": "row"})
    elif fault == "unavailable":
        ha.results["config_entries/flow/progress"] = HAUnreachable("private-ha-failure")
    elif fault == "unsupported":
        ha.results["config_entries/flow/progress"] = {"new": "shape"}
    elif fault == "empty":
        rows.clear()
    else:
        rows.extend({**rows[0], "flow_id": f"pending-{i}"} for i in range(12))
    response = await send(app, client, "請探索家庭設備", "coverage", discover_devices=True)
    result = store.conversation(id=response.json()["id"])["discovery"]
    source = next(item for item in result["sources"] if item["id"] == "ha_discovery")
    assert result["limitations"] and "private-ha-failure" not in json.dumps(result)
    if fault in {"unavailable", "unsupported"}:
        assert result["state"] == "unavailable" and result["candidate_count"] is None
    else:
        assert result["state"] == "ready"
    if fault == "partial":
        assert source["state"] == "partial" and result["candidate_count"] is None
        assert len(result["candidates"]) == 1
    if fault == "empty":
        assert source["state"] == "ok" and result["candidate_count"] == 0
    if fault == "bounded":
        assert result["truncated"] and result["candidate_count"] == 13
        assert len(result["candidates"]) == 8


async def test_generic_integration_names_do_not_invent_device_models(discovery_chat):
    app, client, ha, store, _ = discovery_chat
    ha.results["config_entries/flow/progress"] = [{"flow_id": domain, "handler": domain,
        "context": {"source": "discovery"}} for domain in ("matter", "home_connect", "synology_dsm", "unknown_brand")]
    response = await send(app, client, "請探索家庭設備", "names", discover_devices=True)
    result = store.conversation(id=response.json()["id"])["discovery"]
    titles = {row["integration_domain"]: row["title"] for row in result["candidates"]}
    assert titles == {"matter": "Matter 裝置", "home_connect": "Home Connect 家電",
                      "synology_dsm": "Synology 網路儲存設備", "unknown_brand": "待確認的設備或服務"}
    assert all(word not in json.dumps(result) for word in ("Bosch", "Echo", "HP", "洗碗機"))


@pytest.mark.parametrize("change", ["owner", "token", "base", "websocket", "epoch", "policy", "revoked"])
async def test_changed_authority_during_read_never_returns_new_observations(discovery_chat, change):
    app, _, ha, store, _ = discovery_chat
    token, item = await active_grant(discovery_chat)
    old = store.conversation(id=item["id"])["discovery"]
    original = ha.read

    async def changed(target):
        value = await original(target)
        if change in {"token", "base", "websocket"}:
            setattr(ha, change, "changed")
        elif change == "owner":
            store.set_setting("owner", "different")
        elif change == "epoch":
            store.set_setting("hermes_web_epoch", "changed")
        elif change == "policy":
            store.set_setting("policy", {"entities": [{"entity_id": "light.changed", "read": True}]})
        else:
            ha.revoked = True
        return value

    ha.read = changed
    async with mcp_client(app, token) as client:
        response = await rpc(client)
    assert response.status_code == 403 and "Desk light" not in response.text
    assert store.conversation(id=item["id"])["discovery"] == old


@pytest.mark.parametrize("change", ["logout", "connection", "remote_owner"])
async def test_response_durability_rechecks_discovery_authority(discovery_chat, monkeypatch, change):
    app, _, ha, store, _ = discovery_chat
    token, _ = await active_grant(discovery_chat)
    original = store.durable

    async def durability():
        await original()
        if change == "logout":
            store.set_setting("hermes_web_epoch", "logged-out")
        elif change == "connection":
            ha.token = "replaced-token"
        else:
            ha.revoked = True

    monkeypatch.setattr(store, "durable", durability)
    async with mcp_client(app, token) as client:
        response = await rpc(client)
    assert response.status_code == 403 and "Desk light" not in response.text


@pytest.mark.parametrize("failure", [HAUnreachable("private owner failure"), TimeoutError()])
async def test_unbound_or_disconnected_home_reports_no_candidates(discovery_chat, failure):
    app, client, ha, store, _ = discovery_chat
    store.set_setting("owner", None)
    client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
    response = await send(app, client, "請探索家庭設備", "unbound", discover_devices=True)
    result = store.conversation(id=response.json()["id"])["discovery"]
    assert result["state"] == "unavailable" and not result["candidates"] and not ha.calls
    store.set_setting("owner", "owner")
    client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]

    async def offline(_):
        raise failure

    ha.owner = offline
    token, _ = await active_grant(discovery_chat)
    result = await app.state.coordinator.discovery.discover(token)
    assert result["state"] == "unavailable" and "private owner failure" not in json.dumps(result)


async def test_revoked_grant_cannot_reuse_prior_turn_consent(discovery_chat):
    app, _, ha, _, _ = discovery_chat
    token, _ = await active_grant(discovery_chat)
    grant = app.state.coordinator.authorize(token)
    app.state.coordinator.revoke(grant["run_id"])
    reads = len(ha.calls)
    with pytest.raises(HTTPException):
        await app.state.coordinator.discovery.discover(token)
    assert len(ha.calls) == reads
