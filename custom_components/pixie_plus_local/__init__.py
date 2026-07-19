"""Home Assistant config-entry setup for Pixie Plus Local."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
import json
import logging
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .pixie_inventory import DeviceRecord, PixieInventory, online_value_is_online
from .pixie_ble import (
    BT_STATE_NO_WORKING_PROXY,
    BT_STATE_READY,
    BT_STATE_UNAVAILABLE,
    PixieFirmwareAdvertisement,
    PixieBluetoothRuntime,
    async_scan_pixie_firmware_advertisements,
)
from .pixie_runtime import (
    CloudParams,
    PixieAuthError,
    PixieAuthHandler,
    PixieGatewayConnectionError,
    PixieGatewayResolutionError,
    PixieRuntimeData,
)
from .pixie_value_profiles import hardware_list

LOGGER = logging.getLogger(__name__)

DOMAIN = "pixie_plus_local"
MANUFACTURER = "SAL - Pixie Plus"
INTEGRATION_TITLE = "Pixie Plus Local"
PLATFORMS: tuple[str, ...] = ("light", "switch", "cover", "select", "binary_sensor", "button", "number", "sensor")

CONF_HOME_ID = "home_id"
CONF_HOME_NAME = "home_name"
CONF_USER_ID = "user_id"
CONF_MESHNET = "meshnet"
CONF_MESHNET2 = "meshnet2"
CONF_NETID = "netid"
CONF_INVENTORY_MODE = "inventory_mode"
CONF_INVENTORY_FALLBACK_REASON = "inventory_fallback_reason"
CONF_GATEWAY_IP = "gateway_ip"
CONF_GATEWAY_IP_REQUIRED = "gateway_ip_required"
CONF_PIXIE_USERNAME = "pixie_username"
CONF_PIXIE_PASSWORD = "pixie_password"
CONF_BT_ENABLED = "bt_enabled"
CONF_BT_STATE = "bt_state"
CONF_BT_SOURCE = "bt_source"
CONF_BT_ACCESS_NODE = "bt_access_node"
CONF_BT_BETTER_CANDIDATE_SEEN = "bt_better_candidate_seen"
CONF_BT_ACCESS_NODE_PREFERENCE = "bt_access_node_preference"
CONF_COMMAND_TRANSPORT = "command_transport"

INVENTORY_MODE_LOCAL_53216 = "local_53216"
INVENTORY_MODE_CLOUD_FALLBACK = "cloud_fallback"
INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED = "local_53216_failed"
INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY = "unsupported_gateway"
COMMAND_TRANSPORT_TCP_PRIMARY = "tcp_primary"
COMMAND_TRANSPORT_BT_PRIMARY = "bt_primary"
COMMAND_TRANSPORT_TCP_ONLY = "tcp_only"
COMMAND_TRANSPORT_BT_ONLY = "bt_only"
BT_ACCESS_NODE_AUTO = "auto"
BT_ACCESS_NODE_PREFER_GATEWAY = "prefer_gateway"
ISSUE_ID_MISSING_FALLBACK_CREDENTIALS = "missing_fallback_credentials"
ISSUE_ID_GATEWAY_IP_REQUIRED = "gateway_ip_required"
ISSUE_ID_BT_PROXY_UNAVAILABLE = "bt_proxy_unavailable"
ISSUE_ID_LOCAL_INVENTORY_FALLBACK = "local_inventory_fallback"

COORDINATOR_UPDATE_INTERVAL = timedelta(seconds=10)
TIMER_POLL_INTERVAL_SECONDS = 10.0
INVENTORY_STORE_VERSION = 1
BLE_COMMAND_READY_TIMEOUT = 45.0
INVENTORY_SNAPSHOT_SAVE_DEBOUNCE_SECONDS = 1.5
BLE_FIRMWARE_SCAN_SECONDS = 60.0
BLE_FIRMWARE_SCAN_HOUR = 3
BLE_FIRMWARE_SCAN_MINUTE = 0


def _inventory_store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, INVENTORY_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_inventory")


def _home_log_prefix(home_name: str | None, fallback: str | None = None) -> str:
    label = str(home_name or fallback or "").strip()
    if label and label not in ("unknown", "None"):
        return f"[{label}] "
    return ""


def _firmware_version_text(version: Any) -> str | None:
    """Return the Pixie app-style firmware version text."""
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        return None
    if version_int <= 0:
        return None
    return f"{version_int // 10}.{version_int % 10}"


def _entry_log_prefix(entry: ConfigEntry) -> str:
    return _home_log_prefix(entry.data.get(CONF_HOME_NAME), entry.title)


async def _async_load_inventory_snapshot(hass: HomeAssistant, entry: ConfigEntry) -> PixieInventory | None:
    entry_prefix = _entry_log_prefix(entry)
    payload = await _inventory_store(hass, entry).async_load()
    if not isinstance(payload, dict):
        LOGGER.debug("%sNo stored Pixie inventory snapshot found for entry %s", entry_prefix, entry.entry_id)
        return None
    snapshot = payload.get("inventory")
    if not isinstance(snapshot, dict):
        LOGGER.debug("%sStored Pixie inventory snapshot is missing inventory data for entry %s", entry_prefix, entry.entry_id)
        return None
    try:
        inventory = PixieInventory.from_dict(snapshot)
        LOGGER.debug(
            "%sRestored Pixie inventory snapshot for entry %s: home=%s devices=%s",
            _home_log_prefix(inventory.home_name, entry.title),
            entry.entry_id,
            inventory.home_id,
            len(inventory.devices_by_id),
        )
        return inventory
    except Exception as err:
        LOGGER.warning("%sCould not restore Pixie inventory snapshot: %s", entry_prefix, err)
        return None


async def _async_save_inventory_snapshot(hass: HomeAssistant, entry: ConfigEntry, inventory: PixieInventory | None) -> None:
    if inventory is None:
        return
    await _inventory_store(hass, entry).async_save({"inventory": inventory.to_dict()})
    LOGGER.debug(
        "%sSaved Pixie inventory snapshot for entry %s: home=%s devices=%s",
        _home_log_prefix(inventory.home_name, entry.title),
        entry.entry_id,
        inventory.home_id,
        len(inventory.devices_by_id),
    )


def _inventory_persistent_signature(inventory: PixieInventory | None) -> str | None:
    """Return a stable signature for fields worth persisting to storage."""
    if inventory is None:
        return None

    snapshot = inventory.to_dict()
    for device in snapshot.get("devices") or []:
        if not isinstance(device, dict):
            continue
        runtime = device.get("runtime")
        if not isinstance(runtime, dict):
            continue
        runtime.pop("raw", None)
        runtime.pop("last_source", None)
        runtime.pop("last_updated_ms", None)
        if "online" in runtime:
            online = runtime.get("online")
            runtime["online"] = None if online is None else ("online" if online_value_is_online(online) else "offline")

    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)


def _entry_inventory_mode(entry: ConfigEntry) -> str:
    mode = str(entry.data.get(CONF_INVENTORY_MODE) or INVENTORY_MODE_LOCAL_53216)
    resolved_mode = mode if mode in (INVENTORY_MODE_LOCAL_53216, INVENTORY_MODE_CLOUD_FALLBACK) else INVENTORY_MODE_LOCAL_53216
    if resolved_mode != mode:
        LOGGER.debug("Unknown Pixie inventory mode '%s', defaulting to %s", mode, resolved_mode)
    return resolved_mode


def _entry_inventory_fallback_reason(entry: ConfigEntry) -> str:
    return str(entry.data.get(CONF_INVENTORY_FALLBACK_REASON) or "")


def _entry_gateway_supports_local_inventory_53216(entry: ConfigEntry, inventory: PixieInventory | None = None) -> bool:
    if inventory is not None and inventory.gateway is not None:
        return bool(inventory.gateway.supports_local_inventory_53216)
    gateway_payload = entry.data.get("gateway")
    if isinstance(gateway_payload, dict):
        return bool(gateway_payload.get("supports_local_inventory_53216", True))
    return _entry_inventory_fallback_reason(entry) != INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY


def _loaded_pixie_runtime_entries(hass: HomeAssistant) -> list["PixiePlusConfigEntryRuntimeData"]:
    """Return loaded Pixie runtime managers."""
    runtime_entries: list[PixiePlusConfigEntryRuntimeData] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime_data = getattr(entry, "runtime_data", None)
        if isinstance(runtime_data, PixiePlusConfigEntryRuntimeData):
            runtime_entries.append(runtime_data)
    return runtime_entries


async def _async_apply_ble_firmware_advertisements(
    hass: HomeAssistant,
    adverts: list[PixieFirmwareAdvertisement],
    *,
    reason: str,
) -> int:
    """Apply decoded BLE firmware advertisements to every loaded Pixie entry."""
    if not adverts:
        return 0
    changed = 0
    for runtime_data in _loaded_pixie_runtime_entries(hass):
        changed += await runtime_data.async_apply_ble_firmware_advertisements(adverts, reason=reason)
    return changed


async def _async_run_global_ble_version_scan(hass: HomeAssistant, *, reason: str = "manual") -> int:
    """Run one global BLE firmware-version scan and apply results to loaded homes."""
    LOGGER.debug("Pixie BLE firmware-version scan starting reason=%s duration=%.1fs", reason, BLE_FIRMWARE_SCAN_SECONDS)
    adverts = await async_scan_pixie_firmware_advertisements(hass, duration=BLE_FIRMWARE_SCAN_SECONDS)
    changed = await _async_apply_ble_firmware_advertisements(hass, adverts, reason=f"{reason} BLE scan")
    LOGGER.debug(
        "Pixie BLE firmware-version scan finished reason=%s adverts=%s changed=%s",
        reason,
        len(adverts),
        changed,
    )
    return changed


def _async_ensure_ble_firmware_refresh_hooks(hass: HomeAssistant) -> None:
    """Install global scheduled BLE firmware-version refresh hook once."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("ble_firmware_refresh_hooks"):
        return

    unsubs: list[Callable[[], None]] = []

    def _scheduled_scan(_now: Any) -> None:
        def _create_scan_task() -> None:
            hass.async_create_task(_async_run_global_ble_version_scan(hass, reason="scheduled"))

        hass.loop.call_soon_threadsafe(_create_scan_task)

    unsubs.append(
        async_track_time_change(
            hass,
            _scheduled_scan,
            hour=BLE_FIRMWARE_SCAN_HOUR,
            minute=BLE_FIRMWARE_SCAN_MINUTE,
            second=0,
        )
    )
    domain_data["ble_firmware_refresh_hooks"] = unsubs


def _async_maybe_remove_ble_firmware_refresh_hooks(hass: HomeAssistant, unloading_entry: ConfigEntry) -> None:
    """Remove global BLE firmware hooks when the last Pixie entry unloads."""
    if any(
        entry.entry_id != unloading_entry.entry_id and getattr(entry, "runtime_data", None) is not None
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return
    domain_data = hass.data.get(DOMAIN)
    if not isinstance(domain_data, dict):
        return
    unsubs = domain_data.pop("ble_firmware_refresh_hooks", None)
    if not isinstance(unsubs, list):
        return
    for unsub in unsubs:
        try:
            unsub()
        except Exception:
            LOGGER.debug("Could not remove Pixie BLE firmware refresh hook", exc_info=True)


def _inventory_fallback_reason_text(reason: str) -> str:
    """Return a concise reason for cloud inventory fallback."""
    if reason == INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY:
        return "this Pixie gateway model does not support local inventory over port 53216"
    return "local inventory over port 53216 did not work"


def _entry_username(entry: ConfigEntry) -> str:
    return str(entry.data.get(CONF_PIXIE_USERNAME) or "")


def _entry_password(entry: ConfigEntry) -> str:
    return str(entry.data.get(CONF_PIXIE_PASSWORD) or "")


def _entry_gateway_ip(entry: ConfigEntry) -> str | None:
    value = str(entry.data.get(CONF_GATEWAY_IP) or "").strip()
    return value or None


def _handler_gateway_ip(handler: PixieAuthHandler) -> str | None:
    """Return the gateway IP verified by the current handler, if known."""
    current_hub = getattr(handler, "current_hub", None)
    if isinstance(current_hub, dict):
        value = str(current_hub.get("host") or "").strip()
        return value or None
    return None


def _entry_gateway_ip_required(entry: ConfigEntry) -> bool:
    value = entry.data.get(CONF_GATEWAY_IP_REQUIRED)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _entry_bt_enabled(entry: ConfigEntry) -> bool:
    value = entry.data.get(CONF_BT_ENABLED)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _entry_command_transport(entry: ConfigEntry) -> str:
    if not _entry_bt_enabled(entry):
        return COMMAND_TRANSPORT_TCP_PRIMARY

    value = str(entry.options.get(CONF_COMMAND_TRANSPORT) or COMMAND_TRANSPORT_TCP_PRIMARY)
    allowed = {
        COMMAND_TRANSPORT_TCP_PRIMARY,
        COMMAND_TRANSPORT_BT_PRIMARY,
        COMMAND_TRANSPORT_TCP_ONLY,
        COMMAND_TRANSPORT_BT_ONLY,
    }
    if value not in allowed:
        return COMMAND_TRANSPORT_TCP_PRIMARY
    return value


def _entry_bt_access_node_preference(entry: ConfigEntry) -> str:
    """Return the configured BLE access-node preference."""
    value = str(entry.options.get(CONF_BT_ACCESS_NODE_PREFERENCE) or BT_ACCESS_NODE_AUTO)
    if value not in (BT_ACCESS_NODE_AUTO, BT_ACCESS_NODE_PREFER_GATEWAY):
        return BT_ACCESS_NODE_AUTO
    return value


def _async_remember_ble_access_node(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    source: str | None,
    access_node: str | None,
    rssi: int | None = None,
    better_candidate_seen: bool | None = None,
) -> None:
    """Persist learned BLE access-node hints for future sessions."""
    if not access_node:
        return
    normalized_node = str(access_node).upper()
    normalized_source = str(source).upper() if source else None
    data = dict(entry.data)
    changed = False

    if normalized_source and data.get(CONF_BT_SOURCE) != normalized_source:
        data[CONF_BT_SOURCE] = normalized_source
        changed = True
    if data.get(CONF_BT_ACCESS_NODE) != normalized_node:
        data[CONF_BT_ACCESS_NODE] = normalized_node
        changed = True

    if better_candidate_seen is not None and bool(data.get(CONF_BT_BETTER_CANDIDATE_SEEN)) != bool(better_candidate_seen):
        data[CONF_BT_BETTER_CANDIDATE_SEEN] = bool(better_candidate_seen)
        changed = True

    for stale_key in ("bt_response_access_node", "bt_access_nodes"):
        if stale_key in data:
            data.pop(stale_key, None)
            changed = True

    if not changed:
        return
    hass.config_entries.async_update_entry(entry, data=data)
    LOGGER.info(
        "%sPersisted Pixie BLE access-node hint access_node=%s source=%s rssi=%s better_candidate_seen=%s",
        _entry_log_prefix(entry),
        normalized_node,
        normalized_source,
        rssi,
        better_candidate_seen,
    )


async def _async_update_entry_runtime_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cloud_params: CloudParams,
    *,
    inventory_mode: str,
    username: str,
    password: str,
    gateway_ip_required: bool,
    gateway_ip: str | None,
    inventory_fallback_reason: str | None = None,
) -> None:
    data = dict(entry.data)
    data.update(
        {
            CONF_HOME_ID: cloud_params.home_id,
            CONF_HOME_NAME: cloud_params.home_name,
            CONF_USER_ID: cloud_params.user_id,
            CONF_MESHNET: cloud_params.meshnet,
            CONF_MESHNET2: cloud_params.meshnet2,
            CONF_NETID: cloud_params.netid,
            CONF_INVENTORY_MODE: inventory_mode,
            CONF_GATEWAY_IP_REQUIRED: gateway_ip_required,
        }
    )
    if gateway_ip:
        data[CONF_GATEWAY_IP] = gateway_ip
    else:
        data.pop(CONF_GATEWAY_IP, None)
    if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
        data[CONF_PIXIE_USERNAME] = username
        data[CONF_PIXIE_PASSWORD] = password
        if inventory_fallback_reason:
            data[CONF_INVENTORY_FALLBACK_REASON] = inventory_fallback_reason
        if inventory_fallback_reason == INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED:
            _async_create_local_inventory_fallback_issue(hass, entry)
        else:
            _async_delete_local_inventory_fallback_issue(hass, entry)
    else:
        data.pop(CONF_PIXIE_USERNAME, None)
        data.pop(CONF_PIXIE_PASSWORD, None)
        data.pop(CONF_INVENTORY_FALLBACK_REASON, None)
        _async_delete_local_inventory_fallback_issue(hass, entry)
    hass.config_entries.async_update_entry(entry, data=data)
    LOGGER.debug(
        "%sUpdated Pixie config entry %s for inventory mode %s%s",
        _entry_log_prefix(entry),
        entry.entry_id,
        inventory_mode,
        " with stored credentials" if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK else " without stored credentials",
    )


def _credentials_issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_ID_MISSING_FALLBACK_CREDENTIALS}_{entry.entry_id}"


def _async_create_missing_credentials_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        _credentials_issue_id(entry),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_ID_MISSING_FALLBACK_CREDENTIALS,
        translation_placeholders={
            "entry_title": entry.title or INTEGRATION_TITLE,
        },
    )


def _async_delete_missing_credentials_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _credentials_issue_id(entry))


def _local_inventory_fallback_issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_ID_LOCAL_INVENTORY_FALLBACK}_{entry.entry_id}"


def _async_create_local_inventory_fallback_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    reason = _entry_inventory_fallback_reason(entry)
    ir.async_create_issue(
        hass,
        DOMAIN,
        _local_inventory_fallback_issue_id(entry),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ID_LOCAL_INVENTORY_FALLBACK,
        translation_placeholders={
            "entry_title": entry.title or INTEGRATION_TITLE,
            "reason": _inventory_fallback_reason_text(reason),
        },
    )


def _async_delete_local_inventory_fallback_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _local_inventory_fallback_issue_id(entry))


def _gateway_ip_issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_ID_GATEWAY_IP_REQUIRED}_{entry.entry_id}"


def _async_create_gateway_ip_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        _gateway_ip_issue_id(entry),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_ID_GATEWAY_IP_REQUIRED,
        translation_placeholders={
            "entry_title": entry.title or INTEGRATION_TITLE,
        },
    )


def _async_delete_gateway_ip_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _gateway_ip_issue_id(entry))


def _bt_proxy_issue_id(entry: ConfigEntry) -> str:
    return f"{ISSUE_ID_BT_PROXY_UNAVAILABLE}_{entry.entry_id}"


def _async_create_bt_proxy_issue(hass: HomeAssistant, entry: ConfigEntry, *, error: str | None = None) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        _bt_proxy_issue_id(entry),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ID_BT_PROXY_UNAVAILABLE,
        translation_placeholders={
            "entry_title": entry.title or INTEGRATION_TITLE,
            "error": error or "No working ESPHome Bluetooth proxy was found.",
        },
    )


def _async_delete_bt_proxy_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _bt_proxy_issue_id(entry))


def _handler_cloud_params(handler: PixieAuthHandler, fallback: CloudParams) -> CloudParams:
    return CloudParams(
        home_id=str(handler.home_id or fallback.home_id),
        home_name=str(handler.home_name or fallback.home_name),
        user_id=str(handler.user_id or fallback.user_id),
        meshnet=str(handler.meshnet or fallback.meshnet),
        meshnet2=str(handler.meshnet2 or fallback.meshnet2),
        netid=str(handler.netid_seed or fallback.netid),
    )


@dataclass(frozen=True)
class PixieEndpoint:
    """Represents one Home Assistant entity endpoint."""

    device_id: int
    endpoint_key: str
    command_target: str
    entity_unique_id: str
    device_identifier: str
    device_name: str | None
    via_device_identifier: str | None
    entity_name: str | None = None
    entity_translation_key: str | None = None
    device_translation_key: str | None = None


def gateway_device_identifier(inventory: PixieInventory) -> str:
    """Return the stable gateway device identifier."""
    gateway = inventory.gateway
    if gateway is not None:
        if gateway.gateway_id:
            return f"gateway:{gateway.gateway_id}"
        if gateway.gateway_mac:
            return f"gateway:{gateway.gateway_mac}"
    return f"gateway:home:{inventory.home_id}"


def physical_device_identifier(record: DeviceRecord) -> str:
    """Return the stable identifier for one physical device."""
    if record.mac:
        return f"device:{record.mac}"
    return f"device:id:{record.id}"


def child_device_identifier(record: DeviceRecord, endpoint_key: str) -> str:
    """Return the stable identifier for one child endpoint device."""
    return f"{physical_device_identifier(record)}:{endpoint_key}"


def endpoint_unique_identifier(record: DeviceRecord, endpoint_key: str) -> str:
    """Return the stable unique identifier for one entity endpoint."""
    if endpoint_key == "main":
        return physical_device_identifier(record)
    return child_device_identifier(record, endpoint_key)


async def async_register_device_topology(
    hass: HomeAssistant,
    entry: ConfigEntry,
    inventory: PixieInventory | None,
    *,
    domain: str,
) -> None:
    """Register the gateway and physical devices in the device registry."""
    if inventory is None:
        return

    device_registry = dr.async_get(hass)
    gateway_identifier = gateway_device_identifier(inventory)
    gateway = inventory.gateway
    gateway_kwargs = {
        "config_entry_id": entry.entry_id,
        "identifiers": {(domain, gateway_identifier)},
        "manufacturer": MANUFACTURER,
        "name": gateway.model_name or "Pixie Gateway" if gateway else "Pixie Gateway",
        "model": gateway.model_name if gateway else "Pixie Gateway",
        "model_id": gateway.model_no if gateway else None,
    }
    device_registry.async_get_or_create(**gateway_kwargs)

    for record in inventory.devices_by_id.values():
        if record.capabilities.is_gateway:
            continue

        kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {(domain, physical_device_identifier(record))},
            "manufacturer": MANUFACTURER,
            "name": record.name,
            "model": hardware_list.get(record.model_no, record.model_no),
            "model_id": record.model_no,
            "via_device": (domain, gateway_identifier),
        }
        if version_text := _firmware_version_text(record.version):
            kwargs["sw_version"] = version_text
        device_registry.async_get_or_create(**kwargs)


class PixiePlusCoordinatorEntity(CoordinatorEntity[PixieInventory]):
    """Shared base entity for Pixie Plus Local platforms."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime_data, endpoint: PixieEndpoint, *, domain: str) -> None:
        """Initialize the shared base entity."""
        super().__init__(runtime_data.coordinator)
        self.runtime_data = runtime_data
        self.endpoint = endpoint
        self.domain = domain
        self._attr_unique_id = endpoint.entity_unique_id
        self._attr_name = endpoint.entity_name
        self._attr_translation_key = endpoint.entity_translation_key

    @property
    def record(self) -> DeviceRecord:
        """Return the live device record from the shared inventory."""
        return self.coordinator.data.devices_by_id[self.endpoint.device_id]

    @property
    def available(self) -> bool:
        """Return whether the entity is currently available."""
        if not self.runtime_data.is_any_runtime_healthy():
            return False
        return self.record.runtime.presence == "online"

    @property
    def device_info(self):
        """Return the device registry info for this entity's device."""
        record = self.record
        info = {
            "identifiers": {(self.domain, self.endpoint.device_identifier)},
            "manufacturer": MANUFACTURER,
            "model": hardware_list.get(record.model_no, record.model_no),
            "model_id": record.model_no,
        }
        if self.endpoint.device_name is not None:
            info["name"] = self.endpoint.device_name
        if self.endpoint.device_translation_key is not None:
            info["translation_key"] = self.endpoint.device_translation_key
        if self.endpoint.via_device_identifier is not None:
            info["via_device"] = (self.domain, self.endpoint.via_device_identifier)
        if version_text := _firmware_version_text(record.version):
            info["sw_version"] = version_text
        return info


class PixiePlusRuntimeCoordinator(DataUpdateCoordinator[PixieInventory]):
    """Expose the in-memory Pixie runtime inventory to HA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        pixie_runtime: PixieRuntimeData,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=COORDINATOR_UPDATE_INTERVAL,
            always_update=True,
        )
        self.pixie_runtime = pixie_runtime
        self.runtime_manager: PixiePlusConfigEntryRuntimeData | None = None

    async def _async_update_data(self) -> PixieInventory:
        """Return the current runtime inventory snapshot.

        Also triggers timer countdown polls for active timer devices
        that haven't been polled recently.
        """
        if self.runtime_manager is not None:
            try:
                await self.runtime_manager.async_ensure_runtime(self.hass, reason="coordinator_refresh")
            except Exception as err:
                await self.runtime_manager.async_ensure_ble_runtime()
                if not self.runtime_manager.is_any_runtime_healthy():
                    raise UpdateFailed(f"Pixie runtime unavailable: {err}") from err
                LOGGER.debug(
                    "%sPixie TCP runtime unavailable during refresh, continuing with BLE health: %s",
                    self.runtime_manager._log_prefix,
                    err,
                )
            else:
                await self.runtime_manager.async_ensure_ble_runtime()

        inventory = self.pixie_runtime.inventory
        if inventory is None:
            raise UpdateFailed("Pixie runtime inventory is not initialized")

        runtime_session = self.pixie_runtime.runtime_session
        if runtime_session is not None and not runtime_session.is_alive() and runtime_session.error is not None:
            raise UpdateFailed(f"Pixie gateway runtime stopped: {runtime_session.error}") from runtime_session.error

        # ── Timer countdown polling ──
        # For every timer device that is active (mode=timer + light on),
        # send an f96b69 poll if it has been more than 30 seconds since
        # the last poll. The d36969 response updates timer_remaining_seconds
        # via the normal bleData path.
        if self.runtime_manager is not None:
            import time as _time
            now = _time.time()
            for device_id in sorted(inventory.devices_by_id):
                rec = inventory.devices_by_id[device_id]
                if not rec.capabilities.supports_timer:
                    continue
                if rec.runtime.mode != 1 or not rec.runtime.is_on:
                    continue
                last_poll_markers = [
                    value
                    for value in (
                        rec.runtime.last_timer_poll_at,
                        rec.runtime.last_timer_poll_requested_at,
                    )
                    if isinstance(value, (int, float))
                ]
                last_poll = max(last_poll_markers) if last_poll_markers else None
                if last_poll is not None and (now - last_poll) < TIMER_POLL_INTERVAL_SECONDS:
                    continue
                # Fire-and-forget — don't block the coordinator update
                self.hass.async_create_task(
                    self.runtime_manager.async_send_local_command(
                        self.hass,
                        command_device_id=device_id,
                        command_timer_action="poll",
                    )
                )
                LOGGER.debug("%sQueued timer poll for device %s", self.runtime_manager._log_prefix, device_id)

        return inventory


@dataclass
class PixiePlusConfigEntryRuntimeData:
    """Objects stored in ConfigEntry.runtime_data."""

    handler: PixieAuthHandler
    cloud_params: CloudParams
    pixie_runtime: PixieRuntimeData
    coordinator: PixiePlusRuntimeCoordinator
    entry: ConfigEntry
    ble_runtime: PixieBluetoothRuntime | None = None
    restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_contact_resets: dict[int, asyncio.Handle] = field(default_factory=dict)
    last_persisted_inventory_signature: str | None = None
    pending_inventory_snapshot: PixieInventory | None = None
    pending_inventory_snapshot_signature: str | None = None
    pending_inventory_snapshot_handle: asyncio.Handle | None = None
    connection_state_listeners: list[Callable[[], None]] = field(default_factory=list)
    esphome_proxy_entry_id: str | None = None
    esphome_proxy_unsub: Callable[[], None] | None = None

    @property
    def _log_prefix(self) -> str:
        home_name = str(self.cloud_params.home_name or self.entry.data.get(CONF_HOME_NAME) or self.entry.title or "").strip()
        if home_name and home_name not in ("unknown", "None"):
            return f"[{home_name}] "
        return ""

    @staticmethod
    def _describe_runtime_session(runtime_session) -> str:
        """Return a compact runtime-session status string for logs."""
        if runtime_session is None:
            return "missing"

        summary = runtime_session.health_summary()
        parts = [
            f"alive={summary['alive']}",
            f"primed={summary['primed']}",
            f"closed={summary['connection_closed']}",
            f"hb_failures={summary['consecutive_heartbeat_failures']}",
        ]
        if summary["error"]:
            parts.append(f"error={summary['error']}")
        return ", ".join(parts)

    def async_add_connection_state_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a listener for LAN/BLE connection state changes."""
        self.connection_state_listeners.append(listener)

        def _remove_listener() -> None:
            with suppress(ValueError):
                self.connection_state_listeners.remove(listener)

        return _remove_listener

    def _notify_connection_state_listeners(self) -> None:
        """Notify gateway connection-state sensor entities."""
        for listener in tuple(self.connection_state_listeners):
            try:
                listener()
            except Exception:
                LOGGER.debug("%sConnection state listener failed", self._log_prefix, exc_info=True)

    def push_connection_state_update_from_thread(self) -> None:
        """Push a LAN connection-state update from the TCP worker thread."""
        self.coordinator.hass.loop.call_soon_threadsafe(self._notify_connection_state_listeners)

    def push_connection_state_update_from_loop(self) -> None:
        """Push a connection-state update already running in HA's event loop."""
        self._notify_connection_state_listeners()

    def _attach_runtime_session_health_callback(self) -> None:
        """Wire the current LAN runtime session into the gateway status sensors."""
        runtime_session = self.pixie_runtime.runtime_session
        if runtime_session is not None:
            runtime_session.health_update_callback = self.push_connection_state_update_from_thread

    def _clear_esphome_proxy_monitor(self) -> None:
        """Remove any active ESPHome proxy availability monitor."""
        if self.esphome_proxy_unsub is not None:
            self.esphome_proxy_unsub()
            self.esphome_proxy_unsub = None
        self.esphome_proxy_entry_id = None

    def _handle_ble_access_node_update(
        self,
        hass: HomeAssistant,
        *,
        source: str | None,
        access_node: str | None,
        proxy_entry_id: str | None = None,
        proxy_title: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Persist BLE access-node hints and monitor the selected ESPHome proxy."""
        _async_remember_ble_access_node(
            hass,
            self.entry,
            source=source,
            access_node=access_node,
            **kwargs,
        )
        if proxy_entry_id:
            self._watch_esphome_proxy_entry(hass, proxy_entry_id, proxy_title=proxy_title, source=source)

    def _watch_esphome_proxy_entry(
        self,
        hass: HomeAssistant,
        proxy_entry_id: str,
        *,
        proxy_title: str | None,
        source: str | None,
    ) -> None:
        """Track HA ESPHome API availability for the proxy currently used by BLE."""
        if self.esphome_proxy_entry_id == proxy_entry_id and self.esphome_proxy_unsub is not None:
            return
        self._clear_esphome_proxy_monitor()
        proxy_entry = hass.config_entries.async_get_entry(proxy_entry_id)
        entry_data = getattr(proxy_entry, "runtime_data", None) if proxy_entry is not None else None
        callbacks = getattr(entry_data, "device_update_subscriptions", None)
        if entry_data is None or callbacks is None:
            LOGGER.debug(
                "%sPixie BLE could not monitor ESPHome proxy availability entry_id=%s title=%s source=%s",
                self._log_prefix,
                proxy_entry_id,
                proxy_title,
                source,
            )
            return

        self.esphome_proxy_entry_id = proxy_entry_id

        def _proxy_availability_changed() -> None:
            self._handle_esphome_proxy_availability(entry_data, proxy_title=proxy_title, source=source)

        callbacks.add(_proxy_availability_changed)

        def _unsubscribe() -> None:
            with suppress(KeyError):
                callbacks.remove(_proxy_availability_changed)

        self.esphome_proxy_unsub = _unsubscribe
        LOGGER.debug(
            "%sMonitoring ESPHome Bluetooth proxy availability title=%s source=%s entry_id=%s available=%s",
            self._log_prefix,
            proxy_title,
            source,
            proxy_entry_id,
            bool(getattr(entry_data, "available", False)),
        )
        _proxy_availability_changed()

    def _handle_esphome_proxy_availability(
        self,
        entry_data: Any,
        *,
        proxy_title: str | None,
        source: str | None,
    ) -> None:
        """React to HA ESPHome API availability changes for the active BLE proxy."""
        ble_runtime = self.ble_runtime
        if ble_runtime is None or not _entry_bt_enabled(self.entry):
            return
        available = bool(getattr(entry_data, "available", False))
        if not available:
            reason = f"ESPHome Bluetooth proxy unavailable: {proxy_title or source or 'unknown proxy'}"
            if ble_runtime.health.state == BT_STATE_UNAVAILABLE and ble_runtime.health.last_error == reason:
                return
            LOGGER.warning("%sPixie %s", self._log_prefix, reason)
            ble_runtime.mark_proxy_unavailable(reason)
            return
        LOGGER.debug(
            "%sESPHome Bluetooth proxy available title=%s source=%s ble_state=%s",
            self._log_prefix,
            proxy_title,
            source,
            ble_runtime.health.state,
        )
        if ble_runtime.health.state != BT_STATE_READY:
            ble_runtime.health.state = BT_STATE_UNAVAILABLE
            ble_runtime.health.last_error = "ESPHome Bluetooth proxy available; reconnecting"
            self.push_connection_state_update_from_loop()
            ble_runtime.request_reconnect("ESPHome Bluetooth proxy available; reconnecting")

    def _queue_inventory_snapshot_save(self, inventory: PixieInventory, *, reason: str) -> None:
        """Queue a debounced persistent snapshot save if persistent state changed."""
        signature = _inventory_persistent_signature(inventory)
        if signature is None:
            return
        if signature == self.last_persisted_inventory_signature:
            if self.pending_inventory_snapshot_handle is not None:
                self.pending_inventory_snapshot_handle.cancel()
                self.pending_inventory_snapshot_handle = None
                self.pending_inventory_snapshot = None
                self.pending_inventory_snapshot_signature = None
            LOGGER.debug(
                "%sSkipped Pixie inventory snapshot save for entry %s: only non-persistent runtime metadata changed (%s)",
                self._log_prefix,
                self.entry.entry_id,
                reason,
            )
            return

        self.pending_inventory_snapshot = inventory
        self.pending_inventory_snapshot_signature = signature
        if self.pending_inventory_snapshot_handle is not None and not self.pending_inventory_snapshot_handle.cancelled():
            return

        loop = self.coordinator.hass.loop
        self.pending_inventory_snapshot_handle = loop.call_later(
            INVENTORY_SNAPSHOT_SAVE_DEBOUNCE_SECONDS,
            lambda: self.coordinator.hass.async_create_task(
                self._async_flush_inventory_snapshot_save(reason=reason)
            ),
        )

    async def _async_flush_inventory_snapshot_save(self, *, reason: str = "flush") -> None:
        """Persist the latest pending debounced inventory snapshot."""
        handle = self.pending_inventory_snapshot_handle
        self.pending_inventory_snapshot_handle = None
        if handle is not None:
            handle.cancel()

        inventory = self.pending_inventory_snapshot
        signature = self.pending_inventory_snapshot_signature
        self.pending_inventory_snapshot = None
        self.pending_inventory_snapshot_signature = None
        if inventory is None or signature is None:
            return

        await _async_save_inventory_snapshot(self.coordinator.hass, self.entry, inventory)
        self.last_persisted_inventory_signature = signature
        LOGGER.debug(
            "%sPersisted debounced Pixie inventory snapshot for entry %s (%s)",
            self._log_prefix,
            self.entry.entry_id,
            reason,
        )

    def push_inventory_update_from_thread(self, inventory: PixieInventory) -> None:
        """Push a runtime inventory update to HA from the TCP worker thread."""
        self.pixie_runtime.inventory = inventory
        self._attach_runtime_session_health_callback()
        self.coordinator.hass.loop.call_soon_threadsafe(
            self.coordinator.async_set_updated_data,
            inventory,
        )
        self.coordinator.hass.loop.call_soon_threadsafe(
            partial(self._queue_inventory_snapshot_save, inventory, reason="TCP runtime update")
        )
        # If a timer device needs an immediate poll (external mode change or
        # turn-on), schedule it now instead of waiting for the next coordinator
        # cycle (which can be up to 10 s away).
        for device_id in sorted(inventory.devices_by_id):
            rec = inventory.devices_by_id[device_id]
            if rec.capabilities.supports_contact_sensor and rec.runtime.contact_momentary and rec.runtime.contact_active:
                self.coordinator.hass.loop.call_soon_threadsafe(
                    self._schedule_contact_reset,
                    device_id,
                )
        self._schedule_pending_timer_polls(inventory, from_thread=True, reason="external change")

    def push_inventory_update_from_loop(self, inventory: PixieInventory) -> None:
        """Push a runtime inventory update already running in HA's event loop."""
        self.pixie_runtime.inventory = inventory
        self._attach_runtime_session_health_callback()
        self.coordinator.async_set_updated_data(inventory)
        self._queue_inventory_snapshot_save(inventory, reason="BLE runtime update")
        self._schedule_pending_timer_polls(inventory, from_thread=False, reason="BLE update")

    async def async_apply_ble_firmware_advertisements(
        self,
        adverts: list[PixieFirmwareAdvertisement],
        *,
        reason: str,
    ) -> int:
        """Apply BLE-advertised firmware versions to this entry's inventory by MAC."""
        inventory = self.pixie_runtime.inventory
        if inventory is None:
            return 0
        changed_records: list[DeviceRecord] = []
        records_by_mac = {
            PixieInventory._normalize_mac(record.mac): record
            for record in inventory.devices_by_id.values()
            if PixieInventory._normalize_mac(record.mac)
        }
        for advert in adverts:
            normalized_advert_mac = PixieInventory._normalize_mac(advert.mac)
            current_record = records_by_mac.get(normalized_advert_mac)
            old_version = current_record.version if current_record is not None else None
            changed = inventory.apply_ble_advertised_version(advert.mac, advert.version)
            if changed is None:
                if current_record is None:
                    LOGGER.debug(
                        "%sPixie BLE firmware advertisement ignored for unknown device mac=%s version=%s model=%s id=%s (%s)",
                        self._log_prefix,
                        advert.mac,
                        advert.version,
                        advert.model_no,
                        advert.device_id,
                        reason,
                    )
                    continue
                LOGGER.debug(
                    "%sPixie BLE firmware advertisement unchanged: %s mac=%s version=%s model=%s id=%s (%s)",
                    self._log_prefix,
                    current_record.name,
                    advert.mac,
                    advert.version,
                    advert.model_no,
                    advert.device_id,
                    reason,
                )
                continue
            changed_records.append(changed)
            LOGGER.info(
                "%sPixie BLE firmware version updated: %s mac=%s version=%s->%s source=ble_advertisement (%s)",
                self._log_prefix,
                changed.name,
                changed.mac,
                _firmware_version_text(old_version) or old_version,
                _firmware_version_text(changed.version) or changed.version,
                reason,
            )

        if not changed_records:
            return 0

        self.coordinator.async_set_updated_data(inventory)
        device_registry = dr.async_get(self.coordinator.hass)
        for record in changed_records:
            device = device_registry.async_get_device({(DOMAIN, physical_device_identifier(record))})
            if device is not None and (version_text := _firmware_version_text(record.version)):
                device_registry.async_update_device(device.id, sw_version=version_text)
        await async_register_device_topology(self.coordinator.hass, self.entry, inventory, domain=DOMAIN)
        self._queue_inventory_snapshot_save(inventory, reason=f"BLE firmware version update: {reason}")
        return len(changed_records)

    def _schedule_pending_timer_polls(self, inventory: PixieInventory, *, from_thread: bool, reason: str) -> None:
        """Schedule immediate timer polls requested by newly applied runtime state."""
        for device_id in sorted(inventory.devices_by_id):
            rec = inventory.devices_by_id[device_id]
            if rec.capabilities.supports_timer and rec.runtime.timer_needs_poll:
                rec.runtime.timer_needs_poll = False
                command_coro = self.async_send_local_command(
                    self.coordinator.hass,
                    command_device_id=device_id,
                    command_timer_action="poll",
                )
                if from_thread:
                    self.coordinator.hass.loop.call_soon_threadsafe(
                        self.coordinator.hass.async_create_task,
                        command_coro,
                    )
                else:
                    self.coordinator.hass.async_create_task(command_coro)
                LOGGER.debug("%sImmediate timer poll for device %s (%s)", self._log_prefix, device_id, reason)

    def is_tcp_runtime_healthy(self) -> bool:
        """Return True when the TCP runtime session is currently usable."""
        runtime_session = self.pixie_runtime.runtime_session
        return (
            runtime_session is not None
            and runtime_session.is_alive()
            and not runtime_session.needs_restart()
            and runtime_session.connection_closed_at is None
        )

    def is_tcp_runtime_known_unavailable(self) -> bool:
        """Return True when the current TCP runtime is already known unusable."""
        runtime_session = self.pixie_runtime.runtime_session
        return (
            runtime_session is not None
            and (
                not runtime_session.is_alive()
                or runtime_session.needs_restart()
                or runtime_session.connection_closed_at is not None
                or runtime_session.error is not None
            )
        )

    def is_ble_runtime_healthy(self) -> bool:
        """Return True when the optional BLE runtime is currently usable."""
        return self.ble_runtime is not None and self.ble_runtime.health.healthy

    def is_any_runtime_healthy(self) -> bool:
        """Return True if at least one enabled runtime path is healthy."""
        if self.is_tcp_runtime_healthy():
            return True
        return self.is_ble_runtime_healthy()

    async def async_ensure_ble_runtime(self) -> PixieBluetoothRuntime | None:
        """Ensure the optional BLE runtime task is started when configured."""
        if self.ble_runtime is None or not _entry_bt_enabled(self.entry):
            return None
        await self.ble_runtime.async_start()
        return self.ble_runtime

    async def async_wait_for_ble_runtime_ready(self, timeout: float = BLE_COMMAND_READY_TIMEOUT) -> PixieBluetoothRuntime | None:
        """Start BLE if configured and wait briefly for a usable command session."""
        runtime = await self.async_ensure_ble_runtime()
        if runtime is None:
            return None
        if not runtime.health.healthy:
            LOGGER.debug("%sWaiting up to %.1fs for Pixie BLE runtime to become command-ready", self._log_prefix, timeout)
        deadline = self.coordinator.hass.loop.time() + timeout
        while self.coordinator.hass.loop.time() < deadline:
            if runtime.health.healthy:
                LOGGER.debug(
                    "%sPixie BLE runtime became command-ready source=%s access_node=%s",
                    self._log_prefix,
                    runtime.health.source,
                    runtime.health.access_node,
                )
                return runtime
            await asyncio.sleep(0.25)
        if runtime.health.healthy:
            return runtime
        return None

    def _apply_ble_command_optimistic_update(self, command_kwargs: dict) -> bool:
        """Apply the same transport-neutral optimistic update used by TCP."""
        intent = self.handler.resolve_optimistic_update_intent(command_kwargs)
        if intent is None:
            LOGGER.debug("%sOptimistic update skipped: unsupported command kwargs=%s", self._log_prefix, command_kwargs)
            return False
        applied = self.handler.apply_optimistic_update_intent(intent)
        if applied:
            self.pixie_runtime.inventory = self.handler.inventory
        return applied

    async def _async_send_ble_command(self, command_kwargs: dict, *, wait_for_ready: bool) -> None:
        """Send one command over BLE and apply the shared optimistic update."""
        ble_runtime = self.ble_runtime if self.ble_runtime is not None and self.ble_runtime.health.healthy else None
        if (
            ble_runtime is None
            and wait_for_ready
            and self.ble_runtime is not None
            and self.ble_runtime.health.state in (BT_STATE_UNAVAILABLE, BT_STATE_NO_WORKING_PROXY)
            and self.ble_runtime.health.last_error
        ):
            raise ConfigEntryError(
                "Pixie BLE command transport is not available"
                f": {self.ble_runtime.health.last_error}"
            )
        if ble_runtime is None and wait_for_ready:
            ble_runtime = await self.async_wait_for_ble_runtime_ready()
        if ble_runtime is None or not ble_runtime.health.healthy:
            ble_error = self.ble_runtime.health.last_error if self.ble_runtime is not None else None
            raise ConfigEntryError(
                f"Pixie BLE command transport is not available"
                f"{f': {ble_error}' if ble_error else ''}"
            )
        if not ble_runtime.esphome_api_connected():
            reason = "ESPHome Bluetooth proxy API is not connected"
            ble_runtime.mark_proxy_unavailable(reason)
            raise ConfigEntryError(f"Pixie BLE command transport is not available: {reason}")
        await ble_runtime.async_send_command(command_kwargs)
        self._apply_ble_command_optimistic_update(command_kwargs)
        self.coordinator.async_set_updated_data(self.pixie_runtime.inventory)

    def _schedule_contact_reset(self, device_id: int) -> None:
        """Schedule a short HA-side reset for momentary contact sensor events."""
        existing = self.pending_contact_resets.pop(int(device_id), None)
        if existing is not None:
            existing.cancel()

        def _reset() -> None:
            self.pending_contact_resets.pop(int(device_id), None)
            inventory = self.pixie_runtime.inventory
            if inventory is None:
                return
            runtime = inventory.state_store.apply_device_update(
                inventory.devices_by_id,
                int(device_id),
                source="contact_pulse_reset",
                contact_active=False,
                contact_momentary=False,
            )
            if runtime is None:
                return
            self.coordinator.async_set_updated_data(inventory)

        self.pending_contact_resets[int(device_id)] = self.coordinator.hass.loop.call_later(1.0, _reset)

    async def async_ensure_runtime(self, hass: HomeAssistant, *, reason: str):
        """Ensure there is one healthy live runtime session for this config entry."""
        runtime_session = self.pixie_runtime.runtime_session
        if runtime_session is not None and runtime_session.is_alive() and not runtime_session.needs_restart():
            return runtime_session

        async with self.restart_lock:
            runtime_session = self.pixie_runtime.runtime_session
            if runtime_session is not None and runtime_session.is_alive() and not runtime_session.needs_restart():
                return runtime_session

            if runtime_session is not None:
                LOGGER.warning(
                    "%sRestarting Pixie runtime (%s): %s",
                    self._log_prefix,
                    reason,
                    self._describe_runtime_session(runtime_session),
                )
                await hass.async_add_executor_job(runtime_session.stop_and_join, 5.0)
            else:
                LOGGER.info("%sStarting Pixie runtime (%s)", self._log_prefix, reason)

            restart_handler = PixieAuthHandler()
            restart_handler.inventory = self.pixie_runtime.inventory
            restart_handler.gateway_identity = self.pixie_runtime.inventory.gateway if self.pixie_runtime.inventory else None
            restart_handler.set_inventory_update_callback(self.push_inventory_update_from_thread)

            username = _entry_username(self.entry)
            password = _entry_password(self.entry)
            inventory_mode = _entry_inventory_mode(self.entry)
            gateway_ip = _entry_gateway_ip(self.entry)

            if _entry_gateway_ip_required(self.entry) and gateway_ip is None:
                _async_create_gateway_ip_issue(hass, self.entry)
                raise ConfigEntryError("Pixie gateway requires a stored manual IP address")

            try:
                try:
                    restarted_runtime = await restart_handler.async_bootstrap_gateway(
                        self.cloud_params,
                        username=username,
                        password=password,
                        gateway_ip=gateway_ip,
                        keep_control_alive=True,
                        wait_for_shutdown=False,
                        hydrate_inventory=False,
                    )
                except (PixieGatewayResolutionError, PixieGatewayConnectionError):
                    if not gateway_ip or _entry_gateway_ip_required(self.entry):
                        raise
                    restart_session = restart_handler.runtime_session
                    if restart_session is not None:
                        await hass.async_add_executor_job(restart_session.stop_and_join, 5.0)
                    restart_handler.runtime_session = None
                    LOGGER.warning(
                        "%sStored Pixie gateway IP %s failed during runtime restart; retrying UDP discovery",
                        self._log_prefix,
                        gateway_ip,
                    )
                    restarted_runtime = await restart_handler.async_bootstrap_gateway(
                        self.cloud_params,
                        username=username,
                        password=password,
                        gateway_ip=None,
                        keep_control_alive=True,
                        wait_for_shutdown=False,
                        hydrate_inventory=False,
                    )
            except (PixieGatewayResolutionError, PixieGatewayConnectionError):
                restart_session = restart_handler.runtime_session
                if restart_session is not None:
                    await hass.async_add_executor_job(restart_session.stop_and_join, 5.0)
                _async_create_gateway_ip_issue(hass, self.entry)
                raise
            except Exception:
                restart_session = restart_handler.runtime_session
                if restart_session is not None:
                    await hass.async_add_executor_job(restart_session.stop_and_join, 5.0)
                raise

            if restarted_runtime.runtime_session is None:
                raise ConfigEntryError("Pixie runtime restart completed without a live session")

            self.handler = restart_handler
            self.pixie_runtime.handler = restart_handler
            self.pixie_runtime.runtime_session = restarted_runtime.runtime_session
            self._attach_runtime_session_health_callback()
            self.pixie_runtime.inventory_mode = inventory_mode
            if self.ble_runtime is not None:
                self.ble_runtime.command_builder = restart_handler
            if restarted_runtime.inventory is not None:
                self.pixie_runtime.inventory = restarted_runtime.inventory
                if self.ble_runtime is not None:
                    self.ble_runtime.inventory = restarted_runtime.inventory
            verified_gateway_ip = _handler_gateway_ip(restart_handler)
            if verified_gateway_ip and verified_gateway_ip != _entry_gateway_ip(self.entry):
                data = dict(self.entry.data)
                data[CONF_GATEWAY_IP] = verified_gateway_ip
                hass.config_entries.async_update_entry(self.entry, data=data)
            _async_delete_gateway_ip_issue(hass, self.entry)

            LOGGER.info(
                "%sPixie runtime ready after %s: %s",
                self._log_prefix,
                reason,
                self._describe_runtime_session(self.pixie_runtime.runtime_session),
            )
            return self.pixie_runtime.runtime_session

    async def async_shutdown(self, hass: HomeAssistant) -> None:
        """Stop the long-lived gateway runtime session."""
        async with self.restart_lock:
            self._clear_esphome_proxy_monitor()
            await self._async_flush_inventory_snapshot_save(reason="shutdown")
            for handle in self.pending_contact_resets.values():
                handle.cancel()
            self.pending_contact_resets.clear()
            runtime_session = self.pixie_runtime.runtime_session
            if runtime_session is None:
                if self.ble_runtime is not None:
                    await self.ble_runtime.async_shutdown()
                return

            await hass.async_add_executor_job(runtime_session.stop_and_join, 5.0)
            if self.ble_runtime is not None:
                await self.ble_runtime.async_shutdown()

    async def async_send_command(self, hass: HomeAssistant, **kwargs) -> None:
        """Send a command using the configured transport preference.

        Passes through all kwargs including timer-specific ones:
        - command_timer_action: "restart", "override", "set_duration", "poll"
        - command_timer_duration: int (seconds, 1-86400)
        """
        transport = _entry_command_transport(self.entry)
        if transport in (COMMAND_TRANSPORT_BT_PRIMARY, COMMAND_TRANSPORT_BT_ONLY):
            try:
                await self._async_send_ble_command(dict(kwargs), wait_for_ready=True)
                return
            except Exception as err:
                if transport == COMMAND_TRANSPORT_BT_ONLY:
                    raise
                LOGGER.warning("%sPixie BLE command failed; falling back to TCP: %s", self._log_prefix, err)

        if transport in (COMMAND_TRANSPORT_TCP_PRIMARY, COMMAND_TRANSPORT_TCP_ONLY, COMMAND_TRANSPORT_BT_PRIMARY):
            if transport == COMMAND_TRANSPORT_TCP_PRIMARY and self.is_ble_runtime_healthy() and self.is_tcp_runtime_known_unavailable():
                LOGGER.warning(
                    "%sPixie TCP runtime is unavailable; using BLE fallback without TCP retry: %s",
                    self._log_prefix,
                    self._describe_runtime_session(self.pixie_runtime.runtime_session),
                )
                await self._async_send_ble_command(dict(kwargs), wait_for_ready=False)
                return
            try:
                await self._async_send_tcp_command(hass, **kwargs)
                return
            except Exception as err:
                if transport == COMMAND_TRANSPORT_TCP_ONLY:
                    raise
                if transport == COMMAND_TRANSPORT_TCP_PRIMARY and self.ble_runtime is not None and self.ble_runtime.health.healthy:
                    LOGGER.warning("%sPixie TCP command failed; falling back to BLE: %s", self._log_prefix, err)
                    await self._async_send_ble_command(dict(kwargs), wait_for_ready=False)
                    return
                raise

        raise ConfigEntryError("No Pixie command transport is available")

    async def async_send_local_command(self, hass: HomeAssistant, **kwargs) -> None:
        """Compatibility wrapper for entity code; uses configured transport."""
        await self.async_send_command(hass, **kwargs)

    async def _async_send_tcp_command(self, hass: HomeAssistant, **kwargs) -> None:
        """Send a local command using the single shared 41578 runtime session."""
        runtime_session = await self.async_ensure_runtime(hass, reason="command_send")
        try:
            await hass.async_add_executor_job(runtime_session.send_command, dict(kwargs))
            self.coordinator.async_set_updated_data(self.pixie_runtime.inventory)
            return
        except Exception as err:
            runtime_unhealthy = (
                not runtime_session.is_alive()
                or runtime_session.needs_restart()
                or runtime_session.connection_closed_at is not None
            )
            if runtime_unhealthy:
                LOGGER.warning("%sLive Pixie runtime command failed; restarting shared runtime: %s", self._log_prefix, err)
                recovered_session = await self.async_ensure_runtime(
                    hass,
                    reason="command_send_recovery",
                )
                await hass.async_add_executor_job(recovered_session.send_command, dict(kwargs))
                self.coordinator.async_set_updated_data(self.pixie_runtime.inventory)
                return

            LOGGER.warning("%sLive Pixie runtime command failed on shared runtime: %s", self._log_prefix, err)
            raise


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Pixie Plus Local integration."""
    return True


def _cloud_params_from_entry(entry: ConfigEntry) -> CloudParams:
    """Build bootstrap cloud parameters from persisted config-entry data."""
    missing = [
        key
        for key in (CONF_HOME_ID, CONF_USER_ID, CONF_MESHNET, CONF_MESHNET2, CONF_NETID)
        if not entry.data.get(key)
    ]
    if missing:
        raise ConfigEntryError(
            "Config entry is missing required Pixie runtime fields: " + ", ".join(sorted(missing))
        )

    return CloudParams(
        home_id=str(entry.data[CONF_HOME_ID]),
        home_name=str(entry.data.get(CONF_HOME_NAME) or entry.title or INTEGRATION_TITLE),
        user_id=str(entry.data[CONF_USER_ID]),
        meshnet=str(entry.data[CONF_MESHNET]),
        meshnet2=str(entry.data[CONF_MESHNET2]),
        netid=str(entry.data[CONF_NETID]),
    )


async def _async_build_runtime_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> PixiePlusConfigEntryRuntimeData:
    """Bootstrap the Pixie local runtime and its HA coordinator."""
    cloud_params = _cloud_params_from_entry(entry)
    inventory_mode = _entry_inventory_mode(entry)
    persisted_inventory = await _async_load_inventory_snapshot(hass, entry)
    username = _entry_username(entry)
    password = _entry_password(entry)
    gateway_ip_required = _entry_gateway_ip_required(entry)
    gateway_ip = _entry_gateway_ip(entry)
    fallback_reason = _entry_inventory_fallback_reason(entry)

    if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK and fallback_reason == INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED:
        _async_create_local_inventory_fallback_issue(hass, entry)
    else:
        _async_delete_local_inventory_fallback_issue(hass, entry)

    if gateway_ip_required and gateway_ip is None:
        _async_create_gateway_ip_issue(hass, entry)
        raise ConfigEntryError("Pixie gateway requires a stored manual IP address")

    LOGGER.debug(
        "%sBootstrapping Pixie entry %s in %s mode%s",
        _entry_log_prefix(entry),
        entry.entry_id,
        inventory_mode,
        " with stored inventory snapshot available" if persisted_inventory is not None else " with no stored inventory snapshot",
    )

    handler = PixieAuthHandler()
    coordinator: PixiePlusRuntimeCoordinator | None = None

    async def _shutdown_runtime(current_handler: PixieAuthHandler) -> None:
        runtime_session = current_handler.runtime_session
        if runtime_session is not None:
            await hass.async_add_executor_job(runtime_session.stop_and_join, 5.0)

    async def _async_bootstrap_with_gateway_retry(
        current_handler: PixieAuthHandler,
        current_cloud_params: CloudParams,
        **kwargs,
    ) -> PixieRuntimeData:
        """Try remembered IP first; in auto mode, retry UDP discovery if stale."""
        try:
            return await current_handler.async_bootstrap_gateway(
                current_cloud_params,
                gateway_ip=gateway_ip,
                **kwargs,
            )
        except (PixieGatewayResolutionError, PixieGatewayConnectionError):
            if not gateway_ip or gateway_ip_required:
                raise
            await _shutdown_runtime(current_handler)
            current_handler.runtime_session = None
            LOGGER.warning(
                "%sStored Pixie gateway IP %s failed for entry %s; retrying UDP discovery",
                _entry_log_prefix(entry),
                gateway_ip,
                entry.entry_id,
            )
            return await current_handler.async_bootstrap_gateway(
                current_cloud_params,
                gateway_ip=None,
                **kwargs,
            )

    async def _async_start_snapshot_runtime(
        snapshot_inventory: PixieInventory,
        *,
        runtime_mode: str,
    ) -> tuple[PixieAuthHandler, PixieRuntimeData]:
        snapshot_handler = PixieAuthHandler()
        snapshot_handler.inventory = snapshot_inventory
        snapshot_handler.gateway_identity = snapshot_inventory.gateway
        snapshot_runtime = await _async_bootstrap_with_gateway_retry(
            snapshot_handler,
            cloud_params,
            username="",
            password="",
            keep_control_alive=True,
            wait_for_shutdown=False,
            hydrate_inventory=False,
        )
        snapshot_runtime.inventory = snapshot_inventory
        snapshot_runtime.inventory_mode = runtime_mode
        return snapshot_handler, snapshot_runtime

    async def _async_start_local_inventory_runtime() -> tuple[PixieAuthHandler, PixieRuntimeData]:
        local_handler = PixieAuthHandler()
        local_runtime = await _async_bootstrap_with_gateway_retry(
            local_handler,
            cloud_params,
            username="",
            password="",
            keep_control_alive=True,
            wait_for_shutdown=False,
        )
        local_runtime.inventory_mode = INVENTORY_MODE_LOCAL_53216
        return local_handler, local_runtime

    async def _async_start_cloud_fallback_runtime() -> tuple[PixieAuthHandler, PixieRuntimeData, CloudParams]:
        fallback_handler = PixieAuthHandler()
        refreshed_cloud_params = await fallback_handler.async_fetch_cloud_params(
            username,
            password,
            include_inventory_seed=True,
            selected_home_id=cloud_params.home_id,
        )
        fallback_runtime = await _async_bootstrap_with_gateway_retry(
            fallback_handler,
            refreshed_cloud_params,
            username=username,
            password=password,
            keep_control_alive=True,
            wait_for_shutdown=False,
            hydrate_inventory=False,
        )
        fallback_runtime.inventory_mode = INVENTORY_MODE_CLOUD_FALLBACK
        if fallback_runtime.inventory is None:
            fallback_runtime.inventory = fallback_handler.inventory
        return fallback_handler, fallback_runtime, refreshed_cloud_params

    try:
        if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
            if not (username and password):
                _async_create_missing_credentials_issue(hass, entry)
                raise ConfigEntryError("Pixie cloud-fallback inventory requires stored Pixie credentials")
            LOGGER.debug("%sStarting Pixie entry %s directly in cloud fallback inventory mode", _entry_log_prefix(entry), entry.entry_id)
            handler, pixie_runtime, cloud_params = await _async_start_cloud_fallback_runtime()
            _async_delete_missing_credentials_issue(hass, entry)
        else:
            LOGGER.debug("%sTrying direct local Pixie inventory startup for entry %s", _entry_log_prefix(entry), entry.entry_id)
            handler, pixie_runtime = await _async_start_local_inventory_runtime()

        if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
            cloud_params = _handler_cloud_params(handler, cloud_params)
        elif pixie_runtime.inventory is not None:
            _async_delete_missing_credentials_issue(hass, entry)
            pixie_runtime.inventory_mode = INVENTORY_MODE_LOCAL_53216
        else:
            LOGGER.warning("%sDirect local Pixie inventory startup failed for entry %s", _entry_log_prefix(entry), entry.entry_id)
            await _shutdown_runtime(handler)

            if username and password:
                try:
                    handler, pixie_runtime, cloud_params = await _async_start_cloud_fallback_runtime()
                    _async_delete_missing_credentials_issue(hass, entry)
                    if inventory_mode != INVENTORY_MODE_CLOUD_FALLBACK:
                        LOGGER.warning(
                            "%sPixie direct local inventory failed; switching entry %s to cloud fallback mode",
                            _entry_log_prefix(entry),
                            entry.entry_id,
                        )
                    await _async_update_entry_runtime_data(
                        hass,
                        entry,
                        _handler_cloud_params(handler, cloud_params),
                        inventory_mode=INVENTORY_MODE_CLOUD_FALLBACK,
                        username=username,
                        password=password,
                        gateway_ip_required=gateway_ip_required,
                        gateway_ip=_handler_gateway_ip(handler) or gateway_ip,
                        inventory_fallback_reason=INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED,
                    )
                    cloud_params = _handler_cloud_params(handler, cloud_params)
                except Exception as err:
                    if persisted_inventory is None:
                        raise ConfigEntryNotReady(
                            f"Pixie live inventory unavailable and no stored inventory snapshot exists: {err}"
                        ) from err
                    LOGGER.warning("%sPixie live inventory failed; using stored inventory snapshot: %s", _entry_log_prefix(entry), err)
                    handler, pixie_runtime = await _async_start_snapshot_runtime(
                        persisted_inventory,
                        runtime_mode=inventory_mode,
                    )
            else:
                if persisted_inventory is None:
                    _async_create_missing_credentials_issue(hass, entry)
                    raise ConfigEntryError(
                        "Pixie direct local inventory failed and Pixie credentials are required for cloud fallback"
                    )
                LOGGER.warning(
                    "%sDirect local Pixie inventory failed with no stored Pixie credentials; using stored inventory snapshot",
                    _entry_log_prefix(entry),
                )
                _async_create_missing_credentials_issue(hass, entry)
                handler, pixie_runtime = await _async_start_snapshot_runtime(
                    persisted_inventory,
                    runtime_mode=inventory_mode,
                )

        _async_delete_gateway_ip_issue(hass, entry)
        verified_gateway_ip = _handler_gateway_ip(handler) or gateway_ip
        if verified_gateway_ip and verified_gateway_ip != _entry_gateway_ip(entry):
            await _async_update_entry_runtime_data(
                hass,
                entry,
                cloud_params,
                inventory_mode=pixie_runtime.inventory_mode,
                username=username if pixie_runtime.inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK else "",
                password=password if pixie_runtime.inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK else "",
                gateway_ip_required=gateway_ip_required,
                gateway_ip=verified_gateway_ip,
                inventory_fallback_reason=_entry_inventory_fallback_reason(entry),
            )
        if persisted_inventory is not None and pixie_runtime.inventory is not None:
            preserved_versions = pixie_runtime.inventory.preserve_ble_advertised_versions_from(persisted_inventory)
            if preserved_versions:
                LOGGER.debug(
                    "%sPreserved %s BLE firmware version(s) from stored inventory snapshot",
                    _entry_log_prefix(entry),
                    preserved_versions,
                )
        coordinator = PixiePlusRuntimeCoordinator(hass, entry, pixie_runtime)
        await coordinator.async_config_entry_first_refresh()
        await _async_save_inventory_snapshot(hass, entry, pixie_runtime.inventory)
    except (PixieGatewayResolutionError, PixieGatewayConnectionError) as err:
        await _shutdown_runtime(handler)
        _async_create_gateway_ip_issue(hass, entry)
        raise ConfigEntryError(str(err)) from err
    except PixieAuthError as err:
        await _shutdown_runtime(handler)
        raise ConfigEntryNotReady(str(err)) from err
    except Exception:
        await _shutdown_runtime(handler)
        raise

    runtime_data = PixiePlusConfigEntryRuntimeData(
        handler=handler,
        cloud_params=cloud_params,
        pixie_runtime=pixie_runtime,
        coordinator=coordinator,
        entry=entry,
        ble_runtime=PixieBluetoothRuntime(
            hass=hass,
            cloud_params=cloud_params,
            inventory=pixie_runtime.inventory,
            enabled=_entry_bt_enabled(entry),
            command_builder=handler,
            inventory_update_callback=None,
            preferred_source=str(entry.data.get(CONF_BT_SOURCE) or "") or None,
            preferred_access_node=str(entry.data.get(CONF_BT_ACCESS_NODE) or "") or None,
            access_node_preference=_entry_bt_access_node_preference(entry),
            better_candidate_seen=bool(entry.data.get(CONF_BT_BETTER_CANDIDATE_SEEN)),
        ),
    )
    runtime_data.last_persisted_inventory_signature = _inventory_persistent_signature(pixie_runtime.inventory)
    coordinator.runtime_manager = runtime_data
    if runtime_data.ble_runtime is not None:
        runtime_data.ble_runtime.inventory_update_callback = runtime_data.push_inventory_update_from_loop
        runtime_data.ble_runtime.health_update_callback = runtime_data.push_connection_state_update_from_loop
        runtime_data.ble_runtime.access_node_update_callback = (
            lambda source, access_node, **kwargs: runtime_data._handle_ble_access_node_update(
                hass,
                source=source,
                access_node=access_node,
                **kwargs,
            )
        )
    handler.set_inventory_update_callback(runtime_data.push_inventory_update_from_thread)
    runtime_data._attach_runtime_session_health_callback()
    await runtime_data.async_ensure_ble_runtime()
    if _entry_bt_enabled(entry):
        ble_error = runtime_data.ble_runtime.health.last_error if runtime_data.ble_runtime else None
        if runtime_data.is_ble_runtime_healthy():
            _async_delete_bt_proxy_issue(hass, entry)
        elif ble_error:
            _async_create_bt_proxy_issue(hass, entry, error=ble_error)
        else:
            _async_delete_bt_proxy_issue(hass, entry)
    elif str(entry.data.get(CONF_BT_STATE) or "") == BT_STATE_NO_WORKING_PROXY:
        _async_create_bt_proxy_issue(hass, entry, error="No working ESPHome Bluetooth proxy was found.")
    else:
        _async_delete_bt_proxy_issue(hass, entry)
    return runtime_data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pixie Plus Local from a config entry."""
    runtime_data = await _async_build_runtime_data(hass, entry)
    desired_title = (
        runtime_data.pixie_runtime.inventory.home_name
        if runtime_data.pixie_runtime.inventory and runtime_data.pixie_runtime.inventory.home_name
        else runtime_data.cloud_params.home_name
    ) or INTEGRATION_TITLE
    if entry.title != desired_title:
        hass.config_entries.async_update_entry(entry, title=desired_title)
    entry.runtime_data = runtime_data
    await async_register_device_topology(hass, entry, runtime_data.pixie_runtime.inventory, domain=DOMAIN)
    _async_ensure_ble_firmware_refresh_hooks(hass)

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Clean up entity and device registry entries that are no longer in the
    # live inventory (e.g. devices deleted from the Pixie app).
    if runtime_data.pixie_runtime.inventory is not None:
        inv = runtime_data.pixie_runtime.inventory
        endpoint_keys = (
            "main", "mode", "timer_mode", "restart", "timer_remaining",
            "timer_duration", "hold_time", "brightness_threshold",
            "motion_sensitivity", "refresh_params", "left", "right",
            "usb", "sensor_light_state", "contact_state", "arm",
            "door1", "door2",
        )
        gateway_identifier = gateway_device_identifier(inv)
        valid_entity_ids: set[str] = {
            f"{gateway_identifier}:lan",
            f"{gateway_identifier}:bluetooth",
        }
        valid_device_ids: set[str] = {gateway_identifier}
        for device_id in inv.devices_by_id:
            record = inv.devices_by_id[device_id]
            if record.capabilities.is_gateway:
                continue
            valid_device_ids.add(physical_device_identifier(record))
            for key in endpoint_keys:
                valid_entity_ids.add(endpoint_unique_identifier(record, key))

        # Remove orphaned entities
        ent_reg = er.async_get(hass)
        stale_entities = [
            entity.entity_id
            for entity in ent_reg.entities.values()
            if entity.config_entry_id == entry.entry_id
            and entity.unique_id not in valid_entity_ids
        ]
        for entity_id in stale_entities:
            ent_reg.async_remove(entity_id)
            LOGGER.debug("%sRemoved orphaned entity: %s", _entry_log_prefix(entry), entity_id)

        # Remove orphaned devices
        dev_reg = dr.async_get(hass)
        stale_devices = [
            device.id
            for device in dev_reg.devices.values()
            if entry.entry_id in device.config_entries
            and not any(
                ident in valid_device_ids
                for ident_set in device.identifiers
                for ident in ident_set
            )
        ]
        for device_id in stale_devices:
            dev_reg.async_remove_device(device_id)
            LOGGER.debug("%sRemoved orphaned device: %s", _entry_log_prefix(entry), device_id)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Pixie Plus Local config entry."""
    unload_ok = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if not unload_ok:
        return False

    runtime_data: PixiePlusConfigEntryRuntimeData = entry.runtime_data
    await runtime_data.async_shutdown(hass)
    _async_maybe_remove_ble_firmware_refresh_hooks(hass, entry)
    return True
