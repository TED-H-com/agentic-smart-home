"""Synthetic Synology DSM web API responder; native synology_dsm stays unmodified.

Hand-authored from py-synologydsm-api 2.10.3 (synology_dsm.py, api/**) and the
synology_dsm integration exactly as shipped in the pinned Core 2026.10.1/.2
images. It is NOT a capture of a physical NAS or of DSM firmware. Only
container loopback HTTPS 127.0.0.1:5001 is served, with a throwaway self-signed
certificate generated at startup (the native flow defaults to verify_ssl off).
The test writes a synthetic scenario (account, optional single valid one-time
code, writable shares, identity) into the config dir before Core starts.

Every request is journaled with only api/method/outcome. Credentials, one-time
codes, session ids and device tokens are never written; issued device tokens
persist only as SHA-256 digests so trusted-device logins survive Core restarts.
Anything outside the explicit read allowlist is rejected and journaled as
"rejected". Write-capable APIs are advertised on purpose so an attempted write
reaches this responder instead of failing silently inside the client.
Synthetic SSDP injection replaces LAN observation only, never the native flow,
login, coordinator, entity/backup-agent setup or registry creation.
"""
import asyncio
import hashlib
import json
import secrets
import ssl
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path

from aiohttp import web
from homeassistant.config_entries import SOURCE_SSDP
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

DOMAIN = "agentic_synology_fixture"
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
SCENARIO = "agentic_synology_scenario.json"
JOURNAL = "agentic_synology_fixture_requests.jsonl"
STATE = "agentic_synology_fixture_state.json"
COUNTER = "sensor.agentic_synology_fixture_requests"
PORT = 5001
VERSION_STRING = "DSM 7.2.2-72806 Update 3"
# name: (minVersion, maxVersion, CGI path) as returned by SYNO.API.Info query.
APIS = {
    "SYNO.API.Info": (1, 1, "query.cgi"),
    "SYNO.API.Auth": (1, 7, "entry.cgi"),
    "SYNO.DSM.Info": (1, 2, "entry.cgi"),
    "SYNO.DSM.Network": (1, 2, "entry.cgi"),
    "SYNO.Core.System": (1, 3, "entry.cgi"),
    "SYNO.Core.System.Utilization": (1, 1, "entry.cgi"),
    "SYNO.Core.SecurityScan.Status": (1, 1, "entry.cgi"),
    "SYNO.Core.Hardware.FanSpeed": (1, 1, "entry.cgi"),
    "SYNO.Core.ExternalDevice.Storage.USB": (1, 1, "entry.cgi"),
    "SYNO.Core.Upgrade.Server": (1, 4, "entry.cgi"),
    "SYNO.Storage.CGI.Storage": (1, 1, "entry.cgi"),
    "SYNO.FileStation.List": (1, 2, "entry.cgi"),
    "SYNO.FileStation.Download": (1, 2, "entry.cgi"),
    "SYNO.FileStation.Upload": (1, 3, "entry.cgi"),
    "SYNO.FileStation.Delete": (1, 2, "entry.cgi"),
    "SYNO.FileStation.CreateFolder": (1, 2, "entry.cgi"),
}
# Explicit allowlist: (api, method) -> the only HTTP verb the pinned client uses.
# Login/logout are the client's own session; every other entry is a status read.
# Upgrade.Server check only reports availability (a real NAS may ask Synology).
READS = {
    ("SYNO.API.Info", "query"): "GET",
    ("SYNO.API.Auth", "login"): "GET",
    ("SYNO.API.Auth", "logout"): "GET",
    ("SYNO.DSM.Info", "getinfo"): "GET",
    ("SYNO.DSM.Network", "list"): "GET",
    ("SYNO.Core.System", "info"): "GET",
    ("SYNO.Core.System.Utilization", "get"): "GET",
    ("SYNO.Core.SecurityScan.Status", "system_get"): "GET",
    ("SYNO.Core.Hardware.FanSpeed", "get"): "GET",
    ("SYNO.Core.ExternalDevice.Storage.USB", "list"): "POST",
    ("SYNO.Core.Upgrade.Server", "check"): "GET",
    ("SYNO.Storage.CGI.Storage", "load_info"): "GET",
    ("SYNO.FileStation.List", "list_share"): "GET",
    ("SYNO.FileStation.List", "list"): "GET",
}
UNAUTHENTICATED = {"SYNO.API.Info", "SYNO.API.Auth"}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()



def tls_context(config_dir):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-dsm.invalid")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # The throwaway key lives in the container filesystem only while loading.
    with tempfile.TemporaryDirectory(dir=config_dir) as directory:
        cert_path, key_path = Path(directory, "cert.pem"), Path(directory, "key.pem")
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        context.load_cert_chain(cert_path, key_path)
    return context


def share(entry):
    return {"isdir": True, "name": entry["name"], "path": entry["path"], "additional": {
        "mount_point_type": "", "real_path": "/volume1" + entry["path"], "sync_share": False,
        "owner": {"gid": 100, "group": "users", "uid": 1026, "user": "synthetic"},
        "perm": {"acl": {"append": True, "del": True, "exec": True, "read": True, "write": True},
                 "acl_enable": True, "adv_right": {"disable_download": False, "disable_list": False,
                 "disable_modify": False}, "is_acl_mode": True, "is_share_readonly": False, "posix": 0,
                 "share_right": "RW"},
        "time": {"atime": 1760832000, "crtime": 1760832000, "ctime": 1760832000, "mtime": 1760832000},
        "volume_status": {"freespace": 2605432109876, "readonly": False, "totalspace": 3839999999999}}}


def reads(scenario):
    """Synthetic DSM 7.2 read payloads, shaped for the pinned library parsers."""
    check = {"category": "", "fail": {"danger": 0, "info": 0, "outOfDate": 0, "risk": 0, "warning": 0},
             "failSeverity": "safe", "progress": 100, "runningItem": "", "total": 4, "waitNum": 0}
    shares = [share(entry) for entry in scenario["writable_shares"]]
    return {
        ("SYNO.DSM.Info", "getinfo"): {"codepage": "enu", "model": scenario["model"], "ram": 4096,
            "serial": scenario["serial"], "temperature": 38, "temperature_warn": False,
            "time": "2026-10-19 00:00:00", "uptime": 86400, "version": "72806", "version_string": VERSION_STRING},
        ("SYNO.DSM.Network", "list"): {"dns": ["127.0.0.1"], "gateway": "127.0.0.1",
            "hostname": scenario["hostname"], "workgroup": "WORKGROUP", "interfaces": [{"id": "eth0",
            "ip": [{"address": "127.0.0.1", "netmask": "255.0.0.0"}], "ipv6": [], "mac": scenario["mac"],
            "type": "lan"}]},
        ("SYNO.Core.System", "info"): {"cpu_clock_speed": 2000, "cpu_cores": "4", "cpu_family": "Synthetic",
            "cpu_series": "SYN-4", "enabled_ntp": False, "ntp_server": "", "firmware_ver": VERSION_STRING,
            "model": scenario["model"], "ram_size": 4096, "serial": scenario["serial"], "sys_temp": 38,
            "time": "2026-10-19 00:00:00", "time_zone": "UTC", "time_zone_desc": "(GMT) UTC",
            "up_time": "24:0:0", "usb_dev": []},
        ("SYNO.Core.System.Utilization", "get"): {"time": 1760832000,
            "cpu": {"15min_load": 9, "1min_load": 12, "5min_load": 10, "device": "System", "other_load": 1,
                    "system_load": 2, "user_load": 7},
            "memory": {"avail_real": 2621440, "avail_swap": 2097152, "buffer": 65536, "cached": 1048576,
                       "device": "Memory", "memory_size": 4194304, "real_usage": 21, "si_disk": 0, "so_disk": 0,
                       "swap_usage": 0, "total_real": 3932160, "total_swap": 2097152},
            "network": [{"device": "total", "rx": 1200, "tx": 3400}, {"device": "eth0", "rx": 1200, "tx": 3400}]},
        ("SYNO.Core.SecurityScan.Status", "system_get"): {"lastScanTime": "1760832000", "startTime": "",
            "success": True, "sysProgress": 100, "sysStatus": "safe", "items": {name: {**check, "category": name}
            for name in ("malware", "network", "securitySetting", "systemCheck", "update", "userInfo")}},
        ("SYNO.Core.Hardware.FanSpeed", "get"): {"all_disk_temp_fail": "no", "cool_fan": "yes",
            "dual_fan_speed": "coolfan", "fan_support_adjust_by_ext_nic": "no", "fan_type": 11},
        ("SYNO.Core.ExternalDevice.Storage.USB", "list"): {"devices": []},
        ("SYNO.Core.Upgrade.Server", "check"): {"update": {"available": False, "reboot": "none",
            "restart": "none", "type": "none", "version": ""}},
        ("SYNO.Storage.CGI.Storage", "load_info"): {
            "disks": [{"id": "sata1", "name": "Drive 1", "device": "/dev/sata1", "firm": "SYN1",
                       "diskType": "SATA", "smart_status": "normal", "status": "normal",
                       "exceed_bad_sector_thr": False, "below_remain_life_thr": False, "temp": 33,
                       "model": "SYNTHETIC HDD ", "vendor": "Synthetic", "size_total": "4000787030016"}],
            "env": {"bay_number": "1"},
            "storagePools": [{"id": "reuse_1", "deploy_path": "volume_1", "device_type": "shr_without_disk_protect",
                              "disks": ["sata1"], "status": "normal", "pool_child": [{"id": "volume_1",
                              "size": {"total": "3839999999999", "used": "1234567890123"}}]}],
            "volumes": [{"id": "volume_1", "device_type": "shr_without_disk_protect", "fs_type": "btrfs",
                         "status": "normal", "size": {"free_inode": "0", "total": "3839999999999",
                         "total_device": "3998000000000", "total_inode": "0", "used": "1234567890123"}}]},
        ("SYNO.FileStation.List", "list_share"): {"offset": 0, "shares": shares, "total": len(shares)},
    }


async def async_setup(hass, config):
    scenario = json.loads(await hass.async_add_executor_job(Path(hass.config.path(SCENARIO)).read_text))
    if scenario.get("synthetic") is not True:
        raise ValueError("agentic_synology_fixture requires an explicitly synthetic scenario")
    journal, state_path = Path(hass.config.path(JOURNAL)), Path(hass.config.path(STATE))
    payloads = reads(scenario)
    counts = Counter()
    lock = asyncio.Lock()
    sessions = set()

    def load():
        if journal.exists():
            for line in journal.read_text().splitlines():
                row = json.loads(line)
                counts.update([" ".join((row["api"], row["method"], row["outcome"]))])
        return json.loads(state_path.read_text()) if state_path.exists() else {
            "device_token_sha256": [], "otp_consumed": False}

    state = await hass.async_add_executor_job(load)

    def publish():
        hass.states.async_set(COUNTER, sum(counts.values()), {
            "operations": dict(counts), "rejected_operations": sum(
                count for key, count in counts.items() if key.endswith(" rejected")),
            "scope": "synthetic container loopback only"})

    async def record(api, method, outcome):
        async with lock:
            def append():
                with journal.open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"api": api, "method": method, "outcome": outcome}) + "\n")
                    output.flush()
            await hass.async_add_executor_job(append)
            counts.update([" ".join((api, method, outcome))])
            publish()

    async def save_state():
        encoded = json.dumps(state)
        await hass.async_add_executor_job(state_path.write_text, encoded)

    def ok(data):
        return web.json_response({"data": data, "success": True})

    def error(code, errors=None):
        return web.json_response({"error": {"code": code, **({"errors": errors} if errors else {})},
                                  "success": False})

    def same(given, expected):
        return isinstance(given, str) and secrets.compare_digest(given.encode(), expected.encode())

    async def login(params):
        if not (same(params.get("account"), scenario["username"])
                and same(params.get("passwd"), scenario["password"])):
            return "invalid_credentials", error(400)
        issued, outcome = None, "ok"
        if scenario["otp_required"]:
            token = params.get("device_id")
            # Trusted device first: a valid token skips the code (assumed DSM
            # semantics, not verified against firmware). Codes are single-use.
            if token and digest(token) in state["device_token_sha256"]:
                outcome = "ok_device_token_otp_ignored" if params.get("otp_code") else "ok_device_token"
            elif not params.get("otp_code"):
                return "otp_required", error(403, {"types": [{"type": "authenticator"}]})
            elif state["otp_consumed"] or not same(params["otp_code"], scenario["otp_code"]):
                return "otp_failed", error(404)
            else:
                state["otp_consumed"], outcome = True, "ok_otp"
                if params.get("enable_device_token") == "yes":
                    issued = secrets.token_urlsafe(32)
                    state["device_token_sha256"].append(digest(issued))
                    outcome = "ok_otp_device_token_issued"
                await save_state()
        sid = secrets.token_urlsafe(32)
        sessions.add(sid)
        return outcome, ok({"is_portal_port": False, "sid": sid, **({"did": issued} if issued else {})})

    def folder(params):
        path = params.get("folder_path", "")
        if any(path == entry["path"] or path.startswith(entry["path"] + "/")
               for entry in scenario["writable_shares"]):
            return "ok", ok({"files": [], "offset": 0, "total": 0})
        return "not_found", error(408)

    async def respond(request):
        params = dict(request.query)
        if request.method == "POST":
            try:
                params.update((key, value) for key, value in (await request.post()).items()
                              if isinstance(value, str))
            except (web.HTTPException, ValueError):
                params["method"] = "unreadable-body"
        api, method = params.get("api"), params.get("method")
        known = APIS.get(api)
        try:
            version_ok = known is not None and known[0] <= int(params.get("version", "")) <= known[1]
        except ValueError:
            version_ok = False
        if (READS.get((api, method)) != request.method or not version_ok
                or request.path != "/webapi/" + known[2]):
            await record(api if api in APIS else "unknown",
                         method if (api, method) in READS else "unknown", "rejected")
            return error(105)
        if (api, method) == ("SYNO.FileStation.List", "list_share") and params.get("onlywritable") != "True":
            await record(api, method, "rejected")
            return error(105)
        if api not in UNAUTHENTICATED and params.get("_sid") not in sessions:
            await record(api, method, "session_invalid")
            return error(119)
        if api == "SYNO.API.Info":
            outcome, response = "ok", ok({name: {"minVersion": low, "maxVersion": high, "path": path}
                                          for name, (low, high, path) in APIS.items()})
        elif method == "login":
            outcome, response = await login(params)
        elif method == "logout":
            sessions.discard(params.get("_sid"))
            outcome, response = "ok", ok({})
        elif api == "SYNO.FileStation.List" and method == "list":
            outcome, response = folder(params)
        else:
            outcome, response = "ok", ok(payloads[(api, method)])
        await record(api, method, outcome)
        return response

    context = await hass.async_add_executor_job(tls_context, hass.config.config_dir)
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_route("*", "/{path:.*}", respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT, ssl_context=context).start()
    await record("fixture", "boot", "ok")

    async def discover(call):
        mac = scenario["mac"].replace(":", "").upper()
        await hass.config_entries.flow.async_init("synology_dsm", context={"source": SOURCE_SSDP},
            data=SsdpServiceInfo(ssdp_usn=f"uuid:73796E6F-6473-6D00-0000-{mac.lower()}::upnp:rootdevice",
                ssdp_st="upnp:rootdevice", ssdp_location="http://127.0.0.1:5000/description.xml",
                upnp={"deviceType": "urn:schemas-upnp-org:device:Basic:1", "manufacturer": "Synology",
                      "friendlyName": f"{scenario['friendly_name']} ({scenario['model']})",
                      "modelName": scenario["model"], "serialNumber": mac}))
        await record("fixture", "discover", "ssdp_injected")

    async def stop(event):
        await runner.cleanup()

    async def inspect_entry(call):
        from homeassistant.components.synology_dsm.backup import async_get_backup_agents

        entries = hass.config_entries.async_loaded_entries("synology_dsm")
        for entry in entries:
            await entry.runtime_data.coordinator_central.async_refresh()
        agents = await async_get_backup_agents(hass)
        backups = [backup for agent in agents for backup in await agent.async_list_backups()]
        hass.states.async_set("sensor.agentic_synology_fixture_readback", len(entries), {
            "backup_agent_count": len(agents), "backup_count": len(backups),
            "coordinator_success": all(entry.runtime_data.coordinator_central.last_update_success for entry in entries)})

    hass.services.async_register(DOMAIN, "inspect_entry", inspect_entry)
    hass.services.async_register(DOMAIN, "discover", discover)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True
