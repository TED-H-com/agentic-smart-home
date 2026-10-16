"""Home launcher contract negatives; real HAOS containment is a separate test."""
import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from agentic_host.coding_sandbox import Limits, SandboxError
from agentic_runner import home_launcher as launcher
from agentic_runner import home_sandbox
from agentic_runner.home_sandbox import HomeSandbox, launch_failure

JOB = "home-test-12345678"
IDENTITY = {"boot_id": "fixture-boot", "pid_ns": 101, "cgroup_ns": 102, "mnt_ns": 103}


def group_fixture(tmp_path, monkeypatch, *, populated=0):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "identity", lambda: dict(IDENTITY))
    path = tmp_path / "cgroups" / ("w-" + "a" * 32)
    path.mkdir(parents=True)
    for key, value in {"cgroup.events": f"populated {populated}\nfrozen 0\n", "cgroup.kill": "",
                       "memory.events": "oom 0\noom_kill 0\n", "pids.events": "max 0\n",
                       "cpu.stat": "usage_usec 4\nnr_throttled 1\n"}.items():
        (path / key).write_text(value)
    group = {"name": path.name, "inode": path.stat().st_ino, "runtime": dict(IDENTITY)}
    return path, group


def sandbox_fixture(tmp_path, monkeypatch):
    path, group = group_fixture(tmp_path, monkeypatch)
    root = tmp_path / "work"
    root.mkdir()
    (root / JOB).mkdir()
    sandbox = HomeSandbox(root, None, {})
    job = {"job_id": JOB, "lease_epoch": 1, "state": "running", "runtime": dict(IDENTITY), "cgroup": group,
           "stop_confirmed": False, "launcher": {"pid": 99, "start": 400, "uid": 0}}
    sandbox.save(job)
    return sandbox, job, path


def test_fixed_namespace_command_uses_only_staged_workspace_and_public_runtime():
    args = launcher.bubblewrap_command(32, relay=True)
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
                 "--unshare-uts", "--unshare-cgroup", "--clearenv", "--die-with-parent", "--new-session"):
        assert flag in args
    assert args[args.index("--uid") + 1] == "10000"
    assert args[args.index("--gid") + 1] == "10000"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    writable = [(args[i + 1], args[i + 2]) for i, value in enumerate(args) if value == "--bind"]
    assert writable == [("/opt/agentic-worker/staging/workspace", "/work"),
                        ("/opt/agentic-worker/staging/workspace/scratch", "/tmp")]
    assert [args[i + 1] for i, value in enumerate(args) if value == "--remount-ro"] == ["/proc", "/dev", "/"]
    assert "--tmpfs" not in args and "--dev-bind" not in args
    assert not any(secret in " ".join(args) for secret in ("/data", "/config", "SUPERVISOR_TOKEN", "/sys", "docker.sock", "/dev/fuse"))
    assert args[-4:] == ["--", "/usr/local/bin/python", "-I", "/work/task.py"]
    assert not any("/proc/self/fd/" in value for value in args)


def test_verifier_has_no_relay_but_still_protects_git():
    args = launcher.bubblewrap_command(32)
    assert "/relay.sock" not in args
    assert args[args.index("/work/source/.git") - 2] == "--ro-bind"


def test_staging_mounts_only_after_new_private_namespace(tmp_path, monkeypatch):
    stage = tmp_path / "staging"
    stage.mkdir()
    stage.chmod(0o755)
    (stage / "workspace").mkdir()
    (stage / "workspace").chmod(0o755)
    (stage / "relay.sock").touch(mode=0o600)
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    monkeypatch.setattr(launcher, "STAGING", stage)
    lstat = Path.lstat
    def root_metadata(path):
        original = lstat(path)
        return SimpleNamespace(st_mode=original.st_mode, st_uid=0)
    monkeypatch.setattr(Path, "lstat", root_metadata)
    monkeypatch.setattr(os.path, "ismount", lambda path: path == workspace)
    calls = []
    monkeypatch.setattr(launcher, "syscall", lambda *args: calls.append(args))
    launcher.stage_workspace(workspace)
    assert [entry[0] for entry in calls] == ["unshare", "mount", "mount"]
    assert calls[0][1].value == 0x20000
    assert calls[1][2] == b"/" and calls[1][4].value == 0x44000
    assert calls[2][1:4] == (os.fsencode(workspace), os.fsencode(stage / "workspace"), None)
    assert calls[2][4].value == 4096
    assert workspace.stat().st_mode & 0o777 != 0o777


def test_staging_symlink_never_causes_namespace_or_mount_effect(tmp_path, monkeypatch):
    stage = tmp_path / "staging"
    stage.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(launcher, "STAGING", stage)
    monkeypatch.setattr(launcher, "syscall", lambda *args: pytest.fail("Invalid staging must have no mount effects"))
    with pytest.raises(launcher.BoundaryError, match="home_staging_path_invalid"):
        launcher.stage_workspace(tmp_path)


def test_actual_child_closes_directory_socket_and_other_outside_descriptors(tmp_path):
    # An ordinary isolated subprocess exercises descriptor handling only, with
    # no namespace, mount, privilege or network changes on the dev host.
    script = r'''
import importlib.util,json,os,socket,sys
spec=importlib.util.spec_from_file_location('launcher',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
work=os.open(sys.argv[2],os.O_PATH|os.O_DIRECTORY)
one,two=socket.socketpair();socketfds=[one.fileno(),two.fileno()]
filtered=os.open('/dev/null',os.O_RDONLY)
for fd in [work,*socketfds]:os.set_inheritable(fd,True)
m.inherit_filter_only(filtered)
closed=[]
for fd in [work,*socketfds]:
 try:os.fstat(fd);closed.append(False)
 except OSError:closed.append(True)
print(json.dumps({'outside_closed':closed,'filter_open':os.fstat(filtered).st_mode>0,'filter_inherited':os.get_inheritable(filtered)}))
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, launcher.__file__, str(tmp_path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"outside_closed": [True, True, True], "filter_open": True, "filter_inherited": True}


def private_proc_fixture(monkeypatch, *, child_pid=1, child_namespace=88, visible=("1",), mount_namespace=22):
    sequence = iter([77, child_namespace])
    original_stat = os.stat
    def observed_stat(path, *args, **kwargs):
        if str(path) == "/proc/self/ns/mnt":
            return SimpleNamespace(st_ino=mount_namespace)
        if str(path) == "/proc/self/ns/pid":
            return SimpleNamespace(st_ino=next(sequence))
        if str(path) == "/proc/1/ns/pid":
            return SimpleNamespace(st_ino=child_namespace)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, "stat", observed_stat)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getpid", lambda: child_pid)
    monkeypatch.setattr(os, "fork", lambda: 0)
    monkeypatch.setattr(os, "readlink", lambda path: "1")
    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: iter(Path("/proc") / name for name in visible)
                        if str(path) == "/proc" else original_iterdir(path))
    calls = []
    monkeypatch.setattr(launcher, "syscall", lambda *args: calls.append(args))
    return calls


def test_fresh_proc_requires_separate_mount_pid_namespace_and_only_pid_one(monkeypatch):
    calls = private_proc_fixture(monkeypatch)
    launcher.private_pid_proc(11)
    assert [entry[0] for entry in calls] == ["unshare", "mount"]
    assert calls[0][1].value == 0x20000000
    assert calls[1][1:4] == (b"proc", b"/proc", b"proc") and calls[1][4].value == 14


def test_fresh_proc_never_changes_app_mount_namespace(monkeypatch):
    calls = private_proc_fixture(monkeypatch, mount_namespace=11)
    with pytest.raises(launcher.BoundaryError, match="home_private_mount_namespace_required"):
        launcher.private_pid_proc(11)
    assert calls == []


@pytest.mark.parametrize("pid,namespace", [(2, 88), (1, 77)])
def test_fresh_proc_refuses_missing_private_pid_identity_before_mount(monkeypatch, pid, namespace):
    calls = private_proc_fixture(monkeypatch, child_pid=pid, child_namespace=namespace)
    with pytest.raises(launcher.BoundaryError, match="home_private_pid_namespace_required"):
        launcher.private_pid_proc(11)
    assert [entry[0] for entry in calls] == ["unshare"]


def test_fresh_proc_rejects_visibility_of_any_extra_parent_pid(monkeypatch):
    private_proc_fixture(monkeypatch, visible=("1", "55"))
    with pytest.raises(launcher.BoundaryError, match="home_private_proc_unconfirmed"):
        launcher.private_pid_proc(11)


@pytest.mark.parametrize("failure,code", [("unshare", "home_pid_namespace_unavailable"),
                                        ("mount", "home_private_proc_mount_unavailable")])
def test_fresh_proc_platform_failures_have_fixed_codes(monkeypatch, failure, code):
    private_proc_fixture(monkeypatch)
    def denied(name, *args):
        if name == failure:
            raise launcher.BoundaryError("fixture-private-kernel-details")
    monkeypatch.setattr(launcher, "syscall", denied)
    with pytest.raises(launcher.BoundaryError, match="^" + code + "$"):
        launcher.private_pid_proc(11)


@pytest.mark.parametrize("kind,expected", [("exit", 7), ("term", -15), ("kill", -9)])
def test_actual_trusted_wait_preserves_child_exit_or_fatal_signal(kind, expected):
    # Real wait/termination in an ordinary disposable subprocess. No namespaces,
    # cgroups, mounts, privileges or existing processes are changed.
    script = r'''
import importlib.util,os,signal,sys
spec=importlib.util.spec_from_file_location('launcher',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
child=os.fork()
if child==0:
 if sys.argv[2]=='exit':os._exit(7)
 os.kill(os.getpid(),signal.SIGTERM if sys.argv[2]=='term' else signal.SIGKILL)
 os._exit(99)
signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGTERM})
m.wait_namespace_child(child)
os._exit(98)
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, launcher.__file__, kind],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == expected


@pytest.mark.parametrize("membership", ["0::/system.slice/docker-123.scope", "0::/broker", "", "1:name=systemd:/"])
def test_initialization_rejects_nonprivate_cgroup_membership(tmp_path, membership):
    with pytest.raises(launcher.BoundaryError, match="home_private_app_cgroup_required"):
        launcher.validate_app_cgroup(tmp_path, membership)


def test_global_cgroup_root_cannot_be_used_as_delegation(tmp_path):
    (tmp_path / "cgroup.controllers").write_text("memory cpu pids")
    with pytest.raises(launcher.BoundaryError, match="home_private_app_cgroup_required"):
        launcher.validate_app_cgroup(tmp_path, "0::/")
    for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max"):
        (tmp_path / name).write_text("max")
    launcher.validate_app_cgroup(tmp_path, "0::/")
    (tmp_path / "cgroup.controllers").write_text("cpu pids")
    with pytest.raises(launcher.BoundaryError, match="home_cgroup_controllers_unavailable"):
        launcher.validate_app_cgroup(tmp_path, "0::/")


def test_visible_fuse_device_must_actually_be_accessible(monkeypatch):
    monkeypatch.setattr(Path, "stat", lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o666))
    calls = []
    def denied(path, flags):
        calls.append((path, flags))
        raise PermissionError(errno.EPERM, "fixture private path")
    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(launcher.BoundaryError, match="^home_fuse_device_unavailable$") as caught:
        launcher.validate_fuse_device()
    assert caught.value.__cause__.errno == errno.EPERM
    assert calls == [("/dev/fuse", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)]


def test_fuse_device_probe_immediately_closes_owned_handle(monkeypatch):
    monkeypatch.setattr(Path, "stat", lambda path: SimpleNamespace(st_mode=stat.S_IFCHR | 0o666))
    closed = []
    monkeypatch.setattr(os, "open", lambda *args: 77)
    monkeypatch.setattr(os, "close", closed.append)
    launcher.validate_fuse_device()
    assert closed == [77]


def test_same_boot_namespace_change_never_proves_stop(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "identity", lambda: {**IDENTITY, "pid_ns": 999, "cgroup_ns": 998})
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    assert sandbox.status(JOB)["stop_confirmed"] is False
    assert (path / "cgroup.kill").read_text() == ""
    with pytest.raises(SandboxError, match="unconfirmed"):
        sandbox.dispose(JOB)


def test_changed_kernel_boot_proves_previous_processes_absent(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "identity", lambda: {**IDENTITY, "boot_id": "new-kernel-boot"})
    result = sandbox.cancel(JOB)
    assert result["state"] == "interrupted" and result["stop_confirmed"] and result["previous_boot_stopped"]
    assert (path / "cgroup.kill").read_text() == ""
    with pytest.raises(SandboxError, match="export"):
        sandbox.artifacts(JOB)


def test_replaced_cgroup_is_never_killed(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    job["cgroup"]["inode"] += 1
    sandbox.save(job)
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    assert (path / "cgroup.kill").read_text() == ""


def test_cancel_persists_spawn_fence_and_waits_for_all_descendants(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    result = sandbox.cancel(JOB)
    assert result["state"] == "cancelled" and result["stop_confirmed"]
    assert (sandbox.root / JOB / "cancelled").exists()
    assert (path / "cgroup.kill").read_text() == "1"
    assert sandbox.cancel(JOB)["state"] == "cancelled"


def test_populated_cgroup_keeps_stop_unconfirmed_after_kill(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    (path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    now = [0]
    def clock():
        now[0] += 6
        return now[0]
    monkeypatch.setattr(launcher.time, "monotonic", clock)
    result = sandbox.cancel(JOB)
    assert result["state"] == "stop_unconfirmed" and not result["stop_confirmed"]
    with pytest.raises(SandboxError, match="export"):
        sandbox.artifacts(JOB)


def test_zero_exit_requires_matching_root_receipt_and_empty_cgroup(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    completion = sandbox.root / JOB / "completion.json"
    completion.write_text("{}")
    value = {"runtime": IDENTITY, "cgroup": job["cgroup"], "stop_confirmed": True, "exit_code": 0, "reason": None}
    monkeypatch.setattr(launcher, "private_json", lambda path: value)
    assert sandbox.status(JOB)["state"] == "succeeded"
    (path / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    assert sandbox.status(JOB)["state"] == "stop_unconfirmed"


def test_missing_supervisor_receipt_never_assumes_completion(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "process_identity", lambda pid: None)
    assert sandbox.status(JOB)["state"] == "stop_unconfirmed"


def test_no_arbitrary_mount_parameters_or_loop_fallback(tmp_path):
    sandbox = HomeSandbox(tmp_path, None, {})
    with pytest.raises(SandboxError, match="Unbound"):
        sandbox._execute("mount", "-o", "loop", "/private", "/config")
    sandbox.preparing = {"job_id": JOB}
    with pytest.raises(SandboxError, match="Unknown"):
        sandbox._execute("mount", "-o", "loop", "/private", "/config")


@pytest.mark.parametrize("fstype", ["fuse.ext4", "fuse", "fuse.fuse2fs", "ext4"])
def test_pinned_fuse2fs_mount_subtype_is_exact(tmp_path, monkeypatch, fstype):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    directory = sandbox.root / JOB
    read_fd, gate = os.pipe()
    process = SimpleNamespace(pid=42, poll=lambda: None)
    monkeypatch.setattr(home_sandbox, "_PROCESSES", {})
    monkeypatch.setattr(launcher, "create_group", lambda *args, **kwargs: job["cgroup"])
    monkeypatch.setattr(launcher, "start_gated", lambda *args: (process, gate, {"pid": 42, "start": 1, "uid": 0}))
    monkeypatch.setattr(os.path, "ismount", lambda path: True)
    sandbox.native_execute = lambda *args, **kwargs: SimpleNamespace(stdout=fstype + "\n")
    sandbox.preparing = job
    try:
        command = ("mount", "-o", "loop,nodev,nosuid", str(directory / "workspace.ext4"), str(directory / "workspace"))
        if fstype == "fuse.ext4":
            sandbox._execute(*command)
        else:
            with pytest.raises(launcher.BoundaryError, match="home_fuse_mount_identity_invalid"):
                sandbox._execute(*command)
        assert os.read(read_fd, 1) == b"1"
    finally:
        os.close(read_fd)


def test_launch_preserves_original_command_failure_and_private_diagnostic(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    sandbox = HomeSandbox(root, None, {"agentic-home": tmp_path})
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    original = SandboxError("fixture-private-command", code="command_failed", operation="git.checkout", exit_code=128)
    def failed_prepare(job, program):
        (root / job["job_id"]).mkdir()
        raise original
    monkeypatch.setattr(sandbox, "prepare", failed_prepare)
    with pytest.raises(SandboxError) as caught:
        sandbox.launch(JOB, 1, "agentic-home", "a" * 40, "pass")
    assert caught.value is original
    receipt = json.loads((root / JOB / "launch-error.json").read_text())
    assert receipt == {"mode": "prepare", "error_type": "SandboxError", "error_code": "command_failed",
                       "operation": "git.checkout", "command_exit_code": 128, "errno": None}
    assert sandbox.record(JOB)["state"] == "stop_unconfirmed"
    assert "fixture-private-command" not in json.dumps(receipt)


def test_partial_prepare_fuse_allocation_is_disposed_before_directory_removal(tmp_path, monkeypatch):
    path, group = group_fixture(tmp_path, monkeypatch, populated=1)
    fuse_path = path.with_name("f-" + "a" * 32)
    path.rename(fuse_path)
    group.update(name=fuse_path.name, inode=fuse_path.stat().st_ino)
    root = tmp_path / "work"
    root.mkdir()
    sandbox = HomeSandbox(root, None, {"agentic-home": tmp_path})
    monkeypatch.setattr(sandbox, "_require_work_root", lambda: None)
    monkeypatch.setattr(sandbox, "preflight", lambda: True)
    mounted = [True]
    events = []

    def failed_prepare(job, program):
        workspace = root / job["job_id"] / "workspace"
        for relative in ("source/.git", "profile", "scratch", "artifacts"):
            (workspace / relative).mkdir(parents=True)
        (workspace / "artifacts/experiment.txt").write_text("Preparation is not worker execution evidence")
        job["fuse_cgroup"] = group
        sandbox.save(job)
        raise SandboxError("fixture checkout failure", code="command_failed", operation="git.checkout", exit_code=128)

    monkeypatch.setattr(sandbox, "prepare", failed_prepare)
    monkeypatch.setattr(os.path, "ismount", lambda value: value == root / JOB / "workspace" and mounted[0])

    def unmount(*args, **kwargs):
        assert args == ("umount", str(root / JOB / "workspace"))
        assert (root / JOB / "cancelled").exists()
        assert sandbox.record(JOB)["cleanup_fenced"] is True
        events.append("unmount")
        mounted[0] = False

    sandbox.native_execute = unmount
    kill_group = launcher.kill_group

    def stop_fuse(value):
        assert value == group and not mounted[0]
        events.append("stop_fuse")
        (fuse_path / "cgroup.events").write_text("populated 0\nfrozen 0\n")
        return kill_group(value)

    monkeypatch.setattr(launcher, "kill_group", stop_fuse)
    real_rmdir = Path.rmdir

    def remove_group(value):
        assert value == fuse_path
        assert (fuse_path / "cgroup.kill").read_text() == "1"
        assert sandbox.record(JOB)["removing_groups"] == ["fuse_cgroup"]
        events.append("remove_group")
        for child in fuse_path.iterdir():
            child.unlink()
        real_rmdir(value)

    monkeypatch.setattr(Path, "rmdir", remove_group)
    with pytest.raises(SandboxError, match="checkout failure"):
        sandbox.launch(JOB, 1, "agentic-home", "a" * 40, "pass")
    assert sandbox.record(JOB)["state"] == "stop_unconfirmed"
    assert "cgroup" not in sandbox.record(JOB) and "launcher" not in sandbox.record(JOB)
    assert sandbox.cancel(JOB)["stop_confirmed"] is True
    with pytest.raises(SandboxError, match="worker cgroup unavailable for artifact export"):
        sandbox.artifacts(JOB)
    assert (root / JOB / "workspace/artifacts/experiment.txt").is_file()
    result = sandbox.dispose(JOB)
    assert result["state"] == "disposed" and result["stop_confirmed"] is True
    assert events == ["unmount", "stop_fuse", "remove_group"]
    assert not mounted[0] and not fuse_path.exists()
    assert (root / JOB).is_dir()  # Only the service may now remove this unmounted directory.
    assert sandbox.dispose(JOB)["state"] == "disposed"
    assert len(events) == 3


def test_launch_diagnostic_rejects_untyped_exception_fields():
    error = SandboxError("fixture-secret")
    error.code = "fixture-secret"
    error.diagnostics = {"operation": "fixture-secret", "command_exit_code": "fixture-secret",
                         "timeout_seconds": True, "token": "fixture-secret"}
    receipt = launch_failure(error)
    assert receipt == {"mode": "prepare", "error_type": "SandboxError", "error_code": "sandbox_rejected", "errno": None}


def test_uninitialized_runtime_never_runs_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    sandbox = HomeSandbox(tmp_path / "work", None, {"agentic-home": tmp_path})
    with pytest.raises(SandboxError, match="Home boundary"):
        sandbox.launch(JOB, 1, "agentic-home", "a" * 40, "print('must not run')")
    assert not (tmp_path / "work" / JOB).exists()


def test_shared_input_and_limits_guards_remain_in_force(tmp_path):
    with pytest.raises(SandboxError, match="verifier input"):
        HomeSandbox(tmp_path, None, {}, inputs={"owner-token": b"private"})
    with pytest.raises(SandboxError, match="limit"):
        Limits(disk_mib=0)


def test_normal_exit_always_kills_remaining_cgroup_descendants(tmp_path, monkeypatch):
    _, group = group_fixture(tmp_path, monkeypatch)
    work = tmp_path / "work" / JOB
    work.mkdir(parents=True)
    config = {"cgroup": group, "wall_seconds": 10}
    monkeypatch.setattr(launcher, "private_json", lambda path: config)
    child_read, child_write = os.pipe()
    child = SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    monkeypatch.setattr(launcher, "start_gated", lambda *args: (child, child_write, {"pid": 44, "start": 77, "uid": 0}))
    killed = []
    monkeypatch.setattr(launcher, "kill_group", lambda actual: killed.append(actual) or {"cgroup.events": {"populated": 0}})
    try:
        launcher.supervise(work)
        assert os.read(child_read, 1) == b"1"
    finally:
        os.close(child_read)
    receipt = json.loads((work / "completion.json").read_text())
    assert killed == [group] and receipt["stop_confirmed"] and receipt["exit_code"] == 0


def test_cancel_marker_prevents_even_a_delayed_launcher_from_spawning(tmp_path, monkeypatch):
    _, group = group_fixture(tmp_path, monkeypatch)
    work = tmp_path / "work" / JOB
    work.mkdir(parents=True)
    (work / "cancelled").touch()
    monkeypatch.setattr(launcher, "private_json", lambda path: {"cgroup": group, "wall_seconds": 10})
    monkeypatch.setattr(launcher, "start_gated", lambda *args: pytest.fail("Cancel must fence future spawn"))
    launcher.supervise(work)
    assert json.loads((work / "completion.json").read_text())["stop_confirmed"] is True


def test_pid_reuse_never_signals_another_root_process(monkeypatch):
    closed, signalled = [], []
    monkeypatch.setattr(launcher, "pidfd_open", lambda *args: 90)
    monkeypatch.setattr(launcher.os, "close", closed.append)
    monkeypatch.setattr(launcher, "process_identity", lambda pid: {"pid": pid, "start": 999, "uid": 0})
    monkeypatch.setattr(launcher, "pidfd_kill", lambda *args: signalled.append(args))
    launcher.stop_trusted_process({"pid": 25, "start": 100, "uid": 0})
    assert closed == [90] and signalled == []


def test_actual_pidfd_waits_for_owned_process_termination():
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        expected = launcher.process_identity(child.pid)
        launcher.stop_trusted_process(expected)
        assert child.wait(timeout=2) == -9
        launcher.stop_trusted_process(expected)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


@pytest.mark.parametrize("error", [ValueError("fixture-private-token"), OSError(errno.ENOSPC, "fixture-private-path"),
                                  launcher.BoundaryError("fixture-private-token")])
def test_launcher_error_receipts_never_copy_exception_text(error):
    result = launcher.error_receipt("execute", error)
    assert result["mode"] == "execute" and result["error_code"] == "home_launcher_failure"
    assert "fixture-private" not in json.dumps(result)
    assert result["errno"] == (errno.ENOSPC if isinstance(error, OSError) else None)


def test_error_receipt_retains_only_known_boundary_code():
    assert launcher.error_receipt("supervise", launcher.BoundaryError("home_writer_stop_unconfirmed"))["error_code"] == "home_writer_stop_unconfirmed"


def test_atomic_record_recovers_without_trusting_or_deleting_crash_temporaries(tmp_path):
    target = tmp_path / "runtime.json"
    target.write_text('{"runtime":"previous-boot"}')
    stale = [tmp_path / "runtime.json.new", tmp_path / ".runtime.json.crash.tmp"]
    for path in stale:
        path.write_text('{"stop_confirmed":true')
    value = {"runtime": "current-boot", "stop_confirmed": False}
    launcher.persist(target, value)
    assert json.loads(target.read_text()) == value
    assert target.stat().st_mode & 0o777 == 0o600
    assert all(path.read_text() == '{"stop_confirmed":true' for path in stale)
    assert set(tmp_path.iterdir()) == {target, *stale}


def test_failed_atomic_replace_preserves_original_and_cleans_only_own_temp(tmp_path, monkeypatch):
    target = tmp_path / "completion.json"
    original = '{"stop_confirmed":false}'
    target.write_text(original)
    stale = tmp_path / ".completion.json.crash.tmp"
    stale.write_text("private partial receipt")
    seen = []
    def fail_replace(source, destination):
        seen.append(Path(source))
        assert destination == target and Path(source) != stale
        assert Path(source).stat().st_mode & 0o777 == 0o600
        raise OSError(errno.ENOSPC, "fixture")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError) as error:
        launcher.persist(target, {"stop_confirmed": True})
    assert error.value.errno == errno.ENOSPC and seen
    assert target.read_text() == original and stale.read_text() == "private partial receipt"
    assert set(tmp_path.iterdir()) == {target, stale}


def test_partial_cleanup_stops_watchdog_before_unmount_and_resumes(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    events = []
    mounted = [True]
    monkeypatch.setattr(launcher, "stop_trusted_process", lambda value: events.append("stop_watchdog"))
    monkeypatch.setattr(os.path, "ismount", lambda value: mounted[0])
    def unmount(*args, **kwargs):
        events.append("unmount")
        if events.count("unmount") == 1:
            raise OSError(errno.EBUSY, "fixture")
        mounted[0] = False
    sandbox.native_execute = unmount
    real_rmdir = Path.rmdir
    def rmdir(value):
        assert value == path
        events.append("remove_group")
        for child in path.iterdir():
            child.unlink()
        real_rmdir(value)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    with pytest.raises(OSError):
        sandbox.dispose(JOB)
    assert sandbox.status(JOB)["stop_confirmed"] is True
    with pytest.raises(SandboxError, match="cleanup"):
        sandbox.artifacts(JOB)
    assert sandbox.dispose(JOB)["state"] == "disposed"
    assert events == ["stop_watchdog", "unmount", "unmount", "remove_group"]
    assert sandbox.dispose(JOB)["state"] == "disposed"


def test_removed_group_reappearance_is_not_safe_cleanup(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    job.update(state="cancelled", stop_confirmed=True, cleanup_fenced=True, removed_groups=["cgroup"])
    sandbox.save(job)
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    assert (path / "cgroup.kill").read_text() == ""


def test_crash_after_cgroup_removal_resumes_from_persisted_empty_group_intent(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "stop_trusted_process", lambda value: None)
    monkeypatch.setattr(os.path, "ismount", lambda value: False)
    real_rmdir = Path.rmdir
    def rmdir(value):
        assert value == path
        durable = sandbox.record(JOB)
        assert durable["cleanup_fenced"] and durable["stop_confirmed"]
        assert durable["removing_groups"] == ["cgroup"]
        assert (path / "cgroup.events").read_text().startswith("populated 0")
        for child in path.iterdir():
            child.unlink()
        real_rmdir(value)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    save = sandbox.save
    failed = []
    def save_after_rmdir(value):
        if value.get("removed_groups") == ["cgroup"] and not failed:
            failed.append(True)
            raise OSError(errno.ENOSPC, "fixture crash window")
        return save(value)
    monkeypatch.setattr(sandbox, "save", save_after_rmdir)
    with pytest.raises(OSError):
        sandbox.dispose(JOB)
    assert not path.exists()
    durable = sandbox.record(JOB)
    assert durable["removing_groups"] == ["cgroup"] and not durable.get("removed_groups")
    assert sandbox.cancel(JOB)["stop_confirmed"] is True
    assert sandbox.status(JOB)["stop_confirmed"] is True
    assert sandbox.dispose(JOB)["state"] == "disposed"
    assert sandbox.record(JOB)["removed_groups"] == ["cgroup"]


def test_pending_group_removal_without_cleanup_stop_proof_stays_quarantined(tmp_path, monkeypatch):
    sandbox, job, path = sandbox_fixture(tmp_path, monkeypatch)
    job.update(removing_groups=["cgroup"], stop_confirmed=True)
    sandbox.save(job)
    assert sandbox.cancel(JOB)["state"] == "stop_unconfirmed"
    assert (path / "cgroup.kill").read_text() == ""


def test_actual_filter_blocks_escape_syscalls_but_allows_threads_and_loopback(tmp_path):
    # Generate/load only in a disposable ordinary subprocess. No mount, privilege
    # change, external connection, container or HAOS fixture is performed here.
    path = tmp_path / "seccomp.bpf"
    launcher.seccomp_file(path)
    assert 0 < path.stat().st_size < 65536 and path.stat().st_size % 8 == 0
    script = r'''
import ctypes,errno,json,os,socket,subprocess,sys,threading
class Filter(ctypes.Structure):_fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint32)]
class Program(ctypes.Structure):_fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
data=open(sys.argv[1],'rb').read();rules=(Filter*(len(data)//8)).from_buffer_copy(data)
program=Program(len(rules),rules);libc=ctypes.CDLL(None,use_errno=True)
assert libc.prctl(38,1,0,0,0)==0
assert libc.prctl(22,2,ctypes.byref(program),0,0)==0
result={}
for name,args in [('clone3',(435,0,0)),('unshare',(272,0x10000000)),('setns',(308,-1,0))]:
 result[name]=libc.syscall(*args);result[name+'_errno']=ctypes.get_errno()
pid=libc.syscall(56,0x10000000|17,0,0,0,0)
if pid==0:os._exit(100)
if pid>0:os.waitpid(pid,0)
result['clone_userns']=pid;result['clone_userns_errno']=ctypes.get_errno()
result['nnp']=libc.prctl(39,0,0,0,0)
result['nnp_unset']=libc.prctl(38,0,0,0,0);result['nnp_unset_errno']=ctypes.get_errno()
child=subprocess.run([sys.executable,'-I','-c',"import ctypes,json; lib=ctypes.CDLL(None,use_errno=True); value=lib.unshare(0x10000000); print(json.dumps({'unshare':value,'errno':ctypes.get_errno(),'nnp':lib.prctl(39,0,0,0,0)}))"],capture_output=True,text=True,timeout=5)
assert child.returncode==0
result['exec_child']=json.loads(child.stdout)
result['thread']=False
def worker():result['thread']=True
t=threading.Thread(target=worker);t.start();t.join()
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.close();result['inet_socket']=True
try:socket.socket(40,socket.SOCK_STREAM)
except OSError as e:result['vsock_errno']=e.errno
print(json.dumps(result))
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, str(path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {"clone3": -1, "clone3_errno": errno.ENOSYS, "unshare": -1, "unshare_errno": errno.EPERM,
                     "setns": -1, "setns_errno": errno.EPERM, "clone_userns": -1, "clone_userns_errno": errno.EPERM,
                     "nnp": 1, "nnp_unset": -1, "nnp_unset_errno": errno.EINVAL,
                     "exec_child": {"unshare": -1, "errno": errno.EPERM, "nnp": 1},
                     "thread": True, "inet_socket": True, "vsock_errno": errno.EPERM}
