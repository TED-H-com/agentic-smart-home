"""Observe the enrolled HAOS LAN interface through authenticated read-only APIs.

Adapter v1 follows Core 2026.10.1 network/websocket.py and network/models.py,
and Supervisor 2026.10.0 /network/info (v1). Unknown fields are ignored;
missing required observations never establish readiness. No discovery or
network configuration is changed by observations. Only the fixed Host-enrolled
connection attempt may converge Core network configuration.
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .durability import durable
from .ha import HAError
from .models import digest, utcnow

OBSERVATION_SECONDS = 8
MAX_INTERFACES = 64
MAX_ADDRESSES = 32
MANAGEMENT_NETWORKS = (ipaddress.ip_network("192.168.177.0/24"), ipaddress.ip_network("172.30.32.0/23"))
LIMITS = ["read_only_network_observation", "no_multicast_delivery_test", "no_device_or_matter_control_verified",
          "callback_reachability_not_tested", "ipv6_link_local_not_a_callback_address"]
ReasonCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,95}$", max_length=96)]
HostInterfaceName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,32}$", max_length=32)]
InterfaceId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
GuestMac = Annotated[str, Field(pattern=r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$", max_length=17)]
AddressText = Annotated[str, Field(min_length=1, max_length=45)]


class HostLanInterface(BaseModel):
    """Project known Host fields while tolerating bounded additions upstream."""

    model_config = ConfigDict(extra="ignore", strict=True)

    id: InterfaceId
    label: Annotated[str, Field(min_length=1, max_length=96)]
    interface: HostInterfaceName
    address: AddressText | None
    network: Annotated[str, Field(min_length=1, max_length=49)] | None

    @model_validator(mode="after")
    def valid_addresses(self):
        if (self.address is None) != (self.network is None):
            raise ValueError("host_address_network_pair_required")
        if self.address is not None:
            value, network = ipaddress.IPv4Address(self.address), ipaddress.IPv4Network(self.network)
            if value not in network or value.is_loopback or value.is_multicast or value.is_unspecified:
                raise ValueError("host_address_network_invalid")
        return self


class HomeLanObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    adapter_version: Literal[1] = 1
    state: Literal["not_configured", "needs_home_setup", "unavailable", "waiting_for_address",
                   "adapter_selection_required", "callback_address_required", "ready"]
    reason: ReasonCode
    interface: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$", max_length=64)] | None = None
    ipv4: list[AddressText] = Field(default_factory=list, max_length=MAX_ADDRESSES)
    ipv6: list[AddressText] = Field(default_factory=list, max_length=MAX_ADDRESSES)
    connected: bool | None = None
    discovery_enabled: bool | None = None
    default_adapter: bool | None = None
    internal_url: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    observed_at: str = Field(default_factory=utcnow, min_length=20, max_length=40)
    limits: list[ReasonCode] = Field(default_factory=lambda: list(LIMITS), max_length=16)


class HostLanStatus(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    contract_version: Literal[1]
    mode: Literal["nat", "wired"]
    state: Literal["disabled", "configured", "unavailable", "needs_reconcile"]
    selected_interface_id: InterfaceId | None
    interface: HostInterfaceName | None
    guest_mac: GuestMac | None
    interfaces: Annotated[list[HostLanInterface], Field(max_length=32)]
    vm_state: Literal["running", "blocked", "paused", "in shutdown", "shut off", "crashed", "pmsuspended", "not_created", "unknown"]
    can_configure: bool
    limitations: Annotated[list[ReasonCode], Field(max_length=16)]
    attempt_id: InterfaceId | None = None
    home: HomeLanObservation | None = None
    ha_lan_ip: AddressText | None = None
    ha_internal_url: Annotated[str, Field(max_length=512)] | None = None
    message: Annotated[str, Field(max_length=256)] | None = None
    can_connect: bool = False
    core_configuration_pending: bool = False
    fallback_complete: bool = False
    recovery_pending: bool = False

    @field_validator("contract_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("host_lan_contract_version_invalid")
        return value

    @model_validator(mode="after")
    def consistent_state(self):
        if self.can_configure and self.vm_state not in {"shut off", "not_created"}:
            raise ValueError("host_vm_configuration_state_invalid")
        selected = (self.selected_interface_id, self.interface, self.guest_mac)
        if self.mode == "nat":
            if any(value is not None for value in selected) or self.state not in {"disabled", "needs_reconcile"}:
                raise ValueError("host_nat_state_invalid")
        elif any(value is None for value in selected) or self.state == "disabled":
            raise ValueError("host_wired_state_invalid")
        if self.fallback_complete and (self.mode != "nat" or self.state != "disabled" or self.vm_state != "running"):
            raise ValueError("host_fallback_state_invalid")
        if self.guest_mac is not None:
            mac_address(self.guest_mac)
        if (len({item.id for item in self.interfaces}) != len(self.interfaces)
                or len({item.interface for item in self.interfaces}) != len(self.interfaces)):
            raise ValueError("host_interface_identity_ambiguous")
        if self.state == "configured":
            matches = [item for item in self.interfaces if item.id == self.selected_interface_id]
            if len(matches) != 1 or matches[0].interface != self.interface:
                raise ValueError("host_selected_interface_invalid")
        return self




class HomeLanStatus(HostLanStatus):
    home: HomeLanObservation


class ObservationInvalid(ValueError):
    """Fixed reason code; raw network responses never escape this adapter."""


class SendGuardRejected(HAError):
    """Keep fixed guard reasons outside the transport's ValueError mapping."""


async def guarded_mutation(ha, command, guard, **fields):
    async def send_guard():
        try:
            await guard()
        except ObservationInvalid as error:
            raise SendGuardRejected(str(error)) from None
    try:
        return await ha.ws(command, _receive_timeout=OBSERVATION_SECONDS, _send_guard=send_guard, **fields)
    except SendGuardRejected as error:
        raise ObservationInvalid(str(error)) from None


def observation(state, reason, **fields):
    return HomeLanObservation(state=state, reason=reason, **fields).model_dump(mode="json")


def mac_address(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value):
        raise ObservationInvalid("guest_mac_invalid")
    result = value.lower()
    if int(result[:2], 16) & 1 or result == "00:00:00:00:00:00":
        raise ObservationInvalid("guest_mac_invalid")
    return result


def interface_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value):
        raise ObservationInvalid("interface_contract_invalid")
    return value


def rows(value, maximum, code):
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(row, dict) for row in value):
        raise ObservationInvalid(code)
    return value


def address(value, version, *, prefix):
    if not isinstance(value, str) or len(value) > 64 or "%" in value:
        raise ObservationInvalid("address_contract_invalid")
    try:
        if prefix:
            if "/" not in value:
                raise ValueError()
            configured = ipaddress.ip_interface(value)
            result = configured.ip
            if version == 4 and configured.network.prefixlen < 31 and result in {
                    configured.network.network_address, configured.network.broadcast_address}:
                raise ValueError()
        else:
            result = ipaddress.ip_address(value)
    except ValueError:
        raise ObservationInvalid("address_contract_invalid") from None
    if result.version != version or result.is_multicast or result.is_unspecified or result.is_loopback or result.is_reserved:
        raise ObservationInvalid("address_contract_invalid")
    if result.version == 4 and result == ipaddress.ip_address("255.255.255.255"):
        raise ObservationInvalid("address_contract_invalid")
    if result.version == 6 and result.ipv4_mapped is not None:
        raise ObservationInvalid("address_contract_invalid")
    # These are product management / Supervisor networks, not proof of LAN
    # participation even if a stale adapter happens to advertise their address.
    if any(result.version == network.version and result in network for network in MANAGEMENT_NETWORKS):
        return None
    if version == 4 and result.is_link_local:
        return None
    return str(result)


def supervisor_addresses(row, version):
    block = row.get("ipv" + str(version))
    if block is None and "ipv" + str(version) in row:
        return [], False
    if not isinstance(block, dict) or type(block.get("ready")) is not bool:
        raise ObservationInvalid("supervisor_contract_invalid")
    raw = block.get("address")
    if not isinstance(raw, list) or len(raw) > MAX_ADDRESSES:
        raise ObservationInvalid("address_contract_invalid")
    values = {value for item in raw if (value := address(item, version, prefix=True)) is not None}
    return sorted(values), block["ready"]


def core_addresses(row, version):
    raw = rows(row.get("ipv" + str(version)), MAX_ADDRESSES, "core_contract_invalid")
    return {value for item in raw if (value := address(item.get("address"), version, prefix=False)) is not None}


def usable(values):
    return {value for value in values if not ipaddress.ip_address(value).is_link_local}


def callback_url(raw, addresses):
    if not isinstance(raw, str) or not raw or len(raw) > 512 or any(ord(char) <= 32 for char in raw) or "\\" in raw:
        return None
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not parsed.hostname
                or "%" in parsed.hostname):
            return None
        host = ipaddress.ip_address(parsed.hostname)
        if str(host) not in addresses or host.is_link_local:
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        authority = f"[{host}]" if host.version == 6 else str(host)
        return parsed.scheme + "://" + authority + (":" + str(port) if port is not None else "")
    except ValueError:
        return None


def project_network(guest_mac, supervisor, core, urls):
    if not isinstance(supervisor, dict) or not isinstance(core, dict) or not isinstance(urls, dict):
        raise ObservationInvalid("network_contract_invalid")
    interfaces = rows(supervisor.get("interfaces"), MAX_INTERFACES, "supervisor_contract_invalid")
    adapters = rows(core.get("adapters"), MAX_INTERFACES, "core_contract_invalid")
    if "internal" not in urls or (urls["internal"] is not None and not isinstance(urls["internal"], str)):
        raise ObservationInvalid("callback_contract_invalid")
    # Match only the enrolled guest MAC. No physical NIC name or first-adapter
    # heuristic may accidentally identify HA's management interface as its LAN.
    matched = [row for row in interfaces if isinstance(row.get("mac"), str) and row["mac"].lower() == guest_mac]
    if len(matched) > 1:
        raise ObservationInvalid("guest_mac_ambiguous")
    if not matched:
        return observation("waiting_for_address", "guest_interface_not_observed")
    interface = matched[0]
    name = interface_name(interface.get("interface"))
    if type(interface.get("enabled")) is not bool or type(interface.get("connected")) is not bool:
        raise ObservationInvalid("supervisor_contract_invalid")
    ipv4, ready4 = supervisor_addresses(interface, 4)
    ipv6, ready6 = supervisor_addresses(interface, 6)
    fields = {"interface": name, "ipv4": ipv4, "ipv6": ipv6, "connected": interface["connected"]}
    lan_addresses = usable((ipv4 if ready4 else []) + (ipv6 if ready6 else []))
    if not interface["enabled"] or not interface["connected"] or not lan_addresses:
        return observation("waiting_for_address", "guest_lan_address_not_ready", **fields)
    matched = [row for row in adapters if row.get("name") == name]
    if len(matched) != 1:
        raise ObservationInvalid("core_adapter_missing_or_ambiguous")
    adapter = matched[0]
    if type(adapter.get("enabled")) is not bool or type(adapter.get("default")) is not bool:
        raise ObservationInvalid("core_contract_invalid")
    observed = core_addresses(adapter, 4) | core_addresses(adapter, 6)
    fields.update(discovery_enabled=adapter["enabled"], default_adapter=adapter["default"])
    if not adapter["enabled"]:
        return observation("adapter_selection_required", "core_discovery_adapter_disabled", **fields)
    common = lan_addresses & observed
    if not common:
        raise ObservationInvalid("core_supervisor_address_mismatch")
    internal = callback_url(urls["internal"], common)
    if internal is None:
        return observation("callback_address_required", "internal_url_not_on_observed_lan", **fields)
    return observation("ready", "guest_lan_and_core_configuration_observed", **fields, internal_url=internal)


async def observe_home_lan(ha, store, host_status):
    """Return an owner-checked, bounded observation; never persist or mutate HA."""
    return await _home_lan(ha, store, host_status)


def core_listener(ha):
    """Use only the Host's verified IP management endpoint; never a proxy."""
    raw = getattr(ha, "base", None)
    try:
        if (getattr(ha, "external", None) is not True or not isinstance(raw, str)
                or any(ord(char) <= 32 for char in raw) or "\\" in raw):
            raise ValueError()
        parsed = urlsplit(raw)
        host = ipaddress.ip_address(parsed.hostname)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not any(host in network for network in MANAGEMENT_NETWORKS)):
            raise ValueError()
        port = parsed.port if parsed.port is not None else 80 if parsed.scheme == "http" else 443
        if not 1 <= port <= 65535:
            raise ValueError()
        return parsed.scheme, port
    except (ValueError, TypeError):
        raise ObservationInvalid("core_listener_unverified") from None


def configured_adapters(core):
    if not isinstance(core, dict):
        raise ObservationInvalid("network_contract_invalid")
    configured = core.get("configured_adapters")
    if (not isinstance(configured, list) or len(configured) > MAX_INTERFACES
            or any(not isinstance(item, str) for item in configured)
            or len(set(configured)) != len(configured)):
        raise ObservationInvalid("core_contract_invalid")
    return [interface_name(item) for item in configured]


def core_internal_url(config):
    if not isinstance(config, dict) or "internal_url" not in config:
        raise ObservationInvalid("core_config_contract_invalid")
    return config["internal_url"]


def configuration_plan(guest_mac, supervisor, core, urls, *, listener):
    """Only MAC-matched, cross-checked addresses can become Core settings.

    Core 2026.10.1 network/configure uses config.configured_adapters;
    config/core/update accepts internal_url. Preserve other selected adapters.
    """
    observed = project_network(guest_mac, supervisor, core, urls)
    if observed["state"] not in {"ready", "adapter_selection_required", "callback_address_required"}:
        return observed, None, None
    name = observed["interface"]
    configured = configured_adapters(core)
    # Validate the entire target adapter and match Supervisor/Core addresses even
    # when discovery is currently disabled (the normal observer stops earlier).
    projected = copy.deepcopy(core)
    adapter = next(item for item in projected["adapters"] if item.get("name") == name)
    common = usable(observed["ipv4"] + observed["ipv6"]) & (core_addresses(adapter, 4) | core_addresses(adapter, 6))
    if not common:
        raise ObservationInvalid("core_supervisor_address_mismatch")
    preferred = sorted(common, key=lambda item: (ipaddress.ip_address(item).version, item))[0]
    authority = "[" + preferred + "]" if ":" in preferred else preferred
    scheme, port = listener
    internal = scheme + "://" + authority + (":" + str(port) if port != (80 if scheme == "http" else 443) else "")
    adapter["enabled"] = True
    verified = project_network(guest_mac, supervisor, projected, {"internal": internal})
    if verified["state"] != "ready":
        raise ObservationInvalid("core_contract_invalid")
    adapters = None
    if observed["discovery_enabled"] is not True:
        # An empty list means automatic selection; retain its enabled adapters.
        if not configured:
            configured = [interface_name(item.get("name")) for item in core["adapters"] if item.get("enabled") is True]
        adapters = list(dict.fromkeys([*configured, name]))
    return observed, adapters, internal if callback_url(urls["internal"], common) is None else None


async def converge_home_lan(ha, store, host_status, check_attempt):
    """Converge only the current fixed Host attempt, never a browser GET.

    No raw HA response or management credential crosses into Host. The caller
    must freshly recheck Host's enrolled attempt before every write/readback.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", host_status.get("attempt_id") or ""):
        return observation("unavailable", "home_lan_attempt_required")
    if host_status.get("core_configuration_pending") is not True:
        return await observe_home_lan(ha, store, host_status)
    return await _home_lan(ha, store, host_status, check_attempt=check_attempt)


async def _home_lan(ha, store, host_status, *, check_attempt=None):
    if not isinstance(host_status, dict):
        return observation("unavailable", "host_contract_invalid")
    if host_status.get("mode") == "nat" and host_status.get("state") == "disabled":
        return observation("not_configured", "wired_lan_not_configured")
    if host_status.get("mode") != "wired":
        return observation("unavailable", "host_contract_invalid")
    if host_status.get("state") != "configured":
        return observation("unavailable", "host_lan_not_configured")
    owner = store.setting("owner")
    epoch = store.setting("hermes_web_epoch")
    connection = tuple(getattr(ha, key, None) for key in ("token", "base", "websocket", "supervisor", "external"))
    authority = {"owner": owner, "epoch": epoch, "connection": digest(connection)}
    attempt_record = store.setting("home_lan_configuration_attempt")
    pending = host_status.get("core_configuration_pending") is True
    same_attempt = bool(attempt_record and attempt_record.get("attempt_id") == host_status.get("attempt_id"))
    if pending and same_attempt and (attempt_record.get("revoked") or attempt_record.get("conflicts")
            or any(attempt_record.get(key) != value for key, value in authority.items())):
        if check_attempt:
            store.set_setting("home_lan_configuration_attempt", {**attempt_record, "revoked": True})
            await durable(store)
        return observation("unavailable", "home_lan_reauthorization_required")
    if not owner:
        return observation("needs_home_setup", "home_owner_not_configured")

    attempt_dirty = False

    def fence(*, field=None):
        nonlocal attempt_dirty
        if check_attempt:
            if field:
                attempt_record["conflicts"] = sorted(set([*attempt_record["conflicts"], field]))
            else:
                attempt_record["revoked"] = True
            store.set_setting("home_lan_configuration_attempt", attempt_record)
            attempt_dirty = True

    def guard():
        if (store.setting("owner") != owner or store.setting("hermes_web_epoch") != epoch
                or tuple(getattr(ha, key, None) for key in ("token", "base", "websocket", "supervisor", "external")) != connection):
            fence()
            raise ObservationInvalid("home_authority_changed")

    async def check_owner():
        guard()
        current = await ha.owner(owner)
        guard()
        if (not isinstance(current, dict) or current.get("id") != owner
                or current.get("is_owner") is not True or current.get("is_active") is not True):
            fence()
            raise ObservationInvalid("home_owner_not_active")

    async def mutation_guard():
        await check_owner()
        await check_attempt()
        guard()

    async def initial_read(read, field, validate):
        nonlocal attempt_dirty
        result = await read
        if check_attempt:
            value = validate(result)
            initial = attempt_record.setdefault("initial_fields", {})
            if field not in initial:
                # Record without yielding: a pending sibling must not lose this read.
                initial[field] = digest(value)
                store.set_setting("home_lan_configuration_attempt", attempt_record)
                attempt_dirty = True
        return result

    async def flush_attempt():
        nonlocal attempt_dirty
        if attempt_dirty:
            await durable(store)
            attempt_dirty = False

    def check_initial_field(field, current, owned):
        if digest(current) != attempt_record["initial_fields"][field] and not owned:
            fence(field=field)
            raise ObservationInvalid("home_lan_owner_conflict")

    failed = False
    try:
        guest_mac = mac_address(host_status.get("guest_mac"))
        async with asyncio.timeout(30 if check_attempt else OBSERVATION_SECONDS):
            if check_attempt:
                if not same_attempt:
                    attempt_record = {"attempt_id": host_status["attempt_id"], **authority, "revoked": False, "conflicts": []}
                    store.set_setting("home_lan_configuration_attempt", attempt_record)
                    await durable(store)
                await check_attempt()
            await check_owner()
            results = await asyncio.gather(
                ha.request("GET", "/network/info", supervisor=True, timeout=OBSERVATION_SECONDS),
                initial_read(ha.ws("network", _receive_timeout=OBSERVATION_SECONDS), "configured_adapters", configured_adapters),
                ha.ws("network/url", _receive_timeout=OBSERVATION_SECONDS),
                *([initial_read(ha.request("GET", "/config", timeout=OBSERVATION_SECONDS), "internal_url", core_internal_url)] if check_attempt else []),
                return_exceptions=True)
            if check_attempt:
                guard()
                await flush_attempt()
                for result in (results[1], results[3]):
                    if isinstance(result, ObservationInvalid):
                        raise result
            await check_owner()
            for result in results:
                if isinstance(result, BaseException):
                    raise ObservationInvalid("home_network_observation_unavailable")
            observed = project_network(guest_mac, *results[:3])
            if check_attempt:
                observed, adapters, internal = configuration_plan(guest_mac, *results[:3], listener=core_listener(ha))
                previous = store.setting("home_lan_configuration_rollback")
                if previous and (previous["owner"] != owner or previous["connection"] != digest(connection)):
                    previous = None
                    store.set_setting("home_lan_configuration_rollback", None)
                own_adapters = bool(previous and previous["target_adapters"] is not None
                                    and previous["target_adapters"] == results[1]["configured_adapters"])
                own_url = bool(previous and previous["target_internal_url"] is not None
                               and previous["target_internal_url"] == results[3]["internal_url"])
                for field, target, current, owned in (
                        ("configured_adapters", adapters, results[1]["configured_adapters"], own_adapters),
                        ("internal_url", internal, results[3]["internal_url"], own_url)):
                    if target is not None:
                        check_initial_field(field, current, owned)
                if adapters is not None or internal is not None or (previous and previous.get("attempt_id") != host_status["attempt_id"]):
                    await ha.ensure_write_compatibility()
                    config = await ha.request("GET", "/config", timeout=OBSERVATION_SECONDS)
                    previous_internal = core_internal_url(config)
                    # Carry ownership across successful reconnects. An owner
                    # edit becomes the new baseline for that field; only an
                    # exact product value retains its original baseline.
                    own_url = bool(previous and previous["target_internal_url"] is not None
                                   and previous["target_internal_url"] == previous_internal)
                    if internal is not None:
                        check_initial_field("internal_url", previous_internal, own_url)
                    if previous_internal is not None and not safe_previous_url(previous_internal):
                        raise ObservationInvalid("previous_internal_url_invalid")
                    baseline = {
                        "attempt_id": host_status["attempt_id"], "owner": owner,
                        "connection": digest(connection),
                        "previous_adapters": previous["previous_adapters"] if own_adapters else results[1]["configured_adapters"],
                        "previous_internal_url": previous["previous_internal_url"] if own_url else previous_internal,
                        "target_adapters": previous["target_adapters"] if own_adapters else None,
                        "target_internal_url": previous["target_internal_url"] if own_url else None}
                    save_rollback(store, baseline)
                    await durable(store)
                for command, fields in ([('network/configure', {'config': {'configured_adapters': adapters}})] if adapters is not None else []) + (
                        [('config/core/update', {'internal_url': internal})] if internal is not None else []):
                    await mutation_guard()
                    field = "configured_adapters" if command == "network/configure" else "internal_url"
                    target_key = "target_adapters" if command == "network/configure" else "target_internal_url"
                    expected = copy.deepcopy(results[1][field] if command == "network/configure" else previous_internal)
                    sending = False

                    async def send_guard(command=command, field=field, expected=expected, baseline=baseline, target_key=target_key):
                        nonlocal sending
                        await mutation_guard()
                        # Transport waits must not turn a stale plan into ownership.
                        current = (await ha.ws("network", _receive_timeout=OBSERVATION_SECONDS) if command == "network/configure"
                                   else await ha.request("GET", "/config", timeout=OBSERVATION_SECONDS))
                        guard()
                        if not isinstance(current, dict) or field not in current:
                            raise ObservationInvalid("core_config_contract_invalid")
                        if current[field] != expected:
                            baseline[target_key] = None
                            fence(field=field)
                            raise ObservationInvalid("home_lan_owner_conflict")
                        sending = True

                    # Journal one possible effect before send; discard only when
                    # the guard proves no frame was sent. Lost replies stay owned.
                    save_rollback(store, {**baseline, target_key: adapters if command == "network/configure" else internal})
                    try:
                        await durable(store)
                        await guarded_mutation(ha, command, send_guard, **fields)
                    finally:
                        if not sending:
                            save_rollback(store, baseline)
                            await durable(store)
                    baseline = store.setting("home_lan_configuration_rollback")
                    await check_owner()
                if adapters is not None or internal is not None:
                    results = await asyncio.gather(
                        ha.request("GET", "/network/info", supervisor=True, timeout=OBSERVATION_SECONDS),
                        ha.ws("network", _receive_timeout=OBSERVATION_SECONDS),
                        ha.ws("network/url", _receive_timeout=OBSERVATION_SECONDS))
                    observed = project_network(guest_mac, *results)
                expected_internal = internal if internal is not None else previous["target_internal_url"] if previous else None
                if expected_internal is not None and (internal is not None or observed["internal_url"] == expected_internal):
                    config = await ha.request("GET", "/config", timeout=OBSERVATION_SECONDS)
                    if not isinstance(config, dict) or config.get("internal_url") != expected_internal:
                        raise ObservationInvalid("core_internal_url_readback_mismatch")
                await check_attempt()
                await check_owner()
            guard()
            return observed
    except ObservationInvalid as error:
        failed = True
        return observation("unavailable", str(error))
    except (HAError, TimeoutError, OSError, ValueError, TypeError, KeyError):
        failed = True
        return observation("unavailable", "home_network_observation_unavailable")
    except BaseException:
        failed = True
        raise
    finally:
        if attempt_dirty:
            flushing = asyncio.create_task(durable(store))
            try:
                # Finish interrupted observations even when the caller was cancelled.
                await asyncio.shield(flushing)
            except asyncio.CancelledError:
                while not flushing.done():
                    try:
                        await asyncio.shield(flushing)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not flushing.cancelled():
                    flushing.exception()
                raise
            except Exception:
                if not failed:
                    raise


def save_rollback(store, record):
    owned = record["target_adapters"] is not None or record["target_internal_url"] is not None
    store.set_setting("home_lan_configuration_rollback", record if owned else None)


def safe_previous_url(raw):
    """Bound old owner settings before journaling them; never retain credentials."""
    if not isinstance(raw, str) or len(raw) > 512 or any(ord(char) <= 32 for char in raw) or "\\" in raw:
        return False
    try:
        parsed = urlsplit(raw)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None
                and parsed.password is None and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


async def compensate_home_lan(ha, store, host_status, check_host=None):
    """Adopt the household baseline under fresh authority after Host uses NAT.

    A user's intervening edit or changed HA connection wins. No whole config,
    owner snapshot, or old Controller DB is restored over newer operations.
    """
    record = store.setting("home_lan_configuration_rollback")
    if not record:
        return
    connection = tuple(getattr(ha, key, None) for key in ("token", "base", "websocket", "supervisor", "external"))
    epoch = store.setting("hermes_web_epoch")
    if store.setting("owner") != record["owner"] or digest(connection) != record["connection"]:
        store.set_setting("home_lan_configuration_rollback", None)
        return
    if host_status.get("mode") == "wired" and host_status.get("attempt_id") == record["attempt_id"]:
        return
    if host_status.get("mode") != "nat":
        # A subsequent Host-authorized reconnect adopts this field ledger in
        # converge_home_lan; it must never be compensated over a new live LAN.
        return

    def local_guard():
        if (store.setting("owner") != record["owner"] or store.setting("hermes_web_epoch") != epoch
                or digest(tuple(getattr(ha, key, None) for key in ("token", "base", "websocket", "supervisor", "external"))) != record["connection"]):
            raise ObservationInvalid("home_authority_changed")

    async def guard():
        local_guard()
        owner = await ha.owner(record["owner"])
        local_guard()
        if not isinstance(owner, dict) or owner.get("id") != record["owner"] or owner.get("is_owner") is not True or owner.get("is_active") is not True:
            raise ObservationInvalid("home_owner_not_active")
        if check_host:
            await check_host()
        local_guard()

    class FieldOwnershipChanged(Exception):
        pass

    async def restore(command, target, **fields):
        async def send_guard():
            # Re-read ownership after transport waits; manual edits win.
            if command == "network/configure":
                current = await ha.ws("network", _receive_timeout=OBSERVATION_SECONDS)
                field = "configured_adapters"
            else:
                current = await ha.request("GET", "/config", timeout=OBSERVATION_SECONDS)
                field = "internal_url"
            await guard()
            if not isinstance(current, dict) or current.get(field) != target:
                raise FieldOwnershipChanged()
            local_guard()
        try:
            await guarded_mutation(ha, command, send_guard, **fields)
        except FieldOwnershipChanged:
            pass

    await guard()
    await ha.ensure_write_compatibility()
    network = await ha.ws("network", _receive_timeout=OBSERVATION_SECONDS)
    config = await ha.request("GET", "/config", timeout=OBSERVATION_SECONDS)
    if record["target_adapters"] is not None and network.get("configured_adapters") == record["target_adapters"]:
        await guard()
        await restore("network/configure", record["target_adapters"], config={"configured_adapters": record["previous_adapters"]})
    if record["target_internal_url"] is not None and config.get("internal_url") == record["target_internal_url"]:
        await guard()
        await restore("config/core/update", record["target_internal_url"], internal_url=record["previous_internal_url"])
    await guard()
    store.set_setting("home_lan_configuration_rollback", None)
    await durable(store)
