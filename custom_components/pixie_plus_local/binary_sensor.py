"""Binary sensor platform for Pixie Plus Local."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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


def _iter_binary_sensor_endpoints(inventory) -> list[PixieEndpoint]:
    """Return binary sensor endpoints for sensor-controller devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        parent_identifier = physical_device_identifier(record)

        if not record.capabilities.supports_mode:
            continue

        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="sensor_light_state",
                command_target="relay",
                entity_unique_id=endpoint_unique_identifier(record, "sensor_light_state"),
                device_identifier=parent_identifier,
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_translation_key="sensor_light_state",
            )
        )

    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local binary sensor entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    async_add_entities(
        PixiePlusBinarySensorEntity(runtime_data, endpoint)
        for endpoint in _iter_binary_sensor_endpoints(inventory)
    )


class PixiePlusBinarySensorEntity(PixiePlusCoordinatorEntity, BinarySensorEntity):
    """Representation of a Pixie Plus binary sensor endpoint."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        mode = self.record.runtime.mode
        # No sensor entity in manual mode.
        if isinstance(mode, int):
            return mode == 1
        return False

    @property
    def is_on(self) -> bool | None:
        runtime = self.record.runtime
        if isinstance(runtime.motion, bool):
            return runtime.motion
        if isinstance(runtime.relay, int):
            return runtime.relay != 0
        return runtime.is_on
