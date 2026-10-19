"""OS utility fixtures; no live host or LAN effects."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host.appliance_status import appliance_status, observe_network  # noqa: E402


class Commands:
    def __init__(self):
        self.address = "192.168.1.113"
        self.route = "eno1"
        self.failure = False
        self.journal = ""
        self.hostname = "castle-agentic-home"
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        assert kwargs == {"timeout": 3, "check": False}
        if self.failure:
            raise OSError("utility absent")
        if args[0] == "hostnamectl":
            output = self.hostname
        elif args[0] == "journalctl":
            output = self.journal
        elif args[2] == "route":
            output = json.dumps([{"dev": self.route, "metric": 100}])
        else:
            output = json.dumps([{"ifname": self.route, "address": "02:00:00:00:00:12",
                                  "addr_info": [{"scope": "global", "local": self.address}]}])
        return SimpleNamespace(returncode=0, stdout=output)


def physical(tmp_path, name="eno1"):
    sysfs = tmp_path / "sysfs"
    (sysfs / name / "device").mkdir(parents=True)
    return sysfs


def test_address_changes_persist_without_poll_write_races(tmp_path):
    commands = Commands()
    sysfs = physical(tmp_path)
    state = tmp_path / "state"
    first = observe_network(state, commands, sysfs=sysfs)
    assert first["address"] == "192.168.1.113" and not first["address_changed"]
    commands.address = "192.168.1.112"
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: observe_network(state, commands, sysfs=sysfs), range(12)))
    assert all(value["address_changed"] and value["previous_address"] == "192.168.1.113" for value in values)
    path = state / "appliance-network-observation.json"
    before = path.stat().st_mtime_ns
    assert observe_network(state, commands, sysfs=sysfs)["address_changed"]
    assert path.stat().st_mtime_ns == before
    assert not list(state.glob("*.json.*"))
    commands.failure = True
    missing = observe_network(state, commands, sysfs=sysfs)
    assert missing["state"] == "unavailable" and missing["address"] is None
    assert json.loads(path.read_text())["address"] == "192.168.1.112"


def test_only_physical_default_route_is_reported_and_hostname_is_observed(tmp_path):
    commands = Commands()
    sysfs = physical(tmp_path)
    commands.route = "docker0"
    assert observe_network(tmp_path / "state", commands, sysfs=sysfs)["state"] == "unavailable"
    commands.route = "br0"
    (sysfs / "br0" / "lower_eno1" / "device").mkdir(parents=True)
    commands.hostname = "my-appliance"
    value = observe_network(tmp_path / "state", commands, sysfs=sysfs)
    assert value["address"] == commands.address
    assert value["mdns_hostname"] == "my-appliance.local"
    commands.address = "::"
    assert observe_network(tmp_path / "state", commands, sysfs=sysfs)["state"] == "unavailable"
    commands.hostname = "invalid/path"
    assert observe_network(tmp_path / "state", commands, sysfs=sysfs)["mdns_hostname"] is None


def test_lid_uses_live_verification_not_cached_success_and_journal_is_bounded(tmp_path, monkeypatch):
    from agentic_host import appliance_power

    commands = Commands()
    verified = True
    monkeypatch.setattr(appliance_power, "verify_power", lambda run: {"verified": verified, "checks": {"lid": verified}})
    commands.journal = json.dumps({"MESSAGE": "Suspending..."})
    value = appliance_status(tmp_path, {"appliance_lid": True}, commands)
    assert value["lid"]["message"] == "蓋上蓋子也會繼續運作"
    assert value["suspend"]["past_suspend"] is True
    assert "家庭自動化可能中斷" in value["suspend"]["warning"]
    assert all("--lines=200" in call and "--since=-14days" in call
               for call in commands.calls if call[0] == "journalctl")
    verified = False
    assert appliance_status(tmp_path, {"appliance_lid": True}, commands)["lid"]["state"] == "unverified"
    commands.failure = True
    value = appliance_status(tmp_path, {"appliance_lid": False}, commands)
    assert value["lid"]["state"] == "not_applicable"
    assert value["suspend"]["state"] == "unavailable"
    assert value["network"]["address"] is None


def test_malformed_journal_is_unavailable_not_success(tmp_path):
    commands = Commands()
    commands.journal = "journal output could not be decoded"
    value = appliance_status(tmp_path, {}, commands)
    assert value["lid"]["state"] == "unavailable"
    assert value["suspend"]["state"] == "unavailable"


def test_journal_empty_match_exit_is_not_an_error(tmp_path):
    commands = Commands()
    def execute(*args, **kwargs):
        if args[0] == "journalctl":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return commands(*args, **kwargs)
    value = appliance_status(tmp_path, {}, execute)
    assert value["suspend"]["state"] == "observed"
    assert value["suspend"]["past_suspend"] is False


def test_kernel_suspend_and_power_probe_failure_remain_truthful(tmp_path, monkeypatch):
    from agentic_host import appliance_power
    commands = Commands()
    commands.journal = json.dumps({"MESSAGE": "PM: suspend entry (deep)"})
    def fail(run):
        raise OSError("bus unavailable")
    monkeypatch.setattr(appliance_power, "verify_power", fail)
    value = appliance_status(tmp_path, {"appliance_lid": True}, commands)
    assert value["lid"]["state"] == "unavailable"
    assert value["suspend"]["past_suspend"] is True
