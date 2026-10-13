import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
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
    manifest = {"format": 1, "publisher": "fixture", "version": "0.7.0", "source_commit": "b" * 40,
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


@pytest.mark.parametrize("state", ["sent", "acknowledged", "unknown"])
def test_unconfirmed_app_operation_blocks_controller_switch_even_if_bridge_disabled(updater, state):
    service, _ = updater
    with sqlite3.connect(service.database) as db:
        db.execute("INSERT INTO settings VALUES ('bridge_maintenance',?)", (json.dumps({
            "enabled": False, "state": "disabled", "operation": {"kind": "restart", "state": state}}),))
    with pytest.raises(UpdateError, match="active_or_uncertain_work"):
        offline_check(service.database, 3, pause=True)


def test_candidate_contract_gate_rejects_old_or_wrong_bridge_semantics(monkeypatch):
    from agentic_host.update_probe import require_contracts
    required = SimpleNamespace(setting=lambda key: 1)
    require_contracts(required)
    for value in (None, SimpleNamespace(), SimpleNamespace(BRIDGE_CONTRACT=True),
                  SimpleNamespace(BRIDGE_CONTRACT=2)):
        monkeypatch.setitem(sys.modules, "agentic_controller.bridge", value)
        with pytest.raises((ImportError, AssertionError)):
            require_contracts(required)
    # Installations with no bridge history retain the existing schema-only gate.
    require_contracts(SimpleNamespace(setting=lambda key: None))


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


def probe_fixture_owner(directory, image, uid, gid):
    """Change only the synthetic volume's fixed SQLite files, then remove helper."""
    name = "agentic-updater-fixture-" + uuid.uuid4().hex
    script = """
import os,stat,sys
from pathlib import Path
root=Path('/fixture')
files=list(root.iterdir())
assert len(files)<=3
for path in files:
    assert path.name in {'controller.sqlite3','controller.sqlite3-wal','controller.sqlite3-shm'}
    assert stat.S_ISREG(path.lstat().st_mode)
uid,gid=int(sys.argv[1]),int(sys.argv[2])
for path in files:
    path.chmod(0o600)
    os.chown(path,uid,gid,follow_symlinks=False)
root.chmod(0o700)
os.chown(root,uid,gid,follow_symlinks=False)
"""
    try:
        subprocess.run(["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
                        "--no-healthcheck", "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
                        "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER", "--security-opt", "no-new-privileges:true",
                        "--memory", "64m", "--pids-limit", "16", "--log-driver", "none",
                        "--mount", f"type=bind,src={directory},dst=/fixture", "--entrypoint", "python", image,
                        "-I", "-c", script, str(uid), str(gid)],
                       check=True, capture_output=True, timeout=20)
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=20, check=False)


@pytest.mark.parametrize("actual_container,foreign_owner", [(False, False), (True, False), (True, True)],
                         ids=["database_handles", "packaged_controller", "packaged_controller_foreign_owner"])
def test_fixed_probe_launch_and_database_handles_close_before_volume_release(
        updater, tmp_path, monkeypatch, request, record_property, actual_container, foreign_owner):
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
    database.set_setting("ingress_bridge", {"state": "revoked", "token_hash": "private-placeholder"})
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
        assert Path(path) in {directory, directory / "controller.sqlite3"}
        if not actual_container or Path(path) == directory:
            return
        # The real broker is root; CI is UID1001. Transfer both paths together
        # after creation instead of replacing ownership with world-write access.
        if foreign_owner:
            # Exercise the cross-UID handoff even on a UID1000 developer host.
            probe_fixture_owner(directory, image, 1001, 1001)
        probe_fixture_owner(directory, image, uid, gid)

    monkeypatch.setattr(module, "DiskVolume", DiskFixture)
    monkeypatch.setattr(module, "os", SimpleNamespace(chown=test_chown))
    image = "agentic-home-controller:0.8.0"

    def execute(*args, **kwargs):
        if args[:2] == ("docker", "run"):
            containers.append(args[args.index("--name") + 1])
        result = subprocess.run(args, capture_output=True, text=True, **{**kwargs, "check": False})
        if args[:2] == ("docker", "run"):
            command_diagnostics.append({"returncode": result.returncode,
                                        "controller_import_error": "No module named 'agentic_controller'" in result.stderr,
                                        "controller_chmod_error": "PermissionError: [Errno 1] Operation not permitted" in result.stderr and "chmod" in result.stderr,
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
        try:
            for name in containers:
                subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=20, check=False)
        finally:
            if actual_container and directory.exists():
                probe_fixture_owner(directory, image, os.getuid(), os.getgid())
                assert directory.stat().st_uid == os.getuid()
                assert (directory / "controller.sqlite3").stat().st_uid == os.getuid()
    diagnostics = {"failure_type": failure, "database_handles_at_volume_close": handles,
                   "volume_cleanup_called": released == [True], "command": command_diagnostics,
                   "image": image, "disk_allocation": "substituted_without_host_mount", "umask": "0077",
                   "synthetic_prior_owner": 1001 if foreign_owner else os.getuid(),
                   "controller_uid": 1000, "cleanup_owner": os.getuid()}
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
        elif len(args) > 1 and args[1] == "cp":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="Error: No such container:path: " + args[2])
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


def git_out(repo, *args, git_dir=False):
    command = ["git", "--git-dir", str(repo), *args] if git_dir else ["git", "-C", str(repo), *args]
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()


def write_commit(repo, relative, content, message):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", relative], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True, capture_output=True)
    return git_out(repo, "rev-parse", "HEAD")


class BundleDocker:
    """Stand-in for create/cp/rm. It never starts a container."""

    def __init__(self, bundle, image_id):
        self.bundle = None if bundle is None else Path(bundle)
        self.image_id = image_id
        self.calls = []
        self.created = set()

    def __call__(self, *args, timeout=120, check=True):
        self.calls.append(args)
        if args[0] == "git":
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
            if check and result.returncode:
                raise AssertionError((args, result.stderr))
            return result
        if args[0] != "docker" or args[1] in {"run", "start", "exec"}:
            raise AssertionError(args)
        if args[1:3] == ("image", "inspect") and "--format" in args:
            return subprocess.CompletedProcess(args, 0, self.image_id + "\n", "")
        if args[1:3] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, json.dumps([{"Os": "linux", "Architecture": "amd64", "Config": {}}]), "")
        if args[1] == "pull":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "create":
            self.created.add(args[args.index("--name") + 1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "cp":
            name = args[2].split(":", 1)[0]
            assert name in self.created
            if self.bundle is None:
                return subprocess.CompletedProcess(args, 1, "", "Error: No such container:path: " + args[2])
            Path(args[3]).write_bytes(self.bundle.read_bytes())
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "rm":
            self.created.discard(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def product_history(tmp):
    repo = tmp / "product-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    return repo, write_commit(repo, "controller/app.py", "print('A')\n", "A")


def install_coding(service, repo, commit):
    """Install the same way provision_source does: a bare clone of a HEAD bundle."""
    bare = service.source_repo
    bare.parent.mkdir(parents=True, mode=0o700)
    packed = bare.parent / "install.bundle"
    subprocess.run(["git", "-C", str(repo), "bundle", "create", str(packed), "HEAD"], check=True, capture_output=True)
    subprocess.run(["git", "clone", "--bare", "--no-hardlinks", str(packed), str(bare)], check=True, capture_output=True)
    assert git_out(bare, "rev-parse", "HEAD", git_dir=True) == commit
    lock = {"repo_id": "agentic-home", "commit": commit, "bundle_sha256": "c" * 64}
    service.source_lock.write_text(json.dumps(lock) + "\n")
    os.chmod(service.source_lock, 0o600)
    return lock


def make_bundle(repo, destination, *refs):
    subprocess.run(["git", "-C", str(repo), "bundle", "create", str(destination), *refs], check=True, capture_output=True)
    return destination


def test_status_source_commit_is_null_without_source_json(updater):
    service, record = updater
    status = service.status()
    assert status["deployed_source_commit"] is None
    assert status["records"][0]["source_sync"] == "unavailable"
    assert "source_lock_before" not in status["records"][0]
    assert record["candidate_digest"]


def test_bundle_head_must_equal_source_commit_and_missing_bundle_stays_unavailable(updater, tmp_path):
    service, record = updater
    repo, commit_a = product_history(tmp_path)
    lock = install_coding(service, repo, commit_a)
    commit_b = write_commit(repo, "controller/app.py", "print('B')\n", "B")
    bundle = make_bundle(repo, tmp_path / "b.bundle", "HEAD")
    assert subprocess.run(["git", "--git-dir", str(service.source_repo), "cat-file", "-e", commit_b], capture_output=True).returncode != 0
    record["manifest"]["source_commit"] = commit_a
    service.run = BundleDocker(bundle, record["manifest"]["image_id"])
    with pytest.raises(UpdateError, match="update_source_bundle_invalid"):
        service.stage_source(record)
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_a
    subprocess.run(["git", "-C", str(repo), "branch", "other"], check=True, capture_output=True)
    two = make_bundle(repo, tmp_path / "two.bundle", "HEAD", "other")
    record["manifest"]["source_commit"] = commit_b
    service.run = BundleDocker(two, record["manifest"]["image_id"])
    with pytest.raises(UpdateError, match="update_source_bundle_invalid"):
        service.stage_source(record)
    service.image = lambda: "sha256:" + "1" * 64
    service.run = BundleDocker(None, record["manifest"]["image_id"])
    service.prepare(record)
    assert record["state"] == "staged" and record["source_sync"] == "unavailable"
    assert not service.run.created
    assert not any(call[1] in {"run", "start", "exec"} for call in service.run.calls)
    current = prepare_deployment(service, record)
    service.deploy(record)
    assert record["state"] == "deployed" and current[0] == record["manifest"]["image_id"]
    assert record["source_sync"] == "unavailable"
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_a
    assert json.loads(service.source_lock.read_text()) == lock
    assert service.status()["deployed_source_commit"] == commit_a


def test_deploy_moves_source_rollback_restores_and_submit_binds_head(updater, tmp_path, monkeypatch):
    service, record = updater
    repo, commit_a = product_history(tmp_path)
    lock = install_coding(service, repo, commit_a)
    commit_b = write_commit(repo, "controller/app.py", "print('B')\n", "B")
    bundle = make_bundle(repo, tmp_path / "b.bundle", "HEAD")
    record["manifest"]["source_commit"] = commit_b
    docker = BundleDocker(bundle, record["manifest"]["image_id"])
    service.run = docker
    service.stage_source(record)
    assert not docker.created
    assert not any(call[0] == "docker" and call[1] in {"run", "start", "exec"} for call in docker.calls)
    ref = "refs/agentic/candidate/" + record["manifest"]["image_id"].removeprefix("sha256:")
    assert record["source_sync"] == "pending" and record["source_before"] == commit_a and record["source_candidate"] == commit_b
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_a
    assert git_out(service.source_repo, "rev-parse", ref, git_dir=True) == commit_b
    assert not list(service.root.glob("source-*.bundle"))
    current = prepare_deployment(service, record)
    old = current[0]
    service.deploy(record)
    moved = json.loads(service.source_lock.read_text())
    assert record["state"] == "deployed" and record["source_sync"] == "moved" and current[0] != old
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_b
    assert moved == {"repo_id": "agentic-home", "commit": commit_b, "install_commit": commit_a,
                     "previous_commit": commit_a, "image_id": record["manifest"]["image_id"],
                     "candidate_digest": record["candidate_digest"]}
    assert service.status()["deployed_source_commit"] == commit_b
    stored = json.loads(service.path.read_text())[record["candidate_digest"]]
    assert stored["source_lock_before"] == lock
    assert "source_lock_before" not in service.public(record)
    import agentic_host.coding_service as coding_module
    monkeypatch.setattr(coding_module, "credentials", lambda path: ("fixture", "/v1/responses", {}))
    coder = coding_module.CodingService(service.manager)
    coder.capabilities = lambda: {"available": True}
    coder.execute = lambda identifier: None
    def submit(base):
        body = {"work_contract_major": 1, "id": uuid.uuid4().hex, "case_id": uuid.uuid4().hex,
                "worker_id": "host-coder", "mode": "research", "provider": "codex", "model": "",
                "repo_id": "agentic-home", "goal": "Read the updated source", "questions": [],
                "base_commit": base, "expires": time.time() + 300, "lease_epoch": 1, "context_days": 7}
        return coder.submit(body)
    try:
        refused = submit(commit_a)[1]
        assert refused["error_code"] == "coding_source_revision_mismatch" and refused["state"] == "failed"
        accepted = submit(None)
        coder.threads[accepted[1]["id"]].join(2)
        assert accepted[1]["state"] == "queued" and accepted[1]["base_commit"] == commit_b
        assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_b
    finally:
        coder.close()
    service.deploy(record, rollback=True)
    assert record["state"] == "rolled_back" and record["source_sync"] == "reverted" and current[0] == old
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_a
    assert json.loads(service.source_lock.read_text()) == lock
    assert service.status()["deployed_source_commit"] == commit_a


def test_source_cas_failure_falls_back_without_claiming_a_move(updater, tmp_path):
    service, record = updater
    repo, commit_a = product_history(tmp_path)
    lock = install_coding(service, repo, commit_a)
    commit_b = write_commit(repo, "controller/app.py", "print('B')\n", "B")
    service.run = BundleDocker(make_bundle(repo, tmp_path / "b.bundle", "HEAD"), record["manifest"]["image_id"])
    record["manifest"]["source_commit"] = commit_b
    service.stage_source(record)
    real = service.run
    def fail_cas(*args, **kwargs):
        if args[0] == "git" and "update-ref" in args:
            return subprocess.CompletedProcess(args, 128, "", "cannot lock ref")
        return real(*args, **kwargs)
    service.run = fail_cas
    current = prepare_deployment(service, record)
    old = current[0]
    service.deploy(record)
    assert record["state"] == "rolled_back" and record["error_code"] == "update_source_move_failed"
    assert record["source_sync"] == "pending" and current[0] == old
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_a
    assert json.loads(service.source_lock.read_text()) == lock


def test_active_coding_work_blocks_the_source_move(updater, tmp_path, monkeypatch):
    service, record = updater
    repo, commit_a = product_history(tmp_path)
    install_coding(service, repo, commit_a)
    commit_b = write_commit(repo, "controller/app.py", "print('B')\n", "B")
    record["manifest"]["source_commit"] = commit_b
    service.run = BundleDocker(make_bundle(repo, tmp_path / "b.bundle", "HEAD"), record["manifest"]["image_id"])
    service.stage_source(record)
    import agentic_host.updater as updater_module
    calls = {"n": 0}
    real = updater_module.offline_check
    def gated(*args, **kwargs):
        calls["n"] += 1
        # The source move is the third read, after the image has already passed smoke.
        if calls["n"] == 3:
            raise UpdateError("update_active_or_uncertain_work")
        return real(*args, **kwargs)
    monkeypatch.setattr(updater_module, "offline_check", gated)
    current = prepare_deployment(service, record)
    old = current[0]
    service.deploy(record)
    assert calls["n"] >= 3
    assert record["state"] == "rolled_back" and record["error_code"] == "update_active_or_uncertain_work"
    assert record["source_sync"] == "pending" and current[0] == old
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_a
    assert json.loads(service.source_lock.read_text())["commit"] == commit_a


def test_moved_head_on_rollback_needs_reconcile(updater, tmp_path):
    service, record = updater
    repo, commit_a = product_history(tmp_path)
    install_coding(service, repo, commit_a)
    commit_b = write_commit(repo, "controller/app.py", "print('B')\n", "B")
    record["manifest"]["source_commit"] = commit_b
    service.run = BundleDocker(make_bundle(repo, tmp_path / "b.bundle", "HEAD"), record["manifest"]["image_id"])
    service.stage_source(record)
    prepare_deployment(service, record)
    service.deploy(record)
    assert record["state"] == "deployed"
    tree = git_out(service.source_repo, "rev-parse", commit_b + "^{tree}", git_dir=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    commit_c = subprocess.check_output(["git", "--git-dir", str(service.source_repo), "commit-tree", tree, "-p", commit_b, "-m", "C"],
                                       text=True, env=env).strip()
    subprocess.run(["git", "--git-dir", str(service.source_repo), "update-ref", "HEAD", commit_c, commit_b],
                   check=True, capture_output=True)
    service.deploy(record, rollback=True)
    assert record["state"] == "needs_reconcile" and record["error_code"] == "update_source_diverged"
    assert record["source_sync"] == "needs_reconcile"
    assert git_out(service.source_repo, "rev-parse", "HEAD", git_dir=True) == commit_c
    assert json.loads(service.source_lock.read_text())["commit"] == commit_b


def test_interrupted_source_move_is_not_reported_as_moved(updater, tmp_path):
    service, record = updater
    service.update(record, state="deploying", source_sync="pending", source_before="a" * 40, source_candidate="b" * 40)
    restarted = Updater(service.manager)
    recovered = restarted.records[record["candidate_digest"]]
    assert recovered["state"] == "needs_reconcile" and recovered["source_sync"] == "needs_reconcile"
