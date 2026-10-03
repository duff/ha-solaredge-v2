"""Config flow for SolarEdge ONE (v2 API).

HA's built-in OAuth2 flow cannot be used: SolarEdge does not return the
`state` parameter on the authorization callback, so HA can never match the
callback to the originating flow. Instead the tokens are obtained once out of
band and the refresh token is stored here.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_SITE_ID,
    DOMAIN,
)

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SITE_ID): str,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Required(CONF_REFRESH_TOKEN): str,
    }
)


class SolarEdgeV2ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SolarEdge ONE."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect site id and credentials."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=SCHEMA)

        await self.async_set_unique_id(str(user_input[CONF_SITE_ID]))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"SolarEdge site {user_input[CONF_SITE_ID]}", data=user_input
        )
