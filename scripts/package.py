#!/usr/bin/env python3
"""Build the Linux product's source archive from an explicit allowlist."""
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.11.1"
EXCLUDED = {"__pycache__", "node_modules", "test-results", ".pytest_cache", ".ruff_cache"}


def files(directory):
    for path in sorted(directory.rglob("*")):
        # Preserve the empty build-context directory, never downloaded archives.
        relative = path.relative_to(directory)
        if ".upstream-cache" in relative.parts and relative != Path(".upstream-cache/.gitignore"):
            continue
        if path.is_file() and not path.is_symlink() and not EXCLUDED.intersection(path.relative_to(directory).parts):
            # The Controller build bundle is a local build input, never a source file.
            if not path.name.endswith((".pyc", ".tsbuildinfo")) and path.name != "agentic-home.bundle":
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
    package_notifications()
    package_bridge()
    static = ROOT / "controller/static"
    html = (static / "index.html").read_text()
    for asset in re.findall(r'(?:src|href)="\./([^\"]+)"', html):
        if not (static / asset).is_file():
            raise SystemExit("Missing compiled web asset: " + asset)
    distribution = ROOT / "dist"
    distribution.mkdir(exist_ok=True)
    source = []
    for directory in ("controller", "runner", "hermes", "bridge", "custom_components", "web", "scripts", "contracts", "tests", "docs", "evidence", "host", ".github", "site"):
        source += [(path, "agentic-home/" + str(path.relative_to(ROOT))) for path in files(ROOT / directory)]
    # site/demo is the Pages build of web/demo, not source.
    source = [entry for entry in source if not entry[0].is_relative_to(ROOT / "site/demo")]
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
    from worker_runtime import package_worker_runtime
    package_worker_runtime()
    destination = ROOT / "controller/static/downloads/agentic-runner.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive(destination, [(path, "agentic_runner/" + str(path.relative_to(ROOT / "runner")))
                          for path in files(ROOT / "runner")])


def package_bridge():
    """The HA App serves the exact compiled product UI, without a second build."""
    source, target = ROOT / "controller/static", ROOT / "bridge/static"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "index.html", target / "index.html")
    assets = target / "assets"
    if assets.exists():
        shutil.rmtree(assets)
    shutil.copytree(source / "assets", assets)
    shutil.copyfile(ROOT / "controller/requirements.txt", ROOT / "bridge/requirements.txt")


def package_notifications():
    """Installable native HA adapter; contains no Controller configuration or credentials."""
    directory = ROOT / "custom_components/agentic_home_notifications"
    if not (directory / "manifest.json").is_file():
        raise SystemExit("Missing native notifications integration manifest")
    destination = ROOT / "controller/static/downloads/agentic-home-notifications.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = [(path, str(path.relative_to(ROOT))) for path in files(directory)]
    entries.append((ROOT / "docs/HA_NOTIFICATIONS.md", "INSTALL.md"))
    archive(destination, entries)


if __name__ == "__main__":
    main()
