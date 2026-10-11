from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .providers import ProviderError, Providers


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Pair(Input):
    code: Annotated[str, Field(min_length=32, max_length=100)]
    controller_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    principal: Annotated[str, Field(min_length=1, max_length=128)]
    protocol: Literal[1]


class Login(Input):
    method: Literal["device_code", "api_key", "oauth_token"]
    secret: Annotated[str, Field(max_length=16384)] = ""


class Job(Input):
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    provider: Literal["codex", "claude", "agy", "grok"]
    prompt: Annotated[str, Field(max_length=32768)]
    schema_: dict = Field(alias="schema")
    model: Annotated[str, Field(pattern=r"^[a-zA-Z0-9._:/-]{0,100}$")] = ""
    expires: float


def identity(root):
    private = root / "private"
    private.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    cert_path, key_path = private / "server.pem", private / "server.key"
    if not cert_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agentic-home-runner")])
        now = datetime.now(UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=3650)).sign(key, hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        key_path.chmod(0o600)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert_path, key_path, cert.fingerprint(hashes.SHA256()).hex()


class Broker:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o711)
        # Provider UIDs can traverse these parents, but cannot list other profiles/jobs.
        for folder in (root / "profiles", root / "work"):
            folder.mkdir(exist_ok=True, mode=0o711)
            folder.chmod(0o711)
        self.cert, self.key, self.fingerprint = identity(root)
        (root / "runner.sqlite3").touch(mode=0o600, exist_ok=True)
        (root / "runner.sqlite3").chmod(0o600)
        self.db = sqlite3.connect(root / "runner.sqlite3")
        (root / "runner.sqlite3").chmod(0o600)
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL);
        """)
        self.providers = Providers(root)
        self.worker = asyncio.Semaphore(1)
        self.tasks = {}
        self.code, self.code_expires, self.pair_attempts = secrets.token_urlsafe(32), time.time() + 600, []
        if self.setting("certificate_pin") != self.fingerprint:
            self.settings({"token_hash": None, "controller_id": None, "certificate_pin": self.fingerprint})
        for _id, _, body in self.db.execute("SELECT * FROM jobs").fetchall():
            job = json.loads(body)
            if job["state"] in {"queued", "running"}:
                self.save_job({**job, "state": "failed", "error": "Runner 已重啟，任務未重新執行"})

    def setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def settings(self, values):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                [(k, json.dumps(v)) for k, v in values.items()])

    def save_job(self, job):
        job = {**job, "updated": time.time()}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)",
                            (job["id"], job["digest"], json.dumps(job)))
        return job

    def prune(self):
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE json_extract(body,'$.state') IN ('succeeded','failed','cancelled') "
                            "AND json_extract(body,'$.created') < ?", (time.time() - 7 * 86400,))
        self.providers.auth_sessions = {id: state for id, state in self.providers.auth_sessions.items()
                                       if state.get("_expires", 0) + 3600 > time.time()}

    def job(self, id):
        row = self.db.execute("SELECT body FROM jobs WHERE id=?", (id,)).fetchone()
        return json.loads(row[0]) if row else None

    async def execute(self, request: Job):
        async with self.worker:
            job = self.job(request.id)
            if job["state"] == "cancelled":
                return
            try:
                if request.expires < time.time():
                    raise ProviderError("任務授權已過期")
                self.save_job({**job, "state": "running"})
                result = await self.providers.run(request.id, request.provider, request.prompt,
                                                  request.schema_, request.model)
                self.save_job({**job, "state": "succeeded", "result": result})
            except asyncio.CancelledError:
                self.save_job({**job, "state": "cancelled"})
                raise
            except (ProviderError, TimeoutError, OSError, ValueError) as exc:
                error = str(exc) if isinstance(exc, ProviderError) else "CLI 超時或無法取得結構化結果"
                self.save_job({**job, "state": "failed", "error": error})
            finally:
                metrics = self.providers.metrics.pop(request.id, None)
                if metrics:
                    self.save_job({**self.job(request.id), "resources": metrics})


def create_api(broker):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "請求格式錯誤"}, status_code=422)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if int(request.headers.get("content-length", "0")) > 131072:
            return JSONResponse({"detail": "請求超過大小上限"}, status_code=413)
        if request.url.path != "/pair":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            stored = broker.setting("token_hash")
            if not stored or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), stored):
                return JSONResponse({"detail": "未配對或憑證無效"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.post("/pair")
    async def pair(body: Pair):
        broker.pair_attempts = [t for t in broker.pair_attempts if t > time.time() - 60]
        broker.pair_attempts.append(time.time())
        if len(broker.pair_attempts) > 5:
            raise HTTPException(429, "配對嘗試過於頻繁")
        if broker.setting("token_hash") or time.time() > broker.code_expires:
            raise HTTPException(409, "已配對或配對碼過期")
        if not hmac.compare_digest(body.code, broker.code):
            raise HTTPException(403, "配對碼錯誤")
        token = secrets.token_urlsafe(48)
        broker.settings({"token_hash": hashlib.sha256(token.encode()).hexdigest(),
                         "controller_id": body.controller_id, "principal": body.principal})
        broker.code = ""
        return {"token": token, "protocol": 1, "identity": "agentic-home-runner",
                "features": ["structured_intents", "cancel", "codex", "claude", "agy", "grok"]}

    @app.get("/status")
    async def status():
        providers = []
        for name in ("codex", "claude", "agy", "grok"):
            try:
                providers.append(await broker.providers.health(name))
            except (ProviderError, OSError, TimeoutError):
                providers.append({"provider": name, "status": "unavailable"})
        return {"protocol": 1, "identity": "agentic-home-runner", "providers": providers,
                "running": sum(not t.done() for t in broker.tasks.values())}

    @app.post("/providers/{provider}/auth")
    async def auth(provider: Literal["codex", "claude", "agy", "grok"], body: Login):
        if any(not t.done() for t in broker.tasks.values()):
            raise HTTPException(409, "請先取消或等候目前任務")
        if body.method == "device_code":
            if provider not in {"codex", "grok"}:
                raise HTTPException(422, "此 provider 未提供 device code 登入")
            return await broker.providers.begin_device_auth(uuid.uuid4().hex, provider)
        return await broker.providers.secret_login(provider, body.method, body.secret)

    @app.get("/auth/{id}")
    async def auth_status(id: str):
        state = broker.providers.auth_sessions.get(id)
        if not state:
            raise HTTPException(404, "登入流程已不存在，請重新開始")
        return {k: v for k, v in state.items() if not k.startswith("_")}

    @app.post("/providers/{provider}/logout")
    async def logout(provider: Literal["codex", "claude", "agy", "grok"]):
        if any(not t.done() for t in broker.tasks.values()):
            raise HTTPException(409, "請先取消目前任務")
        return await broker.providers.logout(provider)

    @app.post("/jobs")
    async def job(body: Job):
        if len(body.prompt.encode()) > 32768 or len(json.dumps(body.schema_)) > 32768:
            raise HTTPException(413, "任務上下文超過上限")
        if not time.time() < body.expires <= time.time() + 360:
            raise HTTPException(422, "任務授權時間無效")
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        old = broker.job(body.id)
        if old:
            if old["digest"] != digest:
                raise HTTPException(409, "任務 ID 已用於其他內容")
            return old
        if sum(not t.done() for t in broker.tasks.values()) >= 10:
            raise HTTPException(429, "任務佇列已滿")
        record = broker.save_job({"id": body.id, "digest": digest, "provider": body.provider,
                                  "state": "queued", "created": time.time()})
        broker.prune()
        broker.tasks[body.id] = task = asyncio.create_task(broker.execute(body))
        task.add_done_callback(lambda done: broker.tasks.pop(body.id, None))
        return record

    @app.get("/jobs/{id}")
    async def job_status(id: str):
        record = broker.job(id)
        if not record:
            raise HTTPException(404, "找不到任務")
        return record

    @app.post("/jobs/{id}/cancel")
    async def cancel(id: str):
        record = broker.job(id)
        if not record:
            raise HTTPException(404, "找不到任務")
        if record["state"] in {"succeeded", "failed", "cancelled"}:
            return record
        task = broker.tasks.get(id)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return broker.save_job({**record, "state": "cancelled"})

    return app


def create_ingress(broker, ingress_ip="172.30.32.2"):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health(request: Request):
        if request.client.host not in {ingress_ip, "127.0.0.1", "::1"}:
            raise HTTPException(403)
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if request.client.host != ingress_ip or not request.headers.get("x-remote-user-id"):
            raise HTTPException(403, "請從 Home Assistant 開啟")
        paired = bool(broker.setting("token_hash"))
        return """<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
        <title>Agentic Home Runner</title><body style="font:16px system-ui;max-width:680px;margin:64px auto;padding:24px">
        <h1>Agentic Home Runner</h1><p>""" + ("已與 Controller 配對。登入、任務與取消請使用 Controller。" if paired else
        "在 Runner 的 HA App 記錄複製一次性配對資料，再開啟 Controller 貼上。配對碼 10 分鐘有效；過期請重新啟動 Runner。") + """</p>
        <p>憑證指紋</p><code style="overflow-wrap:anywhere">""" + broker.fingerprint + "</code></body></html>"

    return app


async def serve():
    os.umask(0o077)
    root = Path(os.environ.get("AGENTIC_DATA", "/data"))
    broker = Broker(root)
    host_mode = os.environ.get("AGENTIC_HOST_MODE") == "1"
    token = os.environ.pop("SUPERVISOR_TOKEN", "")
    if not broker.setting("token_hash"):
        hostname = os.environ.get("HOSTNAME", "local-agentic-runner").replace("_", "-")
        if token:
            import aiohttp
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as client:
                    async with client.get("http://supervisor/addons/self/info",
                                          headers={"Authorization": "Bearer " + token}) as response:
                        info = (await response.json()).get("data", {})
                        hostname = info.get("hostname") or info.get("slug", hostname).replace("_", "-")
            except (aiohttp.ClientError, TimeoutError, ValueError):
                pass
        # The code is visible only in Supervisor-managed App logs, never public ingress.
        pairing = {"host": "localhost" if host_mode else hostname, "port": 8443,
                   "fingerprint": broker.fingerprint, "code": broker.code}
        if host_mode:
            path = Path("/pairing/runner.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(pairing))
            os.chown(path, 0, 1000)
            os.chmod(path, 0o640)
        else:
            print("一次性配對資料（私人資料，10 分鐘有效）：" + json.dumps(pairing), flush=True)
    del token
    api = uvicorn.Server(uvicorn.Config(create_api(broker), host="127.0.0.1" if host_mode else "0.0.0.0", port=8443,
        ssl_certfile=str(broker.cert), ssl_keyfile=str(broker.key), proxy_headers=False, access_log=False))
    ingress = uvicorn.Server(uvicorn.Config(create_ingress(broker), host="127.0.0.1" if host_mode else "0.0.0.0", port=8098 if host_mode else 8099,
        proxy_headers=False, access_log=False))
    async def refresh_pairing():
        path = Path("/pairing/runner.json")
        while not broker.setting("token_hash"):
            await asyncio.sleep(30)
            if broker.code_expires < time.time() + 60:
                broker.code, broker.code_expires = secrets.token_urlsafe(32), time.time() + 600
                temp = path.with_suffix(".new")
                temp.write_text(json.dumps({"host": "localhost", "port": 8443,
                                            "fingerprint": broker.fingerprint, "code": broker.code}))
                os.chown(temp, 0, 1000)
                temp.chmod(0o640)
                os.replace(temp, path)
        path.unlink(missing_ok=True)
    pairing_task = asyncio.create_task(refresh_pairing()) if host_mode else None
    try:
        await asyncio.gather(api.serve(), ingress.serve())
    finally:
        if pairing_task:
            pairing_task.cancel()
            await asyncio.gather(pairing_task, return_exceptions=True)
        tasks = list(broker.tasks.values()) + [s["_task"] for s in broker.providers.auth_sessions.values() if s.get("_task")]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for proc in list(broker.providers.processes.values()):
            await broker.providers.stop(proc)
        broker.db.close()


if __name__ == "__main__":
    asyncio.run(serve())
