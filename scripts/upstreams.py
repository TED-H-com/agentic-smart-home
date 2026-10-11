#!/usr/bin/env python3
"""Fetch locked reference checkouts without changing any existing worktree."""
import argparse
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


def fetch(source, parent):
    name, commit, remote = source["name"], source["commit"], source["repository"]
    if not re.fullmatch(r"[a-z_]+", name) or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Invalid lock entry")
    target = parent / name
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not (target / ".git").is_dir():
            raise RuntimeError(f"Refusing to replace existing path: {target}")
        if (git("-C", target, "rev-parse", "HEAD") != commit or
                git("-C", target, "remote", "get-url", "origin") != remote or
                git("-C", target, "status", "--porcelain")):
            raise RuntimeError(f"Existing checkout differs or has changes; left untouched: {target}")
        print(f"Verified existing {name} {commit}")
        return
    temporary = parent / (".fetch-" + uuid.uuid4().hex)
    try:
        git("init", temporary)
        git("-C", temporary, "remote", "add", "origin", remote)
        git("-C", temporary, "fetch", "--depth=1", "origin", commit)
        git("-C", temporary, "checkout", "--detach", "FETCH_HEAD")
        if git("-C", temporary, "rev-parse", "HEAD") != commit:
            raise RuntimeError("Fetched commit does not match lock")
        if target.exists():
            raise RuntimeError("Destination appeared while fetching; refusing replacement")
        temporary.rename(target)
        print(f"Fetched {name} {commit}")
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "fetch"])
    parser.add_argument("--scope", default="all")
    args = parser.parse_args()
    entries = json.loads((ROOT / "upstream.lock.json").read_text())["sources"]
    selected = [s for s in entries if args.scope in {"all", s["name"]}]
    if not selected:
        parser.error("Unknown scope")
    if args.command == "plan":
        print(json.dumps(selected, indent=2))
        return
    parent = ROOT / "_upstream/sources"
    parent.mkdir(parents=True, exist_ok=True)
    for source in selected:
        fetch(source, parent)


if __name__ == "__main__":
    main()
