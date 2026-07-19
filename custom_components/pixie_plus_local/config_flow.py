"""Config flow for Pixie Plus Local."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from ipaddress import IPv4Address
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigFlow, FlowType, OptionsFlowWithReload
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import (
    CONF_GATEWAY_IP,
    CONF_GATEWAY_IP_REQUIRED,
    CONF_INVENTORY_FALLBACK_REASON,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_INVENTORY_MODE,
    CONF_MESHNET,
    CONF_MESHNET2,
    CONF_NETID,
    CONF_PIXIE_PASSWORD,
    CONF_PIXIE_USERNAME,
    CONF_USER_ID,
    CONF_BT_ACCESS_NODE,
    CONF_BT_BETTER_CANDIDATE_SEEN,
    CONF_BT_ENABLED,
    CONF_BT_SOURCE,
    CONF_BT_STATE,
    CONF_BT_ACCESS_NODE_PREFERENCE,
    CONF_COMMAND_TRANSPORT,
    DOMAIN,
    INVENTORY_MODE_CLOUD_FALLBACK,
    INVENTORY_MODE_LOCAL_53216,
    INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED,
    INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY,
    COMMAND_TRANSPORT_BT_ONLY,
    COMMAND_TRANSPORT_BT_PRIMARY,
    COMMAND_TRANSPORT_TCP_ONLY,
    COMMAND_TRANSPORT_TCP_PRIMARY,
    BT_ACCESS_NODE_AUTO,
    BT_ACCESS_NODE_PREFER_GATEWAY,
    _async_delete_missing_credentials_issue,
    _async_delete_gateway_ip_issue,
    _async_run_global_ble_version_scan,
    _entry_gateway_supports_local_inventory_53216,
    _entry_inventory_mode,
    _entry_bt_enabled,
)
from .pixie_ble import (
    BT_STATE_DISABLED,
    BT_STATE_NO_WORKING_PROXY,
    BT_STATE_READY,
    async_probe_pixie_bluetooth_proxy,
)
from .pixie_runtime import (
    CloudHomeList,
    CloudParams,
    PixieAuthError,
    PixieAuthHandler,
    PixieGatewayResolutionError,
)
from .pixie_value_profiles import (
    COVER_ACTION_TO_POSITION_DEFAULT,
    COVER_TILT_ACTION_TO_POSITION_DEFAULT,
)

LOGGER = logging.getLogger(__name__)

INTEGRATION_TITLE = "Pixie Plus Local"

CONF_COVER_CONTROLLER_MAPS = "cover_controller_maps"
CONF_COVER_CONTROLLER_ID = "cover_controller_id"
CONF_COVER_ACTION_MAP = "cover_action_map"
CONF_COVER_TILT_ACTION_MAP = "cover_tilt_action_map"

CONF_COVER_OPEN_POSITION = "cover_open_position"
CONF_COVER_STOP_POSITION = "cover_stop_position"
CONF_COVER_CLOSE_POSITION = "cover_close_position"
CONF_COVER_OPEN_TILT_POSITION = "cover_open_tilt_position"
CONF_COVER_STOP_TILT_POSITION = "cover_stop_tilt_position"
CONF_COVER_CLOSE_TILT_POSITION = "cover_close_tilt_position"
CONF_GATEWAY_CONNECTION_MODE = "gateway_connection_mode"
CONF_SELECTED_HOME_ID = "selected_home_id"
CONF_EXCLUDE_HOME_IDS = "_exclude_home_ids"
CONF_ALLOW_FINISH_SETUP = "_allow_finish_setup"
CONF_ENABLE_BT = "enable_bt"
CONF_ENABLE_BT_LABEL = "Enable Bluetooth support (requires ESPHome Bluetooth proxy)"
FINISH_SETUP_VALUE = "__finish_setup__"
BT_INSTALL_PROBE_TIMEOUT = 75.0

GATEWAY_CONNECTION_MODE_AUTO = "auto"
GATEWAY_CONNECTION_MODE_MANUAL = "manual"


def _enable_bt_from_user_input(user_input: dict[str, Any]) -> bool:
    """Return the Bluetooth checkbox value from translated or fallback form keys."""
    return bool(user_input.get(CONF_ENABLE_BT_LABEL, user_input.get(CONF_ENABLE_BT, False)))


def _bluetooth_data_schema(*, default: bool) -> vol.Schema:
    """Build a Bluetooth form schema with a readable fallback field label."""
    return vol.Schema({vol.Required(CONF_ENABLE_BT_LABEL, default=default): bool})


def _flow_home_log_prefix(data: dict[str, Any] | None, fallback: str | None = None) -> str:
    home_name = ""
    if isinstance(data, dict):
        home_name = str(data.get(CONF_HOME_NAME) or "").strip()
    home_name = home_name or str(fallback or "").strip()
    if home_name and home_name not in ("unknown", "None"):
        return f"[{home_name}] "
    return ""


async def _async_probe_bt_for_flow(
    hass: Any,
    cloud_params: CloudParams,
    inventory: Any | None = None,
    *,
    preferred_source: str | None = None,
    preferred_access_node: str | None = None,
):
    """Run a blocking Pixie BLE capability probe for config flows."""
    return await async_probe_pixie_bluetooth_proxy(
        hass,
        cloud_params,
        inventory,
        preferred_source=preferred_source,
        preferred_access_node=preferred_access_node,
        timeout=BT_INSTALL_PROBE_TIMEOUT,
    )


def _cloud_params_from_entry_data(data: dict[str, Any], title: str = INTEGRATION_TITLE) -> CloudParams:
    """Build cloud params from already validated config-entry data."""
    return CloudParams(
        home_id=str(data[CONF_HOME_ID]),
        home_name=str(data.get(CONF_HOME_NAME) or title),
        user_id=str(data[CONF_USER_ID]),
        meshnet=str(data[CONF_MESHNET]),
        meshnet2=str(data[CONF_MESHNET2]),
        netid=str(data[CONF_NETID]),
    )


async def _async_apply_bluetooth_choice(
    hass: Any,
    *,
    data: dict[str, Any],
    options: dict[str, Any] | None,
    enable_bt: bool,
    cloud_params: CloudParams,
    inventory: Any | None,
    log_label: str,
    preferred_source: str | None = None,
    preferred_access_node: str | None = None,
) -> str | None:
    """Apply the BT enable choice to entry data; return an error key if probing fails."""
    data[CONF_BT_ENABLED] = False
    data[CONF_BT_STATE] = BT_STATE_DISABLED
    data.pop(CONF_BT_SOURCE, None)
    data.pop(CONF_BT_ACCESS_NODE, None)
    data.pop("bt_response_access_node", None)
    data.pop("bt_access_nodes", None)
    data.pop(CONF_BT_BETTER_CANDIDATE_SEEN, None)

    if not enable_bt:
        if options is not None:
            options[CONF_COMMAND_TRANSPORT] = COMMAND_TRANSPORT_TCP_PRIMARY
            options.pop(CONF_BT_ACCESS_NODE_PREFERENCE, None)
        return None

    if options is not None:
        options.setdefault(CONF_COMMAND_TRANSPORT, COMMAND_TRANSPORT_TCP_PRIMARY)
        options.setdefault(CONF_BT_ACCESS_NODE_PREFERENCE, BT_ACCESS_NODE_AUTO)

    probe = await _async_probe_bt_for_flow(
        hass,
        cloud_params,
        inventory,
        preferred_source=preferred_source,
        preferred_access_node=preferred_access_node,
    )
    if probe is not None and probe.healthy:
        prefix = _flow_home_log_prefix(None, cloud_params.home_name)
        LOGGER.info(
            "%sPixie Bluetooth %s accepted source=%s access_node=%s state=%s",
            prefix,
            log_label,
            probe.source,
            probe.access_node,
            probe.state,
        )
        data[CONF_BT_ENABLED] = True
        data[CONF_BT_STATE] = BT_STATE_READY
        if probe.source:
            data[CONF_BT_SOURCE] = probe.source
        if probe.access_node:
            data[CONF_BT_ACCESS_NODE] = probe.access_node
        return None

    prefix = _flow_home_log_prefix(None, cloud_params.home_name)
    LOGGER.warning(
        "%sPixie Bluetooth %s rejected probe=%s state=%s source=%s access_node=%s error=%s",
        prefix,
        log_label,
        probe is not None,
        getattr(probe, "state", None),
        getattr(probe, "source", None),
        getattr(probe, "access_node", None),
        getattr(probe, "last_error", None),
    )
    data[CONF_BT_STATE] = BT_STATE_NO_WORKING_PROXY
    return "bt_proxy_unavailable"


class InvalidAuth(Exception):
    """Authentication failed."""


class CannotConnect(Exception):
    """Connection or bootstrap failed."""


class GatewayIpRequired(Exception):
    """Auto-discovery did not resolve a gateway host."""


@dataclass
class ValidatedSetup:
    """Validated config-entry payload prepared during the flow."""

    title: str
    data: dict[str, Any]
    options: dict[str, Any]
    inventory: Any | None
    has_cover_devices: bool
    cover_devices: dict[str, str]
    inventory_fallback_reason: str | None = None
    inventory_fallback_notice_shown: bool = False


def _is_known_cloud_value(value: Any) -> bool:
    """Return True when a cloud metadata field is populated."""
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized not in ("", "unknown", "none")


def _home_id(home_obj: dict[str, Any]) -> str:
    """Return a Home object's id as a string."""
    return str(home_obj.get("objectId") or "")


def _home_label(home_obj: dict[str, Any]) -> str:
    """Return a readable Home picker label."""
    return str(home_obj.get("name") or "Unnamed home")


def _cloud_params_from_home_obj(home_obj: dict[str, Any], user_id: str) -> CloudParams:
    """Build CloudParams from a selected cloud Home object."""
    home_id = _home_id(home_obj)
    return CloudParams(
        home_id=home_id,
        home_name=str(home_obj.get("name") or "unknown"),
        user_id=str(user_id or "unknown"),
        meshnet=str(home_obj.get("meshNet") if home_obj.get("meshNet") is not None else home_id),
        meshnet2=str(home_obj.get("meshNet2") if home_obj.get("meshNet2") is not None else "unknown"),
        netid=str(home_obj.get("netID") if home_obj.get("netID") is not None else "unknown"),
    )


def _number_selector() -> NumberSelector:
    """Return the selector used for blind button positions."""
    return NumberSelector(
        NumberSelectorConfig(
            min=1,
            max=9,
            step=1,
            mode=NumberSelectorMode.BOX,
        )
    )


def _cover_mapping_schema() -> vol.Schema:
    """Schema for blind button mapping."""
    return vol.Schema(
        {
            vol.Required(CONF_COVER_OPEN_POSITION): _number_selector(),
            vol.Required(CONF_COVER_STOP_POSITION): _number_selector(),
            vol.Required(CONF_COVER_CLOSE_POSITION): _number_selector(),
            vol.Optional(CONF_COVER_OPEN_TILT_POSITION): _number_selector(),
            vol.Optional(CONF_COVER_STOP_TILT_POSITION): _number_selector(),
            vol.Optional(CONF_COVER_CLOSE_TILT_POSITION): _number_selector(),
        }
    )


def _cover_controller_choices(inventory) -> dict[str, str]:
    """Return selectable cover-controller choices keyed by device id."""
    if inventory is None:
        return {}

    choices: dict[str, str] = {}
    for device_id in sorted(inventory.devices_by_id):
        record = inventory.devices_by_id[device_id]
        if record.capabilities.cover_type != "blind":
            continue
        choices[str(record.id)] = f"{record.name} ({record.id})"
    return choices


def get_cover_mapping_for_controller(
    options: dict[str, Any],
    controller_id: str | int,
) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    """Return the configured mapping for one blind controller."""
    controller_maps = options.get(CONF_COVER_CONTROLLER_MAPS) or {}
    controller_entry = controller_maps.get(str(controller_id)) if isinstance(controller_maps, dict) else None

    action_map = None
    tilt_map = None
    if isinstance(controller_entry, dict):
        raw_action_map = controller_entry.get(CONF_COVER_ACTION_MAP)
        raw_tilt_map = controller_entry.get(CONF_COVER_TILT_ACTION_MAP)
        if isinstance(raw_action_map, dict):
            action_map = raw_action_map
        if isinstance(raw_tilt_map, dict):
            tilt_map = raw_tilt_map

    if action_map is None:
        raw_action_map = options.get(CONF_COVER_ACTION_MAP)
        if isinstance(raw_action_map, dict):
            action_map = raw_action_map
    if tilt_map is None:
        raw_tilt_map = options.get(CONF_COVER_TILT_ACTION_MAP)
        if isinstance(raw_tilt_map, dict):
            tilt_map = raw_tilt_map

    return action_map, tilt_map


def _cover_mapping_suggested_values(
    options: dict[str, Any],
    controller_id: str | int,
) -> dict[str, Any]:
    """Build UI suggested values from persisted or default cover mappings."""
    action_map, tilt_map = get_cover_mapping_for_controller(options, controller_id)
    action_map = action_map or COVER_ACTION_TO_POSITION_DEFAULT
    tilt_map = tilt_map or COVER_TILT_ACTION_TO_POSITION_DEFAULT

    return {
        CONF_COVER_OPEN_POSITION: action_map.get("open", action_map.get("up")),
        CONF_COVER_STOP_POSITION: action_map.get("stop"),
        CONF_COVER_CLOSE_POSITION: action_map.get("close", action_map.get("down")),
        CONF_COVER_OPEN_TILT_POSITION: tilt_map.get("open_tilt"),
        CONF_COVER_STOP_TILT_POSITION: tilt_map.get("stop_tilt"),
        CONF_COVER_CLOSE_TILT_POSITION: tilt_map.get("close_tilt"),
    }


def _cover_options_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Convert UI values into persisted cover mapping options."""
    open_position = int(user_input[CONF_COVER_OPEN_POSITION])
    stop_position = int(user_input[CONF_COVER_STOP_POSITION])
    close_position = int(user_input[CONF_COVER_CLOSE_POSITION])

    action_map = {
        "open": open_position,
        "up": open_position,
        "stop": stop_position,
        "close": close_position,
        "down": close_position,
    }

    tilt_map: dict[str, int] = {}
    for option_key, action_key in (
        (CONF_COVER_OPEN_TILT_POSITION, "open_tilt"),
        (CONF_COVER_STOP_TILT_POSITION, "stop_tilt"),
        (CONF_COVER_CLOSE_TILT_POSITION, "close_tilt"),
    ):
        value = user_input.get(option_key)
        if value in (None, ""):
            continue
        tilt_map[action_key] = int(value)

    return {
        CONF_COVER_ACTION_MAP: action_map,
        CONF_COVER_TILT_ACTION_MAP: tilt_map,
    }


def _cover_controller_options_from_input(
    controller_id: str | int,
    user_input: dict[str, Any],
    existing_options: dict[str, Any],
) -> dict[str, Any]:
    """Persist one controller's mapping into entry options."""
    merged_options = dict(existing_options)
    controller_maps = dict(merged_options.get(CONF_COVER_CONTROLLER_MAPS) or {})
    controller_maps[str(controller_id)] = _cover_options_from_input(user_input)
    merged_options[CONF_COVER_CONTROLLER_MAPS] = controller_maps
    return merged_options


def _has_cover_devices(handler: PixieAuthHandler) -> bool:
    """Return True when the seeded or bootstrapped inventory includes covers."""
    inventory = handler.inventory
    if inventory is None:
        return False

    return any(device.capabilities.cover_type == "blind" for device in inventory.devices_by_id.values())


def _build_entry_title(handler: PixieAuthHandler, cloud_params: CloudParams) -> str:
    """Generate a stable, readable entry title."""
    if handler.inventory is not None and handler.inventory.home_name:
        return handler.inventory.home_name
    if cloud_params.home_name and cloud_params.home_name not in ("unknown", "None"):
        return cloud_params.home_name
    return INTEGRATION_TITLE


def _build_entry_data(cloud_params: CloudParams) -> dict[str, Any]:
    """Build the immutable config-entry data payload."""
    return {
        CONF_HOME_ID: cloud_params.home_id,
        CONF_HOME_NAME: cloud_params.home_name,
        CONF_USER_ID: cloud_params.user_id,
        CONF_MESHNET: cloud_params.meshnet,
        CONF_MESHNET2: cloud_params.meshnet2,
        CONF_NETID: cloud_params.netid,
    }


def _build_entry_data_with_mode(
    cloud_params: CloudParams,
    *,
    inventory_mode: str,
    username: str,
    password: str,
    gateway_ip_required: bool,
    gateway_ip: str | None,
    inventory_fallback_reason: str | None = None,
) -> dict[str, Any]:
    data = _build_entry_data(cloud_params)
    data[CONF_INVENTORY_MODE] = inventory_mode
    data[CONF_GATEWAY_IP_REQUIRED] = gateway_ip_required
    if gateway_ip:
        data[CONF_GATEWAY_IP] = gateway_ip
    if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
        data[CONF_PIXIE_USERNAME] = username
        data[CONF_PIXIE_PASSWORD] = password
        if inventory_fallback_reason:
            data[CONF_INVENTORY_FALLBACK_REASON] = inventory_fallback_reason
    return data


def _inventory_fallback_reason_for_inventory(inventory: Any | None) -> str:
    """Return the config-entry reason for a cloud-fallback inventory setup."""
    gateway = getattr(inventory, "gateway", None)
    if gateway is not None and not bool(getattr(gateway, "supports_local_inventory_53216", True)):
        return INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY
    return INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED


def _inventory_fallback_reason_text(reason: str) -> str:
    """Return a human-readable local-inventory fallback reason."""
    if reason == INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY:
        return "the Pixie gateway model does not support local inventory over port 53216"
    return "direct local inventory over port 53216 was unavailable during setup"


def _normalize_gateway_ip(value: Any) -> str:
    return str(IPv4Address(str(value).strip()))


def _entry_gateway_ip_required(entry: ConfigEntry) -> bool:
    value = entry.data.get(CONF_GATEWAY_IP_REQUIRED)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _entry_gateway_ip(entry: ConfigEntry) -> str | None:
    value = str(entry.data.get(CONF_GATEWAY_IP) or "").strip()
    return value or None


def _entry_cloud_params(entry: ConfigEntry) -> CloudParams:
    return CloudParams(
        home_id=str(entry.data[CONF_HOME_ID]),
        home_name=str(entry.data.get(CONF_HOME_NAME) or entry.title or INTEGRATION_TITLE),
        user_id=str(entry.data[CONF_USER_ID]),
        meshnet=str(entry.data[CONF_MESHNET]),
        meshnet2=str(entry.data[CONF_MESHNET2]),
        netid=str(entry.data[CONF_NETID]),
    )


async def _async_validate_setup_input(
    user_input: dict[str, Any],
    *,
    gateway_ip: str | None = None,
    selected_home_id: str | None = None,
) -> ValidatedSetup:
    """Validate credentials, derive runtime params, and verify local bootstrap."""
    username = str(user_input[CONF_USERNAME]).strip()
    password = str(user_input[CONF_PASSWORD])

    handler = PixieAuthHandler()

    try:
        cloud_params = await handler.async_fetch_cloud_params(
            username,
            password,
            include_inventory_seed=True,
            selected_home_id=selected_home_id,
        )
    except PixieAuthError as err:
        raise InvalidAuth from err
    except Exception as err:
        raise CannotConnect from err

    if not _is_known_cloud_value(cloud_params.netid):
        raise CannotConnect("Cloud login did not return a usable netID")
    if not (
        _is_known_cloud_value(cloud_params.meshnet)
        or _is_known_cloud_value(cloud_params.meshnet2)
    ):
        raise CannotConnect("Cloud login did not return usable mesh metadata")

    try:
        await handler.async_bootstrap_gateway(
            cloud_params,
            username=username,
            password=password,
            gateway_ip=gateway_ip,
            keep_control_alive=False,
            wait_for_shutdown=False,
        )
    except PixieGatewayResolutionError as err:
        if gateway_ip is None:
            raise GatewayIpRequired from err
        raise CannotConnect from err
    except PixieAuthError as err:
        raise CannotConnect from err
    except Exception as err:
        raise CannotConnect from err
    finally:
        if handler.runtime_session is not None:
            await asyncio.to_thread(handler.runtime_session.stop_and_join, 5.0)

    inventory_fallback_reason = None
    if handler.inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
        inventory_fallback_reason = _inventory_fallback_reason_for_inventory(handler.inventory)
        LOGGER.warning(
            "%sPixie Plus Local is using cloud-assisted inventory mode because %s",
            _flow_home_log_prefix(None, cloud_params.home_name),
            _inventory_fallback_reason_text(inventory_fallback_reason),
        )

    has_cover_devices = _has_cover_devices(handler)
    options: dict[str, Any] = {}
    cover_devices = _cover_controller_choices(handler.inventory)
    verified_gateway_ip = None
    if isinstance(handler.current_hub, dict):
        verified_gateway_ip = str(handler.current_hub.get("host") or "") or None
    verified_gateway_ip = verified_gateway_ip or gateway_ip

    return ValidatedSetup(
        title=_build_entry_title(handler, cloud_params),
        data=_build_entry_data_with_mode(
            cloud_params,
            inventory_mode=handler.inventory_mode,
            username=username,
            password=password,
            gateway_ip_required=gateway_ip is not None,
            gateway_ip=verified_gateway_ip,
            inventory_fallback_reason=inventory_fallback_reason,
        ),
        options=options,
        inventory=handler.inventory,
        has_cover_devices=has_cover_devices,
        cover_devices=cover_devices,
        inventory_fallback_reason=inventory_fallback_reason,
    )


class PixiePlusLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pixie Plus Local."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._validated_setup: ValidatedSetup | None = None
        self._selected_cover_controller_id: str | None = None
        self._pending_user_input: dict[str, Any] | None = None
        self._pending_home_id: str | None = None
        self._cloud_home_list: CloudHomeList | None = None
        self._available_homes: list[dict[str, Any]] = []
        self._exclude_home_ids: set[str] = set()
        self._allow_finish_setup = False

    async def _async_finish_validated_setup(self):
        """Continue to the remaining setup steps after validation succeeds."""
        if self._validated_setup is None:
            return await self.async_step_user()

        await self.async_set_unique_id(self._validated_setup.data[CONF_HOME_ID])
        self._abort_if_unique_id_configured()

        if (
            self._validated_setup.data.get(CONF_INVENTORY_MODE) == INVENTORY_MODE_CLOUD_FALLBACK
            and not self._validated_setup.inventory_fallback_notice_shown
        ):
            return await self.async_step_inventory_fallback_notice()

        if CONF_BT_ENABLED not in self._validated_setup.data:
            return await self.async_step_bluetooth()

        if self._validated_setup.has_cover_devices:
            return await self.async_step_cover_controller()

        return await self._async_create_validated_entry()

    async def _async_create_validated_entry(self):
        """Create the validated config entry and optionally start another Home flow."""
        if self._validated_setup is None:
            return self.async_abort(reason="unknown")

        await self.async_set_unique_id(self._validated_setup.data[CONF_HOME_ID])
        self._abort_if_unique_id_configured()

        next_flow: tuple[FlowType, str] | None = None
        if self._pending_user_input is not None and self._remaining_homes_after_current():
            username = str(self._pending_user_input.get(CONF_USERNAME) or "").strip()
            password = str(self._pending_user_input.get(CONF_PASSWORD) or "")
            exclude_home_ids = sorted(self._configured_home_ids() | {str(self._validated_setup.data[CONF_HOME_ID])})
            result = await self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_USER},
                data={
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    CONF_EXCLUDE_HOME_IDS: exclude_home_ids,
                    CONF_ALLOW_FINISH_SETUP: True,
                },
            )
            flow_id = result.get("flow_id")
            if isinstance(flow_id, str):
                next_flow = (FlowType.CONFIG_FLOW, flow_id)

        return self.async_create_entry(
            title=self._validated_setup.title,
            data=self._validated_setup.data,
            options=self._validated_setup.options,
            next_flow=next_flow,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PixiePlusLocalOptionsFlow:
        """Create the options flow."""
        return PixiePlusLocalOptionsFlow()

    def _configured_home_ids(self) -> set[str]:
        """Return Home ids already configured in Home Assistant."""
        configured: set[str] = set()
        for entry in self._async_current_entries():
            if entry.unique_id:
                configured.add(str(entry.unique_id))
            if entry.data.get(CONF_HOME_ID):
                configured.add(str(entry.data[CONF_HOME_ID]))
        configured.update(self._exclude_home_ids)
        return configured

    def _unconfigured_homes(self, home_list: CloudHomeList) -> list[dict[str, Any]]:
        """Return visible Homes not already configured."""
        configured = self._configured_home_ids()
        homes = [home for home in home_list.homes if _home_id(home) and _home_id(home) not in configured]
        if home_list.current_home_id:
            homes.sort(key=lambda home: 0 if _home_id(home) == home_list.current_home_id else 1)
        return homes

    def _remaining_homes_after_current(self) -> list[dict[str, Any]]:
        """Return Homes that could still be added after the current validated setup."""
        if self._cloud_home_list is None or self._validated_setup is None:
            return []
        configured = self._configured_home_ids() | {str(self._validated_setup.data[CONF_HOME_ID])}
        return [home for home in self._cloud_home_list.homes if _home_id(home) and _home_id(home) not in configured]

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._pending_user_input = dict(user_input)
            self._pending_home_id = None
            raw_exclude_home_ids = user_input.get(CONF_EXCLUDE_HOME_IDS)
            if isinstance(raw_exclude_home_ids, (list, tuple, set)):
                self._exclude_home_ids = {str(home_id) for home_id in raw_exclude_home_ids if str(home_id)}
            self._allow_finish_setup = bool(user_input.get(CONF_ALLOW_FINISH_SETUP, False))
            handler = PixieAuthHandler()
            try:
                self._cloud_home_list = await handler.async_fetch_cloud_home_list(
                    str(user_input[CONF_USERNAME]).strip(),
                    str(user_input[CONF_PASSWORD]),
                )
                self._available_homes = self._unconfigured_homes(self._cloud_home_list)
            except PixieAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                LOGGER.exception("Unexpected Pixie Plus Local cloud home lookup failure")
                errors["base"] = "cannot_connect"
            else:
                if not self._available_homes:
                    return self.async_abort(reason="already_configured")
                if self._allow_finish_setup or len(self._cloud_home_list.homes) > 1:
                    return await self.async_step_home()

                self._pending_home_id = _home_id(self._available_homes[0])
                try:
                    self._validated_setup = await _async_validate_setup_input(
                        user_input,
                        selected_home_id=self._pending_home_id,
                    )
                except GatewayIpRequired:
                    return await self.async_step_gateway_ip()
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    LOGGER.exception("Unexpected Pixie Plus Local setup failure")
                    errors["base"] = "unknown"
                else:
                    return await self._async_finish_validated_setup()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        autocomplete="username",
                    )
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

    async def async_step_home(self, user_input: dict[str, Any] | None = None):
        """Choose which Pixie Home to add."""
        if self._pending_user_input is None or not self._available_homes:
            return await self.async_step_user()

        errors: dict[str, str] = {}

        if user_input is not None:
            self._pending_home_id = str(user_input[CONF_SELECTED_HOME_ID])
            if self._pending_home_id == FINISH_SETUP_VALUE:
                return self.async_abort(reason="setup_complete")
            try:
                self._validated_setup = await _async_validate_setup_input(
                    self._pending_user_input,
                    selected_home_id=self._pending_home_id,
                )
            except GatewayIpRequired:
                return await self.async_step_gateway_ip()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected Pixie Plus Local setup failure for selected home")
                errors["base"] = "unknown"
            else:
                return await self._async_finish_validated_setup()

        choices = {_home_id(home): _home_label(home) for home in self._available_homes}
        if self._allow_finish_setup:
            choices[FINISH_SETUP_VALUE] = "Finish setup"
        data_schema = vol.Schema(
            {
                vol.Required(CONF_SELECTED_HOME_ID): vol.In(choices),
            }
        )
        return self.async_show_form(
            step_id="home",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"finish_text": ", or finish setup" if self._allow_finish_setup else ""},
        )

    async def async_step_inventory_fallback_notice(self, user_input: dict[str, Any] | None = None):
        """Explain why Pixie credentials must be stored for inventory fallback."""
        if self._validated_setup is None:
            return await self.async_step_user()

        if user_input is not None:
            self._validated_setup.inventory_fallback_notice_shown = True
            return await self._async_finish_validated_setup()

        reason = self._validated_setup.inventory_fallback_reason or INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED
        reason_text = (
            "this Pixie gateway does not provide local inventory"
            if reason == INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY
            else "local inventory over port 53216 did not work"
        )
        return self.async_show_form(
            step_id="inventory_fallback_notice",
            data_schema=vol.Schema({}),
            description_placeholders={"reason": reason_text},
        )

    async def async_step_gateway_ip(self, user_input: dict[str, Any] | None = None):
        """Collect a manual gateway IP when UDP discovery does not find a gateway."""
        if self._pending_user_input is None:
            return await self.async_step_user()

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                gateway_ip = _normalize_gateway_ip(user_input[CONF_GATEWAY_IP])
            except ValueError:
                errors[CONF_GATEWAY_IP] = "invalid_gateway_ip"
            else:
                try:
                    self._validated_setup = await _async_validate_setup_input(
                        self._pending_user_input,
                        gateway_ip=gateway_ip,
                        selected_home_id=self._pending_home_id,
                    )
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    LOGGER.exception("Unexpected Pixie Plus Local manual-IP setup failure")
                    errors["base"] = "unknown"
                else:
                    return await self._async_finish_validated_setup()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_GATEWAY_IP): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="gateway_ip", data_schema=data_schema, errors=errors)

    async def async_step_bluetooth(self, user_input: dict[str, Any] | None = None):
        """Ask whether to enable the optional Pixie Bluetooth pathway."""
        if self._validated_setup is None:
            return await self.async_step_user()

        errors: dict[str, str] = {}
        description_placeholders = {
            "home_name": str(self._validated_setup.data.get(CONF_HOME_NAME) or self._validated_setup.title),
        }

        if user_input is not None:
            enable_bt = _enable_bt_from_user_input(user_input)
            error = await _async_apply_bluetooth_choice(
                self.hass,
                data=self._validated_setup.data,
                options=self._validated_setup.options,
                enable_bt=enable_bt,
                cloud_params=_cloud_params_from_entry_data(self._validated_setup.data, self._validated_setup.title),
                inventory=self._validated_setup.inventory,
                log_label="setup",
            )
            if error is not None:
                errors["base"] = error
                data_schema = _bluetooth_data_schema(default=False)
                return self.async_show_form(
                    step_id="bluetooth",
                    data_schema=data_schema,
                    errors=errors,
                    description_placeholders=description_placeholders,
            )

            LOGGER.info(
                "%sPixie Bluetooth setup step completed enabled=%s state=%s",
                _flow_home_log_prefix(self._validated_setup.data),
                self._validated_setup.data.get(CONF_BT_ENABLED),
                self._validated_setup.data.get(CONF_BT_STATE),
            )
            return await self._async_finish_validated_setup()

        data_schema = _bluetooth_data_schema(default=False)
        return self.async_show_form(
            step_id="bluetooth",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Present reconfiguration actions for the config entry."""
        entry = self._get_reconfigure_entry()
        menu_options = ["reconfigure_credentials", "reconfigure_gateway_connection", "reconfigure_bluetooth"]
        runtime_data = getattr(entry, "runtime_data", None)
        pixie_runtime = getattr(runtime_data, "pixie_runtime", None) if runtime_data is not None else None
        inventory = getattr(pixie_runtime, "inventory", None) if pixie_runtime is not None else None
        if (
            _entry_inventory_mode(entry) == INVENTORY_MODE_CLOUD_FALLBACK
            and _entry_gateway_supports_local_inventory_53216(entry, inventory)
        ):
            menu_options.insert(1, "reconfigure_inventory_mode")
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=menu_options,
        )

    async def async_step_reconfigure_inventory_mode(self, user_input: dict[str, Any] | None = None):
        """Try returning a cloud-fallback entry to direct local inventory."""
        entry = self._get_reconfigure_entry()
        runtime_data = getattr(entry, "runtime_data", None)
        pixie_runtime = getattr(runtime_data, "pixie_runtime", None) if runtime_data is not None else None
        inventory = getattr(pixie_runtime, "inventory", None) if pixie_runtime is not None else None
        if not _entry_gateway_supports_local_inventory_53216(entry, inventory):
            return await self.async_step_reconfigure()

        errors: dict[str, str] = {}
        if user_input is not None:
            handler = PixieAuthHandler()
            try:
                await handler.async_bootstrap_gateway(
                    _entry_cloud_params(entry),
                    username="",
                    password="",
                    gateway_ip=_entry_gateway_ip(entry),
                    keep_control_alive=False,
                    wait_for_shutdown=False,
                )
            except PixieAuthError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected Pixie Plus Local local-inventory reconfigure failure")
                errors["base"] = "unknown"
            finally:
                if handler.runtime_session is not None:
                    await asyncio.to_thread(handler.runtime_session.stop_and_join, 5.0)

            if not errors and handler.inventory is not None:
                verified_gateway_ip = None
                if isinstance(handler.current_hub, dict):
                    verified_gateway_ip = str(handler.current_hub.get("host") or "") or None
                _async_delete_missing_credentials_issue(self.hass, entry)
                return self.async_update_reload_and_abort(
                    entry,
                    data=_build_entry_data_with_mode(
                        _entry_cloud_params(entry),
                        inventory_mode=INVENTORY_MODE_LOCAL_53216,
                        username="",
                        password="",
                        gateway_ip_required=_entry_gateway_ip_required(entry),
                        gateway_ip=verified_gateway_ip or _entry_gateway_ip(entry),
                    ),
                )
            if not errors:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reconfigure_inventory_mode",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_reconfigure_bluetooth(self, user_input: dict[str, Any] | None = None):
        """Enable or disable the optional Bluetooth runtime path."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            enable_bt = _enable_bt_from_user_input(user_input)
            data = dict(entry.data)
            options = dict(entry.options)
            runtime_data = getattr(entry, "runtime_data", None)
            pixie_runtime = getattr(runtime_data, "pixie_runtime", None) if runtime_data is not None else None
            error = await _async_apply_bluetooth_choice(
                self.hass,
                data=data,
                options=options,
                enable_bt=enable_bt,
                cloud_params=_entry_cloud_params(entry),
                inventory=getattr(pixie_runtime, "inventory", None),
                log_label="reconfigure",
                preferred_source=str(entry.data.get(CONF_BT_SOURCE) or "") or None,
                preferred_access_node=str(entry.data.get(CONF_BT_ACCESS_NODE) or "") or None,
            )
            if error is not None:
                errors["base"] = error
                data_schema = _bluetooth_data_schema(default=False)
                return self.async_show_form(
                    step_id="reconfigure_bluetooth",
                    data_schema=data_schema,
                    errors=errors,
                )

            if options != dict(entry.options):
                self.hass.config_entries.async_update_entry(entry, options=options)
            return self.async_update_reload_and_abort(entry, data=data)

        data_schema = _bluetooth_data_schema(default=bool(entry.data.get(CONF_BT_ENABLED)))
        return self.async_show_form(step_id="reconfigure_bluetooth", data_schema=data_schema, errors=errors)

    async def async_step_reconfigure_credentials(self, user_input: dict[str, Any] | None = None):
        """Store Pixie credentials so the entry can use cloud fallback when local inventory fails."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])
            handler = PixieAuthHandler()
            try:
                cloud_params = await handler.async_fetch_cloud_params(
                    username,
                    password,
                    include_inventory_seed=True,
                    selected_home_id=str(entry.data.get(CONF_HOME_ID) or ""),
                )
            except PixieAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(str(cloud_params.home_id))
                self._abort_if_unique_id_mismatch(reason="reconfigure_failed")
                _async_delete_missing_credentials_issue(self.hass, entry)
                return self.async_update_reload_and_abort(
                    entry,
                    data=_build_entry_data_with_mode(
                        cloud_params,
                        inventory_mode=INVENTORY_MODE_CLOUD_FALLBACK,
                        username=username,
                        password=password,
                        gateway_ip_required=_entry_gateway_ip_required(entry),
                        gateway_ip=_entry_gateway_ip(entry),
                        inventory_fallback_reason=str(
                            entry.data.get(CONF_INVENTORY_FALLBACK_REASON)
                            or INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED
                        ),
                    ),
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        autocomplete="username",
                    )
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="reconfigure_credentials", data_schema=data_schema, errors=errors)

    async def async_step_reconfigure_gateway_connection(self, user_input: dict[str, Any] | None = None):
        """Switch between UDP discovery and a stored manual gateway IP."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            mode = str(user_input[CONF_GATEWAY_CONNECTION_MODE])
            if mode == GATEWAY_CONNECTION_MODE_MANUAL:
                return await self.async_step_reconfigure_gateway_ip()

            handler = PixieAuthHandler()
            try:
                await handler.async_bootstrap_gateway(
                    _entry_cloud_params(entry),
                    username="",
                    password="",
                    keep_control_alive=False,
                    wait_for_shutdown=False,
                    hydrate_inventory=False,
                )
            except PixieAuthError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected Pixie Plus Local gateway-mode reconfigure failure")
                errors["base"] = "unknown"
            finally:
                if handler.runtime_session is not None:
                    await asyncio.to_thread(handler.runtime_session.stop_and_join, 5.0)

            if not errors:
                data = dict(entry.data)
                data[CONF_GATEWAY_IP_REQUIRED] = False
                verified_gateway_ip = None
                if isinstance(handler.current_hub, dict):
                    verified_gateway_ip = str(handler.current_hub.get("host") or "") or None
                if verified_gateway_ip:
                    data[CONF_GATEWAY_IP] = verified_gateway_ip
                else:
                    data.pop(CONF_GATEWAY_IP, None)
                _async_delete_gateway_ip_issue(self.hass, entry)
                return self.async_update_reload_and_abort(entry, data=data)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_GATEWAY_CONNECTION_MODE,
                    default=GATEWAY_CONNECTION_MODE_MANUAL if _entry_gateway_ip_required(entry) else GATEWAY_CONNECTION_MODE_AUTO,
                ): vol.In(
                    {
                        GATEWAY_CONNECTION_MODE_AUTO: "Use UDP discovery",
                        GATEWAY_CONNECTION_MODE_MANUAL: "Use a manual gateway IP",
                    }
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure_gateway_connection",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_reconfigure_gateway_ip(self, user_input: dict[str, Any] | None = None):
        """Validate and persist a manual gateway IP."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                gateway_ip = _normalize_gateway_ip(user_input[CONF_GATEWAY_IP])
            except ValueError:
                errors[CONF_GATEWAY_IP] = "invalid_gateway_ip"
            else:
                handler = PixieAuthHandler()
                try:
                    await handler.async_bootstrap_gateway(
                        _entry_cloud_params(entry),
                        username="",
                        password="",
                        gateway_ip=gateway_ip,
                        keep_control_alive=False,
                        wait_for_shutdown=False,
                        hydrate_inventory=False,
                    )
                except PixieAuthError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    LOGGER.exception("Unexpected Pixie Plus Local manual gateway reconfigure failure")
                    errors["base"] = "unknown"
                finally:
                    if handler.runtime_session is not None:
                        await asyncio.to_thread(handler.runtime_session.stop_and_join, 5.0)

                if not errors:
                    data = dict(entry.data)
                    data[CONF_GATEWAY_IP_REQUIRED] = True
                    data[CONF_GATEWAY_IP] = gateway_ip
                    _async_delete_gateway_ip_issue(self.hass, entry)
                    return self.async_update_reload_and_abort(entry, data=data)

        data_schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(CONF_GATEWAY_IP): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.TEXT,
                        )
                    ),
                }
            ),
            {CONF_GATEWAY_IP: _entry_gateway_ip(entry) or ""},
        )
        return self.async_show_form(
            step_id="reconfigure_gateway_ip",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_cover_controller(self, user_input: dict[str, Any] | None = None):
        """Select which blind controller to configure."""
        if self._validated_setup is None:
            return await self.async_step_user()

        cover_devices = self._validated_setup.cover_devices
        if not cover_devices:
            return await self._async_create_validated_entry()

        if len(cover_devices) == 1:
            self._selected_cover_controller_id = next(iter(cover_devices))
            return await self.async_step_cover_mapping()

        if user_input is not None:
            self._selected_cover_controller_id = str(user_input[CONF_COVER_CONTROLLER_ID])
            return await self.async_step_cover_mapping()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_COVER_CONTROLLER_ID): vol.In(cover_devices),
            }
        )
        return self.async_show_form(step_id="cover_controller", data_schema=data_schema)

    async def async_step_cover_mapping(self, user_input: dict[str, Any] | None = None):
        """Configure mapping for the selected blind controller."""
        if self._validated_setup is None:
            return await self.async_step_user()

        controller_id = self._selected_cover_controller_id
        if controller_id is None:
            return await self.async_step_cover_controller()

        if user_input is not None:
            self._validated_setup.options = _cover_controller_options_from_input(
                controller_id,
                user_input,
                self._validated_setup.options,
            )
            return await self._async_create_validated_entry()

        data_schema = self.add_suggested_values_to_schema(
            _cover_mapping_schema(),
            _cover_mapping_suggested_values(self._validated_setup.options, controller_id),
        )
        return self.async_show_form(
            step_id="cover_mapping",
            data_schema=data_schema,
            description_placeholders={
                "controller": self._validated_setup.cover_devices.get(controller_id, controller_id),
            },
        )

class PixiePlusLocalOptionsFlow(OptionsFlowWithReload):
    """Handle Pixie Plus Local mutable options."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._selected_cover_controller_id: str | None = None

    def _cover_devices(self) -> dict[str, str]:
        """Return current cover-controller choices from runtime inventory."""
        runtime_data = getattr(self.config_entry, "runtime_data", None)
        inventory = runtime_data.pixie_runtime.inventory if runtime_data is not None else None
        return _cover_controller_choices(inventory)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Choose which Pixie options to configure."""
        menu_options = ["cover_controller"]
        if _entry_bt_enabled(self.config_entry):
            menu_options = [
                "transport",
                "clear_bluetooth_access_node",
                "update_device_versions",
                *menu_options,
            ]

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_clear_bluetooth_access_node(self, user_input: dict[str, Any] | None = None):
        """Clear learned BLE access-node hints without disabling Bluetooth."""
        if _entry_bt_enabled(self.config_entry):
            data = dict(self.config_entry.data)
            for key in (
                CONF_BT_SOURCE,
                CONF_BT_ACCESS_NODE,
                "bt_response_access_node",
                "bt_access_nodes",
                CONF_BT_BETTER_CANDIDATE_SEEN,
            ):
                data.pop(key, None)
            self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_update_device_versions(self, user_input: dict[str, Any] | None = None):
        """Run a manual BLE scan to refresh device firmware versions."""
        if not _entry_bt_enabled(self.config_entry):
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        await _async_run_global_ble_version_scan(self.hass, reason="manual options")
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_transport(self, user_input: dict[str, Any] | None = None):
        """Configure command transport preference."""
        if not _entry_bt_enabled(self.config_entry):
            options = dict(self.config_entry.options)
            options[CONF_COMMAND_TRANSPORT] = COMMAND_TRANSPORT_TCP_PRIMARY
            options.pop(CONF_BT_ACCESS_NODE_PREFERENCE, None)
            return self.async_create_entry(title="", data=options)

        if user_input is not None:
            options = dict(self.config_entry.options)
            options[CONF_COMMAND_TRANSPORT] = str(user_input[CONF_COMMAND_TRANSPORT])
            options[CONF_BT_ACCESS_NODE_PREFERENCE] = str(user_input[CONF_BT_ACCESS_NODE_PREFERENCE])
            return self.async_create_entry(title="", data=options)

        current = str(self.config_entry.options.get(CONF_COMMAND_TRANSPORT) or COMMAND_TRANSPORT_TCP_PRIMARY)
        current_access_node_preference = str(
            self.config_entry.options.get(CONF_BT_ACCESS_NODE_PREFERENCE) or BT_ACCESS_NODE_AUTO
        )
        if current_access_node_preference not in (BT_ACCESS_NODE_AUTO, BT_ACCESS_NODE_PREFER_GATEWAY):
            current_access_node_preference = BT_ACCESS_NODE_AUTO
        data_schema = vol.Schema(
            {
                vol.Required(CONF_COMMAND_TRANSPORT, default=current): vol.In(
                    {
                        COMMAND_TRANSPORT_TCP_PRIMARY: "TCP primary, BT fallback",
                        COMMAND_TRANSPORT_BT_PRIMARY: "BT primary, TCP fallback",
                        COMMAND_TRANSPORT_TCP_ONLY: "TCP only",
                        COMMAND_TRANSPORT_BT_ONLY: "BT only",
                    }
                ),
                vol.Required(CONF_BT_ACCESS_NODE_PREFERENCE, default=current_access_node_preference): vol.In(
                    {
                        BT_ACCESS_NODE_AUTO: "Auto / best BLE node",
                        BT_ACCESS_NODE_PREFER_GATEWAY: "Prefer gateway, fallback to auto",
                    }
                ),
            }
        )
        return self.async_show_form(step_id="transport", data_schema=data_schema)

    async def async_step_cover_controller(self, user_input: dict[str, Any] | None = None):
        """Choose which blind controller to configure."""
        cover_devices = self._cover_devices()
        if not cover_devices:
            return self.async_abort(reason="no_blind_devices")

        if len(cover_devices) == 1:
            self._selected_cover_controller_id = next(iter(cover_devices))
            return await self.async_step_cover_mapping()

        if user_input is not None:
            self._selected_cover_controller_id = str(user_input[CONF_COVER_CONTROLLER_ID])
            return await self.async_step_cover_mapping()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_COVER_CONTROLLER_ID): vol.In(cover_devices),
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)

    async def async_step_cover_mapping(self, user_input: dict[str, Any] | None = None):
        """Manage per-controller blind mapping options."""
        controller_id = self._selected_cover_controller_id
        if controller_id is None:
            return await self.async_step_init()

        cover_devices = self._cover_devices()

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data=_cover_controller_options_from_input(
                    controller_id,
                    user_input,
                    self.config_entry.options,
                ),
            )

        data_schema = self.add_suggested_values_to_schema(
            _cover_mapping_schema(),
            _cover_mapping_suggested_values(self.config_entry.options, controller_id),
        )
        return self.async_show_form(
            step_id="cover_mapping",
            data_schema=data_schema,
            description_placeholders={
                "controller": cover_devices.get(controller_id, controller_id),
            },
        )
