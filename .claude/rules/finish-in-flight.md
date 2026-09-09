# Finish work in flight

Read `.agents/rules/finish-in-flight-work.md` — it applies to every task in every session.

## Key points

- Your session may be killed mid-turn; worktrees are pruned; nobody reads a closing "here's what I left open" message. Unmerged, undeployed work is lost and the next agent rediscovers it from scratch.
- A defect you find is yours — including the same bug in sibling files, and the stale instruction you followed to get there.
- Review findings get fixed, or a concrete reason on the PR. "Polish, merging anyway" is not a reason.
- Merged ≠ done: finish the deploy chain (haynes-ops tag bump, Flux reconcile, card copy + `?v=N` bump) and verify the running system.
- Tripwire phrases — "worth a follow-up", "out of scope here", "leaving open", "a future PR", "if you want" — mean you are not finished. The only durable deferral is a `backlog/NNN-*.md` entry or a GitHub issue with cold-start context, and only for work that genuinely needs a design decision.
