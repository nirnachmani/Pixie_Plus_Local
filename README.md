# Pixie Plus Local for Home Assistant

Pixie Plus Local is a Home Assistant custom integration for SAL Pixie Plus devices.

Unlike the older Pixie Plus integration, this one controls the gateway locally over your LAN instead of using the cloud. It still uses your Pixie account once during setup to retrieve the metadata required for local access, but after that the integration runs against the gateway directly. I attempted to implement Bluetooth support as well but Pixie Plus devices are not compatible with Home Assistant Bluetooth (Pixie devices ignore CCCD write which is required for a successful handshake.) As such, the integration still requires a Pixie Gateway to work. 

## Features

- Automatic gateway discovery with manual IP option (e.g. if gateway is on another subnet) 
- Local control through the Pixie Plus gateway
- Local push-style state updates from the live gateway session
- Lights, dimmers, switches, smart plugs, RGB strip control, blinds, timer, sensors and gate control
- Blind button mapping is now done through the UI

The integration intentionally does not implement Pixie Plus groups, scenes, schedules, or timers. Home Assistant already covers those use cases more cleanly.

## Requirements

- A Pixie Plus gateway on the same local network as Home Assistant
- All devices already paired and configured in the official Pixie Plus app
- A working Pixie account for the initial setup step

This integration is for gateway-based Pixie Plus systems. It does not expose direct Bluetooth-only control.

## Supported Devices

The current code includes support for these model families:

- Gateway G3 - SGW3BTAM
- Smart Switch G3 - SWL600BTAM
- Smart Dimmer G3 - SDD300BTAM
- Smart Switch G2 - SWL350BT
- Smart Dimmer G2 - SDD350BT
- Smart plug - ESS105/BT
- Smart Socket Outlet - SP023/BTAM
- Dual Relay Control - PC206DR/R/BTAM
- Blind and Signal Control - PC206BS/R/BTAM
- Flexi Smart LED Strip - FLP12V2M/RGBBT
- Flexi Streamline - FLP24V2M
- LED Strip Controller - LT8915DIM/BT
- Smart Passive Infrared Motion Sensor - SMS861CD/BTAM
- Smart Passive Infrared Motion Sensor - SMS862WF/WH/BTAM
- Smart timer switch - STS600BTAM - timer duration setting and countdown might not work - see Issues 
- Gate & Door Control - PC206GD/R/BTAM

For compatible models, the integration supports on/off, dimming, RGB color, and built-in effects where those capabilities exist.

## Installation

### HACS

1. Open HACS.
2. Go to the custom repositories section (HACS →  ⋮ (top right corner) → Custom repositories).
3. Add `https://github.com/nirnachmani/Pixie_Plus_local` as an `Integration` repository.
4. Search for `Pixie Plus Local` in HACS and download it.
5. Restart Home Assistant.
6. Go to Settings > Devices & Services > Add Integration.
7. Search for `Pixie Plus Local` and complete the setup flow.
8. Enter your Pixie Plus username and password when prompted.

### Manual

1. Copy this integration into your Home Assistant custom components directory so the final path is:

```text
config/custom_components/pixie_plus_local/
```

2. Restart Home Assistant.
3. Go to Settings > Devices & Services > Add Integration.
4. Search for `Pixie Plus Local` and complete the setup flow.
5. Enter your Pixie Plus username and password when prompted.


## Notes on migration from the old integration

Delete the old integration before installing the current one (theoretically they can both work at the same time but HA will create a second entity for all devices.)

Entity ID should remain the same as with the old integration but check that this is the case, especially for devices with multiple entities. 

## Blind Configuration

Blind controllers require one extra configuration step because the Pixie system exposes blind commands as button positions in the app's control panel.

During setup, if blind devices are found, Home Assistant will ask you to map blind actions to button positions.

The default mapping is:

- Open: `2`
- Stop: `5`
- Close: `8`

Optional tilt actions can also be mapped:

- Open tilt
- Stop tilt
- Close tilt

The button positions correspond to the 3x3 layout used in the Pixie app:

```text
1 2 3
4 5 6
7 8 9
```

Important notes:

- Use the original app button positions for the blind controller.
- If the Pixie app visually moves a button, the integration still needs the original button position.
- If you have multiple blind controllers, each controller can be configured separately.
- You can change blind mappings later from the integration's options flow in Home Assistant.

Blind entities are exposed as assumed-state covers. The integration sends the configured button commands locally, but it does not derive a state from the gateway.

## Known Limitations

- The integration requires a Pixie Plus gateway.
- Devices must already be set up in the official Pixie app.
- Pixie cloud login is required during the initial setup flow.
- The integration will attempt local inventory refresh but if unsuccessful will operate in a hybrid mode where it requires the cloud on each startup to build the inventory but will otherwise use local communication. In that case, the Pixie Plus username and password will be stored in Home Assistant.
- Groups, scenes, schedules, and timers from the Pixie ecosystem are not implemented.

## Troubleshooting

If setup fails:

- Verify your Pixie username and password.
- Make sure Home Assistant and the Pixie gateway are on the same LAN.
- Confirm that the gateway and devices are already working in the Pixie app.
- Check the Home Assistant logs for `pixie_plus_local` messages.

If blind actions are wrong:

- Re-open the integration options.
- Adjust the button-position mapping for that controller.
- Use the original Pixie app button positions, not the visual position after rearranging buttons.

## Status

This is a custom integration built from reverse engineering and local protocol work. It has been developed against one real-world setup and should still be considered community-supported. Development heavily relied on AI. 

Use it, adapt it, and inspect the code if needed.
