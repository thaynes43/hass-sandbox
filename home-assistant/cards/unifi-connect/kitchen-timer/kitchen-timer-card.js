/**
 * Kitchen Timer Card (hass-sandbox #262)
 *
 * The oven timer on the kitchen UniFi Connect display. It swaps into the Disney
 * countdown slot of the unifi-connect Home view (a conditional pair: the countdown
 * shows while the timer is idle, this card while it runs, is paused or rings), so
 * it keeps the slot's exact footprint: 16:9, 440 x 248 at 1920 x 1080.
 *
 *   running  big mm:ss from finishes_at, progress bar, "+1 min", "Cancel" (second tap
 *            within 3 s confirms, so a brush cannot kill the timer)
 *   paused   the same, digits dimmed
 *   ringing  amber "Oven timer done"; a tap anywhere on the card stops it
 *
 * Reads:  timer.oven, input_boolean.oven_timer_ringing (live-only helpers)
 * Writes: script.kitchen_timer_relay {command: add | cancel | stop, payload} (relay
 *         pattern; never fire_event, which fails for the non-admin display user)
 *
 * Chime: Web Audio, synthesized (the CSP has no data: in media-src). It plays every
 * chime_interval_s while ringing is on, for at most max_ring_s, and only on screens
 * whose own IP is in chime_ips (the current refresh token's last_used_ip, from
 * auth/refresh_tokens). The ringer lives at module level and reads the root
 * <home-assistant> element's hass, so it keeps ringing while the display sits on a
 * sub-view and this card is not in the DOM. Any tap on the page resumes a suspended
 * AudioContext, and the card shows a "Tap for sound" chip while audio is blocked.
 * Once per page load the card writes one logbook line (screen IP, viewport, audio
 * state, path) under timer.oven.
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
  timer_entity: "timer.oven",
  ringing_entity: "input_boolean.oven_timer_ringing",
  relay_script: "kitchen_timer_relay",
  title: "Oven timer",
  chime: true,
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
  episode: null,
  episodeSeenAt: 0,
  silencedEpisode: null,
  ipPromise: null,
  loadLogged: false,
  allowed: null,
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

function ktRingerTick() {
  const cfg = KT.cfg;
  const hass = ktRootHass();
  if (!cfg || !hass || !hass.states) return;
  // While the websocket is down the states are stale: a ring stopped elsewhere would keep chiming.
  if (hass.connected === false) return;
  const ring = hass.states[cfg.ringing_entity];
  if (!ring || ring.state !== "on") {
    KT.episode = null;
    KT.lastChime = 0;
    return;
  }
  // Episode = this run of "on". Measured with the local clock from when this page
  // first saw it, so a skewed display clock cannot cut the cap short.
  if (KT.episode !== ring.last_changed) {
    KT.episode = ring.last_changed;
    KT.episodeSeenAt = Date.now();
    KT.lastChime = 0;
  }
  if (KT.silencedEpisode === KT.episode) return;
  const now = Date.now();
  if (now - KT.episodeSeenAt > cfg.max_ring_s * 1000) return;
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
        return (current && current.last_used_ip) || "";
      })
      .catch(() => "");
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
    this._domBuilt = false;
    this._tick = null;
    this._confirmUntil = 0;
    this._confirmTimer = null;
    this._lastKey = "";
  }

  setConfig(config) {
    const merged = { ...KT_DEFAULTS, ...(config || {}) };
    merged.chime_ips = Array.isArray(merged.chime_ips)
      ? merged.chime_ips.map((ip) => String(ip).trim()).filter(Boolean)
      : [];
    merged.chime_interval_s = Math.max(2, Number(merged.chime_interval_s) || 5);
    merged.max_ring_s = Math.max(10, Number(merged.max_ring_s) || 600);
    this._config = merged;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    KT.hass = hass;
    if (!this._domBuilt) this._buildDom();
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
    return { timer_entity: KT_DEFAULTS.timer_entity };
  }

  connectedCallback() {
    if (this._hass) this._update();
  }

  disconnectedCallback() {
    this._stopTick();
    this._clearConfirm();
  }

  // -------------------------------------------------------------------------
  // Screen gating (chime only where configured) + the once-per-page load log
  // -------------------------------------------------------------------------

  _initScreen() {
    const cfg = this._config;
    const hass = this._hass;
    ktScreenIp(hass).then((ip) => {
      const allowed =
        !!cfg.chime && (cfg.chime_ips.length === 0 || cfg.chime_ips.indexOf(ip) !== -1);
      if (allowed) {
        KT.allowed = true;
        ktArmRinger(cfg);
      } else if (KT.allowed === null) {
        KT.allowed = false;
      }
      this._update();
      if (cfg.log_load && !KT.loadLogged) {
        KT.loadLogged = true;
        const dpr = Math.round((window.devicePixelRatio || 1) * 100) / 100;
        const message =
          `loaded on ${ip || "unknown IP"} (chime ${allowed ? "on" : "off"}), ` +
          `${window.innerWidth}x${window.innerHeight} @${dpr}x, ` +
          `audio ${ktAudioState()}, ${location.pathname}`;
        hass
          .callService("logbook", "log", {
            name: "Kitchen timer card",
            message,
            entity_id: cfg.timer_entity,
          })
          .catch(() => {});
      }
    });
  }

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------

  _view() {
    const hass = this._hass;
    const cfg = this._config;
    const timer = hass && hass.states[cfg.timer_entity];
    const ring = hass && hass.states[cfg.ringing_entity];
    const ringing = !!ring && ring.state === "on";
    const state = timer ? timer.state : "unavailable";
    const attrs = (timer && timer.attributes) || {};
    const total = ktParseDuration(attrs.duration);
    let left = 0;
    if (state === "active" && attrs.finishes_at) {
      left = Math.max(0, (Date.parse(attrs.finishes_at) - Date.now()) / 1000);
    } else if (state === "paused") {
      left = ktParseDuration(attrs.remaining);
    }
    return {
      mode: ringing ? "ring" : state === "active" || state === "paused" ? "run" : "idle",
      paused: state === "paused",
      total,
      left,
      finishesAt: attrs.finishes_at || null,
      ringEpisode: ring ? ring.last_changed : null,
    };
  }

  // -------------------------------------------------------------------------
  // DOM
  // -------------------------------------------------------------------------

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="kt-card mode-idle">
        <div class="kt-run">
          <div class="kt-head">
            <div class="kt-title"><ha-icon icon="mdi:timer-outline"></ha-icon><span class="kt-title-text"></span></div>
            <div class="kt-meta"></div>
          </div>
          <div class="kt-digits">0:00</div>
          <div class="kt-bar"><div class="kt-bar-fill"></div></div>
          <div class="kt-buttons">
            <button class="kt-btn" data-action="add" type="button">+1 min</button>
            <button class="kt-btn kt-cancel" data-action="cancel" type="button">Cancel</button>
          </div>
        </div>
        <div class="kt-ring" data-action="stop">
          <ha-icon class="kt-bell" icon="mdi:bell-ring"></ha-icon>
          <div class="kt-done"></div>
          <div class="kt-hint">Tap anywhere on this card to stop</div>
          <div class="kt-say">or say “stop the oven timer”</div>
        </div>
        <div class="kt-idle">
          <ha-icon icon="mdi:timer-off-outline"></ha-icon>
          <span>No oven timer running</span>
        </div>
        <div class="kt-muted" data-action="unmute" hidden>
          <ha-icon icon="mdi:volume-off"></ha-icon><span>Tap for sound</span>
        </div>
      </div>
    `;
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    this._el = {
      card: $(".kt-card"),
      titleText: $(".kt-title-text"),
      meta: $(".kt-meta"),
      digits: $(".kt-digits"),
      fill: $(".kt-bar-fill"),
      cancel: $(".kt-cancel"),
      done: $(".kt-done"),
      muted: $(".kt-muted"),
    };
    this._bindEvents();
    this._domBuilt = true;
  }

  _update() {
    if (!this._domBuilt || !this._hass) return;
    const cfg = this._config;
    const v = this._view();
    const el = this._el;
    el.card.className = `kt-card mode-${v.mode}${v.paused ? " paused" : ""}`;
    el.titleText.textContent = cfg.title;
    el.done.textContent = `${cfg.title} done`;

    const key = `${v.mode}|${v.paused}|${v.total}|${v.finishesAt}`;
    if (key !== this._lastKey) {
      this._lastKey = key;
      if (v.mode !== "run") this._clearConfirm();
      el.meta.textContent =
        v.mode !== "run"
          ? ""
          : v.paused
            ? `${ktTotalLabel(v.total)} · paused`
            : `${ktTotalLabel(v.total)} · done at ${this._clock(v.finishesAt)}`;
    }

    const secs = Math.ceil(v.left - 0.05);
    el.digits.textContent = ktDigits(secs);
    el.digits.classList.toggle("long", secs >= 3600);
    const frac = v.total > 0 ? Math.min(1, Math.max(0, 1 - v.left / v.total)) : 0;
    el.fill.style.width = `${(frac * 100).toFixed(2)}%`;

    const blocked = KT.allowed === true && ktAudioState() !== "running" && v.mode !== "idle";
    el.muted.hidden = !blocked;

    // Tick while anything is on screen: the digits while running, the muted chip while
    // ringing (a tap elsewhere on the page can unblock audio without a state change).
    if (v.mode !== "idle") this._startTick();
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
      this._dispatch(el.dataset.action);
    });
    root.addEventListener("click", (e) => {
      if (touchActive) return;
      const el = findTarget(e);
      if (el) this._dispatch(el.dataset.action);
    });
  }

  _dispatch(action) {
    const v = this._view();
    if (action === "unmute") {
      const ctx = ktAudioCtx();
      if (ctx && ctx.state !== "running") {
        ctx.resume().then(() => this._update()).catch(() => {});
      }
      return;
    }
    if (action === "stop") {
      // Silence this ring here at once; the relay turns ringing off for every screen.
      KT.silencedEpisode = v.ringEpisode;
      this._callRelay("stop");
      return;
    }
    if (action === "add") {
      this._callRelay("add", { seconds: 60 });
      return;
    }
    if (action === "cancel") {
      if (Date.now() < this._confirmUntil) {
        this._clearConfirm();
        this._callRelay("cancel");
        return;
      }
      this._confirmUntil = Date.now() + KT_CONFIRM_MS;
      this._el.cancel.classList.add("confirm");
      this._el.cancel.textContent = "Tap again to cancel";
      clearTimeout(this._confirmTimer);
      this._confirmTimer = setTimeout(() => this._clearConfirm(), KT_CONFIRM_MS);
    }
  }

  _clearConfirm() {
    this._confirmUntil = 0;
    clearTimeout(this._confirmTimer);
    this._confirmTimer = null;
    if (this._el && this._el.cancel) {
      this._el.cancel.classList.remove("confirm");
      this._el.cancel.textContent = "Cancel";
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
      .kt-run, .kt-ring, .kt-idle { position: absolute; inset: 0; display: none; }
      .mode-run .kt-run, .mode-ring .kt-ring, .mode-idle .kt-idle { display: flex; }

      .kt-run { flex-direction: column; padding: 16px 18px 16px; box-sizing: border-box; }
      .kt-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
      .kt-title {
        display: flex; align-items: center; gap: 6px;
        font-size: 14px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase;
        color: var(--secondary-text-color, rgba(255, 255, 255, 0.75));
      }
      .kt-title ha-icon { --mdc-icon-size: 18px; }
      .kt-meta {
        font-size: 14px; color: var(--secondary-text-color, rgba(255, 255, 255, 0.65));
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .kt-digits {
        flex: 1; display: flex; align-items: center; justify-content: center;
        font-size: 104px; font-weight: 800; line-height: 1; letter-spacing: -2px;
        font-variant-numeric: tabular-nums;
      }
      .kt-digits.long { font-size: 84px; }
      .paused .kt-digits { opacity: 0.5; }
      .kt-bar {
        height: 8px; border-radius: 4px; overflow: hidden;
        background: rgba(255, 255, 255, 0.12); margin: 4px 0 14px;
      }
      .kt-bar-fill { height: 100%; width: 0; background: #f59e0b; border-radius: 4px; }
      .kt-buttons { display: flex; gap: 10px; }
      .kt-btn {
        flex: 1; height: 40px; border: 0; border-radius: 20px;
        background: rgba(255, 255, 255, 0.12); color: inherit;
        font: inherit; font-size: 16px; font-weight: 700; cursor: pointer;
        touch-action: manipulation;
      }
      .kt-btn:active { background: rgba(255, 255, 255, 0.22); }
      .kt-btn.confirm { background: #b91c1c; color: #fff; }

      .kt-ring {
        flex-direction: column; align-items: center; justify-content: center; gap: 6px;
        background: linear-gradient(160deg, #c2410c 0%, #9a3412 100%);
        border: 3px solid #f97316; border-radius: 12px; box-sizing: border-box;
        color: #fff; cursor: pointer; text-align: center; padding: 10px 16px;
        animation: kt-pulse 1.6s ease-in-out infinite;
      }
      .kt-bell { --mdc-icon-size: 52px; color: #fde68a; animation: kt-shake 1.6s ease-in-out infinite; }
      .kt-done { font-size: 40px; font-weight: 800; line-height: 1.1; }
      .kt-hint { font-size: 17px; opacity: 0.95; }
      .kt-say { font-size: 13px; opacity: 0.75; margin-top: 4px; }
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
        align-items: center; justify-content: center; gap: 8px;
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
      .mode-run .kt-muted { top: 40px; }
    `;
  }
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
    description: "Oven timer countdown with a chime when it runs out (kitchen Connect display)",
  });
}
