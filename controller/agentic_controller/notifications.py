"""Owner-bound native HA notifications, independent of conversations and Agents.

HA grants a claim once. A lost claim/send reply is terminal uncertainty, never a
retry instruction. Only sanitized receipts leave the private outbox.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import Field, model_validator

from .channels import line_account_scope
from .durability import durable
from .ha import HAError
from .models import Channel, ChannelConfig, Strict, digest, utcnow

DOMAIN = "agentic_home_notifications"
RECORD_KIND = "notification_delivery"
CONFIG_KEY = "notifications"
CHANNELS = ("line", "telegram", "discord")
TERMINAL = frozenset({"submitted", "unknown", "not_sent"})
POLL_SECONDS = 10
MAX_RECORDS = 96  # 32 in-flight requests plus 64 recent receipts.
HexId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Alias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
Reason = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
DeliveryState = Literal["claiming", "sending", "submitted", "unknown", "not_sent"]


class NotificationRecipient(Strict):
    alias: Alias
    label: Annotated[str, Field(min_length=1, max_length=120)]
    channel: Channel
    chat: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


class NotificationSettings(Strict):
    enabled: bool = False
    recipients: Annotated[list[NotificationRecipient], Field(max_length=16)] = []

    @model_validator(mode="after")
    def unique_aliases(self):
        if len({item.alias for item in self.recipients}) != len(self.recipients):
            raise ValueError("duplicate_alias")
        return self


class NotificationConfigInput(NotificationSettings):
    expected_revision: Hash


class NotificationConfig(NotificationSettings):
    revision: Hash


class NotificationChannelStatus(Strict):
    channel: Channel
    transport: Literal["connected", "connecting", "disconnected", "error", "disabled"]
    login: Literal["ready", "needs_login", "unknown", "not_required"]


class NotificationDestination(Strict):
    channel: Channel
    chat: str
    label: str


class NotificationDelivery(Strict):
    id: Hash
    recipient: Alias
    channel: Channel
    state: DeliveryState
    created_at: str
    updated_at: str
    reason: Reason | None = None


class NotificationIntegration(Strict):
    state: Literal["disabled", "not_installed", "unavailable", "ready", "needs_rebind"]
    source_id: HexId | None = None
    last_seen: str | None = None
    reason: Reason | None = None


class NotificationsSnapshot(Strict):
    config: NotificationConfig
    integration: NotificationIntegration
    channels: Annotated[list[NotificationChannelStatus], Field(max_length=3)]
    destinations: Annotated[list[NotificationDestination], Field(max_length=60)]
    deliveries: Annotated[list[NotificationDelivery], Field(max_length=64)]


class NotificationRequest(Strict):
    id: HexId
    source_id: HexId
    epoch: HexId
    routing_revision: Hash
    recipient: Alias
    kind: Literal["text"]
    message: Annotated[str, Field(min_length=1, max_length=1900)]
    created_at: str
    expires_at: str
    digest: Hash

    @model_validator(mode="after")
    def immutable(self):
        created, expires = instant(self.created_at), instant(self.expires_at)
        if not 0 < (expires - created).total_seconds() <= 3600:
            raise ValueError("invalid_lifetime")
        if digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("invalid_digest")
        return self


class AliasSummary(Strict):
    alias: Alias
    label: Annotated[str, Field(min_length=1, max_length=120)]
    channel: Channel
    capabilities: list[Literal["text"]]


class NativeReceipt(Strict):
    request_id: HexId
    recipient: Alias
    state: Literal["submitted", "unknown", "not_sent"]
    reason: Reason | None
    updated_at: str


class NativeStatus(Strict):
    protocol: Literal[1]
    source_id: HexId
    epoch: HexId
    controller_id: HexId | None
    enabled: bool
    routing_revision: Annotated[str, Field(pattern=r"^(?:[a-f0-9]{64})?$")]
    aliases: Annotated[list[AliasSummary], Field(max_length=16)]
    pending: Annotated[int, Field(ge=0, le=32)]
    last_delivery: NativeReceipt | None


class NativePull(Strict):
    requests: Annotated[list[NotificationRequest], Field(max_length=8)]


class NativeClaim(Strict):
    granted: bool
    request: NotificationRequest | None


class NativeAck(Strict):
    acknowledged: bool


class NotificationRecord(NotificationDelivery):
    request_id: HexId
    source_id: HexId
    epoch: HexId
    claim_id: HexId
    request_digest: Hash
    routing_revision: Hash
    authority: Hash
    expires_at: str
    chat: str | None
    message: str | None
    acknowledged: bool = False


class NotificationRevoked(Exception):
    """The last pre-effect authorization check failed; no send was attempted."""


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("utc_required")
    return parsed


def prune_notifications(tx, context_cutoff):
    # Unexpired tombstones remain even under a newly shortened retention policy.
    # Once expired, the HA request can no longer be imported by any new process.
    tx.execute("DELETE FROM records WHERE kind=? AND created < ? "
        "AND json_extract(body,'$.state') IN ('submitted','unknown','not_sent') "
        "AND julianday(json_extract(body,'$.expires_at')) <= julianday(?)",
        (RECORD_KIND, context_cutoff, utcnow()))


class NotificationManager:
    def __init__(self, store, ha, gateway, controller_id):
        self.store, self.ha, self.gateway = store, ha, gateway
        self.controller_id = controller_id
        self.integration = NotificationIntegration(state="disabled")
        self._lock, self._task = asyncio.Lock(), None
        self._storage_error = False
        self._heartbeat_at = None

    def settings(self):
        return NotificationSettings.model_validate(self.store.setting(CONFIG_KEY, {}))

    def authority(self):
        # Credentials are only inputs to this private digest, never receipts.
        return digest([self.controller_id, self.store.setting("owner"),
            getattr(self.ha, "token", None), getattr(self.ha, "base", None),
            getattr(self.ha, "websocket", None), self.store.setting("updater_gate")])

    def revision(self):
        try:
            settings = self.settings()
        except ValueError:
            return digest(["invalid_settings", self.store.setting(CONFIG_KEY)])
        # Native home notifications are independent of AI pause, model logins,
        # conversation scope and household planning policy.
        return digest([settings.model_dump(), self.store.setting("notifications_generation"),
                       self.authority(), [self.gateway.config(name) for name in CHANNELS],
                       line_account_scope(self.store)])

    def channel_states(self):
        result = []
        for name in CHANNELS:
            enabled = self.gateway.config(name).get("enabled", False)
            state = self.gateway.states.get(name, "connecting" if enabled else "disabled")
            transport = ("disabled" if not enabled else state if state in
                {"connected", "connecting", "disconnected"} else "error")
            if name == "line" and enabled:
                socket = getattr(self.gateway.transports.get(name), "socket", None)
                if socket is not None and not socket.closed:
                    transport = "connected"
            login = ("ready" if state == "connected" and enabled else "needs_login" if state in
                {"needs_reauth", "invalid_token"} else "unknown")
            if name == "line" and login == "ready" and not self._adapter_ready(
                    name, self.gateway.transports.get(name)):
                login = "unknown"
            result.append(NotificationChannelStatus(channel=name, transport=transport, login=login))
        return result

    def snapshot(self):
        try:
            settings = self.settings()
        except ValueError:
            settings = NotificationSettings()
            self._storage_error = True
        integration = self.integration.model_copy(deep=True)
        if self._storage_error:
            integration = NotificationIntegration(state="unavailable", reason="storage_invalid")
        elif not settings.enabled:
            integration.state = "disabled"
        elif integration.state == "ready" and (self._heartbeat_at is None or
                                                monotonic() - self._heartbeat_at >= 60):
            integration.state, integration.reason = "unavailable", "stale_heartbeat"
        deliveries = []
        for raw in self.store.list(RECORD_KIND, MAX_RECORDS):
            try:
                record = NotificationRecord.model_validate(raw)
            except ValueError:
                continue
            deliveries.append(NotificationDelivery.model_validate(
                record.model_dump(include=set(NotificationDelivery.model_fields))))
            if len(deliveries) == 64:
                break
        return NotificationsSnapshot(config=NotificationConfig(**settings.model_dump(), revision=self.revision()),
            integration=integration, channels=self.channel_states(),
            destinations=[NotificationDestination(channel=name, chat=chat, label=chat)
                for name in CHANNELS for chat in self.gateway.config(name).get("chats", [])],
            deliveries=deliveries)

    async def configure(self, body):
        if body.expected_revision != self.revision():
            raise HTTPException(409, "通知設定已變更，請重新載入。")
        if body.enabled and not self.store.setting("owner"):
            raise HTTPException(422, "請先連接 Home Assistant 擁有者。")
        for recipient in body.recipients:
            if body.enabled and recipient.chat not in self.gateway.config(recipient.channel).get("chats", []):
                raise HTTPException(422, "收件人必須使用已設定的聊天室。")
        self.store.set_settings({CONFIG_KEY: body.model_dump(exclude={"expected_revision"}),
                                 "notifications_generation": uuid.uuid4().hex})
        self.integration = NotificationIntegration(state="unavailable" if body.enabled else "disabled",
            source_id=self.integration.source_id, last_seen=self.integration.last_seen,
            reason="awaiting_heartbeat" if body.enabled else None)
        await durable(self.store)
        return self.snapshot()

    def _active(self):
        return bool(self.settings().enabled and self.store.setting("owner") and
                    not self.store.setting("updater_gate"))

    def _adapter_ready(self, channel, adapter):
        if (adapter is None or self.gateway.states.get(channel) != "connected" or
            getattr(adapter, "config", None) != self.gateway.config(channel)):
            return False
        if channel == "line":
            account = line_account_scope(self.store)
            return bool(account and account["account_hash"] and
                        getattr(adapter, "notification_ready", False) is True and
                        getattr(adapter, "notification_scope", None) == account)
        return True

    def _recipient(self, alias):
        recipient = next((item for item in self.settings().recipients if item.alias == alias), None)
        if not recipient:
            return None
        config = ChannelConfig.model_validate(self.gateway.config(recipient.channel))
        return recipient if config.enabled and config.secret and recipient.chat in config.chats else None

    async def _owner(self, authority):
        owner_id = self.store.setting("owner")
        if not owner_id or self.authority() != authority:
            return False
        owner = await self.ha.owner(owner_id)
        return bool(owner and owner.get("id") == owner_id and owner.get("is_owner") is True and
                    owner.get("is_active") is True and self.authority() == authority)

    def _guard(self, record, adapter, *, state="sending"):
        current = self.store.get(RECORD_KIND, record.id)
        recipient = self._recipient(record.recipient)
        if (not self._active() or self.authority() != record.authority or
            self.revision() != record.routing_revision or not recipient or
            recipient.channel != record.channel or recipient.chat != record.chat or
            not current or current.get("state") != state or
            current.get("request_digest") != record.request_digest or
            self.gateway.transports.get(record.channel) is not adapter or
            not self._adapter_ready(record.channel, adapter) or
            instant(record.expires_at) <= datetime.now(UTC)):
            raise NotificationRevoked()

    def _finish(self, record, state, reason=None):
        current = self.store.get(RECORD_KIND, record.id)
        if not current or current.get("state") in TERMINAL:
            return
        self.store.put(RECORD_KIND, {**current, "state": state, "reason": reason,
            "updated_at": utcnow(), "message": None, "chat": None})

    def _prune(self):
        # Never evict an unexpired claim tombstone. Expired requests cannot pass
        # validation, so older receipts may then leave the bounded private store.
        now = datetime.now(UTC)
        records = self.store.list(RECORD_KIND, MAX_RECORDS + 1)
        removable = [raw["id"] for raw in records[64:] if raw.get("state") in TERMINAL and
                     instant(raw["expires_at"]) <= now]
        days = self.store.setting("retention", {"context_days": 7})["context_days"]
        pending = self.store.setting("retention_pending")
        if pending:
            days = min(days, pending["context_days"])
        with self.store.transaction() as tx:
            prune_notifications(tx, (now - timedelta(days=days)).isoformat())
            if removable:
                tx.executemany("DELETE FROM records WHERE kind=? AND id=?",
                               [(RECORD_KIND, id) for id in removable])

    async def start(self):
        if self._task and not self._task.done():
            return
        try:
            self.settings()
            records = self.store.list(RECORD_KIND, MAX_RECORDS + 1)
            if len(records) > MAX_RECORDS:
                raise ValueError("outbox_bound")
            validated = [NotificationRecord.model_validate(raw) for raw in records]
            for record in validated:
                instant(record.expires_at)
            for record in validated:
                if record.state in {"claiming", "sending"}:
                    self._finish(record, "unknown", "interrupted")
            self._prune()
            await durable(self.store)
        except (ValueError, OSError, RuntimeError):
            self._storage_error = True
            self.integration = NotificationIntegration(state="unavailable", reason="storage_invalid")
            return
        self._task = asyncio.create_task(self._run())

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await durable(self.store)

    def _unavailable(self, exc):
        code = str(exc).rsplit(": ", 1)[-1] if isinstance(exc, HAError) else "unavailable"
        state = "not_installed" if code in {"unknown_command", "not_configured"} else (
            "needs_rebind" if code == "needs_rebind" else "unavailable")
        reason = code if code in {"unknown_command", "not_configured", "needs_rebind",
            "storage_unavailable", "disabled", "stale_heartbeat"} else "unavailable"
        self.integration = NotificationIntegration(state=state, source_id=self.integration.source_id,
            last_seen=self.integration.last_seen, reason=reason)

    async def _run(self):
        while True:
            try:
                await self.advance()
            except Exception as exc:
                self._unavailable(exc)  # No raw HA/provider error or payload is exposed.
            await asyncio.sleep(POLL_SECONDS)

    async def advance(self):
        async with self._lock:
            if self._storage_error:
                return
            if self.store.setting(CONFIG_KEY) is None:
                return  # Installation alone does not enroll or enable native HA.
            self._prune()
            authority, revision = self.authority(), self.revision()
            status = await self._heartbeat(authority, revision)
            if status is None:
                return
            for raw in self.store.list(RECORD_KIND, MAX_RECORDS):
                record = NotificationRecord.model_validate(raw)
                if record.state in TERMINAL:
                    if not await self._refresh_binding(status, authority, revision):
                        return
                    await self._ack(record, status, authority)
            if not status.enabled or not await self._refresh_binding(status, authority, revision):
                return
            response = NativePull.model_validate(await self.ha.ws(DOMAIN + "/pull",
                                                                  controller_id=self.controller_id))
            if self.revision() != revision or self.authority() != authority:
                return
            for request in response.requests:
                if self.revision() != revision or self.authority() != authority:
                    return
                if await self._process(request, status, authority) is False:
                    return

    async def _heartbeat(self, authority, revision):
        if not await self._owner(authority):
            self.integration = NotificationIntegration(state="unavailable", reason="owner_unavailable")
            return None
        if self.revision() != revision:
            return None
        aliases = [AliasSummary(alias=item.alias, label=item.label, channel=item.channel,
                                capabilities=["text"]).model_dump()
                   for item in self.settings().recipients if self._recipient(item.alias)]
        enabled = self._active() and bool(aliases)
        status = NativeStatus.model_validate(await self.ha.ws(DOMAIN + "/update_status",
            controller_id=self.controller_id, enabled=enabled, routing_revision=revision,
            aliases=aliases, channels=[item.model_dump() for item in self.channel_states()]))
        if self.authority() != authority or self.revision() != revision:
            return None
        if (status.controller_id != self.controller_id or status.routing_revision != revision or
            status.enabled != enabled or [item.model_dump() for item in status.aliases] != aliases):
            raise ValueError("invalid_status")
        self._heartbeat_at = monotonic()
        self.integration = NotificationIntegration(state="ready" if enabled else "disabled",
            source_id=status.source_id, last_seen=utcnow())
        return status

    async def _refresh_binding(self, status, authority, revision):
        if self.authority() != authority or self.revision() != revision:
            return False
        if self._heartbeat_at is not None and monotonic() - self._heartbeat_at < 20:
            return True
        fresh = await self._heartbeat(authority, revision)
        return bool(fresh and (fresh.source_id, fresh.epoch, fresh.controller_id) ==
                    (status.source_id, status.epoch, status.controller_id))

    async def _process(self, request, status, authority):
        now = datetime.now(UTC)
        if (request.source_id != status.source_id or request.epoch != status.epoch or
            request.routing_revision != self.revision() or instant(request.expires_at) <= now or
            (instant(request.created_at) - now).total_seconds() > 30):
            return
        recipient = self._recipient(request.recipient)
        if not self._active() or not recipient:
            return
        adapter = self.gateway.transports.get(recipient.channel)
        if not self._adapter_ready(recipient.channel, adapter):
            return  # No claim was made. HA still owns the pending request.
        if not await self._refresh_binding(status, authority, request.routing_revision):
            return False  # A refresh must not lend a new enrollment to an old batch.
        id = digest([request.source_id, request.epoch, request.id])
        if self.store.get(RECORD_KIND, id):
            return  # A previous uncertain claim is never retried.
        if len(self.store.list(RECORD_KIND, MAX_RECORDS)) >= MAX_RECORDS:
            return
        record = NotificationRecord(id=id, request_id=request.id, source_id=request.source_id,
            epoch=request.epoch, claim_id=uuid.uuid4().hex, request_digest=request.digest,
            routing_revision=request.routing_revision, authority=authority, recipient=request.recipient,
            channel=recipient.channel, chat=recipient.chat, message=request.message,
            expires_at=request.expires_at, created_at=request.created_at, updated_at=utcnow(), state="claiming")
        self.store.put(RECORD_KIND, record.model_dump())
        try:
            await durable(self.store)
            self._guard(record, adapter, state="claiming")
            try:
                claim = NativeClaim.model_validate(await self.ha.ws(DOMAIN + "/claim",
                    controller_id=self.controller_id, source_id=record.source_id, epoch=record.epoch,
                    request_id=record.request_id, digest=record.request_digest,
                    routing_revision=record.routing_revision, claim_id=record.claim_id))
            except Exception:
                self._finish(record, "unknown", "claim_unconfirmed")
                return
            if not claim.granted or claim.request != request:
                self._finish(record, "unknown", "claim_unconfirmed")
                return
            self._guard(record, adapter, state="claiming")
            try:
                owner = await self._owner(authority)
            except Exception:
                owner = False
            if not owner:
                self._finish(record, "not_sent", "owner_unavailable")
                return
            self._guard(record, adapter, state="claiming")
            self.store.put(RECORD_KIND, {**record.model_dump(), "state": "sending", "updated_at": utcnow()})
            await durable(self.store)
            self._guard(record, adapter)
            try:
                await adapter.send(record.chat, record.message, guard=lambda: self._guard(record, adapter))
            except NotificationRevoked:
                self._finish(record, "not_sent", "authority_changed")
            except Exception:
                self._finish(record, "unknown", "send_unconfirmed")
            else:
                self._finish(record, "submitted")
        except NotificationRevoked:
            self._finish(record, "not_sent", "authority_changed")
        finally:
            self._finish(record, "unknown", "interrupted")
            current = NotificationRecord.model_validate(self.store.get(RECORD_KIND, record.id))
            await self._ack(current, status, authority)

    async def _ack(self, record, status, authority):
        if (record.acknowledged or record.state not in TERMINAL or
            record.source_id != status.source_id or record.epoch != status.epoch or
            instant(record.expires_at) <= datetime.now(UTC) or self.authority() != authority):
            return
        await durable(self.store)
        if self.authority() != authority:
            return
        try:
            response = NativeAck.model_validate(await self.ha.ws(DOMAIN + "/ack",
                controller_id=self.controller_id, source_id=record.source_id, epoch=record.epoch,
                request_id=record.request_id, digest=record.request_digest, claim_id=record.claim_id,
                state=record.state, reason=record.reason))
        except Exception:
            return  # Only the same terminal receipt can be retried, never the send.
        if response.acknowledged and self.authority() == authority:
            self.store.put(RECORD_KIND, {**record.model_dump(), "acknowledged": True})


def routes(app, manager):
    @app.get("/api/notifications", response_model=NotificationsSnapshot)
    async def get_notifications():
        return manager.snapshot()

    @app.post("/api/notifications/config", response_model=NotificationsSnapshot)
    async def configure_notifications(body: NotificationConfigInput):
        return await manager.configure(body)
