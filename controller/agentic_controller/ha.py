from __future__ import annotations

import asyncio
import random
from collections import deque
from datetime import datetime
from urllib.parse import quote

import aiohttp

from .durability import client_session, durable
from .models import digest, utcnow

TESTED_CORE_VERSIONS = frozenset({"2026.10.1"})
# MCP tool calls must wait longer than this. See hermes MCP_TOOL_TIMEOUT.
HA_CLIENT_TIMEOUT = 30
# These commands observe Core state or subscribe to observations. Unknown
# commands stay fenced so a newly added mutation cannot silently bypass storage.
READ_ONLY_WS_COMMANDS = frozenset({
    "auth", "ping", "get_states", "get_config", "get_services", "subscribe_events",
    "auth/current_user", "config/auth/list", "auth/refresh_tokens",
    "config/entity_registry/list", "config/device_registry/list", "config/area_registry/list",
    "config/floor_registry/list", "config/label_registry/list",
    "cloud/status", "config_entries/get", "config_entries/flow/progress", "validate_config", "automation/config",
    "network", "network/url",
})


def read_only_ws(command, fields):
    if command == "supervisor/api":
        method = fields.get("method")
        return isinstance(method, str) and method.upper() in {"GET", "HEAD", "OPTIONS"}
    return command in READ_ONLY_WS_COMMANDS


class HAError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class HAUnreachable(HAError):
    """Home Assistant did not answer the request."""


class HomeAssistant:
    def __init__(self, token, base="http://supervisor/core/api", websocket="ws://supervisor/core/websocket",
                 supervisor="http://supervisor", external=False):
        self.token, self.base, self.websocket, self.supervisor = token, base.rstrip("/"), websocket, supervisor
        self.external = external
        self.store = None
        self.session = None
        self.connected = False
        self.last_observed = None
        self.observations = deque(maxlen=1000)

    async def start(self):
        self.session = client_session(self.store,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=aiohttp.ClientTimeout(total=HA_CLIENT_TIMEOUT), trust_env=False)

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(self, method, path, data=None, *, supervisor=False, missing_ok=False, timeout=None):
        if timeout is None:
            timeout = HA_CLIENT_TIMEOUT
        if supervisor and self.external:
            # Core's authenticated owner WebSocket forwards to Supervisor. No Supervisor token leaves HAOS.
            return await self.ws("supervisor/api", endpoint=path, method=method.lower(), data=data or {},
                                 timeout=timeout, _receive_timeout=timeout + 10)
        try:
            async with self.session.request(method, (self.supervisor if supervisor else self.base) + path,
                                            json=data, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if missing_ok and response.status == 404:
                    return None
                if response.status >= 300:
                    raise HAError(f"Home Assistant {method} {path}: HTTP {response.status}", response.status)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 8 * 1024 * 1024:
                        raise HAError("HA 回應超過大小上限")
                import json
                result = json.loads(raw) if raw else {}
                if supervisor:
                    if result.get("result") != "ok":
                        raise HAError("Supervisor 操作未成功")
                    return result.get("data", {})
                return result
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HAUnreachable("Home Assistant 連線中斷或回應無效") from exc
        except ValueError as exc:
            raise HAError("Home Assistant 連線中斷或回應無效") from exc

    async def ws_connect(self):
        ws = await self.session.ws_connect(self.websocket, heartbeat=20, max_msg_size=8 * 1024 * 1024)
        try:
            msg = await ws.receive_json(timeout=15)
            if msg.get("type") != "auth_required":
                raise HAError("HA WebSocket 握手無效")
            await ws.send_json({"type": "auth", "access_token": self.token})
            if (await ws.receive_json(timeout=15)).get("type") != "auth_ok":
                raise HAError("HA WebSocket 認證失敗", 401)
        except BaseException:
            await ws.close()
            raise
        return ws

    async def ws(self, command, *, _receive_timeout=20, **kwargs):
        try:
            ws = await self.ws_connect()
            async with ws:
                if not read_only_ws(command, kwargs):
                    await durable(self.store)
                await ws.send_json({"id": 1, "type": command, **kwargs})
                result = await ws.receive_json(timeout=_receive_timeout)
                if result.get("id") != 1 or result.get("success") is not True:
                    code = result.get("error", {}).get("code", "invalid_response")
                    if command == "supervisor/api" and code == "unknown_command":
                        # Core is up, but its Supervisor integration has not loaded yet.
                        raise HAUnreachable(f"HA {command}: {code}")
                    raise HAError(f"HA {command}: {code}")
                return result["result"]
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in {401, 403}:
                raise HAError("HA WebSocket 認證失敗", exc.status) from exc
            raise HAUnreachable("HA WebSocket 連線中斷") from exc
        except (aiohttp.ClientError, TimeoutError, aiohttp.WSMessageTypeError) as exc:
            raise HAUnreachable("HA WebSocket 連線中斷") from exc
        except ValueError as exc:
            raise HAError("HA WebSocket 連線中斷") from exc

    async def owner(self, user_id):
        users = await self.ws("config/auth/list")
        return next((u for u in users if u["id"] == user_id and u.get("is_owner") is True
                     and u.get("is_active") is True), None)

    async def inventory(self):
        states, registry, devices, areas, services = await asyncio.gather(
            self.request("GET", "/states"), self.ws("config/entity_registry/list"),
            self.ws("config/device_registry/list"), self.ws("config/area_registry/list"),
            self.ws("get_services"))
        regs = {r["entity_id"]: r for r in registry}
        devs, area_names = {d["id"]: d for d in devices}, {a["area_id"]: a["name"] for a in areas}
        now, result = utcnow(), []
        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
            reg = regs.get(entity_id, {})
            device = devs.get(reg.get("device_id"), {})
            area_id = reg.get("area_id") or device.get("area_id")
            metadata = {"entity_id": entity_id, "domain": domain, "device_id": reg.get("device_id"),
                        "area_id": area_id, "device_class": state.get("attributes", {}).get("device_class"),
                        "supported_features": state.get("attributes", {}).get("supported_features"),
                        "config_entry_id": reg.get("config_entry_id")}
            result.append({**metadata, "metadata_hash": digest(metadata),
                           "name": str(state.get("attributes", {}).get("friendly_name", entity_id))[:150],
                           "area": area_names.get(area_id), "state": str(state["state"])[:256],
                           "availability": state["state"] if state["state"] in {"unknown", "unavailable"}
                           else "available", "observed_at": now, "last_changed": state.get("last_changed"),
                           "actions": [a for a in ("turn_on", "turn_off") if a in services.get(domain, {})]})
        return result

    async def validate(self, config):
        await self.ensure_write_compatibility()
        fields = {key: config[key] for key in ("triggers", "conditions", "actions")}
        try:
            result = await self.ws("validate_config", **fields)
        except HAError as exc:
            if "invalid_format" not in str(exc):
                raise
            # Older Core used singular keys. Both paths are read-only native validation.
            fields = {key[:-1]: value for key, value in fields.items()}
            result = await self.ws("validate_config", **fields)
        if any(result.get(key, {}).get("valid") is not True for key in fields):
            raise HAError("Home Assistant 原生設定驗證拒絕此規則", 422)

    async def ensure_write_compatibility(self):
        version = (await self.request("GET", "/config")).get("version")
        if version not in TESTED_CORE_VERSIONS:
            raise HAError(f"此版本尚未包含在自動化寫入相容範圍：Core {version}。請更新 Agentic Home。", 409)

    async def config(self, resource_id):
        return await self.request("GET", f"/config/automation/config/{quote(resource_id, safe='')}", missing_ok=True)

    async def write(self, resource_id, config):
        return await self.request("POST", f"/config/automation/config/{quote(resource_id, safe='')}", config)

    async def service(self, domain, service, entity_id):
        return await self.request("POST", f"/services/{domain}/{service}", {"entity_id": entity_id})

    async def automation_entity(self, resource_id, attempts=20, expected=None):
        for _ in range(attempts):
            states = await self.request("GET", "/states")
            found = next((s for s in states if s["entity_id"].startswith("automation.")
                          and s.get("attributes", {}).get("id") == resource_id), None)
            if found:
                if expected is None:
                    return found
                # The config writer acknowledges before its asynchronous reload finishes.
                # Compare the running entity's raw config, not just the file endpoint.
                try:
                    loaded = await self.ws("automation/config", entity_id=found["entity_id"])
                    if digest(loaded.get("config")) == digest(expected):
                        return found
                except HAError:
                    pass
            await asyncio.sleep(0.5)
        raise HAError("規則已寫入，HA 尚未載入相符的 automation 設定")

    async def dependencies(self, entity_ids, owned):
        """Inspect native automation configs, without changing other authors' resources."""
        states = await self.request("GET", "/states")
        conflicts = []
        for state in states:
            if not state["entity_id"].startswith("automation."):
                continue
            id = state.get("attributes", {}).get("id")
            if not id:
                raise HAError("現有 automation 缺少 ID，請先由原生 HA 編輯器補齊", 409)
            if id in owned:
                continue
            config = await self.config(id)
            text = str(config)
            if any(entity in text for entity in entity_ids):
                conflicts.append(state["entity_id"])
        return conflicts

    async def backup(self, name):
        # Core answers its API while it is still starting; a Supervisor backup of it then fails.
        core = await self.request("GET", "/config")
        if core.get("state", "RUNNING") != "RUNNING":
            raise HAError("Home Assistant 還在啟動；等它啟動完成後再部署")
        if self.external:
            status = await self.request("GET", "/supervisor/info", supervisor=True)
            if status.get("healthy") is not True:
                raise HAError("Supervisor 尚未就緒；等待 HAOS 啟動完成後再部署")
        data = await self.request("POST", "/backups/new/partial",
                                  {"name": name, "homeassistant": True, "addons": [],
                                   "folders": [], "background": False}, supervisor=True, timeout=600)
        slug = data.get("slug")
        if not slug:
            raise HAError("Supervisor 尚未完成備份")
        info = await self.request("GET", f"/backups/{quote(slug, safe='')}/info", supervisor=True)
        if info.get("slug") != slug or not info.get("homeassistant"):
            raise HAError("備份讀回未包含 Home Assistant")
        return {"id": slug, "created_at": utcnow(), "name": name}

    async def watch(self):
        delay = 1
        while True:
            try:
                ws = await self.ws_connect()
                async with ws:
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
                    ack = await ws.receive_json(timeout=20)
                    if not ack.get("success"):
                        raise HAError("狀態事件訂閱失敗")
                    # Re-snapshot after every reconnect. Never infer missing events.
                    await self.request("GET", "/states")
                    self.connected, self.last_observed, delay = True, utcnow(), 1
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        event = msg.json().get("event")
                        if event:
                            self.observations.append(event)
                            self.last_observed = utcnow()
            except (HAError, aiohttp.ClientError, TimeoutError, ValueError):
                pass
            finally:
                self.connected = False
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 30)

    def evidence(self, proposal):
        """Require a real source edge and correlated automation/target contexts."""
        intent = proposal.get("intent")
        if not intent or not proposal.get("deployed_at"):
            return None
        cutoff = datetime.fromisoformat(proposal["deployed_at"])
        events = [e for e in self.observations if datetime.fromisoformat(e["time_fired"]) >= cutoff]
        trigger = intent["trigger"]
        for event in events:
            data = event["data"]
            old, new = data.get("old_state") or {}, data.get("new_state") or {}
            if (data.get("entity_id") != trigger["entity_id"] or old.get("state") != trigger["from_state"]
                    or new.get("state") != trigger["to_state"]):
                continue
            source_context = (new.get("context") or event.get("context", {})).get("id")
            for run in events:
                auto = run["data"].get("new_state") or {}
                context = auto.get("context", {})
                if (auto.get("attributes", {}).get("id") != proposal["resource_id"] or not source_context
                        or context.get("parent_id") != source_context):
                    continue
                observed = []
                for action in intent["actions"]:
                    expected = "on" if action["action"] == "turn_on" else "off"
                    matched = next((target for target in events if
                        target["data"].get("entity_id") == action["entity_id"] and
                        (target["data"].get("new_state") or {}).get("state") == expected and
                        (target["data"].get("new_state") or {}).get("context", {}).get("id") == context.get("id")), None)
                    if matched:
                        observed.append({"entity_id": action["entity_id"], "state": expected,
                                         "at": matched["time_fired"], "context_id": context["id"]})
                if len(observed) == len(intent["actions"]):
                    return {"source": trigger["entity_id"], "source_at": event["time_fired"],
                            "automation_context": context["id"], "targets": observed,
                            "kind": "real_state_transition", "observed_at": utcnow()}
        return None
