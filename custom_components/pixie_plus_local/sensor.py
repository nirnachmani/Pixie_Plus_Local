"""Sensor platform for Pixie Plus Local (timer remaining)."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import (
    DOMAIN,
    MANUFACTURER,
    PixieEndpoint,
    PixiePlusConfigEntryRuntimeData,
    PixiePlusCoordinatorEntity,
    endpoint_unique_identifier,
    gateway_device_identifier,
    physical_device_identifier,
)
from .pixie_ble import BT_STATE_DISABLED, BT_STATE_NO_WORKING_PROXY

LOGGER = logging.getLogger(__name__)

CONNECTION_STATE_CONNECTED = "connected"
CONNECTION_STATE_CONNECTING = "connecting"
CONNECTION_STATE_DISCONNECTED = "disconnected"


def _gateway_connection_unique_identifier(inventory, key: str) -> str:
    """Return a stable unique identifier for one gateway connection sensor."""
    return f"{gateway_device_identifier(inventory)}:{key}"


def _lan_connection_state(runtime_data: PixiePlusConfigEntryRuntimeData) -> str:
    """Return the current LAN/TCP control connection state."""
    runtime_session = runtime_data.pixie_runtime.runtime_session
    if runtime_session is None:
        return CONNECTION_STATE_DISCONNECTED
    if not runtime_session.is_alive():
        return CONNECTION_STATE_DISCONNECTED
    if runtime_session.primed_at is None:
        return CONNECTION_STATE_CONNECTING
    if (
        runtime_session.error is not None
        or runtime_session.connection_closed_at is not None
        or runtime_session.needs_restart()
    ):
        return CONNECTION_STATE_DISCONNECTED
    return CONNECTION_STATE_CONNECTED


def _bluetooth_connection_state(runtime_data: PixiePlusConfigEntryRuntimeData) -> str:
    """Return the current Bluetooth runtime connection state."""
    ble_runtime = runtime_data.ble_runtime
    if ble_runtime is None or not ble_runtime.enabled:
        return CONNECTION_STATE_DISCONNECTED
    if ble_runtime.health.healthy:
        return CONNECTION_STATE_CONNECTED
    if ble_runtime.health.state in (BT_STATE_DISABLED, BT_STATE_NO_WORKING_PROXY):
        return CONNECTION_STATE_DISCONNECTED
    task = ble_runtime._task
    if task is not None and not task.done():
        return CONNECTION_STATE_CONNECTING
    return CONNECTION_STATE_DISCONNECTED


def _iter_timer_sensor_endpoints(inventory) -> list[PixieEndpoint]:
    """Return timer remaining sensor endpoints for timer-capable devices."""
    gateway_identifier = gateway_device_identifier(inventory)
    endpoints: list[PixieEndpoint] = []
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if not record.capabilities.supports_timer:
            continue
        endpoints.append(
            PixieEndpoint(
                device_id=record.id,
                endpoint_key="timer_remaining",
                command_target="timer_poll",
                entity_unique_id=endpoint_unique_identifier(record, "timer_remaining"),
                device_identifier=physical_device_identifier(record),
                device_name=record.name,
                via_device_identifier=gateway_identifier,
                entity_name="Timer",
            )
        )
    return endpoints


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Pixie Plus Local sensor entities."""
    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        return

    gateway_identifier = gateway_device_identifier(inventory)
    gateway = inventory.gateway
    LOGGER.debug(
        "%sAdding Pixie gateway connection sensors gateway_identifier=%s gateway_name=%s gateway_model=%s",
        runtime_data._log_prefix,
        gateway_identifier,
        gateway.model_name if gateway else None,
        gateway.model_no if gateway else None,
    )
    entities: list[SensorEntity] = [
        PixieGatewayConnectionSensorEntity(runtime_data, "lan", "LAN", _lan_connection_state),
        PixieGatewayConnectionSensorEntity(runtime_data, "bluetooth", "Bluetooth", _bluetooth_connection_state),
    ]
    entities.extend(
        PixiePlusTimerRemainingSensorEntity(runtime_data, endpoint)
        for endpoint in _iter_timer_sensor_endpoints(inventory)
    )
    async_add_entities(entities)


class PixieGatewayConnectionSensorEntity(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor showing one gateway connection path state."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        CONNECTION_STATE_DISCONNECTED,
        CONNECTION_STATE_CONNECTING,
        CONNECTION_STATE_CONNECTED,
    ]

    def __init__(
        self,
        runtime_data: PixiePlusConfigEntryRuntimeData,
        key: str,
        name: str,
        state_getter,
    ) -> None:
        super().__init__(runtime_data.coordinator)
        self.runtime_data = runtime_data
        self._state_getter = state_getter
        self._last_native_value: str | None = None
        inventory = runtime_data.pixie_runtime.inventory
        self._gateway_identifier = gateway_device_identifier(inventory)
        self._gateway = inventory.gateway
        self._attr_unique_id = _gateway_connection_unique_identifier(inventory, key)
        self._attr_name = name
        LOGGER.debug(
            "%sPrepared Pixie gateway connection sensor name=%s unique_id=%s gateway_identifier=%s initial_state=%s",
            runtime_data._log_prefix,
            name,
            self._attr_unique_id,
            self._gateway_identifier,
            self.native_value,
        )

    @property
    def native_value(self) -> str:
        """Return the current connection state."""
        return self._state_getter(self.runtime_data)

    @property
    def available(self) -> bool:
        """Gateway connection status sensors are available while the integration is loaded."""
        return True

    @property
    def device_info(self):
        """Attach the connection sensors to the gateway device."""
        gateway = self._gateway
        return {
            "identifiers": {(DOMAIN, self._gateway_identifier)},
            "manufacturer": MANUFACTURER,
            "name": gateway.model_name or "Pixie Gateway" if gateway else "Pixie Gateway",
            "model": gateway.model_name if gateway else "Pixie Gateway",
            "model_id": gateway.model_no if gateway else None,
        }

    async def async_added_to_hass(self) -> None:
        """Register for immediate connection-state updates."""
        await super().async_added_to_hass()
        self._last_native_value = self.native_value
        LOGGER.debug(
            "%sAdded Pixie gateway connection sensor entity_id=%s unique_id=%s gateway_identifier=%s state=%s",
            self.runtime_data._log_prefix,
            self.entity_id,
            self.unique_id,
            self._gateway_identifier,
            self._last_native_value,
        )
        self.async_on_remove(
            self.runtime_data.async_add_connection_state_listener(self._handle_connection_state_update)
        )

    @callback
    def _handle_connection_state_update(self) -> None:
        """Write state only when the derived connection state actually changes."""
        native_value = self.native_value
        if native_value == self._last_native_value:
            return
        self._last_native_value = native_value
        self.async_write_ha_state()


class PixiePlusTimerRemainingSensorEntity(PixiePlusCoordinatorEntity, SensorEntity):
    """Sensor showing remaining time on an active timer.

    Uses local estimation between polls: the base value comes from the last
    d36969 response, then wall-clock elapsed time is subtracted every second
    to give a smooth countdown. The coordinator polls every 10s to correct drift.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"

    def __init__(self, runtime_data: PixiePlusConfigEntryRuntimeData, endpoint: PixieEndpoint) -> None:
        super().__init__(runtime_data, endpoint, domain=DOMAIN)
        self._tick_remove = None

    @property
    def available(self) -> bool:
        """Sensor is only available when timer mode is active and light is on."""
        if not super().available:
            return False
        runtime = self.record.runtime
        return runtime.mode == 1 and runtime.is_on is True

    @property
    def native_value(self) -> float | None:
        """Return the estimated remaining timer seconds.

        Computed from the last authoritative poll value minus wall-clock
        elapsed time since that poll. Never returns below 0.
        """
        runtime = self.record.runtime
        remaining = runtime.timer_remaining_seconds
        if remaining is None:
            return None

        last_poll = runtime.last_timer_poll_at
        if last_poll is None:
            return float(remaining)

        import time as _time
        elapsed_seconds = _time.time() - last_poll
        estimated = max(0.0, float(remaining) - elapsed_seconds)
        return round(estimated, 1)

    async def async_added_to_hass(self) -> None:
        """Start 1-second refresh ticks when entity is added."""
        await super().async_added_to_hass()
        self._start_ticking()

    async def async_will_remove_from_hass(self) -> None:
        """Stop refresh ticks when entity is removed."""
        self._stop_ticking()
        await super().async_will_remove_from_hass()

    @callback
    def _start_ticking(self) -> None:
        """Schedule a 1-second callback to refresh the sensor value."""
        if self._tick_remove is not None:
            return
        self._tick_remove = async_track_time_interval(
            self.hass,
            self._tick,
            timedelta(seconds=1),
        )

    @callback
    def _stop_ticking(self) -> None:
        """Remove the 1-second callback."""
        if self._tick_remove is not None:
            self._tick_remove()
            self._tick_remove = None

    @callback
    def _tick(self, _now=None) -> None:
        """Refresh the entity state from the estimation formula."""
        self.async_write_ha_state()
