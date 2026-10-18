"""Controller heartbeat, independently from channel login and delivery."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import NotificationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ControllerConnection(entry.runtime_data)])


class ControllerConnection(NotificationEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, runtime):
        super().__init__(runtime, "controller_connection", "Controller connection")

    @property
    def is_on(self):
        return self.runtime.queue.available
