# Backlog

Long-running improvement efforts for hass-sandbox that are too large for a single
session and not yet scheduled. Each item is one markdown file, numbered in the
order it was raised. This is the "V2" planning space: hass-sandbox was written by
hand over a long period, and several areas deserve a proper revamp rather than
incremental patching.

## How this folder works

- One file per item: `NNN-short-slug.md` (zero-padded, next free number).
- Every item starts as **Proposed**. Update the status line in the item file as
  it moves: `Proposed` → `Planned` (has a plan in `.agents/plans/`) → `In
  progress` → `Done` (keep the file; record the outcome).
- Keep items self-contained: problem, current state, candidate approaches, open
  questions. An agent picking one up should not need this README for context.
- When an item graduates to actual work, write a plan in `.agents/plans/`
  (see `.agents/playbooks/multi-agent-plan.md`) and link it from the item.

## Items

| # | Item | Status | Size |
|---|------|--------|------|
| 001 | [Custom card deployment revamp / HACS integration](001-card-deployment-and-hacs-integration.md) | Proposed | Major |
