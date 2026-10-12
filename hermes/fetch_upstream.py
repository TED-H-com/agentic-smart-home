"""Fetch the reviewed source archive, verify it, and extract regular files only."""

import hashlib
import json
import tarfile
import tempfile
import urllib.request
from pathlib import Path


def fetch(destination=Path("/opt/hermes")):
    lock = json.loads(Path(__file__).with_name("hermes.lock.json").read_text())
    # Image build filesystem; not a test scratch directory or a host tmpfs.
    with tempfile.TemporaryDirectory(dir="/opt") as temporary:
        archive = Path(temporary) / "upstream.tar.gz"
        urllib.request.urlretrieve(lock["archive_url"], archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != lock["sha256"]:
            raise ValueError("Hermes source archive digest mismatch")
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as source:
            for member in source:
                parts = Path(member.name).parts
                if not parts or parts[0] != "hermes-agent-" + lock["commit"]:
                    raise ValueError("Unexpected archive root")
                if len(parts) == 1:
                    continue
                relative = Path(*parts[1:])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Unsafe source path")
                if member.issym() or member.islnk():
                    # Upstream skills contain convenience symlinks; not part of the runtime.
                    continue
                if not member.isfile() and not member.isdir():
                    raise ValueError("Special source file refused")
                member.name = str(relative)
                source.extract(member, destination, filter="data")
        (destination / ".agentic-commit").write_text(lock["commit"] + "\n")


if __name__ == "__main__":
    fetch()
