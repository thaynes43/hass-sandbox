# Custom Lovelace card rules

When working on `**/*.js` card files, read `.agents/rules/custom-card-guidelines.md` for full detail.

## Critical patterns

**Communication**: Use `hass.callService("script", "<app>_relay", { command, payload })` only. Never `fire_event` — fails silently for non-admin users.

**Touch/click deduplication**: Use a single delegated listener on `shadowRoot`. `touchend` handles touch → calls `e.preventDefault()` and dispatches action → sets a 400ms `touchActive` flag. `click` handler gates on `if (touchActive) return`. Without this, actions fire twice on touch devices.

**Critical Android/UniFi rule**: Never call `e.preventDefault()` on `touchend` when the target is `<input>`, `<select>`, or `<textarea>`. Android webviews won't open keyboard or dropdown.

**Re-render guard is for edits, not focus**: track an edit in progress — a delegated `input`/`keydown` from a text-entry control (`INPUT` other than checkbox/radio/button/submit/reset, `TEXTAREA`, `SELECT`) sets `this._editing`; `change`, `focusout` and `disconnectedCallback` clear it — and skip *every* re-render path (`set hass` **and** any refresh timer) while it is set. Do not gate on `shadowRoot.activeElement`: a merely focused control (a tapped checkbox, or a number box someone clicked into and left) would block renders until focus moves, which froze the health-check detail card twice on 2026-09-09. An un-gated re-render replaces the node mid-typing and, in Chromium, commits the half-typed value.

**Card skeleton**: Extend `HTMLElement`, `attachShadow({ mode: "open" })`, implement `setConfig(config)` and `set hass(hass)`, register with `customElements.define()`.

**Cache busting**: After updating card JS, bump `?v=N` on the Lovelace resource URL.

**Target platforms**: Desktop (Chrome/Firefox/Edge), iOS Companion App, Android/UniFi Connect Display — all must work.
