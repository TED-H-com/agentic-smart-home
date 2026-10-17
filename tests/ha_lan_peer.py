"""Synthetic DHCP/IPv6/mDNS peer inside the disposable QEMU Docker namespace.

This program is never installed in the product. It requires an explicit launch
argument and the fixture namespace marker before making any network change.
"""
import ipaddress
import os
import select
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

DEVICE = "ahltap"
IPV4 = "198.18.42.1"
IPV6 = "fd42:177:42::1"
SERVICE = "_hap._tcp.local."
INSTANCE = "Agentic LAN Fixture." + SERVICE
HOST = "agentic-lan-fixture.local."


def name(value):
    return b"".join(bytes([len(label)]) + label.encode() for label in value.rstrip(".").split(".")) + b"\0"


def record(owner, kind, data):
    return name(owner) + struct.pack("!HHIH", kind, 1, 30, len(data)) + data


def advertisement():
    properties = ("id=02:11:22:33:44:55", "md=AgenticLANFixture", "sf=1", "ci=5", "c#=1", "s#=1", "pv=1.1", "ff=0")
    txt = b"".join(bytes([len(value)]) + value.encode() for value in properties)
    return (struct.pack("!6H", 0, 0x8400, 0, 5, 0, 0)
            + record(SERVICE, 12, name(INSTANCE))
            + record(INSTANCE, 33, struct.pack("!3H", 0, 0, 9) + name(HOST))
            + record(INSTANCE, 16, txt)
            + record(HOST, 1, ipaddress.ip_address(IPV4).packed)
            + record(HOST, 28, ipaddress.ip_address(IPV6).packed))


def serve():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, DEVICE.encode() + b"\0")
    sock.bind(("", 5353))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(IPV4))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton("224.0.0.251") + socket.inet_aton(IPV4))
    packet = advertisement()
    while True:
        sock.sendto(packet, ("224.0.0.251", 5353))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not select.select([sock], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            received, peer = sock.recvfrom(8192)
            if len(received) >= 12 and not received[2] & 0x80:
                sock.sendto(packet, peer if peer[1] != 5353 else ("224.0.0.251", 5353))


def launch(arguments):
    if os.environ.get("AGENTIC_DISPOSABLE_LAN_LAB") != "1" or not Path("/lab/linux.qcow2").is_file():
        raise RuntimeError("disposable_network_namespace_required")
    if not arguments or arguments[0] != "qemu-system-x86_64":
        raise RuntimeError("fixed_qemu_launcher_required")
    commands = [
        ["ip", "tuntap", "add", "dev", DEVICE, "mode", "tap"],
        ["ip", "address", "add", IPV4 + "/24", "dev", DEVICE],
        ["ip", "-6", "address", "add", IPV6 + "/64", "dev", DEVICE, "nodad"],
        ["ip", "link", "set", DEVICE, "up"],
        ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "198.18.42.0/24", "-o", "eth0", "-j", "MASQUERADE"],
        ["iptables", "-A", "FORWARD", "-i", DEVICE, "-j", "ACCEPT"],
        ["iptables", "-A", "FORWARD", "-o", DEVICE, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    ]
    for command in commands:
        subprocess.run(command, check=True, timeout=10)
    dnsmasq = ["dnsmasq", "--no-daemon", "--conf-file=/dev/null", "--bind-interfaces", "--interface=" + DEVICE,
               "--except-interface=lo", "--dhcp-authoritative",
               "--dhcp-range=198.18.42.100,198.18.42.150,255.255.255.0,1h",
               "--dhcp-host=52:54:00:aa:42:01,198.18.42.10", "--dhcp-leasefile=/lab/lan-leases",
               "--enable-ra", "--dhcp-range=fd42:177:42::,ra-stateless,64,1h", "--ra-param=" + DEVICE + ",10,60"]
    with open("/lab/lan-peer.log", "ab") as output:
        subprocess.Popen(dnsmasq, stdout=output, stderr=output)
        subprocess.Popen([sys.executable, __file__, "serve"], stdout=output, stderr=output)
    os.execvp(arguments[0], arguments)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "launch":
        launch(sys.argv[2:])
    elif sys.argv[1:] == ["serve"] and os.environ.get("AGENTIC_DISPOSABLE_LAN_LAB") == "1":
        serve()
    else:
        raise SystemExit("explicit disposable lab launch required")
