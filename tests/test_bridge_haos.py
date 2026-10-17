"""Real HAOS Ingress/App/TLS integration; inference remains an explicit fixture.

AGENTIC_BRIDGE_PUBLIC=1 installs the fixed published release branch through the
product manager. Default is a local source delivery fixture, never called GitHub
installation evidence. All credentials, networks, containers and disks are owned
by this test. No Supervisor/HA/OS upgrade operation is performed by the harness.
"""
import asyncio
import contextlib
import json
import lzma
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path

import aiohttp
import pytest
from agentic_controller.bridge_maintenance import BridgeMaintenance, BridgeRelease
from agentic_controller.ha import HAError, HomeAssistant

ROOT = Path(__file__).parents[1]
PYTHON = "python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
BUILDER = "docker:29.7.2-cli@sha256:3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e"
CONTROLLER = "agentic-home-controller:0.10.0"


class NoControllerState:
    """A test probe acts for the tester; it has no Controller state to persist first."""

    async def durable(self):
        return None


class ProbeHA(HomeAssistant):
    last_error = None

    async def start(self):
        if self.store is None:
            self.store = NoControllerState()
        await super().start()

    async def ws(self, command, *, _receive_timeout=20, **kwargs):
        async with await self.ws_connect() as stream:
            await stream.send_json({"id": 1, "type": command, **kwargs})
            result = await stream.receive_json(timeout=_receive_timeout)
            if not result.get("success"):
                error = result.get("error", {})
                text = str(error.get("message", "")).lower()
                self.last_error = {"job_missing": "job does not exist" in text,
                    "update_required": "needs to be updated" in text,
                    "message_bytes": len(text.encode()), "upstream_unknown_error": error.get("code") == "unknown_error"}
                raise HAError("supervisor_job_missing" if self.last_error["job_missing"] else "upstream_request_failed")
            return result["result"]

HOST_CONTRACT_FIXTURE = r'''
import json, os, threading
from aiohttp import web

def host_contract_app(receipt):
    reads, written, lock = 0, 0, threading.Lock()
    def record(count):
        nonlocal written
        # The receipt is evidence only. This answer shares the Controller's event loop and its 5 s
        # Host proof bound, and a VM backup can stall a test-disk write for longer than that.
        with lock:
            if count > written:
                pending = receipt.with_name(receipt.name + '.pending')
                pending.write_text(json.dumps({'kind':'unix_host_contract_fixture_not_installed_host',
                    'ingress_bridge_contract':1,'status_reads':count}))
                os.replace(pending, receipt)
                written = count
    async def status(request):
        nonlocal reads
        reads += 1
        threading.Thread(target=record, args=(reads,), daemon=True).start()
        return web.json_response({'controller_update':{'ingress_bridge_contract':1}})
    app = web.Application()
    app.router.add_get('/status', status, allow_head=False)
    async def unsupported(request):
        return web.Response(status=404)
    app.router.add_route('*', '/{tail:.*}', unsupported)
    return app
'''

OUTER = HOST_CONTRACT_FIXTURE + r'''
import asyncio, json, os, secrets
from pathlib import Path
import uvicorn
from agentic_controller.app import create_app
from agentic_controller.ha import HomeAssistant
from agentic_controller.store import Store
os.umask(0o077)
root = Path('/lab/outer-data')
root.mkdir(mode=0o700, exist_ok=True)
settings = json.loads(Path('/lab/identity.json').read_text())
(root/'access-key').write_text(settings['access_key'])
store = Store(root/'controller.sqlite3')
store.set_setting('owner', settings['owner'])
class InferenceFixture:
    def __init__(self): self.runs = {}
    async def call(self, method, path, body=None, key=None):
        if path == '/v1/capabilities': return {'state':'ready','auth_verified_at':'fixture-only'}
        if path == '/v1/sessions': return {'session_id':'a'*32, **body}
        if path.endswith('/runs'):
            value = {'run_id':body['run_id'],'state':'succeeded','answer':'HAOS transport inference fixture'}
            self.runs[body['run_id']] = value
            return value
        if path.startswith('/v1/runs/'): return self.runs.get(path.split('/')[3].split('?')[0], {'state':'cancelled'})
        return {'state':'configured','auth_verified_at':None}
ha = HomeAssistant(settings['token'], 'http://192.168.177.2:8123/api',
                   'ws://192.168.177.2:8123/api/websocket', external=True)
app = create_app(store=store, ha=ha, host_mode=True, hermes_client=InferenceFixture())
async def main():
    host = web.AppRunner(host_contract_app(Path('/lab/host-contract-fixture.json')), access_log=None)
    await host.setup()
    await web.UnixSite(host, '/run/agentic-home/host.sock').start()
    try:
        await app.state.ingress_bridge.require_host()
        options = await app.state.ingress_bridge.prepare_pairing()
        await store.durable()
        Path('/lab/pairing.json').write_text(json.dumps(options))
        if settings['public']:
            store.put('bootstrap_v1', {'id':'setup','run_id':'d'*32,'state':'completed','bridge_auto_enroll':True})
        await uvicorn.Server(uvicorn.Config(app, host='0.0.0.0',port=8099,
            proxy_headers=False,access_log=False,log_level='warning')).serve()
    finally:
        store.close()
        await host.cleanup()
asyncio.run(main())
'''


def process(args, *, timeout=120, input=None):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout)
    if result.returncode:
        # Never echo a private log, API body, credential or CLI error into JUnit.
        raise AssertionError(f"fixture_command_failed_{Path(args[0]).name}_{result.returncode}")
    return result.stdout


def docker(*args, timeout=120):
    return process(["docker", *args], timeout=timeout).decode().strip()


def setting(path, key):
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
        row = database.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}


def lifecycle_observation(names, since, *, run=subprocess.run):
    """Read only exact owned identities before teardown, with independent bounds."""
    observed = {}
    for role, name in names.items():
        value = {}
        for kind, args in (("state", ["inspect", "--format", "{{json .State}}", name]),
                ("events", ["events", "--since", str(int(since)), "--until", str(time.time()),
                            "--filter", "container=" + name, "--format", "{{json .}}"] )):
            try:
                result = run(["docker", *args], capture_output=True, text=True, timeout=5)
                value[kind + "_returncode"] = result.returncode
                if result.returncode or len(result.stdout) > 16384:
                    continue
                if kind == "state":
                    raw = json.loads(result.stdout)
                    value[kind] = {key: raw[key] for key in ("Running", "Paused", "Restarting", "OOMKilled", "Dead")
                                   if type(raw.get(key)) is bool}
                    value[kind].update({key: raw[key] for key in ("Pid", "ExitCode") if type(raw.get(key)) is int})
                    if raw.get("Status") in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
                        value[kind]["Status"] = raw["Status"]
                else:
                    values = [json.loads(line) for line in result.stdout.splitlines()]
                    value[kind] = [{"action": item["Action"], "timeNano": item["timeNano"]} for item in values
                        if item.get("Action") in {"create", "start", "die", "destroy", "kill", "stop"}
                        and type(item.get("timeNano")) is int]
            except Exception as error:
                value[kind + "_error_type"] = type(error).__name__
        observed[role] = value
    return observed


async def finish_bridge_test(evidence, *, report, tmp_path, label, outer, vm, network, ha, started,
                             run=subprocess.run):
    """Keep the product result even when independent, bounded teardown steps fail."""
    if evidence["status"] == "running":
        evidence.update(status="interrupted", error_type="BaseException")
    product_status = evidence["status"]
    evidence.update(product_status=product_status, cleanup_status="running")

    def write_report():
        pending = report.with_name(report.name + ".pending")
        pending.write_text(json.dumps(evidence, indent=2) + "\n")
        pending.replace(report)

    # Preserve completed assertions before any slow Docker cleanup. A transient
    # write error must still allow cleanup; the final write below remains required.
    with contextlib.suppress(OSError):
        write_report()
    cleanup, observations = {}, {}

    def command(step, args, timeout):
        try:
            result = run(["docker", *args], capture_output=True, timeout=timeout)
            observations[step] = {"returncode": result.returncode}
            return result
        except (OSError, subprocess.TimeoutExpired) as error:
            # Command text, stderr and exception messages can contain private data.
            observations[step] = {"error_type": type(error).__name__}
            return None

    def remove_container(role, name):
        command(role + "_remove", ["rm", "-fv", name], 30)
        # Removal can time out after Docker accepted it. Observe independently;
        # a successful command alone does not prove the owned resource is absent.
        check = command(role + "_observe", ["ps", "--all", "--quiet", "--no-trunc",
                        "--filter", "name=^/" + name + "$"], 5)
        cleanup[role + "_removed"] = check is not None and check.returncode == 0 and not check.stdout.strip()

    cleanup["ha_client_closed"] = ha is None
    if ha is not None:
        try:
            async with asyncio.timeout(10):
                await ha.close()
            cleanup["ha_client_closed"] = True
        except Exception as error:
            observations["ha_client_close"] = {"error_type": type(error).__name__}
    for role, name in (("outer", outer), ("vm", vm)):
        remove_container(role, name)
    cleaner = "agentic-bridge-clean-" + label
    owned = command("disk_fixture_ownership_restore", ["run", "--rm", "--name", cleaner,
                    "-v", f"{tmp_path}:/lab", "agentic-home-builder:0.2.0", "chown", "-R",
                    f"{os.getuid()}:{os.getgid()}", "/lab"], 60)
    cleanup["disk_fixture_ownership_restored"] = owned is not None and owned.returncode == 0
    # A timed-out `docker run --rm` may leave the ownership helper running.
    remove_container("ownership_helper", cleaner)
    command("network_remove", ["network", "rm", network], 30)
    check = command("network_observe", ["network", "ls", "--quiet", "--filter", "name=^" + network + "$"], 5)
    cleanup["network_removed"] = check is not None and check.returncode == 0 and not check.stdout.strip()
    cleaned = all(cleanup.values())
    evidence.update(cleanup=cleaned, cleanup_steps=cleanup, cleanup_observations=observations,
                    cleanup_status="passed" if cleaned else "failed",
                    duration_seconds=round(time.monotonic() - started, 3))
    if product_status == "passed" and not cleaned:
        evidence.update(status="failed", error_type="AssertionError", error_code="owned_bridge_resources_not_removed")
    write_report()
    # Never replace the original product assertion or interrupt with a teardown
    # assertion, but a product pass with leaked resources must fail the test run.
    if product_status == "passed" and not cleaned:
        raise AssertionError("owned_bridge_resources_not_removed")


def initial_app_contract(info, slug):
    # Reuse the production capability contract. Local source delivery is a test
    # fixture; production repository identity is verified by the manager itself.
    BridgeMaintenance(None, None, None).validate_app(
        info, {"slug": slug, "repository_id": slug.rsplit("_agentic_bridge", 1)[0]}, installed=True)
    assert info["options"] == {} and info["state"] == "stopped", "initial_app_options_changed"
    assert info["boot"] == "auto" and info["ingress_panel"] is False, "initial_app_boot_changed"
    assert info["auto_update"] is False and info["watchdog"] is False, "initial_app_policy_changed"


def public_delivery_contract(repositories, info, record):
    expected = BridgeRelease()
    repository_id = info.get("repository")
    assert isinstance(repository_id, str) and re.fullmatch(r"[a-f0-9]{8}", repository_id), "public_repository_identity_invalid"
    assert info.get("slug") == repository_id + "_" + expected.app, "public_app_identity_invalid"
    assert info.get("version") == expected.version and info.get("build") is True, "public_app_version_invalid"
    assert isinstance(repositories, list), "public_repository_contract_invalid"
    sources = [item.get("source") for item in repositories if isinstance(item, dict) and item.get("slug") == repository_id]
    assert sources == [expected.repository], "public_repository_source_not_verified"
    assert (record.get("managed") is True and record.get("slug") == info["slug"] and
            record.get("repository_id") == repository_id and
            record.get("release") == {"repository": expected.repository, "app": expected.app,
                                      "version": expected.version}), "public_manager_identity_not_verified"
    return {"source": sources[0], "reference": expected.repository.split("#", 1)[1], "repository_id": repository_id,
            "app_slug": info["slug"], "version": info["version"], "source_build": True,
            "commit_digest_pinned_by_supervisor": False}


async def wait_for_install(ha, slug, job_id, *, attempts=120, delay=5):
    for _ in range(attempts):
        confirmation = "supervisor_job_and_app"
        try:
            job = await ha.request("GET", "/jobs/" + job_id, supervisor=True)
        except HAError:
            # Completed Supervisor jobs are normally deleted (cleanup=True).
            # A missing reply alone never establishes installation success.
            confirmation = "app_postcondition_job_unavailable"
        else:
            assert job.get("uuid") == job_id and job.get("reference") == slug, "install_job_identity_invalid"
            assert (job.get("done") is None or type(job.get("done")) is bool) and isinstance(job.get("errors"), list), "install_job_contract_invalid"
            if job.get("done") is not True:
                await asyncio.sleep(delay)
                continue
            assert not job["errors"], "supervisor_app_install_job_failed"
        listed = await ha.request("GET", "/addons", supervisor=True)
        if any(item.get("slug") == slug for item in listed["addons"]):
            info = await ha.request("GET", f"/addons/{slug}/info", supervisor=True)
            initial_app_contract(info, slug)
            return confirmation
        await asyncio.sleep(delay)
    raise AssertionError("supervisor_install_postcondition_timeout")


async def test_real_haos_sidebar_bridge(tmp_path, request):
    if not request.config.getoption("--haos-vm"):
        pytest.skip("Actual HAOS App/Ingress needs --haos-vm")
    assert str(tmp_path).startswith(str(Path.home() / "agent-work/tmp/pytest"))
    public = os.environ.get("AGENTIC_BRIDGE_PUBLIC") == "1"
    label = uuid.uuid4().hex[:10]
    vm, outer, network = ("agentic-bridge-" + kind + "-" + label for kind in ("vm", "outer", "net"))
    started, started_wall = time.monotonic(), time.time()
    evidence = {"kind": "real_haos_sidebar_bridge", "haos_image": "18.3", "status": "running",
                "delivery": "published_release_branch" if public else "local_source_fixture",
                "observed_from": "haos_app_via_core_ingress_to_outer_pinned_tls",
                "inference": "protocol_fixture_no_cloud_account",
                "outer_source": "candidate_source_mounted_readonly", "checks": {}}
    evidence["outer_source_commit"] = process(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip()
    changes = process(["git", "-C", str(ROOT), "status", "--porcelain", "--",
                       "controller/agentic_controller", "controller/static", "bridge"])
    evidence["outer_source_uncommitted"] = bool(changes.strip())
    report = ROOT / "evidence/bridge-haos.json"
    progress = ROOT / "evidence/bridge-haos-progress.json"
    ha = None
    ssh, slug = None, None

    def phase(value):
        evidence["phase"] = value
        progress.write_text(json.dumps({"phase": value, "elapsed_seconds": round(time.monotonic() - started),
                                        "delivery": evidence["delivery"]}, indent=2) + "\n")

    try:
        phase("prepare_disposable_disks")
        with lzma.open(ROOT / "build/cache/haos_ova-18.3.qcow2.xz", "rb") as src, (tmp_path / "haos.qcow2").open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        process(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "key")])
        (tmp_path / "config").mkdir()
        shutil.copyfile(tmp_path / "key.pub", tmp_path / "config/authorized_keys")
        with (tmp_path / "config.ext4").open("wb") as stream:
            stream.truncate(32 * 1024**2)
        process(["/usr/sbin/mkfs.ext4", "-q", "-F", "-L", "CONFIG", "-d", str(tmp_path / "config"),
                 str(tmp_path / "config.ext4")])
        docker("network", "create", "--subnet", "192.168.177.0/24", "--gateway", "192.168.177.254", network)
        phase("boot_official_haos")
        docker("run", "-d", "--name", vm, "--network", network, "--ip", "192.168.177.2",
               "--device", "/dev/kvm", "--memory", "4096m", "--cpus", "3", "--pids-limit", "256",
               "-p", "127.0.0.1::8123", "-p", "127.0.0.1::2222", "-v", f"{tmp_path}:/lab",
               "agentic-home-builder:0.2.0", "bash", "-ec",
               "cp /usr/share/OVMF/OVMF_VARS_4M.fd /lab/nvram.fd\nqemu-img resize /lab/haos.qcow2 64G\n"
               "exec qemu-system-x86_64 -enable-kvm -cpu host -machine q35 -m 3072 -smp 2 "
               "-drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd "
               "-drive if=pflash,format=raw,file=/lab/nvram.fd "
               "-drive file=/lab/haos.qcow2,format=qcow2,if=none,id=disk "
               "-device virtio-scsi-pci -device scsi-hd,drive=disk "
               "-drive file=/lab/config.ext4,format=raw,if=virtio "
               "-netdev user,id=net,hostfwd=tcp:0.0.0.0:8123-:80,hostfwd=tcp:0.0.0.0:2222-:22222 "
               "-device virtio-net-pci,netdev=net -display none -serial file:/lab/serial.log", timeout=600)
        ports = json.loads(docker("inspect", vm))[0]["NetworkSettings"]["Ports"]
        base = "http://127.0.0.1:" + ports["8123/tcp"][0]["HostPort"]
        ssh = ["ssh", "-i", str(tmp_path / "key"), "-p", ports["2222/tcp"][0]["HostPort"],
               "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
               "-o", f"UserKnownHostsFile={tmp_path / 'known_hosts'}", "-o", "ConnectTimeout=5", "root@127.0.0.1"]
        # Blocking SSH calls run inside this loop; a pooled connection could outlive uvicorn's 5 s keep-alive.
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8),
                                         cookie_jar=aiohttp.CookieJar(unsafe=True),
                                         connector=aiohttp.TCPConnector(force_close=True)) as client:
            for _ in range(240):
                try:
                    async with client.get(base + "/api/onboarding") as response:
                        if response.status == 200 and isinstance(await response.json(), list):
                            break
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    pass
                await asyncio.sleep(3)
            else:
                raise AssertionError("haos_core_readiness_timeout")
            phase("native_disposable_owner")
            owner_password = secrets.token_urlsafe(32)
            async with client.post(base + "/api/onboarding/users", json={"client_id": base + "/",
                    "name": "Disposable Bridge Owner", "username": "bridge-owner",
                    "password": owner_password, "language": "en"}) as response:
                assert response.status == 200, "native_owner_creation_failed"
                onboarding = await response.json()
            async with client.post(base + "/auth/token", data={"grant_type": "authorization_code",
                    "code": onboarding["auth_code"], "client_id": base + "/"}) as response:
                assert response.status == 200, "native_token_exchange_failed"
                credentials = await response.json()
            for step in ("core_config", "analytics", "integration"):
                fields = {"client_id": base + "/", "redirect_uri": base + "/"} if step == "integration" else {}
                async with client.post(base + "/api/onboarding/" + step, json=fields,
                        headers={"Authorization": "Bearer " + credentials["access_token"]}) as response:
                    assert response.status == 200, "native_fixture_onboarding_failed"
            ha = ProbeHA(credentials["access_token"], base + "/api",
                              base.replace("http:", "ws:") + "/api/websocket", external=True)
            await ha.start()
            owner = await ha.ws("auth/current_user")
            assert await ha.owner(owner["id"])
            phase("supervisor_readiness")
            for _ in range(120):
                supervisor = await ha.request("GET", "/supervisor/info", supervisor=True)
                if supervisor.get("healthy") is True:
                    break
                await asyncio.sleep(3)
            else:
                raise AssertionError("supervisor_not_healthy_no_automatic_update")
            evidence.update(core=(await ha.request("GET", "/config"))["version"], supervisor=supervisor["version"])
            phase("outer_candidate_controller")
            identity = tmp_path / "identity.json"
            identity.write_text(json.dumps({"owner": owner["id"], "token": credentials["access_token"],
                                           "access_key": secrets.token_urlsafe(32), "public": public}))
            identity.chmod(0o600)
            (tmp_path / "outer.py").write_text(OUTER)
            (tmp_path / "host-socket").mkdir(mode=0o700)
            evidence["outer_dependency_image"] = docker("image", "inspect", CONTROLLER, "--format", "{{.Id}}")
            docker("run", "-d", "--name", outer, "--network", network, "--ip", "192.168.177.1",
                   "--memory", "768m", "--cpus", "1", "--pids-limit", "128", "--user", f"{os.getuid()}:{os.getgid()}",
                   "-p", "127.0.0.1::8099", "-e", "PYTHONPATH=/app",
                   "-e", "AGENTIC_DATA=/lab/outer-data", "-e", "AGENTIC_HOST_MODE=1",
                   "-v", f"{tmp_path}:/lab", "-v", f"{tmp_path / 'host-socket'}:/run/agentic-home",
                   "-v", f"{ROOT / 'controller/agentic_controller'}:/app/agentic_controller:ro",
                   "-v", f"{ROOT / 'controller/static'}:/app/static:ro", CONTROLLER,
                   "python", "/lab/outer.py", timeout=600)
            database = tmp_path / "outer-data/controller.sqlite3"
            for _ in range(60):
                if (tmp_path / "pairing.json").exists():
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError("outer_fixture_start_failed")
            options = json.loads((tmp_path / "pairing.json").read_text())
            if public:
                phase("published_repository_install")
                for _ in range(120):
                    current = setting(database, "bridge_maintenance")
                    if current.get("stage") == "monitor":
                        slug = current["slug"]
                        break
                    assert current.get("state") not in {"blocked", "needs_reconcile"}, "published_install_blocked"
                    await asyncio.sleep(10)
                else:
                    raise AssertionError("published_install_timeout")
            else:
                phase("local_source_fixture_delivery")
                process([*ssh, "true"], timeout=10)
                staging = tmp_path / "bridge-source"
                shutil.copytree(ROOT / "bridge", staging, ignore=shutil.ignore_patterns("__pycache__", "static"))
                shutil.copytree(ROOT / "controller/static", staging / "static")
                with tarfile.open(tmp_path / "bridge.tar.gz", "w:gz") as archive:
                    archive.add(staging, arcname="agentic_bridge")
                for reference in (PYTHON, BUILDER):
                    docker("image", "inspect", reference)
                docker("save", "-o", str(tmp_path / "bases.tar"), PYTHON, "docker:29.7.2-cli", timeout=600)
                with (tmp_path / "bases.tar").open("rb") as stream:
                    loaded = subprocess.run([*ssh, "docker load"], stdin=stream, capture_output=True, timeout=600)
                assert loaded.returncode == 0, "haos_base_preload_failed"
                with (tmp_path / "bridge.tar.gz").open("rb") as stream:
                    delivered = subprocess.run([*ssh, "mkdir -p /mnt/data/supervisor/apps/local && tar xzf - -C /mnt/data/supervisor/apps/local"],
                                               stdin=stream, capture_output=True, timeout=60)
                assert delivered.returncode == 0, "local_source_delivery_failed"
                await ha.request("POST", "/store/reload", supervisor=True, timeout=60)
                slug = "local_agentic_bridge"
                catalog = await ha.request("GET", f"/store/addons/{slug}/info", supervisor=True)
                assert catalog["version_latest"] == "0.10.0" and catalog["build"] is True
                phase("supervisor_app_install")
                receipt = await ha.request("POST", f"/store/addons/{slug}/install", {"background": True}, supervisor=True)
                assert isinstance(receipt, dict) and isinstance(receipt.get("job_id"), str)
                evidence["install_confirmation"] = await wait_for_install(ha, slug, receipt["job_id"])
                await ha.request("POST", f"/addons/{slug}/options", {"options": options, "boot": "auto",
                                 "auto_update": False, "watchdog": False, "ingress_panel": True}, supervisor=True)
                await ha.request("POST", f"/addons/{slug}/start", supervisor=True, timeout=120)
            info = await ha.request("GET", f"/addons/{slug}/info", supervisor=True)
            BridgeMaintenance(None, None, None).validate_app(
                info, {"slug": slug, "repository_id": info["repository"]}, installed=True)
            if public:
                repositories = await ha.request("GET", "/store/repositories", supervisor=True)
                evidence["public_repository"] = public_delivery_contract(
                    repositories, info, setting(database, "bridge_maintenance"))
            assert info["options"] == options and info["network"] in ({}, None)
            assert all(info[key] is False for key in ("hassio_api", "homeassistant_api", "docker_api", "full_access", "host_network", "host_pid"))
            assert info["watchdog"] is False and info["auto_update"] is False
            evidence["checks"]["supervisor_app_contract"] = True
            phase("app_to_outer_pinned_tls_heartbeat")
            for _ in range(45):
                state = setting(database, "ingress_bridge")
                if state.get("service_last_seen"):
                    break
                await asyncio.sleep(2)
            else:
                raise AssertionError("app_heartbeat_timeout")
            assert state.get("owner_verified_at") is None
            evidence["checks"]["service_heartbeat_without_owner_capability"] = True
            phase("native_ingress_owner_and_conversation")
            ingress = await ha.request("POST", "/ingress/session", supervisor=True)
            cookie = {"ingress_session": ingress["session"]}
            prefix = base + info["ingress_url"].rstrip("/") + "/"
            async with client.get(prefix + "api/session") as response:
                assert response.status == 401, "ingress_requires_native_session"
            async with client.get(prefix + "api/session", cookies=cookie,
                                  headers={"X-Remote-User-Id": "f" * 32}) as response:
                assert response.status == 200, "real_ingress_owner_session_failed"
                session = await response.json()
            assert session["principal"] == owner["id"]
            evidence["checks"]["ingress_native_identity_overrides_forged_header"] = True
            # A real Core-container peer is not the Supervisor, even with a forged identity header.
            script = ("import urllib.request,urllib.error,json; "
                      f"req=urllib.request.Request('http://{info['ip_address']}:8099/api/session',"
                      f"headers={{'X-Remote-User-Id':'{owner['id']}'}}); "
                      "\ntry: status=urllib.request.urlopen(req,timeout=5).status"
                      "\nexcept urllib.error.HTTPError as error: status=error.code"
                      "\nprint(json.dumps({'status':status}))")
            direct = process([*ssh, "docker exec homeassistant python3 -c " + shlex.quote(script)], timeout=15)
            assert json.loads(direct)["status"] == 403
            evidence["checks"]["direct_app_forged_header_denied"] = True
            if not public:
                phase("wrong_tls_pin_fails_closed")
                prior_seen = setting(database, "ingress_bridge")["service_last_seen"]
                await ha.request("POST", f"/addons/{slug}/options", {"options": {**options, "fingerprint": "0" * 64}}, supervisor=True)
                await ha.request("POST", f"/addons/{slug}/restart", supervisor=True, timeout=120)
                await asyncio.sleep(25)
                async with client.get(prefix + "api/session", cookies=cookie) as response:
                    assert response.status == 503, "wrong_tls_pin_was_accepted"
                assert setting(database, "ingress_bridge")["service_last_seen"] == prior_seen
                await ha.request("POST", f"/addons/{slug}/options", {"options": options}, supervisor=True)
                await ha.request("POST", f"/addons/{slug}/restart", supervisor=True, timeout=120)
                for _ in range(30):
                    if setting(database, "ingress_bridge")["service_last_seen"] > prior_seen:
                        break
                    await asyncio.sleep(2)
                else:
                    raise AssertionError("restored_tls_pin_not_ready")
                evidence["checks"]["wrong_tls_pin_denied_and_recovered"] = True
            phase("native_ingress_conversation")
            headers = {"Origin": base, "Sec-Fetch-Site": "same-origin", "X-Agentic-CSRF": session["csrf"],
                       "Idempotency-Key": "haos-bridge-first"}
            body = {"message": "Disposable HAOS transport fixture", "coordinator": "hermes", "new_conversation": True}
            async with client.post(prefix + "api/gateway/messages", json=body, cookies=cookie, headers=headers) as response:
                assert response.status == 202, "ingress_conversation_dispatch_failed"
                first = await response.json()
            async def completed(identifier):
                for _ in range(30):
                    async with client.get(prefix + "api/gateway/messages", cookies=cookie) as response:
                        assert response.status == 200
                        value = next(item for item in (await response.json())["messages"] if item["id"] == identifier)
                    if value["state"] == "succeeded":
                        return value
                    assert value["state"] in {"queued", "planning"}, "fixture_conversation_failed"
                    await asyncio.sleep(1)
                raise AssertionError("fixture_conversation_timeout")
            first = await completed(first["id"])
            assert first["answer"] == "HAOS transport inference fixture"
            async with client.post(prefix + "api/gateway/messages", json=body, cookies=cookie, headers=headers) as response:
                assert response.status == 202 and (await response.json())["id"] == first["id"]
            headers["Idempotency-Key"] = "haos-bridge-followup"
            async with client.post(prefix + "api/gateway/messages", json={"message": "Continue fixture", "coordinator": "hermes",
                    "reply_to": first["id"]}, cookies=cookie, headers=headers) as response:
                assert response.status == 202, "ingress_followup_failed"
                followup = await response.json()
            followup = await completed(followup["id"])
            assert first["case_id"] == followup["case_id"] and first["thread_id"] == followup["thread_id"]
            evidence["checks"]["same_outer_case_and_deduplicated_conversation"] = True
            async with client.get(prefix + "api/host/status", cookies=cookie) as response:
                assert response.status == 403, "bridge_host_management_not_denied"
            async with client.post(prefix + "api/gateway/messages", json={"message": "forged", "coordinator": "hermes"},
                    cookies=cookie, headers={"Origin": "https://foreign.invalid", "Idempotency-Key": "forged"}) as response:
                assert response.status == 403, "bridge_csrf_not_denied"
            evidence["checks"]["host_management_and_cross_origin_denied"] = True
            phase("native_restart_preserves_pairing_and_case")
            await ha.request("POST", f"/addons/{slug}/restart", supervisor=True, timeout=120)
            after = await ha.request("GET", f"/addons/{slug}/info", supervisor=True)
            assert after["options"] == options
            for _ in range(30):
                try:
                    async with client.get(prefix + "api/gateway/messages", cookies=cookie) as response:
                        if response.status == 200:
                            saved = await response.json()
                            break
                except (aiohttp.ClientError, TimeoutError):
                    pass
                await asyncio.sleep(2)
            else:
                raise AssertionError("ingress_restart_readiness_timeout")
            assert any(item["id"] == first["id"] for item in saved["messages"])
            evidence["checks"]["app_restart_preserves_options_and_outer_case"] = True
            phase("native_sidebar_browser")
            from bridge_native_browser import NativeBridgeBrowserError, exercise_native_bridge
            try:
                browser = await exercise_native_bridge(base_url=base, username="bridge-owner",
                    password=owner_password, slug=slug, ingress_url=info["ingress_url"],
                    screenshot=ROOT / "evidence/ui/bridge-native-haos.png",
                    expected_case_id=first["case_id"], expected_thread_id=first["thread_id"])
            except NativeBridgeBrowserError as error:
                evidence["native_browser"] = error.evidence
                raise
            evidence["native_browser"] = browser
            phase("native_stop_respected")
            await ha.request("POST", f"/addons/{slug}/stop", supervisor=True)
            await asyncio.sleep(20)
            stopped = await ha.request("GET", f"/addons/{slug}/info", supervisor=True)
            assert stopped["state"] == "stopped"
            if public:
                deadline = time.monotonic() + 30
                while setting(database, "bridge_maintenance").get("state") != "user_disabled":
                    assert time.monotonic() < deadline, "manual_stop_observation_not_persisted"
                    await asyncio.sleep(.25)
            evidence["checks"]["manual_stop_remains_stopped"] = True
            host_fixture = json.loads((tmp_path / "host-contract-fixture.json").read_text())
            assert (host_fixture["kind"] == "unix_host_contract_fixture_not_installed_host" and
                    host_fixture["ingress_bridge_contract"] == 1 and host_fixture["status_reads"] > 0)
            evidence["host_capability"] = host_fixture
            evidence["status"] = "passed"
    except BaseException as exc:
        evidence.update(status="failed", error_type=type(exc).__name__)
        evidence["lifecycle_before_cleanup"] = lifecycle_observation({"vm": vm, "outer": outer}, started_wall)
        if isinstance(exc, subprocess.TimeoutExpired):
            evidence["command_timeout_seconds"] = exc.timeout
        if ha:
            evidence["upstream_error"] = ha.last_error
            for key, endpoint in (("supervisor", "/supervisor/info"),
                                  ("app", f"/store/addons/{slug}/info") if slug else ("jobs", "/jobs/info")):
                try:
                    async with asyncio.timeout(10):
                        observed = await ha.request("GET", endpoint, supervisor=True, timeout=5)
                    evidence[key + "_failure_observation"] = {name: observed.get(name) for name in
                        ("version", "version_latest", "healthy", "state", "installed", "available", "build")
                        if isinstance(observed, dict) and type(observed.get(name)) in (str, bool)}
                except Exception:
                    evidence[key + "_failure_observation"] = {"available": False}
        if ssh:
            try:
                logs = subprocess.run([*ssh, "ha supervisor logs | tail -80"], capture_output=True, timeout=15)
                text = logs.stdout[-65536:].decode(errors="replace").lower()
                evidence["supervisor_log_signatures"] = {name: pattern in text for name, pattern in
                    {"update_required": "needs to be updated", "job_missing": "job does not exist",
                     "build_failure": "build failed", "registry_failure": "failed to resolve",
                     "no_space": "no space left", "permission": "permission denied",
                     "network_failure": "temporary failure", "not_installed": "not installed"}.items()}
                evidence["supervisor_log_signatures"]["bytes_observed"] = len(text.encode())
            except Exception:
                evidence["supervisor_log_signatures"] = {"unavailable": True}
        raise
    finally:
        await finish_bridge_test(evidence, report=report, tmp_path=tmp_path, label=label,
            outer=outer, vm=vm, network=network, ha=ha, started=started)


@pytest.mark.parametrize("failure", ["timeout", "oserror"])
@pytest.mark.parametrize("failed_step", ["outer_remove", "outer_observe", "vm_remove", "vm_observe",
    "ownership_restore", "ownership_helper_remove", "ownership_helper_observe", "network_remove", "network_observe"])
async def test_bridge_teardown_continues_and_records_product_result(tmp_path, failure, failed_step):
    report = tmp_path / "receipt.json"
    private = "private-command-output-and-path"
    steps = ["outer_remove", "outer_observe", "vm_remove", "vm_observe", "ownership_restore",
             "ownership_helper_remove", "ownership_helper_observe", "network_remove", "network_observe"]
    calls = []

    def run(args, **kwargs):
        step = steps[len(calls)]
        calls.append(args)
        # Assertions are durable before the first slow cleanup call.
        before = json.loads(report.read_text())
        assert before["product_status"] == "passed" and before["cleanup_status"] == "running"
        assert kwargs["capture_output"] is True and kwargs["timeout"] in {5, 30, 60}
        if step == failed_step:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=private, stderr=private)
            raise OSError(private)
        # A failed removal leaves an owned resource; readback must catch it even
        # though all later cleanup steps and the final receipt still run.
        residual = step.endswith("_observe") and failed_step == step.removesuffix("_observe") + "_remove"
        return subprocess.CompletedProcess(args, 0, stdout=b"owned-residual\n" if residual else b"", stderr=b"")

    evidence = {"status": "passed", "checks": {"manual_stop_remains_stopped": True}}
    with pytest.raises(AssertionError, match="^owned_bridge_resources_not_removed$"):
        await finish_bridge_test(evidence, report=report, tmp_path=tmp_path, label="fixture",
            outer="owned-outer", vm="owned-vm", network="owned-network", ha=None,
            started=time.monotonic(), run=run)
    assert len(calls) == len(steps)
    assert calls[0] == ["docker", "rm", "-fv", "owned-outer"]
    assert calls[2] == ["docker", "rm", "-fv", "owned-vm"]
    assert calls[5] == ["docker", "rm", "-fv", "agentic-bridge-clean-fixture"]
    assert calls[7] == ["docker", "network", "rm", "owned-network"]
    for index, name in ((1, "owned-outer"), (3, "owned-vm"), (6, "agentic-bridge-clean-fixture")):
        assert calls[index][-2:] == ["--filter", "name=^/" + name + "$"]
    assert calls[8][-2:] == ["--filter", "name=^owned-network$"]
    result = json.loads(report.read_text())
    assert result["product_status"] == "passed" and result["cleanup_status"] == "failed"
    assert result["status"] == "failed" and result["cleanup"] is False
    assert result["checks"] == {"manual_stop_remains_stopped": True}
    assert any(item.get("error_type") == ("TimeoutExpired" if failure == "timeout" else "OSError")
               for item in result["cleanup_observations"].values())
    assert private not in report.read_text() and "owned-residual" not in report.read_text()
    assert not report.with_name("receipt.json.pending").exists()


async def test_bridge_teardown_confirms_absence_after_removal_timeout(tmp_path):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["rm", "-fv"] or args[1:3] == ["network", "rm"]:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    report = tmp_path / "receipt.json"
    evidence = {"status": "passed"}
    await finish_bridge_test(evidence, report=report, tmp_path=tmp_path, label="fixture",
        outer="owned-outer", vm="owned-vm", network="owned-network", ha=None,
        started=time.monotonic(), run=run)
    result = json.loads(report.read_text())
    assert len(calls) == 9
    assert result["status"] == result["product_status"] == result["cleanup_status"] == "passed"
    assert result["cleanup"] is True and all(result["cleanup_steps"].values())
    assert result["cleanup_observations"]["outer_remove"] == {"error_type": "TimeoutExpired"}


@pytest.mark.parametrize("product_status", ["passed", "failed"])
async def test_bridge_teardown_close_failure_does_not_hide_product_failure(tmp_path, product_status):
    class UnavailableHA:
        async def close(self):
            raise OSError("private-ha-close-error")

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    report = tmp_path / "receipt.json"
    evidence = {"status": product_status}
    if product_status == "failed":
        evidence.update(error_type="AssertionError", error_code="product_assertion_failed")
    expected = "product_assertion_failed" if product_status == "failed" else "owned_bridge_resources_not_removed"
    with pytest.raises(AssertionError, match="^" + expected + "$"):
        try:
            if product_status == "failed":
                raise AssertionError("product_assertion_failed")
        finally:
            await finish_bridge_test(evidence, report=report, tmp_path=tmp_path, label="fixture",
                outer="owned-outer", vm="owned-vm", network="owned-network", ha=UnavailableHA(),
                started=time.monotonic(), run=run)
    result = json.loads(report.read_text())
    assert len(calls) == 9
    assert result["product_status"] == product_status and result["cleanup_status"] == "failed"
    assert result["error_code"] == expected
    assert result["cleanup_steps"]["ha_client_closed"] is False
    assert result["cleanup_steps"]["network_removed"] is True
    assert "private-ha-close-error" not in report.read_text()


@pytest.mark.parametrize("outcome", ["completed", "cleaned", "missing_app", "modified", "failed", "pending"])
async def test_install_observer_never_confuses_cleaned_job_with_success(outcome):
    from test_bridge_maintenance import JOB, SLUG, Supervisor

    fixture = Supervisor()
    fixture.install_app()
    if outcome == "missing_app":
        fixture.app = None
    if outcome == "modified":
        fixture.app["options"] = {"host": "native-change"}
    if outcome == "failed":
        fixture.job["errors"] = [{"message": "untrusted-upstream-error"}]
    if outcome == "pending":
        fixture.job["done"] = False

    class Observation:
        async def request(self, method, path, *, supervisor):
            if outcome in {"cleaned", "missing_app", "modified"} and path.startswith("/jobs/"):
                raise HAError("job_missing")
            return await fixture.request(method, path, supervisor=supervisor, timeout=30)

    if outcome in {"completed", "cleaned"}:
        expected = "supervisor_job_and_app" if outcome == "completed" else "app_postcondition_job_unavailable"
        assert await wait_for_install(Observation(), SLUG, JOB, attempts=2, delay=0) == expected
    else:
        with pytest.raises(AssertionError):
            await wait_for_install(Observation(), SLUG, JOB, attempts=2, delay=0)
    assert not any(method != "GET" for method, _, _ in fixture.calls)


@pytest.mark.parametrize("change", [None, "source", "repository", "version", "slug", "unmanaged", "release"])
def test_public_delivery_evidence_requires_actual_exact_repository(change):
    release = BridgeRelease()
    repositories = [{"slug": "aabbccdd", "source": release.repository}]
    info = {"repository": "aabbccdd", "slug": "aabbccdd_agentic_bridge", "version": "0.9.0", "build": True}
    record = {"managed": True, "repository_id": "aabbccdd", "slug": info["slug"],
              "release": {"repository": release.repository, "app": release.app, "version": release.version}}
    if change == "source":
        repositories[0]["source"] = "https://untrusted.invalid/repo"
    elif change == "repository":
        info["repository"] = "local"
    elif change == "version":
        info["version"] = "0.7.1"
    elif change == "slug":
        info["slug"] = "local_agentic_bridge"
    elif change == "unmanaged":
        record["managed"] = False
    elif change == "release":
        record["release"]["repository"] = "https://untrusted.invalid/repo"
    if change:
        with pytest.raises(AssertionError):
            public_delivery_contract(repositories, info, record)
    else:
        result = public_delivery_contract(repositories, info, record)
        assert result["source"] == release.repository and result["reference"] == release.repository.split("#", 1)[1]
        assert result["commit_digest_pinned_by_supervisor"] is False
        assert set(result) == {"source", "reference", "repository_id", "app_slug", "version", "source_build",
                               "commit_digest_pinned_by_supervisor"}



@pytest.mark.parametrize("failure", [None, "timeout", "malformed"])
def test_owned_lifecycle_diagnostics_are_bounded_and_never_export_raw_fields(failure):
    private = "fixture-private-value"
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 5 and kwargs["capture_output"] is True
        assert "owned-fixture" in args or "container=owned-fixture" in args
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 5, stderr=private)
        if failure == "malformed":
            return type("Result", (), {"returncode": 0, "stdout": private})()
        body = {"Running": False, "Pid": 0, "ExitCode": 137, "Error": private} if args[1] == "inspect" else {
            "Action": "die", "timeNano": 1000000000, "Actor": {"secret": private}}
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(body)})()
    observed = lifecycle_observation({"vm": "owned-fixture"}, time.time(), run=run)
    assert len(calls) == 2 and private not in json.dumps(observed)
    if failure:
        assert "state" not in observed["vm"] and "events" not in observed["vm"]
    else:
        assert observed["vm"]["state"] == {"Running": False, "Pid": 0, "ExitCode": 137}
        assert observed["vm"]["events"] == [{"action": "die", "timeNano": 1000000000}]



async def test_host_capability_fixture_uses_real_unix_http_and_exposes_no_actions(tmp_path):
    from aiohttp import web
    namespace = {}
    exec(HOST_CONTRACT_FIXTURE, namespace)
    receipt = tmp_path / "receipt.json"
    server = web.AppRunner(namespace["host_contract_app"](receipt))
    socket = tmp_path.parent / "bridge-contract.sock"
    await server.setup()
    try:
        await web.UnixSite(server, str(socket)).start()
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket))) as client:
            async with client.get("http://localhost/status") as response:
                assert response.status == 200
                assert await response.json() == {"controller_update": {"ingress_bridge_contract": 1}}
            for method, path in (("POST", "/actions"), ("POST", "/status"), ("HEAD", "/status"), ("GET", "/unknown")):
                async with client.request(method, "http://localhost" + path, json={}) as response:
                    assert response.status == 404
        deadline = time.monotonic() + 10
        while not receipt.exists():
            assert time.monotonic() < deadline, "host_fixture_receipt_missing"
            await asyncio.sleep(.05)
        value = json.loads(receipt.read_text())
        assert value == {"kind": "unix_host_contract_fixture_not_installed_host",
                         "ingress_bridge_contract": 1, "status_reads": 1}
    finally:
        await server.cleanup()
        socket.unlink(missing_ok=True)


async def test_host_capability_fixture_answers_while_its_receipt_write_is_stalled(tmp_path):
    from aiohttp import web
    namespace = {}
    exec(HOST_CONTRACT_FIXTURE, namespace)

    class StalledDisk:
        """The receipt's final rename waits, as a test-disk write does during a VM backup."""
        release = threading.Event()

        def replace(self, source, target):
            assert self.release.wait(30)
            os.replace(source, target)

    namespace["os"] = disk = StalledDisk()
    receipt = tmp_path / "receipt.json"
    server = web.AppRunner(namespace["host_contract_app"](receipt))
    socket = tmp_path.parent / "bridge-stalled.sock"
    await server.setup()
    try:
        await web.UnixSite(server, str(socket)).start()
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(socket))) as client:
            for _ in range(3):
                started = time.monotonic()
                async with client.get("http://localhost/status") as response:
                    assert response.status == 200
                    assert await response.json() == {"controller_update": {"ingress_bridge_contract": 1}}
                assert time.monotonic() - started < 2
        assert not receipt.exists()
        disk.release.set()
        deadline = time.monotonic() + 10
        while not receipt.exists() or json.loads(receipt.read_text())["status_reads"] != 3:
            assert time.monotonic() < deadline, "host_fixture_receipt_not_latest"
            await asyncio.sleep(.05)
    finally:
        disk.release.set()
        await server.cleanup()
        socket.unlink(missing_ok=True)
