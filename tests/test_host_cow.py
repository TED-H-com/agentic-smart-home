import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.cow import FIELDS, MARGIN, RootCow
from agentic_host.manager import HostError, atomic

GIB = 1024**3


def row(name="restore", percent=60, *, origin="root-uuid", vg="volume-uuid"):
    return {"lv_uuid": name + "-uuid", "vg_uuid": vg, "vg_name": "volume", "lv_name": name,
            "lv_path": "/dev/volume/" + name, "origin_uuid": origin, "lv_attr": "swi-a-s---",
            "data_percent": str(percent), "lv_size": str(10 * GIB),
            "vg_free": str(30 * GIB), "vg_extent_size": str(4 * 1024**2)}


@pytest.fixture
def guard(tmp_path):
    root = {**row("root"), "lv_uuid": "root-uuid", "origin_uuid": "", "lv_attr": "owi-aos---"}
    manager = SimpleNamespace(root=tmp_path, rows=[root, row()], calls=[], shutdowns=0,
                              source="/dev/volume/root", fail_extend=False, fail_observe=False,
                              readback=True)

    def execute(*args, **kwargs):
        manager.calls.append(args)
        if manager.fail_observe:
            raise RuntimeError("private diagnostic details")
        if args[0] == "findmnt":
            assert args == ("findmnt", "--noheadings", "--output", "SOURCE", "--target", "/")
            return SimpleNamespace(stdout=manager.source)
        if args[0] == "lvs":
            assert args == ("lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "--options", FIELDS)
            return SimpleNamespace(stdout=json.dumps({"report": [{"lv": manager.rows}]}))
        assert args[:4] == ("lvextend", "--yes", "--extents", args[3])
        assert args[4] == "--" and args[3].startswith("+")
        if manager.fail_extend:
            raise HostError("extension failed")
        item = next(item for item in manager.rows if item["lv_path"] == args[5])
        if manager.readback:
            increase = int(args[3]) * int(item["vg_extent_size"])
            old_size = int(item["lv_size"])
            item["lv_size"] = str(old_size + increase)
            item["data_percent"] = str(float(item["data_percent"]) * old_size / (old_size + increase))
            for current in manager.rows:
                current["vg_free"] = str(max(0, int(current["vg_free"]) - increase))
        return SimpleNamespace(stdout="", returncode=0)

    def shutdown():
        manager.shutdowns += 1

    manager.run, manager.shutdown = execute, shutdown
    manager.locked = nullcontext
    return RootCow(manager)


def test_status_warns_at_sixty_without_mutating_or_exposing_paths(guard):
    status = guard.status()
    assert status["state"] == "warning" and status["used_percent"] == 60
    assert "60%" in status["message"] and status["enrolled"] is False
    assert "/dev/" not in json.dumps(status)
    assert not guard.path.exists()
    assert not any(call[0] == "lvextend" for call in guard.manager.calls)


def test_reconcile_pins_mounted_root_and_extends_with_verified_readback(guard):
    result = guard.reconcile()
    assert json.loads(guard.path.read_text()) == {"schema": 1, "root_lv_uuid": "root-uuid", "vg_uuid": "volume-uuid"}
    assert guard.path.stat().st_mode & 0o777 == 0o600
    assert result["state"] == "healthy" and result["used_percent"] <= 50
    assert "增加" in result["last_action"]
    extensions = [call for call in guard.manager.calls if call[0] == "lvextend"]
    assert len(extensions) == 1 and extensions[0][-1] == "/dev/volume/restore"


def test_enrollment_survives_name_change_and_refuses_replaced_root(guard):
    guard.reconcile()
    guard.manager.rows[0]["lv_uuid"] = "replacement-uuid"
    guard.manager.calls.clear()
    assert guard.status()["state"] == "unavailable"
    with pytest.raises(HostError, match="原有還原點已保留"):
        guard.require_capacity(GIB)
    assert not any(call[0] == "lvextend" for call in guard.manager.calls)


@pytest.mark.parametrize("field,value", [("origin_uuid", "another-uuid"), ("vg_uuid", "another-vg"),
                                         ("lv_attr", "Vwi-a-tz--")])
def test_never_extends_unrelated_or_thin_volumes(guard, field, value):
    guard.manager.rows[1][field] = value
    assert guard.reconcile()["state"] == "not_applicable"
    assert not any(call[0] == "lvextend" for call in guard.manager.calls)


@pytest.mark.parametrize("field,value", [("lv_name", "--foreign"), ("lv_path", "/dev/foreign"),
                                         ("lv_name", "../foreign"), ("data_percent", "nan"),
                                         ("data_percent", ""), ("vg_extent_size", "0")])
def test_ambiguous_snapshot_contract_blocks_new_copies(guard, field, value):
    guard.manager.rows[1][field] = value
    with pytest.raises(HostError, match="已停止新增備份"):
        guard.require_capacity(GIB)
    assert not any(call[0] == "lvextend" for call in guard.manager.calls)


@pytest.mark.parametrize("percent,attr", [(100, "swi-I-s---"), ("", "swi-I-s---")])
def test_invalid_snapshot_never_reported_healthy_or_mutated(guard, percent, attr):
    guard.manager.rows[1].update(data_percent=str(percent), lv_attr=attr)
    assert guard.reconcile()["state"] == "invalid"
    with pytest.raises(HostError):
        guard.require_capacity(0)
    assert not any(call[0] == "lvextend" for call in guard.manager.calls)


def test_merge_in_progress_is_not_misreported_as_invalid_or_changed(guard):
    guard.manager.rows[1]["lv_attr"] = "Swi-a-s---"
    assert guard.status()["state"] == "unavailable"
    with pytest.raises(HostError):
        guard.require_capacity(GIB)
    assert not any(call[0] == "lvextend" for call in guard.manager.calls)


def test_bounded_extension_uses_remaining_extents_then_blocks_unsafe_copy(guard):
    guard.manager.rows[1]["vg_free"] = str(4 * 1024**2)
    with pytest.raises(HostError, match="原有還原點已保留"):
        guard.require_capacity(10 * GIB)
    extensions = [call for call in guard.manager.calls if call[0] == "lvextend"]
    assert len(extensions) == 1 and extensions[0][3] == "+1"


def test_command_success_without_capacity_readback_is_not_success(guard):
    guard.manager.readback = False
    with pytest.raises(HostError):
        guard.require_capacity(5 * GIB)
    assert guard.status()["state"] == "warning"
    assert guard.status()["last_action"] is None


def test_monitor_cleanly_stops_ha_when_no_space_and_does_not_delete_snapshot(guard):
    guard.manager.rows[1].update(data_percent="85", vg_free="0")

    class Stop:
        count = 0

        def is_set(self):
            return self.count >= 2

        def wait(self, seconds):
            assert seconds == 5
            self.count += 1

    guard.monitor(Stop())
    assert guard.manager.shutdowns == 1
    assert "HA 已正常關閉" in guard.status()["last_action"]
    assert len(guard.manager.rows) == 2
    assert all(call[0] in {"findmnt", "lvs"} for call in guard.manager.calls)


def test_failed_extension_still_attempts_graceful_protection(guard):
    guard.manager.rows[1]["data_percent"] = "85"
    guard.manager.fail_extend = True

    class Stop:
        done = False

        def is_set(self):
            return self.done

        def wait(self, seconds):
            self.done = True

    guard.monitor(Stop())
    assert guard.manager.shutdowns == 1


def test_missing_observation_is_honest_and_never_leaks_diagnostic_output(guard):
    guard.manager.fail_observe = True
    result = guard.status()
    assert result["state"] == "unavailable" and "private" not in json.dumps(result)
    with pytest.raises(HostError):
        guard.require_capacity(0)


def test_monitor_defers_shutdown_during_lifecycle_transaction(guard):
    guard.manager.rows[1].update(data_percent="85", vg_free="0")

    def locked():
        raise HostError("Another host operation is running")

    class Stop:
        done = False

        def is_set(self):
            return self.done

        def wait(self, seconds):
            self.done = True

    guard.manager.locked = locked
    guard.monitor(Stop())
    assert guard.manager.shutdowns == 0 and not guard.quiesced
    assert "尚未完成" in guard.status()["last_action"]


def test_non_lvm_mount_needs_no_snapshot_capacity(guard):
    guard.manager.source = "overlay"
    guard.require_capacity(1_000 * GIB)
    assert guard.status()["state"] == "not_applicable"
    assert not guard.path.exists()


def test_copy_capacity_accounts_for_sparse_blocks_and_metadata(guard, tmp_path, monkeypatch):
    image = tmp_path / "haos.qcow2"
    with image.open("wb") as stream:
        stream.write(b"allocated")
        stream.truncate(64 * GIB)
    captured = []
    monkeypatch.setattr(guard, "require_capacity", captured.append)
    guard.require_copy_capacity([image])
    allocated = image.stat().st_blocks * 512
    assert captured == [allocated + allocated // 10 + MARGIN]


def test_forged_enrollment_marker_cannot_redirect_extension(guard):
    atomic(guard.path, {"schema": 1, "root_lv_uuid": "restore-uuid", "vg_uuid": "volume-uuid"})
    with pytest.raises(HostError):
        guard.require_capacity(GIB)
    assert not any(call[0] == "lvextend" for call in guard.manager.calls)


def test_actual_lvm_report_fields_in_isolated_container(request):
    """Validate lvs CLI schema without host device mounts or LVM mutations."""
    import subprocess

    if not request.config.getoption("--containers"):
        pytest.skip("requires isolated LVM tooling container")
    available = subprocess.run(["docker", "image", "inspect", "agentic-home-builder:0.2.0"],
                               capture_output=True, text=True, timeout=10)
    if available.returncode and "No such image" in available.stderr:
        pytest.skip("local LVM builder image unavailable; this test never pulls or builds")
    assert available.returncode == 0, "Cannot inspect isolated LVM tooling image"
    result = subprocess.run([
        "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "128m", "--pids-limit", "32",
        "agentic-home-builder:0.2.0", "lvs", "--readonly", "--nolocking", "--reportformat", "json",
        "--units", "b", "--nosuffix", "--options", FIELDS,
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {"report": [{"lv": []}]}
