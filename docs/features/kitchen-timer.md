# Kitchen Oven Timer

"Okay Nabu, set the oven timer for 35 minutes." The kitchen voice assistant starts the timer, the wall display in the kitchen shows the countdown in big digits, and when it runs out the display chimes until someone taps it while the voice assistant says so out loud.

| Running | Done |
|---|---|
| ![Oven timer counting down on the kitchen display](../img/kitchen-timer-running.png) | ![Oven timer done, amber card on the kitchen display](../img/kitchen-timer-ringing.png) |

## Why not the oven's own timer?

The wall oven is a connected Café double oven, and Home Assistant can write its kitchen timer. The oven even counts it down, but its touchscreen only draws a timer that was started on the touchscreen itself. A remote one runs invisibly and can't be dismissed at the oven. The SmartHQ app has no kitchen timer either: its "timer" is Cook Time, which starts a timed bake, and a kitchen timer must never be able to switch an oven on. So the timer lives in Home Assistant, and the kitchen display, which everyone can see from the stove, shows it.

## How it works

```
"set the oven timer for 35 minutes"         tap +1 min / Cancel / tap to stop
        │ (kitchen Voice PE)                          │ (kitchen display card)
        ▼                                             ▼
 sentence triggers ──────────► script.kitchen_timer_relay ◄──────────┘
 (before any AI agent)          the only thing that changes the timer
                                         │
                               timer.oven (survives restarts)
                                         │ runs out
                                         ▼
                     ringing on ── display chimes every 5 s (up to 10 min)
                                └─ Voice PE: "Oven timer is done."
```

- **Voice** is handled by sentence triggers, so the phrases are matched locally in a fraction of a second, before any AI agent sees them. "Set a timer for 5 minutes" without the word *oven*, or a named timer like "pasta", is still the voice assistant's own built-in timer.
- **One script** owns the timer. The display card and the voice phrases both call it, so "add 5 minutes", the +1 min button and the spoken replies all follow the same rules.
- **The display card** swaps into the slot of the family's cruise countdown while a timer runs or rings, and the countdown comes back when it's done. Both cards are exactly the same size, so nothing else on the pixel-tuned dashboard moves.

## What you can say

| Say | Reply |
|---|---|
| "Set the oven timer for 10 minutes", "set a thirty-five minute oven timer" | "Oven timer set for 35 minutes." |
| "How long is left on the oven timer?" | "About 12 minutes left on the oven timer." |
| "Add 5 minutes to the oven timer" | "Added 5 minutes. About 17 minutes left." |
| "Cancel the oven timer", "stop the oven timer" | "Oven timer canceled." / "Oven timer stopped." |

Hours, minutes and seconds work in digits or words ("an hour and a half", "ninety seconds"). If the length can't be understood, the reply asks again instead of guessing.

## The chime

The chime is a short three-note bell synthesized by the browser on the display, so there is no sound file to host. It plays every five seconds until someone taps the card, says "stop the oven timer", or ten minutes pass. Only the kitchen display chimes; the second wall display shows the same countdown silently. The display allows sound without a tap, but if audio is ever blocked, a small "Tap for sound" badge appears and any tap turns sound back on. The voice assistant's announcement plays either way.

## Built with

- Helpers: `timer.oven` and `input_boolean.oven_timer_ringing`
- `script.kitchen_timer_relay`, `automation.voice_oven_timer_sentences`, `automation.oven_timer_ring_and_announce` (`home-assistant/` in the repo)
- `kitchen-timer-card`, a custom Lovelace card (`home-assistant/cards/unifi-connect/kitchen-timer/`)
