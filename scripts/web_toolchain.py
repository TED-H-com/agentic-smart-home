#!/usr/bin/env python3
"""Pin the runner image web toolchain to the product web manifests.

The copies are exact bytes. The verifier refuses a candidate whose
web/package.json or web/package-lock.json differs from this pin.
"""
import argparse
import sys
from pathlib import Path

FILES = ("package.json", "package-lock.json")


def toolchain_dir(root: Path) -> Path:
    return root / "runner" / "web-toolchain"


def sync_web_toolchain(root: Path, *, check: bool) -> None:
    source = root / "web"
    target = toolchain_dir(root)
    if not check:
        target.mkdir(parents=True, exist_ok=True)
    drifted = []
    for name in FILES:
        origin = source / name
        if origin.is_symlink() or not origin.is_file():
            raise SystemExit("web toolchain source missing: " + name)
        data = origin.read_bytes()
        dest = target / name
        if check:
            if dest.is_symlink() or not dest.is_file() or dest.read_bytes() != data:
                drifted.append(name)
        else:
            dest.write_bytes(data)
    if drifted:
        raise SystemExit("web toolchain drift: " + ", ".join(drifted))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.root or Path(__file__).resolve().parents[1]
    sync_web_toolchain(root, check=args.check)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
