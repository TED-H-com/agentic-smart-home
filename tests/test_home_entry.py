"""Native-navigation compatibility, using synthetic HA observations only."""
import asyncio
import copy
from unittest.mock import AsyncMock

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.ha import HAError
from agentic_controller.home_entry import home_entry


@pytest.fixture
def configured(store, ha):
    store.put("bootstrap_v1", {"id": "setup", "state": "completed"})
    store.set_setting("owner", "owner")
    ha.request = AsyncMock(return_value={"version": "2026.10.1", "components": ["frontend", "api"],
                                         "state": "RUNNING"})
    return store, ha


async def test_completed_setup_uses_live_owner_and_native_capabilities(configured):
    store, ha = configured
    before = copy.deepcopy(store.get("bootstrap_v1", "setup"))
    assert (await home_entry(store, ha)).model_dump() == {
        "contract_version": 1, "state": "ready", "native_path": "/", "reason_code": None}
    ha.request.assert_awaited_once_with("GET", "/config")
    assert store.get("bootstrap_v1", "setup") == before
    assert not ha.calls and not ha.writes
    ha.revoked = True
    assert (await home_entry(store, ha)).reason_code == "home_identity_changed"
    assert ha.request.await_count == 1  # No cached owner grants a later navigation.


async def test_incomplete_setup_and_unbound_home_make_no_upstream_call(store, ha):
    ha.request = AsyncMock(side_effect=AssertionError("unexpected upstream call"))
    assert (await home_entry(store, ha)).state == "setup"
    store.put("bootstrap_v1", {"id": "setup", "state": "completed"})
    assert (await home_entry(store, ha)).reason_code == "home_not_bound"
    ha.request.assert_not_awaited()


@pytest.mark.parametrize("core", [
    {"version": "2027.99.1", "components": ["frontend", "api"], "new_field": {"ignored": True}},
    {"version": "2027.99.1", "components": ["frontend", "api"], "state": "RUNNING"},
])
async def test_new_versions_and_additive_fields_do_not_break_native_navigation(configured, core):
    store, ha = configured
    ha.request.return_value = core
    assert (await home_entry(store, ha)).state == "ready"


@pytest.mark.parametrize("core", [None, [], {}, {"version": 123, "components": ["frontend"]},
    {"version": "future", "components": "frontend"}, {"version": "future", "components": ["api"]},
    {"version": "future", "components": ["frontend", {}]},
    {"version": "future", "components": ["frontend"], "state": "STARTING"}])
async def test_missing_or_changed_semantics_do_not_redirect(configured, core):
    store, ha = configured
    ha.request.return_value = core
    result = await home_entry(store, ha)
    assert result.state in {"unsupported", "unavailable"}
    assert result.native_path == "/"
    assert store.get("bootstrap_v1", "setup")["state"] == "completed"


@pytest.mark.parametrize("error", [HAError("sensitive upstream detail", 401), OSError("private"),
                                  TypeError("upstream shape"), KeyError("required field")])
async def test_upstream_failures_keep_outer_entry_and_do_not_leak(configured, error):
    store, ha = configured
    ha.request.side_effect = error
    result = await home_entry(store, ha)
    assert result.state != "ready"
    assert str(error) not in result.model_dump_json()
    assert store.get("bootstrap_v1", "setup")["state"] == "completed"


async def test_slow_ha_is_bounded_and_cancelled(configured):
    store, ha = configured
    cancelled = []
    async def slow(*args):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)
    ha.request.side_effect = slow
    assert (await home_entry(store, ha, timeout=0.01)).reason_code == "home_unavailable"
    assert cancelled == [True]


@pytest.mark.parametrize("change", ["owner", "maintenance", "setup"])
async def test_recheck_authority_after_observation(configured, change):
    store, ha = configured
    core = ha.request.return_value
    async def changed(*args):
        if change == "owner":
            store.set_setting("owner", "another-owner")
        elif change == "maintenance":
            store.set_setting("updater_gate", {"id": "fixture"})
        else:
            store.put("bootstrap_v1", {"id": "setup", "state": "blocked"})
        return core
    ha.request.side_effect = changed
    assert (await home_entry(store, ha)).state == "unavailable"


async def test_endpoint_requires_outer_login_and_does_not_expose_arbitrary_url(configured, tmp_path, monkeypatch):
    store, ha = configured
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("synthetic-home-entry-login")
    app = create_app(store=store, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099") as client:
        assert (await client.get("/api/host/home-entry")).status_code == 401
        assert (await client.post("/api/host/login", json={"key": "synthetic-home-entry-login"},
                                  headers={"origin": "http://localhost:8099"})).status_code == 200
        response = await client.get("/api/host/home-entry?url=https://untrusted.example/")
        assert response.status_code == 200 and response.json()["state"] == "ready"
        assert response.json()["native_path"] == "/" and "untrusted" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/api/host/home-entry", headers={"host": "untrusted.example"})).status_code == 403
