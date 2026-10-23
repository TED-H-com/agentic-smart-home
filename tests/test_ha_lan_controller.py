"""Read-only LAN observations, with no real network, CLI or household mutations."""
import asyncio
import copy
import json
import threading

import pytest
from agentic_controller import ha_lan
from agentic_controller.ha import HAError, read_only_ws
from pydantic import ValidationError

GUEST = "02:12:34:56:78:9a"
HOST = {"mode": "wired", "state": "configured", "guest_mac": GUEST}


class NetworkHA:
    token, base, websocket, supervisor, external = "fixture-token", "http://192.168.177.2:80/api", "fixture-ws", "fixture-supervisor", True

    def __init__(self):
        self.calls = []
        self.owner_reads = 0
        self.owner_value = {"id": "owner", "is_owner": True, "is_active": True}
        self.on_read = None
        self.on_owner = None
        self.supervisor_info = {"interfaces": [{"interface": "end1", "mac": GUEST.upper(), "enabled": True,
            "connected": True, "ipv4": {"address": ["192.168.10.20/24"], "ready": True},
            "ipv6": {"address": ["fd12:3456::20/64", "fe80::20/64"], "ready": True}}]}
        self.network = {"configured_adapters": ["end1"], "adapters": [{"name": "end1", "index": 3,
            "enabled": True, "default": False, "auto": False,
            "ipv4": [{"address": "192.168.10.20", "network_prefix": 24}],
            "ipv6": [{"address": "fd12:3456::20", "network_prefix": 64, "flowinfo": 0, "scope_id": 0},
                     {"address": "fe80::20", "network_prefix": 64, "flowinfo": 0, "scope_id": 3}]}]}
        self.urls = {"internal": "http://192.168.10.20:8123/", "external": "https://private-cloud.invalid/", "cloud": None}

    async def owner(self, owner):
        self.calls.append(("owner", owner))
        self.owner_reads += 1
        if self.on_owner:
            self.on_owner(self.owner_reads)
        return copy.deepcopy(self.owner_value)

    async def request(self, method, path, *, supervisor=False, timeout=None):
        self.calls.append((method, path, supervisor))
        assert method == "GET" and path == "/network/info" and supervisor is True
        assert timeout <= 8
        if self.on_read:
            self.on_read(path)
        return copy.deepcopy(self.supervisor_info)

    async def ws(self, command, **kwargs):
        self.calls.append(("WS", command))
        assert command in {"network", "network/url"} and set(kwargs) == {"_receive_timeout"}
        if self.on_read:
            self.on_read(command)
        return copy.deepcopy(self.network if command == "network" else self.urls)


@pytest.fixture
def network(store):
    store.set_setting("owner", "owner")
    store.set_setting("hermes_web_epoch", "fixture-epoch")
    return NetworkHA()


async def test_exact_mac_cross_checks_core_and_supervisor_without_requiring_default_adapter(network, store):
    before = store.setting("policy")
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "ready" and result["interface"] == "end1"
    assert result["ipv4"] == ["192.168.10.20"] and result["ipv6"] == ["fd12:3456::20", "fe80::20"]
    assert result["connected"] is True and result["discovery_enabled"] is True and result["default_adapter"] is False
    assert result["internal_url"] == "http://192.168.10.20:8123"
    assert result["adapter_version"] == 1 and result["observed_at"]
    assert "no_device_or_matter_control_verified" in result["limits"]
    assert network.calls[0] == network.calls[-1] == ("owner", "owner") and len(network.calls) == 5
    assert store.setting("policy") == before
    assert all(secret not in json.dumps(result) for secret in ("fixture-token", "private-cloud", GUEST))


async def test_ipv6_ula_callback_and_unknown_future_fields_are_supported(network, store):
    network.urls["internal"] = "https://[fd12:3456::20]:8123/"
    network.supervisor_info["future"] = {"private": "discarded"}
    network.supervisor_info["interfaces"][0]["future"] = "discarded"
    network.network["future"] = "discarded"
    network.network["adapters"][0]["future"] = "discarded"
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "ready" and result["internal_url"] == "https://[fd12:3456::20]:8123"
    assert "discarded" not in json.dumps(result)


@pytest.mark.parametrize("host,state", [({"mode": "nat", "state": "disabled"}, "not_configured"),
    ({}, "unavailable"), ({"mode": "future", "state": "configured"}, "unavailable"),
    ({**HOST, "state": "unavailable"}, "unavailable"), ({**HOST, "guest_mac": "ff:ff:ff:ff:ff:ff"}, "unavailable"),
    ({**HOST, "guest_mac": "00:00:00:00:00:00"}, "unavailable")])
async def test_unconfigured_and_invalid_host_observations_do_not_query_home(network, store, host, state):
    result = await ha_lan.observe_home_lan(network, store, host)
    assert result["state"] == state and network.calls == []
    assert result["ipv4"] == [] and result["internal_url"] is None


async def test_missing_owner_requires_native_home_setup_without_querying_home(network, store):
    store.set_setting("owner", None)
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "needs_home_setup" and network.calls == []


@pytest.mark.parametrize("fault", ["revoked_before", "revoked_after", "inactive", "wrong_owner", "epoch", "owner",
                                   "token", "base", "websocket", "supervisor", "external"])
async def test_authority_changes_never_return_observed_addresses(network, store, fault):
    if fault == "revoked_before":
        network.owner_value = None
    elif fault == "inactive":
        network.owner_value["is_active"] = False
    elif fault == "wrong_owner":
        network.owner_value["id"] = "another-owner"
    elif fault == "revoked_after":
        def revoke(count):
            if count == 2:
                network.owner_value["is_owner"] = False
        network.on_owner = revoke
    else:
        def changed(path):
            if path != "network":
                return
            if fault in {"owner", "epoch"}:
                store.set_setting("owner" if fault == "owner" else "hermes_web_epoch", "changed")
            else:
                setattr(network, fault, False if fault == "external" else "changed")
        network.on_read = changed
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "unavailable" and result["interface"] is None
    assert result["ipv4"] == result["ipv6"] == [] and result["internal_url"] is None
    if fault in {"revoked_before", "inactive", "wrong_owner"}:
        assert network.calls == [("owner", "owner")]


@pytest.mark.parametrize("fault", ["duplicate_mac", "duplicate_adapter", "wrong_adapter_name", "missing_mac",
    "interfaces_not_list", "too_many_interfaces", "missing_connected", "connected_integer", "missing_enabled",
    "missing_ipv4", "missing_ready", "missing_core_ipv6", "missing_core_enabled", "missing_core_default",
    "addresses_not_list", "malformed_core_address", "core_address_mismatch", "missing_internal", "invalid_internal"])
async def test_schema_drift_and_ambiguous_identity_fail_closed(network, store, fault):
    interface = network.supervisor_info["interfaces"][0]
    adapter = network.network["adapters"][0]
    if fault == "duplicate_mac":
        network.supervisor_info["interfaces"].append({**interface, "interface": "different"})
    elif fault == "duplicate_adapter":
        network.network["adapters"].append(copy.deepcopy(adapter))
    elif fault == "wrong_adapter_name":
        adapter["name"] = "management0"
    elif fault == "missing_mac":
        del interface["mac"]
    elif fault == "interfaces_not_list":
        network.supervisor_info["interfaces"] = {}
    elif fault == "too_many_interfaces":
        network.supervisor_info["interfaces"] = [{}] * 65
    elif fault == "missing_connected":
        del interface["connected"]
    elif fault == "connected_integer":
        interface["connected"] = 1
    elif fault == "missing_enabled":
        del interface["enabled"]
    elif fault == "missing_ipv4":
        del interface["ipv4"]
    elif fault == "missing_ready":
        del interface["ipv4"]["ready"]
    elif fault == "missing_core_ipv6":
        del adapter["ipv6"]
    elif fault == "missing_core_enabled":
        del adapter["enabled"]
    elif fault == "missing_core_default":
        del adapter["default"]
    elif fault == "addresses_not_list":
        interface["ipv4"]["address"] = "192.168.10.20/24"
    elif fault == "malformed_core_address":
        adapter["ipv4"][0]["address"] = "<script>"
    elif fault == "core_address_mismatch":
        adapter["ipv4"][0]["address"] = "192.168.10.21"
        adapter["ipv6"] = []
    elif fault == "missing_internal":
        del network.urls["internal"]
    else:
        network.urls["internal"] = {}
    result = await ha_lan.observe_home_lan(network, store, HOST)
    if fault == "missing_mac":
        assert result["state"] == "waiting_for_address"
    else:
        assert result["state"] == "unavailable"
    assert result["ipv4"] == result["ipv6"] == [] and result["internal_url"] is None


@pytest.mark.parametrize("value", ["127.0.0.1/8", "0.0.0.0/24", "224.0.0.1/24", "255.255.255.255/32",
    "192.168.10.255/24", "192.168.10.0/24", "192.168.177.2/24", "172.30.32.1/23", "169.254.1.1/16", "240.0.0.1/24"])
async def test_management_and_non_unicast_addresses_cannot_establish_lan_readiness(network, store, value):
    network.supervisor_info["interfaces"][0]["ipv4"]["address"] = [value]
    network.supervisor_info["interfaces"][0]["ipv6"] = None
    network.network["adapters"][0]["ipv4"] = [{"address": value.split("/")[0]}]
    network.network["adapters"][0]["ipv6"] = []
    network.urls["internal"] = "http://" + value.split("/")[0] + ":8123"
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] in {"waiting_for_address", "unavailable"} and result["internal_url"] is None


@pytest.mark.parametrize("value", ["::/64", "::1/128", "ff02::fb/64", "::ffff:192.168.10.20/128"])
async def test_unsafe_ipv6_observations_are_rejected(network, store, value):
    network.supervisor_info["interfaces"][0]["ipv6"]["address"] = [value]
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "unavailable" and result["internal_url"] is None


@pytest.mark.parametrize("fault", ["disconnected", "disabled", "no_address", "address_not_ready", "link_local_only"])
async def test_guest_connection_and_dhcp_are_observed_before_adapter_guidance(network, store, fault):
    interface = network.supervisor_info["interfaces"][0]
    if fault == "disconnected":
        interface["connected"] = False
    elif fault == "disabled":
        interface["enabled"] = False
    elif fault == "address_not_ready":
        interface["ipv4"]["ready"] = interface["ipv6"]["ready"] = False
    else:
        interface["ipv4"]["address"] = []
        interface["ipv6"]["address"] = ["fe80::20/64"] if fault == "link_local_only" else []
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "waiting_for_address" and result["internal_url"] is None
    if fault == "link_local_only":
        assert result["ipv6"] == ["fe80::20"]


async def test_disabled_core_adapter_requests_selection_without_writing_config(network, store):
    network.network["adapters"][0]["enabled"] = False
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "adapter_selection_required" and result["discovery_enabled"] is False
    assert result["internal_url"] is None and network.owner_reads == 2


@pytest.mark.parametrize("value", [None, "", "http://homeassistant.local:8123", "https://evil.invalid/",
    "http://192.168.177.2:8123", "http://172.30.32.1:8123", "http://127.0.0.1:8123",
    "http://192.168.10.20.evil.invalid/", "http://evil@192.168.10.20/", "http://192.168.10.20@evil.invalid/",
    "javascript:alert(1)", "//192.168.10.20/", "http://192.168.10.20/path", "http://192.168.10.20/?token=secret",
    "http://192.168.10.20/#fragment", "http://192.168.10.20:99999", "http://192.168.10.20:0",
    "http://192.168.10.20\\@evil.invalid/", "http://192.168.10.20\n/", "http://[fe80::20%25end1]:8123",
    "http://[fe80::20]:8123"])
async def test_only_observed_lan_ip_origins_are_eligible_callback_urls(network, store, value):
    network.urls["internal"] = value
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "callback_address_required" and result["internal_url"] is None
    assert result["discovery_enabled"] is True
    assert "evil" not in json.dumps(result) and "secret" not in json.dumps(result)


async def test_source_failure_still_rechecks_owner_and_never_discloses_raw_error(network, store):
    def fail(path):
        if path == "network":
            raise HAError("private-token-at-private-url")
    network.on_read = fail
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "unavailable" and network.owner_reads == 2
    assert "private" not in json.dumps(result)


async def test_total_timeout_cancels_reads_and_does_not_return_partial_observations(network, store, monkeypatch):
    monkeypatch.setattr(ha_lan, "OBSERVATION_SECONDS", 0.02)
    ended = []
    async def stalled(command, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            ended.append(command)
    network.ws = stalled
    result = await ha_lan.observe_home_lan(network, store, HOST)
    assert result["state"] == "unavailable" and result["ipv4"] == []
    assert set(ended) == {"network", "network/url"}


def test_network_observation_commands_are_read_only_but_configuration_remains_fenced():
    assert read_only_ws("network", {}) and read_only_ws("network/url", {})
    assert not read_only_ws("network/configure", {})
    assert not read_only_ws("network/unknown-future-write", {})


def host_contract():
    return {"contract_version": 1, "mode": "wired", "state": "configured", "selected_interface_id": "a" * 64,
        "interface": "enp1s0", "guest_mac": GUEST,
        "interfaces": [{"id": "a" * 64, "label": "enp1s0 · 192.168.10.1", "interface": "enp1s0",
                        "address": "192.168.10.1", "network": "192.168.10.0/24"}],
        "vm_state": "running", "can_configure": False, "limitations": ["wired_ethernet_only"]}


async def test_typed_route_contract_projects_unknown_host_fields_and_contains_home_result(network, store):
    raw = host_contract()
    raw["private_future"] = "not-for-browser"
    raw["interfaces"][0]["private_future"] = "not-for-browser"
    typed = ha_lan.HostLanStatus.model_validate(raw)
    assert "private_future" not in json.dumps(typed.model_dump())
    home = await ha_lan.observe_home_lan(network, store, typed.model_dump())
    result = ha_lan.HomeLanStatus.model_validate({**typed.model_dump(), "home": home}).model_dump(mode="json")
    assert result["home"]["state"] == "ready" and result["contract_version"] == 1


@pytest.mark.parametrize("field,value", [("contract_version", True), ("contract_version", 2), ("mode", "future"),
    ("state", "future"), ("selected_interface_id", None), ("selected_interface_id", "b" * 64),
    ("guest_mac", None), ("guest_mac", "01:00:00:00:00:00"), ("interface", "wrong-interface"),
    ("can_configure", True), ("can_configure", 0), ("vm_state", "future"), ("limitations", ["x" * 97])])
def test_host_contract_rejects_missing_or_contradictory_required_semantics(field, value):
    raw = host_contract()
    raw[field] = value
    with pytest.raises(ValidationError):
        ha_lan.HostLanStatus.model_validate(raw)


@pytest.mark.parametrize("fault", ["missing_array", "over_limit", "duplicate_id", "duplicate_name", "invalid_ip", "invalid_network",
                                   "address_without_network", "wrong_network", "oversize_label", "oversize_name"])
def test_host_interface_inventory_is_typed_bounded_and_identity_consistent(fault):
    raw = host_contract()
    interface = raw["interfaces"][0]
    if fault == "missing_array":
        del raw["interfaces"]
    elif fault == "over_limit":
        raw["interfaces"] = [interface] * 33
    elif fault == "duplicate_id":
        raw["interfaces"].append(copy.deepcopy(interface))
    elif fault == "duplicate_name":
        raw["interfaces"].append({**interface, "id": "b" * 64})
    else:
        field, value = {"invalid_ip": ("address", "arbitrary"), "invalid_network": ("network", "private"),
                       "address_without_network": ("network", None), "wrong_network": ("network", "192.168.11.0/24"),
                       "oversize_label": ("label", "a" * 97), "oversize_name": ("interface", "a" * 33)}[fault]
        interface[field] = value
    with pytest.raises(ValidationError):
        ha_lan.HostLanStatus.model_validate(raw)


def test_unconfigured_and_pending_host_states_retain_explicit_null_selection():
    raw = host_contract()
    raw.update(mode="nat", state="disabled", selected_interface_id=None, interface=None, guest_mac=None,
               vm_state="shut off", can_configure=True)
    raw["interfaces"][0].update(address=None, network=None)
    assert ha_lan.HostLanStatus.model_validate(raw).state == "disabled"
    raw["state"] = "needs_reconcile"
    assert ha_lan.HostLanStatus.model_validate(raw).state == "needs_reconcile"
    # A future Host may impose another configuration restriction; its false
    # capability never grants more authority and remains a valid observation.
    raw["can_configure"] = False
    assert ha_lan.HostLanStatus.model_validate(raw).can_configure is False


class ConvergingHA(NetworkHA):
    def __init__(self):
        super().__init__()
        self.network['adapters'][0]['enabled'] = False
        self.network['configured_adapters'] = []
        self.urls['internal'] = 'http://192.168.177.2:8123'
        self.internal_url = None
        self.mutations = []
        self.compatible = True

    async def ensure_write_compatibility(self):
        if not self.compatible:
            raise HAError('unsupported_core')

    async def request(self, method, path, **kwargs):
        if path == '/config':
            return {'version': '2026.10.1', 'internal_url': self.internal_url}
        return await super().request(method, path, **kwargs)

    async def ws(self, command, **kwargs):
        if guard := kwargs.pop('_send_guard', None):
            await guard()
        if command == 'network/configure':
            self.mutations.append((command, copy.deepcopy(kwargs['config'])))
            self.network['configured_adapters'] = kwargs['config']['configured_adapters']
            self.network['adapters'][0]['enabled'] = 'end1' in self.network['configured_adapters']
            return {'configured_adapters': self.network['configured_adapters']}
        if command == 'config/core/update':
            self.mutations.append((command, {'internal_url': kwargs['internal_url']}))
            self.internal_url = kwargs['internal_url']
            self.urls['internal'] = self.internal_url
            return None
        return await super().ws(command, **kwargs)


def attempt():
    return {**HOST, 'attempt_id': 'b' * 64, 'core_configuration_pending': True}


async def test_fixed_attempt_converges_then_reads_back_without_manual_core_settings(network, store):
    ha = ConvergingHA()
    checks = []
    async def guard():
        checks.append(True)
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['state'] == 'ready' and result['internal_url'] == 'http://192.168.10.20'
    assert ha.mutations == [('network/configure', {'configured_adapters': ['end1']}),
                            ('config/core/update', {'internal_url': 'http://192.168.10.20'})]
    assert len(checks) >= 4 and ha.owner_reads >= 6
    record = store.setting('home_lan_configuration_rollback')
    assert record['previous_adapters'] == [] and record['previous_internal_url'] is None
    assert 'fixture-token' not in json.dumps(record)
    ha.mutations.clear()
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['state'] == 'ready'
    assert ha.mutations == []


async def test_convergence_preserves_existing_selected_management_adapters(network, store):
    ha = ConvergingHA()
    ha.network['configured_adapters'] = ['management0']
    async def guard():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['state'] == 'ready'
    assert ha.network['configured_adapters'] == ['management0', 'end1']


@pytest.mark.parametrize('fault', ['attempt_changed', 'revoked', 'unsupported', 'wrong_mac', 'mismatch', 'invalid_previous_url', 'invalid_configured'])
async def test_convergence_never_writes_without_current_authority_and_valid_contract(network, store, fault):
    ha = ConvergingHA()
    async def guard():
        if fault == 'attempt_changed':
            raise ha_lan.ObservationInvalid('home_lan_attempt_changed')
    if fault == 'revoked':
        ha.owner_value['is_owner'] = False
    elif fault == 'unsupported':
        ha.compatible = False
    elif fault == 'wrong_mac':
        ha.supervisor_info['interfaces'][0]['mac'] = '02:00:00:00:00:01'
    elif fault == 'mismatch':
        ha.network['adapters'][0]['ipv4'] = [{'address': '192.168.10.21'}]
        ha.network['adapters'][0]['ipv6'] = []
    elif fault == 'invalid_previous_url':
        ha.internal_url = 'https://user:secret@private.invalid/'
    elif fault == 'invalid_configured':
        ha.network['configured_adapters'] = [None]
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['state'] != 'ready' and ha.mutations == []


async def test_completed_attempt_only_observes_and_does_not_reapply_over_owner_edits(network, store):
    ha = ConvergingHA()
    async def guard():
        raise AssertionError('completed attempt must not have a mutation scope')
    result = await ha_lan.converge_home_lan(ha, store, {**attempt(), 'core_configuration_pending': False}, guard)
    assert result['state'] == 'adapter_selection_required' and ha.mutations == []
    result = await ha_lan.converge_home_lan(ha, store, {**attempt(), 'attempt_id': None}, guard)
    assert result['reason'] == 'home_lan_attempt_required' and ha.mutations == []


@pytest.mark.parametrize('owner_edit', [False, True])
async def test_nat_fallback_compensates_exact_settings_and_preserves_intervening_owner_edits(network, store, owner_edit):
    ha = ConvergingHA()
    async def guard():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['state'] == 'ready'
    if owner_edit:
        ha.network['configured_adapters'] = ['owner-selection']
        ha.internal_url = 'https://owner.example.invalid'
    ha.mutations.clear()
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat', 'attempt_id': None})
    if owner_edit:
        assert ha.mutations == []
    else:
        assert ha.mutations == [('network/configure', {'configured_adapters': []}),
                                ('config/core/update', {'internal_url': None})]
    assert store.setting('home_lan_configuration_rollback') is None


@pytest.mark.parametrize('changed', ['owner', 'connection'])
async def test_compensation_never_replays_under_changed_home_authority(network, store, changed):
    ha = ConvergingHA()
    async def guard():
        pass
    await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    if changed == 'owner':
        store.set_setting('owner', 'another-owner')
    else:
        ha.base = 'http://192.168.177.3:80/api'
    ha.mutations.clear()
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'})
    assert ha.mutations == [] and store.setting('home_lan_configuration_rollback') is None


async def test_failed_core_write_keeps_compensation_journal_and_recovers_on_nat(network, store):
    ha = ConvergingHA()
    original = ha.ws
    async def lose_response(command, **kwargs):
        result = await original(command, **kwargs)
        if command == 'network/configure':
            raise HAError('lost_after_write')
        return result
    ha.ws = lose_response
    async def guard():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['state'] == 'unavailable' and store.setting('home_lan_configuration_rollback')
    ha.ws = original
    ha.mutations.clear()
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'})
    assert ha.mutations == [('network/configure', {'configured_adapters': []})]
    assert store.setting('home_lan_configuration_rollback') is None


async def test_fallback_compensation_rechecks_host_before_each_effect(network, store):
    ha = ConvergingHA()
    async def guard():
        pass
    await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    ha.mutations.clear()
    async def changed_host():
        raise ha_lan.ObservationInvalid('home_lan_attempt_changed')
    with pytest.raises(ha_lan.ObservationInvalid):
        await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'}, changed_host)
    assert ha.mutations == [] and store.setting('home_lan_configuration_rollback')


async def test_background_maintenance_converges_without_browser_and_publishes_exact_attempt(network, store, monkeypatch):
    from agentic_controller import operations
    ha = ConvergingHA()
    initial = {**host_contract(), **attempt()}
    posts = []
    async def broker(method, path, body=None, *, store):
        if method == 'GET':
            assert path == '/network/ha-lan'
            return copy.deepcopy(initial)
        assert path == '/network/ha-lan/observation'
        posts.append(body)
        return {'accepted': True}
    async def end_loop(_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(operations, 'broker_call', broker)
    monkeypatch.setattr(operations.asyncio, 'sleep', end_loop)
    with pytest.raises(asyncio.CancelledError):
        await operations.maintain_home_lan(ha, store)
    assert len(posts) == 1 and posts[0]['attempt_id'] == initial['attempt_id']
    assert posts[0]['guest_mac'] == GUEST and posts[0]['home']['state'] == 'ready'
    assert ha.mutations


async def test_successful_lan_retains_field_ownership_for_later_explicit_nat_switch(network, store):
    ha = ConvergingHA()
    async def guard():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['state'] == 'ready'
    await ha_lan.compensate_home_lan(ha, store, {**attempt(), 'core_configuration_pending': False})
    assert store.setting('home_lan_configuration_rollback')
    ha.mutations.clear()
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'})
    assert ha.mutations == [('network/configure', {'configured_adapters': []}),
                            ('config/core/update', {'internal_url': None})]


async def test_reconnect_adopts_owned_fields_without_overwriting_original_baseline(network, store):
    ha = ConvergingHA()
    async def guard():
        pass
    await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    reconnect = {**attempt(), 'attempt_id': 'c' * 64}
    await ha_lan.compensate_home_lan(ha, store, reconnect)
    ha.mutations.clear()
    assert (await ha_lan.converge_home_lan(ha, store, reconnect, guard))['state'] == 'ready'
    assert ha.mutations == []
    record = store.setting('home_lan_configuration_rollback')
    assert record['attempt_id'] == 'c' * 64 and record['previous_adapters'] == []
    assert record['previous_internal_url'] is None
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'})
    assert ha.internal_url is None and ha.network['configured_adapters'] == []


@pytest.mark.parametrize('base,ipv6,expected', [
    ('http://192.168.177.2:80/api', False, 'http://192.168.10.20'),
    ('http://192.168.177.2:8123/api', False, 'http://192.168.10.20:8123'),
    ('https://192.168.177.2:8443/api', False, 'https://192.168.10.20:8443'),
    ('https://192.168.177.2:443/api', True, 'https://[fd12:3456::20]'),
    ('http://192.168.177.2:8123/api', True, 'http://[fd12:3456::20]:8123'),
])
async def test_convergence_uses_verified_core_listener(network, store, base, ipv6, expected):
    ha = ConvergingHA()
    ha.base = base
    if ipv6:
        ha.supervisor_info['interfaces'][0]['ipv4']['address'] = []
        ha.network['adapters'][0]['ipv4'] = []
    async def guard():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['state'] == 'ready' and result['internal_url'] == expected
    assert ha.internal_url == expected


@pytest.mark.parametrize('external,base', [
    (False, 'http://192.168.177.2:80/api'), (True, 'http://supervisor/core/api'),
    (True, 'http://192.168.10.20:80/api'), (True, 'fixture-core'),
    (True, '//192.168.177.2/api'), (True, 'ftp://192.168.177.2/api'),
    (True, 'http://user:secret@192.168.177.2/api'), (True, 'http://192.168.177.2:0/api'),
])
async def test_unverified_core_listener_cannot_mutate(network, store, external, base):
    ha = ConvergingHA()
    ha.external, ha.base = external, base
    async def guard():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['reason'] == 'core_listener_unverified' and ha.mutations == []


async def test_product_url_requires_exact_core_config_readback(network, store):
    ha = ConvergingHA()
    original = ha.request
    async def different_stored_value(method, path, **kwargs):
        if path == '/config' and any(command == 'config/core/update' for command, _ in ha.mutations):
            return {'internal_url': 'http://192.168.10.20:9999'}
        return await original(method, path, **kwargs)
    ha.request = different_stored_value
    async def guard():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['reason'] == 'core_internal_url_readback_mismatch'
    assert result['state'] == 'unavailable'
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['reason'] == 'core_internal_url_readback_mismatch'
    assert result['state'] == 'unavailable'


async def test_enabling_adapter_preserves_existing_owner_lan_url(network, store):
    ha = ConvergingHA()
    ha.internal_url = ha.urls['internal'] = 'https://192.168.10.20:8443'
    async def guard():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['state'] == 'ready'
    assert ha.mutations == [('network/configure', {'configured_adapters': ['end1']})]
    assert ha.internal_url == 'https://192.168.10.20:8443'


@pytest.mark.parametrize('operation,fault', [(operation, fault) for operation in ['converge', 'compensate']
    for fault in ['epoch', 'owner', 'attempt', 'host_mode', 'active_owner', 'connection', 'field_edit']])
@pytest.mark.parametrize('command', ['network/configure', 'config/core/update'])
@pytest.mark.parametrize('barrier', ['connect', 'durable'])
async def test_lan_mutations_recheck_authority_at_transport_send(network, store, monkeypatch, operation, command, barrier, fault):
    from agentic_controller.ha import HomeAssistant
    from aiohttp import web

    fixture = ConvergingHA()
    fixture.internal_url = fixture.urls['internal'] = 'http://192.168.10.20'
    if command == 'config/core/update' or operation == 'compensate':
        fixture.network['adapters'][0]['enabled'] = True
        fixture.network['configured_adapters'] = ['end1']
    if command == 'config/core/update' and operation == 'converge':
        fixture.internal_url = fixture.urls['internal'] = None
    mutations = []
    application = web.Application()
    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'auth_required'})
        assert (await ws.receive_json())['type'] == 'auth'
        await ws.send_json({'type': 'auth_ok'})
        async for message in ws:
            frame = message.json()
            kind = frame['type']
            if kind == 'config/auth/list':
                result = [fixture.owner_value]
            elif kind in {'network', 'network/url'}:
                result = fixture.network if kind == 'network' else fixture.urls
            else:
                mutations.append(frame)
                result = await fixture.ws(kind, **{key: value for key, value in frame.items() if key not in {'id', 'type'}})
            await ws.send_json({'id': frame['id'], 'type': 'result', 'success': True, 'result': result})
        return ws
    application.router.add_get('/websocket', websocket)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ha = HomeAssistant('fixture', base=fixture.base, websocket=f'ws://127.0.0.1:{port}/websocket', external=True)
    ha.store = store
    # Only management reads are synthetic; every WS uses the real transport.
    ha.request = fixture.request
    await ha.start()
    initial = attempt() if operation == 'converge' else {'mode': 'nat', 'attempt_id': None}
    current = dict(initial)
    async def check_host():
        if current != initial:
            raise ha_lan.ObservationInvalid('home_lan_attempt_changed')
    if operation == 'compensate':
        store.set_setting('home_lan_configuration_rollback', {
            'attempt_id': 'b' * 64, 'owner': 'owner', 'epoch': store.setting('hermes_web_epoch'),
            'connection': ha_lan.digest(tuple(getattr(ha, key) for key in ('token', 'base', 'websocket', 'supervisor', 'external'))),
            'previous_adapters': [], 'previous_internal_url': None,
            'target_adapters': ['end1'] if command == 'network/configure' else None,
            'target_internal_url': fixture.internal_url if command == 'config/core/update' else None})
    entered, release = asyncio.Event(), asyncio.Event()
    armed = False
    original_ws, original_connect, original_durable = ha.ws, ha.ws_connect, store.durable
    async def arm(kind, **kwargs):
        nonlocal armed
        if kind == command:
            armed = True
        return await original_ws(kind, **kwargs)
    async def pause(stage):
        nonlocal armed
        if armed and barrier == stage:
            armed = False
            entered.set()
            await release.wait()
    async def connect():
        ws = await original_connect()
        await pause('connect')
        return ws
    async def disk():
        await original_durable()
        await pause('durable')
    monkeypatch.setattr(ha, 'ws', arm)
    monkeypatch.setattr(ha, 'ws_connect', connect)
    monkeypatch.setattr(store, 'durable', disk)
    async def run():
        if operation == 'converge':
            return await ha_lan.converge_home_lan(ha, store, initial, check_host)
        try:
            await ha_lan.compensate_home_lan(ha, store, initial, check_host)
        except ha_lan.ObservationInvalid as error:
            return {'reason': str(error)}
        return {'reason': 'completed'}
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if fault in {'epoch', 'owner'}:
            store.set_setting('hermes_web_epoch' if fault == 'epoch' else 'owner', 'changed')
        elif fault == 'attempt':
            current['attempt_id'] = 'c' * 64
        elif fault == 'host_mode':
            current['mode'] = 'nat' if operation == 'converge' else 'wired'
        elif fault == 'active_owner':
            fixture.owner_value['is_active'] = False
        elif fault == 'field_edit':
            fixture.network['configured_adapters'] = ['owner-selection']
            fixture.internal_url = 'https://owner.example.invalid'
        else:
            ha.token = 'changed-fixture'
        release.set()
        result = await asyncio.wait_for(task, 5)
        expected = ('home_lan_attempt_changed' if fault in {'attempt', 'host_mode'} else
                    'home_owner_not_active' if fault == 'active_owner' else 'home_authority_changed')
        if fault == 'field_edit':
            assert mutations == []
            assert fixture.network['configured_adapters'] == ['owner-selection']
            assert fixture.internal_url == 'https://owner.example.invalid'
            assert store.setting('home_lan_configuration_rollback') is None
            assert result['reason'] == ('home_lan_owner_conflict' if operation == 'converge' else 'completed')
            if operation == 'converge':
                assert (await run())['reason'] == 'home_lan_reauthorization_required'
                assert mutations == []
                assert (await ha_lan.observe_home_lan(ha, store, initial))['reason'] == 'home_lan_reauthorization_required'
                # Even reverting the value cannot renew the fenced attempt.
                fixture.network['configured_adapters'] = [] if command == 'network/configure' else ['end1']
                fixture.internal_url = fixture.urls['internal'] = None
                assert (await run())['reason'] == 'home_lan_reauthorization_required'
                initial['attempt_id'] = current['attempt_id'] = 'c' * 64
                assert (await run())['state'] == 'ready'
                assert mutations
            return
        assert result['reason'] == expected
        assert mutations == []
        assert bool(store.setting('home_lan_configuration_rollback')) == (operation == 'compensate')
        if fault == 'epoch':
            await ha_lan.compensate_home_lan(ha, store, attempt(), check_host)
            assert bool(store.setting('home_lan_configuration_rollback')) == (operation == 'compensate')
            if operation == 'converge':
                assert (await run())['reason'] == 'home_lan_reauthorization_required'
                assert mutations == []
                initial['attempt_id'] = current['attempt_id'] = 'c' * 64
                assert (await run())['state'] == 'ready'
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ha.close()
        await runner.cleanup()


@pytest.mark.parametrize('owner_edit', [False, True])
async def test_logout_retains_baseline_until_fresh_nat_compensation(network, store, owner_edit):
    ha = ConvergingHA()
    async def guard():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['state'] == 'ready'
    baseline = store.setting('home_lan_configuration_rollback')
    store.set_setting('hermes_web_epoch', 'logged-out')
    await ha_lan.compensate_home_lan(ha, store, attempt(), guard)
    assert store.setting('home_lan_configuration_rollback') == baseline
    store.set_setting('hermes_web_epoch', 'new-login')
    if owner_edit:
        ha.network['configured_adapters'] = ['owner-selection']
        ha.internal_url = 'https://owner.example.invalid'
    ha.mutations.clear()
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'}, guard)
    assert ha.network['configured_adapters'] == (['owner-selection'] if owner_edit else [])
    assert ha.internal_url == ('https://owner.example.invalid' if owner_edit else None)
    assert len(ha.mutations) == (0 if owner_edit else 2)
    assert store.setting('home_lan_configuration_rollback') is None


async def test_reconnect_after_logout_carries_original_field_baseline(network, store):
    ha = ConvergingHA()
    async def guard():
        pass
    await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    store.set_setting('hermes_web_epoch', 'new-login')
    reconnect = {**attempt(), 'attempt_id': 'c' * 64}
    assert (await ha_lan.converge_home_lan(ha, store, reconnect, guard))['state'] == 'ready'
    baseline = store.setting('home_lan_configuration_rollback')
    assert baseline and baseline['previous_adapters'] == [] and baseline['previous_internal_url'] is None
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'}, guard)
    assert ha.network['configured_adapters'] == [] and ha.internal_url is None


@pytest.mark.parametrize('fault', ['epoch', 'owner', 'connection'])
async def test_attempt_authority_survives_retry_and_store_reopen(network, store, fault):
    from agentic_controller.store import Store
    ha = ConvergingHA()
    original = ha.ws
    changed = False
    async def revoke(command, **kwargs):
        nonlocal changed
        if command == 'network/configure' and not changed:
            changed = True
            if fault == 'connection':
                ha.token = 'changed-fixture'
            else:
                store.set_setting('hermes_web_epoch' if fault == 'epoch' else 'owner', 'changed')
        return await original(command, **kwargs)
    ha.ws = revoke
    async def guard():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['reason'] == 'home_authority_changed'
    assert ha.mutations == []
    # Maintenance retries the same Host attempt even without another owner action.
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['reason'] == 'home_lan_reauthorization_required'
    assert ha.mutations == []
    await store.durable()
    fresh = Store(store.path)
    try:
        assert (await ha_lan.converge_home_lan(ha, fresh, attempt(), guard))['reason'] == 'home_lan_reauthorization_required'
        fresh.set_setting('owner', 'owner')
        fresh.set_setting('hermes_web_epoch', 'new-login')
        assert (await ha_lan.converge_home_lan(ha, fresh, {**attempt(), 'attempt_id': 'c' * 64}, guard))['state'] == 'ready'
    finally:
        fresh.close()


async def test_completed_attempt_ignores_later_logout_but_pending_attempt_is_fenced(network, store):
    ha = ConvergingHA()
    async def guard():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['state'] == 'ready'
    store.set_setting('hermes_web_epoch', 'logged-out')
    ha.mutations.clear()
    pending = await ha_lan.observe_home_lan(ha, store, attempt())
    assert pending['reason'] == 'home_lan_reauthorization_required'
    complete = {**attempt(), 'core_configuration_pending': False}
    assert (await ha_lan.observe_home_lan(ha, store, complete))['state'] == 'ready'
    assert (await ha_lan.converge_home_lan(ha, store, complete, guard))['state'] == 'ready'
    assert ha.mutations == []


@pytest.mark.parametrize('fault', ['epoch', 'field_edit'])
async def test_fenced_attempt_compensates_only_fields_already_written(network, store, fault):
    ha = ConvergingHA()
    original = ha.ws
    async def change(command, **kwargs):
        if command == 'config/core/update':
            if fault == 'epoch':
                store.set_setting('hermes_web_epoch', 'logged-out')
            else:
                ha.internal_url = 'https://owner.example.invalid'
        return await original(command, **kwargs)
    ha.ws = change
    async def guard():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), guard)
    assert result['reason'] == ('home_authority_changed' if fault == 'epoch' else 'home_lan_owner_conflict')
    assert ha.mutations == [('network/configure', {'configured_adapters': ['end1']})]
    baseline = store.setting('home_lan_configuration_rollback')
    assert baseline['target_adapters'] == ['end1'] and baseline['target_internal_url'] is None
    ha.ws = original
    ha.mutations.clear()
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), guard))['reason'] == 'home_lan_reauthorization_required'
    assert ha.mutations == []
    store.set_setting('hermes_web_epoch', 'new-login')
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'}, guard)
    assert ha.mutations == [('network/configure', {'configured_adapters': []})]
    assert ha.internal_url == (None if fault == 'epoch' else 'https://owner.example.invalid')
    assert store.setting('home_lan_configuration_rollback') is None


@pytest.mark.parametrize('command', ['network/configure', 'config/core/update'])
async def test_convergence_rereads_field_after_remote_authority_checks(network, store, command):
    ha = ConvergingHA()
    if command == 'config/core/update':
        ha.network['configured_adapters'] = ['end1']
        ha.network['adapters'][0]['enabled'] = True
    original = ha.ws
    armed = False
    async def arm(kind, **kwargs):
        nonlocal armed
        if kind == command:
            armed = True
        return await original(kind, **kwargs)
    ha.ws = arm
    async def check_host():
        if armed:
            ha.network['configured_adapters'] = ['owner-selection']
            ha.internal_url = 'https://owner.example.invalid'
    result = await ha_lan.converge_home_lan(ha, store, attempt(), check_host)
    assert result['reason'] == 'home_lan_owner_conflict'
    assert ha.mutations == []
    assert store.setting('home_lan_configuration_rollback') is None


@pytest.mark.parametrize('mode,state,vm', [('wired', 'configured', 'running'), ('nat', 'needs_reconcile', 'running'),
                                         ('nat', 'disabled', 'shut off')])
def test_host_contract_rejects_inconsistent_completed_fallback(mode, state, vm):
    raw = host_contract()
    if mode == 'nat':
        raw.update(selected_interface_id=None, interface=None, guest_mac=None)
    raw.update(mode=mode, state=state, vm_state=vm, fallback_complete=True)
    with pytest.raises(ValidationError, match='host_fallback_state_invalid'):
        ha_lan.HostLanStatus.model_validate(raw)


def test_older_host_cannot_imply_completed_fallback():
    raw = host_contract()
    raw.update(mode='nat', state='disabled', selected_interface_id=None, interface=None, guest_mac=None,
               vm_state='running', message='舊訊息')
    assert ha_lan.HostLanStatus.model_validate(raw).fallback_complete is False
    raw['fallback_complete'] = True
    assert ha_lan.HostLanStatus.model_validate(raw).fallback_complete is True


def test_recovery_pending_defaults_false_for_older_hosts():
    raw = host_contract()
    assert ha_lan.HostLanStatus.model_validate(raw).recovery_pending is False
    raw['recovery_pending'] = True
    assert ha_lan.HostLanStatus.model_validate(raw).recovery_pending is True


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('command', ['network/configure', 'config/core/update'])
@pytest.mark.parametrize('fault', ['owner_edit', 'unchanged', 'lost_reply'])
async def test_same_attempt_retry_after_failed_transport(network, store, command, restart, fault):
    from agentic_controller.ha import HomeAssistant
    from agentic_controller.models import digest
    from agentic_controller.store import Store
    from aiohttp import web
    fixture = ConvergingHA()
    fixture.network['adapters'].append({'name': 'end0', 'index': 2, 'enabled': True,
        'default': True, 'auto': True, 'ipv4': [{'address': '192.168.177.2', 'network_prefix': 24}], 'ipv6': []})
    if command == 'network/configure':
        fixture.internal_url = fixture.urls['internal'] = 'http://192.168.10.20'
    else:
        fixture.network['configured_adapters'] = ['end1']
        fixture.network['adapters'][0]['enabled'] = True
    field = 'configured_adapters' if command == 'network/configure' else 'internal_url'
    target_key = 'target_adapters' if command == 'network/configure' else 'target_internal_url'
    initial = {'configured_adapters': digest(fixture.network['configured_adapters']), 'internal_url': digest(fixture.internal_url)}
    owner_value = ['end0'] if command == 'network/configure' else 'https://owner.example.invalid'
    entered, release = asyncio.Event(), asyncio.Event()
    armed, fail_once = False, True
    mutations = []
    application = web.Application()

    async def websocket(request):
        nonlocal armed
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'auth_required'})
        assert (await ws.receive_json())['type'] == 'auth'
        fail = armed
        if fail:
            armed = False
            entered.set()
            await release.wait()
            if fault != 'lost_reply':
                await ws.close()
                return ws
        await ws.send_json({'type': 'auth_ok'})
        async for message in ws:
            frame = message.json()
            kind = frame['type']
            if kind == 'config/auth/list':
                result = [fixture.owner_value]
            elif kind in {'network', 'network/url'}:
                result = fixture.network if kind == 'network' else fixture.urls
            else:
                mutations.append(frame)
                result = await fixture.ws(kind, **{key: value for key, value in frame.items() if key not in {'id', 'type'}})
                if fail:
                    await ws.close()
                    return ws
            await ws.send_json({'id': frame['id'], 'type': 'result', 'success': True, 'result': result})
        return ws

    application.router.add_get('/websocket', websocket)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ha = HomeAssistant('fixture', base=fixture.base, websocket=f'ws://127.0.0.1:{port}/websocket', external=True)
    ha.store = store
    ha.request = fixture.request
    await ha.start()
    original = ha.ws

    async def ws(kind, **kwargs):
        nonlocal armed, fail_once
        if kind == command and fail_once:
            armed, fail_once = True, False
        return await original(kind, **kwargs)

    ha.ws = ws
    async def check_host():
        pass
    task = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    fresh = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        disk = Store(store.path)
        try:
            recorded = disk.setting('home_lan_configuration_attempt')
        finally:
            disk.close()
        if fault == 'owner_edit':
            if command == 'network/configure':
                fixture.network[field] = owner_value
            else:
                fixture.internal_url = fixture.urls['internal'] = owner_value
        release.set()
        first = await asyncio.wait_for(task, 5)
        assert first['reason'] == 'home_network_observation_unavailable'
        record = store.setting('home_lan_configuration_attempt')
        assert not record['revoked'] and not record['conflicts']
        if fault == 'lost_reply':
            assert len(mutations) == 1
            assert store.setting('home_lan_configuration_rollback')[target_key] is not None
        else:
            assert mutations == [] and store.setting('home_lan_configuration_rollback') is None
        await store.durable()
        fresh = Store(store.path) if restart else None
        active = fresh or store
        ha.store = active
        mutations.clear()
        second = await ha_lan.converge_home_lan(ha, active, attempt(), check_host)
        if fault == 'owner_edit':
            assert mutations == []
            assert (fixture.network[field] if command == 'network/configure' else fixture.internal_url) == owner_value
            assert second['reason'] == 'home_lan_owner_conflict'
            disk = Store(store.path)
            try:
                assert disk.setting('home_lan_configuration_attempt')['conflicts'] == [field]
            finally:
                disk.close()
            assert (await ha_lan.converge_home_lan(ha, active, attempt(), check_host))['reason'] == 'home_lan_reauthorization_required'
            assert mutations == [] and active.setting('home_lan_configuration_rollback') is None
            assert (await ha_lan.converge_home_lan(ha, active, {**attempt(), 'attempt_id': 'c' * 64}, check_host))['state'] == 'ready'
            assert len(mutations) == 1 and mutations[0]['type'] == command
            assert active.setting('home_lan_configuration_attempt')['initial_fields'] == {**initial, field: digest(owner_value)}
        else:
            assert second['state'] == 'ready'
            assert len(mutations) == (0 if fault == 'lost_reply' else 1)
            record = active.setting('home_lan_configuration_attempt')
            assert not record['revoked'] and not record['conflicts']
        assert recorded['initial_fields'] == initial
        assert 'token' not in recorded
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ha.close()
        await runner.cleanup()
        if fresh:
            fresh.close()


@pytest.mark.parametrize('legacy', [False, True])
async def test_attempt_retains_first_observation_for_later_plans_and_legacy_records(network, store, legacy):
    from agentic_controller.models import digest
    ha = ConvergingHA()
    ha.internal_url = ha.urls['internal'] = 'http://192.168.10.20'
    async def check_host():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['state'] == 'ready'
    record = store.setting('home_lan_configuration_attempt')
    if legacy:
        record.pop('initial_fields', None)
        record['expected'] = {'configured_adapters': ['ignored'], 'internal_url': 'https://ignored.invalid'}
        store.set_setting('home_lan_configuration_attempt', record)
    else:
        assert record['initial_fields'] == {'configured_adapters': digest([]), 'internal_url': digest('http://192.168.10.20')}
    ha.supervisor_info['interfaces'][0]['ipv4']['address'] = ['192.168.10.21/24']
    ha.network['adapters'][0]['ipv4'][0]['address'] = '192.168.10.21'
    original = ha.ws
    async def fail(kind, **kwargs):
        if kind == 'config/core/update':
            raise HAError('fixture handshake closed')
        return await original(kind, **kwargs)
    ha.ws = fail
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == 'home_network_observation_unavailable'
    expected = {'internal_url': digest('http://192.168.10.20'), 'configured_adapters': digest(['end1'] if legacy else [])}
    assert store.setting('home_lan_configuration_attempt')['initial_fields'] == expected
    assert store.setting('home_lan_configuration_rollback')['target_internal_url'] is None
    ha.internal_url = ha.urls['internal'] = 'https://owner.example.invalid'
    ha.ws = original
    ha.mutations.clear()
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == 'home_lan_owner_conflict'
    assert ha.mutations == []


@pytest.mark.parametrize('command', ['network/configure', 'config/core/update'])
async def test_same_attempt_accepts_owned_target_when_another_write_is_needed(network, store, command):
    ha = ConvergingHA()
    original = ha.ws
    async def lose_reply(kind, **kwargs):
        result = await original(kind, **kwargs)
        if kind == command:
            raise HAError('fixture reply lost')
        return result
    ha.ws = lose_reply
    async def check_host():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == 'home_network_observation_unavailable'
    if command == 'network/configure':
        ha.network['adapters'][0]['enabled'] = False
    else:
        ha.supervisor_info['interfaces'][0]['ipv4']['address'] = ['192.168.10.21/24']
        ha.network['adapters'][0]['ipv4'][0]['address'] = '192.168.10.21'
    ha.ws = original
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['state'] == 'ready'
    await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'}, check_host)
    assert ha.network['configured_adapters'] == [] and ha.internal_url is None


async def test_retry_fences_url_edit_before_other_writes_or_baseline_validation(network, store):
    from agentic_controller.models import digest
    ha = ConvergingHA()
    original = ha.ws
    async def fail(kind, **kwargs):
        if kind == 'config/core/update':
            raise HAError('fixture handshake closed')
        return await original(kind, **kwargs)
    ha.ws = fail
    async def check_host():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == 'home_network_observation_unavailable'
    baseline = store.setting('home_lan_configuration_rollback')
    assert baseline['target_adapters'] == ['end1'] and baseline['target_internal_url'] is None
    ha.internal_url = ha.urls['internal'] = 'https://owner.example.invalid/home'
    ha.network['adapters'][0]['enabled'] = False
    ha.ws = original
    ha.mutations.clear()
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == 'home_lan_owner_conflict'
    assert ha.mutations == [] and ha.internal_url == 'https://owner.example.invalid/home'
    assert store.setting('home_lan_configuration_rollback') == baseline
    record = store.setting('home_lan_configuration_attempt')
    assert record['initial_fields'] == {'configured_adapters': digest([]), 'internal_url': digest(None)}
    assert record['conflicts'] == ['internal_url'] and 'owner.example' not in json.dumps(record)
    ha.internal_url = None
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == 'home_lan_reauthorization_required'
    assert ha.mutations == []


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('owner_edit', [False, True])
@pytest.mark.parametrize('stage,field', [
    (stage, field) for stage in ('owner', 'compatibility', 'config_read', 'sibling_read')
    for field in ('configured_adapters', 'internal_url')
] + [('unsafe_url', 'configured_adapters')])
async def test_first_observation_survives_early_failure(network, store, restart, owner_edit, stage, field):
    from agentic_controller.ha import HomeAssistant
    from agentic_controller.models import digest
    from agentic_controller.store import Store
    ha = ConvergingHA()
    ha.network['adapters'].append({'name': 'end0', 'index': 2, 'enabled': True,
        'default': True, 'auto': True, 'ipv4': [{'address': '192.168.177.2', 'network_prefix': 24}], 'ipv6': []})
    if field == 'configured_adapters':
        ha.internal_url = ha.urls['internal'] = 'http://192.168.10.20'
    else:
        ha.network['configured_adapters'] = ['end1']
        ha.network['adapters'][0]['enabled'] = True
    if stage == 'unsafe_url':
        ha.internal_url = 'https://owner.invalid/path'
    initial = {'configured_adapters': digest(ha.network['configured_adapters']), 'internal_url': digest(ha.internal_url)}
    owner_value = ['end0'] if field == 'configured_adapters' else 'https://owner.invalid'
    ha.ensure_write_compatibility = HomeAssistant.ensure_write_compatibility.__get__(ha)
    original_owner, original_request, original_ws = ha.owner, ha.request, ha.ws
    after_gather, reads, fired = False, 0, False
    disk_at_wait = []
    network_read = asyncio.Event()

    def edit():
        if owner_edit:
            if field == 'configured_adapters':
                ha.network[field] = owner_value
            else:
                ha.internal_url = ha.urls['internal'] = owner_value

    def fail():
        nonlocal fired
        fired = True
        edit()
        raise HAError('fixture transient observation failure')

    async def owner(value):
        nonlocal after_gather
        if ha.owner_reads == 1:
            after_gather = True
            disk = Store(store.path)
            try:
                disk_at_wait.append(disk.setting('home_lan_configuration_attempt'))
            finally:
                disk.close()
            if stage == 'owner':
                fail()
        return await original_owner(value)

    async def request(method, path, **kwargs):
        nonlocal reads
        if path == '/config' and after_gather:
            reads += 1
            if (stage == 'compatibility' and reads == 1) or (stage == 'config_read' and reads == 2):
                fail()
        return await original_request(method, path, **kwargs)

    async def ws(command, **kwargs):
        if command == 'network/url' and stage == 'sibling_read':
            await network_read.wait()
            await asyncio.sleep(0)
            fail()
        result = await original_ws(command, **kwargs)
        if command == 'network':
            network_read.set()
        return result

    ha.owner, ha.request, ha.ws = owner, request, ws
    async def check_host():
        pass
    first = await ha_lan.converge_home_lan(ha, store, attempt(), check_host)
    assert first['reason'] == ('previous_internal_url_invalid' if stage == 'unsafe_url' else 'home_network_observation_unavailable')
    assert fired is (stage != 'unsafe_url') and ha.mutations == []
    record = store.setting('home_lan_configuration_attempt')
    assert not record['revoked'] and not record['conflicts']
    assert store.setting('home_lan_configuration_rollback') is None
    if stage == 'unsafe_url':
        ha.internal_url = ha.urls['internal']
        edit()
    ha.owner, ha.request, ha.ws = original_owner, original_request, original_ws
    fresh = Store(store.path) if restart else None
    try:
        active = fresh or store
        second = await ha_lan.converge_home_lan(ha, active, attempt(), check_host)
        if owner_edit:
            assert ha.mutations == []
            assert (ha.network[field] if field == 'configured_adapters' else ha.internal_url) == owner_value
            assert second['reason'] == 'home_lan_owner_conflict'
            disk = Store(store.path)
            try:
                assert disk.setting('home_lan_configuration_attempt')['conflicts'] == [field]
            finally:
                disk.close()
            assert (await ha_lan.converge_home_lan(ha, active, attempt(), check_host))['reason'] == 'home_lan_reauthorization_required'
            assert ha.mutations == [] and active.setting('home_lan_configuration_rollback') is None
            assert (await ha_lan.converge_home_lan(ha, active, {**attempt(), 'attempt_id': 'c' * 64}, check_host))['state'] == 'ready'
            assert len(ha.mutations) == 1
            assert active.setting('home_lan_configuration_attempt')['initial_fields'][field] == digest(owner_value)
        else:
            assert second['state'] == 'ready' and len(ha.mutations) == 1
            assert not active.setting('home_lan_configuration_attempt')['conflicts']
        assert record['initial_fields'] == initial and 'expected' not in record
        assert disk_at_wait[0]['initial_fields'] == initial
    finally:
        if fresh:
            fresh.close()


async def test_first_observation_fences_edit_before_explicit_config_read(network, store):
    from agentic_controller.ha import HomeAssistant
    from agentic_controller.models import digest
    from agentic_controller.store import Store
    ha = ConvergingHA()
    ha.ensure_write_compatibility = HomeAssistant.ensure_write_compatibility.__get__(ha)
    original = ha.request
    after_gather, reads = False, 0
    def owner(count):
        nonlocal after_gather
        if count == 2:
            after_gather = True
    ha.on_owner = owner
    async def request(method, path, **kwargs):
        nonlocal reads
        if path == '/config' and after_gather:
            reads += 1
            if reads == 2:
                ha.internal_url = ha.urls['internal'] = 'https://owner.invalid'
        return await original(method, path, **kwargs)
    ha.request = request
    async def check_host():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), check_host)
    assert ha.mutations == [] and ha.internal_url == 'https://owner.invalid'
    assert result['reason'] == 'home_lan_owner_conflict'
    disk = Store(store.path)
    try:
        record = disk.setting('home_lan_configuration_attempt')
        assert record['initial_fields'] == {'configured_adapters': digest([]), 'internal_url': digest(None)}
        assert record['conflicts'] == ['internal_url']
        assert disk.setting('home_lan_configuration_rollback') is None
        assert (await ha_lan.converge_home_lan(ha, disk, attempt(), check_host))['reason'] == 'home_lan_reauthorization_required'
        assert ha.mutations == []
    finally:
        disk.close()


@pytest.mark.parametrize('established', [False, True])
@pytest.mark.parametrize('unsafe', ['https://fixture-user:fixture-secret@owner.invalid',
    'https://owner.invalid/?fixture-secret=123', 'https://owner.invalid/#fixture-secret',
    'https://owner.invalid/' + 'x' * 20000, {'secret': 'fixture-secret'}],
    ids=['credentials', 'query', 'fragment', 'oversize', 'non_string'])
async def test_first_observation_persists_only_digests_of_unsafe_urls(network, store, established, unsafe):
    from agentic_controller.models import digest
    from agentic_controller.store import Store
    ha = ConvergingHA()
    if established:
        original = ha.ws
        async def fail(command, **kwargs):
            if command == 'config/core/update':
                raise HAError('fixture handshake failed')
            return await original(command, **kwargs)
        ha.ws = fail
    async def check_host():
        pass
    if established:
        assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == 'home_network_observation_unavailable'
        ha.ws = original
    before = store.setting('home_lan_configuration_rollback')
    ha.internal_url = unsafe
    ha.urls['internal'] = unsafe if isinstance(unsafe, str) else None
    ha.mutations.clear()
    result = await ha_lan.converge_home_lan(ha, store, attempt(), check_host)
    assert result['reason'] == ('home_lan_owner_conflict' if established else 'previous_internal_url_invalid')
    assert ha.mutations == []
    disk = Store(store.path)
    try:
        record = disk.setting('home_lan_configuration_attempt')
        rollback = disk.setting('home_lan_configuration_rollback')
        assert rollback == before
        assert record['initial_fields'] == {'configured_adapters': digest([]), 'internal_url': digest(None if established else unsafe)}
        assert 'expected' not in record
        serialized = json.dumps([record, rollback])
        assert 'fixture-secret' not in serialized and 'owner.invalid' not in serialized and len(serialized) < 3000
    finally:
        disk.close()


@pytest.mark.parametrize('fault,reason,retained', [
    ('network_failure', 'home_network_observation_unavailable', 'internal_url'),
    ('config_failure', 'home_network_observation_unavailable', 'configured_adapters'),
    ('network_shape', 'network_contract_invalid', 'internal_url'),
    ('adapters_missing', 'core_contract_invalid', 'internal_url'),
    ('adapters_shape', 'core_contract_invalid', 'internal_url'),
    ('adapters_name', 'interface_contract_invalid', 'internal_url'),
    ('config_shape', 'core_config_contract_invalid', 'configured_adapters'),
    ('config_missing', 'core_config_contract_invalid', 'configured_adapters')])
async def test_first_observation_records_valid_sibling_before_contract_failure(network, store, fault, reason, retained):
    from agentic_controller.models import digest
    from agentic_controller.store import Store
    ha = ConvergingHA()
    original_request, original_ws = ha.request, ha.ws
    async def request(method, path, **kwargs):
        if path == '/config':
            if fault == 'config_failure':
                raise HAError('fixture read failed')
            if fault == 'config_shape':
                return []
            if fault == 'config_missing':
                return {}
        return await original_request(method, path, **kwargs)
    async def ws(command, **kwargs):
        if command == 'network':
            if fault == 'network_failure':
                raise HAError('fixture read failed')
            if fault == 'network_shape':
                return []
            if fault == 'adapters_missing':
                ha.network.pop('configured_adapters')
            elif fault == 'adapters_shape':
                ha.network['configured_adapters'] = [None]
            elif fault == 'adapters_name':
                ha.network['configured_adapters'] = ['bad/name']
        return await original_ws(command, **kwargs)
    ha.request, ha.ws = request, ws
    async def check_host():
        pass
    result = await ha_lan.converge_home_lan(ha, store, attempt(), check_host)
    assert result['reason'] == reason and ha.mutations == []
    disk = Store(store.path)
    try:
        record = disk.setting('home_lan_configuration_attempt')
        assert record['initial_fields'] == {retained: digest([] if retained == 'configured_adapters' else None)}
        assert not record['revoked'] and not record['conflicts']
        assert disk.setting('home_lan_configuration_rollback') is None
    finally:
        disk.close()


@pytest.mark.parametrize('interruption', ['timeout', 'cancel'])
@pytest.mark.parametrize('field', ['configured_adapters', 'internal_url'])
@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('owner_edit', [False, True])
async def test_first_observation_survives_partial_gather(network, store, monkeypatch, interruption, field, restart, owner_edit):
    from agentic_controller.models import digest
    from agentic_controller.store import Store
    ha = ConvergingHA()
    ha.network['adapters'].append({'name': 'end0', 'index': 2, 'enabled': True,
        'default': True, 'auto': True, 'ipv4': [{'address': '192.168.177.2', 'network_prefix': 24}], 'ipv6': []})
    if field == 'configured_adapters':
        ha.internal_url = ha.urls['internal'] = 'http://192.168.10.20'
    else:
        ha.network['configured_adapters'] = ['end1']
        ha.network['adapters'][0]['enabled'] = True
    initial = {'configured_adapters': digest(ha.network['configured_adapters']), 'internal_url': digest(ha.internal_url)}
    original_request, original_ws = ha.request, ha.ws
    seen, cancelled = set(), []
    all_seen, sibling_waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    while_pending = []

    def observe(name):
        seen.add(name)
        if seen == {'configured_adapters', 'internal_url'}:
            all_seen.set()

    async def request(method, path, **kwargs):
        if path == '/network/info':
            await all_seen.wait()
            while_pending.append(store.setting('home_lan_configuration_attempt'))
            sibling_waiting.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(path)
                raise
        result = await original_request(method, path, **kwargs)
        if path == '/config':
            observe('internal_url')
        return result

    async def ws(command, **kwargs):
        result = await original_ws(command, **kwargs)
        if command == 'network':
            observe('configured_adapters')
        return result

    ha.request, ha.ws = request, ws
    if interruption == 'timeout':
        original_timeout = asyncio.timeout
        monkeypatch.setattr(asyncio, 'timeout', lambda delay: original_timeout(0.05 if delay == 30 else delay))
    async def check_host():
        pass
    task = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    fresh = None
    try:
        await asyncio.wait_for(sibling_waiting.wait(), 5)
        if interruption == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)['reason'] == 'home_network_observation_unavailable'
        assert cancelled == ['/network/info'] and ha.mutations == []
        record = store.setting('home_lan_configuration_attempt')
        assert not record['revoked'] and not record['conflicts']
        assert store.setting('home_lan_configuration_rollback') is None
        owner_value = ['end0'] if field == 'configured_adapters' else 'https://owner.invalid'
        if owner_edit:
            if field == 'configured_adapters':
                ha.network[field] = owner_value
            else:
                ha.internal_url = ha.urls['internal'] = owner_value
        ha.request, ha.ws = original_request, original_ws
        fresh = Store(store.path) if restart else None
        active = fresh or store
        second = await ha_lan.converge_home_lan(ha, active, attempt(), check_host)
        if owner_edit:
            assert ha.mutations == []
            assert (ha.network[field] if field == 'configured_adapters' else ha.internal_url) == owner_value
            assert second['reason'] == 'home_lan_owner_conflict'
            assert active.setting('home_lan_configuration_attempt')['conflicts'] == [field]
            assert (await ha_lan.converge_home_lan(ha, active, attempt(), check_host))['reason'] == 'home_lan_reauthorization_required'
            assert ha.mutations == [] and active.setting('home_lan_configuration_rollback') is None
        else:
            assert second['state'] == 'ready' and len(ha.mutations) == 1
            assert not active.setting('home_lan_configuration_attempt')['conflicts']
        assert record['initial_fields'] == while_pending[0]['initial_fields'] == initial
        assert 'expected' not in record
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if fresh:
            fresh.close()


@pytest.mark.parametrize('outcome', ['ready', 'invalid', 'transient'])
async def test_first_observation_unchanged_poll_does_not_write_baseline(network, store, monkeypatch, outcome):
    ha = ConvergingHA()
    async def check_host():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['state'] == 'ready'
    ha.mutations.clear()
    original_setting, original_durable, original_owner = store.set_setting, ha_lan.durable, ha.owner
    writes, flushes = [], []
    def setting(key, value):
        writes.append(key)
        return original_setting(key, value)
    async def durable(value):
        flushes.append(True)
        await original_durable(value)
    async def owner(value):
        result = await original_owner(value)
        if outcome == 'transient' and ha.owner_reads == 2:
            raise HAError('fixture owner read failed')
        return result
    ha.owner_reads = 0
    ha.owner = owner
    if outcome == 'invalid':
        ha.network['configured_adapters'] = [None]
    monkeypatch.setattr(store, 'set_setting', setting)
    monkeypatch.setattr(ha_lan, 'durable', durable)
    result = await ha_lan.converge_home_lan(ha, store, attempt(), check_host)
    assert result['reason'] == {'ready': 'guest_lan_and_core_configuration_observed', 'invalid': 'core_contract_invalid',
        'transient': 'home_network_observation_unavailable'}[outcome]
    assert ha.mutations == [] and writes == [] and flushes == []


@pytest.mark.parametrize('outcome', ['timeout', 'cancel', 'authority', 'barrier'])
async def test_first_observation_flush_failure_preserves_original_outcome(journal_store, monkeypatch, outcome):
    store = journal_store
    ha = ConvergingHA()
    original_request, original_durable = ha.request, ha_lan.durable
    waiting, release = asyncio.Event(), asyncio.Event()
    flushes = []
    async def request(method, path, **kwargs):
        if path == '/network/info':
            waiting.set()
            await release.wait()
            if outcome == 'authority':
                store.set_setting('hermes_web_epoch', 'changed')
        return await original_request(method, path, **kwargs)
    async def durable(value):
        record = store.setting('home_lan_configuration_attempt')
        if record and (record.get('initial_fields') or record['revoked']):
            flushes.append(True)
            if outcome == 'barrier' and len(flushes) == 1:
                raise OSError('fixture journal unavailable')
            raise RuntimeError('fixture journal flush failed')
        await original_durable(value)
    ha.request = request
    monkeypatch.setattr(ha_lan, 'durable', durable)
    if outcome == 'timeout':
        original_timeout = asyncio.timeout
        monkeypatch.setattr(asyncio, 'timeout', lambda delay: original_timeout(0.05 if delay == 30 else delay))
    async def check_host():
        pass
    task = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        if outcome == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if outcome != 'timeout':
                release.set()
            result = await task
            assert result['reason'] == ('home_authority_changed' if outcome == 'authority' else 'home_network_observation_unavailable')
        assert len(flushes) == (2 if outcome == 'barrier' else 1)
        assert ha.mutations == [] and store.setting('home_lan_configuration_rollback') is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('stage', ['partial_gather', 'barrier'])
async def test_first_observation_cancellation_waits_for_exit_flush(network, store, monkeypatch, stage):
    from agentic_controller.models import digest
    from agentic_controller.store import Store
    ha = ConvergingHA()
    original_request, original_durable = ha.request, ha_lan.durable
    waiting, flushing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    flush_tasks = []
    async def request(method, path, **kwargs):
        if path == '/network/info' and stage == 'partial_gather':
            waiting.set()
            await release.wait()
        return await original_request(method, path, **kwargs)
    async def durable(value):
        if store.setting('home_lan_configuration_attempt', {}).get('initial_fields'):
            flush_tasks.append(asyncio.current_task())
            if len(flush_tasks) == 1 and stage == 'barrier':
                waiting.set()
            else:
                flushing.set()
            await release.wait()
        await original_durable(value)
    ha.request = request
    monkeypatch.setattr(ha_lan, 'durable', durable)
    async def check_host():
        pass
    task = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        await asyncio.wait_for(flushing.wait(), 1)
        assert flush_tasks[-1] is task
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        disk = Store(store.path)
        try:
            assert disk.setting('home_lan_configuration_attempt')['initial_fields'] == {
                'configured_adapters': digest([]), 'internal_url': digest(None)}
        finally:
            disk.close()
        assert ha.mutations == [] and store.setting('home_lan_configuration_rollback') is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def journal_store(tmp_path):
    from agentic_controller.store import Store
    store = Store(tmp_path / 'journal.sqlite3')
    store.set_setting('owner', 'owner')
    store.set_setting('hermes_web_epoch', 'fixture-epoch')
    yield store
    if store.failed:
        with pytest.raises(RuntimeError, match='持久化失敗'):
            store.close()
    else:
        store.close()


@pytest.fixture
def held_lan_writer(monkeypatch):
    from agentic_controller import store_journal
    entered, release = threading.Event(), threading.Event()
    failure = []
    def hold():
        entered.set()
        assert release.wait(10), 'test did not release writer'
        if failure:
            raise OSError('fixture disk failure')
    def arm():
        monkeypatch.setattr(store_journal, 'before_disk_commit', hold)
    yield arm, entered, release, failure
    release.set()


@pytest.mark.parametrize('interruption', ['timeout', 'cancel', 'repeated_cancel'])
@pytest.mark.parametrize('batch', ['summary', 'exit'])
@pytest.mark.parametrize('disk_error', [False, True])
async def test_stalled_observation_writer_is_bounded_and_restart_protects_owner(
        journal_store, held_lan_writer, monkeypatch, caplog, interruption, batch, disk_error):
    from agentic_controller import store_journal
    from agentic_controller.store import Store
    store, ha = journal_store, ConvergingHA()
    arm, entered, release, failure = held_lan_writer
    waiting, finishing = asyncio.Event(), asyncio.Event()
    original_request, original_ws, original_finish = ha.request, ha.ws, ha_lan.finish_journal
    async def request(method, path, **kwargs):
        if path == '/network/info':
            waiting.set()
            await asyncio.Event().wait()
        return await original_request(method, path, **kwargs)
    async def ws(command, **kwargs):
        if command == 'network' and batch == 'summary':
            arm()
        return await original_ws(command, **kwargs)
    async def finish(*args, **kwargs):
        finishing.set()
        return await original_finish(*args, **kwargs)
    async def check_host():
        pass
    ha.request, ha.ws = request, ws
    monkeypatch.setattr(ha_lan, 'finish_journal', finish)
    finish_seconds, timeout = ha_lan.JOURNAL_FINISH_SECONDS, asyncio.timeout
    monkeypatch.setattr(ha_lan, 'JOURNAL_FINISH_SECONDS', .05)
    if interruption == 'timeout':
        monkeypatch.setattr(asyncio, 'timeout', lambda delay: timeout(.15 if delay == 30 else delay))
    task = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        if batch == 'exit':
            await store.durable()
            arm()
        else:
            assert await asyncio.to_thread(entered.wait, 2)
        if interruption != 'timeout':
            task.cancel()
        await asyncio.wait_for(finishing.wait(), 2)
        assert await asyncio.to_thread(entered.wait, 2)
        if interruption == 'repeated_cancel':
            task.cancel()
            task.cancel()
        # asyncio.wait does not cancel the subject on timeout: a hung finally
        # would fail this assertion while its actual writer remains stalled.
        done, _ = await asyncio.wait({task}, timeout=.5)
        assert done == {task} and not release.is_set()
        if interruption == 'timeout':
            assert task.result()['reason'] == 'home_network_observation_unavailable'
        else:
            with pytest.raises(asyncio.CancelledError):
                task.result()
        assert store.failed and ha.mutations == []
        assert 'Home LAN journal persistence failed' in caplog.text
        with pytest.raises(RuntimeError, match='持久化失敗'):
            store.set_setting('must-not-write', True)
        with pytest.raises(RuntimeError, match='持久化失敗'):
            await store.durable()
        # The owner changes the exact field after we returned. A late commit
        # must neither revive the failed Store nor turn this into a new baseline.
        ha.network['configured_adapters'] = ['owner0']
        if disk_error:
            failure.append(True)
        release.set()
        with pytest.raises(RuntimeError, match='持久化失敗'):
            await asyncio.to_thread(store.close)
        assert store.failed
        # Accelerated deadlines belong to the stalled operation, not healthy
        # recovery with a fresh Store and real disk persistence barriers.
        monkeypatch.setattr(asyncio, 'timeout', timeout)
        monkeypatch.setattr(ha_lan, 'JOURNAL_FINISH_SECONDS', finish_seconds)
        monkeypatch.setattr(store_journal, 'before_disk_commit', lambda: None)
        ha.request, ha.ws = original_request, original_ws
        fresh = Store(store.path)
        try:
            record = fresh.setting('home_lan_configuration_attempt')
            assert bool(record.get('observations_pending')) == (batch == 'summary' or disk_error)
            result = await ha_lan.converge_home_lan(ha, fresh, attempt(), check_host)
            assert result['reason'] == ('home_lan_reauthorization_required' if record.get('observations_pending')
                                        else 'home_lan_owner_conflict')
            assert ha.mutations == [] and ha.network['configured_adapters'] == ['owner0']
            # Storage recovery alone does not renew the interrupted authority;
            # an explicit new Host attempt can authorize a new observed baseline.
            result = await ha_lan.converge_home_lan(ha, fresh, {**attempt(), 'attempt_id': 'c' * 64}, check_host)
            assert result['state'] == 'ready'
        finally:
            fresh.close()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('fault', ['conflict', 'cancel', 'lost_reply'])
@pytest.mark.parametrize('cleanup', ['stall', 'blocked', 'error'])
async def test_failed_effect_finalization_retains_primary_outcome_and_recovery_fence(
        journal_store, held_lan_writer, monkeypatch, caplog, fault, cleanup):
    from agentic_controller import store_journal
    from agentic_controller.store import Store
    store, ha = journal_store, ConvergingHA()
    arm, entered, release, failure = held_lan_writer
    original_ws, original_finish = ha.ws, ha_lan.finish_journal
    async def finish(*args, **kwargs):
        # Persist the conflict first so the held batch is deterministic.
        await store.durable()
        arm()
        if cleanup == 'blocked':
            store.set_setting('fixture-blocker', True)
            assert await asyncio.to_thread(entered.wait, 2)
        if cleanup == 'error':
            failure.append(True)
            release.set()
        return await original_finish(*args, **kwargs)
    async def ws(command, **kwargs):
        if command != 'network/configure':
            return await original_ws(command, **kwargs)
        # The possible-effect record has already passed its real disk barrier.
        if fault == 'conflict':
            # The owner's value is deliberately exactly the planned target.
            # Stale durable ownership must never allow restoring [] over it.
            ha.network['configured_adapters'] = ['end1']
            return await original_ws(command, **kwargs)
        if fault == 'cancel':
            ha.network['configured_adapters'] = ['end1']
            raise asyncio.CancelledError()
        await original_ws(command, **kwargs)
        raise HAError('fixture reply lost')
    async def check_host():
        pass
    ha.ws = ws
    monkeypatch.setattr(ha_lan, 'finish_journal', finish)
    monkeypatch.setattr(ha_lan, 'JOURNAL_FINISH_SECONDS', .05)
    task = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        done, _ = await asyncio.wait({task}, timeout=.5)
        assert done == {task}
        if fault == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                task.result()
        else:
            assert task.result()['reason'] == ('home_lan_owner_conflict' if fault == 'conflict'
                                               else 'home_network_observation_unavailable')
        assert store.failed and 'Home LAN journal persistence failed' in caplog.text
        assert len(ha.mutations) == (1 if fault == 'lost_reply' else 0)
        release.set()
        with pytest.raises(RuntimeError, match='持久化失敗'):
            await asyncio.to_thread(store.close)
        monkeypatch.setattr(store_journal, 'before_disk_commit', lambda: None)
        ha.ws = original_ws
        monkeypatch.setattr(ha_lan, 'finish_journal', original_finish)
        fresh = Store(store.path)
        try:
            record = fresh.setting('home_lan_configuration_rollback')
            if cleanup != 'stall':
                assert record['pending_field'] == 'target_adapters'
                assert record['target_adapters'] == ['end1']
                for current in (attempt(), {**attempt(), 'attempt_id': 'c' * 64}):
                    result = await ha_lan.converge_home_lan(ha, fresh, current, check_host)
                    assert result['reason'] == 'home_lan_journal_recovery_required'
                with pytest.raises(ha_lan.ObservationInvalid, match='home_lan_journal_recovery_required'):
                    await ha_lan.compensate_home_lan(ha, fresh, {'mode': 'nat'}, check_host)
                assert fresh.setting('home_lan_configuration_rollback') == record
                fresh.set_setting('owner', 'another-owner')
                ha.base = 'http://192.168.177.3:80/api'
                for current in (attempt(), {'mode': 'nat', 'state': 'disabled'}):
                    observed = await ha_lan.observe_home_lan(ha, fresh, current)
                    assert observed['reason'] == 'home_lan_journal_recovery_required'
                assert fresh.setting('home_lan_configuration_rollback') == record
            else:
                # A late successful cleanup is safe: its entire transaction
                # includes either the proven unsent revocation or sent claim.
                assert bool(record) == (fault == 'lost_reply')
                if record:
                    assert not record.get('pending_field')
                await ha_lan.compensate_home_lan(ha, fresh, {'mode': 'nat'}, check_host)
            assert ha.network['configured_adapters'] == ([] if fault == 'lost_reply' and cleanup == 'stall' else ['end1'])
            assert len(ha.mutations) == (2 if fault == 'lost_reply' and cleanup == 'stall' else 1 if fault == 'lost_reply' else 0)
        finally:
            fresh.close()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stalled_reauthorization_flush_is_bounded(journal_store, held_lan_writer, monkeypatch):
    store, ha = journal_store, ConvergingHA()
    arm, entered, release, _ = held_lan_writer
    async def check_host():
        pass
    assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['state'] == 'ready'
    store.set_setting('hermes_web_epoch', 'logged-out')
    await store.durable()
    arm()
    monkeypatch.setattr(ha_lan, 'JOURNAL_FINISH_SECONDS', .05)
    task = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        done, _ = await asyncio.wait({task}, timeout=.5)
        assert done == {task} and task.result()['reason'] == 'home_lan_reauthorization_required'
        assert store.failed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_lan_watcher_shutdown_gather_finishes_while_writer_is_stalled(
        journal_store, held_lan_writer, monkeypatch):
    from agentic_controller import operations
    store, ha = journal_store, ConvergingHA()
    arm, entered, release, _ = held_lan_writer
    original_ws, original_request = ha.ws, ha.request
    async def broker(method, path, body=None, *, store):
        assert method == 'GET' and path == '/network/ha-lan'
        return {**host_contract(), **attempt()}
    async def ws(command, **kwargs):
        if command == 'network':
            arm()
        return await original_ws(command, **kwargs)
    async def request(method, path, **kwargs):
        if path == '/network/info':
            await asyncio.Event().wait()
        return await original_request(method, path, **kwargs)
    ha.ws, ha.request = ws, request
    monkeypatch.setattr(operations, 'broker_call', broker)
    monkeypatch.setattr(ha_lan, 'JOURNAL_FINISH_SECONDS', .05)
    watcher = asyncio.create_task(operations.maintain_home_lan(ha, store))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        watcher.cancel()
        shutdown = asyncio.gather(watcher, return_exceptions=True)
        done, _ = await asyncio.wait({shutdown}, timeout=.5)
        assert done == {shutdown} and isinstance(shutdown.result()[0], asyncio.CancelledError)
        assert store.failed and ha.mutations == [] and not release.is_set()
    finally:
        release.set()
        if not watcher.done():
            watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.parametrize('stage', ['observations', 'effect'])
@pytest.mark.parametrize('outcome', ['healthy', 'cancel', 'journal_failure'])
async def test_active_convergence_status_does_not_misreport_durable_pending_recovery(
        journal_store, stage, outcome):
    from agentic_controller.store import Store
    store, ha = journal_store, ConvergingHA()
    waiting, release, cancelling, cancel_release = (asyncio.Event() for _ in range(4))
    original_request, original_ws = ha.request, ha.ws
    async def pause():
        waiting.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Keep the exact transport task suspended during cancellation so
            # concurrent reads must not mistake a cancelling writer for progress.
            cancelling.set()
            await cancel_release.wait()
            raise
    async def request(method, path, **kwargs):
        if stage == 'observations' and path == '/network/info':
            await pause()
        return await original_request(method, path, **kwargs)
    async def ws(command, **kwargs):
        if stage == 'effect' and command == 'network/configure':
            await pause()
        return await original_ws(command, **kwargs)
    async def check_host():
        pass
    ha.request, ha.ws = request, ws
    operation = asyncio.create_task(ha_lan.converge_home_lan(ha, store, attempt(), check_host))
    expected_recovery = ('home_lan_reauthorization_required' if stage == 'observations'
                         else 'home_lan_journal_recovery_required')
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        await store.durable()
        before = list(store.db.execute('SELECT key, value FROM settings'))
        observed = await ha_lan.observe_home_lan(ha, store, attempt())
        assert observed['reason'] == 'home_lan_configuration_in_progress'
        assert list(store.db.execute('SELECT key, value FROM settings')) == before
        assert ha.mutations == []
        if stage == 'effect':
            # The hint cannot bypass any mutation/recovery gate, or disguise
            # the old operation when Host reports NAT or a different attempt.
            assert (await ha_lan.converge_home_lan(ha, store, attempt(), check_host))['reason'] == expected_recovery
            with pytest.raises(ha_lan.ObservationInvalid, match=expected_recovery):
                await ha_lan.compensate_home_lan(ha, store, {'mode': 'nat'}, check_host)
            for current in ({**attempt(), 'attempt_id': 'c' * 64}, {'mode': 'nat', 'state': 'disabled'}):
                assert (await ha_lan.observe_home_lan(ha, store, current))['reason'] == expected_recovery
            assert (await ha_lan.observe_home_lan(ha, store, attempt()))['reason'] == 'home_lan_configuration_in_progress'
        # A different Store, even in the same process, has no proof that its
        # persisted pending marker belongs to a currently running operation.
        reopened = Store(store.path)
        try:
            assert (await ha_lan.observe_home_lan(ha, reopened, attempt()))['reason'] == expected_recovery
        finally:
            reopened.close()
        if outcome == 'healthy':
            release.set()
            assert (await operation)['state'] == 'ready'
            assert (await ha_lan.observe_home_lan(ha, store, attempt()))['state'] == 'ready'
        else:
            if outcome == 'journal_failure':
                store.fail_journal(OSError('fixture journal failure'))
                assert (await ha_lan.observe_home_lan(ha, store, attempt()))['reason'] == expected_recovery
            operation.cancel()
            await asyncio.wait_for(cancelling.wait(), 2)
            assert (await ha_lan.observe_home_lan(ha, store, attempt()))['reason'] == expected_recovery
            cancel_release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert ha.mutations == []
        assert getattr(store, '_home_lan_operation', None) is None
    finally:
        release.set()
        cancel_release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
