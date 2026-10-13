"""Protocol fixtures: no real cloud logins or household changes are made by these tests."""
import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.models import ChannelConfig, ChatInput, canonical
from agentic_controller.runner import Runner, RunnerError
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


async def test_pairing_receiver_binds_one_verified_sender_without_pre_auth_model_work(system, store, monkeypatch):
    app, client, outer, _ = system
    gateway = app.state.gateway
    ready, hold = asyncio.Event(), asyncio.Event()
    receiver = None
    class Transport:
        def __init__(self, store, config):
            self.config = config
        async def run(self, receive, state):
            nonlocal receiver
            receiver = receive
            state("connected")
            ready.set()
            await hold.wait()
        async def send(self, chat, text):
            assert chat == '-10001' and text == 'host reply'
    monkeypatch.setitem(__import__('agentic_controller.gateway', fromlist=['TRANSPORTS']).TRANSPORTS, 'telegram', Transport)
    store.set_setting('gateway', {'primary': 'telegram', 'runtime': 'host', 'provider': 'codex', 'coordinator': 'direct'})
    response = await client.post('/api/gateway/channels/telegram/pairing', json={'secret': '12345:fixture-private-token'})
    assert response.status_code == 202
    pairing = response.json()
    await ready.wait()
    assert pairing['state'] == 'pending' and len(pairing['code']) >= 24
    before = store.db.total_changes
    assert (await client.get('/api/gateway/channels/telegram/pairing')).json() == pairing
    assert store.db.total_changes == before
    await receiver('event1', '-10001', '42', 'untrusted pre-authorization request')
    await receiver('event2', '-10001', '42', '/pair wrong-code')
    await receiver('event3', '-10001', '42', '/pair 非ASCII')
    assert not store.list('conversation') and not outer.jobs
    await receiver('event4', '-10001', '42', '/pair ' + pairing['code'])
    assert gateway.pairing('telegram')['state'] == 'paired'
    assert gateway.config('telegram')['chats'] == ['-10001'] and gateway.config('telegram')['senders'] == ['42']
    await receiver('event4', '-10002', '43', '/pair ' + pairing['code'])
    assert gateway.config('telegram')['senders'] == ['42']
    await receiver('event5', '-10001', '43', 'unauthorized sender')
    assert not store.list('conversation')
    await receiver('event6', '-10001', '42', 'authorized request')
    await drain(app)
    assert len(outer.jobs) == 1
    assert pairing['code'] not in outer.jobs[0]['prompt'] and 'fixture-private-token' not in outer.jobs[0]['prompt']
    assert pairing['code'] not in (await client.get('/api/gateway')).text
    # A replay on the old credential must never cause its receiver to adopt a
    # newly configured credential/audience before that transport is replaced.
    store.set_setting('channel_telegram', {**gateway.config('telegram'), 'secret': '12345:rotated'})
    await receiver('event7', '-10001', '42', '/pair ' + pairing['code'])
    await receiver('event8', '-10001', '42', 'stale transport after token rotation')
    assert len(outer.jobs) == 1 and len(store.list('conversation')) == 1


async def test_pairing_expiry_timer_stops_receiver_and_clears_code(system, store, monkeypatch):
    import time
    app, client, _, _ = system
    gateway = app.state.gateway
    restarts = []
    async def restart(name):
        restarts.append(name)
    monkeypatch.setattr(gateway, 'restart', restart)
    pairing = (await client.post('/api/gateway/channels/telegram/pairing', json={'secret': '12345:fixture'})).json()
    pending = store.setting('channel_pairing_telegram')
    store.set_setting('channel_pairing_telegram', {**pending, 'expires_at': time.time() + .01})
    gateway.pair_timer('telegram')
    await gateway.pairing_timers['telegram']
    assert gateway.pairing('telegram')['state'] == 'expired'
    assert store.setting('channel_pairing_telegram')['code'] is None
    assert restarts == ['telegram', 'telegram']
    assert gateway.consume_pairing('telegram', 'late', '123', '456', '/pair ' + pairing['code'], gateway.config('telegram')) == 'ignored'


async def test_pending_pairing_survives_gateway_restart_without_authorizing_messages(system, store, monkeypatch):
    from agentic_controller.gateway import Gateway
    from agentic_controller.models import ChannelPairingInput
    app, _, _, _ = system
    gateway = app.state.gateway
    ready = asyncio.Event()
    class Transport:
        def __init__(self, store, config):
            self.config = config
        async def run(self, receive, state):
            state('connected')
            ready.set()
            await asyncio.Event().wait()
    monkeypatch.setitem(__import__('agentic_controller.gateway', fromlist=['TRANSPORTS']).TRANSPORTS, 'telegram', Transport)
    first = await gateway.begin_pairing('telegram', ChannelPairingInput(secret='12345:fixture'), 'restart-key')
    await gateway.close()
    ready.clear()
    restored = Gateway(store, gateway.runners, gateway.engine, gateway.plan_home, {}, None)
    try:
        await restored.start()
        async with asyncio.timeout(1):
            await ready.wait()
        assert restored.pairing('telegram') == first
        assert not restored.config('telegram')['enabled'] and not restored.config('telegram')['senders']
        assert 'telegram' in restored.workers and not store.list('conversation')
    finally:
        await restored.close()


async def test_pairing_reset_expiry_and_preferences_revoke_old_code(system, store, monkeypatch):
    import time
    app, client, _, _ = system
    gateway = app.state.gateway
    async def no_transport(name):
        return None
    monkeypatch.setattr(gateway, 'restart', no_transport)
    first = (await client.post('/api/gateway/channels/telegram/pairing', json={'secret': '12345:fixture'})).json()
    config = gateway.config('telegram')
    assert (await client.post('/api/gateway/channels/telegram/pairing', json={'secret': '12345:fixture'})).json() == first
    second = (await client.post('/api/gateway/channels/telegram/pairing', json={},
                               headers={'Idempotency-Key': 'replacement'})).json()
    assert second['code'] != first['code']
    assert gateway.consume_pairing('telegram', 'a', '123', '4', '/pair ' + first['code'], config) == 'ignored'
    pending = store.setting('channel_pairing_telegram')
    store.set_setting('channel_pairing_telegram', {**pending, 'expires_at': time.time() - 1})
    before = store.db.total_changes
    assert (await client.get('/api/gateway/channels/telegram/pairing')).json()['state'] == 'expired'
    assert store.db.total_changes == before
    assert gateway.consume_pairing('telegram', 'b', '123', '4', '/pair ' + second['code'], config) == 'ignored'
    await client.post('/api/gateway/channels/telegram/pairing', json={}, headers={'Idempotency-Key': 'fresh'})
    store.set_setting('gateway', {'primary': 'telegram'})
    await client.post('/api/gateway/preferences', json={'primary': 'web'})
    assert gateway.pairing('telegram')['state'] == 'cancelled'
    assert gateway.config('telegram')['enabled'] is False


async def test_line_pairing_requires_observed_chat_and_never_widens_upstream_allowlist(system, store, monkeypatch, tmp_path):
    app, client, _, _ = system
    gateway = app.state.gateway
    chat, sender = 'c' + '1' * 32, 'u' + '2' * 32
    async def no_transport(name):
        return None
    async def token():
        return 'fixture-private-line-token'
    monkeypatch.setattr(gateway, 'restart', no_transport)
    monkeypatch.setattr(gateway, 'line_token', token)
    assert (await client.post('/api/gateway/channels/line/pairing', json={'chat': chat})).status_code == 422
    gateway.line_login = {'status': 'ready', 'channels': [{'channelId': chat, 'name': 'Owner selected chat'}]}
    assert (await client.post('/api/gateway/channels/line/pairing', json={'chat': 'c' + '9' * 32})).status_code == 422
    pairing = (await client.post('/api/gateway/channels/line/pairing', json={'chat': chat})).json()
    assert json.loads((tmp_path/'line-config/gateway.json').read_text()) == {'enabled': True, 'chats': [chat]}
    config = gateway.config('line')
    assert gateway.consume_pairing('line', '1', 'c' + '9' * 32, sender, '/pair ' + pairing['code'], config) == 'ignored'
    assert gateway.consume_pairing('line', '2', chat, sender, '/pair ' + pairing['code'], config) == 'paired'
    assert gateway.config('line')['senders'] == [sender]
    assert gateway.pairing('line')['code'] is None
    assert not store.list('conversation')


def context(job):
    return json.loads(job['prompt'].rsplit('\n', 1)[1])


async def send(client, text, key, **fields):
    return await client.post('/api/gateway/messages', json={'message': text, **fields},
                             headers={'Idempotency-Key': key})


async def test_followups_wait_for_parent_and_keep_idempotency(system):
    app, client, outer, _ = system
    started, release = asyncio.Event(), asyncio.Event()
    async def run(job):
        outer.jobs.append(job)
        if len(outer.jobs) == 1:
            started.set()
            await release.wait()
            return {'message': '你想查看哪個執行環境？', 'needs_user': True}
        return {'message': '外層 Linux 的說明'}
    outer.run = run
    first = (await send(client, '說明一下', 'first')).json()
    await started.wait()
    response = await send(client, '外層', 'second', reply_to=first['id'])
    second = response.json()
    assert response.status_code == 202 and second['thread_id'] == first['id']
    await asyncio.sleep(0)
    assert len(outer.jobs) == 1
    release.set()
    await drain(app)
    assert context(outer.jobs[1])['conversation']['turns'] == [
        {'request': '說明一下', 'answer': '你想查看哪個執行環境？', 'state': 'awaiting_user'}]
    assert (await send(client, '外層', 'second', reply_to=first['id'])).json()['id'] == second['id']
    assert (await send(client, '其他', 'second', reply_to=first['id'])).status_code == 409
    assert len(outer.jobs) == 2
    await send(client, '重新開始', 'third')
    await drain(app)
    assert context(outer.jobs[-1])['conversation']['turns'] == []
    items = (await client.get('/api/gateway/messages')).json()['messages']
    assert all('scope' not in item and 'request_digest' not in item for item in [first, second, *items])


@pytest.mark.parametrize('change', [{'provider':'grok'}, {'runtime':'haos'}])
async def test_web_cannot_attach_another_agent_context(system, change):
    app, client, _, _ = system
    first = (await send(client, 'private host question', 'first')).json()
    await drain(app)
    assert (await send(client, 'continue', 'second', reply_to=first['id'], **change)).status_code == 409
    assert (await send(client, 'continue', 'invalid', reply_to='invalid')).status_code == 422
    assert (await send(client, 'continue', 'both', reply_to=first['id'], new_conversation=True)).status_code == 422


async def test_external_context_is_scoped_to_channel_audience_sender_and_agent(system, store):
    app, client, outer, inner = system
    gateway = app.state.gateway
    store.set_setting('owner', 'owner')
    client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
    config = ChannelConfig(enabled=True, secret='fixture-private-token', chats=['room', 'other'], senders=['alice', 'bob']).model_dump()
    for name in ('telegram', 'discord'):
        store.set_setting('channel_' + name, config)
    def submit(event, source='telegram', chat='room', sender='alice', **fields):
        return gateway.submit(ChatInput(message=event, **fields), source, chat, sender, event, config=gateway.config(source))
    first = submit('private question')
    await drain(app)
    second = submit('follow up')
    await drain(app)
    assert second['reply_to'] == first['id']
    assert context(outer.jobs[-1])['conversation']['turns'][0]['request'] == 'private question'
    for i, fields in enumerate(({'source':'discord'}, {'chat':'other'}, {'sender':'bob'}, {'provider':'grok'}, {'runtime':'haos'})):
        item = submit('isolated-' + str(i), **fields)
        await drain(app)
        assert item['reply_to'] is None
        assert context((inner.jobs if fields.get('runtime') == 'haos' else outer.jobs)[-1])['conversation']['turns'] == []
    assert 'fixture-private-token' not in ''.join(job['prompt'] for job in outer.jobs + inner.jobs)
    reset = submit('fresh question', new_conversation=True)
    await drain(app)
    assert reset['thread_id'] == reset['id'] and reset['reply_to'] is None
    assert submit('fresh question', new_conversation=True)['id'] == reset['id']
    assert submit('after reset')['reply_to'] == reset['id']
    await drain(app)
    store.set_setting('channel_telegram', {**config, 'secret': 'rotated-fixture-token'})
    rotated = submit('after rotation')
    await drain(app)
    assert rotated['reply_to'] is None
    assert (await send(client, 'Web cannot inherit an external chat', 'web', reply_to=rotated['id'])).status_code == 409


async def test_home_followup_uses_fresh_observations_and_still_requires_approval(system, store, ha):
    app, client, _, inner = system
    store.set_setting('owner', 'owner')
    client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
    inner.answer = {'message': '要操作哪盞燈？', 'needs_user': True}
    first = (await send(client, '打開燈', 'first', runtime='haos')).json()
    await drain(app)
    first_task = store.get('conversation', first['id'])['task_id']
    ha.states[TARGET] = 'on'
    inner.answer = {'message':'提出操作', 'action':{'entity_id': TARGET, 'action':'turn_off'}}
    second = (await send(client, '測試燈，改為關閉', 'second', runtime='haos', reply_to=first['id'])).json()
    await drain(app)
    data = context(inner.jobs[-1])
    assert data['conversation']['turns'][0]['answer'] == '要操作哪盞燈？'
    assert next(e for e in data['observations'] if e['entity_id'] == TARGET)['state'] == 'on'
    item = store.get('conversation', second['id'])
    assert item['state'] == 'awaiting_approval' and not ha.writes and not ha.calls
    assert store.get('task', first_task)['continued_by'] == item['task_id']
    await send(client, '好，同意', 'third', runtime='haos', reply_to=second['id'])
    await drain(app)
    assert not ha.writes and not ha.calls and not store.list('approval')
    store.set_setting('policy', {'entities': []})
    assert (await send(client, '繼續', 'revoked', runtime='haos', reply_to=second['id'])).status_code == 409


async def test_queued_message_rechecks_permissions_before_disclosing_context(system, store):
    app, _, _, inner = system
    store.set_setting('owner', 'owner')
    item = app.state.gateway.submit(ChatInput(message='private question', runtime='haos'), 'web', 'owner', 'owner', 'first')
    store.set_setting('policy', {'entities': []})
    await drain(app)
    assert not inner.jobs and store.get('conversation', item['id'])['state'] == 'failed'


async def test_history_is_bounded_and_retention_applies_before_cleanup(system, store):
    app, client, outer, _ = system
    previous = None
    for i in range(8):
        result = (await send(client, f'message {i}', str(i), reply_to=previous)).json()
        previous = result['id']
        await drain(app)
    history = context(outer.jobs[-1])['conversation']
    assert [turn['request'] for turn in history['turns']] == [f'message {i}' for i in range(1, 7)]
    assert history['truncated']
    outer.answer = {'message': '測試' * 2000}
    result = (await send(client, '測試' * 2000, 'large', reply_to=previous)).json()
    await drain(app)
    await send(client, '接續', 'bounded', reply_to=result['id'])
    await drain(app)
    history = context(outer.jobs[-1])['conversation']
    assert len(canonical(history).encode()) <= 8192 and history['truncated']
    assert all(len(turn[key].encode()) <= 2048 for turn in history['turns'] for key in ('request', 'answer'))
    assert history['turns'][-1]['request'].endswith('…')
    with store.db:
        store.db.execute("UPDATE records SET created='2000-01-01' WHERE kind='conversation'")
    assert store.get('conversation', previous)  # Periodic pruning has not run yet.
    assert (await send(client, 'expired', 'expired', reply_to=previous)).status_code == 409
    store.prune()
    assert (await send(client, 'message 0', '0', reply_to=None)).status_code == 410


async def test_cancelling_a_queued_followup_does_not_cancel_its_parent(system, store):
    app, client, outer, _ = system
    started, release = asyncio.Event(), asyncio.Event()
    async def run(job):
        outer.jobs.append(job)
        started.set()
        await release.wait()
        return {'message': 'finished'}
    outer.run = run
    first = (await send(client, 'parent', 'first')).json()
    await started.wait()
    second = (await send(client, 'child', 'second', reply_to=first['id'])).json()
    await asyncio.sleep(0)
    pending = app.state.running['chat-' + second['id']]
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    release.set()
    await drain(app)
    assert store.get('conversation', first['id'])['state'] == 'succeeded'
    assert store.get('conversation', second['id'])['state'] == 'interrupted'
    assert len(outer.jobs) == 1


async def test_failed_parent_stops_followup_and_completed_threads_survive_restart(system, store):
    app, client, outer, _ = system
    first = (await send(client, 'keep this context', 'first')).json()
    await drain(app)
    await app.state.gateway.start()
    await send(client, 'after restart', 'second', reply_to=first['id'])
    await drain(app)
    assert context(outer.jobs[-1])['conversation']['turns'][0]['request'] == 'keep this context'
    started, release = asyncio.Event(), asyncio.Event()
    async def fail(job):
        outer.jobs.append(job)
        started.set()
        await release.wait()
        raise RunnerError('fixture failure')
    outer.run = fail
    third = (await send(client, 'failing parent', 'third')).json()
    await started.wait()
    child = (await send(client, 'waiting child', 'child', reply_to=third['id'])).json()
    release.set()
    await drain(app)
    assert len(outer.jobs) == 3
    assert store.get('conversation', child['id'])['state'] == 'failed'
    assert '前一則訊息未完成' in store.get('conversation', child['id'])['answer']


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
    # This case verifies the retained restricted Runner mode, selected explicitly
    # now that new channel conversations default to Hermes in v3.
    store.set_setting('gateway', {'coordinator': 'direct'})
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
    await receive_handler('new', 'room', 'owner', '/host /new fresh conversation')
    await drain(app)
    reset = store.list('conversation')[0]
    assert reset['message'] == 'fresh conversation' and reset['reply_to'] is None
    assert context(outer.jobs[-1])['conversation']['turns'] == []
    await receive_handler('follow', 'room', 'owner', 'follow up')
    await drain(app)
    assert store.list('conversation')[0]['reply_to'] == reset['id']
    assert context(outer.jobs[-1])['conversation']['turns'][0]['request'] == 'fresh conversation'
    sent_count = len(sent)
    await receive_handler('4','room','owner','response must not reach changed audience')
    store.set_setting('channel_telegram',{**config,'senders':['someone-else']})
    await drain(app)
    assert len(sent) == sent_count and store.list('conversation')[0]['delivery'] == 'not_sent'


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


async def test_direct_chat_keeps_the_runner_category(system, store, monkeypatch):
    app, client, outer, _ = system

    async def confirmed(*args, **kwargs):
        return True

    monkeypatch.setattr('agentic_controller.gateway.cancel_and_confirm', confirmed)

    async def limited(job):
        raise RunnerError('rate_limited：quota secret sk-live')

    outer.run = limited
    response = await send(client, '限流', 'rate-limited')
    await drain(app)
    item = store.get('conversation', response.json()['id'])
    assert item['state'] == 'failed' and item['error_code'] == 'rate_limited' and item['reason'] == 'rate_limited'
    assert '稍後再送出' in item['answer'] and 'sk-live' not in item['answer'] and 'quota' not in item['answer']

    async def timed_out(job):
        raise TimeoutError

    outer.run = timed_out
    response = await send(client, '逾時', 'direct-timeout')
    await drain(app)
    item = store.get('conversation', response.json()['id'])
    assert item['error_code'] == 'timeout' and '逾時' in item['answer']

    async def cli_timeout(job):
        raise RunnerError('CLI 超時或無法取得結構化結果 raw provider')

    outer.run = cli_timeout
    response = await send(client, '結構', 'cli-timeout')
    await drain(app)
    item = store.get('conversation', response.json()['id'])
    assert item['error_code'] == 'timeout' and 'raw provider' not in item['answer']

    async def unknown(job):
        raise RunnerError('fixture failure sk-live')

    outer.run = unknown
    response = await send(client, '未知', 'unknown-runner')
    await drain(app)
    item = store.get('conversation', response.json()['id'])
    assert item['error_code'] is None and '配對、登入與連線' in item['answer'] and 'sk-live' not in item['answer']

    async def unconfirmed(job):
        raise RunnerError('stop_unconfirmed')

    outer.run = unconfirmed
    response = await send(client, '未確認', 'stop-unconfirmed')
    await drain(app)
    item = store.get('conversation', response.json()['id'])
    assert item['state'] == 'needs_reconcile' and item['error_code'] == 'stop_unconfirmed'


async def test_line_qr_refresh_and_distinct_login_failures(system, monkeypatch):
    app, client, _, _ = system
    gateway = app.state.gateway
    monkeypatch.setattr('agentic_controller.gateway.LINE_QR_SECONDS', 0.4)
    frames, sent = asyncio.Queue(), []

    class Socket:
        async def receive(self):
            return await frames.get()

        async def send_json(self, payload):
            sent.append(payload)

    @asynccontextmanager
    async def admin():
        yield Socket()

    def text(payload):
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: payload)

    monkeypatch.setattr('agentic_controller.gateway.line_admin', admin)
    await client.post('/api/gateway/line/login')
    await frames.put(text({'type': 'auth:state', 'state': 'idle'}))
    await asyncio.sleep(0.05)
    assert sent and sent[-1] == {'type': 'auth:start'}
    await frames.put(text({'type': 'auth:qr', 'url': 'https://line.example/qr/1'}))
    await asyncio.sleep(0.05)
    status = (await client.get('/api/gateway/line/login')).json()
    assert status['qr'].endswith('/1') and status['qr_epoch'] == 1 and status['status'] == 'authenticating'
    first_expires = status['expires']
    await frames.put(text({'type': 'auth:qr', 'url': 'https://line.example/qr/2'}))
    await asyncio.sleep(0.05)
    status = (await client.get('/api/gateway/line/login')).json()
    assert status['qr'].endswith('/2') and status['qr_epoch'] == 2 and status['expires'] > first_expires
    await asyncio.wait_for(gateway.line_task, timeout=2)
    status = (await client.get('/api/gateway/line/login')).json()
    assert status['status'] == 'qr_expired' and status['reason'] == 'qr_expired' and status['qr'] is None
    assert 'QR code 已過期' in status['message'] and 'line.example' not in status['message']

    monkeypatch.setattr('agentic_controller.gateway.LINE_QR_SECONDS', 5)
    await client.post('/api/gateway/line/login')
    await frames.put(text({'type': 'auth:state', 'state': 'idle'}))
    await frames.put(text({'type': 'auth:pin', 'code': '123456'}))
    await frames.put(text({'type': 'error', 'code': 'pin_mismatch', 'message': 'wrong pin from LINE'}))
    await asyncio.wait_for(gateway.line_task, timeout=2)
    status = (await client.get('/api/gateway/line/login')).json()
    assert status['status'] == 'pin_failed' and status['pin'] is None
    assert 'PIN' in status['message'] and 'wrong pin' not in status['message']

    await client.post('/api/gateway/line/login')
    await frames.put(text({'type': 'auth:state', 'state': 'idle'}))
    await frames.put(text({'type': 'error', 'reason': 'unofficial client rejected'}))
    await asyncio.wait_for(gateway.line_task, timeout=2)
    status = (await client.get('/api/gateway/line/login')).json()
    assert status['status'] == 'login_rejected' and 'unofficial' not in json.dumps(status)

    class Closed:
        async def receive(self):
            raise aiohttp.ClientConnectionError
        async def send_json(self, payload):
            return None

    @asynccontextmanager
    async def down():
        yield Closed()

    monkeypatch.setattr('agentic_controller.gateway.line_admin', down)
    await client.post('/api/gateway/line/login')
    await asyncio.wait_for(gateway.line_task, timeout=2)
    status = (await client.get('/api/gateway/line/login')).json()
    assert status['status'] == 'service_unavailable' and status['reason'] == 'service_unavailable'


async def test_telegram_and_discord_connect_errors_stay_distinct(system, store, monkeypatch):
    from agentic_controller.channels import ChannelError, telegram_failure
    from agentic_controller.gateway import TRANSPORTS

    assert telegram_failure(401).code == 'invalid_token'
    assert telegram_failure(409, 'terminated by other getUpdates request').reason == 'poller_conflict'
    assert telegram_failure(409, 'webhook is set').reason == 'webhook_active'
    assert telegram_failure(429).code == 'rate_limited'
    assert telegram_failure(500).code == 'unavailable'
    generic = ChannelError('通訊服務拒絕連線，請確認權杖、權限或速率限制', status=401, detail='Unauthorized secret')
    assert generic.code == 'unavailable' and 'secret' not in str(generic)

    app, _, _, _ = system
    gateway = app.state.gateway
    mode = {'name': 'invalid_token'}

    async def replies(session, method, url, **kwargs):
        operation = url.rsplit('/', 1)[-1]
        if mode['name'] == 'invalid_token':
            raise ChannelError('通訊服務拒絕連線，請確認權杖、權限或速率限制', status=401, detail='Unauthorized')
        if operation == 'getMe':
            return {'ok': True, 'result': {'id': 1, 'is_bot': True}}
        if operation == 'getWebhookInfo':
            url_value = 'https://example.invalid/hook' if mode['name'] == 'webhook' else ''
            return {'ok': True, 'result': {'url': url_value}}
        if mode['name'] == 'poller':
            raise ChannelError('通訊服務拒絕連線，請確認權杖、權限或速率限制', status=409,
                               detail='Conflict: terminated by other getUpdates request')
        if mode['name'] == 'rate':
            raise ChannelError('通訊服務拒絕連線，請確認權杖、權限或速率限制', status=429, detail='Too Many Requests')
        return {'ok': True, 'result': []}

    monkeypatch.setattr('agentic_controller.channels.json_request', replies)
    store.set_setting('channel_telegram', {'enabled': True, 'secret': '12345:synthetic', 'chats': ['1'], 'senders': ['2']})

    async def expect(name, reason):
        mode['name'] = name
        expected = {'webhook': 'webhook_conflict', 'poller': 'webhook_conflict', 'rate': 'rate_limited'}.get(name, name)
        await gateway.restart('telegram')
        for _ in range(40):
            if gateway.states.get('telegram') == expected:
                break
            await asyncio.sleep(0.02)
        assert gateway.states.get('telegram') == expected
        assert gateway.reasons.get('telegram') == reason
        channel = next(item for item in gateway.public()['channels'] if item['id'] == 'telegram')
        assert channel['status'] == expected and channel['reason'] == reason
        assert 'Unauthorized' not in json.dumps(channel) and 'getUpdates' not in json.dumps(channel)

    await expect('invalid_token', 'invalid_token')
    await expect('webhook', 'webhook_active')
    await expect('poller', 'poller_conflict')
    await expect('rate', 'rate_limited')

    class DiscordBoom:
        def __init__(self, store, config):
            pass

        async def run(self, receive, state):
            raise ChannelError('通訊服務拒絕連線，請確認權杖、權限或速率限制', status=401, detail='discord-secret')

        async def send(self, chat, text):
            return None

    monkeypatch.setitem(TRANSPORTS, 'discord', DiscordBoom)
    store.set_setting('channel_discord', {'enabled': True, 'secret': 'bot', 'chats': ['1'], 'senders': ['2']})
    await gateway.restart('discord')
    await asyncio.sleep(0.05)
    assert gateway.states.get('discord') == 'unavailable'
    assert gateway.reasons.get('discord') is None
    assert 'discord-secret' not in json.dumps(gateway.public())


async def test_concurrent_submits_share_one_thread_and_one_execution(system):
    app, _, _, _ = system
    gateway = app.state.gateway
    executed = []

    class Store:
        def __init__(self):
            self.items = {}

        def get(self, kind, identifier):
            time.sleep(0.05)
            return self.items.get(identifier)

        def conversation(self, *, id=None, scope=None):
            time.sleep(0.05)
            if id:
                return self.items.get(id)
            matches = [item for item in self.items.values() if item.get('scope') == scope]
            return matches[-1] if matches else None

        def request(self, *args, **kwargs):
            return None

        def put(self, kind, item):
            self.items[item['id']] = item
            return item

        def setting(self, key, default=None):
            return False if key == 'paused' else ({} if default is None else default)

    store = Store()
    original = gateway.store, gateway.execute, gateway.scope

    async def execute(item, config):
        executed.append(item['id'])

    def scope(*args, **kwargs):
        return 'scope-fixed'

    gateway.store, gateway.execute, gateway.scope = store, execute, scope
    gateway._loop = asyncio.get_running_loop()
    errors = []
    try:
        results = []

        def work(event):
            try:
                results.append(gateway.submit(ChatInput(message=event), 'telegram', 'room', 'alice', event, config={'enabled': True}))
            except Exception as exc:
                errors.append(exc)

        async def join(threads):
            deadline = time.monotonic() + 3
            for thread in threads:
                while thread.is_alive():
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.01)

        threads = [threading.Thread(target=work, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        await join(threads)
        assert not errors and len(results) == 2
        roots = [item for item in results if item['reply_to'] is None]
        children = [item for item in results if item['reply_to']]
        assert len(roots) == 1 and len(children) == 1
        assert children[0]['reply_to'] == roots[0]['id']
        assert children[0]['thread_id'] == roots[0]['thread_id'] == roots[0]['id']

        same = []

        def again():
            try:
                same.append(gateway.submit(ChatInput(message='same'), 'web', 'owner', 'owner', 'same-key'))
            except Exception as exc:
                errors.append(exc)

        pair = [threading.Thread(target=again) for _ in range(2)]
        for thread in pair:
            thread.start()
        await join(pair)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not errors and same[0]['id'] == same[1]['id']
        assert executed.count(same[0]['id']) == 1
        assert len(gateway._submit_locks) == 0
    finally:
        gateway.store, gateway.execute, gateway.scope = original
