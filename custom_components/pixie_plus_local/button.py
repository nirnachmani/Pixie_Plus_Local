"""Button platform for Pixie Plus Local (timer restart, sensor refresh)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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


def _iter_timer_button_endpoints(inventory) -> list[PixieEndpoint]:
    """Return restart button endpoints for timer-capable devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_timer:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="restart",
                command_target="timer_restart",
                entity_unique_id=endpoint_unique_identifier(record, "restart"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Restart",
            )
        )
    return endpoints


def _iter_sensor_refresh_endpoints(inventory) -> list[PixieEndpoint]:
    """Return refresh button endpoints for sensor devices with configurable params."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not (
            record.capabilities.supports_hold_time
            or record.capabilities.supports_brightness_threshold
            or record.capabilities.supports_motion_sensitivity
        ):
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="refresh_params",
                command_target="sensor_poll",
                entity_unique_id=endpoint_unique_identifier(record, "refresh_params"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Refresh settings",
            )
        )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local button entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    entities: list = []
    for endpoint in _iter_timer_button_endpoints(inventory):
        entities.append(PixiePlusTimerRestartButtonEntity(runtime_data, endpoint))
    for endpoint in _iter_sensor_refresh_endpoints(inventory):
        entities.append(PixiePlusSensorRefreshButtonEntity(runtime_data, endpoint))
    async_add_entities(entities)


class PixiePlusTimerRestartButtonEntity(PixiePlusCoordinatorEntity, ButtonEntity):
    """Restart button for timer switch countdown."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def available(self) -> bool:
        """Restart button is only available when timer mode is active and light is on."""
        if not super().available:
            return False
        runtime = self.record.runtime
        return runtime.mode == 1 and runtime.is_on is True

    async def async_press(self) -> None:
        """Press the restart button to reset the timer countdown."""
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_timer_action="restart",
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err


class PixiePlusSensorRefreshButtonEntity(PixiePlusCoordinatorEntity, ButtonEntity):
    """Refresh button for sensor device settings (hold time, brightness, sensitivity)."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    async def async_press(self) -> None:
        """Press the button to query current sensor parameters from the device."""
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_timer_action="poll",
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
