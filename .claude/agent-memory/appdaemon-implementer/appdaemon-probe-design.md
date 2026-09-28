---
name: appdaemon-probe-design
description: A boolean probe collapses self-healing and never-self-healing failures into one indistinguishable log line — preserve what the other side actually said
metadata:
  type: reference
---

# A yes/no health probe hides the non-self-healing half of its failure modes

Found in review 2026-09-18: `local_file_exists` collapsed 404 / 301 / 401 /
timeout / connection-error into `False`, so the give-up WARNING read
identically for "the file has not landed yet" (transient, the next poll fixes
it) and "the configured URL is wrong / the service is down" (never self-heals,
feature silently frozen forever).

**Shape of the fix:** `*_status(...) -> int` returning the real HTTP status plus
a negative sentinel (`STATUS_UNREACHABLE = -1`) for "no answer at all"; keep the
boolean as a thin `== 200` wrapper so existing callers and tests stay valid.
Callers remember the last status for the in-flight job and branch the operator
hint on it — and say explicitly which branches will NOT self-heal and which log
will not explain them.

**General rule:** any probe whose result reaches a human must preserve *what the
other side said*, not just whether it was what we wanted.

The same rule applies to a status sensor: distinguish "I could not look"
(publish `unknown` counts) from "I looked and could not fix it" (publish the
real counts plus the error). `apps/assist_exposure_guard/_publish_status` is the
worked example.

## Same trap in `ping_check` (not fixed, 2026-09-28)

The AppDaemon image's busybox `ping` exits 1 with "bad address" for an
unresolvable name, and `check_utils._ping_once` maps every non-zero exit to
detail `"timeout"`. So a DNS failure on a `*.haynesnetwork` `ping_host` reads
exactly like a dead device — and on a repairable checker it arms a power cycle.
Runbooks for FQDN-pinged checkers must say "compare the other `.haynesnetwork`
pings first".

## Confirming a switch after a service call needs `last_changed`, not just state

After `switch/turn_on` (fire-and-forget), an awaited `get_state` seconds later
can still return the `on` from *before* the power cycle — HA had not yet
reported the `off` (UniFi PDU outlets: >10 s latency, ~40 s re-provision). An
`on` only proves the new command landed if `get_state(e, attribute="all")`
shows `last_changed >= cycle start` (HA timestamps are tz-aware ISO; compare to
`datetime.now(timezone.utc)`). An unchanged `on` at the end of a generous window
means the `off` never registered. Worked example:
`apps/health_checks/shared/switch_power_cycle.py`.

Related: [[appdaemon-service-calls-and-staging]]
