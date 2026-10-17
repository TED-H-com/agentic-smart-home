"""Private upstream contract failures must never become an activation success."""

import importlib
import json
import os
import subprocess
import time
import types
import uuid
from pathlib import Path

import pytest
from agentic_hermes.upstream import HermesAdapter, IncompatibleUpstream


@pytest.fixture
def upstream(monkeypatch):
    state = {"closed": 0, "saved": [], "executed": [], "result": {
        "completed": True, "final_response": "fixture result", "messages": [{"role": "assistant", "content": "fixture result"}],
    }}

    class Agent:
        def __init__(self, **kwargs):
            self._skip_mcp_refresh = False
            self.valid_tool_names = {"mcp__agentic__host_status"}
            state["options"] = kwargs

        def run_conversation(self, message, *, system_message, conversation_history, task_id):
            return state["result"]

        def close(self):
            state["closed"] += 1

    def dispatch(name, arguments):
        state["executed"].append(name)

    def poll(issuer, *, device_auth_id, user_code, poll_interval):
        return {"authorization_code": "fixture-private-code", "code_verifier": "fixture-private-verifier"}

    def download(force_refresh=False):
        raise AssertionError("catalog download")

    modules = {
        "agent.model_metadata": types.SimpleNamespace(fetch_model_metadata=download, _model_metadata_cache={},
                                                      _model_metadata_cache_time=0, _MODEL_CACHE_TTL=3600),
        "run_agent": types.SimpleNamespace(AIAgent=Agent),
        "model_tools": types.SimpleNamespace(handle_function_call=dispatch),
        "tools.mcp_tool_discovery": types.SimpleNamespace(discover_mcp_tools=lambda *, allowed_mcp_names: ["mcp__agentic__host_status"]),
        "tools.mcp_tool_schema": types.SimpleNamespace(mcp_prefixed_tool_name=lambda server, name: "mcp__" + server + "__" + name.replace(".", "_")),
        "hermes_cli.auth_codex": types.SimpleNamespace(
            CODEX_OAUTH_CLIENT_ID="fixture-client", CODEX_OAUTH_TOKEN_URL="https://auth.openai.com/oauth/token",
            _codex_request_device_code=lambda issuer, client: {"device_auth_id": "fixture-private-device", "user_code": "ABCD-EFGH", "interval": 3},
            _codex_poll_authorization_code=poll,
            _codex_exchange_authorization_code=lambda issuer, client, code: {"access_token": "fixture-private-access", "refresh_token": "fixture-private-refresh"},
            _save_codex_tokens=lambda tokens: state["saved"].append(tokens),
            _read_codex_tokens=lambda: {"tokens": {"access_token": "fixture-private-access", "refresh_token": "fixture-private-refresh"}},
            resolve_codex_runtime_credentials=lambda: {"api_key": "fixture-private-access", "base_url": "https://chatgpt.com/backend-api/codex"},
        ),
    }
    original_import = importlib.import_module
    monkeypatch.setattr(importlib, "import_module", lambda name: modules[name] if name in modules else original_import(name))
    return modules, state


def turn(adapter):
    return adapter.turn(provider="openrouter", api_key="fixture-private-key", base_url="http://127.0.0.1",
                        model="fixture", session_id="fixture", message="fixture", history=[], run_id="fixture", system="fixture")


def test_complete_consumed_contract_and_lifecycle(upstream):
    modules, state = upstream
    adapter = HermesAdapter()
    assert adapter.validate_contract() == {"adapter_contract": 1}
    dispatch = modules["model_tools"].handle_function_call
    assert turn(adapter)["answer"] == "fixture result"
    assert state["closed"] == 1 and modules["model_tools"].handle_function_call is dispatch
    assert state["options"]["enabled_toolsets"] == ["mcp-agentic"]
    assert state["options"]["skip_memory"] and state["options"]["skip_background_review"]
    assert not state["options"]["load_soul_identity"] and state["options"]["fallback_model"] is None
    # The turn's catalog is a fresh in-process cache, so pricing never downloads one.
    catalog = modules["agent.model_metadata"]
    assert catalog._model_metadata_cache and time.time() - catalog._model_metadata_cache_time < 5
    adapter.finish_device(adapter.request_device())
    assert len(state["saved"]) == 1


@pytest.mark.parametrize("failure", ["missing_module", "missing_symbol", "not_callable", "new_required_parameter", "removed_parameter", "changed_tool_mapping", "changed_issuer", "changed_catalog_cache"])
def test_incompatible_upstream_rejected_without_execution(upstream, monkeypatch, failure):
    modules, state = upstream
    if failure == "missing_module":
        def missing(name):
            raise ImportError("fixture-private-upstream-secret")
        monkeypatch.setattr(importlib, "import_module", missing)
    elif failure == "missing_symbol":
        del modules["hermes_cli.auth_codex"]._read_codex_tokens
    elif failure == "not_callable":
        modules["run_agent"].AIAgent = "fixture-private-upstream-secret"
    elif failure == "new_required_parameter":
        modules["hermes_cli.auth_codex"]._read_codex_tokens = lambda required_new_argument: {}
    elif failure == "removed_parameter":
        modules["tools.mcp_tool_discovery"].discover_mcp_tools = lambda: []
    elif failure == "changed_tool_mapping":
        modules["tools.mcp_tool_schema"].mcp_prefixed_tool_name = lambda server, name: name
    elif failure == "changed_catalog_cache":
        del modules["agent.model_metadata"]._model_metadata_cache
    else:
        modules["hermes_cli.auth_codex"].CODEX_OAUTH_TOKEN_URL = "https://other.invalid/token"
    with pytest.raises(IncompatibleUpstream) as error:
        HermesAdapter().validate_contract()
    assert "fixture-private" not in str(error.value)
    assert state["closed"] == 0 and not state["executed"] and not state["saved"]


@pytest.mark.parametrize("result", [None, [], {}, {"completed": "true"},
    {"completed": True, "final_response": {"secret": "fixture-private-upstream-secret"}},
    {"completed": True, "messages": {}}, {"completed": True, "messages": ["malformed"]},
    {"completed": True, "input_tokens": True}, {"completed": True, "total_tokens": -1}])
def test_malformed_agent_result_fails_closed_and_closes_agent(upstream, result):
    modules, state = upstream
    state["result"] = result
    dispatch = modules["model_tools"].handle_function_call
    with pytest.raises(IncompatibleUpstream):
        turn(HermesAdapter())
    assert state["closed"] == 1 and modules["model_tools"].handle_function_call is dispatch


@pytest.mark.parametrize("fault", ["builtin_discovered", "builtin_enabled", "missing_refresh_switch", "run_raises"])
def test_changed_agent_authority_fails_closed(upstream, fault):
    modules, state = upstream
    original = modules["run_agent"].AIAgent
    if fault == "builtin_discovered":
        modules["tools.mcp_tool_discovery"].discover_mcp_tools = lambda *, allowed_mcp_names: ["terminal"]
    else:
        class ChangedAgent(original):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                if fault == "builtin_enabled":
                    self.valid_tool_names.add("terminal")
                if fault == "missing_refresh_switch":
                    del self._skip_mcp_refresh

            def run_conversation(self, *args, **kwargs):
                raise RuntimeError("fixture-upstream-exception")
        modules["run_agent"].AIAgent = ChangedAgent
    dispatch = modules["model_tools"].handle_function_call
    with pytest.raises(RuntimeError):
        turn(HermesAdapter())
    assert state["closed"] == (0 if fault == "builtin_discovered" else 1)
    assert modules["model_tools"].handle_function_call is dispatch
    assert not state["executed"]


@pytest.mark.parametrize("device", [None, {"user_code": "bad"},
    {"user_code": "ABCD-EFGH", "device_auth_id": "fixture", "interval": True},
    {"user_code": "ABCD-EFGH", "device_auth_id": {}, "interval": 3}])
def test_malformed_device_never_polls_or_saves(upstream, device):
    modules, state = upstream
    modules["hermes_cli.auth_codex"]._codex_request_device_code = lambda issuer, client: device
    with pytest.raises(IncompatibleUpstream):
        HermesAdapter().request_device()
    assert not state["saved"]


@pytest.mark.parametrize("step,value", [("poll", {}), ("exchange", {"access_token": "fixture-private-access"}),
                                        ("read", {"tokens": []}), ("resolve", {"api_key": "fixture-private-access"})])
def test_malformed_auth_material_never_becomes_configured(upstream, step, value):
    modules, state = upstream
    auth = modules["hermes_cli.auth_codex"]
    adapter = HermesAdapter()
    if step == "poll":
        auth._codex_poll_authorization_code = lambda issuer, **kwargs: value
    elif step == "exchange":
        auth._codex_exchange_authorization_code = lambda *args: value
    elif step == "read":
        auth._read_codex_tokens = lambda: value
    else:
        auth.resolve_codex_runtime_credentials = lambda: value
    with pytest.raises(IncompatibleUpstream):
        if step == "read":
            adapter.read_tokens()
        elif step == "resolve":
            adapter.credentials()
        else:
            adapter.finish_device(adapter.request_device())
    assert not state["saved"]


@pytest.mark.parametrize("fault", ["unavailable", "malformed_symbol", "new_required_parameter", "malformed_result"])
def test_real_candidate_gate_refuses_changed_upstream(request, fault):
    if not request.config.getoption("--containers"):
        pytest.skip("Actual candidate gate requires --containers")
    name = "agentic-hermes-incompatible-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    image = os.environ.get("AGENTIC_HERMES_IMAGE", "agentic-home-hermes:0.10.0")
    root = Path(__file__).parents[1] / "hermes"
    mounts = []
    if os.environ.get("AGENTIC_HERMES_TEST_SOURCE") == "1":
        mounts = ["-v", f"{root / 'agentic_hermes'}:/app/agentic_hermes:ro",
                  "-v", f"{root / 'compatibility_probe.py'}:/app/compatibility_probe.py:ro"]
    script = "import sys,subprocess,compatibility_probe\n"
    if fault == "unavailable":
        script += "sys.modules['run_agent'] = None\n"
    elif fault == "malformed_symbol":
        script += "import run_agent; run_agent.AIAgent = 'fixture-private-secret'\n"
    elif fault == "new_required_parameter":
        script += "from hermes_cli import auth_codex; auth_codex._read_codex_tokens = lambda required_new_parameter: {}\n"
    else:
        # Inject a signature-compatible broken result into the real child after
        # actual upstream agent/MCP initialization. No fake activation decision.
        script += '''original = subprocess.run
def changed(command, *args, **kwargs):
    if command[-2:] == ['-m', 'agentic_hermes.child']:
        command = command[:-2] + ['-c', "import run_agent; run_agent.AIAgent.run_conversation = lambda *args, **kwargs: {'completed': True, 'final_response': {'secret': 'fixture-private-secret'}}; from agentic_hermes.child import main; main()"]
    return original(command, *args, **kwargs)
subprocess.run = changed
'''
    script += "raise SystemExit(compatibility_probe.main())\n"
    command = ["docker", "run", "--rm", "--name", name, *mounts, "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--memory=512m", "--pids-limit=64", "-v", volume + ":/data",
               "-e", "HOME=/data", "-e", "HERMES_HOME=/data", "-e", "TMPDIR=/data",
               "-e", "PYTHONPATH=/app:/opt/hermes", "--entrypoint=python", image, "-c", script]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        assert result.returncode == 1
        stage = "chat_tool_roundtrip" if fault == "malformed_result" else "consumed_signatures"
        assert json.loads(result.stdout) == {"compatible": False, "adapter_contract": 1,
                                            "error": "upstream_contract_incompatible", "cloud_account_verified": False,
                                            "diagnostic": {"schema": 1, "stage": stage, "kind": "contract_failure"}}
        assert "fixture-private-secret" not in result.stdout
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)
