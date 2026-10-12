from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
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

from .bootstrap import BootstrapJob, BootstrapResult
from .bootstrap import prompt as bootstrap_prompt
from .providers import ProviderError, Providers, StopUnconfirmed
from .retention import MAX_IDENTITIES, retained


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


class Retention(Input):
    context_days: Annotated[int, Field(ge=1, le=7)]


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
        self.db.executescript("""PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL);
        """)
        self.providers = Providers(root)
        self.worker = asyncio.Semaphore(1)
        self.tasks = {}
        self.code, self.code_expires, self.pair_attempts = secrets.token_urlsafe(32), time.time() + 600, []
        if self.setting("certificate_pin") != self.fingerprint:
            self.settings({"token_hash": None, "controller_id": None, "certificate_pin": self.fingerprint,
                           "pair_recovery": None})
        for _id, _, body in self.db.execute("SELECT * FROM jobs").fetchall():
            job = json.loads(body)
            if job["state"] in {"running", "stop_unconfirmed"}:
                self.providers.quarantine("runner_restarted_with_unconfirmed_job")
                self.save_job({**job, "state": "stop_unconfirmed", "error": "stop_unconfirmed"})
            elif job["state"] == "queued":
                self.save_job({**job, "state": "failed", "error": "Runner 已重啟，任務未重新執行"})
        self.prune()

    def setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def settings(self, values):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                [(k, json.dumps(v)) for k, v in values.items()])

    def pairing_token(self, body):
        # Exact retries derive the same bearer without storing it in SQLite.
        # The seed remains inside the existing private, backup-excluded directory.
        path = self.root / "private" / "pairing-recovery.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(48))
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                    stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                    raise ValueError("invalid private seed")
                seed = stream.read(49)
            if len(seed) != 48:
                raise ValueError("invalid private seed")
        except (OSError, ValueError):
            raise HTTPException(409, "配對恢復資訊無效") from None
        return hmac.new(seed, body.model_dump_json().encode(), hashlib.sha384).hexdigest()

    def refresh_pairing_file(self, path, now=None):
        """Retain only the exact original installer handoff until its deadline."""
        now = time.time() if now is None else now
        if self.setting("token_hash"):
            recovery = self.setting("pair_recovery") or {}
            try:
                pairing = json.loads(path.read_text())
                if (recovery.get("expires", 0) > now and pairing.get("fingerprint") == self.fingerprint and
                    isinstance(pairing.get("code"), str) and
                    hmac.compare_digest(hashlib.sha256(pairing["code"].encode()).hexdigest(),
                                        recovery.get("code_hash", ""))):
                    return True
            except (FileNotFoundError, ValueError, AttributeError, TypeError):
                pass
            path.unlink(missing_ok=True)
            return False
        if self.code_expires < now + 60:
            self.code, self.code_expires = secrets.token_urlsafe(32), now + 600
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".new")
        temporary.write_text(json.dumps({"host": "localhost", "port": 8443,
                                        "fingerprint": self.fingerprint, "code": self.code}))
        if os.getuid() == 0:
            os.chown(temporary, 0, 1000)
        temporary.chmod(0o640)
        os.replace(temporary, path)
        return True

    @staticmethod
    def pairing_response(token):
        return {"token": token, "protocol": 1, "identity": "agentic-home-runner",
                "features": ["structured_intents", "cancel", "codex", "claude", "agy", "grok"]}

    def save_job(self, job):
        old = self.db.execute("SELECT body FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if old:
            previous = json.loads(old[0])
            if "retained_until" in previous:
                job = {**job, "retained_until": previous["retained_until"]}
            if previous.get("content_expired"):
                job = {**job, "content_expired": True}
        elif self.db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= MAX_IDENTITIES:
            raise HTTPException(507, "job_identity_ledger_full")
        job = retained(job, self.setting("context_days") or 7)
        job = {**job, "updated": time.time()}
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)",
                            (job["id"], job["digest"], json.dumps(job)))
        return job

    def prune(self):
        changed = False
        with self.db:
            for identifier, raw in self.db.execute("SELECT id,body FROM jobs").fetchall():
                previous = json.loads(raw)
                item = retained(previous, self.setting("context_days") or 7)
                if item != previous:
                    self.db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(item), identifier))
                    changed = True
                if item.get("content_expired") and item["state"] == "stop_unconfirmed":
                    self.providers.quarantine("expired_context_requires_stop")
        if changed:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.providers.auth_sessions = {id: state for id, state in self.providers.auth_sessions.items()
                                       if state.get("_expires", 0) + 3600 > time.time()}

    def job(self, id):
        self.prune()
        row = self.db.execute("SELECT body FROM jobs WHERE id=?", (id,)).fetchone()
        return json.loads(row[0]) if row else None

    async def execute(self, request: Job):
        async with self.worker:
            job = self.job(request.id)
            if job["state"] == "cancelled":
                return
            try:
                self.providers.ensure_available()
                if request.expires < time.time():
                    raise ProviderError("任務授權已過期")
                self.save_job({**job, "state": "running"})
                result = await self.providers.run(request.id, request.provider, request.prompt,
                                                  request.schema_, request.model)
                self.save_job({**job, "state": "succeeded", "result": result})
            except StopUnconfirmed:
                self.save_job({**job, "state": "stop_unconfirmed", "error": "stop_unconfirmed"})
            except asyncio.CancelledError:
                state = "stop_unconfirmed" if self.providers.unconfirmed else "cancelled"
                self.save_job({**job, "state": state, "error": "stop_unconfirmed" if self.providers.unconfirmed else None})
                raise
            except (ProviderError, TimeoutError, OSError, ValueError) as exc:
                error = str(exc) if isinstance(exc, ProviderError) else "CLI 超時或無法取得結構化結果"
                self.save_job({**job, "state": "failed", "error": error})
            finally:
                metrics = self.providers.metrics.pop(request.id, None)
                if metrics:
                    self.save_job({**self.job(request.id), "resources": metrics})

    async def execute_bootstrap(self, request: BootstrapJob):
        async with self.worker:
            job = self.job(request.id)
            if job["state"] == "cancelled":
                return
            try:
                self.providers.ensure_available()
                if request.expires < time.time():
                    raise ProviderError("bootstrap_expired")
                self.save_job({**job, "state": "running"})
                health = await self.providers.health(request.provider)
                if health["status"] != "connected":
                    raise ProviderError("needs_reauth")
                result = await self.providers.run(request.id, request.provider, bootstrap_prompt(request),
                                                  BootstrapResult.model_json_schema(), request.model)
                safe = BootstrapResult.model_validate(result).model_dump()
                self.save_job({**job, "state": "succeeded", "result": safe, "evidence": {
                    "observed_from": "host_runner", "provider": request.provider,
                    "cli_version": health["version"], "inference_completed": True,
                    "live_mutations": False,
                }})
            except StopUnconfirmed:
                self.save_job({**job, "state": "stop_unconfirmed", "error": "stop_unconfirmed"})
            except asyncio.CancelledError:
                state = "stop_unconfirmed" if self.providers.unconfirmed else "cancelled"
                self.save_job({**job, "state": state, "error": "stop_unconfirmed" if self.providers.unconfirmed else None})
                raise
            except (ProviderError, TimeoutError, OSError, ValueError) as exc:
                # Neither CLI diagnostics nor rejected structured fields reach Controller.
                error = "needs_reauth" if "needs_reauth" in str(exc) else "bootstrap_assistance_failed"
                self.save_job({**job, "state": "failed", "error": error})
            finally:
                self.providers.metrics.pop(request.id, None)


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

    def require_fenced():
        if broker.providers.unconfirmed:
            raise HTTPException(409, "stop_unconfirmed")

    @app.post("/pair")
    async def pair(body: Pair):
        broker.pair_attempts = [t for t in broker.pair_attempts if t > time.time() - 60]
        broker.pair_attempts.append(time.time())
        if len(broker.pair_attempts) > 5:
            raise HTTPException(429, "配對嘗試過於頻繁")
        if broker.setting("token_hash"):
            recovery = broker.setting("pair_recovery") or {}
            expected = {"controller_id": body.controller_id, "principal": body.principal, "protocol": body.protocol}
            if (recovery.get("expires", 0) <= time.time() or
                any(recovery.get(key) != value for key, value in expected.items()) or
                not hmac.compare_digest(recovery.get("code_hash", ""), hashlib.sha256(body.code.encode()).hexdigest())):
                raise HTTPException(409, "已配對或配對碼過期")
            token = broker.pairing_token(body)
            if not hmac.compare_digest(broker.setting("token_hash"), hashlib.sha256(token.encode()).hexdigest()):
                raise HTTPException(409, "配對恢復資訊無效")
            return broker.pairing_response(token)
        if time.time() > broker.code_expires:
            raise HTTPException(409, "已配對或配對碼過期")
        if not hmac.compare_digest(body.code, broker.code):
            raise HTTPException(403, "配對碼錯誤")
        token = broker.pairing_token(body)
        broker.settings({"token_hash": hashlib.sha256(token.encode()).hexdigest(),
                         "controller_id": body.controller_id, "principal": body.principal,
                         "pair_recovery": {"controller_id": body.controller_id, "principal": body.principal,
                            "protocol": body.protocol, "code_hash": hashlib.sha256(body.code.encode()).hexdigest(),
                            "expires": broker.code_expires}})
        broker.code = ""
        return broker.pairing_response(token)

    @app.get("/status")
    async def status():
        providers = []
        for name in ("codex", "claude", "agy", "grok"):
            try:
                providers.append(await broker.providers.health(name))
            except (ProviderError, OSError, TimeoutError):
                providers.append({"provider": name, "status": "unavailable"})
        return {"protocol": 1, "identity": "agentic-home-runner", "providers": providers,
                "running": sum(not t.done() for t in broker.tasks.values()),
                "execution_state": "stop_unconfirmed" if broker.providers.unconfirmed else
                    "busy" if any(not t.done() for t in broker.tasks.values()) else "idle"}

    @app.post("/retention")
    async def retention(body: Retention):
        broker.settings({"context_days": body.context_days})
        broker.prune()
        if os.environ.get("AGENTIC_HOST_MODE") == "1":
            from .coding import host_request
            try:
                status, _ = await host_request("POST", "/retention", body.model_dump())
                if status != 200:
                    raise HTTPException(503, "coding_retention_pending")
            except (OSError, ValueError, TimeoutError):
                raise HTTPException(503, "coding_retention_pending") from None
        return {"context_days": body.context_days, "identity_limit": MAX_IDENTITIES}

    @app.get("/retention")
    async def retention_status():
        return {"context_days": broker.setting("context_days") or 7, "identity_limit": MAX_IDENTITIES}

    @app.post("/reconcile")
    async def reconcile():
        if any(not task.done() for task in broker.tasks.values()):
            raise HTTPException(409, "runner_busy")
        async def external(previous):
            if os.environ.get("AGENTIC_HOST_MODE") != "1" or "container_id" not in previous:
                return False
            from .coding import host_request
            try:
                code, evidence = await host_request("POST", "/reconcile-runtime", previous)
                return code == 200 and evidence.get("stop_confirmed") is True
            except Exception:
                return False
        if not await broker.providers.reconcile(external):
            raise HTTPException(409, "stop_unconfirmed")
        for row in broker.db.execute("SELECT body FROM jobs").fetchall():
            job = json.loads(row[0])
            if job["state"] == "stop_unconfirmed":
                broker.save_job({**job, "state": "failed", "stop_confirmed": True,
                                 "error": "interrupted_after_reconciliation"})
        return {"execution_state": "idle", "stop_confirmed": True, "replayed": False}

    @app.post("/providers/{provider}/auth")
    async def auth(provider: Literal["codex", "claude", "agy", "grok"], body: Login):
        require_fenced()
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
        require_fenced()
        if any(not t.done() for t in broker.tasks.values()):
            raise HTTPException(409, "請先取消目前任務")
        return await broker.providers.logout(provider)

    @app.post("/jobs")
    async def job(body: Job):
        if len(body.prompt.encode()) > 32768 or len(json.dumps(body.schema_)) > 32768:
            raise HTTPException(413, "任務上下文超過上限")
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        old = broker.job(body.id)
        if old:
            if old["digest"] != digest:
                raise HTTPException(409, "任務 ID 已用於其他內容")
            return old
        if not time.time() < body.expires <= time.time() + 360:
            raise HTTPException(422, "任務授權時間無效")
        require_fenced()
        if sum(not t.done() for t in broker.tasks.values()) >= 10:
            raise HTTPException(429, "任務佇列已滿")
        record = broker.save_job({"id": body.id, "digest": digest, "provider": body.provider,
                                  "state": "queued", "created": time.time()})
        broker.prune()
        broker.tasks[body.id] = task = asyncio.create_task(broker.execute(body))
        task.add_done_callback(lambda done: broker.tasks.pop(body.id, None))
        return record

    @app.post("/bootstrap/jobs", status_code=202)
    async def bootstrap_job(body: BootstrapJob):
        if os.environ.get("AGENTIC_HOST_MODE") != "1":
            raise HTTPException(403, "bootstrap_requires_host_runner")
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        old = broker.job(body.id)
        if old:
            if old["digest"] != digest or old.get("kind") != "bootstrap_assist":
                raise HTTPException(409, "bootstrap_idempotency_conflict")
            return old
        require_fenced()
        if not time.time() < body.expires <= time.time() + 360:
            raise HTTPException(422, "bootstrap_expired")
        if len(broker.tasks) >= 10:
            raise HTTPException(429, "任務佇列已滿")
        record = broker.save_job({"id": body.id, "digest": digest, "provider": body.provider,
                                  "kind": "bootstrap_assist", "state": "queued", "created": time.time()})
        broker.prune()
        broker.tasks[body.id] = task = asyncio.create_task(broker.execute_bootstrap(body))
        task.add_done_callback(lambda done: broker.tasks.pop(body.id, None))
        return record

    @app.get("/bootstrap/jobs/{id}")
    async def bootstrap_status(id: str):
        record = broker.job(id)
        if not record or record.get("kind") != "bootstrap_assist":
            raise HTTPException(404, "找不到初始化任務")
        return record

    @app.post("/bootstrap/jobs/{id}/cancel")
    async def bootstrap_cancel(id: str):
        await bootstrap_status(id)
        return await cancel(id)

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
            # All callers await the same writer's fence. A second cancel or a
            # disconnected request must not inject cancellation into its cleanup.
            if not task.cancelling():
                task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
        record = broker.job(id)
        if record["state"] == "stop_unconfirmed" or broker.providers.unconfirmed:
            return broker.save_job({**record, "state": "stop_unconfirmed", "error": "stop_unconfirmed"})
        return broker.save_job({**record, "state": "cancelled"})

    from .coding import routes as coding_routes
    coding_routes(app, broker)
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
    if host_mode:
        from .coding import host_request
        try:
            status, runtime = await host_request("GET", "/runtime")
            if status == 200 and runtime.get("pid_namespace") == broker.providers.runtime["pid_namespace"]:
                broker.providers.runtime.update(runtime)
                broker.providers.write_runtime()
        except Exception:
            pass  # Missing trusted identity never enables reconciliation.
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
            broker.refresh_pairing_file(Path("/pairing/runner.json"))
        else:
            print("一次性配對資料（私人資料，10 分鐘有效）：" + json.dumps(pairing), flush=True)
    del token
    api = uvicorn.Server(uvicorn.Config(create_api(broker), host="127.0.0.1" if host_mode else "0.0.0.0", port=8443,
        ssl_certfile=str(broker.cert), ssl_keyfile=str(broker.key), proxy_headers=False, access_log=False))
    ingress = uvicorn.Server(uvicorn.Config(create_ingress(broker), host="127.0.0.1" if host_mode else "0.0.0.0", port=8098 if host_mode else 8099,
        proxy_headers=False, access_log=False))
    async def refresh_pairing():
        path = Path("/pairing/runner.json")
        while broker.refresh_pairing_file(path):
            await asyncio.sleep(30)
    pairing_task = asyncio.create_task(refresh_pairing()) if host_mode else None
    async def expire_content():
        while True:
            broker.prune()
            await asyncio.sleep(60)
    retention_task = asyncio.create_task(expire_content())
    try:
        await asyncio.gather(api.serve(), ingress.serve())
    finally:
        retention_task.cancel()
        await asyncio.gather(retention_task, return_exceptions=True)
        if pairing_task:
            pairing_task.cancel()
            await asyncio.gather(pairing_task, return_exceptions=True)
        coding = broker.coding_credentials
        tasks = list(broker.tasks.values()) + [s["_task"] for provider in (broker.providers, coding)
                                              for s in provider.auth_sessions.values() if s.get("_task")]
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for proc in list(broker.providers.processes.values()):
            await broker.providers.stop(proc)
        for proc in list(coding.processes.values()):
            await coding.stop(proc)
        broker.db.close()


if __name__ == "__main__":
    asyncio.run(serve())
