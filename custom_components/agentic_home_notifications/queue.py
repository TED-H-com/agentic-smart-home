"""Bounded, durable notification requests; no transport or credentials live here."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta

DOMAIN = "agentic_home_notifications"
MAX_PENDING, MAX_RECENT, MAX_ALIASES = 32, 64, 16
MAX_TEXT, TTL, HEARTBEAT = 1900, 3600, 60
HEX = re.compile(r"[a-f0-9]{32}")
SHA = re.compile(r"[a-f0-9]{64}")
ALIAS = re.compile(r"[a-z][a-z0-9_]{0,31}")
REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
CHANNELS = {"line", "telegram", "discord"}
REQUEST_FIELDS = {"id", "source_id", "epoch", "routing_revision", "recipient", "kind", "message",
                  "created_at", "expires_at", "digest"}
ACK_FIELDS = {"controller_id", "source_id", "epoch", "request_id", "digest", "claim_id", "state", "reason"}


class QueueError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code="invalid_request"):
    if not condition:
        raise QueueError(code)


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise QueueError("invalid_request") from None
    return hashlib.sha256(raw).hexdigest()


def timestamp(value):
    require(isinstance(value, str) and len(value) <= 40)
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise QueueError("invalid_request") from None
    require(result.tzinfo is not None and result.utcoffset() == timedelta(0))
    return result


def validate_request(value):
    require(isinstance(value, dict) and set(value) == REQUEST_FIELDS)
    for key in ("id", "source_id", "epoch"):
        require(matches(HEX, value[key]))
    require(matches(SHA, value["routing_revision"]) and matches(ALIAS, value["recipient"]))
    require(value["kind"] == "text" and isinstance(value["message"], str)
            and 1 <= len(value["message"]) <= MAX_TEXT)
    require(timedelta(0) < timestamp(value["expires_at"]) - timestamp(value["created_at"]) <= timedelta(seconds=TTL))
    require(value["digest"] == digest({k: v for k, v in value.items() if k != "digest"}))


def validate_aliases(values):
    require(isinstance(values, list) and len(values) <= MAX_ALIASES)
    seen = set()
    for value in values:
        require(isinstance(value, dict) and set(value) == {"alias", "label", "channel", "capabilities"})
        require(matches(ALIAS, value["alias"]) and value["alias"] not in seen)
        require(isinstance(value["label"], str) and len(value["label"]) <= 120)
        require(isinstance(value["channel"], str) and value["channel"] in CHANNELS
                and value["capabilities"] == ["text"])
        seen.add(value["alias"])
    digest(values)


def validate_channels(values):
    require(isinstance(values, list) and len(values) <= 3)
    seen = set()
    for value in values:
        require(isinstance(value, dict) and set(value) == {"channel", "transport", "login"})
        require(isinstance(value["channel"], str) and value["channel"] in CHANNELS and value["channel"] not in seen)
        require(isinstance(value["transport"], str)
                and value["transport"] in {"connected", "connecting", "disconnected", "error", "disabled"})
        require(isinstance(value["login"], str) and value["login"] in {"ready", "needs_login", "unknown", "not_required"})
        seen.add(value["channel"])


def validate_ack(value):
    require(isinstance(value, dict) and set(value) == ACK_FIELDS)
    for key in ("controller_id", "source_id", "epoch", "request_id", "claim_id"):
        require(matches(HEX, value[key]))
    require(matches(SHA, value["digest"]) and isinstance(value["state"], str)
            and value["state"] in {"submitted", "unknown", "not_sent"})
    require(value["reason"] is None or matches(REASON, value["reason"]))


class NotificationQueue:
    """Single consumer enrollment; claims are durable one-shot grants, never leases."""

    def __init__(self, save, *, now=lambda: datetime.now(UTC), tick=time.monotonic):
        self.save, self.now, self.tick = save, now, tick
        self.lock = asyncio.Lock()
        self.data = None
        self.last_heartbeat = None
        self.healthy = True

    async def restore(self, source_id, epoch, stored, *, allow_create=False):
        require(matches(HEX, source_id) and matches(HEX, epoch), "storage_unavailable")
        if stored is None:
            require(allow_create, "storage_unavailable")
            initial = {"schema": 1, "source_id": source_id, "epoch": epoch, "revision": 0,
                       "owner_id": None, "controller_id": None, "enabled": False,
                       "routing_revision": "", "aliases": [], "channels": [], "queue": [], "recent": []}
            await self._commit(initial)
            return
        try:
            self._validate_stored(stored, source_id, epoch)
        except (QueueError, KeyError, TypeError, ValueError):
            self.healthy = False
            raise QueueError("storage_unavailable") from None
        self.data = copy.deepcopy(stored)
        proposed = copy.deepcopy(stored)
        self._expire(proposed)
        for item in proposed["queue"]:
            if item["claim"]:
                item["uncertain"] = True
        if proposed != stored:
            await self._commit(proposed)

    def _validate_stored(self, value, source_id, epoch):
        require(isinstance(value, dict) and set(value) == {
            "schema", "source_id", "epoch", "revision", "owner_id", "controller_id", "enabled",
            "routing_revision", "aliases", "channels", "queue", "recent"})
        require(type(value["schema"]) is int and value["schema"] == 1
                and type(value["revision"]) is int and value["revision"] >= 1)
        require(value["source_id"] == source_id and value["epoch"] == epoch)
        require(type(value["enabled"]) is bool)
        if value["controller_id"] is None:
            require(value["owner_id"] is None and value["enabled"] is False
                    and value["routing_revision"] == "" and not value["aliases"]
                    and not value["channels"] and not value["queue"] and not value["recent"])
        else:
            require(matches(HEX, value["controller_id"]) and matches(HEX, value["owner_id"])
                    and matches(SHA, value["routing_revision"]))
        validate_aliases(value["aliases"])
        validate_channels(value["channels"])
        require(isinstance(value["queue"], list) and len(value["queue"]) <= MAX_PENDING)
        require(isinstance(value["recent"], list) and len(value["recent"]) <= MAX_RECENT)
        seen = set()
        for item in value["queue"]:
            require(isinstance(item, dict) and set(item) == {"request", "claim", "uncertain"})
            request = item["request"]
            validate_request(request)
            require(request["source_id"] == source_id and request["epoch"] == epoch
                    and request["id"] not in seen and type(item["uncertain"]) is bool)
            seen.add(request["id"])
            claim = item["claim"]
            if claim is not None:
                require(isinstance(claim, dict) and set(claim) == {"id", "at"} and matches(HEX, claim["id"]))
                timestamp(claim["at"])
            else:
                require(item["uncertain"] is False)
        for receipt in value["recent"]:
            require(isinstance(receipt, dict) and set(receipt) == {"request_id", "recipient", "state", "reason",
                    "updated_at", "expires_at", "ack"} and receipt["request_id"] not in seen)
            require(matches(HEX, receipt["request_id"]) and matches(ALIAS, receipt["recipient"])
                    and receipt["state"] in {"submitted", "unknown", "not_sent"})
            require(receipt["reason"] is None or matches(REASON, receipt["reason"]))
            timestamp(receipt["updated_at"])
            timestamp(receipt["expires_at"])
            if receipt["ack"] is not None:
                validate_ack(receipt["ack"])
                require(receipt["ack"]["source_id"] == source_id and receipt["ack"]["epoch"] == epoch
                        and receipt["ack"]["controller_id"] == value["controller_id"]
                        and receipt["ack"]["request_id"] == receipt["request_id"]
                        and receipt["ack"]["state"] == receipt["state"]
                        and receipt["ack"]["reason"] == receipt["reason"])
            seen.add(receipt["request_id"])

    async def _commit(self, proposed):
        require(self.healthy, "storage_unavailable")
        proposed = copy.deepcopy(proposed)
        proposed["revision"] += 1
        try:
            await self.save(proposed)
        except BaseException:
            # The write may have happened. Never continue from an older in-memory claim.
            self.healthy = False
            self.last_heartbeat = None
            raise
        self.data = proposed

    def check_owner(self, owner_id):
        require(self.healthy and self.data is not None, "storage_unavailable")
        require(matches(HEX, owner_id), "unauthorized")
        require(self.data["owner_id"] in (None, owner_id), "needs_rebind")

    def _bound(self, owner_id, controller_id):
        self.check_owner(owner_id)
        require(matches(HEX, controller_id))
        require(self.data["controller_id"] == controller_id and self.data["owner_id"] == owner_id,
                "needs_rebind")

    @property
    def available(self):
        return (self.healthy and self.last_heartbeat is not None
                and 0 <= self.tick() - self.last_heartbeat < HEARTBEAT)

    def _active(self):
        require(self.data["enabled"], "disabled")
        require(self.available, "stale_heartbeat")

    def _terminal(self, proposed, item, state, reason, ack=None):
        request = item["request"]
        receipt = {"request_id": request["id"], "recipient": request["recipient"], "state": state,
                   "reason": reason, "updated_at": self.now().isoformat(),
                   "expires_at": request["expires_at"], "ack": copy.deepcopy(ack)}
        proposed["recent"].append(receipt)
        proposed["recent"] = proposed["recent"][-MAX_RECENT:]

    def _expire(self, proposed):
        retained = []
        for item in proposed["queue"]:
            if timestamp(item["request"]["expires_at"]) <= self.now():
                self._terminal(proposed, item, "unknown" if item["claim"] else "not_sent",
                               "claim_expired" if item["claim"] else "expired")
            else:
                retained.append(item)
        proposed["queue"] = retained

    def status(self, owner_id):
        self.check_owner(owner_id)
        receipt = self.data["recent"][-1] if self.data["recent"] else None
        uncertain = [item for item in self.data["queue"] if item["claim"] and item["uncertain"]]
        if uncertain:
            item = uncertain[-1]
            receipt = {"request_id": item["request"]["id"], "recipient": item["request"]["recipient"],
                       "state": "unknown", "reason": "claim_interrupted", "updated_at": item["claim"]["at"]}
        public_receipt = ({k: receipt[k] for k in ("request_id", "recipient", "state", "reason", "updated_at")}
                          if receipt else None)
        return {"protocol": 1, "source_id": self.data["source_id"], "epoch": self.data["epoch"],
                "controller_id": self.data["controller_id"], "enabled": self.data["enabled"],
                "routing_revision": self.data["routing_revision"], "aliases": copy.deepcopy(self.data["aliases"]),
                "pending": len(self.data["queue"]), "last_delivery": public_receipt}

    async def update_status(self, owner_id, controller_id, enabled, routing_revision, aliases, channels):
        require(matches(HEX, controller_id) and type(enabled) is bool and matches(SHA, routing_revision))
        validate_aliases(aliases)
        validate_channels(channels)
        async with self.lock:
            self.check_owner(owner_id)
            require(self.data["controller_id"] in (None, controller_id), "needs_rebind")
            proposed = copy.deepcopy(self.data)
            self._expire(proposed)
            # Even if a caller fails to rotate a revision, it cannot retarget aliases under it.
            if proposed["routing_revision"] == routing_revision:
                require(proposed["aliases"] == aliases and proposed["enabled"] == enabled, "routing_conflict")
            retained = []
            for item in proposed["queue"]:
                if item["claim"] is None and item["request"]["routing_revision"] != routing_revision:
                    self._terminal(proposed, item, "not_sent", "routing_changed")
                else:
                    retained.append(item)
            proposed.update(owner_id=owner_id, controller_id=controller_id, enabled=enabled,
                            routing_revision=routing_revision, aliases=copy.deepcopy(aliases),
                            channels=copy.deepcopy(channels), queue=retained)
            if proposed != self.data:
                await self._commit(proposed)
            self.last_heartbeat = self.tick()
            return self.status(owner_id)

    async def enqueue(self, owner_id, recipient, message):
        require(matches(ALIAS, recipient) and isinstance(message, str) and 1 <= len(message) <= MAX_TEXT)
        async with self.lock:
            self.check_owner(owner_id)
            require(self.data["owner_id"] == owner_id and self.data["controller_id"] is not None, "needs_rebind")
            self._active()
            require(any(item["alias"] == recipient for item in self.data["aliases"]), "unknown_recipient")
            proposed = copy.deepcopy(self.data)
            self._expire(proposed)
            require(len(proposed["queue"]) < MAX_PENDING, "queue_full")
            now = self.now()
            request = {"id": uuid.uuid4().hex, "source_id": proposed["source_id"], "epoch": proposed["epoch"],
                       "routing_revision": proposed["routing_revision"], "recipient": recipient,
                       "kind": "text", "message": message, "created_at": now.isoformat(),
                       "expires_at": (now + timedelta(seconds=TTL)).isoformat()}
            request["digest"] = digest(request)
            proposed["queue"].append({"request": request, "claim": None, "uncertain": False})
            await self._commit(proposed)
            return {"request_id": request["id"], "state": "queued"}

    async def pull(self, owner_id, controller_id):
        async with self.lock:
            self._bound(owner_id, controller_id)
            self._active()
            proposed = copy.deepcopy(self.data)
            self._expire(proposed)
            if proposed != self.data:
                await self._commit(proposed)
            ready_channels = {channel["channel"] for channel in self.data["channels"]
                              if channel["transport"] == "connected"
                              and channel["login"] in {"ready", "not_required"}}
            ready_aliases = {alias["alias"] for alias in self.data["aliases"]
                             if alias["channel"] in ready_channels}
            return {"requests": [copy.deepcopy(item["request"]) for item in self.data["queue"]
                    if item["claim"] is None and item["request"]["routing_revision"] == self.data["routing_revision"]
                    and item["request"]["recipient"] in ready_aliases][:8]}

    def _origin(self, source_id, epoch):
        require(source_id == self.data["source_id"] and epoch == self.data["epoch"], "needs_rebind")

    async def claim(self, owner_id, controller_id, source_id, epoch, request_id, digest, routing_revision, claim_id):
        for value in (controller_id, source_id, epoch, request_id, claim_id):
            require(matches(HEX, value))
        require(matches(SHA, digest) and matches(SHA, routing_revision))
        async with self.lock:
            self._bound(owner_id, controller_id)
            self._origin(source_id, epoch)
            self._active()
            proposed = copy.deepcopy(self.data)
            self._expire(proposed)
            item = next((item for item in proposed["queue"] if item["request"]["id"] == request_id), None)
            if item is not None:
                require(item["request"]["digest"] == digest
                        and item["request"]["routing_revision"] == routing_revision)
            if item is None or item["claim"] is not None or routing_revision != proposed["routing_revision"]:
                if proposed != self.data:
                    await self._commit(proposed)
                return {"granted": False, "request": None}
            item["claim"] = {"id": claim_id, "at": self.now().isoformat()}
            await self._commit(proposed)
            return {"granted": True, "request": copy.deepcopy(item["request"])}

    async def ack(self, owner_id, **receipt):
        validate_ack(receipt)
        async with self.lock:
            self._bound(owner_id, receipt["controller_id"])
            self._origin(receipt["source_id"], receipt["epoch"])
            proposed = copy.deepcopy(self.data)
            self._expire(proposed)
            item = next((item for item in proposed["queue"]
                         if item["request"]["id"] == receipt["request_id"]), None)
            if item is None:
                old = next((item for item in proposed["recent"] if item["request_id"] == receipt["request_id"]), None)
                if proposed != self.data:
                    await self._commit(proposed)
                if old is None or timestamp(old["expires_at"]) <= self.now() or old["ack"] is None:
                    return {"acknowledged": False}
                require(old["ack"] == receipt, "receipt_conflict")
                return {"acknowledged": True}
            require(item["request"]["digest"] == receipt["digest"] and item["claim"] is not None
                    and item["claim"]["id"] == receipt["claim_id"], "receipt_conflict")
            self._terminal(proposed, item, receipt["state"], receipt["reason"], receipt)
            proposed["queue"].remove(item)
            await self._commit(proposed)
            return {"acknowledged": True}

    async def mark_uncertain(self):
        self.last_heartbeat = None
        async with self.lock:
            require(self.healthy, "storage_unavailable")
            proposed = copy.deepcopy(self.data)
            self._expire(proposed)
            for item in proposed["queue"]:
                if item["claim"]:
                    item["uncertain"] = True
            if proposed != self.data:
                await self._commit(proposed)

    async def expire(self):
        async with self.lock:
            require(self.healthy, "storage_unavailable")
            proposed = copy.deepcopy(self.data)
            self._expire(proposed)
            if proposed != self.data:
                await self._commit(proposed)
