"""Home boundary cleanup must stay retryable through the shared service loop."""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))

from agentic_host.coding_service import CodingService
from agentic_runner import home_launcher
from test_home_sandbox import JOB, sandbox_fixture


@pytest.mark.parametrize("failure", ["watchdog", "fuse"])
def test_boundary_cleanup_failure_preserves_resources_and_background_retry(tmp_path, monkeypatch, failure):
    sandbox, record, writer_path = sandbox_fixture(tmp_path, monkeypatch)
    fuse_path = writer_path.with_name("f-" + "b" * 32)
    fuse_path.mkdir()
    for source in writer_path.iterdir():
        (fuse_path / source.name).write_bytes(source.read_bytes())
    record.update(state="cancelled", stop_confirmed=True,
                  fuse_cgroup={**record["cgroup"], "name": fuse_path.name, "inode": fuse_path.stat().st_ino})
    sandbox.save(record)
    blocked = True
    events = []
    kill_group = home_launcher.kill_group

    def stop_watchdog(identity):
        events.append("watchdog")
        if blocked and failure == "watchdog":
            raise home_launcher.BoundaryError("home_launcher_stop_unconfirmed")

    def stop_group(group):
        if group["name"] == fuse_path.name:
            events.append("fuse")
            if blocked and failure == "fuse":
                raise home_launcher.BoundaryError("home_writer_stop_unconfirmed")
        return kill_group(group)

    monkeypatch.setattr(home_launcher, "stop_trusted_process", stop_watchdog)
    monkeypatch.setattr(home_launcher, "kill_group", stop_group)
    original_rmdir = Path.rmdir

    def remove_group(path):
        assert not blocked and path in {writer_path, fuse_path}
        saved = sandbox.record(JOB)
        assert saved["cleanup_fenced"] and saved["stop_confirmed"]
        for child in path.iterdir():
            child.unlink()
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", remove_group)
    service = CodingService.__new__(CodingService)
    service.root, service.worker_id = tmp_path, "home-coder"
    service.sandbox = lambda: sandbox
    service.lock = threading.RLock()
    job = {"id": JOB, "lease_epoch": 1, "state": "failed", "stop_confirmed": True,
           "sandbox_attempted": True, "results_collected": True, "retained_until": time.time() + 86400}
    service.items = {JOB: job}
    published = []
    service.publish = lambda value: published.append(dict(value))
    service.persist = lambda: None
    passes = 0

    def next_pass(timeout):
        nonlocal blocked, passes
        assert timeout == 60
        passes += 1
        if passes == 2:
            # The first failure is retained, not mistaken for completed cleanup,
            # and must not have terminated the real maintenance loop.
            assert job["cleanup_pending"] is True and not job.get("workspace_released")
            assert (sandbox.root / JOB).is_dir() and writer_path.exists() and fuse_path.exists()
            saved = sandbox.record(JOB)
            assert saved["state"] == "cancelled" and saved["stop_confirmed"] is True
            assert bool(saved.get("cleanup_fenced")) == (failure == "fuse")
            assert not saved.get("removed_groups")
            blocked = False
        return passes == 3

    service.closed = SimpleNamespace(wait=next_pass)
    service.maintain()
    assert passes == 3 and job["workspace_released"] is True and "cleanup_pending" not in job
    assert published[-1]["workspace_released"] is True
    assert sandbox.record(JOB)["state"] == "disposed"
    assert not (sandbox.root / JOB).exists() and not writer_path.exists() and not fuse_path.exists()
    assert events == (["watchdog", "watchdog", "fuse"] if failure == "watchdog"
                      else ["watchdog", "fuse", "fuse"])
