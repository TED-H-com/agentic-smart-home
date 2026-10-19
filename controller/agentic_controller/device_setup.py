"""Owner-approved, bounded IPP discovery confirmation against Core 2026.10.1.

The private frontend flow API is intentionally isolated here. Only an existing
zeroconf_confirm step with an empty schema is supported; no arbitrary fields,
URLs, credentials, new flows, model approvals, or device-control grants exist.
Upstream references: Core tag 2026.10.1 components/ipp/config_flow.py,
components/ipp/entity.py, components/config/config_entries.py and
helpers/data_entry_flow.py. A lost POST is never automatically repeated.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import Field, ValidationError

from .discovery import DiscoveryAuthority, identifier, project, snapshot
from .ha import HAError
from .models import (
    DiscoveryCandidate,
    DiscoveryDevice,
    DiscoveryIdentifier,
    DiscoveryIntegration,
    DiscoveryRevision,
    DiscoverySource,
    PreviewId,
    Strict,
    canonical,
    digest,
    utcnow,
)
from .response_durability import RESPONSE_GUARD

KIND = "device_setup_v1"
SUPPORTED_CORE = "2026.10.1"
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
State = Literal["proposed", "preparing", "applying", "configured", "needs_reconcile", "blocked"]
PRE_CONFIRMATION_PHASES = {"approved", "backup_prepared", "backup_completed"}


class SendGuardRejected(HTTPException):
    """The transport guard rejected before sending mutation headers/body."""


class DeviceSetupCreate(Strict):
    candidate_id: DiscoveryIdentifier
    integration_domain: Literal["ipp"] = "ipp"


class DeviceSetupApprove(Strict):
    expected_revision: DiscoveryRevision
    proposal_digest: Hash


class DeviceSetupReconcile(Strict):
    expected_revision: DiscoveryRevision


class DeviceSetupView(Strict):
    contract_version: Literal[1] = 1
    id: PreviewId
    revision: DiscoveryRevision
    proposal_digest: Hash
    candidate: DiscoveryCandidate
    candidate_aliases: Annotated[list[DiscoveryIdentifier], Field(max_length=16)] = []
    state: State
    can_approve: bool
    can_reconcile: bool
    message: str
    reason_code: str | None = None
    created_at: str
    updated_at: str
    expires_at: float
    backup_id: DiscoveryIdentifier | None = None
    backup_scope: Literal["home_assistant_configuration"] = "home_assistant_configuration"
    integration: DiscoveryIntegration | None = None
    devices: Annotated[list[DiscoveryDevice], Field(max_length=100)] = []
    devices_truncated: bool = False
    device_count: int | None = None
    entity_count: int | None = None
    available_entity_count: int | None = None
    sources: list[DiscoverySource] = []
    observed_at: str | None = None
    physical_verification: Literal["not_performed"] = "not_performed"
    native_path: Literal["/config/integrations"] = "/config/integrations"


class DeviceSetupList(Strict):
    contract_version: Literal[1] = 1
    setups: list[DeviceSetupView]


class SetupRecord(Strict):
    version: Literal[1] = 1
    id: PreviewId
    binding: Hash
    revision: DiscoveryRevision = 1
    proposal_digest: Hash
    candidate: DiscoveryCandidate
    candidate_aliases: Annotated[list[DiscoveryIdentifier], Field(max_length=16)] = []
    flow_digest: Hash
    form_digest: Hash
    identity_digest: Hash
    baseline_entry_ids: Annotated[list[DiscoveryIdentifier], Field(max_length=1000)]
    state: State = "proposed"
    reason_code: str | None = None
    created_at: str
    updated_at: str
    expires_at: float
    approved_at: str | None = None
    backup_id: DiscoveryIdentifier | None = None
    entry_id: DiscoveryIdentifier | None = None
    phase: str = "proposal"


MESSAGES = {
    "proposed": "確認後會先備份 Home Assistant 設定，再接入這台 IPP 印表機；不會列印或授予 AI 控制權限。",
    "preparing": "正在建立並核對 Home Assistant 設定備份；完成後才會接入印表機。",
    "applying": "接入請求已準備送出，正在核對 Home Assistant 的結果。",
    "configured": "已核對這份接入請求對應的 HA 整合。下方顯示最新設備狀態；尚未驗證實體功能。",
    "needs_reconcile": "上次操作結果尚未確認；只會重新讀取結果，不會重送接入請求。",
    "blocked": "這次接入已停止。請查看原因，修復後重新建立提案。",
}


class DeviceSetups:
    def __init__(self, app, store, ha, host_session=None, *, writer=None, spawn=None):
        self.app, self.store, self.ha, self.host_session = app, store, ha, host_session
        self.writer = writer or asyncio.Lock()
        self.spawn = spawn or (lambda _id, coroutine: asyncio.create_task(coroutine))
        self.running = {}

    def private_hash(self, value):
        return hmac.new(bytes.fromhex(self.store.setting("csrf_secret")), canonical(value).encode(),
                        hashlib.sha256).hexdigest()

    def binding(self, authority):
        return self.private_hash([KIND, authority.owner, *authority.connection])

    async def authorize(self, request):
        authority = DiscoveryAuthority(self.app, self.store, self.ha, request, self.host_session)
        await authority.check_owner()
        self.prune()
        request.scope[RESPONSE_GUARD] = authority.check_owner
        return authority, self.binding(authority)

    def guard(self, authority):
        authority.guard()
        if self.store.setting("updater_gate") or self.store.setting("host_bundle_update_gate"):
            raise HTTPException(423, "device_setup_update_in_progress")

    def write_guard(self, authority):
        self.guard(authority)
        if self.store.setting("paused", False):
            raise HTTPException(409, "device_setup_paused")

    def records(self, binding, *, for_write=False):
        result = []
        for row in self.store.db.execute("SELECT body FROM records WHERE kind=? AND "
                "json_extract(body,'$.binding')=? ORDER BY created DESC,id DESC", (KIND, binding)):
            try:
                result.append(SetupRecord.model_validate_json(row[0]))
            except ValidationError:
                if for_write:
                    raise HTTPException(409, "device_setup_record_contract_changed") from None
                continue  # Future records do not grant authority to old binaries.
        return result

    def record(self, binding, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise HTTPException(404, "device_setup_not_found")
        raw = self.store.get(KIND, identifier)
        try:
            record = SetupRecord.model_validate(raw)
        except ValidationError:
            raise HTTPException(404, "device_setup_not_found") from None
        if record.binding != binding:
            raise HTTPException(404, "device_setup_not_found")
        return record

    def save(self, record, **updates):
        value = record.model_copy(update={"revision": record.revision + 1, "updated_at": utcnow(), **updates})
        # Validation catches unknown or oversized upstream identifiers before persistence.
        value = SetupRecord.model_validate(value.model_dump())
        self.store.put(KIND, value.model_dump())
        return value

    def recover(self):
        for raw in self.store.list(KIND, 100000):
            try:
                record = SetupRecord.model_validate(raw)
            except ValidationError:
                continue
            if record.state in {"preparing", "applying"}:
                # Every confirmation passes a durable confirmation_prepared
                # barrier. An earlier durable phase proves it was not sent.
                state = "blocked" if record.phase in PRE_CONFIRMATION_PHASES else "needs_reconcile"
                self.save(record, state=state, reason_code="device_setup_interrupted")

    def prune(self):
        """Expire household labels, retaining bounded opaque effect fences.

        An unresolved confirmation must survive context/audit expiry and an old
        binary's generic pruning. Deleting its identity would permit a repeated
        write after a lost response. No raw device identifiers are retained.
        """
        days = self.store.setting("retention", {"context_days": 7})["context_days"]
        pending = self.store.setting("retention_pending")
        if pending:
            days = min(days, pending["context_days"])
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        redacted = "IPP 印表機（名稱已依保存設定移除）"
        for raw in self.store.list(KIND, 100000):
            try:
                record = SetupRecord.model_validate(raw)
            except ValidationError:
                continue
            if record.created_at < cutoff and record.candidate.title != redacted:
                self.save(record, candidate=record.candidate.model_copy(update={"title": redacted}))

    def receipt(self, request, binding, action, body, identifier=""):
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise HTTPException(400, "device_setup_idempotency_required")
        scope, payload = f"{KIND}:{binding}:{action}:{identifier}", digest(body.model_dump())
        row = self.store.db.execute("SELECT digest,result FROM requests WHERE scope=? AND key=?",
                                    (scope, key)).fetchone()
        if row and row[0] != payload:
            raise HTTPException(409, "device_setup_request_conflict")
        return (scope, key, payload), row[1] if row else None

    def commit_receipt(self, record, receipt):
        with self.store.transaction() as tx:
            tx.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE "
                "SET body=excluded.body,updated=excluded.updated", (KIND, record.id,
                canonical(record.model_dump()), record.created_at, record.updated_at))
            tx.execute("INSERT INTO requests VALUES (?,?,?,?)", (*receipt, record.id))

    async def read(self, authority, method, target):
        self.guard(authority)
        try:
            result = await (self.ha.ws(target) if method == "ws" else self.ha.request("GET", target))
        except HAError as exc:
            raise HTTPException(503, "device_setup_source_unavailable") from exc
        self.guard(authority)
        return result

    async def current_flow(self, authority, candidate_id):
        config = await self.read(authority, "rest", "/config")
        if not isinstance(config, dict) or config.get("version") != SUPPORTED_CORE:
            raise HTTPException(409, "device_setup_core_unsupported")
        flows = await self.read(authority, "ws", "config_entries/flow/progress")
        if not isinstance(flows, list) or len(flows) > 256:
            raise HTTPException(409, "device_setup_flow_contract_changed")
        matches = [row for row in flows if isinstance(row, dict) and row.get("flow_id") == candidate_id]
        if len(matches) != 1:
            raise HTTPException(409, "device_setup_candidate_changed")
        flow = matches[0]
        context = flow.get("context")
        if (flow.get("handler") != "ipp" or flow.get("step_id") != "zeroconf_confirm"
                or not isinstance(context, dict) or context.get("source") != "zeroconf"):
            raise HTTPException(409, "device_setup_flow_unsupported")
        unique_id = context.get("unique_id")
        if not isinstance(unique_id, str) or not 1 <= len(unique_id) <= 256:
            raise HTTPException(409, "device_setup_identity_unavailable")
        if len(canonical(flow)) > 32768:
            raise HTTPException(409, "device_setup_flow_contract_changed")
        # This GET invokes the step with None. The pinned IPP confirmation step
        # only returns its form; never GET arbitrary handlers or advance steps.
        form = await self.read(authority, "rest", "/config/config_entries/flow/" + identifier(candidate_id))
        if (not isinstance(form, dict) or form.get("type") != "form" or form.get("flow_id") != candidate_id
                or form.get("handler") != "ipp" or form.get("step_id") != "zeroconf_confirm"
                or form.get("data_schema") != [] or form.get("errors") != {} or len(canonical(form)) > 32768):
            raise HTTPException(409, "device_setup_flow_unsupported")
        await authority.check_owner()
        self.guard(authority)
        try:
            candidate = DiscoveryCandidate.model_validate(project("ha_discovery", flow))
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, "device_setup_flow_contract_changed") from exc
        return candidate, self.private_hash(flow), self.private_hash(form), self.private_hash(["ipp", unique_id])

    async def entries(self, authority):
        rows = await self.read(authority, "ws", "config_entries/get")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise HTTPException(503, "device_setup_source_incomplete")
        try:
            ids = [identifier(row["entry_id"]) for row in rows]
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(503, "device_setup_source_incomplete") from exc
        if len(set(ids)) != len(ids):
            raise HTTPException(503, "device_setup_source_incomplete")
        return rows

    async def exact(self, authority, record):
        _, flow_hash, form_hash, identity_hash = await self.current_flow(authority, record.candidate.id)
        if (flow_hash, form_hash, identity_hash) != (record.flow_digest, record.form_digest, record.identity_digest):
            raise HTTPException(409, "device_setup_candidate_changed")

    def view(self, record):
        return DeviceSetupView(**record.model_dump(include={"id", "revision", "proposal_digest", "candidate", "candidate_aliases",
            "state", "reason_code", "created_at", "updated_at", "expires_at", "backup_id"}),
            can_approve=record.state == "proposed" and time.time() < record.expires_at,
            can_reconcile=record.state in {"needs_reconcile", "configured"}, message=MESSAGES[record.state])

    async def create(self, request, body):
        authority, binding = await self.authorize(request)
        receipt, previous = self.receipt(request, binding, "create", body)
        if previous:
            return self.view(self.record(binding, previous))
        candidate, flow_hash, form_hash, identity_hash = await self.current_flow(authority, body.candidate_id)
        entries = await self.entries(authority)
        await authority.check_owner()
        self.guard(authority)
        receipt, previous = self.receipt(request, binding, "create", body)
        if previous:
            return self.view(self.record(binding, previous))
        records = self.records(binding, for_write=True)
        current_entries = {row["entry_id"] for row in entries}
        def previously_completed_and_removed(row):
            return (row.phase == "registration_readback" and row.entry_id is not None
                    and row.entry_id not in current_entries and row.candidate.id != candidate.id)
        # An uncertain earlier attempt must not be bypassed using a new key or
        # a rediscovered flow ID. IPP's stable private identity binds both.
        existing = next((row for row in records if row.identity_digest == identity_hash and
            row.state != "blocked" and not previously_completed_and_removed(row)
            and (row.state != "proposed" or row.expires_at > time.time())), None)
        if existing:
            if candidate.id != existing.candidate.id and candidate.id not in existing.candidate_aliases:
                if len(existing.candidate_aliases) >= 16:
                    raise HTTPException(409, "device_setup_alias_limit_reached")
                existing = self.save(existing, candidate_aliases=[*existing.candidate_aliases, candidate.id])
            self.commit_receipt(existing, receipt)
            return self.view(existing)
        if len(records) >= 100:
            raise HTTPException(409, "device_setup_limit_reached")
        now = utcnow()
        proposal = dict(id=uuid.uuid4().hex, binding=binding, candidate=candidate.model_dump(),
            flow_digest=flow_hash, form_digest=form_hash, identity_digest=identity_hash,
            baseline_entry_ids=sorted(row["entry_id"] for row in entries), created_at=now, updated_at=now,
            expires_at=time.time() + 600)
        record = SetupRecord(**proposal, proposal_digest=digest(proposal))
        self.commit_receipt(record, receipt)
        return self.view(record)

    async def approve(self, request, identifier, body):
        authority, binding = await self.authorize(request)
        receipt, previous = self.receipt(request, binding, "approve", body, identifier)
        if previous:
            return self.view(self.record(binding, previous))
        record = self.record(binding, identifier)
        if (record.revision != body.expected_revision or record.proposal_digest != body.proposal_digest
                or record.state != "proposed" or time.time() >= record.expires_at):
            raise HTTPException(409, "device_setup_proposal_changed")
        self.write_guard(authority)
        record = record.model_copy(update={"state": "preparing", "phase": "approved", "approved_at": utcnow(),
            "updated_at": utcnow(), "revision": record.revision + 1})
        self.commit_receipt(record, receipt)
        await self.store.durable()
        try:
            await authority.check_owner()
            self.write_guard(authority)
        except HTTPException:
            self.save(record, state="blocked", reason_code="device_setup_approval_changed")
            raise
        task = self.spawn("device-setup:" + record.id, self.execute(authority, record))
        self.running[record.id] = task
        task.add_done_callback(lambda _done: self.running.pop(record.id, None))
        return self.view(record)

    async def execute(self, authority, record):
        try:
            await self._execute(authority, record)
        except asyncio.CancelledError:
            # Covers cancellation while waiting for the shared household writer,
            # before _execute enters its effect-aware exception handling.
            current = self.record(record.binding, record.id)
            if current.state in {"preparing", "applying"}:
                self.save(current, state="needs_reconcile" if current.state == "applying" else "blocked",
                          reason_code="device_setup_interrupted")
            raise

    async def _execute(self, authority, record):
        mutated = False
        async with self.writer:
            def local_guard():
                self.write_guard(authority)
                current = self.record(record.binding, record.id)
                if (current.revision != record.revision or current.proposal_digest != record.proposal_digest
                        or current.state not in {"preparing", "applying"} or time.time() >= current.expires_at):
                    raise HTTPException(409, "device_setup_approval_changed")

            async def send_guard():
                try:
                    local_guard()
                    await self.exact(authority, record)
                    baseline = await self.entries(authority)
                    if sorted(row["entry_id"] for row in baseline) != record.baseline_entry_ids:
                        raise HTTPException(409, "device_setup_integrations_changed")
                    await authority.check_owner()
                    local_guard()
                except HTTPException as exc:
                    raise SendGuardRejected(exc.status_code, exc.detail) from None
            try:
                await send_guard()
                record = self.save(record, phase="backup_prepared")
                await self.store.durable()
                await send_guard()
                backup = await self.ha.backup("Agentic Home IPP " + record.id[:12], send_guard=send_guard)
                # A usable Supervisor partial HA backup has been read back by
                # HomeAssistant.backup. This is not a host/full-system restore.
                backup_id = identifier(backup.get("id"))
                self.store.put("backup", backup)
                record = self.save(record, backup_id=backup_id, phase="backup_completed")
                await self.store.snapshot(self.store.path.parent / "snapshots" / (record.id + ".sqlite3"))
                await send_guard()
                baseline = await self.entries(authority)
                if sorted(row["entry_id"] for row in baseline) != record.baseline_entry_ids:
                    raise HTTPException(409, "device_setup_integrations_changed")
                record = self.save(record, state="applying", phase="confirmation_prepared")
                await self.store.durable()
                await send_guard()
                # Intent and backup are durable before the only configuration
                # write. Mark uncertain before awaiting transport, including
                # cancellation and failures after HA accepted the body.
                mutated = True
                result = await self.ha.request("POST", "/config/config_entries/flow/" + record.candidate.id,
                                               {}, send_guard=send_guard)
                if (not isinstance(result, dict) or result.get("type") != "create_entry"
                        or result.get("flow_id") != record.candidate.id or result.get("handler") != "ipp"):
                    raise ValueError("unconfirmed_response")
                entry = result.get("result")
                if (not isinstance(entry, dict) or entry.get("domain") != "ipp"
                        or entry.get("source") != "zeroconf"):
                    raise ValueError("unconfirmed_entry")
                entry_id = identifier(entry.get("entry_id"))
                if entry_id in record.baseline_entry_ids:
                    raise ValueError("preexisting_entry")
                # Keep the exact response identity even if Owner access was
                # revoked while HA replied. No household readback is disclosed.
                record = self.save(record, entry_id=entry_id, phase="confirmation_acknowledged")
                await self.store.durable()
                await authority.check_owner()
                self.guard(authority)
                record = await self.reconcile_record(authority, record)
                if record.state != "configured":
                    self.save(record, state="needs_reconcile", reason_code="device_setup_readback_pending")
            except asyncio.CancelledError:
                self.save(record, state="needs_reconcile" if mutated else "blocked",
                          reason_code="device_setup_interrupted")
                raise
            except (HTTPException, HAError, ValueError, TypeError, OSError) as exc:
                if isinstance(exc, SendGuardRejected):
                    mutated = False  # Both REST hooks and WS guards run before sending.
                reason = (exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str)
                          and exc.detail.startswith("device_setup_") else "device_setup_preparation_failed")
                self.save(record, state="needs_reconcile" if mutated else "blocked",
                          reason_code="device_setup_outcome_unknown" if mutated else reason)
            # Disk failures deliberately propagate: no more effects or response
            # claims are safe after a failed journal. Recovery reads its last
            # durable intent and never resends the confirmation.
            await self.store.durable()

    async def reconcile_record(self, authority, record):
        if record.phase in PRE_CONFIRMATION_PHASES and record.entry_id is None:
            await authority.check_owner()
            self.guard(authority)
            return self.save(record, state="blocked", reason_code="device_setup_interrupted")
        entries = await self.entries(authority)
        selected = next((row for row in entries if row.get("entry_id") == record.entry_id
                         and row.get("domain") == "ipp" and row.get("source") == "zeroconf"), None) if record.entry_id else None
        if not record.entry_id and record.phase in {"confirmation_prepared", "confirmation_acknowledged"}:
            devices = await self.read(authority, "ws", "config/device_registry/list")
            if not isinstance(devices, list) or len(devices) > 1000:
                raise HTTPException(503, "device_setup_source_incomplete")
            matches = set()
            for device in devices:
                if not isinstance(device, dict):
                    raise HTTPException(503, "device_setup_source_incomplete")
                ids = device.get("identifiers", [])
                if not isinstance(ids, list):
                    raise HTTPException(503, "device_setup_source_incomplete")
                if any(isinstance(value, list) and len(value) == 2 and value[0] == "ipp"
                       and self.private_hash(value) == record.identity_digest for value in ids):
                    associated = device.get("config_entries")
                    if not isinstance(associated, list):
                        raise HTTPException(503, "device_setup_source_incomplete")
                    matches.update(row["entry_id"] for row in entries if row["entry_id"] in associated
                        and row["entry_id"] not in record.baseline_entry_ids and row.get("domain") == "ipp"
                        and row.get("source") == "zeroconf")
            if len(matches) == 1:
                selected = next(row for row in entries if row["entry_id"] in matches)
        await authority.check_owner()
        self.guard(authority)
        if selected:
            # Readback is exact registration, not proof it loaded or printed.
            project("ha_integrations", selected)
            return self.save(record, entry_id=selected["entry_id"], state="configured", reason_code=None,
                             phase="registration_readback")
        return self.save(record, state="needs_reconcile", reason_code="device_setup_entry_not_verified")

    async def detail(self, request, identifier):
        authority, binding = await self.authorize(request)
        record = self.record(binding, identifier)
        result = self.view(record)
        if record.entry_id:
            observed = await snapshot(self.ha)
            await authority.check_owner()
            self.guard(authority)
            if self.record(binding, identifier).revision != record.revision:
                raise HTTPException(409, "device_setup_revision_changed")
            sources = {item.id: item for item in observed.sources}
            selected = next((item for item in observed.integrations if item.id == record.entry_id
                             and item.domain == "ipp"), None)
            devices = [item.model_copy(update={"integration_ids": [record.entry_id]}) for item in observed.devices
                       if selected and record.entry_id in item.integration_ids]
            complete = selected is not None and sources["ha_devices"].state == "ok"
            entities = complete and sources["ha_entities"].state == "ok"
            states = entities and sources["ha_states"].state == "ok"
            # A physical device can belong to several integrations. Its other
            # entities are not evidence for the config entry we just created.
            counts, available = await self.entry_entities(authority, record.entry_id) if selected else (None, None)
            devices = [item.model_copy(update={
                "entity_count": sum(entity["device_id"] == item.id for entity in counts) if counts is not None else None,
                "available_entity_count": sum(entity["device_id"] == item.id and not entity["disabled"]
                    and entity["id"] in available for entity in counts)
                    if counts is not None and available is not None else None}) for item in devices]
            result = result.model_copy(update=dict(integration=selected, devices=devices[:100],
                devices_truncated=len(devices) > 100, device_count=len(devices) if complete else None,
                entity_count=len(counts) if entities and counts is not None else None,
                available_entity_count=sum(not entity["disabled"] and entity["id"] in available for entity in counts)
                    if states and counts is not None and available is not None else None,
                sources=observed.sources, observed_at=observed.observed_at))
            if not selected:
                result.message = ("先前的接入結果已保存，但這次未看到對應整合；請核對 HA 是否移除或無法讀取。")
        async def guard():
            await authority.check_owner()
            if self.record(binding, identifier).revision != record.revision:
                raise HTTPException(409, "device_setup_revision_changed")
        request.scope[RESPONSE_GUARD] = guard
        return result

    async def entry_entities(self, authority, entry_id):
        try:
            raw = await self.read(authority, "ws", "config/entity_registry/list")
            if not isinstance(raw, list) or len(raw) > 5000:
                return None, None
            entities = []
            seen = set()
            for row in raw:
                value = project("ha_entities", row)
                if "config_entry_id" not in row:
                    return None, None
                identifier(row.get("config_entry_id"), optional=True)
                if value["id"] in seen:
                    return None, None
                seen.add(value["id"])
                if row.get("config_entry_id") == entry_id:
                    entities.append(value)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            return None, None
        except (ValueError, TypeError):
            return None, None
        try:
            raw = await self.read(authority, "rest", "/states")
            if not isinstance(raw, list) or len(raw) > 5000:
                return entities, None
            values = [project("ha_states", row) for row in raw]
            if len({value["id"] for value in values}) != len(values):
                return entities, None
            return entities, {value["id"] for value in values if value["available"]}
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            return entities, None
        except (ValueError, TypeError):
            return entities, None

    async def listing(self, request):
        _, binding = await self.authorize(request)
        return DeviceSetupList(setups=[self.view(record) for record in self.records(binding)[:100]])

    async def reconcile(self, request, identifier, body):
        authority, binding = await self.authorize(request)
        async with self.writer:
            receipt, previous = self.receipt(request, binding, "reconcile", body, identifier)
            if previous:
                return self.view(self.record(binding, previous))
            record = self.record(binding, identifier)
            if record.revision != body.expected_revision:
                raise HTTPException(409, "device_setup_revision_changed")
            if record.state not in {"configured", "needs_reconcile"} or identifier in self.running:
                raise HTTPException(409, "device_setup_reconcile_unavailable")
            updated = await self.reconcile_record(authority, record)
            self.commit_receipt(updated, receipt)
            return self.view(updated)


def routes(app, store, ha, host_session=None, *, writer=None, spawn=None):
    service = DeviceSetups(app, store, ha, host_session, writer=writer, spawn=spawn)
    app.state.device_setups = service

    @app.post("/api/device-setups", response_model=DeviceSetupView)
    async def create(body: DeviceSetupCreate, request: Request):
        return await service.create(request, body)

    @app.get("/api/device-setups", response_model=DeviceSetupList)
    async def listing(request: Request):
        return await service.listing(request)

    @app.get("/api/device-setups/{identifier}", response_model=DeviceSetupView)
    async def detail(identifier: str, request: Request):
        return await service.detail(request, identifier)

    @app.post("/api/device-setups/{identifier}/approve", response_model=DeviceSetupView, status_code=202)
    async def approve(identifier: str, body: DeviceSetupApprove, request: Request):
        return await service.approve(request, identifier, body)

    @app.post("/api/device-setups/{identifier}/reconcile", response_model=DeviceSetupView)
    async def reconcile(identifier: str, body: DeviceSetupReconcile, request: Request):
        return await service.reconcile(request, identifier, body)

    return service
