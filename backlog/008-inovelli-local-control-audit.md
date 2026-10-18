# Inovelli mmWave local-control audit

Status: In progress (2026-10-18).

Saga: Home Assistant reliable room control (see
[007](007-movie-room-mmwave-owner-validation.md)). Epic: occupancy lighting that HA
owns end to end, so no switch cuts its load on its own radar alone.

## Why

On 2026-10-18 the garage lights went off with Tom standing in the room. The mudroom
Inovelli switch had firmware local control on (`MmwaveControlWiredDevice` =
"Occupancy (default)"). In that mode the switch cuts its own load when its own radar
clears, ignoring every other sensor and automation. Its helper
`input_select.garage_interior_mudroom_mmwave_normal_mode` had
`initial: "Occupancy (default)"`, which reset it on each HA restart. It is now
`initial: Disabled` (#263). Tom asked for an audit of every Inovelli presence switch
against his automations and the switches' current and default occupancy settings.

## Scope

One outcome: every Inovelli presence switch is classified, and the clear-cut conflicts
are fixed live.

- Included: all `select.*_mmwavecontrolwireddevice` entities and their
  `input_select.<zone>_mmwave_normal_mode` helpers (state and `initial`).
- Excluded: editing automations or scripts, detection areas, sensitivity, hold times,
  off-delay helpers, and Z2M settings other than the mode select. No restarts, reloads,
  light toggling or physical tests.

Classes:
- **A, conflict:** HA owns both on and vacancy-off, but local control is not Disabled
  (device, helper or `initial`). Fix.
- **B, latent:** the device is Disabled but the helper state or `initial` would push a
  non-Disabled mode on the next restart or clear-hold. Fix.
- **C, local-only:** no HA vacancy-off path; the switch's own mode turns the lights off.
  Leave it.
- **D, mixed or unclear:** leave it and record the trade-off with a recommendation.

## Acceptance

- Results table below covers every switch: load, HA on/off automations, device / helper
  / `initial` (before and after), class, action.
- Each A and B fix: helper `initial` = Disabled, helper state = Disabled, device select
  reads Disabled (live read-back). Prior values recorded for rollback.
- D-class items listed with a recommendation for Tom.
- PR merged with the evidence; agent-docs/playbooks updated where they track per-zone
  modes.

## Dependencies

None. The garage fix (#263) is already merged and live.

## Ownership

One Claude Code subagent session (Opus 5.5), worktree
`~/work/inovelli-local-control-audit`, branch `agent/inovelli-local-control-audit`.
Runtime writes limited to the `input_select.*_mmwave_normal_mode` helpers (config +
state) and, through the existing sync automation, the matching device selects.

## Budget and stop criteria

About 90 minutes, no subagents, no CPU-heavy loops. Stop when acceptance is met, at the
budget, or if a fix needs new plumbing (record it here instead). Unfinished work goes
under *Next step* below.

## Rollback

Set each changed helper back to its recorded prior `initial` and state with
`ha_config_set_helper` + `input_select.select_option`; the sync automation pushes it to
the device.

## Results

(filled in below)
