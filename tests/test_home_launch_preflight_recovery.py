"""Deterministic reproduction of rejection before the Home sandbox ledger exists.

Real HomeService and HomeSandbox lifecycle code; privileged environment probes,
credentials and relay are fixtures. No containers, mounts or workers are started.
"""
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_coding_job_budgets import bare_source, request

from agentic_host import coding_service
from agentic_host.coding_sandbox import SandboxError
from agentic_runner import home_launcher
from agentic_runner.home_sandbox import HomeSandbox
from agentic_runner.home_service import HomeService


@pytest.mark.parametrize("trigger", ["disk_reserve", "retained_workspaces"])
@pytest.mark.parametrize("recovery", ["background", "explicit_cancel"])
def test_preallocation_refusal_has_durable_identity_and_recovers(tmp_path, monkeypatch, trigger, recovery):
    source = bare_source(tmp_path)
    data = tmp_path / "data"
    root = data / "coding"
    state = {"available_bytes": 64 * 1024**3, "probes": [], "boot_id": "before"}
    trusted = {Path(value) for value in (
        "/usr/bin/bwrap", "/usr/bin/fuse2fs", "/usr/sbin/mkfs.ext4", "/usr/bin/git")}
    original_file, original_stat, original_space = Path.is_file, Path.stat, os.statvfs
    original_uid = os.geteuid
    monkeypatch.setattr(Path, "is_file", lambda path: True if path in trusted else original_file(path))
    monkeypatch.setattr(Path, "stat", lambda path, *args, **kwargs:
        SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o755) if path in trusted
        else original_stat(path, *args, **kwargs))
    monkeypatch.setattr(home_launcher, "ROOT", root)
    monkeypatch.setattr(home_launcher, "runtime_context", lambda: {})
    monkeypatch.setattr(home_launcher, "require_private", lambda path: None)
    monkeypatch.setattr(home_launcher, "identity", lambda:
        {"boot_id": state["boot_id"], "pid_ns": 10, "cgroup_ns": 11, "mnt_ns": 12})

    def disk_space(path):
        if path != root / "work":
            return original_space(path)
        state["probes"].append(state["available_bytes"])
        return SimpleNamespace(f_bavail=state["available_bytes"], f_frsize=1)

    monkeypatch.setattr(os, "statvfs", disk_space)
    monkeypatch.setattr(coding_service, "credentials", lambda path: ("fixture", "/responses", {}))

    class Relay:
        path = None

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            # Another workload consumes disk after the cached ready check but
            # before launch's mandatory fresh preflight. No API manipulation.
            if trigger == "disk_reserve":
                state["available_bytes"] = 0

        def close(self):
            pass

    def native_execute(*args, **kwargs):
        assert args[:3] == ("findmnt", "-n", "-o"), "sandbox allocation must never start"
        return SimpleNamespace(stdout="ext4\n")

    def sandbox_factory(*args, **kwargs):
        return HomeSandbox(*args, execute=native_execute, **kwargs)

    def create_service():
        monkeypatch.setattr(os, "geteuid", original_uid)
        service = HomeService(data, packaged_source=source, sandbox_factory=sandbox_factory, relay_factory=Relay)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        return service

    service = create_service()
    payload = request(expires=time.time() + 300)
    try:
        assert service.capabilities()["available"] is True
        if trigger == "retained_workspaces":
            for index in range(1, 5):
                previous_id = str(index) * 32
                (root / "work" / previous_id).mkdir()
                service.sandbox().save({"job_id": previous_id, "state": "cancelled", "stop_confirmed": True})
        status, queued = service.submit(payload)
        assert status == 202 and queued["state"] == "queued"
        service.threads[payload["id"]].join(3)
        assert not service.threads[payload["id"]].is_alive()
        if trigger == "disk_reserve":
            assert state["probes"] == [64 * 1024**3, 0]
        job = service.items[payload["id"]]
        assert job["sandbox_attempted"] and not job["execution_started"]
        assert job["state"] == "stop_unconfirmed" and not job["stop_confirmed"]
        ledger = root / "work" / (payload["id"] + ".json")
        assert ledger.exists(), "preallocation refusal needs an exact durable no-execution record"
        assert not (root / "work" / payload["id"]).exists()
        assert json.loads(service.state_path.read_text())[payload["id"]]["sandbox_attempted"] is True

        state["available_bytes"] = 64 * 1024**3
        if recovery == "background":
            service.prune()
        else:
            assert service.handle("POST", f"/coding/jobs/{payload['id']}/cancel", {})[0] == 200
        recovered = service.items[payload["id"]]
        assert recovered["state"] == "failed" and recovered["stop_confirmed"] is True
        assert recovered["execution_started"] is False and recovered["workspace_released"] is True
        assert not service.active()
        assert json.loads(ledger.read_text())["state"] == "disposed"
    finally:
        service.close()

    # Confirmed rejection survives a restart and does not replay its goal.
    state["boot_id"] = "after"
    restarted = create_service()
    try:
        assert restarted.items[payload["id"]]["state"] == "failed"
        restarted.prune()
        assert restarted.items[payload["id"]]["stop_confirmed"] is True
        assert not restarted.active() and restarted.threads == {}
    finally:
        restarted.close()


@pytest.mark.parametrize("unsafe", ["wrong_root", "symlink", "nonprivate", "runtime_unavailable", "lock_unavailable"])
def test_launch_validates_root_and_lock_before_writing_identity(tmp_path, monkeypatch, unsafe):
    root = tmp_path / "work"
    root.mkdir(mode=0o700)
    sandbox = HomeSandbox(root, None, {"agentic-home": tmp_path})
    monkeypatch.setattr(home_launcher, "ROOT", tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(home_launcher, "runtime_context", lambda: {})
    if unsafe == "wrong_root":
        monkeypatch.setattr(home_launcher, "ROOT", tmp_path / "other")
    elif unsafe == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        root.rmdir()
        root.symlink_to(target, target_is_directory=True)
    elif unsafe == "nonprivate":
        root.chmod(0o755)
        original_stat = Path.stat
        monkeypatch.setattr(Path, "stat", lambda path, *args, **kwargs:
            SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755) if path == root
            else original_stat(path, *args, **kwargs))
    elif unsafe == "runtime_unavailable":
        def unavailable():
            raise home_launcher.BoundaryError("home_runtime_identity_changed")
        monkeypatch.setattr(home_launcher, "runtime_context", unavailable)
    else:
        monkeypatch.setattr(home_launcher, "require_private", lambda path: None)
        def unavailable_lock():
            raise OSError("fixture lock unavailable")
        monkeypatch.setattr(sandbox, "lock", unavailable_lock)
    with pytest.raises((SandboxError, OSError)):
        sandbox.launch("a" * 32, 1, "agentic-home", "b" * 40, "pass")
    assert not list(root.glob("*.json"))
    assert not (root / "writer.lock").exists()


@pytest.mark.parametrize("existing", ["same_request", "different_request", "previous_writer"])
def test_preflight_change_never_clears_replayed_identity_or_previous_writer(tmp_path, monkeypatch, existing):
    root = tmp_path / "work"
    root.mkdir()
    sandbox = HomeSandbox(root, None, {"agentic-home": tmp_path})
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    def unreachable():
        pytest.fail("An existing writer must be resolved before resource preflight or allocation")
    monkeypatch.setattr(sandbox, "preflight", unreachable)
    previous_id, commit = "a" * 32, "b" * 40
    digest = hashlib.sha256(json.dumps([previous_id, 1, "agentic-home", commit, "pass", {}],
                                      separators=(",", ":")).encode()).hexdigest()
    before = {"job_id": previous_id, "lease_epoch": 1, "state": "stop_unconfirmed",
              "stop_confirmed": False, "request_digest": digest}
    sandbox.save(before)
    if existing == "same_request":
        observed = sandbox.launch(previous_id, 1, "agentic-home", commit, "pass")
        assert observed["state"] == "stop_unconfirmed" and observed["stop_confirmed"] is False
    elif existing == "different_request":
        with pytest.raises(SandboxError, match="Job ID reused"):
            sandbox.launch(previous_id, 1, "agentic-home", commit, "different")
    else:
        with pytest.raises(SandboxError, match="Previous writer has not been fenced"):
            sandbox.launch("c" * 32, 1, "agentic-home", commit, "pass")
    assert sandbox.record(previous_id) == before
    assert list(root.glob("*.json")) == [root / (previous_id + ".json")]


@pytest.mark.parametrize("boot", ["original-boot", "new-kernel-boot"])
def test_missing_ledger_cannot_be_replaced_by_a_stop_or_boot_claim(tmp_path, monkeypatch, boot):
    root = tmp_path / "work"
    root.mkdir()
    sandbox = HomeSandbox(root, None, {"agentic-home": tmp_path})
    monkeypatch.setattr(home_launcher, "identity", lambda:
        {"boot_id": boot, "pid_ns": 10, "cgroup_ns": 11, "mnt_ns": 12})
    for observe in (sandbox.status, sandbox.cancel, sandbox.dispose):
        with pytest.raises(SandboxError, match="Unknown job"):
            observe("a" * 32)
    assert not list(root.glob("*.json"))
