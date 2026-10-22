import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host.host_reconcile import TARGETS, HostReconcile, fingerprint
from agentic_host.manager import HostError

CANDIDATE = "b" * 64
UNIT = "/etc/systemd/system/agentic-home-host.service"


@pytest.fixture
def reconcile(tmp_path, monkeypatch):
    manager = SimpleNamespace(root=tmp_path / "state", product=tmp_path / "old", calls=[])
    manager.root.mkdir()
    product, system = tmp_path / "new", tmp_path / "system"
    old_unit = b"[Service]\nExecStart=/usr/bin/python3 -m agentic_host.daemon\n"
    new_unit = old_unit + b"Restart=on-failure\n"
    for base, content in ((manager.product, old_unit), (product, new_unit)):
        unit = base / "host/systemd/agentic-home-host.service"
        unit.parent.mkdir(parents=True)
        unit.write_bytes(content)
        unit.chmod(0o644)
    installed = system / UNIT.lstrip("/")
    installed.parent.mkdir(parents=True)
    installed.write_bytes(old_unit)
    installed.chmod(0o644)

    def run(*args, **kwargs):
        manager.calls.append(args)
        assert args[0] in {"systemctl", "netplan"}
        if args[:2] == ("systemctl", "show"):
            return SimpleNamespace(stdout="/etc/systemd/system/" + args[-1])
        return SimpleNamespace(stdout="", returncode=0)

    manager.run = run
    reconciler = HostReconcile(manager, product=product, system_root=system)
    # Real ownership checking is replaced only for the confined non-root fixture.
    original = HostReconcile._trusted

    def confined(path, *, optional=False):
        assert Path(path).is_relative_to(tmp_path)
        for item in (path, *path.parents):
            if item.is_symlink():
                raise HostError("Host update configuration path is untrusted")
        if not optional and not path.exists():
            raise HostError("Host update configuration file is missing")

    monkeypatch.setattr(reconciler, "_trusted", confined)
    reconciler.original_trusted = original
    return reconciler


def test_prepare_apply_health_and_rollback_roundtrip_preserves_file_mode(reconcile):
    record = reconcile.prepare(CANDIDATE)
    before = reconcile._path(UNIT).read_bytes()
    assert record["phase"] == "prepared" and reconcile._path(UNIT).read_bytes() == before
    assert reconcile.apply(CANDIDATE)["state"] == "applied"
    assert reconcile._path(UNIT).read_bytes() != before
    assert reconcile._path(UNIT).stat().st_mode & 0o777 == 0o644
    assert reconcile.health(CANDIDATE)["state"] == "healthy"
    assert reconcile.rollback(CANDIDATE) == {"state": "rolled_back", "owner_modified_preserved": []}
    assert reconcile._path(UNIT).read_bytes() == before
    assert reconcile.rollback(CANDIDATE)["state"] == "rolled_back"
    assert not any(call[:2] in {("systemctl", "restart"), ("systemctl", "start")} for call in reconcile.manager.calls)


def test_apply_is_idempotent_after_journaled_write_loses_response(reconcile, monkeypatch):
    reconcile.prepare(CANDIDATE)
    original = reconcile._write
    calls = []

    def interrupted(target, image):
        original(target, image)
        calls.append(target)
        raise OSError("synthetic lost response")

    monkeypatch.setattr(reconcile, "_write", interrupted)
    with pytest.raises(OSError):
        reconcile.apply(CANDIDATE)
    monkeypatch.setattr(reconcile, "_write", original)
    assert reconcile.apply(CANDIDATE)["state"] == "applied"
    assert len(calls) == 1


def test_existing_owner_edit_blocks_first_adoption(reconcile):
    reconcile._path(UNIT).write_bytes(b"owner custom service")
    with pytest.raises(HostError, match="自行修改"):
        reconcile.prepare(CANDIDATE)
    assert reconcile._path(UNIT).read_bytes() == b"owner custom service"
    assert not reconcile._journal(CANDIDATE).exists()


def test_owner_edit_between_prepare_and_apply_is_preserved(reconcile):
    reconcile.prepare(CANDIDATE)
    reconcile._path(UNIT).write_bytes(b"new owner custom service")
    with pytest.raises(HostError, match="已保留原設定"):
        reconcile.apply(CANDIDATE)
    assert reconcile._path(UNIT).read_bytes() == b"new owner custom service"


def test_rollback_never_overwrites_owner_edit_after_apply(reconcile):
    reconcile.prepare(CANDIDATE)
    reconcile.apply(CANDIDATE)
    reconcile._path(UNIT).write_bytes(b"owner changed after update")
    result = reconcile.rollback(CANDIDATE)
    assert result["owner_modified_preserved"] == [UNIT]
    assert reconcile._path(UNIT).read_bytes() == b"owner changed after update"
    assert UNIT not in json.loads(reconcile.ledger.read_text())


def test_rollback_does_not_require_either_product_tree(reconcile):
    import shutil

    reconcile.prepare(CANDIDATE)
    reconcile.apply(CANDIDATE)
    shutil.rmtree(reconcile.product)
    shutil.rmtree(reconcile.manager.product)
    assert reconcile.rollback(CANDIDATE)["state"] == "rolled_back"


@pytest.mark.parametrize("target", ["/etc/systemd/system/agentic-home-update.service",
    "/etc/systemd/system/agentic-home-update-recover.service", "/etc/systemd/system/agentic-home-firstboot.service",
    "/etc/shadow", "/etc/systemd/system/../../shadow", "/etc/netplan/owner.yaml"])
def test_manifest_cannot_touch_engine_firstboot_or_arbitrary_paths(reconcile, target):
    manifest = reconcile.product / "host/managed-system.json"
    manifest.write_text(json.dumps({"format": 1, "files": [{"target": target,
        "source": "host/systemd/agentic-home-host.service", "mode": 0o644}]}))
    with pytest.raises(HostError, match="not allowed"):
        reconcile.prepare(CANDIDATE)
    assert not reconcile.manager.calls


@pytest.mark.parametrize("extra", [{"command": "whoami"}, {"hooks": ["sudo true"]}, {"url": "https://example.invalid"}])
def test_manifest_does_not_accept_commands_hooks_or_network_locations(reconcile, extra):
    (reconcile.product / "host/managed-system.json").write_text(json.dumps({"format": 1, "files": [], **extra}))
    with pytest.raises(HostError, match="manifest is invalid"):
        reconcile.prepare(CANDIDATE)


@pytest.mark.parametrize("location", ["target", "source", "journal", "temporary", "ledger"])
def test_symlinks_never_redirect_configuration_or_backups(reconcile, location):
    outside = reconcile.manager.root / "outside"
    outside.write_text("unchanged")
    if location in {"target", "source"}:
        path = reconcile._path(UNIT) if location == "target" else reconcile.product / "host/systemd/agentic-home-host.service"
        path.unlink()
        path.symlink_to(outside)
        with pytest.raises(HostError, match="untrusted"):
            reconcile.prepare(CANDIDATE)
    else:
        reconcile.prepare(CANDIDATE)
        path = (reconcile._journal(CANDIDATE) if location == "journal" else reconcile.ledger if location == "ledger"
                else reconcile._path(UNIT).with_name("agentic-home-host.service.agentic-new"))
        path.unlink(missing_ok=True)
        path.symlink_to(outside)
        with pytest.raises(HostError, match="untrusted"):
            reconcile.apply(CANDIDATE)
    assert outside.read_text() == "unchanged"


def test_health_checks_actual_unit_file_and_service_activity(reconcile):
    reconcile.prepare(CANDIDATE)
    reconcile.apply(CANDIDATE)
    reconcile.health(CANDIDATE)
    assert ("systemctl", "show", "--property=FragmentPath", "--value", "agentic-home-host.service") in reconcile.manager.calls
    assert ("systemctl", "is-active", "--quiet", "agentic-home-host.service") in reconcile.manager.calls
    reconcile._path(UNIT).write_bytes(b"unexpected drift")
    with pytest.raises(HostError, match="readback mismatch"):
        reconcile.health(CANDIDATE)


def test_optional_managed_os_files_have_fixed_sources_actions_and_reboot_scope(reconcile):
    logind = "/etc/systemd/logind.conf.d/60-agentic-home.conf"
    netplan = "/etc/netplan/60-agentic-home.yaml"
    folder = reconcile.product / "host/managed"
    folder.mkdir()
    (folder / "logind.conf").write_text("[Login]\nHandleLidSwitch=ignore\n")
    (folder / "netplan.yaml").write_text("network:\n  version: 2\n")
    entries = [{"target": target, "source": TARGETS[target][0], "mode": TARGETS[target][1]} for target in (logind, netplan)]
    (reconcile.product / "host/managed-system.json").write_text(json.dumps({"format": 1, "files": entries}))
    reconcile.prepare(CANDIDATE)
    assert reconcile.apply(CANDIDATE) == {"state": "applied", "reboot_required": True}
    assert ("systemctl", "kill", "--kill-whom=main", "--signal=HUP", "systemd-logind.service") in reconcile.manager.calls
    assert ("netplan", "generate") in reconcile.manager.calls
    assert not any(call[:2] == ("netplan", "apply") for call in reconcile.manager.calls)
    reconcile.rollback(CANDIDATE)
    assert not reconcile._path(logind).exists() and not reconcile._path(netplan).exists()


def test_new_managed_file_is_removed_on_rollback_and_mode_only_owner_edit_is_kept(reconcile):
    reconcile._path(UNIT).unlink()
    reconcile.prepare(CANDIDATE)
    reconcile.apply(CANDIDATE)
    reconcile._path(UNIT).chmod(0o600)
    assert reconcile.rollback(CANDIDATE)["owner_modified_preserved"] == [UNIT]
    assert reconcile._path(UNIT).stat().st_mode & 0o777 == 0o600


def test_journal_digest_tampering_fails_before_side_effects(reconcile):
    reconcile.prepare(CANDIDATE)
    path = reconcile._journal(CANDIDATE)
    value = json.loads(path.read_text())
    value["files"][0]["after"]["sha256"] = fingerprint(b"different")
    path.write_text(json.dumps(value))
    with pytest.raises(HostError, match="preimage is invalid"):
        reconcile.apply(CANDIDATE)
    assert not reconcile.manager.calls


def test_real_path_checker_rejects_non_root_and_writable_modes(reconcile, tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("public")
    original_stat = Path.stat

    def fake_stat(path, **kwargs):
        info = original_stat(path, **kwargs)
        return SimpleNamespace(st_uid=1000 if path == target else 0, st_mode=info.st_mode)

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(HostError, match="ownership is untrusted"):
        reconcile.original_trusted(target)
