"""Home coding containment inside the actual HAOS App, without a Docker API.

Clone preparation and bounded artifact export are shared with the reviewed Host
implementation. Only allocation and process containment differ here. Namespace
changes on the same boot are NOT accepted as proof an old writer has stopped.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from agentic_host.coding_sandbox import (
    ERROR_CODES,
    ID,
    SHA,
    CodingSandbox,
    SandboxError,
    command,
    read_artifacts,
)

from . import home_launcher as launcher

_PROCESSES = {}
STOPPED = {"succeeded", "failed", "cancelled", "interrupted", "disposed"}


def launch_failure(exc):
    result = {**launcher.error_receipt("unknown", exc), "mode": "prepare"}
    if isinstance(exc, SandboxError):
        result.update(error_type="SandboxError", error_code=exc.code if exc.code in ERROR_CODES else "sandbox_rejected")
        # Reconstruct through the shared typed validator; never copy arbitrary
        # exception text or an unchecked diagnostics mapping into the ledger.
        typed = SandboxError("", operation=exc.diagnostics.get("operation"),
                             exit_code=exc.diagnostics.get("command_exit_code"),
                             timeout_seconds=exc.diagnostics.get("timeout_seconds"))
        result.update(typed.diagnostics)
        # Preflight wraps boundary failures for callers. Preserve only its
        # immediate, typed, allowlisted cause; neither message is public data.
        cause = exc.__cause__
        if isinstance(cause, launcher.BoundaryError) and str(cause) in launcher.ERROR_CODES:
            result["error_code"] = str(cause)
    return result


class HomeSandbox(CodingSandbox):
    def __init__(self, root, image, repos, *, limits=None, relay=None, inputs=None, execute=command):
        super().__init__(root, image or "sha256:" + "0" * 64, repos,
                         limits=limits, relay=relay, inputs=inputs, execute=execute)
        self.native_execute = execute
        self.execute = self._execute
        self.preparing = None

    def _require_work_root(self):
        try:
            if os.geteuid() != 0 or self.root != launcher.ROOT / "work":
                raise launcher.BoundaryError("home_fixed_work_root_required")
            launcher.runtime_context()
            self.root.mkdir(mode=0o700, exist_ok=True)
            launcher.require_private(self.root)
        except (OSError, ValueError, launcher.BoundaryError) as exc:
            raise SandboxError("Home boundary unavailable") from exc

    def preflight(self):
        self._require_work_root()
        try:
            for binary in ("/usr/bin/bwrap", "/usr/bin/fuse2fs", "/usr/sbin/mkfs.ext4", "/usr/bin/git"):
                path = Path(binary)
                if not path.is_file() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                    raise launcher.BoundaryError("home_trusted_tool_unavailable")
            fs = self.native_execute("findmnt", "-n", "-o", "FSTYPE", "-T", str(self.root)).stdout.strip()
            if fs not in {"ext4", "xfs", "btrfs"}:
                raise launcher.BoundaryError("home_disk_backing_required")
            space = os.statvfs(self.root)
            if space.f_bavail * space.f_frsize < self.limits.disk_mib * 1024**2 + 4 * 1024**3:
                raise launcher.BoundaryError("home_disk_reserve_required")
            if self.relay is not None:
                relay = Path(self.relay)
                if (relay.parent != launcher.ROOT / "relays" or relay.is_symlink() or
                        not stat.S_ISSOCK(relay.stat().st_mode) or relay.stat().st_uid != 10000):
                    raise launcher.BoundaryError("home_fixed_relay_required")
            return True
        except (OSError, ValueError, launcher.BoundaryError) as exc:
            raise SandboxError("Home boundary unavailable") from exc

    def save(self, job):
        super().save(job)
        return {k: v for k, v in job.items() if k not in {"request_digest", "launcher", "fuse", "cgroup", "fuse_cgroup"}}

    def _execute(self, *args, **kwargs):
        if args[0] != "mount":
            return self.native_execute(*args, **kwargs)
        job = self.preparing
        if job is None:
            raise SandboxError("Unbound Home mount")
        directory = self.root / job["job_id"]
        if args != ("mount", "-o", "loop,nodev,nosuid", str(directory / "workspace.ext4"), str(directory / "workspace")):
            raise SandboxError("Unknown Home mount request")
        group = launcher.create_group("f-" + hashlib.sha256(job["job_id"].encode()).hexdigest()[:32],
                                      memory_mib=128, pids=16, cpus=1)
        job["fuse_cgroup"] = group
        self.save(job)
        process, gate, identity = launcher.start_gated("fuse", directory, group)
        _PROCESSES[process.pid] = process
        try:
            job["fuse"] = identity
            self.save(job)
            os.write(gate, b"1")
        finally:
            os.close(gate)
        deadline = time.monotonic() + 8
        while not os.path.ismount(directory / "workspace"):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise launcher.BoundaryError("home_fuse_mount_unavailable")
            time.sleep(.025)
        fs = self.native_execute("findmnt", "-n", "-o", "FSTYPE", "-T", str(directory / "workspace")).stdout.strip()
        # Pinned fuse2fs 1.47.0 sets subtype=ext4 in its default mount options.
        if fs != "fuse.ext4":
            raise launcher.BoundaryError("home_fuse_mount_identity_invalid")
        return None

    def prepare(self, job, program):
        self.preparing = job
        try:
            workspace = super().prepare(job, program)
            launcher.seccomp_file(workspace.parent / "seccomp.bpf")
            return workspace
        finally:
            self.preparing = None

    def launch(self, job_id, lease_epoch, repo_id, base_commit, program):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id) or type(lease_epoch) is not int or lease_epoch < 1:
            raise SandboxError("Invalid job or lease")
        if repo_id not in self.repos or not isinstance(base_commit, str) or not SHA.fullmatch(base_commit):
            raise SandboxError("Repository and fixed commit are required")
        if not isinstance(program, str) or not 0 < len(program.encode()) <= 65536:
            raise SandboxError("Program exceeds limit")
        values = [job_id, lease_epoch, repo_id, base_commit, program,
                  {k: hashlib.sha256(v).hexdigest() for k, v in self.inputs.items()}]
        digest = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
        # Validate the trusted root before taking its lock or writing any record.
        # Resource/tool preflight belongs to the durable lifecycle below: a
        # rejected fresh launch must still have an exact identity to cancel.
        self._require_work_root()
        with self.lock():
            if (self.root / (job_id + ".json")).exists():
                previous = self.record(job_id)
                if previous["request_digest"] != digest:
                    raise SandboxError("Job ID reused with different payload")
                return self.save(previous)
            for path in self.root.glob("*.json"):
                if json.loads(path.read_text())["state"] not in STOPPED:
                    raise SandboxError("Previous writer has not been fenced")
            job = {"job_id": job_id, "lease_epoch": lease_epoch, "repo_id": repo_id, "base_commit": base_commit,
                   "request_digest": digest, "state": "preparing", "observed_from": "haos_app",
                   "runtime": launcher.identity(), "stop_confirmed": False, "model_backend": "none", "ha_backend": "none"}
            self.save(job)
            try:
                self.preflight()
                if sum(path.is_dir() for path in self.root.iterdir()) >= 4:
                    raise launcher.BoundaryError("home_retained_workspace_budget_exhausted")
                self.prepare(job, program)
                group = launcher.create_group("w-" + hashlib.sha256(job_id.encode()).hexdigest()[:32],
                                              memory_mib=self.limits.memory_mib, pids=self.limits.pids, cpus=self.limits.cpus)
                job["cgroup"] = group
                self.save(job)
                directory = self.root / job_id
                launcher.persist(directory / "launch.json", {"cgroup": group, "wall_seconds": self.limits.wall_seconds,
                                                              "relay": str(self.relay) if self.relay else None})
                process, gate, identity = launcher.start_gated("supervise", directory)
                _PROCESSES[process.pid] = process
                try:
                    job.update(launcher=identity, state="running")
                    self.save(job)
                    os.write(gate, b"1")
                finally:
                    os.close(gate)
            except (OSError, ValueError, launcher.BoundaryError, SandboxError) as exc:
                job["state"] = "stop_unconfirmed"
                job["launch_failure"] = launch_failure(exc)
                # A full backing filesystem or failed mount must not replace
                # the original failure merely because diagnostics also fail.
                try:
                    launcher.persist(self.root / job_id / "launch-error.json", job["launch_failure"])
                except (OSError, ValueError):
                    pass
                self.save(job)
                if isinstance(exc, SandboxError):
                    raise
                raise SandboxError("Home launch requires reconciliation") from None
            return self.save(job)

    def _current(self, job):
        current = launcher.identity()
        if job["runtime"] == current:
            return True
        if job["runtime"]["boot_id"] != current["boot_id"]:
            job.update(state="interrupted", stop_confirmed=True, previous_boot_stopped=True)
            return False
        raise launcher.BoundaryError("home_previous_namespace_stop_unconfirmed")

    def _cleanup_group_absent(self, job, name):
        removed = name in job.get("removed_groups", [])
        pending = name in job.get("removing_groups", [])
        if not removed and not pending:
            return False
        group = job[name]
        if (not job.get("cleanup_fenced") or job.get("stop_confirmed") is not True or
                not re.fullmatch(r"[wf]-[a-f0-9]{32}", group["name"])):
            raise launcher.BoundaryError("home_cgroup_replaced")
        absent = not os.path.lexists(launcher.ROOT / "cgroups" / group["name"])
        if removed and not absent:
            raise launcher.BoundaryError("home_cgroup_replaced")
        # A pending removal may already have completed before a crashed final
        # save. A still-present group must pass the usual inode/empty proof.
        return absent

    def status(self, job_id):
        with self.lock():
            job = self.record(job_id)
            if job["state"] == "disposed":
                return self.save(job)
            try:
                if self._current(job):
                    group = job.get("cgroup")
                    if group and not self._cleanup_group_absent(job, "cgroup"):
                        counts = launcher.counters(launcher.group_path(group))
                        job.update(resource_counters=counts, cgroup_populated=counts["cgroup.events"].get("populated") != 0,
                                   oom_killed=counts["memory.events"].get("oom_kill", 0) > 0)
                    path = self.root / job_id / "completion.json"
                    if job.get("cleanup_fenced"):
                        if group and not self._cleanup_group_absent(job, "cgroup") and not launcher.empty(group):
                            raise launcher.BoundaryError("home_writer_stop_unconfirmed")
                    elif path.exists():
                        value = launcher.private_json(path)
                        if (value.get("runtime") != job["runtime"] or value.get("cgroup") != group or
                                value.get("stop_confirmed") is not True or not launcher.empty(group)):
                            raise launcher.BoundaryError("home_completion_unconfirmed")
                        if job["state"] not in {"cancelled", "interrupted"}:
                            exit_code = value.get("exit_code")
                            job.update(state="succeeded" if type(exit_code) is int and exit_code == 0 and value.get("reason") is None else "failed",
                                       exit_code=exit_code, stop_confirmed=True)
                    elif job["state"] not in STOPPED:
                        expected = job.get("launcher")
                        if expected is None or launcher.process_identity(expected["pid"]) != expected:
                            job.update(state="stop_unconfirmed", stop_confirmed=False)
            except (OSError, ValueError, launcher.BoundaryError):
                job.update(state="stop_unconfirmed", stop_confirmed=False)
            self._reap(job)
            return self.save(job)

    @staticmethod
    def _reap(job):
        for key in ("launcher", "fuse"):
            pid = job.get(key, {}).get("pid")
            process = _PROCESSES.get(pid)
            if process is not None and process.poll() is not None:
                _PROCESSES.pop(pid, None)

    def cancel(self, job_id):
        with self.lock():
            job = self.record(job_id)
            if job["state"] == "disposed":
                return self.save(job)
            try:
                if self._current(job):
                    directory = self.root / job_id
                    if directory.exists():
                        if job.get("cgroup") and not self._cleanup_group_absent(job, "cgroup"):
                            counts = launcher.cancel_writer(directory, job["cgroup"])
                            job.update(resource_counters=counts, cgroup_populated=False)
                        else:
                            # Launch holds writer.lock through cgroup persistence;
                            # without a group it cannot have released any code.
                            with launcher.spawn_lock(directory):
                                (directory / "cancelled").touch(mode=0o600)
                    job.update(state=job["state"] if job["state"] in STOPPED else "cancelled", stop_confirmed=True)
            except (OSError, ValueError, launcher.BoundaryError):
                job.update(state="stop_unconfirmed", stop_confirmed=False)
            self._reap(job)
            return self.save(job)

    def artifacts(self, job_id):
        result = self.status(job_id)
        if result["state"] not in STOPPED or result.get("stop_confirmed") is not True or result.get("previous_boot_stopped"):
            raise SandboxError("Home writer must be proven stopped before export")
        with self.lock():
            job = self.record(job_id)
            if job.get("cleanup_fenced"):
                raise SandboxError("Home workspace cleanup has begun")
            group = job.get("cgroup")
            if not isinstance(group, dict) or not group:
                # Preparing the clone/artifact tree precedes worker creation.
                # A cancelled partial preparation has no execution evidence to
                # export, but its typed rejection must not prevent disposal.
                raise SandboxError("Home worker cgroup unavailable for artifact export")
            if not launcher.empty(group):
                raise SandboxError("Home writer still active")
            return read_artifacts(self.root / job_id / "workspace/artifacts", self.limits)

    def dispose(self, job_id):
        try:
            return self._dispose(job_id)
        except launcher.BoundaryError as exc:
            # Shared-service cleanup retries SandboxError. Keep launcher-specific
            # failures inside that contract without changing any stop proof.
            raise SandboxError("Home workspace cleanup unconfirmed") from exc

    def _dispose(self, job_id):
        result = self.cancel(job_id)
        if result["state"] == "disposed":
            return result
        if not result.get("stop_confirmed"):
            raise SandboxError("Home writer stop unconfirmed")
        with self.lock():
            job = self.record(job_id)
            mount = self.root / job_id / "workspace"
            if job.get("previous_boot_stopped"):
                if os.path.ismount(mount):
                    raise SandboxError("Unexpected workspace after boot change")
            else:
                # The durable cancel marker plus the writer cgroup's empty
                # receipt fence late execution. Stop the trusted watchdog too
                # before removing any resources it might still observe.
                if not job.get("cleanup_fenced"):
                    if job.get("launcher"):
                        launcher.stop_trusted_process(job["launcher"])
                        process = _PROCESSES.get(job["launcher"]["pid"])
                        if process is not None:
                            process.wait(timeout=5)
                    job["cleanup_fenced"] = True
                    self.save(job)
                if os.path.ismount(mount):
                    self.native_execute("umount", str(mount), timeout=10)
                if os.path.ismount(mount):
                    raise SandboxError("Home FUSE unmount unconfirmed")
                for name in ("fuse_cgroup", "cgroup"):
                    if not job.get(name):
                        continue
                    if not self._cleanup_group_absent(job, name):
                        launcher.kill_group(job[name])
                        if name not in job.get("removing_groups", []):
                            job.setdefault("removing_groups", []).append(name)
                            self.save(job)  # Persist empty-group removal intent BEFORE rmdir.
                        launcher.group_path(job[name]).rmdir()
                    if name not in job.get("removed_groups", []):
                        job.setdefault("removed_groups", []).append(name)
                        self.save(job)
            self._reap(job)
            job.update(state="disposed", stop_confirmed=True)
            return self.save(job)
