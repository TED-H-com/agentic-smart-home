"""Owner-started install, pair and login preparation for the Home coding worker.

The tick never creates a record and never posts to Supervisor unless this record
is already authorized. Pairing codes stay in Supervisor options only long enough
to call the existing runner pairing path; they are not stored, logged or returned.

The trusted repository is the sidebar ``#stable`` channel, and the version is
``agentic_controller.VERSION``. While ``update_available``, observation reads the
installed App only: it does not pair, restart, clear options, or poll login.
Setup that has not located the App, or a missing App at ``repository`` or
``install``, adopts this release and continues the normal install. A finished
install of the older App is recorded, then the Owner update is offered. A
missing App at a later stage stays removed.
A legacy ``#release/X.Y.Z`` slug is not uninstalled. Removing an App deletes
its data directory, including the worker credential store.
HAOS boot and backups stop and start Apps: App states other than ``started`` decide nothing
and no effect is posted unless Supervisor ``/info`` is ``running``; a stop must persist.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import asdict, dataclass

from . import VERSION
from .bridge_maintenance import (
    UPDATE_BODY,
    BridgeRelease,
    maintenance_update,
    release_drift,
    version_gate,
    version_order,
)
from .ha import HAError
from .models import digest, utcnow
from .runner import RunnerError

KEY = "home_worker_maintenance"
HOST, PORT = "192.168.177.2", 8443
STARTUP_GRACE = 120
OPERATION_TIMEOUT = 600
JOB_TIMEOUT = 3600  # A running Supervisor job, such as the local image build, is progress.
STOP_CONFIRM_SECONDS = 30  # A boot, backup or restart passes through stopped; an Owner stop persists.
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
SETTLED = {"running", "waiting", "login_ready", "ready", "degraded"}


@dataclass(frozen=True)
class HomeWorkerRelease:
    """Same trusted repository as the sidebar App. The runner contract is fixed here."""

    repository: str = BridgeRelease().repository
    app: str = "agentic_runner"
    version: str = VERSION


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

    def drift(self, record):
        return release_drift(record.get("release") if isinstance(record, dict) else None, self.release)

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
        limitations = ["supervisor_trusted_repository_not_atomic_digest_pin",
                       "stopped_or_removed_app_returns_only_on_owner_repair",
                       "no_heartbeat_restart",
                       "hassio_api_is_not_enabled_for_self_options",
                       "credential_configured_is_not_promoted_to_inference_verified"]
        stored = record.get("release") if isinstance(record, dict) else None
        if isinstance(stored, dict) and stored.get("repository") != self.release.repository:
            # A different branch is a different App slug. Updating it would delete the App data directory.
            limitations.insert(3, "no_app_update_across_releases")
        result["limitations"] = limitations
        result["update"] = maintenance_update(
            "home_worker", self.release, record, self.drift(record) if record else "current")
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
        drift = self.drift(current)
        if drift == "channel":
            raise HomeWorkerFault("home_worker_release_channel_changed", "needs_reconcile")
        if drift == "ahead":
            raise HomeWorkerFault("home_worker_release_ahead", "waiting")
        if drift == "invalid":
            raise HomeWorkerFault("home_worker_release_unparsable", "needs_reconcile")
        # Same repository, older version stays readable. Pairing and login wait for the Owner update.

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

    async def supervisor_running(self, record, token):
        """Only a Supervisor in ``running`` lets an App state decide. Boot and backups move Apps themselves."""
        raw = await self.request(record, token, "GET", "/info")
        state = raw.get("state") if isinstance(raw, dict) else None
        if not isinstance(state, str):
            raise HomeWorkerFault("home_worker_observation_unavailable", "unavailable")
        if state != "running":
            self.save(record, stopped_since=None)
            raise HomeWorkerFault("home_worker_supervisor_busy", "waiting")

    def confirm_stop(self, record):
        """Disable only a stop seen again STOP_CONFIRM_SECONDS later with nothing else in between."""
        since, now = record.get("stopped_since"), self.clock()
        if type(since) not in (int, float) or since > now:
            since = now
        if now - since < STOP_CONFIRM_SECONDS:
            self.save(record, stopped_since=since, state="waiting", code="home_worker_observation_pending")
            return
        self.save(record, enabled=False, state="user_disabled", code="home_worker_app_stopped", stopped_since=None)

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
                if previous["owner"] != owner:
                    raise HomeWorkerFault("home_worker_authorization_changed", "needs_reconcile")
                drift = self.drift(previous)
                if drift == "channel":
                    # A legacy #release/X.Y.Z slug is a different App. Do not uninstall or add #stable.
                    raise HomeWorkerFault("home_worker_release_channel_changed", "needs_reconcile")
                if drift == "ahead":
                    raise HomeWorkerFault("home_worker_release_ahead", "waiting")
                if drift == "invalid":
                    raise HomeWorkerFault("home_worker_release_unparsable", "needs_reconcile")
                # An older same-channel record, including user_disabled, is left unchanged.
                return self.status()
            record = {"id": uuid.uuid4().hex, "owner": owner, "enabled": True, "managed": False,
                      "state": "running", "stage": "repository", "code": None, "release": asdict(self.release),
                      "operation": None, "pair_restarts": 0, "start_sent": False, "installed_by_us": False,
                      "setup_run_id": setup.get("run_id"), "fingerprint": None, "credential_state": None}
            self.save(record)
            return self.status()

    async def repair(self, *, commit_guard=None):
        """Owner retry. Re-reads the App from the first stage and allows one start or install.

        The tick alone never undoes a stop, removal or edit. Privileges, ports and options are never written.
        """
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
            drift = self.drift(record)
            if drift == "channel":
                raise HomeWorkerFault("home_worker_release_channel_changed", "needs_reconcile")
            if drift == "ahead":
                raise HomeWorkerFault("home_worker_release_ahead", "waiting")
            if drift == "invalid":
                raise HomeWorkerFault("home_worker_release_unparsable", "needs_reconcile")
            if drift == "update" and (record.get("operation") or {}).get("kind") == "update":
                # Observe the update already sent. Do not post it again.
                self.save(record, enabled=True, state="running", code=None)
                return self.status()
            if record.get("enabled") and record.get("state") in SETTLED | {"update_available"}:
                return self.status()
            operation = record.get("operation")
            if operation and record.get("state") == "unavailable" and self.clock() < operation["deadline"]:
                # A transient fault during a Supervisor job. Observe the job instead of posting again.
                self.save(record, enabled=True, state="running", code=None)
                return self.status()
            self.save(record, enabled=True, state="running", code=None, stage="repository", operation=None,
                      pair_restarts=0, pair_deadline=None, failed_code_hash=None, start_sent=False,
                      started_once=False, owner_start=True)
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
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # A store or status error must not end observation for the process lifetime.
            await asyncio.sleep(15)

    def pinned(self, data, *, installed):
        # Store listings can omit port and device fields. A field that is present must already match.
        # Store info reports apparmor "default" until install loads runner/apparmor.txt.
        # The installed readback is the adoption pin and must contain every field.
        exact = (("privileged", PRIVILEGED), ("devices", DEVICES), ("network", NETWORK))
        apparmor = (APPARMOR,) if installed else (APPARMOR, "default")
        return (isinstance(data, dict) and all(data.get(key) is False for key in SAFE_FLAGS) and
                all(data.get(key) == expected for key, expected in exact if installed or key in data) and
                (data.get("apparmor") in apparmor if installed or "apparmor" in data else True))

    @staticmethod
    def privilege_view(app):
        return {"privileged": app.get("privileged"), "devices": app.get("devices"), "apparmor": app.get("apparmor"),
                "network": app.get("network"), **{key: app.get(key) for key in SAFE_FLAGS}}

    def validate_app(self, data, record, *, installed, expected_version=None):
        version_key = "version" if installed else "version_latest"
        expected = self.release.version if expected_version is None else expected_version
        if (not self.pinned(data, installed=installed) or data.get("slug") != record.get("slug") or
                data.get("repository") != record.get("repository_id") or data.get("available") is not True or
                data.get(version_key) != expected):
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

    async def app_info(self, record, token):
        result = await self.request(record, token, "GET", "/addons")
        if (not isinstance(result, dict) or not isinstance(result.get("addons"), list) or
                any(not isinstance(item, dict) or not isinstance(item.get("slug"), str) for item in result["addons"])):
            raise HomeWorkerFault("home_worker_supervisor_contract_invalid", "needs_reconcile")
        matches = [item for item in result["addons"] if item["slug"] == record["slug"]]
        if len(matches) > 1:
            raise HomeWorkerFault("home_worker_supervisor_contract_invalid", "needs_reconcile")
        if not matches:
            return None
        return await self.request(record, token, "GET", f"/addons/{record['slug']}/info")

    async def installed(self, record, token, *, expected_version=None):
        raw = await self.app_info(record, token)
        if raw is None:
            return None
        return self.validate_app(raw, record, installed=True, expected_version=expected_version)

    def note_app(self, record, app):
        # Any other observation interrupts a pending stop confirmation.
        stopped = record.get("stopped_since") if app.get("state") == "stopped" else None
        self.save(record, app_state=app.get("state"), version=app.get("version"), observed_at=utcnow(),
                  stopped_since=stopped)

    def waiting(self, record):
        operation = record.get("operation")
        if operation and self.clock() >= operation["deadline"]:
            raise HomeWorkerFault("home_worker_operation_unconfirmed", "needs_reconcile")
        self.save(record, state="waiting", code="home_worker_observation_pending")

    async def effect(self, record, token, kind, path, body=None, operation_extra=None):
        if kind != "update" and self.drift(record) == "update":
            raise HomeWorkerFault("home_worker_update_available", "update_available")
        if kind == "update" and self.busy():
            raise HomeWorkerFault("home_worker_busy")
        await self.owner(record, token)
        supervisor = await self.request(record, token, "GET", "/supervisor/info")
        if not isinstance(supervisor, dict) or supervisor.get("healthy") is not True:
            raise HomeWorkerFault("home_worker_supervisor_not_healthy", "unavailable")
        # Before the readbacks and counters below: a boot or cold backup can show the App stopped mid-transition.
        await self.supervisor_running(record, token)
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
            self.save(record, start_sent=True, owner_start=False)
        if kind == "update" and self.busy():
            raise HomeWorkerFault("home_worker_busy")
        operation = {"kind": kind, "state": "sent", "created": self.clock(),
                     "deadline": self.clock() + OPERATION_TIMEOUT, "job_id": None, **(operation_extra or {})}
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
            operation["deadline"] = min(operation["created"] + JOB_TIMEOUT,
                                        max(operation["deadline"], self.clock() + OPERATION_TIMEOUT))
            self.save(record, operation=operation)
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
        if self.drift(record) == "update" or (record.get("operation") or {}).get("kind") == "update":
            await self.advance_update(record, token)
            return
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
        # Core's WebSocket proxy reports a missing App as unknown_error, not 404. Read the listing instead.
        listing = await self.request(record, token, "GET", "/store/addons")
        if (not isinstance(listing, dict) or not isinstance(listing.get("addons"), list) or
                any(not isinstance(item, dict) for item in listing["addons"])):
            raise HomeWorkerFault("home_worker_store_contract_invalid", "needs_reconcile")
        if not any(item.get("slug") == slug for item in listing["addons"]):
            self.save(record, state="unavailable", code="home_worker_app_not_published", operation=None)
            return
        raw = await self.request(record, token, "GET", f"/store/addons/{slug}/info")
        raw = await self.lockstep(record, token, raw)
        self.validate_app(raw, record, installed=False)
        self.save(record, stage="install", operation=None, state="running", code=None)

    async def advance_install(self, record, token):
        app = await self.installed(record, token)
        operation = record.get("operation")
        if app:
            if not operation or operation.get("kind") != "install":
                await self.adopt_existing(record, token, app)
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
        raw = await self.lockstep(record, token, raw)
        self.validate_app(raw, record, installed=False)
        await self.request(record, token, "GET", f"/store/addons/{record['slug']}/availability")
        await self.effect(record, token, "install", f"/store/addons/{record['slug']}/install", {"background": True})

    async def adopt_existing(self, record, token, app):
        """A pre-existing App is adopted only when the pin matches. It is never repaired or started."""
        self.note_app(record, app)
        if app.get("state") != "started":
            await self.supervisor_running(record, token)
        options = app.get("options")
        pairing = self.published_pairing(options)
        if app.get("state") == "started" and (options == {} or pairing):
            self.save(record, managed=True, installed_by_us=False, stage="pair", operation=None, state="running",
                      code=None, pair_deadline=self.clock() + STARTUP_GRACE)
            return
        if app.get("state") == "stopped":
            if record.get("owner_start") and self.at_defaults(app):
                self.save(record, managed=True, stage="start", operation=None, state="running", code=None)
                return
            self.confirm_stop(record)
            return
        raise HomeWorkerFault("home_worker_existing_app_unconfirmed", "needs_reconcile")

    async def advance_start(self, record, token):
        app = await self.installed(record, token)
        if app is None:
            self.save(record, enabled=False, state="user_removed", code="home_worker_app_removed")
            return
        self.note_app(record, app)
        if app.get("state") != "started":
            await self.supervisor_running(record, token)
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
        # Only this record's own install, or an explicit owner repair, permits one start.
        allowed = record.get("owner_start") or (record.get("installed_by_us") and not record.get("started_once"))
        if not allowed or not self.at_defaults(app):
            self.confirm_stop(record)
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
        if app.get("state") != "started":
            await self.supervisor_running(record, token)
        if app.get("state") == "stopped":
            self.confirm_stop(record)
            return
        if app.get("state") != "started":
            self.save(record, state="waiting", code="home_worker_app_starting")
            return
        published = self.published_pairing(app.get("options"))
        peer = self.store.setting("runner_haos")
        if not published:
            if app.get("options") not in ({}, None):
                self.save(record, enabled=False, state="user_modified", code="home_worker_configuration_changed")
                return
            if peer and peer.get("host") == HOST and peer.get("port") == PORT and peer.get("fingerprint"):
                # Already paired, for example by the manual JSON flow. The poll checks the TLS pin now.
                await self.begin_clear(record, token, app, peer["fingerprint"])
                return
            deadline = record.get("pair_deadline")
            if deadline is None:
                self.save(record, pair_deadline=self.clock() + STARTUP_GRACE, state="waiting",
                          code="home_worker_pairing_pending")
                return
            if self.clock() < deadline:
                self.save(record, state="waiting", code="home_worker_pairing_pending")
                return
            # Already paired with another Controller, or Supervisor rejected self-options. Never enable hassio_api.
            raise HomeWorkerFault("home_worker_pairing_not_published", "needs_reconcile")
        fingerprint, code = published
        if peer and peer.get("host") == HOST and peer.get("port") == PORT and peer.get("fingerprint") == fingerprint:
            await self.begin_clear(record, token, app, fingerprint)
            return
        # The App publishes only while it has no Controller, so a peer with another fingerprint is stale.
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
            deadline = record.get("pair_deadline")
            if exc.status_code is None and deadline is not None and self.clock() < deadline:
                # The App publishes before its TLS listener is up. Unreachable is not a rejected code.
                self.save(record, state="waiting", code="home_worker_pairing_pending")
                return
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
            # Offer login only after the capability poll confirms a reachable worker and its auth state.
            self.save(record, stage="login", operation=None, fingerprint=fingerprint,
                      failed_code_hash=None, privilege_view=None)
            await self.observe_worker(record)
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
        self.save(record, stage="login", operation=None, privilege_view=None, failed_code_hash=None)
        await self.observe_worker(record)

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
        if app.get("state") != "started":
            await self.supervisor_running(record, token)
        if app.get("state") == "started" and self.published_pairing(app.get("options")):
            # The App lost its pairing and published a new code. Pair again under the same fences.
            self.save(record, stage="pair", state="running", code=None, pair_deadline=self.clock() + STARTUP_GRACE)
            return
        if app.get("options") != {}:
            self.save(record, enabled=False, state="user_modified", code="home_worker_configuration_changed")
            return
        if app.get("state") == "stopped":
            self.confirm_stop(record)
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
            # A login cannot reach an unreachable Runner. Keep polling instead of offering it.
            self.save(record, state="unavailable", code="home_worker_worker_unavailable", worker_available=False)
            return
        if not isinstance(raw, dict) or raw.get("worker_id") != "home-coder" or raw.get("runtime_location") != "haos_app":
            self.save(record, state="unavailable", code="home_worker_capability_mismatch",
                      worker_available=False, runtime_location=None, credential_state=None)
            return
        credential = None
        for provider in raw.get("providers") or []:
            if isinstance(provider, dict) and provider.get("id") == "codex":
                value = provider.get("credential_state")
                credential = value if value in CREDENTIALS else "unavailable"
                break
        available = raw.get("available") is True
        signed_in = credential in {"configured", "inference_verified"}
        if signed_in and not available:
            # Login cannot fix a blocked sandbox or source. Do not ask for it again.
            self.save(record, credential_state=credential, worker_available=False, runtime_location="haos_app",
                      state="unavailable", stage="monitor", code="home_worker_runtime_blocked")
            return
        ready = available and signed_in
        self.save(record, credential_state=credential, worker_available=available, runtime_location="haos_app",
                  state="ready" if ready else "login_ready", stage="monitor" if ready else "login",
                  code=None if ready else "home_worker_login_required")

    async def lockstep(self, record, token, info):
        """Install or update only when ``version_latest`` is exactly this release."""
        latest = info.get("version_latest") if isinstance(info, dict) else None
        decision = version_gate(latest, self.release.version, reloaded=bool(record.get("release_store_reloaded")))
        if decision is None:
            if record.get("release_store_reloaded"):
                self.save(record, release_store_reloaded=False)
            return info
        if decision == "reload":
            self.save(record, release_store_reloaded=True)
            await self.request(record, token, "POST", "/store/reload")
            info = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
            return await self.lockstep(record, token, info)
        if decision == "ahead":
            self.save(record, state="waiting", code="home_worker_release_ahead")
            raise HomeWorkerFault("home_worker_release_ahead", "waiting")
        if decision == "unpublished":
            self.save(record, state="waiting", code="home_worker_release_not_published")
            raise HomeWorkerFault("home_worker_release_not_published", "waiting")
        raise HomeWorkerFault("home_worker_app_contract_mismatch", "needs_reconcile")

    def installed_pin_problem(self, raw, record, *, expected_version, options_digest, prior_state=None, created=None):
        if not isinstance(raw, dict):
            return "home_worker_update_contract_mismatch"
        if raw.get("version") != expected_version:
            return "home_worker_update_version_mismatch"
        if (any(raw.get(key) is not False for key in SAFE_FLAGS) or raw.get("privileged") != PRIVILEGED or
                raw.get("devices") != DEVICES):
            return "home_worker_update_security_mismatch"
        if raw.get("apparmor") != APPARMOR:
            return "home_worker_update_apparmor_mismatch"
        if raw.get("network") != NETWORK:
            return "home_worker_update_port_mismatch"
        options = raw.get("options")
        if not isinstance(options, dict) or digest(options) != options_digest:
            return "home_worker_update_options_mismatch"
        if options != {} and self.published_pairing(options) is None:
            return "home_worker_update_options_mismatch"
        if prior_state is not None:
            expected_state = "started" if prior_state == "startup" else prior_state
            if expected_state in {"started", "stopped"}:
                state = raw.get("state")
                if (expected_state == "started" and state == "startup" and created is not None and
                        self.clock() < created + STARTUP_GRACE):
                    return "home_worker_update_starting"
                if state != expected_state:
                    return "home_worker_update_state_mismatch"
        try:
            self.validate_app(raw, record, installed=True, expected_version=expected_version)
        except HomeWorkerFault:
            return "home_worker_update_contract_mismatch"
        return None

    def adopt_release(self, record, raw, *, stage=None):
        kept = stage or record.get("stage")
        if kept not in {"repository", "install", "start", "pair", "login", "monitor"}:
            kept = "monitor"
        self.save(record, release=asdict(self.release), operation=None, stage=kept,
                  grace_until=self.clock() + STARTUP_GRACE, state="running", code=None,
                  version=raw.get("version"), app_state=raw.get("state"), observed_at=utcnow(),
                  release_store_reloaded=False, stopped_since=None)

    def note_update_available(self, record, raw):
        # Version and App state only. Do not pair, restart, clear options, or offer login.
        self.save(record, state="update_available", code="home_worker_update_available", operation=None,
                  app_state=raw.get("state"), version=raw.get("version"), observed_at=utcnow())

    def options_snapshot(self, options):
        """Digest of the current options. The pairing code itself is not stored."""
        if options == {}:
            return digest({})
        if self.published_pairing(options):
            return digest(options)
        return None

    async def update(self, *, commit_guard=None):
        """Owner update of the installed Home App to exactly this Controller's release."""
        async with self.lock:
            record = self.record()
            if not record:
                raise HomeWorkerFault("home_worker_setup_required")
            token = self.ha.token
            await self.owner(record, token)
            if commit_guard:
                commit_guard()
            if self.busy():
                raise HomeWorkerFault("home_worker_busy")
            operation = record.get("operation") or {}
            if operation.get("kind") == "update" and record.get("state") == "update_available":
                return self.status()
            if record.get("state") != "update_available" or self.drift(record) != "update":
                raise HomeWorkerFault("home_worker_update_not_allowed", "needs_reconcile")
            try:
                await self.begin_update(record, token)
            except HAError:
                current = self.record()
                if current and current["id"] == record["id"]:
                    pending = current.get("operation") or {}
                    lost = pending.get("kind") == "update"
                    expired = lost and self.clock() >= pending.get("deadline", 0)
                    self.save(current, state="needs_reconcile" if expired else "unavailable",
                              code="home_worker_operation_unconfirmed" if expired else
                              "home_worker_observation_unavailable")
            return self.status()

    async def begin_update(self, record, token):
        if self.busy():
            raise HomeWorkerFault("home_worker_busy")
        if not record.get("slug"):
            self.save(record, state="needs_reconcile", code="home_worker_update_app_missing", operation=None)
            return
        info = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
        info = await self.lockstep(record, token, info)
        raw = await self.app_info(record, token)
        if raw is None:
            self.save(record, state="needs_reconcile", code="home_worker_update_app_missing", operation=None)
            return
        snapshot = self.options_snapshot(raw.get("options"))
        order = version_order(raw.get("version"), self.release.version)
        if snapshot is None:
            self.save(record, state="needs_reconcile", code="home_worker_update_options_mismatch", operation=None)
            return
        if order == "same":
            problem = self.installed_pin_problem(
                raw, record, expected_version=self.release.version, options_digest=snapshot,
                prior_state=raw.get("state"), created=self.clock())
            if problem:
                self.save(record, state="needs_reconcile", code=problem, operation=None)
                return
            self.adopt_release(record, raw)
            await self.advance(record, token)
            return
        if order != "older":
            if order == "newer":
                self.save(record, state="waiting", code="home_worker_release_ahead", operation=None)
                return
            self.save(record, state="needs_reconcile", code="home_worker_update_version_mismatch", operation=None)
            return
        problem = self.installed_pin_problem(
            raw, record, expected_version=record.get("release", {}).get("version"), options_digest=snapshot)
        if problem:
            self.save(record, state="needs_reconcile", code=problem, operation=None)
            return
        extra = {"target_release": asdict(self.release), "app_state": raw.get("state"),
                 "options_digest": snapshot, "stage": record.get("stage")}
        await self.effect(record, token, "update", f"/store/addons/{record['slug']}/update", dict(UPDATE_BODY), extra)

    def adopt_in_place(self, record):
        """Pin this release without clearing the stage or an operation already sent."""
        self.save(record, release=asdict(self.release), release_store_reloaded=False)

    async def advance_update(self, record, token):
        operation = record.get("operation") or {}
        if operation.get("kind") == "update":
            await self.finish_update(record, token)
            return
        if not record.get("slug"):
            # Setup never located the App. Continue the normal install at this release.
            self.adopt_in_place(record)
            await self.advance(record, token)
            return
        if operation:
            await self.finish_foreign_operation(record, token)
            return
        if record.get("code") in {"home_worker_release_not_published", "home_worker_release_ahead"}:
            info = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
            await self.lockstep(record, token, info)
        raw = await self.app_info(record, token)
        if raw is None:
            if record.get("stage") in {"repository", "install"}:
                self.adopt_in_place(record)
                await self.advance(record, token)
                return
            self.save(record, enabled=False, state="user_removed", code="home_worker_app_removed")
            return
        order = version_order(raw.get("version"), self.release.version)
        if order == "older":
            self.note_update_available(record, raw)
            return
        if order == "newer":
            self.save(record, state="waiting", code="home_worker_release_ahead")
            return
        if order != "same":
            raise HomeWorkerFault("home_worker_update_version_mismatch", "needs_reconcile")
        snapshot = self.options_snapshot(raw.get("options"))
        if snapshot is None:
            raise HomeWorkerFault("home_worker_update_options_mismatch", "needs_reconcile")
        problem = self.installed_pin_problem(
            raw, record, expected_version=self.release.version, options_digest=snapshot,
            prior_state=raw.get("state"), created=self.clock())
        if problem:
            raise HomeWorkerFault(problem, "needs_reconcile")
        self.adopt_release(record, raw)
        await self.advance(record, token)

    async def finish_foreign_operation(self, record, token):
        operation = record.get("operation") or {}
        try:
            done = await self.job(record, token) if operation.get("job_id") else True
        except HAError:
            if self.clock() >= operation.get("deadline", 0):
                raise HomeWorkerFault("home_worker_operation_unconfirmed", "needs_reconcile") from None
            self.save(record, state="unavailable", code="home_worker_observation_unavailable")
            return
        if not done:
            self.waiting(record)
            return
        if operation.get("kind") == "install":
            await self.finish_observed_install(record, token)
            return
        self.save(record, operation=None, state="update_available", code="home_worker_update_available")

    async def finish_observed_install(self, record, token):
        """Bookkeep a finished install. Do not post it again."""
        operation = record.get("operation") or {}
        raw = await self.app_info(record, token)
        if raw is None:
            self.waiting(record)
            return
        if raw.get("version") == self.release.version:
            self.adopt_in_place(record)
            await self.advance(record, token)
            return
        stored = record.get("release") if isinstance(record.get("release"), dict) else {}
        old = stored.get("version")
        if raw.get("version") == old:
            self.validate_app(raw, record, installed=True, expected_version=old)
            if not self.at_defaults(raw):
                raise HomeWorkerFault("home_worker_initial_configuration_changed", "needs_reconcile")
            confirmation = "supervisor_job_and_app" if operation.get("job_id") else "app_postcondition"
            self.save(record, managed=True, installed_by_us=True, stage="start", operation=None,
                      install_confirmation=confirmation, state="update_available",
                      code="home_worker_update_available")
            return
        raise HomeWorkerFault("home_worker_update_version_mismatch", "needs_reconcile")

    async def update_not_applied(self, record, token, code):
        """A failed or expired update can be retried only when the previous App is still valid."""
        raw = await self.app_info(record, token)
        if raw is not None and raw.get("version") == self.release.version:
            await self.readback_update(record, token)
            return
        stored = record.get("release") if isinstance(record.get("release"), dict) else {}
        old = stored.get("version")
        snapshot = self.options_snapshot(raw.get("options")) if isinstance(raw, dict) else None
        intact = snapshot is not None and self.installed_pin_problem(
            raw, record, expected_version=old, options_digest=snapshot) is None
        if raw is not None and raw.get("version") == old and intact:
            self.save(record, operation=None, state="update_available", code="home_worker_update_failed",
                      app_state=raw.get("state"), version=raw.get("version"), observed_at=utcnow())
            return
        raise HomeWorkerFault(code, "needs_reconcile")

    def note_update_finished(self, record):
        """The readback grace starts when Supervisor finishes, not when the update was sent."""
        operation = record.get("operation") or {}
        if operation.get("done_at"):
            return
        operation["done_at"] = self.clock()
        self.save(record, operation=operation)

    async def finish_update(self, record, token):
        operation = record.get("operation") or {}
        if operation.get("target_release") != asdict(self.release):
            raise HomeWorkerFault("home_worker_release_changed", "needs_reconcile")
        if not operation.get("job_id"):
            await self.observe_unconfirmed_update(record, token)
            return
        try:
            done = await self.job(record, token)
        except HAError:
            await self.observe_unconfirmed_update(record, token)
            return
        except HomeWorkerFault as exc:
            if exc.code != "home_worker_supervisor_job_failed":
                raise
            await self.update_not_applied(record, token, exc.code)
            return
        if not done:
            self.waiting(record)
            return
        self.note_update_finished(record)
        await self.readback_update(record, token)

    async def observe_unconfirmed_update(self, record, token):
        operation = record.get("operation") or {}
        raw = await self.app_info(record, token)
        if raw is not None and raw.get("version") == self.release.version:
            self.note_update_finished(record)
            await self.readback_update(record, token)
            return
        if self.clock() >= operation.get("deadline", 0):
            await self.update_not_applied(record, token, "home_worker_operation_unconfirmed")
            return
        self.save(record, state="unavailable", code="home_worker_observation_unavailable")

    async def readback_update(self, record, token):
        operation = record.get("operation") or {}
        raw = await self.app_info(record, token)
        if raw is None:
            raise HomeWorkerFault("home_worker_update_app_missing", "needs_reconcile")
        problem = self.installed_pin_problem(
            raw, record, expected_version=self.release.version, options_digest=operation.get("options_digest"),
            prior_state=operation.get("app_state"), created=operation.get("done_at") or operation.get("created"))
        if problem == "home_worker_update_starting":
            self.save(record, state="running", code=problem, app_state=raw.get("state"),
                      version=raw.get("version"), observed_at=utcnow())
            return
        if problem:
            raise HomeWorkerFault(problem, "needs_reconcile")
        self.adopt_release(record, raw, stage=operation.get("stage"))
        await self.advance(record, token)
