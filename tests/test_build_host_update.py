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
    baseline = tmp_path / "baseline-product"
    old_runtime = {
        "images.lock.json": json.dumps({"agentic-home-controller:0.11.0": "sha256:" + "a" * 64,
                                        "agentic-home-runner:0.11.0": "sha256:" + "b" * 64}).encode(),
        "providers.lock.json": b'{"codex":"old-tested-version"}',
        "host/bases.lock.json": b'{"haos":"old-tested-version"}',
        "host/network.lock.json": b'{"tailscale":"old-tested-version"}',
        "host/compose.yaml": b'services:\n  controller:\n    image: ${AGENTIC_CONTROLLER_IMAGE:-agentic-home-controller:0.11.0}\n  runner:\n    image: agentic-home-runner:0.11.0\n',
    }
    for name, content in old_runtime.items():
        path = baseline / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (baseline / "BUNDLE.sha256.json").write_text(json.dumps({name: hashlib.sha256(content).hexdigest() for name, content in old_runtime.items()}))
    for name in old_runtime:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(old_runtime[name].replace(b"old-", b"new-").replace(b"0.11.0", b"0.12.0"))
    return source


def build(product, output):
    return build_module.build(product, output, baseline_product=product.parent / "baseline-product", version="0.11.1", source_commit="a" * 40,
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
    old_images = json.dumps({"agentic-home-controller:0.11.0": "sha256:" + "a" * 64,
                             "agentic-home-runner:0.11.0": "sha256:" + "b" * 64}).encode()
    old_compose = b"services:\n  controller:\n    image: ${AGENTIC_CONTROLLER_IMAGE:-agentic-home-controller:0.11.0}\n  runner:\n    image: agentic-home-runner:0.11.0\n"
    (lab._test.previous / "images.lock.json").write_bytes(old_images)
    (lab._test.previous / "host/compose.yaml").write_bytes(old_compose)
    (lab._test.previous / "BUNDLE.sha256.json").write_text(json.dumps({name: hashlib.sha256((lab._test.previous / name).read_bytes()).hexdigest() for name in build_module.LOCKS}))
    files["images.lock.json"] = json.dumps({"agentic-home-controller:0.12.0": "sha256:" + "c" * 64,
                                          "agentic-home-runner:0.12.0": "sha256:" + "d" * 64}).encode()
    files["host/compose.yaml"] = old_compose.replace(b"0.11.0", b"0.12.0")
    runtime = lab.manager.root / "runtime.env"
    runtime.write_text("AGENTIC_CONTROLLER_IMAGE=sha256:" + "c" * 64 + "\n")
    runtime_before = runtime.read_bytes()
    files.update({"docs/HOST_UPDATES.md": b"public documentation", "INSTALL.md": b"installation",
                  "channels.lock.json": b"{}", "upstream.lock.json": b"{}"})
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    output = tmp_path / "release-output"
    manifest = build_module.build(source, output, baseline_product=lab._test.previous, version="0.11.1", source_commit="a" * 40,
                                  tests=[{"name": "host-tests", "result": "passed", "sha256": "b" * 64}])
    from agentic_host.host_update import canonical
    body = {"manifest": manifest, "signature": base64.b64encode(lab._test.private.sign(canonical(manifest))).decode()}
    lab._test.archives[manifest["bundle"]["url"]] = (output / "agentic-home-host-0.11.1.tar").read_bytes()
    record = lab.stage(body, background=False)
    assert record["state"] == "staged"
    assert (lab.bundles / record["candidate_digest"] / "docs/HOST_UPDATES.md").read_bytes() == files["docs/HOST_UPDATES.md"]
    candidate = lab.bundles / record["candidate_digest"]
    assert (candidate / "images.lock.json").read_bytes() == old_images
    assert (candidate / "host/compose.yaml").read_bytes() == old_compose
    assert (source / "images.lock.json").read_bytes() == files["images.lock.json"]
    assert (source / "host/compose.yaml").read_bytes() == files["host/compose.yaml"]
    assert runtime.read_bytes() == runtime_before  # separately approved Controller override survives
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



def test_archive_uses_authenticated_baseline_runtime_without_modifying_new_product(product, tmp_path):
    output = tmp_path / "output"
    baseline = product.parent / "baseline-product"
    originals = {name: (product / name).read_bytes() for name in build_module.LOCKS}
    build(product, output)
    with tarfile.open(output / "agentic-home-host-0.11.1.tar") as archive:
        for name in build_module.LOCKS:
            assert archive.extractfile(name).read() == (baseline / name).read_bytes()
            assert (product / name).read_bytes() == originals[name]
    assert b"0.12.0" in originals["host/compose.yaml"]


@pytest.mark.parametrize("fault", ["missing-index", "modified", "symlink", "missing-runtime"])
def test_builder_rejects_unverified_baseline_runtime_inputs(product, tmp_path, fault):
    baseline = product.parent / "baseline-product"
    if fault == "missing-index":
        (baseline / "BUNDLE.sha256.json").unlink()
    elif fault == "modified":
        (baseline / "images.lock.json").write_text("{}")
    elif fault == "symlink":
        (baseline / "host/compose.yaml").unlink()
        (baseline / "host/compose.yaml").symlink_to(product / "host/compose.yaml")
    else:
        (baseline / "host/network.lock.json").unlink()
    with pytest.raises(ValueError, match="baseline"):
        build(product, tmp_path / "output")
