"""Executed only inside the disposable installed Controller container.

Credentials stay on its disposable /data disk; stdout contains fixed checks and
fixture addresses only. This never calls a model/provider or discovers a real LAN.
"""
import asyncio
import ipaddress
import json
import os
import secrets
from pathlib import Path

import aiohttp

HA = "http://192.168.177.2:80"
WEB = "http://localhost:8099"
PRIVATE = Path("/data/test-ha-lan-identity.json")


async def request(client, method, url, **kwargs):
    async with client.request(method, url, **kwargs) as response:
        if response.status != 200:
            raise AssertionError("fixture_http_status_" + str(response.status))
        return await response.json()


async def ws(client, token, command, **fields):
    async with client.ws_connect(HA.replace("http:", "ws:") + "/api/websocket", max_msg_size=1024 * 1024) as connection:
        assert (await connection.receive_json(timeout=20))["type"] == "auth_required"
        await connection.send_json({"type": "auth", "access_token": token})
        assert (await connection.receive_json(timeout=20))["type"] == "auth_ok"
        await connection.send_json({"id": 1, "type": command, **fields})
        result = await connection.receive_json(timeout=20)
        assert result.get("id") == 1 and result.get("success") is True, "native_api_failed"
        return result["result"]


async def controller(client):
    await request(client, "POST", WEB + "/api/host/login", json={"key": Path("/data/access-key").read_text().strip()},
                  headers={"Origin": WEB})
    session = await request(client, "GET", WEB + "/api/session")
    return {"Origin": WEB, "X-Agentic-CSRF": session["csrf"]}


async def onboard(client):
    assert not PRIVATE.exists(), "fixture_owner_already_created"
    data = await request(client, "POST", HA + "/api/onboarding/users", json={"client_id": HA + "/",
                         "name": "Disposable LAN Owner", "username": "lan-owner", "password": secrets.token_urlsafe(32), "language": "en"})
    token = (await request(client, "POST", HA + "/auth/token", data={"grant_type": "authorization_code",
                          "code": data["auth_code"], "client_id": HA + "/"}))["access_token"]
    for step in ("core_config", "analytics", "integration"):
        fields = {"client_id": HA + "/", "redirect_uri": HA + "/"} if step == "integration" else {}
        await request(client, "POST", HA + "/api/onboarding/" + step, json=fields, headers={"Authorization": "Bearer " + token})
    token = await ws(client, token, "auth/long_lived_access_token", client_name="Disposable wired LAN test", lifespan=1)
    with PRIVATE.open("w") as output:
        os.chmod(PRIVATE, 0o600)
        json.dump({"token": token}, output)
    headers = await controller(client)
    await request(client, "POST", WEB + "/api/host/connect", json={"token": token}, headers=headers)
    return {"native_owner_fixture": True, "product_owner_connection": True}


async def observe(client, wired):
    token = json.loads(PRIVATE.read_text())["token"]
    await controller(client)
    status = await request(client, "GET", WEB + "/api/network/ha-lan")
    host = status.get("host", status)
    core = await ws(client, token, "network")
    supervisor = await ws(client, token, "supervisor/api", endpoint="/network/info", method="get", data={})
    # Core's Supervisor proxy returns the API data directly.
    if "data" in supervisor and "interfaces" not in supervisor:
        supervisor = supervisor["data"]
    management = next(row for row in supervisor["interfaces"] if str(row.get("mac", "")).lower() == "52:54:00:aa:17:02")
    assert any(value.split("/")[0] == "192.168.177.2" for value in management["ipv4"]["address"])
    if not wired:
        assert host["state"] == "disabled" and host["mode"] == "nat"
        assert management["ipv4"]["gateway"] == "192.168.177.1"
        adapter = next(row for row in core["adapters"] if row["name"] == management["interface"])
        assert adapter["default"] is True and adapter["enabled"] is True
        assert not any(any(str(address).startswith("198.18.42.") for address in (row.get("ipv4") or {}).get("address", []))
                       for row in supervisor["interfaces"])
        return {"management_reachable": True, "nat_gateway_restored": True, "lan_nic_removed": True}
    assert host["state"] == "configured" and host["mode"] == "wired"
    guest = next(row for row in supervisor["interfaces"] if str(row.get("mac", "")).lower() == host["guest_mac"])
    ipv4 = [str(ipaddress.ip_interface(value).ip) for value in guest["ipv4"]["address"]]
    ipv6 = [str(ipaddress.ip_interface(value).ip) for value in guest["ipv6"]["address"]]
    assert guest["connected"] is True and any(value.startswith("198.18.42.") for value in ipv4)
    assert any(value.startswith("fd42:177:42:") for value in ipv6)
    assert guest["ipv4"]["gateway"] == "198.18.42.1"
    assert management["ipv4"]["gateway"] in {None, "", "0.0.0.0"}
    adapter = next(row for row in core["adapters"] if row["name"] == guest["interface"])
    assert adapter["default"] is True and adapter["enabled"] is True
    assert core["configured_adapters"] == [], "test_must_not_manually_select_core_adapter"
    flows = await ws(client, token, "config_entries/flow/progress")
    assert any(row.get("handler") == "homekit_controller" and row.get("context", {}).get("source") == "zeroconf"
               and "Agentic LAN Fixture" in json.dumps(row.get("context", {})) for row in flows), "native_multicast_discovery_not_observed"
    urls = await ws(client, token, "network/url")
    assert any(urls.get("internal", "").startswith("http://" + value + ":") for value in ipv4)
    # Product observation is separate from this actual synthetic multicast proof.
    observed = status.get("observation") or status.get("home") or status.get("guest")
    assert observed and observed["state"] == "ready", "product_observation_not_ready"
    return {"management_reachable": True, "dhcp_ipv4": True, "ipv6_ra": True, "lan_default_gateway": True,
            "management_gateway_absent": True, "native_discovery_adapter_auto": True,
            "native_mdns_flow_observed": True, "product_observation_ready": True, "callback_url": urls["internal"]}


async def main():
    phase = os.environ["AGENTIC_LAN_PROBE_PHASE"]
    assert phase in {"onboard", "wired", "nat"}
    timeout = aiohttp.ClientTimeout(total=120 if phase == "onboard" else 30)
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        if phase == "onboard":
            result = await onboard(client)
        else:
            for attempt in range(120):
                try:
                    result = await observe(client, phase == "wired")
                    break
                except (AssertionError, aiohttp.ClientError, TimeoutError, KeyError, StopIteration):
                    if attempt == 119:
                        raise
                    await asyncio.sleep(5)
        print(json.dumps(result))


async def bounded():
    async with asyncio.timeout(720):
        await main()


if __name__ == "__main__":
    asyncio.run(bounded())
