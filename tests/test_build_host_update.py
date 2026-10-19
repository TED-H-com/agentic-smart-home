"""Public Host release artifact construction, without any signing key."""
import base64
import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest
from test_host_update import files_for
from test_host_update import lab as host_update_lab

lab = host_update_lab

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("build_host_update", ROOT / "scripts/build_host_update.py")
build_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_module)


@pytest.fixture
def product(tmp_path):
    source = tmp_path / "product"
    for name in build_module.REQUIRED:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public product input\n")
    (source / "host/host-update-contract.json").write_text(json.dumps(build_module.CONTRACT))
    (source / "assets").mkdir()
    (source / "assets/secret-image.tar").write_bytes(b"must not be shipped")
    (source / ".git").mkdir()
    (source / ".git/config").write_text("must not be shipped")
    return source


def build(product, output):
    return build_module.build(product, output, version="0.11.1", source_commit="a" * 40,
                              tests=[{"name": "host-tests", "result": "passed", "sha256": "b" * 64}], now=100)


def test_unsigned_archive_has_exact_manifest_and_no_runtime_payload(product, tmp_path):
    output = tmp_path / "output"
    manifest = build(product, output)
    archive = output / "agentic-home-host-0.11.1.tar"
    assert manifest["bundle"]["size"] == archive.stat().st_size
    assert manifest["bundle"]["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest["expires_at"] - manifest["issued_at"] == 7 * 86400
    assert not (output / "agentic-home-host-update.json").exists()
    assert "signature" not in json.loads((output / "agentic-home-host-update.unsigned.json").read_text())
    with tarfile.open(archive) as package:
        files = package.getmembers()
        checksums = json.load(package.extractfile("BUNDLE.sha256.json"))
        assert {member.name for member in files} == set(checksums) | {"BUNDLE.sha256.json"}
        assert not any(name.startswith(("assets/", ".git/")) for name in checksums)
        for member in files:
            assert member.isfile() and not member.pax_headers
            assert member.uid == member.gid == 0 and member.mode == 0o644
            if member.name != "BUNDLE.sha256.json":
                assert hashlib.sha256(package.extractfile(member).read()).hexdigest() == checksums[member.name]


def test_release_artifact_never_overwrites_an_existing_version(product, tmp_path):
    build(product, tmp_path / "output")
    with pytest.raises(ValueError, match="replace"):
        build(product, tmp_path / "output")


def test_built_product_can_be_verified_and_staged_by_the_actual_updater(lab, tmp_path):
    source = tmp_path / "release-product"
    files = files_for(lab)
    files.update({"docs/HOST_UPDATES.md": b"public documentation", "INSTALL.md": b"installation",
                  "channels.lock.json": b"{}", "upstream.lock.json": b"{}"})
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    output = tmp_path / "release-output"
    manifest = build_module.build(source, output, version="0.11.1", source_commit="a" * 40,
                                  tests=[{"name": "host-tests", "result": "passed", "sha256": "b" * 64}])
    from agentic_host.host_update import canonical
    body = {"manifest": manifest, "signature": base64.b64encode(lab._test.private.sign(canonical(manifest))).decode()}
    lab._test.archives[manifest["bundle"]["url"]] = (output / "agentic-home-host-0.11.1.tar").read_bytes()
    record = lab.stage(body, background=False)
    assert record["state"] == "staged"
    assert (lab.bundles / record["candidate_digest"] / "docs/HOST_UPDATES.md").read_bytes() == files["docs/HOST_UPDATES.md"]
    assert lab.current() == lab._test.previous


@pytest.mark.parametrize("fault", ["link", "missing", "contract"])
def test_release_refuses_unsafe_or_incomplete_candidate(product, tmp_path, fault):
    if fault == "link":
        (product / "host/untrusted.py").symlink_to("/etc/passwd")
    elif fault == "missing":
        (product / "host/agentic_host/daemon.py").unlink()
    else:
        (product / "host/host-update-contract.json").write_text("{}")
    with pytest.raises(ValueError):
        build(product, tmp_path / "output")
