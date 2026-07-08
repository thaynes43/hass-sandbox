# Battery Checker

## Overview

A health checker that monitors battery levels across battery-powered devices and warns before batteries die. Uses a base `BatteryChecker` class that can be instantiated multiple times with different configs for different device groups.

A separate `UpsChecker` handles UPS-specific monitoring (battery charge + load utilization) since UPS devices have different health semantics than simple battery devices.

## Battery Device Audit (2026-03-25)

### Summary

- **Total battery-level sensor entities found**: 73
- **Integrations represented**: Z-Wave JS, Hunter Douglas PowerView, Airthings, Schlage, UniFi Protect, Mobile App (iOS), NUT (UPS), Zigbee2MQTT, Generac

### Devices Needing Immediate Attention

| Entity ID | Friendly Name | Level | Integration |
|-----------|--------------|-------|-------------|
| `sensor.primary_bedroom_wave_plus_battery` | Primary Bedroom Wave Plus | 0% | Airthings |
| `sensor.primary_bathroom_battery` | Primary Bathroom | 1% | Airthings |
| `sensor.front_door_lock_battery` | Front Door Lock | 4% | Schlage |
| `sensor.shed_temperature_sensor_battery_level` | Shed Temperature Sensor | 16% | Z-Wave JS |
| `sensor.upstairs_cloffice_temperature_sensor_01_battery_level` | Cloffice Temperature Sensor | 34% | Z-Wave JS |
| `sensor.cigar_humidity_sensor_01_battery_level` | Tupperdor 01 | 35% | Z-Wave JS |

## Checker Architecture

### BatteryChecker (base class)

Config-driven checker that monitors a list of battery percentage entities. Each instance can have its own warning/critical thresholds and dependency.

**Config pattern**:
```yaml
zwave_battery_checker_dev:
  module: health_checks.checker_apps.battery_checker.battery_checker
  class: BatteryChecker
  checker_id: zwave_batteries
  checker_name: Z-Wave Batteries
  check_interval_s: 300
  warning_threshold: 20
  critical_threshold: 10
  health_dependencies:
    - checker_id: zwave
  entity_patterns:
    - include: "sensor\\..*_battery_level$"
    - exclude: ".*iphone.*"
    - exclude: ".*ipad.*"
    - exclude: ".*unifi_display.*"
```

`entity_patterns` is a list of regex rules evaluated in order against entity IDs (via `re.search`, so substring matching). `include` adds matches to the monitored set; `exclude` removes them. This lets broad-include rules auto-enroll new devices while still filtering out known noisy entities (e.g. mobile_app/companion phones and tablets that share the `_battery_level` suffix with Z-Wave sensors). Each matched entity becomes a check named after its friendly name.

Status logic:
- `level > warning_threshold` → ok
- `warning_threshold >= level > critical_threshold` → warning
- `level <= critical_threshold` → critical
- Entity unavailable / unknown / missing / unreadable → **unknown** (never
  critical). A missing reading is *no data*, not a low battery, so it never
  pages: an integration/connectivity blip that drops a whole battery group
  at once would otherwise false-page as dead batteries (the entity goes
  straight to `unavailable` without ever reporting a low percentage). A
  genuine battery drains gradually and trips the warning/critical numeric
  thresholds *before* the device drops off; a device that goes straight to
  unavailable is a connectivity failure owned by that integration's own
  health checker.

#### Disconnect-aware guard (opt-in)

Some battery sensors report a physically-impossible reading when the device
loses its link to a hub/gateway rather than when the battery is actually low.
Hunter Douglas PowerView G3 shades are the motivating case: on an RF/gateway
disconnect the shade reports **0%**, and a flapping gateway makes it bounce
`100% ↔ 0%` many times an hour. A real battery never loses 40+ percentage
points between two consecutive readings, so treating that 0% as "critical low
battery" produces false pages for a condition a human can't fix by charging.

When `disconnect_aware: true`, an **implausible drop** — the last healthy
reading was `>= disconnect_healthy_floor` and the current reading is
`<= critical_threshold` — is reported as **warning** (UI-only, no page) with a
`suspected gateway disconnect (was N%, now M%) — see Shade Gateway` detail,
instead of `critical`. A genuine gradual decline (the last healthy reading was
already below the floor) still pages `critical` as normal. The checker tracks
the last healthy reading per entity, seeded from the current state at startup;
a cold start never fabricates a baseline from a low reading.

| Option | Default | Meaning |
|--------|---------|---------|
| `disconnect_aware` | `false` | Enable the guard. Off by default so unrelated battery groups are unchanged. |
| `disconnect_healthy_floor` | `40` | The last-healthy reading must be at or above this (%) for a drop to `<= critical_threshold` to count as a suspected disconnect rather than a real low battery. |

This only *suppresses the false low-battery page*; paging and remediation for
the disconnect itself are owned by the dedicated
[`shade_gateway_checker`](../shade_gateway_checker/README.md), which detects the
disconnect, waits out a grace period, and auto power-cycles the gateway.
`shade_battery_checker` sets `disconnect_aware: true`; the other battery groups
leave it off.

```yaml
shade_battery_checker:
  # ...
  critical_threshold: 5
  disconnect_aware: true
  disconnect_healthy_floor: 40
```

### UpsChecker (separate class)

Monitors UPS devices with two check types per UPS:
1. **Battery Charge** — percentage, warning at 90% (any discharge is concerning), critical at 50%
2. **Load Utilization** — percentage, warning at 90%, critical at 100%

**Config pattern**:
```yaml
ups_checker_dev:
  module: health_checks.checker_apps.battery_checker.ups_checker
  class: UpsChecker
  checker_id: ups
  checker_name: UPS
  check_interval_s: 120
  ups_devices:
    - name: APC 2700W
      battery_entity: sensor.apc_2700w_battery_charge
      load_entity: sensor.apc_2700w_load
      battery_warning: 90
      battery_critical: 50
      load_warning: 90
      load_critical: 100
    - name: APC 900W
      battery_entity: sensor.apc_900w_01_battery_charge
      load_entity: sensor.apc_900w_01_load
      battery_warning: 90
      battery_critical: 50
      load_warning: 90
      load_critical: 100
```

## Planned Checker Instances

### 1. `zwave_battery_checker`
- **Class**: BatteryChecker
- **Entities**: 27 Z-Wave JS battery sensors
- **Dependency**: `zwave`
- **Thresholds**: Warning 20%, Critical 10%

### 2. `shade_battery_checker`
- **Class**: BatteryChecker
- **Entities**: 22 Hunter Douglas PowerView shade batteries
- **Dependency**: None
- **Thresholds**: Warning 25%, Critical 5%
- **Note**: Shades report battery in coarse steps (100→50→25→10→5→0), not smooth decrements. These thresholds account for that behavior.

### 3. `lock_battery_checker`
- **Class**: BatteryChecker
- **Entities**: 4 Schlage lock batteries
- **Dependency**: `cloud` (Schlage cloud integration)
- **Thresholds**: Warning 25%, Critical 10%

### 4. `airthings_battery_checker`
- **Class**: BatteryChecker
- **Entities**: 6 Airthings battery sensors
- **Dependency**: None
- **Thresholds**: Warning 20%, Critical 5%

### 5. `protect_battery_checker`
- **Class**: BatteryChecker
- **Entities**: 4 UniFi Protect USL Entry sensors
- **Dependency**: None
- **Thresholds**: Warning 20%, Critical 10%

### 6. `zigbee_battery_checker`
- **Class**: BatteryChecker
- **Entities**: 3 Zigbee2MQTT battery sensors
- **Dependency**: `zigbee`
- **Thresholds**: Warning 20%, Critical 10%

### 7. `ups_checker`
- **Class**: UpsChecker
- **Entities**: 2 NUT UPS devices (charge + load per device)
- **Dependency**: None
- **Battery thresholds**: Warning 90% (any discharge is concerning), Critical 50%
- **Load thresholds**: Warning 90%, Critical 100%+

### Not Included

- **Mobile App entities** (phones, tablets, UniFi displays): Personal devices that fluctuate by design. Low-battery alerts would be noisy.
- **Generac generator**: Voltage-based (13.5V), not percentage. Separate concern from UPS monitoring — may get its own checker later.

## Entity Lists by Instance

### Z-Wave Batteries (27 sensors)

| Entity ID | Name |
|-----------|------|
| `sensor.basement_movie_room_wall_remote_battery_level` | Movie Room Wall Remote |
| `sensor.server_room_temperature_sensor_01_battery_level` | Server Room Temp |
| `sensor.upstairs_cloffice_temperature_sensor_01_battery_level` | Cloffice Temp |
| `sensor.cigar_humidity_sensor_01_battery_level` | Tupperdor 01 |
| `sensor.cigar_humidity_sensor_02_battery_level` | Tupperdor 02 |
| `sensor.cigar_humidity_sensor_03_battery_level` | Jar 01 |
| `sensor.cigar_humidity_sensor_04_battery_level` | Jar 02 |
| `sensor.cigar_humidity_sensor_05_battery_level` | Cooler |
| `sensor.cigar_humidity_sensor_06_battery_level` | Tupperdor 03 |
| `sensor.cigar_humidity_sensor_07_battery_level` | Tupperdor 04 |
| `sensor.upstairs_laundry_range_extender_battery_level` | Laundry Range Extender |
| `sensor.garage_range_extender_battery_level` | Garage Range Extender |
| `sensor.basement_rumpus_range_extender_battery_level` | Rumpus Range Extender |
| `sensor.shed_extender_battery_level` | Shed Extender |
| `sensor.shed_temperature_sensor_battery_level` | Shed Temp |
| `sensor.basement_storage_trap_leak_sensor_battery_level` | Storage Trap Leak |
| `sensor.basement_storage_sump_pump_leak_sensor_battery_level` | Sump Pump Leak |
| `sensor.basement_storage_open_closed_sensor_battery_level` | Storage Open/Close |
| `sensor.basement_movie_room_water_meter_leak_sensor_battery_level` | Movie Room Water Leak |
| `sensor.basement_server_room_water_main_leak_sensor_battery_level` | Server Room Water Main Leak |
| `sensor.basement_server_room_ac_leak_sensor_battery_level` | Server Room AC Leak |
| `sensor.shed_door_open_closed_sensor_battery_level` | Shed Door Open/Close |
| `sensor.shed_indoor_motion_sensor_battery_level` | Shed Motion |
| `sensor.upstairs_primary_closet_open_closed_sensor_battery_level` | Primary Closet Open/Close |
| `sensor.basement_hallway_qsensor_battery_level` | Hallway Q-Sensor |
| `sensor.basement_concessions_qsensor_battery_level` | Concessions Q-Sensor |
| `sensor.basement_rumpus_room_wall_remote_battery_level` | Rumpus Wall Remote |
| `sensor.basement_sump_pump_shock_sensor_battery_level` | Sump Pump Shock |
| `sensor.basement_ejector_pump_shock_sensor_battery_level` | Ejector Pump Shock |

### Shade Batteries (22 sensors)

| Entity ID | Name |
|-----------|------|
| `sensor.kitchen_shade_battery` | Kitchen |
| `sensor.livingroom_backyard_shade_1_battery` | Living Room Backyard 1 |
| `sensor.livingroom_backyard_shade_2_battery` | Living Room Backyard 2 |
| `sensor.livingroom_side_yard_shade_1_battery` | Living Room Side Yard 1 |
| `sensor.livingroom_side_yard_shade_2_battery` | Living Room Side Yard 2 |
| `sensor.livingroom_front_yard_shade_battery` | Living Room Front Yard |
| `sensor.dining_room_shade_1_battery` | Dining Room 1 |
| `sensor.dining_room_shade_2_battery` | Dining Room 2 |
| `sensor.study_shade_1_battery` | Study 1 |
| `sensor.study_shade_2_battery` | Study 2 |
| `sensor.first_floor_bathroom_shade_battery` | First Floor Bathroom |
| `sensor.primary_bedroom_front_shade_battery` | Primary Bedroom Front |
| `sensor.primary_bedroom_side_shade_battery` | Primary Bedroom Side |
| `sensor.primary_bathroom_shade_battery` | Primary Bathroom |
| `sensor.kids_bathroom_shade_battery` | Kids Bathroom |
| `sensor.blue_room_shade_1_battery` | Blue Room 1 |
| `sensor.blue_room_shade_2_battery` | Blue Room 2 |
| `sensor.white_room_shade_1_battery` | White Room 1 |
| `sensor.white_room_shade_2_battery` | White Room 2 |
| `sensor.pink_room_shade_1_battery` | Pink Room 1 |
| `sensor.pink_room_shade_2_battery` | Pink Room 2 |
| `sensor.cloffice_shade_battery` | Cloffice |

### Lock Batteries (4 sensors)

| Entity ID | Name |
|-----------|------|
| `sensor.bulkhead_lock_battery` | Bulkhead |
| `sensor.front_door_lock_battery` | Front Door |
| `sensor.side_door_lock_battery` | Side Door |
| `sensor.mudroom_door_lock_battery` | Mudroom |

### Airthings Batteries (6 sensors)

| Entity ID | Name |
|-----------|------|
| `sensor.livingroom_view_plus_battery` | Living Room View Plus |
| `sensor.basement_view_radon_battery` | Basement View Radon |
| `sensor.primary_bedroom_wave_plus_battery` | Primary Bedroom Wave Plus |
| `sensor.primary_bathroom_battery` | Primary Bathroom |
| `sensor.basement_wave_mini_battery` | Basement Wave Mini |
| `sensor.laundry_room_wave_mini_battery` | Laundry Room Wave Mini |

### UniFi Protect Batteries (4 sensors)

| Entity ID | Name |
|-----------|------|
| `sensor.usl_entry_battery_3` | Front Door |
| `sensor.usl_entry_battery_2` | Garage Side Door |
| `sensor.kitchen_slider_usl_entry_battery` | Kitchen Slider |
| `sensor.usl_entry_battery` | Basement Bulkhead |

### Zigbee Batteries (3 sensors)

| Entity ID | Name |
|-----------|------|
| `sensor.basement_rumpus_aqara_fp300_presence_battery` | Rumpus FP300 Presence |
| `sensor.basement_aqara_w100_01_battery` | MON1800 |
| `sensor.basement_aqara_w100_02_battery` | MA50 |

### UPS Devices (2 devices, 2 checks each)

| Battery Entity | Load Entity | Name |
|---------------|-------------|------|
| `sensor.apc_2700w_battery_charge` | `sensor.apc_2700w_load` | APC 2700W |
| `sensor.apc_900w_01_battery_charge` | `sensor.apc_900w_01_load` | APC 900W |
