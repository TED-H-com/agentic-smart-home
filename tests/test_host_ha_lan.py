"""Owned, stopped-VM wired-LAN configuration; no development-host network use."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host.daemon import Jobs  # noqa: E402
from agentic_host.ha_lan import DNSMASQ, HALan, physical_identity, validate_domain, validate_network  # noqa: E402
from agentic_host.manager import IP, MAC, NAME, HostError, Manager, atomic, domain_xml, network_xml  # noqa: E402

OWNER = "3d58ff36-88e0-45b2-bb6d-d9aa718ad479"
FOREIGN = "734fa345-7f05-402e-91f0-179b753ea266"


class Libvirt:
    def __init__(self):
        self.domain = self.network = None
        self.vm = "shut off"
        self.active = True
        self.autostart = True
        self.calls, self.others, self.ports = [], {}, ""
        self.fail = None
        self.fail_after = False

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == "iptables":
            return SimpleNamespace(returncode=0, stdout="")
        assert args[:3] == ("virsh", "--connect", "qemu:///system")
        command = args[3]
        failed = command == self.fail
        if failed:
            self.fail = None
            if not self.fail_after:
                raise HostError("injected_failure")
        result, code = "", 0
        if command == "domstate":
            result, code = self.vm, 0 if self.domain is not None else 1
        elif command == "dominfo":
            result = "Autostart: " + ("enable" if self.autostart else "disable")
        elif command == "dumpxml":
            node = self.domain if args[4] in {NAME, OWNER} else self.others[args[4]]
            result, code = ET.tostring(node, encoding="unicode") if node is not None else "", 0 if node is not None else 1
        elif command == "net-dumpxml":
            result, code = ET.tostring(self.network, encoding="unicode") if self.network is not None else "", 0 if self.network is not None else 1
        elif command == "net-info":
            result = "Active: " + ("yes" if self.active else "no")
        elif command == "list":
            result = "\n".join(([OWNER] if self.domain is not None else []) + list(self.others))
        elif command == "net-port-list":
            assert self.active, "inactive networks cannot enumerate live ports"
            result = self.ports
        elif command == "autostart":
            self.autostart = "--disable" not in args
        elif command == "net-destroy":
            self.active = False
        elif command == "net-define":
            self.network = ET.fromstring(Path(args[4]).read_text())
        elif command == "define":
            self.domain = ET.fromstring(Path(args[4]).read_text())
        elif command == "net-start":
            self.active = True
        elif command == "start":
            self.vm = "running"
        elif command != "net-autostart":
            raise AssertionError(args)
        if failed and self.fail_after:
            raise HostError("injected_uncertain_failure")
        return SimpleNamespace(returncode=code, stdout=result)


@pytest.fixture
def lab(tmp_path):
    libvirt = Libvirt()
    manager = Manager(tmp_path / "state", tmp_path / "product", tmp_path / "images", libvirt.run)
    profile = json.loads((ROOT / "host/profiles/laptop-amd64.json").read_text())
    atomic(manager.state_file, {"owner": OWNER, "profile": profile, "complete": True})
    libvirt.domain = ET.fromstring(domain_xml(profile, manager.images / "haos.qcow2", manager.images / "nvram.fd", OWNER))
    libvirt.network = ET.fromstring(network_xml(OWNER))
    links = [{"ifname": "enp1s0", "address": "00:11:22:33:44:55", "link_type": "ether",
              "flags": ["UP", "LOWER_UP", "BROADCAST", "MULTICAST"]}]
    addresses = [{"ifname": "enp1s0", "addr_info": [{"family": "inet", "scope": "global", "local": "192.0.2.20", "prefixlen": 24}]}]
    def read(arguments, timeout):
        assert timeout == 1
        return copy.deepcopy(links if "link" in arguments else addresses)
    lan = HALan(manager, read_ip=read, identity=lambda name: "/sys/devices/pci/fixture/" + name if name.startswith("en") else None)
    return lan, libvirt, links, addresses


def test_interfaces_bind_physical_identity_not_dhcp_and_reject_unsupported_links(lab):
    lan, _, links, addresses = lab
    first = lan.interfaces()[0]
    addresses[0]["addr_info"][0]["local"] = "192.0.2.21"
    assert lan.interfaces()[0]["id"] == first["id"]
    for change in ({"master": "br0"}, {"linkinfo": {"info_kind": "vlan"}}, {"flags": ["UP"]},
                   {"address": "ff:ff:ff:ff:ff:ff"}, {"ifname": "wlan0"}, {"ifname": "en;reboot"}):
        links.append({**links[0], **change})
    assert len(lan.interfaces()) == 1
    links[0]["address"] = "00:11:22:33:44:56"
    assert lan.interfaces()[0]["id"] != first["id"]


def test_physical_identity_excludes_wifi_and_virtual_links(tmp_path):
    net = tmp_path / "net"
    device = tmp_path / "devices/pci0000/ethernet"
    device.mkdir(parents=True)
    for name in ("eth0", "wlan0", "wifi1"):
        path = net / name
        path.mkdir(parents=True)
        (path / "device").symlink_to(device, target_is_directory=True)
    (net / "wlan0/wireless").mkdir()
    (net / "wifi1/phy80211").mkdir()
    (net / "bridge0").mkdir()
    assert physical_identity("eth0", net) == str(device)
    assert all(physical_identity(name, net) is None for name in ("wlan0", "wifi1", "bridge0"))


def test_wire_can_describe_active_wired_link_without_host_ipv4(lab):
    lan, _, _, addresses = lab
    addresses.clear()
    assert lan.interfaces()[0]["address"] is None
    assert lan.interfaces()[0]["network"] is None


def test_configure_then_disable_retains_management_and_restores_gateway(lab):
    lan, vm, _, _ = lab
    selected = lan.interfaces()[0]
    before = ET.tostring(vm.domain.find("devices/interface"))
    response = lan.configure(selected["id"])
    assert response["mode"] == "wired" and response["state"] == "configured"
    assert "online" not in response and response["guest_mac"].startswith("02:")
    assert vm.domain.findall("devices/interface")[1].find("source").attrib == {"dev": "enp1s0", "mode": "bridge"}
    assert vm.network.find(f"{{{DNSMASQ}}}options/{{{DNSMASQ}}}option").get("value") == "dhcp-option=3"
    assert vm.network.find("ip/dhcp/host").attrib == {"mac": MAC, "name": "agentic-haos", "ip": IP}
    assert ET.tostring(vm.domain.find("devices/interface")) == before
    assert vm.vm == "shut off" and vm.active and vm.autostart
    assert not any(call[0] in {"ip", "nmcli", "brctl"} for call in vm.calls)
    lan.configure(None)
    assert len(vm.domain.findall("devices/interface")) == 1
    assert vm.network.find(f"{{{DNSMASQ}}}options") is None
    assert lan.status()["state"] == "disabled"


def test_initial_selection_does_not_create_guest_or_network(lab):
    lan, vm, _, _ = lab
    lan.manager.state_file.unlink()
    vm.domain = vm.network = None
    response = lan.configure(lan.interfaces()[0]["id"])
    assert response["state"] == "configured" and response["vm_state"] == "not_created"
    assert vm.domain is vm.network is None
    assert not any("define" in call or "net-define" in call for call in vm.calls)


@pytest.mark.parametrize("value", ["eth0", "192.0.2.0/24", "x" * 64, [], True])
def test_only_opaque_current_ids_are_accepted(lab, value):
    lan, vm, _, _ = lab
    with pytest.raises(HostError, match="interface_id_invalid"):
        lan.configure(value)
    assert vm.calls == []


def test_stale_selection_and_running_vm_have_no_mutations(lab):
    lan, vm, links, _ = lab
    selected = lan.interfaces()[0]["id"]
    links.clear()
    with pytest.raises(HostError, match="selected_interface_unavailable"):
        lan.configure(selected)
    vm.vm = "running"
    with pytest.raises(HostError, match="shutdown_required"):
        lan.configure(None)
    assert all(call[3] == "domstate" for call in vm.calls)


@pytest.mark.parametrize("change", ["management", "foreign_nic", "network_owner", "extra_dhcp", "external_guest", "live_port",
                                   "firewall_zone", "custom_nat"])
def test_manual_edits_shared_network_and_live_ports_fail_before_mutation(lab, change):
    lan, vm, _, _ = lab
    if change == "management":
        vm.domain.find("devices/interface/mac").set("address", "02:00:00:00:00:ff")
    elif change == "foreign_nic":
        vm.domain.find("devices").append(copy.deepcopy(vm.domain.find("devices/interface")))
    elif change == "network_owner":
        vm.network.find("metadata")[0].text = FOREIGN
    elif change == "extra_dhcp":
        ET.SubElement(vm.network.find("ip/dhcp"), "range", {"start": "192.168.177.10", "end": "192.168.177.20"})
    elif change == "external_guest":
        vm.others[FOREIGN] = copy.deepcopy(vm.domain)
    elif change == "firewall_zone":
        vm.network.find("bridge").set("zone", "trusted")
    elif change == "custom_nat":
        nat = ET.SubElement(vm.network.find("forward"), "nat")
        ET.SubElement(nat, "port", {"start": "40000", "end": "45000"})
    else:
        vm.ports = FOREIGN
    with pytest.raises(HostError):
        lan.configure(lan.interfaces()[0]["id"])
    assert not any(call[3] in {"define", "net-destroy", "net-define", "autostart"} for call in vm.calls)


@pytest.mark.parametrize("stage", ["net-destroy", "net-define", "net-start", "define"])
@pytest.mark.parametrize("after", [False, True])
def test_interrupted_stages_fence_start_and_explicit_retry_reconciles(lab, stage, after):
    lan, vm, _, _ = lab
    selected = lan.interfaces()[0]["id"]
    vm.fail, vm.fail_after = stage, after
    with pytest.raises(HostError, match="injected"):
        lan.configure(selected)
    assert lan.status()["state"] == "needs_reconcile" and not vm.autostart
    with pytest.raises(HostError, match="needs_reconcile"):
        lan.manager.start()
    response = lan.configure(selected)
    assert response["state"] == "configured" and vm.autostart and vm.active
    assert "pending" not in lan.record()
    validate_domain(vm.domain, [lan.record()["choice"]])
    validate_network(vm.network, OWNER, {True})


def test_interrupted_enable_can_be_explicitly_removed_when_cable_disappears(lab):
    lan, vm, links, _ = lab
    vm.fail, vm.fail_after = "define", True
    with pytest.raises(HostError):
        lan.configure(lan.interfaces()[0]["id"])
    links.clear()
    response = lan.configure(None)
    assert response["state"] == "disabled" and vm.autostart
    validate_domain(vm.domain, [None])
    validate_network(vm.network, OWNER, {False})


def test_reconcile_never_overwrites_unrecognised_manual_network_edit(lab):
    lan, vm, _, _ = lab
    vm.fail = "define"
    selected = lan.interfaces()[0]["id"]
    with pytest.raises(HostError):
        lan.configure(selected)
    vm.network.find("bridge").set("name", "foreign")
    vm.calls.clear()
    with pytest.raises(HostError, match="network_changed"):
        lan.configure(None)
    assert not any(call[3] in {"define", "net-destroy", "net-define"} for call in vm.calls)


def test_jobs_validate_fields_before_thread_launch(lab):
    lan, _, _, _ = lab
    jobs = Jobs(lan.manager)
    for body in ({"action": "configure_lan"}, {"action": "configure_lan", "interface_id": "eth0"},
                 {"action": "start", "interface_id": None},
                 {"action": "configure_lan", "interface_id": None, "snapshot": "somewhere"}):
        with pytest.raises(HostError):
            jobs.start({"id": "test-operation", **body})
    assert jobs.items == {}


@pytest.mark.parametrize("stage", ["journal", "forwarding", "final_record", "autostart_enable"])
def test_commit_tail_distinguishes_config_fence_from_autostart_availability(lab, monkeypatch, stage):
    from agentic_host import ha_lan

    lan, vm, _, _ = lab
    selected = lan.interfaces()[0]["id"]
    original_atomic = ha_lan.atomic
    count = 0

    def interrupted(path, value):
        nonlocal count
        count += 1
        if (stage == "journal" and count == 1) or (stage == "final_record" and count == 2):
            raise OSError("fixture_power_loss")
        original_atomic(path, value)
    monkeypatch.setattr(ha_lan, "atomic", interrupted)
    if stage == "forwarding":
        monkeypatch.setattr(lan.manager, "refresh_forwarding", lambda: (_ for _ in ()).throw(HostError("fixture_forwarding")))
    elif stage == "autostart_enable":
        original = lan._autostart
        def autostart(enabled):
            if enabled:
                raise HostError("fixture_autostart")
            original(False)
        monkeypatch.setattr(lan, "_autostart", autostart)
    with pytest.raises((OSError, HostError)):
        lan.configure(selected)
    assert not vm.autostart
    record = lan.record()
    if stage in {"forwarding", "final_record"}:
        assert record.get("pending")
        with pytest.raises(HostError, match="needs_reconcile"):
            lan.manager.start()
    elif stage == "journal":
        assert not record.get("pending") and record["choice"] is None
        validate_network(vm.network, OWNER, {False})
        validate_domain(vm.domain, [None])
    else:
        assert not record.get("pending") and record["choice"]
        validate_network(vm.network, OWNER, {True})
        validate_domain(vm.domain, [record["choice"]])


def test_cli_lan_job_validation_does_not_accept_interface_names(lab):
    from agentic_host.cli import JobObservationError, validate_job

    selected = lab[0].interfaces()[0]["id"]
    for value in (selected, None):
        request = {"id": "lan-configuration", "action": "configure_lan", "interface_id": value}
        job = {"id": request["id"], "request": request, "state": "succeeded", "result": {}}
        assert validate_job(job, request["id"]) == job
    job["request"]["interface_id"] = "enp1s0"
    with pytest.raises(JobObservationError):
        validate_job(job, request["id"])


def test_only_generated_default_nat_range_is_accepted(lab):
    _, vm, _, _ = lab
    nat = ET.SubElement(vm.network.find("forward"), "nat")
    ET.SubElement(nat, "port", {"start": "1024", "end": "65535"})
    validate_network(vm.network, OWNER, {False})
    nat.set("ipv6", "yes")
    with pytest.raises(HostError, match="network_changed"):
        validate_network(vm.network, OWNER, {False})
