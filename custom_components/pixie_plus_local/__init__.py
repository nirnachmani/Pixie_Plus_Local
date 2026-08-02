"""Home Assistant config-entry setup for Pixie Plus Local."""

from __future__ import annotations

from contextlib import suppress
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .pixie_ble import (
    BT_STATE_NO_WORKING_PROXY,
    PixieBluetoothRuntime,
    async_discover_ble_only_mesh_inventory,
)
from .pixie_const import (
    CONF_BLE_INVENTORY,
    CONF_BLE_MEMBERSHIP,
    CONF_BT_ACCESS_NODE,
    CONF_BT_BETTER_CANDIDATE_SEEN,
    CONF_BT_ENABLED,
    CONF_BT_SOURCE,
    CONF_BT_STATE,
    CONF_GATEWAY_IP,
    CONF_GATEWAY_IP_REQUIRED,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_INVENTORY_FALLBACK_REASON,
    CONF_INVENTORY_MODE,
    CONF_MESHNET,
    CONF_MESHNET2,
    CONF_NETID,
    CONF_PIXIE_PASSWORD,
    CONF_PIXIE_PIN,
    CONF_PIXIE_USERNAME,
    CONF_USER_ID,
    DOMAIN,
    INTEGRATION_TITLE,
    INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED,
    INVENTORY_MODE_BLE_ADVERTISEMENT,
    INVENTORY_MODE_CLOUD_FALLBACK,
    INVENTORY_MODE_LOCAL_53216,
    PLATFORMS,
)
from .pixie_ha import (
    PixiePlusConfigEntryRuntimeData,
    PixiePlusRuntimeCoordinator,
    _async_create_bt_proxy_issue,
    _async_create_gateway_ip_issue,
    _async_create_local_inventory_fallback_issue,
    _async_create_missing_credentials_issue,
    _async_delete_bt_proxy_issue,
    _async_delete_gateway_ip_issue,
    _async_delete_local_inventory_fallback_issue,
    _async_delete_missing_credentials_issue,
    _async_ensure_ble_firmware_refresh_hooks,
    _async_load_inventory_snapshot,
    _async_maybe_remove_ble_firmware_refresh_hooks,
    _async_save_inventory_snapshot,
    _async_update_entry_runtime_data,
    _entry_bt_access_node_preference,
    _entry_bt_enabled,
    _entry_gateway_ip,
    _entry_gateway_ip_required,
    _entry_home_name,
    _entry_inventory_fallback_reason,
    _entry_inventory_mode,
    _entry_log_prefix,
    _entry_password,
    _entry_username,
    _handler_cloud_params,
    _handler_gateway_ip,
    _inventory_persistent_signature,
    async_cleanup_orphaned_registry_entries,
    async_register_device_topology,
    physical_device_identifier,
)
from .pixie_inventory import DeviceRecord, PixieInventory
from .pixie_runtime import (
    CloudParams,
    PixieAuthError,
    PixieAuthHandler,
    PixieGatewayConnectionError,
    PixieGatewayResolutionError,
    PixieRuntimeData,
)

LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Pixie Plus Local integration."""
    return True


def _cloud_params_from_entry(entry: ConfigEntry) -> CloudParams:
    """Build bootstrap cloud parameters from persisted config-entry data."""
    if _entry_inventory_mode(entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
        home_id = str(entry.data.get(CONF_HOME_ID) or entry.unique_id or entry.entry_id)
        home_name = str(entry.data.get(CONF_HOME_NAME) or entry.title or "Pixie")
        pin = str(entry.data.get(CONF_PIXIE_PIN) or "")
        membership = str(entry.data.get(CONF_BLE_MEMBERSHIP) or "")
        return CloudParams(
            home_id=home_id,
            home_name=home_name,
            user_id="ble_only",
            meshnet=membership or "unknown",
            meshnet2=membership or "unknown",
            netid=pin,
        )

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

    if inventory_mode == INVENTORY_MODE_BLE_ADVERTISEMENT:
        handler = PixieAuthHandler()
        handler.inventory_mode = INVENTORY_MODE_BLE_ADVERTISEMENT
        membership = str(entry.data.get(CONF_BLE_MEMBERSHIP) or "").strip().lower()
        if not membership:
            raise ConfigEntryNotReady("Pixie BLE-only entry is missing its mesh membership")
        if persisted_inventory is None and isinstance(entry.data.get(CONF_BLE_INVENTORY), dict):
            try:
                persisted_inventory = PixieInventory.from_dict(dict(entry.data[CONF_BLE_INVENTORY]))
                LOGGER.debug(
                    "%sRestored Pixie BLE-only inventory from config entry data: devices=%s",
                    _entry_log_prefix(entry),
                    len(persisted_inventory.devices_by_id),
                )
            except Exception as err:
                LOGGER.debug(
                    "%sCould not restore Pixie BLE-only inventory from config entry data: %s",
                    _entry_log_prefix(entry),
                    err,
                )
        home_name = str(entry.data.get(CONF_HOME_NAME) or entry.title or "Pixie")
        discovery = None
        discovery_error: Exception | None = None
        try:
            discovery = await async_discover_ble_only_mesh_inventory(
                hass,
                cloud_params,
                home_name=home_name,
                pin=str(entry.data.get(CONF_PIXIE_PIN) or ""),
                membership=membership,
                inventory=persisted_inventory,
                preferred_source=str(entry.data.get(CONF_BT_SOURCE) or "") or None,
                preferred_access_node=str(entry.data.get(CONF_BT_ACCESS_NODE) or "") or None,
            )
        except Exception as err:
            discovery_error = err
            LOGGER.info(
                "%sPixie BLE-only logged-in inventory discovery unavailable: %s",
                _entry_log_prefix(entry),
                err,
            )

        adverts = list(discovery.identities) if discovery is not None else []
        scanned_inventory = PixieInventory.from_ble_advertisements(
            adverts,
            home_name=home_name,
            membership=membership,
        )
        if persisted_inventory is None:
            if not scanned_inventory.devices_by_id:
                raise ConfigEntryNotReady(
                    f"Pixie BLE-only inventory discovery found no matching devices: {discovery_error}"
                )
            persisted_inventory = scanned_inventory
            merge_summary = {
                "added": len(scanned_inventory.devices_by_id),
                "updated": 0,
                "retained_missing": 0,
            }
        else:
            merge_summary = persisted_inventory.merge_ble_advertisement_inventory(scanned_inventory)
            if not persisted_inventory.devices_by_id:
                raise ConfigEntryNotReady("Pixie BLE-only inventory scan found no matching devices")

        LOGGER.info(
            "%sPixie BLE-only startup inventory merged devices=%s identity_or_advert_records=%s scanned=%s added=%s updated=%s retained_missing=%s",
            _entry_log_prefix(entry),
            len(persisted_inventory.devices_by_id),
            len(adverts),
            len(scanned_inventory.devices_by_id),
            merge_summary["added"],
            merge_summary["updated"],
            merge_summary["retained_missing"],
        )
        handler.inventory = persisted_inventory
        if discovery is not None:
            for ble_hex in discovery.pending_bledata_hex:
                handler.apply_bledata_hex(ble_hex, source="ble_runtime", bulk_source="ble_runtime")
        data = dict(entry.data)
        data[CONF_BLE_INVENTORY] = persisted_inventory.to_dict()
        if discovery is not None:
            if discovery.health.source:
                data[CONF_BT_SOURCE] = discovery.health.source
            if discovery.health.access_node:
                data[CONF_BT_ACCESS_NODE] = discovery.health.access_node
        hass.config_entries.async_update_entry(entry, data=data)
        pixie_runtime = PixieRuntimeData(
            handler=handler,
            runtime_session=None,
            inventory=persisted_inventory,
            inventory_mode=INVENTORY_MODE_BLE_ADVERTISEMENT,
        )
        coordinator = PixiePlusRuntimeCoordinator(hass, entry, pixie_runtime)
        await coordinator.async_config_entry_first_refresh()
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
                enabled=True,
                command_builder=handler,
                inventory_update_callback=None,
                preferred_source=str(entry.data.get(CONF_BT_SOURCE) or "") or None,
                preferred_access_node=str(entry.data.get(CONF_BT_ACCESS_NODE) or "") or None,
                better_candidate_seen=bool(entry.data.get(CONF_BT_BETTER_CANDIDATE_SEEN)),
                login_seed=str(entry.data.get(CONF_PIXIE_PIN) or ""),
                membership=str(entry.data.get(CONF_BLE_MEMBERSHIP) or ""),
                send_runtime_sync_on_connect=True,
            ),
        )
        runtime_data.last_persisted_inventory_signature = _inventory_persistent_signature(pixie_runtime.inventory)
        coordinator.runtime_manager = runtime_data
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
        handler.set_config_update_callback(runtime_data.push_config_update_from_thread)
        handler.set_unknown_device_update_callback(runtime_data.push_unknown_device_update_from_runtime)
        await runtime_data.async_ensure_ble_runtime()
        await _async_save_inventory_snapshot(hass, entry, pixie_runtime.inventory)
        return runtime_data

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
            preserved_gate_settings = pixie_runtime.inventory.preserve_gate_settings_from(persisted_inventory)
            preserved_indicator_led_settings = pixie_runtime.inventory.preserve_indicator_led_settings_from(persisted_inventory)
            preserved_sensor_config_settings = pixie_runtime.inventory.preserve_sensor_config_settings_from(persisted_inventory)
            if preserved_versions or preserved_gate_settings or preserved_indicator_led_settings or preserved_sensor_config_settings:
                LOGGER.debug(
                    "%sPreserved stored inventory metadata: ble_firmware_versions=%s gate_setting_records=%s indicator_led_setting_records=%s sensor_config_records=%s",
                    _entry_log_prefix(entry),
                    preserved_versions,
                    preserved_gate_settings,
                    preserved_indicator_led_settings,
                    preserved_sensor_config_settings,
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
    handler.set_config_update_callback(runtime_data.push_config_update_from_thread)
    handler.set_unknown_device_update_callback(runtime_data.push_unknown_device_update_from_runtime)
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
    runtime_data.async_setup_ha_device_name_sync(hass)
    _async_ensure_ble_firmware_refresh_hooks(hass)
    runtime_data.start_power_meter_polling()

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await async_cleanup_orphaned_registry_entries(
        hass,
        entry,
        runtime_data.pixie_runtime.inventory,
        reason="setup",
    )

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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Remove a Pixie device from Pixie first, then from HA's inventory."""
    runtime_data = getattr(entry, "runtime_data", None)
    inventory = None
    if isinstance(runtime_data, PixiePlusConfigEntryRuntimeData):
        inventory = runtime_data.pixie_runtime.inventory
    if inventory is None:
        inventory = await _async_load_inventory_snapshot(hass, entry)
    if inventory is None and isinstance(entry.data.get(CONF_BLE_INVENTORY), dict):
        try:
            inventory = PixieInventory.from_dict(dict(entry.data[CONF_BLE_INVENTORY]))
        except Exception as err:
            LOGGER.debug(
                "%sCould not restore Pixie BLE-only inventory while removing device: %s",
                _entry_log_prefix(entry),
                err,
            )
    if inventory is None:
        return False

    target: DeviceRecord | None = None
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        normalized_identifier = str(identifier or "")
        for record in inventory.devices_by_id.values():
            physical_identifier = physical_device_identifier(record)
            if (
                normalized_identifier == physical_identifier
                or normalized_identifier.startswith(f"{physical_identifier}:")
            ):
                target = record
                break
        if target is not None:
            break

    if target is None:
        if _entry_inventory_mode(entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
            LOGGER.info(
                "%sAllowing HA removal of stale BLE-only Pixie registry device identifiers=%s",
                _entry_log_prefix(entry),
                sorted(str(identifier) for domain, identifier in device_entry.identifiers if domain == DOMAIN),
            )
            return True
        return False

    if not isinstance(runtime_data, PixiePlusConfigEntryRuntimeData):
        LOGGER.warning("%sCannot remove Pixie device %s because runtime is not loaded", _entry_log_prefix(entry), target.id)
        return False

    try:
        await runtime_data.async_remove_pixie_device(target)
    except Exception as err:
        LOGGER.warning(
            "%sPixie device removal failed; leaving HA device in place id=%s name=%s mac=%s: %s",
            _entry_log_prefix(entry),
            target.id,
            target.name,
            target.mac,
            err,
        )
        return False

    removed: DeviceRecord | None = None
    for domain, identifier in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        removed = inventory.remove_device_by_ha_identifier(identifier)
        if removed is not None:
            break

    if removed is None:
        return False

    data = dict(entry.data)
    if _entry_inventory_mode(entry) == INVENTORY_MODE_BLE_ADVERTISEMENT:
        data[CONF_BLE_INVENTORY] = inventory.to_dict()
        hass.config_entries.async_update_entry(entry, data=data)
    await _async_save_inventory_snapshot(hass, entry, inventory)

    runtime_data.pixie_runtime.inventory = inventory
    runtime_data.coordinator.async_set_updated_data(inventory)
    runtime_data.last_persisted_inventory_signature = _inventory_persistent_signature(inventory)

    LOGGER.info(
        "%sRemoved Pixie device from inventory: id=%s name=%s mac=%s",
        _entry_log_prefix(entry),
        removed.id,
        removed.name,
        removed.mac,
    )
    return True
