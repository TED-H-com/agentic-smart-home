"""Fake HTTP/clock checks for the installed auth probe; no services or VM needed."""
import copy
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe():
    spec = importlib.util.spec_from_file_location("runner_readiness_probe", Path(__file__).with_name("installed_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def ready():
    return {"runner": {"protocol": 1, "identity": "agentic-home-runner",
                       "providers": [{"provider": name, "status": "needs_reauth"}
                                     for name in ("codex", "claude", "agy", "grok")]},
            "onboarding": {"runner_paired": True}}


def unavailable():
    return {"runner": {"status": "unavailable", "providers": [],
                       "message": "private token /private/path connection or fingerprint"},
            "onboarding": {"runner_paired": True}}


def observe(probe, replies, clock=None, deadline=330):
    clock = clock or Clock()
    calls = []
    def request(path, *, timeout):
        calls.append((path, timeout))
        result = replies.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result
    result = probe.wait_for_runner_status(request, deadline=deadline, clock=clock, pause=clock.sleep)
    return result, calls, clock


def test_unavailable_then_ready_retains_first_shape_without_private_message(probe):
    expected = ready()
    expected["runner"]["future_field"] = {"private": "secret"}
    result, calls, clock = observe(probe, [unavailable(), expected])
    assert result is expected
    assert [path for path, _ in calls] == ["/api/status", "/api/status"]
    assert clock.sleeps == [2]
    evidence = probe.DIAGNOSTICS["runner_status"]
    assert evidence["result"] == "ready"
    first = evidence["first_failed_observation"]
    assert first["result"] == "unavailable" and first["runner_paired"] is True
    assert first["identity"] == "absent" and first["message_shape"] == "text"
    assert first == evidence["observations"][0]
    serialized = json.dumps(evidence)
    assert all(secret not in serialized for secret in ("private", "secret", "fingerprint", "connection"))


@pytest.mark.parametrize("fault", ["root_array", "runner_missing", "runner_array", "onboarding_missing",
    "unpaired", "unpaired_status", "paired_integer", "identity_missing", "identity_wrong", "identity_array",
    "protocol_wrong", "protocol_bool", "providers_missing", "providers_duplicate", "providers_wrong_name",
    "providers_malformed", "provider_status_missing", "unknown_state", "unavailable_with_identity",
    "unavailable_with_protocol", "unavailable_with_providers", "unavailable_without_message", "unavailable_bad_message"])
def test_semantic_failures_are_retained_and_never_retried(probe, fault):
    value = unavailable() if fault.startswith("unavailable_") else ready()
    runner, onboarding = value["runner"], value["onboarding"]
    if fault == "root_array":
        value = []
    elif fault == "runner_missing":
        del value["runner"]
    elif fault == "runner_array":
        value["runner"] = []
    elif fault == "onboarding_missing":
        del value["onboarding"]
    elif fault == "unpaired":
        onboarding["runner_paired"] = False
    elif fault == "unpaired_status":
        runner.update(status="unpaired", providers=[])
    elif fault == "paired_integer":
        onboarding["runner_paired"] = 1
    elif fault == "identity_missing":
        del runner["identity"]
    elif fault.startswith("identity_"):
        runner["identity"] = [] if fault == "identity_array" else "private-wrong-identity"
    elif fault.startswith("protocol_"):
        runner["protocol"] = True if fault == "protocol_bool" else 2
    elif fault == "providers_missing":
        del runner["providers"]
    elif fault == "providers_duplicate":
        runner["providers"][0] = copy.deepcopy(runner["providers"][1])
    elif fault == "providers_wrong_name":
        runner["providers"][0]["provider"] = "private-provider"
    elif fault == "providers_malformed":
        runner["providers"][0] = "private"
    elif fault == "provider_status_missing":
        del runner["providers"][0]["status"]
    elif fault == "unknown_state":
        runner["status"] = "private-state"
    elif fault == "unavailable_with_identity":
        runner["identity"] = "agentic-home-runner"
    elif fault == "unavailable_with_protocol":
        runner["protocol"] = 1
    elif fault == "unavailable_with_providers":
        runner["providers"] = ready()["runner"]["providers"]
    elif fault == "unavailable_without_message":
        del runner["message"]
    elif fault == "unavailable_bad_message":
        runner["message"] = {"private": "secret"}
    replies = [value, ready()]
    with pytest.raises(AssertionError, match="runner_status_"):
        observe(probe, replies)
    assert len(replies) == 1
    evidence = probe.DIAGNOSTICS["runner_status"]
    assert len(evidence["observations"]) == 1
    assert evidence["result"] not in {"ready", "unavailable"}
    assert "private" not in json.dumps(evidence)


@pytest.mark.parametrize("error", [TimeoutError("private"), urllib.error.URLError("private"),
    urllib.error.HTTPError("http://private", 503, "private", {}, None),
    json.JSONDecodeError("private", "private", 0)])
def test_request_failures_preserve_first_evidence_and_original_exception_without_retry(probe, error):
    replies = [error, ready()]
    with pytest.raises(type(error)) as caught:
        observe(probe, replies)
    assert caught.value is error and len(replies) == 1
    evidence = probe.DIAGNOSTICS["runner_status"]
    assert evidence["first_failed_observation"]["error_type"] == type(error).__name__
    assert "private" not in json.dumps(evidence)


def test_attempt_limit_does_not_turn_persistent_unavailability_into_success(probe):
    replies = [unavailable() for _ in range(3)] + [ready()]
    with pytest.raises(AssertionError, match="runner_readiness_exhausted"):
        observe(probe, replies)
    assert len(replies) == 1
    assert len(probe.DIAGNOSTICS["runner_status"]["observations"]) == 3
    assert probe.DIAGNOSTICS["runner_status"]["first_failed_observation"]["result"] == "unavailable"


def test_later_request_failure_cannot_replace_first_unavailable_observation(probe):
    error = TimeoutError("private later error")
    with pytest.raises(TimeoutError) as caught:
        observe(probe, [unavailable(), error, ready()])
    assert caught.value is error
    evidence = probe.DIAGNOSTICS["runner_status"]
    assert evidence["first_failed_observation"]["result"] == "unavailable"
    assert evidence["observations"][-1]["result"] == "request_failed"
    assert "private" not in json.dumps(evidence)


@pytest.mark.parametrize("budget", [35, 330])
def test_request_and_sleep_budgets_share_absolute_deadline(probe, budget):
    clock = Clock()
    calls = []
    def request(path, *, timeout):
        assert path == "/api/status"
        calls.append(timeout)
        clock.now += timeout
        return unavailable()
    with pytest.raises(AssertionError, match="runner_readiness_exhausted"):
        probe.wait_for_runner_status(request, deadline=budget, clock=clock, pause=clock.sleep)
    assert clock.now <= min(budget, 150)
    assert calls == ([35] if budget == 35 else [90, 58])
    assert len(calls) <= probe.RUNNER_READINESS_ATTEMPTS


@pytest.mark.parametrize("expire_after_login", [False, True])
def test_actual_probe_repeats_only_status_get_and_keeps_existing_success_checks(probe, monkeypatch, expire_after_login):
    clock = Clock()
    monkeypatch.setattr(probe.time, "monotonic", clock)
    original_wait = probe.wait_for_runner_status
    monkeypatch.setattr(probe, "wait_for_runner_status", lambda request, **kwargs:
                        original_wait(request, **kwargs, clock=clock, pause=clock.sleep))
    monkeypatch.setitem(sys.modules, "agentic_host.desktop", SimpleNamespace(issue_ticket=lambda: "private-ticket"))
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs: "private-key" if
                        str(path) == "/var/lib/agentic-home/controller/access-key" else original_read(path, *args, **kwargs))
    host = {"controller_update": {"ingress_bridge_contract": 1}, "provisioned": False, "vm": "stopped"}
    provider = {"id": "codex", "supported": True, "credential_state": "needs_auth",
                "auth_methods": ["device_code", "api_key"], "models": [
                    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]}
    hermes = {"runtime": {"state": "needs_auth", "auth_verified_at": None}, "workers": [
        {"worker_id": "host-coder", "work_contract_major": 1, "runtime_location": "host_container",
         "observed_from": "paired_runner", "available": True, "state": "ready", "reason": None,
         "modes": ["research", "development"], "providers": [provider]},
        {"worker_id": "home-coder", "work_contract_major": 1, "runtime_location": "haos_app",
         "observed_from": "controller", "available": False, "state": "blocked", "reason": "runner_unavailable",
         "modes": [], "providers": []}]}
    job = {"id": "fixture", "worker_id": "host-coder", "provider": "codex", "state": "blocked",
           "reason": "coding_needs_auth", "execution_started": False, "stop_confirmed": True,
           "artifacts": [], "evidence_ids": []}
    replies = {"/api/host/desktop-login": [{"authenticated": True},
        urllib.error.HTTPError("private", 403, "private", {}, None)],
        "/api/host/login": [{"authenticated": True}],
        "/api/session": [{"mode": "host", "principal": "host-owner", "csrf": "private-csrf"}],
        "/api/host/status": [host], "/api/status": [unavailable(), ready()],
        "/api/hermes": [hermes], "/api/bootstrap": [{"state": "idle", "steps": [None] * 9}],
        "/api/worker/jobs": [job, job], "/api/workers": [{"active_job_id": None}]}
    calls = []
    def open_request(request, *, timeout):
        path = request.selector
        calls.append((request.get_method(), path))
        assert 0 < timeout <= min(90, 330 - clock())
        value = replies[path].pop(0)
        if isinstance(value, BaseException):
            raise value
        if expire_after_login and path == "/api/host/login":
            clock.now = 330
        response = io.BytesIO(json.dumps(value).encode())
        response.status = 200
        return response
    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    if expire_after_login:
        with pytest.raises(AssertionError, match="auth_probe_deadline"):
            probe.probe()
        assert calls == [("POST", "/api/host/desktop-login"), ("POST", "/api/host/desktop-login"),
                         ("POST", "/api/host/login")]
        assert probe.STAGE == "auth_probe_deadline"
        return
    result = probe.probe()
    assert result["automatic_runner_pairing"] == "passed" and result["coding_without_login"] == "blocked_no_execution"
    assert calls.count(("GET", "/api/status")) == 2
    assert [path for method, path in calls if method == "POST"] == [
        "/api/host/desktop-login", "/api/host/desktop-login", "/api/host/login", "/api/worker/jobs", "/api/worker/jobs"]
    assert all(not remaining for remaining in replies.values())
    assert "private" not in json.dumps({"result": result, "diagnostics": probe.DIAGNOSTICS})
