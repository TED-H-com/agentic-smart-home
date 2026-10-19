"""Private Hermes adapter, durable cases, and narrowly scoped MCP capabilities.

No CLI execution or household write authority lives in this module. New record
kinds use data_contract=1 in existing tables; database user_version=3 protects
old binaries from interpreting the new settings and record semantics.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import get_args

import aiohttp
from fastapi import HTTPException, Request

from . import VERSION
from .durability import client_session
from .host import host_call
from .models import BootstrapRetry, BootstrapStepId, HermesDeviceAuth, canonical, digest, utcnow
from .runner import Runner
from .work import HermesAuth
from .workers import Workers

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "needs_auth", "needs_reauth"}
_PASSTHROUGH = {"needs_auth", "needs_reauth", "not_found", "session_busy", "idempotency_conflict",
                "scope_mismatch", "stop_unconfirmed", "session_expired", "expired", "access_denied",
                "slow_down", "expired_token", "oauth_failed", "rate_limited", "provider_unavailable", "timeout"}
NEXT_STEP = {
    "needs_auth": "請先登入智能管家。",
    "needs_reauth": "登入已失效，請重新登入原帳號後再送出。",
    "session_busy": "智能管家正在處理另一則訊息，請等它完成後再送出。",
    "session_expired": "這段對話已到期，請開始新對話。",
    "expired": "這段對話已到期，請開始新對話。",
    "rate_limited": "供應商暫時限制請求，請稍後再送出同一則訊息。",
    "provider_unavailable": "模型服務暫時無法連線，請稍後再試。",
    "timeout": "這次回覆逾時，請稍後再送出。",
    "runtime_unavailable": "智能管家暫時無法連線，請稍後再試。",
    "inference_failed": "這次回覆未完成，請稍後再送出。",
    "stop_unconfirmed": "還在確認上一則回覆已停止，確認前不能送出新訊息。",
}
_LOGIN_HTTP = {
    "stop_unconfirmed": (409, "尚未確認前一次登入程序已停止，請重新檢查狀態。"),
    "access_denied": (409, "官方登入被拒絕，請重新開始授權。"),
    "slow_down": (429, "官方登入暫時忙碌，請稍後再試，不要重複開啟授權。"),
    "expired_token": (409, "官方授權已過期，請重新開始。"),
    "oauth_failed": (409, "官方登入未完成，請重新檢查後再試。"),
}


def raise_login(exc, fallback):
    if exc.code in _LOGIN_HTTP:
        status, message = _LOGIN_HTTP[exc.code]
        raise HTTPException(status, {"code": exc.code, "message": message, "reason": exc.code}) from exc
    raise HTTPException(503, fallback) from exc


class HermesError(Exception):
    def __init__(self, code="runtime_unavailable"):
        self.code = code
        super().__init__(code)


class HermesClient:
    """The endpoint is installed configuration, never a model or browser input."""
    def __init__(self, store):
        self.store = store

    async def call(self, method, path, body=None, key=None):
        try:
            token = Path(os.environ.get("AGENTIC_HERMES_TOKEN_FILE", "/hermes-link/service-token")).read_text().strip()
            if len(token) < 32:
                raise HermesError()
            headers = {"Authorization": "Bearer " + token}
            if key:
                headers["Idempotency-Key"] = key
            async with client_session(self.store, timeout=aiohttp.ClientTimeout(total=12)) as session:
                async with session.request(method, "http://127.0.0.1:8096" + path, json=body,
                                           headers=headers, allow_redirects=False) as response:
                    raw = await response.content.read(262145)
                    if len(raw) > 262144:
                        raise HermesError("invalid_response")
                    value = json.loads(raw)
                    if response.status >= 300:
                        code = value.get("detail", {}).get("code") if isinstance(value.get("detail"), dict) else None
                        raise HermesError(code if code in _PASSTHROUGH else "runtime_unavailable")
                    if not isinstance(value, dict):
                        raise HermesError("invalid_response")
                    return value
        except (OSError, aiohttp.ClientError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
            raise HermesError() from exc


class Coordinator:
    def __init__(self, store, client=None):
        self.store, self.client = store, client or HermesClient(store)
        if isinstance(self.client, HermesClient):
            self.client.store = store
        self.scope_valid = lambda item: False
        self.worker_manager = Workers(store, {"host": Runner(store), "haos": Runner(store, "haos")}, self)

    async def capabilities(self):
        try:
            runtime = await self.client.call("GET", "/v1/capabilities")
        except HermesError:
            runtime = {"state": "unavailable", "runtime": "hermes", "auth_verified_at": None}
        capabilities = await self.worker_manager.capabilities()
        return {"runtime": runtime, **capabilities}

    def workers(self):
        return self.worker_manager.public_capabilities()["workers"]

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

    async def recover(self, source=None, *, message_ids=None):
        # Invalidate tools first. Query/cancel the exact accepted run; never replay inference.
        pending = []
        for run in self.store.list("hermes_run", 10000):
            if message_ids is not None and run.get("message_id") not in message_ids:
                continue
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
                jobs = self.worker_manager.case_jobs(case["case_id"])
                message = item["message"]
                if jobs:
                    message += "\n\nController same-case work handles (status only; inspect worker.result for evidence; this is not approval):\n" + canonical(jobs)
                result = await self.client.call("POST", f'/v1/sessions/{case["session_id"]}/runs', {
                    "scope_id": item["scope"], "case_id": case["case_id"], "run_id": run_id,
                    "message": message, "mcp_token": token}, run_id)
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
            error = result.get("error") if state != "succeeded" else None
            if not isinstance(error, str) or error not in NEXT_STEP:
                error = None
            if state == "succeeded":
                answer = str(result.get("answer") or "")
            else:
                answer = NEXT_STEP.get(error) or "智能管家沒有完成回覆，請確認登入狀態後再送出。"
            response = {"state": "succeeded" if state == "succeeded" else "failed",
                    "answer": answer[:16000], "case_id": case["case_id"], "hermes_run_id": run_id}
            if error:
                response.update(error_code=error, reason=error)
            linked = self.store.get("conversation", item["id"]) or {}
            if linked.get("discovery"):
                response["discovery"] = linked["discovery"]
            if linked.get("task_id"):
                response["task_id"] = linked["task_id"]
                task = self.store.get("task", linked["task_id"]) or {}
                if task.get("state") == "awaiting_approval":
                    response.update(state="awaiting_approval", answer="已準備家庭操作提案。請開啟 Web 工作台檢查內容並批准；目前尚未執行。")
            return response
        except (asyncio.CancelledError, HermesError, TimeoutError, KeyError, TypeError) as exc:
            self.revoke(run_id)
            # Stop acknowledgement is deliberately distinct from cancellation intent.
            state = "stop_unconfirmed"
            with contextlib.suppress(HermesError, TimeoutError):
                stopped = await self.client.call("POST", f"/v1/runs/{run_id}/cancel", {"scope_id": item["scope"]})
                if stopped.get("state") in TERMINAL:
                    state = stopped["state"]
            self.store.put("hermes_run", {**run, "state": state, "updated_at": utcnow()})
            if isinstance(exc, TimeoutError):
                raise HermesError("timeout") from exc
            raise
        finally:
            self.revoke(run_id)

    async def host_evidence(self, grant):
        data = {"state": "unavailable"}
        with contextlib.suppress(HTTPException):
            raw = await host_call(store=self.store)
            data = {k: v for k, v in raw.items() if k in {"kvm", "provisioned", "storage_free_bytes"}
                    and isinstance(v, (bool, int))}
            if raw.get("vm") in {"running", "shut off", "paused", "not_created", "unknown"}:
                data["vm"] = raw["vm"]
            platform = raw.get("bootstrap", {})
            native = platform.get("ha", {}) if isinstance(platform, dict) else {}
            if isinstance(native, dict) and type(native.get("reachable")) is bool:
                setup = {key: native[key] for key in ("reachable", "onboarding_available", "onboarding_complete")
                         if type(native.get(key)) is bool}
                setup["observed_from"] = "outer_host_to_core_onboarding_api"
                setup["steps"] = [{"step": item["step"], "done": item["done"]}
                    for item in native.get("steps", [])[:10] if isinstance(item, dict)
                    and isinstance(item.get("step"), str)
                    and item.get("step") in {"user", "core_config", "integration", "analytics"}
                    and type(item.get("done")) is bool] if isinstance(native.get("steps"), list) else []
                if type(native.get("http_status")) is int and native["http_status"] in {200, 401, 404}:
                    setup["http_status"] = native["http_status"]
                data["ha_onboarding"] = setup
            bootstrap = self.store.get("bootstrap_v1", "setup") or {}
            if bootstrap.get("current_step") in get_args(BootstrapStepId):
                data["setup_step"] = bootstrap["current_step"]
        evidence_id = uuid.uuid4().hex
        evidence = {"id": evidence_id, "evidence_id": evidence_id, "data_contract": 1, "case_id": grant["case_id"],
            "observed_from": "host_broker", "target_component": "host_and_managed_vm",
            "collection_method": "fixed_broker_status", "observed_at": utcnow(),
            "version_refs": {"controller": VERSION}, "content_digest": digest(data),
            "redaction": "field_allowlist", "limitations": ["Native onboarding observations do not establish HA owner authority or physical device health."],
            "facts": data}
        self.store.put("case_evidence", evidence)
        return evidence


def routes(app, coordinator, store):
    @app.get("/api/hermes")
    async def status():
        return await coordinator.capabilities()

    @app.post("/api/hermes/auth/device", status_code=202)
    async def device_auth(body: HermesDeviceAuth, request: Request):
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise HTTPException(400, "缺少有效的 Idempotency-Key")
        previous = store.request("hermes_device_auth", key, digest(body.model_dump()))
        if not previous:
            try:
                runtime = await coordinator.client.call("GET", "/v1/capabilities")
            except HermesError as exc:
                raise_login(exc, "Hermes 官方登入服務尚未就緒；重試請沿用同一識別碼")
            pending = runtime.get("pending_auth") if isinstance(runtime, dict) else None
            if isinstance(pending, dict) and pending.get("state") == "pending" and pending.get("auth_id"):
                store.request("hermes_device_auth", key, digest(body.model_dump()), str(pending["auth_id"]))
                return pending
            store.set_setting("hermes_auth_epoch", uuid.uuid4().hex)
            store.request("hermes_device_auth", key, digest(body.model_dump()), "started")
            await coordinator.recover()
        try:
            return await coordinator.client.call("POST", "/v1/auth/device", body.model_dump(), key)
        except HermesError as exc:
            raise_login(exc, "Hermes 官方登入服務尚未就緒；重試請沿用同一識別碼")

    @app.get("/api/hermes/auth/device/{auth_id}")
    async def device_auth_status(auth_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", auth_id):
            raise HTTPException(422, "無效登入 ID")
        try:
            return await coordinator.client.call("GET", "/v1/auth/device/" + auth_id)
        except HermesError as exc:
            raise_login(exc, "Hermes 登入狀態暫時無法確認")

    @app.post("/api/hermes/auth/device/{auth_id}/cancel")
    async def device_auth_cancel(auth_id: str, body: BootstrapRetry):
        if not re.fullmatch(r"[a-f0-9]{32}", auth_id):
            raise HTTPException(422, "無效登入 ID")
        try:
            return await coordinator.client.call("POST", "/v1/auth/device/" + auth_id + "/cancel", {})
        except HermesError as exc:
            raise_login(exc, "Hermes 登入停止尚未確認")

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

    def case_view(item):
        return {**{k: v for k, v in item.items() if k not in {"scope", "session_id", "id"}},
                "jobs": coordinator.worker_manager.case_jobs(item["case_id"])}

    @app.get("/api/hermes/cases")
    async def cases():
        store.prune()
        return {"cases": [case_view(item) for item in store.list("hermes_case", 100)],
                "runs": [{k: v for k, v in item.items() if k not in {"scope", "session_id"}}
                         for item in store.list("hermes_run", 100)]}

    @app.get("/api/hermes/cases/{case_id}")
    async def case_status(case_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", case_id):
            raise HTTPException(422, "invalid_case_id")
        store.prune()
        item = next((value for value in store.list("hermes_case", 10000) if value["case_id"] == case_id), None)
        if not item:
            raise HTTPException(404, "case_not_found_or_expired")
        return case_view(item)

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
