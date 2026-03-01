/**
 * Photo Frame Viewer Card
 *
 * Custom Lovelace card for controlling the photo frame slideshow.
 * Reads/writes HA entities for pause, interval, and image selection.
 * Designed to visually match the Immich Fetcher Config card.
 */

const PFV_ENTITIES = {
  paused: "input_boolean.wall_display_photo_frame_paused",
  interval: "input_number.wall_display_photo_frame_interval_seconds",
  picker: "input_select.wall_display_photo_frame_image",
  imageUrl: "input_text.wall_display_photo_frame_image_local_url",
};

class PhotoFrameViewerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._lastSnapshot = null;
  }

  setConfig(config) {
    this._config = config;
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

  _snapshot() {
    if (!this._hass) return null;
    const parts = Object.values(PFV_ENTITIES).map((eid) => {
      const s = this._hass.states[eid];
      return s ? `${s.state}|${s.last_updated}` : "?";
    });
    return parts.join("~");
  }

  _state(entityId) {
    const s = this._hass?.states?.[entityId];
    return s ? s.state : null;
  }

  _attr(entityId, attr) {
    const s = this._hass?.states?.[entityId];
    return s?.attributes?.[attr];
  }

  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data);
  }

  // ── Render ────────────────────────────────────────────────────────

  _render() {
    const paused = this._state(PFV_ENTITIES.paused) === "on";
    const interval = parseFloat(this._state(PFV_ENTITIES.interval)) || 5;
    const currentLabel = this._state(PFV_ENTITIES.picker) || "—";
    const options = this._attr(PFV_ENTITIES.picker, "options") || [];
    const minInterval = this._attr(PFV_ENTITIES.interval, "min") ?? 1;
    const maxInterval = this._attr(PFV_ENTITIES.interval, "max") ?? 30;
    const stepInterval = this._attr(PFV_ENTITIES.interval, "step") ?? 1;

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
            </div>
          </div>

        </div>
      </ha-card>
    `;
    this._attachEventListeners();
  }

  // ── Event Listeners ───────────────────────────────────────────────

  _attachEventListeners() {
    const root = this.shadowRoot;

    root.querySelectorAll("[data-action]").forEach((el) => {
      const action = el.dataset.action;

      if (action === "toggle-pause") {
        el.addEventListener("click", () => {
          const paused = this._state(PFV_ENTITIES.paused) === "on";
          this._callService("input_boolean", paused ? "turn_off" : "turn_on", {
            entity_id: PFV_ENTITIES.paused,
          });
        });
      }

      if (action === "prev") {
        el.addEventListener("click", () => {
          this._callService("input_select", "select_previous", {
            entity_id: PFV_ENTITIES.picker,
            cycle: true,
          });
        });
      }

      if (action === "next") {
        el.addEventListener("click", () => {
          this._callService("input_select", "select_next", {
            entity_id: PFV_ENTITIES.picker,
            cycle: true,
          });
        });
      }

      if (action === "set-interval") {
        el.addEventListener("input", () => {
          const val = parseFloat(el.value);
          this._callService("input_number", "set_value", {
            entity_id: PFV_ENTITIES.interval,
            value: val,
          });
        });
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

      input[type="range"] {
        width: 100%;
        accent-color: var(--pfv-primary);
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
          This card requires no configuration. It reads from the
          photo frame viewer entities automatically.
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
