#!/usr/bin/env python3
"""Pixie inventory model built from Home cloud payload.

This module intentionally does not use groups/scenes because HA manages those.
Runtime state is seeded from onlineList (cloud_seed) and later expected to be
updated from local hub TCP messages (hub_update).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import json

from .pixie_value_profiles import decode_value_byte, get_model_capabilities, hardware_list


STATE_UNSET = object()


def online_value_is_online(value: Any) -> bool:
    """Return whether a cloud/GwData online value indicates the device is connected."""
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _normalize_optional_int(value: Any) -> Optional[int]:
    """Normalize optional numeric fields that may arrive as int-like strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            return int(candidate)
        except ValueError:
            return None
    return None


def derive_is_on_from_state(
    capabilities: "DeviceCapabilities",
    br: Optional[int],
    r: Optional[int],
    mode: Optional[int] = None,
    relay: Optional[int] = None,
) -> Optional[bool]:
    """Derive an on/off state from the current runtime fields.

    The protocol does not expose a universal is_on field, so this mirrors the
    current model-family rules used elsewhere in the codebase.
    
    For sensor-capable devices (supports_sensor), relay directly indicates on/off state.
    """
    if capabilities.supports_cover:
        return None

    # Timer switch devices (e.g., 2113): br=0 means off, br>0 means on
    if capabilities.supports_timer and isinstance(br, int):
        return br > 0

    # Sensor controller devices (e.g., 3001) use relay field for on/off.
    if capabilities.supports_sensor and isinstance(relay, int):
        return relay != 0

    if capabilities.supports_multi_channel or capabilities.supports_usb_subentity:
        if isinstance(r, int):
            return r != 0
        if isinstance(br, int):
            return br > 0
        return None

    if isinstance(br, int):
        return br > 0

    if isinstance(r, int) and (
        capabilities.supports_onoff
        or capabilities.supports_dimming
        or capabilities.supports_color
        or capabilities.supports_effects
    ):
        return r != 0

    return None


def parse_gateway_id(bridge_name: Optional[str]) -> Optional[str]:
    """Extract the app-visible gateway id from a bridgeName value."""
    if not bridge_name:
        return None

    parts = str(bridge_name).split("_")
    if len(parts) < 3:
        return None

    candidate = parts[-2] if parts[-1] == "" else parts[-1]
    return candidate if candidate.isdigit() else None


@dataclass(frozen=True)
class GatewayIdentity:
    """Gateway identity derived from the gateway device list record."""

    gateway_mac: str
    model_no: str
    model_name: Optional[str]
    gateway_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gateway_mac": self.gateway_mac,
            "model_no": self.model_no,
            "model_name": self.model_name,
            "gateway_id": self.gateway_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GatewayIdentity":
        return cls(
            gateway_mac=str(data.get("gateway_mac") or ""),
            model_no=str(data.get("model_no") or ""),
            model_name=str(data.get("model_name")) if data.get("model_name") is not None else None,
            gateway_id=str(data.get("gateway_id")) if data.get("gateway_id") is not None else None,
        )


@dataclass
class RuntimeState:
    """Runtime state snapshot for a device."""

    online: Any = None
    presence: str = "offline"
    is_on: Optional[bool] = None
    br: Optional[int] = None
    rgb: Optional[List[int]] = None
    effect: Optional[str] = None
    effect_speed: Optional[int] = None
    r: Optional[int] = None
    mode: Optional[int] = None
    relay: Optional[int] = None
    motion: Optional[bool] = None
    timer_total_seconds: Optional[int] = None
    timer_remaining_seconds: Optional[int] = None
    last_timer_poll_at: Optional[float] = None
    timer_needs_poll: bool = False
    hold_time_seconds: Optional[int] = None
    brightness_threshold: Optional[int] = None
    motion_sensitivity: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    last_source: str = "cloud_seed"
    last_updated_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "online": self.online,
            "presence": self.presence,
            "is_on": self.is_on,
            "br": self.br,
            "rgb": self.rgb,
            "effect": self.effect,
            "effect_speed": self.effect_speed,
            "r": self.r,
            "mode": self.mode,
            "relay": self.relay,
            "motion": self.motion,
            "timer_total_seconds": self.timer_total_seconds,
            "timer_remaining_seconds": self.timer_remaining_seconds,
            "last_timer_poll_at": self.last_timer_poll_at,
            "timer_needs_poll": self.timer_needs_poll,
            "hold_time_seconds": self.hold_time_seconds,
            "brightness_threshold": self.brightness_threshold,
            "motion_sensitivity": self.motion_sensitivity,
            "raw": self.raw,
            "last_source": self.last_source,
            "last_updated_ms": self.last_updated_ms,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeState":
        return cls(
            online=data.get("online"),
            presence=str(data.get("presence") or "offline"),
            is_on=data.get("is_on"),
            br=_normalize_optional_int(data.get("br")),
            rgb=list(data.get("rgb")) if isinstance(data.get("rgb"), list) else None,
            effect=data.get("effect"),
            effect_speed=data.get("effect_speed"),
            r=_normalize_optional_int(data.get("r")),
            mode=_normalize_optional_int(data.get("mode")),
            relay=_normalize_optional_int(data.get("relay")),
            motion=data.get("motion"),
            timer_total_seconds=_normalize_optional_int(data.get("timer_total_seconds")),
            timer_remaining_seconds=_normalize_optional_int(data.get("timer_remaining_seconds")),
            last_timer_poll_at=data.get("last_timer_poll_at"),
            timer_needs_poll=bool(data.get("timer_needs_poll", False)),
            hold_time_seconds=_normalize_optional_int(data.get("hold_time_seconds")),
            brightness_threshold=_normalize_optional_int(data.get("brightness_threshold")),
            motion_sensitivity=_normalize_optional_int(data.get("motion_sensitivity")),
            raw=dict(data.get("raw") or {}),
            last_source=str(data.get("last_source") or "snapshot"),
            last_updated_ms=data.get("last_updated_ms"),
        )


@dataclass
class DeviceStateStore:
    """Live runtime state keyed separately from static device metadata."""

    states_by_id: Dict[int, RuntimeState] = field(default_factory=dict)

    def bind(self, device_id: int, runtime_state: RuntimeState) -> RuntimeState:
        self.states_by_id[int(device_id)] = runtime_state
        return runtime_state

    def get(self, device_id: int) -> Optional[RuntimeState]:
        return self.states_by_id.get(int(device_id))

    def apply_device_update(
        self,
        devices_by_id: Dict[int, "DeviceRecord"],
        device_id: int,
        *,
        source: str,
        online: Any = STATE_UNSET,
        presence: Any = STATE_UNSET,
        br: Any = STATE_UNSET,
        rgb: Any = STATE_UNSET,
        effect: Any = STATE_UNSET,
        effect_speed: Any = STATE_UNSET,
        r: Any = STATE_UNSET,
        mode: Any = STATE_UNSET,
        relay: Any = STATE_UNSET,
        motion: Any = STATE_UNSET,
        timer_total_seconds: Any = STATE_UNSET,
        timer_remaining_seconds: Any = STATE_UNSET,
        last_timer_poll_at: Any = STATE_UNSET,
        timer_needs_poll: Any = STATE_UNSET,
        hold_time_seconds: Any = STATE_UNSET,
        brightness_threshold: Any = STATE_UNSET,
        motion_sensitivity: Any = STATE_UNSET,
        raw: Any = STATE_UNSET,
        updated_ms: Optional[int] = None,
    ) -> Optional[RuntimeState]:
        """Apply a normalized runtime patch to one device state object."""
        device_id = int(device_id)
        inv_rec = devices_by_id.get(device_id)
        runtime = self.get(device_id)
        if not inv_rec or runtime is None:
            return None

        if online is not STATE_UNSET:
            runtime.online = online
        if presence is not STATE_UNSET:
            runtime.presence = presence
        elif online is not STATE_UNSET:
            runtime.presence = "online" if online_value_is_online(online) else "offline"

        if br is not STATE_UNSET:
            runtime.br = _normalize_optional_int(br)
        if rgb is not STATE_UNSET:
            runtime.rgb = list(rgb) if isinstance(rgb, list) else rgb
        if effect is not STATE_UNSET:
            runtime.effect = effect
        if effect_speed is not STATE_UNSET:
            runtime.effect_speed = effect_speed
        if r is not STATE_UNSET:
            runtime.r = _normalize_optional_int(r)
        if mode is not STATE_UNSET:
            runtime.mode = _normalize_optional_int(mode)
        if relay is not STATE_UNSET:
            runtime.relay = _normalize_optional_int(relay)
        if motion is not STATE_UNSET:
            runtime.motion = motion
        if timer_total_seconds is not STATE_UNSET:
            runtime.timer_total_seconds = _normalize_optional_int(timer_total_seconds)
        if timer_remaining_seconds is not STATE_UNSET:
            runtime.timer_remaining_seconds = _normalize_optional_int(timer_remaining_seconds)
        if last_timer_poll_at is not STATE_UNSET:
            runtime.last_timer_poll_at = last_timer_poll_at
        if timer_needs_poll is not STATE_UNSET:
            runtime.timer_needs_poll = bool(timer_needs_poll)
        if hold_time_seconds is not STATE_UNSET:
            runtime.hold_time_seconds = _normalize_optional_int(hold_time_seconds)
        if brightness_threshold is not STATE_UNSET:
            runtime.brightness_threshold = _normalize_optional_int(brightness_threshold)
        if motion_sensitivity is not STATE_UNSET:
            runtime.motion_sensitivity = _normalize_optional_int(motion_sensitivity)
        if raw is not STATE_UNSET:
            runtime.raw = raw

        runtime.is_on = derive_is_on_from_state(
            inv_rec.capabilities,
            runtime.br,
            runtime.r,
            runtime.mode,
            runtime.relay,
        )
        runtime.last_source = source
        runtime.last_updated_ms = updated_ms if updated_ms is not None else int(datetime.now().timestamp() * 1000)
        return runtime

    def apply_gwdata_bulk(
        self,
        devices_by_id: Dict[int, "DeviceRecord"],
        records: List[Dict[str, Any]],
        source: str,
        *,
        full_snapshot: bool = False,
    ) -> int:
        """Apply GwData bulk records to runtime state for all known devices."""
        if not records:
            return 0

        applied = 0
        present_ids = set()
        now_ms = int(datetime.now().timestamp() * 1000)

        for rec_data in records:
            try:
                dev_id = int(rec_data.get("id"))
            except Exception:
                continue

            inv_rec = devices_by_id.get(dev_id)
            if not inv_rec or self.get(dev_id) is None:
                continue

            present_ids.add(dev_id)
            online_value = rec_data.get("online")
            update_br = STATE_UNSET
            update_r = STATE_UNSET
            update_mode = STATE_UNSET
            update_relay = STATE_UNSET
            update_motion = STATE_UNSET

            # Handle mode/relay for sensor-capable devices.
            mode_val = rec_data.get("mode")
            if _normalize_optional_int(mode_val) is not None:
                update_mode = mode_val

            relay_val = rec_data.get("relay")
            if _normalize_optional_int(relay_val) is not None:
                update_relay = relay_val

            br_obj = rec_data.get("br")
            if isinstance(br_obj, dict):
                if inv_rec.capabilities.supports_sensor:
                    raw_value = br_obj.get("raw")
                    if isinstance(raw_value, int):
                        interpreted = decode_value_byte(inv_rec.model_no, raw_value)
                        if interpreted.get("mode") == "sensor_controller":
                            mode_value = interpreted.get("mode_value")
                            relay_on = interpreted.get("relay_on")
                            motion = interpreted.get("motion")

                            if update_mode is STATE_UNSET and isinstance(mode_value, int):
                                update_mode = mode_value
                            if update_relay is STATE_UNSET and isinstance(relay_on, bool):
                                update_relay = 1 if relay_on else 0
                            if isinstance(relay_on, bool):
                                update_br = 100 if relay_on else 0
                            if isinstance(motion, bool):
                                update_motion = motion
                elif inv_rec.capabilities.supports_timer:
                    pct = br_obj.get("pct")
                    if isinstance(pct, int):
                        # Bulk br for timer: 0=off, 1=timer, 2=override
                        update_br = 100 if pct > 0 else 0
                        if pct == 1:
                            update_mode = 1
                        elif pct == 2:
                            update_mode = 2
                elif br_obj.get("type") == "single":
                    pct = br_obj.get("pct")
                    if isinstance(pct, int):
                        update_br = max(0, min(100, int(pct)))
                elif br_obj.get("type") == "multi":
                    left_on = bool(br_obj.get("ch1"))
                    right_on = bool(br_obj.get("ch2"))
                    if left_on and right_on:
                        update_r = 3
                    elif left_on:
                        update_r = 1
                    elif right_on:
                        update_r = 2
                    else:
                        update_r = 0

                    if inv_rec.model_no == "0107":
                        update_br = 100 if bool(update_r & 0x01) else 0

            runtime = self.apply_device_update(
                devices_by_id,
                dev_id,
                source=source,
                online=online_value,
                br=update_br,
                r=update_r,
                mode=update_mode,
                relay=update_relay,
                motion=update_motion,
                updated_ms=now_ms,
            )
            if runtime is None:
                continue
            applied += 1

        if full_snapshot:
            for inv_id, inv_rec in devices_by_id.items():
                if inv_id not in present_ids:
                    runtime = self.apply_device_update(
                        devices_by_id,
                        inv_id,
                        source=source,
                        online=0,
                        presence="offline",
                        updated_ms=now_ms,
                    )
                    if runtime is None:
                        continue

        return applied


@dataclass
class DeviceCapabilities:
    """Capability profile derived from device type range and record flags.

    Rules (reverse-engineered from the app's native Java layer):
    - is_light           : expose the primary endpoint as a HA light
    - is_switch          : expose the primary endpoint as a HA switch
    - supports_dimming  : model supports a brightness payload/state
    - supports_color    : model supports RGB payload/state
    - supports_effects  : model supports named effect commands/state
    - supports_multi_channel : device record has both 'left_name' and
                          'right_name' fields (only dual-output devices do)
    - supports_cover    : device type == 11
    - supports_usb_subentity : model 0107 (type=1, stype=7) only
    """

    is_light: bool = False
    is_switch: bool = False
    supports_onoff: bool = True
    supports_dimming: bool = False
    supports_color: bool = False
    supports_effects: bool = False
    effect_names: List[str] = field(default_factory=list)
    supports_multi_channel: bool = False
    supports_usb_subentity: bool = False
    supports_cover: bool = False
    supports_sensor: bool = False
    supports_motion_sensor: bool = False
    supports_photocell_sensor: bool = False
    supports_timer: bool = False
    timer_modes: List[str] = field(default_factory=list)
    supports_hold_time: bool = False
    supports_brightness_threshold: bool = False
    brightness_threshold_options: List[str] = field(default_factory=list)
    supports_motion_sensitivity: bool = False
    motion_sensitivity_options: List[str] = field(default_factory=list)
    capability_hints: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_light": self.is_light,
            "is_switch": self.is_switch,
            "supports_onoff": self.supports_onoff,
            "supports_dimming": self.supports_dimming,
            "supports_color": self.supports_color,
            "supports_effects": self.supports_effects,
            "effect_names": list(self.effect_names),
            "supports_multi_channel": self.supports_multi_channel,
            "supports_usb_subentity": self.supports_usb_subentity,
            "supports_cover": self.supports_cover,
            "supports_sensor": self.supports_sensor,
            "supports_motion_sensor": self.supports_motion_sensor,
            "supports_photocell_sensor": self.supports_photocell_sensor,
            "supports_timer": self.supports_timer,
            "timer_modes": list(self.timer_modes),
            "supports_hold_time": self.supports_hold_time,
            "supports_brightness_threshold": self.supports_brightness_threshold,
            "brightness_threshold_options": list(self.brightness_threshold_options),
            "supports_motion_sensitivity": self.supports_motion_sensitivity,
            "motion_sensitivity_options": list(self.motion_sensitivity_options),
            "capability_hints": dict(self.capability_hints),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceCapabilities":
        return cls(
            is_light=bool(data.get("is_light", False)),
            is_switch=bool(data.get("is_switch", False)),
            supports_onoff=bool(data.get("supports_onoff", True)),
            supports_dimming=bool(data.get("supports_dimming", False)),
            supports_color=bool(data.get("supports_color", False)),
            supports_effects=bool(data.get("supports_effects", False)),
            effect_names=list(data.get("effect_names") or []),
            supports_multi_channel=bool(data.get("supports_multi_channel", False)),
            supports_usb_subentity=bool(data.get("supports_usb_subentity", False)),
            supports_cover=bool(data.get("supports_cover", False)),
            supports_sensor=bool(data.get("supports_sensor", data.get("supports_mode", False))),
            supports_motion_sensor=bool(data.get("supports_motion_sensor", False)),
            supports_photocell_sensor=bool(data.get("supports_photocell_sensor", False)),
            supports_timer=bool(data.get("supports_timer", False)),
            timer_modes=list(data.get("timer_modes") or []),
            supports_hold_time=bool(data.get("supports_hold_time", False)),
            supports_brightness_threshold=bool(data.get("supports_brightness_threshold", False)),
            brightness_threshold_options=list(data.get("brightness_threshold_options") or []),
            supports_motion_sensitivity=bool(data.get("supports_motion_sensitivity", False)),
            motion_sensitivity_options=list(data.get("motion_sensitivity_options") or []),
            capability_hints=dict(data.get("capability_hints") or {}),
        )


@dataclass
class DeviceRecord:
    """Normalized device entry."""

    id: int
    type: int
    stype: int
    model_no: str
    name: str
    mac: str
    version: Optional[int] = None

    left_name: Optional[str] = None
    right_name: Optional[str] = None

    rooms: List[Any] = field(default_factory=list)
    import_mode: Optional[int] = None

    capabilities: DeviceCapabilities = field(default_factory=DeviceCapabilities)
    runtime: RuntimeState = field(default_factory=RuntimeState)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "stype": self.stype,
            "model_no": self.model_no,
            "name": self.name,
            "mac": self.mac,
            "version": self.version,
            "left_name": self.left_name,
            "right_name": self.right_name,
            "rooms": list(self.rooms),
            "import_mode": self.import_mode,
            "capabilities": self.capabilities.to_dict(),
            "runtime": self.runtime.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceRecord":
        return cls(
            id=int(data.get("id", 0)),
            type=int(data.get("type", 0)),
            stype=int(data.get("stype", 0)),
            model_no=str(data.get("model_no") or ""),
            name=str(data.get("name") or ""),
            mac=str(data.get("mac") or ""),
            version=data.get("version"),
            left_name=data.get("left_name"),
            right_name=data.get("right_name"),
            rooms=list(data.get("rooms") or []),
            import_mode=data.get("import_mode"),
            capabilities=DeviceCapabilities.from_dict(dict(data.get("capabilities") or {})),
            runtime=RuntimeState.from_dict(dict(data.get("runtime") or {})),
        )


@dataclass
class PixieInventory:
    """Top-level inventory model built after login."""

    home_id: str
    home_name: Optional[str]
    user_id: str
    net_id: Optional[str]
    mesh_net: Optional[str]
    mesh_net2: Optional[str]
    generated_at: datetime
    gateway: Optional[GatewayIdentity] = None
    devices_by_id: Dict[int, DeviceRecord] = field(default_factory=dict)
    state_store: DeviceStateStore = field(default_factory=DeviceStateStore)

    @staticmethod
    def _model_no(device_obj: Dict[str, Any]) -> str:
        return f"{int(device_obj.get('type', 0)):02d}{int(device_obj.get('stype', 0)):02d}"

    @staticmethod
    def _infer_capabilities(device_obj: Dict[str, Any], online_obj: Dict[str, Any]) -> DeviceCapabilities:
        dtype = int(device_obj.get("type", 0))
        dstype = int(device_obj.get("stype", 0))
        model_no = f"{dtype:02d}{dstype:02d}"
        cap = DeviceCapabilities()
        model_caps = get_model_capabilities(model_no)

        cap.is_light = model_caps["is_light"]
        cap.is_switch = model_caps["is_switch"]
        cap.supports_onoff = model_caps["supports_onoff"]
        cap.supports_dimming = model_caps["supports_dimming"]
        cap.supports_color = model_caps["supports_color"]
        cap.supports_effects = model_caps["supports_effects"]
        cap.effect_names = model_caps["effect_names"]
        cap.supports_multi_channel = model_caps["supports_multi_channel"]
        cap.supports_usb_subentity = model_caps["supports_usb_subentity"]
        cap.supports_cover = model_caps["supports_cover"]
        cap.supports_sensor = model_caps["supports_sensor"]
        cap.supports_motion_sensor = model_caps["supports_motion_sensor"]
        cap.supports_photocell_sensor = model_caps["supports_photocell_sensor"]
        cap.supports_timer = model_caps["supports_timer"]
        cap.timer_modes = model_caps["timer_modes"]
        cap.supports_hold_time = model_caps["supports_hold_time"]
        cap.supports_brightness_threshold = model_caps["supports_brightness_threshold"]
        cap.brightness_threshold_options = model_caps["brightness_threshold_options"]
        cap.supports_motion_sensitivity = model_caps["supports_motion_sensitivity"]
        cap.motion_sensitivity_options = model_caps["motion_sensitivity_options"]

        cap.capability_hints = {
            "model_no": model_no,
        }
        return cap

    @classmethod
    def _extract_gateway_identity(cls, devices: List[Dict[str, Any]]) -> Optional[GatewayIdentity]:
        for device_obj in devices:
            model_no = cls._model_no(device_obj)
            if model_no != "0102":
                continue

            gateway_mac = str(device_obj.get("mac") or "")
            return GatewayIdentity(
                gateway_mac=gateway_mac,
                model_no=model_no,
                model_name=hardware_list.get(model_no),
                gateway_id=parse_gateway_id(device_obj.get("bridgeName")),
            )

        return None

    @classmethod
    def from_home_object(cls, home_obj: Dict[str, Any], user_id: str, source: str = "cloud_seed") -> "PixieInventory":
        now_ms = int(datetime.now().timestamp() * 1000)

        inv = cls(
            home_id=str(home_obj.get("objectId", "")),
            home_name=str(home_obj.get("name")) if home_obj.get("name") is not None else None,
            user_id=user_id,
            net_id=str(home_obj.get("netID")) if home_obj.get("netID") is not None else None,
            mesh_net=str(home_obj.get("meshNet")) if home_obj.get("meshNet") is not None else None,
            mesh_net2=str(home_obj.get("meshNet2")) if home_obj.get("meshNet2") is not None else None,
            generated_at=datetime.now(),
        )

        online_map = home_obj.get("onlineList") or {}
        devices = home_obj.get("deviceList") or []
        inv.gateway = cls._extract_gateway_identity(devices)

        for d in devices:
            dev_id = int(d.get("id", 0))
            online = online_map.get(str(dev_id), {}) if isinstance(online_map, dict) else {}
            online_value = online.get("online") if isinstance(online, dict) else None
            is_online = online_value_is_online(online_value)

            rec = DeviceRecord(
                id=dev_id,
                type=int(d.get("type", 0)),
                stype=int(d.get("stype", 0)),
                model_no=cls._model_no(d),
                name=str(d.get("name") or d.get("bridgeName") or d.get("mac") or f"device_{dev_id}"),
                mac=str(d.get("mac", "")),
                version=d.get("version"),
                left_name=d.get("left_name"),
                right_name=d.get("right_name"),
                rooms=list(d.get("rooms") or []),
                import_mode=d.get("importMode"),
            )

            rec.capabilities = cls._infer_capabilities(d, online)

            # Runtime state is seeded ONLY from onlineList.
            runtime_state = RuntimeState(
                online=online_value,
                presence="online" if is_online else "offline",
                br=_normalize_optional_int(online.get("br")),
                r=_normalize_optional_int(online.get("r")),
                mode=_normalize_optional_int(online.get("mode")),
                relay=_normalize_optional_int(online.get("relay")),
                raw=dict(online),
                last_source=source,
                last_updated_ms=now_ms,
            )
            # Seed timer duration from device state (already in seconds).
            if rec.capabilities.supports_timer:
                dev_state = d.get("state") if isinstance(d.get("state"), dict) else {}
                timer_seconds = _normalize_optional_int(dev_state.get("second"))
                if timer_seconds is not None and timer_seconds > 0:
                    runtime_state.timer_total_seconds = timer_seconds
                # Default to timer mode when light is off (matches device behaviour:
                # turning on without explicitly selecting override always starts timer).
                if runtime_state.mode is None:
                    runtime_state.mode = 1

            runtime_state.is_on = derive_is_on_from_state(
                rec.capabilities,
                runtime_state.br,
                runtime_state.r,
                runtime_state.mode,
                runtime_state.relay,
            )
            rec.runtime = inv.state_store.bind(rec.id, runtime_state)

            inv.devices_by_id[rec.id] = rec

        return inv

    def apply_gwdata_bulk(self, records: List[Dict[str, Any]], source: str, *, full_snapshot: bool = False) -> int:
        """Apply GwData bulk records to runtime state for all known devices."""
        return self.state_store.apply_gwdata_bulk(
            self.devices_by_id,
            records,
            source,
            full_snapshot=full_snapshot,
        )

    def apply_device_update(self, device_id: int, *, source: str, **kwargs: Any) -> Optional[RuntimeState]:
        """Apply a normalized runtime patch to one inventory device."""
        return self.state_store.apply_device_update(
            self.devices_by_id,
            device_id,
            source=source,
            **kwargs,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "home_id": self.home_id,
            "home_name": self.home_name,
            "user_id": self.user_id,
            "net_id": self.net_id,
            "mesh_net": self.mesh_net,
            "mesh_net2": self.mesh_net2,
            "generated_at": self.generated_at.isoformat(),
            "gateway": self.gateway.to_dict() if self.gateway is not None else None,
            "devices": [
                self.devices_by_id[dev_id].to_dict()
                for dev_id in sorted(self.devices_by_id)
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PixieInventory":
        generated_at_raw = data.get("generated_at")
        try:
            generated_at = datetime.fromisoformat(str(generated_at_raw))
        except Exception:
            generated_at = datetime.now()

        inv = cls(
            home_id=str(data.get("home_id") or ""),
            home_name=str(data.get("home_name")) if data.get("home_name") is not None else None,
            user_id=str(data.get("user_id") or "unknown"),
            net_id=str(data.get("net_id")) if data.get("net_id") is not None else None,
            mesh_net=str(data.get("mesh_net")) if data.get("mesh_net") is not None else None,
            mesh_net2=str(data.get("mesh_net2")) if data.get("mesh_net2") is not None else None,
            generated_at=generated_at,
            gateway=GatewayIdentity.from_dict(dict(data.get("gateway") or {})) if isinstance(data.get("gateway"), dict) else None,
        )

        for rec_obj in data.get("devices") or []:
            if not isinstance(rec_obj, dict):
                continue
            rec = DeviceRecord.from_dict(rec_obj)
            rec.runtime = inv.state_store.bind(rec.id, rec.runtime)
            inv.devices_by_id[rec.id] = rec

        return inv

    def debug_lines(self) -> List[str]:
        lines: List[str] = []
        lines.append(
            f"Inventory: home={self.home_id} devices={len(self.devices_by_id)} netID={self.net_id} meshNet={self.mesh_net} meshNet2={self.mesh_net2}"
        )
        if self.gateway:
            lines.append(
                "Gateway: mac={mac} model={model_no} model_name={model_name} gateway_id={gateway_id}".format(
                    mac=self.gateway.gateway_mac,
                    model_no=self.gateway.model_no,
                    model_name=self.gateway.model_name,
                    gateway_id=self.gateway.gateway_id,
                )
            )

        for dev_id in sorted(self.devices_by_id.keys()):
            d = self.devices_by_id[dev_id]
            caps = []
            if d.capabilities.supports_onoff:
                caps.append("onoff")
            if d.capabilities.supports_dimming:
                caps.append("dimming")
            if d.capabilities.supports_color:
                caps.append("color")
            if d.capabilities.supports_effects:
                caps.append("effects")
            if d.capabilities.supports_multi_channel:
                caps.append("multi_channel")
            if d.capabilities.supports_usb_subentity:
                caps.append("usb_subentity")
            if d.capabilities.supports_cover:
                caps.append("cover")
            if d.capabilities.supports_sensor:
                caps.append("sensor")
            if d.capabilities.supports_motion_sensor:
                caps.append("motion_sensor")
            if d.capabilities.supports_photocell_sensor:
                caps.append("photocell_sensor")

            lines.append(
                " - id={id} model={model} name={name} caps=[{caps}] state_seed=(presence={presence}, is_on={is_on}, br={br}, r={r}, mode={mode}, relay={relay}) src={src}".format(
                    id=d.id,
                    model=d.model_no,
                    name=d.name,
                    caps=", ".join(caps),
                    presence=d.runtime.presence,
                    is_on=d.runtime.is_on,
                    br=d.runtime.br,
                    r=d.runtime.r,
                    mode=d.runtime.mode,
                    relay=d.runtime.relay,
                    src=d.runtime.last_source,
                )
            )

        return lines

    def debug_lines_verbose(self) -> List[str]:
        """Verbose inventory dump for protocol/debug analysis."""
        lines: List[str] = []
        lines.append(
            f"Inventory(verbose): home={self.home_id} devices={len(self.devices_by_id)} netID={self.net_id} meshNet={self.mesh_net} meshNet2={self.mesh_net2}"
        )
        if self.gateway:
            lines.append(
                " - gateway mac={mac} model={model_no} model_name={model_name} gateway_id={gateway_id}".format(
                    mac=self.gateway.gateway_mac,
                    model_no=self.gateway.model_no,
                    model_name=self.gateway.model_name,
                    gateway_id=self.gateway.gateway_id,
                )
            )

        for dev_id in sorted(self.devices_by_id.keys()):
            d = self.devices_by_id[dev_id]
            lines.append(f" - device id={d.id} name={d.name} model={d.model_no} mac={d.mac}")
            lines.append(
                "   identity: type={type} stype={stype} version={version} left_name={left} right_name={right}".format(
                    type=d.type,
                    stype=d.stype,
                    version=d.version,
                    left=d.left_name,
                    right=d.right_name,
                )
            )
            lines.append(
                "   network: importMode={im}".format(
                    im=d.import_mode,
                )
            )
            lines.append(
                "   flags: rooms={rooms}".format(
                    rooms=d.rooms,
                )
            )
            lines.append(
                "   capabilities: light={light} switch={switch} onoff={onoff} dimming={dim} color={color} effects={effects} multi_channel={multi} usb_subentity={usb} supports_cover={cover}".format(
                    light=d.capabilities.is_light,
                    switch=d.capabilities.is_switch,
                    onoff=d.capabilities.supports_onoff,
                    dim=d.capabilities.supports_dimming,
                    color=d.capabilities.supports_color,
                    effects=d.capabilities.supports_effects,
                    multi=d.capabilities.supports_multi_channel,
                    usb=d.capabilities.supports_usb_subentity,
                    cover=d.capabilities.supports_cover,
                )
            )
            lines.append(
                f"   capability_hints: {json.dumps(d.capabilities.capability_hints, ensure_ascii=False, sort_keys=True)}"
            )
            lines.append(
                "   runtime: presence={presence} online={online} is_on={is_on} br={br} r={r} mode={mode} relay={relay} src={src} last_updated_ms={ts}".format(
                    presence=d.runtime.presence,
                    online=d.runtime.online,
                    is_on=d.runtime.is_on,
                    br=d.runtime.br,
                    r=d.runtime.r,
                    mode=d.runtime.mode,
                    relay=d.runtime.relay,
                    src=d.runtime.last_source,
                    ts=d.runtime.last_updated_ms,
                )
            )
            lines.append(
                f"   runtime_raw: {json.dumps(d.runtime.raw, ensure_ascii=False, sort_keys=True)}"
            )

        return lines
