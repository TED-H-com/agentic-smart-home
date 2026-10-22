"""Owner-only fixed host operations. Never registered as model tools."""
import asyncio
import json
import re
import time
import uuid
from typing import Annotated, Literal

import aiohttp
from fastapi import HTTPException, Request
from pydantic import Field, StrictFloat, StrictInt, ValidationError, field_validator, model_validator

from .durability import client_session, durable
from .ha import HAError
from .ha_lan import (
    HomeLanStatus,
    HostLanStatus,
    ObservationInvalid,
    compensate_home_lan,
    converge_home_lan,
    observe_home_lan,
)
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


class TrustedPublisherStatus(Strict):
    contract: Literal[1]
    state: Literal['unavailable', 'approval_required', 'trusted', 'conflict']
    message: Annotated[str, Field(min_length=1, max_length=256)]
    code: Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,95}$')]
    publisher: Literal['ted-h-com-agentic-smart-home'] | None
    repository: Literal['TED-H-com/agentic-smart-home'] | None
    channel: Literal['stable'] | None
    image_repository: Literal['ghcr.io/ted-h-com/agentic-smart-home-controller'] | None
    key_sha256: HexDigest | None
    review_digest: HexDigest | None
    scope: Literal['controller_only_same_schema']
    authority: Literal['signed_controller_images_only']

    @field_validator('contract', mode='before')
    @classmethod
    def exact_contract(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('publisher_contract_invalid')
        return value

    @model_validator(mode='after')
    def pin_required_for_trust(self):
        if self.state in {'approval_required', 'trusted'} and any(value is None for value in (
                self.publisher, self.repository, self.channel, self.image_repository, self.key_sha256, self.review_digest)):
            raise ValueError('publisher_pin_required')
        return self


class TrustedPublisherOffer(TrustedPublisherStatus):
    approval: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')]
    expires_at: StrictInt | StrictFloat


class TrustedPublisherApproval(Strict):
    review_digest: HexDigest
    approval: Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')]


def publisher_status(raw):
    try:
        return TrustedPublisherStatus.model_validate(raw).model_dump(mode='json')
    except ValidationError:
        raise HTTPException(503, '無法核對官方更新來源，請使用含官方更新來源憑證的受信任主機版本。') from None


def unsupported_publisher_status():
    return TrustedPublisherStatus(contract=1, state='unavailable',
        code='official_publisher_host_unsupported', message='這台主機版本尚不支援設定官方更新來源，需取得含官方更新來源憑證的受信任主機版本。',
        publisher=None, repository=None, channel=None, image_repository=None, key_sha256=None, review_digest=None,
        scope='controller_only_same_schema', authority='signed_controller_images_only').model_dump(mode='json')


async def broker_call(method, path, body=None, *, store):
    """Only trusted route code supplies a method/path; no configurable network target."""
    allowed = (method == 'GET' and (path in {'/network', '/network/ha-lan', '/updates', '/updates/publisher'} or
        re.fullmatch(r'/updates/[a-f0-9]{64}|/previews/[a-f0-9]{32}', path))) or (
        method == 'POST' and (re.fullmatch(r'/previews/[a-f0-9]{32}/revoke', path) or path in {'/network/ha-lan/observation', '/network/preferences', '/network/login', '/network/revoke', '/previews',
                                     '/updates/publisher/offer', '/updates/publisher/approve', '/updates/stage', '/updates/apply', '/updates/rollback-offer', '/updates/rollback'}))
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


async def maintain_home_lan(ha, store):
    """Continue the fixed Host attempt without requiring an open browser.

    Only Host creates attempts (under its action/restore-point policy). This
    loop cannot select a NIC, start a guest, or invent a network target.
    """
    async def status():
        return HostLanStatus.model_validate(await broker_call('GET', '/network/ha-lan', store=store)).model_dump(mode='json')

    while True:
        try:
            initial = await status()
            async def check_compensation(initial=initial):
                current = await status()
                if any(current.get(key) != initial.get(key) for key in ('attempt_id', 'mode', 'state')):
                    raise ObservationInvalid('home_lan_attempt_changed')

            await compensate_home_lan(ha, store, initial, check_compensation)
            if initial['mode'] == 'wired' and initial['state'] == 'configured' and initial['attempt_id']:
                async def check_attempt(initial=initial):
                    current = await status()
                    if any(current.get(key) != initial.get(key) for key in (
                            'attempt_id', 'guest_mac', 'selected_interface_id', 'mode', 'state', 'core_configuration_pending')):
                        raise ObservationInvalid('home_lan_attempt_changed')

                observed = await converge_home_lan(ha, store, initial, check_attempt)
                if observed['state'] == 'ready':
                    await check_attempt()
                    await durable(store)
                    await broker_call('POST', '/network/ha-lan/observation', {
                        'attempt_id': initial['attempt_id'], 'guest_mac': initial['guest_mac'], 'home': observed}, store=store)
        except (HTTPException, ValidationError, ObservationInvalid, HAError, OSError, TimeoutError):
            # Broker/HA restarts remain retryable. Host owns the bounded deadline
            # and NAT fallback; a failed observation never becomes a success.
            pass
        await asyncio.sleep(5)


class Operations:
    def __init__(self, store, workers, *, host_mode=False, host_auth=None):
        self.store, self.workers, self.preview_lock = store, workers, asyncio.Lock()
        self.host_mode, self.host_auth = host_mode, host_auth

    def owner_scope(self, request):
        return {**self.workers.authority(request=request), 'policy': None}

    def guard(self, authority):
        if (authority['expires'] <= time.time() or authority['owner'] != self.store.setting('owner') or
            authority['web_epoch'] != self.store.setting('hermes_web_epoch') or
            (authority.get('bridge_pairing_id') and not getattr(self.workers, 'bridge_valid', lambda _: False)(authority))):
            raise HTTPException(403, 'owner_authority_revoked')

    def publisher_owner(self, request):
        if not self.host_mode or getattr(request.state, 'bridge_identity', None):
            raise HTTPException(403, '請從主機管理頁確認官方更新來源。')
        authority = self.owner_scope(request)
        self.publisher_guard(request, authority)
        return authority

    def publisher_guard(self, request, authority):
        self.guard(authority)
        if self.host_auth is None or not self.host_auth.verify(request):
            raise HTTPException(403, '主機登入已變更，請重新登入並查看更新來源。')

    async def publisher_offer(self, request):
        authority = self.publisher_owner(request)
        await durable(self.store)
        self.publisher_guard(request, authority)
        raw = await broker_call('POST', '/updates/publisher/offer', {}, store=self.store)
        self.publisher_guard(request, authority)
        try:
            offer = TrustedPublisherOffer.model_validate(raw)
        except ValidationError:
            raise HTTPException(503, '無法核對官方更新來源，請稍後重新讀取。') from None
        now = time.time()
        if offer.state != 'approval_required' or not now < offer.expires_at <= now + 305:
            raise HTTPException(409, '更新來源確認已過期，請重新查看。')
        self.store.set_setting('trusted_publisher_offer', {
            'approval_digest': digest(offer.approval), 'review_digest': offer.review_digest,
            'expires_at': offer.expires_at, 'authority': authority,
            'session_digest': digest(request.cookies.get('agentic_host', ''))})
        await durable(self.store)
        self.publisher_guard(request, authority)
        return offer.model_dump(mode='json')

    async def publisher_approve(self, body, request):
        authority = self.publisher_owner(request)
        record = self.store.setting('trusted_publisher_offer')
        if (not record or record['approval_digest'] != digest(body.approval)
                or record['review_digest'] != body.review_digest or record['expires_at'] <= time.time()
                or record['session_digest'] != digest(request.cookies.get('agentic_host', ''))):
            raise HTTPException(409, '更新來源確認已過期，請重新查看。')
        self.publisher_guard(request, record['authority'])
        await durable(self.store)
        self.publisher_guard(request, authority)
        self.publisher_guard(request, record['authority'])
        result = publisher_status(await broker_call('POST', '/updates/publisher/approve', body.model_dump(), store=self.store))
        self.store.set_setting('trusted_publisher_offer', None)
        await durable(self.store)
        return result

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


def routes(app, store, workers, *, host_mode=False, host_auth=None):
    operations = Operations(store, workers, host_mode=host_mode, host_auth=host_auth)

    @app.get('/api/network')
    async def network():
        return await broker_call('GET', '/network', store=store)

    @app.get('/api/network/ha-lan', response_model=HomeLanStatus)
    async def home_lan():
        raw = await broker_call('GET', '/network/ha-lan', store=store)
        try:
            value = HostLanStatus.model_validate(raw).model_dump(mode='json')
        except ValidationError:
            raise HTTPException(503, 'ha_lan_host_contract_unavailable') from None
        home = await observe_home_lan(app.state.ha, store, value)
        if home['state'] == 'ready':
            value['limitations'] = [item for item in value['limitations'] if item != 'guest_dhcp_and_discovery_not_yet_verified']
        return {**value, 'home': home}

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

    @app.get('/api/updates/publisher', response_model=TrustedPublisherStatus)
    async def trusted_publisher():
        try:
            return publisher_status(await broker_call('GET', '/updates/publisher', store=store))
        except HTTPException as error:
            if error.status_code in {404, 405}:
                return unsupported_publisher_status()
            raise

    @app.post('/api/updates/publisher/offer', response_model=TrustedPublisherOffer)
    async def publisher_offer(body: WorkerAction, request: Request):
        return await operations.publisher_offer(request)

    @app.post('/api/updates/publisher/approve', response_model=TrustedPublisherStatus)
    async def publisher_approve(body: TrustedPublisherApproval, request: Request):
        return await operations.publisher_approve(body, request)

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
