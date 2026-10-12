"""Runtime review regressions: real child processes, synthetic credentials only."""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from agentic_hermes.app import process_info, signal_members
from agentic_runner.app import Broker, create_api
from agentic_runner.providers import Providers, StopUnconfirmed


async def install_fork_fixture(broker, tmp_path, monkeypatch, *, leader_ignores_term=False):
    child_path = tmp_path / "review-child.pid"
    source = (
        "import os,signal,time\n"
        "from pathlib import Path\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if leader_ignores_term else "") +
        "if os.fork() == 0:\n"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        " for fd in (0,1,2): os.close(fd)\n"
        f" path=Path({str(child_path)!r})\n"
        " pending=path.with_suffix('.pending')\n"
        " pending.write_text(str(os.getpid()))\n"
        " pending.replace(path)\n"
        "while True: time.sleep(1)\n"
    )
    spawned = []
    actual_spawn = asyncio.create_subprocess_exec

    async def fixture_spawn(binary, *args, **kwargs):
        assert binary == "codex"
        proc = await actual_spawn(sys.executable, "-c", source, **kwargs)
        spawned.append(proc)
        return proc

    async def probe(provider):
        return {"provider": provider, "version": "0.157.1", "version_matches": True}

    monkeypatch.setattr(broker.providers, "probe", probe)
    monkeypatch.setattr(broker.providers, "uid", lambda _: os.getuid())
    monkeypatch.setattr("agentic_runner.providers.shutil.which", lambda _: "/fixture/codex")
    monkeypatch.setattr("agentic_runner.providers.asyncio.create_subprocess_exec", fixture_spawn)
    return child_path, spawned


async def child_ready(path):
    for _ in range(200):
        if path.exists():
            pid = int(path.read_text())
            return pid, process_info(pid)["start"]
        await asyncio.sleep(0.01)
    raise AssertionError("fixture child did not become ready")


async def cleanup_fixture(broker, spawned, identity):
    if identity:
        signal_members({identity[0]: identity[1]}, signal.SIGKILL)
    for proc in spawned:
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=3)
    tasks = list(broker.tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    broker.db.close()


def job_body(identifier="a" * 32):
    return {"id": identifier, "provider": "codex", "prompt": "synthetic review fixture",
            "schema": {}, "expires": time.time() + 300}


async def test_runner_cancel_fences_term_ignoring_descendant_before_acknowledgement(tmp_path, monkeypatch):
    broker = Broker(tmp_path / "runner")
    broker.settings({"token_hash": hashlib.sha256(b"review-token").hexdigest()})
    child_path, spawned = await install_fork_fixture(broker, tmp_path, monkeypatch)
    identity = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)),
                                    base_url="https://runner", headers={"Authorization": "Bearer review-token"}) as client:
            assert (await client.post("/jobs", json=job_body())).status_code == 200
            identity = await child_ready(child_path)
            result = await client.post("/jobs/" + "a" * 32 + "/cancel")
            assert result.status_code == 200 and result.json()["state"] == "cancelled"
            info = process_info(identity[0])
            assert info is None or info["state"] in {"Z", "X"}, "cancelled acknowledged while old CLI descendant is alive"
            assert not broker.providers.processes
    finally:
        await cleanup_fixture(broker, spawned, identity)


@pytest.mark.parametrize("disconnect_first", [False, True])
async def test_duplicate_cancel_and_request_disconnect_do_not_interrupt_shared_fence(
        tmp_path, monkeypatch, disconnect_first):
    from agentic_runner import providers as provider_module

    broker = Broker(tmp_path / "runner")
    broker.settings({"token_hash": hashlib.sha256(b"review-token").hexdigest()})
    child_path, spawned = await install_fork_fixture(broker, tmp_path, monkeypatch, leader_ignores_term=True)
    identity, requests = None, []
    term_sent = asyncio.Event()
    original_signal = provider_module.signal_members

    def signal_and_observe(members, uid, value):
        original_signal(members, uid, value)
        if value == signal.SIGTERM:
            term_sent.set()

    monkeypatch.setattr(provider_module, "signal_members", signal_and_observe)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)),
                                    base_url="https://runner", headers={"Authorization": "Bearer review-token"}) as client:
            assert (await client.post("/jobs", json=job_body())).status_code == 200
            identity = await child_ready(child_path)
            writer = broker.tasks["a" * 32]
            first = asyncio.create_task(client.post("/jobs/" + "a" * 32 + "/cancel"))
            requests.append(first)
            await asyncio.wait_for(term_sent.wait(), 1)
            if disconnect_first:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            second = asyncio.create_task(client.post("/jobs/" + "a" * 32 + "/cancel"))
            requests.append(second)
            result = await asyncio.wait_for(second, 4)
            assert result.status_code == 200 and result.json()["state"] == "cancelled"
            if not disconnect_first:
                assert (await first).json()["state"] == "cancelled"
            assert writer.cancelling() == 1
            assert not broker.providers.unconfirmed and not broker.providers.processes
            info = process_info(identity[0])
            assert info is None or info["state"] in {"Z", "X"}
            assert (await client.post("/jobs/" + "a" * 32 + "/cancel")).json()["state"] == "cancelled"
    finally:
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        await cleanup_fixture(broker, spawned, identity)


async def test_unconfirmed_stop_blocks_queued_and_future_writers_and_survives_restart(tmp_path, monkeypatch):
    root = tmp_path / "runner"
    broker = Broker(root)
    broker.settings({"token_hash": hashlib.sha256(b"review-token").hexdigest()})
    child_path, spawned = await install_fork_fixture(broker, tmp_path, monkeypatch)
    identity = None

    def denied_signal(*args):
        raise PermissionError("synthetic pidfd failure")

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)),
                                    base_url="https://runner", headers={"Authorization": "Bearer review-token"}) as client:
            await client.post("/jobs", json=job_body())
            identity = await child_ready(child_path)
            await client.post("/jobs", json=job_body("b" * 32))
            monkeypatch.setattr("agentic_runner.providers.signal_members", denied_signal)
            cancelled = await client.post("/jobs/" + "a" * 32 + "/cancel")
            assert cancelled.json()["state"] == "stop_unconfirmed"
            await asyncio.gather(*list(broker.tasks.values()), return_exceptions=True)
            assert broker.job("b" * 32)["state"] == "stop_unconfirmed"
            assert len(spawned) == 1
            assert (await client.post("/jobs", json=job_body("c" * 32))).status_code == 409
            assert (await client.post("/providers/codex/logout")).status_code == 409
            assert (await client.get("/status")).json()["execution_state"] == "stop_unconfirmed"
    finally:
        await cleanup_fixture(broker, spawned, identity)
    restarted = Broker(root)
    try:
        assert restarted.job("a" * 32)["state"] == "stop_unconfirmed"
        with pytest.raises(StopUnconfirmed):
            restarted.providers.ensure_available()
    finally:
        restarted.db.close()


async def test_recycled_process_identity_is_never_signalled(tmp_path, monkeypatch):
    providers = Providers(tmp_path)
    proc = type("Fixture", (), {"pid": 654321, "_agentic_fence": {
        "uid": os.getuid(), "members": {654321: 10}, "lock": asyncio.Lock(), "stopped": False}})()
    monkeypatch.setattr("agentic_runner.providers.group_members", lambda *_: {654321: 11})
    signals = []
    monkeypatch.setattr("agentic_runner.providers.signal_members", lambda *args: signals.append(args))
    with pytest.raises(StopUnconfirmed):
        await providers.stop(proc)
    assert signals == []
    assert providers.unconfirmed


async def test_pair_recovery_survives_restart_and_pair_file_refresh_without_rotating_bearer(tmp_path):
    root, handoff = tmp_path / "runner", tmp_path / "pairing" / "runner.json"
    broker = Broker(root)
    broker.refresh_pairing_file(handoff)
    original = handoff.read_bytes()
    deadline = broker.code_expires
    body = {"code": broker.code, "controller_id": "a" * 32, "principal": "owner", "protocol": 1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner") as client:
        first = await client.post("/pair", json=body)
        assert first.status_code == 200
        token = first.json()["token"]
    broker.db.close()
    broker = Broker(root)
    try:
        assert broker.refresh_pairing_file(handoff, deadline - 60)
        assert handoff.read_bytes() == original
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner") as client:
            recovered = await client.post("/pair", json={**body, "code": json.loads(handoff.read_text())["code"]})
            assert recovered.status_code == 200 and recovered.json()["token"] == token
            for changed in ({"principal": "other"}, {"controller_id": "b" * 32}, {"code": "x" * 32}):
                assert (await client.post("/pair", json={**body, **changed})).status_code == 409
            recovery = broker.setting("pair_recovery")
            broker.settings({"pair_recovery": {**recovery, "expires": time.time() - 1}})
            assert (await client.post("/pair", json=body)).status_code == 409
        assert not broker.refresh_pairing_file(handoff, deadline)
        assert not handoff.exists()
        key = root / "private/pairing-recovery.key"
        assert key.stat().st_mode & 0o777 == 0o600
        assert token.encode() not in (root / "runner.sqlite3").read_bytes()
        assert broker.setting("token_hash") == hashlib.sha256(token.encode()).hexdigest()
    finally:
        broker.db.close()


@pytest.mark.parametrize("tamper", ["fingerprint", "code"])
async def test_pair_file_recovery_rejects_mismatched_handoff(tmp_path, tamper):
    broker = Broker(tmp_path / "runner")
    handoff = tmp_path / "pairing/runner.json"
    broker.refresh_pairing_file(handoff)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner") as client:
            await client.post("/pair", json={"code": broker.code, "controller_id": "a" * 32,
                                            "principal": "owner", "protocol": 1})
        data = json.loads(handoff.read_text())
        data[tamper] = "wrong"
        handoff.write_text(json.dumps(data))
        assert not broker.refresh_pairing_file(handoff)
        assert not handoff.exists()
    finally:
        broker.db.close()


@pytest.mark.parametrize("invalid", ["permissions", "symlink", "length"])
async def test_imported_pair_recovery_seed_fails_closed(tmp_path, invalid):
    broker = Broker(tmp_path)
    key = tmp_path / "private/pairing-recovery.key"
    if invalid == "symlink":
        target = tmp_path / "other-key"
        target.write_bytes(b"a" * 48)
        key.symlink_to(target)
    else:
        key.write_bytes(b"a" * (47 if invalid == "length" else 48))
        key.chmod(0o644 if invalid == "permissions" else 0o600)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner") as client:
            result = await client.post("/pair", json={"code": broker.code, "controller_id": "a" * 32,
                                                     "principal": "owner", "protocol": 1})
            assert result.status_code == 409 and not broker.setting("token_hash")
    finally:
        broker.db.close()


def test_imported_fifo_pair_seed_is_rejected_without_blocking_api(tmp_path):
    root = tmp_path / "runner"
    broker = Broker(root)
    broker.db.close()
    os.mkfifo(root / "private/pairing-recovery.key", 0o600)
    script = '''
import asyncio,sys
from pathlib import Path
import httpx
from agentic_runner.app import Broker,create_api
async def main():
    broker=Broker(Path(sys.argv[1]))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)),base_url='https://runner') as client:
            result=await client.post('/pair',json={'code':broker.code,'controller_id':'a'*32,'principal':'fixture','protocol':1})
            assert result.status_code==409
            assert not broker.setting('token_hash')
            print('rejected_fifo')
    finally:
        broker.db.close()
asyncio.run(main())
'''
    # An asyncio timeout cannot interrupt blocking os.open; enforce this bound
    # outside the API process, and let subprocess kill/reap it on regression.
    result = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "rejected_fifo"


def test_actual_packaged_runner_status_all_four_providers(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Packaged CLI requires --containers")
    name = "agentic-review-health-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    source = Path(__file__).parents[1] / "runner/agentic_runner"
    script = '''
import asyncio,hashlib,json
from pathlib import Path
import aiohttp,uvicorn
from agentic_runner.app import Broker,create_api
async def main():
    broker=Broker(Path('/data'))
    broker.settings({'token_hash':hashlib.sha256(b'fixture').hexdigest()})
    server=uvicorn.Server(uvicorn.Config(create_api(broker),host='127.0.0.1',port=8787,access_log=False,log_level='error'))
    task=asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(30):
          while not server.started: await asyncio.sleep(.01)
          async with aiohttp.ClientSession(headers={'Authorization':'Bearer fixture'}) as client:
            async with client.get('http://127.0.0.1:8787/status') as result:
                assert result.status==200,result.status
                body=await result.json()
            assert body['execution_state']=='idle',body
            assert {p['provider'] for p in body['providers']}=={'codex','claude','agy','grok'}
            assert all(p['status']=='needs_reauth' and p['version_matches'] for p in body['providers']),body
            assert not broker.providers.unconfirmed
            print(json.dumps(body))
    finally:
        server.should_exit=True
        await task
        broker.db.close()
asyncio.run(main())
'''
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--network=none", "--read-only",
            "--pids-limit=128", "--memory=1g", "-v", volume + ":/data", "-v", str(source) + ":/app/agentic_runner:ro",
            "--entrypoint", "python", os.environ.get("AGENTIC_RUNNER_IMAGE", "agentic-home-runner:0.6.1"), "-c", script],
            capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, check=False)
