"""Config flow for Pixie Plus Local."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from ipaddress import IPv4Address
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlowWithReload
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
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_INVENTORY_MODE,
    CONF_MESHNET,
    CONF_MESHNET2,
    CONF_NETID,
    CONF_PIXIE_PASSWORD,
    CONF_PIXIE_USERNAME,
    CONF_USER_ID,
    DOMAIN,
    INVENTORY_MODE_CLOUD_FALLBACK,
    _async_delete_missing_credentials_issue,
    _async_delete_gateway_ip_issue,
)
from .pixie_runtime import (
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

GATEWAY_CONNECTION_MODE_AUTO = "auto"
GATEWAY_CONNECTION_MODE_MANUAL = "manual"


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
    has_cover_devices: bool
    cover_devices: dict[str, str]


def _is_known_cloud_value(value: Any) -> bool:
    """Return True when a cloud metadata field is populated."""
    if value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized not in ("", "unknown", "none")


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
        if record.model_no != "1102":
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

    return any(device.model_no == "1102" for device in inventory.devices_by_id.values())


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
) -> dict[str, Any]:
    data = _build_entry_data(cloud_params)
    data[CONF_INVENTORY_MODE] = inventory_mode
    data[CONF_GATEWAY_IP_REQUIRED] = gateway_ip_required
    if gateway_ip_required and gateway_ip:
        data[CONF_GATEWAY_IP] = gateway_ip
    if inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
        data[CONF_PIXIE_USERNAME] = username
        data[CONF_PIXIE_PASSWORD] = password
    return data


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

    if handler.inventory_mode == INVENTORY_MODE_CLOUD_FALLBACK:
        LOGGER.warning(
            "Pixie Plus Local is using cloud-assisted inventory mode because direct local inventory was unavailable during setup"
        )

    has_cover_devices = _has_cover_devices(handler)
    options: dict[str, Any] = {}
    cover_devices = _cover_controller_choices(handler.inventory)

    return ValidatedSetup(
        title=_build_entry_title(handler, cloud_params),
        data=_build_entry_data_with_mode(
            cloud_params,
            inventory_mode=handler.inventory_mode,
            username=username,
            password=password,
            gateway_ip_required=gateway_ip is not None,
            gateway_ip=gateway_ip,
        ),
        options=options,
        has_cover_devices=has_cover_devices,
        cover_devices=cover_devices,
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

    async def _async_finish_validated_setup(self):
        """Continue to the remaining setup steps after validation succeeds."""
        if self._validated_setup is None:
            return await self.async_step_user()

        await self.async_set_unique_id(self._validated_setup.data[CONF_HOME_ID])
        self._abort_if_unique_id_configured()

        if self._validated_setup.has_cover_devices:
            return await self.async_step_cover_controller()

        return self.async_create_entry(
            title=self._validated_setup.title,
            data=self._validated_setup.data,
            options=self._validated_setup.options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PixiePlusLocalOptionsFlow:
        """Create the options flow."""
        return PixiePlusLocalOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._pending_user_input = dict(user_input)
            try:
                self._validated_setup = await _async_validate_setup_input(user_input)
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

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Present reconfiguration actions for the config entry."""
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["reconfigure_credentials", "reconfigure_gateway_connection"],
        )

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
            return self.async_create_entry(
                title=self._validated_setup.title,
                data=self._validated_setup.data,
                options=self._validated_setup.options,
            )

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
            return self.async_create_entry(
                title=self._validated_setup.title,
                data=self._validated_setup.data,
                options=_cover_controller_options_from_input(
                    controller_id,
                    user_input,
                    self._validated_setup.options,
                ),
            )

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
