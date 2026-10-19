"""Signed official Host bundles; the immutable external executor survives switching.

No manifest-defined commands, trust changes, package manager, DB restoration or
HAOS update. Candidate code executes only after exact owner approval and backup.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .manager import HostError, Manager, atomic, sync_path
from .publisher import public_key
from .updater import clear_gate, offline_check

PUBLISHER = "ted-h-com-agentic-smart-home"
REPOSITORY = "TED-H-com/agentic-smart-home"
IMAGE_REPOSITORY = "ghcr.io/ted-h-com/agentic-smart-home-controller"
COMPATIBILITY = {"host_protocol": 1, "controller_protocol": 1, "controller_schema": 3, "reconcile": 1}
CONTRACT = {"contract": 1, **COMPATIBILITY, "executor": 1}
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?\Z")
RELEASE = "https://github.com/TED-H-com/agentic-smart-home/releases/"
LATEST = RELEASE + "latest/download/agentic-home-host-update.json"
MAX_ARCHIVE = 128 * 1024**2
MAX_EXPANDED = 256 * 1024**2
MAX_FILE = 32 * 1024**2
MAX_FILES = 10000
AUTHORITY = Path("/usr/lib/agentic-home-update/current/official-update-channel.json")
TRUST = Path("/etc/agentic-home/update-publishers.json")
ENGINE = Path("/usr/lib/agentic-home-update/current")
BUNDLES = Path("/opt/agentic-home-bundles")
BUSY = {"queued", "activating", "rolling_back", "needs_reconcile"}
STATES = BUSY | {"staging", "staged", "deployed", "rolled_back", "failed"}
REQUIRED = {"host/agentic_host/__init__.py", "host/agentic_host/daemon.py", "host/agentic_host/host_reconcile.py",
            "host/compose.yaml", "host/host-update-contract.json", "images.lock.json", "pyproject.toml", "providers.lock.json"}
LOCKS = ("images.lock.json", "providers.lock.json", "host/bases.lock.json", "host/network.lock.json")
TOPS = {"host", "controller", "runner", "hermes", "bridge", "custom_components", "web", "contracts", "scripts"}
TOP_FILES = {"pyproject.toml", "uv.lock", "providers.lock.json", "images.lock.json", "BUNDLE.sha256.json",
             "AGENTS.md", "DESIGN.md", "README.md", "PROJECT_PLAN.md", "PROJECT_SPEC.md"}


class HostUpdateError(HostError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def secure(path, *, optional=False, symlink=False):
    """All writable installed authority/state paths belong exclusively to root."""
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink():
            if item == path and symlink:
                continue
            # Only the pinned external engine's protected current link is allowed.
            if item == ENGINE:
                secure(item.resolve())
                continue
            raise HostUpdateError("host_update_path_untrusted")
        if not item.exists():
            if item == path and not optional:
                raise HostUpdateError("host_update_path_missing")
            continue
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise HostUpdateError("host_update_path_untrusted")


def read_json(path, maximum=131072):
    secure(path)
    if not Path(path).is_file() or Path(path).stat().st_size > maximum:
        raise HostUpdateError("host_update_record_invalid")
    return json.loads(Path(path).read_text())


def exact_protocol(value, expected=COMPATIBILITY):
    return (isinstance(value, dict) and set(value) == set(expected)
            and all(type(value[key]) is int and value[key] == wanted for key, wanted in expected.items()))


def validate_manifest(value, *, now=None):
    fields = {"format", "kind", "publisher", "version", "source_commit", "bundle", "compatibility", "issued_at", "expires_at", "tests"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["format"]) is not int or value["format"] != 1
            or value["kind"] != "agentic-home-host-bundle" or value["publisher"] != PUBLISHER
            or not isinstance(value["version"], str) or not VERSION.fullmatch(value["version"])
            or not isinstance(value["source_commit"], str) or not re.fullmatch(r"[a-f0-9]{40}", value["source_commit"])):
        raise HostUpdateError("host_update_manifest_invalid")
    compatibility = value["compatibility"]
    if (not isinstance(compatibility, dict) or set(compatibility) != set(COMPATIBILITY)
            or any(type(compatibility[key]) is not int or compatibility[key] != expected for key, expected in COMPATIBILITY.items())):
        raise HostUpdateError("host_update_protocol_unsupported")
    bundle = value["bundle"]
    expected_url = RELEASE + "download/v" + value["version"] + "/agentic-home-host-" + value["version"] + ".tar"
    if (not isinstance(bundle, dict) or set(bundle) != {"url", "sha256", "size"} or bundle["url"] != expected_url
            or not isinstance(bundle["sha256"], str) or not DIGEST.fullmatch(bundle["sha256"])
            or type(bundle["size"]) is not int or not 1024 <= bundle["size"] <= MAX_ARCHIVE):
        raise HostUpdateError("host_update_bundle_invalid")
    now = time.time() if now is None else now
    if (any(type(value[key]) not in {int, float} or not math.isfinite(value[key]) for key in ("issued_at", "expires_at"))
            or not value["issued_at"] <= now < value["expires_at"] <= value["issued_at"] + 30 * 86400):
        raise HostUpdateError("host_update_manifest_expired")
    tests = value["tests"]
    if not isinstance(tests, list) or not 1 <= len(tests) <= 20:
        raise HostUpdateError("host_update_evidence_required")
    for row in tests:
        if (not isinstance(row, dict) or set(row) != {"name", "result", "sha256"} or row["result"] != "passed"
                or not isinstance(row["name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", row["name"])
                or not isinstance(row["sha256"], str) or not DIGEST.fullmatch(row["sha256"])):
            raise HostUpdateError("host_update_evidence_invalid")
    return value


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        parsed = urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
                or parsed.fragment or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com"}
                or (parsed.hostname == "github.com" and not newurl.startswith(RELEASE + "download/"))):
            raise HostUpdateError("host_update_redirect_forbidden")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, target, maximum):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), OfficialRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "Agentic-Home-Host-Update/1"})
    with opener.open(request, timeout=30) as response, Path(target).open("xb") as stream:
        total = 0
        while chunk := response.read(min(1024 * 1024, maximum + 1 - total)):
            total += len(chunk)
            if total > maximum:
                raise HostUpdateError("host_update_download_too_large")
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())


class HostBundle:
    def __init__(self, manager, *, active=None, bundles=None, authority=None, trust=None, restore=None,
                 download=None, broker_probe=None, controller_probe=None, launch=None, reconcile=None):
        self.manager, self.run = manager, manager.run
        self.active = Path(active) if active is not None else manager.product
        self.bundles = Path(bundles) if bundles is not None else BUNDLES
        self.authority = Path(authority) if authority is not None else AUTHORITY
        self.trust = Path(trust) if trust is not None else TRUST
        self.root = manager.root / "host-updates"
        secure(self.root, optional=True)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root / "records.json"
        self.check_path = self.root / "check.json"
        self.thread_lock = threading.RLock()
        self.fetch = download or globals()["download"]
        self.restore, self.reconcile = restore, reconcile
        self.broker_probe = broker_probe or self._broker_probe
        self.controller_probe = controller_probe or self._controller_probe
        self.launch = launch or self._launch
        self.stage_threads = {}
        self.check_thread = None

    @contextmanager
    def locked(self):
        with self.thread_lock:
            path = self.root / "operation.lock"
            secure(path, optional=True)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HostUpdateError("host_update_operation_busy") from None
                yield

    def records(self):
        value = read_json(self.path, 4 * 1024**2) if self.path.exists() else {}
        if not isinstance(value, dict) or len(value) > 100:
            raise HostUpdateError("host_update_record_invalid")
        for key, record in value.items():
            if not DIGEST.fullmatch(key) or not isinstance(record, dict) or record.get("state") not in STATES:
                raise HostUpdateError("host_update_record_invalid")
        return value

    def save(self, record):
        records = self.records()
        record["updated_at"] = time.time()
        records[record["candidate_digest"]] = record
        secure(self.path.with_name(self.path.name + ".new"), optional=True)
        atomic(self.path, records)

    def authority_pin(self):
        value = read_json(self.authority, 16384)
        fields = {"format", "publisher", "repository", "channel", "image_repository", "pin"}
        if (not isinstance(value, dict) or set(value) != fields or type(value["format"]) is not int or value["format"] != 1
                or value["publisher"] != PUBLISHER or value["repository"] != REPOSITORY or value["channel"] != "stable"):
            raise HostUpdateError("host_update_official_pin_invalid")
        pin = value["pin"]
        if pin is None:
            raise HostUpdateError("host_update_official_pin_missing")
        if (not isinstance(pin, dict) or set(pin) != {"public_key_pem", "key_sha256"}
                or value["image_repository"] != IMAGE_REPOSITORY):
            raise HostUpdateError("host_update_official_pin_invalid")
        pem, fingerprint = public_key(pin["public_key_pem"])
        if pin["key_sha256"] != fingerprint:
            raise HostUpdateError("host_update_official_pin_invalid")
        trust = read_json(self.trust)
        if (not isinstance(trust, dict) or set(trust) != {"format", "publishers"} or type(trust["format"]) is not int
                or trust["format"] != 1 or not isinstance(trust["publishers"], dict)):
            raise HostUpdateError("host_update_trust_invalid")
        entry = trust["publishers"].get(PUBLISHER)
        if (not isinstance(entry, dict) or set(entry) != {"public_key_pem", "image_repository"}
                or entry["image_repository"] != IMAGE_REPOSITORY or public_key(entry["public_key_pem"])[1] != fingerprint):
            raise HostUpdateError("host_update_official_publisher_not_trusted")
        return pem, fingerprint

    def verify_envelope(self, body, *, rollback=False):
        if not isinstance(body, dict) or set(body) != {"manifest", "signature"}:
            raise HostUpdateError("host_update_envelope_invalid")
        manifest = body["manifest"]
        validate_manifest(manifest, now=manifest.get("issued_at") if rollback and isinstance(manifest, dict) else None)
        pem, fingerprint = self.authority_pin()
        try:
            signature = base64.b64decode(body["signature"], validate=True)
        except (ValueError, TypeError):
            raise HostUpdateError("host_update_signature_invalid") from None
        if len(signature) != 64:
            raise HostUpdateError("host_update_signature_invalid")
        with tempfile.TemporaryDirectory(prefix="signature-", dir=self.root) as temporary:
            directory = Path(temporary)
            (directory / "key.pem").write_text(pem)
            (directory / "manifest").write_bytes(canonical(manifest))
            (directory / "signature").write_bytes(signature)
            result = self.run("openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(directory / "key.pem"),
                              "-rawin", "-in", str(directory / "manifest"), "-sigfile", str(directory / "signature"), check=False, timeout=10)
            if result.returncode:
                raise HostUpdateError("host_update_signature_invalid")
        return digest(manifest), fingerprint

    def current(self):
        secure(self.active.parent)
        if not self.active.is_symlink():
            raise HostUpdateError("host_update_bootstrap_required")
        target = self.active.resolve(strict=True)
        secure(self.bundles)
        secure(target)
        if target.parent != self.bundles.resolve() or not target.is_dir():
            raise HostUpdateError("host_update_product_not_owned")
        return target

    @staticmethod
    def public(record):
        fields = {"candidate_digest", "manifest", "state", "created_at", "updated_at", "message", "error_code", "auto_rollback",
                  "approval_id", "approval_expires_at", "rollback_approval_id", "rollback_expires_at"}
        result = {key: value for key, value in record.items() if key in fields}
        receipt = record.get("restore_point")
        if isinstance(receipt, dict):
            result["restore_point"] = {key: receipt.get(key) for key in ("scope", "ha_snapshot", "root_snapshot_name")}
            result["restore_point"]["state"] = "ready" if receipt.get("phase") == "complete" else receipt.get("state", receipt.get("phase"))
            root_snapshot = receipt.get("root_snapshot")
            if isinstance(root_snapshot, dict):
                result["restore_point"]["root_snapshot_name"] = root_snapshot.get("name")
        return result

    def status(self):
        code, available = None, True
        try:
            self.authority_pin()
            self.current()
        except (HostError, OSError, ValueError, TypeError, KeyError):
            code, available = "host_update_bootstrap_or_official_pin_required", False
        records = self.records()
        current_source = None
        try:
            target = self.current().name
            current_source = records.get(target, {}).get("manifest", {}).get("source_commit")
        except (HostError, OSError):
            pass
        check = read_json(self.check_path) if self.check_path.exists() else {
            "state": "idle", "code": None, "message": "尚未檢查主機更新。", "candidate_digest": None, "checked_at": None}
        return {"contract": 1, "scope": "host_bundle_same_schema", "compatibility": dict(COMPATIBILITY),
                "available": available, "code": code,
                "message": "可檢查主機更新；安裝前會請您核准並自動建立還原點。" if available else "此主機尚未備妥受信任的更新元件或官方金鑰；請使用經驗證的安裝媒體準備更新。",
                "current_source_commit": current_source, "check": check,
                "records": [self.public(row) for row in sorted(records.values(), key=lambda row: row["created_at"], reverse=True)[:50]]}

    def recover_preparations(self):
        """Broker startup only: never execute or activate a crashed preparation."""
        with self.locked():
            for record in self.records().values():
                if record["state"] == "staging":
                    record.update(state="failed", error_code="host_update_staging_interrupted",
                                  message="更新準備中斷，主機維持原狀；請重新檢查更新。")
                    self.save(record)
            if self.check_path.exists():
                check = read_json(self.check_path)
                if check.get("state") == "checking":
                    atomic(self.check_path, {"state": "failed", "code": "host_update_check_interrupted",
                                            "message": "更新檢查中斷，請重試。", "candidate_digest": None, "checked_at": time.time()})

    def require_no_controller_update(self):
        path = self.manager.root / "updates/records.json"
        if not path.exists():
            return
        records = read_json(path, 4 * 1024**2)
        if not isinstance(records, dict) or any(not isinstance(row, dict) for row in records.values()):
            raise HostUpdateError("host_update_controller_state_uncertain")
        if any(row.get("state") in {"staging", "deploying", "rolling_back", "needs_reconcile"} for row in records.values()):
            raise HostUpdateError("host_update_controller_update_active")

    def busy(self):
        return any(record["state"] in BUSY for record in self.records().values())

    def guard(self, path, body):
        if path.startswith("/host-updates/") or (path == "/actions" and isinstance(body, dict) and body.get("action") == "shutdown"):
            return
        if path == "/coding/fence" or (path.startswith("/coding/jobs/") and path.endswith("/cancel")):
            return
        if not self.busy() and not any(row["state"] == "staging" for row in self.records().values()):
            return
        raise HostUpdateError("host_update_pending_recovery")

    def check(self, body):
        if body != {}:
            raise HostUpdateError("host_update_request_invalid")
        self.authority_pin()
        self.current()
        with self.locked():
            if self.busy():
                raise HostUpdateError("host_update_pending_recovery")
            current = read_json(self.check_path) if self.check_path.exists() else {}
            if current.get("state") == "checking" and self.check_thread is not None and self.check_thread.is_alive():
                return self.status()
            atomic(self.check_path, {"state": "checking", "code": None, "message": "正在檢查並驗證官方主機更新。", "candidate_digest": None, "checked_at": time.time()})
        self.check_thread = threading.Thread(target=self._check, daemon=True, name="host-update-check")
        self.check_thread.start()
        return self.status()

    def _check(self):
        try:
            with tempfile.TemporaryDirectory(prefix="check-", dir=self.root) as temporary:
                target = Path(temporary) / "envelope.json"
                self.fetch(LATEST, target, 131072)
                body = json.loads(target.read_text())
                record = self.stage(body, background=False)
            result = {"state": "available", "code": None, "message": "主機更新已驗證完成，等待您核准安裝。", "candidate_digest": record["candidate_digest"]}
        except urllib.error.HTTPError as exc:
            result = {"state": "unavailable" if exc.code == 404 else "failed", "code": "host_update_release_unavailable", "message": "目前尚未發布可用的官方主機更新。", "candidate_digest": None}
        except Exception:
            result = {"state": "failed", "code": "host_update_check_failed", "message": "暫時無法安全確認更新，尚未變更主機；請稍後重試。", "candidate_digest": None}
        atomic(self.check_path, {**result, "checked_at": time.time()})

    def stage(self, body, *, background=True):
        candidate, fingerprint = self.verify_envelope(body)
        self.current()
        with self.locked():
            if self.busy():
                raise HostUpdateError("host_update_pending_recovery")
            existing = self.records().get(candidate)
            if existing and existing["state"] == "staged":
                self._validate_tree(self.bundles / candidate, body["manifest"])
                if str(self.current()) != existing["previous"] or self._tree_identity(self.current()) != existing["previous_tree_digest"]:
                    raise HostUpdateError("host_update_product_changed")
                existing.update(approval_id=secrets.token_urlsafe(32), approval_expires_at=time.time() + 600)
                self.save(existing)
                return self.public(existing)
            if existing and existing["state"] == "staging":
                worker = self.stage_threads.get(candidate)
                if worker is not None and worker.is_alive():
                    return self.public(existing)
            if existing and existing["state"] in {"deployed", "rolled_back"}:
                return self.public(existing)
            record = {"candidate_digest": candidate, "manifest": body["manifest"], "signature": body["signature"],
                      "key_sha256": fingerprint, "previous": str(self.current()), "previous_tree_digest": self._tree_identity(self.current()),
                      "state": "staging", "created_at": time.time(), "updated_at": time.time(),
                      "message": "正在下載並驗證主機更新；尚未執行更新程式。", "error_code": None, "auto_rollback": False}
            self.save(record)
        if background:
            worker = threading.Thread(target=self._stage, args=(candidate,), daemon=True, name="host-update-stage")
            self.stage_threads[candidate] = worker
            worker.start()
        else:
            self._stage(candidate)
        return self.public(self.records()[candidate])

    def _stage(self, candidate):
        with self.locked():
            record = self.records()[candidate]
            if record["state"] != "staging":
                return
            try:
                with self.manager.locked():
                    self._stage_copy(record)
            except Exception:
                record.update(state="failed", error_code="host_update_host_busy",
                              message="主機正在進行其他維護，尚未準備更新；請稍後重試。")
            self.save(record)

    def _stage_copy(self, record):
        candidate = record["candidate_digest"]
        phase = "capacity"
        try:
            capacity = self.restore
            if capacity is None:
                from .host_restore import HostRestore
                capacity = HostRestore(self.manager)
            capacity.capacity(MAX_EXPANDED + record["manifest"]["bundle"]["size"] + 64 * 1024**2)
            target = self.bundles / candidate
            if target.exists():
                self._validate_tree(target, record["manifest"])
            else:
                partial = self.bundles / (candidate + ".partial")
                if partial.exists() or partial.is_symlink():
                    secure(partial)
                    if self.current() == partial:
                        raise HostUpdateError("host_update_product_changed")
                    shutil.rmtree(partial)  # only an inactive, owned extraction scratch tree
                phase = "download"
                with tempfile.TemporaryDirectory(prefix="bundle-", dir=self.root) as temporary:
                    archive = Path(temporary) / "product.tar"
                    bundle = record["manifest"]["bundle"]
                    self.fetch(bundle["url"], archive, bundle["size"])
                    if archive.stat().st_size != bundle["size"] or hashlib.sha256(archive.read_bytes()).hexdigest() != bundle["sha256"]:
                        raise HostUpdateError("host_update_bundle_digest_mismatch")
                    phase = "archive"
                    self._extract(archive, record)
            self.verify_envelope({"manifest": record["manifest"], "signature": record["signature"]})
            record.update(state="staged", message="主機更新已驗證；核准後才會建立還原點並安裝。",
                          approval_id=secrets.token_urlsafe(32), approval_expires_at=time.time() + 600)
        except Exception:
            code, message = "host_update_stage_failed", "更新檔案未通過安全檢查，尚未變更主機。"
            if phase == "capacity":
                code, message = "host_update_capacity_unavailable", "磁碟還原點空間不足或無法確認，原有還原點已保留；尚未安裝更新。"
            elif phase == "download":
                code, message = "host_update_download_failed", "暫時無法下載官方更新，主機未變更；請檢查網路後重試。"
            record.update(state="failed", message=message, error_code=code)
        finally:
            if "capacity" in locals():
                try:
                    capacity.capacity(0)
                except Exception:
                    record.update(state="failed", message="磁碟還原點空間尚未確認，尚未變更主機。", error_code="host_update_storage_unconfirmed")

    def _extract(self, archive, record):
        self.bundles.mkdir(mode=0o755, parents=True, exist_ok=True)
        secure(self.bundles)
        target = self.bundles / record["candidate_digest"]
        if target.exists() or target.is_symlink():
            raise HostUpdateError("host_update_candidate_already_exists")
        staging = self.bundles / (record["candidate_digest"] + ".partial")
        if staging.exists() or staging.is_symlink():
            raise HostUpdateError("host_update_partial_needs_review")
        staging.mkdir(mode=0o700)
        try:
            names, files, expanded = set(), set(), 0
            with tarfile.open(archive, "r:") as stream:
                for member in stream:
                    name = member.name.rstrip("/")
                    path = PurePosixPath(name)
                    parts = name.split("/")
                    if (not name or len(name) > 240 or len(parts) > 16 or path.is_absolute() or any(part in {"", ".", ".."} for part in parts)
                            or any(not re.fullmatch(r"[A-Za-z0-9_.@+-]{1,128}", part) for part in parts)
                            or name in names or "__pycache__" in parts or name.endswith(".pyc")
                            or len(names) >= MAX_FILES or member.pax_headers or member.mode & 0o7000
                            or not (member.isdir() or member.isfile()) or member.sparse
                            or parts[0] not in TOPS | TOP_FILES or (parts[0] in TOP_FILES and len(parts) != 1)
                            or member.size > MAX_FILE or member.size < 0):
                        raise HostUpdateError("host_update_archive_unsafe")
                    names.add(name)
                    expanded += member.size
                    if expanded > MAX_EXPANDED:
                        raise HostUpdateError("host_update_archive_too_large")
                    destination = staging.joinpath(*parts)
                    if member.isdir():
                        destination.mkdir(mode=0o755, parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                        with stream.extractfile(member) as source, destination.open("xb") as output:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
                        destination.chmod(0o755 if member.mode & 0o111 else 0o644)
                        files.add(name)
            if not REQUIRED.issubset(files):
                raise HostUpdateError("host_update_bundle_incomplete")
            self._validate_tree(staging, record["manifest"])
            asset_link = self.current() / "assets"
            secure(asset_link, symlink=True)
            assets = asset_link.resolve(strict=True)
            secure(assets)
            if not assets.is_dir():
                raise HostUpdateError("host_update_baseline_assets_missing")
            (staging / "assets").symlink_to(assets, target_is_directory=True)
            staging.chmod(0o755)
            sync_path(staging)
            os.rename(staging, target)
            sync_path(self.bundles)
        except BaseException:
            shutil.rmtree(staging)
            raise

    def _validate_tree(self, target, manifest):
        secure(target)
        index = read_json(target / "BUNDLE.sha256.json", 2 * 1024**2)
        if not isinstance(index, dict) or not REQUIRED.issubset(index) or len(index) > MAX_FILES:
            raise HostUpdateError("host_update_bundle_index_invalid")
        actual = {str(path.relative_to(target)) for path in target.rglob("*")
                  if path.is_file() and not path.is_symlink() and "__pycache__" not in path.relative_to(target).parts
                  and path.suffix != ".pyc"}
        if actual != set(index) | {"BUNDLE.sha256.json"}:
            raise HostUpdateError("host_update_bundle_index_invalid")
        for name, expected in index.items():
            if (not isinstance(name, str) or not isinstance(expected, str) or not DIGEST.fullmatch(expected)
                    or name.startswith("/") or ".." in name.split("/") or (target / name).is_symlink()
                    or hashlib.sha256((target / name).read_bytes()).hexdigest() != expected):
                raise HostUpdateError("host_update_bundle_digest_mismatch")
        for path in target.rglob("*"):
            if path.is_symlink() and path != target / "assets":
                raise HostUpdateError("host_update_archive_unsafe")
        contract = read_json(target / "host/host-update-contract.json")
        if not exact_protocol(contract, CONTRACT):
            raise HostUpdateError("host_update_protocol_unsupported")
        for name in LOCKS:
            before, after = self.current() / name, target / name
            if before.exists() != after.exists() or (before.exists() and before.read_bytes() != after.read_bytes()):
                raise HostUpdateError("host_update_runtime_change_not_supported")
        descriptor = target / "host/official-update-channel.json"
        if descriptor.exists() and read_json(descriptor, 16384) != read_json(self.authority, 16384):
            raise HostUpdateError("host_update_key_rotation_not_supported")

    def _tree_identity(self, target):
        secure(target)
        entries, size = {}, 0
        for folder, directories, files in os.walk(target, followlinks=False):
            base = Path(folder)
            if base == target and "assets" in directories:
                directories.remove("assets")
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in directories:
                if (base / name).is_symlink():
                    raise HostUpdateError("host_update_product_changed")
            for name in sorted(files):
                if name.endswith(".pyc"):
                    continue
                path = base / name
                if path.is_symlink() or not path.is_file():
                    raise HostUpdateError("host_update_product_changed")
                size += path.stat().st_size
                if size > MAX_EXPANDED or len(entries) >= MAX_FILES:
                    raise HostUpdateError("host_update_product_changed")
                entries[str(path.relative_to(target))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest(entries)

    def _approval(self, body, *, rollback=False):
        if (not isinstance(body, dict) or set(body) != {"candidate_digest", "approval_id"}
                or not isinstance(body["candidate_digest"], str) or not DIGEST.fullmatch(body["candidate_digest"])
                or not isinstance(body["approval_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", body["approval_id"])):
            raise HostUpdateError("host_update_approval_invalid")
        candidate = body["candidate_digest"]
        operation = "rollback" if rollback else "apply"
        with self.locked():
            record = self.records().get(candidate)
            if not record:
                raise HostUpdateError("host_update_not_found")
            token_hash = hashlib.sha256(body["approval_id"].encode()).hexdigest()
            if record.get("consumed_token_sha256") == token_hash and record.get("consumed_action", record.get("operation")) == operation:
                return self.public(record)  # lost replies do not activate a second time
            token, expiry = ("rollback_approval_id", "rollback_expires_at") if rollback else ("approval_id", "approval_expires_at")
            recovery_only = rollback and record["state"] == "needs_reconcile"
            others_busy = any(row["state"] in BUSY and row["candidate_digest"] != candidate for row in self.records().values())
            allowed = {"deployed", "needs_reconcile"} if rollback else {"staged"}
            if (others_busy or (self.busy() and not recovery_only) or record["state"] not in allowed
                    or not secrets.compare_digest(record.get(token, ""), body["approval_id"])
                    or record.get(expiry, 0) <= time.time()):
                raise HostUpdateError("host_update_approval_invalid")
            self.require_no_controller_update()
            current = self.current()
            if rollback:
                if (current not in {self.bundles / candidate, Path(record["previous"])}
                        or (not recovery_only and current != self.bundles / candidate)
                        or self._tree_identity(Path(record["previous"])) != record["previous_tree_digest"]):
                    raise HostUpdateError("host_update_product_changed")
                if record.get("rollback_current_digest") != self._tree_identity(current):
                    raise HostUpdateError("host_update_product_changed")
            else:
                _, fingerprint = self.verify_envelope({"manifest": record["manifest"], "signature": record["signature"]})
                if fingerprint != record["key_sha256"]:
                    raise HostUpdateError("host_update_official_pin_changed")
                self._validate_tree(self.bundles / candidate, record["manifest"])
                if str(current) != record["previous"] or self._tree_identity(current) != record["previous_tree_digest"]:
                    raise HostUpdateError("host_update_product_changed")
            if rollback and not recovery_only and record.get("restore_point"):
                record.setdefault("prior_restore_points", []).append(record["restore_point"])
                record.pop("restore_point", None)
                record.pop("restore_attempt", None)
            previous_phase = record.get("phase")
            record.update(state="queued", operation=record.get("operation", operation) if recovery_only else operation,
                          consumed_action=operation, recovery_only=recovery_only, recovery_phase=previous_phase if recovery_only else None,
                          phase="approved", consumed_token_sha256=token_hash,
                          message="已核准；正在準備還原點與主機維護。", error_code=None)
            record.pop(token, None)
            record.pop(expiry, None)
            self.save(record)
        try:
            self.launch()
        except Exception:
            # The request was durable before launch. Never guess whether systemd
            # started it or overwrite a newer executor result after a lost reply.
            pass
        return self.public(self.records()[candidate])

    def rollback_offer(self, body):
        if not isinstance(body, dict) or set(body) != {"candidate_digest"} or not isinstance(body["candidate_digest"], str) or not DIGEST.fullmatch(body["candidate_digest"]):
            raise HostUpdateError("host_update_request_invalid")
        with self.locked():
            record = self.records().get(body["candidate_digest"])
            if (not record or record["state"] not in {"deployed", "needs_reconcile"}
                    or any(row["state"] in BUSY and row["candidate_digest"] != record["candidate_digest"] for row in self.records().values())
                    or self.current() not in {self.bundles / body["candidate_digest"], Path(record["previous"])}
                    or (record["state"] == "deployed" and self.current() != self.bundles / body["candidate_digest"])):
                raise HostUpdateError("host_update_rollback_unavailable")
            if self._tree_identity(Path(record["previous"])) != record["previous_tree_digest"]:
                raise HostUpdateError("host_update_product_changed")
            record.update(rollback_approval_id=secrets.token_urlsafe(32), rollback_expires_at=time.time() + 600,
                          rollback_current_digest=self._tree_identity(self.current()))
            self.save(record)
            return self.public(record)

    def _launch(self):
        self.run("systemctl", "start", "--no-block", "agentic-home-update.service", timeout=15)

    def _compose(self, *args, product=None, **kwargs):
        return self.run("docker", "compose", "--env-file", str(self.manager.root / "runtime.env"), "-f",
                        str((product or self.active) / "host/compose.yaml"), *args, **kwargs)

    def _stop_controller(self, record):
        running = self._compose("ps", "--status", "running", "-q", "controller", timeout=10).stdout.strip()
        if "controller_was_running" not in record:
            record["controller_was_running"] = bool(running)
            self.save(record)
        self._compose("stop", "controller", timeout=90)
        identifier = self._compose("ps", "-a", "-q", "controller", timeout=10).stdout.strip()
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            raise HostUpdateError("host_update_controller_stop_unconfirmed")
        result = json.loads(self.run("docker", "inspect", "--format", "{{json .State}}", identifier, timeout=10).stdout)
        if result.get("Running") is not False or result.get("Paused") is not False:
            raise HostUpdateError("host_update_controller_stop_unconfirmed")
        offline_check(self.manager.root.parent / "controller/controller.sqlite3", 3, pause=True, gate=record["candidate_digest"])

    def _switch(self, target):
        target = Path(target)
        secure(target)
        if target.parent != self.bundles.resolve() or not target.is_dir():
            raise HostUpdateError("host_update_product_not_owned")
        temporary = self.active.with_name(self.active.name + ".update-new")
        if temporary.exists() or temporary.is_symlink():
            raise HostUpdateError("host_update_switch_needs_review")
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, self.active)
        sync_path(self.active.parent)

    def _hook(self, action, candidate, *, immutable=False):
        # Fixed command/module. No command, environment, executable or path comes
        # from the signed manifest; rollback always uses the stable engine code.
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": str(ENGINE if immutable else self.active / "host")}
        result = subprocess.run(["/usr/bin/python3", "-m", "agentic_host.host_reconcile", "--" + action,
                                 "--candidate", candidate], cwd="/", env=environment, capture_output=True, timeout=120)
        if result.returncode:
            raise HostUpdateError("host_update_reconcile_failed")

    def _start_services(self, record):
        self.run("systemctl", "daemon-reload", timeout=30)
        self.run("systemctl", "restart", "agentic-home-host.service", timeout=90)
        self.run("systemctl", "restart", "agentic-home-proxy.service", timeout=90)
        if record.get("controller_was_running"):
            self._compose("up", "-d", "--no-deps", "controller", timeout=120)

    def _health(self, *, candidate=True, controller=True):
        proof = self.broker_probe()
        if (not isinstance(proof, dict) or proof.get("bridge") != {"controller_update": {"ingress_bridge_contract": 1}}
                or not isinstance(proof.get("status"), dict) or "vm" not in proof["status"]):
            raise HostUpdateError("host_update_broker_health_failed")
        if candidate and not exact_protocol(proof.get("contract"), CONTRACT):
            raise HostUpdateError("host_update_protocol_unsupported")
        if controller:
            health = self.controller_probe()
            if not isinstance(health, dict) or health.get("status") != "ok" or (candidate and not exact_protocol(health.get("host_update"))):
                raise HostUpdateError("host_update_controller_health_failed")
        database = self.manager.root.parent / "controller/controller.sqlite3"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 3 or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HostUpdateError("host_update_schema_changed")

    @staticmethod
    def _broker_probe():
        # The root executor must use the existing UID1000 management role. It
        # cannot make the root Runner role a management client.
        script = '''import http.client,json,socket
class Unix(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(5);self.sock.connect('/run/agentic-home/host.sock')
result={}
for key,path in [('contract','/host-update-contract'),('bridge','/bridge-contract'),('status','/status')]:
 c=Unix('localhost');c.request('GET',path);r=c.getresponse();raw=r.read(1048577)
 if r.status!=200 or len(raw)>1048576:
  if key=='contract':result[key]=None;continue
  raise RuntimeError('broker health unavailable')
 value=json.loads(raw);result[key]={'vm':value.get('vm')} if key=='status' else value;c.close()
print(json.dumps(result))'''
        last = None
        for _ in range(30):
            try:
                result = subprocess.run(["/usr/bin/python3", "-c", script], user=1000, group=1000, extra_groups=(),
                                        cwd="/", env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=20)
                if result.returncode or len(result.stdout) > 8192:
                    raise HostUpdateError("host_update_broker_health_failed")
                return json.loads(result.stdout)
            except (OSError, ValueError, HostError, subprocess.SubprocessError) as exc:
                last = exc
                time.sleep(1)
        raise HostUpdateError("host_update_broker_health_failed") from last

    @staticmethod
    def _controller_probe():
        from .bootstrap import NoRedirect
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        last = None
        for _ in range(30):
            try:
                with opener.open("http://localhost:8099/health", timeout=3) as response:
                    raw = response.read(8193)
                if len(raw) > 8192:
                    raise HostUpdateError("host_update_controller_health_failed")
                return json.loads(raw)
            except (OSError, ValueError, HostError) as exc:
                last = exc
                time.sleep(1)
        raise HostUpdateError("host_update_controller_health_failed") from last

    def _create_restore_point(self, record):
        attempt = record.setdefault("restore_attempt", {
            "candidate_digest": digest({"candidate": record["candidate_digest"], "operation": record["operation"],
                                         "approval": record["consumed_token_sha256"]}),
            "restore_required_bytes": MAX_EXPANDED})
        def save_restore(value):
            record["restore_attempt"] = value
            if value.get("restore_point"):
                record["restore_point"] = value["restore_point"]
            self.save(record)
        self.save(record)
        if self.restore is None:
            from .host_restore import HostRestore
            restore = HostRestore(self.manager, save=save_restore)
        else:
            restore = self.restore
        restore.capacity(MAX_EXPANDED)
        receipt = restore.create(attempt)
        record["restore_point"] = receipt
        self.save(record)
        return receipt

    def _recover_ha(self, record):
        attempt = record.get("restore_attempt")
        receipt = attempt.get("restore_point") if isinstance(attempt, dict) else None
        if not isinstance(receipt, dict) or receipt.get("phase") not in {
                "ha_shutdown_requested", "ha_snapshot_requested", "ha_snapshot_complete"}:
            return
        def save_restore(value):
            record["restore_attempt"] = value
            record["restore_point"] = value["restore_point"]
            self.save(record)
        if self.restore is None:
            from .host_restore import HostRestore
            restore = HostRestore(self.manager, save=save_restore)
        else:
            restore = self.restore
        # Compensation only: never repeat an uncertain snapshot or merge data.
        restore.recover_ha(attempt)
        save_restore(attempt)

    def _reconcile_service(self, target):
        if self.reconcile is not None:
            return self.reconcile
        from .host_reconcile import HostReconcile
        return HostReconcile(self.manager, product=target)

    def execute(self, *, recover=False):
        if os.geteuid() != 0:
            raise HostUpdateError("host_update_local_root_required")
        with self.locked(), self.manager.locked():
            pending = [record for record in self.records().values() if record["state"] in BUSY]
            if not pending:
                return None
            if len(pending) != 1:
                raise HostUpdateError("host_update_multiple_unresolved_attempts")
            record = pending[0]
            if record.get("recovery_only"):
                record["phase"] = record.get("recovery_phase") or "recovering"
                self.save(record)
                return self._fallback(record)
            if recover:
                if record["state"] == "queued" and record.get("phase") == "approved":
                    # Boot recovery never turns yesterday's approval into a new
                    # installation. No Live effect was committed at this phase.
                    record.update(state="failed", error_code="host_update_executor_interrupted_before_start",
                                  message="安裝尚未開始便中斷，主機維持原狀；請重新檢查後核准。")
                    self.save(record)
                    return self.public(record)
                return self._fallback(record)
            if record["state"] != "queued" or record.get("phase") != "approved":
                return self._fallback(record)
            operation = record["operation"]
            try:
                self.require_no_controller_update()
                if operation == "apply":
                    _, fingerprint = self.verify_envelope({"manifest": record["manifest"], "signature": record["signature"]})
                    if fingerprint != record["key_sha256"] or str(self.current()) != record["previous"]:
                        raise HostUpdateError("host_update_revision_changed")
                    if self._tree_identity(self.current()) != record["previous_tree_digest"]:
                        raise HostUpdateError("host_update_product_changed")
                    target = self.bundles / record["candidate_digest"]
                    self._validate_tree(target, record["manifest"])
                    health = self.controller_probe()
                    if not isinstance(health, dict) or health.get("status") != "ok" or not exact_protocol(health.get("host_update")):
                        raise HostUpdateError("host_update_controller_protocol_unsupported")
                elif operation == "rollback":
                    target = Path(record["previous"])
                    if self.current() != self.bundles / record["candidate_digest"] or self._tree_identity(target) != record["previous_tree_digest"]:
                        raise HostUpdateError("host_update_product_changed")
                else:
                    raise HostUpdateError("host_update_record_invalid")
                record.update(state="rolling_back" if operation == "rollback" else "activating", phase="stopping_controller",
                              restore_required_bytes=MAX_EXPANDED, message="正在建立還原點並準備主機維護。")
                self.save(record)
                self._stop_controller(record)
                record["phase"] = "creating_restore_point"
                self.save(record)
                record["restore_point"] = self._create_restore_point(record)
                self.save(record)
                if operation == "apply":
                    self._reconcile_service(target).prepare(record["candidate_digest"], previous_product=Path(record["previous"]))
                record["phase"] = "switching"
                self.save(record)
                if operation == "rollback":
                    self._hook("rollback", record["candidate_digest"], immutable=True)
                self._switch(target)
                record["phase"] = "reconciling"
                self.save(record)
                if operation == "apply":
                    self._hook("apply", record["candidate_digest"])
                self._start_services(record)
                if operation == "apply":
                    self._hook("health", record["candidate_digest"])
                self._health(candidate=operation == "apply", controller=record.get("controller_was_running", False))
                clear_gate(self.manager.root.parent / "controller/controller.sqlite3", record["candidate_digest"])
                record.update(state="rolled_back" if operation == "rollback" else "deployed", phase="complete",
                              message="已回復先前的主機程式，家庭資料保留；AI 維持暫停，請確認後再恢復。" if operation == "rollback" else "主機更新完成，家庭資料與 Home Assistant 保留；AI 維持暫停，請確認後再恢復。", error_code=None)
                self.save(record)
            except Exception:
                if record.get("phase") == "approved":
                    record.update(state="failed", error_code="host_update_preflight_failed", message="安裝前檢查未通過，主機尚未變更。")
                    self.save(record)
                else:
                    return self._fallback(record)
            return self.public(record)

    def _fallback(self, record):
        try:
            # A failure during backups has not authorized any code switch. Only
            # undo reconciliation once its prepare journal could have existed.
            phase = record.get("phase")
            if phase in {"switching", "reconciling", "recovering"}:
                record["phase"] = "recovering"
                self.save(record)
                self._hook("rollback", record["candidate_digest"], immutable=True)
            previous = Path(record["previous"])
            if self.current() != previous:
                if self._tree_identity(previous) != record["previous_tree_digest"]:
                    raise HostUpdateError("host_update_product_changed")
                self._switch(previous)
            self._recover_ha(record)
            self._start_services(record)
            self._health(candidate=False, controller=record.get("controller_was_running", False))
            clear_gate(self.manager.root.parent / "controller/controller.sqlite3", record["candidate_digest"])
            record.update(state="rolled_back", phase="complete", auto_rollback=record.get("operation") == "apply",
                          error_code="host_update_automatic_rollback" if record.get("operation") == "apply" else "host_update_rollback_recovered",
                          message="更新未完成，已回復先前的主機程式；家庭資料保留，AI 維持暫停。")
        except Exception:
            record.update(state="needs_reconcile", error_code="host_update_recovery_unconfirmed",
                          message="更新未完成，自動回復尚未確認；已保留還原點與管理入口，請重試復原或聯絡支援。")
        self.save(record)
        return self.public(record)

    def handle(self, method, path, body=None):
        if method == "GET" and path == "/host-updates":
            return 200, self.status()
        if method == "GET" and path.startswith("/host-updates/"):
            candidate = path.removeprefix("/host-updates/")
            record = self.records().get(candidate) if DIGEST.fullmatch(candidate) else None
            return (200, self.public(record)) if record else (404, {"detail": "host_update_not_found"})
        if method == "POST":
            if path == "/host-updates/check":
                return 202, self.check(body)
            if path == "/host-updates/stage":
                return 202, self.stage(body)
            if path == "/host-updates/apply":
                return 202, self._approval(body)
            if path == "/host-updates/rollback-offer":
                return 200, self.rollback_offer(body)
            if path == "/host-updates/rollback":
                return 202, self._approval(body, rollback=True)
        raise HostUpdateError("host_update_request_invalid")


def main():
    parser = argparse.ArgumentParser(description="Fixed trusted Host update executor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--execute", action="store_true")
    group.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    HostBundle(Manager()).execute(recover=args.recover)


if __name__ == "__main__":
    main()
