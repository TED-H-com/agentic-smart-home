"""Product adapter contract v1 for the pinned, *private* Hermes Python API.

This is the only production module that imports upstream implementation details.
Hermes still owns inference, MCP transport and OAuth; this facade translates and
checks their boundary, not their implementation. A new upstream revision must
also pass compatibility_probe.py: signatures cannot prove behavior or isolation.
All instances live in a single-purpose child process with an isolated home.
"""

import importlib
import inspect
import json
import re

from .models import TOOLS

CONTRACT_VERSION = 1
ISSUER = "https://auth.openai.com"
VERIFICATION_URI = ISSUER + "/codex/device"


class IncompatibleUpstream(RuntimeError):
    """Deliberately excludes exception bodies, tokens and upstream response data."""


def require(condition):
    if not condition:
        raise IncompatibleUpstream("upstream_contract_incompatible")


def guarded_dispatch(dispatch, allowed):
    """Keep this defense even when Hermes rejects an undeclared model tool call."""
    def call(name, *args, **kwargs):
        if name not in allowed:
            return json.dumps({"error": "tool_not_authorized"})
        return dispatch(name, *args, **kwargs)
    return call


def agent_options(*, provider, api_key, base_url, model, session_id):
    return {
        "provider": provider, "api_key": api_key,
        "api_mode": "codex_responses" if provider == "openai-codex" else "chat_completions",
        "base_url": base_url, "model": model, "enabled_toolsets": ["mcp-agentic"],
        "session_id": session_id, "max_iterations": 12, "max_tokens": 4096,
        "quiet_mode": True, "save_trajectories": False, "verbose_logging": False,
        "skip_context_files": True, "skip_memory": True, "skip_background_review": True,
        "load_soul_identity": False, "fallback_model": None, "checkpoints_enabled": False,
        "run_budget_seconds": 290,
    }


class HermesAdapter:
    contract_version = CONTRACT_VERSION

    def __init__(self):
        self._modules = {}

    def _module(self, name):
        if name not in self._modules:
            try:
                self._modules[name] = importlib.import_module(name)
            except Exception:
                raise IncompatibleUpstream("upstream_unavailable") from None
        return self._modules[name]

    def _symbol(self, module, name, *args, **kwargs):
        symbol = getattr(self._module(module), name, None)
        require(callable(symbol))
        try:
            # Full bind detects newly required arguments, unlike bind_partial.
            inspect.signature(symbol).bind(*args, **kwargs)
        except (TypeError, ValueError):
            raise IncompatibleUpstream("upstream_signature_incompatible") from None
        return symbol

    def _auth(self, name, *args, **kwargs):
        return self._symbol("hermes_cli.auth_codex", name, *args, **kwargs)(*args, **kwargs)

    def _client_id(self):
        module = self._module("hermes_cli.auth_codex")
        value = getattr(module, "CODEX_OAUTH_CLIENT_ID", None)
        require(isinstance(value, str) and 0 < len(value) <= 200)
        require(getattr(module, "CODEX_OAUTH_TOKEN_URL", None) == ISSUER + "/oauth/token")
        return value

    def validate_contract(self):
        """No inference or network I/O. Used before the behavioral offline gate."""
        options = agent_options(provider="openai-codex", api_key="fixture", base_url=ISSUER,
                                model="fixture", session_id="fixture")
        agent = self._symbol("run_agent", "AIAgent", **options)
        try:
            inspect.signature(agent.run_conversation).bind(None, "fixture", system_message="fixture",
                                                          conversation_history=[], task_id="fixture")
            inspect.signature(agent.close).bind(None)
        except (AttributeError, TypeError, ValueError):
            raise IncompatibleUpstream("upstream_signature_incompatible") from None
        self._symbol("tools.mcp_tool_discovery", "discover_mcp_tools", allowed_mcp_names=["agentic"])
        self.allowed_tools()
        self._symbol("model_tools", "handle_function_call", "fixture", {})
        client_id = self._client_id()
        for name, args, kwargs in (
            ("_codex_request_device_code", (ISSUER, client_id), {}),
            ("_codex_poll_authorization_code", (ISSUER,),
             {"device_auth_id": "fixture", "user_code": "ABCD-EFGH", "poll_interval": 3}),
            ("_codex_exchange_authorization_code", (ISSUER, client_id, {}), {}),
            ("_save_codex_tokens", ({"access_token": "fixture", "refresh_token": "fixture"},), {}),
            ("_read_codex_tokens", (), {}),
            ("resolve_codex_runtime_credentials", (), {}),
        ):
            self._symbol("hermes_cli.auth_codex", name, *args, **kwargs)
        return {"adapter_contract": CONTRACT_VERSION}

    def allowed_tools(self):
        prefix = self._symbol("tools.mcp_tool_schema", "mcp_prefixed_tool_name", "agentic", "host.status")
        names = {prefix("agentic", name) for name in TOOLS}
        require(names == {"mcp__agentic__" + name.replace(".", "_") for name in TOOLS})
        return names

    @staticmethod
    def _tokens(value):
        require(isinstance(value, dict))
        require(all(isinstance(value.get(key), str) and value[key]
                    for key in ("access_token", "refresh_token")))
        return {key: value[key] for key in ("access_token", "refresh_token")}

    def read_tokens(self):
        value = self._auth("_read_codex_tokens")
        require(isinstance(value, dict))
        self._tokens(value.get("tokens"))
        # Refresh may introduce additional secret fields; keep them in the
        # child's redaction set even though a fresh login stores only two keys.
        return {key: token for key, token in value["tokens"].items() if isinstance(token, str) and token}

    def save_tokens(self, value):
        self._auth("_save_codex_tokens", self._tokens(value))

    def credentials(self):
        value = self._auth("resolve_codex_runtime_credentials")
        require(isinstance(value, dict))
        require(all(isinstance(value.get(key), str) and value[key] for key in ("api_key", "base_url")))
        return value["api_key"], value["base_url"]

    def request_device(self):
        value = self._auth("_codex_request_device_code", ISSUER, self._client_id())
        require(isinstance(value, dict))
        code, device_id, interval = (value.get(key) for key in ("user_code", "device_auth_id", "interval"))
        require(isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{4,8}-[A-Z0-9]{4,8}", code))
        require(isinstance(device_id, str) and 0 < len(device_id) <= 4096)
        require(type(interval) is int and interval >= 0)
        return {"user_code": code, "device_auth_id": device_id, "interval": max(3, min(30, interval))}

    def finish_device(self, device):
        authorization = self._auth("_codex_poll_authorization_code", ISSUER,
            device_auth_id=device["device_auth_id"], user_code=device["user_code"], poll_interval=device["interval"])
        require(isinstance(authorization, dict))
        require(all(isinstance(authorization.get(key), str) and authorization[key]
                    for key in ("authorization_code", "code_verifier")))
        tokens = self._auth("_codex_exchange_authorization_code", ISSUER, self._client_id(), authorization)
        self.save_tokens(tokens)

    def turn(self, *, message, history, run_id, system, **options):
        allowed = self.allowed_tools()
        discovered = self._symbol("tools.mcp_tool_discovery", "discover_mcp_tools",
                                  allowed_mcp_names=["agentic"])(allowed_mcp_names=["agentic"])
        require(isinstance(discovered, (list, set, tuple, dict)))
        require(bool(discovered) and set(discovered) <= allowed)
        module = self._module("model_tools")
        dispatch = self._symbol("model_tools", "handle_function_call", "fixture", {})
        module.handle_function_call = guarded_dispatch(dispatch, allowed)
        agent = None
        try:
            kwargs = agent_options(**options)
            agent = self._symbol("run_agent", "AIAgent", **kwargs)(**kwargs)
            # Private capability flag, checked rather than silently inventing an attribute.
            require(hasattr(agent, "_skip_mcp_refresh"))
            agent._skip_mcp_refresh = True
            names = agent.valid_tool_names
            require(isinstance(names, (set, frozenset)) and bool(names) and names <= allowed)
            result = agent.run_conversation(message, system_message=system,
                                            conversation_history=history, task_id=run_id)
            return self.normalize_result(result, names)
        finally:
            try:
                if agent is not None:
                    agent.close()
            finally:
                module.handle_function_call = dispatch

    @staticmethod
    def normalize_result(result, names):
        require(isinstance(result, dict) and type(result.get("completed")) is bool)
        error = None
        if result.get("failed") or not result["completed"] or result.get("interrupted"):
            reason = str(result.get("error", "")) + str(result.get("turn_exit_reason", ""))
            error = classify_provider_failure(reason)
        answer, history = result.get("final_response", ""), result.get("messages", [])
        require(error is not None or (isinstance(answer, str) and isinstance(history, list)
                                      and all(isinstance(item, dict) for item in history)))
        usage = {key: result.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")}
        require(all(value is None or (type(value) is int and value >= 0) for value in usage.values()))
        return {"answer": answer if error is None else "", "history": history if error is None else [],
                "error": error, "usage": usage, "tools": sorted(names)}


# Known provider tokens only. The raw body is never returned.
_AUTH_TOKENS = ("401", "403", "unauthorized", "authentication", "invalid api key", "relogin")
_RATE_TOKENS = ("429", "quota", "rate limit", "rate_limit", "too many requests", "insufficient_quota")
_TIMEOUT_TOKENS = ("timeout", "timed out", "deadline", "408", "504")
_PROVIDER_TOKENS = ("500", "502", "503", "bad gateway", "service unavailable", "connection", "network")


def classify_provider_failure(reason):
    """Map a provider failure to a stable code. Do not treat a bare 'limit' as rate limiting."""
    text = str(reason or "").lower()
    if any(part in text for part in _AUTH_TOKENS):
        return "needs_reauth"
    if any(part in text for part in _RATE_TOKENS):
        return "rate_limited"
    if any(part in text for part in _TIMEOUT_TOKENS):
        return "timeout"
    if any(part in text for part in _PROVIDER_TOKENS):
        return "provider_unavailable"
    return "inference_failed"
