"""Supervisor contract fixtures only: no real App installation, account or HAOS VM."""
import copy
import json
from pathlib import Path

import pytest
import yaml
from agentic_controller.bridge_maintenance import BridgeRelease
from agentic_controller.ha import HAError
from agentic_controller.home_worker_maintenance import (
    DEVICES,
    HOST,
    KEY,
    NETWORK,
    PORT,
    PRIVILEGED,
    SAFE_FLAGS,
    STARTUP_GRACE,
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
        self.app, self.healthy, self.publish = None, True, True
        self.code, self.before, self.fail = CODE, None, None
        self.catalog_missing, self.corrupt_on_options = False, False
        self.catalog = {"slug": SLUG, "repository": REPO, "version_latest": RELEASE.version, "available": True,
                        "apparmor": "profile", "privileged": list(PRIVILEGED), "devices": list(DEVICES),
                        "network": dict(NETWORK), **{key: False for key in SAFE_FLAGS}}
        self.job = {"uuid": JOB, "reference": SLUG, "done": True, "errors": []}

    async def owner(self, owner):
        return {"id": owner, "is_owner": True, "is_active": True} if owner and not self.revoked else None

    def install_app(self):
        self.app = {**self.catalog, "version": RELEASE.version, "options": {}, "state": "stopped", "boot": "auto"}

    async def request(self, method, path, body=None, *, supervisor, timeout):
        assert supervisor is True and timeout == 30
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.before:
            await self.before(method, path)
        if self.fail == (method, path):
            raise HAError("fixture transport", 503)
        if path == "/supervisor/info":
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
        elif path == f"/store/addons/{SLUG}/info":
            if self.catalog_missing:
                raise HAError("missing", 404)
            value = self.catalog
        elif path == f"/store/addons/{SLUG}/availability":
            value = None
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
            value = None
        else:
            raise AssertionError((method, path))
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
                                    {"apparmor": "default"}, {"network": {"8443/tcp": None}},
                                    {"full_access": True}, {"host_network": True}])
def test_privilege_pin_rejects_widened_apps(manager, change):
    catalog = dict(manager.ha.catalog)
    catalog.update(change)
    record = {"slug": SLUG, "repository_id": REPO, "managed": False}
    with pytest.raises(HomeWorkerFault, match="home_worker_app_contract_mismatch"):
        manager.validate_app(catalog, record, installed=False)


def test_store_listing_may_omit_port_fields_that_the_install_must_show(manager):
    record = {"slug": SLUG, "repository_id": REPO, "managed": False}
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


async def test_preexisting_stopped_app_is_not_started(manager):
    manager.ha.repos = [{"source": RELEASE.repository, "slug": REPO}]
    manager.ha.install_app()
    await manager.authorize()
    for _ in range(6):
        await manager.tick()
        if manager.status()["state"] == "user_disabled":
            break
    assert manager.status()["code"] == "home_worker_app_stopped"
    assert not manager.ha.posts("/start") and not manager.ha.posts("/install")
    await manager.repair()
    await manager.tick()
    assert manager.status()["state"] == "user_disabled" and not manager.ha.posts("/start")


async def test_later_stop_or_removal_is_not_reversed(manager):
    await login_ready(manager)
    starts, installs = len(manager.ha.posts("/start")), len(manager.ha.posts("/install"))
    manager.ha.app["state"] = "stopped"
    assert (await manager.tick())["state"] == "user_disabled"
    await manager.tick()
    assert len(manager.ha.posts("/start")) == starts
    manager.ha.app = None
    await manager.repair()
    assert (await manager.tick())["state"] == "user_removed"
    await manager.tick()
    assert len(manager.ha.posts("/install")) == installs
    assert not manager.ha.posts("/uninstall")


async def test_pair_uses_fixed_host_and_status_never_contains_the_code(manager):
    status = await login_ready(manager)
    paired = manager.runner.paired[0]
    assert paired["pairing"] == {"host": HOST, "port": PORT, "fingerprint": FINGERPRINT, "code": CODE}
    assert paired["principal"] == "host-owner" and paired["controller_id"] == "d" * 32
    assert manager.ha.app["options"] == {}
    assert manager.ha.app["privileged"] == PRIVILEGED and manager.ha.app["devices"] == DEVICES
    assert manager.ha.app["hassio_api"] is False and manager.ha.app["network"] == NETWORK
    assert manager.store.setting("runner_haos")["fingerprint"] == FINGERPRINT
    assert manager.store.setting("runner_haos")["host"] == HOST
    blob = secret_blob(manager)
    assert CODE not in blob and NEW_CODE not in blob and "runner-bearer-secret" not in blob
    assert status["login"]["show"] is True and status["login"]["worker_id"] == "home-coder"
    assert status["state"] == "login_ready" and status["stage"] == "login"
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
    assert (await manager.tick())["code"] == "home_worker_self_options_rejected"
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
    assert manager.runner.calls == [("GET", "/coding/capabilities")]
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
                 "/api/host/home-worker/maintenance/repair", "/api/host/home-worker/maintenance/disable",
                 "/api/host/home-worker/maintenance/restart"):
        assert path in paths
