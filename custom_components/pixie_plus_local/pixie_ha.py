"""Home Assistant glue for Pixie Plus Local."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
import json
import logging
from typing import Any, Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator, UpdateFailed

from .pixie_ble import (
    BT_STATE_NO_WORKING_PROXY,
    BT_STATE_READY,
    BT_STATE_UNAVAILABLE,
    PixieBluetoothRuntime,
    PixieFirmwareAdvertisement,
    async_scan_pixie_firmware_advertisements,
)
from .pixie_const import (
    BLE_COMMAND_READY_TIMEOUT,
    BLE_FIRMWARE_SCAN_HOUR,
    BLE_FIRMWARE_SCAN_MINUTE,
    BLE_FIRMWARE_SCAN_SECONDS,
    BT_ACCESS_NODE_AUTO,
    BT_ACCESS_NODE_PREFER_GATEWAY,
    COMMAND_TRANSPORT_BT_ONLY,
    COMMAND_TRANSPORT_BT_PRIMARY,
    COMMAND_TRANSPORT_TCP_ONLY,
    COMMAND_TRANSPORT_TCP_PRIMARY,
    CONF_BLE_INVENTORY,
    CONF_BT_ACCESS_NODE,
    CONF_BT_ACCESS_NODE_PREFERENCE,
    CONF_BT_BETTER_CANDIDATE_SEEN,
    CONF_BT_ENABLED,
    CONF_BT_SOURCE,
    CONF_COMMAND_TRANSPORT,
    CONF_GATEWAY_IP,
    CONF_GATEWAY_IP_REQUIRED,
    CONF_HOME_NAME,
    CONF_INVENTORY_FALLBACK_REASON,
    CONF_INVENTORY_MODE,
    CONF_MESHNET,
    CONF_MESHNET2,
    CONF_NETID,
    CONF_PIXIE_PASSWORD,
    CONF_PIXIE_USERNAME,
    CONF_POWER_POLL_INTERVALS,
    CONF_SYNC_HA_DEVICE_NAMES,
    CONF_USER_ID,
    COORDINATOR_UPDATE_INTERVAL,
    DOMAIN,
    INTEGRATION_TITLE,
    INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED,
    INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY,
    INVENTORY_MODE_BLE_ADVERTISEMENT,
    INVENTORY_MODE_CLOUD_FALLBACK,
    INVENTORY_MODE_LOCAL_53216,
    INVENTORY_SNAPSHOT_SAVE_DEBOUNCE_SECONDS,
    INVENTORY_STORE_VERSION,
    ISSUE_ID_BT_PROXY_UNAVAILABLE,
    ISSUE_ID_GATEWAY_IP_REQUIRED,
    ISSUE_ID_LOCAL_INVENTORY_FALLBACK,
    ISSUE_ID_MISSING_FALLBACK_CREDENTIALS,
    MANUFACTURER,
    POWER_POLL_DEFAULT_INTERVAL_SECONDS,
    POWER_POLL_MAX_INTERVAL_SECONDS,
    TIMER_POLL_INTERVAL_SECONDS,
)
from .pixie_inventory import DeviceRecord, PixieInventory, online_value_is_online
from .pixie_provisioning import PixieProvisioningMixin
from .pixie_runtime import (
    CloudParams,
    PixieAuthHandler,
    PixieGatewayConnectionError,
    PixieGatewayResolutionError,
    PixieRuntimeData,
)
from .pixie_value_profiles import get_startup_config_refresh_specs_for_capabilities, hardware_list

LOGGER = logging.getLogger(__name__)

def device_added_signal(entry: ConfigEntry) -> str:
    """Return the dispatcher signal used when a Pixie device is added at runtime."""
    return f"{DOMAIN}_{entry.entry_id}_device_added"


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


def _entry_home_name(entry: ConfigEntry) -> str:
    """Return a readable home name for logs and errors."""
    return str(entry.data.get(CONF_HOME_NAME) or entry.title or INTEGRATION_TITLE)


def inventory_home_id(inventory: PixieInventory | None, cloud_params: CloudParams) -> str:
    """Return the Pixie Home object id for 53216 management payloads."""
    if inventory is not None and inventory.home_id:
        return str(inventory.home_id)
    return str(cloud_params.home_id)


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
    resolved_mode = mode if mode in (
        INVENTORY_MODE_LOCAL_53216,
        INVENTORY_MODE_CLOUD_FALLBACK,
        INVENTORY_MODE_BLE_ADVERTISEMENT,
    ) else INVENTORY_MODE_LOCAL_53216
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


def _inventory_record_by_mac(inventory: PixieInventory | None, mac: str) -> DeviceRecord | None:
    """Return the inventory record with the given normalized MAC."""
    normalized = PixieInventory._normalize_mac(mac)
    if inventory is None or not normalized:
        return None
    for record in inventory.devices_by_id.values():
        if PixieInventory._normalize_mac(record.mac) == normalized:
            return record
    return None


async def _async_inventory_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> PixieInventory | None:
    """Return the best available inventory for a config entry."""
    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, PixiePlusConfigEntryRuntimeData):
        inventory = runtime_data.pixie_runtime.inventory
        if inventory is not None:
            return inventory
    inventory = await _async_load_inventory_snapshot(hass, entry)
    if inventory is not None:
        return inventory
    if isinstance(entry.data.get(CONF_BLE_INVENTORY), dict):
        try:
            return PixieInventory.from_dict(dict(entry.data[CONF_BLE_INVENTORY]))
        except Exception as err:
            LOGGER.debug("%sCould not restore Pixie inventory from entry data: %s", _entry_log_prefix(entry), err)
    return None


async def _async_remove_stale_ble_only_owner_by_mac(
    hass: HomeAssistant,
    entry: ConfigEntry,
    mac: str,
    *,
    new_entry: ConfigEntry,
) -> bool:
    """Remove a stale BLE-only HA/inventory owner for a device now added elsewhere."""
    if _entry_inventory_mode(entry) != INVENTORY_MODE_BLE_ADVERTISEMENT:
        return False
    inventory = await _async_inventory_for_entry(hass, entry)
    record = _inventory_record_by_mac(inventory, mac)
    if inventory is None or record is None:
        return False

    physical_identifier = physical_device_identifier(record)
    removed = inventory.remove_device_by_ha_identifier(physical_identifier)
    if removed is None:
        return False

    ent_reg = er.async_get(hass)
    stale_entities = [
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.config_entry_id == entry.entry_id
        and (
            entity.unique_id == physical_identifier
            or entity.unique_id.startswith(f"{physical_identifier}:")
        )
    ]
    for entity_id in stale_entities:
        ent_reg.async_remove(entity_id)

    dev_reg = dr.async_get(hass)
    stale_devices = [
        device.id
        for device in dev_reg.devices.values()
        if entry.entry_id in device.config_entries
        and any(
            domain == DOMAIN
            and (
                identifier == physical_identifier
                or str(identifier).startswith(f"{physical_identifier}:")
            )
            for domain, identifier in device.identifiers
        )
    ]
    for device_id in stale_devices:
        dev_reg.async_remove_device(device_id)

    data = dict(entry.data)
    data[CONF_BLE_INVENTORY] = inventory.to_dict()
    hass.config_entries.async_update_entry(entry, data=data)
    await _async_save_inventory_snapshot(hass, entry, inventory)

    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, PixiePlusConfigEntryRuntimeData):
        runtime_data.pixie_runtime.inventory = inventory
        runtime_data.coordinator.async_set_updated_data(inventory)
        runtime_data.last_persisted_inventory_signature = _inventory_persistent_signature(inventory)

    LOGGER.info(
        "%sRemoved stale BLE-only Pixie device owner after adding it to %s: id=%s name=%s mac=%s",
        _entry_log_prefix(entry),
        _entry_home_name(new_entry),
        removed.id,
        removed.name,
        removed.mac,
    )
    return True


async def _async_verify_gateway_owner_conflicts_before_add(
    hass: HomeAssistant,
    current_entry: ConfigEntry,
    macs: set[str],
) -> None:
    """Reload stale-looking gateway entries and fail only if a MAC still belongs there."""
    normalized_macs = {PixieInventory._normalize_mac(mac) for mac in macs if PixieInventory._normalize_mac(mac)}
    if not normalized_macs:
        return

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == current_entry.entry_id:
            continue
        if _entry_inventory_mode(entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
            continue
        inventory = await _async_inventory_for_entry(hass, entry)
        conflicts = {
            mac
            for mac in normalized_macs
            if _inventory_record_by_mac(inventory, mac) is not None
        }
        if not conflicts:
            continue

        LOGGER.info(
            "%sPossible stale Pixie gateway ownership conflict for %s; reloading %s before add-device continues",
            _entry_log_prefix(current_entry),
            ", ".join(_format_mac(mac) for mac in sorted(conflicts)),
            _entry_home_name(entry),
        )
        try:
            reload_ok = await hass.config_entries.async_reload(entry.entry_id)
        except Exception as err:
            raise ConfigEntryError(
                f"Could not reload Pixie home {_entry_home_name(entry)} to verify device ownership"
            ) from err
        if reload_ok is False:
            raise ConfigEntryError(f"Could not reload Pixie home {_entry_home_name(entry)} to verify device ownership")

        refreshed_inventory = await _async_inventory_for_entry(hass, entry)
        remaining = {
            mac
            for mac in conflicts
            if _inventory_record_by_mac(refreshed_inventory, mac) is not None
        }
        if remaining:
            raise ConfigEntryError(
                "Pixie device already exists in another Pixie home: "
                + ", ".join(_format_mac(mac) for mac in sorted(remaining))
            )
        LOGGER.info(
            "%sStale Pixie gateway ownership conflict cleared after reloading %s",
            _entry_log_prefix(current_entry),
            _entry_home_name(entry),
        )


async def _async_cleanup_stale_ble_only_owners_after_add(
    hass: HomeAssistant,
    current_entry: ConfigEntry,
    macs: set[str],
) -> None:
    """Remove stale BLE-only ownership for devices successfully added to this entry."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == current_entry.entry_id:
            continue
        if _entry_inventory_mode(entry) != INVENTORY_MODE_BLE_ADVERTISEMENT:
            continue
        for mac in macs:
            await _async_remove_stale_ble_only_owner_by_mac(hass, entry, mac, new_entry=current_entry)


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
    if _entry_inventory_mode(entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
        return True
    value = entry.data.get(CONF_BT_ENABLED)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _entry_sync_ha_device_names(entry: ConfigEntry) -> bool:
    """Return whether HA device-name changes should be written back to Pixie."""
    value = entry.options.get(CONF_SYNC_HA_DEVICE_NAMES)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _entry_command_transport(entry: ConfigEntry) -> str:
    if _entry_inventory_mode(entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
        return COMMAND_TRANSPORT_BT_ONLY
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


def parent_device_identifier(inventory: PixieInventory) -> str | None:
    """Return the parent device identifier, or None for BLE-only homes."""
    if inventory.gateway is None and str(inventory.home_id or "").startswith("pixie_ble:"):
        return None
    return gateway_device_identifier(inventory)


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


def _physical_device_identifier_variants(record: DeviceRecord) -> set[str]:
    """Return current and normalized physical identifiers for one device."""
    identifiers = {physical_device_identifier(record)}
    normalized_mac = PixieInventory._normalize_mac(record.mac)
    if normalized_mac:
        normalized_identifier = "device:" + ":".join(
            normalized_mac[i : i + 2] for i in range(0, 12, 2)
        ).lower()
        identifiers.add(normalized_identifier)
    return identifiers


def _physical_device_identifier_aliases(record: DeviceRecord) -> set[str]:
    """Return registry identifiers that may have represented this physical device."""
    identifiers = _physical_device_identifier_variants(record)
    try:
        identifiers.add(f"device:id:{int(record.id)}")
    except (TypeError, ValueError):
        pass
    return identifiers


def _endpoint_unique_identifier_variants(record: DeviceRecord, endpoint_key: str) -> set[str]:
    """Return current and normalized endpoint identifiers for one device endpoint."""
    if endpoint_key == "main":
        return _physical_device_identifier_variants(record)
    return {
        f"{identifier}:{endpoint_key}"
        for identifier in _physical_device_identifier_variants(record)
    }


def _conf_index_selected(old: list[int] | None, new: list[int]) -> int:
    """Return the Pixie app-style 53216 selected bitmask for changed ConfIndex slots."""
    if not old:
        return 127
    selected = 0
    max_len = max(len(old), len(new))
    for idx in range(max_len):
        old_value = old[idx] if idx < len(old) else None
        new_value = new[idx] if idx < len(new) else None
        if old_value != new_value:
            selected |= 1 << idx
    return selected


def _complete_home_payload(
    payload: dict[str, Any],
    *,
    inventory: PixieInventory,
    cloud_params: CloudParams,
) -> dict[str, Any]:
    """Fill fields omitted by selective 53216 responses before inventory parsing."""
    completed = dict(payload)
    if not completed.get("objectId"):
        completed["objectId"] = inventory.home_id or cloud_params.home_id
    if not completed.get("name"):
        completed["name"] = inventory.home_name or cloud_params.home_name
    if completed.get("netID") is None:
        completed["netID"] = inventory.net_id or cloud_params.netid
    if completed.get("meshNet") is None:
        completed["meshNet"] = inventory.mesh_net or cloud_params.meshnet
    if completed.get("meshNet2") is None:
        completed["meshNet2"] = inventory.mesh_net2 or cloud_params.meshnet2
    if completed.get("Gateway") is None and inventory.gateway is not None:
        completed["Gateway"] = inventory.gateway.gateway_mac
    return completed


def _preserve_runtime_state_by_mac(old_inventory: PixieInventory, new_inventory: PixieInventory) -> None:
    """Preserve runtime state for devices that still represent the same physical MAC."""
    old_by_mac = {
        PixieInventory._normalize_mac(record.mac): record
        for record in old_inventory.devices_by_id.values()
        if PixieInventory._normalize_mac(record.mac)
    }
    for record in new_inventory.devices_by_id.values():
        old_record = old_by_mac.get(PixieInventory._normalize_mac(record.mac))
        if old_record is not None:
            record.runtime = new_inventory.state_store.bind(record.id, old_record.runtime)


async def async_cleanup_orphaned_registry_entries(
    hass: HomeAssistant,
    entry: ConfigEntry,
    inventory: PixieInventory | None,
    *,
    reason: str,
) -> None:
    """Remove HA entities/devices that no longer exist in the current Pixie inventory."""
    if inventory is None:
        return

    endpoint_keys = (
        "main", "mode", "timer_mode", "restart", "timer_remaining",
        "timer_duration", "hold_time", "brightness_threshold",
        "motion_sensitivity", "refresh_params", "left", "right",
        "usb", "sensor_light_state", "contact_state", "arm",
        "door1", "door2", "left_power", "left_energy", "left_current",
        "left_voltage", "right_power", "right_energy", "right_current",
        "right_voltage", "power_poll_interval", "gate_signal_width",
        "door1_open_duration", "door1_close_duration", "door2_open_duration",
        "door2_close_duration", "gate_refresh_settings", "indicator_led_on",
        "indicator_led_off", "indicator_led_refresh_settings",
        "outlet_led_indicator", "outlet_all_device_control", "outlet_child_lock",
        "plug_socket_led_indicator", "plug_usb_led_indicator",
        "plug_all_devices_control", "plug_led_refresh_settings",
        "sensor_led_indicator", "learn_brightness_threshold",
    )
    parent_identifier = parent_device_identifier(inventory)
    valid_entity_ids: set[str] = set()
    valid_device_ids: set[str] = set()
    if parent_identifier is not None:
        valid_entity_ids.update({
            f"{parent_identifier}:lan",
            f"{parent_identifier}:bluetooth",
        })
        valid_device_ids.add(parent_identifier)
    for device_id in inventory.devices_by_id:
        record = inventory.devices_by_id[device_id]
        if record.capabilities.is_gateway:
            continue
        valid_device_ids.update(_physical_device_identifier_variants(record))
        for key in endpoint_keys:
            valid_entity_ids.update(_endpoint_unique_identifier_variants(record, key))

    ent_reg = er.async_get(hass)
    stale_entities = [
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.config_entry_id == entry.entry_id
        and entity.unique_id not in valid_entity_ids
    ]
    for entity_id in stale_entities:
        ent_reg.async_remove(entity_id)
        LOGGER.debug("%sRemoved orphaned entity: %s (%s)", _entry_log_prefix(entry), entity_id, reason)

    dev_reg = dr.async_get(hass)
    canonical_by_alias: dict[str, str] = {}
    for record in inventory.devices_by_id.values():
        if record.capabilities.is_gateway:
            continue
        canonical = physical_device_identifier(record)
        for alias in _physical_device_identifier_aliases(record):
            canonical_by_alias[alias] = canonical

    devices_by_canonical: dict[str, list[dr.DeviceEntry]] = {}
    for device in dev_reg.devices.values():
        if entry.entry_id not in device.config_entries:
            continue
        matched_canonical = None
        for ident_domain, ident_value in device.identifiers:
            if ident_domain != DOMAIN:
                continue
            matched_canonical = canonical_by_alias.get(str(ident_value))
            if matched_canonical is not None:
                break
        if matched_canonical is not None:
            devices_by_canonical.setdefault(matched_canonical, []).append(device)

    duplicate_device_ids: set[str] = set()
    for canonical, devices in devices_by_canonical.items():
        if len(devices) <= 1:
            continue
        preferred = next(
            (
                device
                for device in devices
                if any(domain == DOMAIN and identifier == canonical for domain, identifier in device.identifiers)
            ),
            devices[0],
        )
        for device in devices:
            if device.id == preferred.id:
                continue
            duplicate_device_ids.add(device.id)

    for device_id in duplicate_device_ids:
        duplicate_entities = [
            entity.entity_id
            for entity in ent_reg.entities.values()
            if entity.config_entry_id == entry.entry_id
            and entity.device_id == device_id
        ]
        for entity_id in duplicate_entities:
            ent_reg.async_remove(entity_id)
            LOGGER.debug(
                "%sRemoved duplicate device entity: %s (%s)",
                _entry_log_prefix(entry),
                entity_id,
                reason,
            )
        dev_reg.async_remove_device(device_id)
        LOGGER.debug("%sRemoved duplicate device registry entry: %s (%s)", _entry_log_prefix(entry), device_id, reason)

    stale_devices = [
        device.id
        for device in dev_reg.devices.values()
        if entry.entry_id in device.config_entries
        and device.id not in duplicate_device_ids
        and not any(
            domain == DOMAIN and str(identifier) in valid_device_ids
            for domain, identifier in device.identifiers
        )
    ]
    for device_id in stale_devices:
        dev_reg.async_remove_device(device_id)
        LOGGER.debug("%sRemoved orphaned device: %s (%s)", _entry_log_prefix(entry), device_id, reason)




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
    gateway = inventory.gateway
    ble_only = _entry_inventory_mode(entry) == INVENTORY_MODE_BLE_ADVERTISEMENT
    parent_identifier = None if ble_only else parent_device_identifier(inventory)
    if parent_identifier is not None:
        gateway_kwargs = {
            "config_entry_id": entry.entry_id,
            "identifiers": {(domain, parent_identifier)},
            "manufacturer": MANUFACTURER,
            "name": gateway.model_name or "Pixie Gateway" if gateway else "Pixie Gateway",
            "model": gateway.model_name if gateway else "Pixie Gateway",
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
        }
        if parent_identifier is not None:
            kwargs["via_device"] = (domain, parent_identifier)
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
        self._last_record: DeviceRecord | None = None
        inventory = runtime_data.pixie_runtime.inventory
        if inventory is not None:
            self._last_record = inventory.devices_by_id.get(endpoint.device_id)
        self._attr_unique_id = endpoint.entity_unique_id
        self._attr_name = endpoint.entity_name
        self._attr_translation_key = endpoint.entity_translation_key

    @property
    def record(self) -> DeviceRecord:
        """Return the live device record from the shared inventory."""
        record = self.coordinator.data.devices_by_id.get(self.endpoint.device_id)
        if record is not None:
            self._last_record = record
            return record
        if self._last_record is not None:
            return self._last_record
        raise KeyError(self.endpoint.device_id)

    @property
    def available(self) -> bool:
        """Return whether the entity is currently available."""
        if not self.runtime_data.is_any_runtime_healthy():
            return False
        if self.endpoint.device_id not in self.coordinator.data.devices_by_id:
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
        self.entry = entry
        self.pixie_runtime = pixie_runtime
        self.runtime_manager: PixiePlusConfigEntryRuntimeData | None = None

    async def _async_update_data(self) -> PixieInventory:
        """Return the current runtime inventory snapshot.

        Also triggers timer countdown polls for active timer devices
        that haven't been polled recently.
        """
        inventory = self.pixie_runtime.inventory
        if self.runtime_manager is not None:
            if _entry_inventory_mode(self.entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
                await self.runtime_manager.async_ensure_ble_runtime()
                if not self.runtime_manager.is_any_runtime_healthy():
                    if inventory is not None:
                        return inventory
                    raise UpdateFailed("Pixie BLE runtime unavailable")
            else:
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
            self.runtime_manager.schedule_startup_config_refreshes(inventory)

        return inventory


@dataclass
class PixiePlusConfigEntryRuntimeData(PixieProvisioningMixin):
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
    power_poll_remove: Callable[[], None] | None = None
    power_poll_inflight: set[int] = field(default_factory=set)
    last_power_poll_requested_at: dict[int, float] = field(default_factory=dict)
    config_refresh_requested: set[tuple[int, str]] = field(default_factory=set)
    config_refresh_last_presence: dict[int, str] = field(default_factory=dict)
    ble_runtime_suspend_count: int = 0
    conf_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ha_name_sync_unsub: Callable[[], None] | None = None
    ha_name_sync_inflight: set[int] = field(default_factory=set)
    ha_name_sync_suppressed_device_ids: set[str] = field(default_factory=set)
    ble_external_add_inflight: set[int] = field(default_factory=set)
    ble_external_add_last_attempt: dict[int, float] = field(default_factory=dict)

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

    def async_setup_ha_device_name_sync(self, hass: HomeAssistant) -> None:
        """Listen for opt-in HA device-name changes that should be pushed to Pixie."""
        if self.ha_name_sync_unsub is not None:
            return
        event_type = getattr(dr, "EVENT_DEVICE_REGISTRY_UPDATED", "device_registry_updated")

        @callback
        def _handle_device_registry_update(event: Any) -> None:
            data = getattr(event, "data", {}) or {}
            if data.get("action") not in (None, "update"):
                return
            device_id = data.get("device_id")
            if not device_id:
                return
            device_id = str(device_id)
            if device_id in self.ha_name_sync_suppressed_device_ids:
                self.ha_name_sync_suppressed_device_ids.discard(device_id)
                LOGGER.debug("%sSuppressing HA-to-Pixie name sync for Pixie-driven HA device update", self._log_prefix)
                return
            changes = data.get("changes")
            if not isinstance(changes, dict) or "name_by_user" not in changes:
                return
            hass.async_create_task(self.async_maybe_sync_ha_device_name(device_id))

        self.ha_name_sync_unsub = hass.bus.async_listen(event_type, _handle_device_registry_update)

    def _record_for_ha_device_id(self, ha_device_id: str) -> DeviceRecord | None:
        """Return the Pixie record represented by a HA device registry id."""
        inventory = self.pixie_runtime.inventory
        if inventory is None:
            return None
        device_registry = dr.async_get(self.coordinator.hass)
        device = device_registry.async_get(ha_device_id)
        if device is None:
            return None
        device_config_entry_id = getattr(device, "config_entry_id", None)
        if device_config_entry_id is not None:
            if device_config_entry_id != self.entry.entry_id:
                return None
        elif self.entry.entry_id not in getattr(device, "config_entries", set()):
            return None
        record_by_identifier = {
            physical_device_identifier(record): record
            for record in inventory.devices_by_id.values()
            if not record.capabilities.is_gateway
        }
        for domain, identifier in device.identifiers:
            if domain != DOMAIN:
                continue
            record = record_by_identifier.get(str(identifier))
            if record is not None:
                return record
        return None

    def _ha_user_device_name(self, ha_device_id: str) -> str:
        """Return the user-set HA device name, or an empty string if none is set."""
        device_registry = dr.async_get(self.coordinator.hass)
        device = device_registry.async_get(ha_device_id)
        if device is None:
            return ""
        return str(getattr(device, "name_by_user", None) or "").strip()

    def _suppress_next_ha_name_sync_event(self, ha_device_id: str) -> None:
        """Prevent a Pixie-driven HA registry name update from echoing back to Pixie."""
        self.ha_name_sync_suppressed_device_ids.add(ha_device_id)
        self.coordinator.hass.loop.call_later(
            5.0,
            self.ha_name_sync_suppressed_device_ids.discard,
            ha_device_id,
        )

    def _sync_changed_pixie_device_names_to_ha(
        self,
        old_inventory: PixieInventory,
        new_inventory: PixieInventory,
    ) -> None:
        """Apply app/Pixie device renames from refreshed inventory to HA device names."""
        device_registry = dr.async_get(self.coordinator.hass)
        for device_id, new_record in new_inventory.devices_by_id.items():
            if new_record.capabilities.is_gateway:
                continue
            old_record = old_inventory.devices_by_id.get(device_id)
            if old_record is None or old_record.name == new_record.name:
                continue
            new_name = str(new_record.name or "").strip()
            if not new_name:
                continue
            device = device_registry.async_get_device({(DOMAIN, physical_device_identifier(new_record))})
            if device is None:
                continue
            if str(getattr(device, "name_by_user", None) or "").strip() == new_name:
                continue
            self._suppress_next_ha_name_sync_event(str(device.id))
            device_registry.async_update_device(device.id, name_by_user=new_name)
            LOGGER.info(
                "%sPixie app device name synced to HA id=%s old=%r new=%r",
                self._log_prefix,
                new_record.id,
                old_record.name,
                new_name,
            )

    async def async_maybe_sync_ha_device_name(self, ha_device_id: str) -> None:
        """Push an opt-in HA device rename to the Pixie app through 53216."""
        if not _entry_sync_ha_device_names(self.entry):
            return
        if _entry_inventory_mode(self.entry) != INVENTORY_MODE_LOCAL_53216:
            return

        inventory = self.pixie_runtime.inventory
        if inventory is None or not _entry_gateway_supports_local_inventory_53216(self.entry, inventory):
            return

        record = self._record_for_ha_device_id(ha_device_id)
        if record is None or record.capabilities.is_gateway:
            return

        new_name = self._ha_user_device_name(ha_device_id)
        if not new_name or new_name == record.name:
            return
        if record.id in self.ha_name_sync_inflight:
            LOGGER.debug(
                "%sSkipping HA-to-Pixie name sync for %s because one is already in flight",
                self._log_prefix,
                record.name,
            )
            return

        self.ha_name_sync_inflight.add(record.id)
        try:
            await self.async_sync_ha_device_name_to_pixie(record, new_name)
        finally:
            self.ha_name_sync_inflight.discard(record.id)

    def _device_manager_payload_for_name(self, record: DeviceRecord, new_name: str) -> dict[str, Any]:
        """Build the captured 53216 deviceManager payload with only the name changed."""
        raw_device = dict(record.raw_device or {})
        if not raw_device:
            raw_device = {
                "mac": record.mac,
                "type": record.type,
                "stype": record.stype,
                "id": record.id,
                "version": record.version,
                "online": record.runtime.online,
                "groups": [],
                "rooms": list(record.rooms),
            }
        raw_device["name"] = new_name
        return {
            "func": "deviceManager",
            "data": {
                "homeId": inventory_home_id(self.pixie_runtime.inventory, self.cloud_params),
                "dev": raw_device,
                "flag": 3,
            },
        }

    async def async_sync_ha_device_name_to_pixie(self, record: DeviceRecord, new_name: str) -> None:
        """Write a HA device rename to the Pixie gateway local configuration."""
        hub_ip = _handler_gateway_ip(self.handler) or _entry_gateway_ip(self.entry)
        if not hub_ip:
            LOGGER.warning("%sCould not sync HA device name to Pixie: no gateway IP", self._log_prefix)
            return
        try:
            net_id_int = int(str(self.cloud_params.netid))
            mesh_net2_int = int(str(self.cloud_params.meshnet2))
        except (TypeError, ValueError):
            LOGGER.warning("%sCould not sync HA device name to Pixie: missing netID/meshNet2", self._log_prefix)
            return

        payload = self._device_manager_payload_for_name(record, new_name)
        LOGGER.info(
            "%sSyncing HA device name to Pixie id=%s old=%r new=%r",
            self._log_prefix,
            record.id,
            record.name,
            new_name,
        )
        try:
            result = await self.coordinator.hass.async_add_executor_job(
                partial(
                    self.handler.send_53216_json,
                    hub_ip=hub_ip,
                    net_id_int=net_id_int,
                    mesh_net2_int=mesh_net2_int,
                    payload=payload,
                    timeout=5.0,
                )
            )
        except Exception as err:
            LOGGER.warning("%sHA-to-Pixie device name sync failed for %s: %s", self._log_prefix, record.name, err)
            return

        if str(result.get("result") or "").lower() != "success":
            LOGGER.warning(
                "%sHA-to-Pixie device name sync returned unexpected response for %s: %s",
                self._log_prefix,
                record.name,
                result,
            )
            return
        conf_index = result.get("confIndex")
        if isinstance(conf_index, list):
            await self.async_handle_gateway_conf_update(conf_index)
        LOGGER.info("%sHA device name synced to Pixie id=%s name=%r", self._log_prefix, record.id, new_name)

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

    def _push_inventory_update(
        self,
        inventory: PixieInventory,
        *,
        from_thread: bool,
        snapshot_reason: str,
        timer_reason: str,
    ) -> None:
        """Push a runtime inventory update to HA and run shared follow-up work."""
        self.pixie_runtime.inventory = inventory
        self._attach_runtime_session_health_callback()
        loop = self.coordinator.hass.loop
        if from_thread:
            loop.call_soon_threadsafe(self.coordinator.async_set_updated_data, inventory)
            loop.call_soon_threadsafe(
                partial(self._queue_inventory_snapshot_save, inventory, reason=snapshot_reason)
            )
        else:
            self.coordinator.async_set_updated_data(inventory)
            self._queue_inventory_snapshot_save(inventory, reason=snapshot_reason)

        for device_id in sorted(inventory.devices_by_id):
            rec = inventory.devices_by_id[device_id]
            if rec.capabilities.supports_contact_sensor and rec.runtime.contact_momentary and rec.runtime.contact_active:
                if from_thread:
                    loop.call_soon_threadsafe(self._schedule_contact_reset, device_id)
                else:
                    self._schedule_contact_reset(device_id)

        # If a timer device needs an immediate poll (external mode change or
        # turn-on), schedule it now instead of waiting for the next coordinator
        # cycle (which can be up to 10 s away).
        self._schedule_pending_timer_polls(inventory, from_thread=from_thread, reason=timer_reason)
        if from_thread:
            loop.call_soon_threadsafe(self._schedule_config_refreshes_for_online_transitions, inventory)
        else:
            self._schedule_config_refreshes_for_online_transitions(inventory)

    def push_inventory_update_from_thread(self, inventory: PixieInventory) -> None:
        """Push a runtime inventory update to HA from the TCP worker thread."""
        self._push_inventory_update(
            inventory,
            from_thread=True,
            snapshot_reason="TCP runtime update",
            timer_reason="external change",
        )

    def push_inventory_update_from_loop(self, inventory: PixieInventory) -> None:
        """Push a runtime inventory update already running in HA's event loop."""
        self._push_inventory_update(
            inventory,
            from_thread=False,
            snapshot_reason="BLE runtime update",
            timer_reason="BLE update",
        )

    def push_unknown_device_update_from_runtime(self, device_id: int) -> None:
        """Schedule BLE-only dynamic discovery for an unknown runtime device id."""
        loop = self.coordinator.hass.loop

        def _schedule() -> None:
            self.coordinator.hass.async_create_task(
                self.async_handle_ble_only_unknown_device(int(device_id))
            )

        loop.call_soon_threadsafe(_schedule)

    async def async_handle_ble_only_unknown_device(self, device_id: int) -> None:
        """Dynamically add a BLE-only device that appeared in runtime dc1102 traffic."""
        if _entry_inventory_mode(self.entry) != INVENTORY_MODE_BLE_ADVERTISEMENT:
            return
        import time as _time
        now = _time.time()
        inventory = self.pixie_runtime.inventory
        if inventory is None or int(device_id) in inventory.devices_by_id:
            return
        if int(device_id) in self.ble_external_add_inflight:
            return

        last_attempt = float(self.ble_external_add_last_attempt.get(int(device_id), 0.0) or 0.0)
        if now - last_attempt < 30.0:
            LOGGER.debug(
                "%sPixie BLE-only external add skipped by cooldown dev_id=%s",
                self._log_prefix,
                device_id,
            )
            return

        ble_runtime = await self.async_wait_for_ble_runtime_ready(timeout=5.0)
        if ble_runtime is None or not ble_runtime.health.healthy:
            LOGGER.debug(
                "%sPixie BLE-only external add skipped because BLE runtime is unavailable dev_id=%s",
                self._log_prefix,
                device_id,
            )
            return

        self.ble_external_add_inflight.add(int(device_id))
        self.ble_external_add_last_attempt[int(device_id)] = now
        try:
            LOGGER.info(
                "%sPixie BLE-only unknown device appeared; requesting identity dev_id=%s",
                self._log_prefix,
                device_id,
            )
            identity = await ble_runtime.async_request_identity(int(device_id))
            if identity is None:
                LOGGER.debug(
                    "%sPixie BLE-only external add identity request returned no db1102 dev_id=%s",
                    self._log_prefix,
                    device_id,
                )
                return
            model_no = str(getattr(identity, "model_no", "") or "")
            if model_no not in hardware_list:
                LOGGER.debug(
                    "%sPixie BLE-only external add ignored unsupported model dev_id=%s model=%s mac=%s",
                    self._log_prefix,
                    device_id,
                    model_no,
                    getattr(identity, "mac", None),
                )
                return

            inventory = self.pixie_runtime.inventory
            if inventory is None:
                return
            old_ids = set(inventory.devices_by_id)
            record = inventory.add_or_update_ble_identity_device(
                identity,
                device_id=int(identity.device_id),
                source="ble_runtime_external_add",
            )
            new_ids = set(inventory.devices_by_id)
            added_ids = new_ids - old_ids
            self.reset_config_refresh_state_for_devices((int(record.id),))

            data = dict(self.entry.data)
            data[CONF_BLE_INVENTORY] = inventory.to_dict()
            self.coordinator.hass.config_entries.async_update_entry(self.entry, data=data)
            self.pixie_runtime.inventory = inventory
            if self.ble_runtime is not None:
                self.ble_runtime.inventory = inventory

            replayed = self.handler.replay_unknown_device_updates({int(record.id)})
            await _async_cleanup_stale_ble_only_owners_after_add(
                self.coordinator.hass,
                self.entry,
                {PixieInventory._normalize_mac(record.mac)},
            )
            await async_register_device_topology(self.coordinator.hass, self.entry, inventory, domain=DOMAIN)
            self.coordinator.async_set_updated_data(inventory)
            for added_id in sorted(added_ids):
                async_dispatcher_send(self.coordinator.hass, device_added_signal(self.entry), int(added_id))
            self._queue_inventory_snapshot_save(inventory, reason="BLE-only external device add")
            self.start_power_meter_polling()
            self.schedule_config_refreshes_for_devices(inventory, {int(record.id)}, reason="device_added")
            LOGGER.info(
                "%sPixie BLE-only external device added id=%s name=%s mac=%s model=%s replayed=%s",
                self._log_prefix,
                record.id,
                record.name,
                record.mac,
                record.model_no,
                replayed,
            )
        except Exception as err:
            LOGGER.warning(
                "%sPixie BLE-only external add failed dev_id=%s: %s",
                self._log_prefix,
                device_id,
                err,
            )
        finally:
            self.ble_external_add_inflight.discard(int(device_id))

    def push_config_update_from_thread(self, conf_index: list[int]) -> None:
        """Push a gateway configuration update from the TCP worker thread."""
        loop = self.coordinator.hass.loop

        def _schedule() -> None:
            self.coordinator.hass.async_create_task(self.async_handle_gateway_conf_update(conf_index))

        loop.call_soon_threadsafe(_schedule)

    async def async_handle_gateway_conf_update(self, conf_index: list[int]) -> None:
        """Refresh runtime inventory after a gateway confUpdate notification."""
        if _entry_inventory_mode(self.entry) != INVENTORY_MODE_LOCAL_53216:
            LOGGER.debug(
                "%sIgnoring gateway confUpdate for non-local inventory mode=%s confIndex=%s",
                self._log_prefix,
                _entry_inventory_mode(self.entry),
                conf_index,
            )
            return

        inventory = self.pixie_runtime.inventory
        if inventory is None:
            LOGGER.debug("%sIgnoring gateway confUpdate because inventory is not initialized", self._log_prefix)
            return
        if not _entry_gateway_supports_local_inventory_53216(self.entry, inventory):
            LOGGER.debug("%sIgnoring gateway confUpdate because gateway does not support 53216 inventory", self._log_prefix)
            return

        async with self.conf_update_lock:
            inventory = self.pixie_runtime.inventory
            if inventory is None:
                return
            selected = _conf_index_selected(inventory.conf_index, conf_index)
            if selected == 0:
                LOGGER.debug("%sGateway confUpdate did not change ConfIndex; no inventory refresh needed", self._log_prefix)
                return

            if not (selected & 0x01):
                inventory.conf_index = list(conf_index)
                self.coordinator.async_set_updated_data(inventory)
                self._queue_inventory_snapshot_save(inventory, reason=f"gateway confUpdate selected={selected}")
                LOGGER.debug(
                    "%sGateway confUpdate selected=%s does not include deviceList; stored ConfIndex only",
                    self._log_prefix,
                    selected,
                )
                return

            self.handler.begin_unknown_device_update_hold(
                seconds=15.0,
                reason=f"gateway confUpdate selected={selected}",
            )

            hub_ip = _handler_gateway_ip(self.handler) or _entry_gateway_ip(self.entry)
            if not hub_ip:
                LOGGER.warning("%sGateway confUpdate received but no gateway IP is available for 53216 refresh", self._log_prefix)
                return

            try:
                net_id_int = int(str(self.cloud_params.netid))
                mesh_net2_int = int(str(self.cloud_params.meshnet2))
            except (TypeError, ValueError):
                LOGGER.warning(
                    "%sGateway confUpdate received but netID/meshNet2 are unavailable for 53216 refresh",
                    self._log_prefix,
                )
                return

            old_inventory = inventory
            old_ids = set(old_inventory.devices_by_id)
            LOGGER.debug(
                "%sGateway confUpdate refreshing local inventory selected=%s oldConfIndex=%s newConfIndex=%s",
                self._log_prefix,
                selected,
                old_inventory.conf_index,
                conf_index,
            )
            try:
                sync_result = await self.coordinator.hass.async_add_executor_job(
                    partial(
                        self.handler._sync_inventory_53216_once,
                        hub_ip=hub_ip,
                        net_id_int=net_id_int,
                        mesh_net2_int=mesh_net2_int,
                        timeout=5.0,
                        selected=selected,
                    )
                )
                payload = self.handler._extract_53216_inventory_payload(sync_result.get("data"))
                if payload is None:
                    LOGGER.warning("%sGateway confUpdate 53216 refresh returned no deviceList payload", self._log_prefix)
                    return
                home_payload = _complete_home_payload(
                    payload,
                    inventory=old_inventory,
                    cloud_params=self.cloud_params,
                )
                home_payload["ConfIndex"] = list(conf_index)
                self.handler._set_inventory_from_home_object(
                    home_payload,
                    user_id=self.cloud_params.user_id,
                    source=f"hub_53216_confupdate_selected_{selected}",
                    show_devices=False,
                )
                new_inventory = self.handler.inventory
                if new_inventory is None:
                    LOGGER.warning("%sGateway confUpdate 53216 refresh did not build an inventory", self._log_prefix)
                    return
                _preserve_runtime_state_by_mac(old_inventory, new_inventory)
                new_inventory.conf_index = list(conf_index)
                self.pixie_runtime.inventory = new_inventory
                if self.ble_runtime is not None:
                    self.ble_runtime.inventory = new_inventory
                new_ids = set(new_inventory.devices_by_id)
                replayed = self.handler.replay_unknown_device_updates(new_ids - old_ids)

                self._sync_changed_pixie_device_names_to_ha(old_inventory, new_inventory)
                await async_register_device_topology(self.coordinator.hass, self.entry, new_inventory, domain=DOMAIN)
                self.coordinator.async_set_updated_data(new_inventory)
                for device_id in sorted(new_ids - old_ids):
                    async_dispatcher_send(self.coordinator.hass, device_added_signal(self.entry), int(device_id))
                await async_cleanup_orphaned_registry_entries(
                    self.coordinator.hass,
                    self.entry,
                    new_inventory,
                    reason=f"gateway confUpdate selected={selected}",
                )
                self._queue_inventory_snapshot_save(new_inventory, reason=f"gateway confUpdate selected={selected}")
                self.start_power_meter_polling()
                LOGGER.info(
                    "%sGateway confUpdate inventory refresh applied selected=%s added=%s removed=%s replayed=%s devices=%s",
                    self._log_prefix,
                    selected,
                    sorted(new_ids - old_ids),
                    sorted(old_ids - new_ids),
                    replayed,
                    len(new_inventory.devices_by_id),
                )
            except Exception as err:
                LOGGER.warning(
                    "%sGateway confUpdate inventory refresh failed selected=%s: %s",
                    self._log_prefix,
                    selected,
                    err,
                )

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

    async def async_refresh_config_for_device(self, device_id: int, refresh_key: str, *, reason: str) -> None:
        """Refresh one config group for a device using the existing command path."""
        inventory = self.pixie_runtime.inventory
        rec = inventory.devices_by_id.get(int(device_id)) if inventory is not None else None
        if rec is None:
            return
        for spec in get_startup_config_refresh_specs_for_capabilities(rec.capabilities):
            if str(spec.get("key")) != str(refresh_key):
                continue
            command_kwargs = dict(spec.get("command_kwargs") or {})
            if not command_kwargs:
                return
            await self.async_send_local_command(
                self.coordinator.hass,
                command_device_id=int(device_id),
                **command_kwargs,
            )
            LOGGER.debug(
                "%sConfig refresh sent: device=%s key=%s reason=%s",
                self._log_prefix,
                device_id,
                refresh_key,
                reason,
            )
            return

    async def _async_delayed_config_refresh(
        self,
        device_id: int,
        refresh_key: str,
        *,
        reason: str,
        delay_seconds: float,
    ) -> None:
        """Run a config refresh after a short readiness delay."""
        await asyncio.sleep(delay_seconds)
        await self.async_refresh_config_for_device(device_id, refresh_key, reason=reason)

    def _schedule_config_refresh(
        self,
        rec: DeviceRecord,
        spec: dict[str, Any],
        *,
        reason: str,
        delay_seconds: float = 0.0,
    ) -> bool:
        """Schedule one config refresh spec for an online device."""
        refresh_key = str(spec.get("key") or "")
        command_kwargs = dict(spec.get("command_kwargs") or {})
        if not refresh_key or not command_kwargs:
            return False
        refresh_id = (int(rec.id), refresh_key)
        if refresh_id in self.config_refresh_requested:
            return False
        if rec.runtime.presence != "online":
            return False
        self.config_refresh_requested.add(refresh_id)
        refresh_coro = (
            self._async_delayed_config_refresh(
                int(rec.id),
                refresh_key,
                reason=reason,
                delay_seconds=delay_seconds,
            )
            if delay_seconds > 0
            else self.async_refresh_config_for_device(int(rec.id), refresh_key, reason=reason)
        )
        self.coordinator.hass.async_create_task(refresh_coro)
        LOGGER.debug(
            "%sConfig refresh queued: device=%s key=%s reason=%s delay=%.1fs",
            self._log_prefix,
            rec.id,
            refresh_key,
            reason,
            delay_seconds,
        )
        return True

    def schedule_config_refreshes_for_devices(
        self,
        inventory: PixieInventory,
        device_ids: Iterable[int],
        *,
        reason: str,
    ) -> None:
        """Schedule startup-style config refreshes for selected devices."""
        for device_id in sorted({int(device_id) for device_id in device_ids}):
            if device_id not in inventory.devices_by_id:
                continue
            rec = inventory.devices_by_id[device_id]
            specs = get_startup_config_refresh_specs_for_capabilities(rec.capabilities)
            if not specs:
                continue
            device_id_int = int(device_id)
            if rec.runtime.presence != "online":
                if self.config_refresh_last_presence.get(device_id_int) != rec.runtime.presence:
                    LOGGER.debug(
                        "%sConfig refresh skipped while offline: device=%s keys=%s reason=%s",
                        self._log_prefix,
                        device_id,
                        [str(spec.get("key")) for spec in specs],
                        reason,
                    )
                self.config_refresh_last_presence[device_id_int] = rec.runtime.presence
                continue
            self.config_refresh_last_presence[device_id_int] = rec.runtime.presence
            for spec in specs:
                self._schedule_config_refresh(
                    rec,
                    spec,
                    reason=reason,
                    delay_seconds=1.0 if reason == "device_added" else 0.0,
                )

    def reset_config_refresh_state_for_devices(self, device_ids: Iterable[int]) -> None:
        """Clear config-refresh bookkeeping for devices that were newly added/re-added."""
        reset_ids = {int(device_id) for device_id in device_ids}
        if not reset_ids:
            return
        self.config_refresh_requested = {
            refresh_id
            for refresh_id in self.config_refresh_requested
            if int(refresh_id[0]) not in reset_ids
        }
        for device_id in reset_ids:
            self.config_refresh_last_presence.pop(device_id, None)

    def schedule_startup_config_refreshes(self, inventory: PixieInventory) -> None:
        """Schedule startup/reload config refreshes for online devices."""
        self.schedule_config_refreshes_for_devices(
            inventory,
            inventory.devices_by_id,
            reason="startup",
        )

    def _schedule_config_refreshes_for_online_transitions(self, inventory: PixieInventory) -> None:
        """Refresh config once when a refresh-capable device comes online after being offline."""
        for device_id in sorted(inventory.devices_by_id):
            rec = inventory.devices_by_id[device_id]
            specs = get_startup_config_refresh_specs_for_capabilities(rec.capabilities)
            if not specs:
                continue
            device_id_int = int(device_id)
            previous_presence = self.config_refresh_last_presence.get(device_id_int)
            current_presence = rec.runtime.presence
            self.config_refresh_last_presence[device_id_int] = current_presence
            if previous_presence == "offline" and current_presence == "online":
                for spec in specs:
                    self._schedule_config_refresh(rec, spec, reason="online_transition", delay_seconds=1.0)

    def _power_meter_option_key(self, record: DeviceRecord) -> str:
        """Return the stable options key for one power-meter device."""
        normalized_mac = PixieInventory._normalize_mac(record.mac)
        return normalized_mac or str(record.id)

    def power_meter_poll_interval_seconds(self, record: DeviceRecord) -> int:
        """Return the configured poll interval for one power-meter device."""
        intervals = self.entry.options.get(CONF_POWER_POLL_INTERVALS)
        value = None
        if isinstance(intervals, dict):
            key = self._power_meter_option_key(record)
            value = intervals.get(key)
            if value is None:
                value = intervals.get(str(record.id))
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            seconds = POWER_POLL_DEFAULT_INTERVAL_SECONDS
        return max(1, min(POWER_POLL_MAX_INTERVAL_SECONDS, seconds))

    async def async_set_power_meter_poll_interval(self, record: DeviceRecord, seconds: int) -> None:
        """Persist a power-meter poll interval without reloading the entry."""
        clamped = max(1, min(POWER_POLL_MAX_INTERVAL_SECONDS, int(seconds)))
        intervals = dict(self.entry.options.get(CONF_POWER_POLL_INTERVALS) or {})
        intervals[self._power_meter_option_key(record)] = clamped
        options = dict(self.entry.options)
        options[CONF_POWER_POLL_INTERVALS] = intervals
        self.coordinator.hass.config_entries.async_update_entry(self.entry, options=options)
        self.coordinator.async_set_updated_data(self.pixie_runtime.inventory)
        LOGGER.info(
            "%sPower meter poll interval updated: device=%s interval=%ss",
            self._log_prefix,
            record.id,
            clamped,
        )

    async def async_poll_power_meter_device(self, device_id: int, *, reason: str) -> None:
        """Poll one power-meter device for live and energy values."""
        inventory = self.pixie_runtime.inventory
        rec = inventory.devices_by_id.get(int(device_id)) if inventory is not None else None
        if rec is None or not rec.capabilities.supports_power_metering:
            return
        if int(device_id) in self.power_poll_inflight:
            return
        self.power_poll_inflight.add(int(device_id))
        try:
            import time as _time
            self.last_power_poll_requested_at[int(device_id)] = _time.time()
            await self.async_send_local_command(
                self.coordinator.hass,
                command_device_id=int(device_id),
                command_power_meter_action="poll",
            )
            LOGGER.debug("%sPower meter poll queued: device=%s reason=%s", self._log_prefix, device_id, reason)
        finally:
            self.power_poll_inflight.discard(int(device_id))

    @callback
    def _handle_power_meter_poll_tick(self, _now=None) -> None:
        """Run due power-meter polls."""
        inventory = self.pixie_runtime.inventory
        if inventory is None:
            return
        import time as _time
        now = _time.time()
        for device_id in sorted(inventory.devices_by_id):
            rec = inventory.devices_by_id[device_id]
            if not rec.capabilities.supports_power_metering or rec.runtime.presence != "online":
                continue
            interval = self.power_meter_poll_interval_seconds(rec)
            last_requested = self.last_power_poll_requested_at.get(device_id)
            last_response = rec.runtime.last_power_meter_poll_at
            markers = [
                value
                for value in (last_requested, last_response)
                if isinstance(value, (int, float))
            ]
            last_poll = max(markers) if markers else None
            if last_poll is not None and now - float(last_poll) < interval:
                continue
            self.coordinator.hass.async_create_task(
                self.async_poll_power_meter_device(device_id, reason="interval")
            )

    def start_power_meter_polling(self) -> None:
        """Start interval polling when this entry contains power-meter devices."""
        inventory = self.pixie_runtime.inventory
        if inventory is None or self.power_poll_remove is not None:
            return
        if not any(rec.capabilities.supports_power_metering for rec in inventory.devices_by_id.values()):
            return
        self.power_poll_remove = async_track_time_interval(
            self.coordinator.hass,
            self._handle_power_meter_poll_tick,
            timedelta(seconds=1),
        )
        for device_id, rec in inventory.devices_by_id.items():
            if rec.capabilities.supports_power_metering:
                self.coordinator.hass.async_create_task(
                    self.async_poll_power_meter_device(device_id, reason="startup")
                )
        LOGGER.debug("%sStarted power meter polling", self._log_prefix)

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
        if self.ble_runtime is None or not _entry_bt_enabled(self.entry) or self.ble_runtime_suspend_count > 0:
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
        if command_kwargs.get("command_raw_hexes") is None:
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
        if _entry_inventory_mode(self.entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
            raise ConfigEntryError("Pixie BLE-only entries do not have a TCP gateway runtime")
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
            restart_handler.set_config_update_callback(self.push_config_update_from_thread)
            restart_handler.set_unknown_device_update_callback(self.push_unknown_device_update_from_runtime)

            username = _entry_username(self.entry)
            password = _entry_password(self.entry)
            inventory_mode = _entry_inventory_mode(self.entry)
            restart_handler.inventory_mode = inventory_mode
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
            if self.ha_name_sync_unsub is not None:
                self.ha_name_sync_unsub()
                self.ha_name_sync_unsub = None
            self.ha_name_sync_inflight.clear()
            self.ha_name_sync_suppressed_device_ids.clear()
            self._clear_esphome_proxy_monitor()
            if self.power_poll_remove is not None:
                self.power_poll_remove()
                self.power_poll_remove = None
            self.power_poll_inflight.clear()
            self.last_power_poll_requested_at.clear()
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
