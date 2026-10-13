import json

import httpx
import pytest
from agentic_bridge.app import MAX_REQUEST, Options, RelayError, create_app, load_options
from fastapi.responses import JSONResponse
from pydantic import ValidationError

OWNER = "a" * 32
HEADERS = {"x-remote-user-id": OWNER, "origin": "http://ha.test", "sec-fetch-site": "same-origin"}


class FakeRelay:
    def __init__(self):
        self.calls = []
        self.failed = False

    async def call(self, value):
        self.calls.append(value)
        if self.failed:
            raise RelayError()
        return JSONResponse({"accepted": True})


def client(app, peer="172.30.32.2"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
                             base_url="http://ha.test")


@pytest.mark.parametrize("peer", ["127.0.0.1", "192.168.177.1", "172.30.33.7"])
async def test_direct_clients_cannot_spoof_ingress_identity(peer):
    relay = FakeRelay()
    async with client(create_app(relay=relay), peer) as browser:
        response = await browser.get("/api/session", headers=HEADERS)
    assert response.status_code == 403 and not relay.calls


@pytest.mark.parametrize("identity", ["", "host-owner", "a" * 31, "G" * 32])
async def test_invalid_ingress_user_is_rejected(identity):
    relay = FakeRelay()
    async with client(create_app(relay=relay)) as browser:
        response = await browser.get("/api/session", headers={"x-remote-user-id": identity})
    assert response.status_code == 403 and not relay.calls


async def test_proxy_preserves_user_and_idempotency_without_browser_credentials():
    relay = FakeRelay()
    async with client(create_app(relay=relay)) as browser:
        response = await browser.post("/api/gateway/messages", headers={**HEADERS,
            "x-agentic-csrf": "csrf", "idempotency-key": "same-retry", "authorization": "Bearer browser-secret",
            "cookie": "host-session=never-forward"}, json={"message": "test", "coordinator": "hermes"})
    assert response.status_code == 200
    assert relay.calls == [{"user_id": OWNER, "method": "POST", "path": "/api/gateway/messages",
        "body": {"message": "test", "coordinator": "hermes"}, "csrf": "csrf", "idempotency_key": "same-retry",
        "origin": "http://ha.test", "host": "ha.test", "fetch_site": "same-origin"}]
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "frame-src" not in response.headers["content-security-policy"]


@pytest.mark.parametrize("headers", [
    {"origin": "http://evil.test"}, {"origin": "http://ha.test/path"},
    {"sec-fetch-site": "cross-site"}, {"content-type": "text/plain"}, {"origin": ""},
    {"origin": "http://[invalid"},
])
async def test_cross_origin_and_simple_post_are_rejected(headers):
    relay = FakeRelay()
    async with client(create_app(relay=relay)) as browser:
        response = await browser.post("/api/gateway/messages", headers={**HEADERS, **headers}, json={})
    assert response.status_code == 403 and not relay.calls


async def test_requests_cannot_reach_service_heartbeat_or_unbounded_payloads():
    relay = FakeRelay()
    async with client(create_app(relay=relay)) as browser:
        for path in ("/api/bridge/heartbeat", "/api/session?target=host", "/api/%2e%2e/host"):
            assert (await browser.get(path, headers=HEADERS)).status_code == 404
        result = await browser.post("/api/gateway/messages", headers=HEADERS, json={"message": "x" * MAX_REQUEST})
    assert result.status_code == 413 and not relay.calls


async def test_outer_offline_keeps_app_alive_and_exposes_no_secrets():
    relay = FakeRelay()
    relay.failed = True
    app = create_app(relay=relay)
    async with client(app) as browser:
        response = await browser.get("/api/session", headers=HEADERS)
        assert response.status_code == 503
        assert response.json() == {"detail": "暫時連不上外層管家，對話仍保留，請稍後重試。"}
        assert (await browser.get("/api/deployment", headers=HEADERS)).json() == {
            "mode": "bridge", "authenticated": False, "bridge_state": "unavailable"}
    async with client(app, "127.0.0.1") as local:
        assert (await local.get("/health")).status_code == 200


async def test_deployment_requires_outer_owner_validation_and_unpaired_is_not_ready():
    relay = FakeRelay()
    async with client(create_app(relay=relay)) as browser:
        await browser.get("/api/deployment", headers=HEADERS)
    assert relay.calls[0]["user_id"] == OWNER and relay.calls[0]["path"] == "/api/deployment"
    async with client(create_app()) as browser:
        assert (await browser.get("/api/deployment", headers=HEADERS)).json() == {
            "mode": "bridge", "authenticated": False, "bridge_state": "unpaired"}


@pytest.mark.parametrize("status,body,state,authenticated", [
    (200, {"mode": "bridge", "authenticated": True, "bridge_state": "ready"}, "ready", True),
    (401, {"detail": "bridge_unpaired"}, "unpaired", False),
    (403, {"detail": "bridge_owner_required"}, "owner_required", False),
    (403, {"detail": "bridge_authority_revoked"}, "owner_required", False),
    (503, {"detail": "bridge_owner_unavailable"}, "unavailable", False),
    (403, {"detail": "bridge_peer_invalid"}, "unavailable", False),
    (500, {"detail": "private diagnostic"}, "unavailable", False),
    (200, {"mode": "host", "authenticated": True}, "unavailable", False),
    (200, {"mode": "bridge", "authenticated": 1, "bridge_state": "ready"}, "unavailable", False),
])
async def test_first_page_gates_on_real_transport_result_without_exposing_details(status, body, state, authenticated):
    class RelayFixture:
        async def call(self, payload):
            return JSONResponse(body, status_code=status)
    async with client(create_app(relay=RelayFixture())) as browser:
        response = await browser.get("/api/deployment", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"mode": "bridge", "authenticated": authenticated, "bridge_state": state}


def test_options_restrict_destination_and_reject_partial_or_symlink_config(tmp_path):
    options = {"protocol": 1, "host": "192.168.177.1", "port": 8444, "fingerprint": "b" * 64,
               "token": "c" * 43, "pairing_id": "d" * 32}
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options))
    assert load_options(path) == Options(**options)
    for field, value in (("host", "attacker.test"), ("port", 8099), ("protocol", 2), ("fingerprint", ""),
                         ("protocol", True), ("protocol", 1.0), ("protocol", "1"), ("port", 8444.0)):
        with pytest.raises(ValidationError):
            Options(**{**options, field: value})
    link = tmp_path / "link.json"
    link.symlink_to(path)
    assert load_options(link) is None
    path.write_text('{"protocol":1}')
    assert load_options(path) is None
