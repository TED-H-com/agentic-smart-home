"""Versioned worker inputs. Execution authority never comes from a model field."""
from typing import Annotated, Literal

from pydantic import Field, SecretStr

from .models import Strict

WorkerId = Literal["host-coder", "home-coder"]
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class HermesAuth(Strict):
    provider: Literal["openrouter"] = "openrouter"
    model: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_./:@-]+$")]
    api_key: SecretStr = Field(min_length=8, max_length=4096)


class WorkerSubmit(Strict):
    work_contract_major: Literal[1] = 1
    worker_id: WorkerId
    mode: Literal["research", "development"]
    idempotency_key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{8,100}$")]
    goal: Annotated[str, Field(min_length=1, max_length=4000)]
    questions: Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=8)] = []
    repo_id: Literal["agentic-home"] = "agentic-home"
    # No URL, command, mount, lease, approval, credential, or caller-selected endpoint.


class WorkerQuery(Strict):
    job_id: Identifier


class EvidenceQuery(Strict):
    evidence_ids: Annotated[list[Identifier], Field(min_length=1, max_length=8)]


class NoArguments(Strict):
    pass
