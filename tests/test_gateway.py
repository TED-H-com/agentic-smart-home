"""Protocol fixtures: no real cloud logins or household changes are made by these tests."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.models import ChannelConfig
from agentic_controller.runner import Runner
from conftest import TARGET
from fastapi import HTTPException


class FixtureRunner:
    def __init__(self, scope):
        self.scope, self.jobs, self.calls = scope, [], []
        self.answer = {"message": scope + " reply"}

    async def run(self, job):
        self.jobs.append(job)
        return self.answer

    async def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"protocol": 1, "providers": [{"provider": "codex", "status": "connected"}], "status": "connected"}


@pytest.fixture
async def system(store, ha, tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    monkeypatch.setenv('AGENTIC_LINE_CONFIG', str(tmp_path/'line-config/gateway.json'))
    (tmp_path/'access-key').write_text('fixture-machine-access-key')
    outer, inner = FixtureRunner('host'), FixtureRunner('haos')
    async def offline():
        raise HTTPException(503, 'fixture offline')
    monkeypatch.setattr('agentic_controller.gateway.host_call', offline)
    app = create_app(store=store, ha=ha, runner=outer, inner_runner=inner, host_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099',
                                 headers={'Origin':'http://localhost:8099'}) as client:
        response = await client.post('/api/host/login', json={'key':'fixture-machine-access-key'})
        assert response.status_code == 200
        client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
        client.headers['Idempotency-Key'] = 'fixture-message'
        yield app, client, outer, inner
    await app.state.gateway.close()
    for task in list(app.state.running.values()):
        task.cancel()
    await asyncio.gather(*list(app.state.running.values()), return_exceptions=True)


async def drain(app):
    await asyncio.gather(*list(app.state.running.values()))


async def test_web_onboarding_and_chat_are_independent_of_ha(system, ha, store):
    app, client, outer, inner = system
    ha.revoked = True
    gateway = (await client.get('/api/gateway')).json()
    assert gateway['preferences']['primary'] == 'line'
    assert gateway['web']['always_available'] and not any(c['enabled'] for c in gateway['channels'])
    assert (await client.post('/api/gateway/onboarding', json={'step':2})).status_code == 200
    assert (await client.get('/api/gateway')).json()['step'] == 2
    response = await client.post('/api/gateway/messages', json={'message':'在 HA 關機時對話', 'provider':'grok'})
    assert response.status_code == 202
    await drain(app)
    items = (await client.get('/api/gateway/messages')).json()['messages']
    assert items[0]['answer'] == 'host reply' and len(outer.jobs) == 1 and not inner.jobs
    assert 'fixture-machine-access-key' not in outer.jobs[0]['prompt']
    assert not ha.writes
    assert (await client.post('/api/gateway/messages', json={'message':'在 HA 關機時對話', 'provider':'grok'})).status_code == 202
    assert (await client.post('/api/gateway/messages', json={'message':'不同內容'})).status_code == 409
    assert len(outer.jobs) == 1


async def test_inner_messages_use_inner_runner_and_still_require_exact_approval(system, store, ha):
    app, client, outer, inner = system
    store.set_setting('owner','owner')
    client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
    inner.answer = {'message':'提出開燈操作', 'action':{'entity_id':TARGET,'action':'turn_on'}}
    response = await client.post('/api/gateway/messages', json={'message':'開燈','runtime':'haos'})
    assert response.status_code == 202
    await drain(app)
    item = store.list('conversation')[0]
    assert item['state'] == 'awaiting_approval' and len(inner.jobs) == 1 and not outer.jobs
    assert not ha.writes and not ha.calls
    task = store.get('task',item['task_id'])
    assert task['runtime'] == 'haos' and store.get('proposal',task['proposal_id'])
    assert '批准' in item['answer']


async def test_runtime_credentials_are_separate_and_scoped(system):
    _, client, outer, inner = system
    response = await client.post('/api/runtimes/haos/providers/grok/auth', json={'method':'device_code'})
    assert response.status_code == 200 and not outer.calls
    assert inner.calls == [('POST','/providers/grok/auth',{'method':'device_code','secret':''})]
    await client.post('/api/runtimes/host/providers/agy/auth', json={'method':'api_key','secret':'fixture-key'})
    assert outer.calls[-1][2]['secret'] == 'fixture-key'
    assert len(inner.calls) == 1


async def test_channel_config_has_explicit_audience_and_never_returns_credentials(system):
    app, client, _, _ = system
    invalid = {'enabled':True,'secret':'123:fixture','chats':[],'senders':['123']}
    response = await client.post('/api/gateway/channels/telegram', json=invalid)
    assert response.status_code == 422 and '123:fixture' not in response.text
    # A disabled channel stores a key without starting any external connection.
    response = await client.post('/api/gateway/channels/telegram', json={**invalid,'enabled':False})
    assert response.status_code == 200 and '123:fixture' not in response.text
    assert app.state.gateway.config('telegram')['secret'] == '123:fixture'
    response = await client.post('/api/gateway/channels/telegram', json={'enabled':False})
    assert response.status_code == 200 and app.state.gateway.config('telegram')['secret'] == '123:fixture'
    client.headers['Origin'] = 'https://evil.example'
    assert (await client.post('/api/gateway/preferences', json={'primary':'web'})).status_code == 403


async def test_channel_filters_deduplicates_and_routes_back_to_same_conversation(system, store, monkeypatch):
    app, _, outer, inner = system
    ready = asyncio.Event()
    sent = []
    receive_handler = None
    class FixtureTransport:
        def __init__(self, store, config):
            pass
        async def run(self, receive, state):
            nonlocal receive_handler
            receive_handler = receive
            state('connected')
            ready.set()
            await asyncio.Future()
        async def send(self, chat, text):
            sent.append((chat,text))
    monkeypatch.setitem(__import__('agentic_controller.gateway',fromlist=['TRANSPORTS']).TRANSPORTS,'telegram',FixtureTransport)
    config = ChannelConfig(enabled=True,secret='fixture',chats=['room'],senders=['owner']).model_dump()
    store.set_setting('channel_telegram',config)
    await app.state.gateway.restart('telegram')
    await ready.wait()
    await receive_handler('1','room','intruder','ignored')
    await receive_handler('2','other-room','owner','ignored')
    await receive_handler('3','room','owner','/host hello')
    await receive_handler('3','room','owner','/host hello')
    await drain(app)
    assert len(outer.jobs) == 1 and not inner.jobs and sent == [('room','host reply')]
    item=store.list('conversation')[0]
    assert item['source'] == 'telegram' and item['delivery'] == 'sent'
    await receive_handler('4','room','owner','response must not reach changed audience')
    store.set_setting('channel_telegram',{**config,'senders':['someone-else']})
    await drain(app)
    assert len(sent) == 1 and store.list('conversation')[0]['delivery'] == 'not_sent'


async def test_restart_marks_incomplete_messages_and_does_not_replay(system, store):
    app, _, outer, _ = system
    store.put('conversation', {'id':'interrupted','state':'planning','created_at':'2026-10-11','source':'web'})
    await app.state.gateway.start()
    assert store.get('conversation','interrupted')['state'] == 'interrupted'
    assert not outer.jobs


async def test_pairing_uses_fixed_vm_and_does_not_replace_host_identity(store):
    outer = {'host':'localhost','fingerprint':'a'*64,'token':'outer-token','port':8443}
    store.set_setting('runner',outer)
    class PairRunner(Runner):
        async def call(self, method, path, data=None, pairing=None):
            assert pairing['host'] == '192.168.177.2'
            return {'protocol':1,'identity':'agentic-home-runner','token':'inner-token'}
    await PairRunner(store,'haos').pair({'host':'192.168.177.2','fingerprint':'b'*64,'code':'c'*32,'port':8443},'owner','d'*32)
    assert store.setting('runner') == outer and store.setting('runner_haos')['token'] == 'inner-token'


async def test_prune_removes_conversation_context(store):
    store.put('conversation',{'id':'old','message':'private context'})
    with store.db:
        store.db.execute("UPDATE records SET created='2000-01-01' WHERE kind='conversation'")
    store.prune()
    assert not store.list('conversation')


async def test_line_activation_writes_allowlist_before_requesting_token(system, tmp_path, monkeypatch):
    app, client, _, _ = system
    chat = 'c' + '1' * 32

    async def token():
        assert json.loads((tmp_path / 'line-config/gateway.json').read_text()) == {
            'enabled': True, 'chats': [chat]}
        return 'fixture-private-line-token'

    async def restart(name):
        assert name == 'line'

    monkeypatch.setattr(app.state.gateway, 'line_token', token)
    monkeypatch.setattr(app.state.gateway, 'restart', restart)
    response = await client.post('/api/gateway/channels/line', json={
        'enabled': True, 'chats': [chat], 'senders': ['u' + '2' * 32]})
    assert response.status_code == 200 and 'fixture-private-line-token' not in response.text
    assert app.state.gateway.config('line')['secret'] == 'fixture-private-line-token'


async def test_line_token_waits_for_reload_and_requests_only_once(system, monkeypatch):
    app, _, _, _ = system
    connections, requests = [], []

    class Socket:
        def __init__(self, enabled):
            self.enabled = enabled

        async def send_json(self, data):
            assert self.enabled
            requests.append(data)

        async def __aiter__(self):
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: {
                'type': 'api:state', 'enabled': self.enabled})
            if self.enabled:
                assert requests == [{'type': 'api:token:create'}]
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: {
                    'type': 'api:token', 'token': 'fixture-token'})

    @asynccontextmanager
    async def admin():
        connections.append(True)
        yield Socket(enabled=len(connections) > 1)

    monkeypatch.setattr('agentic_controller.gateway.line_admin', admin)
    assert await app.state.gateway.line_token() == 'fixture-token'
    assert len(connections) == 2
