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
        "supports_mode": True,
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
        "supports_effects": bool(caps.get("supports_effects", False)),
        "effect_names": [str(effect_name) for effect_name in caps.get("effect_names", [])],
        "supports_multi_channel": bool(caps.get("supports_multi_channel", False)),
        "supports_usb_subentity": bool(caps.get("supports_usb_subentity", False)),
        "supports_cover": bool(caps.get("supports_cover", False)),
        "supports_mode": bool(caps.get("supports_mode", False)),
    }


def get_model_effect_names(model_no: str) -> list[str]:
    """Return the supported effect names for a model number."""
    return get_model_capabilities(model_no)["effect_names"]


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
MODE_DUAL_CHANNEL = "dual_channel"
MODE_PLUG_WITH_USB = "plug_with_usb"
MODE_SENSOR_CONTROLLER = "sensor_controller"

def _decode_mode_from_capabilities(model_no: str) -> str:
    """Resolve value-byte decoding mode from the model capability flags.

    Precedence matters here: USB and multi-channel devices also support on/off,
    but their value-byte encoding is more specific than plain relay semantics.
    """
    capabilities = get_model_capabilities(model_no)

    if capabilities["supports_mode"]:
        return MODE_SENSOR_CONTROLLER
    if capabilities["supports_usb_subentity"]:
        return MODE_PLUG_WITH_USB
    if capabilities["supports_multi_channel"]:
        return MODE_DUAL_CHANNEL
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

    if mode == MODE_SENSOR_CONTROLLER:
        # Value-byte bitfield observed for model 3001 updates:
        # - bit 2: mode (1=sensor, 0=manual)
        # - bit 0: relay/light state (1=on, 0=off)
        # - bit 1: motion (1=detected, 0=clear)
        sensor_mode = 1 if (value_byte & 0x04) else 0
        relay_on = bool(value_byte & 0x01)
        motion = bool(value_byte & 0x02)
        result["mode_value"] = sensor_mode
        result["relay_on"] = relay_on
        result["motion"] = motion
        result["is_on"] = relay_on
        return result

    return result
