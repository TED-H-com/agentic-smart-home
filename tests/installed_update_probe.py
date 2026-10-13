"""Run as root ONLY in the disposable installed-product VM, after other probes.

Registry delivery and publisher identity are explicit synthetic fixtures. The
installed broker's signature verification, bounded compatibility probe, compose
activation, authenticated smoke, SQLite snapshots and rollback execute for real.
"""
import base64
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

STAGE = "disposable_vm_guard"
FAILED_STAGE = None
PRIMARY_FAILURE = None
DIAGNOSTICS = {"cleanup_started": False, "cleanup_completed": False}
SAFE_ERRORS = {
    "disposable_installed_vm_required", "installed_update_failed", "installed_update_deadline",
    "candidate_environment_invalid", "candidate_image_invalid", "update_manifest_invalid",
    "update_compatibility_invalid", "update_test_evidence_required", "update_test_evidence_invalid",
    "update_manifest_expired", "update_schema_mismatch", "update_database_integrity",
    "update_worker_lease_active", "update_active_or_uncertain_work", "update_gate_revision_changed",
    "update_publisher_store_untrusted", "update_publisher_store_invalid", "update_publisher_not_trusted",
    "update_repository_not_trusted", "update_publisher_key_invalid", "update_signature_invalid",
    "update_runtime_mismatch", "update_request_invalid", "update_busy_or_unresolved",
    "update_image_digest_mismatch", "update_image_runtime_invalid", "update_stage_failed",
    "update_controller_unavailable", "update_current_image_invalid", "update_controller_stop_unconfirmed",
    "update_health_invalid", "update_health_failed", "update_probe_identity_invalid",
    "update_rollback_unavailable", "update_approval_invalid", "update_host_operation_conflict",
    "update_base_revision_changed", "update_rollback_baseline_unavailable", "update_ha_version_incompatible",
    "update_rollback_revision_changed", "update_smoke_mismatch", "update_activation_mismatch",
    "update_final_smoke_mismatch", "update_staging_interrupted", "update_interrupted",
    "update_deployment_failed", "update_fallback_unconfirmed",
    "update_source_bundle_invalid", "update_source_diverged", "update_source_move_failed", "update_source_unavailable",
    "update_source_extract_failed", "update_source_command_failed",
    "host_memory_reserve_required", "host_resource_limits_unavailable", "container_identity_invalid",
    "container_stop_unconfirmed", "container_ownership_unconfirmed", "root_broker_required",
    "disk_backing_required", "host_reserve_insufficient", "workspace_mount_unverified",
    "command_timeout", "command_failed", "sandbox_rejected",
}
UPDATE_PHASES = {"stopping", "probing", "activating", "complete"}

STATE_ROOT = Path("/var/lib/agentic-home/installed-update-probe")
UNIT = "agentic-installed-update-probe.service"
STAGES = {
    "disposable_vm_guard", "installed_module_imports", "installed_update_baseline",
    "stop_host_broker_for_isolated_fixture", "build_fixed_label_candidate",
    "installed_bridge_contract_probe",
    "ephemeral_publisher_and_signature", "verify_signature_and_stage_candidate",
    "approved_candidate_activation", "durable_post_deployment_record", "approved_same_database_rollback",
    "rollback_readback_and_row_preservation", "restore_original_image_and_cleanup",
}
ERROR_TYPES = {"RuntimeError", "AssertionError", "HostError", "UpdateError", "OSError", "FileNotFoundError",
               "PermissionError", "ImportError", "ModuleNotFoundError", "TimeoutExpired", "CalledProcessError",
               "ValueError", "KeyError", "TypeError", "JSONDecodeError", "OperationalError", "SandboxError"}
LIFECYCLE_ACTIONS = {"provision", "start", "shutdown", "snapshot", "restore", "bootstrap_hermes"}
HOST_CONTRACT_CHECKS = {"linux_runtime", "libvirt_connection", "libvirt_kvm", "route_json", "default_route",
                        "sparse_copy", "qemu_image", "docker_limits", "compose_api"}
HOST_CONTRACT_ERRORS = {"command_missing", "permission_denied", "probe_timeout", "command_failed",
                        "response_invalid", "required_capability_missing"}
HOST_COMMANDS = {"cp", "virsh", "qemu-img", "chmod", "chown", "iptables", "ip", "docker"}
HOST_VM_STATES = {"running", "blocked", "paused", "in shutdown", "shut off", "crashed", "pmsuspended"}
HOST_SETUP_ERRORS = {
    "upstream_redirect_refused", "upstream_origin_refused", "upstream_metadata_invalid", "upstream_revision_invalid",
    "upstream_archive_limit", "upstream_archive_path", "upstream_archive_special_file", "existing_hermes_account",
    "existing_hermes_state", "existing_hermes_sessions", "candidate_image_invalid", "candidate_environment_invalid",
    "hermes_install_needs_reconcile", "hermes_install_disk_space", "bundled_image_invalid", "hermes_adapter_contract_unavailable",
    "hermes_install_failed", "hermes_fallback_unconfirmed", "hermes_install_interrupted",
}
LIFECYCLE_ERRORS = {
    "Profile fields do not match schema 1": "host_profile_schema_invalid",
    "Unknown Linux base": "host_linux_base_unknown", "Invalid profile id": "host_profile_id_invalid",
    "Invalid profile label": "host_profile_label_invalid", "Invalid profile flags": "host_profile_flags_invalid",
    "Reserve at least 2 GiB for the outer Linux": "host_memory_reserve_required",
    "This release provisions the dedicated NAT network only": "host_profile_network_unsupported",
    **{"Invalid profile " + key: "host_profile_" + key + "_invalid" for key in ("memory_mib", "vcpus", "disk_gib")},
    **{"host_contract_" + key + "_unavailable": "host_contract_" + key + "_unavailable" for key in HOST_CONTRACT_CHECKS},
    "Another host operation is running": "host_operation_busy",
    "HAOS has not been provisioned by this product": "haos_not_provisioned",
    "Existing VM is not owned by Agentic Home": "haos_ownership_mismatch",
    "VM disk was changed outside Agentic Home": "haos_disk_changed",
    "KVM unavailable; enable Intel VT-x / AMD-V in firmware": "host_kvm_unavailable",
    "Existing VM or image directory has no ownership record; refusing to replace": "haos_existing_resource_unowned",
    "192.168.177.0/24 overlaps an existing route": "host_network_route_overlap",
    "Installed profile differs from VM ownership record; no implicit replacement": "haos_profile_changed",
    "Finish the recorded restore before starting the VM": "haos_restore_pending",
    "Existing network is not owned by Agentic Home": "host_network_unowned",
    "Interrupted restore: re-run restore with the recorded snapshot id": "haos_restore_interrupted",
    "VM definition is missing; run provision": "haos_definition_missing",
    "VM definition is missing": "haos_definition_missing",
    "HAOS has not shut down; no forced power-off was performed": "haos_shutdown_unconfirmed",
    "Shut down HAOS before restoring": "restore_requires_shutdown",
    "Invalid snapshot id": "snapshot_id_invalid",
    "Snapshot ownership mismatch": "snapshot_ownership_mismatch",
    "Snapshot checksum mismatch": "snapshot_checksum_mismatch",
    "A different restore must be completed first": "restore_conflict",
    "Unknown lifecycle operation": "host_lifecycle_unknown", "Unknown lifecycle action": "host_lifecycle_unknown",
    "Unknown job fields": "host_job_fields_invalid", "Invalid operation id": "host_operation_id_invalid",
    "Unexpected snapshot": "host_snapshot_unexpected",
    "Idempotency key reused with different operation": "host_operation_id_conflict",
    "Invalid locked artifact": "host_artifact_lock_invalid", "Refusing symlink in artifact cache": "host_artifact_symlink_refused",
    "Artifact redirected away from HTTPS": "host_artifact_https_required",
    "host_service_unavailable": "host_service_unavailable",
    "broker_role_forbidden": "broker_role_forbidden",
}
HOST_MECHANISM_ERRORS = {"host_command_failed", "host_artifact_cached_checksum_mismatch", "host_artifact_download_checksum_mismatch",
                         "host_vm_state_unexpected", "host_file_missing", "host_permission_denied", "host_disk_full",
                         "host_readonly_filesystem", "host_io_error"}


def lifecycle_error(message):
    """Classify bounded mechanisms, never return any part of exception prose."""
    if not isinstance(message, str) or len(message) > 4096:
        return None
    if message in LIFECYCLE_ERRORS:
        return LIFECYCLE_ERRORS[message]
    if message in HOST_SETUP_ERRORS | SAFE_ERRORS:
        return message
    if any(message.startswith(name + ":") for name in HOST_COMMANDS):
        return "host_command_failed"
    if message.startswith("Cached checksum mismatch: ") and message.endswith("; remove corrupt file explicitly"):
        return "host_artifact_cached_checksum_mismatch"
    if message.startswith("Downloaded artifact checksum mismatch: "):
        return "host_artifact_download_checksum_mismatch"
    if message.startswith("VM is "):
        return "host_vm_state_unexpected"
    for number, code in ((2, "host_file_missing"), (13, "host_permission_denied"), (28, "host_disk_full"),
                         (30, "host_readonly_filesystem"), (5, "host_io_error")):
        if message.startswith(f"[Errno {number}] "):
            return code
    return None


def compatibility_receipt(value, action=None):
    """Project the last stored check; never rerun compatibility after a failure."""
    action = action if isinstance(action, str) and action in LIFECYCLE_ACTIONS else None
    result = {"observation": "missing_or_unreadable" if value is None else "invalid", "state": "unknown",
              "scope": "required_fixed_host_contracts", "reuse_for_mutations": False, "action": None,
              "requested_action": action, "matches_lifecycle_action": None, "observed_at": None, "checks": []}
    if not isinstance(value, dict):
        return result
    if action is None and isinstance(value.get("requested_action"), str) and value["requested_action"] in LIFECYCLE_ACTIONS:
        result["requested_action"] = action = value["requested_action"]
    if isinstance(value.get("observation"), str) and value["observation"] in {"missing_or_unreadable", "invalid"} and not value.get("checks"):
        result["observation"] = value["observation"]
        return result
    checks = value.get("checks")
    if value.get("scope") != result["scope"] or not isinstance(checks, list) or len(checks) > 16:
        return result
    result["action"] = value.get("action") if isinstance(value.get("action"), str) and value["action"] in LIFECYCLE_ACTIONS else None
    result["matches_lifecycle_action"] = result["action"] == action if action is not None else None
    timestamp = value.get("observed_at")
    if type(timestamp) in {int, float} and 0 <= timestamp <= 4102444800 and math.isfinite(timestamp):
        result["observed_at"] = timestamp
    known, invalid = set(), False
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or check["id"] not in HOST_CONTRACT_CHECKS or check["id"] in known:
            invalid = True
            continue
        known.add(check["id"])
        state, code = check.get("state"), check.get("error_code")
        valid = (state == "supported" and code is None) or (state == "blocked" and isinstance(code, str) and code in HOST_CONTRACT_ERRORS)
        invalid |= not valid
        affected = check.get("affected_actions", [])
        result["checks"].append({"id": check["id"], "state": state if valid else "unknown",
            "error_code": code if valid else "unknown", "observed_from": "outer_host_fixed_probe",
            "affected_actions": sorted({item for item in affected if isinstance(item, str) and item in LIFECYCLE_ACTIONS})
                if isinstance(affected, list) and len(affected) <= 6 else []})
    state = value.get("state")
    if isinstance(state, str) and state in {"supported", "blocked"} and result["checks"] and not invalid and result["action"] and result["observed_at"] is not None:
        result["state"] = state
        result["observation"] = "ok"
    return result


def validate_guard(uid, marker, hostname, serials, virtualization, installed):
    if not (uid == 0 and marker == "1" and hostname == "agentic-disposable" and
            "AGENTIC_TEST_ONLY" in serials and virtualization in {"kvm", "qemu"} and installed):
        raise RuntimeError("disposable_installed_vm_required")


def guard():
    # Reject before imports, state-directory creation, or any service command.
    if os.geteuid() != 0 or os.environ.get("AGENTIC_DISPOSABLE_VM") != "1":
        raise RuntimeError("disposable_installed_vm_required")
    serials = [*Path("/sys/class/block").glob("*/serial"), *Path("/sys/class/block").glob("*/device/serial")]
    validate_guard(os.geteuid(), os.environ.get("AGENTIC_DISPOSABLE_VM"),
                   Path("/etc/hostname").read_text().strip(), [p.read_text().strip() for p in serials],
                   subprocess.check_output(["systemd-detect-virt", "--vm"], text=True, timeout=5).strip(),
                   Path("/opt/agentic-home/host/compose.yaml").is_file())


def persist(name, value):
    temporary = STATE_ROOT / (name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, STATE_ROOT / name)


def checkpoint(stage):
    global STAGE
    assert stage in STAGES
    STAGE = stage
    persist("checkpoint.json", {"state": "running", "stage": stage})


def safe_error(exc, stage):
    code = getattr(exc, "code", None) or str(exc)
    return {"error_code": code if isinstance(code, str) and code in SAFE_ERRORS else "unclassified_update_error",
            "error_type": type(exc).__name__ if type(exc).__name__ in ERROR_TYPES else "OtherError", "stage": stage}


def probe_failure(exc):
    """Retain only typed mechanism before Updater converts it to a generic code."""
    result = safe_error(exc, "approved_candidate_activation")
    raw = getattr(exc, "diagnostics", {})
    if isinstance(raw, dict):
        if raw.get("operation") in {"umount", "mount", "mkfs.ext4", "findmnt", "docker.run", "docker.inspect", "docker.info", "docker.rm", "docker.kill"}:
            result["operation"] = raw["operation"]
        if type(raw.get("command_exit_code")) is int and -128 <= raw["command_exit_code"] <= 255:
            result["command_exit_code"] = raw["command_exit_code"]
    result["signatures"] = process_diagnostics(subprocess.CompletedProcess([], None, "", str(exc)))["stderr_signatures"]
    return result


def observed_probe(probe, root, *args):
    capacity = shutil.disk_usage(root)
    DIAGNOSTICS["probe_resources"] = {"free_disk_bytes": capacity.free}
    entries = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    DIAGNOSTICS["probe_resources"]["available_memory_bytes"] = int(entries["MemAvailable"].split()[0]) * 1024
    try:
        return probe(*args)
    except Exception as exc:
        DIAGNOSTICS["probe_failure"] = probe_failure(exc)
        raise


def observed_run(run, *args, **kwargs):
    from agentic_host.manager import HostError

    try:
        result = run(*args, **{**kwargs, "check": False})
    except subprocess.TimeoutExpired as exc:
        DIAGNOSTICS.setdefault("command_failure", process_diagnostics(subprocess.CompletedProcess([], None, exc.stdout, exc.stderr)))
        raise
    if kwargs.get("check", True) and result.returncode:
        DIAGNOSTICS.setdefault("command_failure", process_diagnostics(result))
        # Preserve manager.run's existing checked-command semantics exactly.
        raise HostError(f"{args[0]}: {result.stderr.strip()[:500]}")
    return result


def begin_cleanup():
    DIAGNOSTICS["cleanup_started"] = True
    try:
        checkpoint("restore_original_image_and_cleanup")
    except Exception:
        # ENOSPC or diagnostic I/O must never prevent real cleanup.
        DIAGNOSTICS["cleanup_checkpoint_failed"] = True


def process_diagnostics(result):
    """No subprocess text leaves the disposable VM/test process."""
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    sample = stderr[:16384].lower()
    return {"returncode": result.returncode if type(result.returncode) is int and -255 <= result.returncode <= 255 else None,
            "stdout_bytes": len(stdout.encode()), "stderr_bytes": len(stderr.encode()),
            "stderr_truncated": len(stderr) > 16384,
            "stderr_signatures": {key: any(marker in sample for marker in markers) for key, markers in {
                "traceback": ("traceback (most recent call last)",),
                "import_error": ("modulenotfounderror", "importerror"),
                "syntax_error": ("syntaxerror",), "permission_denied": ("permission denied",),
                "ssh_transport": ("connection reset", "connection closed", "connection refused", "broken pipe", "connection timed out"),
                "process_killed": ("killed", "terminated"), "sudo_error": ("sudo:",),
                "timeout": ("timed out", "timeoutexpired", "timeout error"),
                "disk_full": ("no space left on device",), "read_only_filesystem": ("read-only file system",),
                "io_error": ("input/output error",), "oom": ("out of memory", "oom-kill", "killed process"),
                "file_missing": ("no such file or directory", "cannot find the file"),
                "kvm_unavailable": ("failed to initialize kvm", "kvm is not supported", "kvm unavailable", "could not access kvm"),
                "libvirt_connection": ("failed to connect to the hypervisor", "failed to connect socket"),
                "firmware_unavailable": ("unable to find any master var store", "could not find any firmware", "unable to find 'ovmf"),
                "network_conflict": ("overlaps an existing route", "network is already in use", "address already in use"),
                "checksum_mismatch": ("checksum mismatch",), "image_invalid": ("image is corrupt", "not in qcow2 format", "unsupported image format"),
                "security_policy_denied": ("apparmor=\"denied\"", "avc:  denied", "operation not permitted"),
                "resource_unavailable": ("cannot allocate memory", "resource temporarily unavailable", "too many open files"),
            }.items()}}


def bridge_contract_probe(manager, original, base_tag, fixture, identity, database, schema):
    """Exercise installed enforcement; fixture authority never enters the Live DB.

    This is a current-image derivative with one removed declaration, NOT an old
    released Controller. Both candidates execute the installed fixed script on
    real bounded ext4 volumes. Docker, volume and probe calls are not replaced.
    """
    from agentic_host.manager import HostError
    from agentic_host.updater import UpdateError, Updater, offline_check

    checkpoint("installed_bridge_contract_probe")
    run = manager.run
    root = fixture / "bridge-contract"
    root.mkdir(mode=0o700)
    context, host, data = root / "context", root / "host", root / "controller"
    for directory in (context, host, data):
        directory.mkdir(mode=0o700)
    scratch = data / "controller.sqlite3"
    revoked = json.dumps({"state": "revoked", "pairing_id": "synthetic-revoked-generation"})
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(scratch)) as target:
        with target:
            for row in source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name IN ('settings','records','requests','events')"):
                target.execute(row[0])
            target.execute(f"PRAGMA user_version={int(schema)}")
            target.execute("INSERT INTO settings VALUES ('ingress_bridge',?)", (revoked,))
    scratch.chmod(0o600)
    service = Updater(SimpleNamespace(root=host, product=manager.product, run=run))
    assert service.database == scratch and service.database != database
    missing_tag = "agentic-update-no-bridge-contract:" + identity
    missing_image = None
    observations = []
    expected_marker = [True]
    receipt = None

    def actual_command(*args, **kwargs):
        if args[:2] != ("docker", "run"):
            return run(*args, **kwargs)
        # Observe only the fixed probe's scrubbed input and exact exit cause.
        name = args[args.index("--name") + 1]
        assert re.fullmatch(r"agentic-update-probe-[a-f0-9]{32}", name)
        mounts = [args[i + 1] for i, item in enumerate(args) if item == "--mount"]
        assert f"type=bind,src={manager.product / 'host/agentic_host/update_probe.py'},dst=/fixed-probe.py,readonly" in mounts
        mount = next(value for value in mounts if value.endswith(",dst=/probe"))
        directory = Path(mount.removeprefix("type=bind,src=").removesuffix(",dst=/probe"))
        assert directory.parent.parent == service.root / "probes"
        with closing(sqlite3.connect((directory / "controller.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            settings = dict(db.execute("SELECT key,value FROM settings"))
            assert settings == {"paused": "true", "retention": '{"context_days":7}',
                                **({"required_ingress_bridge_contract": "1"} if expected_marker[0] else {})}
            assert all(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in ("records", "requests", "events"))
        result = run(*args, **{**kwargs, "check": False})
        observations.append({"name": name, "directory": directory, "returncode": result.returncode,
                             "missing_contract": "ImportError: cannot import name 'BRIDGE_CONTRACT' from 'agentic_controller.bridge'" in result.stderr})
        if result.returncode:
            # A fixed safe exception preserves real checked-command behavior.
            # Never export raw traceback, arguments, paths or candidate text.
            raise HostError("installed_bridge_fixed_probe_failed")
        return result

    def prove_probe_cleanup():
        assert not service.probe_path.exists()
        probes = service.root / "probes"
        assert not probes.exists() or not list(probes.iterdir())
        for observation in observations:
            assert not run("docker", "container", "ls", "-a", "--filter", "name=^/" + observation["name"] + "$",
                           "--format", "{{.Names}}", timeout=15).stdout.strip()
            assert not observation["directory"].parent.exists() and not os.path.ismount(observation["directory"])
            assert not run("losetup", "-j", str(observation["directory"].parent / "workspace.ext4"), timeout=15).stdout.strip()

    service.run = actual_command
    try:
        # Build only a one-line incompatibility fixture from the installed image.
        patch = ("from pathlib import Path; p=Path('/app/agentic_controller/bridge.py'); s=p.read_text(); "
                 "old='BRIDGE_CONTRACT = 1\\n'; assert s.count(old)==1; p.write_text(s.replace(old,''))")
        (context / "Dockerfile").write_text(f"FROM {base_tag}\nRUN " + json.dumps(["python", "-c", patch]) + "\n")
        run("docker", "build", "--network", "none", "--pull=false", "-t", missing_tag, str(context), timeout=180)
        missing_image = run("docker", "image", "inspect", "--format", "{{.Id}}", missing_tag).stdout.strip()
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", missing_image) and missing_image != original
        before = hashlib.sha256(scratch.read_bytes()).hexdigest()
        service.probe(original, schema)
        prove_probe_cleanup()
        try:
            service.probe(missing_image, schema)
        except HostError:
            assert len(observations) == 2 and observations[-1]["returncode"] == 1 and observations[-1]["missing_contract"]
        else:
            raise AssertionError("missing_bridge_contract_was_accepted")
        prove_probe_cleanup()
        assert hashlib.sha256(scratch.read_bytes()).hexdigest() == before
        # Control: without bridge history the same derivative remains compatible.
        with closing(sqlite3.connect(scratch)) as db, db:
            db.execute("DELETE FROM settings WHERE key='ingress_bridge'")
        expected_marker[0] = False
        service.probe(missing_image, schema)
        prove_probe_cleanup()
        with closing(sqlite3.connect(scratch)) as db, db:
            db.execute("INSERT INTO settings VALUES ('ingress_bridge',?)", (revoked,))
        offline_check(scratch, schema)
        with closing(sqlite3.connect(scratch)) as db, db:
            db.execute("INSERT INTO settings VALUES ('bridge_maintenance',?)",
                       (json.dumps({"operation": {"kind": "restart", "state": "pending", "job_id": "synthetic-supervisor-job"}}),))
        pending_before = hashlib.sha256(scratch.read_bytes()).hexdigest()
        try:
            offline_check(scratch, schema)
        except UpdateError as exc:
            assert str(exc) == "update_active_or_uncertain_work"
        else:
            raise AssertionError("pending_bridge_operation_was_accepted")
        assert hashlib.sha256(scratch.read_bytes()).hexdigest() == pending_before
        assert [item["returncode"] for item in observations] == [0, 1, 0]
        receipt = {
            "contract": 1, "observed_from": "installed_host_fixed_probe", "schema": schema,
            "database_fixture": "isolated_structure_only_revoked_bridge",
            "fixed_script_sha256": hashlib.sha256((manager.product / "host/agentic_host/update_probe.py").read_bytes()).hexdigest(),
            "supporting_image": original, "missing_contract_image": missing_image,
            "synthetic_missing_contract_image": True, "real_historical_release": False,
            "fixed_probe_executions": 3,
            "checks": {key: True for key in ("revoked_history_marker_copied", "supporting_fixed_probe_passed",
                "missing_contract_fixed_probe_rejected", "missing_contract_import_error", "missing_contract_without_history_passed",
                "revoked_database_unchanged", "pending_maintenance_rejected", "pending_maintenance_readonly")},
        }
    finally:
        # Recover only this helper's exact private journal. If fencing fails,
        # leave its disk intact; the surrounding VM teardown is the last fence.
        service.reconcile_probe()
        prove_probe_cleanup()
        run("docker", "image", "rm", missing_tag, timeout=30, check=False)
        if missing_image:
            run("docker", "image", "rm", missing_image, timeout=30, check=False)
        listed = run("docker", "image", "ls", "-aq", "--no-trunc", timeout=15).stdout.splitlines()
        assert missing_image is None or missing_image not in listed
        shutil.rmtree(root)
        assert not root.exists()
        if receipt is not None:
            receipt["cleanup"] = {key: True for key in ("probe_containers_absent", "quota_volumes_absent",
                "probe_journal_absent", "fixture_image_absent", "scratch_database_absent")}
    return receipt


def lifecycle_receipt(value):
    if not isinstance(value, dict):
        return None
    identifier = value.get("id")
    request = value.get("request", {})
    action = request.get("action") if isinstance(request, dict) else None
    if action is None and isinstance(value.get("action"), str) and value["action"] in LIFECYCLE_ACTIONS:
        action = value["action"]
    state = value.get("state")
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        return None
    result = {"id": identifier, "action": action if isinstance(action, str) and action in LIFECYCLE_ACTIONS else "unknown",
              "state": state if isinstance(state, str) and state in {"running", "succeeded", "failed", "needs_reconcile"} else "unknown"}
    message = value.get("message", "")
    if not isinstance(message, str):
        message = ""
    known = lifecycle_error(message)
    supplied = value.get("error_code")
    if not isinstance(supplied, str):
        supplied = None
    result["error_code"] = (known or supplied) if (known or supplied) in set(LIFECYCLE_ERRORS.values()) | SAFE_ERRORS | HOST_SETUP_ERRORS | HOST_MECHANISM_ERRORS else "unclassified_host_error"
    if result["state"] == "succeeded":
        result["error_code"] = None
    result["operation"] = next((name for name in sorted(HOST_COMMANDS) if message.startswith(name + ":")), None)
    if result["operation"] is None and isinstance(value.get("operation"), str) and value["operation"] in HOST_COMMANDS:
        result["operation"] = value["operation"]
    result["message_signatures"] = process_diagnostics(subprocess.CompletedProcess([], None, "", message))["stderr_signatures"]
    if isinstance(value.get("message_signatures"), dict):
        result["message_signatures"] = {key: value["message_signatures"].get(key) is True for key in result["message_signatures"]}
    return result


def product_file_metadata(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    except OSError:
        return {"kind": "unreadable"}
    kind = "regular" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "other"
    return {"kind": kind, **({"bytes": info.st_size} if kind == "regular" and 0 <= info.st_size <= 2**63 else {})}


def libvirt_receipt(status, text, kind, owner):
    result = {"observation": status, "xml_valid": False, "name_matches": None, "ownership_matches": None}
    try:
        if status != "ok" or not isinstance(text, str) or len(text.encode()) > 65536:
            return result
        value = ET.fromstring(text)
        if value.tag != kind:
            return result
        result["xml_valid"] = True
        result["name_matches"] = value.findtext("name") == ("agentic-home-haos" if kind == "domain" else "agentic-home")
        actual = value.findtext("metadata/{https://agentic-home.local/managed/v1}owner")
        result["ownership_matches"] = (actual == owner and (kind != "domain" or value.findtext("uuid") == owner)) if owner else None
        if kind == "domain":
            disks = value.findall("devices/disk/source")
            result["disk_matches"] = len(disks) == 1 and disks[0].get("file") == "/var/lib/libvirt/images/agentic-home/haos.qcow2"
    except (ValueError, ET.ParseError, RecursionError):
        pass
    return result


def failure_observation(identifier=None, action=None):
    """Read only reviewed files and fixed diagnostics before VM teardown."""
    guard()
    sys.path.insert(0, "/opt/agentic-home/host")
    from agentic_host.support import _capture, _read

    result = {"jobs": [], "selection": "exact_id" if identifier else "recent_action_unbound"}
    raw = _read(Path("/var/lib/agentic-home/host/jobs.json"), limit=65536)
    try:
        jobs = json.loads(raw) if raw else {}
        for value in list(jobs.values())[-20:]:
            receipt = lifecycle_receipt(value)
            if receipt and ((identifier and receipt["id"] == identifier) or (not identifier and receipt["action"] == action)):
                result["jobs"].append(receipt)
        result["jobs"] = result["jobs"][-5:]
    except (ValueError, AttributeError, RecursionError):
        result["job_read_failed"] = True
    try:
        raw = _read(Path("/var/lib/agentic-home/host/compatibility.json"), limit=16384)
        value = json.loads(raw) if raw is not None else None
    except (ValueError, RecursionError):
        value = False
    result["compatibility"] = compatibility_receipt(value, action)
    try:
        raw = _read(Path("/var/lib/agentic-home/host/state.json"), limit=16384)
        state = json.loads(raw) if raw is not None else None
    except (ValueError, RecursionError):
        state = None
    owner = state.get("owner") if isinstance(state, dict) else None
    owner = owner if isinstance(owner, str) and re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", owner) else None
    result["provision_state"] = {"record_readable": isinstance(state, dict), "owner_reference_valid": owner is not None,
        "complete": state.get("complete") if isinstance(state, dict) and type(state.get("complete")) is bool else None,
        "restore_pending": bool(state.get("restore_pending")) if isinstance(state, dict) else None,
        "files": {name: product_file_metadata(Path("/var/lib/libvirt/images/agentic-home") / filename) for name, filename in
                  (("disk", "haos.qcow2"), ("partial_disk", "haos.partial"), ("nvram", "nvram.fd"))}}
    capacity = shutil.disk_usage("/var/lib/agentic-home")
    result["filesystem"] = {"total_bytes": capacity.total, "used_bytes": capacity.used, "free_bytes": capacity.free}
    fixed = {
        "broker_unit": ["/usr/bin/systemctl", "show", "agentic-home-host.service", "--property=ActiveState,Result"],
        "vm": ["/usr/bin/virsh", "--readonly", "--connect", "qemu:///system", "domstate", "agentic-home-haos"],
        "journal": ["/usr/bin/journalctl", "--no-pager", "--lines=100", "--unit=agentic-home-host.service",
                    "--unit=agentic-home-firstboot.service", "--unit=docker.service", "--unit=libvirtd.service"],
        "kernel": ["/usr/bin/journalctl", "--no-pager", "--dmesg", "--lines=100"],
        "domain": ["/usr/bin/virsh", "--readonly", "--connect", "qemu:///system", "dumpxml", "agentic-home-haos", "--inactive"],
        "network": ["/usr/bin/virsh", "--readonly", "--connect", "qemu:///system", "net-dumpxml", "agentic-home"],
    }
    def capture(args):
        try:
            return _capture(args)
        except Exception:
            return "probe_failed", ""
    # Six fixed read-only captures, each bounded by support._capture (3s/64KiB),
    # run in two batches within the existing SSH observation deadline.
    with ThreadPoolExecutor(max_workers=3) as pool:
        observations = list(pool.map(capture, fixed.values()))
    for name, (status, text) in zip(fixed, observations, strict=True):
        if name in {"domain", "network"}:
            result[name] = libvirt_receipt(status, text, name, owner)
            continue
        result[name] = {"observation": status,
                        "signatures": process_diagnostics(subprocess.CompletedProcess([], None, "", text))["stderr_signatures"]}
        if name == "vm":
            result[name]["state"] = text.strip() if text.strip() in HOST_VM_STATES else "unknown"
        if name == "broker_unit":
            fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            result[name]["active"] = fields.get("ActiveState") if fields.get("ActiveState") in {"active", "inactive", "failed", "activating", "deactivating"} else "unknown"
            result[name]["oom_killed"] = fields.get("Result") == "oom-kill"
    return result


def launch(source):
    guard()
    os.umask(0o077)
    # Exclusive fixed directory is the launch receipt. Never replay effects.
    STATE_ROOT.mkdir(mode=0o700, exist_ok=False)
    checkpoint("disposable_vm_guard")
    script = STATE_ROOT / "probe.py"
    script.write_text(source)
    script.chmod(0o400)
    completed = subprocess.run([
        "systemd-run", "--unit=" + UNIT, "--property=RuntimeMaxSec=600", "--property=KillMode=control-group",
        "--property=RemainAfterExit=yes",
        "--property=UMask=0077", "--property=StandardOutput=append:" + str(STATE_ROOT / "stdout"),
        "--property=StandardError=append:" + str(STATE_ROOT / "stderr"),
        "--setenv=AGENTIC_DISPOSABLE_VM=1", "/usr/bin/python3", str(script),
    ], capture_output=True, text=True, timeout=15)
    return {"launch": process_diagnostics(completed)}


def observe():
    guard()
    result = None
    checkpoint_value = None
    for name in ("checkpoint.json", "result.json"):
        path = STATE_ROOT / name
        if path.is_file():
            if path.stat().st_size > 65536:
                raise RuntimeError("probe_record_oversized")
            value = json.loads(path.read_text())
            if name == "checkpoint.json":
                checkpoint_value = value
            else:
                result = value
    unit = subprocess.run(["systemctl", "show", UNIT, "--property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus"],
                          capture_output=True, text=True, timeout=10)
    fields = dict(line.split("=", 1) for line in unit.stdout.splitlines() if "=" in line)
    status = {key: fields.get(key) for key in ("ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus")}
    logs = []
    lengths = []
    for name in ("stdout", "stderr"):
        path = STATE_ROOT / name
        if path.exists():
            lengths.append(path.stat().st_size)
            with path.open("rb") as stream:
                logs.append(stream.read(16384).decode(errors="replace"))
        else:
            lengths.append(0)
            logs.append("")
    diagnostic = process_diagnostics(subprocess.CompletedProcess([], None, *logs))
    diagnostic.update(stdout_bytes=lengths[0], stderr_bytes=lengths[1], stderr_truncated=lengths[1] > 16384)
    return {"checkpoint": checkpoint_value, "result": result, "unit": status, "process": diagnostic}


def main():
    global STAGE, FAILED_STAGE, PRIMARY_FAILURE
    guard()
    checkpoint("installed_module_imports")
    sys.path.insert(0, "/opt/agentic-home/host")
    from agentic_host.bootstrap import write_environment
    from agentic_host.manager import Manager, atomic, run
    from agentic_host.updater import Updater, canonical

    checkpoint("installed_update_baseline")
    os.umask(0o077)
    manager = Manager()
    service = Updater(manager)
    identity = uuid.uuid4().hex
    fixture = manager.root / ("update-fixture-" + identity)
    fixture.mkdir(mode=0o700)
    environment_before = service.environment.read_bytes() if service.environment.exists() else None
    records_before = service.path.read_bytes()
    source_json_before = service.source_lock.read_bytes()
    source_head_before = run("git", "--git-dir", str(service.source_repo), "rev-parse", "HEAD").stdout.strip()
    original = service.image()
    baseline = service.health()
    with sqlite3.connect(service.database.as_uri() + "?mode=ro", uri=True) as db:
        schema = db.execute("PRAGMA user_version").fetchone()[0]
        paused_before = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
    assert schema == 3
    base_tag, candidate_tag = "agentic-update-base:" + identity, "agentic-update-candidate:" + identity
    candidate = None
    record = None
    result = None
    checkpoint("stop_host_broker_for_isolated_fixture")
    run("systemctl", "stop", "agentic-home-host.service", timeout=60)
    try:
        checkpoint("build_fixed_label_candidate")
        run("docker", "tag", original, base_tag)
        # One disposable commit on top of the installed source, so the deploy
        # really moves coding HEAD and the rollback really returns it.
        clone = fixture / "source-clone"
        run("git", "clone", "--bare", "--no-hardlinks", "--", str(service.source_repo), str(clone), timeout=180)
        tree = run("git", "--git-dir", str(clone), "rev-parse", "HEAD^{tree}").stdout.strip()
        source_commit = run("git", "-c", "user.name=Disposable fixture", "-c", "user.email=fixture@example.invalid",
                            "--git-dir", str(clone), "commit-tree", tree, "-p", source_head_before,
                            "-m", "Disposable update fixture").stdout.strip()
        run("git", "--git-dir", str(clone), "update-ref", "HEAD", source_commit)
        run("git", "--git-dir", str(clone), "bundle", "create", str(fixture / "agentic-home.bundle"), "HEAD", timeout=180)
        shutil.rmtree(clone)
        (fixture / "Dockerfile").write_text(f"FROM {base_tag}\nLABEL agentic.disposable.update={identity}\n"
                                            "COPY agentic-home.bundle /app/source/agentic-home.bundle\n")
        run("docker", "build", "--network", "none", "--pull=false", "-t", candidate_tag, str(fixture), timeout=180)
        candidate = run("docker", "image", "inspect", "--format", "{{.Id}}", candidate_tag).stdout.strip()
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", candidate) and candidate != original
        bridge_contract = bridge_contract_probe(manager, original, base_tag, fixture, identity, service.database, schema)
        checkpoint("ephemeral_publisher_and_signature")
        run("openssl", "genpkey", "-algorithm", "ED25519", "-out", str(fixture / "private.pem"))
        run("openssl", "pkey", "-in", str(fixture / "private.pem"), "-pubout", "-out", str(fixture / "public.pem"))
        trust = fixture / "publishers.json"
        atomic(trust, {"format": 1, "publishers": {"disposable-fixture": {
            "public_key_pem": (fixture / "public.pem").read_text(), "image_repository": "ghcr.io/disposable-fixture/controller"}}})
        # Fixture trust is private to this updater instance, never enrolled in
        # /etc and never shipped as a real production publisher.
        service.trust = trust
        import agentic_host.updater as module
        module.TRUST = fixture / "absent-system-trust.json"
        manifest = {"format": 1, "publisher": "disposable-fixture", "version": baseline["version"],
                    "source_commit": source_commit, "image": "ghcr.io/disposable-fixture/controller@" + candidate,
                    "image_id": candidate, "compatibility": {"schema": schema, "worker_contract": 1, "runner_protocol": 1,
                    "runtime_digest": service.runtime_digest(), "ha_core_versions": [baseline["ha_version"] or "2026.10.1"]},
                    "tests": [{"name": "disposable-fixture-only", "result": "passed", "sha256": hashlib.sha256(b"fixture-label-only").hexdigest()}],
                    "issued_at": time.time() - 1, "expires_at": time.time() + 1800}
        manifest_path = fixture / "manifest.json"
        manifest_path.write_bytes(canonical(manifest))
        run("openssl", "pkeyutl", "-sign", "-inkey", str(fixture / "private.pem"), "-rawin", "-in", str(manifest_path),
            "-out", str(fixture / "signature"))
        signature = base64.b64encode((fixture / "signature").read_bytes()).decode()
        deliveries = []
        def execute(*args, **kwargs):
            # The ONLY delivery substitution: a fixed, already built candidate.
            # All other commands use the actual installed Linux/Docker services.
            if args == ("docker", "pull", manifest["image"]):
                deliveries.append("preloaded_fixture")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args == ("docker", "image", "inspect", "--format", "{{.Id}}", manifest["image"]):
                return run("docker", "image", "inspect", "--format", "{{.Id}}", candidate, **kwargs)
            if args == ("docker", "image", "inspect", manifest["image"]):
                return run("docker", "image", "inspect", candidate, **kwargs)
            return observed_run(run, *args, **kwargs)
        service.run = execute
        original_probe = service.probe
        service.probe = lambda *args: observed_probe(original_probe, manager.root, *args)
        checkpoint("verify_signature_and_stage_candidate")
        status, record = service.handle("POST", "/updates/stage", {"manifest": manifest, "signature": signature})
        assert status == 202
        record = service.records[record["candidate_digest"]]
        def wait(states, timeout=600):
            deadline = time.monotonic() + timeout
            while record["state"] not in states:
                if record["state"] in {"failed", "needs_reconcile", "rolled_back"}:
                    DIAGNOSTICS["updater_state"] = record["state"]
                    DIAGNOSTICS["updater_phase"] = record.get("phase") if record.get("phase") in UPDATE_PHASES else "unknown"
                    error = record.get("error_code")
                    DIAGNOSTICS["updater_error_code"] = error if error in SAFE_ERRORS else "unclassified_update_error"
                    raise RuntimeError("installed_update_failed")
                if time.monotonic() > deadline:
                    raise RuntimeError("installed_update_deadline")
                time.sleep(.25)
        wait({"staged"})
        digest = record["candidate_digest"]
        checkpoint("approved_candidate_activation")
        service.handle("POST", "/updates/apply", {"candidate_digest": digest, "approval_id": record["approval_id"]})
        wait({"deployed"})
        assert service.image() == candidate
        assert record["source_sync"] == "moved" and service.status()["deployed_source_commit"] == source_commit
        assert run("git", "--git-dir", str(service.source_repo), "rev-parse", "HEAD").stdout.strip() == source_commit
        moved = json.loads(service.source_lock.read_text())
        assert moved["commit"] == source_commit and moved["previous_commit"] == source_head_before
        # This is a synthetic durable post-deployment record in the real Live
        # database, written while its owner is stopped. It represents the data
        # that would be lost by restoring the pre-deployment snapshot.
        checkpoint("durable_post_deployment_record")
        service.stop()
        marker = "post-deployment-" + identity
        with sqlite3.connect(service.database) as db:
            now = "2026-10-12T00:00:00+00:00"
            db.execute("INSERT INTO records VALUES (?,?,?,?,?)", ("update_vm_fixture", marker, json.dumps({"id": marker, "fixture": True}), now, now))
        service.activate(candidate)
        service.health()
        checkpoint("approved_same_database_rollback")
        offered = service.handle("POST", "/updates/rollback-offer", {"candidate_digest": digest})[1]
        service.handle("POST", "/updates/rollback", {"candidate_digest": digest, "approval_id": offered["rollback_approval_id"]})
        wait({"rolled_back"})
        checkpoint("rollback_readback_and_row_preservation")
        assert service.image() == original
        assert record["source_sync"] == "reverted"
        assert run("git", "--git-dir", str(service.source_repo), "rev-parse", "HEAD").stdout.strip() == source_head_before
        assert json.loads(service.source_lock.read_text()) == json.loads(source_json_before)
        with sqlite3.connect(service.database.as_uri() + "?mode=ro", uri=True) as db:
            assert db.execute("SELECT body FROM records WHERE kind='update_vm_fixture' AND id=?", (marker,)).fetchone()
            assert db.execute("PRAGMA user_version").fetchone()[0] == schema
            assert db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"
            assert db.execute("SELECT value FROM settings WHERE key='updater_gate'").fetchone() is None
        result = {"state": "passed", "observed_from": "disposable_installed_linux_vm_root",
                  "actual": ["ed25519_signature_verification", "bounded_candidate_probe", "compose_activation", "authenticated_owner_smoke",
                             "consistent_snapshot", "schema3", "maintenance_gate", "same_database_rollback", "post_deployment_row_preserved",
                             "revoked_bridge_fixed_probe_enforced", "pending_bridge_operation_blocked", "isolated_probe_cleanup_verified",
                             "coding_source_moved_with_image_and_rolled_back"],
                  "fixtures": ["ephemeral_publisher", "preloaded_registry_delivery", "candidate_changes_label_and_source_bundle", "synthetic_post_deployment_record",
                               "synthetic_missing_bridge_contract_image", "isolated_structure_only_revoked_bridge"],
                  "real_cloud": False, "real_publisher_release": False, "original_image": original, "candidate_image": candidate,
                  "registry_delivery_calls": len(deliveries), "final_schema": schema, "rollback_restored_backup": False,
                  "bridge_update_contract": bridge_contract}
    except Exception as exc:
        FAILED_STAGE = STAGE
        PRIMARY_FAILURE = safe_error(exc, STAGE)
        raise
    finally:
        # Exact original artifact and owner pause setting are restored; never
        # restore a stale DB. The surrounding disposable VM is deleted by pytest.
        begin_cleanup()
        service.stop()
        with sqlite3.connect(service.database) as db:
            db.execute("DELETE FROM records WHERE kind='update_vm_fixture'")
            db.execute("DELETE FROM settings WHERE key='updater_gate'")
            if paused_before:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('paused',?)", paused_before)
            else:
                db.execute("DELETE FROM settings WHERE key='paused'")
        write_environment(service.environment, original, "AGENTIC_CONTROLLER_IMAGE")
        service.compose("up", "-d", "--no-deps", "controller", timeout=120)
        service.health()
        if environment_before is None:
            service.environment.unlink(missing_ok=True)
        else:
            service.environment.write_bytes(environment_before)
            service.environment.chmod(0o600)
        service.path.write_bytes(records_before)
        service.path.chmod(0o600)
        # The coding source returns to its exact installed state as well.
        if run("git", "--git-dir", str(service.source_repo), "rev-parse", "HEAD", check=False).stdout.strip() != source_head_before:
            run("git", "--git-dir", str(service.source_repo), "update-ref", "HEAD", source_head_before)
        if service.source_lock.read_bytes() != source_json_before:
            service.source_lock.write_bytes(source_json_before)
            service.source_lock.chmod(0o600)
        if candidate:
            run("git", "--git-dir", str(service.source_repo), "update-ref", "-d",
                "refs/agentic/candidate/" + candidate.removeprefix("sha256:"), check=False)
        run("docker", "image", "rm", candidate_tag, base_tag, timeout=30, check=False)
        if candidate:
            run("docker", "image", "rm", candidate, timeout=30, check=False)
        # Never recursively remove a quota disk whose writer/unmount could not
        # be fenced. The test runner will tear down the entire disposable VM.
        assert not any(Path(line.split()[4]).is_relative_to(fixture) for line in Path("/proc/self/mountinfo").read_text().splitlines())
        shutil.rmtree(fixture)
        run("systemctl", "start", "agentic-home-host.service", timeout=60)
        DIAGNOSTICS["cleanup_completed"] = True
    result["diagnostics"] = DIAGNOSTICS
    return result


def execute():
    guarded = False
    try:
        guard()
        guarded = True
        result = main()
    except Exception as exc:
        if PRIMARY_FAILURE and DIAGNOSTICS["cleanup_started"] and not DIAGNOSTICS["cleanup_completed"]:
            cleanup = safe_error(exc, STAGE)
            DIAGNOSTICS.update(cleanup_error_code=cleanup["error_code"], cleanup_error_type=cleanup["error_type"])
        result = {"state": "failed", **(PRIMARY_FAILURE or safe_error(exc, FAILED_STAGE or STAGE)), "diagnostics": DIAGNOSTICS}
    if guarded:
        try:
            persist("result.json", result)
        except Exception:
            result["diagnostics"]["result_persist_failed"] = True
            # Without a durable receipt the caller cannot confirm completion.
            result["state"] = "failed"
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--observe"]:
        print(json.dumps(observe(), sort_keys=True), flush=True)
    else:
        raise SystemExit(execute())
