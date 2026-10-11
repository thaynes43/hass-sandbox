/**
 * DOM-shim harness for appdaemon/apps/health_checks/cards/health-check-detail-card.js
 *
 * Run:  node health_check_detail_card_harness.js [path-to-card.js]
 * Out:  one JSON object on stdout, consumed by tests/test_health_check_detail_card.py
 *
 * Why this exists
 * ---------------
 * The card's touch/click/change dispatch has regressed twice.  A native form
 * control fires touchend -> click -> change for a single tap, so a handler that
 * dispatches from more than one of those sends the command twice — and worse,
 * touchend and click carry the *pre*-toggle value of a checkbox, so the second
 * command undoes the first.  The fix (dispatch native controls from "change"
 * only, non-native ones from touchend/click behind a 400 ms guard) is invisible
 * to a unit test of the Python side, so it is pinned here instead.
 *
 * Deliberately dependency-free: node's stdlib only, so CI needs nothing but a
 * node binary.  The shim below is not a browser — it is the smallest DOM the
 * card actually touches (parse innerHTML, walk simple selectors, bubble events
 * through a shadow root).  Where it models browser behaviour that matters to
 * the card, the reasoning is in a comment at that spot.
 */

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const { ShimElement, ShimNode, ShimEvent, shimDocument, customElements } = require("./card_dom_shim");

// ---------------------------------------------------------------------------
// Load the real card into a context wired to the shim
// ---------------------------------------------------------------------------

const CARD_PATH = path.resolve(
  process.argv[2] ||
    path.join(
      __dirname,
      "..",
      "..",
      "apps",
      "health_checks",
      "cards",
      "health-check-detail-card.js"
    )
);

const notes = [];
// Timers are collected so the harness can let the event loop drain instead of
// process.exit()-ing, which can truncate a piped stdout.
const timers = new Set();
// Interval callbacks are kept by id as well, so a scenario can fire a tick on
// demand — the card's refresh timer is 15 s, which no test is going to wait for.
const intervalCallbacks = new Map();

const sandbox = {
  HTMLElement: ShimElement,
  Element: ShimElement,
  Node: ShimNode,
  document: shimDocument,
  customElements,
  console: {
    log: (...a) => notes.push(a.join(" ")),
    warn: (...a) => notes.push(a.join(" ")),
    error: (...a) => notes.push(a.join(" ")),
  },
  setInterval: (fn, ms) => {
    const id = setInterval(fn, ms);
    timers.add(id);
    intervalCallbacks.set(id, fn);
    return id;
  },
  clearInterval: (id) => {
    timers.delete(id);
    intervalCallbacks.delete(id);
    return clearInterval(id);
  },
  setTimeout: (fn, ms) => {
    const id = setTimeout(fn, ms);
    timers.add(id);
    return id;
  },
  clearTimeout: (id) => {
    timers.delete(id);
    return clearTimeout(id);
  },
  Date,
  Math,
  JSON,
  Infinity,
  isNaN,
  isFinite,
  parseInt,
  parseFloat,
  Object,
  Array,
  Set,
  Map,
  String,
  Number,
  Boolean,
  Error,
  Promise,
  RegExp,
  Intl: typeof Intl !== "undefined" ? Intl : undefined,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(CARD_PATH, "utf8"), sandbox, {
  filename: CARD_PATH,
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const NOW = new Date();
const pad = (n) => String(n).padStart(2, "0");
// The card treats the checker section as offline unless the heartbeat is fresh,
// and only renders repair controls when it is online.
const HEARTBEAT =
  `${NOW.getFullYear()}-${pad(NOW.getMonth() + 1)}-${pad(NOW.getDate())} ` +
  `${pad(NOW.getHours())}:${pad(NOW.getMinutes())}:${pad(NOW.getSeconds())}`;
const ISO = NOW.toISOString();

// The shade gateway is the checker whose bounds are not 1/60/1 — the whole
// reason the card cannot hard-code them.
const SHADE_BOUNDS = { min: 15, max: 360, step: 15 };

function checker(overrides) {
  return Object.assign(
    {
      name: "Shade Gateway",
      status: "critical",
      is_dependency: false,
      supports_repair: true,
      muted: false,
      last_check: ISO,
      checks: [
        {
          name: "Gateway",
          status: "critical",
          detail: "disconnected",
          last_changed: ISO,
        },
      ],
      repair_state: {
        status: "idle",
        auto_repair_enabled: false,
        auto_repair_delay_min: 120,
        auto_repair_delay_bounds: SHADE_BOUNDS,
        last_repair_attempt: ISO,
      },
      alert_history: [],
    },
    overrides
  );
}

/** A fresh card with one checker mounted, plus the recorded relay calls. */
function mount(checkerId, checkerData) {
  const calls = [];
  const hass = {
    states: {
      "sensor.health_check_status": {
        state: "critical",
        attributes: { checkers: { [checkerId]: checkerData } },
      },
      "input_datetime.appdaemon_heartbeat": {
        state: HEARTBEAT,
        attributes: {},
      },
    },
    callService: (domain, service, data) => {
      let payload = data && data.payload;
      try {
        payload = JSON.parse(payload);
      } catch (e) {
        /* keep the raw string — a non-JSON payload is itself a finding */
      }
      calls.push({
        domain,
        service,
        command: data && data.command,
        payload,
      });
      return Promise.resolve();
    },
  };

  const card = shimDocument.createElement("health-check-detail-card");
  card.setConfig({});
  shimDocument.body.appendChild(card);
  card.hass = hass;
  // `hass` comes back so a scenario can mutate the state and set it again —
  // the card ignores a set whose snapshot has not moved.
  return { card, calls, root: card.shadowRoot, hass };
}

function teardown(card) {
  card.disconnectedCallback();
}

// ---------------------------------------------------------------------------
// Interaction simulation
// ---------------------------------------------------------------------------

const NATIVE_TAGS = new Set(["INPUT", "SELECT", "TEXTAREA"]);

function fire(el, type, options) {
  const evt = new ShimEvent(type, options);
  el.dispatchEvent(evt);
  return evt;
}

/**
 * A single tap or click, in the order a browser produces it.
 *
 * Touch: touchstart -> touchend -> click, then (for a checkbox) the activation
 * that flips `checked`, then `change`.  Two details are modelled on purpose:
 *
 * 1. A `preventDefault()` on touchend of a native control cancels the
 *    activation entirely — no toggle, no `change`.  That is the Android
 *    wall-display bug the card's isNativeFormEl() guard exists to avoid, so the
 *    harness reproduces it rather than papering over it.
 * 2. For a non-native element the click is dispatched even when touchend was
 *    prevented.  A conforming browser suppresses it; Android webviews have not
 *    always.  Dispatching it anyway is the stricter test of the 400 ms
 *    `_touchActive` guard, which is the thing that actually stops a double
 *    dispatch.
 */
function tap(el, { touch }) {
  let prevented = false;
  if (touch) {
    fire(el, "touchstart", { cancelable: false });
    prevented = fire(el, "touchend").defaultPrevented;
  }
  const isNative = NATIVE_TAGS.has(el.tagName);
  if (isNative && prevented) return;

  if (el.type === "checkbox") el.checked = !el.checked;
  fire(el, "click");
  if (isNative) {
    // A checkbox's activation fires `input` and then `change` — both, from the
    // one tap.  Modelled because the card's edit guard has to survive it: were
    // the guard to treat any `input` as the start of an edit, this pair alone
    // would decide whether a tapped checkbox freezes the card.
    fire(el, "input", { composed: true, cancelable: false });
    fire(el, "change", { composed: false, cancelable: false });
  }
}

/**
 * Keystrokes into the delay box, with nothing committed — a half-typed value.
 *
 * A browser produces `keydown` and then `input` per character; one round of the
 * pair is enough, and setting `.value` without them would be a test of nothing,
 * since a property write raises no event a card could ever hear.
 */
function typeInto(el, value) {
  el.focus();
  el.value = String(value);
  fire(el, "keydown", { composed: true });
  fire(el, "input", { composed: true, cancelable: false });
}

/**
 * Type into the delay box and commit it, which is what fires "change".
 *
 * `change` comes *before* `blur`: the spec fires it from the unfocusing steps,
 * so a browser reports the committed value while the box still has the focus.
 */
function editDelay(el, value) {
  typeInto(el, value);
  fire(el, "change", { composed: false, cancelable: false });
  el.blur();
}

/**
 * Fire one tick of the card's 15 s refresh timer, without waiting 15 s for it.
 * The card holds the interval id in `_refreshTimer`; the sandbox kept the
 * callback under the same id.
 */
function fireRefreshTick(card) {
  const fn = intervalCallbacks.get(card._refreshTimer);
  if (!fn) throw new Error("harness: the card registered no refresh timer");
  fn();
}

// ---------------------------------------------------------------------------
// Scenarios
// ---------------------------------------------------------------------------

const results = {};

function record(name, calls, extra) {
  results[name] = Object.assign({ calls }, extra || {});
}

// (a) touch tap on the auto-repair checkbox
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const box = root.querySelector('.repair-auto-toggle[data-checker="shade_gateway"]');
  tap(box, { touch: true });
  record("touch_toggle", calls, { checked_after: box.checked });
  teardown(card);
}

// (b) mouse click on the auto-repair checkbox
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const box = root.querySelector('.repair-auto-toggle[data-checker="shade_gateway"]');
  tap(box, { touch: false });
  record("mouse_toggle", calls, { checked_after: box.checked });
  teardown(card);
}

// (c) delay committed with "change" — in range for the published bounds
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  editDelay(input, 180);
  record("delay_change", calls);
  teardown(card);
}

// (d) touch tap on a non-form button
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const btn = root.querySelector('.repair-btn[data-checker="shade_gateway"]');
  tap(btn, { touch: true });
  record("touch_button", calls);
  teardown(card);
}

// (e) the delay input carries the checker's own bounds — or the fallback
{
  const { card, root } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  results.bounds_published = {
    min: input.getAttribute("min"),
    max: input.getAttribute("max"),
    step: input.getAttribute("step"),
    value: input.getAttribute("value"),
  };
  teardown(card);
}
{
  // An older checker, or a sensor payload cached from before the field
  // existed: no bounds at all.
  const legacy = checker();
  legacy.repair_state = Object.assign({}, legacy.repair_state);
  delete legacy.repair_state.auto_repair_delay_bounds;
  legacy.repair_state.auto_repair_delay_min = 5;
  const { card, root } = mount("shade_gateway", legacy);
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  results.bounds_absent = {
    min: input.getAttribute("min"),
    max: input.getAttribute("max"),
    step: input.getAttribute("step"),
    value: input.getAttribute("value"),
  };
  teardown(card);
}

// (f) the client enforces one bound and no more: 1 is a safety floor, because
// a 0 or negative delay collapses the dwell gate altogether.
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  editDelay(input, 0);
  record("delay_below_floor", calls);
  teardown(card);
}

// ...but the upper bound belongs to the backend, which clamps it *loudly*
// (_clamp_delay(loud=True) warns and republishes the correction).  A guard here
// would drop the command with no relay call, no log and nothing on screen — and
// while the checker has not published its bounds yet the fallback max is 60, so
// that silent drop swallowed a perfectly legal shade-gateway 180.
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  editDelay(input, 9000);
  record("delay_above_max", calls);
  teardown(card);
}

// The toggle carries the delay alongside it, so its guard has to use the same
// bounds: 120 is legal for the shade gateway and was dropped by `<= 60`.
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const box = root.querySelector('.repair-auto-toggle[data-checker="shade_gateway"]');
  tap(box, { touch: false });
  record("toggle_carries_out_of_legacy_range_delay", calls);
  teardown(card);
}

// (g) the 15 s refresh tick must not eat a half-typed delay.  _update() rewrites
// innerHTML wholesale, so a tick landing mid-edit swaps the focused input for a
// fresh node carrying the published value — the keystrokes and the focus both
// gone, on a wall display where re-typing means re-finding the popup.  `set
// hass` has always guarded; the timer had not.
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  let changeEvents = 0;
  root.addEventListener("change", () => {
    changeEvents += 1;
  });

  typeInto(input, "18"); // mid-way to 180 — one keystroke short

  fireRefreshTick(card);

  const after = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  record("refresh_during_edit", calls, {
    same_node: after === input,
    value_after: after ? after.value : null,
    change_events: changeEvents,
  });

  // Closing the popup mid-edit is the one path where no focusout is
  // guaranteed to arrive, so the box is deliberately NOT blurred first.  A
  // flag surviving the close would freeze the card the next time it opens.
  teardown(card);
  results.refresh_during_edit.editing_after_close = card._editing;
  shimDocument.activeElement = null;
}

// (g2) the mirror image of (g), and the reason focus was the wrong signal all
// along: a delay box the operator merely tapped into and then walked away from
// holds nothing.  Guarding on activeElement stopped both re-render paths there
// too — indefinitely, because a wall display never gets clicked elsewhere.
{
  const { card, calls, root, hass } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  const detailBefore = root.querySelector(".check-detail").textContent;

  input.focus(); // ...and nothing typed

  // Stated, not assumed: if the box were not really focused, or not really a
  // text control, the scenario would pass for a reason that proves nothing.
  const focusedTag = root.activeElement ? root.activeElement.tagName : null;
  const focusedType = root.activeElement ? root.activeElement.type : null;

  hass.states["sensor.health_check_status"].attributes.checkers.shade_gateway
    .checks[0].detail = "gateway back";
  fireRefreshTick(card);

  const after = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  record("refresh_with_input_merely_focused", calls, {
    focused_tag: focusedTag,
    focused_type: focusedType,
    detail_before: detailBefore,
    detail_after_tick: root.querySelector(".check-detail").textContent,
    node_replaced: after !== input,
  });
  shimDocument.activeElement = null;
  teardown(card);
}

// (g3) a committed value releases the guard at once, so the very next render
// shows the server's number again.  A spinner click is exactly this pair —
// `input` then `change`, with the focus never leaving the box — which is why
// the release cannot be made to wait for a blur.
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');

  typeInto(input, "180");
  fire(input, "change", { composed: false, cancelable: false });
  const valueTyped = input.value;

  // The box still holds the focus here — that is the point.  A guard reading
  // activeElement would block the tick below and this would prove nothing.
  const focusedAtTick = root.activeElement === input ? input.tagName : null;

  // The backend has not accepted 180 yet, so the redraw must show the 120 the
  // sensor still publishes rather than leaving the typed value on screen.
  fireRefreshTick(card);

  const after = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  record("refresh_after_commit", calls, {
    value_typed: valueTyped,
    focused_at_tick: focusedAtTick,
    node_replaced: after !== input,
    value_after: after ? after.value : null,
  });
  shimDocument.activeElement = null;
  teardown(card);
}

// (h) ...and the mirror image: a tap on the auto-repair checkbox must NOT
// freeze the card.  A checkbox keeps the browser focus after a tap, and on a
// wall display nothing ever clicks elsewhere afterwards — so a focus guard that
// counts every INPUT stops both re-render paths (`set hass` AND the 15 s tick)
// from that one tap until someone physically walks up to the display.  Unlike
// the delay box, a checkbox holds no typed text: there is nothing for a
// re-render to destroy, so it must not qualify.
{
  const { card, calls, root, hass } = mount("shade_gateway", checker());
  const box = root.querySelector('.repair-auto-toggle[data-checker="shade_gateway"]');
  tap(box, { touch: true });
  // A real tap leaves the focus on the control it landed on; `tap` models only
  // the event sequence, so the focus is stated here.
  box.focus();
  const focusedTag = root.activeElement ? root.activeElement.tagName : null;
  const focusedType = root.activeElement ? root.activeElement.type : null;

  const detailBefore = root.querySelector(".check-detail").textContent;
  const nodeBefore = root.querySelector(
    '.repair-auto-toggle[data-checker="shade_gateway"]'
  );

  // Move what _snapshot() actually hashes — the status entity's state and its
  // `checkers` attribute — or `set hass` would skip the render for its own
  // unrelated reason and prove nothing.
  const status = hass.states["sensor.health_check_status"];
  status.state = "warning";
  status.attributes = {
    checkers: {
      shade_gateway: checker({
        status: "warning",
        checks: [
          {
            name: "Gateway",
            status: "warning",
            detail: "reconnecting",
            last_changed: ISO,
          },
        ],
      }),
    },
  };
  card.hass = hass;
  const detailAfterSetHass = root.querySelector(".check-detail").textContent;

  // The 15 s tick is the other re-render path and reads the same guard.
  status.attributes.checkers.shade_gateway.checks[0].detail = "gateway back";
  fireRefreshTick(card);
  const detailAfterTick = root.querySelector(".check-detail").textContent;

  record("rerender_with_checkbox_focused", calls, {
    focused_tag: focusedTag,
    focused_type: focusedType,
    detail_before: detailBefore,
    detail_after_set_hass: detailAfterSetHass,
    detail_after_tick: detailAfterTick,
    node_replaced:
      root.querySelector('.repair-auto-toggle[data-checker="shade_gateway"]') !==
      nodeBefore,
  });
  // Focus is global to the shim document and the re-render dropped it into
  // <body>; clear it so the next scenario starts with nothing focused.
  shimDocument.activeElement = null;
  teardown(card);
}

// (h3) the same freeze reached through the keyboard.  A keystroke arrives from
// whatever holds the focus, and a checkbox that takes one never follows it with
// an `input` or a `change` — so a guard that treated any keydown as the start
// of an edit would stop here for good, with nothing left to lower the flag.
{
  const { card, calls, root, hass } = mount("shade_gateway", checker());
  const box = root.querySelector('.repair-auto-toggle[data-checker="shade_gateway"]');
  box.focus();
  fire(box, "keydown", { composed: true }); // an arrow key: no activation
  // Read before the tick: afterwards the re-render has replaced the box and
  // dropped the focus, so a later read would say nothing about the keystroke.
  const focusedType = root.activeElement ? root.activeElement.type : null;

  hass.states["sensor.health_check_status"].attributes.checkers.shade_gateway
    .checks[0].detail = "gateway back";
  fireRefreshTick(card);

  record("refresh_after_keydown_on_checkbox", calls, {
    focused_type: focusedType,
    detail_after_tick: root.querySelector(".check-detail").textContent,
  });
  shimDocument.activeElement = null;
  teardown(card);
}

results.notes = notes;

for (const id of timers) {
  clearInterval(id);
  clearTimeout(id);
}

process.stdout.write(JSON.stringify(results, null, 2) + "\n");
