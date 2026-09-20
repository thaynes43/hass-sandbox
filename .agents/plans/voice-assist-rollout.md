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

## Inventory (as found on 2026-09-18, HA core-2026.9.2 — the STARTING point; models and prompts were replaced in Phase 4, current ids are in `voice-assist-handoff.md`)

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
**Not yet verified by voice** on Movie Room / Rumpus; on Kitchen only one spoken music request
(2026-09-19, played fine) — lights, state questions, door tools and the persona are still unspoken
there.

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

1. a Music Assistant player named in the request (`media_player` argument, entity_id or friendly
   name). The agent then passes **no area** (the script targets the union of both, so its own room
   would start playing too), and a named speaker that is **not** a Music Assistant player stops
   the request ("that speaker was not found") even when an area or other, valid speakers came with
   it — it is never dropped silently (blueprint variable `unresolved_players`);
2. a room named in the request (`area` argument) → `music_assistant.play_media` targeted at the
   area, i.e. **every Music Assistant player assigned to that HA area** (a room *and* a speaker,
   both named on purpose, target both);
3. nothing named → the agent passes the area it is in. It knows that because HA's Assist API prompt
   says "You are in area X …" (`components/intent/llm.py` in 2026.9), taken from the **area of the
   satellite device**. The `area` argument is an area selector, so HA resolves spoken names and
   aliases to area ids (`helpers/llm.py`, `ScriptTool`);
4. no area and no player → **handed back, nothing plays** (Tom's ruling 2026-09-19: "hand it back,
   never guess"). The blueprint returns "call this tool again with the area the request comes from,
   or ask which room", and the Play Music script no longer sets a `default_player`. Until then the
   fallback was `media_player.primary_bedroom`, and a room agent that left its area out (seen on
   "play <song> by <artist>" and on add-to-queue requests) played in the bedroom from another room.

An area with no Music Assistant player in it is a **silent failure**: `play_media` returns success,
nothing plays, and the agent says it is playing (verified by calling `play_media` at `rumpus_room`
before the fix). Pause / stop / volume / next are local intents (no LLM, ~0.1 s) and act on
**exposed** media players, preferring the satellite's area.

| Satellite (device area) | Room-less "play X" lands on | Checked |
|---|---|---|
| Primary Bedroom | `media_player.primary_bedroom` (Sonos Beam) | text-as-satellite: agent passed `primary_bedroom`, Beam played, "stop the music" paused locally |
| Kitchen | `media_player.kitchen` (Sonos Amp) | Tom's spoken request on 2026-09-19 played there; three text-as-satellite single-song requests carried `kitchen` and played |
| Movie Room | `media_player.movie_room` (Sonos Port — audible only if the AVR is on its input; not checked) | text-as-satellite, many runs on 2026-09-19 (the empty-room test bed): agent passed `movie_room`, the Port played; never heard by anyone |
| Rumpus Room | `media_player.ls50_wireless_ii_174476_4` (KEF LS50 W II via Music Assistant), since 2026-09-19 | text-as-satellite: played on the KEFs, "stop the music" paused locally |

Test as a satellite without speaking: `scripts/voice-bench/run.sh bench.py "MODE=pipe REPS=1
PIPELINE=<id> DEVICE_ID=<satellite device id> QUERIES='Play some Miles Davis'"` — the timeline
prints the tool call with its arguments. It really plays; check the room is empty and stop it.

The satellites' own Music Assistant players (`media_player.home_assistant_voice_*`) have no area on
purpose, so music never lands on a box's speaker. `media_player.unnamed_room` ("Pool") and
`media_player.shed` have no area either; Back Yard is grouped with Pool.

**Rumpus Room KEFs: no special handling (Tom's ruling, 2026-09-19 evening).** The KEFs are the
Rumpus PC's speakers over HDMI; the Music Assistant KEF entity is in the Rumpus Room area, named
"Rumpus Room Speakers" (aliases KEFs / KEF speakers / Rumpus speakers) and exposed, so music lands
there like in any other room. An earlier version of the Play Music script saved "the PC volume",
started voice music at 50 % and restored the level with an automation ten minutes after the music
stopped. It was removed the same day — script `pre_actions`/`actions`, the helper
`input_number.rumpus_room_kef_saved_volume` and
`automation.rumpus_room_kefs_restore_volume_after_voice_music` — because it was solving a problem
that does not exist: the speakers keep their own volume per input (streaming plays at the level
they remember for it, 50 % at the time; the PC input comes back at its own level by itself), and
the owner's ruling is to let them play like all the rest and smooth out anything odd later
(issue #159, closed). The blueprint's `pre_actions` input stays, unused.

**Moving and sharing music (2026-09-19 evening).** Tom asked the kitchen box to move the music to
the living room and got a different song: the agents only had Play Music, which starts something
new. Two more voice tools, both area-based like Play Music and both on the guard allowlist since
v1.18.7: `script.voice_move_music` (`music_assistant.transfer_queue`: the same song carries on in
the new room, the old room stops) and `script.voice_group_music` (`media_player.join` / `unjoin`
on the Music Assistant players = a native Sonos group: the same song in several rooms in sync, and
rooms leaving again). Verified by direct calls with Tom listening: kitchen joined the living room
on the same track and left again with the living room still playing; a move living room → kitchen
continued at the same position; the Rumpus KEFs joined a Sonos group too (Music Assistant syncs
them); and removing the room that **leads** the group works because the tool hands the queue to a
room that stays first (a plain unjoin of the leader left the wrong room playing). Seen once and not reproduced: on the first ungroup Music Assistant
2.10.3 logged `maximum recursion depth exceeded` in its stream feeder and the living room player
then accepted requests without playing until Music Assistant was restarted.

**First voice test of Move/Group Music (Tom, 2026-09-19 late evening) — three fixes, all live.**
(1) Both tools took rooms as HA **area** fields; when the agent passed "bathroom" / "bedroom" (areas:
"Primary Bathroom" / "Primary Bedroom") Home Assistant crashed the whole request
(`IndexError` at `helpers/llm.py` `intent.find_areas(...)[0]`, red blink, no answer). Both tools now
take rooms from a **fixed select list** mapped to area ids inside the script, like `voice_shades`.
Play Music still uses the blueprint's area field; it has passed exact area ids every time, so it
was left alone — switch it to a list if it ever blinks red. (2) The agent named the room it was in
as the source while the music played in the other named room: Group Music now picks whichever
named room is really playing. (3) Re-grouping rooms that were already grouped, with a *member* as
source, made Music Assistant tear the group apart and the music died after a second: the tool now
uses the group's **leader** and leaves an existing group alone. Verified by replaying Tom's exact
phrases as the bedroom box: bedroom + bathroom in sync, held.

**"Stop" ungroups, "pause" keeps the group (Tom's ruling, 2026-09-19).** Home Assistant's built-in
intent answers "stop the music" with *Paused* and leaves a speaker group together; when the bedroom
soundbar then switched to the TV, the TV audio also played in the grouped bathroom.
`automation.voice_stop_the_music_stop_and_ungroup` is a **sentence trigger** (it runs before the
built-in intents, locally, ~0.3 s): it stops the music in the room the request comes from (or, if
nothing plays there, wherever music is playing), unjoins the followers, and never touches a
soundbar whose source is `TV` (a leader on its TV input keeps playing; only its followers are
released). Music Assistant reports a *paused* Sonos group as `idle`, so grouped players are in scope
even when idle, and "stop the music everywhere" covers every room. "Pause" is untouched. Verified as the kitchen box (kitchen + living
room grouped → "Stopped.", both idle, group gone) and as the bedroom box with the TV on
("No music is playing.", TV untouched).

**Queue, track lists and the default player (2026-09-19, after Tom's first spoken music test in
the bedroom).** Two defects found by that test, plus a third (no default player, routing order
item 4 above) found while fixing them; all fixed live and verified by text-as-satellite — mostly on
the Movie Room box (empty room, AVR path), in the evening also as the Rumpus and Kitchen boxes:

- *Old queue came back.* Music Assistant's default `enqueue` for a **track** request is `play`
  (play now, keep the old queue); artists, albums and playlists default to `replace`
  (`controllers/player_queues/config.py`, MA 2.10.3). A mood request is a track list, so whatever an
  earlier request had queued played next (Miles Davis after "party music"). The blueprint has a
  third local input, `enqueue_option` (default "Music Assistant default" = upstream behaviour); the
  Play Music script sets it to `replace`. Verified: a 227-item artist queue became exactly the 5
  requested tracks. So that "replace by default" does not take queueing away, the blueprint also
  has an optional LLM-facing field `queue` (`add` / `next`, only when the request talks about the
  queue); it wins over `enqueue_option`, and for it the shuffle step only runs when the request
  itself asked to shuffle (the existing queue is kept, and its shuffle setting is somebody's
  choice; the flip side: an "add" that ends up building the whole queue, because the target's queue
  was empty, inherits whatever shuffle setting that player was left with). Found while testing it: on "add X to the queue" the agent
  **left the area out**, and the script's fallback is the default player, so the song went into the
  bedroom Beam's queue from the Movie Room. Two fixes: the script's `area_prompt` says an
  add/next request still needs the area, and the blueprint **hands an add/next request without an
  area or player back** ("call this tool again and give the area") instead of using the default
  player. Verified: first call without area → handed back → second call with `movie_room` →
  queued there; "play Waterloo next" passed the area straight away. Review rounds 2 and 3 shaped
  the rest: an "add" aimed at a speaker that is **not playing** would start nothing (a silent success),
  and turning such an add into `replace` would destroy a **paused** queue. So `add`/`next` are passed on as they are only when **every** targeted Music
  Assistant player is already playing (blueprint variable `queue_effective`; unavailable/unknown
  players are not counted, so a stale entity cannot switch queueing off); otherwise the
  request becomes `play` — the song starts now and the existing queue is kept behind it. An add is
  therefore never silent and never destructive. Verified on the Movie Room player: paused
  227-item queue + "add Dancing Queen" → `enqueue: play`, playing, 228 items, old queue intact;
  then "add Waterloo" while playing → `enqueue: add`, 229 items, current song kept; shuffle step
  skipped both times. The same two cases were repeated on the Rumpus KEFs that evening (227 →
  228 → 229 items) before their volume logic was removed.
  Also verified: from the Movie Room box "play Miles Davis on the Rumpus Room Speakers" → the agent
  invented `media_player.kefs`, the hand-back named it, the retry carried `rumpus_room`, and only
  the KEFs played (before the hand-back that request would have gone to the bedroom default);
  three "play <song> by <artist>" requests as the Kitchen box all carried `kitchen`.
  **Open for the TVs/AVR work below:** `queue_targets` ignores `unavailable`/`unknown` players but
  not `off`/`standby` ones. Every room has exactly one Music Assistant player today and none of
  them reports `off`, but a TV- or AVR-backed MA player added to a room would, while powered down,
  keep "every target is playing" from ever holding there, and each add would play now instead of
  appending. Decide the rule when such a player is added (should a powered-down TV be a music
  target at all?), then extend the filter.
- *Invented track lists did not resolve.* For "party mood" the agent first sent
  `Title - Artist featuring X` entries; Music Assistant splits on " - " as *artist - title*, so
  **none** of them resolved and the call failed (`Could not resolve [...]`; a list where only some
  entries resolve plays those and reports nothing — two later test lists played 4 of 5). Its retry
  used bare titles, which matched the wrong versions (a KIDZ BOP "Party Rock Anthem"). The
  script's `media_id_prompt` input now spells out `Artist name - Song name`, main artist only, at
  most five songs for a mood request (one artist named → `artist` parameter + bare song names),
  and "retry with fewer, more famous songs" when the tool could resolve none. Verified: five
  entries in the right form, one call (script run 1.1 s; the 7-track kitchen request had taken
  4.7 s). Mood → *playlist* is not an option here: `music_assistant.search` returns no provider
  playlists for "party hits", only library playlists.

The stop-start playback Tom heard in the bedroom the same day was **not** a voice defect: the Beam
is wireless on SonosNet with marginal links and dropped every stream that afternoon (`ERROR_LSE`,
`ERROR_BUFFERING`). A text re-test at 18:35, after Tom had moved SonosNet from channel 11 to 1 and
with the house empty, played 5.5 minutes without a stream error although the link numbers had not
changed — "marginal, currently working", not fixed. Plan agreed with Tom: SonosNet off + soundbars
wired (`backlog/002-sonosnet-off-wired-soundbars.md`).

Still to do in this phase: the TVs, the AVR and the Frame (duplicate registrations), and the Sonos players
being Music Assistant-only (no turn_on/turn_off). The separate "ChatGPT for Music Assistant" agent
(`conversation.chatgpt`) belongs to the older JSON-prompt approach and is not used by the script.

## Phase 5 — MCP servers as LLM tool sources (not started)

HA's `mcp` client speaks streamable HTTP then SSE, no stdio, OAuth only (no static bearer field);
`llm_hass_api` is a real multi-select and merged tools get namespaced.
