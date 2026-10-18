"""HA Store with checked durable commits rather than best-effort saves."""
from __future__ import annotations

from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store

from .queue import DOMAIN, QueueError
from .storage_io import read_owned


class CheckedStore:
    def __init__(self, hass, entry_id):
        self.hass = hass
        self.key = f"{DOMAIN}.{entry_id}"
        self.store = Store(hass, 1, self.key, private=True, atomic_writes=True)

    async def load(self):
        try:
            # Store.async_load can turn corruption into empty state. For this
            # journal that would forget consumed claims, so validate disk directly.
            return await self.hass.async_add_executor_job(read_owned, self.store.path, self.key)
        except (OSError, ValueError, TypeError, QueueError):
            raise QueueError("storage_unavailable") from None

    async def save(self, data):
        # Native onboarding/config flows can run before async_start; not_running
        # is a valid initial setup state. Only shutdown states defer/refuse saves.
        if self.hass.state in {CoreState.stopping, CoreState.final_write, CoreState.stopped}:
            raise QueueError("storage_unavailable")
        try:
            await self.store.async_save(data)
            # HA's Store catches WriteError. Do not acknowledge until the exact
            # revision is independently read and its file and directory are synced.
            await self.hass.async_add_executor_job(self._verify, data)
        except (OSError, ValueError, TypeError, QueueError):
            raise QueueError("storage_unavailable") from None

    def _verify(self, data):
        read_owned(self.store.path, self.key, expected=data)

    async def remove(self):
        await self.store.async_remove()
