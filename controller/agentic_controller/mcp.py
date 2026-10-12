"""Stateless Streamable HTTP MCP, on a separate loopback listener and identity."""
import json
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .models import canonical, digest, utcnow
from .store import Conflict
from .work import EvidenceQuery, NoArguments, WorkerQuery, WorkerSubmit

TOOLS = {
    "host.status": (NoArguments, "Read fixed host diagnostics; no HA Core or physical-device claim."),
    "worker.capabilities": (NoArguments, "Read actual readiness and blocking prerequisites; no inference."),
    "worker.submit": (WorkerSubmit, "Request research/development for a fixed worker. Blocked capabilities do not execute."),
    "worker.status": (WorkerQuery, "Read a job within this case; no inference."),
    "worker.cancel": (WorkerQuery, "Request cancellation; stop_unconfirmed must not be treated as stopped."),
    "worker.result": (WorkerQuery, "Read bounded broker findings for this case."),
    "case.evidence.read": (EvidenceQuery, "Read evidence IDs in this case; no paths, URLs, or cross-case queries."),
}


def create_mcp(coordinator):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = coordinator.store

    @app.post("/mcp")
    async def rpc(request: Request):
        if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
                or request.headers.get("host") not in {"127.0.0.1:8097", "localhost:8097"}
                or request.headers.get("origin")):
            raise HTTPException(403, "private_service_only")
        auth = request.headers.get("authorization", "")
        grant = coordinator.authorize(auth[7:] if auth.startswith("Bearer ") else "")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, "request_too_large")
        # Streaming may yield to logout, cancellation, or another tool call.
        # Recheck authority and budget from the current committed grant.
        grant = coordinator.authorize(auth[7:])
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or isinstance(body.get("id"), (dict, list, bool)):
            raise HTTPException(400, "invalid_request")
        identifier, method = body.get("id"), body.get("method")
        if identifier is not None and (not isinstance(identifier, (int, str)) or len(str(identifier)) > 128):
            raise HTTPException(400, "invalid_request_id")
        if method == "notifications/initialized" and identifier is None:
            return Response(status_code=202)
        result = None
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "agentic-home-controller", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": description, "inputSchema": model.model_json_schema(),
                "annotations": {"readOnlyHint": name not in {"worker.submit", "worker.cancel"},
                                "openWorldHint": False}}
                for name, (model, description) in TOOLS.items()]}
        elif method == "tools/call":
            params = body.get("params", {})
            try:
                if not isinstance(params, dict) or set(params) - {"name", "arguments", "_meta"}:
                    raise ValueError("invalid_parameters")
                name = params.get("name")
                if not isinstance(name, str) or name not in TOOLS:
                    raise ValueError("unknown_tool")
                count = grant.get("calls", 0)
                if count >= 64:
                    raise ValueError("tool_budget_exhausted")
                store.put("mcp_grant", {**grant, "calls": count + 1})
                arguments = TOOLS[name][0].model_validate(params.get("arguments", {}))
                if name == "host.status":
                    value = await coordinator.host_evidence(grant)
                    coordinator.authorize(auth[7:])
                elif name == "worker.capabilities":
                    value = {"work_contract_major": 1, "workers": coordinator.workers()}
                elif name == "worker.submit":
                    payload = arguments.model_dump()
                    key = arguments.idempotency_key
                    prior = store.request("worker:" + grant["case_id"], key, digest(payload))
                    if prior:
                        value = store.get("work_job", prior)
                    else:
                        if sum(j["case_id"] == grant["case_id"] for j in store.list("work_job", 10000)) >= 4:
                            raise ValueError("case_job_budget_exhausted")
                        capability = next(c for c in coordinator.workers() if c["worker_id"] == arguments.worker_id)
                        job_id = uuid.uuid4().hex
                        value = {"id": job_id, "job_id": job_id, "case_id": grant["case_id"], "data_contract": 1,
                            "work_contract_major": 1, "worker_id": arguments.worker_id, "mode": arguments.mode,
                            "state": "blocked", "created_at": utcnow(), "reason": capability["reason"],
                            "execution_started": False, "artifacts": [], "facts": [], "inferences": [],
                            "unknowns": ["Worker execution is not enabled."], "experiments": []}
                        # A single SQLite transaction makes handle and deduplication tombstone inseparable.
                        with store.db:
                            now = utcnow()
                            store.db.execute("INSERT INTO records VALUES (?,?,?,?,?)",
                                             ("work_job", value["id"], canonical(value), now, now))
                            store.db.execute("INSERT INTO requests VALUES (?,?,?,?)",
                                             ("worker:" + grant["case_id"], key, digest(payload), value["id"]))
                elif name == "case.evidence.read":
                    values = [store.get("case_evidence", id) for id in arguments.evidence_ids]
                    if any(not v or v["case_id"] != grant["case_id"] for v in values):
                        raise ValueError("evidence_not_found_in_case")
                    value = {"evidence": values}
                else:
                    value = store.get("work_job", arguments.job_id)
                    if not value or value["case_id"] != grant["case_id"]:
                        raise ValueError("job_not_found_in_case")
                    # Currently only blocked jobs exist: nothing was launched, no lease to hand off.
                    if name == "worker.cancel":
                        value = store.put("work_job", {**value, "state": "cancelled", "execution_started": False})
                result = {"content": [{"type": "text", "text": canonical(value)}], "isError": False}
            except (ValidationError, ValueError, Conflict):
                # Validation inputs may contain secrets; never echo Pydantic details to a model.
                result = {"content": [{"type": "text", "text": "invalid_or_out_of_scope_request"}], "isError": True}
        if result is None:
            return JSONResponse({"jsonrpc": "2.0", "id": identifier,
                                 "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": identifier, "result": result},
                            headers={"Cache-Control": "no-store"})

    return app
