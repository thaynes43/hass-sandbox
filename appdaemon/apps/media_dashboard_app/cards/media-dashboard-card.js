/**
 * Media Dashboard Card
 *
 * Compact media poster card for the wall-display dashboard.
 * Shows 3 poster slots in a row with category tabs at the bottom.
 * Auto-rotates through categories; supports swipe/arrow navigation within
 * a category. Tap a poster to open the detail popup.
 *
 * Reads:  sensor.media_dashboard_status — overall state + categories attribute
 * Writes: script.media_dashboard_relay  — dismiss command
 *
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App,
 *            Android/UniFi wall display.
 */

const MD_DEFAULTS = {
  status_entity: "sensor.media_dashboard_status",
  navigation_path: "#media-dashboard-popup",
  relay_script: "media_dashboard_relay",
  auto_rotate_s: 15,
};

class MediaDashboardCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...MD_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._touchActive = false;
    this._activeCategory = "in_theaters";
    this._scrollOffset = 0;
    this._rotateTimer = null;
    this._interactionTimeout = null;
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...MD_DEFAULTS, ...config };
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
    return 3;
  }

  static getStubConfig() {
    return { ...MD_DEFAULTS };
  }

  connectedCallback() {
    super.connectedCallback?.();
    this._startRotate();
  }

  disconnectedCallback() {
    super.disconnectedCallback?.();
    this._stopRotate();
    if (this._interactionTimeout) {
      clearTimeout(this._interactionTimeout);
      this._interactionTimeout = null;
    }
  }

  // ---------------------------------------------------------------------------
  // Snapshot — re-render only when relevant state changes
  // ---------------------------------------------------------------------------

  _snapshot() {
    if (!this._hass) return null;
    const status = this._hass.states[this._config.status_entity];
    if (!status) return null;

    // Cheap snapshot: top-level category keys + item count per category
    const categories = status.attributes?.categories || {};
    const categorySummary = {};
    for (const [key, items] of Object.entries(categories)) {
      categorySummary[key] = Array.isArray(items) ? items.length : 0;
    }

    return JSON.stringify({
      state: status.state,
      categorySummary,
    });
  }

  // ---------------------------------------------------------------------------
  // Auto-rotate categories
  // ---------------------------------------------------------------------------

  _startRotate() {
    if (this._rotateTimer) return;
    const intervalMs = (Number(this._config.auto_rotate_s) || 15) * 1000;
    this._rotateTimer = setInterval(() => {
      this._rotateNextCategory();
    }, intervalMs);
  }

  _stopRotate() {
    if (this._rotateTimer) {
      clearInterval(this._rotateTimer);
      this._rotateTimer = null;
    }
  }

  _pauseForInteraction() {
    this._stopRotate();
    if (this._interactionTimeout) {
      clearTimeout(this._interactionTimeout);
    }
    // Resume auto-rotate after 30s of no interaction
    this._interactionTimeout = setTimeout(() => {
      this._interactionTimeout = null;
      this._startRotate();
    }, 30000);
  }

  _rotateNextCategory() {
    const tabs = ["in_theaters", "plex_movies", "plex_shows", "coming_soon"];
    const current = tabs.indexOf(this._activeCategory);
    this._activeCategory = tabs[(current + 1) % tabs.length];
    this._scrollOffset = 0;
    this._update();
  }

  // ---------------------------------------------------------------------------
  // Data helpers
  // ---------------------------------------------------------------------------

  _getCategoryItems() {
    if (!this._hass) return [];
    const status = this._hass.states[this._config.status_entity];
    const categories = status?.attributes?.categories || {};
    const items = categories[this._activeCategory];
    return Array.isArray(items) ? items : [];
  }

  _escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str || "";
    return div.innerHTML;
  }

  // ---------------------------------------------------------------------------
  // Navigation
  // ---------------------------------------------------------------------------

  _navigate(path) {
    const targetPath = String(path || "").trim();
    if (!targetPath) return;
    window.history.pushState(null, "", targetPath);
    window.dispatchEvent(new Event("location-changed"));
  }

  // ---------------------------------------------------------------------------
  // Build DOM once, then do targeted updates
  // ---------------------------------------------------------------------------

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="md-card" data-action="open-popup">
        <div class="md-header">
          <span class="md-title">MEDIA &amp; MOVIES</span>
          <div class="md-nav">
            <span class="md-arrow" data-action="prev"><ha-icon icon="mdi:chevron-left" style="--mdc-icon-size:20px;"></ha-icon></span>
            <span class="md-arrow" data-action="next"><ha-icon icon="mdi:chevron-right" style="--mdc-icon-size:20px;"></ha-icon></span>
          </div>
        </div>
        <div class="md-poster-row"></div>
        <div class="md-tabs">
          <div class="md-tab" data-action="tab" data-category="in_theaters"><ha-icon icon="mdi:filmstrip" style="--mdc-icon-size:14px;"></ha-icon> THEATERS</div>
          <div class="md-tab" data-action="tab" data-category="plex_movies"><img class="md-tab-icon" src="/local/media-dashboard/icons/plex.png" alt="Plex" /> MOVIES</div>
          <div class="md-tab" data-action="tab" data-category="plex_shows"><img class="md-tab-icon" src="/local/media-dashboard/icons/plex.png" alt="Plex" /> SHOWS</div>
          <div class="md-tab" data-action="tab" data-category="coming_soon"><ha-icon icon="mdi:calendar-clock" style="--mdc-icon-size:14px;"></ha-icon> SOON</div>
        </div>
      </div>
    `;

    this._els = {
      card: this.shadowRoot.querySelector(".md-card"),
      posterRow: this.shadowRoot.querySelector(".md-poster-row"),
      tabs: this.shadowRoot.querySelectorAll(".md-tab"),
      prevArrow: this.shadowRoot.querySelector('[data-action="prev"]'),
      nextArrow: this.shadowRoot.querySelector('[data-action="next"]'),
    };

    this._domBuilt = true;
  }

  _update() {
    if (!this._els) return;

    const items = this._getCategoryItems();
    const totalItems = items.length;
    const VISIBLE = 3;

    // Clamp scroll offset
    const maxOffset = Math.max(0, totalItems - VISIBLE);
    if (this._scrollOffset > maxOffset) this._scrollOffset = maxOffset;
    if (this._scrollOffset < 0) this._scrollOffset = 0;

    const visibleItems = items.slice(this._scrollOffset, this._scrollOffset + VISIBLE);

    // Render posters
    let html = "";
    if (visibleItems.length === 0) {
      html = `<div class="md-empty">No items available</div>`;
    } else {
      for (const item of visibleItems) {
        const id = this._escapeHtml(String(item.id || ""));
        const title = this._escapeHtml(String(item.title || ""));
        const subtitle = this._escapeHtml(String(item.subtitle || ""));
        const poster = this._escapeHtml(String(item.poster || ""));
        const isLiked = !!item.liked;

        let imgHtml;
        if (poster) {
          imgHtml = `<img src="${poster}" alt="${title}" loading="lazy" class="md-poster-img" />`;
        } else {
          imgHtml = `<div class="md-poster-placeholder">
            <span class="md-poster-placeholder-text">${title.charAt(0) || "?"}</span>
          </div>`;
        }

        const likedBadge = isLiked
          ? `<span class="md-liked-badge"><ha-icon icon="mdi:heart" style="--mdc-icon-size:16px;"></ha-icon></span>`
          : "";

        html += `
          <div class="md-poster" data-action="open-popup" data-id="${id}">
            ${imgHtml}
            ${likedBadge}
            <div class="md-poster-info">
              <div class="md-poster-title">${title}</div>
              <div class="md-poster-sub">${subtitle}</div>
            </div>
            <div class="md-dismiss" data-action="dismiss" data-id="${id}" title="Dismiss">&#10005;</div>
          </div>
        `;
      }

      // Pad with empty slots if fewer than VISIBLE items
      for (let i = visibleItems.length; i < VISIBLE; i++) {
        html += `<div class="md-poster md-poster-empty"></div>`;
      }
    }
    this._els.posterRow.innerHTML = html;

    // Update active tab
    this._els.tabs.forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.category === this._activeCategory);
    });

    // Update nav arrows
    if (this._els.prevArrow) {
      this._els.prevArrow.classList.toggle("md-arrow-hidden", this._scrollOffset === 0);
    }
    if (this._els.nextArrow) {
      const atEnd = this._scrollOffset >= maxOffset;
      this._els.nextArrow.classList.toggle("md-arrow-hidden", atEnd);
    }
  }

  // ---------------------------------------------------------------------------
  // Touch / click deduplication
  // ---------------------------------------------------------------------------

  _bindEvents() {
    const root = this.shadowRoot;

    const findActionEl = (evt) => {
      for (const node of evt.composedPath()) {
        if (node instanceof Element && node.dataset?.action) return node;
      }
      return null;
    };

    const dispatchAction = (el) => {
      const action = el.dataset.action;

      if (action === "open-popup") {
        this._navigate(this._config.navigation_path);
        return;
      }

      if (action === "prev") {
        this._scrollOffset = Math.max(0, this._scrollOffset - 3);
        this._pauseForInteraction();
        this._update();
        return;
      }

      if (action === "next") {
        const items = this._getCategoryItems();
        const maxOffset = Math.max(0, items.length - 3);
        this._scrollOffset = Math.min(maxOffset, this._scrollOffset + 3);
        this._pauseForInteraction();
        this._update();
        return;
      }

      if (action === "tab") {
        const category = el.dataset.category;
        if (category && category !== this._activeCategory) {
          this._activeCategory = category;
          this._scrollOffset = 0;
          this._pauseForInteraction();
          this._update();
        }
        return;
      }

      if (action === "dismiss") {
        const id = el.dataset.id;
        if (id && this._hass) {
          this._hass.callService("script", this._config.relay_script, {
            command: "dismiss",
            payload: JSON.stringify({ id }),
          });
          this._pauseForInteraction();
        }
        return;
      }
    };

    let touchCancelled = false;

    ["touchcancel", "touchmove", "scroll"].forEach((evtName) => {
      root.addEventListener(
        evtName,
        () => {
          touchCancelled = true;
        },
        { passive: true }
      );
    });

    root.addEventListener(
      "touchstart",
      () => {
        touchCancelled = false;
      },
      { passive: true }
    );

    root.addEventListener(
      "touchend",
      (e) => {
        const el = findActionEl(e);
        if (touchCancelled || !el) {
          this._touchActive = false;
          return;
        }

        const tag = el.tagName?.toLowerCase();
        const nativeEl =
          tag === "input" || tag === "select" || tag === "textarea";
        if (!nativeEl && e.cancelable) e.preventDefault();

        this._touchActive = true;
        dispatchAction(el);
        setTimeout(() => {
          this._touchActive = false;
        }, 400);
      },
      { passive: false }
    );

    root.addEventListener("click", (e) => {
      if (this._touchActive) return;
      const el = findActionEl(e);
      if (el) dispatchAction(el);
    });
  }

  // ---------------------------------------------------------------------------
  // Styles — glass-morphism matching the dock-bar aesthetic
  // ---------------------------------------------------------------------------

  _styles() {
    return `
      :host {
        display: block;
      }

      .md-card {
        display: flex;
        flex-direction: column;
        gap: 10px;
        padding: 14px 16px;
        border-radius: 20px;
        background: rgba(28, 29, 33, 0.32);
        border: 1px solid rgba(255, 255, 255, 0.14);
        box-shadow:
          0 14px 40px rgba(0, 0, 0, 0.28),
          inset 0 1px 0 rgba(255, 255, 255, 0.18),
          inset 0 0 18px rgba(255, 255, 255, 0.04);
        backdrop-filter: blur(14px) saturate(1.2);
        -webkit-backdrop-filter: blur(14px) saturate(1.2);
      }

      /* ---- Header ---- */

      .md-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
      }

      .md-title {
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: rgba(240, 243, 255, 0.75);
      }

      .md-nav {
        display: flex;
        align-items: center;
        gap: 4px;
      }

      .md-arrow {
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 24px;
        height: 24px;
        border-radius: 50%;
        font-size: 11px;
        color: rgba(240, 243, 255, 0.6);
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid rgba(255, 255, 255, 0.1);
        transition: background 150ms ease, color 150ms ease, opacity 150ms ease;
        user-select: none;
        -webkit-user-select: none;
      }

      .md-arrow:active {
        background: rgba(255, 255, 255, 0.14);
        color: rgba(240, 243, 255, 0.9);
      }

      .md-arrow.md-arrow-hidden {
        opacity: 0;
        pointer-events: none;
      }

      /* ---- Poster row ---- */

      .md-poster-row {
        display: flex;
        gap: 8px;
        align-items: flex-start;
      }

      .md-poster {
        position: relative;
        flex: 1 1 0;
        min-width: 0;
        cursor: pointer;
        border-radius: 8px;
        overflow: hidden;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.08);
        transition: filter 120ms ease;
      }

      .md-poster:active {
        filter: brightness(0.88);
      }

      .md-poster-empty {
        aspect-ratio: 2 / 3;
        max-height: 150px;
        cursor: default;
        background: rgba(255, 255, 255, 0.03);
        border-color: rgba(255, 255, 255, 0.04);
      }

      .md-poster-img {
        display: block;
        width: 100%;
        aspect-ratio: 2 / 3;
        max-height: 150px;
        object-fit: cover;
        background: rgba(255, 255, 255, 0.05);
      }

      .md-poster-placeholder {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 100%;
        aspect-ratio: 2 / 3;
        max-height: 150px;
        background: rgba(255, 255, 255, 0.05);
      }

      .md-poster-placeholder-text {
        font-size: 28px;
        font-weight: 700;
        color: rgba(240, 243, 255, 0.3);
        text-transform: uppercase;
      }

      .md-poster-info {
        padding: 5px 6px 6px;
        background: rgba(10, 10, 14, 0.7);
      }

      .md-poster-title {
        font-size: 11px;
        font-weight: 600;
        color: rgba(240, 243, 255, 0.9);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        letter-spacing: 0.01em;
      }

      .md-poster-sub {
        margin-top: 1px;
        font-size: 10px;
        font-weight: 400;
        color: rgba(200, 205, 220, 0.6);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      /* ---- Liked badge ---- */

      .md-liked-badge {
        position: absolute;
        top: 4px;
        left: 4px;
        color: #e53935;
        filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.7));
        pointer-events: none;
        z-index: 1;
      }

      /* ---- Dismiss button ---- */

      .md-dismiss {
        position: absolute;
        top: 4px;
        right: 4px;
        display: flex;
        align-items: center;
        justify-content: center;
        width: 20px;
        height: 20px;
        border-radius: 50%;
        font-size: 10px;
        font-weight: 700;
        color: rgba(240, 243, 255, 0.9);
        background: rgba(0, 0, 0, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.15);
        opacity: 0;
        transition: opacity 150ms ease, background 150ms ease;
        cursor: pointer;
        /* Ensure dismiss does not propagate to poster tap */
        z-index: 2;
      }

      .md-poster:hover .md-dismiss,
      .md-poster:active .md-dismiss {
        opacity: 1;
      }

      /* Always visible on touch devices where hover is unavailable */
      @media (hover: none) {
        .md-dismiss {
          opacity: 0.7;
        }
      }

      .md-dismiss:active {
        background: rgba(200, 60, 60, 0.65);
      }

      /* ---- Empty state ---- */

      .md-empty {
        flex: 1;
        text-align: center;
        padding: 24px 8px;
        font-size: 12px;
        color: rgba(200, 205, 220, 0.45);
        font-style: italic;
      }

      /* ---- Category tabs ---- */

      .md-tabs {
        display: flex;
        gap: 4px;
        align-items: center;
        border-top: 1px solid rgba(255, 255, 255, 0.07);
        padding-top: 8px;
      }

      .md-tab {
        flex: 1 1 0;
        text-align: center;
        cursor: pointer;
        padding: 4px 2px;
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: rgba(200, 205, 220, 0.5);
        border-bottom: 2px solid transparent;
        transition: color 150ms ease, border-color 150ms ease;
        user-select: none;
        -webkit-user-select: none;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .md-tab:active {
        color: rgba(240, 243, 255, 0.8);
      }

      .md-tab.active {
        color: rgba(240, 243, 255, 0.95);
        border-bottom-color: var(--primary-color, #66b3ff);
      }

      .md-tab-icon {
        width: 14px;
        height: 14px;
        vertical-align: middle;
        margin-right: 2px;
        object-fit: contain;
      }
    `;
  }
}

customElements.define("media-dashboard-card", MediaDashboardCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "media-dashboard-card",
  name: "Media Dashboard Card",
  description: "Compact media poster card for wall displays",
});
