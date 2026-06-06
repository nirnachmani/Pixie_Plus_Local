"""Light platform for Pixie Plus Local."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
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


def ha_brightness_to_percent(brightness: int | None) -> int | None:
    """Convert HA 1..255 brightness to device 0..100 percent."""
    if brightness is None:
        return None
    if brightness <= 0:
        return 0
    return max(1, min(100, round((int(brightness) / 255) * 100)))


def percent_to_ha_brightness(percent: int | None) -> int | None:
    """Convert device 0..100 brightness to HA 1..255 brightness."""
    if percent is None:
        return None
    if percent <= 0:
        return None
    return max(1, min(255, round((int(percent) / 100) * 255)))


def _iter_light_endpoints(inventory) -> list[PixieEndpoint]:
    """Return light endpoints from inventory."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.is_light:
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
                entity_translation_key="light",
            )
        )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local light entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    async_add_entities(PixiePlusLightEntity(runtime_data, endpoint) for endpoint in _iter_light_endpoints(inventory))


class PixiePlusLightEntity(PixiePlusCoordinatorEntity, LightEntity):
    """Representation of a Pixie Plus light-like device."""

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        features = LightEntityFeature(0)
        if self.record.capabilities.supports_effects:
            features |= LightEntityFeature.EFFECT
            self._attr_effect_list = list(self.record.capabilities.effect_names)
        if self.record.capabilities.supports_effects and not self._attr_effect_list:
            self._attr_effect_list = []
        self._attr_supported_features = features

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        if self.record.capabilities.supports_color:
            return {ColorMode.RGB}
        if self.record.capabilities.supports_dimming:
            return {ColorMode.BRIGHTNESS}
        return {ColorMode.ONOFF}

    @property
    def color_mode(self) -> ColorMode:
        if self.record.capabilities.supports_color:
            return ColorMode.RGB
        if self.record.capabilities.supports_dimming:
            return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF

    @property
    def is_on(self) -> bool | None:
        if self.record.capabilities.supports_mode and isinstance(self.record.runtime.relay, int):
            return self.record.runtime.relay != 0
        return self.record.runtime.is_on

    @property
    def available(self) -> bool:
        return super().available

    @property
    def brightness(self) -> int | None:
        if not (self.record.capabilities.supports_dimming or self.record.capabilities.supports_color):
            return None
        return percent_to_ha_brightness(self.record.runtime.br)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        rgb = self.record.runtime.rgb
        if not self.record.capabilities.supports_color or not isinstance(rgb, list) or len(rgb) != 3:
            return None
        return (int(rgb[0]), int(rgb[1]), int(rgb[2]))

    @property
    def effect(self) -> str | None:
        if not self.record.capabilities.supports_effects:
            return None
        return self.record.runtime.effect

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.record.capabilities.supports_mode and self.record.runtime.mode == 1:
            raise HomeAssistantError("Manual light control is disabled while device mode is sensor")

        brightness_pct = ha_brightness_to_percent(kwargs.get(ATTR_BRIGHTNESS))

        try:
            if self.record.capabilities.supports_color and ATTR_RGB_COLOR in kwargs:
                await self.runtime_data.async_send_local_command(
                    self.hass,
                    command_device_id=self.record.id,
                    command_color_rgb=tuple(int(value) for value in kwargs[ATTR_RGB_COLOR]),
                    command_brightness=brightness_pct,
                )
                return

            if self.record.capabilities.supports_effects and ATTR_EFFECT in kwargs:
                await self.runtime_data.async_send_local_command(
                    self.hass,
                    command_device_id=self.record.id,
                    command_effect=str(kwargs[ATTR_EFFECT]),
                    command_brightness=brightness_pct,
                )
                return

            if self.record.capabilities.supports_dimming and brightness_pct is not None:
                await self.runtime_data.async_send_local_command(
                    self.hass,
                    command_device_id=self.record.id,
                    command_brightness=brightness_pct,
                )
                return

            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_state=True,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.record.capabilities.supports_mode and self.record.runtime.mode == 1:
            raise HomeAssistantError("Manual light control is disabled while device mode is sensor")

        try:
            await self.runtime_data.async_send_local_command(
                self.hass,
                command_device_id=self.record.id,
                command_state=False,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err