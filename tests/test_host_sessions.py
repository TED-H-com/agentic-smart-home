"""Host cookies are individually revocable, durable, and bind browser CSRF."""

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from types import SimpleNamespace

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.host import HostSession
from agentic_controller.store import Store

KEY = "fixture-host-key-" + "a" * 32
ORIGIN = "http://localhost:8099"


def cookie(response):
    return SimpleCookie(response.headers["set-cookie"])["agentic_host"].value


def request(value):
    return SimpleNamespace(cookies={"agentic_host": value})


@pytest.fixture
def host_app(store, ha, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text(KEY)
    return create_app(store=store, ha=ha, host_mode=True)


@pytest.fixture
def failing_disk_app(ha, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path))
    (tmp_path / "access-key").write_text(KEY)
    store = Store(tmp_path / "controller.sqlite3")
    try:
        yield create_app(store=store, ha=ha, host_mode=True), store
    finally:
        if store.failed:
            with pytest.raises(RuntimeError, match="持久化失敗"):
                store.close()
        else:
            store.close()


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"})


async def login(browser):
    response = await browser.post("/api/host/login", json={"key": KEY})
    assert response.status_code == 200
    session = await browser.get("/api/session")
    assert session.status_code == 200
    browser.headers["X-Agentic-CSRF"] = session.json()["csrf"]
    return cookie(response)


async def test_logout_replay_denied_on_every_protected_route_and_after_restart(host_app, store, ha):
    async with client(host_app) as browser:
        saved = await login(browser)
        assert (await browser.get("/api/status")).status_code == 200
        response = await browser.post("/api/host/logout")
        assert response.status_code == 200 and response.json() == {"authenticated": False}
        removed = response.headers["set-cookie"].lower()
        assert "httponly" in removed and "samesite=strict" in removed and "path=/" in removed and "max-age=0" in removed
        assert not store.list("host_session")
        # Check the real on-disk DB before closing or flushing the memory Store.
        with sqlite3.connect(store.path) as disk:
            assert disk.execute("SELECT COUNT(*) FROM records WHERE kind='host_session'").fetchone()[0] == 0
        browser.headers["Cookie"] = "agentic_host=" + saved
        for path in ("/api/status", "/api/discovery", "/api/session"):
            assert (await browser.get(path)).status_code == 401
        assert (await browser.post("/api/pause", json={"paused": True})).status_code == 401
        assert (await browser.get("/api/deployment")).json()["authenticated"] is False

    # Reopen a separate Store from disk; process restart must not resurrect it.
    restored = Store(store.path)
    try:
        restarted = create_app(store=restored, ha=ha, host_mode=True)
        async with client(restarted) as browser:
            browser.headers["Cookie"] = "agentic_host=" + saved
            assert (await browser.get("/api/session")).status_code == 401
    finally:
        restored.close()


async def test_independent_logins_have_distinct_csrf_and_logout_only_revokes_its_cookie(host_app):
    async with client(host_app) as first, client(host_app) as second:
        a, b = await login(first), await login(second)
        assert a != b
        first_csrf, second_csrf = first.headers["X-Agentic-CSRF"], second.headers["X-Agentic-CSRF"]
        assert first_csrf != second_csrf
        response = await second.post("/api/pause", json={"paused": True}, headers={"X-Agentic-CSRF": first_csrf})
        assert response.status_code == 403
        assert (await second.post("/api/pause", json={"paused": True})).status_code == 200
        assert (await first.post("/api/host/logout")).status_code == 200
        assert (await second.get("/api/session")).status_code == 200
        assert (await second.post("/api/pause", json={"paused": False})).status_code == 200
        # A fresh login also cannot reuse a CSRF token from the previous login.
        fresh = await login(first)
        assert fresh != a
        assert (await first.post("/api/pause", json={"paused": True},
                                 headers={"X-Agentic-CSRF": first_csrf})).status_code == 403


async def test_issued_session_is_durable_and_still_valid_after_restart(host_app, store, ha):
    async with client(host_app) as browser:
        saved = await login(browser)
    with sqlite3.connect(store.path) as disk:
        records = disk.execute("SELECT body FROM records WHERE kind='host_session'").fetchall()
        assert len(records) == 1
        assert saved not in records[0][0] and KEY not in records[0][0]
        assert json.loads(records[0][0])["id"] == hashlib.sha256(saved.encode()).hexdigest()
    restored = Store(store.path)
    try:
        restarted = create_app(store=restored, ha=ha, host_mode=True)
        async with client(restarted) as browser:
            browser.headers["Cookie"] = "agentic_host=" + saved
            assert (await browser.get("/api/session")).status_code == 200
    finally:
        restored.close()


async def test_logout_revokes_before_waiting_for_coordinator_recovery(host_app, store):
    entered, release = asyncio.Event(), asyncio.Event()

    async def recover(**kwargs):
        entered.set()
        await release.wait()

    host_app.state.coordinator.recover = recover
    async with client(host_app) as browser, client(host_app) as replay:
        saved = await login(browser)
        replay.headers["Cookie"] = "agentic_host=" + saved
        logout = asyncio.create_task(browser.post("/api/host/logout"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not logout.done()
            assert (await replay.get("/api/session")).status_code == 401
            with sqlite3.connect(store.path) as disk:
                assert disk.execute("SELECT COUNT(*) FROM records WHERE kind='host_session'").fetchone()[0] == 0
        finally:
            release.set()
            response = await asyncio.wait_for(logout, timeout=2)
        assert response.status_code == 200


@pytest.mark.parametrize("route", ["login", "logout"])
async def test_success_response_waits_for_session_durability(host_app, store, monkeypatch, route):
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.durable

    async def delayed():
        entered.set()
        await release.wait()
        await original()

    async with client(host_app) as browser:
        if route == "logout":
            await login(browser)
        monkeypatch.setattr(store, "durable", delayed)
        pending = asyncio.create_task(browser.post("/api/host/" + route,
            **({"json": {"key": KEY}} if route == "login" else {})))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not pending.done()
        finally:
            release.set()
            response = await asyncio.wait_for(pending, timeout=2)
        assert response.status_code == 200


async def test_access_key_rotation_revokes_every_session_and_old_key_restore_does_not_revive(host_app, store, tmp_path):
    async with client(host_app) as first, client(host_app) as second:
        a, b = await login(first), await login(second)
        old_epoch = store.setting("hermes_web_epoch")
        (tmp_path / "access-key").write_text("replacement-host-key-" + "b" * 32)
        assert (await first.get("/api/session")).status_code == 401
        assert (await second.get("/api/session")).status_code == 401
        assert not store.list("host_session")
        with sqlite3.connect(store.path) as disk:
            assert disk.execute("SELECT COUNT(*) FROM records WHERE kind='host_session'").fetchone()[0] == 0
        assert store.setting("hermes_web_epoch") != old_epoch
        assert (await first.post("/api/host/login", json={"key": KEY})).status_code == 403
        (tmp_path / "access-key").write_text(KEY)
        for saved in (a, b):
            first.headers["Cookie"] = "agentic_host=" + saved
            assert (await first.get("/api/session")).status_code == 401
        first.headers.pop("Cookie")
        await login(first)


@pytest.mark.parametrize("action", ["login", "logout", "rotate"])
async def test_failed_disk_commit_never_claims_session_success(failing_disk_app, tmp_path, monkeypatch, action):
    from agentic_controller import store_journal
    host_app, store = failing_disk_app

    def fail():
        raise OSError("fixture disk failure")

    async with client(host_app) as browser:
        saved = await login(browser)
        monkeypatch.setattr(store_journal, "before_disk_commit", fail)
        if action == "rotate":
            (tmp_path / "access-key").write_text("replacement-access-key-" + "b" * 32)
            response = await browser.get("/api/session")
        else:
            response = await browser.post("/api/host/" + action,
                **({"json": {"key": KEY}} if action == "login" else {}))
        assert response.status_code == 503
        assert "set-cookie" not in response.headers
        assert store.failed
        browser.headers["Cookie"] = "agentic_host=" + saved
        # Existing read-only rescue remains available after a failed *new*
        # login, but a revocation already applied in memory remains revoked.
        assert (await browser.get("/api/session")).status_code == (200 if action == "login" else 401)


async def test_legacy_cookie_and_legacy_csrf_require_reauthentication(host_app, store):
    secret = store.setting("csrf_secret")
    expires = str(int(time.time()) + 43200)
    legacy = expires + "." + hmac.new(secret.encode(), expires.encode(), hashlib.sha256).hexdigest()
    async with client(host_app) as browser:
        browser.headers["Cookie"] = "agentic_host=" + legacy
        assert (await browser.get("/api/session")).status_code == 401
        browser.headers.pop("Cookie")
        await login(browser)
        expires = str(int(time.time()) + 3600)
        old_csrf = expires + "." + hmac.new(bytes.fromhex(secret),
            ("host-owner:" + expires).encode(), hashlib.sha256).hexdigest()
        assert (await browser.post("/api/host/logout", headers={"X-Agentic-CSRF": old_csrf})).status_code == 403
        assert (await browser.get("/api/session")).status_code == 200


@pytest.mark.parametrize("mutation", ["missing", "malformed", "signature", "unrecorded", "expired", "future", "key_missing", "rotation_with_failed_journal"])
def test_invalid_session_fails_closed(store, tmp_path, monkeypatch, mutation):
    from agentic_controller import host

    (tmp_path / "access-key").write_text(KEY)
    auth = HostSession(tmp_path, "fixture-signing-secret", store)
    saved = cookie(auth.login(KEY))
    assert auth.verify(request(saved))
    now = time.time()
    if mutation == "missing":
        saved = ""
    elif mutation == "malformed":
        saved += ".extra"
    elif mutation == "signature":
        saved = saved[:-1] + ("0" if saved[-1] != "0" else "1")
    elif mutation == "unrecorded":
        value = str(int(now) + 3600) + "." + secrets.token_urlsafe(32)
        saved = value + "." + auth.sign("host-session:v1:" + value)
    elif mutation == "expired":
        monkeypatch.setattr(host, "time", SimpleNamespace(time=lambda: now + auth.lifetime + 1))
    elif mutation == "future":
        monkeypatch.setattr(host, "time", SimpleNamespace(time=lambda: now - 100))
    elif mutation == "key_missing":
        (tmp_path / "access-key").unlink()
    else:
        (tmp_path / "access-key").write_text("replacement-access-key-" + "b" * 32)
        monkeypatch.setattr(Store, "failed", property(lambda self: True))
    assert not auth.verify(request(saved))


async def test_logout_rejects_wrong_origin_and_cross_site_without_revoking(host_app):
    async with client(host_app) as browser:
        await login(browser)
        for headers in ({"Origin": "http://evil.test"}, {"Sec-Fetch-Site": "cross-site"}, {"X-Agentic-CSRF": ""}):
            assert (await browser.post("/api/host/logout", headers=headers)).status_code == 403
            assert (await browser.get("/api/session")).status_code == 200


async def test_desktop_ticket_issues_the_same_revocable_session_once(host_app, tmp_path, store):
    ticket = secrets.token_urlsafe(32)
    file = tmp_path / "desktop-ticket.json"
    file.write_text(json.dumps({"ticket_hash": hashlib.sha256(ticket.encode()).hexdigest(), "expires": time.time() + 90}))
    file.chmod(0o600)
    async with client(host_app) as browser:
        response = await browser.post("/api/host/desktop-login", json={"ticket": ticket})
        assert response.status_code == 200
        saved = cookie(response)
        assert len(store.list("host_session")) == 1 and not file.read_text()
        assert (await browser.post("/api/host/desktop-login", json={"ticket": ticket})).status_code == 403
        browser.headers["X-Agentic-CSRF"] = (await browser.get("/api/session")).json()["csrf"]
        assert (await browser.post("/api/host/logout")).status_code == 200
        browser.headers["Cookie"] = "agentic_host=" + saved
        assert (await browser.get("/api/session")).status_code == 401
