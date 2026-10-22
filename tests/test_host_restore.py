import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host.host_restore import GIB, HostRestore
from agentic_host.manager import HostError

CANDIDATE = "a" * 64


def volume(name="root", *, percent=None, origin=""):
    return {"lv_uuid": name + "-uuid", "vg_uuid": "volume-uuid", "vg_name": "volume", "lv_name": name,
            "lv_path": "/dev/volume/" + name, "origin_uuid": origin,
            "lv_attr": "owi-aos---" if percent is None else "swi-a-s---", "lv_size": str(100 * GIB if percent is None else 10 * GIB),
            "data_percent": "" if percent is None else str(percent), "vg_free": str(100 * GIB),
            "vg_extent_size": str(4 * 1024**2), "lv_tags": ""}


@pytest.fixture
def restore(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    for name in ("haos.qcow2", "nvram.fd"):
        (images / name).write_bytes(b"synthetic-disk")
    manager = SimpleNamespace(root=tmp_path, images=images, rows=[volume()], source="/dev/volume/root",
                              calls=[], vm="running", provisioned=True, snapshots=0, saved=[], fail_snapshot=False,
                              fail_create=False, extend_readback=True)

    def run(*args, **kwargs):
        manager.calls.append(args)
        if args[0] == "findmnt":
            return SimpleNamespace(stdout=manager.source)
        if args[0] == "lvs":
            return SimpleNamespace(stdout=json.dumps({"report": [{"lv": manager.rows}]}))
        if args[0] == "lvextend":
            item = next(row for row in manager.rows if row["lv_path"] == args[-1])
            added = int(args[3]) * int(manager.rows[0]["vg_extent_size"])
            if manager.extend_readback:
                size = int(item["lv_size"])
                item["data_percent"] = str(float(item["data_percent"]) * size / (size + added))
                item["lv_size"] = str(size + added)
                for row in manager.rows:
                    row["vg_free"] = str(int(row["vg_free"]) - added)
            return SimpleNamespace(stdout="")
        assert args[0] == "lvcreate"
        if manager.fail_create:
            raise HostError("synthetic lvcreate failure")
        name = args[args.index("--name") + 1]
        assert not any(row["lv_name"] == name for row in manager.rows)
        snapshot = volume(name, percent=0, origin="root-uuid")
        snapshot["lv_size"] = args[args.index("--size") + 1][:-1]
        snapshot["lv_tags"] = ",".join(args[index + 1] for index, value in enumerate(args) if value == "--addtag")
        for row in manager.rows:
            row["vg_free"] = str(int(row["vg_free"]) - int(snapshot["lv_size"]))
        manager.rows.append(snapshot)
        return SimpleNamespace(stdout="")

    def shutdown():
        assert manager.saved[-1]["restore_point"]["phase"] == "ha_shutdown_requested"
        manager.vm = "shut off"

    def snapshot():
        assert manager.saved[-1]["restore_point"]["phase"] == "ha_snapshot_requested"
        assert manager.vm == "shut off"
        manager.snapshots += 1
        if manager.fail_snapshot:
            raise HostError("synthetic snapshot failure")
        return {"snapshot": "20261019T000000Z-1234abcd"}

    def start():
        manager.vm = "running"

    manager.run, manager.shutdown, manager.snapshot, manager.start = run, shutdown, snapshot, start
    manager.state = lambda: {"owner": "ha-owner"} if manager.provisioned else {}
    manager.owned = lambda: object()
    manager.virsh = lambda *args, **kwargs: SimpleNamespace(stdout=manager.vm)
    return HostRestore(manager, save=lambda record: manager.saved.append(copy.deepcopy(record)))


def test_creates_ha_then_tagged_root_restore_points_once_and_restores_running_state(restore):
    record = {"candidate_digest": CANDIDATE, "restore_required_bytes": GIB}
    receipt = restore.create(record)
    assert receipt["phase"] == "complete" and receipt["ha_snapshot"] == "20261019T000000Z-1234abcd"
    assert receipt["root_lv_uuid"] == "root-uuid" and receipt["vg_uuid"] == "volume-uuid"
    assert receipt["root_snapshot_uuid"] and receipt["root_snapshot_tag"].endswith(CANDIDATE)
    assert restore.manager.vm == "running" and restore.manager.snapshots == 1
    assert restore.create(record) == receipt and restore.manager.snapshots == 1
    assert len([call for call in restore.manager.calls if call[0] == "lvcreate"]) == 1
    assert all(call[0] in {"findmnt", "lvs", "lvcreate"} for call in restore.manager.calls)


@pytest.mark.parametrize("state", ["shut off", "not_created"])
def test_stopped_or_missing_ha_never_starts_it(restore, state):
    restore.manager.vm = state
    restore.manager.provisioned = state != "not_created"
    receipt = restore.create({"candidate_digest": CANDIDATE})
    assert restore.manager.vm == state
    assert bool(receipt["ha_snapshot"]) is (state == "shut off")


@pytest.mark.parametrize("source", ["overlay", "/dev/unknown/root"])
def test_unsupported_root_fails_without_any_mutation(restore, source):
    restore.manager.source = source
    with pytest.raises(HostError):
        restore.create({"candidate_digest": CANDIDATE})
    assert not restore.manager.saved and restore.manager.snapshots == 0
    assert not any(call[0] in {"lvcreate", "lvextend"} for call in restore.manager.calls)


@pytest.mark.parametrize("fault", ["root_thin", "root_snapshot", "root_uuid", "root_path", "root_extent", "no_free"])
def test_unsafe_lvm_state_fails_closed(restore, fault):
    root = restore.manager.rows[0]
    if fault == "root_thin":
        root["lv_attr"] = "Vwi-a-tz--"
    elif fault == "root_snapshot":
        root["origin_uuid"] = "another-uuid"
    elif fault == "root_uuid":
        root["lv_uuid"] = ""
    elif fault == "root_path":
        root["lv_name"] = "../elsewhere"
    elif fault == "root_extent":
        root["vg_extent_size"] = "nan"
    else:
        root["vg_free"] = "0"
    with pytest.raises(HostError):
        restore.capacity(GIB)
    assert restore.manager.vm == "running" and restore.manager.snapshots == 0
    assert not any(call[0] == "lvcreate" for call in restore.manager.calls)


def test_narrow_fallback_extends_only_exact_root_snapshot_and_reads_back(restore):
    restore.manager.rows += [volume("existing", percent=65, origin="root-uuid"), volume("unrelated", percent=99, origin="another-uuid")]
    result = restore.capacity(GIB)
    assert result["root_lv_uuid"] == "root-uuid"
    calls = [call for call in restore.manager.calls if call[0] == "lvextend"]
    assert len(calls) == 1 and calls[0][-1] == "/dev/volume/existing"
    assert restore.manager.vm == "running"


@pytest.mark.parametrize("percent,attr", [(85, "swi-a-s---"), (100, "swi-I-s---"), (45, "Swi-a-s---")])
def test_critical_invalid_or_merging_snapshot_never_extended_or_ha_stopped(restore, percent, attr):
    restore.manager.rows.append({**volume("existing", percent=percent, origin="root-uuid"), "lv_attr": attr})
    with pytest.raises(HostError):
        restore.capacity(GIB)
    assert restore.manager.vm == "running"
    assert not any(call[0] in {"lvextend", "lvcreate"} for call in restore.manager.calls)


def test_failed_extension_readback_never_authorizes_update(restore):
    restore.manager.rows.append(volume("existing", percent=65, origin="root-uuid"))
    restore.manager.extend_readback = False
    with pytest.raises(HostError, match="無法確認"):
        restore.capacity(GIB)


def test_uses_newer_root_cow_guard_without_monitor_or_shutdown(restore):
    calls = []
    restore.manager.root_cow = SimpleNamespace(require_capacity=calls.append)
    restore.capacity(GIB)
    assert calls == [GIB] and restore.manager.vm == "running"


def test_interrupted_ha_snapshot_never_repeated_and_failure_restarts_original_ha(restore):
    restore.manager.fail_snapshot = True
    record = {"candidate_digest": CANDIDATE}
    with pytest.raises(HostError, match="snapshot failure"):
        restore.create(record)
    assert restore.manager.vm == "running" and restore.manager.snapshots == 1
    with pytest.raises(HostError, match="不會重複"):
        restore.create(record)
    assert restore.manager.snapshots == 1
    assert not any(call[0] == "lvcreate" for call in restore.manager.calls)


def test_uncertain_root_snapshot_is_verified_by_exact_candidate_tags_not_duplicated(restore):
    record = {"candidate_digest": CANDIDATE}
    restore.create(record)
    record["restore_point"]["phase"] = "root_snapshot_requested"
    assert restore.create(record)["phase"] == "complete"
    assert len([call for call in restore.manager.calls if call[0] == "lvcreate"]) == 1


def test_failed_root_create_does_not_retry_or_adopt_unowned_snapshot(restore):
    restore.manager.fail_create = True
    record = {"candidate_digest": CANDIDATE}
    with pytest.raises(HostError):
        restore.create(record)
    restore.manager.fail_create = False
    with pytest.raises(HostError, match="不會重複"):
        restore.create(record)
    assert len([call for call in restore.manager.calls if call[0] == "lvcreate"]) == 1


def test_complete_receipt_rechecks_snapshot_existence_before_reuse(restore):
    record = {"candidate_digest": CANDIDATE}
    restore.create(record)
    restore.manager.rows.pop()
    with pytest.raises(HostError, match="已改變"):
        restore.create(record)


def test_missing_persistence_callback_prevents_ha_or_lvm_mutation(restore):
    restore.save = None
    with pytest.raises(HostError, match="持久記錄"):
        restore.create({"candidate_digest": CANDIDATE})
    assert restore.manager.snapshots == 0 and restore.manager.vm == "running"
    assert not any(call[0] == "lvcreate" for call in restore.manager.calls)


@pytest.mark.parametrize("phase", ["ha_shutdown_requested", "ha_snapshot_requested", "ha_snapshot_complete"])
def test_interrupted_ha_phase_restores_original_availability_but_never_duplicates(restore, phase):
    restore.manager.vm = "shut off"
    record = {"candidate_digest": CANDIDATE, "restore_point": {"phase": phase, "ha_original_state": "running"}}
    with pytest.raises(HostError, match="不會重複"):
        restore.create(record)
    assert restore.manager.vm == "running" and restore.manager.snapshots == 0
    assert not any(call[0] == "lvcreate" for call in restore.manager.calls)


def test_shutdown_timeout_that_actually_stopped_guest_restarts_it_before_failing(restore):
    def shutdown():
        restore.manager.vm = "shut off"
        raise HostError("synthetic timeout after shutdown")

    restore.manager.shutdown = shutdown
    with pytest.raises(HostError, match="synthetic timeout"):
        restore.create({"candidate_digest": CANDIDATE})
    assert restore.manager.vm == "running" and restore.manager.snapshots == 0


def test_durable_journal_failure_after_shutdown_restarts_original_guest(restore):
    original = restore.save

    def failing_save(record):
        if record["restore_point"]["phase"] == "ha_snapshot_requested":
            raise OSError("synthetic journal failure")
        original(record)

    restore.save = failing_save
    with pytest.raises(OSError, match="journal failure"):
        restore.create({"candidate_digest": CANDIDATE})
    assert restore.manager.vm == "running" and restore.manager.snapshots == 0


def test_root_snapshot_after_creation_rechecks_other_existing_snapshot_capacity(restore):
    restore.manager.rows.append(volume("existing", percent=40, origin="root-uuid"))
    original = restore.manager.run

    def concurrent_write(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[0] == "lvcreate":
            restore.manager.rows[1]["data_percent"] = "90"
        return value

    restore.manager.run = concurrent_write
    with pytest.raises(HostError, match="建立後空間不足"):
        restore.create({"candidate_digest": CANDIDATE})
    assert restore.manager.vm == "running"


def test_root_snapshot_receipt_is_not_used_after_growth_exhausts_headroom(restore):
    record = {"candidate_digest": CANDIDATE}
    restore.create(record)
    restore.manager.rows[-1]["data_percent"] = "79"
    with pytest.raises(HostError, match="空間不足"):
        restore.create(record)


@pytest.mark.parametrize("pending", ["restore", "lan"])
def test_unresolved_ha_restore_or_network_transaction_does_not_shut_guest_down(restore, pending):
    if pending == "restore":
        restore.manager.state = lambda: {"owner": "ha-owner", "restore_pending": "earlier-snapshot"}
    else:
        (restore.manager.root / "ha-lan.json").write_text(json.dumps({"contract_version": 1, "choice": None, "pending": {"target": None}}))
    with pytest.raises(HostError, match="未完成"):
        restore.create({"candidate_digest": CANDIDATE})
    assert restore.manager.vm == "running" and not restore.manager.saved


def test_filesystem_capacity_is_reserved_before_any_lvm_mutation_or_guest_stop(restore, monkeypatch):
    monkeypatch.setattr("agentic_host.host_restore.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(HostError, match="磁碟可用空間不足"):
        restore.capacity(GIB)
    assert restore.manager.vm == "running" and not restore.manager.calls


def test_recover_ha_verifies_original_running_availability_without_creating_snapshot(restore):
    restore.manager.vm = "shut off"
    record = {"restore_point": {"phase": "ha_snapshot_requested", "ha_original_state": "running"}}
    assert restore.recover_ha(record) == {"state": "running_verified", "vm": "running"}
    assert restore.manager.snapshots == 0 and record["restore_point"]["phase"] == "ha_snapshot_requested"
    assert record["restore_point"]["ha_recovery"] == "running_verified"


def test_recover_ha_requires_readback_and_preserves_manually_started_guest(restore):
    restore.manager.start = lambda: None
    restore.manager.vm = "shut off"
    with pytest.raises(HostError, match="尚未確認"):
        restore.recover_ha({"restore_point": {"ha_original_state": "running"}})
    restore.manager.vm = "running"
    assert restore.recover_ha({"restore_point": {"ha_original_state": "shut off"}})["state"] == "owner_state_preserved"
    assert restore.manager.snapshots == 0


@pytest.mark.parametrize("pending", ["restore", "lan"])
def test_completed_receipt_reuse_cannot_skip_live_restore_or_lan_journal(restore, pending):
    record = {"candidate_digest": CANDIDATE}
    restore.create(record)
    if pending == "restore":
        restore.manager.state = lambda: {"owner": "ha-owner", "restore_pending": "earlier-snapshot"}
    else:
        (restore.manager.root / "ha-lan.json").write_text(json.dumps({"contract_version": 1, "choice": None, "pending": {"target": None}}))
    with pytest.raises(HostError, match="未完成"):
        restore.create(record)
    assert restore.manager.snapshots == 1
