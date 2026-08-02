"""Pixie add/remove provisioning helpers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import os
from typing import Any

from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .pixie_ble import PixieBluetoothRuntime, async_scan_pixie_advertisement_identities, hash_mesh_id_to_broadcast
from .pixie_ble_crypto import decrypt_notification_packet
from .pixie_const import (
    CONF_BLE_INVENTORY,
    CONF_BT_ENABLED,
    CONF_PIXIE_PIN,
    DOMAIN,
    INVENTORY_MODE_BLE_ADVERTISEMENT,
    PIXIE_ADD_SCAN_SECONDS,
)
from .pixie_inventory import DeviceRecord, PixieInventory
from .pixie_runtime import CloudParams
from .pixie_value_profiles import (
    get_post_add_mode_choice,
    get_post_add_mode_selection_for_identity,
    hardware_list,
)

LOGGER = logging.getLogger(__name__)
PIXIE_UNPROVISIONED_ADVERT_MEMBERSHIP = hash_mesh_id_to_broadcast("123")
PIXIE_ADD_MODE_FRESH = "fresh_unprovisioned"
PIXIE_ADD_MODE_READD_BLE_ONLY = "readd_ble_only_same_home"
PIXIE_ADD_MODE_READD_GATEWAY = "readd_gateway_same_home"

@dataclass
class PixieAddDeviceResult:
    """Summarize one add-device bulk operation."""

    added: list[str] = field(default_factory=list)
    added_macs: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PixieBleOnlyNewHomeResult:
    """Result of provisioning a new BLE-only Pixie home during setup."""

    inventory: PixieInventory
    result: PixieAddDeviceResult


def _pixie_device_address_id(advert: Any) -> int:
    """Return the Pixie app-style candidate id from an advert DeviceAddress."""
    try:
        value = int(getattr(advert, "device_id", 0) or 0) % 250
    except (TypeError, ValueError):
        value = 0
    return value or 1


def _allocate_pixie_device_id(advert: Any, occupied_ids: set[int]) -> int:
    """Allocate the Pixie app-style id, falling back on the first free slot."""
    candidate = _pixie_device_address_id(advert)
    if candidate not in occupied_ids:
        return candidate
    for dev_id in range(1, 250):
        if dev_id not in occupied_ids:
            return dev_id
    raise ConfigEntryError("No free Pixie device id is available")


def _pixie_advert_payload_bytes(advert: Any) -> bytes:
    """Return decoded manufacturer payload bytes carried by a Pixie advert."""
    payload_hex = str(getattr(advert, "payload_hex", "") or "")
    if not payload_hex:
        return b""
    try:
        return bytes.fromhex(payload_hex)
    except ValueError:
        return b""


def _gateway_mesh_value_hex(meshnet2: Any) -> str:
    """Return gateway meshNet2 in the little-endian byte order used by adverts."""
    try:
        value = int(str(meshnet2).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigEntryError("Pixie gateway meshNet2 is not available") from exc
    return (value & 0xFFFFFFFF).to_bytes(4, "little", signed=False).hex()


def _gateway_mesh_value_bytes(meshnet2: Any) -> bytes:
    """Return gateway meshNet2 bytes for management commands."""
    return bytes.fromhex(_gateway_mesh_value_hex(meshnet2))


def _pixie_advert_gateway_mesh_value(advert: Any) -> str:
    """Return normalized gateway mesh value bytes from a decoded Pixie advert."""
    value = str(getattr(advert, "gateway_mesh_value", "") or "").strip().lower()
    if value:
        return value
    payload = _pixie_advert_payload_bytes(advert)
    return payload[13:17].hex() if len(payload) >= 17 else ""


def _pixie_advert_membership(advert: Any) -> str:
    """Return normalized membership bytes from a decoded Pixie advert."""
    return str(getattr(advert, "membership", "") or "").strip().lower()


def _inventory_mac_set(inventory: PixieInventory) -> set[str]:
    """Return normalized MACs already present in one Pixie inventory."""
    return {
        mac
        for record in inventory.devices_by_id.values()
        if (mac := PixieInventory._normalize_mac(record.mac))
    }


def _candidate_label(model_no: str) -> str:
    """Return the user-facing label for a scanned add candidate."""
    return str(hardware_list.get(model_no, model_no or "Pixie device"))


def _candidate_dict(advert: Any, *, device_id: int, model_no: str, add_mode: str, login_seed: str) -> dict[str, Any]:
    """Build a scan candidate with explicit provisioning mode metadata."""
    mac = PixieInventory._normalize_mac(getattr(advert, "mac", ""))
    return {
        "key": mac,
        "identity": advert,
        "device_id": int(device_id),
        "add_mode": add_mode,
        "login_seed": str(login_seed),
        "label": _candidate_label(model_no),
        "post_add_mode_selection": get_post_add_mode_selection_for_identity(advert),
    }


def _format_mac(normalized_or_mac: str) -> str:
    """Format a normalized MAC for logs and device records."""
    normalized = PixieInventory._normalize_mac(normalized_or_mac)
    if not normalized:
        return str(normalized_or_mac or "")
    return ":".join(normalized[i : i + 2] for i in range(0, 12, 2)).lower()


def _raw_management_packet(device_id: int, body: bytes) -> str:
    """Build a captured-shape raw management packet for TCP/BLE transport."""
    return (os.urandom(3) + b"\x03\x04" + int(device_id).to_bytes(2, "little") + body).hex()


def _raw_add_device_assign_packet(device_id: int) -> str:
    """Build the captured 1912 packet used to assign a fresh device id."""
    body = b"\x00\x00\xe0\x11\x02" + int(device_id).to_bytes(2, "little") + b"\x00" * 8
    return (b"\x01\x00\x00\x00\x00" + body).hex()


def _selected_post_add_mode_choice(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return the selected post-add mode choice for a scan candidate."""
    selection = candidate.get("post_add_mode_selection")
    selected_value = str(candidate.get("post_add_mode") or "")
    if not isinstance(selection, dict) or not selected_value:
        return {}
    return get_post_add_mode_choice(selection, selected_value)


def _raw_post_add_mode_packet(choice: dict[str, Any]) -> str:
    """Build a configured post-add mode command packet."""
    try:
        device_id = int(choice.get("command_device_id", 0))
        body = bytes.fromhex("".join(str(choice.get("command_body_hex") or "").split()))
    except (TypeError, ValueError) as exc:
        raise ConfigEntryError("Invalid Pixie post-add mode command profile") from exc
    if not body:
        raise ConfigEntryError("Invalid Pixie post-add mode command profile")
    return _raw_management_packet(device_id, body)


def _device_manager_payload_from_record(record: DeviceRecord, *, online: Any | None = None) -> dict[str, Any]:
    """Build the gateway DeviceManager device object from the current inventory record."""
    payload: dict[str, Any] = {
        "mac": _format_mac(record.mac),
        "type": int(record.type),
        "stype": int(record.stype),
        "id": int(record.id),
        "version": int(record.version or 0),
        "online": record.runtime.online if online is None else online,
        "groups": [],
        "rooms": list(record.rooms or []),
        "state": {},
        "name": record.name,
    }
    if record.left_name:
        payload["left_name"] = record.left_name
    if record.right_name:
        payload["right_name"] = record.right_name
    return payload


async def async_scan_addable_pixie_devices(
    hass: Any,
    inventory: PixieInventory,
    *,
    log_prefix: str = "",
) -> list[dict[str, Any]]:
    """Return decoded, supported Pixie devices that can be added to this home."""
    adverts = await async_scan_pixie_advertisement_identities(
        hass,
        duration=PIXIE_ADD_SCAN_SECONDS,
    )
    occupied_ids = set(int(dev_id) for dev_id in inventory.devices_by_id)
    existing_macs = _inventory_mac_set(inventory)
    ble_only_membership = str(inventory.mesh_net or "").strip().lower()
    try:
        gateway_mesh_value = _gateway_mesh_value_hex(inventory.mesh_net2)
    except ConfigEntryError:
        gateway_mesh_value = ""
    candidates: list[dict[str, Any]] = []
    skipped: dict[str, int] = {
        "missing_mac": 0,
        "already_in_inventory": 0,
        "unsupported_model": 0,
        "id_already_in_use": 0,
        "accepted_fresh_unprovisioned": 0,
        "accepted_readd_ble_only_same_home": 0,
        "accepted_readd_gateway_same_home": 0,
        "not_unprovisioned_add_mode": 0,
    }
    for advert in adverts:
        mac = PixieInventory._normalize_mac(getattr(advert, "mac", ""))
        if not mac:
            skipped["missing_mac"] += 1
            continue
        if mac in existing_macs:
            skipped["already_in_inventory"] += 1
            continue
        model_no = str(getattr(advert, "model_no", "") or "")
        if model_no not in hardware_list:
            skipped["unsupported_model"] += 1
            continue
        membership = _pixie_advert_membership(advert)
        advert_gateway_mesh_value = _pixie_advert_gateway_mesh_value(advert)
        if membership == PIXIE_UNPROVISIONED_ADVERT_MEMBERSHIP:
            device_id = _allocate_pixie_device_id(advert, occupied_ids)
            occupied_ids.add(device_id)
            candidates.append(_candidate_dict(
                advert,
                device_id=device_id,
                model_no=model_no,
                add_mode=PIXIE_ADD_MODE_FRESH,
                login_seed="123",
            ))
            skipped["accepted_fresh_unprovisioned"] += 1
            continue
        if ble_only_membership and membership == ble_only_membership:
            device_id = _pixie_device_address_id(advert)
            if device_id in occupied_ids:
                skipped["id_already_in_use"] += 1
                continue
            occupied_ids.add(device_id)
            candidates.append(_candidate_dict(
                advert,
                device_id=device_id,
                model_no=model_no,
                add_mode=PIXIE_ADD_MODE_READD_BLE_ONLY,
                login_seed="",
            ))
            skipped["accepted_readd_ble_only_same_home"] += 1
            continue
        if gateway_mesh_value and membership == "00000000" and advert_gateway_mesh_value == gateway_mesh_value:
            device_id = _pixie_device_address_id(advert)
            if device_id in occupied_ids:
                skipped["id_already_in_use"] += 1
                continue
            occupied_ids.add(device_id)
            candidates.append(_candidate_dict(
                advert,
                device_id=device_id,
                model_no=model_no,
                add_mode=PIXIE_ADD_MODE_READD_GATEWAY,
                login_seed=str(inventory.net_id or ""),
            ))
            skipped["accepted_readd_gateway_same_home"] += 1
            continue
        if membership != PIXIE_UNPROVISIONED_ADVERT_MEMBERSHIP:
            skipped["not_unprovisioned_add_mode"] += 1
            continue
    LOGGER.info(
        "%sPixie add-device scan found %s addable candidate(s) skipped=%s",
        log_prefix,
        len(candidates),
        skipped,
    )
    return candidates


async def _async_connect_provisioning_runtime(
    runtime: PixieBluetoothRuntime,
    *,
    target_mac: str,
    log_prefix: str,
    attempts: int = 3,
) -> asyncio.Queue[bytes]:
    """Connect to a fresh Pixie target, retrying transient BLE failures."""
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await runtime._connect_login_enable()
        except Exception as err:
            last_error = err
            message = str(err)
            if "rejected the provisioning login" in message or "Invalid login response" in message:
                raise
            LOGGER.warning(
                "%sPixie add-device target connection attempt %s/%s failed mac=%s: %s",
                log_prefix,
                attempt,
                attempts,
                target_mac,
                err,
            )
            await runtime._disconnect_client()
            if attempt < attempts:
                await asyncio.sleep(0.5)
    raise ConfigEntryError(f"Could not connect to selected Pixie device {target_mac}") from last_error


async def _async_drain_add_device_notifications(
    runtime: PixieBluetoothRuntime,
    notify_queue: asyncio.Queue[bytes],
    *,
    log_prefix: str,
    reason: str,
) -> None:
    """Drain and log notifications that arrived before the next add-device step."""
    if runtime._session_key is None or runtime._device_mac is None:
        raise ConfigEntryError("Pixie BLE provisioning session is not ready")
    drained = 0
    while not notify_queue.empty():
        raw = notify_queue.get_nowait()
        drained += 1
        try:
            payload = decrypt_notification_packet(runtime._session_key, runtime._device_mac, raw)
        except Exception:
            LOGGER.debug("%sPixie add-device drained notification decrypt failed reason=%s raw=%s", log_prefix, reason, raw.hex(), exc_info=True)
            continue
        LOGGER.debug(
            "%sPixie add-device drained notification reason=%s payload=%s",
            log_prefix,
            reason,
            payload.hex(),
        )
    if drained:
        LOGGER.debug("%sPixie add-device drained %s notification(s) reason=%s", log_prefix, drained, reason)


async def _async_wait_for_add_device_ack(
    runtime: PixieBluetoothRuntime,
    notify_queue: asyncio.Queue[bytes],
    device_id: int,
    *,
    log_prefix: str,
    timeout: float = 5.0,
) -> None:
    """Wait for the captured e11102 acknowledgement after assigning a device id."""
    if runtime._session_key is None or runtime._device_mac is None:
        raise ConfigEntryError("Pixie BLE provisioning session is not ready")
    expected = b"\xe1\x11\x02" + int(device_id).to_bytes(2, "little")
    end_at = asyncio.get_running_loop().time() + max(0.1, timeout)
    while True:
        wait_left = end_at - asyncio.get_running_loop().time()
        if wait_left <= 0:
            raise ConfigEntryError(f"Pixie device did not confirm assigned id {device_id}")
        try:
            raw = await asyncio.wait_for(notify_queue.get(), timeout=wait_left)
        except TimeoutError as err:
            raise ConfigEntryError(f"Pixie device did not confirm assigned id {device_id}") from err
        try:
            payload = decrypt_notification_packet(runtime._session_key, runtime._device_mac, raw)
        except Exception:
            LOGGER.debug("%sPixie add-device acknowledgement decrypt failed raw=%s", log_prefix, raw.hex(), exc_info=True)
            continue
        if payload.startswith(expected):
            LOGGER.debug(
                "%sPixie add-device acknowledgement received id=%s payload=%s",
                log_prefix,
                device_id,
                payload.hex(),
            )
            return
        LOGGER.debug(
            "%sPixie add-device waiting for e11102 id=%s ignored payload=%s",
            log_prefix,
            device_id,
            payload.hex(),
        )


async def _async_wait_for_post_add_mode_ack(
    runtime: PixieBluetoothRuntime,
    notify_queue: asyncio.Queue[bytes],
    ack_prefix_hex: str,
    *,
    log_prefix: str,
    timeout: float = 5.0,
) -> None:
    """Wait for a configured post-add mode acknowledgement."""
    if runtime._session_key is None or runtime._device_mac is None:
        raise ConfigEntryError("Pixie BLE provisioning session is not ready")
    try:
        expected = bytes.fromhex("".join(str(ack_prefix_hex).split()))
    except ValueError as exc:
        raise ConfigEntryError("Invalid Pixie post-add mode acknowledgement profile") from exc
    end_at = asyncio.get_running_loop().time() + max(0.1, timeout)
    while True:
        wait_left = end_at - asyncio.get_running_loop().time()
        if wait_left <= 0:
            raise ConfigEntryError(f"Pixie device did not confirm post-add mode {expected.hex()}")
        try:
            raw = await asyncio.wait_for(notify_queue.get(), timeout=wait_left)
        except TimeoutError as err:
            raise ConfigEntryError(f"Pixie device did not confirm post-add mode {expected.hex()}") from err
        try:
            payload = decrypt_notification_packet(runtime._session_key, runtime._device_mac, raw)
        except Exception:
            LOGGER.debug("%sPixie post-add mode acknowledgement decrypt failed raw=%s", log_prefix, raw.hex(), exc_info=True)
            continue
        if expected and expected in payload:
            LOGGER.debug(
                "%sPixie post-add mode acknowledgement received expected=%s payload=%s",
                log_prefix,
                expected.hex(),
                payload.hex(),
            )
            return
        LOGGER.debug(
            "%sPixie post-add mode waiting for expected=%s ignored payload=%s",
            log_prefix,
            expected.hex(),
            payload.hex(),
        )


async def async_provision_pixie_identity(
    hass: Any,
    cloud_params: CloudParams,
    identity: Any,
    *,
    device_id: int,
    netid: str,
    login_seed: str = "123",
    log_prefix: str,
    ble_only: bool,
    post_add_mode_choice: dict[str, Any] | None = None,
) -> None:
    """Provision one fresh Pixie device directly over BLE."""
    target_mac = _format_mac(str(getattr(identity, "mac", "") or ""))
    if not target_mac:
        raise ConfigEntryError("Selected Pixie device has no valid MAC address")
    runtime = PixieBluetoothRuntime(
        hass=hass,
        cloud_params=cloud_params,
        inventory=None,
        enabled=True,
        preferred_access_node=target_mac.upper(),
        login_seed=str(login_seed or "123"),
        send_notify_enable_writes=False,
        active_scan_duration=PIXIE_ADD_SCAN_SECONDS,
        remember_access_node_on_connect=False,
        compare_access_node_after_connect=False,
        strict_preferred_access_node=True,
    )
    notify_queue = None
    try:
        LOGGER.info(
            "%sProvisioning Pixie device over BLE mac=%s assigned_id=%s",
            log_prefix,
            target_mac,
            device_id,
        )
        notify_queue = await _async_connect_provisioning_runtime(runtime, target_mac=target_mac, log_prefix=log_prefix)
        await runtime.async_write_provisioning_blocks(netid=str(netid or ""))
        e011 = _raw_add_device_assign_packet(device_id)
        await _async_drain_add_device_notifications(runtime, notify_queue, log_prefix=log_prefix, reason="before e01102")
        await runtime.async_send_plain_1912_hexes((e011,), target="add_device_e01102")
        await _async_wait_for_add_device_ack(runtime, notify_queue, device_id, log_prefix=log_prefix)
        if not ble_only:
            cc = _raw_management_packet(device_id, b"\xcc\x6b\x69" + _gateway_mesh_value_bytes(cloud_params.meshnet2))
            await runtime.async_send_plain_1912_hexes((cc,), target="add_device_cc6b69")
        if post_add_mode_choice:
            command_hex = _raw_post_add_mode_packet(post_add_mode_choice)
            ack_prefix_hex = str(post_add_mode_choice.get("ack_prefix_hex") or "")
            if not ack_prefix_hex:
                raise ConfigEntryError("Invalid Pixie post-add mode profile")
            await _async_drain_add_device_notifications(runtime, notify_queue, log_prefix=log_prefix, reason="before post-add mode")
            await runtime.async_send_plain_1912_hexes((command_hex,), target="add_device_post_add_mode")
            await _async_wait_for_post_add_mode_ack(runtime, notify_queue, ack_prefix_hex, log_prefix=log_prefix)
    finally:
        if notify_queue is not None:
            with suppress(Exception):
                while not notify_queue.empty():
                    notify_queue.get_nowait()
        await runtime._disconnect_client()


async def async_create_ble_only_home_from_devices(
    hass: Any,
    *,
    home_name: str,
    pin: str,
    membership: str,
    cloud_params: CloudParams,
    candidates: list[dict[str, Any]],
) -> PixieBleOnlyNewHomeResult:
    """Provision selected devices and return a new BLE-only home inventory."""
    inventory = PixieInventory.from_ble_advertisements([], home_name=home_name, membership=membership)
    result = PixieAddDeviceResult()
    occupied_ids: set[int] = set()
    log_prefix = f"[{home_name}] " if home_name else ""
    for candidate in candidates:
        identity = candidate["identity"]
        post_add_mode_choice = _selected_post_add_mode_choice(candidate)
        device_id = int(candidate.get("device_id") or _allocate_pixie_device_id(identity, occupied_ids))
        occupied_ids.add(device_id)
        add_mode = str(candidate.get("add_mode") or PIXIE_ADD_MODE_FRESH)
        login_seed = str(candidate.get("login_seed") or "123")
        if add_mode == PIXIE_ADD_MODE_READD_BLE_ONLY:
            login_seed = str(pin or "")
        label = str(candidate.get("label") or getattr(identity, "model_name", None) or "Pixie device")
        mac = _format_mac(str(getattr(identity, "mac", "") or ""))
        try:
            await asyncio.wait_for(
                async_provision_pixie_identity(
                    hass,
                    cloud_params,
                    identity,
                    device_id=device_id,
                    netid=pin,
                    login_seed=login_seed,
                    log_prefix=log_prefix,
                    ble_only=True,
                    post_add_mode_choice=post_add_mode_choice,
                ),
                timeout=max(75.0, PIXIE_ADD_SCAN_SECONDS * 3.0 + 30.0),
            )
        except Exception as err:
            error_text = str(err).lower()
            reason = (
                "failed_to_connect"
                if isinstance(err, TimeoutError) or "connect" in error_text
                else "failed_provisioning"
            )
            LOGGER.warning(
                "%sPixie new BLE-only home add-device failed label=%s mac=%s assigned_id=%s reason=%s: %s",
                log_prefix,
                label,
                mac,
                device_id,
                reason,
                err,
            )
            result.failed.append({"label": label, "mac": mac, "reason": reason})
            continue
        record = inventory.add_or_update_ble_identity_device(
            identity,
            device_id=device_id,
            model_no_override=post_add_mode_choice.get("result_model_no") if post_add_mode_choice else None,
        )
        result.added.append(record.name)
        result.added_macs.append(PixieInventory._normalize_mac(record.mac))
        LOGGER.info(
            "%sAdded Pixie device to new BLE-only home id=%s name=%s mac=%s model=%s",
            log_prefix,
            record.id,
            record.name,
            record.mac,
            record.model_no,
        )
    return PixieBleOnlyNewHomeResult(inventory=inventory, result=result)


class PixieProvisioningMixin:
    """Add/remove provisioning behavior for a Pixie config-entry runtime."""

    async def async_scan_addable_pixie_devices(self) -> list[dict[str, Any]]:
        """Return decoded, supported Pixie devices that appear unprovisioned."""
        inventory = self.pixie_runtime.inventory
        if inventory is None:
            return []
        return await async_scan_addable_pixie_devices(self.coordinator.hass, inventory, log_prefix=self._log_prefix)

    async def async_add_pixie_devices(self, candidates: list[dict[str, Any]]) -> PixieAddDeviceResult:
        """Provision selected Pixie devices and add them to this entry inventory."""
        from .pixie_ha import (
            _async_cleanup_stale_ble_only_owners_after_add,
            _async_save_inventory_snapshot,
            _async_verify_gateway_owner_conflicts_before_add,
            _entry_bt_enabled,
            _entry_gateway_supports_local_inventory_53216,
            _entry_inventory_mode,
            _inventory_persistent_signature,
            async_register_device_topology,
            device_added_signal,
            physical_device_identifier,
        )

        result = PixieAddDeviceResult()
        if not candidates:
            return result
        inventory = self.pixie_runtime.inventory
        if inventory is None:
            raise ConfigEntryError("Pixie inventory is not available")
        ble_only = _entry_inventory_mode(self.entry) == INVENTORY_MODE_BLE_ADVERTISEMENT
        if not ble_only and not _entry_gateway_supports_local_inventory_53216(self.entry, inventory):
            raise ConfigEntryError("This Pixie gateway does not support local add/remove")
        if not _entry_bt_enabled(self.entry):
            raise ConfigEntryError("Adding Pixie devices requires Bluetooth support")

        candidate_macs = {
            PixieInventory._normalize_mac(getattr(candidate.get("identity"), "mac", ""))
            for candidate in candidates
            if PixieInventory._normalize_mac(getattr(candidate.get("identity"), "mac", ""))
        }
        await _async_verify_gateway_owner_conflicts_before_add(
            self.coordinator.hass,
            self.entry,
            candidate_macs,
        )
        suspended_ble_runtimes = await self._async_suspend_ble_runtimes_for_add()
        added_device_ids: set[int] = set()
        added_power_meter_device = False
        try:
            for candidate in candidates:
                identity = candidate["identity"]
                post_add_mode_choice = _selected_post_add_mode_choice(candidate)
                device_id = int(candidate["device_id"])
                add_mode = str(candidate.get("add_mode") or PIXIE_ADD_MODE_FRESH)
                login_seed = str(candidate.get("login_seed") or "123")
                if add_mode == PIXIE_ADD_MODE_READD_BLE_ONLY:
                    login_seed = str(self.entry.data.get(CONF_PIXIE_PIN) or "")
                elif add_mode == PIXIE_ADD_MODE_READD_GATEWAY:
                    login_seed = str(self.cloud_params.netid or "")
                label = str(candidate.get("label") or getattr(identity, "model_name", None) or "Pixie device")
                mac = _format_mac(str(getattr(identity, "mac", "") or ""))
                try:
                    await asyncio.wait_for(
                        self._async_provision_one_device(
                            identity,
                            device_id=device_id,
                            ble_only=ble_only,
                            login_seed=login_seed,
                            post_add_mode_choice=post_add_mode_choice,
                        ),
                        timeout=max(75.0, PIXIE_ADD_SCAN_SECONDS * 3.0 + 30.0),
                    )
                except Exception as err:
                    error_text = str(err).lower()
                    reason = (
                        "failed_to_connect"
                        if isinstance(err, TimeoutError) or "connect" in error_text
                        else "failed_provisioning"
                    )
                    LOGGER.warning(
                        "%sPixie add-device failed before gateway persistence label=%s mac=%s assigned_id=%s reason=%s: %s",
                        self._log_prefix,
                        label,
                        mac,
                        device_id,
                        reason,
                        err,
                    )
                    result.failed.append({"label": label, "mac": mac, "reason": reason})
                    continue
                record = inventory.add_or_update_ble_identity_device(
                    identity,
                    device_id=device_id,
                    model_no_override=post_add_mode_choice.get("result_model_no") if post_add_mode_choice else None,
                )
                self.reset_config_refresh_state_for_devices((int(record.id),))
                if record.capabilities.supports_power_metering:
                    added_power_meter_device = True
                if not ble_only:
                    try:
                        await self._async_gateway_add_device(record)
                    except Exception as err:
                        inventory.remove_device_by_ha_identifier(physical_device_identifier(record))
                        reason = "failed_gateway_persistence"
                        LOGGER.warning(
                            "%sPixie add-device gateway persistence failed label=%s mac=%s assigned_id=%s: %s",
                            self._log_prefix,
                            label,
                            mac,
                            device_id,
                            err,
                        )
                        result.failed.append({"label": label, "mac": mac, "reason": reason})
                        continue
                result.added.append(record.name)
                result.added_macs.append(PixieInventory._normalize_mac(record.mac))
                LOGGER.info(
                    "%sAdded Pixie device id=%s name=%s mac=%s model=%s",
                    self._log_prefix,
                    record.id,
                    record.name,
                    record.mac,
                    record.model_no,
                )

                data = dict(self.entry.data)
                if ble_only:
                    data[CONF_BLE_INVENTORY] = inventory.to_dict()
                    self.coordinator.hass.config_entries.async_update_entry(self.entry, data=data)
                await _async_cleanup_stale_ble_only_owners_after_add(
                    self.coordinator.hass,
                    self.entry,
                    {PixieInventory._normalize_mac(record.mac)},
                )
                self.pixie_runtime.inventory = inventory
                self.coordinator.async_set_updated_data(inventory)
                await async_register_device_topology(self.coordinator.hass, self.entry, inventory, domain=DOMAIN)
                await _async_save_inventory_snapshot(self.coordinator.hass, self.entry, inventory)
                self.last_persisted_inventory_signature = _inventory_persistent_signature(inventory)
                async_dispatcher_send(self.coordinator.hass, device_added_signal(self.entry), int(record.id))
                added_device_ids.add(int(record.id))

            if result.failed:
                LOGGER.info(
                    "%sPixie add-device bulk completed with partial result added=%s failed=%s",
                    self._log_prefix,
                    len(result.added),
                    result.failed,
                )
            return result
        finally:
            await self._async_resume_ble_runtimes_after_add(suspended_ble_runtimes)
            if added_device_ids and self.pixie_runtime.inventory is not None:
                if added_power_meter_device:
                    self.start_power_meter_polling()
                self.schedule_config_refreshes_for_devices(
                    self.pixie_runtime.inventory,
                    added_device_ids,
                    reason="device_added",
                )

    async def _async_suspend_ble_runtimes_for_add(self) -> list["PixiePlusConfigEntryRuntimeData"]:
        """Temporarily release long-lived BLE sessions before direct provisioning."""
        from .pixie_ha import _entry_bt_enabled, _entry_home_name, _loaded_pixie_runtime_entries

        suspended: list[PixiePlusConfigEntryRuntimeData] = []
        for runtime_data in _loaded_pixie_runtime_entries(self.coordinator.hass):
            ble_runtime = runtime_data.ble_runtime
            if ble_runtime is None or not _entry_bt_enabled(runtime_data.entry):
                continue
            runtime_data.ble_runtime_suspend_count += 1
            LOGGER.info(
                "%sSuspending Pixie BLE runtime for add-device provisioning entry=%s",
                self._log_prefix,
                _entry_home_name(runtime_data.entry),
            )
            try:
                await ble_runtime.async_shutdown()
            except Exception as err:
                runtime_data.ble_runtime_suspend_count = max(0, runtime_data.ble_runtime_suspend_count - 1)
                raise ConfigEntryError(
                    f"Could not release Bluetooth proxy before adding device for {_entry_home_name(runtime_data.entry)}"
                ) from err
            suspended.append(runtime_data)
        return suspended

    async def _async_resume_ble_runtimes_after_add(self, suspended: list["PixiePlusConfigEntryRuntimeData"]) -> None:
        """Restart BLE sessions that were stopped for direct provisioning."""
        from .pixie_ha import _entry_bt_enabled, _entry_home_name

        for runtime_data in suspended:
            runtime_data.ble_runtime_suspend_count = max(0, runtime_data.ble_runtime_suspend_count - 1)
            if not _entry_bt_enabled(runtime_data.entry):
                continue
            try:
                await runtime_data.async_ensure_ble_runtime()
            except Exception:
                LOGGER.warning(
                    "%sCould not resume Pixie BLE runtime after add-device provisioning entry=%s",
                    self._log_prefix,
                    _entry_home_name(runtime_data.entry),
                    exc_info=True,
                )

    async def _async_provision_one_device(
        self,
        identity: Any,
        *,
        device_id: int,
        ble_only: bool,
        login_seed: str = "123",
        post_add_mode_choice: dict[str, Any] | None = None,
    ) -> None:
        """Provision one fresh Pixie device directly over BLE."""
        target_mac = _format_mac(str(getattr(identity, "mac", "") or ""))
        if not target_mac:
            raise ConfigEntryError("Selected Pixie device has no valid MAC address")
        runtime = PixieBluetoothRuntime(
            hass=self.coordinator.hass,
            cloud_params=self.cloud_params,
            inventory=None,
            enabled=True,
            preferred_access_node=target_mac.upper(),
            login_seed=str(login_seed or "123"),
            send_notify_enable_writes=False,
            active_scan_duration=PIXIE_ADD_SCAN_SECONDS,
            remember_access_node_on_connect=False,
            compare_access_node_after_connect=False,
            strict_preferred_access_node=True,
        )
        notify_queue = None
        try:
            LOGGER.info(
                "%sProvisioning Pixie device over BLE mac=%s assigned_id=%s",
                self._log_prefix,
                target_mac,
                device_id,
            )
            notify_queue = await _async_connect_provisioning_runtime(runtime, target_mac=target_mac, log_prefix=self._log_prefix)
            await runtime.async_write_provisioning_blocks(netid=str(self.cloud_params.netid or self.entry.data.get(CONF_PIXIE_PIN) or ""))
            e011 = _raw_add_device_assign_packet(device_id)
            await _async_drain_add_device_notifications(runtime, notify_queue, log_prefix=self._log_prefix, reason="before e01102")
            await runtime.async_send_plain_1912_hexes((e011,), target="add_device_e01102")
            await _async_wait_for_add_device_ack(runtime, notify_queue, device_id, log_prefix=self._log_prefix)
            if not ble_only:
                cc = _raw_management_packet(device_id, b"\xcc\x6b\x69" + _gateway_mesh_value_bytes(self.cloud_params.meshnet2))
                await runtime.async_send_plain_1912_hexes((cc,), target="add_device_cc6b69")
            if post_add_mode_choice:
                command_hex = _raw_post_add_mode_packet(post_add_mode_choice)
                ack_prefix_hex = str(post_add_mode_choice.get("ack_prefix_hex") or "")
                if not ack_prefix_hex:
                    raise ConfigEntryError("Invalid Pixie post-add mode profile")
                await _async_drain_add_device_notifications(runtime, notify_queue, log_prefix=self._log_prefix, reason="before post-add mode")
                await runtime.async_send_plain_1912_hexes((command_hex,), target="add_device_post_add_mode")
                await _async_wait_for_post_add_mode_ack(runtime, notify_queue, ack_prefix_hex, log_prefix=self._log_prefix)
        finally:
            if notify_queue is not None:
                with suppress(Exception):
                    while not notify_queue.empty():
                        notify_queue.get_nowait()
            await runtime._disconnect_client()

    async def _async_connect_provisioning_runtime(
        self,
        runtime: PixieBluetoothRuntime,
        target_mac: str,
        *,
        attempts: int = 3,
    ) -> asyncio.Queue[bytes]:
        """Connect to the selected fresh Pixie target, retrying transient BLE failures."""
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return await runtime._connect_login_enable()
            except Exception as err:
                last_error = err
                message = str(err)
                if "rejected the provisioning login" in message or "Invalid login response" in message:
                    raise
                LOGGER.warning(
                    "%sPixie add-device target connection attempt %s/%s failed mac=%s: %s",
                    self._log_prefix,
                    attempt,
                    attempts,
                    target_mac,
                    err,
                )
                await runtime._disconnect_client()
                if attempt < attempts:
                    await asyncio.sleep(0.5)
        raise ConfigEntryError(f"Could not connect to selected Pixie device {target_mac}") from last_error

    async def _async_drain_add_device_notifications(
        self,
        runtime: PixieBluetoothRuntime,
        notify_queue: asyncio.Queue[bytes],
        *,
        reason: str,
    ) -> None:
        """Drain and log notifications that arrived before the next add-device step."""
        if runtime._session_key is None or runtime._device_mac is None:
            raise ConfigEntryError("Pixie BLE provisioning session is not ready")
        drained = 0
        while not notify_queue.empty():
            raw = notify_queue.get_nowait()
            drained += 1
            try:
                payload = decrypt_notification_packet(runtime._session_key, runtime._device_mac, raw)
            except Exception:
                LOGGER.debug("%sPixie add-device drained notification decrypt failed reason=%s raw=%s", self._log_prefix, reason, raw.hex(), exc_info=True)
                continue
            LOGGER.debug(
                "%sPixie add-device drained notification reason=%s payload=%s",
                self._log_prefix,
                reason,
                payload.hex(),
            )
        if drained:
            LOGGER.debug("%sPixie add-device drained %s notification(s) reason=%s", self._log_prefix, drained, reason)

    async def _async_wait_for_add_device_ack(
        self,
        runtime: PixieBluetoothRuntime,
        notify_queue: asyncio.Queue[bytes],
        device_id: int,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Wait for the captured e11102 acknowledgement after assigning a device id."""
        if runtime._session_key is None or runtime._device_mac is None:
            raise ConfigEntryError("Pixie BLE provisioning session is not ready")
        expected = b"\xe1\x11\x02" + int(device_id).to_bytes(2, "little")
        end_at = asyncio.get_running_loop().time() + max(0.1, timeout)
        while True:
            wait_left = end_at - asyncio.get_running_loop().time()
            if wait_left <= 0:
                raise ConfigEntryError(f"Pixie device did not confirm assigned id {device_id}")
            try:
                raw = await asyncio.wait_for(notify_queue.get(), timeout=wait_left)
            except TimeoutError as err:
                raise ConfigEntryError(f"Pixie device did not confirm assigned id {device_id}") from err
            try:
                payload = decrypt_notification_packet(runtime._session_key, runtime._device_mac, raw)
            except Exception:
                LOGGER.debug("%sPixie add-device acknowledgement decrypt failed raw=%s", self._log_prefix, raw.hex(), exc_info=True)
                continue
            if payload.startswith(expected):
                LOGGER.debug(
                    "%sPixie add-device acknowledgement received id=%s payload=%s",
                    self._log_prefix,
                    device_id,
                    payload.hex(),
                )
                return
            LOGGER.debug(
                "%sPixie add-device waiting for e11102 id=%s ignored payload=%s",
                self._log_prefix,
                device_id,
                payload.hex(),
            )

    async def _async_send_53216_management(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one 53216 gateway-management payload."""
        from .pixie_ha import _entry_gateway_ip, _handler_gateway_ip

        gateway_ip = _entry_gateway_ip(self.entry)
        if not gateway_ip:
            gateway_ip = _handler_gateway_ip(self.handler)
        if not gateway_ip:
            raise ConfigEntryError("Pixie gateway IP is not known")
        try:
            net_id = int(str(self.cloud_params.netid))
            mesh_net2 = int(str(self.cloud_params.meshnet2))
        except (TypeError, ValueError) as exc:
            raise ConfigEntryError("Pixie gateway local-inventory keys are not available") from exc
        return await self.coordinator.hass.async_add_executor_job(
            self.handler.send_53216_json,
            gateway_ip,
            net_id,
            mesh_net2,
            payload,
        )

    async def _async_gateway_add_device(self, record: DeviceRecord) -> None:
        """Persist a newly provisioned device through the Gateway G3 local inventory path."""
        await self._async_send_53216_management({
            "func": "deviceManager",
            "data": {
                "homeId": self.cloud_params.home_id,
                "flag": 2,
                "dev": _device_manager_payload_from_record(record, online=1),
            },
        })
        await self._async_send_53216_management({
            "func": "deviceManager",
            "data": {
                "homeId": self.cloud_params.home_id,
                "devs": [int(record.id)],
                "room_id": [-1],
                "flag": 6,
            },
        })

    async def async_remove_pixie_device(self, record: DeviceRecord) -> None:
        """Remove one Pixie device from the Pixie mesh/home before HA deletes it."""
        from .pixie_ha import _entry_gateway_supports_local_inventory_53216, _entry_inventory_mode

        inventory = self.pixie_runtime.inventory
        if inventory is None:
            raise ConfigEntryError("Pixie inventory is not available")
        ble_only = _entry_inventory_mode(self.entry) == INVENTORY_MODE_BLE_ADVERTISEMENT
        if not ble_only and not _entry_gateway_supports_local_inventory_53216(self.entry, inventory):
            raise ConfigEntryError("This Pixie gateway does not support local add/remove")
        if record.capabilities.is_gateway:
            raise ConfigEntryError("The Pixie gateway device cannot be removed from this entry")

        if ble_only:
            await self._async_remove_ble_only_device(record)
        else:
            await self._async_remove_gateway_device(record)

    async def _async_remove_ble_only_device(self, record: DeviceRecord) -> None:
        """Remove/deprovision one device through a BLE-only mesh connection."""
        runtime = await self.async_wait_for_ble_runtime_ready()
        if runtime is None or not runtime.health.healthy:
            raise ConfigEntryError("Pixie BLE runtime is not available for device removal")
        if record.runtime.presence == "online":
            identity_hex = _raw_management_packet(record.id, b"\xda\x11\x02\x10\x00")
            status_hex = _raw_management_packet(record.id, b"\xd9\x6b\x69\x00\x00\x00")
            await runtime.async_send_raw_core_hexes(record.id, (identity_hex, status_hex), target="remove_validate", delay_after=0.25)
        fc_packets = [
            _raw_management_packet(record.id, b"\xfc\x69\x69" + b"\x00" * 10),
            _raw_management_packet(record.id, b"\xfc\x69\x69" + b"\x00" * 10),
        ]
        await runtime.async_send_raw_core_hexes(record.id, tuple(fc_packets), target="remove_device", delay_after=0.2)

    async def _async_remove_gateway_device(self, record: DeviceRecord) -> None:
        """Remove/deprovision one device through Gateway G3 local management."""
        if record.runtime.presence == "online":
            validate_packets = (
                _raw_management_packet(record.id, b"\xd9\x6b\x69\x77\x00"),
                _raw_management_packet(record.id, b"\xda\x11\x02\x10\x00"),
            )
            await self._async_send_tcp_command(
                self.coordinator.hass,
                command_device_id=record.id,
                command_raw_hexes=validate_packets,
                command_raw_target="remove_validate",
                command_raw_repeat=0,
                command_raw_delay=0.25,
            )
        await self._async_send_53216_management({
            "func": "deviceManager",
            "data": {
                "homeId": self.cloud_params.home_id,
                "flag": 4,
                "dev": _device_manager_payload_from_record(record),
            },
        })
        fc_packets = (
            _raw_management_packet(record.id, b"\xfc\x69\x69" + b"\x00" * 10),
            _raw_management_packet(record.id, b"\xfc\x69\x69" + b"\x00" * 10),
        )
        await self._async_send_tcp_command(
            self.coordinator.hass,
            command_device_id=record.id,
            command_raw_hexes=fc_packets,
            command_raw_target="remove_device",
            command_raw_repeat=0,
            command_raw_delay=0.2,
        )
