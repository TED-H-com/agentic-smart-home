"""Owner-initiated local HA OAuth, without asking people to copy HA tokens.

The temporary OAuth grant proves identity. After verifying the active owner we
create a named, revocable one-year HA access token and use the existing binding
path. Neither credential nor authorization code goes to an agent or browser API.
"""

import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from urllib.parse import urlencode

import aiohttp
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .durability import client_session, durable
from .ha import HomeAssistant
from .host import connect_home_owner
from .models import Strict


class Authorize(Strict):
    pass


class HomeAuthorization:
    def __init__(self, auth, ha, store, *, factory=HomeAssistant, bind=connect_home_owner):
        self.auth, self.ha, self.store = auth, ha, store
        self.factory, self.bind = factory, bind
        self.lock = asyncio.Lock()

    def public(self):
        identifier = self.store.setting("ha_oauth_current")
        record = self.store.get("ha_oauth", identifier) if identifier else None
        if not record:
            return {"state": "idle", "error_code": None}
        state = record["state"]
        if state == "pending" and record["expires"] < time.time():
            state = "expired"
        return {"state": state, "error_code": record.get("error_code")}

    def start(self, request, key):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,200}", key):
            raise HTTPException(400, "缺少有效的 Idempotency-Key")
        authority = request.headers.get("host")
        if authority not in {"localhost:8099", "127.0.0.1:8099"}:
            raise HTTPException(403, "請從本機管理頁面開始授權")
        cookie = request.cookies.get("agentic_host", "")
        state = self.auth.sign("ha-onboarding:" + cookie + ":" + key)
        identifier = hashlib.sha256(state.encode()).hexdigest()
        record = self.store.get("ha_oauth", identifier)
        if record and (record["expires"] < time.time() or record["state"] != "pending"):
            raise HTTPException(409, "此授權已結束，請重新開始")
        origin = "http://" + authority
        if not record:
            record = {"id": identifier, "state": "pending", "expires": time.time() + 600,
                      "cookie_digest": hashlib.sha256(cookie.encode()).hexdigest(), "origin": origin,
                      "owner": self.store.setting("owner"), "epoch": self.store.setting("hermes_web_epoch"),
                      "error_code": None}
            self.store.put("ha_oauth", record)
        self.store.set_setting("ha_oauth_current", identifier)
        browser_ha = "http://" + authority.split(":")[0] + ":8123"
        return {"url": browser_ha + "/auth/authorize?" + urlencode({
            "client_id": origin + "/", "redirect_uri": origin + "/api/host/authorized", "state": state})}

    async def token_request(self, path, fields):
        # URL comes from trusted deployment, never an OAuth redirect or model.
        base = self.ha.base.removesuffix("/api")
        async with client_session(self.store, timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
            async with session.post(base + path, data=fields, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError("ha_oauth_exchange_failed")
                if path == "/auth/revoke":
                    return {}
                body = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    body.extend(chunk)
                    if len(body) > 32768:
                        raise ValueError("ha_oauth_exchange_failed")
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError("ha_oauth_exchange_failed")
                for name in ("access_token", "refresh_token"):
                    if not isinstance(result.get(name), str) or not 20 <= len(result[name]) <= 16384:
                        raise ValueError("ha_oauth_exchange_failed")
                if result.get("token_type", "").lower() != "bearer":
                    raise ValueError("ha_oauth_exchange_failed")
                return result

    def require_scope(self, request, identifier, expected_state):
        """Check current authority synchronously, including at credential commit."""
        record = self.store.get("ha_oauth", identifier)
        cookie_digest = hashlib.sha256(request.cookies.get("agentic_host", "").encode()).hexdigest()
        if (not record or self.store.setting("ha_oauth_current") != identifier or
            self.store.setting("updater_gate") or
            record["state"] != expected_state or record["expires"] <= time.time() or
            not self.auth.verify(request) or
            not hmac.compare_digest(record["cookie_digest"], cookie_digest) or
            record["origin"] != "http://" + request.headers.get("host", "") or
            record["owner"] != self.store.setting("owner") or
            record["epoch"] != self.store.setting("hermes_web_epoch")):
            raise ValueError("ha_authorization_expired")
        return record

    async def finish(self, request, state, code):
        if not re.fullmatch(r"[a-f0-9]{64}", state or "") or not isinstance(code, str) or not 1 <= len(code) <= 4096:
            raise HTTPException(400, "授權回應無效，請重新開始")
        identifier = hashlib.sha256(state.encode()).hexdigest()
        async with self.lock:
            try:
                record = self.require_scope(request, identifier, "pending")
            except ValueError:
                raise HTTPException(400, "授權已過期或來源不符，請重新開始") from None
            record["state"] = "exchanging"
            self.store.put("ha_oauth", record)  # Consume before a remote effect; never replay code.
            def commit_guard():
                self.require_scope(request, identifier, "exchanging")

            grant, candidate, connected = None, None, False
            client_name = "Agentic Smart Home " + uuid.uuid4().hex
            try:
                grant = await self.token_request("/auth/token", {"grant_type": "authorization_code",
                    "code": code, "client_id": record["origin"] + "/"})
                candidate = self.factory(grant["access_token"], self.ha.base, self.ha.websocket, external=True)
                candidate.store = self.store
                await candidate.start()
                user = await candidate.ws("auth/current_user")
                if not await candidate.owner(user["id"]):
                    raise ValueError("ha_owner_required")
                if self.store.setting("owner") not in {None, user["id"]}:
                    raise ValueError("ha_different_household")
                commit_guard()
                token = await candidate.ws("auth/long_lived_access_token", client_name=client_name, lifespan=365)
                if not isinstance(token, str) or not 20 <= len(token) <= 16384:
                    raise ValueError("ha_oauth_exchange_failed")
                commit_guard()
                # Binding performs additional awaited owner checks. Its synchronous
                # guard runs after those checks, immediately before writing anything.
                await self.bind(self.auth, self.ha, self.store, token, commit_guard=commit_guard)
                connected = True
                record.update(state="connected", error_code=None)
            except Exception as exc:
                code = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                    "ha_owner_required", "ha_different_household", "ha_authorization_expired"} else "ha_authorization_failed"
                record.update(state="failed", error_code=code)
            finally:
                if candidate:
                    if not connected:
                        try:
                            # Only delete the token created by this exact attempt.
                            tokens = await candidate.ws("auth/refresh_tokens")
                            for item in tokens:
                                if item.get("client_name") == client_name and item.get("type") == "long_lived_access_token":
                                    await candidate.ws("auth/delete_refresh_token", refresh_token_id=item["id"])
                        except Exception:
                            record["cleanup"] = "unconfirmed"
                    await candidate.close()
                if grant:
                    try:
                        await self.token_request("/auth/revoke", {"token": grant["refresh_token"]})
                    except Exception:
                        record["cleanup"] = "unconfirmed"
                self.store.put("ha_oauth", record)
        return self.public()

    def reject_callback(self, request, state):
        # A rejected current handoff may show a safe failure. An old/forged state
        # must not overwrite the newer handoff, and no code is ever retained.
        if not re.fullmatch(r"[a-f0-9]{64}", state or ""):
            return
        identifier = hashlib.sha256(state.encode()).hexdigest()
        try:
            record = self.require_scope(request, identifier, "pending")
        except ValueError:
            return
        record.update(state="failed", error_code="ha_authorization_failed")
        self.store.put("ha_oauth", record)


def routes(app, auth, ha, store):
    service = HomeAuthorization(auth, ha, store)
    app.state.ha_authorization = service

    @app.post("/api/host/authorize")
    async def authorize(body: Authorize, request: Request):
        return service.start(request, request.headers.get("idempotency-key", ""))

    @app.get("/api/host/authorization")
    async def authorization_status():
        return service.public()

    @app.get("/api/host/authorized")
    async def authorized(request: Request):
        # No token/code is reflected, retained in records, or placed in final URL.
        state = request.query_params.get("state")
        try:
            await service.finish(request, state, request.query_params.get("code"))
        except HTTPException:
            service.reject_callback(request, state)
        await durable(service.store)
        return RedirectResponse("/#onboarding", status_code=303,
                                headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
