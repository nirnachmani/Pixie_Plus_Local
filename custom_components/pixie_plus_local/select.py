"""Select platform for Pixie Plus Local."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
from .pixie_value_profiles import (
    get_indicator_led_options,
    get_sensor_select_options_for_capabilities,
    get_timer_select_options_for_capabilities,
    indicator_led_option_to_value,
    indicator_led_value_to_option,
    sensor_mode_value_to_option_for_capabilities,
    sensor_option_to_mode_value_for_capabilities,
    timer_mode_value_to_option,
    timer_option_to_mode_value,
)


def _learned_threshold_raw_to_index(raw: int | None) -> int | None:
    """Collapse learned 3002 brightness-threshold raw values to the app's five categories."""
    if not isinstance(raw, int):
        return None
    if raw < 800:
        return 0
    if raw < 1600:
        return 1
    if raw < 2400:
        return 2
    if raw < 3200:
        return 3
    return 4


def _iter_mode_select_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return mode select endpoints from sensor controller and timer devices."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
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


def _iter_brightness_threshold_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return brightness threshold select endpoints for sensor devices."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
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


def _iter_motion_sensitivity_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return motion sensitivity select endpoints for sensor devices."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
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


def _iter_indicator_led_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return indicator LED select endpoints for switch-like devices."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        if not record.capabilities.supports_switch_indicator_led:
            continue
        parent_identifier = physical_device_identifier(record)
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="indicator_led_on",
                command_target="indicator_led_on",
                entity_unique_id=endpoint_unique_identifier(record, "indicator_led_on"),
                device_identifier=parent_identifier,
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="LED when on",
            )
        )
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="indicator_led_off",
                command_target="indicator_led_off",
                entity_unique_id=endpoint_unique_identifier(record, "indicator_led_off"),
                device_identifier=parent_identifier,
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="LED when off",
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
    for endpoint in _iter_indicator_led_endpoints(inventory):
        entities.append(PixiePlusIndicatorLedSelectEntity(runtime_data, endpoint))
    async_add_entities(entities)

    @callback
    def _async_add_device_entities(device_id: int) -> None:
        current_inventory = runtime_data.pixie_runtime.inventory
        if current_inventory is None:
            return
        entities_to_add: list = []
        for endpoint in _iter_mode_select_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusModeSelectEntity(runtime_data, endpoint))
        for endpoint in _iter_brightness_threshold_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusSensorParamSelectEntity(runtime_data, endpoint))
        for endpoint in _iter_motion_sensitivity_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusSensorParamSelectEntity(runtime_data, endpoint))
        for endpoint in _iter_indicator_led_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusIndicatorLedSelectEntity(runtime_data, endpoint))
        if entities_to_add:
            async_add_entities(entities_to_add)

    entry.async_on_unload(async_dispatcher_connect(hass, device_added_signal(entry), _async_add_device_entities))


class PixiePlusModeSelectEntity(PixiePlusCoordinatorEntity, SelectEntity):
    """Representation of a Pixie Plus mode select entity (sensor or timer)."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        if self.record.capabilities.supports_timer:
            self._attr_options = get_timer_select_options_for_capabilities(self.record.capabilities)
        else:
            self._attr_options = get_sensor_select_options_for_capabilities(self.record.capabilities)

    @property
    def current_option(self) -> str | None:
        runtime = self.record.runtime
        if isinstance(runtime.mode, int):
            if self.record.capabilities.supports_timer:
                return timer_mode_value_to_option(runtime.mode)
            return sensor_mode_value_to_option_for_capabilities(self.record.capabilities, runtime.mode)
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
            mode_value = sensor_option_to_mode_value_for_capabilities(self.record.capabilities, option)
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

    _attr_entity_category = EntityCategory.CONFIG

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
            if val == 5:
                val = _learned_threshold_raw_to_index(runtime.learned_brightness_threshold_raw)
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


class PixiePlusIndicatorLedSelectEntity(PixiePlusCoordinatorEntity, SelectEntity):
    """Select entity for switch indicator LED settings."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        self._when_on = endpoint.endpoint_key == "indicator_led_on"
        self._attr_options = get_indicator_led_options(when_on=self._when_on)

    @property
    def current_option(self) -> str | None:
        runtime = self.record.runtime
        value = runtime.indicator_led_on if self._when_on else runtime.indicator_led_off
        option = indicator_led_value_to_option(value)
        if option in self._attr_options:
            return option
        return None

    async def async_select_option(self, option: str) -> None:
        """Change the switch indicator LED setting."""
        if option not in self._attr_options:
            raise HomeAssistantError(f"Unsupported LED option: {option}")

        runtime = self.record.runtime
        current_on = runtime.indicator_led_on
        current_off = runtime.indicator_led_off
        if self._when_on:
            on_value = indicator_led_option_to_value(option, when_on=True)
            off_value = current_off
        else:
            on_value = current_on
            off_value = indicator_led_option_to_value(option, when_on=False)
        if on_value is None or off_value is None:
            raise HomeAssistantError("Refresh LED settings before changing this option")

        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_indicator_led_action="set",
                command_indicator_led_on=int(on_value),
                command_indicator_led_off=int(off_value),
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
