"""Persistent fixed host coding broker. Only this service owns Docker and disk mounts."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .coding_program import VERIFY_PROGRAM, digest_request, program
from .coding_relay import Relay, credentials
from .coding_sandbox import CodingSandbox, Limits, SandboxError, command
from .manager import atomic

MODELS = ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol"]
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "interrupted"}
ARTIFACT_LIMIT = 180 * 1024


def validate(body):
    required = {"work_contract_major", "id", "case_id", "worker_id", "mode", "provider", "model", "repo_id",
                "goal", "questions", "base_commit", "expires", "lease_epoch", "context_days"}
    if not isinstance(body, dict) or set(body) != required:
        raise SandboxError("Invalid coding fields")
    for field in ("id", "case_id"):
        if not isinstance(body[field], str) or not re.fullmatch(r"[a-f0-9]{32}", body[field]):
            raise SandboxError("Invalid coding identity")
    if (body["work_contract_major"] != 1 or type(body["work_contract_major"]) is not int or
        body["worker_id"] != "host-coder" or body["repo_id"] != "agentic-home" or body["provider"] != "codex" or
        body["mode"] not in {"research", "development"} or body["model"] not in ["", *MODELS]):
        raise SandboxError("Unsupported coding request")
    if (not isinstance(body["goal"], str) or not 1 <= len(body["goal"]) <= 4000 or
        not isinstance(body["questions"], list) or len(body["questions"]) > 8 or
        not all(isinstance(v, str) and len(v) <= 500 for v in body["questions"])):
        raise SandboxError("Invalid coding context")
    if body["base_commit"] is not None and not re.fullmatch(r"[a-f0-9]{40}", body["base_commit"]):
        raise SandboxError("Invalid coding commit")
    if type(body["expires"]) not in {float, int} or type(body["lease_epoch"]) is not int or body["lease_epoch"] < 1:
        raise SandboxError("Invalid coding lease")
    if type(body["context_days"]) is not int or not 1 <= body["context_days"] <= 7:
        raise SandboxError("Invalid retention")
    return body


class CodingService:
    def __init__(self, manager):
        self.manager = manager
        self.root = manager.root.parent / "coding"
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.state_path = self.root / "jobs.json"
        self.items = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.lock = threading.RLock()
        self.threads, self.relays, self.sandboxes = {}, {}, {}
        self.credential_path = manager.root.parent / "runner/coding-auth/profiles/codex/auth.json"
        self.source = self.root / "source"
        self.image = None
        self.auth_lock = self.root / "auth-lock.json"
        self.closing = False
        self.capability_cache = None
        try:
            images = json.loads((manager.product / "images.lock.json").read_text())
            choices = [v for k, v in images.items() if re.fullmatch(r"agentic-home-runner:[0-9.]+", k)]
            if len(choices) == 1 and re.fullmatch(r"sha256:[a-f0-9]{64}", choices[0]):
                self.image = choices[0]
        except (OSError, ValueError):
            pass
        for job in self.items.values():
            if job["state"] not in TERMINAL:
                job.update(state="stop_unconfirmed", stop_confirmed=False, error_code="broker_restarted")
                # Exact persisted container identity is required; never replay the original goal.
                try:
                    sandbox = self.sandbox()
                    result = sandbox.cancel(job.get("sandbox_job_id", job["id"]))
                    if result["state"] in TERMINAL:
                        job.update(state="interrupted", stop_confirmed=True)
                except (OSError, ValueError, SandboxError):
                    pass
        self.persist()
        self.closed = threading.Event()
        self.maintenance = threading.Thread(target=self.maintain, daemon=True)
        self.maintenance.start()

    def maintain(self):
        while not self.closed.wait(60):
            try:
                self.prune()
            except (OSError, ValueError, SandboxError):
                pass

    def persist(self):
        atomic(self.state_path, self.items)

    def sandbox(self, relay=None):
        if not self.image:
            raise SandboxError("Pinned runner image unavailable")
        return CodingSandbox(self.root / "work", self.image, {"agentic-home": self.source},
                             limits=Limits(disk_mib=512, memory_mib=768, pids=128, wall_seconds=300,
                                           artifact_bytes=4 * 1024**2, artifact_files=14, web_artifacts=True), relay=relay)

    def capabilities(self):
        ready, reason = False, "source_or_image_unavailable"
        if self.capability_cache and time.monotonic() - self.capability_cache[0] < 20:
            ready, reason = self.capability_cache[1:]
        elif self.image and self.source.exists():
            try:
                self.sandbox().preflight()
                ready, reason = True, None
            except (OSError, SandboxError):
                reason = "hard_boundary_unavailable"
            self.capability_cache = (time.monotonic(), ready, reason)
        credential_state = "needs_auth"
        try:
            credentials(self.credential_path)
            credential_state = "configured"
            if (self.root / "inference-verified.json").exists():
                credential_state = "inference_verified"
            if (self.root / "auth-failed.json").exists():
                credential_state = "needs_reauth"
        except (OSError, ValueError, SandboxError):
            pass
        return {"work_contract_major": 1, "worker_id": "host-coder", "runtime_location": "host_container",
                "available": ready, "state": "ready" if ready else "blocked", "reason": reason,
                "modes": ["research", "development"] if ready else [], "max_wall_seconds": 300,
                "providers": [{"id": "codex", "supported": ready, "models": MODELS,
                               "auth_methods": ["device_code", "api_key"], "credential_state": credential_state}]}

    def public(self, job):
        return {k: v for k, v in job.items() if k not in {"request", "digest", "retained_until", "sandbox_job_id"}}

    def active(self):
        return any(not value.get("stop_confirmed", False) for value in self.items.values())

    def refused(self, body, digest, reason):
        job = {k: body[k] for k in ("id", "case_id", "worker_id", "mode", "provider", "lease_epoch", "work_contract_major")}
        job.update(state="failed", execution_started=False, stop_confirmed=True, runtime_location="host_container",
                   base_commit=None, findings={}, evidence=[], artifacts=[], error_code=reason, request=body,
                   digest=digest, created=time.time(), retained_until=time.time() + body["context_days"] * 86400)
        self.items[body["id"]] = job
        self.persist()
        return 202, self.public(job)

    def submit(self, body):
        body = validate(body)
        digest = digest_request(body)
        with self.lock:
            if body["id"] in self.items:
                existing = self.items[body["id"]]
                if existing["digest"] != digest:
                    return 409, {"detail": "coding_id_payload_mismatch"}
                return 202, self.public(existing)
            if len(self.items) >= 10000:
                return 507, {"detail": "coding_identity_ledger_full"}
            if self.closing or self.active() or self.auth_lock.exists():
                return self.refused(body, digest, "coding_writer_or_auth_fence_active")
            if not time.time() < body["expires"] <= time.time() + 310:
                return self.refused(body, digest, "coding_authorization_expired")
            if not self.capabilities()["available"]:
                return self.refused(body, digest, "coding_boundary_unavailable")
            try:
                credentials(self.credential_path)
            except (OSError, ValueError, SandboxError):
                return self.refused(body, digest, "coding_needs_auth")
            commit = command("git", "--git-dir", str(self.source), "rev-parse", "HEAD").stdout.strip()
            lock = json.loads((self.root / "source.json").read_text())
            if (not re.fullmatch(r"[a-f0-9]{40}", commit) or body["base_commit"] not in {None, commit}
                or lock.get("commit") != commit or lock.get("repo_id") != "agentic-home"):
                return self.refused(body, digest, "coding_source_revision_mismatch")
            job = {k: body[k] for k in ("id", "case_id", "worker_id", "mode", "provider", "lease_epoch", "work_contract_major")}
            job.update(state="queued", execution_started=False, stop_confirmed=False, runtime_location="host_container",
                       base_commit=commit, findings={k: [] for k in ("facts", "inferences", "unknowns", "experiments", "next_steps")},
                       evidence=[], artifacts=[], error_code=None, request=body, digest=digest,
                       created=time.time(), retained_until=time.time() + body["context_days"] * 86400)
            self.items[body["id"]] = job
            self.persist()
            thread = threading.Thread(target=self.execute, args=(body["id"],), daemon=True)
            self.threads[body["id"]] = thread
            thread.start()
            return 202, self.public(job)

    def execute(self, id):
        relay = None
        try:
            with self.lock:
                job = self.items[id]
                if job["state"] != "queued":
                    return
                relay = Relay(self.root / "relays" / (id + ".sock"), self.credential_path, job["request"]["expires"])
                relay.model = job["request"]["model"] or "gpt-6-astra"
                relay.start()
                self.relays[id] = relay
                sandbox = self.sandbox(relay.path)
                self.sandboxes[id] = sandbox
                sandbox.launch(id, job["lease_epoch"], "agentic-home", job["base_commit"], program(job["request"]))
                job.update(state="running", execution_started=True)
                self.persist()
            while True:
                time.sleep(0.25)
                with self.lock:
                    if job["state"] == "cancelled":
                        return
                    if time.time() >= job["request"]["expires"] or self.closing:
                        result = sandbox.cancel(id)
                    else:
                        result = sandbox.status(id)
                    if result["state"] in {"running", "preparing"}:
                        continue
                    if result["state"] == "stop_unconfirmed":
                        job.update(state="stop_unconfirmed", stop_confirmed=False, error_code="writer_stop_unconfirmed")
                    else:
                        job.update(state=result["state"], stop_confirmed=True, exit_code=result.get("exit_code"))
                        if relay.auth_failed:
                            atomic(self.root / "auth-failed.json", {"at": time.time()})
                            job["error_code"] = "coding_needs_reauth"
                        patch = self.collect(job, sandbox)
                        if job["state"] == "succeeded" and patch:
                            self.verify(job, patch)
                    self.persist()
                    return
        except (OSError, ValueError, SandboxError) as exc:
            with self.lock:
                job = self.items[id]
                # Absence of launch acknowledgement does not prove Docker did not start.
                stopped = job.get("stop_confirmed", False)
                job.update(state="failed" if stopped else "stop_unconfirmed", stop_confirmed=stopped,
                           error_code=exc.code if isinstance(exc, SandboxError) else "coding_execution_failed")
                self.persist()
        finally:
            if relay:
                relay.close()
            with self.lock:
                job = self.items[id]
                if job.get("stop_confirmed"):
                    self.release_workspace(job)
                    self.persist()

    def release_workspace(self, job):
        if not job.get("execution_started") or job.get("workspace_released"):
            return
        try:
            for id in {job["id"], job.get("sandbox_job_id")} - {None}:
                self.sandbox().dispose(id)
                path = self.root / "work" / id
                if path.exists():
                    shutil.rmtree(path)
            job["workspace_released"] = True
            job.pop("cleanup_pending", None)
        except (OSError, SandboxError):
            job["cleanup_pending"] = True

    def collect(self, job, sandbox):
        workspace = sandbox.root / job["id"] / "workspace"
        source = workspace / "source"
        if source.is_symlink() or (source / ".git").is_symlink() or not (source / ".git").is_dir():
            raise SandboxError("Source identity changed")
        entries = 0
        for directory, dirs, files in os.walk(source, followlinks=False):
            if Path(directory) == source:
                dirs.remove(".git")
            entries += len(dirs) + len(files)
            if entries > 50000 or len(Path(directory).relative_to(source).parts) > 30:
                raise SandboxError("Candidate tree limit")
            for name in dirs + files:
                path = Path(directory) / name
                metadata = path.lstat()
                if name == ".git" or stat.S_ISLNK(metadata.st_mode) or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                    raise SandboxError("Candidate links or nested repository refused")
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                    raise SandboxError("Candidate hardlink refused")
        # .git is a separate read-only bind inside the worker. Disable external diff/textconv.
        command("git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false", "-C", str(workspace / "source"),
                "add", "--intent-to-add", "--all", timeout=15)
        patch = command("git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false", "-C", str(workspace / "source"),
                        "diff", "--no-ext-diff", "--no-textconv", job["base_commit"], "--", timeout=15).stdout
        if len(patch.encode()) <= ARTIFACT_LIMIT:
            path = workspace / "artifacts" / "changes.patch"
            path.unlink(missing_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(patch)
        exported = sandbox.artifacts(job["id"])
        output = self.root / "artifacts" / job["id"]
        output.mkdir(parents=True, mode=0o700, exist_ok=True)
        for entry in exported:
            raw = entry.pop("content").encode()
            if len(raw) > ARTIFACT_LIMIT:
                continue
            if entry["name"] == "findings.txt":
                job["findings"]["inferences"] = ["Unverified CLI summary: " + raw.decode()[:12000]]
            artifact_id = hashlib.sha256((job["id"] + ":" + entry["name"]).encode()).hexdigest()[:32]
            kind = "patch" if entry["name"].endswith((".patch", ".diff")) else "evidence"
            media_type = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                          ".svg": "image/svg+xml"}.get(Path(entry["name"]).suffix, "text/plain")
            if media_type != "text/plain":
                kind = "preview_asset"
            metadata = {"id": artifact_id, "job_id": job["id"], "kind": kind, "name": entry["name"],
                        "media_type": media_type, "sha256": entry["sha256"], "size": len(raw)}
            atomic(output / (artifact_id + ".json"), {**metadata, "content_base64": base64.b64encode(raw).decode()})
            job["artifacts"].append(metadata)
        content = {"base_commit": job["base_commit"], "cli_version": "0.157.1", "exit_code": job.get("exit_code")}
        digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        job["evidence"] = [{"evidence_id": hashlib.sha256((job["id"] + "execution").encode()).hexdigest()[:32],
            "observed_from": "host_container", "target_component": "isolated_repository",
            "collection_method": "pinned_codex_tools", "observed_at": datetime.now(UTC).isoformat(),
            "version_refs": {"codex": "0.157.1", "source": job["base_commit"]}, "content": content,
            "content_digest": digest, "redaction": "fixed_public_fields",
            "limitations": ["No live household or deployment access", "CLI claims remain untrusted"]}]
        if job["state"] == "succeeded":
            atomic(self.root / "inference-verified.json", {"job_id": job["id"], "at": time.time()})
        return patch.encode() if len(patch.encode()) <= ARTIFACT_LIMIT else None

    def verify(self, job, patch):
        # Called only after the development container is proved stopped. No auth/relay reuse.
        remaining = int(job["request"]["expires"] - time.time())
        if remaining < 5:
            job.update(state="failed", error_code="verification_deadline_exhausted")
            return
        id = job["id"] + "-verify"
        sandbox = CodingSandbox(self.root / "work", self.image, {"agentic-home": self.source},
                                limits=Limits(disk_mib=512, memory_mib=768, pids=128,
                                              wall_seconds=min(remaining, 300)), inputs={"candidate.patch": patch})
        job.update(state="running", stop_confirmed=False, sandbox_job_id=id,
                   verification={"state": "running", "patch_sha256": hashlib.sha256(patch).hexdigest()})
        self.sandboxes[job["id"]] = sandbox
        self.persist()
        sandbox.launch(id, job["lease_epoch"], "agentic-home", job["base_commit"], VERIFY_PROGRAM)
        # Drop the broker lock between observations so explicit cancellation remains effective.
        while True:
            self.lock.release()
            try:
                time.sleep(0.25)
            finally:
                self.lock.acquire()
            if job["state"] == "cancelled":
                return
            result = sandbox.cancel(id) if time.time() >= job["request"]["expires"] else sandbox.status(id)
            if result["state"] in {"running", "preparing"}:
                continue
            stopped = result["state"] in TERMINAL
            job.update(state=result["state"], stop_confirmed=stopped)
            if stopped:
                job["verification"] = {"state": "passed" if result["state"] == "succeeded" else "failed",
                    "patch_sha256": hashlib.sha256(patch).hexdigest(), "observed_from": "independent_host_verifier",
                    "exit_code": result.get("exit_code"), "stop_confirmed": True, "trusted_tests": "pinned_baseline",
                    "test_command_sha256": hashlib.sha256(VERIFY_PROGRAM.encode()).hexdigest(), "network": "none"}
                for entry in sandbox.artifacts(id):
                    raw = entry["content"].encode()
                    if len(raw) > ARTIFACT_LIMIT:
                        continue
                    aid = hashlib.sha256((id + entry["name"]).encode()).hexdigest()[:32]
                    meta = {"id": aid, "job_id": job["id"], "kind": "test_report", "name": entry["name"],
                            "media_type": "text/plain", "sha256": entry["sha256"], "size": len(raw)}
                    atomic(self.root / "artifacts" / job["id"] / (aid + ".json"),
                           {**meta, "content_base64": base64.b64encode(raw).decode()})
                    job["artifacts"].append(meta)
            return

    def cancel(self, id):
        with self.lock:
            job = self.items[id]
            if job.get("stop_confirmed"):
                return self.public(job)
            if job["state"] == "queued":
                job.update(state="cancelled", stop_confirmed=True)
            else:
                result = self.sandboxes.get(id, self.sandbox()).cancel(job.get("sandbox_job_id", id))
                stopped = result["state"] in TERMINAL
                job.update(state="cancelled" if stopped else "stop_unconfirmed", stop_confirmed=stopped,
                           error_code=None if stopped else "writer_stop_unconfirmed")
                if stopped and not job["artifacts"]:
                    try:
                        self.collect(job, self.sandbox())
                    except (OSError, SandboxError):
                        job["error_code"] = "checkpoint_export_failed"
            self.persist()
            return self.public(job)

    def prune(self, days=None):
        with self.lock:
            for job in self.items.values():
                if job.get("stop_confirmed") and job.get("cleanup_pending"):
                    self.release_workspace(job)
                if days is not None:
                    job["retained_until"] = min(job["retained_until"], job["created"] + days * 86400)
                if job["retained_until"] > time.time() or job.get("content_expired"):
                    continue
                self.cancel(job["id"])
                if not job["stop_confirmed"]:
                    continue
                path = self.root / "artifacts" / job["id"]
                if path.exists():
                    shutil.rmtree(path)
                try:
                    if job["execution_started"] and not job.get("workspace_released"):
                        for sandbox_id in (job["id"], job.get("sandbox_job_id")):
                            if sandbox_id:
                                self.sandbox().dispose(sandbox_id)
                                directory = self.root / "work" / sandbox_id
                                if directory.exists():
                                    shutil.rmtree(directory)
                    workspace = self.root / "work" / job["id"]
                    if workspace.exists():
                        shutil.rmtree(workspace)
                except (OSError, SandboxError):
                    job["error_code"] = "retention_workspace_cleanup_pending"
                    continue
                job.update(state="expired", content_expired=True, request=None, findings={}, evidence=[], artifacts=[])
            self.persist()

    def handle(self, method, path, body=None):
        try:
            self.prune()
            if path == "/coding/runtime" and method == "GET":
                data = json.loads(command("docker", "inspect", "agentic-home-runner-1").stdout)[0]
                if data["Config"]["Labels"].get("com.docker.compose.project") != "agentic-home" or not data["State"]["Running"]:
                    raise SandboxError("Runner identity unavailable")
                pid = data["State"]["Pid"]
                return 200, {"container_id": data["Id"], "host_init_pid": pid,
                    "host_init_start": int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]),
                    "pid_namespace": os.stat(f"/proc/{pid}/ns/pid").st_ino}
            if path == "/coding/reconcile-runtime" and method == "POST":
                if not isinstance(body, dict) or not re.fullmatch(r"[a-f0-9]{64}", body.get("container_id", "")):
                    return 422, {"detail": "invalid_runtime_identity"}
                pid, start = body.get("host_init_pid"), body.get("host_init_start")
                if type(pid) is not int or pid < 1 or type(start) is not int:
                    return 422, {"detail": "invalid_runtime_identity"}
                inspect = command("docker", "inspect", body["container_id"], check=False)
                if inspect.returncode == 0:
                    info = json.loads(inspect.stdout)[0]
                    if info["Id"] != body["container_id"] or info["State"]["Running"] or info["State"].get("Paused"):
                        return 409, {"stop_confirmed": False}
                else:
                    # A working Docker inventory plus vanished exact namespace-init proves teardown.
                    ids = command("docker", "ps", "--all", "--quiet", "--no-trunc").stdout.splitlines()
                    if body["container_id"] in ids:
                        return 409, {"stop_confirmed": False}
                try:
                    current = int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19])
                    if current == start:
                        return 409, {"stop_confirmed": False}
                except FileNotFoundError:
                    pass
                return 200, {"stop_confirmed": True, "proof": "exact_old_container_and_namespace_init_stopped"}
            if path == "/coding/capabilities" and method == "GET":
                return 200, self.capabilities()
            if path == "/coding/jobs" and method == "POST":
                return self.submit(body)
            if path == "/coding/retention" and method == "POST":
                if not isinstance(body, dict) or set(body) != {"context_days"} or type(body["context_days"]) is not int or not 1 <= body["context_days"] <= 7:
                    return 422, {"detail": "invalid_retention"}
                self.prune(body["context_days"])
                return 200, body
            if path == "/coding/fence" and method == "POST":
                with self.lock:
                    if not isinstance(body, dict) or set(body) != {"id"} or not re.fullmatch(r"[a-f0-9]{32}", body["id"]):
                        return 422, {"detail": "invalid_auth_identity"}
                    if self.auth_lock.exists():
                        if json.loads(self.auth_lock.read_text()).get("id") == body["id"]:
                            return 200, {"stop_confirmed": True}
                        return 409, {"detail": "coding_auth_in_progress"}
                    for id in self.items:
                        if not self.items[id].get("stop_confirmed"):
                            self.cancel(id)
                    if self.active():
                        return 409, {"detail": "writer_stop_unconfirmed"}
                    atomic(self.auth_lock, {"id": body["id"], "at": time.time()})
                    (self.root / "inference-verified.json").unlink(missing_ok=True)
                    (self.root / "auth-failed.json").unlink(missing_ok=True)
                    return 200, {"stop_confirmed": True}
            if path == "/coding/auth-release" and method == "POST":
                with self.lock:
                    if not self.auth_lock.exists() or json.loads(self.auth_lock.read_text()).get("id") != body.get("id"):
                        return 409, {"detail": "coding_auth_identity_mismatch"}
                    self.auth_lock.unlink(missing_ok=True)
                    return 200, {"released": True}
            match = re.fullmatch(r"/coding/jobs/([a-f0-9]{32})(?:/(cancel|handoff|artifacts/([a-f0-9]{32})))?", path)
            if match and match[1] in self.items:
                id, action = match[1], match[2]
                if method == "GET" and action is None:
                    return 200, self.public(self.items[id])
                if method == "POST" and action == "cancel":
                    return 200, self.cancel(id)
                if method == "POST" and action == "handoff":
                    job = self.cancel(id)
                    if not job["stop_confirmed"]:
                        return 409, job
                    if job["execution_started"] and not job["artifacts"]:
                        with self.lock:
                            self.collect(self.items[id], self.sandbox())
                            self.persist()
                            job = self.public(self.items[id])
                    return 200, {**job, "checkpoint": {"base_commit": job["base_commit"],
                        "artifact_ids": [a["id"] for a in job["artifacts"]]}, "live_session": False}
                if method == "GET" and action and action.startswith("artifacts/"):
                    file = self.root / "artifacts" / id / (match[3] + ".json")
                    if file.exists():
                        return 200, json.loads(file.read_text())
            return 404, {"detail": "unknown_coding_resource"}
        except (OSError, ValueError, KeyError, TypeError, SandboxError):
            return 409, {"detail": "coding_operation_rejected"}

    def close(self):
        self.closing = True
        self.closed.set()
        for id in list(self.items):
            if not self.items[id].get("stop_confirmed"):
                self.cancel(id)
        for thread in self.threads.values():
            thread.join(timeout=20)
        self.maintenance.join(timeout=2)
