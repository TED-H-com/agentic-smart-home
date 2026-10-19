import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import bootstrap_media as module
from agentic_host.bootstrap_media import DIRECTORY, FILES, INSERT_USB, LABEL, BootstrapMedia
from agentic_host.manager import HostError


@pytest.fixture
def media(tmp_path, monkeypatch):
    root = tmp_path / "usb"
    (root / DIRECTORY).mkdir(parents=True)
    files = {}
    for name in FILES:
        data = ("verified public data: " + name).encode()
        (root / DIRECTORY / name).write_bytes(data)
        (root / DIRECTORY / name).chmod(0o666)  # Normal untrusted VFAT-style ownership/mode is acceptable.
        files[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    metadata = {"format": 1, "label": LABEL, "directory": DIRECTORY, "files": files}
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(metadata))
    monkeypatch.setattr(module, "read_json", lambda *_: json.loads(path.read_text()))

    def secure(path, **kwargs):
        assert Path(path).is_relative_to(tmp_path)
        if path.is_symlink():
            raise HostError("unsafe destination")

    monkeypatch.setattr(module, "secure", secure)
    manager = SimpleNamespace(calls=[], mounted=True, duplicate=False, label=LABEL)

    def run(*args, **kwargs):
        manager.calls.append(args)
        if args[0] == "/usr/bin/lsblk":
            devices = [{"name": "/dev/sdb", "type": "disk", "rm": False, "tran": "usb", "children": [
                {"name": "/dev/sdb1", "type": "part", "label": manager.label, "rm": False}]}]
            if manager.duplicate:
                devices.append({"name": "/dev/sdc1", "type": "part", "label": LABEL, "rm": True})
            return SimpleNamespace(stdout=json.dumps({"blockdevices": devices}), returncode=0)
        if args[0] == "/usr/bin/findmnt":
            return SimpleNamespace(returncode=0 if manager.mounted else 1,
                stdout=json.dumps({"filesystems": [{"source": "/dev/sdb1", "target": "/media/owner/AH-UPDATE"}]}))
        assert args[0] == "/usr/bin/udisksctl"
        manager.mounted = True
        return SimpleNamespace(stdout="", returncode=0)

    manager.run = run
    instance = BootstrapMedia(manager, path)
    instance.fixture = SimpleNamespace(root=root, tmp=tmp_path, path=path)
    return instance


def test_exact_single_removable_label_is_auto_located_without_owner_paths(media):
    assert media.locate() == Path("/media/owner/AH-UPDATE")
    assert all(call[0] in {"/usr/bin/lsblk", "/usr/bin/findmnt"} for call in media.manager.calls)


def test_unmounted_usb_is_mounted_with_fixed_readonly_options(media):
    media.manager.mounted = False
    media.locate()
    assert ("/usr/bin/udisksctl", "mount", "--block-device", "/dev/sdb1", "--options",
            "ro,nodev,nosuid,noexec", "--no-user-interaction") in media.manager.calls


@pytest.mark.parametrize("fault", ["missing", "ambiguous"])
def test_missing_or_duplicate_label_requests_one_official_usb_and_never_mounts(media, fault):
    if fault == "missing":
        media.manager.label = "OTHER"
    else:
        media.manager.duplicate = True
    with pytest.raises(HostError, match="請插入官方更新 USB"):
        media.locate()
    assert not any(call[0] == "/usr/bin/udisksctl" for call in media.manager.calls)


def test_vfat_files_are_hashed_then_root_private_copies_are_rehashed(media, monkeypatch):
    monkeypatch.setattr(media, "locate", lambda: media.fixture.root)
    assert media.inspect()["label"] == LABEL
    files = media.copy(media.fixture.tmp / "cache")
    for name, path in files.items():
        assert path.read_bytes() == (media.fixture.root / DIRECTORY / name).read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
    assert (media.fixture.tmp / "cache").stat().st_mode & 0o777 == 0o700


def test_changed_source_after_review_never_reaches_tar_or_docker_parser(media, monkeypatch):
    monkeypatch.setattr(media, "locate", lambda: media.fixture.root)
    media.inspect()
    source = media.fixture.root / DIRECTORY / "host-bundle.tar"
    source.write_bytes(b"x" * source.stat().st_size)
    with pytest.raises(HostError, match="尚未解析或套用"):
        media.copy(media.fixture.tmp / "cache")
    assert not (media.fixture.tmp / "cache/host-bundle.tar").exists()


@pytest.mark.parametrize("kind", ["leaf_symlink", "parent_symlink", "fifo", "oversize"])
def test_media_paths_cannot_redirect_privileged_reads_or_exceed_authenticated_size(media, monkeypatch, kind):
    monkeypatch.setattr(media, "locate", lambda: media.fixture.root)
    path = media.fixture.root / DIRECTORY / "host-bundle.tar"
    outside = media.fixture.tmp / "private-host-file"
    outside.write_text("never copy this")
    if kind == "parent_symlink":
        folder = path.parent
        folder.rename(folder.with_name("moved"))
        folder.symlink_to(folder.with_name("moved"), target_is_directory=True)
    else:
        path.unlink()
        if kind == "leaf_symlink":
            path.symlink_to(outside)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(b"too much" * 100)
    with pytest.raises((OSError, HostError)):
        media.copy(media.fixture.tmp / "cache")
    assert outside.read_text() == "never copy this"


@pytest.mark.parametrize("change", [{"label": "arbitrary"}, {"directory": "../../etc"}, {"url": "https://attacker.invalid"}])
def test_installed_manifest_cannot_supply_alternate_labels_paths_or_urls(media, change):
    value = {**media.manifest, **change}
    with pytest.raises(HostError):
        module.validate_manifest(value)


def test_small_package_preinst_contains_only_fixed_readonly_capacity_observation():
    assert module.SMALL_LIMIT == 16 * 1024**2
    assert 'required = 80 * 1024**2' in module.PREINST
    assert all(command not in module.PREINST for command in ("lvextend", "lvcreate", "shutdown", "unlink", "docker", "mount("))
    assert INSERT_USB.startswith("請插入官方更新 USB")
