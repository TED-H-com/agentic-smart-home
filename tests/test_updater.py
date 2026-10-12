import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.bootstrap import write_environment
from agentic_host.updater import UpdateError, Updater, canonical, clear_gate, offline_check, validate_manifest


@pytest.fixture
def updater(tmp_path):
    root, product = tmp_path / "data/host", tmp_path / "product"
    root.mkdir(parents=True)
    product.mkdir()
    (product / "images.lock.json").write_text(json.dumps({"agentic-home-runner:0.5.1": "sha256:" + "a" * 64}))
    locked = []
    @contextmanager
    def lock():
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()
    service = Updater(SimpleNamespace(root=root, product=product, run=lambda *args, **kwargs: subprocess.run(args, capture_output=True, text=True), locked=lock))
    service.data.mkdir()
    with sqlite3.connect(service.database) as db:
        db.executescript("PRAGMA user_version=3; CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE records(kind TEXT,id TEXT,body TEXT); CREATE TABLE requests(scope TEXT,key TEXT,digest TEXT,result TEXT); CREATE TABLE events(seq INTEGER);")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    service.publishers = lambda: {"fixture": {"public_key_pem": public, "image_repository": "ghcr.io/fixture/controller"}}
    manifest = {"format": 1, "publisher": "fixture", "version": "0.6.0", "source_commit": "b" * 40,
                "image": "ghcr.io/fixture/controller@sha256:" + "c" * 64, "image_id": "sha256:" + "d" * 64,
                "compatibility": {"schema": 3, "worker_contract": 1, "runner_protocol": 1, "runtime_digest": service.runtime_digest(), "ha_core_versions": ["2026.10.1"]},
                "tests": [{"name": "regression", "result": "passed", "sha256": "e" * 64}], "issued_at": time.time() - 1, "expires_at": time.time() + 1000}
    signature = base64.b64encode(private.sign(canonical(manifest))).decode()
    record = {"candidate_digest": hashlib.sha256(canonical(manifest)).hexdigest(), "manifest": manifest, "signature": signature,
              "state": "staged", "approval_id": "synthetic-approval", "approval_expires_at": time.time() + 100}
    service.records[record["candidate_digest"]] = record
    service._test_locked = locked
    return service, record


def test_actual_openssl_signature_and_publisher_repository_tampering(updater):
    service, record = updater
    service.verify(record["manifest"], record["signature"])
    record["manifest"]["version"] = "9.9.9"
    with pytest.raises(UpdateError, match="signature_invalid"):
        service.verify(record["manifest"], record["signature"])
    record["manifest"]["image"] = "evil.test/arbitrary@sha256:" + "c" * 64
    with pytest.raises(UpdateError, match="repository_not_trusted"):
        service.verify(record["manifest"], record["signature"])


def test_manifest_rejects_hooks_other_components_expiry_and_schema_migration(updater):
    service, record = updater
    manifest = record["manifest"]
    for field in ("shell", "compose", "mounts", "hermes", "haos"):
        with pytest.raises(UpdateError, match="manifest_invalid"):
            validate_manifest({**manifest, field: "forbidden"})
    with pytest.raises(UpdateError, match="expired"):
        validate_manifest({**manifest, "expires_at": time.time() - 1})
    with pytest.raises(UpdateError, match="schema_mismatch"):
        offline_check(service.database, 2)


@pytest.mark.parametrize("kind,state", [("task", "executing"), ("task", "needs_reconcile"), ("work_job", "waiting_for_user"),
                                      ("work_job", "stop_unconfirmed"), ("hermes_run", "running")])
def test_stopped_database_active_work_blocks_update(updater, kind, state):
    service, _ = updater
    with sqlite3.connect(service.database) as db:
        db.execute("INSERT INTO records VALUES (?,?,?)", (kind, "synthetic", json.dumps({"state": state})))
    with pytest.raises(UpdateError, match="active_or_uncertain"):
        offline_check(service.database, 3, pause=True)


def test_worker_lease_blocks_even_without_job_and_approval_is_bound(updater):
    service, record = updater
    with sqlite3.connect(service.database) as db:
        db.execute("INSERT INTO settings VALUES ('worker_lease','{}')")
    with pytest.raises(UpdateError, match="lease_active"):
        offline_check(service.database, 3)
    for digest, approval in (("0" * 64, record["approval_id"]), (record["candidate_digest"], "wrong")):
        with pytest.raises(UpdateError, match="approval_invalid"):
            service.apply({"candidate_digest": digest, "approval_id": approval})


def prepare_deployment(service, record):
    current = ["sha256:" + "1" * 64]
    record["base_image"] = current[0]
    service.image = lambda: current[0]
    service.stop = lambda: None
    service.probe = lambda image, schema: None
    service.activate = lambda image: current.__setitem__(0, image)
    def health():
        assert service._test_locked
        return {"version": record["manifest"]["version"] if current[0] == record["manifest"]["image_id"] else "0.5.1",
                "ha": "connected", "ha_version": "2026.10.1", "runner": "paired", "paused": True}
    service.health = health
    return current


def test_deploy_and_later_rollback_preserve_new_live_rows(updater):
    service, record = updater
    current = prepare_deployment(service, record)
    old = current[0]
    service.deploy(record)
    assert record["state"] == "deployed" and current[0] != old
    snapshot = service.root / record["snapshot"]
    assert snapshot.exists() and snapshot.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(service.database) as db:
        db.execute("INSERT INTO records VALUES ('task','new-live-operation','{\"state\":\"succeeded\"}')")
    service.deploy(record, rollback=True)
    assert record["state"] == "rolled_back" and current[0] == old
    with sqlite3.connect(service.database) as db:
        assert db.execute("SELECT id FROM records").fetchone()[0] == "new-live-operation"
        assert db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"


def test_failed_candidate_health_rolls_artifact_back_without_replacing_database(updater):
    service, record = updater
    current = prepare_deployment(service, record)
    old, real_health = current[0], service.health
    def health():
        if current[0] != old:
            with sqlite3.connect(service.database) as db:
                db.execute("INSERT INTO records VALUES ('task','preserved','{\"state\":\"succeeded\"}')")
            raise UpdateError("update_health_failed")
        return real_health()
    service.health = health
    service.deploy(record)
    assert record["state"] == "rolled_back" and current[0] == old
    with sqlite3.connect(service.database) as db:
        assert db.execute("SELECT id FROM records").fetchone()[0] == "preserved"


def test_new_schema_after_bad_candidate_fails_closed_without_old_database_restore(updater):
    service, record = updater
    current = prepare_deployment(service, record)
    old, real_health = current[0], service.health
    def health():
        if current[0] != old:
            with sqlite3.connect(service.database) as db:
                db.execute("PRAGMA user_version=99")
            raise UpdateError("update_health_failed")
        return real_health()
    service.health = health
    service.deploy(record)
    assert record["state"] == "needs_reconcile" and current[0] != old
    with sqlite3.connect(service.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_environment_overrides_preserve_both_pinned_components(tmp_path):
    path = tmp_path / "runtime.env"
    write_environment(path, "sha256:" + "1" * 64)
    write_environment(path, "sha256:" + "2" * 64, "AGENTIC_CONTROLLER_IMAGE")
    write_environment(path, "sha256:" + "3" * 64)
    assert path.read_text().splitlines() == ["AGENTIC_CONTROLLER_IMAGE=sha256:" + "2" * 64, "AGENTIC_HERMES_IMAGE=sha256:" + "3" * 64]


@pytest.mark.parametrize("actual_container", [False, True], ids=["database_handles", "packaged_controller"])
def test_fixed_probe_launch_and_database_handles_close_before_volume_release(
        updater, tmp_path, monkeypatch, request, record_property, actual_container):
    """Exercise the actual probe; only root-only disk allocation is substituted.

    No developer-host mount is created. Installed-VM coverage separately proves
    the ext4 quota/mount implementation. The Docker variant retains the product
    command, image, UID and limits and tests its actual mounted Python entrypoint.
    """
    if actual_container and not request.config.getoption("--containers"):
        pytest.skip("requires --containers and the packaged Controller image")
    import agentic_host.updater as module
    from agentic_controller.store import Store

    service, _ = updater
    service.database.unlink()
    database = Store(service.database)
    database.close()
    service.manager.product = Path(__file__).resolve().parents[1]
    directory = tmp_path / "probe-volume"
    released, handles, containers = [], [], []
    command_diagnostics = []

    class DiskFixture:
        def __init__(self, root, identity, size):
            assert root == service.root / "probes" and len(identity) == 32 and size == 64

        def create(self):
            directory.mkdir(mode=0o755)
            return directory

        def close(self):
            for fd in Path("/proc/self/fd").iterdir():
                try:
                    target = Path(os.readlink(fd))
                except FileNotFoundError:
                    continue
                if target == service.database or target.is_relative_to(directory):
                    handles.append("live_database" if target == service.database else "probe_database")
            released.append(True)

    def test_chown(path, uid, gid):
        assert (uid, gid) == (1000, 1000)
        if os.getuid() == 1000:
            os.chown(path, uid, gid)
        else:
            # Non-root CI has UID 1001. This disk-plumbing fixture grants only
            # its synthetic data access; the real container still runs UID 1000.
            Path(path).chmod(0o777 if Path(path).is_dir() else 0o666)

    monkeypatch.setattr(module, "DiskVolume", DiskFixture)
    monkeypatch.setattr(module, "os", SimpleNamespace(chown=test_chown))
    image = "agentic-home-controller:0.6.0"

    def execute(*args, **kwargs):
        if args[:2] == ("docker", "run"):
            containers.append(args[args.index("--name") + 1])
        result = subprocess.run(args, capture_output=True, text=True, **{**kwargs, "check": False})
        if args[:2] == ("docker", "run"):
            command_diagnostics.append({"returncode": result.returncode,
                                        "controller_import_error": "No module named 'agentic_controller'" in result.stderr,
                                        "stdout_bytes": len(result.stdout.encode()), "stderr_bytes": len(result.stderr.encode())})
        if kwargs.get("check", True) and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, args)
        return result

    if actual_container:
        service.run = execute
        image = execute("docker", "image", "inspect", "--format", "{{.Id}}", image, timeout=10).stdout.strip()
    else:
        monkeypatch.setattr(module, "ready", lambda *args: None)
        monkeypatch.setattr(module, "fence", lambda *args: None)
        service.run = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    old_umask = os.umask(0o077)
    failure = None
    try:
        try:
            service.probe(image, 3)
        except Exception as exc:
            failure = type(exc).__name__
    finally:
        os.umask(old_umask)
        for name in containers:
            subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=20, check=False)
    diagnostics = {"failure_type": failure, "database_handles_at_volume_close": handles,
                   "volume_cleanup_called": released == [True], "command": command_diagnostics,
                   "image": image, "disk_allocation": "substituted_without_host_mount", "umask": "0077"}
    record_property("probe_diagnostics", json.dumps(diagnostics, sort_keys=True))
    assert failure is None, diagnostics
    assert released == [True] and handles == [], diagnostics
    assert not service.probe_path.exists()


def test_restart_fences_deployment_and_requires_new_explicit_rollback_offer(updater):
    service, record = updater
    record.update(state="deploying", previous_image="sha256:" + "1" * 64)
    service.update(record)
    restarted = Updater(service.manager)
    recovered = restarted.records[record["candidate_digest"]]
    assert recovered["state"] == "needs_reconcile" and recovered["error_code"] == "update_interrupted"
    assert restarted.offer_rollback({"candidate_digest": record["candidate_digest"]})["rollback_approval_id"]
    assert recovered["state"] == "needs_reconcile"


def test_rejected_active_work_restarts_same_binary_and_does_not_activate_candidate(updater):
    service, record = updater
    current = prepare_deployment(service, record)
    original = current[0]
    with sqlite3.connect(service.database) as db:
        db.execute("INSERT INTO records VALUES ('work_job','working','{\"state\":\"running\"}')")
    service.deploy(record)
    assert record["state"] == "failed" and record["error_code"] == "update_active_or_uncertain_work"
    assert current[0] == original


def test_expired_manifest_can_still_verify_for_existing_rollback(updater):
    service, record = updater
    # The exact signed envelope is still checked. Expiry blocks a new deployment,
    # while an already installed revision remains eligible for safe rollback.
    import unittest.mock
    with unittest.mock.patch("agentic_host.updater.time.time", return_value=record["manifest"]["expires_at"] + 1):
        with pytest.raises(UpdateError, match="expired"):
            service.verify(record["manifest"], record["signature"])
        service.verify(record["manifest"], record["signature"], rollback=True)


def test_lost_activation_response_checks_new_database_before_fallback(updater):
    service, record = updater
    current = prepare_deployment(service, record)
    original = current[0]
    def activate(image):
        current[0] = image
        if image != original:
            with sqlite3.connect(service.database) as db:
                db.execute("PRAGMA user_version=99")
            raise TimeoutError("synthetic lost compose response")
    service.activate = activate
    service.deploy(record)
    assert record["state"] == "needs_reconcile" and current[0] != original


def test_staged_approval_cannot_target_a_changed_current_revision(updater):
    service, record = updater
    current = prepare_deployment(service, record)
    current[0] = "sha256:" + "2" * 64
    stopped = []
    service.stop = lambda: stopped.append(True)
    service.deploy(record)
    assert record["state"] == "failed" and record["error_code"] == "update_base_revision_changed"
    assert not stopped


def test_restart_rollback_works_when_candidate_web_is_down(updater):
    service, record = updater
    current = prepare_deployment(service, record)
    original, normal = current[0], service.health
    record.update(previous_image=original, state="needs_reconcile", baseline={"version": "0.5.1", "ha": "connected", "ha_version": "2026.10.1", "runner": "paired", "paused": True})
    current[0] = record["manifest"]["image_id"]
    def health():
        if current[0] != original:
            raise UpdateError("update_health_failed")
        return normal()
    service.health = health
    service.deploy(record, rollback=True)
    assert record["state"] == "rolled_back" and current[0] == original


def test_maintenance_gate_binds_revision_and_is_removed_only_after_smoke(updater):
    service, record = updater
    offline_check(service.database, 3, pause=True, gate=record["candidate_digest"])
    with pytest.raises(UpdateError, match="gate_revision_changed"):
        clear_gate(service.database, "0" * 64)
    with sqlite3.connect(service.database) as db:
        assert json.loads(db.execute("SELECT value FROM settings WHERE key='updater_gate'").fetchone()[0]) == record["candidate_digest"]
    prepare_deployment(service, record)
    service.deploy(record)
    assert record["state"] == "deployed"
    with sqlite3.connect(service.database) as db:
        assert db.execute("SELECT value FROM settings WHERE key='updater_gate'").fetchone() is None
        assert db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"


@pytest.mark.parametrize("volumes,expected", [(None, "staged"), ({"/unbounded": {}}, "failed")])
def test_candidate_cannot_add_implicit_docker_volumes(updater, volumes, expected):
    service, record = updater
    service.image = lambda: "sha256:" + "1" * 64
    def execute(*args, **kwargs):
        if "--format" in args:
            output = record["manifest"]["image_id"]
        elif "inspect" in args:
            output = json.dumps([{"Os": "linux", "Architecture": "amd64", "Config": {"Volumes": volumes}}])
        else:
            output = ""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
    service.run = execute
    service.prepare(record)
    assert record["state"] == expected
    if expected == "failed":
        assert record["error_code"] == "update_image_runtime_invalid"


def aged_snapshot(service, record, days, nonce="1" * 16, *, rollback=False):
    path = service.root / (record["candidate_digest"] + ("-rollback-" if rollback else "-") + nonce + ".sqlite3")
    service.backup(path)
    when = time.time() - days * 86400
    os.utime(path, (when, when))
    return path


def test_snapshot_expiry_is_audit_bounded_and_does_not_alter_live_database(updater):
    service, record = updater
    expired = aged_snapshot(service, record, 31)
    recent = aged_snapshot(service, record, 29, "2" * 16, rollback=True)
    unknown = service.root / "manual-backup.sqlite3"
    unknown.write_text("not an updater-owned filename")
    os.utime(unknown, (1, 1))
    before = service.database.read_bytes()
    assert service.expire_snapshots() == 1
    assert not expired.exists() and recent.exists() and unknown.exists()
    assert service.database.read_bytes() == before
    assert record["state"] == "staged"


@pytest.mark.parametrize("audit,pending", [(30, 2), (2, 20), (2, None)])
def test_snapshot_expiry_honors_stricter_current_or_pending_policy(updater, audit, pending):
    service, record = updater
    with sqlite3.connect(service.database) as db:
        db.execute("INSERT INTO settings VALUES ('retention',?)", (json.dumps({"audit_days": audit, "context_days": 1}),))
        db.execute("INSERT INTO settings VALUES ('retention_pending',?)", (json.dumps(None if pending is None else {"audit_days": pending, "context_days": 1}),))
    expired = aged_snapshot(service, record, 3)
    fresh = aged_snapshot(service, record, 1, "2" * 16)
    service.status()
    assert not expired.exists() and fresh.exists()


@pytest.mark.parametrize("state", ["deploying", "rolling_back", "needs_reconcile"])
def test_snapshot_expiry_preserves_unresolved_candidate_recovery(updater, state):
    service, record = updater
    before = aged_snapshot(service, record, 60)
    during_rollback = aged_snapshot(service, record, 50, rollback=True)
    record.update(state=state, snapshot=during_rollback.name)
    assert service.expire_snapshots() == 0
    assert before.exists() and during_rollback.exists()
    # Recovery completion releases both generations to the same audit policy.
    record["state"] = "rolled_back"
    assert service.expire_snapshots() == 2


def test_restart_snapshot_cleanup_preserves_interrupted_recovery(updater):
    service, record = updater
    protected = aged_snapshot(service, record, 40)
    expired = service.root / ("f" * 64 + "-" + "2" * 16 + ".sqlite3")
    service.backup(expired)
    os.utime(expired, (1, 1))
    service.update(record, state="deploying", snapshot=protected.name)
    restarted = Updater(service.manager)
    assert restarted.records[record["candidate_digest"]]["state"] == "needs_reconcile"
    assert protected.exists() and not expired.exists()


def test_snapshot_cleanup_never_traverses_links_or_subdirectories(updater, tmp_path):
    service, record = updater
    external = tmp_path / "private.sqlite3"
    external.write_text("untouched")
    os.utime(external, (1, 1))
    link = service.root / (record["candidate_digest"] + "-" + "1" * 16 + ".sqlite3")
    link.symlink_to(external)
    nested = service.root / "nested"
    nested.mkdir()
    (nested / link.name).write_text("untouched nested file")
    assert service.expire_snapshots() == 0
    assert link.is_symlink() and external.read_text() == "untouched"
    # Even a trusted-looking directory name cannot redirect the cleanup root.
    alias = tmp_path / "aliased-updates"
    alias.symlink_to(service.root, target_is_directory=True)
    service.root = alias
    assert service.expire_snapshots() == 0


@pytest.mark.parametrize("policy", ["invalid json", '{"audit_days":true}', '{"audit_days":0}'])
def test_unreadable_retention_never_guesses_expiry_for_recovery_material(updater, policy):
    service, record = updater
    old = aged_snapshot(service, record, 60)
    with sqlite3.connect(service.database) as db:
        db.execute("INSERT INTO settings VALUES ('retention_pending',?)", (policy,))
    assert service.expire_snapshots() == 0 and old.exists()
