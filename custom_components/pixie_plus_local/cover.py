"""Cover platform for Pixie Plus Local (blinds and gate doors)."""

from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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
from .config_flow import get_cover_mapping_for_controller
from .pixie_value_profiles import (
    GATE_STATE_CLOSED,
    GATE_STATE_CLOSING,
    GATE_STATE_OPEN,
    GATE_STATE_OPENING,
    GATE_STATE_PAUSED,
    estimate_gate_motion_position_percent,
    gate_can_run_action,
)


def _iter_cover_endpoints(inventory) -> list[PixieEndpoint]:
    """Return blind cover endpoints (excludes gate devices)."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_cover or record.capabilities.supports_gate:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="main",
                command_target="relay",
                entity_unique_id=endpoint_unique_identifier(record, "main"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_translation_key="blind",
            )
        )
    return endpoints


def _iter_gate_endpoints(inventory) -> list[PixieEndpoint]:
    """Return gate door cover endpoints."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    door_names = {0: "Door 1", 1: "Door 2"}
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_gate:
            continue
        for door in range(record.capabilities.gate_doors):
            endpoints.append(
                PixieEndpoint(
                    device_id=record.id,
                    endpoint_key=f"door{door + 1}",
                    command_target=f"gate_door{door}",
                    entity_unique_id=endpoint_unique_identifier(record, f"door{door + 1}"),
                    device_identifier=physical_device_identifier(record),
                    device_name=record.name,
                    via_device_identifier=gateway_identifier,
                    entity_name=door_names.get(door, f"Door {door + 1}"),
                )
            )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local cover entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    entities: list = []
    for endpoint in _iter_cover_endpoints(inventory):
        entities.append(PixiePlusCoverEntity(runtime_data, entry, endpoint))
    for endpoint in _iter_gate_endpoints(inventory):
        entities.append(PixiePlusGateCoverEntity(runtime_data, endpoint))
    async_add_entities(entities)


class PixiePlusCoverEntity(PixiePlusCoordinatorEntity, CoverEntity):
    """Representation of a Pixie Plus blind controller."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, entry: ConfigEntry, endpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        self.entry = entry
        self._attr_device_class = CoverDeviceClass.BLIND

        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        _action_map, tilt_map = get_cover_mapping_for_controller(entry.options, endpoint.device_id)
        tilt_map = tilt_map or {}
        if tilt_map.get("open_tilt"):
            features |= CoverEntityFeature.OPEN_TILT
        if tilt_map.get("close_tilt"):
            features |= CoverEntityFeature.CLOSE_TILT
        if tilt_map.get("stop_tilt"):
            features |= CoverEntityFeature.STOP_TILT
        self._attr_supported_features = features

        self._assumed_is_closed: bool | None = None
        self._is_opening = False
        self._is_closing = False

    @property
    def assumed_state(self) -> bool:
        return True

    @property
    def is_closed(self) -> bool | None:
        return self._assumed_is_closed

    @property
    def is_opening(self) -> bool | None:
        return self._is_opening

    @property
    def is_closing(self) -> bool | None:
        return self._is_closing

    async def _async_send_cover_action(self, action: str) -> None:
        try:
            action_map, tilt_action_map = get_cover_mapping_for_controller(self.entry.options, self.record.id)
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_cover_action=action,
                command_cover_action_map=action_map,
                command_cover_tilt_action_map=tilt_action_map,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

    async def async_open_cover(self, **kwargs) -> None:
        await self._async_send_cover_action("open")
        self._assumed_is_closed = False
        self._is_opening = True
        self._is_closing = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs) -> None:
        await self._async_send_cover_action("close")
        self._assumed_is_closed = True
        self._is_opening = False
        self._is_closing = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs) -> None:
        await self._async_send_cover_action("stop")
        self._is_opening = False
        self._is_closing = False
        self.async_write_ha_state()

    async def async_open_cover_tilt(self, **kwargs) -> None:
        await self._async_send_cover_action("open_tilt")

    async def async_close_cover_tilt(self, **kwargs) -> None:
        await self._async_send_cover_action("close_tilt")

    async def async_stop_cover_tilt(self, **kwargs) -> None:
        await self._async_send_cover_action("stop_tilt")


class PixiePlusGateCoverEntity(PixiePlusCoordinatorEntity, CoverEntity):
    """Cover entity for a single gate door."""

    _attr_device_class = CoverDeviceClass.GATE

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        self._door_index = 0 if endpoint.endpoint_key == "door1" else 1
        self._attr_supported_features = (
            CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        )
        self._tick_remove = None

    @property
    def assumed_state(self) -> bool:
        return False

    def _door_state(self) -> int | None:
        runtime = self.record.runtime
        if self._door_index == 0:
            return runtime.door1_state
        return runtime.door2_state

    def _door_decoded(self) -> dict | None:
        runtime = self.record.runtime
        decoded = runtime.door1_decoded if self._door_index == 0 else runtime.door2_decoded
        return decoded if isinstance(decoded, dict) else None

    def _door_motion_plan(self) -> dict | None:
        runtime = self.record.runtime
        motion_plan = runtime.door1_motion_plan if self._door_index == 0 else runtime.door2_motion_plan
        return motion_plan if isinstance(motion_plan, dict) else None

    @property
    def current_cover_position(self) -> int | None:
        motion_plan = self._door_motion_plan()
        if motion_plan is not None:
            estimated_position = estimate_gate_motion_position_percent(motion_plan, int(time.time() * 1000))
            if isinstance(estimated_position, int):
                return estimated_position
        decoded = self._door_decoded()
        if not isinstance(decoded, dict):
            return None
        position = decoded.get("position_percent")
        return int(position) if isinstance(position, int) else None

    @property
    def extra_state_attributes(self) -> dict:
        decoded = self._door_decoded() or {}
        valid_actions = [
            action
            for action in ("open", "close", "stop")
            if gate_can_run_action(decoded if decoded else None, action)
        ]
        return {
            "gate_state": decoded.get("state"),
            "gate_raw_state": f"0x{self._door_state():02x}" if isinstance(self._door_state(), int) else None,
            "gate_state_known": decoded.get("known"),
            "gate_next_action": decoded.get("next_action"),
            "gate_fault": decoded.get("fault"),
            "gate_fault_code": decoded.get("fault_code"),
            "gate_sensor_closed": decoded.get("sensor_closed"),
            "gate_interpolating": self._door_motion_plan() is not None,
            "gate_valid_actions": valid_actions,
        }

    @property
    def is_closed(self) -> bool | None:
        decoded = self._door_decoded()
        if not isinstance(decoded, dict):
            return None
        state = decoded.get("state")
        if state == GATE_STATE_CLOSED:
            return True
        if state in {GATE_STATE_OPEN, GATE_STATE_OPENING, GATE_STATE_CLOSING, GATE_STATE_PAUSED}:
            return False
        return None

    @property
    def is_opening(self) -> bool | None:
        decoded = self._door_decoded()
        return bool(isinstance(decoded, dict) and decoded.get("state") == GATE_STATE_OPENING)

    @property
    def is_closing(self) -> bool | None:
        decoded = self._door_decoded()
        return bool(isinstance(decoded, dict) and decoded.get("state") == GATE_STATE_CLOSING)

    async def async_added_to_hass(self) -> None:
        """Start 1-second refresh ticks for interpolated gate movement."""
        await super().async_added_to_hass()
        self._start_ticking()

    async def async_will_remove_from_hass(self) -> None:
        """Stop the 1-second refresh ticks when the entity is removed."""
        self._stop_ticking()
        await super().async_will_remove_from_hass()

    @callback
    def _start_ticking(self) -> None:
        """Schedule a 1-second callback to refresh interpolated position."""
        if self._tick_remove is not None:
            return
        self._tick_remove = async_track_time_interval(
            self.hass,
            self._tick,
            timedelta(seconds=1),
        )

    @callback
    def _stop_ticking(self) -> None:
        """Remove the 1-second interpolation callback."""
        if self._tick_remove is not None:
            self._tick_remove()
            self._tick_remove = None

    @callback
    def _tick(self, _now=None) -> None:
        """Refresh the entity so interpolated position is reflected in HA."""
        if self._door_motion_plan() is not None:
            self.async_write_ha_state()

    async def _async_send_gate_action(self, action: str) -> None:
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_cover_action=action,
                command_gate_door=self._door_index,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

    async def async_open_cover(self, **kwargs) -> None:
        await self._async_send_gate_action("open")

    async def async_close_cover(self, **kwargs) -> None:
        await self._async_send_gate_action("close")

    async def async_stop_cover(self, **kwargs) -> None:
        await self._async_send_gate_action("stop")
