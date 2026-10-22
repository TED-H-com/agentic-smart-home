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
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from .discovery import _ip_json
from .manager import IP, MAC, NAME, NETWORK, NS, HostError, atomic, parse_xml, sync_path

DNSMASQ = "http://libvirt.org/schemas/network/dnsmasq/1.0"
ET.register_namespace("dnsmasq", DNSMASQ)
AUTO_INTERFACE = object()
CONNECT_SECONDS = 180
INTERFACE_ID = re.compile(r"[a-f0-9]{64}")
_NAME = re.compile(r"[a-zA-Z0-9_.:-]{1,32}")
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
LIMITATIONS = ["wired_ethernet_only", "bridges_vlans_wifi_not_supported",
               "guest_dhcp_and_discovery_not_yet_verified", "lan_dhcp_required", "lan_exposes_native_ha_services",
               "management_subnet_retained", "manual_ha_network_preferences_may_override_detection"]


def host_boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


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
        self.transaction_path = manager.root / "ha-lan-connect.json"
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
            dhcp = False
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
                    dhcp = raw.get("dynamic") is True or (isinstance(raw.get("flags"), list) and "dynamic" in raw["flags"])
                    break
            # The choice is a physical NIC, so DHCP lease changes do not change its ID.
            identifier = hashlib.sha256(json.dumps([name, mac, identity], separators=(",", ":")).encode()).hexdigest()
            found.append({"id": identifier, "label": name + (" · " + address if address else ""),
                          "interface": name, "address": address, "network": network, "dhcp": dhcp})
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
        home = record.get("home") if choice and not record.get("pending") else None
        try:
            fresh = 0 <= time.time() - datetime.fromisoformat(home["observed_at"].replace("Z", "+00:00")).timestamp() <= 180
        except (TypeError, ValueError, KeyError):
            fresh = False
        same_boot = bool(record.get("host_boot_id") and record["host_boot_id"] == host_boot_id())
        verified = bool(home and fresh and same_boot and home.get("state") == "ready" and vm_state == "running"
                        and state == "configured")
        if verified:
            limitations.remove("guest_dhcp_and_discovery_not_yet_verified")
        address = (home.get("ipv4") or [None])[0] if verified else None
        if not address and choice and same_boot and state == "configured" and vm_state == "running":
            if 0 <= time.time() - record.get("guest_ipv4_observed_at", 0) <= 180:
                address = record.get("guest_ipv4")
        message = ("HA 已連上家庭網路：" + address if address else
                   "正在確認 Home Assistant 的家庭網路位址" if choice else "Home Assistant 使用基本網路")
        fallback_complete = bool(record.get("fallback_complete") is True and not choice and state == "disabled"
                                 and vm_state == "running" and not self.transaction_path.exists())
        if fallback_complete and record.get("fallback_message"):
            message = record["fallback_message"]
        return {"contract_version": 1, "mode": "wired" if choice else "nat", "state": state,
                "attempt_id": record.get("attempt_id"), "home": home if verified else None,
                "core_configuration_pending": bool(choice and record.get("attempt_id") and record.get("allow_core_configuration") and not record.get("home")),
                "ha_lan_ip": address, "ha_internal_url": home.get("internal_url") if verified else None,
                "fallback_complete": fallback_complete,
                "message": message, "can_connect": vm_state in {"running", "shut off", "not_created"} and (not self.transaction_path.exists() or self.recoverable()),
                "diagnostics": "主機無法直接連線此家庭位址是 macvtap 的正常限制；產品透過保留的管理網路確認 HA。",
                "reservation": "此 HA 網卡使用固定的裝置識別；可在路由器為它保留目前位址。產品未變更路由器設定。",
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
        return parse_xml(result.stdout)

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

    def select(self, interface_id=AUTO_INTERFACE):
        if interface_id is AUTO_INTERFACE:
            candidates = self.interfaces()
            if len(candidates) != 1:
                raise HostError("ha_lan_select_wired_interface" if candidates else "ha_lan_no_wired_interface")
            return candidates[0]["id"]
        if interface_id is not None and (not isinstance(interface_id, str) or not INTERFACE_ID.fullmatch(interface_id)):
            raise HostError("ha_lan_interface_id_invalid")
        return interface_id

    def configure(self, interface_id=AUTO_INTERFACE):
        interface_id = self.select(interface_id)
        if interface_id is not None and (not isinstance(interface_id, str) or not INTERFACE_ID.fullmatch(interface_id)):
            raise HostError("ha_lan_interface_id_invalid")
        if self.manager.state().get("restore_pending"):
            raise HostError("ha_lan_restore_pending")
        if self.vm_state() not in {"shut off", "not_created"}:
            raise HostError("ha_lan_shutdown_required")
        record = self.record()
        choice = None
        deployed = next((item for item in self.choices() if item), None)
        if deployed and not record.get("guest_mac"):
            record["guest_mac"] = deployed["guest_mac"]
        if interface_id is not None:
            selected = next((item for item in self.interfaces() if item["id"] == interface_id), None)
            if selected is None:
                raise HostError("ha_lan_selected_interface_unavailable")
            if selected["network"] and ipaddress.ip_network(selected["network"]).overlaps(ipaddress.ip_network("192.168.177.0/24")):
                raise HostError("ha_lan_management_subnet_overlap")
            previous = next((item for item in self.choices() if item), None)
            # Retain the deployed MAC (including the old random MAC), even after
            # disabling/re-enabling or changing the uplink. Never break leases.
            installation = record.get("installation_id") or self.manager.state().get("owner") or str(uuid.uuid4())
            value = hashlib.sha256(("agentic-home:ha-lan:v1:" + installation).encode()).hexdigest()[:10]
            mac = record.get("guest_mac") or (previous["guest_mac"] if previous else None)
            mac = mac or "02:" + ":".join(value[i:i + 2] for i in range(0, 10, 2))
            record.update(installation_id=installation, guest_mac=mac)
            choice = {**selected, "guest_mac": mac}
        for key in ("home", "guest_ipv4", "guest_ipv4_observed_at", "host_boot_id", "attempt_id", "attempt_started_at", "allow_core_configuration", "fallback_message", "fallback_complete"):
            record.pop(key, None)
        owner = self.manager.state().get("owner")
        if not owner:
            if self._xml("dumpxml", NAME, "--inactive", missing=True) is not None:
                raise HostError("ha_lan_domain_not_owned")
            if self._xml("net-dumpxml", NETWORK, missing=True) is not None:
                raise HostError("ha_lan_network_not_owned")
            atomic(self.path, {**record, "contract_version": 1, "choice": choice})
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
        record.pop("pending", None)
        atomic(self.path, {**record, "contract_version": 1, "choice": choice})
        # Commit known configuration before restoring availability. An uncertain
        # autostart response cannot turn known-safe XML back into pending XML.
        if domain is not None:
            try:
                self._autostart(True)
            except (OSError, HostError, subprocess.SubprocessError):
                raise HostError("ha_lan_configured_autostart_unavailable") from None
        sync_path(self.manager.root)
        return self.status()


    def observe(self, body, *, persist=True):
        """Accept bounded readback only from the existing UID1000 broker role.

        An attempt token and enrolled MAC bind observations to this boot/config.
        This route carries no HA credential and performs no caller-chosen I/O.
        """
        if not isinstance(body, dict) or set(body) != {"attempt_id", "guest_mac", "home"}:
            raise HostError("ha_lan_observation_invalid")
        record = self.record()
        choice, home = record["choice"], body["home"]
        if (not choice or record.get("pending") or not record.get("attempt_id")
                or body["attempt_id"] != record["attempt_id"] or body["guest_mac"] != choice["guest_mac"]):
            raise HostError("ha_lan_observation_stale")
        if (not isinstance(home, dict) or len(json.dumps(home)) > 16384
                or home.get("state") != "ready" or home.get("connected") is not True
                or home.get("discovery_enabled") is not True):
            raise HostError("ha_lan_observation_invalid")
        try:
            observed_at = datetime.fromisoformat(home["observed_at"].replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                raise ValueError()
            observed_at = observed_at.timestamp()
            if not max(time.time() - 60, record.get("attempt_started_at", 0) - 5) <= observed_at <= time.time() + 5:
                raise ValueError()
        except (TypeError, ValueError, KeyError, AttributeError):
            raise HostError("ha_lan_observation_stale") from None
        addresses = home.get("ipv4")
        if not isinstance(addresses, list) or not 1 <= len(addresses) <= 32:
            raise HostError("ha_lan_observation_invalid")
        for address in addresses:
            self._lan_address(address, choice)
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(home.get("internal_url", ""))
            if (parsed.scheme not in {"http", "https"} or parsed.hostname not in addresses
                    or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                    or parsed.path not in {"", "/"} or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError()
        except (ValueError, TypeError):
            raise HostError("ha_lan_observation_invalid") from None
        # Separate file avoids overwriting an in-flight libvirt configuration journal.
        with self.manager.lan_observation_lock:
            if persist:
                atomic(self._observation_path(record["attempt_id"]), body)
            if self.record().get("attempt_id") != record["attempt_id"]:
                raise HostError("ha_lan_observation_stale")
        return {"accepted": True}

    def _observation_path(self, attempt_id):
        if not isinstance(attempt_id, str) or not INTERFACE_ID.fullmatch(attempt_id):
            raise HostError("ha_lan_observation_invalid")
        return self.manager.root / "ha-lan-observations" / (attempt_id + ".json")

    @staticmethod
    def _lan_address(value, choice):
        try:
            address = ipaddress.IPv4Address(value)
            network = ipaddress.ip_network(choice["network"]) if choice.get("network") else None
            if (address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local
                    or address in ipaddress.ip_network("192.168.177.0/24")
                    or address in ipaddress.ip_network("172.30.32.0/23")
                    or (network and address not in network)
                    or (network and network.prefixlen < 31 and address in {network.network_address, network.broadcast_address})):
                raise ValueError()
        except (ValueError, TypeError):
            raise HostError("ha_lan_observation_invalid") from None
        return str(address)

    def _guest_address(self):
        # Fixed guest-agent command, over the owned libvirt channel. Never probe
        # arbitrary LAN hosts; macvtap deliberately prevents host->guest LAN I/O.
        result = self.manager.virsh("qemu-agent-command", NAME,
                                    '{"execute":"guest-network-get-interfaces"}',
                                    "--timeout", "5", check=False, timeout=8)
        if result.returncode or len(result.stdout) > 131072:
            return None
        payload = json.loads(result.stdout)
        rows = payload.get("return")
        choice = self.record()["choice"]
        if not isinstance(rows, list) or len(rows) > 64 or not choice:
            return None
        matched = [row for row in rows if isinstance(row, dict)
                   and str(row.get("hardware-address", "")).lower() == choice["guest_mac"]]
        if len(matched) != 1:
            return None
        addresses = matched[0].get("ip-addresses", [])
        if not isinstance(addresses, list) or len(addresses) > 64:
            return None
        for row in addresses:
            if isinstance(row, dict) and row.get("ip-address-type") == "ipv4":
                try:
                    return self._lan_address(row.get("ip-address"), choice)
                except HostError:
                    continue
        return None

    def begin_attempt(self, *, allow_configuration=True):
        record = self.record()
        if not record["choice"]:
            return record
        record.update(attempt_id=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), attempt_started_at=time.time(),
                      allow_core_configuration=allow_configuration, host_boot_id=host_boot_id())
        record.pop("home", None)
        record.pop("guest_ipv4", None)
        record.pop("fallback_message", None)
        record.pop("fallback_complete", None)
        atomic(self.path, record)
        # These files contain only transient network proofs, never restore points.
        for path in (self.manager.root / "ha-lan-observations").glob("*.json"):
            if path.name != record["attempt_id"] + ".json":
                path.unlink(missing_ok=True)
        return record

    def verify(self, *, initial=False, timeout=CONNECT_SECONDS):
        record = self.begin_attempt()
        if not record["choice"]:
            return self.status()
        deadline = time.monotonic() + timeout
        while True:
            observation = self._observation_path(record["attempt_id"])
            if observation.exists():
                body = json.loads(observation.read_text())
                if body.get("attempt_id") == record["attempt_id"]:
                    try:
                        self.observe(body, persist=False)  # recheck current MAC and fresh proof
                    except HostError:
                        pass
                    else:
                        record["home"] = body["home"]
                        atomic(self.path, record)
                        return self.status()
            if initial:
                try:
                    address = self._guest_address()
                except (OSError, ValueError, HostError, subprocess.SubprocessError):
                    address = None
                if address:
                    record["guest_ipv4"] = address
                    record["guest_ipv4_observed_at"] = time.time()
                    atomic(self.path, record)
                    return self.status()
            if time.monotonic() >= deadline:
                raise HostError("ha_lan_address_not_verified")
            time.sleep(2)

    def connect(self, interface_id=AUTO_INTERFACE):
        """One approved product action: restore point, clean stop, switch/readback.

        Fallback repeats the owned XML checks. Unknown edits remain fenced rather
        than forcibly replacing administrator configuration or powering off HA.
        """
        if self.transaction_path.exists():
            return self.recover()
        recovering_pending = bool(self.record().get("pending"))
        # An explicit product retry of an old interrupted transaction first
        # protects its data and completes NAT; never retry an uncertain target.
        interface_id = None if recovering_pending else self.select(interface_id)
        if not self.manager.state().get("owner"):
            return self.configure(interface_id)
        if self.manager.state().get("restore_pending"):
            raise HostError("ha_lan_restore_pending")
        # Validate selection before HA downtime or snapshot writes.
        if interface_id is not None and not any(item["id"] == interface_id for item in self.interfaces()):
            raise HostError("ha_lan_selected_interface_unavailable")
        # Failure to create a restore point must never authorize a configuration write.
        try:
            snapshot_options = {"restart": False}
            if recovering_pending:
                snapshot_options["lan_recovery"] = True
            restore_point = self.manager.snapshot(**snapshot_options)["snapshot"]
        except (OSError, ValueError, HostError, subprocess.SubprocessError):
            raise HostError("建立還原點時遇到問題，尚未變更網路；請稍後重試。") from None
        # Durable before any NIC/network change: after a crash the broker can
        # reuse this exact restore point and complete NAT recovery automatically.
        try:
            atomic(self.transaction_path, {"contract_version": 1, "owner": self.manager.state()["owner"],
                                           "restore_point": restore_point, "state": "switching"})
        except OSError:
            if self.transaction_path.exists():
                return self.recover()
            if not recovering_pending:
                self.manager.start()
            raise HostError("無法保存網路變更紀錄，尚未變更網路；已保留還原點，請稍後重試。") from None
        if recovering_pending:
            return self.recover()
        try:
            self.configure(interface_id)
            self.manager.start()
            result = self.verify() if interface_id is not None else self.status()
            self.transaction_path.unlink()
            sync_path(self.manager.root)
            return {**result, "restore_point": restore_point}
        except (OSError, ValueError, HostError, subprocess.SubprocessError):
            return self.recover()

    def provision_fallback(self):
        if self.transaction_path.exists():
            return self.recover()
        state = self.manager.state()
        if state.get("complete") is not False:
            return self.connect(None)
        if not state.get("owner") or state.get("restore_pending"):
            raise HostError("ha_lan_initial_fallback_not_authorized")
        # First provisioning has no completed household or restore point.
        # Journal before shutdown so the daemon can finish even an inner pending switch.
        atomic(self.transaction_path, {"contract_version": 1, "owner": state["owner"],
                                       "kind": "initial_nat_fallback", "state": "switching"})
        return self.recover()

    def recoverable(self):
        try:
            transaction = json.loads(self.transaction_path.read_text())
            if not isinstance(transaction, dict):
                return False
            state = self.manager.state()
            if transaction.get("kind") == "initial_nat_fallback":
                return (set(transaction) == {"contract_version", "owner", "kind", "state"}
                        and type(transaction["contract_version"]) is int and transaction["contract_version"] == 1
                        and transaction["state"] == "switching" and state.get("complete") is False
                        and not state.get("restore_pending") and isinstance(transaction["owner"], str)
                        and bool(_UUID.fullmatch(transaction["owner"])) and transaction["owner"] == state.get("owner"))
            if "kind" in transaction:
                return False
            identifier = transaction["restore_point"]
            if (transaction.get("contract_version") != 1 or transaction.get("owner") != self.manager.state().get("owner")
                    or not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", identifier)):
                return False
            source = self.manager.root / "snapshots" / identifier
            manifest = json.loads((source / "manifest.json").read_text())
            files = manifest["files"]
            return (manifest["owner"] == transaction["owner"] and isinstance(files, dict)
                    and set(files) == {"haos.qcow2", "nvram.fd"}
                    and all(isinstance(value, str) and INTERFACE_ID.fullmatch(value)
                            and (source / name).is_file() for name, value in files.items()))
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def recover(self):
        """Under the lifecycle lock, finish interrupted switching to known NAT.

        Normal switches require the journalled, owned completed restore point.
        The distinct first-provision journal is valid only before completion.
        Recovery changes networking only, never restores older HA data.
        """
        if not self.recoverable():
            raise HostError("自動恢復尚未完成：無法確認原有還原點；已保留資料與管理入口，請聯絡支援。")
        transaction = json.loads(self.transaction_path.read_text())
        initial = transaction.get("kind") == "initial_nat_fallback"
        try:
            self.manager.shutdown()
            self.configure(None)
            self.manager.start()
            record = self.record()
            record["fallback_complete"] = True
            record["fallback_message"] = ("家庭網路尚未準備好，已自動使用基本網路；家庭資料保留，可稍後重試。" if initial else
                                          "家庭網路暫時無法接通，已自動恢復基本網路；家庭資料保留，請確認網路線後重試。")
            atomic(self.path, record)
            self.transaction_path.unlink()
            sync_path(self.manager.root)
        except (OSError, ValueError, HostError, subprocess.SubprocessError):
            raise HostError("家庭網路暫時無法接通，自動恢復尚未完成；已保留資料與管理入口，請稍後重試。") from None
        return {**self.status(), "fallback": True, **({} if initial else {"restore_point": transaction["restore_point"]})}

    def default_choice(self):
        # Only pristine installations inherit a default. Existing NAT choices,
        # existing deployments, and interrupted journals are never overwritten.
        if self.path.exists() or self.manager.state_file.exists() or self.transaction_path.exists():
            return False
        try:
            if (self._xml("dumpxml", NAME, "--inactive", missing=True) is not None
                    or self._xml("net-dumpxml", NETWORK, missing=True) is not None):
                return False
            candidates = self.interfaces()
            if len(candidates) == 1 and candidates[0].get("dhcp"):
                self.configure(candidates[0]["id"])
                return True
        except (OSError, ValueError, HostError, subprocess.SubprocessError):
            pass
        return False


    def refresh_observation(self):
        """Called under the lifecycle lock; refresh first-install IP-only proof."""
        record = self.record()
        if record.get("pending"):
            return
        if (record["choice"] and (not record.get("attempt_id") or record.get("host_boot_id") != host_boot_id())
                and self.vm_state() == "running"):
            # Adopt deployed LAN state for read-only reporting, preserving any
            # owner's manual Core preferences during migration.
            record = self.begin_attempt(allow_configuration=False)
        if not record.get("attempt_id"):
            return
        path = self._observation_path(record["attempt_id"])
        if not path.exists():
            return
        body = json.loads(path.read_text())
        if body.get("attempt_id") != record["attempt_id"]:
            return
        try:
            self.observe(body, persist=False)
        except HostError:
            return
        if record.get("home") != body["home"]:
            record["home"] = body["home"]
            atomic(self.path, record)
