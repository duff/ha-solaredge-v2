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
    SITE_DAY_MINUTES,
    SITE_NIGHT_MINUTES,
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
UNIT_TO_W = {"W": 1.0, "KW": 1000.0, "MW": 1_000_000.0}


def _kwh(section: dict[str, Any], key: str) -> float | None:
    """Return one Site Overview total in kWh, or None if it is missing."""
    value = section.get(key)
    if value is None:
        return None
    unit = str(section.get("unit") or section.get("units") or "WH").upper()
    return round(value * UNIT_TO_KWH.get(unit, 0.001), 3)


def _sum_metric(
    meters: dict[str, Any], name: str, scale: dict[str, float], default: str
) -> list[tuple[str, float]] | None:
    """Sum one meter metric across all meters, per timestamp, oldest first.

    Returns None when no meter reports that metric at all.
    """
    totals: dict[str, float] = {}
    found = False
    for metrics in meters.values():
        series = metrics.get(name) if isinstance(metrics, dict) else None
        if not isinstance(series, dict):
            continue
        found = True
        factor = scale.get(str(series.get("unit") or default).upper(), 1.0)
        for point in series.get("values") or []:
            value, stamp = point.get("value"), point.get("timestamp")
            if value is not None and stamp is not None:
                totals[stamp] = totals.get(stamp, 0.0) + value * factor
    return sorted(totals.items()) if found else None


def _day_from_points(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive current power, energy today and peak from 15-minute power."""
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


def _site_from_meters(
    meters: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]] | None] | None:
    """Turn meter telemetry into site totals, latest power and the production curve.

    Returns None when no meter reports import or export, so the caller can
    fall back to Site Overview.
    """
    energy = {
        key: _sum_metric(meters, f"{key}Energy", UNIT_TO_KWH, "WH")
        for key in ("production", "consumption", "import", "export")
    }
    power = {
        key: _sum_metric(meters, f"{key}Power", UNIT_TO_W, "W")
        for key in ("production", "consumption", "import", "export")
    }
    if energy["import"] is None and energy["export"] is None:
        return None

    def total(key: str) -> float | None:
        series = energy[key]
        return round(sum(v for _, v in series), 3) if series is not None else None

    def latest(key: str) -> float | None:
        series = power[key]
        return round(series[-1][1], 1) if series else (0.0 if series is not None else None)

    site = {
        "site_production": total("production"),
        "site_consumption": total("consumption"),
        "site_from_grid": total("import"),
        "site_to_grid": total("export"),
        "site_production_power": latest("production"),
        "site_consumption_power": latest("consumption"),
        "grid_import_power": latest("import"),
        "grid_export_power": latest("export"),
        "site_power_as_of": max(
            (s[-1][0] for s in power.values() if s), default=None
        ),
    }
    # No consumption meter: consumption = production + import - export.
    parts = ("site_production", "site_from_grid", "site_to_grid")
    if site["site_consumption"] is None and all(site[k] is not None for k in parts):
        site["site_consumption"] = round(
            site["site_production"] + site["site_from_grid"] - site["site_to_grid"], 3
        )
    parts = ("site_production_power", "grid_import_power", "grid_export_power")
    if site["site_consumption_power"] is None and all(site[k] is not None for k in parts):
        site["site_consumption_power"] = round(
            site["site_production_power"]
            + site["grid_import_power"]
            - site["grid_export_power"],
            1,
        )
    curve = (
        [{"timestamp": t, "value": v} for t, v in power["production"]]
        if power["production"] is not None
        else None
    )
    return site, curve


PLATFORMS: list[Platform] = [Platform.SENSOR]


class SolarEdgeV2Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch the day's power curve and site meters; derive power, energy and peak."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.entry = entry
        self.site_id = str(entry.data[CONF_SITE_ID])
        self._session = aiohttp_client.async_get_clientsession(hass)
        self._last_fetch_date = None
        self._site: dict[str, Any] = {}
        self._site_at = 0.0
        self._site_date = None
        self._production_curve: list[dict[str, Any]] | None = None
        self._logged_meters = False
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
                # Only a rejected grant needs a new refresh token. A server
                # error or rate limit is retried on the next update; asking
                # for reauth then would stop polling for nothing.
                if resp.status in (400, 401):
                    raise ConfigEntryAuthFailed(
                        f"SolarEdge rejected the refresh token ({resp.status})"
                    )
                if resp.status != 200:
                    raise UpdateFailed(
                        f"Token refresh failed ({resp.status}); will retry"
                    )
                body = await resp.json()
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

    def force_site(self) -> None:
        """Make the next update fetch the site meters regardless of schedule."""
        self._site_at = 0.0

    async def _async_update_data(self) -> dict[str, Any]:
        today = dt_util.now().date()
        site = await self._async_site(today)
        if self._production_curve is not None and self._site_date == today:
            # The meters report production: no separate /power call needed.
            day = _day_from_points(self._production_curve)
            self._last_fetch_date = today
        elif self.daylight_only and self.data is not None and not is_up(self.hass):
            # Save API credits overnight. Keep today's totals, but start a
            # fresh day at midnight so energy_today does not carry over.
            if self._last_fetch_date == today:
                day = {**self.data, "current_power": 0.0}
            else:
                day = dict(EMPTY_DAY)
        else:
            day = await self._async_fetch_power(today)
        return {**day, **site}

    async def _async_site(self, today) -> dict[str, Any]:
        """Return today's site meter data, fetching it when due.

        Meter telemetry gives totals and 15-minute power. Without import or
        export meters it falls back to Site Overview totals. Errors other
        than an auth failure keep the last values so one bad call does not
        blank the other sensors too.
        """
        minutes = SITE_DAY_MINUTES if is_up(self.hass) else SITE_NIGHT_MINUTES
        if (
            self._site_date == today
            and time.time() - self._site_at < minutes * 60 - 30
        ):
            return self._site

        now = dt_util.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        meters = await self._async_get(
            "meters/telemetry",
            {
                "resolution": "QUARTER_HOUR",
                "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "to": now.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )
        if meters is None:
            return self._site
        parsed = _site_from_meters(meters.get("meters") or {})
        if parsed is None:
            if not self._logged_meters:
                _LOGGER.info("SolarEdge meters report no import or export; using Site Overview")
                self._logged_meters = True
            overview = await self._async_get("overview")
            if overview is None:
                return self._site
            production = overview.get("production") or {}
            consumption = overview.get("consumption") or {}
            site = {
                "site_production": _kwh(production, "total"),
                "site_consumption": _kwh(consumption, "total"),
                "site_from_grid": _kwh(consumption, "fromGrid"),
                "site_to_grid": _kwh(production, "toGrid"),
            }
            curve = None
        else:
            site, curve = parsed
            if not self._logged_meters:
                _LOGGER.info(
                    "SolarEdge meters: %s",
                    {
                        str(sn): sorted(m) if isinstance(m, dict) else None
                        for sn, m in (meters.get("meters") or {}).items()
                    },
                )
                self._logged_meters = True
        self._site = site
        self._production_curve = curve
        self._site_at = time.time()
        self._site_date = today
        return self._site

    async def _async_get(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        """GET one site endpoint. None (logged) on any failure but auth."""
        token = await self._async_ensure_token()
        url = f"{API_BASE}/sites/{self.site_id}/{path}"
        try:
            async with self._session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as resp:
                if resp.status == 401:
                    raise ConfigEntryAuthFailed("SolarEdge rejected the access token")
                if resp.status != 200:
                    _LOGGER.warning(
                        "SolarEdge %s returned %s; keeping the last values",
                        path,
                        resp.status,
                    )
                    return None
                body = await resp.json()
        except aiohttp.ClientError as err:
            _LOGGER.warning("Error fetching SolarEdge %s: %s", path, err)
            return None
        return body if isinstance(body, dict) else None

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
        return _day_from_points(points)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SolarEdge ONE from a config entry."""
    coordinator = SolarEdgeV2Coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    @callback
    def _async_day_end(_now) -> None:
        """Fetch the day's last site meter totals before midnight."""
        coordinator.force_site()
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
