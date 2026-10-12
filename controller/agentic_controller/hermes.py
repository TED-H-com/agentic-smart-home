"""Private Hermes adapter, durable cases, and narrowly scoped MCP capabilities.

No CLI execution or household write authority lives in this module. New record
kinds use data_contract=1 in existing tables; database user_version=2 protects
old binaries from interpreting the new settings and record semantics.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path

import aiohttp
from fastapi import HTTPException, Request

from . import VERSION
from .host import host_call
from .models import digest, utcnow
from .work import HermesAuth

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "needs_auth", "needs_reauth"}


class HermesError(Exception):
    def __init__(self, code="runtime_unavailable"):
        self.code = code
        super().__init__(code)


class HermesClient:
    """The endpoint is installed configuration, never a model or browser input."""
    async def call(self, method, path, body=None, key=None):
        try:
            token = Path(os.environ.get("AGENTIC_HERMES_TOKEN_FILE", "/hermes-link/service-token")).read_text().strip()
            if len(token) < 32:
                raise HermesError()
            headers = {"Authorization": "Bearer " + token}
            if key:
                headers["Idempotency-Key"] = key
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.request(method, "http://127.0.0.1:8096" + path, json=body,
                                           headers=headers, allow_redirects=False) as response:
                    raw = await response.content.read(262145)
                    if len(raw) > 262144:
                        raise HermesError("invalid_response")
                    value = json.loads(raw)
                    if response.status >= 300:
                        code = value.get("detail", {}).get("code") if isinstance(value.get("detail"), dict) else None
                        raise HermesError(code if code in {"needs_auth", "needs_reauth", "not_found",
                            "session_busy", "idempotency_conflict", "scope_mismatch", "stop_unconfirmed",
                            "session_expired", "expired"} else "runtime_unavailable")
                    if not isinstance(value, dict):
                        raise HermesError("invalid_response")
                    return value
        except (OSError, aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
            raise HermesError() from exc


class Coordinator:
    def __init__(self, store, client=None):
        self.store, self.client = store, client or HermesClient()
        self.scope_valid = lambda item: False

    async def capabilities(self):
        try:
            runtime = await self.client.call("GET", "/v1/capabilities")
        except HermesError:
            runtime = {"state": "unavailable", "runtime": "hermes", "auth_verified_at": None}
        return {"runtime": runtime, "workers": self.workers(), "work_contract_major": 1}

    @staticmethod
    def workers():
        return [{"worker_id": name, "work_contract_major": 1, "available": False,
                 "modes": ["research", "development"], "state": "blocked",
                 "observed_from": location, "reason": reason}
                for name, location, reason in (
                    ("host-coder", "host", "Coding sandbox foundation only; provider egress and bounded workspace must pass before dispatch."),
                    ("home-coder", "haos_app", "Requires a real HAOS App coding sandbox and independent containment verification."))]

    def revoke(self, run_id):
        for grant in self.store.list("mcp_grant", 10000):
            if grant["run_id"] == run_id:
                self.store.put("mcp_grant", {**grant, "revoked": True})

    def grant(self, item, case_id, run_id):
        token = secrets.token_urlsafe(48)
        self.store.put("mcp_grant", {"id": hashlib.sha256(token.encode()).hexdigest(), "data_contract": 1,
            "case_id": case_id, "run_id": run_id, "message_id": item["id"], "scope": item["scope"],
            "expires": time.time() + 600, "revoked": False, "created_at": utcnow()})
        return token

    def authorize(self, token):
        if not token or len(token) > 128:
            raise HTTPException(401, "capability_required")
        grant = self.store.get("mcp_grant", hashlib.sha256(token.encode()).hexdigest())
        item = self.store.conversation(id=grant["message_id"]) if grant else None
        if (not grant or grant["revoked"] or grant["expires"] <= time.time() or not item
                or item["scope"] != grant["scope"] or not self.scope_valid(item)):
            raise HTTPException(403, "capability_expired_or_revoked")
        return grant

    async def recover(self, source=None):
        # Invalidate tools first. Query/cancel the exact accepted run; never replay inference.
        pending = []
        for run in self.store.list("hermes_run", 10000):
            if source:
                item = self.store.get("conversation", run.get("message_id", ""))
                if not item or item["source"] != source:
                    continue
            if run["state"] not in TERMINAL:
                self.revoke(run["id"])
                self.store.put("hermes_run", {**run, "state": "stop_unconfirmed", "updated_at": utcnow()})
                pending.append(run)
        semaphore = asyncio.Semaphore(4)
        async def stop(run):
            async with semaphore:
                with contextlib.suppress(HermesError):
                    result = await self.client.call("POST", f'/v1/runs/{run["id"]}/cancel', {"scope_id": run["scope"]})
                    if result.get("state") in TERMINAL:
                        self.store.put("hermes_run", {**run, "state": result["state"], "updated_at": utcnow()})
        # A broken bridge cannot indefinitely block Web startup or owner logout.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(12):
                await asyncio.gather(*(stop(run) for run in pending))

    async def run(self, item):
        case_key = digest([item["scope"], item["thread_id"]])
        case = self.store.get("hermes_case", case_key)
        if not case:
            case = self.store.put("hermes_case", {"id": case_key, "case_id": uuid.uuid4().hex,
                "data_contract": 1, "scope": item["scope"], "created_at": utcnow(), "session_id": None})
        if not case["session_id"]:
            session = await self.client.call("POST", "/v1/sessions", {"scope_id": item["scope"]}, case_key)
            case = self.store.put("hermes_case", {**case, "session_id": session["session_id"]})
        run_id = uuid.uuid4().hex
        run = self.store.put("hermes_run", {"id": run_id, "data_contract": 1, "scope": item["scope"],
            "case_id": case["case_id"], "session_id": case["session_id"], "message_id": item["id"],
            "created_at": utcnow(), "state": "dispatching"})
        item.update(case_id=case["case_id"], hermes_run_id=run_id)
        self.store.put("conversation", item)
        token = self.grant(item, case["case_id"], run_id)
        def authorized():
            try:
                self.authorize(token)
            except HTTPException as exc:
                raise HermesError("scope_mismatch") from exc
        try:
            authorized()
            try:
                result = await self.client.call("POST", f'/v1/sessions/{case["session_id"]}/runs', {
                    "scope_id": item["scope"], "case_id": case["case_id"], "run_id": run_id,
                    "message": item["message"], "mcp_token": token}, run_id)
            except HermesError as exc:
                if exc.code in {"needs_auth", "needs_reauth", "session_busy", "scope_mismatch", "stop_unconfirmed",
                                "session_expired", "expired"}:
                    raise
                # A lost response may have accepted the job. Look up that handle only.
                result = await self.client.call("GET", f'/v1/runs/{run_id}?scope_id={item["scope"]}')
            async with asyncio.timeout(590):
                while result.get("state") not in TERMINAL:
                    authorized()
                    self.store.put("hermes_run", {**run, "state": result.get("state", "running"), "updated_at": utcnow()})
                    await asyncio.sleep(0.5)
                    result = await self.client.call("GET", f'/v1/runs/{run_id}?scope_id={item["scope"]}')
            authorized()
            state = result["state"]
            self.store.put("hermes_run", {**run, "state": state, "updated_at": utcnow()})
            return {"state": "succeeded" if state == "succeeded" else "failed",
                    "answer": str(result.get("answer") or "Hermes 未完成回覆，請檢查登入與服務狀態。")[:16000],
                    "case_id": case["case_id"], "hermes_run_id": run_id}
        except (asyncio.CancelledError, HermesError, TimeoutError, KeyError, TypeError):
            self.revoke(run_id)
            # Stop acknowledgement is deliberately distinct from cancellation intent.
            state = "stop_unconfirmed"
            with contextlib.suppress(HermesError, TimeoutError):
                stopped = await self.client.call("POST", f"/v1/runs/{run_id}/cancel", {"scope_id": item["scope"]})
                if stopped.get("state") in TERMINAL:
                    state = stopped["state"]
            self.store.put("hermes_run", {**run, "state": state, "updated_at": utcnow()})
            raise
        finally:
            self.revoke(run_id)

    async def host_evidence(self, grant):
        data = {"state": "unavailable"}
        with contextlib.suppress(HTTPException):
            raw = await host_call()
            data = {k: v for k, v in raw.items() if k in {"kvm", "provisioned", "storage_free_bytes"}
                    and isinstance(v, (bool, int))}
            if raw.get("vm") in {"running", "shut off", "paused", "not_created", "unknown"}:
                data["vm"] = raw["vm"]
        evidence_id = uuid.uuid4().hex
        evidence = {"id": evidence_id, "evidence_id": evidence_id, "data_contract": 1, "case_id": grant["case_id"],
            "observed_from": "host_broker", "target_component": "host_and_managed_vm",
            "collection_method": "fixed_broker_status", "observed_at": utcnow(),
            "version_refs": {"controller": VERSION}, "content_digest": digest(data),
            "redaction": "field_allowlist", "limitations": ["Does not establish HA Core or physical device health."],
            "facts": data}
        self.store.put("case_evidence", evidence)
        return evidence


def routes(app, coordinator, store):
    @app.get("/api/hermes")
    async def status():
        return await coordinator.capabilities()

    @app.post("/api/hermes/auth")
    async def auth(body: HermesAuth):
        # Rotating model/credentials also cuts off the previous provider's context.
        store.set_setting("hermes_auth_epoch", uuid.uuid4().hex)
        try:
            return await coordinator.client.call("POST", "/v1/auth", {
                "provider": body.provider, "model": body.model, "api_key": body.api_key.get_secret_value()})
        except HermesError as exc:
            raise HTTPException(503, "Hermes 登入服務尚未就緒") from exc

    @app.post("/api/hermes/auth/logout")
    async def logout():
        store.set_setting("hermes_auth_epoch", uuid.uuid4().hex)
        await coordinator.recover()
        try:
            return await coordinator.client.call("POST", "/v1/auth/logout", {})
        except HermesError as exc:
            raise HTTPException(503, "已撤銷工具權限；請恢復 Hermes 服務後再次登出以清除其憑證") from exc

    @app.get("/api/hermes/cases")
    async def cases():
        return {"cases": [{k: v for k, v in item.items() if k not in {"scope", "session_id", "id"}}
                          for item in store.list("hermes_case", 100)],
                "runs": [{k: v for k, v in item.items() if k not in {"scope", "session_id"}}
                         for item in store.list("hermes_run", 100)]}

    @app.post("/api/hermes/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        run = store.get("hermes_run", run_id)
        if not run:
            raise HTTPException(404, "找不到此工作")
        coordinator.revoke(run_id)
        state = "stop_unconfirmed"
        with contextlib.suppress(HermesError):
            result = await coordinator.client.call("POST", f"/v1/runs/{run_id}/cancel", {"scope_id": run["scope"]})
            if result.get("state") in TERMINAL:
                state = result["state"]
        store.put("hermes_run", {**run, "state": state, "updated_at": utcnow()})
        return {"run_id": run_id, "state": state}
