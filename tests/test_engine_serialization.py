"""Household writer regressions with synthetic HA and disk-backed Store fixtures."""
import asyncio
import time

import pytest
from agentic_controller.engine import Engine
from agentic_controller.ha import HAError
from agentic_controller.models import HELPERS, AutomationIntent, DeviceAction, digest
from agentic_controller.store import Conflict, Store
from conftest import SOURCE, TARGET, FakeHA


@pytest.fixture
def context(tmp_path):
    stores = []

    def opened(path):
        store = Store(path)
        stores.append(store)
        return store

    store = opened(tmp_path / 'engine.sqlite3')
    store.set_setting('policy', {'entities': [
        {'entity_id': entity, 'read': True, 'write': 'dedicated_helper', 'purpose': 'synthetic engine test'}
        for entity in HELPERS]})
    ha = FakeHA()
    yield Engine(store, ha), ha, opened, stores
    for current in stores:
        current.close()


async def action(engine, operation, *, approve=True, entity=TARGET, legacy_sequence=None):
    task = await engine.propose(engine.task('synthetic ' + operation, 'fixture'),
                                action=DeviceAction(entity_id=entity, action=operation))
    proposal = engine.store.get('proposal', task['proposal_id'])
    if legacy_sequence is not None:
        # Represent a saved PR135 proposal whose approval-bound digest already
        # included this field. Product code must preserve the exact record.
        proposal['sequence'] = legacy_sequence
        proposal['digest'] = digest({key: value for key, value in proposal.items() if key != 'digest'})
        engine.store.put('proposal', proposal)
    if approve:
        assert engine.approve(proposal, 'owner', proposal['digest'], proposal['id'])
    return proposal


async def test_rejected_newer_approval_does_not_consume_valid_older_action(context, monkeypatch):
    engine, ha, _, _ = context
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    older = await action(engine, 'turn_on', approve=False)
    clock[0] += 1
    newer = await action(engine, 'turn_off')
    clock[0] += 1
    assert engine.approve(older, 'owner', older['digest'], older['id'])
    # Both proposals were approved before proposal expiry. Only the newer
    # proposal's earlier approval expires while dispatch is delayed.
    clock[0] += 599.5
    await engine.deploy(newer, 'owner')
    assert engine.store.get('task', newer['task_id'])['state'] == 'failed'
    assert ha.calls == []
    await engine.deploy(older, 'owner')
    assert engine.store.get('task', older['task_id'])['state'] == 'succeeded'
    assert ha.states[TARGET] == 'on'


@pytest.mark.parametrize('legacy_sequence', [None, 64])
async def test_pending_proposal_survives_restart_without_poisoning_fresh_actions(context, legacy_sequence):
    engine, ha, opened, stores = context
    await action(engine, 'turn_off', approve=False)
    pending = await action(engine, 'turn_off', approve=False, legacy_sequence=legacy_sequence)
    original = dict(pending)
    path = engine.store.path
    engine.store.close()
    stores.remove(engine.store)
    restored = opened(path)
    restored.recover()
    restarted = Engine(restored, ha)
    pending = restored.get('proposal', pending['id'])
    assert pending == original
    assert restored.get('task', pending['task_id'])['state'] == 'awaiting_approval'
    assert restarted.approve(pending, 'owner', pending['digest'], pending['id'])
    await restarted.deploy(pending, 'owner')
    assert restored.get('task', pending['task_id'])['state'] == 'succeeded'
    fresh = await action(restarted, 'turn_on')
    await restarted.deploy(fresh, 'owner')
    assert restored.get('task', fresh['task_id'])['state'] == 'succeeded'
    assert ha.states[TARGET] == 'on'
    assert restored.get('proposal', pending['id']) == original


async def test_helper_writer_cannot_interleave_action_send_and_readback(context):
    engine, ha, _, _ = context
    intent = AutomationIntent.model_validate({'title': 'synthetic helper rule', 'trigger': {
        'entity_id': SOURCE, 'from_state': 'off', 'to_state': 'on'},
        'actions': [{'entity_id': TARGET, 'action': 'turn_on'}]})
    task = await engine.propose(engine.task('synthetic automation', 'fixture'), intent)
    rule = engine.store.get('proposal', task['proposal_id'])
    assert engine.approve(rule, 'owner', rule['digest'], rule['id'])
    await engine.deploy(rule, 'owner')
    single = await action(engine, 'turn_on')
    sent, release, overwritten = asyncio.Event(), asyncio.Event(), asyncio.Event()
    service = ha.service

    async def pause_response(domain, operation, entity):
        await service(domain, operation, entity)
        if entity == TARGET and operation == 'turn_on' and not sent.is_set():
            sent.set()
            await release.wait()
        elif entity == TARGET and operation == 'turn_off' and sent.is_set() and not release.is_set():
            overwritten.set()

    ha.service = pause_response
    deployment = asyncio.create_task(engine.deploy(single, 'owner'))
    helper = None
    try:
        await asyncio.wait_for(sent.wait(), 2)
        helper = asyncio.create_task(engine.helper_test(rule, 'owner', 'synthetic-helper'))
        try:
            await asyncio.wait_for(overwritten.wait(), .5)
        except TimeoutError:
            pass
        release.set()
        await asyncio.wait_for(asyncio.gather(deployment, helper), 2)
        assert engine.store.get('task', single['task_id'])['state'] == 'succeeded'
        assert not overwritten.is_set()
    finally:
        release.set()
        for task in [deployment, helper]:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[task for task in [deployment, helper] if task is not None],
                             return_exceptions=True)


@pytest.mark.parametrize('second_entity', [TARGET, SOURCE])
async def test_device_actions_share_writer_through_readback(context, second_entity):
    engine, ha, _, _ = context
    first = await action(engine, 'turn_on')
    second = await action(engine, 'turn_off', entity=second_entity)
    sent, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    service = ha.service
    async def pause_response(domain, operation, entity):
        await service(domain, operation, entity)
        if not sent.is_set():
            sent.set()
            await release.wait()
    async def deploy_second():
        queued.set()
        await engine.deploy(second, 'owner')
    ha.service = pause_response
    running = asyncio.create_task(engine.deploy(first, 'owner'))
    waiting = None
    try:
        await asyncio.wait_for(sent.wait(), 2)
        waiting = asyncio.create_task(deploy_second())
        await asyncio.wait_for(queued.wait(), 2)
        assert not waiting.done()
        assert ha.calls == [('input_boolean', 'turn_on', TARGET)]
        release.set()
        await asyncio.wait_for(asyncio.gather(running, waiting), 2)
        assert engine.store.get('task', first['task_id'])['state'] == 'succeeded'
        assert engine.store.get('task', second['task_id'])['state'] == 'succeeded'
        assert ha.calls == [('input_boolean', 'turn_on', TARGET), ('input_boolean', 'turn_off', second_entity)]
        assert ha.states[second_entity] == 'off'
    finally:
        release.set()
        for task in (running, waiting):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[task for task in (running, waiting) if task is not None], return_exceptions=True)


async def test_later_approval_can_execute_an_older_fresh_proposal(context):
    engine, ha, _, _ = context
    older = await action(engine, 'turn_on', approve=False)
    newer = await action(engine, 'turn_off')
    await engine.deploy(newer, 'owner')
    assert engine.approve(older, 'owner', older['digest'], older['id'])
    await engine.deploy(older, 'owner')
    assert engine.store.get('task', older['task_id'])['state'] == 'succeeded'
    assert engine.store.get('task', newer['task_id'])['state'] == 'succeeded'
    assert ha.calls == [('input_boolean', 'turn_off', TARGET), ('input_boolean', 'turn_on', TARGET)]


@pytest.mark.parametrize('lose_response', [False, True])
async def test_action_approval_replay_and_reconciliation_after_restart_never_resend(context, lose_response):
    engine, ha, opened, stores = context
    single = await action(engine, 'turn_on', legacy_sequence=64)
    original = dict(single)
    service = ha.service
    async def send(domain, operation, entity):
        await service(domain, operation, entity)
        if lose_response:
            raise HAError('synthetic acknowledgement lost')
    ha.service = send
    await engine.deploy(single, 'owner')
    expected = 'needs_reconcile' if lose_response else 'succeeded'
    assert engine.store.get('task', single['task_id'])['state'] == expected
    path = engine.store.path
    engine.store.close()
    stores.remove(engine.store)
    restored = opened(path)
    restored.recover()
    restarted = Engine(restored, ha)
    single = restored.get('proposal', single['id'])
    assert single == original
    assert not restarted.approve(single, 'owner', single['digest'], single['id'])
    with pytest.raises(Conflict):
        restarted.approve(single, 'owner', single['digest'], 'different-key')
    observed = await restarted.reconcile(single)
    assert observed['state'] == 'on'
    assert restored.get('task', single['task_id'])['state'] == expected
    assert ha.calls == [('input_boolean', 'turn_on', TARGET)]
