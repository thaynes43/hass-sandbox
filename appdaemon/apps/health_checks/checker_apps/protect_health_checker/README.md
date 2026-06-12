# Protect Health Checker

Detects and auto-heals the silent UniFi Protect websocket freeze. The failure mode: the Protect integration's websocket dies with **zero log errors** — camera entity attributes keep updating, but motion/smart-detection binary sensors stop changing state entirely. Every affected sensor keeps an identical `last_changed` (the moment it was last re-registered). The proven fix is reloading the Protect config entry, which this checker performs automatically.

## Checks

| Check | Method | Healthy When |
|-------|--------|-------------|
| Sensor Discovery | `integration_entities()` template rendered server-side via `HaAdminClient` (or the `motion_entities` config override) | At least one Protect event sensor found |
| Sensor Availability | Share of event sensors `unavailable` longer than `availability_grace_s` | Below `availability_critical_pct` (critical) and zero (ok); anything in between is a warning |
| Camera Events | Newest `last_changed` across **available camera-group** sensors, aged in **active-hours seconds** | Younger than `stale_after_s`, or (while frozen) a genuine post-freeze event has arrived |
| Entry Sensors | Availability of the USL entry-sensor group, with a last-event detail | All entry channels available (quiet doors never page) |

### Sensor Discovery

Discovery renders a Jinja2 template against live HA that lists every `binary_sensor` owned by `integration_domain` (entity ID, device class, state, `last_changed`, device ID), then classifies by **device**: any Protect device that exposes a `door`/`moisture`/`tamper` channel is an **entry sensor** (USL) — its motion and contact channels join the entry group; everything else with `device_class: motion` or an `_detected` suffix is a **camera**. No entity list to maintain — new cameras are picked up automatically.

- **ok** — N event sensors discovered
- **warning** — template render failed but a cached sensor list from a previous cycle is available (falls back to per-entity `get_state`)
- **critical** — no event sensors found (config entry not loaded?), or no `ha_url`/`ha_token_env` and no `motion_entities` override

Setting `motion_entities` skips template discovery entirely and monitors exactly that list (all treated as the camera group).

### Sensor Availability — the hard-outage fast path

The freeze the staleness check hunts is *silent*: sensors stay available but stop changing. A **hard** outage (UNVR down, auth failure like the 2026-06-11 401s) looks completely different — every entity flips to `unavailable` at once. Two properties make this its own check:

- An `unavailable` transition **refreshes `last_changed` without being an event**, so the staleness clock restarts and would not fire for another `stale_after_s` (3h). The availability check pages in `availability_grace_s` (default 15m) instead.
- A sensor counts as *down* only after it has been unavailable for longer than the grace period (its `last_changed` is the transition moment; entities missing from the state machine entirely are timed from first observation). The grace also absorbs the brief unavailable blip a config-entry reload causes, so auto-heal cannot trip its own alarm mid-repair.
- Recovery is **latched**: once an outage is confirmed, only sensors actually coming back available clear it. A reload re-registers every entity and resets every `last_changed` — without the latch, a *failed* heal would reset the dwell clocks, false-resolve the page, and flap on every retry. (This is the availability-path analogue of the frozen-baseline rule below.)

`availability_critical_pct` (default 90) of sensors down → **critical** (integration-level outage → pages, and auto-heal fires). Any smaller subset down → **warning** (e.g. the USL group dropping off overnight) — visible in Alertmanager/dashboard, no page.

### Camera Events — active-hours staleness

Overnight the house is quiet and some cameras legitimately see nothing for ~12 hours, so wall-clock staleness would false-positive every morning. Instead, the age of the newest event is measured as the **overlap between [newest event, now] and the daily active window** (`active_start`–`active_end` in `active_tz`, must not cross midnight). Overnight hours contribute zero staleness, and a freeze is only ever *declared* while the current time is inside the window. With the defaults (08:00–23:00, 3h threshold), a websocket that dies overnight is detected by ~11:00 the next morning.

Freshness only ever considers **available camera-group** sensors: unavailable sensors are excluded (their `last_changed` is a transition, not an event), and entry-sensor activity cannot mask a camera freeze. If every camera sensor is unavailable the check reports `unknown` and Sensor Availability carries the alert — unless a freeze was already firing, in which case it stays critical so the page is not dropped mid-incident.

### Entry Sensors

The USL entry sensors (motion + contact channels) get their own line item: **ok** with a newest-event detail while available, **warning** when channels are unavailable past the grace period. There is deliberately no freshness threshold — a door that nobody opens for a day is normal and must never page.

## Frozen-Baseline State Machine

```
healthy → frozen     (no event for stale_after_s of active hours, inside the window;
                      baseline = newest event timestamp at detection)
frozen  → frozen     (critical every cycle until a GENUINE event arrives)
frozen  → healthy    (any sensor's last_changed strictly newer than the baseline)
```

While frozen, the check stays **critical** until an event arrives that is *strictly newer than the baseline*. The baseline is what makes auto-heal verification honest:

- At detection time, the baseline is the newest event timestamp seen.
- **After a config-entry reload, every Protect entity is re-registered with a fresh `last_changed`.** Those timestamps are newer than the detection baseline, so without compensation the checker would instantly "recover" — and every reload would look successful even if the event stream was still dead. To prevent this, the repair flow moves the baseline forward to *reload-completion + `repair_settle_s`*: all re-registration timestamps land inside the settle window and do **not** count. Only a real motion/smart-detection event after the settle window proves the stream is alive.

## Repair

Auto-heal reloads the Protect config entry via the HA REST API:

1. Discover the loaded config entry at runtime — `HaAdminClient.list_config_entries(integration_domain)`, keeping only `state == "loaded"` (an ignored UDM discovery entry also exists and must not be reloaded; never hardcode the entry ID). Warns and uses the first if multiple are loaded.
2. Reload it via `HaAdminClient.reload_config_entry()` — REST rather than `call_service` because AppDaemon cancels service calls after ~60s and a Protect reload can take longer.
3. Move the event baseline to reload-completion + `repair_settle_s` (see above).
4. Poll every 30s for up to `repair_recovery_wait_s` for a sensor with `last_changed` newer than the baseline.
5. Report **success** (and unfreeze) on the first genuine event, or **failed** after the timeout — the alert stays firing.

### Repair State Machine

```
idle → pending         (critical, auto-repair enabled; deadline = unhealthy_since + delay,
                        pushed out past the reload cooldown if needed)
pending → idle         (checks recover, OR cancel_repair command received)
pending → in_progress  (deadline reached, executing reload)
in_progress → success  (genuine event within repair_recovery_wait_s)
in_progress → failed   (no genuine event — alert keeps firing)
failed → in_progress   (still critical: retries once the reload cooldown allows)
failed → idle          (checks recover naturally)
```

### Reload Cooldown

Auto-repair reloads at most once per `reload_cooldown_s` (default 1/hour) — pending and failed-retry deadlines are pushed out past the cooldown. **Manual repair from the card bypasses the cooldown** (and the delay): a human clicking "Repair" means now.

### Safety Rules

- Only sustained **critical** triggers auto-repair — **warning** and **unknown** never do
- A repair in progress suppresses auto-repair evaluation entirely
- **success**/**failed** states clear back to **idle** automatically once all checks pass
- Without admin access (`ha_url`/`ha_token_env`), repair immediately reports **failed** instead of pretending

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_boolean.protect_health_auto_repair` | Helper | Toggle auto-repair on/off |
| `input_number.protect_health_auto_repair_delay` | Helper | Minutes before auto-repair triggers (1-60) |

On **first provision only**, the toggle is turned on when `auto_repair_enabled_default` is true and the delay is set to `auto_repair_delay_min_default`. After that the helpers are the source of truth — config defaults are not re-applied.

## Configuration Reference

```yaml
protect_health_checker:
  module: health_checks.checker_apps.protect_health_checker.protect_health_checker
  class: ProtectHealthChecker
  ha_url: !secret ha_url                  # Required for discovery + reload
  ha_token_env: TOKEN                     # Env var NAME holding a long-lived admin token
  checker_id: protect                     # Unique ID (default: protect)
  checker_name: UniFi Protect             # Display name on cards (default: UniFi Protect)
  integration_domain: unifiprotect        # Integration to discover/reload (default: unifiprotect)
  # motion_entities:                      # Optional override — skip discovery, monitor exactly these
  #   - binary_sensor.g4_doorbell_motion
  stale_after_s: 10800                    # Active-hours seconds before declaring a freeze (default: 10800 = 3h)
  active_start: "08:00"                   # Daily active window start, HH:MM (default: 08:00)
  active_end: "23:00"                     # Daily active window end (default: 23:00; must not cross midnight)
  active_tz: America/New_York             # Timezone for the window (default: America/New_York)
  check_interval_s: 300                   # Check frequency (default: 300)
  availability_grace_s: 900               # Unavailable dwell before a sensor counts as down (default: 900 = 15m)
  availability_critical_pct: 90           # % of sensors down for availability critical (default: 90)
  reload_cooldown_s: 3600                 # Min seconds between auto-repair reloads (default: 3600)
  repair_settle_s: 60                     # Post-reload settle window; re-registration timestamps inside it don't count (default: 60)
  repair_recovery_wait_s: 600             # Max seconds to wait for a genuine event after reload (default: 600)
  auto_repair_enabled_default: true       # Toggle state on FIRST provision only (default: true)
  auto_repair_delay_min_default: 1        # Delay helper value on FIRST provision only (default: 1)
  alerting:
    alertname: ProtectEventStreamFrozen   # Default would be UniFiProtectUnhealthy
```

## Commands

The checker listens for relay commands routed by the controller (event `health_check_repair_protect`):

| Command | Payload | Description |
|---------|---------|-------------|
| `start_repair` | `{"checker_id": "protect"}` | Manual config-entry reload — bypasses the reload cooldown |
| `cancel_repair` | `{"checker_id": "protect"}` | Cancel a pending auto-repair (returns to idle) |
| `update_repair_config` | `{"checker_id": "protect", "auto_repair_enabled": true, "auto_repair_delay_min": 5}` | Update the helper-backed auto-repair settings |

## Alerting

The `alerting` block is passed through to the controller at registration and consumed by its Alertmanager bridge (`shared/alertmanager_bridge.py`):

- Checker **critical** → one `ProtectEventStreamFrozen` alert with `severity=critical` — the cluster's Alertmanager routes only critical to Pushover, so a frozen event stream **pages the phone**
- **warning** (e.g. discovery falling back to cache) → `severity=warning`, visible in Alertmanager/Grafana only
- The alert description carries the failing check details plus auto-repair progress (`auto-repair in progress` / `auto-repair FAILED: …`)
- On recovery (a genuine event unfreezes the checker) the bridge posts `endsAt=now` for an immediate `[RESOLVED]` notification
- `alerting.enabled: false` opts the checker out entirely (default: true)

## Dependencies

- `providers/ha_provisioner` — `HAProvisioner` creates the auto-repair helpers on startup; `HaAdminClient` does template-based sensor discovery, config-entry listing, and the reload itself
- `health_check_controller` — registration/status via HA events (never `get_app`); Alertmanager mirroring lives in the controller, not here
