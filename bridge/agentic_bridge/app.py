"""HA Ingress presentation only. Authority and conversations stay in the outer Controller."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import VERSION

MAX_REQUEST = 131072
MAX_RESPONSE = 8 * 1024 * 1024
USER_ID = re.compile(r"[a-f0-9]{32}")
# Below the Controller bridge transport keep-alive. Every relay call is a POST, which is not retried.
RELAY_KEEPALIVE_SECONDS = 2


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal[1]
    host: Literal["192.168.177.1"]
    port: Literal[8444]
    fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    token: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{32,128}$")]
    pairing_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]

    @field_validator("protocol", "port", mode="before")
    @classmethod
    def integer_contract(cls, value):
        if type(value) is not int:
            raise ValueError("integer required")
        return value


class RelayError(Exception):
    pass


class Relay:
    def __init__(self, options: Options):
        self.options = options
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=40),
                                            connector=aiohttp.TCPConnector(limit=8, keepalive_timeout=RELAY_KEEPALIVE_SECONDS))

    async def close(self):
        if self.session:
            await self.session.close()

    async def call(self, payload):
        if not self.session:
            raise RelayError()
        url = f"https://{self.options.host}:{self.options.port}/v1/request"
        payload = {**payload, "protocol": 1, "pairing_id": self.options.pairing_id}
        try:
            async with self.session.post(url, json=payload, allow_redirects=False,
                                         ssl=aiohttp.Fingerprint(bytes.fromhex(self.options.fingerprint)),
                                         headers={"Authorization": "Bearer " + self.options.token}) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type not in {"application/json", "image/png"} or 300 <= response.status < 400:
                    raise RelayError()
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise RelayError()
                if content_type == "image/png":
                    if response.status != 200 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise RelayError()
                else:
                    json.loads(raw)
                # Never relay cookies, redirects, CORS or arbitrary upstream headers.
                return Response(bytes(raw), status_code=response.status, media_type=content_type)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise RelayError() from exc


def load_options(path=Path("/data/options.json")):
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            return None
        return Options.model_validate_json(path.read_bytes())
    except (OSError, ValueError, ValidationError):
        return None


def deployment_view(response):
    """Let an unauthenticated page identify its surface without exposing data."""
    try:
        value = json.loads(response.body)
        if (response.status_code == 200 and value.get("mode") == "bridge"
                and value.get("authenticated") is True and value.get("bridge_state") == "ready"):
            return JSONResponse({"mode": "bridge", "authenticated": True, "bridge_state": "ready"})
        code = value.get("detail")
    except (ValueError, AttributeError):
        code = None
    state = "unavailable"
    if response.status_code == 401 and code == "bridge_unpaired":
        state = "unpaired"
    elif response.status_code == 403 and code in {"bridge_owner_required", "bridge_authority_revoked"}:
        state = "owner_required"
    return JSONResponse({"mode": "bridge", "authenticated": False, "bridge_state": state})


def create_app(*, relay=None, options=None, ingress_ip="172.30.32.2", static=None, heartbeat_interval=20):
    options = options if options is not None else load_options()
    relay = relay if relay is not None else (Relay(options) if options else None)

    @asynccontextmanager
    async def lifespan(app):
        async def heartbeat():
            while True:
                try:
                    await relay.call({"user_id": "", "method": "GET", "path": "/api/bridge/heartbeat"})
                except RelayError:
                    pass
                await asyncio.sleep(heartbeat_interval)

        task = None
        if relay:
            await relay.start()
            task = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if relay:
                await relay.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def ingress_identity(request: Request, call_next):
        peer = request.client.host if request.client else ""
        if request.url.path == "/health" and peer in {ingress_ip, "127.0.0.1", "::1"}:
            # Liveness only: a disconnected outer host must not cause restart loops.
            return JSONResponse({"status": "ok", "version": VERSION, "kind": "ingress_bridge"})
        identities = request.headers.getlist("x-remote-user-id")
        if peer != ingress_ip or len(identities) != 1 or not USER_ID.fullmatch(identities[0]):
            return JSONResponse({"detail": "請從 Home Assistant 開啟智能管家"}, status_code=403)
        request.state.user_id = identities[0]
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'self'; form-action 'self'"
        )
        return response

    @app.api_route("/api/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        if not relay:
            if path == "deployment" and request.method == "GET":
                return JSONResponse({"mode": "bridge", "authenticated": False, "bridge_state": "unpaired"})
            return JSONResponse({"detail": "尚未連接外層管家，請從外層設定完成連接。"}, status_code=503)
        route = "/api/" + path
        # Neither browser requests nor untrusted paths can reach service-only health.
        if (len(route) > 512 or not re.fullmatch(r"/api/[A-Za-z0-9_./-]+", route)
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or route.startswith("/api/bridge/heartbeat")):
            return JSONResponse({"detail": "不支援此操作"}, status_code=404)
        if request.url.query:
            return JSONResponse({"detail": "不支援此操作"}, status_code=404)
        body = None
        if request.method == "POST":
            origin = request.headers.get("origin", "")
            try:
                parts = urlsplit(origin)
            except ValueError:
                return JSONResponse({"detail": "頁面來源不符，請重新開啟。"}, status_code=403)
            if (parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password
                    or parts.path or parts.query or parts.fragment
                    or parts.netloc != request.headers.get("host", "")
                    or request.headers.get("sec-fetch-site", "") not in {"same-origin", "same-site", ""}
                    or request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json"):
                return JSONResponse({"detail": "頁面來源不符，請重新開啟。"}, status_code=403)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_REQUEST:
                    return JSONResponse({"detail": "訊息太大"}, status_code=413)
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError()
            except ValueError:
                return JSONResponse({"detail": "訊息格式無效"}, status_code=422)
        payload = {"user_id": request.state.user_id, "method": request.method, "path": route, "body": body,
                   "csrf": request.headers.get("x-agentic-csrf", ""),
                   "idempotency_key": request.headers.get("idempotency-key", ""),
                   "origin": request.headers.get("origin", ""), "host": request.headers.get("host", ""),
                   "fetch_site": request.headers.get("sec-fetch-site", "")}
        try:
            response = await relay.call(payload)
            return deployment_view(response) if path == "deployment" and request.method == "GET" else response
        except RelayError:
            if path == "deployment" and request.method == "GET":
                return JSONResponse({"mode": "bridge", "authenticated": False, "bridge_state": "unavailable"})
            return JSONResponse({"detail": "暫時連不上外層管家，對話仍保留，請稍後重試。"}, status_code=503)

    location = Path(static) if static else Path("/app/static")
    if location.is_dir():
        app.mount("/", StaticFiles(directory=location, html=True), name="static")
    return app
