"""Outbound-only transports. Credentials never cross into an Agent job."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager

import aiohttp

from .durability import client_session, durable
from .models import digest

LINE_ACCOUNT_SCOPE = "notification_line_account"


def line_account_scope(store):
    value = store.setting(LINE_ACCOUNT_SCOPE)
    if (not isinstance(value, dict) or set(value) != {"account_hash", "generation"} or
        not isinstance(value.get("generation"), str) or
        re.fullmatch(r"[a-f0-9]{32}", value["generation"]) is None or
        (value.get("account_hash") is not None and
         (not isinstance(value["account_hash"], str) or
          re.fullmatch(r"[a-f0-9]{64}", value["account_hash"]) is None))):
        return None
    return value


class ChannelError(Exception):
    def __init__(self, message, code="unavailable", *, status=None, reason=None, detail=""):
        super().__init__(message)
        self.code = code
        self.status = status
        # `detail` is a bounded internal hint. Public clients only see `code` and `reason`.
        self.reason = code if reason is None else reason
        self.detail = detail


def telegram_failure(status, description=""):
    """Distinct Telegram connect codes. The provider description is not returned."""
    text = str(description or "").lower()
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None
    if status == 401:
        return ChannelError("權杖無效，請向 BotFather 確認後重新貼上。", "invalid_token", reason="invalid_token")
    if status == 429:
        return ChannelError("Telegram 暫時限制請求，請稍後再試。", "rate_limited", reason="rate_limited")
    if status == 409:
        if "webhook" in text:
            return ChannelError("這個 bot 已設定 webhook，請改用專用 bot 或先移除 webhook。",
                                "webhook_conflict", reason="webhook_active")
        return ChannelError("已有另一個程式在讀取這個 bot，請先停止它再連線。",
                            "webhook_conflict", reason="poller_conflict")
    return ChannelError("Telegram 暫時無法連線，請稍後再試。", "unavailable", reason="unavailable")


@asynccontextmanager
async def line_admin(store):
    # LINE.js uses an HttpOnly cookie AND matching Origin for its local admin socket.
    async with client_session(store, cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
        async with session.get("http://127.0.0.1:3789/", allow_redirects=False) as response:
            if response.status != 200:
                raise ChannelError("LINE.js 尚未啟動")
            await response.read()
        async with session.ws_connect("http://127.0.0.1:3789/ws", origin="http://127.0.0.1:3789",
                                      heartbeat=20, max_msg_size=262144) as socket:
            yield socket


async def json_request(session, method, url, **kwargs):
    async with session.request(method, url, allow_redirects=False, **kwargs) as response:
        if response.status >= 300:
            # URLs can contain Telegram credentials. Never surface raw HTTP exceptions.
            detail = ""
            try:
                raw = await response.content.read(2048)
                parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                description = parsed.get("description") if isinstance(parsed, dict) else ""
                if isinstance(description, str):
                    detail = description[:180]
            except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
                detail = ""
            raise ChannelError("通訊服務拒絕連線，請確認權杖、權限或速率限制", status=response.status, detail=detail)
        raw = bytearray()
        async for chunk in response.content.iter_chunked(16384):
            raw.extend(chunk)
            if len(raw) > 1024 * 1024:
                raise ChannelError("通訊服務回應超過上限")
        return json.loads(raw)


class LineTransport:
    def __init__(self, store, config):
        self.store, self.config, self.socket = store, config, None
        self.pending = {}
        self.notification_ready = False
        self.notification_scope = None

    async def _notification_auth(self, user_id):
        # Invalidate synchronous pre-send guards before any disk await. A normal
        # connection loss keeps the persisted account generation; an explicit
        # logout or changed/malformed account rotates it, including after restart.
        self.notification_ready = False
        self.notification_scope = None
        previous = line_account_scope(self.store)
        valid = isinstance(user_id, str) and re.fullmatch(r"u[a-f0-9]{32}", user_id) is not None
        account_hash = digest(user_id) if valid else (previous or {}).get("account_hash")
        scope = previous if valid and previous and previous["account_hash"] == account_hash else {
            "account_hash": account_hash, "generation": uuid.uuid4().hex}
        if scope != previous:
            self.store.set_setting(LINE_ACCOUNT_SCOPE, scope)
        await durable(self.store)
        self.notification_ready = bool(valid and line_account_scope(self.store) == scope)
        self.notification_scope = scope if self.notification_ready else None
        return self.notification_ready

    async def run(self, receive, state):
        me = None
        async with client_session(self.store, timeout=aiohttp.ClientTimeout(total=30), trust_env=False) as session:
            async with session.ws_connect("http://127.0.0.1:3789/api/ws", heartbeat=20, max_msg_size=262144,
                    headers={"Authorization": "Bearer " + self.config["secret"]}) as socket:
                self.socket = socket
                try:
                    async for message in socket:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        frame = message.json()
                        kind = frame.get("type")
                        if kind == "auth:ready":
                            profile = frame.get("profile")
                            me = profile.get("userId") if isinstance(profile, dict) else None
                            ready = await self._notification_auth(me)
                            if not ready:
                                me = None
                            state("connected" if ready else "needs_reauth")
                        elif kind == "auth:state" and frame.get("state") != "ready":
                            me = None
                            await self._notification_auth(None)
                            state("needs_reauth")
                        elif kind in {"sent", "error"}:
                            future = self.pending.get(frame.get("requestId"))
                            if future and not future.done():
                                future.set_result(kind == "sent")
                        elif kind == "message" and me:
                            item = frame.get("message", {})
                            if item.get("senderId") != me and not item.get("unsent"):
                                await receive(str(item.get("messageId", "")), str(item.get("channelId", "")),
                                              str(item.get("senderId", "")), item.get("text", ""))
                finally:
                    self.notification_ready = False
                    self.notification_scope = None
                    self.socket = None
                    for future in self.pending.values():
                        if not future.done():
                            future.set_result(False)

    async def send(self, chat, text, *, guard=None):
        if not self.socket or self.socket.closed:
            raise ChannelError("LINE 尚未連線")
        id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[id] = future
        try:
            await durable(self.store)
            if guard:
                guard()
            await self.socket.send_json({"type": "message:send", "requestId": id, "chatId": chat, "text": text[:1900]})
            if not await asyncio.wait_for(future, 20):
                raise ChannelError("LINE 未確認訊息送達")
        finally:
            self.pending.pop(id, None)


class TelegramTransport:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.base = "https://api.telegram.org/bot" + config["secret"]
        self.session = None

    async def _call(self, session, method, url, **kwargs):
        try:
            return await json_request(session, method, url, **kwargs)
        except ChannelError as exc:
            raise telegram_failure(exc.status, exc.detail) from exc

    async def run(self, receive, state):
        async with client_session(self.store, timeout=aiohttp.ClientTimeout(total=40), trust_env=False) as session:
            self.session = session
            result = await self._call(session, "POST", self.base + "/getMe")
            if not result.get("ok"):
                raise ChannelError("權杖無效，請向 BotFather 確認後重新貼上。", "invalid_token", reason="invalid_token")
            hook = await self._call(session, "POST", self.base + "/getWebhookInfo")
            if not hook.get("ok"):
                raise telegram_failure(hook.get("error_code"), str(hook.get("description") or "")[:180])
            if hook.get("result", {}).get("url"):
                raise ChannelError("這個 bot 已設定 webhook，請改用專用 bot 或先移除 webhook。",
                                   "webhook_conflict", reason="webhook_active")
            # Cursor is per credential so replacing a bot cannot inherit another bot's update IDs.
            from .models import digest
            key = "telegram_cursor_" + digest(self.config["secret"])
            offset = self.store.setting(key)
            if offset is None:
                # Bound the initial history while retaining a fresh pairing that
                # was followed by other messages before this poll completed.
                initial = await self._call(session, "POST", self.base + "/getUpdates",
                                           json={"offset": -100, "limit": 100, "timeout": 0,
                                                 "allowed_updates": ["message"]})
                if not initial.get("ok"):
                    raise telegram_failure(initial.get("error_code"), str(initial.get("description") or "")[:180])
                # A fresh pairing code can arrive while getMe/webhook checks run.
                # Preserve only this typed handshake for Gateway's current-code,
                # expiry and audience checks; ordinary history is still skipped.
                for update in initial.get("result", []):
                    message = update.get("message", {})
                    sender, text = message.get("from", {}), message.get("text", "")
                    if not sender.get("is_bot") and isinstance(text, str) and text.startswith("/pair "):
                        await receive(str(update["update_id"]), str(message.get("chat", {}).get("id", "")),
                                      str(sender.get("id", "")), text)
                offset = max((u["update_id"] + 1 for u in initial.get("result", [])), default=0)
                self.store.set_setting(key, offset)
            state("connected")
            while True:
                result = await self._call(session, "POST", self.base + "/getUpdates", json={
                    "offset": offset, "timeout": 25, "allowed_updates": ["message"], "limit": 50})
                if not result.get("ok"):
                    raise telegram_failure(result.get("error_code"), str(result.get("description") or "")[:180])
                state("connected")
                for update in result.get("result", []):
                    message = update.get("message", {})
                    sender = message.get("from", {})
                    if not sender.get("is_bot"):
                        await receive(str(update["update_id"]), str(message.get("chat", {}).get("id", "")),
                                      str(sender.get("id", "")), message.get("text", ""))
                    offset = update["update_id"] + 1
                    self.store.set_setting(key, offset)

    async def send(self, chat, text, *, guard=None):
        result = await self._call(self.session, "POST", self.base + "/sendMessage", json={
            "chat_id": chat, "text": text[:1900], "link_preview_options": {"is_disabled": True}},
            **({"trace_request_ctx": {"notification_guard": guard}} if guard else {}))
        if not result.get("ok"):
            code = result.get("error_code")
            if code:
                raise telegram_failure(code, str(result.get("description") or "")[:180])
            raise ChannelError("Telegram 未確認訊息送達")


class DiscordTransport:
    def __init__(self, store, config):
        self.store, self.config, self.session = store, config, None
        self.base = "https://discord.com/api/v10"

    async def run(self, receive, state):
        async with client_session(self.store, headers={"Authorization": "Bot " + self.config["secret"]},
                timeout=aiohttp.ClientTimeout(total=30), trust_env=False) as session:
            self.session = session
            me = await json_request(session, "GET", self.base + "/users/@me")
            if not me.get("bot"):
                raise ChannelError("請使用 Discord bot token")
            from .models import digest
            key = "discord_cursor_" + digest(self.config["secret"])
            cursors = self.store.setting(key, {})
            while True:
                for chat in self.config["chats"]:
                    params = {"limit": "100", "after": cursors[chat]} if chat in cursors else {"limit": "1"}
                    messages = await json_request(session, "GET", self.base + f"/channels/{chat}/messages", params=params)
                    if chat not in cursors:
                        # Discord snowflake at this instant prevents a newly empty channel replaying old posts later.
                        cursors[chat] = max((m["id"] for m in messages), key=int, default=str((int(time.time()*1000)-1420070400000)<<22))
                    else:
                        for message in sorted(messages, key=lambda m: int(m["id"])):
                            author = message.get("author", {})
                            if not author.get("bot") and not message.get("webhook_id"):
                                await receive(message["id"], chat, str(author.get("id", "")), message.get("content", ""))
                            cursors[chat] = message["id"]
                    self.store.set_setting(key, cursors)
                    state("connected")
                await asyncio.sleep(3)

    async def send(self, chat, text, *, guard=None):
        await json_request(self.session, "POST", self.base + f"/channels/{chat}/messages", json={
            "content": text[:1900], "allowed_mentions": {"parse": []}},
            **({"trace_request_ctx": {"notification_guard": guard}} if guard else {}))


TRANSPORTS = {"line": LineTransport, "telegram": TelegramTransport, "discord": DiscordTransport}
