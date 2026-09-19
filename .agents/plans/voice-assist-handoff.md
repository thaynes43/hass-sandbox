# Voice assist — cold-start handoff (written 2026-09-19, 14:45 local)

The previous session was ended by a dev-env restart, not by finishing. Everything it built is
merged and deployed; nothing is half-applied. This file is what to read first. The full record is
`.agents/plans/voice-assist-rollout.md`; the per-floor control maps are
`agent-docs/voice-control-map.md`; the four live agent prompts are backed up in
`agent-docs/voice-agent-prompts.md`.

## What Tom wants next (his words, 2026-09-19)

> We can go room by room and test the boxes, make sure we test music in each room, as well as
> work on my ThrdReailty V&M Assistant Dev Edition device.

So the next session is **interactive testing with Tom, one room at a time**, fixing what each test
turns up, and then the new device. Tom speaks to the box; you read what actually happened and fix.

## Standing rules (from Tom, all still in force)

- Read `.claude/CLAUDE.md` and the memory index. Use the `home-assistant` MCP server and load its
  best-practices skill before any write (the write key rotates hourly: re-read
  `references/scenes.md` when a write fails with `BPS_ACKNOWLEDGMENT_REQUIRED`).
- Genuine questions go to Tom **one at a time** with `AskUserQuestion`. When he is at his desk he
  often dismisses the widget and answers by typing in chat instead — treat that as the answer.
- **Never echo API keys.** `scripts/voice-bench/run.sh` pipes the HA token over stdin for you.
- Voice exposure follows the physical controls: one floor at a time, concepts scripted like the
  wall buttons (`script.voice_*`), never raw dangerous entities. Locks, garage doors, alarm,
  pool/spa gear are **ask + secure only** (status sensors, lock-only and close-only scripts).
- A new voice **script** or **switch** must be on the `assist_exposure_guard` allowlist
  (`rules.py` + `apps-prod.yaml` + README + the test's curated tuple), released and deployed
  **before** it is exposed; otherwise the guard un-exposes it in ~30 s and pushes Tom's phone.
  Lights, fans, climate, media players, covers (non-garage) and sensors pass without a release.
- Review bots are Opus 5 and re-review fresh on every push: single-purpose PRs, batch a round's
  fixes into one push, merge after round 2 unless a HIGH/behavioural finding is open, answer the
  rest on the PR. Check a bot's claim against the installed HA source before "fixing" it.
- People live here. Before any test that switches or plays something, check the room's occupancy
  sensors, check the final state afterwards, and put back what you changed.

## Live state at handoff

- AppDaemon `ghcr.io/thaynes43/appdaemon:1.18.6`; guard: 112 exposed entities, 0 violations,
  `switch_allowlist=3`.
- HA core 2026.9.2. All four agents: `gpt-5.6-terra`, reasoning `none`, verbosity low, priority
  tier, web search on; each prompt = Tom's persona + one shared block. All pipelines: HA Cloud STT
  + HA Cloud TTS, `prefer_local_intents: true`.
- Voice PE firmware: ESPHome 2026.8.2 on all four, self-compiled from the k8s ESPHome pod
  (haynes-ops issue #2969 tracks the recompile once that pod reaches 2026.9.x).

| Room | Satellite device id (for `DEVICE_ID=`) | Pipeline id | Room-less music lands on |
|---|---|---|---|
| Primary Bedroom | `9140746b067691b4aeb5a66c38a642db` | `01jbaynz8ff9fd97wfy9240zr9` | `media_player.primary_bedroom` (Sonos Beam) |
| Kitchen | `d1add9183bc149141ffba9f93af6dc26` | `01jbqv0j9wjz49e4rnz3wptffh` | `media_player.kitchen` (Sonos Amp) |
| Movie Room | `28c4d487734253cfec7955cbdee0f539` | `01jk451rswcggg0xt1d5yfxr7b` | `media_player.movie_room` (Sonos Port) |
| Rumpus Room | `f5875cab40e9e50a156e1e2e69040a85` | `01jtvee3cf1vfbczk2dmst64qy` | `media_player.ls50_wireless_ii_174476_4` (KEFs, "Rumpus Room Speakers") |

The four room agents (OpenAI `openai_conversation` **subentries**; checked live 2026-09-19). Read
settings with `scripts/voice-bench/run.sh openai_agents.py`; write them with
`ha_config_set_helper(helper_type="config_subentry", entry_id=…, subentry_type="conversation",
subentry_id=…, config={…})`:

| Room | Agent entity | Config entry id | Subentry id |
|---|---|---|---|
| Primary Bedroom | `conversation.bedroom_assist` | `01JBM33KVTM1FHF795G01R2C4X` | `01M2TXW9MYTV11R6B8S138SY22` |
| Kitchen | `conversation.chatgpt_2` | `01JBM33KVTM1FHF795G01R2C4X` | `01JZ8DWMCR7G2EJN8KVNVCR7QF` |
| Movie Room | `conversation.chatgpt_5` | `01JK456T3JV6CPBG2ZQ2FS10GE` | `01JZ8DWMCRND9599AR8EFJVN0A` |
| Rumpus Room | `conversation.rumpus_room_chatgpt_4` | `01JK456T3JV6CPBG2ZQ2FS10GE` | `01JZ8DWMCR5ZFTVM61SG13HVFR` |

(`conversation.chatgpt`, "ChatGPT for Music Assistant", is the older JSON-prompt agent; the Play
Music script does not use it.) The Inventory table in the rollout plan is the **2026-09-18
starting point** (old models); the rows above and *Phase 4* there are current.

## Tools you will use in every room

```bash
# What a REAL spoken request did (HA keeps the last 10 runs per pipeline, in memory):
scripts/voice-bench/run.sh debug_runs.py "N=4 PIPELINE=<pipeline id>"
#   -> STT text, local vs LLM, tool calls, timings, the spoken reply, errors.

# The same request as text, AS that satellite (the agent is told its area). It really acts.
scripts/voice-bench/run.sh bench.py "MODE=pipe REPS=1 PIPELINE=<pipeline id> DEVICE_ID=<device id> QUERIES='Play some jazz|Stop the music'"
#   -> prints each tool call WITH its arguments.

scripts/voice-bench/run.sh persona_check.py        # four agents, text-only persona + spoken rules
scripts/voice-bench/run.sh door_status_check.py    # locks/garage truth vs what agents say (read-only)
```

- A tool/script misbehaving: `ha_get_automation_traces("script.<id>")` shows every step and
  variable. The guard: `sensor.assist_exposure_guard` (state = exposed count; attributes
  `violations_last_run`, `last_enforced_entities`) and `kubectl logs -n home-automation
  deploy/appdaemon -c app | grep assist_exposure_guard`.
- Never run the synthetic **voice** modes (`MODE=voice`) against a room: a mis-transcription turned
  the Rumpus lights on at night once. Text (`MODE=pipe`) or Tom's real voice only.

## Room-by-room test plan

For each room: Tom speaks, you run `debug_runs.py` for that pipeline, and compare. Look for:
wrong transcript (STT), wrong target (alias/area problem), a tool that should exist but does not,
a reply ending in "?" when no follow-up was wanted (it keeps the mic open), replies over ~2–3 s
for tool-call questions, and anything the persona gets wrong. Cover in each room:

1. a local command ("turn on the lights") — should be `local=True`, ~0.1 s;
2. a state question ("is the fan on?") — goes to the LLM, ~2–3 s;
3. the room's `script.voice_*` tools (below) and dimming ("set the lights to 30 percent");
4. a house-wide tool ("lock all the doors", "are the garage doors closed?", "is the front door
   locked?") — doors can only be secured, the mudroom↔garage door is normally unlocked;
5. **music: "play <artist>" with no room, then "turn it up", "next song", "stop the music"**, and
   once "play <artist> in the <other room>";
6. shades where the room has them (`script.voice_shades`: upstairs "open" = tilt-open).

| Room | Room tools to exercise | Music notes / known gaps |
|---|---|---|
| Primary Bedroom (voice-tested by Tom on 2026-09-18 for speed only) | `light.primary_bedroom_lights`, nightstands (Tom/Kellie), fan, shades, `script.voice_primary_bathroom_{lights_on,lights_off,shower_lights}`, `script.voice_cloffice_bright`, Kellie's four bedroom scene scripts, thermostat, TV | Spoken 2026-09-19: routing right, but the Beam dropped every stream (wireless SonosNet link, see *Sonos* below). Two voice-side defects found and fixed the same day (queue replace, track-list format: rollout plan *Phase 3*). Re-test after the Beam is wired. |
| Kitchen (baseline was ~16 s before tuning; never voice-tested since) | kitchen main/island/under-cabinet/sink, `script.voice_kitchen_lights_off`, `script.voice_entrance_all_off`, living room / study / dining / mudroom / entrance lights, downstairs thermostat, `light.downstairs_bathroom_lights` | **Music never tested here**, routing is by configuration only. |
| Movie Room | `script.voice_movie_room_{bright,dim,red_night_mode,ambient_scene,color_toggle}`, recessed + ambient lights, LG TV, Shield, `climate.movie_room_breeze` | **Music never tested.** The Sonos Port feeds the AVR: find out with Tom whether it is audible with the AVR off / on another input. If not, the fix is a pre-step like the Rumpus one (power + input on `media_player.str_az5000es*`; there are three duplicate AVR registrations — pick the working one first). |
| Rumpus Room (`prefer_local_intents` stays true — Tom's ruling) | `script.voice_rumpus_room_{bright,dim,color_toggle}`, lamp, `climate.rumpus_room_breeze` | Verified by text three times and heard by Tom. Starts at **50 %** (his choice), saves the KEFs' PC level in `input_number.rumpus_room_kef_saved_volume`, restores it 10 min after music stops (`automation.rumpus_room_kefs_restore_volume_after_voice_music`). **Not verified:** a request while the KEFs are fully asleep (they wake at their own 20 %; the script waits up to 15 s for `playing` and sets 50 % again). Best tested first thing, before anyone has used that PC. |

House-wide things any box should handle: exterior lights (porch, front door, "landscape lights",
lamp post — `unavailable` in HA at handoff, driveway, back yard, garage, "flood light", "motion
flood light", "patio lights", "shed lights"), hot tub mode ("I'm going in the hot tub" →
`script.voice_hot_tub_mode_on`; off refuses while a spa light is on), the kids' rooms (everything
their ZEN32s do), both thermostats.

How music routing works, the patched blueprint, and why the Rumpus volume logic looks the way it
does: rollout plan, *Phase 3*. **The Music Assistant blueprint is locally patched**
(`home-assistant/blueprints/music_assistant_llm_voice_script.yaml`: `pre_actions` and
`enqueue_option` inputs + `playing_before` variable). Re-importing upstream silently drops it.

## The ThirdReality voice device (researched 2026-09-19; still in its box)

"THIRDREALITY Voice & Music Assistant Dev Edition" (network name `3RSPK-<MAC>`). Tom: it is for
the cloffice, but **which room it ends up in is decided after onboarding**, and so are its persona,
agent and pipeline — do not create them first. It needs a **different wake word from the bedroom
box** (they are in earshot); all four Voice PEs use "Okay Nabu".

What the research found (vendor repo `github.com/thirdreality/voice-music-assistant`; most vendor
and forum sites are blocked by the pod's egress allowlist, so treat details as unverified until the
unit is on the network):

- **Not an ESPHome device.** An Amlogic A113X Linux box whose C++ daemon *speaks* the ESPHome
  native API (TCP 6053, mDNS `_esphomelib._tcp`). No YAML, no adoption, no self-compile; firmware
  updates come from the vendor through an HA `update` entity. HA gives it a normal
  `assist_satellite` (announce, start_conversation, timers); pipeline and wake word are set in that
  entity's Configure dialog.
- **Onboarding:** USB-C power (no adapter included) → Home ring blinks yellow (else hold Home 15 s)
  → HA **phone app** → Discovered → "3RSPK-… Improv via BLE" → **HNETIoT** (2.4 GHz only) → it
  reappears as "3RSPK-… ESPHome" → Add. Checked 2026-09-19: the IoT VLAN has internet (the
  firmware refuses to start its voice service until NTP succeeds), IoT→HA 8123 is allowed for the
  TTS fetch, and HA sits on the IoT L2 at 192.168.50.249, so discovery works like the Voice PEs.
- **Wake words shipped:** okay_nabu, hey_jarvis, hey_mycroft, hey_home_assistant, okay_computer,
  hey_luna, alexa (+ two novelty ones), two slots, plus a fixed "stop". Custom ones need a firmware
  rebuild. Suggested to Tom: Hey Jarvis. Update to firmware ≥ 1.2.3 before judging the mic (quiet
  mic / garbled audio bugs before that; settings did not survive a power cycle before 1.2.2).
- **Music:** its `media_player` has no `play_media`; music goes through Music Assistant's
  **Sendspin** provider, which is already enabled (MA 2.10.3; the four Voice PEs are Sendspin
  players). MA is hostNetwork with an IoT-VLAN address (192.168.50.104, a plain DHCP lease) and
  dials out to `_sendspin._tcp` devices on the same subnet, so no firewall rule is involved *if*
  the box advertises itself; if it dials in instead it is sent to 192.168.40.59:8927, which
  "Block Local Access from IoT" stops (fix = one narrow allow rule, ask Tom first). The Primary
  Cloffice area has no MA player today, so decide with Tom where room-less music should land.
- **Known problems to plan for:** it does not reconnect after an HA restart until it is
  power-cycled (vendor issue #15, open) — this HA rolls on every upgrade, so build a workaround
  once it is in; it ships with unauthenticated root ADB on TCP 5555 and a default root SSH
  password — raise locking it down with Tom once it is online. Give it a DHCP reservation.
- Cloffice controls to cover (control-map *Second floor*): `light.upstairs_primary_cloffice_lights`
  incl. dimming, `script.voice_cloffice_bright`, `light.den_hue_iris_light`,
  `cover.cloffice_shade_combined` via `script.voice_shades`. Hold-to-dim on the wall switch is dead
  since the 2026-09-18 restart (issue #144).

## Sonos: SonosNet off + wired soundbars (agreed with Tom 2026-09-19, waits for him to be home)

Why it is in this file: the first spoken bedroom music test kept stopping. The Beam
(`media_player.primary_bedroom`, 192.168.0.6) is wireless on SonosNet with marginal links (34–41)
to the wired Amps and 2–3k PHY errors/s; moving SonosNet from channel 11 to 1 did not help. Tom's
decision: run the Sonos app's **Disable SonosNet** wizard (system-wide, app ≥ 85 — it cannot be
done over VPN, the app needs his phone on the home Wi-Fi) and re-enable the soundbars' switch
ports: **Switch Pro Max 48 PoE ports 3, 5, 6, 7** (disabled years ago because of Sonos loops).
Tom makes the port changes in the UniFi app (the mcp-unifi port tools do not work, haynes-ops
#2984); the agent verifies SonosNet is off on all 19 units first
(`http://<ip>:1400/status/wireless` → `SonosNetDisabled`, via curl from the HA pod), then watches
one port at a time. Port settings, topology, monitoring baseline and what is still owed afterwards
(two dead-Ethernet units have no disabled port; DHCP reservations; re-test bedroom music) are in
the agent memory `sonosnet-off-wired-soundbars`. Never use the per-device "Disable Wi-Fi" on a
soundbar: it cuts off its surrounds and Sub.

## Still open after testing

- Phase 3 remainder: TVs, the AVR and the Frame (duplicate registrations), Sonos players are
  Music Assistant-only (no turn_on/turn_off). Phase 5: MCP servers as LLM tool sources (research).
- Parked with cold-start context: hass-sandbox #144 (live-HA defects the floor maps found, older
  ESPHome devices), #149 (human-facing voice page for the docs site), haynes-ops #2969.
- Deploy chain for any AppDaemon change: hass-sandbox PR → merge → GHCR image → haynes-ops `tag:`
  bump PR → `flux reconcile kustomization appdaemon -n home-automation --with-source` → rollout
  status → confirm on the PR. haynes-ops `Flux Local - Test (main)` flakes on Helm repo fetches:
  push an empty commit to re-run it.
