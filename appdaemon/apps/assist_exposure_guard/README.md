# assist_exposure_guard

Enforces a deny list over Home Assistant's Assist (voice assistant) exposure
list, un-exposing anything dangerous and notifying the owner.

## Why this app exists

The Assist exposure list is the **only** security boundary in front of the LLM
voice agents. Every conversation tool call runs through
`intent.async_match_targets`, which silently drops entities that are not
exposed — and there is no per-pipeline, per-agent or per-satellite scoping, no
permission layer, and no confirmation step. "What is exposed" *is* "what a
voice can do", and the `mcp_server` integration sees the same list.

Concretely, on HA 2026.9.2:

- `OnOffIntentHandler` maps `HassTurnOff` on a `lock` to `lock.unlock` — an
  exposed lock can be unlocked by voice, with no PIN.
- `HassTurnOn` on a `cover` maps to `cover.open_cover`, and `cover` is in
  `DEFAULT_EXPOSED_DOMAINS` — so "expose new entities" would expose a garage
  door by itself.
- Any exposed `script` is an unrestricted tool: whatever the script does, the
  model can do.

The exposure list is edited from the HA UI, where "expose everything in this
area" is one click. This app is the backstop for that click (owner ruling,
2026-09-18; see `.agents/plans/voice-assist-rollout.md` §Phase 2).

## How it works

1. On startup, every `check_interval_minutes`, and — debounced by
   `registry_debounce_s` — whenever HA fires `entity_registry_updated`, list
   the entities exposed to the `conversation` assistant with the admin
   WebSocket command `homeassistant/expose_entity/list`. The ids are normalised
   once (`strip().lower()`, de-duplicated) and that single value is used for
   every lookup below, in the sensor attributes and in the un-expose call.
2. Fetch the registry entries of **those entities only**
   (`config/entity_registry/get_entries` — the whole registry is too large for
   one WebSocket frame here) for each entity's `platform` (the supplying
   integration), and read `device_class` from state
   for `cover.*` entities only (the state attribute is the *effective* class;
   the registry holds only the user override and the integration default, and
   `cover` is the only domain with a device-class rule).
3. Evaluate every exposed entity against the deny rules in `rules.py` — a pure
   module with no AppDaemon or HA imports. Each entity produces **at most one**
   violation: the first rule it breaks.
4. If `enforce` is true, un-expose every violator with **one**
   `homeassistant/expose_entity` command. If it is false, report only.
5. Notify on **two separate channels** — see *Notifications* below — plus an
   optional mobile push when `notify_service` is set.
6. Publish `sensor.assist_exposure_guard` so the guard's own health is visible.

## Rule evaluation order

| # | Rule | Effect |
|---|------|--------|
| 1 | `allow_entities` | Per-entity override — beats every deny rule below |
| 2 | `deny_domains` | Whole domains that must never be exposed |
| 3 | `deny_cover_device_classes` | Garage/gate/door covers (`cover` itself stays allowed for shades) |
| 4 | `deny_integrations` | Every entity whose registry `platform` matches |
| 5 | `deny_entity_globs` | `fnmatch` patterns over the entity id |
| 6 | `switch_allowlist` | The `switch` domain is **deny by default** — a switch is exposed by name, never by area |
| 7 | `script_allowlist_globs` | An exposed script is an unrestricted tool — the shipped default names every allowed script explicitly, no patterns |

Ordering matters for the notification text: a PDU outlet is reported as
"matches denied pattern `switch.usp_pdu_pro_*`" rather than the generic
switch-default-deny reason, and a globbed switch cannot be re-enabled by adding
it to `switch_allowlist`.

## Dependencies

- `providers/ha_provisioner` — `AssistExposureClient` (all HA WebSocket traffic;
  security rule S2)
- `providers/secrets` — `resolve_arg_secret` for `ha_url` / `ha_url_env`

## Self-provisioned entities

| Entity | Type | Purpose |
|--------|------|---------|
| `sensor.assist_exposure_guard` | `set_state` virtual sensor | Number of exposed entities; attributes carry `violations_last_run`, `violating_entities`, `last_enforced`, `last_enforced_entities`, `last_run`, `last_trigger`, `enforce`, `assistant`, `last_error` |

Both entity-list attributes are capped at 20 ids plus "…and N more" (the exact count is
`violations_last_run`), so a bulk mis-exposure cannot publish a tens-of-KB attribute.

`last_enforced` (ISO time, or `never`) and `last_enforced_entities` are the
durable half: they describe an action already taken, so they survive the clean
run that enforcement itself triggers, and are re-seeded from the sensor on
startup so an AppDaemon reload does not wipe them. Everything else describes
the current list and resets each run.

State and `violations_last_run` read `unknown` only when the check itself could
not run (HA unreachable, non-admin token). An enforcement failure keeps the
real counts and explains itself in `last_error`, so a dashboard can tell
"I could not look" apart from "I looked and could not fix it".

The app needs no helpers or relay scripts, so there is nothing for
`ha_provisioner` to create — `set_state` materialises the sensor on the first
run.

> **AppDaemon 4.5.13 note:** `set_state` kwargs go through
> `utils.clean_http_kwargs`, which converts `True` to `"true"` and then drops
> every value equal to `None` or `False` — and `0 == False`. A zero count or a
> `False` flag would therefore vanish from the published state, so the sensor
> state and every attribute are published as non-empty strings.

## Known limitations

These are deliberate fail-open choices — this app is a backstop, and a false
positive costs a human a manual re-exposure in the HA UI:

- **A cover whose `device_class` cannot be read is treated as unclassified**
  for that run, so the garage/gate/door rule cannot fire. That happens when the
  entity is briefly unavailable; failing closed would un-expose window shades
  during an HA blip. The miss is logged at `WARNING` and the next check catches
  it. It also means a garage opener that ships *no* `device_class` is invisible
  to rule 3 — add it to `deny_entity_globs` or `deny_domains` instead.
- **An entity with no registry entry has no `platform`**, so `deny_integrations`
  cannot match it. Every integration-backed entity has a registry entry; entities
  created purely in state (template sensors defined in YAML, `set_state` virtual
  sensors) do not.
- **A malformed exposed id has no `platform` either.** An id that is not a
  well-formed `domain.object_id` is left out of the registry request (HA
  validates the id list all-or-nothing) and logged at WARNING by
  `AssistExposureClient` (the AppDaemon main log, not this app's log); the domain, glob
  and deny-by-default rules still apply to it. It also cannot be un-exposed
  by this app, so it is left out of the un-expose batch — the rest of the
  batch still applies. See *Partial enforcement* below: it is reported as
  **still exposed** every run until a human removes or renames it.

## Notifications

There are **two** persistent notifications, because they answer two different
questions, and conflating them made enforcement erase its own evidence.

| Notification | id | Says | Cleared by |
|---|---|---|---|
| Enforcement record | `<notification_id>_enforced` | "I un-exposed these, at this time" — an action already taken | **Only the user.** Never auto-dismissed |
| Current state | `<notification_id>` | "These are exposed right now and should not be" (`enforce: false`), or "UN-EXPOSE FAILED — still exposed", or "the check could not run" | The next clean run |

Why they must be separate: `homeassistant/expose_entity` makes HA fire
`entity_registry_updated`, which arms this app's own `registry_debounce_s`
timer. That re-check finds a clean list — because the app just cleaned it — so
anything auto-cleared on a clean run is gone within ~30 seconds of being
written. An enforcement report is a record, not a state, so it lives on its own
id and survives. A second enforcement replaces the body under the same id with
a freshly timestamped one, so the record stays one entry rather than a stack.

The same reasoning applies to the sensor: `violations_last_run` and
`violating_entities` describe the current list and go to `0` / `none` on that
clean re-check, while **`last_enforced` and `last_enforced_entities` persist**.
They are also re-seeded from the sensor on startup, so an AppDaemon reload does
not erase the record either.

### Partial enforcement

A run can un-expose some violators and leave others. HA validates
`entity_ids` all-or-nothing, so `AssistExposureClient.set_exposure` filters
malformed ids out of the batch rather than letting one of them make HA reject
the lot — and it returns an `ExposureChange` naming what it `sent` and what it
`skipped`. **Both notifications can therefore appear from the same run**, and
the guard partitions the violations by what actually applied:

- the ones in `sent` get the enforcement record, sized and listed from those
  ids only — it never claims an entity was un-exposed while it is still
  exposed;
- everything else goes down the current-state path: the "UN-EXPOSE FAILED —
  STILL EXPOSED" notice naming the ids, a phone push, and `last_error`
  spelling out that HA would not accept them. That repeats every run, because
  nothing here can fix it — only a human removing or renaming the entity can.
  When that happens, the next run applies the change and clears the notice.

If **nothing** applied, no enforcement record is written at all. The sensor
stays consistent across the split: `last_enforced_entities` lists only what
changed, while `violating_entities` and `violations_last_run` describe
everything the run found.

`notify_service` mirrors notifications to a phone — the only copy nothing can
clear, so it is set in `apps-prod.yaml`. Pushes are deduplicated by condition,
not by run: every **enforcement** pushes (each is a distinct event), but an
unchanged current-state condition — the same report-only finding, or the same
check failure repeating while HA is down — pushes **once**. It pushes again
when the violating entity set changes, when the error text changes, or after
the condition clears and returns. Without that, a standing finding would buzz
every `check_interval_minutes` until the owner muted the app, and a muted app
reports nothing at all. The persistent notification is still refreshed every
run, so its timestamp stays current.

## Associated card

None. The sensor is intended for an entities card or a template badge.

## Config reference

| Key | Required | Default | Purpose |
|-----|----------|---------|---------|
| `ha_url` / `ha_url_env` | yes | — | HA base URL (`!secret ha_url`, or an env var name) |
| `ha_token_env` | yes | — | **Name** of the env var holding the admin long-lived token (rule S1/S7). The exposure WebSocket commands are `require_admin` |
| `assistant` | no | `conversation` | Assistant id to guard. `cloud.alexa` / `cloud.google_assistant` also work |
| `check_interval_minutes` | no | `15` | Periodic re-check. Floored at 60 s |
| `registry_debounce_s` | no | `30` | Quiet period after an `entity_registry_updated` burst |
| `enforce` | no | `true` | `true` un-exposes violators; `false` reports only |
| `notify_service` | no | *(unset in code; set in `apps-prod.yaml`)* | Mirror every notification to a phone, e.g. `notify/mobile_app_toms_iphone_air`. Accepts `notify.x`, `notify/x` or a bare `x` |
| `notification_id` | no | `assist_exposure_guard` | Current-state notification id. The enforcement record uses `<notification_id>_enforced` |
| `status_sensor` | no | `sensor.assist_exposure_guard` | Status sensor entity id |
| `registry_event` | no | `entity_registry_updated` | Event that triggers a debounced re-check |
| `deny_domains` | no | see below | Domains never exposed |
| `deny_cover_device_classes` | no | `garage`, `gate`, `door` | Cover device classes never exposed |
| `deny_integrations` | no | `intellicenter`, `gecko` | Registry `platform` values never exposed |
| `deny_entity_globs` | no | see below | `fnmatch` patterns over the entity id |
| `switch_allowlist` | no | `[]` | The only switches allowed to be exposed |
| `script_allowlist_globs` | no | see below | The only scripts allowed to be exposed |
| `allow_entities` | no | `[]` | Per-entity override beating every deny rule |

A list key that is **absent** takes the default; a list key present but empty
(`deny_domains: []`) is honoured as empty, which is how an operator
deliberately disables a rule.

### Default deny lists

```yaml
deny_domains: [lock, alarm_control_panel, siren, camera, button, input_button,
               valve, water_heater, automation, update, number, select, lawn_mower,
               scene, input_boolean, input_select, input_number, input_text, input_datetime]
deny_cover_device_classes: [garage, gate, door]
deny_integrations: [intellicenter, gecko]
deny_entity_globs:
  - "switch.power_distribution_*"
  - "switch.usp_pdu_pro_*"
  - "switch.*ebike*"
  - "switch.*mombike*"
  - "switch.dryer_power"
  - "switch.washer_power"
  - "switch.*printer*"
  - "switch.server_room_ac_power_switch"
  - "switch.unifi_network_*"
  - "switch.*unifi_network_*"
  - "switch.zigbee2mqtt_bridge_permit_join"
  - "switch.*_privacy_mode"
  - "switch.*_detections_*"
  - "switch.ratgdov25i_*"
  - "light.ratgdov25i_*"
  - "switch.spa_intouch3_switch"
  - "switch.nrz120804q_*"
switch_allowlist: []
# No patterns on purpose — see below.
script_allowlist_globs:
  - "script.voice_movie_room_bright"
  - "script.voice_movie_room_dim"
  - "script.voice_movie_room_red_night_mode"
  - "script.voice_movie_room_ambient_scene"
  - "script.voice_movie_room_color_toggle"
  - "script.voice_movie_room_hold_lights"
  - "script.voice_rumpus_room_bright"
  - "script.voice_rumpus_room_dim"
  - "script.voice_rumpus_room_color_toggle"
  - "script.voice_rumpus_room_hold_lights"
  - "script.voice_shades"
  - "script.llm_script_for_music_assistant_voice_requests"
  - "script.kellie_mobile_primary_bedroom_relaxed"
  - "script.kellie_mobile_primary_bedroom_focused"
  - "script.kellie_mobile_primary_bedroom_bedtime"
  - "script.kellie_mobile_primary_bedroom_sleep"
allow_entities: []
```

### Why the script list has no patterns

An exposed script is an unrestricted tool: whatever the script does, the model
can do — and these are not "secure-direction-only". `script.voice_*_hold_lights`
disables automations by design, which is exactly the kind of power that must be
reviewed rather than inferred from a filename.

A glob such as `script.voice_*` would make the **filename** the security
boundary: anyone creating `script.voice_anything` later would hand the voice
agent a tool nobody looked at. So every allowed script is named explicitly.
Adding a new voice tool means adding it to this list in the same PR that
creates the script. (The config key is still `fnmatch`-matched, so an operator
*can* configure a pattern — the shipped default simply does not.)

## Manual setup required

None. The app provisions nothing and reads nothing from the filesystem.

Two operational notes:

- **The token must be admin.** `homeassistant/expose_entity` and
  `homeassistant/expose_entity/list` are decorated `@websocket_api.require_admin`.
  The AppDaemon long-lived token already is; a non-admin token (or a missing
  one) fails the whole check, which raises the "check failed" notification and
  lands in `sensor.assist_exposure_guard`'s `last_error`. The app still starts:
  the client is built lazily so a credential problem cannot leave the guard
  permanently inert and silent — it retries on the next scheduled check.
- **There is only one exposure list.** A dev instance of this app therefore
  reads and could write production state — run it with `enforce: false`.

## Upstream / downstream dependencies

Standalone. Nothing else in this repo reads its sensor or its events.

It is, however, the enforcement half of the voice-assistant rollout: the
curation half (per-room exposure proposals, spoken aliases, the hand-written
`script.voice_*` tools) is applied by hand in HA. **When a curated room adds a
switch or a script, add it to `switch_allowlist` / `script_allowlist_globs` in
BOTH `rules.py` (the default) and `apps-prod.yaml`, in the same PR** —
`test_prod_yaml_rule_lists_equal_the_code_defaults` fails when only one of the
two is edited (fix the drift, never the test), and without the entry this app
un-exposes the new tool within `check_interval_minutes`. That coupling is the point: a new voice tool gets
reviewed here or it does not reach a voice agent.
