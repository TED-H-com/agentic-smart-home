"""Native HA actions backed by the existing outer Controller owner connection."""
from __future__ import annotations

import logging
from datetime import timedelta

import probatio
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, ServiceValidationError, Unauthorized
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, PLATFORMS
from .persistence import CheckedStore
from .queue import NotificationQueue, QueueError, require

_LOGGER = logging.getLogger(__name__)
COMMAND_FIELDS = {
    "status": {},
    "update_status": {"controller_id": str, "enabled": bool, "routing_revision": str,
                      "aliases": list, "channels": list},
    "pull": {"controller_id": str},
    "claim": {"controller_id": str, "source_id": str, "epoch": str, "request_id": str,
              "digest": str, "routing_revision": str, "claim_id": str},
    "ack": {"controller_id": str, "source_id": str, "epoch": str, "request_id": str,
            "digest": str, "claim_id": str, "state": str, "reason": probatio.Any(str, None)},
}


class Runtime:
    def __init__(self, hass, entry, queue):
        self.hass, self.entry, self.queue = hass, entry, queue
        self.listeners = set()

    def changed(self):
        for callback in tuple(self.listeners):
            callback()

    async def tick(self, _now):
        if self.queue.healthy:
            try:
                await self.queue.expire()
            except QueueError:
                pass
        self.changed()


def runtime(hass):
    entries = hass.config_entries.async_entries(DOMAIN)
    require(len(entries) == 1 and entries[0].state is ConfigEntryState.LOADED
            and getattr(entries[0], "runtime_data", None) is not None, "not_configured")
    return entries[0].runtime_data


async def current_owner(hass, connection):
    user = connection.user
    if user is None:
        raise Unauthorized
    user = await hass.auth.async_get_user(user.id)
    if user is None or not user.is_owner or not user.is_active or user.system_generated:
        raise Unauthorized
    return user.id


def websocket_handler(command):
    schema = {probatio.Required("type"): f"{DOMAIN}/{command}",
              **{probatio.Required(key): value for key, value in COMMAND_FIELDS[command].items()}}

    @websocket_api.websocket_command(schema)
    @websocket_api.async_response
    async def handle(hass, connection, msg):
        owner_id = await current_owner(hass, connection)
        try:
            require(set(msg) == {"id", "type", *COMMAND_FIELDS[command]})
            manager = runtime(hass)
            fields = {key: msg[key] for key in COMMAND_FIELDS[command]}
            if command == "status":
                result = manager.queue.status(owner_id)
            else:
                result = await getattr(manager.queue, command)(owner_id, **fields)
            manager.changed()
            connection.send_result(msg["id"], result)
        except QueueError as error:
            connection.send_error(msg["id"], error.code, error.code)

    return handle


async def async_setup(hass, config):
    for command in COMMAND_FIELDS:
        websocket_api.async_register_command(hass, websocket_handler(command))

    async def send_text(call):
        try:
            require(set(call.data) == {"recipient", "message"})
            if call.context.user_id is not None:
                user = await hass.auth.async_get_user(call.context.user_id)
                if user is None or not user.is_active or not user.is_admin:
                    raise Unauthorized
            manager = runtime(hass)
            owner_id = manager.queue.data["owner_id"]
            owner = await hass.auth.async_get_user(owner_id) if owner_id else None
            require(owner is not None and owner.is_owner and owner.is_active and not owner.system_generated,
                    "needs_rebind")
            result = await manager.queue.enqueue(owner_id, call.data["recipient"], call.data["message"])
            manager.changed()
            return result
        except QueueError as error:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key=error.code) from None

    hass.services.async_register(DOMAIN, "send_text", send_text,
        schema=probatio.Schema({probatio.Required("recipient"): str, probatio.Required("message"): str}),
        supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass, entry):
    persistence = CheckedStore(hass, entry.entry_id)
    queue = NotificationQueue(persistence.save)
    try:
        require(set(entry.data) == {"source_id", "epoch", "initialized"}
                and type(entry.data["initialized"]) is bool, "storage_unavailable")
        await queue.restore(entry.data["source_id"], entry.data["epoch"], await persistence.load(),
                            allow_create=not entry.data["initialized"])
    except QueueError:
        raise ConfigEntryError("Notification queue storage requires repair; enrollment has not been reset") from None
    if not entry.data["initialized"]:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "initialized": True})
    entry.runtime_data = Runtime(hass, entry, queue)
    entry.async_on_unload(async_track_time_interval(hass, entry.runtime_data.tick, timedelta(seconds=5)))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    entry.runtime_data.queue.last_heartbeat = None
    if hass.state not in {CoreState.stopping, CoreState.final_write, CoreState.stopped}:
        try:
            await entry.runtime_data.queue.mark_uncertain()
        except QueueError:
            _LOGGER.error("Notification queue storage unavailable during unload")
    return True


async def async_remove_entry(hass, entry):
    # Removing this integration is the explicit reset boundary; re-adding creates
    # a new source and epoch and cannot authorize old requests or claims.
    await CheckedStore(hass, entry.entry_id).remove()
