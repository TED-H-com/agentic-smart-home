"""One-time owner GUI for an externally authenticated, fixed local bootstrap package.

Checksums and root ownership do not authenticate the initial package. Install it
only through an independently authenticated OS/media distribution channel. No
browser, model, download URL, private key, or caller-selected path is accepted.
"""
import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .bootstrap import write_environment
from .bootstrap_media import INSERT_USB, PREINST, BootstrapMedia
from .host_restore import HostRestore
from .host_update import (
    COMPATIBILITY,
    IMAGE_REPOSITORY,
    PUBLISHER,
    HostBundle,
    digest,
    exact_protocol,
    read_json,
    secure,
)
from .manager import HostError, Manager, atomic
from .publisher import TRUST, enroll, inspect, public_key, store_digest
from .update_bootstrap import (
    BUNDLES,
    install_engine,
    installed_engine_identity,
    tree_identity,
    trusted,
    version_layout,
)
from .updater import Updater, offline_check

PAYLOAD = Path("/usr/share/agentic-home-update-bootstrap")
TOOL = "/usr/bin/agentic-home-update-bootstrap"
SECONDS = 600
MAX_CACHE = 16 * 1024**3
MISSING_PIN = "安裝包未附正式官方驗證金鑰，無法進行更新；請取得受信任的正式安裝包。"
RECOVERED = "已恢復先前運行的管理與家庭服務；先前主機更新結果尚未確認，不會重送更新。請聯絡支援核對。"
RECOVERY_NAME = "agentic-home-update-bootstrap-recover.service"
ENGINE_UNITS = ("agentic-home-update.service", "agentic-home-update-recover.service", "agentic-home-update-api.service")
RECOVERY_UNIT = ("[Unit]\nDescription=Recover approved Agentic Home bootstrap guest availability\n"
                 "After=libvirtd.service agentic-home-host.service\nWants=libvirtd.service\n\n[Service]\n"
                 "Type=oneshot\nExecStart=/usr/bin/agentic-home-update-bootstrap --recover-availability\n"
                 "TimeoutStartSec=240\nUMask=0077\nProtectHome=true\nNoNewPrivileges=true\n\n[Install]\nWantedBy=multi-user.target\n")
POSTINST = ("#!/bin/sh\nset -eu\nif [ \"$1\" = configure ]; then\n"
            "  if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi\n"
            "  systemctl --no-reload enable agentic-home-update-bootstrap-recover.service\nfi\n")


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


class BootstrapOwner:
    def __init__(self, manager, *, payload=PAYLOAD, trust=TRUST):
        self.manager, self.payload, self.trust = manager, Path(payload), Path(trust)
        self.path = manager.root / "owner-update-bootstrap.json"

    def _trust(self):
        secure(self.trust, optional=True)
        if not self.trust.exists():
            return {"exists": False, "sha256": None}, {"format": 1, "publishers": {}}
        value = read_json(self.trust)
        if (not isinstance(value, dict) or set(value) != {"format", "publishers"}
                or type(value["format"]) is not int or value["format"] != 1 or not isinstance(value["publishers"], dict)):
            raise HostError("更新來源設定無法安全核對，尚未變更主機。")
        return {"exists": True, "sha256": file_digest(self.trust)}, value

    def _payload(self):
        source = self.payload / "product"
        trusted(source, directory=True)
        metadata = read_json(source / "host/official-update-channel.json", 16384)
        # Use the same fixed Host verifier, with a temporary copy of the shipped
        # pin, before enrolling anything in the system trust store.
        pin = metadata.get("pin") if isinstance(metadata, dict) else None
        if not pin:
            raise HostError(MISSING_PIN)
        pem, fingerprint = public_key(pin.get("public_key_pem"))
        host = read_json(self.payload / "host-update.json")
        media = BootstrapMedia(self.manager, self.payload / "media.json")
        with tempfile.TemporaryDirectory(prefix="owner-bootstrap-", dir=self.manager.root) as temporary:
            trust = Path(temporary) / "publishers.json"
            atomic(trust, {"format": 1, "publishers": {PUBLISHER: {"public_key_pem": pem, "image_repository": IMAGE_REPOSITORY}}})
            verifier = HostBundle(self.manager, authority=source / "host/official-update-channel.json", trust=trust)
            candidate, verified = verifier.verify_envelope(host)
            expected = media.manifest["files"]["host-bundle.tar"]
            if verified != fingerprint or expected["size"] != host["manifest"]["bundle"]["size"] or expected["sha256"] != host["manifest"]["bundle"]["sha256"]:
                raise HostError("主機更新檔案與官方簽章不符，尚未變更主機。")
        controller_path = self.payload / "controller-update.json"
        controller = read_json(controller_path) if controller_path.exists() else None
        cache, cache_hash, controller_candidate = None, None, None
        if controller is not None:
            if not isinstance(controller, dict) or set(controller) != {"manifest", "signature"}:
                raise HostError("管理服務更新說明檔無效。")
            manifest = controller["manifest"]
            if manifest.get("publisher") != PUBLISHER or manifest.get("image", "").split("@", 1)[0] != IMAGE_REPOSITORY:
                raise HostError("管理服務更新來源與官方金鑰不符。")
            verifier = object.__new__(Updater)
            verifier.manager, verifier.run = self.manager, self.manager.run
            verifier.root, verifier.environment = self.manager.root, self.manager.root / "runtime.env"
            verifier.publishers = lambda: {PUBLISHER: {"public_key_pem": pem, "image_repository": IMAGE_REPOSITORY}}
            verifier.verify(manifest, controller["signature"])
            controller_candidate = digest(manifest)
            cache_hash = media.manifest["files"]["controller-images.tar"]["sha256"]
        # The root-installed manifest authenticates the review. Hash bulky USB
        # bytes once, during the approved private copy, before any parser runs.
        media.inspect(verify=False)
        return SimpleNamespace(source=source, pem=pem, fingerprint=fingerprint, host=host, candidate=candidate,
                               archive=None, controller=controller, controller_candidate=controller_candidate,
                               cache=cache, cache_hash=cache_hash, media=media,
                               archive_size=media.manifest["files"]["host-bundle.tar"]["size"],
                               cache_size=media.manifest["files"]["controller-images.tar"]["size"])

    def _summary(self):
        payload = self._payload()
        trust_identity, trust = self._trust()
        previous = trust["publishers"].get(PUBLISHER)
        if previous is not None and (not isinstance(previous, dict) or set(previous) != {"public_key_pem", "image_repository"}
                or previous["image_repository"] != IMAGE_REPOSITORY or public_key(previous["public_key_pem"])[1] != payload.fingerprint):
            raise HostError("目前更新金鑰與安裝包不同；系統不會自行替換，請聯絡支援核對。")
        current = self.manager.product
        if current.is_symlink():
            current = current.resolve(strict=True)
        trusted(current, directory=True)
        view = HostBundle(self.manager)
        health = view.controller_probe()
        needs_controller = not isinstance(health, dict) or health.get("status") != "ok" or not exact_protocol(health.get("host_update"), COMPATIBILITY)
        if needs_controller and payload.controller is None:
            raise HostError("此主機需要一併更新管理服務，但安裝包未附相容的官方版本；尚未變更主機。")
        engine, _, _ = tree_identity(payload.source)
        recovery = Path("/etc/systemd/system") / RECOVERY_NAME
        postinst = Path("/var/lib/dpkg/info/agentic-home-update-bootstrap.postinst")
        preinst = Path("/var/lib/dpkg/info/agentic-home-update-bootstrap.preinst")
        if (file_digest(trusted(recovery)) != hashlib.sha256(RECOVERY_UNIT.encode()).hexdigest()
                or file_digest(trusted(postinst)) != hashlib.sha256(POSTINST.encode()).hexdigest()
                or file_digest(trusted(preinst)) != hashlib.sha256(PREINST.encode()).hexdigest()):
            raise HostError("更新復原安裝包無法安全核對，尚未變更主機。")
        runtime = trusted(self.manager.root / "runtime.env")
        units = {}
        for name in ENGINE_UNITS:
            source = trusted(payload.source / "host/systemd" / name)
            target = Path("/etc/systemd/system") / name
            secure(target, optional=True)
            units[name] = {"shipped": file_digest(source), "installed": file_digest(target) if target.exists() else None}
            if units[name]["installed"] not in {None, units[name]["shipped"]}:
                raise HostError("更新復原服務已被修改，尚未變更主機。")
        return {"format": 1, "publisher": PUBLISHER, "key_sha256": payload.fingerprint,
                "host_candidate": payload.candidate, "host_version": payload.host["manifest"]["version"],
                "controller_candidate": payload.controller_candidate if needs_controller else None,
                "controller_version": payload.controller["manifest"]["version"] if needs_controller else None,
                "controller_required": needs_controller, "controller_cache_sha256": payload.cache_hash,
                "trust": trust_identity, "current_tree": view._tree_identity(current), "current_path": str(current),
                "current_controller_image": self._controller_view().image(),
                "current_runtime_sha256": file_digest(runtime),
                "availability_recovery": {"unit_sha256": file_digest(recovery), "postinst_sha256": file_digest(postinst),
                                          "preinst_sha256": file_digest(preinst),
                                          "scope": "only restore approved interrupted baseline service availability; never repeat backup or update"},
                "media_manifest_sha256": file_digest(self.payload / "media.json"),
                "installed_engine": installed_engine_identity(), "engine": engine, "units": units,
                "scope": "pause household AI, exact official key enrollment, restore points, fixed Host updater and Host bundle; same-schema Controller prerequisite if listed"}

    @contextmanager
    def locked(self):
        path = self.manager.root / "owner-update-bootstrap.lock"
        secure(path, optional=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HostError("主機更新正在處理，請勿重複操作。") from None
            yield

    def review(self):
        with self.locked():
            if self.path.exists():
                existing = read_json(self.path)
                if existing.get("state") in {"applying", "needs_reconcile"}:
                    if self._recover_availability(existing)["state"] == "availability_restored":
                        raise HostError(RECOVERED)
                    raise HostError("先前主機更新已開始或結果尚未確認，不會重送；請查看管理頁或聯絡支援。")
                if existing.get("state") == "complete":
                    raise HostError("先前主機更新已完成，不會重送；請查看管理頁。")
            return self._review()

    def _recover_availability(self, value):
        if value.get("state") not in {"applying", "needs_reconcile"}:
            return {"state": "not_needed"}
        attempt = value.get("restore_attempt")
        journal = attempt.get("restore_point") if isinstance(attempt, dict) else None
        recover_ha = (isinstance(journal, dict) and journal.get("phase") in {
            "ha_shutdown_requested", "ha_snapshot_requested", "ha_snapshot_complete"} and journal.get("ha_original_state") == "running")
        availability = value.get("availability", {})
        recover_service = any(isinstance(item, dict) and item.get("state") in {"stop_requested", "start_requested"}
                              for item in availability.values())
        if not recover_ha and not recover_service:
            return {"state": "not_needed"}
        def save(item):
            value["restore_attempt"] = item
            self._save(value)
        with self.manager.locked():
            try:
                self._resume_broker(value)
                self._resume_controller(value)
                if recover_ha:
                    receipt = HostRestore(self.manager, save=save).recover_ha(attempt)
                    if receipt.get("state") != "running_verified":
                        raise HostError("尚未確認家庭系統已重新啟動。")
            finally:
                value["state"] = "needs_reconcile"
                self._save(value)
        return {"state": "availability_restored", "message": RECOVERED}

    def recover_availability(self):
        with self.locked():
            if not self.path.exists():
                return {"state": "not_needed"}
            return self._recover_availability(read_json(self.path))

    def _review(self):
        summary = self._summary()
        value = {"summary": summary, "expires_at": time.time() + SECONDS, "nonce": secrets.token_hex(32), "state": "offered",
                 "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "deadline": time.monotonic() + SECONDS}
        value["review_digest"] = digest(value)
        secure(self.path, optional=True)
        secure(self.path.with_name(self.path.name + ".new"), optional=True)
        atomic(self.path, value)
        return {**summary, "review_digest": value["review_digest"], "expires_at": value["expires_at"],
                "message": "此步驟會暫停家庭 AI、信任上列官方金鑰、建立還原點並更新主機。家庭系統會短暫重新啟動；不會還原舊家庭資料。"}

    def _save(self, value):
        secure(self.path.with_name(self.path.name + ".new"), optional=True)
        atomic(self.path, value)

    def _controller_view(self):
        service = object.__new__(Updater)
        service.manager, service.run = self.manager, self.manager.run
        service.environment = self.manager.root / "runtime.env"
        service.data = self.manager.root.parent / "controller"
        return service

    def _updates_idle(self):
        jobs = self.manager.root / "jobs.json"
        if jobs.exists():
            values = read_json(jobs, 4 * 1024**2)
            if not isinstance(values, dict) or any(not isinstance(item, dict) or item.get("state") not in {
                    "succeeded", "failed"} for item in values.values()):
                raise HostError("主機尚有進行中或未確認的作業，已停止更新。")
        path = self.manager.root / "updates/records.json"
        if path.exists():
            records = read_json(path, 4 * 1024**2)
            if not isinstance(records, dict) or any(not isinstance(item, dict) or item.get("state") in {
                    "staging", "deploying", "rolling_back", "needs_reconcile"} for item in records.values()):
                raise HostError("管理服務有尚未完成的更新作業，已停止主機更新。")

    def _baseline_current(self, value):
        current = self.manager.product.resolve(strict=True)
        trusted(current, directory=True)
        allowed = {Path(value["summary"]["current_path"])}
        layout = self.manager.root / "host-update-bootstrap.json"
        if layout.exists():
            record = read_json(layout)
            name = record.get("baseline")
            import re
            if not isinstance(name, str) or not re.fullmatch(r"bootstrap-[a-f0-9]{32}", name):
                raise HostError("無法確認原有主機程式，未重啟服務。")
            allowed.add(BUNDLES / name)
        if current not in allowed or HostBundle(self.manager)._tree_identity(current) != value["summary"]["current_tree"]:
            raise HostError("主機程式已變更，未以舊版本重啟服務；請聯絡支援。")

    def _resume_controller(self, value):
        intent = value.get("availability", {}).get("controller")
        if not isinstance(intent, dict) or intent.get("state") not in {"stop_requested", "start_requested"}:
            return
        self._baseline_current(value)
        current = file_digest(trusted(self.manager.root / "runtime.env"))
        if current not in intent["runtime_sha256"]:
            raise HostError("管理服務設定已變更，未套用舊設定；請聯絡支援。")
        service = self._controller_view()
        if service.image() != intent["image"]:
            raise HostError("管理服務版本已變更，未切回舊版本；請聯絡支援。")
        intent["state"] = "start_requested"
        self._save(value)
        service.activate(intent["image"])
        if service.image() != intent["image"]:
            raise HostError("尚未確認原有管理服務已重新啟動。")
        service.health()
        intent["state"] = "running_verified"
        self._save(value)

    def _resume_broker(self, value):
        intent = value.get("availability", {}).get("broker")
        if not isinstance(intent, dict) or intent.get("state") not in {"stop_requested", "start_requested"}:
            return
        self._baseline_current(value)
        intent["state"] = "start_requested"
        self._save(value)
        self.manager.run("systemctl", "start", "agentic-home-host.service", timeout=90)
        self.manager.run("systemctl", "is-active", "--quiet", "agentic-home-host.service", timeout=15)
        proof = HostBundle(self.manager).broker_probe()
        if (not isinstance(proof, dict) or not isinstance(proof.get("status"), dict)
                or "vm" not in proof["status"] or not isinstance(proof.get("bridge"), dict)
                or not exact_protocol(proof["bridge"].get("controller_update"), {"ingress_bridge_contract": 1})):
            raise HostError("尚未確認主機管理服務已恢復，請聯絡支援。")
        intent["state"] = "running_verified"
        self._save(value)

    def _pause_existing(self, value):
        self._updates_idle()
        service = self._controller_view()
        with self.manager.locked():
            baseline = service.image()
            original = trusted(self.manager.root / "runtime.env")
            if baseline != value["summary"]["current_controller_image"] or file_digest(original) != value["summary"]["current_runtime_sha256"]:
                raise HostError("管理服務版本或設定已變更，尚未停止服務；請重新核對。")
            with tempfile.TemporaryDirectory(prefix="bootstrap-runtime-", dir=self.manager.root) as temporary:
                target = Path(temporary) / "runtime.env"
                target.write_bytes(original.read_bytes())
                write_environment(target, baseline, "AGENTIC_CONTROLLER_IMAGE")
                after = file_digest(target)
            value.setdefault("availability", {})["controller"] = {
                "state": "stop_requested", "image": baseline, "runtime_sha256": [file_digest(original), after]}
            value["phase"] = "controller_stop_requested"
            self._save(value)
            try:
                service.stop()
                offline_check(service.data / "controller.sqlite3", 3, pause=True)
            finally:
                # A stop can succeed while its reply is lost. Never leave the
                # original UI stopped merely because a preflight was rejected.
                self._resume_controller(value)

    def _controller(self, payload):
        self._updates_idle()
        manifest = payload.controller["manifest"]
        image_id, image_ref = manifest["image_id"], manifest["image"]
        self.manager.run("docker", "load", "--input", str(payload.cache), timeout=900)
        if self.manager.run("docker", "image", "inspect", "--format", "{{.Id}}", image_id, timeout=30).stdout.strip() != image_id:
            raise HostError("管理服務映像檔與簽署版本不符，尚未切換版本。")
        # The signed config digest covers layer diffIDs. Docker save/load may
        # discard RepoDigests, so map only this signed reference to its verified
        # immutable image ID and satisfy pull from the local authenticated cache.
        original = self.manager.run

        def cached(*args, **kwargs):
            if args[:2] == ("docker", "pull"):
                if len(args) != 3 or args[2] != image_ref:
                    raise HostError("bootstrap_controller_network_pull_forbidden")
                return original("docker", "image", "inspect", image_id, **kwargs)
            return original(*(image_id if item == image_ref else item for item in args), **kwargs)

        service = Updater(self.manager)
        service.run = cached
        envelope = payload.controller
        service.verify(envelope["manifest"], envelope["signature"])
        candidate = digest(manifest)
        if any(item["state"] in {"staging", "deploying", "rolling_back", "needs_reconcile"} for item in service.records.values()):
            raise HostError("管理服務有尚未完成的更新作業，已停止主機更新。")
        record = {"candidate_digest": candidate, **envelope, "state": "staging", "created_at": time.time()}
        service.records[candidate] = record
        service.update(record)
        service.prepare(record)
        if record["state"] != "staged":
            raise HostError("管理服務更新未通過驗證，主機程式尚未切換。")
        service.apply({"candidate_digest": candidate, "approval_id": record["approval_id"]})
        deadline = time.monotonic() + 900
        while record["state"] in {"deploying", "rolling_back"}:
            if time.monotonic() >= deadline:
                raise HostError("管理服務更新結果尚未確認；不會重送更新，請聯絡支援。")
            time.sleep(1)
        if record["state"] != "deployed":
            raise HostError("管理服務更新未完成，主機程式尚未切換；家庭資料保留。")
        health = HostBundle(self.manager).controller_probe()
        if not isinstance(health, dict) or health.get("status") != "ok" or not exact_protocol(health.get("host_update"), COMPATIBILITY):
            raise HostError("管理服務尚未具備安全主機更新能力，已停止主機更新。")

    def apply(self, approval):
        if not isinstance(approval, str) or len(approval) != 64 or any(char not in "0123456789abcdef" for char in approval):
            raise HostError("更新核准內容無效，請重新查看。")
        lock = self.manager.root / "owner-update-bootstrap.lock"
        secure(lock, optional=True)
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HostError("主機更新正在處理，請勿重複操作。") from None
            value = read_json(self.path)
            if (value.get("state") != "offered" or value.get("review_digest") != approval or value.get("expires_at", 0) <= time.time()
                    or value.get("deadline", 0) <= time.monotonic()
                    or value.get("boot_id") != Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
                raise HostError("更新核准已過期或已使用，請重新查看。")
            if value.get("summary") != self._summary():
                raise HostError("主機或更新來源已變更，尚未套用；請重新查看。")
            payload = self._payload()
            value["state"] = "applying"
            value["phase"] = "capacity"
            self._save(value)
            stopped = False
            try:
                required = 256 * 1024**2 + payload.archive_size + payload.cache_size + (payload.cache_size * 4 if value["summary"]["controller_required"] else 0)
                restore = HostRestore(self.manager, save=lambda item: (value.update(restore_attempt=item), self._save(value)))
                attempt = {"candidate_digest": digest({"bootstrap": approval}), "restore_required_bytes": required}
                restore.capacity(required)  # Before any docker load or filesystem conversion.
                value["phase"] = "pausing"
                self._save(value)
                self._pause_existing(value)
                value["phase"] = "restoring"
                self._save(value)
                with self.manager.locked():
                    restore.create(attempt)
                    value["restore_attempt"] = attempt
                    self._save(value)
                    value["phase"] = "copying_media"
                    self._save(value)
                    self._updates_idle()
                    copied = payload.media.copy(self.manager.root / "bootstrap-cache" / approval)
                    payload.archive, payload.cache = copied["host-bundle.tar"], copied["controller-images.tar"]
                    stopped = True
                    value.setdefault("availability", {})["broker"] = {"state": "stop_requested"}
                    value["phase"] = "broker_stop_requested"
                    self._save(value)
                    self.manager.run("systemctl", "stop", "agentic-home-host.service", timeout=90)
                    trust_identity, trust = self._trust()
                    if trust_identity != value["summary"]["trust"]:
                        raise HostError("更新來源設定已變更，已停止更新。")
                    if PUBLISHER not in trust["publishers"]:
                        pin_review = inspect(PUBLISHER, IMAGE_REPOSITORY, payload.pem)
                        enroll(PUBLISHER, IMAGE_REPOSITORY, payload.pem, pin_review["review_digest"], path=self.trust,
                               expected_store_digest=store_digest(trust))
                    reviewed_engine = value["summary"]["installed_engine"]
                    if installed_engine_identity() != reviewed_engine:
                        raise HostError("主機更新器已變更，尚未替換；請聯絡支援。")
                    if reviewed_engine["state"] == "inactive":
                        install_engine(payload.source, expected_inactive_engine_digest=reviewed_engine["digest"])
                    else:
                        install_engine(payload.source)
                    version_layout(self.manager)
                    for name in value["summary"]["units"]:
                        source = payload.source / "host/systemd" / name
                        target = Path("/etc/systemd/system") / name
                        if not target.exists():
                            with target.open("xb") as output:
                                output.write(source.read_bytes())
                                output.flush()
                                os.fsync(output.fileno())
                            target.chmod(0o644)
                    self.manager.run("systemctl", "daemon-reload", timeout=30)
                    self.manager.run("systemctl", "enable", "agentic-home-update-recover.service", timeout=30)
                self._resume_broker(value)
                stopped = False
                # This endpoint belongs to the immutable engine so a future
                # rollback to the preserved 0.11 tree retains Host update UI.
                self.manager.run("systemctl", "enable", "--now", "agentic-home-update-api.service", timeout=90)
                self.manager.run("systemctl", "is-active", "--quiet", "agentic-home-update-api.service", timeout=15)
                if value["summary"]["controller_required"]:
                    value["phase"] = "controller_updating"
                    self._save(value)
                    self._controller(payload)
                def local_bundle(url, destination, maximum):
                    if url != payload.host["manifest"]["bundle"]["url"] or maximum != payload.archive.stat().st_size:
                        raise HostError("bootstrap_bundle_reference_changed")
                    shutil.copyfile(payload.archive, destination)
                reusable = SimpleNamespace(capacity=restore.capacity, create=lambda _: restore.create(attempt))
                engine = HostBundle(self.manager, download=local_bundle, restore=reusable, launch=lambda: None)
                value["phase"] = "host_staging"
                self._save(value)
                record = engine.stage(payload.host, background=False)
                if record["state"] != "staged":
                    raise HostError("主機更新檔案未通過驗證，尚未切換主機版本。")
                engine._approval({"candidate_digest": record["candidate_digest"], "approval_id": record["approval_id"]})
                value["phase"] = "host_executing"
                self._save(value)
                result = engine.execute()
                if not result or result["state"] != "deployed":
                    raise HostError("主機更新未完成；已保留原版本與還原點，請查看管理頁狀態。")
                value["state"] = "complete"
                self._save(value)
                return {"state": "complete", "message": "主機更新完成，家庭資料保留。"}
            except BaseException:
                value["state"] = "failed" if value.get("phase") == "capacity" else "needs_reconcile"
                self._save(value)
                raise
            finally:
                if stopped:
                    self._resume_broker(value)


def gui():
    try:
        review = subprocess.run(["/usr/bin/pkexec", TOOL, "--review"], capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        subprocess.run(["/usr/bin/zenity", "--error", "--title=主機更新", "--text=核對結果尚未確認，請重新開啟更新工具查看狀態。"], check=False)
        return
    if review.returncode:
        subprocess.run(["/usr/bin/zenity", "--error", "--title=主機更新", "--text=" + (review.stderr.strip() or "無法核對更新安裝包。")], check=False)
        return
    value = json.loads(review.stdout)
    text = (value["message"] + "\n\n主機版本：" + value["host_version"] +
            ("\n管理服務版本：" + value["controller_version"] if value["controller_required"] else "") +
            "\n\n官方金鑰指紋：\n" + value["key_sha256"] + "\n\n只適用於已從可信來源取得的安裝包。")
    answer = subprocess.run(["/usr/bin/zenity", "--question", "--title=確認主機更新", "--width=620", "--text=" + text], check=False)
    if answer.returncode:
        return
    progress = subprocess.Popen(["/usr/bin/zenity", "--progress", "--pulsate", "--no-cancel", "--auto-close",
                                 "--title=主機更新", "--text=正在核對並更新主機，可能需要一段時間，請保持更新 USB 連接。"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = subprocess.run(["/usr/bin/pkexec", TOOL, "--apply", value["review_digest"]], capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        result = SimpleNamespace(returncode=1, stderr="更新結果尚未確認，請重新開啟更新工具查看狀態；請勿重送更新。")
    finally:
        if progress.stdin:
            progress.stdin.close()
        try:
            progress.wait(timeout=5)
        except subprocess.TimeoutExpired:
            progress.terminate()
            try:
                progress.wait(timeout=5)
            except subprocess.TimeoutExpired:
                progress.kill()
    subprocess.run(["/usr/bin/zenity", "--info" if result.returncode == 0 else "--error", "--title=主機更新",
                    "--text=" + ("更新完成，請重新開啟管家管理頁。" if result.returncode == 0 else result.stderr.strip() or "更新結果尚未確認，請聯絡支援。")], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choices = parser.add_mutually_exclusive_group(required=True)
    choices.add_argument("--gui", action="store_true")
    choices.add_argument("--review", action="store_true")
    choices.add_argument("--apply")
    choices.add_argument("--recover-availability", action="store_true")
    args = parser.parse_args()
    if args.gui:
        return gui()
    if os.geteuid() != 0:
        parser.error("需要由主機的安全授權視窗確認管理員身分。")
    try:
        tool = BootstrapOwner(Manager())
        result = tool.recover_availability() if args.recover_availability else tool.review() if args.review else tool.apply(args.apply)
        print(json.dumps(result, ensure_ascii=False))
    except HostError as exc:
        parser.exit(1, (str(exc) if str(exc) in {MISSING_PIN, RECOVERED, INSERT_USB} else "無法安全完成主機更新；尚未確認的操作不會重送，原有還原點已保留。請聯絡支援核對。") + "\n")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "無法安全完成主機更新；尚未確認的操作不會重送，原有還原點已保留。請聯絡支援核對。\n")


if __name__ == "__main__":
    main()
