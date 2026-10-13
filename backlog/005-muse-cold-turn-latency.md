# 005 — Reduce Muse's cold-turn latency without losing house tools

**Status:** Proposed
**Size:** Medium (tool/context design and native voice validation)
**Raised:** 2026-10-13, Phone Assist incident and hass-sandbox#246

## Problem and evidence

Phone Assist's first “How many lights are on?” answer took 37.89 seconds, with visible text after 31.70 seconds. It returned a correct exposed-control count. Its first model request evaluated 17,690 prompt tokens in 14.814 seconds. A second, overlapping conversation evaluated another 17,685 tokens and slowed the first answer's final generation. The warm repeat took 7.46 seconds. GetLiveContext itself completed in approximately 0.3 milliseconds.

The reconstructed Phone profile advertises 49 tools. Its 31 exposed script schemas occupy 37,386 of 47,174 tool-schema JSON characters. Characters are a schema-size diagnostic, not a measured token attribution. Rumpus advertises 67 tools, including Watch history. The direct fixture benchmark retains those schemas but replaces native inventory; its timing must not be reported as microphone-to-speaker latency.

Private traces, profiles, untouched baseline and reviewed results are under `/home/dev/.local/state/hardware-maintenance/muse-light-count-vetting-20261013/`. The public runner and synthetic cases are in `scripts/voice-bench/`. No credentials or live inventory belong in this backlog.

## Current deployment and decisions to evaluate

Muse runs on Card C in talosw04's eGPU dock. Phone uses house Assist tools; Rumpus uses house Assist and Watch history, with local Parakeet and Kokoro. They share the existing two-slot model server and keep separate conversation histories. The prompt correction in #246 addresses freshness, clarification and count scope; it does not establish an acceptable latency budget or prove the physical voice box.

Evaluate compact script argument schemas, room-specific tool/context selection, and cache reuse under two independently active clients. Preserve Tom's existing scripts, exposure boundary, room behavior and Watch history. Choose the approach using measured native requests; merely keeping the model warm cannot guarantee that a different room's prompt is cached. Do not switch models, change GPU placement or drop capabilities as an implicit fix.

Native HA's chat accepts another turn before the first intent finishes. In the incident, `?` began a new conversation before the first conversation ID arrived. Investigate serialization or a visible busy state separately if native traces confirm that overlap remains a problem. The screenshot alone does not prove a lost final response.

## Acceptance evidence

Agree on latency targets before selecting a design. Measure fresh and cached Phone/Rumpus requests, alternating clients and one bounded overlap. Retain per-stage timing, exact tool calls and answers. Keep the light-count accuracy suite passing, then prove transcription, Muse routing and audible playback on the Rumpus device. Run inference sequentially by default; declare shared-service activity before bounded concurrency tests. Never run CPU burners or wide test loops in the dev-env pod.
