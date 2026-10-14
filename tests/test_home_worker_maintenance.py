"""Supervisor contract fixtures only: no real App installation, account or HAOS VM."""
import asyncio
import copy
import json
from pathlib import Path

import pytest
import yaml
from agentic_controller.bridge_maintenance import BridgeRelease
from agentic_controller.ha import HAError, HAUnreachable
from agentic_controller.home_worker_maintenance import (
    APP_START_TIMEOUT,
    DEVICES,
    HOST,
    JOB_TIMEOUT,
    KEY,
    NETWORK,
    OPERATION_TIMEOUT,
    PORT,
    PRIVILEGED,
    SAFE_FLAGS,
    STARTUP_GRACE,
    STOP_CONFIRM_SECONDS,
    HomeWorkerFault,
    HomeWorkerMaintenance,
    HomeWorkerRelease,
)
from agentic_controller.runner import RunnerError

RELEASE = HomeWorkerRelease()
REPO = "aabbccdd"
SLUG = REPO + "_agentic_runner"
JOB = "e" * 32
FINGERPRINT = "ab" * 32
CODE = "Q" * 43
NEW_CODE = "R" * 43


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class FakeRunner:
    def __init__(self, store):
        self.store, self.paired, self.calls = store, [], []
        self.fail, self.consume_fail = None, False
        self.capabilities = {"work_contract_major": 1, "worker_id": "home-coder", "runtime_location": "haos_app",
                             "available": False, "state": "blocked", "providers": [
                                 {"id": "codex", "supported": True, "credential_state": "needs_auth"}]}

    async def pair(self, pairing, principal, controller_id):
        self.paired.append({"pairing": copy.deepcopy(pairing), "principal": principal,
                            "controller_id": controller_id})
        if self.fail:
            error, self.fail = self.fail, None if self.consume_fail else self.fail
            raise error
        self.store.set_setting("runner_haos", {"host": pairing["host"], "port": pairing["port"],
                                                "fingerprint": pairing["fingerprint"], "token": "runner-bearer-secret"})
        return {"paired": True}

    async def call(self, method, path, data=None, pairing=None):
        self.calls.append((method, path))
        if method != "GET" or path != "/coding/capabilities":
            raise AssertionError((method, path))
        return copy.deepcopy(self.capabilities)


class Supervisor:
    def __init__(self):
        self.token, self.revoked, self.repos, self.calls = "fixture-ha-token", False, [], []
        self.timeouts = []
        self.app, self.healthy, self.publish = None, True, True
        self.code, self.before, self.fail = CODE, None, None
        self.catalog_missing, self.corrupt_on_options = False, False
        self.reload_version, self.update_state, self.lose = None, None, None
        self.catalog = {"slug": SLUG, "repository": REPO, "version_latest": RELEASE.version, "available": True,
                        "apparmor": "default", **{key: False for key in SAFE_FLAGS}}
        self.job = {"uuid": JOB, "reference": SLUG, "done": True, "errors": []}
        self.core_state, self.core_info = "running", None  # GET /info; core_info overrides the body.

    async def owner(self, owner):
        return {"id": owner, "is_owner": True, "is_active": True} if owner and not self.revoked else None

    def install_app(self):
        self.app = {**self.catalog, "version": RELEASE.version, "apparmor": "profile",
                    "privileged": list(PRIVILEGED), "devices": list(DEVICES), "network": dict(NETWORK),
                    "options": {}, "state": "stopped", "boot": "auto"}

    async def request(self, method, path, body=None, *, supervisor, timeout):
        expected_timeout = APP_START_TIMEOUT if method == "POST" and path in {
            f"/addons/{SLUG}/start", f"/addons/{SLUG}/restart"} else 30
        assert supervisor is True and timeout == expected_timeout
        self.calls.append((method, path, copy.deepcopy(body)))
        self.timeouts.append((method, path, timeout))
        if self.before:
            await self.before(method, path)
        if self.fail == (method, path):
            raise HAError("fixture transport", 503)
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
            value = {"addons": [{"slug": SLUG}] if self.app else []}
        elif path == "/store/addons":
            listed = {key: self.catalog[key] for key in ("slug", "repository", "version_latest", "available")}
            value = {"addons": [] if self.catalog_missing else [{"slug": "core_ssh"}, listed]}
        elif path == f"/store/addons/{SLUG}/info":
            if self.catalog_missing:
                raise HAError("missing", 404)
            value = dict(self.catalog)
            if self.app and value.get("apparmor") == "default":
                value["apparmor"] = "profile"
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
        elif path in {f"/addons/{SLUG}/start", f"/addons/{SLUG}/restart"}:
            assert self.app["privileged"] == PRIVILEGED and self.app["devices"] == DEVICES
            self.app["state"] = "started"
            if path.endswith("/restart"):
                self.code = NEW_CODE
            if self.publish:
                self.app["options"] = {"pairing_fingerprint": FINGERPRINT, "pairing_code": self.code}
            value = None
        elif path == f"/addons/{SLUG}/options":
            assert set(body) == {"options"} and "hassio_api" not in json.dumps(body)
            self.app["options"] = copy.deepcopy(body["options"])
            if self.corrupt_on_options:
                self.app["privileged"] = ["SYS_ADMIN", "NET_ADMIN"]
            # The real options POST answers without a job. A pending job here covers the defensive wait.
            value = None if self.job.get("done", True) else {"job_id": JOB}
        else:
            raise AssertionError((method, path))
        if self.lose == (method, path):
            raise HAError("response lost after effect", 503)
        return copy.deepcopy(value)

    def posts(self, suffix=""):
        return [call for call in self.calls if call[0] == "POST" and call[1].endswith(suffix)]


@pytest.fixture
def manager(store):
    store.set_setting("owner", "owner")
    store.put("bootstrap_v1", {"id": "setup", "state": "completed", "run_id": "c" * 32})
    clock = Clock()
    ha = Supervisor()
    return HomeWorkerMaintenance(store, ha, FakeRunner(store), "d" * 32, clock=clock)


def secret_blob(manager):
    return json.dumps({"status": manager.status(), "record": manager.record(),
                       "bridge": manager.store.setting("bridge_maintenance"),
                       "host_runner": manager.store.setting("runner")}, default=str)


async def drive(manager, stage):
    for _ in range(16):
        record = manager.record()
        if record and record["stage"] == stage and not record.get("operation"):
            return
        await manager.tick()
    raise AssertionError(manager.status())


async def login_ready(manager):
    manager.runner.capabilities["available"] = False
    manager.runner.capabilities["providers"][0]["credential_state"] = "needs_auth"
    await manager.authorize()
    for _ in range(16):
        if manager.status()["state"] == "login_ready":
            return manager.status()
        await manager.tick()
    raise AssertionError(manager.status())


def test_pin_matches_runner_config_and_bridge_repository():
    config = yaml.safe_load(Path("runner/config.yaml").read_text())
    assert RELEASE.repository == BridgeRelease().repository
    assert config["version"] == RELEASE.version and config["slug"] == RELEASE.app
    assert config["privileged"] == PRIVILEGED and config["devices"] == DEVICES and config["ports"] == NETWORK
    assert config["apparmor"] is True
    assert config["schema"] == {"pairing_fingerprint": "str?", "pairing_code": "str?"}
    for key in ("hassio_api", "homeassistant_api", "docker_api", "full_access", "host_network", "host_pid"):
        assert config[key] is False
    text = Path("controller/agentic_controller/home_worker_maintenance.py").read_text()
    assert "inference-verified" not in text and "coding-auth" not in text and "SUPERVISOR_TOKEN" not in text


@pytest.mark.parametrize("change", [{"hassio_api": True}, {"devices": ["/dev/fuse", "/dev/ttyUSB0"]},
                                    {"privileged": ["NET_ADMIN"]}, {"privileged": ["SYS_ADMIN", "NET_ADMIN"]},
                                    {"apparmor": "disable"}, {"network": {"8443/tcp": None}},
                                    {"full_access": True}, {"host_network": True}])
def test_privilege_pin_rejects_widened_apps(manager, change):
    catalog = dict(manager.ha.catalog)
    catalog.update(change)
    record = {"slug": SLUG, "repository_id": REPO, "managed": False}
    with pytest.raises(HomeWorkerFault, match="home_worker_app_contract_mismatch"):
        manager.validate_app(catalog, record, installed=False)
    manager.ha.install_app()
    manager.ha.app.update(change)
    with pytest.raises(HomeWorkerFault, match="home_worker_app_contract_mismatch"):
        manager.validate_app(manager.ha.app, record, installed=True)


@pytest.mark.parametrize("value", ["default", "profile"])
def test_store_pin_accepts_pre_and_post_install_apparmor(manager, value):
    record = {"slug": SLUG, "repository_id": REPO, "managed": False}
    assert manager.validate_app({**manager.ha.catalog, "apparmor": value}, record, installed=False)["apparmor"] == value


@pytest.mark.parametrize("value", ["disable", None, "", "unconfined", True])
def test_store_pin_rejects_invalid_apparmor(manager, value):
    record = {"slug": SLUG, "repository_id": REPO, "managed": False}
    with pytest.raises(HomeWorkerFault, match="home_worker_app_contract_mismatch"):
        manager.validate_app({**manager.ha.catalog, "apparmor": value}, record, installed=False)


@pytest.mark.parametrize("value", ["default", "disable"])
def test_installed_pin_requires_loaded_apparmor_profile(manager, value):
    record = {"slug": SLUG, "repository_id": REPO, "managed": False}
    manager.ha.install_app()
    manager.ha.app["apparmor"] = value
    with pytest.raises(HomeWorkerFault, match="home_worker_app_contract_mismatch"):
        manager.validate_app(manager.ha.app, record, installed=True)


def test_store_listing_may_omit_port_fields_that_the_install_must_show(manager):
    record = {"slug": SLUG, "repository_id": REPO, "managed": False}
    assert {"privileged", "devices", "network"}.isdisjoint(manager.ha.catalog)
    assert manager.validate_app(manager.ha.catalog, record, installed=False)["apparmor"] == "default"
    listed = {key: value for key, value in manager.ha.catalog.items()
              if key not in {"privileged", "devices", "network", "apparmor"}}
    assert manager.validate_app(listed, record, installed=False)["slug"] == SLUG
    with pytest.raises(HomeWorkerFault, match="home_worker_app_contract_mismatch"):
        manager.validate_app({**listed, "version": RELEASE.version, "options": {}, "state": "stopped"},
                             record, installed=True)


async def test_tick_never_creates_a_record(manager):
    manager.store.put("bootstrap_v1", {"id": "setup", "state": "completed", "bridge_auto_enroll": True,
                                       "run_id": "c" * 32})
    for _ in range(3):
        assert (await manager.tick())["state"] == "not_authorized"
    assert manager.store.setting(KEY) is None and not manager.ha.calls
    calls = list(manager.ha.calls)
    manager.status()
    assert manager.ha.calls == calls


@pytest.mark.parametrize("setup,owner", [("running", "owner"), ("completed", None)])
async def test_authorize_requires_completed_setup_and_live_owner(manager, setup, owner):
    manager.store.put("bootstrap_v1", {"id": "setup", "state": setup})
    manager.store.set_setting("owner", owner)
    with pytest.raises(HomeWorkerFault, match="home_worker_setup_required"):
        await manager.authorize()
    manager.store.put("bootstrap_v1", {"id": "setup", "state": "completed"})
    manager.store.set_setting("owner", "owner")
    manager.ha.revoked = True
    with pytest.raises(HomeWorkerFault, match="home_worker_owner_required"):
        await manager.authorize()
    assert manager.record() is None and not manager.ha.calls


async def test_commit_guard_and_owner_change_prevent_the_record(manager):
    def revoke():
        raise PermissionError("revoked browser")

    with pytest.raises(PermissionError):
        await manager.authorize(commit_guard=revoke)
    assert manager.record() is None

    def replace_owner():
        manager.store.set_setting("owner", "other")

    with pytest.raises(HomeWorkerFault, match="home_worker_owner_changed"):
        await manager.authorize(commit_guard=replace_owner)
    assert manager.record() is None and not manager.ha.calls


@pytest.mark.parametrize("kind", ["paused", "updater", "owner", "unhealthy"])
async def test_fences_block_every_supervisor_post(manager, kind):
    await manager.authorize()
    if kind == "paused":
        manager.store.set_setting("paused", True)
    elif kind == "updater":
        manager.store.set_setting("updater_gate", {"id": "fixture"})
    elif kind == "owner":
        manager.ha.revoked = True
    else:
        manager.ha.healthy = False
    await manager.tick()
    assert not manager.ha.posts()
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    if kind == "unhealthy":
        manager.ha.healthy = True
        await drive(manager, "install")
        manager.ha.healthy = False
        await manager.tick()
        assert not manager.ha.posts("/install")
        manager.ha.healthy = True
        await drive(manager, "start")
        manager.ha.healthy = False
        await manager.tick()
        assert not manager.ha.posts("/start")
        manager.ha.healthy = True
        await drive(manager, "pair")
        manager.runner.fail = RunnerError("expired", 409)
        manager.ha.healthy = False
        await manager.tick()
        assert not manager.ha.posts("/restart") and not manager.ha.posts("/options")


async def test_unpublished_app_is_not_sideloaded(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    manager.ha.catalog_missing = True
    await manager.authorize()
    assert (await manager.tick())["code"] == "home_worker_app_not_published"
    assert not manager.ha.posts("/install")
    assert not any("agentic-runner.zip" in path or path.endswith(".zip") for _, path, _ in manager.ha.calls)


@pytest.mark.parametrize("change", [{"hassio_api": True}, {"devices": ["/dev/fuse", "/dev/net/tun"]},
                                    {"privileged": ["SYS_ADMIN", "NET_ADMIN"]}])
async def test_preexisting_pin_mismatch_is_not_repaired(manager, change):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    manager.ha.install_app()
    manager.ha.app.update(change)
    await manager.authorize()
    for _ in range(6):
        await manager.tick()
        if manager.status()["state"] == "needs_reconcile":
            break
    assert manager.status()["state"] == "needs_reconcile"
    assert not manager.ha.posts("/install") and not manager.ha.posts("/start") and not manager.ha.posts("/options")
    assert manager.ha.app["hassio_api"] is change.get("hassio_api", False)


async def test_preexisting_stopped_app_starts_only_on_owner_repair(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    manager.ha.install_app()
    await manager.authorize()
    for _ in range(6):
        await manager.tick()
        if manager.status()["state"] == "user_disabled":
            break
        manager.clock.now += STOP_CONFIRM_SECONDS
    assert manager.status()["code"] == "home_worker_app_stopped"
    for _ in range(3):
        await manager.tick()
    assert not manager.ha.posts("/start") and not manager.ha.posts("/install")
    await manager.repair()
    for _ in range(16):
        if manager.status()["state"] == "login_ready":
            break
        await manager.tick()
    assert manager.status()["state"] == "login_ready"
    assert len(manager.ha.posts("/start")) == 1 and not manager.ha.posts("/install")
    manager.ha.app["state"] = "stopped"
    for _ in range(3):
        await manager.tick()
        manager.clock.now += STOP_CONFIRM_SECONDS
    assert manager.status()["state"] == "user_disabled" and len(manager.ha.posts("/start")) == 1


async def test_later_stop_or_removal_is_not_reversed_by_the_tick(manager):
    await login_ready(manager)
    starts, installs = len(manager.ha.posts("/start")), len(manager.ha.posts("/install"))
    manager.ha.app["state"] = "stopped"
    assert (await manager.tick())["code"] == "home_worker_observation_pending"
    manager.clock.now += STOP_CONFIRM_SECONDS
    assert (await manager.tick())["state"] == "user_disabled"
    await manager.tick()
    assert len(manager.ha.posts("/start")) == starts
    manager.ha.app = None
    for _ in range(3):
        await manager.tick()
    assert manager.status()["state"] == "user_disabled"
    assert len(manager.ha.posts("/install")) == installs and len(manager.ha.posts("/start")) == starts
    assert not manager.ha.posts("/uninstall")


async def test_owner_repair_reinstalls_a_removed_app_once_and_pairs_the_new_instance(manager):
    await login_ready(manager)
    old_peer = copy.deepcopy(manager.store.setting("runner_haos"))
    manager.ha.app = None
    assert (await manager.tick())["state"] == "user_removed"
    await manager.repair()
    new_fingerprint = "cd" * 32

    async def new_instance(method, path):
        if path == f"/addons/{SLUG}/start" and method == "POST":
            manager.ha.code = NEW_CODE
    manager.ha.before = new_instance
    for _ in range(20):
        status = manager.status()
        if status["state"] == "login_ready":
            break
        if manager.ha.app and manager.ha.app["options"].get("pairing_fingerprint") == FINGERPRINT:
            manager.ha.app["options"]["pairing_fingerprint"] = new_fingerprint
        await manager.tick()
    assert manager.status()["state"] == "login_ready"
    assert len(manager.ha.posts("/install")) == 2 and len(manager.ha.posts("/start")) == 2
    assert manager.runner.paired[-1]["pairing"]["fingerprint"] == new_fingerprint
    assert manager.store.setting("runner_haos")["fingerprint"] == new_fingerprint != old_peer["fingerprint"]
    assert CODE not in secret_blob(manager) and NEW_CODE not in secret_blob(manager)


async def test_repair_is_a_no_op_while_settled(manager):
    status = await login_ready(manager)
    calls = list(manager.ha.calls)
    assert (await manager.repair())["stage"] == status["stage"] == "login"
    assert manager.ha.calls[len(calls):] == [] and not manager.record().get("owner_start")


async def test_pair_uses_fixed_host_and_status_never_contains_the_code(manager):
    store_before = await manager.ha.request("GET", f"/store/addons/{SLUG}/info", supervisor=True, timeout=30)
    assert store_before["apparmor"] == "default" and {"privileged", "devices", "network"}.isdisjoint(store_before)
    manager.ha.calls.clear()
    status = await login_ready(manager)
    store_after = await manager.ha.request("GET", f"/store/addons/{SLUG}/info", supervisor=True, timeout=30)
    assert store_after["apparmor"] == "profile" and {"privileged", "devices", "network"}.isdisjoint(store_after)
    assert len(manager.ha.posts("/install")) == 1 and len(manager.ha.posts("/start")) == 1
    assert len(manager.ha.posts("/options")) == 1
    paired = manager.runner.paired[0]
    assert paired["pairing"] == {"host": HOST, "port": PORT, "fingerprint": FINGERPRINT, "code": CODE}
    assert paired["principal"] == "host-owner" and paired["controller_id"] == "d" * 32
    assert manager.ha.app["options"] == {} and manager.ha.app["apparmor"] == "profile"
    assert manager.ha.app["privileged"] == PRIVILEGED and manager.ha.app["devices"] == DEVICES
    assert manager.ha.app["hassio_api"] is False and manager.ha.app["network"] == NETWORK
    assert manager.store.setting("runner_haos")["fingerprint"] == FINGERPRINT
    assert manager.store.setting("runner_haos")["host"] == HOST
    blob = secret_blob(manager)
    assert CODE not in blob and NEW_CODE not in blob and "runner-bearer-secret" not in blob
    assert status["login"]["show"] is True and status["login"]["worker_id"] == "home-coder"
    assert status["state"] == "login_ready" and status["stage"] == "login"
    assert status["runtime_location"] == "haos_app" and status["credential_state"] == "needs_auth"
    assert status["worker_available"] is False
    assert manager.runner.calls == [("GET", "/coding/capabilities")]
    assert status["limitations"] == [
        "supervisor_trusted_repository_not_atomic_digest_pin",
        "stopped_or_removed_app_returns_only_on_owner_repair",
        "no_heartbeat_restart",
        "hassio_api_is_not_enabled_for_self_options",
        "credential_configured_is_not_promoted_to_inference_verified",
    ]
    assert not any("/auth" in path for _, path in manager.runner.calls)


async def test_options_clear_that_changes_privileges_is_not_accepted(manager):
    manager.ha.corrupt_on_options = True
    await manager.authorize()
    for _ in range(16):
        await manager.tick()
        if manager.status()["state"] in {"needs_reconcile", "user_modified"}:
            break
    assert manager.status()["state"] in {"needs_reconcile", "user_modified"}
    assert manager.ha.app["privileged"] == ["SYS_ADMIN", "NET_ADMIN"]
    assert len(manager.ha.posts("/options")) == 1
    assert CODE not in secret_blob(manager)


async def test_self_options_rejection_does_not_enable_hassio_api(manager):
    manager.ha.publish = False
    await manager.authorize()
    for _ in range(16):
        await manager.tick()
        if manager.record() and manager.record()["stage"] == "pair":
            break
    manager.clock.now += STARTUP_GRACE + 1
    assert (await manager.tick())["code"] == "home_worker_pairing_not_published"
    assert manager.ha.app["hassio_api"] is False
    assert not any("hassio_api" in json.dumps(body) for _, _, body in manager.ha.posts() if body)
    assert not manager.runner.paired


async def test_expired_code_restarts_once_then_needs_reconcile(manager):
    manager.runner.fail = RunnerError("expired", 409)
    await manager.authorize()
    for _ in range(16):
        await manager.tick()
        if manager.status()["state"] == "needs_reconcile":
            break
    assert manager.status()["code"] == "home_worker_pairing_unconfirmed"
    assert len(manager.ha.posts("/restart")) == 1
    assert CODE not in secret_blob(manager) and NEW_CODE not in secret_blob(manager)


async def test_one_restart_then_a_new_code_can_pair(manager):
    manager.runner.fail = RunnerError("expired", 409)
    manager.runner.consume_fail = True
    await login_ready(manager)
    assert [item["pairing"]["code"] for item in manager.runner.paired] == [CODE, NEW_CODE]
    assert len(manager.ha.posts("/restart")) == 1
    assert manager.ha.app["options"] == {} and manager.ha.app["privileged"] == PRIVILEGED
    assert CODE not in secret_blob(manager) and NEW_CODE not in secret_blob(manager)


@pytest.mark.parametrize("barrier", ["worker_lease", "worker_auth_pending"])
async def test_lease_or_auth_barrier_refuses_pair_and_restart(manager, barrier):
    await manager.authorize()
    await drive(manager, "pair")
    manager.store.set_setting(barrier, {"job_id": "a" * 32})
    assert (await manager.tick())["code"] == "home_worker_coding_busy"
    assert not manager.runner.paired and not manager.ha.posts("/restart")
    manager.store.set_setting(barrier, None)
    await login_ready(manager)
    manager.ha.app["state"] = "error"
    assert (await manager.tick())["state"] == "degraded"
    assert manager.status()["restart_allowed"] is True
    manager.store.set_setting(barrier, {"id": "pending"})
    assert manager.status()["restart_allowed"] is False
    with pytest.raises(HomeWorkerFault, match="home_worker_coding_busy"):
        await manager.restart()
    assert not manager.ha.posts("/restart")


async def test_explicit_restart_is_fenced_and_crash_does_not_restart_itself(manager):
    await login_ready(manager)
    manager.ha.app["state"] = "error"
    assert (await manager.tick())["state"] == "degraded"
    await manager.tick()
    assert not manager.ha.posts("/restart")
    manager.store.set_setting("paused", True)
    with pytest.raises(HomeWorkerFault, match="home_worker_paused"):
        await manager.restart()
    manager.store.set_setting("paused", False)
    manager.ha.healthy = False
    with pytest.raises(HomeWorkerFault, match="home_worker_supervisor_not_healthy"):
        await manager.restart()
    assert not manager.ha.posts("/restart")
    manager.ha.healthy = True
    await manager.restart()
    assert len(manager.ha.posts("/restart")) == 1


async def test_configured_is_not_promoted_and_credentials_are_not_read(manager, monkeypatch, tmp_path):
    canaries = {tmp_path / "hermes" / "auth.json": "HERMES-CANARY-SECRET",
                tmp_path / "coding-auth" / "codex.json": "HOST-CANARY-SECRET",
                tmp_path / "home_operator" / "profile.json": "OPERATOR-CANARY-SECRET",
                tmp_path / "inference-verified.json": "INFERENCE-CANARY-SECRET"}
    for path, text in canaries.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    seen = []
    real_open = open

    def spy(file, *args, **kwargs):
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    await login_ready(manager)
    manager.runner.capabilities["available"] = True
    manager.runner.capabilities["providers"][0]["credential_state"] = "configured"
    status = await manager.tick()
    assert status["state"] == "ready" and status["credential_state"] == "configured"
    assert status["runtime_location"] == "haos_app" and status["worker_available"] is True
    assert "inference_verified" not in json.dumps(manager.record())
    assert manager.runner.calls == [("GET", "/coding/capabilities"), ("GET", "/coding/capabilities")]
    joined = "\n".join(seen)
    for needle in ("hermes", "coding-auth", "home_operator", "inference-verified"):
        assert needle not in joined
    blob = secret_blob(manager)
    for text in canaries.values():
        assert text not in blob


async def test_disable_stops_observation_only(manager):
    await login_ready(manager)
    peer = copy.deepcopy(manager.store.setting("runner_haos"))
    calls = list(manager.ha.calls)
    status = await manager.disable()
    assert manager.ha.calls == calls and manager.store.setting("runner_haos") == peer
    assert status["state"] == "user_disabled" and status["code"] == "home_worker_observation_disabled"
    assert not manager.ha.posts("/uninstall")
    await manager.tick()
    assert manager.ha.calls == calls


async def test_second_authorize_does_not_restart_a_disabled_record(manager):
    await login_ready(manager)
    await manager.disable()
    calls = list(manager.ha.calls)
    assert (await manager.authorize())["state"] == "user_disabled"
    assert manager.ha.calls == calls and len([item for item in manager.store.db.execute(
        "SELECT key FROM settings WHERE key=?", (KEY,))]) == 1


def test_routes_are_owner_maintenance_endpoints(store):
    from agentic_controller.app import create_app
    app = create_app(store=store, ha=Supervisor(), host_mode=True)
    paths = {getattr(route, "path", None) for route in app.routes}
    for path in ("/api/host/home-worker/maintenance", "/api/host/home-worker/maintenance/authorize",
                 "/api/host/home-worker/maintenance/repair", "/api/host/home-worker/maintenance/update",
                 "/api/host/home-worker/maintenance/disable", "/api/host/home-worker/maintenance/restart"):
        assert path in paths


async def test_long_local_build_keeps_waiting_while_supervisor_reports_the_job(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    installed = manager.ha.install_app
    manager.ha.install_app = lambda: None
    manager.ha.job = {"uuid": JOB, "reference": SLUG, "done": False, "errors": []}
    await manager.authorize()
    await drive(manager, "install")
    await manager.tick()
    assert manager.record()["operation"]["kind"] == "install"
    for _ in range(int(JOB_TIMEOUT / OPERATION_TIMEOUT) * 2 - 2):
        manager.clock.now += OPERATION_TIMEOUT / 2
        assert (await manager.tick())["state"] == "waiting"
    assert len(manager.ha.posts("/install")) == 1
    manager.ha.job = {"uuid": JOB, "reference": SLUG, "done": True, "errors": []}
    installed()
    for _ in range(8):
        await manager.tick()
    assert manager.status()["state"] == "login_ready" and manager.record()["installed_by_us"] is True
    assert len(manager.ha.posts("/install")) == 1 and len(manager.ha.posts("/start")) == 1


async def test_supervisor_job_running_past_the_cap_needs_reconcile(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    manager.ha.install_app = lambda: None
    manager.ha.job = {"uuid": JOB, "reference": SLUG, "done": False, "errors": []}
    await manager.authorize()
    await drive(manager, "install")
    await manager.tick()
    for _ in range(int(JOB_TIMEOUT / OPERATION_TIMEOUT) * 2 + 2):
        manager.clock.now += OPERATION_TIMEOUT / 2
        await manager.tick()
    assert manager.status()["state"] == "needs_reconcile"
    assert manager.status()["code"] == "home_worker_operation_unconfirmed"
    assert len(manager.ha.posts("/install")) == 1


async def test_unreachable_runner_waits_for_its_listener_before_the_one_restart(manager):
    manager.runner.fail = RunnerError("Runner 無法連線")
    manager.runner.consume_fail = True
    await manager.authorize()
    for _ in range(16):
        if manager.runner.paired:
            break
        await manager.tick()
    assert manager.status()["code"] == "home_worker_pairing_pending" and not manager.ha.posts("/restart")
    status = await manager.tick()
    assert status["state"] == "login_ready" and not manager.ha.posts("/restart")
    assert [item["pairing"]["code"] for item in manager.runner.paired] == [CODE, CODE]


async def test_unreachable_runner_after_the_grace_uses_the_single_restart(manager):
    manager.runner.fail = RunnerError("Runner 無法連線")
    await manager.authorize()
    for _ in range(16):
        if manager.runner.paired:
            break
        await manager.tick()
    manager.clock.now += STARTUP_GRACE + 1
    await manager.tick()
    assert len(manager.ha.posts("/restart")) == 1
    for _ in range(8):
        manager.clock.now += STARTUP_GRACE + 1
        await manager.tick()
    assert manager.status()["state"] == "needs_reconcile" and len(manager.ha.posts("/restart")) == 1


async def test_app_already_paired_by_the_manual_flow_goes_to_login(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    manager.ha.install_app()
    manager.ha.app["state"] = "started"
    manager.store.set_setting("runner_haos", {"host": HOST, "port": PORT, "fingerprint": FINGERPRINT,
                                              "token": "runner-bearer-secret"})
    await manager.authorize()
    await drive(manager, "pair")
    assert not manager.runner.calls
    status = await manager.tick()
    assert status["state"] == "login_ready" and status["stage"] == "login"
    assert status["fingerprint"] == FINGERPRINT and status["login"]["show"] is True
    assert status["runtime_location"] == "haos_app" and status["credential_state"] == "needs_auth"
    assert status["worker_available"] is False
    assert manager.runner.calls == [("GET", "/coding/capabilities")]
    assert not manager.runner.paired and not manager.ha.posts()
    assert "runner-bearer-secret" not in json.dumps(manager.status())


async def test_fresh_enable_populates_capabilities_before_offering_login(manager):
    await manager.authorize()
    await drive(manager, "pair")
    assert not manager.runner.paired and not manager.runner.calls
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == (
        "login_ready", "login", "home_worker_login_required")
    assert status["runtime_location"] == "haos_app"
    assert status["credential_state"] == "needs_auth"
    assert status["worker_available"] is False and status["login"]["show"] is True
    assert manager.runner.calls == [("GET", "/coding/capabilities")]


@pytest.mark.parametrize("error", [RunnerError("Runner 無法連線"), OSError("connect refused")])
async def test_capability_poll_failure_on_pair_tick_withholds_login_until_reachable(manager, error):
    await manager.authorize()
    await drive(manager, "pair")
    real_call = manager.runner.call

    async def down(method, path, data=None, pairing=None):
        manager.runner.calls.append((method, path))
        raise error

    manager.runner.call = down
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == (
        "unavailable", "login", "home_worker_worker_unavailable")
    assert status["worker_available"] is False and status["login"]["show"] is False
    assert len(manager.runner.paired) == 1 and len(manager.ha.posts("/options")) == 1
    assert manager.runner.calls == [("GET", "/coding/capabilities")]
    posts = len(manager.ha.posts())
    manager.runner.call = real_call
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == (
        "login_ready", "login", "home_worker_login_required")
    assert status["runtime_location"] == "haos_app" and status["credential_state"] == "needs_auth"
    assert status["worker_available"] is False and status["login"]["show"] is True
    assert len(manager.runner.paired) == 1 and len(manager.ha.posts("/options")) == 1
    assert len(manager.ha.posts()) == posts
    assert manager.runner.calls == [("GET", "/coding/capabilities"), ("GET", "/coding/capabilities")]


async def test_already_signed_in_worker_goes_straight_to_ready_after_clear(manager):
    manager.runner.capabilities["available"] = True
    manager.runner.capabilities["providers"][0]["credential_state"] = "configured"
    await manager.authorize()
    await drive(manager, "pair")
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == ("ready", "monitor", None)
    assert status["runtime_location"] == "haos_app" and status["credential_state"] == "configured"
    assert status["worker_available"] is True and status["login"]["show"] is False
    assert manager.runner.calls == [("GET", "/coding/capabilities")]


async def test_already_paired_pending_clear_stays_in_pair_until_job_finishes(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    manager.ha.install_app()
    manager.ha.app["state"] = "started"
    manager.ha.app["options"] = {"pairing_fingerprint": FINGERPRINT, "pairing_code": CODE}
    manager.store.set_setting("runner_haos", {"host": HOST, "port": PORT, "fingerprint": FINGERPRINT,
                                              "token": "runner-bearer-secret"})
    await manager.authorize()
    await drive(manager, "pair")
    manager.ha.job = {"uuid": JOB, "reference": SLUG, "done": False, "errors": []}
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == (
        "waiting", "pair", "home_worker_observation_pending")
    assert status["operation"]["kind"] == "clear_options" and status["login"]["show"] is False
    assert not manager.runner.paired and not manager.runner.calls
    status = await manager.tick()
    assert status["stage"] == "pair" and status["operation"]["kind"] == "clear_options"
    assert not manager.runner.calls and len(manager.ha.posts("/options")) == 1
    manager.ha.job["done"] = True
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == (
        "login_ready", "login", "home_worker_login_required")
    assert "operation" not in status and status["login"]["show"] is True
    assert status["runtime_location"] == "haos_app" and status["credential_state"] == "needs_auth"
    assert status["worker_available"] is False
    assert not manager.runner.paired and len(manager.ha.posts("/options")) == 1
    assert manager.runner.calls == [("GET", "/coding/capabilities")]


async def test_signed_in_but_blocked_runtime_does_not_ask_for_login(manager):
    await login_ready(manager)
    manager.runner.capabilities["available"] = False
    manager.runner.capabilities["providers"][0]["credential_state"] = "configured"
    status = await manager.tick()
    assert status["state"] == "unavailable" and status["code"] == "home_worker_runtime_blocked"
    assert status["login"]["show"] is False
    manager.runner.capabilities["available"] = True
    assert (await manager.tick())["state"] == "ready"


async def test_unreachable_runner_after_pairing_is_not_offered_a_login(manager):
    await login_ready(manager)

    async def down(method, path, data=None, pairing=None):
        raise RunnerError("Runner 無法連線")
    manager.runner.call = down
    status = await manager.tick()
    assert status["state"] == "unavailable" and status["code"] == "home_worker_worker_unavailable"
    assert status["login"]["show"] is False


async def test_app_that_lost_its_pairing_is_paired_again(manager):
    await login_ready(manager)
    manager.ha.app["options"] = {"pairing_fingerprint": FINGERPRINT, "pairing_code": NEW_CODE}
    manager.store.set_setting("runner_haos", None)
    for _ in range(8):
        await manager.tick()
        if manager.status()["state"] == "login_ready":
            break
    assert manager.status()["state"] == "login_ready" and manager.ha.app["options"] == {}
    assert manager.runner.paired[-1]["pairing"]["code"] == NEW_CODE
    assert NEW_CODE not in secret_blob(manager)


async def test_failed_build_can_be_retried_by_the_owner_at_once(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    installed = manager.ha.install_app
    manager.ha.install_app = lambda: None
    manager.ha.job = {"uuid": JOB, "reference": SLUG, "done": True, "errors": [{"message": "build failed"}]}
    await manager.authorize()
    for _ in range(8):
        await manager.tick()
        if manager.status()["state"] == "needs_reconcile":
            break
    assert manager.status()["code"] == "home_worker_supervisor_job_failed"
    manager.ha.job = {"uuid": JOB, "reference": SLUG, "done": True, "errors": []}
    manager.ha.install_app = installed
    await manager.repair()
    for _ in range(12):
        if manager.status()["state"] == "login_ready":
            break
        await manager.tick()
    assert manager.status()["state"] == "login_ready" and len(manager.ha.posts("/install")) == 2


async def test_installed_app_with_default_apparmor_is_never_started(manager):
    installed = manager.ha.install_app

    def install_without_profile():
        installed()
        manager.ha.app["apparmor"] = "default"

    manager.ha.install_app = install_without_profile
    await manager.authorize()
    for _ in range(10):
        await manager.tick()
        if manager.status()["state"] in {"needs_reconcile", "user_modified"}:
            break
    status = manager.status()
    assert status["state"] in {"needs_reconcile", "user_modified"}
    assert status["code"] == "home_worker_app_contract_mismatch"
    assert len(manager.ha.posts("/install")) == 1
    assert not manager.ha.posts("/start")


# Supervisor transitions. Fixture core states only: no real HAOS boot, backup or Controller restart.

@pytest.mark.parametrize("steps", [
    [("setup", "unknown"), ("startup", "stopped"), ("startup", "stopped"), ("startup", "startup"),
     ("startup", "error"), ("running", "started")],
    [("freeze", "stopped"), ("freeze", "stopped"), ("freeze", "startup"), ("running", "started")],
], ids=["haos_reboot", "cold_backup"])
async def test_haos_reboot_or_cold_backup_keeps_the_home_worker_paired(manager, steps):
    await login_ready(manager)
    sent, pairs, fingerprint = len(manager.ha.posts()), len(manager.runner.paired), manager.record()["fingerprint"]
    for core, app_state in steps[:-1]:
        manager.ha.core_state, manager.ha.app["state"] = core, app_state
        manager.clock.now += 60
        status = await manager.tick()
        assert (status["state"], status["code"]) == ("waiting", "home_worker_supervisor_busy")
        assert status["enabled"] is True and manager.record()["stopped_since"] is None
    manager.ha.core_state, manager.ha.app["state"] = steps[-1]
    manager.clock.now += 60
    status = await manager.tick()
    assert status["state"] == "login_ready" and status["fingerprint"] == fingerprint
    assert len(manager.ha.posts()) == sent and len(manager.runner.paired) == pairs


@pytest.mark.parametrize("stage", ["install", "start", "pair"])
async def test_supervisor_busy_defers_every_stopped_path(manager, stage):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    if stage == "install":
        manager.ha.install_app()  # A pre-existing stopped App reaches adopt_existing.
        await manager.authorize()
        await drive(manager, "install")
    else:
        await manager.authorize()
        await drive(manager, "start")
        if stage == "pair":
            manager.ha.publish = False
            await drive(manager, "pair")
        manager.ha.app["state"] = "stopped"
        record = manager.record()
        record.update(started_once=True, owner_start=False)  # This record may not start the App again.
        manager.save(record)
    sent = len(manager.ha.posts())
    manager.ha.core_state = "startup"
    for _ in range(3):
        manager.clock.now += STOP_CONFIRM_SECONDS
        assert (await manager.tick())["code"] == "home_worker_supervisor_busy"
    assert manager.record()["enabled"] is True and len(manager.ha.posts()) == sent
    manager.ha.core_state = "running"
    assert (await manager.tick())["code"] == "home_worker_observation_pending"
    manager.clock.now += STOP_CONFIRM_SECONDS
    status = await manager.tick()
    assert (status["state"], status["code"]) == ("user_disabled", "home_worker_app_stopped")
    assert len(manager.ha.posts()) == sent


async def test_real_stop_is_confirmed_after_the_window_and_survives_controller_restart(manager):
    await login_ready(manager)
    sent = len(manager.ha.posts())
    manager.ha.app["state"] = "stopped"
    status = await manager.tick()
    assert (status["state"], status["code"]) == ("waiting", "home_worker_observation_pending")
    assert status["enabled"] is True and manager.record()["stopped_since"] == manager.clock()
    # Any other observation resets the timer.
    manager.clock.now += STOP_CONFIRM_SECONDS
    manager.ha.app["state"] = "started"
    assert (await manager.tick())["state"] == "login_ready" and manager.record()["stopped_since"] is None
    manager.ha.app["state"] = "stopped"
    await manager.tick()
    first = manager.clock()
    manager.clock.now += STOP_CONFIRM_SECONDS - 1
    restarted = HomeWorkerMaintenance(manager.store, manager.ha, manager.runner, "d" * 32, clock=manager.clock)
    assert (await restarted.tick())["code"] == "home_worker_observation_pending"
    assert restarted.record()["stopped_since"] == first and restarted.record()["enabled"] is True
    manager.clock.now += 1
    status = await restarted.tick()
    assert (status["state"], status["code"]) == ("user_disabled", "home_worker_app_stopped")
    assert status["enabled"] is False and len(manager.ha.posts()) == sent


async def test_supervisor_transition_clears_a_pending_stop(manager):
    await login_ready(manager)
    manager.ha.app["state"] = "stopped"
    await manager.tick()
    manager.clock.now += STOP_CONFIRM_SECONDS
    manager.ha.core_state = "freeze"
    assert (await manager.tick())["code"] == "home_worker_supervisor_busy"
    assert manager.record()["stopped_since"] is None and manager.record()["enabled"] is True
    manager.ha.core_state = "running"
    assert (await manager.tick())["code"] == "home_worker_observation_pending"
    manager.clock.now += STOP_CONFIRM_SECONDS
    assert (await manager.tick())["state"] == "user_disabled"


async def test_crash_is_degraded_only_while_supervisor_is_running(manager):
    await login_ready(manager)
    manager.ha.app["state"] = "error"
    manager.ha.core_state = "shutdown"
    assert (await manager.tick())["code"] == "home_worker_supervisor_busy"
    manager.ha.core_state = "running"
    assert (await manager.tick())["state"] == "degraded"
    manager.ha.core_state = "freeze"
    with pytest.raises(HomeWorkerFault, match="home_worker_supervisor_busy"):
        await manager.restart()
    assert not manager.ha.posts("/restart")


@pytest.mark.parametrize("kind,path,body", [
    ("add_repository", "/store/repositories", {"repository": RELEASE.repository}),
    ("install", f"/store/addons/{SLUG}/install", {"background": True}),
    ("start", f"/addons/{SLUG}/start", None),
    ("restart", f"/addons/{SLUG}/restart", None),
    ("clear_options", f"/addons/{SLUG}/options", {"options": {}}),
    ("update", f"/store/addons/{SLUG}/update", {"backup": False, "background": True})])
@pytest.mark.parametrize("core", ["startup", "freeze"])
async def test_no_effect_is_posted_unless_supervisor_is_running(manager, kind, path, body, core):
    await login_ready(manager)
    sent, before = len(manager.ha.posts()), copy.deepcopy(manager.record())
    manager.ha.core_state = core
    with pytest.raises(HomeWorkerFault) as raised:
        await manager.effect(manager.record(), manager.ha.token, kind, path, body)
    assert (raised.value.code, raised.value.state) == ("home_worker_supervisor_busy", "waiting")
    record = manager.record()
    assert len(manager.ha.posts()) == sent and record["operation"] == before["operation"] is None
    for key in ("pair_restarts", "start_sent", "owner_start", "pair_deadline"):
        assert record.get(key) == before.get(key)


async def test_setup_effects_wait_for_supervisor_running(manager):
    await manager.authorize()
    manager.ha.core_state = "startup"
    assert (await manager.tick())["code"] == "home_worker_supervisor_busy"
    assert not manager.ha.posts() and manager.record()["operation"] is None
    manager.ha.core_state = "running"
    await login_ready(manager)
    assert len(manager.ha.posts("/repositories")) == 1 and len(manager.ha.posts("/start")) == 1


@pytest.mark.parametrize("fault", ["transport", [], {}, {"state": None}, {"state": 1}, "running"])
async def test_unreadable_supervisor_state_is_transient(manager, fault):
    await login_ready(manager)
    sent = len(manager.ha.posts())
    manager.ha.app["state"] = "stopped"
    if fault == "transport":
        manager.ha.fail = ("GET", "/info")
    else:
        manager.ha.core_info = fault
    status = await manager.tick()
    assert (status["state"], status["code"]) == ("unavailable", "home_worker_observation_unavailable")
    assert status["enabled"] is True
    manager.ha.fail = manager.ha.core_info = None
    manager.ha.app["state"] = "started"
    assert (await manager.tick())["state"] == "login_ready"
    assert len(manager.ha.posts()) == sent


async def test_controller_start_runs_the_observation_loop(manager):
    # Controller startup calls start(); in-memory observation state must never shadow the loop method.
    await manager.start()
    try:
        await asyncio.sleep(0.05)
        assert manager.task is not None and not manager.task.done()
    finally:
        await manager.close()
    assert manager.task is None


async def test_fresh_enable_and_pairing_restart_wait_for_supervisor_app_start_timeout(manager):
    assert APP_START_TIMEOUT == 150
    await manager.authorize()
    await drive(manager, "start")

    async def slow_start(method, path):
        if method == "POST" and path == f"/addons/{SLUG}/start":
            assert manager.ha.timeouts[-1] == ("POST", f"/addons/{SLUG}/start", 150)
            manager.clock.now += 45

    manager.ha.before = slow_start
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == ("running", "start", None)
    assert status["operation"]["kind"] == "start" and status["operation"]["state"] == "acknowledged"
    manager.ha.before = None

    # Next tick sees the App started and advances to pair without ever entering unavailable.
    status = await manager.tick()
    assert (status["state"], status["stage"], status["code"]) == ("running", "pair", None)
    assert "operation" not in status

    # An expired pairing code triggers the single pairing restart with timeout 150.
    manager.runner.fail = RunnerError("expired", 409)
    manager.runner.consume_fail = True
    status = await manager.tick()
    assert status["stage"] == "pair" and status["operation"]["kind"] == "restart"
    status = await login_ready(manager)
    assert status["state"] == "login_ready"

    start_calls = [item for item in manager.ha.timeouts if item[1] in {
        f"/addons/{SLUG}/start", f"/addons/{SLUG}/restart"}]
    other_calls = [item for item in manager.ha.timeouts if item[1] not in {
        f"/addons/{SLUG}/start", f"/addons/{SLUG}/restart"}]
    assert start_calls == [("POST", f"/addons/{SLUG}/start", 150), ("POST", f"/addons/{SLUG}/restart", 150)]
    assert other_calls and all(timeout == 30 for _, _, timeout in other_calls)
    other_paths = {(method, path) for method, path, _ in other_calls}
    for expected in (
        ("GET", "/store/repositories"),
        ("POST", "/store/repositories"),
        ("GET", "/store/addons"),
        ("GET", f"/store/addons/{SLUG}/info"),
        ("GET", f"/store/addons/{SLUG}/availability"),
        ("POST", f"/store/addons/{SLUG}/install"),
        ("GET", "/addons"),
        ("GET", f"/addons/{SLUG}/info"),
        ("GET", "/supervisor/info"),
        ("GET", "/info"),
        ("GET", f"/jobs/{JOB}"),
        ("POST", f"/addons/{SLUG}/options"),
    ):
        assert expected in other_paths


@pytest.mark.parametrize("error,expired", [(HAUnreachable("fixture"), False), (HAError("fixture", 401), False),
                                          (HAUnreachable("fixture"), True)])
async def test_home_outage_preserves_intent_and_recovers(manager, monkeypatch, error, expired):
    await login_ready(manager)
    manager.save(manager.record(), stopped_since=manager.clock())
    if expired:
        manager.save(manager.record(), operation={"kind": "start", "deadline": manager.clock() - 1})
    before = copy.deepcopy(manager.record())
    calls = copy.deepcopy(manager.ha.calls)
    owner = manager.ha.owner

    async def fail(*args):
        raise error

    monkeypatch.setattr(manager.ha, "owner", fail)
    result = await manager.tick()
    state = "needs_reconcile" if expired else "waiting" if isinstance(error, HAUnreachable) else "unavailable"
    code = "home_worker_" + ("operation_unconfirmed" if expired else
                          "home_unreachable" if isinstance(error, HAUnreachable) else "observation_unavailable")
    assert result["state"] == state and result["code"] == code
    after = manager.record()
    changed = {"state", "code", "updated_at"}
    if isinstance(error, HAUnreachable) and not expired:
        changed.add("stopped_since")
        assert after["stopped_since"] is None
    assert {k: v for k, v in before.items() if k not in changed} == {k: v for k, v in after.items() if k not in changed}
    assert manager.ha.calls == calls
    if not expired:
        monkeypatch.setattr(manager.ha, "owner", owner)
        assert (await manager.tick())["state"] == "login_ready"


@pytest.mark.parametrize("path", ["/info", f"/addons/{SLUG}/info"])
async def test_mid_observation_home_outage_waits(manager, monkeypatch, path):
    await login_ready(manager)
    if path == "/info":
        manager.ha.app["state"] = "stopped"
    request = manager.ha.request

    async def fail(method, target, *args, **kwargs):
        if target == path:
            raise HAUnreachable("fixture")
        return await request(method, target, *args, **kwargs)

    monkeypatch.setattr(manager.ha, "request", fail)
    assert (await manager.tick())["code"] == "home_worker_home_unreachable"


async def test_home_outage_requires_new_stop_confirmation(manager, monkeypatch):
    await login_ready(manager)
    manager.ha.app["state"] = "stopped"
    await manager.tick()
    manager.clock.now += STOP_CONFIRM_SECONDS
    owner = manager.ha.owner

    async def fail(*args):
        raise HAUnreachable("fixture")

    monkeypatch.setattr(manager.ha, "owner", fail)
    await manager.tick()
    monkeypatch.setattr(manager.ha, "owner", owner)
    assert (await manager.tick())["state"] == "waiting"
    manager.clock.now += STOP_CONFIRM_SECONDS - 1
    assert (await manager.tick())["state"] == "waiting"
    manager.clock.now += 1
    assert (await manager.tick())["state"] == "user_disabled"


async def test_repair_route_home_outage_is_calm_and_preserves_record(manager, monkeypatch, tmp_path):
    import httpx
    from agentic_controller.app import create_app

    await login_ready(manager)
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    monkeypatch.setenv("AGENTIC_LINE_CONFIG", str(tmp_path / "line-config/gateway.json"))
    (tmp_path / "access-key").write_text("fixture-machine-access-key")
    app = create_app(store=manager.store, ha=manager.ha, host_mode=True)
    before = copy.deepcopy(manager.record())

    async def fail(*args):
        raise HAUnreachable("fixture")

    monkeypatch.setattr(manager.ha, "owner", fail)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                 headers={"Origin": "http://localhost:8099"}) as client:
        assert (await client.post("/api/host/login", json={"key": "fixture-machine-access-key"})).status_code == 200
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        response = await client.post("/api/host/home-worker/maintenance/repair", json={})
    assert response.status_code == 409 and response.json()["detail"] == "home_worker_home_unreachable"
    assert manager.record() == before
    if getattr(app.state, "gateway", None):
        await app.state.gateway.close()
