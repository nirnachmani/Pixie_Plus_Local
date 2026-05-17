"""Switch platform for Pixie Plus Local."""

from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import (
    DOMAIN,
    PixieEndpoint,
    PixiePlusConfigEntryRuntimeData,
    PixiePlusCoordinatorEntity,
    child_device_identifier,
    endpoint_unique_identifier,
    gateway_device_identifier,
    physical_device_identifier,
)

SWITCH_DEVICE_CLASSES = {
    "0107": "outlet",
    "0208": "outlet",
    "1002": "switch",
}


def _iter_switch_endpoints(inventory) -> list[PixieEndpoint]:
    """Return switch endpoints from inventory."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        parent_identifier = physical_device_identifier(record)

        if not record.capabilities.is_switch:
            continue

        if record.capabilities.supports_multi_channel:
            left_name = record.left_name
            right_name = record.right_name
            endpoints.extend(
                [
                    PixieEndpoint(
                        device_id=record.id,
                        endpoint_key="left",
                        command_target="left",
                        entity_unique_id=child_device_identifier(record, "left"),
                        device_identifier=parent_identifier,
                        device_name=record.name,
                        via_device_identifier=gateway_identifier,
                        entity_name=left_name or "Left Relay",
                    ),
                    PixieEndpoint(
                        device_id=record.id,
                        endpoint_key="right",
                        command_target="right",
                        entity_unique_id=child_device_identifier(record, "right"),
                        device_identifier=parent_identifier,
                        device_name=record.name,
                        via_device_identifier=gateway_identifier,
                        entity_name=right_name or "Right Relay",
                    ),
                ]
            )
            continue

        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="main",
                command_target="relay",
                entity_unique_id=endpoint_unique_identifier(record, "main"),
                device_identifier=parent_identifier,
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_translation_key="switch",
            )
        )

        if not record.capabilities.supports_usb_subentity:
            continue

        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="usb",
                command_target="usb",
                entity_unique_id=child_device_identifier(record, "usb"),
                device_identifier=parent_identifier,
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="USB",
            )
        )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local switch entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    async_add_entities(PixiePlusSwitchEntity(runtime_data, endpoint) for endpoint in _iter_switch_endpoints(inventory))


class PixiePlusSwitchEntity(PixiePlusCoordinatorEntity, SwitchEntity):
    """Representation of a Pixie Plus switch endpoint."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        device_class = SWITCH_DEVICE_CLASSES.get(self.record.model_no, "switch")
        self._attr_device_class = (
            SwitchDeviceClass.OUTLET if device_class == "outlet" else SwitchDeviceClass.SWITCH
        )

    @property
    def is_on(self) -> bool | None:
        runtime = self.record.runtime
        target = self.endpoint.command_target
        endpoint_key = self.endpoint.endpoint_key

        if target == "usb":
            return bool(runtime.r & 0x02) if isinstance(runtime.r, int) else None
        if endpoint_key == "main" and self.record.capabilities.supports_usb_subentity:
            return bool(runtime.r & 0x01) if isinstance(runtime.r, int) else runtime.is_on
        if target == "left":
            return bool(runtime.r & 0x01) if isinstance(runtime.r, int) else None
        if target == "right":
            return bool(runtime.r & 0x02) if isinstance(runtime.r, int) else None
        return runtime.is_on

    async def async_turn_on(self, **kwargs) -> None:
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_state=True,
                command_target=self.endpoint.command_target,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs) -> None:
        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_state=False,
                command_target=self.endpoint.command_target,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err