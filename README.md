# Pixie Plus Local for Home Assistant

## I need your help !! 

Part of this integration doesn't work and I need your help to figure out why (the integration is functional but not completely local).

The integration communicates with the Pixie gateway on two ports, 41578 and 53216. Each port uses different encryption. Port 41578 is used to send commands and get updates to/from the gateway. Port 53216 provides an initial snapshot of all the Pixie devices. On my system the integration works with both ports, but for other users port 53216 communication doesn't work and I can't figure out why. For now I manage this issue by getting the snapshot of all the Pixie devices from the cloud if port 53216 communications fails. This happens every time the integration loads. I also store the device snapshot in HA to be used if there is no access to the cloud (in that case if you add or remove devices that won't be reflected in the integration). However, I prefer to figure out the underlying issue so that the integration can be truly local.

This is where you can help me by sending me debug logging from the integration and corresponding capture of port 53216 communication. This requires some technical knowledge. 

I only need this data from systems where port 53216 fails. In those cases you will see a warning in the log:

```
Pixie Plus Local is using cloud-assisted inventory mode because direct local inventory was unavailable during setup
```

If you don't see this message it means the integration is using port 53216.


To get debug logging you need to add the following to configuration.yaml:

```
logger:
  default: warn
  logs:
    custom_components.pixie_plus_local: debug  
```

I will need all logging related to pixie_plus_local. Note that if you filter the log you won't see the lines that contain the data that I need.

To capture the traffic you will need to install [mitmproxy](https://www.mitmproxy.org) on a computer and WireGuard on the mobile phone which you use for the Pixie Plus app. You  need to run mitmproxy in [WireGaurd Mode](https://docs.mitmproxy.org/stable/concepts/modes/#wireguard) and configure WireGuard on the phone accordingly (it's easy, mitmproxy gives you a QR code to use with WireGaurd). Once they are connected you need to start the Pixie Plus app. You will see that one of the captures is TCP traffic on port 53216 - I need the data that is transferred in that port. An example of the data can be seen [here](https://github.com/nirnachmani/Pixie_Plus_Local/blob/main/port_53216_traffic_example.jpg). Make sure you include everything. Contact me on Github to discuss how you will transfer this to me. Thanks in advance.  

##

Pixie Plus Local is a Home Assistant custom integration for SAL Pixie Plus devices.

Unlike the older Pixie Plus integration, this one controls the hub locally over your LAN instead of using the cloud. It still uses your Pixie account once during setup to retrieve the metadata the hub requires for local access, but after that the integration runs against the hub directly. I attempted to implement Bluetooth support as well but Pixie Plus devices are not compatible with Home Assistant Bluetooth (Pixie devices ignore CCCD write which is required for a successful handshake.) As such, the integration still requires a Pixie Gateway to work. 

## Features

- Automatic gateway discovery
- Local control through the Pixie Plus hub
- Local push-style state updates from the live gateway session
- Lights, dimmers, switches, smart plugs, RGB strip control, and blinds
- Blind button mapping is now done through the UI

The integration intentionally does not implement Pixie Plus groups, scenes, schedules, or timers. Home Assistant already covers those use cases more cleanly.

## Requirements

- A Pixie Plus hub on the same local network as Home Assistant
- All devices already paired and configured in the official Pixie Plus app
- A working Pixie account for the initial setup step

This integration is for hub-based Pixie Plus systems. It does not expose direct Bluetooth-only control.

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

Blind entities are exposed as assumed-state covers. The integration sends the configured button commands locally, but it does not derive an exact open percentage from the hub.

## Known Limitations

- The integration requires a Pixie Plus hub.
- Devices must already be set up in the official Pixie app.
- Pixie cloud login is required during the initial setup flow.
- The integration will attempt local inventory refresh but if unsuccessful will operate in a hybrid mode where it requires the cloud on each startup to build the inventory but will otherwise use local communication. In that case, the Pixie Plus username and password will be stored in Home Assistant.
- Groups, scenes, schedules, and timers from the Pixie ecosystem are not implemented.

## Troubleshooting

If setup fails:

- Verify your Pixie username and password.
- Make sure Home Assistant and the Pixie hub are on the same LAN.
- Confirm that the hub and devices are already working in the Pixie app.
- Check the Home Assistant logs for `pixie_plus_local` messages.

If blind actions are wrong:

- Re-open the integration options.
- Adjust the button-position mapping for that controller.
- Use the original Pixie app button positions, not the visual position after rearranging buttons.

## Status

This is a custom integration built from reverse engineering and local protocol work. It has been developed against one real-world setup and should still be considered community-supported. Development heavily relied on AI. 

Use it, adapt it, and inspect the code if needed.
