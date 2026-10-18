# Movie room unexpected lights OFF — read-only story

Saga: Home Assistant reliable room control (existing repository automation project).
Epic: Explain occupied movie-room lighting shutdown with attributable runtime evidence.

Scope: Investigate October 17, 2026 America/New_York (04:00Z Oct 17–04:00Z Oct 18). Only OFF is unexpected; Penelope manually restored ON. No remediation, device commands, configuration changes, restarts, broadcasts, or other sessions. Separately created Opus session remains untouched.

Acceptance: Discover live entities/configuration; narrow timestamps from recorder; correlate OFF and manual ON anchor, TV, both occupancy sensors, traces and available context chains; distinguish proof from hypotheses and document retention gaps. Preserve evidence without credentials.

Dependencies: Existing authorized HA MCP read access; user's account of manual ON and TV remaining ON. Runtime evidence availability must be checked.

Owner: This single Codex session, agent/hass-sandbox-1017-233702 at /home/dev/work/hass-sandbox-1017-233702. Owns only investigation documents/evidence here; reads HA through MCP. No agents or Claude sessions.

Budget: Target 45 minutes, concurrency 1; no paid API calls. Quota/usage allowance unknown. Checkpoint at 30 minutes or if evidence access blocks attribution.

Artifacts/handoff: This record, sanitized MCP results in an evidence subdirectory, final findings with local/UTC times and limits. No application PR/deployment authorized.

Stop: Evidence-backed attribution or documented evidence limit; budget/context boundary; explicit pause. Smallest proposed fix only after cause established; no implementation.

Status: Complete. OFF attributed to retained five-minute lights automation trace and matching logbook/recorder transitions. TV path cancelled before its first warning. Sensor-report cause remains unestablished; evidence limits documented. See movie-room-off-2026-10-17-findings.md and movie-room-off-2026-10-17-evidence/. No remediation authorized or applied; next safe step is review of the proposed shared shutdown behavior.
