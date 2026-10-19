"""Outer Linux authentication and typed host-broker client, independent of HA availability."""
import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
import uuid
from pathlib import Path
from typing import Literal

import aiohttp
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from .durability import client_session, durable, file_io
from .ha import HAError, HomeAssistant
from .models import Strict
from .runner import RunnerError, cancel_and_confirm

STOP_UNCONFIRMED = "還在確認查詢已停止，請稍後再問一次。"


class Unlock(Strict):
    key: str = Field(min_length=20, max_length=200)


class DesktopLogin(Strict):
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class ConnectHA(Strict):
    token: str = Field(min_length=20, max_length=16384)


class HostAction(Strict):
    action: str = Field(pattern="^(provision|start|shutdown|snapshot|restore|configure_lan)$")
    snapshot: str | None = Field(default=None, pattern=r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
    interface_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def scoped_fields(self):
        if self.action == "configure_lan":
            if "interface_id" not in self.model_fields_set or self.snapshot is not None:
                raise ValueError("configure_lan requires an explicit interface choice and no snapshot")
        elif "interface_id" in self.model_fields_set:
            raise ValueError("interface_id is only valid for configure_lan")
        return self



class HostQuestion(Strict):
    message: str = Field(min_length=1, max_length=5000)
    provider: Literal["codex", "claude", "agy", "grok"] = "codex"


class HostAnswer(Strict):
    message: str
    suggested_action: Literal["none", "provision", "start", "shutdown", "snapshot"]


class HostSession:
    lifetime = 43200

    def __init__(self, root, secret, store):
        self.root, self.secret, self.attempts = Path(root), secret.encode(), []
        self.store = store

    def sign(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def binding(self, *, expected_key=None):
        """Observe key replacement before accepting a session or issuing one.

        Only a keyed fingerprint is persisted. Once a replacement is observed,
        restoring an older key cannot resurrect its deleted sessions.
        """
        try:
            key = (self.root / "access-key").read_text().strip()
        except (OSError, UnicodeError):
            return None
        if not key:
            return None
        if expected_key is not None and not hmac.compare_digest(expected_key.encode(), key.encode()):
            raise HTTPException(403, "主機登入金鑰不正確")
        fingerprint = self.sign("host-access-key:" + key)
        previous = self.store.setting("host_session_access_key")
        if fingerprint != previous:
            if self.store.failed:
                return None
            from .models import canonical
            settings = {"host_session_access_key": fingerprint}
            if previous is not None:
                # Pending Web work must also lose its old authority. First
                # enrollment must not disturb an existing HA bridge proof.
                settings["hermes_web_epoch"] = uuid.uuid4().hex
            with self.store.transaction() as tx:
                tx.execute("DELETE FROM records WHERE kind='host_session'")
                tx.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                               [(name, canonical(value)) for name, value in settings.items()])
        return fingerprint

    def scope(self, request):
        cookie = request.cookies.get("agentic_host", "")
        # Stateless pre-fix cookies deliberately require a fresh login. They
        # cannot be upgraded safely because previously copied cookies survive.
        if not re.fullmatch(r"[0-9]{1,12}\.[A-Za-z0-9_-]{43}\.[a-f0-9]{64}", cookie):
            return None
        try:
            expires, nonce, signature = cookie.split(".")
            now = time.time()
            if (not now < int(expires) <= now + self.lifetime + 60 or
                    not hmac.compare_digest(signature, self.sign("host-session:v1:" + expires + "." + nonce))):
                return None
            binding = self.binding()
            identifier = hashlib.sha256(cookie.encode()).hexdigest()
            record = self.store.get("host_session", identifier)
            if (binding and record and record.get("version") == 1 and record.get("expires") == int(expires)
                    and record.get("access_key") == binding):
                return identifier
        except (ValueError, TypeError):
            pass
        return None

    def verify(self, request):
        return self.scope(request) is not None

    async def authenticate(self, request):
        previous = self.store.setting("host_session_access_key")
        identifier = self.scope(request)
        if previous != self.store.setting("host_session_access_key"):
            # Key replacement can first be observed by a GET. Commit its
            # revocations even though the normal response barrier only waits
            # for mutations. A changed binding always removed the session.
            try:
                await durable(self.store)
            except RuntimeError:
                raise HTTPException(503, "主機登入狀態無法保存，請稍後再試") from None
        return identifier

    def revoke(self, request):
        identifier = hashlib.sha256(request.cookies.get("agentic_host", "").encode()).hexdigest()
        with self.store.transaction() as tx:
            tx.execute("DELETE FROM records WHERE kind='host_session' AND id=?", (identifier,))

    def login(self, key):
        self.attempts = [t for t in self.attempts if t > time.time() - 60]
        if len(self.attempts) >= 8:
            raise HTTPException(429, "請稍後再試")
        self.attempts.append(time.time())
        return self.cookie(key=key)

    def desktop_login(self, ticket):
        # The installer-owned fixed directory is the only ticket source. Truncate
        # under flock before issuing a cookie so concurrent open descriptors cannot replay.
        try:
            fd = os.open(self.root / "desktop-ticket.json", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "r+") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 4096:
                    raise ValueError("invalid ticket file")
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                value = json.loads(stream.read(4097))
                expires, expected = value.get("expires"), value.get("ticket_hash")
                if (type(expires) not in {int, float} or not time.time() < expires <= time.time() + 120
                        or not isinstance(expected, str) or len(expected) != 64
                        or not hmac.compare_digest(hashlib.sha256(ticket.encode()).hexdigest(), expected)):
                    raise ValueError("invalid desktop ticket")
                stream.seek(0)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError, TypeError, AttributeError):
            raise HTTPException(403, "桌面登入已失效，請重新從桌面開啟管理頁面") from None
        # The file operation runs off the event loop. Session persistence must
        # be done by its single Controller owner after returning to the loop.

    def cookie(self, *, key=None):
        binding = self.binding(expected_key=key)
        if not binding:
            raise HTTPException(503, "主機登入暫時無法使用，請稍後再試")
        now = time.time()
        expires = str(int(now) + self.lifetime)
        value = expires + "." + secrets.token_urlsafe(32)
        value += "." + self.sign("host-session:v1:" + value)
        with self.store.transaction() as tx:
            tx.execute("DELETE FROM records WHERE kind='host_session' AND json_extract(body,'$.expires')<=?", (now,))
        self.store.put("host_session", {"id": hashlib.sha256(value.encode()).hexdigest(), "version": 1,
            "expires": int(expires), "access_key": binding})
        response = JSONResponse({"authenticated": True})
        response.set_cookie("agentic_host", value, httponly=True,
                            samesite="strict", max_age=self.lifetime, path="/")
        return response


async def host_call(method="GET", body=None, *, store):
    return await _host_call(method, "/status" if method == "GET" else "/actions", body, store=store)


async def host_bridge_contract(*, store):
    """Fresh installed-updater proof from the fixed broker, without VM/HA status I/O."""
    try:
        return await _host_call("GET", "/bridge-contract", store=store)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    # The previous broker proved the same updater contract in /status. Only an
    # absent route selects it; IngressBridge bounds both reads together to 5 s.
    return await _host_call("GET", "/status", store=store)


async def _host_call(method, path, body=None, *, store):
    connector = aiohttp.UnixConnector(path="/run/agentic-home/host.sock")
    try:
        async with client_session(store, connector=connector, timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.request(method, "http://localhost" + path, json=body, allow_redirects=False) as response:
                result = await response.json()
                if response.status >= 300:
                    raise HTTPException(response.status, result.get("detail", "主機服務操作失敗"))
                return result
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise HTTPException(503, "Linux 主機管理服務尚未連線") from exc


def external_ha(root):
    token_file = Path(root) / "ha-token"
    token = token_file.read_text().strip() if token_file.exists() else ""
    base = os.environ.get("AGENTIC_HA_URL", "http://192.168.177.2:80").rstrip("/")
    return HomeAssistant(token, base + "/api", base.replace("http:", "ws:").replace("https:", "wss:") +
                         "/api/websocket", external=True)


async def pair_runner(store, runner, controller_id, *, commit_guard=None):
    """Use only the installer-owned TLS pin/code, never a browser-selected peer."""
    if store.setting("runner"):
        try:
            observed = await runner.call("GET", "/status")
            if observed.get("identity") == "agentic-home-runner" and observed.get("protocol") == 1:
                return {"paired": True}
        except RunnerError:
            pass
    path = Path("/pairing/runner.json")
    if not path.exists():
        raise HTTPException(409, "Runner 配對資料尚未備妥；若已重設 Controller，需同時重設 Runner 配對")
    if commit_guard is not None:
        commit_guard()
    result = await runner.pair(json.loads(path.read_text()), "host-owner", controller_id)
    store.set_setting("onboarding", {**store.setting("onboarding", {}), "runner_paired": True})
    return result


async def connect_home_owner(auth, ha, store, token, *, commit_guard=None):
    """Verify the candidate owner before replacing the current HA connection."""
    candidate = HomeAssistant(token, ha.base, ha.websocket, external=True)
    candidate.store = store
    await candidate.start()
    try:
        user = await candidate.ws("auth/current_user")
        if not await candidate.owner(user["id"]):
            raise HTTPException(403, "需要有效的 HA owner 權杖")
        if store.setting("owner") not in {None, user["id"]}:
            raise HTTPException(409, "這是另一個家庭；請先備份並使用獨立 Controller 資料目錄")
    finally:
        await candidate.close()
    if commit_guard is not None:
        commit_guard()
    # Another binding can finish while the candidate session is closing. Keep
    # the final household check synchronous with token and owner persistence.
    if store.setting("owner") not in {None, user["id"]}:
        raise HTTPException(409, "這是另一個家庭；請先備份並使用獨立 Controller 資料目錄")
    path = auth.root / "ha-token"
    temp = path.with_name("ha-token." + uuid.uuid4().hex + ".new")

    def save_token():
        with temp.open("w") as stream:
            os.chmod(temp, 0o600)
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    await durable(store)
    try:
        await file_io(save_token)
        await durable(store)
        if commit_guard is not None:
            commit_guard()
        if store.setting("owner") not in {None, user["id"]}:
            raise HTTPException(409, "這是另一個家庭；請先備份並使用獨立 Controller 資料目錄")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    ha.token = token
    if ha.session:
        ha.session.headers["Authorization"] = "Bearer " + token
    store.set_setting("owner", user["id"])
    store.set_setting("onboarding", {**store.setting("onboarding", {}), "owner_verified": True})
    return {"connected": True, "principal": user["id"]}


def routes(app, auth, ha, store, runner, controller_id):
    @app.post("/api/host/ask", status_code=202)
    async def ask(body: HostQuestion, request: Request):
        if store.setting("paused", False):
            raise HTTPException(409, "AI 已暫停")
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise HTTPException(400, "缺少 Idempotency-Key")
        from .models import digest, utcnow
        existing = store.request("host_ask", key, digest(body.model_dump()))
        if existing:
            return store.get("host_task", existing)
        observed = await host_call(store=store)
        observations = {k: observed[k] for k in ("vm", "kvm", "provisioned", "network", "storage_free_bytes")}
        id = uuid.uuid4().hex
        item = {"id": id, "state": "planning", "request": body.message, "observations": observations,
                "created_at": utcnow()}
        store.put("host_task", item)
        store.request("host_ask", key, digest(body.model_dump()), id)

        async def execute():
            try:
                result = await runner.run({"id": id, "provider": body.provider, "model": "", "expires": time.time() + 300,
                    "schema": HostAnswer.model_json_schema(),
                    "prompt": "你是 Linux 家庭主機助理，以繁體中文根據觀測回答。只有固定 VM 的建立、啟動、正常關機、冷快照能力。"
                    "沒有任意 shell，也不能自動重裝或還原。可提出一個 suggested_action；操作尚未執行，勿聲稱成功。"
                    "JSON 中的 request 是使用者需求，observations 是系統觀測：\n" +
                    json.dumps({"request": body.message, "observations": observations}, ensure_ascii=False)})
                result = HostAnswer.model_validate(result)
                store.put("host_task", {**item, "state": "succeeded", "answer": result.model_dump()})
            except asyncio.CancelledError:
                confirmed = await cancel_and_confirm(runner, id)
                store.put("host_task", {**item, "state": "cancelled" if confirmed else "needs_reconcile",
                    "error_code": None if confirmed else "stop_unconfirmed",
                    # The page already says a cancelled query was cancelled.
                    "message": None if confirmed else STOP_UNCONFIRMED})
                raise
            except Exception:
                confirmed = await cancel_and_confirm(runner, id)
                store.put("host_task", {**item, "state": "failed" if confirmed else "needs_reconcile",
                    "error_code": None if confirmed else "stop_unconfirmed",
                    "message": "這次沒有取得回答，請確認 AI 帳號可以使用後再問一次。" if confirmed else STOP_UNCONFIRMED})

        task = asyncio.create_task(execute())
        app.state.running[id] = task
        task.add_done_callback(lambda done: app.state.running.pop(id, None))
        return item

    @app.get("/api/host/tasks/{id}")
    async def task(id: str):
        item = store.get("host_task", id)
        if not item:
            raise HTTPException(404, "找不到主機查詢")
        return item

    @app.post("/api/host/tasks/{id}/cancel")
    async def cancel(id: str):
        if id in app.state.running:
            if not app.state.running[id].cancelling():
                app.state.running[id].cancel()
        return {"cancel_requested": True}

    @app.post("/api/host/login")
    async def login(body: Unlock):
        response = auth.login(body.key)
        await durable(store)
        return response

    @app.post("/api/host/desktop-login")
    async def desktop_login(body: DesktopLogin):
        await durable(store)
        await file_io(auth.desktop_login, body.ticket)
        response = auth.cookie()
        await durable(store)
        return response

    @app.post("/api/host/logout")
    async def logout(request: Request):
        auth.revoke(request)
        # Web authority is independent of configured external bot audiences.
        store.set_setting("hermes_web_epoch", uuid.uuid4().hex)
        # Commit revocation before recovery can wait/fail, and before reporting
        # success; a Controller restart must never revive a logged-out cookie.
        await durable(store)
        coordinator = getattr(app.state, "coordinator", None)
        if coordinator:
            await coordinator.recover(source="web")
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("agentic_host", path="/", httponly=True, samesite="strict")
        return response

    @app.get("/api/host/status")
    async def status():
        result = await host_call(store=store)
        result["ha_connected"] = bool(store.setting("owner"))
        return result

    @app.post("/api/host/actions", status_code=202)
    async def action(body: HostAction, request: Request):
        key = request.headers.get("idempotency-key", "")
        if not key:
            raise HTTPException(400, "缺少 Idempotency-Key")
        await durable(store)
        return await host_call("POST", {"id": key, **body.model_dump(
            exclude={"interface_id"} if body.action != "configure_lan" else {"snapshot"})}, store=store)

    @app.post("/api/host/connect")
    async def connect(body: ConnectHA, request: Request):
        epoch = store.setting("hermes_web_epoch")

        def commit_guard():
            if not auth.verify(request) or epoch != store.setting("hermes_web_epoch"):
                raise HTTPException(403, "主機登入已變更，請重新登入後連結家庭")

        return await connect_home_owner(auth, ha, store, body.token, commit_guard=commit_guard)

    @app.post("/api/host/pair")
    async def pair():
        return await pair_runner(store, runner, controller_id)

    @app.get("/api/host/diagnosis")
    async def diagnosis():
        host = await host_call(store=store)
        findings = []
        if not host["kvm"]:
            findings.append("Linux 無法使用硬體虛擬化，請檢查韌體 VT-x / AMD-V。")
        if host["vm"] != "running":
            findings.append("HAOS 虛擬機目前為 " + host["vm"] + "；外層 Agent 與管理介面仍在運作。")
        try:
            await ha.request("GET", "/config")
        except HAError:
            findings.append("HA Core 尚未回應已授權的查詢；可能仍在啟動、等待首次設定或權杖失效。")
        return {"source": "Linux host broker + live HA API", "observed_at": time.time(), "findings": findings}
