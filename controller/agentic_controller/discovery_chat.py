"""Case-scoped discovery sharing through the existing Hermes MCP boundary.

Only the current Owner message can opt in. The model cannot supply consent,
start a LAN scan, create a config flow, or grant household read/control access.
Results reuse the read-only HA adapter and remain ordinary retained conversation
content; they never become setup approvals or physical verification evidence.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re

from fastapi import HTTPException
from pydantic import ValidationError

from .discovery import snapshot
from .ha import HAError
from .ha_lan import HomeLanObservation, HostLanStatus, observe_home_lan
from .models import (
    ConversationDiscovery,
    DiscoveryChatCandidate,
    DiscoveryChatEnvironment,
    DiscoveryChatNetwork,
    DiscoveryChatObservation,
    InventorySource,
    digest,
    utcnow,
)
from .operations import broker_call
from .response_durability import RESPONSE_GUARD

# These are integration types, not identification of a particular device/model.
# Keep setup text generic: the actual native flow owns credentials and pairing.
INTEGRATION_NAMES = {
    "hue": "Philips Hue 燈具與橋接器",
    "cast": "Google Cast 播放裝置",
    "esphome": "ESPHome 裝置",
    "homekit_controller": "HomeKit 裝置",
    "shelly": "Shelly 裝置",
    "tuya": "Tuya / Smart Life 裝置",
    "ipp": "網路印表機",
    "synology_dsm": "Synology 網路儲存設備",
    "matter": "Matter 裝置",
    "home_connect": "Home Connect 家電",
}
SETUP_STEP = "先確認是你要加入的設備，再選擇「協助設定」；可在「接入進度」繼續登入與配對。"
IPP_SETUP_STEP = "選擇「協助設定」查看這台印表機的接入提案；你確認後，系統會先保存家庭設定備份，再接入並讀回結果。"
SHELLY_SETUP_STEP = "選擇「核對型號」；目前聊天接入支援無密碼的 Shelly 1，確認後先備份家庭設定，再接入並讀回結果。"
LIMITATIONS = [
    "這次只讀取家庭系統已發現的項目；沒有掃描整個區域網路，也沒有分享已儲存的網路盤點。",
    "探索結果不是設備控制授權；設定、帳號登入與實體配對仍需由你確認。",
    "已發現的項目可能是橋接器、服務或帳號，不能當成實體設備數量；尚未驗證實體功能。",
    "沒有候選項目不代表家裡沒有設備；網路隔離、休眠及尚未配對都可能影響探索。",
]
ENVIRONMENT_LIMITATIONS = [
    "這次分享 HA 設備與整合、已儲存的網路盤點及家庭網路診斷；沒有啟動新的網路掃描。",
    "設備登錄、整合、探索候選與網路觀察是不同來源；名稱相同也不代表同一台實體設備。",
    "盤點保留原始來源時間；超過 24 小時的資料不提供目前接入建議，匯入清單不證明設備在線。",
    "最多分享八個接入候選，以及各十六筆設備、整合與網路觀察；截短不代表其餘設備不存在。",
    "不分享 IP、MAC、設定密碼或原始匯入內容；這次分享不授予任何設備控制或設定權限。",
    "HA 網路設定就緒不代表探索廣播或實體功能已驗證；沒有候選也不代表家裡沒有設備。",
]
INVENTORY_LABELS = {"lan_neighbors": "已儲存的鄰居表", "lan_mdns": "已儲存的 mDNS 觀察",
                    "lan_ssdp": "已儲存的 SSDP 觀察", "router_import": "已匯入的網路清單"}
NETWORK_STEPS = {
    "not_configured": "家庭目前使用基本網路，尚未接上家中有線設備。可在主機的智能管家選擇「接通家庭網路」，確認後接入。",
    "needs_home_setup": "先完成家庭登入，再重新檢查 HA 的探索網路。",
    "waiting_for_address": "家庭尚未取得可用位址。請檢查接線；主機智能管家的「家庭網路」會持續讀取進度。",
    "adapter_selection_required": "家庭的設備探索設定尚未完成。請在主機智能管家的「家庭網路」確認進度，再繼續探索。",
    "callback_address_required": "家庭本機網址尚未設定完成。請在主機智能管家的「家庭網路」確認進度，再繼續接入。",
    "ready": "家庭網路與探索設定已就緒，可繼續探索設備；探索廣播及實體功能仍需實測。",
    "unavailable": "暫時無法確認家庭網路。可在主機智能管家的「家庭網路」查看進度；其他已取得的觀察仍可使用。",
}


def shared_label(value, *, secrets=()):
    """Project display labels, never embedded addresses, URLs or credential text."""
    if value is None:
        return None
    value = " ".join(value.split())
    for secret in secrets:
        if isinstance(secret, str) and len(secret) > 5:
            value = value.replace(secret, "[已隱藏]")
    value = re.sub(r"(?i)\b(?:https?|wss?)://\S+", "[已隱藏連結]", value)
    value = re.sub(r"(?i)\b(?:bearer\s+.+|(?:password|passwd|secret|token|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*[=:]\s*.+)",
                   "[已隱藏憑證]", value)
    value = re.sub(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])", "[已隱藏位址]", value)
    # Native Shelly names often embed the compact MAC suffix. Treat this
    # bounded hardware-identifier shape like the colon-separated spelling.
    value = re.sub(r"(?i)(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])", "[已隱藏位址]", value)

    def hide_address(match):
        try:
            ipaddress.ip_address(match[0])
        except ValueError:
            return match[0]
        return "[已隱藏位址]"

    value = re.sub(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", hide_address, value)
    value = re.sub(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-f:])", hide_address, value)
    return value[:160] or None


async def network_context(ha, store):
    """Only fixed read-only diagnostics, with no interface or address disclosure."""
    mode = "unknown"
    try:
        async with asyncio.timeout(10):
            raw = await broker_call("GET", "/network/ha-lan", store=store)
            host = HostLanStatus.model_validate(raw).model_dump(mode="json")
            mode = host["mode"]
            home = HomeLanObservation.model_validate(await observe_home_lan(ha, store, host))
        return DiscoveryChatNetwork(state=home.state, mode=mode, observed_at=home.observed_at,
            reason_code=home.reason, next_step=NETWORK_STEPS[home.state])
    except (HTTPException, ValidationError, TimeoutError, OSError):
        return DiscoveryChatNetwork(state="unavailable", mode=mode, observed_at=utcnow(),
            reason_code="network_observation_unavailable", next_step=NETWORK_STEPS["unavailable"])


def complete(source):
    return source.state == "ok" and not source.truncated


def interleave(left, right):
    """Keep both sources represented when the combined candidate list is bounded."""
    return [rows[index] for index in range(max(len(left), len(right)))
            for rows in (left, right) if index < len(rows)]


class ChatDiscoveryAuthority:
    """Bind one MCP read to its original scope, Owner and HA connection."""

    def __init__(self, store, ha, coordinator, token):
        self.store, self.ha, self.coordinator, self.token = store, ha, coordinator, token
        self.owner = store.setting("owner")
        self.connection = tuple(getattr(ha, key, None) for key in ("token", "base", "websocket"))
        self.network_connection = tuple(getattr(ha, key, None) for key in ("supervisor", "external"))
        self.inventory = None

    def guard(self):
        grant = self.coordinator.authorize(self.token)
        if (self.owner != self.store.setting("owner") or self.connection != tuple(
                getattr(self.ha, key, None) for key in ("token", "base", "websocket"))
                or self.network_connection != tuple(getattr(self.ha, key, None) for key in ("supervisor", "external"))):
            raise HTTPException(403, "discovery_authority_changed")
        if self.inventory is not None:
            inventory, binding, revision, content_digest = self.inventory
            current = inventory.view(binding)
            if current.revision != revision or current.content_digest != content_digest:
                raise HTTPException(409, "discovery_inventory_changed")
        return grant

    async def check_owner(self):
        self.guard()
        # Keep two read guards and the response guard inside the MCP deadline,
        # even if HA's ordinary client timeout is longer than discovery's.
        async with asyncio.timeout(5):
            current = await self.ha.owner(self.owner)
        self.guard()
        if not current:
            raise HTTPException(403, "discovery_owner_required")

    async def check_response(self):
        try:
            await self.check_owner()
        except (HAError, TimeoutError):
            raise HTTPException(503, "discovery_owner_unavailable") from None


class DiscoveryChat:
    def __init__(self, app, store, ha, coordinator):
        self.app, self.store, self.ha, self.coordinator = app, store, ha, coordinator

    def save(self, token, result):
        # Re-fetch the message so another tool's task link cannot be overwritten.
        grant = self.coordinator.authorize(token)
        message = self.store.conversation(id=grant["message_id"])
        value = result.model_dump()
        if result.state != "consent_required":
            value["content_digest"] = digest({key: item for key, item in value.items() if key != "content_digest"})
        self.store.put("conversation", {**message, "discovery": value})
        return value

    def environment(self, authority, observed, network):
        """Reuse only the inventory belonging to this verified Owner/HA binding."""
        authority.guard()
        def safe(value):
            return shared_label(value, secrets=authority.connection)
        sources = {source.id: source for source in observed.sources}
        devices = [device.model_copy(update={"name": safe(device.name) or "未命名設備",
            "manufacturer": safe(device.manufacturer), "model": safe(device.model)})
            for device in observed.devices[:16]]
        integrations = [entry.model_copy(update={"title": safe(entry.title) or entry.domain})
                        for entry in observed.integrations[:16]]
        environment = DiscoveryChatEnvironment(devices=devices, integrations=integrations, network=network,
            device_count=len(observed.devices) if complete(sources["ha_devices"]) else None,
            integration_count=len(observed.integrations) if complete(sources["ha_integrations"]) else None,
            truncated=any(source.truncated for source in observed.sources)
                      or len(observed.devices) > 16 or len(observed.integrations) > 16)
        inventory = self.app.state.network_inventory
        binding = inventory.binding(authority)
        try:
            view = inventory.view(binding)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            environment.inventory_sources = [InventorySource(id=identifier, state="unavailable",
                method={"lan_neighbors": "neighbor_table", "lan_mdns": "mdns", "lan_ssdp": "ssdp",
                        "router_import": "json_import"}[identifier], target=label,
                reason_code="saved_inventory_unavailable") for identifier, label in INVENTORY_LABELS.items()]
            return environment, [], False
        authority.inventory = (inventory, binding, view.revision, view.content_digest)
        rows = [DiscoveryChatObservation(id=row.id, source_id=row.source_id, name=safe(row.name),
            services=[safe(value) or "未知服務" for value in row.services], observed_at=row.observed_at,
            source_updated_at=row.source_updated_at, reachability=row.reachability,
            manufacturer=safe(row.metadata.get("manufacturer")), model=safe(row.metadata.get("model")))
            for row in view.observations]
        environment.inventory_observations = rows[:16]
        environment.inventory_sources = [source.model_copy(update={
            "target": INVENTORY_LABELS[source.id],
            "reason_code": safe(source.reason_code),
            "limitations": [safe(value) or "來源限制未知" for value in source.limitations],
            "content_digest": digest([row.model_dump() for row in rows if row.source_id == source.id])
                              if source.content_digest else None,
        }) for source in view.sources]
        known = all(source.state in {"ok", "not_configured"} and not source.truncated for source in view.sources)
        environment.inventory_observation_count = len(rows) if known else None
        environment.inventory_revision, environment.inventory_expires_at = view.revision, view.expires_at
        environment.truncated |= len(rows) > 16 or any(source.truncated for source in view.sources)
        candidates = [DiscoveryChatCandidate(id=item.id, integration_domain=item.integration_domain,
            title=safe(item.title) or "待確認的設備或服務", discovery_method="network_inventory",
            source_id="network_inventory", integration_name=INTEGRATION_NAMES.get(item.integration_domain, "待確認的設備或服務"),
            next_step=safe(item.next_step) or "這是已儲存盤點的接入線索，尚未確認設備身分。先選擇「協助設定」，再依提示登入或配對。")
            for item in view.recommendations]
        return environment, candidates, known

    async def discover(self, token, request=None):
        grant = self.coordinator.authorize(token)
        if request is not None:
            request.scope[RESPONSE_GUARD] = lambda: self.coordinator.authorize(token)
        message = self.store.conversation(id=grant["message_id"])
        # Consent is a typed Web request field, never model arguments or prose.
        # It deliberately does not carry over to a follow-up message/case.
        expanded = message.get("discover_environment") is True
        share_scope = "environment" if expanded else "ha_candidates"
        if message.get("discover_devices") is not True and not expanded:
            return self.save(token, ConversationDiscovery(state="consent_required", limitations=[
                "按「探索家庭設備」後，這次對話可分享接入候選、HA 設備與整合、已儲存網路盤點及網路診斷。",
                "分享範圍會在按鈕旁說明；不分享 IP、MAC 或設定密碼，不會掃描網路、新增設備或改變控制權限。",
            ]))
        authority = ChatDiscoveryAuthority(self.store, self.ha, self.coordinator, token)
        if not authority.owner:
            return self.save(token, ConversationDiscovery(state="unavailable", share_scope=share_scope, limitations=[
                "請先完成家庭登入，再探索可接入的設備。",
            ]))
        try:
            await authority.check_owner()
            if expanded:
                observed, network = await asyncio.gather(snapshot(self.ha), network_context(self.ha, self.store))
                environment, inventory_candidates, inventory_known = self.environment(authority, observed, network)
            else:
                observed = await snapshot(self.ha)
                environment, inventory_candidates, inventory_known = None, [], True
            await authority.check_owner()
        except (HAError, TimeoutError):
            authority.guard()
            return self.save(token, ConversationDiscovery(state="unavailable", share_scope=share_scope, limitations=[
                "暫時無法確認家庭擁有者與連線，請稍後按「重新探索」；已儲存盤點尚未分享，這不代表家裡沒有設備。",
            ]))
        source = next(source for source in observed.sources if source.id == "ha_discovery")
        candidates = []
        for candidate in observed.candidates:
            name = INTEGRATION_NAMES.get(candidate.integration_domain, "待確認的設備或服務")
            title = name if candidate.title == candidate.integration_domain else shared_label(
                candidate.title, secrets=authority.connection) or name
            next_step = SETUP_STEP
            if candidate.source_id == "ha_discovery" and candidate.discovery_method == "zeroconf":
                if candidate.integration_domain == "ipp":
                    next_step = IPP_SETUP_STEP
                elif candidate.integration_domain == "shelly" and candidate.confirmation_step == "confirm_discovery":
                    next_step = SHELLY_SETUP_STEP
            if (candidate.source_id == "ha_discovery" and candidate.integration_domain == "synology_dsm"
                    and candidate.discovery_method in {"ssdp", "zeroconf"} and candidate.confirmation_step == "link"):
                next_step = "選擇「協助設定」，在接入卡輸入 NAS 帳號密碼；帳號密碼不會傳給管家。"
            candidates.append(DiscoveryChatCandidate(**{**candidate.model_dump(), "title": title},
                integration_name=name, next_step=next_step))
        candidates = interleave(candidates, inventory_candidates)
        available = source.state in {"ok", "partial"}
        if environment is not None:
            available |= bool(environment.inventory_observations) or any(
                item.id in {"ha_devices", "ha_integrations"} and item.state in {"ok", "partial"}
                for item in observed.sources) or environment.network.state != "unavailable"
        result = ConversationDiscovery(
            state="ready" if available else "unavailable", share_scope=share_scope,
            observed_at=observed.observed_at, candidates=candidates[:8], sources=[
                item.model_copy(update={"upstream_version": shared_label(item.upstream_version, secrets=authority.connection)})
                for item in observed.sources],
            candidate_count=len(candidates) if complete(source) and inventory_known else None,
            truncated=source.truncated or len(candidates) > 8 or bool(environment and environment.truncated),
            environment=environment, limitations=ENVIRONMENT_LIMITATIONS if expanded else LIMITATIONS,
        )
        authority.guard()
        if request is not None:
            request.scope[RESPONSE_GUARD] = authority.check_response
        return self.save(token, result)
