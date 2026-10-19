"""Fixed Unix transport and no-replay guarantees against isolated socket peers."""
import asyncio
import errno
import socket
from http.cookies import SimpleCookie
from types import SimpleNamespace

import aiohttp
import pytest
from agentic_controller import host_updates
from agentic_controller.host import HostSession
from aiohttp import web
from fastapi import HTTPException


@pytest.fixture
async def peers(tmp_path, monkeypatch):
    paths = {'stable': tmp_path / 'stable.sock', 'legacy': tmp_path / 'legacy.sock'}
    state = {'stable_path': paths['stable'], 'replies': {'stable': (200, {'peer': 'stable'}), 'legacy': (200, {'peer': 'legacy'})}}
    calls, attempts, runners = [], [], []
    for name in ('stable', 'legacy'):
        async def handle(request, name=name):
            calls.append((name, request.method, request.path))
            reply = state['replies'][name]
            if callable(reply):
                return await reply(request)
            code, body = reply
            return web.json_response(body, status=code)
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', handle)
        runner = web.AppRunner(app, shutdown_timeout=.1)
        await runner.setup()
        await web.UnixSite(runner, str(paths[name])).start()
        runners.append(runner)
    connector = aiohttp.UnixConnector
    def fixed_connector(*, path):
        attempts.append(path)
        assert path in host_updates.HOST_UPDATE_SOCKETS
        target = state['stable_path'] if path == host_updates.HOST_UPDATE_SOCKETS[0] else paths['legacy']
        return connector(path=str(target))
    monkeypatch.setattr(host_updates.aiohttp, 'UnixConnector', fixed_connector)
    try:
        yield state, calls, attempts
    finally:
        for runner in runners:
            await runner.cleanup()


@pytest.mark.parametrize('method,path', [('GET', '/host-updates'), ('POST', '/host-updates/check')])
async def test_stable_socket_is_preferred_and_legacy_is_never_contacted(peers, store, method, path):
    _, calls, attempts = peers
    result = await host_updates.broker_call(method, path, {} if method == 'POST' else None, store=store)
    assert result == {'peer': 'stable'}
    assert attempts == [host_updates.HOST_UPDATE_SOCKETS[0]]
    assert calls == [('stable', method, path)]


@pytest.mark.parametrize('failure', ['absent', 'refused'])
async def test_only_absent_or_refused_stable_socket_selects_fixed_legacy_before_transmission(peers, store, tmp_path, failure):
    state, calls, attempts = peers
    target = tmp_path / (failure + '.sock')
    if failure == 'refused':
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(target))
    state['stable_path'] = target
    assert await host_updates.broker_call('POST', '/host-updates/check', {}, store=store) == {'peer': 'legacy'}
    assert attempts == list(host_updates.HOST_UPDATE_SOCKETS)
    assert calls == [('legacy', 'POST', '/host-updates/check')]


@pytest.mark.parametrize('method', ['GET', 'POST'])
async def test_lost_response_sends_exactly_one_request_and_never_falls_back(peers, store, method):
    state, calls, attempts = peers
    async def lose(request):
        request.transport.abort()
        return web.json_response({'accepted': True})
    state['replies']['stable'] = lose
    with pytest.raises(HTTPException) as error:
        await host_updates.broker_call(method, '/host-updates/check' if method == 'POST' else '/host-updates',
                                       {} if method == 'POST' else None, store=store)
    assert error.value.status_code == 503
    assert len(calls) == 1 and calls[0][0] == 'stable'
    assert attempts == [host_updates.HOST_UPDATE_SOCKETS[0]]


@pytest.mark.parametrize('code', [302, 403, 404, 409, 500, 503])
async def test_http_errors_never_select_a_different_peer(peers, store, code):
    state, calls, attempts = peers
    state['replies']['stable'] = (code, {'detail': 'host_update_rejected'})
    with pytest.raises(HTTPException) as error:
        await host_updates.broker_call('POST', '/host-updates/apply', {}, store=store)
    assert error.value.status_code == code
    assert attempts == [host_updates.HOST_UPDATE_SOCKETS[0]] and len(calls) == 1


async def test_timeout_after_admission_never_falls_back(peers, store, monkeypatch):
    state, calls, attempts = peers
    ended = asyncio.Event()
    async def delayed(request):
        await ended.wait()
        return web.json_response({'accepted': True})
    state['replies']['stable'] = delayed
    session = host_updates.client_session
    def short_session(store, **kwargs):
        kwargs['timeout'] = aiohttp.ClientTimeout(total=.03)
        return session(store, **kwargs)
    monkeypatch.setattr(host_updates, 'client_session', short_session)
    try:
        with pytest.raises(HTTPException) as error:
            await host_updates.broker_call('POST', '/host-updates/apply', {}, store=store)
        assert error.value.status_code == 503 and len(calls) == 1
        assert attempts == [host_updates.HOST_UPDATE_SOCKETS[0]]
    finally:
        ended.set()


@pytest.mark.parametrize('reply', ['malformed', 'oversized', 'array'])
async def test_ambiguous_replies_never_retry_on_legacy(peers, store, reply):
    state, calls, attempts = peers
    async def response(request):
        return web.Response(text='{' if reply == 'malformed' else ' ' * 262145 if reply == 'oversized' else '[]')
    state['replies']['stable'] = response
    with pytest.raises(HTTPException):
        await host_updates.broker_call('POST', '/host-updates/apply', {}, store=store)
    assert len(calls) == 1 and attempts == [host_updates.HOST_UPDATE_SOCKETS[0]]


@pytest.mark.parametrize('problem', [errno.EACCES, errno.EIO, errno.ETIMEDOUT])
async def test_other_connector_errors_are_not_absence(peers, store, monkeypatch, problem):
    _, calls, attempts = peers
    real_session = host_updates.client_session
    def failing_session(store, **kwargs):
        trace = aiohttp.TraceConfig()
        async def fail(session, context, params):
            raise aiohttp.ClientConnectorError(None, OSError(problem, 'fixture-connect-error'))
        trace.on_connection_create_start.append(fail)
        kwargs['trace_configs'].append(trace)
        return real_session(store, **kwargs)
    monkeypatch.setattr(host_updates, 'client_session', failing_session)
    with pytest.raises(HTTPException):
        await host_updates.broker_call('POST', '/host-updates/apply', {}, store=store)
    assert not calls and attempts == [host_updates.HOST_UPDATE_SOCKETS[0]]


@pytest.mark.parametrize('when', ['durability', 'connected'])
async def test_real_revocable_session_is_checked_after_last_transport_await(peers, store, tmp_path, monkeypatch, when):
    _, calls, attempts = peers
    (tmp_path / 'access-key').write_text('fixture-local-host-access-key')
    auth = HostSession(tmp_path, 'fixture-signing-secret', store)
    cookie = SimpleCookie(auth.cookie().headers['set-cookie'])['agentic_host'].value
    request = SimpleNamespace(state=SimpleNamespace(), cookies={'agentic_host': cookie})
    operations = host_updates.HostUpdates(store, auth, host_mode=True)
    authority = operations.owner(request)
    if when == 'durability':
        original = store.durable
        async def revoke():
            auth.revoke(request)
            await original()
        monkeypatch.setattr(store, 'durable', revoke)
    else:
        real_session = host_updates.client_session
        def revoked_session(store, **kwargs):
            trace = aiohttp.TraceConfig()
            async def revoke(session, context, params):
                auth.revoke(request)
            trace.on_connection_create_end.append(revoke)
            kwargs['trace_configs'].append(trace)
            return real_session(store, **kwargs)
        monkeypatch.setattr(host_updates, 'client_session', revoked_session)
    with pytest.raises(HTTPException) as error:
        await host_updates.broker_call('POST', '/host-updates/apply', {}, store=store,
                                       commit_guard=lambda: operations.guard(request, authority))
    assert error.value.status_code == 403 and not calls
    assert attempts == [host_updates.HOST_UPDATE_SOCKETS[0]]
