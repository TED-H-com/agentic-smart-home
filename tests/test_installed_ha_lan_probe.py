"""The acceptance probe requires native evidence; fixtures do not claim HAOS proof."""
import asyncio
import copy
import inspect
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
    management = {"interface": "enp1s0", "mac": "52:54:00:aa:17:02", "connected": True, "enabled": True,
                  "ipv4": {"address": ["192.168.177.2/24"], "gateway": None, "ready": True}, "ipv6": None}
    lan = {"interface": "enp2s0", "mac": "02:00:00:00:00:42", "connected": True, "enabled": True,
           "ipv4": {"address": ["198.18.42.100/24"], "gateway": "198.18.42.1", "ready": True},
           "ipv6": {"address": ["fd42:177:42::100/64"], "ready": True}}
    values = {"network": {"configured_adapters": [], "adapters": [{"name": "enp1s0", "default": False, "enabled": False, "ipv4": [{"address": "192.168.177.2"}], "ipv6": []},
              {"name": "enp2s0", "default": True, "enabled": True, "ipv4": [{"address": "198.18.42.100"}],
               "ipv6": [{"address": "fd42:177:42::100"}]}]},
              "supervisor/api": {"interfaces": [management, lan]},
              "config_entries/flow/progress": [{"handler": "homekit_controller", "context": {"source": "zeroconf",
                  "title_placeholders": {"name": "Agentic LAN Fixture"}}}],
              "network/url": {"internal": "http://198.18.42.100:8123"}}
    status = {"state": "configured", "mode": "wired", "guest_mac": lan["mac"], "home": {"state": "ready"}}
    calls = []
    async def controller(client, proof=None):
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


def assert_peer_ptr_response(response, transaction, question, expected):
    """Validate the one exact local PTR answer emitted by the fixture dnsmasq."""
    assert 12 <= len(response) <= 4096 and response[:2] == transaction
    _, flags, questions, answers, _, _ = struct.unpack_from("!6H", response)
    assert flags & 0x8000 and not flags & 0x7A0F
    assert questions == answers == 1 and response[12:12 + len(question)] == question
    offset = 12 + len(question)
    owner = b"\xc0\x0c" if response[offset:offset + 2] == b"\xc0\x0c" else question[:-4]
    assert response[offset:offset + len(owner)] == owner
    offset += len(owner)
    assert offset + 10 <= len(response)
    kind, record_class, _, size = struct.unpack_from("!HHIH", response, offset)
    assert (kind, record_class) == (12, 1) and size == len(expected)
    assert response[offset + 10:offset + 10 + size] == expected


@pytest.mark.parametrize("bad", [None, "transaction", "question", "answer", "rcode", "truncated"])
def test_peer_ptr_proof_rejects_unrelated_or_incomplete_dns(bad):
    transaction = b"\x12\x34"
    question = ha_lan_peer.name("1.42.18.198.in-addr.arpa.") + struct.pack("!HH", 12, 1)
    expected = ha_lan_peer.name(ha_lan_peer.DNS_HOST)
    response = (transaction + struct.pack("!5H", 0x8580, 1, 1, 0, 0) + question
                + b"\xc0\x0c" + struct.pack("!HHIH", 12, 1, 0, len(expected)) + expected)
    if bad == "transaction":
        response = b"\x00\x00" + response[2:]
    elif bad == "question":
        response = response[:13] + b"2" + response[14:]
    elif bad == "answer":
        response = response[:-2] + b"x\0"
    elif bad == "rcode":
        response = response[:3] + b"\x83" + response[4:]
    elif bad == "truncated":
        response = response[:-1]
    if bad:
        with pytest.raises(AssertionError):
            assert_peer_ptr_response(response, transaction, question, expected)
    else:
        assert_peer_ptr_response(response, transaction, question, expected)


def test_actual_lan_lab_namespace_starts_dhcp_ra_and_mdns(tmp_path):
    if os.environ.get("AGENTIC_LAN_LAB_SMOKE") != "1":
        pytest.skip("Explicit test-only Docker namespace smoke; not HAOS acceptance")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    (tmp_path / "linux.qcow2").touch()  # Namespace guard only; no VM is launched.
    name = "agentic-lan-lab-smoke-" + uuid.uuid4().hex[:10]
    root = Path(__file__).resolve().parent
    script = inspect.getsource(assert_peer_ptr_response) + '''
import ipaddress,os,socket,struct,subprocess,sys,time
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
s.close()
# A listening DNS socket is insufficient: prove forwarding via the peer using
# the container's actual resolv.conf, including on Docker's default bridge.
transaction=os.urandom(2)
question=peer.name('github.com.')+struct.pack('!HH',1,1)
query=transaction+struct.pack('!5H',0x0100,1,0,0,0)+question
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as dns:
    dns.settimeout(5)
    dns.connect((peer.IPV4,53))
    dns.send(query)
    response=dns.recv(4096)
assert len(response)>=12 and response[:2]==transaction
_,flags,questions,answers,_,_=struct.unpack_from('!6H',response)
assert flags & 0x8000 and not flags & 0x7A0F  # Response, query opcode, untruncated, NOERROR.
assert questions==1 and 0<answers<=32
assert response[12:12+len(question)]==question
offset=12+len(question)
public_address=False
for _ in range(answers):
    while True:
        assert offset<len(response)
        length=response[offset]
        if length & 0xC0 == 0xC0:
            assert offset+1<len(response)
            pointer=((length & 0x3F)<<8)|response[offset+1]
            assert 12<=pointer<len(response)
            offset+=2
            break
        assert length<=63 and offset+1+length<=len(response)
        offset+=1+length
        if length==0:
            break
    assert offset+10<=len(response)
    kind,record_class,_,size=struct.unpack_from('!HHIH',response,offset)
    offset+=10
    assert offset+size<=len(response)
    if kind==1 and record_class==1 and size==4:
        public_address=public_address or ipaddress.IPv4Address(response[offset:offset+4]).is_global
    offset+=size
assert public_address
# Fault injection stays in this disposable smoke namespace. Block upstream
# DNS after proving normal forwarding; queries to the peer itself stay allowed.
for tool,address in (('iptables',peer.IPV4),('ip6tables',peer.IPV6)):
    for protocol in ('udp','tcp'):
        subprocess.run([tool,'-I','OUTPUT','1','-p',protocol,'--dport','53',
                        '!','-d',address,'-j','REJECT'],check=True,timeout=5)
for family,address in ((socket.AF_INET,peer.IPV4),(socket.AF_INET6,peer.IPV6)):
    transaction=os.urandom(2)
    question=peer.name(ipaddress.ip_address(address).reverse_pointer)+struct.pack('!HH',12,1)
    query=transaction+struct.pack('!5H',0x0100,1,0,0,0)+question
    with socket.socket(family,socket.SOCK_DGRAM) as dns:
        dns.settimeout(5)
        dns.connect((address,53))
        dns.send(query)
        response=dns.recv(4096)
    assert_peer_ptr_response(response,transaction,question,peer.name(peer.DNS_HOST))
print('isolated_namespace_dhcp_ra_mdns_dns_forwarding_local_ptr_started')
'''
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--cap-add", "NET_ADMIN",
            "--device", "/dev/net/tun", "--sysctl", "net.ipv4.ip_forward=1", "--sysctl", "net.ipv6.conf.all.disable_ipv6=0",
            "-e", "AGENTIC_DISPOSABLE_LAN_LAB=1", "-v", f"{tmp_path}:/lab", "-v", f"{root}:/scripts:ro",
            "-i", "agentic-home-lan-lab:0.11.1", "python3", "-"], input=script, text=True,
            capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr[-2000:]
        assert result.stdout.strip() == "isolated_namespace_dhcp_ra_mdns_dns_forwarding_local_ptr_started"
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


@pytest.mark.parametrize(('section', 'value', 'code'), [
    ('interfaces', None, 'supervisor_interfaces_failed'),
    ('interfaces', {}, 'supervisor_interfaces_failed'),
    ('interfaces', [None], 'supervisor_interfaces_failed'),
    ('interfaces', [], 'management_interface_failed'),
    ('adapters', None, 'native_adapters_failed'),
    ('adapters', [None], 'native_adapters_failed'),
    ('adapters', [], 'native_adapter_failed'),
    ('flows', None, 'native_discovery_rows_failed'),
    ('flows', [None], 'native_discovery_rows_failed'),
    ('flows', [{'context': None}], 'native_discovery_rows_failed'),
    ('network', None, 'native_network_shape_failed'),
    ('supervisor', None, 'supervisor_shape_failed'),
    ('urls', None, 'native_urls_shape_failed'),
    ('internal', None, 'native_callback_address_failed'),
])
async def test_missing_or_malformed_native_shapes_have_fixed_codes(observed, section, value, code):
    values, _, _ = observed
    if section == 'interfaces':
        values['supervisor/api']['interfaces'] = value
    elif section == 'adapters':
        values['network']['adapters'] = value
    elif section == 'flows':
        values['config_entries/flow/progress'] = value
    elif section == 'internal':
        values['network/url']['internal'] = value
    else:
        values[{'network': 'network', 'supervisor': 'supervisor/api', 'urls': 'network/url'}[section]] = value
    proof = probe.Proof()
    with pytest.raises(probe.ProofFailure) as error:
        await probe.observe(None, True, proof)
    assert error.value.code == code
    assert proof.checks[code.removesuffix('_failed')] is False
    assert probe.validate_failure(proof.failure('wired', code)) is not None


@pytest.mark.parametrize(('target', 'change', 'code'), [
    ('management', 'remove', 'management_interface_failed'),
    ('guest', 'remove', 'guest_interface_failed'),
    ('guest', 'duplicate', 'guest_interface_failed'),
    ('management', 'null_ipv4', 'management_ipv4_addresses_failed'),
    ('guest', 'null_ipv4', 'guest_ipv4_addresses_failed'),
    ('guest', 'null_ipv6', 'guest_ipv6_addresses_failed'),
    ('guest', 'bad_ip', 'guest_ipv6_addresses_failed'),
    ('guest', 'wrong_family', 'guest_ipv6_addresses_failed'),
    ('management', 'missing_gateway', 'management_gateway_field_failed'),
])
async def test_nullable_or_absent_interfaces_never_escape_as_stopiteration(observed, target, change, code):
    values, _, _ = observed
    interfaces = values['supervisor/api']['interfaces']
    row = interfaces[0 if target == 'management' else 1]
    if change == 'remove':
        interfaces.remove(row)
    elif change == 'duplicate':
        interfaces.append(copy.deepcopy(row))
    elif change.startswith('null_'):
        row[change.removeprefix('null_')] = None
    elif change == 'bad_ip':
        row['ipv6']['address'] = ['private-secret-invalid-address']
    elif change == 'wrong_family':
        row['ipv6']['address'] = ['198.18.42.100/24']
    else:
        row['ipv4'].pop('gateway')
    with pytest.raises(probe.ProofFailure) as error:
        await probe.observe(None, True)
    assert error.value.code == code
    assert 'private-secret' not in str(error.value)


async def test_fixture_matches_real_product_projection_without_weakening_proof(observed):
    from agentic_controller.ha_lan import project_network
    values, status, _ = observed
    native = project_network(status['guest_mac'], values['supervisor/api'], values['network'], values['network/url'])
    assert native['state'] == 'ready'
    assert native['default_adapter'] is True and native['discovery_enabled'] is True
    assert (await probe.observe(None, True))['product_observation_ready'] is True


async def test_failed_attempt_does_not_keep_prior_success_checks(observed):
    values, _, _ = observed
    proof = probe.Proof()
    await probe.observe(None, True, proof)
    assert proof.checks['native_mdns_flow'] is True
    values['supervisor/api']['interfaces'][1]['ipv6'] = None
    with pytest.raises(probe.ProofFailure, match='guest_ipv6_addresses_failed'):
        await probe.observe(None, True, proof)
    assert proof.checks['guest_ipv6_addresses'] is False
    assert 'native_mdns_flow' not in proof.checks
    assert 'native_callback_address' not in proof.checks


async def test_transient_missing_ipv6_retries_with_same_budget_then_requires_proof(observed, monkeypatch):
    values, _, _ = observed
    saved = values['supervisor/api']['interfaces'][1]['ipv6']
    values['supervisor/api']['interfaces'][1]['ipv6'] = None
    pauses = []
    async def pause(seconds):
        pauses.append(seconds)
        values['supervisor/api']['interfaces'][1]['ipv6'] = saved
    monkeypatch.setattr(probe.asyncio, 'sleep', pause)
    result = await probe.main('wired', probe.Proof())
    assert pauses == [5]
    assert result['ipv6_ra'] is True and result['native_mdns_flow_observed'] is True


async def test_readonly_retry_budget_is_unchanged_and_onboarding_effect_is_not_retried(monkeypatch):
    calls = []
    pauses = []
    async def observe(client, wired, proof):
        calls.append(wired)
        proof.require('guest_interface', False)
    async def pause(seconds):
        pauses.append(seconds)
    monkeypatch.setattr(probe, 'observe', observe)
    monkeypatch.setattr(probe.asyncio, 'sleep', pause)
    with pytest.raises(probe.ProofFailure, match='guest_interface_failed'):
        await probe.main('wired', probe.Proof())
    assert len(calls) == 120 and pauses == [5] * 119
    calls.clear()
    pauses.clear()
    async def onboard(client, proof):
        calls.append('effect')
        proof.require('owner_connect_api', False)
    monkeypatch.setattr(probe, 'onboard', onboard)
    with pytest.raises(probe.ProofFailure, match='owner_connect_api_failed'):
        await probe.main('onboard', probe.Proof())
    assert calls == ['effect'] and pauses == []


@pytest.mark.parametrize(('stage', 'code'), [
    ('product_status', 'product_status_api_failed'),
    ('network', 'native_network_api_failed'),
    ('supervisor/api', 'native_supervisor_api_failed'),
    ('config_entries/flow/progress', 'native_discovery_api_failed'),
    ('network/url', 'native_urls_api_failed'),
])
async def test_api_exceptions_are_stage_bound_and_redacted(observed, monkeypatch, stage, code):
    original = probe.ws
    async def fail_ws(client, token, command, **fields):
        if command == stage:
            raise RuntimeError('Bearer private-token http://192.0.2.19/private?secret=password')
        return await original(client, token, command, **fields)
    async def fail_http(*args, **kwargs):
        raise probe.aiohttp.ClientError('raw private HTTP response token=password')
    if stage == 'product_status':
        monkeypatch.setattr(probe, 'request', fail_http)
    else:
        monkeypatch.setattr(probe, 'ws', fail_ws)
    proof = probe.Proof()
    with pytest.raises(probe.ProofFailure) as error:
        await probe.observe(None, True, proof)
    assert error.value.code == code
    output = json.dumps(proof.failure('wired', error.value.code))
    assert not any(secret in output for secret in ('private-token', '192.0.2.19', 'password', 'Bearer', 'http://'))


@pytest.mark.parametrize('exception', [RuntimeError('token-secret'), TypeError('body-secret'), StopIteration('url-secret')])
def test_entrypoint_emits_one_safe_failure_and_nonzero_without_traceback(monkeypatch, capsys, exception):
    monkeypatch.setenv('AGENTIC_LAN_PROBE_PHASE', 'wired')
    async def bounded(phase, proof):
        proof.note('native_network_api', False)
        raise exception
    monkeypatch.setattr(probe, 'bounded', bounded)
    assert probe.execute() == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value == {'kind': 'installed_ha_lan_probe', 'status': 'failed', 'phase': 'wired',
                     'error_code': 'probe_internal_error', 'checks': {'native_network_api': False}}
    assert output.err == '' and len(output.out.splitlines()) == 1
    assert 'secret' not in output.out and 'Traceback' not in output.out


def test_overall_timeout_preserves_pending_stage_in_safe_terminal_json(monkeypatch, capsys):
    monkeypatch.setenv('AGENTIC_LAN_PROBE_PHASE', 'wired')
    original_timeout = asyncio.timeout
    budgets = []
    def timeout(seconds):
        budgets.append(seconds)
        return original_timeout(0.001)
    async def blocked(phase, proof):
        proof.note('native_discovery_api', False)
        await asyncio.Future()
    monkeypatch.setattr(probe.asyncio, 'timeout', timeout)
    monkeypatch.setattr(probe, 'main', blocked)
    assert probe.execute() == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value['error_code'] == 'probe_deadline_exceeded'
    assert value['checks'] == {'native_discovery_api': False}
    assert budgets == [720] and output.err == ''
    assert probe.validate_failure(value) == value


@pytest.mark.parametrize('change', ['extra_field', 'unknown_code', 'unknown_check', 'integer_check', 'phase', 'status', 'missing', 'checks_list'])
def test_failure_validator_rejects_untrusted_or_ambiguous_schema(change):
    value = {'kind': 'installed_ha_lan_probe', 'status': 'failed', 'phase': 'wired',
             'error_code': 'native_mdns_flow_failed', 'checks': {'native_mdns_flow': False}}
    if change == 'extra_field':
        value['url'] = 'http://private.invalid'
    elif change == 'unknown_code':
        value['error_code'] = 'secret'
    elif change == 'unknown_check':
        value['checks']['private-token'] = False
    elif change == 'integer_check':
        value['checks']['native_mdns_flow'] = 0
    elif change == 'phase':
        value['phase'] = ['wired']
    elif change == 'status':
        value['status'] = 'passed'
    elif change == 'checks_list':
        value['checks'] = []
    else:
        del value['kind']
    assert probe.validate_failure(value) is None


def test_failure_projection_is_detached_bounded_and_invalid_phase_never_echoes(monkeypatch, capsys):
    proof = probe.Proof()
    for name in probe.CHECK_NAMES:
        proof.note(name, True)
    value = proof.failure('wired', 'probe_deadline_exceeded')
    projected = probe.validate_failure(value)
    assert projected == value and projected is not value and projected['checks'] is not value['checks']
    assert len(json.dumps(value).encode()) < 8192
    monkeypatch.setenv('AGENTIC_LAN_PROBE_PHASE', 'private-invalid-value')
    assert probe.execute() == 1
    output = capsys.readouterr()
    failure = json.loads(output.out)
    assert failure['phase'] is None and failure['error_code'] == 'probe_phase_invalid'
    assert probe.validate_failure(failure) is None
    assert 'private-invalid-value' not in output.out and output.err == ''


@pytest.mark.parametrize(('field', 'code'), [('guest_connected', 'guest_connected_failed'),
    ('dhcp_ipv4', 'dhcp_ipv4_failed'), ('adapter_enabled', 'native_adapter_enabled_failed'),
    ('lan_gateway', 'lan_default_gateway_failed')])
async def test_each_additional_network_requirement_remains_mandatory(observed, field, code):
    values, _, _ = observed
    guest = values['supervisor/api']['interfaces'][1]
    if field == 'guest_connected':
        guest['connected'] = False
    elif field == 'dhcp_ipv4':
        guest['ipv4']['address'] = ['192.0.2.100/24']
    elif field == 'adapter_enabled':
        values['network']['adapters'][1]['enabled'] = False
    else:
        guest['ipv4']['gateway'] = None
    with pytest.raises(probe.ProofFailure) as error:
        await probe.observe(None, True)
    assert error.value.code == code


def test_terminal_proof_failure_preserves_fixed_code_and_product_state_hints(observed, monkeypatch, capsys):
    _, status, _ = observed
    status['home']['state'] = 'callback_address_required'
    monkeypatch.setenv('AGENTIC_LAN_PROBE_PHASE', 'wired')
    async def once(phase, proof):
        return await probe.observe(None, True, proof)
    monkeypatch.setattr(probe, 'bounded', once)
    assert probe.execute() == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value['error_code'] == 'product_observation_ready_failed'
    assert value['checks']['native_mdns_flow'] is True
    assert value['checks']['native_callback_address'] is True
    assert value['checks']['product_observation_ready'] is False
    assert value['checks']['product_state_callback_address_required'] is True
    assert value['checks']['product_state_ready'] is False
    assert probe.validate_failure(value) == value
    assert not any(secret in output.out for secret in ('private-fixture-token', '198.18.42.', 'http://', '52:54:'))
    assert output.err == ''


def test_success_stdout_still_supplies_private_fixture_callback(observed, monkeypatch, capsys):
    monkeypatch.setenv('AGENTIC_LAN_PROBE_PHASE', 'wired')
    async def once(phase, proof):
        return await probe.observe(None, True, proof)
    monkeypatch.setattr(probe, 'bounded', once)
    assert probe.execute() == 0
    output = capsys.readouterr()
    assert json.loads(output.out)['callback_url'] == 'http://198.18.42.100:8123'
    assert 'private-fixture-token' not in output.out and output.err == ''


@pytest.mark.parametrize(('stage', 'code'), [('login', 'product_login_api_failed'), ('session', 'product_session_api_failed'),
                                          ('csrf', 'product_session_csrf_failed')])
async def test_controller_auth_failures_are_fixed_before_any_native_api(monkeypatch, stage, code):
    monkeypatch.setattr(Path, 'read_text', lambda *args, **kwargs: 'private-access-key')
    calls = []
    async def request(client, method, url, **kwargs):
        calls.append(method)
        if stage == 'login' or (stage == 'session' and method == 'GET'):
            raise RuntimeError('private-access-key http://secret.invalid/rawbody')
        return {} if method == 'POST' else {'csrf': None}
    monkeypatch.setattr(probe, 'request', request)
    proof = probe.Proof()
    with pytest.raises(probe.ProofFailure) as error:
        await probe.controller(None, proof)
    assert error.value.code == code
    assert calls == (['POST'] if stage == 'login' else ['POST', 'GET'])
    assert 'private-access-key' not in json.dumps(proof.failure('wired', error.value.code))


@pytest.mark.parametrize('url', ['http://198.18.42.100', 'http://198.18.42.100/',
    'http://198.18.42.100:80', 'http://198.18.42.100:80/', 'http://198.18.42.100:8123',
    'http://198.18.42.100:1', 'http://198.18.42.100:65535'])
async def test_native_callback_accepts_default_or_explicit_http_port_without_skipping_other_proofs(observed, url):
    values, _, calls = observed
    values['network/url']['internal'] = url
    assert probe.callback_address_matches(url, ['198.18.42.100']) is True
    result = await probe.observe(None, True)
    assert result['callback_url'] == url
    assert result['native_mdns_flow_observed'] is True and result['product_observation_ready'] is True
    assert 'config_entries/flow/progress' in {command for command, _ in calls}
    # This helper establishes only the target. The installer still contacts it
    # from the synthetic peer and requires the native authenticated API's 401.
    assert 'native_callback_reachable_from_lan_peer' not in result


@pytest.mark.parametrize('url', [
    'http://owner@198.18.42.100', 'http://owner:secret@198.18.42.100',
    'http://198.18.42.100@evil.invalid', 'http://198.18.42.100.evil.invalid',
    'http://198.18.42.1000', 'http://198.18.42.100.', 'http://198.18.42.101',
    'http://homeassistant.local', 'http://198.18.42.100:0', 'http://198.18.42.100:65536',
    'http://198.18.42.100:-1', 'http://198.18.42.100:+80', 'http://198.18.42.100:bad',
    'http://198.18.42.100:', 'http://198.18.42.100:８０',
    'http://198.18.42.100?secret=x', 'http://198.18.42.100?',
    'http://198.18.42.100#secret', 'http://198.18.42.100#',
    'http://198.18.42.100/api', 'http://198.18.42.100//', 'http://198.18.42.100/%2f',
    'http://198.18.42.100\\@evil.invalid', ' http://198.18.42.100',
    'http://198.18.42.100\n', 'http://198.18.42.100\x7f',
    'http://198.18.42.\t100', 'https://198.18.42.100', '//198.18.42.100',
    'http://[::ffff:c612:2a64]', 'http://198.18.042.100', None, {}, 123,
])
async def test_native_callback_rejects_unsafe_or_unobserved_targets(observed, url):
    values, _, _ = observed
    values['network/url']['internal'] = url
    assert probe.callback_address_matches(url, ['198.18.42.100']) is False
    with pytest.raises(probe.ProofFailure, match='native_callback_address_failed'):
        await probe.observe(None, True)


def test_callback_requires_observed_fixture_subnet_even_if_other_address_is_reported():
    assert probe.callback_address_matches('http://192.168.177.2', ['192.168.177.2']) is False
    assert probe.callback_address_matches('http://198.18.42.108', ['198.18.42.108']) is True
    assert probe.callback_address_matches('http://198.18.42.108', []) is False
    assert probe.callback_address_matches('http://198.18.42.108', None) is False
    assert probe.callback_address_matches('http://198.18.42.108', ['198.18.42.108', None]) is False


async def test_default_port_acceptance_does_not_weaken_multicast_requirement(observed):
    values, _, _ = observed
    values['network/url']['internal'] = 'http://198.18.42.100'
    values['config_entries/flow/progress'] = []
    with pytest.raises(probe.ProofFailure, match='native_mdns_flow_failed'):
        await probe.observe(None, True)
