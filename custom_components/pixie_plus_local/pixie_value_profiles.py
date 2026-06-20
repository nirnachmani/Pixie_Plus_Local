#!/usr/bin/env python3
"""Model-level value-byte decoding profiles.

Keep this file simple and editable (similar to const.py style) so new models can
be added without touching parser logic.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# Human-readable model names (optional but useful for logs/debug).
hardware_list = {
    "0102": "Gateway G3 - SGW3BTAM",
    "2213": "Smart Switch G3 - SWL600BTAM",
    "2211": "Smart Switch - Unknown Model",
    "2313": "Smart dimmer G3 - SDD300BTAM",
    "2013": "rippleSHIELD DIMMER - SDD400SFI",
    "0107": "Smart plug - ESS105/BT",
    "2702": "Flexi smart LED strip - FLP12V2M/RGBBT",
    "2402": "Flexi Streamline - FLP24V2M",
    "2403": "LED Strip Controller - LT8915DIM/BT",
    "0208": "Smart Socket Outlet - SP023/BTAM",
    "1002": "Dual Relay Control - PC206DR/R/BTAM",
    "1102": "Blind & Signal Control - PC206BS/R/BTAM",
    "2212": "Smart Switch G2 - SWL350BT",
    "2312": "Smart Dimmer G2 - SDD350BT",
    "2311": "Smart Dimmer G2 - SDD350BT",
    "3001": "Smart passive infrared motion sensor - SMS861CD/BTAM",
    "3002": "Smart passive infrared motion sensor - SMS862WF/WH/BTAM",
    "2113": "Smart Timer Switch - STS600BTAM",
    "2552": "Smart Dimmer rippleSHIELD - SDD400RS/BTAM",
    "1217": "Gate & Door Control - PC206GD/R/BTAM",
}

# Unified model capability truth.
MODEL_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "0102": {
        "is_light": False,
        "is_switch": False,
        "supports_onoff": False,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "0107": {
        "is_light": False,
        "is_switch": True,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": True,
        "supports_cover": False,
    },
    "0208": {
        "is_light": False,
        "is_switch": True,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": True,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "1002": {
        "is_light": False,
        "is_switch": True,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": True,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "1102": {
        "is_light": False,
        "is_switch": False,
        "supports_onoff": False,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": True,
    },
    "2213": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2211": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2212": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2313": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2013": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2312": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2311": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2402": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2403": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "2702": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": True,
        "supports_effects": True,
        "effect_names": ["flash", "strobe", "fade", "smooth"],
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "3001": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
        "supports_sensor": True,
        "supports_motion_sensor": True,
        "supports_photocell_sensor": False,
        "supports_hold_time": True,
        "supports_brightness_threshold": True,
        "brightness_threshold_options": ["Dark", "Night", "Evening", "Dusk", "Day"],
        "supports_motion_sensitivity": True,
        "motion_sensitivity_options": ["Low", "Medium", "High"],
    },
    "3002": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
        "supports_sensor": True,
        "supports_motion_sensor": True,
        "supports_photocell_sensor": True,
        "supports_hold_time": True,
        "supports_brightness_threshold": True,
        "brightness_threshold_options": ["Dark", "Night", "Evening", "Dusk", "Day"],
        "supports_motion_sensitivity": True,
        "motion_sensitivity_options": ["Low", "Medium", "High"],
    },
    "2113": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
        "supports_timer": True,
        "timer_modes": ["timer", "override"],
    },
    "2552": {
        "is_light": True,
        "is_switch": False,
        "supports_onoff": True,
        "supports_dimming": True,
        "supports_color": False,
        "supports_color_temp": True,
        "color_temp_min_kelvin": 3000,
        "color_temp_max_kelvin": 6500,
        "color_temp_cct_min": 0,
        "color_temp_cct_max": 255,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": False,
    },
    "1217": {
        "is_light": False,
        "is_switch": False,
        "supports_onoff": False,
        "supports_dimming": False,
        "supports_color": False,
        "supports_effects": False,
        "supports_multi_channel": False,
        "supports_usb_subentity": False,
        "supports_cover": True,
        "supports_gate": True,
        "gate_doors": 2,
    },
}


def get_model_capabilities(model_no: str) -> Dict[str, Any]:
    """Return normalized capability flags for a model number."""
    caps = MODEL_CAPABILITIES.get(str(model_no), {})
    return {
        "is_light": bool(caps.get("is_light", False)),
        "is_switch": bool(caps.get("is_switch", False)),
        "supports_onoff": bool(caps.get("supports_onoff", False)),
        "supports_dimming": bool(caps.get("supports_dimming", False)),
        "supports_color": bool(caps.get("supports_color", False)),
        "supports_color_temp": bool(caps.get("supports_color_temp", False)),
        "color_temp_min_kelvin": int(caps.get("color_temp_min_kelvin", 0)),
        "color_temp_max_kelvin": int(caps.get("color_temp_max_kelvin", 0)),
        "color_temp_cct_min": int(caps.get("color_temp_cct_min", 0)),
        "color_temp_cct_max": int(caps.get("color_temp_cct_max", 255)),
        "supports_effects": bool(caps.get("supports_effects", False)),
        "effect_names": [str(effect_name) for effect_name in caps.get("effect_names", [])],
        "supports_multi_channel": bool(caps.get("supports_multi_channel", False)),
        "supports_usb_subentity": bool(caps.get("supports_usb_subentity", False)),
        "supports_cover": bool(caps.get("supports_cover", False)),
        "supports_sensor": bool(caps.get("supports_sensor", caps.get("supports_mode", False))),
        "supports_motion_sensor": bool(caps.get("supports_motion_sensor", False)),
        "supports_photocell_sensor": bool(caps.get("supports_photocell_sensor", False)),
        "supports_timer": bool(caps.get("supports_timer", False)),
        "timer_modes": [str(mode) for mode in caps.get("timer_modes", [])],
        "supports_hold_time": bool(caps.get("supports_hold_time", False)),
        "supports_brightness_threshold": bool(caps.get("supports_brightness_threshold", False)),
        "brightness_threshold_options": [str(o) for o in caps.get("brightness_threshold_options", [])],
        "supports_motion_sensitivity": bool(caps.get("supports_motion_sensitivity", False)),
        "motion_sensitivity_options": [str(o) for o in caps.get("motion_sensitivity_options", [])],
        "supports_gate": bool(caps.get("supports_gate", False)),
        "gate_doors": int(caps.get("gate_doors", 0)),
    }


def get_model_effect_names(model_no: str) -> list[str]:
    """Return the supported effect names for a model number."""
    return get_model_capabilities(model_no)["effect_names"]


def get_supported_sensor_mode_values(model_no: str) -> list[int]:
    """Return supported normalized sensor mode values for a model."""
    capabilities = get_model_capabilities(model_no)
    if not capabilities["supports_sensor"]:
        return []

    mode_values = [0]
    if capabilities["supports_motion_sensor"]:
        mode_values.append(1)
    if capabilities["supports_photocell_sensor"]:
        mode_values.append(2)
    return mode_values


def get_sensor_select_options(model_no: str) -> list[str]:
    """Return ordered HA-facing select options for sensor-capable models."""
    capabilities = get_model_capabilities(model_no)
    if not capabilities["supports_sensor"]:
        return []

    options: list[str] = []
    if capabilities["supports_motion_sensor"]:
        options.append("motion")
    if capabilities["supports_photocell_sensor"]:
        options.append("photocell")
    options.append("switch")
    return options


def sensor_mode_value_to_option(model_no: str, mode_value: int) -> Optional[str]:
    """Map a normalized sensor mode value into a HA-facing option string."""
    capabilities = get_model_capabilities(model_no)
    if not capabilities["supports_sensor"]:
        return None
    if mode_value == 1 and capabilities["supports_motion_sensor"]:
        return "motion"
    if mode_value == 2 and capabilities["supports_photocell_sensor"]:
        return "photocell"
    if mode_value == 0:
        return "switch"
    return None


def sensor_option_to_mode_value(model_no: str, option: str) -> Optional[int]:
    """Map a HA-facing sensor option into a normalized mode value."""
    capabilities = get_model_capabilities(model_no)
    if not capabilities["supports_sensor"]:
        return None

    normalized = str(option or "").strip().lower()
    if normalized == "motion" and capabilities["supports_motion_sensor"]:
        return 1
    if normalized == "photocell" and capabilities["supports_photocell_sensor"]:
        return 2
    if normalized == "switch":
        return 0
    return None


def get_timer_select_options(model_no: str) -> list[str]:
    """Return ordered HA-facing select options for timer-capable models."""
    capabilities = get_model_capabilities(model_no)
    if not capabilities["supports_timer"]:
        return []
    return list(capabilities["timer_modes"])


def timer_mode_value_to_option(mode_value: int) -> Optional[str]:
    """Map a timer mode int value (1=timer, 2=override) into a HA-facing option string."""
    if mode_value == 1:
        return "timer"
    if mode_value == 2:
        return "override"
    return None


def timer_option_to_mode_value(option: str) -> Optional[int]:
    """Map a HA-facing timer option string into the int mode value."""
    normalized = str(option or "").strip().lower()
    if normalized == "timer":
        return 1
    if normalized == "override":
        return 2
    return None


def get_all_effect_names() -> list[str]:
    """Return all known effect names across effect-capable models."""
    seen: list[str] = []
    for model_no in MODEL_CAPABILITIES:
        for effect_name in get_model_effect_names(model_no):
            if effect_name not in seen:
                seen.append(effect_name)
    return seen


# Static cover mapping used by local press-command control.
# Users can edit this manually to match app button layout.
#
# `position` is the panel position (1..9) configured in the app.
# Runtime command byte sent in c16969 is (position - 1).
#
# Default requested mapping:
# - position 2 -> up/open
# - position 5 -> stop
# - position 8 -> down/close
COVER_ACTION_TO_POSITION_DEFAULT = {
    "open": 2,
    "up": 2,
    "stop": 5,
    "close": 8,
    "down": 8,
}

# Optional manual tilt-action mapping.
COVER_TILT_ACTION_TO_POSITION_DEFAULT: Dict[str, int] = {}


def cover_action_to_position(action: str, mapping: Optional[Dict[str, int]] = None) -> Optional[int]:
    """Resolve a cover action name into a configured button position.

    This is intentionally static/user-editable for now.
    """
    if not action:
        return None
    source = mapping or COVER_ACTION_TO_POSITION_DEFAULT
    return source.get(str(action).strip().lower())


def cover_tilt_action_to_position(
    action: str,
    mapping: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """Resolve a tilt action name into a configured button position."""
    if not action:
        return None
    source = mapping or COVER_TILT_ACTION_TO_POSITION_DEFAULT
    return source.get(str(action).strip().lower())


def resolve_cover_command_position(
    action: str,
    action_mapping: Optional[Dict[str, int]] = None,
    tilt_mapping: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """Resolve a Home Assistant cover action into a configured app button position."""
    normalized = str(action or "").strip().lower().replace("-", "_")

    if normalized in {"open_tilt", "close_tilt", "stop_tilt"}:
        return cover_tilt_action_to_position(normalized, tilt_mapping)

    return cover_action_to_position(normalized, action_mapping)


# Decoder modes are intentionally small/explicit.
MODE_RAW = "raw"
MODE_BRIGHTNESS = "brightness"
MODE_TUNABLE_WHITE = "tunable_white"
MODE_DUAL_CHANNEL = "dual_channel"
MODE_PLUG_WITH_USB = "plug_with_usb"
MODE_SENSOR_CONTROLLER = "sensor_controller"
MODE_TIMER_SWITCH = "timer_switch"
MODE_GATE = "gate"

GATE_STATE_OPEN = "open"
GATE_STATE_CLOSED = "closed"
GATE_STATE_OPENING = "opening"
GATE_STATE_CLOSING = "closing"
GATE_STATE_PAUSED = "paused"
GATE_STATE_FAULT = "fault"


def _gate_position_raw_to_percent(position_raw: int) -> int:
    """Convert a 0..1000 gate position value into HA's 0..100 percent scale."""
    return max(0, min(100, int(round(position_raw / 10.0))))


def _gate_percent_to_position_raw(position_percent: int) -> int:
    """Convert HA's 0..100 percent scale back to the gate's 0..1000 scale."""
    return max(0, min(1000, int(position_percent) * 10))


def _gate_bucket_to_percent(bucket: int) -> int:
    """Convert the coarse 0x0..0xa nibble position bucket into percent."""
    return max(0, min(100, int(bucket) * 10))


def _quantize_gate_percent_to_bucket(position_percent: int) -> int:
    """Quantize a percent value to the device's visible 10% reporting buckets."""
    bounded = max(0, min(100, int(position_percent)))
    return max(0, min(100, int(round(bounded / 10.0)) * 10))


def decode_gate_state_byte(door_index: int, value_byte: int) -> Dict[str, Any]:
    """Decode one gate door state byte using only medium/high-confidence mappings.

    Unknown or low-confidence bytes remain undecoded so callers can preserve the
    raw state and log additional context without forcing a wrong interpretation.
    """
    door_index = int(door_index)
    value_byte = int(value_byte) & 0xFF
    result: Dict[str, Any] = {
        "mode": MODE_GATE,
        "door_index": door_index,
        "value_byte": value_byte,
        "known": False,
        "state": None,
        "position_percent": None,
        "moving": False,
        "direction": None,
        "next_action": None,
        "fault": False,
        "fault_code": None,
        "sensor_closed": None,
    }

    terminal_closed = 0x06 if door_index == 0 else 0x0E
    terminal_open = 0xA0 if door_index == 0 else 0xA8
    closed_aliases = {terminal_closed}
    open_aliases = {terminal_open}
    if door_index == 0:
        # Door 1 has also been observed reporting 0x0e as its closed terminal
        # byte after a normal close cycle. Treat it as a proven closed alias.
        closed_aliases.add(0x0E)
        # Door 1 occasionally reports the door-2 open terminal byte at the end
        # of an opening run. Treat it as a proven open-side alias.
        open_aliases.add(0xA8)

    if value_byte in closed_aliases:
        result.update(
            known=True,
            state=GATE_STATE_CLOSED,
            position_percent=0,
            next_action="open",
            sensor_closed=True,
        )
        return result

    if value_byte in open_aliases:
        result.update(
            known=True,
            state=GATE_STATE_OPEN,
            position_percent=100,
            next_action="close",
            sensor_closed=False,
        )
        return result

    if value_byte == 0xEE:
        result.update(
            known=True,
            state=GATE_STATE_FAULT,
            fault=True,
            fault_code="exception",
        )
        return result

    if value_byte == 0xDA:
        result.update(
            known=True,
            state=GATE_STATE_FAULT,
            fault=True,
            fault_code="close_timeout",
        )
        return result

    bucket = (value_byte >> 4) & 0x0F
    low = value_byte & 0x0F

    if low == 0x0B and 0x01 <= bucket <= 0x09:
        result.update(
            known=True,
            state=GATE_STATE_OPENING,
            position_percent=_gate_bucket_to_percent(bucket),
            moving=True,
            direction=GATE_STATE_OPENING,
            next_action="stop",
            sensor_closed=False,
        )
        return result

    if low == 0x09 and 0x01 <= bucket <= 0x0A:
        result.update(
            known=True,
            state=GATE_STATE_CLOSING,
            position_percent=_gate_bucket_to_percent(bucket),
            moving=True,
            direction=GATE_STATE_CLOSING,
            next_action="stop",
            sensor_closed=False,
        )
        return result

    if low in {0x08, 0x0A} and 0x01 <= bucket <= 0x09:
        result.update(
            known=True,
            state=GATE_STATE_PAUSED,
            position_percent=_gate_bucket_to_percent(bucket),
            next_action="close",
            sensor_closed=False,
        )
        return result

    return result


def decode_gate_command_reply(door_index: int, state_byte: int, position_raw: int, runtime_ms: int) -> Dict[str, Any]:
    """Decode the richer d36969 gate reply into the canonical gate model."""
    result = decode_gate_state_byte(door_index, state_byte)
    result.update(
        door_index=int(door_index),
        value_byte=int(state_byte) & 0xFF,
        position_raw=max(0, int(position_raw)),
        runtime_ms=max(0, int(runtime_ms)),
        source_kind="gate_command_reply",
    )

    if state_byte == 0x0F:
        result.update(
            known=True,
            state=GATE_STATE_OPENING,
            moving=True,
            direction=GATE_STATE_OPENING,
            next_action="stop",
            sensor_closed=True,
        )
    elif state_byte == 0xA9:
        result.update(
            known=True,
            state=GATE_STATE_CLOSING,
            moving=True,
            direction=GATE_STATE_CLOSING,
            next_action="stop",
            sensor_closed=False,
        )

    if result.get("known"):
        result["position_percent"] = _gate_position_raw_to_percent(position_raw)

    return result


def gate_can_run_action(decoded_state: Optional[Dict[str, Any]], action: str) -> bool:
    """Return whether a gate action is allowed for the current decoded state."""
    normalized = str(action or "").strip().lower()
    if normalized not in {"open", "close", "stop"}:
        return False

    if not isinstance(decoded_state, dict):
        return normalized in {"open", "close"}

    state = decoded_state.get("state")
    known = bool(decoded_state.get("known"))
    fault = bool(decoded_state.get("fault"))

    if normalized == "stop":
        return state in {GATE_STATE_OPENING, GATE_STATE_CLOSING}

    if fault:
        return normalized in {"open", "close"}

    if not known:
        return normalized in {"open", "close"}

    if state == GATE_STATE_CLOSED:
        return normalized == "open"
    if state == GATE_STATE_OPEN:
        return normalized == "close"
    if state in {GATE_STATE_OPENING, GATE_STATE_CLOSING}:
        return False
    if state == GATE_STATE_PAUSED:
        return normalized == "close"

    return False


def build_gate_motion_plan(decoded_state: Optional[Dict[str, Any]], started_ms: int) -> Optional[Dict[str, Any]]:
    """Build an interpolation plan from a gate reply or decoded motion state."""
    if not isinstance(decoded_state, dict) or not decoded_state.get("known"):
        return None

    state = decoded_state.get("state")
    if state not in {GATE_STATE_OPENING, GATE_STATE_CLOSING}:
        return None

    position_raw = decoded_state.get("position_raw")
    runtime_ms = decoded_state.get("runtime_ms")
    if not isinstance(position_raw, int) or not isinstance(runtime_ms, int) or runtime_ms <= 0:
        return None

    target_position_raw = 1000 if state == GATE_STATE_OPENING else 0
    if position_raw == target_position_raw:
        return None

    return {
        "state": state,
        "source": "reply",
        "started_ms": int(started_ms),
        "duration_ms": int(runtime_ms),
        "start_position_raw": max(0, min(1000, int(position_raw))),
        "target_position_raw": target_position_raw,
    }


def build_gate_motion_plan_from_learned_duration(
    decoded_state: Optional[Dict[str, Any]],
    started_ms: int,
    learned_duration_ms: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Build an interpolation plan from a coarse gate update and learned duration."""
    if not isinstance(decoded_state, dict) or not decoded_state.get("known"):
        return None
    if not isinstance(learned_duration_ms, int) or learned_duration_ms <= 0:
        return None

    state = decoded_state.get("state")
    position_percent = decoded_state.get("position_percent")
    if state not in {GATE_STATE_OPENING, GATE_STATE_CLOSING} or not isinstance(position_percent, int):
        return None

    current_position_raw = _gate_percent_to_position_raw(position_percent)
    if state == GATE_STATE_OPENING:
        target_position_raw = 1000
        remaining_fraction = max(0.0, min(1.0, (100 - position_percent) / 100.0))
    else:
        target_position_raw = 0
        remaining_fraction = max(0.0, min(1.0, position_percent / 100.0))

    remaining_ms = int(round(learned_duration_ms * remaining_fraction))
    if remaining_ms <= 0 or current_position_raw == target_position_raw:
        return None

    return {
        "state": state,
        "source": "learned_duration",
        "started_ms": int(started_ms),
        "duration_ms": remaining_ms,
        "start_position_raw": current_position_raw,
        "target_position_raw": target_position_raw,
    }


def sync_gate_motion_plan(
    existing_plan: Optional[Dict[str, Any]],
    decoded_state: Optional[Dict[str, Any]],
    updated_ms: int,
) -> Optional[Dict[str, Any]]:
    """Reconcile an interpolation plan with a newer known gate update."""
    if not isinstance(decoded_state, dict) or not decoded_state.get("known"):
        return existing_plan if isinstance(existing_plan, dict) else None

    state = decoded_state.get("state")
    if state not in {GATE_STATE_OPENING, GATE_STATE_CLOSING}:
        return None

    fresh_plan = build_gate_motion_plan(decoded_state, updated_ms)
    if fresh_plan is not None:
        return fresh_plan

    if not isinstance(existing_plan, dict):
        return None

    start_position_raw = existing_plan.get("start_position_raw")
    target_position_raw = existing_plan.get("target_position_raw")
    duration_ms = existing_plan.get("duration_ms")
    position_percent = decoded_state.get("position_percent")
    if (
        not isinstance(start_position_raw, int)
        or not isinstance(target_position_raw, int)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
        or not isinstance(position_percent, int)
    ):
        return None

    total_distance = abs(target_position_raw - start_position_raw)
    if total_distance == 0:
        return None

    current_position_raw = _gate_percent_to_position_raw(position_percent)
    remaining_distance = abs(target_position_raw - current_position_raw)
    if remaining_distance == 0:
        return None

    speed_raw_per_ms = total_distance / float(duration_ms)
    if speed_raw_per_ms <= 0:
        return None

    remaining_ms = int(round(remaining_distance / speed_raw_per_ms))
    if remaining_ms <= 0:
        return None

    return {
        "state": state,
        "source": existing_plan.get("source") if isinstance(existing_plan.get("source"), str) else "reply",
        "started_ms": int(updated_ms),
        "duration_ms": remaining_ms,
        "start_position_raw": current_position_raw,
        "target_position_raw": target_position_raw,
    }


def estimate_gate_motion_position_raw(plan: Optional[Dict[str, Any]], now_ms: int) -> Optional[int]:
    """Estimate current gate position raw value from an active motion plan."""
    if not isinstance(plan, dict):
        return None

    start_position_raw = plan.get("start_position_raw")
    target_position_raw = plan.get("target_position_raw")
    duration_ms = plan.get("duration_ms")
    started_ms = plan.get("started_ms")
    if (
        not isinstance(start_position_raw, int)
        or not isinstance(target_position_raw, int)
        or not isinstance(duration_ms, int)
        or not isinstance(started_ms, int)
    ):
        return None

    if duration_ms <= 0:
        return max(0, min(1000, target_position_raw))

    elapsed_ms = max(0, min(int(now_ms) - started_ms, duration_ms))
    delta_raw = target_position_raw - start_position_raw
    estimated_raw = start_position_raw + int(round(delta_raw * (elapsed_ms / float(duration_ms))))
    return max(0, min(1000, estimated_raw))


def estimate_gate_motion_position_percent(plan: Optional[Dict[str, Any]], now_ms: int) -> Optional[int]:
    """Estimate current gate position percent from an active motion plan."""
    estimated_raw = estimate_gate_motion_position_raw(plan, now_ms)
    if estimated_raw is None:
        return None
    return _quantize_gate_percent_to_bucket(_gate_position_raw_to_percent(estimated_raw))

def _decode_mode_from_capabilities(model_no: str) -> str:
    """Resolve value-byte decoding mode from the model capability flags.

    Precedence matters here: USB and multi-channel devices also support on/off,
    but their value-byte encoding is more specific than plain relay semantics.
    """
    capabilities = get_model_capabilities(model_no)

    if capabilities["supports_gate"]:
        return MODE_GATE
    if capabilities["supports_timer"]:
        return MODE_TIMER_SWITCH
    if capabilities["supports_sensor"]:
        return MODE_SENSOR_CONTROLLER
    if capabilities["supports_usb_subentity"]:
        return MODE_PLUG_WITH_USB
    if capabilities["supports_multi_channel"]:
        return MODE_DUAL_CHANNEL
    if capabilities["supports_color_temp"]:
        return MODE_TUNABLE_WHITE
    if capabilities["supports_dimming"]:
        return MODE_BRIGHTNESS
    return MODE_RAW


def decode_value_byte(model_no: str, value_byte: int) -> Dict[str, Any]:
    """Decode value byte using capability-derived device semantics.

    Returns a dict with at least:
    - mode
    - value_byte
    and optionally inferred semantic fields.
    """
    mode = _decode_mode_from_capabilities(model_no)
    result: Dict[str, Any] = {
        "mode": mode,
        "value_byte": value_byte,
    }

    if mode == MODE_BRIGHTNESS:
        result["brightness_0_100"] = value_byte
        result["is_on"] = value_byte > 0
        return result

    if mode == MODE_TUNABLE_WHITE:
        brightness = max(0, min(100, value_byte - 0x80))
        result["brightness_0_100"] = brightness
        result["is_on"] = value_byte > 0x80
        return result

    if mode == MODE_DUAL_CHANNEL:
        # Bitmask inference: left/right are inferred from low bits.
        # This is intentionally simple and easy to adjust per model later.
        left_on = bool(value_byte & 0x01)
        right_on = bool(value_byte & 0x02)
        result["left_on"] = left_on
        result["right_on"] = right_on
        if left_on and right_on:
            result["channel_state"] = "both_on"
        elif left_on:
            result["channel_state"] = "left_on"
        elif right_on:
            result["channel_state"] = "right_on"
        else:
            result["channel_state"] = "both_off"
        return result

    if mode == MODE_PLUG_WITH_USB:
        # Derived from old integration websocket mapping:
        # 0c/0d -> usb off, 0e/0f -> usb on.
        # This corresponds to bit1 toggling USB state.
        result["main_relay_on"] = bool(value_byte & 0x01)
        result["usb_on"] = bool(value_byte & 0x02)
        return result

    if mode == MODE_TIMER_SWITCH:
        # Timer switch value_byte encoding (observed values):
        # 0x00 = off
        # 0x01 = timer mode on (seen when switching from override, longer durations)
        # 0x02 = override mode (light on, no timer)
        # 0x04 = timer mode on (normal turn-on, shorter durations)
        # 0x06 = timer restarting
        result["is_on"] = value_byte != 0
        if value_byte == 0x00:
            result["timer_mode"] = None
        elif value_byte == 0x02:
            result["timer_mode"] = "override"
        else:
            # Any other non-zero value (0x01, 0x04, 0x06, etc.) = timer mode on
            result["timer_mode"] = "timer"
        result["restarting"] = value_byte == 0x06
        return result

    if mode == MODE_SENSOR_CONTROLLER:
        capabilities = get_model_capabilities(model_no)
        # Sensor-controller bitfield:
        # - bit 0: relay/light state (1=on, 0=off)
        # - bit 1: motion event
        # - bit 2: motion mode
        # - bit 3: photocell mode
        sensor_mode = 0
        if capabilities["supports_photocell_sensor"] and (value_byte & 0x08):
            sensor_mode = 2
        elif capabilities["supports_motion_sensor"] and (value_byte & 0x04):
            sensor_mode = 1
        relay_on = bool(value_byte & 0x01)
        motion = bool(value_byte & 0x02) if capabilities["supports_motion_sensor"] else False
        result["mode_value"] = sensor_mode
        result["relay_on"] = relay_on
        result["motion"] = motion
        result["is_on"] = relay_on
        return result

    return result
