"""Supervisor contract fixtures only: no real App installation or account claim."""
import asyncio
import copy
import json

import pytest
from agentic_controller.bridge_maintenance import (
    HEARTBEAT_MAX_AGE,
    KEY,
    OPERATION_TIMEOUT,
    STARTUP_GRACE,
    STOP_CONFIRM_SECONDS,
    BridgeFault,
    BridgeMaintenance,
    BridgeRelease,
)
from agentic_controller.ha import HAError

RELEASE = BridgeRelease()
REPO = "aabbccdd"
SLUG = REPO + "_agentic_bridge"
JOB = "e" * 32


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Bridge:
    def __init__(self, clock):
        self.clock = clock
        self.options = {"protocol": 1, "host": "192.168.177.1", "port": 8444,
                        "fingerprint": "f" * 64, "token": "t" * 64, "pairing_id": "b" * 32}
        self.state, self.listener, self.seen = "prepared", True, None
        self.preparations = 0
        self.host_available = True

    async def require_host(self):
        self.host_guard()

    def host_guard(self):
        if not self.host_available:
            from fastapi import HTTPException
            raise HTTPException(503, "bridge_host_contract_required")

    def prepare_pairing(self):
        self.preparations += 1
        return copy.deepcopy(self.options)

    def status(self):
        return {"state": self.state, "pairing_id": self.options["pairing_id"],
                "listener_ready": self.listener, "service_last_seen": self.seen}


class Supervisor:
    def __init__(self):
        self.token = "fixture-ha-token"
        self.revoked = False
        self.repos, self.calls = [], []
        self.app = None
        self.catalog = {"slug": SLUG, "repository": REPO, "version_latest": "0.9.0", "available": True,
                        "ingress": True, "build": True, "hassio_api": False, "homeassistant_api": False,
                        "auth_api": False, "docker_api": False, "full_access": False, "host_network": False,
                        "host_pid": False}
        self.job = {"uuid": JOB, "reference": SLUG, "done": True, "errors": []}
        self.fail, self.lose, self.before = None, None, None
        self.reload_version, self.update_state = None, None
        self.healthy = True
        self.owner_hook = None
        self.owner_calls = 0
        self.core_state, self.core_info = "running", None  # GET /info; core_info overrides the body.

    async def owner(self, owner):
        self.owner_calls += 1
        if self.owner_hook:
            await self.owner_hook()
        return {"id": owner, "is_owner": True, "is_active": True} if not self.revoked else None

    def install_app(self):
        self.app = {**self.catalog, "version": "0.9.0", "protected": True, "ingress_port": 8099,
                    "network": {}, "options": {}, "state": "stopped", "boot": "auto",
                    "privileged": [], "devices": [], "host_ipc": False, "host_uts": False,
                    "host_dbus": False, "apparmor": "default",
                    "ingress_panel": False, "auto_update": False, "watchdog": False}

    async def request(self, method, path, body=None, *, supervisor, timeout):
        assert supervisor is True and timeout == 30
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.before:
            await self.before(method, path)
        if self.fail == (method, path):
            raise HAError("private fixture token should never escape")
        if path == "/info":
            assert method == "GET"
            value = {"state": self.core_state} if self.core_info is None else self.core_info
        elif path == "/supervisor/info":
            value = {"healthy": self.healthy}
        elif path == "/store/repositories":
            if method == "POST":
                assert body == {"repository": RELEASE.repository}
                self.repos.append({"source": RELEASE.repository, "slug": REPO})
                value = None
            else:
                value = self.repos
        elif path == "/addons":
            value = {"addons": [self.app] if self.app else []}
        elif path == f"/store/addons/{SLUG}/info":
            value = self.catalog
        elif path == f"/store/addons/{SLUG}/availability":
            value = None
        elif path == "/store/reload":
            assert method == "POST" and body is None
            if self.reload_version:
                self.catalog["version_latest"] = self.reload_version
            value = None
        elif path == f"/store/addons/{SLUG}/update":
            assert method == "POST" and body == {"backup": False, "background": True}
            assert self.app
            value = {"job_id": JOB}
            if self.lose != (method, path):
                self.app["version"] = self.catalog["version_latest"]
                if self.update_state:
                    self.app["state"] = self.update_state
        elif path == f"/store/addons/{SLUG}/install":
            assert body == {"background": True}
            self.install_app()
            value = {"job_id": JOB}
        elif path == f"/addons/{SLUG}/info":
            assert self.app
            value = self.app
        elif path == f"/jobs/{JOB}":
            value = self.job
        elif path == f"/addons/{SLUG}/options/validate":
            value = {"valid": True}
        elif path == f"/addons/{SLUG}/options":
            self.app.update(copy.deepcopy(body))
            value = None
        elif path in {f"/addons/{SLUG}/start", f"/addons/{SLUG}/restart"}:
            self.app["state"] = "started"
            value = None
        else:
            raise AssertionError((method, path))
        if self.lose == (method, path):
            raise HAError("response lost after effect")
        return copy.deepcopy(value)

    def writes(self, suffix):
        return [call for call in self.calls if call[0] == "POST" and call[1].endswith(suffix)]


@pytest.fixture
def manager(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed"})
    clock = Clock()
    ha, bridge = Supervisor(), Bridge(clock)
    return BridgeMaintenance(store, ha, bridge, clock=clock)


async def advance_to(manager, stage):
    for _ in range(15):
        if manager.record()["stage"] == stage:
            return
        await manager.tick()
    raise AssertionError(manager.status())


async def installed(manager, *, auto_repair=False):
    await manager.authorize(auto_repair=auto_repair)
    await advance_to(manager, "monitor")
    return manager.ha.app


async def test_host_capability_loss_preserves_intent_and_dispatches_no_supervisor_effect(manager):
    await manager.authorize(auto_repair=True)
    await manager.tick()  # Persist and send one repository addition.
    intent = copy.deepcopy(manager.record()['operation'])
    writes = copy.deepcopy(manager.ha.calls)
    manager.bridge.host_available = False
    status = await manager.tick()
    assert status['state'] == 'unavailable' and status['code'] == 'bridge_host_contract_required'
    assert manager.record()['operation'] == intent and manager.ha.calls == writes
    for method in (manager.repair, lambda: manager.policy(auto_repair=True)):
        with pytest.raises(BridgeFault, match='bridge_host_contract_required'):
            await method()
    assert manager.ha.calls == writes


async def test_explicit_reconnect_requires_host_proof_before_rotating_identity(manager):
    await installed(manager)
    manager.suspend()
    previous = copy.deepcopy(manager.record())
    manager.bridge.host_available = False
    prepared = manager.bridge.preparations
    with pytest.raises(BridgeFault, match='bridge_host_contract_required'):
        await manager.authorize(auto_repair=True)
    assert manager.record() == previous and manager.bridge.preparations == prepared


async def test_complete_flow_reads_back_fixed_source_version_settings_and_heartbeat(manager):
    app = await installed(manager)
    assert manager.status()["state"] == "starting"
    assert manager.status()["managed"] is True
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy"
    assert app["options"] == manager.bridge.options
    assert manager.ha.writes("/install") and manager.ha.writes("/options") and manager.ha.writes("/start")
    assert app["watchdog"] is False and app["auto_update"] is False
    assert not any("/uninstall" in path or "/update" in path or "/rebuild" in path for _, path, _ in manager.ha.calls)
    public = json.dumps(manager.status())
    assert "t" * 64 not in public and "fixture-ha-token" not in public
    assert "t" * 64 not in json.dumps(manager.record())


async def test_status_is_pure_and_lifespan_does_not_enroll_old_completed_setup(manager):
    original = manager.store.setting(KEY)
    for _ in range(3):
        assert manager.status()["state"] == "not_authorized"
        await manager.tick()
    assert manager.store.setting(KEY) == original
    assert not manager.ha.calls and manager.bridge.preparations == 0


@pytest.mark.parametrize("setup,owner", [("running", "owner"), ("completed", None)])
async def test_authorization_requires_complete_setup_and_owner(manager, setup, owner):
    manager.store.put("bootstrap_v1", {"id": "setup", "state": setup})
    manager.store.set_setting("owner", owner)
    with pytest.raises(BridgeFault, match="bridge_setup_required"):
        await manager.authorize()
    assert not manager.record() and not manager.ha.calls


async def test_revoked_owner_and_browser_scope_fence_authorization(manager):
    manager.ha.revoked = True
    with pytest.raises(BridgeFault, match="bridge_owner_required"):
        await manager.authorize()
    manager.ha.revoked = False

    def guard():
        raise PermissionError("fixture revoked browser")

    with pytest.raises(PermissionError):
        await manager.authorize(commit_guard=guard)
    assert not manager.record() and manager.bridge.preparations == 0


@pytest.mark.parametrize("path", ["/store/repositories", f"/store/addons/{SLUG}/install",
                                   f"/addons/{SLUG}/options", f"/addons/{SLUG}/start"])
async def test_lost_mutation_response_recovers_by_readback_without_replay(manager, path):
    manager.ha.lose = ("POST", path)
    await installed(manager)
    assert len([call for call in manager.ha.calls if call[:2] == ("POST", path)]) == 1
    assert manager.status()["managed"] is True


async def test_process_restart_recovers_exact_pending_install_job(manager):
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    assert manager.record()["operation"]["job_id"] == JOB
    manager2 = BridgeMaintenance(manager.store, manager.ha, manager.bridge, clock=manager.clock)
    await advance_to(manager2, "monitor")
    assert len(manager.ha.writes("/install")) == 1
    assert any(path == f"/jobs/{JOB}" for _, path, _ in manager.ha.calls)


async def test_pending_job_waits_errors_block_without_raw_error(manager):
    manager.ha.job["done"] = False
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    assert (await manager.tick())["state"] == "waiting"
    manager.ha.job.update(done=True, errors=[{"message": "fixture-sensitive-error"}])
    assert (await manager.tick())["code"] == "bridge_supervisor_job_failed"
    assert "fixture-sensitive-error" not in json.dumps(manager.record())
    assert len(manager.ha.writes("/install")) == 1


@pytest.mark.parametrize("change", [{"done": 1}, {"uuid": "bad"}, {"reference": "other"}, {"errors": None}])
async def test_job_required_contract_checked(manager, change):
    manager.ha.job.update(change)
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    assert (await manager.tick())["code"] == "bridge_job_contract_invalid"


async def test_transport_failure_is_not_absence_and_preserves_existing_app(manager):
    await installed(manager, auto_repair=True)
    manager.ha.fail = ("GET", "/addons")
    assert (await manager.tick())["state"] == "unavailable"
    assert manager.record()["enabled"] and manager.ha.app
    assert not manager.ha.writes("/restart")
    assert len(manager.ha.writes("/install")) == 1


@pytest.mark.parametrize("change,state", [({"state": "stopped"}, "user_disabled"),
    ({"boot": "manual"}, "user_modified"), ({"watchdog": True}, "user_modified"),
    ({"ingress_panel": False}, "user_modified"), ({"options": {"host": "custom"}}, "user_modified"),
    ({"auto_update": True}, "user_modified")])
async def test_manual_intent_is_sticky_and_never_reversed(manager, change, state):
    app = await installed(manager, auto_repair=True)
    app.update(change)
    if state == "user_disabled":
        assert (await manager.tick())["code"] == "bridge_app_stop_pending"
        manager.clock.now += STOP_CONFIRM_SECONDS
    assert (await manager.tick())["state"] == state
    writes = len(manager.ha.calls)
    await manager.authorize(auto_repair=True)
    await manager.tick()
    assert len(manager.ha.calls) == writes
    assert not manager.record()["enabled"]
    assert not manager.ha.writes("/restart")


async def test_uninstall_never_reinstalls(manager):
    await installed(manager, auto_repair=True)
    manager.ha.app = None
    assert (await manager.tick())["state"] == "user_removed"
    await manager.authorize(auto_repair=True)
    await manager.tick()
    assert len(manager.ha.writes("/install")) == 1


async def test_existing_install_is_not_silently_adopted(manager):
    manager.ha.install_app()
    await manager.authorize()
    await advance_to(manager, "install")
    assert (await manager.tick())["code"] == "bridge_existing_app_unmanaged"
    assert not manager.ha.writes("/options") and not manager.ha.writes("/start")


@pytest.mark.parametrize("change", [{"hassio_api": True},
    {"host_network": True}, {"repository": "other"}, {"build": False}, {"ingress": False}])
async def test_store_drift_blocks_install(manager, change):
    manager.ha.catalog.update(change)
    await manager.authorize()
    await advance_to(manager, "install")
    assert (await manager.tick())["code"] == "bridge_app_contract_mismatch"
    assert not manager.ha.writes("/install")


async def test_new_upstream_fields_are_accepted(manager):
    manager.ha.catalog["future_feature"] = {"version": "unknown"}
    await installed(manager)
    assert manager.status()["managed"] is True


async def test_exact_repo_source_not_metadata_url_drives_install(manager):
    manager.ha.repos = [{"source": "https://untrusted.invalid/repo", "slug": REPO,
                         "url": RELEASE.repository}]
    await manager.authorize()
    await manager.tick()
    assert manager.ha.writes("/repositories")
    assert manager.status()["stage"] == "repository"


async def test_bounded_restart_budget_survives_restart_and_cannot_be_reset_by_retry(manager):
    await installed(manager, auto_repair=True)
    for expected in (1, 2):
        await manager.tick()  # A restarted Controller first observes its own started run.
        manager.clock.now += STARTUP_GRACE + 1
        await manager.tick()
        assert len(manager.ha.writes("/restart")) == expected
        manager = BridgeMaintenance(manager.store, manager.ha, manager.bridge, clock=manager.clock)
    await manager.tick()
    manager.clock.now += STARTUP_GRACE + 1
    assert (await manager.tick())["code"] == "bridge_repair_budget_exhausted"
    with pytest.raises(BridgeFault, match="bridge_repair_budget_exhausted"):
        await manager.repair()
    manager.clock.now += 100000
    await manager.tick()
    assert len(manager.ha.writes("/restart")) == 2


async def test_repair_is_opt_in_and_outer_listener_outage_never_restarts_app(manager):
    await installed(manager)
    manager.clock.now += STARTUP_GRACE + 1
    assert (await manager.tick())["state"] == "degraded"
    await manager.policy(auto_repair=True)
    manager.bridge.listener = False
    assert (await manager.tick())["code"] == "bridge_listener_unavailable"
    assert not manager.ha.writes("/restart")


async def test_recent_service_heartbeat_is_health_without_claiming_owner_ui(manager):
    await installed(manager, auto_repair=True)
    manager.clock.now += STARTUP_GRACE + 1
    manager.bridge.seen = manager.clock()
    assert manager.bridge.state == "prepared"
    assert (await manager.tick())["state"] == "healthy"
    manager.clock.now += HEARTBEAT_MAX_AGE + 1
    await manager.tick()
    assert len(manager.ha.writes("/restart")) == 1


async def test_unknown_restart_effect_is_not_repeated(manager):
    await installed(manager, auto_repair=True)
    manager.clock.now += STARTUP_GRACE + 1
    manager.ha.lose = ("POST", f"/addons/{SLUG}/restart")
    assert (await manager.tick())["state"] == "unavailable"
    await manager.tick()
    manager.clock.now += OPERATION_TIMEOUT + 1
    assert (await manager.tick())["state"] == "needs_reconcile"
    await manager.repair()
    await manager.tick()
    assert len(manager.ha.writes("/restart")) == 1


async def test_owner_changes_while_probe_waits_prevent_effects(manager):
    await manager.authorize()
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(method, path):
        if path == "/supervisor/info":
            entered.set()
            await release.wait()

    manager.ha.before = barrier
    task = asyncio.create_task(manager.tick())
    await entered.wait()
    manager.ha.token = "replacement-token"
    release.set()
    result = await task
    assert result["code"] == "bridge_authorization_changed"
    assert not manager.ha.writes("/repositories")


async def test_disable_during_await_remains_disabled_and_no_effect(manager):
    await manager.authorize()
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(method, path):
        entered.set()
        await release.wait()

    manager.ha.before = barrier
    task = asyncio.create_task(manager.tick())
    await entered.wait()
    manager.suspend()
    release.set()
    assert (await task)["state"] == "disabled"
    assert not manager.ha.writes("/repositories")


async def test_configuration_edit_before_initial_options_is_not_overwritten(manager):
    await manager.authorize()
    await advance_to(manager, "configure")
    manager.ha.app["watchdog"] = True
    assert (await manager.tick())["code"] == "bridge_options_changed_or_unconfirmed"
    assert not manager.ha.writes("/options")


async def test_supervisor_unhealthy_blocks_only_bridge_effect(manager):
    manager.ha.healthy = False
    await manager.authorize()
    assert (await manager.tick())["code"] == "bridge_supervisor_not_healthy"
    assert not manager.ha.writes("/repositories")
    manager.ha.healthy = True
    await manager.tick()
    assert len(manager.ha.writes("/repositories")) == 1


async def test_record_ahead_of_the_controller_waits_without_requests(manager):
    await manager.authorize()
    changed = BridgeMaintenance(manager.store, manager.ha, manager.bridge,
                                release=BridgeRelease(version="0.7.1"), clock=manager.clock)
    assert (await changed.tick())["code"] == "bridge_release_ahead"
    assert not manager.ha.calls


async def test_new_setup_consent_auto_enrolls_once_and_survives_disable(manager):
    manager.store.put("bootstrap_v1", {"id": "setup", "run_id": "c" * 32, "state": "completed",
                                       "bridge_auto_enroll": True})
    await manager.tick()
    assert manager.record()["setup_run_id"] == "c" * 32
    assert manager.record()["auto_repair"] is True
    manager.suspend()
    calls = len(manager.ha.calls)
    await manager.tick()
    assert manager.status()["state"] == "disabled" and len(manager.ha.calls) == calls


@pytest.mark.parametrize("setting,value,code", [("paused", True, "bridge_paused"),
    ("updater_gate", {"id": "fixture-update"}, "bridge_update_in_progress")])
async def test_setup_preauthorization_waits_for_effect_gate_without_consuming_it(manager, setting, value, code):
    manager.store.put("bootstrap_v1", {"id": "setup", "run_id": "c" * 32, "state": "completed",
                                       "bridge_auto_enroll": True})
    manager.store.set_setting(setting, value)
    assert (await manager.tick())["code"] == code
    assert not manager.record() and not manager.ha.calls
    manager.store.set_setting(setting, False)
    await manager.tick()
    assert manager.record() and manager.ha.writes("/repositories")


async def test_owner_revoked_during_auto_enrollment_no_mutation_and_retry_is_bounded_tick(manager):
    manager.store.put("bootstrap_v1", {"id": "setup", "state": "completed", "bridge_auto_enroll": True})
    manager.ha.revoked = True
    assert (await manager.tick())["code"] == "bridge_owner_required"
    assert manager.ha.owner_calls == 1 and not manager.ha.calls


@pytest.mark.parametrize("setting,value", [("paused", True), ("updater_gate", {"id": "fixture"})])
async def test_effect_gate_closes_across_await(manager, setting, value):
    await manager.authorize()

    async def change_gate(method, path):
        if path == "/supervisor/info":
            manager.store.set_setting(setting, value)

    manager.ha.before = change_gate
    assert (await manager.tick())["state"] == "unavailable"
    assert not manager.ha.writes("/repositories")


async def test_config_edit_during_validation_fences_options_write(manager):
    await manager.authorize()
    await advance_to(manager, "configure")

    async def change_native_option(method, path):
        if path.endswith("/options/validate"):
            manager.ha.app["options"]["token"] = "native-owner-change"

    manager.ha.before = change_native_option
    assert (await manager.tick())["state"] == "user_modified"
    assert not manager.ha.writes("/options")


async def test_startup_state_is_not_contract_failure_or_restart_request(manager):
    await installed(manager, auto_repair=True)
    manager.ha.app["state"] = "startup"
    assert (await manager.tick())["state"] == "starting"
    manager.clock.now += STARTUP_GRACE + 1
    assert (await manager.tick())["code"] == "bridge_app_state_unconfirmed"
    assert not manager.ha.writes("/restart")


@pytest.mark.parametrize("key,value", [("token", 123), ("port", True), ("protocol", True),
                                        ("host", "127.0.0.1"), ("fingerprint", "short")])
async def test_pairing_options_are_fixed_and_typed(manager, key, value):
    manager.bridge.options[key] = value
    with pytest.raises(BridgeFault, match="bridge_pairing_contract_invalid"):
        await manager.authorize()
    assert not manager.record() and not manager.ha.calls


@pytest.mark.parametrize("initial_state", ["started", "stopped"])
async def test_explicit_reconnect_rotates_only_own_unchanged_app_then_waits_new_heartbeat(manager, initial_state):
    await installed(manager, auto_repair=True)
    old_id = manager.record()["pairing_id"]
    original_options = copy.deepcopy(manager.ha.app["options"])
    manager.ha.app["state"] = initial_state
    manager.suspend()
    manager.bridge.state = "revoked"
    original = manager.bridge.prepare_pairing

    def rotate(*, repair=False):
        if repair:
            manager.bridge.options.update(pairing_id="c" * 32, token="v" * 64)
            manager.bridge.state = "prepared"
        return original()

    manager.bridge.prepare_pairing = rotate
    assert (await manager.authorize(auto_repair=True))["state"] == "pending"
    assert manager.record()["pairing_id"] != old_id
    assert manager.ha.app["options"] == original_options
    await advance_to(manager, "reconnect")
    await manager.tick()
    assert (await manager.tick())["state"] == "waiting"
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy"
    assert manager.ha.app["options"] == manager.bridge.options
    assert manager.record()["repair_count"] == 0
    assert len(manager.ha.writes("/install")) == 1


@pytest.mark.parametrize("field,value", [("options", {"token": "native-change"}), ("boot", "manual"),
                                         ("version", "different"), ("host_network", True)])
async def test_reconnect_refuses_native_config_source_or_security_drift(manager, field, value):
    await installed(manager)
    manager.suspend()
    manager.ha.app[field] = value
    before = manager.bridge.preparations
    with pytest.raises(BridgeFault):
        await manager.authorize(auto_repair=True)
    assert manager.bridge.preparations == before
    assert manager.record()["state"] == "disabled"


async def test_reconnect_never_resets_unknown_operation_or_restart_budget(manager):
    await installed(manager, auto_repair=True)
    record = manager.record()
    record.update(operation={"kind": "restart", "state": "sent"}, repairs=[1, 2], repair_count=2)
    manager.save(record)
    manager.suspend()
    with pytest.raises(BridgeFault, match="bridge_reconnect_unconfirmed"):
        await manager.authorize(auto_repair=True)
    assert manager.record()["repairs"] == [1, 2] and manager.record()["repair_count"] == 2


async def test_cleaned_completed_install_job_uses_exact_fresh_postcondition(manager):
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    manager.ha.fail = ("GET", f"/jobs/{JOB}")
    resumed = BridgeMaintenance(manager.store, manager.ha, manager.bridge, clock=manager.clock)
    assert (await resumed.tick())["stage"] == "configure"
    assert resumed.status()["install_confirmation"] == "app_postcondition_job_unavailable"
    await advance_to(resumed, "monitor")
    assert len(manager.ha.writes("/install")) == 1


async def test_missing_job_without_installed_app_is_not_success_or_reinstall(manager):
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    manager.ha.app = None
    manager.ha.fail = ("GET", f"/jobs/{JOB}")
    assert (await manager.tick())["state"] == "unavailable"
    manager.clock.now += OPERATION_TIMEOUT + 1
    assert (await manager.tick())["state"] == "needs_reconcile"
    assert manager.record()["managed"] is False
    assert len(manager.ha.writes("/install")) == 1
    assert not manager.ha.writes("/options")


@pytest.mark.parametrize("change", [{"options": {"host": "native-change"}}, {"boot": "manual"},
    {"state": "started"}, {"watchdog": True}, {"auto_update": True}, {"ingress_panel": True}])
async def test_cleaned_job_does_not_adopt_native_initial_settings(manager, change):
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    manager.ha.fail = ("GET", f"/jobs/{JOB}")
    manager.ha.app.update(change)
    assert (await manager.tick())["code"] == "bridge_initial_configuration_changed"
    assert not manager.ha.writes("/options") and not manager.ha.writes("/start")


async def test_cleaned_job_revalidates_repository_and_fresh_owner(manager):
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    manager.ha.fail = ("GET", f"/jobs/{JOB}")
    manager.ha.repos[0]["source"] = "https://untrusted.invalid/repo"
    assert (await manager.tick())["code"] == "bridge_repository_changed"
    assert manager.record()["managed"] is False
    assert not manager.ha.writes("/options")


async def test_cleaned_job_fresh_owner_revocation_fences_adoption(manager):
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    manager.ha.fail = ("GET", f"/jobs/{JOB}")

    async def revoke(method, path):
        if path == f"/jobs/{JOB}":
            manager.ha.revoked = True

    manager.ha.before = revoke
    assert (await manager.tick())["code"] == "bridge_owner_required"
    assert manager.record()["managed"] is False
    assert not manager.ha.writes("/options")


@pytest.mark.parametrize("change", [{"privileged": ["SYS_ADMIN"]}, {"privileged": None},
    {"devices": ["/dev/kvm"]}, {"devices": None}, {"host_ipc": True}, {"host_uts": True},
    {"host_dbus": True}, {"apparmor": False}, {"apparmor": "disable"}, {"apparmor": "profile"},
    {"apparmor": None}])
async def test_installed_security_drift_never_starts_or_repairs(manager, change):
    await manager.authorize()
    await advance_to(manager, "install")
    await manager.tick()
    manager.ha.app.update(change)
    manager.ha.fail = ("GET", f"/jobs/{JOB}")
    assert (await manager.tick())["code"] == "bridge_app_contract_mismatch"
    assert not manager.ha.writes("/options") and not manager.ha.writes("/start")
    assert not manager.ha.writes("/restart")


# Supervisor transitions. Fixture core states only: no real HAOS boot, backup or Controller restart.

def posts(ha):
    return [call for call in ha.calls if call[0] == "POST"]


async def healthy(manager):
    await installed(manager, auto_repair=True)
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy"


@pytest.mark.parametrize("steps", [
    [("setup", "unknown"), ("startup", "stopped"), ("startup", "stopped"), ("startup", "startup"),
     ("startup", "started"), ("running", "started")],
    [("freeze", "stopped"), ("freeze", "stopped"), ("freeze", "startup"), ("running", "started")],
], ids=["haos_reboot", "cold_backup"])
async def test_haos_reboot_or_cold_backup_keeps_the_sidebar_without_restart(manager, steps):
    await healthy(manager)
    sent = len(posts(manager.ha))
    manager.clock.now += 600  # grace_until expired; the heartbeat stays stale until the App reconnects.
    for core, app_state in steps:
        manager.ha.core_state, manager.ha.app["state"] = core, app_state
        manager.clock.now += 60
        status = await manager.tick()
        assert status["state"] in {"waiting", "starting"}, status
        assert status["code"] in {"bridge_supervisor_busy", "bridge_heartbeat_pending"}
        assert manager.record()["enabled"] is True and manager.record()["stopped_since"] is None
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy"
    assert len(posts(manager.ha)) == sent and manager.record()["repairs"] == []
    assert manager.record()["repair_count"] == 0


async def test_real_stop_is_confirmed_after_the_window_and_survives_controller_restart(manager):
    await healthy(manager)
    sent = len(posts(manager.ha))
    manager.ha.app["state"] = "stopped"
    status = await manager.tick()
    assert (status["state"], status["code"]) == ("waiting", "bridge_app_stop_pending")
    assert manager.record()["enabled"] is True and manager.record()["stopped_since"] == manager.clock()
    # Any other observation resets the timer.
    manager.clock.now += STOP_CONFIRM_SECONDS
    manager.ha.app["state"] = "started"
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy" and manager.record()["stopped_since"] is None
    manager.ha.app["state"] = "stopped"
    await manager.tick()
    first = manager.clock()
    manager.clock.now += STOP_CONFIRM_SECONDS - 1
    restarted = BridgeMaintenance(manager.store, manager.ha, manager.bridge, clock=manager.clock)
    assert (await restarted.tick())["code"] == "bridge_app_stop_pending"
    assert restarted.record()["stopped_since"] == first and restarted.record()["enabled"] is True
    manager.clock.now += 1
    status = await restarted.tick()
    assert (status["state"], status["code"]) == ("user_disabled", "bridge_app_stopped")
    assert restarted.record()["enabled"] is False and len(posts(manager.ha)) == sent


async def test_supervisor_transition_clears_a_pending_stop(manager):
    await healthy(manager)
    manager.ha.app["state"] = "stopped"
    await manager.tick()
    manager.clock.now += STOP_CONFIRM_SECONDS
    manager.ha.core_state = "freeze"
    assert (await manager.tick())["code"] == "bridge_supervisor_busy"
    assert manager.record()["stopped_since"] is None and manager.record()["enabled"] is True
    manager.ha.core_state = "running"
    assert (await manager.tick())["code"] == "bridge_app_stop_pending"
    manager.clock.now += STOP_CONFIRM_SECONDS
    assert (await manager.tick())["state"] == "user_disabled"


@pytest.mark.parametrize("kind,path,body", [
    ("add_repository", "/store/repositories", {"repository": RELEASE.repository}),
    ("install", f"/store/addons/{SLUG}/install", {"background": True}),
    ("configure", f"/addons/{SLUG}/options", {"options": {}}),
    ("start", f"/addons/{SLUG}/start", None),
    ("restart", f"/addons/{SLUG}/restart", None),
    ("update", f"/store/addons/{SLUG}/update", {"backup": False, "background": True})])
@pytest.mark.parametrize("core", ["startup", "freeze"])
async def test_no_effect_is_posted_unless_supervisor_is_running(manager, kind, path, body, core):
    await healthy(manager)
    sent, before = len(posts(manager.ha)), copy.deepcopy(manager.record())
    manager.ha.core_state = core
    with pytest.raises(BridgeFault) as raised:
        await manager.effect(manager.record(), manager.ha.token, kind, path, body)
    assert (raised.value.code, raised.value.state) == ("bridge_supervisor_busy", "waiting")
    assert len(posts(manager.ha)) == sent and manager.record()["operation"] == before["operation"] is None


async def test_auto_repair_restart_waits_for_supervisor_running(manager):
    await healthy(manager)
    manager.clock.now += STARTUP_GRACE + HEARTBEAT_MAX_AGE
    manager.ha.core_state = "freeze"
    status = await manager.tick()
    assert (status["state"], status["code"]) == ("waiting", "bridge_supervisor_busy")
    assert not manager.ha.writes("/restart") and manager.record()["repair_count"] == 0
    manager.ha.core_state = "running"
    # The App may have been restarted inside the busy window: a fresh grace applies.
    assert (await manager.tick())["code"] == "bridge_heartbeat_pending"
    manager.clock.now += STARTUP_GRACE
    # Supervisor turns busy between the heartbeat gate and the POST: still nothing is sent.
    reads = []

    async def freeze_on_effect_read(method, path):
        if path == "/info":
            reads.append(path)
            if len(reads) == 2:
                manager.ha.core_state = "freeze"

    manager.ha.before = freeze_on_effect_read
    assert (await manager.tick())["code"] == "bridge_supervisor_busy"
    assert len(reads) == 2 and not manager.ha.writes("/restart") and manager.record()["operation"] is None
    manager.ha.before, manager.ha.core_state = None, "running"
    await manager.tick()
    manager.clock.now += STARTUP_GRACE
    await manager.tick()
    assert len(manager.ha.writes("/restart")) == 1


@pytest.mark.parametrize("fault", ["transport", [], {}, {"state": None}, {"state": 1}, "running"])
async def test_unreadable_supervisor_state_is_transient(manager, fault):
    await healthy(manager)
    sent = len(posts(manager.ha))
    manager.ha.app["state"] = "stopped"
    if fault == "transport":
        manager.ha.fail = ("GET", "/info")
    else:
        manager.ha.core_info = fault
    status = await manager.tick()
    assert (status["state"], status["code"]) == ("unavailable", "bridge_observation_unavailable")
    assert manager.record()["enabled"] is True
    manager.ha.fail = manager.ha.core_info = None
    manager.ha.app["state"] = "started"
    manager.bridge.seen = manager.clock()
    assert (await manager.tick())["state"] == "healthy"
    assert len(posts(manager.ha)) == sent


async def test_controller_restart_gives_a_fresh_heartbeat_grace_then_budgeted_repair(manager):
    await healthy(manager)
    calls = len(manager.ha.calls)
    await manager.tick()
    assert ("GET", "/info", None) not in manager.ha.calls[calls:]  # Healthy ticks do not read /info.
    manager.clock.now += 3600  # Controller (and HAOS) were down: grace_until and heartbeat are stale.
    restarted = BridgeMaintenance(manager.store, manager.ha, manager.bridge, clock=manager.clock)
    start = manager.clock()
    for offset in (0, 40, 80, STARTUP_GRACE - 1):
        manager.clock.now = start + offset
        status = await restarted.tick()
        assert (status["state"], status["code"]) == ("starting", "bridge_heartbeat_pending")
    assert not manager.ha.writes("/restart")
    manager.clock.now = start + STARTUP_GRACE
    await restarted.tick()
    assert len(manager.ha.writes("/restart")) == 1 and restarted.record()["repair_count"] == 1


async def test_failed_tick_before_the_app_read_restarts_the_observed_run(manager):
    await healthy(manager)
    manager.clock.now += 3600
    manager = BridgeMaintenance(manager.store, manager.ha, manager.bridge, clock=manager.clock)
    await manager.tick()  # The restarted Controller's started run begins.
    manager.clock.now += STARTUP_GRACE - 10
    manager.ha.fail = ("GET", "/addons")
    assert (await manager.tick())["state"] == "unavailable"
    manager.ha.fail = None
    manager.clock.now += 20
    assert (await manager.tick())["code"] == "bridge_heartbeat_pending"
    manager.clock.now += STARTUP_GRACE
    await manager.tick()
    assert len(manager.ha.writes("/restart")) == 1


async def test_startup_after_a_gap_gets_a_fresh_grace_then_needs_reconcile(manager):
    await healthy(manager)
    manager.clock.now += 3600
    manager.ha.app["state"] = "startup"
    assert (await manager.tick())["code"] == "bridge_app_starting"
    manager.clock.now += STARTUP_GRACE - 1
    assert (await manager.tick())["code"] == "bridge_app_starting"
    manager.clock.now += 1
    status = await manager.tick()
    assert (status["state"], status["code"]) == ("needs_reconcile", "bridge_app_state_unconfirmed")
    assert not manager.ha.writes("/restart")
