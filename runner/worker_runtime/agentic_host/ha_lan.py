"""An explicitly selected wired HAOS NIC, with fixed management connectivity.

Only a stopped, product-owned VM and its exclusive product network are changed.
No host interface address, bridge, route or NetworkManager profile is rewritten.
Interrupted operations retain a journal and require an explicit configure retry.
"""

import copy
import hashlib
import ipaddress
import json
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .discovery import _ip_json
from .manager import IP, MAC, NAME, NETWORK, NS, HostError, atomic, sync_path

DNSMASQ = "http://libvirt.org/schemas/network/dnsmasq/1.0"
ET.register_namespace("dnsmasq", DNSMASQ)
INTERFACE_ID = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"[a-zA-Z0-9_.:-]{1,32}")
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
LIMITATIONS = ["wired_ethernet_only", "bridges_vlans_wifi_not_supported", "haos_must_be_stopped",
               "guest_dhcp_and_discovery_not_yet_verified", "lan_dhcp_required", "lan_exposes_native_ha_services",
               "management_subnet_retained", "manual_ha_network_preferences_may_override_detection"]


def physical_identity(name, sysfs=Path("/sys/class/net")):
    path = sysfs / name
    if (not (path / "device").exists() or "/virtual/" in str(path.resolve())
            or (path / "wireless").exists() or (path / "phy80211").exists()):
        return None
    return str((path / "device").resolve())


def add_interface(domain, choice):
    if not choice:
        return
    node = ET.SubElement(domain.find("devices"), "interface", {"type": "direct", "trustGuestRxFilters": "yes"})
    ET.SubElement(node, "mac", {"address": choice["guest_mac"]})
    ET.SubElement(node, "source", {"dev": choice["interface"], "mode": "bridge"})
    ET.SubElement(node, "model", {"type": "virtio"})
    ET.SubElement(node, "alias", {"name": "ua-agentic-lan"})


def no_default_router(network):
    options = ET.SubElement(network, f"{{{DNSMASQ}}}options")
    ET.SubElement(options, f"{{{DNSMASQ}}}option", {"value": "dhcp-option=3"})


def network_mode_xml(network, *, wired):
    """Change only the router option on a previously validated definition.

    Destroying a libvirt network stops it but retains its persistent UUID. A
    fresh UUID-less definition generates a different UUID and collides with the
    existing name. Preserve its actual identity, MAC and generated defaults.
    """
    updated = copy.deepcopy(network)
    for options in updated.findall(f"{{{DNSMASQ}}}options"):
        updated.remove(options)
    if wired:
        no_default_router(updated)
    return ET.tostring(updated, encoding="unicode")


def _same_interface(node, *, direct=None):
    source, mac, model = node.find("source"), node.find("mac"), node.find("model")
    if source is None or mac is None or model is None or model.get("type") != "virtio":
        return False
    expected = {"dev": direct["interface"], "mode": "bridge"} if direct else {"network": NETWORK}
    # Libvirt adds generated port IDs and bridge names to live network sources.
    if direct:
        if dict(source.attrib) != expected:
            return False
    elif any(source.get(key) != value for key, value in expected.items()) or set(source.attrib) - {"network", "portid", "bridge"}:
        return False
    if mac.get("address", "").lower() != (direct["guest_mac"] if direct else MAC):
        return False
    if node.get("type") != ("direct" if direct else "network"):
        return False
    if direct and (node.get("trustGuestRxFilters") != "yes" or node.find("alias") is None
                   or node.find("alias").get("name") != "ua-agentic-lan"):
        return False
    # Generated PCI addresses, targets and default drivers do not change authority.
    return not any(child.tag not in {"source", "mac", "model", "alias", "address", "target", "driver"} for child in node)


def validate_domain(domain, choices):
    interfaces = domain.findall("devices/interface")
    if len(interfaces) not in {1, 2} or not _same_interface(interfaces[0]):
        raise HostError("ha_lan_management_interface_changed")
    if len(interfaces) == 1 and None in choices:
        return
    if len(interfaces) == 2 and any(choice and _same_interface(interfaces[1], direct=choice) for choice in choices):
        return
    raise HostError("ha_lan_interface_changed")


def validate_network(network, owner, modes):
    """Accept only our fixed network semantics plus libvirt-generated identities."""
    if (network.tag != "network" or network.findtext("name") != NETWORK
            or network.findtext(f"metadata/{{{NS}}}owner") != owner):
        raise HostError("ha_lan_network_not_owned")
    identities, macs = network.findall("uuid"), network.findall("mac")
    if (len(identities) != 1 or not _UUID.fullmatch(identities[0].text or "")
            or identities[0].text == "00000000-0000-0000-0000-000000000000"
            or identities[0].attrib or list(identities[0]) or len(macs) > 1):
        raise HostError("ha_lan_network_changed")
    if macs:
        address = macs[0].get("address", "")
        if (set(macs[0].attrib) != {"address"} or not _MAC.fullmatch(address)
                or int(address[:2], 16) & 1 or address == "00:00:00:00:00:00" or list(macs[0])):
            raise HostError("ha_lan_network_changed")
    if any(child.tag not in {"name", "uuid", "metadata", "forward", "bridge", "mac", "ip", f"{{{DNSMASQ}}}options"}
           for child in network):
        raise HostError("ha_lan_network_changed")
    if any(len(network.findall(tag)) != 1 for tag in ("name", "metadata", "forward", "bridge", "ip")):
        raise HostError("ha_lan_network_changed")
    forward, bridge = network.find("forward"), network.find("bridge")
    addresses, options = network.findall("ip"), network.findall(f"{{{DNSMASQ}}}options")
    if (forward is None or forward.get("mode") != "nat" or set(forward.attrib) != {"mode"}
            or any(child.tag != "nat" for child in forward) or bridge is None
            or bridge.get("name") != "virbr-ah" or len(addresses) != 1
            or addresses[0].attrib != {"address": "192.168.177.1", "netmask": "255.255.255.0"}):
        raise HostError("ha_lan_network_changed")
    if bridge.attrib != {"name": "virbr-ah", "stp": "on", "delay": "0"} or list(bridge):
        raise HostError("ha_lan_network_changed")
    # Libvirt may materialize its default NAT port range. A custom range or
    # firewall zone is an external policy edit, never silently replace it.
    if list(forward):
        nat = list(forward)
        if (len(nat) != 1 or nat[0].attrib or len(list(nat[0])) != 1 or nat[0][0].tag != "port"
                or nat[0][0].attrib != {"start": "1024", "end": "65535"} or list(nat[0][0])):
            raise HostError("ha_lan_network_changed")
    dhcp = addresses[0].find("dhcp")
    hosts = [] if dhcp is None else list(dhcp)
    if (len(list(addresses[0])) != 1 or len(hosts) != 1 or hosts[0].tag != "host"
            or hosts[0].attrib != {"mac": MAC, "name": "agentic-haos", "ip": IP} or list(hosts[0])):
        raise HostError("ha_lan_network_changed")
    wired = bool(options)
    if wired and (len(options) != 1 or len(list(options[0])) != 1
                  or options[0][0].tag != f"{{{DNSMASQ}}}option" or options[0][0].attrib != {"value": "dhcp-option=3"}):
        raise HostError("ha_lan_network_changed")
    if wired not in modes:
        raise HostError("ha_lan_network_mode_mismatch")


class HALan:
    def __init__(self, manager, *, read_ip=None, identity=None):
        self.manager = manager
        self.path = manager.root / "ha-lan.json"
        self.read_ip = read_ip or _ip_json
        self.identity = identity or physical_identity

    def record(self):
        if not self.path.exists():
            return {"contract_version": 1, "choice": None}
        value = json.loads(self.path.read_text())
        if value.get("contract_version") != 1 or "choice" not in value:
            raise HostError("ha_lan_record_invalid")
        return value

    def choices(self):
        record = self.record()
        return record.get("pending", {}).get("accepted", [record["choice"]])

    def ready(self):
        record = self.record()
        if record.get("pending"):
            raise HostError("ha_lan_needs_reconcile")
        choice = record["choice"]
        if choice and not any(item["id"] == choice["id"] for item in self.interfaces()):
            raise HostError("ha_lan_selected_interface_unavailable")
        return choice

    def interfaces(self):
        links = self.read_ip(("-j", "-d", "link", "show"), 1.0)
        addresses = self.read_ip(("-j", "-4", "address", "show"), 1.0)
        if (not isinstance(links, list) or not isinstance(addresses, list)
                or len(json.dumps([links, addresses])) > 512 * 1024):
            raise HostError("ha_lan_interface_inventory_invalid")
        found = []
        for link in links[:256]:
            if not isinstance(link, dict):
                continue
            name, mac = link.get("ifname"), link.get("address")
            flags, info = link.get("flags", []), link.get("linkinfo") or {}
            if (not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(mac, str)
                    or not _MAC.fullmatch(mac) or int(mac[:2], 16) & 1 or mac == "00:00:00:00:00:00"
                    or not isinstance(flags, list) or not {"UP", "LOWER_UP"}.issubset(flags)
                    or "LOOPBACK" in flags or link.get("link_type") != "ether" or link.get("master")
                    or not isinstance(info, dict) or info.get("info_kind")):
                continue
            identity = self.identity(name)
            if not identity:
                continue
            address = network = None
            for item in addresses[:256]:
                if not isinstance(item, dict) or item.get("ifname") != name:
                    continue
                for raw in item.get("addr_info", []) if isinstance(item.get("addr_info"), list) else []:
                    if not isinstance(raw, dict) or raw.get("family") != "inet" or raw.get("scope") != "global":
                        continue
                    try:
                        value = ipaddress.IPv4Interface(str(raw["local"]) + "/" + str(raw["prefixlen"]))
                    except (ValueError, KeyError):
                        continue
                    if value.ip.is_unspecified or value.ip.is_loopback or value.ip.is_multicast:
                        continue
                    address, network = str(value.ip), str(value.network)
                    break
            # The choice is a physical NIC, so DHCP lease changes do not change its ID.
            identifier = hashlib.sha256(json.dumps([name, mac, identity], separators=(",", ":")).encode()).hexdigest()
            found.append({"id": identifier, "label": name + (" · " + address if address else ""),
                          "interface": name, "address": address, "network": network})
            if len(found) == 32:
                break
        return found

    def vm_state(self):
        result = self.manager.virsh("domstate", NAME, check=False, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else "not_created"

    def status(self):
        record, limitations = self.record(), list(LIMITATIONS)
        choice = record["choice"]
        try:
            interfaces = self.interfaces()
        except (OSError, ValueError, HostError, subprocess.SubprocessError):
            interfaces = []
            limitations.append("interface_inventory_unavailable")
        try:
            vm_state = self.vm_state()
        except (OSError, HostError, subprocess.SubprocessError):
            vm_state = "unknown"
        state = "configured" if choice else "disabled"
        if choice and choice["id"] not in {item["id"] for item in interfaces}:
            state = "unavailable"
        if record.get("pending"):
            state = "needs_reconcile"
        return {"contract_version": 1, "mode": "wired" if choice else "nat", "state": state,
                "selected_interface_id": choice["id"] if choice else None,
                "interface": choice["interface"] if choice else None, "guest_mac": choice["guest_mac"] if choice else None,
                "interfaces": interfaces, "vm_state": vm_state, "can_configure": vm_state in {"shut off", "not_created"},
                "limitations": limitations}

    def _xml(self, *args, missing=False):
        result = self.manager.virsh(*args, check=False, timeout=10)
        if result.returncode:
            if missing:
                return None
            raise HostError("ha_lan_libvirt_read_failed")
        if len(result.stdout) > 1024 * 1024:
            raise HostError("ha_lan_libvirt_response_limit")
        return ET.fromstring(result.stdout)

    def _exclusive(self, owner):
        result = self.manager.virsh("list", "--all", "--uuid", timeout=10)
        identifiers = result.stdout.split()
        if len(identifiers) > 64 or any(not _UUID.fullmatch(item) for item in identifiers):
            raise HostError("ha_lan_domain_inventory_invalid")
        for identifier in identifiers:
            if identifier == owner:
                continue
            for arguments in (("dumpxml", identifier, "--inactive"), ("dumpxml", identifier)):
                domain = self._xml(*arguments)
                for source in domain.findall("devices/interface/source"):
                    if source.get("network") == NETWORK or source.get("bridge") == "virbr-ah":
                        raise HostError("ha_lan_network_has_other_guests")
        if self._network_active():
            ports = self.manager.virsh("net-port-list", NETWORK, "--uuid", timeout=10)
            if ports.stdout.strip():
                raise HostError("ha_lan_network_has_live_ports")

    def _network_active(self):
        info = self.manager.virsh("net-info", NETWORK, timeout=10).stdout
        match = re.search(r"^Active:\s+(yes|no)\s*$", info, re.MULTILINE)
        if match is None:
            raise HostError("ha_lan_network_state_unknown")
        return match[1] == "yes"

    def _autostart(self, enabled):
        self.manager.virsh("autostart", NAME, *([] if enabled else ["--disable"]))
        info = self.manager.virsh("dominfo", NAME, timeout=10).stdout
        expected = "enable" if enabled else "disable"
        if not re.search(r"^Autostart:\s+" + expected + r"\s*$", info, re.MULTILINE):
            raise HostError("ha_lan_autostart_readback_failed")

    def configure(self, interface_id):
        if interface_id is not None and (not isinstance(interface_id, str) or not INTERFACE_ID.fullmatch(interface_id)):
            raise HostError("ha_lan_interface_id_invalid")
        if self.manager.state().get("restore_pending"):
            raise HostError("ha_lan_restore_pending")
        if self.vm_state() not in {"shut off", "not_created"}:
            raise HostError("ha_lan_shutdown_required")
        record = self.record()
        choice = None
        if interface_id is not None:
            selected = next((item for item in self.interfaces() if item["id"] == interface_id), None)
            if selected is None:
                raise HostError("ha_lan_selected_interface_unavailable")
            if selected["network"] and ipaddress.ip_network(selected["network"]).overlaps(ipaddress.ip_network("192.168.177.0/24")):
                raise HostError("ha_lan_management_subnet_overlap")
            previous = next((item for item in self.choices() if item and item["id"] == interface_id), None)
            value = hashlib.sha256(uuid.uuid4().bytes).hexdigest()[:10]
            mac = previous["guest_mac"] if previous else "02:" + ":".join(value[i:i + 2] for i in range(0, 10, 2))
            choice = {**selected, "guest_mac": mac}
        owner = self.manager.state().get("owner")
        if not owner:
            if self._xml("dumpxml", NAME, "--inactive", missing=True) is not None:
                raise HostError("ha_lan_domain_not_owned")
            if self._xml("net-dumpxml", NETWORK, missing=True) is not None:
                raise HostError("ha_lan_network_not_owned")
            atomic(self.path, {"contract_version": 1, "choice": choice})
            return self.status()
        domain = self.manager.owned()
        accepted = self.choices()
        if domain is not None:
            validate_domain(domain, accepted)
        network = self._xml("net-dumpxml", NETWORK, "--inactive", missing=True)
        if network is not None:
            validate_network(network, owner, {bool(item) for item in accepted})
            validate_network(self._xml("net-dumpxml", NETWORK), owner, {bool(item) for item in accepted})
            self._exclusive(owner)
        if record.get("pending") and choice is not None:
            target = record["pending"]["target"]
            if target is None or target["id"] != choice["id"]:
                raise HostError("ha_lan_retry_same_selection_or_disable")
        if choice not in accepted:
            accepted.append(choice)
        if domain is not None:
            self._autostart(False)
        # A crash before this journal loses automatic startup only. No network
        # mutation has happened. While pending, libvirt autostart is fenced.
        atomic(self.path, {**record, "pending": {"target": choice, "accepted": accepted}})
        if self.vm_state() not in {"shut off", "not_created"}:
            raise HostError("ha_lan_shutdown_required")
        if choice and not any(item["id"] == choice["id"] for item in self.interfaces()):
            raise HostError("ha_lan_selected_interface_unavailable")
        # Each stage is repeatable only after validating that observed objects
        # are one of the journalled states. Unknown/manual edits remain fenced.
        if network is not None:
            if self._network_active():
                self.manager.virsh("net-destroy", NETWORK)
            path = self.manager.root / "ha-lan-network.xml"
            path.write_text(network_mode_xml(network, wired=bool(choice)))
            self.manager.virsh("net-define", str(path))
            validate_network(self._xml("net-dumpxml", NETWORK, "--inactive"), owner, {bool(choice)})
            self.manager.virsh("net-start", NETWORK)
            self.manager.virsh("net-autostart", NETWORK)
            validate_network(self._xml("net-dumpxml", NETWORK), owner, {bool(choice)})
        if domain is not None:
            if self.vm_state() != "shut off":
                raise HostError("ha_lan_shutdown_required")
            interfaces = domain.findall("devices/interface")
            if len(interfaces) == 2:
                domain.find("devices").remove(interfaces[1])
            add_interface(domain, choice)
            path = self.manager.root / "ha-lan-domain.xml"
            path.write_text(ET.tostring(domain, encoding="unicode"))
            self.manager.virsh("define", str(path))
            validate_domain(self._xml("dumpxml", NAME, "--inactive"), [choice])
        self.manager.refresh_forwarding()
        atomic(self.path, {"contract_version": 1, "choice": choice})
        # Commit known configuration before restoring availability. An uncertain
        # autostart response cannot turn known-safe XML back into pending XML.
        if domain is not None:
            try:
                self._autostart(True)
            except (OSError, HostError, subprocess.SubprocessError):
                raise HostError("ha_lan_configured_autostart_unavailable") from None
        sync_path(self.manager.root)
        return self.status()
