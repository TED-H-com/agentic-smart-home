"""Actual packaged integration, native auth/actions, durable queue and Core restart.

No provider account is contacted. All data and faults belong to the disk-backed
isolated Core fixture, which removes its container in finally.
"""
import asyncio
import hashlib
import json
import secrets
import uuid
import zipfile
from pathlib import Path

import aiohttp
from agentic_controller.ha import HAError, HomeAssistant
from agentic_controller.models import digest
from agentic_controller.store import Store
from test_real_ha import docker, real_container, wait_ready  # noqa: F401

DOMAIN = "agentic_home_notifications"
ROOT = Path(__file__).parents[1]


def install_archive(config):
    archive = ROOT / "controller/static/downloads/agentic-home-notifications.zip"
    assert archive.is_file(), "Build scripts/package.py::package_notifications before real Core tests"
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("MANIFEST.sha256.json"))
        assert set(source.namelist()) == {*manifest, "MANIFEST.sha256.json"}
        for name, checksum in manifest.items():
            payload = source.read(name)
            assert hashlib.sha256(payload).hexdigest() == checksum
            if name == "INSTALL.md":
                continue
            relative = Path(name)
            assert relative.parts[:2] == ("custom_components", DOMAIN)
            assert not relative.is_absolute() and ".." not in relative.parts
            # A stale built archive must not accidentally validate new source.
            assert payload == (ROOT / relative).read_bytes()
            target = config / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)


async def access_token(base, code):
    async with aiohttp.ClientSession() as session:
        async with session.post(base + "/auth/token", data={"grant_type": "authorization_code",
                                "code": code, "client_id": base + "/"}) as response:
            assert response.status == 200, "Isolated authentication failed"
            return (await response.json())["access_token"]


async def login_user(owner, base, *, admin):
    username, password = "fixture-" + uuid.uuid4().hex[:8], secrets.token_urlsafe(32)
    created = await owner.ws("config/auth/create", name="Notification fixture",
                             group_ids=["system-admin" if admin else "system-users"])
    await owner.ws("config/auth_provider/homeassistant/create", user_id=created["user"]["id"],
                   username=username, password=password)
    async with aiohttp.ClientSession() as session:
        async with session.post(base + "/auth/login_flow", json={"client_id": base + "/",
                                "redirect_uri": base + "/", "handler": ["homeassistant", None]}) as response:
            assert response.status == 200, "Isolated login flow failed"
            flow = await response.json()
        async with session.post(base + "/auth/login_flow/" + flow["flow_id"],
                                json={"client_id": base + "/", "username": username,
                                      "password": password}) as response:
            assert response.status == 200, "Isolated login failed"
            result = await response.json()
            assert result["type"] == "create_entry", "Isolated login did not authorize"
    return await access_token(base, result["result"])


async def client(token, base, store):
    ha = HomeAssistant(token, base + "/api", base.replace("http:", "ws:") + "/api/websocket", external=True)
    ha.store = store
    await ha.start()
    return ha


async def raw_command(ha, command, **fields):
    async with await ha.ws_connect() as connection:
        await connection.send_json({"id": 1, "type": command, **fields})
        return await connection.receive_json(timeout=20)


async def native_status(ha):
    last = None
    for _ in range(60):
        try:
            return await ha.ws(DOMAIN + "/status")
        except HAError as error:
            last = str(error)
        await asyncio.sleep(1)
    raise AssertionError("Packaged native integration did not load: " + str(last))


async def restart(name, ha):
    await asyncio.to_thread(docker, "restart", name)
    port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostPort"]
    base = f"http://127.0.0.1:{port}"
    ha.base, ha.websocket = base + "/api", base.replace("http:", "ws:") + "/api/websocket"
    await wait_ready(base)
    return base


async def send_text(ha, message="Native text fixture"):
    return await ha.ws("call_service", domain=DOMAIN, service="send_text",
                       service_data={"recipient": "family", "message": message}, return_response=True)


async def test_packaged_native_actions_owner_queue_restart_and_storage_failure(real_container, tmp_path):  # noqa: F811
    name, base, config = real_container
    await asyncio.to_thread(docker, "stop", name)
    install_archive(config)
    await asyncio.to_thread(docker, "start", name)
    port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostPort"]
    base = f"http://127.0.0.1:{port}"
    await wait_ready(base)
    async with aiohttp.ClientSession() as session:
        async with session.post(base + "/api/onboarding/users", json={"client_id": base + "/",
                                "name": "Native fixture owner", "username": "native-owner",
                                "password": secrets.token_urlsafe(32), "language": "en"}) as response:
            assert response.status == 200, "Isolated owner onboarding failed"
            code = (await response.json())["auth_code"]
    store = Store(tmp_path / "native-controller.sqlite3")
    ha = await client(await access_token(base, code), base, store)
    peers = []
    try:
        flow = await ha.request("POST", "/config/config_entries/flow", {"handler": DOMAIN,
                                                                      "show_advanced_options": False})
        assert flow["type"] == "form"
        result = await ha.request("POST", "/config/config_entries/flow/" + flow["flow_id"], {})
        assert result["type"] == "create_entry"
        initial = await native_status(ha)
        assert initial["controller_id"] is None and initial["enabled"] is False
        services = await ha.ws("get_services")
        assert set(services[DOMAIN]) == {"send_text"}
        controller = uuid.uuid4().hex
        heartbeat = {"controller_id": controller, "enabled": True, "routing_revision": digest("route-one"),
                     "aliases": [{"alias": "family", "label": "Fixture family", "channel": "line",
                                  "capabilities": ["text"]}],
                     "channels": [{"channel": "line", "transport": "connected", "login": "ready"}]}
        status = await ha.ws(DOMAIN + "/update_status", **heartbeat)
        assert status["controller_id"] == controller
        registry = [entity for entity in await ha.ws("config/entity_registry/list") if entity["platform"] == DOMAIN]
        assert len(registry) == 6
        identities = {(entity["unique_id"], entity["entity_id"]) for entity in registry}
        entity_ids = {entity["unique_id"].split("_", 1)[1]: entity["entity_id"] for entity in registry}
        states = {state["entity_id"]: state for state in await ha.request("GET", "/states")}
        assert states[entity_ids["controller_connection"]]["state"] == "on"
        assert states[entity_ids["pending"]]["state"] == "0"
        assert states[entity_ids["line_login"]]["state"] == "ready"
        assert states[entity_ids["last_delivery"]]["state"] == "none"
        for entity_id in entity_ids.values():
            assert not {"message", "chat", "token", "claim_id"}.intersection(states[entity_id]["attributes"])
        wrong = await raw_command(ha, DOMAIN + "/update_status", **{**heartbeat, "controller_id": uuid.uuid4().hex})
        assert wrong["success"] is False and wrong["error"]["code"] == "needs_rebind"

        # A real non-owner admin has broad native API permissions but never queue ownership.
        for admin in (True, False):
            peer = await client(await login_user(ha, base, admin=admin), base, store)
            peers.append(peer)
            identity = await peer.ws("auth/current_user")
            assert identity["is_owner"] is False and identity["is_admin"] is admin
            for command, fields in (("status", {}), ("update_status", heartbeat),
                                    ("pull", {"controller_id": controller})):
                denied = await raw_command(peer, DOMAIN + "/" + command, **fields)
                assert denied["success"] is False and denied["error"]["code"] == "unauthorized"
            if not admin:
                denied = await raw_command(peer, "call_service", domain=DOMAIN, service="send_text",
                    service_data={"recipient": "family", "message": "Not authorized"}, return_response=True)
                # Native call_service wraps its handler's Unauthorized in a
                # HomeAssistantError; dedicated commands above retain the code.
                assert denied["success"] is False and denied["error"]["code"] == "home_assistant_error"
        assert (await ha.ws(DOMAIN + "/status"))["pending"] == 0

        action = await send_text(ha, "門已開啟")
        assert action["response"]["state"] == "queued"
        request = (await ha.ws(DOMAIN + "/pull", controller_id=controller))["requests"][0]
        assert request["id"] == action["response"]["request_id"]
        assert request["digest"] == digest({key: value for key, value in request.items() if key != "digest"})
        claim = {"controller_id": controller, "source_id": status["source_id"], "epoch": status["epoch"],
                 "request_id": request["id"], "digest": request["digest"],
                 "routing_revision": request["routing_revision"], "claim_id": uuid.uuid4().hex}
        granted = await ha.ws(DOMAIN + "/claim", **claim)
        assert granted == {"granted": True, "request": request}
        assert await ha.ws(DOMAIN + "/claim", **claim) == {"granted": False, "request": None}
        # All five queue operations are owner-only, including effect authorization and receipts.
        ack = {key: value for key, value in claim.items() if key != "routing_revision"}
        ack.update(state="submitted", reason=None)
        for command, fields in (("claim", claim), ("ack", ack)):
            denied = await raw_command(peers[0], DOMAIN + "/" + command, **fields)
            assert denied["success"] is False and denied["error"]["code"] == "unauthorized"

        # Native automation execution uses the same action with its own system context.
        automation_id = "native_notification_fixture"
        await ha.write(automation_id, {"id": automation_id, "alias": "Native notification fixture",
            "triggers": [{"trigger": "event", "event_type": "native_notification_fixture"}],
            "conditions": [], "actions": [{"action": DOMAIN + ".send_text",
                "data": {"recipient": "family", "message": "Automation context fixture"}}], "mode": "single"})
        await ha.automation_entity(automation_id)
        await ha.ws("fire_event", event_type="native_notification_fixture", event_data={})
        for _ in range(50):
            pulled = (await ha.ws(DOMAIN + "/pull", controller_id=controller))["requests"]
            if pulled:
                break
            await asyncio.sleep(0.1)
        assert len(pulled) == 1 and pulled[0]["message"] == "Automation context fixture"

        base = await restart(name, ha)
        restored = await native_status(ha)
        assert restored["source_id"] == status["source_id"] and restored["epoch"] == status["epoch"]
        assert restored["pending"] == 2 and restored["last_delivery"]["state"] == "unknown"
        assert restored["last_delivery"]["request_id"] == request["id"]
        registry = [entity for entity in await ha.ws("config/entity_registry/list") if entity["platform"] == DOMAIN]
        assert {(entity["unique_id"], entity["entity_id"]) for entity in registry} == identities
        states = {state["entity_id"]: state for state in await ha.request("GET", "/states")}
        assert states[entity_ids["controller_connection"]]["state"] == "off"
        assert states[entity_ids["last_delivery"]]["state"] == "unknown"
        stale = await raw_command(ha, DOMAIN + "/pull", controller_id=controller)
        assert stale["success"] is False and stale["error"]["code"] == "stale_heartbeat"
        await ha.ws(DOMAIN + "/update_status", **heartbeat)
        assert await ha.ws(DOMAIN + "/claim", **claim) == {"granted": False, "request": None}
        assert (await ha.ws(DOMAIN + "/pull", controller_id=controller))["requests"] == pulled
        assert await ha.ws(DOMAIN + "/ack", **ack) == {"acknowledged": True}
        assert await ha.ws(DOMAIN + "/ack", **ack) == {"acknowledged": True}
        conflict = await raw_command(ha, DOMAIN + "/ack", **{**ack, "state": "unknown"})
        assert conflict["success"] is False and conflict["error"]["code"] == "receipt_conflict"
        assert (await ha.ws(DOMAIN + "/status"))["last_delivery"]["state"] == "submitted"

        entries = await ha.ws("config_entries/get", domain=DOMAIN)
        assert len(entries) == 1
        entry_id = entries[0]["entry_id"]
        storage_path = "/config/.storage/" + DOMAIN + "." + entry_id
        # A directory at our file path makes HA Store's otherwise swallowed atomic
        # write failure real. Exact readback must prevent a success response.
        fault = "import os,sys; p=sys.argv[1]; os.rename(p,p+'.fixture-backup'); os.mkdir(p)"
        docker("exec", name, "python", "-c", fault, storage_path)
        try:
            failed = await raw_command(ha, "call_service", domain=DOMAIN, service="send_text",
                service_data={"recipient": "family", "message": "Must not be accepted"}, return_response=True)
            assert failed["success"] is False
            fenced = await raw_command(ha, DOMAIN + "/status")
            assert fenced["success"] is False and fenced["error"]["code"] == "storage_unavailable"
        finally:
            repair = "import os,sys; p=sys.argv[1]; os.rmdir(p); os.rename(p+'.fixture-backup',p)"
            docker("exec", name, "python", "-c", repair, storage_path)

        # Restore the good journal through restart, then prove corrupt storage is
        # neither overwritten nor mistaken for a fresh enrollment.
        await restart(name, ha)
        recovered = await native_status(ha)
        assert recovered["pending"] == 1 and recovered["last_delivery"]["state"] == "submitted"
        docker("exec", name, "python", "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('{broken')", storage_path)
        await restart(name, ha)
        for _ in range(60):
            failure = await raw_command(ha, DOMAIN + "/status")
            if failure.get("error", {}).get("code") == "not_configured":
                break
            await asyncio.sleep(1)
        assert failure["success"] is False and failure["error"]["code"] == "not_configured"
        assert docker("exec", name, "cat", storage_path) == "{broken"
    finally:
        for peer in peers:
            await peer.close()
        await ha.close()
        store.close()
