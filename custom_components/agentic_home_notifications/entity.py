"""Small status projections, never message contents or destination IDs."""
from homeassistant.helpers.entity import Entity, EntityCategory

from .const import DOMAIN, NAME


class NotificationEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime, key, name):
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = {"identifiers": {(DOMAIN, runtime.entry.entry_id)}, "name": NAME,
                                  "manufacturer": "Agentic Home", "model": "Native notification queue"}

    @property
    def available(self):
        return self.runtime.queue.healthy

    async def async_added_to_hass(self):
        self.runtime.listeners.add(self.async_write_ha_state)
        self.async_on_remove(lambda: self.runtime.listeners.discard(self.async_write_ha_state))
