"""Build-time download of the locked official Antigravity binary; never runs an installer."""
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path

lock = json.loads(Path(__file__).with_name("agy.lock.json").read_text())
archive = Path("/tmp/agy.tar.gz")
try:
    urllib.request.urlretrieve(lock["url"], archive)
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha512").hexdigest() != lock["sha512"]:
            raise SystemExit("Antigravity archive checksum mismatch")
    with tarfile.open(archive) as package:
        # Extract one known regular file without trusting archive paths or links.
        member = package.getmember("antigravity")
        if not member.isfile():
            raise SystemExit("Antigravity binary is not a regular file")
        binary = Path("/usr/local/bin/agy")
        binary.write_bytes(package.extractfile(member).read())
        binary.chmod(0o755)
finally:
    archive.unlink(missing_ok=True)
