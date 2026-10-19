"""Case-scoped discovery sharing through the existing Hermes MCP boundary.

Only the current Owner message can opt in. The model cannot supply consent,
start a LAN scan, create a config flow, or grant household read/control access.
Results reuse the read-only HA adapter and remain ordinary retained conversation
content; they never become setup approvals or physical verification evidence.
"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from .discovery import snapshot
from .ha import HAError
from .models import ConversationDiscovery, DiscoveryChatCandidate, digest
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
LIMITATIONS = [
    "這次只讀取家庭系統已發現的項目；沒有掃描整個區域網路，也沒有分享已儲存的網路盤點。",
    "探索結果不是設備控制授權；設定、帳號登入與實體配對仍需由你確認。",
    "已發現的項目可能是橋接器、服務或帳號，不能當成實體設備數量；尚未驗證實體功能。",
    "沒有候選項目不代表家裡沒有設備；網路隔離、休眠及尚未配對都可能影響探索。",
]


class ChatDiscoveryAuthority:
    """Bind one MCP read to its original scope, Owner and HA connection."""

    def __init__(self, store, ha, coordinator, token):
        self.store, self.ha, self.coordinator, self.token = store, ha, coordinator, token
        self.owner = store.setting("owner")
        self.connection = tuple(getattr(ha, key, None) for key in ("token", "base", "websocket"))

    def guard(self):
        grant = self.coordinator.authorize(self.token)
        if self.owner != self.store.setting("owner") or self.connection != tuple(
                getattr(self.ha, key, None) for key in ("token", "base", "websocket")):
            raise HTTPException(403, "discovery_authority_changed")
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

    async def discover(self, token, request=None):
        grant = self.coordinator.authorize(token)
        if request is not None:
            request.scope[RESPONSE_GUARD] = lambda: self.coordinator.authorize(token)
        message = self.store.conversation(id=grant["message_id"])
        # Consent is a typed Web request field, never model arguments or prose.
        # It deliberately does not carry over to a follow-up message/case.
        if message.get("discover_devices") is not True:
            return self.save(token, ConversationDiscovery(state="consent_required", limitations=[
                "按「探索家庭設備」後，這次對話會分享最多八項已發現的名稱、來源時間及設定建議給管家。",
                "不讀取 IP、MAC、設定密碼欄位或已儲存的網路盤點；探索不會新增設備或改變控制權限。",
            ]))
        authority = ChatDiscoveryAuthority(self.store, self.ha, self.coordinator, token)
        if not authority.owner:
            return self.save(token, ConversationDiscovery(state="unavailable", limitations=[
                "請先完成家庭登入，再探索可接入的設備。",
            ]))
        try:
            await authority.check_owner()
            observed = await snapshot(self.ha)
            await authority.check_owner()
        except (HAError, TimeoutError):
            authority.guard()
            return self.save(token, ConversationDiscovery(state="unavailable", limitations=[
                "暫時無法確認家庭連線，請稍後按「重新探索」；這不代表家裡沒有設備。",
            ]))
        source = next(source for source in observed.sources if source.id == "ha_discovery")
        candidates = []
        for candidate in observed.candidates[:8]:
            name = INTEGRATION_NAMES.get(candidate.integration_domain, "待確認的設備或服務")
            title = name if candidate.title == candidate.integration_domain else candidate.title
            candidates.append(DiscoveryChatCandidate(**{**candidate.model_dump(), "title": title},
                integration_name=name, next_step=SETUP_STEP))
        result = ConversationDiscovery(
            state="ready" if source.state in {"ok", "partial"} else "unavailable",
            observed_at=observed.observed_at, candidates=candidates, sources=observed.sources,
            candidate_count=len(observed.candidates) if source.state == "ok" and not source.truncated else None,
            truncated=source.truncated or len(observed.candidates) > 8,
            limitations=LIMITATIONS,
        )
        authority.guard()
        if request is not None:
            request.scope[RESPONSE_GUARD] = authority.check_response
        return self.save(token, result)
