#!/usr/bin/env python3
"""Build an unsigned Host candidate for an external, approved signing pipeline.

No private key is accepted, generated, stored or published by this command.
Use a new product source tree and an independently authenticated old complete
product. Only the baseline's OCI/runtime pins and compose references are retained.
"""
import argparse
import hashlib
import json
import re
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.host_update import CONTRACT, LOCKS, REQUIRED, TOP_FILES, TOPS  # noqa: E402

MAX_BYTES = 128 * 1024**2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def runtime_baseline(product):
    """Read only pinned runtime inputs from an independently authenticated bundle.

    Its checksum index establishes file integrity, not initial authenticity.
    Maintainers must authenticate the baseline media through their release chain.
    """
    product = Path(product).resolve(strict=True)
    index_path = product / "BUNDLE.sha256.json"
    if index_path.is_symlink() or not index_path.is_file() or index_path.stat().st_size > 8 * 1024**2:
        raise ValueError("An authenticated complete baseline product with its checksum index is required")
    index = json.loads(index_path.read_text())
    if not isinstance(index, dict):
        raise ValueError("The baseline checksum index is invalid")
    files = {}
    for name in LOCKS:
        path = product / name
        if (any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(product))
                or not path.is_file() or path.stat().st_size > 32 * 1024**2
                or not isinstance(index.get(name), str) or not re.fullmatch(r"[a-f0-9]{64}", index[name])
                or hashlib.sha256(path.read_bytes()).hexdigest() != index[name]):
            raise ValueError("The baseline runtime inputs must match its authenticated checksum index")
        files[name] = path
    return files


def appliance_manifest(files):
    """Explicitly enroll the reviewed fixed adapter, never a candidate hook."""
    if "host/agentic_host/appliance.py" not in files:
        return None
    source = files.get("host/managed-system.json")
    if source is not None:
        value = json.loads(source.read_bytes())
        if (not isinstance(value, dict) or set(value) not in ({"format", "files"}, {"format", "files", "appliance_policy"})
                or type(value["format"]) is not int or value["format"] != 1 or not isinstance(value["files"], list)
                or ("appliance_policy" in value and (type(value["appliance_policy"]) is not int or value["appliance_policy"] != 1))):
            raise ValueError("Unsupported managed appliance policy")
    else:
        # Preserve the reconciler's default service plan when adding an explicit
        # manifest to a complete product which previously relied on that default.
        units = ("agentic-home.service", "agentic-home-host.service", "agentic-home-proxy.service")
        value = {"format": 1, "files": [{"target": "/etc/systemd/system/" + unit,
                  "source": "host/systemd/" + unit, "mode": 0o644}
                 for unit in units if "host/systemd/" + unit in files]}
    return canonical({**value, "appliance_policy": 1})


def build(product, destination, *, baseline_product, version, source_commit, tests, now=None):
    product, destination = Path(product), Path(destination)
    baseline = runtime_baseline(baseline_product)
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
    directories = TOPS
    top_files = TOP_FILES - {"BUNDLE.sha256.json"}
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
    # Host-only updates retain the old OCI/runtime contract and boot references.
    # The separately signed Controller prerequisite uses runtime.env explicitly.
    files.update(baseline)
    policy = appliance_manifest(files)
    if policy is not None:
        files["host/managed-system.json"] = policy
    if (not REQUIRED <= files.keys() or len(files) > 10000
            or sum(len(p) if isinstance(p, bytes) else p.stat().st_size for p in files.values()) > 256 * 1024**2):
        raise ValueError("Host candidate is incomplete or exceeds its bounds")
    manifest = {name: hashlib.sha256(path if isinstance(path, bytes) else path.read_bytes()).hexdigest() for name, path in files.items()}
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"agentic-home-host-{version}.tar"
    if archive.exists():
        raise ValueError("Refusing to replace an existing release artifact")
    import io
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as output:
        for name, path in files.items():
            if isinstance(path, bytes):
                info = tarfile.TarInfo(name)
                info.mode, info.size = 0o644, len(path)
                info.uname = info.gname = "root"
                output.addfile(info, io.BytesIO(path))
                continue
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
    parser.add_argument("--baseline-product", required=True, type=Path,
                        help="Independently authenticated complete old product; retain its runtime pins and compose")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--test-evidence", required=True, type=Path)
    args = parser.parse_args()
    build(args.product, args.output, baseline_product=args.baseline_product, version=args.version, source_commit=args.source_commit,
          tests=json.loads(args.test_evidence.read_text()))
    print("Unsigned Host bundle built; an approved external signer must produce the release envelope.")


if __name__ == "__main__":
    main()
