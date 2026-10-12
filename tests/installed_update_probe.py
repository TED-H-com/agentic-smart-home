"""Run as root ONLY in the disposable installed-product VM, after other probes.

Registry delivery and publisher identity are explicit synthetic fixtures. The
installed broker's signature verification, bounded compatibility probe, compose
activation, authenticated smoke, SQLite snapshots and rollback execute for real.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, "/opt/agentic-home/host")
from agentic_host.bootstrap import write_environment
from agentic_host.manager import Manager, atomic, run
from agentic_host.updater import Updater, canonical

STAGE = "disposable_vm_guard"
FAILED_STAGE = None
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
}


def main():
    global STAGE, FAILED_STAGE
    if os.geteuid() != 0 or not Path("/opt/agentic-home/installed.json").exists():
        # Some media records installed.json in /var/lib; accept only the exact
        # installed root plus explicit caller-supplied disposable-VM marker.
        if os.geteuid() != 0 or os.environ.get("AGENTIC_DISPOSABLE_VM") != "1" or not Path("/opt/agentic-home/host/compose.yaml").exists():
            raise RuntimeError("disposable_installed_vm_required")
    if os.environ.get("AGENTIC_DISPOSABLE_VM") != "1":
        raise RuntimeError("disposable_installed_vm_required")
    STAGE = "installed_update_baseline"
    os.umask(0o077)
    manager = Manager()
    service = Updater(manager)
    identity = uuid.uuid4().hex
    fixture = manager.root / ("update-fixture-" + identity)
    fixture.mkdir(mode=0o700)
    environment_before = service.environment.read_bytes() if service.environment.exists() else None
    records_before = service.path.read_bytes()
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
    STAGE = "stop_host_broker_for_isolated_fixture"
    run("systemctl", "stop", "agentic-home-host.service", timeout=60)
    try:
        STAGE = "build_fixed_label_candidate"
        run("docker", "tag", original, base_tag)
        (fixture / "Dockerfile").write_text(f"FROM {base_tag}\nLABEL agentic.disposable.update={identity}\n")
        run("docker", "build", "--network", "none", "--pull=false", "-t", candidate_tag, str(fixture), timeout=180)
        candidate = run("docker", "image", "inspect", "--format", "{{.Id}}", candidate_tag).stdout.strip()
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", candidate) and candidate != original
        STAGE = "ephemeral_publisher_and_signature"
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
                    "source_commit": "e" * 40, "image": "ghcr.io/disposable-fixture/controller@" + candidate,
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
            return run(*args, **kwargs)
        service.run = execute
        STAGE = "verify_signature_and_stage_candidate"
        status, record = service.handle("POST", "/updates/stage", {"manifest": manifest, "signature": signature})
        assert status == 202
        record = service.records[record["candidate_digest"]]
        def wait(states, timeout=600):
            deadline = time.monotonic() + timeout
            while record["state"] not in states:
                if record["state"] in {"failed", "needs_reconcile", "rolled_back"}:
                    DIAGNOSTICS["updater_state"] = record["state"]
                    error = record.get("error_code")
                    DIAGNOSTICS["updater_error_code"] = error if error in SAFE_ERRORS else "unclassified_update_error"
                    raise RuntimeError("installed_update_failed")
                if time.monotonic() > deadline:
                    raise RuntimeError("installed_update_deadline")
                time.sleep(.25)
        wait({"staged"})
        digest = record["candidate_digest"]
        STAGE = "approved_candidate_activation"
        service.handle("POST", "/updates/apply", {"candidate_digest": digest, "approval_id": record["approval_id"]})
        wait({"deployed"})
        assert service.image() == candidate
        # This is a synthetic durable post-deployment record in the real Live
        # database, written while its owner is stopped. It represents the data
        # that would be lost by restoring the pre-deployment snapshot.
        STAGE = "durable_post_deployment_record"
        service.stop()
        marker = "post-deployment-" + identity
        with sqlite3.connect(service.database) as db:
            now = "2026-10-12T00:00:00+00:00"
            db.execute("INSERT INTO records VALUES (?,?,?,?,?)", ("update_vm_fixture", marker, json.dumps({"id": marker, "fixture": True}), now, now))
        service.activate(candidate)
        service.health()
        STAGE = "approved_same_database_rollback"
        offered = service.handle("POST", "/updates/rollback-offer", {"candidate_digest": digest})[1]
        service.handle("POST", "/updates/rollback", {"candidate_digest": digest, "approval_id": offered["rollback_approval_id"]})
        wait({"rolled_back"})
        STAGE = "rollback_readback_and_row_preservation"
        assert service.image() == original
        with sqlite3.connect(service.database.as_uri() + "?mode=ro", uri=True) as db:
            assert db.execute("SELECT body FROM records WHERE kind='update_vm_fixture' AND id=?", (marker,)).fetchone()
            assert db.execute("PRAGMA user_version").fetchone()[0] == schema
            assert db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"
            assert db.execute("SELECT value FROM settings WHERE key='updater_gate'").fetchone() is None
        result = {"state": "passed", "observed_from": "disposable_installed_linux_vm_root",
                  "actual": ["ed25519_signature_verification", "bounded_candidate_probe", "compose_activation", "authenticated_owner_smoke",
                             "consistent_snapshot", "schema3", "maintenance_gate", "same_database_rollback", "post_deployment_row_preserved"],
                  "fixtures": ["ephemeral_publisher", "preloaded_registry_delivery", "candidate_changes_only_image_label", "synthetic_post_deployment_record"],
                  "real_cloud": False, "real_publisher_release": False, "original_image": original, "candidate_image": candidate,
                  "registry_delivery_calls": len(deliveries), "final_schema": schema, "rollback_restored_backup": False}
    except Exception:
        FAILED_STAGE = STAGE
        raise
    finally:
        # Exact original artifact and owner pause setting are restored; never
        # restore a stale DB. The surrounding disposable VM is deleted by pytest.
        STAGE = "restore_original_image_and_cleanup"
        DIAGNOSTICS["cleanup_started"] = True
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
        run("docker", "image", "rm", candidate_tag, base_tag, timeout=30, check=False)
        if candidate:
            run("docker", "image", "rm", candidate, timeout=30, check=False)
        shutil.rmtree(fixture)
        run("systemctl", "start", "agentic-home-host.service", timeout=60)
        DIAGNOSTICS["cleanup_completed"] = True
    result["diagnostics"] = DIAGNOSTICS
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not print subprocess output, account material, or private paths.
        code = str(exc) if str(exc) in SAFE_ERRORS else "unclassified_update_error"
        print(json.dumps({"state": "failed", "error_code": code, "error_type": type(exc).__name__,
                          "stage": FAILED_STAGE or STAGE, "diagnostics": DIAGNOSTICS}), flush=True)
        raise SystemExit(1) from None
