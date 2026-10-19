#!/usr/bin/env python3
"""Build an unsigned Host candidate for an external, approved signing pipeline.

No private key is accepted, generated, stored or published by this command.
Use an already-built product directory so actual OCI/runtime pins remain exact.
"""
import argparse
import hashlib
import json
import re
import tarfile
import time
from pathlib import Path

REQUIRED = {"host/agentic_host/__init__.py", "host/agentic_host/daemon.py",
            "host/agentic_host/host_reconcile.py", "host/host-update-contract.json",
            "host/compose.yaml", "images.lock.json", "pyproject.toml", "providers.lock.json"}
CONTRACT = {"contract": 1, "host_protocol": 1, "controller_protocol": 1,
            "controller_schema": 3, "reconcile": 1, "executor": 1}
MAX_BYTES = 128 * 1024**2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def build(product, destination, *, version, source_commit, tests, now=None):
    product, destination = Path(product), Path(destination)
    if (not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?", version)
            or not re.fullmatch(r"[0-9a-f]{40}", source_commit)
            or not isinstance(tests, list) or not 1 <= len(tests) <= 20):
        raise ValueError("A version, exact source commit and test evidence are required")
    for evidence in tests:
        if (not isinstance(evidence, dict) or set(evidence) != {"name", "result", "sha256"}
                or not isinstance(evidence["name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", evidence["name"])
                or evidence["result"] != "passed" or not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])):
            raise ValueError("Exact passed test evidence hashes are required")
    if json.loads((product / "host/host-update-contract.json").read_text()) != CONTRACT:
        raise ValueError("Unsupported Host update contract")
    files = {}
    # These are public product inputs, never a home directory, secret store,
    # generated image payload or arbitrary checkout including its Git history.
    directories = {"host", "controller", "runner", "hermes", "bridge", "custom_components", "contracts", "docs", "scripts"}
    top_files = {"README.md", "INSTALL.md", "PROJECT_PLAN.md", "PROJECT_SPEC.md", "providers.lock.json",
                 "upstream.lock.json", "channels.lock.json", "pyproject.toml", "images.lock.json"}
    skip = {"__pycache__", "node_modules", ".pytest_cache", ".ruff_cache", ".upstream-cache", "test-results"}
    for path in sorted(product.rglob("*")):
        relative = path.relative_to(product)
        if relative.parts[0] not in directories | top_files or any(part in skip for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Host candidates cannot contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix in {".pyc", ".tsbuildinfo"}:
            raise ValueError("Host candidates require regular public product files")
        if len(relative.as_posix()) > 240 or any(len(part) > 128 for part in relative.parts):
            raise ValueError("Host candidate path exceeds its bounds")
        if path.stat().st_size > 32 * 1024**2:
            raise ValueError("Host candidate file exceeds 32 MiB")
        files[relative.as_posix()] = path
    if not REQUIRED <= files.keys() or len(files) > 10000 or sum(p.stat().st_size for p in files.values()) > 256 * 1024**2:
        raise ValueError("Host candidate is incomplete or exceeds its bounds")
    manifest = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"agentic-home-host-{version}.tar"
    if archive.exists():
        raise ValueError("Refusing to replace an existing release artifact")
    import io
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as output:
        for name, path in files.items():
            info = output.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o755 if info.mode & 0o111 else 0o644
            info.mtime = 0
            with path.open("rb") as content:
                output.addfile(info, content)
        payload = canonical(manifest)
        info = tarfile.TarInfo("BUNDLE.sha256.json")
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mode = 0o644
        info.size = len(payload)
        output.addfile(info, io.BytesIO(payload))
    if archive.stat().st_size > MAX_BYTES:
        archive.unlink()
        raise ValueError("Host candidate archive exceeds 128 MiB")
    issued = int(time.time() if now is None else now)
    envelope = {"format": 1, "kind": "agentic-home-host-bundle", "publisher": "ted-h-com-agentic-smart-home",
                "version": version, "source_commit": source_commit,
                "bundle": {"url": f"https://github.com/TED-H-com/agentic-smart-home/releases/download/v{version}/{archive.name}",
                           "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size},
                "compatibility": {"host_protocol": 1, "controller_protocol": 1, "controller_schema": 3, "reconcile": 1},
                "tests": tests, "issued_at": issued, "expires_at": issued + 7 * 86400}
    (destination / "agentic-home-host-update.unsigned.json").write_bytes(canonical(envelope) + b"\n")
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--test-evidence", required=True, type=Path)
    args = parser.parse_args()
    build(args.product, args.output, version=args.version, source_commit=args.source_commit,
          tests=json.loads(args.test_evidence.read_text()))
    print("Unsigned Host bundle built; an approved external signer must produce the release envelope.")


if __name__ == "__main__":
    main()
