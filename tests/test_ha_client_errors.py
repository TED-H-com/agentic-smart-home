"""Local transport fixtures; no real Home Assistant or accounts."""
import asyncio
import socket
from contextlib import asynccontextmanager

import aiohttp
import pytest
from agentic_controller.ha import HAError, HAUnreachable, HomeAssistant
from aiohttp import web


@asynccontextmanager
async def client(handler):
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ha = HomeAssistant('fixture', base=f'http://127.0.0.1:{port}',
                       websocket=f'ws://127.0.0.1:{port}/ws')
    await ha.start()
    try:
        yield ha
    finally:
        await ha.close()
        await runner.cleanup()


@pytest.mark.parametrize('method', ['request', 'ws'])
async def test_connection_refused_is_unreachable(method):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    ha = HomeAssistant('fixture', base=f'http://127.0.0.1:{port}',
                       websocket=f'ws://127.0.0.1:{port}/ws')
    await ha.start()
    try:
        with pytest.raises(HAUnreachable):
            await (ha.request('GET', '/') if method == 'request' else ha.ws('fixture'))
    finally:
        await ha.close()


@pytest.mark.parametrize('method', ['request', 'ws'])
async def test_timeout_is_unreachable(method):
    async def handler(request):
        if method == 'request':
            await asyncio.sleep(.1)
            return web.json_response({})
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'auth_required'})
        await ws.receive_json()
        await ws.send_json({'type': 'auth_ok'})
        await ws.receive_json()
        await asyncio.sleep(.1)
        await ws.close()
        return ws

    async with client(handler) as ha:
        with pytest.raises(HAUnreachable):
            await (ha.request('GET', '/', timeout=.01) if method == 'request'
                   else ha.ws('fixture', _receive_timeout=.01))


@pytest.mark.parametrize('reply', ['close', 'invalid', 'failure', 'auth', 'handshake'])
async def test_websocket_reply_classification(reply):
    async def handler(request):
        if reply == 'handshake':
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'auth_required'})
        await ws.receive_json()
        await ws.send_json({'type': 'auth_invalid' if reply == 'auth' else 'auth_ok'})
        if reply != 'auth':
            await ws.receive_json()
            if reply == 'invalid':
                await ws.send_str('invalid JSON')
            elif reply == 'failure':
                await ws.send_json({'id': 1, 'success': False, 'error': {'code': 'unknown_error'}})
        await ws.close()
        return ws

    async with client(handler) as ha:
        with pytest.raises(HAError) as error:
            await ha.ws('fixture')
        assert isinstance(error.value, HAUnreachable) is (reply == 'close')
        if reply == 'auth':
            assert error.value.status == 401
        if reply == 'handshake':
            assert error.value.status == 502


@pytest.mark.parametrize('status,body', [(200, 'invalid JSON'), (401, '{}')])
async def test_http_answers_are_plain_errors(status, body):
    async def handler(request):
        return web.Response(status=status, text=body)

    async with client(handler) as ha:
        with pytest.raises(HAError) as error:
            await ha.request('GET', '/')
        assert not isinstance(error.value, HAUnreachable)
        if status == 401:
            assert error.value.status == 401


async def test_handshake_transport_timeout_is_unreachable(monkeypatch):
    async def handler(request):
        return web.json_response({})

    async with client(handler) as ha:
        async def timeout(*args, **kwargs):
            raise aiohttp.ConnectionTimeoutError('fixture')
        monkeypatch.setattr(ha.session, 'ws_connect', timeout)
        with pytest.raises(HAUnreachable):
            await ha.ws('fixture')


async def test_supervisor_failure_answer_stays_plain_error():
    async def handler(request):
        return web.json_response({'result': 'error'})

    async with client(handler) as ha:
        ha.supervisor = ha.base
        with pytest.raises(HAError) as error:
            await ha.request('GET', '/', supervisor=True)
        assert not isinstance(error.value, HAUnreachable)


async def test_invalid_handshake_contract_stays_plain_error():
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({'type': 'unexpected'})
        await ws.close()
        return ws

    async with client(handler) as ha:
        with pytest.raises(HAError, match='握手無效') as error:
            await ha.ws('fixture')
        assert not isinstance(error.value, HAUnreachable)
