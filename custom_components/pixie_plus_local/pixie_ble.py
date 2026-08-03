"""Bluetooth runtime for Pixie Plus through HA-managed ESPHome proxies."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import logging
import os
import time
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .pixie_inventory import PixieInventory
from .pixie_const import BT_ACCESS_NODE_AUTO, BT_ACCESS_NODE_PREFER_GATEWAY
from .pixie_runtime import (
    CloudParams,
    command_reply_route_kind,
    is_command_reply_route_packet,
    patch_command_reply_route,
)
from .pixie_ble_crypto import (
    build_login_packet,
    decrypt_notification_packet,
    encrypt_command_packet,
    fn_eacc,
    process_login_response,
)
LOGGER = logging.getLogger(__name__)

PIXIE_ADV_SERVICE_UUID = "0000cdab-0000-1000-8000-00805f9b34fb"
PIXIE_SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
PIXIE_CHAR_1911_SUFFIX = "1911"
PIXIE_CHAR_1912_SUFFIX = "1912"
PIXIE_CHAR_1914_SUFFIX = "1914"
PIXIE_DEFAULT_NAME = "Smart Light"
BLE_NOTIFY_WAIT_SECONDS = 5.0
BLE_CANDIDATE_CONNECT_TIMEOUT_SECONDS = 15.0
BLE_FAILED_CANDIDATE_DISCONNECT_TIMEOUT_SECONDS = 2.0
BLE_COMMAND_MIN_GAP_SECONDS = 0.075
BLE_BETTER_RSSI_DELTA_DBM = 10
BLE_ONLY_POST_LOGIN_COLLECT_SECONDS = 5.0
BLE_ONLY_IDENTITY_REPLY_SECONDS = 1.5
BLE_ONLY_CONNECT_SCAN_ATTEMPTS: tuple[float, ...] = (5.0, 5.0, 20.0)
BLE_ONLY_BROADCAST_HASH_KEY = b"skytone"
BT_STATE_DISABLED = "disabled"
BT_STATE_READY = "ready"
BT_STATE_NO_WORKING_PROXY = "no_working_proxy"
BT_STATE_UNAVAILABLE = "unavailable"


class _PixieBleReconnectRequested(RuntimeError):
    """Internal signal used to restart a BLE session without long backoff."""


def hash_mesh_id_to_broadcast(pin: str | int) -> str:
    """Return SAL Pixie BLE-only broadcast membership bytes for a PIN.

    This mirrors the native libbt_struct gmi/hashMeshIdToBroadcast routine.
    The returned hex is the little-endian byte order seen in BLE adverts.
    """
    mesh_id = int(str(pin).strip())
    value = mesh_id & 0xFFFFFFFF
    for idx in range(16):
        key_byte = BLE_ONLY_BROADCAST_HASH_KEY[idx % len(BLE_ONLY_BROADCAST_HASH_KEY)]
        signed_value = value if value < 0x80000000 else value - 0x100000000
        value = (((value ^ key_byte) << 8) & 0xFFFFFFFF) ^ ((signed_value >> 24) & 0xFFFFFFFF)
    return value.to_bytes(4, "little", signed=False).hex()


@dataclass
class PixieBluetoothHealth:
    """Small health snapshot for the optional BLE runtime path."""

    enabled: bool = False
    state: str = BT_STATE_DISABLED
    source: str | None = None
    access_node: str | None = None
    last_connected_at: float | None = None
    last_update_at: float | None = None
    reconnect_count: int = 0
    last_error: str | None = None

    @property
    def healthy(self) -> bool:
        """Return True when BLE is configured and currently usable."""
        return self.enabled and self.state == BT_STATE_READY and self.last_error is None


@dataclass
class _ESPHomeProxyRef:
    """A loaded HA ESPHome proxy entry that can perform native GATT calls."""

    entry_id: str
    title: str
    source: str
    client: Any
    feature_flags: int
    connections_free: int | None = None
    connections_limit: int | None = None


@dataclass(frozen=True)
class PixieFirmwareAdvertisement:
    """Firmware-bearing Pixie BLE advertisement decoded from a long manufacturer block."""

    mac: str
    version: int
    model_no: str
    device_id: int
    manufacturer_id: int | None = None
    rssi: int | None = None


@dataclass(frozen=True)
class PixieBleAdvertisementIdentity:
    """Full identity decoded from a long Pixie BLE manufacturer advertisement."""

    mac: str
    version: int
    model_no: str
    device_id: int
    product_type: int
    product_stype: int
    membership: str | None = None
    gateway_mesh_value: str | None = None
    byte10: int | None = None
    manufacturer_id: int | None = None
    rssi: int | None = None
    payload_hex: str | None = None


@dataclass
class PixieBleOnlyMeshDiscovery:
    """Inventory evidence collected after logging into a no-gateway Pixie mesh."""

    identities: list[PixieBleAdvertisementIdentity]
    pending_bledata_hex: list[str]
    seen_device_ids: set[int]
    health: PixieBluetoothHealth


@dataclass
class PixieBluetoothRuntime:
    """Own the optional Pixie BLE runtime session.

    The class owns the Pixie GATT login/notify/command path through HA's loaded
    ESPHome Bluetooth proxy client. It deliberately does not use local BlueZ.
    """

    hass: HomeAssistant
    cloud_params: CloudParams
    inventory: PixieInventory | None
    enabled: bool
    command_builder: Any | None = None
    inventory_update_callback: Callable[[PixieInventory], None] | None = None
    access_node_update_callback: Callable[..., None] | None = None
    health_update_callback: Callable[[], None] | None = None
    preferred_source: str | None = None
    preferred_access_node: str | None = None
    access_node_preference: str = BT_ACCESS_NODE_AUTO
    better_candidate_seen: bool = False
    login_seed: str | None = None
    membership: str | None = None
    send_runtime_sync_on_connect: bool = False
    send_notify_enable_writes: bool = True
    active_scan_duration: float = 5.0
    remember_access_node_on_connect: bool = True
    compare_access_node_after_connect: bool = True
    strict_preferred_access_node: bool = False
    proxy_refs: list[_ESPHomeProxyRef] | None = None
    health: PixieBluetoothHealth = field(default_factory=PixieBluetoothHealth)
    _task: asyncio.Task[None] | None = None
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    _client: Any | None = None
    _session_key: bytes | None = None
    _device_mac: bytes | None = None
    _ble_address_int: int | None = None
    _connection_unsub: Callable[[], None] | None = None
    _notify_stop: Callable[[], Any] | None = None
    _notify_remove: Callable[[], None] | None = None
    _char_1912: int | None = None
    _char_1914: int | None = None
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_1912_write_at: float | None = None
    _reconnect_event: asyncio.Event = field(default_factory=asyncio.Event)
    _reconnect_reason: str | None = None
    _active_candidate_rssi: int | None = None
    _active_proxy_entry_id: str | None = None
    _active_proxy_title: str | None = None
    _post_connect_scan_task: asyncio.Task[None] | None = None
    _pending_identity_requests: dict[int, asyncio.Future[PixieBleAdvertisementIdentity]] = field(default_factory=dict)

    @property
    def _log_prefix(self) -> str:
        home_name = str(getattr(self.cloud_params, "home_name", "") or "").strip()
        if home_name and home_name not in ("unknown", "None"):
            return f"[{home_name}] "
        return ""

    def _notify_health_update(self) -> None:
        """Notify the integration layer that BLE connection health changed."""
        if self.health_update_callback is None:
            return
        try:
            self.health_update_callback()
        except Exception:
            LOGGER.debug("%sPixie BLE health update callback failed", self._log_prefix, exc_info=True)

    async def async_start(self) -> None:
        """Start the BLE runtime if enabled."""
        self.health.enabled = self.enabled
        if not self.enabled:
            self.health.state = BT_STATE_DISABLED
            self._notify_health_update()
            return
        if self._task is not None and not self._task.done():
            return
        self.health.source = self.preferred_source
        self.health.access_node = self.preferred_access_node
        self.health.state = BT_STATE_UNAVAILABLE
        self.health.last_error = None
        self._notify_health_update()
        self._stop_event.clear()
        if hasattr(self.hass, "async_create_background_task"):
            try:
                self._task = self.hass.async_create_background_task(
                    self._run_session(),
                    name="pixie_plus_ble_runtime",
                    eager_start=True,
                )
            except TypeError:
                self._task = self.hass.async_create_background_task(
                    self._run_session(),
                    name="pixie_plus_ble_runtime",
                )
        else:
            self._task = self.hass.async_create_task(self._run_session(), name="pixie_plus_ble_runtime")

    async def async_shutdown(self) -> None:
        """Stop the BLE runtime task."""
        self._stop_event.set()
        if self._post_connect_scan_task is not None:
            self._post_connect_scan_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._post_connect_scan_task
            self._post_connect_scan_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None
        await self._disconnect_client()
        self.health.state = BT_STATE_DISABLED
        self.health.last_error = None
        self._notify_health_update()

    async def _run_session(self) -> None:
        """Run a recoverable Pixie BLE session loop."""
        while not self._stop_event.is_set():
            try:
                await self._connect_and_run_once()
            except asyncio.CancelledError:
                raise
            except _PixieBleReconnectRequested as err:
                self.health.state = BT_STATE_UNAVAILABLE
                self.health.last_error = str(err)
                self.health.reconnect_count += 1
                self._notify_health_update()
                LOGGER.warning("%sPixie BLE session reconnect requested: %s", self._log_prefix, err)
                await self._disconnect_client()
                await self._wait_before_retry(0.25)
                continue
            except Exception as err:
                self.health.state = BT_STATE_UNAVAILABLE
                self.health.last_error = str(err)
                self.health.reconnect_count += 1
                self._notify_health_update()
                LOGGER.warning("%sPixie BLE session stopped; retrying: %s", self._log_prefix, err)
                await self._disconnect_client()
                await self._wait_before_retry(min(60.0, 3.0 + self.health.reconnect_count))
                continue

    async def _wait_before_retry(self, timeout: float) -> None:
        """Wait for retry delay, stop, or an explicit reconnect request."""
        stop_task = asyncio.create_task(self._stop_event.wait())
        reconnect_task = asyncio.create_task(self._reconnect_event.wait())
        try:
            await asyncio.wait(
                {stop_task, reconnect_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if reconnect_task.done() and reconnect_task.result():
                self._reconnect_event.clear()
        finally:
            for task in (stop_task, reconnect_task):
                if not task.done():
                    task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(stop_task, reconnect_task)

    async def _connect_and_run_once(self) -> None:
        """Connect, login, enable notifications, and wait until disconnected."""
        self._reconnect_event.clear()
        self._reconnect_reason = None
        notify_queue = await self._connect_login_enable()
        while not self._stop_event.is_set():
            if self._reconnect_event.is_set():
                reason = self._reconnect_reason or "BLE reconnect requested"
                self._reconnect_event.clear()
                self._reconnect_reason = None
                raise _PixieBleReconnectRequested(reason)
            notify_task = asyncio.create_task(notify_queue.get())
            reconnect_task = asyncio.create_task(self._reconnect_event.wait())
            pending: set[asyncio.Task[Any]] = set()
            try:
                done, pending = await asyncio.wait(
                    {notify_task, reconnect_task},
                    timeout=BLE_NOTIFY_WAIT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                for task in (notify_task, reconnect_task):
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.gather(notify_task, reconnect_task)
                raise
            finally:
                for task in pending:
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.gather(*pending)
            if reconnect_task in done and reconnect_task.result():
                reason = self._reconnect_reason or "BLE reconnect requested"
                self._reconnect_event.clear()
                self._reconnect_reason = None
                raise _PixieBleReconnectRequested(reason)
            if notify_task not in done:
                continue
            self._handle_notification(notify_task.result())

    async def _connect_and_run_probe(self) -> None:
        """Connect far enough to prove this HA ESPHome proxy path works."""
        await self._connect_login_enable()

    async def _connect_login_enable(self) -> asyncio.Queue[bytes]:
        """Connect, login, enable 1911 notifications, and return the notify queue."""
        gateway_address = self._preferred_access_node_from_inventory()
        prefer_gateway = self.access_node_preference == BT_ACCESS_NODE_PREFER_GATEWAY and gateway_address
        preferred_address: str | None = None
        preferred_reason = "none"
        scan_fallback_address: str | None = None
        if prefer_gateway:
            preferred_address = gateway_address
            preferred_reason = "gateway preference"
        elif not self.better_candidate_seen:
            if self.preferred_access_node:
                if self._preferred_access_node_allowed(self.preferred_access_node):
                    preferred_address = self.preferred_access_node
                    preferred_reason = "selected target" if self.strict_preferred_access_node else "persisted strongest node"
                else:
                    scan_fallback_address = self.preferred_access_node
                    LOGGER.info(
                        "%sPixie BLE ignored persisted access node outside this home inventory: %s",
                        self._log_prefix,
                        self.preferred_access_node,
                    )
        else:
            scan_fallback_address = self.preferred_access_node
            LOGGER.info(
                "%sPixie BLE previous scan found a better candidate; scanning before selecting access node",
                self._log_prefix,
            )

        if preferred_address:
            LOGGER.info(
                "%sPixie BLE access-node preference active reason=%s preferred=%s persisted=%s",
                self._log_prefix,
                preferred_reason,
                preferred_address,
                self.preferred_access_node,
            )
        proxies = self.proxy_refs if self.proxy_refs is not None else _iter_esphome_bluetooth_proxies(self.hass)
        if preferred_address:
            cached_candidates = await _async_discover_candidates(
                self.hass,
                preferred_source=self.preferred_source,
                preferred_address=preferred_address,
                proxies=proxies,
                active_scan=False,
                include_discovered_service_info=False,
            )
            if cached_candidates:
                try:
                    return await self._try_connect_login_candidates(
                        self._rank_candidates(cached_candidates),
                        gateway_address=gateway_address,
                    )
                except Exception as err:
                    if self.strict_preferred_access_node and "Invalid login response" in str(err):
                        raise RuntimeError(
                            f"Selected Pixie BLE device {preferred_address} rejected the provisioning login"
                        ) from err
                    LOGGER.debug(
                        "%sPixie BLE cached preferred access-node path failed; falling back to active scan: %s",
                        self._log_prefix,
                        err,
                    )
            else:
                LOGGER.debug(
                    "%sPixie BLE preferred access-node %s was not in the HA Bluetooth cache; falling back to active scan",
                    self._log_prefix,
                    preferred_address,
                )
            if self.strict_preferred_access_node:
                candidates = await _async_discover_candidates(
                    self.hass,
                    preferred_source=self.preferred_source,
                    preferred_address=preferred_address,
                    proxies=proxies,
                    active_scan=True,
                    scan_duration=self.active_scan_duration,
                    include_discovered_service_info=True,
                )
                candidates = [
                    candidate
                    for candidate in candidates
                    if _normalize_mac(str(candidate.get("address") or "")) == _normalize_mac(preferred_address)
                ]
                if not candidates:
                    raise RuntimeError(f"Selected Pixie BLE device {preferred_address} was not found during active scan")
                return await self._try_connect_login_candidates(
                    self._rank_candidates(candidates),
                    gateway_address=gateway_address,
                )

        candidates = await _async_discover_candidates(
            self.hass,
            preferred_source=self.preferred_source,
            preferred_address=None,
            proxies=proxies,
            active_scan=True,
            scan_duration=self.active_scan_duration,
            include_discovered_service_info=True,
        )
        if not candidates:
            if scan_fallback_address:
                LOGGER.debug(
                    "%sPixie BLE scan found no candidates; trying previous persisted node %s",
                    self._log_prefix,
                    scan_fallback_address,
                )
                fallback_candidates = await _async_discover_candidates(
                    self.hass,
                    preferred_source=self.preferred_source,
                    preferred_address=scan_fallback_address,
                    proxies=proxies,
                    active_scan=False,
                    include_discovered_service_info=False,
                )
                if fallback_candidates:
                    return await self._try_connect_login_candidates(
                        self._rank_candidates(fallback_candidates),
                        gateway_address=gateway_address,
                    )
            raise RuntimeError("No connectable Pixie BLE advertisement found")

        try:
            return await self._try_connect_login_candidates(
                self._rank_candidates(candidates),
                gateway_address=gateway_address,
            )
        except Exception:
            if not scan_fallback_address:
                raise
            LOGGER.debug(
                "%sPixie BLE scanned candidates failed; trying previous persisted node %s",
                self._log_prefix,
                scan_fallback_address,
            )
            fallback_candidates = await _async_discover_candidates(
                self.hass,
                preferred_source=self.preferred_source,
                preferred_address=scan_fallback_address,
                proxies=proxies,
                active_scan=False,
                include_discovered_service_info=False,
            )
            if not fallback_candidates:
                raise
            return await self._try_connect_login_candidates(
                self._rank_candidates(fallback_candidates),
                gateway_address=gateway_address,
            )

    async def _try_connect_login_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        gateway_address: str | None,
    ) -> asyncio.Queue[bytes]:
        """Try candidates in order until one completes the Pixie login flow."""
        last_error: Exception | None = None
        for index, candidate in enumerate(candidates, start=1):
            address = candidate.get("address")
            source = candidate.get("source")
            rssi = candidate.get("rssi")
            is_gateway = _normalize_mac(str(address or "")) == _normalize_mac(str(gateway_address or ""))
            LOGGER.debug(
                "%sPixie BLE trying candidate %s/%s address=%s source=%s rssi=%s gateway=%s timeout=%.1fs",
                self._log_prefix,
                index,
                len(candidates),
                address,
                source,
                rssi,
                is_gateway,
                BLE_CANDIDATE_CONNECT_TIMEOUT_SECONDS,
            )
            try:
                return await asyncio.wait_for(
                    self._connect_login_enable_candidate(candidate, gateway_address=gateway_address),
                    timeout=BLE_CANDIDATE_CONNECT_TIMEOUT_SECONDS,
                )
            except Exception as err:
                last_error = err
                LOGGER.debug(
                    "%sPixie BLE candidate failed address=%s source=%s rssi=%s gateway=%s; trying next if available: %r",
                    self._log_prefix,
                    address,
                    source,
                    rssi,
                    is_gateway,
                    err,
                )
                await self._disconnect_client(timeout=BLE_FAILED_CANDIDATE_DISCONNECT_TIMEOUT_SECONDS)
        raise RuntimeError(f"No Pixie BLE candidate completed login/notify flow: {last_error}")

    def _known_inventory_macs(self) -> set[str]:
        """Return BLE MACs that belong to this Pixie home inventory."""
        if self.inventory is None:
            return set()
        macs: set[str] = set()
        for rec in self.inventory.devices_by_id.values():
            normalized = _normalize_mac(str(getattr(rec, "mac", "") or ""))
            if normalized:
                macs.add(normalized)
        gateway = self.inventory.gateway
        gateway_mac = _normalize_mac(str(getattr(gateway, "gateway_mac", "") or "")) if gateway is not None else ""
        if gateway_mac:
            macs.add(gateway_mac)
        return macs

    def _preferred_access_node_allowed(self, address: str) -> bool:
        """Return whether a persisted access-node hint can be tried directly."""
        known_macs = self._known_inventory_macs()
        if not known_macs:
            return True
        return _normalize_mac(str(address or "")) in known_macs

    def _access_node_reply_route_node_id(self) -> int | None:
        """Return the connected access node's Pixie device id for reply routing."""
        if self.inventory is None:
            return None
        access_node = _normalize_mac(str(self.health.access_node or ""))
        if not access_node:
            return None
        for dev_id, rec in self.inventory.devices_by_id.items():
            if _normalize_mac(str(getattr(rec, "mac", "") or "")) == access_node:
                return int(dev_id)
        gateway = self.inventory.gateway
        gateway_mac = _normalize_mac(str(getattr(gateway, "gateway_mac", "") or "")) if gateway is not None else ""
        if gateway_mac and gateway_mac == access_node:
            for dev_id, rec in self.inventory.devices_by_id.items():
                if getattr(getattr(rec, "capabilities", None), "is_gateway", False):
                    return int(dev_id)
        return None

    def _filter_home_candidates(self, candidates: list[dict[str, Any]], *, context: str) -> list[dict[str, Any]]:
        """Keep candidates that are known members of this Pixie home or mesh."""
        expected_membership = str(self.membership or "").strip().lower()
        if expected_membership:
            matching = [
                candidate
                for candidate in candidates
                if str(candidate.get("membership") or "").strip().lower() == expected_membership
            ]
            mismatched = [
                candidate
                for candidate in candidates
                if candidate.get("membership")
                and str(candidate.get("membership") or "").strip().lower() != expected_membership
            ]
            unknown = [candidate for candidate in candidates if not candidate.get("membership")]
            if matching:
                if mismatched or unknown:
                    LOGGER.debug(
                        "%sPixie BLE filtered access-node candidates by membership during %s: expected=%s matching=%s unknown=%s mismatched=%s",
                        self._log_prefix,
                        context,
                        expected_membership,
                        len(matching),
                        len(unknown),
                        [
                            {
                                "address": item.get("address"),
                                "source": item.get("source"),
                                "rssi": item.get("rssi"),
                                "membership": item.get("membership"),
                            }
                            for item in mismatched
                        ],
                    )
                return matching
            if mismatched:
                candidates = unknown
                LOGGER.debug(
                    "%sPixie BLE ignored %s wrong-membership access-node candidate(s) during %s expected=%s",
                    self._log_prefix,
                    len(mismatched),
                    context,
                    expected_membership,
                )

        known_macs = self._known_inventory_macs()
        if not known_macs:
            return candidates
        kept: list[dict[str, Any]] = []
        ignored: list[dict[str, Any]] = []
        for candidate in candidates:
            address = str(candidate.get("address") or "").upper()
            if _normalize_mac(address) in known_macs:
                kept.append(candidate)
            else:
                ignored.append(candidate)
        if ignored:
            LOGGER.debug(
                "%sPixie BLE ignored %s non-inventory access-node candidate(s) during %s: %s",
                self._log_prefix,
                len(ignored),
                context,
                [
                    {
                        "address": item.get("address"),
                        "source": item.get("source"),
                        "rssi": item.get("rssi"),
                        "membership": item.get("membership"),
                    }
                    for item in ignored
                ],
            )
        if not kept and candidates:
            LOGGER.debug(
                "%sPixie BLE no access-node candidates matched this home inventory during %s",
                self._log_prefix,
                context,
            )
        return kept

    def _rank_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rank candidates by gateway preference, RSSI, and practical tie-breakers."""
        candidates = self._filter_home_candidates(candidates, context="connection ranking")
        gateway_address = self._preferred_access_node_from_inventory()
        prefer_gateway = self.access_node_preference == BT_ACCESS_NODE_PREFER_GATEWAY and gateway_address

        def score(candidate: dict[str, Any]) -> tuple[int, int, int, int]:
            address = str(candidate.get("address") or "").upper()
            source = str(candidate.get("source") or "").upper()
            rssi = int(candidate.get("rssi") or -999)
            gateway_bonus = 10000 if prefer_gateway and address == gateway_address else 0
            source_score = 10 if self.preferred_source and source == str(self.preferred_source).upper() else 0
            return (
                gateway_bonus,
                rssi,
                source_score,
                int(getattr(candidate.get("proxy"), "connections_free", 0) or 0),
            )

        ranked = sorted(candidates, key=score, reverse=True)
        LOGGER.debug(
            "%sPixie BLE candidate ranking preference=%s candidates=%s",
            self._log_prefix,
            self.access_node_preference,
            [
                {
                    "address": item.get("address"),
                    "source": item.get("source"),
                    "rssi": item.get("rssi"),
                    "membership": item.get("membership"),
                    "gateway": str(item.get("address") or "").upper() == str(gateway_address or "").upper(),
                }
                for item in ranked
            ],
        )
        return ranked

    def _preferred_access_node_from_inventory(self) -> str | None:
        """Return the gateway BLE MAC from inventory, if available."""
        gateway = self.inventory.gateway if self.inventory is not None else None
        gateway_mac = str(getattr(gateway, "gateway_mac", "") or "").strip()
        if not gateway_mac:
            return None
        normalized = _normalize_mac(gateway_mac)
        if len(normalized) != 12:
            return None
        return ":".join(normalized[i : i + 2] for i in range(0, 12, 2)).upper()

    async def _connect_login_enable_candidate(
        self,
        candidate: dict[str, Any],
        *,
        gateway_address: str | None,
    ) -> asyncio.Queue[bytes]:
        """Connect/login/enable one candidate and return its notification queue."""
        proxy: _ESPHomeProxyRef = candidate["proxy"]
        address = str(candidate["address"]).upper()
        address_int = _mac_to_int(address)
        address_type = int(candidate.get("address_type", 0))
        self.health.source = proxy.source
        self.health.access_node = address
        self._active_proxy_entry_id = proxy.entry_id
        self._active_proxy_title = proxy.title
        self._active_candidate_rssi = int(candidate.get("rssi") or -999)
        self._device_mac = _mac_bytes(address)
        self._ble_address_int = address_int

        client = proxy.client
        self._client = client
        await self._native_connect(client, address_int, address_type, proxy)
        self.health.last_connected_at = time.time()
        self.health.last_error = None

        services_model = await client.bluetooth_gatt_get_services(address_int)
        chars = _resolve_pixie_chars(services_model.services)
        char_1914 = chars.get(PIXIE_CHAR_1914_SUFFIX)
        char_1911 = chars.get(PIXIE_CHAR_1911_SUFFIX)
        char_1912 = chars.get(PIXIE_CHAR_1912_SUFFIX)
        if char_1914 is None or char_1911 is None or char_1912 is None:
            raise RuntimeError(f"Pixie GATT characteristics missing: discovered={sorted(chars)}")
        char_1914_handle = int(getattr(char_1914, "handle"))
        char_1911_handle = int(getattr(char_1911, "handle"))
        self._char_1914 = char_1914_handle
        self._char_1912 = int(getattr(char_1912, "handle"))

        login_pkt, rand_phone = build_login_packet(
            device_name=PIXIE_DEFAULT_NAME,
            netid=str(self.login_seed if self.login_seed is not None else self.cloud_params.netid),
        )
        await client.bluetooth_gatt_write(address_int, char_1914_handle, login_pkt, response=True, timeout=20.0)
        login_rsp = bytes(await client.bluetooth_gatt_read(address_int, char_1914_handle, timeout=20.0))
        self._session_key = process_login_response(
            login_rsp,
            rand_phone,
            device_name=PIXIE_DEFAULT_NAME,
            netid=str(self.login_seed if self.login_seed is not None else self.cloud_params.netid),
        )

        notify_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _notify_handler(_handle: int, data: bytearray | bytes) -> None:
            notify_queue.put_nowait(bytes(data))

        self._notify_stop, self._notify_remove = await client.bluetooth_gatt_start_notify(
            address_int,
            char_1911_handle,
            _notify_handler,
            timeout=10.0,
        )
        if self.send_notify_enable_writes:
            await client.bluetooth_gatt_write(address_int, char_1911_handle, b"\x01", response=True, timeout=20.0)
            await asyncio.sleep(0.05)
            await client.bluetooth_gatt_write(address_int, char_1911_handle, b"\x01", response=True, timeout=20.0)
        if self.send_runtime_sync_on_connect:
            await self._send_runtime_sync_request()
        self.health.enabled = self.enabled
        self.health.state = BT_STATE_READY
        self.health.last_error = None
        self.health.last_update_at = time.time()
        self._notify_health_update()
        LOGGER.info(
            "%sPixie BLE session ready via %s source=%s rssi=%s",
            self._log_prefix,
            address,
            self.health.source,
            self._active_candidate_rssi,
        )
        if self.remember_access_node_on_connect:
            self._remember_access_node(better_candidate_seen=False)
        if (
            self.compare_access_node_after_connect
            and not (
                self.access_node_preference == BT_ACCESS_NODE_PREFER_GATEWAY
                and gateway_address
                and address == gateway_address
            )
        ):
            self._schedule_post_connect_comparison_scan()
        return notify_queue

    async def _send_runtime_sync_request(self) -> None:
        """Send the BLE-only c56969 runtime sync request observed in the Pixie app."""
        if self._client is None or self._session_key is None or self._device_mac is None or self._char_1912 is None:
            return
        timestamp = int(time.time()).to_bytes(4, byteorder="little", signed=False)
        plain_pkt = os.urandom(3) + b"\x03\x04\xff\xff\xc5\x69\x69" + timestamp + b"\x00"
        encrypted = encrypt_command_packet(self._session_key, self._device_mac, plain_pkt)
        LOGGER.debug(
            "%sPixie BLE 1912 runtime sync plain=%s cipher=%s",
            self._log_prefix,
            plain_pkt.hex(),
            encrypted.hex(),
        )
        await self._client.bluetooth_gatt_write(self._ble_address_int, self._char_1912, encrypted, response=False)

    async def _send_identity_request(self, device_id: int) -> None:
        """Request db1102 identity metadata from one logged-in mesh device."""
        if self._client is None or self._session_key is None or self._device_mac is None or self._char_1912 is None:
            return
        try:
            target = int(device_id)
        except (TypeError, ValueError):
            return
        if target <= 0 or target > 255:
            return
        plain_pkt = (
            os.urandom(3)
            + b"\x03\x04"
            + int(target).to_bytes(2, byteorder="little")
            + b"\xda\x11\x02\x10\x00"
        )
        encrypted = encrypt_command_packet(self._session_key, self._device_mac, plain_pkt)
        LOGGER.debug(
            "%sPixie BLE 1912 identity request target=%s plain=%s cipher=%s",
            self._log_prefix,
            target,
            plain_pkt.hex(),
            encrypted.hex(),
        )
        await self._client.bluetooth_gatt_write(self._ble_address_int, self._char_1912, encrypted, response=False)

    def _remember_access_node(
        self,
        *,
        better_candidate_seen: bool | None = None,
    ) -> None:
        """Publish learned access-node selection hints to the integration layer."""
        if self.access_node_update_callback is None:
            return
        self.access_node_update_callback(
            self.health.source,
            self.health.access_node,
            proxy_entry_id=self._active_proxy_entry_id,
            proxy_title=self._active_proxy_title,
            rssi=self._active_candidate_rssi,
            better_candidate_seen=better_candidate_seen,
        )

    def _schedule_post_connect_comparison_scan(self) -> None:
        """Schedule a short scan to decide whether the next session should rescan."""
        if self._post_connect_scan_task is not None and not self._post_connect_scan_task.done():
            return
        if self._active_candidate_rssi is None or self._active_candidate_rssi <= -999:
            return
        self._post_connect_scan_task = asyncio.create_task(self._async_post_connect_comparison_scan())

    async def _async_post_connect_comparison_scan(self) -> None:
        """Look for a much stronger node, but leave the current session stable."""
        await asyncio.sleep(1.0)
        if self._stop_event.is_set() or self.health.state != BT_STATE_READY:
            return
        proxies = self.proxy_refs if self.proxy_refs is not None else _iter_esphome_bluetooth_proxies(self.hass)
        candidates = await _async_discover_candidates(
            self.hass,
            preferred_source=self.preferred_source,
            preferred_address=None,
            proxies=proxies,
            active_scan=True,
            include_discovered_service_info=True,
        )
        candidates = self._filter_home_candidates(candidates, context="post-connect comparison")
        current_address = str(self.health.access_node or "").upper()
        current_rssi = int(self._active_candidate_rssi or -999)
        best: dict[str, Any] | None = None
        for candidate in self._rank_candidates(candidates):
            address = str(candidate.get("address") or "").upper()
            if not address or address == current_address:
                continue
            best = candidate
            break
        if best is None:
            LOGGER.debug("%sPixie BLE post-connect scan found no alternate candidate", self._log_prefix)
            return
        best_rssi = int(best.get("rssi") or -999)
        delta = best_rssi - current_rssi
        if delta >= BLE_BETTER_RSSI_DELTA_DBM:
            LOGGER.info(
                "%sPixie BLE stronger candidate seen for next reconnect current=%s rssi=%s better=%s rssi=%s delta=%s",
                self._log_prefix,
                current_address,
                current_rssi,
                best.get("address"),
                best_rssi,
                delta,
            )
            self._remember_access_node(better_candidate_seen=True)
        else:
            LOGGER.debug(
                "%sPixie BLE post-connect scan kept current node current=%s rssi=%s best=%s rssi=%s delta=%s",
                self._log_prefix,
                current_address,
                current_rssi,
                best.get("address"),
                best_rssi,
                delta,
            )

    async def _native_connect(
        self,
        client: Any,
        address_int: int,
        address_type: int,
        proxy: _ESPHomeProxyRef,
    ) -> None:
        """Connect to a Pixie node through the HA-owned ESPHome API client."""
        events: list[tuple[bool, int, int]] = []

        def _connection_state(connected: bool, mtu: int, error: int) -> None:
            events.append((connected, mtu, error))
            if self.health.state != BT_STATE_READY or self._stop_event.is_set():
                return
            if connected and error == 0:
                return
            self._request_reconnect(
                f"BLE connection state changed connected={connected} mtu={mtu} error={error}"
            )

        self._connection_unsub = await client.bluetooth_device_connect(
            address_int,
            _connection_state,
            timeout=30.0,
            disconnect_timeout=20.0,
            feature_flags=proxy.feature_flags,
            has_cache=False,
            address_type=address_type,
        )
        if not any(connected and error == 0 for connected, _mtu, error in events):
            raise RuntimeError(f"ESPHome proxy connect returned without a successful connected event: {events}")

    async def async_send_command(self, command_kwargs: dict[str, Any]) -> None:
        """Send a command via BLE 1912."""
        if self._client is None or self._session_key is None or self._device_mac is None or self._char_1912 is None:
            raise RuntimeError("Pixie BLE session is not ready")
        plain_packets = self._build_plain_1912_packets(command_kwargs)
        async with self._write_lock:
            for index, (plain_pkt, delay) in enumerate(plain_packets, start=1):
                await self._pace_1912_write()
                encrypted = encrypt_command_packet(self._session_key, self._device_mac, plain_pkt)
                LOGGER.debug(
                    "%sPixie BLE 1912 write %s/%s plain=%s cipher=%s delay_after=%.3fs kwargs=%s",
                    self._log_prefix,
                    index,
                    len(plain_packets),
                    plain_pkt.hex(),
                    encrypted.hex(),
                    delay,
                    _redact_command_kwargs(command_kwargs),
                )
                try:
                    await self._client.bluetooth_gatt_write(self._ble_address_int, self._char_1912, encrypted, response=False)
                except Exception as err:
                    reason = f"BLE command write failed: {err}"
                    self._request_reconnect(reason)
                    await self._disconnect_client()
                    raise RuntimeError(reason) from err
                self._last_1912_write_at = time.monotonic()
                if delay:
                    await asyncio.sleep(delay)

    async def async_send_raw_core_hexes(
        self,
        device_id: int,
        raw_hexes: list[str] | tuple[str, ...],
        *,
        target: str = "raw_management",
        delay_after: float = 0.0,
    ) -> None:
        """Send captured transport-neutral command cores through the current BLE session."""
        command_kwargs = {
            "command_device_id": int(device_id),
            "command_raw_hexes": tuple(str(item) for item in raw_hexes),
            "command_raw_target": target,
            "command_raw_delay": float(delay_after),
        }
        await self.async_send_command(command_kwargs)

    async def async_send_plain_1912_hexes(
        self,
        plain_hexes: list[str] | tuple[str, ...],
        *,
        target: str,
        delay_after: float = 0.0,
        response: bool = True,
    ) -> None:
        """Encrypt and send exact plaintext 1912 packets without the command planner."""
        if self._client is None or self._session_key is None or self._device_mac is None or self._char_1912 is None:
            raise RuntimeError("Pixie BLE session is not ready")
        async with self._write_lock:
            for index, plain_hex in enumerate(plain_hexes, start=1):
                raw = bytes.fromhex("".join(str(plain_hex).split()))
                await self._pace_1912_write()
                encrypted = encrypt_command_packet(self._session_key, self._device_mac, raw)
                LOGGER.debug(
                    "%sPixie BLE 1912 direct write target=%s %s/%s plain=%s cipher=%s",
                    self._log_prefix,
                    target,
                    index,
                    len(plain_hexes),
                    raw.hex(),
                    encrypted.hex(),
                )
                await self._client.bluetooth_gatt_write(
                    self._ble_address_int,
                    self._char_1912,
                    encrypted,
                    response=response,
                    timeout=20.0,
                )
                self._last_1912_write_at = time.monotonic()
                if delay_after:
                    await asyncio.sleep(delay_after)

    async def _pace_1912_write(self) -> None:
        """Keep a small gap between writes to the Pixie command characteristic."""
        if self._last_1912_write_at is None:
            return
        wait_for = BLE_COMMAND_MIN_GAP_SECONDS - (time.monotonic() - self._last_1912_write_at)
        if wait_for <= 0:
            return
        LOGGER.debug("%sPixie BLE command pacing wait %.3fs", self._log_prefix, wait_for)
        await asyncio.sleep(wait_for)

    def _encrypt_1914_block(self, command: int, plaintext16: bytes) -> bytes:
        """Build a 1914 provisioning block using the logged-in session key."""
        if self._session_key is None:
            raise RuntimeError("Pixie BLE session key is not available")
        if len(plaintext16) != 16:
            raise ValueError(f"1914 provisioning block must be 16 bytes, got {len(plaintext16)}")
        return bytes([int(command) & 0xFF]) + fn_eacc(self._session_key, plaintext16) + b"\x00\x00\x00"

    async def async_write_provisioning_blocks(
        self,
        *,
        netid: str,
        mesh_key: bytes = bytes.fromhex("c0c1c2c3c4c5c6c7d8d9dadbdcdddedf"),
    ) -> None:
        """Write the captured 1914 provisioning blocks to the connected fresh target."""
        if self._client is None or self._ble_address_int is None or self._char_1914 is None:
            raise RuntimeError("Pixie BLE provisioning session is not ready")
        blocks = (
            (0x04, PIXIE_DEFAULT_NAME.encode().ljust(16, b"\x00")[:16]),
            (0x05, str(netid).encode().ljust(16, b"\x00")[:16]),
            (0x06, bytes(mesh_key).ljust(16, b"\x00")[:16]),
        )
        for command, plain in blocks:
            packet = self._encrypt_1914_block(command, plain)
            LOGGER.debug(
                "%sPixie BLE 1914 provisioning write command=0x%02x plain=%s packet=%s",
                self._log_prefix,
                command,
                plain.hex(),
                packet.hex(),
            )
            await self._client.bluetooth_gatt_write(
                self._ble_address_int,
                self._char_1914,
                packet,
                response=True,
                timeout=20.0,
            )
            await asyncio.sleep(0.15)
        with suppress(Exception):
            response = bytes(await self._client.bluetooth_gatt_read(
                self._ble_address_int,
                self._char_1914,
                timeout=5.0,
            ))
            LOGGER.debug("%sPixie BLE 1914 provisioning read response=%s", self._log_prefix, response.hex())

    def _build_plain_1912_packets(self, command_kwargs: dict[str, Any]) -> list[tuple[bytes, float]]:
        """Build plaintext 1912 command packets from the shared core command plan."""
        builder = self.command_builder
        if builder is None:
            raise RuntimeError("No Pixie command builder is available")
        build_core_command_plan = getattr(builder, "build_core_command_plan", None)
        if not callable(build_core_command_plan):
            raise RuntimeError("Pixie command builder does not support shared core command plans")
        plan = build_core_command_plan(command_kwargs)
        if not plan.packets:
            raise RuntimeError(f"Unsupported BLE command kwargs: {sorted(command_kwargs)}")
        reply_route_id = self._access_node_reply_route_node_id()
        packets: list[tuple[bytes, float]] = []
        for packet in plan.packets:
            command_hex, old_route, new_route, route_kind = patch_command_reply_route(
                packet.command_hex,
                reply_route_id,
            )
            if old_route is not None:
                LOGGER.debug(
                    "%sCommand reply route resolved transport=ble kind=%s device=%s reply_node=%s old=%s new=%s access_node=%s",
                    self._log_prefix,
                    route_kind,
                    plan.device_id,
                    reply_route_id,
                    old_route,
                    new_route,
                    self.health.access_node,
                )
            else:
                try:
                    is_reply_route = is_command_reply_route_packet(bytes.fromhex(packet.command_hex))
                    route_kind = route_kind or command_reply_route_kind(bytes.fromhex(packet.command_hex))
                except ValueError:
                    is_reply_route = False
                if is_reply_route:
                    LOGGER.debug(
                        "%sCommand reply route not patched transport=ble kind=%s device=%s reason=no_access_node_route_id access_node=%s",
                        self._log_prefix,
                        route_kind,
                        plan.device_id,
                        self.health.access_node,
                    )
            packets.append((bytes.fromhex(command_hex), packet.delay_after))
        return packets

    def _handle_notification(self, raw: bytes) -> None:
        """Decrypt and classify a raw 1911 notification."""
        if self._session_key is None or self._device_mac is None:
            return
        try:
            payload = decrypt_notification_packet(self._session_key, self._device_mac, raw)
        except Exception as err:
            LOGGER.debug("%sPixie BLE notification decrypt failed len=%s raw=%s err=%s", self._log_prefix, len(raw), raw.hex(), err)
            return
        self.mark_update()
        hint = _decode_ble_payload(raw, payload)
        LOGGER.debug("%sPixie BLE notification raw=%s payload=%s hint=%s", self._log_prefix, raw.hex(), payload.hex(), hint)
        self._resolve_identity_request_from_hint(hint)
        try:
            applied = self._apply_ble_payload_hint(hint)
        except Exception:
            LOGGER.exception("%sPixie BLE notification apply failed raw=%s payload=%s hint=%s", self._log_prefix, raw.hex(), payload.hex(), hint)
            return
        if applied and self.inventory is not None and self.inventory_update_callback is not None:
            self.inventory_update_callback(self.inventory)

    def _resolve_identity_request_from_hint(self, hint: dict[str, Any]) -> None:
        """Resolve a pending one-shot db1102 identity request, if this packet is its reply."""
        identity = _identity_from_db1102_hint(hint, membership=self.membership)
        if identity is None:
            return
        future = self._pending_identity_requests.get(int(identity.device_id))
        if future is not None and not future.done():
            future.set_result(identity)

    async def async_request_identity(self, device_id: int, *, timeout: float = BLE_ONLY_IDENTITY_REPLY_SECONDS) -> PixieBleAdvertisementIdentity | None:
        """Request and await one db1102 identity response for a logged-in mesh device."""
        try:
            target = int(device_id)
        except (TypeError, ValueError):
            return None
        if target <= 0 or target > 255:
            return None
        if self._client is None or self._session_key is None or self._device_mac is None or self._char_1912 is None:
            raise RuntimeError("Pixie BLE session is not ready")

        existing = self._pending_identity_requests.get(target)
        if existing is not None and not existing.done():
            with suppress(TimeoutError):
                return await asyncio.wait_for(asyncio.shield(existing), timeout=max(0.1, float(timeout)))
            return None

        future: asyncio.Future[PixieBleAdvertisementIdentity] = self.hass.loop.create_future()
        self._pending_identity_requests[target] = future
        try:
            await self._send_identity_request(target)
            return await asyncio.wait_for(future, timeout=max(0.1, float(timeout)))
        except TimeoutError:
            LOGGER.debug("%sPixie BLE identity request timed out target=%s", self._log_prefix, target)
            return None
        finally:
            if self._pending_identity_requests.get(target) is future:
                self._pending_identity_requests.pop(target, None)

    def _apply_ble_payload_hint(self, hint: dict[str, Any]) -> int:
        """Apply decoded BLE notification by feeding the shared runtime parser."""
        if self.inventory is None or self.command_builder is None:
            return 0
        apply_bledata_hex = getattr(self.command_builder, "apply_bledata_hex", None)
        if not callable(apply_bledata_hex):
            return 0
        core_hex = _core_bledata_hex_from_1911_hint(hint)
        if not core_hex:
            return 0
        return int(apply_bledata_hex(
            core_hex,
            payload_meta={"type": "bleData", "transport": "ble_1911", "hint": hint},
            source="ble_runtime",
            bulk_source="ble_runtime",
            full_snapshot=False,
            queue_bulk=False,
            notify_inventory=False,
        ) or 0)

    async def _disconnect_client(self, *, timeout: float = 10.0) -> None:
        client = self._client
        address_int = self._ble_address_int
        notify_stop = self._notify_stop
        notify_remove = self._notify_remove
        connection_unsub = self._connection_unsub
        self._client = None
        self._session_key = None
        self._device_mac = None
        self._ble_address_int = None
        self._connection_unsub = None
        self._notify_stop = None
        self._notify_remove = None
        self._char_1912 = None
        self._char_1914 = None
        if client is None:
            return
        if notify_stop is not None:
            try:
                result = notify_stop()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                LOGGER.debug("%sError while stopping Pixie BLE notification", self._log_prefix, exc_info=True)
                if notify_remove is not None:
                    try:
                        notify_remove()
                    except Exception:
                        LOGGER.debug("%sError while removing Pixie BLE notification callback", self._log_prefix, exc_info=True)
        elif notify_remove is not None:
            try:
                notify_remove()
            except Exception:
                LOGGER.debug("%sError while removing Pixie BLE notification callback", self._log_prefix, exc_info=True)
        if connection_unsub is not None:
            try:
                connection_unsub()
            except Exception:
                LOGGER.debug("%sError while removing Pixie BLE connection callback", self._log_prefix, exc_info=True)
        if address_int is None:
            return
        if not _esphome_client_api_connected(client):
            LOGGER.debug(
                "%sSkipping Pixie BLE device disconnect because ESPHome API is not connected",
                self._log_prefix,
            )
            return
        try:
            await client.bluetooth_device_disconnect(address_int, timeout=timeout)
        except Exception as err:
            if timeout <= BLE_FAILED_CANDIDATE_DISCONNECT_TIMEOUT_SECONDS:
                LOGGER.debug(
                    "%sPixie BLE failed-candidate disconnect did not complete within %.1fs: %r",
                    self._log_prefix,
                    timeout,
                    err,
                )
            else:
                LOGGER.debug("%sError while disconnecting Pixie BLE client", self._log_prefix, exc_info=True)

    def mark_update(self) -> None:
        """Record that BLE delivered a usable runtime update."""
        self.health.last_update_at = time.time()
        self.health.last_error = None
        self.health.state = BT_STATE_READY
        self._notify_health_update()

    def _request_reconnect(self, reason: str) -> None:
        """Ask the runtime loop to reconnect without depending on notification traffic."""
        if self._stop_event.is_set():
            return
        self._reconnect_reason = reason
        self._reconnect_event.set()
        self.health.state = BT_STATE_UNAVAILABLE
        self.health.last_error = reason
        self._notify_health_update()

    def mark_proxy_unavailable(self, reason: str) -> None:
        """Mark the upstream ESPHome proxy unavailable and request a reconnect."""
        self._request_reconnect(reason)

    def request_reconnect(self, reason: str) -> None:
        """Request a BLE reconnect from the HA event loop."""
        self._request_reconnect(reason)

    def esphome_api_connected(self) -> bool:
        """Return whether the selected ESPHome API client is known connected."""
        return _esphome_client_api_connected(self._client)

    async def async_request_reconnect(self, reason: str) -> None:
        """Ask the runtime loop to reconnect the current BLE session."""
        self._request_reconnect(reason)
        await self._disconnect_client()


async def async_probe_pixie_bluetooth_proxy(
    hass: HomeAssistant,
    cloud_params: CloudParams,
    inventory: PixieInventory | None,
    *,
    preferred_source: str | None = None,
    preferred_access_node: str | None = None,
    login_seed: str | None = None,
    membership: str | None = None,
    send_runtime_sync_on_connect: bool = False,
    timeout: float = 12.0,
) -> PixieBluetoothHealth:
    """Probe for a working Pixie-capable HA Bluetooth proxy source.

    This is the integration entry point for the future concrete Pixie probe. It
    currently returns a bounded, explicit no-proxy result instead of falling back
    to BlueZ or claiming the ESPHome API subscription directly.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    attempt = 0
    proxies = _iter_esphome_bluetooth_proxies(hass)
    home_name = str(getattr(cloud_params, "home_name", "") or "").strip()
    log_prefix = f"[{home_name}] " if home_name and home_name not in ("unknown", "None") else ""
    runtime = PixieBluetoothRuntime(
        hass=hass,
        cloud_params=cloud_params,
        inventory=inventory,
        enabled=True,
        preferred_source=preferred_source,
        preferred_access_node=preferred_access_node,
        proxy_refs=proxies,
        login_seed=login_seed,
        membership=membership,
        send_runtime_sync_on_connect=send_runtime_sync_on_connect,
    )
    runtime.health.enabled = True
    LOGGER.info(
        "%sPixie BLE install probe starting timeout=%.1fs esphome_proxy_count=%s proxies=%s",
        log_prefix,
        timeout,
        len(proxies),
        [
            {
                "entry": proxy.title,
                "source": proxy.source,
                "connections_free": proxy.connections_free,
                "connections_limit": proxy.connections_limit,
                "feature_flags": f"0x{proxy.feature_flags:x}",
            }
            for proxy in proxies
        ],
    )
    while time.monotonic() < deadline:
        attempt += 1
        try:
            remaining = deadline - time.monotonic()
            await asyncio.wait_for(runtime._connect_and_run_probe(), timeout=max(5.0, min(35.0, remaining)))
        except Exception as err:
            last_error = err
            LOGGER.warning("%sPixie BLE install probe attempt %s failed: %s", log_prefix, attempt, err)
            await runtime._disconnect_client()
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(3.0, max(0.2, deadline - time.monotonic())))
            continue
        health = runtime.health
        await runtime._disconnect_client()
        LOGGER.info(
            "%sPixie BLE install probe succeeded source=%s access_node=%s",
            log_prefix,
            health.source,
            health.access_node,
        )
        return health
    await runtime._disconnect_client()
    LOGGER.warning("%sPixie BLE install probe failed after %s attempt(s): %s", log_prefix, attempt, last_error)
    return PixieBluetoothHealth(
        enabled=True,
        state=BT_STATE_NO_WORKING_PROXY,
        source=runtime.health.source or preferred_source,
        access_node=runtime.health.access_node or preferred_access_node,
        last_error=str(last_error) if last_error is not None else "Timed out probing Pixie BLE proxy path.",
    )


async def async_discover_ble_only_mesh_inventory(
    hass: HomeAssistant,
    cloud_params: CloudParams,
    *,
    home_name: str,
    pin: str,
    membership: str,
    inventory: PixieInventory | None,
    preferred_source: str | None = None,
    preferred_access_node: str | None = None,
) -> PixieBleOnlyMeshDiscovery:
    """Login to a no-gateway Pixie mesh and request identities for observed devices."""
    last_error: Exception | None = None
    log_prefix = f"[{home_name}] " if home_name else ""

    for attempt, scan_duration in enumerate(BLE_ONLY_CONNECT_SCAN_ATTEMPTS, start=1):
        runtime = PixieBluetoothRuntime(
            hass=hass,
            cloud_params=cloud_params,
            inventory=inventory,
            enabled=True,
            preferred_source=preferred_source,
            preferred_access_node=preferred_access_node,
            login_seed=pin,
            membership=membership,
            send_runtime_sync_on_connect=True,
            active_scan_duration=scan_duration,
        )
        try:
            LOGGER.info(
                "%sPixie BLE-only mesh discovery connecting attempt=%s active_scan=%.1fs",
                log_prefix,
                attempt,
                scan_duration,
            )
            notify_queue = await runtime._connect_login_enable()
            result = await _async_collect_ble_only_mesh_inventory(
                runtime,
                notify_queue,
                membership=membership,
            )
            if result.identities:
                return result
            last_error = RuntimeError("logged in but no db1102 identity responses were received")
            LOGGER.info(
                "%sPixie BLE-only mesh discovery found no identities after login; retrying while setup timeout remains",
                log_prefix,
            )
        except Exception as err:
            last_error = err
            LOGGER.info(
                "%sPixie BLE-only mesh discovery attempt=%s failed: %s",
                log_prefix,
                attempt,
                err,
            )
        finally:
            await runtime._disconnect_client()
        if attempt < len(BLE_ONLY_CONNECT_SCAN_ATTEMPTS):
            await asyncio.sleep(1.0)

    raise RuntimeError(f"Pixie BLE-only mesh discovery failed: {last_error}")


async def async_probe_ble_only_mesh_login(
    hass: HomeAssistant,
    cloud_params: CloudParams,
    *,
    home_name: str,
    pin: str,
    membership: str,
    inventory: PixieInventory | None = None,
    preferred_source: str | None = None,
    preferred_access_node: str | None = None,
) -> PixieBluetoothHealth:
    """Login to a no-gateway Pixie mesh without building inventory."""
    last_error: Exception | None = None
    log_prefix = f"[{home_name}] " if home_name else ""

    for attempt, scan_duration in enumerate(BLE_ONLY_CONNECT_SCAN_ATTEMPTS, start=1):
        runtime = PixieBluetoothRuntime(
            hass=hass,
            cloud_params=cloud_params,
            inventory=inventory,
            enabled=True,
            preferred_source=preferred_source,
            preferred_access_node=preferred_access_node,
            login_seed=pin,
            membership=membership,
            send_runtime_sync_on_connect=False,
            active_scan_duration=scan_duration,
        )
        try:
            LOGGER.info(
                "%sPixie BLE-only login probe connecting attempt=%s active_scan=%.1fs",
                log_prefix,
                attempt,
                scan_duration,
            )
            await runtime._connect_login_enable()
            return PixieBluetoothHealth(
                enabled=True,
                state=runtime.health.state,
                source=runtime.health.source,
                access_node=runtime.health.access_node,
                last_connected_at=runtime.health.last_connected_at,
                last_update_at=runtime.health.last_update_at,
                reconnect_count=runtime.health.reconnect_count,
                last_error=runtime.health.last_error,
            )
        except Exception as err:
            last_error = err
            LOGGER.info(
                "%sPixie BLE-only login probe attempt=%s failed: %s",
                log_prefix,
                attempt,
                err,
            )
        finally:
            await runtime._disconnect_client()
        if attempt < len(BLE_ONLY_CONNECT_SCAN_ATTEMPTS):
            await asyncio.sleep(1.0)

    raise RuntimeError(f"Pixie BLE-only login probe failed: {last_error}")


async def _async_collect_ble_only_mesh_inventory(
    runtime: PixieBluetoothRuntime,
    notify_queue: asyncio.Queue[bytes],
    *,
    membership: str,
) -> PixieBleOnlyMeshDiscovery:
    """Collect post-login dc1102 device IDs and db1102 identity replies."""
    identities_by_mac: dict[str, PixieBleAdvertisementIdentity] = {}
    pending_bledata_hex: list[str] = []
    seen_device_ids: set[int] = set()

    def handle_raw(raw: bytes) -> dict[str, Any] | None:
        if runtime._session_key is None or runtime._device_mac is None:
            return None
        try:
            payload = decrypt_notification_packet(runtime._session_key, runtime._device_mac, raw)
        except Exception:
            LOGGER.debug("%sPixie BLE-only discovery could not decrypt notification", runtime._log_prefix, exc_info=True)
            return None
        hint = _decode_ble_payload(raw, payload)
        core_hex = _core_bledata_hex_from_1911_hint(hint)
        if core_hex and str(hint.get("prefix") or "") != "db1102":
            pending_bledata_hex.append(core_hex)
        for record in hint.get("records") or []:
            try:
                dev_id = int(record.get("device_id"))
            except (AttributeError, TypeError, ValueError):
                continue
            if 0 < dev_id <= 255:
                seen_device_ids.add(dev_id)
        identity = _identity_from_db1102_hint(hint, membership=membership)
        if identity is not None:
            identities_by_mac[_normalize_mac(identity.mac)] = identity
            seen_device_ids.add(identity.device_id)
        return hint

    async def drain_for(duration: float, *, stop_when_ids: set[int] | None = None) -> None:
        end_at = time.monotonic() + max(0.0, duration)
        while time.monotonic() < end_at:
            wait_left = max(0.05, end_at - time.monotonic())
            try:
                raw = await asyncio.wait_for(notify_queue.get(), timeout=wait_left)
            except TimeoutError:
                break
            hint = handle_raw(raw)
            if stop_when_ids and hint is not None:
                identity = _identity_from_db1102_hint(hint, membership=membership)
                if identity is not None and identity.device_id in stop_when_ids:
                    return

    await drain_for(BLE_ONLY_POST_LOGIN_COLLECT_SECONDS)

    if runtime.inventory is not None:
        for dev_id in runtime.inventory.devices_by_id:
            try:
                seen_device_ids.add(int(dev_id))
            except (TypeError, ValueError):
                continue

    LOGGER.info(
        "%sPixie BLE-only mesh discovery post-login records seen_ids=%s identities=%s",
        runtime._log_prefix,
        sorted(seen_device_ids),
        len(identities_by_mac),
    )

    for dev_id in sorted(seen_device_ids):
        if any(identity.device_id == dev_id for identity in identities_by_mac.values()):
            continue
        await runtime._send_identity_request(dev_id)
        await drain_for(BLE_ONLY_IDENTITY_REPLY_SECONDS, stop_when_ids={dev_id})

    LOGGER.info(
        "%sPixie BLE-only mesh discovery completed seen_ids=%s identity_responses=%s access_node=%s source=%s",
        runtime._log_prefix,
        sorted(seen_device_ids),
        len(identities_by_mac),
        runtime.health.access_node,
        runtime.health.source,
    )
    return PixieBleOnlyMeshDiscovery(
        identities=list(identities_by_mac.values()),
        pending_bledata_hex=pending_bledata_hex,
        seen_device_ids=seen_device_ids,
        health=PixieBluetoothHealth(
            enabled=True,
            state=runtime.health.state,
            source=runtime.health.source,
            access_node=runtime.health.access_node,
            last_connected_at=runtime.health.last_connected_at,
            last_update_at=runtime.health.last_update_at,
            reconnect_count=runtime.health.reconnect_count,
            last_error=runtime.health.last_error,
        ),
    )


async def _async_discover_candidates(
    hass: HomeAssistant,
    *,
    preferred_source: str | None,
    preferred_address: str | None,
    proxies: list[_ESPHomeProxyRef] | None = None,
    active_scan: bool = True,
    scan_duration: float = 5.0,
    include_discovered_service_info: bool = True,
) -> list[dict[str, Any]]:
    """Return currently discovered Pixie BLE candidates."""
    from homeassistant.components import bluetooth

    if active_scan:
        try:
            await bluetooth.async_request_active_scan(hass, duration=scan_duration)
        except TypeError:
            await bluetooth.async_request_active_scan(hass)
        except Exception:
            LOGGER.debug("Pixie BLE active scan request failed", exc_info=True)

    candidates: list[dict[str, Any]] = []
    pixie_seen = 0
    unresolved_sources: set[str] = set()
    if preferred_address:
        scanner_devices = bluetooth.async_scanner_devices_by_address(
            hass,
            preferred_address,
            connectable=True,
        )
        for scanner_device in scanner_devices or []:
            scanner = getattr(scanner_device, "scanner", None)
            source = getattr(scanner, "source", None)
            if preferred_source and source and str(source).lower() != preferred_source.lower():
                continue
            proxy = _resolve_esphome_proxy(hass, source, proxies=proxies)
            if proxy is None:
                unresolved_sources.add(str(source))
                continue
            _add_candidate(
                candidates,
                address=preferred_address,
                source=proxy.source,
                proxy=proxy,
                ble_device=scanner_device.ble_device,
                address_type=_address_type_from_ble_device(scanner_device.ble_device),
                rssi=getattr(scanner_device.advertisement, "rssi", -999),
                membership=_candidate_membership_from_advertisement(scanner_device.advertisement),
            )

    if include_discovered_service_info:
        for service_info in bluetooth.async_discovered_service_info(hass, connectable=True) or []:
            if not _is_pixie_service_info(service_info):
                continue
            pixie_seen += 1
            source = getattr(service_info, "source", None)
            if preferred_source and source and str(source).lower() != preferred_source.lower():
                continue
            proxy = _resolve_esphome_proxy(hass, source, proxies=proxies)
            if proxy is None:
                unresolved_sources.add(str(source))
                LOGGER.debug(
                    "Skipping Pixie BLE candidate %s from non-ESPHome source %s",
                    getattr(service_info, "address", None),
                    source,
                )
                continue
            _add_candidate(
                candidates,
                address=service_info.address,
                source=proxy.source,
                proxy=proxy,
                ble_device=service_info.device,
                address_type=_address_type_from_ble_device(service_info.device),
                rssi=getattr(service_info, "rssi", -999),
                membership=_candidate_membership_from_advertisement(service_info),
            )

    if not candidates:
        if active_scan or include_discovered_service_info:
            LOGGER.warning(
                "Pixie BLE discovery found no usable ESPHome-proxy candidates; pixie_seen=%s unresolved_sources=%s",
                pixie_seen,
                sorted(unresolved_sources),
            )
        return []
    candidates.sort(key=lambda item: (
        1 if preferred_address and str(item["address"]).upper() == preferred_address.upper() else 0,
        1 if preferred_source and str(item.get("source") or "").lower() == preferred_source.lower() else 0,
        int(getattr(item.get("proxy"), "connections_free", 0) or 0),
        int(item.get("rssi") or -999),
    ), reverse=True)
    return candidates


async def async_scan_pixie_firmware_advertisements(
    hass: HomeAssistant,
    *,
    duration: float = 10.0,
) -> list[PixieFirmwareAdvertisement]:
    """Request a short active scan and return firmware-bearing Pixie advertisements."""
    identities = await async_scan_pixie_advertisement_identities(hass, duration=duration)
    return [
        PixieFirmwareAdvertisement(
            mac=identity.mac,
            version=identity.version,
            model_no=identity.model_no,
            device_id=identity.device_id,
            manufacturer_id=identity.manufacturer_id,
            rssi=identity.rssi,
        )
        for identity in identities
    ]


async def async_scan_pixie_advertisement_identities(
    hass: HomeAssistant,
    *,
    duration: float = 10.0,
) -> list[PixieBleAdvertisementIdentity]:
    """Request a short active scan and return full Pixie long-advertisement identities."""
    from homeassistant.components import bluetooth

    adverts: dict[str, PixieBleAdvertisementIdentity] = {}
    live_seen = 0
    live_decoded = 0
    live_undecoded: dict[str, dict[str, Any]] = {}
    cache_seen: set[tuple[str, str | None]] = set()

    def _service_info_summary(service_info: Any) -> dict[str, Any]:
        manufacturer_data = getattr(service_info, "manufacturer_data", None) or {}
        service_data = getattr(service_info, "service_data", None) or {}
        source = getattr(service_info, "source", None)
        return {
            "address": getattr(service_info, "address", None),
            "name": getattr(service_info, "name", None),
            "source": source,
            "rssi": getattr(service_info, "rssi", None),
            "manufacturer_ids": sorted(str(key) for key in manufacturer_data.keys()) if isinstance(manufacturer_data, dict) else [],
            "manufacturer_lengths": {
                str(key): len(bytes(value or b""))
                for key, value in manufacturer_data.items()
            } if isinstance(manufacturer_data, dict) else {},
            "service_uuids": list(getattr(service_info, "service_uuids", None) or []),
            "service_data_uuids": sorted(str(key) for key in service_data.keys()) if isinstance(service_data, dict) else [],
        }

    def _collect_service_info(service_info: Any, *, origin: str) -> None:
        nonlocal live_decoded
        decoded = decode_pixie_advertisement_identities(service_info)
        if decoded:
            for advert in decoded:
                adverts[advert.mac] = advert
            if origin == "live":
                live_decoded += len(decoded)
            return
        if origin != "live" or not _is_pixie_service_info(service_info):
            return
        summary = _service_info_summary(service_info)
        key = "%s|%s|%s|%s" % (
            summary.get("address"),
            summary.get("source"),
            summary.get("manufacturer_lengths"),
            summary.get("service_uuids"),
        )
        live_undecoded[key] = summary

    def collect_cached_adverts() -> None:
        for connectable in (True, False):
            try:
                service_infos = bluetooth.async_discovered_service_info(hass, connectable=connectable) or []
            except TypeError:
                if not connectable:
                    continue
                service_infos = bluetooth.async_discovered_service_info(hass) or []
            for service_info in service_infos:
                cache_seen.add((str(getattr(service_info, "address", "") or ""), getattr(service_info, "source", None)))
                _collect_service_info(service_info, origin="cache")

    @callback
    def _async_live_advertisement(service_info: Any, _change: Any) -> None:
        nonlocal live_seen
        live_seen += 1
        _collect_service_info(service_info, origin="live")

    async def request_active_scan() -> None:
        try:
            await bluetooth.async_request_active_scan(hass, duration=duration)
        except TypeError:
            await bluetooth.async_request_active_scan(hass)
        except Exception:
            LOGGER.debug("Pixie BLE firmware-version active scan request failed", exc_info=True)

    cancel_callbacks: list[Any] = []
    for connectable in (True, False):
        for matcher in (
            {"service_uuid": PIXIE_ADV_SERVICE_UUID, "connectable": connectable},
            {"manufacturer_id": 0x0211, "connectable": connectable},
        ):
            try:
                cancel_callbacks.append(
                    bluetooth.async_register_callback(
                        hass,
                        _async_live_advertisement,
                        matcher,
                        bluetooth.BluetoothScanningMode.ACTIVE,
                    )
                )
            except Exception:
                LOGGER.debug("Pixie BLE live advertisement callback registration failed matcher=%s", matcher, exc_info=True)

    scan_task = asyncio.create_task(request_active_scan())
    deadline = time.monotonic() + max(0.0, duration)
    try:
        while time.monotonic() < deadline:
            collect_cached_adverts()
            await asyncio.sleep(0.25)
        collect_cached_adverts()
        with suppress(TimeoutError):
            await asyncio.wait_for(scan_task, timeout=1.0)
    finally:
        if not scan_task.done():
            scan_task.cancel()
            with suppress(asyncio.CancelledError):
                await scan_task
        for cancel_callback in cancel_callbacks:
            with suppress(Exception):
                cancel_callback()
    LOGGER.debug(
        "Pixie BLE identity scan finished duration=%.1fs identities=%s live_events=%s live_identity_events=%s cache_service_infos=%s live_undecoded_pixie_like=%s",
        duration,
        len(adverts),
        live_seen,
        live_decoded,
        len(cache_seen),
        len(live_undecoded),
    )
    if live_undecoded:
        LOGGER.debug(
            "Pixie BLE identity scan saw undecoded Pixie-like live adverts: %s",
            list(live_undecoded.values())[:25],
        )
    return list(adverts.values())


def _parse_raw_ble_advertisement_payload(data: bytes) -> dict[str, Any]:
    """Parse raw BLE AD structures into the pieces Pixie discovery needs."""
    name = ""
    service_uuids: list[str] = []
    manufacturer_blocks: list[tuple[int, bytes]] = []
    idx = 0
    while idx < len(data):
        length = data[idx]
        idx += 1
        if length == 0:
            break
        if idx + length > len(data):
            break
        ad_type = data[idx]
        value = data[idx + 1 : idx + length]
        idx += length

        if ad_type in (0x08, 0x09):
            name = value.decode("utf-8", errors="replace")
        elif ad_type in (0x02, 0x03):
            for pos in range(0, len(value) - 1, 2):
                short_uuid = int.from_bytes(value[pos : pos + 2], "little")
                service_uuids.append(f"0000{short_uuid:04x}-0000-1000-8000-00805f9b34fb")
        elif ad_type in (0x06, 0x07):
            for pos in range(0, len(value) - 15, 16):
                raw = value[pos : pos + 16]
                service_uuids.append(
                    f"{raw[3::-1].hex()}-{raw[5:3:-1].hex()}-"
                    f"{raw[7:5:-1].hex()}-{raw[8:10].hex()}-{raw[10:16].hex()}"
                )
        elif ad_type == 0xFF and len(value) >= 2:
            manufacturer_id = int.from_bytes(value[0:2], "little")
            manufacturer_blocks.append((manufacturer_id, value[2:]))

    manufacturer_ids = sorted({manufacturer_id for manufacturer_id, _data in manufacturer_blocks})
    manufacturer_lengths: dict[str, list[int]] = {}
    manufacturer_hex: dict[str, list[str]] = {}
    for manufacturer_id, block in manufacturer_blocks:
        key = str(manufacturer_id)
        manufacturer_lengths.setdefault(key, []).append(len(block))
        if LOGGER.isEnabledFor(logging.DEBUG):
            manufacturer_hex.setdefault(key, []).append(block.hex())
    parsed: dict[str, Any] = {
        "name": name,
        "service_uuids": service_uuids,
        "manufacturer_blocks": manufacturer_blocks,
        "manufacturer_ids": manufacturer_ids,
        "manufacturer_lengths": manufacturer_lengths,
    }
    if manufacturer_hex:
        parsed["manufacturer_hex"] = manufacturer_hex
    return parsed


def _is_pixie_raw_advertisement(parsed: dict[str, Any]) -> bool:
    """Return True when raw advertisement data looks Pixie-related."""
    name = str(parsed.get("name") or "").strip().lower()
    service_uuids = {str(uuid).lower() for uuid in parsed.get("service_uuids") or []}
    manufacturer_ids = set(parsed.get("manufacturer_ids") or [])
    return (
        name == "smart light"
        or PIXIE_ADV_SERVICE_UUID in service_uuids
        or 0x0211 in manufacturer_ids
    )


def _decode_pixie_raw_advertisement_identities(
    data: bytes,
    *,
    address: str,
    rssi: int | None,
) -> list[PixieBleAdvertisementIdentity]:
    """Decode Pixie identity advertisements from raw AD structures."""
    parsed = _parse_raw_ble_advertisement_payload(data)
    decoded: dict[str, PixieBleAdvertisementIdentity] = {}
    for manufacturer_id, block in parsed["manufacturer_blocks"]:
        for identity in _decode_pixie_identity_manufacturer_block(
            block,
            manufacturer_id=manufacturer_id,
            rssi=rssi,
        ):
            decoded[identity.mac] = identity
    if decoded:
        return list(decoded.values())
    # Some stacks can present manufacturer bytes at unexpected boundaries; keep
    # one full-packet scan as a diagnostic fallback for raw ESPHome events.
    for identity in _decode_pixie_identity_manufacturer_block(
        data,
        manufacturer_id=None,
        rssi=rssi,
    ):
        decoded[identity.mac] = identity
    return list(decoded.values())


def decode_pixie_advertisement_identities(service_info: Any) -> list[PixieBleAdvertisementIdentity]:
    """Decode full Pixie identity from long manufacturer advertisements."""
    manufacturer_data = getattr(service_info, "manufacturer_data", None) or {}
    rssi = getattr(service_info, "rssi", None)
    decoded: dict[str, PixieBleAdvertisementIdentity] = {}
    if not isinstance(manufacturer_data, dict):
        return []
    for manufacturer_id, data in manufacturer_data.items():
        try:
            manufacturer_int = int(manufacturer_id)
        except (TypeError, ValueError):
            manufacturer_int = None
        for advert in _decode_pixie_identity_manufacturer_block(
            bytes(data or b""),
            manufacturer_id=manufacturer_int,
            rssi=rssi,
        ):
            decoded[advert.mac] = advert
    return list(decoded.values())


def _candidate_membership_from_advertisement(advertisement: Any) -> str | None:
    """Return Pixie BLE-only membership from an advert-like HA object when present."""
    for identity in decode_pixie_advertisement_identities(advertisement):
        membership = str(identity.membership or "").strip().lower()
        if membership:
            return membership
    return None


def decode_pixie_firmware_advertisements(service_info: Any) -> list[PixieFirmwareAdvertisement]:
    """Decode firmware-bearing long Pixie manufacturer advertisements from HA service info."""
    return [
        PixieFirmwareAdvertisement(
            mac=identity.mac,
            version=identity.version,
            model_no=identity.model_no,
            device_id=identity.device_id,
            manufacturer_id=identity.manufacturer_id,
            rssi=identity.rssi,
        )
        for identity in decode_pixie_advertisement_identities(service_info)
    ]


def _decode_pixie_firmware_manufacturer_block(
    data: bytes,
    *,
    manufacturer_id: int | None,
    rssi: int | None,
) -> list[PixieFirmwareAdvertisement]:
    return [
        PixieFirmwareAdvertisement(
            mac=identity.mac,
            version=identity.version,
            model_no=identity.model_no,
            device_id=identity.device_id,
            manufacturer_id=identity.manufacturer_id,
            rssi=identity.rssi,
        )
        for identity in _decode_pixie_identity_manufacturer_block(
            data,
            manufacturer_id=manufacturer_id,
            rssi=rssi,
        )
    ]


def _decode_pixie_identity_manufacturer_block(
    data: bytes,
    *,
    manufacturer_id: int | None,
    rssi: int | None,
) -> list[PixieBleAdvertisementIdentity]:
    """Decode Pixie long manufacturer blocks.

    HA and Android expose manufacturer data at different byte boundaries, so
    this scans for the Pixie marker instead of assuming a single fixed prefix.
    Short identity-only blocks are ignored because they do not carry firmware.
    """
    adverts: list[PixieBleAdvertisementIdentity] = []
    search = data
    if manufacturer_id in (0x0211, 0x0422):
        search = b"\x11\x02" + data
    marker = b"\x11\x02"
    for idx in range(0, max(0, len(search) - 9)):
        if search[idx : idx + 2] != marker:
            continue
        payload = search[idx + 2 :]
        if len(payload) < 8:
            continue
        mac_tail_le = payload[:4]
        full_mac = bytes([0x00, 0x21]) + mac_tail_le[::-1]
        if len(full_mac) != 6 or full_mac[2] != 0x4D:
            continue
        product_type = payload[4]
        product_stype = payload[5]
        packed_version = payload[6]
        device_id = payload[7]
        version = packed_version >> 2
        if not (0 < version < 100):
            continue
        byte10 = payload[8] if len(payload) >= 9 else None
        membership = payload[9:13].hex() if len(payload) >= 13 else None
        gateway_mesh_value = payload[13:17].hex() if len(payload) >= 17 else None
        mac = ":".join(f"{byte:02X}" for byte in full_mac)
        adverts.append(PixieBleAdvertisementIdentity(
            mac=mac,
            version=version,
            model_no=f"{product_type:02d}{product_stype:02d}",
            device_id=device_id,
            product_type=product_type,
            product_stype=product_stype,
            membership=membership,
            gateway_mesh_value=gateway_mesh_value,
            byte10=byte10,
            manufacturer_id=manufacturer_id,
            rssi=rssi,
            payload_hex=payload.hex(),
        ))

    return adverts


def _add_candidate(
    candidates: list[dict[str, Any]],
    *,
    address: str,
    source: str,
    proxy: _ESPHomeProxyRef,
    ble_device: Any,
    address_type: int,
    rssi: int,
    membership: str | None = None,
) -> None:
    """Append a candidate unless the same address/source pair is already queued."""
    address_upper = str(address).upper()
    source_upper = str(source).upper()
    normalized_membership = str(membership or "").strip().lower() or None
    for item in candidates:
        if (
            str(item.get("address", "")).upper() == address_upper
            and str(item.get("source", "")).upper() == source_upper
        ):
            if normalized_membership and not item.get("membership"):
                item["membership"] = normalized_membership
            return
    candidates.append({
        "address": address_upper,
        "source": source,
        "proxy": proxy,
        "ble_device": ble_device,
        "address_type": address_type,
        "rssi": rssi,
        "membership": normalized_membership,
    })


def _resolve_esphome_proxy(
    hass: HomeAssistant,
    source: Any,
    *,
    proxies: list[_ESPHomeProxyRef] | None = None,
) -> _ESPHomeProxyRef | None:
    """Resolve a HA Bluetooth scanner source to a loaded ESPHome proxy client."""
    proxies = list(proxies if proxies is not None else _iter_esphome_bluetooth_proxies(hass))
    source_norm = _normalize_mac(str(source or ""))
    if source_norm:
        for proxy in proxies:
            if source_norm == _normalize_mac(proxy.source):
                return proxy
    if len(proxies) == 1:
        LOGGER.debug(
            "Using only loaded ESPHome Bluetooth proxy %s for unresolved scanner source %s",
            proxies[0].source,
            source,
        )
        return proxies[0]
    return None


def _esphome_client_api_connected(client: Any | None) -> bool:
    """Return whether an ESPHome API client currently has an authenticated connection."""
    if client is None:
        return False
    connection = getattr(client, "_connection", None)
    if connection is None:
        return False
    return bool(getattr(connection, "is_connected", False))


def _iter_esphome_bluetooth_proxies(hass: HomeAssistant) -> list[_ESPHomeProxyRef]:
    """Return loaded ESPHome Bluetooth proxies that can do native GATT."""
    try:
        from aioesphomeapi import BluetoothProxyFeature
    except Exception:
        LOGGER.debug("aioesphomeapi is unavailable; cannot use ESPHome Bluetooth proxy")
        return []

    proxies: list[_ESPHomeProxyRef] = []
    for entry in hass.config_entries.async_entries("esphome"):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        entry_data = getattr(entry, "runtime_data", None)
        if entry_data is None or not bool(getattr(entry_data, "available", False)):
            continue
        device_info = getattr(entry_data, "device_info", None)
        client = getattr(entry_data, "client", None)
        if device_info is None or client is None:
            continue
        bluetooth_device = getattr(entry_data, "bluetooth_device", None)
        possible_sources = [
            getattr(bluetooth_device, "mac_address", None),
            getattr(device_info, "bluetooth_mac_address", None),
            getattr(device_info, "mac_address", None),
        ]
        api_version = getattr(entry_data, "api_version", None) or getattr(client, "api_version", None)
        try:
            feature_flags = int(device_info.bluetooth_proxy_feature_flags_compat(api_version))
        except Exception:
            LOGGER.debug("ESPHome entry %s has no usable Bluetooth proxy feature flags", entry.title)
            continue
        required = BluetoothProxyFeature.ACTIVE_CONNECTIONS | BluetoothProxyFeature.REMOTE_CACHING
        if feature_flags & required != required:
            LOGGER.debug(
                "ESPHome entry %s lacks required proxy features flags=0x%x required=0x%x",
                entry.title,
                feature_flags,
                int(required),
            )
            continue
        normalized_sources = [_normalize_mac(str(candidate or "")) for candidate in possible_sources]
        source_value = next((candidate for candidate in normalized_sources if candidate), "")
        if not source_value:
            continue
        proxies.append(_ESPHomeProxyRef(
            entry_id=entry.entry_id,
            title=entry.title,
            source=_format_mac(source_value),
            client=client,
            feature_flags=feature_flags,
            connections_free=getattr(bluetooth_device, "ble_connections_free", None),
            connections_limit=getattr(bluetooth_device, "ble_connections_limit", None),
        ))
    return proxies


def _is_pixie_service_info(service_info: Any) -> bool:
    name = (getattr(service_info, "name", None) or "").strip().lower()
    uuids = {str(uuid).lower() for uuid in (getattr(service_info, "service_uuids", None) or [])}
    manufacturer_data = getattr(service_info, "manufacturer_data", None) or {}
    return (
        name == "smart light"
        or PIXIE_ADV_SERVICE_UUID in uuids
        or 0x0211 in manufacturer_data
    )


def _resolve_pixie_chars(services: Any) -> dict[str, Any]:
    chars: dict[str, Any] = {}
    for service in services:
        for char in getattr(service, "characteristics", []) or []:
            uuid = str(getattr(char, "uuid", "")).lower()
            for suffix in (PIXIE_CHAR_1911_SUFFIX, PIXIE_CHAR_1912_SUFFIX, PIXIE_CHAR_1914_SUFFIX):
                if uuid.endswith(suffix):
                    chars[suffix] = char
    return chars


def _mac_bytes(address: str) -> bytes:
    return bytes.fromhex(address.replace(":", "").replace("-", ""))


def _mac_to_int(address: str) -> int:
    return int(address.replace(":", "").replace("-", ""), 16)


def _normalize_mac(address: str) -> str:
    raw = address.replace(":", "").replace("-", "").strip().upper()
    if len(raw) != 12:
        return ""
    try:
        int(raw, 16)
    except ValueError:
        return ""
    return raw


def _format_mac(raw: str) -> str:
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2))


def _address_type_from_ble_device(ble_device: Any) -> int:
    details = getattr(ble_device, "details", None) or {}
    if not isinstance(details, dict):
        return 0
    try:
        return int(details.get("address_type", 0))
    except (TypeError, ValueError):
        return 0


def _redact_command_kwargs(command_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return command kwargs suitable for debug logs."""
    return {
        str(key): value
        for key, value in sorted(command_kwargs.items(), key=lambda item: str(item[0]))
        if not str(key).lower().endswith(("password", "token", "key"))
    }


def _core_bledata_hex_from_1911_hint(hint: dict[str, Any]) -> str | None:
    """Normalize a BLE 1911 notification into the core bleData hex shape."""
    payload_hex = str(hint.get("payload_hex") or "")
    if not payload_hex:
        return None
    prefix = str(hint.get("prefix") or "")
    if prefix in ("dc1102", "db1102"):
        return "641b1000000000" + payload_hex
    if prefix == "d36969":
        header = hint.get("header")
        header_device_hex = ""
        if isinstance(header, dict):
            header_device_hex = str(header.get("device_id_hex") or "")
        if len(header_device_hex) != 4:
            return None
        return "010203" + header_device_hex + "0000" + payload_hex
    return None


def _identity_from_db1102_hint(
    hint: dict[str, Any],
    *,
    membership: str | None,
) -> PixieBleAdvertisementIdentity | None:
    """Decode db1102 identity replies into the same identity shape as long adverts."""
    if str(hint.get("prefix") or "") != "db1102":
        return None
    payload_hex = str(hint.get("payload_hex") or "")
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError:
        return None
    if len(payload) < 13 or payload[:3] != b"\xdb\x11\x02":
        return None
    header = hint.get("header")
    device_id = header.get("device_id") if isinstance(header, dict) else None
    try:
        device_id_int = int(device_id)
    except (TypeError, ValueError):
        return None
    if device_id_int <= 0 or device_id_int > 255:
        return None

    body = payload[3:]
    if len(body) < 8:
        return None
    product_type = body[1]
    product_stype = body[2]
    packed_version = body[3]
    mac_tail_le = body[4:8]
    if len(mac_tail_le) != 4:
        return None
    mac = "00:21:" + ":".join(f"{byte:02X}" for byte in reversed(mac_tail_le))
    version = int(packed_version) >> 2
    return PixieBleAdvertisementIdentity(
        mac=mac,
        version=version,
        model_no=f"{product_type:02d}{product_stype:02d}",
        device_id=device_id_int,
        product_type=product_type,
        product_stype=product_stype,
        membership=str(membership or "").strip().lower() or None,
        byte10=body[8] if len(body) > 8 else None,
    )


def _decode_ble_payload(raw: bytes, payload: bytes) -> dict[str, Any]:
    """Return a lightweight diagnostic decode for one decrypted 1911 packet."""
    result: dict[str, Any] = {
        "raw_len": len(raw),
        "payload_hex": payload.hex(),
        "payload_len": len(payload),
    }
    if len(raw) >= 7:
        header_device_id = int.from_bytes(raw[3:5], byteorder="little")
        result["header"] = {
            "nonce": raw[:3].hex(),
            "device_id": header_device_id,
            "device_id_hex": raw[3:5].hex(),
            "auth": raw[5:7].hex(),
        }
    if len(payload) < 3:
        result["kind"] = "short"
        return result
    prefix = payload[:3].hex()
    result["prefix"] = prefix
    if prefix == "d36969":
        return _decode_d36969_payload(result, payload)
    if prefix == "db1102":
        identity = _identity_from_db1102_hint(result, membership=None)
        if identity is not None:
            result["kind"] = "identity_response"
            result["identity"] = {
                "mac": identity.mac,
                "version": identity.version,
                "model_no": identity.model_no,
                "device_id": identity.device_id,
                "product_type": identity.product_type,
                "product_stype": identity.product_stype,
                "byte10": identity.byte10,
            }
        else:
            result["kind"] = "ack_like"
        return result
    if prefix != "dc1102":
        result["kind"] = "unknown"
        return result
    records: list[dict[str, int]] = []
    mesh_status_records: list[dict[str, int | str]] = []
    for offset in (3, 7):
        if len(payload) < offset + 4:
            continue
        chunk = payload[offset : offset + 4]
        if chunk in (b"\x00\x00\x00\x00",) or chunk[0] == 0x00:
            continue
        if chunk[0] == 0xFF:
            mesh_status_records.append({
                "offset": offset,
                "pseudo_id": "0xff",
                "online_value": chunk[1],
                "value_byte": chunk[2],
                "tail": chunk[3],
            })
            continue
        records.append({
            "offset": offset,
            "device_id": chunk[0],
            "online_value": chunk[1],
            "value_byte": chunk[2],
            "tail": chunk[3],
        })
    if mesh_status_records:
        result["mesh_status_records"] = mesh_status_records
    result["kind"] = "record_container" if records else "mesh_status"
    result["records"] = records
    return result


def _decode_d36969_payload(result: dict[str, Any], payload: bytes) -> dict[str, Any]:
    """Decode d36969 response-style payloads.

    Unlike dc1102 mesh record containers, d36969 bodies do not carry the device
    id. For 1911 notifications the id is in the raw packet header bytes 3:5.
    """
    header = result.get("header")
    header_device_id = header.get("device_id") if isinstance(header, dict) else None
    result["opcode"] = "d36969"
    result["device_id"] = header_device_id
    if len(payload) < 5:
        result["kind"] = "d36969_short"
        return result
    flag = payload[3]
    result["d3_flag"] = flag
    result["subtype"] = payload[4]
    if flag != 0xB9:
        result["kind"] = "d36969_ack"
        return result
    result["kind"] = "d36969_b9"
    if len(payload) >= 13 and payload[4] == 0x10:
        result["word1_le"] = int.from_bytes(payload[5:9], byteorder="little")
        result["word2_le"] = int.from_bytes(payload[9:13], byteorder="little")
    return result
