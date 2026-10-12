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
