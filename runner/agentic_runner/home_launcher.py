"""Trusted HAOS-only cgroup, FUSE and namespace launcher; never a shell API.

This file is also executed with ``python -I`` by the broker. Only fixed modes
and private root-owned records cross that boundary. Candidate code is never
executed before its cgroup, namespace and credential restrictions are active.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import resource
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path("/data/coding")
UID = 10000
STAGING = Path("/opt/agentic-worker/staging")
SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1"}
_CONTEXT = None


class BoundaryError(RuntimeError):
    pass


ERROR_CODES = frozenset({
    "home_launcher_failure", "home_launcher_identity_unavailable", "home_launcher_stop_unconfirmed",
    "home_launcher_mode_invalid", "home_launcher_path_invalid", "home_private_record_invalid",
    "home_private_root_required", "home_private_ancestor_required", "home_worker_not_in_cgroup",
    "home_cgroup_identity_changed", "home_cgroup_name_invalid", "home_cgroup_replaced",
    "home_writer_stop_unconfirmed", "home_relay_socket_required", "home_spawn_fence_unavailable",
    "home_prctl_unavailable", "home_pidfd_open_failed", "home_pidfd_signal_failed",
    "home_fuse_mount_unavailable", "home_fuse_mount_identity_invalid",
    "home_fuse_device_unavailable",
    "home_staging_path_invalid", "home_staging_target_busy",
    "home_private_mount_namespace_required", "home_private_pid_namespace_required",
    "home_private_proc_unconfirmed", "home_namespace_child_wait_invalid",
    "home_pid_namespace_unavailable", "home_pid_namespace_fork_unavailable", "home_private_proc_mount_unavailable",
})


def error_receipt(mode, exc):
    code = str(exc) if isinstance(exc, BoundaryError) else None
    return {"mode": mode if mode in {"execute", "supervise", "fuse"} else "unknown",
            "error_code": code if code in ERROR_CODES else "home_launcher_failure",
            "error_type": "BoundaryError" if isinstance(exc, BoundaryError) else "OSError" if isinstance(exc, OSError) else "Exception",
            "errno": exc.errno if isinstance(exc, OSError) and type(exc.errno) is int and 0 < exc.errno < 4096 else None}


def record_error(directory, mode, exc):
    # Diagnostic failure must never replace fencing or the original failure.
    try:
        directory = Path(directory)
        if (os.geteuid() != 0 or directory.parent != ROOT / "work" or directory.resolve() != directory or
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", directory.name)):
            return
        require_private(directory)
        persist(directory / "launch-error.json", error_receipt(mode, exc))
    except Exception:
        pass


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077 or metadata.st_size > 65536:
            raise BoundaryError("home_private_record_invalid")
        with os.fdopen(fd, "r", closefd=False) as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise BoundaryError("home_private_record_invalid")
        return value
    finally:
        os.close(fd)


def persist(path, value):
    path = Path(path)
    # A crash may strand this private temporary, but must never make future
    # writes depend on deleting or trusting another invocation's partial file.
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def identity():
    return {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            **{name + "_ns": os.stat("/proc/self/ns/" + name).st_ino for name in ("pid", "cgroup", "mnt")}}


def process_identity(pid):
    try:
        path = Path("/proc") / str(pid)
        fields = (path / "stat").read_text().rsplit(") ", 1)[1].split()
        return {"pid": pid, "start": int(fields[19]), "uid": path.stat().st_uid}
    except (FileNotFoundError, ProcessLookupError):
        return None


def pidfd_open(pid):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes, libc.pidfd_open.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    fd = libc.pidfd_open(pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "home_pidfd_open_failed")
    return fd


def pidfd_kill(fd):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    libc.pidfd_send_signal.restype = ctypes.c_int
    if libc.pidfd_send_signal(fd, signal.SIGKILL, None, 0) != 0:
        raise OSError(ctypes.get_errno(), "home_pidfd_signal_failed")


def stop_trusted_process(expected):
    """Only after its worker cgroup is empty; never signal a recycled PID."""
    try:
        fd = pidfd_open(expected["pid"])
    except ProcessLookupError:
        return
    try:
        if process_identity(expected["pid"]) != expected:
            return
        pidfd_kill(fd)
        poll = select.poll()
        poll.register(fd, select.POLLIN)
        if not any(flags & select.POLLIN for _, flags in poll.poll(5000)):
            raise BoundaryError("home_launcher_stop_unconfirmed")
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def require_private(path):
    path = Path(path)
    if path.resolve() != path or path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
        raise BoundaryError("home_private_root_required")
    for parent in path.parents:
        if parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
            raise BoundaryError("home_private_ancestor_required")


def syscall(name, *args):
    libc = ctypes.CDLL(None, use_errno=True)
    if getattr(libc, name)(*args) != 0:
        raise BoundaryError("home_" + name + "_unavailable")


def validate_app_cgroup(root, membership):
    # Global cgroup-v2 root has no memory.max/cpu.max/pids.max. Together with
    # 0::/ this rejects a host cgroup mount masquerading as App delegation.
    if membership != "0::/" or not all((root / name).is_file() for name in
                                      ("memory.max", "memory.swap.max", "pids.max", "cpu.max")):
        raise BoundaryError("home_private_app_cgroup_required")
    if not {"memory", "pids", "cpu"} <= set((root / "cgroup.controllers").read_text().split()):
        raise BoundaryError("home_cgroup_controllers_unavailable")


def validate_fuse_device():
    # A visible character node does not imply the App's device cgroup grants
    # access. Opening then immediately closing creates no filesystem mount.
    try:
        if not stat.S_ISCHR(Path("/dev/fuse").stat().st_mode):
            raise BoundaryError("home_fuse_device_required")
        descriptor = os.open("/dev/fuse", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        os.close(descriptor)
    except OSError as exc:
        raise BoundaryError("home_fuse_device_unavailable") from exc


def initialize_home_runtime(root=ROOT):
    """Call once, synchronously BEFORE Runner threads, in the actual HAOS App."""
    global _CONTEXT
    root = Path(root)
    if root != ROOT or os.geteuid() != 0 or os.environ.get("AGENTIC_HOST_MODE") == "1":
        raise BoundaryError("home_app_root_required")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private(root)
    try:
        _CONTEXT = runtime_context()
        return _CONTEXT.copy()
    except (OSError, ValueError, BoundaryError):
        pass
    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise BoundaryError("home_init_before_threads_required")
    validate_app_cgroup(Path("/sys/fs/cgroup"), Path("/proc/self/cgroup").read_text().strip())
    validate_fuse_device()
    syscall("unshare", ctypes.c_int(0x00020000))  # CLONE_NEWNS; never the host mount namespace.
    syscall("mount", None, b"/", None, ctypes.c_ulong(0x4000 | 0x40000), None)  # REC|PRIVATE
    mount = root / "cgroups"
    mount.mkdir(mode=0o700, exist_ok=True)
    syscall("mount", b"none", os.fsencode(mount), b"cgroup2", ctypes.c_ulong(2 | 4 | 8), None)
    validate_app_cgroup(mount, Path("/proc/self/cgroup").read_text().strip())
    broker = mount / "broker"
    broker.mkdir(exist_ok=True)
    # There must be no internal processes before enabling domain controllers.
    for pid in (mount / "cgroup.procs").read_text().split():
        (broker / "cgroup.procs").write_text(pid)
    (mount / "cgroup.subtree_control").write_text("+memory +pids +cpu")
    if not {"memory", "pids", "cpu"} <= set((mount / "cgroup.subtree_control").read_text().split()):
        raise BoundaryError("home_cgroup_delegation_unavailable")
    context = {"contract": 1, "runtime": identity(), "cgroup_root_inode": mount.stat().st_ino}
    persist(root / "runtime.json", context)
    _CONTEXT = context
    return context.copy()


def runtime_context():
    value = private_json(ROOT / "runtime.json")
    if (value.get("contract") != 1 or value.get("runtime") != identity() or
            value.get("cgroup_root_inode") != (ROOT / "cgroups").stat().st_ino or
            Path("/proc/self/cgroup").read_text().strip() != "0::/broker"):
        raise BoundaryError("home_runtime_identity_changed")
    return value


def runtime_observation():
    context = runtime_context()
    return {"observed_from": "haos_app", "boundary_contract": 1,
            "namespace_ids": {k: v for k, v in context["runtime"].items() if k.endswith("_ns")},
            "private_cgroup_root": True,
            "controllers": sorted((ROOT / "cgroups/cgroup.subtree_control").read_text().split()),
            "worker_network": "private_namespace_relay_only"}


def create_group(name, *, memory_mib, pids, cpus):
    context = runtime_context()
    if not re.fullmatch(r"[wf]-[a-f0-9]{32}", name):
        raise BoundaryError("home_cgroup_name_invalid")
    path = ROOT / "cgroups" / name
    path.mkdir()
    values = {"memory.max": str(memory_mib * 1024**2), "memory.swap.max": "0", "memory.oom.group": "1",
              "pids.max": str(pids), "cpu.max": str(cpus * 100000) + " 100000"}
    for key, value in values.items():
        (path / key).write_text(value)
        if (path / key).read_text().strip() != value:
            raise BoundaryError("home_cgroup_limit_unconfirmed")
    if not (path / "cgroup.kill").exists():
        raise BoundaryError("home_cgroup_kill_unavailable")
    return {"name": name, "inode": path.stat().st_ino, "runtime": context["runtime"], "limits": values}


def group_path(group):
    if group.get("runtime") != identity() or not re.fullmatch(r"[wf]-[a-f0-9]{32}", str(group.get("name", ""))):
        raise BoundaryError("home_cgroup_identity_changed")
    path = ROOT / "cgroups" / group["name"]
    if path.is_symlink() or path.stat().st_ino != group.get("inode"):
        raise BoundaryError("home_cgroup_replaced")
    return path


def counters(path):
    result = {}
    for name in ("memory.events", "pids.events", "cpu.stat", "cgroup.events"):
        result[name] = {key: int(value) for line in (path / name).read_text().splitlines()
                        for key, value in [line.split()]}
    return result


def empty(group):
    return counters(group_path(group))["cgroup.events"].get("populated") == 0


def kill_group(group, timeout=5):
    path = group_path(group)
    (path / "cgroup.kill").write_text("1")
    deadline = time.monotonic() + timeout
    while not empty(group):
        if time.monotonic() >= deadline:
            raise BoundaryError("home_writer_stop_unconfirmed")
        time.sleep(.025)
    return counters(path)


@contextmanager
def spawn_lock(directory):
    descriptor = os.open(directory / "spawn.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BoundaryError("home_spawn_fence_unavailable") from None
                time.sleep(.025)
        yield
    finally:
        os.close(descriptor)


def cancel_writer(directory, group):
    with spawn_lock(directory):
        descriptor = os.open(directory / "cancelled", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        return kill_group(group)


def seccomp_file(path):
    """Bounded native filter loaded by bwrap AFTER its privileged setup."""
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    class Compare(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
                                              ctypes.POINTER(Compare)]
    context = library.seccomp_init(0x7fff0000)
    if not context:
        raise BoundaryError("home_seccomp_unavailable")
    def deny(name, comparison=None, error=errno.EPERM):
        number = library.seccomp_syscall_resolve_name(name.encode())
        if number < 0:
            raise BoundaryError("home_seccomp_syscall_unavailable")
        values = (Compare * 1)(comparison) if comparison is not None else None
        if library.seccomp_rule_add_array(context, 0x50000 | error, number, int(comparison is not None), values):
            raise BoundaryError("home_seccomp_rule_unavailable")
    try:
        for name in ("unshare", "setns", "mount", "umount2", "pivot_root", "chroot", "fsopen", "fsconfig", "fsmount",
                     "move_mount", "open_tree", "mount_setattr", "ptrace", "process_vm_readv", "process_vm_writev",
                     "bpf", "perf_event_open", "keyctl", "add_key", "request_key", "kexec_load", "init_module",
                     "finit_module", "delete_module", "reboot", "swapon", "swapoff", "open_by_handle_at", "userfaultfd"):
            deny(name)
        deny("clone3", error=errno.ENOSYS)
        for flag in (0x20000, 0x4000000, 0x8000000, 0x10000000, 0x20000000, 0x40000000, 0x2000000):
            deny("clone", Compare(0, 7, flag, flag))  # MASKED_EQ
        for request in (0x5412, 0x541c):
            deny("ioctl", Compare(1, 4, request, 0))  # EQ; TIOCSTI / TIOCLINUX
        for family in (16, 17, 40):
            deny("socket", Compare(0, 4, family, 0))  # netlink, packet, vsock
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            if library.seccomp_export_bpf(context, fd):
                raise BoundaryError("home_seccomp_export_unavailable")
        finally:
            os.close(fd)
    finally:
        library.seccomp_release(context)


def stage_workspace(workspace, relay=None):
    """Give unprivileged bwrap realpath-safe aliases only in this child namespace.

    Never make /data traversable. Aliases are immutable image placeholders;
    neither another App process nor the eventual model receives an outside FD.
    """
    targets = (STAGING, STAGING / "workspace", STAGING / "relay.sock")
    for target in targets:
        metadata = target.lstat()
        expected = stat.S_ISREG if target.name == "relay.sock" else stat.S_ISDIR
        if (target.resolve() != target or not expected(metadata.st_mode) or metadata.st_uid != 0 or
                metadata.st_mode & 0o022):
            raise BoundaryError("home_staging_path_invalid")
        if os.path.ismount(target):
            raise BoundaryError("home_staging_target_busy")
    if workspace.is_symlink() or not os.path.ismount(workspace):
        raise BoundaryError("home_staging_path_invalid")
    if relay is not None:
        relay = Path(relay)
        metadata = relay.lstat()
        if (relay.parent != ROOT / "relays" or not stat.S_ISSOCK(metadata.st_mode) or
                metadata.st_uid != UID or relay.is_symlink()):
            raise BoundaryError("home_relay_socket_required")
    syscall("unshare", ctypes.c_int(0x00020000))
    syscall("mount", None, b"/", None, ctypes.c_ulong(0x4000 | 0x40000), None)
    syscall("mount", os.fsencode(workspace), os.fsencode(STAGING / "workspace"), None, ctypes.c_ulong(4096), None)
    if relay is not None:
        syscall("mount", os.fsencode(relay), os.fsencode(STAGING / "relay.sock"), None, ctypes.c_ulong(4096), None)


def inherit_filter_only(filter_fd):
    # bwrap 0.8 closes its consumed seccomp FD, but preserves unrelated FDs for
    # the candidate. Explicitly close every other non-stdio descriptor here.
    descriptors = [int(path.name) for path in Path("/proc/self/fd").iterdir() if path.name.isdecimal()]
    for fd in descriptors:
        if fd > 2 and fd != filter_fd:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise
    os.set_inheritable(filter_fd, True)


def wait_namespace_child(pid):
    """Trusted parent preserves normal exits and fatal signals exactly."""
    while True:
        try:
            found, status = os.waitpid(pid, 0)
            break
        except InterruptedError:
            continue
    if found != pid:
        raise BoundaryError("home_namespace_child_wait_invalid")
    if os.WIFEXITED(status):
        os._exit(os.WEXITSTATUS(status))
    if os.WIFSIGNALED(status):
        fatal = os.WTERMSIG(status)
        if fatal not in {signal.SIGKILL, signal.SIGSTOP}:
            signal.signal(fatal, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {fatal})
        os.kill(os.getpid(), fatal)
        os._exit(128 + fatal)  # Defensive fallback if a signal was blocked.
    raise BoundaryError("home_namespace_child_wait_invalid")


def private_pid_proc(app_mount_namespace):
    """Mount fresh proc for a new PID namespace, never the App's parent PIDs.

    Docker masks parts of the App's proc mount. Unprivileged bwrap cannot make
    a new proc mount from that view. The trusted root child first creates a
    complete view of its OWN fresh PID namespace inside the already private
    mount namespace, then bwrap creates its final namespaces and read-only proc.
    """
    if (os.geteuid() != 0 or type(app_mount_namespace) is not int or
            os.stat("/proc/self/ns/mnt").st_ino == app_mount_namespace):
        raise BoundaryError("home_private_mount_namespace_required")
    original = os.stat("/proc/self/ns/pid").st_ino
    try:
        syscall("unshare", ctypes.c_int(0x20000000))  # CLONE_NEWPID applies to children.
    except BoundaryError as exc:
        raise BoundaryError("home_pid_namespace_unavailable") from exc
    try:
        child = os.fork()
    except OSError as exc:
        raise BoundaryError("home_pid_namespace_fork_unavailable") from exc
    if child:
        wait_namespace_child(child)  # Does not return or run candidate code.
        raise BoundaryError("home_namespace_child_wait_invalid")
    current = os.stat("/proc/self/ns/pid").st_ino
    if os.getpid() != 1 or current == original:
        raise BoundaryError("home_private_pid_namespace_required")
    try:
        syscall("mount", b"proc", b"/proc", b"proc", ctypes.c_ulong(2 | 4 | 8), None)
    except BoundaryError as exc:
        raise BoundaryError("home_private_proc_mount_unavailable") from exc
    if (os.readlink("/proc/self") != "1" or os.stat("/proc/1/ns/pid").st_ino != current or
            {path.name for path in Path("/proc").iterdir() if path.name.isdecimal()} != {"1"}):
        raise BoundaryError("home_private_proc_unconfirmed")


def bubblewrap_command(filter_fd, *, relay=False):
    work = str(STAGING / "workspace")
    # Mandatory inherited seccomp blocks all further namespace creation/join.
    # Do not ask bwrap to write the App's read-only /proc/sys userns controls.
    args = ["/usr/bin/bwrap", "--unshare-user", "--uid", str(UID), "--gid", str(UID),
            "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/opt/agents", "/opt/agents",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--dir", "/app/agentic_runner", "--ro-bind", "/app/agentic_runner/codex-models.json", "/app/agentic_runner/codex-models.json",
            "--proc", "/proc", "--remount-ro", "/proc", "--dev", "/dev", "--remount-ro", "/dev",
            "--bind", work, "/work", "--bind", work + "/scratch", "/tmp",
            "--ro-bind", work + "/source/.git", "/work/source/.git"]
    if relay:
        args += ["--ro-bind", str(STAGING / "relay.sock"), "/relay.sock"]
    args += ["--remount-ro", "/", "--chdir", "/work/source", "--seccomp", str(filter_fd)]
    for key, value in {**SAFE_ENV, "HOME": "/work/profile", "TMPDIR": "/work/scratch",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        args += ["--setenv", key, value]
    return [*args, "--", "/usr/local/bin/python", "-I", "/work/task.py"]


def start_gated(mode, directory, group=None):
    """A child cannot exec anything until its exact identity is persisted."""
    read_fd, write_fd = os.pipe()
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), mode, str(directory), str(read_fd)],
                                   pass_fds=(read_fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=SAFE_ENV, start_new_session=True)
        if group is not None:
            (group_path(group) / "cgroup.procs").write_text(str(process.pid))
        found = process_identity(process.pid)
        if found is None or found["uid"] != 0:
            raise BoundaryError("home_launcher_identity_unavailable")
        return process, write_fd, found
    except Exception:
        os.close(write_fd)  # EOF fences a child not yet released.
        if process is not None:
            process.wait(timeout=5)
        raise
    finally:
        os.close(read_fd)


def execute_worker(directory):
    config = private_json(directory / "launch.json")
    path = group_path(config["cgroup"])
    if str(os.getpid()) not in (path / "cgroup.procs").read_text().split():
        raise BoundaryError("home_worker_not_in_cgroup")
    workspace = directory / "workspace"
    filter_fd = os.open(directory / "seccomp.bpf", os.O_RDONLY | os.O_NOFOLLOW)
    stage_workspace(workspace, config.get("relay"))
    private_pid_proc(config["cgroup"]["runtime"]["mnt_ns"])
    args = bubblewrap_command(filter_fd, relay=bool(config.get("relay")))
    for target, name in ((1, "stdout.txt"), (2, "stderr.txt")):
        fd = os.open(workspace / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.dup2(fd, target)
        os.close(fd)
    inherit_filter_only(filter_fd)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024**2, 16 * 1024**2))
    syscall("prctl", ctypes.c_int(38), ctypes.c_ulong(1), 0, 0, 0)  # NO_NEW_PRIVS
    os.setgroups([])
    os.setgid(UID)
    os.setuid(UID)
    os.execve(args[0], args, SAFE_ENV)


def supervise(directory):
    config = private_json(directory / "launch.json")
    group = config["cgroup"]
    child, stopped, exit_code, reason = None, False, None, None
    try:
        with spawn_lock(directory):
            if not (directory / "cancelled").exists():
                child, gate, child_identity = start_gated("execute", directory, group)
                try:
                    persist(directory / "worker.json", {"process": child_identity, "runtime": identity()})
                    os.write(gate, b"1")
                finally:
                    os.close(gate)
        deadline = time.monotonic() + config["wall_seconds"]
        while child and child.poll() is None:
            if time.monotonic() >= deadline or (directory / "cancelled").exists():
                reason = "deadline" if time.monotonic() >= deadline else "cancelled"
                break
            time.sleep(.025)
        counts = kill_group(group)  # ALWAYS fence daemonized descendants, even after exit 0.
        stopped = True
        if child:
            exit_code = child.wait(timeout=5)
    except Exception as exc:
        record_error(directory, "supervise", exc)
        reason = "home_launcher_failure"
        try:
            counts = kill_group(group)
            stopped = True
        except Exception:
            counts = {}
    persist(directory / "completion.json", {"runtime": identity(), "cgroup": group, "stop_confirmed": stopped,
            "exit_code": exit_code, "reason": reason, "resource_counters": counts})


def main():
    mode, raw_directory, raw_fd = sys.argv[1:]
    directory = Path(raw_directory)
    if (os.geteuid() != 0 or directory.parent != ROOT / "work" or directory.resolve() != directory or
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", directory.name)):
        raise BoundaryError("home_launcher_path_invalid")
    require_private(directory)
    gate = int(raw_fd)
    released = os.read(gate, 1) == b"1"
    os.close(gate)
    if not released:
        return
    if mode == "execute":
        execute_worker(directory)
    elif mode == "supervise":
        supervise(directory)
    elif mode == "fuse":
        os.execve("/usr/bin/fuse2fs", ["fuse2fs", "-f", "-o", "rw,allow_other,nodev,nosuid",
                                      str(directory / "workspace.ext4"), str(directory / "workspace")], SAFE_ENV)
    else:
        raise BoundaryError("home_launcher_mode_invalid")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if len(sys.argv) == 4:
            record_error(sys.argv[2], sys.argv[1], exc)
        raise SystemExit(1) from None
