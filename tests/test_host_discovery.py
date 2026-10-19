"""Bounded Host LAN discovery with synthetic packets and loopback-only transport."""
import copy
import json
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agentic_controller.models import InventoryCollection, InventoryScopes

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host import discovery
from agentic_host.discovery import Discovery
from agentic_host.manager import HostError

STAMP = "2026-10-16T12:00:00+00:00"


def link(name, **fields):
    return {"ifname": name, "flags": ["UP", "LOWER_UP", "BROADCAST", "MULTICAST"],
            "link_type": "ether", **fields}


def address(name, ip="192.0.2.10", prefix=24):
    return {"ifname": name, "addr_info": [{"family": "inet", "scope": "global", "local": ip, "prefixlen": prefix}]}


@pytest.fixture
def broker(monkeypatch):
    raw = {"link": [link("enp1s0")], "address": [address("enp1s0")], "neighbor": []}
    calls = []

    def read(arguments, timeout):
        calls.append((arguments, timeout))
        key = next(key for key in raw if key in arguments)
        return copy.deepcopy(raw[key])

    monkeypatch.setattr(discovery, "_LAST_SCAN", float("-inf"))
    monkeypatch.setattr(discovery, "_now", lambda: STAMP)
    instance = Discovery(read_ip=read, physical=lambda name: name in {"enp1s0", "wlp2s0"})
    return instance, raw, calls


def dns_name(name):
    return b"".join(bytes([len(part.encode())]) + part.encode() for part in name.rstrip(".").split(".")) + b"\0"


def ptr_packet(service="_hue._tcp.local.", name="Fixture bridge", *, ttl=120, txt=False):
    owner = dns_name(service)
    target = bytes([len(name.encode())]) + name.encode() + b"\xc0\x0c"
    answer = owner + struct.pack("!HHIH", 12, 1, ttl, len(target)) + target
    extra = b""
    if txt:
        secret = b"\x19api_key=do-not-return-this"
        extra = dns_name(name + "." + service) + struct.pack("!HHIH", 16, 1, 120, len(secret)) + secret
    return struct.pack("!6H", 0, 0x8400, 0, 1, 0, int(txt)) + answer + extra


def ssdp_packet(service="urn:schemas-upnp-org:device:Basic:1", usn="uuid:fixture-device"):
    return ("HTTP/1.1 200 OK\r\nST: " + service + "\r\nUSN: " + usn
            + "\r\nSERVER: FixtureOS/1.0 UPnP/1.1\r\nLOCATION: http://user:secret@192.0.2.30/private.xml"
            + "\r\nX-Private-Token: hidden-secret\r\n\r\n").encode()


def with_vendor_txt(packet, *fields, instance="Fixture NAS", service="_http._tcp.local.", copies=1, ttl=120):
    header = list(struct.unpack_from("!6H", packet))
    header[5] += copies
    data = b"".join(bytes([len(field)]) + field for field in fields)
    record = dns_name(instance + "." + service) + struct.pack("!HHIH", 16, 1, ttl, len(data)) + data
    return struct.pack("!6H", *header) + packet[12:] + record * copies


def test_scopes_only_current_physical_links_and_exact_local_ipv4(broker):
    service, raw, calls = broker
    raw["link"] += [link("wlp2s0"), link("docker0"), link("virbr0", linkinfo={"info_kind": "bridge"}),
                    link("enp1s0.50", linkinfo={"info_kind": "vlan"}), link("tun0", link_type="none"),
                    link("lo", flags=["UP", "LOWER_UP", "LOOPBACK"]), link("down0", flags=["UP"])]
    raw["address"] += [address("wlp2s0", "192.168.50.8"), address("docker0", "172.17.0.1"),
                       address("virbr0", "192.168.122.1"), address("enp1s0.50", "192.168.50.10"),
                       address("tun0", "100.64.1.1"), address("lo", "127.0.0.1", 8), address("down0")]
    result = InventoryScopes.model_validate(service.scopes()).model_dump()
    assert [scope["interface"] for scope in result["scopes"]] == ["enp1s0", "wlp2s0"]
    assert result["scopes"][0]["network"] == "192.0.2.0/24"
    assert "bridges_vlans_tunnels_not_supported" in result["limitations"]
    assert [args for args, _ in calls] == [("-j", "-d", "link", "show"), ("-j", "-4", "address", "show")]
    assert all(0 < timeout <= 1 for _, timeout in calls)
    assert service.scopes()["scopes"][0]["id"] == result["scopes"][0]["id"]
    raw["address"][0] = address("enp1s0", "192.0.2.11")
    assert service.scopes()["scopes"][0]["id"] != result["scopes"][0]["id"]


def test_scopes_reject_enslaved_links_bad_types_and_unusable_addresses(broker):
    service, raw, _ = broker
    raw["link"] += [None, link("wlp2s0", master="br0"), {"ifname": []}, link("x;shutdown")]
    raw["address"] += [None, {"ifname": []}, address("wlp2s0"), address("enp1s0", "0.0.0.0"),
                       address("enp1s0", "224.0.0.1"), address("enp1s0", "127.0.0.1"),
                       address("enp1s0", prefix=0), address("enp1s0", prefix=True), address("enp1s0", prefix=32)]
    assert len(service.scopes()["scopes"]) == 1
    raw["address"] = [address("enp1s0") for _ in range(100)]
    assert len(service.scopes()["scopes"]) == 1
    raw["link"] = []
    assert service.scopes()["scopes"] == []


def test_scope_inventory_reports_command_failure_without_disclosing_output(broker):
    service, _, _ = broker

    def failed(*args):
        raise HostError("private-host-command-output")

    service.read_ip = failed
    result = service.scopes()
    assert result["scopes"] == []
    assert "interface_inventory_unavailable" in result["limitations"]
    assert "private-host" not in json.dumps(result)


def test_scopes_bound_results_and_reject_excessive_command_output(broker):
    service, raw, _ = broker
    raw["address"] = [address("enp1s0", f"192.0.2.{index}") for index in range(1, 50)]
    assert len(service.scopes()["scopes"]) == 32
    assert "interface_scope_limit" in service.scopes()["limitations"]
    raw["address"] = [{"ignored": "x" * (discovery.MAX_COMMAND_BYTES + 1)}]
    assert service.scopes()["scopes"] == []


def test_mdns_queries_fixed_services_qu_flag_and_compressed_ptr_ignores_txt():
    for service in discovery.SERVICES:
        packet = discovery.mdns_query(service)
        assert struct.unpack_from("!6H", packet) == (0, 0, 1, 0, 0, 0)
        name, position = discovery._name(packet, 12)
        assert name == service
        assert struct.unpack_from("!HH", packet, position) == (12, 0x8001)
    with pytest.raises(ValueError):
        discovery.mdns_query("_secret._tcp.local.")
    result = discovery.parse_mdns(ptr_packet(txt=True))
    assert result == [("_hue._tcp.local.", "Fixture bridge")]
    assert "api_key" not in json.dumps(result)
    assert discovery.parse_mdns(ptr_packet(ttl=0)) == []
    assert discovery.parse_mdns(ptr_packet(service="_unknown._tcp.local.")) == []


@pytest.mark.parametrize("service", ["_ipp._tcp.local.", "_ipps._tcp.local.", "_matterc._udp.local."])
def test_common_device_ptrs_are_queried_and_collected_without_txt(service):
    packet = discovery.mdns_query(service)
    name, position = discovery._name(packet, 12)
    assert name == service and struct.unpack_from("!HH", packet, position) == (12, 0x8001)
    assert discovery.parse_mdns(ptr_packet(service=service, name="Fixture shared service", txt=True)) == [
        (service, "Fixture shared service")]
    assert discovery.parse_mdns(ptr_packet(service=service, ttl=0)) == []


@pytest.mark.parametrize("fields,expected", [
    ((b"vendor=Synology", b"password=never-return-this"), {"vendor": "synology"}),
    ((b"VeNdOr=synology-private-suffix",), {"vendor": "synology"}),
    ((b"vendor=NotSynology",), {}), ((b"vendor=",), {}), ((b"vendor",), {}),
    ((b"vendor=Synology", b"VENDOR=Other"), {}),
    ((b"vendor=Synology", b"vendor=Synology"), {}), ((b"manufacturer=Synology",), {}),
])
def test_mdns_vendor_projects_only_fixed_classification_on_exact_ptr(fields, expected):
    packet = with_vendor_txt(ptr_packet(service="_http._tcp.local.", name="Fixture NAS"), *fields)
    assert discovery._mdns_observations(packet) == [("_http._tcp.local.", "Fixture NAS", expected)]
    assert discovery.parse_mdns(packet) == [("_http._tcp.local.", "Fixture NAS")]


@pytest.mark.parametrize("extra", [
    {"instance": "Different NAS"}, {"service": "_ipp._tcp.local."}, {"copies": 2}, {"ttl": 0},
])
def test_mdns_vendor_cannot_bind_other_instance_service_ambiguous_or_expired_txt(extra):
    packet = with_vendor_txt(ptr_packet(service="_http._tcp.local.", name="Fixture NAS"), b"vendor=Synology", **extra)
    assert discovery._mdns_observations(packet) == [("_http._tcp.local.", "Fixture NAS", {})]


def test_malformed_vendor_txt_cannot_supply_classification():
    packet = with_vendor_txt(ptr_packet(service="_http._tcp.local.", name="Fixture NAS"), b"vendor=Synology")
    # The record length is valid, but its first TXT string overstates its size.
    packet = packet[:-16] + b"\xff" + packet[-15:]
    assert discovery._mdns_observations(packet) == [("_http._tcp.local.", "Fixture NAS", {})]


@pytest.mark.parametrize("packet", [b"", b"\0" * 11, b"x" * (discovery.MAX_PACKET + 1),
    struct.pack("!6H", 0, 0, 0, 0, 0, 0), struct.pack("!6H", 0, 0x8200, 0, 0, 0, 0),
    struct.pack("!6H", 0, 0x8000, 65, 0, 0, 0), struct.pack("!6H", 0, 0x8000, 0, 513, 0, 0),
    struct.pack("!6H", 0, 0x8000, 0, 1, 0, 0) + b"\xc0\x0c",
    struct.pack("!6H", 0, 0x8000, 0, 1, 0, 0) + b"\xc0\xff",
    struct.pack("!6H", 0, 0x8000, 0, 1, 0, 0) + b"\x40" + b"a" * 64 + b"\0",
    ptr_packet()[:-1]])
def test_mdns_malformed_packets_and_pointer_cycles_are_bounded(packet):
    with pytest.raises(ValueError):
        discovery.parse_mdns(packet)


def test_dns_indirect_compression_loop_and_overlong_name_rejected():
    with pytest.raises(ValueError):
        discovery._name(b"\x01a\xc0\x00", 0)
    with pytest.raises(ValueError):
        discovery._name((b"\x3f" + b"a" * 63) * 4 + b"\0", 0)


def test_ssdp_only_projected_protocol_hints_no_location_or_unknown_headers():
    service, metadata = discovery.parse_ssdp(ssdp_packet())
    assert service == "urn:schemas-upnp-org:device:Basic:1"
    assert metadata == {"service_type": service, "server": "FixtureOS/1.0 UPnP/1.1", "usn": "uuid:fixture-device"}
    assert not any(value in json.dumps(metadata) for value in ("secret", "http:", "LOCATION", "private.xml"))
    _, metadata = discovery.parse_ssdp(ssdp_packet(usn="https://credential:secret@evil.test"))
    assert "usn" not in metadata


@pytest.mark.parametrize("packet", [b"", b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\n",
    b"NOTIFY * HTTP/1.1\r\nST: upnp:rootdevice\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nST: upnp:rootdevice\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nST: https://evil.test/secret\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nSERVER: invalid\x01header\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nSERVER: \xff\r\n\r\n"])
def test_ssdp_malformed_and_ambiguous_headers_fail_closed(packet):
    with pytest.raises(ValueError):
        discovery.parse_ssdp(packet)


def test_neighbors_cannot_claim_reachability_and_ignore_cross_subnet_or_incomplete(broker):
    service, raw, calls = broker
    scope = service.scopes()["scopes"][0]
    raw["neighbor"] = [
        {"dst": "192.0.2.20", "lladdr": "AA:BB:CC:00:00:01", "state": ["REACHABLE"]},
        {"dst": "192.0.2.21", "lladdr": "aa:bb:cc:00:00:02", "state": ["STALE"]},
        {"dst": "192.0.2.22", "lladdr": "aa:bb:cc:00:00:03", "state": ["FAILED"]},
        {"dst": "192.0.3.20", "lladdr": "aa:bb:cc:00:00:04", "state": ["REACHABLE"]},
        {"dst": "192.0.2.10", "lladdr": "aa:bb:cc:00:00:05", "state": ["REACHABLE"]},
        {"dst": "192.0.2.23", "lladdr": "aa:bb:cc:00:00:06", "state": [{}]},
        {"dst": "192.0.2.24", "lladdr": "aa:bb:cc:00:00:07", "state": ["STALE"], "dev": "wlp2s0"},
        {"dst": "192.0.2.25", "lladdr": "ff:ff:ff:ff:ff:ff", "state": ["STALE"]},
    ]
    source, rows = service._neighbors(scope, STAMP, time.monotonic() + 3)
    assert source["state"] == "ok"
    assert sorted(row["address"] for row in rows.values()) == ["192.0.2.20", "192.0.2.21"]
    assert all(row["reachability"] == "unknown" and row["metadata"] == {} for row in rows.values())
    assert calls[-1][0] == ("-j", "-4", "neighbor", "show", "dev", "enp1s0")


def test_neighbor_limits_and_source_failure_are_reported(broker, monkeypatch):
    service, raw, _ = broker
    scope = service.scopes()["scopes"][0]
    monkeypatch.setattr(discovery, "MAX_ROWS", 2)
    raw["neighbor"] = [None, *[{"dst": f"192.0.2.{index}", "lladdr": f"aa:bb:cc:00:00:{index:02x}", "state": ["STALE"]}
                               for index in range(20, 24)]]
    source, rows = service._neighbors(scope, STAMP, time.monotonic() + 3)
    assert len(rows) == 2 and source["state"] == "partial" and source["truncated"]
    service.read_ip = lambda *_: (_ for _ in ()).throw(OSError("private route"))
    source, rows = service._neighbors(scope, STAMP, time.monotonic() + 3)
    assert source["state"] == "unavailable" and not rows
    assert "private route" not in json.dumps(source)


def test_socket_interface_binding_and_only_fixed_multicast_requests(broker):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]

    class RecordingSocket:
        def __init__(self, *_):
            self.options, self.sent, self.closed = [], [], False

        def setsockopt(self, *args):
            self.options.append(args)

        def bind(self, value):
            self.bound = value

        def setblocking(self, value):
            self.blocking = value

        def sendto(self, *args):
            self.sent.append(args)

        def close(self):
            self.closed = True

    service.socket_factory = RecordingSocket
    for kind in ("mdns", "ssdp"):
        sock = service._socket(scope, kind)
        assert sock.bound == ("192.0.2.10", 0) and not sock.blocking
        assert (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"enp1s0\0") in sock.options
        assert (socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("192.0.2.10")) in sock.options
        assert (socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255 if kind == "mdns" else 1) in sock.options
        if kind == "mdns":
            assert len(sock.sent) == len(discovery.SERVICES)
            assert {target for _, target in sock.sent} == {("224.0.0.251", 5353)}
        else:
            assert sock.sent[0][1] == ("239.255.255.250", 1900)
            assert b"MX: 1\r\nST: ssdp:all" in sock.sent[0][0]
        sock.close()


@pytest.mark.parametrize("fail_at", ["setsockopt", "bind", "sendto"])
def test_socket_setup_failure_always_closes_socket(broker, fail_at):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]

    class FailedSocket:
        closed = False

        def __getattr__(self, name):
            def operation(*args):
                if name == fail_at:
                    raise OSError("fixture socket error")
            return operation

        def close(self):
            self.closed = True

    sock = FailedSocket()
    service.socket_factory = lambda *_: sock
    with pytest.raises(OSError):
        service._socket(scope, "mdns")
    assert sock.closed


class LocalDatagrams:
    """Kernel UDP transport on 127.0.0.1; synthetic peer metadata is explicit."""
    def __init__(self, packets):
        self.reader = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.reader.bind(("127.0.0.1", 0))
        self.reader.setblocking(False)
        self.peers = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as writer:
            for packet, peer in packets:
                writer.sendto(packet, self.reader.getsockname())
                self.peers.append(peer)
        self.closed = False

    def fileno(self):
        return self.reader.fileno()

    def recvfrom(self, size):
        packet, _ = self.reader.recvfrom(size)
        return packet, self.peers.pop(0)

    def close(self):
        self.closed = True
        self.reader.close()


def install_local_transport(service, monkeypatch, packets):
    sockets = []

    def opened(scope, kind):
        sock = LocalDatagrams(packets.get(kind, []))
        sockets.append(sock)
        return sock

    monkeypatch.setattr(service, "_socket", opened)
    monkeypatch.setattr(discovery, "RECEIVE_SECONDS", 0.1)
    return sockets


@pytest.mark.parametrize("ssdp_port", [1900, 49152])
def test_loopback_transport_full_scan_keeps_sources_separate_and_rejects_outside_scope(broker, monkeypatch, ssdp_port):
    service, raw, _ = broker
    scope = service.scopes()["scopes"][0]
    raw["neighbor"] = [{"dst": "192.0.2.20", "lladdr": "aa:bb:cc:00:00:01", "state": ["REACHABLE"]}]
    sockets = install_local_transport(service, monkeypatch, {
        "mdns": [(ptr_packet(txt=True), ("192.0.2.20", 5353)), (ptr_packet(), ("192.0.3.20", 5353)),
                 (ptr_packet(), ("192.0.2.21", 53)), (ptr_packet(), ("192.0.2.10", 5353))],
        "ssdp": [(ssdp_packet(), ("192.0.2.20", ssdp_port))]})
    result = InventoryCollection.model_validate(service.scan({"scope_id": scope["id"]})).model_dump()
    assert len(result["observations"]) == 3
    assert {row["source_id"] for row in result["observations"]} == {"lan_neighbors", "lan_mdns", "lan_ssdp"}
    assert len({row["id"] for row in result["observations"]}) == 3
    assert all(source["state"] == "ok" and source["item_count"] == 1 and source["adapter_version"] == 1 for source in result["sources"])
    assert all(len(source["content_digest"]) == 64 for source in result["sources"])
    assert all(sock.closed for sock in sockets)
    assert not any(secret in json.dumps(result) for secret in ("secret", "api_key", "LOCATION"))


def test_printer_proxy_and_matter_records_stay_separate_in_bounded_scan(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    types = ["_ipp._tcp.local.", "_ipps._tcp.local.", "_matterc._udp.local.", "_matter._tcp.local."]
    # A responder can proxy several services; its address/name is not device identity.
    packets = [(ptr_packet(service=kind, name="Fixture shared service", txt=True), ("192.0.2.20", 5353))
               for kind in types]
    packets += [(ptr_packet(service="_ipp._tcp.local."), ("192.0.3.20", 5353)),
                (ptr_packet(service="_matterc._udp.local."), ("192.0.2.21", 53))]
    sockets = install_local_transport(service, monkeypatch, {"mdns": packets})
    result = InventoryCollection.model_validate(service.scan({"scope_id": scope["id"]})).model_dump()
    rows = result["observations"]
    assert len(rows) == len(types) and len({row["id"] for row in rows}) == len(types)
    assert {row["services"][0] for row in rows} == set(types)
    assert all(row["address"] == "192.0.2.20" and row["reachability"] == "responded" for row in rows)
    assert "api_key" not in json.dumps(result)
    mdns = next(source for source in result["sources"] if source["id"] == "lan_mdns")
    assert "mdns_address_is_responder_not_verified_device" in mdns["limitations"]
    assert all(sock.closed for sock in sockets)


def test_nas_vendor_scan_never_keeps_raw_txt_or_associates_by_address(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    nas = with_vendor_txt(ptr_packet(service="_http._tcp.local.", name="Fixture NAS"),
                          b"vendor=Synology-private-suffix", b"password=never-persist-this")
    sockets = install_local_transport(service, monkeypatch, {"mdns": [
        (nas, ("192.0.2.20", 5353)),
        (ptr_packet(service="_http._tcp.local.", name="Other HTTP server"), ("192.0.2.20", 5353))]})
    result = InventoryCollection.model_validate(service.scan({"scope_id": scope["id"]})).model_dump()
    rows = {row["name"]: row for row in result["observations"]}
    assert rows["Fixture NAS"]["metadata"] == {"service_type": "_http._tcp.local.", "vendor": "synology"}
    assert rows["Other HTTP server"]["metadata"] == {"service_type": "_http._tcp.local."}
    assert not any(secret in json.dumps(result) for secret in ("password", "never-persist", "private-suffix"))
    assert all(sock.closed for sock in sockets)


def test_multicast_partial_packets_dedup_and_row_limit(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    monkeypatch.setattr(discovery, "MAX_ROWS", 2)
    sockets = install_local_transport(service, monkeypatch, {
        "mdns": [(b"malformed", ("192.0.2.20", 5353)), (ptr_packet(), ("192.0.2.20", 5353)),
                 (ptr_packet(), ("192.0.2.20", 5353)), (ptr_packet(name="Other"), ("192.0.2.20", 5353)),
                 (ptr_packet(name="Third"), ("192.0.2.20", 5353))], "ssdp": []})
    sources, rows = service._multicast(scope, STAMP, time.monotonic() + 3)
    assert len(rows["mdns"]) == 2
    assert sources["mdns"]["state"] == "partial" and sources["mdns"]["truncated"]
    assert sources["ssdp"]["state"] == "ok" and not rows["ssdp"]
    assert all(sock.closed for sock in sockets)


@pytest.mark.parametrize("limit", ["MAX_PACKETS", "MAX_BYTES"])
def test_multicast_global_packet_and_byte_caps_include_ignored_traffic(broker, monkeypatch, limit):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    monkeypatch.setattr(discovery, limit, 2)
    sockets = install_local_transport(service, monkeypatch, {
        "mdns": [(ptr_packet(), ("192.0.3.20", 5353))] * 4,
        "ssdp": [(ssdp_packet(), ("192.0.3.20", 1900))] * 4})
    sources, rows = service._multicast(scope, STAMP, time.monotonic() + 3)
    assert all(source["state"] == "partial" and source["truncated"] and source["reason_code"] == "packet_limit"
               for source in sources.values())
    assert not rows["mdns"] and not rows["ssdp"]
    assert all(sock.closed for sock in sockets)


def test_one_multicast_transport_failure_keeps_other_source_working(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    sockets = []

    def opened(scope, kind):
        if kind == "mdns":
            raise OSError("private-host-interface-details")
        sock = LocalDatagrams([(ssdp_packet(), ("192.0.2.20", 1900))])
        sockets.append(sock)
        return sock

    monkeypatch.setattr(service, "_socket", opened)
    monkeypatch.setattr(discovery, "RECEIVE_SECONDS", 0.01)
    sources, rows = service._multicast(scope, STAMP, time.monotonic() + 3)
    assert sources["mdns"]["state"] == "unavailable"
    assert sources["ssdp"]["state"] == "ok" and len(rows["ssdp"]) == 1
    assert all(sock.closed for sock in sockets)
    assert "private-host" not in json.dumps(sources)


def test_receive_error_closes_socket_and_other_source_survives(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    sockets = []

    class BrokenReceiver(LocalDatagrams):
        def recvfrom(self, size):
            raise OSError("private-host-error")

    def opened(scope, kind):
        cls = BrokenReceiver if kind == "mdns" else LocalDatagrams
        sock = cls([(ptr_packet(), ("192.0.2.20", 5353))] if kind == "mdns" else [(ssdp_packet(), ("192.0.2.20", 1900))])
        sockets.append(sock)
        return sock

    monkeypatch.setattr(service, "_socket", opened)
    monkeypatch.setattr(discovery, "RECEIVE_SECONDS", 0.01)
    sources, rows = service._multicast(scope, STAMP, time.monotonic() + 3)
    assert sources["mdns"]["state"] == "partial" and sources["mdns"]["reason_code"] == "multicast_receive_unavailable"
    assert sources["ssdp"]["state"] == "ok" and len(rows["ssdp"]) == 1
    assert all(sock.closed for sock in sockets)


def test_elapsed_scan_deadline_stops_receive_window_and_marks_partial(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    sockets = install_local_transport(service, monkeypatch, {})
    started = time.monotonic()
    sources, rows = service._multicast(scope, STAMP, started - 0.001)
    assert time.monotonic() - started < 0.5
    assert all(source["state"] == "partial" and source["reason_code"] == "scan_time_limit" for source in sources.values())
    assert not rows["mdns"] and not rows["ssdp"]
    assert sockets == []


def test_scan_requires_unchanged_scope_and_never_takes_addresses_or_commands(broker, monkeypatch):
    service, raw, _ = broker
    scope = service.scopes()["scopes"][0]
    called = []
    monkeypatch.setattr(service, "_socket", lambda *args: called.append(args))
    for body in ({"scope_id": "192.0.2.0/24"}, {"scope_id": scope["id"], "cidr": "10.0.0.0/8"},
                 {"scope_id": [scope["id"]]}, {"command": "nmap"}, []):
        with pytest.raises(HostError, match="discovery_scan_invalid"):
            service.scan(body)
    raw["address"][0] = address("enp1s0", "192.0.2.11")
    with pytest.raises(HostError, match="discovery_scope_unavailable"):
        service.scan({"scope_id": scope["id"]})
    assert not called and not discovery._SCAN_LOCK.locked()


def test_scan_lock_and_cooldown_cover_all_service_instances(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]
    install_local_transport(service, monkeypatch, {})
    discovery._SCAN_LOCK.acquire()
    try:
        with pytest.raises(HostError, match="discovery_scan_busy"):
            service.scan({"scope_id": scope["id"]})
    finally:
        discovery._SCAN_LOCK.release()
    service.scan({"scope_id": scope["id"]})
    other = Discovery(read_ip=service.read_ip, physical=service.physical)
    with pytest.raises(HostError, match="discovery_scan_cooldown"):
        other.scan({"scope_id": scope["id"]})
    assert not discovery._SCAN_LOCK.locked()
    monkeypatch.setattr(discovery, "_LAST_SCAN", time.monotonic() - discovery.COOLDOWN_SECONDS - 1)
    assert service.scan({"scope_id": scope["id"]})["scope_id"] == scope["id"]


def test_failed_scan_releases_lock_and_keeps_cooldown(broker, monkeypatch):
    service, _, _ = broker
    scope = service.scopes()["scopes"][0]

    def failed(*args):
        raise RuntimeError("internal fixture failure")

    monkeypatch.setattr(service, "_neighbors", failed)
    with pytest.raises(RuntimeError, match="internal fixture failure"):
        service.scan({"scope_id": scope["id"]})
    assert not discovery._SCAN_LOCK.locked()
    with pytest.raises(HostError, match="discovery_scan_cooldown"):
        service.scan({"scope_id": scope["id"]})


def test_broker_handles_only_exact_discovery_routes(broker):
    service, _, _ = broker
    assert service.handle("GET", "/discovery/scopes")[0] == 200
    for method, path, body in [("GET", "/discovery/scan", None), ("POST", "/discovery/scopes", {}),
                               ("POST", "/discovery/scan?cidr=any", {}), ("POST", "/discovery/command", {})]:
        with pytest.raises(HostError, match="discovery_request_invalid"):
            service.handle(method, path, body)


@pytest.mark.parametrize("script,timeout,expected", [
    ("import sys;sys.stdout.write('[{\"ifname\":\"fixture0\"}]')", 1, "ok"),
    ("import sys;sys.stdout.write('x' * 400000);sys.stdout.flush()", 1, "limit"),
    ("import time;time.sleep(10)", 0.03, "timeout"),
    ("import sys;sys.stdout.write('[]');sys.exit(1)", 1, "unavailable"),
])
def test_fixed_ip_reader_bounds_real_subprocess_bytes_time_and_reaps(monkeypatch, script, timeout, expected):
    actual = subprocess.Popen
    processes, calls = [], []

    def launch(args, **kwargs):
        calls.append((args, kwargs))
        process = actual([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(discovery.subprocess, "Popen", launch)
    if expected == "ok":
        assert discovery._ip_json(("-j", "-d", "link", "show"), timeout) == [{"ifname": "fixture0"}]
    else:
        with pytest.raises(HostError, match="discovery_command_" + expected):
            discovery._ip_json(("-j", "-d", "link", "show"), timeout)
    assert calls[0][0] == ["ip", "-j", "-d", "link", "show"]
    assert "shell" not in calls[0][1]
    assert all(process.poll() is not None and process.stdout.closed for process in processes)
