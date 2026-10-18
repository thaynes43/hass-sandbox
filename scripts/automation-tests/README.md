# Movie-room automation checks

These checks load the actual YAML mirrors. No test connects to HA or operates devices.
Run serially with fake time:

```bash
nice -n 19 uv run --with jinja2 --with pyyaml --with pytest pytest -q scripts/automation-tests
```

The bounded executor implements the YAML constructs used by these automations. It checks
the full 15-minute silent absence delay plus 15/10/5/1-minute warnings, both-sensor
aggregation, cancellation in each wait and during announcements, TV transitions,
pending-toggle cancellation, light holds, failed announcements, and lights-only cleanup.
It is not a complete HA runtime. No CPU burners, stress tests or parallel test loops.

`ha_runtime_probe.py` separately validates the original configurations with the installed
HA validator and executes duration-shortened copies in a NEW isolated `HomeAssistant`
object. Its service registry contains only fake handlers. It reads a JSON array of the
TV and lights configurations from stdin; it requires HA's Python environment and is
not imported by pytest or CI. The supported deployment is a separate operation through
HA's automation config API, which reloads only the specified automation ID in the
installed version. Neither test performs deployment.

Rollback and incident evidence: `.agents/plans/movie-room-off-2026-10-17-evidence/`.
Deployment/handoff: `.agents/plans/movie-room-coordinated-shutdown-fix.md`.
