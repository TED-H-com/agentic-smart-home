"""Outer Linux authentication and typed host-broker client, independent of HA availability."""
import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Literal

import aiohttp
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .ha import HAError, HomeAssistant
from .models import Strict
from .runner import RunnerError


class Unlock(Strict):
    key: str = Field(min_length=20, max_length=200)


class DesktopLogin(Strict):
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class ConnectHA(Strict):
    token: str = Field(min_length=20, max_length=16384)


class HostAction(Strict):
    action: str = Field(pattern="^(provision|start|shutdown|snapshot|restore)$")
    snapshot: str | None = Field(default=None, pattern=r"^[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")


class HostQuestion(Strict):
    message: str = Field(min_length=1, max_length=5000)
    provider: Literal["codex", "claude", "agy", "grok"] = "codex"


class HostAnswer(Strict):
    message: str
    suggested_action: Literal["none", "provision", "start", "shutdown", "snapshot"]


class HostSession:
    def __init__(self, root, secret):
        self.root, self.secret, self.attempts = Path(root), secret.encode(), []

    def sign(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def verify(self, request):
        cookie = request.cookies.get("agentic_host", "")
        try:
            expires, signature = cookie.split(".")
            return time.time() < int(expires) <= time.time() + 43260 and hmac.compare_digest(signature, self.sign(expires))
        except (ValueError, TypeError):
            return False

    def login(self, key):
        self.attempts = [t for t in self.attempts if t > time.time() - 60]
        if len(self.attempts) >= 8:
            raise HTTPException(429, "請稍後再試")
        self.attempts.append(time.time())
        expected = (self.root / "access-key").read_text().strip()
        if not hmac.compare_digest(key, expected):
            raise HTTPException(403, "主機登入金鑰不正確")
        return self.cookie()

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
        return self.cookie()

    def cookie(self):
        expires = str(int(time.time()) + 43200)
        response = JSONResponse({"authenticated": True})
        response.set_cookie("agentic_host", expires + "." + self.sign(expires), httponly=True,
                            samesite="strict", max_age=43200, path="/")
        return response


async def host_call(method="GET", body=None):
    connector = aiohttp.UnixConnector(path="/run/agentic-home/host.sock")
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.request(method, "http://localhost/" + ("status" if method == "GET" else "actions"),
                                       json=body) as response:
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


async def pair_runner(store, runner, controller_id):
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
    result = await runner.pair(json.loads(path.read_text()), "host-owner", controller_id)
    store.set_setting("onboarding", {**store.setting("onboarding", {}), "runner_paired": True})
    return result


async def connect_home_owner(auth, ha, store, token, *, commit_guard=None):
    """Verify the candidate owner before replacing the current HA connection."""
    candidate = HomeAssistant(token, ha.base, ha.websocket, external=True)
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
    path = auth.root / "ha-token"
    temp = path.with_suffix(".new")
    with temp.open("w") as stream:
        os.chmod(temp, 0o600)
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
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
        observed = await host_call()
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
                await runner.call("POST", f"/jobs/{id}/cancel", {})
                store.put("host_task", {**item, "state": "cancelled"})
                raise
            except Exception:
                store.put("host_task", {**item, "state": "failed", "message": "Agent 查詢未完成，請檢查官方登入或供應商狀態。"})

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
            app.state.running[id].cancel()
        return {"cancel_requested": True}

    @app.post("/api/host/login")
    async def login(body: Unlock):
        return auth.login(body.key)

    @app.post("/api/host/desktop-login")
    async def desktop_login(body: DesktopLogin):
        return auth.desktop_login(body.ticket)

    @app.post("/api/host/logout")
    async def logout():
        # Web authority is independent of configured external bot audiences.
        store.set_setting("hermes_web_epoch", uuid.uuid4().hex)
        coordinator = getattr(app.state, "coordinator", None)
        if coordinator:
            await coordinator.recover(source="web")
        response = JSONResponse({"authenticated": False})
        response.delete_cookie("agentic_host", path="/")
        return response

    @app.get("/api/host/status")
    async def status():
        result = await host_call()
        result["ha_connected"] = bool(store.setting("owner"))
        return result

    @app.post("/api/host/actions", status_code=202)
    async def action(body: HostAction, request: Request):
        key = request.headers.get("idempotency-key", "")
        if not key:
            raise HTTPException(400, "缺少 Idempotency-Key")
        return await host_call("POST", {"id": key, **body.model_dump()})

    @app.post("/api/host/connect")
    async def connect(body: ConnectHA):
        return await connect_home_owner(auth, ha, store, body.token)

    @app.post("/api/host/pair")
    async def pair():
        return await pair_runner(store, runner, controller_id)

    @app.get("/api/host/diagnosis")
    async def diagnosis():
        host = await host_call()
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
