"""Full Linux installation rehearsal. Every disk/credential is temporary and isolated."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def command(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def test_full_linux_install_with_nested_haos(tmp_path, request):
    if not request.config.getoption("--linux-install"):
        pytest.skip("Use --linux-install to install onto a disposable virtual disk")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    profile = os.environ.get("AGENTIC_INSTALL_TEST_PROFILE", "laptop-amd64")
    assert profile in {"laptop-amd64", "server-amd64"}
    iso = ROOT / "dist" / ("agentic-home-0.3.1-" + profile + ".iso")
    assert iso.exists()
    with iso.open("rb") as stream:
        iso_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    spec = importlib.util.spec_from_file_location("builder", ROOT / "scripts/build_host.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    command("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "test-key"))
    config = build.autoinstall({"id": profile})
    autoinstall = config["autoinstall"]
    autoinstall.pop("interactive-sections")
    autoinstall["identity"] = {"hostname": "agentic-disposable", "username": "tester", "password": "!"}
    autoinstall["ssh"] = {"install-server": True, "allow-pw": False,
                          "authorized-keys": [(tmp_path / "test-key.pub").read_text().strip()]}
    autoinstall["storage"] = {"layout": {"name": "direct", "match": {"serial": "AGENTIC_TEST_ONLY"}}}
    autoinstall["shutdown"] = "poweroff"
    autoinstall["late-commands"] += [
        "printf 'tester ALL=(root) NOPASSWD: ALL\\n' > /target/etc/sudoers.d/90-agentic-test",
        "chmod 440 /target/etc/sudoers.d/90-agentic-test"]
    (tmp_path / "user-data").write_text("#cloud-config\n" + json.dumps(config, indent=2))
    # Connect from QEMU's loopback: Docker's outer 172.17/16 otherwise overlaps
    # Docker inside the guest after firstboot and breaks the return route.
    (tmp_path / "ssh-proxy.py").write_text('''import socket,sys,threading
s=socket.create_connection(('127.0.0.1',2222))
def forward():
 while data:=sys.stdin.buffer.read1(65536): s.sendall(data)
 s.shutdown(socket.SHUT_WR)
threading.Thread(target=forward,daemon=True).start()
while data:=s.recv(65536):
 sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
''')
    name = "agentic-linux-install-test-" + uuid.uuid4().hex[:10]
    started = time.time()
    try:
        command("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tmp_path}:/lab",
                "-v", f"{iso}:/source.iso:ro", "agentic-home-builder:0.2.0", "xorriso",
                "-indev", "/source.iso", "-outdev", "/lab/test.iso", "-boot_image", "any", "replay",
                "-map", "/lab/user-data", "/agentic-seed/user-data", "-commit")
        command("docker", "run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{tmp_path}:/lab",
                "agentic-home-builder:0.2.0", "qemu-img", "create", "-f", "qcow2", "/lab/linux.qcow2", "120G")
        qemu = ["qemu-system-x86_64", "-enable-kvm", "-cpu", "host", "-machine", "q35", "-m", "10240", "-smp", "4",
                "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd",
                "-drive", "if=pflash,format=raw,file=/lab/nvram.fd",
                "-drive", "file=/lab/linux.qcow2,format=qcow2,if=none,id=disk",
                "-device", "virtio-blk-pci,drive=disk,serial=AGENTIC_TEST_ONLY",
                "-netdev", "user,id=net,hostfwd=tcp:0.0.0.0:2222-:22", "-device", "virtio-net-pci,netdev=net",
                "-display", "none", "-serial", "file:/lab/serial.log", "-monitor", "unix:/lab/monitor.sock,server,nowait"]
        command("docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                "cp", "/usr/share/OVMF/OVMF_VARS_4M.fd", "/lab/nvram.fd")
        common = ["docker", "run", "-d", "--name", name, "--device", "/dev/kvm", "--memory", "12g", "--cpus", "4",
                  "-p", "127.0.0.1::2222", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0"]
        command(*common, *qemu, "-cdrom", "/lab/test.iso", "-boot", "d")
        for _ in range(180):
            state = json.loads(command("docker", "inspect", name))[0]["State"]
            if not state["Running"]:
                assert state["ExitCode"] == 0 and not state["OOMKilled"]
                break
            time.sleep(5)
        else:
            raise AssertionError("Linux installer did not power off within 15 minutes")
        command("docker", "rm", "-fv", name)
        command(*common, *qemu)
        ssh = ["ssh", "-i", str(tmp_path / "test-key"), "-o", "BatchMode=yes",
               "-o", f"ProxyCommand=docker exec -i {name} python3 /lab/ssh-proxy.py",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5",
               "tester@127.0.0.1"]
        for _ in range(240):
            result = subprocess.run([*ssh, "test -f /var/lib/agentic-home/installed.json"], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(5)
        else:
            logs = subprocess.run([*ssh, "journalctl -u agentic-home-firstboot --no-pager -n 50"], capture_output=True, text=True)
            raise AssertionError("Firstboot did not complete: " + logs.stdout[-4000:])
        host = json.loads(command(*ssh, "agentic-home status"))
        assert host["vm"] == "running" and host["provisioned"] and host["kvm"]
        assert command(*ssh, "systemctl is-active agentic-home agentic-home-host agentic-home-proxy") == "active\nactive\nactive"
        assert command(*ssh, "curl -fsS http://localhost:8099/api/deployment")
        auth_probe = command(*ssh, "python3 -", input=(ROOT / "tests/installed_probe.py").read_text())
        (ROOT / "evidence" / ("installed-auth-" + profile + ".json")).write_text(auth_probe + "\n")
        def wait_ha():
            for _ in range(180):
                response = subprocess.run([*ssh, "curl --max-time 3 -fsS http://localhost:8123/api/onboarding"],
                                          capture_output=True)
                try:
                    if response.returncode == 0 and isinstance(json.loads(response.stdout), list):
                        return
                except ValueError:
                    pass
                time.sleep(5)
            raise AssertionError("Nested HAOS HTTP did not become ready")

        wait_ha()
        command(*ssh, "agentic-home shutdown")
        assert json.loads(command(*ssh, "agentic-home status"))["vm"] == "shut off"
        assert command(*ssh, "curl -fsS http://localhost:8099/api/deployment")
        command(*ssh, "agentic-home snapshot")
        snapshot = json.loads(command(*ssh, "agentic-home status"))["snapshots"][0]["id"]
        command(*ssh, "agentic-home restore --snapshot " + snapshot)
        command(*ssh, "agentic-home start")
        wait_ha()
        subprocess.run([*ssh, "sudo systemctl reboot"], capture_output=True)
        time.sleep(15)
        for _ in range(120):
            result = subprocess.run([*ssh, "systemctl is-active agentic-home"], capture_output=True)
            if result.returncode == 0:
                break
            time.sleep(5)
        else:
            raise AssertionError("Outer product did not restart")
        assert json.loads(command(*ssh, "agentic-home status"))["vm"] == "running"
        wait_ha()
        evidence = {"kind": "real_linux_install_nested_kvm", "profile": profile, "base": "Ubuntu 24.04.5",
                    "iso_sha256": iso_hash,
                    "installation": "passed", "firstboot": "passed", "haos_vm_running": True,
                    "outer_ui_with_haos_off": True, "cold_snapshot_restore": "passed",
                    "outer_reboot_haos_autostart": "passed",
                    "ha_readiness_check": "real Core onboarding API JSON list after startup, restore and outer reboot",
                    "duration_seconds": round(time.time() - started),
                    "installer_inputs": "Disposable disk serial + ephemeral SSH key supplied by fixture",
                    "physical_laptop": "not_run", "cloud_login": "not_run"}
        report_name = "linux-install-server.json" if profile == "server-amd64" else "linux-install.json"
        (ROOT / "evidence" / report_name).write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        subprocess.run(["docker", "stop", "-t", "10", name], capture_output=True)
        # Keep only the nonsecret failure console when useful; all keys and virtual disks are removed.
        if (tmp_path / "serial.log").exists():
            shutil.copyfile(tmp_path / "serial.log", ROOT / "build/linux-install-console.log")
        subprocess.run(["docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/lab"], capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
