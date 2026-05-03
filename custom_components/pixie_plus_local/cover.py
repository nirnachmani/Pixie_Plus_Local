"""Cover platform for Pixie Plus Local."""

from __future__ import annotations

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
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
from .config_flow import get_cover_mapping_for_controller


def _iter_cover_endpoints(inventory) -> list[PixieEndpoint]:
    """Return cover endpoints from inventory."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_cover:
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

    async_add_entities(PixiePlusCoverEntity(runtime_data, entry, endpoint) for endpoint in _iter_cover_endpoints(inventory))


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