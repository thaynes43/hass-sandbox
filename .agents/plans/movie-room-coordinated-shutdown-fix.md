# Story: coordinate movie-room TV and lights shutdown

Saga: Home Assistant reliable room control. Epic: safe occupied movie-room lighting and warned empty-room shutdown.

Scope: Tom confirmed the 2026-10-17 23:18:49 EDT incident and authorized this fix and scoped deployment in the same session. Update only the two affected automations and their mirrors/docs/tests. TV-active shutdown owns TV plus both light groups; first warning at least 15 minutes before shutdown, reminders at 10/5/1. Cancel on occupancy and handle TV changes/holds without bypass. No sensor tuning or unrelated refactoring. Preserve original incident evidence and rollback configs.

Acceptance: Simulated timing/cancellation/hold/TV-transition tests; review diff and available findings; ready PR, required checks, squash merge; scoped deployment through supported HA mechanism; read back loaded configuration and enabled/idle state. No real broadcasts, device actions, trigger tests, HA restart or global reload. Stop with exact blocker if a broader operation is required.

Dependencies: Confirmed attribution and evidence in movie-room-off-2026-10-17-findings.md. Live originals match saved rollback configs (hashes 2ed7e24f71801b90 and d78c34ad1a2a661c). Both automations currently enabled/idle; pending helper off.

Owner: This same single Codex session, existing isolated task worktree/branch. No subagents or Claude usage; separate Opus session untouched. Authorized runtime writes limited to these two automation configurations via supported scoped update.

Budget: Target 60 minutes, checkpoint at 45 minutes/context pressure; concurrency one; actual quota unknown, no metered API work. Tests low CPU, serial/fake time; no burners, stress tests, wide parallel/looped test runs.

Plan: Preserve effective pre-warning absence delay max(existing TV delay helper minus 5 minutes, zero), currently 15 minutes; add 15-minute warning phase for nominal 30 minutes total. Preserve helper value rather than silently changing it. Capture rollback before writes, deploy guards first then owner; verify without execution.

Artifacts/handoff: YAML mirrors, focused offline tests, PR/check evidence, deployment readback and rollback instructions here. Stop when accepted deployment verified, concrete scope/approval blocker, budget/context threshold, or explicit pause.

## Explicit checkpoint — stopped

Tom directed stopping at the PR/CI boundary without permission to consume Claude CI. No further HA queries, writes, tests, PR creation, merging or deployment after this checkpoint. Separate Opus session untouched; no agents/Claude sessions launched.

Tested source: commit 6753b38fca6d2b95f3b8528d463d9c73db791a13, pushed to agent/hass-sandbox-1017-233702. No PR opened, no GitHub CI run for this branch/PR verified, no merge. Local review completed. Existing Claude PR review/audits were not invoked.

Runtime status must be stated accurately: BEFORE this checkpoint, the previously authorized two scoped config updates had already succeeded at 2026-10-18 00:14:43 EDT (04:14:43Z, lights) and 00:14:51 EDT (04:14:51Z, TV). HA's installed config API reloads only the supplied automation ID. No global reload or restart occurred. Both saved configs and loaded raw_config exactly matched the tested YAML; both automations were on/current=0, last_triggered unchanged. Pending remained off; lights, TV and satellite last_changed timestamps were unchanged. An unrelated automation's last_changed was unchanged. This deployment is live but source is NOT merged. Do not redeploy or roll back while waiting for the CI decision.

Evidence: movie-room-coordinated-shutdown-deployment.json (before/after states, scoped write receipts and stored config hashes); movie-room-coordinated-shutdown-runtime-readback.json (supported read-only automation/config WebSocket responses from the running entities). Original incident evidence remains unchanged.

Behavior: current helper 00:20:00 retains max(helper minus 5 min, 0) = 15 min initial absence. Warn 15/10/5/1 minutes remaining, wait 5/5/4/1 minutes after successful announcements; nominal total 30 min plus speech time, so first warning precedes shutdown by at least 15 min. Either occupancy sensor on/unknown/unavailable cancels the entire countdown; TV off/unknown/unavailable or pending-toggle off also cancels and clears pending. TV becoming active while already empty starts a fresh full cycle; active playback state changes do not reset it. Failed service/announcement aborts the shutdown subtree and clears pending. Immediate wait-template evaluation and repeated native guards cover changes during announcements. Existing LED manual/automation hold comparisons protect lights at shutdown; TV auto-off policy remains unchanged. Both light groups shut down before TV to avoid the TV-off restart interrupting the lights action.

TV-off lights-only cleanup: requires confirmed TV off, both sensors off and no pending TV shutdown; starts a fresh configured five-minute delay, with hold checks before/after. Occupancy or TV activation cancels. A subsequent off-to-unavailable transition is allowed for webOS's normal powered-off behavior; unknown/unavailable alone cannot START cleanup. Countdown cancellation never completes an old lights timer.

Validation: 84 serial fake-time config-driven tests passed in 3.03s. Both configurations passed the installed HA validator. Five real HA Script probes in a NEW isolated HomeAssistant object with only fake services passed: success, announcement failure, manual hold, automation hold and occupancy during announcement. Tests never connected to live HA or called real services. Schema validation/probe processes used the installed Python modules through in-pod exec; configuration deployment and runtime readback were separate. No actual announcements, shutdowns, room controls or live trigger tests performed. These remain deliberately unverified tonight. Tool style warnings were reviewed: compound cross-entity trigger edges and hold comparisons require templates; wait_template's immediate evaluation is intentional for cancellation already true during an announcement. The preexisting both-sensor occupancy recheck was retained to avoid unrelated refactoring.

Rollback: the two original complete configs are in movie-room-off-2026-10-17-evidence/automation_light_inovelli_movie_room_motion_cleared_lights_off.json and automation_media_basement_movie_room_no_occupancy_tv_off.json, under config (including IDs/categories). Fetch current hashes immediately before any authorized rollback; restore original TV config first, then original lights config via ha_config_set_automation using each latest config_hash and original category. This restores the incident-era five-minute unannounced lights behavior, so rollback is not to be executed casually. No rollback performed.

Minimal next authorization: allow ONLY the repository's existing Claude GitHub CI reviewers/audits for this PR (no local Claude sessions or additional agents), and resume the ready-PR/check/review/merge chain. If all Claude usage stays prohibited, keep this boundary held; changing shared CI policy needs separate explicit authorization. Pending clarification is not approval. Re-check live/source drift read-only only after Tom resumes work.
