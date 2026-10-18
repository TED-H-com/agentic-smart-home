"""Explicit local enrollment; channel credentials remain in the outer Controller."""
import uuid

import probatio
from homeassistant import config_entries

from .const import DOMAIN, NAME


class NotificationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={
                "source_id": uuid.uuid4().hex, "epoch": uuid.uuid4().hex, "initialized": False})
        return self.async_show_form(step_id="user", data_schema=probatio.Schema({}))
