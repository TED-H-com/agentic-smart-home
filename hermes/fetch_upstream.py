"""Fetch the reviewed source archive, verify it, and extract regular files only."""

import hashlib
import json
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

DOWNLOAD_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRY_WAIT_SECONDS = 60
RETRY_BACKOFF_SECONDS = 5
RETRY_HTTP_CODES = {429, 502, 503, 504}


def _retry_delay(header, attempt):
    if header is None:
        return RETRY_BACKOFF_SECONDS * 2**attempt
    value = header.strip()
    try:
        if value.isascii() and value.isdecimal():
            delay = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError("Missing timezone")
            delay = max(0, date.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid Hermes Retry-After header") from None
    if delay > MAX_RETRY_WAIT_SECONDS:
        raise ValueError("Hermes Retry-After exceeds retry wait limit")
    return delay


def _download(url, archive):
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            return
        except urllib.error.HTTPError as error:
            try:
                archive.unlink(missing_ok=True)
                if error.code not in RETRY_HTTP_CODES or attempt == DOWNLOAD_ATTEMPTS - 1:
                    raise
                delay = _retry_delay((error.headers or {}).get("Retry-After"), attempt)
            finally:
                error.close()
            time.sleep(delay)


def fetch(destination=Path("/opt/hermes")):
    lock = json.loads(Path(__file__).with_name("hermes.lock.json").read_text())
    # Image build filesystem; not a test scratch directory or a host tmpfs.
    with tempfile.TemporaryDirectory(dir="/opt") as temporary:
        archive = Path(temporary) / "upstream.tar.gz"
        _download(lock["archive_url"], archive)
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
