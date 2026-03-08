# Home Assistant helpers: create / update / delete via MCP

### When to use this

Use the MCP tools to manage helpers so **entity IDs exist immediately** and can be referenced safely in `cards/**`, `automations/**`, and `scripts/**`. We do not maintain YAML files for helpers in the repo.

### Critical rule: pass arguments as proper JSON

All tool parameters must be valid JSON objects/values. Do not pass Python dict literals or mangled strings — the MCP server will reject them with `Invalid JSON`.

---

## Workflow: find & verify helpers

Use this before creating a helper (to avoid duplicates), when diagnosing issues, or when validating that a provisioner-created entity actually exists.

**Option A — Search by name / keyword (1 call)**

Returns all matching entity IDs, friendly names, current state, and match scores. Works across all helper types at once. Best when you know part of the name.

```json
{
  "tool": "ha_search_entities",
  "arguments": {
    "query": "detection summary timing",
    "domain_filter": "input_text",
    "limit": 10
  }
}
```

The `state` field in the response is the **current live value** of the helper — no separate verification call needed. Omit `domain_filter` to search across all domains at once.

**Option B — List all helpers of a type (1 call)**

Returns every stored helper of that type with its full configuration. Best for an exhaustive inventory.

```json
{
  "tool": "ha_config_list_helpers",
  "arguments": {
    "helper_type": "input_text"
  }
}
```

**Option C — Check current state of a known entity (1 call)**

Use when you already know the exact `entity_id` and want its current state or attributes.

```json
{
  "tool": "ha_get_state",
  "arguments": {
    "entity_id": "input_text.bulkhead_detection_summary_timing"
  }
}
```

> **Note on AppDaemon-provisioned helpers**: helpers created via `HAProvisioner` at app startup may not appear in AppDaemon's local state cache immediately. The entities are created via HA REST API; AppDaemon learns about them via WebSocket `state_changed` events which arrive within seconds. If `ha_search_entities` or `ha_get_state` return the entity, it exists in HA. The AppDaemon "entity not found in namespace" log warning is transient and resolves automatically.

---

## Workflow: create a new helper

**Step 1 — Discovery (1 call, optional)**

Only needed if you are unsure whether the helper exists. Skip if you are certain it is new.

```json
{
  "tool": "ha_search_entities",
  "arguments": {
    "query": "<name or keyword>",
    "domain_filter": "input_text",
    "limit": 10
  }
}
```

**Step 2 — Create (1 call)**

Use `ha_config_set_helper`. Omit `helper_id` to create; provide it to update the config.

```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_text",
    "name": "Wall Display Entry Locks Status",
    "icon": "mdi:shield-lock"
  }
}
```

---

## Workflow: update a helper's live value (1 call)

**`input_text` — set value:**

```json
{
  "tool": "ha_call_service",
  "arguments": {
    "domain": "input_text",
    "service": "set_value",
    "entity_id": "input_text.wall_display_entry_locks_status",
    "data": { "value": "All OK" }
  }
}
```

**`input_boolean` — turn on / off / toggle:**

```json
{
  "tool": "ha_call_service",
  "arguments": {
    "domain": "input_boolean",
    "service": "turn_on",
    "entity_id": "input_boolean.upstairs_primary_bathroom_use_shower_delay"
  }
}
```

**`input_select` — set option:**

```json
{
  "tool": "ha_call_service",
  "arguments": {
    "domain": "input_select",
    "service": "select_option",
    "entity_id": "input_select.downstairs_bathroom_vanity_mmwave_normal_mode",
    "data": { "option": "Vacancy" }
  }
}
```

---

## Workflow: update a helper's configuration (1 call)

Provide `helper_id` (the slug after the domain in entity_id). No discovery call needed if you know the entity_id.

```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_text",
    "helper_id": "wall_display_entry_locks_status",
    "name": "Wall Display Entry Locks Status",
    "icon": "mdi:shield-lock-outline"
  }
}
```

---

## Workflow: delete a helper (1 call)

```json
{
  "tool": "ha_config_remove_helper",
  "arguments": {
    "helper_type": "input_text",
    "helper_id": "wall_display_entry_locks_status"
  }
}
```

**Warning**: deleting a helper that is referenced in automations, scripts, or cards will break those references. Confirm nothing depends on it before deleting.

---

## Helper types and known-good examples

### `input_boolean`
```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_boolean",
    "name": "Upstairs Primary Bathroom Use Shower Delay",
    "icon": "mdi:timer-outline",
    "initial": false
  }
}
```

### `input_text`
```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_text",
    "name": "Wall Display Entry Locks Status",
    "icon": "mdi:shield-lock",
    "initial": ""
  }
}
```

### `input_select`
```json
{
  "tool": "ha_config_set_helper",
  "arguments": {
    "helper_type": "input_select",
    "name": "Downstairs Bathroom Vanity mmWave Normal Mode",
    "icon": "mdi:motion-sensor",
    "options": [
      "Disabled",
      "Occupancy (default)",
      "Vacancy",
      "Wasteful Occupancy",
      "Mirrored Occupancy",
      "Mirrored Vacancy",
      "Mirrored Wasteful Occupancy"
    ],
    "initial": "Occupancy (default)"
  }
}
```

---

## Inovelli mmWave normal-mode helpers (special case)

- **Entity id convention**: `input_select.<zone>_mmwave_normal_mode`
- **Options**: must match `select.*_mmwavecontrolwireddevice` options.

After creating, also update:

- **Cards**: pass `normal_mode_input_select: input_select.<zone>_mmwave_normal_mode` (not hardcoded `normal_mode:`).
- **Sync automation**: add to `automations/inovelli_mmwave_normal_mode_sync_input_select.yaml`.
- **Clear-hold registry**: add `normal_mode_input_select` in `scripts/inovelli/entities-defined/clear_hold_on_all_inovelli_presence_controlled_switches.yaml`.

---

## After creating or deleting (don't forget)

- **Scope communication**: follow `ha-change-scope-communication.mdc` — use **Repo YAML & Live HA Updated** and list the entity_id under **Live HA entities updated**.
- **No repo YAML for helpers**: we do not maintain helper definition YAML files. The MCP-created entity is the source of truth.
