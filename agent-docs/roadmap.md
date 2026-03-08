## Home Automation Roadmap

This is a living roadmap for evolving the home into a **data-rich, reliable, and observable** system.

Guiding principles:

- **UI-first control knobs**: anything a human needs to tune/override should be visible on the dashboard.
- **Few helpers, not helper sprawl**: use helpers for user-facing toggles and “knobs”; avoid helpers as internal program state unless it must survive restarts.
- **Right tool for the job**: keep simple glue in HA YAML; move stateful/algorithmic workflows into AppDaemon when YAML gets brittle.

---

## Phase 1 (Now): Finish Occupancy-Based Lighting (foundational)

Goal: consistent patterns, stable behavior, fewer edge cases.

### High priority zones

- **Mudroom**
  - Add lux gating using the night light lux sensor to avoid turning lights on when it’s already bright.

- **Downstairs Bathroom**
  - Need to turn off the other lights using the presence sensor

- **Entrance**
  - Standard mmWave occupancy on/off.
  - Consider lux gating due to spill light from mudroom/kitchen.

- **Garage**
  - Reduce “stays on too long” behavior.
  - Merge second mmWave + one primary camera into a coherent occupancy signal (spring priority).

### Medium priority zones

- **Kitchen**
  - Decide whether this is “occupancy-driven” or “cleanup/off if left on after hours”.

- **Dining room**
  - Two mmWave + ecobee sensor — define the occupancy truth table.

- **Living room**
  - Cleanup/off-after-hours baseline; later: accent/sconces modes.

- **Foyer**
  - Advanced: path lighting at night (single recessed lights triggered by door/route).

- **Laundry room**
  - Currently built-in occupancy control; evaluate lux gating.

### Low priority / optional

- **Study**: likely minimal/no automation.

- **Blue / Pink / White rooms**
  - Mostly ZEN32 + ecobee only; consider “lights left on” watchdog for Pink room.

- **Primary bedroom suite**
  - Closet is good; “Clofffice” needs occupancy sensing plan (FP2/FP300?).
  - Might want lux for the primary bath vanity as mornings are very bright

---

## Phase 2 (Next): Brightness + time-of-day behaviors

Goal: “more human” lighting without constant manual adjustments.

- Define per-zone brightness schedules (time-of-day / scene-like behaviors).
- Add overrides that are easy from the dashboard.
- Standardize “manual override” vs “automation intent” semantics.

---

## Phase 3 (Next): Dashboard 2.0 (observability + tuning)

Goal: dashboards are a **control plane**, not just pretty tiles.

- Expand the Bubble Card patterns across zones.
- Add lightweight “why did this happen?” visibility:
  - sensor state snapshots
  - automation state + last triggered
  - key helpers (“knobs”)

Targets:
- in-wall UniFi Connect displays
- iPad wall mount

---

## Phase 4 (Next): Voice 3.0 (reliability + latency)

Goal: voice is reliable enough to replace “walking to the wall tablet”.

- Reduce end-to-end latency (wake → intent → action → confirmation).
- Evaluate local models (Ollama) vs OpenAI for tool-calling accuracy.
- Improve “voice as UI” patterns:
  - confirmations for sensitive actions
  - countdown + cancel flows (like the Movie Room TV auto-off)

---

## Phase 5 (Next): Home / Away / Vacation inference

Goal: a single coherent “house state” that other systems can trust.

- Fuse signals:
  - occupancy sensors, alarm state, UniFi Protect, presence, schedules
- Produce a small set of “outputs” for downstream automations:
  - e.g. `input_select.house_mode`, `input_boolean.quiet_hours`, etc.

This is a strong candidate for AppDaemon if it becomes stateful/algorithmic.

---

## Phase 6 (Next): Enhance ZEN32 scene controllers

Goal: stop needing separate fan remotes.

- Fan speed controls for Modern Forms fans.
- Add dimming where appropriate.
- Seasonal logic:
  - fan reverse in winter vs normal in summer

---

## Phase 7 (Next): Flood Watch 2.0 (expand + automate mitigation)

Goal: detect early, notify correctly, mitigate safely.

- Expand sensors (under sinks, other leak points).
- Integrate Zooz smart valve shutoff:
  - only for specific leak classifications
  - with a clear “human override” and audible confirmation

---

## Phase 8 (Future): New devices / systems

- **Unfolded Circle Remote 3**
  - Integrate and map high-value routines.

- **Pool & hot tub**
  - Integrations + safety monitoring (“are people in the pool” checks).

- **Shed**
  - eBike charging automation using power monitoring:
    - stop power when charging completes / after safe threshold

---

## Phase 9 (Future): UniFi Camera + LLM image recognition

Goal: vision becomes a reliable “sensor”.

- Local inference (Ollama) to control cost.
- Use one “golden path” camera per area.
- Patterns:
  - driveway/package detection
  - garage door open/close verification
  - “people in pool” safety checks

---

## AppDaemon (tooling direction)

AppDaemon is likely valuable when:

- workflows require **persistent or derived state**
- you need **multi-step sequences** with retries/backoff
- you want **structured code + better logs** than YAML affords

Adopt incrementally:

- Start with 1–2 apps (house-mode engine, media/presence supervisor)
- Keep HA YAML as the UI + device glue layer
