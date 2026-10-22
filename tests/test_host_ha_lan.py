"""Owned, stopped-VM wired-LAN configuration; no development-host network use."""
import copy
import json
import sys
import uuid
from datetime import UTC, datetime
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
NETWORK_UUID = "7857bb95-bd31-4e64-86f4-c42e509fb235"
NETWORK_MAC = "52:54:00:bb:17:01"


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
            replacement = ET.fromstring(Path(args[4]).read_text())
            if replacement.find("uuid") is None:
                ET.SubElement(replacement, "uuid").text = str(uuid.uuid4())
            if (self.network is not None and replacement.findtext("name") == self.network.findtext("name")
                    and replacement.findtext("uuid") != self.network.findtext("uuid")):
                raise HostError("virsh: network already exists with uuid")
            if replacement.find("mac") is None:
                ET.SubElement(replacement, "mac", {"address": NETWORK_MAC})
            self.network = replacement
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
    ET.SubElement(libvirt.network, "uuid").text = NETWORK_UUID
    ET.SubElement(libvirt.network, "mac", {"address": NETWORK_MAC})
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
    identity = (vm.network.findtext("uuid"), dict(vm.network.find("mac").attrib))
    assert identity[0] != OWNER
    response = lan.configure(selected["id"])
    assert response["mode"] == "wired" and response["state"] == "configured"
    assert "online" not in response and response["guest_mac"].startswith("02:")
    assert vm.domain.findall("devices/interface")[1].find("source").attrib == {"dev": "enp1s0", "mode": "bridge"}
    assert vm.network.find(f"{{{DNSMASQ}}}options/{{{DNSMASQ}}}option").get("value") == "dhcp-option=3"
    assert vm.network.find("ip/dhcp/host").attrib == {"mac": MAC, "name": "agentic-haos", "ip": IP}
    assert ET.tostring(vm.domain.find("devices/interface")) == before
    assert vm.vm == "shut off" and vm.active and vm.autostart
    assert (vm.network.findtext("uuid"), dict(vm.network.find("mac").attrib)) == identity
    assert not any(call[0] in {"ip", "nmcli", "brctl"} for call in vm.calls)
    lan.configure(None)
    assert len(vm.domain.findall("devices/interface")) == 1
    assert vm.network.find(f"{{{DNSMASQ}}}options") is None
    assert lan.status()["state"] == "disabled"
    assert (vm.network.findtext("uuid"), dict(vm.network.find("mac").attrib)) == identity


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
    assert vm.network.findtext("uuid") == NETWORK_UUID != OWNER
    assert vm.network.find("mac").attrib == {"address": NETWORK_MAC}


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
    assert vm.network.findtext("uuid") == NETWORK_UUID
    assert vm.network.find("mac").attrib == {"address": NETWORK_MAC}


@pytest.mark.parametrize("fault", ["missing_uuid", "duplicate_uuid", "invalid_uuid", "nil_uuid", "uuid_attributes",
                                   "duplicate_mac", "invalid_mac", "multicast_mac", "zero_mac", "mac_attributes"])
def test_invalid_existing_network_identity_fails_before_any_mutation(lab, fault):
    lan, vm, _, _ = lab
    identity, mac = vm.network.find("uuid"), vm.network.find("mac")
    if fault == "missing_uuid":
        vm.network.remove(identity)
    elif fault == "duplicate_uuid":
        ET.SubElement(vm.network, "uuid").text = NETWORK_UUID
    elif fault == "invalid_uuid":
        identity.text = "not-a-uuid"
    elif fault == "nil_uuid":
        identity.text = "00000000-0000-0000-0000-000000000000"
    elif fault == "uuid_attributes":
        identity.set("unexpected", "value")
    elif fault == "duplicate_mac":
        ET.SubElement(vm.network, "mac", {"address": NETWORK_MAC})
    elif fault == "mac_attributes":
        mac.set("unexpected", "value")
    else:
        mac.set("address", {"invalid_mac": "invalid", "multicast_mac": "ff:ff:ff:ff:ff:ff",
                            "zero_mac": "00:00:00:00:00:00"}[fault])
    with pytest.raises(HostError, match="network_changed"):
        lan.configure(lan.interfaces()[0]["id"])
    assert not lan.path.exists() and vm.autostart and vm.active
    assert not any(call[3] in {"autostart", "net-destroy", "net-define", "define"} for call in vm.calls)


def test_mode_change_preserves_validated_network_definition_and_generated_defaults(lab):
    lan, vm, _, _ = lab
    nat = ET.SubElement(vm.network.find("forward"), "nat")
    ET.SubElement(nat, "port", {"start": "1024", "end": "65535"})
    original = ET.tostring(vm.network)
    lan.configure(lan.interfaces()[0]["id"])
    assert vm.network.find("forward/nat/port").attrib == {"start": "1024", "end": "65535"}
    lan.configure(None)
    assert ET.tostring(vm.network) == original


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
    for body in ({"action": "configure_lan", "interface_id": "eth0"},
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



RESTORE_POINT = "20261019T000000Z-aabbccdd"


def completed_snapshot(lan):
    source = lan.manager.root / "snapshots" / RESTORE_POINT
    source.mkdir(parents=True, exist_ok=True)
    for name in ("haos.qcow2", "nvram.fd"):
        (source / name).write_bytes(b"fixture")
    atomic(source / "manifest.json", {"owner": OWNER, "created_at": 1,
                                      "files": {"haos.qcow2": "a" * 64, "nvram.fd": "b" * 64}})
    return {"snapshot": RESTORE_POINT}


def ready_body(lan, **change):
    record = lan.record()
    home = {"adapter_version": 1, "state": "ready", "reason": "verified", "interface": "enp7s0",
            "ipv4": ["192.0.2.99"], "ipv6": [], "connected": True, "discovery_enabled": True,
            "default_adapter": True, "internal_url": "http://192.0.2.99:8123",
            "observed_at": datetime.now(UTC).isoformat(), "limits": []}
    return {"attempt_id": record["attempt_id"], "guest_mac": record["choice"]["guest_mac"],
            "home": {**home, **change}}


def test_auto_unique_nic_and_multi_nic_requires_selection(lab):
    lan, _, links, _ = lab
    assert lan.configure()["interface"] == "enp1s0"
    links.append({**links[0], "ifname": "enp2s0", "address": "00:11:22:33:44:66"})
    with pytest.raises(HostError, match="select_wired_interface"):
        lan.configure()
    assert lan.configure(None)["mode"] == "nat"
    links.clear()
    with pytest.raises(HostError, match="no_wired_interface"):
        lan.configure()


def test_stable_mac_survives_disable_reenable_and_preserves_deployed_random_mac(lab):
    lan, _, _, _ = lab
    first = lan.configure()["guest_mac"]
    lan.configure(None)
    assert lan.configure()["guest_mac"] == first
    record = lan.record()
    deployed = "02:8e:10:d2:c4:b9"
    # An old deployment has no installation_id or top-level guest_mac.
    record = {"contract_version": 1, "choice": {**record["choice"], "guest_mac": deployed}}
    atomic(lan.path, record)
    lan.manager.run.__self__.domain.findall("devices/interface")[1].find("mac").set("address", deployed)
    lan.configure(None)
    assert lan.configure()["guest_mac"] == deployed


def test_mac_is_product_derived_for_same_installation(lab):
    lan, vm, _, _ = lab
    first = lan.configure()["guest_mac"]
    lan.configure(None)
    lan.path.unlink()
    assert lan.configure()["guest_mac"] == first
    assert int(first[:2], 16) & 3 == 2


def test_one_action_stops_with_restore_point_then_starts_and_verifies(lab, monkeypatch):
    lan, vm, _, _ = lab
    vm.vm = "running"
    events = []
    def snapshot(*, restart):
        assert restart is False
        events.append("restore-point-and-clean-stop")
        vm.vm = "shut off"
        return completed_snapshot(lan)
    def start():
        assert vm.domain.findall("devices/interface")[1].get("type") == "direct"
        events.append("start")
        vm.vm = "running"
    def verify():
        events.append("verified")
        lan.begin_attempt()
        lan.observe(ready_body(lan))
        lan.refresh_observation()
        return lan.status()
    monkeypatch.setattr(lan.manager, "snapshot", snapshot)
    monkeypatch.setattr(lan.manager, "start", start)
    monkeypatch.setattr(lan, "verify", verify)
    result = lan.connect()
    assert events == ["restore-point-and-clean-stop", "start", "verified"]
    assert result["restore_point"] == RESTORE_POINT
    assert result["ha_lan_ip"] == "192.0.2.99"
    assert result["message"] == "HA 已連上家庭網路：192.0.2.99"
    assert "guest_dhcp_and_discovery_not_yet_verified" not in result["limitations"]


@pytest.mark.parametrize("stage", ["configure", "start", "verify"])
def test_one_action_automatically_returns_to_nat_on_failure(lab, monkeypatch, stage):
    lan, vm, _, _ = lab
    def snapshot(*, restart):
        vm.vm = "shut off"
        return completed_snapshot(lan)
    monkeypatch.setattr(lan.manager, "snapshot", snapshot)
    monkeypatch.setattr(lan.manager, "shutdown", lambda: setattr(vm, "vm", "shut off"))
    count = 0
    def start():
        nonlocal count
        count += 1
        if stage == "start" and count == 1:
            raise HostError("injected")
        vm.vm = "running"
    monkeypatch.setattr(lan.manager, "start", start)
    monkeypatch.setattr(lan, "verify", lambda: (_ for _ in ()).throw(HostError("injected")))
    if stage == "configure":
        vm.fail, vm.fail_after = "define", True
    result = lan.connect()
    assert result["fallback"] is True and result["mode"] == "nat"
    assert result["restore_point"] == RESTORE_POINT and "已自動恢復基本網路" in result["message"]
    assert vm.vm == "running"
    validate_network(vm.network, OWNER, {False})
    validate_domain(vm.domain, [None])


def test_failed_restore_point_never_changes_network(lab, monkeypatch):
    lan, vm, _, _ = lab
    before = ET.tostring(vm.network), ET.tostring(vm.domain)
    monkeypatch.setattr(lan.manager, "snapshot", lambda **kw: (_ for _ in ()).throw(HostError("backup_failed")))
    with pytest.raises(HostError, match="建立還原點"):
        lan.connect()
    assert (ET.tostring(vm.network), ET.tostring(vm.domain)) == before
    assert not lan.path.exists()


def test_observation_requires_current_attempt_mac_and_ready_proof(lab):
    lan, vm, _, _ = lab
    lan.configure()
    vm.vm = "running"
    lan.begin_attempt()
    body = ready_body(lan)
    for invalid in ({**body, "attempt_id": "f" * 64}, {**body, "guest_mac": "02:00:00:00:00:01"},
                    ready_body(lan, state="waiting_for_address"), ready_body(lan, discovery_enabled=False),
                    ready_body(lan, internal_url="http://192.168.177.2:8123"),
                    ready_body(lan, ipv4=["198.51.100.99"]),
                    ready_body(lan, observed_at="2000-01-01T00:00:00Z")):
        with pytest.raises(HostError, match="observation_"):
            lan.observe(invalid)
    assert lan.status()["ha_lan_ip"] is None
    assert lan.observe(body) == {"accepted": True}
    lan.refresh_observation()
    assert lan.status()["ha_lan_ip"] == "192.0.2.99"
    assert lan.status()["core_configuration_pending"] is False
    lan.begin_attempt()
    lan.refresh_observation()
    assert lan.status()["ha_lan_ip"] is None
    assert lan.status()["core_configuration_pending"] is True


def test_verified_status_expires_and_stopped_guest_does_not_claim_online(lab):
    lan, vm, _, _ = lab
    lan.configure()
    lan.begin_attempt()
    lan.observe(ready_body(lan))
    lan.refresh_observation()
    vm.vm = "running"
    assert lan.status()["ha_lan_ip"] == "192.0.2.99"
    vm.vm = "shut off"
    assert lan.status()["ha_lan_ip"] is None
    vm.vm = "running"
    record = lan.record()
    record["home"]["observed_at"] = "2000-01-01T00:00:00Z"
    atomic(lan.path, record)
    assert lan.status()["ha_lan_ip"] is None
    assert "guest_dhcp_and_discovery_not_yet_verified" in lan.status()["limitations"]


def test_first_install_defaults_only_single_wired_dhcp_and_never_overwrites_choice(lab):
    lan, _, _, addresses = lab
    state = lan.manager.state()
    state["complete"] = False
    atomic(lan.manager.state_file, state)
    assert lan.default_choice() is False
    addresses[0]["addr_info"][0]["dynamic"] = True
    assert lan.default_choice() is True
    assert lan.status()["mode"] == "wired"
    lan.configure(None)
    assert lan.default_choice() is False
    assert lan.status()["mode"] == "nat"


def test_existing_install_nat_is_not_silently_changed(lab):
    lan, _, _, addresses = lab
    addresses[0]["addr_info"][0]["dynamic"] = True
    assert lan.default_choice() is False
    assert not lan.path.exists()


def test_initial_guest_agent_ip_proof_does_not_claim_discovery_verified(lab, monkeypatch):
    lan, vm, _, _ = lab
    lan.configure()
    vm.vm = "running"
    monkeypatch.setattr(lan, "_guest_address", lambda: "192.0.2.99")
    result = lan.verify(initial=True, timeout=0)
    assert result["ha_lan_ip"] == "192.0.2.99"
    assert result["attempt_id"] and result["core_configuration_pending"]
    assert "guest_dhcp_and_discovery_not_yet_verified" in result["limitations"]


def test_missing_guest_address_times_out_without_external_network_probe(lab, monkeypatch):
    lan, _, _, _ = lab
    lan.configure()
    monkeypatch.setattr(lan, "_guest_address", lambda: None)
    with pytest.raises(HostError, match="address_not_verified"):
        lan.verify(initial=True, timeout=0)


def test_jobs_accept_auto_nic_without_losing_explicit_null(lab, monkeypatch):
    lan, _, _, _ = lab
    observed = []
    monkeypatch.setattr(lan.manager, "action", lambda action, snapshot, **kw: observed.append(kw) or {})
    jobs = Jobs(lan.manager)
    for identifier, fields in (("auto-nic-job", {}), ("disable-nic-job", {"interface_id": None})):
        job = {"id": identifier, "request": {"id": identifier, "action": "configure_lan", **fields}}
        jobs.items[identifier] = job
        jobs.execute(job)
        assert job["state"] == "succeeded"
    assert observed == [{}, {"interface_id": None}]



def test_existing_wired_install_is_adopted_for_read_only_ip_reporting(lab):
    lan, vm, _, _ = lab
    lan.configure()
    vm.vm = "running"
    assert not lan.record().get("attempt_id")
    lan.refresh_observation()
    assert lan.status()["attempt_id"]
    assert lan.status()["core_configuration_pending"] is False
    lan.observe(ready_body(lan))
    lan.refresh_observation()
    assert lan.status()["ha_lan_ip"] == "192.0.2.99"


def test_snapshot_copy_failure_restores_running_vm_without_network_changes(lab, monkeypatch):
    lan, vm, _, _ = lab
    manager = lan.manager
    manager.images.mkdir(parents=True)
    for name in ("haos.qcow2", "nvram.fd"):
        (manager.images / name).write_bytes(b"fixture")
    monkeypatch.setattr(manager, "require_compatibility", lambda action: {})
    monkeypatch.setattr(manager.root_cow, "require_copy_capacity", lambda paths: None)
    monkeypatch.setattr(manager, "shutdown", lambda: setattr(vm, "vm", "shut off"))
    monkeypatch.setattr(manager, "start", lambda: setattr(vm, "vm", "running"))
    execute = manager.run
    def run(*args, **kwargs):
        if args[0] == "cp":
            raise HostError("copy_failed")
        return execute(*args, **kwargs)
    monkeypatch.setattr(manager, "run", run)
    vm.vm = "running"
    with pytest.raises(HostError, match="copy_failed"):
        manager.snapshot(restart=False)
    assert vm.vm == "running" and not lan.path.exists()



def test_guest_agent_readback_is_fixed_command_and_matches_only_enrolled_mac(lab, monkeypatch):
    lan, _, _, _ = lab
    lan.configure()
    mac = lan.record()["choice"]["guest_mac"]
    rows = [{"hardware-address": MAC, "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": IP}]},
            {"hardware-address": mac, "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "192.0.2.99"}]}]
    def virsh(*args, **kwargs):
        assert args == ("qemu-agent-command", NAME, '{"execute":"guest-network-get-interfaces"}', "--timeout", "5")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"return": rows}))
    monkeypatch.setattr(lan.manager, "virsh", virsh)
    assert lan._guest_address() == "192.0.2.99"
    rows[1]["hardware-address"] = "02:00:00:00:00:01"
    assert lan._guest_address() is None
    rows[1]["hardware-address"] = mac
    rows[1]["ip-addresses"][0]["ip-address"] = "198.51.100.99"
    assert lan._guest_address() is None


def test_initial_guest_agent_ip_proof_expires_and_host_reboot_invalidates(lab, monkeypatch):
    from agentic_host import ha_lan
    lan, vm, _, _ = lab
    lan.configure()
    vm.vm = "running"
    monkeypatch.setattr(lan, "_guest_address", lambda: "192.0.2.99")
    lan.verify(initial=True, timeout=0)
    record = lan.record()
    record["guest_ipv4_observed_at"] = 0
    atomic(lan.path, record)
    assert lan.status()["ha_lan_ip"] is None
    lan.observe(ready_body(lan))
    lan.refresh_observation()
    assert lan.status()["ha_lan_ip"] == "192.0.2.99"
    monkeypatch.setattr(ha_lan, "host_boot_id", lambda: "new-host-boot")
    assert lan.status()["ha_lan_ip"] is None
    lan.refresh_observation()
    assert lan.record()["host_boot_id"] == "new-host-boot"
    assert lan.status()["core_configuration_pending"] is False


def test_manager_status_uses_same_verified_lan_and_profile(lab, monkeypatch):
    from agentic_host import bootstrap, ha_lan
    lan, vm, _, _ = lab
    lan.configure()
    lan.begin_attempt()
    vm.vm = "running"
    lan.observe(ready_body(lan))
    lan.refresh_observation()
    monkeypatch.setattr(ha_lan, "HALan", lambda manager: lan)
    monkeypatch.setattr(bootstrap.InitialSetup, "status", lambda setup: {})
    monkeypatch.setattr(lan.manager.root_cow, "status", lambda: {"state": "healthy"})
    result = lan.manager.status()
    assert result["network"] == result["profile"]["network"] == result["ha_lan"]["mode"] == "wired"
    assert result["ha_internal_url"] == "http://192.0.2.99:8123"
    assert result["network_message"] == "HA 已連上家庭網路：192.0.2.99"


def test_malformed_libvirt_xml_is_normalized_and_safe_fallback_runs(lab, monkeypatch):
    lan, vm, _, _ = lab
    execute = lan.manager.virsh
    bad = [True]
    def virsh(*args, **kwargs):
        if args[0] == "dumpxml" and bad[0]:
            bad[0] = False
            return SimpleNamespace(returncode=0, stdout="<broken")
        return execute(*args, **kwargs)
    monkeypatch.setattr(lan.manager, "virsh", virsh)
    monkeypatch.setattr(lan.manager, "snapshot", lambda **kw: completed_snapshot(lan))
    monkeypatch.setattr(lan.manager, "shutdown", lambda: setattr(vm, "vm", "shut off"))
    monkeypatch.setattr(lan.manager, "start", lambda: setattr(vm, "vm", "running"))
    result = lan.connect()
    assert result["fallback"] and result["mode"] == "nat"
    assert vm.vm == "running"



def test_provision_checks_cow_capacity_before_download_or_extraction(lab, monkeypatch):
    from agentic_host import manager as manager_module
    lan, _, _, _ = lab
    manager = lan.manager
    (manager.product / "host").mkdir(parents=True)
    (manager.product / "host/bases.lock.json").write_text((ROOT / "host/bases.lock.json").read_text())
    profile = manager.state()["profile"]
    read_text = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == "/etc/agentic-home/profile.json":
            return json.dumps(profile)
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(manager_module.os, "access", lambda *args: True)
    monkeypatch.setattr(manager, "require_compatibility", lambda action: {})
    def capacity(required):
        assert required >= profile["disk_gib"] * 1024**3
        raise HostError("cow_protected")
    monkeypatch.setattr(manager.root_cow, "require_capacity", capacity)
    monkeypatch.setattr(manager_module, "fetch", lambda *args: pytest.fail("download precedes capacity guard"))
    with pytest.raises(HostError, match="cow_protected"):
        manager.provision()
    assert not manager.images.exists()



def test_interrupted_switch_with_failed_fallback_recovers_same_restore_point(lab, monkeypatch):
    lan, vm, _, _ = lab
    monkeypatch.setattr(lan.manager, "snapshot", lambda **kw: completed_snapshot(lan))
    monkeypatch.setattr(lan.manager, "shutdown", lambda: setattr(vm, "vm", "shut off"))
    monkeypatch.setattr(lan.manager, "start", lambda: setattr(vm, "vm", "running"))
    configure = lan.configure
    def broken_configure(interface_id):
        if interface_id is None:
            raise HostError("fallback_temporarily_unavailable")
        vm.fail, vm.fail_after = "define", True
        return configure(interface_id)
    monkeypatch.setattr(lan, "configure", broken_configure)
    with pytest.raises(HostError, match="自動恢復尚未完成"):
        lan.connect()
    assert lan.record()["pending"] and lan.recoverable()
    assert lan.status()["can_connect"] is True
    monkeypatch.setattr(lan, "configure", configure)
    monkeypatch.setattr(lan.manager, "snapshot", lambda **kw: pytest.fail("must reuse completed restore point"))
    result = lan.connect(None)
    assert result["fallback"] and result["mode"] == "nat" and result["restore_point"] == RESTORE_POINT
    assert not lan.transaction_path.exists()
    assert (lan.manager.root / "snapshots" / RESTORE_POINT / "manifest.json").exists()
    assert vm.vm == "running"


def test_recovery_never_invents_missing_restore_point_or_overwrites_manual_edits(lab, monkeypatch):
    lan, vm, _, _ = lab
    completed_snapshot(lan)
    atomic(lan.transaction_path, {"contract_version": 1, "owner": OWNER,
                                 "restore_point": RESTORE_POINT, "state": "switching"})
    vm.domain.find("devices/interface/mac").set("address", "02:00:00:00:00:ff")
    monkeypatch.setattr(lan.manager, "require_compatibility", lambda action: {})
    with pytest.raises(HostError, match="自動恢復尚未完成"):
        lan.recover()
    assert lan.transaction_path.exists()
    vm.calls.clear()
    (lan.manager.root / "snapshots" / RESTORE_POINT / "manifest.json").unlink()
    with pytest.raises(HostError, match="無法確認原有還原點"):
        lan.recover()
    assert not vm.calls


def test_explicit_legacy_pending_recovery_backs_up_before_returning_nat(lab, monkeypatch):
    lan, vm, _, _ = lab
    vm.fail, vm.fail_after = "define", True
    with pytest.raises(HostError):
        lan.configure()
    assert lan.record()["pending"] and not lan.transaction_path.exists()
    assert lan.status()["can_connect"] is True
    events = []
    def snapshot(**options):
        assert options == {"restart": False, "lan_recovery": True}
        assert lan.record()["pending"]
        events.append("backup")
        return completed_snapshot(lan)
    monkeypatch.setattr(lan.manager, "snapshot", snapshot)
    monkeypatch.setattr(lan.manager, "shutdown", lambda: events.append("shutdown"))
    monkeypatch.setattr(lan.manager, "start", lambda: events.append("start"))
    result = lan.connect(None)
    assert result["fallback"] and result["mode"] == "nat"
    assert events == ["backup", "shutdown", "start"]
    assert not lan.record().get("pending")


def test_legacy_pending_backup_failure_stays_fenced_without_recursive_start(lab, monkeypatch):
    lan, vm, _, _ = lab
    vm.fail, vm.fail_after = "define", True
    with pytest.raises(HostError):
        lan.configure()
    manager = lan.manager
    manager.images.mkdir(parents=True)
    for name in ("haos.qcow2", "nvram.fd"):
        (manager.images / name).write_bytes(b"fixture")
    monkeypatch.setattr(manager, "require_compatibility", lambda action: {})
    monkeypatch.setattr(manager.root_cow, "require_copy_capacity", lambda paths: None)
    monkeypatch.setattr(manager, "shutdown", lambda: setattr(vm, "vm", "shut off"))
    monkeypatch.setattr(manager, "start", lambda: pytest.fail("must preserve uncertain configuration fence"))
    execute = manager.run
    def run(*args, **kwargs):
        if args[0] == "cp":
            raise HostError("copy_failed")
        return execute(*args, **kwargs)
    monkeypatch.setattr(manager, "run", run)
    vm.vm = "running"
    with pytest.raises(HostError, match="copy_failed"):
        manager.snapshot(restart=False, lan_recovery=True)
    assert vm.vm == "shut off" and lan.record()["pending"]


def test_concurrent_observation_posts_are_serialized_independently_of_lifecycle_lock(lab):
    from concurrent.futures import ThreadPoolExecutor
    lan, vm, _, _ = lab
    lan.configure()
    lan.begin_attempt()
    vm.vm = "running"
    body = ready_body(lan)
    # An active connect job holds this lock while observation callbacks must
    # remain available to complete authenticated readback.
    with lan.manager.locked(), ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(lan.observe, [body] * 12)) == [{"accepted": True}] * 12
    lan.refresh_observation()
    assert lan.status()["ha_lan_ip"] == "192.0.2.99"



def test_cow_critical_or_invalid_never_blocks_home_restart(lab, monkeypatch):
    lan, vm, _, _ = lab
    manager = lan.manager

    lan.configure(None)
    monkeypatch.setattr(manager.root_cow, "require_capacity", lambda required: (_ for _ in ()).throw(HostError("snapshot exhausted")))
    monkeypatch.setattr(manager, "require_compatibility", lambda action: None)
    assert manager.start()["vm"] == "running"


@pytest.fixture
def initial_provision(lab, monkeypatch):
    from agentic_host import ha_lan, manager as manager_module
    lan, vm, _, _ = lab
    state = lan.manager.state()
    state['complete'] = False
    atomic(lan.manager.state_file, state)
    lan.configure()
    manager = lan.manager
    (manager.product / 'host').mkdir(parents=True)
    (manager.product / 'host/bases.lock.json').write_text((ROOT / 'host/bases.lock.json').read_text())
    manager.images.mkdir(parents=True)
    for name in ('haos.qcow2', 'nvram.fd'):
        (manager.images / name).write_bytes(b'fixture')
    read_text = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == '/etc/agentic-home/profile.json':
            return json.dumps(state['profile'])
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(manager_module.os, 'access', lambda *args: True)
    current = [lan]
    monkeypatch.setattr(ha_lan, 'HALan', lambda manager: current[0])
    def prepare(manager):
        run = manager.run
        def execute(*args, **kwargs):
            if args[0] in {'chown', 'chmod'}:
                return SimpleNamespace(returncode=0, stdout='')
            return run(*args, **kwargs)
        monkeypatch.setattr(manager, 'run', execute)
        monkeypatch.setattr(manager, 'require_compatibility', lambda action: {})
        monkeypatch.setattr(manager, 'shutdown', lambda: setattr(vm, 'vm', 'shut off'))
        monkeypatch.setattr(manager, 'status', lambda: {'complete': manager.state()['complete'], 'ha_lan': current[0].status()})
    prepare(manager)
    monkeypatch.setattr(lan, 'verify', lambda **kwargs: (_ for _ in ()).throw(HostError('no_initial_address')))
    def restart():
        fresh = Manager(manager.root, manager.product, manager.images, vm.run)
        current[0] = HALan(fresh, read_ip=lan.read_ip, identity=lan.identity)
        prepare(fresh)
        return current[0]
    return lan, vm, restart


@pytest.mark.parametrize('stage', ['journal', 'network', 'domain', 'before_start'])
def test_first_provision_fallback_recovers_after_interruption(initial_provision, monkeypatch, stage):
    from agentic_host import ha_lan
    lan, vm, restart = initial_provision
    class Interrupted(BaseException):
        pass
    original_atomic, original_run, original_configure = ha_lan.atomic, lan.manager.run, lan.configure
    def write(path, value):
        original_atomic(path, value)
        if stage == 'journal' and path == lan.transaction_path:
            raise Interrupted()
    def run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        if (stage == 'network' and 'net-define' in args) or (stage == 'domain' and 'define' in args):
            raise Interrupted()
        return result
    def configure(interface_id):
        result = original_configure(interface_id)
        if stage == 'before_start' and interface_id is None:
            raise Interrupted()
        return result
    with monkeypatch.context() as crash:
        crash.setattr(ha_lan, 'atomic', write)
        crash.setattr(lan.manager, 'run', run)
        crash.setattr(lan, 'configure', configure)
        with pytest.raises(Interrupted):
            lan.manager.provision()
    assert lan.transaction_path.exists() and lan.recoverable()
    transaction = json.loads(lan.transaction_path.read_text())
    assert transaction['kind'] == 'initial_nat_fallback' and 'restore_point' not in transaction
    assert not lan.manager.state()['complete']
    assert '已自動' not in lan.status()['message']
    if stage == 'journal':
        assert vm.vm == 'running' and len(vm.domain.findall('devices/interface')) == 2
    fresh = restart()
    # This is the daemon's existing journal-first recovery path after restart.
    if fresh.transaction_path.exists():
        result = fresh.recover()
    assert result['mode'] == 'nat' and result['fallback'] and 'restore_point' not in result
    assert vm.vm == 'running' and not fresh.transaction_path.exists() and not fresh.record().get('pending')
    validate_network(vm.network, OWNER, {False})
    validate_domain(vm.domain, [None])
    result = fresh.manager.provision()
    assert result['complete'] and result['ha_lan']['mode'] == 'nat'
    assert not (fresh.manager.root / 'snapshots').exists()


@pytest.mark.parametrize('fault', ['foreign', 'no_owner', 'completed', 'unknown_complete', 'restore_pending',
                                   'restore_point', 'null_restore_point', 'kind', 'state', 'extra'])
def test_initial_fallback_journal_cannot_bypass_normal_restore_point_checks(lab, fault):
    lan, vm, _, _ = lab
    state = {**lan.manager.state(), 'complete': False}
    transaction = {'contract_version': 1, 'owner': OWNER, 'kind': 'initial_nat_fallback', 'state': 'switching'}
    if fault == 'foreign':
        transaction['owner'] = FOREIGN
    elif fault == 'no_owner':
        state.pop('owner')
        transaction['owner'] = None
    elif fault == 'completed':
        state['complete'] = True
    elif fault == 'unknown_complete':
        state.pop('complete')
    elif fault == 'restore_pending':
        state['restore_pending'] = RESTORE_POINT
    elif fault in {'restore_point', 'null_restore_point'}:
        transaction['restore_point'] = RESTORE_POINT if fault == 'restore_point' else None
    elif fault == 'kind':
        transaction['kind'] = 'unknown'
    elif fault == 'state':
        transaction['state'] = 'done'
    else:
        transaction['extra'] = True
    atomic(lan.manager.state_file, state)
    atomic(lan.transaction_path, transaction)
    before = len(vm.calls)
    assert not lan.recoverable()
    with pytest.raises(HostError, match='自動恢復尚未完成'):
        lan.recover()
    assert len(vm.calls) == before
