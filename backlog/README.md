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
  progress` → `Done` (keep the file; record the outcome). An item that was
  fully delivered in one release, and whose reasoning now lives in the code and
  its README, may instead be deleted with a line below saying where it landed —
  its number is still not reused.
- Keep items self-contained: problem, current state, candidate approaches, open
  questions. An agent picking one up should not need this README for context.
- When an item graduates to actual work, write a plan in `.agents/plans/`
  (see `.agents/playbooks/multi-agent-plan.md`) and link it from the item.

## Items

| # | Item | Status | Size |
|---|------|--------|------|
| 001 | [Custom card deployment revamp / HACS integration](001-card-deployment-and-hacs-integration.md) | Proposed | Major |
| 002 | [Sonos: SonosNet off, soundbars back on Ethernet](002-sonosnet-off-wired-soundbars.md) | Done for the soundbars 2026-09-19 (Movie Room Port still on Wi-Fi) | Small |

Item 003 ("The image prompt's frame notes do not match the frames actually
sent") was implemented in AppDaemon 1.21.1 and its file removed: image providers
now report `capabilities.max_input_images`, the app trims to it before building
the prompt, and the per-frame notes are labelled by send position. Numbers are
not reused — the next item is 004.
