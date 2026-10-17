"""Fetch the reviewed source archive, verify it, and extract regular files only."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
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


def _lock():
    lock = json.loads(Path(__file__).with_name("hermes.lock.json").read_text())
    if not isinstance(lock.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", lock["sha256"]):
        raise ValueError("Invalid Hermes source archive digest")
    return lock


def _digest(archive):
    with archive.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _cache_path(cache_dir, lock):
    directory = Path(cache_dir).absolute()
    if directory.resolve() != directory:
        raise ValueError("Hermes archive cache directory must not be a symlink")
    return directory / (lock["sha256"] + ".tar.gz")


def _verified_archive(lock, archive, cache_dir=None):
    """Consume a cache read-only; verify the private copy actually extracted."""
    if cache_dir is not None:
        cached = _cache_path(cache_dir, lock)
        try:
            fd = os.open(cached, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Hermes cached archive must be a regular file")
                with archive.open("wb") as output:
                    shutil.copyfileobj(source, output)
            if _digest(archive) == lock["sha256"]:
                return True
            # A corrupt cache is a miss, never extraction input. Do not modify
            # the cache: Docker mounts it read-only. Download truncates scratch.
    _download(lock["archive_url"], archive)
    if _digest(archive) != lock["sha256"]:
        raise ValueError("Hermes source archive digest mismatch")
    return False


def populate_cache(cache_dir):
    """Host-side writer: atomically publish only complete, verified downloads."""
    lock = _lock()
    cached = _cache_path(cache_dir, lock)
    cached.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cached.parent) as temporary:
        archive = Path(temporary) / "upstream.tar.gz"
        if not _verified_archive(lock, archive, cached.parent):
            os.replace(archive, cached)
    return cached


def fetch(destination=Path("/opt/hermes"), cache_dir=None):
    lock = _lock()
    # Image build filesystem; not a test scratch directory or a host tmpfs.
    with tempfile.TemporaryDirectory(dir="/opt") as temporary:
        archive = Path(temporary) / "upstream.tar.gz"
        _verified_archive(lock, archive, cache_dir)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, help="Optional read-only archive cache when extracting")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download-only", action="store_true", help="Populate writable cache without extracting")
    mode.add_argument("--print-sha256", action="store_true", help="Print the validated lock digest without downloading")
    args = parser.parse_args(argv)
    if args.print_sha256:
        print(_lock()["sha256"])
    elif args.download_only:
        if args.cache_dir is None:
            parser.error("--download-only requires --cache-dir")
        populate_cache(args.cache_dir)
    else:
        fetch(cache_dir=args.cache_dir)


if __name__ == "__main__":
    main()
