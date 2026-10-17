"""The acceptance probe requires native evidence; fixtures do not claim HAOS proof."""
import copy
import json
import os
import struct
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import ha_lan_peer
import installed_ha_lan_probe as probe
import pytest
from test_linux_install import native_api_probe_script


@pytest.fixture
def observed(tmp_path, monkeypatch):
    private = tmp_path / "identity.json"
    private.write_text(json.dumps({"token": "private-fixture-token"}))
    monkeypatch.setattr(probe, "PRIVATE", private)
    management = {"interface": "enp1s0", "mac": "52:54:00:aa:17:02", "connected": True,
                  "ipv4": {"address": ["192.168.177.2/24"], "gateway": None}}
    lan = {"interface": "enp2s0", "mac": "02:00:00:00:00:42", "connected": True,
           "ipv4": {"address": ["198.18.42.100/24"], "gateway": "198.18.42.1"},
           "ipv6": {"address": ["fd42:177:42::100/64"]}}
    values = {"network": {"configured_adapters": [], "adapters": [{"name": "enp1s0", "default": False, "enabled": False},
              {"name": "enp2s0", "default": True, "enabled": True}]},
              "supervisor/api": {"interfaces": [management, lan]},
              "config_entries/flow/progress": [{"handler": "homekit_controller", "context": {"source": "zeroconf",
                  "title_placeholders": {"name": "Agentic LAN Fixture"}}}],
              "network/url": {"internal": "http://198.18.42.100:8123"}}
    status = {"state": "configured", "mode": "wired", "guest_mac": lan["mac"], "home": {"state": "ready"}}
    calls = []
    async def controller(client):
        return {}
    async def request(client, method, url, **kwargs):
        assert method == "GET" and url.endswith("/api/network/ha-lan")
        return copy.deepcopy(status)
    async def ws(client, token, command, **kwargs):
        assert token == "private-fixture-token"
        calls.append((command, kwargs))
        return copy.deepcopy(values[command])
    monkeypatch.setattr(probe, "controller", controller)
    monkeypatch.setattr(probe, "request", request)
    monkeypatch.setattr(probe, "ws", ws)
    return values, status, calls


async def test_probe_requires_native_route_ipv6_discovery_and_product_readback(observed):
    _, _, calls = observed
    result = await probe.observe(None, True)
    assert result["native_mdns_flow_observed"] and result["ipv6_ra"] and result["product_observation_ready"]
    assert {command for command, _ in calls} == {"network", "supervisor/api", "config_entries/flow/progress", "network/url"}
    assert all(command != "supervisor/api" or fields["method"] == "get" for command, fields in calls)
    assert "private-fixture-token" not in json.dumps(result)


@pytest.mark.parametrize("missing", ["native_discovery", "default_route", "ipv6", "nat_gateway_suppression",
                                     "automatic_selection", "callback_address", "product_observation"])
async def test_related_checks_do_not_substitute_for_each_missing_native_proof(observed, missing):
    values, status, _ = observed
    if missing == "native_discovery":
        values["config_entries/flow/progress"] = []
    elif missing == "default_route":
        values["network"]["adapters"][1]["default"] = False
    elif missing == "ipv6":
        values["supervisor/api"]["interfaces"][1]["ipv6"]["address"] = ["fe80::100/64"]
    elif missing == "nat_gateway_suppression":
        values["supervisor/api"]["interfaces"][0]["ipv4"]["gateway"] = "192.168.177.1"
    elif missing == "automatic_selection":
        values["network"]["configured_adapters"] = ["enp2s0"]
    elif missing == "callback_address":
        values["network/url"]["internal"] = "http://192.168.177.2:8123"
    else:
        status["home"]["state"] = "callback_address_required"
    with pytest.raises(AssertionError):
        await probe.observe(None, True)


async def test_probe_requires_nat_restoration_and_removal(observed):
    values, status, _ = observed
    status.update(state="disabled", mode="nat")
    values["supervisor/api"]["interfaces"] = values["supervisor/api"]["interfaces"][:1]
    values["supervisor/api"]["interfaces"][0]["ipv4"]["gateway"] = "192.168.177.1"
    values["network"]["adapters"][0].update(default=True, enabled=True)
    result = await probe.observe(None, False)
    assert result == {"management_reachable": True, "nat_gateway_restored": True, "lan_nic_removed": True}


def test_peer_sends_actual_service_records_and_never_native_flow_requests(monkeypatch, tmp_path):
    packet = ha_lan_peer.advertisement()
    assert struct.unpack_from("!6H", packet) == (0, 0x8400, 0, 5, 0, 0)
    assert b"Agentic LAN Fixture" in packet and b"id=02:11:22:33:44:55" in packet
    assert b"sf=1" in packet and b"md=AgenticLANFixture" in packet
    monkeypatch.delenv("AGENTIC_DISPOSABLE_LAN_LAB", raising=False)
    monkeypatch.setattr(ha_lan_peer.subprocess, "run", lambda *args, **kwargs: pytest.fail("no network change outside fixture"))
    with pytest.raises(RuntimeError, match="namespace_required"):
        ha_lan_peer.launch(["qemu-system-x86_64"])


def test_actual_lan_lab_namespace_starts_dhcp_ra_and_mdns(tmp_path):
    if os.environ.get("AGENTIC_LAN_LAB_SMOKE") != "1":
        pytest.skip("Explicit test-only Docker namespace smoke; not HAOS acceptance")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    (tmp_path / "linux.qcow2").touch()  # Namespace guard only; no VM is launched.
    name = "agentic-lan-lab-smoke-" + uuid.uuid4().hex[:10]
    root = Path(__file__).resolve().parent
    script = '''
import socket,subprocess,sys,time
sys.path.insert(0,'/scripts')
import ha_lan_peer as peer
peer.os.execvp=lambda *args: None
peer.launch(['qemu-system-x86_64'])
time.sleep(1)
listeners=subprocess.check_output(['ss','-lnup'],text=True)
assert ':53 ' in listeners and ':67 ' in listeners and ':5353 ' in listeners
addresses=subprocess.check_output(['ip','-6','address','show','dev','ahltap'],text=True)
assert 'fd42:177:42::1/64' in addresses
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.setsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,b'ahltap\\0')
s.bind(('',5353))
s.setsockopt(socket.IPPROTO_IP,socket.IP_ADD_MEMBERSHIP,socket.inet_aton('224.0.0.251')+socket.inet_aton('198.18.42.1'))
s.settimeout(5)
data,_=s.recvfrom(8192)
assert b'Agentic LAN Fixture' in data
print('isolated_namespace_dhcp_ra_mdns_started')
'''
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--cap-add", "NET_ADMIN",
            "--device", "/dev/net/tun", "--sysctl", "net.ipv4.ip_forward=1", "--sysctl", "net.ipv6.conf.all.disable_ipv6=0",
            "-e", "AGENTIC_DISPOSABLE_LAN_LAB=1", "-v", f"{tmp_path}:/lab", "-v", f"{root}:/scripts:ro",
            "-i", "agentic-home-lan-lab:0.10.0", "python3", "-"], input=script, text=True,
            capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr[-2000:]
        assert result.stdout.strip() == "isolated_namespace_dhcp_ra_mdns_started"
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=20)
        remaining = subprocess.run(["docker", "ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"],
                                   capture_output=True, text=True, check=True, timeout=10)
        assert not remaining.stdout.strip()


@pytest.mark.parametrize("status", [200, 401, 404, 503])
def test_post_owner_readiness_requires_native_auth_endpoint_not_onboarding(monkeypatch, status):
    calls = []
    def open_url(url, timeout):
        calls.append((url, timeout))
        if status != 200:
            raise urllib.error.HTTPError(url, status, "fixture", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    script = native_api_probe_script("http://198.18.42.100:8123", 10)
    if status == 401:
        exec(script, {})
    else:
        with pytest.raises(AssertionError):
            exec(script, {})
    assert calls == [("http://198.18.42.100:8123/api/", 10)]
