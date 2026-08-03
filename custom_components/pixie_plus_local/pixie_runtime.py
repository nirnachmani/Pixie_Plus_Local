#!/usr/bin/env python3
"""
Pixie Plus Autonomous Authentication Handler (Auto-Discover + Key Extraction)

Fully autonomous flow:
1. Broadcast UDP discovery to find available hubs on LAN
2. Capture handshake via MITM proxy to extract session key
3. Store credentials + key for subsequent commands without manual hub IP

Based on Android app analysis:
- q0.b:UDP broadcast discovers gateways on port 41580
- After discovery, TCP connects to port 41578 for control
"""

import json
import logging
import os
import socket
import threading
import time
import base64
import queue
import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple, Callable, Iterable

# Network constants
UDP_DISCOVERY_PORT = 41580
TCP_CONTROL_PORT = 41578
BROADCAST_ADDRESS = "255.255.255.255"
RUNTIME_IDLE_TIMEOUT_SECONDS = 45.0
RUNTIME_MAX_CONSECUTIVE_HEARTBEAT_FAILURES = 3
RUNTIME_COMMAND_BASE_TIMEOUT_SECONDS = 10.0
RUNTIME_COMMAND_PER_AHEAD_SECONDS = 2.0
RUNTIME_COMMAND_MAX_TIMEOUT_SECONDS = 60.0
RUNTIME_COMMAND_MIN_GAP_SECONDS = 0.1
LOCAL_TIMER_RESTART_GUARD_SECONDS = 4.0
TIMER_STATUS_CORRECTION_DEADBAND_SECONDS = 1.0
LOCAL_AMBIGUOUS_BLUE_CONFIRM_SECONDS = 8.0
UNKNOWN_DEVICE_UPDATE_REPLAY_SECONDS = 15.0
from datetime import datetime, timezone
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from .pixie_protocol import PixieEnvelope, PixieMessage, PixieCrypto
from .pixie_inventory import (
    GatewayIdentity,
    PixieInventory,
    supports_outlet_runtime_config,
    supports_plug_led_settings,
    supports_sensor_advanced_settings,
)
from .pixie_value_profiles import (
    EFFECT_COMMAND_ENCODINGS,
    decode_color_runtime_state_for_capabilities,
    decode_color_temp_runtime_state_for_capabilities,
    decode_contact_runtime_state,
    build_gate_motion_plan,
    build_gate_motion_plan_from_learned_duration,
    decode_gate_command_reply,
    decode_gate_state_byte,
    decode_value_byte_for_capabilities,
    is_stale_gate_motion_progress_update,
    sync_gate_motion_plan,
    gate_can_run_action,
    get_supported_sensor_mode_values_for_capabilities,
    INDICATOR_LED_OFF_VALUES,
    INDICATOR_LED_ON_VALUES,
    resolve_cover_command_position,
)

LOGGER = logging.getLogger(__name__)

GATE_OPEN_DURATION_FIELD_OFFSET_MS = 2000
GATE_CLOSE_DURATION_FIELD_OFFSET_MS = 1000


def _decode_gate_open_duration_ms(field_ms: int) -> int:
    """Convert the gate open-duration wire field to app/HA display milliseconds."""
    return max(0, int(field_ms) + GATE_OPEN_DURATION_FIELD_OFFSET_MS)


def _decode_gate_close_duration_ms(field_ms: int) -> int:
    """Convert the gate close-duration wire field to app/HA display milliseconds."""
    return max(0, int(field_ms) - GATE_CLOSE_DURATION_FIELD_OFFSET_MS)


def _encode_gate_open_duration_field_ms(duration_ms: int) -> int:
    """Convert app/HA gate open-duration milliseconds to the wire field."""
    return max(0, int(duration_ms) - GATE_OPEN_DURATION_FIELD_OFFSET_MS)


def _encode_gate_close_duration_field_ms(duration_ms: int) -> int:
    """Convert app/HA gate close-duration milliseconds to the wire field."""
    return max(0, int(duration_ms) + GATE_CLOSE_DURATION_FIELD_OFFSET_MS)


# ============================================================================
# ENVELOPE ENCODING/DECODING (moved to pixie_protocol.py)
# ============================================================================

# Removed: encode_envelope, decode_envelope - now in pixie_protocol.PixieEnvelope


class PixieHub:
    """Represents a discovered Pixie hub."""

    def __init__(self, host: str, port: int = 41580):
        self.host = host
        self.port = port
        self.is_valid = False
        self.meshnet: Optional[str] = None
        self.meshnet2: Optional[str] = None
        self.from_value: Optional[str] = None

    def __repr__(self):
        return f"Hub({self.host}:{self.port})"


class PixieAuthError(Exception):
    """Base exception for Pixie authentication errors"""
    pass


class PixieInvalidCredentialsError(PixieAuthError):
    """Pixie cloud rejected the supplied username/password."""


class PixieGatewayResolutionError(PixieAuthError):
    """Gateway discovery could not resolve a single usable host."""


class PixieGatewayConnectionError(PixieAuthError):
    """Gateway host was selected but could not be reached successfully."""


@dataclass(frozen=True)
class CloudParams:
    """Persistable cloud-derived parameters required for local gateway access."""

    home_id: str
    home_name: str
    user_id: str
    meshnet: str
    meshnet2: str
    netid: str


@dataclass(frozen=True)
class CloudHomeList:
    """Cloud login result plus the homes visible to that account."""

    user_id: str
    session_token: str
    current_home_id: str | None
    homes: Tuple[Dict[str, Any], ...]


@dataclass(frozen=True)
class PixieOptimisticUpdateIntent:
    """Transport-neutral HA runtime prediction for one user command."""

    device_id: int
    target: str
    value: Any = None
    brightness_level: Optional[int] = None
    rgb_color: Optional[Tuple[int, int, int]] = None
    effect_name: Optional[str] = None
    effect_speed: Optional[int] = None
    cover_button_position: Optional[int] = None


@dataclass(frozen=True)
class PixieCoreCommandPacket:
    """One core Pixie command packet before TCP/BLE transport wrapping."""

    command_hex: str
    tcp_repeat: int = 0
    delay_after: float = 0.0
    log_message: Optional[str] = None
    log_args: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class PixieCoreCommandPlan:
    """Transport-neutral core command sequence for one HA command."""

    device_id: int
    target: str
    packets: Tuple[PixieCoreCommandPacket, ...]
    optimistic_intent: Optional[PixieOptimisticUpdateIntent] = None
    result: Dict[str, Any] = field(default_factory=dict)


def _command_reply_route_slice(command: bytes) -> Optional[Tuple[int, int, str]]:
    """Return the reply-route byte slice for commands that expect d36969 replies."""
    if (
        len(command) == 17
        and command[7:10] == b"\xf9\x6b\x69"
        and command[10:15] == b"\x05\x00\x00\x00\x00"
    ):
        return 15, 17, "timer_poll"
    if (
        len(command) == 13
        and command[7:10] == b"\xff\x6b\x69"
        and command[10] in (0x02, 0x03)
    ):
        return 11, 13, "power_meter_poll"
    return None


def is_command_reply_route_packet(command: bytes) -> bool:
    """Return True for commands that carry a reply route id."""
    return _command_reply_route_slice(command) is not None


def command_reply_route_kind(command: bytes) -> Optional[str]:
    """Return the routeable command kind, when known."""
    route = _command_reply_route_slice(command)
    return route[2] if route is not None else None


def patch_command_reply_route(
    command_hex: str,
    reply_node_id: Optional[int],
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Patch the reply route field when the packet shape matches.

    Returns (command_hex, old_route_hex, new_route_hex, kind). The route values
    are None when the packet is not a known routeable shape, or when no route id
    is available.
    """
    try:
        command = bytes.fromhex(command_hex)
    except ValueError:
        return command_hex, None, None, None
    route = _command_reply_route_slice(command)
    if route is None:
        return command_hex, None, None, None
    start, end, kind = route
    if reply_node_id is None:
        return command_hex, None, None, kind
    try:
        route_id = int(reply_node_id)
    except (TypeError, ValueError):
        return command_hex, None, None, kind
    if not (0 <= route_id <= 0xFFFF):
        return command_hex, None, None, kind
    old_route = command[start:end].hex()
    route_bytes = route_id.to_bytes(2, byteorder="little", signed=False)
    new_route = route_bytes.hex()
    if old_route == new_route:
        return command_hex, old_route, new_route, kind
    patched = command[:start] + route_bytes + command[end:]
    return patched.hex(), old_route, new_route, kind


@dataclass
class PixieRuntimeSession:
    """Owns the long-lived 41578 control thread and its readiness state."""

    handler: "PixieAuthHandler"
    host: str
    port: int
    keep_control_alive: bool
    stop_event: threading.Event = field(default_factory=threading.Event)
    ready_event: threading.Event = field(default_factory=threading.Event)
    ready_state: Dict[str, Any] = field(default_factory=dict)
    control_result: Dict[str, Any] = field(default_factory=lambda: {"result": None, "error": None})
    command_kwargs: Dict[str, Any] = field(default_factory=dict)
    command_queue: "queue.Queue[Dict[str, Any]]" = field(default_factory=queue.Queue)
    last_inbound_at: Optional[float] = None
    last_heartbeat_sent_at: Optional[float] = None
    last_heartbeat_reply_at: Optional[float] = None
    primed_at: Optional[float] = None
    connection_closed_at: Optional[float] = None
    consecutive_heartbeat_failures: int = 0
    thread: Optional[threading.Thread] = None
    command_state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    command_sequence: int = 0
    active_command_id: Optional[int] = None
    active_command_started_at: Optional[float] = None
    last_command_sent_at: Optional[float] = None
    health_update_callback: Optional[Callable[[], None]] = field(default=None, repr=False)

    def _update_health_state(self, **kwargs: Any) -> None:
        self.ready_state.update(kwargs)
        if self.health_update_callback is not None:
            try:
                self.health_update_callback()
            except Exception:
                LOGGER.debug("Pixie runtime health update callback failed", exc_info=True)

    def start(self) -> None:
        if self.thread is not None:
            return

        self._update_health_state(started_at=time.time(), stop_requested=False)

        self.thread = threading.Thread(
            target=self._run,
            name="pixie-41578-control-main",
            daemon=False,
        )
        self.thread.start()

    def _run(self) -> None:
        try:
            self.control_result["result"] = self.handler._perform_handshake_capture(
                self.host,
                self.port,
                runtime_session=self,
                control_ready_event=self.ready_event,
                control_ready_state=self.ready_state,
                stop_event=self.stop_event,
                keep_control_alive=self.keep_control_alive,
                command_request_queue=self.command_queue,
                **self.command_kwargs,
            )
        except Exception as exc:
            self.control_result["error"] = exc
            self._update_health_state(last_error=str(exc))

    def wait_until_primed(self, timeout: float) -> bool:
        return self.ready_event.wait(timeout=timeout)

    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def join(self, timeout: Optional[float] = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def stop(self) -> None:
        self.stop_event.set()
        self._update_health_state(stop_requested=True)

    def stop_and_join(self, timeout: float = 5.0) -> None:
        self.stop()
        self.join(timeout=timeout)

    def mark_inbound_traffic(self, when: Optional[float] = None) -> None:
        ts = time.time() if when is None else float(when)
        self.last_inbound_at = ts
        self._update_health_state(last_inbound_at=ts)

    def mark_primed(self, when: Optional[float] = None) -> None:
        if self.primed_at is not None:
            return
        ts = time.time() if when is None else float(when)
        self.primed_at = ts
        self._update_health_state(primed_at=ts)

    def mark_heartbeat_sent(self, when: Optional[float] = None) -> None:
        ts = time.time() if when is None else float(when)
        self.last_heartbeat_sent_at = ts
        self._update_health_state(last_heartbeat_sent_at=ts)

    def mark_heartbeat_reply(self, when: Optional[float] = None) -> None:
        ts = time.time() if when is None else float(when)
        self.last_heartbeat_reply_at = ts
        self.consecutive_heartbeat_failures = 0
        self._update_health_state(
            last_heartbeat_reply_at=ts,
            consecutive_heartbeat_failures=0,
        )

    def mark_heartbeat_failure(self) -> None:
        self.consecutive_heartbeat_failures += 1
        self._update_health_state(
            consecutive_heartbeat_failures=self.consecutive_heartbeat_failures,
        )

    def mark_connection_closed(self, when: Optional[float] = None) -> None:
        if self.connection_closed_at is not None:
            return
        ts = time.time() if when is None else float(when)
        self.connection_closed_at = ts
        self._update_health_state(connection_closed=True, connection_closed_at=ts)

    def health_summary(self) -> Dict[str, Any]:
        return {
            "alive": self.is_alive(),
            "primed": self.primed_at is not None,
            "connection_closed": self.connection_closed_at is not None,
            "consecutive_heartbeat_failures": self.consecutive_heartbeat_failures,
            "last_inbound_at": self.last_inbound_at,
            "last_heartbeat_sent_at": self.last_heartbeat_sent_at,
            "last_heartbeat_reply_at": self.last_heartbeat_reply_at,
            "error": str(self.error) if self.error is not None else None,
        }

    def needs_restart(
        self,
        *,
        now: Optional[float] = None,
        idle_timeout: float = RUNTIME_IDLE_TIMEOUT_SECONDS,
        max_heartbeat_failures: int = RUNTIME_MAX_CONSECUTIVE_HEARTBEAT_FAILURES,
    ) -> bool:
        if self.stop_event.is_set():
            return False

        if self.error is not None or self.connection_closed_at is not None:
            return True

        if self.thread is not None and not self.thread.is_alive():
            return True

        if self.primed_at is None:
            return False

        if self.consecutive_heartbeat_failures < max_heartbeat_failures:
            return False

        return True

    def reserve_command_slot(self) -> tuple[int, int]:
        """Reserve a command slot and report how many commands are already ahead."""
        with self.command_state_lock:
            self.command_sequence += 1
            queued = self.command_queue.qsize()
            in_flight = 1 if self.active_command_id is not None else 0
            return self.command_sequence, queued + in_flight

    def mark_command_started(self, command_id: int) -> None:
        """Mark a queued command as actively executing on the live session."""
        with self.command_state_lock:
            self.active_command_id = command_id
            self.active_command_started_at = time.time()

    def mark_command_finished(self, command_id: int) -> None:
        """Clear the active command marker once execution completes."""
        with self.command_state_lock:
            if self.active_command_id == command_id:
                self.active_command_id = None
                self.active_command_started_at = None

    def command_backlog_snapshot(self) -> Dict[str, Any]:
        """Return current queue depth and active-command state for logging."""
        with self.command_state_lock:
            return {
                "queued": self.command_queue.qsize(),
                "active_command_id": self.active_command_id,
                "active_for": (
                    None
                    if self.active_command_started_at is None
                    else max(0.0, time.time() - self.active_command_started_at)
                ),
            }

    def throttle_before_command_send(self, min_gap: float = RUNTIME_COMMAND_MIN_GAP_SECONDS) -> None:
        """Enforce a minimum gap between queued command sends."""
        with self.command_state_lock:
            last_sent_at = self.last_command_sent_at

        if last_sent_at is None:
            return

        remaining = min_gap - (time.time() - last_sent_at)
        if remaining > 0:
            time.sleep(remaining)

    def mark_command_sent(self, when: Optional[float] = None) -> None:
        """Record when a queued command was written to the TCP socket."""
        ts = time.time() if when is None else float(when)
        with self.command_state_lock:
            self.last_command_sent_at = ts

    def send_command(self, command_kwargs: Dict[str, Any], timeout: float = RUNTIME_COMMAND_BASE_TIMEOUT_SECONDS) -> Dict[str, Any]:
        """Send a local command via the existing control session."""
        if not self.is_alive():
            raise RuntimeError("Pixie control session is not running")
        if self.needs_restart():
            raise RuntimeError(f"Pixie control session is unhealthy: {self.health_summary()}")
        if not self.wait_until_primed(timeout=min(timeout, 5.0)):
            raise TimeoutError(f"Pixie control session is not primed (state={self.ready_state})")

        command_id, commands_ahead = self.reserve_command_slot()
        effective_timeout = min(
            RUNTIME_COMMAND_MAX_TIMEOUT_SECONDS,
            max(timeout, RUNTIME_COMMAND_BASE_TIMEOUT_SECONDS) + (commands_ahead * RUNTIME_COMMAND_PER_AHEAD_SECONDS),
        )
        response_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue(maxsize=1)
        self.command_queue.put({
            "command_id": command_id,
            "kwargs": dict(command_kwargs),
            "response_queue": response_queue,
        })
        try:
            status, payload = response_queue.get(timeout=effective_timeout)
        except queue.Empty as exc:
            backlog = self.command_backlog_snapshot()
            raise TimeoutError(
                "Timed out waiting for live Pixie command completion "
                f"(command_id={command_id}, ahead={commands_ahead}, timeout={effective_timeout:.1f}s, "
                f"queued_now={backlog['queued']}, active_command_id={backlog['active_command_id']}, "
                f"active_for={backlog['active_for']})"
            ) from exc

        if status == "error":
            if isinstance(payload, Exception):
                raise payload
            raise RuntimeError(str(payload))

        return payload if isinstance(payload, dict) else {"status": payload}

    @property
    def error(self) -> Optional[Exception]:
        return self.control_result.get("error")

    @property
    def result(self) -> Optional[Dict[str, Any]]:
        return self.control_result.get("result")


@dataclass
class PixieRuntimeData:
    """Live runtime objects intended to back the future HA integration layer."""

    handler: "PixieAuthHandler"
    runtime_session: Optional[PixieRuntimeSession]
    inventory: Optional[PixieInventory]
    inventory_mode: str


# ============================================================================
# API CONFIGURATION
# ============================================================================

def listen_for_responses(sock: socket.socket, timeout: int = 10) -> Tuple[List[PixieHub], Optional[Dict[str, Any]]]:
    """
    Listen for UDP responses and parse envelope format (matches Java q0/b.java).

    Args:
        sock: Open UDP socket
        timeout: Maximum time to listen

    Returns:
        Tuple of (list of discovered hubs, decoded envelope dict from first valid response)
    """
    hubs_found = []
    first_decoded = None
    seen_ips = set()
    printed_identifier = False

    try:
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            sock.settimeout(1.0)
            try:
                data, addr = sock.recvfrom(65536)
                src_ip, src_port = addr

                # Try to parse as JSON response (matches Java q0/b.java F method)
                try:
                    response = json.loads(data.decode('utf-8', errors='ignore'))

                    gateway_type = response.get('type', '')
                    gateway_meshnet = response.get('meshNet', '')
                    gateway_meshnet2 = response.get('meshNet2', '')
                    gateway_from = response.get('from', '')

                    if not printed_identifier:
                        LOGGER.debug(
                            "Hub broadcast: type=%s meshNet=%s meshNet2=%s from=%s",
                            gateway_type,
                            gateway_meshnet,
                            gateway_meshnet2,
                            gateway_from,
                        )
                        printed_identifier = True

                    if first_decoded is None:
                        first_decoded = response

                    # Check for gateway response (matches Java validation)
                    gateway_type = response.get('type', '')
                    gateway_meshnet = response.get('meshNet', '')
                    gateway_meshnet2 = response.get('meshNet2', '')

                    if gateway_type == 'GW' and (gateway_meshnet or gateway_meshnet2):
                        if src_ip in seen_ips:
                            LOGGER.debug("Duplicate gateway advert from %s skipped", src_ip)
                            continue
                        hub = PixieHub(src_ip, UDP_DISCOVERY_PORT)
                        hub.is_valid = True
                        hub.meshnet = str(gateway_meshnet or "") or None
                        hub.meshnet2 = str(gateway_meshnet2 or "") or None
                        hub.from_value = str(gateway_from or "") or None
                        hubs_found.append(hub)
                        seen_ips.add(src_ip)
                        LOGGER.debug("Valid gateway discovered at %s:%s", src_ip, src_port)

                except json.JSONDecodeError:
                    # Fallback: try to decode as raw envelope
                    decoded = PixieEnvelope.decode(data, None)  # No key for broadcast
                    if decoded:
                        gateway_type = decoded.get('type', '')
                        gateway_meshnet = decoded.get('meshNet', '')
                        gateway_meshnet2 = decoded.get('meshNet2', '')
                        gateway_from = decoded.get('from', '')

                        if not printed_identifier:
                            LOGGER.debug(
                                "Hub broadcast: type=%s meshNet=%s meshNet2=%s from=%s",
                                gateway_type,
                                gateway_meshnet,
                                gateway_meshnet2,
                                gateway_from,
                            )
                            printed_identifier = True

                        if first_decoded is None:
                            first_decoded = decoded

                        if gateway_type == 'GW' and (gateway_meshnet or gateway_meshnet2):
                            if src_ip in seen_ips:
                                LOGGER.debug("Duplicate gateway advert from %s skipped", src_ip)
                                continue
                            hub = PixieHub(src_ip, UDP_DISCOVERY_PORT)
                            hub.is_valid = True
                            hub.meshnet = str(gateway_meshnet or "") or None
                            hub.meshnet2 = str(gateway_meshnet2 or "") or None
                            hub.from_value = str(gateway_from or "") or None
                            hubs_found.append(hub)
                            seen_ips.add(src_ip)
                            LOGGER.debug("Valid gateway discovered at %s:%s", src_ip, src_port)
                    else:
                        LOGGER.debug("Could not decode UDP response from %s:%s (len=%s)", src_ip, src_port, len(data))

            except socket.timeout:
                continue
            except Exception as e:
                LOGGER.debug("Error processing UDP response: %s", e)

    except Exception as e:
        LOGGER.warning("Gateway discovery receive loop failed: %s", e)
    finally:
        sock.close()

    return hubs_found, first_decoded


# ============================================================================
# API CONFIGURATION
# ============================================================================

# Real Pixie Plus Cloud API endpoints (from pixiepluslogin.py)
API_URL = {
    "login": "https://www.pixie.app/p0/pixieCloud/login",
    "user_query": "https://www.pixie.app/p0/pixieCloud/functions/userQuery",
    "home": "https://www.pixie.app/p0/pixieCloud/classes/Home",
    "livegroup": "https://www.pixie.app/p0/pixieCloud/classes/LiveGroup",
}

# Pixie Plus Constants (from Android app)
APPLICATION_ID = "6426f04c206c108275ede71b9fd09ac8"
CLIENT_KEY = "35779bd411c751ff87577cd762118dad"

# Network defaults (from Android code)
UDP_DISCOVERY_PORT = 41580
TCP_CONTROL_PORT = 41578
TCP_SYNC_PORT = 53216


class PixieAuthHandler:
    """
    Handles complete autonomous authentication flow for Pixie Plus.

    Fully automatic process:
    1. Scan LAN via UDP broadcast to discover hubs (no IP required from user)
    2. Set up MITM proxy on discovered hub port
    3. Capture handshake traffic during TCP connection
    4. Extract netID, meshNet from login response
    5. Extract session key using netID as decryption seed
    6. Store credentials + key for all future commands
    """

    def __init__(self, credentials_path: Optional[str] = None, verbose: bool = False):
        self.verbose = verbose
        self.suppress_heartbeat_logs = False
        self.netid_seed = None
        self.dump_structures = False
        self.dump_dir = "debug_dumps"
        self.meshnet = None
        self.meshnet2 = None
        self.home_id: Optional[str] = None
        self.home_name: Optional[str] = None
        self.user_id: Optional[str] = None
        self.session_token: Optional[str] = None
        self.session_key_hex = None
        self.current_hub: Optional[Dict[str, Any]] = None
        self.inventory: Optional[PixieInventory] = None
        self.gateway_identity: Optional[GatewayIdentity] = None
        self.runtime_session: Optional[PixieRuntimeSession] = None
        self.stored_username: Optional[str] = None
        self.stored_password: Optional[str] = None
        self.inventory_mode: str = "local_53216"
        self._command_counter = 0x10  # App-style brightness/cover commands observed starting at 0x10.
        self._mode_command_counter = 0x01  # Captured 3001 mode commands observed starting at 0x01.
        self._timer_command_counter = 0x01  # Captured timer switch commands observed starting at 0x01.
        self._cached_cloud_home_obj: Optional[Dict[str, Any]] = None
        self._pending_bulk_ble_updates: List[Dict[str, Any]] = []
        self._pending_bulk_lock = threading.Lock()
        self._pending_unknown_ble_updates: Dict[int, Dict[str, Any]] = {}
        self._unknown_ble_updates_hold_until = 0.0
        self._inventory_update_callback: Optional[Callable[[PixieInventory], None]] = None
        self._config_update_callback: Optional[Callable[[List[int]], None]] = None
        self._unknown_device_update_callback: Optional[Callable[[int], None]] = None
        self._awaiting_initial_gwdata_bulk = False

    def _debug_enabled(self) -> bool:
        return self.verbose or LOGGER.isEnabledFor(logging.DEBUG)

    def _log_message(self, message: str) -> str:
        home_name = str(self.home_name or "").strip()
        if home_name and home_name not in ("unknown", "None"):
            return f"[{home_name}] {message}"
        return message

    def _log_debug(self, message: str, *args: Any) -> None:
        LOGGER.debug(self._log_message(message), *args)

    def _gateway_reply_route_node_id(self) -> Optional[int]:
        """Return the current home gateway's Pixie device id for reply routing."""
        inventory = self.inventory
        if inventory is None:
            return None
        gateway = inventory.gateway
        gateway_mac = PixieInventory._normalize_mac(getattr(gateway, "gateway_mac", None)) if gateway is not None else ""
        if gateway_mac:
            for dev_id, rec in inventory.devices_by_id.items():
                if PixieInventory._normalize_mac(getattr(rec, "mac", None)) == gateway_mac:
                    return int(dev_id)
        for dev_id, rec in inventory.devices_by_id.items():
            if getattr(getattr(rec, "capabilities", None), "is_gateway", False):
                return int(dev_id)
        return None

    def _log_info(self, message: str, *args: Any) -> None:
        LOGGER.info(self._log_message(message), *args)

    def _log_warning(self, message: str, *args: Any) -> None:
        LOGGER.warning(self._log_message(message), *args)

    def _log_error(self, message: str, *args: Any) -> None:
        LOGGER.error(self._log_message(message), *args)

    def _log_exception(self, message: str, *args: Any) -> None:
        LOGGER.exception(self._log_message(message), *args)

    def _log_multiline_debug(self, header: str, lines: List[str]) -> None:
        if not self._debug_enabled():
            return
        if lines:
            LOGGER.debug("%s\n%s", self._log_message(header), "\n".join(lines))
        else:
            LOGGER.debug("%s", self._log_message(header))

    def set_inventory_update_callback(
        self,
        callback: Optional[Callable[[PixieInventory], None]],
    ) -> None:
        """Register a callback invoked after runtime inventory changes."""
        self._inventory_update_callback = callback

    def set_config_update_callback(
        self,
        callback: Optional[Callable[[List[int]], None]],
    ) -> None:
        """Register a callback invoked after gateway configuration changes."""
        self._config_update_callback = callback

    def set_unknown_device_update_callback(
        self,
        callback: Optional[Callable[[int], None]],
    ) -> None:
        """Register a callback invoked when runtime traffic references an unknown device."""
        self._unknown_device_update_callback = callback

    def _notify_inventory_updated(self) -> None:
        """Notify the integration layer that runtime inventory changed."""
        if self.inventory is None or self._inventory_update_callback is None:
            return
        try:
            self._inventory_update_callback(self.inventory)
        except Exception as exc:
            self._log_debug("Inventory update callback failed: %s", exc)

    def _notify_config_updated(self, conf_index: List[int]) -> None:
        """Notify the integration layer that gateway configuration changed."""
        if self._config_update_callback is None:
            return
        try:
            self._config_update_callback(conf_index)
        except Exception as exc:
            self._log_debug("Config update callback failed: %s", exc)

    def _notify_unknown_device_update(self, device_id: int) -> None:
        """Notify the integration layer that an unknown device appeared in runtime traffic."""
        if self._unknown_device_update_callback is None:
            return
        try:
            self._unknown_device_update_callback(int(device_id))
        except Exception as exc:
            self._log_debug("Unknown-device update callback failed: %s", exc)

    def begin_unknown_device_update_hold(self, *, seconds: float = UNKNOWN_DEVICE_UPDATE_REPLAY_SECONDS, reason: str = "") -> None:
        """Temporarily keep unknown single-device updates during inventory refresh."""
        until = time.time() + max(0.0, float(seconds))
        self._unknown_ble_updates_hold_until = max(self._unknown_ble_updates_hold_until, until)
        self._log_debug(
            "Replaying recent unknown device updates for %.1fs%s",
            max(0.0, self._unknown_ble_updates_hold_until - time.time()),
            f" ({reason})" if reason else "",
        )

    def _buffer_unknown_device_update(
        self,
        dev_id: int,
        *,
        ble_hex: str,
        payload_meta: Dict[str, Any],
        source: str,
    ) -> bool:
        """Remember latest unknown-device update for possible confUpdate replay."""
        now = time.time()
        stale_before = now - UNKNOWN_DEVICE_UPDATE_REPLAY_SECONDS
        for stale_id, pending in list(self._pending_unknown_ble_updates.items()):
            if float(pending.get("stored_at") or 0.0) < stale_before:
                self._pending_unknown_ble_updates.pop(stale_id, None)
        self._pending_unknown_ble_updates[int(dev_id)] = {
            "ble_hex": ble_hex,
            "payload_meta": dict(payload_meta or {}),
            "source": source,
            "stored_at": now,
        }
        if now <= self._unknown_ble_updates_hold_until:
            self._log_debug(
                "Buffered unknown dev_id=%s update pending inventory refresh source=%s",
                dev_id,
                source,
            )
            return True
        return False

    def replay_unknown_device_updates(self, device_ids: Optional[Iterable[int]] = None) -> int:
        """Replay buffered unknown-device updates after inventory refresh adds those IDs."""
        if not self.inventory or not self._pending_unknown_ble_updates:
            return 0
        wanted = set(int(device_id) for device_id in device_ids) if device_ids is not None else set(self.inventory.devices_by_id)
        applied = 0
        stale_before = time.time() - UNKNOWN_DEVICE_UPDATE_REPLAY_SECONDS
        replay_ids = [
            dev_id
            for dev_id in sorted(self._pending_unknown_ble_updates)
            if dev_id in wanted and dev_id in self.inventory.devices_by_id
            and float(self._pending_unknown_ble_updates[dev_id].get("stored_at") or 0.0) >= stale_before
        ]
        for dev_id in replay_ids:
            pending = self._pending_unknown_ble_updates.pop(dev_id, None)
            if not pending:
                continue
            applied += self.apply_bledata_hex(
                str(pending.get("ble_hex") or ""),
                payload_meta=dict(pending.get("payload_meta") or {}),
                source=str(pending.get("source") or "hub_update"),
                notify_inventory=False,
            )
        now = time.time()
        if now > self._unknown_ble_updates_hold_until:
            self._pending_unknown_ble_updates.clear()
            self._unknown_ble_updates_hold_until = 0.0
        if applied > 0:
            self._notify_inventory_updated()
            self._log_debug("Replayed %s buffered unknown device update(s) after inventory refresh", applied)
        return applied

    def _dump_structure_json(self, filename: str, payload: Any) -> None:
        """Write optional debug structure JSON files for offline shape comparison."""
        if not self.dump_structures:
            return
        try:
            os.makedirs(self.dump_dir, exist_ok=True)
            out_path = os.path.join(self.dump_dir, filename)
            with open(out_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            self._log_debug("Dumped structure JSON: %s", out_path)
        except Exception as e:
            self._log_warning("Could not dump structure file %s: %s", filename, e)

    def _queue_bulk_ble_records(self, records: List[Dict[str, Any]], source: str, *, full_snapshot: bool) -> None:
        """Queue bulk bleData records for later application once inventory exists."""
        if not records:
            return
        with self._pending_bulk_lock:
            self._pending_bulk_ble_updates.append({
                "source": source,
                "records": records,
                "full_snapshot": full_snapshot,
            })

    def _drain_bulk_ble_records(self) -> List[Dict[str, Any]]:
        """Drain queued bulk bleData record batches."""
        with self._pending_bulk_lock:
            batches = list(self._pending_bulk_ble_updates)
            self._pending_bulk_ble_updates.clear()
        return batches

    @staticmethod
    def _decode_bulk_br(value: int) -> Dict[str, Any]:
        """Decode GwData bulk br field into single or dual-channel state."""
        if value >= 12 and (value & ~0x03) != 0 and value <= 127:
            upper = value & ~0x03
            if upper in (12, 16):
                return {
                    "type": "multi",
                    "raw": value,
                    "ch1": bool(value & 0x01),
                    "ch2": bool(value & 0x02),
                }
        return {
            "type": "single",
            "raw": value,
            "pct": value,
        }

    def _apply_bulk_ble_records_to_inventory(
        self,
        records: List[Dict[str, Any]],
        source: str,
        *,
        full_snapshot: bool,
        notify_inventory: bool = True,
    ) -> int:
        """Apply bulk bleData records to inventory runtime using minimal state fields.

        Only presence, brightness (scalar models), bitmask state via runtime.r,
        derived is_on, and runtime.last_source are updated.
        """
        if not self.inventory or not records:
            return 0
        applied = self.inventory.apply_gwdata_bulk(records, source, full_snapshot=full_snapshot)
        if applied > 0 and notify_inventory:
            self._notify_inventory_updated()
        return applied

    def decode_bledata_hex(self, hex_payload: str) -> Optional[Dict[str, Any]]:
        """Decode a Pixie core bleData hex payload into normalized records."""
        if not isinstance(hex_payload, str):
            return None

        clean = hex_payload.strip().lower()
        if not clean or len(clean) % 2 != 0:
            return None

        try:
            raw = bytes.fromhex(clean)
        except Exception:
            return None

        decoded: Dict[str, Any] = {
            "hex": clean,
            "length": len(raw),
            "bytes": [int(b) for b in raw],
        }

        if len(raw) >= 18 and (len(raw) - 6) % 4 == 0:
            records: List[Dict[str, Any]] = []
            for i in range(6, len(raw) - 3, 4):
                dev_id = int(raw[i])
                online = int(raw[i + 1])
                br_raw = int(raw[i + 2])
                rssi_raw = int(raw[i + 3])
                if dev_id in (0, 255):
                    continue
                records.append({
                    "id": dev_id,
                    "online": online,
                    "br_raw": br_raw,
                    "br": self._decode_bulk_br(br_raw),
                    "rssi_raw": rssi_raw,
                })

            if len(records) >= 2:
                decoded["kind"] = "bulk"
                decoded["records"] = records
                return decoded

        mode_command = self._decode_sensor_mode_command(raw)
        if mode_command is not None:
            decoded["kind"] = "single"
            decoded["opcode"] = "c16969"
            decoded["device_id"] = mode_command["device_id"]
            decoded["mode"] = mode_command["mode"]
            decoded["relay"] = mode_command["relay"]
            decoded["records"] = [{
                "id": decoded["device_id"],
                "online": None,
                "br_raw": None,
                "br": {"type": "single", "raw": None, "pct": 100 if decoded["relay"] else 0},
                "mode": decoded["mode"],
                "relay": decoded["relay"],
                "rgb": None,
                "value_byte": None,
                "tail_flag": None,
                "is_on_from_tail": None,
                "online_value": None,
            }]
            return decoded

        if len(raw) >= 14 and raw[7] == 0xC1 and raw[8] == 0x69 and raw[9] == 0x69:
            decoded["kind"] = "single"
            decoded["opcode"] = "c16969"
            decoded["device_id"] = int(raw[5])
            decoded["rgb"] = [int(raw[10]), int(raw[11]), int(raw[12])]
            brightness_raw = int(raw[13])
            decoded["brightness_raw"] = brightness_raw
            decoded["brightness_0_100"] = max(0, min(100, round((brightness_raw * 100) / 256)))
            decoded["records"] = [{
                "id": decoded["device_id"],
                "online": None,
                "br_raw": brightness_raw,
                "br": {"type": "single", "raw": brightness_raw, "pct": decoded["brightness_0_100"]},
                "rgb": list(decoded["rgb"]),
                "value_byte": None,
                "tail_flag": None,
                "is_on_from_tail": None,
                "online_value": None,
            }]
            return decoded

        if len(raw) >= 13 and raw[7:10] in (b"\xfb\x6b\x69", b"\xfc\x6b\x69"):
            dev_id = int.from_bytes(raw[5:7], byteorder="little")
            rec = self.inventory.devices_by_id.get(dev_id) if self.inventory else None
            if rec and rec.capabilities.supports_gate:
                opcode = raw[7:10]
                record: Dict[str, Any] = {
                    "id": dev_id,
                    "online": None,
                    "br_raw": None,
                    "br": {"type": "single", "raw": None, "pct": None},
                    "value_byte": None,
                    "tail_flag": None,
                    "is_on_from_tail": None,
                    "online_value": None,
                    "settings_kind": int(opcode[0]),
                }
                if opcode == b"\xfb\x6b\x69" and raw[10] == 0x01 and len(raw) >= 13:
                    width_ds = int(raw[11])
                    decoded["kind"] = "gate_settings"
                    decoded["device_id"] = dev_id
                    record["gate_signal_width_seconds"] = max(1, min(5, round(width_ds / 10)))
                    decoded["records"] = [record]
                    return decoded
                if opcode == b"\xfc\x6b\x69" and raw[10] == 0x01 and len(raw) >= 20:
                    door_index = int(raw[11])
                    open_field_ms = int.from_bytes(raw[12:14], byteorder="little")
                    close_field_ms = int.from_bytes(raw[14:16], byteorder="little")
                    decoded["kind"] = "gate_settings"
                    decoded["device_id"] = dev_id
                    record.update({
                        "gate_door": door_index,
                        "gate_open_duration_ms": _decode_gate_open_duration_ms(open_field_ms),
                        "gate_close_duration_ms": _decode_gate_close_duration_ms(close_field_ms),
                        "gate_extra1_duration_ms": int.from_bytes(raw[16:18], byteorder="little"),
                        "gate_extra2_duration_ms": int.from_bytes(raw[18:20], byteorder="little"),
                    })
                    decoded["records"] = [record]
                    return decoded

        d3_pos = raw.find(b"\xd3\x69\x69")
        if d3_pos >= 0 and len(raw) >= d3_pos + 10:
            flag_byte = raw[d3_pos + 3]
            dev_id = int.from_bytes(raw[3:5], byteorder="little")
            data_start = d3_pos + 4
            rec = self.inventory.devices_by_id.get(dev_id) if self.inventory else None
            if rec and rec.capabilities.supports_power_metering and flag_byte == 0xbf and len(raw) >= data_start + 9:
                subtype = int(raw[data_start])
                payload = raw[data_start + 1 : data_start + 9]
                decoded["kind"] = "power_meter_status"
                decoded["device_id"] = dev_id
                decoded["meter_subtype"] = subtype
                record: Dict[str, Any] = {
                    "id": dev_id,
                    "online": None,
                    "br_raw": None,
                    "br": {"type": "single", "raw": None, "pct": None},
                    "meter_subtype": subtype,
                    "value_byte": None,
                    "tail_flag": None,
                    "is_on_from_tail": None,
                    "online_value": None,
                }
                if subtype == 0x02:
                    left_current_ma = int.from_bytes(payload[0:2], byteorder="little")
                    right_current_ma = int.from_bytes(payload[2:4], byteorder="little")
                    voltage_cv = int.from_bytes(payload[4:8], byteorder="little")
                    voltage_v = voltage_cv / 100.0
                    record.update({
                        "left_current_a": left_current_ma / 1000.0,
                        "right_current_a": right_current_ma / 1000.0,
                        "voltage_v": voltage_v,
                        "left_power_w": (left_current_ma * voltage_cv) / 100000.0,
                        "right_power_w": (right_current_ma * voltage_cv) / 100000.0,
                    })
                elif subtype == 0x03:
                    record.update({
                        "left_energy_kwh": int.from_bytes(payload[0:4], byteorder="little") / 1000.0,
                        "right_energy_kwh": int.from_bytes(payload[4:8], byteorder="little") / 1000.0,
                    })
                else:
                    decoded["kind"] = "d36969"
                    decoded["flag"] = flag_byte
                    decoded["data_hex"] = raw[data_start:].hex()
                    return decoded
                decoded["records"] = [record]
                return decoded
            if rec and rec.capabilities.supports_switch_indicator_led and flag_byte == 0x99 and len(raw) >= data_start + 3:
                block = int(raw[data_start])
                if block == 0x10:
                    decoded["kind"] = "indicator_led_settings"
                    decoded["device_id"] = dev_id
                    decoded["settings_kind"] = flag_byte
                    decoded["settings_block"] = block
                    decoded["records"] = [{
                        "id": dev_id,
                        "online": None,
                        "br_raw": None,
                        "br": {"type": "single", "raw": None, "pct": None},
                        "indicator_led_on": int(raw[data_start + 1]),
                        "indicator_led_off": int(raw[data_start + 2]),
                        "value_byte": None,
                        "tail_flag": None,
                        "is_on_from_tail": None,
                        "online_value": None,
                    }]
                    return decoded
            if rec and supports_sensor_advanced_settings(rec.capabilities) and flag_byte == 0x94 and len(raw) >= data_start + 9:
                block = int(raw[data_start])
                if block == 0x10:
                    led_code = int(raw[data_start + 2])
                    decoded["kind"] = "sensor_advanced_settings"
                    decoded["device_id"] = dev_id
                    decoded["settings_kind"] = flag_byte
                    decoded["settings_block"] = block
                    decoded["records"] = [{
                        "id": dev_id,
                        "online": None,
                        "br_raw": None,
                        "br": {"type": "single", "raw": None, "pct": None},
                        "sensor_led_indicator": led_code == 0x12,
                        "value_byte": None,
                        "tail_flag": None,
                        "is_on_from_tail": None,
                        "online_value": None,
                    }]
                    return decoded
            if rec and supports_plug_led_settings(rec.capabilities) and flag_byte == 0x94 and len(raw) >= data_start + 9:
                block = int(raw[data_start])
                marker = int(raw[data_start + 1])
                if block == 0x10 and marker == 0xFF:
                    decoded["kind"] = "plug_led_settings"
                    decoded["device_id"] = dev_id
                    decoded["settings_kind"] = flag_byte
                    decoded["settings_block"] = block
                    decoded["records"] = [{
                        "id": dev_id,
                        "online": None,
                        "br_raw": None,
                        "br": {"type": "single", "raw": None, "pct": None},
                        "plug_socket_led_indicator": int(raw[data_start + 2]) == 0x05,
                        "plug_usb_led_indicator": int(raw[data_start + 5]) == 0xBC,
                        "value_byte": None,
                        "tail_flag": None,
                        "is_on_from_tail": None,
                        "online_value": None,
                    }]
                    return decoded
            if rec and rec.capabilities.supports_gate and flag_byte in (0xbb, 0xbc, 0xbd):
                decoded["kind"] = "gate_settings"
                decoded["device_id"] = dev_id
                decoded["settings_kind"] = flag_byte
                record: Dict[str, Any] = {
                    "id": dev_id,
                    "online": None,
                    "br_raw": None,
                    "br": {"type": "single", "raw": None, "pct": None},
                    "value_byte": None,
                    "tail_flag": None,
                    "is_on_from_tail": None,
                    "online_value": None,
                    "settings_kind": flag_byte,
                }
                try:
                    if flag_byte == 0xbb and len(raw) >= data_start + 3 and raw[data_start] == 0x10:
                        width_ds = int(raw[data_start + 1])
                        record["gate_signal_width_seconds"] = max(1, min(5, round(width_ds / 10)))
                    elif flag_byte == 0xbc and len(raw) >= data_start + 9:
                        door_index = int(raw[data_start])
                        open_field_ms = int.from_bytes(raw[data_start + 1 : data_start + 3], byteorder="little")
                        close_field_ms = int.from_bytes(raw[data_start + 3 : data_start + 5], byteorder="little")
                        record.update({
                            "gate_door": door_index,
                            "gate_open_duration_ms": _decode_gate_open_duration_ms(open_field_ms),
                            "gate_close_duration_ms": _decode_gate_close_duration_ms(close_field_ms),
                            "gate_extra1_duration_ms": int.from_bytes(raw[data_start + 5 : data_start + 7], byteorder="little"),
                            "gate_extra2_duration_ms": int.from_bytes(raw[data_start + 7 : data_start + 9], byteorder="little"),
                        })
                    elif flag_byte == 0xbd:
                        decoded["kind"] = "gate_settings_info"
                        decoded["data_hex"] = raw[data_start:].hex()
                        decoded["records"] = [record]
                        return decoded
                    else:
                        decoded["kind"] = "d36969"
                        decoded["flag"] = flag_byte
                        decoded["data_hex"] = raw[data_start:].hex()
                        return decoded
                except Exception:
                    decoded["kind"] = "d36969"
                    decoded["flag"] = flag_byte
                    decoded["data_hex"] = raw[data_start:].hex()
                    return decoded
                decoded["records"] = [record]
                return decoded
            if flag_byte != 0xb9:
                return None

            if rec and rec.capabilities.supports_timer:
                decoded["kind"] = "timer_status"
                decoded["device_id"] = dev_id
                try:
                    decoded["timer_total_seconds"] = int.from_bytes(raw[data_start + 1 : data_start + 5], byteorder="little")
                    decoded["timer_remaining_seconds"] = int.from_bytes(raw[data_start + 5 : data_start + 9], byteorder="little")
                except Exception:
                    decoded["timer_total_seconds"] = None
                    decoded["timer_remaining_seconds"] = None
                decoded["records"] = [{
                    "id": dev_id,
                    "online": None,
                    "br_raw": None,
                    "br": {"type": "single", "raw": None, "pct": None},
                    "timer_total_seconds": decoded["timer_total_seconds"],
                    "timer_remaining_seconds": decoded["timer_remaining_seconds"],
                    "value_byte": None,
                    "tail_flag": None,
                    "is_on_from_tail": None,
                    "online_value": None,
                }]
                return decoded
            if rec and rec.capabilities.supports_sensor:
                decoded["kind"] = "sensor_params"
                decoded["device_id"] = dev_id
                try:
                    decoded["brightness_threshold"] = int(raw[data_start + 2])
                    decoded["hold_time_seconds"] = int.from_bytes(raw[data_start + 4 : data_start + 6], byteorder="little")
                    decoded["motion_sensitivity"] = int(raw[data_start + 6])
                    decoded["learned_brightness_threshold_raw"] = int.from_bytes(
                        raw[data_start + 7 : data_start + 9],
                        byteorder="little",
                    )
                except Exception:
                    decoded["brightness_threshold"] = None
                    decoded["hold_time_seconds"] = None
                    decoded["motion_sensitivity"] = None
                    decoded["learned_brightness_threshold_raw"] = None
                decoded["records"] = [{
                    "id": dev_id,
                    "online": None,
                    "br_raw": None,
                    "br": {"type": "single", "raw": None, "pct": None},
                    "hold_time_seconds": decoded.get("hold_time_seconds"),
                    "brightness_threshold": decoded.get("brightness_threshold"),
                    "motion_sensitivity": decoded.get("motion_sensitivity"),
                    "learned_brightness_threshold_raw": decoded.get("learned_brightness_threshold_raw"),
                    "value_byte": None,
                    "tail_flag": None,
                    "is_on_from_tail": None,
                    "online_value": None,
                }]
                return decoded
            if rec and rec.capabilities.supports_gate:
                decoded["kind"] = "gate_status"
                decoded["device_id"] = dev_id
                try:
                    door_index = int(raw[data_start + 1])
                    state_byte = int(raw[data_start + 3])
                    position_raw = int.from_bytes(raw[data_start + 4 : data_start + 6], byteorder="little")
                    runtime_ms = int.from_bytes(raw[data_start + 6 : data_start + 9], byteorder="little")
                    decoded_state = decode_gate_command_reply(
                        door_index,
                        state_byte,
                        position_raw,
                        runtime_ms,
                        rec.capabilities,
                    )
                except Exception:
                    door_index = None
                    state_byte = None
                    position_raw = None
                    runtime_ms = None
                    decoded_state = None
                decoded["records"] = [{
                    "id": dev_id,
                    "online": None,
                    "br_raw": None,
                    "br": {"type": "single", "raw": None, "pct": None},
                    "gate_door": door_index,
                    "door_state": state_byte,
                    "door_decoded": decoded_state,
                    "position_raw": position_raw,
                    "runtime_ms": runtime_ms,
                    "value_byte": None,
                    "tail_flag": None,
                    "is_on_from_tail": None,
                    "online_value": None,
                }]
                return decoded
            decoded["kind"] = "d36969"
            decoded["device_id"] = dev_id
            decoded["flag"] = flag_byte
            decoded["data_hex"] = raw[data_start:].hex()
            return decoded

        if len(raw) >= 14 and raw[7:10] == b"\xdc\x11\x02":
            dc_records: List[Dict[str, Any]] = []
            mesh_status_records: List[Dict[str, Any]] = []
            for offset in range(10, len(raw) - 3, 4):
                chunk = raw[offset : offset + 4]
                dev_id = int(chunk[0])
                if dev_id == 0:
                    continue
                if dev_id == 0xFF:
                    mesh_status_records.append({
                        "offset": offset,
                        "pseudo_id": "0xff",
                        "online_value": int(chunk[1]),
                        "value_byte": int(chunk[2]),
                        "tail_flag": int(chunk[3]),
                    })
                    continue
                value_byte = int(chunk[2])
                dc_records.append({
                    "id": dev_id,
                    "online": int(chunk[1]),
                    "br_raw": value_byte,
                    "br": self._decode_bulk_br(value_byte),
                    "rgb": None,
                    "value_byte": value_byte,
                    "tail_flag": int(chunk[3]),
                    "is_on_from_tail": bool(chunk[3] & 0x10),
                    "online_value": int(chunk[1]),
                })

            if mesh_status_records:
                decoded["mesh_status_records"] = mesh_status_records
                if not dc_records:
                    decoded["kind"] = "mesh_status"
                    return decoded
                decoded["kind"] = "single" if len(dc_records) == 1 else "record_container"
                decoded["records"] = dc_records
                return decoded
            if dc_records:
                decoded["kind"] = "single" if len(dc_records) == 1 else "record_container"
                decoded["records"] = dc_records
                return decoded

        if len(raw) >= 11:
            decoded["device_id"] = int(raw[10])
        if len(raw) >= 12:
            decoded["online_value"] = int(raw[11])
        if len(raw) >= 13:
            decoded["value_byte"] = int(raw[12])
            decoded["level"] = decoded["value_byte"]
        if len(raw) >= 14:
            tail = int(raw[13])
            decoded["tail_flag"] = tail
            decoded["is_on_from_tail"] = bool(tail & 0x10)

        if isinstance(decoded.get("device_id"), int):
            value_byte = decoded.get("value_byte")
            if isinstance(value_byte, int):
                record_br = self._decode_bulk_br(value_byte)
            else:
                record_br = {"type": "single", "raw": None, "pct": None}
            decoded["kind"] = "single"
            decoded["records"] = [{
                "id": decoded["device_id"],
                "online": decoded.get("online_value") if isinstance(decoded.get("online_value"), int) else None,
                "br_raw": value_byte if isinstance(value_byte, int) else None,
                "br": record_br,
                "rgb": decoded.get("rgb") if isinstance(decoded.get("rgb"), list) else None,
                "value_byte": value_byte,
                "tail_flag": decoded.get("tail_flag"),
                "is_on_from_tail": decoded.get("is_on_from_tail"),
                "online_value": decoded.get("online_value"),
            }]

        return decoded

    def apply_bledata_hex(
        self,
        ble_hex: str,
        *,
        payload_meta: Optional[Dict[str, Any]] = None,
        source: str = "hub_update",
        bulk_source: str = "hub_gwdata",
        full_snapshot: bool = False,
        queue_bulk: bool = False,
        notify_inventory: bool = True,
    ) -> int:
        """Decode and apply one core Pixie bleData payload to inventory."""
        decoded = self.decode_bledata_hex(ble_hex)
        if not decoded:
            self._log_debug("BLE decode: unable to parse data field")
            return 0

        kind = decoded.get("kind")
        self._log_debug("BLE apply: kind=%s hex_preview=%s", kind, ble_hex[:40] if ble_hex else "none")
        records = decoded.get("records") if isinstance(decoded.get("records"), list) else []

        if kind == "bulk":
            self._log_debug("BLE decode (bulk): records=%s", len(records))
            if queue_bulk:
                self._queue_bulk_ble_records(records, source=bulk_source, full_snapshot=full_snapshot)
            if self.inventory:
                applied = self._apply_bulk_ble_records_to_inventory(
                    records,
                    source=bulk_source,
                    full_snapshot=full_snapshot,
                    notify_inventory=notify_inventory,
                )
                self._log_debug("Inventory bulk update: applied=%s", applied)
                return applied
            return 0

        if kind == "mesh_status":
            self._log_debug("BLE mesh-status record(s): %s", decoded.get("mesh_status_records") or [])
            return 0

        payload_meta = payload_meta or {}
        if kind == "timer_status":
            if not self.inventory:
                return 0
            first = records[0] if records else {}
            dev_id = first.get("id")
            timer_total = first.get("timer_total_seconds")
            timer_remaining = first.get("timer_remaining_seconds")
            if isinstance(dev_id, int) and dev_id in self.inventory.devices_by_id:
                rec = self.inventory.devices_by_id[dev_id]
                now = time.time()
                current_total = rec.runtime.timer_total_seconds
                current_remaining = rec.runtime.timer_remaining_seconds
                last_poll_at = rec.runtime.last_timer_poll_at
                if (
                    isinstance(timer_remaining, int)
                    and isinstance(current_remaining, int)
                    and isinstance(last_poll_at, (int, float))
                    and timer_total == current_total
                ):
                    estimated_remaining = max(0.0, float(current_remaining) - (now - float(last_poll_at)))
                    correction = abs(float(timer_remaining) - estimated_remaining)
                    near_zero = timer_remaining <= 1 or estimated_remaining <= 1.0
                    if not near_zero and correction < TIMER_STATUS_CORRECTION_DEADBAND_SECONDS:
                        self._log_debug(
                            "Timer status update ignored within deadband: dev_id=%s total=%s remaining=%s estimated=%.2f correction=%.2f",
                            dev_id,
                            timer_total,
                            timer_remaining,
                            estimated_remaining,
                            correction,
                        )
                        return 0
                self.inventory.apply_device_update(
                    dev_id,
                    source=source,
                    timer_total_seconds=timer_total,
                    timer_remaining_seconds=timer_remaining,
                    last_timer_poll_at=now,
                )
                self._log_debug("Timer status update: dev_id=%s total=%s remaining=%s", dev_id, timer_total, timer_remaining)
                if notify_inventory:
                    self._notify_inventory_updated()
                return 1
            return 0

        if kind == "power_meter_status":
            if not self.inventory:
                return 0
            first = records[0] if records else {}
            dev_id = first.get("id")
            if isinstance(dev_id, int) and dev_id in self.inventory.devices_by_id:
                now = time.time()
                update_kwargs = {
                    key: first[key]
                    for key in (
                        "left_power_w",
                        "right_power_w",
                        "left_energy_kwh",
                        "right_energy_kwh",
                        "left_current_a",
                        "right_current_a",
                        "voltage_v",
                    )
                    if key in first
                }
                self.inventory.apply_device_update(
                    dev_id,
                    source=source,
                    last_power_meter_poll_at=now,
                    **update_kwargs,
                )
                self._log_debug(
                    "Power meter update: dev_id=%s subtype=0x%02x left_w=%s right_w=%s left_kwh=%s right_kwh=%s left_a=%s right_a=%s voltage=%s",
                    dev_id,
                    int(first.get("meter_subtype") or 0),
                    first.get("left_power_w"),
                    first.get("right_power_w"),
                    first.get("left_energy_kwh"),
                    first.get("right_energy_kwh"),
                    first.get("left_current_a"),
                    first.get("right_current_a"),
                    first.get("voltage_v"),
                )
                if notify_inventory:
                    self._notify_inventory_updated()
                return 1
            return 0

        if kind == "sensor_params":
            if not self.inventory:
                return 0
            dev_id = decoded.get("device_id")
            if isinstance(dev_id, int) and dev_id in self.inventory.devices_by_id:
                self.inventory.apply_device_update(
                    dev_id,
                    source=source,
                    hold_time_seconds=decoded.get("hold_time_seconds"),
                    brightness_threshold=decoded.get("brightness_threshold"),
                    motion_sensitivity=decoded.get("motion_sensitivity"),
                    learned_brightness_threshold_raw=decoded.get("learned_brightness_threshold_raw"),
                )
                self._log_debug(
                    "Sensor params update: dev_id=%s hold=%s bright=%s sens=%s learned_raw=%s",
                    dev_id,
                    decoded.get("hold_time_seconds"),
                    decoded.get("brightness_threshold"),
                    decoded.get("motion_sensitivity"),
                    decoded.get("learned_brightness_threshold_raw"),
                )
                if notify_inventory:
                    self._notify_inventory_updated()
                return 1
            return 0

        if kind == "sensor_advanced_settings":
            if not self.inventory:
                return 0
            first = records[0] if records else {}
            dev_id = first.get("id")
            if isinstance(dev_id, int) and dev_id in self.inventory.devices_by_id:
                self.inventory.apply_device_update(
                    dev_id,
                    source=source,
                    sensor_led_indicator=first.get("sensor_led_indicator"),
                )
                self._log_debug(
                    "Sensor advanced settings update: dev_id=%s led=%s",
                    dev_id,
                    first.get("sensor_led_indicator"),
                )
                if notify_inventory:
                    self._notify_inventory_updated()
                return 1
            return 0

        if kind == "indicator_led_settings":
            if not self.inventory:
                return 0
            first = records[0] if records else {}
            dev_id = first.get("id")
            if isinstance(dev_id, int) and dev_id in self.inventory.devices_by_id:
                self.inventory.apply_device_update(
                    dev_id,
                    source=source,
                    indicator_led_on=first.get("indicator_led_on"),
                    indicator_led_off=first.get("indicator_led_off"),
                )
                self._log_debug(
                    "Indicator LED settings update: dev_id=%s on=%s off=%s",
                    dev_id,
                    first.get("indicator_led_on"),
                    first.get("indicator_led_off"),
                )
                if notify_inventory:
                    self._notify_inventory_updated()
                return 1
            return 0

        if kind == "plug_led_settings":
            if not self.inventory:
                return 0
            first = records[0] if records else {}
            dev_id = first.get("id")
            if isinstance(dev_id, int) and dev_id in self.inventory.devices_by_id:
                self.inventory.apply_device_update(
                    dev_id,
                    source=source,
                    plug_socket_led_indicator=first.get("plug_socket_led_indicator"),
                    plug_usb_led_indicator=first.get("plug_usb_led_indicator"),
                )
                self._log_debug(
                    "Plug LED settings update: dev_id=%s socket=%s usb=%s",
                    dev_id,
                    first.get("plug_socket_led_indicator"),
                    first.get("plug_usb_led_indicator"),
                )
                if notify_inventory:
                    self._notify_inventory_updated()
                return 1
            return 0

        if kind == "gate_settings":
            if not self.inventory:
                return 0
            first = records[0] if records else {}
            dev_id = first.get("id")
            if not isinstance(dev_id, int) or dev_id not in self.inventory.devices_by_id:
                return 0
            rec = self.inventory.devices_by_id[dev_id]
            if not rec.capabilities.supports_gate:
                return 0

            update_kwargs: Dict[str, Any] = {
                "online": 1,
                "presence": "online",
            }
            if "gate_signal_width_seconds" in first:
                update_kwargs["gate_signal_width_seconds"] = first.get("gate_signal_width_seconds")

            door_index = first.get("gate_door")
            if isinstance(door_index, int):
                if door_index == 0:
                    update_kwargs["door1_open_duration_ms"] = first.get("gate_open_duration_ms")
                    update_kwargs["door1_close_duration_ms"] = first.get("gate_close_duration_ms")
                    update_kwargs["door1_extra1_duration_ms"] = first.get("gate_extra1_duration_ms")
                    update_kwargs["door1_extra2_duration_ms"] = first.get("gate_extra2_duration_ms")
                elif door_index == 1 and rec.capabilities.gate_doors >= 2:
                    update_kwargs["door2_open_duration_ms"] = first.get("gate_open_duration_ms")
                    update_kwargs["door2_close_duration_ms"] = first.get("gate_close_duration_ms")
                    update_kwargs["door2_extra1_duration_ms"] = first.get("gate_extra1_duration_ms")
                    update_kwargs["door2_extra2_duration_ms"] = first.get("gate_extra2_duration_ms")

            self.inventory.apply_device_update(dev_id, source=source, **update_kwargs)
            self._log_debug(
                "Gate settings update: dev_id=%s signal_width=%s door=%s open_ms=%s close_ms=%s extra1_ms=%s extra2_ms=%s",
                dev_id,
                first.get("gate_signal_width_seconds"),
                door_index,
                first.get("gate_open_duration_ms"),
                first.get("gate_close_duration_ms"),
                first.get("gate_extra1_duration_ms"),
                first.get("gate_extra2_duration_ms"),
            )
            if notify_inventory:
                self._notify_inventory_updated()
            return 1

        if kind == "gate_settings_info":
            if records:
                self._log_debug(
                    "Gate settings edit-info: dev_id=%s data=%s",
                    records[0].get("id"),
                    decoded.get("data_hex"),
                )
            return 0

        if kind == "gate_status":
            if not self.inventory or not records:
                return 0
            first = records[0]
            dev_id = first.get("id")
            door_index = first.get("gate_door")
            door_state = first.get("door_state")
            door_decoded = first.get("door_decoded")
            if not isinstance(dev_id, int) or not isinstance(door_index, int) or not isinstance(door_state, int):
                return 0

            rec = self.inventory.devices_by_id.get(dev_id)
            if rec is None:
                return 0

            previous = rec.runtime.door1_decoded if door_index == 0 else rec.runtime.door2_decoded
            previous_motion_plan = rec.runtime.door1_motion_plan if door_index == 0 else rec.runtime.door2_motion_plan
            updated_ms = int(time.time() * 1000)
            if not isinstance(door_decoded, dict) or not door_decoded.get("known"):
                self._log_debug(
                    "Gate unknown d36969 byte: dev_id=%s door=%s raw=0x%02x prev_state=%s prev_pos=%s prev_raw=%s",
                    dev_id,
                    door_index + 1,
                    door_state,
                    previous.get("state") if isinstance(previous, dict) else None,
                    previous.get("position_percent") if isinstance(previous, dict) else None,
                    previous.get("value_byte") if isinstance(previous, dict) else None,
                )

            update_kwargs: Dict[str, Any] = {
                "online": 1,
                "presence": "online",
                "raw": {
                    "hub_type": payload_meta.get("type"),
                    "hub_data": ble_hex,
                    "hub_utc": payload_meta.get("UTC"),
                    "ble_decoded": decoded,
                    "ble_interpreted": door_decoded,
                },
            }
            if door_index == 0:
                update_kwargs["door1_state"] = door_state
                if isinstance(door_decoded, dict) and door_decoded.get("known"):
                    update_kwargs["door1_decoded"] = door_decoded
                    update_kwargs["door1_motion_plan"] = build_gate_motion_plan(door_decoded, updated_ms)
                    if door_decoded.get("state") == "opening" and door_decoded.get("position_raw") == 0:
                        update_kwargs["door1_open_duration_ms"] = door_decoded.get("runtime_ms")
                    elif door_decoded.get("state") == "closing" and door_decoded.get("position_raw") == 1000:
                        update_kwargs["door1_close_duration_ms"] = door_decoded.get("runtime_ms")
            elif door_index == 1:
                update_kwargs["door2_state"] = door_state
                if isinstance(door_decoded, dict) and door_decoded.get("known"):
                    update_kwargs["door2_decoded"] = door_decoded
                    update_kwargs["door2_motion_plan"] = build_gate_motion_plan(door_decoded, updated_ms)
                    if door_decoded.get("state") == "opening" and door_decoded.get("position_raw") == 0:
                        update_kwargs["door2_open_duration_ms"] = door_decoded.get("runtime_ms")
                    elif door_decoded.get("state") == "closing" and door_decoded.get("position_raw") == 1000:
                        update_kwargs["door2_close_duration_ms"] = door_decoded.get("runtime_ms")

            if isinstance(door_decoded, dict) and not door_decoded.get("known"):
                if door_index == 0:
                    update_kwargs["door1_motion_plan"] = previous_motion_plan
                else:
                    update_kwargs["door2_motion_plan"] = previous_motion_plan

            self.inventory.apply_device_update(
                dev_id,
                source=source,
                updated_ms=updated_ms,
                **update_kwargs,
            )
            if notify_inventory:
                self._notify_inventory_updated()
            return 1

        if kind == "d36969":
            self._log_debug(
                "BLE d36969 unhandled: dev_id=%s flag=0x%02x data=%s",
                decoded.get("device_id"),
                decoded.get("flag") if isinstance(decoded.get("flag"), int) else 0,
                decoded.get("data_hex"),
            )
            return 0

        if not records or not self.inventory:
            return 0

        applied = 0
        for first in records:
            dev_id = first.get("id")
            if not isinstance(dev_id, int):
                continue
            rec = self.inventory.devices_by_id.get(dev_id)
            if not rec:
                self._buffer_unknown_device_update(
                    dev_id,
                    ble_hex=ble_hex,
                    payload_meta=payload_meta,
                    source=source,
                )
                self._notify_unknown_device_update(dev_id)
                self._log_debug("Inventory: unknown dev_id=%s (not in inventory)", dev_id)
                continue

            value_byte = first.get("value_byte")
            tail = first.get("tail_flag")
            on_tail = first.get("is_on_from_tail")
            if rec.capabilities.supports_timer:
                self._log_debug(
                    "TIMER bleData: dev_id=%s model=%s value=0x%02x tail=0x%02x",
                    dev_id,
                    rec.model_no,
                    value_byte if isinstance(value_byte, int) else 0,
                    tail if isinstance(tail, int) else 0,
                )
            self._log_debug(
                "BLE decode: dev_id=%s online_value=%s value=0x%02x tail=0x%02x on_tail=%s",
                dev_id,
                first.get("online_value"),
                value_byte if isinstance(value_byte, int) else 0,
                tail if isinstance(tail, int) else 0,
                on_tail,
            )

            prev_br = rec.runtime.br
            prev_cct = rec.runtime.cct
            prev_rgb = rec.runtime.rgb
            prev_r = rec.runtime.r
            prev_gate_decoded = {
                0: rec.runtime.door1_decoded if isinstance(rec.runtime.door1_decoded, dict) else None,
                1: rec.runtime.door2_decoded if isinstance(rec.runtime.door2_decoded, dict) else None,
            }

            interpreted = None
            mode = None
            rgb_from_packet = first.get("rgb") if isinstance(first.get("rgb"), list) else None
            br_from_packet = decoded.get("brightness_0_100") if isinstance(decoded.get("brightness_0_100"), int) else None
            record_online_value = first.get("online")
            if isinstance(record_online_value, int):
                update_kwargs: Dict[str, Any] = {"online": record_online_value}
            else:
                update_kwargs = {"online": 1, "presence": "online"}

            if rgb_from_packet is not None:
                update_kwargs["rgb"] = [int(rgb_from_packet[0]), int(rgb_from_packet[1]), int(rgb_from_packet[2])]
                if br_from_packet is not None:
                    update_kwargs["br"] = br_from_packet

            mode_from_packet = first.get("mode")
            relay_from_packet = first.get("relay")
            motion_from_packet = first.get("motion")
            if isinstance(mode_from_packet, int):
                update_kwargs["mode"] = mode_from_packet
            if isinstance(relay_from_packet, int):
                update_kwargs["relay"] = relay_from_packet
                update_kwargs["br"] = 100 if relay_from_packet else 0
            if isinstance(motion_from_packet, bool):
                update_kwargs["motion"] = motion_from_packet

            if isinstance(value_byte, int):
                interpreted = decode_value_byte_for_capabilities(rec.capabilities, value_byte)
                self._log_debug("BLE interpreted: model=%s mode=%s data=%s", rec.model_no, interpreted.get("mode"), json.dumps(interpreted, ensure_ascii=False, sort_keys=True))
                mode = interpreted.get("mode")

                if mode == "brightness":
                    update_kwargs["br"] = interpreted.get("brightness_0_100")
                elif mode == "dual_channel":
                    left_on = bool(interpreted.get("left_on"))
                    right_on = bool(interpreted.get("right_on"))
                    update_kwargs["r"] = 3 if left_on and right_on else 1 if left_on else 2 if right_on else 0
                    if supports_outlet_runtime_config(rec.capabilities):
                        update_kwargs["outlet_led_indicator"] = bool(value_byte & 0x10)
                        if isinstance(tail, int):
                            update_kwargs["outlet_all_device_control"] = bool(tail & 0x01)
                            update_kwargs["outlet_child_lock"] = bool(tail & 0x02)
                elif mode == "plug_with_usb":
                    relay_on = bool(interpreted.get("main_relay_on"))
                    usb_on = bool(interpreted.get("usb_on"))
                    update_kwargs["r"] = (1 if relay_on else 0) | (2 if usb_on else 0)
                    update_kwargs["br"] = 100 if relay_on else 0
                    if supports_plug_led_settings(rec.capabilities):
                        update_kwargs["outlet_all_device_control"] = bool(value_byte & 0x08)
                elif mode == "sensor_controller":
                    mode_value = interpreted.get("mode_value")
                    relay_on = interpreted.get("relay_on")
                    motion = interpreted.get("motion")
                    if isinstance(mode_value, int):
                        update_kwargs["mode"] = mode_value
                    if isinstance(relay_on, bool):
                        update_kwargs["relay"] = 1 if relay_on else 0
                        update_kwargs["br"] = 100 if relay_on else 0
                    if isinstance(motion, bool):
                        update_kwargs["motion"] = motion
                elif mode == "contact_sensor":
                    if isinstance(tail, int):
                        decoded_contact = decode_contact_runtime_state(
                            rec.capabilities,
                            value_byte,
                            tail,
                            prev_armed=rec.runtime.armed,
                            prev_source=rec.runtime.last_source,
                            allow_pulse=True,
                        )
                        if "armed" in decoded_contact:
                            update_kwargs["armed"] = decoded_contact.get("armed")
                        if "contact_active" in decoded_contact:
                            update_kwargs["contact_active"] = decoded_contact.get("contact_active")
                        update_kwargs["contact_momentary"] = bool(decoded_contact.get("pulse_event"))
                elif mode == "timer_switch":
                    timer_mode = interpreted.get("timer_mode")
                    restarting = interpreted.get("restarting")
                    self._log_debug("TIMER interpreted: dev_id=%s value=0x%02x timer_mode=%s restart=%s", dev_id, value_byte, timer_mode, restarting)
                    suppress_restart_countdown_reset = False
                    if restarting and isinstance(rec.runtime.local_timer_restart_at, (int, float)):
                        since_local_restart = time.time() - rec.runtime.local_timer_restart_at
                        suppress_restart_countdown_reset = 0 <= since_local_restart < LOCAL_TIMER_RESTART_GUARD_SECONDS
                    if timer_mode == "timer":
                        update_kwargs["mode"] = 1
                        update_kwargs["br"] = 100
                        prev_mode = rec.runtime.mode
                        prev_on = rec.runtime.is_on
                        if (prev_mode != 1 or (not prev_on and timer_mode == "timer")) and not suppress_restart_countdown_reset:
                            if rec.runtime.timer_total_seconds is not None:
                                update_kwargs["timer_remaining_seconds"] = rec.runtime.timer_total_seconds
                            update_kwargs["last_timer_poll_at"] = time.time()
                            update_kwargs["timer_needs_poll"] = True
                    elif timer_mode == "override":
                        update_kwargs["mode"] = 2
                        update_kwargs["br"] = 100
                    elif timer_mode is None:
                        update_kwargs["br"] = 0
                    if restarting and not suppress_restart_countdown_reset:
                        if rec.runtime.timer_total_seconds is not None:
                            update_kwargs["timer_remaining_seconds"] = rec.runtime.timer_total_seconds
                        update_kwargs["last_timer_poll_at"] = time.time()
                        update_kwargs["timer_needs_poll"] = True
                    elif restarting:
                        self._log_debug(
                            "TIMER restart-state countdown reset suppressed: dev_id=%s guard=%.1fs",
                            dev_id,
                            LOCAL_TIMER_RESTART_GUARD_SECONDS,
                        )
                elif mode == "tunable_white":
                    if isinstance(tail, int):
                        decoded_temp = decode_color_temp_runtime_state_for_capabilities(rec.capabilities, value_byte, tail)
                        brightness = decoded_temp.get("brightness_0_100")
                        if isinstance(brightness, int):
                            update_kwargs["br"] = brightness
                        cct = decoded_temp.get("cct")
                        if isinstance(cct, int):
                            update_kwargs["cct"] = cct
                elif mode == "color_effect":
                    if isinstance(tail, int):
                        decoded_color = decode_color_runtime_state_for_capabilities(rec.capabilities, value_byte, tail)
                        brightness = decoded_color.get("brightness_0_100")
                        if isinstance(brightness, int):
                            update_kwargs["br"] = brightness
                        if decoded_color.get("mode") == "tunable_white":
                            cct = decoded_color.get("cct")
                            if isinstance(cct, int):
                                update_kwargs["cct"] = cct
                            update_kwargs["effect"] = None
                        else:
                            if rec.capabilities.combined_runtime_encoding:
                                update_kwargs["cct"] = None
                            if "effect" in decoded_color:
                                update_kwargs["effect"] = decoded_color.get("effect")
                            if decoded_color.get("effect") is None and isinstance(decoded_color.get("rgb"), list):
                                rgb_update = [int(channel) for channel in decoded_color["rgb"]]
                                if decoded_color.get("white_preferred_tail") and isinstance(rec.runtime.local_ambiguous_blue_intent_until, (int, float)):
                                    now = time.time()
                                    if now <= rec.runtime.local_ambiguous_blue_intent_until:
                                        rgb_update = [0, 0, 255]
                                        self._log_debug(
                                            "BLE color decode: preserving recent local blue intent for white-preferred tail dev_id=%s tail=0x%02x",
                                            dev_id,
                                            tail,
                                        )
                                update_kwargs["rgb"] = rgb_update
                elif mode == "gate":
                    if isinstance(value_byte, int):
                        door1_decoded = decode_gate_state_byte(0, value_byte, rec.capabilities)
                        if door1_decoded.get("known"):
                            now_ms = int(time.time() * 1000)
                            stale_progress = is_stale_gate_motion_progress_update(
                                rec.runtime.door1_motion_plan,
                                door1_decoded,
                                now_ms,
                            )
                            if stale_progress:
                                self._log_debug(
                                    "Gate compact progress ignored as stale: dev_id=%s door=1 raw=0x%02x state=%s pos=%s",
                                    dev_id,
                                    value_byte,
                                    door1_decoded.get("state"),
                                    door1_decoded.get("position_percent"),
                                )
                            else:
                                update_kwargs["door1_state"] = value_byte
                                update_kwargs["door1_decoded"] = door1_decoded
                            motion_plan = sync_gate_motion_plan(rec.runtime.door1_motion_plan, door1_decoded, now_ms)
                            if motion_plan is None:
                                learned_duration_ms = rec.runtime.door1_open_duration_ms if door1_decoded.get("state") == "opening" else rec.runtime.door1_close_duration_ms
                                motion_plan = build_gate_motion_plan_from_learned_duration(door1_decoded, now_ms, learned_duration_ms)
                            update_kwargs["door1_motion_plan"] = motion_plan
                        else:
                            update_kwargs["door1_state"] = value_byte
                    if isinstance(tail, int) and rec.capabilities.gate_doors >= 2:
                        door2_decoded = decode_gate_state_byte(1, tail, rec.capabilities)
                        if door2_decoded.get("known"):
                            now_ms = int(time.time() * 1000)
                            stale_progress = is_stale_gate_motion_progress_update(
                                rec.runtime.door2_motion_plan,
                                door2_decoded,
                                now_ms,
                            )
                            if stale_progress:
                                self._log_debug(
                                    "Gate compact progress ignored as stale: dev_id=%s door=2 raw=0x%02x state=%s pos=%s",
                                    dev_id,
                                    tail,
                                    door2_decoded.get("state"),
                                    door2_decoded.get("position_percent"),
                                )
                            else:
                                update_kwargs["door2_state"] = tail
                                update_kwargs["door2_decoded"] = door2_decoded
                            motion_plan = sync_gate_motion_plan(rec.runtime.door2_motion_plan, door2_decoded, now_ms)
                            if motion_plan is None:
                                learned_duration_ms = rec.runtime.door2_open_duration_ms if door2_decoded.get("state") == "opening" else rec.runtime.door2_close_duration_ms
                                motion_plan = build_gate_motion_plan_from_learned_duration(door2_decoded, now_ms, learned_duration_ms)
                            update_kwargs["door2_motion_plan"] = motion_plan
                        else:
                            update_kwargs["door2_state"] = tail
                elif mode == "raw":
                    if rec.capabilities.supports_onoff and not rec.capabilities.supports_dimming and not rec.capabilities.supports_multi_channel and not rec.capabilities.supports_usb_subentity and not rec.capabilities.supports_cover:
                        update_kwargs["br"] = 100 if value_byte > 0 else 0

            update_kwargs["raw"] = {
                "hub_type": payload_meta.get("type"),
                "hub_data": ble_hex,
                "hub_utc": payload_meta.get("UTC"),
                "ble_decoded": decoded,
                "ble_interpreted": interpreted,
            }
            updated_runtime = self.inventory.apply_device_update(dev_id, source=source, **update_kwargs)
            if mode == "gate":
                gate_debug_states = [
                    (0, value_byte if isinstance(value_byte, int) else None, update_kwargs.get("door1_decoded")),
                ]
                if rec.capabilities.gate_doors >= 2:
                    gate_debug_states.append((1, tail if isinstance(tail, int) else None, update_kwargs.get("door2_decoded")))
                for door_index, raw_state, decoded_state in gate_debug_states:
                    if isinstance(raw_state, int) and (not isinstance(decoded_state, dict)):
                        previous = prev_gate_decoded.get(door_index)
                        self._log_debug(
                            "Gate unknown bleData byte: dev_id=%s door=%s raw=0x%02x prev_state=%s prev_pos=%s prev_raw=%s",
                            dev_id,
                            door_index + 1,
                            raw_state,
                            previous.get("state") if isinstance(previous, dict) else None,
                            previous.get("position_percent") if isinstance(previous, dict) else None,
                            previous.get("value_byte") if isinstance(previous, dict) else None,
                        )
            if updated_runtime is None:
                continue
            applied += 1
            if rec.capabilities.supports_timer:
                self._log_debug(
                    "TIMER state after update: dev_id=%s br=%s mode=%s is_on=%s total=%s remaining=%s",
                    dev_id,
                    updated_runtime.br,
                    updated_runtime.mode,
                    updated_runtime.is_on,
                    updated_runtime.timer_total_seconds,
                    updated_runtime.timer_remaining_seconds,
                )

            if notify_inventory:
                self._notify_inventory_updated()

            summary_parts = []
            if isinstance(record_online_value, int):
                summary_parts.append(f"online_value={record_online_value}")
                summary_parts.append(f"presence={updated_runtime.presence}")
            if rgb_from_packet is not None:
                summary_parts.append(f"rgb {prev_rgb}->{updated_runtime.rgb}")
                if br_from_packet is not None:
                    summary_parts.append(f"br {prev_br}->{updated_runtime.br}")
            elif interpreted and interpreted.get("mode") == "brightness":
                summary_parts.append(f"br {prev_br}->{updated_runtime.br}")
            elif interpreted and interpreted.get("mode") == "dual_channel":
                summary_parts.append(f"r {prev_r}->{updated_runtime.r}")
                summary_parts.append(f"channel={interpreted.get('channel_state')}")
            elif interpreted and interpreted.get("mode") == "raw" and isinstance(on_tail, bool):
                summary_parts.append(f"on_tail={on_tail}")
                summary_parts.append(f"br {prev_br}->{updated_runtime.br}")
            elif interpreted and interpreted.get("mode") == "tunable_white":
                summary_parts.append(f"br {prev_br}->{updated_runtime.br}")
                summary_parts.append(f"cct {prev_cct}->{updated_runtime.cct}")
            elif interpreted and interpreted.get("mode") == "color_effect":
                summary_parts.append(f"br {prev_br}->{updated_runtime.br}")
                summary_parts.append(f"rgb {prev_rgb}->{updated_runtime.rgb}")
                if updated_runtime.effect is not None:
                    summary_parts.append(f"effect={updated_runtime.effect}")
            else:
                summary_parts.append(f"value={value_byte}")
            self._log_debug("Inventory update: id=%s name=%s %s src=%s", dev_id, rec.name, ", ".join(summary_parts), source)
        return applied

    def _apply_cloud_params(self, cloud_params: CloudParams) -> None:
        """Apply cloud-derived parameters to the handler's in-memory context."""
        self.home_id = cloud_params.home_id
        self.home_name = cloud_params.home_name
        self.user_id = cloud_params.user_id
        self.meshnet = cloud_params.meshnet
        self.meshnet2 = cloud_params.meshnet2
        self.netid_seed = cloud_params.netid

    def _current_gateway_identity(self) -> Optional[GatewayIdentity]:
        """Return the current parsed gateway identity from inventory if available."""
        if self.inventory and self.inventory.gateway:
            self.gateway_identity = self.inventory.gateway
        return self.gateway_identity

    def _build_auth_result_snapshot(self, hub_ip: str, hub_port: int) -> Dict[str, Any]:
        """Build the legacy auth result shape from current in-memory handler state."""
        return {
            "status": "success",
            "config": {
                "netid": self.netid_seed,
                "meshnet": self.meshnet,
                "meshnet2": self.meshnet2,
            },
            "session_key_hex": self.session_key_hex,
            "hub_ip": hub_ip,
            "hub_port": hub_port,
        }

    def _resolve_gateway_ip(self, gateway_ip: Optional[str]) -> str:
        """Resolve a gateway IP either from the caller or via UDP discovery."""
        candidates = self._resolve_gateway_candidates(gateway_ip)
        if len(candidates) == 1:
            return candidates[0]
        self._log_warning("Multiple gateways discovered; unable to choose automatically")
        raise PixieGatewayResolutionError(
            "Multiple Pixie gateways were discovered; enter the gateway IP explicitly"
        )

    def _resolve_gateway_candidates(self, gateway_ip: Optional[str]) -> List[str]:
        """Return gateway IP candidates ordered by confidence."""
        if gateway_ip:
            self._log_debug("Using explicit gateway IP: %s", gateway_ip)
            return [gateway_ip]

        self._log_debug("Scanning LAN for Pixie gateways")
        discovered_hubs = self.scan_lan_for_hubs()

        if not discovered_hubs:
            self._log_warning("No gateways discovered via UDP broadcast")
            raise PixieGatewayResolutionError(
                "No Pixie gateway was discovered via UDP within 10 seconds"
            )

        expected_values = {str(v) for v in (self.meshnet, self.meshnet2) if v not in (None, "", "unknown")}

        def _candidate_rank(hub: PixieHub) -> tuple[int, str]:
            advertised_values = {str(v) for v in (hub.meshnet, hub.meshnet2) if v not in (None, "", "unknown")}
            return (0 if expected_values & advertised_values else 1, hub.host)

        ordered_hubs = sorted((hub for hub in discovered_hubs if hub.is_valid), key=_candidate_rank)
        candidates = [hub.host for hub in ordered_hubs]
        self._log_debug("Gateway candidates discovered: %s", candidates)
        return candidates

    def _start_runtime_session(
        self,
        hub_ip: str,
        *,
        stop_event: Optional[threading.Event],
        keep_control_alive: bool,
        command_device_id: Optional[int],
        command_state: Optional[bool],
        command_brightness: Optional[int],
        command_color_rgb: Optional[Tuple[int, int, int]],
        command_color_temp_cct: Optional[int],
        command_white: bool,
        command_effect: Optional[str],
        command_target: Optional[str],
        command_mode: Optional[int],
        command_cover_action: Optional[str],
        command_cover_action_map: Optional[Dict[str, int]],
        command_cover_tilt_action_map: Optional[Dict[str, int]],
        command_timer_action: Optional[str] = None,
        command_timer_duration: Optional[int] = None,
        command_power_meter_action: Optional[str] = None,
        command_sensor_param: Optional[str] = None,
        command_sensor_param_value: Optional[int] = None,
        command_gate_param: Optional[str] = None,
        command_gate_param_value: Optional[int] = None,
        command_gate_door: Optional[int] = None,
        command_indicator_led_action: Optional[str] = None,
        command_indicator_led_on: Optional[int] = None,
        command_indicator_led_off: Optional[int] = None,
        command_raw_hexes: Optional[Tuple[str, ...]] = None,
        command_raw_target: Optional[str] = None,
    ) -> PixieRuntimeSession:
        """Start the long-lived 41578 runtime session."""
        runtime_session = PixieRuntimeSession(
            handler=self,
            host=hub_ip,
            port=TCP_CONTROL_PORT,
            keep_control_alive=keep_control_alive,
            stop_event=stop_event or threading.Event(),
            command_kwargs={
                "command_device_id": command_device_id,
                "command_state": command_state,
                "command_brightness": command_brightness,
                "command_color_rgb": command_color_rgb,
                "command_color_temp_cct": command_color_temp_cct,
                "command_white": command_white,
                "command_effect": command_effect,
                "command_target": command_target,
                "command_mode": command_mode,
                "command_cover_action": command_cover_action,
                "command_cover_action_map": command_cover_action_map,
                "command_cover_tilt_action_map": command_cover_tilt_action_map,
                "command_timer_action": command_timer_action,
                "command_timer_duration": command_timer_duration,
                "command_power_meter_action": command_power_meter_action,
                "command_sensor_param": command_sensor_param,
                "command_sensor_param_value": command_sensor_param_value,
                "command_gate_param": command_gate_param,
                "command_gate_param_value": command_gate_param_value,
                "command_gate_door": command_gate_door,
                "command_indicator_led_action": command_indicator_led_action,
                "command_indicator_led_on": command_indicator_led_on,
                "command_indicator_led_off": command_indicator_led_off,
            },
        )
        runtime_session.start()
        self.runtime_session = runtime_session
        return runtime_session

    def _hydrate_local_inventory(
        self,
        runtime_session: PixieRuntimeSession,
        *,
        hub_ip: str,
        sync_timeout: float,
        cloud_home_cached: Optional[Dict[str, Any]],
    ) -> bool:
        """Build the initial inventory from 53216 plus runtime GwData when available."""
        self._log_debug("Hydrating startup inventory from %s:%s", hub_ip, TCP_SYNC_PORT)
        inventory_loaded = False
        hub_payload: Optional[Dict[str, Any]] = None
        net_id_int = int(str(self.netid_seed)) if self.netid_seed not in (None, "", "unknown") else None
        gateway_identity = self._current_gateway_identity()
        supports_53216 = (
            gateway_identity.supports_local_inventory_53216
            if gateway_identity is not None
            else True
        )

        if not supports_53216:
            self._log_debug(
                "Gateway model %s does not support %s inventory; using cloud inventory snapshot",
                gateway_identity.model_no if gateway_identity is not None else "unknown",
                TCP_SYNC_PORT,
            )
        elif net_id_int is not None:
            try:
                self._log_debug("Attempting one-shot %s inventory request", TCP_SYNC_PORT)
                sync_result = self._sync_inventory_53216_once(
                    hub_ip=hub_ip,
                    net_id_int=net_id_int,
                    mesh_net2_int=int(self.meshnet2),
                    timeout=sync_timeout,
                )
                payload = self._extract_53216_inventory_payload(sync_result.get("data"))
                if payload:
                    if not payload.get("objectId") and self.home_id:
                        payload["objectId"] = self.home_id
                    if not payload.get("name") and self.home_name not in (None, "", "unknown"):
                        payload["name"] = self.home_name
                    if payload.get("netID") is None and self.netid_seed not in (None, "", "unknown"):
                        payload["netID"] = self.netid_seed
                    if payload.get("meshNet") is None and self.meshnet not in (None, "", "unknown"):
                        payload["meshNet"] = self.meshnet
                    if payload.get("meshNet2") is None and self.meshnet2 not in (None, "", "unknown"):
                        payload["meshNet2"] = self.meshnet2
                    hub_payload = payload
                    self._log_debug("Captured gateway identity payload from port %s", TCP_SYNC_PORT)
                else:
                    self._log_warning("Port %s returned no usable startup inventory payload", TCP_SYNC_PORT)
            except Exception as sync_err:
                self._log_warning("Gateway %s startup inventory failed: %s", TCP_SYNC_PORT, sync_err)
        else:
            self._log_warning("netID unavailable; skipping %s startup inventory", TCP_SYNC_PORT)

        if hub_payload:
            self._log_debug("Waiting up to %ss for GwData bulk state", max(0.5, float(sync_timeout)))
            gw_deadline = time.time() + max(0.5, float(sync_timeout))
            while time.time() < gw_deadline:
                if runtime_session.ready_state.get("saw_bulk_bledata"):
                    break
                if runtime_session.error is not None:
                    break
                time.sleep(0.1)

            if runtime_session.ready_state.get("saw_bulk_bledata"):
                inventory_user_id = str(self.user_id) if self.user_id not in (None, "", "unknown") else "unknown"
                self._set_inventory_from_home_object(hub_payload, inventory_user_id, source="hub_53216")
                pending_batches = self._drain_bulk_ble_records()
                total_applied = 0
                updated_ids = set()
                for batch in pending_batches:
                    batch_source = str(batch.get("source") or "hub_gwdata")
                    batch_full_snapshot = bool(batch.get("full_snapshot", False))
                    records = batch.get("records") if isinstance(batch.get("records"), list) else []
                    total_applied += self._apply_bulk_ble_records_to_inventory(
                        records,
                        source=batch_source,
                        full_snapshot=batch_full_snapshot,
                    )
                    for rec_data in records:
                        try:
                            rec_id = int(rec_data.get("id"))
                        except Exception:
                            continue
                        if rec_id in self.inventory.devices_by_id:
                            updated_ids.add(rec_id)
                self._log_debug("Applied %s GwData bulk runtime updates", total_applied)

                all_ids = set(self.inventory.devices_by_id.keys()) if self.inventory else set()
                missing_ids = sorted(all_ids - updated_ids)
                if missing_ids:
                    self._log_debug(
                        "GwData bulk discrepancy: updated=%s inventory=%s missing=%s",
                        len(updated_ids),
                        len(all_ids),
                        len(missing_ids),
                    )
                    for miss_id in missing_ids:
                        miss_rec = self.inventory.devices_by_id.get(miss_id)
                        if not miss_rec:
                            continue
                        self._log_debug(
                            "GwData missing inventory device: id=%s model=%s name=%s",
                            miss_rec.id,
                            miss_rec.model_no,
                            miss_rec.name,
                        )
                else:
                    self._log_debug("GwData bulk covered all inventory devices")

                if self.verbose:
                    post_debug_dump = self.inventory.debug_lines_verbose()
                    self._log_multiline_debug("Final startup inventory snapshot after runtime hydration", post_debug_dump)
                else:
                    post_debug_dump = self.inventory.debug_lines()
                    self._log_multiline_debug("Final startup inventory summary", post_debug_dump)
                inventory_loaded = True
                self._log_debug("Startup inventory source: hub %s + GwData bulk", TCP_SYNC_PORT)
            else:
                self._log_warning("GwData bulk not ready before timeout; using cloud fallback snapshot")

        self._awaiting_initial_gwdata_bulk = False

        if inventory_loaded:
            self.inventory_mode = "local_53216"
            return True

        self._log_debug("Falling back to Home API inventory snapshot")
        home_obj = cloud_home_cached
        if home_obj is None:
            home_obj = self._fetch_home_object(
                homeid=str(self.home_id) if self.home_id else None,
                sessiontoken=str(self.session_token) if self.session_token else None,
            )
        if home_obj:
            inventory_user_id = str(self.user_id) if self.user_id not in (None, "", "unknown") else "unknown"
            fallback_source = "cloud_fallback_cached" if home_obj is cloud_home_cached else "cloud_fallback"
            self._set_inventory_from_home_object(home_obj, inventory_user_id, source=fallback_source)
            self.inventory_mode = "cloud_fallback"
            self._log_debug("Startup inventory source: %s", fallback_source)
            return True

        self._log_warning("Home API fallback inventory unavailable")
        return False

    def fetch_cloud_params(
        self,
        username: str,
        password: str,
        *,
        include_inventory_seed: bool = True,
        selected_home_id: Optional[str] = None,
    ) -> CloudParams:
        """Fetch cloud-derived parameters required to access the local gateway."""
        config = self._fetch_login_data(
            username,
            password,
            include_inventory_seed=include_inventory_seed,
            selected_home_id=selected_home_id,
        )
        cloud_params = CloudParams(
            home_id=str(config.get("homeid") or "unknown"),
            home_name=str(config.get("home_name") or "unknown"),
            user_id=str(config.get("userid") or "unknown"),
            meshnet=str(config.get("meshnet") or "unknown"),
            meshnet2=str(config.get("meshnet2") or "unknown"),
            netid=str(config.get("netid") or "unknown"),
        )
        self._apply_cloud_params(cloud_params)
        self.session_token = config.get("sessiontoken")
        return cloud_params

    async def async_fetch_cloud_params(
        self,
        username: str,
        password: str,
        *,
        include_inventory_seed: bool = True,
        selected_home_id: Optional[str] = None,
    ) -> CloudParams:
        """Async wrapper for cloud parameter retrieval."""
        return await asyncio.to_thread(
            self.fetch_cloud_params,
            username,
            password,
            include_inventory_seed=include_inventory_seed,
            selected_home_id=selected_home_id,
        )

    def bootstrap_gateway(
        self,
        cloud_params: CloudParams,
        *,
        username: str,
        password: str,
        gateway_ip: Optional[str] = None,
        sync_timeout: float = 5.0,
        command_device_id: Optional[int] = None,
        command_state: Optional[bool] = None,
        command_brightness: Optional[int] = None,
        command_color_rgb: Optional[Tuple[int, int, int]] = None,
        command_color_temp_cct: Optional[int] = None,
        command_white: bool = False,
        command_effect: Optional[str] = None,
        command_target: Optional[str] = None,
        command_mode: Optional[int] = None,
        command_cover_action: Optional[str] = None,
        command_cover_action_map: Optional[Dict[str, int]] = None,
        command_cover_tilt_action_map: Optional[Dict[str, int]] = None,
        command_timer_action: Optional[str] = None,
        command_timer_duration: Optional[int] = None,
        command_power_meter_action: Optional[str] = None,
        command_sensor_param: Optional[str] = None,
        command_sensor_param_value: Optional[int] = None,
        command_gate_param: Optional[str] = None,
        command_gate_param_value: Optional[int] = None,
        command_gate_door: Optional[int] = None,
        command_indicator_led_action: Optional[str] = None,
        command_indicator_led_on: Optional[int] = None,
        command_indicator_led_off: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
        keep_control_alive: bool = False,
        wait_for_shutdown: bool = False,
        hydrate_inventory: bool = True,
    ) -> PixieRuntimeData:
        """Bootstrap local gateway access using already-fetched cloud parameters."""
        self._apply_cloud_params(cloud_params)
        self.stored_username = username
        self.stored_password = password

        auth_result = self.discover_and_connect(
            username=username,
            password=password,
            hub_ip=gateway_ip,
            login_required=False,
            sync_timeout=sync_timeout,
            command_device_id=command_device_id,
            command_state=command_state,
            command_brightness=command_brightness,
            command_color_rgb=command_color_rgb,
            command_color_temp_cct=command_color_temp_cct,
            command_white=command_white,
            command_effect=command_effect,
            command_target=command_target,
            command_mode=command_mode,
            command_cover_action=command_cover_action,
            command_cover_action_map=command_cover_action_map,
            command_cover_tilt_action_map=command_cover_tilt_action_map,
            command_timer_action=command_timer_action,
            command_timer_duration=command_timer_duration,
            command_power_meter_action=command_power_meter_action,
            command_sensor_param=command_sensor_param,
            command_sensor_param_value=command_sensor_param_value,
            command_gate_param=command_gate_param,
            command_gate_param_value=command_gate_param_value,
            command_gate_door=command_gate_door,
            command_indicator_led_action=command_indicator_led_action,
            command_indicator_led_on=command_indicator_led_on,
            command_indicator_led_off=command_indicator_led_off,
            stop_event=stop_event,
            keep_control_alive=keep_control_alive,
            wait_for_shutdown=wait_for_shutdown,
            hydrate_inventory=hydrate_inventory,
        )

        self._current_gateway_identity()
        return PixieRuntimeData(
            handler=self,
            runtime_session=self.runtime_session,
            inventory=self.inventory,
            inventory_mode=self.inventory_mode,
        )

    async def async_bootstrap_gateway(self, cloud_params: CloudParams, **kwargs: Any) -> PixieRuntimeData:
        """Async wrapper for local gateway bootstrap."""
        return await asyncio.to_thread(self.bootstrap_gateway, cloud_params, **kwargs)

    def scan_lan_for_hubs(self, broadcast_address: str = "255.255.255.255",
                          timeout: int = 10) -> List[PixieHub]:
        """
        Scan local network for Pixie hubs via UDP broadcast.

        Matches q0.b logic from Android app - discovers gateways automatically.

        Args:
            broadcast_address: IPv4 multicast/broadcast address (default: 255.255.255.255)
            timeout: Seconds to wait for responses

        Returns:
            List of discovered Hub objects
        """

        self._log_debug("Scanning LAN for Pixie hubs via UDP broadcast")

        hubs_found: List[PixieHub] = []
        sock: Optional[socket.socket] = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", UDP_DISCOVERY_PORT))
            sock.settimeout(timeout)

            self._log_debug(
                "Listening for UDP broadcasts from hubs on port %s (passive mode)",
                UDP_DISCOVERY_PORT,
            )

            hubs_found, decoded = listen_for_responses(sock, timeout=timeout)

            if decoded:
                meshnet = decoded.get("meshNet")
                if meshnet and self.meshnet in (None, "", "unknown"):
                    self.meshnet = meshnet
                    self._log_debug("Updated meshNet from UDP response: %s", meshnet)
                meshnet2 = decoded.get("meshNet2")
                if meshnet2 and self.meshnet2 in (None, "", "unknown"):
                    self.meshnet2 = meshnet2
                    self._log_debug("Updated meshNet2 from UDP response: %s", meshnet2)
                if meshnet2 and not self.meshnet:
                    self.meshnet = meshnet2
                    self._log_debug("Updated meshNet from meshNet2 fallback: %s", meshnet2)

        except Exception as exc:
            self._log_warning("Discovery scan error: %s", exc)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        self._log_debug("Discovery scan finished with %s hub(s)", len(hubs_found))
        return hubs_found

    def discover_and_connect(
        self,
        username: str,
        password: str,
        hub_ip: Optional[str] = None,
        login_required: bool = True,
        sync_timeout: float = 5.0,
        command_device_id: Optional[int] = None,
        command_state: Optional[bool] = None,
        command_brightness: Optional[int] = None,
        command_color_rgb: Optional[Tuple[int, int, int]] = None,
        command_color_temp_cct: Optional[int] = None,
        command_white: bool = False,
        command_effect: Optional[str] = None,
        command_target: Optional[str] = None,
        command_mode: Optional[int] = None,
        command_cover_action: Optional[str] = None,
        command_cover_action_map: Optional[Dict[str, int]] = None,
        command_cover_tilt_action_map: Optional[Dict[str, int]] = None,
        command_timer_action: Optional[str] = None,
        command_timer_duration: Optional[int] = None,
        command_power_meter_action: Optional[str] = None,
        command_sensor_param: Optional[str] = None,
        command_sensor_param_value: Optional[int] = None,
        command_gate_param: Optional[str] = None,
        command_gate_param_value: Optional[int] = None,
        command_gate_door: Optional[int] = None,
        command_indicator_led_action: Optional[str] = None,
        command_indicator_led_on: Optional[int] = None,
        command_indicator_led_off: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
        keep_control_alive: bool = True,
        wait_for_shutdown: bool = True,
        hydrate_inventory: bool = True,
    ) -> Dict[str, Any]:
        """
        Full autonomous discovery + connection flow.

        Args:
            username: User's email/username
            password: User's password
            hub_ip: Optional override for specific hub IP (for testing)
            command_device_id: Optional device ID to command after handshake
            command_state: Optional target on/off state for local command
            command_brightness: Optional brightness 0-100 for brightness command
            command_color_rgb: Optional RGB tuple for color command
            command_color_temp_cct: Optional 0-255 tunable-white position
            command_white: Whether color command was requested via --white
            command_mode: Optional sensor mode command (0=switch, 1=motion, 2=photocell when supported)
            command_target: Optional target endpoint for on/off command (relay|usb|left|right|both)
            command_cover_action: Optional cover action command

        Returns:
            Complete auth result with extracted credentials and session key
        """

        self._log_debug("Starting Pixie gateway bootstrap flow")

        with self._pending_bulk_lock:
            self._pending_bulk_ble_updates.clear()
        self._awaiting_initial_gwdata_bulk = True

        # Step 1: Fetch metadata from cloud API only when required.
        needs_login = login_required or not all([self.netid_seed, self.meshnet])
        config: Dict[str, Any] = {
            "netid": self.netid_seed,
            "meshnet": self.meshnet,
            "meshnet2": self.meshnet2,
            "homeid": self.home_id,
            "home_name": self.home_name,
            "userid": self.user_id,
            "sessiontoken": self.session_token,
        }
        if needs_login:
            self._log_debug("Fetching metadata from Pixie cloud API")
            config = self._fetch_login_data(username, password, include_inventory_seed=False)
        else:
            self._log_debug("Using cached login metadata")

        cloud_home_cached = self._cached_cloud_home_obj if isinstance(self._cached_cloud_home_obj, dict) else None
        if cloud_home_cached:
            self._log_debug("Cloud Home snapshot cached for startup fallback")
        else:
            self._log_debug("No cached cloud Home snapshot available")

        # Store metadata for hub discovery/53216/control orchestration
        self.meshnet = config.get('meshnet')
        self.meshnet2 = config.get('meshnet2')
        self.netid_seed = config.get('netid')
        self.home_id = config.get('homeid')
        self.home_name = config.get('home_name')
        self.user_id = config.get('userid')
        self.session_token = config.get('sessiontoken')

        # also store credentials for the step 3 save path
        self.stored_username = username
        self.stored_password = password

        self._log_debug("Bootstrap metadata: meshNet=%s meshNet2=%s netID=%s", self.meshnet, self.meshnet2, self.netid_seed)

        if hub_ip is None:
            candidate_hosts = self._resolve_gateway_candidates(None)
            last_error: Optional[Exception] = None
            for candidate_host in candidate_hosts:
                self._log_debug("Trying Pixie gateway candidate %s for home %s", candidate_host, self.home_id)
                try:
                    return self.discover_and_connect(
                        username=username,
                        password=password,
                        hub_ip=candidate_host,
                        login_required=False,
                        sync_timeout=sync_timeout,
                        command_device_id=command_device_id,
                        command_state=command_state,
                        command_brightness=command_brightness,
                        command_color_rgb=command_color_rgb,
                        command_color_temp_cct=command_color_temp_cct,
                        command_white=command_white,
                        command_effect=command_effect,
                        command_target=command_target,
                        command_mode=command_mode,
                        command_cover_action=command_cover_action,
                        command_cover_action_map=command_cover_action_map,
                        command_cover_tilt_action_map=command_cover_tilt_action_map,
                        command_timer_action=command_timer_action,
                        command_timer_duration=command_timer_duration,
                        command_power_meter_action=command_power_meter_action,
                        command_sensor_param=command_sensor_param,
                        command_sensor_param_value=command_sensor_param_value,
                        command_gate_param=command_gate_param,
                        command_gate_param_value=command_gate_param_value,
                        command_gate_door=command_gate_door,
                        command_indicator_led_action=command_indicator_led_action,
                        command_indicator_led_on=command_indicator_led_on,
                        command_indicator_led_off=command_indicator_led_off,
                        stop_event=stop_event,
                        keep_control_alive=keep_control_alive,
                        wait_for_shutdown=wait_for_shutdown,
                        hydrate_inventory=hydrate_inventory,
                    )
                except PixieAuthError as exc:
                    last_error = exc
                    self._log_debug("Pixie gateway candidate %s rejected: %s", candidate_host, exc)
                    if self.runtime_session is not None:
                        self.runtime_session.stop_and_join(timeout=5.0)
                    self.runtime_session = None
                    self.session_key_hex = None

            raise PixieGatewayResolutionError(
                f"No discovered Pixie gateway matched the selected home ({last_error})"
            )

        hub_ip = self._resolve_gateway_ip(hub_ip)

        # Step 3: Start 41578 control loop in background and keep it alive.
        self._log_debug("Starting TCP control channel on %s:%s", hub_ip, TCP_CONTROL_PORT)
        runtime_session = self._start_runtime_session(
            hub_ip,
            stop_event=stop_event,
            keep_control_alive=keep_control_alive,
            command_device_id=command_device_id,
            command_state=command_state,
            command_brightness=command_brightness,
            command_color_rgb=command_color_rgb,
            command_color_temp_cct=command_color_temp_cct,
            command_white=command_white,
            command_effect=command_effect,
            command_target=command_target,
            command_mode=command_mode,
            command_cover_action=command_cover_action,
            command_cover_action_map=command_cover_action_map,
            command_cover_tilt_action_map=command_cover_tilt_action_map,
            command_timer_action=command_timer_action,
            command_timer_duration=command_timer_duration,
            command_power_meter_action=command_power_meter_action,
            command_sensor_param=command_sensor_param,
            command_sensor_param_value=command_sensor_param_value,
            command_gate_param=command_gate_param,
            command_gate_param_value=command_gate_param_value,
            command_gate_door=command_gate_door,
            command_indicator_led_action=command_indicator_led_action,
            command_indicator_led_on=command_indicator_led_on,
            command_indicator_led_off=command_indicator_led_off,
        )

        priming_timeout = 5.0
        primed = runtime_session.wait_until_primed(timeout=priming_timeout)
        if primed:
            if hydrate_inventory:
                self._log_debug("41578 primed; starting one-shot %s inventory hydration", TCP_SYNC_PORT)
            else:
                self._log_debug("41578 primed; using cloud inventory snapshot")
        else:
            self._log_warning("41578 priming timeout; continuing startup inventory with state=%s", runtime_session.ready_state)

        if hydrate_inventory:
            self._hydrate_local_inventory(
                runtime_session,
                hub_ip=hub_ip,
                sync_timeout=sync_timeout,
                cloud_home_cached=cloud_home_cached,
            )

        if keep_control_alive and wait_for_shutdown:
            self._log_debug("Control channel remains active on port %s awaiting shutdown", TCP_CONTROL_PORT)
            try:
                while runtime_session.is_alive():
                    runtime_session.join(timeout=0.5)
            except KeyboardInterrupt:
                self._log_info("Stop requested while waiting for control channel shutdown")
                runtime_session.stop_and_join(timeout=3.0)
                result = runtime_session.result
                if result:
                    return result
                raise PixieAuthError("Stopped by user")
        elif not keep_control_alive:
            runtime_session.stop_and_join(timeout=5.0)

        if runtime_session.error is not None:
            raise PixieGatewayConnectionError(f"Control channel failed: {runtime_session.error}")

        if wait_for_shutdown or not keep_control_alive:
            auth_result = runtime_session.result
            if not auth_result:
                raise PixieGatewayConnectionError("Handshake capture failed - ensure the gateway is reachable")
            return auth_result

        auth_snapshot = self._build_auth_result_snapshot(hub_ip, TCP_CONTROL_PORT)
        if not auth_snapshot.get("session_key_hex"):
            raise PixieAuthError("Handshake did not yield a session key before bootstrap completion")
        return auth_snapshot

    @staticmethod
    def _derive_sync_53216_key(unix_seconds: int, net_id: int) -> bytes:
        """Derive the 16-byte AES key used by the 53216 EA/EB exchange."""
        xor_val = int(net_id) ^ int(unix_seconds)
        combined = "Pixie" + format(xor_val & 0xFFFFFFFFFFFFFFFF, "x")
        arr = bytearray(16)
        for i, ch in enumerate(combined[:16]):
            arr[i] = ord(ch)
        return bytes(arr)

    @staticmethod
    def _build_sync_53216_ea(unix_seconds: int, net_id: int, nonce: int) -> bytes:
        """Build EA wire message: ea + len + nonce + base64(0x01 + AES_CBC(payload))."""
        return PixieAuthHandler._build_sync_53216_ea_for_payload(
            unix_seconds,
            net_id,
            nonce,
            b'{"get":{"selected":127}}',
        )

    @staticmethod
    def _build_sync_53216_ea_for_payload(unix_seconds: int, net_id: int, nonce: int, plaintext: bytes) -> bytes:
        """Build EA wire message for an arbitrary 53216 JSON payload."""
        iv = b"0" * 16
        key = PixieAuthHandler._derive_sync_53216_key(unix_seconds, net_id)
        ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, 16))
        payload_b64 = base64.b64encode(bytes([0x01]) + ciphertext).decode("ascii")
        wire = f"ea{len(payload_b64):08x}{nonce:08x}{payload_b64}".encode("ascii")
        return wire

    @staticmethod
    def _derive_sync_53216_nonce(unix_seconds: int, mesh_net2: int) -> int:
        """Derive 53216 nonce from timestamp and meshNet2.

        The gateway reconstructs the timestamp from the nonce using:
          ts_high  = (nonce >> 24) ^ (meshNet2 >> 24)
          ts_low24 = (nonce & 0xFFFFFF) ^ (meshNet2 & 0xFFFFFF)

        So the nonce must encode the clock so the gateway can recover it.
        """
        xor_const = int(mesh_net2) & 0xFFFFFF
        nonce_high = ((int(unix_seconds) >> 24) ^ (int(mesh_net2) >> 24)) & 0xFF
        ts_low24 = int(unix_seconds) & 0xFFFFFF
        nonce_low24 = ts_low24 ^ xor_const
        return ((nonce_high << 24) | (nonce_low24 & 0xFFFFFF)) & 0xFFFFFFFF

    @staticmethod
    def _read_sync_53216_eb_frames(
        sock: socket.socket,
        timeout: float = 5.0,
    ) -> Tuple[bytes, List[str], List[Dict[str, Any]]]:
        """Read EB bytes from 53216 and return raw bytes, payload chunks, and parse metadata."""
        def _parse_frames(raw_ascii: str) -> Tuple[List[str], List[Dict[str, Any]], bool]:
            payload_parts: List[str] = []
            frame_infos: List[Dict[str, Any]] = []
            idx = 0
            saw_incomplete = False

            while idx < len(raw_ascii):
                marker = raw_ascii.find("eb", idx)
                if marker < 0:
                    break
                if marker + 10 > len(raw_ascii):
                    frame_infos.append({
                        "marker": marker,
                        "incomplete_header": True,
                        "available": len(raw_ascii) - marker,
                        "required": 10,
                    })
                    saw_incomplete = True
                    break

                length_hex = raw_ascii[marker + 2:marker + 10]
                try:
                    payload_len = int(length_hex, 16)
                except ValueError:
                    idx = marker + 2
                    continue

                payload_start = marker + 10
                payload_end = payload_start + payload_len
                if payload_end > len(raw_ascii):
                    frame_infos.append({
                        "marker": marker,
                        "length_hex": length_hex,
                        "payload_len": payload_len,
                        "payload_start": payload_start,
                        "payload_end": payload_end,
                        "in_bounds": False,
                        "missing_bytes": payload_end - len(raw_ascii),
                    })
                    saw_incomplete = True
                    break

                payload = raw_ascii[payload_start:payload_end]
                payload_parts.append(payload)
                frame_infos.append({
                    "marker": marker,
                    "length_hex": length_hex,
                    "payload_len": payload_len,
                    "payload_start": payload_start,
                    "payload_end": payload_end,
                    "in_bounds": True,
                    "payload_preview": payload[:48],
                })
                idx = payload_end

            return payload_parts, frame_infos, saw_incomplete

        chunks: List[bytes] = []
        per_recv_timeout = min(max(float(timeout), 0.1), 1.0)
        sock.settimeout(per_recv_timeout)
        deadline = time.time() + max(timeout * 4.0, 10.0)

        payload_parts: List[str] = []
        frame_infos: List[Dict[str, Any]] = []
        saw_incomplete = False

        while time.time() < deadline:
            try:
                packet = sock.recv(4096)
                if not packet:
                    break
                chunks.append(packet)
            except socket.timeout:
                # Keep waiting if headers advertise more bytes than currently buffered.
                if saw_incomplete:
                    continue
                if payload_parts:
                    break
                continue

            raw_ascii = b"".join(chunks).decode("ascii", errors="ignore")
            payload_parts, frame_infos, saw_incomplete = _parse_frames(raw_ascii)
            if payload_parts and not saw_incomplete:
                break

        raw = b"".join(chunks)
        if not raw:
            raise PixieAuthError("No data received from port 53216")

        raw_ascii = raw.decode("ascii", errors="ignore")
        payload_parts, frame_infos, saw_incomplete = _parse_frames(raw_ascii)

        if saw_incomplete:
            missing = None
            if frame_infos:
                last = frame_infos[-1]
                missing = last.get("missing_bytes") or max(0, int(last.get("required", 0)) - int(last.get("available", 0)))
            raise PixieAuthError(
                "Incomplete EB frame from 53216 response"
                + (f" (missing~{missing} bytes)" if isinstance(missing, int) else "")
            )

        if not payload_parts:
            raise PixieAuthError("Could not parse EB frame header(s) from 53216 response")

        return raw, payload_parts, frame_infos

    @staticmethod
    def _decrypt_sync_53216_eb_payload(unix_seconds: int, net_id: int, eb_payload_b64: str) -> Tuple[Any, str]:
        """Decrypt EB payload and return parsed JSON object + decode mode.

        The primary path intentionally matches decrypt_test3.py exactly:
        base64-decode -> AES-CBC decrypt -> unpad(or rstrip(0x00)) -> base64-decode -> JSON.
        """
        iv = b"0" * 16
        key = PixieAuthHandler._derive_sync_53216_key(unix_seconds, net_id)

        cleaned = "".join(eb_payload_b64.split())
        enc = base64.b64decode(cleaned + "=" * ((-len(cleaned)) % 4))
        enc_raw_len = len(enc)
        if len(enc) % 16 != 0:
            enc = enc[: len(enc) - (len(enc) % 16)]
        if not enc:
            raise PixieAuthError("EB ciphertext is empty after block alignment")

        pt = AES.new(key, AES.MODE_CBC, iv).decrypt(enc)
        try:
            pt = unpad(pt, 16)
        except Exception:
            pt = pt.rstrip(b"\x00")

        # Strict decrypt_test3 behavior: decrypted bytes are base64-wrapped JSON.
        try:
            json_bytes = base64.b64decode(pt)
            return json.loads(json_bytes.decode("utf-8")), "json_b64_wrapped_exact"
        except Exception:
            pass

        # Fallback A: direct UTF-8 JSON.
        try:
            text = pt.decode("utf-8")
            if text.lstrip().startswith("{"):
                return json.loads(text), "json_direct"
        except Exception:
            pass

        # Fallback B: base64 with optional padding recovery.
        try:
            json_bytes = base64.b64decode(pt + b"=" * ((-len(pt)) % 4))
            return json.loads(json_bytes.decode("utf-8")), "json_b64_wrapped"
        except Exception:
            pass

        # Fallback C: leading status byte + base64-wrapped JSON.
        if len(pt) > 1:
            try:
                json_bytes = base64.b64decode(pt[1:] + b"=" * ((-len(pt[1:])) % 4))
                return json.loads(json_bytes.decode("utf-8")), "json_b64_wrapped_skip1"
            except Exception:
                pass

        # Provide small diagnostic preview for troubleshooting.
        preview_hex = pt[:32].hex()
        preview_ascii = "".join(chr(b) if 32 <= b < 127 else "." for b in pt[:64])
        key_hex = key.hex()
        raise PixieAuthError(
            "Decrypted payload not recognized as JSON "
            f"(key={key_hex}, eb_raw_len={enc_raw_len}, eb_ct_len={len(enc)}, "
            f"pt_len={len(pt)}, hex={preview_hex}, ascii={preview_ascii})"
        )

    @staticmethod
    def _is_valid_53216_inventory_payload(obj: Any) -> bool:
        """Return True only for strong inventory-like payloads (avoid false positives like `26`)."""
        if not isinstance(obj, dict):
            return False

        # Canonical shape from decrypt_test3/decrypt_test_EB_result:
        # {"result":"success", "data": {"deviceList": [...]}}
        if "data" in obj and isinstance(obj.get("data"), dict):
            data_obj = obj.get("data")
            if isinstance(data_obj.get("deviceList"), list):
                return True

        # Relaxed fallback: payload itself may be the data object.
        if isinstance(obj.get("deviceList"), list):
            return True

        return False

    @staticmethod
    def _extract_53216_inventory_payload(obj: Any) -> Optional[Dict[str, Any]]:
        """Extract inventory object from decrypted 53216 JSON response."""
        if not isinstance(obj, dict):
            return None

        data_obj = obj.get("data")
        if isinstance(data_obj, dict) and isinstance(data_obj.get("deviceList"), list):
            return dict(data_obj)

        if isinstance(obj.get("deviceList"), list):
            return dict(obj)

        return None

    def _set_inventory_from_home_object(
        self,
        home_obj: Dict[str, Any],
        user_id: str,
        source: str,
        *,
        show_devices: Optional[bool] = None,
    ) -> None:
        """Build and assign normalized inventory from a Home-like object payload."""
        previous_inventory = self.inventory
        self.inventory = PixieInventory.from_home_object(
            home_obj,
            user_id=str(user_id or "unknown"),
            source=source,
        )
        preserved_versions = self.inventory.preserve_ble_advertised_versions_from(previous_inventory)
        preserved_gate_settings = self.inventory.preserve_gate_settings_from(previous_inventory)
        preserved_indicator_led_settings = self.inventory.preserve_indicator_led_settings_from(previous_inventory)
        preserved_sensor_config_settings = self.inventory.preserve_sensor_config_settings_from(previous_inventory)
        preserved_plug_config_settings = self.inventory.preserve_plug_config_settings_from(previous_inventory)
        self.gateway_identity = self.inventory.gateway
        preserved_parts = []
        if preserved_versions:
            preserved_parts.append(f"preserved {preserved_versions} BLE firmware version(s)")
        if preserved_gate_settings:
            preserved_parts.append(f"preserved {preserved_gate_settings} gate setting record(s)")
        if preserved_indicator_led_settings:
            preserved_parts.append(f"preserved {preserved_indicator_led_settings} indicator LED setting record(s)")
        if preserved_sensor_config_settings:
            preserved_parts.append(f"preserved {preserved_sensor_config_settings} sensor config setting record(s)")
        if preserved_plug_config_settings:
            preserved_parts.append(f"preserved {preserved_plug_config_settings} plug config setting record(s)")
        self._log_debug("Built inventory from %s%s", source, f"; {'; '.join(preserved_parts)}" if preserved_parts else "")
        show = self.verbose if show_devices is None else bool(show_devices)
        if show:
            debug_dump = (
                self.inventory.debug_lines_verbose()
                if self.verbose
                else self.inventory.debug_lines()
            )
            self._log_multiline_debug(f"Inventory dump for {source}", debug_dump)
        else:
            self._log_debug(
                "Inventory summary: home=%s devices=%s netID=%s",
                self.inventory.home_id,
                len(self.inventory.devices_by_id),
                self.inventory.net_id,
            )
            if source.startswith("cloud"):
                self._log_multiline_debug(
                    f"Inventory device summary for {source}",
                    self.inventory.debug_lines(),
                )

    def _fetch_home_object(self, homeid: Optional[str], sessiontoken: Optional[str]) -> Optional[Dict[str, Any]]:
        """Fetch Home object from cloud for metadata fallback only."""
        if not homeid or homeid == "unknown" or not sessiontoken or sessiontoken == "unknown":
            return None

        try:
            import httpx

            headers = {
                "x-parse-session-token": sessiontoken,
                "x-parse-application-id": APPLICATION_ID,
                "x-parse-client-key": CLIENT_KEY,
            }
            body = {
                "where": json.dumps({"objectId": homeid}),
            }

            response = httpx.get(API_URL["home"], params=body, headers=headers)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and isinstance(data.get("results"), list) and data["results"]:
                home_obj = data["results"][0]
                self._dump_structure_json("cloud_home_object.json", home_obj)
                if isinstance(home_obj, dict):
                    self._dump_structure_json("cloud_home_onlineList.json", home_obj.get("onlineList") or {})
                    self._dump_structure_json("cloud_home_deviceList.json", home_obj.get("deviceList") or [])
                    self._dump_structure_json("cloud_home_groupList.json", home_obj.get("groupList") or [])
                    self._dump_structure_json("cloud_home_sceneList.json", home_obj.get("sceneList") or [])
                return home_obj
        except Exception as e:
            self._log_debug("Could not fetch Home fallback object: %s", e)

        return None

    def _sync_inventory_53216_once(
        self,
        hub_ip: str,
        net_id_int: int,
        mesh_net2_int: int,
        timeout: float = 5.0,
        selected: int = 127,
        force_ts: Optional[int] = None,
        force_nonce: Optional[int] = None,
        force_ea_b64: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run one-shot EA/EB exchange on port 53216 and return decrypted inventory JSON."""
        debug_mode = self._debug_enabled()
        ts_float = time.time()
        ts = int(force_ts) if force_ts is not None else int(ts_float)
        nonce = int(force_nonce) if force_nonce is not None else self._derive_sync_53216_nonce(ts, mesh_net2_int)
        key_hex = self._derive_sync_53216_key(ts, net_id_int).hex()
        selected_int = max(0, int(selected))
        if force_ea_b64:
            ea_payload_b64 = "".join(str(force_ea_b64).split())
            ea_wire = f"ea{len(ea_payload_b64):08x}{nonce:08x}{ea_payload_b64}".encode("ascii")
        else:
            plaintext = json.dumps(
                {"get": {"selected": selected_int}},
                separators=(",", ":"),
            ).encode("utf-8")
            ea_wire = self._build_sync_53216_ea_for_payload(ts, net_id_int, nonce, plaintext)
        ea_wire_ascii = ea_wire.decode("ascii", errors="replace")

        if debug_mode:
            debug_lines = [
                f"unix_seconds_float: {ts_float:.6f}",
                f"unix_seconds: {ts}",
                f"utc_time: {datetime.fromtimestamp(ts, timezone.utc).isoformat()}",
                f"nonce: 0x{nonce:08x}",
                "ts_source: forced" if force_ts is not None else "ts_source: current_time",
                "nonce_source: forced" if force_nonce is not None else "nonce_source: derived_from_ts",
                "ea_payload_source: forced" if force_ea_b64 else "ea_payload_source: generated",
                f"selected: {selected_int}",
                f"key_hex(ts): {key_hex}",
                f"EA bytes: {len(ea_wire)}",
                f"EA wire: {ea_wire_ascii}",
            ]
            self._log_multiline_debug("53216 EA request parameters", debug_lines)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((hub_ip, TCP_SYNC_PORT))
            send_ts_float = time.time()
            sock.sendall(ea_wire)
            recv_start_ts_float = time.time()
            eb_raw, eb_parts, eb_frame_infos = self._read_sync_53216_eb_frames(sock, timeout=timeout)
            recv_end_ts_float = time.time()

        send_ts = int(send_ts_float)
        recv_start_ts = int(recv_start_ts_float)
        recv_end_ts = int(recv_end_ts_float)
        if debug_mode:
            frame_lines = [
                f"send_time_float: {send_ts_float:.6f} (sec={send_ts})",
                f"recv_start_float: {recv_start_ts_float:.6f} (sec={recv_start_ts})",
                f"recv_end_float: {recv_end_ts_float:.6f} (sec={recv_end_ts})",
                f"ts_delta_send_minus_build: {send_ts - ts}",
                f"EB frame parts: {len(eb_parts)}",
                f"EB raw bytes: {len(eb_raw)}",
                f"EB raw ascii: {eb_raw.decode('ascii', errors='replace')}",
                f"EB raw hex: {eb_raw.hex()}",
            ]
            frame_lines.extend(
                "EB frame {idx}: marker={marker} len_hex={length_hex} len={payload_len} in_bounds={in_bounds}".format(
                    idx=i,
                    marker=info.get("marker"),
                    length_hex=info.get("length_hex"),
                    payload_len=info.get("payload_len"),
                    in_bounds=info.get("in_bounds"),
                )
                for i, info in enumerate(eb_frame_infos)
            )
            self._log_multiline_debug("53216 EB response trace", frame_lines)

        attempts: List[Tuple[str, str]] = []
        if len(eb_parts) > 1:
            attempts.append(("concat", "".join(eb_parts)))
        for i, part in enumerate(eb_parts):
            attempts.append((f"part[{i}]", part))

        if debug_mode:
            for label, payload_b64 in attempts:
                self._log_debug("53216 EB payload %s b64 (%s): %s", label, len(payload_b64), payload_b64)

        debug_bundle = {
            "hub_ip": hub_ip,
            "netid": net_id_int,
            "ts_build_float": ts_float,
            "ts_build_int": ts,
            "ts_send_float": send_ts_float,
            "ts_send_int": send_ts,
            "ts_recv_start_float": recv_start_ts_float,
            "ts_recv_start_int": recv_start_ts,
            "ts_recv_end_float": recv_end_ts_float,
            "ts_recv_end_int": recv_end_ts,
            "nonce_hex": f"0x{nonce:08x}",
            "force_ts": force_ts,
            "force_nonce": force_nonce,
            "force_ea_b64": force_ea_b64,
            "selected": selected_int,
            "key_hex": key_hex,
            "ea_wire": ea_wire_ascii,
            "eb_raw_ascii": eb_raw.decode("ascii", errors="replace"),
            "eb_raw_hex": eb_raw.hex(),
            "eb_frame_infos": eb_frame_infos,
            "eb_payloads": [{"label": label, "b64": payload_b64} for label, payload_b64 in attempts],
        }

        if debug_mode:
            metadata_lines = [
                f"hub_ip: {hub_ip}",
                f"netid: {net_id_int}",
                f"key_hex: {key_hex}",
                f"ts_build: {ts}",
                f"ts_send: {send_ts}",
                f"ts_recv_start: {recv_start_ts}",
                f"ts_recv_end: {recv_end_ts}",
                f"nonce_hex: 0x{nonce:08x}",
                f"selected: {selected_int}",
                f"payload_sources: {', '.join(label for label, _ in attempts)}",
            ]
            self._log_multiline_debug("53216 decrypt attempt metadata", metadata_lines)

        last_err: Optional[Exception] = None
        for label, payload_b64 in attempts:
            try:
                data, mode = self._decrypt_sync_53216_eb_payload(ts, net_id_int, payload_b64)
                if self._is_valid_53216_inventory_payload(data):
                    if debug_mode:
                        self._log_debug("53216 decrypt succeeded with source=%s mode=%s", label, mode)
                    self._dump_structure_json("hub_53216_decrypted_root.json", data)
                    payload_obj = self._extract_53216_inventory_payload(data)
                    if payload_obj is not None:
                        self._dump_structure_json("hub_53216_inventory_payload.json", payload_obj)
                        self._dump_structure_json("hub_53216_deviceList.json", payload_obj.get("deviceList") or [])
                        self._dump_structure_json("hub_53216_groupList.json", payload_obj.get("groupList") or [])
                        self._dump_structure_json("hub_53216_sceneList.json", payload_obj.get("sceneList") or [])
                    if debug_mode:
                        with open("sync53216_debug_last.json", "w", encoding="utf-8") as fp:
                            json.dump(debug_bundle, fp, ensure_ascii=False, indent=2)
                        self._log_debug("Wrote 53216 debug bundle: sync53216_debug_last.json")
                    return {
                        "status": "success",
                        "hub_ip": hub_ip,
                        "netid": str(net_id_int),
                        "unix_seconds": ts,
                        "nonce_hex": f"0x{nonce:08x}",
                        "eb_source": label,
                        "decode_mode": mode,
                        "data": data,
                    }

                raise PixieAuthError(
                    "Decrypted payload does not match expected inventory schema "
                    f"(source={label}, mode={mode}, type={type(data).__name__}, value={data!r})"
                )
            except Exception as exc:
                last_err = exc

        candidate_ts: List[Tuple[str, int]] = []
        seen_ts: set[int] = set()

        def _add_ts(label: str, ts_val: int) -> None:
            if ts_val in seen_ts:
                return
            seen_ts.add(ts_val)
            candidate_ts.append((label, ts_val))

        _add_ts("build", ts)
        _add_ts("send", send_ts)
        _add_ts("recv_start", recv_start_ts)
        _add_ts("recv_end", recv_end_ts)
        for offset in (-2, -1, 1, 2):
            _add_ts(f"build_{offset:+d}", ts + offset)
        candidate_results: List[Dict[str, Any]] = []
        for ts_label, ts_val in candidate_ts:
            for label, payload_b64 in attempts:
                rec: Dict[str, Any] = {
                    "ts_label": ts_label,
                    "ts": ts_val,
                    "source": label,
                    "ok": False,
                }
                try:
                    data, mode = self._decrypt_sync_53216_eb_payload(ts_val, net_id_int, payload_b64)
                    rec["ok"] = True
                    rec["mode"] = mode
                    rec["is_inventory_shape"] = self._is_valid_53216_inventory_payload(data)
                    rec["data_type"] = type(data).__name__
                    if isinstance(data, dict):
                        rec["dict_keys"] = list(data.keys())[:8]
                except Exception as exc:
                    rec["error"] = str(exc)
                candidate_results.append(rec)

        debug_bundle["candidate_decryptions"] = candidate_results
        hit_count = sum(1 for x in candidate_results if x.get("ok"))
        if debug_mode:
            self._log_debug("53216 candidate decryptions: %s/%s successful JSON parses", hit_count, len(candidate_results))

            candidate_lines = []
            for rec in candidate_results:
                base = (
                    f"ts_label={rec.get('ts_label')} ts={rec.get('ts')} "
                    f"source={rec.get('source')} ok={rec.get('ok')}"
                )
                if rec.get("ok"):
                    extra = (
                        f" mode={rec.get('mode')} inventory_shape={rec.get('is_inventory_shape')}"
                        f" data_type={rec.get('data_type')}"
                    )
                    if rec.get("dict_keys"):
                        extra += f" dict_keys={rec.get('dict_keys')}"
                    candidate_lines.append(base + extra)
                else:
                    candidate_lines.append(base + f" error={rec.get('error')}")
            self._log_multiline_debug("53216 candidate decryption matrix", candidate_lines)

        if debug_mode:
            with open("sync53216_debug_last.json", "w", encoding="utf-8") as fp:
                json.dump(debug_bundle, fp, ensure_ascii=False, indent=2)
            self._log_debug("Wrote 53216 debug bundle: sync53216_debug_last.json")
        raise PixieAuthError(f"Failed to decrypt EB payload from 53216: {last_err}")

    def send_53216_json(
        self,
        hub_ip: str,
        net_id_int: int,
        mesh_net2_int: int,
        payload: Dict[str, Any],
        timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Send one captured 53216 management JSON payload and return decrypted response."""
        ts_float = time.time()
        ts = int(ts_float)
        nonce = self._derive_sync_53216_nonce(ts, mesh_net2_int)
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ea_wire = self._build_sync_53216_ea_for_payload(ts, net_id_int, nonce, plaintext)
        if self._debug_enabled():
            self._log_debug(
                "53216 management request payload=%s wire_len=%s nonce=0x%08x",
                plaintext.decode("utf-8", errors="replace"),
                len(ea_wire),
                nonce,
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((hub_ip, TCP_SYNC_PORT))
            sock.sendall(ea_wire)
            eb_raw, eb_parts, eb_frame_infos = self._read_sync_53216_eb_frames(sock, timeout=timeout)

        if self._debug_enabled():
            self._log_debug(
                "53216 management response raw_len=%s parts=%s frames=%s",
                len(eb_raw),
                len(eb_parts),
                eb_frame_infos,
            )
        last_err: Exception | None = None
        timestamp_candidates = (ts, ts - 1, ts + 1, ts - 2, ts + 2)
        attempts: List[Tuple[str, str]] = []
        if len(eb_parts) > 1:
            attempts.append(("concat", "".join(eb_parts)))
        for i, part in enumerate(eb_parts):
            attempts.append((f"part[{i}]", part))
        for label, payload_b64 in attempts:
            for ts_candidate in timestamp_candidates:
                try:
                    data, mode = self._decrypt_sync_53216_eb_payload(ts_candidate, int(net_id_int), payload_b64)
                    if self._debug_enabled():
                        self._log_debug(
                            "53216 management decrypt succeeded source=%s mode=%s ts=%s data=%s",
                            label,
                            mode,
                            ts_candidate,
                            data,
                        )
                    return data if isinstance(data, dict) else {"data": data}
                except Exception as err:
                    last_err = err
        raise PixieAuthError(f"Failed to decrypt 53216 management response: {last_err}")

    def _find_meshnet_record(self, api_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Find the best record in LiveGroup response with meshNet/meshNet2/netID."""

        def _rec_search(item):
            if isinstance(item, dict):
                if item.get('meshNet') is not None or item.get('meshNet2') is not None or item.get('netID') is not None:
                    return item
                if item.get('MeshNet') is not None or item.get('MeshNet2') is not None:
                    return item
                # Search nested objects and arrays
                for k, v in item.items():
                    found = _rec_search(v)
                    if found is not None:
                        return found
            elif isinstance(item, list):
                for subitem in item:
                    found = _rec_search(subitem)
                    if found is not None:
                        return found
            elif isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    found = _rec_search(parsed)
                    if found is not None:
                        return found
                except json.JSONDecodeError:
                    pass
                # Try to decode as hex envelope
                try:
                    import binascii
                    bytes_data = binascii.unhexlify(item)
                    decoded = PixieEnvelope.decode(bytes_data, None)  # No key for search
                    if decoded:
                        found = _rec_search(decoded)
                        if found is not None:
                            return found
                except:
                    pass
            return None

        # Top-level search
        found = _rec_search(api_data)
        if found is not None:
            return found

        # If this is not enough, inspect results as fallback
        results = api_data.get('results')
        if isinstance(results, list):
            for index, entry in enumerate(results):
                if not isinstance(entry, dict):
                    continue
                found = _rec_search(entry)
                if found is not None:
                    return found

        return None

    @staticmethod
    def _check_cloud_login_response(response: Any) -> None:
        """Raise a typed error when the cloud reports bad credentials."""
        if response.status_code == 403:
            raise PixieInvalidCredentialsError("Invalid Pixie username/password")

        if response.status_code in (400, 401, 404):
            try:
                error_payload = response.json()
            except Exception:
                error_payload = {}
            error_code = error_payload.get("code") if isinstance(error_payload, dict) else None
            error_text = str(error_payload.get("error") or "") if isinstance(error_payload, dict) else ""
            if error_code == 101 or "invalid username/password" in error_text.lower():
                raise PixieInvalidCredentialsError("Invalid Pixie username/password")

    def fetch_cloud_home_list(self, username: str, password: str) -> CloudHomeList:
        """Log in to Pixie cloud and return every Home visible to this account."""
        import httpx

        headers = {
            "x-parse-application-id": APPLICATION_ID,
            "x-parse-installation-id": "cli-installation",
            "x-parse-client-key": CLIENT_KEY,
            "x-parse-revocable-session": "1",
        }
        response = httpx.post(API_URL["login"], json={"username": username, "password": password}, headers=headers)
        self._check_cloud_login_response(response)
        response.raise_for_status()

        login_data = response.json()
        user_id = str(login_data.get("objectId") or "unknown")
        session_token = str(login_data.get("sessionToken") or "unknown")
        cur_home = login_data.get("curHome") if isinstance(login_data.get("curHome"), dict) else {}
        current_home_id = str(cur_home.get("objectId")) if cur_home.get("objectId") is not None else None

        home_headers = {
            "x-parse-session-token": session_token,
            "x-parse-application-id": APPLICATION_ID,
            "x-parse-client-key": CLIENT_KEY,
        }
        homes: List[Dict[str, Any]] = []
        skip = 0
        limit = 100
        while True:
            home_response = httpx.get(
                API_URL["home"],
                params={"where": "{}", "skip": skip, "limit": limit},
                headers=home_headers,
            )
            home_response.raise_for_status()
            batch = home_response.json().get("results", [])
            if not isinstance(batch, list):
                break
            homes.extend(home for home in batch if isinstance(home, dict))
            if len(batch) < limit:
                break
            skip += limit

        self._log_debug(
            "Cloud login returned %s home(s), current_home=%s",
            len(homes),
            current_home_id,
        )
        return CloudHomeList(
            user_id=user_id,
            session_token=session_token,
            current_home_id=current_home_id,
            homes=tuple(homes),
        )

    async def async_fetch_cloud_home_list(self, username: str, password: str) -> CloudHomeList:
        """Async wrapper for visible Pixie Home listing."""
        return await asyncio.to_thread(self.fetch_cloud_home_list, username, password)

    def _home_object_to_login_config(
        self,
        home_obj: Dict[str, Any],
        *,
        user_id: str,
        session_token: str,
        include_inventory_seed: bool,
    ) -> Dict[str, Any]:
        """Build the legacy login config shape from a selected Home object."""
        homeid = str(home_obj.get("objectId") or "unknown")
        home_name = str(home_obj.get("name") or "unknown")
        meshnet = str(home_obj.get("meshNet")) if home_obj.get("meshNet") is not None else homeid
        meshnet2 = str(home_obj.get("meshNet2")) if home_obj.get("meshNet2") is not None else "unknown"
        netid = str(home_obj.get("netID")) if home_obj.get("netID") is not None else (self.netid_seed or "unknown")

        self._cached_cloud_home_obj = dict(home_obj)
        if include_inventory_seed:
            try:
                self._set_inventory_from_home_object(home_obj, str(user_id), source="cloud_seed")
            except Exception as inv_err:
                self._log_debug("Could not build inventory from Home payload: %s", inv_err)

        return {
            "netid": netid,
            "meshnet": meshnet,
            "meshnet2": meshnet2,
            "homeid": homeid,
            "home_name": home_name,
            "userid": str(user_id or "unknown"),
            "sessiontoken": str(session_token or "unknown"),
        }

    @staticmethod
    def _select_home_object(home_list: CloudHomeList, selected_home_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Choose a Home object by id, current home, or first available home."""
        homes = list(home_list.homes)
        if selected_home_id:
            for home in homes:
                if str(home.get("objectId") or "") == str(selected_home_id):
                    return home
            return None
        if home_list.current_home_id:
            for home in homes:
                if str(home.get("objectId") or "") == str(home_list.current_home_id):
                    return home
        return homes[0] if homes else None


    def _fetch_login_data(
        self,
        username: str,
        password: str,
        include_inventory_seed: bool = True,
        selected_home_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Fetch netID, meshNet from Pixie Plus cloud API.
        
        Based on pixiepluslogin.py login() and livegroup_get_objectID() functions.
        """
        self._log_debug("Calling cloud API endpoints: %s and %s", API_URL['login'], API_URL['livegroup'])

        # Default values
        netid = self.netid_seed or "unknown"
        meshnet = "unknown"
        meshnet2 = "unknown"  # Initialize meshnet2 to prevent NameError
        homeid = "unknown"
        home_name = "unknown"
        userid = "unknown"
        sessiontoken = "unknown"

        try:
            import httpx

            home_list = self.fetch_cloud_home_list(username, password)
            userid = home_list.user_id
            sessiontoken = home_list.session_token
            selected_home = self._select_home_object(home_list, selected_home_id)
            if selected_home is not None:
                selected_config = self._home_object_to_login_config(
                    selected_home,
                    user_id=userid,
                    session_token=sessiontoken,
                    include_inventory_seed=include_inventory_seed,
                )
                netid = selected_config["netid"]
                meshnet = selected_config["meshnet"]
                meshnet2 = selected_config["meshnet2"]
                homeid = selected_config["homeid"]
                home_name = selected_config["home_name"]

            self._log_debug(
                "Cloud login succeeded: user=%s home=%s sessionToken=%s meshNet=%s",
                userid,
                homeid,
                '***' if sessiontoken else None,
                meshnet,
            )

            # Fallback: older accounts may still leave home metadata unresolved.
            if homeid is None or str(homeid) in ("", "unknown", "None"):
                try:
                    home_list_headers = {
                        "x-parse-session-token": sessiontoken,
                        "x-parse-application-id": APPLICATION_ID,
                        "x-parse-client-key": CLIENT_KEY,
                    }
                    home_list_resp = httpx.get(
                        API_URL["home"], params={"limit": 10}, headers=home_list_headers
                    )
                    home_list_resp.raise_for_status()
                    home_results = home_list_resp.json().get("results", [])
                    if home_results:
                        home0 = home_results[0]
                        homeid = home0.get("objectId", homeid)
                        if home0.get("name"):
                            home_name = str(home0.get("name"))
                        if home0.get("meshNet") is not None:
                            meshnet = str(home0.get("meshNet"))
                        if home0.get("meshNet2") is not None:
                            meshnet2 = str(home0.get("meshNet2"))
                        if home0.get("netID") is not None:
                            netid = str(home0.get("netID"))
                        self._log_debug(
                            "Resolved home via Home-class fallback: home=%s meshNet=%s meshNet2=%s netID=%s",
                            homeid, meshnet, meshnet2, netid,
                        )
                    else:
                        self._log_warning(
                            "Home-class fallback returned no homes for this account"
                        )
                except Exception as exc:
                    self._log_debug("Home-class fallback failed: %s", exc)

            # Try to get meshNet from LiveGroup
            try:
                headers = {
                    "x-parse-session-token": sessiontoken,
                    "x-parse-application-id": APPLICATION_ID,
                    "x-parse-client-key": CLIENT_KEY,
                }

                body = {
                    "where": json.dumps({"GroupID": {"$regex": homeid + "$", "$options": "i"}}),
                    "limit": 2,
                }

                response = httpx.get(API_URL["livegroup"], params=body, headers=headers)
                response.raise_for_status()
                data = response.json()

                record = self._find_meshnet_record(data)
                if record is not None:
                    meshnet_value = record.get('meshNet') or record.get('MeshNet')
                    meshnet2_value = record.get('meshNet2') or record.get('MeshNet2')

                    if meshnet_value:
                        meshnet = str(meshnet_value)
                        self._log_debug("Updated meshNet from LiveGroup: %s", meshnet)
                    if meshnet2_value:
                        meshnet2 = str(meshnet2_value)
                        self._log_debug("Updated meshNet2 from LiveGroup: %s", meshnet2)

            except Exception as e:
                self._log_debug("Could not fetch mesh data from LiveGroup: %s", e)

            # Try to get meshNet/netID from Home API. Inventory build is optional.
            try:
                home_obj = self._fetch_home_object(homeid=str(homeid), sessiontoken=str(sessiontoken))
                if home_obj:
                    self._cached_cloud_home_obj = dict(home_obj)
                    if include_inventory_seed:
                        try:
                            self._set_inventory_from_home_object(home_obj, str(userid), source="cloud_seed")
                        except Exception as inv_err:
                            self._log_debug("Could not build inventory from Home payload: %s", inv_err)

                    meshnet_home = home_obj.get('meshNet')
                    meshnet2_home = home_obj.get('meshNet2')
                    netid_home = home_obj.get('netID')
                    home_name_value = home_obj.get('name')
                    if meshnet_home:
                        meshnet = str(meshnet_home)
                        self._log_debug("Got meshNet from Home API: %s", meshnet)
                    if meshnet2_home:
                        meshnet2 = str(meshnet2_home)
                        self._log_debug("Got meshNet2 from Home API: %s", meshnet2)
                    if netid_home:
                        netid = str(netid_home)
                        self._log_debug("Got netID from Home API: %s", netid)
                    if home_name_value:
                        home_name = str(home_name_value)
                        self._log_debug("Got home name from Home API: %s", home_name)

            except Exception as e:
                self._log_debug("Could not fetch metadata from Home API: %s", e)

        except httpx.HTTPStatusError as e:
            self._log_warning("Cloud API HTTP error: %s - %s", e.response.status_code, e.response.text[:100])
            try:
                error_payload = e.response.json()
            except Exception:
                error_payload = {}
            error_code = error_payload.get("code") if isinstance(error_payload, dict) else None
            error_text = str(error_payload.get("error") or "") if isinstance(error_payload, dict) else ""
            if error_code == 101 or "invalid username/password" in error_text.lower():
                raise PixieInvalidCredentialsError("Invalid Pixie username/password") from e
        except Exception as e:
            if isinstance(e, PixieInvalidCredentialsError):
                raise
            self._log_warning("Could not fetch login data from cloud API: %s", e)

        return {
            'netid': netid,
            'meshnet': meshnet,
            'meshnet2': meshnet2,
            'homeid': homeid,
            'home_name': home_name,
            'userid': userid,
            'sessiontoken': sessiontoken
        }

    def _default_optimistic_brightness_percent(self, rec: Optional[Any]) -> int:
        """Return the brightness HA should predict for commands that retain brightness."""
        if rec and isinstance(rec.runtime.br, int):
            return max(0, min(100, int(rec.runtime.br)))
        return 100

    def resolve_optimistic_update_intent(self, command_kwargs: Dict[str, Any]) -> Optional[PixieOptimisticUpdateIntent]:
        """Resolve HA command kwargs into a transport-neutral optimistic update intent."""
        if not self.inventory:
            return None
        try:
            device_id = int(command_kwargs["command_device_id"])
        except (KeyError, TypeError, ValueError):
            return None

        rec = self.inventory.devices_by_id.get(device_id)
        if rec is None:
            return None

        command_state = command_kwargs.get("command_state")
        command_brightness = command_kwargs.get("command_brightness")
        command_color_rgb = command_kwargs.get("command_color_rgb")
        command_color_temp_cct = command_kwargs.get("command_color_temp_cct")
        command_effect = command_kwargs.get("command_effect")
        command_mode = command_kwargs.get("command_mode")
        command_cover_action = command_kwargs.get("command_cover_action")
        command_cover_action_map = command_kwargs.get("command_cover_action_map")
        command_cover_tilt_action_map = command_kwargs.get("command_cover_tilt_action_map")
        command_timer_action = command_kwargs.get("command_timer_action")
        command_timer_duration = command_kwargs.get("command_timer_duration")
        command_power_meter_action = command_kwargs.get("command_power_meter_action")
        command_sensor_param = command_kwargs.get("command_sensor_param")
        command_sensor_param_value = command_kwargs.get("command_sensor_param_value")
        command_gate_param = command_kwargs.get("command_gate_param")
        command_gate_param_value = command_kwargs.get("command_gate_param_value")
        command_indicator_led_action = command_kwargs.get("command_indicator_led_action")
        command_indicator_led_on = command_kwargs.get("command_indicator_led_on")
        command_indicator_led_off = command_kwargs.get("command_indicator_led_off")
        command_target = command_kwargs.get("command_target")

        if command_indicator_led_action == "set":
            return PixieOptimisticUpdateIntent(
                device_id=device_id,
                target="indicator_led_settings",
                value={
                    "on": command_indicator_led_on,
                    "off": command_indicator_led_off,
                },
            )

        if command_gate_param is not None:
            target = str(command_gate_param)
            if target == "signal_width" and command_gate_param_value is not None:
                return PixieOptimisticUpdateIntent(device_id=device_id, target="gate_signal_width", value=int(command_gate_param_value))
            if target in {"door_open_duration", "door_close_duration"} and command_gate_param_value is not None:
                return PixieOptimisticUpdateIntent(
                    device_id=device_id,
                    target=f"gate_{target}",
                    value={
                        "door": int(command_kwargs.get("command_gate_door") or 0),
                        "seconds": int(command_gate_param_value),
                    },
                )
            if target == "refresh_settings":
                return None

        if command_sensor_param is not None and command_sensor_param_value is not None:
            target = str(command_sensor_param)
            if target in {"hold_time", "brightness_threshold", "motion_sensitivity"}:
                return PixieOptimisticUpdateIntent(
                    device_id=device_id,
                    target=target,
                    value=int(command_sensor_param_value),
                )
            return None

        if command_timer_action == "restart":
            return PixieOptimisticUpdateIntent(device_id=device_id, target="timer_restart", value=True)
        if command_timer_action == "override":
            return PixieOptimisticUpdateIntent(device_id=device_id, target="timer_override", value=True)
        if command_timer_action == "set_duration" and command_timer_duration is not None:
            return PixieOptimisticUpdateIntent(
                device_id=device_id,
                target="timer_duration",
                value=int(command_timer_duration),
            )
        if command_timer_action == "poll" and rec.capabilities.supports_timer:
            return PixieOptimisticUpdateIntent(device_id=device_id, target="timer_poll_stamp")
        if command_power_meter_action == "poll" and rec.capabilities.supports_power_metering:
            return PixieOptimisticUpdateIntent(device_id=device_id, target="power_meter_poll_stamp")

        if command_mode is not None:
            mode_value = int(command_mode)
            target = "timer_mode" if rec.capabilities.supports_timer and mode_value in (1, 2) else "mode"
            return PixieOptimisticUpdateIntent(device_id=device_id, target=target, value=mode_value)

        if command_effect is not None:
            brightness = (
                int(command_brightness)
                if command_brightness is not None
                else self._default_optimistic_brightness_percent(rec)
            )
            effect_name = str(command_effect).strip().lower()
            return PixieOptimisticUpdateIntent(
                device_id=device_id,
                target="effect",
                value=effect_name,
                brightness_level=brightness,
                effect_name=effect_name,
                effect_speed=4,
            )

        if command_color_rgb is not None:
            brightness = (
                int(command_brightness)
                if command_brightness is not None
                else self._default_optimistic_brightness_percent(rec)
            )
            if brightness == 0:
                brightness = 100
            rgb = tuple(int(value) for value in command_color_rgb)
            return PixieOptimisticUpdateIntent(
                device_id=device_id,
                target="color",
                value=rgb,
                brightness_level=brightness,
                rgb_color=rgb,
            )

        if command_color_temp_cct is not None:
            brightness = (
                int(command_brightness)
                if command_brightness is not None
                else self._default_optimistic_brightness_percent(rec)
            )
            if brightness == 0:
                brightness = 100
            return PixieOptimisticUpdateIntent(
                device_id=device_id,
                target="color_temp",
                value=int(command_color_temp_cct),
                brightness_level=brightness,
            )

        if command_brightness is not None:
            return PixieOptimisticUpdateIntent(
                device_id=device_id,
                target="brightness",
                value=int(command_brightness),
            )

        if command_cover_action is not None:
            normalized_cover_action = str(command_cover_action).strip().lower().replace("-", "_")
            cover_button_position = resolve_cover_command_position(
                normalized_cover_action,
                action_mapping=command_cover_action_map,
                tilt_mapping=command_cover_tilt_action_map,
            )
            return PixieOptimisticUpdateIntent(
                device_id=device_id,
                target="cover",
                value=normalized_cover_action,
                cover_button_position=cover_button_position,
            )

        if command_state is not None:
            state = bool(command_state)
            if rec.capabilities.supports_timer:
                return PixieOptimisticUpdateIntent(device_id=device_id, target="timer_relay", value=state)
            if rec.capabilities.supports_contact_sensor:
                return PixieOptimisticUpdateIntent(device_id=device_id, target="arm", value=state)
            effective_target = self._resolve_command_target_for_device(device_id, command_target)
            if rec.capabilities.supports_sensor and effective_target == "relay":
                return PixieOptimisticUpdateIntent(device_id=device_id, target="relay", value=state)
            return PixieOptimisticUpdateIntent(device_id=device_id, target=effective_target, value=state)

        return None

    def build_core_command_plan(self, command_kwargs: Dict[str, Any]) -> PixieCoreCommandPlan:
        """Build the transport-neutral Pixie core command packet sequence."""
        if not self.inventory:
            raise PixieAuthError("No Pixie inventory is available")
        try:
            device_id = int(command_kwargs["command_device_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PixieAuthError("Missing or invalid command_device_id") from exc

        rec = self.inventory.devices_by_id.get(device_id)
        if rec is None:
            raise PixieAuthError(f"Unknown Pixie device id: {device_id}")

        command_state = command_kwargs.get("command_state")
        command_brightness = command_kwargs.get("command_brightness")
        command_color_rgb = command_kwargs.get("command_color_rgb")
        command_color_temp_cct = command_kwargs.get("command_color_temp_cct")
        command_effect = command_kwargs.get("command_effect")
        command_mode = command_kwargs.get("command_mode")
        command_cover_action = command_kwargs.get("command_cover_action")
        command_cover_action_map = command_kwargs.get("command_cover_action_map")
        command_cover_tilt_action_map = command_kwargs.get("command_cover_tilt_action_map")
        command_timer_action = command_kwargs.get("command_timer_action")
        command_timer_duration = command_kwargs.get("command_timer_duration")
        command_power_meter_action = command_kwargs.get("command_power_meter_action")
        command_sensor_param = command_kwargs.get("command_sensor_param")
        command_sensor_param_value = command_kwargs.get("command_sensor_param_value")
        command_gate_param = command_kwargs.get("command_gate_param")
        command_gate_param_value = command_kwargs.get("command_gate_param_value")
        command_gate_door = command_kwargs.get("command_gate_door")
        command_indicator_led_action = command_kwargs.get("command_indicator_led_action")
        command_indicator_led_on = command_kwargs.get("command_indicator_led_on")
        command_indicator_led_off = command_kwargs.get("command_indicator_led_off")
        command_target = command_kwargs.get("command_target")
        command_raw_hexes = command_kwargs.get("command_raw_hexes")
        command_raw_target = str(command_kwargs.get("command_raw_target") or "raw")

        is_cover_cmd = command_cover_action is not None
        is_effect_cmd = command_effect is not None
        is_color_cmd = command_color_rgb is not None
        is_color_temp_cmd = command_color_temp_cct is not None
        is_brightness_cmd = (
            command_brightness is not None
            and not is_color_cmd
            and not is_color_temp_cmd
            and not is_effect_cmd
            and not is_cover_cmd
        )
        is_mode_cmd = command_mode is not None
        optimistic_intent = self.resolve_optimistic_update_intent(command_kwargs)

        if is_color_cmd and not rec.capabilities.supports_color:
            raise PixieAuthError(f"Model {rec.model_no} does not support color")
        if is_color_temp_cmd and not rec.capabilities.supports_color_temp:
            raise PixieAuthError(f"Model {rec.model_no} does not support color temperature")
        if is_cover_cmd and not rec.capabilities.supports_cover and not rec.capabilities.supports_gate:
            raise PixieAuthError(f"Model {rec.model_no} does not support cover commands")
        if is_mode_cmd and not rec.capabilities.supports_sensor and not rec.capabilities.supports_timer:
            raise PixieAuthError(f"Model {rec.model_no} does not support mode commands")
        if is_mode_cmd and rec.capabilities.supports_sensor:
            allowed_sensor_modes = get_supported_sensor_mode_values_for_capabilities(rec.capabilities)
            requested_sensor_mode = int(command_mode)
            if requested_sensor_mode not in allowed_sensor_modes:
                raise PixieAuthError(
                    f"Mode {requested_sensor_mode} not allowed for model {rec.model_no}: {allowed_sensor_modes}"
                )
        if is_effect_cmd:
            allowed_effects = rec.capabilities.effect_names
            if not allowed_effects:
                raise PixieAuthError(f"Model {rec.model_no} does not support effects")
            if not rec.capabilities.effect_command_encoding:
                raise PixieAuthError(f"Model {rec.model_no} supports effects but has no effect command encoding")
            if str(command_effect).strip().lower() not in allowed_effects:
                raise PixieAuthError(f"Effect '{command_effect}' not allowed for model {rec.model_no}: {allowed_effects}")

        if command_indicator_led_action is not None:
            action = str(command_indicator_led_action)
            if action not in {"poll", "set"}:
                raise PixieAuthError(f"Unsupported indicator LED action: {action}")
            if action == "poll":
                if not rec.capabilities.supports_switch_indicator_led and not supports_plug_led_settings(rec.capabilities):
                    raise PixieAuthError(f"Model {rec.model_no} does not support LED settings")
            elif not rec.capabilities.supports_switch_indicator_led:
                raise PixieAuthError(f"Model {rec.model_no} does not support switch indicator LED settings")
            if action == "set":
                if command_indicator_led_on is None or command_indicator_led_off is None:
                    raise PixieAuthError("Both indicator LED values are required")
                on_value = int(command_indicator_led_on)
                off_value = int(command_indicator_led_off)
                if on_value not in INDICATOR_LED_ON_VALUES:
                    raise PixieAuthError(f"Unsupported indicator LED on value: {on_value}")
                if off_value not in INDICATOR_LED_OFF_VALUES:
                    raise PixieAuthError(f"Unsupported indicator LED off value: {off_value}")

        def _packet(
            command_hex: str,
            *,
            tcp_repeat: int = 0,
            delay_after: float = 0.0,
            log_message: Optional[str] = None,
            log_args: Tuple[Any, ...] = (),
        ) -> PixieCoreCommandPacket:
            return PixieCoreCommandPacket(
                command_hex=command_hex,
                tcp_repeat=tcp_repeat,
                delay_after=delay_after,
                log_message=log_message,
                log_args=log_args,
            )

        if command_raw_hexes is not None:
            packets: list[PixieCoreCommandPacket] = []
            for index, raw_hex in enumerate(command_raw_hexes):
                raw = "".join(str(raw_hex).split()).lower()
                try:
                    bytes.fromhex(raw)
                except ValueError as exc:
                    raise PixieAuthError(f"Invalid raw Pixie command hex: {raw_hex}") from exc
                packets.append(_packet(
                    raw,
                    tcp_repeat=int(command_kwargs.get("command_raw_repeat", 0) or 0),
                    delay_after=float(command_kwargs.get("command_raw_delay", 0.0) or 0.0),
                    log_message="Sending raw Pixie management command: device_id=%s target=%s hex=%s",
                    log_args=(device_id, command_raw_target, raw),
                ))
            return PixieCoreCommandPlan(
                device_id=device_id,
                target=command_raw_target,
                packets=tuple(packets),
                optimistic_intent=None,
                result={"target": command_raw_target, "device_id": device_id, "packets": len(packets)},
            )

        if command_power_meter_action == "poll":
            if not rec.capabilities.supports_power_metering:
                raise PixieAuthError(f"Model {rec.model_no} does not support power metering")
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="power_meter_poll",
                packets=(
                    _packet(
                        self._build_power_meter_poll_command_hex(device_id, 0x02),
                        tcp_repeat=1,
                        delay_after=0.2,
                        log_message="Sending power meter live poll command: device_id=%s opcode=ff6b69 subtype=0x02",
                        log_args=(device_id,),
                    ),
                    _packet(
                        self._build_power_meter_poll_command_hex(device_id, 0x03),
                        tcp_repeat=1,
                        log_message="Sending power meter energy poll command: device_id=%s opcode=ff6b69 subtype=0x03",
                        log_args=(device_id,),
                    ),
                ),
                optimistic_intent=optimistic_intent,
                result={"target": "power_meter_poll", "device_id": device_id},
            )

        if command_indicator_led_action is not None:
            action = str(command_indicator_led_action)
            if action == "poll":
                refresh_target = (
                    "plug_led_settings_refresh"
                    if supports_plug_led_settings(rec.capabilities) and not rec.capabilities.supports_switch_indicator_led
                    else "indicator_led_settings_refresh"
                )
                refresh_label = (
                    "plug LED"
                    if refresh_target == "plug_led_settings_refresh"
                    else "indicator LED"
                )
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target=refresh_target,
                    packets=(_packet(
                        (
                            self._build_plug_led_poll_command_hex(device_id)
                            if refresh_target == "plug_led_settings_refresh"
                            else self._build_indicator_led_poll_command_hex(device_id)
                        ),
                        tcp_repeat=1,
                        log_message="Sending %s settings poll: dev_id=%s opcode=d96b69",
                        log_args=(refresh_label, device_id),
                    ),),
                    optimistic_intent=optimistic_intent,
                    result={"target": refresh_target, "device_id": device_id},
                )
            on_value = int(command_indicator_led_on)
            off_value = int(command_indicator_led_off)
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="indicator_led_settings",
                packets=(_packet(
                    self._build_indicator_led_set_command_hex(device_id, on_value=on_value, off_value=off_value),
                    tcp_repeat=1,
                    log_message="Sending indicator LED settings: dev_id=%s on=%s off=%s opcode=d96b69",
                    log_args=(device_id, on_value, off_value),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "indicator_led_settings", "device_id": device_id},
            )

        if command_gate_param is not None:
            if not rec.capabilities.supports_gate:
                raise PixieAuthError(f"Model {rec.model_no} does not support gate settings")
            param = str(command_gate_param)
            door_index = int(command_gate_door) if command_gate_door is not None else 0
            if door_index < 0 or door_index >= max(1, int(rec.capabilities.gate_doors)):
                raise PixieAuthError(f"Gate door index out of range: {door_index}")
            ka = {"counter_attr": "_timer_command_counter", "minimum_counter": 0x01}
            if param == "refresh_settings":
                packets = [
                    _packet(
                        self._build_shifted_prefix_command_hex(device_id, opcode=b"\xfd\x6b\x69", payload=b"\x10\x00", **ka),
                        tcp_repeat=1,
                        delay_after=0.2,
                        log_message="Gate settings refresh cmd hex: %s",
                    ),
                    _packet(
                        self._build_gate_signal_width_query_command_hex(device_id),
                        tcp_repeat=1,
                        delay_after=0.2,
                        log_message="Gate signal-width query cmd hex: %s",
                    ),
                ]
                for current_door in range(max(1, int(rec.capabilities.gate_doors))):
                    packets.append(_packet(
                        self._build_gate_duration_query_command_hex(device_id, current_door),
                        tcp_repeat=1,
                        delay_after=0.2,
                        log_message="Gate duration query cmd hex: %s",
                    ))
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target="gate_settings_refresh",
                    packets=tuple(packets),
                    optimistic_intent=optimistic_intent,
                    result={"target": "gate_settings_refresh", "device_id": device_id},
                )
            if command_gate_param_value is None:
                raise PixieAuthError(f"Missing value for gate param: {param}")
            if param == "signal_width":
                seconds = max(1, min(5, int(command_gate_param_value)))
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target="gate_signal_width",
                    packets=(_packet(
                        self._build_gate_signal_width_set_command_hex(device_id, seconds),
                        tcp_repeat=1,
                        log_message="Sending gate signal width: dev_id=%s seconds=%s opcode=fb6b69",
                        log_args=(device_id, seconds),
                    ),),
                    optimistic_intent=optimistic_intent,
                    result={"target": "gate_signal_width", "device_id": device_id},
                )
            if param in {"door_open_duration", "door_close_duration"}:
                seconds = max(1, min(60, int(command_gate_param_value)))
                current_open = rec.runtime.door1_open_duration_ms if door_index == 0 else rec.runtime.door2_open_duration_ms
                current_close = rec.runtime.door1_close_duration_ms if door_index == 0 else rec.runtime.door2_close_duration_ms
                if param == "door_open_duration":
                    open_ms = seconds * 1000
                    close_ms = current_close
                else:
                    open_ms = current_open
                    close_ms = seconds * 1000
                if not isinstance(open_ms, int) or not isinstance(close_ms, int):
                    raise PixieAuthError("Gate durations are not known; refresh gate settings first")
                extra1_ms = rec.runtime.door1_extra1_duration_ms if door_index == 0 else rec.runtime.door2_extra1_duration_ms
                extra2_ms = rec.runtime.door1_extra2_duration_ms if door_index == 0 else rec.runtime.door2_extra2_duration_ms
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target=f"gate_{param}",
                    packets=(_packet(
                        self._build_gate_duration_set_command_hex(
                            device_id,
                            door_index,
                            open_duration_ms=open_ms,
                            close_duration_ms=close_ms,
                            extra1_ms=extra1_ms,
                            extra2_ms=extra2_ms,
                        ),
                        log_message="Sending gate duration: dev_id=%s door=%s param=%s seconds=%s opcode=fc6b69",
                        log_args=(device_id, door_index + 1, param, seconds),
                    ),),
                    optimistic_intent=optimistic_intent,
                    result={"target": f"gate_{param}", "device_id": device_id, "door": door_index},
                )
            raise PixieAuthError(f"Unknown gate param: {param}")

        if is_cover_cmd and rec.capabilities.supports_gate:
            door_index = int(command_gate_door) if command_gate_door is not None else 0
            gate_state = rec.runtime.door1_decoded if door_index == 0 else rec.runtime.door2_decoded
            if not gate_can_run_action(gate_state, str(command_cover_action)):
                gate_state_name = gate_state.get("state") if isinstance(gate_state, dict) else "unknown"
                next_action = gate_state.get("next_action") if isinstance(gate_state, dict) else None
                raise PixieAuthError(
                    f"Gate action '{command_cover_action}' is not allowed for door {door_index + 1} while state is {gate_state_name}"
                    + (f" (next action: {next_action})" if next_action else "")
                )
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="gate",
                packets=(_packet(
                    self._build_gate_command_hex(device_id, door_index),
                    tcp_repeat=1,
                    log_message="Sending gate command: dev_id=%s door=%s action=%s opcode=f96b69",
                    log_args=(device_id, door_index, command_cover_action),
                ),),
                optimistic_intent=None,
                result={"target": "gate", "device_id": device_id, "door": door_index},
            )

        if command_timer_action == "poll" and rec.capabilities.supports_sensor:
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="sensor_poll",
                packets=(_packet(
                    self._build_sensor_poll_command_hex(device_id),
                    tcp_repeat=1,
                    log_message="Sending sensor poll: dev_id=%s opcode=f96b69",
                    log_args=(device_id,),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "sensor_poll", "device_id": device_id},
            )

        if command_sensor_param is not None and rec.capabilities.supports_sensor:
            if str(command_sensor_param) == "advanced_settings":
                if not supports_sensor_advanced_settings(rec.capabilities):
                    raise PixieAuthError(f"Model {rec.model_no} does not support advanced sensor settings")
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target="sensor_advanced_settings_refresh",
                    packets=(_packet(
                        self._build_sensor_advanced_poll_command_hex(device_id),
                        tcp_repeat=1,
                        log_message="Sending sensor advanced settings poll: dev_id=%s opcode=d96b69",
                        log_args=(device_id,),
                    ),),
                    optimistic_intent=optimistic_intent,
                    result={"target": "sensor_advanced_settings_refresh", "device_id": device_id},
                )
            param_map = {
                "hold_time": 5,
                "brightness_threshold": 4,
                "motion_sensitivity": 2,
            }
            param_id = param_map.get(str(command_sensor_param))
            if param_id is None:
                raise PixieAuthError(f"Unknown sensor param: {command_sensor_param}")
            if command_sensor_param_value is None:
                raise PixieAuthError(f"Missing value for sensor param: {command_sensor_param}")
            ka = {"counter_attr": "_timer_command_counter", "minimum_counter": 0x01}
            packets = (
                _packet(
                    self._build_shifted_prefix_command_hex(device_id, opcode=b"\xd9\x6b\x69", payload=b"\x77\x00", **ka),
                    tcp_repeat=1,
                    delay_after=0.2,
                    log_message="Edit sequence cmd hex: %s",
                ),
                _packet(
                    self._build_shifted_prefix_command_hex(device_id, opcode=b"\xf9\x6b\x69", payload=b"\x01\x00" + b"\x00" * 8, **ka),
                    tcp_repeat=1,
                    delay_after=0.2,
                    log_message="Edit sequence cmd hex: %s",
                ),
                _packet(
                    self._build_shifted_prefix_command_hex(device_id, opcode=b"\xfd\x6b\x69", payload=b"\x10\x00", **ka),
                    tcp_repeat=1,
                    delay_after=0.2,
                    log_message="Edit sequence cmd hex: %s",
                ),
                _packet(
                    self._build_sensor_param_command_hex(device_id, param_id, int(command_sensor_param_value)),
                    log_message="Sending sensor param: dev_id=%s param=%s(%s) value=%s opcode=d26c69",
                    log_args=(device_id, command_sensor_param, param_id, command_sensor_param_value),
                ),
            )
            return PixieCoreCommandPlan(
                device_id=device_id,
                target=str(command_sensor_param),
                packets=packets,
                optimistic_intent=optimistic_intent,
                result={"target": str(command_sensor_param), "device_id": device_id},
            )

        if command_state is not None and rec.capabilities.supports_contact_sensor:
            effective_target = self._resolve_command_target_for_device(device_id, command_target)
            if effective_target != "arm":
                raise PixieAuthError(f"Unsupported command target for contact sensor: {effective_target}")
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="arm",
                packets=(_packet(
                    self._build_contact_arm_command_hex(device_id, armed=bool(command_state)),
                    log_message="Sending contact sensor arm command: device_id=%s state=%s opcode=ca6b69",
                    log_args=(device_id, "armed" if command_state else "disarmed"),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "arm", "device_id": device_id},
            )

        is_timer_cmd = rec.capabilities.supports_timer and (
            command_timer_action is not None
            or command_timer_duration is not None
            or (is_mode_cmd and not rec.capabilities.supports_sensor)
            or (
                command_state is not None
                and not is_cover_cmd
                and not is_effect_cmd
                and not is_color_cmd
                and not is_brightness_cmd
                and not is_mode_cmd
            )
        )
        if is_timer_cmd:
            packets: List[PixieCoreCommandPacket] = []
            target = "timer_relay"
            if command_timer_action == "restart":
                target = "timer_restart"
                packets.append(_packet(
                    self._build_timer_restart_command_hex(device_id),
                    delay_after=0.2,
                    log_message="Sending timer restart command: device_id=%s opcode=c16969",
                    log_args=(device_id,),
                ))
                packets.append(_packet(self._build_timer_poll_command_hex(device_id), tcp_repeat=1))
            elif command_timer_action == "override":
                target = "timer_override"
                packets.append(_packet(
                    self._build_timer_override_command_hex(device_id),
                    log_message="Sending timer override command: device_id=%s opcode=c16969",
                    log_args=(device_id,),
                ))
            elif command_timer_action == "set_duration":
                target = "timer_duration"
                duration = int(command_timer_duration) if command_timer_duration is not None else 60
                duration_commands = self._build_timer_set_duration_commands(device_id, duration)
                for index, (command_hex, repeat) in enumerate(duration_commands):
                    packets.append(_packet(
                        command_hex,
                        tcp_repeat=repeat,
                        delay_after=0.3 if index == len(duration_commands) - 1 else 0.2,
                        log_message="Edit sequence cmd hex: %s",
                    ))
                packets.append(_packet(self._build_timer_poll_command_hex(device_id), tcp_repeat=1))
            elif command_timer_action == "poll":
                target = "timer_poll"
                packets.append(_packet(
                    self._build_timer_poll_command_hex(device_id),
                    tcp_repeat=1,
                    log_message="Sending timer poll command: device_id=%s opcode=f96b69",
                    log_args=(device_id,),
                ))
            elif command_mode is not None:
                target = "timer_mode"
                if int(command_mode) == 2:
                    packets.append(_packet(
                        self._build_timer_override_command_hex(device_id),
                        log_message="Sending timer mode switch (override): device_id=%s",
                        log_args=(device_id,),
                    ))
                else:
                    packets.append(_packet(
                        self._build_timer_onoff_command_hex(device_id, is_on=True),
                        delay_after=0.2,
                        log_message="Sending timer mode switch (timer, light on): device_id=%s",
                        log_args=(device_id,),
                    ))
                    packets.append(_packet(self._build_timer_poll_command_hex(device_id), tcp_repeat=1))
            elif command_state is not None:
                target = "timer_relay"
                packets.append(_packet(
                    self._build_timer_onoff_command_hex(device_id, is_on=bool(command_state)),
                    delay_after=0.2 if command_state else 0.0,
                    log_message="Sending timer on/off command: device_id=%s state=%s opcode=ed6969",
                    log_args=(device_id, "on" if command_state else "off"),
                ))
                if command_state:
                    packets.append(_packet(self._build_timer_poll_command_hex(device_id), tcp_repeat=1))
            else:
                target = "timer_poll"
                packets.append(_packet(self._build_timer_poll_command_hex(device_id), tcp_repeat=1))
            return PixieCoreCommandPlan(
                device_id=device_id,
                target=target,
                packets=tuple(packets),
                optimistic_intent=optimistic_intent,
                result={"target": target, "device_id": device_id},
            )

        if is_cover_cmd:
            normalized_cover_action = str(command_cover_action).strip().lower().replace("-", "_")
            cover_button_position = resolve_cover_command_position(
                normalized_cover_action,
                action_mapping=command_cover_action_map,
                tilt_mapping=command_cover_tilt_action_map,
            )
            if cover_button_position is None:
                raise PixieAuthError(f"No manual button mapping configured for cover action '{normalized_cover_action}'")
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="cover",
                packets=(_packet(
                    self._build_cover_press_command_hex(device_id, button_position=cover_button_position),
                    log_message="Sending local cover command: device_id=%s action=%s button_position=%s opcode=c16969",
                    log_args=(device_id, normalized_cover_action, cover_button_position),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "cover", "device_id": device_id},
            )

        if is_effect_cmd:
            effect_name = str(command_effect).strip().lower()
            effect_speed = 0x04
            effect_brightness = self._default_optimistic_brightness_percent(rec)
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="effect",
                packets=(_packet(
                    self._build_effect_command_hex(
                        device_id,
                        effect_name=effect_name,
                        effect_speed=effect_speed,
                        brightness_level=effect_brightness,
                        capabilities=rec.capabilities,
                    ),
                    log_message="Sending effect command: device_id=%s effect=%s speed=0x%02x brightness=%s opcode=f86969",
                    log_args=(device_id, effect_name or "none", effect_speed, effect_brightness),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "effect", "device_id": device_id},
            )

        if is_color_cmd:
            color_brightness = int(command_brightness) if command_brightness is not None else self._default_optimistic_brightness_percent(rec)
            if color_brightness == 0:
                color_brightness = 100
            rgb = tuple(int(v) for v in command_color_rgb)
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="color",
                packets=(_packet(
                    self._build_color_command_hex(device_id, rgb=rgb, brightness_level=color_brightness),
                    log_message="Sending color command: device_id=%s rgb=%s brightness=%s opcode=c16969",
                    log_args=(device_id, rgb, color_brightness),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "color", "device_id": device_id},
            )

        if is_color_temp_cmd:
            color_brightness = int(command_brightness) if command_brightness is not None else self._default_optimistic_brightness_percent(rec)
            if color_brightness == 0:
                color_brightness = 100
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="color_temp",
                packets=(_packet(
                    self._build_tunable_white_command_hex(
                        device_id,
                        cct=int(command_color_temp_cct),
                        brightness_level=color_brightness,
                    ),
                    log_message="Sending tunable-white command: device_id=%s cct=%s brightness=%s opcode=c16969",
                    log_args=(device_id, command_color_temp_cct, color_brightness),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "color_temp", "device_id": device_id},
            )

        if is_brightness_cmd:
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="brightness",
                packets=(_packet(
                    self._build_brightness_command_hex(device_id, brightness_level=int(command_brightness)),
                    log_message="Sending brightness command: device_id=%s brightness=%s opcode=e76969",
                    log_args=(device_id, command_brightness),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "brightness", "device_id": device_id},
            )

        if is_mode_cmd:
            return PixieCoreCommandPlan(
                device_id=device_id,
                target="mode",
                packets=(_packet(
                    self._build_mode_command_hex(device_id, mode=int(command_mode)),
                    log_message="Sending mode command: device_id=%s mode=%s relay=0 opcode=c16969",
                    log_args=(device_id, command_mode),
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": "mode", "device_id": device_id},
            )

        if command_state is not None:
            effective_target = self._resolve_command_target_for_device(device_id, command_target)
            if effective_target == "sensor_led_indicator":
                if not supports_sensor_advanced_settings(rec.capabilities):
                    raise PixieAuthError(f"Model {rec.model_no} does not support advanced sensor settings")
                command_hex = self._build_sensor_led_indicator_command_hex(device_id, enabled=bool(command_state))
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target=effective_target,
                    packets=(_packet(
                        command_hex,
                        log_message="Sending sensor config command: device_id=%s target=%s state=%s opcode=ff6969",
                        log_args=(device_id, effective_target, "on" if command_state else "off"),
                    ),),
                    optimistic_intent=optimistic_intent,
                    result={"target": effective_target, "device_id": device_id},
                )
            if effective_target in {"plug_socket_led_indicator", "plug_usb_led_indicator"}:
                if not supports_plug_led_settings(rec.capabilities):
                    raise PixieAuthError(f"Model {rec.model_no} does not support plug LED settings")
                command_hex = self._build_plug_led_indicator_command_hex(
                    device_id,
                    target=effective_target,
                    enabled=bool(command_state),
                    current_socket_enabled=rec.runtime.plug_socket_led_indicator,
                    current_usb_enabled=rec.runtime.plug_usb_led_indicator,
                )
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target=effective_target,
                    packets=(_packet(
                        command_hex,
                        log_message="Sending plug LED config command: device_id=%s target=%s state=%s opcode=ff6969",
                        log_args=(device_id, effective_target, "on" if command_state else "off"),
                    ),),
                    optimistic_intent=optimistic_intent,
                    result={"target": effective_target, "device_id": device_id},
                )
            if effective_target in {"outlet_led_indicator", "outlet_all_device_control", "outlet_child_lock"}:
                supports_config_target = supports_outlet_runtime_config(rec.capabilities) or (
                    effective_target == "outlet_all_device_control"
                    and supports_plug_led_settings(rec.capabilities)
                )
                if not supports_config_target:
                    raise PixieAuthError(f"Model {rec.model_no} does not support outlet configuration commands")
                if effective_target == "outlet_led_indicator":
                    command_hex = self._build_outlet_led_indicator_command_hex(device_id, enabled=bool(command_state))
                    opcode_name = "ff6969"
                else:
                    command_hex = self._build_outlet_control_flag_command_hex(
                        device_id,
                        target=effective_target,
                        enabled=bool(command_state),
                    )
                    opcode_name = "fe6b69"
                return PixieCoreCommandPlan(
                    device_id=device_id,
                    target=effective_target,
                    packets=(_packet(
                        command_hex,
                        log_message="Sending outlet config command: device_id=%s target=%s state=%s opcode=%s",
                        log_args=(device_id, effective_target, "on" if command_state else "off", opcode_name),
                    ),),
                    optimistic_intent=optimistic_intent,
                    result={"target": effective_target, "device_id": device_id},
                )
            if rec.capabilities.supports_sensor and effective_target == "relay":
                command_hex = self._build_mode_command_hex(device_id, mode=0, relay=1 if bool(command_state) else 0)
                log_args = (device_id, "relay/main", "on" if command_state else "off", "c16969", 0)
            else:
                command_spec = self._resolve_onoff_command_spec(effective_target)
                if effective_target == "usb":
                    command_hex, state_byte_used = self._build_0107_usb_command_hex(device_id, is_on=bool(command_state))
                    log_args = (device_id, command_spec["label"], "on" if command_state else "off", command_spec["opcode_name"], command_spec["selector"], state_byte_used)
                else:
                    command_hex = self._build_6969_onoff_command_hex(
                        device_id,
                        is_on=bool(command_state),
                        opcode=command_spec["opcode"],
                        selector=command_spec["selector"],
                    )
                    log_args = (device_id, command_spec["label"], "on" if command_state else "off", command_spec["opcode_name"], command_spec["selector"], None)
            return PixieCoreCommandPlan(
                device_id=device_id,
                target=effective_target,
                packets=(_packet(
                    command_hex,
                    log_message="Sending local on/off command: device_id=%s target=%s state=%s opcode=%s selector=%s",
                    log_args=log_args[:5],
                ),),
                optimistic_intent=optimistic_intent,
                result={"target": effective_target, "device_id": device_id},
            )

        raise PixieAuthError(f"Unsupported Pixie command kwargs: {sorted(command_kwargs)}")

    def apply_optimistic_update_intent(self, intent: PixieOptimisticUpdateIntent) -> bool:
        """Apply a transport-neutral optimistic state update after a command send."""
        return self._apply_local_command_optimistic_update(
            intent.device_id,
            intent.value,
            None,
            target=intent.target,
            opcode_name="",
            brightness_level=intent.brightness_level,
            rgb_color=intent.rgb_color,
            effect_name=intent.effect_name,
            effect_speed=intent.effect_speed,
            cover_button_position=intent.cover_button_position,
        )

    def _apply_local_command_optimistic_update(
        self,
        device_id: int,
        value: Any,
        command_hex: Optional[str],
        *,
        target: str,
        opcode_name: str,
        brightness_level: Optional[int] = None,
        rgb_color: Optional[Tuple[int, int, int]] = None,
        effect_name: Optional[str] = None,
        effect_speed: Optional[int] = None,
        cover_button_position: Optional[int] = None,
    ) -> bool:
        """Apply a transport-neutral optimistic state update after a command send."""
        if not self.inventory:
            return False

        rec = self.inventory.devices_by_id.get(int(device_id))
        if not rec:
            self._log_debug("Inventory optimistic update skipped: unknown dev_id=%s", device_id)
            return False

        prev_br = rec.runtime.br
        prev_cct = rec.runtime.cct
        prev_rgb = rec.runtime.rgb
        prev_effect = rec.runtime.effect
        prev_effect_speed = rec.runtime.effect_speed
        prev_r = rec.runtime.r
        prev_source = rec.runtime.last_source
        prev_mode = rec.runtime.mode
        prev_relay = rec.runtime.relay
        prev_motion = rec.runtime.motion
        prev_armed = rec.runtime.armed
        prev_contact = rec.runtime.contact_active
        update_kwargs: Dict[str, Any] = {}

        if target == "brightness":
            if isinstance(value, int):
                update_kwargs["br"] = value
        elif target == "color":
            if isinstance(brightness_level, int):
                update_kwargs["br"] = brightness_level
            if rgb_color is not None:
                update_kwargs["rgb"] = [int(rgb_color[0]), int(rgb_color[1]), int(rgb_color[2])]
            update_kwargs["effect"] = None
        elif target == "color_temp":
            if isinstance(brightness_level, int):
                update_kwargs["br"] = brightness_level
            update_kwargs["cct"] = int(value) if value is not None else None
            update_kwargs["effect"] = None
        elif target == "effect":
            if isinstance(brightness_level, int):
                update_kwargs["br"] = brightness_level
            update_kwargs["effect"] = effect_name
            update_kwargs["effect_speed"] = effect_speed
        elif target == "speed":
            if isinstance(brightness_level, int):
                update_kwargs["br"] = brightness_level
            update_kwargs["effect"] = effect_name
            update_kwargs["effect_speed"] = effect_speed
        elif target == "cover":
            pass
        elif target == "timer_relay":
            update_kwargs["br"] = 100 if value else 0
            if value:
                update_kwargs["mode"] = 1
                if rec.runtime.timer_total_seconds is not None:
                    update_kwargs["timer_remaining_seconds"] = rec.runtime.timer_total_seconds
                    update_kwargs["last_timer_poll_at"] = time.time()
        elif target == "timer_override":
            update_kwargs["br"] = 100
            update_kwargs["mode"] = 2
        elif target == "timer_restart":
            update_kwargs["br"] = 100
            update_kwargs["mode"] = 1
            update_kwargs["timer_remaining_seconds"] = rec.runtime.timer_total_seconds
        elif target == "timer_mode":
            mode_value = int(value) if value is not None else 1
            update_kwargs["mode"] = mode_value
            if mode_value == 2:
                update_kwargs["br"] = 100
            elif mode_value == 1:
                update_kwargs["br"] = 100
        elif target == "timer_duration":
            pass
        elif target == "hold_time":
            update_kwargs["hold_time_seconds"] = int(value) if value is not None else None
        elif target == "brightness_threshold":
            update_kwargs["brightness_threshold"] = int(value) if value is not None else None
        elif target == "motion_sensitivity":
            update_kwargs["motion_sensitivity"] = int(value) if value is not None else None
        elif target == "sensor_led_indicator":
            update_kwargs["sensor_led_indicator"] = bool(value)
        elif target == "indicator_led_settings":
            if isinstance(value, dict):
                update_kwargs["indicator_led_on"] = value.get("on")
                update_kwargs["indicator_led_off"] = value.get("off")
        elif target == "outlet_led_indicator":
            update_kwargs["outlet_led_indicator"] = bool(value)
        elif target == "outlet_all_device_control":
            update_kwargs["outlet_all_device_control"] = bool(value)
        elif target == "outlet_child_lock":
            update_kwargs["outlet_child_lock"] = bool(value)
        elif target == "plug_socket_led_indicator":
            update_kwargs["plug_socket_led_indicator"] = bool(value)
        elif target == "plug_usb_led_indicator":
            update_kwargs["plug_usb_led_indicator"] = bool(value)
        elif target == "gate_signal_width":
            update_kwargs["gate_signal_width_seconds"] = int(value) if value is not None else None
        elif target in {"gate_door_open_duration", "gate_door_close_duration"}:
            if isinstance(value, dict):
                door_index = int(value.get("door") or 0)
                duration_ms = int(value.get("seconds") or 0) * 1000
                if target == "gate_door_open_duration":
                    update_kwargs["door1_open_duration_ms" if door_index == 0 else "door2_open_duration_ms"] = duration_ms
                else:
                    update_kwargs["door1_close_duration_ms" if door_index == 0 else "door2_close_duration_ms"] = duration_ms
        elif target == "timer_poll_stamp":
            import time as _time
            update_kwargs["last_timer_poll_requested_at"] = _time.time()
        elif target == "power_meter_poll_stamp":
            import time as _time
            update_kwargs["last_power_meter_poll_at"] = _time.time()
        elif target == "mode":
            mode_value = int(value)
            update_kwargs["mode"] = mode_value
            update_kwargs["relay"] = 0
            update_kwargs["motion"] = False
            update_kwargs["br"] = 0
        elif target == "arm":
            update_kwargs["armed"] = bool(value)
            update_kwargs["contact_momentary"] = False
            if value:
                update_kwargs["contact_active"] = False
            else:
                update_kwargs["contact_active"] = None
        elif target == "relay" and rec.capabilities.supports_sensor:
            update_kwargs["mode"] = 0
            update_kwargs["relay"] = 1 if value else 0
            update_kwargs["motion"] = False
            update_kwargs["br"] = 100 if value else 0
        elif rec.capabilities.supports_dimming:
            def _remembered_turn_on_brightness() -> int:
                for candidate in (rec.runtime.last_nonzero_br, rec.runtime.br):
                    if isinstance(candidate, int) and candidate > 0:
                        return max(1, min(100, candidate))
                return 100

            update_kwargs["br"] = _remembered_turn_on_brightness() if value else 0
        elif rec.capabilities.supports_usb_subentity:
            if isinstance(rec.runtime.r, int):
                current_relay_on = bool(rec.runtime.r & 0x01)
                current_usb_on = bool(rec.runtime.r & 0x02)
            elif isinstance(rec.runtime.br, int):
                current_relay_on = rec.runtime.br > 0
                current_usb_on = False
            else:
                current_relay_on = False
                current_usb_on = False

            if target == "usb":
                next_relay_on = current_relay_on
                next_usb_on = value
            else:
                next_relay_on = value
                next_usb_on = current_usb_on

            update_kwargs["r"] = (1 if next_relay_on else 0) | (2 if next_usb_on else 0)
            update_kwargs["br"] = 100 if next_relay_on else 0
        else:
            update_kwargs["br"] = 100 if value else 0

        if target in ("left", "right", "both"):
            current_r = rec.runtime.r if isinstance(rec.runtime.r, int) else 0
            if target == "left":
                if value:
                    update_kwargs["r"] = current_r | 0x01
                else:
                    update_kwargs["r"] = current_r & ~0x01
            elif target == "right":
                if value:
                    update_kwargs["r"] = current_r | 0x02
                else:
                    update_kwargs["r"] = current_r & ~0x02
            else:
                update_kwargs["r"] = 3 if value else 0

        update_kwargs["raw"] = {
            "optimistic_update": {
                "device_id": int(device_id),
                "target": target,
                "requested_state": (
                    f"{value}"
                    if target in ("brightness", "color", "color_temp", "effect", "speed", "cover", "mode")
                    else ("on" if value else "off")
                ),
                "brightness_level": brightness_level,
                "cct": update_kwargs.get("cct"),
                "rgb_color": list(rgb_color) if rgb_color is not None else None,
                "effect_name": effect_name,
                "effect_speed": effect_speed,
                "cover_button_position": cover_button_position,
                "pending_verification": True,
            }
        }
        updated_runtime = self.inventory.apply_device_update(
            device_id,
            source="local_command_optimistic",
            **update_kwargs,
        )
        if updated_runtime is None:
            return False
        if target == "color" and int(rec.capabilities.color_runtime_white_preferred_tail) >= 0:
            if rgb_color is not None and tuple(int(channel) for channel in rgb_color[:3]) == (0, 0, 255):
                updated_runtime.local_ambiguous_blue_intent_until = time.time() + LOCAL_AMBIGUOUS_BLUE_CONFIRM_SECONDS
            else:
                updated_runtime.local_ambiguous_blue_intent_until = None
        elif target in ("color_temp", "effect", "brightness"):
            updated_runtime.local_ambiguous_blue_intent_until = None
        if target == "timer_restart":
            updated_runtime.local_timer_restart_at = time.time()

        summary_parts = [
            f"br {prev_br}->{updated_runtime.br}",
            f"cct {prev_cct}->{updated_runtime.cct}",
            f"rgb {prev_rgb}->{updated_runtime.rgb}",
            f"effect {prev_effect}->{updated_runtime.effect}",
            f"speed {prev_effect_speed}->{updated_runtime.effect_speed}",
            f"r {prev_r}->{updated_runtime.r}",
        ]
        if prev_mode != updated_runtime.mode:
            summary_parts.append(f"mode {prev_mode}->{updated_runtime.mode}")
        if prev_relay != updated_runtime.relay:
            summary_parts.append(f"relay {prev_relay}->{updated_runtime.relay}")
        if prev_motion != updated_runtime.motion:
            summary_parts.append(f"motion {prev_motion}->{updated_runtime.motion}")
        if prev_armed != updated_runtime.armed:
            summary_parts.append(f"armed {prev_armed}->{updated_runtime.armed}")
        if prev_contact != updated_runtime.contact_active:
            summary_parts.append(f"contact {prev_contact}->{updated_runtime.contact_active}")

        self._log_debug(
            "Inventory optimistic update: id=%s name=%s target=%s %s src %s->%s",
            rec.id,
            rec.name,
            target,
            " ".join(summary_parts),
            prev_source,
            updated_runtime.last_source,
        )
        return True

    def _perform_handshake_capture(
        self,
        hub_ip: str,
        hub_port: int,
        runtime_session: Optional[PixieRuntimeSession] = None,
        control_ready_event: Optional[threading.Event] = None,
        control_ready_state: Optional[Dict[str, Any]] = None,
        stop_event: Optional[threading.Event] = None,
        keep_control_alive: bool = True,
        command_request_queue: Optional["queue.Queue[Dict[str, Any]]"] = None,
        *,
        command_device_id: Optional[int] = None,
        command_state: Optional[bool] = None,
        command_brightness: Optional[int] = None,
        command_color_rgb: Optional[Tuple[int, int, int]] = None,
        command_color_temp_cct: Optional[int] = None,
        command_white: bool = False,
        command_effect: Optional[str] = None,
        command_target: Optional[str] = None,
        command_mode: Optional[int] = None,
        command_cover_action: Optional[str] = None,
        command_cover_action_map: Optional[Dict[str, int]] = None,
        command_cover_tilt_action_map: Optional[Dict[str, int]] = None,
        command_timer_action: Optional[str] = None,
        command_timer_duration: Optional[int] = None,
        command_power_meter_action: Optional[str] = None,
        command_sensor_param: Optional[str] = None,
        command_sensor_param_value: Optional[int] = None,
        command_gate_param: Optional[str] = None,
        command_gate_param_value: Optional[int] = None,
        command_gate_door: Optional[int] = None,
        command_indicator_led_action: Optional[str] = None,
        command_indicator_led_on: Optional[int] = None,
        command_indicator_led_off: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Perform full client-mode TCP handshake sequence (matches Java app flow).

        Args:
            hub_ip: Target hub IP address
            hub_port: Hub TCP control port
            command_device_id: Optional device ID to command after handshake
            command_state: Optional target on/off state for local command
            command_brightness: Optional brightness 0-100 for brightness command
            command_color_rgb: Optional RGB tuple for color command
            command_color_temp_cct: Optional 0-255 tunable-white position
            command_white: Whether color command was requested via --white
            command_effect: Optional effect name command
            command_target: Optional target endpoint for on/off command
            command_cover_action: Optional cover action command

        Returns:
            Auth result dict if successful
        """

        from .pixie_protocol import (
            PixieMessage,
            PixieEnvelope,
            FLAG_DUAL_DATA,
            FLAG_EACK,
            FLAG_HEARTBEAT,
            FLAG_SINGLE_DATA,
        )

        self._log_debug("Connecting as client to %s:%s", hub_ip, hub_port)

        # Create TCP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10.0)

        try:
            sock.connect((hub_ip, hub_port))
            self._log_debug("TCP connection established")
            self.current_hub = {"host": hub_ip, "port": hub_port}
        except Exception as e:
            self._log_warning("TCP connection failed: %s", e)
            return None

        extracted_key = None
        incoming_queue: "queue.Queue[Tuple[float, bytes]]" = queue.Queue()
        reader_stop = threading.Event()
        reader_thread: Optional[threading.Thread] = None
        connection_closed = False
        should_stop = stop_event or threading.Event()
        pending_requests = deque()
        readiness = {
            "saw_bledata": False,
            "saw_bulk_bledata": False,
            "saw_eack_reply": False,
            "sent_heartbeat": False,
            "saw_heartbeat_reply": False,
            "ready_signaled": False,
        }

        if control_ready_state is not None:
            control_ready_state.update(readiness)

        def _update_ready_state(**kwargs: Any) -> None:
            changed = False
            for key, value in kwargs.items():
                if key in readiness and readiness[key] != value:
                    readiness[key] = value
                    changed = True
            if control_ready_state is not None and changed:
                control_ready_state.update(readiness)

        def _maybe_signal_ready() -> None:
            if readiness["ready_signaled"]:
                return
            if (
                readiness["saw_bledata"]
                and readiness["saw_eack_reply"]
                and readiness["sent_heartbeat"]
                and readiness["saw_heartbeat_reply"]
            ):
                _update_ready_state(ready_signaled=True)
                if runtime_session is not None:
                    runtime_session.mark_primed()
                if control_ready_event is not None:
                    control_ready_event.set()
                self._log_debug("41578 control primed: bleData + eack_reply + first heartbeat roundtrip")

        def _parse_message(raw_b64: str, key: Optional[str]) -> Dict[str, Any]:
            """Decode base64, parse envelope flag, and decrypt JSON when possible."""
            parsed: Dict[str, Any] = {
                "raw_b64": raw_b64,
                "flag": None,
                "envelope": None,
                "plaintext": None,
                "json": None,
                "error": None,
            }

            try:
                envelope_bytes = PixieEnvelope.from_base64(raw_b64)
                envelope = PixieEnvelope.decode(envelope_bytes)
                parsed["envelope"] = envelope
                if not envelope:
                    parsed["error"] = "invalid envelope"
                    return parsed

                parsed["flag"] = envelope.get("flag1")
                if key and parsed["flag"] != FLAG_DUAL_DATA:
                    plaintext = PixieEnvelope.decrypt_envelope(envelope, key)
                    parsed["plaintext"] = plaintext
                    if plaintext:
                        try:
                            parsed["json"] = json.loads(plaintext)
                        except Exception:
                            parsed["json"] = None
                return parsed
            except Exception as exc:
                parsed["error"] = str(exc)
                return parsed

        def _classify_message(direction: str, parsed: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
            """Classify a transport frame by envelope flag, content, and context."""
            flag = parsed.get("flag")
            payload = parsed.get("json") or {}
            op = payload.get("op") if isinstance(payload, dict) else None
            code = payload.get("code") if isinstance(payload, dict) else None

            matched_request: Optional[Dict[str, Any]] = None

            if flag == FLAG_DUAL_DATA:
                return "session_init", None

            if direction == "out":
                if flag == FLAG_HEARTBEAT and op == "ack" and code == 0:
                    return "heartbeat", None
                if flag == FLAG_EACK and op == "ack" and code == 0:
                    return "eack", None
                if flag == FLAG_SINGLE_DATA:
                    return "command", None
                return f"out_flag_{flag}", None

            if flag == FLAG_EACK and op == "ack" and code == 0:
                if pending_requests:
                    matched_request = pending_requests.popleft()
                    if matched_request["kind"] == "heartbeat":
                        return "heartbeat_reply", matched_request
                    if matched_request["kind"] == "eack":
                        return "eack_reply", matched_request
                return "ack", None

            if flag == FLAG_HEARTBEAT and op == "ack" and code == 0:
                if pending_requests:
                    matched_request = pending_requests.popleft()
                    if matched_request["kind"] == "heartbeat":
                        return "heartbeat_reply", matched_request
                return "heartbeat_push", None

            if flag == FLAG_SINGLE_DATA:
                if op == "ack" and code == 0:
                    return "encrypted_ack", None
                if parsed.get("json") is not None:
                    return "device_or_control_update", None
                if parsed.get("plaintext"):
                    return "encrypted_payload", None

            return f"in_flag_{flag}", None

        def _decode_ble_data(hex_payload: str) -> Optional[Dict[str, Any]]:
            """Decode bleData payloads into normalized single or bulk records."""
            return self.decode_bledata_hex(hex_payload)

        def _apply_flag1_update(parsed: Dict[str, Any]) -> None:
            """Apply known flag=1 bleData updates into inventory runtime state."""
            payload = parsed.get("json")
            if not isinstance(payload, dict):
                return

            if payload.get("type") != "bleData":
                return

            ble_hex = payload.get("data")
            decoded = self.decode_bledata_hex(ble_hex)
            if not decoded:
                self._log_debug("BLE decode: unable to parse data field")
                return
            full_snapshot = False
            if decoded.get("kind") == "bulk":
                full_snapshot = self._awaiting_initial_gwdata_bulk
                if full_snapshot:
                    self._awaiting_initial_gwdata_bulk = False
                _update_ready_state(saw_bulk_bledata=True)
            self.apply_bledata_hex(
                ble_hex,
                payload_meta=payload,
                source="hub_update",
                bulk_source="hub_gwdata",
                full_snapshot=full_snapshot,
                queue_bulk=decoded.get("kind") == "bulk",
            )

        def _apply_conf_update(parsed: Dict[str, Any]) -> None:
            """Apply gateway configuration-change notifications."""
            payload = parsed.get("json")
            if not isinstance(payload, dict):
                return

            data = payload.get("data")
            if not isinstance(data, dict) or data.get("type") != "confUpdate":
                return

            raw_conf_index = data.get("confIndex")
            if not isinstance(raw_conf_index, list):
                self._log_debug("Gateway confUpdate ignored without confIndex: %s", data)
                return

            conf_index: List[int] = []
            for value in raw_conf_index:
                try:
                    conf_index.append(int(value))
                except (TypeError, ValueError):
                    self._log_debug("Gateway confUpdate ignored with invalid confIndex: %s", raw_conf_index)
                    return
            self._log_debug("Gateway confUpdate received confIndex=%s", conf_index)
            self._notify_config_updated(conf_index)

        def _send_requested_local_command(
            *,
            command_device_id: int,
            command_state: Optional[bool] = None,
            command_brightness: Optional[int] = None,
            command_color_rgb: Optional[Tuple[int, int, int]] = None,
            command_color_temp_cct: Optional[int] = None,
            command_effect: Optional[str] = None,
            command_target: Optional[str] = None,
            command_mode: Optional[int] = None,
            command_cover_action: Optional[str] = None,
            command_cover_action_map: Optional[Dict[str, int]] = None,
            command_cover_tilt_action_map: Optional[Dict[str, int]] = None,
            command_timer_action: Optional[str] = None,
            command_timer_duration: Optional[int] = None,
            command_power_meter_action: Optional[str] = None,
            command_sensor_param: Optional[str] = None,
            command_sensor_param_value: Optional[int] = None,
            command_gate_param: Optional[str] = None,
            command_gate_param_value: Optional[int] = None,
            command_gate_door: Optional[int] = None,
            command_indicator_led_action: Optional[str] = None,
            command_indicator_led_on: Optional[int] = None,
            command_indicator_led_off: Optional[int] = None,
            command_raw_hexes: Optional[Tuple[str, ...]] = None,
            command_raw_target: Optional[str] = None,
            command_raw_repeat: int = 0,
            command_raw_delay: float = 0.0,
        ) -> Dict[str, Any]:
            """Send one local command on the already-authenticated TCP socket."""
            if not readiness["ready_signaled"]:
                self._log_debug("Waiting for 41578 control priming before local command send")
                prime_deadline = time.time() + 5.0
                heartbeat_attempt = 0
                while time.time() < prime_deadline and not connection_closed and not should_stop.is_set() and not readiness["ready_signaled"]:
                    if _drain_incoming() > 0 and readiness["ready_signaled"]:
                        break
                    heartbeat_attempt += 1
                    got_traffic = _send_heartbeat_frame(f"HEARTBEAT PRIME #{heartbeat_attempt}")
                    if readiness["ready_signaled"]:
                        break
                    if not got_traffic:
                        self._log_debug(
                            "No incoming TCP traffic in priming heartbeat #%s response window",
                            heartbeat_attempt,
                        )
                    if not readiness["ready_signaled"]:
                        time.sleep(0.2)

            if not readiness["ready_signaled"]:
                raise PixieAuthError(f"41578 control channel not primed (state={readiness})")

            sender_identity = self.stored_username
            if not sender_identity and self.user_id not in (None, "", "unknown"):
                sender_identity = str(self.user_id)
            if not sender_identity:
                raise PixieAuthError("No sender identity available for local command")

            command_kwargs = {
                "command_device_id": command_device_id,
                "command_state": command_state,
                "command_brightness": command_brightness,
                "command_color_rgb": command_color_rgb,
                "command_color_temp_cct": command_color_temp_cct,
                "command_effect": command_effect,
                "command_target": command_target,
                "command_mode": command_mode,
                "command_cover_action": command_cover_action,
                "command_cover_action_map": command_cover_action_map,
                "command_cover_tilt_action_map": command_cover_tilt_action_map,
                "command_timer_action": command_timer_action,
                "command_timer_duration": command_timer_duration,
                "command_power_meter_action": command_power_meter_action,
                "command_sensor_param": command_sensor_param,
                "command_sensor_param_value": command_sensor_param_value,
                "command_gate_param": command_gate_param,
                "command_gate_param_value": command_gate_param_value,
                "command_gate_door": command_gate_door,
                "command_indicator_led_action": command_indicator_led_action,
                "command_indicator_led_on": command_indicator_led_on,
                "command_indicator_led_off": command_indicator_led_off,
                "command_raw_hexes": command_raw_hexes,
                "command_raw_target": command_raw_target,
                "command_raw_repeat": command_raw_repeat,
                "command_raw_delay": command_raw_delay,
            }
            plan = self.build_core_command_plan(command_kwargs)

            tcp_reply_route_id = self._gateway_reply_route_node_id()
            for packet in plan.packets:
                if packet.log_message:
                    if packet.log_args:
                        self._log_debug(packet.log_message, *packet.log_args)
                    elif "%s" in packet.log_message:
                        self._log_debug(packet.log_message, packet.command_hex)
                    else:
                        self._log_debug(packet.log_message)

                command_hex, old_route, new_route, route_kind = patch_command_reply_route(
                    packet.command_hex,
                    tcp_reply_route_id,
                )
                if old_route is not None:
                    self._log_debug(
                        "Command reply route resolved transport=tcp kind=%s device=%s reply_node=%s old=%s new=%s",
                        route_kind,
                        plan.device_id,
                        tcp_reply_route_id,
                        old_route,
                        new_route,
                    )
                elif is_command_reply_route_packet(bytes.fromhex(packet.command_hex)):
                    self._log_debug(
                        "Command reply route not patched transport=tcp kind=%s device=%s reason=no_gateway_route_id",
                        route_kind,
                        plan.device_id,
                    )

                command_debug = self._build_local_bledata_command_debug(
                    key=extracted_key,
                    command_hex=command_hex,
                    from_email=sender_identity,
                    repeat=packet.tcp_repeat,
                )
                command_b64 = command_debug["base64"]
                if self.verbose:
                    self._log_debug("Core command hex: %s", command_hex)
                    self._print_local_command_debug(command_debug)

                command_parsed = _parse_message(command_b64, extracted_key)
                command_route, command_match = _classify_message("out", command_parsed)
                _log_message("out", command_parsed, command_route, command_match)
                sock.sendall(command_b64.encode("utf-8"))
                if runtime_session is not None:
                    runtime_session.mark_command_sent()
                _drain_incoming()
                if packet.delay_after:
                    time.sleep(packet.delay_after)

            if plan.optimistic_intent is not None:
                self.apply_optimistic_update_intent(plan.optimistic_intent)
            return dict(plan.result or {"target": plan.target, "device_id": plan.device_id})

        def _log_message(
            direction: str,
            parsed: Dict[str, Any],
            route: str,
            matched_request: Optional[Dict[str, Any]],
            *,
            byte_len: Optional[int] = None,
        ) -> None:
            """Log routed message details in a way that links requests and replies."""
            if self.suppress_heartbeat_logs and route in {"heartbeat", "heartbeat_reply", "heartbeat_push", "ack"}:
                return

            prefix = "OUT" if direction == "out" else "IN"
            size_note = f" ({byte_len} bytes)" if byte_len is not None else ""
            lines = [
                f"Raw base64: {parsed['raw_b64']}",
            ]

            flag = parsed.get("flag")
            if flag is not None:
                lines.append(f"{prefix} flag: {flag}")

            if matched_request:
                label = matched_request.get("label")
                lines.append(f"Routed as reply to: {label}")

            if parsed.get("json") is not None:
                lines.append(f"{prefix} decrypted JSON: {json.dumps(parsed['json'], ensure_ascii=False)}")
            elif parsed.get("plaintext"):
                lines.append(f"{prefix} decrypted payload: {parsed['plaintext']}")
            elif flag == FLAG_DUAL_DATA:
                lines.append(f"{prefix} plaintext: (dual-block handshake envelope)")
            elif parsed.get("error"):
                lines.append(f"{prefix} parse error: {parsed['error']}")

            self._log_multiline_debug(f"{prefix} {route.upper()}{size_note}", lines)

        def _tcp_reader_loop() -> None:
            """Continuously read TCP frames and enqueue them for processing."""
            while not reader_stop.is_set() and not should_stop.is_set():
                try:
                    packet = sock.recv(4096)
                    if not packet:
                        incoming_queue.put((time.time(), b""))
                        break
                    if runtime_session is not None:
                        runtime_session.mark_inbound_traffic()
                    incoming_queue.put((time.time(), packet))
                except socket.timeout:
                    continue
                except OSError:
                    if not reader_stop.is_set():
                        incoming_queue.put((time.time(), b""))
                    break

        def _drain_incoming(max_messages: int = 20) -> int:
            """Drain queued packets and print/decrypt immediately."""
            nonlocal connection_closed

            processed = 0
            while processed < max_messages:
                try:
                    _, packet = incoming_queue.get_nowait()
                except queue.Empty:
                    break

                if packet == b"":
                    if not connection_closed:
                        self._log_warning("TCP connection closed by hub")
                        connection_closed = True
                        if runtime_session is not None:
                            runtime_session.mark_connection_closed()
                    continue

                raw_b64 = packet.decode("utf-8", errors="ignore").strip()
                if not raw_b64:
                    continue

                parsed = _parse_message(raw_b64, extracted_key)
                route, matched_request = _classify_message("in", parsed)
                _log_message("in", parsed, route, matched_request, byte_len=len(packet))

                payload_obj = parsed.get("json") if isinstance(parsed.get("json"), dict) else None
                if route == "device_or_control_update" and isinstance(payload_obj, dict) and payload_obj.get("type") == "bleData":
                    _update_ready_state(saw_bledata=True)
                    _maybe_signal_ready()
                elif route == "eack_reply":
                    _update_ready_state(saw_eack_reply=True)
                    # In captures, the first ACK after heartbeat can still be routed as
                    # eack_reply due request ordering. Treat any ACK after heartbeat send
                    # as completing the first heartbeat roundtrip.
                    if readiness["sent_heartbeat"]:
                        _update_ready_state(saw_heartbeat_reply=True)
                        if runtime_session is not None:
                            runtime_session.mark_heartbeat_reply()
                    _maybe_signal_ready()
                elif route == "ack":
                    if readiness["sent_heartbeat"]:
                        _update_ready_state(saw_heartbeat_reply=True)
                        if runtime_session is not None:
                            runtime_session.mark_heartbeat_reply()
                    _maybe_signal_ready()
                elif route in ("heartbeat_reply", "heartbeat_push"):
                    _update_ready_state(saw_heartbeat_reply=True)
                    if runtime_session is not None:
                        runtime_session.mark_heartbeat_reply()
                    _maybe_signal_ready()

                if route == "device_or_control_update":
                    _apply_flag1_update(parsed)
                    _apply_conf_update(parsed)
                processed += 1

            return processed

        def _send_heartbeat_frame(label: str) -> bool:
            """Send one heartbeat and process a short response window."""
            hb_msg = PixieMessage.build_heartbeat(extracted_key)
            _update_ready_state(sent_heartbeat=True)
            previous_reply_at = runtime_session.last_heartbeat_reply_at if runtime_session is not None else None
            if runtime_session is not None:
                runtime_session.mark_heartbeat_sent()
            pending_requests.append({"kind": "heartbeat", "label": label})
            hb_parsed = _parse_message(hb_msg, extracted_key)
            hb_route, hb_match = _classify_message("out", hb_parsed)
            _log_message("out", hb_parsed, hb_route, hb_match)
            sock.sendall(hb_msg.encode('utf-8'))

            response_window_end = time.time() + 1.5
            got_traffic = False
            while time.time() < response_window_end and not connection_closed and not should_stop.is_set():
                if _drain_incoming() > 0:
                    got_traffic = True
                time.sleep(0.05)
            if runtime_session is not None and runtime_session.last_heartbeat_reply_at == previous_reply_at:
                runtime_session.mark_heartbeat_failure()
            return got_traffic

        def _decrypt_initial_dual_parts(envelope_struct: Dict[str, Any]) -> tuple[Optional[tuple[str, str]], Optional[str]]:
            """Try stored netID first, then its integer-like form for zero-padded accounts."""
            netid_candidates: list[str] = []
            if self.netid_seed not in (None, "", "unknown"):
                stored_netid = str(self.netid_seed)
                netid_candidates.append(stored_netid)
                stripped_netid = stored_netid.lstrip("0") or "0"
                if stripped_netid != stored_netid:
                    netid_candidates.append(stripped_netid)

            for candidate in netid_candidates:
                parts = PixieEnvelope.decrypt_dual_parts(envelope_struct, candidate)
                if parts:
                    if candidate != str(self.netid_seed):
                        self._log_debug(
                            "Initial hub handshake decrypted with normalized netID %s from stored %s",
                            candidate,
                            self.netid_seed,
                        )
                    return parts, candidate
            return None, None

        try:
            # Java flow: hub sends first, then app sends eack, then heartbeat loop starts.
            self._log_debug("Waiting for hub's initial message")
            response_data = sock.recv(4096)

            if response_data:
                self._log_debug("Received %s bytes from hub", len(response_data))
                try:
                    raw_b64 = response_data.decode('utf-8').strip()
                    self._log_debug("Initial hub raw base64: %s", raw_b64)

                    envelope_bytes = PixieEnvelope.from_base64(raw_b64)
                    self._log_debug("Initial hub envelope bytes: %s...", envelope_bytes.hex()[:100])
                    envelope_struct = PixieEnvelope.decode(envelope_bytes)

                    # Java 2.22 flow:
                    # - data1 decrypted with netID => session key (f14376j)
                    # - data2 decrypted with session key => mesh validation value
                    if envelope_struct and envelope_struct.get("flag1") == 0:
                        parts, handshake_netid = _decrypt_initial_dual_parts(envelope_struct)
                        if parts:
                            part1, part2 = parts
                            self._log_debug("Session key extracted (Java f14376j): %s", part1)
                            self._log_debug("Mesh validation value (data2): %s", part2)

                            expected_values = {str(v) for v in [self.meshnet, self.meshnet2] if v not in (None, "", "unknown")}
                            if expected_values and part2 not in expected_values:
                                self._log_warning(
                                    "Mesh validation mismatch: got=%s, expected one of %s",
                                    part2,
                                    sorted(expected_values),
                                )
                                raise PixieGatewayConnectionError(
                                    f"Gateway mesh validation mismatch: got {part2}, expected one of {sorted(expected_values)}"
                                )
                            elif expected_values:
                                self._log_debug("Mesh validation matched cloud/UDP values")
                            extracted_key = part1
                            self.session_key_hex = extracted_key
                            initial_parsed = _parse_message(raw_b64, handshake_netid or self.netid_seed)
                            initial_route, initial_match = _classify_message("in", initial_parsed)
                            _log_message("in", initial_parsed, initial_route, initial_match, byte_len=len(response_data))
                        else:
                            self._log_warning("Could not decrypt dual-block envelope")
                    else:
                        self._log_warning("Initial hub message is not dual-block flag=0 envelope")
                except Exception as e:
                    self._log_warning("Response parse error: %s", e)
                    self._log_debug("Initial hub raw bytes: %s...", response_data.hex()[:100])

            if extracted_key:
                # Start asynchronous reader before GwData/eack to avoid missing
                # early unsolicited bulk updates.
                sock.settimeout(0.5)
                reader_thread = threading.Thread(
                    target=_tcp_reader_loop,
                    name="pixie-tcp-reader",
                    daemon=True,
                )
                reader_thread.start()

            # Step 2: Send app-like initial GwData with extracted session key.
            if extracted_key:
                gwdata_msg = PixieMessage.build_gwdata_init(extracted_key)
                gw_parsed = _parse_message(gwdata_msg, extracted_key)
                gw_route, gw_match = _classify_message("out", gw_parsed)
                _log_message("out", gw_parsed, gw_route, gw_match)
                sock.sendall(gwdata_msg.encode('utf-8'))

            # Step 3: Send eack with extracted session key.
            if extracted_key:
                eack_msg = PixieMessage.build_eack(extracted_key)
                pending_requests.append({"kind": "eack", "label": "EACK"})
                out_parsed = _parse_message(eack_msg, extracted_key)
                out_route, out_match = _classify_message("out", out_parsed)
                _log_message("out", out_parsed, out_route, out_match)
                sock.sendall(eack_msg.encode('utf-8'))

                # Let early replies land before forcing the first heartbeat.
                eack_window_end = time.time() + 0.75
                while time.time() < eack_window_end and not connection_closed and not should_stop.is_set():
                    _drain_incoming()
                    if readiness["ready_signaled"]:
                        break
                    time.sleep(0.05)

                # Optional command send after the session is authenticated.
                has_any_command = any(
                    value is not None
                    for value in (
                        command_state,
                        command_brightness,
                        command_color_rgb,
                        command_color_temp_cct,
                        command_effect,
                        command_mode,
                        command_cover_action,
                        command_timer_action,
                        command_power_meter_action,
                        command_sensor_param,
                        command_indicator_led_action,
                    )
                )
                if command_device_id is not None and has_any_command:
                    try:
                        _send_requested_local_command(
                            command_device_id=command_device_id,
                            command_state=command_state,
                            command_brightness=command_brightness,
                            command_color_rgb=command_color_rgb,
                            command_color_temp_cct=command_color_temp_cct,
                            command_effect=command_effect,
                            command_target=command_target,
                            command_mode=command_mode,
                            command_cover_action=command_cover_action,
                            command_cover_action_map=command_cover_action_map,
                            command_cover_tilt_action_map=command_cover_tilt_action_map,
                            command_timer_action=command_timer_action,
                            command_timer_duration=command_timer_duration,
                            command_power_meter_action=command_power_meter_action,
                            command_sensor_param=command_sensor_param,
                            command_sensor_param_value=command_sensor_param_value,
                            command_gate_param=command_gate_param,
                            command_gate_param_value=command_gate_param_value,
                            command_gate_door=command_gate_door,
                            command_indicator_led_action=command_indicator_led_action,
                            command_indicator_led_on=command_indicator_led_on,
                            command_indicator_led_off=command_indicator_led_off,
                            command_raw_hexes=command_raw_hexes,
                            command_raw_target=command_raw_target,
                            command_raw_repeat=command_raw_repeat,
                            command_raw_delay=command_raw_delay,
                        )
                    except Exception as exc:
                        self._log_warning("Local command not sent: %s", exc)

                # Step 4: Continuous heartbeat loop until user stops with Ctrl+C.
                if keep_control_alive:
                    hb_idx = 0
                    self._log_debug("Starting continuous heartbeat loop")
                    try:
                        while not connection_closed and not should_stop.is_set():
                            delay = 2.0 if hb_idx == 0 else 10.0

                            # Keep processing incoming push updates while waiting for
                            # the next heartbeat tick.
                            wait_end = time.time() + delay
                            while time.time() < wait_end and not connection_closed and not should_stop.is_set():
                                _drain_incoming()
                                if command_request_queue is not None:
                                    while True:
                                        try:
                                            command_request = command_request_queue.get_nowait()
                                        except queue.Empty:
                                            break

                                        command_id = command_request.get("command_id") if isinstance(command_request, dict) else None
                                        response_queue = command_request.get("response_queue") if isinstance(command_request, dict) else None
                                        request_kwargs = command_request.get("kwargs") if isinstance(command_request, dict) else None
                                        if runtime_session is not None and isinstance(command_id, int):
                                            runtime_session.mark_command_started(command_id)
                                            runtime_session.throttle_before_command_send()
                                        try:
                                            result = _send_requested_local_command(**(request_kwargs or {}))
                                            if response_queue is not None:
                                                response_queue.put(("ok", result))
                                        except Exception as exc:
                                            if response_queue is not None:
                                                response_queue.put(("error", exc))
                                            else:
                                                self._log_warning("Live queued command failed: %s", exc)
                                        finally:
                                            if runtime_session is not None and isinstance(command_id, int):
                                                runtime_session.mark_command_finished(command_id)
                                time.sleep(0.1)

                            if connection_closed or should_stop.is_set():
                                break

                            hb_idx += 1

                            got_traffic = _send_heartbeat_frame(f"HEARTBEAT #{hb_idx}")

                            if not got_traffic and not self.suppress_heartbeat_logs:
                                self._log_debug(
                                    "No incoming TCP traffic in heartbeat #%s response window",
                                    hb_idx,
                                )
                    except KeyboardInterrupt:
                        self._log_info("Heartbeat loop stopped by user")
                    finally:
                        if should_stop.is_set() and not connection_closed:
                            self._log_info("Control stop signal received")
                else:
                    self._log_debug("Control keepalive skipped (one-shot startup mode)")

        except Exception as e:
            self._log_exception("Handshake error: %s", e)
        finally:
            reader_stop.set()
            if reader_thread and reader_thread.is_alive():
                reader_thread.join(timeout=1.0)
            sock.close()

        # Step 3: Extract netID/meshNet from already fetched cloud config
        self._log_debug("Finalizing credentials")

        if not self.netid_seed:
            raise PixieAuthError("No netID seed available; ensure cloud login succeeded before handshake capture")

        # Use previously fetched values (from _fetch_login_data)
        config = {
            'netid': self.netid_seed,
            'meshnet': self.meshnet,
            'meshnet2': self.meshnet2,
        }

        # Update session key if extracted
        if extracted_key:
            self.session_key_hex = extracted_key

        # Step 4: Skip file persistence (integration-layer config stores auth data)
        # We keep in-memory values only and do not persist to ~/.pixie_auth.
        # credentials are expected to be managed by Home Assistant integration.

        result = {
            'status': 'success',
            'config': config,
            'session_key_hex': self.session_key_hex,
            'hub_ip': hub_ip,
            'hub_port': hub_port
        }

        self._log_debug("Client-mode handshake complete")

        return result

    def _next_command_sequence(self, cmd_type: int) -> bytes:
        """Return the next 3-byte command header: [counter] [cmd_type] 04
        
        Args:
            cmd_type: 0x08 for USB, 0x09 for relay commands
        """
        if self._command_counter < 0x10:
            self._command_counter = 0x10
        counter_byte = (self._command_counter & 0xFF).to_bytes(1, byteorder="little")
        self._command_counter = (self._command_counter + 1) & 0xFF
        if self._command_counter < 0x10:
            self._command_counter = 0x10
        cmd_type_byte = cmd_type.to_bytes(1, byteorder="little")
        return counter_byte + cmd_type_byte + bytes([0x04])

    def _next_shifted_sequence(self, *, counter_attr: str, minimum_counter: int) -> bytes:
        """Return the captured 3-byte rolling prefix [counter][counter>>1][counter>>2]."""
        counter_value = int(getattr(self, counter_attr, minimum_counter)) & 0xFF
        minimum = max(0x01, int(minimum_counter) & 0xFF)
        if counter_value < minimum:
            counter_value = minimum

        next_counter = (counter_value + 1) & 0xFF
        if next_counter < minimum:
            next_counter = minimum
        setattr(self, counter_attr, next_counter)

        return bytes([
            counter_value,
            (counter_value >> 1) & 0xFF,
            (counter_value >> 2) & 0xFF,
        ])

    def _next_brightness_sequence(self) -> bytes:
        """Return the captured 3-byte dimmer/cover command prefix."""
        return self._next_shifted_sequence(counter_attr="_command_counter", minimum_counter=0x10)

    def _build_shifted_prefix_command_hex(
        self,
        destination_id: int,
        opcode: bytes,
        payload: bytes,
        *,
        counter_attr: str,
        minimum_counter: int,
    ) -> str:
        """Build a command with the shifted-sequence prefix: [c|c>>1|c>>2][0304][dst_le][opcode:3][payload]."""
        sequence = self._next_shifted_sequence(counter_attr=counter_attr, minimum_counter=minimum_counter)
        src_bytes = (1027).to_bytes(2, byteorder="little")
        dst_bytes = int(destination_id).to_bytes(2, byteorder="little", signed=False)
        packet = sequence + src_bytes + dst_bytes + opcode + payload
        return packet.hex()

    def _build_sensor_mode_payload(self, *, mode: int, relay: int) -> bytes:
        """Return the captured 3001 mode payload after c16969."""
        return bytes([0x03, int(mode) & 0xFF, int(relay) & 0xFF, 0x00, 0x00, 0x00, 0x01, 0x1E, 0x00, 0x00])

    def _decode_sensor_mode_command(self, raw: bytes) -> Optional[Dict[str, int]]:
        """Decode the captured 3001 c16969 mode command layout."""
        if len(raw) != 20:
            return None
        if raw[7:10] != b"\xc1ii":
            return None
        if raw[10] != 0x03:
            return None
        if raw[13:] != b"\x00\x00\x00\x01\x1e\x00\x00":
            return None

        return {
            "device_id": int(raw[5]),
            "mode": int(raw[11]),
            "relay": int(raw[12]),
        }

    def _build_6969_onoff_command_hex(
        self,
        destination_id: int,
        *,
        is_on: bool,
        opcode: int,
        selector: int = 0,
    ) -> str:
        """Build a [counter][09:relay][04][opcode,0x69,0x69] on/off command packet for local bleData control."""
        sequence = self._next_command_sequence(cmd_type=0x09)  # 0x09 = relay command type
        src_bytes = (1027).to_bytes(2, byteorder="little")
        dst_bytes = int(destination_id).to_bytes(2, byteorder="little", signed=False)
        state_byte = b"\x01" if is_on else b"\x00"
        selector_byte = int(selector).to_bytes(1, byteorder="little", signed=False)
        payload = state_byte + selector_byte + (b"\x00" * 8)
        packet = sequence + src_bytes + dst_bytes + bytes([int(opcode) & 0xFF, 0x69, 0x69]) + payload
        return packet.hex()

    def _build_outlet_led_indicator_command_hex(self, destination_id: int, *, enabled: bool) -> str:
        """Build captured 0208 LED indicator command using opcode ff6969."""
        payload = (b"\xff" if enabled else b"\x00") + (b"\x00" * 9)
        return self._build_shifted_prefix_command_hex(
            destination_id,
            opcode=b"\xff\x69\x69",
            payload=payload,
            counter_attr="_command_counter",
            minimum_counter=0x01,
        )

    def _build_outlet_control_flag_command_hex(self, destination_id: int, *, target: str, enabled: bool) -> str:
        """Build captured 0208 all-device-control/child-lock command using opcode fe6b69."""
        if target == "outlet_all_device_control":
            payload_value = 0x01 if enabled else 0x00
        elif target == "outlet_child_lock":
            payload_value = 0x03 if enabled else 0x02
        else:
            raise PixieAuthError(f"Unsupported outlet config target: {target}")
        return self._build_shifted_prefix_command_hex(
            destination_id,
            opcode=b"\xfe\x6b\x69",
            payload=bytes([payload_value]),
            counter_attr="_command_counter",
            minimum_counter=0x01,
        )

    def _build_plug_led_indicator_command_hex(
        self,
        destination_id: int,
        *,
        target: str,
        enabled: bool,
        current_socket_enabled: Optional[bool],
        current_usb_enabled: Optional[bool],
    ) -> str:
        """Build captured 0107 socket/USB LED settings block using opcode ff6969."""
        socket_enabled = bool(current_socket_enabled) if current_socket_enabled is not None else True
        usb_enabled = bool(current_usb_enabled) if current_usb_enabled is not None else True
        if target == "plug_socket_led_indicator":
            socket_enabled = bool(enabled)
        elif target == "plug_usb_led_indicator":
            usb_enabled = bool(enabled)
        else:
            raise PixieAuthError(f"Unsupported plug LED target: {target}")
        payload = bytes([
            0xFF,
            0x05 if socket_enabled else 0x00,
            0x00,
            0x00,
            0xBC if usb_enabled else 0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
        ])
        return self._build_shifted_prefix_command_hex(
            destination_id,
            opcode=b"\xff\x69\x69",
            payload=payload,
            counter_attr="_command_counter",
            minimum_counter=0x01,
        )

    def _build_0107_usb_command_hex(self, destination_id: int, *, is_on: bool) -> str:
        """Build 0107 USB command for direct local TCP test using state bytes 0x08/0x0c.

        This mirrors legacy cloud command semantics where USB toggle is treated
        independently from relay state.
        """
        sequence = self._next_command_sequence(cmd_type=0x08)  # 0x08 = USB command type
        src_bytes = (1027).to_bytes(2, byteorder="little")
        dst_bytes = int(destination_id).to_bytes(2, byteorder="little", signed=False)

        state_byte = 0x0C if is_on else 0x08
        state_byte_val = state_byte.to_bytes(1, byteorder="little", signed=False)
        payload = state_byte_val + (b"\x00" * 3)  # 4 bytes total: [state][000000]
        packet = sequence + src_bytes + dst_bytes + bytes([0xC1, 0x69, 0x69]) + payload
        return packet.hex(), state_byte

    def _build_brightness_command_hex(
        self,
        destination_id: int,
        *,
        brightness_level: int,
    ) -> str:
        """Build a local dimmer brightness command using the captured e76969 format."""
        if not (0 <= brightness_level <= 100):
            raise PixieAuthError(f"Brightness must be 0-100, got {brightness_level}")

        sequence = self._next_brightness_sequence()
        src_bytes = (1027).to_bytes(2, byteorder="little")
        destination_marker = b"\xff\xff"
        dst_bytes = int(destination_id).to_bytes(2, byteorder="little", signed=False)

        # Captures align more closely with a 0-256 scale than simple floor(0-255).
        brightness_byte = min(0xFF, max(0x00, round((brightness_level * 256) / 100)))
        payload = bytes([0x32, 0x00, 0x10, brightness_byte, 0x00, 0x00]) + dst_bytes

        packet = sequence + src_bytes + destination_marker + bytes([0xE7, 0x69, 0x69]) + payload
        return packet.hex()

    @staticmethod
    def _tunable_white_payload_from_cct(cct: int) -> Tuple[int, int, int]:
        """Map a 0..255 tunable-white position to the observed c16969 payload bytes.

        Captures indicate the rippleSHIELD dimmer uses a piecewise white-temperature
        ramp with a fixed middle byte and varying warm/cool bytes:
        - WW (0)   -> ff b8 80
        - CW (~125)-> ff b8 fd
        - DL (255) -> 80 b8 ff
        """
        cct = max(0, min(255, int(cct)))
        if cct <= 125:
            blue = 0x80 + round((0xFD - 0x80) * (cct / 125.0))
            return (0xFF, 0xB8, max(0x80, min(0xFD, int(blue))))

        fraction = (cct - 125) / 130.0
        red = 0xFF + round((0x80 - 0xFF) * fraction)
        return (max(0x80, min(0xFF, int(red))), 0xB8, 0xFF)

    def _build_tunable_white_command_hex(
        self,
        destination_id: int,
        *,
        cct: int,
        brightness_level: int,
    ) -> str:
        """Build app-style tunable-white command using observed c16969 payload bytes."""
        if not (0 <= brightness_level <= 100):
            raise PixieAuthError(f"Brightness must be 0-100, got {brightness_level}")

        warm, center, cool = self._tunable_white_payload_from_cct(cct)

        cmd_num = (self._command_counter & 0xFF)
        self._command_counter = (self._command_counter + 1) & 0xFF
        if self._command_counter < 0x10:
            self._command_counter = 0x10

        dev_id_byte = int(destination_id) & 0xFF
        brightness_byte = min(0xFF, max(0x00, round((brightness_level * 256) / 100)))

        command_hex = (
            f"{cmd_num:02x}"
            "00000304"
            f"{dev_id_byte:02x}"
            "00c16969"
            f"{warm:02x}{center:02x}{cool:02x}"
            f"{brightness_byte:02x}"
        )
        return command_hex

    def _build_color_command_hex(
        self,
        destination_id: int,
        *,
        rgb: Tuple[int, int, int],
        brightness_level: int,
    ) -> str:
        """Build app-style color command using captured c16969 payload bytes.

        The unresolved prefix bytes stay on the existing local command path,
        but the opcode/payload now match captures: [RRGGBB][brightness].
        """
        if not (0 <= brightness_level <= 100):
            raise PixieAuthError(f"Brightness must be 0-100, got {brightness_level}")

        r, g, b = rgb
        for channel in (r, g, b):
            if not (0 <= channel <= 255):
                raise PixieAuthError(f"RGB channel out of range 0-255: {channel}")

        cmd_num = (self._command_counter & 0xFF)
        self._command_counter = (self._command_counter + 1) & 0xFF
        if self._command_counter < 0x10:
            self._command_counter = 0x10

        dev_id_byte = int(destination_id) & 0xFF
        brightness_byte = min(0xFF, max(0x00, round((brightness_level * 256) / 100)))

        command_hex = (
            f"{cmd_num:02x}"
            "00000304"
            f"{dev_id_byte:02x}"
            "00c16969"
            f"{r:02x}{g:02x}{b:02x}"
            f"{brightness_byte:02x}"
        )
        return command_hex

    def _build_effect_command_hex(
        self,
        destination_id: int,
        *,
        effect_name: Optional[str],
        effect_speed: int,
        brightness_level: int,
        capabilities: Optional[Any] = None,
    ) -> str:
        """Build app-style effect command using the model capability encoding."""
        normalized = (effect_name or "none").strip().lower()
        encoding = str(getattr(capabilities, "effect_command_encoding", "") or "")
        if not encoding:
            raise PixieAuthError("Effect command encoding is required for effect-capable devices")
        effect_map = EFFECT_COMMAND_ENCODINGS.get(encoding)
        if effect_map is None:
            raise PixieAuthError(f"Unsupported effect command encoding: {encoding}")
        if normalized not in effect_map:
            raise PixieAuthError(f"Unsupported effect for {encoding} encoding: {effect_name}")
        if not (0 <= effect_speed <= 255):
            raise PixieAuthError(f"Effect speed must be 0-255, got {effect_speed}")
        if not (0 <= brightness_level <= 100):
            raise PixieAuthError(f"Brightness must be 0-100, got {brightness_level}")

        cmd_num = (self._command_counter & 0xFF)
        self._command_counter = (self._command_counter + 1) & 0xFF
        if self._command_counter < 0x10:
            self._command_counter = 0x10

        dev_id_byte = int(destination_id) & 0xFF
        brightness_byte = min(0xFF, max(0x00, round((brightness_level * 256) / 100)))
        if encoding == "legacy":
            effect_payload = (
                f"{effect_map[normalized]}"
                f"{effect_speed:02x}"
                "ff00"
                f"{brightness_byte:02x}"
            )
        elif encoding == "template":
            effect_payload = (
                f"{effect_speed:02x}"
                f"{brightness_byte:02x}"
                f"{effect_map[normalized]}"
            )
        else:
            raise PixieAuthError(f"Unsupported effect command encoding: {encoding}")
        command_hex = (
            f"{cmd_num:02x}"
            "00000304"
            f"{dev_id_byte:02x}"
            "00f86969"
            f"{effect_payload}"
        )
        return command_hex

    def _build_cover_press_command_hex(
        self,
        destination_id: int,
        *,
        button_position: int,
    ) -> str:
        """Build app-style cover button press command using captured c16969 payload format."""
        if not (1 <= int(button_position) <= 9):
            raise PixieAuthError(f"Cover button position must be 1-9, got {button_position}")

        sequence = self._next_brightness_sequence()
        src_bytes = (1027).to_bytes(2, byteorder="little")
        dst_bytes = int(destination_id).to_bytes(2, byteorder="little", signed=False)
        payload = b"\x00\x00\x00" + bytes([(int(button_position) - 1) & 0xFF])
        packet = sequence + src_bytes + dst_bytes + bytes([0xC1, 0x69, 0x69]) + payload
        return packet.hex()

    def _build_mode_command_hex(
        self,
        destination_id: int,
        *,
        mode: int,
        relay: int = 0,
    ) -> str:
        """Build c16969 mode/relay command for sensor-capable devices.

        Captured payload bytes after c16969 are:
        [0x03][mode][relay][00][00][00][01][1e][00][00]
        where mode is a normalized sensor-family mode value and relay: 0=off, 1=on.

        Captured prefix uses the rolling 3-byte form, starting 01 00 00.
        """
        if not 0 <= int(mode) <= 255:
            raise PixieAuthError(f"Mode must fit in one byte, got {mode}")
        if relay not in (0, 1):
            raise PixieAuthError(f"Relay must be 0 (off) or 1 (on), got {relay}")

        sequence = self._next_shifted_sequence(counter_attr="_mode_command_counter", minimum_counter=0x01)
        src_bytes = (1027).to_bytes(2, byteorder="little")
        dst_bytes = int(destination_id).to_bytes(2, byteorder="little", signed=False)
        payload = self._build_sensor_mode_payload(mode=mode, relay=relay)
        packet = sequence + src_bytes + dst_bytes + bytes([0xC1, 0x69, 0x69]) + payload
        return packet.hex()

    def _resolve_command_target_for_device(self, device_id: int, requested_target: Optional[str]) -> str:
        """Resolve command target with a per-device default when --target is omitted."""
        if requested_target:
            return requested_target.strip().lower()

        if self.inventory:
            rec = self.inventory.devices_by_id.get(int(device_id))
            if rec and rec.capabilities.supports_usb_subentity:
                return "relay"

        return "relay"

    def _resolve_onoff_command_spec(self, target: str) -> Dict[str, Any]:
        """Resolve command target into opcode/selector values recovered from captures."""
        normalized = (target or "relay").strip().lower()
        spec_map: Dict[str, Dict[str, Any]] = {
            "relay": {"opcode": 0xED, "selector": 0, "label": "relay/main", "opcode_name": "ed6969"},
            "usb": {"opcode": 0xC1, "selector": 0, "label": "usb", "opcode_name": "c16969"},
            "left": {"opcode": 0xED, "selector": 1, "label": "left", "opcode_name": "ed6969"},
            "right": {"opcode": 0xED, "selector": 2, "label": "right", "opcode_name": "ed6969"},
            "both": {"opcode": 0xED, "selector": 0, "label": "both", "opcode_name": "ed6969"},
        }
        if normalized not in spec_map:
            raise PixieAuthError(f"Unsupported command target: {target}")
        return spec_map[normalized]

    # ------------------------------------------------------------------
    # Gate (1217) command
    # ------------------------------------------------------------------

    def _build_gate_command_hex(self, device_id: int, door_index: int) -> str:
        """Build f96b69 gate cycle command for one door.

        Same command cycles open→pause→close→open based on current state.
        Payload: 03 [door_index] 00 00 00 00 00 0c 02 00 00 (10 bytes, matches capture).
        """
        payload = bytes([0x03, door_index & 0xFF]) + b"\x00" * 4 + b"\x0c\x02\x00\x00"
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xf9\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_gate_signal_width_query_command_hex(self, device_id: int) -> str:
        """Build fb6b69 gate signal-width query command."""
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xfb\x6b\x69",
            payload=b"\x00\x00\x00",
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_gate_signal_width_set_command_hex(self, device_id: int, seconds: int) -> str:
        """Build fb6b69 gate signal-width set command."""
        width_ds = max(10, min(50, int(seconds) * 10))
        payload = b"\x01" + bytes([width_ds, width_ds])
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xfb\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_gate_duration_query_command_hex(self, device_id: int, door_index: int) -> str:
        """Build fc6b69 gate duration query command for one door."""
        payload = b"\x00" + bytes([int(door_index) & 0xFF]) + b"\x00" * 8
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xfc\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_gate_duration_set_command_hex(
        self,
        device_id: int,
        door_index: int,
        *,
        open_duration_ms: int,
        close_duration_ms: int,
        extra1_ms: Optional[int],
        extra2_ms: Optional[int],
    ) -> str:
        """Build fc6b69 gate duration set command for one door."""
        open_field_ms = _encode_gate_open_duration_field_ms(open_duration_ms)
        close_field_ms = _encode_gate_close_duration_field_ms(close_duration_ms)
        extra1 = 3000 if extra1_ms is None else max(0, int(extra1_ms))
        extra2 = 3000 if extra2_ms is None else max(0, int(extra2_ms))
        payload = (
            b"\x01"
            + bytes([int(door_index) & 0xFF])
            + int(open_field_ms).to_bytes(2, byteorder="little", signed=False)
            + int(close_field_ms).to_bytes(2, byteorder="little", signed=False)
            + int(extra1).to_bytes(2, byteorder="little", signed=False)
            + int(extra2).to_bytes(2, byteorder="little", signed=False)
        )
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xfc\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    # ------------------------------------------------------------------
    # Sensor (3001/3002) parameter commands
    # ------------------------------------------------------------------

    def _build_sensor_poll_command_hex(self, device_id: int) -> str:
        """Build the f96b69 sensor-parameter poll to query hold time, brightness, sensitivity."""
        payload = b"\x01\x00" + b"\x00" * 8
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xf9\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_sensor_param_command_hex(self, device_id: int, param_id: int, value: int) -> str:
        """Build a d26c69 parameter-setting command.

        param_id: 2=sensitivity, 4=brightness threshold, 5=hold time (seconds).
        Payload: [param_id] [value_le:2] [zeros:7] = 10 bytes (matches capture).
        """
        payload = bytes([param_id]) + int(value).to_bytes(2, byteorder="little") + b"\x00" * 7
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xd2\x6c\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_sensor_advanced_poll_command_hex(self, device_id: int) -> str:
        """Build the captured sensor LED settings poll."""
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xd9\x6b\x69",
            payload=b"\x77\x00",
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_sensor_led_indicator_command_hex(self, device_id: int, *, enabled: bool) -> str:
        """Build the captured sensor LED indicator command."""
        payload = (b"\xa0\x12" if enabled else b"\xa0\x00") + b"\x00" * 8
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xff\x69\x69",
            payload=payload,
            counter_attr="_command_counter",
            minimum_counter=0x01,
        )

    def _build_indicator_led_poll_command_hex(self, device_id: int) -> str:
        """Build the d96b69 switch indicator LED settings poll."""
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xd9\x6b\x69",
            payload=b"\x00\x00\x00",
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_plug_led_poll_command_hex(self, device_id: int) -> str:
        """Build the captured 0107 socket/USB LED settings poll."""
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xd9\x6b\x69",
            payload=b"\x77\x00",
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_indicator_led_set_command_hex(self, device_id: int, *, on_value: int, off_value: int) -> str:
        """Build the d96b69 switch indicator LED settings command."""
        payload = bytes([int(on_value), int(off_value), 0x01])
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xd9\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_contact_arm_command_hex(self, device_id: int, *, armed: bool) -> str:
        """Build the captured ca6b69 arm/disarm command for the contact sensor."""
        payload = bytes([0x01 if armed else 0x00]) + b"\xf6\xff\xff"
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xca\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    # ------------------------------------------------------------------
    # Timer switch (2113) command builders
    # ------------------------------------------------------------------

    def _build_timer_onoff_command_hex(self, device_id: int, *, is_on: bool) -> str:
        """Build ed6969 on/off command for timer switch using shifted-sequence prefix."""
        state_byte = b"\x01" if is_on else b"\x00"
        payload = state_byte + b"\x00" * 9
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xed\x69\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_timer_override_command_hex(self, device_id: int) -> str:
        """Build c46969 override command (payload 0x02)."""
        payload = b"\x02" + b"\x00" * 7
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xc4\x69\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_timer_restart_command_hex(self, device_id: int) -> str:
        """Build c46969 restart command (payload 0x06)."""
        payload = b"\x06" + b"\x00" * 7
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xc4\x69\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_timer_poll_command_hex(self, device_id: int) -> str:
        """Build f96b69 timer poll command to request countdown status."""
        payload = b"\x05\x00\x00\x00\x00\x77\x00"
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xf9\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_power_meter_poll_command_hex(self, device_id: int, subtype: int) -> str:
        """Build ff6b69 power-meter poll command for live values or energy totals."""
        if subtype not in (0x02, 0x03):
            raise PixieAuthError(f"Unsupported power meter poll subtype: {subtype}")
        payload = bytes([subtype]) + b"\x77\x00"
        return self._build_shifted_prefix_command_hex(
            device_id,
            opcode=b"\xff\x6b\x69",
            payload=payload,
            counter_attr="_timer_command_counter",
            minimum_counter=0x01,
        )

    def _build_timer_set_duration_commands(self, device_id: int, duration_seconds: int) -> list[tuple[str, int]]:
        """Build the 4-command sequence to set timer duration on the device.

        Sequence: d96b69 enter edit → f96b69 ack → fd6b69 value → c46969 save.
        Duration is in seconds (1-86400, matching the device's 1 sec to 24 hour range).
        Returns a list of (hex, repeat) tuples to send in order.
        """
        if not (1 <= duration_seconds <= 86400):
            raise PixieAuthError(f"Timer duration must be 1-86400 seconds, got {duration_seconds}")

        ka = {"counter_attr": "_timer_command_counter", "minimum_counter": 0x01}
        commands: list[tuple[str, int]] = []

        # 1. Enter edit mode: d96b69 (repeat=1)
        commands.append((self._build_shifted_prefix_command_hex(
            device_id, opcode=b"\xd9\x6b\x69", payload=b"\x00\x00\x00", **ka,
        ), 1))

        # 2. Timer ack/poll: f96b69 (repeat=1)
        commands.append((self._build_timer_poll_command_hex(device_id), 1))

        # 3. Timer duration value: fd6b69 (repeat=1)
        commands.append((self._build_shifted_prefix_command_hex(
            device_id, opcode=b"\xfd\x6b\x69", payload=b"\x10\x00", **ka,
        ), 1))

        # 4. Save timer: c46969 (repeat=0)
        dur_bytes = int(duration_seconds).to_bytes(2, byteorder="little")
        payload = b"\x04" + dur_bytes + b"\x00" * 4
        commands.append((self._build_shifted_prefix_command_hex(
            device_id, opcode=b"\xc4\x69\x69", payload=payload, **ka,
        ), 0))

        return commands

    def _build_local_bledata_command_debug(self, *, key: str, command_hex: str, from_email: str, repeat: int = 0) -> Dict[str, Any]:
        """Build a local bleData command and return all debug stages."""
        payload = {
            "data": {
                "type": "bleData",
                "data": command_hex,
                "repeat": repeat,
            },
            "from": from_email,
        }
        plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        encrypted = PixieCrypto.encrypt(plaintext, key)
        envelope = bytes([1]) + encrypted
        return {
            "payload": payload,
            "plaintext_json": plaintext,
            "command_hex": command_hex,
            "encrypted_hex": encrypted.hex(),
            "envelope_hex": envelope.hex(),
            "base64": PixieEnvelope.to_base64(envelope),
        }

    def _print_local_command_debug(self, command_debug: Dict[str, Any]) -> None:
        """Print local command build stages for debugging command failures."""
        if not self._debug_enabled():
            return
        self._log_debug("Local command payload JSON: %s", json.dumps(command_debug.get('payload', {}), ensure_ascii=False))
        self._log_debug("Local command plaintext JSON: %s", command_debug.get('plaintext_json'))
        self._log_debug("Local command encrypted hex: %s", command_debug.get('encrypted_hex'))
        self._log_debug("Local command envelope hex: %s", command_debug.get('envelope_hex'))
        self._log_debug("Local command base64: %s", command_debug.get('base64'))

# Removed: AuthCredentials class and _save_credentials method - no longer needed


# Removed: PixieCrypto, _pkcs7_pad, _pkcs7_unpad - now in pixie_protocol.py
# Removed: bytes_to_hex - not needed
