"""Official publisher approval protocol fixtures; no production key or trust change."""
import time
from contextlib import asynccontextmanager, nullcontext

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.operations import TrustedPublisherApproval, TrustedPublisherStatus
from fastapi import HTTPException
from pydantic import ValidationError

TOKEN = 'fixture-only-approval-token-' + 'a' * 20
REVIEW = 'b' * 64


def summary(state='approval_required'):
    return {'contract': 1, 'state': state, 'message': '請確認官方更新來源。', 'code': 'official_publisher_owner_approval_required',
            'publisher': 'ted-h-com-agentic-smart-home', 'repository': 'TED-H-com/agentic-smart-home',
            'channel': 'stable', 'image_repository': 'ghcr.io/ted-h-com/agentic-smart-home-controller',
            'key_sha256': 'a' * 64, 'review_digest': REVIEW,
            'scope': 'controller_only_same_schema', 'authority': 'signed_controller_images_only'}


@asynccontextmanager
async def client_fixture(store, ha, tmp_path, monkeypatch, broker):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    monkeypatch.setenv('AGENTIC_LINE_CONFIG', str(tmp_path / 'line/gateway.json'))
    (tmp_path / 'access-key').write_text('synthetic-publisher-owner-key')
    monkeypatch.setattr('agentic_controller.operations.broker_call', broker)
    app = create_app(store=store, ha=ha, host_mode=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099',
                                     headers={'Origin': 'http://localhost:8099'}) as client:
            yield client
    finally:
        await app.state.gateway.close()


async def login(client):
    assert (await client.post('/api/host/login', json={'key': 'synthetic-publisher-owner-key'})).status_code == 200
    client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']


async def test_owner_confirms_exact_shipped_pin_and_only_hashed_offer_is_persisted(store, ha, tmp_path, monkeypatch):
    calls = []
    async def broker(method, path, body=None, *, store):
        calls.append((method, path, body))
        if path == '/updates/publisher/offer':
            assert body == {}
            return {**summary(), 'approval': TOKEN, 'expires_at': time.time() + 300}
        if path == '/updates/publisher/approve':
            assert body == {'review_digest': REVIEW, 'approval': TOKEN}
            return summary('trusted')
        assert method == 'GET' and path == '/updates/publisher'
        return summary()
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        body = {'review_digest': REVIEW, 'approval': TOKEN}
        assert (await client.get('/api/updates/publisher')).status_code == 401
        assert (await client.post('/api/updates/publisher/offer', json={})).status_code == 401
        await client.post('/api/host/login', json={'key': 'synthetic-publisher-owner-key'})
        assert (await client.post('/api/updates/publisher/offer', json={})).status_code == 403
        await login(client)
        status = await client.get('/api/updates/publisher')
        assert status.status_code == 200 and status.json()['key_sha256'] == 'a' * 64
        offer = await client.post('/api/updates/publisher/offer', json={})
        assert offer.status_code == 200 and offer.json()['approval'] == TOKEN
        assert TOKEN not in str(store.setting('trusted_publisher_offer'))
        approved = await client.post('/api/updates/publisher/approve', json=body)
        assert approved.status_code == 200 and approved.json()['state'] == 'trusted'
        assert store.setting('trusted_publisher_offer') is None
        assert (await client.post('/api/updates/publisher/approve', json=body)).status_code == 409
        assert len([item for item in calls if item[1] == '/updates/publisher/approve']) == 1


@pytest.mark.parametrize('fault', ['expired', 'wrong_review', 'wrong_token', 'session', 'logout_epoch', 'owner'])
async def test_offer_cannot_be_replayed_under_changed_owner_session_or_exact_content(store, ha, tmp_path, monkeypatch, fault):
    calls = []
    async def broker(method, path, body=None, *, store):
        calls.append(path)
        assert path == '/updates/publisher/offer'
        return {**summary(), 'approval': TOKEN, 'expires_at': time.time() + 300}
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        assert (await client.post('/api/updates/publisher/offer', json={})).status_code == 200
        body = {'review_digest': REVIEW, 'approval': TOKEN}
        if fault == 'expired':
            record = store.setting('trusted_publisher_offer')
            store.set_setting('trusted_publisher_offer', {**record, 'expires_at': time.time() - 1})
        elif fault == 'wrong_review':
            body['review_digest'] = 'c' * 64
        elif fault == 'wrong_token':
            body['approval'] = 'c' * 43
        elif fault == 'session':
            record = store.setting('trusted_publisher_offer')
            store.set_setting('trusted_publisher_offer', {**record, 'session_digest': 'd' * 64})
        elif fault == 'logout_epoch':
            store.set_setting('hermes_web_epoch', 'another-login')
        elif fault == 'owner':
            store.set_setting('owner', 'changed-owner')
        result = await client.post('/api/updates/publisher/approve', json=body)
        assert result.status_code in {403, 409}
        assert calls == ['/updates/publisher/offer']


@pytest.mark.parametrize('path,body', [
    ('offer', {'public_key_pem': 'untrusted'}), ('offer', {'repository': 'attacker/repository'}),
    ('offer', {'url': 'https://attacker.invalid'}),
    ('approve', {'review_digest': REVIEW, 'approval': TOKEN, 'publisher': 'attacker'}),
    ('approve', {'review_digest': '../actions', 'approval': TOKEN}),
])
async def test_browser_cannot_supply_keys_channels_or_alternate_targets(store, ha, tmp_path, monkeypatch, path, body):
    async def broker(*args, **kwargs):
        raise AssertionError('invalid input must never reach Host')
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        assert (await client.post('/api/updates/publisher/' + path, json=body)).status_code == 422


async def test_old_host_get_returns_plain_safe_unavailable_state(store, ha, tmp_path, monkeypatch):
    async def broker(*args, **kwargs):
        raise HTTPException(404, 'update_not_found')
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        result = await client.get('/api/updates/publisher')
        assert result.status_code == 200 and result.json()['state'] == 'unavailable'
        assert result.json()['key_sha256'] is None
        assert '受信任主機版本' in result.json()['message']


async def test_scope_change_during_offer_cannot_create_an_approval(store, ha, tmp_path, monkeypatch):
    async def broker(*args, **kwargs):
        store.set_setting('hermes_web_epoch', 'revoked-during-read')
        return {**summary(), 'approval': TOKEN, 'expires_at': time.time() + 300}
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        result = await client.post('/api/updates/publisher/offer', json={})
        assert result.status_code == 403 and store.setting('trusted_publisher_offer') is None


@pytest.mark.parametrize('change', [
    {'contract': True}, {'contract': 2}, {'repository': 'attacker/repo'}, {'key_sha256': None},
    {'authority': 'arbitrary_host_changes'}, {'scope': 'whole_host'}, {'public_key_pem': 'unexpected'},
])
def test_pin_summary_fails_closed_on_missing_or_contradictory_semantics(change):
    with pytest.raises(ValidationError):
        TrustedPublisherStatus.model_validate({**summary(), **change})


def test_approval_request_only_carries_exact_offer_references():
    with pytest.raises(ValidationError):
        TrustedPublisherApproval.model_validate({'review_digest': REVIEW, 'approval': TOKEN, 'key': 'untrusted'})


async def test_real_host_publisher_summary_offer_and_approval_match_controller_contract(store, ha, tmp_path, monkeypatch):
    """Real Host parser/approval against confined files and a public test vector."""
    import base64
    import json
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'host'))
    from agentic_host import publisher

    # RFC 8032 public test vector. There is no private or production signing key.
    der = bytes.fromhex('302a300506032b6570032100d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')
    pem = '-----BEGIN PUBLIC KEY-----\n' + base64.b64encode(der).decode() + '\n-----END PUBLIC KEY-----\n'
    clean, fingerprint = publisher.public_key(pem)
    product = tmp_path / 'product'
    (product / 'host').mkdir(parents=True)
    metadata = {'format': 1, 'publisher': publisher.OFFICIAL_PUBLISHER, 'repository': publisher.OFFICIAL_REPOSITORY,
                'channel': publisher.OFFICIAL_CHANNEL, 'image_repository': publisher.OFFICIAL_IMAGE,
                'pin': {'public_key_pem': clean, 'key_sha256': fingerprint}}
    (product / 'host/official-update-channel.json').write_text(json.dumps(metadata))
    trust = tmp_path / 'trust/publishers.json'
    # Replace only installation ownership checks; every actual write is confined
    # to this test's disk directory. No system publisher file is opened.
    def confined(path, *, optional=False):
        assert Path(path).is_relative_to(tmp_path)
    monkeypatch.setattr(publisher, 'trusted_path', confined)
    monkeypatch.setattr(publisher.os, 'geteuid', lambda: 0)
    official = publisher.OfficialPublisher(SimpleNamespace(product=product, locked=nullcontext), trust_path=trust)
    async def broker(method, path, body=None, *, store):
        if method == 'GET':
            return official.status()
        if path.endswith('/offer'):
            return official.offer(body)
        assert path.endswith('/approve')
        return official.approve(body)
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        current = await client.get('/api/updates/publisher')
        assert current.status_code == 200 and current.json()['state'] == 'approval_required'
        assert current.json()['key_sha256'] == fingerprint
        offer = await client.post('/api/updates/publisher/offer', json={})
        assert offer.status_code == 200
        approval = {key: offer.json()[key] for key in ('review_digest', 'approval')}
        accepted = await client.post('/api/updates/publisher/approve', json=approval)
        assert accepted.status_code == 200 and accepted.json()['state'] == 'trusted'
        assert json.loads(trust.read_text())['publishers'][publisher.OFFICIAL_PUBLISHER]['public_key_pem'] == clean


@pytest.mark.parametrize('action', ['offer', 'approve'])
async def test_existing_host_session_verifier_is_rechecked_after_durability_wait(store, monkeypatch, action):
    from types import SimpleNamespace

    from agentic_controller.models import digest
    from agentic_controller.operations import Operations

    authority = {'owner': None, 'web_epoch': None, 'expires': time.time() + 300}
    auth = SimpleNamespace(active=True)
    auth.verify = lambda request: auth.active
    workers = SimpleNamespace(authority=lambda request: authority)
    operations = Operations(store, workers, host_mode=True, host_auth=auth)
    request = SimpleNamespace(state=SimpleNamespace(), cookies={'agentic_host': 'fixture-session'})
    store.set_setting('trusted_publisher_offer', {
        'approval_digest': digest(TOKEN), 'review_digest': REVIEW, 'expires_at': time.time() + 300,
        'authority': authority, 'session_digest': digest('fixture-session')})
    async def revoke(_):
        auth.active = False
    async def forbidden_broker(*args, **kwargs):
        raise AssertionError('revoked session must not reach privileged Host')
    monkeypatch.setattr('agentic_controller.operations.durable', revoke)
    monkeypatch.setattr('agentic_controller.operations.broker_call', forbidden_broker)
    with pytest.raises(HTTPException) as error:
        if action == 'offer':
            await operations.publisher_offer(request)
        else:
            await operations.publisher_approve(TrustedPublisherApproval(review_digest=REVIEW, approval=TOKEN), request)
    assert error.value.status_code == 403
