"""The SolarEdge ONE (v2 API) integration."""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_BASE,
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SITE_ID,
    DOMAIN,
    TOKEN_EXPIRY_MARGIN,
    TOKEN_URL,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR]


class SolarEdgeV2Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch the day's power curve and derive power, energy and peak from it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self.entry = entry
        self.site_id = str(entry.data[CONF_SITE_ID])
        self._session = aiohttp_client.async_get_clientsession(hass)

    async def _async_ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed.

        SolarEdge ROTATES the refresh token on every refresh, so the new one
        must be persisted or the integration dies after a single refresh.
        """
        data = self.entry.data
        expires_at = data.get(CONF_EXPIRES_AT, 0)
        token = data.get(CONF_ACCESS_TOKEN)

        if token and time.time() < expires_at - TOKEN_EXPIRY_MARGIN:
            return token

        basic = base64.b64encode(
            f"{data[CONF_CLIENT_ID]}:{data[CONF_CLIENT_SECRET]}".encode()
        ).decode()
        try:
            async with self._session.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": data[CONF_REFRESH_TOKEN],
                },
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    raise ConfigEntryAuthFailed(
                        f"Token refresh failed ({resp.status}): {body}"
                    )
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Token refresh error: {err}") from err

        new = {
            **data,
            CONF_ACCESS_TOKEN: body["access_token"],
            CONF_EXPIRES_AT: time.time() + body.get("expires_in", 7200),
        }
        if body.get("refresh_token"):
            new[CONF_REFRESH_TOKEN] = body["refresh_token"]
        self.hass.config_entries.async_update_entry(self.entry, data=new)
        _LOGGER.debug("SolarEdge token refreshed; expires in %ss", body.get("expires_in"))
        return body["access_token"]

    async def _async_update_data(self) -> dict[str, Any]:
        token = await self._async_ensure_token()
        url = f"{API_BASE}/sites/{self.site_id}/power"
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as resp:
                if resp.status == 401:
                    raise ConfigEntryAuthFailed("SolarEdge rejected the access token")
                if resp.status == 429:
                    raise UpdateFailed("SolarEdge rate limit / credit budget exceeded")
                if resp.status != 200:
                    raise UpdateFailed(f"SolarEdge returned {resp.status}")
                payload = await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error talking to SolarEdge: {err}") from err

        points = [p for p in payload.get("values", []) if p.get("value") is not None]
        if not points:
            # Legitimate overnight: no production yet today.
            return {"current_power": 0.0, "energy_today": 0.0,
                    "peak_power": 0.0, "peak_time": None, "samples": 0}

        # QUARTER_HOUR resolution -> each sample covers 0.25h.
        energy_wh = sum(p["value"] * 0.25 for p in points)
        peak = max(points, key=lambda p: p["value"])
        return {
            "current_power": float(points[-1]["value"]),
            "energy_today": round(energy_wh / 1000, 3),
            "peak_power": float(peak["value"]),
            "peak_time": peak["timestamp"],
            "samples": len(points),
        }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SolarEdge ONE from a config entry."""
    coordinator = SolarEdgeV2Coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
