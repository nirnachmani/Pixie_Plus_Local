"""Home Assistant config-entry setup for Pixie Plus Local."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .pixie_inventory import DeviceRecord, PixieInventory
from .pixie_runtime import CloudParams, PixieAuthError, PixieAuthHandler, PixieRuntimeData
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
CONF_PIXIE_USERNAME = "pixie_username"
CONF_PIXIE_PASSWORD = "pixie_password"

INVENTORY_MODE_LOCAL_53216 = "local_53216"
INVENTORY_MODE_CLOUD_FALLBACK = "cloud_fallback"
ISSUE_ID_MISSING_FALLBACK_CREDENTIALS = "missing_fallback_credentials"

COORDINATOR_UPDATE_INTERVAL = timedelta(seconds=10)
TIMER_POLL_INTERVAL_SECONDS = 10.0
INVENTORY_STORE_VERSION = 1


def _inventory_store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, INVENTORY_STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_inventory")


async def _async_load_inventory_snapshot(hass: HomeAssistant, entry: ConfigEntry) -> PixieInventory | None:
    payload = await _inventory_store(hass, entry).async_load()
    if not isinstance(payload, dict):
        LOGGER.debug("No stored Pixie inventory snapshot found for entry %s", entry.entry_id)
        return None
    snapshot = payload.get("inventory")
    if not isinstance(snapshot, dict):
        LOGGER.debug("Stored Pixie inventory snapshot is missing inventory data for entry %s", entry.entry_id)
        return None
    try:
        inventory = PixieInventory.from_dict(snapshot)
        LOGGER.debug(
            "Restored Pixie inventory snapshot for entry %s: home=%s devices=%s",
            entry.entry_id,
            inventory.home_id,
            len(inventory.devices_by_id),
        )
        return inventory
    except Exception as err:
        LOGGER.warning("Could not restore Pixie inventory snapshot: %s", err)
        return None


async def _async_save_inventory_snapshot(hass: HomeAssistant, entry: ConfigEntry, inventory: PixieInventory | None) -> None:
    if inventory is None:
        return
    await _inventory_store(hass, entry).async_save({"inventory": inventory.to_dict()})
    LOGGER.debug(
        "Saved Pixie inventory snapshot for entry %s: home=%s devices=%s",
        entry.entry_id,
        inventory.home_id,
        len(inventory.devices_by_id),
    )


def _entry_inventory_mode(entry: ConfigEntry) -> str:
    mode = str(entry.data.get(CONF_INVENTORY_MODE) or INVENTORY_MODE_LOCAL_53216)
    resolved_mode = mode if mode in (INVENTORY_MODE_LOCAL_53216, INVENTORY_MODE_CLOUD_FALLBACK) else INVENTORY_MODE_LOCAL_53216
    if resolved_mode != mode:
        LOGGER.debug("Unknown Pixie inventory mode '%s', defaulting to %s", mode, resolved_mode)
    return resolved_mode


def _entry_username(entry: ConfigEntry) -> str:
    return str(entry.data.get(CONF_PIXIE_USERNAME) or "")


def _entry_password(entry: ConfigEntry) -> str:
    return str(entry.data.get(CONF_PIXIE_PASSWORD) or "")


async def _async_update_entry_runtime_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cloud_params: CloudParams,
    *,
    inventory_mode: str,
    username: str,
    password: str,
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
        }
    )
    if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
        data[CONF_PIXIE_USERNAME] = username
        data[CONF_PIXIE_PASSWORD] = password
    else:
        data.pop(CONF_PIXIE_USERNAME, None)
        data.pop(CONF_PIXIE_PASSWORD, None)
    hass.config_entries.async_update_entry(entry, data=data)
    LOGGER.debug(
        "Updated Pixie config entry %s for inventory mode %s%s",
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
        if record.model_no == "0102":
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
        if record.version is not None:
            kwargs["sw_version"] = str(record.version)
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
        runtime_session = self.runtime_data.pixie_runtime.runtime_session
        if runtime_session is None or not runtime_session.is_alive():
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
        if record.version is not None:
            info["sw_version"] = str(record.version)
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
                raise UpdateFailed(f"Pixie runtime unavailable: {err}") from err

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
                last_poll = rec.runtime.last_timer_poll_at
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
                LOGGER.debug("Queued timer poll for device %s", device_id)

        return inventory


@dataclass
class PixiePlusConfigEntryRuntimeData:
    """Objects stored in ConfigEntry.runtime_data."""

    handler: PixieAuthHandler
    cloud_params: CloudParams
    pixie_runtime: PixieRuntimeData
    coordinator: PixiePlusRuntimeCoordinator
    entry: ConfigEntry
    restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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

    def push_inventory_update_from_thread(self, inventory: PixieInventory) -> None:
        """Push a runtime inventory update to HA from the TCP worker thread."""
        self.pixie_runtime.inventory = inventory
        self.coordinator.hass.loop.call_soon_threadsafe(
            self.coordinator.async_set_updated_data,
            inventory,
        )
        self.coordinator.hass.loop.call_soon_threadsafe(
            self.coordinator.hass.async_create_task,
            _async_save_inventory_snapshot(self.coordinator.hass, self.entry, inventory),
        )
        # If a timer device needs an immediate poll (external mode change or
        # turn-on), schedule it now instead of waiting for the next coordinator
        # cycle (which can be up to 10 s away).
        for device_id in sorted(inventory.devices_by_id):
            rec = inventory.devices_by_id[device_id]
            if rec.capabilities.supports_timer and rec.runtime.timer_needs_poll:
                rec.runtime.timer_needs_poll = False
                self.coordinator.hass.loop.call_soon_threadsafe(
                    self.coordinator.hass.async_create_task,
                    self.async_send_local_command(
                        self.coordinator.hass,
                        command_device_id=device_id,
                        command_timer_action="poll",
                    ),
                )
                LOGGER.debug("Immediate timer poll for device %s (external change)", device_id)

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
                    "Restarting Pixie runtime (%s): %s",
                    reason,
                    self._describe_runtime_session(runtime_session),
                )
                await hass.async_add_executor_job(runtime_session.stop_and_join, 5.0)
            else:
                LOGGER.info("Starting Pixie runtime (%s)", reason)

            restart_handler = PixieAuthHandler()
            restart_handler.inventory = self.pixie_runtime.inventory
            restart_handler.gateway_identity = self.pixie_runtime.inventory.gateway if self.pixie_runtime.inventory else None
            restart_handler.set_inventory_update_callback(self.push_inventory_update_from_thread)

            username = _entry_username(self.entry)
            password = _entry_password(self.entry)
            inventory_mode = _entry_inventory_mode(self.entry)

            try:
                restarted_runtime = await restart_handler.async_bootstrap_gateway(
                    self.cloud_params,
                    username=username,
                    password=password,
                    keep_control_alive=True,
                    wait_for_shutdown=False,
                    hydrate_inventory=False,
                )
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
            self.pixie_runtime.inventory_mode = inventory_mode
            if restarted_runtime.inventory is not None:
                self.pixie_runtime.inventory = restarted_runtime.inventory

            LOGGER.info(
                "Pixie runtime ready after %s: %s",
                reason,
                self._describe_runtime_session(self.pixie_runtime.runtime_session),
            )
            return self.pixie_runtime.runtime_session

    async def async_shutdown(self, hass: HomeAssistant) -> None:
        """Stop the long-lived gateway runtime session."""
        async with self.restart_lock:
            runtime_session = self.pixie_runtime.runtime_session
            if runtime_session is None:
                return

            await hass.async_add_executor_job(runtime_session.stop_and_join, 5.0)

    async def async_send_local_command(self, hass: HomeAssistant, **kwargs) -> None:
        """Send a local command using the single shared 41578 runtime session.

        Passes through all kwargs including timer-specific ones:
        - command_timer_action: "restart", "override", "set_duration", "poll"
        - command_timer_duration: int (seconds, 1-86400)
        """
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
                LOGGER.warning("Live Pixie runtime command failed; restarting shared runtime: %s", err)
                recovered_session = await self.async_ensure_runtime(
                    hass,
                    reason="command_send_recovery",
                )
                await hass.async_add_executor_job(recovered_session.send_command, dict(kwargs))
                self.coordinator.async_set_updated_data(self.pixie_runtime.inventory)
                return

            LOGGER.warning("Live Pixie runtime command failed on shared runtime: %s", err)
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

    LOGGER.debug(
        "Bootstrapping Pixie entry %s in %s mode%s",
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

    async def _async_start_snapshot_runtime(
        snapshot_inventory: PixieInventory,
        *,
        runtime_mode: str,
    ) -> tuple[PixieAuthHandler, PixieRuntimeData]:
        snapshot_handler = PixieAuthHandler()
        snapshot_handler.inventory = snapshot_inventory
        snapshot_handler.gateway_identity = snapshot_inventory.gateway
        snapshot_runtime = await snapshot_handler.async_bootstrap_gateway(
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
        local_runtime = await local_handler.async_bootstrap_gateway(
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
        )
        fallback_runtime = await fallback_handler.async_bootstrap_gateway(
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
        LOGGER.debug("Trying direct local Pixie inventory startup for entry %s", entry.entry_id)
        handler, pixie_runtime = await _async_start_local_inventory_runtime()

        if pixie_runtime.inventory is not None:
            _async_delete_missing_credentials_issue(hass, entry)
            if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
                LOGGER.info("Pixie entry %s recovered direct local inventory; switching to local_53216 mode", entry.entry_id)
                await _async_update_entry_runtime_data(
                    hass,
                    entry,
                    cloud_params,
                    inventory_mode=INVENTORY_MODE_LOCAL_53216,
                    username="",
                    password="",
                )
            pixie_runtime.inventory_mode = INVENTORY_MODE_LOCAL_53216
        else:
            LOGGER.warning("Direct local Pixie inventory startup failed for entry %s", entry.entry_id)
            await _shutdown_runtime(handler)

            if username and password:
                try:
                    handler, pixie_runtime, cloud_params = await _async_start_cloud_fallback_runtime()
                    _async_delete_missing_credentials_issue(hass, entry)
                    if inventory_mode != INVENTORY_MODE_CLOUD_FALLBACK:
                        LOGGER.warning(
                            "Pixie direct local inventory failed; switching entry %s to cloud fallback mode",
                            entry.entry_id,
                        )
                    await _async_update_entry_runtime_data(
                        hass,
                        entry,
                        _handler_cloud_params(handler, cloud_params),
                        inventory_mode=INVENTORY_MODE_CLOUD_FALLBACK,
                        username=username,
                        password=password,
                    )
                    cloud_params = _handler_cloud_params(handler, cloud_params)
                except Exception as err:
                    if persisted_inventory is None:
                        raise ConfigEntryNotReady(
                            f"Pixie live inventory unavailable and no stored inventory snapshot exists: {err}"
                        ) from err
                    LOGGER.warning("Pixie live inventory failed; using stored inventory snapshot: %s", err)
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
                    "Direct local Pixie inventory failed with no stored Pixie credentials; using stored inventory snapshot"
                )
                _async_create_missing_credentials_issue(hass, entry)
                handler, pixie_runtime = await _async_start_snapshot_runtime(
                    persisted_inventory,
                    runtime_mode=inventory_mode,
                )

        coordinator = PixiePlusRuntimeCoordinator(hass, entry, pixie_runtime)
        await coordinator.async_config_entry_first_refresh()
        await _async_save_inventory_snapshot(hass, entry, pixie_runtime.inventory)
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
    )
    coordinator.runtime_manager = runtime_data
    handler.set_inventory_update_callback(runtime_data.push_inventory_update_from_thread)
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
            "usb", "sensor_light_state",
        )
        valid_entity_ids: set[str] = set()
        valid_device_ids: set[str] = {gateway_device_identifier(inv)}
        for device_id in inv.devices_by_id:
            record = inv.devices_by_id[device_id]
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
            LOGGER.debug("Removed orphaned entity: %s", entity_id)

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
            LOGGER.debug("Removed orphaned device: %s", device_id)

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
    return True