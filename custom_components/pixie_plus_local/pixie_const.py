"""Shared constants for Pixie Plus Local."""

from __future__ import annotations

from datetime import timedelta

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
CONF_INVENTORY_FALLBACK_REASON = "inventory_fallback_reason"
CONF_GATEWAY_IP = "gateway_ip"
CONF_GATEWAY_IP_REQUIRED = "gateway_ip_required"
CONF_PIXIE_USERNAME = "pixie_username"
CONF_PIXIE_PASSWORD = "pixie_password"
CONF_BT_ENABLED = "bt_enabled"
CONF_BT_STATE = "bt_state"
CONF_BT_SOURCE = "bt_source"
CONF_BT_ACCESS_NODE = "bt_access_node"
CONF_BT_BETTER_CANDIDATE_SEEN = "bt_better_candidate_seen"
CONF_BT_ACCESS_NODE_PREFERENCE = "bt_access_node_preference"
CONF_COMMAND_TRANSPORT = "command_transport"
CONF_PIXIE_PIN = "pixie_pin"
CONF_BLE_MEMBERSHIP = "ble_membership"
CONF_BLE_INVENTORY = "ble_inventory"
CONF_BLE_SCAN_SECONDS = "ble_scan_seconds"
CONF_POWER_POLL_INTERVALS = "power_poll_intervals"
CONF_SYNC_HA_DEVICE_NAMES = "sync_ha_device_names"

INVENTORY_MODE_LOCAL_53216 = "local_53216"
INVENTORY_MODE_CLOUD_FALLBACK = "cloud_fallback"
INVENTORY_MODE_BLE_ADVERTISEMENT = "ble_advertisement"
INVENTORY_FALLBACK_REASON_LOCAL_53216_FAILED = "local_53216_failed"
INVENTORY_FALLBACK_REASON_UNSUPPORTED_GATEWAY = "unsupported_gateway"

COMMAND_TRANSPORT_TCP_PRIMARY = "tcp_primary"
COMMAND_TRANSPORT_BT_PRIMARY = "bt_primary"
COMMAND_TRANSPORT_TCP_ONLY = "tcp_only"
COMMAND_TRANSPORT_BT_ONLY = "bt_only"

BT_ACCESS_NODE_AUTO = "auto"
BT_ACCESS_NODE_PREFER_GATEWAY = "prefer_gateway"

ISSUE_ID_MISSING_FALLBACK_CREDENTIALS = "missing_fallback_credentials"
ISSUE_ID_GATEWAY_IP_REQUIRED = "gateway_ip_required"
ISSUE_ID_BT_PROXY_UNAVAILABLE = "bt_proxy_unavailable"
ISSUE_ID_LOCAL_INVENTORY_FALLBACK = "local_inventory_fallback"

COORDINATOR_UPDATE_INTERVAL = timedelta(seconds=10)
TIMER_POLL_INTERVAL_SECONDS = 10.0
POWER_POLL_DEFAULT_INTERVAL_SECONDS = 60
POWER_POLL_MAX_INTERVAL_SECONDS = 86400

INVENTORY_STORE_VERSION = 1
BLE_COMMAND_READY_TIMEOUT = 45.0
INVENTORY_SNAPSHOT_SAVE_DEBOUNCE_SECONDS = 1.5
BLE_FIRMWARE_SCAN_SECONDS = 20.0
BLE_FIRMWARE_SCAN_HOUR = 3
BLE_FIRMWARE_SCAN_MINUTE = 0
PIXIE_ADD_SCAN_SECONDS = 20.0
