"""Fixed owner Host update protocol fixtures; no live Host or signing key used."""
import copy
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.host_updates import HostBundleManifest, HostBundleStage, HostUpdates, broker_call
from agentic_controller.models import digest
from fastapi import HTTPException
from pydantic import ValidationError

CANDIDATE = 'b' * 64
TOKEN = 'fixture-host-approval-' + 'a' * 24
COMPATIBILITY = {'host_protocol': 1, 'controller_protocol': 1, 'controller_schema': 3, 'reconcile': 1}


def manifest():
    return {'format': 1, 'kind': 'agentic-home-host-bundle', 'publisher': 'ted-h-com-agentic-smart-home',
            'version': '0.12.0', 'source_commit': 'a' * 40,
            'bundle': {'url': 'https://github.com/TED-H-com/agentic-smart-home/releases/download/v0.12.0/agentic-home-host-0.12.0.tar',
                       'sha256': 'c' * 64, 'size': 12345}, 'compatibility': copy.deepcopy(COMPATIBILITY),
            'issued_at': 1780000000, 'expires_at': 1780086400,
            'tests': [{'name': 'isolated-host-upgrade', 'result': 'passed', 'sha256': 'd' * 64}]}


def record(state='staged'):
    return {'candidate_digest': CANDIDATE, 'manifest': manifest(), 'state': state,
            'created_at': time.time(), 'updated_at': time.time(), 'message': '主機更新已準備好。',
            'error_code': None, 'auto_rollback': False, 'approval_id': TOKEN,
            'approval_expires_at': time.time() + 300}


def status(records=None):
    return {'contract': 1, 'scope': 'host_bundle_same_schema', 'compatibility': copy.deepcopy(COMPATIBILITY),
            'available': True, 'code': None, 'message': '可檢查官方主機更新。', 'records': records or [],
            'current_source_commit': 'd' * 40,
            'check': {'state': 'idle', 'code': None, 'message': '', 'candidate_digest': None, 'checked_at': None}}


@asynccontextmanager
async def client_fixture(store, ha, tmp_path, monkeypatch, broker):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    monkeypatch.setenv('AGENTIC_LINE_CONFIG', str(tmp_path / 'line/gateway.json'))
    (tmp_path / 'access-key').write_text('synthetic-host-update-owner-key')
    monkeypatch.setattr('agentic_controller.host_updates.broker_call', broker)
    app = create_app(store=store, ha=ha, host_mode=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099',
                                     headers={'Origin': 'http://localhost:8099'}) as client:
            yield client
    finally:
        await app.state.gateway.close()


async def login(client):
    assert (await client.post('/api/host/login', json={'key': 'synthetic-host-update-owner-key'})).status_code == 200
    client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']


async def test_check_stages_without_approval_then_exact_owner_apply_is_single_effect(store, ha, tmp_path, monkeypatch):
    calls = []
    current = status()
    async def broker(method, path, body=None, *, store):
        calls.append((method, path, body))
        if method == 'GET':
            return copy.deepcopy(current)
        if path.endswith('/check'):
            assert body == {}
            current['check']['state'] = 'checking'
            return copy.deepcopy(current)
        assert path.endswith('/apply') and body == {'candidate_digest': CANDIDATE, 'approval_id': TOKEN}
        current['records'][0]['state'] = 'queued'
        return copy.deepcopy(current['records'][0])
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        health = await client.get('/health')
        assert health.status_code == 200 and health.json()['host_update'] == COMPATIBILITY
        assert (await client.get('/api/host-updates')).status_code == 401
        await client.post('/api/host/login', json={'key': 'synthetic-host-update-owner-key'})
        assert (await client.post('/api/host-updates/check', json={})).status_code == 403
        await login(client)
        assert (await client.post('/api/host-updates/check', json={})).status_code == 202
        assert not [item for item in calls if item[1].endswith('/apply')]
        current.update(records=[record()], check={**current['check'], 'state': 'available'})
        response = await client.get('/api/host-updates')
        assert response.status_code == 200 and response.json()['records'][0]['state'] == 'staged'
        assert TOKEN not in str(store.setting('host_bundle_owner_offers'))
        body = {'candidate_digest': CANDIDATE, 'approval_id': TOKEN}
        assert (await client.post('/api/host-updates/apply', json=body)).status_code == 202
        assert (await client.post('/api/host-updates/apply', json=body)).status_code == 409
        assert len([item for item in calls if item[1].endswith('/apply')]) == 1


@pytest.mark.parametrize('fault', ['epoch', 'owner', 'session', 'expired', 'token', 'candidate', 'incompatible'])
async def test_apply_fences_scope_expiry_wrong_candidate_and_protocol_changes(store, ha, tmp_path, monkeypatch, fault):
    current = status([record()])
    calls = []
    async def broker(method, path, body=None, *, store):
        calls.append(method)
        assert method == 'GET', 'rejected approval cannot invoke Host mutation'
        return copy.deepcopy(current)
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        assert (await client.get('/api/host-updates')).status_code == 200
        body = {'candidate_digest': CANDIDATE, 'approval_id': TOKEN}
        if fault == 'epoch':
            store.set_setting('hermes_web_epoch', 'another-login')
        elif fault == 'owner':
            store.set_setting('owner', 'another-owner')
        elif fault == 'session':
            offers = store.setting('host_bundle_owner_offers')
            offers['apply:' + CANDIDATE]['authority']['session'] = digest('other-session')
            store.set_setting('host_bundle_owner_offers', offers)
        elif fault == 'expired':
            offers = store.setting('host_bundle_owner_offers')
            offers['apply:' + CANDIDATE]['expires'] = time.time() - 1
            store.set_setting('host_bundle_owner_offers', offers)
        elif fault == 'token':
            body['approval_id'] = 'x' * 43
        elif fault == 'candidate':
            body['candidate_digest'] = 'c' * 64
        else:
            current['compatibility']['controller_schema'] = 4
        response = await client.post('/api/host-updates/apply', json=body)
        assert response.status_code in {403, 409, 503}


async def test_old_host_is_truthfully_unavailable_without_reinstall_or_unapproved_bootstrap(store, ha, tmp_path, monkeypatch):
    async def broker(method, *args, **kwargs):
        assert method == 'GET'
        raise HTTPException(404, 'host_update_not_found')
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        value = await client.get('/api/host-updates')
        assert value.status_code == 200 and value.json()['available'] is False
        assert value.json()['compatibility'] is None and '不需重裝' in value.json()['message']
        assert (await client.post('/api/host-updates/check', json={})).status_code == 409


async def test_manual_rollback_is_separately_approved_and_remains_available_after_source_revocation(store, ha, tmp_path, monkeypatch):
    current = status([record('deployed')])
    current['available'] = False
    calls = []
    async def broker(method, path, body=None, *, store):
        calls.append(path)
        if method == 'GET':
            return copy.deepcopy(current)
        if path.endswith('/rollback-offer'):
            current['records'][0].update(rollback_approval_id='z' * 43, rollback_expires_at=time.time() + 300)
        elif path.endswith('/rollback'):
            assert body == {'candidate_digest': CANDIDATE, 'approval_id': 'z' * 43}
            current['records'][0]['state'] = 'rolling_back'
        return copy.deepcopy(current['records'][0])
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        store.set_setting('updater_gate', True)
        offer = await client.post('/api/host-updates/rollback-offer', json={'candidate_digest': CANDIDATE})
        assert offer.status_code == 200 and '/host-updates/rollback' not in calls
        approved = await client.post('/api/host-updates/rollback', json={'candidate_digest': CANDIDATE, 'approval_id': 'z' * 43})
        assert approved.status_code == 202 and approved.json()['state'] == 'rolling_back'


@pytest.mark.parametrize('path,body', [('check', {'url': 'https://attacker.invalid'}), ('check', {'approved': True}),
    ('apply', {'candidate_digest': CANDIDATE, 'approval_id': TOKEN, 'shell': 'anything'}),
    ('stage', {'manifest': manifest(), 'signature': 'A' * 86 + '==', 'public_key': 'untrusted'})])
async def test_browser_cannot_supply_targets_keys_shell_or_an_approval_boolean(store, ha, tmp_path, monkeypatch, path, body):
    async def broker(*args, **kwargs):
        raise AssertionError('invalid fields must not reach Host')
    async with client_fixture(store, ha, tmp_path, monkeypatch, broker) as client:
        await login(client)
        assert (await client.post('/api/host-updates/' + path, json=body)).status_code == 422


@pytest.mark.parametrize('field,value', [('format', True), ('publisher', 'attacker'), ('kind', 'controller-image')])
def test_manifest_identity_semantics_are_exact(field, value):
    raw = manifest()
    raw[field] = value
    with pytest.raises(ValidationError):
        HostBundleManifest.model_validate(raw)


@pytest.mark.parametrize('url', ['https://attacker.invalid/bundle.tar',
    'https://github.com/TED-H-com/agentic-smart-home/releases/download/v0.13.0/agentic-home-host-0.13.0.tar',
    'https://github.com/TED-H-com/agentic-smart-home/releases/download/v0.12.0/agentic-home-host-0.12.0.tar?token=secret',
    'https://evil@github.com/TED-H-com/agentic-smart-home/releases/download/v0.12.0/agentic-home-host-0.12.0.tar'])
def test_manifest_bundle_url_is_fixed_official_and_version_bound(url):
    raw = manifest()
    raw['bundle']['url'] = url
    with pytest.raises(ValidationError):
        HostBundleManifest.model_validate(raw)


@pytest.mark.parametrize('fault', ['protocol', 'boolean_protocol', 'schema', 'oversize', 'long_lifetime', 'boolean_timestamp'])
def test_manifest_compatibility_size_and_time_are_bounded(fault):
    raw = manifest()
    if fault == 'protocol':
        raw['compatibility']['host_protocol'] = 2
    elif fault == 'boolean_protocol':
        raw['compatibility']['reconcile'] = True
    elif fault == 'schema':
        raw['compatibility']['controller_schema'] = 4
    elif fault == 'oversize':
        raw['bundle']['size'] = 128 * 1024 * 1024 + 1
    elif fault == 'long_lifetime':
        raw['expires_at'] = raw['issued_at'] + 31 * 86400
    else:
        raw['issued_at'] = True
    with pytest.raises(ValidationError):
        HostBundleManifest.model_validate(raw)


def test_canonical_signed_numbers_remain_exact_and_unknown_fields_are_rejected():
    raw = manifest()
    assert HostBundleManifest.model_validate(raw).model_dump() == raw
    raw['issued_at'] += .5
    assert digest(HostBundleManifest.model_validate(raw).model_dump()) == digest(raw)
    with pytest.raises(ValidationError):
        HostBundleStage.model_validate({'manifest': {**raw, 'command': 'never'}, 'signature': 'A' * 86 + '=='})


@pytest.mark.parametrize('path', ['/actions', '/host-updates/../../actions', 'https://attacker.invalid', '/host-updates/other'])
async def test_fixed_broker_rejects_arbitrary_paths_before_opening_socket(store, path):
    with pytest.raises(HTTPException) as error:
        await broker_call('POST', path, {}, store=store)
    assert error.value.status_code == 422


async def test_host_session_verifier_rechecks_after_durability_before_check(store, monkeypatch):
    auth = SimpleNamespace(active=True)
    auth.verify = lambda request: auth.active
    operations = HostUpdates(store, auth, host_mode=True)
    request = SimpleNamespace(state=SimpleNamespace(), cookies={'agentic_host': 'fixture-session'})
    async def broker(method, *args, **kwargs):
        assert method == 'GET'
        return status()
    async def revoke(_): auth.active = False
    monkeypatch.setattr('agentic_controller.host_updates.broker_call', broker)
    monkeypatch.setattr('agentic_controller.host_updates.durable', revoke)
    with pytest.raises(HTTPException) as error:
        await operations.check(request)
    assert error.value.status_code == 403


def test_actual_host_public_status_and_manifest_agree_with_controller_models(tmp_path, monkeypatch):
    from pathlib import Path

    from agentic_controller.host_updates import HostBundleStatus

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'host'))
    from agentic_host.host_update import HostBundle, validate_manifest

    raw = manifest()
    assert validate_manifest(raw, now=raw['issued_at'] + 1) == HostBundleManifest.model_validate(raw).model_dump()
    engine = HostBundle.__new__(HostBundle)
    engine.authority_pin = lambda: None
    engine.current = lambda: tmp_path / CANDIDATE
    fixture_record = record()
    fixture_record.update(private_path='must-not-leak', restore_point={
        'scope': 'HAOS disk/UEFI and root LVM restore points; no automatic merge or full-system rollback',
        'state': 'ready', 'ha_snapshot': '20261019T000000Z-1234abcd', 'root_snapshot_name': 'agentic_host_update_fixture',
        'private_journal': 'must-not-leak'})
    engine.records = lambda: {CANDIDATE: fixture_record}
    engine.check_path = tmp_path / 'not-created-check.json'
    result = HostBundleStatus.model_validate(engine.status()).model_dump(mode='json')
    assert result['records'][0]['restore_point']['state'] == 'ready'
    assert 'must-not-leak' not in str(result)
    fixture_record['restore_point'] = {'phase': 'preparing', 'ha_snapshot': None}
    preparing = HostBundleStatus.model_validate(engine.status())
    assert preparing.records[0].restore_point.state == 'preparing'


def test_health_descriptor_is_loaded_code_capability_and_cannot_be_changed_by_its_caller():
    from agentic_controller.host_updates import health_descriptor
    value = health_descriptor()
    assert value == COMPATIBILITY
    value['host_protocol'] = 999
    assert health_descriptor() == COMPATIBILITY
