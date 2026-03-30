/**
 * Media Dashboard Detail Card
 *
 * Popup detail card for the media dashboard. Shows all three media categories
 * (In Theaters, New on Plex, Coming Soon) as horizontally-scrollable poster rows.
 * Tapping a poster sends a get_detail relay command and expands an inline detail
 * panel with synopsis, ratings, runtime, genres, and showtimes.
 *
 * Reads:
 *   sensor.media_dashboard_status  — category/poster lists + last_updated
 *   sensor.media_dashboard_detail  — full detail for the selected item
 *
 * Sends commands via relay script:
 *   get_detail  — fetch full detail for a selected item
 *   like        — thumbs-up an item
 *   dismiss     — thumbs-down / hide an item
 *   refresh     — trigger a data refresh from all sources
 *
 * Mandatory patterns:
 *   - Shadow DOM
 *   - Touch/click deduplication (400ms flag)
 *   - NEVER preventDefault() on input/select/textarea touchend (Android)
 *   - Focus guard: skip re-render when shadowRoot.activeElement is set
 *   - Relay script for card → AppDaemon communication (never fire_event)
 *
 * Platforms: Desktop, iOS Companion App, Android/UniFi wall display.
 */

const MDD_DEFAULTS = {
  status_entity: "sensor.media_dashboard_status",
  detail_entity: "sensor.media_dashboard_detail",
  relay_script: "media_dashboard_relay",
};

// ---------------------------------------------------------------------------
// HTML escaping utility
// ---------------------------------------------------------------------------

function mddEscapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Card class
// ---------------------------------------------------------------------------

class MediaDashboardDetailCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...MDD_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._touchActive = false;
    this._selectedItemId = null;
    this._refreshTimer = null;
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...MDD_DEFAULTS, ...config };
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
      (active.tagName === "INPUT" ||
        active.tagName === "TEXTAREA" ||
        active.tagName === "SELECT")
    ) {
      return;
    }

    if (!this._domBuilt) {
      this._buildDom();
      this._update();
      this._startRefreshTimer();
      return;
    }

    this._update();
  }

  getCardSize() {
    return 6;
  }

  static getStubConfig() {
    return { ...MDD_DEFAULTS };
  }

  connectedCallback() {
    // Restart refresh timer when popup is reopened
    if (this._domBuilt) {
      this._startRefreshTimer();
      this._update();
    }
  }

  disconnectedCallback() {
    // Clear selection when popup closes so next open starts fresh
    this._selectedItemId = null;
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  // ---------------------------------------------------------------------------
  // Snapshot — re-render only when relevant state changes
  // ---------------------------------------------------------------------------

  _snapshot() {
    if (!this._hass) return null;
    const status = this._hass.states[this._config.status_entity];
    const detail = this._hass.states[this._config.detail_entity];
    return JSON.stringify({
      status: status
        ? {
            s: status.state,
            attrs: status.attributes,
          }
        : null,
      detail: detail
        ? {
            s: detail.state,
            attrs: detail.attributes,
          }
        : null,
      selectedId: this._selectedItemId,
    });
  }

  // ---------------------------------------------------------------------------
  // Refresh timer — re-render periodically so staleness timestamps stay fresh
  // ---------------------------------------------------------------------------

  _startRefreshTimer() {
    if (this._refreshTimer) return;
    this._refreshTimer = setInterval(() => {
      this._update();
    }, 30000);
  }

  // ---------------------------------------------------------------------------
  // Relay script
  // ---------------------------------------------------------------------------

  _callRelay(command, payload = {}) {
    if (!this._hass) return;
    this._hass
      .callService("script", this._config.relay_script, {
        command,
        payload: JSON.stringify(payload),
      })
      .catch((err) =>
        console.warn("media-dashboard relay failed:", command, err)
      );
  }

  // ---------------------------------------------------------------------------
  // Build DOM once
  // ---------------------------------------------------------------------------

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="mdd-root">
        <div class="mdd-header">
          <span class="mdd-title">MEDIA &amp; MOVIES</span>
          <span class="mdd-close" data-action="close">&#10005;</span>
        </div>
        <div class="mdd-body"></div>
        <div class="mdd-footer">
          <span class="mdd-updated"></span>
          <span class="mdd-refresh" data-action="refresh">&#8635; Refresh</span>
        </div>
      </div>
    `;

    this._els = {
      body: this.shadowRoot.querySelector(".mdd-body"),
      updated: this.shadowRoot.querySelector(".mdd-updated"),
    };

    this._bindEvents();
    this._domBuilt = true;
  }

  // ---------------------------------------------------------------------------
  // Update — full re-render of body content
  // ---------------------------------------------------------------------------

  _update() {
    if (!this._els) return;

    const statusEntity = this._hass?.states?.[this._config.status_entity];
    const detailEntity = this._hass?.states?.[this._config.detail_entity];

    // Update footer timestamp
    const lastUpdated = statusEntity?.attributes?.last_updated;
    this._els.updated.textContent = lastUpdated
      ? `Updated ${this._formatRelative(lastUpdated)}`
      : "";

    // Category definitions in render order
    const CATEGORIES = [
      { key: "in_theaters", label: "IN THEATERS" },
      { key: "plex_new", label: "NEW ON PLEX" },
      { key: "coming_soon", label: "COMING SOON" },
    ];

    let bodyHtml = "";

    // Render detail panel FIRST (at top) if an item is selected
    if (this._selectedItemId !== null) {
      const detailAttrs = detailEntity?.attributes || {};
      const detailId = detailEntity?.state;

      if (detailId === String(this._selectedItemId)) {
        bodyHtml += this._renderDetailPanel(detailAttrs);
      } else {
        // Detail is loading — show spinner
        bodyHtml += this._renderDetailLoading();
      }
    }

    // Render each category section below the detail
    for (const cat of CATEGORIES) {
      const items =
        statusEntity?.attributes?.categories?.[cat.key] || [];
      bodyHtml += this._renderSection(cat.key, cat.label, items);
    }

    this._els.body.innerHTML = bodyHtml;
  }

  // ---------------------------------------------------------------------------
  // Section renderer — horizontal poster scroll
  // ---------------------------------------------------------------------------

  _renderSection(key, label, items) {
    if (!items || items.length === 0) {
      return `
        <div class="mdd-section" data-category="${mddEscapeHtml(key)}">
          <div class="mdd-section-header">${mddEscapeHtml(label)}</div>
          <div class="mdd-empty-section">No items available</div>
        </div>
      `;
    }

    let postersHtml = "";
    for (const item of items) {
      postersHtml += this._renderPoster(item);
    }

    return `
      <div class="mdd-section" data-category="${mddEscapeHtml(key)}">
        <div class="mdd-section-header">${mddEscapeHtml(label)}</div>
        <div class="mdd-poster-scroll">
          ${postersHtml}
        </div>
      </div>
    `;
  }

  _renderPoster(item) {
    const id = mddEscapeHtml(String(item.id ?? ""));
    const title = mddEscapeHtml(item.title || "");
    const subtitle = mddEscapeHtml(item.subtitle || item.year || "");
    const posterSrc = mddEscapeHtml(item.poster || "");
    const isSelected = String(item.id) === String(this._selectedItemId);
    const selectedClass = isSelected ? " mdd-poster--selected" : "";

    const imgHtml = posterSrc
      ? `<img class="mdd-poster-img" src="${posterSrc}" alt="${title}" loading="lazy" />`
      : `<div class="mdd-poster-placeholder">
           <span class="mdd-poster-placeholder-text">${title}</span>
         </div>`;

    return `
      <div class="mdd-poster${selectedClass}" data-action="select" data-id="${id}">
        ${imgHtml}
        <div class="mdd-poster-title">${title}</div>
        <div class="mdd-poster-sub">${subtitle}</div>
        <div class="mdd-poster-btns">
          <span class="mdd-btn mdd-btn-like" data-action="like" data-id="${id}" title="Like">&#128077;</span>
          <span class="mdd-btn mdd-btn-dismiss" data-action="dismiss" data-id="${id}" title="Dismiss">&#128078;</span>
        </div>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Detail panel renderer
  // ---------------------------------------------------------------------------

  _renderDetailPanel(attrs) {
    const id = mddEscapeHtml(String(attrs.id ?? ""));
    const title = mddEscapeHtml(attrs.title || "");
    const posterSrc = mddEscapeHtml(
      attrs.poster || ""
    );
    const score = attrs.tmdb_score != null ? attrs.tmdb_score.toFixed(1) : "—";
    const rating = mddEscapeHtml(attrs.rating || "");
    const runtime = attrs.runtime_min
      ? `${Math.floor(attrs.runtime_min / 60)}h ${attrs.runtime_min % 60}m`
      : "";
    const genres = Array.isArray(attrs.genres)
      ? mddEscapeHtml(attrs.genres.join(", "))
      : mddEscapeHtml(attrs.genres || "");
    const summary = mddEscapeHtml(attrs.summary || "");

    const metaParts = [score ? `&#9733; ${score}` : null, rating, runtime, genres]
      .filter(Boolean)
      .join(" &middot; ");

    const imgHtml = posterSrc
      ? `<img class="mdd-detail-poster" src="${posterSrc}" alt="${title}" />`
      : `<div class="mdd-detail-poster mdd-detail-poster--placeholder">
           <span>${title}</span>
         </div>`;

    const showtimesHtml = this._renderShowtimes(attrs.showtimes, attrs.showtimes_date);

    return `
      <div class="mdd-detail">
        <div class="mdd-detail-back" data-action="back">&#9664; Back</div>
        <div class="mdd-detail-content">
          ${imgHtml}
          <div class="mdd-detail-meta">
            <div class="mdd-detail-title">${title}</div>
            <div class="mdd-detail-info">${metaParts}</div>
            <div class="mdd-detail-btns">
              <span class="mdd-btn mdd-btn-like" data-action="like" data-id="${id}" title="Like">&#128077; Like</span>
              <span class="mdd-btn mdd-btn-dismiss" data-action="dismiss" data-id="${id}" title="Dismiss">&#128078; Dismiss</span>
            </div>
          </div>
        </div>
        ${summary ? `<div class="mdd-detail-summary">${summary}</div>` : ""}
        ${showtimesHtml}
      </div>
    `;
  }

  _renderShowtimes(showtimes, showtimesDate) {
    if (!showtimes) return "";

    // The app publishes showtimes as {entries: [...], stale, note}
    const entries = showtimes.entries || showtimes.theaters || [];
    if (entries.length === 0) {
      const note = showtimes.note;
      return note
        ? `<div class="mdd-showtimes"><div class="mdd-showtimes-header">${mddEscapeHtml(note)}</div></div>`
        : "";
    }

    // Group entries by day label (e.g. "Today", "Tomorrow", "Monday")
    const dayOrder = [];
    const dayMap = {};
    for (const entry of entries) {
      const dayKey = entry.day || entry.date || "Today";
      const dateLabel = entry.date || "";
      if (!dayMap[dayKey]) {
        dayMap[dayKey] = { label: dayKey, date: dateLabel, theaters: [] };
        dayOrder.push(dayKey);
      }
      dayMap[dayKey].theaters.push(entry);
    }

    let html = '<div class="mdd-showtimes">';
    html += `<div class="mdd-showtimes-header">SHOWTIMES</div>`;

    for (const dayKey of dayOrder) {
      const day = dayMap[dayKey];
      const dayLabel = day.date
        ? `${mddEscapeHtml(day.label)} &mdash; ${mddEscapeHtml(day.date)}`
        : mddEscapeHtml(day.label);

      html += `<div class="mdd-showtime-day">`;
      html += `<div class="mdd-day-label">${dayLabel}</div>`;

      for (const theater of day.theaters) {
        const cinemaName = mddEscapeHtml(theater.cinema_name || "");
        const times = theater.times || [];
        if (times.length === 0) continue;

        let timesHtml = "";
        for (const t of times) {
          timesHtml += `<span class="mdd-time">${mddEscapeHtml(t)}</span>`;
        }

        html += `
          <div class="mdd-theater">
            <div class="mdd-theater-name">${cinemaName}</div>
            <div class="mdd-theater-times">${timesHtml}</div>
          </div>
        `;
      }

      html += `</div>`;
    }

    html += "</div>";
    return html;
  }

  _renderDetailLoading() {
    return `
      <div class="mdd-detail mdd-detail--loading">
        <div class="mdd-detail-back" data-action="back">&#9664; Back</div>
        <div class="mdd-loading">
          <div class="mdd-loading-spinner"></div>
          <span>Loading details...</span>
        </div>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Formatting helpers
  // ---------------------------------------------------------------------------

  _formatRelative(isoStr) {
    if (!isoStr) return "";
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    const seconds = Math.floor((Date.now() - d.getTime()) / 1000);
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
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
      const id = el.dataset.id;

      if (action === "select") {
        this._selectedItemId = id;
        this._callRelay("get_detail", { id });
        // Invalidate snapshot so next hass set triggers a re-render
        this._lastSnapshot = null;
        this._update();
      } else if (action === "back") {
        this._selectedItemId = null;
        this._lastSnapshot = null;
        this._update();
      } else if (action === "like") {
        this._callRelay("like", { id });
      } else if (action === "dismiss") {
        this._callRelay("dismiss", { id });
      } else if (action === "refresh") {
        this._callRelay("refresh", { source: "all" });
      } else if (action === "close") {
        history.back();
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
  // Styles — glass morphism consistent with compact card
  // ---------------------------------------------------------------------------

  _styles() {
    return `
      :host {
        --mdd-radius: 14px;
        --mdd-glass-bg: rgba(28, 29, 33, 0.88);
        --mdd-glass-border: rgba(255, 255, 255, 0.14);
        --mdd-text: rgba(240, 243, 255, 0.92);
        --mdd-muted: rgba(200, 205, 220, 0.6);
        --mdd-accent: var(--primary-color, #66b3ff);
        --mdd-section-label: rgba(180, 190, 210, 0.7);
        --mdd-poster-bg: rgba(40, 42, 50, 0.9);
        --mdd-time-bg: rgba(60, 70, 90, 0.7);
        --mdd-like-color: #66cc77;
        --mdd-dismiss-color: #e06060;
        display: block;
      }

      .mdd-root {
        display: flex;
        flex-direction: column;
        background: var(--mdd-glass-bg);
        border: 1px solid var(--mdd-glass-border);
        border-radius: var(--mdd-radius);
        box-shadow:
          0 16px 48px rgba(0, 0, 0, 0.4),
          inset 0 1px 0 rgba(255, 255, 255, 0.12),
          inset 0 0 24px rgba(255, 255, 255, 0.03);
        backdrop-filter: blur(18px) saturate(1.3);
        -webkit-backdrop-filter: blur(18px) saturate(1.3);
        overflow: hidden;
        max-height: 90vh;
      }

      /* ---- Header ---- */

      .mdd-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 14px 18px 10px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        flex-shrink: 0;
      }

      .mdd-title {
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.12em;
        color: var(--mdd-text);
        text-transform: uppercase;
      }

      .mdd-close {
        font-size: 16px;
        color: var(--mdd-muted);
        cursor: pointer;
        padding: 4px 6px;
        border-radius: 6px;
        line-height: 1;
        transition: color 150ms ease, background 150ms ease;
        user-select: none;
        -webkit-user-select: none;
      }

      .mdd-close:active {
        color: var(--mdd-text);
        background: rgba(255, 255, 255, 0.08);
      }

      /* ---- Body ---- */

      .mdd-body {
        flex: 1;
        overflow-y: auto;
        overflow-x: hidden;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: thin;
        scrollbar-color: rgba(255, 255, 255, 0.15) transparent;
        padding: 8px 0 4px;
      }

      .mdd-body::-webkit-scrollbar {
        width: 4px;
      }

      .mdd-body::-webkit-scrollbar-track {
        background: transparent;
      }

      .mdd-body::-webkit-scrollbar-thumb {
        background: rgba(255, 255, 255, 0.18);
        border-radius: 2px;
      }

      /* ---- Sections ---- */

      .mdd-section {
        margin-bottom: 16px;
      }

      .mdd-section-header {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.1em;
        color: var(--mdd-section-label);
        padding: 0 16px 8px;
        text-transform: uppercase;
      }

      .mdd-empty-section {
        padding: 8px 16px;
        font-size: 13px;
        color: var(--mdd-muted);
        font-style: italic;
      }

      /* ---- Poster scroll row ---- */

      .mdd-poster-scroll {
        display: flex;
        flex-direction: row;
        gap: 10px;
        overflow-x: auto;
        overflow-y: hidden;
        -webkit-overflow-scrolling: touch;
        padding: 0 16px 8px;
        scroll-snap-type: x mandatory;
        scrollbar-width: thin;
        scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
      }

      .mdd-poster-scroll::-webkit-scrollbar {
        height: 3px;
      }

      .mdd-poster-scroll::-webkit-scrollbar-track {
        background: transparent;
      }

      .mdd-poster-scroll::-webkit-scrollbar-thumb {
        background: rgba(255, 255, 255, 0.15);
        border-radius: 2px;
      }

      /* ---- Individual poster ---- */

      .mdd-poster {
        flex: 0 0 auto;
        width: 110px;
        display: flex;
        flex-direction: column;
        cursor: pointer;
        scroll-snap-align: start;
        border-radius: 8px;
        overflow: hidden;
        background: var(--mdd-poster-bg);
        border: 1px solid rgba(255, 255, 255, 0.08);
        transition: transform 150ms ease, border-color 150ms ease;
        user-select: none;
        -webkit-user-select: none;
      }

      .mdd-poster:active {
        transform: scale(0.96);
      }

      .mdd-poster--selected {
        border-color: var(--mdd-accent);
        box-shadow: 0 0 0 1px var(--mdd-accent);
      }

      .mdd-poster-img {
        width: 100%;
        aspect-ratio: 2 / 3;
        object-fit: cover;
        display: block;
        background: rgba(40, 42, 50, 1);
      }

      .mdd-poster-placeholder {
        width: 100%;
        aspect-ratio: 2 / 3;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(50, 55, 70, 0.8);
        padding: 8px;
        box-sizing: border-box;
      }

      .mdd-poster-placeholder-text {
        font-size: 11px;
        color: var(--mdd-muted);
        text-align: center;
        line-height: 1.3;
        word-break: break-word;
      }

      .mdd-poster-title {
        font-size: 11px;
        font-weight: 600;
        color: var(--mdd-text);
        padding: 4px 6px 2px;
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        line-height: 1.3;
      }

      .mdd-poster-sub {
        font-size: 10px;
        color: var(--mdd-muted);
        padding: 0 6px 4px;
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
      }

      .mdd-poster-btns {
        display: flex;
        justify-content: space-around;
        padding: 2px 4px 6px;
        border-top: 1px solid rgba(255, 255, 255, 0.06);
      }

      /* ---- Action buttons (like/dismiss) ---- */

      .mdd-btn {
        cursor: pointer;
        padding: 3px 6px;
        border-radius: 4px;
        font-size: 13px;
        line-height: 1;
        transition: background 120ms ease, transform 100ms ease;
        user-select: none;
        -webkit-user-select: none;
      }

      .mdd-btn:active {
        transform: scale(0.9);
      }

      .mdd-btn-like:active {
        background: rgba(102, 204, 119, 0.2);
      }

      .mdd-btn-dismiss:active {
        background: rgba(224, 96, 96, 0.2);
      }

      /* ---- Detail panel ---- */

      .mdd-detail {
        margin: 0 12px 16px;
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 10px;
        background: rgba(30, 32, 40, 0.6);
        overflow: hidden;
      }

      .mdd-detail--loading {
        padding-bottom: 24px;
      }

      .mdd-detail-back {
        font-size: 12px;
        font-weight: 600;
        color: var(--mdd-accent);
        padding: 10px 14px 8px;
        cursor: pointer;
        display: inline-block;
        transition: opacity 150ms ease;
        user-select: none;
        -webkit-user-select: none;
      }

      .mdd-detail-back:active {
        opacity: 0.7;
      }

      .mdd-detail-content {
        display: flex;
        flex-direction: row;
        gap: 12px;
        padding: 0 14px 12px;
        align-items: flex-start;
      }

      .mdd-detail-poster {
        flex: 0 0 auto;
        width: 90px;
        aspect-ratio: 2 / 3;
        object-fit: cover;
        border-radius: 6px;
        background: rgba(40, 42, 50, 1);
        display: block;
      }

      .mdd-detail-poster--placeholder {
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(50, 55, 70, 0.8);
        font-size: 11px;
        color: var(--mdd-muted);
        text-align: center;
        padding: 8px;
        box-sizing: border-box;
      }

      .mdd-detail-meta {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding-top: 2px;
      }

      .mdd-detail-title {
        font-size: 15px;
        font-weight: 700;
        color: var(--mdd-text);
        line-height: 1.25;
        word-break: break-word;
      }

      .mdd-detail-info {
        font-size: 12px;
        color: var(--mdd-muted);
        line-height: 1.4;
      }

      .mdd-detail-btns {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 4px;
      }

      .mdd-detail-btns .mdd-btn {
        font-size: 12px;
        padding: 4px 10px;
        border: 1px solid rgba(255, 255, 255, 0.1);
        background: rgba(255, 255, 255, 0.04);
        border-radius: 12px;
        display: flex;
        align-items: center;
        gap: 4px;
      }

      .mdd-detail-summary {
        font-size: 13px;
        color: rgba(210, 215, 230, 0.85);
        line-height: 1.55;
        padding: 0 14px 12px;
        border-top: 1px solid rgba(255, 255, 255, 0.06);
        padding-top: 10px;
      }

      /* ---- Showtimes ---- */

      .mdd-showtimes {
        border-top: 1px solid rgba(255, 255, 255, 0.06);
        padding: 10px 14px 14px;
      }

      .mdd-showtimes-header {
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.1em;
        color: var(--mdd-section-label);
        text-transform: uppercase;
        margin-bottom: 10px;
      }

      .mdd-showtime-day {
        margin-bottom: 14px;
      }

      .mdd-day-label {
        font-size: 11px;
        font-weight: 600;
        color: rgba(240, 243, 255, 0.85);
        margin-bottom: 6px;
        padding-bottom: 4px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      }

      .mdd-theater {
        margin-bottom: 10px;
      }

      .mdd-theater:last-child {
        margin-bottom: 0;
      }

      .mdd-theater-name {
        font-size: 12px;
        font-weight: 700;
        color: var(--mdd-text);
        margin-bottom: 5px;
      }

      .mdd-theater-times {
        display: flex;
        flex-wrap: wrap;
        gap: 5px;
      }

      .mdd-time {
        font-size: 11px;
        font-weight: 600;
        color: rgba(180, 200, 240, 0.9);
        background: var(--mdd-time-bg);
        border: 1px solid rgba(100, 130, 200, 0.25);
        border-radius: 6px;
        padding: 3px 8px;
        white-space: nowrap;
        letter-spacing: 0.03em;
      }

      /* ---- Loading indicator ---- */

      .mdd-loading {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 12px;
        padding: 24px 16px;
        color: var(--mdd-muted);
        font-size: 13px;
      }

      .mdd-loading-spinner {
        width: 24px;
        height: 24px;
        border: 2px solid rgba(255, 255, 255, 0.1);
        border-top-color: var(--mdd-accent);
        border-radius: 50%;
        animation: mdd-spin 0.8s linear infinite;
      }

      @keyframes mdd-spin {
        to { transform: rotate(360deg); }
      }

      /* ---- Footer ---- */

      .mdd-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 18px 12px;
        border-top: 1px solid rgba(255, 255, 255, 0.07);
        flex-shrink: 0;
      }

      .mdd-updated {
        font-size: 11px;
        color: var(--mdd-muted);
        letter-spacing: 0.02em;
      }

      .mdd-refresh {
        font-size: 12px;
        font-weight: 600;
        color: var(--mdd-accent);
        cursor: pointer;
        padding: 4px 8px;
        border-radius: 6px;
        transition: background 150ms ease;
        user-select: none;
        -webkit-user-select: none;
        letter-spacing: 0.03em;
      }

      .mdd-refresh:active {
        background: rgba(102, 179, 255, 0.1);
      }
    `;
  }
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

customElements.define("media-dashboard-detail-card", MediaDashboardDetailCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "media-dashboard-detail-card",
  name: "Media Dashboard Detail Card",
  description: "Popup detail card with showtimes and media info",
});
