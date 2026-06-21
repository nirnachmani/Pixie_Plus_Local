"""Binary sensor platform for Pixie Plus Local."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
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
    """Return binary sensor endpoints for supported sensor-style devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        parent_identifier = physical_device_identifier(record)

        if record.capabilities.supports_sensor:
            endpoints.append(
                PixieEndpoint(
                    device_id=record.id,
                    endpoint_key="sensor_light_state",
                    command_target="relay",
                    entity_unique_id=endpoint_unique_identifier(record, "sensor_light_state"),
                    device_identifier=parent_identifier,
                    device_name=record.name,
                    via_device_identifier=gateway_identifier,
                    entity_name="Motion",
                )
            )
            continue

        if record.capabilities.supports_contact_sensor:
            endpoints.append(
                PixieEndpoint(
                    device_id=record.id,
                    endpoint_key="contact_state",
                    command_target="contact",
                    entity_unique_id=endpoint_unique_identifier(record, "contact_state"),
                    device_identifier=parent_identifier,
                    device_name=record.name,
                    via_device_identifier=gateway_identifier,
                    entity_name=None,
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
        if self.record.model_no == "3010":
            self._attr_device_class = BinarySensorDeviceClass.DOOR

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.record.capabilities.supports_contact_sensor:
            return self.record.runtime.armed is not False
        mode = self.record.runtime.mode
        # No sensor entity in switch/manual mode.
        if isinstance(mode, int):
            return mode != 0
        return False

    @property
    def is_on(self) -> bool | None:
        runtime = self.record.runtime
        if self.record.capabilities.supports_contact_sensor:
            if self.record.model_no == "3010":
                if runtime.contact_active is None:
                    return None
                return not runtime.contact_active
            return runtime.contact_active
        if runtime.mode == 1 and isinstance(runtime.motion, bool):
            return runtime.motion
        if isinstance(runtime.relay, int):
            return runtime.relay != 0
        if isinstance(runtime.motion, bool):
            return runtime.motion
        return runtime.is_on
