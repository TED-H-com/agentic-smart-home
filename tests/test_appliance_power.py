"""OS policy fixtures only: no system service on the development host is touched."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host import appliance, appliance_power
from agentic_host.appliance_power import LID_PROPERTIES, POWER_KEYS, SLEEP_TARGETS, reconcile_power
from agentic_host.manager import HostError

PROFILE = {"appliance_lid": True, "desktop": True}


class System:
    def __init__(self, broken=False):
        self.calls, self.masked, self.loaded, self.broken = [], set(), False, broken

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("busctl", "get-property"):
            return SimpleNamespace(returncode=0, stdout='s "ignore"' if self.loaded else 's "suspend"')
        if args[:2] == ("systemctl", "is-enabled"):
            return SimpleNamespace(returncode=1, stdout="masked" if args[-1] in self.masked else "static")
        if args[:2] == ("systemctl", "mask"):
            self.masked.update(args[2:])
        if args == ("systemctl", "kill", "-s", "HUP", "systemd-logind") and not self.broken:
            self.loaded = True
        return SimpleNamespace(returncode=0, stdout="")


def test_power_reconcile_live_verification_and_idempotence(tmp_path):
    system = System()
    result = reconcile_power(PROFILE, tmp_path, system)
    assert result["verified"] and result["changed"]
    assert set(result["checks"]) == set(LID_PROPERTIES + SLEEP_TARGETS)
    assert system.masked == set(SLEEP_TARGETS)
    assert ("systemctl", "kill", "-s", "HUP", "systemd-logind") in system.calls
    assert not any("restart" in call for call in system.calls)
    dconf = (tmp_path / "etc/dconf/db/local.d/60-agentic-home").read_text()
    locks = (tmp_path / "etc/dconf/db/local.d/locks/60-agentic-home").read_text()
    for key, value in POWER_KEYS.items():
        assert key + "=" + value in dconf
        assert "/org/gnome/settings-daemon/plugins/power/" + key in locks
    system.calls.clear()
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}
    second = reconcile_power(PROFILE, tmp_path, system)
    assert second["verified"] and not second["changed"]
    assert all(path.stat().st_mtime_ns == stamp for path, stamp in before.items())
    assert all(call[:2] in {("systemctl", "is-enabled"), ("busctl", "get-property")} for call in system.calls)
    # Configuration drift in the same boot must trigger reload even if files are unchanged.
    system.loaded = False
    assert reconcile_power(PROFILE, tmp_path, system)["verified"]


def test_power_no_effect_for_non_appliance_and_offline_stage(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline stage must not touch live services")
    assert not reconcile_power({"appliance_lid": False}, tmp_path, forbidden)["applicable"]
    assert not list(tmp_path.iterdir())
    result = reconcile_power(PROFILE, tmp_path, forbidden, runtime=False)
    assert result["verified"] is None and result["changed"]
    assert all((tmp_path / "etc/systemd/system" / name).readlink() == Path("/dev/null")
               for name in SLEEP_TARGETS)
    assert not reconcile_power(PROFILE, tmp_path, forbidden, runtime=False)["changed"]


def test_power_failed_reload_does_not_claim_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(appliance_power.time, "sleep", lambda _: None)
    result = reconcile_power(PROFILE, tmp_path, System(broken=True))
    assert not result["verified"]
    assert not result["checks"]["HandleLidSwitch"]


@pytest.mark.parametrize("failed", ["network", "power", None])
def test_install_verification_checks_both_readbacks(monkeypatch, failed):
    path = Path(__file__).resolve().parents[1] / "host/install.py"
    spec = importlib.util.spec_from_file_location("appliance_install_fixture", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    monkeypatch.setattr("agentic_host.appliance_network.reconcile_network",
                        lambda: {"verified": failed != "network"})
    monkeypatch.setattr(installer, "reconcile_power",
                        lambda _: {"applicable": True, "verified": failed != "power"})
    if failed:
        with pytest.raises(HostError, match="appliance_" + failed + "_verification"):
            installer.verify_appliance(PROFILE)
    else:
        installer.verify_appliance(PROFILE)


def test_host_startup_reconcile_keeps_rescue_and_does_not_execute_source(tmp_path, monkeypatch):
    root, state = tmp_path / "os", tmp_path / "state"
    (root / "etc/agentic-home").mkdir(parents=True)
    state.mkdir()
    (root / "etc/agentic-home/profile.json").write_text(json.dumps(PROFILE))
    calls = []
    def fail(**kwargs):
        calls.append("network")
        raise HostError("sensitive raw output must not be persisted")
    monkeypatch.setattr("agentic_host.appliance_network.reconcile_network", fail)
    monkeypatch.setattr(appliance, "reconcile_power", lambda *a, **kw: {"verified": True})
    manager = SimpleNamespace(root=state, run=lambda *a, **kw: None)
    result = appliance.reconcile_appliance(manager, root)
    assert calls == ["network"] and result["state"] == "needs_attention"
    assert "sensitive" not in (state / "appliance-reconcile.json").read_text()
    assert result["checks"]["power"]["verified"]


def test_installer_pins_network_before_desktop_packages(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "host/install.py"
    spec = importlib.util.spec_from_file_location("appliance_install_order_fixture", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    calls = []
    class DesktopReached(Exception):
        pass
    def run(*args):
        calls.append(args)
        if "ubuntu-desktop-minimal" in args:
            raise DesktopReached
    monkeypatch.setattr(installer, "preflight", lambda *args: None)
    monkeypatch.setattr(installer, "verify_bundle", lambda *args: None)
    monkeypatch.setattr(installer, "TARGET", installer.ROOT)
    monkeypatch.setattr(installer, "STATE", tmp_path)
    monkeypatch.setattr(installer, "atomic", lambda *args: None)
    monkeypatch.setattr(installer, "run", run)
    monkeypatch.setattr("agentic_host.appliance_network.reconcile_network",
                        lambda: calls.append(("pin-network",)) or {"verified": True})
    with pytest.raises(DesktopReached):
        installer.install(PROFILE, {})
    assert calls[:3] == [("apt-get", "update"),
                         ("apt-get", "install", "-y", "python3-yaml", "ethtool", "netplan.io"),
                         ("pin-network",)]
