# Home Assistant automations & scripts: creation via MCP

### When to use this

Use the MCP tools (`ha_config_set_automation`, `ha_config_set_script`) to create or update automations and scripts in Home Assistant so they are **live immediately**. Always keep a mirror YAML file in the repo under `automations/` or `scripts/`.

### Critical rule: `config` must be a proper JSON object

The `config` parameter accepts **either** a JSON object (dict) **or** a JSON string.

**Always pass it as a JSON object (dict), not a string.** This is the #1 cause of failure — previous agents produced mangled output like `[object Object]` or Python dict literals with single quotes, which the MCP server rejects with:

```
Invalid JSON in config: Expecting value: line 1 column 2 (char 1)
```

### JSON serialization rules for HA configs

1. **Jinja templates are just strings.** `{{ }}` and `{% %}` inside a JSON string value are fine — they are not JSON structure.
2. **Single quotes inside Jinja are safe.** JSON uses double quotes for string delimiters, so `"{{ 'locked' if x == 'locking' else 'unlocked' }}"` is valid JSON.
3. **Collapse multi-line Jinja into one line** when passing via MCP. The YAML repo file can use `>-` or `|` blocks, but the MCP `config` value must be a single JSON string (use spaces instead of newlines).
4. **`service` vs `action`**: HA accepts both. The MCP tool may normalize `service` to `action` in the stored config. Either works when creating.

### Workflow: create a new automation

**Step 1 — Discovery (1 call)**

Confirm the automation does not already exist:

```json
{
  "tool": "ha_search_entities",
  "arguments": {
    "query": "<alias or keyword>",
    "domain_filter": "automation",
    "limit": 5
  }
}
```

**Step 2 — Create (1 call)**

Pass the full config as a JSON object to `ha_config_set_automation`. Omit `identifier` to create new.

```json
{
  "tool": "ha_config_set_automation",
  "arguments": {
    "config": {
      "alias": "My Automation Name",
      "description": "What this does.",
      "mode": "single",
      "trigger": [ "..." ],
      "action": [ "..." ]
    }
  }
}
```

For updates, add `identifier`:

```json
{
  "tool": "ha_config_set_automation",
  "arguments": {
    "identifier": "automation.my_automation_name",
    "config": { "...": "..." }
  }
}
```

**Step 3 — Verify (1 call, optional)**

```json
{
  "tool": "ha_config_get_automation",
  "arguments": {
    "identifier": "automation.my_automation_name"
  }
}
```

### Workflow: create a new script

Same pattern using `ha_config_set_script` / `ha_config_get_script`. Scripts require a `script_id` (slug) and `config` with a `sequence` key instead of `trigger`+`action`.

```json
{
  "tool": "ha_config_set_script",
  "arguments": {
    "script_id": "my_script_name",
    "config": {
      "alias": "My Script Name",
      "description": "What this does.",
      "mode": "single",
      "sequence": [ "..." ]
    }
  }
}
```

### Known-good example: simple automation (state trigger, template action)

This automation writes an aggregate lock status string to an `input_text` helper whenever any lock changes state:

```json
{
  "tool": "ha_config_set_automation",
  "arguments": {
    "config": {
      "alias": "Wall Display - Entry Locks Status",
      "description": "Maintain an aggregate status string for the wall display dashboard lock popup button.",
      "mode": "single",
      "trigger": [
        {
          "platform": "state",
          "entity_id": [
            "lock.front_door_lock",
            "lock.side_door_lock",
            "lock.bulkhead_lock",
            "lock.mudroom_door_lock"
          ]
        }
      ],
      "action": [
        {
          "action": "input_text.set_value",
          "target": {"entity_id": "input_text.wall_display_entry_locks_status"},
          "data": {
            "value": "{% set must_lock = ['lock.front_door_lock','lock.side_door_lock','lock.bulkhead_lock'] %} {% set ok_unlock = ['lock.mudroom_door_lock'] %} {% set bad_must_lock = must_lock | reject('is_state','locked') | list %} {% set bad_ok_unlock = ok_unlock | reject('is_state','unlocked') | list %} {% set bad = bad_must_lock + bad_ok_unlock %} {% if bad | length == 0 %} All OK {% else %} {% set ns = namespace(names=[]) %} {% for e in bad %} {% set ns.names = ns.names + [state_attr(e,'friendly_name') or e] %} {% endfor %} {% if ns.names | length == 1 %} Check {{ ns.names[0] }} {% elif ns.names | length == 2 %} Check {{ ns.names[0] }} and {{ ns.names[1] }} {% else %} Check {{ ns.names[0:-1] | join(', ') }}, and {{ ns.names[-1] }} {% endif %} {% endif %}"
          }
        }
      ]
    }
  }
}
```

### Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Invalid JSON in config` | Config passed as Python dict literal or mangled string | Pass as a proper JSON object |
| `[object Object]` in config | Serialization bug in tool call layer | Ensure the config value is a JSON dict, not a stringified placeholder |
| Jinja `{{ }}` causes parse error | Template delimiters confused with JSON | They are just string content; double-check surrounding quotes |
| Multi-line Jinja rejected | Newlines in JSON string without escaping | Collapse to single line with spaces, or use `\n` escapes |

### After creating (don't forget)

- **Repo mirror**: always create/update the corresponding YAML file under `automations/` or `scripts/` so the repo stays in sync.
- **Scope communication**: follow `ha-change-scope-communication.md` — use **Repo YAML & Live HA Updated** and list the entity_id under **Live HA entities updated**.
