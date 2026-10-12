"""Fixed bootstrap assistance; model output never authorizes installation."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Observations(Strict):
    network_ready: bool
    hermes_installed: bool
    hermes_auth_state: Literal["needs_auth", "configured", "ready", "needs_reauth", "unavailable"]
    channel: Literal["line", "telegram", "discord", "web"]
    ha_state: Literal["not_installed", "stopped", "running", "unknown"]


class BootstrapJob(Strict):
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    provider: Literal["codex", "claude", "agy", "grok"]
    stage: Literal["hermes_setup"]
    observations: Observations
    expires: float
    model: Annotated[str, Field(pattern=r"^[a-zA-Z0-9._:/-]{0,100}$")] = ""


class BootstrapResult(Strict):
    decision: Literal["continue", "needs_input", "blocked"]
    next_step: Literal["install_hermes", "authenticate_hermes", "connect_channel", "start_home_assistant", "review_network"]
    reason_codes: Annotated[list[Literal[
        "network_unavailable", "runtime_missing", "hermes_login_required",
        "channel_setup_required", "ha_setup_required", "ready_for_next_stage",
    ]], Field(min_length=1, max_length=6)]


PROMPT = """You are the coding CLI assisting Agentic Smart Home first-run setup.
Read only the supplied typed observations. Do not call tools, execute commands,
read files or request credentials. Return exactly the supplied JSON schema.
The reviewed sequence is network readiness, prepare the official Hermes runtime,
authenticate Hermes in its OWN credential store, connect the selected channel,
then prepare Home Assistant. Hermes authentication is separate from your login.
If network is unavailable, recommend review_network. If Hermes is absent,
recommend install_hermes. If Hermes has no verified inference yet, recommend
authenticate_hermes. Otherwise recommend connect_channel before starting HA.
Your result is advisory: the trusted installer performs fixed operations and
checks actual evidence. Never report installation or household changes as done.
Do not output commands, secrets, URLs, prose, or extra fields.
Observed setup facts:
"""


def prompt(body: BootstrapJob) -> str:
    return PROMPT + json.dumps(body.observations.model_dump(), sort_keys=True)
