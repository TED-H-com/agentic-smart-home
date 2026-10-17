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
    token, base, websocket, supervisor, external = "fixture-token", "fixture-core", "fixture-ws", "fixture-supervisor", True

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
