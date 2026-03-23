# Vestaboard Controller Audit Runbook

## Purpose

Verify that the Vestaboard controller is correctly managing frame rotation, TTL enforcement, queue priority, fallback behavior, and sleep windows. This runbook should be run by an agent after the system has been live for at least 24 hours.

## Prerequisites

- Access to Grafana MCP server with Loki datasource (UID: `loki`) for production audits
- Or a local log file captured via `appdaemon -c appdaemon 2>&1 | tee audit-log-capture.txt` for dev audits
- AppDaemon logs available via `{app="appdaemon"}` label in Loki (production)
- Production apps use names WITHOUT `_dev` suffix (e.g., `calendar_clock` not `calendar_clock_dev`)

## Queue behavior reference

The controller uses a **FIFO** queue system with these key behaviors:

| Concept | Behavior |
|---------|----------|
| **Pending queue** | FIFO — first pushed = first promoted. One frame per source (same-source dedup). |
| **Fallback queue** | FIFO — first displaced = first re-promoted. One frame per source (same-source dedup). Consulted BEFORE pending. |
| **should_expire=True** | Frame **auto-leaves the board** when TTL elapses. Tick removes it and promotes from fallback/pending. |
| **should_expire=False** | Frame **holds the board** after TTL until displaced by a new push. |
| **Displacement** | ALL displaced frames go to fallback with remaining TTL preserved, regardless of `should_expire`. |
| **Exhausted fallback** | Fallback frames with `remaining_ttl_s=0` are pruned — they have no time left to display. |
| **Promotion priority** | Fallback first, then pending. Displaced frames resume before new content shows. |

## Step 1: Gather Configuration

### 1a. Automation instance config (from apps YAML)

Each automation ID maps to a specific instance. Read `apps-prod.yaml` (or `apps-dev.yaml` for dev) to get instance-specific details:

| Field | Where to find it | Why it matters |
|-------|-----------------|----------------|
| `calendar_entity` | Calendar Summary apps | Which Google/HA calendar this instance watches |
| `weather_entity` | Weather Schedule app | Which weather provider entity |
| `ai_provider_conf` | AI Art/Message apps | Which LLM model bundle |
| `frame_library_path` | Library apps | Path to frame library JSON |
| `prompt_data_bundles_path` | AI Message app | Path to prompt data YAML |

Build an **Automation Instance Table** like:

| App ID | Type | Instance Detail | TTL | should_expire | Trigger |
|--------|------|-----------------|-----|---------------|---------|
| calendar_clock | Calendar Clock | *(system clock)* | 15m | True | Every 5m |
| calendar_summary_family | Calendar Summary | `calendar.family_gmail` | 15m | True | Cooldown 180-300m |
| weather_schedule | Weather | `weather.forecast_home` | 60m | False | 07:45, 17:45 (force_push=True) |
| messages_from_library | Messages From Library | frame-library.json | 30m | True | Random 30-120m |
| art_from_library | Art From Library | frame-library.json | 30m | True | Random 60-240m |
| art_generated_by_ai | AI Art Generator | openai-pixel-art | 30m | True | Random 30-240m |
| message_generated_by_ai | AI Message Generator | openai-default | 30m | False | Random 30-240m |

### 1b. UI config (from HA sensor)

Query the HA sensor for current runtime config:

```
mcp__home-assistant__ha_get_state(entity_id="sensor.vestaboard_controller_status")
```

From `all_automations`, extract each automation's:
- `id`, `enabled`, `config.ttl_minutes`, `config.should_expire`
- For frequency-based: `frequency_min_minutes`, `frequency_max_minutes`
- For scheduled: `time_list`
- For calendar: `cooldown_min_minutes`, `cooldown_max_minutes`, `reminder_threshold_minutes`
- For calendar_clock: `update_interval_minutes`

Also note the sleep window from the controller's init log.

## Step 2: Query Board Writes (24h)

### From Loki (production)

```logql
{app="appdaemon"} |= "Board write successful"
```

Parameters:
- `startRfc3339`: 24 hours ago in UTC
- `endRfc3339`: now in UTC
- `limit`: 100 (paginate with `direction: forward` if needed)
- `direction`: forward

### From log file (dev)

```bash
grep "Board write successful" audit-log-capture.txt
```

This gives all physical board writes with timestamps and sources.

## Step 3: Query Queue Events (24h)

### Frame promotions (TTL expired, next frame shown)
```bash
grep "FrameQueue.*promoted" audit-log-capture.txt
```

### Frame pushes (immediate display or queued)
```bash
grep "FrameQueue.*push" audit-log-capture.txt
```

### should_expire auto-removal
```bash
grep "should_expire=True.*TTL expired.*removing" audit-log-capture.txt
```

### Fallback displacement
```bash
grep "displaced frame to fallback" audit-log-capture.txt
```

### Fallback pruning (exhausted frames)
```bash
grep "prune.*exhausted" audit-log-capture.txt
```

### Fallback same-source dedup
```bash
grep "fallback dedup same-source" audit-log-capture.txt
```

### Sleep suppression
```bash
grep "Board write suppressed" audit-log-capture.txt
```

### Cooldown events (calendar summary)
```bash
grep "Cooldown" audit-log-capture.txt
```

For Loki, use the equivalent `|=` filter syntax: `{app="appdaemon"} |= "pattern"`.

## Step 4: Build the Audit Table

Process the logs into a markdown table with these columns:

| Column | Description |
|--------|-------------|
| Time (local) | When the board write occurred |
| Source | Automation instance ID (e.g. `calendar_summary_family`) |
| Action | `displayed` (direct push), `promoted` (from fallback or pending), `same-source` (update), `queued`, `auto-removed` (should_expire TTL), `displaced-to-fallback`, `pruned`, `sleep-start`, `sleep-end` |
| Remaining TTL | Time left on the source's TTL window. |
| On Board | How long this frame stayed before the next write |
| Notes | Queue depth (pending=N fallback=N), dedup events, should_expire behavior |

Include **non-write events** (queued, auto-removed, displaced, pruned, dedup) as rows with italic/dash markers to show the full lifecycle.

### How to compute Remaining TTL

TTL anchors to when a source **first claims the board** (promoted or displayed). Same-source updates do NOT reset it.

```
ttl_anchor = timestamp of first display (promoted or displayed action)
remaining_ttl = ttl_s - (current_timestamp - ttl_anchor)
```

For same-source updates: `remaining = ttl_anchor + ttl_s - this_write_timestamp`

When a NEW source takes the board (promoted/displayed with different source), `ttl_anchor` resets.

### How to compute On Board

`next_write_timestamp - this_write_timestamp`

### Queue lifecycle rows

Include non-write events to show the full picture:
- `queued` — frame entered pending queue (note pending count)
- `queued (dedup)` — same-source frame replaced older pending frame
- `auto-removed` — `should_expire=True` + TTL expired → frame removed from board by tick
- `holds board` — `should_expire=False` + TTL expired → frame stays on board
- `displaced-to-fallback` — frame displaced mid-TTL, moved to fallback with remaining TTL
- `pruned (exhausted)` — fallback frame with `remaining_ttl_s=0` dropped
- `fallback dedup` — older same-source fallback frame evicted when newer one enters

## Step 5: Verify Expected Behavior

### TTL Enforcement
- [ ] Frames with `should_expire=True` auto-leave the board when TTL expires (within 15s tick tolerance)
- [ ] Frames with `should_expire=False` hold the board after TTL until displaced by a new push
- [ ] No frame stays on the board longer than its TTL + 15s unless `should_expire=False`

### Displacement and Fallback
- [ ] Displaced frames go to fallback with remaining TTL preserved
- [ ] Fallback frames with `remaining_ttl_s=0` are pruned (not re-promoted)
- [ ] No same-source duplicates in fallback (dedup evicts older)
- [ ] Fallback is promoted BEFORE pending
- [ ] Fallback is FIFO (first displaced = first re-promoted)
- [ ] No rapid cycling (fallback frame promoted → immediately expires → back to fallback)

### Pending Queue
- [ ] Pending is FIFO (first pushed = first promoted)
- [ ] Same-source dedup works: only one pending frame per source at a time
- [ ] Pending frames eventually get promoted (not starved by fallback cycling)
- [ ] Pending count stays reasonable (< 5 under normal operation)

### Same-Source Updates
- [ ] `calendar_clock` updates every N minutes (configured `update_interval_minutes`) with same-source replacement (no queuing)
- [ ] Same-source updates do NOT reset the displayed_at timestamp (TTL counts from original display time)
- [ ] `weather_schedule` re-fetches every 15 minutes during its TTL window

### Sleep Window
- [ ] No board writes occur during the sleep window (01:00 - 06:45 by default)
- [ ] Board reconciles on wake (first write after sleep end)
- [ ] Queue state is reasonable after wake (no bloated fallback/pending from sleep-window accumulation)

### Calendar Summary
- [ ] Events are only shown when future (`seconds_until >= 0`)
- [ ] Force push (`override_ttl=True`) only happens for events within reminder threshold
- [ ] Cooldown prevents immediate re-push after non-urgent display
- [ ] `max_age_s` is set on frames to prevent stale queued frames from being promoted

### Weather Schedule
- [ ] Fires at configured times (e.g., 07:45, 17:45)
- [ ] `force_push=True` overrides active TTL when configured
- [ ] Re-fetches every 15 minutes during its TTL window
- [ ] Weather frames are not stuck in fallback after TTL expires

### Frequency-Based Automations (messages, art, AI)
- [ ] Fire intervals fall within configured min/max range
- [ ] No duplicate fires in rapid succession

## Step 6: Identify Anomalies

Flag any of these as issues:

1. **Stale frame**: Frame on board for > TTL + 30s without being `should_expire=False` holding
2. **Rapid cycling**: Multiple board writes from the same source within 30 seconds (unless restart or same-source update)
3. **Fallback cycling**: Frame repeatedly promoted from fallback with `remaining_ttl_s=0` (should have been pruned)
4. **Fallback bloat**: fallback count > 3 (suggests dedup or pruning failure)
5. **Pending starvation**: pending count > 5 or pending frames never promoted over a long period
6. **Missing automation**: An enabled automation that never fires in 24h
7. **Elapsed events**: Calendar summary showing "AGO" on a NEW push (not countdown update)
8. **Same-source duplicates**: Multiple frames from the same source in fallback or pending
9. **Writes during sleep**: Any board write between sleep start and sleep end

## Step 7: Report

Generate a summary with:
1. Total board writes in audit period
2. Writes per source (breakdown)
3. Average time on board per source
4. TTL compliance rate (% of `should_expire=True` frames that auto-removed within TTL + 15s)
5. Fallback health: max fallback depth, any cycling detected, prune events
6. Pending health: max pending depth, starvation periods
7. Sleep window compliance (any writes during sleep?)
8. Anomalies found (from Step 6)
9. The full audit table (or key excerpts for long sessions)

## Example Queries

### Loki (production)

```logql
# All board writes in last 24h
{app="appdaemon"} |= "Board write successful"

# Extract source from board write log line
{app="appdaemon"} |= "Board write successful" | regexp "source='(?P<source>[^']+)'"

# Count board writes per source
sum by (source) (count_over_time({app="appdaemon"} |= "Board write successful" | regexp "source='(?P<source>[^']+)'" [24h]))

# Queue depth over time
{app="appdaemon"} |= "pending=" | regexp "pending=(?P<pending>\\d+)"

# Fallback cycling detection
{app="appdaemon"} |= "re-promoting with remaining_ttl_s=0"

# Exhausted frame pruning
{app="appdaemon"} |= "prune" |= "exhausted"
```

### Log file (dev)

```bash
# Board writes per source
grep "Board write successful" audit-log-capture.txt | grep -oP "source='[^']+'" | sort | uniq -c | sort -rn

# Queue depth progression
grep "pending=" audit-log-capture.txt | grep -oP "pending=\d+ fallback=\d+" | tail -20

# Fallback cycling detection
grep "re-promoting with remaining_ttl_s=0" audit-log-capture.txt | wc -l

# Exhausted frame pruning
grep "prune.*exhausted" audit-log-capture.txt | wc -l

# Same-source fallback dedup
grep "fallback dedup same-source" audit-log-capture.txt | wc -l

# Rapid write detection (writes within 15s of each other)
grep "Board write successful" audit-log-capture.txt | awk -F'[ :]' '{print $1,$2":"$3":"$4}' | uniq -c | sort -rn | head -10
```

## Notes

- The tick interval is 15 seconds, so TTL transitions have up to 15s latency
- Same-source pushes (e.g., clock updates) replace the displayed frame immediately without queuing
- The `source=` parameter in board write logs was added in commit 38c4df3 — older logs won't have it
- Dev instance apps end in `_dev`; production apps do not
- Dev instance logs go to terminal stdout; production logs go to Loki via Kubernetes
- Fallback same-source dedup was added in commit 71b61c9 — older logs may show duplicate same-source fallback entries
- Exhausted fallback pruning (remaining_ttl_s=0) was added in commit 0fc14b2 and refined in 71b61c9
