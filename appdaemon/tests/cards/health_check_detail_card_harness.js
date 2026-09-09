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

// ---------------------------------------------------------------------------
// HTML parsing
// ---------------------------------------------------------------------------

const VOID_TAGS = new Set([
  "area", "base", "br", "col", "embed", "hr", "img", "input",
  "link", "meta", "param", "source", "track", "wbr",
]);
// Contents are text, not markup: the card injects a whole stylesheet this way.
const RAW_TEXT_TAGS = new Set(["style", "script", "textarea"]);

const ATTR_RE = /([^\s"'>/=]+)(\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>`]+)))?/g;

function parseAttributes(source) {
  const attrs = new Map();
  ATTR_RE.lastIndex = 0;
  let m;
  while ((m = ATTR_RE.exec(source)) !== null) {
    const value = m[4] ?? m[5] ?? m[6] ?? "";
    attrs.set(m[1].toLowerCase(), decodeEntities(value));
  }
  return attrs;
}

function decodeEntities(str) {
  return str
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

function escapeText(str) {
  // What a browser does when serialising a text node into innerHTML: & < >
  // only. hcdEscapeHtml() rides on exactly this.
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(str) {
  return escapeText(str).replace(/"/g, "&quot;");
}

/** Parse *html* into a flat list of child nodes of *parent*. */
function parseHtml(html, parent) {
  const roots = [];
  let open = [];
  let i = 0;

  const currentParent = () => (open.length ? open[open.length - 1] : null);
  const append = (node) => {
    const p = currentParent();
    if (p) p.appendChild(node);
    else {
      node.parentNode = parent;
      roots.push(node);
    }
  };

  while (i < html.length) {
    const lt = html.indexOf("<", i);
    if (lt === -1) {
      const text = html.slice(i);
      if (text) append(new ShimText(text));
      break;
    }
    if (lt > i) append(new ShimText(html.slice(i, lt)));

    if (html.startsWith("<!--", lt)) {
      const end = html.indexOf("-->", lt);
      i = end === -1 ? html.length : end + 3;
      continue;
    }
    if (html.startsWith("<!", lt)) {
      const end = html.indexOf(">", lt);
      i = end === -1 ? html.length : end + 1;
      continue;
    }

    const gt = html.indexOf(">", lt);
    if (gt === -1) {
      append(new ShimText(html.slice(lt)));
      break;
    }
    const raw = html.slice(lt + 1, gt);
    i = gt + 1;

    if (raw.startsWith("/")) {
      const name = raw.slice(1).trim().toLowerCase();
      for (let d = open.length - 1; d >= 0; d--) {
        if (open[d].localName === name) {
          open = open.slice(0, d);
          break;
        }
      }
      continue;
    }

    const selfClosing = raw.endsWith("/");
    const body = selfClosing ? raw.slice(0, -1) : raw;
    const space = body.search(/\s/);
    const name = (space === -1 ? body : body.slice(0, space)).toLowerCase();
    const attrs = space === -1 ? new Map() : parseAttributes(body.slice(space));

    const el = new ShimElement(name);
    for (const [k, v] of attrs) el.setAttribute(k, v);
    append(el);

    if (RAW_TEXT_TAGS.has(name)) {
      const close = html.toLowerCase().indexOf(`</${name}`, i);
      const end = close === -1 ? html.length : close;
      if (end > i) el.appendChild(new ShimText(html.slice(i, end)));
      const after = html.indexOf(">", end);
      i = after === -1 ? html.length : after + 1;
      continue;
    }
    if (!selfClosing && !VOID_TAGS.has(name)) open.push(el);
  }
  return roots;
}

// ---------------------------------------------------------------------------
// Selectors — compound only (tag, .class, [attr], [attr="value"]).
// That is every selector the card uses; a combinator would be a silent
// mismatch, so it throws rather than quietly matching nothing.
// ---------------------------------------------------------------------------

function parseSelector(selector) {
  const sel = selector.trim();
  if (/[\s>+~,]/.test(sel)) {
    throw new Error(`harness selector too complex: ${selector}`);
  }
  const parsed = { tag: null, classes: [], attrs: [] };
  const re = /^([a-zA-Z][\w-]*)|^\.([\w-]+)|^\[([\w-]+)(?:([~^$*|]?=)"?([^\]"]*)"?)?\]/;
  let rest = sel;
  while (rest.length) {
    const m = re.exec(rest);
    if (!m) throw new Error(`harness cannot parse selector: ${selector}`);
    if (m[1]) parsed.tag = m[1].toLowerCase();
    else if (m[2]) parsed.classes.push(m[2]);
    else if (m[3]) parsed.attrs.push({ name: m[3].toLowerCase(), value: m[5] });
    rest = rest.slice(m[0].length);
  }
  return parsed;
}

function matchesSelector(el, parsed) {
  if (parsed.tag && el.localName !== parsed.tag) return false;
  for (const cls of parsed.classes) {
    if (!el.classList.contains(cls)) return false;
  }
  for (const attr of parsed.attrs) {
    if (!el.hasAttribute(attr.name)) return false;
    if (attr.value !== undefined && el.getAttribute(attr.name) !== attr.value) {
      return false;
    }
  }
  return true;
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

class ShimEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.bubbles = options.bubbles !== false;
    this.cancelable = options.cancelable !== false;
    // "change" and "input" are NOT composed: they stop at the shadow root.
    // Every other event the card listens for is.
    this.composed = options.composed === true;
    this.defaultPrevented = false;
    this.target = null;
    this._path = [];
    this._stopped = false;
  }

  composedPath() {
    return this._path;
  }

  preventDefault() {
    if (this.cancelable) this.defaultPrevented = true;
  }

  stopPropagation() {
    this._stopped = true;
  }
}

// ---------------------------------------------------------------------------
// Nodes
// ---------------------------------------------------------------------------

/**
 * Drop a replaced subtree on the floor, the way a browser does.
 *
 * Two details matter to the card.  A node that has left the tree has no
 * parentNode any more, and if it held the focus the document blurs it —
 * activeElement falls back to <body> rather than resolving to a node nothing
 * can see.  Without that second part the card's focus guard would keep
 * answering "yes, an input is focused" about an input that was destroyed three
 * re-renders ago, and the guard would look like it worked when it did not.
 */
function detachSubtree(node) {
  if (shimDocument.activeElement === node) {
    shimDocument.activeElement = shimDocument.body;
  }
  for (const child of node.childNodes) detachSubtree(child);
  node.parentNode = null;
}

class ShimNode {
  constructor() {
    this.parentNode = null;
    this.childNodes = [];
    this._listeners = new Map();
  }

  appendChild(node) {
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  get children() {
    return this.childNodes.filter((n) => n instanceof ShimElement);
  }

  addEventListener(type, handler, options) {
    const capture =
      options === true || Boolean(options && options.capture === true);
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push({ handler, capture });
  }

  removeEventListener(type, handler) {
    const list = this._listeners.get(type);
    if (list) this._listeners.set(type, list.filter((l) => l.handler !== handler));
  }

  _fire(evt, capture) {
    const list = this._listeners.get(evt.type);
    if (!list) return;
    for (const entry of list.slice()) {
      if (entry.capture !== capture) continue;
      entry.handler.call(this, evt);
    }
  }

  dispatchEvent(evt) {
    const path = [];
    let node = this;
    while (node) {
      path.push(node);
      if (node instanceof ShimShadowRoot) {
        // A non-composed event dies at the shadow boundary — which is why the
        // card's "change" listener has to live on the shadow root itself.
        if (!evt.composed) break;
        node = node.host;
      } else {
        node = node.parentNode;
      }
    }
    evt._path = path;
    evt.target = this;

    for (let i = path.length - 1; i >= 1 && !evt._stopped; i--) {
      path[i]._fire(evt, true);
    }
    if (!evt._stopped) path[0]._fire(evt, true);
    if (!evt._stopped) path[0]._fire(evt, false);
    if (evt.bubbles) {
      for (let i = 1; i < path.length && !evt._stopped; i++) {
        path[i]._fire(evt, false);
      }
    }
    return !evt.defaultPrevented;
  }

  // -- queries ------------------------------------------------------------

  _descendants(out = []) {
    for (const child of this.childNodes) {
      if (child instanceof ShimElement) {
        out.push(child);
        child._descendants(out);
      }
    }
    return out;
  }

  querySelectorAll(selector) {
    const parsed = parseSelector(selector);
    return this._descendants().filter((el) => matchesSelector(el, parsed));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  get innerHTML() {
    return this.childNodes.map(serialize).join("");
  }

  set innerHTML(html) {
    for (const child of this.childNodes) detachSubtree(child);
    this.childNodes = parseHtml(String(html), this);
  }

  get textContent() {
    return this.childNodes.map(textOf).join("");
  }

  set textContent(value) {
    this.childNodes = [];
    if (value !== "" && value != null) this.appendChild(new ShimText(String(value)));
  }
}

class ShimText extends ShimNode {
  constructor(data) {
    super();
    this.nodeType = 3;
    this.data = String(data);
  }
}

class ShimElement extends ShimNode {
  constructor(localName) {
    super();
    this.nodeType = 1;
    this.localName = String(localName).toLowerCase();
    this.tagName = this.localName.toUpperCase();
    this._attrs = new Map();
    this.shadowRoot = null;
  }

  // -- attributes ---------------------------------------------------------

  getAttribute(name) {
    const key = String(name).toLowerCase();
    return this._attrs.has(key) ? this._attrs.get(key) : null;
  }

  setAttribute(name, value) {
    this._attrs.set(String(name).toLowerCase(), String(value));
  }

  removeAttribute(name) {
    this._attrs.delete(String(name).toLowerCase());
  }

  hasAttribute(name) {
    return this._attrs.has(String(name).toLowerCase());
  }

  get classList() {
    const self = this;
    const read = () => (self.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    const write = (list) => self.setAttribute("class", list.join(" "));
    return {
      contains: (name) => read().includes(name),
      add: (name) => {
        const l = read();
        if (!l.includes(name)) write(l.concat(name));
      },
      remove: (name) => write(read().filter((c) => c !== name)),
      toggle: (name) => {
        const l = read();
        if (l.includes(name)) {
          write(l.filter((c) => c !== name));
          return false;
        }
        write(l.concat(name));
        return true;
      },
    };
  }

  get dataset() {
    const data = {};
    for (const [key, value] of this._attrs) {
      if (!key.startsWith("data-")) continue;
      const camel = key
        .slice(5)
        .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      data[camel] = value;
    }
    return data;
  }

  // -- form-control properties -------------------------------------------

  get type() {
    return this.getAttribute("type");
  }

  get value() {
    return this._value !== undefined ? this._value : this.getAttribute("value") || "";
  }

  set value(v) {
    this._value = String(v);
  }

  get checked() {
    return this._checked !== undefined ? this._checked : this.hasAttribute("checked");
  }

  set checked(v) {
    this._checked = Boolean(v);
  }

  // -- shadow DOM ---------------------------------------------------------

  attachShadow() {
    this.shadowRoot = new ShimShadowRoot(this);
    return this.shadowRoot;
  }

  blur() {
    if (shimDocument.activeElement === this) shimDocument.activeElement = null;
  }

  focus() {
    shimDocument.activeElement = this;
  }
}

class ShimShadowRoot extends ShimNode {
  constructor(host) {
    super();
    this.host = host;
    this.nodeType = 11;
  }

  // The card's focus guard reads this before every re-render.
  get activeElement() {
    const active = shimDocument.activeElement;
    if (!active) return null;
    let node = active;
    while (node) {
      if (node === this) return active;
      node = node instanceof ShimShadowRoot ? node.host : node.parentNode;
    }
    return null;
  }
}

function serialize(node) {
  if (node instanceof ShimText) return escapeText(node.data);
  if (!(node instanceof ShimElement)) return "";
  let attrs = "";
  for (const [k, v] of node._attrs) attrs += ` ${k}="${escapeAttr(v)}"`;
  if (VOID_TAGS.has(node.localName)) return `<${node.localName}${attrs}>`;
  const inner = node.childNodes.map(serialize).join("");
  return `<${node.localName}${attrs}>${inner}</${node.localName}>`;
}

function textOf(node) {
  if (node instanceof ShimText) return node.data;
  return node.childNodes.map(textOf).join("");
}

// ---------------------------------------------------------------------------
// document / window / customElements
// ---------------------------------------------------------------------------

const registry = new Map();

const shimDocument = Object.assign(new ShimNode(), {
  activeElement: null,
  createElement(tag) {
    const name = String(tag).toLowerCase();
    const Ctor = registry.get(name);
    if (Ctor) {
      const el = new Ctor();
      el.localName = name;
      el.tagName = name.toUpperCase();
      return el;
    }
    return new ShimElement(name);
  },
});
shimDocument.body = new ShimElement("body");
shimDocument.appendChild(shimDocument.body);

const customElements = {
  define(name, ctor) {
    registry.set(String(name).toLowerCase(), ctor);
  },
  get(name) {
    return registry.get(String(name).toLowerCase());
  },
};

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
  return { card, calls, root: card.shadowRoot };
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
  if (isNative) fire(el, "change", { composed: false, cancelable: false });
}

/** Type into the delay box and commit it, which is what fires "change". */
function editDelay(el, value) {
  el.focus();
  el.value = String(value);
  el.blur();
  fire(el, "change", { composed: false, cancelable: false });
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
// hass` has always guarded on activeElement; the timer had not.
{
  const { card, calls, root } = mount("shade_gateway", checker());
  const input = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  let changeEvents = 0;
  root.addEventListener("change", () => {
    changeEvents += 1;
  });

  input.focus();
  input.value = "18"; // mid-way to 180 — one keystroke short
  fireRefreshTick(card);

  const after = root.querySelector('.repair-delay-input[data-checker="shade_gateway"]');
  record("refresh_during_edit", calls, {
    same_node: after === input,
    value_after: after ? after.value : null,
    change_events: changeEvents,
  });
  input.blur();
  teardown(card);
}

results.notes = notes;

for (const id of timers) {
  clearInterval(id);
  clearTimeout(id);
}

process.stdout.write(JSON.stringify(results, null, 2) + "\n");
