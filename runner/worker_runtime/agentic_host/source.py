"""Provision the release's immutable Git source for private coding clones."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .artifacts import sha256
from .manager import HostError, atomic

SHA40 = re.compile(r"[a-f0-9]{40}")


def source_identity(root):
    """Read coding source.json. Missing or invalid files yield null commits."""
    unknown = {"source_commit": None, "install_commit": None}
    path = Path(root) / "source.json"
    try:
        if path.is_symlink() or not path.is_file():
            return unknown
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("repo_id") != "agentic-home":
            return unknown
        commit = value.get("commit")
        if not isinstance(commit, str) or not SHA40.fullmatch(commit):
            return unknown
        install = value.get("install_commit")
        if install is None:
            return {"source_commit": commit, "install_commit": None}
        if not isinstance(install, str) or not SHA40.fullmatch(install):
            return unknown
        return {"source_commit": commit, "install_commit": install}
    except (OSError, ValueError, UnicodeError):
        return unknown


def provision_source(product, state):
    assets = Path(product) / "assets"
    lock = json.loads((assets / "coding-source.json").read_text())
    if set(lock) != {"repo_id", "commit", "bundle_sha256"} or lock["repo_id"] != "agentic-home" or not re.fullmatch(r"[a-f0-9]{40}", lock["commit"]) or not re.fullmatch(r"[a-f0-9]{64}", lock["bundle_sha256"]):
        raise HostError("Invalid coding source lock")
    bundle = assets / "coding-source.bundle"
    if bundle.is_symlink() or sha256(bundle) != lock["bundle_sha256"]:
        raise HostError("Coding source checksum mismatch")
    coding = Path(state) / "coding"
    coding.mkdir(mode=0o700, parents=True, exist_ok=True)
    if coding.is_symlink() or (coding / "source").is_symlink():
        raise HostError("Coding source path is untrusted")
    if (coding / "source").exists():
        try:
            previous = json.loads((coding / "source.json").read_text())
            actual = subprocess.check_output(["git", "-C", str(coding / "source"), "rev-parse", "HEAD"], text=True).strip()
            if previous == lock and actual == lock["commit"]:
                return lock  # A later firstboot step may have failed; reuse only this exact source.
            # An updater may have moved HEAD off the install bundle. Keep that repo.
            if (isinstance(previous, dict) and previous.get("repo_id") == "agentic-home"
                and previous.get("install_commit") == lock["commit"] and actual == previous.get("commit")
                and isinstance(previous.get("commit"), str) and SHA40.fullmatch(previous["commit"])):
                return previous
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
        raise HostError("Coding source already exists with different content")
    os.chmod(coding, 0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".source-", dir=coding))
    try:
        target = temporary / "source"
        subprocess.run(["git", "clone", "--bare", "--no-hardlinks", str(bundle), str(target)], check=True, capture_output=True)
        actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True).strip()
        if actual != lock["commit"]:
            raise HostError("Coding source commit mismatch")
        subprocess.run(["git", "-C", str(target), "fsck", "--full"], check=True, capture_output=True)
        os.chmod(target, 0o700)
        os.replace(target, coding / "source")
        atomic(coding / "source.json", lock)
        return lock
    finally:
        shutil.rmtree(temporary)
