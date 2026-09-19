# Voice assistant tuning and rollout (Voice PE + OpenAI)

Owner ask (Tom, 2026-09-18): make the Home Assistant Voice PE satellites respond quickly with
OpenAI-backed agents, bedroom first, then expose things safely, fix music, roll out to the
other three satellites, and research MCP tool sources.

**Cold start? Read `.agents/plans/voice-assist-handoff.md` first** — state at the 2026-09-19
handoff, the room-by-room test plan Tom asked for, and the ThirdReality voice device.

**Status (2026-09-19):** phase 1 (bedroom) done and voice-tested by Tom. Phase 2: every floor and
the exterior applied under Tom's rulings; guard `assist_exposure_guard` v1.18.6 deployed. Phase 4:
all four rooms on gpt-5.6-terra with modernised prompts, firmware on ESPHome 2026.8.2. Phase 3:
music routing mapped, Rumpus Room fixed; TVs/AVR/Frame open. Phase 5 not started. Next: test each
room with Tom (music included), then the ThirdReality device in the cloffice.

## Inventory (verified 2026-09-18, HA core-2026.9.2)

| Satellite | Pipeline select | Pipeline | Agent |
|---|---|---|---|
| `assist_satellite.primary_bedroom_voice_assist_satellite` | `select.primary_bedroom_voice_assistant` | Bedroom Assistant `01jbaynz8ff9fd97wfy9240zr9` | `conversation.bedroom_assist` (gpt-5.4-mini, none, priority) |
| rumpus_room | — | Rumpus Room Assist `01jtvee3cf1vfbczk2dmst64qy` | `conversation.rumpus_room_chatgpt_4` (gpt-5-mini, low) |
| kitchen_assist | — | Kitchen Assist `01jbqv0j9wjz49e4rnz3wptffh` | `conversation.chatgpt_2` (gpt-5-mini, **medium**) |
| movie_room | — | Movie Room Assist `01jk451rswcggg0xt1d5yfxr7b` | `conversation.chatgpt_5` (gpt-5-mini, **medium**) |

`openai_conversation` has two config entries on the same OpenAI account
(`01JBM33KVTM1FHF795G01R2C4X`: Music Assistant / Kitchen / Bedroom agents;
`01JK456T3JV6CPBG2ZQ2FS10GE`: Movie Room / Rumpus Room). Agents are **subentries**; the MCP
options probe cannot show their model fields, so read them from inside the HA pod
(`.storage/core.config_entries`, read-only, drop `api_key` and `prompt` before printing) and
write them with `ha_config_set_helper(helper_type="config_subentry", entry_id, subentry_type="conversation", subentry_id, config={...})`.

36 entities are exposed to Assist, so prompt size is **not** a latency factor today.

## Phase 1a — the "Bedroom GPT-5.6" error (fixed)

- Real error (HA system log): `404 model_not_found` for `GPT-Realtime-2.1`, then `gpt-realtime`.
- The account **does** list `gpt-realtime`, `gpt-realtime-2.1`, `gpt-realtime-2.1-mini`, … The 404
  is an endpoint mismatch, not a typo or an access problem: HA's integration calls the
  Responses API, and realtime (speech-to-speech) models are only served by the Realtime API.
  No HA core integration speaks the Realtime API, so that model family cannot be used here.
- Subentry `01M2TXW9MYTV11R6B8S138SY22` now runs `gpt-5.4-mini`, `reasoning_effort: none`,
  `verbosity: low`, `service_tier: priority`, web search on (Tom had enabled it).
- Cleanup (Tom approved): the subentry/device is now titled "Bedroom Assist", entity id
  `conversation.bedroom_assist` (was `conversation.bedroom_gpt_5_6`); the unused gpt-4o-mini
  agent "Bedroom ChatGPT" (`conversation.chatgpt_3`, subentry `01JZ8DWMCRGRJJ5P5YEJGSSBJJ`,
  `recommended: true`, same prompt) was deleted after confirming nothing referenced it.
  Subentry titles are renamed with the WS command `config_entries/subentries/update`.
- What Tom "probably meant": `gpt-realtime-2.1` — a real, voice-native model, but
  `v1/realtime`-only (OpenAI's model page lists Responses/Chat Completions as not supported).
  Realtime + Assist exists only as young custom components (`matt123p/ha-gemini-live` on stock
  firmware; `TristanBrotherton/voicepe-realtime` reflashes the box); core's pluggable
  voice-engine work is merged to `main` but in no 2026.9.x release. Not worth it today.

Reasoning-effort values the installed integration offers: `gpt-5.6*` none…max, `gpt-5.2–5.5`
none…xhigh, `gpt-5.1` none…high, `gpt-5`/`gpt-5-mini` **minimal**…high (no `none`).
`recommended: true` still means `gpt-4o-mini`. `gpt-6-astra` cannot be used on 2026.9.2 (the
integration sends `temperature`/`top_p`, which Astra rejects) and has no `none` effort anyway.

Price per 1M tokens in/out (OpenAI pricing page, 2026-09-18): gpt-5.4-mini 0.75/4.50,
gpt-5.4-nano 0.20/1.25 (no priority tier), gpt-5.6-luna 0.20/1.20, -terra 2/12, -sol 4/20,
gpt-5-mini 0.25/2.00, gpt-4o-mini 0.15/0.60. Priority tier = 2x; at voice volumes that is cents
a month and it trims the long tail.

## Phase 1b — measurements

Harness: `scripts/voice-bench/` (`run.sh bench.py "MODE=voice|pipe|conv|stt|tts …"`,
`run.sh debug_runs.py` for the real runs a satellite made, `run.sh openai_agents.py` for agent
settings + account model ids) — scripts run inside the HA pod and drive `assist_pipeline/run` over the WebSocket
API with a Piper-generated utterance streamed in real time, plus the `/api/stt/<engine>`,
`/api/tts_get_url` and `/api/conversation/process` REST endpoints for per-engine numbers.
Times are seconds **after the end of speech**, query "What is the temperature in the bedroom?"
(needs one `GetLiveContext` tool round trip).

| Stage | Before | After |
|---|---|---|
| End-of-speech detection (VAD, "default") | 0.9 | 0.9 |
| STT | faster_whisper 1.65 | HA Cloud 0.05 (streams while you talk) |
| LLM incl. tool round trip | gpt-4o-mini 2.4–4.1, outliers to 15 s | gpt-5.4-mini/none/priority 1.7–1.9 |
| TTS first audio (HA Cloud) | 0.2–0.8 | unchanged |
| **Total to reply text** | **5.0–6.7** | **2.6–2.8** (one 6.2 s outlier in 4 runs) |

Tom's real runs on the box after the change (debug runs, seconds after end-of-speech to reply
text): fuzzy command "kill the closet lights" 2.2 (light reacts at 1.4), temperature question
4.5, web-search question 3.9; STT 0.04–0.07 every time.

Engine comparison: STT REST round trip faster_whisper 1.7 s, HA Cloud 1.0 s, OpenAI 0.7–1.8 s
(no streaming, variable); TTS first byte HA Cloud 0.2–0.3 s, Piper 0.7–2.4 s, OpenAI 1.0–2.9 s.
LLM totals for the tool-call query: gpt-5.4-mini/none ≈ 2.0, gpt-5.4-nano/none ≈ 2.0 (no
faster, weaker), gpt-5.6-luna/none ≈ 2.3, gpt-4o-mini ≈ 2.7 with a long tail.

## Phase 1c — applied to "Bedroom Assistant"

| Setting | Was (rollback) | Now |
|---|---|---|
| `conversation_engine` | `conversation.chatgpt_3` (since deleted — recreate a `recommended: true` agent to go back) | `conversation.bedroom_assist` |
| `stt_engine` / `stt_language` | `stt.faster_whisper` / `en` | `stt.home_assistant_cloud` / `en-US` |
| TTS | `tts.home_assistant_cloud`, `JennyNeural||assistant` | unchanged (already the fastest) |
| `prefer_local_intents` | `true` | unchanged |

Left alone: `select.primary_bedroom_voice_finished_speaking_detection` = `default` (≈ 0.9 s of
silence). `aggressive` would save ~0.5 s but can cut off a mid-sentence pause; Tom was happy
without it.

How `prefer_local_intents` really behaves with an LLM agent (read from the installed
`assist_pipeline/pipeline.py` + `conversation/default_agent.py`, and confirmed live): exact-match
sentences are handled locally in ~0.1 s **except** `HassGetState` and
`HassMediaSearchAndPlay`, which are always handed to the LLM when the agent supports control.
So "turn on the closet lights" never touches OpenAI; state questions and music requests always
do, and each costs two model round trips (tool call, then the spoken answer). Replies under 60
characters are not TTS-streamed (`STREAM_RESPONSE_CHARS`), so time-to-last-token is what counts.

## Phase 2 — safe exposure plan (rulings by Tom, 2026-09-18)

### What the installed source (2026.9.2) says — the constraints

- **Exposure is the only security boundary, and it is one global list.** Every LLM tool call
  goes through `intent.async_match_targets`, which drops unexposed entities. There is no
  per-pipeline, per-agent or per-satellite exposure, no permission layer, and no native
  confirmation step. `mcp_server` sees the same list.
- **An exposed lock can be unlocked and an exposed garage door opened, with no PIN.**
  `OnOffIntentHandler` maps `HassTurnOff` on a lock to `lock.unlock` and `HassTurnOn` on a cover
  to `cover.open_cover`; the shipped prompt even teaches the model to do it. `cover` is in
  `DEFAULT_EXPOSED_DOMAINS`, so "expose new entities" (currently **off** — keep it off) would
  expose a garage door by itself. Alarm panels have no intent path, but any exposed **script** is
  an unrestricted tool: whatever the script does, the model can do.
- Tools exist only for integrations that ship an `llm.py` (light, climate, fan, media_player,
  script, todo, calendar, vacuum, humidifier, lawn_mower, assist_satellite, intent,
  intent_script). The model never sees entity ids or states up front — only names/aliases,
  domain and area — and reads state on demand with `GetLiveContext`.
- The only bulk primitive is the admin WS call `homeassistant/expose_entity` with a list of
  entity ids. Assist ignores labels entirely. Aliases are an ordered list (2026.9) whose `null`
  entry stands for the computed name — drop it and the entity answers only to spoken aliases,
  with no rename anywhere in the UI.
- Automation `conversation:` sentence triggers run before everything else on every pipeline
  and never reach the LLM — the deterministic path for anything that must not be improvised.

### Ruling 1 — the dangerous set: "ask + secure only"

Never exposed, in any form: `lock.*`, garage/gate/door covers and the two ratgdo
`button.*_toggle_door` (and the whole `button` domain), `alarm_control_panel.*`, `siren.*`,
`camera.*`, the `water_heater` domain (it is the two GE ovens, the toaster oven and the pool
heater), everything from the `intellicenter` and Gecko spa integrations, rack/USP PDU outlets,
e-bike chargers, washer/dryer/printer/server-room-AC power, UniFi firewall/VPN switches,
`zigbee2mqtt_bridge_permit_join`, camera privacy/detection switches, lock/oven config switches,
automations. Switches are **deny by default**: a switch is exposed only by name, never by area.

Voice may **ask** and may move things in the **secure direction only**:

- Status comes from read-only Template **sensor** helpers whose state is the lock's or cover's
  own word (`locked` / `unlocked`, `open` / `closed`) — never by exposing the lock or cover
  itself. Not binary sensors: with `device_class: lock` an LLM agent read `on` as "locked" for
  the (unlocked) mudroom door on 2026-09-19; with text states all six mirrors equal their sources and
  all 24 answers (four agents × six doors, each asked by name) matched the truth
  (`scripts/voice-bench/run.sh door_status_check.py`, which prints a verdict per line).
- `script.voice_lock_all_doors` locks the **three exterior doors** (front, garage side door,
  bulkhead). Tom, 2026-09-19: the mudroom↔garage door is left out — the family keeps it
  unlocked. `script.voice_close_garage_doors` closes both ratgdo covers. Each has a
  `description:` written for the model and no fields. No unlock, open, disarm, pool/spa, oven or
  PDU script is ever exposed. (Built 2026-09-19 together with the six status helpers
  `sensor.{front_door,side_door,bulkhead,mudroom_door}_lock_state` and
  `sensor.{tesla,wagoneer}_garage_door_state`.)

### Ruling 2 — workflow: agent curates, Tom approves, a guard enforces

Corrected by Tom on 2026-09-18 after a proposal that batched Kitchen + Rumpus + Movie Room from
raw area listings: **one floor at a time, slowly, and map what already controls things first**
so the voice vocabulary is the one humans already use. Concepts, not entities: a concept the
buttons treat as one thing (Movie Room "ambient lights" = gradients + floor lamps + play bars)
is one voice handle, and looks/modes are **zero-argument `script.voice_*` scripts that call
exactly what the scene-controller button calls**. Voice on/off behaves like the paddle —
automations keep running. **No hold tools** (Tom, 2026-09-19: holds are not used day to day);
the two basement hold scripts created on 2026-09-18 were removed again. The one exception is
**hot tub mode** (below).

1. Per floor: subagents map physical controls → human concepts → automation ownership (what
   turns it on/off, off-delays, hold helpers). Maps live in `agent-docs/voice-control-map.md`.
2. The agent proposes that floor's concepts in one `AskUserQuestion`; on approval it creates the
   `script.voice_*` tools live (mirrored under `home-assistant/scripts/voice/`), exposes the
   handles, and sets spoken aliases. An alias list without the `null` entry drops the machine
   name from what the model sees, without renaming anything in the UI.
3. Never exposed regardless of floor: staircases, storage, zero-delay motion lights, the
   satellites' own LED/mute/media entities, duplicate AVR/KEF registrations (music phase).
4. Housekeeping done: four dead exposures removed; areas and floors now have spoken aliases
   ("Bedroom", "Living room", "Upstairs", "Downstairs", …) — without them "the bedroom" did not
   resolve and every temperature question fell through to the LLM.
5. **AppDaemon guard** (`assist_exposure_guard`): on a schedule and on entity-registry changes,
   list exposed entities, apply the deny rules above (domains, garage-class covers,
   integrations, name patterns, deny-by-default switches and scripts with allowlists in the
   app YAML), un-expose violators and notify Tom.

### Hot tub mode (Tom's ruling, 2026-09-19)

The hold the family actually uses keeps the bright back-yard flood light **off** while people are
in the hot tub. `automation.switch_back_yard_spa_lights_manage_spotlight_auto_hold` does it: spa
lights on → auto-hold the spotlight switch (LED 130, the three spotlight automations disabled)
and turn the spotlight off; spa lights off → clear the hold. It had been dead since 2026-06-01
(it triggered on `light.westford_spa_light_1`, which no longer exists) and was switched off; it
now triggers on `light.back_yard_westford_spa_light_1` / `_2` (release when both are off), is
enabled, and was verified end to end (LED 210 → 130 → 210, automations off → on).
`script.voice_hot_tub_mode_on` / `_off` hold the parameters — the automation just calls them — so
an agent can act on "I'm going in the hot tub" / "we're out of the hot tub". Mode Off refuses
(and says so in its response) while a spa light is still on; the automation releases when
neither spa light is on any more, so an `unavailable` bulb cannot wedge the hold. A hold engaged
by voice with the spa lights never on stays until someone says they are out (the tool's
description tells the agent to remind them). Verified live: spa light on → LED 130 + three
automations off; Mode Off refused; spa light off → LED 210 + automations on. They are exposed once
the guard release that allowlists them (v1.18.4) is rolled out — until then the guard would
un-expose them.

### Shades (Tom's ruling)

One parameterized tool, `script.voice_shades(room, position)`, fires the same gateway scenes as
the ZEN32 buttons and schedules; the `cover.*` groups are no longer exposed (nothing in the house
actuates them, they cannot say tilt-open, and they use a different RF path). Plain "open" =
tilt-open upstairs, fully open downstairs — "just like the automations". Verified: mapping
table evaluated for every branch, a no-op `close` fired the right scene, and the bedroom agent
calls `script__voice_shades` for "close the bedroom shades". Rooms: primary bedroom/bathroom,
cloffice (+ privacy), kitchen, living room, dining room, study, first-floor bathroom, downstairs.

### Progress

| Floor | State |
|---|---|
| Second floor — primary suite | **Applied 2026-09-18** (Tom approved): + `climate.second_floor_ecobee`, `cover.primary_bedroom_shades`, `cover.1_6`, `cover.cloffice_shade_combined`, bedroom humidity, `media_player.primary_bedroom_lg_tv`; fan/nightstand aliases fixed. "What is the temperature in the bedroom" now answers locally in 0.05 s. Corrections after the floor map (Tom ruled): "bedroom lights" is now the new HA group `light.primary_bedroom_lights` (ceiling + nook + nightstands) instead of an alias on the five-room suite group; "nightstand lights" moved to the Z2M group `light.upstairs_primary_nightstand_lights` that the ZEN32 and the mode scripts drive (the HA group is no longer exposed). 2026-09-19: primary bathroom scripts mirror the switch config buttons (`voice_primary_bathroom_lights_on` = recessed Config 1x: all four loads + shower delay; `_lights_off` = vanity Config 1x; `_shower_lights` = shower Config 1x), the recessed group no longer claims "bathroom lights"; cloffice gets `voice_cloffice_bright` (ZEN32 big 2x, 100 % 2823 K) and the Iris lamp — dimming already works on the exposed group. A voice device for the cloffice is coming (Tom) — a ThirdReality unit, not a Voice PE; how it onboards is unverified, see *The ThirdReality voice device* in `voice-assist-handoff.md`. **Kids' rooms (Tom, 2026-09-19: "everything the ZEN32 does")**: fan light, fan, Sonos and TV in Blue (Jackson), White and Pink (Penelope) rooms, the two nightstand bulbs, area aliases "Jackson's room" / "Penelope's room", and `voice_shades` now knows `blue_room`, `white_room`, `pink_room`, `kids_bathroom` and `upstairs`. The ZEN32 relay switches stay unexposed (fan mains power). Still pending: upstairs foyer lights — see *Waiting on Tom*. |
| Basement | **Applied 2026-09-18** (Tom approved): kept recessed/ambient/TV/Shield/Sonos/AC; + rumpus lamp, `climate.rumpus_room_breeze`, concessions + hall lights; eight `script.voice_{movie,rumpus}_room_*` tools (bright, dim, red night mode, ambient scene, color toggle; the two hold tools were removed on Tom's word). Verified read-only (someone was watching a movie): on 2026-09-18 the Movie Room agent listed all six of its tools correctly (five remain after the hold removal). **Not yet exercised by voice.** |
| First floor | **Applied 2026-09-19** (Tom approved): kitchen main/island/under-cabinet/sink lights, living room recessed/lamp/sconces/fan light/fan, `climate.first_floor_ecobee`, study lights/bookshelf/fan light/fan/Sonos, dining table + cove, mudroom, entrance, the foyer chandelier, and a new HA group `light.downstairs_bathroom_lights` (vanity + fan light + shower). Two button mirrors, lights-off only: `script.voice_kitchen_lights_off` (Foyer-Chaos config 1x) and `script.voice_entrance_all_off` (Entrance config 1x) — exposed once the guard release that allowlists them (v1.18.5) is rolled out. Area aliases "Office" and "Downstairs bathroom". Never: ovens/fridge, locks, fan-controller relays, dining strip, permit-join, printer. TVs / the Frame wait for the music phase. |
| Exterior | mapped (`agent-docs/voice-control-map.md`). **Built 2026-09-19** under Ruling 1: `script.voice_lock_all_doors` (front, side, bulkhead — not the mudroom↔garage door), `script.voice_close_garage_doors` and the six read-only lock/garage status helpers. Hot tub mode (`script.voice_hot_tub_mode_on/_off`, v1.18.4) holds the back-yard flood off while the tub is in use — see *Hot tub mode* above. **Lights applied 2026-09-19** (Tom approved, and added both floods: "barnyard visibility lights used when we need a lot of light"): porch group `light.outdoor_front_porch_lights`, front door light, a new HA group `light.landscape_lights` (Lily + Calla), lamp post, driveway, back-yard dimmer, garage interior lights and the back-yard flood `light.downstairs_kitchen_back_yard_spotlight` (its person/slider/auto-off automations still run against a voice turn-on: off 5 min after the yard is clear, 45 min cap; hot tub mode still holds it off). Aliases only, no renames. Three relays — `switch.back_yard_retaining_wall_lights_relay`, `switch.shed_exterior_lights_shelly_relay`, `switch.back_yard_backyard_motion_light_relay` — are on the guard's `switch_allowlist` from v1.18.6 and get exposed once that release is rolled out. Still never: garage opener bulbs, pool/spa gear, holiday scenes. Every schedule is unconditional, so a voice change lasts until the next sunset/midnight/sunrise edge. |

### Known defects to fix while executing

- "What is the temperature in the bedroom" on the built-in agent lands on a script:
  `HassClimateGetTemperature` is climate-only, `climate.second_floor_ecobee` (the only climate
  entity in Primary Bedroom) is not exposed, and the four bedroom mode scripts are all named
  "Set Primary Bedroom to …". Expose the Ecobee; rename the scripts so the room is not the
  leading token.
- Only Primary Bedroom has room-mode scripts; Kitchen, Rumpus Room and Movie Room get a set in
  phase 4.
- `fan.primary_bedroom_fan_fan` speaks as "Primary Bedroom Fan Fan".

## Phase 4 — all four rooms on the tuned setup (applied 2026-09-19)

Tom asked for the newest fast model, so the candidates were re-measured on a throwaway agent
(text-only, reasoning off, priority tier, 6 runs × 3 questions): every GPT-5.6 tier answers a
tool-call question in ~2 s, the same as gpt-5.4-mini — latency is the two round trips, not model
size. **Chosen: `gpt-5.6-terra`** (no outliers in 18 runs; luna had two 4–5 s outliers, sol one).

| Room | Before (voice run, s after end of speech) | After (text run, tool-call question) |
|---|---|---|
| Kitchen (Regina) | ~16 (Whisper 1.6–3.1 + gpt-5-mini medium 12–13) | 2.2–3.0 |
| Movie Room | 7–11 (gpt-5-mini medium) | 2.1–2.8 |
| Rumpus Room (Jarvis) | ~7.5 (Whisper 1.7 + gpt-5-mini low) | 2.1–2.9 |
| Primary Bedroom | 2.2–4.5 (gpt-5.4-mini) | 2.5–3.2 |

Applied: all four agents → `gpt-5.6-terra`, `reasoning_effort: none`, `verbosity: low`,
`service_tier: priority`, web search kept; Kitchen and Rumpus pipelines → HA Cloud STT; Rumpus
`prefer_local_intents` false → true (like the other three — exact phrases now answer locally
without the Jarvis voice; revert if Tom misses it). Prompts: Tom's personas kept verbatim
(Rumpus lost only its stale "GPT 4o tool_calls JSON" protocol) plus one shared block —
spoken-output rules, the question-mark/open-mic rule with its reason, room context, the new voice
tools, the never-by-voice list. Backup and rationale: `agent-docs/voice-agent-prompts.md`.
Verified text-only (`scripts/voice-bench/run.sh persona_check.py`): personas intact, no trailing
question marks, brightness spoken as a percentage, lock requests refused in character.
**Not yet verified by voice** on Kitchen / Movie Room / Rumpus.

Lesson: a synthetic *voice* bench is not read-only on a Whisper pipeline — Piper audio of "Are the
rumpus room lights on?" became "Either Rumpus rim lights on" and the old agent switched the
lights on in an empty room (restored). Bench other rooms with text.

Voice PE firmware (done 2026-09-19, one box at a time, Tom's go-ahead): the four boxes are
self-compiled from the k8s ESPHome pod, so they get no OTA and have no update entities. All four
went from ESPHome 2026.1.2 (built 2026-01-31) to 2026.8.2 via `esphome compile` + `esphome upload
--device OTA` run with `kubectl exec` in the pod (yaml `home-assistant-voice-03` = Rumpus, `-04`
= Kitchen, `-097e30` = Movie Room, `-099cd4` = Bedroom; compile ~10 min under `nohup` with a log
file, upload ~10 s, reboot ~1 min; settings and pipeline selects survived). Upstream `dev` now
requires ESPHome 2026.9.0, so the pod (2026.8.2) fell back to its cached package checkout
(`0579e7b`, 2026-07-08) — that includes the 26.4.0 fix for TTS responses timing out before they
play and everything in 26.6.0. **Follow-up** (tracked in
https://github.com/thaynes43/haynes-ops/issues/2969): once the ESPHome pod is on 2026.9.x,
recompile the four boxes again to reach voice-pe 26.9.0.

## Waiting on Tom (ask one at a time — the single list; the tables above only say "pending")

- Voice-test all four rooms **with Tom, one room at a time, music in every room** (his ask,
  2026-09-19; Rumpus keeps `prefer_local_intents: true`). Plan: `voice-assist-handoff.md`.
- The cloffice voice device — Tom named it "ThrdReailty V&M Assistant Dev Edition" (ThirdReality);
  not in HA yet and nothing about it is verified. Area Primary Cloffice, own agent + pipeline,
  persona from Tom, and a speaker decision (that area has no Music Assistant player).

## Phase 3 — music (routing mapped and the Rumpus Room fixed 2026-09-19; TVs/AVR still open)

**How a box picks the speaker.** The box picks nothing. Its room agent calls the one shared tool,
`script.llm_script_for_music_assistant_voice_requests` ("Play Music", the Music Assistant LLM
blueprint), and that script resolves the target in this order:

1. a Music Assistant player named in the request (`media_player` argument, entity_id or friendly name);
2. a room named in the request (`area` argument) → `music_assistant.play_media` targeted at the
   area, i.e. **every Music Assistant player assigned to that HA area**;
3. nothing named → the agent passes the area it is in. It knows that because HA's Assist API prompt
   says "You are in area X …" (`components/intent/llm.py` in 2026.9), taken from the **area of the
   satellite device**. The `area` argument is an area selector, so HA resolves spoken names and
   aliases to area ids (`helpers/llm.py`, `ScriptTool`);
4. no area at all (a request that does not come from a satellite) → the blueprint's
   `default_player`, `media_player.primary_bedroom`.

An area with no Music Assistant player in it is a **silent failure**: `play_media` returns success,
nothing plays, and the agent says it is playing (verified by calling `play_media` at `rumpus_room`
before the fix). Pause / stop / volume / next are local intents (no LLM, ~0.1 s) and act on
**exposed** media players, preferring the satellite's area.

| Satellite (device area) | Room-less "play X" lands on | Checked |
|---|---|---|
| Primary Bedroom | `media_player.primary_bedroom` (Sonos Beam) | text-as-satellite: agent passed `primary_bedroom`, Beam played, "stop the music" paused locally |
| Kitchen | `media_player.kitchen` (Sonos Amp) | by configuration only |
| Movie Room | `media_player.movie_room` (Sonos Port — audible only if the AVR is on its input; not checked) | by configuration only |
| Rumpus Room | `media_player.ls50_wireless_ii_174476_4` (KEF LS50 W II via Music Assistant), since 2026-09-19 | text-as-satellite: played on the KEFs, "stop the music" paused locally |

Test as a satellite without speaking: `scripts/voice-bench/run.sh bench.py "MODE=pipe REPS=1
PIPELINE=<id> DEVICE_ID=<satellite device id> QUERIES='Play some Miles Davis'"` — the timeline
prints the tool call with its arguments. It really plays; check the room is empty and stop it.

The satellites' own Music Assistant players (`media_player.home_assistant_voice_*`) have no area on
purpose, so music never lands on a box's speaker. `media_player.unnamed_room` ("Pool") and
`media_player.shed` have no area either; Back Yard is grouped with Pool.

**Rumpus Room KEFs (Tom's rulings, 2026-09-19: "KEFs, at a set volume", then 50 % by ear).** The
KEFs are the Rumpus Room PC's speakers over HDMI and sit at 92 % for that, so:

- the Music Assistant KEF entity is in the Rumpus Room area, named "Rumpus Room Speakers" (aliases
  KEFs / KEF speakers / Rumpus speakers) and exposed;
- the blueprint is **locally patched** (`home-assistant/blueprints/music_assistant_llm_voice_script.yaml`):
  a `pre_actions` input that runs before `play_media`, and a `playing_before` variable. The Play
  Music script uses them to save the speakers' volume into
  `input_number.rumpus_room_kef_saved_volume` (live-only helper, 0 = nothing saved) and set 50 %
  **before** playing, and sets 50 % again in the blueprint's after-play `actions`, after waiting
  (up to 15 s) for the speaker to report `playing` — the KEFs wake from standby at their own 20 %,
  which overrode the first set in the first live test. Both only
  when the speakers were not already playing, so a volume someone chose mid-session survives the
  next request. "The speaker" is resolved as *the first Music Assistant player in the Rumpus Room
  area*, not by entity id (the KEFs have been re-registered before: `_2`, `_3`, `_4`), and exactly
  that one player is read, clamped and restored. The volume is read into a variable before
  anything is written, so two parallel runs (the blueprint is `mode: parallel`) can never save the
  50 % voice level as the PC level. If the player reports no volume at that moment, the script
  saves the usual PC level (0.92, a constant in the script) and logs a warning, so the 50 % can
  always be undone;
- `automation.rumpus_room_kefs_restore_volume_after_voice_music` clears the queue (soft-fail) and
  restores the saved volume once that player has been quiet for 10 minutes. Normal path: a state
  trigger on the KEF entity (verified: fired 10 min after the stop, 50 % → 92 %, helper → 0).
  Backstop: a 10-minute tick with the same conditions plus "the helper is at least ~10 minutes
  old", for a request that saved the volume and then never played, an HA restart mid-wait, or a
  re-registered entity; the helper-age condition keeps a tick from undoing a request that is just
  starting.

Tom confirmed (2026-09-19) that the KEFs switch back to the PC's HDMI input by themselves as soon
as the PC makes a sound, so nothing has to manage their input, and that 92 % on the KEFs is normal
(Windows is the second volume lever and attenuates it) — so restoring it is right.

The Music Assistant player only reports its own queue: over ten days of daily PC use on the HDMI
input it was never `playing` (only `idle`), and it stayed `idle` on 2026-09-19 while Tom had PC
audio running between the test plays — so "already playing" in the script can only mean voice/MA
music, and PC audio never makes a request skip the 50 % start. The wait for `playing` sits inside
the tool call on purpose: it costs a second or two normally and up to 15 s only when playback never
starts; re-clamping from an automation on every `playing` edge instead would also override a
volume someone chose and then paused/resumed.

Open: the wake-from-standby path of the after-play volume set has not been re-tested (the KEFs
were awake for every run after that step was added). Still to
do in this phase: the TVs, the AVR and the Frame (duplicate registrations), and the Sonos players
being Music Assistant-only (no turn_on/turn_off). The separate "ChatGPT for Music Assistant" agent
(`conversation.chatgpt`) belongs to the older JSON-prompt approach and is not used by the script.

## Phase 5 — MCP servers as LLM tool sources (not started)

HA's `mcp` client speaks streamable HTTP then SSE, no stdio, OAuth only (no static bearer field);
`llm_hass_api` is a real multi-select and merged tools get namespaced.
