"""Select platform for Pixie Plus Local."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
from .pixie_value_profiles import (
    get_sensor_select_options,
    get_timer_select_options,
    sensor_mode_value_to_option,
    sensor_option_to_mode_value,
    timer_mode_value_to_option,
    timer_option_to_mode_value,
)


def _iter_mode_select_endpoints(inventory) -> list[PixieEndpoint]:
    """Return mode select endpoints from sensor controller and timer devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        parent_identifier = physical_device_identifier(record)

        if record.capabilities.supports_sensor:
            endpoints.append(
                PixieEndpoint(
                    device_id=record.id,
                    endpoint_key="mode",
                    command_target="mode",
                    entity_unique_id=endpoint_unique_identifier(record, "mode"),
                    device_identifier=parent_identifier,
                    device_name=record.name,
                    via_device_identifier=gateway_identifier,
                    entity_name="Mode",
                )
            )
        elif record.capabilities.supports_timer:
            endpoints.append(
                PixieEndpoint(
                    device_id=record.id,
                    endpoint_key="timer_mode",
                    command_target="mode",
                    entity_unique_id=endpoint_unique_identifier(record, "timer_mode"),
                    device_identifier=parent_identifier,
                    device_name=record.name,
                    via_device_identifier=gateway_identifier,
                    entity_name="Mode",
                )
            )

    return endpoints


def _iter_brightness_threshold_endpoints(inventory) -> list[PixieEndpoint]:
    """Return brightness threshold select endpoints for sensor devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_brightness_threshold:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="brightness_threshold",
                command_target="brightness_threshold",
                entity_unique_id=endpoint_unique_identifier(record, "brightness_threshold"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Brightness threshold",
            )
        )
    return endpoints


def _iter_motion_sensitivity_endpoints(inventory) -> list[PixieEndpoint]:
    """Return motion sensitivity select endpoints for sensor devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_motion_sensitivity:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="motion_sensitivity",
                command_target="motion_sensitivity",
                entity_unique_id=endpoint_unique_identifier(record, "motion_sensitivity"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Sensitivity",
            )
        )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local select entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    entities: list = []
    for endpoint in _iter_mode_select_endpoints(inventory):
        entities.append(PixiePlusModeSelectEntity(runtime_data, endpoint))
    for endpoint in _iter_brightness_threshold_endpoints(inventory):
        entities.append(PixiePlusSensorParamSelectEntity(runtime_data, endpoint))
    for endpoint in _iter_motion_sensitivity_endpoints(inventory):
        entities.append(PixiePlusSensorParamSelectEntity(runtime_data, endpoint))
    async_add_entities(entities)


class PixiePlusModeSelectEntity(PixiePlusCoordinatorEntity, SelectEntity):
    """Representation of a Pixie Plus mode select entity (sensor or timer)."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        if self.record.capabilities.supports_timer:
            self._attr_options = get_timer_select_options(self.record.model_no)
        else:
            self._attr_options = get_sensor_select_options(self.record.model_no)

    @property
    def current_option(self) -> str | None:
        runtime = self.record.runtime
        if isinstance(runtime.mode, int):
            if self.record.capabilities.supports_timer:
                return timer_mode_value_to_option(runtime.mode)
            return sensor_mode_value_to_option(self.record.model_no, runtime.mode)
        return None

    async def async_select_option(self, option: str) -> None:
        """Change mode to the selected option."""
        if option not in self._attr_options:
            raise HomeAssistantError(f"Unsupported mode option: {option}")

        if self.record.capabilities.supports_timer:
            mode_value = timer_option_to_mode_value(option)
            if mode_value is None:
                raise HomeAssistantError(f"Unsupported timer mode option: {option}")
            try:
                await self.runtime_data.async_send_local_command(
                    self.hass,
                    command_device_id=self.record.id,
                    command_mode=mode_value,
                )
            except Exception as err:
                raise HomeAssistantError(str(err)) from err
        else:
            mode_value = sensor_option_to_mode_value(self.record.model_no, option)
            if mode_value is None:
                raise HomeAssistantError(f"Unsupported mode option: {option}")
            try:
                await self.runtime_data.async_send_local_command(
                    self.hass,
                    command_device_id=self.record.id,
                    command_mode=mode_value,
                )
            except Exception as err:
                raise HomeAssistantError(str(err)) from err


class PixiePlusSensorParamSelectEntity(PixiePlusCoordinatorEntity, SelectEntity):
    """Select entity for sensor parameters (brightness threshold, sensitivity)."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        caps = self.record.capabilities
        if endpoint.endpoint_key == "brightness_threshold":
            self._attr_options = list(caps.brightness_threshold_options)
        elif endpoint.endpoint_key == "motion_sensitivity":
            self._attr_options = list(caps.motion_sensitivity_options)

    @property
    def current_option(self) -> str | None:
        runtime = self.record.runtime
        if self.endpoint.endpoint_key == "brightness_threshold":
            val = runtime.brightness_threshold
        elif self.endpoint.endpoint_key == "motion_sensitivity":
            val = runtime.motion_sensitivity
        else:
            return None
        if isinstance(val, int) and 0 <= val < len(self._attr_options):
            return self._attr_options[val]
        return None

    async def async_select_option(self, option: str) -> None:
        """Change the parameter to the selected option."""
        if option not in self._attr_options:
            raise HomeAssistantError(f"Unsupported option: {option}")
        value = self._attr_options.index(option)
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_sensor_param=self.endpoint.command_target,
                command_sensor_param_value=value,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
