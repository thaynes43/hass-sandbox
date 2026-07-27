# Health Checks

System health monitoring for the Home Assistant dashboard. Provides visibility into AppDaemon backend status, network protocol stack health (Zigbee, Z-Wave), MQTT broker and device health, environmental sensor monitoring, device health (Spa, fans, printers) with optional auto-repair capability, PowerView shade gateway RF-disconnect detection (with auto power-cycle repair), the UniFi Protect camera event stream (with config-entry-reload auto-heal), and the ComfyUI image-generation service. Critical checker failures can page the phone via the cluster's Alertmanager (see [Alertmanager Bridge](#alertmanager-bridge)).

## Architecture

```
┌──────────────────────────┐      HA Events       ┌─────────────────────────┐
│  NetworkProtocolChecker  │ ──────────────────▶   │  HealthCheckController  │
│  (Zigbee instance)       │  register_checker     │                         │
│                          │  report_status        │  Provisions:            │
├──────────────────────────┤                       │  - input_datetime       │
│  NetworkProtocolChecker  │ ──────────────────▶   │    .appdaemon_heartbeat │
│  (Z-Wave instance)       │                       │  - script               │
│                          │                       │    .health_check_relay  │
├──────────────────────────┤                       │                         │
│  MqttBrokerChecker       │ ──────────────────▶   │  Resolves dependencies: │
│                          │                       │  (published view only)  │
├──────────────────────────┤                       │                         │
│  MqttDeviceChecker       │ ──────────────────▶   │  Routes repair cmds:    │
│  (depends: mqtt_broker)  │  + dependencies       │  start_repair           │
├──────────────────────────┤                       │  update_repair_config   │
│  TempHumidityChecker     │ ──────────────────▶   │                         │
│                          │  + dependencies       │                         │
├──────────────────────────┤                       │                         │
│  SpaHealthChecker        │ ──────────────────▶   │  Publishes:             │
│  (supports_repair: true) │  + repair_state       │  - sensor               │
│                          │                       │    .health_check_status │
│  ◀── health_check_repair_spa ──                  │                         │
├──────────────────────────┤                       │                         │
│  ShadeGatewayChecker     │ ──────────────────▶   │  Mirrors to             │
│  (supports_repair: true) │  + alerting            │  Alertmanager:          │
│  ◀── health_check_repair_shade_gateway ──         │                         │
├──────────────────────────┤                       │                         │
│  ProtectHealthChecker    │ ──────────────────▶   │  Mirrors to             │
│  (supports_repair: true) │  + alerting           │  Alertmanager (when     │
│                          │                       │  alertmanager_url set): │
│  ◀── health_check_repair_protect ──              │  critical → page        │
├──────────────────────────┤                       │  warning  → UI only     │
│  ImageGenHealthChecker   │ ──────────────────▶   │  ok       → resolve     │
│  (page-only, no repair)  │  + alerting           │                         │
└──────────────────────────┘                       │                         │
                                                   │                         │
     ◀── health_check_controller_ready ──          │                         │
     ◀── health_check_recheck ───────────          └─────────────────────────┘
                                                              │
                                                              ▼
                                                   ┌─────────────────────────┐
                                                   │  Custom Lovelace Cards  │
                                                   │  health-check-card.js   │
                                                   │  health-check-detail    │
                                                   │    -card.js             │
                                                   └─────────────────────────┘
```

## How It Works

### Event-Based Decoupling

Checker apps communicate with the controller **exclusively via HA events** — never `get_app()`. This allows:

- The controller to run in production Kubernetes while new checkers are developed on a laptop
- Independent restart and lifecycle management
- Easy addition of new checker types without modifying the controller

### Heartbeat Mechanism

The controller updates `input_datetime.appdaemon_heartbeat` every 60 seconds. The custom card computes staleness client-side by comparing the timestamp against `Date.now()`. If stale > 180s, the card shows AppDaemon as offline — **no HA automation or template sensor needed**.

### Generic Protocol Checker

`NetworkProtocolChecker` is a single class instantiated per-protocol via `apps.yaml` config. Each instance performs three checks:

1. **Entity state** — verify an HA entity matches an expected healthy state
2. **Radio ping** — ICMP ping a PoE radio/coordinator hostname
3. **Web UI** — HTTP GET a management web interface URL

Adding a new protocol (e.g. Thread) requires only a new `apps.yaml` entry — no code changes.

### Spa Health Checker

`SpaHealthChecker` monitors a Gecko-integrated hot tub with four checks and optional auto-repair via power cycling. See `spa_health_checker/README.md` for details.

### Fan Health Checker

`FanHealthChecker` monitors all Modern Forms ceiling fans as a single checker (2 checks per fan). Supports per-fan repair via `script.zen32_hard_reset`. See `fan_health_checker/README.md` for details.

### Basic Device Checker

`BasicDeviceChecker` is a generic, config-driven checker for any device needing entity state monitoring and an optional IP ping. No repair support. See `device_checker/README.md` for details.

### Device Group Checker

`DeviceGroupChecker` monitors multiple devices as a single checker. Each device can have one or more entity checks and an optional IP ping. Check names are prefixed with the device name (e.g. "Movie Room Status", "Movie Room Ping"). No repair support.

`RepairableDeviceGroupChecker` extends `DeviceGroupChecker` with per-device repair via smart switch power cycling. Each device can have its own `repair_switch`, or all devices can share a top-level switch. Per-device repair state is tracked and reported via `device_repairs` in the repair_state payload. See `device_group_checker/README.md` for details.

### MQTT Broker Checker

`MqttBrokerChecker` verifies AppDaemon can communicate with the MQTT broker by performing a publish/subscribe round-trip ping test. Publishes a JSON message with a unique nonce to a configurable topic, then listens for the message to come back. Reports **ok** with round-trip latency on success, or **critical** on timeout. See `mqtt_broker_checker/README.md` for details.

### MQTT Device Checker

`MqttDeviceChecker` monitors devices via both HA entity state and MQTT message timestamps. Discovers entities using configurable regex patterns and creates two checks per device: a State check and an MQTT check. Cross-check logic is symmetric: if only one check fails it is downgraded to **warning**; both must fail for **critical**. MQTT checks can declare a dependency on a protocol checker (e.g. Zigbee) so they show as **unknown** when the protocol itself is down. See `mqtt_device_checker/README.md` for details.

### Temp/Humidity Checker

`TempHumidityChecker` monitors environmental sensors with configurable warning and critical thresholds. Supports temperature, humidity, or both sensor types. Each sensor can have per-sensor threshold overrides and can declare a dependency on another checker. See `temp_humidity_checker/README.md` for details.

### Shade Gateway Checker

`ShadeGatewayChecker` owns gateway-wide RF-disconnect detection for all PowerView shade batteries, separate from the plain-threshold `shade_batteries` `BatteryChecker` instance. PowerView G3 shades report `0%` battery when they lose their RF link to the gateway, not only when the battery is genuinely dead — a disconnected gateway can cause every shade on it to flap `100% <-> 0%` many times over several hours. `ShadeGatewayChecker` detects the implausible-drop signature (a healthy reading collapsing straight to ~0%, which a real battery cannot do in one step) via `listen_state` on every shade battery sensor, models disconnects as a single gateway-level episode that survives mid-episode flap-backs to 100% (only sustained flap-free health clears it), and after a grace period auto-repairs by pressing a UniFi PoE power-cycle button on the port feeding the primary gateway — one restart attempt per episode, escalating to a critical page if that doesn't restore the shades. See `shade_gateway_checker/README.md` for full detail, and `battery_checker/README.md` for the cooperating `disconnect_aware` guard on the plain battery checker.

### Protect Health Checker

`ProtectHealthChecker` detects both UniFi Protect failure modes: the silent websocket freeze (sensors stay available but stop changing — zero log errors) and the hard integration outage (UNVR down/auth failure — everything flips `unavailable`). Four checks: **Sensor Discovery** finds all Protect event sensors via the entity registry (`integration_entities` template, with a `motion_entities` config override) and classifies devices into cameras vs USL entry sensors; **Sensor Availability** pages within `availability_grace_s` (15m) when essentially all sensors are unavailable — the fast path a staleness clock can't provide, since the unavailable transition itself refreshes `last_changed` — while warnings fire only for fully-dark devices witnessed alive this app-lifetime within a 24h window (disabled channels and intentionally-unplugged cameras stay quiet); **Camera Events** requires the newest `last_changed` across available camera sensors to be younger than `stale_after_s`, measured in *active-hours* seconds so overnight quiet never accumulates staleness; **Entry Sensors** tracks the USL group's availability without a freshness threshold (quiet doors never page). Once frozen, the check stays critical until a *genuine* event arrives — one strictly newer than the detection-time baseline — so the post-reload re-registration timestamps cannot fake a recovery. Auto-repair reloads the loaded Protect config entry (discovered at runtime via `HaAdminClient`, never hardcoded; max one reload per `reload_cooldown_s`), then waits for a real event past the settle window before declaring success.

### ImageGen Health Checker

`ImageGenHealthChecker` watches the ComfyUI image-generation service by polling `GET /prompt` (`exec_info.queue_remaining`) via `ComfyUIStatusClient`. Two checks: **API Reachable** is a warning while the endpoint is unreachable, escalating to critical after `unreachable_after_s`; **Queue Progress** goes critical when the queue counter stays > 0 without any movement for `queue_stuck_after_s`. A wedged ComfyUI is the canonical symptom of the GPU falling off the PCI bus on the Proxmox host — only a host reboot fixes that — so this checker is **page-only**: no repair support. The 30-minute stuck threshold sits safely above the ~8.5-minute cold-start generation, and ComfyUI's in-memory queue resets to 0 on restart, which simply reads as healthy.

### AC Mains Checker

`AcMainsChecker` watches `binary_sensor.<device>_ac_mains_disconnected` on mains-powered Z-Wave devices with battery backup (Zooz ZAC38 range extenders) so that **loss of wall power pages immediately** rather than surfacing days later as a dead node. When mains drops, a ZAC38 transparently falls back to its internal battery and keeps routing — nothing looks broken until the battery runs flat and the node dies. That is exactly what happened to `shed_extender` on 2026-07-21: a blown breaker went unnoticed for five days, and was only discovered by accident when an unrelated Z-Wave restart flushed a stale battery reading. `on` → `critical` (default, configurable via `disconnected_status`), `off` → ok, and `unavailable` → **unknown** rather than critical, since a vanished node is a Z-Wave connectivity failure owned by the `zwave` checker (declared as a `health_dependency`, so Z-Wave outages mask these checks instead of paging the whole fleet at once). Unlike battery sensors these entities carry no `device_class`, so include/exclude patterns are the only selector — and the excludes are load-bearing, because **battery-only Z-Wave devices expose the same sensor and report `on` permanently** (they have no mains to lose). Three such devices are excluded on this install; see `ac_mains_checker/README.md` for the list and the `node_status` (`alive` = mains, `asleep` = battery) test for classifying new ones.

### Dependency System

Checkers can declare `dependencies` during registration to express that some of their checks depend on another checker being healthy. At publish time, the controller resolves these dependencies: if a dependency checker is unhealthy (`critical`/`degraded`) or missing entirely, the affected checks are overridden to `unknown` with detail `"dependency unavailable"` in the **published view only** -- the internal state is never modified. This prevents misleading alerts when a shared dependency (e.g., the Zigbee protocol stack) is down. A dependency that is merely `unknown` (registered but not reporting) does **not** mask its dependents: an unknown dependency raises no Alertmanager alert of its own, so masking would let a genuine dependent failure go completely silent.

Dependencies are declared per-check via `affects_checks`, or if omitted, all checks in the registering checker are affected:

```json
{
  "dependencies": [
    {
      "checker_id": "zigbee",
      "affects_checks": ["Device1 MQTT", "Device2 MQTT"]
    }
  ]
}
```

The checker-level status is recomputed from the modified checks in the published view using the standard severity precedence: critical > degraded > warning > unknown > ok.

### Repair Feature

Checkers can declare `supports_repair: true` during registration. The controller routes repair commands to the specific checker without knowing how to repair — all repair logic lives in the checker app. The detail card shows repair controls (manual button, auto-repair toggle, delay config) for repair-capable checkers.

### Alertmanager Bridge

When `alertmanager_url` is configured on the controller, checker health is mirrored into the cluster's Prometheus Alertmanager — one alert per unhealthy checker. The decision logic lives in `shared/alertmanager_bridge.py` (pure, no HTTP); `providers/alertmanager` does the actual `POST /api/v2/alerts`.

Severity mapping (the cluster's Alertmanager routes only `severity=critical` to Pushover):

| Checker status | Alert severity | Effect |
|----------------|----------------|--------|
| `critical` | `critical` | Pages the phone |
| `warning` / `degraded` | `warning` | Visible in Alertmanager/Grafana UI only |
| `ok` / `unknown` | — | Alert resolved (`unknown` is typically a dependency outage already alerted by the dependency's own checker) |

Mechanics:

- **For-duration gate (flap suppression)** — a checker that is non-ok for only a poll or two should not page. Each severity has a configurable `for` duration (`alert_for_seconds` on the controller, with per-checker `alert_for_overrides`). When a checker first goes non-ok the alert is held *pending* — nothing is posted — and only promoted to firing once it has stayed non-ok for `>= for` seconds, confirmed on a **later** status report (Prometheus `for:` semantics: a single-sample blip can never be promoted). If the checker recovers while pending, the alert is dropped silently (no page, no `[RESOLVED]` noise). `for=0` (the default when unconfigured) raises immediately. Most checkers poll every 300s, so `critical: 300` ≈ "two consecutive failing checks": a sustained 0%/offline still pages, a one-poll glitch does not.
- **Escalation gate** — a *severity escalation* of an already-firing alert (warning → critical) goes through the same for-duration gate instead of paging immediately: the firing warning stays up while the critical escalation is held *pending*, and only after the checker has stayed critical for `>= for(critical)` seconds is the warning resolved and the critical raised. If the checker de-escalates — or falls back to the severity already firing — while the escalation is pending, the pending escalation is dropped silently and the warning keeps firing, so a warning↔critical flapper can never page. De-escalations (critical → warning) apply immediately, as does everything when `for=0`.
- **Repair hold** — while a repair-capable checker's auto-repair is scheduled or running (`repair_state.status` of `pending`/`in_progress`), a *due* critical promotion is withheld so auto-repair gets a chance to fix the problem before anyone is paged — up to `alert_repair_hold_cap_s` total pending time (default 1800s) so a stuck repair can never permanently silence a real outage. A failed repair releases the hold on the next report. Checkers with `for=0` (explicit "page now" overrides) never enter the pending gate and are therefore never held.
- **Label-set identity** — `alertname` + `severity` + `source=appdaemon-health-check` + `checker=<id>` identify the alert. If the labels change, the old alert is resolved and a new one raised; if only the failing-check details change, annotations are refreshed in place. A severity change that *escalates* (warning → critical) is held by the escalation gate above first; de-escalations swap immediately.
- **Re-post keep-alive** — Alertmanager auto-resolves silent alerts after its `resolve_timeout` (5m in this cluster), so the controller re-posts all firing alerts every `alertmanager_repost_interval_s` (default 120s — must stay below the resolve_timeout).
- **Immediate resolve** — on recovery the bridge sends one final post with `endsAt=now`, producing an immediate `[RESOLVED]` notification instead of waiting out the resolve_timeout.
- **Fail-open** — Alertmanager being down never breaks health checking: failed posts are logged and retried by the next sync or re-post tick. An unsent raise stays in the active set so the re-post loop delivers it; an unsent resolve falls back to the resolve_timeout.
- **Repair context** — the alert description includes auto-repair progress ("auto-repair in progress" / "auto-repair FAILED: ...") alongside the failing checks.

Checkers opt in/out and name their alert via an `alerting` block in the registration payload (forwarded from their `apps.yaml` config):

```yaml
alerting:
  enabled: true                        # default true
  alertname: ProtectEventStreamFrozen  # default <CheckerName>Unhealthy (e.g. ImageGenUnhealthy)
```

### Per-Checker Mute

Any checker's paging can be silenced on demand from the detail card — useful during planned maintenance or a known-but-unactionable outage. The detail card shows an **Alerting** row for every checker with **Mute 1d**, **Mute 7d**, and **Mute** (indefinite) buttons, an **Unmute** button while muted, and a **MUTED** chip in the checker's header. These fire the `mute_checker` / `unmute_checker` relay commands.

A muted checker still runs its checks and reports status to the sensor — the card renders it normally, just with the MUTED chip — but its Alertmanager alert is suppressed: `_publish_status` passes the checker to the bridge with `alerting.enabled=false`, so the bridge resolves any firing alert and drops any pending one. Nothing pages while muted. Unmuting restores alerting; if the checker is still unhealthy the alert re-raises (back through the for-duration gate) on the next report.

- **Timed vs indefinite** — `mute_checker` takes an optional `duration_s`; absent means mute indefinitely. Timed mutes are lifted automatically by the heartbeat tick once their expiry passes.
- **Persistence** — mute state is stored per checker in a lazily-provisioned `input_text.health_check_mute_<checker_id>` helper (JSON `{"muted", "until"}`), so it survives an AppDaemon restart and is restored when the checker re-registers. An expired mute found on restart is treated as unmuted.
- **Audit trail** — mute and unmute are recorded in the checker's `alert_history` as `Alerting` events, and each checker's sensor attributes expose `muted` (bool) and `muted_until` (ISO timestamp or `null`).

### Prometheus Metrics

The controller exposes Prometheus metrics on a dedicated port (default `9100`, `/metrics`), scraped by a `ServiceMonitor` in the cluster. Because every checker reports through the controller, **base metrics are emitted for all checkers with zero per-checker code** — the controller mirrors its resolved (dependency- and mute-aware) snapshot into gauges on every `_publish_status()`. The exporter lives in `providers/metrics` and runs its exposition server in a daemon thread, isolated from AppDaemon's asyncio loop; it degrades to a no-op if `prometheus-client` is missing or `metrics_enabled: false`.

**Base metrics** (all prefixed `appdaemon_health_`):

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `checker_status` | gauge | `checker_id` | Aggregate status as a severity int (`ok=0, warning=1, degraded=2, critical=3, unknown=-1`) |
| `check_status` | gauge | `checker_id, check` | Per-check status (same severity encoding) |
| `checks` | gauge | `checker_id, kind` | Check counts (`kind=total\|ok\|non_ok`) |
| `checker_last_report_timestamp_seconds` | gauge | `checker_id` | Unix time of last report (freshness) |
| `check_state_entered_timestamp_seconds` | gauge | `checker_id, check` | When the check entered its current state (time-in-state) |
| `checker_supports_repair` / `checker_auto_repair_enabled` / `checker_muted` | gauge | `checker_id` | Repair capability / auto-repair toggle / mute flags |
| `alerts_firing` / `alerts_pending` | gauge | `severity` | Firing / for-gated pending Alertmanager alerts by severity |
| `controller_up` | gauge | — | 1 while the controller runs |
| `repairs_total` | counter | `checker_id, device, result` | Repair completions (`result=success\|failed`) |
| `repair_recovery_duration_seconds` | histogram | `checker_id, result` | Recovery time from repair start to recovery |

**Checker-emitted metrics (opt-in protocol).** A checker adds richness by including two optional fields on its `report_status` payload; the controller (`_ingest_reported_metrics`) forwards them to the exporter — no controller changes needed for new metrics:

- `repair_events`: `[{ "result": "success"|"failed", "duration_s"?: float, "device"?: str }]` — a one-shot edge event emitted when a repair concludes (feeds `repairs_total` + the recovery histogram). Repair-capable checkers buffer these in `self._pending_repair_events` and drain them into the next report exactly once.
- `metrics`: `[{ "name": snake_case, "value": number, "type"?: "gauge"|"counter"|"histogram", "labels"?: {...} }]` — arbitrary domain values, exposed as `appdaemon_health_custom_<name>` with a `checker_id` label plus any supplied labels. Current emitters: `temp_humidity` (`temperature_fahrenheit`, `humidity_percent`), `battery`/`ups` (`battery_percent`, …), `imagegen` (`queue_remaining`).

Keep custom names unit-suffixed and labels low, stable cardinality (never timestamps / free-text / unbounded ids).

## Dependencies

- `providers/ha_provisioner` — creates HA helpers and scripts on startup; `HaAdminClient` gives ProtectHealthChecker entity discovery and config-entry reload
- `providers/alertmanager` — posts/resolves alerts in the cluster Alertmanager (controller, when `alertmanager_url` is set)
- `providers/metrics` — Prometheus exporter; exposition server + base gauges + repair/custom metric ingest (controller)
- `providers/ai_providers/comfyui` — `ComfyUIStatusClient` queue polling (ImageGenHealthChecker)
- `aiohttp` — HTTP health checks (in `shared/check_utils.py`)
- `prometheus-client` — metrics exposition (controller)

## Self-Provisioned Entities

| Entity | Type | Purpose |
|--------|------|---------|
| `input_datetime.appdaemon_heartbeat` | Helper | Controller heartbeat timestamp |
| `script.health_check_relay` | Script | Card → AppDaemon command relay |
| `sensor.health_check_status` | Virtual sensor | Aggregated health status (via `set_state`) |
| `input_text.health_check_mute_<checker_id>` | Helper | Per-checker mute state as JSON (lazily provisioned on first mute) |
| `input_boolean.spa_health_auto_repair` | Helper | Auto-repair toggle (provisioned by SpaHealthChecker) |
| `input_number.spa_health_auto_repair_delay` | Helper | Auto-repair delay in minutes (provisioned by SpaHealthChecker) |
| `input_boolean.protect_health_auto_repair` | Helper | Auto-repair toggle (provisioned by ProtectHealthChecker) |
| `input_number.protect_health_auto_repair_delay` | Helper | Auto-repair delay in minutes (provisioned by ProtectHealthChecker) |
| `input_boolean.shade_gateway_health_auto_repair` | Helper | Auto-repair toggle (provisioned by ShadeGatewayChecker, default ON) |
| `input_number.shade_gateway_health_auto_repair_delay` | Helper | Auto-repair grace period in minutes (provisioned by ShadeGatewayChecker, 15-360, default 120) |

## Associated Cards

| Card | File | Purpose |
|------|------|---------|
| `health-check-card` | `cards/health-check-card.js` | Compact summary bar for wall-display |
| `health-check-detail-card` | `cards/health-check-detail-card.js` | Full detail popup with alert history, repair controls, and per-checker mute (Alerting row) |

## Configuration Reference

### HealthCheckController

```yaml
health_check_controller:
  module: health_checks.controller.health_check_controller
  class: HealthCheckController
  ha_url: !secret ha_url
  ha_token_env: TOKEN
  heartbeat_interval_s: 60       # Heartbeat update frequency (seconds)
  alert_history_max: 50          # Max alerts retained per checker
  alert_retention: "1:12:00:00"  # TTL for alert history entries (DD:HH:MM:SS or HH:MM:SS)
  metrics_enabled: true          # Expose Prometheus metrics (default true)
  metrics_port: 9100             # /metrics exposition port (scraped by ServiceMonitor)
  # Optional Alertmanager mirroring — omit alertmanager_url to disable
  alertmanager_url: http://kube-prometheus-stack-alertmanager.observability.svc.cluster.local:9093
  alertmanager_repost_interval_s: 120  # Firing-alert re-post period; must stay < Alertmanager resolve_timeout (5m)
  # For-duration gate (flap suppression) — omit for raise-immediately (for=0).
  alert_for_seconds:           # how long a checker must stay non-ok before paging, by severity
    critical: 300              # ~5 min; ≈ two consecutive failing checks at the default 300s poll
    warning: 600               # warnings are UI-only; quieter still
  alert_for_overrides:         # per-checker override: checker_id -> severity -> seconds (0 = page now)
    ups:
      critical: 0              # power loss is time-sensitive — never debounce
      warning: 0
  # Repair hold — a scheduled/running auto-repair withholds a due critical page
  # (up to this many total pending seconds) so it can fix the problem first.
  alert_repair_hold_cap_s: 1800  # default 1800s (30 min); 0 disables the hold
```

### NetworkProtocolChecker

```yaml
zigbee_health_checker:
  module: health_checks.checker_apps.network_protocol_checker.network_protocol_checker
  class: NetworkProtocolChecker
  checker_id: zigbee                                          # Unique ID
  checker_name: Zigbee                                        # Display name
  entity_id: binary_sensor.zigbee2mqtt_bridge_connection_state  # HA entity to monitor
  entity_healthy_state: "on"                                  # Expected healthy state
  entity_check_name: Bridge Connection                        # Check display name
  radio_host: tubeszb-zigbee01.haynesnetwork                  # Hostname to ping
  radio_check_name: Coordinator Ping                          # Check display name
  web_ui_url: https://zigbee.haynesops.com                    # URL to GET
  web_ui_check_name: Web UI                                   # Check display name
  check_interval_s: 180                                       # Check frequency (seconds)
```

Any check can be disabled by omitting its config key (e.g., remove `radio_host` to skip the ping check).

## Relay Commands

| Command | Payload | Description |
|---------|---------|-------------|
| `force_recheck` | `{}` | Triggers all checkers to run immediately |
| `start_repair` | `{"checker_id": "spa"}` | Trigger manual repair for a specific checker |
| `update_repair_config` | `{"checker_id": "spa", "auto_repair_enabled": true, "auto_repair_delay_min": 5}` | Update auto-repair settings |
| `clear_alert_history` | `{"checker_id": "optional"}` | Clear alert history for one or all checkers |
| `mute_checker` | `{"checker_id": "spa", "duration_s": 86400}` | Suppress a checker's Alertmanager paging; omit `duration_s` to mute indefinitely |
| `unmute_checker` | `{"checker_id": "spa"}` | Re-enable a checker's paging |
| `record_note` | `{"checker_id": "spa", "note": "power-cycled gateway", "source": "shepherd"}` | Insert a triage note into the checker's alert history (audit trail for automation; `source` defaults to `agent`) |

## Sensor Attributes Schema

`sensor.health_check_status` attributes:

```json
{
  "checkers": {
    "<checker_id>": {
      "name": "Zigbee",
      "status": "ok|warning|degraded|critical|unknown",
      "last_check": "2026-03-19T20:00:00",
      "checks": [
        {
          "name": "Bridge Connection",
          "status": "ok",
          "detail": "on",
          "last_changed": "2026-03-19T20:00:00"
        }
      ],
      "checks_summary": {
        "total": 3,
        "ok": 3,
        "non_ok": 0
      },
      "alert_history": [
        {
          "timestamp": "2026-03-19T19:55:00",
          "check": "Bridge Connection",
          "from_status": "ok",
          "to_status": "critical",
          "detail": "Expected 'on', got 'off'",
          "previous_state_entered": "2026-03-19T18:30:00",
          "previous_state_duration_s": 5100.0
        }
      ],
      "supports_repair": false,
      "repair_state": null,
      "muted": false,
      "muted_until": null
    }
  },
  "last_updated": "2026-03-19T20:00:00",
  "friendly_name": "Health Check Status",
  "icon": "mdi:heart-pulse"
}
```

**Note:** For checkers with more than 20 checks, only non-ok checks are included in the `checks` array to stay within HA's WebSocket attribute size limit. The `checks_summary` object always contains the full counts.

## Manual Setup Required

1. **Lovelace resources** — register the JS card files:
   - `/local/health-checks/health-check-card.js`
   - `/local/health-checks/health-check-detail-card.js`
2. **Copy card JS** to `/config/www/health-checks/` on the HA instance
3. **Add cards** to the Wall-Display dashboard (see `home-assistant/cards/wall-display/`)

## Folder Structure

```
health_checks/
├── controller/
│   ├── __init__.py
│   └── health_check_controller.py
├── checker_apps/
│   ├── __init__.py
│   ├── ac_mains_checker/
│   │   ├── __init__.py
│   │   ├── ac_mains_checker.py
│   │   └── README.md
│   ├── network_protocol_checker/
│   │   ├── __init__.py
│   │   ├── network_protocol_checker.py
│   │   └── README.md
│   ├── mqtt_broker_checker/
│   │   ├── __init__.py
│   │   ├── mqtt_broker_checker.py
│   │   └── README.md
│   ├── mqtt_device_checker/
│   │   ├── __init__.py
│   │   ├── mqtt_device_checker.py
│   │   └── README.md
│   ├── temp_humidity_checker/
│   │   ├── __init__.py
│   │   ├── temp_humidity_checker.py
│   │   └── README.md
│   ├── spa_health_checker/
│   │   ├── __init__.py
│   │   ├── spa_health_checker.py
│   │   └── README.md
│   ├── fan_health_checker/
│   │   ├── __init__.py
│   │   ├── fan_health_checker.py
│   │   └── README.md
│   ├── device_checker/
│   │   ├── __init__.py
│   │   ├── device_checker.py
│   │   ├── repairable_device_checker.py
│   │   └── README.md
│   ├── device_group_checker/
│   │   ├── __init__.py
│   │   ├── device_group_checker.py
│   │   ├── repairable_device_group_checker.py
│   │   └── README.md
│   ├── protect_health_checker/
│   │   ├── __init__.py
│   │   └── protect_health_checker.py
│   ├── shade_gateway_checker/
│   │   ├── __init__.py
│   │   ├── shade_gateway_checker.py
│   │   └── README.md
│   └── imagegen_health_checker/
│       ├── __init__.py
│       └── imagegen_health_checker.py
├── shared/
│   ├── __init__.py
│   ├── check_utils.py
│   └── alertmanager_bridge.py
├── cards/
│   ├── health-check-card.js
│   └── health-check-detail-card.js
└── README.md
```
