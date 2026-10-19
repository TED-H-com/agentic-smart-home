"""Owner-present, fixed Host bundle update routes; never exposed as model tools."""
from __future__ import annotations

import json
import math
import re
import time
from typing import Annotated, Literal

import aiohttp
from fastapi import HTTPException, Request
from pydantic import (
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from .durability import client_session, durable
from .models import Strict, digest
from .work import WorkerAction

HOST_UPDATE_PROTOCOLS = {'host_protocol': 1, 'controller_protocol': 1, 'controller_schema': 3, 'reconcile': 1}


def health_descriptor():
    """Loaded Controller code capability, never an installed-Host authorization."""
    return dict(HOST_UPDATE_PROTOCOLS)


HexDigest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Version = Annotated[str, Field(pattern=r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?$', max_length=100)]
ApprovalId = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{32,100}$')]
Reason = Annotated[str, Field(pattern=r'^[a-z][a-z0-9_]{0,95}$')]


class HostBundle(Strict):
    url: Annotated[str, Field(max_length=512, pattern=r'^https://github\.com/TED-H-com/agentic-smart-home/releases/download/v[0-9a-z.-]+/agentic-home-host-[0-9a-z.-]+\.tar$')]
    sha256: HexDigest
    size: Annotated[int, Field(ge=1024, le=128 * 1024 * 1024)]


class HostBundleCompatibility(Strict):
    host_protocol: Literal[1]
    controller_protocol: Literal[1]
    controller_schema: Literal[3]
    reconcile: Literal[1]

    @field_validator('host_protocol', 'controller_protocol', 'controller_schema', 'reconcile', mode='before')
    @classmethod
    def exact_integers(cls, value):
        if type(value) is not int:
            raise ValueError('host_update_protocol_invalid')
        return value


class HostBundleTest(Strict):
    name: Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]{1,96}$')]
    result: Literal['passed']
    sha256: HexDigest


class HostBundleManifest(Strict):
    format: Literal[1]
    kind: Literal['agentic-home-host-bundle']
    publisher: Literal['ted-h-com-agentic-smart-home']
    version: Version
    source_commit: Annotated[str, Field(pattern=r'^[a-f0-9]{40}$')]
    bundle: HostBundle
    compatibility: HostBundleCompatibility
    issued_at: StrictInt | StrictFloat
    expires_at: StrictInt | StrictFloat
    tests: Annotated[list[HostBundleTest], Field(min_length=1, max_length=20)]

    @field_validator('format', mode='before')
    @classmethod
    def exact_format(cls, value):
        if type(value) is not int:
            raise ValueError('host_update_format_invalid')
        return value

    @model_validator(mode='after')
    def fixed_release_and_valid_window(self):
        expected = f'https://github.com/TED-H-com/agentic-smart-home/releases/download/v{self.version}/agentic-home-host-{self.version}.tar'
        if self.bundle.url != expected:
            raise ValueError('host_update_release_target_invalid')
        if (not math.isfinite(self.issued_at) or not math.isfinite(self.expires_at)
                or not 0 < self.expires_at - self.issued_at <= 30 * 86400):
            raise ValueError('host_update_time_window_invalid')
        return self


class HostBundleStage(Strict):
    manifest: HostBundleManifest
    signature: Annotated[str, Field(min_length=80, max_length=100, pattern=r'^[A-Za-z0-9+/]+=*$')]


class HostBundleReference(Strict):
    candidate_digest: HexDigest


class HostBundleApply(HostBundleReference):
    approval_id: ApprovalId


class HostRestoreSummary(Strict):
    model_config = ConfigDict(extra='ignore', strict=True)
    scope: Annotated[str, Field(max_length=160)] | None = None
    state: Reason | None = None
    ha_snapshot: Annotated[str, Field(max_length=100)] | None = None
    root_snapshot_name: Annotated[str, Field(max_length=128)] | None = None


class HostBundleRecord(Strict):
    model_config = ConfigDict(extra='ignore', strict=True)
    candidate_digest: HexDigest
    manifest: HostBundleManifest
    state: Literal['staging', 'staged', 'queued', 'activating', 'deployed', 'rolling_back', 'rolled_back', 'failed', 'needs_reconcile']
    created_at: StrictInt | StrictFloat
    updated_at: StrictInt | StrictFloat
    message: Annotated[str, Field(max_length=256)]
    error_code: Reason | None = None
    auto_rollback: bool = False
    approval_id: ApprovalId | None = None
    approval_expires_at: StrictInt | StrictFloat | None = None
    rollback_approval_id: ApprovalId | None = None
    rollback_expires_at: StrictInt | StrictFloat | None = None
    restore_point: HostRestoreSummary | None = None

    @model_validator(mode='after')
    def capability_pairs(self):
        for token, expires in ((self.approval_id, self.approval_expires_at),
                               (self.rollback_approval_id, self.rollback_expires_at)):
            if (token is None) != (expires is None) or expires is not None and not math.isfinite(expires):
                raise ValueError('host_update_approval_invalid')
        if not math.isfinite(self.created_at) or not math.isfinite(self.updated_at):
            raise ValueError('host_update_timestamp_invalid')
        return self


class HostBundleCheck(Strict):
    model_config = ConfigDict(extra='ignore', strict=True)
    state: Literal['idle', 'checking', 'available', 'unavailable', 'failed']
    code: Reason | None = None
    message: Annotated[str, Field(max_length=256)]
    candidate_digest: HexDigest | None = None
    checked_at: StrictInt | StrictFloat | None = None


class HostBundleStatus(Strict):
    model_config = ConfigDict(extra='ignore', strict=True)
    contract: Literal[1]
    scope: Literal['host_bundle_same_schema']
    compatibility: HostBundleCompatibility | None
    available: bool
    code: Reason | None = None
    message: Annotated[str, Field(max_length=256)]
    records: Annotated[list[HostBundleRecord], Field(max_length=50)]
    current_source_commit: Annotated[str, Field(pattern=r'^[a-f0-9]{40}$')] | None = None
    check: HostBundleCheck

    @field_validator('contract', mode='before')
    @classmethod
    def exact_contract(cls, value):
        if type(value) is not int:
            raise ValueError('host_update_contract_invalid')
        return value

    @model_validator(mode='after')
    def compatible_capability(self):
        if self.available and self.compatibility is None:
            raise ValueError('host_update_compatibility_required')
        if len({item.candidate_digest for item in self.records}) != len(self.records):
            raise ValueError('host_update_candidates_ambiguous')
        return self


def parsed_status(raw):
    try:
        return HostBundleStatus.model_validate(raw)
    except ValidationError:
        raise HTTPException(503, '主機更新介面不相容，請先取得經核准的更新啟用支援。') from None


def parsed_record(raw):
    try:
        return HostBundleRecord.model_validate(raw)
    except ValidationError:
        raise HTTPException(503, '無法核對主機更新版本，請重新檢查。') from None


def unsupported_status():
    message = '目前主機需要先由你核准一次更新啟用，才能直接檢查主機更新；不需重裝，請聯絡支援協助。'
    return HostBundleStatus(contract=1, scope='host_bundle_same_schema', compatibility=None, available=False,
                           code='host_update_bootstrap_required', message=message, records=[],
                           check=HostBundleCheck(state='unavailable', code='host_update_bootstrap_required', message=message))


async def broker_call(method, path, body=None, *, store):
    """The endpoint allowlist is fixed; no URL/peer from a request is used here."""
    allowed = (method == 'GET' and (path == '/host-updates' or re.fullmatch(r'/host-updates/[a-f0-9]{64}', path))) or (
        method == 'POST' and path in {'/host-updates/check', '/host-updates/stage', '/host-updates/apply',
                                     '/host-updates/rollback-offer', '/host-updates/rollback'})
    if not allowed:
        raise HTTPException(422, 'invalid_host_update_operation')
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
                    code = value.get('detail')
                    safe = code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', code) else 'host_update_operation_failed'
                    raise HTTPException(response.status, safe)
                return value
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError):
        raise HTTPException(503, '主機更新服務暫時無法回應，恢復連線後會自動核對結果。') from None


class HostUpdates:
    def __init__(self, store, host_auth, *, host_mode=False):
        self.store, self.host_auth, self.host_mode = store, host_auth, host_mode

    def owner(self, request):
        if (not self.host_mode or getattr(request.state, 'bridge_identity', None)
                or self.host_auth is None or not self.host_auth.verify(request)):
            raise HTTPException(403, '請從主機管理頁登入並確認主機更新。')
        authority = {'owner': self.store.setting('owner'), 'epoch': self.store.setting('hermes_web_epoch'),
                     'session': digest(request.cookies.get('agentic_host', ''))}
        self.guard(request, authority)
        return authority

    def guard(self, request, authority):
        if (self.host_auth is None or not self.host_auth.verify(request)
                or getattr(request.state, 'bridge_identity', None)
                or self.store.setting('owner') != authority['owner']
                or self.store.setting('hermes_web_epoch') != authority['epoch']
                or digest(request.cookies.get('agentic_host', '')) != authority['session']):
            raise HTTPException(403, '主機登入已變更，請重新登入並查看更新版本。')

    def remember(self, records, authority):
        """Only hashes of short-lived Host capabilities accompany owner receipts."""
        offers = self.store.setting('host_bundle_owner_offers', {})
        now = time.time()
        offers = {key: value for key, value in offers.items() if value['expires'] > now}
        for record in records:
            for action, token, expires in (
                    ('apply', record.approval_id, record.approval_expires_at),
                    ('rollback', record.rollback_approval_id, record.rollback_expires_at)):
                if token is not None and expires is not None and expires > now:
                    offers[action + ':' + record.candidate_digest] = {
                        'token': digest(token), 'expires': expires, 'authority': authority}
        # Host records and this ledger are bounded; no raw token is persisted.
        self.store.set_setting('host_bundle_owner_offers', dict(list(offers.items())[-200:]))

    def exact_offer(self, body, action, request):
        record = self.store.setting('host_bundle_owner_offers', {}).get(action + ':' + body.candidate_digest)
        if not record or record['token'] != digest(body.approval_id) or record['expires'] <= time.time():
            raise HTTPException(409, '主機更新確認已過期，請重新檢查版本。')
        self.guard(request, record['authority'])
        return record

    async def status(self, request, *, remember=True):
        authority = self.owner(request)
        try:
            raw = await broker_call('GET', '/host-updates', store=self.store)
        except HTTPException as error:
            if error.status_code not in {404, 405}:
                raise
            raw = unsupported_status().model_dump(mode='json')
        self.guard(request, authority)
        value = parsed_status(raw)
        if remember:
            self.remember(value.records, authority)
            await durable(self.store)
            self.guard(request, authority)
        return value

    async def preflight(self, request, *, require_source=True):
        authority = self.owner(request)
        status = await self.status(request, remember=False)
        self.guard(request, authority)
        if status.compatibility is None or require_source and not status.available:
            raise HTTPException(409, status.message or '主機更新尚未啟用，請先核對更新來源。')
        return authority, status

    async def check(self, request):
        authority, _ = await self.preflight(request)
        await durable(self.store)
        self.guard(request, authority)
        value = parsed_status(await broker_call('POST', '/host-updates/check', {}, store=self.store))
        self.guard(request, authority)
        self.remember(value.records, authority)
        await durable(self.store)
        self.guard(request, authority)
        return value

    async def stage(self, body, request):
        authority, _ = await self.preflight(request)
        await durable(self.store)
        self.guard(request, authority)
        value = parsed_record(await broker_call('POST', '/host-updates/stage', body.model_dump(), store=self.store))
        self.guard(request, authority)
        self.remember([value], authority)
        await durable(self.store)
        self.guard(request, authority)
        return value

    async def apply(self, body, request, *, rollback=False):
        authority = self.owner(request)
        action = 'rollback' if rollback else 'apply'
        offer = self.exact_offer(body, action, request)
        _, status = await self.preflight(request, require_source=not rollback)
        current = next((item for item in status.records if item.candidate_digest == body.candidate_digest), None)
        token = current.rollback_approval_id if rollback and current else current.approval_id if current else None
        expected = 'deployed' if rollback else 'staged'
        if current is None or current.state != expected or token != body.approval_id:
            raise HTTPException(409, '主機更新版本已變更，請重新查看並確認。')
        await durable(self.store)
        self.guard(request, authority)
        self.guard(request, offer['authority'])
        self.exact_offer(body, action, request)
        value = parsed_record(await broker_call('POST', '/host-updates/' + action, body.model_dump(), store=self.store))
        # The Host owns a durable transaction from this point. A lost/revoked
        # response never invents completion or automatically submits again.
        self.guard(request, authority)
        return value

    async def rollback_offer(self, body, request):
        authority, status = await self.preflight(request, require_source=False)
        current = next((item for item in status.records if item.candidate_digest == body.candidate_digest), None)
        if current is None or current.state != 'deployed':
            raise HTTPException(409, '這個版本目前無法退回，請重新讀取主機更新狀態。')
        await durable(self.store)
        self.guard(request, authority)
        value = parsed_record(await broker_call('POST', '/host-updates/rollback-offer', body.model_dump(), store=self.store))
        self.guard(request, authority)
        self.remember([value], authority)
        await durable(self.store)
        self.guard(request, authority)
        return value

    async def record(self, candidate_digest, request):
        authority = self.owner(request)
        value = parsed_record(await broker_call('GET', '/host-updates/' + candidate_digest, store=self.store))
        self.guard(request, authority)
        self.remember([value], authority)
        await durable(self.store)
        self.guard(request, authority)
        return value


def routes(app, store, *, host_auth, host_mode=False):
    operations = HostUpdates(store, host_auth, host_mode=host_mode)

    @app.get('/api/host-updates', response_model=HostBundleStatus)
    async def status(request: Request):
        return await operations.status(request)

    @app.post('/api/host-updates/check', response_model=HostBundleStatus, status_code=202)
    async def check(body: WorkerAction, request: Request):
        return await operations.check(request)

    @app.post('/api/host-updates/stage', response_model=HostBundleRecord, status_code=202)
    async def stage(body: HostBundleStage, request: Request):
        return await operations.stage(body, request)

    @app.post('/api/host-updates/apply', response_model=HostBundleRecord, status_code=202)
    async def apply(body: HostBundleApply, request: Request):
        return await operations.apply(body, request)

    @app.post('/api/host-updates/rollback-offer', response_model=HostBundleRecord)
    async def rollback_offer(body: HostBundleReference, request: Request):
        return await operations.rollback_offer(body, request)

    @app.post('/api/host-updates/rollback', response_model=HostBundleRecord, status_code=202)
    async def rollback(body: HostBundleApply, request: Request):
        return await operations.apply(body, request, rollback=True)

    @app.get('/api/host-updates/{candidate_digest}', response_model=HostBundleRecord)
    async def record(candidate_digest: str, request: Request):
        if not re.fullmatch(r'[a-f0-9]{64}', candidate_digest):
            raise HTTPException(422, 'invalid_host_update_candidate')
        return await operations.record(candidate_digest, request)

    return operations
