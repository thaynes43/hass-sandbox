# Finish work in flight

> **Applies to:** every session, every repo, every task. (Tom, 2026-09-09)

## The rule

**Your session is the unit of delivery.** Whatever you started, found, or were told
about is *done* — merged **and deployed** — before your final message, or it is
parked somewhere durable that does not depend on a human reading that message.
There is no third state.

## Why

- Sessions are short-lived and die without warning: a dev-env pod roll, a context
  wipe, a closed tab, a quota wall. The turn you are writing may be the last one.
- Worktrees are pruned. Uncommitted or unmerged work in `~/work/<task>` is gone at
  the next `agent-run prune`.
- **Nobody reads a closing "here is what I left open" message.** The owner does not
  triage chat transcripts or PR comments for hand-offs. A "worth a follow-up" line
  is, in practice, a deletion.
- The next agent starts cold. Everything you chose not to finish is rediscovered
  from scratch, paid for twice, and often shipped again with the bug you already
  knew about.

On 2026-09-09 a session ended with five items in exactly that state: two review
findings merged "as polish", a stale deploy instruction the agent had personally
tripped over, a defect it had fixed in one checker and then *described* as still
live in five siblings, and a card guard it called "worth a follow-up if you want".
Every one of them cost a second session to redo. This rule exists so that stops.

## What "finished" means

1. **A defect you find is yours.** Especially one you introduced, one a reviewer
   found, or the same bug in a sibling file, app, or checker. Fixing one instance
   and narrating the other four is not a fix.
2. **Review findings get fixed, not filed.** A finding you will not fix needs a
   concrete, stated reason on the PR why it is wrong or does not apply. "Polish",
   "not blocking", "deploy cost", "already had N rounds" are not reasons. A review
   or audit job that failed on its own turn budget is not a finding — but it is
   also not a reason to skip the findings it *did* post.
3. **Merged is not done.** If the repo has a deploy chain after merge — image tag
   bump in haynes-ops, Flux reconcile, card copy into the HA pod plus `?v=N`
   bump, helper re-provisioning — finish the chain and verify the running system,
   in the same session.
4. **Stale instructions you followed are in scope.** If a rule, playbook, README,
   or `CLAUDE.md` step turned out to be wrong when you followed it, fix the
   instruction in the same PR or the same session.
5. **Scope you discovered is still scope.** Finding that a task is bigger than it
   looked is normal. It is a reason to do more, not a reason to stop at the part
   you had planned.

## Tripwire phrases

If any of these appear in your commit message, PR body, PR comment, or final
reply, you are not finished:

> worth a follow-up · out of scope for this PR · leaving this open · a future PR
> could · known issue · I'll leave that for · nice to have · low priority ·
> flagging for later · not in this change · someone should · if you want

Rewrite the sentence as either *"done: …"* or a durable deferral (below).

## The only acceptable deferral

Work may be handed off **only** when it genuinely needs something you cannot
produce — a design decision from the owner, or a multi-session plan — and only in
a form that survives you:

- **A `backlog/NNN-<slug>.md` entry** in this repo (format: `backlog/README.md`),
  with enough context to be picked up cold: what, where (`file:line`), why it
  matters, what you already ruled out.
- **A GitHub issue** with the same content, when the work belongs to another repo.
- **An `AskUserQuestion`** at the moment the decision is needed — if it is a
  *decision*, not a task.

A chat message, a PR comment, a "Not verified" line, a memory file, or a note in
your final reply is **not** a deferral. Those exist to *report* what happened; they
do not transfer work.

## Before your final message

- Every PR you opened: `MERGED`, deploy chain done, running system verified.
- Every review finding: fixed, or a concrete reason on the PR.
- Every "I'll …" or "should …" you wrote earlier in the session: done.
- Every stale doc you tripped over: corrected.
- Your final message contains none of the tripwire phrases.

If the honest answer to any of these is "no", you are not at your final message
yet.
