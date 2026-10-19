"""Startup authority markers fence OS writes, including unreadable markers."""
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import appliance, appliance_status
from agentic_host.daemon import appliance_startup


@pytest.fixture
def startup(tmp_path, monkeypatch):
    manager = SimpleNamespace(root=tmp_path, run=lambda *args: None, locked=nullcontext)
    calls = []
    monkeypatch.setattr(appliance, "reconcile_appliance", lambda value: calls.append(("reconcile", value)))
    monkeypatch.setattr(appliance_status, "observe_network", lambda *args: calls.append(("observe", args)))
    return manager, calls


def test_fresh_install_retains_fixed_appliance_reconciliation(startup):
    manager, calls = startup
    appliance_startup(manager)
    assert [call[0] for call in calls] == ["reconcile", "observe"]
    assert not (manager.root / "appliance-update-managed.json").exists()


@pytest.mark.parametrize("kind", ["valid", "malformed", "directory", "symlink"])
def test_any_marker_inode_suppresses_writes_and_remains_untouched(startup, kind):
    manager, calls = startup
    marker = manager.root / "appliance-update-managed.json"
    if kind == "directory":
        marker.mkdir()
    elif kind == "symlink":
        marker.symlink_to(manager.root / "absent-target")
    else:
        marker.write_text('{"format":1,"candidate_digest":"' + "a" * 64 + '","authority":"host_update"}' if kind == "valid" else "invalid-json")
    before = marker.lstat()
    appliance_startup(manager)
    assert [call[0] for call in calls] == ["observe"]
    assert marker.lstat() == before


def test_unreadable_marker_fails_closed_but_readonly_observation_continues(startup, monkeypatch):
    manager, calls = startup
    original = Path.lstat
    def denied(path, *args, **kwargs):
        if path == manager.root / "appliance-update-managed.json":
            raise PermissionError("fixture")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", denied)
    appliance_startup(manager)
    assert [call[0] for call in calls] == ["observe"]


def test_marker_is_rechecked_inside_shared_lifecycle_lock(startup):
    manager, calls = startup
    @contextmanager
    def acquired():
        (manager.root / "appliance-update-managed.json").write_text("updater captured preimages")
        yield
    manager.locked = acquired
    appliance_startup(manager)
    assert [call[0] for call in calls] == ["observe"]


def test_busy_lifecycle_lock_never_allows_startup_os_writes(startup):
    manager, calls = startup
    @contextmanager
    def busy():
        raise RuntimeError("fixture lifecycle busy")
        yield
    manager.locked = busy
    appliance_startup(manager)
    assert [call[0] for call in calls] == ["observe"]
