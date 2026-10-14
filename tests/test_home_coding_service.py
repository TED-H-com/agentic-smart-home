"""Offline shared-service/local transport proof; hard isolation is tested separately on HAOS."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host.coding_program import program, verification_program  # noqa: E402
from agentic_host.coding_sandbox import SandboxError  # noqa: E402
from agentic_host.coding_service import CodingService  # noqa: E402
from agentic_runner import home_service  # noqa: E402
from agentic_runner.app import Broker, create_api  # noqa: E402
from agentic_runner.coding import CodingCredentials  # noqa: E402
from agentic_runner.home_service import HomeRelay, HomeService, validate_source  # noqa: E402


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


@pytest.mark.parametrize("existing", [False, True])
def test_coding_auth_umask077_parent_traversable_and_profiles_private(tmp_path, existing):
    root = tmp_path / "coding-auth"
    previous = os.umask(0o077)
    try:
        if existing:
            (root / "profiles").mkdir(parents=True, mode=0o700)
        role = CodingCredentials(root)
    finally:
        os.umask(previous)
    assert role.root.stat().st_mode & 0o777 == 0o711
    assert (role.root / "profiles").stat().st_mode & 0o777 == 0o711
    assert (role.root / "private").stat().st_mode & 0o777 == 0o700
    for provider in ("codex", "claude", "agy", "grok"):
        assert role.profile(provider).stat().st_mode & 0o777 == 0o700
    assert role.environment("codex")["CODEX_HOME"] == str(role.profile("codex"))


def test_actual_coding_auth_uid1010_under_umask077(request):
    if not request.config.getoption("--containers"):
        pytest.skip("Actual UID and pinned Codex proof requires --containers")
    root = Path(__file__).parents[1]
    name = "agentic-home-auth-dac-" + uuid.uuid4().hex[:12]
    script = r'''
import asyncio,json,os,subprocess
from pathlib import Path
from agentic_runner.coding import CodingCredentials
os.umask(0o077)
base=Path('/fixture');base.mkdir(mode=0o711);base.chmod(0o711)
async def check():
 for existing in (False,True):
  root=base/str(existing)
  if existing:(root/'profiles').mkdir(parents=True,mode=0o700)
  role=CodingCredentials(root)
  assert (root/'profiles').stat().st_uid==0
  assert (root/'profiles').stat().st_mode&0o777==0o711
  home=role.profile('codex')
  assert home.stat().st_uid==1010 and home.stat().st_mode&0o777==0o700
  result=await role.secret_login('codex','api_key','sk-offline-dac-fixture-not-a-real-key')
  assert result['status']=='connected',result
  assert (home/'auth.json').stat().st_uid==1010
  assert (home/'auth.json').stat().st_mode&0o777==0o600
  program="""
import os,sys
from pathlib import Path
root=Path(sys.argv[1])
assert (root/'profiles/codex/auth.json').read_bytes()
for path in (root,root/'profiles',root/'private',root/'profiles/claude'):
 try:list(path.iterdir())
 except PermissionError:pass
 else:raise AssertionError('private directory could be listed')
try:(root/'profiles/other').mkdir()
except PermissionError:pass
else:raise AssertionError('sibling profile could be created')
"""
  child=subprocess.run(['python','-I','-c',program,str(root)],user=1010,group=1010,extra_groups=[],
    env={'PATH':'/usr/local/bin:/usr/bin:/bin'},capture_output=True,text=True,timeout=10)
  assert child.returncode==0,(child.returncode,child.stderr)
 print(json.dumps({'fresh_and_existing':True,'umask':'0077','uid':1010,
  'actual_codex_local_key_storage':True,'parent_traversal_without_listing':True,'real_account_verified':False}))
asyncio.run(check())
'''
    try:
        result = subprocess.run(["docker", "run", "--rm", "--name", name, "--network=none",
            "--memory=512m", "--memory-swap=512m", "--pids-limit=128", "--cpus=1",
            "--mount", f"type=bind,src={root / 'runner/agentic_runner'},dst=/app/agentic_runner,readonly",
            "--entrypoint", "python", os.environ.get("AGENTIC_RUNNER_IMAGE", "agentic-home-runner:0.9.0"),
            "-c", script], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        value = json.loads(result.stdout)
        assert value["uid"] == 1010 and value["actual_codex_local_key_storage"] is True
        assert value["real_account_verified"] is False and value["parent_traversal_without_listing"] is True
    finally:
        subprocess.run(["docker", "rm", "-fv", name], capture_output=True, check=False, timeout=120)


@pytest.fixture
def packaged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Home fixture")
    git(repo, "config", "user.email", "home@example.invalid")
    (repo / "value.py").write_text("value = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic offline source fixture")
    bare = tmp_path / "packaged/source"
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "--bare", "--no-hardlinks", str(repo), str(bare)], check=True, capture_output=True)
    bare.chmod(0o700)
    lock = {"repo_id": "agentic-home", "repository": home_service.REPOSITORY, "commit": git(repo, "rev-parse", "HEAD")}
    bare.with_suffix(".json").write_text(json.dumps(lock))
    bare.with_suffix(".json").chmod(0o600)
    return bare, lock


class BoundaryFixture:
    calls = []
    def __init__(self, root, image, repositories, *, limits, relay=None, inputs=None):
        self.root = root
        self.settings = {"image": image, "repositories": repositories, "limits": limits, "relay": relay, "inputs": inputs}
        self.calls.append(self)
    def preflight(self):
        return None
    def cancel(self, id):
        return {"state": "stop_unconfirmed"}


def request(**changes):
    return {"work_contract_major": 1, "id": "a" * 32, "case_id": "b" * 32,
        "worker_id": "home-coder", "mode": "development", "provider": "codex", "model": "",
        "repo_id": "agentic-home", "goal": "Investigate local facts", "questions": [], "base_commit": None,
        "expires": time.time() + 100, "lease_epoch": 1, "context_days": 7, **changes}


def test_exact_packaged_source_used_only_as_private_clone_input(packaged, tmp_path):
    bare, lock = packaged
    assert validate_source(bare) == lock
    private = tmp_path / "private-clone"
    subprocess.run(["git", "clone", "--no-local", "--no-hardlinks", str(bare), str(private)],
                   check=True, capture_output=True)
    (private / "value.py").write_text("private edits\n")
    assert git(bare, "show", "HEAD:value.py") == "value = 1"
    assert not (private / ".git/objects/info/alternates").exists()
    bare.with_suffix(".json").write_text(json.dumps({**lock, "repository": "https://other.invalid"}))
    with pytest.raises(SandboxError):
        validate_source(bare)


@pytest.mark.parametrize("attack", ["symlink", "fifo", "repository", "commit", "writable"])
def test_packaged_identity_rejected_before_source_copy(packaged, tmp_path, attack):
    bare, lock = packaged
    metadata = bare.with_suffix(".json")
    if attack in {"symlink", "fifo"}:
        metadata.unlink()
        if attack == "symlink":
            metadata.symlink_to(tmp_path / "private")
        else:
            os.mkfifo(metadata)
    elif attack == "writable":
        metadata.chmod(0o666)
    else:
        metadata.write_text(json.dumps({**lock, attack: "a" * 40 if attack == "commit" else "https://other.invalid"}))
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises((SandboxError, OSError)):
        validate_source(bare)
    assert not (root / "source").exists()


def test_home_identity_credentials_refusal_receipt_and_no_host_runtime(packaged, tmp_path):
    bare, lock = packaged
    service = HomeService(tmp_path / "data", packaged_source=bare, sandbox_factory=BoundaryFixture)
    try:
        caps = service.capabilities()
        assert caps["worker_id"] == "home-coder" and caps["runtime_location"] == "haos_app"
        assert caps["source_commit"] == lock["commit"] and caps["install_commit"] is None
        assert caps["available"] is True and caps["providers"][0]["credential_state"] == "needs_auth"
        payload = request()
        status, job = service.handle("POST", "/coding/jobs", payload)
        assert status == 202 and job["error_code"] == "coding_needs_auth"
        assert not job["execution_started"] and job["stop_confirmed"]
        assert job["runtime_location"] == "haos_app"
        assert service.handle("POST", "/coding/jobs", payload) == (status, job)
        assert service.handle("POST", "/coding/jobs", request(worker_id="host-coder"))[0] == 409
        for method, path in (("GET", "/coding/runtime"), ("POST", "/coding/reconcile-runtime")):
            assert service.handle(method, path, {})[0] == 409
        assert service.credential_path == tmp_path / "data/coding-auth/profiles/codex/auth.json"
        assert service.image is None
        assert service.source == bare and service.source_lock_path == bare.with_suffix(".json")
        assert not (service.root / "source").exists()
    finally:
        service.close()


def test_missing_boundary_preserves_explicit_coding_block(packaged, tmp_path):
    service = HomeService(tmp_path / "data", packaged_source=packaged[0], sandbox_factory=BoundaryFixture,
                          runtime_ready=False)
    try:
        caps = service.capabilities()
        assert caps["available"] is False and caps["reason"] == "hard_boundary_unavailable"
        assert service.submit(request())[1]["error_code"] == "coding_boundary_unavailable"
    finally:
        service.close()


def test_observe_fresh_fixed_scope_limit_and_no_model_network_or_secret(tmp_path):
    observed = []
    def observer():
        observed.append(len(observed) + 1)
        return {"sample": observed[-1], "core_api_from_app": {"http_status": 401, "reachable": True}}
    relay = HomeRelay(tmp_path / "a.sock", tmp_path / "never-open-auth.json", time.time() + 30,
                      observer=observer, version_refs={"app": "fixture", "image": "unavailable"})
    for index in range(8):
        status, _, raw = relay.request("GET", "/observe", b"")
        value = json.loads(raw)
        assert status == 200 and value["content"]["sample"] == index + 1
        assert value["observed_from"] == "haos_app"
        content = json.dumps(value["content"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        assert value["content_digest"] == hashlib.sha256(content).hexdigest()
    with pytest.raises(SandboxError):
        relay.request("GET", "/observe", b"")
    assert len(relay.evidence()) == 8 and not relay.credential_path.exists()
    for method, path, body in (("POST", "/observe", b""), ("GET", "/observe", b"url=secret"),
                              ("GET", "/observe?url=http://supervisor", b"")):
        with pytest.raises(SandboxError):
            relay.request(method, path, body)


def test_observe_cannot_complete_after_cancel_or_expiry(tmp_path):
    relay = None
    def observer():
        relay.deadline = 0
        return {"fixed": True}
    relay = HomeRelay(tmp_path / "a.sock", tmp_path / "auth", time.time() + 30, observer=observer)
    with pytest.raises(SandboxError):
        relay.request("GET", "/observe", b"")
    assert relay.evidence() == []


def test_network_probe_fixed_target_clean_env_and_timeout(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"dns_resolved": True, "reachable": True,
            "http_status": 401, "http_port": 80, "attempted_ports": [80],
            "endpoints": [{"port": 80, "reachable": True, "http_status": 401, "error_code": None}]}).encode())
    monkeypatch.setenv("SUPERVISOR_TOKEN", "must-never-copy")
    monkeypatch.setattr(home_service.subprocess, "run", run)
    assert home_service.network_observation()["http_status"] == 401
    argv, kwargs = calls[0]
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"} and kwargs["timeout"] == 3
    assert "homeassistant" in argv[3] and "response.read" not in argv[3] and "Authorization" not in argv[3]
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 3)
    monkeypatch.setattr(home_service.subprocess, "run", timeout)
    assert home_service.network_observation()["timed_out"] is True


@pytest.mark.parametrize("replies,port,status,attempted", [
    ({80: 401}, 80, 401, [80]), ({80: 200}, 80, 200, [80]),
    ({8123: 401}, 8123, 401, [80, 8123]), ({}, None, None, [80, 8123]),
    ({80: 302, 8123: 401}, 80, 302, [80])])
def test_actual_observer_program_only_fixed_ports_no_auth_body_or_redirect(monkeypatch, replies, port, status, attempted):
    import http.client
    import socket
    calls, closed = [], []
    def resolve(host, lookup_port, *, type):
        assert host == "homeassistant" and lookup_port == 80 and type == socket.SOCK_STREAM
        return []
    class Connection:
        def __init__(self, host, http_port, timeout):
            assert host == "homeassistant" and http_port in (80, 8123) and timeout == 1
            self.port = http_port
        def request(self, method, path, *, headers):
            assert method == "GET" and path == "/api/"
            assert headers == {"Accept": "application/json", "Connection": "close"}
            calls.append(self.port)
            if self.port not in replies:
                raise ConnectionRefusedError("fixture unavailable")
        def getresponse(self):
            # No read/body/location methods: observing only the status is allowed.
            return SimpleNamespace(status=replies[self.port])
        def close(self):
            closed.append(self.port)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(home_service.NETWORK_OBSERVATION, "fixed-app-observer", "exec"), {})
    raw = output.getvalue().encode()
    monkeypatch.setattr(home_service.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=raw))
    value = home_service.network_observation()
    assert calls == attempted and closed == attempted
    assert value == {"dns_resolved": True, "reachable": port is not None, "http_status": status,
        "http_port": port, "attempted_ports": attempted, "timed_out": False,
        "endpoints": [{"port": target, "reachable": target in replies, "http_status": replies.get(target),
                       "error_code": None if target in replies else "connection_failed"} for target in attempted]}


@pytest.mark.parametrize("change", [{"http_port": 1234}, {"attempted_ports": [8123]},
    {"attempted_ports": [80, 8123, 1234]}, {"attempted_ports": "80"},
    {"http_status": True}, {"reachable": 1}, {"dns_resolved": False},
    {"http_status": None}, {"http_port": None}, {"http_port": 80.0}, {"attempted_ports": [80.0]},
    {"endpoints": []}, {"endpoints": [{"port": 80, "reachable": False, "http_status": None, "error_code": "private text"}]},
    {"endpoints": [{"port": 80, "reachable": False, "http_status": None, "error_code": "connection_failed"}]}])
def test_network_observation_rejects_malformed_or_unproven_endpoint(monkeypatch, change):
    value = {"dns_resolved": True, "reachable": True, "http_status": 401,
             "http_port": 80, "attempted_ports": [80],
             "endpoints": [{"port": 80, "reachable": True, "http_status": 401, "error_code": None}], **change}
    monkeypatch.setattr(home_service.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=json.dumps(value).encode()))
    result = home_service.network_observation()
    assert result["observation_failed"] and result["reachable"] is False
    assert result["http_port"] is None and result["attempted_ports"] is None and result["endpoints"] is None


def test_home_program_explicit_observation_and_verifier_location():
    value = program(request(), observed_from="haos_app")
    compile(value, "home-program", "exec")
    assert "OBSERVED_FROM='haos_app'" in value and "127.0.0.1:8765/observe" in value
    verifier = verification_program("independent_home_verifier")
    compile(verifier, "home-verifier", "exec")
    assert verifier.startswith("VERIFICATION_LOCATION='independent_home_verifier'")
    assert "SUPERVISOR_TOKEN" not in value + verifier


async def test_actual_tls_api_routes_local_backend_off_loop_and_keep_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_HOST_MODE", raising=False)
    broker = Broker(tmp_path / "runner")
    broker.settings({"token_hash": hashlib.sha256(b"fixture-token").hexdigest()})
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Local:
        def handle(self, method, path, body=None):
            calls.append((threading.get_ident(), method, path, body))
            if path == "/coding/capabilities":
                entered.set()
                assert release.wait(3)
            return 200, {"state": "ready", "worker_id": "home-coder"}
    broker.home_coding = Local()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner") as client:
            assert (await client.get("/coding/capabilities")).status_code == 401
            assert not calls
            client.headers["Authorization"] = "Bearer fixture-token"
            pending = asyncio.create_task(client.get("/coding/capabilities"))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            # The event loop and independent read-only API remain usable while backend blocks.
            assert (await asyncio.wait_for(client.get("/retention"), 0.5)).status_code == 200
            release.set()
            assert (await pending).json()["worker_id"] == "home-coder"
            assert calls[0][0] != threading.get_ident()
            assert (await client.post("/coding/jobs", json=request(worker_id="host-coder"))).status_code == 409
            assert (await client.post("/coding/jobs", json=request())).status_code == 200
            assert calls[-1][2] == "/coding/jobs"
            assert (await client.post("/retention", json={"context_days": 1})).status_code == 200
            assert calls[-1][2:] == ("/coding/retention", {"context_days": 1})
    finally:
        release.set()
        broker.db.close()


def test_shared_host_defaults_unchanged(tmp_path):
    service = CodingService(SimpleNamespace(root=tmp_path / "host", product=tmp_path / "product"))
    try:
        assert service.worker_id == "host-coder" and service.runtime_location == "host_container"
        assert service.credential_path == tmp_path / "runner/coding-auth/profiles/codex/auth.json"
        assert service.verifier_location == "independent_host_verifier"
        assert service.submit(request(worker_id="host-coder"))[1]["error_code"] == "coding_boundary_unavailable"
    finally:
        service.close()


def test_shared_home_execution_collects_app_evidence_then_separate_verifier(packaged, tmp_path, monkeypatch):
    from agentic_host import coding_service
    monkeypatch.setattr(coding_service, "credentials", lambda _: ("fixture", "/responses", {}))
    launches = []
    class GitBoundary(BoundaryFixture):
        def launch(self, id, epoch, repo, commit, code):
            launches.append((id, self.settings, code))
            workspace = self.root / id / "workspace"
            workspace.mkdir(parents=True)
            for name in ("artifacts", "scratch", "profile"):
                (workspace / name).mkdir()
            subprocess.run(["git", "clone", "--no-hardlinks", str(self.settings["repositories"][repo]),
                            str(workspace / "source")], check=True, capture_output=True)
            assert git(workspace / "source", "rev-parse", "HEAD") == commit
            if self.settings["inputs"]:
                assert self.settings["relay"] is None
                assert self.settings["inputs"]["candidate.patch"].startswith(b"diff --git a/value.py")
                subprocess.run(["git", "-C", str(workspace / "source"), "apply", "-"],
                               input=self.settings["inputs"]["candidate.patch"], check=True, capture_output=True)
                assert (workspace / "source/value.py").read_text() == "value = 2\n"
                (workspace / "artifacts/verification.json").write_text('{"fixture_verifier":true}')
            else:
                (workspace / "source/value.py").write_text("value = 2\n")
                (workspace / "artifacts/findings.txt").write_text("Fixture result, not actual cloud inference")
        def status(self, id):
            return {"state": "succeeded", "exit_code": 0, "stop_confirmed": True,
                    "resource_counters": {"memory.events": {"oom_kill": 0}, "pids.events": {"max": 0},
                                          "cpu.stat": {"usage_usec": 7}, "cgroup.events": {"populated": 0}}}
        cancel = status
        def artifacts(self, id):
            return [{"name": p.name, "content": p.read_text(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in (self.root / id / "workspace/artifacts").iterdir()]
        def dispose(self, id):
            return {"job_id": id, "lease_epoch": 1, "state": "disposed", "stop_confirmed": True}
    class ObservationRelay(HomeRelay):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, observer=lambda: {"fixture_app_observation": True}, **kwargs)
        def start(self):
            self.request("GET", "/observe", b"")
        def close(self):
            self.deadline = 0
    service = HomeService(tmp_path / "data", packaged_source=packaged[0], sandbox_factory=GitBoundary,
                          relay_factory=ObservationRelay)
    try:
        payload = request()
        status, queued = service.submit(payload)
        assert status == 202 and queued["state"] == "queued"
        service.threads[payload["id"]].join(5)
        assert not service.threads[payload["id"]].is_alive()
        result = service.handle("GET", "/coding/jobs/" + payload["id"])[1]
        assert result["state"] == "succeeded" and result["stop_confirmed"] is True
        assert result["verification"]["observed_from"] == "independent_home_verifier"
        assert result["verification"]["patch_sha256"] == hashlib.sha256(launches[1][1]["inputs"]["candidate.patch"]).hexdigest()
        assert result["verification"]["network"] == "none"
        assert len(launches) == 2 and launches[0][1]["relay"] is not None and launches[1][1]["relay"] is None
        assert "VERIFICATION_LOCATION='independent_home_verifier'" in launches[1][2]
        assert len(result["evidence"]) == 3 and all(e["observed_from"] == "haos_app" for e in result["evidence"])
        assert result["evidence"][2]["content"]["cgroup.events"]["populated"] == 0
        assert result["evidence"][1]["content"] == {"fixture_app_observation": True}
        assert result["evidence"][0]["version_refs"]["image"] == "unavailable"
        assert git(service.source, "show", "HEAD:value.py") == "value = 1"
        assert result["workspace_released"] is True
        assert not (service.root / "work" / payload["id"]).exists()
        assert service.submit(payload)[1] == result and len(launches) == 2
        patch = next(a for a in result["artifacts"] if a["kind"] == "patch")
        exported = service.handle("GET", "/coding/jobs/" + payload["id"] + "/artifacts/" + patch["id"])[1]
        assert exported["sha256"] == result["verification"]["patch_sha256"]
    finally:
        service.close()


def test_home_persisted_uncertain_writer_and_auth_fences_never_replay(packaged, tmp_path):
    root = tmp_path / "data"
    service = HomeService(root, packaged_source=packaged[0], sandbox_factory=BoundaryFixture)
    payload = request()
    _, failed = service.submit(payload)
    uncertain = service.items[payload["id"]]
    uncertain.update(state="stop_unconfirmed", stop_confirmed=False, execution_started=True)
    service.persist()
    service.close()
    restored = HomeService(root, packaged_source=packaged[0], sandbox_factory=BoundaryFixture)
    try:
        assert restored.items[payload["id"]]["state"] == "stop_unconfirmed"
        assert restored.submit(payload)[1]["stop_confirmed"] is False
        assert restored.submit(request(id="c" * 32))[1]["error_code"] == "coding_writer_or_auth_fence_active"
        assert restored.handle("POST", "/coding/fence", {"id": "d" * 32})[0] == 409
        assert restored.threads == {}
        assert failed["execution_started"] is False
    finally:
        restored.close()


def partial_service(packaged, tmp_path, monkeypatch):
    """Real service/ledger/files; only the mount/process adapter is substituted."""
    from agentic_host import coding_service
    state = {"mounted": False, "calls": [], "launch_receipt": None, "dispose_error": False}
    class PartialBoundary(BoundaryFixture):
        def launch(self, id, epoch, repo, commit, code):
            state["launch_receipt"] = json.loads((self.root.parent / "jobs.json").read_text())[id]
            directory = self.root / id
            (directory / "workspace").mkdir(parents=True)
            (directory / "workspace/partial.txt").write_text("partial allocation")
            (self.root / (id + ".json")).write_text(json.dumps({"job_id": id, "lease_epoch": epoch}))
            state["mounted"] = True
            raise SandboxError("Fixture failure after private filesystem allocation")
        def cancel(self, id):
            if not (self.root / (id + ".json")).exists():
                raise SandboxError("Unknown job")
            state["calls"].append("cancel")
            return {"state": "cancelled", "stop_confirmed": True}
        def dispose(self, id):
            state["calls"].append("dispose")
            if state["dispose_error"]:
                raise SandboxError("Unconfirmed private mount")
            value = json.loads((self.root / (id + ".json")).read_text())
            if not state.get("keep_mounted"):
                state["mounted"] = False
            return {**value, "state": "disposed", "stop_confirmed": True, **state.get("override", {})}
    class Relay:
        def __init__(self, path, *args, **kwargs):
            self.path = path
        def start(self):
            pass
        def close(self):
            pass
    monkeypatch.setattr(coding_service, "credentials", lambda _: ("fixture", "/responses", {}))
    original_mount, original_remove = coding_service.os.path.ismount, coding_service.shutil.rmtree
    def mounted(path):
        return state["mounted"] if Path(path).name == "workspace" else original_mount(path)
    def remove(path, *args, **kwargs):
        if Path(path).parent.name == "work":
            assert not state["mounted"], "attempted to remove a still-mounted partial workspace"
            state["calls"].append("remove")
        return original_remove(path, *args, **kwargs)
    monkeypatch.setattr(coding_service.os.path, "ismount", mounted)
    monkeypatch.setattr(coding_service.shutil, "rmtree", remove)
    service = HomeService(tmp_path / "data", packaged_source=packaged[0], sandbox_factory=PartialBoundary,
                          relay_factory=Relay)
    return service, state


def test_partial_launch_attempt_is_durable_and_cancel_disposes_without_execution_ack(packaged, tmp_path, monkeypatch):
    service, state = partial_service(packaged, tmp_path, monkeypatch)
    payload = request()
    try:
        service.submit(payload)
        service.threads[payload["id"]].join(3)
        assert not service.threads[payload["id"]].is_alive()
        assert state["launch_receipt"].get("sandbox_attempted") is True
        assert state["launch_receipt"]["execution_started"] is False
        assert service.items[payload["id"]]["state"] == "stop_unconfirmed"
        result = service.cancel(payload["id"])
        assert result["stop_confirmed"] and result["execution_started"] is False
        assert result["workspace_released"] is True
        assert state["calls"] == ["cancel", "dispose", "remove"]
        assert not (service.root / "work" / payload["id"]).exists()
    finally:
        service.close()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("dispose_error", [False, True])
def test_retention_partial_workspace_requires_exact_disposal_before_removal(packaged, tmp_path, monkeypatch, legacy, dispose_error):
    service, state = partial_service(packaged, tmp_path, monkeypatch)
    payload = request()
    try:
        service.submit(payload)
        service.threads[payload["id"]].join(3)
        job = service.items[payload["id"]]
        job.update(state="cancelled", stop_confirmed=True, retained_until=0)
        if legacy:
            job.pop("sandbox_attempted", None)
        state["dispose_error"] = dispose_error
        service.prune()
        if dispose_error:
            assert job["cleanup_pending"] and not job.get("workspace_released")
            assert not job.get("content_expired") and state["mounted"]
            assert (service.root / "work" / payload["id"] / "workspace/partial.txt").is_file()
            assert "remove" not in state["calls"]
        else:
            assert job["workspace_released"] and job["state"] == "expired"
            assert state["calls"] == ["dispose", "remove"]
            assert not (service.root / "work" / payload["id"]).exists()
    finally:
        service.close()


@pytest.mark.parametrize("override", [{"job_id": "e" * 32}, {"lease_epoch": 2},
                                     {"state": "cancelled"}, {"stop_confirmed": False}])
def test_partial_cleanup_rejects_wrong_disposal_identity_or_stop_proof(packaged, tmp_path, monkeypatch, override):
    service, state = partial_service(packaged, tmp_path, monkeypatch)
    payload = request()
    try:
        service.submit(payload)
        service.threads[payload["id"]].join(3)
        job = service.items[payload["id"]]
        job.update(state="cancelled", stop_confirmed=True, retained_until=0)
        state["override"] = override
        service.prune()
        assert job["cleanup_pending"] and not job.get("content_expired")
        assert (service.root / "work" / payload["id"]).is_dir()
        assert "remove" not in state["calls"]
    finally:
        service.close()


def test_disposed_receipt_does_not_allow_rmtree_of_remaining_mount(packaged, tmp_path, monkeypatch):
    service, state = partial_service(packaged, tmp_path, monkeypatch)
    payload = request()
    try:
        service.submit(payload)
        service.threads[payload["id"]].join(3)
        job = service.items[payload["id"]]
        job.update(state="cancelled", stop_confirmed=True, retained_until=0)
        state["keep_mounted"] = True
        service.prune()
        assert job["cleanup_pending"] and not job.get("content_expired")
        assert state["mounted"] and "remove" not in state["calls"]
    finally:
        service.close()


def test_missing_partial_ledger_never_becomes_stop_proof_on_restart(packaged, tmp_path, monkeypatch):
    service, state = partial_service(packaged, tmp_path, monkeypatch)
    payload = request()
    service.submit(payload)
    service.threads[payload["id"]].join(3)
    ledger = service.root / "work" / (payload["id"] + ".json")
    saved = ledger.read_bytes()
    ledger.unlink()  # Lost private ledger is not evidence no allocation occurred.
    service.closed.set()
    service.maintenance.join(1)
    restarted = HomeService(tmp_path / "data", packaged_source=packaged[0], sandbox_factory=service.sandbox_factory)
    try:
        job = restarted.items[payload["id"]]
        assert job["sandbox_attempted"] and not job["execution_started"]
        assert job["state"] == "stop_unconfirmed" and not job["stop_confirmed"]
        job["retained_until"] = 0
        assert restarted.handle("POST", "/coding/retention", {"context_days": 1})[0] == 409
        assert "dispose" not in state["calls"] and "remove" not in state["calls"]
        assert (restarted.root / "work" / payload["id"]).is_dir()
    finally:
        ledger.write_bytes(saved)
        restarted.close()


async def test_home_missing_service_retention_does_not_ack_complete(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_HOST_MODE", raising=False)
    broker = Broker(tmp_path / "runner")
    broker.settings({"token_hash": hashlib.sha256(b"fixture-token").hexdigest()})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_api(broker)), base_url="https://runner",
                                     headers={"Authorization": "Bearer fixture-token"}) as client:
            caps = await client.get("/coding/capabilities")
            assert caps.status_code == 200 and caps.json()["available"] is False
            assert (await client.post("/retention", json={"context_days": 1})).status_code == 503
            assert (await client.post("/coding/jobs", json=request())).status_code == 503
    finally:
        broker.db.close()


async def test_home_boundary_startup_failure_keeps_runner_and_removes_supervisor_secret(tmp_path, monkeypatch):
    from agentic_runner import app as runner_app
    from agentic_runner import home_launcher
    monkeypatch.delenv("AGENTIC_HOST_MODE", raising=False)
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path / "runner"))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fixture-private-supervisor-token")
    monkeypatch.setenv("HASSIO_TOKEN", "fixture-private-legacy-supervisor-token")
    calls = []
    def initialize():
        calls.append("namespace")
        raise home_launcher.BoundaryError("fixture_unavailable")
    def broker_factory(root):
        assert calls == ["namespace"]
        broker = Broker(root)
        broker.settings({"token_hash": hashlib.sha256(b"fixture-token").hexdigest()})
        return broker
    class Local:
        def __init__(self, root, *, runtime_ready):
            assert runtime_ready is False and "SUPERVISOR_TOKEN" not in os.environ
            assert "HASSIO_TOKEN" not in os.environ
            calls.append("home_service")
        def prune(self, days):
            assert days == 7
        def close(self):
            calls.append("closed")
    class Server:
        def __init__(self, config):
            self.config = config
        async def serve(self):
            calls.append("served")
    monkeypatch.setattr(home_launcher, "initialize_home_runtime", initialize)
    monkeypatch.setattr(runner_app, "Broker", broker_factory)
    monkeypatch.setattr(home_service, "HomeService", Local)
    monkeypatch.setattr(runner_app.uvicorn, "Server", Server)
    previous_umask = os.umask(0o077)
    try:
        await runner_app.serve()
    finally:
        os.umask(previous_umask)
    assert calls == ["namespace", "home_service", "served", "served", "closed"]


@pytest.mark.parametrize("old_stopped", [True, False])
def test_packaged_source_upgrade_new_dispatch_only_after_old_writer_stop(packaged, tmp_path, monkeypatch, old_stopped):
    from agentic_host import coding_service
    original, old_lock = packaged
    data = tmp_path / "data"
    first = HomeService(data, packaged_source=original, sandbox_factory=BoundaryFixture)
    old_request = request()
    first.submit(old_request)  # Persist exact identity first; no real provider credentials.
    record = first.items[old_request["id"]]
    record.update(state="succeeded" if old_stopped else "stop_unconfirmed", stop_confirmed=old_stopped,
                  execution_started=True, workspace_released=old_stopped, base_commit=old_lock["commit"],
                  evidence=[{"fixture_old_source": old_lock["commit"]}])
    artifact = first.root / "artifacts" / old_request["id"] / "retained.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"fixture_checkpoint":"unchanged"}')
    before = artifact.read_bytes()
    first.persist()
    first.close()
    # Simulate a new image's independently pinned immutable source at the same public repo.
    repo = tmp_path / "repo"
    (repo / "value.py").write_text("value = 3\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Updated public fixture revision")
    newer = tmp_path / "new-image/source"
    newer.parent.mkdir()
    subprocess.run(["git", "clone", "--bare", "--no-hardlinks", str(repo), str(newer)], check=True, capture_output=True)
    newer.chmod(0o700)
    new_commit = git(repo, "rev-parse", "HEAD")
    newer.with_suffix(".json").write_text(json.dumps({**old_lock, "commit": new_commit}))
    newer.with_suffix(".json").chmod(0o600)
    current = HomeService(data, packaged_source=newer, sandbox_factory=BoundaryFixture)
    started = []
    current.execute = started.append  # Only execution is substituted; acceptance/ledger/source checks are real.
    monkeypatch.setattr(coding_service, "credentials", lambda _: ("fixture", "/responses", {}))
    try:
        _, result = current.submit(request(id="e" * 32))
        if old_stopped:
            current.threads["e" * 32].join(1)
            assert result["state"] == "queued" and result["base_commit"] == new_commit
            assert started == ["e" * 32]
        else:
            assert result["error_code"] == "coding_writer_or_auth_fence_active" and not started
            assert current.items[old_request["id"]]["stop_confirmed"] is False
        assert current.items[old_request["id"]]["evidence"] == [{"fixture_old_source": old_lock["commit"]}]
        assert artifact.read_bytes() == before
        assert git(original, "rev-parse", "HEAD") == old_lock["commit"]
        assert current.source == newer
    finally:
        current.close()


@pytest.mark.parametrize("failure", ["malformed_state", "wrong_root", "null_entry", "missing_fields",
    "wrong_identity", "wrong_state", "wrong_execution_flag", "wrong_stop_flag", "wrong_cancel_id",
    "wrong_attempt_flag", "missing_retention", "wrong_retention", "prune", "prune_and_close"])
async def test_coding_startup_fault_keeps_legacy_api_and_never_publishes_partial_service(tmp_path, monkeypatch, failure):
    from agentic_runner import app as runner_app
    from agentic_runner import home_launcher
    monkeypatch.delenv("AGENTIC_HOST_MODE", raising=False)
    monkeypatch.setenv("AGENTIC_DATA", str(tmp_path / "runner"))
    calls, brokers = [], []
    monkeypatch.setattr(home_launcher, "initialize_home_runtime", lambda: None)
    def broker_factory(root):
        broker = Broker(root)
        broker.settings({"token_hash": hashlib.sha256(b"fixture-token").hexdigest()})
        brokers.append(broker)
        return broker
    original_service = HomeService
    class Local:
        def __init__(self, root, *, runtime_ready):
            if failure not in {"prune", "prune_and_close"}:
                record = {"id": "a" * 32, "state": "succeeded", "execution_started": True, "stop_confirmed": True,
                          "created": time.time(), "retained_until": time.time() + 86400}
                values = {"wrong_root": [], "null_entry": {"a" * 32: None}, "missing_fields": {"a" * 32: {}},
                    "wrong_identity": {"a" * 32: {**record, "id": "b" * 32}},
                    "wrong_state": {"a" * 32: {**record, "state": None}},
                    "wrong_execution_flag": {"a" * 32: {**record, "execution_started": 1}},
                    "wrong_stop_flag": {"a" * 32: {**record, "stop_confirmed": 1}},
                    "wrong_attempt_flag": {"a" * 32: {**record, "sandbox_attempted": "true"}},
                    "wrong_cancel_id": {"a" * 32: {**record, "sandbox_job_id": []}},
                    "missing_retention": {"a" * 32: {key: value for key, value in record.items() if key != "retained_until"}},
                    "wrong_retention": {"a" * 32: {**record, "retained_until": "invalid"}}}
                (root / "coding").mkdir()
                (root / "coding/jobs.json").write_text('{"broken":' if failure == "malformed_state" else json.dumps(values[failure]))
                # Real shared-service JSON load must fail before any worker dispatch/thread.
                original_service(root, sandbox_factory=BoundaryFixture, packaged_source=tmp_path / "missing-source")
                raise AssertionError("malformed state was accepted")
        def prune(self, days):
            calls.append("prune")
            raise OSError("fixture persistence failure")
        def close(self):
            calls.append("close")
            if failure == "prune_and_close":
                raise OSError("fixture close persistence failure")
    class Server:
        def __init__(self, config):
            self.config = config
        async def serve(self):
            assert not hasattr(brokers[0], "home_coding")
            if self.config.port != 8443:
                return
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.config.app),
                    base_url="https://runner", headers={"Authorization": "Bearer fixture-token"}) as client:
                assert (await client.get("/retention")).status_code == 200  # Existing Runner API still serves.
                caps = (await client.get("/coding/capabilities")).json()
                assert caps["available"] is False and caps["state"] == "blocked"
                assert (await client.post("/coding/jobs", json=request())).status_code == 503
                calls.append("legacy_api_available")
    monkeypatch.setattr(runner_app, "Broker", broker_factory)
    monkeypatch.setattr(home_service, "HomeService", Local)
    monkeypatch.setattr(runner_app.uvicorn, "Server", Server)
    previous_umask = os.umask(0o077)
    try:
        await runner_app.serve()
    finally:
        os.umask(previous_umask)
    assert calls == (["prune", "close", "legacy_api_available"] if failure in {"prune", "prune_and_close"}
                     else ["legacy_api_available"])


def test_existing_host_refusal_and_expired_tombstone_remain_compatible(tmp_path):
    manager = SimpleNamespace(root=tmp_path / "host", product=tmp_path / "product")
    service = CodingService(manager)
    payload = request(worker_id="host-coder")
    _, refused = service.submit(payload)
    tombstone = {**service.items[payload["id"]], "id": "c" * 32, "state": "expired", "request": None,
                 "content_expired": True, "artifacts": [], "evidence": [], "findings": {}}
    service.items[tombstone["id"]] = tombstone
    service.persist()
    service.close()
    restored = CodingService(manager)
    try:
        assert restored.public(restored.items[payload["id"]]) == refused
        assert restored.items[tombstone["id"]] == tombstone
        assert restored.threads == {}
    finally:
        restored.close()
