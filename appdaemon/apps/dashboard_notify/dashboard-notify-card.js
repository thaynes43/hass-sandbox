/**
 * Dashboard Notify Card
 *
 * Custom Lovelace carousel card for wall-display notifications.
 * Reads state from sensor.dashboard_notify_status.
 * Sends commands via the relay script (dashboard_notify_relay).
 * Supports swipe navigation with interactive drag, horizontal slide
 * transitions on advance, and touch/click deduplication.
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
    this._domBuilt = false;
    this._touchActive = false;

    // Slide/swipe state
    this._sliding = false;
    this._swipeDragX = 0;
    this._swipeStartX = 0;
    this._swipeStartY = 0;
    this._swipeLocked = false; // true once we know it's horizontal
    this._prevImageUrl = "";
    this._prevText = "";
    this._prevMeta = "";
  }

  setConfig(config) {
    this._config = { ...DN_DEFAULTS, ...config };
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;

    const snap = this._snapshot();
    if (!firstSet && snap === this._lastSnapshot) return;

    const oldIndex = this._lastActiveIndex ?? -1;
    this._lastSnapshot = snap;

    const notifications = this._sensorAttr("notifications") || [];
    const activeIndex = this._sensorAttr("active_index") || 0;
    this._lastActiveIndex = activeIndex;

    if (!this._domBuilt) {
      this._buildDom();
      this._update();
      return;
    }

    // Focus guard
    const active = this.shadowRoot?.activeElement;
    if (active) {
      const tag = active.tagName;
      if (tag === "TEXTAREA" || (tag === "INPUT" && active.type !== "range")) return;
    }

    // Determine slide direction when index changes and we have multiple items
    if (!this._sliding && notifications.length > 1 && oldIndex >= 0 && oldIndex !== activeIndex) {
      const count = notifications.length;
      // Determine direction: going forward or backward?
      const fwd =
        (activeIndex === (oldIndex + 1) % count) ||
        (oldIndex === count - 1 && activeIndex === 0);
      this._slideTransition(fwd ? "left" : "right");
    } else if (!this._sliding) {
      this._update();
    }
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

  _getDisplayData() {
    const notifications = this._sensorAttr("notifications") || [];
    const activeIndex = this._sensorAttr("active_index") || 0;
    const paused = this._sensorAttr("paused") || false;
    const placeholderUrl = this._sensorAttr("placeholder_url") || "";
    const count = notifications.length;

    let imageUrl = "", text = "", notifClass = "", metaLine = "";

    if (count > 0 && activeIndex < count) {
      const c = notifications[activeIndex];
      imageUrl = c.image_url || "";
      text = c.text || "";
      notifClass = c["class"] || "";
      metaLine = this._buildMetaLine(c.created_at, c.expires_at);
    } else if (placeholderUrl) {
      imageUrl = placeholderUrl;
      text = "No new notifications";
      notifClass = "placeholder";
    }

    return { notifications, activeIndex, paused, placeholderUrl, count, imageUrl, text, notifClass, metaLine };
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

  // ── DOM build (once) ────────────────────────────────────────────

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="image-area">
          <div class="slide-container">
            <div class="slide slide-prev"><img /></div>
            <div class="slide slide-current"><img /></div>
            <div class="slide slide-next"><img /></div>
          </div>
          <div class="pause-overlay" style="display:none">
            <ha-icon icon="mdi:pause-circle-outline"></ha-icon>
          </div>
          <button class="dismiss-btn" data-action="dismiss" style="display:none">
            <ha-icon icon="mdi:delete-outline"></ha-icon>
          </button>
        </div>
        <div class="text-area">
          <div class="notif-text"></div>
          <div class="notif-meta"></div>
        </div>
        <div class="nav-dots"></div>
        <div class="controls">
          <button class="btn-icon btn-prev" data-action="previous" style="display:none">
            <ha-icon icon="mdi:chevron-left"></ha-icon>
          </button>
          <button class="btn-icon" data-action="toggle_pause">
            <ha-icon icon="mdi:pause"></ha-icon>
          </button>
          <button class="btn-icon btn-next" data-action="next" style="display:none">
            <ha-icon icon="mdi:chevron-right"></ha-icon>
          </button>
        </div>
      </ha-card>
    `;

    // Cache element refs
    const root = this.shadowRoot;
    this._els = {
      slideContainer: root.querySelector(".slide-container"),
      slidePrev: root.querySelector(".slide-prev img"),
      slideCurrent: root.querySelector(".slide-current img"),
      slideNext: root.querySelector(".slide-next img"),
      pauseOverlay: root.querySelector(".pause-overlay"),
      dismissBtn: root.querySelector(".dismiss-btn"),
      textArea: root.querySelector(".text-area"),
      notifText: root.querySelector(".notif-text"),
      notifMeta: root.querySelector(".notif-meta"),
      navDots: root.querySelector(".nav-dots"),
      btnPrev: root.querySelector(".btn-prev"),
      btnNext: root.querySelector(".btn-next"),
      pauseBtn: root.querySelector('[data-action="toggle_pause"]'),
      pauseIcon: root.querySelector('[data-action="toggle_pause"] ha-icon'),
    };

    this._bindDelegatedEvents();
    this._domBuilt = true;
  }

  // ── Update DOM in place ─────────────────────────────────────────

  _update() {
    if (!this._els) return;
    const d = this._getDisplayData();
    const e = this._els;
    const placeholderUrl = d.placeholderUrl;

    // Current image
    if (d.imageUrl) {
      e.slideCurrent.src = d.imageUrl;
      e.slideCurrent.style.display = "";
      e.slideCurrent.onerror = () => {
        e.slideCurrent.onerror = null;
        if (placeholderUrl) e.slideCurrent.src = placeholderUrl;
        else e.slideCurrent.style.display = "none";
      };
    } else {
      e.slideCurrent.style.display = "none";
    }

    // Save for slide transitions
    this._prevImageUrl = d.imageUrl;
    this._prevText = d.text;
    this._prevMeta = d.metaLine;

    // Text
    e.textArea.className = `text-area ${(d.notifClass || "").toLowerCase()}`;
    e.notifText.textContent = d.text;
    e.notifMeta.textContent = d.metaLine;
    e.notifMeta.style.display = d.metaLine ? "" : "none";

    // Overlays
    e.pauseOverlay.style.display = d.paused ? "" : "none";
    e.dismissBtn.style.display = d.count > 0 ? "" : "none";
    e.pauseIcon.setAttribute("icon", d.paused ? "mdi:play" : "mdi:pause");

    // Nav buttons
    e.btnPrev.style.display = d.count > 1 ? "" : "none";
    e.btnNext.style.display = d.count > 1 ? "" : "none";

    // Nav dots
    if (d.count > 1) {
      let dots = "";
      for (let i = 0; i < d.count; i++) {
        dots += `<span class="dot ${i === d.activeIndex ? "active" : ""}" data-action="goto" data-index="${i}"></span>`;
      }
      e.navDots.innerHTML = dots;
      e.navDots.style.display = "";
    } else {
      e.navDots.innerHTML = "";
      e.navDots.style.display = "none";
    }

    // Reset slide container position
    e.slideContainer.style.transition = "none";
    e.slideContainer.style.transform = "translateX(0)";
  }

  // ── Slide transition ────────────────────────────────────────────

  _slideTransition(direction) {
    if (!this._els) return;
    this._sliding = true;
    const e = this._els;
    const d = this._getDisplayData();
    const placeholderUrl = d.placeholderUrl;

    // Put the old image in prev/next slot, new image in current
    if (direction === "left") {
      // Sliding left: old is current, new arrives from right
      e.slidePrev.src = this._prevImageUrl || "";
      e.slidePrev.parentElement.style.display = "";
      e.slideNext.parentElement.style.display = "none";
      e.slideCurrent.src = d.imageUrl || "";
      e.slideCurrent.onerror = () => {
        e.slideCurrent.onerror = null;
        if (placeholderUrl) e.slideCurrent.src = placeholderUrl;
      };
      // Start with container shifted right so prev is visible
      e.slideContainer.style.transition = "none";
      e.slideContainer.style.transform = "translateX(-100%)";
    } else {
      // Sliding right: old is current, new arrives from left
      e.slideNext.src = this._prevImageUrl || "";
      e.slideNext.parentElement.style.display = "";
      e.slidePrev.parentElement.style.display = "none";
      e.slideCurrent.src = d.imageUrl || "";
      e.slideCurrent.onerror = () => {
        e.slideCurrent.onerror = null;
        if (placeholderUrl) e.slideCurrent.src = placeholderUrl;
      };
      // Start with container shifted left so next is visible
      e.slideContainer.style.transition = "none";
      e.slideContainer.style.transform = "translateX(100%)";
    }

    // Update text immediately
    e.textArea.className = `text-area ${(d.notifClass || "").toLowerCase()}`;
    e.notifText.textContent = d.text;
    e.notifMeta.textContent = d.metaLine;
    e.notifMeta.style.display = d.metaLine ? "" : "none";

    // Force reflow then animate to center
    void e.slideContainer.offsetWidth;
    e.slideContainer.style.transition = "transform 0.35s ease-out";
    e.slideContainer.style.transform = "translateX(0)";

    const onDone = () => {
      e.slideContainer.removeEventListener("transitionend", onDone);
      e.slidePrev.parentElement.style.display = "none";
      e.slideNext.parentElement.style.display = "none";
      this._prevImageUrl = d.imageUrl;
      this._prevText = d.text;
      this._prevMeta = d.metaLine;
      this._sliding = false;

      // Update remaining UI (dots, buttons, etc.)
      this._update();
    };
    e.slideContainer.addEventListener("transitionend", onDone);

    // Safety timeout in case transitionend doesn't fire
    setTimeout(() => {
      if (this._sliding) onDone();
    }, 500);
  }

  // ── Interactive swipe drag ──────────────────────────────────────

  _onSwipeStart(x, y) {
    this._swipeStartX = x;
    this._swipeStartY = y;
    this._swipeDragX = 0;
    this._swipeLocked = false;

    const d = this._getDisplayData();
    if (d.count <= 1) return;

    // Preload neighbor images into prev/next slots
    const prevIdx = (d.activeIndex - 1 + d.count) % d.count;
    const nextIdx = (d.activeIndex + 1) % d.count;
    const e = this._els;
    e.slidePrev.src = d.notifications[prevIdx]?.image_url || "";
    e.slideNext.src = d.notifications[nextIdx]?.image_url || "";
    e.slidePrev.parentElement.style.display = "";
    e.slideNext.parentElement.style.display = "";
    e.slideContainer.style.transition = "none";
  }

  _onSwipeMove(x, y) {
    const d = this._getDisplayData();
    if (d.count <= 1) return;

    const dx = x - this._swipeStartX;
    const dy = y - this._swipeStartY;

    if (!this._swipeLocked) {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
      // Lock to horizontal or vertical
      if (Math.abs(dy) > Math.abs(dx)) return; // vertical scroll, bail
      this._swipeLocked = true;
    }

    this._swipeDragX = dx;
    const e = this._els;
    e.slideContainer.style.transform = `translateX(${dx}px)`;
  }

  _onSwipeEnd() {
    const d = this._getDisplayData();
    if (d.count <= 1 || !this._swipeLocked) {
      this._swipeLocked = false;
      return false;
    }

    const e = this._els;
    const containerWidth = e.slideContainer.offsetWidth || 300;
    const threshold = containerWidth * 0.2;
    const dx = this._swipeDragX;
    this._swipeLocked = false;

    if (Math.abs(dx) > threshold) {
      // Complete the swipe with animation
      const dir = dx > 0 ? "right" : "left";
      const target = dir === "left" ? -containerWidth : containerWidth;
      e.slideContainer.style.transition = "transform 0.25s ease-out";
      e.slideContainer.style.transform = `translateX(${target}px)`;

      const onDone = () => {
        e.slideContainer.removeEventListener("transitionend", onDone);
        e.slidePrev.parentElement.style.display = "none";
        e.slideNext.parentElement.style.display = "none";
        this._callRelay(dir === "left" ? "next" : "previous");
      };
      e.slideContainer.addEventListener("transitionend", onDone);
      setTimeout(() => onDone(), 350);
      return true;
    } else {
      // Snap back
      e.slideContainer.style.transition = "transform 0.2s ease-out";
      e.slideContainer.style.transform = "translateX(0)";
      const cleanup = () => {
        e.slideContainer.removeEventListener("transitionend", cleanup);
        e.slidePrev.parentElement.style.display = "none";
        e.slideNext.parentElement.style.display = "none";
      };
      e.slideContainer.addEventListener("transitionend", cleanup);
      setTimeout(cleanup, 300);
      return true; // consumed the gesture
    }
  }

  // ── Delegated event handling (touch/click dedup) ───────────────

  _bindDelegatedEvents() {
    const root = this.shadowRoot;

    root.addEventListener(
      "touchstart",
      (e) => {
        const t = e.changedTouches[0];
        this._onSwipeStart(t.clientX, t.clientY);
      },
      { passive: true }
    );

    root.addEventListener(
      "touchmove",
      (e) => {
        if (!this._swipeLocked && !this._swipeStartX) return;
        const t = e.changedTouches[0];
        this._onSwipeMove(t.clientX, t.clientY);
        if (this._swipeLocked) e.preventDefault();
      },
      { passive: false }
    );

    root.addEventListener("touchend", (e) => {
      const target = e.target;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;

      // Try swipe completion first
      if (this._onSwipeEnd()) {
        e.preventDefault();
        this._touchActive = true;
        setTimeout(() => { this._touchActive = false; }, 400);
        return;
      }

      // Button tap
      const actionEl = e.target.closest("[data-action]");
      if (actionEl) {
        e.preventDefault();
        this._touchActive = true;
        setTimeout(() => { this._touchActive = false; }, 400);
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
      .slide-container {
        display: flex;
        width: 100%;
        height: 100%;
        will-change: transform;
      }
      .slide {
        flex: 0 0 100%;
        width: 100%;
        height: 100%;
      }
      .slide-prev {
        margin-left: -100%;
      }
      .slide-next {
        /* sits naturally after current */
      }
      .slide img {
        width: 100%;
        height: 100%;
        object-fit: cover;
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
        pointer-events: none;
      }
      .dismiss-btn {
        position: absolute;
        top: 6px;
        left: 6px;
        background: rgba(0, 0, 0, 0.45);
        border: none;
        border-radius: 50%;
        width: 32px;
        height: 32px;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        color: rgba(255, 255, 255, 0.85);
        --mdc-icon-size: 18px;
        padding: 0;
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
