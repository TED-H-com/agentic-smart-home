"""Offline Supervisor fixtures for App updates. Not a real HAOS update or #stable slug."""
import copy
import json
from pathlib import Path

import httpx
import pytest
import yaml
from agentic_controller import VERSION
from agentic_controller.app import create_app
from agentic_controller.bridge_maintenance import (
    OPERATION_TIMEOUT,
    STARTUP_GRACE,
    BridgeFault,
    BridgeMaintenance,
    BridgeRelease,
    version_order,
)
from agentic_controller.home_worker_maintenance import (
    HomeWorkerFault,
    HomeWorkerMaintenance,
    HomeWorkerRelease,
)
from test_bridge_maintenance import JOB, Bridge, Clock, Supervisor, advance_to, installed
from test_bridge_maintenance import SLUG as BRIDGE_SLUG
from test_home_worker_maintenance import CODE, FakeRunner, login_ready
from test_home_worker_maintenance import SLUG as HOME_SLUG
from test_home_worker_maintenance import Supervisor as HomeSupervisor

LEGACY = "https://github.com/teddashh/agentic-smart-home#release/0.8.0"


@pytest.fixture
def manager(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed"})
    clock = Clock()
    return BridgeMaintenance(store, Supervisor(), Bridge(clock), clock=clock)


@pytest.fixture
def home(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed", "run_id": "c" * 32})
    clock = Clock()
    return HomeWorkerMaintenance(store, HomeSupervisor(), FakeRunner(store), "d" * 32, clock=clock)


def test_versions_compare_as_numeric_tuples():
    assert version_order("0.9.0", "0.10.0") == "older"
    assert version_order("1.0.0", "0.9.9") == "newer"
    assert version_order("0.8", "0.8.0") == "invalid"
    assert version_order("0.8.0", "0.8.0") == "same"


def test_app_config_versions_match_controller_version():
    bridge = yaml.safe_load(Path("bridge/config.yaml").read_text())
    runner = yaml.safe_load(Path("runner/config.yaml").read_text())
    assert bridge["version"] == VERSION == runner["version"]
    assert BridgeRelease().version == VERSION == HomeWorkerRelease().version
    assert BridgeRelease().repository.endswith("#stable")
    assert HomeWorkerRelease().repository == BridgeRelease().repository


async def healthy(manager):
    await installed(manager)
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy"
    return manager


def bump(manager, version="0.9.0"):
    return BridgeMaintenance(manager.store, manager.ha, manager.bridge,
                             release=BridgeRelease(version=version), clock=manager.clock)


def bump_home(manager, version="0.9.0"):
    return HomeWorkerMaintenance(manager.store, manager.ha, manager.runner, manager.controller_id,
                                 release=HomeWorkerRelease(version=version), clock=manager.clock)


async def test_version_bump_offers_an_update_and_keeps_the_app_running(manager):
    await healthy(manager)
    bumped = bump(manager)
    calls = len(manager.ha.calls)
    status = await bumped.tick()
    assert status["state"] == "update_available" and status["code"] == "bridge_update_available"
    assert status["update"] == {"available": True, "from": "0.8.0", "to": "0.9.0", "blocked_code": None}
    for key in ("stage", "slug", "release", "limitations", "managed", "pairing_id", "auto_repair"):
        assert key in status
    assert all(call[0] == "GET" for call in manager.ha.calls[calls:])
    assert not manager.ha.writes("/restart") and not manager.ha.writes("/update")
    manager.clock.now += STARTUP_GRACE + 1
    manager.bridge.listener = False
    assert (await bumped.tick())["state"] == "update_available"
    assert not manager.ha.writes("/restart")


async def test_owner_update_persists_intent_then_becomes_healthy(manager):
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    manager.ha.catalog["version_latest"] = "0.9.0"
    seen = {}

    async def before(method, path):
        if path.endswith("/update"):
            seen["operation"] = copy.deepcopy(bumped.record()["operation"])

    manager.ha.before = before
    status = await bumped.update()
    assert seen["operation"]["kind"] == "update" and seen["operation"]["job_id"] is None
    assert seen["operation"]["target_release"] == asdict_release("0.9.0")
    assert status["operation"]["job_id"] == JOB and "target_release" not in status["operation"]
    assert manager.ha.writes("/update")[0][2] == {"backup": False, "background": True}
    manager.bridge.seen = manager.clock()
    done = await bumped.tick()
    assert done["state"] == "healthy" and done["stage"] == "monitor"
    assert bumped.record()["release"]["version"] == "0.9.0" and bumped.record()["operation"] is None
    assert len(manager.ha.writes("/update")) == 1
    with pytest.raises(BridgeFault, match="bridge_update_not_allowed"):
        await bumped.update()
    assert len(manager.ha.writes("/update")) == 1


def asdict_release(version, app="agentic_bridge"):
    return {"repository": BridgeRelease().repository, "app": app, "version": version}


@pytest.mark.parametrize("change,code", [
    ({"version": "0.8.0"}, "bridge_update_version_mismatch"),
    ({"hassio_api": True}, "bridge_update_security_mismatch"),
    ({"options": {"host": "custom"}}, "bridge_update_options_mismatch"),
    ({"ingress_port": 1}, "bridge_update_port_mismatch"),
    ({"apparmor": "disable"}, "bridge_update_apparmor_mismatch"),
])
async def test_bridge_readback_mismatch_does_not_roll_back(manager, change, code):
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    manager.ha.catalog["version_latest"] = "0.9.0"
    await bumped.update()
    sent = len(manager.ha.calls)
    manager.ha.app.update(change)
    status = await bumped.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == code
    assert bumped.record()["release"]["version"] == "0.8.0"
    assert len(manager.ha.writes("/update")) == 1
    assert not any(call[0] == "POST" for call in manager.ha.calls[sent:])
    await bumped.tick()
    assert len(manager.ha.writes("/update")) == 1
    assert not any(call[0] == "POST" for call in manager.ha.calls[sent:])


async def test_bridge_startup_grace_then_state_mismatch(manager):
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    manager.ha.catalog["version_latest"] = "0.9.0"
    manager.ha.update_state = "startup"
    await bumped.update()
    assert (await bumped.tick())["code"] == "bridge_update_starting"
    assert bumped.record()["release"]["version"] == "0.8.0"
    manager.clock.now += STARTUP_GRACE + 1
    assert (await bumped.tick())["code"] == "bridge_update_state_mismatch"
    assert len(manager.ha.writes("/update")) == 1 and bumped.record()["release"]["version"] == "0.8.0"


async def test_store_still_older_after_one_reload_waits(manager):
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    with pytest.raises(BridgeFault, match="bridge_release_not_published"):
        await bumped.update()
    assert bumped.record()["state"] == "waiting" and bumped.record()["code"] == "bridge_release_not_published"
    assert len(manager.ha.writes("/reload")) == 1 and not manager.ha.writes("/update")
    await bumped.tick()
    assert len(manager.ha.writes("/reload")) == 1
    assert bumped.record()["code"] == "bridge_release_not_published"


async def test_newer_catalog_is_not_installed(manager):
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    manager.ha.catalog["version_latest"] = "0.10.0"
    with pytest.raises(BridgeFault, match="bridge_release_ahead"):
        await bumped.update()
    assert bumped.record()["code"] == "bridge_release_ahead"
    assert not manager.ha.writes("/reload") and not manager.ha.writes("/update")


async def test_lost_update_response_is_observed_and_not_repeated(manager):
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    manager.ha.catalog["version_latest"] = "0.9.0"
    manager.ha.lose = ("POST", f"/store/addons/{BRIDGE_SLUG}/update")
    status = await bumped.update()
    assert status["state"] == "unavailable" and status["code"] == "bridge_observation_unavailable"
    assert len(manager.ha.writes("/update")) == 1
    await bumped.tick()
    assert len(manager.ha.writes("/update")) == 1
    manager.clock.now += OPERATION_TIMEOUT + 1
    assert (await bumped.tick())["code"] == "bridge_operation_unconfirmed"
    assert len(manager.ha.writes("/update")) == 1


@pytest.mark.parametrize("state", ["user_disabled", "user_removed", "user_modified"])
async def test_user_terminal_records_ignore_a_bridge_release_change(manager, state):
    await healthy(manager)
    record = manager.record()
    record["release"] = {**record["release"], "version": "0.7.0"}
    record["state"] = state
    record["enabled"] = False
    manager.save(record)
    before, calls = copy.deepcopy(manager.record()), list(manager.ha.calls)
    bumped = bump(manager)
    await bumped.tick()
    await bumped.authorize()
    assert manager.record() == before and manager.ha.calls == calls


async def test_legacy_bridge_repository_is_not_uninstalled(manager):
    await healthy(manager)
    record = manager.record()
    record["release"] = {**record["release"], "repository": LEGACY}
    manager.save(record)
    calls = len(manager.ha.calls)
    status = await manager.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == "bridge_release_channel_changed"
    assert status["update"]["available"] is False
    assert status["update"]["blocked_code"] == "bridge_release_channel_changed"
    assert not any(call[0] == "POST" for call in manager.ha.calls[calls:])
    assert not manager.ha.writes("/uninstall")


async def test_reenable_at_an_old_release_does_not_rotate_pairing(manager):
    await healthy(manager)
    record = manager.record()
    record["release"] = {**record["release"], "version": "0.7.0"}
    manager.save(record)
    manager.suspend()
    prepared, calls = manager.bridge.preparations, len(manager.ha.calls)
    assert (await manager.authorize())["state"] == "update_available"
    assert manager.bridge.preparations == prepared and len(manager.ha.calls) == calls
    assert not manager.ha.writes("/restart")


async def test_fresh_install_reloads_once_when_the_store_is_behind(manager):
    manager.ha.catalog["version_latest"] = "0.7.1"
    await manager.authorize()
    await advance_to(manager, "install")
    status = await manager.tick()
    assert status["state"] == "waiting" and status["code"] == "bridge_release_not_published"
    assert status["update"]["blocked_code"] == "bridge_release_not_published"
    assert len(manager.ha.writes("/reload")) == 1 and not manager.ha.writes("/install")
    await manager.tick()
    assert len(manager.ha.writes("/reload")) == 1


async def test_fresh_install_refuses_a_numerically_newer_catalog(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed"})
    clock = Clock()
    ha, bridge = Supervisor(), Bridge(clock)
    ha.catalog["version_latest"] = "0.10.0"
    manager = BridgeMaintenance(store, ha, bridge, release=BridgeRelease(version="0.9.0"), clock=clock)
    await manager.authorize()
    await advance_to(manager, "install")
    status = await manager.tick()
    assert status["code"] == "bridge_release_ahead" and status["state"] == "waiting"
    assert not ha.writes("/reload") and not ha.writes("/install")


async def test_fresh_install_continues_after_reload_publishes_the_pin(manager):
    manager.ha.catalog["version_latest"] = "0.7.1"
    manager.ha.reload_version = VERSION
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    assert len(manager.ha.writes("/reload")) == 1 and len(manager.ha.writes("/install")) == 1


async def test_home_version_bump_is_read_only_until_the_owner_updates(home):
    await login_ready(home)
    bumped = bump_home(home)
    calls, runner_calls = len(home.ha.calls), list(home.runner.calls)
    status = await bumped.tick()
    assert status["state"] == "update_available" and status["code"] == "home_worker_update_available"
    assert status["update"] == {"available": True, "from": VERSION, "to": "0.9.0", "blocked_code": None}
    assert status["login"]["show"] is False
    assert "no_app_update_across_releases" not in status["limitations"]
    for key in ("stage", "slug", "release", "credential_state", "enabled", "managed"):
        assert key in status
    assert home.runner.calls == runner_calls
    assert all(call[0] == "GET" for call in home.ha.calls[calls:])
    assert not home.ha.posts("/restart") and not home.ha.posts("/update")
    home.ha.catalog["version_latest"] = "0.9.0"
    seen = {}

    async def before(method, path):
        if path.endswith("/update"):
            seen["operation"] = copy.deepcopy(bumped.record()["operation"])

    home.ha.before = before
    await bumped.update()
    operation = seen["operation"]
    assert operation["kind"] == "update" and operation["target_release"]["version"] == "0.9.0"
    assert operation["job_id"] is None and CODE not in json.dumps(operation)
    home.runner.capabilities["available"] = True
    home.runner.capabilities["providers"][0]["credential_state"] = "configured"
    done = await bumped.tick()
    assert done["state"] == "ready" and done["stage"] == "monitor"
    assert bumped.record()["release"]["version"] == "0.9.0" and bumped.record()["operation"] is None
    assert len(home.ha.posts("/update")) == 1
    assert home.ha.posts("/update")[0][2] == {"backup": False, "background": True}


@pytest.mark.parametrize("change,code", [
    ({"version": VERSION}, "home_worker_update_version_mismatch"),
    ({"hassio_api": True}, "home_worker_update_security_mismatch"),
    ({"options": {"pairing_code": "not-a-published-pairing"}}, "home_worker_update_options_mismatch"),
    ({"network": {"8443/tcp": 1}}, "home_worker_update_port_mismatch"),
    ({"apparmor": "default"}, "home_worker_update_apparmor_mismatch"),
])
async def test_home_readback_mismatch_does_not_roll_back(home, change, code):
    await login_ready(home)
    bumped = bump_home(home)
    await bumped.tick()
    home.ha.catalog["version_latest"] = "0.9.0"
    await bumped.update()
    sent, runner_calls = len(home.ha.calls), list(home.runner.calls)
    home.ha.app.update(change)
    status = await bumped.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == code
    assert bumped.record()["release"]["version"] == VERSION
    assert len(home.ha.posts("/update")) == 1
    assert not any(call[0] == "POST" for call in home.ha.calls[sent:])
    assert home.runner.calls == runner_calls
    await bumped.tick()
    assert len(home.ha.posts("/update")) == 1
    assert not any(call[0] == "POST" for call in home.ha.calls[sent:])
    assert home.runner.calls == runner_calls


async def test_home_store_versions_and_lost_update(home):
    await login_ready(home)
    behind = bump_home(home)
    await behind.tick()
    with pytest.raises(HomeWorkerFault, match="home_worker_release_not_published"):
        await behind.update()
    assert behind.record()["state"] == "waiting"
    assert len(home.ha.posts("/reload")) == 1 and not home.ha.posts("/update")
    await behind.tick()
    assert len(home.ha.posts("/reload")) == 1

    home.ha.catalog["version_latest"] = "0.10.0"
    record = behind.record()
    record["state"] = "update_available"
    record["code"] = "home_worker_update_available"
    behind.save(record)
    posts = len(home.ha.posts("/update"))
    with pytest.raises(HomeWorkerFault, match="home_worker_release_ahead"):
        await behind.update()
    assert len(home.ha.posts("/reload")) == 1 and len(home.ha.posts("/update")) == posts

    home.ha.catalog["version_latest"] = "0.9.0"
    record = behind.record()
    record["state"] = "update_available"
    record["code"] = "home_worker_update_available"
    behind.save(record)
    home.ha.lose = ("POST", f"/store/addons/{HOME_SLUG}/update")
    status = await behind.update()
    assert status["code"] == "home_worker_observation_unavailable"
    assert len(home.ha.posts("/update")) == posts + 1
    await behind.tick()
    home.clock.now += OPERATION_TIMEOUT + 1
    assert (await behind.tick())["code"] == "home_worker_operation_unconfirmed"
    assert len(home.ha.posts("/update")) == posts + 1


@pytest.mark.parametrize("flag", ["worker_lease", "worker_auth_pending"])
async def test_home_worker_busy_posts_nothing(home, flag):
    await login_ready(home)
    bumped = bump_home(home)
    await bumped.tick()
    home.store.set_setting(flag, {"id": "fixture"})
    calls = list(home.ha.calls)
    with pytest.raises(HomeWorkerFault, match="home_worker_busy"):
        await bumped.update()
    assert home.ha.calls == calls and bumped.record()["operation"] is None
    assert bumped.record()["state"] == "update_available"


@pytest.mark.parametrize("state", ["user_disabled", "user_removed", "user_modified"])
async def test_user_terminal_records_ignore_a_home_release_change(home, state):
    await home.authorize()
    record = home.record()
    record["release"] = {**record["release"], "version": "0.7.0"}
    record["state"] = state
    record["enabled"] = False
    home.save(record)
    before, calls = copy.deepcopy(home.record()), list(home.ha.calls)
    await bump_home(home).tick()
    await home.authorize()
    assert home.record() == before and home.ha.calls == calls


async def test_home_repair_of_an_old_release_does_not_reinstall(home):
    await login_ready(home)
    record = home.record()
    stage = record["stage"]
    record["release"] = {**record["release"], "version": "0.7.0"}
    record["enabled"] = False
    record["state"] = "user_disabled"
    home.save(record)
    calls = list(home.ha.calls)
    status = await home.repair()
    assert status["state"] == "update_available" and status["code"] == "home_worker_update_available"
    assert home.record()["stage"] == stage and home.record()["enabled"] is True
    assert home.ha.calls == calls


async def test_legacy_home_repository_is_not_replaced(home):
    await login_ready(home)
    record = home.record()
    record["release"] = {**record["release"], "repository": LEGACY}
    home.save(record)
    calls = len(home.ha.calls)
    status = await home.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == "home_worker_release_channel_changed"
    assert status["update"]["blocked_code"] == "home_worker_release_channel_changed"
    assert "no_app_update_across_releases" in status["limitations"]
    assert not any(call[0] == "POST" for call in home.ha.calls[calls:])
    assert not home.ha.posts("/uninstall")


async def test_home_fresh_install_lockstep(home):
    home.ha.catalog["version_latest"] = "0.7.1"
    await home.authorize()
    for _ in range(6):
        if home.record() and home.record().get("code") == "home_worker_release_not_published":
            break
        await home.tick()
    assert home.record()["state"] == "waiting"
    assert len(home.ha.posts("/reload")) == 1 and not home.ha.posts("/install")
    await home.tick()
    assert len(home.ha.posts("/reload")) == 1

    home.store.set_setting("home_worker_maintenance", None)
    home.ha.calls.clear()
    home.ha.repos.clear()
    home.ha.catalog["version_latest"] = "0.7.1"
    home.ha.reload_version = VERSION
    await home.authorize()
    for _ in range(8):
        if home.ha.posts("/install"):
            break
        await home.tick()
    assert len(home.ha.posts("/reload")) == 1 and len(home.ha.posts("/install")) == 1


async def test_home_fresh_install_refuses_a_numerically_newer_catalog(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed", "run_id": "c" * 32})
    clock, ha = Clock(), HomeSupervisor()
    ha.catalog["version_latest"] = "0.10.0"
    manager = HomeWorkerMaintenance(store, ha, FakeRunner(store), "d" * 32,
                                    release=HomeWorkerRelease(version="0.9.0"), clock=clock)
    await manager.authorize()
    for _ in range(6):
        if manager.record() and manager.record().get("code") == "home_worker_release_ahead":
            break
        await manager.tick()
    assert manager.record()["code"] == "home_worker_release_ahead"
    assert not ha.posts("/reload") and not ha.posts("/install")


async def test_home_worker_busy_route_returns_409(store, ha, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    monkeypatch.setenv("AGENTIC_LINE_CONFIG", str(tmp_path / "line-config/gateway.json"))
    (tmp_path / "access-key").write_text("fixture-machine-access-key")
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed", "run_id": "c" * 32})
    release = HomeWorkerRelease()
    record = {"id": "a" * 32, "owner": "owner", "enabled": True, "managed": True,
              "state": "update_available", "stage": "monitor", "code": "home_worker_update_available",
              "release": {"repository": release.repository, "app": release.app, "version": "0.7.0"},
              "slug": HOME_SLUG, "repository_id": "aabbccdd", "operation": None}
    store.set_setting("home_worker_maintenance", record)
    store.set_setting("worker_lease", {"id": "fixture"})
    ha.token = "fixture-ha-token"
    app = create_app(store=store, ha=ha, host_mode=True)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/host/bridge/maintenance/update" in paths
    assert "/api/host/home-worker/maintenance/update" in paths
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                 headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "fixture-machine-access-key"})).status_code == 200
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        response = await client.post("/api/host/home-worker/maintenance/update", json={})
    assert response.status_code == 409 and response.json()["detail"] == "home_worker_busy"
    saved = store.setting("home_worker_maintenance")
    assert saved["operation"] is None and saved["state"] == "update_available"
    if getattr(app.state, "gateway", None):
        await app.state.gateway.close()
