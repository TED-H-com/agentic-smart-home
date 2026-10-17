"""Fixed Host observations and owner-imported router inventories.

Records are private context, never authority or an automatic device association.
Schema 3 adds an ignorable v1 record kind; an opaque revision survives clear/prune.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import aiohttp
from fastapi import HTTPException, Request
from pydantic import Field, ValidationError

from .discovery import DiscoveryAuthority
from .durability import client_session
from .models import (
    DiscoveryCandidate,
    InventoryClear,
    InventoryCollection,
    InventoryImport,
    InventoryObservation,
    InventoryRecommendation,
    InventoryReference,
    InventoryScan,
    InventoryScopes,
    InventorySnapshot,
    InventorySource,
    Strict,
    canonical,
    digest,
    utcnow,
)
from .response_durability import RESPONSE_GUARD

KIND = "network_inventory"
LAN_IDS = {"lan_neighbors", "lan_mdns", "lan_ssdp"}
METHODS = {"lan_neighbors": "neighbor_table", "lan_mdns": "mdns", "lan_ssdp": "ssdp",
           "router_import": "json_import"}
LIMITATIONS = ["觀察列不等於實體設備；不同來源不會依 IP、MAC 或名稱自動合併。",
               "LAN 掃描僅支援 IPv4 實體介面；虛擬橋接、VLAN 與隧道不在掃描範圍。",
               "只能看到選定網段的回應與鄰居快取；VLAN、休眠、Zigbee、Thread 與雲端設備可能缺席。",
               "盤點與接入建議不授予控制權限，也不代表實體功能已驗證。",
               "觀察超過 24 小時後停止提供接入建議；原始匯入檔案不會保存。"]
METADATA_KEYS = {"manufacturer", "model", "server", "service_type", "usn"}


class InventoryRecord(Strict):
    version: Literal[1] = 1
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    revision: Annotated[int, Field(ge=1)]
    sources: Annotated[list[InventorySource], Field(max_length=4)] = []
    observations: Annotated[list[InventoryObservation], Field(max_length=1024)] = []
    created_at: str
    updated_at: str


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = datetime.fromtimestamp(value, UTC)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone_required")
        parsed = parsed.astimezone(UTC)
    else:
        raise ValueError("invalid_timestamp")
    if parsed > datetime.now(UTC) + timedelta(minutes=5) or parsed.year < 1970:
        raise ValueError("invalid_timestamp")
    return parsed.isoformat()


def plain(value, *, limit=160):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_label")
    value = " ".join(value.split())
    if len(value) > limit or any(ord(char) < 32 for char in value):
        raise ValueError("invalid_label")
    return value or None


def prune_record(record, cutoff):
    """Drop source content independently; keep only a non-content CAS tombstone."""
    sources = [source for source in record.sources if source.observed_at and source.observed_at >= cutoff]
    ids = {source.id for source in sources}
    return record.model_copy(update={"sources": sources,
        "observations": [row for row in record.observations if row.source_id in ids and row.observed_at >= cutoff]})


def prune_inventory(tx, cutoff):
    rows = list(tx.execute("SELECT body FROM records WHERE kind=?", (KIND,)))
    for row in rows:
        try:
            record = InventoryRecord.model_validate_json(row[0])
        except ValidationError:
            continue
        kept = prune_record(record, cutoff)
        if kept != record:
            tx.execute("UPDATE records SET body=? WHERE kind=? AND id=?", (canonical(kept.model_dump()), KIND, record.id))


async def broker_call(method, path, body=None, *, store):
    if (method, path) not in {("GET", "/discovery/scopes"), ("POST", "/discovery/scan")}:
        raise HTTPException(422, "inventory_invalid_host_operation")
    try:
        connector = aiohttp.UnixConnector(path="/run/agentic-home/host.sock")
        async with client_session(store, connector=connector, timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as client:
            async with client.request(method, "http://host" + path, json=body, allow_redirects=False) as response:
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 1048576:
                        raise ValueError("response_limit")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("response_shape")
                if response.status >= 300:
                    raise HTTPException(503, "inventory_host_unavailable")
                return value
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError):
        raise HTTPException(503, "inventory_host_unavailable") from None


def imported_rows(body):
    """Project documented inventory fields only; unknown columns never persist."""
    if len(body.content.encode("utf-8")) > 65536:
        raise HTTPException(422, "inventory_import_too_large")
    try:
        if body.format == "json":
            rows = json.loads(body.content)
            if isinstance(rows, dict):
                rows = next((rows[key] for key in ("devices", "clients", "leases") if isinstance(rows.get(key), list)), None)
            if not isinstance(rows, list):
                raise ValueError("expected_rows")
        elif body.format == "csv":
            rows = list(csv.DictReader(io.StringIO(body.content), strict=True))
        else:
            rows = []
            for line in body.content.splitlines():
                if not line.strip():
                    continue
                parts = line.split()
                # dnsmasq's first column is lease EXPIRY, not last seen.
                rows.append({"mac": parts[1], "ip": parts[2], "hostname": None if parts[3] == "*" else parts[3]}
                            if len(parts) == 5 and parts[0].isdigit() else None)
        now = utcnow()
        observations, occurrences, invalid = [], {}, False
        for raw in rows[:256]:
            try:
                if not isinstance(raw, dict):
                    raise ValueError("invalid_row")
                ip = raw.get("ip", raw.get("ip_address", raw.get("address")))
                address = str(ipaddress.ip_address(ip.strip())) if isinstance(ip, str) and ip.strip() else None
                if ip not in (None, "") and address is None:
                    raise ValueError("invalid_address")
                mac = raw.get("mac", raw.get("mac_address"))
                if mac not in (None, ""):
                    if not isinstance(mac, str):
                        raise ValueError("invalid_mac")
                    mac = mac.lower().replace("-", ":")
                    if not re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac):
                        raise ValueError("invalid_mac")
                else:
                    mac = None
                name = plain(raw.get("hostname", raw.get("name")))
                if not any((address, mac, name)):
                    raise ValueError("missing_identity")
                metadata = {key: value for key in ("manufacturer", "model") if (value := plain(raw.get(key)))}
                services = raw.get("services", [])
                if isinstance(services, str):
                    services = [part.strip() for part in services.split(";") if part.strip()]
                if not isinstance(services, list) or len(services) > 16:
                    raise ValueError("invalid_services")
                service_values = {plain(service) for service in services}
                if None in service_values:
                    raise ValueError("invalid_service")
                services = sorted(service_values)
                last_seen = raw.get("last_seen")
                source_updated = timestamp(last_seen) if last_seen not in (None, "") else None
                identity = digest([address, mac, name, services, metadata])[:40]
                duplicate = occurrences.get(identity, 0)
                occurrences[identity] = duplicate + 1
                observations.append(InventoryObservation(id=f"router_{identity}_{duplicate}", source_id="router_import",
                    scope_id="router_import", address=address, mac=mac, name=name, services=services,
                    observed_at=now, source_updated_at=source_updated, metadata=metadata))
            except (ValueError, TypeError, OverflowError, OSError):
                invalid = True
        if not observations:
            raise ValueError("no_valid_rows")
        source = InventorySource(id="router_import", state="partial" if invalid or len(rows) > 256 else "ok",
            method=f"{body.format}_import", target=plain(body.label, limit=80), observed_at=now,
            item_count=len(observations), truncated=len(rows) > 256,
            reason_code="invalid_rows" if invalid else "source_limit" if len(rows) > 256 else None,
            content_digest=digest([row.model_dump() for row in observations]),
            limitations=["匯入租約或清單不能證明設備目前在線；服務與廠商欄位只作為待確認線索。"])
        return source, observations
    except (ValueError, TypeError, csv.Error, RecursionError):
        raise HTTPException(422, "inventory_import_invalid") from None


FINGERPRINTS = (
    ("_hue._tcp", "hue", "Philips Hue", "在 HA 新增 Hue，並依提示按下橋接器上的連線按鈕。"),
    ("_googlecast._tcp", "cast", "Google Cast", "在 HA 新增 Google Cast；這不會匯入整個 Google Home 帳號。"),
    ("_esphomelib._tcp", "esphome", "ESPHome", "在 HA 新增 ESPHome；如要求加密金鑰，請在 HA 本機設定。"),
    ("_hap._tcp", "homekit_controller", "HomeKit Device", "在 HA 新增 HomeKit Device；配對碼與已配對狀態請在 HA 確認。"),
    ("_shelly._tcp", "shelly", "Shelly", "在 HA 新增 Shelly，並確認設備與 HA 的網路可互通。"),
)


def recommendations(observations, sources):
    fresh_cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    fresh = {source.id for source in sources if source.state in {"ok", "partial"} and source.observed_at and
             source.observed_at >= fresh_cutoff}
    result = []
    for row in observations:
        if row.source_id not in fresh or (row.source_updated_at and row.source_updated_at < fresh_cutoff):
            continue
        services = {service.lower().rstrip(".").removesuffix(".local") for service in row.services}
        fingerprint = next((item for item in FINGERPRINTS if item[0] in services), None)
        matched_service = fingerprint[0] if fingerprint else None
        if fingerprint is None and "_hap._udp" in services:
            fingerprint, matched_service = FINGERPRINTS[3], "_hap._udp"
        if (fingerprint is None and row.source_id == "lan_mdns" and "_http._tcp" in services and
            (row.name or "").lower().startswith("shelly")):
            fingerprint, matched_service = FINGERPRINTS[4], "_http._tcp / Shelly 名稱"
        confidence, reason = "hint", "匯入清單的服務欄位；尚未驗證設備回應。"
        if fingerprint and row.source_id == "lan_mdns":
            confidence, reason = "service_match", f"mDNS 回應宣告 {matched_service}；仍需由 HA 完成配對與驗證。"
        if fingerprint is None:
            server = row.metadata.get("server", "").lower()
            hints = " ".join([row.name or "", row.metadata.get("manufacturer", ""), row.metadata.get("model", "")]).lower()
            if row.source_id == "lan_ssdp" and ("ipbridge" in server or "philips hue" in server):
                fingerprint, reason = FINGERPRINTS[0], "SSDP 的橋接器宣告含 Hue 線索；尚未驗證廠商與設備身分。"
            elif re.search(r"\bhue\b", hints) and re.search(r"\b(philips|signify|hue)\b", hints):
                fingerprint, reason = FINGERPRINTS[0], "名稱、廠商或型號含 Hue 線索；不是已確認的設備身分。"
            elif "esphome" in hints:
                fingerprint, reason = FINGERPRINTS[2], "名稱或廠商含 ESPHome 線索；請先確認設備類型。"
            elif "shelly" in hints:
                fingerprint, reason = FINGERPRINTS[4], "名稱或廠商含 Shelly 線索；請先確認設備類型。"
            elif any(value in hints for value in ("tuya", "smart life", "smartlife")):
                fingerprint = ("account", "tuya", "Tuya / Smart Life", "在 HA 新增 Tuya，依原生流程登入 Smart Life / Tuya Smart；帳號授權不在此處輸入。")
                reason = "名稱或廠商含 Tuya 線索；透過帳號確認設備，不能由 LAN 列判定帳號內容。"
        if fingerprint is None:
            continue
        _, domain, title, step = fingerprint
        result.append(InventoryRecommendation(id="inventory_" + digest([row.id, domain])[:40],
            observation_id=row.id, integration_domain=domain, title=row.name or title,
            confidence=confidence, reason=reason, next_step=step,
            documentation_url=f"https://www.home-assistant.io/integrations/{domain}/"))
    return result


class NetworkInventory:
    def __init__(self, app, store, ha, host_session):
        self.app, self.store, self.ha, self.host_session = app, store, ha, host_session
        self.lock = asyncio.Lock()

    def cutoff(self):
        days = self.store.setting("retention", {"context_days": 7})["context_days"]
        pending = self.store.setting("retention_pending")
        return (datetime.now(UTC) - timedelta(days=min(days, pending["context_days"]) if pending else days)).isoformat()

    def binding(self, authority):
        token, base, websocket = authority.connection
        return hmac.new(bytes.fromhex(self.store.setting("csrf_secret")),
            canonical(["network_inventory/v1", authority.owner, base, websocket, token]).encode(), hashlib.sha256).hexdigest()

    async def authorize(self, request):
        authority = DiscoveryAuthority(self.app, self.store, self.ha, request, self.host_session)
        await authority.check_owner()
        request.scope[RESPONSE_GUARD] = authority.check_owner
        return authority, self.binding(authority)

    def record(self, binding):
        raw = self.store.get(KIND, binding)
        if raw is None:
            return None
        try:
            record = InventoryRecord.model_validate(raw)
        except ValidationError:
            raise HTTPException(503, "inventory_record_unsupported") from None
        return prune_record(record, self.cutoff())

    def view(self, binding):
        record = self.record(binding)
        sources = list(record.sources) if record else []
        observations = list(record.observations) if record else []
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        for index, source in enumerate(sources):
            if source.observed_at and source.observed_at < cutoff:
                sources[index] = source.model_copy(update={"state": "expired", "reason_code": "observation_expired"})
        for source_id in (*sorted(LAN_IDS), "router_import"):
            if not any(source.id == source_id for source in sources):
                sources.append(InventorySource(id=source_id, method=METHODS[source_id], state="not_configured",
                    target="尚未掃描" if source_id in LAN_IDS else "尚未匯入"))
        expires = [(datetime.fromisoformat(source.observed_at) + timedelta(hours=24)).isoformat()
                   for source in sources if source.observed_at and source.state in {"ok", "partial"}]
        all_observations = sorted(observations, key=lambda row: (row.source_id, row.id))

        def projected(limit):
            rows = all_observations[:limit]
            projected_sources = list(sources)
            limitations = list(LIMITATIONS)
            if limit < len(all_observations):
                limitations.append("傳輸大小限制：僅顯示部分觀察；來源數量為本次顯示列數。")
                for index, source in enumerate(sources):
                    original_rows = [row for row in all_observations if row.source_id == source.id]
                    selected_rows = [row for row in rows if row.source_id == source.id]
                    if len(selected_rows) != len(original_rows):
                        projected_sources[index] = source.model_copy(update={
                            "state": "partial" if source.state == "ok" else source.state,
                            "item_count": len(selected_rows), "truncated": True, "reason_code": "response_limit",
                            "content_digest": digest([row.model_dump() for row in selected_rows])})
            suggested = recommendations(rows, projected_sources)
            suggested_ids = {item.observation_id for item in suggested}
            recommendation_expiry = expires + [
                (datetime.fromisoformat(row.source_updated_at) + timedelta(hours=24)).isoformat()
                for row in rows if row.id in suggested_ids and row.source_updated_at]
            payload = {"rule_adapter_version": 1, "sources": [source.model_dump() for source in projected_sources],
                       "observations": [row.model_dump() for row in rows],
                       "recommendations": [item.model_dump() for item in suggested]}
            return InventorySnapshot(revision=record.revision if record else 0,
                observed_at=record.updated_at if record else utcnow(),
                expires_at=min(recommendation_expiry) if recommendation_expiry else None,
                sources=projected_sources, observations=rows, recommendations=suggested,
                limitations=limitations, content_digest=digest(payload))

        result = projected(len(all_observations))
        if len(canonical(result.model_dump()).encode()) <= 750 * 1024:
            return result
        low, high = 0, len(all_observations)
        while low < high:
            middle = (low + high + 1) // 2
            if len(canonical(projected(middle).model_dump()).encode()) <= 750 * 1024:
                low = middle
            else:
                high = middle - 1
        return projected(low)

    def candidate(self, authority, identifier, domain):
        view = self.view(self.binding(authority))
        recommendation = next((item for item in view.recommendations if item.id == identifier and item.integration_domain == domain), None)
        if recommendation is None:
            return None
        return DiscoveryCandidate(id=recommendation.id, integration_domain=domain, title=recommendation.title,
                                  discovery_method="network_inventory", source_id="network_inventory")

    def receipt(self, request, binding, action, body):
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise HTTPException(400, "inventory_idempotency_required")
        scope, fingerprint = f"{KIND}:{binding}:{action}", digest(body.model_dump())
        row = self.store.db.execute("SELECT digest,result FROM requests WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row and row[0] != fingerprint:
            raise HTTPException(409, "inventory_request_conflict")
        return (scope, key, fingerprint), InventoryReference.model_validate_json(row[1]) if row else None

    def commit(self, authority, binding, sources, observations, receipt):
        authority.guard()
        if self.store.setting("updater_gate"):
            raise HTTPException(423, "update_in_progress")
        previous, now = self.record(binding), utcnow()
        record = InventoryRecord(id=binding, revision=previous.revision + 1 if previous else 1,
            sources=sources, observations=observations, created_at=previous.created_at if previous else now, updated_at=now)
        reference = InventoryReference(revision=record.revision)
        with self.store.transaction() as tx:
            tx.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET "
                "body=excluded.body,updated=excluded.updated", (KIND, binding, canonical(record.model_dump()), record.created_at, now))
            tx.execute("INSERT INTO requests VALUES (?,?,?,?)", (*receipt, canonical(reference.model_dump())))
        return reference

    def protect(self, request, authority, binding, revision, content_digest):
        async def guard():
            await authority.check_owner()
            current = self.record(binding)
            if (current.revision if current else 0) != revision:
                raise HTTPException(409, "inventory_revision_changed")
            if self.view(binding).content_digest != content_digest:
                raise HTTPException(409, "inventory_observation_expired")
        request.scope[RESPONSE_GUARD] = guard

    async def listing(self, request):
        authority, binding = await self.authorize(request)
        result = self.view(binding)
        self.protect(request, authority, binding, result.revision, result.content_digest)
        return result

    async def scopes(self, request):
        authority, _ = await self.authorize(request)
        value = await broker_call("GET", "/discovery/scopes", store=self.store)
        await authority.check_owner()
        try:
            return InventoryScopes.model_validate(value)
        except ValidationError:
            raise HTTPException(503, "inventory_host_contract_changed") from None

    async def mutate(self, request, body, action):
        authority, binding = await self.authorize(request)
        async with self.lock:
            await authority.check_owner()
            receipt, previous = self.receipt(request, binding, action, body)
            if previous:
                return previous
            current = self.record(binding)
            sources = list(current.sources) if current else []
            observations = list(current.observations) if current else []
            if action == "scan":
                if self.store.setting("updater_gate"):
                    raise HTTPException(423, "update_in_progress")
                raw = await broker_call("POST", "/discovery/scan", body.model_dump(), store=self.store)
                await authority.check_owner()
                try:
                    result = InventoryCollection.model_validate(raw)
                    if (result.scope_id != body.scope_id or len(result.sources) != 3 or
                        {source.id for source in result.sources} != LAN_IDS):
                        raise ValueError("source_contract")
                    result.observed_at = timestamp(result.observed_at)
                    seen = set()
                    for source in result.sources:
                        if source.method != METHODS[source.id] or source.state not in {"ok", "partial", "unavailable"}:
                            raise ValueError("source_contract")
                        source.observed_at = timestamp(source.observed_at or result.observed_at)
                        if source.observed_at != result.observed_at:
                            raise ValueError("source_timestamp")
                    for row in result.observations:
                        if row.source_id not in LAN_IDS or row.scope_id != body.scope_id or row.id in seen:
                            raise ValueError("observation_contract")
                        seen.add(row.id)
                        if row.address is not None:
                            row.address = str(ipaddress.ip_address(row.address))
                        row.observed_at = timestamp(row.observed_at)
                        if row.observed_at != result.observed_at:
                            raise ValueError("observation_timestamp")
                        if row.source_updated_at:
                            row.source_updated_at = timestamp(row.source_updated_at)
                        row.metadata = {key: value for key, value in row.metadata.items() if key in METADATA_KEYS}
                    for source in result.sources:
                        rows = [row for row in result.observations if row.source_id == source.id]
                        if len(rows) > 256 or (source.state == "unavailable" and rows):
                            raise ValueError("source_rows")
                        source.item_count = len(rows)
                        source.content_digest = digest([row.model_dump() for row in rows])
                except (ValueError, TypeError, OverflowError, OSError):
                    raise HTTPException(503, "inventory_host_contract_changed") from None
                replaced, added_sources, added_rows = LAN_IDS, result.sources, result.observations
            elif action == "import":
                source, rows = imported_rows(body)
                replaced, added_sources, added_rows = {"router_import"}, [source], rows
            else:
                if (current.revision if current else 0) != body.expected_revision:
                    raise HTTPException(409, "inventory_revision_changed")
                replaced, added_sources, added_rows = LAN_IDS | {"router_import"}, [], []
            sources = [source for source in sources if source.id not in replaced] + added_sources
            observations = [row for row in observations if row.source_id not in replaced] + added_rows
            return self.commit(authority, binding, sources, observations, receipt)


def routes(app, store, ha, host_session=None):
    service = NetworkInventory(app, store, ha, host_session)
    app.state.network_inventory = service

    @app.get("/api/discovery/inventory", response_model=InventorySnapshot)
    async def listing(request: Request):
        return await service.listing(request)

    @app.get("/api/discovery/inventory/scopes", response_model=InventoryScopes)
    async def scopes(request: Request):
        return await service.scopes(request)

    @app.post("/api/discovery/inventory/scan", response_model=InventoryReference)
    async def scan(body: InventoryScan, request: Request):
        return await service.mutate(request, body, "scan")

    @app.post("/api/discovery/inventory/import", response_model=InventoryReference)
    async def import_inventory(body: InventoryImport, request: Request):
        return await service.mutate(request, body, "import")

    @app.post("/api/discovery/inventory/clear", response_model=InventoryReference)
    async def clear(body: InventoryClear, request: Request):
        return await service.mutate(request, body, "clear")
