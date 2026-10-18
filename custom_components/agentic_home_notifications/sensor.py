"""Native queue, delivery, and channel status entities."""
from homeassistant.components.sensor import SensorEntity

from .entity import NotificationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = entry.runtime_data
    async_add_entities([QueueDepth(runtime), LastDelivery(runtime),
                        *[ChannelLogin(runtime, channel) for channel in ("line", "telegram", "discord")]])


class QueueDepth(NotificationEntity, SensorEntity):
    def __init__(self, runtime):
        super().__init__(runtime, "pending", "Pending notifications")

    @property
    def native_value(self):
        return len(self.runtime.queue.data["queue"])


class LastDelivery(NotificationEntity, SensorEntity):
    def __init__(self, runtime):
        super().__init__(runtime, "last_delivery", "Last delivery")

    @property
    def native_value(self):
        queue = self.runtime.queue
        if not queue.healthy:
            return None
        if queue.data["owner_id"] is None:
            return "none"
        receipt = queue.status(queue.data["owner_id"])["last_delivery"]
        return receipt["state"] if receipt else "none"


class ChannelLogin(NotificationEntity, SensorEntity):
    def __init__(self, runtime, channel):
        super().__init__(runtime, channel + "_login", channel.upper() + " login")
        self.channel = channel

    @property
    def native_value(self):
        if not self.runtime.queue.available:
            return "unknown"
        return next((item["login"] for item in self.runtime.queue.data["channels"]
                     if item["channel"] == self.channel), "unknown")

    @property
    def extra_state_attributes(self):
        if not self.runtime.queue.available:
            return {"transport": "unknown"}
        return {"transport": next((item["transport"] for item in self.runtime.queue.data["channels"]
                                   if item["channel"] == self.channel), "unknown")}
