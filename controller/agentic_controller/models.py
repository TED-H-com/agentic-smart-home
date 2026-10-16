from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EntityId = Annotated[str, Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$", max_length=255)]
ResourceId = Annotated[str, Field(pattern=r"^agentic_[a-f0-9]{32}$")]
HELPERS = frozenset({"input_boolean.agentic_motion_test", "input_boolean.agentic_lamp_test"})


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


PreviewId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class WorkerPreview(Strict):
    """Screenshot stays the default. Interactive carries no artifact bytes."""
    mode: Literal["screenshot", "interactive"] = "screenshot"
    artifact_ids: Annotated[list[PreviewId], Field(max_length=16)] = []

    @model_validator(mode="after")
    def artifacts_match_mode(self):
        if self.mode == "interactive":
            if self.artifact_ids:
                raise ValueError("interactive_preview_has_no_artifacts")
        elif not self.artifact_ids:
            raise ValueError("screenshot_artifacts_required")
        return self


class HomeEntry(Strict):
    """Product-owned navigation contract; never accepts an upstream redirect URL."""
    contract_version: Literal[1] = 1
    state: Literal["ready", "setup", "unavailable", "unsupported"]
    native_path: Literal["/"] = "/"
    reason_code: Literal["setup_incomplete", "home_not_bound", "home_unavailable",
                         "home_identity_changed", "home_contract_changed", "maintenance"] | None = None


class DiscoverySource(Strict):
    id: str
    state: Literal["ok", "partial", "unavailable", "unsupported", "not_implemented"]
    location: Literal["home_assistant", "not_implemented"] = "home_assistant"
    method: Literal["websocket", "rest", "not_implemented"]
    target: str
    adapter_version: Literal[1] = 1
    upstream_version: str | None = None
    observed_at: str | None = None
    item_count: int | None = None
    truncated: bool = False
    reason_code: str | None = None
    content_digest: str | None = None


class DiscoveryCandidate(Strict):
    id: str
    integration_domain: str
    title: str
    discovery_method: str
    source_id: Literal["ha_discovery"] = "ha_discovery"
    native_path: Literal["/config/integrations"] = "/config/integrations"


class DiscoveryDevice(Strict):
    id: str
    name: str
    manufacturer: str | None = None
    model: str | None = None
    via_device_id: str | None = None
    parent_device_id: str | None = None
    integration_ids: list[str] = []
    disabled: bool = False
    entity_count: int | None = None
    available_entity_count: int | None = None
    source_id: Literal["ha_devices"] = "ha_devices"


class DiscoveryIntegration(Strict):
    id: str
    domain: str
    title: str
    state: Literal["loaded", "not_loaded", "retry", "error", "disabled", "ignored", "unknown"]
    source_id: Literal["ha_integrations"] = "ha_integrations"


class DiscoverySnapshot(Strict):
    """Owner-visible observations, separate from entity permissions and physical verification."""
    contract_version: Literal[1] = 1
    observed_at: str
    sources: list[DiscoverySource]
    candidates: list[DiscoveryCandidate]
    devices: list[DiscoveryDevice]
    integrations: list[DiscoveryIntegration]
    native_path: Literal["/config/integrations"] = "/config/integrations"
    limitations: list[str]
    content_digest: str


DiscoveryIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
DiscoveryDomain = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
DiscoveryRevision = Annotated[int, Field(ge=1)]


class DiscoverySetupCreate(Strict):
    """Select a current HA candidate; labels and evidence come from the server."""
    candidate_id: DiscoveryIdentifier
    integration_domain: DiscoveryDomain


class DiscoverySetupSelect(Strict):
    expected_revision: DiscoveryRevision
    entry_id: DiscoveryIdentifier


class DiscoverySetupForget(Strict):
    expected_revision: DiscoveryRevision


class DiscoverySetupReference(Strict):
    """Mutation acknowledgements contain no household labels or observations."""
    contract_version: Literal[1] = 1
    id: PreviewId
    revision: DiscoveryRevision
    forgotten: bool = False


class DiscoverySetup(Strict):
    """Public projection of a private, versioned, household-bound tracking record."""
    contract_version: Literal[1] = 1
    id: PreviewId
    revision: DiscoveryRevision
    candidate: DiscoveryCandidate
    candidate_observed_at: str
    created_at: str
    updated_at: str
    expires_at: str
    selected_entry_id: DiscoveryIdentifier | None = None
    selected_at: str | None = None


class DiscoverySetupList(Strict):
    contract_version: Literal[1] = 1
    setups: Annotated[list[DiscoverySetup], Field(max_length=20)]
    limit: Literal[20] = 20
    retention_days: Annotated[int, Field(ge=1)]


class DiscoverySetupOption(Strict):
    integration: DiscoveryIntegration
    # False means absent from a complete baseline, not "created by this setup".
    known_before: bool | None


class DiscoverySetupView(Strict):
    """Current readback; owner-selected association is not physical identity proof.

    Counts cover the related HA device records and all their registry entities,
    not every entity of the selected integration. No action verification occurs.
    """
    contract_version: Literal[1] = 1
    setup: DiscoverySetup
    observed_at: str
    candidate_state: Literal["present", "not_seen", "unknown"]
    integration_state: Literal["unselected", "present", "not_seen", "unknown"]
    integration: DiscoveryIntegration | None = None
    options: Annotated[list[DiscoverySetupOption], Field(max_length=100)]
    options_truncated: bool = False
    devices: Annotated[list[DiscoveryDevice], Field(max_length=100)]
    devices_truncated: bool = False
    device_count: Annotated[int, Field(ge=0)] | None = None
    entity_count: Annotated[int, Field(ge=0)] | None = None
    available_entity_count: Annotated[int, Field(ge=0)] | None = None
    sources: Annotated[list[DiscoverySource], Field(max_length=16)]
    physical_verification: Literal["not_performed"] = "not_performed"
    native_path: Literal["/config/integrations"] = "/config/integrations"


Runtime = Literal["host", "haos"]
Provider = Literal["codex", "claude", "agy", "grok"]
Channel = Literal["line", "discord", "telegram"]


class GatewayPreferences(Strict):
    primary: Literal["web", "line", "discord", "telegram"] = "line"
    runtime: Runtime = "host"
    provider: Provider = "codex"
    coordinator: Literal["hermes", "direct"] = "hermes"


class ChannelConfig(Strict):
    enabled: bool = False
    secret: Annotated[str, Field(max_length=16384)] = ""
    chats: Annotated[list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]], Field(max_length=20)] = []
    senders: Annotated[list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]], Field(max_length=20)] = []

    @model_validator(mode="after")
    def explicit_audience(self):
        if self.enabled and (not self.chats or not self.senders):
            raise ValueError("啟用前請選擇聊天室和允許使用的人")
        if "\n" in self.secret or "\r" in self.secret:
            raise ValueError("權杖格式無效")
        return self


class ChannelPairingInput(Strict):
    secret: Annotated[str, Field(max_length=16384)] = ""
    chat: Annotated[str, Field(pattern=r"^[ucrs][0-9a-f]{32}$")] | None = None


class ChatInput(Strict):
    message: Annotated[str, Field(min_length=1, max_length=5000)]
    runtime: Runtime = "host"
    provider: Provider = "codex"
    # Existing API clients retain the restricted direct path; the normal Web and
    # channel entry points explicitly select the default Hermes coordinator.
    coordinator: Literal["hermes", "direct"] = "direct"
    reply_to: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    new_conversation: bool = False

    @model_validator(mode="after")
    def one_conversation(self):
        if self.reply_to and self.new_conversation:
            raise ValueError("新對話不能同時接續舊訊息")
        return self


class InnerPair(Strict):
    fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    code: Annotated[str, Field(min_length=32, max_length=100)]


class AgentLogin(Strict):
    method: Literal["device_code", "api_key", "oauth_token"]
    secret: Annotated[str, Field(max_length=16384)] = ""


class OnboardingStep(Strict):
    step: Annotated[int, Field(ge=0, le=4)]


BootstrapStepId = Literal["network", "runner_pairing", "cli_auth", "cli_assist", "hermes_install",
                          "hermes_auth", "channel", "ha_provision", "ha_native"]
BootstrapChannel = Literal["line", "telegram", "discord", "web"]


class BootstrapStart(Strict):
    provider: Provider
    channel: BootstrapChannel = "line"


class BootstrapPreferences(Strict):
    channel: BootstrapChannel


class BootstrapRetry(Strict):
    pass


class HermesDeviceAuth(Strict):
    provider: Literal["openai-codex"] = "openai-codex"
    model: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_./:@-]+$")]


class BootstrapStep(Strict):
    id: BootstrapStepId
    state: Literal["pending", "running", "waiting_input", "blocked", "completed", "skipped"] = "pending"
    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,80}$")] | None = None
    verified_at: str | None = None


class BootstrapHumanAction(Strict):
    kind: Literal["cli_login", "hermes_login", "channel_setup", "ha_native_onboarding", "retry"]
    message: str
    provider: Provider | None = None
    channel: BootstrapChannel | None = None
    url: str | None = None
    code: str | None = None


class BootstrapStatus(Strict):
    contract: Literal[1] = 1
    id: str | None = None
    state: Literal["idle", "running", "waiting_input", "blocked", "completed"] = "idle"
    provider: Provider | None = None
    channel: BootstrapChannel = "line"
    current_step: BootstrapStepId | None = None
    steps: list[BootstrapStep] = []
    human_action: BootstrapHumanAction | None = None
    updated_at: str | None = None


class Trigger(Strict):
    entity_id: EntityId
    from_state: Annotated[str, Field(min_length=1, max_length=64)]
    to_state: Annotated[str, Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def concrete_states(self):
        if self.from_state == self.to_state:
            raise ValueError("觸發前後的狀態必須不同")
        for state in (self.from_state, self.to_state):
            if state in {"unknown", "unavailable"} or re.search(r"[{}\n\r]", state):
                raise ValueError("只接受固定且可觀測的狀態")
        return self


class TimeWindow(Strict):
    after: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$")]
    before: Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$")]


class DeviceAction(Strict):
    entity_id: EntityId
    action: Literal["turn_on", "turn_off"]


class AutomationIntent(Strict):
    title: Annotated[str, Field(min_length=1, max_length=100)]
    resource_id: ResourceId | None = None
    trigger: Trigger
    time_window: TimeWindow | None = None
    actions: Annotated[list[DeviceAction], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def no_feedback(self):
        targets = [a.entity_id for a in self.actions]
        if self.trigger.entity_id in targets or len(targets) != len(set(targets)):
            raise ValueError("觸發來源不可同時是動作目標；目標不可重複")
        return self


class Decision(Strict):
    message: Annotated[str, Field(min_length=1, max_length=8000)]
    automation: AutomationIntent | None = None
    action: DeviceAction | None = None
    inspect_entities: Annotated[list[EntityId], Field(max_length=20)] = []
    needs_user: bool = False

    @model_validator(mode="after")
    def one_intent(self):
        if sum((self.automation is not None, self.action is not None, bool(self.inspect_entities))) > 1:
            raise ValueError("每次只提出一種操作")
        return self


class EntityPermission(Strict):
    entity_id: EntityId
    read: bool
    write: Literal["none", "dedicated_helper", "lighting", "low_risk_switch"] = "none"
    purpose: Annotated[str, Field(max_length=200)] = ""

    @model_validator(mode="after")
    def classification(self):
        if self.write != "none" and not self.read:
            raise ValueError("可操作設備必須同時允許讀取")
        if self.write == "dedicated_helper" and self.entity_id not in HELPERS:
            raise ValueError("只能授權兩個專用測試 helper")
        if self.write == "lighting" and not self.entity_id.startswith("light."):
            raise ValueError("照明授權只適用 light")
        if self.write == "low_risk_switch" and not self.entity_id.startswith("switch."):
            raise ValueError("開關授權只適用 switch")
        if self.write != "none" and len(self.purpose.strip()) < 2:
            raise ValueError("請記錄設備用途")
        return self


class Policy(Strict):
    entities: Annotated[list[EntityPermission], Field(max_length=2000)] = []

    @model_validator(mode="after")
    def unique_entities(self):
        ids = [p.entity_id for p in self.entities]
        if len(set(ids)) != len(ids):
            raise ValueError("設備授權不可重複")
        return self


def compile_automation(intent: AutomationIntent, resource_id: str) -> dict:
    """Compile a finite intent. No templates, arbitrary YAML or indirect actions."""
    return {
        "id": resource_id,
        "alias": intent.title,
        "description": "Managed by Agentic Home · compiler 1",
        "triggers": [{"platform": "state", "entity_id": intent.trigger.entity_id,
                     "from": intent.trigger.from_state, "to": intent.trigger.to_state}],
        "conditions": ([{"condition": "time", **intent.time_window.model_dump()}]
                      if intent.time_window else []),
        "actions": [{"action": f"{a.entity_id.split('.')[0]}.{a.action}",
                    "target": {"entity_id": a.entity_id}} for a in intent.actions],
        "mode": "single",
    }


def provider_schema() -> dict:
    """OpenAI strict structured output requires all object fields to be required."""
    schema = Decision.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema
