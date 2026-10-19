import base64
import copy
import importlib.util
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host import bootstrap_owner as module  # noqa: E402
from agentic_host.bootstrap_media import DIRECTORY, LABEL, BootstrapMedia  # noqa: E402
from agentic_host.bootstrap_owner import BootstrapOwner  # noqa: E402
from agentic_host.host_update import COMPATIBILITY, IMAGE_REPOSITORY, PUBLISHER, REPOSITORY, canonical, digest  # noqa: E402
from agentic_host.manager import HostError  # noqa: E402
from agentic_host.publisher import public_key  # noqa: E402


@pytest.fixture
def owner(tmp_path, monkeypatch):
    previous_umask = os.umask(0o077)
    original_stat = Path.stat

    def root_stat(path, *args, **kwargs):
        value = list(original_stat(path, *args, **kwargs))
        if value[4] == os.getuid():
            value[4] = 0
        if not path.is_relative_to(tmp_path):
            value[0] &= ~0o022
        return os.stat_result(value)

    monkeypatch.setattr(Path, "stat", root_stat)
    root, product, payload = tmp_path / "state/host", tmp_path / "installed", tmp_path / "payload"
    root.mkdir(parents=True)
    product.mkdir()
    (product / "images.lock.json").write_text('{}')
    (root / "runtime.env").write_text("# Original runtime\n")
    source = payload / "product/host"
    (source / "agentic_host").mkdir(parents=True)
    for name in ("__init__.py", "bootstrap_owner.py", "bootstrap_media.py", "host_update.py", "host_restore.py", "host_reconcile.py", "update_bootstrap.py"):
        (source / "agentic_host" / name).write_text("# public synthetic fixture\n")
    (source / "systemd").mkdir()
    for name in module.ENGINE_UNITS:
        (source / "systemd" / name).write_text("[Service]\nType=oneshot\n")
    # Ephemeral test key only; no private material is written or published.
    private = Ed25519PrivateKey.generate()
    pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    metadata = {"format": 1, "publisher": PUBLISHER, "repository": REPOSITORY, "channel": "stable",
                "image_repository": IMAGE_REPOSITORY, "pin": {"public_key_pem": pem, "key_sha256": public_key(pem)[1]}}
    (source / "official-update-channel.json").write_text(json.dumps(metadata))
    archive = payload / "host-bundle.tar"
    archive.write_bytes(b"signed-fixture-archive" * 1024)
    manifest = {"format": 1, "kind": "agentic-home-host-bundle", "publisher": PUBLISHER,
                "version": "0.12.0", "source_commit": "a" * 40,
                "bundle": {"url": "https://github.com/TED-H-com/agentic-smart-home/releases/download/v0.12.0/agentic-home-host-0.12.0.tar",
                           "sha256": module.file_digest(archive), "size": archive.stat().st_size},
                "compatibility": dict(COMPATIBILITY), "issued_at": time.time() - 10, "expires_at": time.time() + 3600,
                "tests": [{"name": "fixture", "result": "passed", "sha256": "b" * 64}]}
    # Host manifest compatibility omits Controller's additional executor field.
    host = {"manifest": manifest, "signature": base64.b64encode(private.sign(canonical(manifest))).decode()}
    (payload / "host-update.json").write_text(json.dumps(host))
    controller_manifest = {"format": 1, "publisher": PUBLISHER, "version": "0.12.0", "source_commit": "c" * 40,
        "image": IMAGE_REPOSITORY + "@sha256:" + "d" * 64, "image_id": "sha256:" + "e" * 64,
        "compatibility": {"schema": 3, "worker_contract": 1, "runner_protocol": 1,
                          "runtime_digest": digest({}), "ha_core_versions": ["2026.10.1"]},
        "tests": [{"name": "fixture", "result": "passed", "sha256": "f" * 64}],
        "issued_at": time.time() - 10, "expires_at": time.time() + 3600}
    controller = {"manifest": controller_manifest, "signature": base64.b64encode(private.sign(canonical(controller_manifest))).decode()}
    (payload / "controller-update.json").write_text(json.dumps(controller))
    (payload / "controller-images.tar").write_bytes(b"synthetic-cache")
    usb = tmp_path / "usb"
    (usb / DIRECTORY).mkdir(parents=True)
    files = {}
    for name in ("host-bundle.tar", "controller-images.tar"):
        data = (payload / name).read_bytes()
        (usb / DIRECTORY / name).write_bytes(data)
        files[name] = {"size": len(data), "sha256": module.file_digest(payload / name)}
    (payload / "media.json").write_text(json.dumps({"format": 1, "label": LABEL, "directory": DIRECTORY, "files": files}))
    monkeypatch.setattr(BootstrapMedia, "locate", lambda _: usb)
    monkeypatch.setattr(module, "installed_engine_identity", lambda: {"state": "absent", "digest": None})
    calls = []

    def run(*args, **kwargs):
        if args[0] == "openssl":
            return subprocess.run(args, capture_output=True, text=True, timeout=10)
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=controller_manifest["image_id"], stderr="")

    manager = SimpleNamespace(root=root, product=product, run=run, locked=nullcontext)
    tool = BootstrapOwner(manager, payload=payload, trust=tmp_path / "etc/publishers.json")
    tool.fixture = SimpleNamespace(private=private, metadata=metadata, host=host, controller=controller, calls=calls, usb=usb)
    try:
        yield tool
    finally:
        os.umask(previous_umask)


def test_root_review_verifies_both_actual_signatures_without_enrolling_key(owner):
    payload = owner._payload()
    assert payload.candidate == digest(owner.fixture.host["manifest"])
    assert payload.controller_candidate == digest(owner.fixture.controller["manifest"])
    assert payload.fingerprint == owner.fixture.metadata["pin"]["key_sha256"]
    assert not owner.trust.exists() and not owner.fixture.calls


@pytest.mark.parametrize("fault", ["null_pin", "host_signature", "archive", "controller_signature", "wrong_repository"])
def test_invalid_or_missing_authenticity_evidence_never_changes_trust(owner, fault):
    if fault == "null_pin":
        value = {**owner.fixture.metadata, "pin": None, "image_repository": None}
        (owner.payload / "product/host/official-update-channel.json").write_text(json.dumps(value))
    elif fault == "archive":
        (owner.fixture.usb / DIRECTORY / "host-bundle.tar").write_bytes(b"tampered")
    else:
        path = owner.payload / ("host-update.json" if fault == "host_signature" else "controller-update.json")
        value = json.loads(path.read_text())
        if fault == "wrong_repository":
            value["manifest"]["image"] = "ghcr.io/attacker/controller@sha256:" + "d" * 64
        else:
            value["signature"] = base64.b64encode(b"wrong" * 12 + b"bad!").decode()
        path.write_text(json.dumps(value))
    with pytest.raises((HostError, ValueError)):
        owner._payload()
    assert not owner.trust.exists() and not owner.fixture.calls


def summary(owner):
    return {"host_candidate": digest(owner.fixture.host["manifest"]), "host_version": "0.12.0", "current_tree": "a" * 64,
            "trust": {"exists": False, "sha256": None}, "key_sha256": owner.fixture.metadata["pin"]["key_sha256"],
            "controller_candidate": digest(owner.fixture.controller["manifest"]), "controller_version": "0.12.0",
            "controller_required": True, "controller_cache_sha256": module.file_digest(owner.payload / "controller-images.tar"),
            "current_path": str(owner.manager.product), "current_controller_image": "sha256:" + "d" * 64,
            "current_runtime_sha256": module.file_digest(owner.manager.root / "runtime.env"),
            "installed_engine": {"state": "absent", "digest": None}, "units": {}}


def test_review_is_exact_expiring_and_contains_no_private_key_or_capability(owner, monkeypatch):
    monkeypatch.setattr(owner, "_summary", lambda: summary(owner))
    result = owner.review()
    stored = json.loads(owner.path.read_text())
    assert stored["state"] == "offered" and 0 < stored["deadline"] - time.monotonic() <= 600
    assert result["review_digest"] == stored["review_digest"]
    assert "PRIVATE KEY" not in json.dumps(result) and not owner.trust.exists()


@pytest.mark.parametrize("fault", ["expired", "deadline", "boot", "used", "wrong_digest", "tree", "trust", "candidate", "cache", "api_unit"])
def test_approval_rechecks_expiry_session_boot_and_every_reviewed_revision(owner, monkeypatch, fault):
    current = summary(owner)
    monkeypatch.setattr(owner, "_summary", lambda: copy.deepcopy(current))
    review = owner.review()
    value = json.loads(owner.path.read_text())
    approval = review["review_digest"]
    if fault == "expired":
        value["expires_at"] = time.time() - 1
    elif fault == "deadline":
        value["deadline"] = time.monotonic() - 1
    elif fault == "boot":
        value["boot_id"] = "another-boot"
    elif fault == "used":
        value["state"] = "applying"
    elif fault == "wrong_digest":
        approval = "0" * 64
    elif fault == "api_unit":
        current["units"]["agentic-home-update-api.service"] = {"shipped": "c" * 64, "installed": None}
    else:
        key = {"tree": "current_tree", "trust": "trust", "candidate": "host_candidate", "cache": "controller_cache_sha256"}[fault]
        current[key] = "changed"
    owner.path.write_text(json.dumps(value))
    with pytest.raises(HostError):
        owner.apply(approval)
    assert not owner.fixture.calls and not owner.trust.exists()


@pytest.mark.parametrize("state", ["applying", "needs_reconcile", "complete"])
def test_lost_or_interrupted_bootstrap_cannot_be_reoffered_or_repeated(owner, monkeypatch, state):
    owner.path.write_text(json.dumps({"state": state}))
    monkeypatch.setattr(owner, "_summary", lambda: pytest.fail("must not generate a new approval"))
    with pytest.raises(HostError, match="不會重送"):
        owner.review()


@pytest.mark.parametrize("engine_state", ["absent", "inactive"])
def test_apply_orders_capacity_restore_trust_engine_controller_and_host_without_duplicate_snapshot(owner, monkeypatch, engine_state):
    reviewed = summary(owner)
    reviewed["installed_engine"] = {"state": engine_state, "digest": "e" * 64 if engine_state == "inactive" else None}
    monkeypatch.setattr(module, "installed_engine_identity", lambda: reviewed["installed_engine"])
    monkeypatch.setattr(owner, "_summary", lambda: copy.deepcopy(reviewed))
    payload = owner._payload()
    monkeypatch.setattr(owner, "_payload", lambda: payload)
    events = []
    state = {"locked": False, "broker": True}

    @contextmanager
    def manager_lock():
        assert not state["locked"]
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    owner.manager.locked = manager_lock
    original_run = owner.manager.run

    def broker_run(*args, **kwargs):
        if args[:2] == ("systemctl", "stop"):
            assert state["locked"]
            state["broker"] = False
        if args[:2] == ("systemctl", "start"):
            state["broker"] = True
        if args == ("systemctl", "enable", "--now", "agentic-home-update-api.service"):
            assert state["broker"] and not state["locked"]
            events.append(("update_api",))
        return original_run(*args, **kwargs)

    owner.manager.run = broker_run

    class Restore:
        def __init__(self, manager, *, save):
            self.save = save

        def capacity(self, required):
            events.append(("capacity", required))

        def create(self, record):
            if record.get("restore_point"):
                return record["restore_point"]
            assert state["locked"]
            events.append(("restore",))
            record["restore_point"] = {"phase": "complete"}
            self.save(record)
            return record["restore_point"]

    class Engine:
        def __init__(self, manager, **kwargs):
            self.restore = kwargs.get("restore")

        def stage(self, envelope, **kwargs):
            events.append(("host_stage",))
            return {"state": "staged", "candidate_digest": reviewed["host_candidate"], "approval_id": "x" * 43}

        def _approval(self, body):
            events.append(("host_approved",))

        def execute(self):
            assert state["broker"] and not state["locked"], "Host engine preflight needs the original broker running"
            self.restore.create({})
            events.append(("host_execute",))
            return {"state": "deployed"}

    monkeypatch.setattr(module, "HostRestore", Restore)
    monkeypatch.setattr(module, "HostBundle", Engine)
    monkeypatch.setattr(module, "enroll", lambda *args, **kwargs: events.append(("trust",)))
    def convert(event):
        assert state["locked"]
        events.append((event,))
    def install(*args, **kwargs):
        assert kwargs == ({"expected_inactive_engine_digest": "e" * 64} if engine_state == "inactive" else {})
        convert("engine")
    monkeypatch.setattr(module, "install_engine", install)
    monkeypatch.setattr(module, "version_layout", lambda *args: convert("layout"))
    monkeypatch.setattr(owner, "_controller", lambda *args: events.append(("controller",)))
    monkeypatch.setattr(owner, "_pause_existing", lambda _: events.append(("pause",)))
    monkeypatch.setattr(owner, "_baseline_current", lambda _: None)
    Engine.broker_probe = lambda self: {"status": {"vm": "running"}, "bridge": {"controller_update": {"ingress_bridge_contract": 1}}}
    review = owner.review()
    assert owner.apply(review["review_digest"])["state"] == "complete"
    names = [item[0] for item in events]
    assert names == ["capacity", "pause", "restore", "trust", "engine", "layout", "update_api", "controller", "host_stage", "host_approved", "host_execute"]
    assert events[0][1] == payload.cache_size * 5 + payload.archive_size + 256 * 1024**2
    assert len([item for item in events if item[0] == "restore"]) == 1
    assert ("systemctl", "start", "agentic-home-host.service") in owner.fixture.calls
    with pytest.raises(HostError):
        owner.apply(review["review_digest"])


def test_failed_or_uncertain_broker_stop_still_attempts_original_broker_restart(owner, monkeypatch):
    monkeypatch.setattr(owner, "_summary", lambda: summary(owner))
    monkeypatch.setattr(module, "HostRestore", lambda *args, **kw: SimpleNamespace(capacity=lambda _: None, create=lambda _: {}))
    monkeypatch.setattr(owner, "_pause_existing", lambda _: None)
    monkeypatch.setattr(owner, "_baseline_current", lambda _: None)
    monkeypatch.setattr(module.HostBundle, "_broker_probe", staticmethod(lambda: {"status": {"vm": "running"}, "bridge": {"controller_update": {"ingress_bridge_contract": 1}}}))
    run = owner.manager.run

    def uncertain(*args, **kwargs):
        if args[:2] == ("systemctl", "stop"):
            raise HostError("synthetic lost stop reply")
        return run(*args, **kwargs)

    monkeypatch.setattr(owner.manager, "run", uncertain)
    review = owner.review()
    with pytest.raises(HostError, match="synthetic lost"):
        owner.apply(review["review_digest"])
    assert json.loads(owner.path.read_text())["state"] == "needs_reconcile"
    assert ("systemctl", "start", "agentic-home-host.service") in owner.fixture.calls


@pytest.mark.parametrize("bad_protocol", [False, True])
def test_bundled_controller_prepare_uses_only_verified_local_image_and_never_network_pull(owner, monkeypatch, bad_protocol):
    payload = owner._payload()
    payload.cache = owner.payload / "controller-images.tar"
    calls = owner.fixture.calls

    class Service:
        def __init__(self, manager):
            self.records = {}

        def verify(self, *args):
            pass

        def update(self, record):
            self.records[record["candidate_digest"]] = record

        def prepare(self, record):
            reference = record["manifest"]["image"]
            self.run("docker", "pull", reference)
            self.run("docker", "image", "inspect", reference)
            record.update(state="staged", approval_id="x" * 43)

        def apply(self, body):
            self.records[body["candidate_digest"]]["state"] = "deployed"

    monkeypatch.setattr(module, "Updater", Service)
    protocol = {**COMPATIBILITY, "host_protocol": True} if bad_protocol else COMPATIBILITY
    monkeypatch.setattr(module, "HostBundle", lambda *args: SimpleNamespace(controller_probe=lambda: {"status": "ok", "host_update": protocol}))
    if bad_protocol:
        with pytest.raises(HostError, match="安全主機更新能力"):
            owner._controller(payload)
    else:
        owner._controller(payload)
    assert calls[0] == ("docker", "load", "--input", str(payload.cache))
    assert not any(call[:2] == ("docker", "pull") for call in calls)
    assert all(payload.controller["manifest"]["image"] not in call for call in calls)


def builder_module():
    spec = importlib.util.spec_from_file_location("fixture_bootstrap_builder", ROOT / "scripts/build_update_bootstrap.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_unsigned_builder_stages_only_public_fixed_inputs_and_gui_with_isolated_python(owner, tmp_path):
    build = builder_module()
    result = build.build(owner.payload / "product", owner.payload / "host-update.json", owner.payload / "host-bundle.tar",
                         owner.payload / "controller-update.json", owner.payload / "controller-images.tar", tmp_path / "output", package=False)
    stage = Path(result["stage"])
    assert result["unsigned_package"] is None and result["authentication"] == "external_authenticated_distribution_required"
    wrapper = (stage / "usr/bin/agentic-home-update-bootstrap").read_text()
    assert "/usr/bin/python3 -I -c" in wrapper
    assert "install.py" not in wrapper and "sudo" not in wrapper
    assert (stage / "DEBIAN/postinst").read_text() == module.POSTINST
    assert (stage / "DEBIAN/preinst").read_text() == module.PREINST
    assert "--apply" not in module.POSTINST and "--review" not in module.POSTINST
    assert (stage / "etc/systemd/system" / module.RECOVERY_NAME).read_text() == module.RECOVERY_UNIT
    for name in module.ENGINE_UNITS:
        assert (stage / "usr/share/agentic-home-update-bootstrap/product/host/systemd" / name).is_file()
    assert (stage / "usr/share/applications/agentic-home-update-bootstrap.desktop").is_file()
    assert not (stage / "usr/share/agentic-home-update-bootstrap/host-bundle.tar").exists()
    assert not (stage / "usr/share/agentic-home-update-bootstrap/controller-images.tar").exists()
    assert (Path(result["media"]) / "host-bundle.tar").is_file()
    assert (Path(result["media"]) / "controller-images.tar").is_file()
    assert "UNSIGNED" in (stage / "usr/share/agentic-home-update-bootstrap/AUTHENTICATION-REQUIRED.txt").read_text()


def test_unsigned_builder_refuses_missing_production_pin_and_bad_signed_envelope(owner, tmp_path):
    build = builder_module()
    descriptor = owner.payload / "product/host/official-update-channel.json"
    descriptor.write_text(json.dumps({**owner.fixture.metadata, "pin": None, "image_repository": None}))
    with pytest.raises(ValueError, match="real reviewed"):
        build.build(owner.payload / "product", owner.payload / "host-update.json", owner.payload / "host-bundle.tar",
                    owner.payload / "controller-update.json", owner.payload / "controller-images.tar", tmp_path / "output", package=False)
    assert not (tmp_path / "output").exists()


def test_gui_only_dispatches_fixed_pkexec_tool_after_explicit_owner_confirmation(monkeypatch):
    calls = []
    review = {"message": "fixture review", "host_version": "0.12.0", "controller_required": False,
              "key_sha256": "a" * 64, "review_digest": "b" * 64}

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(review), stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    progress = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kwargs: (
        progress.append(args) or SimpleNamespace(stdin=SimpleNamespace(close=lambda: progress.append("closed")), wait=lambda **kwargs: None)))
    module.gui()
    assert calls[0] == ["/usr/bin/pkexec", module.TOOL, "--review"]
    assert calls[1][:2] == ["/usr/bin/zenity", "--question"]
    assert calls[2] == ["/usr/bin/pkexec", module.TOOL, "--apply", "b" * 64]
    assert progress[0][:5] == ["/usr/bin/zenity", "--progress", "--pulsate", "--no-cancel", "--auto-close"]
    assert progress[-1] == "closed"


def test_gui_cancel_never_dispatches_privileged_apply(monkeypatch):
    calls = []
    review = {"message": "fixture", "host_version": "0.12.0", "controller_required": False, "key_sha256": "a" * 64}

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if args[0] == "/usr/bin/zenity" else 0, stdout=json.dumps(review), stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    module.gui()
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["stop", "active_work"])
def test_pause_preflight_failure_restarts_and_probes_exact_original_controller(owner, monkeypatch, failure):
    calls = []
    image = "sha256:" + "d" * 64

    def stop():
        calls.append("stop")
        if failure == "stop":
            raise HostError("synthetic stop reply lost")

    def check(*args, **kwargs):
        calls.append("idle_check")
        assert kwargs == {"pause": True}
        raise HostError("synthetic active work")

    service = SimpleNamespace(image=lambda: image, stop=stop, data=owner.manager.root.parent / "controller",
                              activate=lambda actual: calls.append(("activate", actual)), health=lambda: calls.append("health"))
    monkeypatch.setattr(owner, "_controller_view", lambda: service)
    monkeypatch.setattr(owner, "_baseline_current", lambda _: None)
    monkeypatch.setattr(module, "offline_check", check)
    with pytest.raises(HostError):
        owner._pause_existing({"state": "applying", "summary": summary(owner)})
    assert calls[-2:] == [("activate", image), "health"]


def test_capacity_failure_before_snapshot_intent_allows_fresh_owner_review(owner, monkeypatch):
    monkeypatch.setattr(owner, "_summary", lambda: summary(owner))

    def capacity(_):
        raise HostError("synthetic no space")

    monkeypatch.setattr(module, "HostRestore", lambda *args, **kwargs: SimpleNamespace(capacity=capacity))
    review = owner.review()
    with pytest.raises(HostError, match="no space"):
        owner.apply(review["review_digest"])
    assert json.loads(owner.path.read_text())["state"] == "failed"
    assert owner.review()["review_digest"] != review["review_digest"]
    assert not owner.fixture.calls


@pytest.mark.parametrize("state", ["running", "needs_reconcile"])
def test_active_or_uncertain_host_jobs_block_local_oci_cache_load(owner, state):
    (owner.manager.root / "jobs.json").write_text(json.dumps({"existing": {"state": state}}))
    with pytest.raises(HostError, match="未確認"):
        owner._controller(owner._payload())
    assert not owner.fixture.calls


@pytest.mark.parametrize("phase", ["ha_shutdown_requested", "ha_snapshot_requested", "ha_snapshot_complete"])
def test_bootstrap_recovery_restores_only_approved_interrupted_ha_availability(owner, monkeypatch, phase):
    attempt = {"candidate_digest": "a" * 64, "restore_point": {"phase": phase, "ha_original_state": "running"}}
    owner.path.write_text(json.dumps({"state": "applying", "restore_attempt": attempt}))
    calls = []

    class Recovery:
        def __init__(self, manager, *, save):
            self.save = save

        def recover_ha(self, record):
            assert record == attempt
            calls.append(record["restore_point"]["phase"])
            record["restore_point"]["ha_recovery"] = "running_verified"
            self.save(record)
            return {"state": "running_verified", "vm": "running"}

        def create(self, _):
            pytest.fail("availability recovery must never create a new backup")

    monkeypatch.setattr(module, "HostRestore", Recovery)
    assert owner.recover_availability()["state"] == "availability_restored"
    value = json.loads(owner.path.read_text())
    assert value["state"] == "needs_reconcile" and value["restore_attempt"]["restore_point"]["phase"] == phase
    assert calls == [phase] and not owner.trust.exists() and not owner.fixture.calls


@pytest.mark.parametrize("state,phase,original", [
    ("offered", "ha_snapshot_requested", "running"), ("complete", "ha_snapshot_requested", "running"),
    ("applying", "complete", "running"), ("applying", "ha_complete", "running"),
    ("applying", "preparing", "running"), ("applying", "ha_snapshot_requested", "shut off")])
def test_bootstrap_recovery_never_restarts_unapproved_completed_or_owner_stopped_guest(owner, monkeypatch, state, phase, original):
    value = {"state": state, "restore_attempt": {"restore_point": {"phase": phase, "ha_original_state": original}}}
    owner.path.write_text(json.dumps(value))
    monkeypatch.setattr(module, "HostRestore", lambda *args, **kw: pytest.fail("no guest effect authorized"))
    assert owner.recover_availability() == {"state": "not_needed"}
    assert json.loads(owner.path.read_text()) == value and not owner.fixture.calls


def test_reopening_gui_compensates_availability_but_does_not_offer_another_update(owner, monkeypatch):
    owner.path.write_text(json.dumps({"state": "applying", "restore_attempt": {"restore_point": {
        "phase": "ha_snapshot_requested", "ha_original_state": "running"}}}))
    calls = []

    def recover(record):
        calls.append("recover_availability")
        return {"state": "running_verified"}

    monkeypatch.setattr(module, "HostRestore", lambda *args, **kw: SimpleNamespace(recover_ha=recover))
    monkeypatch.setattr(owner, "_summary", lambda: pytest.fail("cannot mint another approval for uncertain snapshot"))
    with pytest.raises(HostError, match="已恢復先前運行"):
        owner.review()
    assert calls == ["recover_availability"]
    assert json.loads(owner.path.read_text())["state"] == "needs_reconcile"


@pytest.mark.parametrize("service_name", ["controller", "broker"])
@pytest.mark.parametrize("intent_state", ["stop_requested", "start_requested"])
def test_crash_after_approved_service_stop_recovers_exact_baseline_only(owner, monkeypatch, service_name, intent_state):
    calls = []
    image = "sha256:" + "d" * 64
    intent = {"state": intent_state, "image": image, "runtime_sha256": [module.file_digest(owner.manager.root / "runtime.env")]}
    value = {"state": "applying", "summary": summary(owner), "availability": {service_name: intent}}
    owner.path.write_text(json.dumps(value))
    monkeypatch.setattr(owner, "_baseline_current", lambda record: calls.append("baseline_verified"))
    monkeypatch.setattr(owner, "_controller_view", lambda: SimpleNamespace(image=lambda: image,
                        activate=lambda actual: calls.append(("activate", actual)), health=lambda: calls.append("health")))
    monkeypatch.setattr(module.HostBundle, "_broker_probe", staticmethod(lambda: {
        "status": {"vm": "running"}, "bridge": {"controller_update": {"ingress_bridge_contract": 1}}})
    )
    monkeypatch.setattr(module, "HostRestore", lambda *args, **kwargs: pytest.fail("no backup or HA mutation needed"))
    assert owner.recover_availability()["state"] == "availability_restored"
    persisted = json.loads(owner.path.read_text())
    assert persisted["availability"][service_name]["state"] == "running_verified"
    assert persisted["state"] == "needs_reconcile"
    assert calls[0] == "baseline_verified"
    if service_name == "controller":
        assert calls[-2:] == [("activate", image), "health"]
        assert not owner.fixture.calls
    else:
        assert owner.fixture.calls == [("systemctl", "start", "agentic-home-host.service"),
                                       ("systemctl", "is-active", "--quiet", "agentic-home-host.service")]
    assert owner.recover_availability() == {"state": "not_needed"}


@pytest.mark.parametrize("drift", ["runtime", "image", "product"])
def test_availability_recovery_never_rolls_back_owner_changes_or_completed_candidate(owner, monkeypatch, drift):
    image = "sha256:" + "d" * 64
    value = {"state": "applying", "summary": summary(owner), "availability": {"controller": {
        "state": "stop_requested", "image": image, "runtime_sha256": [module.file_digest(owner.manager.root / "runtime.env")]}}}
    owner.path.write_text(json.dumps(value))
    if drift == "product":
        monkeypatch.setattr(module.HostBundle, "_tree_identity", lambda self, path: "changed_candidate")
    else:
        monkeypatch.setattr(owner, "_baseline_current", lambda _: None)
    if drift == "runtime":
        (owner.manager.root / "runtime.env").write_text("# owner changed configuration\n")
    monkeypatch.setattr(owner, "_controller_view", lambda: SimpleNamespace(
        image=lambda: "sha256:" + "e" * 64 if drift == "image" else image,
        activate=lambda actual: pytest.fail("must preserve changed owner/candidate version")))
    with pytest.raises(HostError):
        owner.recover_availability()
    assert json.loads(owner.path.read_text())["state"] == "needs_reconcile"
    assert not owner.fixture.calls


def test_controller_stop_intent_is_durable_before_stop_even_when_process_disappears(owner, monkeypatch):
    class ProcessKilled(BaseException):
        pass
    image = "sha256:" + "d" * 64
    value = {"state": "applying", "summary": summary(owner)}
    def stop():
        saved = json.loads(owner.path.read_text())
        assert saved["availability"]["controller"]["state"] == "stop_requested"
        assert saved["availability"]["controller"]["image"] == image
        raise ProcessKilled()
    monkeypatch.setattr(owner, "_controller_view", lambda: SimpleNamespace(image=lambda: image, stop=stop,
                        data=owner.manager.root.parent / "controller"))
    # SIGKILL skips finally entirely; leave the durable stop intent untouched.
    monkeypatch.setattr(owner, "_resume_controller", lambda _: None)
    with pytest.raises(ProcessKilled):
        owner._pause_existing(value)
    assert json.loads(owner.path.read_text())["availability"]["controller"]["state"] == "stop_requested"


@pytest.mark.parametrize("phase", ["review", "apply"])
def test_gui_timeout_reports_unknown_state_and_closes_progress(monkeypatch, phase):
    calls, progress = [], []
    review = {"message": "fixture", "host_version": "0.12.0", "controller_required": False,
              "key_sha256": "a" * 64, "review_digest": "b" * 64}
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "/usr/bin/pkexec" and args[2] == "--" + phase:
            raise module.subprocess.TimeoutExpired(args, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=json.dumps(review), stderr="")
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kwargs: SimpleNamespace(
        stdin=SimpleNamespace(close=lambda: progress.append("closed")), wait=lambda **kwargs: None))
    module.gui()
    assert "尚未確認" in calls[-1][-1]
    if phase == "apply":
        assert progress == ["closed"]
        assert "請勿重送" in calls[-1][-1]
