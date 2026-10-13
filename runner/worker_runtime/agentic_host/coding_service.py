"""Persistent fixed host coding broker. Only this service owns Docker and disk mounts."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import shutil
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .coding_program import digest_request, program, verification_program
from .coding_relay import Relay, credentials
from .coding_sandbox import CodingSandbox, Limits, SandboxError, command
from .manager import atomic

# Slugs with visibility "list" in runner/agentic_runner/codex-models.json, catalog order.
# Hidden catalog entries stay out of the picker. gpt-6-astra stays the default.
MODELS = ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
DEFAULT_MODEL = "gpt-6-astra"
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "interrupted"}
LEDGER_STATES = TERMINAL | {"queued", "launching", "running", "stop_unconfirmed"}
ARTIFACT_LIMIT = 180 * 1024
# Phase budgets are monotonic and do not extend Owner authorization (`expires`).
SETUP_BUDGET_SECONDS = 300
EXECUTION_BUDGET_SECONDS = 300
VERIFICATION_BUDGET_SECONDS = 300
SHUTDOWN_BUDGET_SECONDS = 20
# Same ceiling as HostSession.verify: 12h owner cookie plus 60s skew. Not a longer grant.
AUTHORIZATION_MAX_AHEAD_SECONDS = 43260
# Exact labels from the worker prompt ("Finish with facts, inferences, unknowns, experiments,
# next_steps" in coding_program.py) and Worker Protocol v1 section 4. Absent headings stay empty.
FINDING_KEYS = ("facts", "inferences", "unknowns", "experiments", "next_steps")
_FINDING_HEADING = re.compile(
    r"^(?:#{1,3}[ \t]+|\*\*)?(facts|inferences|unknowns|experiments|next_steps)(?:\*\*)?[ \t]*:?[ \t]*$")
BUDGET_REASONS = {"setup_timeout", "execution_timeout", "verification_timeout", "authorization_expired"}
HIDDEN = {"request", "digest", "retained_until", "sandbox_job_id", "sandbox_attempted", "results_collected",
          "collect_failures"}


def parse_findings(text):
    """Split findings.txt on the five protocol headings only. Never invent a section."""
    sections = {key: [] for key in FINDING_KEYS}
    current = None
    if not isinstance(text, str):
        return sections
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = _FINDING_HEADING.fullmatch(line)
        if match:
            current = match.group(1)
            continue
        if current is None or not line:
            continue
        if line.startswith(("- ", "* ")):
            line = line[2:].strip()
        if not line or len(sections[current]) >= 32:
            continue
        sections[current].append(line[:240])
        if len(json.dumps(sections, ensure_ascii=False).encode()) > 60000:
            sections[current].pop()
            break
    return sections


def validate(body, worker_id="host-coder"):
    required = {"work_contract_major", "id", "case_id", "worker_id", "mode", "provider", "model", "repo_id",
                "goal", "questions", "base_commit", "expires", "lease_epoch", "context_days"}
    if not isinstance(body, dict) or set(body) != required:
        raise SandboxError("Invalid coding fields")
    for field in ("id", "case_id"):
        if not isinstance(body[field], str) or not re.fullmatch(r"[a-f0-9]{32}", body[field]):
            raise SandboxError("Invalid coding identity")
    if (body["work_contract_major"] != 1 or type(body["work_contract_major"]) is not int or
        body["worker_id"] != worker_id or body["repo_id"] != "agentic-home" or body["provider"] != "codex" or
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


def validate_ledger(value):
    """Reject malformed persisted data before accessing fields or attempting a stop."""
    if not isinstance(value, dict):
        raise ValueError("Invalid coding ledger root")
    for identifier, job in value.items():
        if (not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier) or
            not isinstance(job, dict) or job.get("id") != identifier or
            not isinstance(job.get("state"), str) or
            job["state"] not in LEDGER_STATES or
            type(job.get("execution_started")) is not bool or type(job.get("stop_confirmed")) is not bool or
            "sandbox_attempted" in job and type(job["sandbox_attempted"]) is not bool or
            any(type(job.get(key)) not in {int, float} or not math.isfinite(job[key])
                for key in ("created", "retained_until")) or
            "sandbox_job_id" in job and not isinstance(job["sandbox_job_id"], str)):
            raise ValueError("Invalid coding ledger entry")
    return value


class CodingService:
    worker_id = "host-coder"
    runtime_location = "host_container"

    def __init__(self, manager, *, root=None, credential_path=None, source=None, source_lock_path=None,
                 worker_id="host-coder", runtime_location="host_container",
                 sandbox_factory=None, relay_factory=None, version_refs=None):
        if (worker_id, runtime_location) not in {("host-coder", "host_container"), ("home-coder", "haos_app")}:
            raise ValueError("Unsupported worker location")
        self.manager = manager
        self.worker_id, self.runtime_location = worker_id, runtime_location
        self.sandbox_factory = sandbox_factory or CodingSandbox
        self.relay_factory = relay_factory or Relay
        self.version_refs = dict(version_refs or {})
        self.verifier_location = "independent_home_verifier" if worker_id == "home-coder" else "independent_host_verifier"
        self.verify_program = verification_program(self.verifier_location)
        self.root = Path(root) if root is not None else manager.root.parent / "coding"
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.state_path = self.root / "jobs.json"
        self.items = validate_ledger(json.loads(self.state_path.read_text()) if self.state_path.exists() else {})
        self.lock = threading.RLock()
        # Status reads this snapshot lock only. Launch, git and collect never hold it across their work.
        self.state_lock = threading.Lock()
        self.snapshots = {}
        self.threads, self.relays, self.sandboxes = {}, {}, {}
        self.credential_path = Path(credential_path) if credential_path is not None else manager.root.parent / "runner/coding-auth/profiles/codex/auth.json"
        self.source = Path(source) if source is not None else self.root / "source"
        self.source_lock_path = Path(source_lock_path) if source_lock_path is not None else self.root / "source.json"
        self.image = None
        self.auth_lock = self.root / "auth-lock.json"
        self.closing = False
        self.capability_cache = None
        try:
            images = json.loads((manager.product / "images.lock.json").read_text()) if manager is not None else {}
            choices = [v for k, v in images.items() if re.fullmatch(r"agentic-home-runner:[0-9.]+", k)]
            if len(choices) == 1 and re.fullmatch(r"sha256:[a-f0-9]{64}", choices[0]):
                self.image = choices[0]
        except (OSError, ValueError):
            pass
        for job in self.items.values():
            if job["state"] not in TERMINAL and not job.get("sandbox_attempted"):
                # The attempt is persisted before allocation: no container or workspace exists.
                job.update(state="interrupted", stop_confirmed=True, phase="interrupted")
                self.remember_error(job, "broker_restarted")
            elif job["state"] not in TERMINAL:
                job.update(state="stop_unconfirmed", stop_confirmed=False, phase="stop_unconfirmed")
                self.remember_error(job, "broker_restarted")
                # Exact persisted container identity is required; never replay the original goal.
                try:
                    sandbox = self.sandbox()
                    result = sandbox.cancel(job.get("sandbox_job_id", job["id"]))
                    if isinstance(result, dict) and result.get("state") in TERMINAL:
                        self.preserve_stopped(job, sandbox)
                        job.update(state="interrupted", stop_confirmed=True, phase="interrupted")
                except (OSError, ValueError, SandboxError):
                    pass
        self.persist()
        for job in self.items.values():
            self.snapshots[job["id"]] = self.export(job)
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

    def sandbox(self, relay=None, *, limits=None, inputs=None):
        if self.worker_id == "host-coder" and not self.image:
            raise SandboxError("Pinned runner image unavailable")
        return self.sandbox_factory(self.root / "work", self.image, {"agentic-home": self.source},
                             limits=limits or Limits(disk_mib=512, memory_mib=768, pids=128,
                                           wall_seconds=EXECUTION_BUDGET_SECONDS,
                                           artifact_bytes=4 * 1024**2, artifact_files=14, web_artifacts=True), relay=relay, inputs=inputs)

    def remember_error(self, job, code):
        """Record the first explainable reason. Cancel and restart must not clear it."""
        if code and not job.get("error_code"):
            job["error_code"] = code

    def export(self, job):
        return copy.deepcopy(self.public(job))

    def publish(self, job, *, live=None):
        """Publish an immutable status snapshot. `live` keeps a long collect visible."""
        with self.state_lock:
            if live:
                job["phase"] = live
            if job.get("state") not in TERMINAL or live:
                job["heartbeat_at"] = time.time()
            snap = self.export(job)
            if live and job.get("state") in TERMINAL:
                snap["state"] = "running"
                snap["stop_confirmed"] = False
                snap["phase"] = live
            self.snapshots[job["id"]] = snap
            self.persist()

    def view(self, id):
        with self.state_lock:
            snap = self.snapshots.get(id)
            if snap is None and id in self.items:
                snap = self.export(self.items[id])
                self.snapshots[id] = snap
            return None if snap is None else copy.deepcopy(snap)

    def pulse(self, id, stop):
        """Refresh heartbeat_at on the snapshot only. Never takes the launch lock or persists."""
        while not stop.wait(1):
            with self.state_lock:
                job = self.items.get(id)
                snap = self.snapshots.get(id)
                if job is None or snap is None:
                    return
                if job.get("results_collected") and job.get("state") in TERMINAL:
                    return
                if job.get("state") in TERMINAL and job.get("phase") != "collecting":
                    return
                fresh = copy.deepcopy(snap)
                fresh["heartbeat_at"] = time.time()
                if self.snapshots.get(id) is snap:
                    self.snapshots[id] = fresh

    def authorization_expired(self, job):
        request = job.get("request") or {}
        expires = request.get("expires")
        return type(expires) in {int, float} and not isinstance(expires, bool) and time.time() >= expires

    def results_outstanding(self, job):
        """Keep a workspace until collect has copied the diff. A partial launch may still be disposed."""
        if (job.get("results_collected") or job.get("content_expired") or job.get("collect_failures", 0) >= 3
                or job.get("retained_until", 0) <= time.time()):
            return False
        if not (self.root / "work" / job["id"]).exists():
            return False
        return bool(job.get("execution_started") or job.get("error_code") == "setup_timeout")

    def preserve_stopped(self, job, sandbox):
        if job.get("results_collected"):
            return
        try:
            self.collect(job, sandbox)
            job["results_collected"] = True
        except (OSError, ValueError, SandboxError):
            job["collect_failures"] = job.get("collect_failures", 0) + 1
            self.remember_error(job, "checkpoint_export_failed")

    def capabilities(self):
        ready, reason = False, "source_or_image_unavailable"
        if self.capability_cache and time.monotonic() - self.capability_cache[0] < 20:
            ready, reason = self.capability_cache[1:]
        elif (self.image or self.worker_id == "home-coder") and self.source.exists():
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
        return {"work_contract_major": 1, "worker_id": self.worker_id, "runtime_location": self.runtime_location,
                "available": ready, "state": "ready" if ready else "blocked", "reason": reason,
                "modes": ["research", "development"] if ready else [],
                "max_wall_seconds": EXECUTION_BUDGET_SECONDS,
                "budgets": {"setup_seconds": SETUP_BUDGET_SECONDS, "execution_seconds": EXECUTION_BUDGET_SECONDS,
                            "verification_seconds": VERIFICATION_BUDGET_SECONDS},
                "providers": [{"id": "codex", "supported": ready, "models": MODELS,
                               "auth_methods": ["device_code", "api_key"], "credential_state": credential_state}]}

    def public(self, job):
        return {k: v for k, v in job.items() if k not in HIDDEN}

    def active(self):
        return any(not value.get("stop_confirmed", False) for value in self.items.values())

    def refused(self, body, digest, reason):
        job = {k: body[k] for k in ("id", "case_id", "worker_id", "mode", "provider", "lease_epoch", "work_contract_major")}
        job.update(state="failed", phase="failed", execution_started=False, stop_confirmed=True,
                   results_collected=True, runtime_location=self.runtime_location,
                   base_commit=None, findings={key: [] for key in FINDING_KEYS}, evidence=[], artifacts=[],
                   error_code=reason, request=body, digest=digest, created=time.time(),
                   retained_until=time.time() + body["context_days"] * 86400)
        self.items[body["id"]] = job
        self.publish(job)
        return 202, self.view(body["id"])

    def submit(self, body):
        body = validate(body, self.worker_id)
        digest = digest_request(body)
        with self.lock:
            if body["id"] in self.items:
                existing = self.items[body["id"]]
                if existing["digest"] != digest:
                    return 409, {"detail": "coding_id_payload_mismatch"}
                return 202, self.view(body["id"])
            if len(self.items) >= 10000:
                return 507, {"detail": "coding_identity_ledger_full"}
            if self.closing or self.active() or self.auth_lock.exists():
                return self.refused(body, digest, "coding_writer_or_auth_fence_active")
            # Already-expired grants are refused. The horizon matches the owner session ceiling
            # (HostSession.verify). It is not the execution budget and is not widened past that session.
            if not time.time() < body["expires"] <= time.time() + AUTHORIZATION_MAX_AHEAD_SECONDS:
                return self.refused(body, digest, "coding_authorization_expired")
            if not self.capabilities()["available"]:
                return self.refused(body, digest, "coding_boundary_unavailable")
            try:
                credentials(self.credential_path)
            except (OSError, ValueError, SandboxError):
                return self.refused(body, digest, "coding_needs_auth")
            commit = command("git", "--git-dir", str(self.source), "rev-parse", "HEAD").stdout.strip()
            lock = json.loads(self.source_lock_path.read_text())
            if (not re.fullmatch(r"[a-f0-9]{40}", commit) or body["base_commit"] not in {None, commit}
                or lock.get("commit") != commit or lock.get("repo_id") != "agentic-home"):
                return self.refused(body, digest, "coding_source_revision_mismatch")
            job = {k: body[k] for k in ("id", "case_id", "worker_id", "mode", "provider", "lease_epoch", "work_contract_major")}
            job.update(state="queued", phase="queued", execution_started=False, stop_confirmed=False,
                       results_collected=False, runtime_location=self.runtime_location,
                       base_commit=commit, findings={key: [] for key in FINDING_KEYS},
                       evidence=[], artifacts=[], error_code=None, request=body, digest=digest,
                       created=time.time(), retained_until=time.time() + body["context_days"] * 86400,
                       heartbeat_at=time.time())
            self.items[body["id"]] = job
            self.publish(job)
            queued = self.view(body["id"])
            thread = threading.Thread(target=self.execute, args=(body["id"],), daemon=True)
            self.threads[body["id"]] = thread
            thread.start()
            return 202, queued

    def execute(self, id):
        relay = None
        pulse_stop = threading.Event()
        threading.Thread(target=self.pulse, args=(id, pulse_stop), daemon=True).start()
        try:
            with self.lock:
                job = self.items[id]
                if job["state"] != "queued":
                    return
                job.update(state="launching", phase="launching")
                self.publish(job)
                relay = self.relay_factory(self.root / "relays" / (id + ".sock"), self.credential_path, job["request"]["expires"])
                relay.model = job["request"]["model"] or DEFAULT_MODEL
                relay.start()
                self.relays[id] = relay
                sandbox = self.sandbox(relay.path)
                self.sandboxes[id] = sandbox
                # A failed launch acknowledgement can still leave a mounted
                # filesystem or writer. Persist the attempt before allocation.
                job["sandbox_attempted"] = True
                self.persist()
                setup_started = time.monotonic()
                sandbox.launch(id, job["lease_epoch"], "agentic-home", job["base_commit"], program(job["request"], observed_from=self.runtime_location))
                if self.closing:
                    self.remember_error(job, "broker_restarted")
                    self.finish_stopped(job, sandbox, relay, sandbox.cancel(id), "shutdown")
                    return
                if self.authorization_expired(job):
                    self.remember_error(job, "authorization_expired")
                    self.finish_stopped(job, sandbox, relay, sandbox.cancel(id), "authorization_expired")
                    return
                # Clones and the image mount are not part of the execution budget.
                if time.monotonic() - setup_started > SETUP_BUDGET_SECONDS:
                    self.remember_error(job, "setup_timeout")
                    self.finish_stopped(job, sandbox, relay, sandbox.cancel(id), "setup_timeout")
                    return
                job.update(state="running", phase="running", execution_started=True)
                self.publish(job)
                execution_deadline = time.monotonic() + EXECUTION_BUDGET_SECONDS
            while True:
                time.sleep(0.25)
                with self.lock:
                    if job["state"] in TERMINAL or job.get("stop_confirmed"):
                        return
                    if self.closing:
                        self.remember_error(job, "broker_restarted")
                        self.finish_stopped(job, sandbox, relay, sandbox.cancel(id), "shutdown")
                        return
                    if self.authorization_expired(job):
                        self.remember_error(job, "authorization_expired")
                        self.finish_stopped(job, sandbox, relay, sandbox.cancel(id), "authorization_expired")
                        return
                    if time.monotonic() >= execution_deadline:
                        self.remember_error(job, "execution_timeout")
                        self.finish_stopped(job, sandbox, relay, sandbox.cancel(id), "execution_timeout")
                        return
                    result = sandbox.status(id)
                    if result["state"] in {"running", "preparing"}:
                        continue
                    self.finish_stopped(job, sandbox, relay, result, None)
                    return
        except (OSError, ValueError, SandboxError) as exc:
            with self.lock:
                job = self.items[id]
                # Absence of launch acknowledgement does not prove Docker did not start.
                stopped = job.get("stop_confirmed", False)
                job.update(state="failed" if stopped else "stop_unconfirmed",
                           phase="failed" if stopped else "stop_unconfirmed", stop_confirmed=stopped)
                self.remember_error(job, exc.code if isinstance(exc, SandboxError) else "coding_execution_failed")
                self.publish(job)
        finally:
            pulse_stop.set()
            if relay:
                relay.close()
            with self.lock:
                job = self.items[id]
                if job.get("stop_confirmed") and not self.results_outstanding(job):
                    self.release_workspace(job)
                self.publish(job)

    def finish_stopped(self, job, sandbox, relay, result, reason):
        """Record a fenced stop and keep exported results. Caller holds self.lock."""
        stopped = isinstance(result, dict) and result.get("state") in TERMINAL
        if not stopped:
            job.update(state="stop_unconfirmed", phase="stop_unconfirmed", stop_confirmed=False)
            if reason == "shutdown":
                self.remember_error(job, "broker_restarted")
            elif reason:
                self.remember_error(job, reason)
            else:
                self.remember_error(job, "writer_stop_unconfirmed")
            self.publish(job)
            return None
        if reason == "shutdown":
            outcome, code = "interrupted", "broker_restarted"
        elif reason in BUDGET_REASONS or reason == "authorization_expired":
            outcome, code = "failed", reason
        elif reason == "owner_cancelled":
            outcome, code = "cancelled", "owner_cancelled"
        else:
            outcome, code = result.get("state"), None
        if outcome not in TERMINAL:
            outcome = "failed"
        job["exit_code"] = result.get("exit_code")
        if code:
            self.remember_error(job, code)
        if (relay is not None and getattr(relay, "auth_failed", False) and outcome in {"succeeded", "failed"}
                and reason is None):
            atomic(self.root / "auth-failed.json", {"at": time.time()})
            if not job.get("error_code"):
                job["error_code"] = "coding_needs_reauth"
        # Status keeps a live phase while git export runs. The ledger stays non-terminal until it finishes.
        self.publish(job, live="collecting")
        patch = None
        job["state"] = outcome
        try:
            if not job.get("results_collected"):
                patch = self.collect(job, sandbox)
                job["results_collected"] = True
        except (OSError, ValueError, SandboxError):
            job["collect_failures"] = job.get("collect_failures", 0) + 1
            self.remember_error(job, "checkpoint_export_failed")
            if outcome == "succeeded":
                outcome = "failed"
        job.update(state=outcome, phase=outcome, stop_confirmed=True)
        if outcome == "succeeded" and patch and reason is None:
            self.verify(job, patch)
        else:
            self.publish(job)
        return patch

    def release_workspace(self, job):
        if not job.get("stop_confirmed"):
            return False
        if job.get("workspace_released"):
            return True
        try:
            attempted = job.get("execution_started") or job.get("sandbox_attempted")
            disposed = False
            for id in sorted({job["id"], job.get("sandbox_job_id")} - {None}):
                path = self.root / "work" / id
                # Old records have no attempt marker. Existing exact ledger or
                # directory still requires disposal; absence is never a stop proof.
                if not attempted and id == job["id"] and not any(os.path.lexists(p)
                        for p in (path, self.root / "work" / (id + ".json"))):
                    continue
                result = self.sandbox().dispose(id)
                if (not isinstance(result, dict) or result.get("job_id") != id or
                    result.get("lease_epoch") != job.get("lease_epoch") or result.get("state") != "disposed" or
                    self.worker_id == "home-coder" and result.get("stop_confirmed") is not True or
                    path.is_symlink() or os.path.ismount(path) or os.path.ismount(path / "workspace")):
                    raise SandboxError("Workspace disposal not confirmed")
                if path.exists():
                    shutil.rmtree(path)
                disposed = True
            if disposed:
                job["workspace_released"] = True
            job.pop("cleanup_pending", None)
            return True
        except (OSError, ValueError, SandboxError):
            job["cleanup_pending"] = True
            return False

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
                # Headings are the protocol tokens only. Unheaded text stays in the artifact.
                job["findings"] = parse_findings(raw.decode("utf-8", errors="replace"))
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
            "observed_from": self.runtime_location, "target_component": "isolated_repository",
            "collection_method": "pinned_codex_tools", "observed_at": datetime.now(UTC).isoformat(),
            "version_refs": {**self.version_refs, "codex": "0.157.1", "source": job["base_commit"]}, "content": content,
            "content_digest": digest, "redaction": "fixed_public_fields",
            "limitations": ["No live household or deployment access", "CLI claims remain untrusted"]}]
        if job["state"] == "succeeded":
            atomic(self.root / "inference-verified.json", {"job_id": job["id"], "at": time.time()})
        return patch.encode() if len(patch.encode()) <= ARTIFACT_LIMIT else None

    def verify(self, job, patch):
        # Called only after the development container is proved stopped. No auth/relay reuse.
        # Own monotonic budget. Owner expires is a separate cancellation, not a shorter verifier.
        if self.authorization_expired(job) or self.closing:
            job.update(state="failed" if self.authorization_expired(job) else "interrupted", stop_confirmed=True,
                       phase="failed" if self.authorization_expired(job) else "interrupted")
            self.remember_error(job, "authorization_expired" if self.authorization_expired(job) else "broker_restarted")
            self.publish(job)
            return
        remaining = int(job["request"]["expires"] - time.time())
        wall = max(1, min(VERIFICATION_BUDGET_SECONDS, remaining))
        id = job["id"] + "-verify"
        sandbox = self.sandbox(limits=Limits(disk_mib=512, memory_mib=768, pids=128,
                                              wall_seconds=wall), inputs={"candidate.patch": patch})
        job.update(state="running", phase="verifying", stop_confirmed=False, sandbox_job_id=id,
                   verification={"state": "running", "patch_sha256": hashlib.sha256(patch).hexdigest()})
        self.sandboxes[job["id"]] = sandbox
        self.publish(job)
        started = time.monotonic()
        sandbox.launch(id, job["lease_epoch"], "agentic-home", job["base_commit"], self.verify_program)
        deadline = started + VERIFICATION_BUDGET_SECONDS
        # Drop the broker lock between observations so explicit cancellation remains effective.
        while True:
            self.lock.release()
            try:
                time.sleep(0.25)
            finally:
                self.lock.acquire()
            if job["state"] in TERMINAL or self.closing:
                return
            reason = None
            if self.authorization_expired(job):
                reason = "authorization_expired"
            elif time.monotonic() >= deadline:
                reason = "verification_timeout"
            if reason:
                # Keep the budget code even when the fenced stop itself fails.
                self.remember_error(job, reason)
            result = sandbox.cancel(id) if reason else sandbox.status(id)
            if result["state"] in {"running", "preparing"} and reason is None:
                continue
            if job["state"] in TERMINAL:
                return
            stopped = isinstance(result, dict) and result.get("state") in TERMINAL
            if reason:
                # The budget code was recorded above; a failed stop still reads as stop_unconfirmed.
                job.update(state="failed" if stopped else "stop_unconfirmed", stop_confirmed=stopped,
                           phase="failed" if stopped else "stop_unconfirmed")
                self.publish(job)
                return
            job.update(state=result["state"], phase=result["state"], stop_confirmed=stopped)
            if stopped:
                job["verification"] = {"state": "passed" if result["state"] == "succeeded" else "failed",
                    "patch_sha256": hashlib.sha256(patch).hexdigest(), "observed_from": self.verifier_location,
                    "exit_code": result.get("exit_code"), "stop_confirmed": True, "trusted_tests": "pinned_baseline",
                    "test_command_sha256": hashlib.sha256(self.verify_program.encode()).hexdigest(), "network": "none"}
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
            else:
                job.update(state="stop_unconfirmed", phase="stop_unconfirmed", stop_confirmed=False)
                self.remember_error(job, "writer_stop_unconfirmed")
            self.publish(job)
            return

    def cancel(self, id, *, reason="owner_cancelled"):
        with self.lock:
            job = self.items[id]
            if job.get("stop_confirmed"):
                self.publish(job)
                return self.view(id)
            if reason == "owner_cancelled" and self.authorization_expired(job):
                reason = "authorization_expired"
            if job["state"] in {"queued", "launching"} and not job.get("sandbox_attempted"):
                state = "interrupted" if reason == "shutdown" else "cancelled"
                job.update(state=state, phase=state, stop_confirmed=True)
                if reason == "shutdown":
                    self.remember_error(job, "broker_restarted")
                elif reason:
                    self.remember_error(job, reason)
                self.publish(job)
                return self.view(id)
            sandbox = self.sandboxes.get(id, self.sandbox())
            # A missing ledger or a rejected stop must surface. Callers treat that as 409, not as stopped.
            result = sandbox.cancel(job.get("sandbox_job_id", id))
            self.finish_stopped(job, sandbox, self.relays.get(id), result, reason if reason != "shutdown" else "shutdown")
            if job.get("stop_confirmed") and not self.results_outstanding(job):
                self.release_workspace(job)
            self.publish(job)
            return self.view(id)

    def prune(self, days=None):
        with self.lock:
            for job in list(self.items.values()):
                if days is not None:
                    job["retained_until"] = min(job["retained_until"], job["created"] + days * 86400)
                if job.get("stop_confirmed") and self.results_outstanding(job):
                    self.preserve_stopped(job, self.sandboxes.get(job["id"], self.sandbox()))
                if job.get("stop_confirmed") and not job.get("workspace_released") and not self.results_outstanding(job):
                    if self.release_workspace(job):
                        self.publish(job)
                if job["retained_until"] > time.time() or job.get("content_expired"):
                    continue
                if not job.get("stop_confirmed"):
                    self.cancel(job["id"], reason=None)
                if not job.get("stop_confirmed") or self.results_outstanding(job):
                    if not job.get("error_code"):
                        job["error_code"] = "retention_workspace_cleanup_pending"
                    continue
                if not self.release_workspace(job):
                    if not job.get("error_code"):
                        job["error_code"] = "retention_workspace_cleanup_pending"
                    continue
                path = self.root / "artifacts" / job["id"]
                if path.exists():
                    shutil.rmtree(path)
                job.update(state="expired", phase="expired", content_expired=True, request=None, findings={},
                           evidence=[], artifacts=[])
                self.publish(job)
            self.persist()

    def handle(self, method, path, body=None):
        try:
            # Status and capability reads must not wait on launch, git, collect or retention.
            if method != "GET":
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
                    for id in list(self.items):
                        if not self.items[id].get("stop_confirmed"):
                            self.cancel(id, reason=None)
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
                    # Snapshot only. Launch, git and collect hold self.lock and must not block this read.
                    snap = self.view(id)
                    return (200, snap) if snap is not None else (404, {"detail": "unknown_coding_resource"})
                if method == "POST" and action == "cancel":
                    return 200, self.cancel(id)
                if method == "POST" and action == "handoff":
                    job = self.cancel(id)
                    if not job["stop_confirmed"]:
                        return 409, job
                    if job["execution_started"] and not job["artifacts"]:
                        with self.lock:
                            if not self.items[id].get("results_collected"):
                                self.collect(self.items[id], self.sandbox())
                                self.items[id]["results_collected"] = True
                            self.publish(self.items[id])
                            job = self.view(id)
                    return 200, {**job, "checkpoint": {"base_commit": job["base_commit"],
                        "artifact_ids": [a["id"] for a in job["artifacts"]]}, "live_session": False}
                if method == "GET" and action and action.startswith("artifacts/"):
                    file = self.root / "artifacts" / id / (match[3] + ".json")
                    if file.exists():
                        return 200, json.loads(file.read_text())
            return 404, {"detail": "unknown_coding_resource"}
        except (OSError, ValueError, KeyError, TypeError, SandboxError):
            return 409, {"detail": "coding_operation_rejected"}

    def shutdown_job(self, id):
        """Fenced stop plus collect. Does not label the stop as an Owner cancel."""
        with self.lock:
            job = self.items[id]
            if job.get("stop_confirmed") or job["state"] in TERMINAL:
                return
            if not job.get("sandbox_attempted") and not job.get("execution_started"):
                job.update(state="interrupted", phase="interrupted", stop_confirmed=True)
                self.remember_error(job, "broker_restarted")
                self.publish(job)
                return
            sandbox = self.sandboxes.get(id, self.sandbox())
            try:
                result = sandbox.cancel(job.get("sandbox_job_id", id))
            except (OSError, ValueError, SandboxError):
                job.update(state="stop_unconfirmed", phase="stop_unconfirmed", stop_confirmed=False)
                self.remember_error(job, "broker_restarted")
                self.publish(job)
                return
            self.finish_stopped(job, sandbox, self.relays.get(id), result, "shutdown")
            if job.get("stop_confirmed") and not self.results_outstanding(job):
                self.release_workspace(job)
            self.publish(job)

    def close(self):
        self.closing = True
        self.closed.set()
        if getattr(self, "_shutdown_started", False):
            return
        self._shutdown_started = True
        deadline = time.monotonic() + SHUTDOWN_BUDGET_SECONDS
        for id in list(self.items):
            if time.monotonic() >= deadline:
                break
            job = self.items[id]
            if job.get("stop_confirmed") or job["state"] in TERMINAL:
                continue
            self.shutdown_job(id)
        for thread in list(self.threads.values()):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.maintenance.join(timeout=1)
