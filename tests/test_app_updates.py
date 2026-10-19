"""Offline Supervisor fixtures for App updates. Not a real HAOS update or #stable slug."""
import asyncio
import copy
import json
import traceback
from pathlib import Path

import httpx
import pytest
import yaml
from agentic_controller import VERSION as CURRENT_VERSION
from agentic_controller.app import create_app
from agentic_controller.bridge_maintenance import (
    JOB_TIMEOUT,
    OPERATION_TIMEOUT,
    STARTUP_GRACE,
    STORE_RELOAD_ATTEMPTS,
    STORE_RELOAD_BACKOFF,
    BridgeFault,
    BridgeMaintenance,
    BridgeRelease,
    version_order,
)
from agentic_controller.ha import HAError
from agentic_controller.home_worker_maintenance import (
    OPERATION_TIMEOUT as HOME_OPERATION_TIMEOUT,
)
from agentic_controller.home_worker_maintenance import (
    STARTUP_GRACE as HOME_STARTUP_GRACE,
)
from agentic_controller.home_worker_maintenance import (
    HomeWorkerFault,
    HomeWorkerMaintenance,
    HomeWorkerRelease,
)
from test_bridge_maintenance import JOB, Bridge, Clock, Supervisor, advance_to, installed
from test_bridge_maintenance import SLUG as BRIDGE_SLUG
from test_home_worker_maintenance import CODE, FINGERPRINT, NEW_CODE, FakeRunner, login_ready
from test_home_worker_maintenance import REPO as HOME_REPO
from test_home_worker_maintenance import SLUG as HOME_SLUG
from test_home_worker_maintenance import Supervisor as HomeSupervisor

# Historical upgrade scenarios deliberately stay at 0.9.0 → 0.9.1 across releases.
VERSION = "0.9.0"
LEGACY = "https://github.com/teddashh/agentic-smart-home#release/0.8.0"


@pytest.fixture
def manager(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed"})
    clock = Clock()
    return BridgeMaintenance(store, Supervisor(version="0.9.0"), Bridge(clock),
                             release=BridgeRelease(version="0.9.0"), clock=clock)


@pytest.fixture
def home(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed", "run_id": "c" * 32})
    clock = Clock()
    return HomeWorkerMaintenance(store, HomeSupervisor(version="0.9.0"), FakeRunner(store), "d" * 32,
                                 release=HomeWorkerRelease(version="0.9.0"), clock=clock)


def test_versions_compare_as_numeric_tuples():
    assert version_order("0.9.0", "0.10.0") == "older"
    assert version_order("1.0.0", "0.9.9") == "newer"
    assert version_order("0.8", "0.8.0") == "invalid"
    assert version_order("0.8.0", "0.8.0") == "same"


def test_app_config_versions_match_controller_version():
    bridge = yaml.safe_load(Path("bridge/config.yaml").read_text())
    runner = yaml.safe_load(Path("runner/config.yaml").read_text())
    assert bridge["version"] == CURRENT_VERSION == runner["version"]
    assert BridgeRelease().version == CURRENT_VERSION == HomeWorkerRelease().version
    assert BridgeRelease().repository.endswith("#stable")
    assert HomeWorkerRelease().repository == BridgeRelease().repository


async def healthy(manager):
    await installed(manager)
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy"
    return manager


def bump(manager, version="0.9.1"):
    return BridgeMaintenance(manager.store, manager.ha, manager.bridge,
                             release=BridgeRelease(version=version), clock=manager.clock)


def bump_home(manager, version="0.9.1"):
    return HomeWorkerMaintenance(manager.store, manager.ha, manager.runner, manager.controller_id,
                                 release=HomeWorkerRelease(version=version), clock=manager.clock)


async def test_version_bump_offers_an_update_and_keeps_the_app_running(manager):
    await healthy(manager)
    bumped = bump(manager)
    calls = len(manager.ha.calls)
    status = await bumped.tick()
    assert status["state"] == "update_available" and status["code"] == "bridge_update_available"
    assert status["update"] == {"available": True, "from": "0.9.0", "to": "0.9.1", "blocked_code": None}
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
    manager.ha.catalog["version_latest"] = "0.9.1"
    seen = {}

    async def before(method, path):
        if path.endswith("/update"):
            seen["operation"] = copy.deepcopy(bumped.record()["operation"])

    manager.ha.before = before
    status = await bumped.update()
    assert seen["operation"]["kind"] == "update" and seen["operation"]["job_id"] is None
    assert seen["operation"]["target_release"] == asdict_release("0.9.1")
    assert status["operation"]["job_id"] == JOB and "target_release" not in status["operation"]
    assert manager.ha.writes("/update")[0][2] == {"backup": False, "background": True}
    manager.bridge.seen = manager.clock()
    done = await bumped.tick()
    assert done["state"] == "healthy" and done["stage"] == "monitor"
    assert bumped.record()["release"]["version"] == "0.9.1" and bumped.record()["operation"] is None
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
    manager.ha.catalog["version_latest"] = "0.9.1"
    await bumped.update()
    sent = len(manager.ha.calls)
    manager.ha.app.update(change)
    status = await bumped.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == code
    assert bumped.record()["release"]["version"] == "0.9.0"
    assert len(manager.ha.writes("/update")) == 1
    assert not any(call[0] == "POST" for call in manager.ha.calls[sent:])
    await bumped.tick()
    assert len(manager.ha.writes("/update")) == 1
    assert not any(call[0] == "POST" for call in manager.ha.calls[sent:])


async def test_bridge_startup_grace_then_state_mismatch(manager):
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    manager.ha.catalog["version_latest"] = "0.9.1"
    manager.ha.update_state = "startup"
    await bumped.update()
    assert (await bumped.tick())["code"] == "bridge_update_starting"
    assert bumped.record()["release"]["version"] == "0.9.0"
    manager.clock.now += STARTUP_GRACE + 1
    assert (await bumped.tick())["code"] == "bridge_update_state_mismatch"
    assert len(manager.ha.writes("/update")) == 1 and bumped.record()["release"]["version"] == "0.9.0"


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


@pytest.fixture(params=["bridge", "home"])
async def reload_update(request):
    manager = request.getfixturevalue("manager" if request.param == "bridge" else "home")
    await (healthy(manager) if request.param == "bridge" else login_ready(manager))
    restart = bump if request.param == "bridge" else bump_home
    manager = restart(manager)
    await manager.tick()
    return manager, restart


def reload_posts(manager):
    return [call for call in manager.ha.calls if call[:2] == ("POST", "/store/reload")]


@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_failed_store_reload_retries_after_persisted_backoff_and_restart(reload_update, failure):
    manager, restart = reload_update

    async def before(method, path):
        if (method, path) == ("POST", "/store/reload"):
            attempt = manager.record()["release_store_reload"]
            assert attempt["attempts"] == 1 and attempt["retry_at"] == manager.clock() + STORE_RELOAD_BACKOFF
            assert manager.record()["release_store_reloaded"] is False
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise HAError("fixture transient reload failure")

    manager.ha.before = before
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await manager.update()
    else:
        await manager.update()
    manager = restart(manager)
    manager.ha.before = None
    manager.ha.reload_version = "0.9.1"
    await manager.tick()
    assert len(reload_posts(manager)) == 1
    manager.clock.now += STORE_RELOAD_BACKOFF
    status = await manager.tick()
    assert len(reload_posts(manager)) == 2
    assert status["state"] == "update_available"
    assert manager.record()["release_store_reload"] is None
    # A catalog retry never replays the Owner's App update.
    assert not [call for call in manager.ha.calls if call[0] == "POST" and call[1].endswith("/update")]


@pytest.mark.parametrize("failure", ["fail", "lose"])
async def test_store_reload_failure_and_lost_reply_have_finite_restart_safe_budget(reload_update, failure):
    manager, restart = reload_update
    setattr(manager.ha, failure, ("POST", "/store/reload"))
    await manager.update()
    for attempt in range(1, STORE_RELOAD_ATTEMPTS):
        manager = restart(manager)
        await manager.tick()
        assert len(reload_posts(manager)) == attempt
        manager.clock.now += STORE_RELOAD_BACKOFF * 2 ** (attempt - 1)
        await manager.tick()
        assert len(reload_posts(manager)) == attempt + 1
    for _ in range(4):
        manager.clock.now += 86400
        manager = restart(manager)
        status = await manager.tick()
    assert len(reload_posts(manager)) == STORE_RELOAD_ATTEMPTS
    assert status["state"] == "waiting" and status["code"].endswith("_release_not_published")
    assert manager.record()["release_store_reloaded"] is False


async def test_lost_store_reload_reply_observes_published_pin_without_another_post(reload_update):
    manager, restart = reload_update
    manager.ha.reload_version = "0.9.1"
    manager.ha.lose = ("POST", "/store/reload")
    await manager.update()
    manager = restart(manager)
    status = await manager.tick()
    assert status["state"] == "update_available" and len(reload_posts(manager)) == 1
    assert manager.record()["release_store_reload"] is None


async def test_successful_reload_with_lost_readback_does_not_repeat(reload_update):
    manager, restart = reload_update

    async def before(method, path):
        if method == "GET" and path.startswith("/store/addons/") and reload_posts(manager):
            raise HAError("fixture readback lost")

    manager.ha.before = before
    await manager.update()
    assert manager.record()["release_store_reloaded"] is True
    manager.ha.before = None
    manager.clock.now += 86400
    manager = restart(manager)
    await manager.tick()
    assert len(reload_posts(manager)) == 1


async def test_reload_retry_rechecks_current_owner(reload_update):
    manager, _ = reload_update
    manager.ha.fail = ("POST", "/store/reload")
    await manager.update()
    manager.ha.fail = None
    manager.clock.now += STORE_RELOAD_BACKOFF
    manager.store.set_setting("owner", "replaced-owner")
    await manager.tick()
    assert len(reload_posts(manager)) == 1


async def test_legacy_reload_flag_is_preserved_for_same_pin_but_new_release_gets_bounded_budget(reload_update):
    manager, restart = reload_update
    record = manager.record()
    manager.save(record, release_store_reloaded=True)
    # Legacy record was pinned to 0.9.0; 0.9.1 gets one successful refresh.
    with pytest.raises((BridgeFault, HomeWorkerFault), match="release_not_published"):
        await manager.update()
    assert len(reload_posts(manager)) == 1
    manager = restart(manager)
    manager.clock.now += 86400
    await manager.tick()
    assert len(reload_posts(manager)) == 1


async def test_same_release_legacy_reload_flag_is_not_replayed(reload_update):
    manager, restart = reload_update
    manager = restart(manager, version="0.9.0")
    record = manager.record()
    manager.save(record, release_store_reloaded=True)
    with pytest.raises((BridgeFault, HomeWorkerFault), match="release_not_published"):
        await manager.lockstep(record, manager.ha.token, {"version_latest": "0.8.0"})
    assert not reload_posts(manager)


@pytest.mark.parametrize("corrupt", [True, {"attempts": True}, {"attempts": -1}])
async def test_corrupt_store_reload_budget_cannot_be_reset_to_unlimited_posts(reload_update, corrupt):
    manager, _ = reload_update
    if isinstance(corrupt, dict):
        corrupt = {**corrupt, "release": asdict_release("0.9.1", manager.release.app), "retry_at": 0}
    manager.save(manager.record(), release_store_reload=corrupt)
    with pytest.raises((BridgeFault, HomeWorkerFault), match="release_not_published"):
        await manager.update()
    assert not reload_posts(manager)


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
    manager.ha.catalog["version_latest"] = "0.9.1"
    manager.ha.lose = ("POST", f"/store/addons/{BRIDGE_SLUG}/update")
    status = await bumped.update()
    assert status["state"] == "unavailable" and status["code"] == "bridge_observation_unavailable"
    assert len(manager.ha.writes("/update")) == 1
    await bumped.tick()
    assert len(manager.ha.writes("/update")) == 1
    manager.clock.now += OPERATION_TIMEOUT + 1
    status = await bumped.tick()
    assert status["state"] == "update_available" and status["code"] == "bridge_update_failed"
    assert bumped.record()["operation"] is None and bumped.record()["release"]["version"] == "0.9.0"
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


def accept_repair_pairing(bridge):
    original = bridge.prepare_pairing

    async def rotate(*, repair=False):
        if repair:
            bridge.options = {**bridge.options, "pairing_id": "c" * 32, "token": "v" * 64}
            bridge.state = "prepared"
        return await original()

    bridge.prepare_pairing = rotate


async def finish_reconnect(manager):
    for _ in range(10):
        record = manager.record()
        if record["stage"] == "monitor" and not record.get("reconnecting") and record["state"] == "healthy":
            return
        manager.bridge.seen = manager.clock()
        await manager.tick()
    raise AssertionError(manager.status())


async def test_b4_reenable_reconnects_at_the_installed_release(manager):
    await healthy(manager)
    record = manager.record()
    record["release"] = {**record["release"], "version": "0.7.0"}
    manager.save(record)
    manager.ha.app["version"] = "0.7.0"
    manager.suspend()
    manager.bridge.state = "revoked"
    accept_repair_pairing(manager.bridge)
    old_pairing = manager.record()["pairing_id"]
    status = await manager.authorize()
    assert status["state"] == "pending" and status["stage"] == "configure"
    assert manager.record()["release"]["version"] == "0.7.0"
    assert manager.record()["pairing_id"] != old_pairing and manager.bridge.state == "prepared"
    await finish_reconnect(manager)
    assert manager.record()["release"]["version"] == "0.7.0"
    assert (await manager.tick())["code"] == "bridge_update_available"
    assert not manager.ha.writes("/update")

    record = manager.record()
    record.update(release={**record["release"], "version": "0.7.0"}, state="healthy", stage="monitor",
                  reconnecting=False, operation=None)
    manager.save(record)
    manager.ha.app["version"] = VERSION
    manager.suspend()
    manager.bridge.state = "revoked"
    assert (await manager.authorize())["stage"] == "configure"
    assert manager.record()["release"]["version"] == VERSION
    await finish_reconnect(manager)
    assert (await manager.tick())["state"] == "healthy"
    assert manager.record()["release"]["version"] == VERSION and not manager.ha.writes("/update")
    app = manager.ha.app
    bare = {"slug": app["slug"], "repository_id": app["repository"]}
    manager.validate_app(app, bare, installed=True)
    with pytest.raises(BridgeFault, match="bridge_app_contract_mismatch"):
        manager.validate_app(app, {**bare, "release": {"version": "0.7.0"}}, installed=True)


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
    ha, bridge = Supervisor(version="0.9.0"), Bridge(clock)
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
    assert status["update"] == {"available": True, "from": VERSION, "to": "0.9.1", "blocked_code": None}
    assert status["login"]["show"] is False
    assert "no_app_update_across_releases" not in status["limitations"]
    for key in ("stage", "slug", "release", "credential_state", "enabled", "managed"):
        assert key in status
    assert home.runner.calls == runner_calls
    assert all(call[0] == "GET" for call in home.ha.calls[calls:])
    assert not home.ha.posts("/restart") and not home.ha.posts("/update")
    home.ha.catalog["version_latest"] = "0.9.1"
    seen = {}

    async def before(method, path):
        if path.endswith("/update"):
            seen["operation"] = copy.deepcopy(bumped.record()["operation"])

    home.ha.before = before
    await bumped.update()
    operation = seen["operation"]
    assert operation["kind"] == "update" and operation["target_release"]["version"] == "0.9.1"
    assert operation["job_id"] is None and CODE not in json.dumps(operation)
    home.runner.capabilities["available"] = True
    home.runner.capabilities["providers"][0]["credential_state"] = "configured"
    done = await bumped.tick()
    assert done["state"] == "ready" and done["stage"] == "monitor"
    assert bumped.record()["release"]["version"] == "0.9.1" and bumped.record()["operation"] is None
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
    home.ha.catalog["version_latest"] = "0.9.1"
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

    home.ha.catalog["version_latest"] = "0.9.1"
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
    status = await behind.tick()
    assert status["state"] == "update_available" and status["code"] == "home_worker_update_failed"
    assert behind.record()["operation"] is None and behind.record()["release"]["version"] == VERSION
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


async def test_h1_home_repair_resumes_an_update_and_leaves_the_button(home):
    await login_ready(home)
    bumped = bump_home(home)
    await bumped.tick()
    home.ha.catalog["version_latest"] = "0.9.1"
    await bumped.update()
    record = bumped.record()
    record.update(state="needs_reconcile", code="home_worker_operation_unconfirmed", enabled=False)
    bumped.save(record)
    calls = list(home.ha.calls)
    status = await bumped.repair()
    assert status["state"] == "running" and status["code"] is None and status["enabled"] is True
    assert bumped.record()["operation"]["kind"] == "update" and home.ha.calls == calls
    home.runner.capabilities["available"] = True
    home.runner.capabilities["providers"][0]["credential_state"] = "configured"
    assert (await bumped.tick())["state"] == "ready" and len(home.ha.posts("/update")) == 1

    record = bumped.record()
    record.update(release={**record["release"], "version": VERSION}, state="update_available",
                  code="home_worker_update_available", operation=None, enabled=True)
    bumped.save(record)
    before = copy.deepcopy(bumped.record())
    assert (await bumped.repair())["state"] == "update_available" and bumped.record() == before

    record = bumped.record()
    record.update(enabled=False, state="user_disabled", code="home_worker_observation_disabled")
    bumped.save(record)
    calls = list(home.ha.calls)
    status = await bumped.repair()
    assert status["state"] == "running" and bumped.record()["stage"] == "repository"
    assert bumped.record()["operation"] is None and home.ha.calls == calls


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
    clock, ha = Clock(), HomeSupervisor(version="0.9.0")
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


def reset_bridge(manager):
    manager.store.set_setting("bridge_maintenance", None)
    manager.ha.calls.clear()
    manager.ha.app = None
    manager.ha.repos.clear()
    manager.ha.catalog["version_latest"] = "0.9.0"
    manager.ha.job = {"uuid": JOB, "reference": BRIDGE_SLUG, "done": True, "errors": []}
    manager.ha.lose = manager.ha.update_state = None
    manager.bridge.listener, manager.bridge.seen = True, None


async def fresh_bridge_update(manager):
    reset_bridge(manager)
    await healthy(manager)
    bumped = bump(manager)
    await bumped.tick()
    manager.ha.catalog["version_latest"] = "0.9.1"
    return bumped


def home_operation(clock, kind, *, job=True):
    return {"kind": kind, "state": "acknowledged", "created": clock(), "job_id": JOB if job else None,
            "deadline": clock() + HOME_OPERATION_TIMEOUT}


async def stage_home(home, *, stage, operation=None, slug=True, present=True, app_version="0.7.0"):
    home.store.set_setting("home_worker_maintenance", None)
    home.ha.calls.clear()
    home.ha.app = None
    home.ha.repos[:] = [{"source": HomeWorkerRelease().repository, "slug": HOME_REPO}]
    home.ha.catalog["version_latest"] = "0.9.0"
    home.ha.job = {"uuid": JOB, "reference": HOME_SLUG, "done": True, "errors": []}
    home.ha.lose = home.ha.update_state = None
    await home.authorize()
    record = home.record()
    record.update(release={**record["release"], "version": "0.7.0"}, stage=stage, state="running", code=None,
                  operation=operation, enabled=True, managed=False)
    if slug:
        record.update(slug=HOME_SLUG, repository_id=HOME_REPO)
    else:
        record.pop("slug", None)
        record.pop("repository_id", None)
    home.save(record)
    if present:
        home.ha.install_app()
        home.ha.app["version"] = app_version
    return bump_home(home, "0.9.0")


async def test_b1_repair_resumes_an_interrupted_bridge_update(manager):
    bumped = await fresh_bridge_update(manager)
    await bumped.update()
    record = bumped.record()
    record.update(state="needs_reconcile", code="bridge_operation_unconfirmed")
    bumped.save(record)
    posts = len(manager.ha.writes("/update"))
    status = await bumped.repair()
    assert status["state"] == "pending" and status["code"] is None
    assert bumped.record()["operation"]["kind"] == "update"
    manager.bridge.seen = manager.clock()
    assert (await bumped.tick())["state"] == "healthy"
    assert len(manager.ha.writes("/update")) == posts and bumped.record()["release"]["version"] == "0.9.1"

    record = bumped.record()
    record.update(release={**record["release"], "version": "0.8.0"}, state="needs_reconcile",
                  code="bridge_observation_unavailable", operation=None)
    manager.ha.app["version"] = "0.8.0"
    bumped.save(record)
    assert (await bumped.repair())["state"] == "pending"
    assert (await bumped.tick())["state"] == "update_available" and not manager.ha.writes("/update")[posts:]

    record = bumped.record()
    record["release"] = {**record["release"], "repository": LEGACY}
    record.update(state="needs_reconcile", enabled=True)
    bumped.save(record)
    with pytest.raises(BridgeFault, match="bridge_release_channel_changed"):
        await bumped.repair()
    record = bumped.record()
    record["release"] = asdict_release("0.8.0")
    record["code"] = "bridge_repair_budget_exhausted"
    bumped.save(record)
    with pytest.raises(BridgeFault, match="bridge_repair_budget_exhausted"):
        await bumped.repair()


async def test_b2_failed_bridge_update_can_be_retried_without_a_repost(manager):
    bumped = await fresh_bridge_update(manager)
    await bumped.update()
    manager.ha.app.update(version="0.9.0", state="started")
    manager.ha.job.update(done=True, errors=[{"message": "build failed"}])
    status = await bumped.tick()
    assert status["state"] == "update_available" and status["code"] == "bridge_update_failed"
    assert bumped.record()["operation"] is None and "build failed" not in json.dumps(bumped.record())
    assert status["update"]["available"] is True
    manager.ha.job.update(errors=[])
    await bumped.update()
    assert len(manager.ha.writes("/update")) == 2
    manager.ha.job.update(errors=[{"message": "late"}])
    manager.bridge.seen = manager.clock()
    assert (await bumped.tick())["state"] == "healthy"
    assert bumped.record()["release"]["version"] == "0.9.1" and len(manager.ha.writes("/update")) == 2

    bumped = await fresh_bridge_update(manager)
    await bumped.update()
    manager.ha.app.update(version="0.9.0", options={"host": "custom"})
    manager.ha.job.update(done=True, errors=[{"message": "build failed"}])
    status = await bumped.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == "bridge_supervisor_job_failed"
    assert bumped.record()["operation"]["kind"] == "update" and len(manager.ha.writes("/update")) == 1

    manager.ha.job["uuid"] = "bad"
    await bumped.repair()
    assert (await bumped.tick())["code"] == "bridge_job_contract_invalid"
    assert bumped.record()["operation"]["kind"] == "update"

    bumped = await fresh_bridge_update(manager)
    manager.ha.lose = ("POST", f"/store/addons/{BRIDGE_SLUG}/update")
    await bumped.update()
    manager.ha.app = None
    manager.clock.now += OPERATION_TIMEOUT + 1
    assert (await bumped.tick())["code"] == "bridge_operation_unconfirmed"
    assert bumped.record()["state"] == "needs_reconcile" and len(manager.ha.writes("/update")) == 1


async def test_b3_bridge_readback_grace_starts_when_the_build_finishes(manager):
    await healthy(manager)
    raw = copy.deepcopy(manager.ha.app)
    raw.update(version="0.9.0", state="startup")
    digest = manager.record()["options_digest"]
    kwargs = {"expected_version": "0.9.0", "options_digest": digest, "created": manager.clock()}
    assert manager.installed_pin_problem(raw, manager.record(), prior_state="startup", **kwargs) == (
        "bridge_update_starting")
    raw["state"] = "started"
    assert manager.installed_pin_problem(raw, manager.record(), prior_state="error", **kwargs) is None
    assert manager.installed_pin_problem(raw, manager.record(), prior_state="unknown", **kwargs) is None
    raw["state"] = "startup"
    assert manager.installed_pin_problem(raw, manager.record(), prior_state="stopped", **kwargs) == (
        "bridge_update_state_mismatch")

    bumped = await fresh_bridge_update(manager)
    await bumped.update()
    manager.ha.app.update(version="0.9.0", state="started")
    manager.ha.job["done"] = False
    manager.clock.now += STARTUP_GRACE + 50
    assert (await bumped.tick())["state"] == "waiting" and "done_at" not in bumped.record()["operation"]
    manager.ha.job["done"] = True
    manager.ha.app.update(version="0.9.1", state="startup")
    assert (await bumped.tick())["code"] == "bridge_update_starting"
    assert bumped.record()["operation"]["done_at"] == manager.clock()
    manager.clock.now += STARTUP_GRACE - 1
    assert (await bumped.tick())["code"] == "bridge_update_starting"
    manager.clock.now += 2
    assert (await bumped.tick())["code"] == "bridge_update_state_mismatch"
    assert len(manager.ha.writes("/update")) == 1 and bumped.record()["release"]["version"] == "0.9.0"

    bumped = await fresh_bridge_update(manager)
    manager.ha.lose = ("POST", f"/store/addons/{BRIDGE_SLUG}/update")
    await bumped.update()
    manager.ha.lose = None
    manager.ha.app.update(version="0.9.1", state="startup")
    manager.clock.now += STARTUP_GRACE + 30
    assert (await bumped.tick())["code"] == "bridge_update_starting"
    assert bumped.record()["operation"]["done_at"] == manager.clock()
    assert len(manager.ha.writes("/update")) == 1


async def test_b3b_running_bridge_job_extends_its_deadline(manager):
    await manager.authorize()
    await advance_to(manager, "install")
    manager.ha.job["done"] = False
    await manager.tick()
    created = manager.record()["operation"]["created"]
    assert manager.record()["operation"]["kind"] == "install"
    manager.clock.now += OPERATION_TIMEOUT + 5
    assert (await manager.tick())["state"] == "waiting"
    assert manager.record()["operation"]["deadline"] == min(
        created + JOB_TIMEOUT, max(created + OPERATION_TIMEOUT, manager.clock() + OPERATION_TIMEOUT))
    assert len(manager.ha.writes("/install")) == 1
    manager.clock.now = created + JOB_TIMEOUT + 1
    assert (await manager.tick())["code"] == "bridge_operation_unconfirmed"
    assert len(manager.ha.writes("/install")) == 1


async def test_b5_setup_finishes_at_the_pinned_release_until_monitor(manager):
    await healthy(manager)
    record = manager.record()
    record.update(release={**record["release"], "version": "0.7.0"}, auto_repair=True)
    manager.save(record)
    manager.ha.app["version"] = "0.7.0"
    manager.bridge.listener = False
    manager.clock.now += STARTUP_GRACE + 1
    assert (await manager.tick())["state"] == "update_available"
    assert not manager.ha.writes("/restart") and not manager.ha.writes("/update")

    reset_bridge(manager)
    manager.ha.catalog["version_latest"] = "0.7.0"
    old = BridgeMaintenance(manager.store, manager.ha, manager.bridge,
                            release=BridgeRelease(version="0.7.0"), clock=manager.clock)
    await old.authorize()
    await old.tick()
    assert old.record()["stage"] == "repository" and old.record()["operation"]["kind"] == "add_repository"
    bumped = bump(old, "0.8.0")
    await bumped.tick()
    assert bumped.record()["release"]["version"] == "0.8.0" and bumped.record()["stage"] == "install"
    assert bumped.record()["operation"] is None and not manager.ha.writes("/update")

    reset_bridge(manager)
    manager.ha.catalog["version_latest"] = "0.7.0"
    old = BridgeMaintenance(manager.store, manager.ha, manager.bridge,
                            release=BridgeRelease(version="0.7.0"), clock=manager.clock)
    await old.authorize()
    await advance_to(old, "install")
    assert old.record()["operation"] is None
    manager.ha.catalog["version_latest"] = "0.8.0"
    bumped = bump(old, "0.8.0")
    await bumped.tick()
    assert bumped.record()["release"]["version"] == "0.8.0" and len(manager.ha.writes("/install")) == 1

    reset_bridge(manager)
    manager.ha.catalog["version_latest"] = "0.7.0"
    old = BridgeMaintenance(manager.store, manager.ha, manager.bridge,
                            release=BridgeRelease(version="0.7.0"), clock=manager.clock)
    await old.authorize()
    await advance_to(old, "install")
    manager.ha.job["done"] = False
    await old.tick()
    manager.ha.app["version"] = "0.7.0"
    bumped = bump(old, "0.8.0")
    await bumped.tick()
    assert bumped.record()["release"]["version"] == "0.7.0"
    assert bumped.record()["operation"]["kind"] == "install" and len(manager.ha.writes("/install")) == 1
    manager.ha.job["done"] = True
    await bumped.tick()
    assert bumped.record()["stage"] == "configure" and bumped.record()["release"]["version"] == "0.7.0"
    assert bumped.record()["managed"] is True and bumped.record()["operation"] is None
    await bumped.tick()
    assert manager.ha.writes("/options") and bumped.record()["release"]["version"] == "0.7.0"
    assert not manager.ha.writes("/update")


async def test_b6_noting_an_update_does_not_read_bridge_status(manager):
    await healthy(manager)
    callers = []
    original = manager.bridge.status

    def tracing():
        callers.append(traceback.extract_stack()[-2].name)
        return original()

    manager.bridge.status = tracing
    assert (await bump(manager).tick())["state"] == "update_available"
    assert "note_update_available" not in callers and "guard" in callers


async def test_h2_failed_home_update_can_be_retried(home):
    await login_ready(home)
    bumped = bump_home(home)
    await bumped.tick()
    home.ha.catalog["version_latest"] = "0.9.1"
    await bumped.update()
    home.ha.app.update(version=VERSION, state="started",
                       options={"pairing_fingerprint": FINGERPRINT, "pairing_code": NEW_CODE})
    home.ha.job.update(done=True, errors=[{"message": "build failed"}])
    status = await bumped.tick()
    assert status["state"] == "update_available" and status["code"] == "home_worker_update_failed"
    assert bumped.record()["operation"] is None and NEW_CODE not in json.dumps(bumped.record())
    assert status["update"]["available"] is True
    home.ha.job.update(errors=[])
    await bumped.update()
    assert len(home.ha.posts("/update")) == 2 and NEW_CODE not in json.dumps(bumped.record()["operation"])
    home.ha.job.update(errors=[{"message": "late"}])
    home.runner.capabilities["available"] = True
    home.runner.capabilities["providers"][0]["credential_state"] = "configured"
    assert (await bumped.tick())["release"]["version"] == "0.9.1"
    assert len(home.ha.posts("/update")) == 2

    home.store.set_setting("home_worker_maintenance", None)
    home.ha.app = None
    home.ha.repos.clear()
    home.ha.calls.clear()
    home.ha.catalog["version_latest"] = VERSION
    home.ha.job = {"uuid": JOB, "reference": HOME_SLUG, "done": True, "errors": []}
    await login_ready(home)
    bumped = bump_home(home)
    await bumped.tick()
    home.ha.catalog["version_latest"] = "0.9.1"
    await bumped.update()
    home.ha.app.update(version=VERSION, options={"pairing_code": "not-valid"})
    home.ha.job.update(done=True, errors=[{"message": "build failed"}])
    status = await bumped.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == "home_worker_supervisor_job_failed"
    assert bumped.record()["operation"]["kind"] == "update"
    home.ha.job["uuid"] = "bad"
    await bumped.repair()
    assert (await bumped.tick())["code"] == "home_worker_job_contract_invalid"
    assert bumped.record()["operation"]["kind"] == "update" and len(home.ha.posts("/update")) == 1


async def test_h3_home_readback_grace_starts_when_the_build_finishes(home):
    await login_ready(home)
    raw = copy.deepcopy(home.ha.app)
    raw.update(version="0.9.0", state="startup")
    digest = home.options_snapshot({})
    kwargs = {"expected_version": "0.9.0", "options_digest": digest, "created": home.clock()}
    assert home.installed_pin_problem(raw, home.record(), prior_state="startup", **kwargs) == (
        "home_worker_update_starting")
    raw["state"] = "started"
    assert home.installed_pin_problem(raw, home.record(), prior_state="error", **kwargs) is None
    assert home.installed_pin_problem(raw, home.record(), prior_state="unknown", **kwargs) is None
    raw["state"] = "startup"
    assert home.installed_pin_problem(raw, home.record(), prior_state="stopped", **kwargs) == (
        "home_worker_update_state_mismatch")

    bumped = bump_home(home)
    await bumped.tick()
    home.ha.catalog["version_latest"] = "0.9.1"
    await bumped.update()
    home.ha.app.update(version=VERSION, state="started")
    home.ha.job["done"] = False
    home.clock.now += HOME_STARTUP_GRACE + 50
    assert (await bumped.tick())["state"] == "waiting" and "done_at" not in bumped.record()["operation"]
    home.ha.job["done"] = True
    home.ha.app.update(version="0.9.1", state="startup")
    assert (await bumped.tick())["code"] == "home_worker_update_starting"
    assert bumped.record()["operation"]["done_at"] == home.clock()
    home.clock.now += HOME_STARTUP_GRACE - 1
    assert (await bumped.tick())["code"] == "home_worker_update_starting"
    home.clock.now += 2
    assert (await bumped.tick())["code"] == "home_worker_update_state_mismatch"
    assert len(home.ha.posts("/update")) == 1 and bumped.record()["release"]["version"] == VERSION


async def test_h4_home_setup_adopts_a_missing_app_and_records_an_old_install(home):
    bumped = await stage_home(home, stage="repository", slug=False, present=False,
                              operation=home_operation(home.clock, "add_repository", job=False))
    home.ha.repos.clear()
    assert (await bumped.tick())["state"] == "waiting"
    assert bumped.record()["release"]["version"] == "0.9.0"
    assert bumped.record()["operation"]["kind"] == "add_repository" and bumped.record()["stage"] == "repository"
    assert not home.ha.posts("/update") and not home.ha.posts("/repositories")

    home.store.set_setting("home_worker_maintenance", None)
    home.ha.app = None
    home.ha.repos.clear()
    home.ha.calls.clear()
    home.ha.catalog["version_latest"] = VERSION
    home.ha.job = {"uuid": JOB, "reference": HOME_SLUG, "done": True, "errors": []}
    await login_ready(home)
    installed_posts = len(home.ha.posts("/install"))
    home.ha.app = None
    record = home.record()
    record.update(release={**record["release"], "version": "0.7.0"}, state="user_removed", enabled=False)
    home.save(record)
    await home.repair()
    assert home.record()["stage"] == "repository" and home.record()["release"]["version"] == "0.7.0"
    await home.tick()
    assert home.record()["release"]["version"] == VERSION and home.record()["stage"] == "install"
    await home.tick()
    assert len(home.ha.posts("/install")) == installed_posts + 1 and not home.ha.posts("/update")

    bumped = await stage_home(home, stage="install", present=False, operation=home_operation(home.clock, "install"))
    home.ha.job["done"] = False
    assert (await bumped.tick())["state"] == "waiting" and bumped.record()["operation"]["kind"] == "install"
    home.ha.job["done"] = True
    assert (await bumped.tick())["state"] == "waiting" and bumped.record()["release"]["version"] == "0.7.0"

    bumped = await stage_home(home, stage="install", app_version="0.9.0",
                              operation=home_operation(home.clock, "install"))
    await bumped.tick()
    assert bumped.record()["release"]["version"] == "0.9.0" and bumped.record()["stage"] == "start"
    assert bumped.record()["operation"] is None and bumped.record()["state"] == "running"
    assert bumped.record()["managed"] is True and bumped.record()["installed_by_us"] is True
    assert bumped.record()["install_confirmation"] == "supervisor_job_and_app"
    assert not home.ha.posts("/install") and not home.ha.posts("/start")

    bumped = await stage_home(home, stage="install", app_version="0.7.0",
                              operation=home_operation(home.clock, "install"))
    status = await bumped.tick()
    assert status["state"] == "update_available" and status["code"] == "home_worker_update_available"
    assert bumped.record()["stage"] == "start" and bumped.record()["release"]["version"] == "0.7.0"
    assert bumped.record()["install_confirmation"] == "supervisor_job_and_app"
    assert bumped.record()["managed"] is True and bumped.record()["installed_by_us"] is True
    await bumped.tick()
    assert bumped.record()["state"] == "update_available" and not home.ha.posts("/start")

    bumped = await stage_home(home, stage="install", app_version="0.7.0",
                              operation=home_operation(home.clock, "install", job=False))
    await bumped.tick()
    assert bumped.record()["install_confirmation"] == "app_postcondition"

    bumped = await stage_home(home, stage="install", app_version="0.7.0",
                              operation=home_operation(home.clock, "install"))
    home.ha.app["state"] = "started"
    status = await bumped.tick()
    assert status["state"] == "needs_reconcile" and status["code"] == "home_worker_initial_configuration_changed"
    assert not home.ha.posts("/start")

    bumped = await stage_home(home, stage="pair", app_version="0.7.0",
                              operation=home_operation(home.clock, "restart"))
    status = await bumped.tick()
    assert status["state"] == "update_available" and bumped.record()["operation"] is None
    assert bumped.record()["release"]["version"] == "0.7.0" and not home.ha.posts("/restart")

    bumped = await stage_home(home, stage="monitor", present=False)
    status = await bumped.tick()
    assert status["state"] == "user_removed" and status["code"] == "home_worker_app_removed"
    assert status["enabled"] is False and not home.ha.posts("/install")


async def test_h5_adopt_release_keeps_setup_stages(home):
    bumped = bump_home(home, "0.9.0")
    raw = {"version": "0.9.0", "state": "stopped"}
    base = {"operation": {"kind": "install"}, "release": {"version": "0.7.0"}}
    for stage in ("repository", "install", "start", "pair", "login", "monitor"):
        bumped.adopt_release({**base, "stage": stage}, raw)
        assert bumped.record()["stage"] == stage and bumped.record()["operation"] is None
        assert bumped.record()["release"]["version"] == "0.9.0"
    bumped.adopt_release({**base, "stage": "configure"}, raw)
    assert bumped.record()["stage"] == "monitor"

    home.store.set_setting("home_worker_maintenance", None)
    home.ha.app = None
    home.ha.repos.clear()
    await home.authorize()
    record = home.record()
    record.update(release={**record["release"], "version": "0.7.0"}, state="update_available", stage="repository",
                  slug=HOME_SLUG, repository_id=HOME_REPO)
    home.save(record)
    home.ha.repos = [{"source": HomeWorkerRelease().repository, "slug": HOME_REPO}]
    home.ha.install_app()
    home.ha.app.update(version="0.9.0", state="started", options={})
    home.ha.catalog["version_latest"] = "0.9.0"
    bumped = bump_home(home, "0.9.0")
    await bumped.update()
    assert bumped.record()["release"]["version"] == "0.9.0" and bumped.record()["stage"] == "install"
    assert bumped.record()["operation"] is None
