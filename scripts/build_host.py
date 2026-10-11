#!/usr/bin/env python3
"""Build a complete Linux product bundle and optionally a bootable Ubuntu installer ISO."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))
from agentic_host.artifacts import fetch, sha256  # noqa: E402
from agentic_host.manager import validate_profile  # noqa: E402

VERSION = "0.3.1"
SKIP = {"__pycache__", "node_modules", ".pytest_cache", ".ruff_cache", "test-results"}


def command(*args):
    subprocess.run(args, check=True)


def xorriso(*args):
    if shutil.which("xorriso"):
        return command("xorriso", *map(str, args))
    return command("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}",
                   "-v", f"{ROOT}:{ROOT}", "-w", str(ROOT), "agentic-home-builder:0.2.0", "xorriso", *map(str, args))


def autoinstall(profile):
    # Storage, network and identity are the actual OS installer's screens, not development gates.
    # No target disk, reusable user password or preauthorized SSH key is embedded in a public ISO.
    return {"autoinstall": {"version": 1, "interactive-sections": ["network", "storage", "identity"],
        "locale": "en_US.UTF-8", "keyboard": {"layout": "us"}, "refresh-installer": {"update": False},
        "ssh": {"install-server": True, "allow-pw": True},
        "late-commands": ["cd /cdrom/agentic-home && sha256sum -c SHA256SUMS", "mkdir -p /target/opt", "tar -xf /cdrom/agentic-home/product.tar -C /target/opt",
                          "/usr/bin/python3 /target/opt/agentic-home/host/install.py --stage-target /target --profile " + profile["id"]],
        "shutdown": "reboot"}}


def boot_config(old, loopback=False):
    # Retain upstream rescue entries and the signed upstream boot chain; add the product entry first.
    extra = " iso-scan/filename=${iso_path}" if loopback else ""
    menu = ('set timeout=15\nmenuentry "Install Agentic Home Linux" {\n'
            '  set gfxpayload=keep\n'
            '  linux /casper/vmlinuz autoinstall ds=nocloud\\;s=/cdrom/agentic-seed/' + extra + ' ---\n'
            '  initrd /casper/initrd\n}\n')
    return menu + old.replace("set timeout=30", "set timeout=15").replace("\ngrub_platform\n", "\n")


def bundle(destination):
    from package import package_runner
    package_runner()
    stage = ROOT / "build/product/agentic-home"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for directory in ("host", "controller", "runner", "contracts", "docs", "scripts"):
        shutil.copytree(ROOT / directory, stage / directory,
                        ignore=shutil.ignore_patterns(*SKIP, "*.pyc", "*.tsbuildinfo"))
    for name in ("README.md", "INSTALL.md", "PROJECT_PLAN.md", "PROJECT_SPEC.md", "providers.lock.json", "upstream.lock.json", "channels.lock.json"):
        shutil.copyfile(ROOT / name, stage / name)
    assets = stage / "assets"
    assets.mkdir()
    lock = json.loads((ROOT / "host/bases.lock.json").read_text())
    artifact = fetch(lock["haos"], ROOT / "build/cache")
    os.link(artifact, assets / artifact.name)
    tags = ["agentic-home-controller:" + VERSION, "agentic-home-runner:" + VERSION, "agentic-home-linejs:" + VERSION]
    images = {tag: subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True).strip()
              for tag in tags}
    (stage / "images.lock.json").write_text(json.dumps(images, indent=2) + "\n")
    command("docker", "save", "-o", str(assets / "agentic-images.tar"), *tags)
    manifest = {str(path.relative_to(stage)): sha256(path) for path in sorted(stage.rglob("*")) if path.is_file()}
    (stage / "BUNDLE.sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Uncompressed tar avoids an extra compression/decompression of already-compressed OCI and XZ content.
    with tarfile.open(destination, "w") as output:
        output.add(stage, arcname="agentic-home", recursive=True)
    return destination


def iso(profile, product, destination):
    bases = json.loads((ROOT / "host/bases.lock.json").read_text())
    base = fetch(bases["bases"][profile["base"]], ROOT / "build/cache")
    with tempfile.TemporaryDirectory(prefix="iso-", dir=ROOT / "build") as temporary:
        work = Path(temporary)
        seed = work / "seed"
        seed.mkdir()
        (seed / "user-data").write_text("#cloud-config\n" + json.dumps(autoinstall(profile), indent=2) + "\n")
        (seed / "meta-data").write_text("instance-id: agentic-home-installer\n")
        xorriso("-osirrox", "on", "-indev", base, "-extract", "/boot/grub/grub.cfg", work / "grub.cfg",
                "-extract", "/boot/grub/loopback.cfg", work / "loopback.cfg",
                "-extract", "/md5sum.txt", work / "md5sum.txt")
        for name, loopback in (("grub.cfg", False), ("loopback.cfg", True)):
            path = work / name
            path.chmod(0o644)
            path.write_text(boot_config(path.read_text(), loopback))
        (work / "product.sha256").write_text(sha256(product) + "  product.tar\n")
        replacements = {"./boot/grub/grub.cfg": work / "grub.cfg", "./boot/grub/loopback.cfg": work / "loopback.cfg",
                        "./agentic-seed/user-data": seed / "user-data", "./agentic-seed/meta-data": seed / "meta-data",
                        "./agentic-home/product.tar": product, "./agentic-home/SHA256SUMS": work / "product.sha256"}
        retained = [line for line in (work / "md5sum.txt").read_text().splitlines()
                    if line.split(maxsplit=1)[-1] not in replacements]
        for name, path in replacements.items():
            with path.open("rb") as stream:
                retained.append(hashlib.file_digest(stream, "md5").hexdigest() + "  " + name)
        (work / "md5sum.txt").chmod(0o644)
        (work / "md5sum.txt").write_text("\n".join(retained) + "\n")
        partial = destination.with_suffix(".partial.iso")
        partial.unlink(missing_ok=True)
        try:
            xorriso("-indev", base, "-outdev", partial, "-boot_image", "any", "replay",
                    "-volid", "AGENTIC_HOME_031", "-map", product, "/agentic-home/product.tar",
                    "-map", work / "product.sha256", "/agentic-home/SHA256SUMS",
                    "-map", seed, "/agentic-seed", "-map", work / "grub.cfg", "/boot/grub/grub.cfg",
                    "-map", work / "loopback.cfg", "/boot/grub/loopback.cfg",
                    "-map", work / "md5sum.txt", "/md5sum.txt", "-commit")
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="laptop-amd64", choices=[p.stem for p in (ROOT / "host/profiles").glob("*.json")])
    parser.add_argument("--iso", action="store_true", help="Also remaster the selected official Linux installer")
    parser.add_argument("--reuse-bundle", action="store_true")
    args = parser.parse_args()
    profile = json.loads((ROOT / "host/profiles" / (args.profile + ".json")).read_text())
    validate_profile(profile, json.loads((ROOT / "host/bases.lock.json").read_text()))
    (ROOT / "build").mkdir(exist_ok=True)
    distribution = ROOT / "dist"
    distribution.mkdir(exist_ok=True)
    product = distribution / f"agentic-home-{VERSION}-linux-amd64.tar"
    if not args.reuse_bundle:
        bundle(product)
    if args.iso:
        iso(profile, product, distribution / f"agentic-home-{VERSION}-{args.profile}.iso")
    artifacts = [p for p in distribution.glob(f"agentic-home-{VERSION}-*") if p.suffix in {".tar", ".iso", ".zip"}]
    (distribution / "SHA256SUMS").write_text("".join(sha256(path) + "  " + path.name + "\n" for path in sorted(artifacts)))
    print("Built " + product.name + (" and " + args.profile + " ISO" if args.iso else ""))


if __name__ == "__main__":
    main()
