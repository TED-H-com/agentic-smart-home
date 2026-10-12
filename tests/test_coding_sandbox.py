"""Containment unit cases and real Docker negatives, not E1 cloud/HAOS acceptance."""
import json
import os
import subprocess
import sys
import uuid
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
    with pytest.raises(SandboxError, match="quota"):
        read_artifacts(artifacts, Limits(artifact_bytes=1))
    path.unlink()
    (artifacts / "leak.txt").symlink_to(tmp_path / "owner-secret")
    with pytest.raises(SandboxError, match="safely opened"):
        read_artifacts(artifacts, Limits())
    (artifacts / "leak.txt").unlink()
    os.mkfifo(artifacts / "pipe.txt")
    with pytest.raises(SandboxError, match="regular file"):
        read_artifacts(artifacts, Limits())
    (artifacts / "pipe.txt").unlink()
    (artifacts / "token.txt").write_text("access_token = not-a-real-account-token")
    with pytest.raises(SandboxError, match="secret"):
        read_artifacts(artifacts, Limits())
    (artifacts / "token.txt").unlink()
    (artifacts / "active.html").write_text("<script>fetch('/approval')</script>")
    with pytest.raises(SandboxError, match="type"):
        read_artifacts(artifacts, Limits())


def test_unconfirmed_stop_never_releases_writer_or_exports(tmp_path):
    calls = []
    def execute(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "inspect"):
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
    try:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=20)
        subprocess.run(["docker", "start", name], check=True, capture_output=True, timeout=20)
        if stop_method == "cancel":
            import time
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
        code = subprocess.check_output(["docker", "wait", name], text=True, timeout=20).strip()
        state = json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]["State"]
        result = workspace / "artifacts/result.json"
        errors = (workspace / "stderr.txt").read_text() if (workspace / "stderr.txt").exists() else ""
        assert result.exists(), {"stderr": errors, "state": state, "files": [str(p.relative_to(workspace)) for p in workspace.rglob("*")]}
        assert json.loads(result.read_text())["checks"] == "passed"
        assert code == "137" and state["Running"] is False and state["Pid"] == 0
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)


def test_artifact_directory_budget(tmp_path):
    for index in range(513):
        (tmp_path / str(index)).mkdir()
    with pytest.raises(SandboxError, match="directory limit"):
        read_artifacts(tmp_path, Limits())


def test_idempotency_and_active_writer_fencing(tmp_path, monkeypatch):
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path})
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
                       check=True, capture_output=True, timeout=20)
        subprocess.run(["docker", "start", name], check=True, capture_output=True, timeout=20)
        code = subprocess.check_output(["docker", "wait", name], text=True, timeout=20).strip()
        assert code == "0", (tmp_path / "stderr.txt").read_text()
        assert (tmp_path / "artifacts/memory.txt").read_text().startswith("kernel killed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)


def test_retained_workspace_budget_prevents_unbounded_disk_growth(tmp_path, monkeypatch):
    sandbox = CodingSandbox(tmp_path, IMAGE, {"product": tmp_path})
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    for index in range(4):
        (tmp_path / f"retained-{index}").mkdir()
    with pytest.raises(SandboxError, match="Retained workspace budget"):
        sandbox.launch(JOB, 1, "product", "a" * 40, "print('hi')")
    assert not (tmp_path / f"{JOB}.json").exists()


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
