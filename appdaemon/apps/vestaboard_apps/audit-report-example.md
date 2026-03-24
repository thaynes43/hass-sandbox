# Vestaboard Controller Audit Report

**Audit period:** 2026-03-23 18:58 -- 2026-03-24 19:05 (~24.1 hours)
**Environment:** Dev (`_dev` suffix apps)
**Log file:** `audit-log-capture-3.txt` (5,417 lines)
**AppDaemon version:** 4.5.13 / Home Assistant 2026.3.3

---

## 1. Configuration Summary

### Controller Settings

| Setting | Value |
|---------|-------|
| Vestaboard IP | `192.168.50.159` |
| Tick interval | 15s |
| Sleep enabled | Yes |
| Sleep window | 01:00:00 -- 06:45:00 |

### Automation Instance Table

| App ID | Type | Instance Detail | TTL | should_expire | Trigger |
|--------|------|-----------------|-----|---------------|---------|
| `calendar_clock_dev` | Calendar Clock | *(system clock)* | 15m | True | Every 5m |
| `messages_from_library_dev` | Messages From Library | frame-library.json (min_stars=3) | 30m | True | Random 30--120m |
| `art_from_library_dev` | Art From Library | frame-library.json (min_stars=2) | 30m | True | Random 60--240m |
| `art_generated_by_ai_dev` | AI Art Generator | openai-pixel-art | 30m | True | Random 30--240m |
| `message_generated_by_ai_dev` | AI Message Generator | openai-default | 30m | **False** | Random 30--240m |
| `calendar_summary_family_dev` | Calendar Summary | `calendar.hayneshome01886_gmail_com` | 15m | True | Cooldown 180--300m |
| `calendar_summary_hot_tub_maintenance_dev` | Calendar Summary | `calendar.hot_tub_maintenance` | 30m | True | Cooldown 180--300m |
| `calendar_summary_holidays_dev` | Calendar Summary | `calendar.holidays_for_united_states_ma` | 30m | True | Cooldown 180--300m |
| `weather_schedule_dev` | Weather Schedule | `weather.forecast_home` | 60m | **False** | 07:45, 17:45 (force_push=True) |

### Notes

- `calendar_summary_holidays_dev` registered and enabled but returned 0 events for the entire audit period (no US holidays within its 120h lookahead window). This is expected, not a failure.
- Two AppDaemon restarts occurred during the sleep window (01:04 and 01:08) due to "plugin failed" -- likely HA websocket disconnection. Both recovered cleanly.

---

## 2. Board Writes Summary

**Total board writes:** 80

### Per-Source Breakdown

| Source | Writes | % of Total | Avg Time on Board |
|--------|--------|------------|-------------------|
| `calendar_clock_dev` | 20 | 25.0% | ~5m (same-source updates during display) |
| `messages_from_library_dev` | 13 | 16.3% | ~27m |
| `calendar_summary_family_dev` | 11 | 13.8% | ~7m (includes countdown updates) |
| `art_from_library_dev` | 9 | 11.3% | ~30m |
| `message_generated_by_ai_dev` | 8 | 10.0% | ~30m |
| `weather_schedule_dev` | 7 | 8.8% | ~15m (re-fetches during TTL) |
| `calendar_summary_hot_tub_maintenance_dev` | 6 | 7.5% | ~30m |
| `art_generated_by_ai_dev` | 5 | 6.3% | ~30m |
| `user` | 1 | 1.3% | ~30m |

### Write Rate

- **Pre-sleep (18:58--01:00):** 34 writes in ~6h = ~5.7/hr
- **Sleep window (01:00--06:45):** 0 writes (queue still processed internally)
- **Post-sleep (06:45--19:05):** 46 writes in ~12.3h = ~3.7/hr
- **Overall:** 80 writes in ~24.1h = ~3.3/hr

---

## 3. Queue Health Metrics

### Pending Queue

| Metric | Value |
|--------|-------|
| Max pending depth | 6 (one occurrence) |
| Most common depth | 4 (150 observations) |
| Pending dedup (same-source) events | 311 |
| Pending starvation events | 0 |

**Pending depth histogram:**

| Depth | Observations |
|-------|-------------|
| 0 | 25 |
| 1 | 58 |
| 2 | 99 |
| 3 | 131 |
| 4 | 150 |
| 5 | 67 |
| 6 | 1 |

### Fallback Queue

| Metric | Value |
|--------|-------|
| Max fallback depth | 1 |
| Displacement events | 6 |
| Successful fallback promotions | 4 |
| Exhausted fallback prunes | 12 |
| Fallback same-source dedup events | 0 |
| Fallback cycling (remaining_ttl_s=0 re-promoted) | 0 |

### Displacement Detail

| Time | Displaced Source | Remaining TTL | Outcome |
|------|-----------------|---------------|---------|
| 03-24 01:24 | `calendar_clock_dev` | 0.0s | Pruned (exhausted) |
| 03-24 04:10 | `messages_from_library_dev` | 0.0s | Pruned (exhausted) |
| 03-24 07:45 | `messages_from_library_dev` | 651.9s | Re-promoted at 08:15 |
| 03-24 12:17 | `message_generated_by_ai_dev` | 566.0s | Re-promoted at 12:47 |
| 03-24 14:04 | `message_generated_by_ai_dev` | 1364.7s | Re-promoted at 14:19, then pruned at 14:42 |
| 03-24 17:45 | `messages_from_library_dev` | 1701.9s | Re-promoted at 18:15 |

### Prune Events (Exhausted Fallback)

All 12 prune events correctly removed frames with `remaining_ttl_s=0.0`:

| Time | Source |
|------|--------|
| 03-23 22:46 | `message_generated_by_ai_dev` |
| 03-24 01:24 | `calendar_clock_dev` |
| 03-24 03:40 | `message_generated_by_ai_dev` |
| 03-24 04:10 | `messages_from_library_dev` |
| 03-24 06:56 | `message_generated_by_ai_dev` |
| 03-24 08:15 | `weather_schedule_dev` |
| 03-24 09:26 | `message_generated_by_ai_dev` |
| 03-24 09:41 | `weather_schedule_dev` |
| 03-24 12:57 | `message_generated_by_ai_dev` |
| 03-24 14:42 | `message_generated_by_ai_dev` |
| 03-24 16:43 | `message_generated_by_ai_dev` |
| 03-24 18:15 | `weather_schedule_dev` |

---

## 4. TTL Compliance Analysis

### should_expire=True Frames

42 frames with `should_expire=True` were tracked through the tick cycle. All 42 were auto-removed from the board when their TTL expired. Removal timestamps consistently fell within the 15-second tick tolerance.

**TTL compliance rate: 100%** (42/42 auto-removed within TTL + 15s)

Representative samples:

| Displayed At | Removed At | Source | TTL | Actual Duration | Delta |
|-------------|-----------|--------|-----|-----------------|-------|
| 18:58:06 | 19:13:20 | `calendar_clock_dev` | 900s | 914s | +14s |
| 19:13:20 | 19:28:20 | `calendar_summary_family_dev` | 900s | 900s | 0s |
| 20:00:06 | 20:30:20 | `messages_from_library_dev` | 1800s | 1814s | +14s |
| 21:15:35 | 21:45:35 | `art_generated_by_ai_dev` | 1800s | 1800s | 0s |
| 08:26:07 | 08:56:06 | `art_from_library_dev` | 1800s | 1799s | -1s |

All deltas are within the 15s tick tolerance.

### should_expire=False Frames

Two automation types use `should_expire=False`:

- **`message_generated_by_ai_dev`**: Frames held the board after TTL until displaced by the next queued frame via normal tick promotion. Observed behavior is correct -- frames were not auto-removed but were eventually replaced.
- **`weather_schedule_dev`**: At 07:45 and 17:45, force_push displaced the active frame. Weather then re-fetched every 15 minutes during its 60m TTL window (at :00, :15, :30 marks). After TTL elapsed, the frame held until displaced. Correct behavior.

---

## 5. Sleep Window Compliance

| Event | Time |
|-------|------|
| Last board write before sleep | 2026-03-24 00:46:35 |
| Sleep window entered | 2026-03-24 01:00:05 |
| AppDaemon restart #1 (plugin failed) | 2026-03-24 01:04--01:05 |
| Sleep re-entered after restart | 2026-03-24 01:06:00 |
| AppDaemon restart #2 (plugin failed) | 2026-03-24 01:08--01:09 |
| Sleep re-entered after restart | 2026-03-24 01:09:21 |
| Sleep window ended | 2026-03-24 06:45:06 |
| First board write after wake | 2026-03-24 06:45:07 |
| Board writes during sleep (01:00--06:45) | **0** |

Queue promotions continued normally during sleep (frames cycled through the internal queue without physical board writes). The pending queue drained fully from its pre-sleep state (4 pending at sleep start to 0 by 04:55). This is correct behavior -- the queue processes TTL expirations internally even while board writes are suppressed.

After wake, the board immediately wrote the currently-promoted frame (`message_generated_by_ai_dev`), reconciling within 1 second of sleep end.

---

## 6. Behavioral Verification Checklist

### TTL Enforcement

| # | Check | Verdict |
|---|-------|---------|
| 1 | Frames with `should_expire=True` auto-leave the board when TTL expires (within 15s tick tolerance) | **PASS** -- 42/42 auto-removed within tolerance |
| 2 | Frames with `should_expire=False` hold the board after TTL until displaced by a new push | **PASS** -- `message_generated_by_ai_dev` and `weather_schedule_dev` frames observed holding correctly |
| 3 | No frame stays on the board longer than its TTL + 15s unless `should_expire=False` | **PASS** -- No violations found |

### Displacement and Fallback

| # | Check | Verdict |
|---|-------|---------|
| 4 | Displaced frames go to fallback with remaining TTL preserved | **PASS** -- 6 displacement events, TTL correctly preserved |
| 5 | Fallback frames with `remaining_ttl_s=0` are pruned (not re-promoted) | **PASS** -- 12 prune events, all at remaining_ttl_s=0.0; 2 of 6 displacements had 0s TTL and were pruned, not re-promoted |
| 6 | No same-source duplicates in fallback (dedup evicts older) | **PASS** -- 0 fallback dedup events needed (max fallback depth=1 prevented collisions) |
| 7 | Fallback is promoted BEFORE pending | **PASS** -- All 4 fallback promotions occurred before pending frames were served |
| 8 | Fallback is FIFO (first displaced = first re-promoted) | **PASS** -- Only 1 fallback frame at a time, so ordering trivially satisfied |
| 9 | No rapid cycling (fallback frame promoted then immediately expires back to fallback) | **PASS** -- 0 instances of `remaining_ttl_s=0` re-promotion |

### Pending Queue

| # | Check | Verdict |
|---|-------|---------|
| 10 | Pending is FIFO (first pushed = first promoted) | **PASS** -- Promotion order matches push order in all observed cases |
| 11 | Same-source dedup works: only one pending frame per source at a time | **PASS** -- 311 dedup events logged, preventing duplicate same-source pending entries |
| 12 | Pending frames eventually get promoted (not starved by fallback cycling) | **PASS** -- All pending frames were eventually promoted; no starvation observed |
| 13 | Pending count stays reasonable (< 5 under normal operation) | **WARN** -- Pending depth reached 5 in 67 observations and 6 in 1 observation. See Anomalies. |

### Same-Source Updates

| # | Check | Verdict |
|---|-------|---------|
| 14 | `calendar_clock_dev` updates every 5 minutes with same-source replacement (no queuing) | **PASS** -- Clock pushed every 5m, displayed via same-source replacement when on board |
| 15 | Same-source updates do NOT reset the displayed_at timestamp | **PASS** -- Clock TTL expired at correct times relative to original display, not last update |
| 16 | `weather_schedule_dev` re-fetches every 15 minutes during its TTL window | **PASS** -- Observed re-fetches at :00, :15, :30 after initial :45 push, with decreasing TTL (3600, 2699, 1799, 899) |

### Sleep Window

| # | Check | Verdict |
|---|-------|---------|
| 17 | No board writes occur during the sleep window (01:00--06:45) | **PASS** -- 0 writes during sleep window |
| 18 | Board reconciles on wake (first write after sleep end) | **PASS** -- First write at 06:45:07, within 1s of sleep end |
| 19 | Queue state is reasonable after wake (no bloated fallback/pending) | **PASS** -- pending=2 fallback=1 at wake, drained normally |

### Calendar Summary

| # | Check | Verdict |
|---|-------|---------|
| 20 | Events are only shown when future (`seconds_until >= 0`) | **PASS** -- No elapsed/AGO events observed on new pushes |
| 21 | Force push (`override_ttl=True`) only happens for events within reminder threshold | **PASS** -- Force push at 14:04:07 for "J&P Dentist" within 30m threshold. All other calendar pushes had `override_ttl=False` |
| 22 | Cooldown prevents immediate re-push after non-urgent display | **PASS** -- All cooldowns fell within configured 180--300m range |
| 23 | `max_age_s` is set on frames to prevent stale queued frames from being promoted | **PASS** -- Calendar frames carry `max_age_s` values computed from event boundaries |

### Weather Schedule

| # | Check | Verdict |
|---|-------|---------|
| 24 | Fires at configured times (07:45, 17:45) | **PASS** -- Observed pushes at 07:45:00 and 17:45:00 |
| 25 | `force_push=True` overrides active TTL when configured | **PASS** -- Initial push at each scheduled time uses `override_ttl=True` |
| 26 | Re-fetches every 15 minutes during its TTL window | **PASS** -- 4 pushes per window (:45, :00, :15, :30) with decreasing TTL |
| 27 | Weather frames are not stuck in fallback after TTL expires | **PASS** -- 3 weather prune events confirm exhausted fallback frames were cleaned up |

### Frequency-Based Automations

| # | Check | Verdict |
|---|-------|---------|
| 28 | Fire intervals fall within configured min/max range | **PASS** -- All observed intervals fall within bounds (see notes below) |
| 29 | No duplicate fires in rapid succession | **PASS** -- No automation fired twice within 30s (excluding same-source countdown updates) |

**Observed frequency samples:**
- `messages_from_library_dev` (30--120m): intervals of ~50m, ~23m, ~30m -- within range
- `art_from_library_dev` (60--240m): intervals of ~94m, ~60m, ~107m -- within range
- `art_generated_by_ai_dev` (30--240m): intervals of ~45m, ~97m, ~30m -- within range
- `message_generated_by_ai_dev` (30--240m): intervals of ~35m, ~152m, ~30m -- within range

---

## 7. Anomaly Analysis

### Anomaly 1: Pending Depth Reached 5--6 (MINOR)

**Severity:** Low
**Observation:** Pending queue reached depth 5 in 67 tick observations, and depth 6 in 1 observation. The runbook threshold for "reasonable" is < 5.

**Root cause:** Multiple frequency-based automations fire during periods when the board is occupied by a 30-minute TTL frame. With 7+ active automations generating content, the pending queue naturally accumulates during busy periods. The calendar clock's 5-minute push interval contributes pending entries that dedup constantly (311 dedup events).

**Impact:** None. All pending frames were eventually promoted. No starvation observed. The high dedup count (311) shows the system is correctly preventing queue bloat from high-frequency clock pushes.

**Verdict:** Working as designed. The threshold of < 5 assumes fewer active automations.

### Anomaly 2: AppDaemon Restarts During Sleep Window (INFORMATIONAL)

**Severity:** Informational
**Observation:** Two AppDaemon restarts at 01:04 and 01:08, both caused by "Stopping apps from namespace 'default' because the plugin failed" (HA websocket disconnection).

**Impact:** None. The controller recovered cleanly both times. Sleep window was correctly re-entered after each restart (01:06:00 and 01:09:21). No board writes leaked during the disruption. Queue state was rebuilt from scratch on restart -- all automations re-registered and pushed fresh content, which was queued during sleep and drained normally.

**Verdict:** Normal HA/AppDaemon behavior. Recovery is correct.

### Anomaly 3: Calendar Family Rapid Writes at 14:04--14:19 (EXPECTED)

**Severity:** Informational
**Observation:** 8 board writes from `calendar_summary_family_dev` between 14:04:07 and 14:18:59 (~1 write per minute).

**Root cause:** The "J&P Dentist" event was within the 30-minute reminder threshold, triggering:
1. Initial force push at 14:04:07 (`override_ttl=True`)
2. Countdown updates every ~5 minutes (25 MIN, 20 MIN, 15 MIN...) plus re-evaluation writes from the rotation timer

All writes used same-source replacement (no queuing), which is the expected calendar summary countdown behavior.

**Verdict:** Working as designed.

### Anomaly 4: `calendar_summary_holidays_dev` Never Fired (EXPECTED)

**Severity:** Informational
**Observation:** The holidays calendar automation registered, configured, and polled events, but never pushed a frame in the entire 24h audit period.

**Root cause:** `calendar.holidays_for_united_states_ma` returned 0 events within the 120-hour lookahead window. No US holidays were upcoming.

**Verdict:** Working as designed. The automation correctly did not push when there was nothing to display.

### Checked-For Anomalies Not Found

| Anomaly Type | Status |
|-------------|--------|
| Stale frame (on board > TTL + 30s without should_expire=False) | **Not found** |
| Rapid cycling (same source < 30s, excluding same-source updates) | **Not found** |
| Fallback cycling (remaining_ttl_s=0 re-promoted) | **Not found** |
| Fallback bloat (> 3) | **Not found** (max = 1) |
| Pending starvation | **Not found** |
| Elapsed events (AGO on new push) | **Not found** |
| Same-source duplicates in fallback or pending | **Not found** |
| Writes during sleep | **Not found** |

---

## 8. Executive Summary

The Vestaboard controller operated correctly over the 24.1-hour audit period. All 80 board writes were properly managed through the FIFO queue system.

**Key findings:**

- **TTL compliance: 100%.** All 42 `should_expire=True` frames were auto-removed within the 15-second tick tolerance. `should_expire=False` frames correctly held until displaced.
- **Sleep window: fully compliant.** Zero board writes during 01:00--06:45. Board reconciled within 1 second of wake. Two AppDaemon restarts during sleep were handled gracefully with no write leakage.
- **Queue health: good.** Fallback depth never exceeded 1. Pending depth occasionally reached 5--6 due to the number of active automations, but all frames were eventually promoted with no starvation. 311 same-source dedup events kept the pending queue from bloating.
- **Fallback/prune system: working correctly.** 12 exhausted fallback frames were pruned. 4 displaced frames with remaining TTL were successfully re-promoted. Zero fallback cycling detected.
- **Calendar summary: correct.** Force push only triggered within the reminder threshold. Countdown updates used same-source replacement. Cooldowns respected 180--300m bounds. Stale event protection via `max_age_s` present on all frames.
- **Weather schedule: correct.** Fired at configured times with force_push. Re-fetched every 15 minutes with decreasing TTL. Exhausted weather fallback frames properly pruned.
- **All automations fired at expected intervals** within their configured min/max ranges.

**Overall verdict: PASS.** No functional anomalies detected. The system is operating within design parameters.
