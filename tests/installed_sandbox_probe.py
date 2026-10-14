"""Root-only offline sandbox probe for the disposable ISO-install test VM.

Input to `sudo python3 -` inside tests/test_linux_install.py's guest. This script
refuses other hosts. It never installs dependencies or accesses provider accounts.
"""
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PRODUCT = Path("/opt/agentic-home")
STATE = Path("/var/lib/agentic-home")
# A slow shared test disk stretched the 60 MiB ext4 fsync loop past 30 s; use the
# product's 300 s wall budget and size outer alarms above job and cleanup steps.
COMMAND_TIMEOUT_SECONDS = 60
CLEANUP_COMMAND_TIMEOUT_SECONDS = 30
QUOTA_WALL_SECONDS = 300
READBACK_SECONDS = 30
QUOTA_WAIT_SECONDS = QUOTA_WALL_SECONDS + READBACK_SECONDS
CANCEL_READY_SECONDS = 120
PROBE_ALARM_SECONDS = 900
CLEANUP_ALARM_SECONDS = 420
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "DOCKER_HOST": "unix:///var/run/docker.sock"}


class ProbeFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def command(*args, timeout=COMMAND_TIMEOUT_SECONDS, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=SAFE_ENV)
    if check and result.returncode:
        raise ProbeFailure("command_failed_" + Path(args[0]).name)
    return result


def guard():
    require(os.geteuid() == 0, "requires_root_in_disposable_guest")
    require(Path("/etc/hostname").read_text().strip() == "agentic-disposable", "not_disposable_hostname")
    serials = [*Path("/sys/class/block").glob("*/serial"),
               *Path("/sys/class/block").glob("*/device/serial")]
    require(any(path.read_text().strip() == "AGENTIC_TEST_ONLY" for path in serials),
            "disposable_disk_serial_missing")
    require(command("systemd-detect-virt", "--vm").stdout.strip() in {"kvm", "qemu"}, "not_test_vm")
    require((STATE / "installed.json").is_file() and (PRODUCT / "host/agentic_host/coding_sandbox.py").is_file(),
            "product_not_installed")


QUOTA_PROGRAM = r'''import difflib, errno, json, os, pathlib, resource, subprocess, sys
assert os.geteuid() == 10000
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/var/lib/agentic-home/controller').exists()
assert pathlib.Path('/work/source/.git').is_dir()
assert not pathlib.Path('/work/source/.git/objects/info/alternates').exists()
source = pathlib.Path('/work/source/sample.py')
before = source.read_text()
assert before == 'def value():\n    return 1\n'
after = before.replace('return 1', 'return 2')
source.write_text(after)
experiment = "import runpy; assert runpy.run_path('/work/source/sample.py')['value']() == 2"
result = subprocess.run([sys.executable, '-I', '-c', experiment], capture_output=True)
assert result.returncode == 0
# Reserve space for the broker-readable result before exhausting the filesystem.
reserve = pathlib.Path('/work/scratch/result-reserve.bin')
chunk = b'x' * (1024 * 1024)
reserve.write_bytes(chunk)
files, observed_errno = [], None
try:
    for index in range(100):
        path = pathlib.Path('/work/scratch') / ('quota-%03d.bin' % index)
        with path.open('wb', buffering=0) as stream:
            files.append(path)
            stream.write(chunk)
            os.fsync(stream.fileno())
except OSError as exc:
    observed_errno = exc.errno
assert observed_errno == errno.ENOSPC, observed_errno
sizes = [path.stat().st_size for path in files]
fsize_limit = resource.getrlimit(resource.RLIMIT_FSIZE)[0]
assert max(sizes) <= len(chunk) < fsize_limit
assert sum(sizes) > fsize_limit  # Aggregate filesystem exhaustion, not a per-file failure.
capacity = os.statvfs('/work').f_blocks * os.statvfs('/work').f_frsize
assert capacity <= 64 * 1024 * 1024
reserve.unlink()
artifacts = pathlib.Path('/work/artifacts')
(artifacts / 'change.patch').write_text(''.join(difflib.unified_diff(
    before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile='a/sample.py', tofile='b/sample.py')))
(artifacts / 'experiment.py').write_text(experiment + '\n')
(artifacts / 'result.json').write_text(json.dumps({
    'test_exit_code': result.returncode, 'errno': observed_errno,
    'filesystem_capacity_bytes': capacity, 'written_bytes': sum(sizes),
    'largest_file_bytes': max(sizes), 'individual_file_limit_bytes': fsize_limit,
    'observed_from': 'host_container_in_disposable_outer_vm', 'model_backend': 'none'}))
'''

CANCEL_PROGRAM = r'''import json, pathlib, subprocess, time
children = [subprocess.Popen(['/usr/bin/sleep', '60']) for _ in range(3)]
pathlib.Path('/work/artifacts/ready.json').write_text(json.dumps({'started_children': len(children)}))
while True:
    with pathlib.Path('/work/scratch/heartbeat.txt').open('a') as stream:
        stream.write('tick\n')
    time.sleep(.05)
'''


def job_diagnostics(job):
    """Only broker-owned status values; no program output, paths or credentials."""
    states = {"preparing", "running", "succeeded", "failed", "stop_unconfirmed",
              "cancelled", "interrupted", "disposed"}
    data = {"state": job.get("state") if job.get("state") in states else "unreported"}
    if type(job.get("exit_code")) is int and 0 <= job["exit_code"] <= 255:
        data["exit_code"] = job["exit_code"]
    if type(job.get("oom_killed")) is bool:
        data["oom_killed"] = job["oom_killed"]
    failure = job.get("stop_failure")
    if isinstance(failure, dict):
        safe = {}
        if failure.get("error_code") in {"command_failed", "command_timeout", "sandbox_rejected"}:
            safe["error_code"] = failure["error_code"]
        if failure.get("operation") in {"docker.inspect", "docker.kill", "docker.wait"}:
            safe["operation"] = failure["operation"]
        for key, minimum, maximum in (("timeout_seconds", 1, 900), ("command_exit_code", -128, 255)):
            if type(failure.get(key)) is int and minimum <= failure[key] <= maximum:
                safe[key] = failure[key]
        if safe:
            data["stop_failure"] = safe
    return data


def cleanup(directory, jobs):
    """Only owned test containers/mounts; never delete a still-mounted workspace."""
    if directory is None:
        return True
    safe = True
    for job_id in reversed(jobs):
        name = "agentic-coding-" + job_id
        try:
            response = command("docker", "inspect", name, check=False, timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS)
            if response.returncode == 0:
                info = json.loads(response.stdout)[0]
                require(info.get("Config", {}).get("Labels", {}).get("agentic.coding") == name,
                        "cleanup_identity_mismatch")
                command("docker", "rm", "-f", name, timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS)
            else:
                # An unavailable daemon is not proof that its writer stopped.
                names = command("docker", "ps", "-a", "--format", "{{.Names}}",
                                timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS).stdout.splitlines()
                require(name not in names, "cleanup_stop_unconfirmed")
            mount = directory / "jobs" / job_id / "workspace"
            if os.path.ismount(mount):
                command("umount", str(mount), timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS)
            require(not os.path.ismount(mount), "cleanup_mount_remains")
            disk = mount.parent / "workspace.ext4"
            if disk.exists():
                loops = command("losetup", "-j", str(disk), timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS).stdout
                for line in loops.splitlines():
                    device = line.split(":", 1)[0]
                    require(re.fullmatch(r"/dev/loop[0-9]+", device), "unexpected_loop_device")
                    command("losetup", "-d", device, timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS)
                require(not command("losetup", "-j", str(disk), timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS).stdout.strip(),
                        "cleanup_loop_remains")
        except Exception:
            safe = False
    if safe:
        shutil.rmtree(directory)
        safe = not directory.exists()
    return safe


def probe():
    started = time.monotonic()
    directory, jobs, stage = None, [], "guard"
    sandbox, quota_id, cancel_id = None, None, None
    sandbox_error_type = None
    report = {"kind": "installed_offline_coding_sandbox", "runtime_location": "disposable_outer_linux_vm",
              "observed_from": "host_container_in_disposable_outer_vm", "model_backend": "none",
              "ha_backend": "none", "device_kind": "none", "cloud_login": "not_run",
              "home_worker": "not_run", "E1": "not_run", "E2": "not_run", "result": "failed"}
    previous_umask = os.umask(0o077)

    def expired(*_):
        raise ProbeFailure("probe_deadline_exceeded")

    previous_alarm = signal.signal(signal.SIGALRM, expired)
    signal.alarm(PROBE_ALARM_SECONDS)  # Covers preparation, quota wait, cancel readiness and checks before cleanup.
    try:
        guard()
        sys.path.insert(0, str(PRODUCT / "host"))
        from agentic_host.coding_sandbox import CodingSandbox, Limits, SandboxError
        sandbox_error_type = SandboxError

        stage = "fixed_image"
        version = json.loads((STATE / "installed.json").read_text())["version"]
        tag = "agentic-home-runner:" + version
        image = json.loads((PRODUCT / "images.lock.json").read_text())[tag]
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "invalid_installed_image_pin")
        require(command("docker", "image", "inspect", tag, "--format", "{{.Id}}").stdout.strip() == image,
                "installed_image_mismatch")
        report.update(installed_release=version, runner_image_id=image)
        stage = "source_fixture"
        directory = Path(tempfile.mkdtemp(prefix="coding-probe-", dir=STATE))
        repo = directory / "fixture-repo"
        repo.mkdir()
        original = "def value():\n    return 1\n"
        (repo / "sample.py").write_text(original)
        command("git", "init", "-q", str(repo))
        command("git", "-C", str(repo), "add", "sample.py")
        command("git", "-C", str(repo), "-c", "user.name=Disposable probe",
                "-c", "user.email=probe@example.invalid", "-c", "core.hooksPath=/dev/null",
                "commit", "-qm", "isolated baseline")
        commit = command("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        original_metadata = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in (repo / ".git").rglob("*") if path.is_file()}
        sandbox = CodingSandbox(directory / "jobs", image, {"fixture": repo},
                                limits=Limits(disk_mib=64, wall_seconds=QUOTA_WALL_SECONDS))
        token = uuid.uuid4().hex[:16]
        job_id = quota_id = "probe-quota-" + token
        jobs.append(job_id)
        stage = "launch_quota"
        first = sandbox.launch(job_id, 1, "fixture", commit, QUOTA_PROGRAM)
        require(first["state"] == "running", "job_not_started")
        duplicate = sandbox.launch(job_id, 1, "fixture", commit, QUOTA_PROGRAM)
        require(duplicate["job_id"] == first["job_id"], "duplicate_new_handle")
        first_id = sandbox.record(job_id)["container_id"]
        try:
            sandbox.launch(job_id, 2, "fixture", commit, QUOTA_PROGRAM)
        except SandboxError:
            pass
        else:
            raise ProbeFailure("changed_lease_reused_job")
        require(sandbox.record(job_id)["container_id"] == first_id, "duplicate_started_second_container")
        stage = "quota_completion"
        # A slow shared test disk stretched this job past 30 s; wait through the
        # product's 300 s watchdog plus 30 s for Docker's stopped-state readback.
        quota_deadline = time.monotonic() + QUOTA_WAIT_SECONDS
        while True:
            outcome = sandbox.status(job_id)
            report["quota_job"] = job_diagnostics(outcome)
            if outcome["state"] != "running" or time.monotonic() >= quota_deadline:
                break
            time.sleep(min(.2, max(0, quota_deadline - time.monotonic())))
        require(outcome["state"] == "succeeded" and outcome["exit_code"] == 0, "quota_job_did_not_succeed")
        workspace = directory / "jobs" / job_id / "workspace"
        disk = workspace.parent / "workspace.ext4"
        stage = "clone_and_quota_evidence"
        require(command("findmnt", "-n", "-o", "FSTYPE", "-T", str(workspace)).stdout.strip() == "ext4",
                "workspace_not_ext4")
        require(disk.stat().st_size == 64 * 1024 * 1024 and disk.stat().st_blocks * 512 >= disk.stat().st_size,
                "disk_reservation_was_discarded")
        clone = workspace / "source"
        require(clone.stat().st_dev != repo.stat().st_dev, "clone_not_on_independent_filesystem")
        require((clone / ".git/HEAD").read_text().strip() == commit, "clone_wrong_commit")
        require(not (clone / ".git/objects/info/alternates").exists(), "clone_uses_shared_object_store")
        require('[remote "origin"]' not in (clone / ".git/config").read_text(), "clone_retains_remote")
        require((clone / "sample.py").read_text() != original and (repo / "sample.py").read_text() == original,
                "candidate_changed_original_checkout")
        current_metadata = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (repo / ".git").rglob("*") if path.is_file()}
        require(current_metadata == original_metadata, "candidate_changed_original_git_metadata")
        artifacts = sandbox.artifacts(job_id)
        require({item["name"] for item in artifacts} == {"change.patch", "experiment.py", "result.json"},
                "expected_artifacts_missing")
        for artifact in artifacts:
            require(hashlib.sha256(artifact["content"].encode()).hexdigest() == artifact["sha256"],
                    "artifact_digest_mismatch")
        quota = json.loads(next(item["content"] for item in artifacts if item["name"] == "result.json"))
        require(quota["errno"] == errno.ENOSPC and quota["written_bytes"] > quota["individual_file_limit_bytes"]
                and quota["largest_file_bytes"] < quota["individual_file_limit_bytes"], "not_a_filesystem_quota")
        report.update(independent_clone="passed", original_checkout_unchanged=True,
                      original_git_metadata_unchanged=True, idempotency="passed", fixed_ext4_quota=quota,
                      artifacts=[{k: item[k] for k in ("name", "sha256", "size")} for item in artifacts])
        sandbox.dispose(job_id)
        require(not os.path.ismount(workspace), "dispose_left_mount")
        stage = "cancel_and_fencing"
        cancel_id = "probe-cancel-" + token
        jobs.append(cancel_id)
        sandbox.launch(cancel_id, 1, "fixture", commit, CANCEL_PROGRAM)
        cancel_workspace = directory / "jobs" / cancel_id / "workspace"
        ready = cancel_workspace / "artifacts/ready.json"
        cancel_deadline = time.monotonic() + CANCEL_READY_SECONDS
        while time.monotonic() < cancel_deadline:
            if ready.is_file() and (cancel_workspace / "scratch/heartbeat.txt").is_file():
                break
            time.sleep(.1)
        require(ready.is_file() and json.loads(ready.read_text())["started_children"] == 3,
                "cancel_job_never_started_children")
        try:
            sandbox.artifacts(cancel_id)
        except SandboxError:
            pass
        else:
            raise ProbeFailure("export_allowed_with_live_writer")
        require(sandbox.cancel(cancel_id)["state"] == "cancelled", "stop_not_confirmed")
        info = sandbox.inspect(sandbox.record(cancel_id))
        report["cancel_job"] = job_diagnostics({"state": "cancelled", "exit_code": info["State"]["ExitCode"],
                                                "oom_killed": info["State"].get("OOMKilled")})
        require(not info["State"]["Running"] and info["State"]["Pid"] == 0, "writer_still_running")
        heartbeat = cancel_workspace / "scratch/heartbeat.txt"
        frozen = heartbeat.read_bytes()
        time.sleep(.3)
        require(heartbeat.read_bytes() == frozen, "old_writer_still_changes_workspace")
        require(sandbox.artifacts(cancel_id)[0]["name"] == "ready.json", "cancelled_artifact_unavailable")
        sandbox.dispose(cancel_id)
        report.update(cancellation="passed", process_tree_stopped=True, writer_fencing="passed",
                      result="passed", limitations=["Offline Python fixture; no coding CLI or model account.",
                      "Outer disposable VM only; no HAOS App, hardware or household validation."])
    except Exception as exc:
        report.update(result="failed", stage=stage,
                      error=exc.args[0] if isinstance(exc, ProbeFailure) else type(exc).__name__)
        if sandbox_error_type is not None and isinstance(exc, sandbox_error_type):
            # The installed broker exposes fixed codes, enum operations and
            # bounded numeric status only; never include its raw command output.
            report.update(error_code=getattr(exc, "code", "sandbox_rejected"),
                          **getattr(exc, "diagnostics", {}))
        # Read only local broker records here: an unavailable Docker daemon must
        # not consume the remaining cleanup allowance or leak its error output.
        for name, identifier in (("quota_job", quota_id), ("cancel_job", cancel_id)):
            if sandbox is not None and identifier is not None:
                try:
                    current = job_diagnostics(sandbox.record(identifier))
                    report[name] = {**report.get(name, {}), **current}
                except Exception:
                    pass
    finally:
        signal.alarm(CLEANUP_ALARM_SECONDS)
        try:
            report["cleanup"] = "passed" if cleanup(directory, jobs) else "failed"
        except Exception:
            report["cleanup"] = "failed"
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        os.umask(previous_umask)
        if report["cleanup"] != "passed":
            report["result"] = "failed"
        report["duration_seconds"] = round(time.monotonic() - started, 2)
    return report


if __name__ == "__main__":
    result = probe()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["result"] == "passed" else 1)
