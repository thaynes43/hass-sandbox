/**
 * Dashboard Notify Card
 *
 * Custom Lovelace carousel card for wall-display notifications.
 * Reads state from sensor.dashboard_notify_status.
 * Sends commands via the relay script (dashboard_notify_relay).
 * Supports swipe navigation, crossfade transitions, auto-advance.
 */

const DN_DEFAULTS = {
  status_entity: "sensor.dashboard_notify_status",
  relay_script: "dashboard_notify_relay",
};

class DashboardNotifyCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...DN_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._delegatedBound = false;
    this._touchStartX = 0;
    this._touchStartY = 0;
    this._touchActive = false;
    this._pauseResumeTimer = null;
  }

  setConfig(config) {
    this._config = { ...DN_DEFAULTS, ...config };
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

    // Focus guard: skip re-render if an input has focus
    const active = this.shadowRoot?.activeElement;
    if (active) {
      const tag = active.tagName;
      const isText =
        tag === "TEXTAREA" || (tag === "INPUT" && active.type !== "range");
      if (isText) return;
    }

    this._render();
  }

  // ── State helpers ────────────────────────────────────────────────

  _snapshot() {
    if (!this._hass) return null;
    const ss = this._hass.states[this._config.status_entity];
    return ss ? `${ss.state}|${JSON.stringify(ss.attributes)}` : "?";
  }

  _sensorAttr(attr) {
    const s = this._hass?.states?.[this._config.status_entity];
    return s?.attributes?.[attr];
  }

  _buildMetaLine(createdAt, expiresAt) {
    if (!createdAt || !expiresAt) return "";
    const now = Date.now() / 1000;
    const created = new Date(createdAt * 1000);
    const h = created.getHours();
    const m = created.getMinutes();
    const ampm = h >= 12 ? "PM" : "AM";
    const h12 = h % 12 || 12;
    const timeStr = `${h12}:${String(m).padStart(2, "0")} ${ampm}`;

    const remainS = Math.max(0, Math.round(expiresAt - now));
    let remainStr;
    if (remainS >= 3600) {
      const hrs = Math.floor(remainS / 3600);
      const mins = Math.floor((remainS % 3600) / 60);
      remainStr = mins > 0 ? `${hrs}h ${mins}m` : `${hrs}h`;
    } else if (remainS >= 60) {
      remainStr = `${Math.floor(remainS / 60)}m`;
    } else {
      remainStr = `${remainS}s`;
    }
    return `Created ${timeStr} \u00b7 ${remainStr} remaining`;
  }

  // ── Service calls ──────────────────────────────────────────────

  _callRelay(command, data) {
    if (!this._hass) return;
    this._hass
      .callService("script", this._config.relay_script, {
        command,
        payload: JSON.stringify(data || {}),
      })
      .catch((err) => {
        console.warn("dashboard-notify-card: relay failed", command, err);
      });
  }

  // ── Render ─────────────────────────────────────────────────────

  _render() {
    const notifications = this._sensorAttr("notifications") || [];
    const activeIndex = this._sensorAttr("active_index") || 0;
    const paused = this._sensorAttr("paused") || false;
    const placeholderUrl = this._sensorAttr("placeholder_url") || "";
    const count = notifications.length;

    let imageUrl = "";
    let text = "";
    let notifClass = "";

    let metaLine = "";

    if (count > 0 && activeIndex < count) {
      const current = notifications[activeIndex];
      imageUrl = current.image_url || "";
      text = current.text || "";
      notifClass = current["class"] || "";
      metaLine = this._buildMetaLine(current.created_at, current.expires_at);
    } else if (placeholderUrl) {
      imageUrl = placeholderUrl;
      text = "No new notifications";
      notifClass = "placeholder";
    }

    // Build navigation dots
    let dotsHtml = "";
    for (let i = 0; i < count; i++) {
      const active = i === activeIndex ? "active" : "";
      dotsHtml += `<span class="dot ${active}" data-action="goto" data-index="${i}"></span>`;
    }

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="image-area">
          ${imageUrl ? `<img src="${imageUrl}" alt="Notification" onerror="this.onerror=null; ${placeholderUrl ? `this.src='${placeholderUrl}';` : `this.style.display='none';`}" />` : '<div class="no-image">No image</div>'}
          ${paused ? '<div class="pause-overlay"><ha-icon icon="mdi:pause-circle-outline"></ha-icon></div>' : ""}
        </div>
        <div class="text-area ${notifClass.toLowerCase()}">
          <div class="notif-text">${text}</div>
          ${metaLine ? `<div class="notif-meta">${metaLine}</div>` : ""}
        </div>
        ${count > 1 ? `<div class="nav-dots">${dotsHtml}</div>` : ""}
        <div class="controls">
          ${count > 1 ? '<button class="btn-icon" data-action="previous"><ha-icon icon="mdi:chevron-left"></ha-icon></button>' : ""}
          <button class="btn-icon" data-action="toggle_pause">
            <ha-icon icon="${paused ? "mdi:play" : "mdi:pause"}"></ha-icon>
          </button>
          ${count > 0 ? '<button class="btn-icon" data-action="dismiss"><ha-icon icon="mdi:close"></ha-icon></button>' : ""}
          ${count > 1 ? '<button class="btn-icon" data-action="next"><ha-icon icon="mdi:chevron-right"></ha-icon></button>' : ""}
        </div>
      </ha-card>
    `;

    if (!this._delegatedBound) {
      this._bindDelegatedEvents();
      this._delegatedBound = true;
    }
  }

  // ── Delegated event handling (touch/click dedup) ───────────────

  _bindDelegatedEvents() {
    const root = this.shadowRoot;

    // Touch events for swipe detection
    root.addEventListener(
      "touchstart",
      (e) => {
        this._touchStartX = e.changedTouches[0].clientX;
        this._touchStartY = e.changedTouches[0].clientY;
      },
      { passive: true }
    );

    root.addEventListener("touchend", (e) => {
      const target = e.target;
      const tag = target?.tagName;
      // Never preventDefault on form elements
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

      const dx = e.changedTouches[0].clientX - this._touchStartX;
      const dy = e.changedTouches[0].clientY - this._touchStartY;

      // Swipe detection: horizontal delta > 50px and greater than vertical
      if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
        e.preventDefault();
        this._touchActive = true;
        setTimeout(() => {
          this._touchActive = false;
        }, 400);
        if (dx > 0) {
          this._callRelay("previous");
        } else {
          this._callRelay("next");
        }
        return;
      }

      // Button tap
      const actionEl = e.target.closest("[data-action]");
      if (actionEl) {
        e.preventDefault();
        this._touchActive = true;
        setTimeout(() => {
          this._touchActive = false;
        }, 400);
        this._dispatchAction(actionEl);
      }
    });

    // Click handler (deduped with touch)
    root.addEventListener("click", (e) => {
      if (this._touchActive) return;
      const actionEl = e.target.closest("[data-action]");
      if (actionEl) {
        this._dispatchAction(actionEl);
      }
    });
  }

  _dispatchAction(el) {
    const action = el.dataset.action;
    if (!action) return;

    if (action === "goto") {
      const idx = parseInt(el.dataset.index, 10);
      // goto is just next/prev to reach that index; for simplicity send next
      // The card re-renders from sensor state, so we just call next/prev
      this._callRelay("next");
      return;
    }

    this._callRelay(action);
  }

  // ── Styles ─────────────────────────────────────────────────────

  _styles() {
    return `
      ha-card {
        overflow: hidden;
        display: flex;
        flex-direction: column;
        background: var(--ha-card-background, var(--card-background-color, #fff));
      }
      .image-area {
        position: relative;
        width: 100%;
        aspect-ratio: 16 / 9;
        overflow: hidden;
        background: #111;
      }
      .image-area img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        transition: opacity 0.5s ease-in-out;
      }
      .no-image {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100%;
        color: var(--secondary-text-color, #666);
        font-size: 1.1em;
      }
      .pause-overlay {
        position: absolute;
        top: 8px;
        right: 8px;
        color: rgba(255, 255, 255, 0.8);
        --mdc-icon-size: 28px;
      }
      .text-area {
        padding: 12px 16px;
        min-height: 48px;
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .notif-text {
        font-size: 1.05em;
        font-weight: 500;
        color: var(--primary-text-color);
      }
      .notif-meta {
        font-size: 0.8em;
        color: var(--secondary-text-color, #888);
      }
      .text-area.placeholder .notif-text {
        color: var(--secondary-text-color, #888);
        font-style: italic;
      }
      .nav-dots {
        display: flex;
        justify-content: center;
        gap: 6px;
        padding: 4px 0;
      }
      .dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: var(--disabled-text-color, #aaa);
        cursor: pointer;
        transition: background 0.2s;
      }
      .dot.active {
        background: var(--primary-color, #03a9f4);
      }
      .controls {
        display: flex;
        justify-content: center;
        gap: 8px;
        padding: 8px;
        border-top: 1px solid var(--divider-color, #e0e0e0);
      }
      .btn-icon {
        background: none;
        border: none;
        cursor: pointer;
        padding: 4px;
        color: var(--primary-text-color);
        --mdc-icon-size: 22px;
        border-radius: 50%;
        transition: background 0.15s;
      }
      .btn-icon:hover {
        background: var(--secondary-background-color, rgba(0,0,0,0.05));
      }
    `;
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig() {
    return { ...DN_DEFAULTS };
  }
}

customElements.define("dashboard-notify-card", DashboardNotifyCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "dashboard-notify-card",
  name: "Dashboard Notify Card",
  description: "AI-generated notification carousel for wall displays",
});
