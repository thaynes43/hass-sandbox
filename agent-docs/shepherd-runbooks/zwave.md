# Runbook: `zwave` — Z-Wave Network Protocol Checker

Monitors the Z-Wave stack. Checks: `Integration Status`
(`sensor.800_series_long_range_gpio_module_status`, healthy = `ready`),
`Radio Ping` (`tubeszb-zwave01.haynesnetwork`), and `Web UI`
(`https://zwave.haynesops.com`). `check_interval_s: 180`.
`supports_repair: yes` — ESPHome **software** restart of the TubesZB board,
auto-repair defaults **on**. Root dependency (`zwave_batteries` depends on it).

> 🚫 **HARD STOP — never power-cycle this board.** Not through UniFi PoE port
> cycling, not through any switch or plug, not "just once". The previous
> TubesZB dongle was destroyed by repeated PoE restarts. The **only**
> sanctioned action is `button.restart_the_esp32_device_2` (the ESPHome
> software restart), and you should reach it through `start_repair` rather
> than pressing it directly, so the rate limits and recovery verification
> apply.

## Why this checker pages at all

Z-Wave reaches its radio over TCP (`zwave-js-ui` →
`tcp://tubeszb-zwave01:6638`), and that ESP32 stream server accepts one client
and never notices when it dies. The signature outage is therefore a **partial**
failure: `Integration Status` red while `Radio Ping` and `Web UI` stay green.
Normally `apply_cross_check` downgrades that to `warning` — which is why the
2026-09-09 outage ran 4h20m silently. The checker forces `critical` itself in
exactly two cases, and both mean auto-repair is done and a human is needed:

- **the 24h restart budget is spent** — `repair_attempts_24h >= repair_max_per_24h`
  (3). Detail reads `auto-repair cap reached (…), manual action needed`.
- **the radio is unreachable** — `Radio Ping` red too. Detail reads
  `radio unreachable, board is offline — manual action needed`. A software
  restart cannot reach an offline board, so the checker deliberately does not
  attempt one.

So if you are looking at a `zwave` critical, auto-repair has **already given
up** or was never applicable. Do not expect to fix it with another restart.

## Symptoms

- Alert `checker=zwave`, severity critical.
- Every Z-Wave entity in HA reads `unavailable` — locks, Z-Wave motion
  sensors, and the door/motion automations that depend on them do nothing.
- `zwave-js-ui` logs `Failed to open the serial port` on a ~26s loop.

## Diagnosis

1. Read `checkers.zwave.checks[]`:
   - `Integration Status` red, `Radio Ping` **green** → the stale-client wedge.
     The board is alive and holding a dead TCP session.
   - `Radio Ping` red as well → the board is off the network. This is a
     power/Wi-Fi/switch-port problem, not a serial one.
2. Read `checkers.zwave.repair_state`: `repair_attempts_24h` vs
   `repair_max_per_24h` tells you whether the budget is spent, and
   `last_repair_attempt` when it was last tried. `status: failed` with a
   `Cap reached:` detail means auto-repair has stopped.
3. `binary_sensor.tubeszb_zw_serial_connected_2` — `on` while the integration
   is down is the stale-client fingerprint. After several failed restarts it
   may read `off`; that does **not** mean the outage got smaller.
4. Loki: `{namespace="home-automation", app="appdaemon"} |= "Z-Wave"` last 1h
   for the repair ladder, and
   `{namespace="home-automation", app="zwave"}` for the serial-port errors.

## Remediation ladder

1. `record_note` — start the audit trail:
   `{"checker_id":"zwave","note":"triage start: <which check failed>","source":"shepherd"}`.
2. `force_recheck` (payload `{}`) — confirm the fault is still live. Note the
   global caveat in the README: this evaluates auto-repair on **every**
   checker, not just this one.
3. **If `Radio Ping` is red → stop and page a human.** The board is off the
   network; nothing in the sanctioned action set can help. Do not attempt a
   restart, do not touch the PoE port. A human needs to look at the physical
   device or its switch port.
4. **If the budget is spent (`repair_attempts_24h >= 3`) → stop and page a
   human.** Three software restarts have already failed. A fourth is refused
   by the rate limits, and forcing one is exactly the repeated-restart pattern
   that killed the previous board. Escalate.
5. Only if the budget is **not** spent and `repair_state.status` is `idle`
   (which usually means auto-repair was switched off, since it defaults on):
   `start_repair` `{"checker_id":"zwave"}`. This presses the ESPHome software
   restart and polls `Integration Status` for recovery for up to
   `repair_recovery_wait_s` (300s). It skips the dwell but **not** the rate
   limits — a refusal will come back as `Cap reached` / `Rate limited`.
6. If `repair_state.status` is `pending` or `in_progress`, **wait** — do not
   stack a manual repair on top. Recovery is normally ~17s after the press.

## What "fixed" looks like

`Integration Status` back to `ready` and Z-Wave entities available again. A
successful repair reports `repair_state.status: success` for one cycle before
returning to `idle`. Note that restarting the `zwave` **pod** does not fix the
stale-client wedge — our side of the old socket sits in `FIN_WAIT2` and the
board never acks the close — so a rollout restart is not a remediation step
here.
