"""One-time local, authenticated-media bootstrap; never a broker endpoint.

The old 0.11.0 broker cannot authorize installation of a new root executor.
Only a locally authenticated installer may call this module. A downloaded tree
or SHA256SUMS alone is not authenticated media; see docs/HOST_UPDATES.md.
"""
import ctypes
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .manager import HostError, atomic
from .publisher import validate_official_channel

ENGINE = Path("/usr/lib/agentic-home-update")
BUNDLES = Path("/opt/agentic-home-bundles")
ACTIVE = Path("/opt/agentic-home")


def trusted(path, *, directory=False):
    path = Path(path)
    for current in (path, *path.parents):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise HostError("host_bootstrap_source_untrusted")
    if directory != path.is_dir() or not (path.is_dir() or path.is_file()):
        raise HostError("host_bootstrap_source_untrusted")
    return path


def tree_identity(source):
    """Include every engine module and the pinned descriptor in its identity."""
    source = Path(source)
    files = {"official-update-channel.json": source / "host/official-update-channel.json"}
    for path in sorted((source / "host/agentic_host").rglob("*.py")):
        files["agentic_host/" + str(path.relative_to(source / "host/agentic_host"))] = path
    if not {"agentic_host/host_update.py", "agentic_host/host_restore.py", "agentic_host/host_reconcile.py"} <= files.keys():
        raise HostError("host_bootstrap_engine_incomplete")
    manifest = {name: hashlib.sha256(trusted(path).read_bytes()).hexdigest() for name, path in files.items()}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest, files, manifest


def active_product(product):
    """Resolve only the fixed updater-owned active link, never arbitrary links."""
    product = Path(product)
    if product != ACTIVE or not product.is_symlink() or product.lstat().st_uid != 0:
        raise HostError("host_bootstrap_active_unowned")
    trusted(product.parent, directory=True)
    trusted(BUNDLES, directory=True)
    target = product.readlink()
    import re
    if target.parent != BUNDLES or not re.fullmatch(r"[a-f0-9]{64}|bootstrap-[a-f0-9]{32}", target.name):
        raise HostError("host_bootstrap_active_unowned")
    return trusted(target, directory=True)


def sync_directory(path):
    descriptor = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def exchange(first, second):
    """Linux renameat2(RENAME_EXCHANGE): the active path never disappears."""
    libc = ctypes.CDLL(None, use_errno=True)
    operation = getattr(libc, "renameat2", None)
    if operation is None:
        raise HostError("host_bootstrap_atomic_exchange_unavailable")
    if operation(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
        raise HostError("host_bootstrap_atomic_exchange_failed")
    sync_directory(Path(first).parent)
    sync_directory(Path(second).parent)


def installed_engine_identity(*, engine=ENGINE):
    """Inspect the exact installed immutable tree; no key or code is replaced."""
    engine = Path(engine)
    if not engine.exists() and not engine.is_symlink():
        return {"state": "absent", "digest": None}
    trusted(engine, directory=True)
    current = engine / "current"
    if not current.exists() and not current.is_symlink():
        return {"state": "absent", "digest": None}
    if not current.is_symlink() or current.lstat().st_uid != 0:
        raise HostError("host_bootstrap_executor_untrusted")
    relative = current.readlink()
    if relative.parent != Path(".") or not re.fullmatch(r"[a-f0-9]{64}", relative.name):
        raise HostError("host_bootstrap_executor_untrusted")
    target = trusted(engine / relative, directory=True)
    files = {}
    for path in sorted(target.rglob("*")):
        name = path.relative_to(target)
        if "__pycache__" in name.parts or path.suffix == ".pyc":
            continue
        if path.is_dir() and not path.is_symlink():
            trusted(path, directory=True)
            continue
        if (str(name) != "official-update-channel.json" and
                (not name.parts or name.parts[0] != "agentic_host" or path.suffix != ".py")):
            raise HostError("host_bootstrap_executor_untrusted")
        files[name.as_posix()] = hashlib.sha256(trusted(path).read_bytes()).hexdigest()
    required = {"official-update-channel.json", "agentic_host/host_update.py", "agentic_host/host_restore.py", "agentic_host/host_reconcile.py"}
    if not required <= files.keys():
        raise HostError("host_bootstrap_engine_incomplete")
    observed = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if observed != relative.name:
        raise HostError("host_bootstrap_engine_changed")
    descriptor = validate_official_channel(json.loads(trusted(target / "official-update-channel.json").read_text()))
    return {"state": "inactive" if descriptor["pin"] is None else "active", "digest": observed}


def install_engine(source, *, engine=ENGINE, expected_inactive_engine_digest=None):
    """Install immutable modules; only reviewed inactive-pin migration may relink."""
    if os.geteuid() != 0:
        raise HostError("host_bootstrap_local_root_required")
    source, engine = Path(source), Path(engine)
    trusted(source, directory=True)
    digest, files, manifest = tree_identity(source)
    descriptor = validate_official_channel(json.loads(trusted(source / "host/official-update-channel.json").read_text()))
    previous = installed_engine_identity(engine=engine)
    migration = previous["digest"] not in {None, digest}
    if migration:
        if (previous["state"] != "inactive" or descriptor["pin"] is None
                or expected_inactive_engine_digest != previous["digest"]):
            raise HostError("host_bootstrap_executor_already_installed")
    elif expected_inactive_engine_digest is not None and expected_inactive_engine_digest != previous["digest"]:
        raise HostError("host_bootstrap_executor_revision_changed")
    engine.mkdir(parents=True, mode=0o755, exist_ok=True)
    trusted(engine, directory=True)
    target = engine / digest
    if target.exists():
        trusted(target, directory=True)
        for name, expected in manifest.items():
            if hashlib.sha256(trusted(target / name).read_bytes()).hexdigest() != expected:
                raise HostError("host_bootstrap_engine_changed")
    else:
        stage = engine / (".stage-" + digest)
        if stage.exists() or stage.is_symlink():
            raise HostError("host_bootstrap_engine_interrupted")
        stage.mkdir(mode=0o755)
        try:
            for name, path in files.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
                # Copy bytes, not source ownership, setuid bits or ACLs.
                with destination.open("xb") as output:
                    output.write(path.read_bytes())
                    output.flush()
                    os.fsync(output.fileno())
                destination.chmod(0o644)
            os.rename(stage, target)
            sync_directory(engine)
        except BaseException:
            # Unreferenced partial stage has no active effect; preserve for inspection.
            raise
    actual = {path.relative_to(target).as_posix() for path in target.rglob("*")
              if path.is_file() and "__pycache__" not in path.relative_to(target).parts and path.suffix != ".pyc"}
    if actual != set(manifest):
        raise HostError("host_bootstrap_engine_changed")
    current = engine / "current"
    # Recheck after copying, immediately before changing the active pointer.
    if installed_engine_identity(engine=engine) != previous:
        raise HostError("host_bootstrap_executor_revision_changed")
    if previous["digest"] != digest:
        next_link = engine / ".current-next"
        if next_link.exists() or next_link.is_symlink():
            raise HostError("host_bootstrap_engine_interrupted")
        next_link.symlink_to(digest)
        os.replace(next_link, current)
        sync_directory(engine)
    return digest


def version_layout(manager, *, bundles=BUNDLES):
    """Convert an owned directory atomically, journaled before the exchange.

    The self-referencing inactive symlink becomes the original directory in one
    exchange; the new active symlink immediately points to that intact tree.
    On interruption, replay confirms this exact pair rather than moving again.
    """
    if os.geteuid() != 0:
        raise HostError("host_bootstrap_local_root_required")
    active, bundles = Path(manager.product), Path(bundles)
    journal = manager.root / "host-update-bootstrap.json"
    bundles.mkdir(parents=True, exist_ok=True, mode=0o755)
    trusted(bundles, directory=True)
    trusted(active.parent, directory=True)
    if journal.exists():
        value = json.loads(trusted(journal).read_text())
        name = value.get("baseline")
        if (set(value) != {"format", "baseline", "phase"} or value["format"] != 1 or not isinstance(name, str)
                or len(name) != len("bootstrap-") + 32 or not name.startswith("bootstrap-")
                or any(char not in "0123456789abcdef" for char in name[len("bootstrap-"):])):
            raise HostError("host_bootstrap_journal_invalid")
    else:
        if active.is_symlink():
            target = active.readlink()
            if target.parent != bundles:
                raise HostError("host_bootstrap_active_unowned")
            trusted(target, directory=True)
            return target
        trusted(active, directory=True)
        import secrets
        value = {"format": 1, "baseline": "bootstrap-" + secrets.token_hex(16), "phase": "prepared"}
        atomic(journal, value)
    baseline = bundles / value["baseline"]
    if active.is_symlink():
        if active.readlink() != baseline or baseline.is_symlink():
            raise HostError("host_bootstrap_layout_unconfirmed")
        trusted(baseline, directory=True)
    else:
        trusted(active, directory=True)
        if not baseline.exists() and not baseline.is_symlink():
            baseline.symlink_to(baseline)
            sync_directory(bundles)
        if not baseline.is_symlink() or baseline.readlink() != baseline:
            raise HostError("host_bootstrap_layout_unconfirmed")
        exchange(active, baseline)
        trusted(baseline, directory=True)
    value["phase"] = "complete"
    atomic(journal, value)
    return baseline


def pristine(manager, *, engine=ENGINE, bundles=BUNDLES):
    """Initial installation already has disk/OS approval, no used home to migrate."""
    if manager.state() or (manager.root.parent / "controller/controller.sqlite3").exists():
        raise HostError("host_bootstrap_existing_install_requires_owner_approval")
    descriptor = validate_official_channel(json.loads(trusted(manager.product / "host/official-update-channel.json").read_text()))
    # No usable authority exists yet. Defer installation so authenticated media
    # can later install the owner's explicitly approved first signing pin.
    if descriptor["pin"] is not None:
        install_engine(manager.product, engine=engine)
    return version_layout(manager, bundles=bundles)
