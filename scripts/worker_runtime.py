"""Copy the reviewed Host worker primitives into the standalone HAOS App build.

Supervisor builds each App directory independently. These generated files must
match their single maintained source; they are never separately edited.
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_worker_runtime(*, check=False, root=ROOT):
    source = root / "host/agentic_host"
    destination = root / "runner/worker_runtime/agentic_host"
    manifest_path = destination.parent / "manifest.json"
    files = {path.name: path for path in source.glob("*.py") if path.is_file() and not path.is_symlink()}
    expected = {"host/agentic_host/" + name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in sorted(files.items())}
    manifest = {"schema": 1, "source_hashes": expected}
    if check:
        assert json.loads(manifest_path.read_text()) == manifest, "Home worker shared source manifest differs"
        assert {p.name for p in destination.iterdir() if p.name != "__pycache__"} == set(files)
        for name, path in files.items():
            copy = destination / name
            assert not copy.is_symlink() and copy.read_bytes() == path.read_bytes(), "Stale worker primitive: " + name
        return expected
    destination.mkdir(parents=True, exist_ok=True)
    for path in destination.iterdir():
        if path.is_symlink():
            raise ValueError("Worker runtime cannot contain symlinks")
        if path.name not in files:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    for name, path in files.items():
        shutil.copyfile(path, destination / name)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return expected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    print(f"Home worker shared runtime: {len(package_worker_runtime(check=arguments.check))} files")
