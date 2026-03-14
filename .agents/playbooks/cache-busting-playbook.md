# Cache Busting Playbook

## Purpose

Use this playbook when a Home Assistant Lovelace JavaScript resource under `/local/...` has changed and browsers or companion apps are still serving a cached copy.

This covers the MCP workflow for bumping `?v=N` on a dashboard resource.

## When to use it

Use this after updating a custom card such as:

- `/local/vestaboard/vestaboard-configuration-card.js?v=13`
- `/local/photo-frame/photo-frame-viewer-card.js?v=6`
- `/local/dashboard-notify/dashboard-notify-card.js?v=18`

Do not use this for normal sensor/media URLs unless the goal is specifically to version a Lovelace JS resource.

## MCP workflow

### 1. List dashboard resources

Use:

- `ha_config_list_dashboard_resources`

Goal:

- find the existing resource entry
- capture its `resource_id`
- capture the current URL and version

Example result shape:

```json
{
  "id": "046e5c049c0a433cad330a0b2225c806",
  "url": "/local/vestaboard/vestaboard-configuration-card.js?v=13",
  "type": "module"
}
```

### 2. Increment the version number

Take the current URL and bump the integer in `?v=N`.

Examples:

- `?v=13` -> `?v=14`
- `?v=6` -> `?v=7`

If there is no `?v=` yet, add one:

- `/local/my-card.js` -> `/local/my-card.js?v=1`

### 3. Update the resource

Use:

- `ha_config_set_dashboard_resource`

Required fields:

- `resource_id`
- `url`
- `resource_type`

Example:

```json
{
  "resource_id": "046e5c049c0a433cad330a0b2225c806",
  "url": "/local/vestaboard/vestaboard-configuration-card.js?v=14",
  "resource_type": "module"
}
```

## Recommended procedure

1. Update the `.js` file on the Home Assistant instance.
2. Call `ha_config_list_dashboard_resources`.
3. Find the matching `/local/...js` resource.
4. Increment `?v=N`.
5. Call `ha_config_set_dashboard_resource`.
6. Reload the dashboard on the target device.

## Verification

After updating the resource:

- confirm the MCP response shows the new URL
- reload the dashboard or app view
- if the device is stubborn, do a hard refresh or reopen the companion app view

Optional follow-up:

- run `ha_config_list_dashboard_resources` again and verify the stored URL matches the new version

## Optional: update the JS file directly in the pod

Only do this when the user explicitly asks for it.

Reason:

- editing files inside the running `home-assistant` pod is operationally sensitive
- it requires cluster access
- some users want agents to prepare code locally but not deploy it

### Typical target location

For Lovelace resources served from `/local/...`, the file is usually inside the Home Assistant pod under:

- `/config/www/...`

Example:

- `/local/vestaboard/vestaboard-configuration-card.js`
- `/config/www/vestaboard/vestaboard-configuration-card.js`

### Read-only inspection workflow

Use this when you only need to confirm what file is currently running in Home Assistant.

1. Identify the `home-assistant` pod in the `home-automation` namespace.
2. Verify the target file exists in `/config/www/...`.
3. Read or grep the deployed file to compare it with the local workspace copy.

Example:

```bash
kubectl -n home-automation get pods
kubectl -n home-automation exec <home-assistant-pod> -- \
  sh -lc 'ls -l /config/www/vestaboard/vestaboard-configuration-card.js'
kubectl -n home-automation exec <home-assistant-pod> -- \
  sh -lc 'grep -n "getCardSize" /config/www/vestaboard/vestaboard-configuration-card.js'
```

This is safe to do without deployment and is often the fastest way to confirm whether a `?v=` bump is pointing at the expected file contents.

### Safe deployment sequence

1. Confirm the user explicitly wants the agent to update the pod file.
2. Identify the `home-assistant` pod in the `home-automation` namespace.
3. Verify the target file path exists in `/config/www/...`.
4. Copy or write the updated local `.js` file into the pod.
5. Verify the deployed file contents match the expected change.
6. Bump the Lovelace resource `?v=N`.

### Example kubectl workflow

List pods:

```bash
kubectl -n home-automation get pods
```

Verify the target file exists:

```bash
kubectl -n home-automation exec <home-assistant-pod> -- \
  sh -lc 'ls -l /config/www/vestaboard/vestaboard-configuration-card.js'
```

Copy the local file into the pod:

```bash
kubectl -n home-automation cp \
  appdaemon/apps/vestaboard_configuration_app/vestaboard-configuration-card.js \
  <home-assistant-pod>:/config/www/vestaboard/vestaboard-configuration-card.js
```

Verify the deployed file includes the expected text:

```bash
kubectl -n home-automation exec <home-assistant-pod> -- \
  sh -lc 'grep -n "vestaboard-configuration-card" /config/www/vestaboard/vestaboard-configuration-card.js'
```

### Agent rule

If the user did not explicitly request deployment into the pod:

- do not run `kubectl cp`
- do not overwrite files in `/config/www/...`
- stop after preparing the local file and bumping or recommending the `?v=` change as appropriate

If the user did explicitly request pod deployment:

- prefer verifying the destination file before writing
- after copying, verify the deployed contents before updating the resource version

## Notes

- Keep the path identical. Only change the version query unless you are intentionally moving the file.
- Preserve the original `resource_type`, usually `module`.
- Companion apps and kiosk devices cache aggressively. Incrementing `?v=` is often required even when the file contents definitely changed.
- This is separate from media/image cache busting. Use this playbook for Lovelace JS resources only.
