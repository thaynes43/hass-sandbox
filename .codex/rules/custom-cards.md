# Custom card rules for Codex

When working on Lovelace card JavaScript, read:

- `.cursor/rules/custom-card-guidelines.mdc`

## Quick reminders

- Use relay scripts via `hass.callService(...)`, not `fire_event`.
- Support both touch and click without double-firing.
- Never `preventDefault()` on `input`, `select`, or `textarea` touch events.
- Guard against rerendering while an input has focus.
- Bump the Lovelace resource version after card JS changes.
