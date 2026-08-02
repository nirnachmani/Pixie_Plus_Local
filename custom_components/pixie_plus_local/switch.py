"""Switch platform for Pixie Plus Local."""

from __future__ import annotations

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
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
    child_device_identifier,
    device_added_signal,
    endpoint_unique_identifier,
    parent_device_identifier,
    physical_device_identifier,
)
from .pixie_inventory import (
    supports_outlet_runtime_config,
    supports_plug_led_settings,
    supports_sensor_advanced_settings,
)


def _iter_switch_endpoints(inventory, device_id: int | None = None) -> list[PixieEndpoint]:
    """Return switch endpoints from inventory."""
    gateway_identifier = parent_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for current_device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[current_device_id]
        if device_id is not None and int(record.id) != int(device_id):
            continue
        parent_identifier = physical_device_identifier(record)

        if record.capabilities.supports_contact_sensor:
            endpoints.append(
                PixieEndpoint(
                    device_id=record.id,
                    endpoint_key="arm",
                    command_target="arm",
                    entity_unique_id=endpoint_unique_identifier(record, "arm"),
                    device_identifier=parent_identifier,
                    device_name=record.name,
                    via_device_identifier=gateway_identifier,
                    entity_name="Armed",
                )
            )
            continue

        if supports_sensor_advanced_settings(record.capabilities):
            endpoints.extend(
                [
                    PixieEndpoint(
                        device_id=record.id,
                        endpoint_key="sensor_led_indicator",
                        command_target="sensor_led_indicator",
                        entity_unique_id=endpoint_unique_identifier(record, "sensor_led_indicator"),
                        device_identifier=parent_identifier,
                        device_name=record.name,
                        via_device_identifier=gateway_identifier,
                        entity_name="LED indicator",
                    ),
                ]
            )

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
            if supports_outlet_runtime_config(record.capabilities):
                endpoints.extend(
                    [
                        PixieEndpoint(
                            device_id=record.id,
                            endpoint_key="outlet_led_indicator",
                            command_target="outlet_led_indicator",
                            entity_unique_id=endpoint_unique_identifier(record, "outlet_led_indicator"),
                            device_identifier=parent_identifier,
                            device_name=record.name,
                            via_device_identifier=gateway_identifier,
                            entity_name="LED indicator",
                        ),
                        PixieEndpoint(
                            device_id=record.id,
                            endpoint_key="outlet_all_device_control",
                            command_target="outlet_all_device_control",
                            entity_unique_id=endpoint_unique_identifier(record, "outlet_all_device_control"),
                            device_identifier=parent_identifier,
                            device_name=record.name,
                            via_device_identifier=gateway_identifier,
                            entity_name="“All devices” control",
                        ),
                        PixieEndpoint(
                            device_id=record.id,
                            endpoint_key="outlet_child_lock",
                            command_target="outlet_child_lock",
                            entity_unique_id=endpoint_unique_identifier(record, "outlet_child_lock"),
                            device_identifier=parent_identifier,
                            device_name=record.name,
                            via_device_identifier=gateway_identifier,
                            entity_name="Child lock",
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
        if supports_plug_led_settings(record.capabilities):
            endpoints.extend(
                [
                    PixieEndpoint(
                        device_id=record.id,
                        endpoint_key="plug_socket_led_indicator",
                        command_target="plug_socket_led_indicator",
                        entity_unique_id=endpoint_unique_identifier(record, "plug_socket_led_indicator"),
                        device_identifier=parent_identifier,
                        device_name=record.name,
                        via_device_identifier=gateway_identifier,
                        entity_name="Socket LED indicator",
                    ),
                    PixieEndpoint(
                        device_id=record.id,
                        endpoint_key="plug_usb_led_indicator",
                        command_target="plug_usb_led_indicator",
                        entity_unique_id=endpoint_unique_identifier(record, "plug_usb_led_indicator"),
                        device_identifier=parent_identifier,
                        device_name=record.name,
                        via_device_identifier=gateway_identifier,
                        entity_name="USB LED indicator",
                    ),
                    PixieEndpoint(
                        device_id=record.id,
                        endpoint_key="plug_all_devices_control",
                        command_target="outlet_all_device_control",
                        entity_unique_id=endpoint_unique_identifier(record, "plug_all_devices_control"),
                        device_identifier=parent_identifier,
                        device_name=record.name,
                        via_device_identifier=gateway_identifier,
                        entity_name="“All devices” control",
                    ),
                ]
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

    @callback
    def _async_add_device_entities(device_id: int) -> None:
        current_inventory = runtime_data.pixie_runtime.inventory
        if current_inventory is None:
            return
        endpoints = _iter_switch_endpoints(current_inventory, device_id=int(device_id))
        if endpoints:
            async_add_entities(PixiePlusSwitchEntity(runtime_data, endpoint) for endpoint in endpoints)

    entry.async_on_unload(async_dispatcher_connect(hass, device_added_signal(entry), _async_add_device_entities))


class PixiePlusSwitchEntity(PixiePlusCoordinatorEntity, SwitchEntity):
    """Representation of a Pixie Plus switch endpoint."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        if endpoint.command_target in {
            "outlet_led_indicator",
            "outlet_all_device_control",
            "outlet_child_lock",
            "plug_socket_led_indicator",
            "plug_usb_led_indicator",
            "sensor_led_indicator",
        }:
            self._attr_entity_category = EntityCategory.CONFIG
        else:
            device_class = self.record.capabilities.switch_type or "switch"
            self._attr_device_class = (
                SwitchDeviceClass.OUTLET if device_class == "outlet" else SwitchDeviceClass.SWITCH
            )

    @property
    def is_on(self) -> bool | None:
        runtime = self.record.runtime
        target = self.endpoint.command_target
        endpoint_key = self.endpoint.endpoint_key

        if target == "arm":
            return runtime.armed
        if target == "usb":
            return bool(runtime.r & 0x02) if isinstance(runtime.r, int) else None
        if endpoint_key == "main" and self.record.capabilities.supports_usb_subentity:
            return bool(runtime.r & 0x01) if isinstance(runtime.r, int) else runtime.is_on
        if target == "left":
            return bool(runtime.r & 0x01) if isinstance(runtime.r, int) else None
        if target == "right":
            return bool(runtime.r & 0x02) if isinstance(runtime.r, int) else None
        if target == "outlet_led_indicator":
            return runtime.outlet_led_indicator
        if target == "outlet_all_device_control":
            return runtime.outlet_all_device_control
        if target == "outlet_child_lock":
            return runtime.outlet_child_lock
        if target == "plug_socket_led_indicator":
            return runtime.plug_socket_led_indicator
        if target == "plug_usb_led_indicator":
            return runtime.plug_usb_led_indicator
        if target == "sensor_led_indicator":
            return runtime.sensor_led_indicator
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
