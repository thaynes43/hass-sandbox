/**
 * Health Check Detail Card
 *
 * Comprehensive health status popup card. Shows per-checker details,
 * individual check results, alert history, and a force re-check button.
 *
 * Reads:
 *   sensor.health_check_status           — overall state + per-checker attrs
 *   input_datetime.appdaemon_heartbeat   — last heartbeat timestamp
 *
 * Sends commands via relay script:
 *   force_recheck — triggers all checkers to run immediately
 *
 * Mandatory patterns:
 *   - Shadow DOM
 *   - Touch/click deduplication (400ms flag)
 *   - NEVER preventDefault() on input/select/textarea touchend (Android)
 *   - Focus guard: skip re-render when shadowRoot.activeElement is set
 *
 * Platforms: Desktop, iOS Companion App, Android/UniFi wall display.
 */

const HCD_DEFAULTS = {
  status_entity: "sensor.health_check_status",
  heartbeat_entity: "input_datetime.appdaemon_heartbeat",
  relay_script: "health_check_relay",
  stale_threshold_s: 180,
};

function hcdEscapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

class HealthCheckDetailCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...HCD_DEFAULTS };
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
    this._config = { ...HCD_DEFAULTS, ...config };
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
    return 6;
  }

  static getStubConfig() {
    return { ...HCD_DEFAULTS };
  }

  // ---------------------------------------------------------------------------
  // Snapshot
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
  // Refresh timer
  // ---------------------------------------------------------------------------

  _startRefreshTimer() {
    if (this._refreshTimer) return;
    this._refreshTimer = setInterval(() => {
      this._update();
    }, 15000);
  }

  // ---------------------------------------------------------------------------
  // Data helpers
  // ---------------------------------------------------------------------------

  _heartbeatStaleness() {
    const hb = this._hass?.states?.[this._config.heartbeat_entity];
    if (
      !hb ||
      !hb.state ||
      hb.state === "unknown" ||
      hb.state === "unavailable"
    ) {
      return Infinity;
    }
    const ts = new Date(hb.state.replace(" ", "T"));
    if (isNaN(ts.getTime())) return Infinity;
    return (Date.now() - ts.getTime()) / 1000;
  }

  _formatDuration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "unknown";
    if (seconds < 60) return `${Math.floor(seconds)}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  _formatTimestamp(isoStr) {
    if (!isoStr) return "—";
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  _statusIcon(status) {
    switch (status) {
      case "ok":
        return { icon: "mdi:check-circle", color: "var(--success-color, #4caf50)" };
      case "degraded":
        return { icon: "mdi:alert-circle", color: "var(--warning-color, #ff9800)" };
      case "critical":
        return { icon: "mdi:alert-circle", color: "var(--error-color, #f44336)" };
      default:
        return { icon: "mdi:help-circle", color: "var(--disabled-color, #9e9e9e)" };
    }
  }

  _statusIconHtml(status, size = 18) {
    const { icon, color } = this._statusIcon(status);
    return `<ha-icon icon="${icon}" style="color:${color};--mdc-icon-size:${size}px;"></ha-icon>`;
  }

  _statusLabel(status) {
    switch (status) {
      case "ok":
        return "Healthy";
      case "degraded":
        return "Degraded";
      case "critical":
        return "Critical";
      default:
        return "Unknown";
    }
  }

  // ---------------------------------------------------------------------------
  // Relay script
  // ---------------------------------------------------------------------------

  _callRelay(command, payload) {
    if (!this._hass) return;
    this._hass.callService("script", this._config.relay_script, {
      command,
      payload: JSON.stringify(payload || {}),
    });
  }

  // ---------------------------------------------------------------------------
  // Build DOM
  // ---------------------------------------------------------------------------

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="hcd-root">
        <div class="section appdaemon-section"></div>
        <div class="checkers-section"></div>
        <div class="history-section"></div>
        <div class="actions-section">
          <button class="recheck-btn" data-action="recheck">
            <ha-icon icon="mdi:refresh" style="--mdc-icon-size:16px;"></ha-icon> Force Re-check
          </button>
        </div>
      </div>
    `;

    this._els = {
      adSection: this.shadowRoot.querySelector(".appdaemon-section"),
      checkersSection: this.shadowRoot.querySelector(".checkers-section"),
      historySection: this.shadowRoot.querySelector(".history-section"),
    };

    this._bindEvents();
    this._domBuilt = true;
  }

  _update() {
    if (!this._els) return;
    this._updateAppDaemonSection();
    this._updateCheckersSection();
    this._updateHistorySection();
  }

  _updateAppDaemonSection() {
    const staleness = this._heartbeatStaleness();
    const threshold = this._config.stale_threshold_s;
    const online = staleness <= threshold;
    const status = online ? "ok" : "critical";
    const iconHtml = this._statusIconHtml(status, 18);
    const label = online ? "Online" : "Offline";
    const durationStr = isFinite(staleness)
      ? this._formatDuration(staleness)
      : "no heartbeat received";

    const hbState = this._hass?.states?.[this._config.heartbeat_entity];
    const lastHb = hbState?.state || "—";

    this._els.adSection.innerHTML = `
      <div class="section-header">
        <span class="section-icon">${iconHtml}</span>
        <span class="section-title">AppDaemon</span>
        <span class="section-badge badge-${status}">${label}</span>
      </div>
      <div class="section-body">
        <div class="detail-row">
          <span class="detail-label">Last heartbeat:</span>
          <span class="detail-value">${hcdEscapeHtml(lastHb)}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">Staleness:</span>
          <span class="detail-value">${hcdEscapeHtml(durationStr)}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">Threshold:</span>
          <span class="detail-value">${threshold}s</span>
        </div>
      </div>
    `;
  }

  _updateCheckersSection() {
    const statusEntity =
      this._hass?.states?.[this._config.status_entity];
    const checkers = statusEntity?.attributes?.checkers || {};

    if (Object.keys(checkers).length === 0) {
      this._els.checkersSection.innerHTML = `
        <div class="empty-state">No health checkers registered</div>
      `;
      return;
    }

    let html = "";
    for (const [checkerId, checker] of Object.entries(checkers)) {
      const iconHtml = this._statusIconHtml(checker.status, 18);
      const label = this._statusLabel(checker.status);
      const lastCheck = checker.last_check
        ? this._formatTimestamp(checker.last_check)
        : "never";

      let checksHtml = "";
      for (const check of checker.checks || []) {
        const cIconHtml = this._statusIconHtml(check.status, 14);
        const changedStr = check.last_changed
          ? this._formatTimestamp(check.last_changed)
          : "—";
        checksHtml += `
          <div class="check-row">
            <span class="check-icon">${cIconHtml}</span>
            <span class="check-name">${hcdEscapeHtml(check.name)}</span>
            <span class="check-detail">${hcdEscapeHtml(check.detail)}</span>
            <span class="check-changed">${hcdEscapeHtml(changedStr)}</span>
          </div>
        `;
      }

      html += `
        <div class="section checker-block">
          <div class="section-header">
            <span class="section-icon">${iconHtml}</span>
            <span class="section-title">${hcdEscapeHtml(checker.name || checkerId)}</span>
            <span class="section-badge badge-${checker.status}">${label}</span>
          </div>
          <div class="section-body">
            <div class="detail-row">
              <span class="detail-label">Last check:</span>
              <span class="detail-value">${hcdEscapeHtml(lastCheck)}</span>
            </div>
            <div class="checks-grid">
              <div class="checks-header">
                <span></span>
                <span>Check</span>
                <span>Detail</span>
                <span>Changed</span>
              </div>
              ${checksHtml}
            </div>
          </div>
        </div>
      `;
    }

    this._els.checkersSection.innerHTML = html;
  }

  _updateHistorySection() {
    const statusEntity =
      this._hass?.states?.[this._config.status_entity];
    const checkers = statusEntity?.attributes?.checkers || {};

    // Collect all alerts from all checkers, sorted by timestamp descending
    const allAlerts = [];
    for (const [checkerId, checker] of Object.entries(checkers)) {
      for (const alert of checker.alert_history || []) {
        allAlerts.push({
          ...alert,
          checker_name: checker.name || checkerId,
        });
      }
    }
    allAlerts.sort(
      (a, b) => new Date(b.timestamp) - new Date(a.timestamp)
    );

    if (allAlerts.length === 0) {
      this._els.historySection.innerHTML = `
        <div class="section">
          <div class="section-header">
            <span class="section-icon"><ha-icon icon="mdi:clipboard-text-outline" style="--mdc-icon-size:16px;color:var(--disabled-color, #9e9e9e);"></ha-icon></span>
            <span class="section-title">Alert History</span>
          </div>
          <div class="section-body">
            <div class="empty-state">No alerts recorded</div>
          </div>
        </div>
      `;
      return;
    }

    let alertsHtml = "";
    for (const alert of allAlerts.slice(0, 20)) {
      const alertIconHtml = this._statusIconHtml(alert.to_status, 14);
      alertsHtml += `
        <div class="alert-row">
          <span class="alert-icon">${alertIconHtml}</span>
          <span class="alert-time">${hcdEscapeHtml(this._formatTimestamp(alert.timestamp))}</span>
          <span class="alert-checker">${hcdEscapeHtml(alert.checker_name)}</span>
          <span class="alert-check">${hcdEscapeHtml(alert.check)}</span>
          <span class="alert-transition">${hcdEscapeHtml(alert.from_status)} → ${hcdEscapeHtml(alert.to_status)}</span>
        </div>
      `;
    }

    this._els.historySection.innerHTML = `
      <div class="section">
        <div class="section-header">
          <span class="section-icon"><ha-icon icon="mdi:clipboard-text-outline" style="--mdc-icon-size:16px;color:var(--disabled-color, #9e9e9e);"></ha-icon></span>
          <span class="section-title">Alert History</span>
          <span class="section-count">${allAlerts.length}</span>
        </div>
        <div class="section-body alerts-list">
          ${alertsHtml}
        </div>
      </div>
    `;
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
      if (action === "recheck") {
        this._callRelay("force_recheck", {});
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
  // Styles
  // ---------------------------------------------------------------------------

  _styles() {
    return `
      :host {
        --hcd-radius: 14px;
        --hcd-border: color-mix(in srgb, var(--primary-text-color, #fff) 10%, transparent);
        --hcd-text: var(--primary-text-color, #f5f7fa);
        --hcd-muted: color-mix(in srgb, var(--secondary-text-color, #b6beca) 88%, white 12%);
        --hcd-accent: var(--primary-color, #66b3ff);
        --hcd-ok: #4caf50;
        --hcd-critical: #ef5350;
        --hcd-degraded: #ff9800;
        --hcd-unknown: #9e9e9e;
        display: block;
      }

      .hcd-root {
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding: 4px 0;
      }

      .section {
        border-radius: var(--hcd-radius);
        background: rgba(255, 255, 255, 0.03);
        box-shadow: inset 0 0 0 1px var(--hcd-border);
        overflow: hidden;
      }

      .section-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px 14px 8px;
      }

      .section-icon {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }

      .section-title {
        font-size: 15px;
        font-weight: 700;
        color: var(--hcd-text);
        flex: 1;
      }

      .section-badge {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        padding: 3px 8px;
        border-radius: 6px;
      }

      .badge-ok {
        background: rgba(76, 175, 80, 0.18);
        color: var(--hcd-ok);
      }

      .badge-critical {
        background: rgba(239, 83, 80, 0.18);
        color: var(--hcd-critical);
      }

      .badge-degraded {
        background: rgba(255, 152, 0, 0.18);
        color: var(--hcd-degraded);
      }

      .badge-unknown {
        background: rgba(158, 158, 158, 0.18);
        color: var(--hcd-unknown);
      }

      .section-body {
        padding: 4px 14px 14px;
      }

      .section-count {
        font-size: 11px;
        color: var(--hcd-muted);
        font-weight: 600;
      }

      .detail-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 4px 0;
        font-size: 13px;
      }

      .detail-label {
        color: var(--hcd-muted);
        font-weight: 500;
      }

      .detail-value {
        color: var(--hcd-text);
        font-weight: 500;
        font-variant-numeric: tabular-nums;
      }

      .checks-grid {
        margin-top: 8px;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .checks-header {
        display: grid;
        grid-template-columns: 24px 1fr 1fr auto;
        gap: 8px;
        padding: 4px 0;
        font-size: 10px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: var(--hcd-muted);
        border-bottom: 1px solid var(--hcd-border);
      }

      .check-row {
        display: grid;
        grid-template-columns: 24px 1fr 1fr auto;
        gap: 8px;
        align-items: center;
        padding: 6px 0;
        font-size: 12px;
      }

      .check-row + .check-row {
        border-top: 1px solid rgba(255, 255, 255, 0.04);
      }

      .check-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
      }

      .check-name {
        color: var(--hcd-text);
        font-weight: 600;
      }

      .check-detail {
        color: var(--hcd-muted);
        font-size: 11px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .check-changed {
        color: var(--hcd-muted);
        font-size: 10px;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }

      .alerts-list {
        max-height: 240px;
        overflow-y: auto;
      }

      .alert-row {
        display: grid;
        grid-template-columns: 20px auto 1fr 1fr auto;
        gap: 6px;
        align-items: center;
        padding: 5px 0;
        font-size: 11px;
      }

      .alert-row + .alert-row {
        border-top: 1px solid rgba(255, 255, 255, 0.04);
      }

      .alert-icon {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }

      .alert-time {
        color: var(--hcd-muted);
        font-variant-numeric: tabular-nums;
        font-size: 10px;
        white-space: nowrap;
      }

      .alert-checker {
        color: var(--hcd-accent);
        font-weight: 600;
        font-size: 11px;
      }

      .alert-check {
        color: var(--hcd-text);
        font-size: 11px;
      }

      .alert-transition {
        color: var(--hcd-muted);
        font-size: 10px;
        white-space: nowrap;
      }

      .empty-state {
        padding: 12px 14px;
        font-size: 12px;
        color: var(--hcd-muted);
        font-style: italic;
        text-align: center;
      }

      .checker-block {
        margin-bottom: 0;
      }

      .actions-section {
        display: flex;
        justify-content: center;
        padding: 4px 0;
      }

      .recheck-btn {
        all: unset;
        cursor: pointer;
        padding: 10px 20px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid var(--hcd-border);
        color: var(--hcd-accent);
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.02em;
        transition: background 120ms ease, border-color 120ms ease;
      }

      .recheck-btn:hover {
        background: rgba(255, 255, 255, 0.1);
        border-color: rgba(255, 255, 255, 0.2);
      }

      .recheck-btn:active {
        background: rgba(255, 255, 255, 0.04);
      }

      /* Apply section styling to direct children */
      .appdaemon-section {
        border-radius: var(--hcd-radius);
        background: rgba(255, 255, 255, 0.03);
        box-shadow: inset 0 0 0 1px var(--hcd-border);
        overflow: hidden;
      }

      .history-section .section {
        /* Already styled by .section class */
      }
    `;
  }
}

customElements.define("health-check-detail-card", HealthCheckDetailCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "health-check-detail-card",
  name: "Health Check Detail Card",
  description: "Comprehensive system health status with alert history",
  preview: true,
});
