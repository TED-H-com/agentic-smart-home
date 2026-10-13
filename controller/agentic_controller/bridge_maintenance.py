"""Bounded maintenance for the one product-owned Supervisor sidebar App.

Supervisor is the installation authority. Its documented store API has no atomic
commit/digest install or compare-and-swap options endpoint: the product release
branch is trusted, and its exact source/version/security contract is read back.
Unknown effects are observed, never blindly replayed. GET/status does no I/O.

From this release the trusted repository is ``#stable``. A same-channel older
install stays running until the Owner updates it to this Controller's version.
A legacy ``#release/X.Y.Z`` slug is a different App and is never uninstalled here.
While ``update_available``, observation may read the App and the bridge heartbeat
(``listener_ready``, ``service_last_seen``). It does not restart, repair, or re-pair,
and a missing heartbeat does not become degraded.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import asdict, dataclass

from fastapi import HTTPException

from . import VERSION
from .ha import HAError
from .models import digest, utcnow

KEY = "bridge_maintenance"
STARTUP_GRACE = 120
OPERATION_TIMEOUT = 600
HEARTBEAT_MAX_AGE = 90
MAX_RESTARTS = 2
RESTART_WINDOW = 86400
SAFE_FLAGS = ("hassio_api", "homeassistant_api", "auth_api", "docker_api", "full_access",
              "host_network", "host_pid")
TERMINAL = {"disabled", "user_disabled", "user_removed", "user_modified", "needs_reconcile", "blocked"}
HELD = {"disabled", "user_disabled", "user_removed", "user_modified"}
# One channel for every release. `stable` is moved to the release commit at publish time.
STABLE_REPOSITORY = "https://github.com/teddashh/agentic-smart-home#stable"
UPDATE_BODY = {"backup": False, "background": True}


def parse_version(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    return tuple(int(part) for part in match.groups()) if match else None


def version_order(left, right):
    """Compare numeric X.Y.Z releases. Anything else is a contract mismatch."""
    parsed = parse_version(left), parse_version(right)
    if parsed[0] is None or parsed[1] is None:
        return "invalid"
    if parsed[0] < parsed[1]:
        return "older"
    if parsed[0] > parsed[1]:
        return "newer"
    return "same"


def version_gate(latest, pinned, *, reloaded):
    """Decide whether a store ``version_latest`` may be installed or updated to.

    A lower catalog is reloaded once. A higher catalog is never installed.
    """
    order = version_order(latest, pinned)
    if order == "invalid":
        return "invalid"
    if order == "same":
        return None
    if order == "newer":
        return "ahead"
    return "unpublished" if reloaded else "reload"


def release_drift(stored, release):
    if not isinstance(stored, dict):
        return "invalid"
    if stored.get("repository") != release.repository or stored.get("app") != release.app:
        return "channel"
    order = version_order(stored.get("version"), release.version)
    return {"older": "update", "same": "current", "newer": "ahead"}.get(order, "invalid")


def maintenance_update(prefix, release, record, drift):
    """Pure status for the Owner update button. GET does not derive this from Supervisor."""
    empty = {"available": False, "from": None, "to": None, "blocked_code": None}
    if not isinstance(record, dict):
        return empty
    stored = record.get("release") if isinstance(record.get("release"), dict) else {}
    old = stored.get("version") if isinstance(stored.get("version"), str) else None
    catalog_block = {f"{prefix}_release_ahead", f"{prefix}_release_not_published", f"{prefix}_release_unparsable"}
    if drift == "current":
        code = record.get("code") if record.get("code") in catalog_block else None
        return {"available": False, "from": old, "to": release.version, "blocked_code": code} if code else empty
    blocked = {"channel": f"{prefix}_release_channel_changed", "ahead": f"{prefix}_release_ahead",
               "invalid": f"{prefix}_release_unparsable"}.get(drift)
    if drift == "update" and record.get("state") in HELD:
        blocked = record.get("code")
    elif drift == "update" and record.get("code") in catalog_block:
        blocked = record.get("code")
    available = drift == "update" and record.get("state") == "update_available" and not blocked and record.get("enabled", True)
    if not available and blocked is None and record.get("state") != "update_available" and isinstance(record.get("code"), str):
        blocked = record.get("code")
    return {"available": bool(available), "from": old, "to": release.version, "blocked_code": blocked}


@dataclass(frozen=True)
class BridgeRelease:
    """Trusted application configuration, never accepted from a browser/model."""

    repository: str = STABLE_REPOSITORY
    app: str = "agentic_bridge"
    version: str = VERSION


class BridgeFault(Exception):
    def __init__(self, code, state="blocked"):
        self.code, self.state = code, state
        super().__init__(code)


class BridgeMaintenance:
    def __init__(self, store, ha, bridge, *, release=None, clock=time.time):
        self.store, self.ha, self.bridge = store, ha, bridge
        self.release, self.clock = release or BridgeRelease(), clock
        self.lock, self.task = asyncio.Lock(), None

    def record(self):
        return self.store.setting(KEY)

    def save(self, record, **changes):
        record.update(changes, updated_at=utcnow())
        self.store.set_setting(KEY, record)

    def status(self):
        record = self.record()
        if not record:
            pending = self.store.setting("bridge_enrollment", {})
            return {"contract": 1, "state": pending.get("state", "not_authorized"),
                    "code": pending.get("code"), "auto_repair": False,
                    "release": asdict(self.release), "managed": False, "update": maintenance_update(
                        "bridge", self.release, None, "current")}
        # Never include App options, bearer, arbitrary upstream messages or user identity.
        keys = ("state", "stage", "code", "auto_repair", "release", "managed", "slug",
                "observed_at", "updated_at", "app_state", "version", "repair_count", "pairing_id",
                "install_confirmation")
        result = {"contract": 1, **{key: record.get(key) for key in keys}}
        operation = record.get("operation")
        if operation:
            result["operation"] = {key: operation.get(key) for key in ("kind", "state", "job_id", "deadline")}
        result["limitations"] = ["supervisor_trusted_repository_not_atomic_digest_pin",
                                  "stopped_app_is_not_automatically_restarted",
                                  "same_version_rebuild_requires_manual_reconciliation"]
        result["update"] = maintenance_update("bridge", self.release, record, self.drift(record))
        return result

    def drift(self, record):
        return release_drift(record.get("release") if isinstance(record, dict) else None, self.release)

    async def require_host(self):
        try:
            await self.bridge.require_host()
        except HTTPException as exc:
            raise BridgeFault(exc.detail, "unavailable") from None

    def host_guard(self):
        try:
            self.bridge.host_guard()
        except HTTPException as exc:
            raise BridgeFault(exc.detail, "unavailable") from None

    async def authorize(self, *, auto_repair=False, commit_guard=None):
        """Called by the existing explicitly authorized, completed setup flow only."""
        if type(auto_repair) is not bool:
            raise BridgeFault("bridge_policy_invalid")
        async with self.lock:
            self.effect_gate()
            owner, token = self.store.setting("owner"), self.ha.token
            setup = self.store.get("bootstrap_v1", "setup") or {}
            if setup.get("state") != "completed" or not owner:
                raise BridgeFault("bridge_setup_required")
            await self.require_host()
            if not await self.ha.owner(owner):
                raise BridgeFault("bridge_owner_required")
            if commit_guard:
                commit_guard()
            if owner != self.store.setting("owner") or token != self.ha.token:
                raise BridgeFault("bridge_owner_changed")
            self.effect_gate()
            self.host_guard()
            previous = self.record()
            if previous:
                # Setup reconnects must not undo a manual disable/uninstall or rotate credentials.
                if previous["owner"] != owner:
                    raise BridgeFault("bridge_authorization_changed", "needs_reconcile")
                drift = self.drift(previous)
                if drift == "channel":
                    # A legacy #release/X.Y.Z slug is a different App. Do not uninstall or add #stable.
                    raise BridgeFault("bridge_release_channel_changed", "needs_reconcile")
                if drift == "ahead":
                    raise BridgeFault("bridge_release_ahead", "waiting")
                if drift == "invalid":
                    raise BridgeFault("bridge_release_unparsable", "needs_reconcile")
                if drift == "update":
                    # Re-enable stops at the Owner update. It does not rotate the pairing or restart.
                    if previous["state"] == "disabled":
                        self.save(previous, enabled=True, state="update_available",
                                  code="bridge_update_available")
                    return self.status()
                if previous["state"] == "disabled":
                    return await self.reconnect(previous, token, auto_repair, commit_guard)
                return self.status()
            options = self.bridge.prepare_pairing()
            self.validate_options(options)
            record = {"id": uuid.uuid4().hex, "owner": owner, "enabled": True, "managed": False,
                      "state": "pending", "stage": "repository", "code": None,
                      "release": asdict(self.release), "auto_repair": auto_repair,
                      "pairing_id": options["pairing_id"], "options_digest": digest(options),
                      "operation": None, "repairs": [], "repair_count": 0,
                      "setup_run_id": setup.get("run_id")}
            self.save(record)
            return self.status()

    async def reconnect(self, previous, token, auto_repair, commit_guard):
        """Explicit Owner action only; never called by the background enrollment path."""
        if not previous["managed"] or previous.get("operation"):
            raise BridgeFault("bridge_reconnect_unconfirmed", "needs_reconcile")
        generation = previous["id"]

        def unchanged():
            current = self.record()
            if commit_guard:
                commit_guard()
            self.effect_gate()
            self.host_guard()
            if (not current or current["id"] != generation or current["state"] != "disabled" or
                    self.store.setting("owner") != previous["owner"] or self.ha.token != token):
                raise BridgeFault("bridge_authorization_changed", "needs_reconcile")

        unchanged()
        raw = await self.ha.request("GET", f"/addons/{previous['slug']}/info", supervisor=True, timeout=30)
        unchanged()
        app = self.validate_app(raw, previous, installed=True)
        if not self.configured(app, previous) or app["state"] not in {"started", "stopped"}:
            raise BridgeFault("bridge_configuration_changed", "needs_reconcile")
        if not await self.ha.owner(previous["owner"]):
            raise BridgeFault("bridge_owner_required", "needs_reconcile")
        unchanged()
        options = self.bridge.prepare_pairing(repair=True)
        self.validate_options(options)
        # Only the fingerprint/hash is in the maintenance ledger, never the token.
        record = {**previous, "id": uuid.uuid4().hex, "reconnect_of": generation,
                  "enabled": True, "reconnecting": True, "auto_repair": auto_repair,
                  "pairing_id": options["pairing_id"], "options_digest": digest(options),
                  "initial_settings_digest": self.settings_digest(app), "stage": "configure",
                  "state": "pending", "code": None, "operation": None}
        self.save(record)
        return self.status()

    def suspend(self):
        """Synchronous local fence; caller separately revokes the bridge credential."""
        if record := self.record():
            self.save(record, enabled=False, state="disabled", code="bridge_disabled")
        return self.status()

    async def policy(self, *, auto_repair, commit_guard=None):
        if type(auto_repair) is not bool:
            raise BridgeFault("bridge_policy_invalid")
        async with self.lock:
            record = self.record()
            if not record:
                raise BridgeFault("bridge_setup_required")
            await self.owner(record, self.ha.token)
            if commit_guard:
                commit_guard()
            self.save(record, auto_repair=auto_repair)
            return self.status()

    async def repair(self, *, commit_guard=None):
        """Explicitly resume observations, never erase an uncertain effect or repair budget."""
        async with self.lock:
            record = self.record()
            if not record:
                raise BridgeFault("bridge_setup_required")
            await self.owner(record, self.ha.token)
            if commit_guard:
                commit_guard()
            if not record["enabled"] or record["state"] in HELD:
                raise BridgeFault("bridge_manual_action_required", "needs_reconcile")
            if record.get("code") == "bridge_repair_budget_exhausted":
                raise BridgeFault("bridge_repair_budget_exhausted", "needs_reconcile")
            drift = self.drift(record)
            if drift == "update":
                # An old release is updated in place before any restart or repair effect.
                if (record.get("operation") or {}).get("kind") == "update":
                    return self.status()
                self.save(record, state="update_available", code="bridge_update_available")
                return self.status()
            if drift == "channel":
                raise BridgeFault("bridge_release_channel_changed", "needs_reconcile")
            if drift == "ahead":
                raise BridgeFault("bridge_release_ahead", "waiting")
            if drift == "invalid":
                raise BridgeFault("bridge_release_unparsable", "needs_reconcile")
            self.save(record, state="pending", code=None)
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

    def effect_gate(self):
        if self.store.setting("paused", False):
            raise BridgeFault("bridge_paused", "unavailable")
        if self.store.setting("updater_gate"):
            raise BridgeFault("bridge_update_in_progress", "unavailable")

    def guard(self, record, token):
        self.effect_gate()
        self.host_guard()
        current = self.record()
        if (not current or current["id"] != record["id"] or not current["enabled"] or
                current["owner"] != self.store.setting("owner") or token != self.ha.token):
            raise BridgeFault("bridge_authorization_changed", "needs_reconcile")
        drift = self.drift(current)
        if drift == "channel":
            raise BridgeFault("bridge_release_channel_changed", "needs_reconcile")
        if drift == "ahead":
            raise BridgeFault("bridge_release_ahead", "waiting")
        if drift == "invalid":
            raise BridgeFault("bridge_release_unparsable", "needs_reconcile")
        # Same repository, older version stays readable so heartbeat observation can continue.
        identity = self.bridge.status()
        if identity.get("pairing_id") != record["pairing_id"] or identity.get("state") == "revoked":
            raise BridgeFault("bridge_pairing_changed", "needs_reconcile")

    async def owner(self, record, token):
        await self.require_host()
        self.guard(record, token)
        active = await self.ha.owner(record["owner"])
        self.guard(record, token)
        if not active:
            raise BridgeFault("bridge_owner_required", "needs_reconcile")

    async def request(self, record, token, method, path, body=None):
        self.guard(record, token)
        result = await self.ha.request(method, path, body, supervisor=True, timeout=30)
        self.guard(record, token)
        return result

    @staticmethod
    def validate_options(options):
        if (not isinstance(options, dict) or
                any(not isinstance(options.get(key), str) for key in ("fingerprint", "token", "pairing_id")) or
                set(options) !=
                {"protocol", "host", "port", "fingerprint", "token", "pairing_id"} or
                type(options.get("protocol")) is not int or options["protocol"] != 1 or
                options.get("host") != "192.168.177.1" or type(options.get("port")) is not int or
                options["port"] != 8444 or not re.fullmatch(r"[a-f0-9]{64}", options.get("fingerprint", "")) or
                not re.fullmatch(r"[A-Za-z0-9_-]{64}", options.get("token", "")) or
                not re.fullmatch(r"[a-f0-9]{32}", options.get("pairing_id", ""))):
            raise BridgeFault("bridge_pairing_contract_invalid", "needs_reconcile")

    def validate_app(self, data, record, *, installed, expected_version=None):
        expected = self.release.version if expected_version is None else expected_version
        if (not isinstance(data, dict) or data.get("slug") != record["slug"] or
                data.get("repository") != record["repository_id"] or
                data.get("version" if installed else "version_latest") != expected or
                data.get("available") is not True or data.get("ingress") is not True or
                any(data.get(key) is not False for key in SAFE_FLAGS) or
                data.get("build") is not True):
            raise BridgeFault("bridge_app_contract_mismatch")
        if installed and (data.get("protected") is not True or data.get("ingress_port") != 8099 or
                          data.get("network") not in ({}, None) or
                          data.get("privileged") != [] or data.get("devices") != [] or
                          any(data.get(key) is not False for key in ("host_ipc", "host_uts", "host_dbus")) or
                          data.get("apparmor") != "default" or
                          not isinstance(data.get("options"), dict) or
                          data.get("state") not in {"started", "stopped", "startup", "error", "unknown"}):
            raise BridgeFault("bridge_app_contract_mismatch")
        return data

    async def app_info(self, record, token):
        result = await self.request(record, token, "GET", "/addons")
        if (not isinstance(result, dict) or not isinstance(result.get("addons"), list) or
                any(not isinstance(item, dict) or not isinstance(item.get("slug"), str)
                    for item in result["addons"])):
            raise BridgeFault("bridge_supervisor_contract_invalid")
        matches = [item for item in result["addons"] if item["slug"] == record["slug"]]
        if len(matches) > 1:
            raise BridgeFault("bridge_supervisor_contract_invalid")
        if not matches:
            return None
        return await self.request(record, token, "GET", f"/addons/{record['slug']}/info")

    async def installed(self, record, token, *, expected_version=None):
        raw = await self.app_info(record, token)
        if raw is None:
            return None
        return self.validate_app(raw, record, installed=True, expected_version=expected_version)

    @staticmethod
    def settings_digest(app):
        return digest({key: app.get(key) for key in
                       ("options", "boot", "ingress_panel", "auto_update", "watchdog", "state")})

    @staticmethod
    def configured(app, record):
        return (digest(app["options"]) == record["options_digest"] and app.get("boot") == "auto" and
                app.get("ingress_panel") is True and app.get("auto_update") is False and
                app.get("watchdog") is False)

    async def effect(self, record, token, kind, path, body=None, operation_extra=None):
        if kind != "update" and self.drift(record) == "update":
            # The installed App keeps running, but pairing, repair and restart wait for the Owner update.
            raise BridgeFault("bridge_update_available", "update_available")
        # A second active-owner check is immediately adjacent to the persisted intent/POST.
        await self.owner(record, token)
        supervisor = await self.request(record, token, "GET", "/supervisor/info")
        if not isinstance(supervisor, dict) or supervisor.get("healthy") is not True:
            raise BridgeFault("bridge_supervisor_not_healthy", "unavailable")
        await self.owner(record, token)
        if kind in {"configure", "start", "restart"}:
            # Narrow the native UI race after validation/owner awaits. Supervisor
            # has no conditional-write API; a final concurrent native edit cannot
            # be atomically excluded and is detected by the subsequent readback.
            latest = await self.installed(record, token)
            if latest is None:
                raise BridgeFault("bridge_app_removed", "user_removed")
            if kind == "configure":
                unchanged = self.settings_digest(latest) == record["initial_settings_digest"]
            else:
                unchanged = self.configured(latest, record) and latest["state"] == (
                    "started" if kind == "restart" else "stopped")
            if not unchanged:
                raise BridgeFault("bridge_configuration_changed", "user_modified")
        operation = {"kind": kind, "state": "sent", "created": self.clock(),
                     "deadline": self.clock() + OPERATION_TIMEOUT, "job_id": None, **(operation_extra or {})}
        self.save(record, operation=operation, state="running", code=None)
        result = await self.request(record, token, "POST", path, body)
        if isinstance(result, dict) and "job_id" in result:
            if not isinstance(result["job_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", result["job_id"]):
                raise BridgeFault("bridge_job_contract_invalid", "needs_reconcile")
            operation["job_id"] = result["job_id"]
        operation["state"] = "acknowledged"
        self.save(record, operation=operation)

    async def job(self, record, token):
        operation = record.get("operation")
        if not operation or not operation.get("job_id"):
            return True
        raw = await self.request(record, token, "GET", "/jobs/" + operation["job_id"])
        if (not isinstance(raw, dict) or raw.get("uuid") != operation["job_id"] or
                raw.get("reference") != record["slug"] or "done" not in raw or (raw["done"] is not None and type(raw["done"]) is not bool) or
                not isinstance(raw.get("errors"), list)):
            raise BridgeFault("bridge_job_contract_invalid", "needs_reconcile")
        if raw["done"] is not True:
            return False
        if raw["errors"]:
            raise BridgeFault("bridge_supervisor_job_failed", "needs_reconcile")
        return True

    def waiting(self, record):
        operation = record.get("operation")
        if operation and self.clock() >= operation["deadline"]:
            raise BridgeFault("bridge_operation_unconfirmed", "needs_reconcile")
        self.save(record, state="waiting", code="bridge_observation_pending")

    async def tick(self):
        # Only new setup flows carry this explicit preauthorization. A persisted
        # disabled/removed record is the consumed decision and is never overwritten.
        if not self.record():
            setup = self.store.get("bootstrap_v1", "setup") or {}
            if setup.get("state") == "completed" and setup.get("bridge_auto_enroll") is True:
                try:
                    await self.authorize(auto_repair=True)
                except asyncio.CancelledError:
                    raise
                except BridgeFault as exc:
                    self.store.set_setting("bridge_enrollment", {"run_id": setup.get("run_id"),
                        "state": "unavailable", "code": exc.code})
                    return self.status()
                except Exception:
                    self.store.set_setting("bridge_enrollment", {"run_id": setup.get("run_id"),
                        "state": "unavailable", "code": "bridge_owner_observation_unavailable"})
                    return self.status()
        async with self.lock:
            record = self.record()
            if not record or not record["enabled"] or record["state"] in TERMINAL:
                return self.status()
            token = self.ha.token
            try:
                await self.owner(record, token)
                await self.advance(record, token)
            except asyncio.CancelledError:
                raise  # Persisted intent remains available to exact readback after restart.
            except BridgeFault as exc:
                current = self.record()
                if current and current["id"] == record["id"] and current["enabled"]:
                    self.save(current, state=exc.state, code=exc.code)
            except Exception:
                # A failed read is not proof of absence, stopped state or an unhealthy App.
                current = self.record()
                if current and current["id"] == record["id"] and current["enabled"]:
                    operation = current.get("operation")
                    expired = operation and self.clock() >= operation["deadline"]
                    self.save(current, state="needs_reconcile" if expired else "unavailable",
                              code="bridge_operation_unconfirmed" if expired else "bridge_observation_unavailable")
            return self.status()

    async def advance(self, record, token):
        if self.drift(record) == "update" or (record.get("operation") or {}).get("kind") == "update":
            await self.advance_update(record, token)
            return
        stage, operation = record["stage"], record.get("operation")
        if stage == "repository":
            repositories = await self.request(record, token, "GET", "/store/repositories")
            if (not isinstance(repositories, list) or any(not isinstance(item, dict) or
                    not isinstance(item.get("source"), str) or not isinstance(item.get("slug"), str)
                    for item in repositories)):
                raise BridgeFault("bridge_store_contract_invalid")
            matches = [item for item in repositories if item["source"] == self.release.repository]
            if not matches:
                if operation:
                    self.waiting(record)
                else:
                    await self.effect(record, token, "add_repository", "/store/repositories",
                                      {"repository": self.release.repository})
                return
            if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{8}", matches[0]["slug"]):
                raise BridgeFault("bridge_store_contract_invalid")
            repository_id = matches[0]["slug"]
            self.save(record, repository_id=repository_id, slug=repository_id + "_" + self.release.app,
                      stage="install", operation=None, state="running", code=None)
            return
        if stage == "install":
            app = await self.installed(record, token)
            if app:
                if not operation or operation.get("kind") != "install":
                    raise BridgeFault("bridge_existing_app_unmanaged", "needs_reconcile")
                confirmation = "supervisor_job_and_app" if operation.get("job_id") else "app_postcondition"
                try:
                    complete = await self.job(record, token)
                except HAError:
                    # Supervisor's install Job defaults cleanup=True: a completed
                    # job may already be absent. Failure to read it proves nothing.
                    # Confirm the independent installation postcondition below,
                    # never repeat the original POST or ignore a reported job error.
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
                    raise BridgeFault("bridge_repository_changed", "needs_reconcile")
                app = await self.installed(record, token)
                if app is None:
                    self.waiting(record)
                    return
                # An install completes stopped with the reviewed release defaults.
                # Native edits made before our first configuration are not adopted.
                if (app["options"] != {} or app.get("boot") != "auto" or app["state"] != "stopped" or
                        app.get("ingress_panel") is not False or app.get("auto_update") is not False or
                        app.get("watchdog") is not False):
                    raise BridgeFault("bridge_initial_configuration_changed", "needs_reconcile")
                await self.owner(record, token)
                self.save(record, managed=True, stage="configure", operation=None,
                          install_confirmation=confirmation,
                          initial_settings_digest=self.settings_digest(app), state="running", code=None)
                return
            if operation:
                await self.job(record, token)
                self.waiting(record)
                return
            raw = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
            raw = await self.lockstep(record, token, raw)
            self.validate_app(raw, record, installed=False)
            await self.request(record, token, "GET", f"/store/addons/{record['slug']}/availability")
            await self.effect(record, token, "install", f"/store/addons/{record['slug']}/install",
                              {"background": True})
            return
        app = await self.installed(record, token)
        if app is None:
            self.save(record, enabled=False, state="user_removed", code="bridge_app_removed")
            return
        self.save(record, app_state=app["state"], version=app["version"], observed_at=utcnow())
        if stage == "configure":
            if self.configured(app, record):
                self.save(record, stage="reconnect" if record.get("reconnecting") else "start",
                          operation=None, state="running", code=None)
                return
            if operation or self.settings_digest(app) != record["initial_settings_digest"]:
                raise BridgeFault("bridge_options_changed_or_unconfirmed", "needs_reconcile")
            options = self.bridge.prepare_pairing()
            self.validate_options(options)
            if digest(options) != record["options_digest"]:
                raise BridgeFault("bridge_pairing_changed", "needs_reconcile")
            valid = await self.request(record, token, "POST", f"/addons/{record['slug']}/options/validate", options)
            if not isinstance(valid, dict) or valid.get("valid") is not True:
                raise BridgeFault("bridge_options_contract_invalid")
            await self.effect(record, token, "configure", f"/addons/{record['slug']}/options",
                              {"options": options, "boot": "auto", "auto_update": False,
                               "ingress_panel": True, "watchdog": False})
            return
        if not self.configured(app, record):
            self.save(record, enabled=False, state="user_modified", code="bridge_configuration_changed")
            return
        if stage == "reconnect":
            if operation:
                health = self.bridge.status()
                seen = health.get("service_last_seen")
                if (health.get("listener_ready") is True and type(seen) in (int, float) and
                        seen >= operation["created"] and 0 <= self.clock() - seen <= HEARTBEAT_MAX_AGE):
                    self.save(record, stage="monitor", reconnecting=False, operation=None,
                              grace_until=self.clock() + STARTUP_GRACE, state="healthy", code=None)
                else:
                    self.waiting(record)
            elif app["state"] in {"started", "stopped"}:
                kind = "restart" if app["state"] == "started" else "start"
                await self.effect(record, token, kind, f"/addons/{record['slug']}/{kind}")
            else:
                raise BridgeFault("bridge_app_state_unconfirmed", "needs_reconcile")
            return
        if stage == "start":
            if app["state"] in {"error", "unknown"}:
                raise BridgeFault("bridge_app_state_unconfirmed", "needs_reconcile")
            if app["state"] == "startup":
                self.waiting(record)
                return
            if app["state"] == "started":
                self.save(record, stage="monitor", operation=None, grace_until=self.clock() + STARTUP_GRACE,
                          state="starting", code=None)
            elif operation:
                self.waiting(record)
            else:
                await self.effect(record, token, "start", f"/addons/{record['slug']}/start")
            return
        # Stopped is deliberately ambiguous: crash vs manual stop cannot be proved by state alone.
        if app["state"] == "stopped":
            self.save(record, enabled=False, state="user_disabled", code="bridge_app_stopped")
            return
        if stage != "monitor":
            raise BridgeFault("bridge_state_invalid", "needs_reconcile")
        if app["state"] != "started":
            if app["state"] == "startup" and self.clock() < record.get("grace_until", 0):
                self.save(record, state="starting", code="bridge_app_starting")
                return
            raise BridgeFault("bridge_app_state_unconfirmed", "needs_reconcile")
        health = self.bridge.status()
        seen = health.get("service_last_seen")
        healthy = (health.get("listener_ready") is True and type(seen) in (int, float) and
                   0 <= self.clock() - seen <= HEARTBEAT_MAX_AGE)
        if healthy:
            self.save(record, operation=None, state="healthy", code=None)
            return
        if operation:
            self.waiting(record)
            return
        if self.clock() < record.get("grace_until", 0):
            self.save(record, state="starting", code="bridge_heartbeat_pending")
            return
        if not record["auto_repair"]:
            self.save(record, state="degraded", code="bridge_heartbeat_missing")
            return
        attempts = [at for at in record["repairs"] if self.clock() - at < RESTART_WINDOW]
        if len(attempts) >= MAX_RESTARTS:
            raise BridgeFault("bridge_repair_budget_exhausted", "needs_reconcile")
        # Do not repair while the outer listener itself is down: A owns its bounded health field.
        if health.get("listener_ready") is not True:
            self.save(record, state="unavailable", code="bridge_listener_unavailable")
            return
        # Preserve the budget BEFORE posting. A disconnect or Controller crash cannot reset it.
        attempts.append(self.clock())
        self.save(record, repairs=attempts, repair_count=record["repair_count"] + 1)
        await self.effect(record, token, "restart", f"/addons/{record['slug']}/restart")
        self.save(record, operation=None, grace_until=self.clock() + STARTUP_GRACE,
                  state="starting", code="bridge_restart_observing")

    async def lockstep(self, record, token, info):
        """Install or update only when ``version_latest`` is exactly this release."""
        latest = info.get("version_latest") if isinstance(info, dict) else None
        decision = version_gate(latest, self.release.version, reloaded=bool(record.get("release_store_reloaded")))
        if decision is None:
            if record.get("release_store_reloaded"):
                self.save(record, release_store_reloaded=False)
            return info
        if decision == "reload":
            # Persist before the POST so a lost response cannot reload the store twice.
            self.save(record, release_store_reloaded=True)
            await self.request(record, token, "POST", "/store/reload")
            info = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
            return await self.lockstep(record, token, info)
        if decision == "ahead":
            self.save(record, state="waiting", code="bridge_release_ahead")
            raise BridgeFault("bridge_release_ahead", "waiting")
        if decision == "unpublished":
            self.save(record, state="waiting", code="bridge_release_not_published")
            raise BridgeFault("bridge_release_not_published", "waiting")
        raise BridgeFault("bridge_app_contract_mismatch")

    def installed_pin_problem(self, raw, record, *, expected_version, options_digest, prior_state=None, created=None):
        """One specific readback code, or None when the installed App matches the pin."""
        if not isinstance(raw, dict):
            return "bridge_update_contract_mismatch"
        if raw.get("version") != expected_version:
            return "bridge_update_version_mismatch"
        if any(raw.get(key) is not False for key in SAFE_FLAGS):
            return "bridge_update_security_mismatch"
        if raw.get("apparmor") != "default":
            return "bridge_update_apparmor_mismatch"
        if raw.get("ingress_port") != 8099 or raw.get("network") not in ({}, None):
            return "bridge_update_port_mismatch"
        options = raw.get("options")
        if not isinstance(options, dict) or digest(options) != options_digest:
            return "bridge_update_options_mismatch"
        if not (raw.get("boot") == "auto" and raw.get("ingress_panel") is True and
                raw.get("auto_update") is False and raw.get("watchdog") is False):
            return "bridge_update_configuration_mismatch"
        if prior_state is not None:
            state = raw.get("state")
            if (prior_state == "started" and state == "startup" and created is not None and
                    self.clock() < created + STARTUP_GRACE):
                return "bridge_update_starting"
            if state != prior_state:
                return "bridge_update_state_mismatch"
        try:
            self.validate_app(raw, record, installed=True, expected_version=expected_version)
        except BridgeFault:
            return "bridge_update_contract_mismatch"
        return None

    def adopt_release(self, record, raw, *, stage=None):
        kept = stage or record.get("stage")
        if kept not in {"monitor", "start", "configure", "reconnect"}:
            kept = "monitor"
        self.save(record, release=asdict(self.release), operation=None, stage=kept,
                  grace_until=self.clock() + STARTUP_GRACE, state="running", code=None,
                  version=raw.get("version"), app_state=raw.get("state"), observed_at=utcnow(),
                  release_store_reloaded=False)

    def note_update_available(self, record, raw):
        # Heartbeat stays read-only. A missing listener does not restart or leave this state.
        self.bridge.status()
        self.save(record, state="update_available", code="bridge_update_available", operation=None,
                  app_state=raw.get("state"), version=raw.get("version"), observed_at=utcnow())

    async def update(self, *, commit_guard=None):
        """Owner update of the installed App to exactly this Controller's release."""
        async with self.lock:
            record = self.record()
            if not record:
                raise BridgeFault("bridge_setup_required")
            token = self.ha.token
            await self.owner(record, token)
            if commit_guard:
                commit_guard()
            operation = record.get("operation") or {}
            if operation.get("kind") == "update" and record.get("state") == "update_available":
                return self.status()
            if record.get("state") != "update_available" or self.drift(record) != "update":
                raise BridgeFault("bridge_update_not_allowed", "needs_reconcile")
            try:
                await self.begin_update(record, token)
            except HAError:
                current = self.record()
                if current and current["id"] == record["id"]:
                    pending = current.get("operation") or {}
                    lost = pending.get("kind") == "update"
                    expired = lost and self.clock() >= pending.get("deadline", 0)
                    self.save(current, state="needs_reconcile" if expired else "unavailable",
                              code="bridge_operation_unconfirmed" if expired else "bridge_observation_unavailable")
            return self.status()

    async def begin_update(self, record, token):
        if not record.get("slug"):
            self.save(record, state="needs_reconcile", code="bridge_update_app_missing", operation=None)
            return
        info = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
        info = await self.lockstep(record, token, info)
        raw = await self.app_info(record, token)
        if raw is None:
            self.save(record, state="needs_reconcile", code="bridge_update_app_missing", operation=None)
            return
        order = version_order(raw.get("version"), self.release.version)
        if order == "same":
            problem = self.installed_pin_problem(
                raw, record, expected_version=self.release.version, options_digest=record.get("options_digest"),
                prior_state=raw.get("state"), created=self.clock())
            if problem:
                self.save(record, state="needs_reconcile", code=problem, operation=None)
                return
            self.adopt_release(record, raw)
            await self.advance(record, token)
            return
        if order != "older":
            if order == "newer":
                self.save(record, state="waiting", code="bridge_release_ahead", operation=None)
                return
            self.save(record, state="needs_reconcile", code="bridge_update_version_mismatch", operation=None)
            return
        problem = self.installed_pin_problem(
            raw, record, expected_version=record.get("release", {}).get("version"),
            options_digest=record.get("options_digest"))
        if problem:
            self.save(record, state="needs_reconcile", code=problem, operation=None)
            return
        extra = {"target_release": asdict(self.release), "app_state": raw.get("state"),
                 "options_digest": record.get("options_digest"), "stage": record.get("stage")}
        await self.effect(record, token, "update", f"/store/addons/{record['slug']}/update", dict(UPDATE_BODY), extra)

    async def advance_update(self, record, token):
        operation = record.get("operation") or {}
        if operation.get("kind") == "update":
            await self.finish_update(record, token)
            return
        if operation:
            await self.finish_foreign_operation(record, token)
            return
        if record.get("slug") and record.get("code") in {"bridge_release_not_published", "bridge_release_ahead"}:
            info = await self.request(record, token, "GET", f"/store/addons/{record['slug']}/info")
            await self.lockstep(record, token, info)
        if not record.get("slug"):
            self.save(record, state="update_available", code="bridge_update_available")
            return
        raw = await self.app_info(record, token)
        if raw is None:
            self.save(record, enabled=False, state="user_removed", code="bridge_app_removed")
            return
        order = version_order(raw.get("version"), self.release.version)
        if order == "older":
            self.note_update_available(record, raw)
            return
        if order == "newer":
            self.save(record, state="waiting", code="bridge_release_ahead")
            return
        if order != "same":
            raise BridgeFault("bridge_update_version_mismatch", "needs_reconcile")
        problem = self.installed_pin_problem(
            raw, record, expected_version=self.release.version, options_digest=record.get("options_digest"),
            prior_state=raw.get("state"), created=self.clock())
        if problem:
            raise BridgeFault(problem, "needs_reconcile")
        self.adopt_release(record, raw)
        await self.advance(record, token)

    async def finish_foreign_operation(self, record, token):
        """Observe an in-flight install or repair, then stop. Do not post its next effect."""
        operation = record.get("operation") or {}
        try:
            done = await self.job(record, token) if operation.get("job_id") else True
        except HAError:
            if self.clock() >= operation.get("deadline", 0):
                raise BridgeFault("bridge_operation_unconfirmed", "needs_reconcile") from None
            self.save(record, state="unavailable", code="bridge_observation_unavailable")
            return
        if not done:
            self.waiting(record)
            return
        self.save(record, operation=None, state="update_available", code="bridge_update_available")

    async def finish_update(self, record, token):
        operation = record.get("operation") or {}
        if operation.get("target_release") != asdict(self.release):
            raise BridgeFault("bridge_release_changed", "needs_reconcile")
        if not operation.get("job_id"):
            await self.observe_unconfirmed_update(record, token)
            return
        try:
            done = await self.job(record, token)
        except HAError:
            await self.observe_unconfirmed_update(record, token)
            return
        if not done:
            self.waiting(record)
            return
        await self.readback_update(record, token)

    async def observe_unconfirmed_update(self, record, token):
        """A lost update response is observed. It is never posted again."""
        operation = record.get("operation") or {}
        raw = await self.app_info(record, token)
        if raw is not None and raw.get("version") == self.release.version:
            await self.readback_update(record, token)
            return
        if self.clock() >= operation.get("deadline", 0):
            raise BridgeFault("bridge_operation_unconfirmed", "needs_reconcile")
        self.save(record, state="unavailable", code="bridge_observation_unavailable")

    async def readback_update(self, record, token):
        operation = record.get("operation") or {}
        raw = await self.app_info(record, token)
        if raw is None:
            raise BridgeFault("bridge_update_app_missing", "needs_reconcile")
        problem = self.installed_pin_problem(
            raw, record, expected_version=self.release.version, options_digest=operation.get("options_digest"),
            prior_state=operation.get("app_state"), created=operation.get("created"))
        if problem == "bridge_update_starting":
            self.save(record, state="running", code=problem, app_state=raw.get("state"),
                      version=raw.get("version"), observed_at=utcnow())
            return
        if problem:
            raise BridgeFault(problem, "needs_reconcile")
        self.adopt_release(record, raw, stage=operation.get("stage"))
        await self.advance(record, token)
