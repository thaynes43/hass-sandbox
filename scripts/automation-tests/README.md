# Movie-room automation checks

These checks load the actual YAML mirrors. No test connects to HA or operates devices.
Run serially with fake time:

```bash
nice -n 19 uv run --with jinja2 --with pyyaml --with pytest pytest -q scripts/automation-tests
```

The bounded executor implements the YAML constructs used by these automations, including the
top-level condition checked before restart mode stops a run, and HA start and
`automation_reloaded` events. It checks the full 15-minute silent absence delay plus the
15/10/5/1-minute countdown, both-sensor aggregation, cancellation in each wait and during
announcements, TV transitions, pending-toggle cancellation, light holds (LED1 colour or the
lights automation turned off), failed announcements, the silent lights-only countdown (TV not
confirmed active, including unavailable/unknown), and the recheck triggers: hold release,
LED1 change, a light group turning on, HA start and automation reload. A recheck starts the
countdown immediately in a vacant room and never restarts a running one.
It is not a complete HA runtime. No CPU burners, stress tests or parallel test loops.

`ha_runtime_probe.py` separately validates the original configurations with the installed
HA validator and executes duration-shortened copies in a NEW isolated `HomeAssistant`
object, including a silent lights-only run with the TV unavailable and the lights-automation
hold. It runs the actions only, with every wait shortened, so it does not exercise the
top-level "never restart a running countdown" condition or the recheck's skipped absence
window; the fake-time tests cover those, and the condition was also seen live
(`failed_conditions` on a reload during a running countdown, 2026-10-19). Its service registry contains only fake handlers. It reads a JSON array of the
TV and lights configurations from stdin; it requires HA's Python environment and is
not imported by pytest or CI. The supported deployment is a separate operation through
HA's automation config API, which reloads only the specified automation ID in the
installed version. Neither test performs deployment.

Rollback and incident evidence: `.agents/plans/movie-room-off-2026-10-17-evidence/`.
Deployment/handoff: `.agents/plans/movie-room-coordinated-shutdown-fix.md`.
