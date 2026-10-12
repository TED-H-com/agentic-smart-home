"""Real MCP/Engine seam with deterministic HA fixtures; no household side effects."""
import asyncio
import json
import uuid

import httpx
import pytest
from agentic_controller.engine import Engine
from agentic_controller.hermes import Coordinator
from agentic_controller.household import Household
from agentic_controller.mcp import TOOLS, create_mcp
from agentic_controller.models import utcnow
from conftest import SOURCE, TARGET


@pytest.fixture
async def household(store, ha):
    store.set_setting('owner', 'owner')
    engine, coordinator = Engine(store, ha), Coordinator(store)
    coordinator.scope_valid = lambda item: not store.setting('paused', False) and store.setting('owner') == 'owner'
    coordinator.household = Household(store, engine, coordinator)
    item = {'id': uuid.uuid4().hex, 'scope': 'a'*64, 'message': 'Turn on the dedicated test helper', 'created_at': utcnow()}
    store.put('conversation', item)
    case = uuid.uuid4().hex
    token = coordinator.grant(item, case, uuid.uuid4().hex)
    transport = httpx.ASGITransport(app=create_mcp(coordinator), client=('127.0.0.1', 40000))
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8097', headers={'Authorization':'Bearer '+token}) as client:
        async def rpc(name, arguments=None):
            return await client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                                                 'params':{'name':name,'arguments':arguments or {}}})
        yield rpc, engine, coordinator, token, case, item


def output(response):
    result = response.json()['result']
    assert not result['isError'], result
    return json.loads(result['content'][0]['text'])


async def test_mcp_household_proposal_requires_owner_exact_approval(household, ha, store):
    rpc, engine, _, _, _, item = household
    observed = output(await rpc('home.observe', {'entities':[TARGET]}))
    evidence = observed['evidence']
    assert evidence['content'][0]['entity_id'] == TARGET
    assert evidence['observed_from'] == 'controller_to_ha_api' and evidence['limitations']
    args = {'idempotency_key':'precise-action','action':{'entity_id':TARGET,'action':'turn_on'},'evidence_ids':[evidence['id']]}
    proposed = output(await rpc('home.propose', args))
    assert proposed['state'] == 'awaiting_approval' and not ha.calls and not ha.writes
    assert output(await rpc('home.propose', args)) == proposed
    assert len(store.list('proposal')) == 1 and not store.list('approval')
    assert store.get('conversation', item['id'])['task_id'] == proposed['id']
    assert not {'home.approve', 'home.apply', 'home.execute'} & TOOLS.keys()
    approval = store.get('proposal', proposed['proposal_id'])
    assert engine.approve(approval, 'owner', approval['digest'], 'actual-owner-click')
    await engine.deploy(approval, 'owner')
    assert ha.calls == [('input_boolean', 'turn_on', TARGET)]
    status = output(await rpc('home.proposal_status', {'task_id':proposed['id']}))
    assert status['state'] == 'succeeded'


async def test_household_cannot_expand_policy_or_forge_case_evidence(household, ha, store):
    rpc, _, _, _, _, _ = household
    denied = await rpc('home.observe', {'entities':['light.not_authorized']})
    assert denied.json()['result']['isError']
    identifier = uuid.uuid4().hex
    store.put('case_evidence', {'id':identifier,'case_id':'b'*32})
    denied = await rpc('home.propose', {'idempotency_key':'cross-case','action':{'entity_id':TARGET,'action':'turn_on'},'evidence_ids':[identifier]})
    assert denied.json()['result']['isError'] and not store.list('proposal')
    assert not ha.calls and not ha.writes


@pytest.mark.parametrize('mutation', ['owner', 'policy', 'grant', 'paused'])
async def test_final_guard_rejects_revocation_during_ha_validation(household, ha, store, intent, mutation):
    rpc, _, coordinator, token, _, _ = household
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(config):
        entered.set()
        await release.wait()
    ha.validate = delayed
    request = asyncio.create_task(rpc('home.propose', {'idempotency_key':'revoked-proposal','automation':intent.model_dump()}))
    await entered.wait()
    if mutation == 'owner':
        ha.revoked = True
    elif mutation == 'policy':
        store.set_setting('policy', {'entities':[]})
    elif mutation == 'grant':
        coordinator.revoke(coordinator.authorize(token)['run_id'])
    else:
        store.set_setting('paused', True)
    release.set()
    response = await request
    assert response.status_code == 403 or response.json()['result']['isError']
    assert not store.list('proposal') and not store.list('approval') and not ha.writes
    assert store.list('task')[0]['state'] == 'failed'


async def test_automation_path_preserves_existing_compiler_and_no_live_write(household, intent, store, ha):
    rpc, _, _, _, _, _ = household
    proposal = output(await rpc('home.propose', {'idempotency_key':'automation-proposal','automation':intent.model_dump()}))
    actual = store.get('proposal', proposal['proposal_id'])
    assert actual['intent']['trigger']['entity_id'] == SOURCE
    assert actual['staged']['initial_state'] is False
    assert not ha.calls and not ha.writes
    bad = await rpc('home.propose', {'idempotency_key':'bad-shell','automation':intent.model_dump(),'shell':'rm -rf /'})
    assert bad.json()['result']['isError']
