"""Check the installed probe against actual capability/dispatch code, without a VM."""
import importlib.util
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentic_controller.runner import RunnerError
from agentic_controller.work import WorkerStart
from agentic_controller.workers import Workers


@pytest.fixture
def installed_probe():
    spec = importlib.util.spec_from_file_location("installed_probe_contract", Path(__file__).with_name("installed_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def actual_capabilities(tmp_path, monkeypatch, store):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    from agentic_host.coding_service import CodingService

    service = object.__new__(CodingService)
    service.root = tmp_path
    service.credential_path = tmp_path / "absent-role-auth.json"
    # This unit test substitutes only the prior hard-boundary observation.
    # The installed VM performs the actual Docker/namespace/quota checks.
    service.capability_cache = (time.monotonic(), True, None)
    calls = []

    class Peer:
        def __init__(self, host):
            self.host = host

        async def call(self, method, path, body=None):
            calls.append((self.host, method, path))
            if not self.host:
                raise RunnerError("not paired")
            assert method == "GET" and path == "/coding/capabilities"
            return service.capabilities()

    workers = Workers(store, {"host": Peer(True), "haos": Peer(False)}, None)
    workers.closing = True
    yield workers, await workers.capabilities(), calls
    await workers.close()


async def test_installed_probe_accepts_boundary_ready_but_blocks_missing_role_login(actual_capabilities, installed_probe):
    workers, observed, calls = actual_capabilities
    installed_probe.verify_workers(observed)
    body = WorkerStart(worker_id="host-coder", provider="codex", mode="research", goal="No auth must block")
    owner = SimpleNamespace(cookies={"agentic_host": str(int(time.time()) + 3600) + ".fixture"})
    first = await workers.submit(body, "installed-coding-without-login", request=owner)
    replay = await workers.submit(body, "installed-coding-without-login", request=owner)
    installed_probe.verify_auth_blocked(first)
    installed_probe.verify_auth_blocked(replay)
    assert first["id"] == replay["id"]
    assert workers.public_capabilities()["active_job_id"] is None
    assert all(method == "GET" for _, method, _ in calls)
    assert all(type(value) is bool for value in installed_probe.DIAGNOSTICS.values())


@pytest.mark.parametrize("fault", ["boundary_blocked", "already_authenticated", "unexpected_provider", "home_available"])
async def test_installed_probe_keeps_strict_readiness_and_no_account_requirements(actual_capabilities, installed_probe, fault):
    _, observed, _ = actual_capabilities
    host, home = observed["workers"]
    if fault == "boundary_blocked":
        host["available"] = False
    elif fault == "already_authenticated":
        host["providers"][0]["credential_state"] = "configured"
    elif fault == "unexpected_provider":
        host["providers"][0]["id"] = "claude"
    else:
        home["available"] = True
    with pytest.raises(AssertionError):
        installed_probe.verify_workers(observed)
    assert installed_probe.STAGE in {"host_worker_boundary", "host_worker_requires_role_login",
                                    "host_worker_codex_contract", "home_worker_not_paired"}


def test_installed_probe_refuses_execution_without_credentials(installed_probe):
    receipt = {"worker_id": "host-coder", "provider": "codex", "state": "blocked", "reason": "coding_needs_auth",
               "execution_started": True, "stop_confirmed": True, "artifacts": [], "evidence_ids": []}
    with pytest.raises(AssertionError, match="coding_without_login_blocked"):
        installed_probe.verify_auth_blocked(receipt)


def test_bootstrap_cli_diagnostics_preserve_only_fixed_status_and_error_codes():
    from test_linux_install import bootstrap_job_diagnostics

    known = 'Operation fixture\n{"state":"failed","message":"candidate_environment_invalid","private":"not-exported"}'
    assert bootstrap_job_diagnostics(known) == {"state": "failed", "error_code": "candidate_environment_invalid"}
    for output in ('Operation fixture\n{"state":"failed","message":"private_credential_like_value"}',
                   '{"state":"private_state","message":"private message"}', 'not JSON', 'x' * 65537):
        result = bootstrap_job_diagnostics(output)
        assert set(result) == {"state", "error_code"}
        assert result["state"] in {"failed", "unknown"}
        assert result["error_code"] == "unclassified_host_error"


@pytest.fixture
def update_probe():
    spec = importlib.util.spec_from_file_location("installed_update_probe_contract", Path(__file__).with_name("installed_update_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def update_observation():
    return {
        "checkpoint": {"state": "running", "stage": "restore_original_image_and_cleanup"},
        "unit": {"ActiveState": "active", "SubState": "exited", "Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0"},
        "process": {"returncode": None, "stdout_bytes": 42, "stderr_bytes": 0, "stderr_truncated": False, "stderr_signatures": {}},
        "result": {"state": "passed", "observed_from": "disposable_installed_linux_vm_root",
                   "actual": ["ed25519_signature_verification", "bounded_candidate_probe", "compose_activation", "authenticated_owner_smoke",
                              "consistent_snapshot", "schema3", "maintenance_gate", "same_database_rollback", "post_deployment_row_preserved"],
                   "fixtures": ["ephemeral_publisher", "preloaded_registry_delivery", "candidate_changes_only_image_label", "synthetic_post_deployment_record"],
                   "original_image": "sha256:" + "a" * 64, "candidate_image": "sha256:" + "b" * 64,
                   "real_cloud": False, "real_publisher_release": False, "final_schema": 3,
                   "registry_delivery_calls": 1, "rollback_restored_backup": False,
                   "diagnostics": {"cleanup_started": True, "cleanup_completed": True}},
    }


@pytest.mark.parametrize("index,replacement", [(0, 1000), (1, None), (2, "real-laptop"), (3, []), (4, "docker"), (5, False)])
def test_update_probe_guard_rejects_each_missing_disposable_proof(update_probe, index, replacement):
    proof = [0, "1", "agentic-disposable", ["AGENTIC_TEST_ONLY"], "kvm", True]
    update_probe.validate_guard(*proof)
    proof[index] = replacement
    with pytest.raises(RuntimeError, match="disposable_installed_vm_required"):
        update_probe.validate_guard(*proof)


def test_update_probe_guard_failure_has_no_import_or_persist_side_effect(update_probe, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(update_probe, "STATE_ROOT", tmp_path / "must-not-exist")
    monkeypatch.setattr(update_probe.os, "geteuid", lambda: 1000)
    assert update_probe.execute() == 1
    assert not update_probe.STATE_ROOT.exists()
    result = json.loads(capsys.readouterr().out)
    assert result["error_code"] == "disposable_installed_vm_required"
    assert result["stage"] == "disposable_vm_guard"


def test_update_probe_launch_once_private_receipt_and_fixed_unit(update_probe, monkeypatch, tmp_path):
    monkeypatch.setattr(update_probe, "guard", lambda: None)
    monkeypatch.setattr(update_probe, "STATE_ROOT", tmp_path / "private-probe")
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "Created transient unit")
    monkeypatch.setattr(update_probe.subprocess, "run", run)
    prior = update_probe.os.umask(0o077)
    try:
        result = update_probe.launch("# trusted fixture\n")
        with pytest.raises(FileExistsError):
            update_probe.launch("# replay must not launch\n")
    finally:
        update_probe.os.umask(prior)
    assert result["launch"]["returncode"] == 0 and len(calls) == 1
    assert update_probe.STATE_ROOT.stat().st_mode & 0o777 == 0o700
    assert (update_probe.STATE_ROOT / "checkpoint.json").stat().st_mode & 0o777 == 0o600
    assert (update_probe.STATE_ROOT / "probe.py").stat().st_mode & 0o777 == 0o400
    args = calls[0][0]
    assert "--property=RuntimeMaxSec=600" in args and "--property=KillMode=control-group" in args
    assert "--property=RemainAfterExit=yes" in args
    assert args[-2:] == ["/usr/bin/python3", str(update_probe.STATE_ROOT / "probe.py")]


def test_update_probe_import_failure_persists_safe_result(update_probe, monkeypatch, tmp_path, capsys):
    import builtins

    monkeypatch.setattr(update_probe, "guard", lambda: None)
    monkeypatch.setattr(update_probe, "STATE_ROOT", tmp_path)
    original = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == "agentic_host.bootstrap":
            raise ModuleNotFoundError("private import details must not leak")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)
    assert update_probe.execute() == 1
    raw = (tmp_path / "result.json").read_text()
    assert "private import" not in raw
    result = json.loads(raw)
    assert result["stage"] == "installed_module_imports"
    assert result["error_type"] == "ModuleNotFoundError"
    assert json.loads(capsys.readouterr().out) == result


def test_update_probe_recovers_lost_launch_reply_without_relaunch(tmp_path):
    from test_linux_install import installed_update

    report, calls = tmp_path / "evidence.json", []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 255, "", "Connection reset; private credential")
        # Diagnostic receipt must already exist before remote JSON parsing.
        assert json.loads(report.read_text())["transport"]["launch"]["returncode"] == 255
        assert "--observe" in args[-1] and "input" not in kwargs
        return subprocess.CompletedProcess(args, 0, json.dumps(update_observation()), "")
    result = installed_update(["ssh", "fixture"], "# only one launch", report, run=run, pause=lambda _: None)
    assert result["state"] == "passed" and len(calls) == 2
    assert result["transport"]["launch"]["stderr_signatures"]["ssh_transport"]
    assert "private credential" not in report.read_text()


@pytest.mark.parametrize("output", ["", "private malformed output", "[]", '{"state":"passed"}'])
def test_update_probe_empty_or_malformed_observation_never_passes_or_relaunches(tmp_path, output):
    from test_linux_install import installed_update

    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, output, "private stderr")
    report = tmp_path / "evidence.json"
    result = installed_update(["ssh", "fixture"], "# fixture", report, run=run, pause=lambda _: None)
    assert result["state"] == "failed" and result["error_code"] == "probe_observation_unavailable"
    assert len(calls) == 4 and sum("input" in kwargs for _, kwargs in calls) == 1
    assert "private" not in report.read_text()


@pytest.mark.parametrize("fault", ["missing_receipt", "nonzero_exit", "signal_exit", "incomplete_cleanup"])
def test_update_probe_requires_completed_process_and_full_success_receipt(tmp_path, fault):
    from test_linux_install import installed_update

    value = update_observation()
    if fault == "missing_receipt":
        value["result"] = None
    elif fault == "nonzero_exit":
        value["unit"]["ExecMainStatus"] = "1"
    elif fault == "signal_exit":
        value["unit"]["ExecMainCode"] = "2"
    else:
        value["result"]["diagnostics"]["cleanup_completed"] = False
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
    result = installed_update(["ssh", "fixture"], "# fixture", tmp_path / "evidence.json", run=run, pause=lambda _: None)
    assert result["state"] == "failed"


def test_update_probe_waits_for_real_unit_exit_and_recovers_observation_loss(tmp_path):
    from test_linux_install import installed_update

    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if len(calls) in {1, 3}:
            return subprocess.CompletedProcess(args, 255, "", "Connection closed")
        value = update_observation()
        if len(calls) == 2:
            value["unit"]["SubState"] = "running"
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
    result = installed_update(["ssh", "fixture"], "# fixture", tmp_path / "evidence.json", run=run, pause=lambda _: None)
    assert result["state"] == "passed" and len(calls) == 4


def test_update_probe_diagnostics_and_receipt_redact_unknown_fields(update_probe):
    from test_linux_install import decode_update_observation

    text = "Traceback (most recent call last)\nModuleNotFoundError: private_secret\n" + "x" * 20000
    diagnostic = update_probe.process_diagnostics(subprocess.CompletedProcess([], 1, "private_stdout", text))
    assert diagnostic["stderr_signatures"]["traceback"] and diagnostic["stderr_signatures"]["import_error"]
    assert diagnostic["stderr_truncated"] and diagnostic["stderr_bytes"] == len(text)
    assert "private" not in json.dumps(diagnostic)
    value = update_observation()
    value["result"].update(state="failed", error_code="private_error", error_type="private_type", stage="private_stage", secret="private_data")
    value["result"]["diagnostics"]["secret"] = "private_diagnostic"
    value["unit"]["Result"] = "private_unit"
    assert "private" not in json.dumps(decode_update_observation(json.dumps(value)))


def test_update_probe_observes_persisted_checkpoint_and_bounded_private_logs(update_probe, monkeypatch, tmp_path):
    from test_linux_install import decode_update_observation

    monkeypatch.setattr(update_probe, "guard", lambda: None)
    monkeypatch.setattr(update_probe, "STATE_ROOT", tmp_path)
    expected = update_observation()
    update_probe.checkpoint("restore_original_image_and_cleanup")
    update_probe.persist("result.json", expected["result"])
    (tmp_path / "stdout").write_text("private stdout")
    (tmp_path / "stderr").write_text("Permission denied private credential\n" + "x" * 20000)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "ActiveState=active\nSubState=exited\nResult=success\nExecMainCode=1\nExecMainStatus=0\n", "")
    monkeypatch.setattr(update_probe.subprocess, "run", run)
    observed = update_probe.observe()
    assert len(calls) == 1 and calls[0][:2] == ["systemctl", "show"]
    assert "private" not in json.dumps(observed)
    assert observed["process"]["stderr_truncated"]
    assert observed["process"]["stderr_signatures"]["permission_denied"]
    clean = decode_update_observation(json.dumps(observed))
    assert clean["result"]["state"] == "passed"
    assert clean["unit"] == expected["unit"]
    assert clean["checkpoint"] == expected["checkpoint"]


def test_update_probe_running_result_is_bounded_and_never_success(tmp_path):
    from test_linux_install import installed_update

    value = update_observation()
    value["unit"]["SubState"] = "running"
    tick = [0]
    calls = []
    def clock():
        tick[0] += 60
        return tick[0]
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
    result = installed_update(["ssh", "fixture"], "# fixture", tmp_path / "evidence.json", run=run, clock=clock, pause=lambda _: None)
    assert result["state"] == "failed" and result["error_code"] == "probe_observation_deadline"
    assert sum("input" in kwargs for _, kwargs in calls) == 1
