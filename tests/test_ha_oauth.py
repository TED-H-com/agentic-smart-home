"""HA browser handoff: forged/replayed callbacks and actual isolated Core grants."""

import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiohttp
import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.ha import HomeAssistant
from agentic_controller.ha_oauth import HomeAuthorization
from agentic_controller.host import HostSession, connect_home_owner
from fastapi import HTTPException
from test_real_ha import real_container, wait_ready  # noqa: F401

ACCESS = "synthetic-ha-access-" + "a" * 25
REFRESH = "synthetic-ha-refresh-" + "b" * 25
PERSISTENT = "synthetic-ha-durable-" + "c" * 25
SIGNING_SECRET = "test-signing-secret"
SESSION_EXPIRES = str(int(time.time()) + 43200)


def request(cookie=None, host="localhost:8099", auth=None):
    if cookie is None:
        auth = auth or HostSession(Path("."), SIGNING_SECRET)
        cookie = SESSION_EXPIRES + "." + auth.sign(SESSION_EXPIRES)
    return SimpleNamespace(cookies={"agentic_host": cookie}, headers={"host": host})


class Candidate:
    def __init__(self, *args, **kwargs):
        self.calls, self.name = [], None
        self.is_owner = True

    async def start(self):
        pass

    async def close(self):
        pass

    async def owner(self, identifier):
        return {"id": "owner", "is_owner": True} if self.is_owner else None

    async def ws(self, command, **fields):
        self.calls.append((command, fields))
        if command == "auth/current_user":
            return {"id": "owner"}
        if command == "auth/long_lived_access_token":
            self.name = fields["client_name"]
            assert fields["lifespan"] == 365
            return PERSISTENT
        if command == "auth/refresh_tokens":
            own = [{"id": "new-token-id", "client_name": self.name, "type": "long_lived_access_token"}] if self.name else []
            return own + [{"id": "other-token-id", "client_name": "Another application", "type": "long_lived_access_token"}]
        if command == "auth/delete_refresh_token":
            assert fields["refresh_token_id"] == "new-token-id"
            return {}
        raise AssertionError(command)


@pytest.fixture
def authorization(store, tmp_path):
    auth = HostSession(tmp_path, SIGNING_SECRET)
    ha = HomeAssistant("", "http://fixture.invalid/api", "ws://fixture.invalid/api/websocket", external=True)
    candidate = Candidate()
    effects = []

    async def bind(auth, ha, store, token, *, commit_guard):
        assert token == PERSISTENT
        commit_guard()
        effects.append("bound")
        store.set_setting("owner", "owner")

    service = HomeAuthorization(auth, ha, store, factory=lambda *a, **kw: candidate, bind=bind)
    async def exchange(path, fields):
        effects.append(path)
        if path == "/auth/revoke":
            assert fields == {"token": REFRESH}
            return {}
        assert fields["client_id"] == "http://localhost:8099/"
        return {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer"}
    service.token_request = exchange
    return service, candidate, effects


def begin(service, req=None, key="onboarding-1"):
    result = service.start(req or request(auth=service.auth), key)
    parts = urlsplit(result["url"])
    assert parts.path == "/auth/authorize"
    return parse_qs(parts.query)["state"][0]


async def test_oauth_callback_binds_owner_once_without_persisting_codes_or_grants(authorization, store):
    service, candidate, effects = authorization
    state = begin(service)
    assert begin(service) == state  # Lost start response does not create another grant.
    result = await service.finish(request(), state, "one-use-code")
    assert result == {"state": "connected", "error_code": None}
    assert effects == ["/auth/token", "bound", "/auth/revoke"]
    assert not any(call[0] == "auth/delete_refresh_token" for call in candidate.calls)
    with pytest.raises(HTTPException):
        await service.finish(request(), state, "one-use-code")
    records = str(store.db.execute("SELECT body FROM records WHERE kind='ha_oauth'").fetchall())
    assert all(value not in records for value in (ACCESS, REFRESH, PERSISTENT, "one-use-code", "owner-session"))


@pytest.mark.parametrize("mutation", ["cookie", "host", "expired", "replaced", "logout", "owner_changed"])
async def test_callback_scope_and_expiry_fail_before_any_exchange(authorization, mutation):
    service, candidate, effects = authorization
    state = begin(service)
    req = request()
    record = service.store.get("ha_oauth", service.store.setting("ha_oauth_current"))
    if mutation == "cookie":
        req = request("different-session")
    elif mutation == "host":
        req = request(host="127.0.0.1:8099")
    elif mutation == "expired":
        record["expires"] = 0
        service.store.put("ha_oauth", record)
    elif mutation == "replaced":
        begin(service, key="onboarding-2")
    elif mutation == "logout":
        service.store.set_setting("hermes_web_epoch", "changed")
    else:
        service.store.set_setting("owner", "changed")
    with pytest.raises(HTTPException):
        await service.finish(req, state, "code")
    assert not effects and not candidate.calls


async def test_nonowner_is_rejected_and_temporary_grant_is_revoked(authorization):
    service, candidate, effects = authorization
    candidate.is_owner = False
    result = await service.finish(request(), begin(service), "code")
    assert result == {"state": "failed", "error_code": "ha_owner_required"}
    assert effects == ["/auth/token", "/auth/revoke"]
    assert not any(call[0] == "auth/long_lived_access_token" for call in candidate.calls)


async def test_failed_bind_removes_only_this_attempts_generated_token(authorization):
    service, candidate, effects = authorization
    async def fail(*args, **kwargs):
        raise RuntimeError(PERSISTENT)
    service.bind = fail
    result = await service.finish(request(), begin(service), "code")
    assert result == {"state": "failed", "error_code": "ha_authorization_failed"}
    assert ("auth/delete_refresh_token", {"refresh_token_id": "new-token-id"}) in candidate.calls
    assert effects[-1] == "/auth/revoke"


@pytest.mark.parametrize("phase", ["before_mint", "before_persist"])
@pytest.mark.parametrize("mutation", ["logout", "replaced", "owner_changed", "session_expired", "authorization_expired"])
async def test_authority_changes_during_await_prevent_mint_or_atomic_binding(
    authorization, tmp_path, monkeypatch, phase, mutation,
):
    service, candidate, effects = authorization
    req = request(auth=service.auth)
    state = begin(service, req)
    identifier = hashlib.sha256(state.encode()).hexdigest()
    entered, release = asyncio.Event(), asyncio.Event()
    previous_token = "previous-installed-token"
    token_path = tmp_path / "ha-token"
    token_path.write_text(previous_token)
    service.ha.token = previous_token

    if phase == "before_mint":
        exchange = service.token_request

        async def delayed_exchange(path, fields):
            if path == "/auth/token":
                entered.set()
                await release.wait()
            return await exchange(path, fields)

        service.token_request = delayed_exchange
    else:
        # Exercise the actual binding helper: its owner lookup yields after the
        # OAuth handler's final pre-bind check but before any credential write.
        verifier = Candidate()

        async def delayed_owner(identifier):
            entered.set()
            await release.wait()
            return {"id": "owner", "is_owner": True}

        verifier.owner = delayed_owner
        monkeypatch.setattr("agentic_controller.host.HomeAssistant", lambda *a, **kw: verifier)
        service.bind = connect_home_owner

    pending = asyncio.create_task(service.finish(req, state, "one-use-code"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if mutation == "logout":
            service.store.set_setting("hermes_web_epoch", "logged-out-during-exchange")
        elif mutation == "replaced":
            begin(service, req, key="replacement-login")
        elif mutation == "owner_changed":
            service.store.set_setting("owner", "different-owner")
        elif mutation == "session_expired":
            clock = SimpleNamespace(time=lambda: time.time() + 43201)
            monkeypatch.setattr("agentic_controller.host.time", clock)
        else:
            record = service.store.get("ha_oauth", identifier)
            record["expires"] = 0
            service.store.put("ha_oauth", record)
        release.set()
        await asyncio.wait_for(pending, timeout=2)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    record = service.store.get("ha_oauth", identifier)
    assert record["state"] == "failed"
    assert record["error_code"] in {"ha_authorization_expired", "ha_different_household", "ha_authorization_failed"}
    assert "bound" not in effects
    assert token_path.read_text() == previous_token and service.ha.token == previous_token
    assert service.store.setting("owner") == ("different-owner" if mutation == "owner_changed" else None)
    minted = any(call[0] == "auth/long_lived_access_token" for call in candidate.calls)
    deleted = [fields["refresh_token_id"] for command, fields in candidate.calls if command == "auth/delete_refresh_token"]
    assert minted is (phase == "before_persist")
    assert deleted == (["new-token-id"] if minted else [])
    assert effects == ["/auth/token", "/auth/revoke"]
    if mutation == "replaced":
        assert service.public() == {"state": "pending", "error_code": None}
    else:
        assert service.public()["state"] == "failed"


async def test_host_authorization_routes_require_cookie_csrf_and_fixed_host(store, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text("owner-fixture-key-" + "a" * 30)
    ha = HomeAssistant("", "http://fixture.invalid/api", "ws://fixture.invalid/api/websocket", external=True)
    app = create_app(store=store, ha=ha, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8099") as client:
        assert (await client.post("/api/host/authorize", json={})).status_code == 403
        client.headers["Origin"] = "http://localhost:8099"
        assert (await client.post("/api/host/authorize", json={})).status_code == 401
        await client.post("/api/host/login", json={"key": (tmp_path / "access-key").read_text()})
        assert (await client.post("/api/host/authorize", json={})).status_code == 403
        client.headers["X-Agentic-CSRF"] = (await client.get("/api/session")).json()["csrf"]
        client.headers["Idempotency-Key"] = "oauth-route-1"
        result = await client.post("/api/host/authorize", json={})
        assert result.status_code == 200
        assert urlsplit(result.json()["url"]).netloc == "localhost:8123"
        assert (await client.get("/api/host/authorization")).json()["state"] == "pending"
        assert (await client.post("/api/host/authorize", json={"url": "http://attacker"})).status_code == 422
        assert (await client.get("/api/host/authorized?state=forged&code=forged")).status_code == 400


async def test_real_core_oauth_to_revocable_persistent_owner_binding(real_container, tmp_path, store):  # noqa: F811
    _, base, _ = real_container
    await wait_ready(base)
    auth = HostSession(tmp_path, "isolated-core-oauth-secret")
    ha = HomeAssistant("", base + "/api", base.replace("http:", "ws:") + "/api/websocket", external=True)
    await ha.start()
    service = HomeAuthorization(auth, ha, store)
    state = begin(service)
    try:
        async with aiohttp.ClientSession() as session:
            # The native onboarding endpoint issues a real single-use code for
            # this client. No Owner household or cloud account is involved.
            async with session.post(base + "/api/onboarding/users", json={
                "client_id": "http://localhost:8099/", "name": "Isolated Owner", "username": "fixture-owner",
                "password": secrets.token_urlsafe(32), "language": "en"}) as response:
                assert response.status == 200
                code = (await response.json())["auth_code"]
        result = await service.finish(request(auth=auth), state, code)
        assert result["state"] == "connected", result
        assert (tmp_path / "ha-token").stat().st_mode & 0o777 == 0o600
        # Temporary OAuth grant was revoked; persisted token still works.
        user = await ha.ws("auth/current_user")
        assert await ha.owner(user["id"])
        assert (await ha.request("GET", "/config"))["version"] == "2026.10.1"
        tokens = await ha.ws("auth/refresh_tokens")
        assert len(tokens) == 1 and tokens[0]["type"] == "long_lived_access_token"
        assert tokens[0]["client_name"].startswith("Agentic Smart Home ")
        report = {"kind": "real_core_oauth_owner_binding", "runtime_location": "isolated_core_container",
                  "ha_version": "2026.10.1", "model_backend": "none", "device_kind": "none",
                  "authorization_code": "native_onboarding_endpoint", "one_year_named_token": "passed",
                  "owner_readback_after_temporary_grant_revoked": "passed", "token_permissions": "0600",
                  "browser_login_screen": "not_run", "owner_household": "not_run"}
        (Path(__file__).parents[1] / "evidence/ha-onboarding-oauth.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        await ha.close()
