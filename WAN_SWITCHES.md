# Peplink WAN Interface Control Switches

## Overview

The Peplink integration now provides switch entities to enable/disable WAN interfaces directly from Home Assistant.

## Features

- **Enable/Disable WAN interfaces** - Turn WAN connections on/off
- **Real-time state sync** - Automatically updates when coordinator refreshes
- **Additional attributes** - Shows WAN status, type, method, and message
- **Device integration** - Switches are grouped with their corresponding WAN sensors

## Usage

### In the UI

1. Go to **Settings → Devices & Services → Peplink**
2. Click on a WAN device (e.g., "Peplink WAN2")
3. You'll see an **"Enabled"** switch entity
4. Toggle to enable/disable the WAN interface

### In Automations

```yaml
automation:
  - alias: "Disable backup WAN during off-peak hours"
    trigger:
      - platform: time
        at: "23:00:00"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.peplink_wan3_enable
  
  - alias: "Enable backup WAN during peak hours"
    trigger:
      - platform: time
        at: "08:00:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.peplink_wan3_enable
```

### In Scripts

```yaml
failover_to_backup:
  alias: "Failover to Backup WAN"
  sequence:
    - service: switch.turn_off
      target:
        entity_id: switch.peplink_wan1_enable
    - delay:
        seconds: 5
    - service: switch.turn_on
      target:
        entity_id: switch.peplink_wan2_enable
```

## Entity Details

### Entity ID Format
`switch.<device_name>_wan<id>_enable`

Example: `switch.peplink_wan2_enable`

### Attributes

- **wan_id** - WAN interface ID
- **wan_name** - WAN interface name
- **status_led** - Current LED status (green, red, gray, etc.)
- **message** - Connection status message
- **type** - WAN type (ethernet, modem, wifi, etc.)
- **method** - Connection method (dhcp, static, pppoe, etc.)

### Example State

```yaml
state: "on"
attributes:
  wan_id: "2"
  wan_name: "WAN-2"
  status_led: "green"
  message: "Connected"
  type: "ethernet"
  method: "dhcp"
```

## API Implementation

The switches use the Peplink API endpoint:
```
POST /api/config.wan.connection
```

With payload:
```json
{
  "action": "update",
  "list": [
    {
      "id": 2,
      "enable": true
    }
  ]
}
```

## Important Notes

1. **Permissions Required**: Your API user must have **Read-Write** permissions
2. **Config Apply**: The router may require config apply after changing WAN state
3. **Reconnection Time**: Disabling/enabling a WAN may take 5-30 seconds to fully take effect
4. **Active Connections**: Disabling a WAN will drop all active connections on that interface
