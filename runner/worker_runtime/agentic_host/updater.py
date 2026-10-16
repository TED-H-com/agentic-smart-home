"""Publisher-authenticated, Controller-only, same-data-format activation.

No install hooks, arbitrary paths, compose overrides or backup restoration.
Rollback changes the artifact while retaining the current Live database.
"""
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import urllib.request
from contextlib import closing
from pathlib import Path

from .bootstrap import NoRedirect, write_environment
from .coding_volume import DiskVolume
from .fence import fence
from .manager import HostError, atomic, sync_path
from .publisher import TRUST, public_key
from .resources import ready
from .source import SHA40, source_identity

DIGEST = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")
ACTIVE_WORK = {"accepted", "queued", "dispatching", "running", "cancel_requested", "stop_unconfirmed", "needs_reconcile", "waiting_for_user"}
TERMINAL_RUN = {"succeeded", "failed", "cancelled", "expired", "interrupted"}
SNAPSHOT = re.compile(r"(?P<candidate>[a-f0-9]{64})-(?:rollback-)?[a-f0-9]{16}\.sqlite3")
RECOVERY_STATES = {"deploying", "rolling_back", "needs_reconcile"}
# Advertise only from this fixed updater implementation. It protects bridge
# operations and requires the candidate probe to preserve bridge authority.
INGRESS_BRIDGE_CONTRACT = 1
# The signed image digest covers the bundle. Bounds reject a huge copy before
# import. Source objects come only from that local bundle, never from a fetch URL.
SOURCE_BUNDLE_MAX_BYTES = 256 * 1024 * 1024
SOURCE_DOCKER_TIMEOUT = 120
SOURCE_GIT_TIMEOUT = 180
SOURCE_SYNC = {"pending", "moved", "unavailable", "reverted", "needs_reconcile"}


class UpdateError(HostError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def validate_manifest(value, now=None):
    expected = {"format", "publisher", "version", "source_commit", "image", "image_id", "compatibility", "tests", "issued_at", "expires_at"}
    if not isinstance(value, dict) or set(value) != expected or type(value["format"]) is not int or value["format"] != 1:
        raise UpdateError("update_manifest_invalid")
    for key, pattern in (("publisher", r"[a-z0-9-]{1,64}"), ("version", r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?"),
                         ("source_commit", r"[a-f0-9]{40}"), ("image_id", r"sha256:[a-f0-9]{64}"),
                         ("image", r"[a-z0-9.-]+/[a-z0-9_./-]+@sha256:[a-f0-9]{64}")):
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise UpdateError("update_manifest_invalid")
    compatible = value["compatibility"]
    if not isinstance(compatible, dict) or set(compatible) != {"schema", "worker_contract", "runner_protocol", "runtime_digest", "ha_core_versions"}:
        raise UpdateError("update_compatibility_invalid")
    if type(compatible["schema"]) is not int or compatible["schema"] != 3 or any(type(compatible[k]) is not int or compatible[k] != 1 for k in ("worker_contract", "runner_protocol")):
        raise UpdateError("update_compatibility_invalid")
    if not isinstance(compatible["runtime_digest"], str) or not DIGEST.fullmatch(compatible["runtime_digest"]):
        raise UpdateError("update_compatibility_invalid")
    versions = compatible["ha_core_versions"]
    if not isinstance(versions, list) or not 1 <= len(versions) <= 20 or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v) for v in versions):
        raise UpdateError("update_compatibility_invalid")
    tests = value["tests"]
    if not isinstance(tests, list) or not 1 <= len(tests) <= 20:
        raise UpdateError("update_test_evidence_required")
    for item in tests:
        if not isinstance(item, dict) or set(item) != {"name", "result", "sha256"} or item["result"] != "passed" or not isinstance(item["name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", item["name"]) or not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]):
            raise UpdateError("update_test_evidence_invalid")
    now = time.time() if now is None else now
    if any(type(value[k]) not in {int, float} for k in ("issued_at", "expires_at")) or not value["issued_at"] <= now < value["expires_at"] <= value["issued_at"] + 30 * 86400:
        raise UpdateError("update_manifest_expired")
    return value


def offline_check(path, schema, *, pause=False, gate=None):
    """Only call after the fixed Controller process has been proven stopped."""
    with sqlite3.connect(Path(path).as_uri() + ("?mode=rw" if pause else "?mode=ro"), uri=True) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] != schema:
            raise UpdateError("update_schema_mismatch")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise UpdateError("update_database_integrity")
        row = db.execute("SELECT value FROM settings WHERE key='worker_lease'").fetchone()
        if row and json.loads(row[0]) is not None:
            raise UpdateError("update_worker_lease_active")
        # A disabled App may still have a Supervisor operation already sent.
        # Both activation and fallback require its persisted intent to be cleared.
        for key in ("bridge_maintenance", "home_worker_maintenance"):
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row is None:
                continue
            try:
                maintenance = json.loads(row[0])
            except (TypeError, ValueError):
                raise UpdateError("update_active_or_uncertain_work") from None
            if not isinstance(maintenance, dict) or maintenance.get("operation") is not None:
                raise UpdateError("update_active_or_uncertain_work")
        for kind, body in db.execute("SELECT kind,body FROM records WHERE kind IN ('task','work_job','hermes_run')"):
            state = json.loads(body).get("state")
            if ((kind == "task" and state in {"executing", "verifying", "needs_reconcile"}) or
                (kind == "work_job" and state in ACTIVE_WORK) or
                (kind == "hermes_run" and state not in TERMINAL_RUN)):
                raise UpdateError("update_active_or_uncertain_work")
        if pause:
            row = db.execute("SELECT value FROM settings WHERE key='updater_gate'").fetchone()
            if gate and row and json.loads(row[0]) != gate:
                raise UpdateError("update_gate_revision_changed")
            db.execute("INSERT OR REPLACE INTO settings VALUES ('paused','true')")
            if gate:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('updater_gate',?)", (json.dumps(gate),))
            db.commit()


def clear_gate(path, digest):
    with sqlite3.connect(Path(path).as_uri() + "?mode=rw", uri=True) as db:
        row = db.execute("SELECT value FROM settings WHERE key='updater_gate'").fetchone()
        if row and json.loads(row[0]) != digest:
            raise UpdateError("update_gate_revision_changed")
        db.execute("DELETE FROM settings WHERE key='updater_gate'")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('paused','true')")
        db.commit()


class Updater:
    def __init__(self, manager):
        self.manager, self.run = manager, manager.run
        self.root = manager.root / "updates"
        self.coding = manager.root.parent / "coding"
        self.source_repo = self.coding / "source"
        self.source_lock = self.coding / "source.json"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.path = self.root / "records.json"
        self.environment = manager.root / "runtime.env"
        self.data = manager.root.parent / "controller"
        self.database = self.data / "controller.sqlite3"
        self.trust = TRUST if TRUST.exists() else manager.product / "host/update-publishers.json"
        self.probe_path = self.root / "probe.json"
        self.lock = threading.RLock()
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        for value in self.records.values():
            if value["state"] == "staging":
                value.update(state="failed", error_code="update_staging_interrupted")
            elif value["state"] in {"deploying", "rolling_back"}:
                if value.get("source_sync") in {"pending", "moved", "reverted"}:
                    value["source_sync"] = "needs_reconcile"
                value.update(state="needs_reconcile", error_code="update_interrupted")
        atomic(self.path, self.records)
        self.expire_snapshots()

    def expire_snapshots(self):
        """Expire full private DB backups, never images or unresolved recovery data.

        Read the current policy without modifying the running Controller DB.
        If its policy cannot be read safely, leave recovery material untouched
        and retry on the broker's next idle sweep.
        """
        with self.lock:
            try:
                if self.data.is_symlink() or self.database.is_symlink():
                    return 0
                with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                    rows = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('retention','retention_pending')"))
                days = 30
                for key in ("retention", "retention_pending"):
                    policy = json.loads(rows[key]) if key in rows else None
                    if policy is None and key == "retention_pending":
                        continue
                    if key not in rows:
                        continue
                    if not isinstance(policy, dict) or type(policy.get("audit_days")) is not int or not 1 <= policy["audit_days"] <= 30:
                        return 0
                    days = min(days, policy["audit_days"])
                cutoff = time.time() - days * 86400
                protected = {key for key, value in self.records.items() if value["state"] in RECOVERY_STATES}
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except (OSError, sqlite3.Error, ValueError):
                return 0
            removed = 0
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        match = SNAPSHOT.fullmatch(entry.name)
                        if not match or match["candidate"] in protected:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                                os.unlink(entry.name, dir_fd=directory)
                                removed += 1
                        except FileNotFoundError:
                            pass
                if removed:
                    os.fsync(directory)
            finally:
                os.close(directory)
            return removed

    def runtime_digest(self):
        lock = json.loads((self.manager.product / "images.lock.json").read_text())
        values = {k: v for k, v in lock.items() if not k.startswith("agentic-home-controller:")}
        if self.environment.exists():
            for line in self.environment.read_text().splitlines():
                if line.startswith("AGENTIC_HERMES_IMAGE="):
                    values["active_hermes"] = line.split("=", 1)[1]
        return hashlib.sha256(canonical(values)).hexdigest()

    def publishers(self):
        trust = TRUST if TRUST.exists() else self.trust
        if not trust.exists():
            return {}
        # The browser/worker can never install a signing key or change trust.
        for path in (trust, *trust.parents):
            if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                raise UpdateError("update_publisher_store_untrusted")
        value = json.loads(trust.read_text())
        if set(value) != {"format", "publishers"} or value["format"] != 1 or not isinstance(value["publishers"], dict):
            raise UpdateError("update_publisher_store_invalid")
        return value["publishers"]

    def verify(self, manifest, signature, *, rollback=False):
        validate_manifest(manifest, now=manifest.get("issued_at") if rollback else None)
        publisher = self.publishers().get(manifest["publisher"])
        if not isinstance(publisher, dict) or set(publisher) != {"public_key_pem", "image_repository"}:
            raise UpdateError("update_publisher_not_trusted")
        if manifest["image"].split("@", 1)[0] != publisher["image_repository"]:
            raise UpdateError("update_repository_not_trusted")
        try:
            public_key(publisher["public_key_pem"])
        except HostError:
            raise UpdateError("update_publisher_key_invalid") from None
        try:
            raw = base64.b64decode(signature, validate=True)
        except (ValueError, TypeError):
            raise UpdateError("update_signature_invalid") from None
        if len(raw) != 64:
            raise UpdateError("update_signature_invalid")
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.root) as temporary:
            directory = Path(temporary)
            (directory / "manifest").write_bytes(canonical(manifest))
            (directory / "signature").write_bytes(raw)
            (directory / "publisher.pem").write_text(publisher["public_key_pem"])
            check = self.run("openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(directory / "publisher.pem"),
                             "-sigfile", str(directory / "signature"), "-rawin", "-in", str(directory / "manifest"), check=False)
            if check.returncode:
                raise UpdateError("update_signature_invalid")
        if manifest["compatibility"]["runtime_digest"] != self.runtime_digest():
            raise UpdateError("update_runtime_mismatch")

    def update(self, record, **values):
        with self.lock:
            record.update(values)
            atomic(self.path, self.records)

    def public(self, record):
        value = {k: v for k, v in record.items() if k not in {"signature", "source_lock_before"}}
        if value.get("source_sync") not in SOURCE_SYNC:
            value["source_sync"] = "needs_reconcile" if "source_sync" in value else "unavailable"
        return value

    def stage(self, body):
        if not isinstance(body, dict) or set(body) != {"manifest", "signature"}:
            raise UpdateError("update_request_invalid")
        self.verify(body["manifest"], body["signature"])
        digest = hashlib.sha256(canonical(body["manifest"])).hexdigest()
        with self.lock:
            existing = self.records.get(digest)
            if existing and (existing["state"] not in {"staged", "failed"} or
                             (existing["state"] == "staged" and existing.get("approval_expires_at", 0) > time.time())):
                return self.public(existing)
            if any(v["state"] in {"staging", "deploying", "rolling_back", "needs_reconcile"} for v in self.records.values()):
                raise UpdateError("update_busy_or_unresolved")
            record = {"candidate_digest": digest, **body, "state": "staging", "created_at": time.time()}
            self.records[digest] = record
            atomic(self.path, self.records)
        threading.Thread(target=self.prepare, args=(record,), daemon=True).start()
        return self.public(record)

    def prepare(self, record):
        try:
            image = record["manifest"]["image"]
            self.run("docker", "pull", image, timeout=900)
            actual = self.run("docker", "image", "inspect", "--format", "{{.Id}}", image).stdout.strip()
            if actual != record["manifest"]["image_id"]:
                raise UpdateError("update_image_digest_mismatch")
            metadata = json.loads(self.run("docker", "image", "inspect", image).stdout)[0]
            if metadata.get("Os") != "linux" or metadata.get("Architecture") != "amd64" or metadata.get("Config", {}).get("Volumes"):
                raise UpdateError("update_image_runtime_invalid")
            self.stage_source(record)
            self.update(record, state="staged", base_image=self.image(), approval_id=secrets.token_urlsafe(32), approval_expires_at=time.time() + 600)
        except Exception as exc:
            self.update(record, state="failed", error_code=str(exc) if isinstance(exc, UpdateError) else "update_stage_failed")

    def _command(self, *args, timeout=SOURCE_GIT_TIMEOUT, check=True):
        result = self.run(*args, timeout=timeout, check=False)
        if check and result.returncode:
            raise UpdateError("update_source_command_failed")
        return result

    def _git(self, *args, repo=None, timeout=SOURCE_GIT_TIMEOUT, check=True):
        # No repository hooks, and no background gc racing the compare-and-swap.
        return self._command("git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                             "--git-dir", str(repo or self.source_repo), *args, timeout=timeout, check=check)

    def _rev(self, ref="HEAD", repo=None):
        value = self._git("rev-parse", "--verify", ref, repo=repo).stdout.strip()
        if not SHA40.fullmatch(value):
            raise UpdateError("update_source_command_failed")
        return value

    def _read_lock(self):
        path = self.source_lock
        if path.is_symlink() or not path.is_file():
            raise UpdateError("update_source_unavailable")
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            raise UpdateError("update_source_unavailable") from None
        if not isinstance(value, dict) or value.get("repo_id") != "agentic-home":
            raise UpdateError("update_source_unavailable")
        commit = value.get("commit")
        if not isinstance(commit, str) or not SHA40.fullmatch(commit):
            raise UpdateError("update_source_unavailable")
        return value

    def _lock_or_none(self):
        try:
            return self._read_lock()
        except UpdateError:
            return None

    def _require_repo(self):
        if self.source_repo.is_symlink() or not self.source_repo.is_dir() or self.source_lock.is_symlink():
            raise UpdateError("update_source_unavailable")
        return self.source_repo

    @staticmethod
    def _discard(path):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    def _extract_bundle(self, image, destination):
        """Copy the bundle out of a created container. Never start it."""
        if not IMAGE.fullmatch(image):
            raise UpdateError("update_source_extract_failed")
        name = "agentic-update-source-" + secrets.token_hex(8)
        self._discard(destination)
        try:
            self._command("docker", "create", "--name", name, image, timeout=SOURCE_DOCKER_TIMEOUT)
            result = self._command("docker", "cp", name + ":/app/source/agentic-home.bundle", str(destination),
                                   timeout=SOURCE_DOCKER_TIMEOUT, check=False)
            if result.returncode:
                self._discard(destination)
                detail = (result.stderr or "") + (result.stdout or "")
                if "No such container:path" in detail or "Could not find the file" in detail:
                    return False
                raise UpdateError("update_source_extract_failed")
            try:
                info = destination.lstat()
            except OSError:
                raise UpdateError("update_source_extract_failed") from None
            if not stat.S_ISREG(info.st_mode) or not 32 <= info.st_size <= SOURCE_BUNDLE_MAX_BYTES:
                raise UpdateError("update_source_bundle_invalid")
            return True
        finally:
            self._command("docker", "rm", "-f", name, timeout=SOURCE_DOCKER_TIMEOUT, check=False)

    def _verify_bundle(self, bundle, source_commit):
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.root) as temporary:
            empty = Path(temporary) / "empty"
            self._command("git", "init", "-q", "-b", "main", str(empty), timeout=SOURCE_GIT_TIMEOUT)
            verified = self._command("git", "-C", str(empty), "bundle", "verify", str(bundle), timeout=SOURCE_GIT_TIMEOUT, check=False)
            if verified.returncode:
                raise UpdateError("update_source_bundle_invalid")
        listed = self._command("git", "bundle", "list-heads", str(bundle), timeout=SOURCE_GIT_TIMEOUT)
        heads = []
        for line in listed.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2 or not SHA40.fullmatch(parts[0]):
                raise UpdateError("update_source_bundle_invalid")
            heads.append(parts[0])
        if heads != [source_commit]:
            raise UpdateError("update_source_bundle_invalid")

    def stage_source(self, record):
        if not DIGEST.fullmatch(record.get("candidate_digest", "")):
            raise UpdateError("update_source_bundle_invalid")
        if not any(path.is_symlink() or path.exists() for path in (self.source_repo, self.source_lock)):
            # This Host has no coding source. The image still deploys as before.
            record["source_sync"] = "unavailable"
            return
        bundle = self.root / ("source-" + record["candidate_digest"] + ".bundle")
        try:
            if not self._extract_bundle(record["manifest"]["image_id"], bundle):
                # This Host already develops from a coding source. An image with
                # no bundle would leave that source on the old commit, so refuse.
                raise UpdateError("update_source_bundle_missing")
            self._accept_bundle(record, bundle)
        finally:
            self._discard(bundle)

    def _accept_bundle(self, record, bundle):
        source_commit = record["manifest"]["source_commit"]
        ref = "refs/agentic/candidate/" + record["manifest"]["image_id"].removeprefix("sha256:")
        self._verify_bundle(bundle, source_commit)
        self._require_repo()
        before = self._rev("HEAD")
        lock = self._read_lock()
        if lock["commit"] != before:
            raise UpdateError("update_source_unavailable")
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=self.root) as temporary:
            staging = Path(temporary) / "repo"
            self._command("git", "clone", "--bare", "--no-hardlinks", str(bundle), str(staging), timeout=SOURCE_GIT_TIMEOUT)
            if self._rev("HEAD", repo=staging) != source_commit:
                raise UpdateError("update_source_bundle_invalid")
            self._git("fsck", "--full", repo=staging)
            # Local path only. The + applies to the candidate ref, never to HEAD.
            if self._git("fetch", "--no-tags", str(staging), "+HEAD:" + ref, check=False).returncode:
                raise UpdateError("update_source_bundle_invalid")
        if self._rev("HEAD") != before:
            raise UpdateError("update_source_diverged")
        if self._rev(ref) != source_commit:
            raise UpdateError("update_source_bundle_invalid")
        self._git("fsck", "--full")
        record.update(source_sync="pending", source_before=before, source_candidate=source_commit, source_ref=ref,
                      source_lock_before=lock)

    def _moved_lock(self, record):
        install = record["source_lock_before"].get("install_commit") or record["source_before"]
        return {"repo_id": "agentic-home", "commit": record["source_candidate"], "install_commit": install,
                "previous_commit": record["source_before"], "image_id": record["manifest"]["image_id"],
                "candidate_digest": record["candidate_digest"]}

    def align_source(self, record, image, *, idle=None):
        """Make coding HEAD and source.json match the image that runs next.

        The caller has stopped the Controller, so no coding job can bind a
        commit. ``idle`` proves no job is active before any change. HEAD moves
        by compare-and-swap and source.json is replaced atomically, so a rerun
        after an interruption at any point finishes the same move. An unknown
        HEAD or source.json is never overwritten.
        """
        sync = record.get("source_sync")
        if sync in {None, "unavailable"}:
            record["source_sync"] = "unavailable"
            return
        before, candidate, previous = record.get("source_before"), record.get("source_candidate"), record.get("source_lock_before")
        if (not all(isinstance(value, str) and SHA40.fullmatch(value) for value in (before, candidate))
                or not isinstance(previous, dict) or candidate != record["manifest"]["source_commit"]
                or record.get("source_ref") != "refs/agentic/candidate/" + record["manifest"]["image_id"].removeprefix("sha256:")):
            raise UpdateError("update_source_diverged")
        moved = self._moved_lock(record)
        forward = image == record["manifest"]["image_id"]
        commit, lock = (candidate, moved) if forward else (before, previous)
        head, current = self._rev("HEAD"), self._lock_or_none()
        if head not in {before, candidate} or current not in (previous, moved):
            raise UpdateError("update_source_diverged")
        if head != commit or current != lock:
            if idle:
                idle()
            # update-ref refuses a missing object and an unexpected old value.
            if head != commit and self._git("update-ref", "HEAD", commit, head, check=False).returncode and self._rev("HEAD") == head:
                raise UpdateError("update_source_move_failed")
            if current != lock:
                try:
                    atomic(self.source_lock, lock)
                except OSError:
                    raise UpdateError("update_source_move_failed") from None
            if self._rev("HEAD") != commit or self._lock_or_none() != lock:
                raise UpdateError("update_source_diverged")
        record["source_sync"] = "moved" if forward else ("pending" if sync == "pending" else "reverted")

    def compose(self, *args, **kwargs):
        if not self.environment.exists():
            self.environment.touch(mode=0o600)
        return self.run("docker", "compose", "--env-file", str(self.environment), "-f", str(self.manager.product / "host/compose.yaml"), *args, **kwargs)

    def image(self):
        identifier = self.compose("ps", "-a", "-q", "controller").stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            raise UpdateError("update_controller_unavailable")
        image = self.run("docker", "inspect", "--format", "{{.Image}}", identifier).stdout.strip()
        if not IMAGE.fullmatch(image):
            raise UpdateError("update_current_image_invalid")
        return image

    def stop(self):
        self.compose("stop", "controller", timeout=90)
        if self.compose("ps", "--status", "running", "-q", "controller").stdout.strip():
            raise UpdateError("update_controller_stop_unconfirmed")
        identifier = self.compose("ps", "-a", "-q", "controller").stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            raise UpdateError("update_controller_stop_unconfirmed")
        state = json.loads(self.run("docker", "inspect", "--format", "{{json .State}}", identifier).stdout)
        if state.get("Running") or state.get("Paused"):
            raise UpdateError("update_controller_stop_unconfirmed")

    def activate(self, image):
        write_environment(self.environment, image, "AGENTIC_CONTROLLER_IMAGE")
        self.compose("up", "-d", "--no-deps", "controller", timeout=120)

    def health(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        origin = "http://localhost:8099"
        def get(path, body=None):
            request = urllib.request.Request(origin + path, data=None if body is None else canonical(body),
                                             headers={"Content-Type": "application/json", "Origin": origin})
            with opener.open(request, timeout=25) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise UpdateError("update_health_invalid")
            return json.loads(raw)
        key = (self.data / "access-key").read_text().strip()
        last = None
        for _ in range(20):
            try:
                health = get("/health")
                if health.get("status") != "ok" or get("/api/host/login", {"key": key}).get("authenticated") is not True:
                    raise UpdateError("update_health_invalid")
                session, state = get("/api/session"), get("/api/status")
                if session.get("mode") != "host" or session.get("version") != health.get("version"):
                    raise UpdateError("update_health_invalid")
                return {"version": health["version"], "ha": state["ha"]["status"], "ha_version": state["ha"].get("version"),
                        "runner": state["runner"].get("status", "paired"), "paused": state["paused"]}
            except Exception as exc:
                last = exc
                time.sleep(1)
        raise UpdateError("update_health_failed") from last

    def backup(self, destination):
        destination.touch(mode=0o600, exist_ok=False)
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        sync_path(destination)
        sync_path(destination.parent)

    def probe(self, image, schema):
        # A fresh structural database validates opening without leaking Live data.
        # Signed manifest additionally asserts semantic compatibility; no schema migration is allowed.
        ready(self.run, 768)
        identity = secrets.token_hex(16)
        volume = DiskVolume(self.root / "probes", identity, 64)
        name = "agentic-update-probe-" + identity
        atomic(self.probe_path, {"identity": identity, "container": name})
        try:
            directory = volume.create()
            path = directory / "controller.sqlite3"
            with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(path)) as target:
                # sqlite's transaction context commits but does not close its
                # descriptor. Close both before the quota volume is unmounted.
                with target:
                    for row in source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name IN ('settings','records','requests','events')"):
                        target.execute(row[0])
                    target.execute(f"PRAGMA user_version={int(schema)}")
                    target.executemany("INSERT INTO settings VALUES (?,?)", [("paused", "true"), ("retention", '{"context_days":7}')])
                    # A revoked bridge still leaves authority markers in case/job
                    # history. Same SQLite schema does not imply an older binary
                    # understands those semantics. Copy only the required major,
                    # never pairing credentials, Owner identity or maintenance data.
                    if source.execute("SELECT 1 FROM settings WHERE key='ingress_bridge'").fetchone():
                        target.execute("INSERT INTO settings VALUES ('required_ingress_bridge_contract',?)",
                                       (json.dumps(INGRESS_BRIDGE_CONTRACT),))
            os.chown(directory, 1000, 1000)
            os.chown(path, 1000, 1000)
            self.run("docker", "run", "--name", name, "--label", "agentic.update-probe=" + name, "--no-healthcheck", "--network", "none", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges:true", "--user", "1000:1000", "--memory", "256m", "--memory-swap", "256m",
                     "--pids-limit", "32", "--cpus", "1", "--log-driver", "none",
                     "--mount", f"type=bind,src={directory},dst=/probe",
                     "--mount", f"type=bind,src={self.manager.product / 'host/agentic_host/update_probe.py'},dst=/fixed-probe.py,readonly",
                     "--entrypoint", "timeout", image, "--signal=TERM", "--kill-after=3", "40", "python", "/fixed-probe.py", timeout=45)
        finally:
            fence(self.run, name, "agentic.update-probe")
            volume.close()
            self.probe_path.unlink()

    def reconcile_probe(self):
        if not self.probe_path.exists():
            return
        record = json.loads(self.probe_path.read_text())
        identity = record.get("identity", "")
        if not re.fullmatch(r"[a-f0-9]{32}", identity) or record.get("container") != "agentic-update-probe-" + identity:
            raise UpdateError("update_probe_identity_invalid")
        fence(self.run, record["container"], "agentic.update-probe")
        DiskVolume(self.root / "probes", identity, 64).close()
        self.probe_path.unlink()

    def offer_rollback(self, body):
        if not isinstance(body, dict) or set(body) != {"candidate_digest"}:
            raise UpdateError("update_request_invalid")
        with self.lock:
            record = self.records.get(body["candidate_digest"])
            if record is None or record["state"] not in {"deployed", "needs_reconcile"} or not record.get("previous_image"):
                raise UpdateError("update_rollback_unavailable")
            self.update(record, rollback_approval_id=secrets.token_urlsafe(32), rollback_expires_at=time.time() + 600)
            return self.public(record)

    def apply(self, body, rollback=False):
        if not isinstance(body, dict) or set(body) != {"candidate_digest", "approval_id"}:
            raise UpdateError("update_request_invalid")
        with self.lock:
            record = self.records.get(body["candidate_digest"])
            key, expiry = ("rollback_approval_id", "rollback_expires_at") if rollback else ("approval_id", "approval_expires_at")
            allowed = {"deployed", "needs_reconcile"} if rollback else {"staged"}
            if record is None or record["state"] not in allowed or not isinstance(body["approval_id"], str) or not secrets.compare_digest(record.get(key, ""), body["approval_id"]) or record.get(expiry, 0) <= time.time():
                raise UpdateError("update_approval_invalid")
            if any(other is not record and other["state"] in {"staging", "deploying", "rolling_back", "needs_reconcile"} for other in self.records.values()):
                raise UpdateError("update_busy_or_unresolved")
            self.verify(record["manifest"], record["signature"], rollback=rollback)
            self.reconcile_probe()
            record.pop(key, None)
            self.update(record, state="rolling_back" if rollback else "deploying")
        threading.Thread(target=self.deploy, args=(record, rollback), daemon=True).start()
        return self.public(record)

    def deploy(self, record, rollback=False):
        try:
            with self.manager.locked():
                self._deploy_locked(record, rollback)
        except Exception:
            self.update(record, state="failed", error_code="update_host_operation_conflict")

    def _deploy_locked(self, record, rollback=False):
        stopped, switched, gated, previous = False, False, False, None
        schema = record["manifest"]["compatibility"]["schema"]
        try:
            previous = self.image()
            if not rollback and previous != record.get("base_image"):
                raise UpdateError("update_base_revision_changed")
            try:
                baseline = self.health()
            except UpdateError:
                if not rollback:
                    raise
                baseline = record.get("health") or record.get("baseline")
                if not baseline:
                    raise UpdateError("update_rollback_baseline_unavailable") from None
            if not rollback and baseline["ha"] == "connected" and baseline["ha_version"] not in record["manifest"]["compatibility"]["ha_core_versions"]:
                raise UpdateError("update_ha_version_incompatible")
            if rollback and previous not in {record["manifest"]["image_id"], record["previous_image"]}:
                raise UpdateError("update_rollback_revision_changed")
            target = record["previous_image"] if rollback else record["manifest"]["image_id"]
            self.update(record, phase="stopping", baseline=baseline, **({"previous_image": previous} if not rollback else {}))
            stopped = True
            self.stop()
            offline_check(self.database, schema, pause=True, gate=record["candidate_digest"])
            gated = True
            snapshot = self.root / (record["candidate_digest"] + ("-rollback-" if rollback else "-") + secrets.token_hex(8) + ".sqlite3")
            self.backup(snapshot)
            self.update(record, phase="probing", snapshot=snapshot.name)
            self.probe(target, schema)
            self.update(record, phase="activating")
            # Persist intent before a compose call that might succeed remotely
            # and lose its response. Fallback must treat the candidate as used.
            switched = True
            self.activate(target)
            observed = self.health()
            if observed["paused"] is not True or observed["ha"] != baseline["ha"] or observed["runner"] != baseline["runner"] or (not rollback and observed["version"] != record["manifest"]["version"]):
                raise UpdateError("update_smoke_mismatch")
            # Verify actual active artifact independently of the candidate HTTP report.
            if self.image() != target:
                raise UpdateError("update_activation_mismatch")
            # Candidate has passed the fixed authenticated/read-only smoke.
            # Clear the maintenance gate only while it is again stopped, then
            # restart the same proven artifact. Household AI stays paused.
            self.stop()
            offline_check(self.database, schema)
            # Still stopped and idle: no coding job can bind a commit while the
            # coding source follows the artifact.
            self.align_source(record, target)
            clear_gate(self.database, record["candidate_digest"])
            self.activate(target)
            final = self.health()
            if final != observed or self.image() != target:
                raise UpdateError("update_final_smoke_mismatch")
            self.update(record, state="rolled_back" if rollback else "deployed", phase="complete", health=observed,
                        finished_at=time.time(), current_database_preserved=True, mutations_paused=True)
        except Exception as exc:
            code = str(exc) if isinstance(exc, UpdateError) else "update_deployment_failed"
            state, source = "failed", True
            if stopped and previous:
                try:
                    self.stop()
                    # Preserve all post-activation writes. A changed schema or
                    # uncertain job forbids automatic fallback, even on failure.
                    if switched:
                        offline_check(self.database, schema)
                        self.probe(previous, schema)
                    try:
                        self.align_source(record, previous, idle=lambda: offline_check(self.database, schema))
                    except Exception:
                        # The previous Controller still returns. A source that
                        # cannot be confirmed is left as found for reconciliation.
                        source = False
                    self.activate(previous)
                    self.health()
                    if gated:
                        self.stop()
                        offline_check(self.database, schema)
                        clear_gate(self.database, record["candidate_digest"])
                        self.activate(previous)
                        self.health()
                    state = "deployed" if rollback else ("rolled_back" if switched else "failed")
                except Exception:
                    state, code = "needs_reconcile", "update_fallback_unconfirmed"
            if not source or (state == "needs_reconcile" and record.get("source_sync") in {"pending", "moved", "reverted"}):
                state = "needs_reconcile"
                record["source_sync"] = "needs_reconcile"
            self.update(record, state=state, error_code=code, current_database_preserved=True)

    def status(self):
        self.expire_snapshots()
        try:
            trusted = bool(self.publishers())
        except UpdateError:
            trusted = False
        return {"contract": 1, "scope": "controller_only_same_schema", "trusted_publisher_configured": trusted,
                "runtime_digest": self.runtime_digest(), "deployed_source_commit": source_identity(self.coding)["source_commit"],
                "records": [self.public(r) for r in self.records.values()]}

    def handle(self, method, path, body=None):
        if method == "GET" and path == "/updates":
            return 200, self.status()
        if method == "GET" and path.startswith("/updates/"):
            record = self.records.get(path.removeprefix("/updates/"))
            return (200, self.public(record)) if record else (404, {"detail": "update_not_found"})
        if method == "POST":
            if path == "/updates/stage":
                return 202, self.stage(body)
            if path == "/updates/rollback-offer":
                return 200, self.offer_rollback(body)
            if path in {"/updates/apply", "/updates/rollback"}:
                return 202, self.apply(body, rollback=path.endswith("rollback"))
        raise UpdateError("update_request_invalid")
