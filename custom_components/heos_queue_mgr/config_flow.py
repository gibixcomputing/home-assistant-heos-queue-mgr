from __future__ import annotations

from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN


class HeosQueueMgrConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow."""

    VERSION = 1

    async def async_step_user(self, user_input: ConfigType | None = None) -> FlowResult:
        del user_input

        await self.async_set_unique_id("heos_queue_mgr")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title="HEOS Queue Manager", data={})
