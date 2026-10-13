#!/usr/bin/env python3
"""Write the Controller image's exact Git bundle.

The signed image digest covers controller/source/agentic-home.bundle. The
update manifest's source_commit must equal this bundle's only head.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATHS = ["controller", "runner", "hermes", "bridge", "host", "web", "contracts", "scripts",
         "providers.lock.json", "pyproject.toml", "uv.lock", "AGENTS.md", "DESIGN.md", "README.md",
         "PROJECT_PLAN.md", "PROJECT_SPEC.md"]
BUNDLE_NAME = "controller/source/agentic-home.bundle"


def _status_path(line):
    text = line[3:].strip() if len(line) >= 3 else line.strip()
    if " -> " in text:
        text = text.split(" -> ", 1)[1].strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.replace("\\", "/")


def controller_source(root=ROOT, destination=None):
    """Bundle HEAD. Refuse when product paths are dirty, same as coding_source()."""
    root = Path(root)
    destination = Path(destination) if destination is not None else root / BUNDLE_NAME
    changed = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", *PATHS], text=True)
    dirty = [line for line in changed.splitlines() if line.strip() and _status_path(line) != BUNDLE_NAME]
    if dirty:
        raise ValueError("Commit product source before building its controller source bundle")
    if destination.is_symlink():
        raise ValueError("Controller source bundle path is untrusted")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    subprocess.run(["git", "-C", str(root), "bundle", "create", str(destination.resolve()), "HEAD"], check=True)
    heads = [line.split() for line in subprocess.check_output(
        ["git", "bundle", "list-heads", str(destination)], text=True).splitlines() if line.strip()]
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if len(heads) != 1 or heads[0][0] != commit:
        destination.unlink(missing_ok=True)
        raise ValueError("Controller source bundle head does not match HEAD")
    return {"commit": commit, "path": str(destination)}


def main():
    try:
        controller_source()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
