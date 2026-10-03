"""Config flow for SolarEdge ONE (v2 API).

HA's built-in OAuth2 flow cannot be used: SolarEdge does not return the
`state` parameter on the authorization callback, so HA can never match the
callback to the originating flow. Instead the tokens are obtained once out of
band and the refresh token is stored here.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_DAYLIGHT_ONLY,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SITE_ID,
    DEFAULT_DAYLIGHT_ONLY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SITE_ID): str,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): SECRET,
        vol.Required(CONF_REFRESH_TOKEN): SECRET,
    }
)

# A new refresh token makes any cached access token stale.
CLEAR_TOKENS = {CONF_ACCESS_TOKEN: None, CONF_EXPIRES_AT: 0}


class SolarEdgeV2ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SolarEdge ONE."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SolarEdgeV2OptionsFlow()

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

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when SolarEdge rejects the stored tokens."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh refresh token."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_REFRESH_TOKEN): SECRET}),
            )
        return self.async_update_reload_and_abort(
            self._get_reauth_entry(), data_updates={**user_input, **CLEAR_TOKENS}
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the OAuth credentials without removing the integration."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required(
                        CONF_CLIENT_ID, default=entry.data[CONF_CLIENT_ID]
                    ): str,
                    vol.Required(CONF_CLIENT_SECRET): SECRET,
                    vol.Required(CONF_REFRESH_TOKEN): SECRET,
                }
            )
            return self.async_show_form(step_id="reconfigure", data_schema=schema)
        return self.async_update_reload_and_abort(
            entry, data_updates={**user_input, **CLEAR_TOKENS}
        )


class SolarEdgeV2OptionsFlow(OptionsFlow):
    """Polling settings, editable after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            user_input[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_DAYLIGHT_ONLY,
                    default=opts.get(CONF_DAYLIGHT_ONLY, DEFAULT_DAYLIGHT_ONLY),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
