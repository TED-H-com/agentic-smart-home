"""Native proof inside the disposable installed Controller; never a real LAN.

Failure stdout is a fixed, bounded diagnostic schema. Successful callback URLs
remain private fixture transport inputs. Credentials stay on disposable /data.
"""
import asyncio
import ipaddress
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

HA = "http://192.168.177.2:80"
WEB = "http://localhost:8099"
PRIVATE = Path("/data/test-ha-lan-identity.json")
PROBE_PHASES = frozenset({"onboard", "wired", "nat"})
CHECK_NAMES = frozenset({
    "owner_not_created", "owner_create_api", "owner_auth_code", "owner_token_api", "owner_token",
    "owner_core_config_api", "owner_analytics_api", "owner_integration_api", "owner_long_token_api",
    "owner_long_token", "owner_identity_write", "owner_identity_read", "owner_connect_api",
    "product_access_key", "product_login_api", "product_session_api", "product_session_csrf",
    "product_status_api", "product_status_shape", "product_host_shape", "native_network_api",
    "native_network_shape", "native_supervisor_api", "supervisor_shape", "supervisor_interfaces",
    "management_interface", "management_ipv4_addresses", "management_ipv4", "management_gateway_field",
    "host_nat_mode", "nat_gateway_restored", "native_adapters", "native_adapter", "native_adapter_default",
    "native_adapter_enabled", "lan_nic_removed", "host_wired_mode", "guest_mac", "guest_interface",
    "guest_ipv4_addresses", "guest_ipv6_addresses", "guest_connected", "dhcp_ipv4", "ipv6_ra",
    "lan_default_gateway", "management_gateway_absent", "native_adapter_auto", "native_discovery_api",
    "native_discovery_rows", "native_mdns_flow", "native_urls_api", "native_urls_shape",
    "native_callback_address", "product_observation_ready",
    "product_state_known", "product_state_not_configured", "product_state_needs_home_setup",
    "product_state_unavailable", "product_state_waiting_for_address", "product_state_adapter_selection_required",
    "product_state_callback_address_required", "product_state_ready",
})
ERROR_CODES = frozenset({name + "_failed" for name in CHECK_NAMES} | {
    "probe_deadline_exceeded", "probe_internal_error", "probe_phase_invalid",
})


def validate_failure(value):
    """Project only the exact safe wire schema; callers also bind its phase."""
    if (not isinstance(value, dict)
            or set(value) != {"kind", "status", "phase", "error_code", "checks"}
            or value.get("kind") != "installed_ha_lan_probe" or value.get("status") != "failed"
            or not isinstance(value.get("phase"), str) or value["phase"] not in PROBE_PHASES
            or not isinstance(value.get("error_code"), str) or value["error_code"] not in ERROR_CODES):
        return None
    checks = value.get("checks")
    if (not isinstance(checks, dict) or len(checks) > len(CHECK_NAMES)
            or any(not isinstance(key, str) or key not in CHECK_NAMES or type(item) is not bool
                   for key, item in checks.items())):
        return None
    return {"kind": "installed_ha_lan_probe", "status": "failed", "phase": value["phase"],
            "error_code": value["error_code"], "checks": dict(checks)}


class ProofFailure(AssertionError):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "probe_internal_error"
        super().__init__(self.code)


class Proof:
    def __init__(self):
        self.checks = {}

    def note(self, name, value):
        if name not in CHECK_NAMES or type(value) is not bool:
            raise ProofFailure("probe_internal_error")
        self.checks[name] = value

    def require(self, name, value):
        self.note(name, bool(value))
        if not value:
            raise ProofFailure(name + "_failed")

    def read(self, name, callback):
        self.note(name, False)
        try:
            value = callback()
        except Exception:
            raise ProofFailure(name + "_failed") from None
        self.note(name, True)
        return value

    async def call(self, name, operation):
        self.note(name, False)
        try:
            value = await operation
        except Exception:
            raise ProofFailure(name + "_failed") from None
        self.note(name, True)
        return value

    def failure(self, phase, code):
        return {"kind": "installed_ha_lan_probe", "status": "failed",
                "phase": phase if phase in PROBE_PHASES else None,
                "error_code": code if code in ERROR_CODES else "probe_internal_error", "checks": dict(self.checks)}


def mapping(proof, name, value):
    proof.require(name, isinstance(value, dict))
    return value


def rows(proof, name, value):
    proof.require(name, isinstance(value, list) and len(value) <= 128 and all(isinstance(row, dict) for row in value))
    return value


def matching(proof, name, values, predicate):
    matches = [row for row in values if predicate(row)]
    proof.require(name, len(matches) == 1)
    return matches[0]


def addresses(proof, name, row, version):
    block = row.get("ipv" + str(version))
    proof.require(name, isinstance(block, dict) and isinstance(block.get("address"), list)
                  and len(block["address"]) <= 128 and all(isinstance(value, str) for value in block["address"]))
    parsed = proof.read(name, lambda: [ipaddress.ip_interface(value) for value in block["address"]])
    proof.require(name, all(value.version == version for value in parsed))
    return [str(value.ip) for value in parsed]


def callback_address_matches(url, ipv4):
    """Recognize a fixture callback target; actual peer reachability is separate."""
    if (not isinstance(url, str) or not url or len(url) > 512
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in ("\\", "?", "#"))
            or not isinstance(ipv4, (list, tuple, set, frozenset)) or len(ipv4) > 128
            or any(not isinstance(value, str) for value in ipv4)):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or host not in ipv4 or ipaddress.IPv4Address(host) not in ipaddress.IPv4Network("198.18.42.0/24")
                or re.fullmatch(re.escape(host) + r"(?::[0-9]+)?", parsed.netloc) is None):
            return False
        port = parsed.port
        return port is None or 1 <= port <= 65535
    except (ValueError, TypeError):
        return False


async def request(client, method, url, **kwargs):
    async with client.request(method, url, **kwargs) as response:
        if response.status != 200:
            raise AssertionError("fixture_http_status")
        return await response.json()


async def ws(client, token, command, **fields):
    async with client.ws_connect(HA.replace("http:", "ws:") + "/api/websocket", max_msg_size=1024 * 1024) as connection:
        assert (await connection.receive_json(timeout=20))["type"] == "auth_required"
        await connection.send_json({"type": "auth", "access_token": token})
        assert (await connection.receive_json(timeout=20))["type"] == "auth_ok"
        await connection.send_json({"id": 1, "type": command, **fields})
        result = await connection.receive_json(timeout=20)
        assert result.get("id") == 1 and result.get("success") is True
        return result["result"]


async def controller(client, proof=None):
    proof = proof or Proof()
    key = proof.read("product_access_key", lambda: Path("/data/access-key").read_text().strip())
    await proof.call("product_login_api", request(client, "POST", WEB + "/api/host/login", json={"key": key}, headers={"Origin": WEB}))
    session = await proof.call("product_session_api", request(client, "GET", WEB + "/api/session"))
    proof.require("product_session_csrf", isinstance(session, dict) and isinstance(session.get("csrf"), str) and bool(session["csrf"]))
    return {"Origin": WEB, "X-Agentic-CSRF": session["csrf"]}


async def onboard(client, proof=None):
    proof = proof or Proof()
    proof.require("owner_not_created", not PRIVATE.exists())
    data = await proof.call("owner_create_api", request(client, "POST", HA + "/api/onboarding/users", json={"client_id": HA + "/",
                         "name": "Disposable LAN Owner", "username": "lan-owner", "password": secrets.token_urlsafe(32), "language": "en"}))
    proof.require("owner_auth_code", isinstance(data, dict) and isinstance(data.get("auth_code"), str) and bool(data["auth_code"]))
    data = await proof.call("owner_token_api", request(client, "POST", HA + "/auth/token", data={"grant_type": "authorization_code",
                          "code": data["auth_code"], "client_id": HA + "/"}))
    proof.require("owner_token", isinstance(data, dict) and isinstance(data.get("access_token"), str) and bool(data["access_token"]))
    token = data["access_token"]
    for step in ("core_config", "analytics", "integration"):
        fields = {"client_id": HA + "/", "redirect_uri": HA + "/"} if step == "integration" else {}
        await proof.call("owner_" + step + "_api", request(client, "POST", HA + "/api/onboarding/" + step,
                         json=fields, headers={"Authorization": "Bearer " + token}))
    token = await proof.call("owner_long_token_api", ws(client, token, "auth/long_lived_access_token", client_name="Disposable wired LAN test", lifespan=1))
    proof.require("owner_long_token", isinstance(token, str) and bool(token))
    def save():
        with PRIVATE.open("w") as output:
            os.chmod(PRIVATE, 0o600)
            json.dump({"token": token}, output)
    proof.read("owner_identity_write", save)
    headers = await controller(client, proof)
    await proof.call("owner_connect_api", request(client, "POST", WEB + "/api/host/connect", json={"token": token}, headers=headers))
    return {"native_owner_fixture": True, "product_owner_connection": True}


async def observe(client, wired, proof=None):
    proof = proof or Proof()
    proof.checks.clear()  # Never merge a preceding attempt's evidence into this one.
    token = proof.read("owner_identity_read", lambda: json.loads(PRIVATE.read_text())["token"])
    proof.require("owner_identity_read", isinstance(token, str) and bool(token))
    await controller(client, proof)
    status = mapping(proof, "product_status_shape", await proof.call("product_status_api", request(client, "GET", WEB + "/api/network/ha-lan")))
    host = mapping(proof, "product_host_shape", status.get("host", status))
    observed = status.get("observation") or status.get("home") or status.get("guest")
    state = observed.get("state") if isinstance(observed, dict) else None
    states = ("not_configured", "needs_home_setup", "unavailable", "waiting_for_address", "adapter_selection_required", "callback_address_required", "ready")
    proof.note("product_state_known", isinstance(state, str) and state in states)
    for known in states:
        proof.note("product_state_" + known, state == known)
    core = mapping(proof, "native_network_shape", await proof.call("native_network_api", ws(client, token, "network")))
    supervisor = mapping(proof, "supervisor_shape", await proof.call("native_supervisor_api", ws(client, token, "supervisor/api", endpoint="/network/info", method="get", data={})))
    # Core's Supervisor proxy returns the API data directly; tolerate its wrapper.
    if "data" in supervisor and "interfaces" not in supervisor:
        supervisor = mapping(proof, "supervisor_shape", supervisor["data"])
    interfaces = rows(proof, "supervisor_interfaces", supervisor.get("interfaces"))
    management = matching(proof, "management_interface", interfaces, lambda row: str(row.get("mac", "")).lower() == "52:54:00:aa:17:02")
    management4 = addresses(proof, "management_ipv4_addresses", management, 4)
    proof.require("management_ipv4", "192.168.177.2" in management4)
    proof.require("management_gateway_field", "gateway" in management["ipv4"])
    adapters = rows(proof, "native_adapters", core.get("adapters"))
    if not wired:
        proof.require("host_nat_mode", host.get("state") == "disabled" and host.get("mode") == "nat")
        proof.require("nat_gateway_restored", management["ipv4"]["gateway"] == "192.168.177.1")
        adapter = matching(proof, "native_adapter", adapters, lambda row: isinstance(management.get("interface"), str) and row.get("name") == management["interface"])
        proof.require("native_adapter_default", adapter.get("default") is True)
        proof.require("native_adapter_enabled", adapter.get("enabled") is True)
        # Disabled IPv4 may be null after removal; malformed address rows fail.
        lan_removed = True
        for row in interfaces:
            block = row.get("ipv4")
            if block is None:
                continue
            values = addresses(proof, "guest_ipv4_addresses", row, 4)
            lan_removed = lan_removed and not any(value.startswith("198.18.42.") for value in values)
        proof.require("lan_nic_removed", lan_removed)
        return {"management_reachable": True, "nat_gateway_restored": True, "lan_nic_removed": True}
    proof.require("host_wired_mode", host.get("state") == "configured" and host.get("mode") == "wired")
    proof.require("guest_mac", isinstance(host.get("guest_mac"), str) and bool(host["guest_mac"]))
    guest = matching(proof, "guest_interface", interfaces, lambda row: str(row.get("mac", "")).lower() == host["guest_mac"])
    ipv4 = addresses(proof, "guest_ipv4_addresses", guest, 4)
    ipv6 = addresses(proof, "guest_ipv6_addresses", guest, 6)
    proof.require("guest_connected", guest.get("connected") is True)
    proof.require("dhcp_ipv4", any(value.startswith("198.18.42.") for value in ipv4))
    proof.require("ipv6_ra", any(value.startswith("fd42:177:42:") for value in ipv6))
    proof.require("lan_default_gateway", guest["ipv4"].get("gateway") == "198.18.42.1")
    proof.require("management_gateway_absent", management["ipv4"]["gateway"] in (None, "", "0.0.0.0"))
    adapter = matching(proof, "native_adapter", adapters, lambda row: isinstance(guest.get("interface"), str) and row.get("name") == guest["interface"])
    proof.require("native_adapter_default", adapter.get("default") is True)
    proof.require("native_adapter_enabled", adapter.get("enabled") is True)
    proof.require("native_adapter_auto", core.get("configured_adapters") == [])
    flows = rows(proof, "native_discovery_rows", await proof.call("native_discovery_api", ws(client, token, "config_entries/flow/progress")))
    proof.require("native_discovery_rows", all(isinstance(row.get("context", {}), dict) for row in flows))
    proof.require("native_mdns_flow", any(row.get("handler") == "homekit_controller" and row.get("context", {}).get("source") == "zeroconf"
                  and "Agentic LAN Fixture" in json.dumps(row.get("context", {})) for row in flows))
    urls = mapping(proof, "native_urls_shape", await proof.call("native_urls_api", ws(client, token, "network/url")))
    internal = urls.get("internal")
    proof.require("native_callback_address", callback_address_matches(internal, ipv4))
    proof.require("product_observation_ready", isinstance(observed, dict) and observed.get("state") == "ready")
    return {"management_reachable": True, "dhcp_ipv4": True, "ipv6_ra": True, "lan_default_gateway": True,
            "management_gateway_absent": True, "native_discovery_adapter_auto": True,
            "native_mdns_flow_observed": True, "product_observation_ready": True, "callback_url": internal}


async def main(phase, proof):
    timeout = aiohttp.ClientTimeout(total=120 if phase == "onboard" else 30)
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        if phase == "onboard":
            return await onboard(client, proof)  # Effects are never retried.
        for attempt in range(120):
            try:
                return await observe(client, phase == "wired", proof)
            except ProofFailure:
                if attempt == 119:
                    raise
                await asyncio.sleep(5)


async def bounded(phase, proof):
    async with asyncio.timeout(720):
        return await main(phase, proof)


def execute():
    phase = os.environ.get("AGENTIC_LAN_PROBE_PHASE")
    proof = Proof()
    try:
        if phase not in PROBE_PHASES:
            raise ProofFailure("probe_phase_invalid")
        result = asyncio.run(bounded(phase, proof))
    except ProofFailure as error:
        result = proof.failure(phase, error.code)
    except TimeoutError:
        result = proof.failure(phase, "probe_deadline_exceeded")
    except Exception:
        result = proof.failure(phase, "probe_internal_error")
    print(json.dumps(result))
    return 1 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(execute())
