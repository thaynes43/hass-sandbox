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

Related: [[appdaemon-service-calls-and-staging]]
