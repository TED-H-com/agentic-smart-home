"""Owner-approved bounded native setup against tested Core patches.

The private frontend flow API is intentionally isolated here. Only an existing
allowlisted step/schema is supported; no arbitrary fields, URLs, new flows,
model approvals, or device-control grants exist.
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
from pydantic import Field, RootModel, SecretStr, ValidationError, field_validator, model_validator

from .device_setup_adapters import ADAPTERS
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
SUPPORTED_CORE = frozenset({"2026.10.1", "2026.10.2"})
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
State = Literal["proposed", "preparing", "applying", "configured", "needs_reconcile", "needs_input", "blocked"]
InputStep = Literal["link", "2sa", "backup_share"]
InputError = Literal["invalid_auth", "cannot_connect", "unknown", "missing_data", "otp_failed"]
PRE_CONFIRMATION_PHASES = {"approved", "backup_prepared", "backup_completed"}


class SendGuardRejected(HTTPException):
    """The transport guard rejected before sending mutation headers/body."""


class DeviceSetupCreate(Strict):
    candidate_id: DiscoveryIdentifier
    integration_domain: Literal["ipp", "shelly", "synology_dsm"] = "ipp"


class DeviceSetupCredentials(Strict):
    username: Annotated[SecretStr, Field(min_length=1, max_length=128)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=256)]

    @field_validator("username", "password", mode="before")
    @classmethod
    def bounded_secret(cls, value, info):
        limit = 128 if info.field_name == "username" else 256
        if (not isinstance(value, str) or not 1 <= len(value) <= limit
                or info.field_name == "username" and re.search(r"[\x00-\x1f\x7f-\x9f]", value)):
            raise HTTPException(409, "device_setup_input_invalid")
        return value


class DeviceSetupApprove(Strict):
    expected_revision: DiscoveryRevision
    proposal_digest: Hash
    credentials: DeviceSetupCredentials | None = None


class DeviceSetupLinkInput(DeviceSetupCredentials):
    expected_revision: DiscoveryRevision
    step: Literal["link"]


class DeviceSetupOTPInput(Strict):
    expected_revision: DiscoveryRevision
    step: Literal["2sa"]
    otp_code: Annotated[SecretStr, Field(min_length=6, max_length=8, json_schema_extra={"pattern": r"^[0-9]{6,8}$"})]

    @field_validator("otp_code", mode="before")
    @classmethod
    def bounded_otp(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6,8}", value):
            raise HTTPException(409, "device_setup_input_invalid")
        return value


class DeviceSetupShareInput(Strict):
    expected_revision: DiscoveryRevision
    step: Literal["backup_share"]
    backup_share: Annotated[str, Field(pattern=r"^/[^\x00-\x1f]{1,255}$")]

    @field_validator("backup_share", mode="before")
    @classmethod
    def bounded_share(cls, value):
        if not isinstance(value, str) or not re.fullmatch(r"/[^\x00-\x1f]{1,255}", value):
            raise HTTPException(409, "device_setup_input_invalid")
        return value


class DeviceSetupInput(RootModel[Annotated[
        DeviceSetupLinkInput | DeviceSetupOTPInput | DeviceSetupShareInput, Field(discriminator="step")]]):
    pass


class DeviceSetupShareOption(Strict):
    value: Annotated[str, Field(pattern=r"^/[^\x00-\x1f]{1,255}$")]
    label: Annotated[str, Field(max_length=160)]


class DeviceSetupInputView(Strict):
    step: InputStep
    error: InputError | None = None
    options: Annotated[list[DeviceSetupShareOption], Field(max_length=64)] = []
    can_submit: bool


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
    input: DeviceSetupInputView | None = None
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
    # IPP/Shelly keep v2; Synology v3 fences writes by older binaries.
    version: Literal[1, 2, 3] = 1
    id: PreviewId
    binding: Hash
    # Access to history survives credential replacement for the same freshly
    # authenticated HA Owner and exact home endpoints. Execution still binds
    # the original token in binding and in the captured request authority.
    home_binding: Hash | None = None
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
    input_step: InputStep | None = None
    input_error: InputError | None = None
    share_options: Annotated[list[DeviceSetupShareOption], Field(max_length=64)] = []
    backup_path: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")] | None = None
    post_step: InputStep | None = None
    step_posts: Annotated[int, Field(ge=0, le=8)] = 0

    @model_validator(mode="after")
    def qualified_adapter(self):
        adapter = ADAPTERS.get(self.candidate.integration_domain)
        if (adapter is None or (self.version == 1 and adapter.domain != "ipp")
                or self.version == 3 and adapter.domain != "synology_dsm"
                or adapter.domain == "synology_dsm" and self.version != 3):
            raise ValueError("device_setup_record_adapter_mismatch")
        step = getattr(self.candidate, "confirmation_step", None)
        if step is not None and step != adapter.step:
            raise ValueError("device_setup_record_adapter_mismatch")
        return self


def record_body(record):
    # Keep v2 bodies readable by the pre-Synology reader with extra="forbid".
    exclude = {"input_step", "input_error", "share_options", "backup_path", "post_step", "step_posts"}
    return record.model_dump(exclude=exclude if record.version == 2
        and ADAPTERS[record.candidate.integration_domain].record_version == 2 else set())


MESSAGES = {
    "preparing": "正在建立並核對 Home Assistant 設定備份；完成後才會接入設備。",
    "applying": "接入請求已準備送出，正在核對 Home Assistant 的結果。",
    "configured": "已核對這份接入請求對應的 HA 整合。下方顯示最新設備狀態；尚未驗證實體功能。",
    "needs_reconcile": "上次操作結果尚未確認；只會重新讀取結果，不會重送接入請求。",
    "blocked": "這次接入已停止。請查看原因，修復後重新建立提案。",
}
INPUT_MESSAGES = {
    "link": "請輸入這台 NAS 的帳號與密碼。",
    "invalid_auth": "帳號或密碼不正確，請再輸入一次。",
    "cannot_connect": "連不到這台 NAS。請確認 NAS 已開機並接上家裡網路，再試一次。",
    "unknown": "NAS 回應異常，請稍後再試。",
    "missing_data": "NAS 尚未回報完整資料，請先完成 NAS 的儲存空間設定再試。",
    "2sa": "這個帳號開啟了兩步驟驗證。請輸入驗證器 App 目前顯示的驗證碼；通過後 NAS 會記住這台家庭系統。",
    "otp_failed": "驗證碼不正確或已過期，請輸入新的驗證碼。",
    "backup_share": "家庭系統可以把備份存到這台 NAS。請選一個共用資料夾。",
    "device_setup_input_expired": "這一步已逾時，請重新讀取後繼續。",
    "device_setup_input_unavailable": "這一步沒有送出。請重新讀取後再繼續。",
    "device_setup_already_configured": "這台 NAS 已經接入家庭系統。",
    "device_setup_existing_updated": "這台 NAS 原本已接入家庭系統，已用你輸入的帳號更新原有連線。",
    "device_setup_flow_gone": "家庭系統已結束這次接入，請重新探索設備後再試。",
    "device_setup_input_limit": "嘗試次數過多，這次接入已停止。請稍後重新開始。",
}


class DeviceSetups:
    def __init__(self, app, store, ha, host_session=None, *, writer=None, spawn=None):
        self.app, self.store, self.ha, self.host_session = app, store, ha, host_session
        self.writer = writer or asyncio.Lock()
        self.spawn = spawn or (lambda _id, coroutine: asyncio.create_task(coroutine))
        self.running = {}
        self.authorities = {}
        self.started = set()

    def bridge_active(self, generation):
        return bool(generation) and any(authority.identity is not None
            and authority.identity.pairing_id == generation and operation_id in self.running
            and not self.running[operation_id].done() for operation_id, authority in self.authorities.items())

    async def stop_bridge(self, generation):
        if not generation:
            return
        pending = []
        for operation_id, authority in tuple(self.authorities.items()):
            task = self.running.get(operation_id)
            if (authority.identity is not None and authority.identity.pairing_id == generation
                    and task is not None and not task.done()):
                if not task.cancelling():
                    task.cancel()
                pending.append((operation_id, authority, task, operation_id in self.started))
        if pending:
            await asyncio.gather(*(task for _, _, task, _ in pending), return_exceptions=True)
            for operation_id, authority, _, started in pending:
                # A task cancelled before its coroutine's first instruction
                # cannot run execute()'s cancellation handler.
                current = self.record(self.binding(authority), operation_id)
                if current.state in {"preparing", "applying"}:
                    state = ("needs_input" if not started and current.input_step is not None else
                        "blocked" if current.phase in PRE_CONFIRMATION_PHASES else "needs_reconcile")
                    self.save(current, state=state, reason_code="device_setup_interrupted")
            await self.store.durable()

    def private_hash(self, value):
        return hmac.new(bytes.fromhex(self.store.setting("csrf_secret")), canonical(value).encode(),
                        hashlib.sha256).hexdigest()

    def binding(self, authority):
        return self.private_hash([KIND, authority.owner, *authority.connection])

    def home_binding(self, authority):
        _token, base, websocket = authority.connection
        return self.private_hash([KIND, "household-history", authority.owner, base, websocket])

    def adopt_legacy_scope(self, binding, home_binding):
        # A one-way credential hash cannot prove which home an old record
        # belonged to after rotation. Only its original verified binding can
        # supply the missing access scope; never guess from names/flow IDs.
        for record in self.records(binding):
            if record.home_binding is None:
                self.save(record, home_binding=home_binding)

    async def authorize(self, request):
        authority = DiscoveryAuthority(self.app, self.store, self.ha, request, self.host_session)
        await authority.check_owner()
        self.prune()
        self.adopt_legacy_scope(self.binding(authority), self.home_binding(authority))
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

    def records(self, binding, home_binding=None, *, for_write=False):
        result = []
        for row in self.store.db.execute("SELECT body FROM records WHERE kind=? AND "
                "(json_extract(body,'$.binding')=? OR json_extract(body,'$.home_binding')=?) "
                "ORDER BY created DESC,id DESC", (KIND, binding, home_binding)):
            try:
                result.append(SetupRecord.model_validate_json(row[0]))
            except ValidationError:
                if for_write:
                    raise HTTPException(409, "device_setup_record_contract_changed") from None
                continue  # Future records do not grant authority to old binaries.
        return result

    def record(self, binding, identifier, home_binding=None):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise HTTPException(404, "device_setup_not_found")
        raw = self.store.get(KIND, identifier)
        try:
            record = SetupRecord.model_validate(raw)
        except ValidationError:
            raise HTTPException(404, "device_setup_not_found") from None
        if record.binding != binding and (home_binding is None or record.home_binding != home_binding):
            raise HTTPException(404, "device_setup_not_found")
        return record

    def save(self, record, **updates):
        # Exhausted input must not retain a form or fence a fresh proposal.
        if (updates.get("state") == "needs_input" and record.candidate.integration_domain == "synology_dsm"
                and updates.get("step_posts", record.step_posts) >= 8):
            updates.update(state="blocked", reason_code="device_setup_input_limit", input_step=None,
                input_error=None, share_options=[], backup_path=None)
        # Validation catches unknown or oversized upstream identifiers before persistence.
        value = SetupRecord.model_validate({**record.model_dump(),
            "version": ADAPTERS[record.candidate.integration_domain].record_version,
            "revision": record.revision + 1, "updated_at": utcnow(), **updates})
        self.store.put(KIND, record_body(value))
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
        for raw in self.store.list(KIND, 100000):
            try:
                record = SetupRecord.model_validate(raw)
            except ValidationError:
                continue
            redacted = ADAPTERS[record.candidate.integration_domain].label + "（名稱已依保存設定移除）"
            if record.created_at < cutoff and record.candidate.title != redacted:
                self.save(record, candidate=record.candidate.model_copy(update={"title": redacted}))

    def receipt(self, request, binding, action, body, identifier=""):
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise HTTPException(400, "device_setup_idempotency_required")
        value = body.model_dump()
        if isinstance(body, DeviceSetupApprove):
            value.pop("credentials")
            if "credentials" in body.model_fields_set:
                value["credentials_present"] = body.credentials is not None
        elif isinstance(body, DeviceSetupInput):
            value = body.root.model_dump(exclude={"username", "password", "otp_code"})
            value["credentials_present"] = isinstance(body.root, (DeviceSetupLinkInput, DeviceSetupOTPInput))
        scope, payload = f"{KIND}:{binding}:{action}:{identifier}", digest(value)
        row = self.store.db.execute("SELECT digest,result FROM requests WHERE scope=? AND key=?",
                                    (scope, key)).fetchone()
        if row and row[0] != payload:
            raise HTTPException(409, "device_setup_request_conflict")
        return (scope, key, payload), row[1] if row else None

    def commit_receipt(self, record, receipt):
        # A storage-only migration never changes approval identity or resends
        # work. It also marks new candidate fields as incompatible with v1.
        record = SetupRecord.model_validate(record.model_copy(update={
            "version": ADAPTERS[record.candidate.integration_domain].record_version}).model_dump())
        with self.store.transaction() as tx:
            tx.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE "
                "SET body=excluded.body,updated=excluded.updated", (KIND, record.id,
                canonical(record_body(record)), record.created_at, record.updated_at))
            tx.execute("INSERT INTO requests VALUES (?,?,?,?)", (*receipt, record.id))

    async def read(self, authority, method, target):
        self.guard(authority)
        try:
            result = await (self.ha.ws(target) if method == "ws" else self.ha.request("GET", target))
        except HAError as exc:
            raise HTTPException(503, "device_setup_source_unavailable") from exc
        self.guard(authority)
        return result

    async def current_flow(self, authority, candidate_id, domain="ipp"):
        adapter = ADAPTERS[domain]
        config = await self.read(authority, "rest", "/config")
        if (not isinstance(config, dict) or not isinstance(config.get("version"), str)
                or config["version"] not in SUPPORTED_CORE):
            raise HTTPException(409, "device_setup_core_unsupported")
        flows = await self.read(authority, "ws", "config_entries/flow/progress")
        if not isinstance(flows, list) or len(flows) > 256:
            raise HTTPException(409, "device_setup_flow_contract_changed")
        matches = [row for row in flows if isinstance(row, dict) and row.get("flow_id") == candidate_id]
        if len(matches) != 1:
            raise HTTPException(409, "device_setup_candidate_changed")
        flow = matches[0]
        context = flow.get("context")
        if (flow.get("handler") != adapter.domain or flow.get("step_id") != adapter.step
                or not isinstance(context, dict) or not isinstance(context.get("source"), str)
                or context.get("source") not in adapter.sources):
            raise HTTPException(409, "device_setup_flow_unsupported")
        try:
            identity = adapter.identity(context.get("unique_id"))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if len(canonical(flow)) > 32768:
            raise HTTPException(409, "device_setup_flow_contract_changed")
        # GET invokes the step with None. Only the exact allowlisted native
        # confirmation may be read; other steps can have effects even on GET.
        form = await self.read(authority, "rest", "/config/config_entries/flow/" + identifier(candidate_id))
        if (not isinstance(form, dict) or form.get("type") != "form" or form.get("flow_id") != candidate_id
                or form.get("handler") != adapter.domain or form.get("step_id") != adapter.step
                or form.get("errors") != {}):
            raise HTTPException(409, "device_setup_flow_unsupported")
        try:
            adapter.validate_form(form)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        await authority.check_owner()
        self.guard(authority)
        try:
            candidate = DiscoveryCandidate.model_validate(project("ha_discovery", flow))
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, "device_setup_flow_contract_changed") from exc
        if adapter.domain == "synology_dsm":
            # Even proposal GETs cross the secret-bearing response boundary.
            form = adapter.response(form, candidate_id)
        elif len(canonical(form)) > 32768:
            raise HTTPException(409, "device_setup_flow_unsupported")
        return candidate, self.private_hash(flow), self.private_hash(form), self.private_hash(identity)

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
        _, flow_hash, form_hash, identity_hash = await self.current_flow(authority, record.candidate.id,
                                                                       record.candidate.integration_domain)
        if (flow_hash, form_hash, identity_hash) != (record.flow_digest, record.form_digest, record.identity_digest):
            raise HTTPException(409, "device_setup_candidate_changed")

    def view(self, record, binding=None):
        result = DeviceSetupView(**record.model_dump(include={"id", "revision", "proposal_digest", "candidate", "candidate_aliases",
            "state", "reason_code", "created_at", "updated_at", "expires_at", "backup_id"}),
            can_approve=record.state == "proposed" and time.time() < record.expires_at
                and (binding is None or record.binding == binding),
            can_reconcile=record.state in {"needs_reconcile", "configured", "needs_input"},
            message=ADAPTERS[record.candidate.integration_domain].proposal_message if record.state == "proposed"
                else MESSAGES.get(record.state, ""))
        if record.state == "needs_input":
            expired = time.time() >= record.expires_at
            limited = record.step_posts >= 8
            result.input = DeviceSetupInputView(step=record.input_step, error=record.input_error,
                options=record.share_options if record.input_step == "backup_share" else [],
                can_submit=record.reason_code is None and not expired and not limited
                    and (binding is None or record.binding == binding))
            result.message = INPUT_MESSAGES["device_setup_input_limit" if limited else
                "device_setup_input_expired" if expired else "device_setup_input_unavailable"
                if record.reason_code is not None else record.input_error or record.input_step]
        elif (record.state == "blocked" and record.candidate.integration_domain == "synology_dsm"
                and record.reason_code in INPUT_MESSAGES):
            result.message = INPUT_MESSAGES[record.reason_code]
        if binding is not None and record.binding != binding and record.state == "proposed":
            result.reason_code = "device_setup_credentials_changed"
            result.message = "Home Assistant 登入授權已更新；請重新預覽接入提案，舊批准不會沿用。"
        return result

    async def create(self, request, body):
        authority, binding = await self.authorize(request)
        home_binding = self.home_binding(authority)
        receipt, previous = self.receipt(request, binding, "create", body)
        if previous:
            return self.view(self.record(binding, previous, home_binding), binding)
        candidate, flow_hash, form_hash, identity_hash = await self.current_flow(authority, body.candidate_id,
                                                                               body.integration_domain)
        entries = await self.entries(authority)
        await authority.check_owner()
        self.guard(authority)
        receipt, previous = self.receipt(request, binding, "create", body)
        if previous:
            return self.view(self.record(binding, previous, home_binding), binding)
        # Older records lack a provable household scope after token rotation.
        # Do not expose them or silently step around their uncertain effects.
        legacy_fence = self.store.db.execute("SELECT 1 FROM records WHERE kind=? AND "
            "json_extract(body,'$.home_binding') IS NULL AND json_extract(body,'$.identity_digest')=? AND "
            "json_extract(body,'$.state') IN ('preparing','applying','needs_reconcile') LIMIT 1",
            (KIND, identity_hash)).fetchone()
        if legacy_fence:
            raise HTTPException(409, "device_setup_history_authority_unavailable")
        records = self.records(binding, home_binding, for_write=True)
        for index, row in enumerate(records):
            if row.identity_digest == identity_hash and row.binding != binding and row.state == "proposed":
                records[index] = self.save(row, state="blocked", reason_code="device_setup_credentials_changed")
        current_entries = {row["entry_id"] for row in entries}
        def previously_completed_and_removed(row):
            return (row.phase == "registration_readback" and row.entry_id is not None
                    and row.entry_id not in current_entries and row.candidate.id != candidate.id)
        # An uncertain earlier attempt must not be bypassed using a new key or
        # a rediscovered flow ID. Domain-bound stable private identity binds both.
        existing = next((row for row in records if row.identity_digest == identity_hash and
            row.state != "blocked" and not previously_completed_and_removed(row)
            and (row.state != "proposed" or row.expires_at > time.time())), None)
        if existing:
            if candidate.id != existing.candidate.id and candidate.id not in existing.candidate_aliases:
                if len(existing.candidate_aliases) >= 16:
                    raise HTTPException(409, "device_setup_alias_limit_reached")
                existing = self.save(existing, candidate_aliases=[*existing.candidate_aliases, candidate.id])
            self.commit_receipt(existing, receipt)
            return self.view(existing, binding)
        if len(records) >= 100:
            raise HTTPException(409, "device_setup_limit_reached")
        now = utcnow()
        proposal = dict(id=uuid.uuid4().hex, binding=binding, home_binding=home_binding, candidate=candidate.model_dump(),
            flow_digest=flow_hash, form_digest=form_hash, identity_digest=identity_hash,
            baseline_entry_ids=sorted(row["entry_id"] for row in entries), created_at=now, updated_at=now,
            expires_at=time.time() + 600)
        adapter = ADAPTERS[candidate.integration_domain]
        proposal["version"] = adapter.record_version
        record = SetupRecord(**proposal, proposal_digest=digest(proposal))
        self.commit_receipt(record, receipt)
        return self.view(record, binding)

    async def approve(self, request, identifier, body):
        authority, binding = await self.authorize(request)
        home_binding = self.home_binding(authority)
        receipt, previous = self.receipt(request, binding, "approve", body, identifier)
        if previous:
            return self.view(self.record(binding, previous, home_binding), binding)
        record = self.record(binding, identifier, home_binding)
        if record.candidate.integration_domain == "synology_dsm":
            if body.credentials is None:
                raise HTTPException(409, "device_setup_credentials_required")
        elif "credentials" in body.model_fields_set:
            raise HTTPException(409, "device_setup_credentials_unexpected")
        if record.binding != binding:
            raise HTTPException(409, "device_setup_credentials_changed")
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
        self.start(authority, record, self.execute(authority, record, body.credentials))
        return self.view(record, binding)

    def start(self, authority, record, coroutine):
        # Keep authentication metadata, never the HTTP request's cached secret body.
        authority.request = Request(dict(authority.request.scope))
        task = self.spawn("device-setup:" + record.id, coroutine)
        self.running[record.id] = task
        self.authorities[record.id] = authority
        def finished(done):
            if self.running.get(record.id) is done:
                self.running.pop(record.id, None)
                self.authorities.pop(record.id, None)
                self.started.discard(record.id)
        task.add_done_callback(finished)

    @staticmethod
    def secret_payload(body):
        if isinstance(body, DeviceSetupOTPInput):
            return {"otp_code": body.otp_code.get_secret_value()}
        return {"username": body.username.get_secret_value(), "password": body.password.get_secret_value()}

    async def input(self, request, identifier, body):
        authority, binding = await self.authorize(request)
        receipt, previous = self.receipt(request, binding, "input", body, identifier)
        if previous:
            return self.view(self.record(binding, previous, self.home_binding(authority)), binding)
        record = self.record(binding, identifier, self.home_binding(authority))
        step = body.root
        if record.revision != step.expected_revision:
            raise HTTPException(409, "device_setup_revision_changed")
        if (record.candidate.integration_domain != "synology_dsm" or record.state != "needs_input"
                or record.input_step != step.step or record.reason_code is not None or identifier in self.running):
            raise HTTPException(409, "device_setup_input_unavailable")
        if time.time() >= record.expires_at:
            raise HTTPException(409, "device_setup_input_expired")
        if record.step_posts >= 8:
            raise HTTPException(409, "device_setup_input_limit")
        if record.binding != binding:
            raise HTTPException(409, "device_setup_credentials_changed")
        if isinstance(step, DeviceSetupShareInput) and step.backup_share not in {row.value for row in record.share_options}:
            raise HTTPException(409, "device_setup_input_invalid")
        self.write_guard(authority)
        record = self.save(record, state="applying", phase="confirmation_prepared", reason_code=None,
            post_step=step.step, step_posts=record.step_posts + 1)
        self.commit_receipt(record, receipt)
        await self.store.durable()
        self.start(authority, record, self.execute_input(authority, record, step))
        return self.view(record, binding)

    async def synology_progress(self, authority, record):
        config = await self.read(authority, "rest", "/config")
        if (not isinstance(config, dict) or not isinstance(config.get("version"), str)
                or config.get("version") not in SUPPORTED_CORE):
            raise HTTPException(409, "device_setup_core_unsupported")
        rows = await self.read(authority, "ws", "config_entries/flow/progress")
        if not isinstance(rows, list) or len(rows) > 256:
            raise HTTPException(409, "device_setup_flow_contract_changed")
        matches = [row for row in rows if isinstance(row, dict) and row.get("flow_id") == record.candidate.id]
        if not matches:
            return None
        if len(matches) != 1:
            raise HTTPException(409, "device_setup_flow_contract_changed")
        row = matches[0]
        context = row.get("context")
        adapter = ADAPTERS[record.candidate.integration_domain]
        if (row.get("handler") != adapter.domain or not isinstance(context, dict)
                or not isinstance(context.get("source"), str)
                or context.get("source") not in adapter.sources
                or context.get("source") != record.candidate.discovery_method):
            raise HTTPException(409, "device_setup_flow_unsupported")
        try:
            identity = adapter.identity(context.get("unique_id"))
        except ValueError:
            raise HTTPException(409, "device_setup_candidate_changed") from None
        if self.private_hash(identity) != record.identity_digest:
            raise HTTPException(409, "device_setup_candidate_changed")
        return row.get("step_id")

    async def execute_input(self, authority, record, body):
        self.started.add(record.id)
        mutated = False
        transport_ready = False
        def local_guard():
            self.write_guard(authority)
            current = self.record(record.binding, record.id)
            if (current.revision != record.revision or current.proposal_digest != record.proposal_digest
                    or current.state != "applying" or time.time() >= current.expires_at):
                raise HTTPException(409, "device_setup_approval_changed")

        async def send_guard():
            try:
                local_guard()
                if await self.synology_progress(authority, record) != record.post_step:
                    raise HTTPException(409, "device_setup_candidate_changed")
                entries = await self.entries(authority)
                if any(row.get("domain") == "synology_dsm" and row["entry_id"] not in record.baseline_entry_ids
                       for row in entries):
                    raise HTTPException(409, "device_setup_integrations_changed")
                await authority.check_owner()
                local_guard()
            except HTTPException as exc:
                raise SendGuardRejected(exc.status_code, exc.detail) from None
        async def transport_guard():
            nonlocal transport_ready
            await send_guard()
            transport_ready = True
        try:
            async with self.writer:
                await send_guard()
                # The request handler already made confirmation_prepared durable.
                mutated = True
                try:
                    payload = ({"backup_share": body.backup_share, "backup_path": record.backup_path}
                               if isinstance(body, DeviceSetupShareInput) else self.secret_payload(body))
                    raw = await self.ha.request("POST", "/config/config_entries/flow/" + record.candidate.id,
                                                payload, send_guard=transport_guard)
                finally:
                    if "payload" in locals():
                        payload.clear()
                    body = None
                result = ADAPTERS["synology_dsm"].response(raw, record.candidate.id)
                raw = None
                await self.synology_result(authority, record, result)
        except asyncio.CancelledError:
            mutated = transport_ready
            current = self.record(record.binding, record.id)
            if current.state == "applying":
                self.save(current, state="needs_reconcile" if mutated else "needs_input",
                          reason_code="device_setup_outcome_unknown" if mutated else "device_setup_interrupted")
            raise
        except (HTTPException, HAError, ValueError, TypeError, OSError) as exc:
            if isinstance(exc, SendGuardRejected):
                mutated = False
            current = self.record(record.binding, record.id)
            if current.state == "applying":
                self.save(current, state="needs_reconcile" if mutated else "needs_input",
                    reason_code="device_setup_outcome_unknown" if mutated else
                        exc.detail if isinstance(exc, HTTPException) else "device_setup_preparation_failed")
        finally:
            body = None
        await self.store.durable()

    def needs_input(self, record, result):
        return self.save(record, state="needs_input", reason_code=None, input_step=result["step_id"],
            input_error=result.get("error"), share_options=result.get("options", []),
            backup_path=result.get("backup_path"), expires_at=time.time() + 1800, phase="input_required")

    async def synology_result(self, authority, record, result):
        if result["type"] == "form":
            self.needs_input(record, result)
        elif result["type"] == "abort":
            reason = {"already_configured": "device_setup_already_configured",
                      "reconfigure_successful": "device_setup_existing_updated"}
            self.save(record, state="blocked", reason_code=reason.get(result["reason"], "device_setup_flow_aborted"))
        elif result["type"] == "create_entry":
            entry = result["result"]
            if entry["domain"] != "synology_dsm" or entry["source"] != record.candidate.discovery_method:
                raise ValueError("unconfirmed_entry")
            entry_id = identifier(entry["entry_id"])
            if entry_id in record.baseline_entry_ids:
                raise ValueError("preexisting_entry")
            record = self.save(record, entry_id=entry_id, phase="confirmation_acknowledged")
            await self.store.durable()
            await authority.check_owner()
            self.guard(authority)
            await self.reconcile_record(authority, record)
        else:
            raise ValueError("unconfirmed_response")
        await self.store.durable()

    async def execute(self, authority, record, credentials=None):
        self.started.add(record.id)
        try:
            operation = self._execute(authority, record, credentials)
            credentials = None
            await operation
        except asyncio.CancelledError:
            # Covers cancellation while waiting for the shared household writer,
            # before _execute enters its effect-aware exception handling.
            current = self.record(record.binding, record.id)
            if current.state in {"preparing", "applying"}:
                self.save(current, state="needs_reconcile" if current.state == "applying" else "blocked",
                          reason_code="device_setup_interrupted")
            raise
        finally:
            credentials = None

    async def _execute(self, authority, record, credentials=None):
        mutated = False
        adapter = ADAPTERS[record.candidate.integration_domain]
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
                backup = await self.ha.backup("Agentic Home " + adapter.backup_label + " " + record.id[:12], send_guard=send_guard)
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
                updates = {"post_step": "link", "step_posts": 1} if adapter.domain == "synology_dsm" else {}
                record = self.save(record, state="applying", phase="confirmation_prepared", **updates)
                await self.store.durable()
                await send_guard()
                # Intent and backup are durable before the only configuration
                # write. Mark uncertain before awaiting transport, including
                # cancellation and failures after HA accepted the body.
                mutated = True
                try:
                    payload = self.secret_payload(credentials) if credentials is not None else {}
                    result = await self.ha.request("POST", "/config/config_entries/flow/" + record.candidate.id,
                                                   payload, send_guard=send_guard)
                finally:
                    if "payload" in locals():
                        payload.clear()
                    credentials = None
                if adapter.domain == "synology_dsm":
                    result = adapter.response(result, record.candidate.id)
                    await self.synology_result(authority, record, result)
                    return
                if (not isinstance(result, dict) or result.get("type") != "create_entry"
                        or result.get("flow_id") != record.candidate.id or result.get("handler") != adapter.domain):
                    raise ValueError("unconfirmed_response")
                entry = result.get("result")
                if (not isinstance(entry, dict) or entry.get("domain") != adapter.domain
                        or entry.get("source") != record.candidate.discovery_method
                        or entry.get("source") not in adapter.sources):
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
                current = self.record(record.binding, record.id)
                if current.state in {"preparing", "applying"}:
                    self.save(current, state="needs_reconcile" if mutated else "blocked",
                              reason_code="device_setup_outcome_unknown" if mutated and adapter.domain == "synology_dsm"
                                  else "device_setup_interrupted")
                raise
            except (HTTPException, HAError, ValueError, TypeError, OSError) as exc:
                if isinstance(exc, SendGuardRejected):
                    mutated = False  # Both REST hooks and WS guards run before sending.
                reason = (exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str)
                          and exc.detail.startswith("device_setup_") else "device_setup_preparation_failed")
                current = self.record(record.binding, record.id)
                if current.state in {"preparing", "applying"}:
                    self.save(current, state="needs_reconcile" if mutated else "blocked",
                              reason_code="device_setup_outcome_unknown" if mutated else reason)
            # Disk failures deliberately propagate: no more effects or response
            # claims are safe after a failed journal. Recovery reads its last
            # durable intent and never resends the confirmation.
            await self.store.durable()

    async def reconcile_record(self, authority, record):
        adapter = ADAPTERS[record.candidate.integration_domain]
        if record.phase in PRE_CONFIRMATION_PHASES and record.entry_id is None:
            await authority.check_owner()
            self.guard(authority)
            return self.save(record, state="blocked", reason_code="device_setup_interrupted")
        if adapter.domain == "synology_dsm":
            step = await self.synology_progress(authority, record)
            if step is not None and record.entry_id is None:
                if step not in {"link", "2sa", "backup_share"}:
                    return self.save(record, state="blocked", reason_code="device_setup_flow_unsupported")
                entries = await self.entries(authority)
                if any(row.get("domain") == adapter.domain and row["entry_id"] not in record.baseline_entry_ids
                       for row in entries):
                    return self.save(record, state="needs_reconcile", reason_code="device_setup_entry_not_verified")
                result = {"step_id": step}
                if step != "2sa":
                    raw = await self.read(authority, "rest", "/config/config_entries/flow/" + record.candidate.id)
                    try:
                        result = adapter.response(raw, record.candidate.id)
                        raw = None
                        if result["type"] != "form" or result["step_id"] != step:
                            raise ValueError("unconfirmed_step")
                    except (ValueError, TypeError):
                        return self.save(record, state="needs_reconcile", reason_code="device_setup_outcome_unknown")
                await authority.check_owner()
                self.guard(authority)
                return self.needs_input(record, result)
        entries = await self.entries(authority)
        selected = next((row for row in entries if row.get("entry_id") == record.entry_id
                         and row.get("domain") == adapter.domain and row.get("source") == record.candidate.discovery_method
                         and row.get("source") in adapter.sources), None) if record.entry_id else None
        if (adapter.domain == "synology_dsm" or not record.entry_id
                and record.phase in {"confirmation_prepared", "confirmation_acknowledged"}):
            # DSM login resolves the serial again; an acknowledged entry alone
            # does not bind it to the NAS MAC observed during discovery.
            selected = None
            devices = await self.read(authority, "ws", "config/device_registry/list")
            if not isinstance(devices, list) or len(devices) > 1000:
                raise HTTPException(503, "device_setup_source_incomplete")
            matches = set()
            for device in devices:
                if not isinstance(device, dict):
                    raise HTTPException(503, "device_setup_source_incomplete")
                ids = device.get("connections" if adapter.domain == "synology_dsm" else "identifiers", [])
                if not isinstance(ids, list):
                    raise HTTPException(503, "device_setup_source_incomplete")
                matches_identity = False
                for value in ids:
                    if (isinstance(value, list) and len(value) == 2
                            and value[0] == ("mac" if adapter.domain == "synology_dsm" else adapter.domain)):
                        try:
                            mac = value[1]
                            if adapter.domain == "synology_dsm" and isinstance(mac, str):
                                mac = mac.replace(":", "").replace("-", "").lower()
                            identity = adapter.identity(mac)
                        except ValueError:
                            continue
                        matches_identity |= self.private_hash(identity) == record.identity_digest
                if matches_identity:
                    associated = device.get("config_entries")
                    if not isinstance(associated, list):
                        raise HTTPException(503, "device_setup_source_incomplete")
                    matches.update(row["entry_id"] for row in entries if row["entry_id"] in associated
                        and (record.entry_id is None or row["entry_id"] == record.entry_id)
                        and row["entry_id"] not in record.baseline_entry_ids and row.get("domain") == adapter.domain
                        and row.get("source") == record.candidate.discovery_method and row.get("source") in adapter.sources)
            if len(matches) == 1:
                selected = next(row for row in entries if row["entry_id"] in matches)
        await authority.check_owner()
        self.guard(authority)
        if selected:
            # Readback is exact registration, not proof it loaded or printed.
            project("ha_integrations", selected)
            return self.save(record, entry_id=selected["entry_id"], state="configured", reason_code=None,
                             phase="registration_readback")
        if adapter.domain == "synology_dsm" and not any(row.get("domain") == adapter.domain
                and row["entry_id"] not in record.baseline_entry_ids for row in entries):
            return self.save(record, state="blocked", reason_code="device_setup_flow_gone")
        return self.save(record, state="needs_reconcile", reason_code="device_setup_entry_not_verified")

    async def detail(self, request, identifier):
        authority, binding = await self.authorize(request)
        home_binding = self.home_binding(authority)
        record = self.record(binding, identifier, home_binding)
        result = await self.readback(authority, record)
        async def guard():
            await authority.check_owner()
            if self.record(binding, identifier, home_binding).revision != record.revision:
                raise HTTPException(409, "device_setup_revision_changed")
        request.scope[RESPONSE_GUARD] = guard
        return result

    async def readback(self, authority, record):
        """Fresh selected-entry observations; caller supplies captured authority.

        This helper performs no adoption, pruning, reconciliation or HA writes.
        Both HTTP and current-message chat use the same exact association rules.
        """
        binding, home_binding = self.binding(authority), self.home_binding(authority)
        identifier = record.id
        if self.record(binding, identifier, home_binding).revision != record.revision:
            raise HTTPException(409, "device_setup_revision_changed")
        result = self.view(record, binding)
        if record.entry_id:
            observed = await snapshot(self.ha)
            await authority.check_owner()
            self.guard(authority)
            if self.record(binding, identifier, home_binding).revision != record.revision:
                raise HTTPException(409, "device_setup_revision_changed")
            sources = {item.id: item for item in observed.sources}
            selected = next((item for item in observed.integrations if item.id == record.entry_id
                             and item.domain == record.candidate.integration_domain), None)
            devices = [item.model_copy(update={"integration_ids": [record.entry_id]}) for item in observed.devices
                       if selected and record.entry_id in item.integration_ids]
            complete = selected is not None and sources["ha_devices"].state == "ok" and not sources["ha_devices"].truncated
            entities = complete and sources["ha_entities"].state == "ok" and not sources["ha_entities"].truncated
            states = entities and sources["ha_states"].state == "ok" and not sources["ha_states"].truncated
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
        await authority.check_owner()
        self.guard(authority)
        if self.record(binding, identifier, home_binding).revision != record.revision:
            raise HTTPException(409, "device_setup_revision_changed")
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
        authority, binding = await self.authorize(request)
        return DeviceSetupList(setups=[self.view(record, binding)
            for record in self.records(binding, self.home_binding(authority))[:100]])

    async def reconcile(self, request, identifier, body):
        authority, binding = await self.authorize(request)
        home_binding = self.home_binding(authority)
        async with self.writer:
            receipt, previous = self.receipt(request, binding, "reconcile", body, identifier)
            if previous:
                return self.view(self.record(binding, previous, home_binding), binding)
            record = self.record(binding, identifier, home_binding)
            if record.revision != body.expected_revision:
                raise HTTPException(409, "device_setup_revision_changed")
            if record.state not in {"configured", "needs_reconcile", "needs_input"} or identifier in self.running:
                raise HTTPException(409, "device_setup_reconcile_unavailable")
            updated = await self.reconcile_record(authority, record)
            self.commit_receipt(updated, receipt)
            return self.view(updated, binding)


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

    @app.post("/api/device-setups/{identifier}/input", response_model=DeviceSetupView, status_code=202)
    async def input(identifier: str, body: DeviceSetupInput, request: Request):
        return await service.input(request, identifier, body)

    return service
