"""Durable worker protocol fixtures. No claim of cloud or HAOS execution."""
import asyncio
import base64
import hashlib
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.hermes import Coordinator
from agentic_controller.runner import RunnerError
from agentic_controller.store import Conflict, Store
from agentic_controller.work import WorkerStart, WorkerSubmit
from agentic_controller.workers import WorkerError, Workers


class CodingRunner:
    def __init__(self, worker):
        self.worker, self.jobs, self.calls = worker, {}, []
        self.location = 'host_container' if worker == 'host-coder' else 'haos_app'
        self.lost, self.offline, self.unconfirmed = False, False, False
        self.artifacts, self.auth = {}, {}

    async def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.offline:
            raise RunnerError('offline')
        if path == '/coding/capabilities':
            return {'work_contract_major': 1, 'worker_id': self.worker, 'runtime_location': self.location,
                    'available': True, 'state': 'ready', 'modes': ['research', 'development'],
                    'providers': [{'id': 'codex', 'supported': True, 'models': ['fixture-model'],
                        'auth_methods': ['device_code', 'api_key'], 'credential_state': 'configured'}]}
        if path == '/coding/jobs':
            assert body['id'] not in self.jobs
            self.jobs[body['id']] = {**body, 'state': 'running', 'execution_started': True,
                                    'stop_confirmed': False, 'runtime_location': self.location}
            if self.lost:
                raise RunnerError('lost response')
            return self.jobs[body['id']]
        if path.startswith('/coding/jobs/'):
            identifier = path.split('/')[3]
            if identifier not in self.jobs:
                raise RunnerError('not found', 404)
            if '/artifacts/' in path:
                artifact = path.rsplit('/', 1)[1]
                return {'id': artifact, 'job_id': identifier, 'content_base64': self.artifacts[artifact]}
            if path.endswith('/cancel'):
                self.jobs[identifier].update(state='stop_unconfirmed' if self.unconfirmed else 'cancelled',
                                             stop_confirmed=not self.unconfirmed)
            return self.jobs[identifier]
        if path == '/retention':
            return body
        if path.endswith('/logout'):
            self.auth[body['id']] = {'id':body['id'], 'status':'disconnected'}
            return self.auth[body['id']]
        if '/auth/' in path:
            identifier = path.split('/')[3]
            if identifier not in self.auth:
                raise RunnerError('unknown auth', 404)
            return {**self.auth[identifier], 'status':'disconnected' if self.auth[identifier]['status'] == 'disconnected' else 'connected'}
        result = {'id': body['id'], 'status': 'pending', 'url': 'https://auth.openai.com/codex/device',
                  'code': 'ABCD-EFGH', 'private_token': 'never expose'}
        self.auth[body['id']] = result
        return result


@pytest.fixture
async def workers(store):
    coordinator = Coordinator(store)
    runners = {'host': CodingRunner('host-coder'), 'haos': CodingRunner('home-coder')}
    result = Workers(store, runners, coordinator)
    result.closing = True  # Deterministic steps, no unrelated background inference.
    yield result
    await result.close()


def owner():
    return SimpleNamespace(cookies={'agentic_host': str(int(time.time()) + 3600) + '.test'})


async def submit(workers, key='same', worker='host-coder', **extra):
    return await workers.submit(WorkerStart(worker_id=worker, mode='research', goal='Investigate fixture', **extra), key, request=owner())


async def test_single_total_lease_and_lost_reply_recovered_without_duplicate_inference(workers):
    host, home = workers.runners.values()
    host.lost = True
    first = await submit(workers)
    second = await submit(workers, 'second', 'home-coder')
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'needs_reconcile'
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'running'
    assert not home.jobs
    host.jobs[first['id']].update(state='succeeded', stop_confirmed=True)
    await workers.advance()
    await workers.advance()
    assert second['id'] in home.jobs
    assert sum(c[1] == '/coding/jobs' for c in host.calls) == 1


async def test_restart_reconciles_exact_lease_and_pins_runner_identity(workers):
    first = await submit(workers)
    await workers.advance()
    restarted = Workers(workers.store, workers.runners, workers.coordinator)
    restarted.closing = True
    await restarted.advance()
    assert restarted.get(first['id'])['state'] == 'running'
    assert sum(c[1] == '/coding/jobs' for c in workers.runners['host'].calls) == 1
    workers.store.set_setting('runner', {'host': 'new-peer'})
    calls = len(workers.runners['host'].calls)
    await restarted.advance()
    assert restarted.get(first['id'])['reason'] == 'runner_identity_changed'
    assert workers.store.setting('worker_lease')['job_id'] == first['id']
    assert len(workers.runners['host'].calls) == calls


async def test_cancel_intent_never_releases_uncertain_writer(workers):
    first = await submit(workers)
    await submit(workers, 'second', 'home-coder')
    await workers.advance()
    workers.runners['host'].unconfirmed = True
    assert (await workers.cancel(first['id']))['state'] == 'cancel_requested'
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'stop_unconfirmed'
    assert workers.store.setting('worker_lease')['job_id'] == first['id']
    assert not workers.runners['haos'].jobs
    workers.runners['host'].unconfirmed = False
    await workers.advance()
    await workers.advance()
    assert workers.runners['haos'].jobs


async def test_runner_queued_acceptance_is_not_safe_to_cancel_locally(workers):
    first = await submit(workers)
    await workers.advance()
    workers.runners['host'].jobs[first['id']].update(state='queued', execution_started=False)
    await workers.advance()
    assert (await workers.cancel(first['id']))['state'] == 'cancel_requested'
    assert workers.store.setting('worker_lease')
    await workers.advance()
    assert not workers.store.setting('worker_lease')


async def test_scope_revocation_during_capabilities_never_commits(workers):
    original = workers.capabilities
    async def revoke():
        value = await original()
        workers.store.set_setting('hermes_web_epoch', 'rotated')
        return value
    workers.capabilities = revoke
    with pytest.raises(WorkerError, match='worker_authority_revoked'):
        await submit(workers)
    assert not workers.store.list('work_job')


async def test_revoke_running_scope_fences_and_discards_late_findings(workers):
    first = await submit(workers)
    await workers.advance()
    workers.store.set_setting('hermes_web_epoch', 'rotated')
    workers.runners['host'].jobs[first['id']]['findings'] = {'facts': ['private late result']}
    await workers.advance()
    result = workers.get(first['id'])
    assert result['state'] == 'cancelled' and not result['facts']
    assert not workers.store.setting('worker_lease')


@pytest.mark.parametrize('field,value', [('id', 'a'*32), ('lease_epoch', 2), ('runtime_location', 'haos_app'), ('stop_confirmed', False)])
async def test_invalid_stop_receipt_keeps_global_lease(workers, field, value):
    first = await submit(workers)
    await workers.advance()
    workers.runners['host'].jobs[first['id']].update(state='succeeded', stop_confirmed=True)
    workers.runners['host'].jobs[first['id']][field] = value
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'needs_reconcile'
    assert workers.store.setting('worker_lease')['job_id'] == first['id']


def artifact(content=b'<h1>fixture</h1>', name='index.html'):
    return {'id': uuid.uuid4().hex, 'name': name, 'kind': 'ui', 'media_type': 'text/html',
            'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}


async def test_artifacts_validated_atomically_and_bytes_rehashed(workers):
    first = await submit(workers)
    await workers.advance()
    metadata = artifact()
    result = workers.runners['host'].jobs[first['id']]
    result.update(state='succeeded', stop_confirmed=True, artifacts=[metadata, artifact(name='../escape')])
    await workers.advance()
    assert not workers.store.list('worker_artifact')
    result['artifacts'] = [metadata]
    await workers.advance()
    workers.runners['host'].artifacts[metadata['id']] = base64.b64encode(b'<h1>fixture</h1>').decode()
    assert (await workers.artifact(first['id'], metadata['id']))['sha256'] == metadata['sha256']
    workers.runners['host'].artifacts[metadata['id']] = base64.b64encode(b'changed').decode()
    with pytest.raises(WorkerError, match='artifact_digest_mismatch'):
        await workers.artifact(first['id'], metadata['id'])


async def test_retention_tombstone_prevents_context_and_idempotency_resurrection(workers):
    first = await submit(workers)
    await workers.advance()
    workers.runners['host'].jobs[first['id']].update(state='succeeded', stop_confirmed=True)
    await workers.advance()
    item = workers.get(first['id'])
    workers.store.put('work_job', {**item, 'created_epoch': time.time() - 2 * 86400})
    workers.store.set_setting('retention', {'context_days': 1, 'audit_days': 30})
    assert workers.get(first['id'])['content_expired']
    workers.store.set_setting('retention', {'context_days': 7, 'audit_days': 30})
    retry = await submit(workers)
    assert retry['id'] == first['id'] and retry['content_expired'] and 'goal' not in retry
    with pytest.raises(Conflict):
        await workers.submit(WorkerStart(worker_id='host-coder', mode='research', goal='changed'), 'same', request=owner())
    assert len(workers.runners['host'].jobs) == 1


async def test_role_auth_is_independent_and_never_persists_secret(workers):
    first = await submit(workers)
    result = await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth', {'method': 'api_key', 'secret': 'private-fixture-key'}, key='auth-first')
    assert 'private_token' not in result and result['code'] == 'ABCD-EFGH'
    assert workers.store.setting('worker_auth_pending')
    await workers.advance()
    assert not workers.runners['host'].jobs
    await workers.role_auth('host-coder', 'GET', '/coding/auth/' + result['id'])
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'cancelled'
    await submit(workers, 'new-authority')
    await workers.advance()
    with pytest.raises(WorkerError, match='coding_job_must_stop_before_auth'):
        await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/logout', {})
    assert 'private-fixture-key' not in workers.store.path.read_bytes().decode(errors='ignore')


async def test_mcp_cannot_access_other_case_or_inject_authority(workers):
    first = await submit(workers)
    with pytest.raises(WorkerError, match='worker_job_not_found'):
        workers.get(first['id'], 'f' * 32)
    with pytest.raises(ValueError):
        WorkerSubmit(worker_id='host-coder', mode='research', goal='goal', idempotency_key='valid-key', endpoint='http://attacker')


async def test_owner_routes_work_without_ha_and_require_csrf(store, ha, tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    (tmp_path / 'access-key').write_text('fixture-machine-access-key')
    app = create_app(store=store, ha=ha, host_mode=True, runner=CodingRunner('host-coder'), inner_runner=CodingRunner('home-coder'))
    workers = app.state.coordinator.worker_manager
    workers.closing = True
    ha.revoked = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099', headers={'Origin':'http://localhost:8099'}) as client:
        assert (await client.post('/api/host/login', json={'key':'fixture-machine-access-key'})).status_code == 200
        payload = {'worker_id':'host-coder','mode':'research','goal':'fixture'}
        assert (await client.post('/api/worker/jobs', json=payload)).status_code == 403
        client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
        reply = await client.post('/api/worker/jobs', json=payload, headers={'Idempotency-Key':'owner-one'})
        assert reply.status_code == 202, reply.text
        assert 'authority' not in reply.json() and 'request' not in reply.json()
        assert len((await client.get('/api/worker/jobs')).json()['jobs']) == 1
        assert (await client.post('/api/workers/host-coder/providers/codex/auth', json={'method':'device_code'}, headers={'Idempotency-Key':'owner-login'})).status_code == 202
        assert not ha.calls and not ha.writes


def test_schema_three_refuses_old_binary_and_private_snapshot(tmp_path):
    path = tmp_path / 'store.sqlite3'
    old = Store(path)
    old.db.execute('PRAGMA user_version=2')
    old.close()
    new = Store(path)
    assert new.db.execute('PRAGMA user_version').fetchone()[0] == 3
    copies = list(tmp_path.glob('*.before-v3-*'))
    assert len(copies) == 1 and copies[0].stat().st_mode & 0o777 == 0o600
    new.close()


async def test_retention_propagates_all_peers_and_failed_sync_is_not_saved(workers):
    class Bridge:
        async def call(self, method, path, body):
            return body
    workers.coordinator.client = Bridge()
    workers.store.set_settings({'runner': {'host':'host'}, 'runner_haos':{'host':'haos'},
                               'retention':{'context_days':7, 'audit_days':30}})
    workers.runners['haos'].offline = True
    policy = {'context_days':1, 'audit_days':30}
    with pytest.raises(WorkerError, match='retention_sync_pending'):
        await workers.update_retention(policy)
    assert workers.store.setting('retention')['context_days'] == 7
    assert workers.context_days() == 1
    assert workers.store.setting('retention_pending') == policy
    with pytest.raises(WorkerError, match='retention_sync_pending'):
        await submit(workers)
    workers.runners['haos'].offline = False
    await workers.sync_retention()
    assert workers.store.setting('retention') == policy and not workers.store.setting('retention_pending')
    assert any(c[1] == '/retention' for c in workers.runners['host'].calls)
    assert any(c[1] == '/retention' for c in workers.runners['haos'].calls)


async def test_fresh_heartbeat_skips_needs_reconcile_until_stale_or_lost(workers):
    first = await submit(workers)
    await workers.advance()
    host = workers.runners['host']
    host.jobs[first['id']].update(state='launching', phase='launching', heartbeat_at=time.time())
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'launching'
    assert workers.get(first['id'])['phase'] == 'launching'
    host.offline = True
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'launching'
    workers.save(workers.get(first['id']), heartbeat_at=time.time() - 120)
    await workers.advance()
    assert workers.get(first['id'])['state'] == 'needs_reconcile'
    assert workers.get(first['id'])['reason'] == 'runner_response_unconfirmed'


async def test_lost_job_reconciles_even_with_a_fresh_heartbeat(workers):
    first = await submit(workers)
    await workers.advance()
    host = workers.runners['host']
    host.jobs[first['id']].update(state='running', phase='running', heartbeat_at=time.time())
    await workers.advance()
    assert workers.get(first['id'])['phase'] == 'running'
    del host.jobs[first['id']]
    await workers.advance()
    lost = workers.get(first['id'])
    assert lost['state'] == 'needs_reconcile' and lost['reason'] == 'worker_job_unconfirmed'


async def test_running_grant_expiry_is_authorization_expired(workers):
    first = await submit(workers)
    await workers.advance()
    item = workers.get(first['id'])
    # The wire job carries the grant itself, so both run out together.
    assert item['request']['expires'] == item['authority']['expires']
    item['request']['expires'] = item['authority']['expires'] = time.time() - 1
    workers.store.put('work_job', item)
    await workers.advance()
    result = workers.get(first['id'])
    assert result['state'] == 'cancelled' and result['reason'] == 'authorization_expired'


async def test_expired_grant_after_an_owner_change_is_a_revocation(workers):
    first = await submit(workers)
    await workers.advance()
    item = workers.get(first['id'])
    item['request']['expires'] = item['authority']['expires'] = time.time() - 1
    workers.store.put('work_job', item)
    workers.store.set_setting('owner', 'replacement-owner')
    await workers.advance()
    assert workers.get(first['id'])['reason'] == 'authority_revoked'


async def test_cancel_while_lookup_is_waiting_cannot_dispatch_after_local_stop(workers):
    first = await submit(workers)
    entered, release = asyncio.Event(), asyncio.Event()
    runner = workers.runners['host']
    original = runner.call
    async def delayed(method, path, body=None):
        if path == '/coding/jobs/' + first['id']:
            entered.set()
            await release.wait()
        return await original(method, path, body)
    runner.call = delayed
    advancing = asyncio.create_task(workers.advance())
    await entered.wait()
    result = await workers.cancel(first['id'])
    assert result['stop_confirmed']
    release.set()
    await advancing
    assert workers.get(first['id'])['state'] == 'cancelled' and not runner.jobs
    assert not workers.store.setting('worker_lease')


async def test_role_auth_lost_reply_retrieves_same_id_without_relogin(workers):
    runner, original = workers.runners['host'], workers.runners['host'].call
    async def lose(method, path, body=None):
        result = await original(method, path, body)
        if path == '/coding/providers/codex/auth':
            raise RunnerError('lost reply')
        return result
    runner.call = lose
    body = {'method':'api_key','secret':'private-lost-key'}
    with pytest.raises(RunnerError):
        await workers.role_auth('host-coder','POST','/coding/providers/codex/auth',body,key='durable-login')
    first = workers.store.setting('worker_auth_pending')['id']
    result = await workers.role_auth('host-coder','POST','/coding/providers/codex/auth',body,key='durable-login')
    assert result['id'] == first and result['status'] == 'connected'
    assert sum(c[1] == '/coding/providers/codex/auth' for c in runner.calls) == 1
    assert not workers.store.setting('worker_auth_pending')
    assert 'private-lost-key' not in workers.store.path.read_bytes().decode(errors='ignore')


async def test_coding_login_resumes_pending_and_accepts_longer_codes(workers):
    runner = workers.runners['host']
    body = {'method': 'device_code', 'secret': ''}
    first = await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth', body, key='tab-a')
    stored = runner.auth[first['id']]
    stored['code'] = 'ABCD2345-EFGH6789'
    stored['url'] = 'https://auth.openai.com/codex/device'
    posts = sum(c[1] == '/coding/providers/codex/auth' for c in runner.calls)
    second = await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth', body, key='tab-b')
    assert second['id'] == first['id'] and second['code'] == 'ABCD2345-EFGH6789'
    assert second['url'] == 'https://auth.openai.com/codex/device'
    assert sum(c[1] == '/coding/providers/codex/auth' for c in runner.calls) == posts
    identity = workers.peer_identity('host-coder')
    workers.store.set_setting('worker_auth_pending', {
        'worker_id': 'host-coder', 'id': first['id'], 'state': 'pending', 'runner_identity': identity})
    resumed = await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth', body, key='tab-c')
    assert resumed['id'] == first['id']
    assert sum(c[1] == '/coding/providers/codex/auth' for c in runner.calls) == posts
    runner.auth['wide'] = {'id': 'wide', 'status': 'pending', 'code': 'ABC-DEF', 'url': 'https://evil.example/device'}
    stripped = await workers.role_auth('host-coder', 'GET', '/coding/auth/wide')
    assert 'code' not in stripped and 'url' not in stripped
    runner.auth['nine'] = {'id': 'nine', 'status': 'pending', 'code': 'ABCDEFGHI-ABCDEFGH',
                           'url': 'https://auth.openai.com/device'}
    nine = await workers.role_auth('host-coder', 'GET', '/coding/auth/nine')
    assert 'code' not in nine and nine['url'] == 'https://auth.openai.com/device'
    workers.store.set_setting('worker_auth_pending', {
        'worker_id': 'home-coder', 'id': 'a' * 32, 'state': 'pending', 'kind': 'login', 'runner_identity': identity})
    with pytest.raises(WorkerError, match='coding_auth_pending'):
        await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth', body, key='other-worker')
    workers.store.set_setting('worker_auth_pending', {
        'worker_id': 'host-coder', 'id': 'b' * 32, 'state': 'pending', 'kind': 'logout', 'runner_identity': identity})
    with pytest.raises(WorkerError, match='coding_auth_pending'):
        await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth', body, key='during-logout')
    with pytest.raises(Conflict, match='Idempotency-Key'):
        await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth', body, key='')


async def test_commit_guard_rejects_revoked_mcp_grant_before_job_commit(workers):
    def revoked():
        raise WorkerError('revoked', 403)
    with pytest.raises(WorkerError, match='revoked'):
        await workers.submit(WorkerStart(worker_id='host-coder',mode='research',goal='fixture'),'key',request=owner(),commit_guard=revoked)
    assert not workers.store.list('work_job')


async def test_update_gate_blocks_owner_unpause_and_live_freshness(store, ha, tmp_path, monkeypatch):
    from agentic_controller.engine import Engine, Rejected
    monkeypatch.setenv('AGENTIC_DATA', str(tmp_path))
    (tmp_path / 'access-key').write_text('fixture-machine-access-key')
    store.set_setting('updater_gate', 'a' * 64)
    store.set_setting('paused', True)
    app = create_app(store=store, ha=ha, host_mode=True)
    callbacks = []
    from agentic_controller.ha_oauth import HomeAuthorization
    async def forbidden_callback(*args):
        callbacks.append(args)
        raise AssertionError('OAuth must not exchange credentials during update')
    monkeypatch.setattr(HomeAuthorization, 'finish', forbidden_callback)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8099', headers={'Origin':'http://localhost:8099'}) as client:
        assert (await client.post('/api/host/login', json={'key':'fixture-machine-access-key'})).status_code == 200
        client.headers['X-Agentic-CSRF'] = (await client.get('/api/session')).json()['csrf']
        assert (await client.post('/api/pause', json={'paused':False})).status_code == 423
        assert (await client.post('/api/worker/jobs', json={'worker_id':'host-coder','mode':'research','goal':'new write'})).status_code == 423
        assert store.setting('paused') is True
        assert (await client.get('/api/worker/jobs')).status_code == 200
        callback = await client.get('/api/host/authorized?state=once&code=private')
        assert callback.status_code == 303 and callback.headers['location'] == '/#onboarding'
        assert not callbacks
    with pytest.raises(Rejected, match='update_in_progress'):
        await Engine(store, ha).fresh({}, 'owner')


async def test_coding_role_never_inherits_bootstrap_provider(workers):
    workers.store.set_setting('gateway', {'provider':'grok'})
    first = await submit(workers)
    assert first['provider'] == 'codex' and first['state'] == 'queued'
    explicit = await submit(workers, 'explicit-unsupported', provider='grok')
    assert explicit['state'] == 'blocked' and explicit['reason'] == 'unsupported_provider'
    assert explicit['provider'] == 'grok'


async def test_old_auth_poll_cannot_clear_new_pending_flow(workers):
    runner = workers.runners['host']
    runner.auth['a'*32] = {'id':'a'*32,'status':'connected'}
    workers.store.set_setting('worker_auth_pending', {'worker_id':'host-coder','id':'b'*32,'state':'pending'})
    await workers.role_auth('host-coder', 'GET', '/coding/auth/'+'a'*32)
    assert workers.store.setting('worker_auth_pending')['id'] == 'b'*32


async def test_role_logout_lost_reply_keeps_stable_id(workers):
    runner, original = workers.runners['host'], workers.runners['host'].call
    async def lose(method, path, body=None):
        result = await original(method, path, body)
        if path.endswith('/logout'):
            raise RunnerError('lost after credential removal')
        return result
    runner.call = lose
    with pytest.raises(RunnerError):
        await workers.role_auth('host-coder','POST','/coding/providers/codex/logout',{})
    identifier = workers.store.setting('worker_auth_pending')['id']
    repeated = await workers.role_auth('host-coder','POST','/coding/providers/codex/logout',{})
    assert repeated['id'] == identifier and repeated['status'] == 'disconnected'
    assert sum(c[1].endswith('/logout') for c in runner.calls) == 1
    assert not workers.store.setting('worker_auth_pending')


async def test_case_context_contains_only_same_case_bounded_handles(workers):
    from agentic_controller.models import digest, utcnow
    case_id = 'a' * 32
    first = await submit(workers, case_id=case_id)
    foreign = await submit(workers, 'foreign')
    coordinator = workers.coordinator
    coordinator.worker_manager = workers
    coordinator.scope_valid = lambda item: True
    calls = []
    class Bridge:
        async def call(self, method, path, body=None, key=None):
            calls.append((method,path,body,key))
            if path == '/v1/sessions':
                return {'session_id':'d'*32}
            return {'state':'succeeded','answer':'fixture answer'}
    coordinator.client = Bridge()
    item = {'id':'e'*64,'thread_id':'e'*64,'scope':'f'*64,'message':'What is the result?', 'created_at':utcnow()}
    key = digest([item['scope'],item['thread_id']])
    workers.store.put('hermes_case', {'id':key,'case_id':case_id,'scope':item['scope'],'session_id':None,'created_at':utcnow()})
    result = await coordinator.run(item)
    assert result['state'] == 'succeeded'
    message = next(call[2]['message'] for call in calls if call[1].endswith('/runs'))
    assert first['id'] in message and foreign['id'] not in message
    assert 'Investigate fixture' not in message and 'authority' not in message
    assert len(workers.case_jobs(case_id)) == 1


async def test_pair_rotation_during_lookup_never_dispatches_to_new_runner(workers):
    first = await submit(workers)
    original = workers.runners['host'].call
    async def rotate(method, path, body=None):
        if path == '/coding/jobs/' + first['id']:
            workers.store.set_setting('runner', {'host': 'replacement'})
        return await original(method, path, body)
    workers.runners['host'].call = rotate
    await workers.advance()
    assert not workers.runners['host'].jobs
    assert workers.get(first['id'])['reason'] == 'runner_identity_changed'
    assert workers.store.setting('worker_lease')['job_id'] == first['id']


async def test_role_auth_rotation_keeps_pending_old_peer_receipt(workers):
    original = workers.runners['host'].call
    async def rotate(method, path, body=None):
        result = await original(method, path, body)
        if path.endswith('/logout'):
            workers.store.set_setting('runner', {'host': 'replacement'})
        return result
    workers.runners['host'].call = rotate
    with pytest.raises(WorkerError, match='runner_identity_changed'):
        await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/logout', {})
    assert workers.store.setting('worker_auth_pending')
    assert (await workers.capabilities())['auth_pending'].get('runner_identity') is None


async def test_role_auth_does_not_start_after_owner_logout_during_lookup(workers):
    authority = workers.authority(request=owner())
    original = workers.runners['host'].call
    async def revoke(method, path, body=None):
        if path.startswith('/coding/auth/'):
            workers.store.set_setting('hermes_web_epoch', 'logged-out')
        return await original(method, path, body)
    workers.runners['host'].call = revoke
    with pytest.raises(WorkerError, match='owner_authority_revoked'):
        await workers.role_auth('host-coder', 'POST', '/coding/providers/codex/auth',
            {'method': 'api_key', 'secret': 'private'}, key='revoked-login',
            commit_guard=lambda: workers.require_owner(authority))
    assert not workers.runners['host'].auth


async def test_handoff_never_targets_replacement_runner(workers):
    first = await submit(workers)
    await workers.advance()
    workers.store.set_setting('runner', {'host': 'replacement'})
    with pytest.raises(WorkerError, match='runner_identity_changed'):
        await workers.handoff(first['id'])
    assert not any(call[1].endswith('/handoff') for call in workers.runners['host'].calls)
    assert workers.store.setting('worker_lease')['job_id'] == first['id']


async def test_retention_ack_cannot_acknowledge_a_different_peer(workers):
    workers.store.set_settings({'runner': {'host':'original'}, 'retention': {'context_days':7, 'audit_days':30}})
    class Bridge:
        async def call(self, method, path, body):
            return body
    workers.coordinator.client = Bridge()
    original = workers.runners['host'].call
    async def rotate(method, path, body=None):
        result = await original(method, path, body)
        if path == '/retention':
            workers.store.set_setting('runner', {'host': 'replacement'})
        return result
    workers.runners['host'].call = rotate
    with pytest.raises(WorkerError, match='retention_sync_pending'):
        await workers.update_retention({'context_days':1, 'audit_days':30})
    assert workers.store.setting('retention')['context_days'] == 7
    assert workers.store.setting('retention_pending')['context_days'] == 1
