/**
 * Kitchen Timer Card (hass-sandbox #262, #266)
 *
 * Up to three kitchen timers on the kitchen UniFi Connect display. It swaps into the
 * Disney countdown slot of the unifi-connect Home view (a conditional pair: the
 * countdown shows while no timer runs or rings, this card otherwise), so it keeps the
 * slot's exact footprint: 16:9, 440 x 248 at 1920 x 1080.
 *
 *   one timer     big m:ss from finishes_at, progress bar, "+1 min", "Cancel"
 *   two or three  one row each: name, m:ss, a thin progress bar, "+1 min" and a cancel
 *                 button. Rows keep their slot order, so they never move under a finger.
 *   cancel        needs a second tap within 3 s, so a brush cannot kill a timer
 *   ringing       that timer turns amber ("Pasta timer done"); a tap on it stops only it
 *   paused        digits dimmed
 *
 * Reads, per slot N (1-3, live-only helpers):
 *   timer.kitchen_timer_N, input_text.kitchen_timer_N_name, input_boolean.kitchen_timer_N_ringing
 * Writes: script.kitchen_timer_relay {command: add | cancel | stop, payload: {slot, seconds}}
 *         (relay pattern; never fire_event, which fails for the non-admin display user)
 *
 * Names: "Timer" (the one unnamed timer) shows as "Kitchen timer"; "Timer 2" and named
 * timers ("Pasta") show as they are.
 *
 * Chime: Web Audio, synthesized (the CSP has no data: in media-src). It plays every
 * chime_interval_s while any timer rings, for at most max_ring_s per ring, and only on
 * the kitchen screen: a screen chimes when its own IP (the current refresh token's
 * last_used_ip, from auth/refresh_tokens) equals the `ip` attribute of chime_tracker
 * (the kitchen display's UniFi client, tracked by MAC, so a new DHCP lease follows it)
 * or is listed in chime_ips. The ringer lives at module level and reads the root
 * <home-assistant> element's hass, so it keeps ringing while the display sits on a
 * sub-view and this card is not in the DOM. Any tap on the page resumes a suspended
 * AudioContext, and the card shows a "Tap for sound" chip while audio is blocked.
 * Once per page load the card writes one logbook line (screen IP, chime on/off,
 * viewport, audio state, path) under timer.kitchen_timer_1.
 *
 * No text inputs, so no edit-in-progress render guard is needed. Touch/click use one
 * delegated listener with dedupe (.agents/rules/custom-card-guidelines.md section 2).
 *
 * Deploy: /config/www/kitchen-timer/kitchen-timer-card.js, Lovelace resource
 * /local/kitchen-timer/kitchen-timer-card.js?v=N (bump N on every change).
 *
 * Platforms: UniFi Connect Display (Chromium 134 WebView), desktop browsers, iOS app.
 */

const KT_DEFAULTS = {
  slots: [1, 2, 3],
  timer_prefix: "timer.kitchen_timer_",
  name_prefix: "input_text.kitchen_timer_",
  ringing_prefix: "input_boolean.kitchen_timer_",
  relay_script: "kitchen_timer_relay",
  chime: true,
  chime_tracker: "",
  chime_ips: [],
  chime_interval_s: 5,
  max_ring_s: 600,
  volume: 0.6,
  log_load: true,
};

const KT_CONFIRM_MS = 3000;

// ---------------------------------------------------------------------------
// Module-level audio + ringer (one per page, shared by every card instance)
// ---------------------------------------------------------------------------

const KT = {
  ctx: null,
  unlockBound: false,
  ringer: null,
  cfg: null,
  hass: null,
  lastChime: 0,
  episodes: {}, // ringing entity -> { id: last_changed, seenAt: local ms }
  silenced: {}, // ringing entity -> episode id stopped on this screen
  ip: null,
  ipPromise: null,
  loadLogged: false,
};

function ktAudioCtx() {
  if (KT.ctx) return KT.ctx;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  try {
    KT.ctx = new Ctor();
  } catch (err) {
    KT.ctx = null;
  }
  return KT.ctx;
}

function ktAudioState() {
  return KT.ctx ? KT.ctx.state : "none";
}

function ktBindUnlock() {
  if (KT.unlockBound) return;
  KT.unlockBound = true;
  const resume = () => {
    const ctx = ktAudioCtx();
    if (ctx && ctx.state !== "running") ctx.resume().catch(() => {});
  };
  // Chromium grants user activation on pointerup / touchend / click, not on the down events.
  ["pointerdown", "pointerup", "touchstart", "touchend", "click", "keydown"].forEach((evt) => {
    window.addEventListener(evt, resume, { capture: true, passive: true });
  });
}

/** One chime: a soft C6-E6-G6 bell arpeggio, about 1.8 s. Returns false if muted. */
function ktChime(volume) {
  const ctx = ktAudioCtx();
  if (!ctx) return false;
  if (ctx.state !== "running") {
    ctx.resume().catch(() => {});
    return false;
  }
  const master = ctx.createGain();
  master.gain.value = Math.max(0, Math.min(1, Number(volume) || 0.6));
  master.connect(ctx.destination);
  const start = ctx.currentTime + 0.03;
  const notes = [1046.5, 1318.51, 1567.98];
  notes.forEach((freq, i) => {
    const t = start + i * 0.22;
    [[1, 0.6], [2, 0.18], [3.01, 0.07]].forEach(([mult, level]) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq * mult;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(level, t + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 1.4);
      osc.connect(gain);
      gain.connect(master);
      osc.start(t);
      osc.stop(t + 1.45);
    });
  });
  return true;
}

function ktRootHass() {
  const root = document.querySelector("home-assistant");
  return (root && root.hass) || KT.hass;
}

/** True when this screen should chime: its IP is the kitchen display's (tracker) or listed. */
function ktChimeAllowed(cfg, hass) {
  if (!cfg || !cfg.chime || !KT.ip) return false;
  if (cfg.chime_ips.indexOf(KT.ip) !== -1) return true;
  const tracker = cfg.chime_tracker && hass && hass.states && hass.states[cfg.chime_tracker];
  return !!(tracker && tracker.attributes && tracker.attributes.ip === KT.ip);
}

function ktRingerTick() {
  const cfg = KT.cfg;
  const hass = ktRootHass();
  if (!cfg || !hass || !hass.states) return;
  // While the websocket is down the states are stale: a ring stopped elsewhere would keep chiming.
  if (hass.connected === false) return;
  const now = Date.now();
  let due = false;
  cfg.slots.forEach((n) => {
    const ent = cfg.ringing_prefix + n + "_ringing";
    const ring = hass.states[ent];
    if (!ring || ring.state !== "on") {
      delete KT.episodes[ent];
      return;
    }
    // Episode = this run of "on". Measured with the local clock from when this page
    // first saw it, so a skewed display clock cannot cut the cap short.
    if (!KT.episodes[ent] || KT.episodes[ent].id !== ring.last_changed) {
      KT.episodes[ent] = { id: ring.last_changed, seenAt: now };
    }
    const ep = KT.episodes[ent];
    if (KT.silenced[ent] === ep.id) return;
    if (now - ep.seenAt > cfg.max_ring_s * 1000) return;
    due = true;
  });
  if (!due) {
    KT.lastChime = 0;
    return;
  }
  if (!ktChimeAllowed(cfg, hass)) return;
  if (KT.lastChime && now - KT.lastChime < cfg.chime_interval_s * 1000) return;
  if (ktChime(cfg.volume)) KT.lastChime = now;
}

function ktArmRinger(cfg) {
  KT.cfg = cfg;
  ktAudioCtx();
  ktBindUnlock();
  if (!KT.ringer) KT.ringer = setInterval(ktRingerTick, 500);
}

/** This screen's IP as HA saw it (the current refresh token's last_used_ip). */
function ktScreenIp(hass) {
  if (!KT.ipPromise) {
    KT.ipPromise = hass
      .callWS({ type: "auth/refresh_tokens" })
      .then((tokens) => {
        const current = (tokens || []).find((t) => t && t.is_current);
        KT.ip = (current && current.last_used_ip) || "";
        return KT.ip;
      })
      .catch(() => {
        KT.ip = "";
        return "";
      });
  }
  return KT.ipPromise;
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

class KitchenTimerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...KT_DEFAULTS };
    this._hass = null;
    this._tick = null;
    this._layoutKey = "";
    this._confirm = {}; // slot -> until (ms)
    this._confirmTimers = {};
    this._rows = {};
    this._bound = false;
  }

  setConfig(config) {
    const merged = { ...KT_DEFAULTS, ...(config || {}) };
    merged.slots = (Array.isArray(merged.slots) ? merged.slots : KT_DEFAULTS.slots)
      .map((n) => Number(n))
      .filter((n) => n > 0);
    merged.chime_ips = Array.isArray(merged.chime_ips)
      ? merged.chime_ips.map((ip) => String(ip).trim()).filter(Boolean)
      : [];
    merged.chime_interval_s = Math.max(2, Number(merged.chime_interval_s) || 5);
    merged.max_ring_s = Math.max(10, Number(merged.max_ring_s) || 600);
    this._config = merged;
    this._layoutKey = "";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    KT.hass = hass;
    if (!this._bound) this._buildShell();
    if (first) this._initScreen();
    this._update();
  }

  getCardSize() {
    return 4;
  }

  getGridOptions() {
    return { columns: 12, rows: "auto" };
  }

  static getStubConfig() {
    return { chime_tracker: "" };
  }

  connectedCallback() {
    if (this._hass) this._update();
  }

  disconnectedCallback() {
    this._stopTick();
    Object.keys(this._confirmTimers).forEach((slot) => clearTimeout(this._confirmTimers[slot]));
    this._confirmTimers = {};
    this._confirm = {};
  }

  // -------------------------------------------------------------------------
  // Screen gating (chime only on the kitchen screen) + the once-per-page load log
  // -------------------------------------------------------------------------

  _initScreen() {
    const cfg = this._config;
    const hass = this._hass;
    ktArmRinger(cfg);
    ktScreenIp(hass).then((ip) => {
      this._update();
      if (cfg.log_load && !KT.loadLogged) {
        KT.loadLogged = true;
        const allowed = ktChimeAllowed(cfg, ktRootHass() || hass);
        const dpr = Math.round((window.devicePixelRatio || 1) * 100) / 100;
        const message =
          `loaded on ${ip || "unknown IP"} (chime ${allowed ? "on" : "off"}), ` +
          `${window.innerWidth}x${window.innerHeight} @${dpr}x, ` +
          `audio ${ktAudioState()}, ${location.pathname}`;
        hass
          .callService("logbook", "log", {
            name: "Kitchen timer card",
            message,
            entity_id: cfg.timer_prefix + cfg.slots[0],
          })
          .catch(() => {});
      }
    });
  }

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  _timers() {
    const hass = this._hass;
    const cfg = this._config;
    const out = [];
    cfg.slots.forEach((n) => {
      const timer = hass && hass.states[cfg.timer_prefix + n];
      const ringEnt = cfg.ringing_prefix + n + "_ringing";
      const ring = hass && hass.states[ringEnt];
      const nameState = hass && hass.states[cfg.name_prefix + n + "_name"];
      const state = timer ? timer.state : "unavailable";
      const ringing = !!ring && ring.state === "on";
      const running = state === "active" || state === "paused";
      if (!running && !ringing) return;
      const attrs = (timer && timer.attributes) || {};
      let left = 0;
      if (state === "active" && attrs.finishes_at) {
        left = Math.max(0, (Date.parse(attrs.finishes_at) - Date.now()) / 1000);
      } else if (state === "paused") {
        left = ktParseDuration(attrs.remaining);
      }
      const raw = nameState && nameState.state;
      const name = raw && !["unknown", "unavailable", ""].includes(raw.trim()) ? raw.trim() : `Timer ${n}`;
      out.push({
        slot: n,
        name,
        label: ktLabel(name),
        doneText: ktDoneText(name),
        mode: ringing && !running ? "ring" : "run",
        ringing,
        paused: state === "paused",
        total: ktParseDuration(attrs.duration),
        left,
        finishesAt: attrs.finishes_at || null,
        ringEntity: ringEnt,
        ringEpisode: ring ? ring.last_changed : null,
      });
    });
    return out;
  }

  // -------------------------------------------------------------------------
  // DOM
  // -------------------------------------------------------------------------

  _buildShell() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="kt-card"><div class="kt-body"></div>
        <div class="kt-muted" data-action="unmute" hidden>
          <ha-icon icon="mdi:volume-off"></ha-icon><span>Tap for sound</span>
        </div>
      </div>
    `;
    this._el = {
      card: this.shadowRoot.querySelector(".kt-card"),
      body: this.shadowRoot.querySelector(".kt-body"),
      muted: this.shadowRoot.querySelector(".kt-muted"),
    };
    this._bindEvents();
    this._bound = true;
  }

  /** Rebuild the rows when the set of timers or their modes change; otherwise only update text. */
  _layout(timers) {
    const key =
      timers.length === 0
        ? "idle"
        : timers.map((t) => `${t.slot}:${t.mode}:${t.name}`).join("|") + `#${timers.length}`;
    if (key === this._layoutKey) return;
    this._layoutKey = key;
    this._rows = {};
    const body = this._el.body;
    const count = timers.length;
    this._el.card.className = `kt-card count-${count}`;
    if (count === 0) {
      body.innerHTML = `
        <div class="kt-idle"><ha-icon icon="mdi:timer-off-outline"></ha-icon><span>No kitchen timers running</span></div>`;
      return;
    }
    if (count === 1) {
      const t = timers[0];
      body.innerHTML =
        t.mode === "ring"
          ? `
        <div class="kt-hero-ring" data-action="stop" data-slot="${t.slot}">
          <ha-icon class="kt-bell" icon="mdi:bell-ring"></ha-icon>
          <div class="kt-done">${ktEsc(t.doneText)}</div>
          <div class="kt-hint">Tap anywhere on this card to stop</div>
          <div class="kt-say">or say “stop the timer”</div>
        </div>`
          : `
        <div class="kt-hero" data-slot="${t.slot}">
          <div class="kt-head">
            <div class="kt-title"><ha-icon icon="mdi:timer-outline"></ha-icon><span>${ktEsc(t.label)}</span></div>
            <div class="kt-meta"></div>
          </div>
          <div class="kt-digits">0:00</div>
          <div class="kt-bar"><div class="kt-bar-fill"></div></div>
          <div class="kt-buttons">
            <button class="kt-btn" data-action="add" data-slot="${t.slot}" type="button">+1 min</button>
            <button class="kt-btn kt-cancel" data-action="cancel" data-slot="${t.slot}" type="button">Cancel</button>
          </div>
        </div>`;
    } else {
      body.innerHTML = `<div class="kt-rows">${timers
        .map((t) =>
          t.mode === "ring"
            ? `
          <div class="kt-row ring" data-action="stop" data-slot="${t.slot}">
            <ha-icon class="kt-bell" icon="mdi:bell-ring"></ha-icon>
            <div class="kt-row-done">${ktEsc(t.doneText)}</div>
            <div class="kt-row-hint">Tap to stop</div>
          </div>`
            : `
          <div class="kt-row" data-slot="${t.slot}">
            <div class="kt-row-name">
              <div class="kt-row-label">${ktEsc(t.label)}</div>
              <div class="kt-row-meta"></div>
            </div>
            <div class="kt-row-digits">0:00</div>
            <button class="kt-pill" data-action="add" data-slot="${t.slot}" type="button">+1 min</button>
            <button class="kt-x kt-cancel" data-action="cancel" data-slot="${t.slot}" type="button" aria-label="Cancel ${ktEsc(t.label)}">
              <ha-icon icon="mdi:close"></ha-icon><span class="kt-x-text">Cancel?</span>
            </button>
            <div class="kt-row-bar"><div class="kt-bar-fill"></div></div>
          </div>`
        )
        .join("")}</div>`;
    }
    timers.forEach((t) => {
      const root = body.querySelector(`[data-slot="${t.slot}"]`);
      if (!root) return;
      this._rows[t.slot] = {
        root,
        digits: root.querySelector(".kt-digits, .kt-row-digits"),
        meta: root.querySelector(".kt-meta, .kt-row-meta"),
        fill: root.querySelector(".kt-bar-fill"),
        cancel: root.querySelector(".kt-cancel"),
      };
      this._paintConfirm(t.slot);
    });
  }

  _update() {
    if (!this._bound || !this._hass) return;
    const timers = this._timers();
    this._layout(timers);
    timers.forEach((t) => {
      const row = this._rows[t.slot];
      if (!row || t.mode !== "run") return;
      const secs = Math.ceil(t.left - 0.05);
      if (row.digits) {
        row.digits.textContent = ktDigits(secs);
        row.digits.classList.toggle("long", secs >= 3600);
        row.digits.classList.toggle("dim", t.paused);
      }
      if (row.meta) {
        const metaKey = `${t.paused}|${t.total}|${t.finishesAt}`;
        if (row.metaKey !== metaKey) {
          row.metaKey = metaKey;
          row.meta.textContent = t.paused
            ? `${ktTotalLabel(t.total)} · paused`
            : `${ktTotalLabel(t.total)} · done at ${this._clock(t.finishesAt)}`;
        }
      }
      if (row.fill) {
        const frac = t.total > 0 ? Math.min(1, Math.max(0, 1 - t.left / t.total)) : 0;
        row.fill.style.width = `${(frac * 100).toFixed(2)}%`;
      }
    });

    const anyRing = timers.some((t) => t.ringing);
    const blocked =
      anyRing && ktChimeAllowed(this._config, this._hass) && ktAudioState() !== "running";
    this._el.muted.hidden = !blocked;

    // Tick while anything is on screen: the digits while running, the muted chip while
    // ringing (a tap elsewhere on the page can unblock audio without a state change).
    if (timers.length) this._startTick();
    else this._stopTick();
  }

  _clock(iso) {
    if (!iso) return "";
    try {
      const tz = this._hass && this._hass.config && this._hass.config.time_zone;
      return new Intl.DateTimeFormat("en-US", {
        hour: "numeric",
        minute: "2-digit",
        timeZone: tz || undefined,
      }).format(new Date(iso));
    } catch (err) {
      return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    }
  }

  _startTick() {
    if (this._tick) return;
    this._tick = setInterval(() => this._update(), 250);
  }

  _stopTick() {
    if (!this._tick) return;
    clearInterval(this._tick);
    this._tick = null;
  }

  // -------------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------------

  _bindEvents() {
    const root = this.shadowRoot;
    // A tap with a little finger drift still counts (the dashboard does not scroll);
    // only a real drag (> 16 px) or a touchcancel drops it. No preventDefault: the
    // touchActive gate already stops the click that follows a handled touch.
    let touchActive = false;
    let touchCancelled = false;
    let startX = 0;
    let startY = 0;
    const findTarget = (e) => {
      const path = e.composedPath ? e.composedPath() : [];
      for (const node of path) {
        if (node === root) break;
        if (node && node.dataset && node.dataset.action) return node;
      }
      return null;
    };
    const point = (e) => (e.changedTouches && e.changedTouches[0]) || (e.touches && e.touches[0]) || null;
    root.addEventListener("touchcancel", () => { touchCancelled = true; }, { passive: true });
    root.addEventListener("touchstart", (e) => {
      touchActive = true;
      touchCancelled = false;
      const p = point(e);
      startX = p ? p.clientX : 0;
      startY = p ? p.clientY : 0;
    }, { passive: true });
    root.addEventListener("touchend", (e) => {
      const p = point(e);
      const moved = p ? Math.hypot(p.clientX - startX, p.clientY - startY) > 16 : false;
      const el = findTarget(e);
      setTimeout(() => { touchActive = false; }, 400);
      if (touchCancelled || moved || !el) return;
      this._dispatch(el.dataset.action, Number(el.dataset.slot) || 0);
    });
    root.addEventListener("click", (e) => {
      if (touchActive) return;
      const el = findTarget(e);
      if (el) this._dispatch(el.dataset.action, Number(el.dataset.slot) || 0);
    });
  }

  _dispatch(action, slot) {
    if (action === "unmute") {
      const ctx = ktAudioCtx();
      if (ctx && ctx.state !== "running") {
        ctx.resume().then(() => this._update()).catch(() => {});
      }
      return;
    }
    if (!slot) return;
    const t = this._timers().find((x) => x.slot === slot);
    if (action === "stop") {
      // Silence this ring here at once; the relay turns it off for every screen.
      if (t && t.ringEpisode) KT.silenced[t.ringEntity] = t.ringEpisode;
      this._callRelay("stop", { slot });
      return;
    }
    if (action === "add") {
      this._callRelay("add", { slot, seconds: 60 });
      return;
    }
    if (action === "cancel") {
      if (Date.now() < (this._confirm[slot] || 0)) {
        this._clearConfirm(slot);
        this._callRelay("cancel", { slot });
        return;
      }
      this._confirm[slot] = Date.now() + KT_CONFIRM_MS;
      clearTimeout(this._confirmTimers[slot]);
      this._confirmTimers[slot] = setTimeout(() => this._clearConfirm(slot), KT_CONFIRM_MS);
      this._paintConfirm(slot);
    }
  }

  _clearConfirm(slot) {
    delete this._confirm[slot];
    clearTimeout(this._confirmTimers[slot]);
    delete this._confirmTimers[slot];
    this._paintConfirm(slot);
  }

  _paintConfirm(slot) {
    const row = this._rows[slot];
    if (!row || !row.cancel) return;
    const on = Date.now() < (this._confirm[slot] || 0);
    row.cancel.classList.toggle("confirm", on);
    if (row.cancel.classList.contains("kt-btn")) {
      row.cancel.textContent = on ? "Tap again to cancel" : "Cancel";
    }
  }

  _callRelay(command, payload) {
    if (!this._hass) return;
    this._hass
      .callService("script", this._config.relay_script, {
        command,
        payload: JSON.stringify(payload || {}),
      })
      .catch((err) => {
        console.warn("kitchen-timer-card: relay failed", command, err);
      });
  }

  // -------------------------------------------------------------------------
  // Styles
  // -------------------------------------------------------------------------

  _styles() {
    return `
      :host { display: block; touch-action: pan-y; }
      .kt-card {
        position: relative;
        width: 100%;
        aspect-ratio: 16 / 9;
        box-sizing: border-box;
        overflow: hidden;
        border-radius: 12px;
        background: var(--ha-card-background, var(--card-background-color, #1c2230));
        color: var(--primary-text-color, #fff);
        font-family: var(--ha-font-family-body, Roboto, "Noto Sans", sans-serif);
        -webkit-tap-highlight-color: transparent;
        user-select: none;
      }
      .kt-body { position: absolute; inset: 0; }
      button { font: inherit; color: inherit; border: 0; cursor: pointer; touch-action: manipulation; }

      /* ---- one timer: the big layout ---- */
      .kt-hero { position: absolute; inset: 0; display: flex; flex-direction: column; padding: 16px 18px; box-sizing: border-box; }
      .kt-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; }
      .kt-title {
        display: flex; align-items: center; gap: 6px; min-width: 0;
        font-size: 14px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase;
        color: var(--secondary-text-color, rgba(255, 255, 255, 0.75));
      }
      .kt-title span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .kt-title ha-icon { --mdc-icon-size: 18px; flex: none; }
      .kt-meta {
        flex: none; font-size: 14px; color: var(--secondary-text-color, rgba(255, 255, 255, 0.65));
        white-space: nowrap;
      }
      .kt-digits {
        flex: 1; display: flex; align-items: center; justify-content: center;
        font-size: 104px; font-weight: 800; line-height: 1; letter-spacing: -2px;
        font-variant-numeric: tabular-nums;
      }
      .kt-digits.long { font-size: 84px; }
      .dim { opacity: 0.5; }
      .kt-bar {
        height: 8px; border-radius: 4px; overflow: hidden;
        background: rgba(255, 255, 255, 0.12); margin: 4px 0 14px;
      }
      .kt-bar-fill { height: 100%; width: 0; background: #f59e0b; border-radius: 4px; }
      .kt-buttons { display: flex; gap: 10px; }
      .kt-btn {
        flex: 1; height: 40px; border-radius: 20px;
        background: rgba(255, 255, 255, 0.12);
        font-size: 16px; font-weight: 700;
      }
      .kt-btn:active, .kt-pill:active, .kt-x:active { background: rgba(255, 255, 255, 0.22); }
      .kt-btn.confirm { background: #b91c1c; color: #fff; }

      .kt-hero-ring {
        position: absolute; inset: 0;
        display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
        background: linear-gradient(160deg, #c2410c 0%, #9a3412 100%);
        border: 3px solid #f97316; border-radius: 12px; box-sizing: border-box;
        color: #fff; cursor: pointer; text-align: center; padding: 10px 16px;
        animation: kt-pulse 1.6s ease-in-out infinite;
      }
      .kt-hero-ring .kt-bell { --mdc-icon-size: 52px; }
      .kt-done { font-size: 40px; font-weight: 800; line-height: 1.1; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .kt-hint { font-size: 17px; opacity: 0.95; }
      .kt-say { font-size: 13px; opacity: 0.75; margin-top: 4px; }

      /* ---- two or three timers: one row each ---- */
      .kt-rows {
        position: absolute; inset: 0; padding: 10px; box-sizing: border-box;
        display: flex; flex-direction: column; gap: 8px;
      }
      .count-3 .kt-rows { gap: 6px; padding: 8px; }
      .kt-row {
        position: relative; flex: 1 1 0; min-height: 0;
        display: flex; align-items: center; gap: 10px;
        padding: 0 12px 0 14px; box-sizing: border-box; overflow: hidden;
        border-radius: 10px; background: rgba(255, 255, 255, 0.06);
      }
      .kt-row-name { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
      .kt-row-label {
        font-size: 19px; font-weight: 700; line-height: 1.15;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .kt-row-meta {
        font-size: 13px; color: var(--secondary-text-color, rgba(255, 255, 255, 0.65));
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .kt-row-digits {
        flex: none; font-size: 50px; font-weight: 800; line-height: 1; letter-spacing: -1px;
        font-variant-numeric: tabular-nums;
      }
      .kt-row-digits.long { font-size: 38px; }
      .kt-pill {
        flex: none; height: 44px; padding: 0 14px; border-radius: 22px;
        background: rgba(255, 255, 255, 0.12); font-size: 15px; font-weight: 700; white-space: nowrap;
      }
      .kt-x {
        flex: none; height: 44px; min-width: 44px; padding: 0; border-radius: 22px;
        display: flex; align-items: center; justify-content: center; gap: 4px;
        background: rgba(255, 255, 255, 0.12);
      }
      .kt-x ha-icon { --mdc-icon-size: 22px; }
      .kt-x .kt-x-text { display: none; font-size: 14px; font-weight: 700; }
      .kt-x.confirm { background: #b91c1c; color: #fff; padding: 0 12px; }
      .kt-x.confirm ha-icon { display: none; }
      .kt-x.confirm .kt-x-text { display: inline; }
      .kt-row-bar {
        position: absolute; left: 0; right: 0; bottom: 0; height: 4px;
        background: rgba(255, 255, 255, 0.08);
      }
      .kt-row-bar .kt-bar-fill { border-radius: 0 2px 2px 0; }

      .count-3 .kt-row { gap: 8px; padding: 0 10px 0 12px; }
      .count-3 .kt-row-label { font-size: 17px; }
      .count-3 .kt-row-meta { font-size: 12px; }
      .count-3 .kt-row-digits { font-size: 38px; }
      .count-3 .kt-row-digits.long { font-size: 30px; }
      .count-3 .kt-pill { height: 40px; padding: 0 12px; font-size: 14px; }
      .count-3 .kt-x { height: 40px; min-width: 40px; }

      .kt-row.ring {
        justify-content: flex-start; gap: 10px; cursor: pointer;
        background: linear-gradient(160deg, #c2410c 0%, #9a3412 100%);
        border: 2px solid #f97316; color: #fff;
        animation: kt-pulse 1.6s ease-in-out infinite;
      }
      .kt-row.ring .kt-bell { --mdc-icon-size: 30px; flex: none; }
      .count-3 .kt-row.ring .kt-bell { --mdc-icon-size: 26px; }
      .kt-row-done {
        flex: 1 1 auto; min-width: 0; font-size: 24px; font-weight: 800;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .count-3 .kt-row-done { font-size: 21px; }
      .kt-row-hint { flex: none; font-size: 15px; font-weight: 600; opacity: 0.9; }

      .kt-bell { color: #fde68a; animation: kt-shake 1.6s ease-in-out infinite; }
      @keyframes kt-pulse {
        0%, 100% { border-color: #f97316; filter: brightness(1); }
        50% { border-color: #fdba74; filter: brightness(1.15); }
      }
      @keyframes kt-shake {
        0%, 60%, 100% { transform: rotate(0deg); }
        10%, 30%, 50% { transform: rotate(-12deg); }
        20%, 40% { transform: rotate(12deg); }
      }

      .kt-idle {
        position: absolute; inset: 0;
        display: flex; align-items: center; justify-content: center; gap: 8px;
        color: var(--secondary-text-color, rgba(255, 255, 255, 0.6)); font-size: 16px;
      }

      .kt-muted {
        position: absolute; top: 10px; right: 10px; z-index: 2;
        display: flex; align-items: center; gap: 4px;
        padding: 4px 10px; border-radius: 14px;
        background: rgba(0, 0, 0, 0.55); color: #fff; font-size: 12px; cursor: pointer;
      }
      .kt-muted[hidden] { display: none; }
      .kt-muted ha-icon { --mdc-icon-size: 16px; }
      .count-1 .kt-muted { top: 40px; }
    `;
  }
}

/** Display label: the one unnamed timer is the "Kitchen timer"; "Timer 2" and named timers stay as they are. */
function ktLabel(name) {
  return name === "Timer" ? "Kitchen timer" : name;
}

function ktDoneText(name) {
  if (name === "Timer") return "Kitchen timer done";
  if (/^Timer \d+$/.test(name)) return `${name} done`;
  return `${name} timer done`;
}

function ktEsc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function ktParseDuration(value) {
  if (value == null || value === "") return 0;
  if (typeof value === "number") return value;
  const str = String(value).trim();
  let days = 0;
  let rest = str;
  const dayMatch = str.match(/^(\d+)\s+days?,\s*(.*)$/);
  if (dayMatch) {
    days = Number(dayMatch[1]);
    rest = dayMatch[2];
  }
  const parts = rest.split(":").map(Number);
  if (parts.some((n) => Number.isNaN(n))) return 0;
  while (parts.length < 3) parts.unshift(0);
  return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2];
}

function ktDigits(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function ktTotalLabel(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return `${s} sec`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  if (h === 0) return `${m} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}

if (!customElements.get("kitchen-timer-card")) {
  customElements.define("kitchen-timer-card", KitchenTimerCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "kitchen-timer-card",
    name: "Kitchen Timer Card",
    description: "Up to three kitchen timers with a chime when one runs out (kitchen Connect display)",
  });
}
