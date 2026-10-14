"""Real HAOS guest, real Supervisor backup through external owner connection. No user hardware."""
import asyncio
import json
import lzma
import os
import re
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
# Core answers onboarding long before it is RUNNING; first boot on a busy test disk takes minutes more.
READY_SECONDS = 900
BACKUP_ATTEMPTS = 3
JOB = re.compile(r"check job '([a-f0-9]{32})'")


class Refused(AssertionError):
    def __init__(self, error):
        super().__init__(error)
        self.error = error if isinstance(error, dict) else {}


class DiagnosticHA(HomeAssistant):
    async def ws(self, command, *, _receive_timeout=20, **kwargs):
        async with await self.ws_connect() as ws:
            await ws.send_json({"id": 1, "type": command, **kwargs})
            result = await ws.receive_json(timeout=_receive_timeout)
            if not result.get("success"):
                raise Refused(result.get("error"))
            return result["result"]


async def wait_ready(ha, seen):
    """Back up only once Core is RUNNING and the Supervisor is running and healthy. Keeps each distinct state."""
    deadline = time.monotonic() + READY_SECONDS
    while True:
        core = await ha.request("GET", "/config")
        info = await ha.request("GET", "/info", supervisor=True)
        supervisor = await ha.request("GET", "/supervisor/info", supervisor=True)
        state = {"core": core.get("state"), "supervisor": info.get("state"), "healthy": supervisor.get("healthy")}
        if not seen or seen[-1] != state:
            seen.append(state)
        if state == {"core": "RUNNING", "supervisor": "running", "healthy": True}:
            return core, supervisor
        if time.monotonic() > deadline:
            raise AssertionError(f"HAOS did not finish starting: {seen}")
        await asyncio.sleep(3)


def brief(job, depth=0):
    """A Supervisor job's names, stages and error messages only."""
    errors = [{"type": item.get("type"), "message": str(item.get("message"))[:300], "stage": item.get("stage")}
              for item in job.get("errors") or [] if isinstance(item, dict)]
    children = [brief(item, depth + 1) for item in job.get("child_jobs") or [] if isinstance(item, dict)]
    return {"name": job.get("name"), "stage": job.get("stage"), "done": job.get("done"), "errors": errors[:5],
            "child_jobs": children[:10] if depth < 3 else []}


async def failed_job(ha, error):
    match = JOB.search(str(error.get("message", "")))
    if not match:
        return None
    try:
        return brief(await ha.request("GET", "/jobs/" + match.group(1), supervisor=True))
    except Exception as exc:  # A missing diagnostic must not hide the refused backup.
        return {"unavailable": type(exc).__name__}


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


async def test_real_haos_boot_owner_and_supervisor_backup(tmp_path, request, store):
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
            # Password hashing and auth storage writes right after boot can take over 5 s on loaded disks; do not retry if timed out.
            async with session.post(base + "/api/onboarding/users", json={"client_id": base + "/",
                                    "name": "Disposable Test Owner", "username": "test-owner",
                                    "password": secrets.token_urlsafe(32), "language": "en"},
                                    timeout=aiohttp.ClientTimeout(total=120)) as response:
                onboarding = await response.json()
                assert response.status == 200
            async with session.post(base + "/auth/token", data={"grant_type": "authorization_code",
                                    "code": onboarding["auth_code"], "client_id": base + "/"},
                                    timeout=aiohttp.ClientTimeout(total=120)) as response:
                auth = await response.json()
                assert response.status == 200
        ha = DiagnosticHA(auth["access_token"], base + "/api", base.replace("http:", "ws:") + "/api/websocket", external=True)
        ha.store = store
        await ha.start()
        seen, failures = [], []
        try:
            owner = await ha.ws("auth/current_user")
            assert await ha.owner(owner["id"])
            for attempt in range(1, BACKUP_ATTEMPTS + 1):
                core, supervisor = await wait_ready(ha, seen)
                try:
                    backup = await ha.backup("Agentic disposable VM backup")
                    break
                except Refused as exc:
                    # The Supervisor's own backup job failed. Keep its reason, then wait for HAOS again.
                    failures.append({"attempt": attempt, "code": exc.error.get("code"),
                                     "job": await failed_job(ha, exc.error)})
                    if attempt == BACKUP_ATTEMPTS:
                        raise
                    await asyncio.sleep(30)
            assert backup["id"]
            evidence = {"kind": "real_haos_kvm", "haos_image": "18.3", "core": core["version"],
                        "supervisor": supervisor["version"], "owner_verified": True,
                        "backup_completed_and_readback": True, "backup_attempts": len(failures) + 1,
                        "backup_failures": failures, "ready_states": seen,
                        "duration_seconds": round(time.time() - started),
                        "cloud_login": "not_run", "laptop_hardware": "not_run"}
            (ROOT / "evidence/haos-vm.json").write_text(json.dumps(evidence, indent=2) + "\n")
        except Exception:
            (ROOT / "evidence/haos-vm-failure.json").write_text(json.dumps({
                "kind": "real_haos_kvm", "result": "failed", "ready_states": seen, "backup_failures": failures,
                "duration_seconds": round(time.time() - started)}, indent=2) + "\n")
            raise
        finally:
            await ha.close()
    finally:
        subprocess.run(["docker", "stop", "-t", "10", name], capture_output=True)
        subprocess.run(["docker", "run", "--rm", "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0",
                        "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/lab"], capture_output=True)
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True)


async def test_refused_backup_keeps_only_its_job_names_stages_and_errors():
    identity = "7b4d82e97f5b41a2978cf0cc30d25400"
    job = {"name": "backup_manager_partial_backup", "uuid": identity, "reference": "x", "stage": "home_assistant",
           "done": True, "created": "now", "errors": [{"type": "BackupError", "message": "m" * 400,
                                                         "stage": "home_assistant", "extra": "dropped"}],
           "child_jobs": [{"name": "home_assistant_core_backup", "done": True, "progress": 0,
                           "errors": [{"type": "HomeAssistantBackupError", "message": "Backup failed"}]}]}

    class Supervisor:
        async def request(self, method, path, supervisor=False):
            assert (method, path, supervisor) == ("GET", "/jobs/" + identity, True)
            return job

    refused = {"code": "unknown_error",
               "message": f"An error occurred while making backup, check job '{identity}' or supervisor logs for details"}
    assert await failed_job(Supervisor(), refused) == {
        "name": "backup_manager_partial_backup", "stage": "home_assistant", "done": True,
        "errors": [{"type": "BackupError", "message": "m" * 300, "stage": "home_assistant"}],
        "child_jobs": [{"name": "home_assistant_core_backup", "stage": None, "done": True, "child_jobs": [],
                        "errors": [{"type": "HomeAssistantBackupError", "message": "Backup failed", "stage": None}]}]}
    assert await failed_job(Supervisor(), {"code": "unknown_error", "message": "no job named"}) is None

    class Gone:
        async def request(self, *args, **kwargs):
            raise Refused({"code": "unknown_error"})

    assert await failed_job(Gone(), refused) == {"unavailable": "Refused"}
