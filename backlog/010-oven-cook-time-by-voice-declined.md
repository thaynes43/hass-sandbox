# Oven Cook Time by voice (declined)

Status: Declined (Tom, 2026-10-18: he wants a kitchen timer, not a timed bake). Kept so nobody
rebuilds it without that decision changing.
Size: Medium (a ge_home patch, an HA restart, and safety checks around a heating appliance).

Links: [#255](https://github.com/thaynes43/hass-sandbox/issues/255) (why the oven's own kitchen
timer can't be set remotely, with the ERD capture), [#259](https://github.com/thaynes43/hass-sandbox/issues/259),
[#262](https://github.com/thaynes43/hass-sandbox/issues/262) (what was built instead: `timer.oven` on the
kitchen Connect display).

## What Cook Time is

Cook Time is the only oven "timer" the SmartHQ app offers for the Cafe double wall oven
(CTD90FP4N4W2 via `ge_home`). It is not a kitchen timer: it starts a timed cooking mode. The app
capture on 2026-10-18 (#255, ge_home update_coordinator debug log) shows a 2-minute Bake at 350 F
written as one cook-mode ERD:

- `0x5100` (upper oven cook mode) = `01 015E 0002 …`: mode `01` (Bake), `015E` = 350 F,
  `0002` = 2 minutes of cook time. `0x5120` (recipe status) and `0x5104` (cook time remaining = 2)
  followed. The kitchen timer ERD `0x5105` never moved, so the LCD showed the bake, not a timer.

## Why it was not built

- **No HA service writes cook time.** `ge_home` exposes `set_timer` / `clear_timer` (the kitchen timer,
  `0x5105`, which runs hidden on this oven's LCD: #255) and the oven `water_heater` entity's mode and
  temperature. Nothing takes a cook time.
- **The `water_heater` path is unsafe for this.** Setting a mode or temperature through the
  `water_heater` entity writes `0x5100` with cook time `0000`, so it zeroes a running cook time, and on
  an oven that is off it **starts** the oven. A "timer" that can switch on a heating element is the
  wrong tool for "remind me in 10 minutes".
- **It would need a ge_home patch plus an HA restart:** a raw `0x5100` write that keeps the current
  mode and temperature and changes only the cook-time field, refusing when the oven is off. That is
  custom code on a cooking appliance, outside the integration's tested paths.
- **Tom declined it on 2026-10-18** because he wants a kitchen timer. #262 built that instead:
  `timer.oven`, set by voice in the kitchen, counting down and chiming on the kitchen Connect display.

## If it comes back

Start from the #255 capture (the exact ERD sequence the app writes), get Tom's go for a raw-write test
on an oven that is already baking, and keep the "oven is off -> refuse" rule.
