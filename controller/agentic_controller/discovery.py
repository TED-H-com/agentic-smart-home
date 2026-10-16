"""Read-only discovery for the Owner Web and verified Ingress sidebar.

No scans, setup writes or model grants. An Owner may copy selected observations
into an ordinary chat message; this endpoint does not send anything to a model.

The config flow adapter follows Core 2026.10.1 / frontend 20260930.3.
These frontend commands are isolated here; their semantics are not a stable API promise.
"""
from __future__ import annotations

import asyncio
import re

from fastapi import HTTPException, Request

from .ha import HAError, HAUnreachable
from .models import (
    DiscoveryCandidate,
    DiscoveryDevice,
    DiscoveryIntegration,
    DiscoverySnapshot,
    DiscoverySource,
    digest,
    utcnow,
)

SOURCE_TIMEOUT = 12
DISCOVERY_METHODS = frozenset({
    "bluetooth", "dhcp", "discovery", "esphome", "hardware", "hassio", "homekit",
    "integration_discovery", "mqtt", "ssdp", "unignore", "usb", "zeroconf",
})
OTHER_FLOW_METHODS = frozenset({"reauth", "user", "reconfigure", "ignore", "import", "system"})
SOURCES = (
    ("ha_discovery", "websocket", "config_entries/flow/progress", 256),
    ("ha_devices", "websocket", "config/device_registry/list", 1000),
    ("ha_integrations", "websocket", "config_entries/get", 1000),
    ("ha_entities", "websocket", "config/entity_registry/list", 5000),
    ("ha_states", "rest", "/states", 5000),
    ("ha_config", "rest", "/config", 1),
)


def identifier(value, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid_identifier")
    return value


def domain(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise ValueError("invalid_domain")
    return value


def label(value, fallback=None):
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise ValueError("invalid_label")
    # Plain text only. Consumers must escape it; it is never an instruction or URL.
    return " ".join(value.split())[:160] or fallback


def entity_id(value):
    if not isinstance(value, str) or len(value) > 255 or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value):
        raise ValueError("invalid_entity_id")
    return value


def project(source, row):
    if not isinstance(row, dict):
        raise ValueError("invalid_row")
    if source == "ha_discovery":
        context = row.get("context")
        if not isinstance(context, dict) or not isinstance(context.get("source"), str):
            raise ValueError("invalid_context")
        if context["source"] in OTHER_FLOW_METHODS:
            return None  # These known flows are not newly discovered devices.
        if context["source"] not in DISCOVERY_METHODS:
            raise ValueError("unknown_flow_source")
        integration = domain(row.get("handler"))
        placeholders = context.get("title_placeholders") or {}
        if not isinstance(placeholders, dict):
            raise ValueError("invalid_placeholders")
        return DiscoveryCandidate(id=identifier(row.get("flow_id")), integration_domain=integration,
            title=label(placeholders.get("name"), integration), discovery_method=context["source"]).model_dump()
    if source == "ha_devices":
        device = identifier(row.get("id"))
        if row.get("config_entry_id") is not None:
            entries = [identifier(row["config_entry_id"])]
        else:
            entries = row.get("config_entries", [])
            if not isinstance(entries, list) or len(entries) > 100:
                raise ValueError("invalid_config_entries")
            entries = sorted({identifier(entry) for entry in entries})
        return DiscoveryDevice(id=device, name=label(row.get("name_by_user")) or label(row.get("name"), device),
            manufacturer=label(row.get("manufacturer")), model=label(row.get("model")),
            via_device_id=identifier(row.get("via_device_id"), optional=True),
            parent_device_id=identifier(row.get("parent_device_id"), optional=True),
            integration_ids=entries, disabled=row.get("disabled_by") is not None).model_dump()
    if source == "ha_integrations":
        integration = domain(row.get("domain"))
        raw_state = row.get("state")
        state = {"loaded": "loaded", "not_loaded": "not_loaded", "setup_retry": "retry",
                 "setup_error": "error", "migration_error": "error", "failed_unload": "error"}.get(
                     raw_state if isinstance(raw_state, str) else "", "unknown")
        if row.get("source") == "ignore":
            state = "ignored"
        elif row.get("disabled_by") is not None:
            state = "disabled"
        return DiscoveryIntegration(id=identifier(row.get("entry_id")), domain=integration,
            title=label(row.get("title"), integration), state=state).model_dump()
    if source == "ha_entities":
        return {"id": entity_id(row.get("entity_id")),
                "device_id": identifier(row.get("device_id"), optional=True),
                "disabled": row.get("disabled_by") is not None}
    if source == "ha_states":
        if not isinstance(row.get("state"), str):
            raise ValueError("invalid_state")
        return {"id": entity_id(row.get("entity_id")),
                "available": row["state"] not in {"", "unknown", "unavailable"}}
    version = label(row.get("version"))
    if not version:
        raise ValueError("missing_version")
    return {"id": "core", "version": version}


async def collect(ha, specification):
    name, method, target, limit = specification
    source = DiscoverySource(id=name, state="unavailable", method=method, target=target)
    try:
        async with asyncio.timeout(SOURCE_TIMEOUT):
            raw = await (ha.ws(target) if method == "websocket" else ha.request("GET", target))
        rows = [raw] if name == "ha_config" else raw
        if not isinstance(rows, list):
            raise ValueError("invalid_source_shape")
        values, seen, invalid = [], set(), False
        for row in rows[:limit]:
            try:
                value = project(name, row)
                if value is None:
                    continue
                if value["id"] in seen:
                    raise ValueError("duplicate_source_id")
                seen.add(value["id"])
                values.append(value)
            except (ValueError, TypeError):
                invalid = True
        values.sort(key=lambda item: item["id"])
        source.truncated = len(rows) > limit
        source.state = "partial" if invalid or source.truncated else "ok"
        source.reason_code = "invalid_rows" if invalid else "source_limit" if source.truncated else None
        source.observed_at, source.item_count = utcnow(), len(values)
        source.content_digest = digest(values)
        return source, values
    except (HAUnreachable, TimeoutError):
        source.reason_code = "source_unavailable"
    except HAError as exc:
        unsupported = exc.status in {404, 405, 501} or str(exc) == f"HA {target}: unknown_command"
        source.state = "unsupported" if unsupported else "unavailable"
        source.reason_code = "source_unsupported" if unsupported else "source_unavailable"
    except (ValueError, TypeError):
        source.state, source.reason_code = "unsupported", "source_contract_changed"
    return source, []


async def snapshot(ha):
    results = await asyncio.gather(*(collect(ha, specification) for specification in SOURCES))
    sources = {source.id: source for source, _ in results}
    values = {source.id: rows for source, rows in results}
    version = values["ha_config"][0]["version"] if values["ha_config"] else None
    for source in sources.values():
        source.upstream_version = version
    states = {row["id"]: row["available"] for row in values["ha_states"]}
    entities = {}
    for row in values["ha_entities"]:
        entities.setdefault(row["device_id"], []).append(row)
    for device in values["ha_devices"]:
        # Partial lists cannot establish totals. Registry presence is not physical reachability.
        if sources["ha_entities"].state == "ok":
            related = entities.get(device["id"], [])
            device["entity_count"] = len(related)
            if sources["ha_states"].state == "ok":
                device["available_entity_count"] = sum(
                    not device["disabled"] and not row["disabled"] and states.get(row["id"], False)
                    for row in related)
    if sources["ha_devices"].content_digest is not None:
        sources["ha_devices"].content_digest = digest(values["ha_devices"])
    unimplemented = [DiscoverySource(id=name, state="not_implemented", location="not_implemented",
        method="not_implemented", target=name) for name in ("router_inventory", "lan_probe", "account_inventory")]
    result = {"contract_version": 1, "observed_at": utcnow(),
        "sources": [source.model_dump() for source in sources.values()] + [s.model_dump() for s in unimplemented],
        "candidates": values["ha_discovery"], "devices": values["ha_devices"],
        "integrations": values["ha_integrations"], "native_path": "/config/integrations",
        "limitations": ["ha_observations_only", "network_coverage_not_verified", "registry_is_not_physical_identity",
                        "api_availability_is_not_physical_verification", "discovery_does_not_grant_ai_access"]}
    return DiscoverySnapshot(**result, content_digest=digest(result))


def routes(app, store, ha, host_session=None):
    async def active_owner(owner):
        try:
            current = await ha.owner(owner)
        except HAError as exc:
            raise HTTPException(503, "discovery_owner_unavailable") from exc
        if not current or store.setting("owner") != owner:
            raise HTTPException(403, "discovery_owner_required")

    @app.get("/api/discovery", response_model=DiscoverySnapshot)
    async def discovery(request: Request):
        epoch = store.setting("hermes_web_epoch")
        token = getattr(ha, "token", None)
        identity = getattr(request.state, "bridge_identity", None)
        def session_guard():
            if store.setting("hermes_web_epoch") != epoch:
                raise HTTPException(401, "discovery_session_expired")
            if getattr(ha, "token", None) != token:
                raise HTTPException(403, "discovery_authority_changed")
            if identity:
                bridge = getattr(app.state, "ingress_bridge", None)
                # Recheck the unforgeable request marker, current pairing and
                # process-local Host proof after Owner checks and the snapshot.
                if not bridge or bridge.identity(request) is not identity:
                    raise HTTPException(403, "discovery_owner_required")
            elif host_session and not host_session.verify(request):
                raise HTTPException(401, "discovery_session_expired")

        session_guard()
        owner = store.setting("owner")
        if not owner or request.state.principal != owner:
            raise HTTPException(403, "discovery_owner_required")
        await active_owner(owner)
        session_guard()
        result = await snapshot(ha)
        await active_owner(owner)
        session_guard()
        return result
