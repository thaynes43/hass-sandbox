/**
 * Countdown Card
 *
 * Displays countdown timers with AI-generated background images.
 * Auto-rotates through multiple countdowns; supports swipe navigation.
 * Tap opens the configuration popup.
 *
 * Reads:  sensor.countdown_status
 *
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App,
 *            Android/UniFi wall display.
 */

const CD_DEFAULTS = {
  status_entity: "sensor.countdown_status",
  navigation_path: "#countdown-popup",
};

class CountdownCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...CD_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._localIndex = 0;
    this._activeLayer = "a";
    this._rotateTimer = null;
    this._fadeInProgress = false;
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...CD_DEFAULTS, ...config };
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;

    const snap = this._snapshot();
    if (!firstSet && snap === this._lastSnapshot) return;
    this._lastSnapshot = snap;

    // Focus guard — skip re-render if an input has focus
    const active = this.shadowRoot?.activeElement;
    if (
      active &&
      (active.tagName === "INPUT" || active.tagName === "TEXTAREA")
    ) {
      return;
    }

    if (!this._domBuilt) {
      this._buildDom();
      this._bindEvents();
      this._update();
      return;
    }

    this._update();
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return { ...CD_DEFAULTS };
  }

  connectedCallback() {
    super.connectedCallback?.();
    this._startRotate();
  }

  disconnectedCallback() {
    super.disconnectedCallback?.();
    this._stopRotate();
  }

  // ---------------------------------------------------------------------------
  // Snapshot — re-render only when relevant state changes
  // ---------------------------------------------------------------------------

  _snapshot() {
    if (!this._hass) return null;
    const s = this._hass.states[this._config.status_entity];
    if (!s) return null;
    const a = s.attributes || {};
    return JSON.stringify({
      state: s.state,
      active: a.active_countdown?.id,
      countdowns: (a.countdowns || []).map((c) => ({
        id: c.id,
        text: c.countdown_text,
        img: c.image_url,
      })),
    });
  }

  // ---------------------------------------------------------------------------
  // Build DOM once
  // ---------------------------------------------------------------------------

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="cd-card" data-action="open-popup">
        <div class="cd-layer cd-layer-a">
          <img class="cd-bg" alt="" />
          <div class="cd-overlay">
            <div class="cd-header-text">
              <div class="cd-title"></div>
              <div class="cd-subtitle"></div>
            </div>
            <div class="cd-countdown-anchor">
              <div class="cd-countdown-text"></div>
            </div>
          </div>
        </div>
        <div class="cd-layer cd-layer-b" style="opacity:0;">
          <img class="cd-bg" alt="" />
          <div class="cd-overlay">
            <div class="cd-header-text">
              <div class="cd-title"></div>
              <div class="cd-subtitle"></div>
            </div>
            <div class="cd-countdown-anchor">
              <div class="cd-countdown-text"></div>
            </div>
          </div>
        </div>
        <div class="cd-dots"></div>
      </div>
    `;

    this._els = {
      card: this.shadowRoot.querySelector(".cd-card"),
      layerA: this.shadowRoot.querySelector(".cd-layer-a"),
      layerB: this.shadowRoot.querySelector(".cd-layer-b"),
      dots: this.shadowRoot.querySelector(".cd-dots"),
    };

    this._domBuilt = true;
  }

  // ---------------------------------------------------------------------------
  // Update DOM in place (no layer swap — just refresh active layer content)
  // ---------------------------------------------------------------------------

  _update() {
    if (!this._els) return;

    const s = this._hass?.states[this._config.status_entity];
    if (!s || !s.attributes) {
      this._showEmpty();
      return;
    }

    const attrs = s.attributes;
    const visible = (attrs.countdowns || []).filter((c) => c.countdown_text);

    if (visible.length === 0) {
      this._showEmpty();
      return;
    }

    // Clamp index in case list shrank
    if (this._localIndex >= visible.length) {
      this._localIndex = 0;
    }

    const idx = this._localIndex % visible.length;
    const countdown = visible[idx];

    // Paint the active layer directly (no transition — this is a data refresh)
    this._paintLayer(this._activeLayer, countdown);

    // Update dots
    this._updateDots(visible.length, idx);
  }

  _showEmpty() {
    if (!this._els) return;
    const layer = this._getLayerEl(this._activeLayer);
    if (!layer) return;

    const bg = layer.querySelector(".cd-bg");
    bg.removeAttribute("src");
    bg.style.display = "none";

    const title = layer.querySelector(".cd-title");
    const subtitle = layer.querySelector(".cd-subtitle");
    const textEl = layer.querySelector(".cd-countdown-text");
    const anchor = layer.querySelector(".cd-countdown-anchor");

    title.textContent = "No countdowns configured";
    subtitle.textContent = "";
    textEl.textContent = "";
    textEl.className = "cd-countdown-text";
    textEl.style.cssText = "";
    anchor.style.top = "70%";

    this._els.dots.innerHTML = "";
  }

  // ---------------------------------------------------------------------------
  // Layer painting helpers
  // ---------------------------------------------------------------------------

  _getLayerEl(which) {
    return which === "a" ? this._els.layerA : this._els.layerB;
  }

  _getInactiveLayer() {
    return this._activeLayer === "a" ? "b" : "a";
  }

  _paintLayer(which, countdown) {
    const layer = this._getLayerEl(which);
    if (!layer) return;

    const bg = layer.querySelector(".cd-bg");
    const title = layer.querySelector(".cd-title");
    const subtitle = layer.querySelector(".cd-subtitle");
    const textEl = layer.querySelector(".cd-countdown-text");
    const anchor = layer.querySelector(".cd-countdown-anchor");

    // Background image
    if (countdown.image_url) {
      bg.style.display = "";
      if (bg.getAttribute("src") !== countdown.image_url) {
        bg.src = countdown.image_url;
      }
    } else {
      bg.removeAttribute("src");
      bg.style.display = "none";
    }

    // Title / subtitle
    title.textContent = countdown.title || "";
    subtitle.textContent = countdown.subtitle || "";

    // Countdown text + style
    const style = countdown.text_style || {};
    textEl.textContent = countdown.countdown_text || "";
    textEl.style.fontSize = (style.font_size || 50) + "px";
    textEl.style.color = style.color || "#ffd24a";

    if (style.text_shadow !== false) {
      textEl.style.textShadow =
        "-3px -3px 6px rgba(0,0,0,0.9), " +
        "3px -3px 6px rgba(0,0,0,0.9), " +
        "-3px 3px 6px rgba(0,0,0,0.9), " +
        "3px 3px 6px rgba(0,0,0,0.9), " +
        "0 0 10px rgba(255,210,74,0.9), " +
        "0 0 22px rgba(255,160,40,0.7), " +
        "0 0 32px rgba(255,120,20,0.5)";
    } else {
      textEl.style.textShadow = "none";
    }

    // NOW pulse animation
    const isNow = countdown.countdown_text === "NOW";
    textEl.classList.toggle("cd-now", isNow);

    // Vertical position of countdown text via the anchor div
    const posY = style.position_y != null ? style.position_y : 70;
    anchor.style.top = posY + "%";
  }

  // ---------------------------------------------------------------------------
  // Cross-fade transition
  // ---------------------------------------------------------------------------

  _crossFadeTo(idx) {
    if (!this._els || !this._domBuilt) return;
    if (this._fadeInProgress) {
      // Already fading — just update index so next paint is correct
      this._localIndex = idx;
      return;
    }

    const s = this._hass?.states[this._config.status_entity];
    const visible = ((s?.attributes?.countdowns) || []).filter((c) => c.countdown_text);
    if (visible.length === 0) return;

    const countdown = visible[idx % visible.length];
    const inactiveKey = this._getInactiveLayer();
    const inactiveEl = this._getLayerEl(inactiveKey);
    const activeEl = this._getLayerEl(this._activeLayer);

    // Populate the inactive layer (hidden behind active)
    this._paintLayer(inactiveKey, countdown);

    // Update dots immediately (feels more responsive)
    this._updateDots(visible.length, idx % visible.length);

    // Preload background before fading in
    const imageUrl = countdown.image_url || "";
    const doFade = () => {
      this._fadeInProgress = true;
      inactiveEl.style.opacity = "1";
      activeEl.style.opacity = "0";

      setTimeout(() => {
        // Swap active tracking
        this._activeLayer = inactiveKey;
        this._fadeInProgress = false;
      }, 650); // slightly longer than CSS transition (0.6s)
    };

    if (imageUrl) {
      const probe = new Image();
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        doFade();
      };
      probe.onload = finish;
      probe.onerror = finish; // fade even if image fails
      probe.src = imageUrl;
      // If already cached
      if (probe.complete && probe.naturalWidth > 0) finish();
    } else {
      doFade();
    }
  }

  // ---------------------------------------------------------------------------
  // Navigation dots
  // ---------------------------------------------------------------------------

  _updateDots(total, activeIdx) {
    if (!this._els) return;
    if (total <= 1) {
      this._els.dots.innerHTML = "";
      return;
    }

    let html = "";
    for (let i = 0; i < total; i++) {
      html += `<div class="cd-dot${i === activeIdx ? " active" : ""}"></div>`;
    }
    this._els.dots.innerHTML = html;
  }

  // ---------------------------------------------------------------------------
  // Auto-rotate
  // ---------------------------------------------------------------------------

  _startRotate() {
    this._stopRotate();
    const interval = (this._config.auto_rotate_s || 15) * 1000;
    this._rotateTimer = setInterval(() => this._rotateNext(), interval);
  }

  _stopRotate() {
    if (this._rotateTimer) {
      clearInterval(this._rotateTimer);
      this._rotateTimer = null;
    }
  }

  _rotateNext() {
    const s = this._hass?.states[this._config.status_entity];
    const visible = ((s?.attributes?.countdowns) || []).filter((c) => c.countdown_text);
    if (visible.length <= 1) return;
    this._localIndex = ((this._localIndex || 0) + 1) % visible.length;
    this._crossFadeTo(this._localIndex);
  }

  _rotatePrev() {
    const s = this._hass?.states[this._config.status_entity];
    const visible = ((s?.attributes?.countdowns) || []).filter((c) => c.countdown_text);
    if (visible.length <= 1) return;
    this._localIndex = ((this._localIndex || 0) - 1 + visible.length) % visible.length;
    this._crossFadeTo(this._localIndex);
  }

  // ---------------------------------------------------------------------------
  // Touch / click event handling with deduplication
  // ---------------------------------------------------------------------------

  _bindEvents() {
    const root = this.shadowRoot;
    let touchActive = false;
    let touchCancelled = false;
    let touchStartY = null;

    ["touchcancel", "touchmove", "scroll"].forEach((evt) => {
      root.addEventListener(
        evt,
        () => {
          touchCancelled = true;
        },
        { passive: true }
      );
    });

    root.addEventListener(
      "touchstart",
      (e) => {
        touchActive = true;
        touchCancelled = false;
        touchStartY = e.touches[0]?.clientY ?? null;
      },
      { passive: true }
    );

    root.addEventListener("touchend", (e) => {
      if (touchCancelled) {
        touchActive = false;
        touchStartY = null;
        return;
      }

      // Check for vertical swipe
      const endY = e.changedTouches[0]?.clientY;
      if (touchStartY != null && endY != null) {
        const dy = endY - touchStartY;
        if (Math.abs(dy) > 40) {
          // Swipe detected — navigate
          touchStartY = null;
          if (dy < 0) {
            this._rotateNext();
          } else {
            this._rotatePrev();
          }
          setTimeout(() => {
            touchActive = false;
          }, 400);
          return;
        }
      }
      touchStartY = null;

      // Tap — find action target
      const el = this._findTarget(e);
      if (!el) {
        touchActive = false;
        return;
      }

      const tag = el.tagName?.toLowerCase();
      const nativeEl = tag === "input" || tag === "select" || tag === "textarea";
      if (!nativeEl && e.cancelable) e.preventDefault();

      this._dispatchAction(el);
      setTimeout(() => {
        touchActive = false;
      }, 400);
    });

    root.addEventListener("click", (e) => {
      if (touchActive) return;
      const el = this._findTarget(e);
      if (el) this._dispatchAction(el);
    });
  }

  _findTarget(e) {
    for (const node of e.composedPath()) {
      if (node instanceof Element && node.dataset?.action) return node;
    }
    return null;
  }

  _dispatchAction(el) {
    const action = el.dataset.action;
    if (!action) return;

    if (action === "open-popup") {
      window.history.pushState(null, "", this._config.navigation_path);
      window.dispatchEvent(new Event("location-changed"));
    }
  }

  // ---------------------------------------------------------------------------
  // Styles
  // ---------------------------------------------------------------------------

  _styles() {
    return `
      :host {
        display: block;
        touch-action: pan-y;
      }

      .cd-card {
        position: relative;
        width: 100%;
        aspect-ratio: 16 / 9;
        overflow: hidden;
        border-radius: 12px;
        cursor: pointer;
        background: var(--card-background-color, #1c1c1c);
      }

      .cd-layer {
        position: absolute;
        inset: 0;
        transition: opacity 0.6s ease;
      }

      .cd-bg {
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        object-fit: cover;
        display: block;
      }

      /* Dark gradient fallback when no image is available */
      .cd-layer::before {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(
          160deg,
          #0d1b2a 0%,
          #1a2744 50%,
          #0f1e35 100%
        );
        z-index: 0;
      }

      /* Dim gradient over image for text legibility */
      .cd-layer::after {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(
          to bottom,
          rgba(0, 0, 0, 0.35) 0%,
          rgba(0, 0, 0, 0.10) 40%,
          rgba(0, 0, 0, 0.10) 60%,
          rgba(0, 0, 0, 0.45) 100%
        );
        z-index: 1;
        pointer-events: none;
      }

      .cd-bg {
        z-index: 0;
      }

      .cd-overlay {
        position: absolute;
        inset: 0;
        z-index: 2;
        pointer-events: none;
      }

      /* Title + subtitle pinned to top */
      .cd-header-text {
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        padding: 14px 18px 10px;
        display: flex;
        flex-direction: column;
        align-items: center;
      }

      .cd-title {
        font-size: 24px;
        font-weight: 800;
        color: #fff;
        text-transform: uppercase;
        letter-spacing: 2px;
        text-shadow: 2px 2px 8px rgba(0, 0, 0, 0.8);
        text-align: center;
        line-height: 1.2;
      }

      .cd-subtitle {
        font-size: 16px;
        font-weight: 600;
        color: rgba(255, 255, 255, 0.85);
        text-shadow: 1px 1px 4px rgba(0, 0, 0, 0.8);
        margin-top: 4px;
        text-align: center;
        line-height: 1.2;
      }

      /* Countdown text positioned via inline top on the anchor div */
      .cd-countdown-anchor {
        position: absolute;
        left: 0;
        right: 0;
        transform: translateY(-50%);
        display: flex;
        justify-content: center;
        align-items: center;
      }

      .cd-countdown-text {
        font-weight: 1000;
        text-transform: uppercase;
        letter-spacing: 4px;
        text-align: center;
        line-height: 1;
      }

      .cd-countdown-text.cd-now {
        animation: cd-pulse 1.5s ease-in-out infinite;
        font-size: 64px !important;
      }

      @keyframes cd-pulse {
        0%, 100% { opacity: 1; transform: scale(1); }
        50%       { opacity: 0.7; transform: scale(1.05); }
      }

      /* Navigation dots */
      .cd-dots {
        position: absolute;
        bottom: 8px;
        left: 50%;
        transform: translateX(-50%);
        display: flex;
        gap: 6px;
        z-index: 3;
        pointer-events: none;
      }

      .cd-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: rgba(255, 255, 255, 0.4);
        transition: background 0.3s;
      }

      .cd-dot.active {
        background: rgba(255, 255, 255, 0.9);
      }
    `;
  }
}

customElements.define("countdown-card", CountdownCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "countdown-card",
  name: "Countdown Card",
  description: "Auto-rotating countdown timers with AI-generated backgrounds",
});
