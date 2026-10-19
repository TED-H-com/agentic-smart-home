#!/usr/bin/env python3
"""Stage an UNSIGNED one-time owner bootstrap Debian package.

Inputs require an actual reviewed public pin and externally signed Host and
Controller envelopes. This does not generate keys, sign, publish, authenticate
the initial package, or install anything. Distribution must authenticate the
result through a reviewed signed OS repository or separately authenticated media.
"""
import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host.bootstrap_owner import POSTINST, RECOVERY_NAME, RECOVERY_UNIT, file_digest  # noqa: E402
from agentic_host.host_update import IMAGE_REPOSITORY, PUBLISHER, REPOSITORY, canonical, validate_manifest  # noqa: E402
from agentic_host.publisher import public_key  # noqa: E402
from agentic_host.updater import validate_manifest as controller_manifest  # noqa: E402


def envelope(path, pem, *, controller=False):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
        raise ValueError("A bounded signed envelope file is required")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {"manifest", "signature"}:
        raise ValueError("A signed envelope is required")
    (controller_manifest if controller else validate_manifest)(value["manifest"])
    manifest = value["manifest"]
    if manifest["publisher"] != PUBLISHER or (controller and manifest["image"].split("@", 1)[0] != IMAGE_REPOSITORY):
        raise ValueError("Only the fixed official publisher is permitted")
    signature = base64.b64decode(value["signature"], validate=True)
    if len(signature) != 64:
        raise ValueError("An Ed25519 signature is required")
    with tempfile.TemporaryDirectory(prefix="agentic-bootstrap-signature-") as temporary:
        directory = Path(temporary)
        (directory / "public.pem").write_text(pem)
        (directory / "manifest").write_bytes(canonical(manifest))
        (directory / "signature").write_bytes(signature)
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(directory / "public.pem"),
                                 "-rawin", "-in", str(directory / "manifest"), "-sigfile", str(directory / "signature")],
                                capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("The envelope does not match the reviewed official public key")
    return value


def build(product, host_manifest, host_bundle, controller_update, controller_images, destination, *, package=True):
    product, destination = Path(product), Path(destination)
    descriptor = product / "host/official-update-channel.json"
    if descriptor.is_symlink() or descriptor.stat().st_size > 16384:
        raise ValueError("A real reviewed public pin is required")
    value = json.loads(descriptor.read_text())
    expected = {"format", "publisher", "repository", "channel", "image_repository", "pin"}
    if (not isinstance(value, dict) or set(value) != expected or type(value["format"]) is not int or value["format"] != 1
            or value["publisher"] != PUBLISHER or value["repository"] != REPOSITORY or value["channel"] != "stable"
            or value["image_repository"] != IMAGE_REPOSITORY or not isinstance(value["pin"], dict)
            or set(value["pin"]) != {"public_key_pem", "key_sha256"}):
        raise ValueError("A real reviewed official public pin is required; null and fixture defaults are unavailable")
    pem, fingerprint = public_key(value["pin"]["public_key_pem"])
    if fingerprint != value["pin"]["key_sha256"]:
        raise ValueError("The reviewed public-key fingerprint does not match")
    host = envelope(host_manifest, pem)
    envelope(controller_update, pem, controller=True)
    archive = Path(host_bundle)
    cache = Path(controller_images)
    if (archive.is_symlink() or not archive.is_file() or archive.stat().st_size != host["manifest"]["bundle"]["size"]
            or file_digest(archive) != host["manifest"]["bundle"]["sha256"]):
        raise ValueError("Host archive does not match the signed envelope")
    if cache.is_symlink() or not cache.is_file() or not 0 < cache.stat().st_size <= 16 * 1024**3:
        raise ValueError("A bounded local Controller OCI cache is required")
    stage = destination / "bootstrap-package"
    if stage.exists():
        raise ValueError("Refusing to overwrite an existing package stage")
    payload = stage / "usr/share/agentic-home-update-bootstrap"
    code = payload / "product/host/agentic_host"
    code.mkdir(parents=True)
    sources = product / "host/agentic_host"
    for source in sorted(sources.rglob("*.py")):
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 4 * 1024**2:
            raise ValueError("Bootstrap code requires bounded regular public Python files")
        target = code / source.relative_to(sources)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    for name in ("bootstrap_owner.py", "host_update.py", "host_restore.py", "host_reconcile.py", "update_bootstrap.py"):
        if not (code / name).is_file():
            raise ValueError("Bootstrap product is incomplete")
    shutil.copyfile(descriptor, payload / "product/host/official-update-channel.json")
    units = payload / "product/host/systemd"
    units.mkdir()
    for name in ("agentic-home-update.service", "agentic-home-update-recover.service"):
        source = product / "host/systemd" / name
        if source.is_symlink() or not source.is_file():
            raise ValueError("Fixed update/recovery services are required")
        shutil.copyfile(source, units / name)
    for source, name in ((host_manifest, "host-update.json"), (archive, "host-bundle.tar"),
                         (controller_update, "controller-update.json"), (cache, "controller-images.tar")):
        shutil.copyfile(source, payload / name)
    binary = stage / "usr/bin/agentic-home-update-bootstrap"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexec /usr/bin/python3 -I -c 'import runpy,sys;"
                      "sys.path.insert(0,\"/usr/share/agentic-home-update-bootstrap/product/host\");"
                      "runpy.run_module(\"agentic_host.bootstrap_owner\",run_name=\"__main__\")' \"$@\"\n")
    binary.chmod(0o755)
    desktop = stage / "usr/share/applications"
    desktop.mkdir()
    shutil.copyfile(ROOT / "host/desktop/agentic-home-update-bootstrap.desktop", desktop / "agentic-home-update-bootstrap.desktop")
    control = stage / "DEBIAN"
    control.mkdir()
    version = host["manifest"]["version"]
    (control / "control").write_text("Package: agentic-home-update-bootstrap\nVersion: " + version +
        "\nArchitecture: amd64\nMaintainer: Agentic Home maintainers\nDepends: python3, openssl, policykit-1, zenity, lvm2\n"
        "Description: One-time owner-approved Agentic Home Host updater bootstrap\n")
    (control / "postinst").write_text(POSTINST)
    (control / "postinst").chmod(0o755)
    recovery = stage / "etc/systemd/system"
    recovery.mkdir(parents=True)
    (recovery / RECOVERY_NAME).write_text(RECOVERY_UNIT)
    (payload / "AUTHENTICATION-REQUIRED.txt").write_text(
        "UNSIGNED BUILD OUTPUT. SHA256 hashes and bundled public keys do not authenticate this initial package.\n"
        "Use only after an independent official signed OS repository or authenticated media verifies this package.\n"
        "No signing key was generated and no release was signed or published by this builder.\n")
    for path in stage.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path not in {binary, control / "postinst"}:
            path.chmod(0o644)
    output = destination / ("agentic-home-update-bootstrap_" + version + "_amd64.UNSIGNED.deb")
    if package:
        if output.exists():
            raise ValueError("Refusing to overwrite an existing package")
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(output)], check=True, timeout=300)
    return {"stage": str(stage), "unsigned_package": str(output) if package else None, "key_sha256": fingerprint,
            "authentication": "external_authenticated_distribution_required"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("product", "host-manifest", "host-bundle", "controller-update", "controller-images", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.product, args.host_manifest, args.host_bundle, args.controller_update,
                           args.controller_images, args.destination), indent=2))


if __name__ == "__main__":
    main()
