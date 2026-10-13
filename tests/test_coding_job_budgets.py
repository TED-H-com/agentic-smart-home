"""Offline clocks and fake sandboxes for coding budgets, restart and status."""
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host import coding_service  # noqa: E402
from agentic_host.coding_sandbox import SandboxError  # noqa: E402
from agentic_host.coding_service import (  # noqa: E402
    EXECUTION_BUDGET_SECONDS,
    FINDING_KEYS,
    MODELS,
    SETUP_BUDGET_SECONDS,
    VERIFICATION_BUDGET_SECONDS,
    parse_findings,
)
from agentic_host.daemon import install_broker_stops  # noqa: E402
from agentic_runner.home_service import REPOSITORY, HomeService  # noqa: E402


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


def bare_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-b", "main"], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "config", "user.name", "Budget fixture"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "budget@example.invalid"], cwd=repo)
    (repo / "value.py").write_text("value = 1\n")
    subprocess.check_call(["git", "add", "."], cwd=repo, stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "commit", "-m", "fixture"], cwd=repo, stdout=subprocess.DEVNULL)
    bare = tmp_path / "packaged/source"
    bare.parent.mkdir()
    subprocess.check_call(["git", "clone", "--bare", "--no-hardlinks", str(repo), str(bare)],
                          stdout=subprocess.DEVNULL)
    bare.chmod(0o700)
    lock = {"repo_id": "agentic-home", "repository": REPOSITORY, "commit": git(repo, "rev-parse", "HEAD")}
    bare.with_suffix(".json").write_text(json.dumps(lock))
    bare.with_suffix(".json").chmod(0o600)
    return bare


def request(**changes):
    return {"work_contract_major": 1, "id": "a" * 32, "case_id": "b" * 32,
            "worker_id": "home-coder", "mode": "development", "provider": "codex", "model": "",
            "repo_id": "agentic-home", "goal": "Investigate local facts", "questions": [],
            "base_commit": None, "expires": time.time() + 40000, "lease_epoch": 1, "context_days": 7,
            **changes}


FINDINGS = """
intro is not a section
facts
- alpha
inferences:
beta
**unknowns**
* gamma

## experiments
delta
next_steps:
epsilon
"""


class Clock:
    def __init__(self):
        self.mono = time.monotonic()
        self.wall = time.time()

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall


class Spec:
    def __init__(self):
        self.launch_mono = 0
        self.verify_launch_mono = 0
        self.status_mono = 0
        self.status_wall = 0
        self.status = {"state": "succeeded", "exit_code": 0, "stop_confirmed": True}
        self.verify_status = {"state": "succeeded", "exit_code": 0, "stop_confirmed": True}
        self.make_workspace = False
        self.findings = "Fixture result, not actual cloud inference"
        self.hold = None
        self.release = None
        self.launches = []
        self.strict_cancel = False
        self.launch_error = None
        self.fail_relay = False
        self.big_patch = False
        self.bad_patch = False
        self.withheld_verify = False
        self.verification_body = None
        self.auth_failed = False
        self.quota_reached = False
        self.turns_exhausted = False
        self.provider_rejected = False
        self.provider_unavailable = False
        self.cancel_calls = []
        self.cancel_result = {"state": "cancelled", "stop_confirmed": True, "exit_code": None}


def boundary(clock, spec):
    class Boundary:
        def __init__(self, root, image, repositories, *, limits, relay=None, inputs=None):
            self.root, self.repos = root, repositories
            self.limits, self.relay, self.inputs = limits, relay, inputs

        def preflight(self):
            return None

        def launch(self, id, epoch, repo, commit, code):
            spec.launches.append({"id": id, "wall": self.limits.wall_seconds, "inputs": bool(self.inputs)})
            if str(id).endswith("-verify"):
                clock.mono += spec.verify_launch_mono
                return
            clock.mono += spec.launch_mono
            if spec.make_workspace:
                workspace = self.root / id / "workspace"
                workspace.mkdir(parents=True)
                for name in ("artifacts", "scratch", "profile"):
                    (workspace / name).mkdir()
                subprocess.check_call(
                    ["git", "clone", "--no-hardlinks", str(self.repos[repo]), str(workspace / "source")],
                    stdout=subprocess.DEVNULL)
                (workspace / "source/value.py").write_text("value = 2\n")
                if spec.big_patch:
                    (workspace / "source/big.txt").write_text("y" * (180 * 1024))
                if spec.bad_patch:
                    (workspace / "source/bad.py").write_bytes(b"value = '\xff'\n")
                (workspace / "artifacts/findings.txt").write_text(spec.findings)
            if spec.launch_error == "stopped":
                error = SandboxError("fixture setup failed", code="command_failed")
                error.stopped = True
                raise error
            if spec.hold is not None:
                spec.hold.set()
                assert spec.release.wait(5)

        def status(self, id):
            if str(id).endswith("-verify"):
                return dict(spec.verify_status)
            clock.mono += spec.status_mono
            clock.wall += spec.status_wall
            return dict(spec.status)

        def cancel(self, id):
            spec.cancel_calls.append(id)
            # The real ledger has no record of a job it never launched.
            if spec.strict_cancel and all(item["id"] != id for item in spec.launches):
                raise SandboxError("Unknown job")
            return dict(spec.cancel_result)

        def artifacts(self, id):
            if spec.verification_body is not None and str(id).endswith("-verify"):
                content = spec.verification_body
                return [{"name": "verification.json", "content": content,
                         "sha256": hashlib.sha256(content.encode()).hexdigest(), "size": len(content.encode())}]
            if spec.withheld_verify and str(id).endswith("-verify"):
                report = "passed\n"
                return [
                    {"name": "secret.py", "kind": "withheld", "sha256": "ab" * 32, "size": 20},
                    {"name": "report.txt", "content": report,
                     "sha256": hashlib.sha256(report.encode()).hexdigest(), "size": len(report)},
                ]
            folder = self.root / str(id).removesuffix("-verify") / "workspace/artifacts"
            if str(id).endswith("-verify"):
                folder = self.root / id / "workspace/artifacts"
            if not folder.is_dir():
                return []
            return [{"name": path.name, "content": path.read_text(),
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in folder.iterdir() if path.is_file()]

        def dispose(self, id):
            return {"job_id": id, "lease_epoch": 1, "state": "disposed", "stop_confirmed": True}

    return Boundary


class Relay:
    auth_failed = False
    quota_reached = False
    turns_exhausted = False
    provider_rejected = False
    provider_unavailable = False

    def __init__(self, path, *args, **kwargs):
        self.path = path

    def start(self):
        if getattr(self, "fail_relay", False):
            raise OSError("relay unavailable")
        return None

    def close(self):
        return None

    def evidence(self):
        return []


def service_for(tmp_path, monkeypatch, clock, spec, *, patch_wall=False):
    monkeypatch.setattr(coding_service, "credentials", lambda _: ("fixture", "/responses", {}))
    monkeypatch.setattr(coding_service.time, "monotonic", clock.monotonic)
    if patch_wall:
        monkeypatch.setattr(coding_service.time, "time", clock.time)
    bare = bare_source(tmp_path)

    def make_relay(*args, **kwargs):
        relay = Relay(*args, **kwargs)
        relay.fail_relay = spec.fail_relay
        relay.auth_failed = spec.auth_failed
        relay.quota_reached = spec.quota_reached
        relay.turns_exhausted = spec.turns_exhausted
        relay.provider_rejected = spec.provider_rejected
        relay.provider_unavailable = spec.provider_unavailable
        return relay

    return HomeService(tmp_path / "data", packaged_source=bare, sandbox_factory=boundary(clock, spec),
                       relay_factory=make_relay)


def wait_thread(service, id):
    # Upper bound only: the job takes under a second alone, but a loaded machine can stretch it.
    service.threads[id].join(30)
    assert not service.threads[id].is_alive()


def test_parse_findings_all_some_and_none():
    parsed = parse_findings(FINDINGS)
    assert parsed == {"facts": ["alpha"], "inferences": ["beta"], "unknowns": ["gamma"],
                      "experiments": ["delta"], "next_steps": ["epsilon"]}
    partial = parse_findings("facts\nonly facts\nnext_steps\n- later\n")
    assert partial["facts"] == ["only facts"] and partial["next_steps"] == ["later"]
    assert partial["inferences"] == [] and partial["unknowns"] == [] and partial["experiments"] == []
    assert parse_findings("no protocol headings") == {key: [] for key in FINDING_KEYS}
    assert parse_findings(None) == {key: [] for key in FINDING_KEYS}
    varied = parse_findings("## Facts\n- Alpha\n**Next steps:**\n- later\nUnknowns:\nmaybe\nNEXT-STEPS\n- second\n")
    assert varied["facts"] == ["Alpha"] and varied["unknowns"] == ["maybe"]
    assert varied["next_steps"] == ["later", "second"]
    assert varied["inferences"] == [] and varied["experiments"] == []


def test_models_match_packaged_catalog():
    catalog = json.loads(Path("runner/agentic_runner/codex-models.json").read_text())
    listed = [item["slug"] for item in catalog["models"] if item.get("visibility") == "list"]
    hidden = {item["slug"] for item in catalog["models"] if item.get("visibility") != "list"}
    assert list(MODELS) == listed and hidden and not hidden & set(MODELS)
    assert MODELS[0] == "gpt-6-astra"
    assert "gpt-6.1-sol" not in MODELS


def test_slow_setup_does_not_consume_execution_budget(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 250
    spec.status_mono = 700
    spec.make_workspace = True
    spec.findings = FINDINGS
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        assert service.submit(payload)[1]["state"] == "queued"
        wait_thread(service, payload["id"])
        job = service.handle("GET", "/coding/jobs/" + payload["id"])[1]
        assert job["state"] == "succeeded" and job["error_code"] is None
        assert job["findings"]["facts"] == ["alpha"] and job["findings"]["inferences"] == ["beta"]
        assert all(job["findings"][key] for key in FINDING_KEYS)
        raw = next(item for item in job["artifacts"] if item["name"] == "findings.txt")
        body = service.handle("GET", "/coding/jobs/" + payload["id"] + "/artifacts/" + raw["id"])[1]
        assert "alpha" in base64.b64decode(body["content_base64"]).decode()
        assert EXECUTION_BUDGET_SECONDS == 840
        assert spec.launches[0]["wall"] == EXECUTION_BUDGET_SECONDS + 60 == 900
        assert spec.launch_mono + spec.status_mono > EXECUTION_BUDGET_SECONDS
    finally:
        service.close()


@pytest.mark.parametrize("kind", ["setup", "execution", "verification", "authorization"])
def test_each_budget_records_its_reason(tmp_path, monkeypatch, kind):
    clock, spec = Clock(), Spec()
    patch_wall = kind == "authorization"
    if kind == "setup":
        spec.launch_mono = SETUP_BUDGET_SECONDS + 5
    elif kind == "execution":
        spec.launch_mono = 1
        spec.status = {"state": "running"}
        spec.status_mono = EXECUTION_BUDGET_SECONDS + 5
    elif kind == "verification":
        spec.launch_mono = 1
        spec.make_workspace = True
        spec.verify_launch_mono = VERIFICATION_BUDGET_SECONDS + 5
    else:
        spec.launch_mono = 1
        spec.status = {"state": "running"}
        spec.status_wall = 50000
        spec.status_mono = 1
    service = service_for(tmp_path, monkeypatch, clock, spec, patch_wall=patch_wall)
    try:
        payload = request(expires=clock.wall + (100 if kind == "authorization" else 40000))
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        expected = {"setup": "setup_timeout", "execution": "execution_timeout",
                    "verification": "verification_timeout", "authorization": "authorization_expired"}[kind]
        assert job["error_code"] == expected and job["state"] == "failed"
        if kind == "setup":
            assert job["execution_started"] is False
        else:
            assert job["execution_started"] is True
    finally:
        service.close()


def test_wall_clock_jump_inside_the_grant_does_not_cancel(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.status_wall = 10000
    spec.status_mono = 1
    spec.make_workspace = True
    service = service_for(tmp_path, monkeypatch, clock, spec, patch_wall=True)
    try:
        payload = request(expires=clock.wall + 40000)
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "succeeded" and job["error_code"] is None
        assert clock.wall > payload["expires"] - 40000
    finally:
        service.close()


def test_cancel_keeps_error_code(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.status = {"state": "running"}
    spec.status_mono = EXECUTION_BUDGET_SECONDS + 5
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        assert service.items[payload["id"]]["error_code"] == "execution_timeout"
        again = service.cancel(payload["id"])
        assert again["error_code"] == "execution_timeout" and again["state"] == "failed"
    finally:
        service.close()


def test_owner_cancel_sets_reason_and_second_cancel_keeps_it(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.status = {"state": "running"}
    spec.hold = threading.Event()
    spec.release = threading.Event()
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        assert spec.hold.wait(3)

        def release_launch():
            assert spec.hold.wait(3)
            time.sleep(0.05)
            spec.release.set()

        threading.Thread(target=release_launch, daemon=True).start()
        result = service.cancel(payload["id"])
        wait_thread(service, payload["id"])
        assert result["error_code"] == "owner_cancelled" and result["state"] == "cancelled"
        assert service.cancel(payload["id"])["error_code"] == "owner_cancelled"
    finally:
        spec.release.set()
        service.close()


def test_close_collects_and_restart_keeps_interrupted_results(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.status = {"state": "running"}
    spec.make_workspace = True
    spec.findings = FINDINGS
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        # Upper bound only: a loaded machine can delay the job thread.
        for _ in range(600):
            if service.items[payload["id"]].get("execution_started"):
                break
            time.sleep(0.05)
        assert service.items[payload["id"]]["execution_started"] is True
        launches = len(spec.launches)
        service.close()
        job = service.items[payload["id"]]
        assert job["state"] == "interrupted" and job["error_code"] == "broker_restarted"
        assert job["findings"]["facts"] == ["alpha"]
        assert any(item["name"] == "findings.txt" for item in job["artifacts"])
        assert (service.root / "artifacts" / payload["id"]).is_dir()
    except Exception:
        service.close()
        raise
    restored = HomeService(tmp_path / "data", packaged_source=service.source, sandbox_factory=boundary(clock, spec),
                           relay_factory=Relay)
    try:
        again = restored.items[payload["id"]]
        assert again["state"] == "interrupted" and again["stop_confirmed"] is True
        assert again["findings"]["facts"] == ["alpha"]
        assert restored.threads == {}
        assert len(spec.launches) == launches
    finally:
        restored.close()


def test_prune_skips_uncollected_execution_workspace(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = SETUP_BUDGET_SECONDS + 5
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        workspace = service.root / "work" / payload["id"]
        workspace.mkdir(parents=True, exist_ok=True)
        marker = workspace / "keep-diff"
        marker.write_text("uncollected")
        job.update(state="interrupted", phase="interrupted", stop_confirmed=True, execution_started=True,
                   results_collected=False, error_code="broker_restarted", workspace_released=False)
        service.prune()
        assert marker.is_file() and not job.get("workspace_released") and not job.get("content_expired")
        job["results_collected"] = True
        service.prune()
        assert not workspace.exists()
    finally:
        service.close()


@pytest.mark.parametrize("state", ["queued", "launching"])
def test_restart_releases_a_job_that_never_reached_the_sandbox(tmp_path, monkeypatch, state):
    clock, spec = Clock(), Spec()
    spec.strict_cancel = True
    service = service_for(tmp_path, monkeypatch, clock, spec)
    service.close()
    payload = request()
    job = {key: payload[key] for key in ("id", "case_id", "worker_id", "mode", "provider", "lease_epoch",
                                         "work_contract_major")}
    # A crash before the persisted sandbox attempt: no container or workspace can exist.
    job.update(state=state, phase=state, execution_started=False, stop_confirmed=False, results_collected=False,
               runtime_location="haos_app", base_commit=None, findings={key: [] for key in FINDING_KEYS},
               evidence=[], artifacts=[], error_code=None, request=payload, digest="0" * 64,
               created=time.time(), retained_until=time.time() + 86400)
    service.state_path.write_text(json.dumps({payload["id"]: job}))
    restored = HomeService(tmp_path / "data", packaged_source=service.source, sandbox_factory=boundary(clock, spec),
                           relay_factory=Relay)
    try:
        again = restored.items[payload["id"]]
        assert again["state"] == "interrupted" and again["stop_confirmed"] is True
        assert again["error_code"] == "broker_restarted" and not restored.active()
        spec.make_workspace = True
        fresh = request(id="c" * 32)
        assert restored.submit(fresh)[1]["state"] == "queued"
        wait_thread(restored, fresh["id"])
        # Only the new job launched; the interrupted one was never replayed.
        assert restored.items[fresh["id"]]["state"] == "succeeded"
        assert spec.launches[0]["id"] == fresh["id"] and payload["id"] not in {item["id"] for item in spec.launches}
    finally:
        restored.close()


@pytest.mark.parametrize("bound", ["collect_failures", "retention"])
def test_uncollected_workspace_is_released_once_its_bound_is_reached(tmp_path, monkeypatch, bound):
    clock, spec = Clock(), Spec()
    spec.launch_mono = SETUP_BUDGET_SECONDS + 5
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        workspace = service.root / "work" / payload["id"]
        workspace.mkdir(parents=True, exist_ok=True)
        job.update(state="interrupted", phase="interrupted", stop_confirmed=True, execution_started=True,
                   results_collected=False, collect_failures=0, error_code="broker_restarted",
                   workspace_released=False)

        def broken(job, sandbox):
            raise OSError("workspace unreadable")
        monkeypatch.setattr(service, "collect", broken)
        if bound == "retention":
            job["retained_until"] = time.time() - 1
            service.prune()
            assert not workspace.exists() and job["content_expired"] is True
            return
        for attempt in range(1, 3):
            service.prune()
            assert workspace.exists() and job["collect_failures"] == attempt
        service.prune()
        # Three failed exports end the wait; a 512 MiB workspace never lingers forever.
        assert job["collect_failures"] == 3 and not workspace.exists()
        assert job["error_code"] == "broker_restarted" and "collect_failures" not in service.view(payload["id"])
    finally:
        service.close()


def test_status_returns_while_launch_holds_its_lock(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.hold = threading.Event()
    spec.release = threading.Event()
    spec.make_workspace = True
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        assert spec.hold.wait(3)
        started = time.perf_counter()
        status, job = service.handle("GET", "/coding/jobs/" + payload["id"])
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5 and status == 200
        assert job["state"] == "launching" and job["phase"] == "launching"
        assert isinstance(job["heartbeat_at"], float)
        spec.release.set()
        wait_thread(service, payload["id"])
        assert service.items[payload["id"]]["state"] == "succeeded"
    finally:
        spec.release.set()
        service.close()


def test_sigterm_schedules_shutdown_off_the_signal_thread():
    coding = SimpleNamespace(closing=False, closed=threading.Event())
    seen = []

    class FakeServer:
        def shutdown(self):
            seen.append(threading.get_ident())

    previous = install_broker_stops(FakeServer(), coding)
    try:
        assert signal.getsignal(signal.SIGTERM) not in {signal.SIG_DFL, signal.SIG_IGN}
        assert signal.getsignal(signal.SIGINT) not in {signal.SIG_DFL, signal.SIG_IGN}
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(3000):
            if seen and coding.closing and coding.closed.is_set():
                break
            time.sleep(0.01)
        assert coding.closing and coding.closed.is_set()
        assert seen and seen[0] != threading.get_ident()
    finally:
        for name, old in previous.items():
            signal.signal(name, old)


@pytest.mark.parametrize("mode", ["relay", "launch"])
def test_setup_failure_ends_failed_and_releases_the_lease(tmp_path, monkeypatch, mode):
    clock, spec = Clock(), Spec()
    closed = []
    if mode == "relay":
        spec.fail_relay = True
    else:
        spec.launch_error = "stopped"
        spec.launch_mono = 1
    monkeypatch.setattr(Relay, "close", lambda self: closed.append(self.path))
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "failed" and job["phase"] == "failed"
        assert job["error_code"] == "coding_setup_failed" and job["stop_confirmed"] is True
        assert service.active() is False and closed
        if mode == "launch":
            assert job.get("sandbox_attempted") is True and job.get("workspace_released") is True
        else:
            assert not job.get("sandbox_attempted")
        second = request(id="d" * 32)
        status, body = service.submit(second)
        assert status == 202 and body["state"] == "queued"
        assert body.get("error_code") != "coding_writer_or_auth_fence_active"
    finally:
        service.close()


def test_oversized_patch_fails_and_keeps_findings(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.make_workspace = True
    spec.big_patch = True
    spec.findings = FINDINGS
    spec.launch_mono = 1
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "failed" and job["error_code"] == "patch_too_large"
        assert job["findings"]["facts"] == ["alpha"] and job["findings"]["next_steps"] == ["epsilon"]
        assert any(item["name"] == "findings.txt" for item in job["artifacts"])
        assert not any(item["name"] == "changes.patch" for item in job["artifacts"])
        workspace = service.root / "work" / payload["id"] / "workspace"
        assert not (workspace / "artifacts" / "changes.patch").exists()
        assert not (service.root / "inference-verified.json").exists()
        assert not any(str(item["id"]).endswith("-verify") for item in spec.launches)
        assert "verification" not in job
    finally:
        service.close()


def test_verifier_stores_withheld_artifact_without_content(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.make_workspace = True
    spec.findings = FINDINGS
    spec.withheld_verify = True
    spec.launch_mono = 1
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "succeeded" and job["error_code"] is None
        withheld = next(item for item in job["artifacts"] if item["name"] == "secret.py")
        assert withheld["kind"] == "withheld"
        stored = json.loads((service.root / "artifacts" / payload["id"] / (withheld["id"] + ".json")).read_text())
        assert stored["kind"] == "withheld" and "content_base64" not in stored
        report = next(item for item in job["artifacts"] if item["name"] == "report.txt")
        body = json.loads((service.root / "artifacts" / payload["id"] / (report["id"] + ".json")).read_text())
        assert "passed" in base64.b64decode(body["content_base64"]).decode()
    finally:
        service.close()


@pytest.mark.parametrize("flag,code", [
    ("quota_reached", "coding_quota_reached"),
    ("turns_exhausted", "coding_turn_limit"),
    ("provider_rejected", "coding_provider_rejected"),
    ("provider_unavailable", "coding_provider_unavailable"),
])
def test_provider_flag_on_a_failed_job_records_its_code(tmp_path, monkeypatch, flag, code):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.status = {"state": "failed", "exit_code": 1, "stop_confirmed": True}
    setattr(spec, flag, True)
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "failed" and job["error_code"] == code
    finally:
        service.close()


@pytest.mark.parametrize("flags,code", [
    (("quota_reached", "turns_exhausted", "provider_rejected", "provider_unavailable"), "coding_quota_reached"),
    (("turns_exhausted", "provider_rejected", "provider_unavailable"), "coding_turn_limit"),
    (("provider_rejected", "provider_unavailable"), "coding_provider_rejected"),
])
def test_provider_flags_keep_the_stated_order(tmp_path, monkeypatch, flags, code):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.status = {"state": "failed", "exit_code": 1, "stop_confirmed": True}
    for flag in flags:
        setattr(spec, flag, True)
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        assert service.items[payload["id"]]["error_code"] == code
        assert service.items[payload["id"]]["state"] == "failed"
    finally:
        service.close()


def test_succeeded_job_records_no_provider_code(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.make_workspace = True
    spec.quota_reached = True
    spec.turns_exhausted = True
    spec.provider_rejected = True
    spec.provider_unavailable = True
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "succeeded" and job["error_code"] is None
    finally:
        service.close()


def test_auth_failed_keeps_reauth_ahead_of_provider_flags(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.status = {"state": "failed", "exit_code": 1, "stop_confirmed": True}
    spec.auth_failed = True
    spec.quota_reached = True
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "failed" and job["error_code"] == "coding_needs_reauth"
        assert (service.root / "auth-failed.json").is_file()
    finally:
        service.close()


@pytest.mark.parametrize("body,expected", [
    ('{"tests_executed": 0}', 0),
    ('{"tests_executed": 3}', 3),
    ('{"tests_executed": 1000000}', 1_000_000),
    ('{"tests_executed": "12"}', None),
    ('{"tests_executed": true}', None),
    ('{"tests_executed": false}', None),
    ('{"tests_executed": -1}', None),
    ('{"tests_executed": 1000001}', None),
    ('{"state": "passed"}', None),
    ('not-json', None),
    ('[1]', None),
])
def test_verifier_test_count_is_display_only(tmp_path, monkeypatch, body, expected):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.make_workspace = True
    spec.verification_body = body
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "succeeded" and job["error_code"] is None
        assert job["verification"]["state"] == "passed"
        if expected is None:
            assert "tests_executed" not in job["verification"]
        else:
            assert job["verification"]["tests_executed"] == expected
    finally:
        service.close()


def test_missing_verifier_receipt_does_not_invent_a_test_count(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.make_workspace = True
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "succeeded" and "tests_executed" not in job["verification"]
    finally:
        service.close()


def test_non_utf8_patch_fails_and_keeps_findings(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.make_workspace = True
    spec.bad_patch = True
    spec.findings = FINDINGS
    spec.launch_mono = 1
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        service.submit(payload)
        wait_thread(service, payload["id"])
        job = service.items[payload["id"]]
        assert job["state"] == "failed" and job["error_code"] == "patch_not_text"
        assert job["findings"]["facts"] == ["alpha"] and job["findings"]["next_steps"] == ["epsilon"]
        assert any(item["name"] == "findings.txt" for item in job["artifacts"])
        assert not any(item["name"] == "changes.patch" for item in job["artifacts"])
        workspace = service.root / "work" / payload["id"] / "workspace"
        assert not (workspace / "artifacts" / "changes.patch").exists()
        assert not any(str(item["id"]).endswith("-verify") for item in spec.launches)
        assert "verification" not in job
    finally:
        service.close()


def _stopped_job(service, payload):
    service.submit(payload)
    wait_thread(service, payload["id"])
    return service.items[payload["id"]]


@pytest.mark.parametrize("code,outcome", [
    ("execution_timeout", "failed"),
    ("owner_cancelled", "cancelled"),
    ("broker_restarted", "interrupted"),
])
def test_prune_retries_an_unconfirmed_stop_before_retention(tmp_path, monkeypatch, code, outcome):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.make_workspace = True
    spec.cancel_result = {"state": "succeeded", "exit_code": 0}
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        job = _stopped_job(service, payload)
        assert job["retained_until"] > time.time()
        job.update(state="stop_unconfirmed", phase="stop_unconfirmed", stop_confirmed=False,
                   error_code=code, results_collected=True, sandbox_job_id=payload["id"])
        spec.cancel_calls.clear()
        service.prune()
        assert spec.cancel_calls == [payload["id"]]
        assert job["state"] == outcome and job["phase"] == outcome and job["stop_confirmed"] is True
        assert job["state"] != "succeeded" and job["error_code"] == code
        assert job.get("content_expired") is not True
    finally:
        service.close()


def test_verifier_retry_marks_verification_not_run(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.make_workspace = True
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        job = _stopped_job(service, payload)
        digest = "ab" * 32
        job.update(state="stop_unconfirmed", phase="stop_unconfirmed", stop_confirmed=False,
                   error_code="execution_timeout", results_collected=False, collect_failures=0,
                   sandbox_job_id=payload["id"] + "-verify",
                   verification={"state": "running", "patch_sha256": digest})
        launches = len(spec.launches)
        service.prune()
        assert job["state"] == "failed" and job["state"] != "succeeded"
        assert job["verification"]["state"] == "not_run"
        assert job["verification"]["patch_sha256"] == digest
        assert job.get("results_collected") is False
        assert job.get("collect_failures", 0) == 0
        assert len(spec.launches) == launches
    finally:
        service.close()


def test_unconfirmed_retry_changes_nothing_and_running_is_untouched(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    spec.cancel_result = {"state": "stop_unconfirmed"}
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        job = _stopped_job(service, payload)
        job.update(state="stop_unconfirmed", phase="stop_unconfirmed", stop_confirmed=False,
                   error_code="execution_timeout", results_collected=True)
        spec.cancel_calls.clear()
        service.prune()
        assert spec.cancel_calls == [payload["id"]]
        assert job["state"] == "stop_unconfirmed" and job["stop_confirmed"] is False
        assert job["error_code"] == "execution_timeout"
        job.update(state="running", phase="running", stop_confirmed=False)
        spec.cancel_calls.clear()
        service.prune()
        assert spec.cancel_calls == []
        assert job["state"] == "running" and job["stop_confirmed"] is False
    finally:
        service.close()


def test_cancel_of_an_unconfirmed_stop_uses_the_retry(tmp_path, monkeypatch):
    clock, spec = Clock(), Spec()
    spec.launch_mono = 1
    service = service_for(tmp_path, monkeypatch, clock, spec)
    try:
        payload = request()
        job = _stopped_job(service, payload)
        job.update(state="stop_unconfirmed", phase="stop_unconfirmed", stop_confirmed=False,
                   error_code=None, results_collected=True)
        spec.cancel_calls.clear()
        viewed = service.cancel(payload["id"])
        assert spec.cancel_calls == [payload["id"]]
        assert viewed["state"] == "failed" and viewed["error_code"] == "writer_stop_unconfirmed"
        assert viewed["stop_confirmed"] is True and "verification" not in service.items[payload["id"]]
    finally:
        service.close()
