"""Number platform for Pixie Plus Local (timer duration, sensor hold time)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
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

def _iter_timer_duration_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return timer duration number endpoints for timer-capable devices."""
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


def _iter_hold_time_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return hold time number endpoints for sensor devices."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
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


def _iter_power_poll_interval_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return power-meter poll interval endpoints."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        if not record.capabilities.supports_power_metering:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="power_poll_interval",
                command_target="power_poll_interval",
                entity_unique_id=endpoint_unique_identifier(record, "power_poll_interval"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Power poll interval",
            )
        )
    return endpoints


def _iter_gate_setting_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return gate configuration number endpoints."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        if not record.capabilities.supports_gate:
            continue
        parent_identifier = physical_device_identifier(record)
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="gate_signal_width",
                command_target="signal_width",
                entity_unique_id=endpoint_unique_identifier(record, "gate_signal_width"),
                device_identifier=parent_identifier,
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Signal width",
            )
        )
        for door_index in range(max(1, int(record.capabilities.gate_doors))):
            door_label = f"Door {door_index + 1} " if record.capabilities.gate_doors >= 2 else ""
            for endpoint_key, command_target, entity_name in (
                (f"door{door_index + 1}_open_duration", "door_open_duration", f"{door_label}Opening duration"),
                (f"door{door_index + 1}_close_duration", "door_close_duration", f"{door_label}Closing duration"),
            ):
                endpoints.append(
                    PixieEndpoint(
                        device_id=record.id,
                        endpoint_key=endpoint_key,
                        command_target=command_target,
                        entity_unique_id=endpoint_unique_identifier(record, endpoint_key),
                        device_identifier=parent_identifier,
                        device_name=record.name,
                        via_device_identifier=gateway_identifier,
                        entity_name=entity_name,
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
    for endpoint in _iter_power_poll_interval_endpoints(inventory):
        entities.append(PixiePlusPowerPollIntervalNumberEntity(runtime_data, endpoint))
    for endpoint in _iter_gate_setting_endpoints(inventory):
        entities.append(PixiePlusGateSettingNumberEntity(runtime_data, endpoint))
    async_add_entities(entities)

    @callback
    def _async_add_device_entities(device_id: int) -> None:
        current_inventory = runtime_data.pixie_runtime.inventory
        if current_inventory is None:
            return
        entities_to_add: list = []
        for endpoint in _iter_timer_duration_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusTimerDurationNumberEntity(runtime_data, endpoint))
        for endpoint in _iter_hold_time_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusHoldTimeNumberEntity(runtime_data, endpoint))
        for endpoint in _iter_power_poll_interval_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusPowerPollIntervalNumberEntity(runtime_data, endpoint))
        for endpoint in _iter_gate_setting_endpoints(current_inventory, device_id=int(device_id)):
            entities_to_add.append(PixiePlusGateSettingNumberEntity(runtime_data, endpoint))
        if entities_to_add:
            async_add_entities(entities_to_add)

    entry.async_on_unload(async_dispatcher_connect(hass, device_added_signal(entry), _async_add_device_entities))


class PixiePlusTimerDurationNumberEntity(PixiePlusCoordinatorEntity, NumberEntity):
    """Number input for setting the timer countdown duration (seconds)."""

    _attr_entity_category = EntityCategory.CONFIG
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

    _attr_entity_category = EntityCategory.CONFIG
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


class PixiePlusPowerPollIntervalNumberEntity(PixiePlusCoordinatorEntity, NumberEntity):
    """Number input for the 0208 metering poll interval."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = 86400
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def native_value(self) -> int | None:
        return self.runtime_data.power_meter_poll_interval_seconds(self.record)

    async def async_set_native_value(self, value: float) -> None:
        seconds = max(1, min(86400, round(value)))
        try:
            await self.runtime_data.async_set_power_meter_poll_interval(self.record, seconds)
        except Exception as err:
            raise HomeAssistantError(str(err)) from err


class PixiePlusGateSettingNumberEntity(PixiePlusCoordinatorEntity, NumberEntity):
    """Number input for gate signal width and travel durations."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        if endpoint.command_target == "signal_width":
            self._attr_native_max_value = 5

    @property
    def native_value(self) -> int | None:
        runtime = self.record.runtime
        key = self.endpoint.endpoint_key
        if key == "gate_signal_width":
            return runtime.gate_signal_width_seconds
        value_ms = None
        if key == "door1_open_duration":
            value_ms = runtime.door1_open_duration_ms
        elif key == "door1_close_duration":
            value_ms = runtime.door1_close_duration_ms
        elif key == "door2_open_duration":
            value_ms = runtime.door2_open_duration_ms
        elif key == "door2_close_duration":
            value_ms = runtime.door2_close_duration_ms
        if isinstance(value_ms, int):
            return max(1, round(value_ms / 1000))
        return None

    async def async_set_native_value(self, value: float) -> None:
        seconds = max(1, min(int(self._attr_native_max_value or 300), round(value)))
        door = 0 if self.endpoint.endpoint_key.startswith("door1") else 1 if self.endpoint.endpoint_key.startswith("door2") else 0
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_gate_param=self.endpoint.command_target,
                command_gate_param_value=seconds,
                command_gate_door=door,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
