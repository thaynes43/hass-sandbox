# Playbook authoring guide

### What is a playbook?

A playbook is a markdown document that teaches AI agents **how to complete a specific task** using MCP tools or repo conventions in as few steps as possible. Playbooks live in `.agents/playbooks/` (shared/canonical) and optionally have wrapper files in `.cursor/playbooks/` (with Cursor-specific frontmatter) for Cursor auto-loading.

### When to create a playbook

Create a playbook when:

- A task involves **MCP tool calls** that agents frequently get wrong (serialization, parameter format, ordering).
- A task has a **repeatable workflow** (discovery → create → verify) that benefits from a template.
- Previous agents have **failed or looped** on the task, and you've found the correct approach.

Do **not** create a playbook for:

- One-off tasks that won't recur.
- Simple tool calls that agents already handle reliably.
- Domain knowledge that belongs in a general rules file instead.

### Playbook structure

Every playbook follows this skeleton:

```markdown
# <Domain>: <task description>

### When to use this
<1–2 sentences on the trigger for using this playbook>

### Critical rule: <the #1 thing agents get wrong>
<Concise explanation of the pitfall and the fix>

### Workflow: <task name>
**Step 1 — <Phase> (N calls)**
<What to do, with example tool call>

**Step 2 — <Phase> (N calls)**
...

### Known-good example: <simple case>
<Complete tool call JSON that has been tested and works>

### Known-good example: <complex case>
<Complete tool call JSON that has been tested and works>

### Common pitfalls
| Symptom | Cause | Fix |
|---------|-------|-----|

### After creating (don't forget)
<Checklist of follow-up tasks>
```

### Authoring principles

1. **Test before documenting.** Only include "known-good" examples that you have actually executed successfully against the MCP server. Never guess at tool call shapes.

2. **Minimize calls.** Each workflow step should state the expected number of MCP calls. The goal is the fewest round-trips possible (typically: 1 discovery + 1 create + 1 optional verify = 3 max).

3. **Show the pitfall first.** The "Critical rule" section is the most important part — it addresses whatever caused previous agents to fail. Put it early and make it unmissable.

4. **Use JSON for examples.** All tool call examples must be valid JSON objects. Never use Python dict literals, YAML, or pseudo-code — agents will copy the format they see.

5. **Keep it scannable.** Use tables for pitfalls, code blocks for examples, and bold for key terms. Agents skim; dense paragraphs get lost.

6. **One playbook per task domain.** Don't combine helpers, automations, and scripts into one mega-playbook. Keep them focused so agents only load what they need.

### File conventions

- **Canonical location**: `.agents/playbooks/<name>.md` — plain markdown, no frontmatter
- **Cursor wrapper**: `.cursor/playbooks/<name>-playbook.mdc` — add Cursor frontmatter (`globs`, `alwaysApply: false`) pointing to the shared file
- **Naming**: `ha-<domain>.md` for Home Assistant tasks, `appdaemon-<domain>.md` for AppDaemon tasks
- **Reference in**: `.claude/CLAUDE.md` playbook table and `.cursor/playbooks/playbook-authoring-guide.mdc` existing playbooks table

### Existing playbooks

| Playbook | Purpose |
|----------|---------|
| `ha-helpers.md` | Create/update HA helpers (input_text, input_boolean, input_select) via MCP |
| `ha-automations-scripts.md` | Create/update HA automations and scripts via MCP |
| `ha-provisioner.md` | Integrate `ha_provisioner` into an AppDaemon app for self-provisioning |
| `occupancy-based-lighting.md` | Add/update occupancy-based lighting zones: helpers, automations, holds, cards |
| `appdaemon-deploy.md` | Deploy AppDaemon apps from dev to production |
| `security-audit.md` | Audit AppDaemon apps for security policy violations |
| `ha-dashboard.md` | Edit HA dashboard views/cards via MCP (config_hash, python_transform, pitfalls) |
| `detection-app.md` | Build/extend the detection summary AppDaemon app |
| `multi-agent-plan.md` | Structure multi-agent plans (Planner → Implementation → Validation) |
| `appdaemon-ai-provider.md` | Add a new AI provider to `providers/ai_providers/` |
| `playbook-authoring-guide.md` | This file — how to write new playbooks |
