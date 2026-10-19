"""Test-only loopback IPP endpoint and synthetic zeroconf discovery injection.

The shipped IPP integration and aioipp parser are unmodified. This fixture
answers only Get-Printer-Attributes; it has no printing/device-control support.
No multicast discovery or physical printer is involved.
"""
import struct
from ipaddress import IPv4Address

from aiohttp import web
from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

DOMAIN = "agentic_ipp_fixture"
IDENTITY = "11111111-2222-4333-8444-555555555555"
NAME = "Agentic Synthetic Printer"
COUNTER = "sensor.agentic_ipp_fixture_requests"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


def attribute(tag, name, value):
    name = name.encode("ascii")
    if isinstance(value, str):
        value = value.encode("utf-8")
    return bytes([tag]) + struct.pack("!H", len(name)) + name + struct.pack("!H", len(value)) + value


def printer_response(request_body, port):
    """IPP successful response, preserving the actual client's request ID."""
    return b"".join([
        request_body[:2], b"\x00\x00", request_body[4:8],
        b"\x01",  # operation attributes
        attribute(0x47, "attributes-charset", "utf-8"),
        attribute(0x48, "attributes-natural-language", "en"),
        b"\x04",  # printer attributes
        attribute(0x42, "printer-name", NAME),
        attribute(0x41, "printer-info", "Synthetic fixture; no physical printer"),
        attribute(0x41, "printer-make-and-model", "Agentic Synthetic Printer"),
        attribute(0x41, "printer-device-id", "MFG:Agentic;MDL:Synthetic Printer;SERIALNUMBER:fixture-only;"),
        attribute(0x45, "printer-uuid", "urn:uuid:" + IDENTITY),
        attribute(0x45, "printer-uri-supported", f"ipp://127.0.0.1:{port}/ipp/print"),
        attribute(0x23, "printer-state", struct.pack("!I", 3)),  # idle
        attribute(0x44, "printer-state-reasons", "none"),
        attribute(0x21, "printer-up-time", struct.pack("!I", 60)),
        b"\x03",  # end of attributes
    ])


async def async_setup(hass, config):
    requests = 0
    rejected = 0
    port = None

    def publish_counter():
        hass.states.async_set(COUNTER, requests, {"rejected_operations": rejected,
            "scope": "synthetic container loopback only"})

    async def respond(request):
        nonlocal requests, rejected
        body = await request.read()
        # 0x000b is Get-Printer-Attributes. Never accept a print job.
        if len(body) < 8 or body[2:4] != b"\x00\x0b":
            rejected += 1
            publish_counter()
            raise web.HTTPBadRequest(text="Only synthetic Get-Printer-Attributes is supported")
        requests += 1
        publish_counter()
        return web.Response(body=printer_response(body, port), content_type="application/ipp")

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/ipp/print", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    publish_counter()

    async def discover(call):
        # This replaces only the network observation. Native HA creates and
        # validates the flow, contacts the real HTTP endpoint, and owns the form.
        await hass.config_entries.flow.async_init("ipp", context={"source": SOURCE_ZEROCONF},
            data=ZeroconfServiceInfo(ip_address=IPv4Address("127.0.0.1"),
                ip_addresses=[IPv4Address("127.0.0.1")], port=port,
                hostname="agentic-synthetic-printer.local.", type="_ipp._tcp.local.",
                name=NAME + "._ipp._tcp.local.", properties={"rp": "ipp/print", "UUID": IDENTITY}))

    async def stop(event):
        await runner.cleanup()

    hass.services.async_register(DOMAIN, "discover", discover)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True
