"""Durable coding dispatch over paired Runner TLS; no CLI or shell in Controller.

The persisted global lease belongs to one exact request. A lost reply, restart,
revoked scope or cancellation intention cannot release it without Runner proof.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import re
import time
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .models import canonical, digest, utcnow
from .runner import RunnerError
from .work import WorkerAction, WorkerAuth, WorkerId, WorkerPreference, WorkerStart, WorkerWireJob

WORKER_SCOPES = {"host-coder": "host", "home-coder": "haos"}
LOCATIONS = {"host-coder": "host_container", "home-coder": "haos_app"}
TERMINAL = {"succeeded", "failed", "cancelled", "blocked", "expired", "interrupted"}
RUNNER_STATES = TERMINAL | {"accepted", "queued", "running", "waiting_for_user", "cancel_requested", "stop_unconfirmed", "interrupted", "needs_reconcile"}
IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_ARTIFACT = 180 * 1024
FINDINGS = ("facts", "inferences", "unknowns", "experiments", "next_steps")


class WorkerError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def code(value, fallback="worker_unavailable"):
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,80}", value) else fallback


def relative_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or "\\" in value or any(ord(c) < 32 for c in value):
        raise WorkerError("invalid_artifact", 502)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"..", ".", ""} for part in value.split("/")):
        raise WorkerError("invalid_artifact", 502)
    return value


class Workers:
    def __init__(self, store, runners, coordinator):
        self.store, self.runners, self.coordinator = store, runners, coordinator
        self.submit_lock, self.advance_lock = asyncio.Lock(), asyncio.Lock()
        self.task, self.event, self.closing = None, asyncio.Event(), False
        self.auth_lock, self.retention_lock = asyncio.Lock(), asyncio.Lock()
        self.retention_retry_after, self.auth_retry_after = 0, 0
        self.cache = {worker: self.unavailable(worker, "not_observed") for worker in WORKER_SCOPES}

    @staticmethod
    def unavailable(worker, reason):
        return {"worker_id": worker, "work_contract_major": 1, "available": False, "supported": False,
                "state": "blocked", "reason": reason, "runtime_location": LOCATIONS[worker],
                "observed_from": "controller", "modes": [], "providers": []}

    def runner(self, worker):
        return self.runners[WORKER_SCOPES[worker]]

    def public_capabilities(self):
        lease = self.store.setting("worker_lease")
        return {"work_contract_major": 1, "concurrency_limit": 1,
                "active_job_id": lease.get("job_id") if lease else None,
                "workers": list(self.cache.values()), "auth_pending": ({k: v for k, v in self.store.setting("worker_auth_pending").items() if k != "runner_identity"}
                    if self.store.setting("worker_auth_pending") else None),
                "retention_sync_pending": bool(self.store.setting("retention_pending"))}

    async def capabilities(self):
        async def observe(worker):
            try:
                raw = await asyncio.wait_for(self.runner(worker).call("GET", "/coding/capabilities"), timeout=5)
                if (not isinstance(raw, dict) or raw.get("work_contract_major") != 1 or
                    raw.get("worker_id") != worker or raw.get("runtime_location") != LOCATIONS[worker]):
                    raise WorkerError("worker_contract_mismatch")
                providers = []
                for provider in raw.get("providers", [])[:4]:
                    if not isinstance(provider, dict) or provider.get("id") not in {"codex", "claude", "agy", "grok"}:
                        continue
                    providers.append({"id": provider["id"], "supported": provider.get("supported") is True,
                        "models": [m for m in provider.get("models", [])[:30] if isinstance(m, str) and
                                   re.fullmatch(r"[a-zA-Z0-9._:/-]{1,100}", m)],
                        "auth_methods": [m for m in provider.get("auth_methods", []) if m in {"device_code", "api_key"}],
                        "credential_state": code(provider.get("credential_state"), "needs_auth")})
                return {"worker_id": worker, "work_contract_major": 1, "available": raw.get("available") is True,
                    "supported": any(p["supported"] for p in providers), "state": code(raw.get("state"), "blocked"),
                    "reason": code(raw.get("reason")) if raw.get("reason") else None,
                    "runtime_location": raw["runtime_location"], "observed_from": "paired_runner",
                    "modes": [m for m in raw.get("modes", []) if m in {"research", "development"}],
                    "providers": providers, "selected_provider": self.preference(worker)["provider"],
                    "selected_model": self.preference(worker)["model"], "observed_at": utcnow()}
            except (RunnerError, WorkerError, OSError, ValueError, TypeError, AttributeError, KeyError):
                return self.unavailable(worker, "runner_unavailable")
        values = await asyncio.gather(*(observe(worker) for worker in WORKER_SCOPES))
        self.cache = {value["worker_id"]: value for value in values}
        return self.public_capabilities()

    def preference(self, worker):
        return self.store.setting("worker_preferences", {}).get(worker, {"provider": "codex", "model": ""})

    def current_provider(self):
        return self.store.setting("gateway", {}).get("provider", "codex")

    def authority(self, *, grant=None, request=None):
        if grant:
            return {"kind": "case", "message_id": grant["message_id"], "scope": grant["scope"],
                    "provider": self.current_provider(), "expires": grant["expires"],
                    "auth_epoch": self.store.setting("worker_auth_epoch")}
        if request is None:
            raise WorkerError("owner_authority_required", 403)
        identity = getattr(getattr(request, "state", None), "bridge_identity", None)
        try:
            expires = identity.expires if identity else int(request.cookies.get("agentic_host", "").split(".")[0])
        except (ValueError, TypeError):
            raise WorkerError("owner_authority_required", 403) from None
        return {"kind": "owner", "owner": self.store.setting("owner"),
                **({"bridge_pairing_id": identity.pairing_id} if identity else {}),
                "web_epoch": self.store.setting("hermes_web_epoch"),
                "policy": digest(self.store.setting("policy", {})), "expires": expires,
                "auth_epoch": self.store.setting("worker_auth_epoch")}

    def authorized(self, authority):
        if (self.store.setting("paused", False) or self.store.setting("updater_gate") or authority.get("expires", 0) <= time.time() or
            authority.get("auth_epoch") != self.store.setting("worker_auth_epoch")):
            return False
        if authority.get("kind") == "case":
            item = self.store.conversation(id=authority["message_id"])
            return bool(item and item.get("scope") == authority.get("scope") and
                        self.current_provider() == authority.get("provider") and self.coordinator.scope_valid(item))
        return (authority.get("kind") == "owner" and
                (not authority.get("bridge_pairing_id") or getattr(self, "bridge_valid", lambda _: False)(authority)) and
                authority.get("owner") == self.store.setting("owner") and
                authority.get("web_epoch") == self.store.setting("hermes_web_epoch") and
                authority.get("policy") == digest(self.store.setting("policy", {})))

    def require_owner(self, authority):
        # Credential management may run while AI is paused, but an old browser
        # request must not start it after logout or replacement owner binding.
        if (authority.get("kind") != "owner" or authority["expires"] <= time.time() or
            authority["owner"] != self.store.setting("owner") or
            authority["web_epoch"] != self.store.setting("hermes_web_epoch") or
            self.store.setting("updater_gate")):
            raise WorkerError("owner_authority_revoked", 403)

    def peer_identity(self, worker):
        return digest(self.store.setting("runner" if worker == "host-coder" else "runner_haos", {}))

    def context_days(self):
        current = self.store.setting("retention", {"context_days": 7})["context_days"]
        pending = self.store.setting("retention_pending")
        return min(current, pending["context_days"]) if pending else current

    def get(self, identifier, case_id=None):
        item = self.store.get("work_job", identifier)
        if not item or (case_id is not None and item["case_id"] != case_id):
            raise WorkerError("worker_job_not_found", 404)
        self.expire(item)
        return self.store.get("work_job", identifier)

    def expire(self, item):
        created = item.get("created_epoch")
        if created is None:
            created = datetime.fromisoformat(item["created_at"]).timestamp()
        if item.get("content_expired") or created + self.context_days() * 86400 > time.time():
            return
        minimal = {key: value for key, value in item.items() if key in {
            "id", "job_id", "case_id", "worker_id", "mode", "provider", "state", "created_at", "created_epoch",
            "updated_at", "data_contract", "work_contract_major", "lease_epoch", "execution_started", "stop_confirmed",
            "request_digest", "runner_identity", "runtime_location", "dispatch_attempted"}}
        minimal.update(content_expired=True, artifacts=[], evidence_ids=[], **{key: [] for key in FINDINGS})
        if item["state"] not in TERMINAL or not item.get("stop_confirmed", False):
            minimal.update(cancel_requested=True, state="cancel_requested", reason="context_expired")
        self.store.put("work_job", minimal)
        with self.store.db:
            self.store.db.execute("DELETE FROM records WHERE kind IN ('worker_artifact','case_evidence') "
                                  "AND json_extract(body,'$.job_id')=?", (item["id"],))

    def public(self, item):
        return {key: value for key, value in item.items() if key not in {
            "authority", "request", "request_digest", "runner_identity", "created_epoch"}}

    def listing(self):
        values = []
        for item in self.store.list("work_job", 100):
            self.expire(item)
            current = self.store.get("work_job", item["id"])
            values.append({key: value for key, value in self.public(current).items() if key not in FINDINGS})
        return {"jobs": values, "active_job_id": self.public_capabilities()["active_job_id"]}

    def case_jobs(self, case_id):
        rows = self.store.db.execute("SELECT body FROM records WHERE kind='work_job' "
            "AND json_extract(body,'$.case_id')=? ORDER BY created DESC LIMIT 8", (case_id,)).fetchall()
        import json
        values = []
        for row in rows:
            item = self.get(json.loads(row[0])["id"], case_id)
            values.append({key: item.get(key) for key in ("id", "state", "worker_id", "mode", "execution_started", "stop_confirmed", "content_expired")} |
                          {"artifact_count": len(item.get("artifacts", [])), "evidence_count": len(item.get("evidence_ids", []))})
        return values

    async def submit(self, body, key, *, grant=None, request=None, commit_guard=None):
        async with self.submit_lock:
            raw = body.model_dump()
            raw.pop("idempotency_key", None)
            authority = self.authority(grant=grant, request=request)
            scope = "worker:" + grant["case_id"] if grant else "owner_worker:" + str(self.store.setting("owner"))
            fingerprint = digest(raw)
            previous = self.store.request(scope, key, fingerprint)
            if previous:
                item = self.store.get("work_job", previous)
                if not item:
                    raise WorkerError("worker_job_expired", 410)
                return self.public(self.get(previous))
            if not self.authorized(authority):
                raise WorkerError("worker_authority_revoked", 403)
            if self.store.setting("worker_auth_pending"):
                raise WorkerError("coding_auth_pending")
            if self.store.setting("retention_pending"):
                raise WorkerError("retention_sync_pending")
            if (self.store.setting("retention") and self.store.setting("retention_peers") !=
                digest([self.store.setting("runner"), self.store.setting("runner_haos")])):
                await self.update_retention(self.store.setting("retention"))
            capabilities = await self.capabilities()
            if not self.authorized(authority):
                raise WorkerError("worker_authority_revoked", 403)
            case_id = grant["case_id"] if grant else raw.get("case_id") or uuid.uuid4().hex
            if self.store.db.execute("SELECT count(*) FROM records WHERE kind='work_job'").fetchone()[0] >= 10000:
                raise WorkerError("worker_identity_ledger_full", 507)
            if sum(job["case_id"] == case_id for job in self.store.list("work_job", 10000)) >= 4:
                raise WorkerError("case_job_budget_exhausted", 429)
            if sum(job["state"] not in TERMINAL for job in self.store.list("work_job", 10000)) >= 8:
                raise WorkerError("worker_queue_full", 429)
            worker = body.worker_id
            provider = raw.get("provider") or self.preference(worker)["provider"]
            model = raw.get("model") or self.preference(worker)["model"]
            capability = next(value for value in capabilities["workers"] if value["worker_id"] == worker)
            selected = next((value for value in capability["providers"] if value["id"] == provider), None)
            reason = None
            if not selected or not selected["supported"]:
                reason = "unsupported_provider" if capability["providers"] else capability["reason"]
            elif body.mode not in capability["modes"] or not capability["available"]:
                reason = capability["reason"] or "worker_unavailable"
            elif selected["credential_state"] not in {"configured", "connected", "inference_verified"}:
                reason = "coding_needs_auth"
            elif model and model not in selected["models"]:
                reason = "unsupported_model"
            identifier, now = uuid.uuid4().hex, time.time()
            envelope = WorkerWireJob(id=identifier, case_id=case_id, worker_id=worker, mode=body.mode,
                provider=provider, model=model, repo_id=body.repo_id, goal=body.goal,
                questions=body.questions, expires=min(now + 300, authority["expires"]), lease_epoch=1,
                context_days=self.context_days()).model_dump()
            peer_key = "runner" if worker == "host-coder" else "runner_haos"
            peer = self.store.setting(peer_key, {})
            item = {"id": identifier, "job_id": identifier, "case_id": case_id, "data_contract": 1,
                "work_contract_major": 1, "worker_id": worker, "mode": body.mode, "provider": provider, "model": model,
                "goal": body.goal, "state": "blocked" if reason else "queued", "reason": reason,
                "created_at": utcnow(), "created_epoch": now, "updated_at": utcnow(), "lease_epoch": 1,
                "execution_started": False, "stop_confirmed": bool(reason), "artifacts": [], "evidence_ids": [],
                "authority": authority, "request": envelope, "request_digest": digest(envelope),
                "runner_identity": digest(peer), **{name: [] for name in FINDINGS}}
            if commit_guard:
                commit_guard()
            with self.store.db:
                timestamp = utcnow()
                self.store.db.execute("INSERT INTO records VALUES (?,?,?,?,?)",
                    ("work_job", identifier, canonical(item), timestamp, timestamp))
                self.store.db.execute("INSERT INTO requests VALUES (?,?,?,?)", (scope, key, fingerprint, identifier))
            if not reason:
                self.wake()
            return self.public(item)

    def wake(self):
        if self.closing:
            return
        self.event.set()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.watch())

    async def start(self):
        self.closing = False
        self.wake()

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def watch(self):
        while not self.closing:
            self.event.clear()
            try:
                await self.advance()
                pending_auth = self.store.setting("worker_auth_pending")
                if pending_auth and pending_auth.get("id") and time.time() >= self.auth_retry_after:
                    self.auth_retry_after = time.time() + 5
                    if pending_auth.get("kind") == "logout":
                        await self.role_auth(pending_auth["worker_id"], "POST", "/coding/providers/codex/logout", {})
                    else:
                        await self.role_auth(pending_auth["worker_id"], "GET", "/coding/auth/" + pending_auth["id"])
                if self.store.setting("retention_pending") and time.time() >= self.retention_retry_after:
                    await self.sync_retention()
            except (RunnerError, WorkerError, OSError, ValueError, TypeError, KeyError):
                # The exact persisted lease remains the recovery authority.
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.event.wait(), 1)

    def save(self, item, **changes):
        return self.store.put("work_job", {**item, **changes, "updated_at": utcnow()})

    async def advance(self):
        async with self.advance_lock:
            for item in self.store.list("work_job", 10000):
                self.expire(item)
            lease = self.store.setting("worker_lease")
            if lease:
                item = self.store.get("work_job", lease["job_id"])
                if not item:
                    return  # Missing authority is not a free lease.
            else:
                if self.store.setting("worker_auth_pending") or self.store.setting("retention_pending"):
                    return
                jobs = self.store.list("work_job", 10000)
                uncertain = [j for j in jobs if j["state"] not in TERMINAL | {"queued"}]
                if uncertain:
                    item = uncertain[-1]
                else:
                    item = next((j for j in reversed(jobs) if j["state"] == "queued"), None)
                if not item:
                    return
                lease = {"job_id": item["id"], "epoch": item["lease_epoch"]}
                self.store.set_setting("worker_lease", lease)
            if item["state"] in TERMINAL and item.get("stop_confirmed"):
                self.store.set_setting("worker_lease", None)
                self.event.set()
                return
            peer_key = "runner" if item["worker_id"] == "host-coder" else "runner_haos"
            if item.get("runner_identity") != digest(self.store.setting(peer_key, {})):
                self.save(item, state="needs_reconcile", reason="runner_identity_changed")
                return
            if (item.get("content_expired") or not self.authorized(item.get("authority", {})) or
                item.get("request", {}).get("expires", 0) <= time.time()):
                if item["state"] == "queued" and not item.get("dispatch_attempted"):
                    self.save(item, state="cancelled", reason="authorization_expired", stop_confirmed=True)
                    self.store.set_setting("worker_lease", None)
                    self.event.set()
                    return
                item = self.save(item, cancel_requested=True, state="cancel_requested", reason="authority_revoked")
            runner = self.runner(item["worker_id"])
            try:
                if item.get("cancel_requested"):
                    result = await runner.call("POST", "/coding/jobs/" + item["id"] + "/cancel", {})
                else:
                    try:
                        result = await runner.call("GET", "/coding/jobs/" + item["id"])
                    except RunnerError as exc:
                        if exc.status_code != 404:
                            raise
                        current = self.get(item["id"])
                        if current.get("execution_started") or current.get("cancel_requested") or not self.authorized(current.get("authority", {})):
                            raise WorkerError("worker_job_unconfirmed") from None
                        if current.get("runner_identity") != self.peer_identity(current["worker_id"]):
                            raise WorkerError("runner_identity_changed") from None
                        self.save(current, state="dispatching", dispatch_attempted=True)
                        result = await runner.call("POST", "/coding/jobs", current["request"])
                if item.get("runner_identity") != self.peer_identity(item["worker_id"]):
                    raise WorkerError("runner_identity_changed")
                self.accept(item["id"], result)
            except (RunnerError, WorkerError, OSError, TimeoutError, ValueError, TypeError, KeyError) as exc:
                current = self.get(item["id"])
                if current["state"] in TERMINAL and current.get("stop_confirmed"):
                    return
                self.save(current, state="stop_unconfirmed" if current.get("cancel_requested") else "needs_reconcile",
                          reason=exc.code if isinstance(exc, WorkerError) else "runner_response_unconfirmed")

    def accept(self, identifier, raw):
        item = self.get(identifier)
        if (not isinstance(raw, dict) or raw.get("id") != identifier or
            raw.get("lease_epoch") != item["lease_epoch"] or raw.get("state") not in RUNNER_STATES):
            raise WorkerError("worker_response_mismatch", 502)
        if raw.get("worker_id", item["worker_id"]) != item["worker_id"] or raw.get("case_id", item["case_id"]) != item["case_id"]:
            raise WorkerError("worker_response_mismatch", 502)
        state, stopped = raw["state"], raw.get("stop_confirmed") is True
        if state in TERMINAL and not stopped:
            raise WorkerError("worker_stop_unconfirmed", 502)
        updates = {"state": state, "execution_started": item.get("execution_started", False) or raw.get("execution_started") is True,
                   "stop_confirmed": stopped, "reason": code(raw["error_code"]) if raw.get("error_code") else None}
        location = raw.get("runtime_location")
        if location is not None or updates["execution_started"]:
            if location != LOCATIONS[item["worker_id"]]:
                raise WorkerError("worker_location_mismatch", 502)
            updates["runtime_location"] = location
        if item.get("cancel_requested") and state not in TERMINAL:
            updates["state"] = "stop_unconfirmed" if state == "stop_unconfirmed" else "cancel_requested"
        evidence_rows, artifact_rows = [], []
        if not item.get("content_expired") and self.authorized(item.get("authority", {})):
            findings = raw.get("findings", {})
            if not isinstance(findings, dict) or len(canonical(findings).encode()) > 65536:
                raise WorkerError("invalid_worker_findings", 502)
            for key in FINDINGS:
                value = findings.get(key, [])
                if not isinstance(value, list) or len(value) > 32:
                    raise WorkerError("invalid_worker_findings", 502)
                updates[key] = value
            if isinstance(raw.get("base_commit"), str) and re.fullmatch(r"[a-f0-9]{40}", raw["base_commit"]):
                updates["base_commit"] = raw["base_commit"]
            artifact_rows = self.artifact_metadata(item, raw.get("artifacts", []))
            evidence_rows = self.evidence(item, raw.get("evidence", []))
            verification = raw.get("verification")
            if verification is not None:
                if not isinstance(verification, dict) or len(canonical(verification).encode()) > 8192:
                    raise WorkerError("invalid_worker_verification", 502)
                updates["verification"] = verification
            updates["artifacts"] = artifact_rows
            updates["evidence_ids"] = [row["id"] for row in evidence_rows]
        if item.get("cancel_requested") and state == "succeeded":
            updates.update(state="cancelled", reason="result_after_cancellation")
        now = utcnow()
        updated = {**item, **updates, "updated_at": now}
        rows = [("worker_artifact", row) for row in artifact_rows] + [("case_evidence", row) for row in evidence_rows] + [("work_job", updated)]
        with self.store.db:
            for kind, row in rows:
                self.store.db.execute("INSERT INTO records VALUES (?,?,?,?,?) ON CONFLICT(kind,id) "
                    "DO UPDATE SET body=excluded.body,updated=excluded.updated", (kind, row["id"], canonical(row), now, now))
            if state in TERMINAL and stopped:
                lease = self.store.setting("worker_lease")
                if lease and lease["job_id"] == identifier and lease["epoch"] == item["lease_epoch"]:
                    self.store.db.execute("UPDATE settings SET value='null' WHERE key='worker_lease'")
                    self.event.set()

    def artifact_metadata(self, item, values):
        if not isinstance(values, list) or len(values) > 16:
            raise WorkerError("invalid_artifact", 502)
        result = []
        seen = set()
        for value in values:
            if (not isinstance(value, dict) or not IDENTIFIER.fullmatch(str(value.get("id", ""))) or
                not SHA256.fullmatch(str(value.get("sha256", ""))) or type(value.get("size")) is not int or
                not 0 <= value["size"] <= MAX_ARTIFACT):
                raise WorkerError("invalid_artifact", 502)
            if value["id"] in seen:
                raise WorkerError("duplicate_artifact_identity", 502)
            seen.add(value["id"])
            metadata = {"id": value["id"], "job_id": item["id"], "case_id": item["case_id"],
                        "sha256": value["sha256"], "size": value["size"], "name": relative_name(value.get("name")),
                        "kind": code(value.get("kind"), "file"), "media_type": str(value.get("media_type", "application/octet-stream"))[:100]}
            previous = self.store.get("worker_artifact", metadata["id"])
            if previous and previous != metadata:
                raise WorkerError("artifact_identity_conflict", 502)
            result.append(metadata)
        return result

    def evidence(self, item, values):
        if not isinstance(values, list) or len(values) > 32:
            raise WorkerError("invalid_worker_evidence", 502)
        result = []
        for value in values:
            required = {"evidence_id", "observed_from", "target_component", "collection_method", "observed_at", "version_refs", "content_digest", "redaction", "limitations"}
            if (not isinstance(value, dict) or not required <= value.keys() or
                not IDENTIFIER.fullmatch(str(value["evidence_id"])) or not SHA256.fullmatch(str(value["content_digest"])) or
                len(canonical(value).encode()) > 16384):
                raise WorkerError("invalid_worker_evidence", 502)
            evidence = {key: value[key] for key in required}
            if "content" in value:
                if digest(value["content"]) != value["content_digest"]:
                    raise WorkerError("evidence_digest_mismatch", 502)
                evidence["content"] = value["content"]
            evidence.update(id=value["evidence_id"], job_id=item["id"], case_id=item["case_id"],
                            data_contract=1, runtime_location=LOCATIONS[item["worker_id"]])
            previous = self.store.get("case_evidence", evidence["id"])
            if previous and previous != evidence:
                raise WorkerError("evidence_identity_conflict", 502)
            result.append(evidence)
        return result

    async def cancel(self, identifier, case_id=None):
        item = self.get(identifier, case_id)
        if item["state"] in TERMINAL and item.get("stop_confirmed"):
            return self.public(item)
        lease = self.store.setting("worker_lease")
        if item["state"] == "queued" and not item.get("dispatch_attempted"):
            item = self.save(item, state="cancelled", stop_confirmed=True, cancel_requested=True)
            if lease and lease["job_id"] == identifier:
                self.store.set_setting("worker_lease", None)
        else:
            item = self.save(item, state="cancel_requested", cancel_requested=True)
            self.wake()
        return self.public(item)

    async def artifact(self, identifier, artifact_id):
        item = self.get(identifier)
        if item.get("content_expired"):
            raise WorkerError("worker_job_expired", 410)
        metadata = self.store.get("worker_artifact", artifact_id)
        if not metadata or metadata["job_id"] != identifier:
            raise WorkerError("artifact_not_found", 404)
        peer_key = "runner" if item["worker_id"] == "host-coder" else "runner_haos"
        if item.get("runner_identity") != digest(self.store.setting(peer_key, {})):
            raise WorkerError("runner_identity_changed")
        value = await self.runner(item["worker_id"]).call("GET", f"/coding/jobs/{identifier}/artifacts/{artifact_id}")
        if not isinstance(value, dict) or value.get("id") != artifact_id or value.get("job_id") != identifier:
            raise WorkerError("invalid_artifact", 502)
        try:
            content = base64.b64decode(value["content_base64"], validate=True)
        except (ValueError, TypeError, KeyError):
            raise WorkerError("invalid_artifact", 502) from None
        if len(content) != metadata["size"] or hashlib.sha256(content).hexdigest() != metadata["sha256"]:
            raise WorkerError("artifact_digest_mismatch", 502)
        if self.get(identifier).get("content_expired"):
            raise WorkerError("worker_job_expired", 410)
        return {**metadata, "content_base64": base64.b64encode(content).decode()}

    async def handoff(self, identifier):
        async with self.advance_lock:
            item = self.get(identifier)
            if item.get("content_expired"):
                raise WorkerError("worker_job_expired", 410)
            if item.get("runner_identity") != self.peer_identity(item["worker_id"]):
                raise WorkerError("runner_identity_changed")
            response = await self.runner(item["worker_id"]).call("POST", "/coding/jobs/" + identifier + "/handoff", {})
            if (not isinstance(response, dict) or response.get("id") != identifier or
                response.get("lease_epoch") != item["lease_epoch"] or response.get("stop_confirmed") is not True or
                response.get("live_session") is not False):
                raise WorkerError("handoff_stop_unconfirmed")
            receipt = await self.runner(item["worker_id"]).call("GET", "/coding/jobs/" + identifier)
            if item.get("runner_identity") != self.peer_identity(item["worker_id"]):
                raise WorkerError("runner_identity_changed")
            self.accept(identifier, receipt)
            item = self.get(identifier)
            checkpoint = response.get("checkpoint", {})
            artifacts = checkpoint.get("artifact_ids", [])
            if (not item.get("stop_confirmed") or item["state"] not in TERMINAL or not isinstance(artifacts, list) or
                len(artifacts) > 16 or not set(artifacts) <= {value["id"] for value in item["artifacts"]} or
                checkpoint.get("base_commit") != item.get("base_commit")):
                raise WorkerError("invalid_handoff_checkpoint", 502)
            return {"id": identifier, "lease_epoch": item["lease_epoch"], "stop_confirmed": True,
                    "checkpoint": {"base_commit": item.get("base_commit"), "artifact_ids": artifacts},
                    "live_session": False}

    async def update_retention(self, policy):
        async with self.retention_lock:
            pending = self.store.setting("retention_pending")
            if pending and pending != policy:
                raise WorkerError("retention_sync_pending")
            self.store.set_setting("retention_pending", policy)
            # Erase expired local context immediately at the stricter limit.
            # A later failed participant or limit increase cannot resurrect it.
            self.store.prune()
            for item in self.store.list("work_job", 10000):
                self.expire(item)
            await self._sync_retention(policy)
        return policy

    async def sync_retention(self):
        async with self.retention_lock:
            policy = self.store.setting("retention_pending")
            if policy:
                await self._sync_retention(policy)

    async def _sync_retention(self, policy):
        from .hermes import HermesError
        self.retention_retry_after = time.time() + 30
        peers = digest([self.store.setting("runner"), self.store.setting("runner_haos")])
        try:
            result = await self.coordinator.client.call("POST", "/v1/retention", {"context_days": policy["context_days"]})
            # Bridge acknowledgement is the successful fixed endpoint response;
            # older bridge builds omit the echoed limit.
            if result.get("context_days", policy["context_days"]) != policy["context_days"]:
                raise WorkerError("retention_sync_pending", 503)
            for scope, setting in (("host", "runner"), ("haos", "runner_haos")):
                if not self.store.setting(setting):
                    continue
                result = await self.runners[scope].call("POST", "/retention", {"context_days": policy["context_days"]})
                if result.get("context_days") != policy["context_days"]:
                    raise WorkerError("retention_sync_pending", 503)
        except (HermesError, RunnerError, OSError, ValueError, TypeError, AttributeError) as exc:
            raise WorkerError("retention_sync_pending", 503) from exc
        from .operations import broker_call
        for preview in self.store.list("worker_preview", 10000):
            job = self.store.get("work_job", preview["job_id"])
            if not job or job.get("content_expired"):
                try:
                    await broker_call("POST", "/previews/" + preview["id"] + "/revoke", {})
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise WorkerError("retention_sync_pending", 503) from exc
        if peers != digest([self.store.setting("runner"), self.store.setting("runner_haos")]):
            raise WorkerError("retention_sync_pending", 503)
        self.store.set_settings({"retention": policy, "retention_pending": None,
            "retention_peers": peers})
        self.store.prune()
        self.event.set()

    async def role_auth(self, worker, method, path, body=None, key=None, commit_guard=None):
        # Serialize auth mutations with dispatch and acceptance. A device flow is
        # durably a pending barrier until its observed terminal result is read.
        async with self.auth_lock, self.advance_lock, self.submit_lock:
            if commit_guard:
                commit_guard()
            peer_setting = "runner" if worker == "host-coder" else "runner_haos"
            peer_identity = digest(self.store.setting(peer_setting, {}))
            pending = self.store.setting("worker_auth_pending")
            if pending and pending.get("worker_id") == worker and pending.get("runner_identity", peer_identity) != peer_identity:
                raise WorkerError("runner_identity_changed")
            enrollment = method == "POST" and path.endswith("/auth")
            logout = method == "POST" and path.endswith("/logout")
            lookup = None
            if logout:
                pending = self.store.setting("worker_auth_pending")
                if self.store.setting("worker_lease"):
                    raise WorkerError("coding_job_must_stop_before_auth")
                if pending:
                    if pending.get("worker_id") != worker or pending.get("kind") != "logout":
                        raise WorkerError("coding_auth_pending")
                    lookup = pending["id"]
                else:
                    lookup = uuid.uuid4().hex
                    self.store.set_settings({"worker_auth_epoch": uuid.uuid4().hex,
                        "worker_auth_pending": {"worker_id": worker, "id": lookup, "state": "dispatching", "kind": "logout", "runner_identity": peer_identity}})
            elif enrollment:
                fingerprint = digest([worker, body, peer_identity])
                scope = "coding_auth:" + worker
                lookup = self.store.request(scope, key or "", fingerprint)
                if not lookup:
                    if self.store.setting("worker_auth_pending"):
                        raise WorkerError("coding_auth_pending")
                    if self.store.setting("worker_lease"):
                        raise WorkerError("coding_job_must_stop_before_auth")
                    lookup = uuid.uuid4().hex
                    settings = {"worker_auth_epoch": uuid.uuid4().hex,
                        "worker_auth_pending": {"worker_id": worker, "id": lookup, "state": "dispatching", "kind": "login", "runner_identity": peer_identity}}
                    with self.store.db:
                        self.store.db.execute("INSERT INTO requests VALUES (?,?,?,?)", (scope, key, fingerprint, lookup))
                        self.store.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                                 [(name, canonical(value)) for name, value in settings.items()])
            elif method == "POST":
                if self.store.setting("worker_lease"):
                    raise WorkerError("coding_job_must_stop_before_auth")
                self.store.set_setting("worker_auth_epoch", uuid.uuid4().hex)
            if enrollment or logout:
                try:
                    response = await self.runner(worker).call("GET", "/coding/auth/" + lookup)
                except RunnerError as exc:
                    if exc.status_code != 404:
                        raise
                    pending = self.store.setting("worker_auth_pending")
                    if self.store.setting("worker_lease") or pending and (pending.get("worker_id") != worker or pending.get("id") != lookup):
                        raise WorkerError("coding_job_or_auth_must_stop") from None
                    if not pending:
                        self.store.set_settings({"worker_auth_epoch": uuid.uuid4().hex,
                            "worker_auth_pending": {"worker_id": worker, "id": lookup, "state": "dispatching",
                                                    "kind": "logout" if logout else "login", "runner_identity": peer_identity}})
                    if peer_identity != self.peer_identity(worker):
                        raise WorkerError("runner_identity_changed") from None
                    if commit_guard:
                        commit_guard()
                    response = await self.runner(worker).call(method, path, {**(body or {}), "id": lookup})
                if not isinstance(response, dict) or response.get("id") != lookup:
                    raise WorkerError("invalid_worker_auth_response", 502)
            else:
                response = await self.runner(worker).call(method, path, body)
            if peer_identity != self.peer_identity(worker):
                raise WorkerError("runner_identity_changed")
            if not isinstance(response, dict):
                raise WorkerError("invalid_worker_auth_response", 502)
            allowed = {"id", "status", "state", "expires_in", "credential_state", "inference_verified_at"}
            result = {key: value for key, value in response.items() if key in allowed and
                      isinstance(value, (str, int, float, type(None))) and len(str(value)) <= 200}
            url = response.get("url")
            if isinstance(url, str) and url in {"https://auth.openai.com/codex/device", "https://auth.openai.com/device"}:
                result["url"] = url
            pairing = response.get("code")
            if isinstance(pairing, str) and re.fullmatch(r"[A-Z0-9]{4,5}-[A-Z0-9]{4,5}", pairing):
                result["code"] = pairing
            pending = self.store.setting("worker_auth_pending")
            if (pending and pending.get("worker_id") == worker and
                pending.get("id") == response.get("id")):
                if response.get("status") in {"connected", "configured", "succeeded", "failed", "cancelled", "expired", "disconnected"}:
                    self.store.set_setting("worker_auth_pending", None)
                    self.event.set()
                elif isinstance(response.get("id"), str) and IDENTIFIER.fullmatch(response["id"]):
                    self.store.set_setting("worker_auth_pending", {**pending, "state": code(response.get("status"), "pending")})
            return result


def routes(app, workers):
    @app.exception_handler(WorkerError)
    async def worker_error(request, exc):
        return JSONResponse({"detail": exc.code}, status_code=exc.status, headers={"Cache-Control": "no-store"})

    @app.post("/api/runtimes/{scope}/reconcile")
    async def reconcile_runtime(scope: Literal["host", "haos"], body: WorkerAction):
        result = await workers.runners[scope].call("POST", "/reconcile", {})
        if not isinstance(result, dict) or result.get("stop_confirmed") is not True or result.get("replayed") is not False:
            raise WorkerError("runner_stop_unconfirmed")
        return {"runtime": scope, "profile": "home_operator", "execution_state": result.get("execution_state"),
                "stop_confirmed": True, "replayed": False, "observed_from": "paired_runner"}

    @app.post("/api/workers/{worker}/reconcile")
    async def reconcile_coding(worker: WorkerId, body: WorkerAction):
        async with workers.auth_lock, workers.advance_lock:
            pending = workers.store.setting("worker_auth_pending")
            peer_setting = "runner" if worker == "host-coder" else "runner_haos"
            peer_identity = digest(workers.store.setting(peer_setting, {}))
            if pending and pending.get("worker_id") == worker and pending.get("runner_identity", peer_identity) != peer_identity:
                raise WorkerError("runner_identity_changed")
            result = await workers.runner(worker).call("POST", "/coding/reconcile", {})
            if not isinstance(result, dict) or result.get("stop_confirmed") is not True or result.get("replayed") is not False:
                raise WorkerError("coding_auth_stop_unconfirmed")
            if peer_identity != workers.peer_identity(worker):
                raise WorkerError("runner_identity_changed")
            pending = workers.store.setting("worker_auth_pending")
            if pending and pending.get("worker_id") == worker:
                workers.store.set_setting("worker_auth_pending", None)
            workers.event.set()
            return {"worker_id": worker, "profile": "coding_auth", "stop_confirmed": True, "replayed": False}

    @app.get("/api/workers")
    async def capabilities():
        return await workers.capabilities()

    @app.get("/api/worker/jobs")
    async def jobs():
        return workers.listing()

    @app.post("/api/worker/jobs", status_code=202)
    async def submit(body: WorkerStart, request: Request):
        return await workers.submit(body, request.headers.get("idempotency-key", ""), request=request)

    @app.get("/api/worker/jobs/{identifier}")
    async def status(identifier: str):
        return workers.public(workers.get(identifier))

    @app.post("/api/worker/jobs/{identifier}/cancel", status_code=202)
    async def cancel(identifier: str, body: WorkerAction):
        return await workers.cancel(identifier)

    @app.post("/api/worker/jobs/{identifier}/reconcile", status_code=202)
    async def reconcile(identifier: str, body: WorkerAction):
        workers.get(identifier)
        workers.wake()
        return workers.public(workers.get(identifier))

    @app.post("/api/worker/jobs/{identifier}/handoff")
    async def handoff(identifier: str, body: WorkerAction):
        return await workers.handoff(identifier)

    @app.get("/api/worker/jobs/{identifier}/artifacts/{artifact_id}")
    async def artifact(identifier: str, artifact_id: str):
        return await workers.artifact(identifier, artifact_id)

    @app.post("/api/workers/{worker}/preferences")
    async def preferences(worker: WorkerId, body: WorkerPreference, request: Request):
        authority = workers.authority(request=request)
        async with workers.advance_lock, workers.submit_lock:
            if workers.store.setting("worker_lease") or workers.store.setting("worker_auth_pending"):
                raise WorkerError("coding_job_must_stop_before_preferences")
            capabilities = await workers.capabilities()
            capability = next(c for c in capabilities["workers"] if c["worker_id"] == worker)
            provider = next((p for p in capability["providers"] if p["id"] == body.provider and p["supported"]), None)
            if not provider or body.model and body.model not in provider["models"]:
                raise WorkerError("unsupported_coding_preference", 422)
            if not workers.authorized(authority):
                raise WorkerError("worker_authority_revoked", 403)
            values = workers.store.setting("worker_preferences", {})
            workers.store.set_settings({"worker_preferences": {**values, worker: body.model_dump()},
                                       "worker_auth_epoch": uuid.uuid4().hex})
            return {"worker_id": worker, **body.model_dump()}

    @app.post("/api/workers/{worker}/providers/codex/auth", status_code=202)
    async def authenticate(worker: WorkerId, body: WorkerAuth, request: Request):
        authority = workers.authority(request=request)
        return await workers.role_auth(worker, "POST", "/coding/providers/codex/auth",
            {"method": body.method, "secret": body.secret.get_secret_value()},
            key=request.headers.get("idempotency-key", ""), commit_guard=lambda: workers.require_owner(authority))

    @app.get("/api/workers/{worker}/auth/{identifier}")
    async def auth_status(worker: WorkerId, identifier: str):
        if not IDENTIFIER.fullmatch(identifier):
            raise HTTPException(422, "invalid_auth_id")
        return await workers.role_auth(worker, "GET", "/coding/auth/" + identifier)

    @app.post("/api/workers/{worker}/auth/{identifier}/cancel")
    async def auth_cancel(worker: WorkerId, identifier: str, body: WorkerAction, request: Request):
        if not IDENTIFIER.fullmatch(identifier):
            raise HTTPException(422, "invalid_auth_id")
        authority = workers.authority(request=request)
        return await workers.role_auth(worker, "POST", "/coding/auth/" + identifier + "/cancel", {},
                                       commit_guard=lambda: workers.require_owner(authority))

    @app.post("/api/workers/{worker}/providers/codex/logout")
    async def logout(worker: WorkerId, body: WorkerAction, request: Request):
        authority = workers.authority(request=request)
        return await workers.role_auth(worker, "POST", "/coding/providers/codex/logout", {},
                                       commit_guard=lambda: workers.require_owner(authority))
