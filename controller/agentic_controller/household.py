"""Case-scoped household observations and proposals; never approval or deployment."""
import asyncio
import uuid
from typing import Annotated

from fastapi import HTTPException
from pydantic import Field, model_validator

from .engine import Rejected
from .ha import HAError
from .models import AutomationIntent, DeviceAction, EntityId, Strict, canonical, digest, utcnow
from .work import Identifier


class HomeObserve(Strict):
    entities: Annotated[list[EntityId], Field(max_length=20)] = []


class HomePropose(Strict):
    idempotency_key: Annotated[str, Field(pattern=r'^[a-zA-Z0-9_-]{8,100}$')]
    automation: AutomationIntent | None = None
    action: DeviceAction | None = None
    evidence_ids: Annotated[list[Identifier], Field(max_length=8)] = []

    @model_validator(mode='after')
    def one_proposal(self):
        if (self.automation is None) == (self.action is None):
            raise ValueError('exactly_one_household_proposal_required')
        return self


class HomeStatus(Strict):
    task_id: Identifier


class Household:
    def __init__(self, store, engine, coordinator):
        self.store, self.engine, self.coordinator = store, engine, coordinator
        self.lock = asyncio.Lock()

    async def guard(self, token, policy=None, task_id=None):
        grant = self.coordinator.authorize(token)
        owner = self.store.setting('owner')
        if not owner or not await self.engine.ha.owner(owner):
            raise Rejected('household_owner_required')
        self.coordinator.authorize(token)
        if owner != self.store.setting('owner') or policy is not None and digest(self.engine.policy().model_dump()) != policy:
            raise Rejected('household_authority_changed')
        if task_id and self.store.get('task', task_id)['state'] != 'planning':
            raise Rejected('household_task_not_planning')
        return grant

    async def observe(self, body, token):
        grant = await self.guard(token)
        policy = digest(self.engine.policy().model_dump())
        permitted = sorted(p.entity_id for p in self.engine.policy().entities if p.read)
        ids = set(body.entities) if body.entities else set(permitted[:20])
        values = await self.engine.permitted(ids, available=False)
        await self.guard(token, policy)
        content = [{'entity_id': item['entity_id'], 'state': item['state'], 'area': item['area'],
                    'availability': item['availability'], 'actions': item['actions'], 'observed_at': item['observed_at']}
                   for item in values.values()]
        identifier = uuid.uuid4().hex
        evidence = {'id': identifier, 'evidence_id': identifier, 'case_id': grant['case_id'], 'data_contract': 1,
            'observed_from': 'controller_to_ha_api', 'target_component': 'authorized_ha_entities',
            'collection_method': 'fresh_policy_filtered_inventory', 'observed_at': utcnow(), 'version_refs': {},
            'content_digest': digest(content), 'content': content, 'redaction': 'policy_and_field_allowlist',
            'limitations': ['HA API states do not establish physical device effects.']}
        self.store.put('case_evidence', evidence)
        return {'evidence': evidence, 'truncated': not body.entities and len(permitted) > 20}

    def status(self, task_id, case_id):
        task = self.store.get('task', task_id)
        if not task or task.get('case_id') != case_id:
            raise Rejected('household_task_not_in_case')
        value = {key: task.get(key) for key in ('id', 'case_id', 'state', 'proposal_id', 'message', 'verification')}
        if task.get('proposal_id'):
            proposal = self.store.get('proposal', task['proposal_id'])
            if proposal:
                value['proposal'] = {key: proposal.get(key) for key in ('id', 'digest', 'expires', 'risk', 'intent', 'action', 'verification_plan')}
                value['owner_review'] = '#home/' + task_id
        return value

    async def propose(self, body, token):
        async with self.lock:
            grant = await self.guard(token)
            policy = digest(self.engine.policy().model_dump())
            payload = body.model_dump(exclude={'idempotency_key'})
            scope = 'household_proposal:' + grant['case_id']
            previous = self.store.request(scope, body.idempotency_key, digest(payload))
            if previous:
                return self.status(previous, grant['case_id'])
            if sum(t.get('case_id') == grant['case_id'] for t in self.store.list('task', 10000)) >= 4:
                raise Rejected('household_case_budget_exhausted')
            self.store.prune()
            evidence = [self.store.get('case_evidence', identifier) for identifier in body.evidence_ids]
            if any(not item or item['case_id'] != grant['case_id'] for item in evidence):
                raise Rejected('household_evidence_not_in_case')
            source = self.store.conversation(id=grant['message_id'])
            task_id, now = uuid.uuid4().hex, utcnow()
            task = {'id': task_id, 'case_id': grant['case_id'], 'provider': self.coordinator.worker_manager.current_provider(),
                    'request': source['message'], 'state': 'planning', 'created_at': now, 'updated_at': now,
                    'message': '正在驗證家庭提案；尚未批准或執行', 'proposal_id': None, 'evidence_ids': body.evidence_ids}
            self.coordinator.authorize(token)
            with self.store.db:
                self.store.db.execute('INSERT INTO records VALUES (?,?,?,?,?)', ('task', task_id, canonical(task), now, now))
                self.store.db.execute('INSERT INTO requests VALUES (?,?,?,?)', (scope, body.idempotency_key, digest(payload), task_id))
            try:
                await self.engine.propose(task, body.automation, body.action,
                    commit_guard=lambda: self.guard(token, policy, task_id))
                source = self.store.get('conversation', grant['message_id'])
                self.store.put('conversation', {**source, 'task_id': task_id})
                return self.status(task_id, grant['case_id'])
            except (Rejected, HAError, HTTPException, asyncio.CancelledError):
                current = self.store.get('task', task_id)
                self.store.transition(task, 'cancelled' if current.get('state') == 'cancelled' else 'failed',
                                      message='家庭提案未完成驗證；沒有執行任何家庭變更')
                raise
