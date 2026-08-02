"""Button platform for Pixie Plus Local config/runtime refresh actions."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .pixie_const import (
    DOMAIN,
)
from .pixie_ha import (
    PixieEndpoint,
    PixiePlusConfigEntryRuntimeData,
    PixiePlusCoordinatorEntity,
    device_added_signal,
    endpoint_unique_identifier,
    parent_device_identifier,
    physical_device_identifier,
)
from .pixie_inventory import supports_plug_led_settings, supports_sensor_advanced_settings


def _iter_timer_button_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return restart button endpoints for timer-capable devices."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
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


def _iter_sensor_refresh_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return refresh button endpoints for sensor devices with configurable params."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
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


def _iter_sensor_learn_threshold_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return learn-threshold button endpoints for supported sensors."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        if not supports_sensor_advanced_settings(record.capabilities):
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="learn_brightness_threshold",
                command_target="learn_brightness_threshold",
                entity_unique_id=endpoint_unique_identifier(record, "learn_brightness_threshold"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Learn brightness threshold",
            )
        )
    return endpoints


def _iter_gate_refresh_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return refresh button endpoints for gate settings."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        if not record.capabilities.supports_gate:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="gate_refresh_settings",
                command_target="gate_settings_refresh",
                entity_unique_id=endpoint_unique_identifier(record, "gate_refresh_settings"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Refresh settings",
            )
        )
    return endpoints


def _iter_indicator_led_refresh_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return refresh button endpoints for switch indicator LED settings."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        if not record.capabilities.supports_switch_indicator_led:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="indicator_led_refresh_settings",
                command_target="indicator_led_settings_refresh",
                entity_unique_id=endpoint_unique_identifier(record, "indicator_led_refresh_settings"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Refresh LED settings",
            )
        )
    return endpoints


def _iter_plug_led_refresh_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return refresh button endpoints for ESS105/BT LED settings."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        if not supports_plug_led_settings(record.capabilities):
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="plug_led_refresh_settings",
                command_target="plug_led_settings_refresh",
                entity_unique_id=endpoint_unique_identifier(record, "plug_led_refresh_settings"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Refresh LED settings",
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
    for endpoint in _iter_sensor_learn_threshold_endpoints(inventory):
        entities.append(PixiePlusSensorLearnThresholdButtonEntity(runtime_data, endpoint))
    for endpoint in _iter_gate_refresh_endpoints(inventory):
        entities.append(PixiePlusGateRefreshButtonEntity(runtime_data, endpoint))
    for endpoint in _iter_indicator_led_refresh_endpoints(inventory):
        entities.append(PixiePlusIndicatorLedRefreshButtonEntity(runtime_data, endpoint))
    for endpoint in _iter_plug_led_refresh_endpoints(inventory):
        entities.append(PixiePlusPlugLedRefreshButtonEntity(runtime_data, endpoint))
    async_add_entities(entities)

    @callback
    def _async_add_device_entities(device_id: int) -> None:
        current_inventory = runtime_data.pixie_runtime.inventory
        if current_inventory is None:
            return
        entities_to_add: list = []
        for endpoint in _iter_timer_button_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusTimerRestartButtonEntity(runtime_data, endpoint))
        for endpoint in _iter_sensor_refresh_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusSensorRefreshButtonEntity(runtime_data, endpoint))
        for endpoint in _iter_sensor_learn_threshold_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusSensorLearnThresholdButtonEntity(runtime_data, endpoint))
        for endpoint in _iter_gate_refresh_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusGateRefreshButtonEntity(runtime_data, endpoint))
        for endpoint in _iter_indicator_led_refresh_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusIndicatorLedRefreshButtonEntity(runtime_data, endpoint))
        for endpoint in _iter_plug_led_refresh_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusPlugLedRefreshButtonEntity(runtime_data, endpoint))
        if entities_to_add:
            async_add_entities(entities_to_add)

    entry.async_on_unload(async_dispatcher_connect(hass, device_added_signal(entry), _async_add_device_entities))


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

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    async def async_press(self) -> None:
        """Press the button to query current sensor parameters from the device."""
        try:
            await self.runtime_data.async_refresh_config_for_device(
                self.record.id,
                "sensor_settings",
                reason="manual",
            )
            if supports_sensor_advanced_settings(self.record.capabilities):
                await self.runtime_data.async_refresh_config_for_device(
                    self.record.id,
                    "sensor_advanced_settings",
                    reason="manual",
                )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err


class PixiePlusSensorLearnThresholdButtonEntity(PixiePlusCoordinatorEntity, ButtonEntity):
    """Learn the current light level as the sensor brightness threshold."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def available(self) -> bool:
        return super().available

    async def async_press(self) -> None:
        """Press the button to learn the current light level."""
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_sensor_param="brightness_threshold",
                command_sensor_param_value=5,
            )
            await asyncio.sleep(1.0)
            await self.runtime_data.async_refresh_config_for_device(
                self.record.id,
                "sensor_settings",
                reason="learn_brightness_threshold",
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err


class PixiePlusGateRefreshButtonEntity(PixiePlusCoordinatorEntity, ButtonEntity):
    """Refresh button for gate settings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    async def async_press(self) -> None:
        """Press the button to query current gate settings from the device."""
        try:
            await self.runtime_data.async_refresh_config_for_device(
                self.record.id,
                "gate_settings",
                reason="manual",
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err


class PixiePlusIndicatorLedRefreshButtonEntity(PixiePlusCoordinatorEntity, ButtonEntity):
    """Refresh button for switch indicator LED settings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    async def async_press(self) -> None:
        """Press the button to query current indicator LED settings from the device."""
        try:
            await self.runtime_data.async_refresh_config_for_device(
                self.record.id,
                "indicator_led_settings",
                reason="manual",
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err


class PixiePlusPlugLedRefreshButtonEntity(PixiePlusCoordinatorEntity, ButtonEntity):
    """Refresh button for ESS105/BT socket and USB LED settings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    async def async_press(self) -> None:
        """Press the button to query current plug LED settings from the device."""
        try:
            await self.runtime_data.async_refresh_config_for_device(
                self.record.id,
                "plug_led_settings",
                reason="manual",
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
