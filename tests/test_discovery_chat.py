"""HTTP/Hermes/MCP discovery fixtures; no cloud, physical devices or HA writes."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agentic_controller import discovery_chat as chat_module
from agentic_controller import network_inventory
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


async def active_grant(system, *, consent=True, environment=False):
    app, client, _, store, _ = system
    response = await send(app, client, "找找可以加入的裝置", uuid.uuid4().hex,
                          discover_devices=consent, discover_environment=environment)
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
        exclude={"reply_to", "new_conversation", "discover_devices", "discover_environment"})
    assert item["request_digest"] == digest(legacy)
    assert (await send(app, client, "ordinary question", "legacy-key", discover_devices=False)).status_code == 202


@pytest.mark.parametrize("arguments", [{"discover_devices": True}, {"discover_environment": True}, {"consent": True},
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


@pytest.fixture
def environment_chat(discovery_chat, monkeypatch):
    calls = []

    async def diagnostic(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        assert (method, path, body) == ("GET", "/network/ha-lan", None)
        return {"contract_version": 1, "mode": "nat", "state": "disabled", "selected_interface_id": None,
                "interface": None, "guest_mac": None, "interfaces": [], "vm_state": "running",
                "can_configure": False, "limitations": []}

    async def scan_forbidden(*args, **kwargs):
        raise AssertionError("Environment sharing cannot start a network scan")

    monkeypatch.setattr(chat_module, "broker_call", diagnostic)
    monkeypatch.setattr(network_inventory, "broker_call", scan_forbidden)
    return discovery_chat, calls


async def import_saved(system, rows=None):
    rows = rows if rows is not None else [{"name": "Saved Hue bridge", "services": ["_hue._tcp.local."],
        "ip": "192.0.2.28", "mac": "02:00:00:00:00:28", "password": "private-import-password"}]
    response = await system[1].post("/api/discovery/inventory/import",
        json={"format": "json", "label": "Private router 192.0.2.1", "content": json.dumps(rows)},
        headers={"Idempotency-Key": uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    return system[3].list(network_inventory.KIND)[0]


async def environment_result(system, key="environment", **flags):
    response = await send(system[0], system[1], "看看家裡已知的設備，並協助接入", key,
                          discover_environment=True, **flags)
    assert response.status_code == 202
    return system[3].conversation(id=response.json()["id"])["discovery"]


@pytest.mark.parametrize("also_candidates", [False, True])
async def test_environment_shares_existing_sources_without_control_permissions_or_scan(environment_chat, also_candidates):
    system, calls = environment_chat
    original = await import_saved(system)
    result = await environment_result(system, discover_devices=also_candidates)
    assert result["share_scope"] == "environment" and result["state"] == "ready"
    environment = result["environment"]
    assert environment["device_count"] == len(environment["devices"]) == 4
    assert environment["integration_count"] == len(environment["integrations"]) == 2
    assert environment["inventory_observation_count"] == 1 and environment["inventory_revision"] == 1
    assert environment["inventory_observations"][0]["name"] == "Saved Hue bridge"
    assert environment["inventory_observations"][0]["observed_at"] == original["observations"][0]["observed_at"]
    assert environment["inventory_observations"][0]["reachability"] == "unknown"
    assert result["candidate_count"] == 2
    assert {item["source_id"] for item in result["candidates"]} == {"ha_discovery", "network_inventory"}
    assert environment["network"]["state"] == "not_configured" and "NAT" in environment["network"]["next_step"]
    assert calls == [("GET", "/network/ha-lan", None)] and len(system[2].calls) == 6
    assert result["content_digest"] == digest({key: value for key, value in result.items() if key != "content_digest"})
    raw = json.dumps(result)
    assert all(value not in raw for value in ("192.0.2.28", "02:00:00:00:00:28", "Private router",
                                              "hidden-secret", "fixture-private-token", "private-import-password", "light.one"))
    assert system[3].setting("policy") == {"entities": []}
    assert not any(system[3].list(kind) for kind in ("approval", "proposal", "discovery_setup"))


async def test_environment_redacts_addresses_links_and_credentials_inside_display_fields(environment_chat):
    system, _ = environment_chat
    name = "Printer 192.0.2.28 02:00:00:00:00:28 fe80::beef https://private.invalid/?token=SECRET"
    await import_saved(system, [{"name": name, "manufacturer": "token=SHOULD_HIDE", "model": "fixture-private-token",
                                "services": ["_hue._tcp", "http://secret.invalid"]}])
    system[2].results["config_entries/flow/progress"][0]["context"]["title_placeholders"]["name"] = name
    system[2].results["config/device_registry/list"][0]["name"] = name
    system[2].results["config_entries/get"][0]["title"] = name
    result = await environment_result(system)
    raw = json.dumps(result)
    assert all(value not in raw for value in ("192.0.2.28", "02:00:00:00:00:28", "fe80::beef", "private.invalid",
                                              "secret.invalid", "SECRET", "SHOULD_HIDE", "fixture-private-token"))
    assert "Printer" in raw and "_hue._tcp" in raw


async def test_legacy_candidate_consent_never_opens_environment_scope(environment_chat, monkeypatch):
    system, calls = environment_chat
    await import_saved(system)
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved inventory requires the explicit wider sharing scope")
    monkeypatch.setattr(system[0].state.network_inventory, "view", forbidden)
    response = await send(system[0], system[1], "只看 HA 候選", "legacy-sharing", discover_devices=True)
    result = system[3].conversation(id=response.json()["id"])["discovery"]
    assert result["share_scope"] == "ha_candidates" and result["environment"] is None and calls == []


async def test_environment_scope_is_per_message_and_changes_receipt(environment_chat):
    system, calls = environment_chat
    app, client, _, store, _ = system
    first = await send(app, client, "探索環境", "environment-key", discover_environment=True)
    assert (await send(app, client, "探索環境", "environment-key", discover_environment=True)).json()["id"] == first.json()["id"]
    assert (await send(app, client, "探索環境", "environment-key", discover_devices=True)).status_code == 409
    second = await send(app, client, "再看看", "no-inherited-scope", reply_to=first.json()["id"])
    assert store.conversation(id=second.json()["id"])["discovery"]["state"] == "consent_required"
    assert len(calls) == 1


async def test_environment_partial_ha_preserves_saved_and_registered_evidence(environment_chat):
    system, _ = environment_chat
    await import_saved(system)
    system[2].results["config_entries/flow/progress"] = HAUnreachable("private-upstream-error")
    system[2].results["config/device_registry/list"].append({"invalid": True})
    result = await environment_result(system)
    assert result["state"] == "ready" and result["candidate_count"] is None
    assert result["environment"]["device_count"] is None and len(result["environment"]["devices"]) == 4
    assert result["candidates"][0]["source_id"] == "network_inventory"
    assert "private-upstream-error" not in json.dumps(result)


async def test_environment_bounds_each_population_and_keeps_both_candidate_sources(environment_chat):
    system, _ = environment_chat
    await import_saved(system, [{"name": f"Hue {index}", "services": ["_hue._tcp"]} for index in range(20)])
    system[2].results["config_entries/flow/progress"] = [{"flow_id": f"pending_{index}", "handler": "hue",
        "context": {"source": "zeroconf"}} for index in range(20)]
    for index in range(20):
        system[2].results["config/device_registry/list"].append({"id": f"device_{index}", "name": "Fixture"})
        system[2].results["config_entries/get"].append({"entry_id": f"entry_{index}", "domain": "fixture", "state": "loaded"})
    result = await environment_result(system)
    assert result["candidate_count"] == 40 and len(result["candidates"]) == 8 and result["truncated"]
    assert {item["source_id"] for item in result["candidates"]} == {"ha_discovery", "network_inventory"}
    environment = result["environment"]
    assert environment["truncated"] and environment["device_count"] == 24 and environment["integration_count"] == 22
    assert environment["inventory_observation_count"] == 20
    assert all(len(environment[key]) == 16 for key in ("devices", "integrations", "inventory_observations"))


@pytest.mark.parametrize("expired_source", [False, True])
async def test_environment_saved_inventory_keeps_age_and_rejects_stale_recommendations(environment_chat, expired_source):
    system, _ = environment_chat
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    record = await import_saved(system, [{"name": "Old Hue bridge", "services": ["_hue._tcp"], "last_seen": old}])
    if expired_source:
        record["sources"][0]["observed_at"] = old
        record["observations"][0]["observed_at"] = old
        system[3].put(network_inventory.KIND, record)
    result = await environment_result(system)
    assert all(item["source_id"] == "ha_discovery" for item in result["candidates"])
    row = result["environment"]["inventory_observations"][0]
    assert row["source_updated_at"] == old
    if expired_source:
        source = next(item for item in result["environment"]["inventory_sources"] if item["id"] == "router_import")
        assert source["state"] == "expired" and source["observed_at"] == old
        assert row["observed_at"] == old and result["candidate_count"] is None


@pytest.mark.parametrize("change", ["revision", "expiry", "owner", "connection", "supervisor", "external", "logout"])
async def test_environment_response_rechecks_saved_inventory_and_owner_authority(environment_chat, monkeypatch, change):
    system, _ = environment_chat
    app, _, ha, store, _ = system
    record = await import_saved(system)
    token, _ = await active_grant(system, environment=True)
    original = store.durable

    async def durability():
        await original()
        if change == "revision":
            store.put(network_inventory.KIND, {**record, "revision": record["revision"] + 1})
        elif change == "expiry":
            record["sources"][0]["observed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
            store.put(network_inventory.KIND, record)
        elif change == "owner":
            ha.revoked = True
        elif change == "connection":
            ha.token = "changed-token"
        elif change in {"supervisor", "external"}:
            setattr(ha, change, "changed-address")
        else:
            store.set_setting("hermes_web_epoch", "logged-out")

    monkeypatch.setattr(store, "durable", durability)
    async with mcp_client(app, token) as client:
        response = await rpc(client)
    assert response.status_code == (409 if change in {"revision", "expiry"} else 403)
    assert "Saved Hue bridge" not in response.text and "Same name" not in response.text


async def test_environment_does_not_reuse_inventory_from_other_home_connection(environment_chat):
    system, _ = environment_chat
    await import_saved(system)
    system[2].token = "new-home-token"
    result = await environment_result(system)
    assert result["environment"]["inventory_revision"] == 0
    assert result["environment"]["inventory_observations"] == []


async def test_environment_owner_unavailable_never_reads_saved_inventory(environment_chat, monkeypatch):
    system, calls = environment_chat
    token, _ = await active_grant(system)
    item = system[3].conversation(id=system[0].state.coordinator.authorize(token)["message_id"])
    system[3].put("conversation", {**item, "discover_environment": True})

    async def offline(_):
        raise HAUnreachable("private-owner-error")

    def forbidden(*args, **kwargs):
        raise AssertionError("No saved household observations without current Owner verification")

    monkeypatch.setattr(system[2], "owner", offline)
    monkeypatch.setattr(system[0].state.network_inventory, "view", forbidden)
    result = await system[0].state.coordinator.discovery.discover(token)
    assert result["state"] == "unavailable" and result["environment"] is None and calls == []


@pytest.mark.parametrize("state", ["waiting_for_address", "adapter_selection_required", "callback_address_required", "ready"])
async def test_environment_network_diagnostic_projects_prerequisite_without_addresses(environment_chat, monkeypatch, state):
    system, _ = environment_chat

    async def host(*args, **kwargs):
        return {"contract_version": 1, "mode": "wired", "state": "configured", "selected_interface_id": "a" * 64,
                "interface": "eth0", "guest_mac": "02:00:00:00:00:28", "interfaces": [
                    {"id": "a" * 64, "label": "Private interface", "interface": "eth0", "address": "192.0.2.1",
                     "network": "192.0.2.0/24"}], "vm_state": "running", "can_configure": False, "limitations": []}

    async def home(*args, **kwargs):
        return {"state": state, "reason": "fixture_prerequisite", "interface": "eth1", "ipv4": ["192.0.2.28"],
                "internal_url": "http://192.0.2.28:8123"}

    monkeypatch.setattr(chat_module, "broker_call", host)
    monkeypatch.setattr(chat_module, "observe_home_lan", home)
    result = await environment_result(system)
    network = result["environment"]["network"]
    assert network["state"] == state and network["mode"] == "wired" and network["next_step"]
    assert all(value not in json.dumps(result) for value in ("192.0.2", "02:00:00:00:00:28", "Private interface", "eth1"))


async def test_environment_network_failure_and_corrupt_inventory_preserve_ha_context(environment_chat, monkeypatch):
    system, _ = environment_chat
    record = await import_saved(system)
    system[3].put(network_inventory.KIND, {**record, "version": 999})

    async def unsupported(*args, **kwargs):
        return {"contract_version": 999, "private": "private-network-error"}

    monkeypatch.setattr(chat_module, "broker_call", unsupported)
    result = await environment_result(system)
    assert result["state"] == "ready" and result["candidate_count"] is None
    environment = result["environment"]
    assert environment["devices"] and environment["inventory_observations"] == []
    assert {source["state"] for source in environment["inventory_sources"]} == {"unavailable"}
    assert environment["network"]["state"] == "unavailable" and "NAT" not in environment["network"]["next_step"]
    assert "private-network-error" not in json.dumps(result)


def test_environment_sharing_does_not_switch_direct_coordinator():
    body = ChatInput(message="探索設備", discover_environment=True)
    assert body.coordinator == "direct"
