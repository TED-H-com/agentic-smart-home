"""Read one current-message setup selection through the Hermes MCP boundary.

The assistant never selects an operation ID or receives approval, backup,
credential or recovery authority. Setup history and fresh HA readback remain
separate. This module performs no setup-record mutations or HA writes.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from .discovery_chat import ChatDiscoveryAuthority, shared_label
from .ha import HAError
from .models import ConversationDeviceSetup, DeviceSetupChatDevice, digest
from .response_durability import RESPONSE_GUARD

LIMITATIONS = [
    "帳號、密碼與驗證碼只在設定卡輸入，不會分享給管家；管家也不能代為送出。",
    "這次只分享你在本則訊息選擇的一筆接入進度；不分享其他接入項目，也不沿用前文的分享選擇。",
    "setup_state 是已保存的操作紀錄；current_state 與整合、設備及實體數量來自這次讀回，兩者不能互相替代。",
    "最多分享十六筆對應設備；來源不完整時總數保持未知。整合已載入或 API 可用不代表實體功能已驗證。",
    "這是唯讀分享；不會重送接入、核對未明操作、批准提案、修改設定或授予設備控制權限。",
    "current_state=not_recorded 只表示尚未保存確切整合 ID；不代表請求未送出或 HA 沒有建立整合。",
]
SOURCE_TARGETS = {
    "ha_integrations": "config_entries/get", "ha_devices": "config/device_registry/list",
    "ha_entities": "config/entity_registry/list", "ha_states": "/states",
}


class SetupChatAuthority(ChatDiscoveryAuthority):
    def __init__(self, service, token, selected):
        super().__init__(service.store, service.ha, service.coordinator, token)
        self.service, self.selected, self.revision = service, selected, None

    def guard(self):
        grant = super().guard()
        self.service.message(self.token, self.selected)
        if self.revision is not None:
            setups = self.service.app.state.device_setups
            record = setups.record(setups.binding(self), self.selected, setups.home_binding(self))
            if record.revision != self.revision:
                raise HTTPException(409, "device_setup_revision_changed")
        return grant


class DeviceSetupChat:
    def __init__(self, app, store, ha, coordinator):
        self.app, self.store, self.ha, self.coordinator = app, store, ha, coordinator

    def message(self, token, selected):
        grant = self.coordinator.authorize(token)
        item = self.store.conversation(id=grant["message_id"])
        if (item.get("source") != "web" or item.get("coordinator") != "hermes"
                or item.get("device_setup_id") != selected
                or selected is not None and item.get("sender") != self.store.setting("owner")):
            raise HTTPException(403, "device_setup_sharing_not_authorized")
        return item

    def save(self, token, selected, result):
        item = self.message(token, selected)
        value = result.model_dump()
        if result.state != "consent_required":
            value["content_digest"] = digest({key: field for key, field in value.items() if key != "content_digest"})
        self.store.put("conversation", {**item, "device_setup_status": value})
        return value

    def projection(self, authority, record, view):
        def safe(value):
            return shared_label(value, secrets=authority.connection)
        days = self.store.setting("retention", {"context_days": 7})["context_days"]
        pending = self.store.setting("retention_pending")
        if pending:
            days = min(days, pending["context_days"])
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        title = safe(view.candidate.title) if record.created_at >= cutoff else None
        sources = {source.id: source for source in view.sources if source.id in SOURCE_TARGETS}
        entry_source = sources.get("ha_integrations")
        current = "not_recorded" if record.entry_id is None else "unknown"
        entry = None
        if entry_source is not None and entry_source.state in {"ok", "partial"}:
            if view.integration is not None:
                current = "present"
                entry = view.integration.model_copy(update={"title": safe(view.integration.title) or "設備整合"})
            elif entry_source.state == "ok" and not entry_source.truncated:
                current = "not_seen"
        devices = [DeviceSetupChatDevice(name=safe(device.name) or "未命名設備",
            manufacturer=safe(device.manufacturer), model=safe(device.model), disabled=device.disabled,
            entity_count=device.entity_count, available_entity_count=device.available_entity_count)
            for device in view.devices[:16]] if current == "present" else []
        counts = (view.device_count, view.entity_count, view.available_entity_count) if current == "present" else (None, None, None)
        projected = {
            "ha_integrations": entry.model_dump() if entry else None,
            "ha_devices": [device.model_dump() for device in devices],
            "ha_entities": {"entity_count": counts[1]},
            "ha_states": {"available_entity_count": counts[2]},
        }
        item_counts = {"ha_integrations": 1 if entry else 0 if current == "not_seen" else None,
                       "ha_devices": counts[0], "ha_entities": counts[1], "ha_states": counts[2]}
        public_sources = [source.model_copy(update={"target": SOURCE_TARGETS[name],
            "upstream_version": safe(source.upstream_version), "reason_code": safe(source.reason_code),
            "item_count": item_counts[name],
            # The digest describes this bounded selection, never the unshared
            # household-wide registry fetched by the internal adapter.
            "content_digest": digest(projected[name]) if source.observed_at else None})
            for name, source in sources.items()]
        return ConversationDeviceSetup(state="unavailable" if current == "unknown" else "ready",
            setup_id=record.id, setup_state=record.state, input_step=record.input_step if record.state == "needs_input" else None,
            setup_updated_at=record.updated_at,
            title=title or "設備接入", current_state=current, integration=entry, devices=devices,
            device_count=counts[0], entity_count=counts[1], available_entity_count=counts[2],
            sources=public_sources, truncated=view.devices_truncated or len(view.devices) > 16
                or any(source.truncated for source in sources.values()),
            observed_at=view.observed_at,
            reason_code="current_readback_unavailable" if current == "unknown" else safe(record.reason_code),
            limitations=LIMITATIONS)

    async def read(self, token, request=None):
        grant = self.coordinator.authorize(token)
        selected = self.store.conversation(id=grant["message_id"]).get("device_setup_id")
        self.message(token, selected)
        if request is not None:
            request.scope[RESPONSE_GUARD] = lambda: self.message(token, selected)
        if selected is None:
            return self.save(token, selected, ConversationDeviceSetup(state="consent_required", limitations=[
                "請開啟「設備接入進度」，選擇要查詢的結果，再按「請管家解讀結果」；本則訊息尚未分享接入項目。",
                "管家只會讀取你這次選擇的一筆進度；這不會批准、重送或修改任何設備設定。",
            ]))
        authority = SetupChatAuthority(self, token, selected)
        try:
            await authority.check_owner()
        except (HAError, TimeoutError):
            authority.guard()
            return self.save(token, selected, ConversationDeviceSetup(state="unavailable", setup_id=selected,
                reason_code="owner_readback_unavailable", limitations=LIMITATIONS))
        setups = self.app.state.device_setups
        try:
            record = setups.record(setups.binding(authority), selected, setups.home_binding(authority))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            if request is not None:
                request.scope[RESPONSE_GUARD] = authority.check_response
            return self.save(token, selected, ConversationDeviceSetup(state="unavailable", setup_id=selected,
                reason_code="setup_not_available", limitations=LIMITATIONS))
        authority.revision = record.revision
        try:
            # Leave time for fresh Owner checks and the final response guard
            # within Hermes' 45-second tool deadline.
            async with asyncio.timeout(20):
                view = await setups.readback(authority, record)
            result = self.projection(authority, record, view)
        except (HAError, TimeoutError):
            authority.guard()
            result = ConversationDeviceSetup(state="unavailable", setup_id=selected,
                setup_state=record.state, input_step=record.input_step if record.state == "needs_input" else None,
                setup_updated_at=record.updated_at,
                reason_code="current_readback_unavailable", limitations=LIMITATIONS)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            authority.guard()
            result = ConversationDeviceSetup(state="unavailable", setup_id=selected,
                setup_state=record.state, input_step=record.input_step if record.state == "needs_input" else None,
                setup_updated_at=record.updated_at,
                reason_code="current_readback_unavailable", limitations=LIMITATIONS)
        await authority.check_response()
        authority.guard()
        if request is not None:
            request.scope[RESPONSE_GUARD] = authority.check_response
        return self.save(token, selected, result)
