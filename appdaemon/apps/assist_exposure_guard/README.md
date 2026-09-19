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
   WebSocket command `homeassistant/expose_entity/list`.
2. Fetch the entity registry (`config/entity_registry/list`) for each entity's
   `platform` (the supplying integration), and read `device_class` from state
   for `cover.*` entities only (the registry's partial dict does not carry it,
   and `cover` is the only domain with a device-class rule).
3. Evaluate every exposed entity against the deny rules in `rules.py` — a pure
   module with no AppDaemon or HA imports. Each entity produces **at most one**
   violation: the first rule it breaks.
4. If `enforce` is true, un-expose every violator with **one**
   `homeassistant/expose_entity` command. If it is false, report only.
5. Raise a persistent notification under a stable `notification_id` (so it
   updates rather than stacks) listing entity ids and reasons, plus an optional
   mobile push when `notify_service` is set. A clean run dismisses it. If the
   un-expose call itself fails, the notification says **UN-EXPOSE FAILED** and
   names the entities that are *still* exposed — the most urgent state this app
   can be in, and one it must never report silently.
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
| 7 | `script_allowlist_globs` | An exposed script is an unrestricted tool |

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
| `sensor.assist_exposure_guard` | `set_state` virtual sensor | Number of exposed entities; attributes carry `violations_last_run`, `violating_entities`, `last_run`, `last_trigger`, `enforce`, `assistant`, `last_error` |

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

Both are deliberate fail-open choices — this app is a backstop, and a false
positive costs a human a manual re-exposure in the HA UI:

- **A cover whose `device_class` cannot be read is treated as unclassified**
  for that run, so the garage/gate/door rule cannot fire. That happens when the
  entity is briefly unavailable; failing closed would un-expose window shades
  during an HA blip. The miss is logged at `WARNING` and the next check catches
  it. It also means a garage opener that ships *no* `device_class` is invisible
  to rule 3 — add it to `deny_entity_globs` or `deny_domains` instead.
- **An entity with no registry entry has no `platform`**, so `deny_integrations`
  cannot match it. Every integration-backed entity has one; entities created
  purely in state (template sensors defined in YAML, `set_state` virtual
  sensors) do not.

## Notification lifetime

The persistent notification reflects the **current** state of the exposure
list, not the history of it. With `enforce: true` the violators are un-exposed
during the same run, so the very next check is clean and dismisses the
notification — typically within `check_interval_minutes`. If nobody is looking
at Home Assistant in that window the notification is gone by the time they are.

Set `notify_service` if that matters: a mobile push is the durable record of
"something was exposed and I took it away", and the app sends one alongside
every persistent notification. The AppDaemon log keeps the same information at
`WARNING` regardless.

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
| `notify_service` | no | *(unset)* | Extra push, e.g. `notify/mobile_app_toms_iphone_air`. Accepts `notify.x`, `notify/x` or a bare `x` |
| `notification_id` | no | `assist_exposure_guard` | Persistent-notification id (stable so it updates in place) |
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
               valve, water_heater, automation, update, number, select, lawn_mower]
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
  - "switch.spa_intouch3_switch"
  - "switch.nrz120804q_*"
switch_allowlist: []
script_allowlist_globs:
  - "script.voice_*"
  - "script.llm_script_for_music_assistant_voice_requests"
  - "script.kellie_mobile_primary_bedroom_*"
allow_entities: []
```

## Manual setup required

None. The app provisions nothing and reads nothing from the filesystem.

Two operational notes:

- **The token must be admin.** `homeassistant/expose_entity` and
  `homeassistant/expose_entity/list` are decorated `@websocket_api.require_admin`.
  The AppDaemon long-lived token already is; a non-admin token fails the whole
  check and the failure lands in `sensor.assist_exposure_guard`'s `last_error`.
- **There is only one exposure list.** A dev instance of this app therefore
  reads and could write production state — run it with `enforce: false`.

## Upstream / downstream dependencies

Standalone. Nothing else in this repo reads its sensor or its events.

It is, however, the enforcement half of the voice-assistant rollout: the
curation half (per-room exposure proposals, spoken aliases, the
`script.voice_*` secure-direction-only scripts) is applied by hand in HA. When
a curated room adds a switch or a script, add it to `switch_allowlist` /
`script_allowlist_globs` in `apps-prod.yaml` — otherwise this app will
un-expose it within `check_interval_minutes`.
