"""Stateless Streamable HTTP MCP, on a separate loopback listener and identity."""
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .engine import Rejected
from .ha import HAError
from .household import HomeObserve, HomePropose, HomeStatus
from .models import canonical
from .response_durability import ResponseDurabilityMiddleware
from .store import Conflict
from .work import EvidenceQuery, NoArguments, WorkerQuery, WorkerSubmit
from .workers import WorkerError

TOOLS = {
    "home.observe": (HomeObserve, "Read fresh policy-authorized HA states; API state is not physical verification."),
    "home.propose": (HomePropose, "Validate a precise household proposal for Owner review. This never approves or deploys."),
    "home.proposal_status": (HomeStatus, "Read a household task in this case; cannot approve, apply or control devices."),
    "host.status": (NoArguments, "Read fixed host diagnostics; no HA Core or physical-device claim."),
    "worker.capabilities": (NoArguments, "Read actual readiness and blocking prerequisites; no inference."),
    "worker.submit": (WorkerSubmit, "Request research/development for a fixed worker. Blocked capabilities do not execute."),
    "worker.status": (WorkerQuery, "Read a job within this case; no inference."),
    "worker.cancel": (WorkerQuery, "Request cancellation; stop_unconfirmed must not be treated as stopped."),
    "worker.result": (WorkerQuery, "Read bounded broker findings for this case."),
    "case.evidence.read": (EvidenceQuery, "Read evidence IDs in this case; no paths, URLs, or cross-case queries."),
}


def tool_error(exc):
    """A code the model can tell apart. Never echo validation text, entity ids, or HA bodies."""
    if isinstance(exc, HAError):
        if isinstance(exc.__cause__, TimeoutError) or getattr(exc, "status", None) == 504:
            return "ha_timeout"
        return "ha_unavailable"
    if isinstance(exc, Rejected):
        text = str(exc)
        if text.startswith("尚未授權") or text in {
            "household_owner_required", "household_authority_changed",
            "無法重新確認 HA owner 身份", "無法確認 owner",
        }:
            return "not_permitted"
    if isinstance(exc, WorkerError) and getattr(exc, "status", None) in {401, 403}:
        return "not_permitted"
    return "invalid_or_out_of_scope_request"


def create_mcp(coordinator):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = coordinator.store
    app.add_middleware(ResponseDurabilityMiddleware, store=store)

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
                "annotations": {"readOnlyHint": name not in {"worker.submit", "worker.cancel", "home.propose"},
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
                if name.startswith("home."):
                    household = getattr(coordinator, "household", None)
                    if not household:
                        raise ValueError("household_unavailable")
                    if name == "home.observe":
                        value = await household.observe(arguments, auth[7:])
                    elif name == "home.propose":
                        value = await household.propose(arguments, auth[7:])
                    else:
                        value = household.status(arguments.task_id, grant["case_id"])
                    coordinator.authorize(auth[7:])
                elif name == "host.status":
                    value = await coordinator.host_evidence(grant)
                    coordinator.authorize(auth[7:])
                elif name == "worker.capabilities":
                    value = await coordinator.worker_manager.capabilities()
                    coordinator.authorize(auth[7:])
                elif name == "worker.submit":
                    value = await coordinator.worker_manager.submit(arguments, arguments.idempotency_key, grant=grant,
                        commit_guard=lambda: coordinator.authorize(auth[7:]))
                    coordinator.authorize(auth[7:])
                elif name == "case.evidence.read":
                    store.prune()
                    for evidence_id in arguments.evidence_ids:
                        evidence = store.get("case_evidence", evidence_id)
                        if evidence and evidence.get("job_id"):
                            coordinator.worker_manager.get(evidence["job_id"], grant["case_id"])
                    values = [store.get("case_evidence", id) for id in arguments.evidence_ids]
                    if any(not v or v["case_id"] != grant["case_id"] for v in values):
                        raise ValueError("evidence_not_found_in_case")
                    value = {"evidence": values}
                else:
                    if name == "worker.cancel":
                        value = await coordinator.worker_manager.cancel(arguments.job_id, grant["case_id"])
                    else:
                        value = coordinator.worker_manager.public(coordinator.worker_manager.get(arguments.job_id, grant["case_id"]))
                    coordinator.authorize(auth[7:])
                coordinator.authorize(auth[7:])
                result = {"content": [{"type": "text", "text": canonical(value)}], "isError": False}
            except (ValidationError, ValueError, Conflict, WorkerError, Rejected, HAError) as exc:
                # Validation inputs may contain secrets; never echo Pydantic details to a model.
                result = {"content": [{"type": "text", "text": tool_error(exc)}], "isError": True}
        if result is None:
            return JSONResponse({"jsonrpc": "2.0", "id": identifier,
                                 "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": identifier, "result": result},
                            headers={"Cache-Control": "no-store"})

    return app
