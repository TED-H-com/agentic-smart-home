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
                              "consistent_snapshot", "schema3", "maintenance_gate", "same_database_rollback", "post_deployment_row_preserved",
                              "revoked_bridge_fixed_probe_enforced", "pending_bridge_operation_blocked", "isolated_probe_cleanup_verified"],
                   "fixtures": ["ephemeral_publisher", "preloaded_registry_delivery", "candidate_changes_only_image_label", "synthetic_post_deployment_record",
                                "synthetic_missing_bridge_contract_image", "isolated_structure_only_revoked_bridge"],
                   "original_image": "sha256:" + "a" * 64, "candidate_image": "sha256:" + "b" * 64,
                   "real_cloud": False, "real_publisher_release": False, "final_schema": 3,
                   "registry_delivery_calls": 1, "rollback_restored_backup": False,
                   "bridge_update_contract": {
                       "contract": 1, "observed_from": "installed_host_fixed_probe", "schema": 3,
                       "database_fixture": "isolated_structure_only_revoked_bridge", "fixed_probe_executions": 3,
                       "supporting_image": "sha256:" + "a" * 64, "missing_contract_image": "sha256:" + "c" * 64,
                       "fixed_script_sha256": "d" * 64, "synthetic_missing_contract_image": True, "real_historical_release": False,
                       "checks": dict.fromkeys(("revoked_history_marker_copied", "supporting_fixed_probe_passed",
                           "missing_contract_fixed_probe_rejected", "missing_contract_import_error", "missing_contract_without_history_passed",
                           "revoked_database_unchanged", "pending_maintenance_rejected", "pending_maintenance_readonly"), True),
                       "cleanup": dict.fromkeys(("probe_containers_absent", "quota_volumes_absent", "probe_journal_absent",
                                                "fixture_image_absent", "scratch_database_absent"), True)},
                   "diagnostics": {"cleanup_started": True, "cleanup_completed": True}},
    }


@pytest.mark.parametrize("fault", ["absent", "wrong_contract", "bool_contract", "wrong_schema", "wrong_origin", "real_release_claim",
    "missing_synthetic_label", "wrong_database", "wrong_count", "float_count", "wrong_supporting_image", "identical_images",
    "bad_image_digest", "bad_script_digest", "missing_check", "false_check", "integer_check", "missing_cleanup", "false_cleanup"])
def test_bridge_update_receipt_fails_closed_for_incomplete_or_ambiguous_proof(fault):
    from test_linux_install import decode_update_observation

    value = update_observation()
    receipt = value["result"]["bridge_update_contract"]
    changes = {"wrong_contract": ("contract", 2), "bool_contract": ("contract", True), "wrong_schema": ("schema", 2),
               "wrong_origin": ("observed_from", "reflected_constant"), "real_release_claim": ("real_historical_release", True),
               "missing_synthetic_label": ("synthetic_missing_contract_image", False), "wrong_database": ("database_fixture", "live"),
               "wrong_count": ("fixed_probe_executions", 2), "float_count": ("fixed_probe_executions", 3.0),
               "wrong_supporting_image": ("supporting_image", "sha256:" + "f" * 64),
               "identical_images": ("missing_contract_image", receipt["supporting_image"]),
               "bad_image_digest": ("missing_contract_image", "private-image"), "bad_script_digest": ("fixed_script_sha256", "private-script")}
    if fault in changes:
        key, replacement = changes[fault]
        receipt[key] = replacement
    elif fault == "absent":
        del value["result"]["bridge_update_contract"]
    elif fault == "missing_check":
        del receipt["checks"]["pending_maintenance_rejected"]
    elif fault == "false_check":
        receipt["checks"]["missing_contract_import_error"] = False
    elif fault == "integer_check":
        receipt["checks"]["supporting_fixed_probe_passed"] = 1
    elif fault == "missing_cleanup":
        del receipt["cleanup"]
    else:
        receipt["cleanup"]["quota_volumes_absent"] = False
    with pytest.raises(ValueError, match="probe_bridge_contract_receipt_invalid"):
        decode_update_observation(json.dumps(value))


def test_bridge_update_receipt_projects_only_proven_fixed_fields():
    from test_linux_install import decode_update_observation

    value = update_observation()
    receipt = value["result"]["bridge_update_contract"]
    expected = json.loads(json.dumps(receipt))
    for target in (receipt, receipt["checks"], receipt["cleanup"]):
        target["private"] = "private log, path, key or candidate prose"
    projected = decode_update_observation(json.dumps(value))["result"]["bridge_update_contract"]
    assert projected == expected and "private log" not in json.dumps(projected)


@pytest.mark.parametrize("fault", [None, "unrelated_failure", "missing_contract_accepted", "container_left"])
def test_installed_bridge_probe_drives_real_probe_method_and_rejects_false_receipts(update_probe, monkeypatch, tmp_path, fault):
    """Only command/volume boundaries are fixtures here; no VM acceptance claim."""
    import shutil
    import sqlite3
    from contextlib import closing

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    import agentic_host.updater as module

    root = tmp_path / "private"
    root.mkdir()
    (root / "receipt").mkdir()
    monkeypatch.setattr(update_probe, "STATE_ROOT", root / "receipt")
    product = Path(__file__).parents[1]
    source = root / "live.sqlite3"
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript("PRAGMA user_version=3; CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
                        "CREATE TABLE records(kind TEXT,body TEXT); CREATE TABLE requests(id TEXT); CREATE TABLE events(id TEXT);")
        db.execute("INSERT INTO settings VALUES ('owner','private-owner')")
        db.execute("INSERT INTO records VALUES ('task','private-live-body')")
    before = source.read_bytes()
    original, missing = "sha256:" + "a" * 64, "sha256:" + "c" * 64
    calls, launches, builds, inspected_names = [], [], [], []
    removed = [False]
    class Volume:
        def __init__(self, parent, identity, size):
            assert size == 64
            self.directory = parent / identity
            self.mount = self.directory / "workspace"
        def create(self):
            self.mount.mkdir(parents=True)
            return self.mount
        def close(self):
            shutil.rmtree(self.directory)
    monkeypatch.setattr(module, "DiskVolume", Volume)
    monkeypatch.setattr(module, "ready", lambda run, memory: None)
    monkeypatch.setattr(module, "fence", lambda run, name, label: inspected_names.append(name))
    monkeypatch.setattr(module.os, "chown", lambda *args: None)
    def run(*args, **kwargs):
        calls.append(args)
        output, error, code = "", "", 0
        if args[:2] == ("docker", "build"):
            builds.append((Path(args[-1]) / "Dockerfile").read_text())
        elif args[:3] == ("docker", "image", "inspect"):
            output = missing
        elif args[:2] == ("docker", "run"):
            assert args[-4:] == ("--kill-after=3", "40", "python", "/fixed-probe.py")
            directory = Path(next(arg.split(",src=", 1)[1].split(",dst=", 1)[0] for arg in args if arg.endswith(",dst=/probe")))
            with closing(sqlite3.connect(directory / "controller.sqlite3")) as db:
                marker = db.execute("SELECT value FROM settings WHERE key='required_ingress_bridge_contract'").fetchone()
            image = args[args.index("--entrypoint") + 2]
            launches.append((image, bool(marker)))
            if image == missing and marker and fault != "missing_contract_accepted":
                code = 1
                error = ("ImportError: cannot import name 'BRIDGE_CONTRACT' from 'agentic_controller.bridge'"
                         if fault != "unrelated_failure" else "ImportError: private unrelated package failure")
        elif args[:3] == ("docker", "container", "ls") and fault == "container_left":
            output = "still-running"
        elif args[:3] == ("docker", "image", "rm"):
            removed[0] = True
        elif args[:3] == ("docker", "image", "ls"):
            output = original + ("\n" + missing if not removed[0] else "")
        return subprocess.CompletedProcess(args, code, output, error)
    manager = SimpleNamespace(root=root, product=product, run=run)
    if fault:
        with pytest.raises(AssertionError):
            update_probe.bridge_contract_probe(manager, original, "fixture-base", root, "a" * 32, source, 3)
    else:
        result = update_probe.bridge_contract_probe(manager, original, "fixture-base", root, "a" * 32, source, 3)
        from test_linux_install import decode_bridge_contract_receipt
        assert decode_bridge_contract_receipt(result, original) == result
        assert launches == [(original, True), (missing, True), (missing, False)]
        assert len(inspected_names) == 3 and len(set(inspected_names)) == 3
        assert not (root / "bridge-contract").exists()
        # The actual Docker build receives one fixed declaration removal only.
        program = json.loads(builds[0].split("\nRUN ", 1)[1])[2]
        source_fixture = root / "bridge.py"
        source_fixture.write_text("# fixture\nBRIDGE_CONTRACT = 1\nUNCHANGED = True\n")
        exec(compile(program.replace("/app/agentic_controller/bridge.py", str(source_fixture)), "fixture-removal", "exec"), {})
        assert source_fixture.read_text() == "# fixture\nUNCHANGED = True\n"
    assert source.read_bytes() == before
    assert all("compose" not in call and "systemctl" not in call for call in calls)


def test_probe_diagnostics_keep_command_import_cause_before_generic_update_error(update_probe, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    from agentic_host.manager import HostError
    from test_linux_install import decode_update_observation

    def run(*args, **kwargs):
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(args, 1, "", "ModuleNotFoundError: No module named 'agentic_controller'; private text")
    with pytest.raises(HostError):
        update_probe.observed_probe(lambda: update_probe.observed_run(run, "docker", "run", "fixed"), tmp_path)
    observation = update_observation()
    observation["result"] = {"state": "failed", "stage": "approved_candidate_activation", "error_code": "installed_update_failed",
                             "error_type": "RuntimeError", "diagnostics": {**update_probe.DIAGNOSTICS,
                             "updater_state": "failed", "updater_phase": "probing", "updater_error_code": "update_deployment_failed"}}
    result = decode_update_observation(json.dumps(observation))["result"]
    diagnostic = result["diagnostics"]
    assert diagnostic["command_failure"]["returncode"] == 1
    assert diagnostic["command_failure"]["stderr_signatures"]["import_error"]
    assert diagnostic["probe_failure"]["error_type"] == "HostError"
    assert diagnostic["updater_phase"] == "probing" and diagnostic["updater_error_code"] == "update_deployment_failed"
    assert diagnostic["probe_resources"]["free_disk_bytes"] > 0 and diagnostic["probe_resources"]["available_memory_bytes"] > 0
    assert "private" not in json.dumps(result) and "agentic_controller" not in json.dumps(result)


def test_probe_diagnostics_keep_volume_command_code_and_primary_identity(update_probe, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    from agentic_host.coding_sandbox import SandboxError
    from test_linux_install import decode_update_observation

    failure = SandboxError("private mount text", code="command_failed", operation="umount", exit_code=32)
    def probe():
        raise failure
    with pytest.raises(SandboxError) as observed:
        update_probe.observed_probe(probe, tmp_path)
    assert observed.value is failure
    observation = update_observation()
    observation["result"]["diagnostics"].update(update_probe.DIAGNOSTICS)
    observation["result"]["diagnostics"].update(cleanup_started=True, cleanup_completed=True)
    result = decode_update_observation(json.dumps(observation))["result"]
    assert result["diagnostics"]["probe_failure"]["operation"] == "umount"
    assert result["diagnostics"]["probe_failure"]["command_exit_code"] == 32
    assert result["diagnostics"]["probe_failure"]["error_code"] == "command_failed"
    assert "private" not in json.dumps(result)


def test_probe_command_timeout_never_retries_and_keeps_bounded_signatures(update_probe, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    calls = []
    failure = subprocess.TimeoutExpired(["docker", "run", "private"], 45, stderr=b"Permission denied private")
    def run(*args, **kwargs):
        calls.append(args)
        raise failure
    with pytest.raises(subprocess.TimeoutExpired) as observed:
        update_probe.observed_run(run, "docker", "run", "fixed", timeout=45)
    assert observed.value is failure and len(calls) == 1
    diagnostic = update_probe.DIAGNOSTICS["command_failure"]
    assert diagnostic["returncode"] is None and diagnostic["stderr_signatures"]["permission_denied"]
    assert "private" not in json.dumps(diagnostic)


def test_update_probe_new_diagnostics_discard_untrusted_fields():
    from test_linux_install import decode_update_observation

    observation = update_observation()
    observation["result"]["diagnostics"].update({
        "updater_state": "needs_reconcile", "updater_phase": "private", "updater_error_code": "update_fallback_unconfirmed",
        "probe_failure": {"error_code": "private", "error_type": "private", "operation": "private", "raw": "private",
                          "signatures": {"import_error": True, "raw": "private"}},
        "command_failure": {"returncode": "private", "stdout_bytes": "private", "stderr": "private", "stderr_signatures": {}},
        "probe_resources": {"free_disk_bytes": -1, "available_memory_bytes": "private", "raw": "private"},
    })
    result = decode_update_observation(json.dumps(observation))["result"]
    assert result["diagnostics"]["updater_phase"] == "unknown"
    assert result["diagnostics"]["updater_error_code"] == "update_fallback_unconfirmed"
    assert result["diagnostics"]["probe_failure"]["error_code"] == "unclassified_update_error"
    assert "private" not in json.dumps(result)


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


@pytest.mark.parametrize("message,signature", [
    ("cp: error writing private/path: No space left on device", "disk_full"),
    ("cp: cannot read private/path: Permission denied", "permission_denied"),
    ("Command with private arguments timed out after 30 seconds", "timeout"),
    ("cp: private/path: Input/output error", "io_error"),
])
def test_snapshot_failure_retains_exit_and_exact_job_without_private_output(tmp_path, message, signature):
    from test_linux_install import record_install_failure

    job = {"id": "f" * 32, "request": {"action": "snapshot", "private": "not-exported"}, "state": "failed", "message": message}
    exc = subprocess.CalledProcessError(1, ["ssh", "fixture", "agentic-home snapshot"],
                                       output="Operation " + job["id"] + "\n" + json.dumps(job), stderr="private stderr")
    report = tmp_path / "failure.json"
    def run(args, **kwargs):
        prior = json.loads(report.read_text())
        assert prior["process"]["returncode"] == 1
        assert prior["lifecycle_receipt"]["id"] == job["id"]
        assert "failure_observation" in kwargs["input"] and job["id"] in kwargs["input"]
        # This is a read-only diagnostic request, never a CLI lifecycle rerun.
        assert args[-1] == "sudo env AGENTIC_DISPOSABLE_VM=1 python3 -"
        return subprocess.CompletedProcess(args, 0, json.dumps({"jobs": [job], "selection": "exact_id",
            "filesystem": {"total_bytes": 120, "used_bytes": 119, "free_bytes": 1, "private": "excluded"}}), "")
    result = record_install_failure(exc, "nested_haos_shutdown_snapshot_restore", report, ["ssh", "fixture"], run=run)
    assert result["error_type"] == "CalledProcessError" and result["lifecycle_action"] == "snapshot"
    assert result["lifecycle_receipt"]["state"] == "failed"
    assert result["lifecycle_receipt"]["message_signatures"][signature]
    assert result["remote"]["jobs"][0]["message_signatures"][signature]
    assert "private" not in report.read_text() and "not-exported" not in report.read_text()


def test_timeout_diagnostics_keep_partial_operation_id_and_failed_probe(tmp_path):
    from test_linux_install import record_install_failure

    exc = subprocess.TimeoutExpired(["ssh", "fixture", "agentic-home snapshot"], 30,
                                    output=("Operation " + "a" * 32 + "\n").encode(), stderr=b"private timeout details")
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("private command", 20, stderr=b"private diagnostic output")
    result = record_install_failure(exc, "nested_haos_shutdown_snapshot_restore", tmp_path / "failure.json", ["ssh"], run=run)
    assert result["error_type"] == "TimeoutExpired" and result["timeout_seconds"] == 30
    assert result["process"]["returncode"] is None
    assert result["lifecycle_receipt"]["id"] == "a" * 32
    assert result["remote"] == "unavailable" and result["diagnostic_error_type"] == "TimeoutExpired"
    assert "private" not in json.dumps(result)


def test_remote_failure_observation_is_readonly_bounded_and_redacted(update_probe, monkeypatch):
    import sys

    monkeypatch.setattr(update_probe, "guard", lambda: None)
    jobs = {"f" * 32: {"id": "f" * 32, "request": {"action": "snapshot"}, "state": "failed",
                       "message": "cp: private file: No space left on device"}}
    calls = []
    def capture(args):
        calls.append(args)
        if "systemctl" in args[0]:
            return "ok", "ActiveState=failed\nResult=oom-kill\n"
        if "virsh" in args[0]:
            return "ok", "shut off\n"
        return "ok", "kernel: Out of memory: Killed process private process details"
    monkeypatch.setitem(sys.modules, "agentic_host.support", SimpleNamespace(
        _capture=capture, _read=lambda path, limit: json.dumps(jobs)))
    monkeypatch.setattr(update_probe.shutil, "disk_usage", lambda _: SimpleNamespace(total=120, used=119, free=1))
    value = update_probe.failure_observation("f" * 32, "snapshot")
    assert len(calls) == 4
    assert all(any(command in args[0] for command in ("systemctl", "virsh", "journalctl")) for args in calls)
    assert "--readonly" in calls[1]
    assert all("--lines=100" in args for args in calls[2:])
    assert value["broker_unit"]["oom_killed"] and value["kernel"]["signatures"]["oom"]
    assert value["jobs"][0]["operation"] == "cp" and value["jobs"][0]["message_signatures"]["disk_full"]
    assert "private" not in json.dumps(value)


@pytest.mark.parametrize("progress_write_fails", [False, True])
@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, ["ssh", "agentic-home snapshot"]),
                                  subprocess.TimeoutExpired(["ssh", "agentic-home snapshot"], 30),
                                  AssertionError("private assertion")])
def test_actual_installer_failure_still_cleans_up_when_diagnostics_break(tmp_path, monkeypatch, error, progress_write_fails):
    import test_linux_install as module

    # Drive the actual installer harness to its first disposable command; every
    # external operation is substituted, so this regression never runs Docker.
    monkeypatch.setattr(module, "ROOT", tmp_path)
    for name in ("dist", "scripts", "evidence", "build"):
        (tmp_path / name).mkdir()
    (tmp_path / "dist/agentic-home-0.7.0-laptop-amd64.iso").write_bytes(b"fixture")
    (tmp_path / "scripts/build_host.py").write_text("def autoinstall(profile): return {'autoinstall': {'late-commands': [], 'interactive-sections': []}}\n")
    def command(*args, **kwargs):
        if args[0] == "ssh-keygen":
            (tmp_path / "test-key.pub").write_text("fixture-public-key")
            return ""
        raise error
    monkeypatch.setattr(module, "command", command)
    order = []
    def diagnostics(*args, **kwargs):
        order.append("diagnostics")
        raise OSError("private diagnostic failure")
    def run(args, **kwargs):
        order.append(tuple(args[:3]))
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(module, "record_install_failure", diagnostics)
    monkeypatch.setattr(module.subprocess, "run", run)
    original_write = Path.write_text
    progress_writes = [0]
    def writing(path, *args, **kwargs):
        if path.name == "installed-progress-laptop-amd64.json":
            progress_writes[0] += 1
            if progress_write_fails and progress_writes[0] > 1:
                raise OSError("private progress failure")
        return original_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", writing)
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda _: True))
    with pytest.raises(type(error)) as observed:
        module.test_full_linux_install_with_nested_haos(tmp_path, request)
    assert observed.value is error
    assert order == ["diagnostics", ("docker", "stop", "-t"), ("docker", "run", "--rm"), ("docker", "rm", "-fv")]


@pytest.mark.parametrize("stdout,returncode", [("", 255), ("private malformed output", 0), ('{"state":"failed"}', 1)])
def test_probe_process_diagnostics_are_saved_before_json_parse(tmp_path, stdout, returncode):
    from test_linux_install import probe_json, record_install_failure

    process = subprocess.CompletedProcess(["ssh", "fixture"], returncode, stdout, "Connection closed private details")
    report = tmp_path / "process.json"
    if stdout.startswith("{"):
        assert probe_json(process, report) == {"state": "failed"}
    else:
        with pytest.raises(json.JSONDecodeError) as captured:
            probe_json(process, report)
        result = record_install_failure(captured.value, "installed_owner_and_role_auth", tmp_path / "failure.json", None)
        assert result["error_type"] == "JSONDecodeError"
        assert result["process"]["returncode"] == returncode
    observed = json.loads(report.read_text())
    assert observed["returncode"] == returncode and observed["stderr_signatures"]["ssh_transport"]
    assert "private" not in report.read_text()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_actual_update_cleanup_survives_checkpoint_failure_and_preserves_primary(update_probe, monkeypatch, tmp_path, cleanup_fails, capsys):
    import sqlite3

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "host"))
    import agentic_host.manager as manager_module
    import agentic_host.updater as updater_module

    monkeypatch.setattr(update_probe, "guard", lambda: None)
    state = tmp_path / "probe"
    state.mkdir()
    monkeypatch.setattr(update_probe, "STATE_ROOT", state)
    root = tmp_path / "host"
    root.mkdir()
    environment, records, database = root / "runtime.env", root / "updates.json", root / "controller.sqlite3"
    environment.write_text("# fixture\n")
    records.write_text("{}")
    with sqlite3.connect(database) as db:
        db.executescript("PRAGMA user_version=3; CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE records(kind TEXT);")
    stopped = []
    def stop():
        stopped.append(True)
        if cleanup_fails:
            raise PermissionError("private cleanup failure")
    service = SimpleNamespace(environment=environment, path=records, database=database,
                              image=lambda: "sha256:" + "a" * 64, health=lambda: {"version": "0.7.0", "ha_version": None},
                              stop=stop, compose=lambda *args, **kwargs: None)
    monkeypatch.setattr(manager_module, "Manager", lambda: SimpleNamespace(root=root))
    monkeypatch.setattr(updater_module, "Updater", lambda manager: service)
    def run(*args, **kwargs):
        if args[:2] == ("docker", "tag"):
            raise RuntimeError("installed_update_failed")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(manager_module, "run", run)
    original = update_probe.persist
    def persist(name, value):
        if name == "checkpoint.json" and value.get("stage") == "restore_original_image_and_cleanup":
            raise OSError(28, "No space left on private filesystem")
        return original(name, value)
    monkeypatch.setattr(update_probe, "persist", persist)
    prior_umask = update_probe.os.umask(0o077)
    try:
        assert update_probe.execute() == 1
    finally:
        update_probe.os.umask(prior_umask)
    result = json.loads(capsys.readouterr().out)
    assert stopped == [True]
    assert result["error_code"] == "installed_update_failed" and result["error_type"] == "RuntimeError"
    assert result["stage"] == "build_fixed_label_candidate"
    assert result["diagnostics"]["cleanup_started"] and result["diagnostics"]["cleanup_checkpoint_failed"]
    assert result["diagnostics"]["cleanup_completed"] is not cleanup_fails
    if cleanup_fails:
        assert result["diagnostics"]["cleanup_error_type"] == "PermissionError"
    assert "private" not in json.dumps(result)
