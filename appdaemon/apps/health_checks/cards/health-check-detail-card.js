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
    this._expandedCheckers = new Set();
    this._collapsedCheckers = new Set();
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  disconnectedCallback() {
    // Reset expanded state when popup closes so next open starts collapsed
    this._expandedCheckers.clear();
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  connectedCallback() {
    // Restart refresh timer when re-attached (popup reopened)
    if (this._domBuilt) {
      this._startRefreshTimer();
      this._update();
    }
  }

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

  _formatDurationCompact(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "?";
    if (seconds < 60) return `${Math.floor(seconds)}s`;
    if (seconds < 3600) {
      const m = Math.floor(seconds / 60);
      const s = Math.floor(seconds % 60);
      return s > 0 ? `${m}m ${s}s` : `${m}m`;
    }
    if (seconds < 86400) {
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      return m > 0 ? `${h}h ${m}m` : `${h}h`;
    }
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    return h > 0 ? `${d}d ${h}h` : `${d}d`;
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

  _statusIconHtml(status, size = 18) {
    const { icon, color } = this._statusIcon(status);
    return `<ha-icon icon="${icon}" style="color:${color};--mdc-icon-size:${size}px;"></ha-icon>`;
  }

  _dependencyIconHtml(checkerId, status, size = 18) {
    const DEP_ICONS = {
      mqtt_broker: "mdi:access-point",
      zigbee: "mdi:zigbee",
      zwave: "mdi:z-wave",
      cloud: "mdi:cloud",
    };
    const icon = DEP_ICONS[checkerId] || "mdi:link-variant";
    const { color } = this._statusIcon(status);
    return `<ha-icon icon="${icon}" style="color:${color};--mdc-icon-size:${size}px;"></ha-icon>`;
  }

  _statusLabel(status) {
    switch (status) {
      case "ok":
        return "Healthy";
      case "warning":
        return "Warning";
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
          <button class="clear-history-btn" data-action="clear_history">
            <ha-icon icon="mdi:notification-clear-all" style="--mdc-icon-size:16px;"></ha-icon> Clear Alert History
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
    const rawHb = hbState?.state || "";
    const lastHb = rawHb ? this._formatTimestamp(rawHb.replace(" ", "T")) : "—";

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
    const staleness = this._heartbeatStaleness();
    const backendOnline = staleness <= this._config.stale_threshold_s;

    const statusEntity =
      this._hass?.states?.[this._config.status_entity];
    const checkers = statusEntity?.attributes?.checkers || {};

    if (Object.keys(checkers).length === 0) {
      this._els.checkersSection.innerHTML = `
        <div class="empty-state">No health checkers registered</div>
      `;
      return;
    }

    // Split into dependencies and regular checkers
    const allEntries = Object.entries(checkers);
    const depEntries = allEntries.filter(
      ([, c]) => c.is_dependency === true || c.is_dependency === "true"
    );
    const regularEntries = allEntries.filter(
      ([, c]) => c.is_dependency !== true && c.is_dependency !== "true"
    );

    // Sort both groups: status severity first, then alphabetically by name
    const STATUS_ORDER = { critical: 0, warning: 1, degraded: 2, unknown: 3, ok: 4 };
    const sortEntries = (entries) => {
      entries.sort((a, b) => {
        const aStatus = backendOnline ? a[1].status : "unknown";
        const bStatus = backendOnline ? b[1].status : "unknown";
        const s = (STATUS_ORDER[aStatus] ?? 3) - (STATUS_ORDER[bStatus] ?? 3);
        return s !== 0 ? s : (a[1].name || "").localeCompare(b[1].name || "");
      });
    };
    sortEntries(depEntries);
    sortEntries(regularEntries);

    let html = "";

    // Render Dependencies section
    if (depEntries.length > 0) {
      html += `<div class="section-label">Dependencies</div>`;
      html += this._renderCheckerEntries(depEntries, backendOnline, true);
    }

    // Render Checkers section
    if (regularEntries.length > 0) {
      html += `<div class="section-label">Checkers</div>`;
      html += this._renderCheckerEntries(regularEntries, backendOnline, false);
    }

    this._els.checkersSection.innerHTML = html;
  }

  _renderCheckerEntries(entries, backendOnline, isDependency) {
    let html = "";
    for (const [checkerId, checker] of entries) {
      const effectiveStatus = backendOnline ? checker.status : "unknown";
      const iconHtml = isDependency
        ? this._dependencyIconHtml(checkerId, effectiveStatus, 18)
        : this._statusIconHtml(effectiveStatus, 18);
      const label = this._statusLabel(effectiveStatus);
      const lastCheck = checker.last_check
        ? this._formatTimestamp(checker.last_check)
        : "never";

      // Sort checks alphabetically only — status is shown via icons/colors.
      // Avoids reordering when checks flip between unknown/ok/critical.
      const sortedChecks = [...(checker.checks || [])].sort((a, b) =>
        (a.name || "").localeCompare(b.name || "")
      );

      let checksHtml = "";
      for (const check of sortedChecks) {
        const effectiveCheckStatus = backendOnline ? check.status : "unknown";
        const cIconHtml = this._statusIconHtml(effectiveCheckStatus, 14);
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

      // Repair section (only for checkers that support repair)
      let repairHtml = "";
      if (checker.supports_repair && backendOnline) {
        repairHtml = this._buildRepairHtml(checkerId, checker);
      }

      // Build a compact summary for the header using checks_summary if available
      const checks = checker.checks || [];
      const summary = checker.checks_summary;
      const totalChecks = summary ? summary.total : checks.length;
      const nonOkCount = summary ? summary.non_ok : checks.filter(
        (c) => (backendOnline ? c.status : "unknown") !== "ok"
      ).length;
      const summaryParts = [];
      if (backendOnline && nonOkCount === 0 && totalChecks > 0) {
        summaryParts.push(`${totalChecks}/${totalChecks} checks passing`);
      } else if (backendOnline && nonOkCount > 0) {
        summaryParts.push(`${nonOkCount}/${totalChecks} failing`);
      }
      if (lastCheck !== "never") {
        summaryParts.push(lastCheck);
      }
      const summaryText = summaryParts.join(" · ");

      // Determine if this checker should be expanded
      // Only expand if user explicitly clicked to expand
      const wasExplicitlyExpanded = this._expandedCheckers.has(checkerId);
      const expandedClass = wasExplicitlyExpanded ? "expanded" : "";

      html += `
        <div class="section checker-block ${expandedClass}" data-checker-id="${hcdEscapeHtml(checkerId)}">
          <div class="section-header checker-toggle" data-action="toggle_checker" data-checker="${hcdEscapeHtml(checkerId)}">
            <span class="section-icon">${iconHtml}</span>
            <span class="section-title">${hcdEscapeHtml(checker.name || checkerId)}</span>
            <span class="header-summary">${hcdEscapeHtml(summaryText)}</span>
            <span class="section-badge badge-${effectiveStatus}">${label}</span>
            <span class="chevron"><ha-icon icon="mdi:chevron-down" style="--mdc-icon-size:18px;"></ha-icon></span>
          </div>
          <div class="section-body collapsible-body">
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
            ${repairHtml}
          </div>
        </div>
      `;
    }
    return html;
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
      const prevDuration = alert.previous_state_duration_s;
      let transitionDetail = "";
      if (prevDuration != null && prevDuration > 0) {
        transitionDetail = ` (was ${hcdEscapeHtml(alert.from_status)} for ${this._formatDurationCompact(prevDuration)})`;
      }
      alertsHtml += `
        <div class="alert-row">
          <span class="alert-icon">${alertIconHtml}</span>
          <span class="alert-time">${hcdEscapeHtml(this._formatTimestamp(alert.timestamp))}</span>
          <span class="alert-source"><span class="alert-checker">${hcdEscapeHtml(alert.checker_name)}</span> <span class="alert-arrow">→</span> ${hcdEscapeHtml(alert.check)}</span>
          <span class="alert-transition">${hcdEscapeHtml(alert.from_status)} → ${hcdEscapeHtml(alert.to_status)}${transitionDetail}</span>
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
  // Repair UI builder
  // ---------------------------------------------------------------------------

  _buildRepairHtml(checkerId, checker) {
    const rs = checker.repair_state || {};
    const status = rs.status || "idle";
    const detail = rs.detail || "";
    const enabled = rs.auto_repair_enabled === true || rs.auto_repair_enabled === "true";
    const delayMin = rs.auto_repair_delay_min || 15;
    const deadline = rs.auto_repair_deadline;
    const lastAttempt = rs.last_repair_attempt;

    // Status indicator
    let statusHtml = "";
    if (status === "pending" && deadline) {
      const remaining = Math.max(
        0,
        Math.floor((new Date(deadline).getTime() - Date.now()) / 1000)
      );
      const min = Math.floor(remaining / 60);
      const sec = remaining % 60;
      statusHtml = `
        <div class="repair-status repair-pending">
          <ha-icon icon="mdi:timer-sand" style="--mdc-icon-size:14px;color:var(--hcd-degraded);"></ha-icon>
          <span>Auto-repair in ${min}:${String(sec).padStart(2, "0")}</span>
        </div>
      `;
    } else if (status === "in_progress") {
      statusHtml = `
        <div class="repair-status repair-in-progress">
          <ha-icon icon="mdi:progress-wrench" style="--mdc-icon-size:14px;color:var(--hcd-accent);"></ha-icon>
          <span>${hcdEscapeHtml(detail) || "Repair in progress..."}</span>
        </div>
      `;
    } else if (status === "success") {
      statusHtml = `
        <div class="repair-status repair-success">
          <ha-icon icon="mdi:check-circle" style="--mdc-icon-size:14px;color:var(--hcd-ok);"></ha-icon>
          <span>${hcdEscapeHtml(detail) || "Repair successful"}</span>
        </div>
      `;
    } else if (status === "failed") {
      statusHtml = `
        <div class="repair-status repair-failed-status">
          <ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:14px;color:var(--hcd-critical);"></ha-icon>
          <span>${hcdEscapeHtml(detail) || "Repair failed"}</span>
        </div>
      `;
    }

    // Per-device repair rows (e.g. device_repairs for FanHealthChecker)
    let deviceRepairsHtml = "";
    const fanRepairs = rs.device_repairs || {};
    const nonIdleRepairs = Object.entries(fanRepairs).filter(
      ([, fr]) => fr.status !== "idle"
    );
    if (nonIdleRepairs.length > 0) {
      let rowsHtml = "";
      for (const [name, fr] of nonIdleRepairs) {
        const frIcon = this._repairStatusIcon(fr.status);
        const frDetail = fr.detail || fr.status;
        rowsHtml += `
          <div class="device-repair-row">
            <span class="device-repair-icon">${frIcon}</span>
            <span class="device-repair-name">${hcdEscapeHtml(name)}</span>
            <span class="device-repair-detail">${hcdEscapeHtml(frDetail)}</span>
          </div>
        `;
      }
      deviceRepairsHtml = `<div class="device-repairs">${rowsHtml}</div>`;
    }

    // Last repair attempt
    let lastAttemptHtml = "";
    if (lastAttempt) {
      lastAttemptHtml = `
        <div class="detail-row">
          <span class="detail-label">Last repair:</span>
          <span class="detail-value">${hcdEscapeHtml(this._formatTimestamp(lastAttempt))}</span>
        </div>
      `;
    }

    // Repair button — show when idle, success, or failed; hide during in_progress/pending
    const showBtn = status === "idle" || status === "success" || status === "failed";
    const btnHtml = showBtn
      ? `<button class="repair-btn" data-action="start_repair" data-checker="${hcdEscapeHtml(checkerId)}">
           <ha-icon icon="mdi:wrench" style="--mdc-icon-size:14px;"></ha-icon> Repair
         </button>`
      : "";

    // Auto-repair controls
    const checkedAttr = enabled ? "checked" : "";
    const controlsHtml = `
      <div class="repair-controls">
        ${btnHtml}
        <label class="repair-auto-label">
          <input type="checkbox" class="repair-auto-toggle"
            data-action="toggle_auto_repair" data-checker="${hcdEscapeHtml(checkerId)}"
            ${checkedAttr}>
          Auto-repair
        </label>
        <label class="repair-delay-label">
          <input type="number" class="repair-delay-input"
            data-action="set_repair_delay" data-checker="${hcdEscapeHtml(checkerId)}"
            value="${delayMin}" min="1" max="60" step="1">
          min
        </label>
      </div>
    `;

    return `
      <div class="repair-section">
        ${statusHtml}
        ${deviceRepairsHtml}
        ${lastAttemptHtml}
        ${controlsHtml}
      </div>
    `;
  }

  _repairStatusIcon(status) {
    switch (status) {
      case "in_progress":
        return `<ha-icon icon="mdi:progress-wrench" style="--mdc-icon-size:12px;color:var(--hcd-accent);"></ha-icon>`;
      case "success":
        return `<ha-icon icon="mdi:check-circle" style="--mdc-icon-size:12px;color:var(--hcd-ok);"></ha-icon>`;
      case "failed":
        return `<ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:12px;color:var(--hcd-critical);"></ha-icon>`;
      case "pending":
        return `<ha-icon icon="mdi:timer-sand" style="--mdc-icon-size:12px;color:var(--hcd-degraded);"></ha-icon>`;
      default:
        return `<ha-icon icon="mdi:help-circle" style="--mdc-icon-size:12px;color:var(--hcd-unknown);"></ha-icon>`;
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
      if (action === "recheck") {
        this._callRelay("force_recheck", {});
      } else if (action === "toggle_checker") {
        const cid = el.dataset.checker;
        const block = root.querySelector(
          `.checker-block[data-checker-id="${cid}"]`
        );
        if (block) {
          block.classList.toggle("expanded");
          if (block.classList.contains("expanded")) {
            this._expandedCheckers.add(cid);
            this._collapsedCheckers.delete(cid);
          } else {
            this._expandedCheckers.delete(cid);
            this._collapsedCheckers.add(cid);
          }
        }
      } else if (action === "clear_history") {
        this._callRelay("clear_alert_history", {});
      } else if (action === "start_repair") {
        this._callRelay("start_repair", { checker_id: el.dataset.checker });
      } else if (action === "toggle_auto_repair") {
        const checker_id = el.dataset.checker;
        const auto_repair_enabled = el.checked;
        const delayInput = root.querySelector(
          `.repair-delay-input[data-checker="${checker_id}"]`
        );
        const auto_repair_delay_min = delayInput
          ? parseInt(delayInput.value, 10)
          : 15;
        this._callRelay("update_repair_config", {
          checker_id,
          auto_repair_enabled,
          auto_repair_delay_min,
        });
      } else if (action === "set_repair_delay") {
        const checker_id = el.dataset.checker;
        const auto_repair_delay_min = parseInt(el.value, 10);
        if (isNaN(auto_repair_delay_min) || auto_repair_delay_min < 1) return;
        const toggle = root.querySelector(
          `.repair-auto-toggle[data-checker="${checker_id}"]`
        );
        const auto_repair_enabled = toggle ? toggle.checked : false;
        this._callRelay("update_repair_config", {
          checker_id,
          auto_repair_enabled,
          auto_repair_delay_min,
        });
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

    // Change events for repair controls (checkbox and number input)
    root.addEventListener("change", (e) => {
      const el = e.target;
      if (el.dataset?.action) {
        dispatchAction(el);
      }
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

      .badge-warning {
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
        grid-template-columns: 20px auto 1fr auto;
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

      .alert-source {
        color: var(--hcd-text);
        font-size: 11px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .alert-checker {
        color: var(--hcd-accent);
        font-weight: 600;
      }

      .alert-arrow {
        color: var(--hcd-muted);
        font-size: 10px;
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

      .section-label {
        font-size: 0.75rem;
        text-transform: uppercase;
        color: var(--secondary-text-color, #888);
        padding: 8px 16px 4px;
        letter-spacing: 0.5px;
        font-weight: 600;
      }

      .section-label + .checker-block {
        margin-top: 0;
      }

      .checker-block {
        margin-bottom: 0;
      }

      /* Collapsible checker sections */
      .checker-toggle {
        cursor: pointer;
        user-select: none;
        -webkit-user-select: none;
      }

      .checker-toggle .section-title {
        flex: 0 0 auto;
      }

      .header-summary {
        flex: 1;
        font-size: 11px;
        color: var(--hcd-muted);
        font-weight: 500;
        text-align: right;
        padding-right: 4px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .chevron {
        display: flex;
        align-items: center;
        color: var(--hcd-muted);
        transition: transform 200ms ease;
        flex-shrink: 0;
      }

      .checker-block.expanded .chevron {
        transform: rotate(180deg);
      }

      .collapsible-body {
        max-height: 0;
        overflow: hidden;
        transition: max-height 250ms ease, padding 250ms ease;
        padding: 0 14px;
      }

      .checker-block.expanded .collapsible-body {
        max-height: 800px;
        padding: 4px 14px 14px;
      }

      .checker-block:not(.expanded) .header-summary {
        display: block;
      }

      .checker-block.expanded .header-summary {
        display: none;
      }

      /* Repair section */
      .repair-section {
        margin-top: 10px;
        padding-top: 10px;
        border-top: 1px solid var(--hcd-border);
      }

      .repair-status {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        font-weight: 600;
        padding: 6px 0;
      }

      .repair-pending span { color: var(--hcd-degraded); }
      .repair-in-progress span { color: var(--hcd-accent); }
      .repair-success span { color: var(--hcd-ok); }
      .repair-failed-status span { color: var(--hcd-critical); }

      .repair-controls {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 6px 0;
        flex-wrap: wrap;
      }

      .repair-btn {
        all: unset;
        cursor: pointer;
        padding: 6px 14px;
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid var(--hcd-border);
        color: var(--hcd-accent);
        font-size: 12px;
        font-weight: 600;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        transition: background 120ms ease, border-color 120ms ease;
      }

      .repair-btn:hover {
        background: rgba(255, 255, 255, 0.1);
        border-color: rgba(255, 255, 255, 0.2);
      }

      .repair-btn:active {
        background: rgba(255, 255, 255, 0.04);
      }

      .repair-auto-label,
      .repair-delay-label {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 12px;
        color: var(--hcd-muted);
        font-weight: 500;
        cursor: pointer;
      }

      .repair-auto-toggle {
        width: 14px;
        height: 14px;
        cursor: pointer;
      }

      .repair-delay-input {
        width: 42px;
        padding: 3px 4px;
        border-radius: 4px;
        border: 1px solid var(--hcd-border);
        background: rgba(255, 255, 255, 0.06);
        color: var(--hcd-text);
        font-size: 12px;
        font-variant-numeric: tabular-nums;
        text-align: center;
      }

      .repair-delay-input:focus {
        outline: 1px solid var(--hcd-accent);
        border-color: var(--hcd-accent);
      }

      /* Per-device repair rows */
      .device-repairs {
        margin: 6px 0;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .device-repair-row {
        display: grid;
        grid-template-columns: 18px auto 1fr;
        gap: 6px;
        align-items: center;
        padding: 3px 0;
        font-size: 11px;
      }

      .device-repair-icon {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }

      .device-repair-name {
        color: var(--hcd-text);
        font-weight: 600;
        white-space: nowrap;
      }

      .device-repair-detail {
        color: var(--hcd-muted);
        font-size: 10px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .actions-section {
        display: flex;
        justify-content: center;
        gap: 12px;
        flex-wrap: wrap;
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
        display: inline-flex;
        align-items: center;
        gap: 6px;
        transition: background 120ms ease, border-color 120ms ease;
      }

      .recheck-btn:hover {
        background: rgba(255, 255, 255, 0.1);
        border-color: rgba(255, 255, 255, 0.2);
      }

      .recheck-btn:active {
        background: rgba(255, 255, 255, 0.04);
      }

      .clear-history-btn {
        all: unset;
        cursor: pointer;
        padding: 10px 20px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid var(--hcd-border);
        color: var(--hcd-muted);
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.02em;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        transition: background 120ms ease, border-color 120ms ease;
      }

      .clear-history-btn:hover {
        background: rgba(255, 255, 255, 0.1);
        border-color: rgba(255, 255, 255, 0.2);
      }

      .clear-history-btn:active {
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
