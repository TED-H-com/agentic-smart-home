"""Actual native queue and Controller interoperate; only the external account is a fixture."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from agentic_controller.engine import Engine
from agentic_controller.gateway import Gateway
from agentic_controller.notifications import NotificationConfigInput, NotificationManager

OWNER = "a" * 32
CONTROLLER = "b" * 32
SOURCE = "c" * 32
EPOCH = "d" * 32


async def harness(store, tmp_path):
    source = Path(__file__).parents[1] / "custom_components/agentic_home_notifications/queue.py"
    spec = importlib.util.spec_from_file_location("native_notification_protocol_seam", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    saved = tmp_path / "native-queue.json"
    fault = SimpleNamespace(after_claim=False)

    async def save(data):
        saved.write_text(json.dumps(data))
        if fault.after_claim and any(item["claim"] for item in data["queue"]):
            fault.after_claim = False
            raise OSError("synthetic response loss after native claim persisted")

    queue = module.NotificationQueue(save)
    await queue.restore(SOURCE, EPOCH, None, allow_create=True)

    class HA:
        token, base, websocket = "synthetic-owner-token", "http://ha.invalid/api", "ws://ha.invalid/websocket"

        async def owner(self, identifier):
            return {"id": OWNER, "is_owner": True, "is_active": True} if identifier == OWNER else None

        async def ws(self, command, **fields):
            assert command.startswith("agentic_home_notifications/")
            return await getattr(queue, command.split("/")[1])(OWNER, **fields)

    class Sender:
        def __init__(self):
            self.messages = []
            self.socket = SimpleNamespace(closed=False)
            self.notification_ready = True
            self.config = None

        async def send(self, chat, text, *, guard):
            guard()
            self.messages.append((chat, text))

    ha, sender = HA(), Sender()
    store.set_setting("owner", OWNER)
    store.set_setting("channel_line", {"enabled": True, "secret": "synthetic-channel-token",
                                      "chats": ["original", "replacement"], "senders": ["synthetic-owner"]})
    store.set_setting("notification_line_account", {"account_hash": "f" * 64, "generation": "e" * 32})
    sender.config = store.setting("channel_line")
    sender.notification_scope = store.setting("notification_line_account")
    gateway = Gateway(store, {}, Engine(store, ha), None, {})
    gateway.transports["line"], gateway.states["line"] = sender, "connected"
    manager = NotificationManager(store, ha, gateway, CONTROLLER)
    config = {"enabled": True, "expected_revision": manager.revision(), "recipients": [
        {"alias": "family", "label": "家庭聊天室", "channel": "line", "chat": "original"}]}
    await manager.configure(NotificationConfigInput.model_validate(config))
    await manager.advance()
    return SimpleNamespace(queue=queue, manager=manager, sender=sender, config=config, saved=saved, fault=fault)


async def test_actual_queue_claim_send_ack_and_repeat_poll(store, tmp_path):
    system = await harness(store, tmp_path)
    request = await system.queue.enqueue(OWNER, "family", "燈已關閉；工程通知 fixture")
    assert request["state"] == "queued"
    await system.manager.advance()
    await system.manager.advance()
    assert system.sender.messages == [("original", "燈已關閉；工程通知 fixture")]
    receipt = system.queue.status(OWNER)["last_delivery"]
    assert receipt["request_id"] == request["request_id"] and receipt["state"] == "submitted"
    assert system.queue.status(OWNER)["pending"] == 0
    public = system.manager.snapshot().model_dump()
    assert public["deliveries"][0]["state"] == "submitted"
    assert "燈已關閉" not in json.dumps(public, ensure_ascii=False)
    record = store.list("notification_delivery")[0]
    assert record["acknowledged"] and record["message"] is None and record["chat"] is None


async def test_retarget_does_not_redirect_old_native_request(store, tmp_path):
    system = await harness(store, tmp_path)
    await system.queue.enqueue(OWNER, "family", "old destination only")
    config = copy.deepcopy(system.config)
    config["expected_revision"] = system.manager.revision()
    config["recipients"][0]["chat"] = "replacement"
    await system.manager.configure(NotificationConfigInput.model_validate(config))
    await system.manager.advance()
    assert not system.sender.messages
    assert system.queue.status(OWNER)["last_delivery"]["reason"] == "routing_changed"
    await system.queue.enqueue(OWNER, "family", "new destination")
    await system.manager.advance()
    assert system.sender.messages == [("replacement", "new destination")]


async def test_lost_native_claim_response_stays_unknown_after_queue_restart(store, tmp_path):
    system = await harness(store, tmp_path)
    await system.queue.enqueue(OWNER, "family", "never automatically retry")
    system.fault.after_claim = True
    await system.manager.advance()
    assert system.manager.snapshot().deliveries[0].state == "unknown"
    assert not system.sender.messages
    stored = json.loads(system.saved.read_text())
    # Reuse the adapter object with a freshly restored native queue, like HA restart.
    system.queue.healthy = True
    system.queue.last_heartbeat = None
    await system.queue.restore(SOURCE, EPOCH, stored)
    await system.manager.advance()
    await system.manager.advance()
    assert not system.sender.messages
    assert system.queue.status(OWNER)["last_delivery"]["state"] == "unknown"


async def test_offline_line_backlog_does_not_block_ready_telegram(store, tmp_path):
    system = await harness(store, tmp_path)
    gateway = system.manager.gateway
    store.set_setting("channel_telegram", {"enabled": True, "secret": "synthetic-telegram-token",
                                          "chats": ["telegram-chat"], "senders": ["synthetic-owner"]})
    telegram = type(system.sender)()
    telegram.config = store.setting("channel_telegram")
    gateway.transports["telegram"], gateway.states["telegram"] = telegram, "connected"
    gateway.states["line"] = "needs_reauth"
    system.sender.notification_ready = False
    config = copy.deepcopy(system.config)
    config["expected_revision"] = system.manager.revision()
    config["recipients"].append({"alias": "alerts", "label": "提醒", "channel": "telegram", "chat": "telegram-chat"})
    await system.manager.configure(NotificationConfigInput.model_validate(config))
    await system.manager.advance()
    for index in range(8):
        await system.queue.enqueue(OWNER, "family", f"offline LINE {index}")
    await system.queue.enqueue(OWNER, "alerts", "ready Telegram")
    await system.manager.advance()
    assert telegram.messages == [("telegram-chat", "ready Telegram")]
    assert not system.sender.messages
    assert system.queue.status(OWNER)["pending"] == 8
