"""Owner-selected, passive HA setup tracking. Never configures HA or grants tools.

Schema 3 stores a private v1 record kind that older binaries safely ignore.
Only selections and a bounded baseline persist; every device readback is fresh.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import Field, ValidationError

from .discovery import DiscoveryAuthority, snapshot
from .models import (
    DiscoveryCandidate,
    DiscoveryIdentifier,
    DiscoveryRevision,
    DiscoverySetup,
    DiscoverySetupCreate,
    DiscoverySetupForget,
    DiscoverySetupList,
    DiscoverySetupOption,
    DiscoverySetupReference,
    DiscoverySetupSelect,
    DiscoverySetupView,
    DiscoverySource,
    PreviewId,
    Strict,
    canonical,
    digest,
    utcnow,
)
from .response_durability import RESPONSE_GUARD

KIND = "discovery_setup"
LIMIT = 20


class SetupRecord(Strict):
    version: Literal[1] = 1
    id: PreviewId
    binding: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    revision: DiscoveryRevision = 1
    candidate: DiscoveryCandidate
    candidate_observed_at: str
    baseline_entry_ids: Annotated[list[DiscoveryIdentifier], Field(max_length=1000)]
    baseline_complete: bool
    selected_entry_id: DiscoveryIdentifier | None = None
    selected_at: str | None = None
    created_at: str
    updated_at: str


class DiscoverySetups:
    def __init__(self, app, store, ha, host_session):
        self.app, self.store, self.ha, self.host_session = app, store, ha, host_session

    def retention_days(self):
        days = self.store.setting("retention", {"context_days": 7})["context_days"]
        pending = self.store.setting("retention_pending")
        return min(days, pending["context_days"]) if pending else days

    def cutoff(self):
        return (datetime.now(UTC) - timedelta(days=self.retention_days())).isoformat()

    def binding(self, authority):
        token, base, websocket = authority.connection
        return hmac.new(bytes.fromhex(self.store.setting("csrf_secret")),
            canonical(["discovery_setup/v1", authority.owner, base, websocket, token]).encode(),
            hashlib.sha256).hexdigest()

    async def authorize(self, request):
        authority = DiscoveryAuthority(self.app, self.store, self.ha, request, self.host_session)
        await authority.check_owner()
        # Includes a fresh remote Owner check after both inner and outer response
        # durability barriers. Captured session authority is never reconstructed.
        request.scope[RESPONSE_GUARD] = authority.check_owner
        return authority, self.binding(authority)

    def commit_guard(self, authority):
        authority.guard()
        if self.store.setting("updater_gate"):
            raise HTTPException(423, "update_in_progress")

    def records(self, binding):
        rows = self.store.db.execute(
            "SELECT body FROM records WHERE kind=? AND json_extract(body,'$.binding')=? "
            "AND created>=? ORDER BY created DESC,id DESC", (KIND, binding, self.cutoff()))
        result = []
        for row in rows:
            try:
                record = SetupRecord.model_validate_json(row[0])
                if record.created_at >= self.cutoff():
                    result.append(record)
            except ValidationError:
                continue  # Unknown record versions grant nothing and are not overwritten.
        return result

    def record(self, binding, identifier):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise HTTPException(404, "discovery_setup_not_found")
        for record in self.records(binding):
            if record.id == identifier:
                return record
        raise HTTPException(404, "discovery_setup_not_found")

    def public(self, record):
        fields = record.model_dump(exclude={"version", "binding", "baseline_entry_ids", "baseline_complete"})
        fields["expires_at"] = (datetime.fromisoformat(record.created_at) +
                                timedelta(days=self.retention_days())).isoformat()
        return DiscoverySetup(**fields)

    def receipt(self, request, binding, action, body, identifier=""):
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise HTTPException(400, "discovery_setup_idempotency_required")
        scope = f"{KIND}:{binding}:{action}:{identifier}"
        payload = body.model_dump()
        # Existing HA create receipts predate the optional source discriminator.
        # Omitted and explicit default HA requests retain their exact old digest.
        if action == "create" and payload.get("source_id") == "ha_discovery":
            payload.pop("source_id")
        request_digest = digest(payload)
        row = self.store.db.execute("SELECT digest,result FROM requests WHERE scope=? AND key=?",
                                    (scope, key)).fetchone()
        previous = None
        if row:
            if row[0] != request_digest:
                raise HTTPException(409, "discovery_setup_request_conflict")
            previous = DiscoverySetupReference.model_validate_json(row[1])
            if not previous.forgotten:
                try:
                    self.record(binding, previous.id)
                except HTTPException:
                    raise HTTPException(410, "discovery_setup_expired") from None
        return (scope, key, request_digest), previous

    def commit(self, record, receipt, *, forget=False):
        result = DiscoverySetupReference(id=record.id, revision=record.revision, forgotten=forget)
        with self.store.transaction() as tx:
            if forget:
                tx.execute("DELETE FROM records WHERE kind=? AND id=?", (KIND, record.id))
            else:
                tx.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE "
                           "SET body=excluded.body,updated=excluded.updated",
                           (KIND, record.id, canonical(record.model_dump()), record.created_at, record.updated_at))
            tx.execute("INSERT INTO requests VALUES (?,?,?,?)", (*receipt, canonical(result.model_dump())))
        return result

    async def create(self, request, body):
        authority, binding = await self.authorize(request)
        receipt, previous = self.receipt(request, binding, "create", body)
        if previous:
            return previous
        observed = await snapshot(self.ha)
        await authority.check_owner()
        self.commit_guard(authority)
        # Another request may have committed while the HA read was pending.
        receipt, previous = self.receipt(request, binding, "create", body)
        if previous:
            return previous
        network = self.app.state.network_inventory
        if body.source_id == "network_inventory":
            candidate = network.candidate(authority, body.candidate_id, body.integration_domain)
            if candidate is None:
                raise HTTPException(409, "discovery_setup_candidate_changed")
            inventory = network.view(network.binding(authority))
            suggestion = next(item for item in inventory.recommendations if item.id == candidate.id)
            candidate_observed_at = next(item.observed_at for item in inventory.observations
                                         if item.id == suggestion.observation_id)
        else:
            candidate = next((item for item in observed.candidates if item.id == body.candidate_id and
                              item.integration_domain == body.integration_domain), None)
            candidate_observed_at = observed.observed_at
        if candidate is None:
            source = next(item for item in observed.sources if item.id == "ha_discovery")
            raise HTTPException(409 if source.state == "ok" else 503,
                                "discovery_setup_candidate_changed" if source.state == "ok" else
                                "discovery_setup_source_unavailable")
        records = self.records(binding)
        existing = next((item for item in records if item.candidate.id == candidate.id and
                         item.candidate.source_id == candidate.source_id and
                         item.candidate.integration_domain == candidate.integration_domain), None)
        if existing:
            return self.commit(existing, receipt)
        if len(records) >= LIMIT:
            raise HTTPException(409, "discovery_setup_limit_reached")
        source = next(item for item in observed.sources if item.id == "ha_integrations")
        now = utcnow()
        record = SetupRecord(id=uuid.uuid4().hex, binding=binding, candidate=candidate,
            candidate_observed_at=candidate_observed_at, created_at=now, updated_at=now,
            baseline_entry_ids=[item.id for item in observed.integrations if item.domain == candidate.integration_domain],
            baseline_complete=source.state == "ok")
        return self.commit(record, receipt)

    async def select(self, request, identifier, body):
        authority, binding = await self.authorize(request)
        receipt, previous = self.receipt(request, binding, "select", body, identifier)
        if previous:
            return previous
        record = self.record(binding, identifier)
        self.revision(record, body.expected_revision)
        observed = await snapshot(self.ha)
        await authority.check_owner()
        self.commit_guard(authority)
        receipt, previous = self.receipt(request, binding, "select", body, identifier)
        if previous:
            return previous
        record = self.record(binding, identifier)
        self.revision(record, body.expected_revision)
        if not any(item.id == body.entry_id and item.domain == record.candidate.integration_domain
                   for item in observed.integrations):
            source = next(item for item in observed.sources if item.id == "ha_integrations")
            raise HTTPException(409 if source.state == "ok" else 503,
                                "discovery_setup_entry_changed" if source.state == "ok" else
                                "discovery_setup_source_unavailable")
        now = utcnow()
        updated = record.model_copy(update={"selected_entry_id": body.entry_id, "selected_at": now,
                                          "updated_at": now, "revision": record.revision + 1})
        return self.commit(updated, receipt)

    @staticmethod
    def revision(record, expected):
        if record.revision != expected:
            raise HTTPException(409, "discovery_setup_revision_changed")

    async def forget(self, request, identifier, body):
        authority, binding = await self.authorize(request)
        receipt, previous = self.receipt(request, binding, "forget", body, identifier)
        if previous:
            return previous
        self.commit_guard(authority)
        record = self.record(binding, identifier)
        self.revision(record, body.expected_revision)
        return self.commit(record.model_copy(update={"revision": record.revision + 1}), receipt, forget=True)

    async def listing(self, request):
        authority, binding = await self.authorize(request)
        records = self.records(binding)[:LIMIT]
        self.protect_readback(request, authority, binding, records)
        return DiscoverySetupList(setups=[self.public(item) for item in records],
                                  retention_days=self.retention_days())

    def protect_readback(self, request, authority, binding, records):
        inventory = self.app.state.network_inventory
        inventory_binding = inventory.binding(authority)
        inventory_digest = (inventory.view(inventory_binding).content_digest
            if any(record.candidate.source_id == "network_inventory" for record in records) else None)
        async def guard():
            await authority.check_owner()
            for record in records:
                self.revision(self.record(binding, record.id), record.revision)
            if inventory_digest is not None and inventory.view(inventory_binding).content_digest != inventory_digest:
                raise HTTPException(409, "inventory_revision_changed")
        request.scope[RESPONSE_GUARD] = guard

    async def detail(self, request, identifier):
        authority, binding = await self.authorize(request)
        record = self.record(binding, identifier)
        observed = await snapshot(self.ha)
        await authority.check_owner()
        current = self.record(binding, identifier)
        self.revision(current, record.revision)
        self.protect_readback(request, authority, binding, [current])
        sources = {item.id: item for item in observed.sources}
        candidate_seen = any(item.id == record.candidate.id and
                             item.integration_domain == record.candidate.integration_domain
                             for item in observed.candidates)
        candidate_source = sources["ha_discovery"]
        if record.candidate.source_id == "network_inventory":
            network = self.app.state.network_inventory
            inventory = network.view(network.binding(authority))
            candidate_seen = any(item.id == record.candidate.id and
                                 item.integration_domain == record.candidate.integration_domain
                                 for item in inventory.recommendations)
            active = [source for source in inventory.sources if source.state in {"ok", "partial"}]
            candidate_source = DiscoverySource(id="network_inventory", location="network_inventory",
                method="local_inventory", target="/api/discovery/inventory",
                state="ok" if all(source.state == "ok" for source in inventory.sources) else "partial" if active else "unavailable",
                observed_at=max((source.observed_at for source in inventory.sources if source.observed_at), default=None),
                item_count=len(inventory.observations), content_digest=inventory.content_digest,
                reason_code=None if active else "inventory_refresh_required")
            observed.sources.append(candidate_source)
        options = [DiscoverySetupOption(integration=item, known_before=(True if item.id in record.baseline_entry_ids
                   else False if record.baseline_complete else None)) for item in observed.integrations
                   if item.domain == record.candidate.integration_domain]
        selected = next((item.integration for item in options if item.integration.id == record.selected_entry_id), None)
        devices = [item for item in observed.devices if selected and selected.id in item.integration_ids]
        complete_devices = selected is not None and sources["ha_devices"].state == "ok"
        complete_entities = complete_devices and sources["ha_entities"].state == "ok"
        complete_states = complete_entities and sources["ha_states"].state == "ok"
        integration_state = ("unselected" if record.selected_entry_id is None else "present" if selected else
                             "not_seen" if sources["ha_integrations"].state == "ok" else "unknown")
        return DiscoverySetupView(setup=self.public(current), observed_at=observed.observed_at,
            candidate_state="present" if candidate_seen else "not_seen" if candidate_source.state == "ok" else "unknown",
            integration_state=integration_state, integration=selected, options=options[:100],
            options_truncated=len(options) > 100,
            # Keep only the selected association. Other config-entry IDs can make
            # 100 otherwise bounded device rows exceed the bridge's 1 MiB limit.
            devices=[item.model_copy(update={"integration_ids": [selected.id]}) for item in devices[:100]],
            devices_truncated=len(devices) > 100,
            device_count=len(devices) if complete_devices else None,
            entity_count=sum(item.entity_count for item in devices) if complete_entities else None,
            available_entity_count=sum(item.available_entity_count for item in devices) if complete_states else None,
            sources=observed.sources)


def routes(app, store, ha, host_session=None):
    service = DiscoverySetups(app, store, ha, host_session)
    app.state.discovery_setups = service

    @app.get("/api/discovery/setups", response_model=DiscoverySetupList)
    async def listing(request: Request):
        return await service.listing(request)

    @app.get("/api/discovery/setups/{identifier}", response_model=DiscoverySetupView)
    async def detail(identifier: str, request: Request):
        return await service.detail(request, identifier)

    @app.post("/api/discovery/setups", response_model=DiscoverySetupReference)
    async def create(body: DiscoverySetupCreate, request: Request):
        return await service.create(request, body)

    @app.post("/api/discovery/setups/{identifier}/select", response_model=DiscoverySetupReference)
    async def select(identifier: str, body: DiscoverySetupSelect, request: Request):
        return await service.select(request, identifier, body)

    @app.post("/api/discovery/setups/{identifier}/forget", response_model=DiscoverySetupReference)
    async def forget(identifier: str, body: DiscoverySetupForget, request: Request):
        return await service.forget(request, identifier, body)
