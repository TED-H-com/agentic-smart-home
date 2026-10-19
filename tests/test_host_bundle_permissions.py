"""The public installation tree must be usable by unprivileged system services."""
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def build_module():
    spec = importlib.util.spec_from_file_location("host_bundle_permissions", ROOT / "scripts/build_host.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    return build


@pytest.mark.parametrize("build_umask", [0o077, 0o022, 0o000])
def test_bundle_public_modes_ignore_build_umask_without_chmod_cache(tmp_path, monkeypatch, build_umask):
    build = build_module()
    source = tmp_path / "source"
    source.mkdir()
    for directory in ("host", "controller", "runner", "hermes", "bridge", "custom_components", "contracts", "docs", "scripts"):
        path = source / directory
        path.mkdir()
        path.chmod(0o777)
        (path / "public.txt").write_text("public product content\n")
        (path / "public.txt").chmod(0o666)
    executable = source / "scripts/launch"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o2775)
    upstream = source / "hermes/.upstream-cache"
    upstream.mkdir()
    (upstream / ".gitignore").write_text("*\n!.gitignore\n")
    (upstream / "archive.tar.gz").write_bytes(b"unnecessary downloaded payload")
    (upstream / "partial").mkdir()
    (upstream / "partial/.gitignore").write_bytes(b"not a build input")
    for name in ("README.md", "INSTALL.md", "PROJECT_PLAN.md", "PROJECT_SPEC.md", "providers.lock.json",
                 "upstream.lock.json", "channels.lock.json"):
        (source / name).write_text("{}\n")
    (source / "host/bases.lock.json").write_text(json.dumps({"haos": {"filename": "haos.xz"}}))
    (source / "host/network.lock.json").write_text(json.dumps({"tailscale": {"filename": "tailscale.tgz"}}))
    (source / "host/official-update-channel.json").write_bytes((ROOT / "host/official-update-channel.json").read_bytes())
    cache = source / "build/cache"
    cache.mkdir(parents=True)
    for name in ("haos.xz", "tailscale.tgz"):
        (cache / name).write_bytes(b"public cached artifact")
        (cache / name).chmod(0o600)
    original_cache = {p.name: (p.stat().st_ino, stat.S_IMODE(p.stat().st_mode)) for p in cache.iterdir()}
    monkeypatch.setattr(build, "ROOT", source)
    monkeypatch.setitem(sys.modules, "package", SimpleNamespace(package_bridge=lambda: None,
                                                                package_notifications=lambda: None,
                                                                package_runner=lambda: None))
    monkeypatch.setattr(build, "coding_source", lambda assets: (assets / "coding-source.bundle").write_bytes(b"source"))
    monkeypatch.setattr(build, "fetch", lambda artifact, destination: destination / artifact["filename"])
    monkeypatch.setattr(build.subprocess, "check_output", lambda *args, **kwargs: "sha256:" + "a" * 64)

    def save_images(*args):
        assert args[:3] == ("docker", "save", "-o")
        Path(args[3]).write_bytes(b"OCI images")

    monkeypatch.setattr(build, "command", save_images)
    archive = tmp_path / "product.tar"
    previous = os.umask(build_umask)
    try:
        build.bundle(archive)
    finally:
        os.umask(previous)

    # A chmod of staged files would mutate these hardlinked cache assets too.
    for path in cache.iterdir():
        assert (path.stat().st_ino, stat.S_IMODE(path.stat().st_mode)) == original_cache[path.name]
        assert (source / "build/product/agentic-home/assets" / path.name).stat().st_ino == path.stat().st_ino
    with tarfile.open(archive) as product:
        members = product.getmembers()
        assert product.getmember("agentic-home").mode == 0o755
        assert product.getmember("agentic-home/assets").mode == 0o755
        assert product.getmember("agentic-home/scripts/launch").mode == 0o755
        for member in members:
            assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "root", "root")
            if member.isdir() or member.name == "agentic-home/scripts/launch":
                assert member.mode == 0o755
            else:
                assert member.mode == 0o644
        manifest = json.load(product.extractfile("agentic-home/BUNDLE.sha256.json"))
        assert {name for name in manifest if ".upstream-cache" in name} == {
            "hermes/.upstream-cache/.gitignore"}
        for relative, digest in manifest.items():
            assert hashlib.sha256(product.extractfile("agentic-home/" + relative).read()).hexdigest() == digest


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE,
                                        tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_public_bundle_refuses_links_and_special_entries(member_type):
    member = tarfile.TarInfo("agentic-home/unexpected")
    member.type = member_type
    with pytest.raises(ValueError, match="regular files and directories"):
        build_module().public_bundle_member(member)



def test_media_build_validates_official_pin_before_any_artifact_fetch(tmp_path, monkeypatch):
    build = build_module()
    from agentic_host.manager import HostError
    (tmp_path / "host").mkdir()
    descriptor = tmp_path / "host/official-update-channel.json"
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build, "fetch", lambda *args: pytest.fail("must validate before fetching"))
    with pytest.raises(ValueError, match="metadata"):
        build.bundle(tmp_path / "never.tar")
    value = json.loads((ROOT / "host/official-update-channel.json").read_text())
    value["repository"] = "someone-else/fake"
    descriptor.write_text(json.dumps(value))
    with pytest.raises(HostError, match="official_publisher_metadata_invalid"):
        build.bundle(tmp_path / "never.tar")
    descriptor.unlink()
    descriptor.symlink_to(ROOT / "host/official-update-channel.json")
    with pytest.raises(ValueError, match="metadata"):
        build.bundle(tmp_path / "never.tar")


def test_unconfigured_official_media_does_not_invent_a_publisher():
    value = build_module().validate_shipped_publisher(ROOT)
    assert value["pin"] is None and value["image_repository"] is None
    assert value["repository"] == "TED-H-com/agentic-smart-home"
