/** Shared minimal DOM for dependency-free custom-card regression tests. */
"use strict";

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
      if (text) append(new ShimText(decodeEntities(text)));
      break;
    }
    if (lt > i) append(new ShimText(decodeEntities(html.slice(i, lt))));

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
  if (/[\s>+~,]/.test(sel.replace(/\[[^\]]*\]/g, ""))) {
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
    // "change" is NOT composed — it stops at the shadow root, which is why
    // the card's listener has to sit on the root itself.  "input" IS composed
    // (the HTML spec fires it with bubbles and composed both true), as are the
    // touch, key and focus events.  Callers pass what each one really is.
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
 *
 * No focusout is synthesised here.  Browsers disagree about whether removing a
 * focused node fires one, so the card must not be allowed to depend on it —
 * it clears its own edit flag in disconnectedCallback instead.
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
      toggle: (name, force) => {
        const l = read();
        if (force === true) {
          if (!l.includes(name)) write(l.concat(name));
          return true;
        }
        if (force === false || l.includes(name)) {
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

  // A browser's HTMLInputElement.type is lower-cased and defaults to "text"
  // when the attribute is absent, and the card's focus guard reads it to tell a
  // text box from a checkbox.  Answering a bare null here would let card code
  // pass in the shim that misbehaves in a browser, so the shim matches the
  // browser instead.  Other elements keep the raw attribute — the harness only
  // reads it to spot a checkbox.
  get type() {
    const attr = this.getAttribute("type");
    if (this.localName !== "input") return attr;
    return attr ? String(attr).toLowerCase() : "text";
  }

  get value() {
    if (this._value !== undefined) return this._value;
    if (this.localName === "select") {
      const options = this.querySelectorAll("option");
      const selected = options.find((option) => option.hasAttribute("selected")) || options[0];
      return selected?.value || "";
    }
    return this.getAttribute("value") || "";
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

  get disabled() { return this.hasAttribute("disabled"); }

  set disabled(value) {
    if (value) this.setAttribute("disabled", "");
    else this.removeAttribute("disabled");
  }

  // -- shadow DOM ---------------------------------------------------------

  attachShadow() {
    this.shadowRoot = new ShimShadowRoot(this);
    return this.shadowRoot;
  }

  // Losing the focus is two events, not none: the non-bubbling `blur` and then
  // the bubbling `focusout`, both composed.  A delegated listener on the shadow
  // root can only ever see the second one — which is exactly why the card
  // listens for `focusout`, and why a shim that dispatched nothing here would
  // have let a `blur` listener look like it worked.
  blur() {
    if (shimDocument.activeElement !== this) return;
    shimDocument.activeElement = null;
    fire(this, "blur", { bubbles: false, composed: true, cancelable: false });
    fire(this, "focusout", { bubbles: true, composed: true, cancelable: false });
  }

  // Taking the focus unfocuses whatever held it first, in that order.
  focus() {
    const previous = shimDocument.activeElement;
    if (previous === this) return;
    if (previous && typeof previous.blur === "function") previous.blur();
    shimDocument.activeElement = this;
    fire(this, "focus", { bubbles: false, composed: true, cancelable: false });
    fire(this, "focusin", { bubbles: true, composed: true, cancelable: false });
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


function fire(el, type, options = {}) {
  const event = new ShimEvent(type, options);
  for (const key of ["key", "relatedTarget", "button", "pointerType"]) {
    if (options[key] !== undefined) event[key] = options[key];
  }
  el.dispatchEvent(event);
  return event;
}

module.exports = { ShimElement, ShimNode, ShimEvent, shimDocument, customElements, fire };
