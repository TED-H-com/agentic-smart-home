"""Real isolated Core authorization/readback around explicitly imported fixture data.

This is not a LAN, router-account or physical Hue acceptance test.
"""
import json
import secrets

import aiohttp
import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.ha import HomeAssistant
from agentic_controller.store import Store
from test_real_ha import real_container, wait_ready


@pytest.fixture
def inventory_core(tmp_path, request):
    yield from real_container.__wrapped__(tmp_path, request)


async def test_real_core_inventory_handoff_reads_registry_without_configuring_device(inventory_core, tmp_path, monkeypatch):
    _, base, _ = inventory_core
    await wait_ready(base)
    async with aiohttp.ClientSession() as session:
        async with session.post(base + "/api/onboarding/users", json={"client_id": base + "/", "name": "Inventory Lab",
                                "username": "inventory-lab", "password": secrets.token_urlsafe(32), "language": "en"}) as response:
            onboarding = await response.json()
            assert response.status == 200
        async with session.post(base + "/auth/token", data={"grant_type": "authorization_code",
                                "code": onboarding["auth_code"], "client_id": base + "/"}) as response:
            auth = await response.json()
            assert response.status == 200
    ha = HomeAssistant(auth["access_token"], base + "/api", base.replace("http:", "ws:") + "/api/websocket")
    store = Store(tmp_path / "inventory-real-core.sqlite3")
    ha.store = store
    await ha.start()
    app = None
    try:
        owner = await ha.ws("auth/current_user")
        store.set_setting("owner", owner["id"])
        monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
        (tmp_path / "access-key").write_text("inventory-real-core-local-test-key")
        app = create_app(store=store, ha=ha, host_mode=True)
        policy_before = store.setting("policy")
        entries_before = await ha.ws("config_entries/get")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099",
                                     headers={"Origin": "http://localhost:8099"}) as client:
            assert (await client.post("/api/host/login", json={"key": "inventory-real-core-local-test-key"})).status_code == 200
            client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
            imported = await client.post("/api/discovery/inventory/import", headers={"Idempotency-Key": "import"},
                json={"format": "json", "label": "Explicit synthetic router export",
                      "content": json.dumps([{"ip": "192.0.2.20", "hostname": "Fixture Hue bridge",
                                              "manufacturer": "Philips Hue"}])})
            assert imported.status_code == 200, imported.text
            observation = (await client.get("/api/discovery/inventory")).json()
            recommendation = observation["recommendations"][0]
            assert recommendation["confidence"] == "hint"
            created = await client.post("/api/discovery/setups", headers={"Idempotency-Key": "setup"}, json={
                "source_id": "network_inventory", "candidate_id": recommendation["id"], "integration_domain": "hue"})
            assert created.status_code == 200, created.text
            path = "/api/discovery/setups/" + created.json()["id"]
            readback = (await client.get(path)).json()
            assert readback["candidate_state"] == "present"
            assert readback["integration_state"] == "unselected" and readback["options"] == []
            assert readback["devices"] == [] and readback["device_count"] is None
            assert readback["physical_verification"] == "not_performed"
            native = [item for item in readback["sources"] if item["location"] == "home_assistant"]
            assert all(item["state"] == "ok" and item["upstream_version"] == "2026.10.1" for item in native)
            rejected = await client.post(path + "/select", headers={"Idempotency-Key": "missing-entry"},
                json={"expected_revision": 1, "entry_id": "not-a-real-entry"})
            assert rejected.status_code == 409
            assert (await client.post("/api/discovery/inventory/clear", headers={"Idempotency-Key": "clear"},
                json={"expected_revision": observation["revision"]})).status_code == 200
            after_clear = (await client.get(path)).json()
            assert after_clear["candidate_state"] != "present"
            assert after_clear["integration_state"] == "unselected"
        assert await ha.ws("config_entries/get") == entries_before
        assert store.setting("policy") == policy_before
        assert not any(store.list(kind) for kind in ("proposal", "approval", "mcp_grant", "conversation"))
    finally:
        if app:
            await app.state.gateway.close()
        await ha.close()
        store.close()
