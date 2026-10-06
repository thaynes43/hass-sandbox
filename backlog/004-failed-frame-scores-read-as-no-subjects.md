# 004 — A frame whose scoring call failed reads as "no subjects"

**Status:** Proposed
**Size:** Medium (selection algorithm change plus tests; no infra)
**Raised:** 2026-10-06, Claude review of hass-sandbox#232 (Ollama request gate, AppDaemon 1.25.1)

## Problem

`detection_summary_app/manager.py` → `_build_bundle` → `score_one` catches any
`ExternalDataGenError` from the multimodal provider, logs `data gen failed ...`
at WARNING, and scores the frame from `{}`. `normalize_score_data({})` gives
`person_score=0.0` and zero counts, which is exactly what a frame showing
nobody scores.

Adaptive selection (`selection.py` → `adaptive_select_and_score`) uses that
predicate (`person_score <= no_people_threshold and animal_count <= 0`) to:

- set the cutoff when it walks forward from the best frame (first "no people"
  frame), then binary-search the boundary;
- confirm the cutoff with a lookahead, which needs spare budget.

So a failed call can:

1. move the cutoff before the subjects actually leave;
2. lose the real peak frame, so `best_min_person_score` / `should_publish_bundle`
   suppress the bundle. The only trace is the `data gen failed` warning, and
   no notification goes out.

## Current state

This predates 1.25.1. Any provider failure has always done this: an HTTP error,
a timeout, empty content, or unparseable JSON. 1.25.1 adds one more cause: a
call that waits more than `queue_wait_s` (300 s) for the process-wide Ollama
slot raises `OllamaQueueTimeout`. That is load-correlated: it happens when many
cameras run at once or Ollama is slow. Overall, 1.25.1 drops fewer frames than
1.25.0 did. Before, the queue lived inside Ollama, and a request had 300 s
*total* (queue plus inference) on its HTTP timeout. Now it has up to 300 s
queued, then its own full 300 s HTTP timeout.

## Candidate approaches

- Carry a `failed` flag on `ScoreResult` (or return `None` from `score_one`).
  Selection would then treat a failed frame as unknown, never as "no people".
  Skip it when walking forward or binary-searching, and exclude it from best/peak
  selection and the population consensus.
- Retry a failed frame once if budget remains, or try a neighbouring frame.
- If every scored frame failed, skip publishing with an explicit "scoring
  unavailable" log line instead of the "no subjects" one, and do not reset the
  cooldown as though the run were empty.

## Open questions

- Should a run whose frames mostly failed publish anyway, using the best frame
  by capture order (a notification with no AI summary), or stay silent?
- Does an all-failed run count toward the cooldown/backoff?
