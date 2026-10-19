# Kitchen Connect display and the kitchen timers

Agent-facing reference for the two UniFi Connect wall displays and the kitchen timers that live on the kitchen one
(hass-sandbox #260 spike, #262 single oven timer, #266 up to three named timers, 2026-10-18). Live HA wins over this
file.

## The two screens

| | Kitchen | Other display |
|---|---|---|
| IP | **192.168.0.105** (DHCP; not checked for a reservation, mcp-unifi was down) | 192.168.0.67 |
| Switch port | Pro Max 48 port 29 | port 42 |
| UniFi tracker | `device_tracker.uc_display_se_21` (MAC 28:70:4e:47:bb:f9) | `device_tracker.uc_display_se_21_2` |
| Dashboard | `unifi-connect`, Home view | `unifi-connect`, Home view (seen 2026-10-18 from the card's load line after Tom restarted both) |
| Timer chime | **yes** | no (shows the same rows, silent) |

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
x 504, y 274.5, 440 x 247.5 (16:9). Measured again on 2026-10-18 with 0, 1, 2 and 3 timers and with
one ringing: the slot stayed 440 x 247.5 at (504, 274.5), school lunch stayed at y 530, "Shed" at 1039.5.

## The kitchen timers

Up to three run at once, one **slot** each (N = 1..3). Helpers are live only and never mirrored; all
are in area Kitchen and hidden from Assist, Alexa and Google (so no agent sees them).

| Piece | Where |
|---|---|
| Helpers per slot | `timer.kitchen_timer_N` (restore on), `input_text.kitchen_timer_N_name` (max 40), `input_boolean.kitchen_timer_N_ringing` |
| Relay script | `script.kitchen_timer_relay` (`home-assistant/scripts/kitchen-timer/`): the only writer of the nine helpers. start / add / subtract / cancel / cancel_all / stop / status / pause / resume from the card and voice; ring / expire / reset from the ring automation. Returns a spoken `response` |
| Ring + announce | `automation.kitchen_timer_ring_and_announce` (`home-assistant/automations/kitchen-timer/`): `timer.finished` → relay `ring` (ringing on) + `assist_satellite.announce` on `assist_satellite.kitchen_assist_assist_satellite` ("Pasta timer is done." / "The timer is done." / "Timer 2 is done."); a ring turns off after 10 min (relay `expire`) and at HA start (relay `reset`) |
| Voice | kitchen satellite only: `custom_sentences/en/kitchen_timers.yaml` → intent `KitchenTimer` → `intent_script` in `packages/kitchen_timer.yaml` → `script.kitchen_timer_voice` → relay. See `agent-docs/voice-control-map.md` |
| Card | `custom:kitchen-timer-card`, `home-assistant/cards/unifi-connect/kitchen-timer/kitchen-timer-card.js` → `/config/www/kitchen-timer/`, resource `/local/kitchen-timer/kitchen-timer-card.js?v=N` |
| Slot swap | `cards[4]` countdown + `cards[5]` timer card with complementary `visibility` over all six helpers; config in `countdown-slot-swap.yaml` next to the card |

Slots and names:

- A slot is **busy** while its timer is active or paused, or while it rings. Start takes the first free
  slot; with none free it takes a slot that is done and ringing; with three running it refuses ("The
  kitchen already has three timers running, pasta, rice and the timer. Cancel one first.").
- The name is the stated one, capitalized ("pasta" → "Pasta"). Without one: "Timer", then "Timer 2",
  "Timer 3". Generic words (kitchen, new, another, second...) count as no name. A start with a name that
  is already busy restarts that timer. The name is cleared when the slot goes idle.
- The card labels "Timer" as **Kitchen timer**; "Timer 2" and named timers show as they are. Rows keep
  slot order, so a row never moves under a finger.
- With several timers and no name: add / cancel / stop go to the one that is done and ringing ("five
  more minutes"); a lone plain "Timer" among named ones is "the timer"; status, pause and resume cover
  all of them; anything else replies with the names and an example instead of guessing. "Timer one",
  "the first timer" also reach the plain "Timer". Names match exactly, then by whole words ("pasta water"
  finds "Pasta", "tea" never finds "Steak"). "The 5 minute timer" matches a timer's *current* length, so
  it stops matching after +1 min or a take-off.
- `stop` silences what is ringing (every ringing one when no name is given); with nothing ringing it
  acts as cancel. "Stop the timers" / "stop all timers" (`stop_all`) silences every ringing timer, or
  cancels them all when nothing rings. `cancel` on a ringing timer stops it too.
- `ring` does nothing (empty reply, so no announcement) when the timer is running again: a +1 min that
  races the finish must not leave the kitchen chiming beside a running timer. The card's ringer also
  skips a slot whose timer runs.

Card behaviour:

- **One timer:** big `m:ss` (`h:mm:ss` from an hour) computed from `finishes_at`, a progress bar
  (elapsed / `duration`), "12 min · done at 8:01 PM", **+1 min** and **Cancel**. Cancel needs a second
  tap within 3 s. Paused shows dimmed digits and "paused".
- **Two or three timers:** one row each with the name, "12 min · done at 8:01 PM", `m:ss`, a thin
  progress bar along the bottom, **+1 min** and a ✕ that turns into a red "Cancel?" for 3 s.
- **+1 min / add** restart the timer with a longer `duration` and then move the end back, because
  `timer.change` refuses to go past the duration the timer started with; that keeps the progress bar
  in place. Take-off works the same way the other direction.
- **Ringing:** that timer turns amber ("Pasta timer done", "Kitchen timer done"); a tap on it sends `stop`
  for that slot only and silences this screen at once (before the state round trip). With one timer the
  whole card is the amber button.
- **Chime:** a Web Audio C6-E6-G6 bell (no audio file: the CSP has no `data:` in `media-src`), every 5 s
  while any `input_boolean.kitchen_timer_N_ringing` is on, for at most 10 min per ring. Only on the
  kitchen screen: a screen chimes when its own IP (read once per page load from `auth/refresh_tokens`,
  `is_current` → `last_used_ip`) equals the `ip` attribute of `chime_tracker`
  (`device_tracker.uc_display_se_21`, the kitchen display's UniFi client by MAC, so a new DHCP lease
  follows it) or is in `chime_ips` (`[192.168.0.105]`, the fallback). The ringer runs at module level
  and reads the root `<home-assistant>` element's `hass`, so it still rings while the screen sits on a
  sub-view.
- **Safety net:** any tap on the page resumes a suspended `AudioContext`; a "Tap for sound" chip shows
  on the card while audio is blocked on the chiming screen; the Voice PE announcement always fires.
- **Load line:** once per page load each screen writes a logbook entry under `timer.kitchen_timer_1`:
  `Kitchen timer card: loaded on <ip> (chime on|off), <w>x<h> @<dpr>x, audio <state>, <path>`. That
  is how to tell which screen shows which dashboard and whether audio is allowed. Headless test
  sessions log `unknown IP` (a long-lived token has no `last_used_ip`) and never chime.

End-to-end tests: 2026-10-18 20:06 (one oven timer, #262: Tom heard the chime and the announcement, one tap
stopped it). The two-timer audible test for #266 is pending; its steps and Tom's result are on issue #266.

## Voice: why custom sentences and not sentence triggers

Sentence-trigger automations (what #262 used) are matched on the text alone, before any intent, for every
satellite. A trigger whose automation condition fails still answers "Done" and never falls through, so a
trigger cannot be limited to one room (checked in the 2026.9.3 source: `conversation/trigger.py`,
`default_agent.py`). Custom sentences can: every block in `kitchen_timers.yaml` has
`requires_context: area: Kitchen`, which hassil checks against the area of the satellite that heard the
sentence. A custom intent needs a handler, and the only YAML one is `intent_script`, which needed one HA restart
to load (Tom's go, 2026-10-18 20:39). The `intent_script` block is a thin shim that calls
`script.kitchen_timer_voice`; all logic stays in UI scripts. Its `platforms` names no real domain, because
intent_script offers each intent as an LLM tool to every agent whose exposed domains overlap `platforms`.

- Edit the sentences: change the file in `/config/custom_sentences/en/`, then `conversation.reload`.
- Edit the handler: change `/config/packages/kitchen_timer.yaml`, then `intent_script.reload` (no restart now).
- Custom sentences win over the built-in timer sentences when both match. Among custom ones the parse with
  fewer wildcards wins, then more literal words. `"{kt_duration} timer"` (start) fits almost anything that ends
  in "timer", so every other command ending in "timer" keeps to one wildcard plus its own verb. "add 2 minutes
  to the pasta timer" captures "2 minutes to the pasta" in one wildcard, and the voice script splits the length
  from the name. With two wildcards it lost to start and restarted pasta at 2 minutes (found on the bench and
  fixed).
- The sentences only run on pipelines with `prefer_local_intents` (the Kitchen Assist pipeline has it).
- Other rooms: their satellites are in other areas, so these never match there and the built-in Voice PE
  timers answer exactly as before. Benched on 2026-10-18 from the Rumpus Room and Movie Room satellites.

## Deploying card changes

1. Copy the JS into the HA pod (kubectl exec + base64, see the card-deploy memory) and check the md5.
2. Bump `?v=N` on the resource. Current resource: `?v=4` (2026-10-18). Tom restarted both displays on
   `?v=3` (three timers) at 21:10 for the audible test; `?v=4` only adds the ringer guard for a timer that
   runs again, and loads at their next page reload.
3. **Restart both displays** (Tom, UniFi Connect app), or let them pick it up at their next page reload
   (every HA update reloads the frontend). Dashboard *config* edits reach an open page live
   (`lovelace_updated`), but new or changed card *code* only loads on a page reload. Until then a
   screen runs the old card code. Confirm with the load lines.

## Testing without disturbing the kitchen

- Voice: `scripts/voice-bench/run.sh bench.py "MODE=pipe REPS=1 PIPELINE=01jbqv0j9wjz49e4rnz3wptffh
  DEVICE_ID=d1add9183bc149141ffba9f93af6dc26 QUERIES='Set a pasta timer for 10 minutes|Cancel all timers'"`.
  It really starts the timers, so both screens show them while they run. Cancel them.
- **Bench cache trap:** the default agent caches recognition results by (text, language, `satellite_id`),
  and a text bench run with only `DEVICE_ID` has no satellite id. The same sentence benched from two rooms
  then gets the first room's cached answer (a Rumpus "Cancel the timer" answered a later kitchen one with the
  built-in reply). Run `conversation.reload` between rooms. Real satellites send their satellite id, so this
  does not happen in use.
- Relay directly: call `script.kitchen_timer_relay` with `return_response` (`command`, `payload` JSON).
  `ring {slot}` on a slot whose timer you cancelled directly fakes a finished timer without the Voice PE
  speaking (the announcement only comes from the ring automation on `timer.finished`).
- Layout: screenshot the live dashboard headless (memory `ha-dashboard-screenshot`) and fake the states
  **page-locally** by wrapping the root element's `_updateHass` so every states update carries fake
  `timer.kitchen_timer_N` / `_name` / `_ringing` entries. Nothing is written to HA and nothing chimes. The
  page sometimes reloads once a few seconds after loading; wait for `hass` again before installing the wrapper.
- Never let a kitchen timer run out, or turn a ringing helper on with the screens on the new code, without
  telling Tom: the kitchen chimes and the Voice PE speaks.

## Known gaps

- A timer that runs out while HA itself is down finishes during startup, before automations load,
  so nothing rings for it.
- The kitchen screen chimes by IP. The tracker gate follows a DHCP change, but if the display ever gets a
  new MAC (replaced unit) update `chime_tracker`. The Voice PE still announces either way.
- The chime volume follows the display volume; there is no per-sound volume in HA beyond the card's
  `volume` option (default 0.6).
- A phrasing none of the kitchen sentences cover goes to the Kitchen OpenAI agent, which still has the
  built-in timer tools and would start a Voice PE timer. The sentences cover the built-in timer phrasings
  plus countdown, pause/resume, take-off and "N more minutes". Delayed commands ("turn off the lights in 5
  minutes") stay built-in on purpose.
