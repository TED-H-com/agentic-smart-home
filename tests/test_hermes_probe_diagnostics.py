"""Fixed probe diagnostics with mocked children/server; no upstream or network runs."""

import copy
import json
import os
import subprocess
import types

import compatibility_probe as probe
import pytest

PRIVATE = "private-provider-output /home/private/account Bearer do-not-publish"
CHILD_STAGES = ("chat_tool_roundtrip", "continued_history", "provider_401",
                "native_device_oauth", "native_responses_tool_roundtrip")


@pytest.fixture
def behavioral(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(probe.sys, "argv", ["compatibility_probe.py"])
    state = types.SimpleNamespace(calls=[], cleanup=[], error_stage=None, error=None)
    state.server = types.SimpleNamespace(
        observed={"chat": 3, "continuation": 1, "native": 2, "unauthorized": 1, "tools": 2},
        failures=[], egress=[], server_port=12345, serve_forever=lambda: None,
        shutdown=lambda: state.cleanup.append("shutdown"),
        server_close=lambda: state.cleanup.append("close"),
    )
    state.responses = [
        {"answer": "chat fixture complete", "tools": sorted(probe.EXPECTED_TOOLS),
         "history": [{"role": "tool", "tool_call_id": "compat-call-1", "content": '{"error":"not authorized"}'}]},
        {"answer": "continuation fixture complete"},
        {"error": "needs_reauth", "answer": "", "history": []},
        {"oauth": "real_upstream_helpers"},
        {"answer": "native fixture complete", "tools": sorted(probe.EXPECTED_TOOLS)},
    ]

    class Adapter:
        def validate_contract(self):
            if state.error_stage == "consumed_signatures":
                raise state.error

    class Thread:
        def __init__(self, *, target, daemon):
            assert target is state.server.serve_forever and daemon is True

        def start(self):
            pass

        def join(self, *, timeout):
            assert timeout == 3
            state.cleanup.append("join")

    def child(arguments, env, payload=None):
        index = len(state.calls)
        state.calls.append((arguments, payload))
        if state.error_stage == CHILD_STAGES[index]:
            raise state.error
        return copy.deepcopy(state.responses[index])

    monkeypatch.setattr(probe, "HermesAdapter", Adapter)
    monkeypatch.setattr(probe, "FixtureServer", lambda: state.server)
    monkeypatch.setattr(probe.threading, "Thread", Thread)
    monkeypatch.setattr(probe, "run_child", child)
    return state


def failure(capsys, stage, kind):
    assert probe.main() == 1
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out and PRIVATE not in captured.err
    assert len(captured.out.encode()) < 512
    assert json.loads(captured.out) == {
        "compatible": False, "adapter_contract": 1, "error": "upstream_contract_incompatible",
        "cloud_account_verified": False, "diagnostic": {"schema": 1, "stage": stage, "kind": kind},
    }


def test_success_contract_stays_unchanged_and_runs_each_child_once(behavioral, capsys):
    assert probe.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "compatible": True, "adapter_contract": 1, "observed_from": "runtime_container",
        "model": "protocol_fixture", "mcp": "official_client_loopback_fixture", "cloud_account_verified": False,
        "checks": ["consumed_signatures", "chat_tool_roundtrip", "continued_history", "tool_allowlist",
                   "builtin_tool_refused", "provider_401", "native_device_oauth", "private_token_store",
                   "native_responses_tool_roundtrip", "secret_redaction", "no_egress_beyond_provider_and_mcp"],
    }
    assert len(behavioral.calls) == 5
    assert behavioral.calls[1][1]["message"] == "fixture-continuation"
    assert behavioral.calls[1][1]["history"] == behavioral.responses[0]["history"]
    assert behavioral.cleanup == ["shutdown", "close", "join"]


@pytest.mark.parametrize("stage", ("consumed_signatures", *CHILD_STAGES))
@pytest.mark.parametrize("error,kind", [
    (probe.IncompatibleUpstream(PRIVATE), "contract_failure"),
    (RuntimeError(PRIVATE), "unexpected"),
    (probe.ProbeFailure("child_timeout"), "child_timeout"),
    (probe.ProbeFailure("child_exit"), "child_exit"),
    (probe.ProbeFailure("invalid_output"), "invalid_output"),
])
def test_failure_stage_and_kind_are_fixed_and_do_not_retry(behavioral, capsys, stage, error, kind):
    behavioral.error_stage, behavioral.error = stage, error
    failure(capsys, stage, kind)
    if stage == "consumed_signatures":
        assert behavioral.calls == [] and behavioral.cleanup == []
    else:
        assert len(behavioral.calls) == CHILD_STAGES.index(stage) + 1
        assert behavioral.cleanup == ["shutdown", "close", "join"]


@pytest.mark.parametrize("index,change", [
    (0, {"answer": "wrong"}), (0, {"tools": ["terminal"]}), (0, {"history": []}),
    (1, {"answer": "wrong"}), (2, {"error": "wrong"}), (2, {"answer": PRIVATE}),
    (2, {"history": [{"content": PRIVATE}]}), (3, {"oauth": "wrong"}),
    (4, {"answer": "wrong"}), (4, {"tools": ["terminal"]}),
])
def test_existing_behavioral_assertions_remain_required(behavioral, capsys, index, change):
    behavioral.responses[index].update(change)
    failure(capsys, CHILD_STAGES[index], "contract_failure")
    assert len(behavioral.calls) == index + 1
    assert behavioral.cleanup == ["shutdown", "close", "join"]


@pytest.mark.parametrize("field", ["failures", "egress"])
def test_fixture_protocol_failure_never_exposes_observation(behavioral, capsys, field):
    getattr(behavioral.server, field).append(PRIVATE)
    failure(capsys, "fixture_finalization", "fixture_protocol")
    assert len(behavioral.calls) == 5 and behavioral.cleanup == ["shutdown", "close", "join"]


@pytest.mark.parametrize("counter", ["chat", "continuation", "native", "unauthorized", "tools"])
def test_all_fixture_counters_remain_exact(behavioral, capsys, counter):
    behavioral.server.observed[counter] += 1
    failure(capsys, "fixture_finalization", "contract_failure")


def test_next_invocation_never_reuses_previous_stage(behavioral, capsys):
    behavioral.error_stage, behavioral.error = "native_responses_tool_roundtrip", RuntimeError(PRIVATE)
    failure(capsys, "native_responses_tool_roundtrip", "unexpected")
    behavioral.error_stage = "consumed_signatures"
    failure(capsys, "consumed_signatures", "unexpected")


@pytest.mark.parametrize("error,kind", [(probe.IncompatibleUpstream(PRIVATE), "contract_failure"),
                                        (RuntimeError(PRIVATE), "unexpected")])
def test_oauth_subprocess_failure_has_its_own_fixed_stage(monkeypatch, capsys, error, kind):
    monkeypatch.setattr(probe.sys, "argv", ["compatibility_probe.py", "--oauth-fixture"])

    def oauth():
        raise error

    monkeypatch.setattr(probe, "oauth_fixture", oauth)
    failure(capsys, "native_device_oauth", kind)


def test_oauth_success_contract_stays_unchanged(monkeypatch, capsys):
    expected = {"oauth": "real_upstream_helpers", "transport": "mock_http", "account_verified": False}
    monkeypatch.setattr(probe.sys, "argv", ["compatibility_probe.py", "--oauth-fixture"])
    monkeypatch.setattr(probe, "oauth_fixture", lambda: expected)
    assert probe.main() == 0 and json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize("result,kind", [
    (subprocess.CompletedProcess([], 1, PRIVATE, PRIVATE), "child_exit"),
    (subprocess.CompletedProcess([], -9, PRIVATE, PRIVATE), "child_exit"),
    (subprocess.CompletedProcess([], 0, "not JSON " + PRIVATE, PRIVATE), "invalid_output"),
    *[(subprocess.CompletedProcess([], 0, raw, PRIVATE), "invalid_output") for raw in ("[]", "null", "1", "true", '"text"')],
    (subprocess.CompletedProcess([], 0, None, PRIVATE), "invalid_output"),
    (subprocess.TimeoutExpired(["private-command"], 30, output=PRIVATE, stderr=PRIVATE), "child_timeout"),
    (UnicodeDecodeError("utf-8", b"\xff", 0, 1, PRIVATE), "invalid_output"),
])
def test_child_failure_categories_preserve_single_attempt_and_timeout(monkeypatch, result, kind):
    calls = []

    def run(command, **options):
        calls.append((command, options))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(probe.ProbeFailure) as error:
        probe.run_child(["-m", "agentic_hermes.child"], dict(os.environ), {"fixture": True})
    assert error.value.kind == kind and str(error.value) == kind
    assert len(calls) == 1 and calls[0][1]["timeout"] == 30
    assert calls[0][1]["capture_output"] is True and calls[0][1]["text"] is True


@pytest.mark.parametrize("secret", [probe.API_KEY, probe.MCP_TOKEN, probe.ACCESS_TOKEN, probe.REFRESH_TOKEN])
def test_child_secret_redaction_assertion_is_unchanged(monkeypatch, secret):
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps({"answer": secret}), PRIVATE))
    with pytest.raises(probe.IncompatibleUpstream, match="^upstream_contract_incompatible$"):
        probe.run_child(["-m", "agentic_hermes.child"], dict(os.environ))


def test_child_success_preserves_output_and_future_fields(monkeypatch):
    expected = {"answer": "fixture", "future_field": {"accepted": True}}
    monkeypatch.setattr(probe.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, json.dumps(expected), PRIVATE))
    assert probe.run_child(["-m", "agentic_hermes.child"], dict(os.environ)) == expected
