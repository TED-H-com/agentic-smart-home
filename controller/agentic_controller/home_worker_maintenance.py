"""Owner-started install, pair and login preparation for the Home coding worker.

The tick never creates a record and never posts to Supervisor unless this record
is already authorized. Pairing codes stay in Supervisor options only long enough
to call the existing runner pairing path; they are not stored, logged or returned.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import asdict, dataclass

from .bridge_maintenance import BridgeRelease
from .ha import HAError
from .models import utcnow
from .runner import RunnerError

KEY = "home_worker_maintenance"
HOST, PORT = "192.168.177.2", 8443
STARTUP_GRACE = 120
OPERATION_TIMEOUT = 600
SAFE_FLAGS = ("hassio_api", "homeassistant_api", "auth_api", "docker_api", "full_access",
              "host_network", "host_pid")
PRIVILEGED = ["SYS_ADMIN"]
DEVICES = ["/dev/fuse"]
NETWORK = {"8443/tcp": 8443}
APPARMOR = "profile"  # runner/config.yaml apparmor: true plus runner/apparmor.txt
TERMINAL = {"user_disabled", "user_removed", "user_modified", "needs_reconcile"}
APP_STATES = {"started", "stopped", "startup", "error", "unknown"}
CREDENTIALS = {"configured", "inference_verified", "needs_auth", "needs_reauth", "unavailable", "connected"}
USER_STOP = {"user_disabled", "user_removed", "user_modified"}


@dataclass(frozen=True)
class HomeWorkerRelease:
    """Same trusted repository as the sidebar App. The runner contract is fixed here."""

    repository: str = BridgeRelease().repository
    app: str = "agentic_runner"
    version: str = "0.8.0"


class HomeWorkerFault(Exception):
    def __init__(self, code, state="unavailable"):
        self.code, self.state = code, state
        super().__init__(code)


class HomeWorkerMaintenance:
    def __init__(self, store, ha, runner, controller_id, *, release=None, clock=time.time):
        self.store, self.ha, self.runner = store, ha, runner
        self.controller_id = controller_id
        self.release, self.clock = release or HomeWorkerRelease(), clock
        self.lock, self.task = asyncio.Lock(), None

    def record(self):
        return self.store.setting(KEY)

    def save(self, record, **changes):
        record.update(changes, updated_at=utcnow())
        self.store.set_setting(KEY, record)

    def status(self):
        record = self.record()
        if not record:
            result = {"contract": 1, "state": "not_authorized", "stage": None, "code": None,
                      "enabled": False, "managed": False, "release": asdict(self.release),
                      "credential_state": None, "worker_available": None, "runtime_location": None}
        else:
            keys = ("state", "stage", "code", "enabled", "managed", "release", "slug", "observed_at",
                    "updated_at", "app_state", "version", "fingerprint", "credential_state",
                    "worker_available", "runtime_location", "pair_restarts", "installed_by_us")
            result = {"contract": 1, **{key: record.get(key) for key in keys}}
            operation = record.get("operation")
            if operation:
                result["operation"] = {key: operation.get(key) for key in ("kind", "state", "job_id", "deadline")}
        result["login"] = {"worker_id": "home-coder", "provider": "codex", "runtime_location": "haos_app",
                           "show": result.get("state") == "login_ready",
                           "auth_path": "/api/workers/home-coder/providers/codex/auth",
                           "auth_poll_path": "/api/workers/home-coder/auth/{id}"}
        result["restart_allowed"] = result.get("state") == "degraded" and not self.busy()
        result["limitations"] = ["supervisor_trusted_repository_not_atomic_digest_pin",
                                  "stopped_or_removed_app_is_not_reinstalled",
                                  "no_heartbeat_restart",
                                  "hassio_api_is_not_enabled_for_self_options",
                                  "self_options_with_hassio_api_false_not_verified_on_haos_vm",
                                  "credential_configured_is_not_promoted_to_inference_verified"]
        return result

    def busy(self):
        return bool(self.store.setting("worker_lease") or self.store.setting("worker_auth_pending"))

    def effect_gate(self):
        if self.store.setting("paused", False):
            raise HomeWorkerFault("home_worker_paused", "unavailable")
        if self.store.setting("updater_gate"):
            raise HomeWorkerFault("home_worker_update_in_progress", "unavailable")

    def guard(self, record, token):
        self.effect_gate()
        current = self.record()
        if (not current or current["id"] != record["id"] or not current["enabled"] or
                current["owner"] != self.store.setting("owner") or token != self.ha.token):
            raise HomeWorkerFault("home_worker_authorization_changed", "needs_reconcile")
        if current["release"] != asdict(self.release):
            raise HomeWorkerFault("home_worker_release_changed", "needs_reconcile")

    async def owner(self, record, token):
        self.guard(record, token)
        active = await self.ha.owner(record["owner"])
        self.guard(record, token)
        if not active:
            raise HomeWorkerFault("home_worker_owner_required", "needs_reconcile")

    async def request(self, record, token, method, path, body=None):
        self.guard(record, token)
        result = await self.ha.request(method, path, body, supervisor=True, timeout=30)
        self.guard(record, token)
        return result

    async def authorize(self, *, commit_guard=None):
        """Explicit owner start. A second call does not undo a stop, removal or edit."""
        async with self.lock:
            self.effect_gate()
            owner, token = self.store.setting("owner"), self.ha.token
            setup = self.store.get("bootstrap_v1", "setup") or {}
            if setup.get("state") != "completed" or not owner:
                raise HomeWorkerFault("home_worker_setup_required")
            if not await self.ha.owner(owner):
                raise HomeWorkerFault("home_worker_owner_required")
            if commit_guard:
                commit_guard()
            if owner != self.store.setting("owner") or token != self.ha.token:
                raise HomeWorkerFault("home_worker_owner_changed")
            self.effect_gate()
            previous = self.record()
            if previous:
                if previous["owner"] != owner or previous["release"] != asdict(self.release):
                    raise HomeWorkerFault("home_worker_authorization_changed", "needs_reconcile")
                return self.status()
            record = {"id": uuid.uuid4().hex, "owner": owner, "enabled": True, "managed": False,
                      "state": "running", "stage": "repository", "code": None, "release": asdict(self.release),
                      "operation": None, "pair_restarts": 0, "start_sent": False, "installed_by_us": False,
                      "setup_run_id": setup.get("run_id"), "fingerprint": None, "credential_state": None}
            self.save(record)
            return self.status()

    async def repair(self, *, commit_guard=None):
        """Resume observation of the existing record. Does not post, reinstall or start."""
        async with self.lock:
            record = self.record()
            if not record:
                raise HomeWorkerFault("home_worker_setup_required")
            token = self.ha.token
            if not await self.ha.owner(record["owner"]):
                raise HomeWorkerFault("home_worker_owner_required", "needs_reconcile")
            if commit_guard:
                commit_guard()
            if record["owner"] != self.store.setting("owner") or token != self.ha.token:
                raise HomeWorkerFault("home_worker_authorization_changed", "needs_reconcile")
            self.effect_gate()
            self.save(record, enabled=True, state="running", code=None)
            return self.status()

    async def disable(self, *, commit_guard=None):
        """Stop observing. Never uninstalls, unpairs or deletes App coding credentials."""
        async with self.lock:
            record = self.record()
            if not record:
                raise HomeWorkerFault("home_worker_setup_required")
            token = self.ha.token
            if not await self.ha.owner(record["owner"]):
                raise HomeWorkerFault("home_worker_owner_required")
            if commit_guard:
                commit_guard()
            if record["owner"] != self.store.setting("owner") or token != self.ha.token:
                raise HomeWorkerFault("home_worker_authorization_changed", "needs_reconcile")
            self.save(record, enabled=False, state="user_disabled", code="home_worker_observation_disabled")
            return self.status()

    async def restart(self, *, commit_guard=None):
        """Owner restart of a crashed App. Refused while a coding lease or auth barrier exists."""
        async with self.lock:
            record = self.record()
            if not record:
                raise HomeWorkerFault("home_worker_setup_required")
            if record["state"] != "degraded":
                raise HomeWorkerFault("home_worker_restart_not_allowed")
            if self.busy():
                raise HomeWorkerFault("home_worker_coding_busy")
            token = self.ha.token
            await self.owner(record, token)
            if commit_guard:
                commit_guard()
            if self.busy():
                raise HomeWorkerFault("home_worker_coding_busy")
            await self.effect(record, token, "restart", f"/addons/{record['slug']}/restart")
            return self.status()

    async def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(15)

    def pinned(self, data, *, installed):
        # Store listings can omit port and device fields. A field that is present must already match.
        # The installed readback is the adoption pin and must contain every field.
        exact = (("privileged", PRIVILEGED), ("devices", DEVICES), ("network", NETWORK), ("apparmor", APPARMOR))
        return (isinstance(data, dict) and all(data.get(key) is False for key in SAFE_FLAGS) and
                all(data.get(key) == expected for key, expected in exact if installed or key in data))

    @staticmethod
    def privilege_view(app):
        return {"privileged": app.get("privileged"), "devices": app.get("devices"), "apparmor": app.get("apparmor"),
                "network": app.get("network"), **{key: app.get(key) for key in SAFE_FLAGS}}

    def validate_app(self, data, record, *, installed):
        version_key = "version" if installed else "version_latest"
        if (not self.pinned(data, installed=installed) or data.get("slug") != record.get("slug") or
                data.get("repository") != record.get("repository_id") or data.get("available") is not True or
                data.get(version_key) != self.release.version):
            state = "user_modified" if installed and record.get("managed") else "needs_reconcile"
            raise HomeWorkerFault("home_worker_app_contract_mismatch", state)
        if installed and (not isinstance(data.get("options"), dict) or data.get("state") not in APP_STATES):
            state = "user_modified" if record.get("managed") else "needs_reconcile"
            raise HomeWorkerFault("home_worker_app_contract_mismatch", state)
        return data

    @staticmethod
    def at_defaults(app):
        return app.get("options") == {} and app.get("boot") == "auto" and app.get("state") == "stopped"

    @staticmethod
    def published_pairing(options):
        if not isinstance(options, dict) or set(options) != {"pairing_fingerprint", "pairing_code"}:
            return None
        fingerprint, code = options.get("pairing_fingerprint"), options.get("pairing_code")
        if (not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint) or
                not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", code)):
            return None
        return fingerprint, code

    async def installed(self, record, token):
        result = await self.request(record, token, "GET", "/addons")
        if (not isinstance(result, dict) or not isinstance(result.get("addons"), list) or
                any(not isinstance(item, dict) or not isinstance(item.get("slug"), str) for item in result["addons"])):
            raise HomeWorkerFault("home_worker_supervisor_contract_invalid", "needs_reconcile")
        matches = [item for item in result["addons"] if item["slug"] == record["slug"]]
        if len(matches) > 1:
            raise HomeWorkerFault("home_worker_supervisor_contract_invalid", "needs_reconcile")
        if not matches:
            return None
        raw = await self.request(record, token, "GET", f"/addons/{record['slug']}/info")
        return self.validate_app(raw, record, installed=True)

    def note_app(self, record, app):
        self.save(record, app_state=app.get("state"), version=app.get("version"), observed_at=utcnow())

    def waiting(self, record):
        operation = record.get("operation")
        if operation and self.clock() >= operation["deadline"]:
            raise HomeWorkerFault("home_worker_operation_unconfirmed", "needs_reconcile")
        self.save(record, state="waiting", code="home_worker_observation_pending")

    async def effect(self, record, token, kind, path, body=None):
        await self.owner(record, token)
        supervisor = await self.request(record, token, "GET", "/supervisor/info")
        if not isinstance(supervisor, dict) or supervisor.get("healthy") is not True:
            raise HomeWorkerFault("home_worker_supervisor_not_healthy", "unavailable")
        await self.owner(record, token)
        if kind in {"start", "restart", "clear_options"}:
            latest = await self.installed(record, token)
            if latest is None:
                raise HomeWorkerFault("home_worker_app_removed", "user_removed")
            if not self.pinned(latest, installed=True):
                raise HomeWorkerFault("home_worker_app_contract_mismatch", "user_modified")
            if kind == "start" and not self.at_defaults(latest):
                raise HomeWorkerFault("home_worker_configuration_changed", "user_modified")
            if kind == "restart" and latest.get("state") not in {"started", "error"}:
                raise HomeWorkerFault("home_worker_app_state_unconfirmed", "needs_reconcile")
            if kind == "clear_options":
                if self.privilege_view(latest) != record.get("privilege_view") or latest.get("state") != "started":
                    raise HomeWorkerFault("home_worker_privilege_changed", "needs_reconcile")
        if kind == "restart" and record.get("stage") == "pair":
            if record.get("pair_restarts", 0) >= 1:
                raise HomeWorkerFault("home_worker_pairing_unconfirmed", "needs_reconcile")
            self.save(record, pair_restarts=record.get("pair_restarts", 0) + 1, failed_code_hash=None,
                      pair_deadline=self.clock() + STARTUP_GRACE)
        if kind == "start":
            self.save(record, start_sent=True)
        operation = {"kind": kind, "state": "sent", "created": self.clock(),
                     "deadline": self.clock() + OPERATION_TIMEOUT, "job_id": None}
        self.save(record, operation=operation, state="running", code=None)
        result = await self.request(record, token, "POST", path, body)
        if isinstance(result, dict) and "job_id" in result:
            if not isinstance(result["job_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", result["job_id"]):
                raise HomeWorkerFault("home_worker_job_contract_invalid", "needs_reconcile")
            operation["job_id"] = result["job_id"]
        operation["state"] = "acknowledged"
        self.save(record, operation=operation)

    async def job(self, record, token):
        operation = record.get("operation")
        if not operation or not operation.get("job_id"):
            return True
        raw = await self.request(record, token, "GET", "/jobs/" + operation["job_id"])
        if (not isinstance(raw, dict) or raw.get("uuid") != operation["job_id"] or raw.get("reference") != record["slug"] or
                "done" not in raw or (raw["done"] is not None and type(raw["done"]) is not bool) or
                not isinstance(raw.get("errors"), list)):
            raise HomeWorkerFault("home_worker_job_contract_invalid", "needs_reconcile")
        if raw["done"] is not True:
            return False
        if raw["errors"]:
            raise HomeWorkerFault("home_worker_supervisor_job_failed", "needs_reconcile")
        return True

    async def tick(self):
        # No auto-enroll. A missing record stays missing, including after completed setup.
        async with self.lock:
            record = self.record()
            if not record or not record.get("enabled") or record.get("state") in TERMINAL:
                return self.status()
            token = self.ha.token
            try:
                await self.owner(record, token)
                await self.advance(record, token)
            except asyncio.CancelledError:
                raise
            except HomeWorkerFault as exc:
                current = self.record()
                if current and current["id"] == record["id"] and current["enabled"]:
                    extra = {"enabled": False} if exc.state in USER_STOP else {}
                    self.save(current, state=exc.state, code=exc.code, **extra)
            except Exception:
                current = self.record()
                if current and current["id"] == record["id"] and current["enabled"]:
                    operation = current.get("operation")
                    expired = operation and self.clock() >= operation["deadline"]
                    self.save(current, state="needs_reconcile" if expired else "unavailable",
                              code="home_worker_operation_unconfirmed" if expired else "home_worker_observation_unavailable")
            return self.status()

    async def advance(self, record, token):
        stage = record["stage"]
        if stage == "repository":
            await self.advance_repository(record, token)
        elif stage == "install":
            await self.advance_install(record, token)
        elif stage == "start":
            await self.advance_start(record, token)
        elif stage == "pair":
            await self.advance_pair(record, token)
        elif stage in {"login", "monitor"}:
            await self.advance_observe(record, token)
        else:
            raise HomeWorkerFault("home_worker_state_invalid", "needs_reconcile")

    async def advance_repository(self, record, token):
        repositories = await self.request(record, token, "GET", "/store/repositories")
        if (not isinstance(repositories, list) or any(not isinstance(item, dict) or not isinstance(item.get("source"), str) or
                not isinstance(item.get("slug"), str) for item in repositories)):
            raise HomeWorkerFault("home_worker_store_contract_invalid", "needs_reconcile")
        matches = [item for item in repositories if item["source"] == self.release.repository]
        if not matches:
            if record.get("operation"):
                self.waiting(record)
            else:
                await self.effect(record, token, "add_repository", "/store/repositories",
                                  {"repository": self.release.repository})
            return
        if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{8}", matches[0]["slug"]):
            raise HomeWorkerFault("home_worker_store_contract_invalid", "needs_reconcile")
        repository_id = matches[0]["slug"]
        slug = repository_id + "_" + self.release.app
        self.save(record, repository_id=repository_id, slug=slug)
        try:
            raw = await self.request(record, token, "GET", f"/store/addons/{slug}/info")
        except HAError as exc:
            if exc.status == 404:
                self.save(record, state="unavailable", code="home_worker_app_not_published", operation=None)
                return
            raise
        self.validate_app(raw, record, installed=False)
        self.save(record, stage="install", operation=None, state="running", code=None)

    async def advance_install(self, record, token):
        app = await self.installed(record, token)
        operation = record.get("operation")
        if app:
            if not operation or operation.get("kind") != "install":
                self.adopt_existing(record, app)
                return
            confirmation = "supervisor_job_and_app" if operation.get("job_id") else "app_postcondition"
            try:
                complete = await self.job(record, token)
            except HAError:
                complete = True
                confirmation = "app_postcondition_job_unavailable"
            if not complete:
                self.waiting(record)
                return
            await self.owner(record, token)
            repositories = await self.request(record, token, "GET", "/store/repositories")
            if (not isinstance(repositories, list) or
                    [item.get("source") for item in repositories if isinstance(item, dict) and
                     item.get("slug") == record["repository_id"]] != [self.release.repository]):
                raise HomeWorkerFault("home_worker_repository_changed", "needs_reconcile")
            app = await self.installed(record, token)
            if app is None:
                self.waiting(record)
                return
            if not self.at_defaults(app):
                raise HomeWorkerFault("home_worker_initial_configuration_changed", "needs_reconcile")
            await self.owner(record, token)
            self.save(record, managed=True, installed_by_us=True, stage="start", operation=None,
                      install_confirmation=confirmation, state="running", code=None)
            return
        # An install that has not appeared yet is observed, never posted again.
        if operation:
            await self.job(record, token)
            self.waiting(record)
            return
        raw = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
        self.validate_app(raw, record, installed=False)
        await self.request(record, token, "GET", f"/store/addons/{record['slug']}/availability")
        await self.effect(record, token, "install", f"/store/addons/{record['slug']}/install", {"background": True})

    def adopt_existing(self, record, app):
        """A pre-existing App is adopted only when the pin matches. It is never repaired or started."""
        options = app.get("options")
        pairing = self.published_pairing(options)
        if app.get("state") == "started" and (options == {} or pairing):
            self.save(record, managed=True, installed_by_us=False, stage="pair", operation=None, state="running",
                      code=None, pair_deadline=self.clock() + STARTUP_GRACE)
            return
        if app.get("state") == "stopped":
            self.save(record, enabled=False, state="user_disabled", code="home_worker_app_stopped")
            return
        raise HomeWorkerFault("home_worker_existing_app_unconfirmed", "needs_reconcile")

    async def advance_start(self, record, token):
        app = await self.installed(record, token)
        if app is None:
            self.save(record, enabled=False, state="user_removed", code="home_worker_app_removed")
            return
        self.note_app(record, app)
        if app.get("state") in {"error", "unknown"}:
            raise HomeWorkerFault("home_worker_app_state_unconfirmed", "needs_reconcile")
        if app.get("state") == "startup":
            self.waiting(record) if record.get("operation") else self.save(
                record, state="waiting", code="home_worker_app_starting")
            return
        if app.get("state") == "started":
            self.save(record, stage="pair", operation=None, state="running", code=None, started_once=True,
                      pair_deadline=self.clock() + STARTUP_GRACE)
            return
        if app.get("state") != "stopped":
            raise HomeWorkerFault("home_worker_app_state_unconfirmed", "needs_reconcile")
        # Stopped after a start we already sent is not started again. A lost reply stays unconfirmed.
        if record.get("started_once") or not record.get("installed_by_us") or not self.at_defaults(app):
            self.save(record, enabled=False, state="user_disabled", code="home_worker_app_stopped")
            return
        if record.get("start_sent"):
            if record.get("operation") and not await self.job(record, token):
                self.waiting(record)
                return
            if record.get("operation") and self.clock() < record["operation"]["deadline"]:
                self.waiting(record)
                return
            raise HomeWorkerFault("home_worker_operation_unconfirmed", "needs_reconcile")
        await self.effect(record, token, "start", f"/addons/{record['slug']}/start")

    async def advance_pair(self, record, token):
        operation = record.get("operation") or {}
        if operation.get("kind") == "clear_options":
            await self.finish_clear(record, token)
            return
        if operation.get("kind") == "restart":
            if not await self.job(record, token):
                self.waiting(record)
                return
            self.save(record, operation=None, state="running", code="home_worker_pairing_pending",
                      pair_deadline=self.clock() + STARTUP_GRACE)
            return
        app = await self.installed(record, token)
        if app is None:
            self.save(record, enabled=False, state="user_removed", code="home_worker_app_removed")
            return
        self.note_app(record, app)
        if app.get("state") == "stopped":
            self.save(record, enabled=False, state="user_disabled", code="home_worker_app_stopped")
            return
        if app.get("state") != "started":
            self.save(record, state="waiting", code="home_worker_app_starting")
            return
        published = self.published_pairing(app.get("options"))
        if not published:
            if app.get("options") not in ({}, None):
                self.save(record, enabled=False, state="user_modified", code="home_worker_configuration_changed")
                return
            deadline = record.get("pair_deadline")
            if deadline is None:
                self.save(record, pair_deadline=self.clock() + STARTUP_GRACE, state="waiting",
                          code="home_worker_pairing_pending")
                return
            if self.clock() < deadline:
                self.save(record, state="waiting", code="home_worker_pairing_pending")
                return
            # The App cannot call Supervisor while hassio_api stays false. Do not enable it.
            if app.get("hassio_api") is False:
                raise HomeWorkerFault("home_worker_self_options_rejected", "needs_reconcile")
            raise HomeWorkerFault("home_worker_pairing_unconfirmed", "needs_reconcile")
        fingerprint, code = published
        peer = self.store.setting("runner_haos")
        if peer:
            if peer.get("host") != HOST or peer.get("port") != PORT or peer.get("fingerprint") != fingerprint:
                raise HomeWorkerFault("home_worker_pairing_mismatch", "needs_reconcile")
            await self.begin_clear(record, token, app, fingerprint)
            return
        if self.busy():
            self.save(record, state="waiting", code="home_worker_coding_busy")
            return
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        if record.get("failed_code_hash") == code_hash:
            await self.begin_pair_restart(record, token)
            return
        try:
            await self.runner.pair({"host": HOST, "port": PORT, "fingerprint": fingerprint, "code": code},
                                   "host-owner", self.controller_id)
        except RunnerError as exc:
            self.save(record, failed_code_hash=code_hash)
            if exc.status_code in {403, 409, 429} or exc.status_code is None:
                await self.begin_pair_restart(record, token)
                return
            raise HomeWorkerFault("home_worker_pair_failed", "needs_reconcile") from None
        await self.begin_clear(record, token, app, fingerprint)

    async def begin_pair_restart(self, record, token):
        if record.get("pair_restarts", 0) >= 1 or self.busy():
            raise HomeWorkerFault("home_worker_pairing_unconfirmed" if not self.busy() else "home_worker_coding_busy",
                                  "needs_reconcile" if not self.busy() else "unavailable")
        await self.effect(record, token, "restart", f"/addons/{record['slug']}/restart")

    async def begin_clear(self, record, token, app, fingerprint):
        if app.get("options") == {}:
            self.save(record, stage="login", state="login_ready", operation=None, code="home_worker_login_required",
                      fingerprint=fingerprint, failed_code_hash=None, privilege_view=None)
            return
        self.save(record, fingerprint=fingerprint, privilege_view=self.privilege_view(app), failed_code_hash=None)
        await self.effect(record, token, "clear_options", f"/addons/{record['slug']}/options", {"options": {}})
        await self.finish_clear(record, token)

    async def finish_clear(self, record, token):
        if not await self.job(record, token):
            self.waiting(record)
            return
        await self.owner(record, token)
        app = await self.installed(record, token)
        if app is None:
            raise HomeWorkerFault("home_worker_app_removed", "user_removed")
        if self.privilege_view(app) != record.get("privilege_view"):
            raise HomeWorkerFault("home_worker_privilege_changed", "needs_reconcile")
        if app.get("options") != {}:
            if (record.get("operation") or {}).get("job_id"):
                self.waiting(record)
                return
            raise HomeWorkerFault("home_worker_options_unconfirmed", "needs_reconcile")
        self.save(record, stage="login", state="login_ready", operation=None, code="home_worker_login_required",
                  privilege_view=None, failed_code_hash=None)

    async def advance_observe(self, record, token):
        operation = record.get("operation") or {}
        if operation.get("kind") == "restart":
            if not await self.job(record, token):
                self.waiting(record)
                return
            self.save(record, operation=None)
        app = await self.installed(record, token)
        if app is None:
            self.save(record, enabled=False, state="user_removed", code="home_worker_app_removed")
            return
        self.note_app(record, app)
        if app.get("options") != {}:
            self.save(record, enabled=False, state="user_modified", code="home_worker_configuration_changed")
            return
        if app.get("state") == "stopped":
            self.save(record, enabled=False, state="user_disabled", code="home_worker_app_stopped")
            return
        if app.get("state") in {"error", "unknown"}:
            self.save(record, state="degraded", code="home_worker_app_crashed")
            return
        if app.get("state") != "started":
            self.save(record, state="waiting", code="home_worker_app_starting")
            return
        await self.observe_worker(record)

    async def observe_worker(self, record):
        # Read the capability poll only. Never call the coding auth API or write credential files.
        try:
            raw = await self.runner.call("GET", "/coding/capabilities")
        except (RunnerError, OSError, TimeoutError, ValueError, TypeError):
            self.save(record, state="login_ready", stage="login", code="home_worker_worker_unavailable",
                      worker_available=False)
            return
        if not isinstance(raw, dict) or raw.get("worker_id") != "home-coder" or raw.get("runtime_location") != "haos_app":
            self.save(record, state="login_ready", stage="login", code="home_worker_capability_mismatch",
                      worker_available=False, runtime_location=None, credential_state=None)
            return
        credential = None
        for provider in raw.get("providers") or []:
            if isinstance(provider, dict) and provider.get("id") == "codex":
                value = provider.get("credential_state")
                credential = value if value in CREDENTIALS else "unavailable"
                break
        available = raw.get("available") is True
        ready = available and credential in {"configured", "inference_verified"}
        self.save(record, credential_state=credential, worker_available=available, runtime_location="haos_app",
                  state="ready" if ready else "login_ready", stage="monitor" if ready else "login",
                  code=None if ready else "home_worker_login_required")
