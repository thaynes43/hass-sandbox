# Seasonal front lighting, 2026

Live Home Assistant configuration mirrors, installed November 1, 2026 for
[OpenProject story #348](https://work.haynesnetwork.com/projects/haynes-engineering/work_packages/348),
under epic #347 and saga #346. No copy/paste or HA restart is required.
The now-online lamp post was added after Tom's sunset review in
[story #352](https://work.haynesnetwork.com/projects/haynes-engineering/work_packages/352).
Tom's household requested a warmer lamp-post color afterward in
[story #353](https://work.haynesnetwork.com/projects/haynes-engineering/work_packages/353):
the current setting is deeper amber (HS 28/100) at 75% brightness.

| Mirror | Live entity | Installed state |
| --- | --- | --- |
| `fall.yaml` | `automation.light_front_yard_sun_sets_colors_of_fall` | Enabled; November 1–26, 2026 |
| `christmas.yaml` | `automation.light_front_yard_sun_sets_christmas_colors` | Enabled; eligible November 27, 2026 onward |
| `visitor.yaml` | `automation.light_front_door_seasonal_visitor_turn_on_lanterns` | Enabled; visitor-only lanterns |
| `midnight.yaml` | `automation.light_front_yard_at_specific_time_turn_off_ambient_lighting` | Enabled; midnight off including lamp post |

## Palettes and schedule

All dates and sunset calculations use HA's America/New_York timezone.
Thanksgiving is November 26 in 2026. These are deliberately the current season's
dates, not a recurring annual holiday scheduler.

| Evenings | Palette |
| --- | --- |
| November 1–7 | Amber, gold, burnt orange |
| November 8–14 | Copper, orange, burgundy |
| November 15–21 | Burgundy and gold with evergreen accents |
| November 22–26 | Final autumn blend, with orange/gold/burgundy still dominant |
| November 27 onward | Red, green and warm gold until a replacement takes over |

Both color controllers run at sunset and recover after HA startup or automation
reload only during the evening, before midnight. Each sets eight Callas, six Lilys
and three porch recessed lights plus the lamp post individually, using five-second fades and two
paced passes to handle the existing Hue mesh's occasional missed commands.
Unavailable lights are skipped so one offline bulb does not block the rest.

The lamp post uses its own deeper amber palette slot throughout fall and
Christmas Colors. The four shared palette slots remain unchanged; it never
receives the evergreen/green slot. The earlier yellow-gold setting was too cool
for the household, so its dedicated swatch is now HS 28/100 at 75% brightness.
It was initially unavailable and excluded, but Tom's sunset review confirmed it
was online and still green. Its live color was corrected and its sunset and
midnight coverage added at that point.

No new routine commands the Zooz yard outlets, Hue string lights,
porch trees or Twinkly decorations. Tom excluded the Zooz outlets and Hue strings
for Colors of Fall; their potential return belongs to his later Get Jolly review.

The front-door lanterns stay off until a visitor is detected or the doorbell is
pressed after dark. They turn off after three clear minutes; a doorbell press
gets at least three minutes. The restarted sequence waits for an already-clear
or newly-cleared person sensor, then delays three minutes. A new detection or
doorbell press restarts that sequence; no independent clear timer can shorten
a newer visit. Midnight cancels a pending wait/delay and turns the lanterns off;
another overnight visitor can still get light.

## Existing automations

These four Halloween automations were turned off, with their configs retained:

- `automation.front_yard_halloween_sun_sets_get_spooky`
- `automation.light_front_door_doorbell_sees_someone_during_halloween_turn_on_lanterns`
- `automation.switch_dining_room_power_strip_outlets_toggle_mirror_to_ch1_for_halloween`
- `automation.switch_front_yard_at_specific_time_turn_off` (the Halloween 02:00 outlet routine)

`automation.light_front_yard_at_specific_time_turn_off_ambient_lighting` remains
enabled: its midnight action covers all 17 core lights, the lamp post and front-door lanterns.
Ordinary ambient sunset lighting and Get Jolly remain disabled. The separate
Twinkly midnight-off automation also remains disabled.

Enabling either `automation.light_front_yard_sun_sets_during_christmas_get_jolly`
or `automation.light_front_yard_at_sunset_turn_on` makes the new controllers yield.
Color actions recheck the takeover flags before each light command; visitor
lighting rechecks before its delayed off so it does not undo the takeover.
The later expanded Christmas setup must review Get Jolly and its corresponding
decoration shutoff together before activation.

## Verification and rollback

HA 2026.10.3 accepted all three configurations. Controlled daytime tests applied
the first fall palette and the Christmas palette to all 17 lights: on state,
hue/saturation and brightness matched, within Hue conversion rounding. Both
runs finished without an error. A transient scene snapshot restored the daytime
off baseline afterward; the Christmas restore needed one retry for two Callas.
Weekly boundaries and the November 26/27 handoff were also rendered in live HA.
The original 24 focused checks passed, including fake-time visitor race, repeat detection,
long presence, takeover and midnight cancellation scenarios. Independent native
review found two visitor timing issues; both were fixed and rereview passed
without remaining substantive findings.
The lamp-post correction passed 26 focused checks, including every fall week's
lamp-post color, the Christmas slot and midnight coverage. Live readback confirmed
warm gold at 75% brightness. The original 17-light evidence remains historical;
the current seasonal inventory contains 18 lights.

Run calendar, device-selection and takeover/recovery checks with:

```sh
python -m pytest -q scripts/seasonal-lighting-tests
```

Evidence and the original four Halloween configurations are retained in
`.agents/plans/seasonal-lighting-2026-evidence/`. To undo this seasonal rollout,
disable the three new automations; reenable only the desired prior controller.
The old Halloween configs were not edited or deleted. Their original enabled
state is recorded in the installation evidence.

Not verified by the daytime test: an actual sunset event or physical camera/
doorbell visitor. No sensor states were forged. The future November 27 calendar
date was evaluated without changing HA's clock. No cluster or AppDaemon change
was made.
