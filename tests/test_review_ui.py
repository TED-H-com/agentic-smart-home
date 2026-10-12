"""Review regressions: actual Controller/transport logic, synthetic upstream replies.

ASGI does not substitute for a live browser, real HA OAuth or Telegram cloud login.
"""
import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.channels import TelegramTransport
from agentic_controller.gateway import Gateway
from agentic_controller.ha import HomeAssistant
from agentic_controller.models import ChannelPairingInput


@pytest.mark.parametrize('failure', ['expired_grant', 'expired_session', 'malformed_callback', 'declined_callback'])
async def test_rejected_ha_callback_always_returns_to_clean_onboarding(store, tmp_path, monkeypatch, failure):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    key = 'review-owner-fixture-' + 'a' * 24
    (tmp_path / 'access-key').write_text(key)
    ha = HomeAssistant('', 'http://fixture.invalid/api', 'ws://fixture.invalid/api/websocket', external=True)
    app = create_app(store=store, ha=ha, host_mode=True)
    exchange_calls = []

    async def no_exchange(*args):
        exchange_calls.append(args)
        raise AssertionError('Rejected authorization must not exchange credentials')

    app.state.ha_authorization.token_request = no_exchange
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099',
                                 headers={'Origin': 'http://localhost:8099'}) as client:
        assert (await client.post('/api/host/login', json={'key': key})).status_code == 200
        client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
        client.headers['Idempotency-Key'] = 'review-native-auth'
        started = await client.post('/api/host/authorize', json={})
        state = parse_qs(urlsplit(started.json()['url']).query)['state'][0]
        if failure == 'expired_grant':
            record = store.get('ha_oauth', store.setting('ha_oauth_current'))
            record['expires'] = 0
            store.put('ha_oauth', record)
        elif failure == 'expired_session':
            client.cookies.clear()
        elif failure == 'malformed_callback':
            state = 'malformed-fixture-state'
        params = {'state': state, 'code': 'one-use-fixture-code'}
        if failure == 'declined_callback':
            params = {'state': state, 'error': 'access_denied'}
        response = await client.get('/api/host/authorized', params=params)
        assert response.status_code == 303
        assert response.headers['location'] == '/#onboarding'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert response.headers['cache-control'] == 'no-store'
        assert 'one-use-fixture-code' not in response.text
        assert not exchange_calls
        if failure == 'declined_callback':
            assert app.state.ha_authorization.public()['state'] == 'failed'
        elif failure == 'malformed_callback':
            assert app.state.ha_authorization.public()['state'] == 'pending'
        elif failure == 'expired_session':
            assert 'set-cookie' not in response.headers
            assert (await client.get('/api/session')).status_code == 401


@pytest.mark.parametrize('kind', ['current_code', 'followed_by_ordinary', 'wrong_code', 'expired_code', 'ordinary', 'bot_sender'])
async def test_telegram_initial_poll_retains_only_current_pairing(store, monkeypatch, kind):
    """A just-displayed pairing command may arrive before initial polling finishes."""
    gateway = Gateway(store, {}, None, None, {})
    model_calls = []
    gateway.submit = lambda *args, **kwargs: model_calls.append((args, kwargs))
    initialized = asyncio.Event()
    requests = []

    async def replies(session, method, url, **kwargs):
        operation = url.rsplit('/', 1)[-1]
        requests.append((operation, kwargs))
        if operation == 'getMe':
            return {'ok': True, 'result': {'id': 99, 'is_bot': True}}
        if operation == 'getWebhookInfo':
            return {'ok': True, 'result': {'url': ''}}
        assert operation == 'getUpdates'
        if kwargs['json']['offset'] == -100:
            assert kwargs['json']['limit'] == 100
            record = store.setting('channel_pairing_telegram')
            code = 'incorrect-code' if kind == 'wrong_code' else record['code']
            text = 'Historical ordinary instruction' if kind == 'ordinary' else '/pair ' + code
            updates = [{'update_id': 100, 'message': {
                'chat': {'id': 55}, 'from': {'id': 77, 'is_bot': kind == 'bot_sender'}, 'text': text}}]
            if kind == 'followed_by_ordinary':
                updates.append({'update_id': 101, 'message': {
                    'chat': {'id': 55}, 'from': {'id': 77}, 'text': 'Hello after pairing'}})
            return {'ok': True, 'result': updates}
        initialized.set()
        await asyncio.Future()

    monkeypatch.setattr('agentic_controller.channels.json_request', replies)
    try:
        await gateway.begin_pairing('telegram', ChannelPairingInput(secret='12345:synthetic_review_bot'), 'review-pair')
        if kind == 'expired_code':
            record = store.setting('channel_pairing_telegram')
            record['expires_at'] = time.time() - 1
            store.set_setting('channel_pairing_telegram', record)
        await asyncio.wait_for(initialized.wait(), timeout=2)
        if kind in {'current_code', 'followed_by_ordinary'}:
            assert gateway.pairing('telegram')['state'] == 'paired'
            assert gateway.config('telegram')['chats'] == ['55']
            assert gateway.config('telegram')['senders'] == ['77']
        else:
            assert gateway.pairing('telegram')['state'] != 'paired'
            assert not gateway.config('telegram')['enabled']
        assert model_calls == []
        assert requests[-1][1]['json']['offset'] == (102 if kind == 'followed_by_ordinary' else 101)
    finally:
        await gateway.close()


async def test_telegram_baseline_never_delivers_ordinary_history(store, monkeypatch):
    delivered = []
    adapter = TelegramTransport(store, {'secret': '12345:synthetic_review_bot'})

    class Finished(Exception):
        pass

    async def replies(session, method, url, **kwargs):
        if url.endswith('/getMe'):
            return {'ok': True, 'result': {'id': 99, 'is_bot': True}}
        if url.endswith('/getWebhookInfo'):
            return {'ok': True, 'result': {'url': ''}}
        if kwargs['json']['offset'] == -100:
            return {'ok': True, 'result': [{'update_id': 100, 'message': {
                'chat': {'id': 55}, 'from': {'id': 77}, 'text': 'Old household instruction'}}]}
        raise Finished

    async def receive(*args):
        delivered.append(args)

    monkeypatch.setattr('agentic_controller.channels.json_request', replies)
    with pytest.raises(Finished):
        await adapter.run(receive, lambda value: None)
    assert delivered == []
