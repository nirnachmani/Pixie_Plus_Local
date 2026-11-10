"""Fan platform for Pixie Plus Local."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import DIRECTION_FORWARD, DIRECTION_REVERSE, FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util.percentage import percentage_to_ranged_value, ranged_value_to_percentage

from .pixie_const import DOMAIN
from .pixie_value_profiles import DEFAULT_FAN_SPEED_COUNT
from .pixie_ha import (
    PixieEndpoint,
    PixiePlusConfigEntryRuntimeData,
    PixiePlusCoordinatorEntity,
    device_added_signal,
    endpoint_unique_identifier,
    parent_device_identifier,
    physical_device_identifier,
)


def _iter_fan_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and record.id != int(device_id):
            continue
        if not record.capabilities.is_fan:
            continue
        endpoints.append(PixieEndpoint(
            device_id=record.id,
            endpoint_key="fan",
            command_target="fan",
            entity_unique_id=endpoint_unique_identifier(record, "fan"),
            device_identifier=physical_device_identifier(record),
            device_name=record.name,
            via_device_identifier=gateway_identifier,
            entity_name="Fan",
        ))
    return endpoints


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return
    async_add_entities(PixiePlusFanEntity(runtime_data, endpoint) for endpoint in _iter_fan_endpoints(inventory))

    @callback
    def _async_add_device_entities(device_id: int) -> None:
        current_inventory = runtime_data.pixie_runtime.inventory
        if current_inventory is not None:
            async_add_entities(PixiePlusFanEntity(runtime_data, endpoint) for endpoint in _iter_fan_endpoints(current_inventory, device_id))

    entry.async_on_unload(async_dispatcher_connect(hass, device_added_signal(entry), _async_add_device_entities))


class PixiePlusFanEntity(PixiePlusCoordinatorEntity, FanEntity):
    """Pixie/Hunter Pacific DC fan with a model-defined number of discrete speeds."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.DIRECTION
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def speed_count(self) -> int:
        """Discrete speeds this model exposes (5715: 9, 0615: 6)."""
        try:
            count = self.record.capabilities.fan_speed_count
        except (KeyError, AttributeError):
            return DEFAULT_FAN_SPEED_COUNT
        return count if isinstance(count, int) and count > 0 else DEFAULT_FAN_SPEED_COUNT

    @property
    def _speed_range(self) -> tuple[int, int]:
        return (1, self.speed_count)

    @property
    def is_on(self) -> bool | None:
        speed = self.record.runtime.fan_speed
        return speed > 0 if isinstance(speed, int) else None

    @property
    def percentage(self) -> int | None:
        speed = self.record.runtime.fan_speed
        if not isinstance(speed, int) or speed <= 0:
            return None
        return ranged_value_to_percentage(self._speed_range, speed)

    @property
    def current_direction(self) -> str | None:
        direction = self.record.runtime.fan_direction
        if direction == "winter":
            return DIRECTION_REVERSE
        if direction == "summer":
            return DIRECTION_FORWARD
        return None

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the fan on at its requested or remembered speed."""
        max_speed = self.speed_count
        if percentage is None:
            speed = self.record.runtime.last_fan_speed or self.record.runtime.fan_speed or max_speed
        else:
            speed = math.ceil(percentage_to_ranged_value(self._speed_range, percentage))
        speed = max(1, min(max_speed, speed))
        try:
            await self.runtime_data.async_send_local_command(self.hass, command_device_id=self.record.id, command_fan_speed=speed)
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.runtime_data.async_send_local_command(self.hass, command_device_id=self.record.id, command_fan_speed=0)
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage <= 0:
            await self.async_turn_off()
        else:
            await self.async_turn_on(percentage=percentage)

    async def async_set_direction(self, direction: str) -> None:
        target = "winter" if direction == DIRECTION_REVERSE else "summer"
        try:
            await self.runtime_data.async_send_local_command(self.hass, command_device_id=self.record.id, command_fan_direction=target)
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
