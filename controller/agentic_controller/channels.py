"""Outbound-only transports. Credentials never cross into an Agent job."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

import aiohttp


class ChannelError(Exception):
    pass


@asynccontextmanager
async def line_admin():
    # LINE.js uses an HttpOnly cookie AND matching Origin for its local admin socket.
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
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
            raise ChannelError("通訊服務拒絕連線，請確認權杖、權限或速率限制")
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

    async def run(self, receive, state):
        me = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=False) as session:
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
                            me = frame["profile"]["userId"]
                            state("connected")
                        elif kind == "auth:state" and frame.get("state") != "ready":
                            me = None
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
                    self.socket = None
                    for future in self.pending.values():
                        if not future.done():
                            future.set_result(False)

    async def send(self, chat, text):
        if not self.socket or self.socket.closed:
            raise ChannelError("LINE 尚未連線")
        id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[id] = future
        try:
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

    async def run(self, receive, state):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40), trust_env=False) as session:
            self.session = session
            result = await json_request(session, "POST", self.base + "/getMe")
            if not result.get("ok"):
                raise ChannelError("Telegram 登入失敗")
            hook = await json_request(session, "POST", self.base + "/getWebhookInfo")
            if hook.get("result", {}).get("url"):
                raise ChannelError("此 Telegram bot 已設定 webhook，請為家庭主機使用獨立 bot")
            state("connected")
            # Cursor is per credential so replacing a bot cannot inherit another bot's update IDs.
            from .models import digest
            key = "telegram_cursor_" + digest(self.config["secret"])
            offset = self.store.setting(key)
            if offset is None:
                # Start at the newest update and acknowledge it without replaying historical commands.
                initial = await json_request(session, "POST", self.base + "/getUpdates",
                                             json={"offset": -1, "limit": 1, "timeout": 0})
                offset = max((u["update_id"] + 1 for u in initial.get("result", [])), default=0)
                self.store.set_setting(key, offset)
            while True:
                result = await json_request(session, "POST", self.base + "/getUpdates", json={
                    "offset": offset, "timeout": 25, "allowed_updates": ["message"], "limit": 50})
                if not result.get("ok"):
                    raise ChannelError("Telegram 暫時無法讀取訊息")
                for update in result.get("result", []):
                    message = update.get("message", {})
                    sender = message.get("from", {})
                    if not sender.get("is_bot"):
                        await receive(str(update["update_id"]), str(message.get("chat", {}).get("id", "")),
                                      str(sender.get("id", "")), message.get("text", ""))
                    offset = update["update_id"] + 1
                    self.store.set_setting(key, offset)

    async def send(self, chat, text):
        result = await json_request(self.session, "POST", self.base + "/sendMessage", json={
            "chat_id": chat, "text": text[:1900], "link_preview_options": {"is_disabled": True}})
        if not result.get("ok"):
            raise ChannelError("Telegram 未確認訊息送達")


class DiscordTransport:
    def __init__(self, store, config):
        self.store, self.config, self.session = store, config, None
        self.base = "https://discord.com/api/v10"

    async def run(self, receive, state):
        async with aiohttp.ClientSession(headers={"Authorization": "Bot " + self.config["secret"]},
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

    async def send(self, chat, text):
        await json_request(self.session, "POST", self.base + f"/channels/{chat}/messages", json={
            "content": text[:1900], "allowed_mentions": {"parse": []}})


TRANSPORTS = {"line": LineTransport, "telegram": TelegramTransport, "discord": DiscordTransport}
