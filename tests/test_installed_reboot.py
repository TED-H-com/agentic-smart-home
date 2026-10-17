"""Fake-only delayed reboot/SSH regressions; no VM, service or provider execution."""
import json
import subprocess
from pathlib import Path

import installed_probe
import pytest
import test_linux_install as install

OLD_BOOT = "3a328855-e514-47e5-a855-819876675536"
NEW_BOOT = "1d0740e4-4ed3-4cbb-97cd-05b83e87042b"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.pauses = []

    def __call__(self):
        return self.now

    def pause(self, seconds):
        assert 0 <= seconds <= 5
        self.pauses.append(seconds)
        self.now += seconds


def state(boot=OLD_BOOT, active=True):
    return {"boot_id": boot, "service_active": active}


class Transport:
    def __init__(self, observations, request_result=0):
        self.observations = iter(observations)
        self.request_result = request_result
        self.calls = []

    def __call__(self, args, **options):
        self.calls.append((args, options))
        assert args[:-1] == ["ssh", "fixture"]
        assert options["capture_output"] and options["text"]
        assert 0 < options["timeout"] <= 30
        if args[-1] == "sudo systemctl reboot":
            result = self.request_result
            assert "input" not in options
        else:
            assert args[-1] == "python3 -"
            assert options["input"] == install.OUTER_BOOT_OBSERVATION_SCRIPT
            result = next(self.observations)
        if result == "timeout":
            raise subprocess.TimeoutExpired(args, options["timeout"], output="private", stderr="private")
        if type(result) is int:
            return subprocess.CompletedProcess(args, result, "", "private transport details")
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    @property
    def writes(self):
        return [call for call in self.calls if call[0][-1] == "sudo systemctl reboot"]


def test_delayed_reboot_does_not_accept_old_boot_even_with_active_service():
    clock = Clock()
    transport = Transport([state(), state(), state(), "timeout", 255,
                           state(NEW_BOOT, False), state(NEW_BOOT)])
    observed = install.reboot_outer(["ssh", "fixture"], 60, run=transport, clock=clock, pause=clock.pause)
    assert len(transport.writes) == 1
    assert observed == {"boot_id_changed": True, "service_active_in_new_boot": True,
                        "reboot_requests": 1, "observations": 7, "unavailable_observations": 2,
                        "same_boot_observations": 2, "new_boot_not_ready_observations": 1,
                        "request_result": "accepted", "duration_seconds": 25}
    assert OLD_BOOT not in json.dumps(observed) and NEW_BOOT not in json.dumps(observed)
    assert "private" not in json.dumps(observed)


@pytest.mark.parametrize("request_result,result", [(0, "accepted"), (255, "transport_disconnected"), ("timeout", "timeout")])
def test_uncertain_reboot_request_is_never_retried(request_result, result):
    clock = Clock()
    transport = Transport([state(), state(NEW_BOOT)], request_result)
    observed = install.reboot_outer(["ssh", "fixture"], 60, run=transport, clock=clock, pause=clock.pause)
    assert len(transport.writes) == 1 and observed["request_result"] == result
    assert observed["service_active_in_new_boot"] and observed["boot_id_changed"]


@pytest.mark.parametrize("after", [state(), state(NEW_BOOT, False), 255, "timeout"])
def test_same_boot_or_new_boot_without_active_service_exhausts_budget(after):
    clock = Clock()
    transport = Transport([state(), after, after])
    with pytest.raises(AssertionError, match="outer_reboot_new_boot_not_ready"):
        install.reboot_outer(["ssh", "fixture"], 7, run=transport, clock=clock, pause=clock.pause)
    assert clock.now == 7 and clock.pauses == [5, 2]
    assert len(transport.writes) == 1
    assert [options["timeout"] for _, options in transport.calls] == [7, 7, 7, 2]


def test_unavailable_baseline_never_sends_reboot():
    clock = Clock()
    transport = Transport([255, "timeout"])
    with pytest.raises(AssertionError, match="outer_reboot_baseline_unavailable"):
        install.reboot_outer(["ssh", "fixture"], 7, run=transport, clock=clock, pause=clock.pause)
    assert clock.now == 7 and not transport.writes


def test_baseline_has_its_own_short_bound_inside_whole_budget():
    clock = Clock()
    transport = Transport([255] * 12)
    with pytest.raises(AssertionError, match="outer_reboot_baseline_unavailable"):
        install.reboot_outer(["ssh", "fixture"], 1800, run=transport, clock=clock, pause=clock.pause)
    assert clock.now == install.REBOOT_BASELINE_SECONDS and not transport.writes


def test_rejected_reboot_request_fails_without_resubmission():
    clock = Clock()
    transport = Transport([state()], 1)
    with pytest.raises(AssertionError, match="outer_reboot_request_failed"):
        install.reboot_outer(["ssh", "fixture"], 60, run=transport, clock=clock, pause=clock.pause)
    assert len(transport.writes) == 1 and len(transport.calls) == 2


@pytest.mark.parametrize("slow_operation", ["baseline", "request", "observation"])
def test_slow_process_consumes_shared_budget_and_late_ready_is_not_accepted(slow_operation):
    clock = Clock()
    calls = []
    def run(args, **options):
        calls.append(args[-1])
        position = {"baseline": 1, "request": 2, "observation": 3}[slow_operation]
        if len(calls) == position:
            clock.now += options["timeout"]
        return subprocess.CompletedProcess(args, 0, json.dumps(state(OLD_BOOT if len(calls) == 1 else NEW_BOOT)), "")
    code = "outer_reboot_baseline_unavailable" if slow_operation == "baseline" else "outer_reboot_new_boot_not_ready"
    with pytest.raises(AssertionError, match=code):
        install.reboot_outer(["ssh", "fixture"], 7, run=run, clock=clock, pause=clock.pause)
    assert clock.now == 7
    assert calls.count("sudo systemctl reboot") == int(slow_operation != "baseline")


@pytest.mark.parametrize("value", [None, [], {}, state("not-a-uuid"), state("0" * 32),
    state("00000000-0000-0000-0000-000000000000"), state(OLD_BOOT.upper()), state(OLD_BOOT, "active"),
    state(OLD_BOOT, 1), {**state(), "raw": "private"}])
def test_boot_observation_requires_exact_valid_identity_and_boolean(value):
    transport = Transport([value])
    assert install.observe_outer_boot(["ssh", "fixture"], 5, run=transport) is None
    assert not transport.writes


@pytest.mark.parametrize("stdout,code", [("", 0), ("private invalid output", 0), ("x" * 513, 0),
                                        (json.dumps(state()), 255), (b"private", 0)])
def test_empty_malformed_oversized_or_failed_transport_is_not_ready(stdout, code):
    def run(args, **options):
        return subprocess.CompletedProcess(args, code, stdout, "private")
    assert install.observe_outer_boot(["ssh", "fixture"], 5, run=run) is None


@pytest.mark.parametrize("seconds", [True, 0, -1, 3601, float("inf"), float("nan"), "60"])
def test_invalid_budget_cannot_execute_any_command(seconds):
    def run(*args, **options):
        raise AssertionError("must not execute")
    with pytest.raises(ValueError, match="invalid_reboot_budget"):
        install.reboot_outer(["ssh", "fixture"], seconds, run=run)


@pytest.mark.parametrize("active", [True, False])
def test_remote_script_observes_one_boot_and_fixed_service_only(monkeypatch, capsys, active):
    reads, calls = [], []
    def read(path):
        reads.append(str(path))
        return NEW_BOOT + "\n"
    def run(args, **options):
        calls.append(args)
        assert options == {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 5}
        return subprocess.CompletedProcess(args, 0 if active else 3)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(subprocess, "run", run)
    exec(install.OUTER_BOOT_OBSERVATION_SCRIPT, {})
    assert reads == ["/proc/sys/kernel/random/boot_id"] * 2
    assert calls == [["systemctl", "is-active", "agentic-home"]]
    assert json.loads(capsys.readouterr().out) == state(NEW_BOOT, active)


def test_remote_script_does_not_combine_identity_and_service_from_different_boots(monkeypatch, capsys):
    reads = iter([OLD_BOOT, NEW_BOOT])
    monkeypatch.setattr(Path, "read_text", lambda _: next(reads))
    monkeypatch.setattr(subprocess, "run", lambda args, **options: subprocess.CompletedProcess(args, 0))
    with pytest.raises(SystemExit) as error:
        exec(install.OUTER_BOOT_OBSERVATION_SCRIPT, {})
    assert error.value.code == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("provisioned", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_auth_entrypoint_keeps_json_diagnostics_for_both_install_states(monkeypatch, capsys, provisioned, fails):
    calls = []
    def probe(**options):
        calls.append(options)
        if fails:
            raise RuntimeError("private access-key and cookie")
        return {"excluded_cli_probes": {"grok": "not_run: owner instruction"}}
    monkeypatch.setattr(installed_probe, "probe", probe)
    monkeypatch.setattr(installed_probe, "STAGE", "home_provisioned")
    monkeypatch.setattr(installed_probe, "OBSERVATIONS", [{"path": "/api/host/status", "status": 200}])
    monkeypatch.setattr(installed_probe, "DIAGNOSTICS", {"host_role_needs_auth": True})
    assert installed_probe.execute(provisioned=provisioned) == int(fails)
    captured = capsys.readouterr()
    value = json.loads(captured.out)
    assert calls == [{"provisioned": provisioned}]
    assert value["http_observations"] == [{"path": "/api/host/status", "status": 200}]
    assert value["diagnostics"] == {"host_role_needs_auth": True}
    assert "private" not in captured.out and captured.err == ""
    if fails:
        assert value["error_type"] == "RuntimeError" and value["result"] == "failed"
        assert value["stage"] == "home_provisioned"
    else:
        assert value["excluded_cli_probes"] == {"grok": "not_run: owner instruction"}


@pytest.mark.parametrize("code", sorted(install.REBOOT_ERRORS))
def test_failure_receipt_keeps_fixed_reboot_cause_only(tmp_path, code):
    path = tmp_path / "failure.json"
    observed = install.record_install_failure(AssertionError(code), "outer_reboot_and_haos_autostart", path, None)
    assert observed["reboot_error_code"] == code
    assert "boot_id" not in path.read_text() and "stdout" not in observed
    for unknown in ("private detail", {"private": "cookie"}):
        observed = install.record_install_failure(AssertionError(unknown), "outer_reboot_and_haos_autostart", path, None)
        assert "reboot_error_code" not in observed and "private" not in path.read_text()
