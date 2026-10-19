"""Confined root-policy roundtrips; no host services, links or NICs are changed."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import appliance_update as module
from agentic_host.manager import HostError
from test_host_reconcile import CANDIDATE, reconcile  # noqa: F401


@pytest.fixture
def appliance(reconcile, monkeypatch):  # noqa: F811 - imported pytest fixture
    adapter = reconcile._appliance()
    profile = adapter.path(module.PROFILE)
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"appliance_lid": True, "desktop": True}))
    netplan = adapter.root / "etc/netplan/01-owner.yaml"
    netplan.parent.mkdir(parents=True)
    netplan.write_text("network:\n  version: 2\n  renderer: networkd\n  ethernets:\n    eno1:\n      dhcp4: true\n")
    (reconcile.product / "host/managed-system.json").write_text(json.dumps({
        "format": 1, "files": [], "appliance_policy": 1}))
    calls, live = [], {"power": False}
    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("busctl", "get-property"):
            return SimpleNamespace(returncode=0, stdout='s "ignore"' if live["power"] else 's "suspend"')
        if args[:2] == ("systemctl", "is-enabled"):
            target = adapter.root / "etc/systemd/system" / args[-1]
            return SimpleNamespace(returncode=1, stdout="masked" if target.is_symlink() else "static")
        if args[:2] == ("systemctl", "kill"):
            live["power"] = adapter.path(module.LOGIND).exists()
        return SimpleNamespace(returncode=0, stdout="")
    reconcile.manager.run = run
    original = Path.lstat
    def owned_lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        fields = list(info)
        fields[4] = 0
        return os.stat_result(fields)
    monkeypatch.setattr(Path, "lstat", owned_lstat)
    adapter.calls, adapter.live, adapter.config = calls, live, netplan
    return adapter


def test_signed_optin_prepares_all_policy_preimages_without_effects_then_reverts(appliance):
    owner = appliance.owner
    profile = appliance.path(module.DCONF_PROFILE)
    profile.parent.mkdir(parents=True)
    profile.write_text("user-db:user\nsystem-db:owner\n")
    profile.chmod(0o644)
    owner.prepare(CANDIDATE)
    value = appliance.load(CANDIDATE)
    assert len(value["files"]) == 13
    assert not appliance.path(module.LOGIND).exists()
    assert appliance.marker.exists()
    assert all(call[:2] in {("netplan", "info"), ("dconf", "help"), ("systemctl", "--version"),
                           ("busctl", "--version")} for call in appliance.calls)
    assert owner.apply(CANDIDATE)["reboot_required"] is True
    assert owner.health(CANDIDATE)["state"] == "healthy"
    assert all(appliance.path(target).readlink() == Path("/dev/null") for target in module.MASKS)
    assert profile.read_text() == "user-db:user\nsystem-db:owner\nsystem-db:local\n"
    overlay = module.network._yaml().safe_load(appliance.path(module.NETPLAN).read_text())
    assert overlay["network"]["ethernets"]["eno1"]["dhcp-identifier"] == "duid"
    assert appliance.config.read_text().endswith("dhcp4: true\n")
    assert not any(call[0] in {"ethtool", "nmcli", "apt-get"} or call[:2] in {
        ("netplan", "apply"), ("systemctl", "restart"), ("systemctl", "unmask")} for call in appliance.calls)
    assert owner.rollback(CANDIDATE)["state"] == "rolled_back"
    assert profile.read_text() == "user-db:user\nsystem-db:owner\n"
    assert all(not appliance.path(target).is_symlink() for target in module.MASKS)
    assert not appliance.path(module.LOGIND).exists() and not appliance.path(module.NETPLAN).exists()
    assert appliance.marker.exists(), "old daemon must not silently reapply reverted policy"


@pytest.mark.parametrize("target", [module.LOGIND, module.DCONF, module.NETPLAN, module.LINK, module.NM])
def test_unknown_owner_policy_blocks_adoption_before_any_write(appliance, target):
    path = appliance.path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# owner's policy\n")
    with pytest.raises(HostError, match="自行修改"):
        appliance.owner.prepare(CANDIDATE)
    assert not appliance.marker.exists() and not appliance.journal(CANDIDATE).exists()
    assert path.read_text() == "# owner's policy\n"


@pytest.mark.parametrize("change", ["profile", "netplan", "target"])
def test_owner_changes_after_prepare_block_effects(appliance, change):
    appliance.owner.prepare(CANDIDATE)
    path = appliance.path(module.PROFILE) if change == "profile" else appliance.config if change == "netplan" else appliance.path(module.NETPLAN)
    path.write_text("# changed after approval\n")
    with pytest.raises(HostError, match="已變更"):
        appliance.owner.apply(CANDIDATE)
    assert path.read_text() == "# changed after approval\n"
    assert not appliance.path(module.LOGIND).exists()


def test_rollback_preserves_later_owner_mode_and_content_edits(appliance):
    appliance.owner.prepare(CANDIDATE)
    appliance.owner.apply(CANDIDATE)
    path = appliance.path(module.NETPLAN)
    path.write_text("# newer owner network policy\n")
    path.chmod(0o640)
    result = appliance.owner.rollback(CANDIDATE)
    assert result["owner_modified_preserved"] == [module.NETPLAN]
    assert path.read_text() == "# newer owner network policy\n" and path.stat().st_mode & 0o777 == 0o640


def test_crash_after_mask_write_does_not_duplicate_and_rollback_needs_no_product(appliance, monkeypatch):
    appliance.owner.prepare(CANDIDATE)
    real = module.ApplianceUpdate.write
    stopped = {"once": False}
    def interrupted(self, target, entry):
        real(self, target, entry)
        if target in module.MASKS and not stopped["once"]:
            stopped["once"] = True
            raise OSError("reply lost after durable mask")
    monkeypatch.setattr(module.ApplianceUpdate, "write", interrupted)
    with pytest.raises(OSError):
        appliance.owner.apply(CANDIDATE)
    monkeypatch.setattr(module.ApplianceUpdate, "write", real)
    assert appliance.owner.apply(CANDIDATE)["state"] == "applied"
    import shutil
    shutil.rmtree(appliance.owner.product)
    shutil.rmtree(appliance.owner.manager.product)
    assert appliance.owner.rollback(CANDIDATE)["state"] == "rolled_back"


def test_missing_dependency_fails_before_journal_marker_or_policy(appliance):
    def unavailable(*args, **kwargs):
        raise HostError("missing trusted OS dependency")
    appliance.manager.run = unavailable
    with pytest.raises(HostError, match="dependency"):
        appliance.owner.prepare(CANDIDATE)
    assert not appliance.journal(CANDIDATE).exists() and not appliance.marker.exists()
    assert not appliance.path(module.NETPLAN).exists()


def test_runtime_power_readback_failure_blocks_update_success(appliance):
    appliance.owner.prepare(CANDIDATE)
    appliance.owner.apply(CANDIDATE)
    appliance.live["power"] = False
    with pytest.raises(HostError, match="尚未確認"):
        appliance.owner.health(CANDIDATE)


@pytest.mark.parametrize("optin", [True, 0, 2, "1"])
def test_signed_policy_optin_must_be_exact_integer_one(appliance, optin):
    (appliance.owner.product / "host/managed-system.json").write_text(json.dumps({
        "format": 1, "files": [], "appliance_policy": optin}))
    with pytest.raises(HostError, match="manifest is invalid"):
        appliance.owner.prepare(CANDIDATE)
    assert not appliance.marker.exists()


def test_sleep_mask_cannot_redirect_into_another_owner_unit(appliance):
    path = appliance.path(sorted(module.MASKS)[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to("/etc/systemd/system/owner.service")
    with pytest.raises(HostError, match="another policy"):
        appliance.owner.prepare(CANDIDATE)
    assert os.readlink(path) == "/etc/systemd/system/owner.service"


def test_later_owner_symlink_replacement_is_preserved_on_rollback(appliance):
    appliance.owner.prepare(CANDIDATE)
    appliance.owner.apply(CANDIDATE)
    path = appliance.path(sorted(module.MASKS)[0])
    path.unlink()
    path.symlink_to("/etc/systemd/system/owner.service")
    result = appliance.owner.rollback(CANDIDATE)
    assert result["owner_modified_preserved"] == ["/" + str(path.relative_to(appliance.root))]
    assert os.readlink(path) == "/etc/systemd/system/owner.service"


def test_effective_network_override_prevents_false_healthy_claim(appliance):
    appliance.owner.prepare(CANDIDATE)
    appliance.owner.apply(CANDIDATE)
    (appliance.root / "etc/netplan/99-z-owner.yaml").write_text("network:\n  renderer: NetworkManager\n")
    with pytest.raises(HostError, match="被其他設定覆蓋"):
        appliance.owner.health(CANDIDATE)


def test_interrupted_temporary_mask_is_reused_only_when_exact_owned_devnull(appliance):
    appliance.owner.prepare(CANDIDATE)
    target = sorted(module.MASKS)[0]
    path = appliance.path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".agentic-new")
    temporary.symlink_to("/dev/null")
    assert appliance.owner.apply(CANDIDATE)["state"] == "applied"
    assert path.readlink() == Path("/dev/null") and not temporary.is_symlink()


def test_nm_live_identity_is_read_only_and_persisted_for_next_boot(appliance, monkeypatch):
    appliance.config.write_text("network:\n  version: 2\n  renderer: NetworkManager\n  ethernets:\n    eno1:\n      dhcp4: true\n")
    monkeypatch.setattr(module.network, "physical_ethernet", lambda root: [{"name": "eno1", "mac": "02:01:02:03:04:05"}])
    real = appliance.manager.run
    def run(*args, **kwargs):
        if args[0] == "nmcli":
            appliance.calls.append(args)
            assert args[:4] == ("nmcli", "--escape", "no", "-g")
            if args[4] == "GENERAL.CON-UUID":
                return SimpleNamespace(returncode=0, stdout="a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12)
            return SimpleNamespace(returncode=0, stdout="01:02:03:04")
        return real(*args, **kwargs)
    appliance.manager.run = run
    appliance.owner.prepare(CANDIDATE)
    appliance.owner.apply(CANDIDATE)
    policy = json.loads(appliance.path(module.POLICY).read_text())
    assert policy["overlay"]["network"]["ethernets"]["eno1"]["networkmanager"]["passthrough"]["ipv4.dhcp-client-id"] == "01:02:03:04"
    assert all("modify" not in call and "reapply" not in call and "up" not in call for call in appliance.calls)
