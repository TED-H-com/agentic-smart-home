"""Containment unit cases and real Docker negatives, not E1 cloud/HAOS acceptance."""
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentic_controller import VERSION

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
from agentic_host.coding_sandbox import (  # noqa: E402
    CodingSandbox,
    Limits,
    SandboxError,
    container_command,
    read_artifacts,
    validate_docker_security,
)

IMAGE = "sha256:" + "1" * 64
JOB = "test-offline-0001"


def test_profiles_are_not_advertised_as_cloud_or_inner_coding(tmp_path):
    sandbox = CodingSandbox(tmp_path, IMAGE, {})
    caps = sandbox.capabilities()
    assert caps["work_contract_major"] == 1
    assert caps["host-coder"]["available"] is False and caps["home-coder"]["available"] is False
    assert not caps["host-coder"]["modes"] and not caps["home-coder"]["modes"]
    assert "HAOS App" in caps["home-coder"]["blocked_reasons"][0]
    with pytest.raises(SandboxError, match="digest"):
        CodingSandbox(tmp_path, "untrusted:latest", {})
    with pytest.raises(SandboxError, match="limit"):
        Limits(disk_mib=0)
    with pytest.raises(SandboxError, match="limit"):
        Limits(wall_seconds=True)


def test_launch_refuses_unsupported_broker_before_docker_or_storage(tmp_path, monkeypatch):
    calls = []
    sandbox = CodingSandbox(tmp_path / "state", IMAGE, {"product": tmp_path}, execute=lambda *a, **k: calls.append(a))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(SandboxError, match="root broker"):
        sandbox.launch(JOB, 1, "product", "a" * 40, "print('hi')")
    assert not calls and not (tmp_path / "state").exists()
    with pytest.raises(SandboxError, match="Repository"):
        sandbox.launch(JOB, 1, "https://untrusted", "a" * 40, "print('hi')")
    with pytest.raises(SandboxError, match="job or lease"):
        sandbox.launch("../../target", 1, "product", "a" * 40, "print('hi')")


def test_container_profile_has_no_network_or_secret_mounts(tmp_path):
    args = container_command("agentic-coding-" + JOB, IMAGE, tmp_path, Limits())
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--memory") + 1] == args[args.index("--memory-swap") + 1] == "256m"
    assert args[args.index("--ipc") + 1] == "none"
    assert "--read-only" in args and "no-new-privileges" in args
    assert not any("/var/run" in arg or "SUPERVISOR_TOKEN" in arg or "--privileged" in arg for arg in args)
    assert "--reuid=10000" in args[-1] and "--bounding-set=-all" in args[-1]
    assert "--signal=KILL 300s" in args[-1]
    with pytest.raises(SandboxError):
        container_command("agentic-coding-" + JOB, IMAGE, Path("/unsafe,readonly=false"), Limits())


def test_artifacts_reject_traversal_links_specials_secrets_and_quota(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    path = artifacts / "reproducer.py"
    path.write_text("print('reproduced')\n")
    item = read_artifacts(artifacts, Limits())[0]
    assert item["name"] == "reproducer.py" and item["untrusted"] and len(item["sha256"]) == 64
    exported = read_artifacts(artifacts, Limits(artifact_bytes=1))
    assert [item["name"] for item in exported] == ["skipped.json"]
    assert json.loads(exported[0]["content"])["skipped"] == [{"name": "reproducer.py", "reason": "size_limit"}]
    path.unlink()
    (artifacts / "leak.txt").symlink_to(tmp_path / "owner-secret")
    with pytest.raises(SandboxError, match="safely opened"):
        read_artifacts(artifacts, Limits())
    (artifacts / "leak.txt").unlink()
    os.mkfifo(artifacts / "pipe.txt")
    with pytest.raises(SandboxError, match="regular file"):
        read_artifacts(artifacts, Limits())
    (artifacts / "pipe.txt").unlink()
    (artifacts / "typed.py").write_text("api_key: SecretStr\napi_key: str | None = None\npassword=body.password\naccess_token: str\n")
    (artifacts / "plain.txt").write_text("access_token = not-a-real-account-token\n")
    kept = {item["name"]: item for item in read_artifacts(artifacts, Limits())}
    assert "content" in kept["typed.py"] and "content" in kept["plain.txt"]
    (artifacts / "typed.py").unlink()
    (artifacts / "plain.txt").unlink()
    (artifacts / "active.html").write_text("<script>fetch('/approval')</script>")
    skipped = {item["name"]: item for item in read_artifacts(artifacts, Limits())}
    assert "active.html" not in skipped
    assert json.loads(skipped["skipped.json"]["content"])["skipped"] == [
        {"name": "active.html", "reason": "unsupported_type"}]


def test_secret_forms_and_controller_name_and_size_rules(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "settings.json").write_text('{"password": "hunter2hunter2"}\n')
    (artifacts / "calls.py").write_text("password = secrets.token_urlsafe(32)\n"
                                        "api_key = build_key_from_parts_v2(seed)\n")
    (artifacts / "fixture.patch").write_text(
        "--- a/tests/test_login.py\n+++ b/tests/test_login.py\n@@ -1 +1,2 @@\n"
        ' password = "context-line-value-abc"\n+password="not-a-real-secret"\n')
    (artifacts / "token.patch").write_text("--- a/.env\n+++ b/.env\n@@ -0,0 +1 @@\n+API_KEY=AKIAIOSFODNN7EXAMPLE12\n")
    (artifacts / ("n" * 157 + ".txt")).write_text("long name\n")
    (artifacts / "back\\slash.txt").write_text("odd name\n")
    (artifacts / "skipped.json").write_text("{}\n")
    (artifacts / "big.log").write_text("x" * (180 * 1024 + 1))
    exported = {item["name"]: item for item in read_artifacts(artifacts, Limits())}
    assert exported["settings.json"]["kind"] == "withheld" and "content" not in exported["settings.json"]
    assert exported["token.patch"]["kind"] == "withheld" and "content" not in exported["token.patch"]
    assert "content" in exported["calls.py"] and "content" in exported["fixture.patch"]
    assert json.loads(exported["skipped.json"]["content"]) == {"skipped": [
        {"name": "back\\slash.txt", "reason": "name_not_allowed"},
        {"name": "big.log", "reason": "size_limit"},
        {"name": "n" * 157 + ".t…", "reason": "name_not_allowed"},
        {"name": "skipped.json", "reason": "name_not_allowed"}]}
    many = tmp_path / "many"
    many.mkdir()
    for index in range(105):
        (many / f"blob-{index:03}.bin").write_text("x")
    listing = json.loads(read_artifacts(many, Limits())[-1]["content"])
    assert len(listing["skipped"]) == 100 and listing["omitted"] == 5


def test_unconfirmed_stop_never_releases_writer_or_exports(tmp_path):
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        if args[:2] in {("docker", "inspect"), ("docker", "version")}:
            raise SandboxError("daemon unavailable")
        return SimpleNamespace(returncode=0, stdout="")
    sandbox = CodingSandbox(tmp_path, IMAGE, {}, execute=execute)
    sandbox.save({"job_id": JOB, "container": "agentic-coding-" + JOB, "state": "running"})
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    with pytest.raises(SandboxError, match="release"):
        sandbox.dispose(JOB)
    with pytest.raises(SandboxError, match="unavailable"):
        sandbox.artifacts(JOB)
    assert not any(args[:2] == ("docker", "rm") or args[0] == "umount" for args in calls)
    assert sandbox.recover()[0]["state"] == "stop_unconfirmed"


def test_restart_kills_owned_process_before_releasing_writer(tmp_path):
    calls, running = [], True
    def execute(*args, **kwargs):
        nonlocal running
        calls.append(args)
        if args[:2] == ("docker", "kill"):
            running = False
        data = [{"Id": "container-123", "Config": {"Labels": {"agentic.coding": "agentic-coding-" + JOB}},
                 "State": {"Running": running, "Paused": False, "Status": "running" if running else "exited",
                           "ExitCode": 137}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(data) if args[1] == "inspect" else "")
    sandbox = CodingSandbox(tmp_path, IMAGE, {}, execute=execute)
    sandbox.save({"job_id": JOB, "container": "agentic-coding-" + JOB, "container_id": "container-123",
                  "state": "running"})
    assert sandbox.recover()[0]["state"] == "interrupted"
    assert calls[0][1] == "inspect" and calls[1][1] == "kill" and calls[2][1] == "wait"
    assert sum(args[1] == "inspect" for args in calls) == 2
    assert not any("start" in args for args in calls)


def test_replaced_container_cannot_be_killed_or_exported(tmp_path):
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{
            "Id": "someone-else", "Config": {"Labels": {"agentic.coding": "agentic-coding-" + JOB}}}]))
    sandbox = CodingSandbox(tmp_path, IMAGE, {}, execute=execute)
    sandbox.save({"job_id": JOB, "container": "agentic-coding-" + JOB, "container_id": "ours", "state": "running"})
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    assert not any(args[1] == "kill" for args in calls)


def boundary_observation(name, workspace, *, run=subprocess.run):
    """Bounded, read-only diagnostics before fixture teardown; never raw errors."""
    result = {}
    started = time.monotonic()
    try:
        inspect = run(["docker", "inspect", "--format", "{{json .State}}", name],
                      capture_output=True, text=True, timeout=5)
        result["inspect_returncode"] = inspect.returncode
        if inspect.returncode == 0 and len(inspect.stdout) <= 16384:
            raw = json.loads(inspect.stdout)
            state = {key: raw[key] for key in ("Running", "Paused", "Restarting", "OOMKilled", "Dead")
                     if type(raw.get(key)) is bool}
            state.update({key: raw[key] for key in ("Pid", "ExitCode") if type(raw.get(key)) is int})
            if raw.get("Status") in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
                state["Status"] = raw["Status"]
            for key in ("StartedAt", "FinishedAt"):
                if isinstance(raw.get(key), str) and len(raw[key]) <= 40:
                    datetime.fromisoformat(raw[key].replace("Z", "+00:00"))
                    state[key] = raw[key]
            if state.get("StartedAt") and state.get("FinishedAt") and not state["FinishedAt"].startswith("0001"):
                state["runtime_seconds"] = round((datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00")) -
                    datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))).total_seconds(), 3)
            result["state"] = state
            message = str(raw.get("Error", "")).lower()
            result["state_error_signatures"] = {key: text in message for key, text in {
                "permission": "permission denied", "no_space": "no space left",
                "resource_unavailable": "resource temporarily unavailable", "timeout": "timeout"}.items()}
    except Exception as error:
        result["inspect_error_type"] = type(error).__name__
    result["inspect_seconds"] = round(time.monotonic() - started, 3)
    try:
        path = workspace / "stderr.txt"
        with path.open("rb") as stream:
            stderr = stream.read(8192).decode(errors="replace").lower()
        result["fixture_stderr_bytes"] = path.stat().st_size
        result["fixture_stderr_signatures"] = {key: text in stderr for key, text in {
            "assertion": "assertionerror", "permission": "permission denied",
            "resource_unavailable": "resource temporarily unavailable", "timeout": "timeout"}.items()}
    except OSError:
        result["fixture_stderr_unavailable"] = True
    try:
        with (workspace / "artifacts/result.json").open() as stream:
            proof = json.loads(stream.read(1024))
        result["negative_checks_completed"] = proof.get("checks") == "passed"
        result["children_started"] = proof.get("children") if type(proof.get("children")) is int else None
    except (OSError, ValueError, AttributeError):
        result["negative_checks_completed"] = False
    return result


@pytest.mark.parametrize("stop_method", ["deadline", "cancel"])
def test_real_docker_offline_negative_boundaries_and_process_tree(tmp_path, request, stop_method):
    if not request.config.getoption("--containers"):
        pytest.skip("requires local packaged Runner image and Docker; foundation only")
    # This smoke deliberately does not claim the root-only bounded filesystem was exercised.
    # It uses disk-backed pytest scratch and verifies the remaining real Docker boundaries.
    image = subprocess.check_output(["docker", "image", "inspect", f"agentic-home-runner:{VERSION}",
                                     "--format", "{{.Id}}"], text=True).strip()
    workspace = tmp_path / "work"
    workspace.mkdir(mode=0o755)
    workspace.chmod(0o777)  # Production mount root is root-owned; this harness runs as uid 1000.
    for directory in ("source", "profile", "scratch", "artifacts"):
        (workspace / directory).mkdir(mode=0o777)
        (workspace / directory).chmod(0o777)  # Test harness only; production root broker chowns UID 10000.
    program = '''import json, os, pathlib, socket, subprocess, time
assert os.geteuid() == 10000
assert "CapEff:\\t0000000000000000" in pathlib.Path('/proc/self/status').read_text()
assert set(pathlib.Path('/sys/class/net').iterdir()) == {pathlib.Path('/sys/class/net/lo')}
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/data').exists()
assert not any(k in os.environ for k in ('SUPERVISOR_TOKEN','XAI_API_KEY','OPENAI_API_KEY','AGENTIC_TOKEN'))
for address in ('192.168.177.2', '169.254.169.254', '1.1.1.1'):
    with socket.socket() as connection:
        connection.settimeout(.2)
        assert connection.connect_ex((address, 443)) != 0
try:
    pathlib.Path('/app/forbidden').write_text('no')
except OSError:
    pass
else:
    raise AssertionError('read-only root bypass')
try:
    os.kill(1, 19)
except PermissionError:
    pass
else:
    raise AssertionError('job can stop watchdog')
assert pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip() == '268435456'
assert pathlib.Path('/sys/fs/cgroup/memory.swap.max').read_text().strip() == '0'
assert pathlib.Path('/sys/fs/cgroup/pids.max').read_text().strip() == '64'
assert pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip() == '100000 100000'
children = []
try:
    for _ in range(80):
        children.append(subprocess.Popen(['/usr/bin/sleep', '60']))
except BlockingIOError:
    pass
else:
    raise AssertionError('PID quota bypass')
assert 1 < len(children) < 64
pathlib.Path('/work/artifacts/result.json').write_text(json.dumps({'checks':'passed','children':len(children)}))
time.sleep(60)
'''
    (workspace / "task.py").write_text(program)
    name = "agentic-coding-" + uuid.uuid4().hex
    args = container_command(name, image, workspace, Limits(wall_seconds=10))
    evidence = {"kind": "real_docker_offline_boundary", "method": stop_method, "image": image,
                "wall_seconds": 10, "control_wait_timeout_seconds": 20, "status": "running", "timings": []}
    report = Path(__file__).parents[1] / f"evidence/coding-boundary-{stop_method}.json"
    def observed(args):
        evidence["stage"] = args[1]
        started = time.monotonic()
        try:
            return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)
        finally:
            evidence["timings"].append({"operation": args[1], "seconds": round(time.monotonic() - started, 3)})
    try:
        observed(args)
        observed(["docker", "start", name])
        if stop_method == "cancel":
            for _ in range(80):
                if (workspace / "artifacts/result.json").exists():
                    break
                time.sleep(.05)
            from agentic_host.coding_sandbox import command
            timings = []
            def observed_command(*args, **kwargs):
                started = time.monotonic()
                try:
                    return command(*args, **kwargs)
                finally:
                    timings.append({"operation": " ".join(args[:2]), "seconds": round(time.monotonic() - started, 2)})
            sandbox = CodingSandbox(tmp_path, image, {}, execute=observed_command)
            sandbox.save({"job_id": name.removeprefix("agentic-coding-"), "container": name, "state": "running"})
            cancelled = sandbox.cancel(name.removeprefix("agentic-coding-"))
            assert cancelled["state"] == "cancelled", {"job": cancelled, "commands": timings}
        code = observed(["docker", "wait", name]).stdout.strip()
        state = json.loads(observed(["docker", "inspect", name]).stdout)[0]["State"]
        result = workspace / "artifacts/result.json"
        errors = (workspace / "stderr.txt").read_text() if (workspace / "stderr.txt").exists() else ""
        assert result.exists(), {"stderr": errors, "state": state, "files": [str(p.relative_to(workspace)) for p in workspace.rglob("*")]}
        assert json.loads(result.read_text())["checks"] == "passed"
        assert code == "137" and state["Running"] is False and state["Pid"] == 0
        evidence["status"] = "passed"
    except BaseException as error:
        evidence.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        try:
            evidence["before_cleanup"] = boundary_observation(name, workspace)
            report.write_text(json.dumps(evidence, indent=2) + "\n")
        except Exception:
            pass  # Diagnostics must never suppress the original failure or cleanup.
        try:
            removed = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)
            evidence["cleanup"] = removed.returncode == 0
        except Exception as error:
            evidence.update(cleanup=False, cleanup_error_type=type(error).__name__)
        finally:
            try:
                report.write_text(json.dumps(evidence, indent=2) + "\n")
            except OSError:
                pass
        if evidence["status"] == "passed" and not evidence.get("cleanup"):
            raise AssertionError("fixture_cleanup_unconfirmed")


def test_artifact_directory_budget(tmp_path):
    for index in range(513):
        (tmp_path / str(index)).mkdir()
    with pytest.raises(SandboxError, match="directory limit"):
        read_artifacts(tmp_path, Limits())


def test_idempotency_and_active_writer_fencing(tmp_path, monkeypatch):
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path})
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    # A lost Docker response remains fenced; an exact retry never runs prepare again.
    import hashlib
    program = "print('hi')"
    payload_hash = hashlib.sha256(json.dumps([JOB, 1, "product", "a" * 40, program],
                                           separators=(",", ":")).encode()).hexdigest()
    sandbox.save({"job_id": JOB, "state": "stop_unconfirmed", "request_digest": payload_hash})
    assert sandbox.launch(JOB, 1, "product", "a" * 40, program)["state"] == "stop_unconfirmed"
    with pytest.raises(SandboxError, match="different payload"):
        sandbox.launch(JOB, 2, "product", "a" * 40, program)
    with pytest.raises(SandboxError, match="fenced"):
        sandbox.launch("test-second-0002", 1, "product", "a" * 40, program)


def test_real_docker_memory_limit_stops_excess_allocation(tmp_path, request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires local packaged Runner image and Docker; foundation only")
    image = subprocess.check_output(["docker", "image", "inspect", f"agentic-home-runner:{VERSION}",
                                     "--format", "{{.Id}}"], text=True).strip()
    for directory in (tmp_path, *(tmp_path / name for name in ("source", "profile", "scratch", "artifacts"))):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o777)
    (tmp_path / "task.py").write_text('''import pathlib, subprocess
child = subprocess.run(['/usr/local/bin/python', '-c', 'a = bytearray(400 * 1024 * 1024)'])
assert child.returncode == -9, child.returncode
events = dict(line.split() for line in pathlib.Path('/sys/fs/cgroup/memory.events').read_text().splitlines())
assert int(events['oom_kill']) >= 1
pathlib.Path('/work/artifacts/memory.txt').write_text('kernel killed allocation over memory budget')
''')
    name = "agentic-coding-" + uuid.uuid4().hex
    try:
        subprocess.run(container_command(name, image, tmp_path, Limits(wall_seconds=10)),
                       check=True, capture_output=True, timeout=120)
        subprocess.run(["docker", "start", name], check=True, capture_output=True, timeout=120)
        code = subprocess.check_output(["docker", "wait", name], text=True, timeout=120).strip()
        assert code == "0", (tmp_path / "stderr.txt").read_text()
        assert (tmp_path / "artifacts/memory.txt").read_text().startswith("kernel killed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)


def test_retained_workspace_budget_prevents_unbounded_disk_growth(tmp_path, monkeypatch):
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path})
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    monkeypatch.setattr(sandbox, "_confirm_writer", lambda job: "absent")
    for index in range(4):
        (tmp_path / f"retained-{index}").mkdir()
    with pytest.raises(SandboxError) as failure:
        sandbox.launch(JOB, 1, "product", "a" * 40, "print('hi')")
    assert failure.value.code == "coding_setup_failed" and failure.value.stopped is True
    assert "Retained workspace budget" in str(failure.value.__cause__)
    assert sandbox.record(JOB)["state"] == "failed"
    assert not (tmp_path / JOB).exists()


def test_disk_format_preserves_reserved_blocks_without_mounting(tmp_path):
    # A real ordinary disk file, never a loop device or host mount. Run only mkfs;
    # stop at the first mount request to test allocation independently of root.
    from agentic_host.coding_sandbox import command
    calls = []
    def prepare_command(*args, **kwargs):
        calls.append(args)
        if args[0] == "mount":
            raise SandboxError("Test deliberately refuses host mounting")
        return command(*args, **kwargs)
    sandbox = CodingSandbox(tmp_path, IMAGE, {}, limits=Limits(disk_mib=64), execute=prepare_command)
    with pytest.raises(SandboxError, match="deliberately refuses"):
        sandbox.prepare({"job_id": JOB}, "print('offline')")
    disk = tmp_path / JOB / "workspace.ext4"
    assert disk.stat().st_size == 64 * 1024 * 1024
    assert disk.stat().st_blocks * 512 >= disk.stat().st_size
    assert calls[-1][0] == "mount"
    assert not os.path.ismount(disk.parent / "workspace")
    header = command("dumpe2fs", "-h", str(disk)).stdout
    assert "Filesystem features:" in header and "extent" in header


def test_verifier_trusted_test_exercises_candidate_restore_helper(tmp_path, monkeypatch, request):
    """Real Git + actual restore test; only root disk/mount/chown plumbing is mocked."""
    import hashlib
    import shutil

    from agentic_host.coding_sandbox import command

    product = Path(__file__).parents[1]
    repo = tmp_path / "baseline"
    (repo / "tests").mkdir(parents=True)
    (repo / "scripts").mkdir()
    for name in ("test_api.py", "test_engine.py", "conftest.py"):
        shutil.copyfile(product / "tests" / name, repo / "tests" / name)
    helper = (product / "scripts/restore_controller.py").read_bytes()
    (repo / "scripts/restore_controller.py").write_bytes(helper)
    for args in (("init", "-q", str(repo)), ("-C", str(repo), "add", "."),
                 ("-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-qm", "Trusted fixture baseline")):
        command("git", *args)
    revision = command("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    root = tmp_path / "work"
    root.mkdir()
    trusted = root / JOB / "workspace/trusted-tests"
    request.addfinalizer(lambda: trusted.chmod(0o700) if trusted.is_dir() else None)
    def fixture_command(*args, **kwargs):
        if args[0] in {"mount", "mkfs.ext4"}:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return command(*args, **kwargs)
    monkeypatch.setattr(os, "posix_fallocate", lambda fd, start, count: os.ftruncate(fd, count))
    monkeypatch.setattr(os, "statvfs", lambda path: SimpleNamespace(f_blocks=64, f_frsize=1024**2))
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path).name == "workspace")
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    sandbox = CodingSandbox(root, IMAGE, {"product": repo}, limits=Limits(disk_mib=64),
                            inputs={"candidate.patch": b"fixture"}, execute=fixture_command)
    mask = os.umask(0o077)
    try:
        work = sandbox.prepare({"job_id": JOB, "repo_id": "product", "base_commit": revision}, "pass")
    finally:
        os.umask(mask)
    trusted = work / "trusted-tests"
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in trusted.iterdir()}
    assert before == {name: hashlib.sha256((repo / "tests" / name).read_bytes()).hexdigest() for name in before}
    assert trusted.stat().st_mode & 0o777 == 0o555
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in trusted.iterdir())
    assert (work / "scripts").is_symlink() and os.readlink(work / "scripts") == "source/scripts"
    assert (work / "scripts").resolve() == work / "source/scripts"
    assert work.stat().st_mode & 0o777 == 0o755
    test = str(trusted / "test_api.py") + "::test_encrypted_snapshot_can_restore"
    bootstrap = ("import sys;sys.path[:0]=sys.argv[1:4];import pytest;raise SystemExit(pytest.main(sys.argv[4:]))")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    def verify(label):
        return subprocess.run([sys.executable, "-B", "-I", "-c", bootstrap,
            str(product / "controller"), str(product / "runner"), str(product / "hermes"),
            "-q", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "-c", str(work / "verification.ini"),
            "--confcutdir", str(trusted), "--basetemp", str(work / "scratch" / label), test],
            env=env, capture_output=True, text=True, timeout=20)
    candidate = work / "source/scripts/restore_controller.py"
    candidate.write_text("raise RuntimeError('candidate-helper-was-executed')\n")
    failed = verify("broken")
    assert failed.returncode == 1 and "candidate-helper-was-executed" in failed.stdout
    candidate.write_bytes(helper)
    fixed = verify("repaired")
    assert fixed.returncode == 0 and "1 passed" in fixed.stdout, fixed.stdout[-2000:]
    assert before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in trusted.iterdir()}
    assert not list(work.rglob(".pytest_cache")) and not list(work.rglob("__pycache__"))


def test_installed_probe_is_guarded_and_embedded_programs_compile(monkeypatch):
    import importlib.util
    path = Path(__file__).with_name("installed_sandbox_probe.py")
    spec = importlib.util.spec_from_file_location("installed_sandbox_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    compile(module.QUOTA_PROGRAM, "quota-probe", "exec")
    compile(module.CANCEL_PROGRAM, "cancel-probe", "exec")
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    def forbidden(*args, **kwargs):
        raise AssertionError("A non-root probe must not run any system command")
    monkeypatch.setattr(module, "command", forbidden)
    report = module.probe()
    assert report["result"] == "failed" and report["error"] == "requires_root_in_disposable_guest"
    assert report["cleanup"] == "passed"
    assert module.job_diagnostics({"state": "stop_unconfirmed", "stop_failure": {
        "error_code": "command_timeout", "operation": "docker.wait", "timeout_seconds": 15,
        "stderr": "private", "command_exit_code": True}}) == {
        "state": "stop_unconfirmed", "stop_failure": {
            "error_code": "command_timeout", "operation": "docker.wait", "timeout_seconds": 15}}


def test_docker_info_uses_json_wire_names_and_still_requires_every_boundary():
    # Captured names from `docker info --format '{{json .}}'`: the CLI Go
    # field CPUCfsQuota is serialized as CpuCfsQuota, not its template name.
    data = {"MemoryLimit": True, "SwapLimit": True, "PidsLimit": True,
            "CpuCfsQuota": True, "CgroupVersion": "2",
            "SecurityOptions": ["name=apparmor,profile=default", "name=seccomp,profile=builtin", "name=cgroupns"]}
    validate_docker_security(data)
    for field in ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota", "CgroupVersion"):
        missing = {key: value for key, value in data.items() if key != field}
        with pytest.raises(SandboxError) as error:
            validate_docker_security(missing)
        assert error.value.code == "resource_limits_unavailable"
        assert error.value.diagnostics["missing_capabilities"] == [field]
    # Accepting the old Go template name would hide the exact regression.
    wrong_name = {key: value for key, value in data.items() if key != "CpuCfsQuota"}
    with pytest.raises(SandboxError) as error:
        validate_docker_security({**wrong_name, "CPUCfsQuota": True})
    assert error.value.diagnostics["missing_capabilities"] == ["CpuCfsQuota"]
    with pytest.raises(SandboxError):
        validate_docker_security({**data, "CpuCfsQuota": "true"})
    for feature in ("seccomp", "apparmor"):
        weakened = {**data, "SecurityOptions": [value for value in data["SecurityOptions"] if feature not in value]}
        with pytest.raises(SandboxError) as error:
            validate_docker_security(weakened)
        assert error.value.code == "docker_security_unavailable"
        assert error.value.diagnostics["missing_capabilities"] == [feature]


def test_real_docker_info_passes_resource_handshake(request):
    if not request.config.getoption("--containers"):
        pytest.skip("requires real Docker info; no container or mount is created")
    actual = json.loads(subprocess.check_output(["docker", "info", "--format", "{{json .}}"], text=True))
    validate_docker_security(actual)


def test_command_failure_has_safe_machine_readable_diagnostics(monkeypatch):
    from agentic_host.coding_sandbox import command
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="private output", stderr="private account details"))
    with pytest.raises(SandboxError) as error:
        command("git", "clone", "https://user:private-credential@example.invalid/repo", "/private/path")
    assert error.value.code == "command_failed"
    assert error.value.diagnostics == {"operation": "git.clone", "command_exit_code": 1}
    assert "private" not in str(error.value)
    untrusted = SandboxError("private error", code="private", operation="private", exit_code=True, missing=["private"])
    assert untrusted.code == "sandbox_rejected" and untrusted.diagnostics == {}


def test_cancel_timeout_records_safe_operation_and_keeps_writer_fenced(tmp_path, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "wait"):
            assert kwargs["timeout"] == 15
            raise subprocess.TimeoutExpired(args, 15, output="private", stderr="private")
        state = [{"Id": "container-123", "Config": {"Labels": {"agentic.coding": "agentic-coding-" + JOB}},
                  "State": {"Running": True, "Paused": False}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(state) if args[1] == "inspect" else "")
    monkeypatch.setattr(subprocess, "run", run)
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path})
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    sandbox.save({"job_id": JOB, "container": "agentic-coding-" + JOB, "container_id": "container-123",
                  "state": "running"})
    stopped = sandbox.cancel(JOB)
    assert stopped["state"] == "stop_unconfirmed"
    assert stopped["stop_failure"] == {
        "error_code": "command_timeout", "operation": "docker.wait", "timeout_seconds": 15}
    assert "private" not in json.dumps(stopped)
    with pytest.raises(SandboxError, match="release"):
        sandbox.dispose(JOB)
    with pytest.raises(SandboxError, match="fenced"):
        sandbox.launch("test-second-0002", 1, "product", "a" * 40, "print('hi')")
    with pytest.raises(SandboxError, match="Writer must stop"):
        sandbox.artifacts(JOB)
    assert not any(args[:2] == ("docker", "rm") or args[0] == "umount" for args in calls)



def test_boundary_diagnostics_are_allowlisted_and_bound_before_cleanup(tmp_path):
    secret = "fixture-private-value"
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/result.json").write_text(json.dumps({"checks": "passed", "children": 62, "token": secret}))
    (tmp_path / "stderr.txt").write_text("Permission denied " + secret)
    def inspect(args, **kwargs):
        assert args == ["docker", "inspect", "--format", "{{json .State}}", "fixture"]
        assert kwargs["timeout"] == 5 and kwargs["capture_output"] is True
        return SimpleNamespace(returncode=0, stdout=json.dumps({"Running": False, "Pid": 0, "ExitCode": 137,
            "Status": "exited", "Error": "no space left " + secret, "Private": secret,
            "StartedAt": "2026-10-12T00:00:00Z", "FinishedAt": "2026-10-12T00:00:10Z"}))
    value = boundary_observation("fixture", tmp_path, run=inspect)
    assert value["state"]["runtime_seconds"] == 10 and value["negative_checks_completed"]
    assert value["fixture_stderr_signatures"]["permission"] and value["state_error_signatures"]["no_space"]
    assert secret not in json.dumps(value) and "Private" not in json.dumps(value)


@pytest.mark.parametrize("failure", ["timeout", "malformed", "missing"])
def test_boundary_diagnostic_failure_returns_typed_evidence_without_raising(tmp_path, failure):
    def inspect(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 5, stderr=b"fixture-private-value")
        return SimpleNamespace(returncode=1 if failure == "missing" else 0, stdout="fixture-private-value")
    value = boundary_observation("fixture", tmp_path, run=inspect)
    assert "state" not in value
    assert value["negative_checks_completed"] is False
    assert "fixture-private-value" not in json.dumps(value)
    if failure != "missing":
        assert value["inspect_error_type"] == ("TimeoutExpired" if failure == "timeout" else "JSONDecodeError")


def _absence_execute(calls, *, ps="", version_error=False, inspect_running=False, exit_code=0):
    """Fake Docker. An empty label listing is absence only after version succeeds."""
    alive = {"running": inspect_running}

    def execute(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "version"):
            assert "{{.Server.Version}}" in args
            if version_error:
                raise OSError("daemon did not answer")
            return SimpleNamespace(returncode=0, stdout="27.0\n")
        if args[:2] == ("docker", "ps"):
            assert "{{.ID}}" in args and any(arg.startswith("label=agentic.coding=") for arg in args)
            return SimpleNamespace(returncode=0, stdout=ps)
        if args[:2] == ("docker", "inspect"):
            if not ps:
                return SimpleNamespace(returncode=1, stdout="")
            running = alive["running"]
            state = {"Running": running, "Paused": False, "OOMKilled": False, "ExitCode": exit_code,
                     "Status": "running" if running else "exited"}
            info = [{"Id": ps.strip(), "Config": {"Labels": {"agentic.coding": args[-1]}}, "State": state}]
            return SimpleNamespace(returncode=0, stdout=json.dumps(info))
        if args[:2] == ("docker", "kill"):
            alive["running"] = False
            return SimpleNamespace(returncode=0, stdout="")
        if args[:2] == ("docker", "wait"):
            assert kwargs.get("timeout") == 15
            return SimpleNamespace(returncode=0, stdout="0\n")
        if args[:2] in {("docker", "create"), ("docker", "start"), ("docker", "rm")} or args[0] == "umount":
            created = "created-id\n" if args[:2] == ("docker", "create") else ""
            return SimpleNamespace(returncode=0, stdout=created)
        return SimpleNamespace(returncode=0, stdout="")

    return execute


@pytest.mark.parametrize("point", ["prepare", "create", "start"])
def test_failed_start_with_proven_absence_releases_the_fence(tmp_path, monkeypatch, point):
    calls, attempts = [], {"prepare": 0, "create": 0, "start": 0}

    def execute(*args, **kwargs):
        if args[:2] == ("docker", "create"):
            attempts["create"] += 1
            if point == "create" and attempts["create"] == 1:
                raise SandboxError("create failed", code="command_failed")
        if args[:2] == ("docker", "start"):
            attempts["start"] += 1
            if point == "start" and attempts["start"] == 1:
                raise subprocess.SubprocessError("start failed")
        return _absence_execute(calls)(*args, **kwargs)

    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path}, execute=execute)
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path).name == "workspace")

    def prepare(job, program):
        attempts["prepare"] += 1
        if point == "prepare" and attempts["prepare"] == 1:
            raise OSError("loop allocation failed")
        return tmp_path / "workspace-mount"

    monkeypatch.setattr(sandbox, "prepare", prepare)
    program = "print('hi')"
    with pytest.raises(SandboxError) as err:
        sandbox.launch(JOB, 1, "product", "a" * 40, program)
    assert err.value.code == "coding_setup_failed" and err.value.stopped is True
    saved = json.loads((tmp_path / f"{JOB}.json").read_text())
    assert saved["state"] == "failed"
    assert saved["setup_failure"] == ("command_failed" if point == "create" else "coding_setup_failed")
    assert any(args[0] == "umount" for args in calls)
    assert not any(args[:2] == ("docker", "rm") for args in calls)
    assert sandbox.launch(JOB, 1, "product", "a" * 40, program)["state"] == "failed"
    assert attempts["prepare"] == 1
    assert sandbox.launch("test-later-0002", 1, "product", "a" * 40, program)["state"] == "running"


def test_unanswering_daemon_keeps_a_failed_start_fenced(tmp_path, monkeypatch):
    calls = []

    def prepare(job, program):
        raise OSError("loop failed")

    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path},
                            execute=_absence_execute(calls, version_error=True))
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    monkeypatch.setattr(sandbox, "prepare", prepare)
    with pytest.raises(OSError, match="loop failed"):
        sandbox.launch(JOB, 1, "product", "a" * 40, "print('hi')")
    assert json.loads((tmp_path / f"{JOB}.json").read_text())["state"] == "stop_unconfirmed"
    assert not any(args[:2] == ("docker", "ps") for args in calls)
    with pytest.raises(SandboxError, match="fenced"):
        sandbox.launch("test-later-0002", 1, "product", "a" * 40, "print('hi')")


def test_several_labeled_containers_are_not_absence(tmp_path, monkeypatch):
    calls = []
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path},
                            execute=_absence_execute(calls, ps="aaaa\nbbbb\n"))
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)

    def prepare(job, program):
        raise SandboxError("mount failed", code="workspace_mount_unverified")

    monkeypatch.setattr(sandbox, "prepare", prepare)
    with pytest.raises(SandboxError) as err:
        sandbox.launch(JOB, 1, "product", "a" * 40, "print('hi')")
    assert err.value.code == "workspace_mount_unverified" and getattr(err.value, "stopped", False) is not True
    assert json.loads((tmp_path / f"{JOB}.json").read_text())["state"] == "stop_unconfirmed"
    with pytest.raises(SandboxError, match="fenced"):
        sandbox.launch("test-later-0002", 1, "product", "a" * 40, "print('hi')")


def test_labeled_container_found_at_start_is_stopped(tmp_path, monkeypatch):
    calls, created = [], {"count": 0}
    fake = _absence_execute(calls, ps="deadbeef\n", inspect_running=True, exit_code=137)

    def execute(*args, **kwargs):
        if args[:2] == ("docker", "ps"):
            label = next(arg for arg in args if arg.startswith("label=agentic.coding="))
            if not label.endswith(JOB):
                calls.append(args)
                assert "{{.ID}}" in args
                return SimpleNamespace(returncode=0, stdout="")
        if args[:2] == ("docker", "create"):
            created["count"] += 1
            if created["count"] == 1:
                raise SandboxError("create lost its reply", code="command_failed")
            return SimpleNamespace(returncode=0, stdout="created-id\n")
        return fake(*args, **kwargs)

    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path}, execute=execute)
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    monkeypatch.setattr(sandbox, "prepare", lambda job, program: tmp_path / "workspace-mount")
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path).name == "workspace")
    with pytest.raises(SandboxError) as err:
        sandbox.launch(JOB, 1, "product", "a" * 40, "print('hi')")
    assert err.value.code == "coding_setup_failed" and err.value.stopped is True
    saved = json.loads((tmp_path / f"{JOB}.json").read_text())
    assert saved["state"] == "failed" and saved["container_id"] == "deadbeef"
    assert saved["setup_failure"] == "command_failed"
    assert any(args[:2] == ("docker", "kill") for args in calls)
    assert any(args[:2] == ("docker", "wait") for args in calls)
    assert sandbox.launch("test-later-0002", 1, "product", "a" * 40, "print('hi')")["state"] == "running"


def test_absence_proof_clears_status_cancel_and_recover(tmp_path, monkeypatch):
    calls = []
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path}, execute=_absence_execute(calls))
    sandbox.save({"job_id": JOB, "container": "agentic-coding-" + JOB, "state": "running"})
    assert sandbox.status(JOB)["state"] == "failed"
    cancel_id = "test-cancel-0005"
    sandbox.save({"job_id": cancel_id, "container": "agentic-coding-" + cancel_id, "state": "running"})
    assert sandbox.cancel(cancel_id)["state"] == "cancelled"
    dispose_id = "test-dispose-0008"
    sandbox.save({"job_id": dispose_id, "container": "agentic-coding-" + dispose_id, "state": "running"})
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path).name == "workspace")
    assert sandbox.dispose(dispose_id)["state"] == "disposed"
    assert any(args[0] == "umount" for args in calls)
    assert not any(args[:2] == ("docker", "rm") for args in calls)
    recover_id = "test-recover-0003"
    sandbox.save({"job_id": recover_id, "container": "agentic-coding-" + recover_id, "state": "preparing"})
    assert {item["job_id"]: item["state"] for item in sandbox.recover()} == {recover_id: "interrupted"}
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    monkeypatch.setattr(sandbox, "prepare", lambda job, program: tmp_path / "workspace-mount")
    assert sandbox.launch("test-later-0002", 1, "product", "a" * 40, "print('hi')")["state"] == "running"


def test_labeled_record_is_stopped_by_status_cancel_and_recover(tmp_path):
    calls = []
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path},
                            execute=_absence_execute(calls, ps="deadbeef\n", inspect_running=False, exit_code=0))
    labeled = "test-labeled-0004"
    sandbox.save({"job_id": labeled, "container": "agentic-coding-" + labeled, "state": "running"})
    assert sandbox.status(labeled)["state"] == "succeeded"
    assert not any(args[:2] == ("docker", "kill") for args in calls)
    running = "test-cancel-0005"
    sandbox.execute = _absence_execute(calls, ps="cafebabe\n", inspect_running=True, exit_code=137)
    sandbox.save({"job_id": running, "container": "agentic-coding-" + running, "state": "running"})
    assert sandbox.cancel(running)["state"] == "cancelled"
    assert any(args[:3] == ("docker", "kill", "--signal") for args in calls)
    recovered = "test-found-0006"
    sandbox.execute = _absence_execute(calls, ps="deadbeef\n", inspect_running=True, exit_code=137)
    sandbox.save({"job_id": recovered, "container": "agentic-coding-" + recovered, "state": "stop_unconfirmed"})
    assert {item["job_id"]: item["state"] for item in sandbox.recover()} == {recovered: "interrupted"}


def test_inspect_failure_uses_proven_absence_or_a_silent_daemon(tmp_path):
    calls = []
    absent = "test-absent-0009"
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path}, execute=_absence_execute(calls, ps=""))
    sandbox.save({"job_id": absent, "container": "agentic-coding-" + absent,
                  "container_id": "gone-container", "state": "running"})
    assert sandbox.cancel(absent)["state"] == "cancelled"
    assert not any(args[:2] == ("docker", "kill") for args in calls)
    calls.clear()
    quiet = "test-silent-0010"
    sandbox.execute = _absence_execute(calls, version_error=True)
    sandbox.save({"job_id": quiet, "container": "agentic-coding-" + quiet,
                  "container_id": "silent-container", "state": "running"})
    assert sandbox.cancel(quiet)["state"] == "stop_unconfirmed"


def test_unanswering_record_stays_stop_unconfirmed(tmp_path, monkeypatch):
    calls = []
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path},
                            execute=_absence_execute(calls, version_error=True))
    quiet = "test-quiet-0007"
    sandbox.save({"job_id": quiet, "container": "agentic-coding-" + quiet, "state": "running"})
    assert sandbox.status(quiet)["state"] == "stop_unconfirmed"
    assert sandbox.cancel(quiet)["state"] == "stop_unconfirmed"
    assert sandbox.recover()[0]["state"] == "stop_unconfirmed"
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    with pytest.raises(SandboxError, match="fenced"):
        sandbox.launch("test-later-0002", 1, "product", "a" * 40, "print('hi')")


def test_patch_lines_and_narrow_secrets_withhold_one_file(tmp_path):
    import hashlib
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "notes.patch").write_text(
        "diff --git a/old.py b/old.py\n--- a/old.py\n+++ b/old.py\n"
        "-api_key = \"abcdefghij\"\n api_key = \"abcdefghij\"\n+print('clean')\n")
    (artifacts / "typed.patch").write_text(
        "diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n"
        "+api_key: SecretStr\n+api_key: str | None = None\n+password=body.password\n+access_token: str\n"
        "+api_key=\"1234567\"\n+api_key=\"abcdefgh\"\n")
    (artifacts / "leak.patch").write_text("diff --git a/t.py b/t.py\n+++ b/t.py\n+api_key=\"AKIAIOSFODNN7EXAMPLE12\"\n")
    (artifacts / "quoted.py").write_text("password = '12345678'\napi_key = \"abcd efgh\"\n")
    (artifacts / "token.py").write_text("api_key = abcdefghij1234567890\n")
    (artifacts / "short.py").write_text("api_key = abcdefghij123456789\n")
    (artifacts / "ok.py").write_text("print('fine')\n")
    (artifacts / "pem.txt").write_text("-----BEGIN PRIVATE KEY-----\n")
    (artifacts / "bearer.txt").write_text("Authorization: Bearer abcdefghijklmnop\n")
    (artifacts / "sk.txt").write_text("sk-abcdefghijklmnop\n")
    items = {item["name"]: item for item in read_artifacts(artifacts, Limits())}
    for name in ("notes.patch", "typed.patch", "short.py", "ok.py"):
        assert "content" in items[name] and items[name].get("kind") != "withheld"
    for name in ("leak.patch", "quoted.py", "token.py", "pem.txt", "bearer.txt", "sk.txt"):
        raw = (artifacts / name).read_bytes()
        assert items[name]["kind"] == "withheld" and "content" not in items[name]
        assert items[name]["size"] == len(raw) and items[name]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert "print('fine')" in items["ok.py"]["content"]


def test_skipped_files_are_listed_and_tampering_stays_fatal(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "web-preview").mkdir()
    (root / "web-preview" / "index.html").write_text("<p>bundle</p>\n")
    (root / "web-fixtures.json").write_text("{}\n")
    (root / "ok.py").write_text("print(1)\n")
    (root / "extra.py").write_text("print(2)\n")
    (root / "big.py").write_text("z" * 80)
    (root / "blob.bin").write_text("nope\n")
    (root / "page.html").write_text("<p>ok</p>\n")
    (root / "bad\nname.txt").write_text("x\n")
    (root / ("n" * 237 + ".txt")).write_text("long\n")
    (root / "nested").mkdir()
    (root / "nested" / "inner.py").write_text("x\n")
    (root / "skipped.json").write_text('{"marker":"worker"}\n')
    exported = {item["name"]: item for item in read_artifacts(root, Limits(artifact_files=1, artifact_bytes=40, web_artifacts=True))}
    assert "content" in exported["extra.py"] and "ok.py" not in exported and "page.html" not in exported
    assert "web-fixtures.json" not in exported and "web-preview/index.html" not in exported
    skipped = json.loads(exported["skipped.json"]["content"])["skipped"]
    reasons = {entry["reason"] for entry in skipped}
    assert reasons == {"unsupported_type", "name_not_allowed", "file_limit", "size_limit"}
    assert {"name": "blob.bin", "reason": "unsupported_type"} in skipped
    assert {"name": "big.py", "reason": "size_limit"} in skipped
    assert {"name": "nested/inner.py", "reason": "name_not_allowed"} in skipped
    assert "worker" not in exported["skipped.json"]["content"]
    assert not any(entry["name"].startswith("web-preview") or entry["name"] == "web-fixtures.json" for entry in skipped)
    for path in root.iterdir():
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir() and not path.is_symlink():
            for child in path.rglob("*"):
                if child.is_file() or child.is_symlink():
                    child.unlink()
    (root / "text.txt").write_bytes(b"hello\x00")
    (root / "noise.log").write_bytes(b"\xff\xfe")
    (root / "zz.py").write_text("print(1)\n")
    # Nine bytes is exactly zz.py, and it sorts last. Counting the bad files would skip it.
    exported = {item["name"]: item for item in read_artifacts(root, Limits(artifact_bytes=9))}
    assert exported["zz.py"]["content"] == "print(1)\n"
    assert "text.txt" not in exported and "noise.log" not in exported
    skipped = json.loads(exported["skipped.json"]["content"])["skipped"]
    assert {"name": "text.txt", "reason": "not_text"} in skipped
    assert {"name": "noise.log", "reason": "not_text"} in skipped
    (root / "noise.log").unlink()
    (root / "zz.py").unlink()
    (root / "text.txt").write_text("plain\n")
    os.link(root / "text.txt", root / "alias.txt")
    with pytest.raises(SandboxError, match="regular file"):
        read_artifacts(root, Limits())
    (root / "alias.txt").unlink()
    (root / "leak.html").symlink_to(root / "text.txt")
    with pytest.raises(SandboxError, match="safely opened"):
        read_artifacts(root, Limits())
