/**
 * Photo Frame Viewer Card
 *
 * Custom Lovelace card for controlling the photo frame slideshow.
 * Reads state from sensor.<prefix>_photo_frame_status attributes.
 * Sends pause/interval commands via the relay script.
 * Navigation (prev/next) uses input_select directly.
 * Designed to visually match the Immich Fetcher Config card.
 */

const PFV_DEFAULTS = {
  status_entity: "sensor.wall_display_photo_frame_status",
  picker_entity: "input_select.wall_display_photo_frame_image",
  relay_script: "wall_display_photo_frame_relay",
  min_interval: 1,
  max_interval: 120,
  step_interval: 1,
  min_auto_resume: 1,
  max_auto_resume: 86400,
  step_auto_resume: 1,
  default_auto_resume: 600,
};

class PhotoFrameViewerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...PFV_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._delegatedBound = false;
    this._intervalDebounce = null;
    this._autoResumeDebounce = null;
  }

  setConfig(config) {
    this._config = { ...PFV_DEFAULTS, ...config };
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;
    if (firstSet) {
      this._render();
      this._lastSnapshot = this._snapshot();
      return;
    }
    const snap = this._snapshot();
    if (snap === this._lastSnapshot) return;
    this._lastSnapshot = snap;
    this._render();
  }

  // ── State helpers ─────────────────────────────────────────────────

  _snapshot() {
    if (!this._hass) return null;
    const ss = this._hass.states[this._config.status_entity];
    const ps = this._hass.states[this._config.picker_entity];
    const statusPart = ss
      ? `${ss.state}|${JSON.stringify(ss.attributes)}`
      : "?";
    const pickerPart = ps
      ? `${ps.state}|${JSON.stringify(ps.attributes.options)}`
      : "?";
    return `${statusPart}~${pickerPart}`;
  }

  _sensorAttr(attr) {
    const s = this._hass?.states?.[this._config.status_entity];
    return s?.attributes?.[attr];
  }

  _pickerState() {
    return this._hass?.states?.[this._config.picker_entity];
  }

  // ── Service calls ─────────────────────────────────────────────────

  /**
   * Send a command to AppDaemon via the relay script.
   * Uses callService (works for all authenticated users, including non-admin).
   */
  _callRelay(command, data) {
    if (!this._hass) return;
    this._hass
      .callService("script", this._config.relay_script, {
        command,
        payload: JSON.stringify(data || {}),
      })
      .catch((err) => {
        console.warn("photo-frame-viewer-card: relay failed", command, err);
      });
  }

  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data);
  }

  // ── Render ────────────────────────────────────────────────────────

  _render() {
    // Don't re-render while a text input is focused (prevents focus loss on typing).
    // Range sliders are exempt — they don't lose meaningful state on re-render.
    const active = this.shadowRoot?.activeElement;
    if (active) {
      const tag = active.tagName;
      const isText = tag === "TEXTAREA" ||
        (tag === "INPUT" && active.type !== "range");
      if (isText) return;
    }

    const paused = this._sensorAttr("paused") === "true";
    const interval =
      parseFloat(this._sensorAttr("interval_seconds")) ||
      this._config.min_interval;
    const pickerSt = this._pickerState();
    const currentLabel = pickerSt?.state || "—";
    const options = pickerSt?.attributes?.options || [];
    const minInterval = this._config.min_interval;
    const maxInterval = this._config.max_interval;
    const stepInterval = this._config.step_interval;
    const autoResumeRaw =
      parseFloat(this._sensorAttr("pause_auto_resume_s")) ||
      0;
    const autoResumeEnabled = autoResumeRaw > 0;
    const autoResumeSeconds = autoResumeEnabled
      ? autoResumeRaw
      : this._config.default_auto_resume;

    const statusIcon = paused ? "mdi:pause-circle" : "mdi:play-circle";
    const statusLabel = paused ? "Paused" : "Playing";
    const statusColor = paused
      ? "var(--pfv-warning, var(--warning-color, #ff9800))"
      : "var(--pfv-success, var(--success-color, #4caf50))";

    const currentIdx = options.indexOf(currentLabel);
    const total = options.length;
    const position = total > 0 ? `${currentIdx + 1} / ${total}` : "—";

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="card-header">
          <div class="header-row">
            <ha-icon icon="mdi:image-frame" class="header-icon"></ha-icon>
            <span class="header-title">Photo Frame</span>
          </div>
        </div>
        <div class="card-content">

          <div class="status-bar">
            <div class="status-row">
              <ha-icon icon="${statusIcon}" style="color:${statusColor};--mdc-icon-size:18px;" class="status-icon"></ha-icon>
              <span class="status-label">${statusLabel}</span>
              <span class="status-position">${position}</span>
              <button class="btn-icon pause-btn" data-action="toggle-pause" title="${paused ? "Resume" : "Pause"}">
                <ha-icon icon="${paused ? "mdi:play" : "mdi:pause"}"></ha-icon>
              </button>
            </div>
            <div class="status-detail">
              <span class="current-photo" title="${this._escapeHtml(currentLabel)}">${this._escapeHtml(currentLabel)}</span>
            </div>
          </div>

          <div class="nav-row">
            <button class="nav-btn" data-action="prev" ${total <= 1 ? "disabled" : ""}>
              <ha-icon icon="mdi:skip-previous"></ha-icon>
              <span>Previous</span>
            </button>
            <button class="nav-btn" data-action="next" ${total <= 1 ? "disabled" : ""}>
              <ha-icon icon="mdi:skip-next"></ha-icon>
              <span>Next</span>
            </button>
          </div>

          <div class="section">
            <div class="section-header">
              <span class="section-title">Settings</span>
            </div>
            <div class="settings-content">
              <div class="field-group">
                <label>Slide interval: ${interval}s</label>
                <input type="range"
                  min="${minInterval}" max="${maxInterval}" step="${stepInterval}"
                  value="${interval}"
                  data-action="set-interval" />
              </div>
              <div class="field-group checkbox-group">
                <label class="checkbox-label">
                  <input
                    type="checkbox"
                    ${autoResumeEnabled ? "checked" : ""}
                    data-action="toggle-auto-resume" />
                  <span>Enable auto unpause</span>
                </label>
              </div>
              ${autoResumeEnabled ? `
                <div class="field-group">
                  <label>Auto unpause after seconds</label>
                  <input
                    type="number"
                    min="${this._config.min_auto_resume}"
                    max="${this._config.max_auto_resume}"
                    step="${this._config.step_auto_resume}"
                    value="${autoResumeSeconds}"
                    data-action="set-auto-resume" />
                </div>
              ` : ""}
            </div>
          </div>

        </div>
      </ha-card>
    `;
    this._ensureDelegatedListeners();
  }

  // ── Delegated event handling ───────────────────────────────────────

  /**
   * Single delegated event system on the shadow root.
   * Handles touch deduplication so actions don't fire twice on touch devices.
   */
  _ensureDelegatedListeners() {
    if (this._delegatedBound) return;
    this._delegatedBound = true;

    const root = this.shadowRoot;
    let touchActive = false;
    let touchCancelled = false;

    const findTarget = (e) => {
      for (const el of e.composedPath()) {
        if (el instanceof Element && el.dataset?.action) return el;
      }
      return null;
    };

    const dispatchAction = (el) => {
      const action = el.dataset.action;
      if (action === "toggle-pause") {
        this._callRelay("toggle_pause");
      } else if (action === "prev") {
        if (!el.disabled) {
          this._callService("input_select", "select_previous", {
            entity_id: this._config.picker_entity,
            cycle: true,
          });
        }
      } else if (action === "next") {
        if (!el.disabled) {
          this._callService("input_select", "select_next", {
            entity_id: this._config.picker_entity,
            cycle: true,
          });
        }
      } else if (action === "toggle-auto-resume") {
        const seconds = el.checked ? this._currentAutoResumeSeconds() : 0;
        this._callRelay("set_pause_auto_resume", { seconds });
      }
    };

    ["touchcancel", "touchmove", "scroll"].forEach((evt) => {
      root.addEventListener(evt, () => { touchCancelled = true; }, { passive: true });
    });

    root.addEventListener(
      "touchstart",
      () => {
        touchActive = true;
        touchCancelled = false;
      },
      { passive: true }
    );

    root.addEventListener("touchend", (e) => {
      const el = findTarget(e);
      if (touchCancelled || !el) {
        touchActive = false;
        return;
      }
      // Never preventDefault on native form elements — Android webviews
      // will not open keyboards, dropdowns, or date pickers.
      const tag = el.tagName?.toLowerCase();
      const nativeEl =
        tag === "input" || tag === "select" || tag === "textarea";
      if (!nativeEl && e.cancelable) e.preventDefault();

      dispatchAction(el);
      setTimeout(() => { touchActive = false; }, 400);
    });

    // Desktop click — skip if touch just handled it to prevent double-fire.
    root.addEventListener("click", (e) => {
      if (touchActive) return;
      const el = findTarget(e);
      if (el) dispatchAction(el);
    });

    // Range slider: update label immediately, debounce the relay call.
    root.addEventListener("input", (e) => {
      const el = e.target;
      if (el?.dataset?.action === "set-interval") {
        const val = parseFloat(el.value);
        if (isNaN(val) || val <= 0) return;

        // Update label text instantly without a full re-render.
        const label = root.querySelector(".field-group label");
        if (label) label.textContent = `Slide interval: ${val}s`;

        // Debounce the relay call — only send after the user stops dragging.
        clearTimeout(this._intervalDebounce);
        this._intervalDebounce = setTimeout(() => {
          this._callRelay("set_interval", { seconds: val });
        }, 400);
      } else if (el?.dataset?.action === "set-auto-resume") {
        const val = parseFloat(el.value);
        if (isNaN(val) || val < this._config.min_auto_resume) return;

        clearTimeout(this._autoResumeDebounce);
        this._autoResumeDebounce = setTimeout(() => {
          this._callRelay("set_pause_auto_resume", { seconds: val });
        }, 400);
      }
    });
  }

  // ── Styles ────────────────────────────────────────────────────────

  _styles() {
    return `
      :host {
        --pfv-radius: 12px;
        --pfv-radius-sm: 8px;
        --pfv-spacing: 16px;
        --pfv-spacing-sm: 8px;
        --pfv-surface: var(--card-background-color, var(--ha-card-background, #fff));
        --pfv-surface-variant: var(--secondary-background-color, #f5f5f5);
        --pfv-on-surface: var(--primary-text-color, #212121);
        --pfv-on-surface-secondary: var(--secondary-text-color, #757575);
        --pfv-primary: var(--primary-color, #03a9f4);
        --pfv-primary-light: color-mix(in srgb, var(--pfv-primary) 15%, transparent);
        --pfv-border: var(--divider-color, #e0e0e0);
        --pfv-success: var(--success-color, #4caf50);
        --pfv-warning: var(--warning-color, #ff9800);
      }

      ha-card {
        overflow: hidden;
      }

      .card-header {
        padding: var(--pfv-spacing) var(--pfv-spacing) 0;
      }

      .header-row {
        display: flex;
        align-items: center;
        gap: var(--pfv-spacing-sm);
      }

      .header-icon {
        color: var(--pfv-primary);
        --mdc-icon-size: 24px;
      }

      .header-title {
        font-size: 18px;
        font-weight: 500;
        color: var(--pfv-on-surface);
      }

      .card-content {
        padding: var(--pfv-spacing);
        display: flex;
        flex-direction: column;
        gap: var(--pfv-spacing);
      }

      /* Status bar */
      .status-bar {
        background: var(--pfv-surface-variant);
        border-radius: var(--pfv-radius-sm);
        padding: 12px;
      }

      .status-row {
        display: flex;
        align-items: center;
        gap: var(--pfv-spacing-sm);
      }

      .status-icon {
        --mdc-icon-size: 18px;
        flex-shrink: 0;
      }

      .status-label {
        font-weight: 500;
        font-size: 14px;
      }

      .status-position {
        flex: 1;
        text-align: right;
        font-size: 13px;
        color: var(--pfv-on-surface-secondary);
        margin-right: 4px;
      }

      .status-detail {
        margin-top: 6px;
        font-size: 12px;
        color: var(--pfv-on-surface-secondary);
      }

      .current-photo {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      /* Pause button */
      .pause-btn {
        --mdc-icon-size: 20px;
      }

      /* Navigation */
      .nav-row {
        display: flex;
        gap: var(--pfv-spacing-sm);
      }

      .nav-btn {
        flex: 1;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        padding: 10px 8px;
        border: 1px solid var(--pfv-border);
        border-radius: var(--pfv-radius-sm);
        background: var(--pfv-surface);
        color: var(--pfv-on-surface);
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        transition: all 150ms;
        font-family: inherit;
        --mdc-icon-size: 18px;
      }

      .nav-btn:hover:not(:disabled) {
        background: var(--pfv-surface-variant);
      }

      .nav-btn:active:not(:disabled) {
        background: var(--pfv-primary-light);
      }

      .nav-btn:disabled {
        opacity: 0.35;
        cursor: default;
      }

      /* Section */
      .section {
        border: 1px solid var(--pfv-border);
        border-radius: var(--pfv-radius);
        overflow: hidden;
      }

      .section-header {
        display: flex;
        align-items: center;
        padding: 12px var(--pfv-spacing);
        background: var(--pfv-surface-variant);
        border-bottom: 1px solid var(--pfv-border);
      }

      .section-title {
        font-weight: 500;
        font-size: 14px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--pfv-on-surface-secondary);
      }

      .settings-content {
        padding: var(--pfv-spacing);
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .field-group {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .field-group label {
        font-size: 12px;
        font-weight: 500;
        color: var(--pfv-on-surface-secondary);
      }

      .checkbox-group {
        gap: 0;
      }

      .checkbox-label {
        display: flex;
        align-items: center;
        gap: 10px;
        color: var(--pfv-on-surface);
        font-size: 14px;
      }

      .checkbox-label input[type="checkbox"] {
        width: 16px;
        height: 16px;
        accent-color: var(--pfv-primary);
      }

      input[type="range"] {
        width: 100%;
        accent-color: var(--pfv-primary);
      }

      input[type="number"] {
        width: 100%;
        box-sizing: border-box;
        padding: 10px 12px;
        border: 1px solid var(--pfv-border);
        border-radius: var(--pfv-radius-sm);
        background: var(--pfv-surface);
        color: var(--pfv-on-surface);
        font: inherit;
      }

      /* Icon buttons */
      .btn-icon {
        background: none;
        border: none;
        cursor: pointer;
        padding: 4px;
        border-radius: 50%;
        color: var(--pfv-on-surface-secondary);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        transition: all 150ms;
      }

      .btn-icon:hover {
        background: var(--pfv-surface-variant);
        color: var(--pfv-on-surface);
      }
    `;
  }

  // ── Utilities ─────────────────────────────────────────────────────

  _escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  _currentAutoResumeSeconds() {
    const input = this.shadowRoot?.querySelector('[data-action="set-auto-resume"]');
    const val = parseFloat(input?.value);
    if (!isNaN(val) && val >= this._config.min_auto_resume) return val;
    const current = parseFloat(this._sensorAttr("pause_auto_resume_s"));
    if (!isNaN(current) && current >= this._config.min_auto_resume) return current;
    return this._config.default_auto_resume;
  }

  // ── HA Card Boilerplate ───────────────────────────────────────────

  getCardSize() {
    return 3;
  }

  static getConfigElement() {
    return document.createElement("photo-frame-viewer-card-editor");
  }

  static getStubConfig() {
    return {};
  }
}

class PhotoFrameViewerCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <div style="padding: 16px;">
        <p style="color: var(--secondary-text-color, #757575); font-size: 14px;">
          This card reads state from <code>sensor.&lt;prefix&gt;_photo_frame_status</code>
          and sends commands via the relay script. No configuration required for
          the default <code>wall_display</code> prefix.
        </p>
      </div>
    `;
  }

  configChanged(newConfig) {
    const event = new Event("config-changed", { bubbles: true, composed: true });
    event.detail = { config: newConfig };
    this.dispatchEvent(event);
  }
}

customElements.define("photo-frame-viewer-card", PhotoFrameViewerCard);
customElements.define("photo-frame-viewer-card-editor", PhotoFrameViewerCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "photo-frame-viewer-card",
  name: "Photo Frame Viewer",
  description: "Control photo frame slideshow: pause, interval, and image navigation.",
  preview: true,
});
