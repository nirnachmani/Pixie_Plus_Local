# Pixie Plus Local for Home Assistant

Pixie Plus Local is a Home Assistant custom integration for SAL Pixie Plus devices. It controls Pixie devices locally via the LAN and/or Bluetooth.  

## Features

- Support Pixie installations with a gateway (via TCP and/or BT) and without a gateway (via BT). BT functionality requires an ESPHome Bluetooth proxy and will not work without one. This is equivalent to the Pixie Plus app and to the SAL Pixie app.
- Automatic gateway discovery with manual IP option (e.g. if gateway is on another subnet) 
- Local control through the Pixie Plus gateway and/or Bluetooth
- Local push-style state updates from the live gateway session and Bluetooth
- Supports many Pixie devices
- Supports multiple Homes/Gateways

The integration intentionally does not implement Pixie Plus groups, scenes, schedules, or timers. Home Assistant already covers those use cases more cleanly.

## Requirements

- All devices already paired and configured in the official Pixie Plus or SAL Pixie apps.
- Pixie credentials (for gateway installations) and PIN for installations without a gateway. 

## Supported Devices

The current code includes support for these models:

- Gateway G3 - SGW3BTAM
- Smart Switch G3 - SWL600BTAM
- Smart Dimmer G3 - SDD300BTAM
- Smart Switch G2 - SWL350BT
- Smart Dimmer G2 - SDD350BT
- Smart plug - ESS105/BT
- Smart Dimmer rippleSHIELD - SDD400RS/BTAM
- SFI Dimmer - SDD400SFI
- Smart timer switch - STS600BTAM 
- Smart Socket Outlet - SP023/BTAM
- Flexi Smart LED Strip - FLP12V2M/RGBBT
- Flexi Streamline - FLP24V2M
- Strip Kit RGB - FLBP24V2RGB/BTAM 
- Smart RGBTW LED strip controller - LT8915RTW/BTAM
- LED Strip Controller - LT8915DIM/BT
- Smart Passive Infrared Motion Sensor - SMS861CD/BTAM
- Smart Passive Infrared Motion Sensor - SMS862WF/WH/BTAM
- Gate & Door Control - PC206GD/R/BTAM
- Dual Relay Control - PC206DR/R/BTAM
- Blind and Signal Control - PC206BS/R/BTAM
- Contact Sensor Transceiver - PC100CS/R/BTAM
- Translator control - PC100T/R/BTAS

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

## Bluetooth functionality

- Bluetooth (BT) functionality requires an ESPHome bluetooth proxy. See [here](https://esphome.io/components/bluetooth_proxy/). ***It will not work without one***.
- The integration will connect to one Pixie device via BT and will use the BLE mesh to send commands and get updates. 
- Once enabled when gateway is present, the user can select if commands will be sent via TCP or BT. 
- Updates from devices will arrive from both TCP and BT, and will race.
- BT can be enabled on initial installation (the integration will ask the user during the install process). It can later be enabled or disabled via reconfigure (under ⋮ after clicking on the integration ) -> Bluetooth support.
- The added benefit of BT is currently minimal, but I am hoping to add the ability to add and remove devices straight from HA, which requires BT. 

## Multiple Homes/Gateways

- Each Home/Gateway is added as a Hub under the integration. 
- On initial setup the integration will ask which Home to add and will give an opportunity to add other Homes and non-gateway devices
- Homes can be added later by using the "Add hub" in the integration page or deleted (click on the ⋮ of the relevant home -> Delete)
- The integration will attempt to find the gateway that the linked to the home and will prompt for an IP address if it can't find it

## Configuration menu (cogwheel icon on each integration instance)

### Bluetooth settings (only shows if BT is enabled for this home)

- Command transport (only shows if there is a gateway and BT is enabled for this home):
   - TCP primary, BT fallback - commands sent via TCP but if TCP is down, the integration will use BT
   - BT Primary, TCP fallback - commands sent via BT but if BT is down, the integration will use TCP
   - TCP only - BT will not be used
   - BT only - TCP will not be used
   
  Because the ESPHome bluetooth proxy requires LAN access, BT modes still depend on the LAN.

- Bluetooth access node
   - Auto / best BLE Node - the integration will connect to the node with the strongest signal
   - Prefers gateway, fallback to auto - the integration will attempt to connect to the gateway but if the gateway is not discoverable, will connect to the node with the strongest signal  

- Clear Bluetooth access-node preference - the integration saves a preferred node to connect to and stores it. This stored node can be cleared by selecting this option.   

### Update device versions (only shows if BT is enabled for this home)

Some devices can show the wrong firmware version (as reported by the gateway). If BT is enabled this scan BT adverts for the correct firmware version for all the devices   

### Blind mapping

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
