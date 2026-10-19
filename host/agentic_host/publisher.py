"""Local-root enrollment and an owner-approved, shipped official pin only.

Web callers cannot provide keys, repositories or URLs. The separate local-root
CLI remains available to maintainers for independently reviewed publishers.
"""
import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

from .manager import HostError, atomic

TRUST = Path("/etc/agentic-home/update-publishers.json")
OFFICIAL_PUBLISHER = "ted-h-com-agentic-smart-home"
OFFICIAL_REPOSITORY = "TED-H-com/agentic-smart-home"
OFFICIAL_IMAGE = "ghcr.io/ted-h-com/agentic-smart-home-controller"
OFFICIAL_CHANNEL = "stable"
OFFER_SECONDS = 300


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def trusted_path(path, *, optional=False):
    """Reject writable ancestry and symlinks, including dangling leaf symlinks."""
    path = Path(path)
    for item in (path, *path.parents):
        if item.is_symlink():
            raise HostError("publisher_store_untrusted")
        if not item.exists():
            if item == path and not optional:
                raise HostError("publisher_store_missing")
            continue
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022 or not (stat.S_ISREG(info.st_mode) if item == path else stat.S_ISDIR(info.st_mode)):
            raise HostError("publisher_store_untrusted")


def store_value(path):
    trusted_path(path, optional=True)
    if not Path(path).exists():
        return {"format": 1, "publishers": {}}
    if Path(path).stat().st_size > 131072:
        raise HostError("publisher_store_invalid")
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or set(value) != {"format", "publishers"} or type(value["format"]) is not int or value["format"] != 1 or not isinstance(value["publishers"], dict):
        raise HostError("publisher_store_invalid")
    return value


def store_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def public_key(pem):
    if not isinstance(pem, str) or len(pem) > 4096:
        raise HostError("publisher_key_invalid")
    match = re.fullmatch(r"\s*-----BEGIN PUBLIC KEY-----\s+([A-Za-z0-9+/=\s]+)-----END PUBLIC KEY-----\s*", pem)
    try:
        der = base64.b64decode("".join(match[1].split()), validate=True) if match else b""
    except ValueError:
        der = b""
    # RFC 8410 Ed25519 SubjectPublicKeyInfo, exactly one 32-byte public key.
    if len(der) != 44 or der[:12] != bytes.fromhex("302a300506032b6570032100"):
        raise HostError("publisher_key_must_be_ed25519")
    clean = "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der).decode() + "\n-----END PUBLIC KEY-----\n"
    return clean, hashlib.sha256(der).hexdigest()


def inspect(identifier, repository, pem):
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", identifier):
        raise HostError("publisher_identity_invalid")
    if not isinstance(repository, str) or not re.fullmatch(r"[a-z0-9.-]+/[a-z0-9_./-]+", repository) or len(repository) > 300 or ".." in repository:
        raise HostError("publisher_repository_invalid")
    _, fingerprint = public_key(pem)
    summary = {"publisher": identifier, "image_repository": repository, "key_sha256": fingerprint,
               "authority": "signed_controller_images_only"}
    digest = hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**summary, "review_digest": digest}


def enroll(identifier, repository, pem, approval, *, revoke=False, path=TRUST, expected_store_digest=None):
    if os.geteuid() != 0:
        raise HostError("publisher_local_root_required")
    summary = inspect(identifier, repository, pem)
    if approval != summary["review_digest"]:
        raise HostError("publisher_review_mismatch")
    path = Path(path)
    trusted_path(path, optional=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    trusted_path(path, optional=True)
    lock_path = path.with_suffix(".lock")
    trusted_path(lock_path, optional=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = store_value(path)
        if expected_store_digest is not None and store_digest(value) != expected_store_digest:
            raise HostError("publisher_revision_changed")
        entry = {"public_key_pem": public_key(pem)[0], "image_repository": repository}
        previous = value["publishers"].get(identifier)
        if revoke:
            if previous != entry:
                raise HostError("publisher_revision_changed")
            del value["publishers"][identifier]
        else:
            if previous is not None and previous != entry:
                raise HostError("publisher_revoke_previous_first")
            value["publishers"][identifier] = entry
        trusted_path(path.with_name(path.name + ".new"), optional=True)
        atomic(path, value)
    return {**summary, "state": "revoked" if revoke else "trusted"}



def validate_official_channel(value):
    """Pure format validation used by media builds and the installed Host.

    The designated image name is a future contract, not proof an artifact exists.
    No network lookup can substitute for the public key shipped in trusted media.
    """
    fields = {"format", "publisher", "repository", "channel", "image_repository", "pin"}
    if (not isinstance(value, dict) or set(value) != fields or type(value["format"]) is not int
            or value["format"] != 1 or value["publisher"] != OFFICIAL_PUBLISHER
            or value["repository"] != OFFICIAL_REPOSITORY or value["channel"] != OFFICIAL_CHANNEL):
        raise HostError("official_publisher_metadata_invalid")
    if value["pin"] is None:
        if value["image_repository"] is not None:
            raise HostError("official_publisher_metadata_invalid")
        return dict(value)
    pin = value["pin"]
    if (not isinstance(pin, dict) or set(pin) != {"public_key_pem", "key_sha256"}
            or value["image_repository"] != OFFICIAL_IMAGE):
        raise HostError("official_publisher_metadata_invalid")
    pem, fingerprint = public_key(pin["public_key_pem"])
    if pin["key_sha256"] != fingerprint:
        raise HostError("official_publisher_fingerprint_mismatch")
    return {**value, "pin": {"public_key_pem": pem, "key_sha256": fingerprint}}


def official_channel(product):
    path = Path(product) / "host/official-update-channel.json"
    trusted_path(path)
    if path.stat().st_size > 16384:
        raise HostError("official_publisher_metadata_invalid")
    return validate_official_channel(json.loads(path.read_text()))


class OfficialPublisher:
    def __init__(self, manager, *, trust_path=None):
        self.manager = manager
        self.trust = Path(trust_path) if trust_path is not None else TRUST
        self.lock = threading.RLock()
        # Process-local authorization expires on broker restart; never persist a
        # browser's approval capability as a reusable root credential.
        self.pending = None

    def review(self):
        metadata = official_channel(self.manager.product)
        trust = store_value(self.trust)
        summary = {"contract": 1, "publisher": OFFICIAL_PUBLISHER, "repository": OFFICIAL_REPOSITORY,
                   "channel": OFFICIAL_CHANNEL, "image_repository": metadata["image_repository"],
                   "key_sha256": metadata["pin"]["key_sha256"] if metadata["pin"] else None,
                   "authority": "signed_controller_images_only", "scope": "controller_only_same_schema",
                   "review_digest": None}
        if metadata["pin"] is None:
            return {**summary, "state": "unavailable", "code": "official_publisher_pin_not_shipped",
                    "message": "官方更新來源尚未備妥；目前版本未附官方驗證金鑰，請等待受信任的新版安裝媒體。"}, metadata, trust
        entry = {"public_key_pem": metadata["pin"]["public_key_pem"], "image_repository": metadata["image_repository"]}
        previous = trust["publishers"].get(OFFICIAL_PUBLISHER)
        digest = store_digest({"summary": summary, "metadata": metadata, "trust": trust})
        if previous is not None and previous != entry:
            state, code, message = "conflict", "official_publisher_key_conflict", "目前的更新驗證金鑰與安裝媒體不同；系統不會自行更換，請聯絡支援核對。"
        elif previous == entry:
            state, code, message = "trusted", "official_publisher_trusted", "已設定官方更新來源；每次安裝更新仍需您核准，並驗證簽章。"
        else:
            state, code, message = "approval_required", "official_publisher_owner_approval_required", "請核准此安裝媒體附帶的官方更新來源；這只允許驗證管家程式更新，不會更新主機或 Home Assistant。"
        return {**summary, "review_digest": digest, "state": state, "code": code, "message": message}, metadata, trust

    def status(self):
        try:
            return self.review()[0]
        except (OSError, ValueError, HostError, TypeError, KeyError):
            return {"contract": 1, "state": "unavailable", "code": "official_publisher_metadata_untrusted",
                    "publisher": OFFICIAL_PUBLISHER, "repository": OFFICIAL_REPOSITORY,
                    "channel": OFFICIAL_CHANNEL, "image_repository": None, "key_sha256": None, "review_digest": None,
                    "authority": "signed_controller_images_only", "scope": "controller_only_same_schema",
                    "message": "無法安全確認官方更新來源；尚未變更信任設定，請聯絡支援取得受信任的安裝媒體。"}

    def offer(self, body):
        if body != {}:
            raise HostError("official_publisher_request_invalid")
        with self.lock:
            summary, _, _ = self.review()
            if summary["state"] != "approval_required":
                raise HostError("official_publisher_not_available_for_approval")
            token = secrets.token_urlsafe(32)
            self.pending = {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                            "review_digest": summary["review_digest"], "deadline": time.monotonic() + OFFER_SECONDS}
            return {**summary, "approval": token, "expires_at": time.time() + OFFER_SECONDS}

    def approve(self, body):
        if (not isinstance(body, dict) or set(body) != {"review_digest", "approval"}
                or not isinstance(body["review_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", body["review_digest"])
                or not isinstance(body["approval"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", body["approval"])):
            raise HostError("official_publisher_approval_invalid")
        with self.lock, self.manager.locked():
            pending = self.pending
            token_hash = hashlib.sha256(body["approval"].encode()).hexdigest()
            if (not pending or not secrets.compare_digest(pending["token_sha256"], token_hash)
                    or pending["review_digest"] != body["review_digest"] or pending["deadline"] <= time.monotonic()):
                raise HostError("official_publisher_approval_invalid")
            self.pending = None  # consume once, including revision-change failures
            summary, metadata, trust = self.review()
            if summary["state"] != "approval_required" or summary["review_digest"] != body["review_digest"]:
                raise HostError("official_publisher_revision_changed")
            pin = metadata["pin"]
            local_review = inspect(OFFICIAL_PUBLISHER, metadata["image_repository"], pin["public_key_pem"])
            enroll(OFFICIAL_PUBLISHER, metadata["image_repository"], pin["public_key_pem"], local_review["review_digest"],
                   path=self.trust, expected_store_digest=store_digest(trust))
            return self.status()


def install_official_default(product, *, new_install, trust_path=TRUST):
    """Only a pristine installer may inherit its authenticated media's exact pin.

    Used systems and pre-existing local trust stores always retain their choices.
    This function never imports a key from the browser, internet or owner home.
    """
    if not new_install or Path(trust_path).exists() or Path(trust_path).is_symlink():
        return {"state": "preserved"}
    metadata = official_channel(product)
    if metadata["pin"] is None:
        return {"state": "unavailable", "code": "official_publisher_pin_not_shipped"}
    pin = metadata["pin"]
    review = inspect(OFFICIAL_PUBLISHER, metadata["image_repository"], pin["public_key_pem"])
    return enroll(OFFICIAL_PUBLISHER, metadata["image_repository"], pin["public_key_pem"], review["review_digest"], path=trust_path)
