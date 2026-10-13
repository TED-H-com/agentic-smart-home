"""Durable, private device-login handles; credentials stay in a Hermes-only home."""

import asyncio
import contextlib
import hashlib
import json
import re
import shutil
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

TERMINAL = {"configured", "failed", "cancelled", "expired", "interrupted"}
LOGIN_REASONS = {"access_denied", "slow_down", "expired_token", "oauth_failed", "stop_unconfirmed"}
_STDERR_REASONS = (
    ("access_denied", "access_denied"),
    ("slow_down", "slow_down"),
    ("expired_token", "expired_token"),
    ("device_code_timeout", "expired_token"),
)


def public(item):
    view = {key: value for key, value in item.items() if key not in {"epoch", "created_at", "updated_at"}}
    # `error` stays for existing clients. `reason` is the same stable code.
    if "error" in view and "reason" not in view:
        view["reason"] = view["error"]
    return view


def reason_from_stderr(raw):
    """Classify a bounded stderr capture. The raw text is never returned."""
    if not raw:
        return None
    text = raw.decode("utf-8", "replace").lower()
    for token, reason in _STDERR_REASONS:
        if token in text:
            return reason
    if "429" in text:
        return "slow_down"
    return None


def failure(code):
    raise HTTPException(409, {"code": code, "message": "Hermes authentication could not complete"})


class DeviceLogin:
    def __init__(self, runtime):
        self.runtime = runtime
        for row in runtime.db.execute("SELECT body FROM records WHERE kind='auth'").fetchall():
            item = json.loads(row[0])
            if item["state"] == "stop_unconfirmed":
                runtime.unconfirmed.add(item["auth_id"])
            elif item["state"] not in TERMINAL:
                self.update(item, "interrupted", "runtime_restarted")

    def update(self, item, state, error=None, **values):
        item.update(state=state, error=error, updated_at=datetime.now(UTC).isoformat(), **values)
        if state != "pending":
            item.update(user_code=None, verification_uri=None)
        self.runtime.put("auth", item["auth_id"], item)
        return item

    def find(self, identifier):
        item = self.runtime.get("auth", identifier)
        if not item:
            raise HTTPException(404, {"code": "not_found", "message": "Login handle not found"})
        if item["epoch"] != self.runtime.auth.get("epoch") and item["state"] == "configured":
            self.update(item, "interrupted", "account_changed")
        return item

    def active_pending(self):
        """The login a new tab must resume. Pending state is enough; expiry is the child's."""
        chosen = None
        for row in self.runtime.db.execute("SELECT body FROM records WHERE kind='auth'"):
            item = json.loads(row[0])
            if item.get("state") != "pending" or item["auth_id"] in self.runtime.unconfirmed:
                continue
            if chosen is None or item.get("created_at", "") >= chosen.get("created_at", ""):
                chosen = item
        return chosen

    async def begin(self, body, key):
        runtime = self.runtime
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        previous = runtime.previous("auth", key, digest)
        if previous:
            return public(self.find(previous["auth_id"]))
        async with runtime.account_change(stop=False):
            # Check again after the mutation lock: concurrent retries must not
            # start two refresh-token families or overwrite each other's handle.
            previous = runtime.previous("auth", key, digest)
            if previous:
                return public(self.find(previous["auth_id"]))
            # A closed tab loses its auth id. Resuming must not mint a new code
            # or wipe a device grant the user may already have approved.
            existing = self.active_pending()
            if existing:
                return public(self.find(existing["auth_id"]))
            await runtime.stop_all()
            runtime.clear_account()
            identifier, epoch = uuid.uuid4().hex, uuid.uuid4().hex
            runtime.auth = {"provider": body.provider, "model": body.model, "epoch": epoch,
                            "state": "needs_auth", "verified_at": None}
            runtime.save_auth()
            home = runtime.directory / "oauth" / epoch
            home.mkdir(parents=True, mode=0o700)
            item = {"auth_id": identifier, "epoch": epoch, "provider": body.provider, "model": body.model,
                    "state": "pending", "verification_uri": None, "user_code": None,
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=900)).isoformat(),
                    "created_at": datetime.now(UTC).isoformat(), "error": None}
            runtime.put("auth", identifier, item)
            runtime.remember("auth", key, digest, identifier)
            task = asyncio.create_task(self.execute(item, home))
            runtime.tasks[identifier] = task
            task.add_done_callback(lambda _: runtime.tasks.pop(identifier, None))
            return public(item)

    async def invoke(self, item, home):
        runtime = self.runtime
        scratch = runtime.directory / "turns" / item["auth_id"]
        for folder in (scratch, scratch / "tmp"):
            folder.mkdir(mode=0o700, exist_ok=True)
        env = runtime.child_environment(scratch)
        env["HERMES_HOME"] = str(home)
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            runtime.python, "-m", "agentic_hermes.oauth_child", cwd=scratch, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        ))
        try:
            proc = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            runtime.track_process(item["auth_id"], await spawning)
            raise
        runtime.track_process(item["auth_id"], proc)
        stderr = bytearray()

        async def drain_stderr():
            assert proc.stderr
            while chunk := await proc.stderr.read(4096):
                if len(stderr) < 4096:
                    stderr.extend(chunk[:4096 - len(stderr)])

        stderr_task = asyncio.create_task(drain_stderr())
        count, final, child_error = 0, None, None
        try:
            while line := await proc.stdout.readline():
                count += len(line)
                if count > 8192:
                    raise ValueError("auth_output_limit")
                event = json.loads(line)
                if event.get("state") == "pending":
                    if event.get("verification_uri") != "https://auth.openai.com/codex/device" or not re.fullmatch(r"[A-Z0-9]{4,8}-[A-Z0-9]{4,8}", event.get("user_code", "")):
                        raise ValueError("invalid_auth_instructions")
                    self.update(item, "pending", verification_uri=event["verification_uri"], user_code=event["user_code"])
                elif event.get("state") in {"configured", "failed", "expired"}:
                    final = event["state"]
                    reason = event.get("reason") or event.get("error")
                    child_error = reason if reason in LOGIN_REASONS else None
                else:
                    raise ValueError("invalid_auth_event")
            await proc.wait()
        finally:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
        # stderr is only a fallback when the child died before a JSON reason.
        # The bytes themselves are discarded.
        if final is None:
            child_error = reason_from_stderr(stderr)
            if child_error == "expired_token":
                final = "expired"
            elif child_error:
                final = "failed"
        del stderr[:]
        # A classified JSON or stderr reason survives a non-zero exit. An
        # unclassified crash stays oauth_failed and never includes stderr text.
        if final is None:
            raise ValueError("auth_child_failed")
        return final, child_error

    async def execute(self, item, home):
        runtime, identifier = self.runtime, item["auth_id"]
        outcome, error = "failed", "oauth_failed"
        try:
            async with asyncio.timeout(900):
                result = await self.invoke(item, home)
            # Tests and older callers return the state string. The child returns (state, reason).
            if isinstance(result, tuple):
                outcome, child_error = result
            else:
                outcome, child_error = result, None
            if outcome == "configured" and not (home / "auth.json").is_file():
                raise ValueError("auth_store_missing")
            if outcome == "configured":
                error = None
            elif child_error in LOGIN_REASONS:
                error = child_error
                if error == "expired_token":
                    outcome = "expired"
            elif outcome == "expired":
                error = "expired_token"
            else:
                outcome, error = "failed", "oauth_failed"
        except asyncio.CancelledError:
            outcome, error = ("interrupted", "runtime_restarted") if runtime._closing else ("cancelled", None)
        except TimeoutError:
            outcome, error = "expired", "expired_token"
        except (OSError, ValueError, KeyError):
            pass
        finally:
            try:
                await runtime.stop_process(identifier)
            except TimeoutError:
                runtime.unconfirmed.add(identifier)
                outcome, error = "stop_unconfirmed", "stop_unconfirmed"
            if outcome == "configured" and runtime.auth.get("epoch") == item["epoch"]:
                runtime.auth.update(state="configured", verified_at=None)
                runtime.save_auth()
            self.update(item, outcome, error)
            if outcome != "stop_unconfirmed":
                shutil.rmtree(runtime.directory / "turns" / identifier, ignore_errors=True)
                if outcome != "configured":
                    shutil.rmtree(home, ignore_errors=True)

    async def cancel(self, item):
        runtime, identifier = self.runtime, item["auth_id"]
        if item["state"] in TERMINAL:
            return public(item)
        task = runtime.tasks.get(identifier)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = self.find(identifier)
        if current["state"] not in TERMINAL:
            if identifier in runtime.processes:
                try:
                    await runtime.stop_process(identifier)
                    self.update(current, "cancelled")
                except TimeoutError:
                    runtime.unconfirmed.add(identifier)
                    self.update(current, "stop_unconfirmed", "stop_unconfirmed")
            elif identifier not in runtime.unconfirmed:
                self.update(current, "cancelled")
        return public(self.find(identifier))
