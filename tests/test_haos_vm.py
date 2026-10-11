"""Real HAOS guest, real Supervisor backup through external owner connection. No user hardware."""
import asyncio
import json
import lzma
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import aiohttp
import pytest
from agentic_controller.ha import HomeAssistant

ROOT = Path(__file__).parents[1]


class DiagnosticHA(HomeAssistant):
    async def ws(self, command, *, _receive_timeout=20, **kwargs):
        async with await self.ws_connect() as ws:
            await ws.send_json({"id": 1, "type": command, **kwargs})
            result = await ws.receive_json(timeout=_receive_timeout)
            assert result.get("success"), result.get("error")
            return result["result"]


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


async def test_real_haos_boot_owner_and_supervisor_backup(tmp_path, request):
    if not request.config.getoption("--haos-vm"):
        pytest.skip("Use --haos-vm for isolated KVM validation")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    image = ROOT / "build/cache/haos_ova-18.3.qcow2.xz"
    with lzma.open(image, "rb") as source, (tmp_path / "haos.qcow2").open("wb") as target:
        shutil.copyfileobj(source, target, 1024 * 1024)
    name = "agentic-haos-vm-test-" + uuid.uuid4().hex[:10]
    started = time.time()
    try:
        docker("run", "-d", "--name", name, "--device", "/dev/kvm", "--memory", "4096m", "--cpus", "3",
               "-p", "127.0.0.1::8123", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0", "bash", "-ec",
               "cp /usr/share/OVMF/OVMF_VARS_4M.fd /lab/nvram.fd\n"
               "qemu-img resize /lab/haos.qcow2 64G\n"
               "exec qemu-system-x86_64 -enable-kvm -cpu host -machine q35 -m 3072 -smp 2 "
               "-drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd "
               "-drive if=pflash,format=raw,file=/lab/nvram.fd "
               "-drive file=/lab/haos.qcow2,format=qcow2,if=none,id=disk "
               "-device virtio-scsi-pci -device scsi-hd,drive=disk "
               "-netdev user,id=net,hostfwd=tcp:0.0.0.0:8123-:80 -device virtio-net-pci,netdev=net "
               "-display none -serial file:/lab/serial.log")
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            for _ in range(240):
                try:
                    async with session.get(base + "/api/onboarding") as response:
                        if response.status == 200 and isinstance(await response.json(), list):
                            break
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    pass
                await asyncio.sleep(3)
            else:
                raise AssertionError("HAOS Core did not become ready; inspect isolated VM console")
            async with session.post(base + "/api/onboarding/users", json={"client_id": base + "/",
                                    "name": "Disposable Test Owner", "username": "test-owner",
                                    "password": secrets.token_urlsafe(32), "language": "en"}) as response:
                onboarding = await response.json()
                assert response.status == 200
            async with session.post(base + "/auth/token", data={"grant_type": "authorization_code",
                                    "code": onboarding["auth_code"], "client_id": base + "/"}) as response:
                auth = await response.json()
                assert response.status == 200
        ha = DiagnosticHA(auth["access_token"], base + "/api", base.replace("http:", "ws:") + "/api/websocket", external=True)
        await ha.start()
        try:
            owner = await ha.ws("auth/current_user")
            assert await ha.owner(owner["id"])
            core = await ha.request("GET", "/config")
            supervisor = await ha.request("GET", "/supervisor/info", supervisor=True)
            for _ in range(90):
                if supervisor.get("healthy") is True:
                    break
                await asyncio.sleep(2)
                supervisor = await ha.request("GET", "/supervisor/info", supervisor=True)
            backup = await ha.backup("Agentic disposable VM backup")
            assert backup["id"]
            evidence = {"kind": "real_haos_kvm", "haos_image": "18.3", "core": core["version"],
                        "supervisor": supervisor["version"], "owner_verified": True,
                        "backup_completed_and_readback": True, "duration_seconds": round(time.time() - started),
                        "cloud_login": "not_run", "laptop_hardware": "not_run"}
            (ROOT / "evidence/haos-vm.json").write_text(json.dumps(evidence, indent=2) + "\n")
        finally:
            await ha.close()
    finally:
        subprocess.run(["docker", "stop", "-t", "10", name], capture_output=True)
        subprocess.run(["docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/lab"], capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)
