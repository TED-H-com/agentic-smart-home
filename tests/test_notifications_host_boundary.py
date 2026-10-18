"""Host settings remain local, CSRF protected, and usable to stop notifications offline."""
import httpx
from agentic_controller.app import create_app


async def test_notification_settings_require_local_session_and_csrf(store, ha, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("fixture-local-notification-owner")
    app = create_app(store=store, ha=ha, host_mode=True)

    async def offline(*args):
        raise AssertionError("Stopping notifications must not require a live HA connection")

    ha.owner = offline
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://localhost:8099") as client:
        assert (await client.get("/api/notifications")).status_code == 401
        assert (await client.get("/api/notifications", headers={"X-Remote-User-Id": "owner"})).status_code == 401
        client.headers["Origin"] = "http://localhost:8099"
        assert (await client.post("/api/host/login", json={"key": "fixture-local-notification-owner"})).status_code == 200
        state = (await client.get("/api/notifications")).json()
        body = {"enabled": False, "expected_revision": state["config"]["revision"], "recipients": []}
        assert (await client.post("/api/notifications/config", json=body)).status_code == 403
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        assert (await client.post("/api/notifications/config", json=body,
                                  headers={"Origin": "https://other.invalid"})).status_code == 403
        saved = await client.post("/api/notifications/config", json=body)
        assert saved.status_code == 200
        assert saved.json()["config"]["enabled"] is False
        assert (await client.post("/api/notifications/config", json=body)).status_code == 409
        assert (await client.get("/api/notifications", headers={"Host": "other.invalid"})).status_code == 403


async def test_notifications_not_mounted_in_legacy_ingress_mode(store, ha):
    app = create_app(store=store, ha=ha, host_mode=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.30.32.2", 1234)),
                                base_url="http://ingress", headers={"X-Remote-User-Id": "owner"}) as client:
        assert (await client.get("/api/notifications")).status_code == 404
