#!/usr/bin/env python3
"""Install the product onto an existing, matching Linux. Does not partition or erase disks."""
import argparse
import json
import os
import platform
import pwd
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentic_host import VERSION  # noqa: E402
from agentic_host.artifacts import sha256  # noqa: E402
from agentic_host.manager import HostError, atomic, validate_profile  # noqa: E402
from agentic_host.source import provision_source  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path("/opt/agentic-home")
STATE = Path("/var/lib/agentic-home")
PACKAGES = ["qemu-kvm", "qemu-utils", "libvirt-daemon-system", "libvirt-clients", "ovmf", "dnsmasq-base",
            "docker.io", "docker-compose-v2", "python3", "ca-certificates", "curl", "openssh-server",
            "git", "e2fsprogs", "util-linux", "lvm2"]


def run(*args):
    subprocess.run(args, check=True)


def verify_bundle(root):
    manifest = root / "BUNDLE.sha256.json"
    if not manifest.is_file():
        raise HostError("Release manifest is missing; build/extract the host install bundle first")
    for name, expected in json.loads(manifest.read_text()).items():
        path = root / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or not path.is_file():
            raise HostError("Invalid bundle path: " + name)
        if sha256(path) != expected:
            raise HostError("Bundle checksum mismatch: " + name)


def stage_target(target, profile):
    """Called by the upstream installer inside its live environment; no host services are touched."""
    target = Path(target).resolve()
    if target == Path("/") or not (target / "etc/os-release").exists():
        raise HostError("--stage-target must be an installed target filesystem")
    verify_bundle(ROOT)
    dest = target / "opt/agentic-home"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if ROOT.resolve() != dest.resolve():
        shutil.copytree(ROOT, dest, dirs_exist_ok=True)
    atomic(target / "etc/agentic-home/profile.json", profile)
    units = target / "etc/systemd/system"
    shutil.copyfile(ROOT / "host/systemd/agentic-home-firstboot.service", units / "agentic-home-firstboot.service")
    wants = units / "multi-user.target.wants"
    wants.mkdir(exist_ok=True)
    link = wants / "agentic-home-firstboot.service"
    link.unlink(missing_ok=True)
    link.symlink_to("../agentic-home-firstboot.service")


def preflight(profile, bases):
    info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v.strip('"')
    base = bases["bases"][profile["base"]]
    if info.get("ID") != base["os_id"] or info.get("VERSION_ID") != base["os_version"]:
        raise HostError("Installed Linux does not match the selected locked base")
    if platform.machine() != "x86_64":
        raise HostError("This profile requires x86-64; ARM needs a separate port")
    if not Path("/dev/kvm").exists():
        raise HostError("KVM unavailable; enable hardware virtualization in firmware")
    memory = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                      if line.startswith("MemTotal:"))) // 1024
    if memory < profile["minimum_host_memory_mib"]:
        raise HostError("Insufficient host RAM for this profile")
    if shutil.disk_usage("/var/lib").free < 40 * 1024**3:
        raise HostError("At least 40 GiB free disk space is required before installing")


def desktop_login_note(access):
    # The product owner matches the Controller's fixed host UID. Do not share
    # the management key with additional unprivileged Linux accounts.
    for account in pwd.getpwall():
        if account.pw_uid != 1000 or not Path(account.pw_dir).is_dir():
            continue
        note = Path(account.pw_dir) / "Agentic-Home.txt"
        note.write_text("Agentic Home\nhttp://localhost:8099\n主機登入金鑰：" + access.read_text() +
                        "\n請保管此檔案。HA 原生設定：http://localhost:8123\n")
        os.chown(note, account.pw_uid, account.pw_gid)
        note.chmod(0o600)


def install(profile, bases):
    preflight(profile, bases)
    verify_bundle(ROOT)
    if ROOT != TARGET:
        if TARGET.exists():
            raise HostError("/opt/agentic-home exists; use a versioned update, do not overwrite an installation")
        shutil.copytree(ROOT, TARGET)
    atomic("/etc/agentic-home/profile.json", profile)
    STATE.mkdir(parents=True, exist_ok=True)
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    run("apt-get", "update")
    packages = PACKAGES + (["ubuntu-desktop-minimal", "firefox", "language-pack-zh-hant", "dconf-cli"] if profile["desktop"] else [])
    run("apt-get", "install", "-y", *packages)
    network = json.loads((TARGET / "host/network.lock.json").read_text())["tailscale"]
    deb = TARGET / "assets" / network["filename"]
    if Path(network["filename"]).name != network["filename"] or deb.is_symlink() or sha256(deb) != network["sha256"]:
        raise HostError("Network package checksum mismatch")
    run("apt-get", "install", "-y", str(deb))
    provision_source(TARGET, STATE)
    for directory, mode, uid, gid in (("controller", 0o700, 1000, 1000), ("runner", 0o700, 0, 0),
                                      ("pairing", 0o750, 0, 1000), ("host", 0o700, 0, 0),
                                      ("hermes", 0o700, 10003, 10003), ("hermes-link", 0o750, 0, 1000),
                                      ("linejs", 0o700, 1000, 1000), ("line-config", 0o700, 1000, 1000)):
        path = STATE / directory
        path.mkdir(exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    access = STATE / "controller/access-key"
    if not access.exists():
        access.write_text(secrets.token_urlsafe(32) + "\n")
    os.chown(access, 1000, 1000)
    os.chmod(access, 0o600)
    hermes_link = STATE / "hermes-link/service-token"
    if not hermes_link.exists():
        hermes_link.write_text(secrets.token_urlsafe(48) + "\n")
    os.chown(hermes_link, 0, 1000)
    os.chmod(hermes_link, 0o640)
    runtime_environment = STATE / "host/runtime.env"
    if not runtime_environment.exists():
        runtime_environment.write_text("# Initial Hermes uses the bundled image until setup verifies a candidate.\n")
        runtime_environment.chmod(0o600)
    for unit in (TARGET / "host/systemd").glob("*.service"):
        shutil.copyfile(unit, Path("/etc/systemd/system") / unit.name)
    cli = Path("/usr/local/bin/agentic-home")
    cli.write_text('#!/bin/sh\nexport PYTHONPATH=/opt/agentic-home/host\nexec /usr/bin/python3 -m agentic_host.cli "$@"\n')
    cli.chmod(0o755)
    if profile["appliance_lid"]:
        folder = Path("/etc/systemd/logind.conf.d")
        folder.mkdir(exist_ok=True)
        (folder / "60-agentic-home.conf").write_text("[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n")
    if profile["desktop"]:
        dconf = Path("/etc/dconf/db/local.d")
        dconf.mkdir(parents=True, exist_ok=True)
        (dconf / "60-agentic-home").write_text("[org/gnome/settings-daemon/plugins/power]\n"
                                               "sleep-inactive-ac-type='nothing'\nsleep-inactive-ac-timeout=0\n")
        dconf_profile = Path("/etc/dconf/profile/user")
        dconf_profile.parent.mkdir(parents=True, exist_ok=True)
        previous = dconf_profile.read_text() if dconf_profile.exists() else "user-db:user\n"
        if "system-db:local" not in previous.splitlines():
            dconf_profile.write_text(previous.rstrip() + "\nsystem-db:local\n")
        run("dconf", "update")
        shutil.copyfile(TARGET / "host/desktop/agentic-home.desktop", "/usr/share/applications/agentic-home.desktop")
        autostart = Path("/etc/xdg/autostart")
        autostart.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TARGET / "host/desktop/agentic-home.desktop", autostart / "agentic-home.desktop")
        # Normal GNOME login remains; no default password or automatic OS login.
        run("systemctl", "set-default", "graphical.target")
        desktop_login_note(access)
    # Only product images are loaded; manifests pin image IDs separately from version tags.
    run("systemctl", "enable", "--now", "docker", "libvirtd", "ssh", "tailscaled")
    run("docker", "load", "-i", str(TARGET / "assets/agentic-images.tar"))
    images = json.loads((TARGET / "images.lock.json").read_text())
    for tag, expected in images.items():
        actual = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True).strip()
        if actual != expected:
            raise HostError("Loaded image ID differs from release: " + tag)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "agentic-home-host", "agentic-home-proxy")
    run("systemctl", "enable", "agentic-home")
    pair_runner()
    # Firstboot starts the outer services; restart also covers a repeated install. The durable
    # Web onboarding creates HAOS automatically after CLI, Hermes and channel setup, or an explicit skip.
    run("systemctl", "restart", "agentic-home")
    record_installation(profile)
    print("Agentic Home installed. Open http://localhost:8099; view local login key: sudo agentic-home access-key")
    if profile["desktop"]:
        run("systemctl", "start", "gdm3")


PAIR_RUNNER = """import asyncio,json,sys,uuid
from pathlib import Path
from agentic_controller.store import Store
from agentic_controller.runner import Runner
async def main():
 s=Store(Path('/data/controller.sqlite3'))
 p=json.load(sys.stdin)
 cid=s.setting('controller_id') or uuid.uuid4().hex
 s.set_setting('controller_id',cid)
 if not s.setting('runner'):
  await Runner(s).pair(p,'host-owner',cid)
  s.set_setting('onboarding',{**s.setting('onboarding',{}),'runner_paired':True})
 s.close()
asyncio.run(main())
"""


def pair_runner():
    """Pair automatically as part of trusted machine bootstrap (no model sees this code or HA credentials).

    A running Controller keeps its database in memory and is its only writer, so a one-off
    Controller container writes the pairing while the service is stopped.
    """
    compose = ["docker", "compose", "--env-file", str(STATE / "host/runtime.env"), "-f", str(TARGET / "host/compose.yaml")]
    run(*compose, "stop", "controller")
    subprocess.run([*compose, "up", "-d", "--no-deps", "--wait", "runner"], check=True, timeout=600)
    pair_file = STATE / "pairing/runner.json"
    pairing = json.loads(pair_file.read_text()) if pair_file.exists() else {}
    payload = {**pairing, "principal": "host-owner", "protocol": 1}
    subprocess.run([*compose, "run", "--rm", "--no-deps", "-T", "-u", "1000:1000", "controller", "python", "-c", PAIR_RUNNER],
                   input=json.dumps(payload), text=True, check=True, timeout=600)


def record_installation(profile):
    atomic(STATE / "installed.json", {"version": VERSION, "profile": profile["id"], "installed_at": time.time()})
    # Nonsecret release metadata remains available to the fixed owner console.
    os.chown(STATE / "installed.json", 0, 1000)
    os.chmod(STATE / "installed.json", 0o640)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="laptop-amd64")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--stage-target")
    args = parser.parse_args()
    if not args.profile.replace("-", "").isalnum():
        raise HostError("Invalid profile name")
    profile_path = Path("/etc/agentic-home/profile.json")
    if not profile_path.exists() or args.stage_target:
        profile_path = ROOT / "host/profiles" / (args.profile + ".json")
    profile = json.loads(profile_path.read_text())
    bases = json.loads((ROOT / "host/bases.lock.json").read_text())
    validate_profile(profile, bases)
    if not args.apply and not args.stage_target:
        print(json.dumps({"profile": profile, "packages": PACKAGES, "target": str(TARGET),
                          "partitions_disks": False}, ensure_ascii=False, indent=2))
        return
    if os.geteuid() != 0:
        raise HostError("Installation requires root on the target machine")
    if args.stage_target:
        stage_target(args.stage_target, profile)
    else:
        install(profile, bases)


if __name__ == "__main__":
    try:
        main()
    except (HostError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
