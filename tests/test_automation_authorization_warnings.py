"""Owned rules keep their lifecycle and expose only local authorization facts."""
import httpx
import pytest
from agentic_controller.app import create_app
from agentic_controller.engine import Engine
from agentic_controller.ha import HAError, HomeAssistant
from agentic_controller.models import TimeWindow
from agentic_controller.store import Store
from conftest import SOURCE, TARGET, proposal


def client_for(store, ha):
    app = create_app(store=store, ha=ha, host_mode=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(
        app=app, client=('172.30.32.2', 1234), raise_app_exceptions=False),
        base_url='http://home.test', headers={'X-Remote-User-Id': 'owner'})


async def deployed(engine, intent):
    p = await proposal(engine, intent)
    engine.approve(p, 'owner', p['digest'], 'initial-deploy')
    await engine.deploy(p, 'owner')
    assert engine.store.get('owned', p['resource_id'])['status'] == 'deployed'
    return p


async def test_same_intent_retry_after_store_restart_is_verified_noop(engine, intent, ha):
    p = await deployed(engine, intent)
    writes, calls = list(ha.writes), list(ha.calls)
    path = engine.store.path
    engine.store.close()
    reopened = Store(path)
    try:
        reopened.recover()
        restarted = Engine(reopened, ha)
        task = await restarted.propose(restarted.task('same request after restart', 'fixture'), intent)
        assert task['state'] == 'succeeded'
        assert task['resource_id'] == p['resource_id']
        assert task['verification'] == 'existing_config_readback'
        assert ha.writes == writes and ha.calls == calls
    finally:
        reopened.close()


@pytest.mark.parametrize('existing', ['disabled', 'externally_deleted'])
async def test_same_title_preserves_existing_automation_lifecycle(engine, intent, ha, existing):
    p = await deployed(engine, intent)
    if existing == 'disabled':
        task = await engine.propose(engine.task('disable', 'fixture'), mode='disable', resource_id=p['resource_id'])
        disable = engine.store.get('proposal', task['proposal_id'])
        engine.approve(disable, 'owner', disable['digest'], 'disable-rule')
        await engine.deploy(disable, 'owner')
        assert engine.store.get('owned', p['resource_id'])['enabled'] is False
    else:
        del ha.configs[p['resource_id']]
        del ha.states['automation.' + p['resource_id']]
    writes, calls = list(ha.writes), list(ha.calls)
    result = await engine.propose(engine.task('same desired rule', 'fixture'), intent)
    if existing == 'disabled':
        assert result['state'] == 'succeeded'
        assert result['resource_id'] == p['resource_id']
        assert engine.store.get('owned', p['resource_id'])['enabled'] is False
    else:
        assert result['state'] == 'awaiting_approval'
        replacement = engine.store.get('proposal', result['proposal_id'])
        assert replacement['resource_id'] != p['resource_id']
        assert replacement['prior'] is None
    assert ha.writes == writes and ha.calls == calls


@pytest.mark.parametrize('status', [403, 503])
async def test_owned_rules_remain_readable_when_inventory_fails(engine, intent, ha, monkeypatch, status):
    p = await deployed(engine, intent)
    async with client_for(engine.store, ha) as client:
        initial = await client.get('/api/automations')
        assert initial.status_code == 200

        async def unavailable():
            raise HAError('inventory observation unavailable', status)
        monkeypatch.setattr(ha, 'inventory', unavailable)
        result = await client.get('/api/automations')
        assert result.status_code == 200, result.text
        assert result.json()['automations'][0]['id'] == p['resource_id']


async def test_missing_live_state_does_not_claim_registered_authorized_target_missing(engine, intent, ha, monkeypatch):
    await deployed(engine, intent)
    original_request = ha.request

    async def request(method, path, *args, **kwargs):
        if path == '/states':
            return [{'entity_id': SOURCE, 'state': 'off', 'attributes': {}}]
        return await original_request(method, path, *args, **kwargs)

    async def ws(kind):
        return {'config/entity_registry/list': [{'entity_id': SOURCE}, {'entity_id': TARGET}],
                'config/device_registry/list': [], 'config/area_registry/list': [],
                'get_services': {'input_boolean': {'turn_on': {}, 'turn_off': {}}}}[kind]

    monkeypatch.setattr(ha, 'request', request)
    monkeypatch.setattr(ha, 'ws', ws, raising=False)
    monkeypatch.setattr(ha, 'inventory', lambda: HomeAssistant.inventory(ha))
    async with client_for(engine.store, ha) as client:
        result = await client.get('/api/automations')
        assert result.status_code == 200
        warnings = result.json()['automations'][0].get('warnings', [])
        assert not any(TARGET in w and ('不存在' in w or '未授權' in w) for w in warnings), warnings


@pytest.mark.parametrize('action', ['disable', 'enable', 'rollback'])
async def test_legacy_equal_time_window_remains_manageable_after_reopen(engine, intent, ha, action):
    # This is the exact TimeWindow mapping accepted by the merge-base model.
    old = intent.model_copy(update={'time_window': TimeWindow.model_construct(
        after='10:00:00', before='10:00:00')})
    p = await deployed(engine, old)
    engine.store.close()
    reopened = Store(engine.store.path)
    try:
        reopened.recover()
        async with client_for(reopened, ha) as client:
            auth = await client.get('/api/session')
            assert auth.status_code == 200
            result = await client.post('/api/automations/' + p['resource_id'] + '/propose',
                json={'action': action}, headers={'X-Agentic-CSRF': auth.json()['csrf'],
                    'Origin': 'http://home.test', 'Idempotency-Key': 'legacy-control'})
            assert result.status_code == 200, result.text
            task = result.json()
            assert task['state'] == 'awaiting_approval'
            control = reopened.get('proposal', task['proposal_id'])
            assert control['mode'] == action
            assert control['intent']['time_window'] == {'after': '10:00:00', 'before': '10:00:00'}
    finally:
        reopened.close()


@pytest.mark.parametrize('revocation', ['removed', 'read_disabled'])
async def test_warnings_cover_every_reference_and_clear_without_changing_owned_state(store, ha, monkeypatch, revocation):
    from agentic_controller.models import AutomationIntent

    targets = ['light.fixture_first', 'light.fixture_second', 'switch.fixture_third']
    saved_intent = AutomationIntent.model_validate({'title': '合成授權測試', 'trigger': {
        'entity_id': SOURCE, 'from_state': 'off', 'to_state': 'on'}, 'time_window': {
            'after': '22:00:00', 'before': '06:00:00'}, 'actions': [
                {'entity_id': target, 'action': 'turn_on'} for target in targets]})
    store.put('owned', {'id': 'agentic_fixture', 'intent': saved_intent.model_dump(),
                        'status': 'deployed', 'enabled': True, 'last_hash': 'unchanged-fixture-hash'})
    before = store.list('owned')
    authorized = {'entities': [{'entity_id': entity, 'read': True, 'write': 'none'}
                              for entity in [SOURCE, *targets]]}
    store.set_setting('policy', authorized)
    writes, calls = list(ha.writes), list(ha.calls)

    async def no_inventory():
        pytest.fail('Owned authorization warnings must not query HA inventory')
    monkeypatch.setattr(ha, 'inventory', no_inventory)
    ha.connected = False
    async with client_for(store, ha) as client:
        initial = await client.get('/api/automations')
        assert initial.status_code == 200
        assert 'warnings' not in initial.json()['automations'][0]
        store.set_setting('policy', {'entities': [
            {**entry, 'read': False}
            for entry in authorized['entities']
            if revocation == 'read_disabled']})
        warned = await client.get('/api/automations')
        assert warned.status_code == 200
        row = warned.json()['automations'][0]
        assert row.pop('warnings') == [f'觸發設備 {SOURCE} 目前未授權讀取',
            *[f'動作設備 {target} 目前未授權讀取' for target in targets]]
        assert row == before[0]
        assert store.list('owned') == before
        store.set_setting('policy', authorized)
        cleared = await client.get('/api/automations')
        assert cleared.status_code == 200
        assert cleared.json()['automations'] == before
    assert store.list('owned') == before
    assert ha.writes == writes and ha.calls == calls


async def test_owned_rule_warnings_do_not_bypass_owner_authentication(store, ha):
    async with client_for(store, ha) as client:
        response = await client.get('/api/automations', headers={'X-Remote-User-Id': 'normal-user'})
        assert response.status_code == 403
