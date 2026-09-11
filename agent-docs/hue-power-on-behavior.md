# Hue power-on behavior (Zigbee2MQTT)

Ruling (Tom, 2026-08-30): after a power loss every Hue bulb restores its
**previous state**. Two deliberate exceptions stay `on` (the kids' nightstands
`upstairs_blue_room_nightstand_hue_bulb_jackson` and
`upstairs_pink_room_nightstand_hue_bulb_penelope`), the photocell-fed
`front_yard_lamp_post_hue_edison` stays `on` (every dusk is a power-on for it),
and `outdoor_hue_string_lights_01` is seasonal decor left unset. Third Reality
night lights are not Hue and are outside this ruling.

## The gotcha: the HA select only covers on/off

`select.<device>_power_on_behavior` is Zigbee2MQTT's generic converter. It writes
one attribute, `genOnOff.startUpOnOff` (0x4003). Hue bulbs keep three more
startup attributes that it never touches:

| attribute | cluster | factory default |
|---|---|---|
| `startUpCurrentLevel` (0x4000) | `genLevelCtrl` | 254 (100 %) |
| `startUpColorTemperature` (0x4010) | `lightingColorCtrl` | 366 mireds (2732 K) |
| Philips startup colour x/y (0x0003 / 0x0004, manufacturer-specific) | `lightingColorCtrl` | unset |

So with the select at `previous` a bulb that was **on** when power dropped
comes back on, but at 100 % and 2732 K. That is exactly what happened on
2026-09-10: a brownout around 19:12 ET (the rack UPS logged a voltage swing
but never went to battery) reset the front porch and front yard bulbs from the
sunset automation's 25 % / 2500 K to 100 % / 2732 K while the select still read
`previous`. Read from the bulb: `startUpOnOff=255`, `startUpCurrentLevel=254`,
`startUpColorTemperature=366`.

## The fix: `hue_power_on_behavior: recover`

Zigbee2MQTT's Philips converter has a Hue-specific key. `recover` writes all of
the startup attributes to "previous" (0xFF / 0xFFFF) in one go:

```json
// publish to zigbee2mqtt/<friendly_name>/set
{"hue_power_on_behavior": "recover"}
```

From HA use the `mqtt.publish` service with that topic and payload. The topic
takes the Zigbee2MQTT **friendly name**, which is not always the HA entity
slug (`Den Hue Iris Light` has spaces). The HA select keeps showing `previous`
afterwards because `startUpOnOff` is still 0xFF; that is correct, not drift.

Applied to all 93 `*hue*` bulbs on 2026-09-10 (the 17 front porch/yard bulbs
via `mqtt.publish`, the rest paced at one device per 1.5 s over MQTT). The four
exceptions above were not touched.

### Verify from the bulb, not from HA state

```json
{"read": {"cluster": "genLevelCtrl", "attributes": ["startUpCurrentLevel"]}}
{"read": {"cluster": "lightingColorCtrl", "attributes": ["startUpColorTemperature"]}}
```

Zigbee2MQTT logs the answer at info level as `Read result of 'genLevelCtrl':
{"startUpCurrentLevel":255}` (and `65535` for the colour temperature). The log
line does not name the device, so read one bulb at a time. White-only bulbs
(`basement_server_hue_white_bulb_*`) have no `lightingColorCtrl` cluster; only
the level read applies.

## Onboarding a new Hue bulb

1. Join it to Zigbee2MQTT and give it its `<area>_hue_<fixture>_NN` name.
2. Publish `{"hue_power_on_behavior": "recover"}` to its `/set` topic.
3. Read `startUpCurrentLevel` back and confirm 255.

Do not "normalise" the four exceptions, and do not rely on the select alone.
