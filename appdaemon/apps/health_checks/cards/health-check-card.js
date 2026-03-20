/**
 * Health Check Card
 *
 * Compact summary card for the wall-display dashboard. Replaces the 3-row
 * bubble-card separator between the photo display and the dock bar.
 *
 * Shows at-a-glance health status for AppDaemon and registered protocol
 * checkers (Zigbee, Z-Wave, etc.).
 *
 * Reads:
 *   sensor.health_check_status           — overall state + per-checker attrs
 *   input_datetime.appdaemon_heartbeat   — last heartbeat timestamp
 *
 * Tapping the card navigates to config.navigation_path (default
 * "#health-check-popup") to open the detail card in a bubble-card popup.
 *
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App,
 *            Android/UniFi wall display.
 */

const HC_DEFAULTS = {
  status_entity: "sensor.health_check_status",
  heartbeat_entity: "input_datetime.appdaemon_heartbeat",
  navigation_path: "#health-check-popup",
  stale_threshold_s: 180,
};

class HealthCheckCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...HC_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._touchActive = false;
    this._refreshTimer = null;
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...HC_DEFAULTS, ...config };
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;

    const snap = this._snapshot();
    if (!firstSet && snap === this._lastSnapshot) return;
    this._lastSnapshot = snap;

    // Focus guard
    const active = this.shadowRoot?.activeElement;
    if (
      active &&
      (active.tagName === "INPUT" || active.tagName === "TEXTAREA")
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
    return 1;
  }

  static getStubConfig() {
    return { ...HC_DEFAULTS };
  }

  // ---------------------------------------------------------------------------
  // Snapshot — re-render only when relevant state changes
  // ---------------------------------------------------------------------------

  _snapshot() {
    if (!this._hass) return null;
    const status = this._hass.states[this._config.status_entity];
    const hb = this._hass.states[this._config.heartbeat_entity];
    return JSON.stringify({
      status: status
        ? { s: status.state, checkers: status.attributes?.checkers }
        : null,
      hb: hb ? hb.state : null,
    });
  }

  // ---------------------------------------------------------------------------
  // Refresh timer — re-render periodically for staleness computation
  // ---------------------------------------------------------------------------

  _startRefreshTimer() {
    if (this._refreshTimer) return;
    this._refreshTimer = setInterval(() => {
      this._update();
    }, 15000); // every 15s to keep duration labels fresh
  }

  // ---------------------------------------------------------------------------
  // Data helpers
  // ---------------------------------------------------------------------------

  /** Get the AppDaemon heartbeat staleness in seconds. */
  _heartbeatStaleness() {
    const hb = this._hass?.states?.[this._config.heartbeat_entity];
    if (!hb || !hb.state || hb.state === "unknown" || hb.state === "unavailable") {
      return Infinity;
    }
    // input_datetime state is "YYYY-MM-DD HH:MM:SS" in HA's server TZ.
    // On wall displays, browser TZ == server TZ (same cluster).
    const ts = new Date(hb.state.replace(" ", "T"));
    if (isNaN(ts.getTime())) return Infinity;
    return (Date.now() - ts.getTime()) / 1000;
  }

  /** Format seconds into a human-readable short duration: "2m", "1h", "3d". */
  _formatDuration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "?";
    if (seconds < 60) return `${Math.floor(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  }

  /** Compute the duration string since a check's last_changed timestamp. */
  _sinceLastChanged(lastChanged) {
    if (!lastChanged) return "";
    const ts = new Date(lastChanged);
    if (isNaN(ts.getTime())) return "";
    const seconds = (Date.now() - ts.getTime()) / 1000;
    if (seconds < 60) return "";
    return this._formatDuration(seconds);
  }

  /** Build the status items array for rendering. */
  _buildStatusItems() {
    const items = [];
    const threshold = this._config.stale_threshold_s;

    // AppDaemon heartbeat
    const staleness = this._heartbeatStaleness();
    const adOnline = staleness <= threshold;
    items.push({
      name: "AppDaemon",
      status: adOnline ? "ok" : "critical",
      duration: adOnline ? "" : this._formatDuration(staleness),
    });

    // Registered checkers
    const status = this._hass?.states?.[this._config.status_entity];
    const checkers = status?.attributes?.checkers || {};
    for (const [, checker] of Object.entries(checkers)) {
      const worstCheck = (checker.checks || []).find(
        (c) => c.status === "critical"
      );
      const duration =
        checker.status !== "ok" && worstCheck?.last_changed
          ? this._sinceLastChanged(worstCheck.last_changed)
          : "";
      items.push({
        name: checker.name || "Unknown",
        status: checker.status || "unknown",
        duration,
      });
    }

    return items;
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
      <div class="hc-bar" data-action="open">
        <div class="hc-items"></div>
      </div>
    `;

    this._els = {
      bar: this.shadowRoot.querySelector(".hc-bar"),
      items: this.shadowRoot.querySelector(".hc-items"),
    };

    this._bindEvents();
    this._domBuilt = true;
  }

  _update() {
    if (!this._els) return;

    const items = this._buildStatusItems();

    // Determine overall status for bar styling
    const hasCritical = items.some((i) => i.status === "critical");
    const hasDegraded = items.some((i) => i.status === "degraded");
    const hasUnknown = items.some(
      (i) => i.status === "unknown" && i.name !== "AppDaemon"
    );

    this._els.bar.classList.toggle("bar-critical", hasCritical);
    this._els.bar.classList.toggle("bar-degraded", !hasCritical && hasDegraded);
    this._els.bar.classList.toggle(
      "bar-unknown",
      !hasCritical && !hasDegraded && hasUnknown
    );

    // Render items
    let html = "";
    for (const item of items) {
      const icon = this._statusIcon(item.status);
      const durationHtml = item.duration
        ? `<span class="item-duration">(${item.duration})</span>`
        : "";
      html += `
        <div class="hc-item status-${item.status}">
          <span class="item-icon">${icon}</span>
          <span class="item-name">${this._escapeHtml(item.name)}</span>
          ${durationHtml}
        </div>`;
    }

    // If no items at all, show a loading state
    if (items.length === 0) {
      html = `<div class="hc-item status-unknown">
        <span class="item-icon">⏳</span>
        <span class="item-name">Loading...</span>
      </div>`;
    }

    this._els.items.innerHTML = html;
  }

  _statusIcon(status) {
    switch (status) {
      case "ok":
        return "✅";
      case "degraded":
        return "⚠️";
      case "critical":
        return "❌";
      default:
        return "❓";
    }
  }

  _escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str || "";
    return div.innerHTML;
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
      if (action === "open") {
        this._navigate(this._config.navigation_path);
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

      .hc-bar {
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 8px 16px;
        border-radius: 28px;
        background: rgba(28, 29, 33, 0.32);
        border: 1px solid rgba(255, 255, 255, 0.14);
        box-shadow:
          0 14px 40px rgba(0, 0, 0, 0.28),
          inset 0 1px 0 rgba(255, 255, 255, 0.18),
          inset 0 0 18px rgba(255, 255, 255, 0.04);
        backdrop-filter: blur(14px) saturate(1.2);
        -webkit-backdrop-filter: blur(14px) saturate(1.2);
        transition: filter 120ms ease, border-color 200ms ease;
      }

      .hc-bar:active {
        filter: brightness(0.92);
      }

      .hc-bar.bar-critical {
        border-color: rgba(255, 70, 70, 0.4);
        box-shadow:
          0 14px 40px rgba(0, 0, 0, 0.28),
          inset 0 1px 0 rgba(255, 100, 100, 0.18),
          inset 0 0 18px rgba(255, 70, 70, 0.08);
      }

      .hc-bar.bar-degraded {
        border-color: rgba(255, 180, 50, 0.35);
      }

      .hc-bar.bar-unknown {
        border-color: rgba(180, 180, 180, 0.25);
      }

      .hc-items {
        display: flex;
        align-items: center;
        gap: 16px;
        flex-wrap: wrap;
        justify-content: center;
      }

      .hc-item {
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: 13px;
        font-weight: 500;
        color: rgba(240, 243, 255, 0.9);
        white-space: nowrap;
      }

      .hc-item.status-ok .item-name {
        color: rgba(240, 243, 255, 0.75);
      }

      .hc-item.status-critical .item-name {
        color: rgba(255, 130, 130, 0.95);
        font-weight: 600;
      }

      .hc-item.status-degraded .item-name {
        color: rgba(255, 200, 100, 0.95);
        font-weight: 600;
      }

      .hc-item.status-unknown .item-name {
        color: rgba(200, 200, 210, 0.7);
      }

      .item-icon {
        font-size: 14px;
        line-height: 1;
      }

      .item-name {
        letter-spacing: 0.02em;
      }

      .item-duration {
        font-size: 11px;
        font-weight: 600;
        color: rgba(255, 130, 130, 0.8);
        margin-left: 1px;
      }
    `;
  }
}

customElements.define("health-check-card", HealthCheckCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "health-check-card",
  name: "Health Check Card",
  description: "Compact system health status bar for wall displays",
  preview: true,
});
