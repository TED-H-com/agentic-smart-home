from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Scope = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]

TOOLS = (
    "host.status", "worker.capabilities", "worker.submit", "worker.status",
    "worker.cancel", "worker.result", "case.evidence.read",
    "home.observe", "home.propose", "home.proposal_status",
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuthInput(Strict):
    provider: Literal["openrouter"]
    model: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_./:@-]+$")]
    api_key: SecretStr = Field(min_length=8, max_length=4096)


class DeviceAuthInput(Strict):
    provider: Literal["openai-codex"]
    model: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_./:@-]+$")]


class SessionInput(Strict):
    scope_id: Scope


class RetentionInput(Strict):
    context_days: Annotated[int, Field(strict=True, ge=1, le=7)]


class RunInput(SessionInput):
    run_id: Identifier
    case_id: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]
    message: Annotated[str, Field(min_length=1, max_length=16000)]
    mcp_token: SecretStr = Field(min_length=16, max_length=4096)
