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

  connectedCallback() {
    super.connectedCallback?.();
    this._startRefreshTimer();
  }

  disconnectedCallback() {
    super.disconnectedCallback?.();
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
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
  _sinceLastChanged(lastChanged, suppressShort = true) {
    if (!lastChanged) return "";
    const ts = new Date(lastChanged);
    if (isNaN(ts.getTime())) return "";
    const seconds = (Date.now() - ts.getTime()) / 1000;
    if (suppressShort && seconds < 60) return "";
    return this._formatDuration(seconds);
  }

  /** Check if the backend (AppDaemon) heartbeat is fresh. */
  _isBackendOnline() {
    const staleness = this._heartbeatStaleness();
    return staleness <= this._config.stale_threshold_s;
  }

  /** Map checker_id to a dependency icon. */
  _dependencyIcon(checkerId) {
    const map = {
      mqtt_broker: "mdi:access-point",
      zigbee: "mdi:zigbee",
      zwave: "mdi:z-wave",
      cloud: "mdi:cloud",
    };
    return map[checkerId] || "mdi:link-variant";
  }

  /** Sort items by status severity, then alphabetically by name. */
  _sortItems(items) {
    const order = { critical: 0, warning: 1, degraded: 2, unknown: 3, ok: 4 };
    items.sort((a, b) => {
      const s = (order[a.status] ?? 3) - (order[b.status] ?? 3);
      return s !== 0 ? s : (a.name || "").localeCompare(b.name || "");
    });
    return items;
  }

  /** Build a single checker item from its data. */
  _buildCheckerItem(checkerId, checker, backendOnline) {
    if (!backendOnline) {
      return {
        checker_id: checkerId,
        name: checker.name || "Unknown",
        status: "unknown",
        duration: "",
        is_dependency: String(checker.is_dependency) === "true",
      };
    }

    let duration = "";
    if (checker.status !== "ok") {
      const failingChecks = (checker.checks || []).filter(
        (c) => c.status !== "ok" && c.last_changed
      );
      if (failingChecks.length > 0) {
        const earliest = failingChecks.reduce((a, b) =>
          new Date(a.last_changed) < new Date(b.last_changed) ? a : b
        );
        duration = this._sinceLastChanged(earliest.last_changed, false);
      }
    }
    const repairFailed =
      checker.repair_state?.status === "failed" ||
      String(checker.repair_state?.status) === "failed";
    return {
      checker_id: checkerId,
      name: checker.name || "Unknown",
      status: checker.status || "unknown",
      duration,
      is_dependency: String(checker.is_dependency) === "true",
      repair_failed: repairFailed,
    };
  }

  /** Build the status items array, split into dependencies and regular. */
  _buildStatusItems() {
    const backendOnline = this._isBackendOnline();
    const status = this._hass?.states?.[this._config.status_entity];
    const checkers = status?.attributes?.checkers || {};
    const dependencies = [];
    const regularItems = [];

    for (const [checkerId, checker] of Object.entries(checkers)) {
      const item = this._buildCheckerItem(checkerId, checker, backendOnline);
      if (item.is_dependency) {
        dependencies.push(item);
      } else {
        regularItems.push(item);
      }
    }

    return {
      dependencies: this._sortItems(dependencies),
      regularItems: this._sortItems(regularItems),
    };
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
        <div class="hc-header">
          <ha-icon icon="mdi:heart-pulse" class="hc-backend-icon"></ha-icon>
          <span class="hc-title">AppDaemon</span>
        </div>
        <div class="hc-items"></div>
      </div>
    `;

    this._els = {
      bar: this.shadowRoot.querySelector(".hc-bar"),
      backendIcon: this.shadowRoot.querySelector(".hc-backend-icon"),
      items: this.shadowRoot.querySelector(".hc-items"),
    };

    // Optional fixed height (px): pins the card so dashboard columns can be
    // aligned to the pixel; the check list fills the space and scrolls.
    const height = Number(this._config.height);
    if (Number.isFinite(height) && height > 0) {
      this._els.bar.classList.add("fixed-height");
      this._els.bar.style.setProperty("--hc-height", `${Math.round(height)}px`);
    }

    this._bindEvents();
    this._domBuilt = true;
  }

  _update() {
    if (!this._els) return;

    const backendOnline = this._isBackendOnline();
    const { dependencies, regularItems } = this._buildStatusItems();
    const allItems = [...dependencies, ...regularItems];

    // Update backend connectivity icon
    this._els.backendIcon.style.color = backendOnline
      ? "var(--success-color, #4caf50)"
      : "var(--error-color, #f44336)";

    // Determine overall status for bar styling (includes deps + regular)
    const hasCritical = !backendOnline || allItems.some((i) => i.status === "critical");
    const hasDegraded = allItems.some((i) => i.status === "degraded");
    const hasWarning = allItems.some((i) => i.status === "warning");
    const hasUnknown = allItems.some((i) => i.status === "unknown");

    this._els.bar.classList.toggle("bar-critical", hasCritical);
    this._els.bar.classList.toggle("bar-degraded", !hasCritical && hasDegraded);
    this._els.bar.classList.toggle("bar-warning", !hasCritical && !hasDegraded && hasWarning);
    this._els.bar.classList.toggle(
      "bar-unknown",
      !hasCritical && !hasDegraded && !hasWarning && hasUnknown
    );

    // Render all items: dependencies first (with custom icons), then regular checkers
    let html = "";

    // Dependencies — use custom per-checker icons
    for (const dep of dependencies) {
      const depIcon = this._dependencyIcon(dep.checker_id);
      const { color } = this._statusIcon(dep.status);
      const durationHtml = dep.duration
        ? `<span class="item-duration">(${dep.duration})</span>`
        : "";
      html += `
        <div class="hc-item status-${dep.status}">
          <ha-icon icon="${depIcon}" style="color:${color};--mdc-icon-size:16px;" class="item-icon"></ha-icon>
          <span class="item-name">${this._escapeHtml(dep.name)}</span>
          ${durationHtml}
        </div>`;
    }

    // Regular checkers — use status-based icons (robot-dead if repair failed)
    for (const item of regularItems) {
      const { icon, color } = this._statusIcon(item.status);
      const displayIcon = item.repair_failed ? "mdi:robot-dead" : icon;
      const durationHtml = item.duration
        ? `<span class="item-duration">(${item.duration})</span>`
        : "";
      html += `
        <div class="hc-item status-${item.status}">
          <ha-icon icon="${displayIcon}" style="color:${color};--mdc-icon-size:16px;" class="item-icon"></ha-icon>
          <span class="item-name">${this._escapeHtml(item.name)}</span>
          ${durationHtml}
        </div>`;
    }

    // If nothing registered yet, show loading
    if (regularItems.length === 0 && dependencies.length === 0) {
      html = `<div class="hc-item status-unknown">
        <ha-icon icon="mdi:loading" style="color:var(--disabled-color, #9e9e9e);--mdc-icon-size:16px;" class="item-icon"></ha-icon>
        <span class="item-name">Loading...</span>
      </div>`;
    }

    this._els.items.innerHTML = html;
  }

  _statusIcon(status) {
    switch (status) {
      case "ok":
        return { icon: "mdi:check-circle", color: "var(--success-color, #4caf50)" };
      case "warning":
        return { icon: "mdi:alert", color: "var(--warning-color, #ff9800)" };
      case "degraded":
        return { icon: "mdi:alert-circle", color: "var(--warning-color, #ff9800)" };
      case "critical":
        return { icon: "mdi:alert-circle", color: "var(--error-color, #f44336)" };
      default:
        return { icon: "mdi:help-circle", color: "var(--disabled-color, #9e9e9e)" };
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
        flex-direction: column;
        align-items: flex-start;
        gap: 6px;
        padding: 10px 16px;
        border-radius: 20px;
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

      .hc-bar.fixed-height {
        height: var(--hc-height);
        box-sizing: border-box;
      }

      .hc-bar.fixed-height .hc-items {
        flex: 1 1 auto;
        align-self: stretch;
        align-content: flex-start;
        min-height: 0;
        max-height: none;
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

      .hc-bar.bar-warning {
        border-color: rgba(255, 180, 50, 0.3);
      }

      .hc-bar.bar-unknown {
        border-color: rgba(180, 180, 180, 0.25);
      }

      .hc-header {
        display: flex;
        align-items: center;
        gap: 6px;
      }

      .hc-backend-icon {
        --mdc-icon-size: 18px;
        flex-shrink: 0;
        transition: color 200ms ease;
      }

      .hc-title {
        font-size: 13px;
        font-weight: 600;
        color: rgba(240, 243, 255, 0.85);
        letter-spacing: 0.03em;
      }

      .hc-items {
        display: flex;
        align-items: center;
        gap: 6px 16px;
        flex-wrap: wrap;
        max-height: calc(5 * 28px);
        overflow-y: auto;
        overflow-x: hidden;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: thin;
        scrollbar-color: rgba(255,255,255,0.2) transparent;
      }
      .hc-items::-webkit-scrollbar {
        width: 4px;
      }
      .hc-items::-webkit-scrollbar-track {
        background: transparent;
      }
      .hc-items::-webkit-scrollbar-thumb {
        background: rgba(255,255,255,0.2);
        border-radius: 2px;
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

      .hc-item.status-warning .item-name {
        color: rgba(255, 200, 100, 0.95);
        font-weight: 600;
      }

      .hc-item.status-unknown .item-name {
        color: rgba(200, 200, 210, 0.7);
      }

      .item-icon {
        --mdc-icon-size: 16px;
        flex-shrink: 0;
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
