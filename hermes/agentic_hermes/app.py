"""Durable service-side sessions/runs around the real upstream Hermes process.

This listener is private to Controller. Its bearer is not an Owner credential;
MCP uses a different, per-run grant and still authorizes every operation there.
"""

import asyncio
import ctypes
import hashlib
import hmac
import json
import os
import shutil
import signal
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import TOOLS, AuthInput, RetentionInput, RunInput, SessionInput

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
PRIVATE = {"scope_id", "digest", "key", "message", "auth_epoch"}
MAX_HISTORY = 192 * 1024


def now():
    return datetime.now(UTC).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fail(status, code, message):
    raise HTTPException(status, {"code": code, "message": message})


def public(run):
    return {k: v for k, v in run.items() if k not in PRIVATE}


def scrub(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [scrub(item, secrets) for item in value]
    if isinstance(value, dict):
        return {scrub(key, secrets): scrub(item, secrets) for key, item in value.items()}
    return value


def process_info(pid):
    try:
        path = Path(f"/proc/{pid}")
        if path.stat().st_uid != os.getuid():
            return None
        fields = (path / "stat").read_text().rsplit(") ", 1)[1].split()
        return {"state": fields[0], "parent": int(fields[1]), "group": int(fields[2]),
                "session": int(fields[3]), "start": int(fields[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


def group_members(group):
    members = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            info = process_info(int(path.name))
            if info and info["group"] == group and info["session"] == group and info["state"] not in {"Z", "X"}:
                members[int(path.name)] = info["start"]
    return members


def signal_members(members, value):
    """Signals reference kernel process handles, never a possibly recycled PID."""
    # Some uv-managed CPython builds omit the Python wrappers even on a
    # pidfd-capable Linux kernel. Bookworm's libc exposes the same safe API.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes, libc.pidfd_open.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    libc.pidfd_send_signal.restype = ctypes.c_int
    for pid, started in members.items():
        try:
            fd = libc.pidfd_open(pid, 0)
            if fd < 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        except ProcessLookupError:
            continue
        try:
            info = process_info(pid)
            if info and info["start"] == started:
                with suppress(ProcessLookupError):
                    if libc.pidfd_send_signal(fd, value, None, 0) != 0:
                        error = ctypes.get_errno()
                        raise OSError(error, os.strerror(error))
        finally:
            os.close(fd)


class Runtime:
    def __init__(self, directory, token, *, upstream=None, mcp_url="http://127.0.0.1:8097/mcp", python=None):
        if len(token) < 32:
            raise ValueError("A unique service bearer of at least 32 characters is required")
        self.directory, self.token = Path(directory), token
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.upstream = Path(upstream or os.environ.get("AGENTIC_HERMES_UPSTREAM", "/opt/hermes"))
        self.python = python or sys.executable
        self.mcp_url = mcp_url
        self.db = sqlite3.connect(self.directory / "runtime.sqlite")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id));
          CREATE TABLE IF NOT EXISTS requests(kind TEXT,key TEXT,digest TEXT,id TEXT,PRIMARY KEY(kind,key));
          CREATE TABLE IF NOT EXISTS events(run TEXT,sequence INTEGER,body TEXT,PRIMARY KEY(run,sequence));
        """)
        self.tasks, self.processes, self.groups = {}, {}, {}
        self.unconfirmed = set()
        self._closing = False
        self.context_days = RetentionInput.model_validate(
            self.get("setting", "retention") or {"context_days": 7}
        ).context_days
        self.auth_path = self.directory / "provider.json"
        self.auth = json.loads(self.auth_path.read_text()) if self.auth_path.exists() else {}
        self.lock = json.loads((Path(__file__).parents[1] / "hermes.lock.json").read_text())
        for row in self.db.execute("SELECT body FROM records WHERE kind='run'").fetchall():
            run = json.loads(row[0])
            if run["state"] == "stop_unconfirmed":
                self.unconfirmed.add(run["run_id"])
            elif run["state"] not in TERMINAL:
                self.transition(run, "interrupted", "runtime_restarted")
        # Unconfirmed groups retain their scratch and quarantine across restart.
        turns = self.directory / "turns"
        turns.mkdir(mode=0o700, exist_ok=True)
        for folder in turns.iterdir():
            if folder.name not in self.unconfirmed:
                shutil.rmtree(folder, ignore_errors=True)
        self.prune()

    def prune(self):
        """Configured context TTL, including completed answer/evidence payloads.

        Minimal request digests remain to prevent replay after content removal.
        Active turns are left alone and have a hard five-minute deadline.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=self.context_days)).isoformat()
        active = {run_id for run_id in self.tasks}
        with self.db:
            for kind, identifier, encoded in self.db.execute("SELECT kind,id,body FROM records").fetchall():
                item = json.loads(encoded)
                if kind == "setting" or identifier in active or identifier in self.unconfirmed:
                    continue
                if item.get("created_at", item.get("updated_at", now())) < cutoff:
                    self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, identifier))
                    if kind == "run":
                        self.db.execute("DELETE FROM events WHERE run=?", (identifier,))

    def get(self, kind, identifier):
        row = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, identifier, body):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, identifier, json.dumps(body)))

    def remember(self, kind, key, body_digest, identifier):
        with self.db:
            self.db.execute("INSERT INTO requests VALUES(?,?,?,?)", (kind, key, body_digest, identifier))

    def previous(self, kind, key, body_digest):
        row = self.db.execute("SELECT digest,id FROM requests WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row:
            if not hmac.compare_digest(row[0], body_digest):
                fail(409, "idempotency_conflict", "This key belongs to another request")
            item = self.get(kind, row[1])
            if item is None:
                fail(410, "expired", "The original record has expired; it will not be replayed")
            return item

    def event(self, run, event):
        cursor = self.db.execute("SELECT coalesce(max(sequence),0) FROM events WHERE run=?", (run["run_id"],)).fetchone()[0]
        run["event_cursor"] = cursor + 1
        row = {"sequence": run["event_cursor"], "event": event, "state": run["state"], "at": now()}
        with self.db:
            self.db.execute("INSERT INTO events VALUES(?,?,?)", (run["run_id"], row["sequence"], json.dumps(row)))

    def transition(self, run, state, error=None):
        run.update(state=state, error=error, updated_at=now())
        self.event(run, state)
        self.put("run", run["run_id"], run)

    def capabilities(self):
        return {
            "protocol": 1, "runtime": "hermes", "upstream_commit": self.lock["commit"],
            "state": self.auth.get("state", "needs_auth"), "provider": "openrouter",
            "execution_state": "stop_unconfirmed" if self.unconfirmed else "busy" if self.tasks else "idle",
            "model": self.auth.get("model", ""), "auth_verified_at": self.auth.get("verified_at"),
            "features": {"sessions": True, "async_runs": True, "events": True, "cancel": True, "mcp": True},
            "tools": list(TOOLS),
            "context_days": self.context_days,
        }

    async def set_retention(self, body):
        if self.unconfirmed:
            fail(409, "stop_unconfirmed", "A prior process group has not been confirmed stopped")
        if body.context_days < self.context_days:
            # Stop inference before its in-memory history could repopulate a
            # session which the reduced policy is about to erase.
            await self.stop_all()
        self.put("setting", "retention", body.model_dump())
        self.context_days = body.context_days
        self.prune()
        return {"context_days": self.context_days}

    def save_auth(self):
        temp = self.auth_path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(self.auth, file)
        os.replace(temp, self.auth_path)

    async def authenticate(self, body):
        await self.stop_all()
        self.auth = {} if body is None else {
            "provider": body.provider, "model": body.model,
            "api_key": body.api_key.get_secret_value(), "epoch": uuid.uuid4().hex,
            "state": "configured", "verified_at": None,
        }
        self.save_auth()
        # Revocation includes runtime context; deduplication tombstones remain.
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind='session'")
        return self.capabilities()

    def session(self, identifier, scope):
        item = self.get("session", identifier)
        if not item:
            fail(404, "not_found", "Session not found")
        if not hmac.compare_digest(scope.encode(), item["scope_id"].encode()):
            fail(403, "scope_mismatch", "Session is outside this scope")
        if item["auth_epoch"] != self.auth.get("epoch", ""):
            fail(409, "session_expired", "Account changed; create a new conversation")
        return item

    def find_run(self, identifier, scope):
        run = self.get("run", identifier)
        if not run:
            fail(404, "not_found", "Run not found")
        if not hmac.compare_digest(scope.encode(), run["scope_id"].encode()):
            fail(403, "scope_mismatch", "Run is outside this scope")
        return run

    def create_session(self, body, key):
        self.prune()
        item = self.previous("session", key, digest(body.model_dump()))
        if item:
            self.session(item["session_id"], body.scope_id)
            return {k: item[k] for k in ("session_id", "scope_id", "created_at")}
        item = {
            "session_id": uuid.uuid4().hex, "scope_id": body.scope_id, "created_at": now(),
            "auth_epoch": self.auth.get("epoch", ""), "history": [],
        }
        if self.db.execute("SELECT count(*) FROM records WHERE kind='session'").fetchone()[0] >= 1000:
            fail(429, "capacity", "Runtime conversation capacity reached")
        self.put("session", item["session_id"], item)
        self.remember("session", key, digest(body.model_dump()), item["session_id"])
        return {k: item[k] for k in ("session_id", "scope_id", "created_at")}

    def submit(self, session_id, body, key):
        self.prune()
        session = self.session(session_id, body.scope_id)
        payload_digest = digest({"session_id": session_id, **body.model_dump(exclude={"mcp_token"})})
        previous = self.previous("run", key, payload_digest)
        if previous:
            return public(previous)
        if self.unconfirmed:
            fail(409, "stop_unconfirmed", "A prior process group has not been confirmed stopped")
        previous = self.get("run", body.run_id)
        if previous:
            if previous["digest"] != payload_digest:
                fail(409, "idempotency_conflict", "Run ID belongs to another request")
            return public(previous)
        if not self.auth.get("api_key"):
            fail(503, "needs_auth", "Configure the Hermes model account first")
        if self.auth.get("state") == "needs_reauth":
            fail(503, "needs_reauth", "The model account must be authenticated again")
        if not (self.upstream / "run_agent.py").is_file():
            fail(503, "runtime_unavailable", "Pinned Hermes runtime is unavailable")
        # One model run at a time is the initial laptop resource budget.
        if self.tasks:
            fail(409, "session_busy", "A Hermes run is already active; resume after its handle completes")
        if self.db.execute("SELECT count(*) FROM records WHERE kind='run'").fetchone()[0] >= 10000:
            fail(429, "capacity", "Runtime run capacity reached")
        run = {
            "run_id": body.run_id, "session_id": session_id, "case_id": body.case_id,
            "scope_id": body.scope_id, "digest": payload_digest, "state": "queued", "answer": "",
            "error": None, "created_at": now(), "updated_at": now(), "event_cursor": 0, "usage": None,
        }
        self.put("run", body.run_id, run)
        self.remember("run", key, payload_digest, body.run_id)
        self.event(run, "queued")
        self.put("run", body.run_id, run)
        task = asyncio.create_task(self.execute(run, body, session))
        self.tasks[body.run_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(body.run_id, None))
        return public(run)

    def child_environment(self, scratch):
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(scratch),
            "HERMES_HOME": str(scratch / ".hermes"), "TMPDIR": str(scratch / "tmp"),
            "PYTHONPATH": f"{Path(__file__).parents[1]}:{self.upstream}",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "AGENTIC_MCP_URL": self.mcp_url, "HERMES_MAX_ITERATIONS": "12",
            "AGENTIC_PARENT_PID": str(os.getpid()),
        }

    def track_process(self, identifier, proc):
        self.processes[identifier] = proc
        leader = process_info(proc.pid)
        known = group_members(proc.pid) if leader and leader["parent"] == os.getpid() else {}
        self.groups[identifier] = {"pid": proc.pid, "members": known}

    async def invoke(self, payload, scratch, identifier):
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            self.python, "-m", "agentic_hermes.child", cwd=scratch,
            env=self.child_environment(scratch), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        ))
        try:
            proc = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            self.track_process(identifier, await spawning)
            raise
        self.track_process(identifier, proc)
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(payload).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        output = bytearray()
        while chunk := await proc.stdout.read(16384):
            output.extend(chunk)
            if len(output) > 512 * 1024:
                raise ValueError("output_limit")
        await proc.wait()
        if proc.returncode:
            raise ValueError("runtime_failed")
        return json.loads(output)

    async def stop_process(self, identifier):
        proc = self.processes.get(identifier)
        if proc is None:
            return
        try:
            group = self.groups[identifier]
            current = group_members(proc.pid)
            if current:
                if not any(current.get(pid) == started for pid, started in group["members"].items()):
                    raise TimeoutError("Process group identity is no longer confirmed")
                group["members"].update(current)
                signal_members(current, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(proc.wait()), 2)
            # Leader exit is insufficient. Freeze the remaining group, then kill
            # every member through pidfds; stopped members anchor its identity.
            for _ in range(100):
                current = group_members(proc.pid)
                if not current:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), 1)
                    self.processes.pop(identifier, None)
                    self.groups.pop(identifier, None)
                    self.unconfirmed.discard(identifier)
                    return
                if not any(current.get(pid) == started for pid, started in group["members"].items()):
                    raise TimeoutError("Process group identity is no longer confirmed")
                for _ in range(8):
                    signal_members(current, signal.SIGSTOP)
                    group["members"].update(current)
                    frozen = group_members(proc.pid)
                    stopped = all(
                        (info := process_info(pid)) is None or info["state"] in {"T", "t", "Z", "X"}
                        for pid in frozen
                    )
                    if frozen == current and stopped:
                        signal_members(frozen, signal.SIGKILL)
                        break
                    current = frozen
                await asyncio.sleep(0.02)
            raise TimeoutError("Process group did not stop")
        except (OSError, KeyError, AttributeError) as exc:
            raise TimeoutError("Process group stop cannot be confirmed") from exc

    def mark_unconfirmed(self, run):
        self.unconfirmed.add(run["run_id"])
        if run["state"] != "stop_unconfirmed":
            self.transition(run, "stop_unconfirmed", "stop_unconfirmed")

    async def execute(self, run, body, session):
        scratch = self.directory / "turns" / body.run_id
        for folder in (scratch, scratch / ".hermes", scratch / "tmp"):
            folder.mkdir(mode=0o700)
        self.transition(run, "running")
        try:
            async with asyncio.timeout(300):
                result = await self.invoke({
                    "run_id": body.run_id, "session_id": run["session_id"],
                    "message": body.message, "history": session["history"],
                    "model": self.auth["model"], "api_key": self.auth["api_key"],
                    "mcp_token": body.mcp_token.get_secret_value(),
                }, scratch, body.run_id)
            await self.stop_process(body.run_id)
            # Exact secret scrubbing is a last boundary, not permission to send secrets to the model.
            result = scrub(result, (self.token, self.auth["api_key"], body.mcp_token.get_secret_value()))
            error = result.get("error")
            if error:
                if error == "needs_reauth":
                    self.auth.update(state="needs_reauth", verified_at=None)
                    self.save_auth()
                self.transition(run, "failed", error if error in {"needs_reauth", "runtime_unavailable", "inference_failed"} else "inference_failed")
                return
            if not isinstance(result.get("answer"), str) or len(result["answer"].encode()) > 32768:
                raise ValueError("answer_limit")
            history = result.get("history", [])
            if not isinstance(history, list) or len(json.dumps(history).encode()) > MAX_HISTORY:
                raise ValueError("context_limit")
            session["history"] = history
            session["updated_at"] = now()
            self.put("session", session["session_id"], session)
            run.update(answer=result["answer"], usage=result.get("usage"))
            self.auth.update(state="ready", verified_at=now())
            self.save_auth()
            self.transition(run, "succeeded")
        except asyncio.CancelledError:
            try:
                await self.stop_process(body.run_id)
                self.transition(run, "interrupted" if self._closing else "cancelled")
            except TimeoutError:
                self.mark_unconfirmed(run)
            raise
        except (OSError, ValueError, KeyError, TimeoutError):
            try:
                await self.stop_process(body.run_id)
                self.transition(run, "failed", "runtime_unavailable")
            except TimeoutError:
                self.mark_unconfirmed(run)
        finally:
            if body.run_id not in self.unconfirmed:
                try:
                    await self.stop_process(body.run_id)
                    shutil.rmtree(scratch, ignore_errors=True)
                except TimeoutError:
                    self.mark_unconfirmed(run)

    async def cancel(self, run):
        task = self.tasks.get(run["run_id"])
        if task and run["state"] not in TERMINAL:
            self.transition(run, "cancel_requested")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before entering execute has no child process.
            current = self.get("run", run["run_id"])
            if current["state"] == "cancel_requested":
                if run["run_id"] in self.processes or run["run_id"] in self.unconfirmed:
                    self.mark_unconfirmed(current)
                else:
                    self.transition(current, "cancelled")
        elif run["state"] == "stop_unconfirmed" and run["run_id"] in self.processes:
            try:
                await self.stop_process(run["run_id"])
                self.transition(run, "cancelled")
            except TimeoutError:
                self.mark_unconfirmed(run)
        return public(self.get("run", run["run_id"]))

    async def stop_all(self, *, require_confirmed=True):
        for identifier in set(self.tasks) | self.unconfirmed:
            await self.cancel(self.get("run", identifier))
        if require_confirmed and self.unconfirmed:
            fail(409, "stop_unconfirmed", "A prior process group has not been confirmed stopped")

    async def close(self):
        self._closing = True
        await self.stop_all(require_confirmed=False)
        self.db.close()


def create_app(runtime):
    @asynccontextmanager
    async def lifespan(app):
        yield
        await runtime.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    @app.middleware("http")
    async def protect(request, call_next):
        if request.headers.get("origin") or not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + runtime.token).encode()
        ):
            return JSONResponse({"detail": {"code": "service_auth", "message": "Private service authentication required"}}, status_code=401)
        # Actual bytes as well as claimed Content-Length are bounded.
        size, chunks = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 128 * 1024:
                return JSONResponse({"detail": {"code": "body_limit", "message": "Request too large"}}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        runtime.prune()
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        # Pydantic's default error response echoes the input, including api_key.
        return JSONResponse({"detail": {"code": "invalid_request", "message": "Request does not match the runtime contract"}}, status_code=422)

    def key(request):
        value = request.headers.get("idempotency-key", "")
        if not value or len(value) > 200:
            fail(422, "idempotency_required", "Supply an Idempotency-Key of at most 200 characters")
        return value

    @app.get("/v1/capabilities")
    async def capabilities():
        return runtime.capabilities()

    @app.post("/v1/auth")
    async def authenticate(body: AuthInput):
        return await runtime.authenticate(body)

    @app.post("/v1/auth/logout")
    async def logout():
        return await runtime.authenticate(None)

    @app.post("/v1/retention")
    async def retention(body: RetentionInput):
        return await runtime.set_retention(body)

    @app.post("/v1/sessions")
    async def create_session(body: SessionInput, request: Request):
        return runtime.create_session(body, key(request))

    @app.post("/v1/sessions/{identifier}/runs", status_code=202)
    async def submit(identifier: str, body: RunInput, request: Request):
        return runtime.submit(identifier, body, key(request))

    @app.get("/v1/runs/{identifier}")
    async def status(identifier: str, scope_id: str):
        return public(runtime.find_run(identifier, scope_id))

    @app.get("/v1/runs/{identifier}/events")
    async def events(identifier: str, scope_id: str, after: int = 0):
        run = runtime.find_run(identifier, scope_id)
        rows = runtime.db.execute("SELECT body FROM events WHERE run=? AND sequence>? ORDER BY sequence LIMIT 100", (identifier, max(0, after))).fetchall()
        return {"events": [json.loads(row[0]) for row in rows], "cursor": run["event_cursor"]}

    @app.post("/v1/runs/{identifier}/cancel")
    async def cancel(identifier: str, body: SessionInput):
        return await runtime.cancel(runtime.find_run(identifier, body.scope_id))

    return app


def main():
    import uvicorn

    os.umask(0o077)
    token = Path(os.environ.get("AGENTIC_HERMES_TOKEN_FILE", "/hermes-link/service-token")).read_text().strip()
    runtime = Runtime(
        os.environ.get("AGENTIC_HERMES_DATA", "/data"), token,
        mcp_url=os.environ.get("AGENTIC_MCP_URL", "http://127.0.0.1:8097/mcp"),
    )
    uvicorn.run(create_app(runtime), host="127.0.0.1", port=8096, access_log=False)


if __name__ == "__main__":
    main()
