"""Coding contract and adapter, separate from restricted home_operator execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Annotated, Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from .providers import Providers


class CodingJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    work_contract_major: Literal[1]
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    case_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    worker_id: Literal["host-coder", "home-coder"]
    mode: Literal["research", "development"]
    provider: Literal["codex"]
    model: Annotated[str, Field(max_length=100)] = ""
    repo_id: Literal["agentic-home"]
    goal: Annotated[str, Field(min_length=1, max_length=4000)]
    questions: Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=8)] = []
    base_commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")] | None = None
    expires: float
    lease_epoch: Annotated[int, Field(ge=1, le=1000000)]
    context_days: Annotated[int, Field(ge=1, le=7)] = 7


class CodingCredentials(Providers):
    """Own UID and auth home. Existing operator profiles are never read or copied."""
    def __init__(self, root):
        root.mkdir(mode=0o711, exist_ok=True)
        root.chmod(0o711)
        # Explicitly restore traversal under the service's umask 0077. The
        # provider UID must reach its own 0700 home without listing siblings.
        profiles = root / "profiles"
        profiles.mkdir(mode=0o711, exist_ok=True)
        profiles.chmod(0o711)
        super().__init__(root)

    def uid(self, provider):
        return 1010 + super().uid(provider) - 1001


class CodingAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    method: Literal["device_code", "api_key"]
    secret: Annotated[str, Field(max_length=16384)] = ""


class CodingAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


def save_receipt(path: Path, value):
    temporary = path.with_suffix(".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _job_status_get(method, path):
    ident = path[6:] if path.startswith("/jobs/") else ""
    return method == "GET" and len(ident) == 32 and all(c in "0123456789abcdef" for c in ident)


async def host_request(method, path, body=None, *, timeout=35):
    # Fixed Unix socket, never an address received in a model/browser envelope.
    connector = aiohttp.UnixConnector(path="/run/agentic-home/host.sock")
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)) as client:
        async with client.request(method, "http://host/coding" + path, json=body) as response:
            raw = await response.content.read(262145)
            if len(raw) > 262144:
                raise ValueError("coding_response_limit")
            return response.status, json.loads(raw)


async def backend_request(broker, method, path, body=None):
    if os.environ.get("AGENTIC_HOST_MODE") == "1":
        # Job status is an in-memory snapshot and must not sit behind launch, git or collect.
        return await host_request(method, path, body, timeout=5 if _job_status_get(method, path) else 35)
    service = getattr(broker, "home_coding", None)
    if service is None:
        if method == "GET" and path == "/capabilities":
            return 200, {"work_contract_major": 1, "worker_id": "home-coder", "runtime_location": "haos_app",
                "available": False, "state": "blocked", "modes": [], "max_wall_seconds": 300,
                "reason": "home_coding_initialization_pending", "providers": [{"id": "codex", "supported": False,
                    "models": [], "auth_methods": [], "credential_state": "unavailable"}]}
        return 503, {"detail": "home_coding_initialization_pending"}
    # The service performs bounded Git, process, cgroup and filesystem operations.
    # Keep TLS status/auth and cancellation handlers responsive while those run.
    return await asyncio.to_thread(service.handle, method, "/coding" + path, body)


def routes(app, broker):
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    credentials = CodingCredentials(broker.root / "coding-auth")
    credentials.runtime.update(broker.providers.runtime)
    credentials.write_runtime()
    broker.coding_credentials = credentials
    receipt_path = credentials.root / "private" / "auth-receipts.json"
    receipts = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    auth_guard = asyncio.Lock()
    for receipt in receipts.values():
        if receipt["status"] == "pending":
            receipt.update(status="stop_unconfirmed", code=None, url=None)
            credentials.quarantine("coding_auth_restarted")
    save_receipt(receipt_path, receipts)

    def safe_receipt(id):
        return {k: v for k, v in receipts[id].items() if k != "digest"}

    async def forward(method, path, body=None):
        try:
            status, value = await backend_request(broker, method, path, body)
            return JSONResponse(value, status_code=status)
        except (OSError, ValueError, aiohttp.ClientError, TimeoutError):
            raise HTTPException(503, "coding_broker_unavailable") from None

    @app.get("/coding/capabilities")
    async def capabilities():
        return await forward("GET", "/capabilities")

    @app.post("/coding/reconcile")
    async def reconcile():
        if auth_guard.locked():
            raise HTTPException(409, "coding_auth_in_progress")
        async def external(previous):
            if os.environ.get("AGENTIC_HOST_MODE") != "1" or "container_id" not in previous:
                return False
            try:
                code, evidence = await host_request("POST", "/reconcile-runtime", previous)
                return code == 200 and evidence.get("stop_confirmed") is True
            except (OSError, ValueError, aiohttp.ClientError, TimeoutError):
                return False
        if not await credentials.reconcile(external):
            raise HTTPException(409, "stop_unconfirmed")
        for id, receipt in receipts.items():
            if receipt["status"] != "pending":
                response = await forward("POST", "/auth-release", {"id": id})
                if response.status_code == 200 and receipt["status"] == "stop_unconfirmed":
                    receipt.update(status="interrupted", url=None, code=None)
        save_receipt(receipt_path, receipts)
        return {"stop_confirmed": True, "replayed": False}

    @app.post("/coding/jobs")
    async def submit(body: CodingJob):
        expected = "host-coder" if os.environ.get("AGENTIC_HOST_MODE") == "1" else "home-coder"
        if body.worker_id != expected:
            raise HTTPException(409, "coding_worker_location_mismatch")
        return await forward("POST", "/jobs", body.model_dump())

    @app.get("/coding/jobs/{id}")
    async def job(id: str):
        return await forward("GET", "/jobs/" + validate_id(id))

    @app.post("/coding/jobs/{id}/cancel")
    async def cancel(id: str):
        return await forward("POST", "/jobs/" + validate_id(id) + "/cancel")

    @app.post("/coding/jobs/{id}/handoff")
    async def handoff(id: str):
        return await forward("POST", "/jobs/" + validate_id(id) + "/handoff")

    @app.get("/coding/jobs/{id}/artifacts/{artifact}")
    async def artifact(id: str, artifact: str):
        return await forward("GET", "/jobs/" + validate_id(id) + "/artifacts/" + validate_id(artifact))

    def validate_id(value):
        import re
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise HTTPException(422, "invalid_coding_identity")
        return value

    @app.post("/coding/providers/codex/auth")
    async def login(body: CodingAuth):
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        async with auth_guard:
            if body.id in receipts:
                if receipts[body.id]["digest"] != digest:
                    raise HTTPException(409, "coding_auth_payload_mismatch")
                return safe_receipt(body.id)
            if len(receipts) >= 1000:
                raise HTTPException(507, "coding_auth_ledger_full")
            check = await forward("POST", "/fence", {"id": body.id})
            if check.status_code != 200:
                return check
            receipts[body.id] = {"id": body.id, "status": "pending", "url": None, "code": None,
                                 "expires_in": 900, "digest": digest}
            save_receipt(receipt_path, receipts)
            device_pending = False
            try:
                if body.method == "device_code":
                    await credentials.begin_device_auth(body.id)
                    original = credentials.auth_sessions[body.id]["_task"]

                    async def release_auth_fence(auth_id):
                        # 1, 2, 4 … seconds, capped at 60, for at most 10 minutes.
                        # Retry transport errors and HTTP 5xx only. Stop on success,
                        # any other 4xx, or while a stop is still unconfirmed.
                        delay, deadline = 1, time.monotonic() + 600
                        while not credentials.unconfirmed and time.monotonic() < deadline:
                            try:
                                response = await forward("POST", "/auth-release", {"id": auth_id})
                            except HTTPException as exc:
                                retryable = exc.status_code >= 500
                            else:
                                retryable = response.status_code >= 500
                            if not retryable or credentials.unconfirmed:
                                return
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                return
                            await asyncio.sleep(min(delay, 60, remaining))
                            delay = min(delay * 2, 60)

                    async def release():
                        await asyncio.gather(original, return_exceptions=True)
                        state = credentials.auth_sessions[body.id]
                        receipts[body.id].update(status=state["status"], code=None, url=None)
                        save_receipt(receipt_path, receipts)
                        await release_auth_fence(body.id)
                    credentials.auth_sessions[body.id]["_release"] = asyncio.create_task(release())
                    device_pending = True
                else:
                    result = await credentials.secret_login("codex", body.method, body.secret)
                    receipts[body.id]["status"] = result["status"]
                    save_receipt(receipt_path, receipts)
                return safe_receipt(body.id)
            finally:
                if not device_pending:
                    if receipts[body.id]["status"] == "pending":
                        receipts[body.id]["status"] = "stop_unconfirmed" if credentials.unconfirmed else "failed"
                        save_receipt(receipt_path, receipts)
                    if not credentials.unconfirmed:
                        await forward("POST", "/auth-release", {"id": body.id})

    @app.get("/coding/auth/{id}")
    async def auth_status(id: str):
        validate_id(id)
        if id not in receipts:
            raise HTTPException(404, "unknown_coding_auth")
        value = credentials.auth_sessions.get(id)
        if value:
            receipts[id].update({k: v for k, v in value.items() if k in {"status", "url", "code", "expires_in"}})
            save_receipt(receipt_path, receipts)
        return safe_receipt(id)

    @app.post("/coding/auth/{id}/cancel")
    async def auth_cancel(id: str):
        value = credentials.auth_sessions.get(validate_id(id))
        if not value:
            raise HTTPException(404, "unknown_coding_auth")
        task = value.get("_task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not credentials.unconfirmed:
            value["status"] = "cancelled"
            receipts[id].update(status="cancelled", url=None, code=None)
            save_receipt(receipt_path, receipts)
        return {"id": id, "status": value["status"]}

    @app.post("/coding/providers/codex/logout")
    async def logout(body: CodingAction):
        async with auth_guard:
            if body.id in receipts:
                if receipts[body.id]["digest"] != "logout":
                    raise HTTPException(409, "coding_auth_payload_mismatch")
                return safe_receipt(body.id)
            if len(receipts) >= 1000:
                raise HTTPException(507, "coding_auth_ledger_full")
            check = await forward("POST", "/fence", {"id": body.id})
            if check.status_code != 200:
                return check
            receipts[body.id] = {"id": body.id, "status": "pending", "url": None, "code": None,
                                 "expires_in": 0, "digest": "logout"}
            save_receipt(receipt_path, receipts)
            try:
                await credentials.logout("codex")
                receipts[body.id]["status"] = "disconnected"
                save_receipt(receipt_path, receipts)
                return safe_receipt(body.id)
            finally:
                if receipts[body.id]["status"] == "pending":
                    receipts[body.id]["status"] = "stop_unconfirmed" if credentials.unconfirmed else "failed"
                    save_receipt(receipt_path, receipts)
                if not credentials.unconfirmed:
                    await forward("POST", "/auth-release", {"id": body.id})
