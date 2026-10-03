"""Sensors for SolarEdge ONE (v2 API)."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SolarEdgeV2Coordinator
from .const import CONF_SITE_ID, DOMAIN


@dataclass(frozen=True, kw_only=True)
class SolarEdgeSensorDescription(SensorEntityDescription):
    """Describes a SolarEdge sensor."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSORS: tuple[SolarEdgeSensorDescription, ...] = (
    SolarEdgeSensorDescription(
        key="current_power",
        translation_key="current_power",
        name="Current power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:solar-power",
        value_fn=lambda d: d.get("current_power"),
    ),
    SolarEdgeSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        name="Energy today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # Resets at midnight, so TOTAL_INCREASING is correct for the
        # Energy dashboard.
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:solar-power-variant",
        value_fn=lambda d: d.get("energy_today"),
    ),
    SolarEdgeSensorDescription(
        key="peak_power_today",
        translation_key="peak_power_today",
        name="Peak power today",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chart-bell-curve",
        value_fn=lambda d: d.get("peak_power"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the SolarEdge sensors."""
    coordinator: SolarEdgeV2Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SolarEdgeV2Sensor(coordinator, entry, desc) for desc in SENSORS
    )


class SolarEdgeV2Sensor(CoordinatorEntity[SolarEdgeV2Coordinator], SensorEntity):
    """A SolarEdge ONE sensor."""

    _attr_has_entity_name = True
    entity_description: SolarEdgeSensorDescription

    def __init__(
        self,
        coordinator: SolarEdgeV2Coordinator,
        entry: ConfigEntry,
        description: SolarEdgeSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        site = str(entry.data[CONF_SITE_ID])
        self._attr_unique_id = f"{site}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site)},
            name=f"SolarEdge site {site}",
            manufacturer="SolarEdge",
            model="ONE v2 API",
            configuration_url="https://monitoring.solaredge.com/",
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose provenance so the reading is not mistaken for live data."""
        data = self.coordinator.data or {}
        if self.entity_description.key == "peak_power_today":
            return {"peak_time": data.get("peak_time")}
        if self.entity_description.key == "current_power":
            return {
                "resolution": "15 minutes",
                "samples_today": data.get("samples"),
                "note": "Latest 15-minute sample, not live wattage",
            }
        return None
