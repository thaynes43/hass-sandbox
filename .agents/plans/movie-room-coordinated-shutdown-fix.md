# Story: coordinate movie-room TV and lights shutdown

Saga: Home Assistant reliable room control. Epic: safe occupied movie-room lighting and warned empty-room shutdown.

Scope: Tom confirmed the 2026-10-17 23:18:49 EDT incident and authorized this fix and scoped deployment in the same session. Update only the two affected automations and their mirrors/docs/tests. TV-active shutdown owns TV plus both light groups; first warning at least 15 minutes before shutdown, reminders at 10/5/1. Cancel on occupancy and handle TV changes/holds without bypass. No sensor tuning or unrelated refactoring. Preserve original incident evidence and rollback configs.

Acceptance: Simulated timing/cancellation/hold/TV-transition tests; review diff and available findings; ready PR, required checks, squash merge; scoped deployment through supported HA mechanism; read back loaded configuration and enabled/idle state. No real broadcasts, device actions, trigger tests, HA restart or global reload. Stop with exact blocker if a broader operation is required.

Dependencies: Confirmed attribution and evidence in movie-room-off-2026-10-17-findings.md. Live originals match saved rollback configs (hashes 2ed7e24f71801b90 and d78c34ad1a2a661c). Both automations currently enabled/idle; pending helper off.

Owner: This same single Codex session, existing isolated task worktree/branch. No subagents or Claude usage; separate Opus session untouched. Authorized runtime writes limited to these two automation configurations via supported scoped update.

Budget: Target 60 minutes, checkpoint at 45 minutes/context pressure; concurrency one; actual quota unknown, no metered API work. Tests low CPU, serial/fake time; no burners, stress tests, wide parallel/looped test runs.

Plan: Preserve effective pre-warning absence delay max(existing TV delay helper minus 5 minutes, zero), currently 15 minutes; add 15-minute warning phase for nominal 30 minutes total. Preserve helper value rather than silently changing it. Capture rollback before writes, deploy guards first then owner; verify without execution.

Artifacts/handoff: YAML mirrors, focused offline tests, PR/check evidence, deployment readback and rollback instructions here. Stop when accepted deployment verified, concrete scope/approval blocker, budget/context threshold, or explicit pause. Status active.
