"""Synthetic multi-source observations, never a physical household scan."""
import asyncio
import copy
import json

import httpx
import pytest
from agentic_controller import discovery
from agentic_controller.app import create_app
from agentic_controller.ha import HAError, HAUnreachable
from agentic_controller.mcp import TOOLS
from agentic_controller.models import digest


class DiscoveryHA:
    def __init__(self):
        self.calls, self.revoked = [], False
        self.results = {
            "config_entries/flow/progress": [
                {"flow_id": "pending", "handler": "fixture_light", "context": {
                    "source": "zeroconf", "title_placeholders": {"name": "Desk light", "secret": "hidden-secret"},
                    "unique_id": "private-serial", "configuration_url": "https://untrusted.invalid"}}],
            "config/device_registry/list": [
                {"id": "hub", "name": "Same name", "config_entry_id": "entry", "manufacturer": "Fixture"},
                {"id": "child", "name": "Same name", "config_entry_id": "entry", "parent_device_id": "hub"},
                {"id": "legacy", "name": "Same name", "config_entries": ["entry", "other"], "via_device_id": "hub"},
                {"id": "empty", "name": "No entities", "config_entries": []}],
            "config_entries/get": [
                {"entry_id": "entry", "domain": "fixture_light", "title": "Fixture", "state": "loaded",
                 "source": "user", "data": {"password": "hidden-secret"}},
                {"entry_id": "other", "domain": "fixture_light", "title": "Fixture", "state": "setup_error"}],
            "config/entity_registry/list": [
                {"entity_id": "light.one", "device_id": "child"},
                {"entity_id": "sensor.offline", "device_id": "child"},
                {"entity_id": "sensor.disabled", "device_id": "child", "disabled_by": "user"},
                {"entity_id": "sensor.orphan", "device_id": None}],
            "/states": [{"entity_id": "light.one", "state": "off"},
                        {"entity_id": "sensor.offline", "state": "unavailable"},
                        {"entity_id": "sensor.disabled", "state": "on"}],
            "/config": {"version": "fixture-only", "latitude": 12.34},
        }

    async def owner(self, owner):
        return {"id": "owner", "is_owner": True, "is_active": True} if owner == "owner" and not self.revoked else None

    async def read(self, target):
        self.calls.append(target)
        result = self.results[target]
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    async def ws(self, target):
        return await self.read(target)

    async def request(self, method, target):
        assert method == "GET"
        return await self.read(target)


def source(result, name):
    return next(item for item in result.sources if item.id == name)


async def test_discovery_combines_registries_without_collapsing_identity_or_leaking_config():
    ha = DiscoveryHA()
    result = await discovery.snapshot(ha)
    assert result.candidates[0].title == "Desk light"
    assert len(result.devices) == 4 and len(result.integrations) == 2
    devices = {item.id: item for item in result.devices}
    assert devices["child"].parent_device_id == "hub" and devices["child"].via_device_id is None
    assert devices["legacy"].via_device_id == "hub"
    assert devices["legacy"].integration_ids == ["entry", "other"]
    assert devices["child"].entity_count == 3 and devices["child"].available_entity_count == 1
    assert devices["empty"].entity_count == 0
    assert result.integrations[1].state == "error"
    assert len(ha.calls) == 6
    assert source(result, "ha_states").item_count == 3
    assert all(s.upstream_version == "fixture-only" for s in result.sources if s.location == "home_assistant")
    raw = result.model_dump_json()
    assert all(secret not in raw for secret in ("hidden-secret", "private-serial", "untrusted.invalid", "latitude"))
    assert result.content_digest == digest(result.model_dump(exclude={"content_digest"}))
    assert {s.id for s in result.sources if s.state == "not_implemented"} == {
        "account_inventory"}
    assert "api_availability_is_not_physical_verification" in result.limitations


@pytest.mark.parametrize("method", sorted(discovery.DISCOVERY_METHODS))
async def test_native_discovery_sources_are_candidates(method):
    ha = DiscoveryHA()
    ha.results["config_entries/flow/progress"][0]["context"]["source"] = method
    assert (await discovery.snapshot(ha)).candidates[0].discovery_method == method


@pytest.mark.parametrize("integration,method,step,expected", [
    ("shelly", "zeroconf", "confirm_discovery", "confirm_discovery"),
    ("ipp", "zeroconf", "zeroconf_confirm", "zeroconf_confirm"),
    ("shelly", "zeroconf", "credentials", None),
    ("shelly", "bluetooth", "confirm_discovery", None),
    ("shelly", "zeroconf", "zeroconf_confirm", None),
    ("hue", "zeroconf", "confirm_discovery", None),
    ("shelly", "zeroconf", {"private": "untrusted"}, None),
    ("shelly", "zeroconf", None, None),
])
async def test_only_reviewed_native_confirmation_steps_are_projected(integration, method, step, expected):
    ha = DiscoveryHA()
    flow = ha.results["config_entries/flow/progress"][0]
    flow.update(handler=integration, step_id=step)
    flow["context"]["source"] = method
    result = await discovery.snapshot(ha)
    assert result.candidates[0].confirmation_step == expected
    assert source(result, "ha_discovery").state == "ok"
    assert len(ha.calls) == 6  # Projection never fetches or submits a form.


@pytest.mark.parametrize("method", ["reauth", "user", "reconfigure", "ignore", "import", "system"])
async def test_other_flows_are_not_new_device_candidates(method):
    ha = DiscoveryHA()
    ha.results["config_entries/flow/progress"][0]["context"]["source"] = method
    result = await discovery.snapshot(ha)
    assert result.candidates == [] and source(result, "ha_discovery").state == "ok"


async def test_future_flow_source_does_not_claim_complete_empty_discovery():
    ha = DiscoveryHA()
    ha.results["config_entries/flow/progress"][0]["context"]["source"] = "future_source"
    result = await discovery.snapshot(ha)
    assert result.candidates == [] and source(result, "ha_discovery").state == "partial"


async def test_valid_long_entity_id_and_device_digest():
    ha = DiscoveryHA()
    entity = "sensor." + "a" * 248
    ha.results["config/entity_registry/list"].append({"entity_id": entity, "device_id": "child"})
    result = await discovery.snapshot(ha)
    assert source(result, "ha_entities").state == "ok"
    assert source(result, "ha_devices").content_digest == digest([d.model_dump() for d in result.devices])


@pytest.mark.parametrize("error,state", [
    (HAUnreachable("private-error"), "unavailable"),
    (HAError("private-error", 403), "unavailable"),
    (HAError("HA config_entries/flow/progress: unknown_command"), "unsupported"),
    ({"unexpected": "private-error"}, "unsupported"),
])
async def test_source_failure_preserves_other_sources_without_error_body(error, state):
    ha = DiscoveryHA()
    ha.results["config_entries/flow/progress"] = error
    result = await discovery.snapshot(ha)
    assert len(result.devices) == 4 and result.candidates == []
    assert source(result, "ha_discovery").state == state
    assert source(result, "ha_discovery").item_count is None
    assert "private-error" not in result.model_dump_json()


@pytest.mark.parametrize("broken", ["ha_entities", "ha_states"])
async def test_missing_dependencies_do_not_invent_zero_counts(broken):
    ha = DiscoveryHA()
    target = next(s[2] for s in discovery.SOURCES if s[0] == broken)
    ha.results[target] = HAUnreachable("no reply")
    result = await discovery.snapshot(ha)
    child = next(d for d in result.devices if d.id == "child")
    assert child.available_entity_count is None
    assert child.entity_count == (None if broken == "ha_entities" else 3)


async def test_partial_and_truncated_sources_are_not_complete_totals():
    ha = DiscoveryHA()
    flows = ha.results["config_entries/flow/progress"]
    flows.extend({"flow_id": f"flow_{i}", "handler": "fixture", "context": {"source": "ssdp"}} for i in range(300))
    ha.results["config/entity_registry/list"].extend(["bad row", {"entity_id": "bad entity id"}])
    result = await discovery.snapshot(ha)
    assert len(result.candidates) == 256 and source(result, "ha_discovery").truncated
    assert source(result, "ha_entities").state == "partial"
    assert all(d.entity_count is None and d.available_entity_count is None for d in result.devices)


async def test_bad_names_ids_and_duplicate_source_rows_are_bounded_and_visible():
    ha = DiscoveryHA()
    rows = ha.results["config_entries/flow/progress"]
    rows[0]["context"]["title_placeholders"]["name"] = "<script>untrusted</script>" + "a" * 1000
    rows.extend([copy.deepcopy(rows[0]), {"flow_id": "../escape", "handler": "bad/url", "context": {"source": "dhcp"}}])
    result = await discovery.snapshot(ha)
    assert len(result.candidates) == 1 and len(result.candidates[0].title) == 160
    assert result.candidates[0].native_path == "/config/integrations"
    assert source(result, "ha_discovery").state == "partial"


@pytest.mark.parametrize("changes,expected", [
    ({"disabled_by": "user"}, "disabled"), ({"source": "ignore"}, "ignored"),
    ({"state": "setup_retry"}, "retry"), ({"state": "future"}, "unknown"),
    ({"state": None}, "unknown"),
])
async def test_integration_readiness_does_not_hide_disabled_ignored_or_unknown(changes, expected):
    ha = DiscoveryHA()
    ha.results["config_entries/get"][0].update(changes)
    result = await discovery.snapshot(ha)
    assert result.integrations[0].state == expected


async def test_source_timeout_and_caller_cancellation(monkeypatch):
    ha = DiscoveryHA()
    original = ha.ws
    entered = asyncio.Event()
    async def wait(target):
        if target == "config_entries/flow/progress":
            entered.set()
            await asyncio.Event().wait()
        return await original(target)
    ha.ws = wait
    monkeypatch.setattr(discovery, "SOURCE_TIMEOUT", .02)
    result = await discovery.snapshot(ha)
    assert source(result, "ha_discovery").state == "unavailable" and len(result.devices) == 4
    monkeypatch.setattr(discovery, "SOURCE_TIMEOUT", 10)
    entered.clear()
    task = asyncio.create_task(discovery.snapshot(ha))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_owner_api_is_read_only_and_does_not_expand_model_access(store):
    ha = DiscoveryHA()
    store.set_setting("owner", "owner")
    before = store.setting("policy")
    app = create_app(store=store, ha=ha)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.30.32.2", 1)),
            base_url="http://home.test", headers={"X-Remote-User-Id": "owner"}) as client:
        response = await client.get("/api/discovery")
        assert response.status_code == 200 and response.json()["contract_version"] == 1
        assert response.headers["cache-control"] == "no-store"
        assert (await client.post("/api/discovery", json={})).status_code in {403, 405}
    assert store.setting("policy") == before
    assert not store.list("case_evidence") and not store.list("proposal")
    assert "home.discovery" not in TOOLS


@pytest.mark.parametrize("fault", ["unbound", "revoked", "wrong_owner", "changed_during_read", "revoked_during_read"])
async def test_discovery_requires_current_owner_before_and_after_read(store, fault):
    ha = DiscoveryHA()
    if fault != "unbound":
        store.set_setting("owner", "owner")
    if fault == "revoked":
        ha.revoked = True
    original = ha.read
    async def changed(target):
        result = await original(target)
        if fault == "changed_during_read":
            store.set_setting("owner", "someone-else")
        if fault == "revoked_during_read":
            ha.revoked = True
        return result
    ha.read = changed
    app = create_app(store=store, ha=ha)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.30.32.2", 1)),
            base_url="http://home.test", headers={"X-Remote-User-Id": "other" if fault == "wrong_owner" else "owner"}) as client:
        response = await client.get("/api/discovery")
        assert response.status_code == 403
        assert "Desk light" not in response.text


async def test_host_login_is_not_sufficient_after_household_owner_revocation(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("fixture-local-key-long-enough")
    store.set_setting("owner", "owner")
    ha = DiscoveryHA()
    app = create_app(store=store, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
            headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "fixture-local-key-long-enough"})).status_code == 200
        ha.revoked = True
        response = await client.get("/api/discovery")
        assert response.status_code == 403 and not ha.calls


@pytest.mark.parametrize("when", ["initial_owner_check", "source_read", "final_owner_check"])
@pytest.mark.parametrize("change", ["web_epoch", "ha_token"])
async def test_host_authority_change_during_read_cannot_return_household_data(store, tmp_path, monkeypatch, when, change):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("fixture-local-key-long-enough")
    store.set_setting("owner", "owner")
    ha = DiscoveryHA()
    ha.token = "original-fixture-token"
    original_read, original_owner = ha.read, ha.owner
    checks = 0
    def revoke():
        if change == "web_epoch":
            store.set_setting("hermes_web_epoch", "logged-out")
        else:
            ha.token = "changed-fixture-token"
    async def read(target):
        result = await original_read(target)
        if when == "source_read":
            revoke()
        return result
    async def owner(identifier):
        nonlocal checks
        checks += 1
        if (when == "initial_owner_check" and checks == 1) or (when == "final_owner_check" and checks == 2):
            revoke()
        return await original_owner(identifier)
    ha.read, ha.owner = read, owner
    app = create_app(store=store, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
            headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "fixture-local-key-long-enough"})).status_code == 200
        response = await client.get("/api/discovery")
        assert response.status_code == (401 if change == "web_epoch" else 403)
        assert "Desk light" not in response.text
        if when == "initial_owner_check":
            assert not ha.calls


async def test_empty_is_success_only_for_sources_that_answered():
    ha = DiscoveryHA()
    for target in ha.results:
        if target != "/config":
            ha.results[target] = []
    result = await discovery.snapshot(ha)
    assert not result.devices and not result.candidates
    assert all(s.state == "ok" for s in result.sources if s.location == "home_assistant")
    assert "network_coverage_not_verified" in json.dumps(result.limitations)
