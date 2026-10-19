"""Bounded, owner-selected LAN observations. No port sweep or URL dereference.

The broker accepts only an opaque ID for a current physical IPv4 interface.
Protocol advertisements are untrusted hints, not device identity or authority.
"""

import hashlib
import ipaddress
import json
import os
import re
import selectors
import socket
import struct
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .manager import HostError

# Fixed discovery types from the corresponding Core 2026.10.2 manifests.
# Read PTR instances and only a Synology vendor classification from matching TXT.
# Never retain raw TXT values, credentials or fetch device descriptions.
SERVICES = ("_hue._tcp.local.", "_googlecast._tcp.local.", "_esphomelib._tcp.local.",
            "_hap._tcp.local.", "_hap._udp.local.", "_shelly._tcp.local.", "_matter._tcp.local.",
            "_matterc._udp.local.", "_ipp._tcp.local.", "_ipps._tcp.local.", "_http._tcp.local.")
MAX_ROWS = 256
MAX_PACKET = 8192
MAX_PACKETS = 512
MAX_BYTES = 1024 * 1024
MAX_COMMAND_BYTES = 256 * 1024
SCAN_SECONDS = 6.0
RECEIVE_SECONDS = 2.5
COOLDOWN_SECONDS = 15.0
_SCAN_LOCK = threading.Lock()
_LAST_SCAN = float("-inf")
_INTERFACE = re.compile(r"[a-zA-Z0-9_.:-]{1,32}")
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
_LIMITATIONS = ["ipv4_only", "physical_interfaces_only", "bridges_vlans_tunnels_not_supported",
                "selected_subnet_only", "no_port_sweep", "advertisements_are_unverified",
                "silent_devices_may_be_missing", "observation_does_not_grant_control"]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _text(value, limit=160):
    if not isinstance(value, str):
        return None
    value = "".join(char for char in value if char.isprintable()).strip()
    return value[:limit] or None


def _now():
    return datetime.now(UTC).isoformat()


def _physical(interface):
    # A real PCI/USB/platform network device has a sysfs device link. Virtual
    # interfaces (including a bridge over a physical NIC) are deliberately not
    # inferred to be household LANs.
    path = Path("/sys/class/net") / interface
    return (path / "device").exists() and "/virtual/" not in str(path.resolve())


def _ip_json(arguments, timeout):
    """Fixed ip(8) commands with a real byte cap, including while output arrives."""
    process = subprocess.Popen(["ip", *arguments], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    data = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HostError("discovery_command_timeout")
                if not selector.select(remaining):
                    raise HostError("discovery_command_timeout")
                block = os.read(process.stdout.fileno(), min(16384, MAX_COMMAND_BYTES + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > MAX_COMMAND_BYTES:
                    raise HostError("discovery_command_limit")
        if process.wait(timeout=max(0.001, deadline - time.monotonic())):
            raise HostError("discovery_command_unavailable")
        value = json.loads(data)
        if not isinstance(value, list):
            raise HostError("discovery_command_invalid")
        return value
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _name(packet, start):
    """Read a DNS name with bounded labels, compression depth and cycle checks."""
    labels, visited, position, resume, length = [], set(), start, None, 0
    while True:
        if position >= len(packet) or position in visited or len(visited) >= 128:
            raise ValueError("dns_name_invalid")
        visited.add(position)
        size = packet[position]
        if size & 0xC0 == 0xC0:
            if position + 1 >= len(packet):
                raise ValueError("dns_pointer_truncated")
            target = ((size & 0x3F) << 8) | packet[position + 1]
            if target >= position:
                raise ValueError("dns_pointer_not_prior")
            if resume is None:
                resume = position + 2
            position = target
            continue
        if size & 0xC0 or size > 63:
            raise ValueError("dns_label_invalid")
        position += 1
        if not size:
            return ".".join(labels) + ".", resume if resume is not None else position
        if position + size > len(packet):
            raise ValueError("dns_label_truncated")
        length += size + 1
        if length > 254:
            raise ValueError("dns_name_too_long")
        labels.append(packet[position:position + size].decode("utf-8", errors="replace"))
        position += size


def mdns_query(service):
    if service not in SERVICES:
        raise ValueError("unknown_service")
    encoded = b"".join(bytes([len(label)]) + label.encode("ascii") for label in service.rstrip(".").split(".")) + b"\0"
    # PTR, IN with the QU (unicast response) bit; a separate ephemeral socket
    # avoids binding or replacing the host's existing mDNS service.
    return struct.pack("!6H", 0, 0, 1, 0, 0, 0) + encoded + struct.pack("!HH", 12, 0x8001)


def _synology_vendor(data):
    """Classify one bounded TXT record; never return its raw field values."""
    position, vendors = 0, []
    while position < len(data):
        size = data[position]
        position += 1
        if position + size > len(data):
            return False
        field = data[position:position + size]
        position += size
        key, separator, value = field.partition(b"=")
        if key.lower() == b"vendor":
            vendors.append(bool(separator) and value.lower().startswith(b"synology"))
    # Duplicate keys are ambiguous, even when both make the same claim.
    return vendors == [True]


def _mdns_observations(packet):
    if not 12 <= len(packet) <= MAX_PACKET:
        raise ValueError("dns_packet_size")
    _, flags, questions, answers, authorities, additional = struct.unpack_from("!6H", packet)
    if not flags & 0x8000 or flags & 0x780F or flags & 0x0200:
        raise ValueError("dns_response_invalid")
    if questions > 64 or answers + authorities + additional > 512:
        raise ValueError("dns_record_limit")
    position, found, vendors = 12, {}, {}
    for _ in range(questions):
        _, position = _name(packet, position)
        position += 4
        if position > len(packet):
            raise ValueError("dns_question_truncated")
    for _ in range(answers + authorities + additional):
        owner, position = _name(packet, position)
        if position + 10 > len(packet):
            raise ValueError("dns_record_truncated")
        kind, cls, ttl, size = struct.unpack_from("!HHIH", packet, position)
        position += 10
        end = position + size
        if end > len(packet):
            raise ValueError("dns_data_truncated")
        owner = owner.lower()
        if kind == 12 and cls & 0x7FFF == 1 and ttl and owner in SERVICES:
            target, consumed = _name(packet, position)
            if consumed != end:
                raise ValueError("dns_ptr_size")
            suffix = "." + owner
            if target.lower().endswith(suffix):
                display = _text(target[:-len(suffix)])
                if display:
                    found[(owner, display)] = target.lower()
        if kind == 16 and cls & 0x7FFF == 1 and ttl and owner.endswith("._http._tcp.local."):
            # A claim applies only to a PTR instance in this same packet. Do not
            # join devices by responder IP or a similar display name. Multiple
            # TXT records for an instance are ambiguous and provide no claim.
            vendors[owner] = _synology_vendor(packet[position:end]) if owner not in vendors else False
        position = end
    return [(service, name, {"vendor": "synology"} if service == "_http._tcp.local."
             and vendors.get(target) else {}) for (service, name), target in sorted(found.items())]


def parse_mdns(packet):
    """Compatibility view of the fixed service types and instance labels."""
    return [(service, name) for service, name, _ in _mdns_observations(packet)]


def parse_ssdp(packet):
    if not packet or len(packet) > MAX_PACKET:
        raise ValueError("ssdp_packet_size")
    raw = packet.decode("ascii", errors="strict")
    if "\r\n\r\n" not in raw:
        raise ValueError("ssdp_header_truncated")
    lines = raw.split("\r\n\r\n", 1)[0].split("\r\n")
    if not re.fullmatch(r"HTTP/1\.[01] 200(?: [ -~]*)?", lines[0]) or len(lines) > 64:
        raise ValueError("ssdp_response_invalid")
    headers = {}
    for line in lines[1:]:
        if ":" not in line or len(line) > 1024:
            raise ValueError("ssdp_header_invalid")
        key, value = line.split(":", 1)
        key = key.lower()
        if not re.fullmatch(r"[a-z0-9-]+", key) or any(ord(char) < 32 for char in value.strip()):
            raise ValueError("ssdp_header_invalid")
        if key in {"st", "server", "usn"}:
            if key in headers:
                raise ValueError("ssdp_duplicate_hint")
            headers[key] = value.strip()
    service = headers.get("st", "")
    if not re.fullmatch(r"(?:urn:[A-Za-z0-9_.:-]{1,156}|upnp:rootdevice|uuid:[A-Za-z0-9-]{1,64})", service):
        raise ValueError("ssdp_service_invalid")
    metadata = {"service_type": service}
    server = _text(headers.get("server"))
    if server:
        metadata["server"] = server
    usn = headers.get("usn", "")
    if re.fullmatch(r"uuid:[A-Za-z0-9-]{1,64}(?:::[A-Za-z0-9_.:-]{1,88})?", usn) and len(usn) <= 160:
        metadata["usn"] = usn
    return service, metadata


def _in_scope(address, scope):
    try:
        value = ipaddress.IPv4Address(address)
        network = ipaddress.IPv4Network(scope["network"])
        return (value in network and not value.is_multicast and not value.is_unspecified
                and not value.is_loopback and value not in {network.network_address, network.broadcast_address}
                and str(value) != scope["address"])
    except (ValueError, TypeError):
        return False


def _observation(source, scope, stamp, address, name=None, mac=None, services=(), metadata=None):
    identity = [source, scope["id"], address, mac, name, sorted(services), (metadata or {}).get("usn")]
    return {"id": "obs_" + _digest(identity)[:40], "source_id": source, "scope_id": scope["id"],
            "address": address, "mac": mac, "name": name, "services": list(services),
            "observed_at": stamp, "source_updated_at": None,
            "reachability": "unknown" if source == "lan_neighbors" else "responded", "metadata": metadata or {}}


class Discovery:
    def __init__(self, manager=None, *, read_ip=None, physical=None, socket_factory=None):
        # Injectable seams are constructor-only and never exposed over the broker.
        self.read_ip = read_ip or _ip_json
        self.physical = physical or _physical
        self.socket_factory = socket_factory or socket.socket

    def _read(self, arguments, deadline=None):
        remaining = 1.0 if deadline is None else min(1.0, deadline - time.monotonic())
        if remaining <= 0:
            raise HostError("discovery_time_limit")
        value = self.read_ip(arguments, remaining)
        if not isinstance(value, list) or len(json.dumps(value).encode()) > MAX_COMMAND_BYTES:
            raise HostError("discovery_command_limit")
        return value

    def scopes(self, deadline=None):
        limitations = list(_LIMITATIONS)
        try:
            links = self._read(("-j", "-d", "link", "show"), deadline)
            addresses = self._read(("-j", "-4", "address", "show"), deadline)
        except (OSError, ValueError, HostError, subprocess.SubprocessError):
            return {"contract_version": 1, "scopes": [], "limitations": limitations + ["interface_inventory_unavailable"]}
        eligible, scopes = set(), []
        for link in links:
            if not isinstance(link, dict):
                continue
            name, flags = link.get("ifname"), link.get("flags", [])
            info = link.get("linkinfo") or {}
            if (isinstance(name, str) and _INTERFACE.fullmatch(name) and isinstance(flags, list)
                    and "UP" in flags and "LOWER_UP" in flags and "LOOPBACK" not in flags
                    and link.get("link_type") == "ether" and not link.get("master")
                    and isinstance(info, dict) and not info.get("info_kind") and self.physical(name)):
                eligible.add(name)
        for item in addresses:
            if not isinstance(item, dict) or not isinstance(item.get("ifname"), str) or item["ifname"] not in eligible:
                continue
            name = item["ifname"]
            for address in item.get("addr_info", []) if isinstance(item.get("addr_info"), list) else []:
                if not isinstance(address, dict) or address.get("family") != "inet" or address.get("scope") not in {"global", "link"}:
                    continue
                if address.get("tentative") or address.get("dadfailed"):
                    continue
                try:
                    prefix = address["prefixlen"]
                    if not isinstance(prefix, int) or isinstance(prefix, bool) or not 8 <= prefix <= 30:
                        continue
                    interface = ipaddress.IPv4Interface(str(address["local"]) + "/" + str(prefix))
                    if interface.ip.is_loopback or interface.ip.is_multicast or interface.ip.is_unspecified:
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                value = {"id": "scope_" + _digest([name, str(interface.ip), prefix])[:32],
                         "label": name + " · " + str(interface.network), "interface": name,
                         "address": str(interface.ip), "network": str(interface.network)}
                if value not in scopes:
                    scopes.append(value)
        scopes.sort(key=lambda item: (item["interface"], item["address"]))
        if len(scopes) > 32:
            limitations.append("interface_scope_limit")
        return {"contract_version": 1, "scopes": scopes[:32], "limitations": limitations}

    @staticmethod
    def _source(source, method, scope, stamp, limitations):
        return {"id": source, "adapter_version": 1, "state": "ok", "method": method, "target": scope["network"],
                "observed_at": stamp, "item_count": 0, "truncated": False,
                "reason_code": None, "content_digest": None, "limitations": limitations}

    def _neighbors(self, scope, stamp, deadline):
        source = self._source("lan_neighbors", "neighbor_table", scope, stamp,
                              ["cached_neighbors_only", "cache_does_not_prove_current_reachability"])
        rows = {}
        try:
            values = self._read(("-j", "-4", "neighbor", "show", "dev", scope["interface"]), deadline)
        except (OSError, ValueError, HostError, subprocess.SubprocessError):
            source.update(state="unavailable", reason_code="neighbor_table_unavailable")
            return source, rows
        for value in values:
            if not isinstance(value, dict):
                source.update(state="partial", reason_code="invalid_neighbor_record")
                continue
            if value.get("dev", scope["interface"]) != scope["interface"] or not _in_scope(value.get("dst"), scope):
                continue
            mac, state = value.get("lladdr"), value.get("state", [])
            if not isinstance(mac, str) or not _MAC.fullmatch(mac.lower()) or mac.lower() in {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}:
                continue
            if not isinstance(state, list) or not all(isinstance(item, str) for item in state) or set(state) & {"FAILED", "INCOMPLETE"}:
                continue
            row = _observation("lan_neighbors", scope, stamp, value["dst"], mac=mac.lower())
            if len(rows) >= MAX_ROWS and row["id"] not in rows:
                source.update(state="partial", reason_code="row_limit", truncated=True)
                break
            rows[row["id"]] = row
        return source, rows

    def _socket(self, scope, kind):
        sock = self.socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, scope["interface"].encode() + b"\0")
            sock.bind((scope["address"], 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(scope["address"]))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255 if kind == "mdns" else 1)
            sock.setblocking(False)
            if kind == "mdns":
                for service in SERVICES:
                    sock.sendto(mdns_query(service), ("224.0.0.251", 5353))
            else:
                sock.sendto(b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n',
                            ("239.255.255.250", 1900))
            return sock
        except Exception:
            sock.close()
            raise

    def _multicast(self, scope, stamp, deadline):
        sources = {kind: self._source("lan_" + kind, kind, scope, stamp,
                                     ["bounded_advertisement_window", "unverified_protocol_hints", "no_description_or_txt_fetch"])
                   for kind in ("mdns", "ssdp")}
        sources["mdns"]["limitations"].append("mdns_address_is_responder_not_verified_device")
        sources["mdns"]["limitations"].append("txt_only_matching_synology_vendor_classification")
        rows = {kind: {} for kind in sources}
        sockets, packets, received = [], 0, 0
        try:
            with selectors.DefaultSelector() as selector:
                for kind in sources:
                    if time.monotonic() >= deadline:
                        sources[kind].update(state="partial", reason_code="scan_time_limit")
                        continue
                    try:
                        sock = self._socket(scope, kind)
                        sockets.append(sock)
                        selector.register(sock, selectors.EVENT_READ, kind)
                    except OSError:
                        sources[kind].update(state="unavailable", reason_code="multicast_transport_unavailable")
                end = min(deadline, time.monotonic() + RECEIVE_SECONDS)
                while selector.get_map() and time.monotonic() < end:
                    ready = selector.select(max(0, end - time.monotonic()))
                    for key, _ in ready:
                        kind, sock = key.data, key.fileobj
                        try:
                            packet, sender = sock.recvfrom(MAX_PACKET + 1)
                        except BlockingIOError:
                            continue
                        except OSError:
                            sources[kind].update(state="partial", reason_code="multicast_receive_unavailable")
                            selector.unregister(sock)
                            continue
                        packets += 1
                        received += len(packet)
                        if packets > MAX_PACKETS or received > MAX_BYTES:
                            for source in sources.values():
                                if source["state"] != "unavailable":
                                    source.update(state="partial", reason_code="packet_limit", truncated=True)
                            return sources, rows
                        # RFC 6762 requires mDNS source port 5353. SSDP search
                        # responses return to our request socket; UDA 1.3.3 does
                        # not require their source port to be the search port.
                        if not _in_scope(sender[0], scope) or (kind == "mdns" and sender[1] != 5353):
                            continue
                        try:
                            if kind == "mdns":
                                additions = [_observation("lan_mdns", scope, stamp, sender[0], name=name,
                                                          services=[service], metadata={"service_type": service, **metadata})
                                             for service, name, metadata in _mdns_observations(packet)]
                            else:
                                service, metadata = parse_ssdp(packet)
                                additions = [_observation("lan_ssdp", scope, stamp, sender[0], services=[service], metadata=metadata)]
                        except (ValueError, UnicodeError, struct.error):
                            sources[kind].update(state="partial", reason_code="invalid_protocol_packet")
                            continue
                        for row in additions:
                            if len(rows[kind]) >= MAX_ROWS and row["id"] not in rows[kind]:
                                sources[kind].update(state="partial", reason_code="row_limit", truncated=True)
                                continue
                            rows[kind][row["id"]] = row
                if time.monotonic() >= deadline:
                    for source in sources.values():
                        if source["state"] != "unavailable":
                            source.update(state="partial", reason_code="scan_time_limit")
        finally:
            for sock in sockets:
                sock.close()
        return sources, rows

    def scan(self, body):
        global _LAST_SCAN
        if (not isinstance(body, dict) or set(body) != {"scope_id"} or not isinstance(body["scope_id"], str)
                or not re.fullmatch(r"scope_[a-f0-9]{32}", body["scope_id"])):
            raise HostError("discovery_scan_invalid")
        if not _SCAN_LOCK.acquire(blocking=False):
            raise HostError("discovery_scan_busy")
        try:
            started = time.monotonic()
            if started - _LAST_SCAN < COOLDOWN_SECONDS:
                raise HostError("discovery_scan_cooldown")
            deadline = started + SCAN_SECONDS
            scope = next((scope for scope in self.scopes(deadline)["scopes"] if scope["id"] == body["scope_id"]), None)
            if scope is None:
                raise HostError("discovery_scope_unavailable")
            _LAST_SCAN = started
            stamp = _now()
            neighbor_source, neighbor_rows = self._neighbors(scope, stamp, deadline)
            multicast_sources, multicast_rows = self._multicast(scope, stamp, deadline)
            sources = [neighbor_source, *multicast_sources.values()]
            rows = {"lan_neighbors": neighbor_rows, **{"lan_" + key: value for key, value in multicast_rows.items()}}
            observations = []
            for source in sources:
                selected = sorted(rows[source["id"]].values(), key=lambda row: row["id"])
                source["item_count"] = len(selected)
                source["content_digest"] = _digest(selected)
                observations.extend(selected)
            return {"contract_version": 1, "scope_id": scope["id"], "observed_at": stamp,
                    "sources": sources, "observations": observations, "limitations": list(_LIMITATIONS)}
        finally:
            _SCAN_LOCK.release()

    def handle(self, method, path, body=None):
        if method == "GET" and path == "/discovery/scopes":
            return 200, self.scopes()
        if method == "POST" and path == "/discovery/scan":
            return 200, self.scan(body)
        raise HostError("discovery_request_invalid")
