# Laundry Notifications

The washer and dryer tell your phone when they're done, but only when you can act on it. If the dryer is still running when the wash finishes, there's nowhere to put the wet load, so nothing is sent until the dryer finishes too. Then one notification says both are ready. If wet clothes are forgotten in the washer, a reminder follows two hours later.

## What you get

| Situation | Notification |
|---|---|
| Only the washer was running | **Washer complete** |
| Only the dryer was running | **Dryer complete** |
| The washer finishes while the dryer is still going | Nothing yet. One **Washer & dryer complete** when the dryer is done |
| The dryer finishes while the washer is still going | **Dryer complete** right away, so it can be emptied. **Washer complete** follows later |
| The washer has been sitting full for 2 hours after its notification | **Washer still needs switching** |

## Knowing the washer was opened

The LG washer has no door sensor in Home Assistant, but it gives itself away: it stays on "cycle complete" until the door is opened, then powers off. On November 25 the washer powered off within a minute or two of the dryer starting on every load. Once, it sat full from 9:08 am until 1:38 pm, which is exactly what the reminder is for. Leaving "complete" (by being opened or starting a new cycle) cancels the reminder.

## How it works

```
LG ThinQ washer status ─┐
                        ├─► laundry_notify (AppDaemon)
LG ThinQ dryer status  ─┘     ├─ decides: notify now, hold, or combine
                              ├─ push to each phone (notify.mobile_app_*)
                              └─ 2 h timer ─► "Washer still needs switching"
                                              (cancelled when the washer is opened)
```

- The decision logic is a small state machine with no Home Assistant code in it, so every combination above is covered by unit tests. We also replayed a real laundry day from history through it.
- Wi-Fi drop-outs (the machines going `unavailable` mid-cycle) are ignored, so a blip never hides or fakes a finish.
- The reminder delay and the dryer-first behavior are config settings.

## Related

- `appdaemon/apps/laundry_notify/README.md`
