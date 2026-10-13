# 006 — Give voice agents grounded light totals and coverage

**Status:** Proposed
**Size:** Medium (count semantics, house tool design and regression validation)
**Raised:** 2026-10-13, Muse reliability benchmark in hass-sandbox#246

## Problem

Muse's original benchmark passed nine of 12 turns. A prompt correction improved fresh lookups and contextual clarification; the targeted retest passed seven of eight turns. It still claimed “Two individual lights are on” after excluding a group, despite having no data establishing the physical bulb count. Both runs completed normally; this failure was not a timeout or truncation.

GetLiveContext reports exposed light controls, names, states and areas. Aliases identify the same record. A dimmer, a ceiling fixture and a group may each represent multiple bulbs. Excluding a named group does not turn the remaining controls into a physical bulb inventory. In the owner's real incident, three of 49 exposed light records were on, while the recorder had 26 on light entities including overlapping groups, members and indicators. Neither number establishes individual bulb totals.

## Current evidence and scope

The synthetic cases and bounded, read-only model runner are in `scripts/voice-bench/`. Untouched baseline, reviewed results, exact native prompt readbacks and the targeted retest are retained privately at `/home/dev/.local/state/hardware-maintenance/muse-light-count-vetting-20261013/`. Do not commit live entity inventory or tokens. The Rumpus physical microphone-to-speaker trial remains unproved.

## Design to resolve

Choose an explicit counting unit and expose a deterministic summary with its coverage and exclusions. A possible house tool could report exposed lighting controls on, requested area, aliases deduplicated, unsettled states and whether group/member or physical bulb information is known. Physical bulb totals need a trustworthy inventory; otherwise the result must explicitly remain unknown. Preserve the existing Assist exposure boundary and avoid double-counting group/member records.

Evaluate whether model instructions are sufficient to reliably render that structured result or whether count answers need a constrained response path. Do not simply rename a control total “bulbs,” hide a failing case, or broaden entity exposure to obtain a larger number.

## Acceptance evidence

Preserve freshness, aliases, room and house scope, unknown/unavailable states, and follow-up context. Require the physical-bulb fixture to acknowledge missing information, and test overlapping group/member records against a declared counting unit. Re-run the full 12-turn suite before claiming a complete corrected regression. Verify live read-only answers against a contemporaneous, independently counted snapshot. Use bounded sequential inference and declared shared-service activity; no CPU burners or wide test loops in dev-env.
