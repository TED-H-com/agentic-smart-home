"""Read-only LAN observations, with no real network, CLI or household mutations."""
import asyncio
import copy
import json

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
        if path == '/config' and ha.mutations:
            return {'internal_url': 'http://192.168.10.20:9999'}
        return await original(method, path, **kwargs)
    ha.request = different_stored_value
    async def guard():
        pass
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


@pytest.mark.parametrize('operation', ['converge', 'compensate'])
@pytest.mark.parametrize('command', ['network/configure', 'config/core/update'])
@pytest.mark.parametrize('barrier', ['connect', 'durable'])
@pytest.mark.parametrize('fault', ['epoch', 'owner', 'attempt', 'host_mode', 'active_owner', 'connection'])
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
        return {'reason': 'unexpected_success'}
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
        else:
            ha.token = 'changed-fixture'
        release.set()
        result = await asyncio.wait_for(task, 5)
        expected = ('home_lan_attempt_changed' if fault in {'attempt', 'host_mode'} else
                    'home_owner_not_active' if fault == 'active_owner' else 'home_authority_changed')
        assert result['reason'] == expected
        assert mutations == []
        assert store.setting('home_lan_configuration_rollback')
        if fault == 'epoch':
            await ha_lan.compensate_home_lan(ha, store, attempt(), check_host)
            assert store.setting('home_lan_configuration_rollback')
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
