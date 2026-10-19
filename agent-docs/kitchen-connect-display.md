# Kitchen Connect display and the oven timer

Agent-facing reference for the two UniFi Connect wall displays and the oven timer that lives on
the kitchen one (hass-sandbox #260 spike, #262 build, 2026-10-18). Live HA wins over this file.

## The two screens

| | Kitchen | Other display |
|---|---|---|
| IP | **192.168.0.105** | 192.168.0.67 |
| Switch port | Pro Max 48 port 29 | port 42 |
| UniFi tracker | `device_tracker.uc_display_se_21` | `device_tracker.uc_display_se_21_2` |
| Dashboard | `unifi-connect`, Home view | `unifi-connect`, Home view (seen 2026-10-18 from the card's load line after Tom restarted both) |
| Oven timer chime | **yes** | no (shows the countdown, silent) |

- Both are UniFi Connect Display SE 21 units in Connect **web mode**: the HA frontend in the system
  WebView (Chromium 134, desktop-style user agent), 1920 x 1080 at 1x, logged in as the non-admin
  user **Haynes Home**. Each screen has its own refresh token, and that token's `last_used_ip` is
  the only thing that tells them apart.
- The mobile_app devices `unifi_display for arm64` / `... upstairs` are stale leftovers of an earlier
  Companion-app trial; notify calls to them fail (no push channel). Nothing uses them.
- **Sound works with no tap.** On 2026-10-18 the kitchen screen's `AudioContext` was `running` on a
  cold page load right after a display restart (the card's load line says `audio running`), and Tom
  heard the spike's chimes without touching the screen. The card still keeps the safety net below.
- Volume is the display's own volume (UniFi Connect app). HA cannot change it.

## Layout rules (unifi-connect Home view at 1920 x 1080)

Every Home card has a fixed height; the columns end on the same pixel and the bottom button row
starts at **y = 1040** (the "Shed" label measures 1039.5). Change the view only by swapping cards
of the same size. The Disney countdown slot is column 2, `views[0].sections[1].cards[4]`:
x 504, y 274.5, 440 x 247.5 (16:9).

## The oven timer

| Piece | Where |
|---|---|
| Helpers (live only, never mirrored) | `timer.oven` (restore on, area Kitchen), `input_boolean.oven_timer_ringing` |
| Relay script | `script.kitchen_timer_relay` (`home-assistant/scripts/kitchen-timer/`): start / add / cancel / stop / status; the only writer of the two helpers; returns a spoken `response` |
| Ring + announce | `automation.oven_timer_ring_and_announce` (`home-assistant/automations/kitchen-timer/`): `timer.finished` → ringing on + `assist_satellite.announce` on `assist_satellite.kitchen_assist_assist_satellite` ("Oven timer is done."); ringing off after 10 min and at HA start |
| Voice | `automation.voice_oven_timer_sentences` (`home-assistant/automations/voice/`), see `agent-docs/voice-control-map.md` |
| Card | `custom:kitchen-timer-card`, `home-assistant/cards/unifi-connect/kitchen-timer/kitchen-timer-card.js` → `/config/www/kitchen-timer/`, resource `/local/kitchen-timer/kitchen-timer-card.js?v=N` |
| Slot swap | `cards[4]` countdown + `cards[5]` timer card with complementary `visibility`; config in `countdown-slot-swap.yaml` next to the card |

Card behaviour:

- **Running:** big `m:ss` (`h:mm:ss` from an hour) computed from `finishes_at`, a progress bar
  (elapsed / `duration`), "12 min · done at 8:01 PM", **+1 min** and **Cancel**. Cancel needs a
  second tap within 3 s. +1 min restarts the timer with a longer `duration` and then moves the end
  back, because `timer.change` refuses to go past the duration the timer started with; that keeps
  the progress bar in place. Paused shows dimmed digits and "paused".
- **Ringing:** amber "Oven timer done", a tap anywhere on the card sends `stop`. The tap silences
  this screen at once (before the state round trip).
- **Chime:** a Web Audio C6-E6-G6 bell (no audio file: the CSP has no `data:` in `media-src`), every
  5 s while `oven_timer_ringing` is on, for at most 10 min per ring. Only on screens whose IP is in
  the card's `chime_ips` (`[192.168.0.105]`), read once per page load from `auth/refresh_tokens`
  (`is_current` → `last_used_ip`). The ringer runs at module level and reads the root
  `<home-assistant>` element's `hass`, so it still rings while the screen sits on a sub-view.
- **Safety net:** any tap on the page resumes a suspended `AudioContext`; a "Tap for sound" chip shows
  on the card while audio is blocked on a chiming screen; the Voice PE announcement always fires.
- **Load line:** once per page load each screen writes a logbook entry under `timer.oven`:
  `Kitchen timer card: loaded on <ip> (chime on|off), <w>x<h> @<dpr>x, audio <state>, <path>`. That
  is how to tell which screen shows which dashboard and whether audio is allowed. Headless test
  sessions log `unknown IP` (a long-lived token has no `last_used_ip`) and never chime.

End-to-end test, 2026-10-18 20:06:06 EDT (a 1-minute timer started by text as the kitchen
satellite): both screens showed the countdown; at 20:07:06 `timer.finished` turned ringing on and the
Voice PE announced (the announce step took about 4 s); Tom heard the display chime at a good volume and
one tap at 20:07:14 sent `stop` from the Haynes Home user, which silenced it.

## Deploying card changes

1. Copy the JS into the HA pod (kubectl exec + base64, see the card-deploy memory) and check the md5.
2. Bump `?v=N` on the resource.
3. **Restart both displays** (Tom, UniFi Connect app), or let them pick it up at their next page reload
   (every HA update reloads the frontend). Current resource: `?v=2` (2026-10-18: review fixes for the audio
   unlock events and a stale-state guard; the displays were last restarted on `v=1`). Dashboard *config* edits reach an open page live
   (`lovelace_updated`), but new or changed card *code* only loads on a page reload. Until then a
   screen shows an error box wherever the new card should render. Confirm with the load lines.

## Testing without disturbing the kitchen

- Voice: `scripts/voice-bench/run.sh bench.py "MODE=pipe REPS=1 PIPELINE=01jbqv0j9wjz49e4rnz3wptffh
  DEVICE_ID=d1add9183bc149141ffba9f93af6dc26 QUERIES='Set the oven timer for 2 minutes|Cancel the oven timer'"`.
  It really sets `timer.oven`, so both screens show the countdown while it runs. Cancel it.
- Layout: screenshot the live dashboard headless (memory `ha-dashboard-screenshot`) and fake the two
  states **page-locally** by wrapping the root element's `_updateHass` so every states update carries a
  fake `timer.oven` / `oven_timer_ringing`. Nothing is written to HA and nothing chimes.
- Never set `oven_timer_ringing` on, or let `timer.oven` run out, without telling Tom: the kitchen chimes
  and the Voice PE speaks.

## Known gaps

- A timer that runs out while HA itself is down finishes during startup, before automations load,
  so nothing rings for it.
- If the kitchen screen's IP changes, it stops chiming (the Voice PE still announces). Update
  `chime_ips` in the dashboard card config.
- The chime volume follows the display volume; there is no per-sound volume in HA beyond the card's
  `volume` option (default 0.6).
