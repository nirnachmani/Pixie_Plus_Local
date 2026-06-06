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


def _iter_mode_select_endpoints(inventory) -> list[PixieEndpoint]:
    """Return mode select endpoints from sensor controller devices."""
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
                endpoint_key="mode",
                command_target="mode",
                entity_unique_id=endpoint_unique_identifier(record, "mode"),
                device_identifier=parent_identifier,
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_translation_key="mode",
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

    async_add_entities(PixiePlusModeSelectEntity(runtime_data, endpoint) for endpoint in _iter_mode_select_endpoints(inventory))


class PixiePlusModeSelectEntity(PixiePlusCoordinatorEntity, SelectEntity):
    """Representation of a Pixie Plus mode select entity."""

    _attr_options = ["sensor", "manual"]

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)

    @property
    def current_option(self) -> str | None:
        runtime = self.record.runtime
        if isinstance(runtime.mode, int):
            return "sensor" if runtime.mode == 1 else "manual"
        return None

    async def async_select_option(self, option: str) -> None:
        """Change mode to the selected option."""
        if option not in self._attr_options:
            raise HomeAssistantError(f"Unsupported mode option: {option}")
        mode_value = 1 if option == "sensor" else 0
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_mode=mode_value,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err
