# Finish work in flight

Read `.agents/rules/finish-in-flight-work.md` — it applies to every task in every session.

## Key points

- Your session is the unit of delivery: everything you start or find is merged **and deployed** before your final message. Sessions die mid-turn, worktrees are pruned, nobody reads a closing hand-off message.
- A defect you find is yours; review findings get fixed or concretely refuted on the PR; merged is not done when the repo has a deploy chain (`git-workflow.md` step 6).
- Automated AI reviews and audits are optional and advisory. Unavailable, failed, skipped, or pending AI jobs do not block delivery or need owner approval. Read existing findings and complete an independent native agent review when AI review is unavailable; required tests, lint, builds, and security checks still apply (`.agents/rules/git-workflow.md`).
- "Worth a follow-up" / "out of scope here" / "if you want" mean you are not finished. The only durable deferral is a `backlog/NNN-*.md` entry or a GitHub issue, for work that genuinely needs a design decision.
