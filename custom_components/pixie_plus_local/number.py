"""Number platform for Pixie Plus Local (timer duration, sensor hold time)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import (
    DOMAIN,
    PixieEndpoint,
    PixiePlusConfigEntryRuntimeData,
    PixiePlusCoordinatorEntity,
    endpoint_unique_identifier,
    gateway_device_identifier,
    physical_device_identifier,
)


def _iter_timer_duration_endpoints(inventory) -> list[PixieEndpoint]:
    """Return timer duration number endpoints for timer-capable devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_timer:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="timer_duration",
                command_target="timer_duration",
                entity_unique_id=endpoint_unique_identifier(record, "timer_duration"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Timer duration",
            )
        )
    return endpoints


def _iter_hold_time_endpoints(inventory) -> list[PixieEndpoint]:
    """Return hold time number endpoints for sensor devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_hold_time:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="hold_time",
                command_target="hold_time",
                entity_unique_id=endpoint_unique_identifier(record, "hold_time"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Hold time",
            )
        )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local number entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    entities: list = []
    for endpoint in _iter_timer_duration_endpoints(inventory):
        entities.append(PixiePlusTimerDurationNumberEntity(runtime_data, endpoint))
    for endpoint in _iter_hold_time_endpoints(inventory):
        entities.append(PixiePlusHoldTimeNumberEntity(runtime_data, endpoint))
    async_add_entities(entities)


class PixiePlusTimerDurationNumberEntity(PixiePlusCoordinatorEntity, NumberEntity):
    """Number input for setting the timer countdown duration (seconds)."""

    _attr_native_min_value = 1
    _attr_native_max_value = 86400
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def native_value(self) -> int | None:
        """Return the currently configured timer duration (total seconds from last status update)."""
        total = self.record.runtime.timer_total_seconds
        if isinstance(total, int):
            return total
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the timer duration on the device (in seconds)."""
        duration_seconds = max(1, min(86400, round(value)))
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_timer_action="set_duration",
                command_timer_duration=duration_seconds,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err


class PixiePlusHoldTimeNumberEntity(PixiePlusCoordinatorEntity, NumberEntity):
    """Number input for the sensor hold time (0–1799 seconds)."""

    _attr_native_min_value = 0
    _attr_native_max_value = 1799
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def native_value(self) -> int | None:
        hold = self.record.runtime.hold_time_seconds
        if isinstance(hold, int):
            return hold
        return None

    async def async_set_native_value(self, value: float) -> None:
        hold_seconds = max(0, min(1799, round(value)))
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_sensor_param="hold_time",
                command_sensor_param_value=hold_seconds,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
