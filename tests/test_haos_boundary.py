"""Actual HAOS App namespace evidence. Every disk, App and credential is disposable."""
import base64
import contextlib
import json
import lzma
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PYTHON = "python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
BUILDER = "docker:29.7.2-cli@sha256:3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e"


def docker(*args, timeout=120):
    return subprocess.check_output(["docker", *args], text=True, timeout=timeout).strip()


def test_actual_haos_app_boundary(tmp_path, request):
    if not request.config.getoption("--haos-vm"):
        pytest.skip("Actual HAOS App probe requires --haos-vm")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    name = "agentic-haos-boundary-" + uuid.uuid4().hex[:10]
    with lzma.open(ROOT / "build/cache/haos_ova-18.3.qcow2.xz", "rb") as source, (tmp_path / "haos.qcow2").open("wb") as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
    for reference in (PYTHON, BUILDER):
        docker("pull", reference, timeout=180)
    # Preload exact public images, avoiding slow repeated downloads through QEMU user networking.
    subprocess.run(["docker", "save", "-o", str(tmp_path / "images.tar"), "docker:29.7.2-cli", PYTHON],
                   check=True, timeout=120)
    shutil.copyfile(ROOT / "tests/haos_boundary_probe.py", tmp_path / "probe.py")
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "key")], check=True, capture_output=True)
    (tmp_path / "config").mkdir()
    shutil.copyfile(tmp_path / "key.pub", tmp_path / "config/authorized_keys")
    with (tmp_path / "config.ext4").open("wb") as stream:
        stream.truncate(32 * 1024**2)
    subprocess.run(["/usr/sbin/mkfs.ext4", "-q", "-F", "-L", "CONFIG", "-d", str(tmp_path / "config"),
                    str(tmp_path / "config.ext4")], check=True, capture_output=True)
    try:
        docker("run", "-d", "--name", name, "--device", "/dev/kvm", "--memory", "4096m", "--cpus", "3",
               "-p", "127.0.0.1::2222", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0", "bash", "-ec",
               "cp /usr/share/OVMF/OVMF_VARS_4M.fd /lab/nvram.fd\nqemu-img resize /lab/haos.qcow2 64G\n"
               "exec qemu-system-x86_64 -enable-kvm -cpu host -machine q35 -m 3072 -smp 2 "
               "-drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd "
               "-drive if=pflash,format=raw,file=/lab/nvram.fd "
               "-drive file=/lab/haos.qcow2,format=qcow2,if=none,id=disk "
               "-device virtio-scsi-pci -device scsi-hd,drive=disk "
               "-drive file=/lab/config.ext4,format=raw,if=virtio "
               "-netdev user,id=net,hostfwd=tcp:0.0.0.0:2222-:22222 -device virtio-net-pci,netdev=net -display none "
               "-serial file:/lab/serial.log", timeout=600)
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["2222/tcp"][0]["HostPort"]
        ssh = ["ssh", "-i", str(tmp_path / "key"), "-p", port, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}",
               "-o", "ConnectTimeout=5", "root@127.0.0.1"]
        for _ in range(120):
            # A probe that times out on a loaded host is only not ready yet.
            with contextlib.suppress(subprocess.TimeoutExpired):
                if subprocess.run([*ssh, "true"], capture_output=True, timeout=8).returncode == 0:
                    break
            time.sleep(3)
        else:
            raise AssertionError("HAOS official CONFIG-key SSH import did not become ready")

        def shell(command, timeout=90):
            result = subprocess.run([*ssh, "sh"], input=command, capture_output=True, text=True, timeout=timeout)
            return result.stdout + result.stderr

        with (tmp_path / "images.tar").open("rb") as images:
            loaded = subprocess.run([*ssh, "docker load"], stdin=images, capture_output=True, timeout=180)
        assert loaded.returncode == 0, "Pinned diagnostic image preload failed"
        evidence = {"observed_from": "real_haos_app", "haos_image": "18.3", "cloud_account": "not_used",
                    "diagnostic_base": PYTHON, "supervisor_builder": BUILDER,
                    "runner_coding_executed": False, "profiles": {}}
        for label, capabilities in (("protected_baseline", []), ("narrow_sys_admin", ["SYS_ADMIN"])):
            slug = "agentic_boundary_" + label
            config = {"name": "Disposable boundary probe", "version": "1.0.0", "slug": slug,
                      "description": "Isolated test only", "arch": ["amd64"], "startup": "application",
                      "boot": "manual", "init": False, "apparmor": True, "hassio_api": False,
                      "homeassistant_api": False, "docker_api": False, "full_access": False,
                      "host_pid": False, "host_network": False, "privileged": capabilities,
                      "options": {}, "schema": {}}
            directory = "/mnt/data/supervisor/apps/local/" + slug
            apparmor = """#include <tunables/global>
profile agentic-boundary flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  file,
  network,
  capability,
  signal,
  mount fstype=cgroup2 -> /data/boundary-probe/cgroup/,
  umount /data/boundary-probe/cgroup/,
}
"""
            commands = ["mkdir -p " + directory]
            files = {"probe.py": (tmp_path / "probe.py").read_text(), "config.json": json.dumps(config), "Dockerfile":
                     'FROM '+evidence['diagnostic_base']+'\nCOPY probe.py /probe.py\nCMD ["python","/probe.py"]\n'}
            if capabilities:
                files["apparmor.txt"] = apparmor.replace("profile agentic-boundary ", "profile " + slug + " ")
            for filename, content in files.items():
                commands.append("echo " + base64.b64encode(content.encode()).decode() + " | base64 -d > " + directory + "/" + filename)
            shell("\n".join(commands))
            # Wait for Supervisor readiness, then use its real local-App install/start APIs.
            for _ in range(90):
                shell("ha store reload", 30)
                output = shell("ha apps info local_" + slug + " --raw-json", 30)
                try:
                    if json.loads(output).get("data", {}).get("slug") == "local_" + slug:
                        break
                except ValueError:
                    pass
                time.sleep(3)
            update_requested = False
            install_deadline = time.monotonic() + 600
            installed = "not attempted"
            while time.monotonic() < install_deadline:
                observed = shell("ha apps info local_" + slug + " --raw-json", 30)
                try:
                    if json.loads(observed).get("data", {}).get("installed") is True:
                        installed = "already installed after retry"
                        break
                except ValueError:
                    pass
                try:
                    installed = shell("ha apps install local_" + slug, 90)
                except subprocess.TimeoutExpired:
                    installed = "Error: install in progress; awaiting real Supervisor readback"
                if "Error:" not in installed and '"result":"error"' not in installed:
                    break
                if "supervisor needs to be updated first" in installed and not update_requested:
                    shell("ha supervisor update", 240)
                    update_requested = True
                elif not any(message in installed.lower() for message in
                             ("updated first", "in progress", "connection refused", "not ready", "starting", "already installed")):
                    break
                time.sleep(3)
            assert "Error:" not in installed and '"result":"error"' not in installed, installed[-1000:] + shell("ha supervisor logs | tail -20")[-3000:]
            started = shell("ha apps start local_" + slug, 120)
            assert "Error:" not in started, started[-1000:]
            time.sleep(25)
            logs = shell("ha apps logs local_" + slug)
            line = next((line for line in logs.splitlines() if line.startswith("AGENTIC_BOUNDARY=")), None)
            assert line, "Actual App did not produce bounded diagnostic: " + logs[-1500:]
            result = json.loads(line.removeprefix("AGENTIC_BOUNDARY="))
            host_namespace = int(shell("stat -Lc %i /proc/self/ns/cgroup").strip())
            result["distinct_from_haos_host_cgroup_namespace"] = result["cgroup_namespace"] != host_namespace
            if result["delegation"].get("delegated_limits"):
                assert result["distinct_from_haos_host_cgroup_namespace"] and result["cgroup_membership"] == "0::/"
            assert result["observed_from"] == "haos_app" and not result["docker_socket_present"]
            evidence["profiles"][label] = result
            (ROOT / "evidence/haos-app-boundary.json").write_text(json.dumps(evidence, indent=2) + "\n")
            shell("ha apps stop local_" + slug)
        (ROOT / "evidence/haos-app-boundary.json").write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        subprocess.run(["docker", "stop", "-t", "10", name], capture_output=True)
        subprocess.run(["docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/lab"], capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
