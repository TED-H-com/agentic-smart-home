"""Bounded private-file validation and durable commit readback."""
import json
import os
import stat
from pathlib import Path

from .queue import QueueError, require

MAX_STORAGE_BYTES = 1024 * 1024


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "storage_unavailable")
        result[key] = value
    return result


def read_owned(path, key, *, expected=None):
    """Read only our bounded regular file; optional commit readback and fsync."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if expected is None:
            return None
        raise QueueError("storage_unavailable") from None
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_STORAGE_BYTES
                and stat.S_IMODE(before.st_mode) & 0o077 == 0, "storage_unavailable")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_STORAGE_BYTES + 1)
        after = os.fstat(descriptor)
        require(len(raw) <= MAX_STORAGE_BYTES and (before.st_ino, before.st_size, before.st_mtime_ns)
                == (after.st_ino, after.st_size, after.st_mtime_ns), "storage_unavailable")
        envelope = json.loads(raw, object_pairs_hook=unique)
        require(isinstance(envelope, dict) and set(envelope) == {"version", "minor_version", "key", "data"}
                and type(envelope["version"]) is int and envelope["version"] == 1
                and envelope["minor_version"] == 1 and envelope["key"] == key, "storage_unavailable")
        if expected is not None:
            require(envelope["data"] == expected, "storage_unavailable")
            os.fsync(descriptor)
            parent = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        return envelope["data"]
    finally:
        os.close(descriptor)
