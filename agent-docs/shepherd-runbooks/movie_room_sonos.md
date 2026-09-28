# Runbook: `movie_room_sonos` — Movie Room Sonos Port

A `RepairableDeviceChecker` instance for the Sonos Port that feeds the Movie
Room receiver. One check, `Ping`, against `movieroomsonos.haynesnetwork`, 3
pings per cycle, ok on the first reply; `ping_fallback_host: 192.168.0.70` (its
DHCP reservation) is pinged instead only when the name does not resolve.
`check_interval_s: 180`. `supports_repair: yes` — power-cycles UniFi USP PDU
Hi-Density outlet 21, `switch.power_distribution_hi_density_outlet_21`.
**Auto-repair defaults ON**, 10 min dwell. No dependencies, no for-override
(standard 300 s critical gate, plus the repair hold while a repair is pending or
running).

The PDU outlet name is Tom's label, **"Sonos Port"**; in Home Assistant it is
only `…_outlet_21`.

## Domain facts (read these first)

- **The wedge.** On 2026-09-22 a network-switch blip left the Port with its
  cable link up but answering nothing — no ARP, no ping, no TCP 1400. Music
  Assistant dropped it and `media_player.movie_room` stayed unavailable for six
  days. A 12 s power cycle of outlet 21 fixed it. That is the fault this
  checker exists for.
- **Ping is the only signal, on purpose.** `media_player.movie_room` is not a
  check: it also goes unavailable on every Music Assistant restart, and that
  must never power-cycle the Port. The outlet's power sensor reads 0 W at idle,
  so power draw is no health signal either.
- **The PDU is slow and occasionally drops its entities.** Toggling any outlet
  re-provisions the PDU for about 40 s; HA took more than 10 s to report the
  outlet back `on`; and once, around a re-provision, the unifi integration
  dropped **all** of the PDU's outlet entities until the integration was
  reloaded. That is why the repair confirms the outlet came back on.
- **A DNS failure never power-cycles a healthy Port, and never hides a dead
  one.** When `movieroomsonos.haynesnetwork` does not resolve, `ping` exits
  with "bad address" and `ping_check` reports **critical**,
  `cannot resolve movieroomsonos.haynesnetwork (3 attempts)` (since v1.24.0;
  before that the detail said `timeout`). On its own that would page and
  power-cycle a healthy Port, so `ping_fallback_host` turns it into the
  fallback's result — the checker pings 192.168.0.70:
  - Port answers → `Ping` is **warning**,
    `4ms via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork`. The
    Port is fine and DNS is not. Warning never pages and never arms a repair;
    the AppDaemon log has one WARNING
    `movieroomsonos.haynesnetwork does not resolve — pinging 192.168.0.70 instead until it does`
    (and an INFO when it resolves again).
  - Port silent → `Ping` is **critical**,
    `timeout (3 attempts) via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork`,
    and auto-repair and paging run as normal.

## What auto-repair does

1. First critical cycle → `repair_state.status = pending`, deadline 10 min out
   (`input_number.movie_room_sonos_health_auto_repair_delay`). The controller
   withholds the critical page while it is pending or in progress (cap 1800 s).
2. Still critical at the deadline → `in_progress`: outlet 21 off, 10 s, on.
3. The outlet must report `on` within 60 s; if not, one more `turn_on` and
   another 60 s. Still not on → `failed`, detail
   `switch.power_distribution_hi_density_outlet_21 did not turn back on — check the outlet`
   (or `… — the entity is missing from Home Assistant — reload the integration that owns it`
   when the unifi integration has dropped the PDU's outlet entities), and no recovery wait.
4. Outlet confirmed on → up to 300 s of recovery polling (the Port takes a
   while to boot and rejoin). Ping back → `success`; otherwise `failed`,
   `Did not recover after 300s`. Ping lost again (critical) after a
   `success`, before a fully healthy cycle → `failed`,
   `Relapsed after a successful repair — recovery did not stick`; the DNS
   fallback's `warning` (Port up, name broken) does not count as a relapse,
   and counts as recovered during the recovery wait. If the outlet
   read `on` without ever reporting `off` (HA may have missed it, or the
   `turn_off` was lost), a failure detail ends
   `(the outlet never reported off — it may not have been power cycled)`.
5. One repair per outage: after `failed` it stays `failed` (no automatic
   retry) until a fully healthy cycle. `failed` releases the page.

## Symptoms

- Alert `checker=movie_room_sonos` (default alertname
  `MovieRoomSonosUnhealthy`), severity critical.
- `Ping` critical, detail `timeout (3 attempts)` — or, during a DNS outage,
  `timeout (3 attempts) via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork`.
- `media_player.movie_room` unavailable; the Movie Room is missing from Music
  Assistant's players.

## Diagnosis

1. Read `checkers.movie_room_sonos` from `sensor.health_check_status`:
   `checks[0].detail` and `repair_state` (`status`, `detail`,
   `auto_repair_enabled`, `auto_repair_deadline`, `last_repair_attempt`).
   - `pending` / `in_progress` → auto-repair has it; **wait** (Verify below).
   - `failed` + `did not turn back on — check the outlet` → the outlet, not the
     Port: go to step 3.
   - `failed` + `… the entity is missing from Home Assistant — reload the integration
     that owns it` → the unifi integration dropped the PDU's outlet entities (seen
     2026-09-28): `homeassistant.reload_config_entry` on the unifi entry, then check
     outlet 21 is `on`.
   - `failed` + `… the switch could not be read — see the AppDaemon log` → AppDaemon's
     `get_state` raised (most likely its Home Assistant connection dropped, which also
     drops the turn_off/turn_on): read the `Could not read …` WARNINGs in the
     AppDaemon log, check outlet 21 is `on`, and do not reload UniFi for this.
   - `failed` + `Did not recover after 300s` → the outlet came back on and the
     Port still does not answer.
   - `failed` + `Relapsed after a successful repair` → the power cycle worked
     briefly and the Port wedged again; treat it like `Did not recover`.
   - `idle` while critical → auto-repair is off
     (`input_boolean.movie_room_sonos_health_auto_repair`) or the dwell has not
     run out.
2. Rule out the network and DNS before blaming the Port. A detail ending
   `via 192.168.0.70 — cannot resolve movieroomsonos.haynesnetwork` means DNS
   is down as well: the verdict is the fallback IP's (a timeout there is a real
   Port outage; a warning means the Port is fine — fix DNS, not the Port). Are
   the other `*.haynesnetwork` pings (`zigbee` Coordinator Ping, `zwave` Radio
   Ping) also failing — critical with `cannot resolve …` means DNS, `timeout`
   means the network? Then triage that, not the Port.
3. Read `switch.power_distribution_hi_density_outlet_21`:
   - `on` → powered.
   - `off` → the outlet was left off (a repair's `turn_on` was lost, or a human
     turned it off).
   - `unavailable` or missing → the unifi integration has dropped the PDU's
     outlet entities.
4. Loki: `{namespace="home-automation", app="appdaemon"} |~ "Movie Room Sonos|outlet_21"`
   last 1h — the ping results, `Turning off/on …outlet_21`, `confirmed on`, and
   any `turning it on again` WARNING or `still not on` ERROR.

## Remediation ladder

1. `record_note` the triage start:
   `{"checker_id":"movie_room_sonos","note":"triage start: <ping detail>, repair <status>","source":"shepherd"}`.
2. `repair_state.status` `pending` / `in_progress` → do nothing but wait.
3. `idle` and still critical: `start_repair` `{"checker_id":"movie_room_sonos"}`.
   It runs the same off / 10 s / on + confirm / 300 s sequence. Do **not**
   toggle the outlet directly — only `start_repair` confirms the outlet came
   back on.
4. `failed` → **Escalate**. Never more than 2 `start_repair` attempts / 6h, and
   never after a `did not turn back on` failure: another power cycle of a PDU
   that is not answering can leave the Port off.

## Verify

- After `start_repair`: 10 s off + up to 120 s of turn-on confirmation + up to
  300 s recovery + one `check_interval_s` (180 s) ≈ **10 min**.
- Recovery = `Ping` ok and `repair_state.status == success`; the bridge
  resolves the page itself.

## Escalate

Let the page through with a `record_note` summary: the ping detail, the
repair_state status and detail, what was tried, the outlet's state, and the
Loki query. The fix is a human's (the Shepherd's guardrails forbid these):

- **Outlet off, or `did not turn back on — check the outlet`** → check outlet
  21 ("Sonos Port") on the PDU in the UniFi app and turn it on there.
- **The PDU's outlet entities are missing or unavailable in HA** → reload the
  UniFi integration (Settings → Devices & services → UniFi Network → Reload).
  They come back after the reload.
- **The outlet is on and the Port still does not ping** after a power cycle →
  check its cable/switch port in the UniFi app (link up with no traffic is the
  2026-09-22 wedge again); physical attention may be needed.
- **The Port pings but `media_player.movie_room` is still unavailable** → not
  this checker's fault (it reads healthy): reload the Sonos provider in Music
  Assistant (Settings → Providers → Sonos → Reload).
