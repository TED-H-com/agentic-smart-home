"""Owner-only fixed host operations. Never registered as model tools."""
import asyncio
import json
import re
import time
import uuid
from typing import Annotated, Literal

import aiohttp
from fastapi import HTTPException, Request
from pydantic import Field, StrictFloat, StrictInt

from .durability import client_session, durable
from .models import Strict, WorkerPreview, canonical, digest, utcnow
from .work import WorkerAction
from .workers import WorkerError

HexDigest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class NetworkPreferences(Strict):
    lan: Literal['dhcp', 'reservation']
    remote: Literal['disabled', 'tailscale']


class UpdateCompatibility(Strict):
    data_schema: Literal[3] = Field(alias="schema")
    worker_contract: Literal[1]
    runner_protocol: Literal[1]
    runtime_digest: HexDigest
    ha_core_versions: Annotated[list[Annotated[str, Field(pattern=r'^[0-9]+\.[0-9]+\.[0-9]+$')]], Field(min_length=1, max_length=20)]


class UpdateTest(Strict):
    name: Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]{1,100}$')]
    result: Literal['passed']
    sha256: HexDigest


class UpdateManifest(Strict):
    format: Literal[1]
    publisher: Annotated[str, Field(pattern=r'^[a-z0-9-]{1,64}$')]
    version: Annotated[str, Field(pattern=r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?$')]
    source_commit: Annotated[str, Field(pattern=r'^[a-f0-9]{40}$')]
    image: Annotated[str, Field(pattern=r'^[a-z0-9.-]+/[a-z0-9_./-]+@sha256:[a-f0-9]{64}$', max_length=400)]
    image_id: Annotated[str, Field(pattern=r'^sha256:[a-f0-9]{64}$')]
    compatibility: UpdateCompatibility
    tests: Annotated[list[UpdateTest], Field(min_length=1, max_length=20)]
    issued_at: StrictInt | StrictFloat
    expires_at: StrictInt | StrictFloat


class UpdateStage(Strict):
    manifest: UpdateManifest
    signature: Annotated[str, Field(min_length=80, max_length=100, pattern=r'^[A-Za-z0-9+/]+=*$')]


class UpdateOffer(Strict):
    candidate_digest: HexDigest


class UpdateApply(UpdateOffer):
    approval_id: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')]


async def broker_call(method, path, body=None, *, store):
    """Only trusted route code supplies a method/path; no configurable network target."""
    allowed = (method == 'GET' and (path in {'/network', '/updates'} or
        re.fullmatch(r'/updates/[a-f0-9]{64}|/previews/[a-f0-9]{32}', path))) or (
        method == 'POST' and (re.fullmatch(r'/previews/[a-f0-9]{32}/revoke', path) or path in {'/network/preferences', '/network/login', '/network/revoke', '/previews',
                                     '/updates/stage', '/updates/apply', '/updates/rollback-offer', '/updates/rollback'}))
    if not allowed:
        raise HTTPException(422, 'invalid_host_operation')
    try:
        connector = aiohttp.UnixConnector(path='/run/agentic-home/host.sock')
        async with client_session(store, connector=connector, timeout=aiohttp.ClientTimeout(total=35), trust_env=False) as client:
            async with client.request(method, 'http://host' + path, json=body, allow_redirects=False) as response:
                raw = await response.content.read(262145)
                if len(raw) > 262144:
                    raise ValueError()
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                if response.status >= 300:
                    detail = value.get('detail')
                    safe = detail if isinstance(detail, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,100}', detail) else 'host_operation_failed'
                    raise HTTPException(response.status, safe)
                return value
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError):
        raise HTTPException(503, 'host_operation_unavailable') from None


class Operations:
    def __init__(self, store, workers, *, host_mode=False):
        self.store, self.workers, self.preview_lock = store, workers, asyncio.Lock()
        self.host_mode = host_mode

    def owner_scope(self, request):
        return {**self.workers.authority(request=request), 'policy': None}

    def guard(self, authority):
        if (authority['expires'] <= time.time() or authority['owner'] != self.store.setting('owner') or
            authority['web_epoch'] != self.store.setting('hermes_web_epoch') or
            (authority.get('bridge_pairing_id') and not getattr(self.workers, 'bridge_valid', lambda _: False)(authority))):
            raise HTTPException(403, 'owner_authority_revoked')

    async def preview(self, identifier, body, request):
        async with self.preview_lock:
            authority = self.owner_scope(request)
            item = self.workers.get(identifier)
            if item.get('content_expired'):
                raise WorkerError('worker_job_expired', 410)
            mode = getattr(body, 'mode', 'screenshot')
            artifact_ids = list(getattr(body, 'artifact_ids', []))
            if len(set(artifact_ids)) != len(artifact_ids):
                raise WorkerError('duplicate_artifact', 422)
            # Broker previews live for at most thirty minutes. Do not create a
            # new copy whose lifetime would outlive its source context.
            if item['created_epoch'] + self.workers.context_days() * 86400 < time.time() + 1860:
                raise WorkerError('preview_context_expiring', 409)
            key = request.headers.get('idempotency-key', '')
            fingerprint = digest([identifier, mode, sorted(artifact_ids)])
            previous = self.store.request('worker_preview', key, fingerprint)
            if previous:
                record = self.store.get('worker_preview', previous)
            else:
                record = {'id': uuid.uuid4().hex, 'job_id': identifier, 'case_id': item['case_id'],
                          'mode': mode, 'artifact_ids': sorted(artifact_ids), 'created_at': utcnow()}
                now = utcnow()
                with self.store.transaction() as tx:
                    tx.execute('INSERT INTO records VALUES (?,?,?,?,?)', ('worker_preview', record['id'], canonical(record), now, now))
                    tx.execute('INSERT INTO requests VALUES (?,?,?,?)', ('worker_preview', key, fingerprint, record['id']))
            if not record:
                raise WorkerError('preview_expired', 410)
            stored_mode = record.get('mode', 'screenshot')
            try:
                existing = await broker_call('GET', '/previews/' + record['id'], store=self.store)
                self.guard(authority)
                return existing
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            if stored_mode == 'interactive':
                # Location first: a bridge or Ingress client must not learn whether a bundle exists.
                bridged = bool(getattr(getattr(request, 'state', None), 'bridge_identity', None))
                if bridged or not self.host_mode:
                    raise WorkerError('interactive_preview_host_only', 409)
                web_preview = (item.get('verification') or {}).get('web_preview') or {}
                if web_preview.get('state') != 'built':
                    raise WorkerError('web_preview_unavailable', 409)
                self.guard(authority)
                if self.workers.get(identifier).get('content_expired'):
                    raise WorkerError('worker_job_expired', 410)
                await durable(self.store)
                return await broker_call('POST', '/previews',
                                         {'id': record['id'], 'mode': 'interactive', 'job_id': identifier}, store=self.store)
            files = []
            for artifact_id in record['artifact_ids']:
                value = await self.workers.artifact(identifier, artifact_id)
                files.append({'path': value['name'], 'sha256': value['sha256'], 'content_base64': value['content_base64']})
            if len({value['path'] for value in files}) != len(files) or 'index.html' not in {value['path'] for value in files}:
                raise WorkerError('preview_entry_or_paths_invalid', 422)
            aggregate = digest([{'path': v['path'], 'sha256': v['sha256']} for v in sorted(files, key=lambda v: v['path'])])
            self.guard(authority)
            if self.workers.get(identifier).get('content_expired'):
                raise WorkerError('worker_job_expired', 410)
            await durable(self.store)
            return await broker_call('POST', '/previews', {'id': record['id'], 'artifact_id': record['id'],
                                                        'artifact_digest': aggregate, 'files': files}, store=self.store)

    async def preview_status(self, identifier):
        record = self.store.get('worker_preview', identifier)
        if not record:
            raise WorkerError('preview_not_found', 404)
        if self.workers.get(record['job_id']).get('content_expired'):
            raise WorkerError('preview_expired', 410)
        return await broker_call('GET', '/previews/' + identifier, store=self.store)


def routes(app, store, workers, *, host_mode=False):
    operations = Operations(store, workers, host_mode=host_mode)

    @app.get('/api/network')
    async def network():
        return await broker_call('GET', '/network', store=store)

    @app.post('/api/network/preferences')
    async def preferences(body: NetworkPreferences):
        await durable(store)
        return await broker_call('POST', '/network/preferences', body.model_dump(), store=store)

    @app.post('/api/network/login')
    async def login(body: WorkerAction):
        await durable(store)
        return await broker_call('POST', '/network/login', {}, store=store)

    @app.post('/api/network/revoke')
    async def revoke(body: WorkerAction):
        await durable(store)
        return await broker_call('POST', '/network/revoke', {}, store=store)

    @app.get('/api/updates')
    async def updates():
        return await broker_call('GET', '/updates', store=store)

    @app.post('/api/updates/stage', status_code=202)
    async def stage(body: UpdateStage):
        await durable(store)
        return await broker_call('POST', '/updates/stage', body.model_dump(by_alias=True), store=store)

    @app.post('/api/updates/apply', status_code=202)
    async def apply(body: UpdateApply):
        await durable(store)
        return await broker_call('POST', '/updates/apply', body.model_dump(), store=store)

    @app.post('/api/updates/rollback-offer')
    async def rollback_offer(body: UpdateOffer):
        await durable(store)
        return await broker_call('POST', '/updates/rollback-offer', body.model_dump(), store=store)

    @app.post('/api/updates/rollback', status_code=202)
    async def rollback(body: UpdateApply):
        await durable(store)
        return await broker_call('POST', '/updates/rollback', body.model_dump(), store=store)

    @app.get('/api/updates/{candidate_digest}')
    async def update_status(candidate_digest: str):
        if not re.fullmatch(r'[a-f0-9]{64}', candidate_digest):
            raise HTTPException(422, 'invalid_candidate_digest')
        return await broker_call('GET', '/updates/' + candidate_digest, store=store)

    @app.post('/api/worker/jobs/{identifier}/previews', status_code=202)
    async def preview(identifier: str, body: WorkerPreview, request: Request):
        return await operations.preview(identifier, body, request)

    @app.post('/api/worker/previews/{identifier}/revoke')
    async def revoke_preview(identifier: str, body: WorkerAction):
        if not store.get('worker_preview', identifier):
            raise WorkerError('preview_not_found', 404)
        await durable(store)
        return await broker_call('POST', '/previews/' + identifier + '/revoke', {}, store=store)

    @app.get('/api/worker/previews/{identifier}')
    async def preview_status(identifier: str):
        return await operations.preview_status(identifier)

    return operations
