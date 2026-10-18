"""Disk-backed notification state machines; no accounts, Agents or household effects."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from agentic_controller.channels import (
    LINE_ACCOUNT_SCOPE,
    DiscordTransport,
    LineTransport,
    TelegramTransport,
)
from agentic_controller.durability import client_session
from agentic_controller.gateway import Gateway
from agentic_controller.ha import HAError
from agentic_controller.models import ChannelConfig, digest
from agentic_controller.notifications import (
    CONFIG_KEY,
    DOMAIN,
    MAX_RECORDS,
    RECORD_KIND,
    NotificationConfigInput,
    NotificationManager,
    NotificationRevoked,
    routes,
)
from agentic_controller.store import Store
from aiohttp import web
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient


class NativeHA:
    def __init__(self):
        self.token, self.base, self.websocket = "private-ha-token", "http://ha/api", "ws://ha/websocket"
        self.valid_owner = True
        self.source_id, self.epoch = "a" * 32, "b" * 32
        self.calls, self.queue, self.claims, self.receipts = [], [], {}, {}
        self.lose_claim = self.lose_ack = False
        self.claim_result = None
        self.hook = None
        self.owner_hook = None
        self.error = None

    async def owner(self, user_id):
        if self.owner_hook:
            await self.owner_hook()
        return {"id": user_id, "is_owner": True, "is_active": True} if self.valid_owner else None

    async def ws(self, command, **body):
        self.calls.append((command, copy.deepcopy(body)))
        if self.error:
            raise self.error
        if self.hook:
            await self.hook(command, body)
        command = command.removeprefix(DOMAIN + "/")
        if command == "update_status":
            self.status = {"protocol": 1, "source_id": self.source_id, "epoch": self.epoch,
                "controller_id": body["controller_id"], "enabled": body["enabled"],
                "routing_revision": body["routing_revision"], "aliases": body["aliases"],
                "pending": len(self.queue), "last_delivery": None}
            return copy.deepcopy(self.status)
        if command == "pull":
            return {"requests": [copy.deepcopy(r) for r in self.queue
                if r["routing_revision"] == self.status["routing_revision"] and r["id"] not in self.claims][:8]}
        if command == "claim":
            if body["request_id"] in self.claims:
                return {"granted": False, "request": None}
            request = next(r for r in self.queue if r["id"] == body["request_id"])
            assert request["digest"] == body["digest"]
            self.claims[body["request_id"]] = copy.deepcopy(body)
            if self.lose_claim:
                raise HAError("private upstream failure with secret")
            return self.claim_result or {"granted": True, "request": copy.deepcopy(request)}
        if command == "ack":
            if body["request_id"] not in self.claims:
                return {"acknowledged": False}
            prior = self.receipts.setdefault(body["request_id"], copy.deepcopy(body))
            assert prior == body
            if self.lose_ack:
                self.lose_ack = False
                raise HAError("lost receipt response")
            return {"acknowledged": True}
        raise AssertionError(command)


class Sender:
    def __init__(self, store):
        self.store, self.sent = store, []
        self.config = store.setting("channel_telegram")
        self.error = self.hook = self.hold = None

    async def send(self, chat, message, *, guard=None):
        await self.store.durable()
        if self.hook:
            await self.hook()
        if guard:
            guard()
        self.sent.append((chat, message))
        if self.hold:
            await self.hold.wait()
        if self.error:
            raise self.error


@pytest.fixture
async def notifications(store, engine):
    ha = NativeHA()
    gateway = Gateway(store, {}, engine, None, {}, None)
    config = ChannelConfig(enabled=True, secret="private-bot-token", chats=["room", "other"],
                           senders=["sender"]).model_dump()
    store.set_settings({"owner": "owner", "channel_telegram": config,
        CONFIG_KEY: {"enabled": True, "recipients": [
            {"alias": "family", "label": "家人", "channel": "telegram", "chat": "room"}]}})
    sender = Sender(store)
    gateway.transports["telegram"] = sender
    gateway.states["telegram"] = "connected"
    manager = NotificationManager(store, ha, gateway, "c" * 32)

    def enqueue(**overrides):
        now = datetime.now(UTC)
        request = {"id": f"{len(ha.queue) + 1:032x}", "source_id": ha.source_id, "epoch": ha.epoch,
            "routing_revision": manager.revision(), "recipient": "family", "kind": "text",
            "message": "private notification: 門已關好", "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(), **overrides}
        request["digest"] = digest(request)
        ha.queue.append(request)
        return request

    yield SimpleNamespace(store=store, ha=ha, gateway=gateway, manager=manager, sender=sender,
                          enqueue=enqueue)
    if manager._task:
        manager._task.cancel()
        await asyncio.gather(manager._task, return_exceptions=True)


def receipt(system):
    return system.store.list(RECORD_KIND)[0]


async def test_one_shot_delivery_persists_before_claim_and_send_and_redacts_public(notifications):
    s = notifications
    request = s.enqueue()

    async def check_claim(command, body):
        if command.endswith("/claim"):
            with __import__("sqlite3").connect(s.store.path) as disk:
                raw = json.loads(disk.execute("SELECT body FROM records WHERE kind=?", (RECORD_KIND,)).fetchone()[0])
            assert raw["state"] == "claiming" and raw["claim_id"] == body["claim_id"]

    async def check_send():
        with __import__("sqlite3").connect(s.store.path) as disk:
            raw = json.loads(disk.execute("SELECT body FROM records WHERE kind=?", (RECORD_KIND,)).fetchone()[0])
        assert raw["state"] == "sending"

    s.ha.hook, s.sender.hook = check_claim, check_send
    await asyncio.gather(s.manager.advance(), s.manager.advance())
    assert s.sender.sent == [("room", request["message"])]
    assert receipt(s)["state"] == "submitted" and receipt(s)["acknowledged"]
    assert receipt(s)["message"] is None and receipt(s)["chat"] is None
    snapshot = s.manager.snapshot().model_dump()
    assert snapshot["integration"]["state"] == "ready"
    assert snapshot["channels"][1] == {"channel": "telegram", "transport": "connected", "login": "ready"}
    public = json.dumps(snapshot)
    assert "private" not in public and "claim_id" not in public and "request_digest" not in public
    assert not s.store.list("conversation") and not s.store.list("work_job")
    assert all(set(alias) == {"alias", "label", "channel", "capabilities"}
               for alias in s.ha.status["aliases"])


@pytest.mark.parametrize("failure", ["claim_lost", "claim_refused", "wrong_claim", "send_lost"])
async def test_ambiguous_claim_or_send_is_never_retried(notifications, failure):
    s = notifications
    request = s.enqueue()
    if failure == "claim_lost":
        s.ha.lose_claim = True
    elif failure == "claim_refused":
        s.ha.claim_result = {"granted": False, "request": None}
    elif failure == "wrong_claim":
        wrong = {**request, "message": "different"}
        wrong["digest"] = digest({k: v for k, v in wrong.items() if k != "digest"})
        s.ha.claim_result = {"granted": True, "request": wrong}
    else:
        s.sender.error = TimeoutError("private transport response was lost")
    await s.manager.advance()
    await s.manager.advance()
    assert receipt(s)["state"] == "unknown"
    assert len(s.sender.sent) == (1 if failure == "send_lost" else 0)
    assert len([c for c, _ in s.ha.calls if c.endswith("/claim")]) == 1
    assert "private" not in json.dumps(s.manager.snapshot().model_dump())


async def test_ack_lost_retries_only_identical_terminal_receipt(notifications):
    s = notifications
    s.enqueue()
    s.ha.lose_ack = True
    await s.manager.advance()
    assert not receipt(s)["acknowledged"]
    await s.manager.advance()
    acks = [body for command, body in s.ha.calls if command.endswith("/ack")]
    assert len(acks) == 2 and acks[0] == acks[1]
    assert receipt(s)["acknowledged"] and len(s.sender.sent) == 1


@pytest.mark.parametrize("change", ["owner", "token", "endpoint", "updater",
                                    "secret", "audience", "sender", "alias", "disabled", "adapter"])
async def test_reconfiguration_after_claim_fences_the_old_route(notifications, change):
    s = notifications
    s.enqueue()

    async def change_after_claim(command, body):
        if not command.endswith("/claim"):
            return
        setting = {"owner": ("owner", "other-owner"), "updater": ("updater_gate", {"id": "update"})}
        if change in setting:
            s.store.set_setting(*setting[change])
        elif change == "token":
            s.ha.token = "other-private-token"
        elif change == "endpoint":
            s.ha.websocket = "ws://other/websocket"
        elif change in {"secret", "audience", "sender"}:
            config = s.gateway.config("telegram")
            key, value = {"secret": ("secret", "other-token"), "audience": ("chats", ["other"]),
                          "sender": ("senders", ["other-sender"])}[change]
            s.store.set_setting("channel_telegram", {**config, key: value})
        elif change == "alias":
            config = s.store.setting(CONFIG_KEY)
            config["recipients"][0]["chat"] = "other"
            s.store.set_setting(CONFIG_KEY, config)
        elif change == "disabled":
            s.store.set_setting(CONFIG_KEY, {**s.store.setting(CONFIG_KEY), "enabled": False})
        else:
            s.gateway.transports["telegram"] = Sender(s.store)

    s.ha.hook = change_after_claim
    await s.manager.advance()
    assert not s.sender.sent and receipt(s)["state"] == "not_sent"
    await s.manager.advance()
    assert not s.sender.sent


async def test_owner_revocation_during_post_claim_proof_prevents_send(notifications):
    s = notifications
    s.enqueue()

    async def revoke(command, body):
        if command.endswith("/claim"):
            s.ha.valid_owner = False

    s.ha.hook = revoke
    await s.manager.advance()
    assert not s.sender.sent and receipt(s)["reason"] == "owner_unavailable"


async def test_internal_transport_barrier_also_rechecks_alias(notifications):
    s = notifications
    s.enqueue()

    async def retarget():
        config = s.store.setting(CONFIG_KEY)
        config["recipients"][0]["chat"] = "other"
        s.store.set_setting(CONFIG_KEY, config)

    s.sender.hook = retarget
    await s.manager.advance()
    assert not s.sender.sent and receipt(s)["state"] == "not_sent"


async def test_offline_adapter_leaves_request_unclaimed_and_later_sends_once(notifications):
    s = notifications
    s.enqueue()
    s.gateway.states["telegram"] = "connecting"
    await s.manager.advance()
    assert not s.ha.claims and not s.store.list(RECORD_KIND)
    s.gateway.states["telegram"] = "connected"
    await s.manager.advance()
    assert len(s.sender.sent) == 1


async def test_never_configured_installation_does_not_enroll_native_integration(notifications):
    s = notifications
    with s.store.transaction() as tx:
        tx.execute("DELETE FROM settings WHERE key=?", (CONFIG_KEY,))
    await s.manager.advance()
    assert not s.ha.calls and s.manager.snapshot().integration.state == "disabled"


@pytest.mark.parametrize("bad", ["digest", "media", "extra", "oversize", "timezone", "ttl"])
async def test_malformed_request_never_claimed(notifications, bad):
    s = notifications
    request = s.enqueue()
    if bad == "digest":
        request["digest"] = "0" * 64
    elif bad == "media":
        request["kind"] = "image"
    elif bad == "extra":
        request["url"] = "https://example.invalid/private"
    elif bad == "oversize":
        request["message"] = "x" * 1901
    elif bad == "timezone":
        request["created_at"] = "2026-10-17T12:00:00"
    else:
        request["expires_at"] = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    with pytest.raises(ValueError):
        await s.manager.advance()
    assert not s.ha.claims and not s.sender.sent


@pytest.mark.parametrize("field,value", [("source_id", "d" * 32), ("epoch", "d" * 32),
    ("recipient", "other_alias"), ("created_at", "2099-01-01T00:00:00+00:00")])
async def test_wrong_identity_or_future_request_never_claimed(notifications, field, value):
    s = notifications
    changes = {field: value}
    if field == "created_at":
        changes["expires_at"] = "2099-01-01T01:00:00+00:00"
    s.enqueue(**changes)
    await s.manager.advance()
    assert not s.ha.claims and not s.sender.sent


async def test_expired_request_and_changed_payload_for_old_id_never_replay(notifications):
    s = notifications
    past = datetime.now(UTC) - timedelta(hours=2)
    s.enqueue(created_at=past.isoformat(), expires_at=(past + timedelta(hours=1)).isoformat())
    await s.manager.advance()
    assert not s.ha.claims
    request = s.enqueue()
    await s.manager.advance()
    s.ha.claims.clear()  # Even a broken peer cannot cause local same-ID replay.
    request["message"] = "changed private payload"
    request["digest"] = digest({k: v for k, v in request.items() if k != "digest"})
    await s.manager.advance()
    assert len(s.sender.sent) == 1


@pytest.mark.parametrize("state", ["claiming", "sending"])
async def test_disk_restart_never_replays_uncertain_state(notifications, state, monkeypatch):
    s = notifications
    s.enqueue()
    await s.manager.advance()
    prior = {**receipt(s), "state": state, "message": "private replay", "chat": "room", "acknowledged": False}
    s.store.put(RECORD_KIND, prior)
    await s.store.durable()
    restarted_store = Store(s.store.path)
    restarted_gateway = Gateway(restarted_store, {}, s.gateway.engine, None, {}, None)
    restarted_gateway.transports["telegram"] = s.sender
    restarted_gateway.states["telegram"] = "connected"
    manager = NotificationManager(restarted_store, s.ha, restarted_gateway, "c" * 32)

    async def idle():
        await asyncio.Future()

    monkeypatch.setattr(manager, "_run", idle)
    try:
        await manager.start()
        assert restarted_store.list(RECORD_KIND)[0]["state"] == "unknown"
        assert restarted_store.list(RECORD_KIND)[0]["message"] is None
        # Do not ack a contradictory previously acknowledged fixture receipt.
        s.ha.receipts.clear()
        await manager.advance()
        assert len(s.sender.sent) == 1
    finally:
        await manager.close()
        restarted_store.close()


async def test_cancellation_during_send_stays_unknown(notifications):
    s = notifications
    s.enqueue()
    s.sender.hold = asyncio.Event()
    task = asyncio.create_task(s.manager.advance())
    while not s.sender.sent:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await s.manager.advance()
    assert receipt(s)["state"] == "unknown" and len(s.sender.sent) == 1


async def test_disk_failure_before_claim_prevents_any_external_effect(notifications, monkeypatch):
    s = notifications
    s.enqueue()

    async def fail(store):
        raise OSError("private disk failure")

    monkeypatch.setattr("agentic_controller.notifications.durable", fail)
    with pytest.raises(OSError):
        await s.manager.advance()
    assert not s.ha.claims and not s.sender.sent
    assert receipt(s)["state"] == "unknown"


@pytest.mark.parametrize("corrupt", ["settings", "record"])
async def test_corrupt_notification_storage_isolated_from_controller_start(notifications, corrupt):
    s = notifications
    if corrupt == "settings":
        s.store.set_setting(CONFIG_KEY, {"enabled": "yes", "secret": "private"})
    else:
        s.store.put(RECORD_KIND, {"id": "x", "private": "unrecognized record"})
    await s.manager.start()
    assert s.manager._task is None
    snapshot = s.manager.snapshot()
    assert snapshot.integration.state == "unavailable" and snapshot.integration.reason == "storage_invalid"
    await s.manager.advance()
    assert not s.ha.calls and not s.sender.sent
    assert "private" not in snapshot.model_dump_json()


async def test_compare_and_swap_validated_destinations_and_offline_disable(notifications):
    s = notifications
    before = s.manager.snapshot().config
    raw = {"enabled": True, "expected_revision": before.revision, "recipients": [
        {"alias": "family", "label": "家人", "channel": "telegram", "chat": "not_configured"}]}
    with pytest.raises(HTTPException) as exc:
        await s.manager.configure(NotificationConfigInput(**raw))
    assert exc.value.status_code == 422
    s.store.set_setting("channel_telegram", {**s.gateway.config("telegram"), "chats": ["other"]})
    with pytest.raises(HTTPException) as exc:
        await s.manager.configure(NotificationConfigInput(**{**raw, "enabled": False}))
    assert exc.value.status_code == 409
    s.ha.error = HAError("offline")
    snapshot = await s.manager.configure(NotificationConfigInput(enabled=False,
        expected_revision=s.manager.revision(), recipients=before.recipients))
    assert not snapshot.config.enabled and snapshot.config.recipients == before.recipients
    assert snapshot.integration.state == "disabled" and not s.ha.calls


async def test_config_requires_owner_before_enabling_and_rejects_extra_fields(notifications):
    s = notifications
    s.store.set_setting("owner", None)
    with pytest.raises(HTTPException) as exc:
        await s.manager.configure(NotificationConfigInput(enabled=True, recipients=[],
                                                        expected_revision=s.manager.revision()))
    assert exc.value.status_code == 422
    with pytest.raises(ValueError):
        NotificationConfigInput(enabled=True, recipients=[], expected_revision=s.manager.revision(), token="private")


async def test_routes_return_typed_snapshot_and_never_receive_arbitrary_notification(notifications):
    s = notifications
    app = FastAPI()
    routes(app, s.manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        before = (await client.get("/api/notifications")).json()
        response = await client.post("/api/notifications/config", json={"enabled": False,
            "expected_revision": before["config"]["revision"], "recipients": before["config"]["recipients"]})
        assert response.status_code == 200 and not response.json()["config"]["enabled"]
        assert (await client.post("/api/notifications/send", json={"message": "private"})).status_code == 404
    assert not s.ha.calls and not s.sender.sent


def test_line_transport_and_login_are_independent(notifications):
    s = notifications
    s.store.set_setting("channel_line", s.gateway.config("telegram"))
    s.gateway.transports["line"] = SimpleNamespace(socket=SimpleNamespace(closed=False))
    s.gateway.states["line"] = "needs_reauth"
    state = s.manager.channel_states()[0]
    assert state.transport == "connected" and state.login == "needs_login"
    s.gateway.transports["line"].socket.closed = True
    assert s.manager.channel_states()[0].transport == "error"


async def test_expired_terminal_receipt_obeys_context_retention_but_claim_tombstone_stays(notifications):
    s = notifications
    s.enqueue()
    await s.manager.advance()
    original = receipt(s)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with s.store.transaction() as tx:
        tx.execute("UPDATE records SET created=? WHERE kind=?", (old, RECORD_KIND))
    s.store.set_setting("retention_pending", {"context_days": 1, "audit_days": 30})
    s.store.prune()
    assert s.store.get(RECORD_KIND, original["id"])  # Still within the one-hour request lifetime.
    s.store.put(RECORD_KIND, {**original, "expires_at": old})
    s.store.prune()
    assert s.store.get(RECORD_KIND, original["id"]) is None


async def test_local_outbox_bound_never_evicts_unexpired_claims(notifications):
    s = notifications
    s.enqueue()
    await s.manager.advance()
    original = receipt(s)
    for number in range(MAX_RECORDS):
        s.store.put(RECORD_KIND, {**original, "id": f"{number:064x}"})
    s.enqueue()
    await s.manager.advance()
    assert len(s.sender.sent) == 1


@pytest.mark.parametrize("transport_type", [TelegramTransport, DiscordTransport])
async def test_actual_http_barrier_guard_blocks_before_network(store, transport_type, monkeypatch):
    seen = []

    async def handler(request):
        seen.append(await request.json())
        return web.json_response({"ok": True})

    server = web.Application()
    server.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    permitted = True

    async def barrier(store):
        nonlocal permitted
        await store.durable()
        permitted = False

    def guard():
        if not permitted:
            raise NotificationRevoked()

    monkeypatch.setattr("agentic_controller.durability.durable", barrier)
    transport = transport_type(store, {"secret": "fixture"})
    transport.base = f"http://127.0.0.1:{port}"
    try:
        async with client_session(store, trust_env=False) as session:
            transport.session = session
            with pytest.raises(NotificationRevoked):
                await transport.send("room", "message", guard=guard)
        assert not seen
    finally:
        await runner.cleanup()


async def test_actual_line_barrier_guard_prevents_frame(store, monkeypatch):
    seen = []

    class Socket:
        closed = False

        async def send_json(self, frame):
            seen.append(frame)

    transport = LineTransport(store, {"secret": "fixture"})
    transport.socket = Socket()
    allowed = True

    async def barrier(store):
        nonlocal allowed
        await store.durable()
        allowed = False

    def guard():
        if not allowed:
            raise NotificationRevoked()

    monkeypatch.setattr("agentic_controller.channels.durable", barrier)
    with pytest.raises(NotificationRevoked):
        await transport.send("room", "message", guard=guard)
    assert not seen and not transport.pending


@pytest.mark.parametrize("transport_type", [TelegramTransport, DiscordTransport])
async def test_http_connection_await_revocation_blocks_before_request_headers(store, transport_type):
    seen, checks = [], []
    acquired, release = asyncio.Event(), asyncio.Event()
    permitted = True

    async def handler(request):
        seen.append(await request.json())
        return web.json_response({"ok": True})

    class DelayedConnector(aiohttp.TCPConnector):
        async def connect(self, *args, **kwargs):
            connection = await super().connect(*args, **kwargs)
            acquired.set()
            await release.wait()
            return connection

    def guard():
        checks.append(permitted)
        if not permitted:
            raise NotificationRevoked()

    server = web.Application()
    server.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    transport = transport_type(store, {"secret": "fixture"})
    transport.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with client_session(store, connector=DelayedConnector(), trust_env=False) as session:
            transport.session = session
            task = asyncio.create_task(transport.send("room", "message", guard=guard))
            await asyncio.wait_for(acquired.wait(), 5)
            permitted = False
            release.set()
            with pytest.raises(NotificationRevoked):
                await task
        assert checks == [True, False] and not seen
    finally:
        release.set()
        await runner.cleanup()


async def test_slow_batch_refreshes_heartbeat_without_reclaim_or_resend(notifications, monkeypatch):
    s = notifications
    clock = [0.0]
    monkeypatch.setattr("agentic_controller.notifications.monotonic", lambda: clock[0])
    for _ in range(3):
        s.enqueue()

    async def slow_send():
        clock[0] += 45

    s.sender.hook = slow_send
    await s.manager.advance()
    assert clock[0] == 135 and len(s.sender.sent) == 3
    updates = [body for command, body in s.ha.calls if command.endswith("/update_status")]
    claims = [body for command, body in s.ha.calls if command.endswith("/claim")]
    assert len(updates) == 3 and len(claims) == 3
    assert len({body["request_id"] for body in claims}) == 3
    assert all(body == updates[0] for body in updates)
    await s.manager.advance()
    assert len(s.sender.sent) == 3
    clock[0] += 61
    assert s.manager.snapshot().integration.reason == "stale_heartbeat"


async def test_refresh_rebind_never_claims_old_pulled_batch(notifications, monkeypatch):
    s = notifications
    clock = [0.0]
    monkeypatch.setattr("agentic_controller.notifications.monotonic", lambda: clock[0])
    s.enqueue()
    s.enqueue()

    async def change_enrollment():
        clock[0] += 30
        s.ha.source_id, s.ha.epoch = "d" * 32, "e" * 32

    s.sender.hook = change_enrollment
    await s.manager.advance()
    assert len(s.sender.sent) == 1 and len(s.ha.claims) == 1
    assert len(s.store.list(RECORD_KIND)) == 1


@pytest.mark.parametrize("key,value", [("paused", True), ("hermes_auth_epoch", "changed"),
    ("hermes_web_epoch", "changed"), ("worker_auth_epoch", "changed"), ("policy", {"entities": []})])
async def test_native_notification_is_independent_of_ai_pause_and_logins(notifications, key, value):
    s = notifications
    request = s.enqueue()
    revision = s.manager.revision()
    s.store.set_setting(key, value)
    assert s.manager.revision() == revision
    await s.manager.advance()
    assert s.sender.sent == [("room", request["message"])]
    assert not s.store.list("conversation") and not s.store.list("work_job")


async def test_old_adapter_cannot_use_new_channel_configuration(notifications):
    s = notifications
    config = {**s.gateway.config("telegram"), "secret": "new-private-token"}
    s.store.set_setting("channel_telegram", config)
    s.enqueue()
    await s.manager.advance()
    assert not s.ha.claims and not s.sender.sent
    s.sender.config = config
    await s.manager.advance()
    assert len(s.sender.sent) == 1


@asynccontextmanager
async def line_runtime(store, config, monkeypatch, publish=None):
    class Socket:
        def __init__(self):
            self.frames, self.states, self.sent = asyncio.Queue(), asyncio.Queue(), []
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            frame = await self.frames.get()
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: frame)

        async def send_json(self, frame):
            self.sent.append(frame)
            await self.frames.put({"type": "sent", "requestId": frame["requestId"]})

        async def feed(self, frame, expected):
            await self.frames.put(frame)
            assert await asyncio.wait_for(self.states.get(), 5) == expected

    socket = Socket()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, *args, **kwargs):
            return socket

    def state(value):
        if publish:
            publish(value)
        socket.states.put_nowait(value)

    async def receive(*args):
        raise AssertionError("notification fixture cannot create conversations")

    monkeypatch.setattr("agentic_controller.channels.client_session", lambda *a, **k: Session())
    adapter = LineTransport(store, config)
    task = asyncio.create_task(adapter.run(receive, state))
    try:
        yield adapter, socket
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def ready(user_id):
    return {"type": "auth:ready", "profile": {"userId": user_id}}


def configure_line(system):
    config = system.gateway.config("telegram")
    system.store.set_setting("channel_line", config)
    settings = system.store.setting(CONFIG_KEY)
    settings["recipients"][0]["channel"] = "line"
    system.store.set_setting(CONFIG_KEY, settings)
    return config


async def test_line_account_logout_and_change_same_socket_fences_queued_requests(notifications, monkeypatch):
    s = notifications
    config = configure_line(s)
    async with line_runtime(s.store, config, monkeypatch,
                            lambda state: s.gateway.states.update(line=state)) as (adapter, socket):
        s.gateway.transports["line"] = adapter
        await socket.feed(ready("u" + "a" * 32), "connected")
        original = s.store.setting(LINE_ACCOUNT_SCOPE)
        s.enqueue()
        await socket.feed({"type": "auth:state", "state": "idle"}, "needs_reauth")
        logged_out = s.store.setting(LINE_ACCOUNT_SCOPE)
        assert original["generation"] != logged_out["generation"]
        await socket.feed(ready("u" + "b" * 32), "connected")
        assert s.store.setting(LINE_ACCOUNT_SCOPE)["account_hash"] == digest("u" + "b" * 32)
        await s.manager.advance()
        assert not s.ha.claims and not socket.sent
        s.enqueue()
        await s.manager.advance()
        assert len(socket.sent) == 1 and receipt(s)["state"] == "submitted"
        assert "u" + "a" * 32 not in json.dumps(s.store.setting(LINE_ACCOUNT_SCOPE))


async def test_line_auth_transition_fences_inflight_claim_before_transition_disk_finishes(notifications, monkeypatch):
    s = notifications
    config = configure_line(s)
    entered, release = asyncio.Event(), asyncio.Event()
    async with line_runtime(s.store, config, monkeypatch,
                            lambda state: s.gateway.states.update(line=state)) as (adapter, socket):
        s.gateway.transports["line"] = adapter
        await socket.feed(ready("u" + "a" * 32), "connected")
        s.enqueue()

        async def block_auth_disk(store):
            entered.set()
            await release.wait()
            await store.durable()

        async def logout_after_claim(command, body):
            if command.endswith("/claim"):
                await socket.frames.put({"type": "auth:state", "state": "idle"})
                await asyncio.wait_for(entered.wait(), 5)

        monkeypatch.setattr("agentic_controller.channels.durable", block_auth_disk)
        s.ha.hook = logout_after_claim
        try:
            await s.manager.advance()
            assert not socket.sent and receipt(s)["state"] == "not_sent"
            assert not adapter.notification_ready
            assert s.manager.channel_states()[0].login == "unknown"
        finally:
            release.set()
        assert await asyncio.wait_for(socket.states.get(), 5) == "needs_reauth"
        await socket.feed(ready("u" + "b" * 32), "connected")
        await s.manager.advance()
        assert not socket.sent


@pytest.mark.parametrize("second,changed", [("a", False), ("b", True)])
async def test_line_persisted_account_scope_survives_reconnect_and_detects_restart_change(
        notifications, monkeypatch, second, changed):
    s = notifications
    config = configure_line(s)
    async with line_runtime(s.store, config, monkeypatch,
                            lambda state: s.gateway.states.update(line=state)) as (adapter, socket):
        s.gateway.transports["line"] = adapter
        await socket.feed(ready("u" + "a" * 32), "connected")
        original = s.store.setting(LINE_ACCOUNT_SCOPE)
        s.enqueue()
    assert not adapter.notification_ready
    await s.store.durable()
    reopened = Store(s.store.path)
    gateway = Gateway(reopened, {}, s.gateway.engine, None, {}, None)
    manager = NotificationManager(reopened, s.ha, gateway, "c" * 32)
    try:
        async with line_runtime(reopened, config, monkeypatch,
                                lambda state: gateway.states.update(line=state)) as (adapter, socket):
            gateway.transports["line"] = adapter
            await socket.feed(ready("u" + second * 32), "connected")
            assert (reopened.setting(LINE_ACCOUNT_SCOPE) != original) is changed
            await manager.advance()
            assert len(socket.sent) == (0 if changed else 1)
            assert len(s.ha.claims) == (0 if changed else 1)
    finally:
        await manager.close()
        reopened.close()


@pytest.mark.parametrize("frame", [{"type": "auth:ready"}, ready(None), ready("me"),
                                   ready("u" + "a" * 33)])
async def test_line_malformed_ready_identity_fails_closed(store, monkeypatch, frame):
    async with line_runtime(store, {"secret": "fixture"}, monkeypatch) as (adapter, socket):
        await socket.feed(frame, "needs_reauth")
        assert not adapter.notification_ready and adapter.notification_scope is None
        assert store.setting(LINE_ACCOUNT_SCOPE)["account_hash"] is None
