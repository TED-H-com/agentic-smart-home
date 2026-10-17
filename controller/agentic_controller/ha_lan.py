"""Observe the enrolled HAOS LAN interface through authenticated read-only APIs.

Adapter v1 follows Core 2026.10.1 network/websocket.py and network/models.py,
and Supervisor 2026.10.0 /network/info (v1). Unknown fields are ignored;
missing required observations never establish readiness. No discovery or
network configuration is changed here.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ha import HAError
from .models import utcnow

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


class HomeLanStatus(HostLanStatus):
    home: HomeLanObservation


class ObservationInvalid(ValueError):
    """Fixed reason code; raw network responses never escape this adapter."""


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
    if not isinstance(host_status, dict):
        return observation("unavailable", "host_contract_invalid")
    if host_status.get("mode") == "nat" and host_status.get("state") == "disabled":
        return observation("not_configured", "wired_lan_not_configured")
    if host_status.get("mode") != "wired":
        return observation("unavailable", "host_contract_invalid")
    if host_status.get("state") != "configured":
        return observation("unavailable", "host_lan_not_configured")
    owner = store.setting("owner")
    if not owner:
        return observation("needs_home_setup", "home_owner_not_configured")
    epoch = store.setting("hermes_web_epoch")
    connection = tuple(getattr(ha, key, None) for key in ("token", "base", "websocket", "supervisor", "external"))

    def guard():
        if (store.setting("owner") != owner or store.setting("hermes_web_epoch") != epoch
                or tuple(getattr(ha, key, None) for key in ("token", "base", "websocket", "supervisor", "external")) != connection):
            raise ObservationInvalid("home_authority_changed")

    async def check_owner():
        guard()
        current = await ha.owner(owner)
        guard()
        if (not isinstance(current, dict) or current.get("id") != owner
                or current.get("is_owner") is not True or current.get("is_active") is not True):
            raise ObservationInvalid("home_owner_not_active")

    try:
        guest_mac = mac_address(host_status.get("guest_mac"))
        async with asyncio.timeout(OBSERVATION_SECONDS):
            await check_owner()
            results = await asyncio.gather(
                ha.request("GET", "/network/info", supervisor=True, timeout=OBSERVATION_SECONDS),
                ha.ws("network", _receive_timeout=OBSERVATION_SECONDS),
                ha.ws("network/url", _receive_timeout=OBSERVATION_SECONDS), return_exceptions=True)
            await check_owner()
            for result in results:
                if isinstance(result, BaseException):
                    raise ObservationInvalid("home_network_observation_unavailable")
            observed = project_network(guest_mac, *results)
            guard()
            return observed
    except ObservationInvalid as error:
        return observation("unavailable", str(error))
    except (HAError, TimeoutError, OSError, ValueError, TypeError, KeyError):
        return observation("unavailable", "home_network_observation_unavailable")
