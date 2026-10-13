"""Explicit local-root release trust enrollment; never a Web/model endpoint."""
import base64
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from .manager import HostError, atomic

TRUST = Path("/etc/agentic-home/update-publishers.json")


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


def enroll(identifier, repository, pem, approval, *, revoke=False, path=TRUST):
    if os.geteuid() != 0:
        raise HostError("publisher_local_root_required")
    summary = inspect(identifier, repository, pem)
    if approval != summary["review_digest"]:
        raise HostError("publisher_review_mismatch")
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise HostError("publisher_store_untrusted")
    if path.is_symlink() or (path.exists() and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022)):
        raise HostError("publisher_store_untrusted")
    with path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = json.loads(path.read_text()) if path.exists() else {"format": 1, "publishers": {}}
        if set(value) != {"format", "publishers"} or value["format"] != 1 or not isinstance(value["publishers"], dict):
            raise HostError("publisher_store_invalid")
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
        atomic(path, value)
    return {**summary, "state": "revoked" if revoke else "trusted"}
