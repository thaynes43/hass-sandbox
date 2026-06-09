# Multi-Agent Plans: Planner → Implementation → Validation

### When to use this

Use this playbook when a task is large enough that you want to split it across one or more **separate agent sessions** (Implementation Agents) and then have another session (Validation Agent) review the result. The Planner Agent writes the plan file and produces short prompts that the **user** copies into fresh agent sessions.

**Key concept:** Each Implementation Agent and the Validation Agent run in their own agent session. They are **not** subagents or background tasks — they are full agent sessions started by the user pasting the prompt you provide.

### Critical rule: all actionable detail lives in the plan file

Prompts are only launchers. Keep them short and point them at the exact plan file path. The plan file itself must hold the architecture context, file ownership, test commands, validation checklist, and re-prompt format. If detail remains only in chat, the next agent will miss requirements.

---

### When NOT to use this

- The task is small enough for one agent to implement and validate in a single session (< 5 files changed, < 2 hours of work). Just do the work directly.
- The user asked for a quick fix or triage, not a full plan.

---

### Critical rules

1. **All detail lives in the plan file, not the prompt.** Implementation and Validation prompts must be short (< 20 lines). They point to the plan file path. All architecture context, instructions, signatures, test tables, and checklists go inside the plan file.

2. **Prompts are for the user to paste.** When you produce prompts, the user will copy-paste them into new agent sessions. Write them accordingly — self-contained, unambiguous, with the exact plan file path.

3. **Plan for parallelism first.** Before writing prompts, analyze which todos have dependencies and which are independent. Group independent work into parallel tracks. Two agents can work simultaneously only if they don't edit the same files.

4. **Validation must produce a copy-pasteable repair prompt.** When validation fails, the Validation Agent must output a fenced `text` block the user can paste directly into a new Implementation Agent session.

5. **Planner performs the final review.** After the user reports that implementation and validation are complete, the original Planner Agent should do a final code-review/test pass and fix any issues the other agents missed.

---

### Workflow

**Step 1 — Planner creates the plan file**

The Planner Agent explores the codebase, discusses requirements with the user, and writes a plan file. The plan file must include:

- **Architecture overview** (mermaid diagram where helpful)
- **Constraints** (what must never happen — deploy, prod changes, etc.)
- **Implementation detail** — one section per change area, with:
  - Which file, method/function, relevant line ranges
  - Exact method signatures, field names, data shapes
  - Code snippets for non-obvious patterns
  - Logging requirements (level, message format)
- **Test case tables** — test name → what it verifies
- **Parallelism analysis** — which todos can run concurrently
- **Validation checklist** — flat, verifiable items grouped by area
- **Agent prompts** — in fenced `text` code blocks
- **Implementation Agent re-prompt template** — a fenced `text` block the Validation Agent can reuse on FAIL

**Step 2 — Planner analyzes parallelism**

Before writing prompts, the Planner must:

1. List every file that will be created or modified
2. Identify **conflict zones** — files touched by more than one todo
3. Group todos into **tracks** where each track's files don't overlap with other tracks
4. Mark dependencies: todo B depends on todo A if B reads output A creates

Write this analysis into the plan as a "Parallelism analysis" section:

```markdown
## Parallelism analysis

| Todo | Files touched | Dependencies | Track |
|------|---------------|-------------|-------|
| manager-cooldown | manager.py | none | A |
| viewer-provision | viewer_app.py | none | A |
| tests-manager | tests/test_cooldown.py (new) | manager-cooldown | A |
| dashboard-update | (MCP only) | viewer-provision | B |

Track A: Python code + tests (sequential within track — shared files)
Track B: MCP + docs (no file conflicts with A — can run in parallel)
```

**Decision matrix:**

| Situation | Agents |
|-----------|--------|
| All todos in one track, or total work < 5 files | **1 Implementation Agent** |
| 2+ independent tracks with no file overlap | **N Implementation Agents in parallel** (one per track) |
| Any number of tracks | **1 Validation Agent** (always reviews full checklist) |

**Step 3 — Planner writes agent prompts**

Every plan needs these prompt sections in fenced `text` code blocks:

**Single Implementation Agent:**

```text
You are an Implementation Agent. Your task is fully described in the plan file at:

  <plan_file_path>

Read the full plan file before doing anything else. It contains architecture context,
detailed implementation instructions, test case tables, and a validation checklist.

Also read these rule files before making any changes:
- .agents/rules/<rule-1>.md
- .agents/rules/<rule-2>.md

Work through all todos in the plan in order. After completing all code changes,
run the full test suite and fix any failures before finishing:

  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"

DO NOT manually deploy to production. All changes stay in the dev environment until merged to main.
```

**Validation Agent** (always one, always last, read-only):

```text
You are a Validation Agent. Review the implementation described in the plan file at:

  <plan_file_path>

Read the full plan file — the "Validation checklist" section lists every requirement to verify.

Also read these rule files:
- .agents/rules/<rule-1>.md

DO NOT modify any files. Your job is to READ and VERIFY only.

Verify each checklist item by reading the relevant source files. Run the full test suite
and include the result in your report:

  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"

Output a PASS or FAIL verdict.

If FAIL, list every failing checklist item with:
  - File path and method/line where the issue is
  - What is wrong or missing
  - What the fix should be

Then produce a copy-pasteable prompt for the Implementation Agent in a fenced
\`\`\`text\`\`\` block.
```

**Step 4 — User executes the plan**

1. Opens a new agent session for each Implementation Agent prompt and pastes it
2. If parallel: runs them simultaneously in separate sessions
3. Waits for all Implementation Agents to finish
4. Opens a new agent session (ideally in Ask/read-only mode) for the Validation Agent prompt
5. If Validation returns FAIL: pastes the re-prompt into a new Implementation Agent session
6. Repeats until PASS

**Step 5 — Planner performs final review**

After Validation returns PASS:
1. Re-read the implemented files and compare to the accepted plan
2. Run the full test suite again
3. Code-review pass for missed bugs, weak validation, stale config/docs drift, leftover artifacts
4. Fix any remaining issues directly

---

### Implementation Agent re-prompt template

When validation fails, the Validation Agent must produce a prompt like this in a `\`\`\`text\`\`\`` block:

```text
You are Implementation Agent <A/B/...> for <plan name>.

Validation Agent has completed a read-only validation pass. The following defects
were found that you must fix.

DEFECT 1

File: <path>

<What is wrong. What the fix should be.>

REQUIRED FIX

1. <First action>
2. <Second action>

Read the plan file and rules before making changes. Do not manually deploy to production.
Run the full test suite after your changes and confirm it passes:

  wsl bash -c "cd /mnt/d/labspace/hass-sandbox && source .venv-wsl/bin/activate && cd appdaemon && python -m pytest tests/ -v --tb=short"
```

---

### Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| Implementation Agent ignores half the requirements | Detail was in chat, not the plan file | Move ALL detail into the plan file before producing prompts |
| Validation Agent gives a vague "PASS" without checking | Checklist not specific enough | Each checklist item must be verifiable by reading one file/method |
| Parallel agents conflict on the same file | Parallelism analysis missing or wrong | Map every file to exactly one track; shared files go in one track |
| Validation Agent modifies code | Prompt didn't say read-only | Always include "DO NOT modify any files" in the Validation prompt |
| Agent tries to deploy to production | Not stated in prompt | Always include "DO NOT manually deploy to production" |
| Implementation Agent skips the test suite | Test command not in prompt | Always include the exact test command |
| Validation re-prompt is hard to act on | FAIL output is vague or not copy-pasteable | Validation Agent must produce a fenced `text` block with complete details |

---

### Planner checklist (after creating a plan)

- [ ] Plan file created successfully with concrete path
- [ ] Every todo has a clear owner track in the parallelism analysis
- [ ] No file appears in more than one track (unless read-only access)
- [ ] Both prompts are in fenced `text` code blocks
- [ ] Both prompts reference the exact plan file path
- [ ] Both prompts list the relevant rule files
- [ ] Both prompts include the test suite command
- [ ] Both prompts include the no-deploy constraint
- [ ] Validation prompt says "DO NOT modify any files"
- [ ] Validation checklist covers every requirement including edge cases
- [ ] Plan includes the final planner review step
