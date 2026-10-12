"""Actual Controller/transport seam; model/HA replies are fixtures, no account acceptance."""
import asyncio
import json
import os
import time
import uuid

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.bridge import IngressBridge
from agentic_controller.models import utcnow
from fastapi import HTTPException

OWNER = 'a' * 32
OTHER = 'b' * 32


class Runtime:
    def __init__(self):
        self.calls, self.runs = [], {}
        self.pending = False

    async def call(self, method, path, body=None, key=None):
        self.calls.append((method, path, body))
        if path == '/v1/sessions':
            return {'session_id': uuid.uuid4().hex}
        if path.endswith('/runs'):
            value = {'state': 'running' if self.pending else 'succeeded', 'answer': 'fixture only'}
            self.runs[body['run_id']] = value
            return value
        if path.endswith('/cancel'):
            self.runs[path.split('/')[3]] = {'state': 'cancelled'}
            return {'state': 'cancelled'}
        if path.startswith('/v1/runs/'):
            return self.runs[path.split('/')[3].split('?')[0]]
        return {'state': 'needs_auth'}


@pytest.fixture
async def system(store, ha, monkeypatch, tmp_path):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    (tmp_path / 'access-key').write_text('test-only-machine-key-no-account')
    store.set_setting('owner', OWNER)
    ha.token = 'test-only-token'
    async def owner(identifier):
        return {'id': OWNER, 'is_owner': True, 'is_active': True} if identifier == OWNER and not ha.revoked else None
    ha.owner = owner
    runtime = Runtime()
    async def host_status():
        return {'controller_update': {'ingress_bridge_contract': 1}}
    app = create_app(store=store, ha=ha, host_mode=True, hermes_client=runtime, bridge_host_status=host_status)
    bridge = app.state.ingress_bridge
    await bridge.require_host()
    options = bridge.prepare_pairing()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.transport, client=('192.168.177.2', 1)),
            base_url='https://192.168.177.1:8444', headers={'Authorization': 'Bearer ' + options['token']}) as client:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099',
                headers={'Origin': 'http://localhost:8099'}) as host:
            await host.post('/api/host/login', json={'key': 'test-only-machine-key-no-account'})
            host.headers['X-Agentic-CSRF'] = (await host.get('/api/session')).json()['csrf']
            yield app, bridge, client, host, options, runtime
    for task in list(app.state.running.values()):
        if not task.cancelling():
            task.cancel()
    await asyncio.gather(*list(app.state.running.values()), return_exceptions=True)
    await bridge.close()


async def call(system, path='/api/session', method='GET', body=None, **changes):
    _, bridge, client, _, options, _ = system
    value = {'protocol': 1, 'pairing_id': options['pairing_id'], 'user_id': OWNER,
             'method': method, 'path': path, 'body': body}
    if method == 'POST':
        value.update(csrf=bridge.csrf(OWNER, options['pairing_id'], int(time.time()) + 3600),
                     origin='http://homeassistant.local:8123', host='homeassistant.local:8123',
                     fetch_site='same-origin', idempotency_key='fixture-key')
    value.update(changes)
    return await client.post('/v1/request', json=value)


async def complete(app):
    await asyncio.gather(*list(app.state.running.values()))


async def test_bridge_deployment_session_separate_csrf_and_heartbeat(system, ha):
    _, bridge, _, host, _, _ = system
    heartbeat = await call(system, '/api/bridge/heartbeat', user_id='')
    assert heartbeat.status_code == 200 and heartbeat.json()['paired'] is True
    assert bridge.status()['service_last_seen'] and bridge.status()['owner_verified_at'] is None
    assert (await call(system, user_id='')).status_code == 403
    assert (await call(system, '/api/deployment')).json() == {'mode': 'bridge', 'authenticated': True, 'bridge_state': 'ready'}
    response = await call(system)
    assert response.status_code == 200
    value = response.json()
    assert value['mode'] == 'bridge' and value['principal'] == OWNER and value['resume'] is None
    assert len(value['storage_scope']) == 64 and 'set-cookie' not in response.headers
    assert value['csrf'] != (await host.get('/api/session')).json()['csrf']
    ha.revoked = True
    assert (await call(system)).status_code == 403
    assert (await call(system, '/api/bridge/heartbeat', user_id='')).status_code == 200


@pytest.mark.parametrize('protocol', [True, 1.0, '1', 2])
async def test_exact_protocol_required(system, protocol):
    assert (await call(system, protocol=protocol)).status_code == 422


@pytest.mark.parametrize('path,method', [('/api/host', 'GET'), ('/api/host/bridge/maintenance', 'GET'),
    ('/api/workers', 'GET'), ('/api/worker/jobs', 'POST'), ('/api/hermes/auth', 'POST'),
    ('/api/gateway', 'GET'), ('/api/retention', 'POST'), ('/api/pause', 'POST'),
    ('/api/gateway/messages?source=line', 'GET'), ('/api/../host', 'GET')])
async def test_no_host_management_or_generic_proxy(system, path, method):
    assert (await call(system, path, method, {} if method == 'POST' else None)).status_code == 403


async def test_forged_ingress_header_or_service_alone_has_no_owner_authority(system):
    app, _, client, _, options, _ = system
    assert (await call(system, user_id=OTHER)).status_code == 403
    assert (await client.post('/v1/request', json={'protocol': 1, 'pairing_id': options['pairing_id'],
        'user_id': OWNER, 'method': 'GET', 'path': '/api/session'}, headers={'Authorization': 'Bearer wrong'})).status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099') as outsider:
        response = await outsider.get('/api/session', headers={'x-remote-user-id': OWNER, 'Authorization': 'Bearer ' + options['token']})
        assert response.status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.ingress_bridge.transport), base_url='https://bridge') as outsider:
        assert (await outsider.post('/v1/request', json={})).status_code == 403


@pytest.mark.parametrize('changes', [{'csrf': 'bad'}, {'fetch_site': 'cross-site'}, {'host': 'other.example'},
    {'origin': 'https://evil.example'}, {'origin': ''}])
async def test_bridge_mutations_require_scoped_csrf_origin(system, changes):
    assert (await call(system, '/api/gateway/messages', 'POST', {'message': 'hello', 'coordinator': 'hermes'}, **changes)).status_code == 403


async def test_same_owner_same_case_resume_and_lost_response_idempotency(system, store):
    app, _, _, host, options, runtime = system
    response = await host.post('/api/gateway/messages', json={'message': 'outer hello', 'coordinator': 'hermes'}, headers={'Idempotency-Key': 'outer'})
    assert response.status_code == 202
    await complete(app)
    original = store.get('conversation', response.json()['id'])
    resume = (await call(system)).json()['resume']
    assert resume == {'message_id': original['id'], 'thread_id': original['thread_id'], 'case_id': original['case_id']}
    body = {'message': 'continue from HA', 'coordinator': 'hermes', 'reply_to': original['id']}
    received = await call(system, '/api/gateway/messages', 'POST', body)
    assert received.status_code == 202
    await complete(app)
    second = store.get('conversation', received.json()['id'])
    assert second['case_id'] == original['case_id'] and second['bridge_pairing_id'] == options['pairing_id']
    replay = await call(system, '/api/gateway/messages', 'POST', body)
    assert replay.json()['id'] == second['id']
    assert len([entry for entry in runtime.calls if entry[1].endswith('/runs')]) == 2
    assert (await host.get('/api/session')).json()['resume']['message_id'] == second['id']
    view = (await call(system, '/api/gateway/messages')).json()['messages']
    assert len(view) == 2 and all('bridge_pairing_id' not in item and 'scope' not in item for item in view)
    assert (await call(system, '/api/hermes/cases/' + second['case_id'])).status_code == 200


async def test_history_resource_acl_excludes_channels_other_owners_and_manual_jobs(system, store):
    _, _, _, _, _, _ = system
    case_id = uuid.uuid4().hex
    store.put('hermes_case', {'id': 'c'*64, 'case_id': case_id, 'scope': 'd'*64, 'session_id': 's'})
    store.put('conversation', {'id': 'e'*64, 'source': 'telegram', 'chat': 'owner', 'sender': OWNER,
        'coordinator': 'hermes', 'case_id': case_id, 'created_at': utcnow()})
    job_id, task_id, preview_id, proposal_id = [uuid.uuid4().hex for _ in range(4)]
    store.put('work_job', {'id': job_id, 'case_id': case_id})
    store.put('task', {'id': task_id, 'case_id': case_id})
    store.put('proposal', {'id': proposal_id, 'task_id': task_id})
    store.put('worker_preview', {'id': preview_id, 'case_id': case_id})
    for path in [f'/api/hermes/cases/{case_id}', f'/api/tasks/{task_id}', f'/api/worker/jobs/{job_id}',
                 f'/api/worker/jobs/{job_id}/artifacts/artifact', f'/api/worker/previews/{preview_id}']:
        assert (await call(system, path)).status_code == 404
    assert (await call(system, f'/api/proposals/{proposal_id}/approve', 'POST', {'digest': 'a'*64})).status_code == 404
    assert (await call(system, '/api/gateway/messages')).json() == {'messages': []}


async def test_pairing_restart_replay_private_permissions_rotation_and_revocation(system, store, ha):
    app, bridge, _, _, options, _ = system
    assert bridge.prepare_pairing() == options
    another = IngressBridge(app, store, ha, app.state.gateway, app.state.coordinator, store.setting('csrf_secret'),
                            host_status=bridge.host_status)
    with pytest.raises(HTTPException, match='bridge_host_contract_required'):
        another.prepare_pairing()
    await another.require_host()
    assert another.prepare_pairing() == options
    assert options['token'] not in json.dumps(bridge.status()) and options['token'] not in store.path.read_bytes().decode(errors='ignore')
    assert bridge.root.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in bridge.root.iterdir())
    await bridge.revoke()
    assert (await call(system)).status_code == 401
    with pytest.raises(HTTPException, match='bridge_pairing_needs_repair'):
        bridge.prepare_pairing()
    repaired = bridge.prepare_pairing(repair=True)
    assert repaired['pairing_id'] != options['pairing_id'] and repaired['token'] != options['token']
    assert (await call(system)).status_code == 401


async def test_owner_change_while_live_check_awaits_cannot_dispatch(system, store, ha):
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_owner(identifier):
        entered.set()
        await release.wait()
        return {'id': identifier}
    ha.owner = delayed_owner
    task = asyncio.create_task(call(system, '/api/gateway/messages', 'POST', {'message': 'no dispatch', 'coordinator': 'hermes'}))
    await entered.wait()
    store.set_setting('owner', OTHER)
    release.set()
    assert (await task).status_code == 403
    assert not store.list('conversation')


async def test_revoke_stops_exact_bridge_run_revokes_mcp_without_stopping_host(system, store):
    app, bridge, _, _, _, runtime = system
    runtime.pending = True
    result = await call(system, '/api/gateway/messages', 'POST', {'message': 'long research', 'coordinator': 'hermes'})
    for _ in range(20):
        if store.list('mcp_grant'):
            break
        await asyncio.sleep(.01)
    source = store.get('conversation', result.json()['id'])
    grant = store.list('mcp_grant')[0]
    assert app.state.gateway.scope_valid(source)
    await bridge.revoke()
    assert not app.state.gateway.scope_valid(source)
    assert store.get('mcp_grant', grant['id'])['revoked'] is True
    assert store.get('hermes_run', grant['run_id'])['state'] == 'cancelled'
    assert store.get('conversation', source['id'])['state'] == 'interrupted'


async def test_paused_and_updater_gate_preserve_outer_controls(system, store):
    store.set_setting('paused', True)
    assert (await call(system, '/api/gateway/messages', 'POST', {'message': 'hello', 'coordinator': 'hermes'})).status_code == 409
    store.set_setting('updater_gate', 'a'*64)
    assert (await call(system, '/api/gateway/messages', 'POST', {'message': 'hello', 'coordinator': 'hermes'})).status_code == 423
    assert (await call(system)).status_code == 200


async def test_preview_projection_never_leaks_local_capability(system):
    _, bridge, _, _, _, _ = system
    identifier = uuid.uuid4().hex
    value = bridge.preview_view({'id': identifier, 'state': 'ready', 'preview_urls': {'390': 'http://127.0.0.2:8100/p/secret/390.png'}, 'capability': 'secret', 'container': 'secret'})
    assert value['preview_urls'] == {'390': f'api/bridge/previews/{identifier}/390.png'}
    assert 'secret' not in json.dumps(value)


async def test_invalid_private_file_fails_closed_without_blocking(system):
    _, bridge, _, _, _, _ = system
    token = bridge.root / 'service-token'
    token.unlink()
    os.mkfifo(token, 0o600)
    with pytest.raises(HTTPException, match='bridge_pairing_needs_repair'):
        bridge.prepare_pairing()


async def test_preview_guard_rechecks_pairing_after_await(system):
    app, bridge, _, _, options, _ = system
    authority = {'owner': OWNER, 'expires': time.time()+100, 'web_epoch': None, 'bridge_pairing_id': options['pairing_id']}
    await call(system)
    app.state.operations.guard(authority)
    await bridge.revoke()
    with pytest.raises(HTTPException, match='owner_authority_revoked'):
        app.state.operations.guard(authority)


@pytest.mark.parametrize('changes', [{'origin': 'http://homeassistant.local:8123/path'},
    {'origin': 'http://homeassistant.local:8123?q=x'}, {'origin': 'http://homeassistant.local:8123#fragment'},
    {'origin': 'http://user:pass@homeassistant.local:8123', 'host': 'user:pass@homeassistant.local:8123'},
    {'fetch_site': 'none'}])
async def test_transport_rejects_non_origin_metadata(system, changes):
    assert (await call(system, '/api/gateway/messages', 'POST', {'message': 'hello', 'coordinator': 'hermes'}, **changes)).status_code == 403


@pytest.mark.parametrize('body', [{'message': 'hello'}, {'message': 'hello', 'coordinator': 'direct'},
    {'message': 'hello', 'coordinator': 'hermes', 'runtime': 'haos'},
    {'message': 'hello', 'coordinator': 'hermes', 'provider': 'claude'}])
async def test_no_silent_coordinator_or_cli_switch(system, body):
    assert (await call(system, '/api/gateway/messages', 'POST', body)).status_code == 422


async def test_revoke_recovers_persisted_orphan_without_canceling_host_run(system, store):
    _, bridge, _, _, options, runtime = system
    bridge_id, host_id, bridge_run, host_run = ['1'*64, '2'*64, '3'*32, '4'*32]
    store.put('conversation', {'id': bridge_id, 'bridge_pairing_id': options['pairing_id']})
    store.put('conversation', {'id': host_id})
    for message_id, run_id in [(bridge_id, bridge_run), (host_id, host_run)]:
        store.put('hermes_run', {'id': run_id, 'message_id': message_id, 'scope': '5'*64, 'state': 'running'})
    await bridge.revoke()
    assert store.get('hermes_run', bridge_run)['state'] == 'cancelled'
    assert store.get('hermes_run', host_run)['state'] == 'running'
    assert [entry[1] for entry in runtime.calls if entry[1].endswith('/cancel')] == [f'/v1/runs/{bridge_run}/cancel']


async def test_stream_request_body_limit_without_content_length(system):
    _, _, client, _, _, _ = system
    async def chunks():
        for _ in range(3):
            yield b'x'*65536
    response = await client.post('/v1/request', content=chunks())
    assert response.status_code == 413


async def test_bridge_task_reply_stays_in_same_coordinator_case(system, store):
    app, _, _, _, _, runtime = system
    initial = await call(system, '/api/gateway/messages', 'POST', {'message': 'clarify household', 'coordinator': 'hermes'})
    await complete(app)
    source = store.get('conversation', initial.json()['id'])
    task = app.state.engine.task('needs a room', 'codex')
    task.update(state='awaiting_user', case_id=source['case_id'])
    store.put('task', task)
    store.put('conversation', {**source, 'task_id': task['id'], 'state': 'awaiting_user'})
    response = await call(system, f"/api/tasks/{task['id']}/reply", 'POST', {'message': 'living room'}, idempotency_key='reply')
    assert response.status_code == 202
    await complete(app)
    followup = store.get('conversation', response.json()['continued_by'])
    assert followup['case_id'] == source['case_id'] and followup['coordinator'] == 'hermes'
    assert (await call(system, f"/api/tasks/{task['id']}/reply", 'POST', {'message': 'living room'}, idempotency_key='reply')).status_code == 202
    assert len([entry for entry in runtime.calls if entry[1].endswith('/runs')]) == 2


async def seed_case(system, store):
    app = system[0]
    response = await call(system, '/api/gateway/messages', 'POST', {'message': 'fixture case', 'coordinator': 'hermes'})
    await complete(app)
    return store.get('conversation', response.json()['id'])['case_id']


async def test_exact_household_approval_uses_existing_engine_and_current_owner(system, store, ha):
    from agentic_controller.models import DeviceAction
    app = system[0]
    case_id = await seed_case(system, store)
    task = app.state.engine.task('turn on helper', 'codex')
    task['case_id'] = case_id
    store.put('task', task)
    proposed = await app.state.engine.propose(task, action=DeviceAction(entity_id='input_boolean.agentic_lamp_test', action='turn_on'))
    proposal = store.get('proposal', proposed['proposal_id'])
    assert not ha.calls and not store.list('approval')
    path = '/api/proposals/' + proposal['id'] + '/approve'
    assert (await call(system, path, 'POST', {'digest': '0'*64})).status_code == 422
    assert not store.list('approval')
    ha.revoked = True
    assert (await call(system, path, 'POST', {'digest': proposal['digest']})).status_code == 403
    ha.revoked = False
    response = await call(system, path, 'POST', {'digest': proposal['digest']})
    assert response.status_code == 202
    await complete(app)
    assert store.get('approval', proposal['id'])['principal'] == OWNER
    assert ha.calls == [('input_boolean', 'turn_on', 'input_boolean.agentic_lamp_test')]
    assert (await call(system, path, 'POST', {'digest': proposal['digest']})).status_code == 202
    await complete(app)
    assert len(ha.calls) == 1


@pytest.mark.parametrize('kind', ['valid', 'hash_mismatch', 'html', 'redirect', 'caller_url', 'revoked'])
async def test_png_relay_fixed_target_type_hash_and_post_await_owner_scope(system, store, monkeypatch, kind):
    import hashlib

    import agentic_controller.bridge as module
    app, bridge = system[:2]
    case_id = await seed_case(system, store)
    identifier = uuid.uuid4().hex
    store.put('worker_preview', {'id': identifier, 'case_id': case_id})
    data = b'\x89PNG\r\n\x1a\nfixture-bytes'
    expected = hashlib.sha256(data).hexdigest()
    url = 'http://127.0.0.2:8100/p/' + 'a'*43 + '/390.png'
    async def status(unused):
        return {'id': identifier, 'state': 'ready', 'preview_urls': {'390': 'http://attacker/' if kind == 'caller_url' else url},
                'image_digests': {'390': '0'*64 if kind == 'hash_mismatch' else expected}}
    monkeypatch.setattr(app.state.operations, 'preview_status', status)
    fetched = []
    class Response:
        status = 302 if kind == 'redirect' else 200
        content_type = 'text/html' if kind == 'html' else 'image/png'
        @property
        def content(self):
            return self
        async def iter_chunked(self, size):
            if kind == 'revoked':
                await bridge.revoke()
            yield data
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    class Session:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, target, **kwargs):
            assert target == url and kwargs == {'allow_redirects': False}
            fetched.append(target)
            return Response()
    monkeypatch.setattr(module.aiohttp, 'ClientSession', Session)
    response = await call(system, f'/api/bridge/previews/{identifier}/390.png')
    if kind == 'valid':
        assert response.status_code == 200 and response.content == data
        assert response.headers['content-type'] == 'image/png'
    else:
        assert response.status_code == (404 if kind == 'caller_url' else 403 if kind == 'revoked' else 502)
    assert len(fetched) == (0 if kind == 'caller_url' else 1)


async def test_unknown_stop_is_retained_and_next_writer_not_claimed_stopped(system, store, monkeypatch):
    from agentic_controller.hermes import HermesError
    app, bridge, _, _, options, runtime = system
    message, run_id = '6'*64, '7'*32
    store.put('conversation', {'id': message, 'bridge_pairing_id': options['pairing_id']})
    store.put('hermes_run', {'id': run_id, 'message_id': message, 'scope': '8'*64, 'state': 'running'})
    async def unavailable(*args):
        raise HermesError('runtime_unavailable')
    monkeypatch.setattr(runtime, 'call', unavailable)
    await bridge.revoke()
    assert store.get('hermes_run', run_id)['state'] == 'stop_unconfirmed'
    assert bridge.status()['state'] == 'revoked'
    assert app.state.gateway.bridge_valid({'bridge_pairing_id': options['pairing_id'], 'sender': OWNER}) is False


async def test_old_csrf_is_not_accepted_after_pairing_repair(system):
    _, bridge, client, _, options, _ = system
    token = bridge.csrf(OWNER, options['pairing_id'], int(time.time())+3600)
    await bridge.revoke()
    new = bridge.prepare_pairing(repair=True)
    client.headers['Authorization'] = 'Bearer ' + new['token']
    response = await call(system, '/api/gateway/messages', 'POST', {'message': 'no replay', 'coordinator': 'hermes'},
                          pairing_id=new['pairing_id'], csrf=token)
    assert response.status_code == 403


async def test_expired_context_cannot_resume_or_authorize_case_before_pruner_runs(system, store):
    from datetime import UTC, datetime, timedelta
    app = system[0]
    case_id = await seed_case(system, store)
    identifier = store.list('conversation')[0]['id']
    old = (datetime.now(UTC)-timedelta(days=2)).isoformat()
    store.db.execute("UPDATE records SET created=? WHERE kind='conversation' AND id=?", (old, identifier))
    store.db.commit()
    store.set_setting('retention_pending', {'audit_days': 30, 'context_days': 1})
    assert store.get('conversation', identifier)  # Prove no periodic deletion was needed.
    assert (await call(system)).json()['resume'] is None
    assert (await call(system, '/api/gateway/messages')).json()['messages'] == []
    assert (await call(system, '/api/hermes/cases/' + case_id)).status_code == 404
    assert app.state.gateway.resume(OWNER) is None


async def test_expired_internal_marker_cannot_skip_mutation_csrf(system):
    from agentic_controller.bridge import MARKER, BridgeIdentity
    from starlette.requests import Request
    _, bridge, _, _, options, _ = system
    await call(system)
    scope = {'type': 'http', 'method': 'POST', 'path': '/api/gateway/messages', 'headers': [],
             MARKER: BridgeIdentity(bridge.issuer, options['pairing_id'], OWNER, 'POST', '/api/gateway/messages', int(time.time())-1)}
    with pytest.raises(HTTPException, match='bridge_authority_revoked'):
        bridge.identity(Request(scope))


async def initial_repair(system, **kwargs):
    host = system[3]
    return await host.post('/api/host/bridge/maintenance/repair', json={}, **kwargs)


@pytest.mark.parametrize('capability', [None, {}, {'controller_update': None},
    *({'controller_update': {'ingress_bridge_contract': value}} for value in (None, False, True, '1', 1.0, 0, 2))])
async def test_legacy_or_malformed_host_cannot_create_bridge_authority(store, ha, monkeypatch, tmp_path, capability):
    from agentic_controller.bridge_maintenance import BridgeFault
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    ha.token = 'fixture-ha-token'
    store.set_setting('owner', OWNER)
    store.put('bootstrap_v1', {'id': 'setup', 'state': 'completed', 'bridge_auto_enroll': True})
    # A persisted claim from an old process is deliberately not a capability.
    store.set_setting('controller_update', {'ingress_bridge_contract': 1})
    async def status():
        return capability
    app = create_app(store=store, ha=ha, host_mode=True, bridge_host_status=status)
    bridge, manager = app.state.ingress_bridge, app.state.bridge_maintenance
    with pytest.raises(BridgeFault, match='bridge_host_contract_required'):
        await manager.authorize(auto_repair=True)
    result = await manager.tick()
    assert result['state'] == 'unavailable' and result['code'] == 'bridge_host_contract_required'
    assert manager.record() is None and not bridge.record() and not bridge.root.exists()
    with pytest.raises(HTTPException, match='bridge_host_contract_required'):
        bridge.prepare_pairing(repair=True)
    await bridge.close()


@pytest.mark.parametrize('change', ['owner', 'token', 'epoch'])
async def test_host_proof_cannot_cross_owner_or_session_change_while_awaiting(system, store, ha, change):
    bridge = system[1]
    entered, release = asyncio.Event(), asyncio.Event()
    async def status():
        entered.set()
        await release.wait()
        return {'controller_update': {'ingress_bridge_contract': 1}}
    bridge.host_status, bridge.host_valid_until = status, 0
    pending = asyncio.create_task(call(system))
    await entered.wait()
    if change == 'owner':
        store.set_setting('owner', OTHER)
    elif change == 'token':
        ha.token = 'changed-fixture-token'
    else:
        store.set_setting('hermes_web_epoch', 'new-session-epoch')
    release.set()
    response = await pending
    assert response.status_code == 403 and response.json()['detail'] == 'bridge_authority_revoked'
    assert not bridge.host_current() and bridge.owner_valid_until == 0


async def test_host_outage_fences_live_model_and_outer_rescue_status_stays_read_only(system, store):
    app, bridge, _, host, _, runtime = system
    runtime.pending = True
    accepted = await call(system, '/api/gateway/messages', 'POST', {'message': 'investigate', 'coordinator': 'hermes'})
    for _ in range(50):
        if runtime.runs:
            break
        await asyncio.sleep(0.01)
    assert accepted.status_code == 202 and runtime.runs
    calls = []
    async def unavailable():
        calls.append(True)
        raise OSError('private diagnostic must not escape')
    bridge.host_status, bridge.host_valid_until = unavailable, 0
    response = await call(system)
    assert response.status_code == 503 and 'private diagnostic' not in response.text
    await bridge.host_stop_task
    assert all(value['state'] == 'cancelled' for value in runtime.runs.values())
    grants = store.list('mcp_grant')
    assert grants and all(item['revoked'] is True for item in grants)
    before = len(calls)
    assert (await host.get('/api/session')).status_code == 200
    assert (await host.get('/api/host/bridge/maintenance')).status_code == 200
    assert len(calls) == before and not bridge.host_current()
    assert not app.state.gateway.running


async def test_host_recovery_does_not_clear_pending_worker_stop_or_uncertain_model(system, store, monkeypatch):
    from agentic_controller.hermes import HermesError
    app, bridge, _, _, options, runtime = system
    message, run_id, job_id = '6'*64, '7'*32, '9'*32
    store.put('conversation', {'id': message, 'bridge_pairing_id': options['pairing_id']})
    store.put('hermes_run', {'id': run_id, 'message_id': message, 'scope': '8'*64, 'state': 'running'})
    store.put('work_job', {'id': job_id, 'state': 'running', 'created_epoch': time.time(),
        'authority': {'message_id': message, 'bridge_pairing_id': options['pairing_id']},
        'execution_started': True, 'stop_confirmed': False, 'dispatch_attempted': True})
    lease = {'job_id': job_id, 'epoch': 1}
    store.set_setting('worker_lease', lease)
    async def unavailable(*args):
        raise HermesError('runtime_unavailable')
    monkeypatch.setattr(runtime, 'call', unavailable)
    bridge.invalidate_host()
    await bridge.require_host()
    assert bridge.host_current()
    assert store.get('hermes_run', run_id)['state'] == 'stop_unconfirmed'
    worker = store.get('work_job', job_id)
    assert worker['state'] == 'cancel_requested' and worker['cancel_requested'] is True
    assert worker['stop_confirmed'] is False and store.setting('worker_lease') == lease
    assert app.state.coordinator.worker_manager.public_capabilities()['active_job_id'] == job_id


async def test_host_recovery_waits_for_previous_cancellation_persistence(system, monkeypatch):
    bridge = system[1]
    entered, release = asyncio.Event(), asyncio.Event()
    async def fence(generation):
        entered.set()
        await release.wait()
    monkeypatch.setattr(bridge, 'stop_origin', fence)
    bridge.invalidate_host()
    await entered.wait()
    pending = asyncio.create_task(bridge.require_host())
    await asyncio.sleep(0)
    assert not pending.done() and not bridge.host_current()
    release.set()
    await pending
    assert bridge.host_current()


async def test_host_gate_timeout_covers_lock_wait_and_keeps_pairing_unchanged(system, monkeypatch):
    import agentic_controller.bridge as module
    bridge, options = system[1], system[4]
    monkeypatch.setattr(module, 'HOST_PROOF_TIMEOUT', 0.05)
    bridge.host_valid_until = 0
    await bridge.host_lock.acquire()
    try:
        response = await asyncio.wait_for(call(system), 1)
    finally:
        bridge.host_lock.release()
    assert response.status_code == 503 and response.json()['detail'] == 'bridge_host_unavailable'
    assert bridge.record()['pairing_id'] == options['pairing_id'] and not bridge.host_current()


async def test_initial_private_identity_repair_cannot_bypass_legacy_host_gate(system, store):
    app, bridge = system[:2]
    store.put('bootstrap_v1', {'id': 'setup', 'state': 'completed'})
    (bridge.root / 'tls.key').write_bytes(b'partial private key')
    prior = bridge.record()
    async def old_host():
        return {'vm': 'running'}
    bridge.host_status, bridge.host_valid_until = old_host, 0
    response = await initial_repair(system)
    assert response.status_code == 503 and response.json()['detail'] == 'bridge_host_contract_required'
    assert bridge.record() == prior and (bridge.root / 'tls.key').read_bytes() == b'partial private key'
    assert app.state.bridge_maintenance.record() is None


async def test_restored_identity_listener_requires_new_process_host_proof(system, store, ha):
    app, original = system[:2]
    observed = asyncio.Event()
    async def old_host():
        observed.set()
        return {'vm': 'running'}
    restarted = IngressBridge(app, store, ha, app.state.gateway, app.state.coordinator,
                              store.setting('csrf_secret'), host_status=old_host)
    task = asyncio.create_task(restarted.listen())
    try:
        await asyncio.wait_for(observed.wait(), 1)
        await asyncio.sleep(0)
        assert not restarted.listener_ready and restarted.server is None
        assert not restarted.host_current() and original.record()['pairing_id'] == restarted.record()['pairing_id']
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await restarted.close()


@pytest.mark.parametrize('orphan', [True, False])
async def test_explicit_initial_repair_recovers_orphan_or_corrupt_private_identity_and_replays(system, store, orphan):
    app, bridge, _, _, options, _ = system
    store.put('bootstrap_v1', {'id': 'setup', 'state': 'completed', 'run_id': 'fixture'})
    if orphan:
        store.db.execute("DELETE FROM settings WHERE key='ingress_bridge'")
        store.db.commit()
        (bridge.root / ('.' + 'f'*32)).write_bytes(b'partial private write')
        (bridge.root / ('.' + 'f'*32)).chmod(0o600)
    else:
        (bridge.root / 'tls.key').write_bytes(b'partial key')
    store.set_setting('bridge_enrollment', {'state': 'unavailable', 'code': 'bridge_owner_observation_unavailable'})
    with pytest.raises(HTTPException):
        bridge.prepare_pairing()
    response = await initial_repair(system)
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'not_authorized'
    current = bridge.prepare_pairing()
    assert current['pairing_id'] != options['pairing_id']
    assert current['token'] not in response.text
    assert app.state.bridge_maintenance.record() is None
    assert not list(bridge.root.glob('.*'))
    assert (await initial_repair(system)).status_code == 200
    assert bridge.prepare_pairing() == current
    # Existing explicit enrollment still works; no Supervisor side effect occurs in repair.
    response = await system[3].post('/api/host/bridge/maintenance/authorize', json={'auto_repair': True})
    assert response.status_code == 200 and response.json()['state'] == 'pending'
    assert app.state.bridge_maintenance.record()['pairing_id'] == current['pairing_id']
    assert not app.state.bridge_maintenance.record()['managed']


@pytest.mark.parametrize('unsafe', ['fifo', 'symlink', 'hardlink', 'unknown', 'directory'])
async def test_initial_repair_never_overwrites_unsafe_private_paths(system, store, tmp_path, unsafe):
    _, bridge, _, _, options, _ = system
    store.put('bootstrap_v1', {'id': 'setup', 'state': 'completed'})
    store.db.execute("DELETE FROM settings WHERE key='ingress_bridge'")
    store.db.commit()
    target = bridge.root / 'service-token'
    target.unlink()
    other = tmp_path / 'unrelated'
    other.write_bytes(b'unchanged unrelated file')
    other.chmod(0o600)
    if unsafe == 'fifo':
        os.mkfifo(target, 0o600)
    elif unsafe == 'symlink':
        target.symlink_to(other)
    elif unsafe == 'hardlink':
        os.link(other, target)
    elif unsafe == 'directory':
        target.mkdir(mode=0o700)
    else:
        target.write_text(options['token'])
        target.chmod(0o600)
        (bridge.root / 'user-notes').write_text('do not replace')
    before = target.lstat().st_ino
    response = await initial_repair(system)
    assert response.status_code == 409 and response.json()['detail'] == 'bridge_pairing_needs_repair'
    assert target.lstat().st_ino == before and other.read_bytes() == b'unchanged unrelated file'
    assert not store.setting('ingress_bridge')


@pytest.mark.parametrize('condition', ['connected', 'revoked', 'seen', 'historical_job', 'bootstrap_incomplete', 'owner_revoked'])
async def test_initial_repair_requires_unused_identity_completed_setup_and_active_owner(system, store, ha, condition):
    _, bridge, _, _, options, _ = system
    store.put('bootstrap_v1', {'id': 'setup', 'state': 'running' if condition == 'bootstrap_incomplete' else 'completed'})
    record = bridge.record()
    if condition in {'connected', 'revoked'}:
        store.set_setting('ingress_bridge', {**record, 'state': condition})
    elif condition == 'seen':
        store.set_setting('ingress_bridge', {**record, 'service_last_seen': time.time()})
    elif condition == 'historical_job':
        store.put('conversation', {'id': 'e'*64, 'bridge_pairing_id': options['pairing_id'], 'state': 'succeeded'})
    elif condition == 'owner_revoked':
        ha.revoked = True
    original = (bridge.root / 'service-token').read_bytes()
    response = await initial_repair(system)
    assert response.status_code == 409
    assert (bridge.root / 'service-token').read_bytes() == original


@pytest.mark.parametrize('changed', ['host_logout', 'owner', 'manager', 'revoke'])
async def test_initial_repair_rechecks_authority_after_owner_await(system, store, ha, changed):
    app, bridge, _, _, options, _ = system
    store.put('bootstrap_v1', {'id': 'setup', 'state': 'completed'})
    entered, release = asyncio.Event(), asyncio.Event()
    async def owner(identifier):
        entered.set()
        await release.wait()
        return {'id': identifier}
    ha.owner = owner
    pending = asyncio.create_task(initial_repair(system))
    await entered.wait()
    if changed == 'host_logout':
        store.set_setting('hermes_web_epoch', uuid.uuid4().hex)
    elif changed == 'owner':
        store.set_setting('owner', OTHER)
    elif changed == 'manager':
        store.set_setting('bridge_maintenance', {'id': 'pending-effect', 'managed': True,
            'operation': {'state': 'sent', 'kind': 'install'}})
    else:
        await bridge.revoke()
    release.set()
    response = await pending
    assert response.status_code == 409
    assert bridge.record()['pairing_id'] == options['pairing_id']
    assert app.state.bridge_maintenance.record() is None or changed == 'manager'
