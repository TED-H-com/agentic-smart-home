"""Real loopback transport barriers, with synthetic Owner and HA responses."""
import asyncio

import aiohttp
import pytest
from agentic_controller.durability import client_session
from agentic_controller.ha import HomeAssistant
from aiohttp import web
from fastapi import HTTPException


async def server(application):
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def test_rest_guard_runs_after_connection_wait_and_prevents_post(store):
    posted = []
    allowed = True
    application = web.Application()
    async def handler(request):
        posted.append(await request.json())
        return web.json_response({})
    application.router.add_post("/confirm", handler)
    runner, port = await server(application)
    class RevokingConnector(aiohttp.TCPConnector):
        async def _create_connection(self, *args, **kwargs):
            nonlocal allowed
            connection = await super()._create_connection(*args, **kwargs)
            allowed = False
            return connection
    ha = HomeAssistant("fixture", base=f"http://127.0.0.1:{port}")
    ha.store = store
    ha.session = client_session(store, connector=RevokingConnector())
    calls = []
    async def guard():
        calls.append(allowed)
        if not allowed:
            raise HTTPException(403, "owner_revoked")
    try:
        with pytest.raises(HTTPException, match="owner_revoked"):
            await ha.request("POST", "/confirm", {}, send_guard=guard)
        assert calls == [True, False]
        assert posted == []
    finally:
        await ha.close()
        await runner.cleanup()


async def test_rest_guard_runs_after_durability_await(store, monkeypatch):
    posted = []
    allowed = True
    application = web.Application()
    async def handler(request):
        posted.append(await request.json())
        return web.json_response({})
    application.router.add_post("/confirm", handler)
    runner, port = await server(application)
    original = store.durable
    async def revoke_after_disk():
        nonlocal allowed
        await original()
        allowed = False
    monkeypatch.setattr(store, "durable", revoke_after_disk)
    ha = HomeAssistant("fixture", base=f"http://127.0.0.1:{port}")
    ha.store = store
    await ha.start()
    async def guard():
        if not allowed:
            raise HTTPException(403, "owner_revoked")
    try:
        with pytest.raises(HTTPException, match="owner_revoked"):
            await ha.request("POST", "/confirm", {}, send_guard=guard)
        assert posted == []
    finally:
        await ha.close()
        await runner.cleanup()


async def test_supervisor_websocket_guard_fences_backup_after_handshake_and_disk(store, monkeypatch):
    commands = []
    allowed = True
    application = web.Application()
    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        assert (await ws.receive_json())["type"] == "auth"
        await ws.send_json({"type": "auth_ok"})
        async for message in ws:
            commands.append(message.json())
        return ws
    application.router.add_get("/websocket", websocket)
    runner, port = await server(application)
    original = store.durable
    async def revoke_after_disk():
        nonlocal allowed
        await original()
        allowed = False
    monkeypatch.setattr(store, "durable", revoke_after_disk)
    ha = HomeAssistant("fixture", websocket=f"ws://127.0.0.1:{port}/websocket", external=True)
    ha.store = store
    await ha.start()
    async def guard():
        if not allowed:
            raise HTTPException(403, "owner_revoked")
    try:
        with pytest.raises(HTTPException, match="owner_revoked"):
            await ha.request("POST", "/backups/new/partial", {}, supervisor=True, send_guard=guard)
        await asyncio.sleep(0)
        assert commands == []
    finally:
        await ha.close()
        await runner.cleanup()
