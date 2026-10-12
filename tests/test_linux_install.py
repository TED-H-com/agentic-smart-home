"""Full Linux installation rehearsal. Every disk/credential is temporary and isolated."""
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
def bootstrap_job_diagnostics(output):
    """Keep only fixed job status and known codes, never CLI text or paths."""
    from installed_update_probe import lifecycle_error

    result = {"state": "unknown", "error_code": "unclassified_host_error"}
    try:
        if len(output.encode()) > 65536:
            return result
        value = json.loads(output[output.index("{"):])
        if value.get("state") in {"running", "succeeded", "failed", "needs_reconcile"}:
            result["state"] = value["state"]
        if code := lifecycle_error(value.get("message")):
            result["error_code"] = code
        elif result["state"] == "succeeded":
            result["error_code"] = None
    except (ValueError, TypeError, AttributeError):
        pass
    return result


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def probe_json(result, diagnostic_path):
    from installed_update_probe import process_diagnostics

    diagnostic_path.write_text(json.dumps(process_diagnostics(result), indent=2) + "\n")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        # Preserve the original parser error while allowing the outer failure
        # collector to retain the actual process/transport evidence.
        exc.stdout, exc.stderr, exc.returncode, exc.cmd = result.stdout, result.stderr, result.returncode, result.args
        raise


def failure_observation_public(value):
    from installed_update_probe import (
        HOST_VM_STATES,
        compatibility_receipt,
        lifecycle_receipt,
        process_diagnostics,
    )

    if not isinstance(value, dict):
        raise ValueError("invalid_failure_observation")
    flags = process_diagnostics(subprocess.CompletedProcess([], None, "", ""))["stderr_signatures"]
    result = {"selection": value.get("selection") if value.get("selection") in {"exact_id", "recent_action_unbound"} else "unknown",
              "jobs": []}
    if isinstance(value.get("jobs"), list):
        result["jobs"] = [receipt for item in value["jobs"][:5] if (receipt := lifecycle_receipt(item)) is not None]
    if isinstance(value.get("filesystem"), dict):
        result["filesystem"] = {key: number for key in ("total_bytes", "used_bytes", "free_bytes")
                                if type(number := value["filesystem"].get(key)) is int and 0 <= number <= 2**63}
    result["compatibility"] = compatibility_receipt(value.get("compatibility"))
    state = value.get("provision_state")
    if isinstance(state, dict):
        result["provision_state"] = {**{key: state.get(key) is True for key in ("record_readable", "owner_reference_valid")},
            **{key: state.get(key) if type(state.get(key)) is bool else None for key in ("complete", "restore_pending")}, "files": {}}
        files = state.get("files") if isinstance(state.get("files"), dict) else {}
        for name in ("disk", "partial_disk", "nvram"):
            item = files.get(name) if isinstance(files.get(name), dict) else {}
            kind = item.get("kind")
            result["provision_state"]["files"][name] = {
                "kind": kind if isinstance(kind, str) and kind in {"absent", "unreadable", "regular", "directory", "symlink", "other"} else "unknown"}
            if kind == "regular" and type(item.get("bytes")) is int and 0 <= item["bytes"] <= 2**63:
                result["provision_state"]["files"][name]["bytes"] = item["bytes"]
    for name in ("domain", "network"):
        item = value.get(name)
        if isinstance(item, dict):
            status = item.get("observation")
            result[name] = {"observation": status if isinstance(status, str) and status in {
                "ok", "timeout", "output_limit", "probe_failed", "command_missing", "permission_denied"} else "unknown",
                "xml_valid": item.get("xml_valid") is True,
                **{key: item.get(key) if type(item.get(key)) is bool else None for key in ("name_matches", "ownership_matches")}}
            if name == "domain" and "disk_matches" in item:
                result[name]["disk_matches"] = item["disk_matches"] if type(item["disk_matches"]) is bool else None
    for name in ("broker_unit", "vm", "journal", "kernel"):
        item = value.get(name, {})
        if not isinstance(item, dict):
            continue
        signatures = item.get("signatures", {})
        if not isinstance(signatures, dict):
            signatures = {}
        result[name] = {"observation": item.get("observation") if item.get("observation") in {
            "ok", "timeout", "output_limit", "probe_failed", "command_missing", "permission_denied"} else "unknown",
            "signatures": {key: signatures.get(key) is True for key in flags}}
        if name == "vm":
            result[name]["state"] = item.get("state") if isinstance(item.get("state"), str) and item["state"] in HOST_VM_STATES else "unknown"
        if name == "broker_unit":
            result[name]["active"] = item.get("active") if item.get("active") in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"
            result[name]["oom_killed"] = item.get("oom_killed") is True
    return result


def record_install_failure(exc, phase, report, ssh, *, run=subprocess.run):
    """Never retry effects; retain safe original diagnostics before VM teardown."""
    from installed_update_probe import LIFECYCLE_ACTIONS, lifecycle_receipt, process_diagnostics

    stdout, stderr = getattr(exc, "stdout", "") or "", getattr(exc, "stderr", "") or ""
    process = subprocess.CompletedProcess([], getattr(exc, "returncode", None), stdout, stderr)
    result = {"phase": phase, "error_type": type(exc).__name__ if type(exc).__name__ in {
        "CalledProcessError", "TimeoutExpired", "AssertionError", "JSONDecodeError", "TypeError"} else "OtherError",
        "process": process_diagnostics(process), "remote": "not_attempted"}
    if isinstance(exc, subprocess.TimeoutExpired) and type(exc.timeout) in {int, float} and 0 <= exc.timeout <= 3600:
        result["timeout_seconds"] = exc.timeout
    args = getattr(exc, "cmd", ())
    remote_command = args[-1] if isinstance(args, (tuple, list)) and args and isinstance(args[-1], str) else ""
    action = next((item for item in LIFECYCLE_ACTIONS if remote_command == "agentic-home " + item or
                   (item == "restore" and remote_command.startswith("agentic-home restore --snapshot "))), None)
    result["lifecycle_action"] = action
    output = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
    receipt = None
    if isinstance(output, str) and len(output.encode()) <= 65536:
        try:
            receipt = lifecycle_receipt(json.loads(output[output.index("{"):]))
        except (ValueError, TypeError, AttributeError):
            pass
        if receipt is None and (match := re.search(r"(?m)^Operation ([a-f0-9]{32})$", output)):
            receipt = lifecycle_receipt({"id": match[1], "action": action, "state": "unknown"})
    result["lifecycle_receipt"] = receipt
    report.write_text(json.dumps(result, indent=2) + "\n")
    if ssh is None:
        return result
    identifier = receipt["id"] if receipt else None
    source = (ROOT / "tests/installed_update_probe.py").read_text()
    encoded = base64.b64encode(source.encode()).decode()
    script = ("import base64,json\ns=base64.b64decode('" + encoded + "').decode()\n"
              "ns={'__name__':'installed_failure_diagnostics'}\nexec(compile(s,'installed_failure_diagnostics','exec'),ns)\n"
              f"print(json.dumps(ns['failure_observation']({identifier!r},{action!r})),flush=True)\n")
    try:
        observed = run([*ssh, "sudo env AGENTIC_DISPOSABLE_VM=1 python3 -"], capture_output=True,
                       text=True, input=script, timeout=20)
        result["diagnostic_process"] = process_diagnostics(observed)
        if observed.returncode != 0 or len(observed.stdout.encode()) > 65536:
            raise ValueError("diagnostic_unavailable")
        result["remote"] = failure_observation_public(json.loads(observed.stdout))
    except Exception as diagnostic_error:
        result["remote"] = "unavailable"
        result["diagnostic_error_type"] = type(diagnostic_error).__name__ if isinstance(diagnostic_error, (
            subprocess.TimeoutExpired, ValueError, OSError)) else "OtherError"
    report.write_text(json.dumps(result, indent=2) + "\n")
    return result


def decode_bridge_contract_receipt(value, supporting_image):
    """Reject partial/ambiguous proof; publish only fixed mechanism and digests."""
    checks = ("revoked_history_marker_copied", "supporting_fixed_probe_passed", "missing_contract_fixed_probe_rejected",
              "missing_contract_import_error", "missing_contract_without_history_passed", "revoked_database_unchanged",
              "pending_maintenance_rejected", "pending_maintenance_readonly")
    cleanup = ("probe_containers_absent", "quota_volumes_absent", "probe_journal_absent", "fixture_image_absent", "scratch_database_absent")
    fixed = {"contract": 1, "observed_from": "installed_host_fixed_probe", "schema": 3,
             "database_fixture": "isolated_structure_only_revoked_bridge", "fixed_probe_executions": 3,
             "synthetic_missing_contract_image": True, "real_historical_release": False}
    if (not isinstance(value, dict) or any(type(value.get(key)) is not type(expected) or value[key] != expected for key, expected in fixed.items()) or
            value.get("supporting_image") != supporting_image or
            not isinstance(value.get("missing_contract_image"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["missing_contract_image"]) or
            value["missing_contract_image"] == supporting_image or
            not isinstance(value.get("fixed_script_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["fixed_script_sha256"]) or
            any(not isinstance(value.get(group), dict) or any(value[group].get(key) is not True for key in keys)
                for group, keys in (("checks", checks), ("cleanup", cleanup)))):
        raise ValueError("probe_bridge_contract_receipt_invalid")
    return {**fixed, **{key: value[key] for key in ("supporting_image", "missing_contract_image", "fixed_script_sha256")},
            "checks": dict.fromkeys(checks, True), "cleanup": dict.fromkeys(cleanup, True)}


def decode_update_observation(output):
    """Validate/project private probe receipts; never publish arbitrary remote text."""
    from installed_update_probe import ERROR_TYPES, SAFE_ERRORS, STAGES, UPDATE_PHASES, process_diagnostics

    if not output or len(output.encode()) > 65536:
        raise ValueError("probe_output_missing_or_oversized")
    value = json.loads(output)
    if not isinstance(value, dict) or set(value) != {"checkpoint", "result", "unit", "process"}:
        raise ValueError("probe_observation_invalid")
    checkpoint = value["checkpoint"]
    if checkpoint is not None and (not isinstance(checkpoint, dict) or checkpoint.get("state") != "running" or checkpoint.get("stage") not in STAGES):
        raise ValueError("probe_checkpoint_invalid")
    clean = {"checkpoint": None if checkpoint is None else {"state": "running", "stage": checkpoint["stage"]}}
    unit = value["unit"]
    if not isinstance(unit, dict):
        raise ValueError("probe_unit_invalid")
    clean["unit"] = {
        "ActiveState": unit.get("ActiveState") if unit.get("ActiveState") in {"active", "activating", "deactivating", "inactive", "failed"} else "unknown",
        "SubState": unit.get("SubState") if unit.get("SubState") in {"running", "exited", "failed", "dead", "start", "stop", "stop-sigterm", "stop-sigkill"} else "unknown",
        "Result": unit.get("Result") if unit.get("Result") in {"success", "exit-code", "signal", "core-dump", "timeout", "oom-kill", "resources"} else "unknown",
        **{key: unit.get(key) if isinstance(unit.get(key), str) and re.fullmatch(r"[0-9]{1,3}", unit[key]) else None
           for key in ("ExecMainCode", "ExecMainStatus")},
    }
    process = value["process"]
    if not isinstance(process, dict):
        raise ValueError("probe_process_invalid")
    clean["process"] = {k: process[k] for k in ("stdout_bytes", "stderr_bytes") if type(process.get(k)) is int and process[k] >= 0}
    clean["process"]["stderr_truncated"] = process.get("stderr_truncated") is True
    flags = process.get("stderr_signatures", {})
    clean["process"]["stderr_signatures"] = {k: flags.get(k) is True for k in (
        "traceback", "import_error", "syntax_error", "permission_denied", "ssh_transport", "process_killed", "sudo_error")}
    result = value["result"]
    if result is None:
        clean["result"] = None
        return clean
    if not isinstance(result, dict) or result.get("state") not in {"passed", "failed"}:
        raise ValueError("probe_result_invalid")
    diagnostic = result.get("diagnostics", {})
    if not isinstance(diagnostic, dict):
        raise ValueError("probe_diagnostics_invalid")
    projected = {"state": result["state"], "diagnostics": {
        k: diagnostic.get(k) is True for k in ("cleanup_started", "cleanup_completed")}}
    for key in ("cleanup_checkpoint_failed", "result_persist_failed"):
        if key in diagnostic:
            projected["diagnostics"][key] = diagnostic[key] is True
    if "cleanup_error_type" in diagnostic:
        projected["diagnostics"]["cleanup_error_type"] = diagnostic["cleanup_error_type"] if diagnostic["cleanup_error_type"] in ERROR_TYPES else "OtherError"
        error = diagnostic.get("cleanup_error_code")
        projected["diagnostics"]["cleanup_error_code"] = error if error in SAFE_ERRORS else "unclassified_update_error"
    if diagnostic.get("updater_state") in {"failed", "needs_reconcile", "rolled_back"}:
        projected["diagnostics"]["updater_state"] = diagnostic["updater_state"]
        projected["diagnostics"]["updater_phase"] = diagnostic.get("updater_phase") if diagnostic.get("updater_phase") in UPDATE_PHASES else "unknown"
        error = diagnostic.get("updater_error_code")
        projected["diagnostics"]["updater_error_code"] = error if error in SAFE_ERRORS else "unclassified_update_error"
    signatures = process_diagnostics(subprocess.CompletedProcess([], None, "", ""))["stderr_signatures"]
    probe_error = diagnostic.get("probe_failure")
    if isinstance(probe_error, dict):
        projected["diagnostics"]["probe_failure"] = {
            "error_code": probe_error.get("error_code") if probe_error.get("error_code") in SAFE_ERRORS else "unclassified_update_error",
            "error_type": probe_error.get("error_type") if probe_error.get("error_type") in ERROR_TYPES else "OtherError",
            "signatures": {key: probe_error.get("signatures", {}).get(key) is True for key in signatures},
        }
        if probe_error.get("operation") in {"umount", "mount", "mkfs.ext4", "findmnt", "docker.run", "docker.inspect", "docker.info", "docker.rm", "docker.kill"}:
            projected["diagnostics"]["probe_failure"]["operation"] = probe_error["operation"]
        if type(probe_error.get("command_exit_code")) is int and -128 <= probe_error["command_exit_code"] <= 255:
            projected["diagnostics"]["probe_failure"]["command_exit_code"] = probe_error["command_exit_code"]
    command = diagnostic.get("command_failure")
    if isinstance(command, dict):
        projected["diagnostics"]["command_failure"] = {
            "returncode": command.get("returncode") if type(command.get("returncode")) is int and -255 <= command["returncode"] <= 255 else None,
            "stderr_signatures": {key: command.get("stderr_signatures", {}).get(key) is True for key in signatures},
            **{key: command[key] for key in ("stdout_bytes", "stderr_bytes") if type(command.get(key)) is int and command[key] >= 0},
            "stderr_truncated": command.get("stderr_truncated") is True,
        }
    resources = diagnostic.get("probe_resources")
    if isinstance(resources, dict):
        projected["diagnostics"]["probe_resources"] = {key: resources[key] for key in ("free_disk_bytes", "available_memory_bytes")
                                                      if type(resources.get(key)) is int and resources[key] >= 0}
    if result["state"] == "failed":
        projected.update(error_code=result.get("error_code") if result.get("error_code") in SAFE_ERRORS else "unclassified_update_error",
                         error_type=result.get("error_type") if result.get("error_type") in ERROR_TYPES else "OtherError",
                         stage=result.get("stage") if result.get("stage") in STAGES else "unknown")
    else:
        actual = ["ed25519_signature_verification", "bounded_candidate_probe", "compose_activation", "authenticated_owner_smoke",
                  "consistent_snapshot", "schema3", "maintenance_gate", "same_database_rollback", "post_deployment_row_preserved",
                  "revoked_bridge_fixed_probe_enforced", "pending_bridge_operation_blocked", "isolated_probe_cleanup_verified"]
        fixtures = ["ephemeral_publisher", "preloaded_registry_delivery", "candidate_changes_only_image_label", "synthetic_post_deployment_record",
                    "synthetic_missing_bridge_contract_image", "isolated_structure_only_revoked_bridge"]
        if (result.get("actual") != actual or result.get("fixtures") != fixtures or result.get("final_schema") != 3 or
                result.get("real_cloud") is not False or result.get("real_publisher_release") is not False or
                result.get("rollback_restored_backup") is not False or result.get("registry_delivery_calls") != 1 or
                not all(projected["diagnostics"].get(k) for k in ("cleanup_started", "cleanup_completed")) or
                any(not isinstance(result.get(k), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", result[k]) for k in ("original_image", "candidate_image")) or
                result["original_image"] == result["candidate_image"]):
            raise ValueError("probe_success_receipt_invalid")
        projected.update(observed_from="disposable_installed_linux_vm_root", actual=actual, fixtures=fixtures,
                         final_schema=3, real_cloud=False, real_publisher_release=False, rollback_restored_backup=False,
                         registry_delivery_calls=1, original_image=result["original_image"], candidate_image=result["candidate_image"],
                         bridge_update_contract=decode_bridge_contract_receipt(result.get("bridge_update_contract"), result["original_image"]))
    clean["result"] = projected
    return clean


def installed_update(ssh, source, report, *, run=subprocess.run, clock=time.monotonic, pause=time.sleep):
    """Launch once, then recover only the same fixed private receipt over fresh SSH."""
    from installed_update_probe import STATE_ROOT, process_diagnostics

    evidence = {"state": "failed", "error_code": "probe_result_unconfirmed", "transport": {}}
    def save():
        report.write_text(json.dumps(evidence, indent=2) + "\n")
    def invoke(command, **kwargs):
        try:
            return run([*ssh, command], capture_output=True, text=True, timeout=30, **kwargs)
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess([], None, exc.stdout or "", exc.stderr or "")
    encoded = base64.b64encode(source.encode()).decode()
    launcher = ("import base64,json\ns=base64.b64decode('" + encoded + "').decode()\n"
                "ns={'__name__':'installed_probe_launcher'}\nexec(compile(s,'installed_update_probe','exec'),ns)\n"
                "print(json.dumps(ns['launch'](s)),flush=True)\n")
    started = invoke("sudo env AGENTIC_DISPOSABLE_VM=1 python3 -", input=launcher)
    evidence["transport"]["launch"] = process_diagnostics(started)
    save()  # Preserve return code/flags BEFORE attempting to parse anything.
    deadline = clock() + 660
    attempts = 0
    misses = 0
    while clock() < deadline:
        response = invoke("sudo env AGENTIC_DISPOSABLE_VM=1 python3 " + str(STATE_ROOT / "probe.py") + " --observe")
        attempts += 1
        evidence["transport"].update(observation_attempts=attempts, observation=process_diagnostics(response))
        save()
        try:
            if response.returncode != 0:
                raise ValueError("probe_observation_transport")
            observed = decode_update_observation(response.stdout)
            evidence["observation"] = observed
        except (ValueError, TypeError, AttributeError):
            misses += 1
            evidence["error_code"] = "probe_observation_unavailable"
            save()
            if misses >= 3:
                return evidence
            pause(2)
            continue
        misses = 0
        unit = observed["unit"]
        if unit["ActiveState"] in {"inactive", "failed", "unknown"} or unit["SubState"] == "exited":
            result = observed["result"]
            if result is not None:
                evidence.update(result)
            if not (result is not None and result["state"] == "passed" and unit["ActiveState"] == "active" and unit["SubState"] == "exited" and
                    unit["Result"] == "success" and unit["ExecMainCode"] == "1" and unit["ExecMainStatus"] == "0"):
                evidence["state"] = "failed"
                evidence.setdefault("error_code", "probe_completion_unconfirmed")
            else:
                evidence.pop("error_code", None)
            save()
            return evidence
        save()
        pause(2)
    evidence["error_code"] = "probe_observation_deadline"
    save()
    return evidence


def test_full_linux_install_with_nested_haos(tmp_path, request):
    if not request.config.getoption("--linux-install"):
        pytest.skip("Use --linux-install to install onto a disposable virtual disk")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    profile = os.environ.get("AGENTIC_INSTALL_TEST_PROFILE", "laptop-amd64")
    assert profile in {"laptop-amd64", "server-amd64"}
    iso = ROOT / "dist" / ("agentic-home-0.7.0-" + profile + ".iso")
    assert iso.exists()
    with iso.open("rb") as stream:
        iso_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    spec = importlib.util.spec_from_file_location("builder", ROOT / "scripts/build_host.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    command("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "test-key"))
    config = build.autoinstall({"id": profile})
    autoinstall = config["autoinstall"]
    autoinstall.pop("interactive-sections")
    autoinstall["identity"] = {"hostname": "agentic-disposable", "username": "tester", "password": "!"}
    autoinstall["ssh"] = {"install-server": True, "allow-pw": False,
                          "authorized-keys": [(tmp_path / "test-key.pub").read_text().strip()]}
    autoinstall["storage"] = {"layout": {"name": "direct", "match": {"serial": "AGENTIC_TEST_ONLY"}}}
    autoinstall["shutdown"] = "poweroff"
    autoinstall["late-commands"] += [
        "printf 'tester ALL=(root) NOPASSWD: ALL\\n' > /target/etc/sudoers.d/90-agentic-test",
        "chmod 440 /target/etc/sudoers.d/90-agentic-test"]
    (tmp_path / "user-data").write_text("#cloud-config\n" + json.dumps(config, indent=2))
    # Connect from QEMU's loopback: Docker's outer 172.17/16 otherwise overlaps
    # Docker inside the guest after firstboot and breaks the return route.
    (tmp_path / "ssh-proxy.py").write_text('''import socket,sys,threading
s=socket.create_connection(('127.0.0.1',2222))
def forward():
 while data:=sys.stdin.buffer.read1(65536): s.sendall(data)
 s.shutdown(socket.SHUT_WR)
threading.Thread(target=forward,daemon=True).start()
while data:=s.recv(65536):
 sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
''')
    name = "agentic-linux-install-test-" + uuid.uuid4().hex[:10]
    started = time.time()
    progress = {"runtime_location": "disposable_outer_linux_vm", "iso_sha256": iso_hash,
                "result": "running", "cloud_login": "not_run"}
    progress_path = ROOT / "evidence" / ("installed-progress-" + profile + ".json")

    def phase(name):
        progress.update(phase=name, duration_seconds=round(time.time() - started))
        progress_path.write_text(json.dumps(progress, indent=2) + "\n")

    try:
        phase("prepare_disposable_media")
        command("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tmp_path}:/lab",
                "-v", f"{iso}:/source.iso:ro", "agentic-home-builder:0.2.0", "xorriso",
                "-indev", "/source.iso", "-outdev", "/lab/test.iso", "-boot_image", "any", "replay",
                "-map", "/lab/user-data", "/agentic-seed/user-data", "-commit")
        command("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tmp_path}:/lab",
                "agentic-home-builder:0.2.0", "qemu-img", "create", "-f", "qcow2", "/lab/linux.qcow2", "120G")
        qemu = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-machine", "q35", "-m", "10240", "-smp", "4",
                "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
                "-drive", "if=pflash,format=raw,file=/lab/nvram.fd",
                "-drive", "file=/lab/linux.qcow2,format=qcow2,if=none,id=disk",
                "-device", "virtio-blk-pci,drive=disk,serial=AGENTIC_TEST_ONLY",
                "-netdev", "user,id=net,hostfwd=tcp:0.0.0.0:2222-:22", "-device", "virtio-net-pci,netdev=net",
                "-display", "none", "-serial", "file:/lab/serial.log", "-monitor", "unix:/lab/monitor.sock,server,nowait"]
        command("docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                "cp", "/usr/share/OVMF/OVMF_VARS_4M.fd", "/lab/nvram.fd")
        common = ["docker", "run", "-d", "--name", name, "--device", "/dev/kvm", "--memory", "12g", "--cpus", "4",
                  "-p", "127.0.0.1::2222", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0"]
        phase("ubuntu_installation")
        command(*common, *qemu, "-cdrom", "/lab/test.iso", "-boot", "d")
        for _ in range(180):
            state = json.loads(command("docker", "inspect", name))[0]["State"]
            if not state["Running"]:
                assert state["ExitCode"] == 0 and not state["OOMKilled"]
                break
            time.sleep(5)
        else:
            raise AssertionError("Linux installer did not power off within 15 minutes")
        command("docker", "rm", "-fv", name)
        phase("installed_firstboot")
        command(*common, *qemu)
        ssh = ["ssh", "-i", str(tmp_path / "test-key"), "-o", "BatchMode=yes",
               "-o", f"ProxyCommand=docker exec -i {name} python3 /lab/ssh-proxy.py",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
               "tester@127.0.0.1"]
        for _ in range(240):
            result = subprocess.run([*ssh, "test -f /var/lib/agentic-home/installed.json"], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(5)
        else:
            logs = subprocess.run([*ssh, "journalctl -u agentic-home-firstboot --no-pager -n 50"], capture_output=True, text=True)
            raise AssertionError("Firstboot did not complete: " + logs.stdout[-4000:])
        phase("installed_identity_and_images")
        host = json.loads(command(*ssh, "agentic-home status"))
        assert not host["provisioned"] and host["kvm"]
        installed = json.loads(command(*ssh, "sudo cat /var/lib/agentic-home/installed.json"))
        assert installed["version"] == "0.7.0" and installed["profile"] == profile
        image_lock = json.loads(command(*ssh, "cat /opt/agentic-home/images.lock.json"))
        expected_tags = {f"agentic-home-{service}:0.7.0" for service in ("controller", "runner", "linejs", "hermes", "preview")}
        assert set(image_lock) == expected_tags
        for tag, digest in image_lock.items():
            assert command(*ssh, "sudo docker image inspect --format '{{.Id}}' " + tag) == digest
        source_lock = json.loads(command(*ssh, "sudo cat /var/lib/agentic-home/coding/source.json"))
        assert source_lock == json.loads(command(*ssh, "cat /opt/agentic-home/assets/coding-source.json"))
        assert command(*ssh, "sudo git -C /var/lib/agentic-home/coding/source rev-parse HEAD") == source_lock["commit"]
        assert command(*ssh, "tailscale version").splitlines()[0] == "1.104.1"
        assert json.loads(command(*ssh, "sudo tailscale status --json"))["BackendState"] == "NeedsLogin"
        assert command(*ssh, "systemctl is-active agentic-home agentic-home-host agentic-home-proxy") == "active\nactive\nactive"
        assert command(*ssh, "curl -fsS http://localhost:8099/api/deployment")
        phase("installed_owner_and_role_auth")
        auth_result = subprocess.run([*ssh, "python3 -"], capture_output=True, text=True, timeout=120,
                                     input=(ROOT / "tests/installed_probe.py").read_text())
        auth_probe = probe_json(auth_result, ROOT / "evidence" / ("installed-process-auth-" + profile + ".json"))
        (ROOT / "evidence" / ("installed-auth-" + profile + ".json")).write_text(
            json.dumps(auth_probe, indent=2) + "\n")
        assert auth_result.returncode == 0 and auth_probe.get("result") != "failed", auth_probe
        # Exercise independent console recovery before adding any household VM.
        phase("controller_stopped_console_recovery")
        diagnostics = json.loads(command(*ssh, "agentic-home diagnostics"))
        assert diagnostics["observations"]
        assert all(item["version_refs"]["installed_application"] == installed["version"]
                   for item in diagnostics["observations"])
        installation = next(item for item in diagnostics["observations"] if item["target_component"] == "installation")
        assert installation["status"] == "collected" and installation["data"]["profile"] == profile
        compose = ("sudo docker compose --env-file /var/lib/agentic-home/host/runtime.env "
                   "-f /opt/agentic-home/host/compose.yaml")
        command(*ssh, compose + " stop controller")
        try:
            unavailable = subprocess.run([*ssh, "curl --max-time 3 -fsS http://localhost:8099/health"], capture_output=True)
            assert unavailable.returncode != 0
            rescue = json.loads(command(*ssh, "agentic-home diagnostics"))
            assert rescue["observations"]
            assert all(item["version_refs"]["installed_application"] == installed["version"]
                       for item in rescue["observations"])
            command(*ssh, "agentic-home support-bundle --output /home/tester/support-probe.json")
            support = json.loads(command(*ssh, "cat /home/tester/support-probe.json"))
            assert support["format"] == "agentic-support-bundle-v1"
            (ROOT / "evidence" / ("installed-rescue-" + profile + ".json")).write_text(json.dumps({
                "runtime_location": "disposable_outer_linux_vm", "controller_stopped": True,
                "diagnostics": rescue, "support_bundle": "created", "cloud_login": "not_run"}, indent=2) + "\n")
        finally:
            command(*ssh, compose + " up -d --wait controller")
        phase("installed_sandbox_quota_and_fencing")
        sandbox_result = subprocess.run([*ssh, "sudo python3 -"], capture_output=True, text=True,
                                       input=(ROOT / "tests/installed_sandbox_probe.py").read_text(), timeout=120)
        # Preserve the probe's allowlisted diagnostics before asserting a failure;
        # never publish arbitrary SSH stderr or workspace contents.
        sandbox_probe = probe_json(sandbox_result, ROOT / "evidence" / ("installed-process-sandbox-" + profile + ".json"))
        (ROOT / "evidence" / ("installed-sandbox-" + profile + ".json")).write_text(
            json.dumps(sandbox_probe, indent=2) + "\n")
        assert sandbox_result.returncode == 0 and sandbox_probe["result"] == "passed", sandbox_probe
        phase("installed_coding_and_independent_verifier")
        coding_result = subprocess.run([*ssh, "sudo python3 -"], capture_output=True, text=True,
                                      input=(ROOT / "tests/installed_coding_probe.py").read_text(), timeout=150)
        coding_probe = probe_json(coding_result, ROOT / "evidence" / ("installed-process-coding-" + profile + ".json"))
        coding_probe["installed_source"] = source_lock["commit"]
        (ROOT / "evidence" / ("installed-coding-" + profile + ".json")).write_text(json.dumps(coding_probe, indent=2) + "\n")
        assert coding_result.returncode == 0 and coding_probe["status"] == "passed", coding_probe
        # Exercise the real first-install shipped-image probe/activation
        # on this disposable OS, without logging into any model account.
        phase("release_hermes_probe_and_activation")
        latest_result = subprocess.run([*ssh, "agentic-home bootstrap_hermes"], capture_output=True,
                                       text=True, timeout=1500)
        bootstrap_hermes = json.loads(command(*ssh, "agentic-home status"))["bootstrap"]["hermes"]
        (ROOT / "evidence" / ("installed-hermes-" + profile + ".json")).write_text(json.dumps({
            "runtime_location": "disposable_outer_linux_vm", "iso_sha256": iso_hash,
            "cli_exit": latest_result.returncode, "job": bootstrap_job_diagnostics(latest_result.stdout),
            "observed": bootstrap_hermes,
            "cloud_login": "not_run"}, indent=2) + "\n")
        assert latest_result.returncode == 0 and bootstrap_hermes["state"] == "ready", bootstrap_hermes
        assert bootstrap_hermes["source"] == "release"
        phase("release_hermes_runtime_revision_readback")
        def hermes_image():
            identifier = command(*ssh, compose + " images -q hermes")
            assert re.fullmatch(r"(?:sha256:)?[a-f0-9]{12,64}", identifier)
            return command(*ssh, "sudo docker image inspect --format '{{.Id}}' " + identifier)
        candidate_id = hermes_image()
        runtime_revision = json.loads(command(*ssh, "python3 -", input='''
import http.cookiejar,json,urllib.request
from pathlib import Path
base='http://localhost:8099'
o=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
r=urllib.request.Request(base+'/api/host/login',data=json.dumps({'key':Path('/var/lib/agentic-home/controller/access-key').read_text().strip()}).encode(),headers={'Origin':base,'Content-Type':'application/json'})
o.open(r).close()
print(json.dumps(json.load(o.open(base+'/api/hermes'))['runtime']['upstream_commit']))
'''))
        assert runtime_revision == bootstrap_hermes["commit"]
        phase("nested_haos_provision")
        command(*ssh, "agentic-home provision")
        host = json.loads(command(*ssh, "agentic-home status"))
        assert host["vm"] == "running" and host["provisioned"]
        def wait_ha(stage):
            phase(stage)
            for _ in range(180):
                response = subprocess.run([*ssh, "curl --max-time 3 -fsS http://localhost:8123/api/onboarding"],
                                          capture_output=True)
                try:
                    if response.returncode == 0 and isinstance(json.loads(response.stdout), list):
                        return
                except ValueError:
                    pass
                time.sleep(5)
            raise AssertionError("Nested HAOS HTTP did not become ready")

        wait_ha("nested_haos_first_boot")
        phase("installed_controller_update_rollback")
        update_probe = installed_update(ssh, (ROOT / "tests/installed_update_probe.py").read_text(),
                                        ROOT / "evidence" / ("installed-update-" + profile + ".json"))
        assert update_probe["state"] == "passed", update_probe
        phase("nested_haos_shutdown_snapshot_restore")
        command(*ssh, "agentic-home shutdown")
        assert json.loads(command(*ssh, "agentic-home status"))["vm"] == "shut off"
        assert command(*ssh, "curl -fsS http://localhost:8099/api/deployment")
        command(*ssh, "agentic-home snapshot")
        snapshot = json.loads(command(*ssh, "agentic-home status"))["snapshots"][0]["id"]
        command(*ssh, "agentic-home restore --snapshot " + snapshot)
        command(*ssh, "agentic-home start")
        wait_ha("nested_haos_restored_boot")
        phase("outer_reboot_and_haos_autostart")
        subprocess.run([*ssh, "sudo systemctl reboot"], capture_output=True)
        time.sleep(15)
        for _ in range(120):
            result = subprocess.run([*ssh, "systemctl is-active agentic-home"], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(5)
        else:
            raise AssertionError("Outer product did not restart")
        assert json.loads(command(*ssh, "agentic-home status"))["vm"] == "running"
        assert hermes_image() == candidate_id
        wait_ha("nested_haos_after_outer_reboot")
        evidence = {"kind": "real_linux_install_nested_kvm", "profile": profile, "base": "Ubuntu 24.04.5",
                    "iso_sha256": iso_hash,
                    "installed_version": installed["version"], "images": image_lock,
                    "source_commit": source_lock["commit"],
                    "host_coding": coding_probe["status"], "controller_update_rollback": update_probe["state"],
                    "tailscale_installed_without_account": "1.104.1",
                    "runtime_location": "disposable_outer_linux_vm", "model_backend": "coding_transport_fixture",
                    "ha_backend": "nested_official_haos", "device_kind": "none",
                    "installation": "passed", "firstboot": "passed", "haos_vm_running": True,
                    "outer_ui_with_haos_off": True, "cold_snapshot_restore": "passed",
                    "firstboot_without_haos": True, "hermes_service_without_account": "needs_auth",
                    "console_rescue_with_controller_stopped": "passed",
                    "desktop_single_use_login": "passed", "bootstrap_get_has_no_side_effect": "passed",
                    "release_hermes": {**bootstrap_hermes, "image_id": candidate_id,
                                      "runtime_revision_readback": "passed", "persists_after_reboot": True},
                    "outer_reboot_haos_autostart": "passed",
                    "ha_readiness_check": "real Core onboarding API JSON list after startup, restore and outer reboot",
                    "duration_seconds": round(time.time() - started),
                    "installer_inputs": "Disposable disk serial + ephemeral SSH key supplied by fixture",
                    "physical_laptop": "not_run", "cloud_login": "not_run"}
        report_name = "linux-install-server.json" if profile == "server-amd64" else "linux-install.json"
        (ROOT / "evidence" / report_name).write_text(json.dumps(evidence, indent=2) + "\n")
        progress["result"] = "passed"
        phase("completed")
    except Exception as exc:
        progress.update(result="failed", error_type=type(exc).__name__)
        try:
            phase(progress["phase"])
        except Exception:
            pass
        try:
            record_install_failure(exc, progress["phase"],
                                   ROOT / "evidence" / ("installed-failure-" + profile + ".json"), locals().get("ssh"))
        except Exception:
            # Diagnostic failure cannot replace the original exception or bypass cleanup.
            progress["failure_diagnostics"] = "unavailable"
        raise
    finally:
        subprocess.run(["docker", "stop", "-t", "10", name], capture_output=True)
        # Keep only the nonsecret failure console when useful; all keys and virtual disks are removed.
        if (tmp_path / "serial.log").exists():
            shutil.copyfile(tmp_path / "serial.log", ROOT / "build/linux-install-console.log")
        subprocess.run(["docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/lab"], capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
