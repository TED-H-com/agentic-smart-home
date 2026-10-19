"""Signed Host bundles in isolated disk trees; never a development-host switch."""
import base64
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import host_update as module
from agentic_host.host_update import (
    COMPATIBILITY,
    CONTRACT,
    HostBundle,
    HostUpdateError,
    canonical,
    validate_manifest,
)
from agentic_host.publisher import public_key


class Restore:
    def __init__(self):
        self.calls = []
    def capacity(self, required):
        self.calls.append(("capacity", required))
    def create(self, record):
        self.calls.append(("create", record["candidate_digest"]))
        return {"state": "complete", "scope": "root_and_haos", "ha_snapshot": "fixture-ha", "root_snapshot_name": "fixture-root"}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    umask = os.umask(0o077)
    original_stat = Path.stat
    def root_stat(path, *args, **kwargs):
        value = list(original_stat(path, *args, **kwargs))
        if value[4] == os.getuid():
            value[4] = 0
        if not path.is_relative_to(tmp_path):
            value[0] &= ~0o022
        return os.stat_result(value)
    monkeypatch.setattr(Path, "stat", root_stat)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    bundles = tmp_path / "bundles"
    previous = bundles / "bootstrap-fixture"
    (previous / "host").mkdir(mode=0o755, parents=True)
    (previous / "assets").mkdir()
    (previous / "assets/kept").write_text("existing-images-never-copied")
    (previous / "images.lock.json").write_text("{}")
    (previous / "providers.lock.json").write_text("{}")
    for name in ("host/bases.lock.json", "host/network.lock.json"):
        (previous / name).write_text("{}")
    (previous / "host/compose.yaml").write_text("fixture source")
    (previous / "BUNDLE.sha256.json").write_text(json.dumps({name: hashlib.sha256((previous / name).read_bytes()).hexdigest() for name in module.LOCKS}))
    active = tmp_path / "product"
    active.symlink_to(previous, target_is_directory=True)
    root = tmp_path / "state/host"
    root.mkdir(parents=True)
    database = root.parent / "controller/controller.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.executescript("PRAGMA user_version=3; CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); INSERT INTO settings VALUES('household_saved','current');")
    private = Ed25519PrivateKey.generate()
    pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    authority = tmp_path / "authority.json"
    authority.write_text(json.dumps({"format": 1, "publisher": module.PUBLISHER, "repository": module.REPOSITORY,
                                     "channel": "stable", "image_repository": module.IMAGE_REPOSITORY,
                                     "pin": {"public_key_pem": pem, "key_sha256": public_key(pem)[1]}}))
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"format": 1, "publishers": {module.PUBLISHER: {"public_key_pem": pem, "image_repository": module.IMAGE_REPOSITORY}}}))
    calls = []
    def run(*args, **kwargs):
        if args[0] == "openssl":
            return subprocess.run(args, capture_output=True, text=True, timeout=10)
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    restore = Restore()
    manager = SimpleNamespace(root=root, product=active, run=run, locked=nullcontext)
    archives = {}
    def download(url, target, maximum):
        data = archives[url]
        if len(data) > maximum:
            raise HostUpdateError("too_large")
        Path(target).write_bytes(data)
    engine = HostBundle(manager, bundles=bundles, authority=authority, trust=trust, download=download,
                        restore=restore, launch=lambda: calls.append(("launch",)),
                        broker_probe=lambda: {"contract": dict(CONTRACT), "bridge": {"controller_update": {"ingress_bridge_contract": 1}}, "status": {"vm": "running"}},
                        controller_probe=lambda: {"status": "ok", "host_update": dict(COMPATIBILITY)},
                        reconcile=SimpleNamespace(prepare=lambda *args, **kwargs: calls.append(("prepare",))))
    engine._test = SimpleNamespace(private=private, archives=archives, calls=calls, previous=previous, restore=restore)
    try:
        yield engine
    finally:
        os.umask(umask)


def files_for(engine):
    files = {name: b"fixture source" for name in module.REQUIRED}
    files["host/host-update-contract.json"] = canonical(CONTRACT)
    files["images.lock.json"] = b"{}"
    files["providers.lock.json"] = b"{}"
    files["host/bases.lock.json"] = b"{}"
    files["host/network.lock.json"] = b"{}"
    return files


def envelope(engine, *, files=None, members=None, manifest_change=None):
    files = files_for(engine) if files is None else files
    index = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files = {**files, "BUNDLE.sha256.json": canonical(index)}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.USTAR_FORMAT) as stream:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.mode, member.size = 0o644, len(data)
            stream.addfile(member, io.BytesIO(data))
        for member in members or []:
            stream.addfile(member, io.BytesIO(b"x" * member.size) if member.isfile() else None)
    data = archive.getvalue()
    version = "0.11.1"
    url = module.RELEASE + "download/v" + version + "/agentic-home-host-" + version + ".tar"
    manifest = {"format": 1, "kind": "agentic-home-host-bundle", "publisher": module.PUBLISHER, "version": version,
                "source_commit": "b" * 40, "bundle": {"url": url, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
                "compatibility": dict(COMPATIBILITY), "issued_at": time.time() - 1, "expires_at": time.time() + 1000,
                "tests": [{"name": "fixture", "result": "passed", "sha256": "c" * 64}]}
    manifest.update(manifest_change or {})
    engine._test.archives[url] = data
    signature = base64.b64encode(engine._test.private.sign(canonical(manifest))).decode()
    return {"manifest": manifest, "signature": signature}


def staged(engine):
    body = envelope(engine)
    record = engine.stage(body, background=False)
    assert record["state"] == "staged"
    return record


def approved(engine):
    record = staged(engine)
    result = engine._approval({"candidate_digest": record["candidate_digest"], "approval_id": record["approval_id"]})
    assert result["state"] == "queued"
    return result


def execution_fixture(engine, monkeypatch):
    monkeypatch.setattr(engine, "_stop_controller", lambda record: record.update(controller_was_running=True))
    monkeypatch.setattr(engine, "_start_services", lambda record: engine._test.calls.append(("restart",)))
    monkeypatch.setattr(engine, "_hook", lambda action, candidate, **kwargs: engine._test.calls.append(("hook", action)))
    monkeypatch.setattr(engine, "_health", lambda **kwargs: engine._test.calls.append(("health", kwargs["candidate"])))


def test_stage_verifies_real_ed25519_extracts_without_executing_or_switching(lab):
    record = staged(lab)
    assert lab.current() == lab._test.previous
    target = lab.bundles / record["candidate_digest"]
    assert (target / "assets").is_symlink()
    assert (target / "assets/kept").read_text() == "existing-images-never-copied"
    assert lab._test.calls == [] and all(call[0] == "capacity" for call in lab._test.restore.calls)
    assert lab.status()["available"] and lab.status()["compatibility"] == COMPATIBILITY


def test_signatures_require_exact_official_pinned_key_and_bound_manifest(lab):
    body = envelope(lab)
    body["manifest"]["version"] = "0.11.2"
    with pytest.raises(HostUpdateError):
        lab.verify_envelope(body)
    body = envelope(lab)
    body["signature"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(HostUpdateError, match="signature_invalid"):
        lab.verify_envelope(body)
    lab.trust.write_text('{"format":1,"publishers":{}}')
    with pytest.raises(HostUpdateError, match="not_trusted"):
        lab.verify_envelope(envelope(lab))
    assert lab.current() == lab._test.previous


@pytest.mark.parametrize("change", [{"format": True}, {"kind": "controller"}, {"publisher": "fixture"},
                                   {"shell": "evil"}, {"compatibility": {**COMPATIBILITY, "host_protocol": True}},
                                   {"compatibility": {**COMPATIBILITY, "controller_schema": 4}},
                                   {"tests": []}, {"expires_at": float("inf")}, {"expires_at": 0}])
def test_manifest_rejects_hooks_other_publishers_protocols_missing_evidence_and_expiry(lab, change):
    manifest = envelope(lab)["manifest"]
    with pytest.raises(HostUpdateError):
        validate_manifest({**manifest, **change})


@pytest.mark.parametrize("url", ["https://evil.test/product.tar", "http://github.com/TED-H-com/agentic-smart-home/file",
                                module.RELEASE + "latest/download/product.tar", module.RELEASE + "download/v0.11.1/agentic-home-host-0.11.1.tar?token=x"])
def test_signed_manifest_cannot_choose_another_download_endpoint(lab, url):
    manifest = envelope(lab)["manifest"]
    with pytest.raises(HostUpdateError, match="bundle_invalid"):
        validate_manifest({**manifest, "bundle": {**manifest["bundle"], "url": url}})


@pytest.mark.parametrize("kind", ["traversal", "absolute", "symlink", "hardlink", "fifo", "duplicate", "assets", "setuid", "many", "large"])
def test_unsafe_archive_never_switches_or_writes_outside_staging(lab, monkeypatch, kind):
    member = tarfile.TarInfo("host/extra")
    member.mode = 0o644
    if kind == "traversal":
        member.name = "host/../../outside"
    elif kind == "absolute":
        member.name = "/outside"
    elif kind == "symlink":
        member.type, member.linkname = tarfile.SYMTYPE, "/etc"
    elif kind == "hardlink":
        member.type, member.linkname = tarfile.LNKTYPE, "/etc/shadow"
    elif kind == "fifo":
        member.type = tarfile.FIFOTYPE
    elif kind == "duplicate":
        member.name = "images.lock.json"
    elif kind == "assets":
        member.name = "assets/arbitrary"
    elif kind == "setuid":
        member.mode = 0o4755
    elif kind == "many":
        monkeypatch.setattr(module, "MAX_FILES", 8)
    else:
        monkeypatch.setattr(module, "MAX_FILE", 2)
    record = lab.stage(envelope(lab, members=[member]), background=False)
    assert record["state"] == "failed"
    assert lab.current() == lab._test.previous
    assert not (lab.bundles.parent / "outside").exists()
    assert not list(lab.bundles.glob("*.partial"))


def test_runtime_lock_changes_are_not_silent_dependency_upgrades(lab):
    files = files_for(lab)
    files["providers.lock.json"] = b'{"changed":true}'
    assert lab.stage(envelope(lab, files=files), background=False)["state"] == "failed"


def test_apply_approval_is_once_expiring_exact_and_survives_lost_reply(lab):
    record = staged(lab)
    body = {"candidate_digest": record["candidate_digest"], "approval_id": record["approval_id"]}
    with pytest.raises(HostUpdateError, match="approval_invalid"):
        lab._approval({**body, "approval_id": "wrong" * 8})
    assert lab._approval(body)["state"] == "queued"
    assert lab._approval(body)["state"] == "queued"
    assert lab._test.calls == [("launch",)]
    assert "approval_id" not in lab.records()[record["candidate_digest"]]
    for path in ("/network/preferences", "/updates/apply", "/coding/jobs", "/actions"):
        with pytest.raises(HostUpdateError, match="pending_recovery"):
            lab.guard(path, {"action": "start"})
    lab.guard("/actions", {"action": "shutdown"})
    lab.guard("/coding/fence", {})


def test_expired_approval_and_changed_previous_tree_block_install(lab):
    record = staged(lab)
    private = lab.records()[record["candidate_digest"]]
    private["approval_expires_at"] = 0
    lab.save(private)
    with pytest.raises(HostUpdateError, match="approval_invalid"):
        lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": record["approval_id"]})
    refreshed = lab.stage(envelope(lab), background=False)
    assert refreshed["state"] == "staged"
    (lab._test.previous / "manual-edit").write_text("owner edit")
    with pytest.raises(HostUpdateError, match="product_changed"):
        lab._approval({"candidate_digest": refreshed["candidate_digest"], "approval_id": refreshed["approval_id"]})


def test_executor_switches_only_after_backup_and_can_manually_rollback_current_data(lab, monkeypatch):
    record = approved(lab)
    execution_fixture(lab, monkeypatch)
    result = lab.execute()
    assert result["state"] == "deployed"
    assert lab.current() == lab.bundles / record["candidate_digest"]
    first_backup = lab._test.restore.calls[-1]
    assert first_backup[0] == "create"
    offer = lab.rollback_offer({"candidate_digest": record["candidate_digest"]})
    lab.trust.write_text('{"format":1,"publishers":{}}')  # rescue remains usable after revocation
    lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": offer["rollback_approval_id"]}, rollback=True)
    result = lab.execute()
    assert result["state"] == "rolled_back"
    assert lab.current() == lab._test.previous
    assert lab._test.restore.calls[-1][1] != first_backup[1]


def test_failed_health_restores_previous_code_automatically(lab, monkeypatch):
    approved(lab)
    execution_fixture(lab, monkeypatch)
    def health(**kwargs):
        if kwargs["candidate"]:
            raise HostUpdateError("new_broker_unhealthy")
    monkeypatch.setattr(lab, "_health", health)
    result = lab.execute()
    assert result["state"] == "rolled_back" and result["auto_rollback"]
    assert lab.current() == lab._test.previous


def test_recovery_never_activates_new_queued_approval_at_boot(lab, monkeypatch):
    approved(lab)
    monkeypatch.setattr(lab, "_switch", lambda target: pytest.fail("boot must not install an update"))
    result = lab.execute(recover=True)
    assert result["state"] == "failed" and lab.current() == lab._test.previous
    assert not any(call[0] == "create" for call in lab._test.restore.calls)


def test_restart_recovery_compensates_a_previously_switched_attempt(lab, monkeypatch):
    record = approved(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored.update(state="activating", phase="reconciling", controller_was_running=True)
    lab.save(stored)
    lab._switch(lab.bundles / record["candidate_digest"])
    execution_fixture(lab, monkeypatch)
    result = lab.execute(recover=True)
    assert result["state"] == "rolled_back" and result["auto_rollback"]
    assert lab.current() == lab._test.previous


def test_uncertain_fallback_keeps_journal_fence_and_never_restores_database(lab, monkeypatch):
    approved(lab)
    execution_fixture(lab, monkeypatch)
    monkeypatch.setattr(lab, "_health", lambda **kwargs: (_ for _ in ()).throw(HostUpdateError("unavailable")))
    result = lab.execute()
    assert result["state"] == "needs_reconcile"
    assert lab.busy()
    assert not any(call[0] in {"lvconvert", "cp", "sqlite3"} for call in lab._test.calls)


def test_post_health_python_cache_does_not_change_program_identity(lab, monkeypatch):
    approved(lab)
    execution_fixture(lab, monkeypatch)
    result = lab.execute()
    target = lab.current()
    cache = target / "host/agentic_host/__pycache__"
    cache.mkdir()
    (cache / "daemon.cpython-313.pyc").write_bytes(b"runtime cache")
    offer = lab.rollback_offer({"candidate_digest": result["candidate_digest"]})
    (cache / "other.pyc").write_bytes(b"another cache")
    lab._approval({"candidate_digest": result["candidate_digest"], "approval_id": offer["rollback_approval_id"]}, rollback=True)
    assert lab.execute()["state"] == "rolled_back"


def test_manual_rollback_failed_health_stays_at_approved_previous_and_fenced(lab, monkeypatch):
    record = approved(lab)
    execution_fixture(lab, monkeypatch)
    assert lab.execute()["state"] == "deployed"
    offer = lab.rollback_offer({"candidate_digest": record["candidate_digest"]})
    body = {"candidate_digest": record["candidate_digest"], "approval_id": offer["rollback_approval_id"]}
    lab._approval(body, rollback=True)
    monkeypatch.setattr(lab, "_health", lambda **kwargs: (_ for _ in ()).throw(HostUpdateError("unavailable")))
    result = lab.execute()
    assert result["state"] == "needs_reconcile" and lab.current() == lab._test.previous
    monkeypatch.setattr(lab, "_health", lambda **kwargs: None)
    rescue = lab.rollback_offer({"candidate_digest": record["candidate_digest"]})
    lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": rescue["rollback_approval_id"]}, rollback=True)
    result = lab.execute()
    assert result["state"] == "rolled_back" and not result["auto_rollback"]
    assert lab.current() == lab._test.previous


def test_interrupted_manual_rollback_finishes_only_the_approved_previous_tree(lab, monkeypatch):
    record = approved(lab)
    execution_fixture(lab, monkeypatch)
    lab.execute()
    offer = lab.rollback_offer({"candidate_digest": record["candidate_digest"]})
    lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": offer["rollback_approval_id"]}, rollback=True)
    stored = lab.records()[record["candidate_digest"]]
    stored.update(state="rolling_back", phase="switching")
    lab.save(stored)
    lab._switch(lab._test.previous)
    result = lab.execute(recover=True)
    assert result["state"] == "rolled_back" and not result["auto_rollback"]
    assert lab.current() == lab._test.previous


def test_success_clears_exact_updater_gate_but_keeps_ai_paused_and_current_data(lab, monkeypatch):
    record = approved(lab)
    database = lab.manager.root.parent / "controller/controller.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO settings VALUES('updater_gate',?)", (json.dumps(record["candidate_digest"]),))
    execution_fixture(lab, monkeypatch)
    assert lab.execute()["state"] == "deployed"
    with sqlite3.connect(database) as connection:
        settings = dict(connection.execute("SELECT key,value FROM settings"))
    assert "updater_gate" not in settings
    assert settings["paused"] == "true" and settings["household_saved"] == "current"


def test_incompatible_controller_is_rejected_before_stopping_or_backups(lab, monkeypatch):
    approved(lab)
    monkeypatch.setattr(lab, "controller_probe", lambda: {"status": "ok", "host_update": {**COMPATIBILITY, "host_protocol": True}})
    monkeypatch.setattr(lab, "_stop_controller", lambda record: pytest.fail("must not stop before protocol proof"))
    result = lab.execute()
    assert result["state"] == "failed" and lab.current() == lab._test.previous
    assert not any(call[0] == "create" for call in lab._test.restore.calls)


@pytest.mark.parametrize("state", ["staging", "deploying", "rolling_back", "needs_reconcile"])
def test_active_controller_update_blocks_owner_apply_before_any_host_effect(lab, state):
    record = staged(lab)
    path = lab.manager.root / "updates/records.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"other": {"state": state}}))
    with pytest.raises(HostUpdateError, match="controller_update_active"):
        lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": record["approval_id"]})
    assert lab._test.calls == []
    assert not any(call[0] == "create" for call in lab._test.restore.calls)


def test_controller_update_state_rechecked_by_executor_before_service_stop(lab, monkeypatch):
    approved(lab)
    path = lab.manager.root / "updates/records.json"
    path.parent.mkdir()
    path.write_text('{"other":{"state":"staging"}}')
    monkeypatch.setattr(lab, "_stop_controller", lambda record: pytest.fail("unsafe concurrent maintenance"))
    result = lab.execute()
    assert result["state"] == "failed"
    assert lab.current() == lab._test.previous


def test_interrupted_staging_adopts_verified_inactive_tree_without_activation(lab):
    record = staged(lab)
    saved = lab.records()[record["candidate_digest"]]
    saved["state"] = "staging"
    lab.save(saved)
    body = {"manifest": saved["manifest"], "signature": saved["signature"]}
    recovered = lab.stage(body, background=False)
    assert recovered["state"] == "staged" and lab.current() == lab._test.previous


def test_preparation_holds_manager_lifecycle_lock_during_capacity_and_copy(lab):
    from contextlib import contextmanager
    held = []
    @contextmanager
    def locked():
        held.append(True)
        try:
            yield
        finally:
            held.pop()
    lab.manager.locked = locked
    fetch = lab.fetch
    def checked_fetch(*args):
        assert held
        return fetch(*args)
    lab.fetch = checked_fetch
    assert staged(lab)["state"] == "staged"
    assert not held


def test_stable_service_startup_marks_interrupted_preparations_retryable_without_install(lab):
    record = staged(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored["state"] = "staging"
    lab.save(stored)
    lab.check_path.write_text('{"state":"checking"}')
    lab.recover_preparations()
    assert lab.records()[record["candidate_digest"]]["state"] == "failed"
    assert lab.status()["check"]["state"] == "failed"
    assert lab.current() == lab._test.previous
    assert lab.stage({"manifest": stored["manifest"], "signature": stored["signature"]}, background=False)["state"] == "staged"


@pytest.mark.parametrize("recovery_only,state,phase", [(False, "queued", "approved"), (True, "queued", "approved"),
                                                      (False, "activating", "creating_restore_point")])
def test_executor_lifecycle_lock_conflict_only_consumes_attempt_before_live_effects(lab, monkeypatch, recovery_only, state, phase):
    from contextlib import contextmanager

    from agentic_host.manager import HostError
    record = approved(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored.update(state=state, phase=phase, recovery_only=recovery_only)
    lab.save(stored)
    @contextmanager
    def busy():
        raise HostError("Another host operation is running")
        yield
    lab.manager.locked = busy
    monkeypatch.setattr(lab, "_stop_controller", lambda record: pytest.fail("must not stop services"))
    if state == "queued" and not recovery_only:
        result = lab.execute()
        assert result["state"] == "failed" and result["error_code"] == "host_update_host_busy"
    else:
        with pytest.raises(HostError):
            lab.execute(recover=True)
        assert lab.records()[record["candidate_digest"]]["state"] == state
    assert lab.current() == lab._test.previous


def cancellation(record, operation="apply"):
    field = "approval_id" if operation == "apply" else "rollback_approval_id"
    return {"candidate_digest": record["candidate_digest"], "operation": operation,
            "approval_digest": hashlib.sha256(record[field].encode()).hexdigest()}


def test_cancel_apply_offer_is_durable_idempotent_and_rejects_delayed_acceptance(lab):
    record = staged(lab)
    body = cancellation(record)
    status, result = lab.handle("POST", "/host-updates/cancel-offer", body)
    assert status == 200 and result["cancelled"] is True
    assert result["record"]["state"] == "failed"
    assert lab.cancel_offer(body)["cancelled"] is True
    with pytest.raises(HostUpdateError, match="approval_invalid"):
        lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": record["approval_id"]})
    assert lab.current() == lab._test.previous and not lab._test.calls
    saved = lab.records()[record["candidate_digest"]]
    fresh = lab.stage({"manifest": saved["manifest"], "signature": saved["signature"]}, background=False)
    assert fresh["state"] == "staged" and fresh["approval_id"] != record["approval_id"]


def test_old_cancellation_proof_preserves_fresh_offer(lab):
    first = staged(lab)
    stored = lab.records()[first["candidate_digest"]]
    fresh = lab.stage({"manifest": stored["manifest"], "signature": stored["signature"]}, background=False)
    assert fresh["approval_id"] != first["approval_id"]
    assert lab.cancel_offer(cancellation(first))["cancelled"] is True
    assert lab.records()[first["candidate_digest"]]["approval_id"] == fresh["approval_id"]
    assert lab._approval({"candidate_digest": fresh["candidate_digest"], "approval_id": fresh["approval_id"]})["state"] == "queued"


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_cancellation_never_undoes_accepted_operation(lab, monkeypatch, operation):
    execution_fixture(lab, monkeypatch)
    record = staged(lab)
    if operation == "rollback":
        lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": record["approval_id"]})
        assert lab.execute()["state"] == "deployed"
        record = lab.rollback_offer({"candidate_digest": record["candidate_digest"]})
    proof = cancellation(record, operation)
    token = "approval_id" if operation == "apply" else "rollback_approval_id"
    lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": record[token]}, rollback=operation == "rollback")
    before = lab.records()
    result = lab.cancel_offer(proof)
    assert result["cancelled"] is False and result["record"]["state"] == "queued"
    assert lab.records() == before


def test_cancel_rollback_offer_keeps_deployed_tree_and_rejects_delayed_rollback(lab, monkeypatch):
    execution_fixture(lab, monkeypatch)
    record = approved(lab)
    assert lab.execute()["state"] == "deployed"
    offer = lab.rollback_offer({"candidate_digest": record["candidate_digest"]})
    proof = cancellation(offer, "rollback")
    result = lab.cancel_offer(proof)
    assert result["cancelled"] is True and result["record"]["state"] == "deployed"
    assert lab.cancel_offer(proof)["cancelled"] is True
    with pytest.raises(HostUpdateError, match="approval_invalid"):
        lab._approval({"candidate_digest": record["candidate_digest"], "approval_id": offer["rollback_approval_id"]}, rollback=True)
    assert lab.current() == lab.bundles / record["candidate_digest"]


def test_matching_uncertain_recovery_offer_cancellation_fails_closed(lab):
    record = staged(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored.update(state="needs_reconcile", rollback_approval_id="uncertain-token")
    lab.save(stored)
    with pytest.raises(HostUpdateError, match="cancellation_uncertain"):
        lab.cancel_offer(cancellation(stored, "rollback"))
    assert lab.records()[record["candidate_digest"]] == stored


@pytest.mark.parametrize("state", ["staging", "queued", "activating", "rolling_back", "needs_reconcile"])
def test_mismatched_cancellation_proof_never_clears_busy_or_uncertain_attempt(lab, state):
    record = staged(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored["state"] = state
    lab.save(stored)
    body = {**cancellation(record), "approval_digest": "f" * 64}
    with pytest.raises(HostUpdateError, match="cancellation_uncertain"):
        lab.cancel_offer(body)
    assert lab.records()[record["candidate_digest"]] == stored


def test_offer_cancellation_cannot_clear_fence_for_another_active_proposal(lab):
    record = staged(lab)
    other = {**lab.records()[record["candidate_digest"]], "candidate_digest": "e" * 64, "state": "activating"}
    lab.save(other)
    with pytest.raises(HostUpdateError, match="cancellation_uncertain"):
        lab.cancel_offer(cancellation(record))
    assert lab.records()[record["candidate_digest"]]["state"] == "staged"


def set_dispatch_gate(engine, record, **changes):
    saved = engine.records()[record["candidate_digest"]]
    gate = {"candidate_digest": saved["candidate_digest"], "operation": saved.get("consumed_action", saved["operation"]),
            "approval_digest": saved["consumed_token_sha256"], "authority": {"owner": "fixture"}, **changes}
    database = engine.manager.root.parent / "controller/controller.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT OR REPLACE INTO settings VALUES ('host_bundle_update_gate',?)", (json.dumps(gate),))
    return database, gate


@pytest.mark.parametrize("change", [{}, {"candidate_digest": "f" * 64}, {"operation": "rollback"},
                                    {"approval_digest": "f" * 64}])
def test_terminal_dispatch_gate_cleanup_requires_exact_proof_and_preserves_other_settings(lab, change):
    record = approved(lab)
    database, gate = set_dispatch_gate(lab, record, **change)
    saved = lab.records()[record["candidate_digest"]]
    saved.update(state="failed", error_code="host_update_preflight_failed")
    lab.save(saved)
    assert lab._clear_dispatch_gate(saved) is (not change)
    with sqlite3.connect(database) as connection:
        values = dict(connection.execute("SELECT key,value FROM settings"))
        assert values["household_saved"] == "current"
        assert json.loads(values["host_bundle_update_gate"]) == (gate if change else None)
        if not change:
            assert values["paused"] == "true"


def test_dispatch_cleanup_never_clears_before_terminal_save_or_when_schema_unsupported(lab):
    record = approved(lab)
    database, gate = set_dispatch_gate(lab, record)
    saved = lab.records()[record["candidate_digest"]]
    assert lab._clear_dispatch_gate(saved) is False
    saved.update(state="failed", error_code="host_update_preflight_failed")
    assert lab._clear_dispatch_gate(saved) is False  # Still queued on disk.
    lab.save(saved)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=4")
    assert lab._clear_dispatch_gate(saved) is False
    with sqlite3.connect(database) as connection:
        assert json.loads(connection.execute("SELECT value FROM settings WHERE key='host_bundle_update_gate'").fetchone()[0]) == gate


def test_unknown_failed_phase_does_not_clear_dispatch_gate(lab):
    record = approved(lab)
    database, gate = set_dispatch_gate(lab, record)
    saved = lab.records()[record["candidate_digest"]]
    saved.update(state="failed", phase="unknown", error_code="host_update_preflight_failed")
    lab.save(saved)
    assert lab._clear_dispatch_gate(saved) is False
    with sqlite3.connect(database) as connection:
        assert json.loads(connection.execute("SELECT value FROM settings WHERE key='host_bundle_update_gate'").fetchone()[0]) == gate


@pytest.mark.parametrize("outcome", ["success", "fallback", "preflight", "before_start", "manager_busy"])
def test_executor_terminal_outcomes_clear_dispatch_gate_without_browser(lab, monkeypatch, outcome):
    from contextlib import contextmanager

    from agentic_host.manager import HostError
    execution_fixture(lab, monkeypatch)
    record = approved(lab)
    database, _ = set_dispatch_gate(lab, record)
    if outcome == "fallback":
        monkeypatch.setattr(lab, "_create_restore_point", lambda record: (_ for _ in ()).throw(HostError("fixture_failure")))
    elif outcome == "preflight":
        monkeypatch.setattr(lab, "require_no_controller_update", lambda: (_ for _ in ()).throw(HostError("fixture_busy")))
    elif outcome == "manager_busy":
        @contextmanager
        def busy():
            raise HostError("fixture_busy")
            yield
        lab.manager.locked = busy
    result = lab.execute(recover=outcome == "before_start")
    assert result["state"] in {"deployed", "rolled_back", "failed"}
    with sqlite3.connect(database) as connection:
        values = dict(connection.execute("SELECT key,value FROM settings"))
        assert json.loads(values["host_bundle_update_gate"]) is None and values["paused"] == "true"


def test_bad_controller_gate_cleanup_cannot_rollback_healthy_install(lab, monkeypatch):
    execution_fixture(lab, monkeypatch)
    record = approved(lab)
    database, _ = set_dispatch_gate(lab, record)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE settings SET value='invalid-json' WHERE key='host_bundle_update_gate'")
    assert lab.execute()["state"] == "deployed"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM settings WHERE key='host_bundle_update_gate'").fetchone()[0] == "invalid-json"


@pytest.mark.parametrize("change", [{"operation": []}, {"operation": "delete"}, {"approval_digest": "bad"},
                                    {"candidate_digest": "bad"}, {"extra": "forbidden"}])
def test_cancellation_request_is_strict(lab, change):
    body = {"candidate_digest": "a" * 64, "operation": "apply", "approval_digest": "b" * 64, **change}
    with pytest.raises(HostUpdateError, match="request_invalid"):
        lab.cancel_offer(body)


@pytest.mark.parametrize("phase", ["ha_shutdown_requested", "ha_snapshot_requested", "ha_snapshot_complete"])
def test_interrupted_ha_backup_restarts_original_running_guest_without_another_snapshot(lab, monkeypatch, phase):
    record = approved(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored.update(state="activating", phase="creating_restore_point", controller_was_running=True,
                  restore_attempt={"candidate_digest": "c" * 64,
                                   "restore_point": {"phase": phase, "ha_original_state": "running", "ha_snapshot": None}})
    lab.save(stored)
    execution_fixture(lab, monkeypatch)
    lab.restore = None  # exercise the real fixed HostRestore.recover_ha implementation
    vm = ["shut off"]
    events = []
    lab.manager.owned = lambda: object()
    lab.manager.snapshot = lambda: pytest.fail("an uncertain snapshot must never be repeated")
    lab.manager.virsh = lambda *args: SimpleNamespace(stdout=vm[0])
    def start():
        events.append("start")
        vm[0] = "running"
    lab.manager.start = start
    monkeypatch.setattr(lab, "_health", lambda **kwargs: events.append("health"))
    result = lab.execute(recover=True)
    assert result["state"] == "rolled_back" and vm[0] == "running"
    assert events == ["start", "health"]
    assert lab.records()[record["candidate_digest"]]["restore_attempt"]["restore_point"]["ha_recovery"] == "running_verified"


def test_interrupted_ha_backup_cannot_claim_rollback_if_restart_readback_is_stopped(lab, monkeypatch):
    record = approved(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored.update(state="activating", phase="creating_restore_point", controller_was_running=True,
                  restore_attempt={"candidate_digest": "c" * 64,
                                   "restore_point": {"phase": "ha_snapshot_requested", "ha_original_state": "running", "ha_snapshot": None}})
    lab.save(stored)
    execution_fixture(lab, monkeypatch)
    lab.restore = None
    lab.manager.owned = lambda: object()
    lab.manager.virsh = lambda *args: SimpleNamespace(stdout="shut off")
    lab.manager.start = lambda: {"vm": "running"}  # response alone is insufficient
    lab.manager.snapshot = lambda: pytest.fail("must not retry snapshot")
    monkeypatch.setattr(lab, "_health", lambda **kwargs: pytest.fail("must not claim healthy while HA is stopped"))
    result = lab.execute(recover=True)
    assert result["state"] == "needs_reconcile" and lab.busy()


def test_complete_backup_does_not_reverse_a_later_owner_rescue_shutdown(lab, monkeypatch):
    record = approved(lab)
    stored = lab.records()[record["candidate_digest"]]
    stored.update(state="activating", phase="creating_restore_point", controller_was_running=True,
                  restore_attempt={"candidate_digest": "c" * 64,
                                   "restore_point": {"phase": "complete", "ha_original_state": "running", "ha_snapshot": "saved"}})
    lab.save(stored)
    execution_fixture(lab, monkeypatch)
    lab.manager.start = lambda: pytest.fail("a completed backup does not authorize reversing a later owner stop")
    assert lab.execute(recover=True)["state"] == "rolled_back"


def test_deferred_os_configuration_is_reported_as_requiring_reboot(lab, monkeypatch):
    approved(lab)
    execution_fixture(lab, monkeypatch)
    monkeypatch.setattr(lab, "_hook", lambda action, candidate, **kwargs:
                        {"state": "applied", "reboot_required": True} if action == "apply" else {})
    result = lab.execute()
    assert result["state"] == "deployed"
    assert "部分主機設定將在重新開機後生效" in result["message"]
    assert lab.records()[result["candidate_digest"]]["reboot_required"] is True



def test_compose_boot_references_cannot_change_under_a_host_only_update(lab):
    files = files_for(lab)
    files["host/compose.yaml"] = b"services:\n  runner:\n    image: unexpected-new-runtime:latest\n"
    result = lab.stage(envelope(lab, files=files), background=False)
    assert result["state"] == "failed" and lab.current() == lab._test.previous
