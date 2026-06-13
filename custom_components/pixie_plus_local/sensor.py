"""Sensor platform for Pixie Plus Local (timer remaining)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import (
    DOMAIN,
    PixieEndpoint,
    PixiePlusConfigEntryRuntimeData,
    PixiePlusCoordinatorEntity,
    endpoint_unique_identifier,
    gateway_device_identifier,
    physical_device_identifier,
)


def _iter_timer_sensor_endpoints(inventory) -> list[PixieEndpoint]:
    """Return timer remaining sensor endpoints for timer-capable devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_timer:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="timer_remaining",
                command_target="timer_poll",
                entity_unique_id=endpoint_unique_identifier(record, "timer_remaining"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Timer",
            )
        )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local sensor entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    async_add_entities(
        PixiePlusTimerRemainingSensorEntity(runtime_data, endpoint)
        for endpoint in _iter_timer_sensor_endpoints(inventory)
    )


class PixiePlusTimerRemainingSensorEntity(PixiePlusCoordinatorEntity, SensorEntity):
    """Sensor showing remaining time on an active timer.

    Uses local estimation between polls: the base value comes from the last
    d36969 response, then wall-clock elapsed time is subtracted every second
    to give a smooth countdown. The coordinator polls every 10s to correct drift.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        self._tick_remove = None

    @property
    def available(self) -> bool:
        """Sensor is only available when timer mode is active and light is on."""
        if not super().available:
            return False
        runtime = self.record.runtime
        return runtime.mode == 1 and runtime.is_on is True

    @property
    def native_value(self) -> float | None:
        """Return the estimated remaining timer seconds.

        Computed from the last authoritative poll value minus wall-clock
        elapsed time since that poll. Never returns below 0.
        """
        runtime = self.record.runtime
        remaining = runtime.timer_remaining_seconds
        if remaining is None:
            return None

        last_poll = runtime.last_timer_poll_at
        if last_poll is None:
            return float(remaining)

        import time as _time
        elapsed_seconds = _time.time() - last_poll
        estimated = max(0.0, float(remaining) - elapsed_seconds)
        return round(estimated, 1)

    async def async_added_to_hass(self) -> None:
        """Start 1-second refresh ticks when entity is added."""
        await super().async_added_to_hass()
        self._start_ticking()

    async def async_will_remove_from_hass(self) -> None:
        """Stop refresh ticks when entity is removed."""
        self._stop_ticking()
        await super().async_will_remove_from_hass()

    @callback
    def _start_ticking(self) -> None:
        """Schedule a 1-second callback to refresh the sensor value."""
        if self._tick_remove is not None:
            return
        self._tick_remove = async_track_time_interval(
            self.hass,
            self._tick,
            timedelta(seconds=1),
        )

    @callback
    def _stop_ticking(self) -> None:
        """Remove the 1-second callback."""
        if self._tick_remove is not None:
            self._tick_remove()
            self._tick_remove = None

    @callback
    def _tick(self, _now=None) -> None:
        """Refresh the entity state from the estimation formula."""
        self.async_write_ha_state()
