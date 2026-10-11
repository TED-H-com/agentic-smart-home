#!/usr/bin/env python3
"""Build the Linux product's source archive from an explicit allowlist."""
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.1"
EXCLUDED = {"__pycache__", "node_modules", "test-results", ".pytest_cache", ".ruff_cache"}


def files(directory):
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink() and not EXCLUDED.intersection(path.relative_to(directory).parts):
            if not path.name.endswith((".pyc", ".tsbuildinfo")):
                yield path


def archive(path, entries):
    manifest = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        def write(name, data):
            # Stable metadata keeps the downloadable Runner identical across image,
            # bundle and source builds. Payload changes still change the manifest.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            output.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        for source, destination in entries:
            data = source.read_bytes()
            write(destination, data)
            manifest[destination] = hashlib.sha256(data).hexdigest()
        write("MANIFEST.sha256.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    print(f"{path.relative_to(ROOT)} · {path.stat().st_size:,} bytes · {len(manifest)} files")


def main():
    package_runner()
    static = ROOT / "controller/static"
    html = (static / "index.html").read_text()
    for asset in re.findall(r'(?:src|href)="\./([^\"]+)"', html):
        if not (static / asset).is_file():
            raise SystemExit("Missing compiled web asset: " + asset)
    distribution = ROOT / "dist"
    distribution.mkdir(exist_ok=True)
    source = []
    for directory in ("controller", "runner", "web", "scripts", "contracts", "tests", "docs", "evidence", "host", ".github"):
        source += [(path, "agentic-home/" + str(path.relative_to(ROOT))) for path in files(ROOT / directory)]
    for path in sorted(ROOT.iterdir()):
        if path.is_file() and path.suffix in {".md", ".json", ".toml", ".lock", ".yaml"}:
            source.append((path, "agentic-home/" + path.name))
    source += [(ROOT / ".gitignore", "agentic-home/.gitignore")]
    archive(distribution / f"agentic-home-{VERSION}-source.zip", source)
    checksums = {}
    for path in sorted(distribution.glob(f"agentic-home-{VERSION}-*")):
        if path.suffix in {".iso", ".tar", ".zip"} and ".partial." not in path.name:
            with path.open("rb") as stream:
                checksums[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    (distribution / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in checksums.items()))


def package_runner():
    destination = ROOT / "controller/static/downloads/agentic-runner.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive(destination, [(path, "agentic_runner/" + str(path.relative_to(ROOT / "runner")))
                          for path in files(ROOT / "runner")])


if __name__ == "__main__":
    main()
