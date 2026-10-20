"""Synthetic SHSW-1 HTTP/CoIoT responder; native Shelly/aioshelly stay unmodified.

Only container loopback is served. Every request is journaled across Core
restarts; all non-read operations are rejected. The relay state is permanently
off. Synthetic Zeroconf injection replaces observation only, never validation,
native configuration flow, coordinator, entity setup or registry creation.

Protocol reviewed against aioshelly 13.34.1 block_device/device.py and coap.py.
This hand-authored fixture is not a capture from a physical household device.
"""
import asyncio
import json
import struct
from collections import Counter
from ipaddress import IPv4Address
from pathlib import Path

from aiohttp import web
from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

DOMAIN = "agentic_shelly_fixture"
MAC = "AABBCC112233"
MODEL = "SHSW-1"
HOSTNAME = "shelly1-" + MAC.lower()
NAME = "Synthetic Shelly 1"
COUNTER = "sensor.agentic_shelly_fixture_requests"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
SHELLY = {"type": MODEL, "mac": MAC, "auth": False,
          "fw": "20230913-114008/v1.14.0-gcb84623", "longid": 1, "num_outputs": 1}
SETTINGS = {"device": {"type": MODEL, "mac": MAC, "hostname": HOSTNAME, "num_outputs": 1},
            "name": NAME, "coiot": {"enabled": True, "update_period": 15, "peer": ""},
            "relays": [{"name": "", "btn_type": "toggle", "appliance_type": "General",
                        "default_state": "off", "auto_on": 0, "auto_off": 0}],
            "fw": SHELLY["fw"], "mode": "relay", "timezone": "UTC"}
STATUS = {"wifi_sta": {"connected": True, "ssid": "synthetic-only", "ip": "127.0.0.1", "rssi": -40},
          "cloud": {"enabled": False, "connected": False}, "mqtt": {"connected": False},
          "relays": [{"ison": False, "has_timer": False, "timer_started": 0,
                      "timer_duration": 0, "timer_remaining": 0, "source": "input"}],
          "inputs": [{"input": 0, "event": "", "event_cnt": 0}], "mac": MAC,
          "uptime": 120, "ram_total": 50000, "ram_free": 30000, "fs_size": 233681, "fs_free": 163000,
          "has_update": False, "update": {"status": "idle", "has_update": False,
              "old_version": SHELLY["fw"], "new_version": SHELLY["fw"]}}
CIT_D = {"blk": [{"I": 1, "D": "relay_0"}],
         "sen": [{"I": 1101, "T": "B", "D": "output", "R": "0/1", "L": 1}]}
CIT_S = {"G": [[0, 1101, 0]]}
READS = {"/shelly": SHELLY, "/settings": SETTINGS, "/status": STATUS, "/cit/d": CIT_D}


def coap_reply(mid, value):
    """Native CoIoT JSON reply with required option 3332 identifying the device."""
    device_id = f"{MODEL}#{MAC}#2".encode()
    # delta 3332 = extended-14 + (3332 - 269), length uses extended-13.
    option = bytes([0xED]) + struct.pack("!H", 3332 - 269) + bytes([len(device_id) - 13]) + device_id
    return b"\x50\x45" + mid + option + b"\xff" + json.dumps(value, separators=(",", ":")).encode()


async def async_setup(hass, config):
    journal = Path(hass.config.path("agentic_shelly_fixture_requests.jsonl"))
    counts = Counter()
    lock = asyncio.Lock()

    def load_journal():
        if journal.exists():
            for line in journal.read_text().splitlines():
                counts.update([json.loads(line)["operation"]])

    await hass.async_add_executor_job(load_journal)

    def publish():
        hass.states.async_set(COUNTER, sum(counts.values()), {
            "operations": dict(counts), "rejected_operations": counts["rejected"],
            "relay_state": "off", "scope": "synthetic container loopback only"})

    async def record(operation):
        async with lock:
            def append():
                with journal.open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"operation": operation}) + "\n")
                    output.flush()
            await hass.async_add_executor_job(append)
            counts.update([operation])
            publish()

    async def respond(request):
        allowed = request.method == "GET" and request.path in READS and not request.query_string
        await record("http:" + request.path if allowed else "rejected")
        if not allowed:
            raise web.HTTPForbidden(text="Synthetic fixture permits fixed read requests only")
        return web.json_response(READS[request.path])

    class CoapResponder(asyncio.DatagramProtocol):
        transport = None

        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, address):
            async def reply():
                # Exact read requests emitted by the pinned native CoIoT client.
                path = next((part for part in ("s", "d")
                    if data == b"\x50\x01\x00\x0a\xb3cit\x01" + part.encode() + b"\xff\x00"), None)
                if path is None or address[0] != "127.0.0.1":
                    await record("rejected")
                    return
                await record("coap:/cit/" + path)
                self.transport.sendto(coap_reply(data[2:4], CIT_S if path == "s" else CIT_D), address)
            hass.async_create_task(reply())

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_route("*", "/{path:.*}", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 80).start()
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        CoapResponder, local_addr=("127.0.0.1", 5683))
    await record("fixture_boot")

    async def discover(call):
        await hass.config_entries.flow.async_init("shelly", context={"source": SOURCE_ZEROCONF},
            data=ZeroconfServiceInfo(ip_address=IPv4Address("127.0.0.1"),
                ip_addresses=[IPv4Address("127.0.0.1")], port=80,
                hostname=HOSTNAME + ".local.", type="_http._tcp.local.",
                name=HOSTNAME + "._http._tcp.local.", properties={}))

    async def model_boundary(call):
        # Exercise the actual pinned Core formatter and support predicate only.
        # This deliberately inconsistent RPC report is NOT a real RPC flow or
        # a physical-device qualification: it demonstrates why model text does
        # not attest generation, even with native helpers left unmodified.
        from homeassistant.components.shelly.utils import get_model_name, is_device_supported

        rpc_report = {"gen": 2, "model": MODEL, "mac": MAC, "auth_en": False}
        hass.states.async_set("sensor.agentic_shelly_model_boundary", "observed", {
            "gen1_reported_model": get_model_name(SHELLY),
            "inconsistent_rpc_reported_model": get_model_name(rpc_report),
            "inconsistent_rpc_support_predicate": is_device_supported(rpc_report),
            "scope": "native formatter only; no RPC connection or flow"})

    async def stop(event):
        transport.close()
        await runner.cleanup()

    hass.services.async_register(DOMAIN, "discover", discover)
    hass.services.async_register(DOMAIN, "model_boundary", model_boundary)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True
