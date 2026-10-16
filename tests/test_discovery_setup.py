"""Passive tracking with synthetic HA replies; no household or account acceptance."""
import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.discovery_setup import KIND
from agentic_controller.ha import HAUnreachable
from agentic_controller.models import canonical
from agentic_controller.store import Store
from test_discovery import DiscoveryHA

BASE = "/api/discovery/setups"
CREATE = {"candidate_id": "pending", "integration_domain": "fixture_light"}


@pytest.fixture
async def setup_system(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("setup-only-local-fixture-key")
    store.set_setting("owner", "owner")
    ha = DiscoveryHA()
    ha.token, ha.base, ha.websocket = "private-fixture-token", "http://fixture/api", "ws://fixture/websocket"
    app = create_app(store=store, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                 headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "setup-only-local-fixture-key"})).status_code == 200
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        await store.durable()
        yield app, client, ha, store
    await app.state.gateway.close()


async def create(system, key="create", body=None):
    return await system[1].post(BASE, json=body or CREATE, headers={"Idempotency-Key": key})


async def select(system, identifier, entry="entry", revision=1, key="select"):
    return await system[1].post(f"{BASE}/{identifier}/select", json={"entry_id": entry, "expected_revision": revision},
                                headers={"Idempotency-Key": key})


async def forget(system, identifier, revision=1, key="forget"):
    return await system[1].post(f"{BASE}/{identifier}/forget", json={"expected_revision": revision},
                                headers={"Idempotency-Key": key})


async def test_explicit_tracking_selection_and_fresh_readback_without_effects(setup_system):
    _, client, ha, store = setup_system
    policy = store.setting("policy")
    response = await create(setup_system)
    assert response.status_code == 200
    reference = response.json()
    assert set(reference) == {"contract_version", "id", "revision", "forgotten"}
    identifier = reference["id"]
    record = store.get(KIND, identifier)
    assert record["version"] == 1 and record["baseline_complete"]
    assert record["baseline_entry_ids"] == ["entry", "other"]
    assert record["selected_entry_id"] is None
    assert "private-fixture-token" not in json.dumps(record)
    view = (await client.get(f"{BASE}/{identifier}")).json()
    assert view["candidate_state"] == "present" and view["integration_state"] == "unselected"
    assert view["devices"] == [] and view["device_count"] is None
    assert all(option["known_before"] is True for option in view["options"])
    assert (await select(setup_system, identifier)).json()["revision"] == 2
    ha.results["config_entries/flow/progress"] = []
    view = (await client.get(f"{BASE}/{identifier}")).json()
    assert view["candidate_state"] == "not_seen" and view["integration_state"] == "present"
    assert view["integration"]["id"] == "entry" and view["physical_verification"] == "not_performed"
    assert (view["device_count"], view["entity_count"], view["available_entity_count"]) == (3, 3, 1)
    assert {device["id"] for device in view["devices"]} == {"child", "hub", "legacy"}
    assert all(device["integration_ids"] == ["entry"] for device in view["devices"])
    assert view["native_path"] == "/config/integrations"
    assert all(private not in json.dumps(view) for private in ("binding", "baseline_entry_ids", "hidden-secret", "latitude"))
    assert "devices" not in store.get(KIND, identifier) and "sources" not in store.get(KIND, identifier)
    assert store.setting("policy") == policy
    assert not any(store.list(kind) for kind in ("conversation", "case_evidence", "proposal", "approval", "mcp_grant"))
    assert client.cookies.get("agentic_host")


async def test_records_survive_disk_reopen_and_new_session_without_epoch_binding(setup_system, tmp_path):
    app, client, _, store = setup_system
    identifier = (await create(setup_system)).json()["id"]
    await store.durable()
    reopened = Store(store.path)
    try:
        assert reopened.get(KIND, identifier) == store.get(KIND, identifier)
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 3
        reopened.recover()
        reopened.prune()
        assert reopened.get(KIND, identifier)["selected_entry_id"] is None
    finally:
        reopened.close()
    store.set_setting("hermes_web_epoch", "new-session")
    assert (await client.get(BASE)).json()["setups"][0]["id"] == identifier
    assert app.state.discovery_setups.retention_days() == 7


@pytest.mark.parametrize("field", ["token", "base", "websocket", "owner"])
async def test_persisted_scope_never_crosses_connection_or_owner(setup_system, field):
    _, client, ha, store = setup_system
    identifier = (await create(setup_system)).json()["id"]
    if field == "owner":
        store.set_setting("owner", "other")
        async def owner(value):
            return {"id": value}
        ha.owner = owner
    else:
        setattr(ha, field, "different-connection")
    assert (await client.get(BASE)).json()["setups"] == []
    assert (await client.get(f"{BASE}/{identifier}")).status_code == 404
    assert store.get(KIND, identifier)


async def test_idempotency_deduplication_conflict_and_forgotten_tombstones(setup_system):
    _, client, _, store = setup_system
    first = (await create(setup_system)).json()
    identifier = first["id"]
    assert (await create(setup_system)).json() == first
    assert (await create(setup_system, key="another-tab")).json() == first
    assert len(store.list(KIND)) == 1
    response = await create(setup_system, body={**CREATE, "candidate_id": "other"})
    assert response.status_code == 409 and response.json()["detail"] == "discovery_setup_request_conflict"
    selected = await select(setup_system, identifier)
    assert selected.status_code == 200 and selected.json()["revision"] == 2
    assert (await select(setup_system, identifier)).json() == selected.json()
    assert (await select(setup_system, identifier, entry="other")).status_code == 409
    assert (await select(setup_system, identifier, entry="other", key="stale")).status_code == 409
    removed = await forget(setup_system, identifier, revision=2)
    assert removed.status_code == 200 and removed.json()["forgotten"]
    assert (await forget(setup_system, identifier, revision=2)).json() == removed.json()
    assert (await create(setup_system)).status_code == 410
    assert (await select(setup_system, identifier)).status_code == 410
    assert (await client.get(f"{BASE}/{identifier}")).status_code == 404
    assert store.list(KIND) == []


@pytest.mark.parametrize("partial", [False, True])
async def test_baseline_is_observation_not_automatic_matching(setup_system, partial):
    _, client, ha, _ = setup_system
    if partial:
        ha.results["config_entries/get"].append({"entry_id": "bad", "domain": "invalid/domain"})
    identifier = (await create(setup_system)).json()["id"]
    ha.results["config_entries/flow/progress"] = []
    ha.results["config_entries/get"] = [ha.results["config_entries/get"][0],
        {"entry_id": "new", "domain": "fixture_light", "title": "Desk light", "state": "loaded"}]
    view = (await client.get(f"{BASE}/{identifier}")).json()
    assert view["candidate_state"] == "not_seen" and view["integration_state"] == "unselected"
    assert {option["integration"]["id"]: option["known_before"] for option in view["options"]} == {
        "entry": True, "new": None if partial else False}
    assert view["devices"] == []


@pytest.mark.parametrize("fault", ["missing", "domain_changed", "unavailable"])
async def test_selected_entry_identity_and_failure_never_fall_back_by_name(setup_system, fault):
    _, client, ha, _ = setup_system
    identifier = (await create(setup_system)).json()["id"]
    await select(setup_system, identifier)
    if fault == "unavailable":
        ha.results["config_entries/get"] = HAUnreachable("private-details")
    elif fault == "domain_changed":
        ha.results["config_entries/get"][0]["domain"] = "another_domain"
    else:
        ha.results["config_entries/get"] = ha.results["config_entries/get"][1:]
    view = (await client.get(f"{BASE}/{identifier}")).json()
    assert view["integration_state"] == ("unknown" if fault == "unavailable" else "not_seen")
    assert view["integration"] is None and view["devices"] == [] and view["device_count"] is None
    assert view["setup"]["selected_entry_id"] == "entry"
    response = await select(setup_system, identifier, revision=2, key="invalid-entry")
    assert response.status_code == (503 if fault == "unavailable" else 409)


@pytest.mark.parametrize("target,field", [("config/device_registry/list", "device_count"),
    ("config/entity_registry/list", "entity_count"), ("/states", "available_entity_count")])
async def test_incomplete_sources_never_invent_totals(setup_system, target, field):
    _, client, ha, _ = setup_system
    identifier = (await create(setup_system)).json()["id"]
    await select(setup_system, identifier)
    ha.results[target].append({"invalid": True})
    view = (await client.get(f"{BASE}/{identifier}")).json()
    assert view[field] is None


async def test_candidate_identity_and_native_path_are_server_owned(setup_system):
    _, _, ha, store = setup_system
    assert (await create(setup_system, body={**CREATE, "integration_domain": "wrong"})).status_code == 409
    assert (await create(setup_system, body={**CREATE, "native_path": "https://untrusted.invalid"})).status_code == 422
    ha.results["config_entries/flow/progress"] = HAUnreachable("private")
    assert (await create(setup_system)).status_code == 503
    assert store.list(KIND) == []


async def test_retention_filters_immediately_then_prunes_without_revival(setup_system):
    _, client, _, store = setup_system
    identifier = (await create(setup_system)).json()["id"]
    record = store.get(KIND, identifier)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    record["created_at"] = old
    with store.transaction() as tx:
        tx.execute("UPDATE records SET body=?,created=? WHERE kind=? AND id=?", (canonical(record), old, KIND, identifier))
    store.set_setting("retention_pending", {"context_days": 1, "audit_days": 30})
    assert (await client.get(BASE)).json()["setups"] == []
    assert (await client.get(f"{BASE}/{identifier}")).status_code == 404
    assert (await create(setup_system)).status_code == 410
    assert store.get(KIND, identifier)
    store.prune()
    assert not store.get(KIND, identifier)
    assert (await create(setup_system)).status_code == 410


async def test_list_limit_is_scoped_and_detail_is_bounded_with_complete_totals(setup_system):
    _, client, ha, store = setup_system
    identifier = (await create(setup_system)).json()["id"]
    template = store.get(KIND, identifier)
    for i in range(25):
        store.put(KIND, {**template, "id": f"{i:032x}", "binding": "f" * 64})
    assert len((await client.get(BASE)).json()["setups"]) == 1
    for i in range(19):
        record = copy.deepcopy(template)
        record.update(id=f"{i + 100:032x}")
        record["candidate"]["id"] = f"candidate{i}"
        store.put(KIND, record)
    ha.results["config_entries/flow/progress"][0]["flow_id"] = "newcandidate"
    assert (await create(setup_system, key="full", body={**CREATE, "candidate_id": "newcandidate"})).status_code == 409
    assert len((await client.get(BASE)).json()["setups"]) == 20
    ha.results["config_entries/get"].extend({"entry_id": f"option{i}", "domain": "fixture_light",
        "title": "界" * 160, "state": "loaded"} for i in range(105))
    ha.results["config/device_registry/list"] = [{"id": f"device{i}", "name": "界" * 160,
        "config_entries": ["entry", *[f"other{j}" for j in range(99)]]} for i in range(105)]
    assert (await select(setup_system, identifier)).status_code == 200
    response = await client.get(f"{BASE}/{identifier}")
    view = response.json()
    assert len(view["devices"]) == len(view["options"]) == 100
    assert view["devices_truncated"] and view["options_truncated"] and view["device_count"] == 105
    assert view["entity_count"] == view["available_entity_count"] == 0
    assert len(response.content) < 1048576


@pytest.mark.parametrize("change", ["owner", "token", "base", "websocket", "epoch", "revoked", "updater"])
async def test_create_rechecks_every_authority_after_snapshot_before_commit(setup_system, change):
    _, _, ha, store = setup_system
    original = ha.read
    async def changed(target):
        result = await original(target)
        if change in {"token", "base", "websocket"}:
            setattr(ha, change, "replaced")
        elif change == "owner":
            store.set_setting("owner", "different")
        elif change == "epoch":
            store.set_setting("hermes_web_epoch", "logged-out")
        elif change == "revoked":
            ha.revoked = True
        else:
            store.set_setting("updater_gate", True)
        return result
    ha.read = changed
    response = await create(setup_system)
    assert response.status_code in {401, 403, 423}
    assert not store.list(KIND)
    assert store.db.execute("SELECT count(*) FROM requests WHERE scope LIKE 'discovery_setup:%'").fetchone()[0] == 0


@pytest.mark.parametrize("replacement", ["select", "forget"])
async def test_late_selection_cannot_overwrite_newer_choice_or_restore_forgotten_record(setup_system, replacement):
    _, _, ha, store = setup_system
    identifier = (await create(setup_system)).json()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = ha.read
    blocked = False
    async def read(target):
        nonlocal blocked
        result = await original(target)
        if target == "config_entries/get" and not blocked:
            blocked = True
            entered.set()
            await release.wait()
        return result
    ha.read = read
    late = asyncio.create_task(select(setup_system, identifier, key="late"))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        latest = (await select(setup_system, identifier, entry="other", key="newer") if replacement == "select"
                  else await forget(setup_system, identifier))
        assert latest.status_code == 200
    finally:
        release.set()
    assert (await late).status_code == (409 if replacement == "select" else 404)
    record = store.get(KIND, identifier)
    assert record["selected_entry_id"] == "other" if replacement == "select" else record is None


async def test_concurrent_create_uses_one_record_and_atomic_receipts(setup_system):
    _, _, ha, store = setup_system
    original = ha.read
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    async def read(target):
        nonlocal count
        result = await original(target)
        if target == "config_entries/get":
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
        return result
    ha.read = read
    pending = [asyncio.create_task(create(setup_system, key=key)) for key in ("tab-one", "tab-two")]
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        release.set()
    replies = await asyncio.gather(*pending)
    assert all(reply.status_code == 200 for reply in replies)
    assert replies[0].json()["id"] == replies[1].json()["id"]
    assert len(store.list(KIND)) == 1
    assert store.db.execute("SELECT count(*) FROM requests WHERE scope LIKE 'discovery_setup:%'").fetchone()[0] == 2
