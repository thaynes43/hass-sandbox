---
name: plan-decomposition-patterns
description: Common multi-agent plan patterns for AppDaemon app + Lovelace card features
type: project
---

## Common decomposition: new AppDaemon app with cards

Standard track split for a new feature with app + cards:
- **Track A**: AppDaemon app (Python) — always first, defines sensor schema contract
- **Track B/C**: Lovelace cards (JS) — can run in parallel after Track A, one agent per card
- **Track D**: Dashboard config (MCP) + version bump — always last

**Why:** Cards depend on the sensor attribute schema finalized by the app. Cards don't overlap files. Dashboard config needs all cards to exist.

## Key references for new apps

- `immich_fetcher_app.py` — best reference for app lifecycle (startup, provisioning, event handling)
- `test_immich_fetcher.py` — best reference for test pattern (mock hassapi, sys.path, provisioner mocking)
- `photo-display-card.js` — best reference for compact display cards
- `immich-fetcher-card.js` — best reference for tabbed/settings UI cards

## Version file location

VERSION is at repo root `/home/thaynes/workspace/hass-sandbox/VERSION`, NOT in `appdaemon/`.

## Config conventions

- `apps-prod.yaml` entries always have `disable: true`
- `apps-dev.yaml` keys end in `_dev`
- Both need `ha_url: !secret ha_url` and `ha_token_env: TOKEN`
- Module path format: `<package>.<module>` (e.g., `school_lunch_app.school_lunch_app`)

## Provider month indexing pitfall

`school_menu` provider returns 0-indexed months (0=Jan). Use `MenuMonth.display_month` for 1-indexed display. This must be called out explicitly in plans to avoid bugs.
