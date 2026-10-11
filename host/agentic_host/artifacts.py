"""Streaming, checksum-verified downloads. Never trusts an unverified cache file."""
import hashlib
import os
import urllib.request
from pathlib import Path


def sha256(path):
    with Path(path).open("rb") as stream:
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()


def fetch(artifact, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    filename = artifact["filename"]
    if Path(filename).name != filename or len(artifact["sha256"]) != 64:
        raise ValueError("Invalid locked artifact")
    path = directory / filename
    if path.is_symlink():
        raise ValueError("Refusing symlink in artifact cache")
    if path.exists():
        if sha256(path) == artifact["sha256"]:
            return path
        raise ValueError(f"Cached checksum mismatch: {filename}; remove corrupt file explicitly")
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    try:
        request = urllib.request.Request(artifact["url"], headers={"User-Agent": "Agentic-Home/0.2"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as stream:
            if not response.url.startswith("https://"):
                raise ValueError("Artifact redirected away from HTTPS")
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if sha256(temporary) != artifact["sha256"]:
            raise ValueError("Downloaded artifact checksum mismatch: " + filename)
        os.replace(temporary, path)
        return path
    finally:
        temporary.unlink(missing_ok=True)
