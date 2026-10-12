"""The SolarEdge ONE (v2 API) integration."""
from __future__ import annotations

import base64
import logging
import time
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.sun import is_up
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    API_BASE,
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
    OVERVIEW_DAY_MINUTES,
    OVERVIEW_NIGHT_MINUTES,
    TOKEN_EXPIRY_MARGIN,
    TOKEN_URL,
)

EMPTY_DAY: dict[str, Any] = {
    "current_power": 0.0,
    "energy_today": 0.0,
    "peak_power": 0.0,
    "peak_time": None,
    "samples": 0,
}

_LOGGER = logging.getLogger(__name__)

UNIT_TO_KWH = {"WH": 0.001, "KWH": 1.0, "MWH": 1000.0}


def _kwh(section: dict[str, Any], key: str) -> float | None:
    """Return one Site Overview total in kWh, or None if it is missing."""
    value = section.get(key)
    if value is None:
        return None
    unit = str(section.get("unit") or section.get("units") or "WH").upper()
    return round(value * UNIT_TO_KWH.get(unit, 0.001), 3)


PLATFORMS: list[Platform] = [Platform.SENSOR]


class SolarEdgeV2Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch the day's power curve and derive power, energy and peak from it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.entry = entry
        self.site_id = str(entry.data[CONF_SITE_ID])
        self._session = aiohttp_client.async_get_clientsession(hass)
        self._last_fetch_date = None
        self._overview: dict[str, Any] = {}
        self._overview_at = 0.0
        self._overview_date = None
        self.apply_options()

    def apply_options(self) -> None:
        """Pick up the polling settings from the entry's options."""
        opts = self.entry.options
        self.update_interval = timedelta(
            minutes=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        self.daylight_only = opts.get(CONF_DAYLIGHT_ONLY, DEFAULT_DAYLIGHT_ONLY)

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

    def force_overview(self) -> None:
        """Make the next update fetch Site Overview regardless of schedule."""
        self._overview_at = 0.0

    async def _async_update_data(self) -> dict[str, Any]:
        today = dt_util.now().date()
        if self.daylight_only and self.data is not None and not is_up(self.hass):
            # Save API credits overnight. Keep today's totals, but start a
            # fresh day at midnight so energy_today does not carry over.
            if self._last_fetch_date == today:
                day = {**self.data, "current_power": 0.0}
            else:
                day = dict(EMPTY_DAY)
        else:
            day = await self._async_fetch_power(today)
        return {**day, **await self._async_overview(today)}

    async def _async_overview(self, today) -> dict[str, Any]:
        """Return today's site meter totals, fetching Site Overview when due.

        Errors other than an auth failure keep the last totals so one bad
        call does not blank the power sensors too.
        """
        minutes = OVERVIEW_DAY_MINUTES if is_up(self.hass) else OVERVIEW_NIGHT_MINUTES
        if (
            self._overview_date == today
            and time.time() - self._overview_at < minutes * 60 - 30
        ):
            return self._overview

        token = await self._async_ensure_token()
        url = f"{API_BASE}/sites/{self.site_id}/overview"
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as resp:
                if resp.status == 401:
                    raise ConfigEntryAuthFailed("SolarEdge rejected the access token")
                if resp.status != 200:
                    _LOGGER.warning(
                        "SolarEdge Site Overview returned %s; keeping the last totals",
                        resp.status,
                    )
                    return self._overview
                payload = await resp.json()
        except aiohttp.ClientError as err:
            _LOGGER.warning("Error fetching SolarEdge Site Overview: %s", err)
            return self._overview

        production = payload.get("production") or {}
        consumption = payload.get("consumption") or {}
        self._overview = {
            "site_production": _kwh(production, "total"),
            "site_consumption": _kwh(consumption, "total"),
            "site_from_grid": _kwh(consumption, "fromGrid"),
            "site_to_grid": _kwh(production, "toGrid"),
        }
        self._overview_at = time.time()
        self._overview_date = today
        return self._overview

    async def _async_fetch_power(self, today) -> dict[str, Any]:
        """Fetch the day's power curve and derive power, energy and peak."""
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

        self._last_fetch_date = today
        points = [p for p in payload.get("values", []) if p.get("value") is not None]
        if not points:
            # Legitimate overnight: no production yet today.
            return dict(EMPTY_DAY)

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
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    @callback
    def _async_day_end(_now) -> None:
        """Fetch the day's last Site Overview totals before midnight."""
        coordinator.force_overview()
        hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(
        async_track_time_change(hass, _async_day_end, hour=23, minute=57, second=0)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options without a reload.

    This listener also fires whenever a token refresh rewrites entry.data;
    reloading then would cost an API call each time, so just re-read options.
    """
    coordinator: SolarEdgeV2Coordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.update_interval != timedelta(
        minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    ) or coordinator.daylight_only != entry.options.get(
        CONF_DAYLIGHT_ONLY, DEFAULT_DAYLIGHT_ONLY
    ):
        coordinator.apply_options()
        await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
