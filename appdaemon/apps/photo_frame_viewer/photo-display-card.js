/**
 * Photo Display Card
 *
 * Fixed-size photo frame card for wall-display slideshows.
 * Reads the current photo from sensor.<prefix>_photo_frame_status and filter
 * metadata from sensor.immich_fetcher_status.
 *
 * This card is the on-dashboard viewer. The separate photo-frame-viewer-card.js
 * remains the controls/config card used inside the popup.
 */

const PDC_DEFAULTS = {
  status_entity: "sensor.wall_display_photo_frame_status",
  picker_entity: "input_select.wall_display_photo_frame_image",
  relay_script: "wall_display_photo_frame_relay",
  fetcher_entity: "sensor.immich_fetcher_status",
  navigation_path: "#photo-frame-settings",
  aspect_ratio: "3 / 4",
  height: "",
  title_label: "Immich Filter",
  subtitle_attr: "",
};

class PhotoDisplayCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...PDC_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._touchActive = false;
    this._swipeStartX = 0;
    this._swipeStartY = 0;
    this._swipeTracking = false;
    this._swipeLocked = false;
    this._swipeDragX = 0;
    this._swipeAnimating = false;
    this._displayedImageUrl = "";
    this._pendingImageUrl = "";
    this._pendingImageDeadline = 0;
  }

  setConfig(config) {
    this._config = { ...PDC_DEFAULTS, ...config };
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;

    const snap = this._snapshot();
    if (!firstSet && snap === this._lastSnapshot) return;
    this._lastSnapshot = snap;

    if (!this._domBuilt) {
      this._buildDom();
      this._update(true);
      return;
    }

    this._update(false);
  }

  disconnectedCallback() {
  }

  _snapshot() {
    if (!this._hass) return null;
    const status = this._hass.states[this._config.status_entity];
    const picker = this._hass.states[this._config.picker_entity];
    const fetcher = this._hass.states[this._config.fetcher_entity];
    return JSON.stringify({
      status: status ? { s: status.state, a: status.attributes } : null,
      picker: picker ? { s: picker.state, o: picker.attributes?.options } : null,
      fetcher: fetcher
        ? {
            a: {
              active_filter_index: fetcher.attributes?.active_filter_index,
              filters: fetcher.attributes?.filters,
              last_fetch_filter: fetcher.attributes?.last_fetch_filter,
            },
          }
        : null,
    });
  }

  _sensorState(entityId) {
    return this._hass?.states?.[entityId] || null;
  }

  _sensorAttr(entityId, attr, fallback = "") {
    const s = this._sensorState(entityId);
    const v = s?.attributes?.[attr];
    return v !== undefined ? v : fallback;
  }

  _parseJsonAttr(entityId, attr, fallback) {
    const raw = this._sensorAttr(entityId, attr, null);
    if (raw === null || raw === undefined) return fallback;
    if (Array.isArray(raw) || typeof raw === "object") return raw;
    try {
      return JSON.parse(raw);
    } catch (_err) {
      return fallback;
    }
  }

  _pickerState() {
    return this._sensorState(this._config.picker_entity);
  }

  _callRelay(command, data) {
    if (!this._hass) return;
    this._hass.callService("script", this._config.relay_script, {
      command,
      payload: JSON.stringify(data || {}),
    });
  }

  _callService(domain, service, data) {
    if (!this._hass) return;
    this._hass.callService(domain, service, data);
  }

  _navigate(path) {
    const targetPath = String(path || "").trim();
    if (!targetPath) return;
    window.history.pushState(null, "", targetPath);
    window.dispatchEvent(new Event("location-changed"));
  }

  _frameImageUrl() {
    const baseUrl = String(
      this._sensorAttr(this._config.status_entity, "image_url", "") || ""
    ).trim();
    if (!baseUrl) return "";

    const cacheBust = String(
      this._sensorAttr(this._config.status_entity, "cache_bust", "") || ""
    ).trim();
    if (!cacheBust) return baseUrl;

    const sep = baseUrl.includes("?") ? "&" : "?";
    return `${baseUrl}${sep}cb=${encodeURIComponent(cacheBust)}`;
  }

  _neighborImageUrl(targetLabel) {
    const baseUrl = this._frameImageUrl();
    const target = String(targetLabel || "").trim();
    if (!baseUrl || !target) return "";

    try {
      const url = new URL(baseUrl, window.location.origin);
      const parts = url.pathname.split("/");
      if (!parts.length) return baseUrl;
      parts[parts.length - 1] = encodeURIComponent(target);
      url.pathname = parts.join("/");
      return `${url.pathname}${url.search}`;
    } catch (_err) {
      const [path, query = ""] = baseUrl.split("?");
      const parts = path.split("/");
      if (!parts.length) return baseUrl;
      parts[parts.length - 1] = encodeURIComponent(target);
      return query ? `${parts.join("/")}?${query}` : parts.join("/");
    }
  }

  _filterTitle() {
    const filters = this._parseJsonAttr(this._config.fetcher_entity, "filters", []);
    const idx = Number(
      this._sensorAttr(this._config.fetcher_entity, "active_filter_index", 0)
    ) || 0;

    if (Array.isArray(filters) && filters[idx]?.name) {
      return String(filters[idx].name);
    }

    const lastFetchFilter = String(
      this._sensorAttr(this._config.fetcher_entity, "last_fetch_filter", "") || ""
    ).trim();
    return lastFetchFilter || "Photo Frame";
  }

  _state() {
    const picker = this._pickerState();
    const options = Array.isArray(picker?.attributes?.options)
      ? picker.attributes.options
      : [];
    const currentLabel = picker?.state || "";
    const pausedRaw = this._sensorAttr(this._config.status_entity, "paused", false);
    const paused = pausedRaw === true || pausedRaw === "true";
    const subtitleAttr = String(this._config.subtitle_attr || "").trim();
    const subtitle = subtitleAttr
      ? String(this._sensorAttr(this._config.status_entity, subtitleAttr, "") || "").trim()
      : "";
    const currentIndex = options.indexOf(currentLabel);
    const count = options.length;
    const prevIndex = count > 0 ? (currentIndex > 0 ? currentIndex - 1 : count - 1) : -1;
    const nextIndex = count > 0 ? (currentIndex >= 0 && currentIndex < count - 1 ? currentIndex + 1 : 0) : -1;
    const prevLabel = prevIndex >= 0 ? String(options[prevIndex] || "") : "";
    const nextLabel = nextIndex >= 0 ? String(options[nextIndex] || "") : "";
    return {
      imageUrl: this._frameImageUrl(),
      paused,
      currentLabel,
      options,
      filterTitle: this._filterTitle(),
      subtitle,
      count,
      currentIndex,
      prevLabel,
      nextLabel,
      prevImageUrl: this._neighborImageUrl(prevLabel),
      nextImageUrl: this._neighborImageUrl(nextLabel),
    };
  }

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="card-shell">
          <div class="card-header">
            <div class="header-copy">
              <div class="header-kicker"></div>
              <div class="header-title"></div>
              <div class="header-subtitle"></div>
            </div>
            <button class="icon-btn settings-btn" data-action="settings" title="Photo frame settings">
              <ha-icon icon="mdi:cog-outline"></ha-icon>
            </button>
          </div>

          <div class="frame-stage" data-swipe-zone="true">
            <div class="frame-backdrop"></div>
            <div class="slide-track">
              <div class="slide-frame slide-prev"><img class="frame-image" alt="" /></div>
              <div class="slide-frame slide-current"><img class="frame-image frame-image-current" alt="" /></div>
              <div class="slide-frame slide-next"><img class="frame-image" alt="" /></div>
            </div>
            <div class="frame-empty">No photo available</div>
            <div class="frame-scrim"></div>
          </div>

          <div class="control-bar">
            <button class="icon-btn nav-btn" data-action="prev" title="Previous photo">
              <ha-icon icon="mdi:chevron-left"></ha-icon>
            </button>
            <button class="icon-btn pause-btn" data-action="toggle-pause" title="Pause slideshow">
              <ha-icon icon="mdi:pause"></ha-icon>
            </button>
            <button class="icon-btn nav-btn" data-action="next" title="Next photo">
              <ha-icon icon="mdi:chevron-right"></ha-icon>
            </button>
            <select class="picker-select" data-action="select-photo"></select>
          </div>
        </div>
      </ha-card>
    `;

    const root = this.shadowRoot;
    this._els = {
      headerKicker: root.querySelector(".header-kicker"),
      headerTitle: root.querySelector(".header-title"),
      headerSubtitle: root.querySelector(".header-subtitle"),
      frameStage: root.querySelector(".frame-stage"),
      backdrop: root.querySelector(".frame-backdrop"),
      slideTrack: root.querySelector(".slide-track"),
      slidePrevEl: root.querySelector(".slide-prev"),
      slideCurrentEl: root.querySelector(".slide-current"),
      slideNextEl: root.querySelector(".slide-next"),
      slidePrev: root.querySelector(".slide-prev img"),
      currentImage: root.querySelector(".frame-image-current"),
      slideNext: root.querySelector(".slide-next img"),
      emptyState: root.querySelector(".frame-empty"),
      pauseIcon: root.querySelector(".pause-btn ha-icon"),
      pickerSelect: root.querySelector(".picker-select"),
    };

    this._bindEvents();
    this._setSlideTransition(false);
    this._setSwipeOffset(0);
    this._domBuilt = true;
  }

  _update(forceImage = false) {
    if (!this._els) return;

    const state = this._state();
    const e = this._els;

    e.headerKicker.textContent = this._config.title_label;
    e.headerTitle.textContent = state.filterTitle;
    e.headerSubtitle.textContent = state.subtitle;
    e.headerSubtitle.style.display = state.subtitle ? "" : "none";
    e.pauseIcon.setAttribute("icon", state.paused ? "mdi:play" : "mdi:pause");

    this._renderPicker(state.options, state.currentLabel);
    this._renderImage(state.imageUrl, forceImage);
  }

  _renderPicker(options, currentLabel) {
    const select = this._els.pickerSelect;
    const nextHtml = options
      .map((option) => {
        const label = this._escapeHtml(String(option));
        const selected = option === currentLabel ? "selected" : "";
        return `<option value="${label}" ${selected}>${label}</option>`;
      })
      .join("");

    if (select.innerHTML !== nextHtml) select.innerHTML = nextHtml;
    if (select.value !== currentLabel) select.value = currentLabel;
    select.disabled = options.length <= 1;
  }

  _setImageElement(img, url) {
    if (!url) {
      img.removeAttribute("src");
      img.style.display = "none";
      return;
    }
    img.style.display = "";
    if (img === this._els.currentImage && img.getAttribute("src") !== url) {
      const replacement = img.cloneNode(false);
      replacement.className = img.className;
      replacement.alt = img.alt;
      replacement.style.display = "";
      replacement.src = url;
      img.parentNode.replaceChild(replacement, img);
      this._els.currentImage = replacement;
      this._els.slideCurrent = replacement;
      return;
    }
    if (img.getAttribute("src") !== url) img.src = url;
  }

  _renderImage(imageUrl, forceImage) {
    const e = this._els;
    const state = this._state();
    const pendingActive =
      this._pendingImageUrl && Date.now() < this._pendingImageDeadline;

    if (pendingActive && imageUrl && imageUrl !== this._pendingImageUrl) {
      this._syncAdjacentSlides(state);
      if (!this._swipeAnimating) this._setSwipeOffset(0);
      return;
    }

    if (imageUrl === this._pendingImageUrl) {
      this._pendingImageUrl = "";
      this._pendingImageDeadline = 0;
    }

    e.backdrop.style.backgroundImage = imageUrl ? `url("${imageUrl}")` : "none";
    e.emptyState.style.display = imageUrl ? "none" : "";

    if (!imageUrl) {
      this._displayedImageUrl = "";
      this._setImageElement(e.currentImage, "");
      this._setImageElement(e.slidePrev, "");
      this._setImageElement(e.slideNext, "");
      this._setSwipeOffset(0);
      return;
    }

    if (forceImage || !this._displayedImageUrl) {
      this._displayedImageUrl = imageUrl;
      this._setImageElement(e.currentImage, imageUrl);
      this._syncAdjacentSlides(state);
      if (!this._swipeAnimating) this._setSwipeOffset(0);
      return;
    }

    if (imageUrl === this._displayedImageUrl) {
      this._syncAdjacentSlides(state);
      if (!this._swipeAnimating) this._setSwipeOffset(0);
      return;
    }

    this._displayedImageUrl = imageUrl;
    this._setImageElement(e.currentImage, imageUrl);
    this._syncAdjacentSlides(state);
    if (!this._swipeAnimating) this._setSwipeOffset(0);
  }

  _syncAdjacentSlides(state = this._state()) {
    this._setImageElement(this._els.slidePrev, state.prevImageUrl || "");
    this._setImageElement(this._els.slideNext, state.nextImageUrl || "");
  }

  _commitSwipeArrival(action) {
    const arrivalImg = action === "next" ? this._els.slideNext : this._els.slidePrev;
    const arrivalUrl = String(arrivalImg?.getAttribute("src") || "").trim();
    if (!arrivalUrl) return;

    this._pendingImageUrl = arrivalUrl;
    this._pendingImageDeadline = Date.now() + 1500;
    this._displayedImageUrl = arrivalUrl;
    this._setImageElement(this._els.currentImage, arrivalUrl);
    this._els.backdrop.style.backgroundImage = `url("${arrivalUrl}")`;
    this._els.emptyState.style.display = "none";
  }

  _setSlideTransition(enabled) {
    const value = enabled ? "transform 180ms ease" : "none";
    this._els.slidePrevEl.style.transition = value;
    this._els.slideCurrentEl.style.transition = value;
    this._els.slideNextEl.style.transition = value;
  }

  _setSwipeOffset(offsetPx) {
    const width = this._els.frameStage.offsetWidth || 300;
    this._els.slidePrevEl.style.transform = `translate3d(${offsetPx - width}px, 0, 0)`;
    this._els.slideCurrentEl.style.transform = `translate3d(${offsetPx}px, 0, 0)`;
    this._els.slideNextEl.style.transform = `translate3d(${offsetPx + width}px, 0, 0)`;
  }

  _startSwipe(x, y) {
    if (this._swipeAnimating) return;
    this._swipeStartX = x;
    this._swipeStartY = y;
    this._swipeTracking = true;
    this._swipeLocked = false;
    this._swipeDragX = 0;
    this._syncAdjacentSlides();
    this._setSlideTransition(false);
    this._setSwipeOffset(0);
  }

  _moveSwipe(x, y) {
    if (!this._swipeTracking || this._swipeAnimating) return false;
    const dx = x - this._swipeStartX;
    const dy = y - this._swipeStartY;

    if (!this._swipeLocked) {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return false;
      if (Math.abs(dy) >= Math.abs(dx)) {
        this._swipeTracking = false;
        return false;
      }
      this._swipeLocked = true;
    }

    this._swipeDragX = dx;
    this._setSwipeOffset(dx);
    return true;
  }

  _finishSwipe() {
    if (this._swipeAnimating) return false;
    const dx = this._swipeDragX;
    const locked = this._swipeLocked;
    this._swipeTracking = false;
    this._swipeLocked = false;
    this._swipeStartX = 0;
    this._swipeStartY = 0;

    if (!locked) {
      this._swipeDragX = 0;
      this._setSlideTransition(true);
      this._setSwipeOffset(0);
      return false;
    }

    const width = this._els.frameStage.offsetWidth || 300;
    const threshold = Math.min(92, width * 0.22);
    if (Math.abs(dx) < threshold) {
      this._swipeDragX = 0;
      this._setSlideTransition(true);
      this._setSwipeOffset(0);
      return false;
    }

    const action = dx < 0 ? "next" : "prev";
    const target = dx < 0 ? -width : width;
    this._swipeAnimating = true;
    this._setSlideTransition(true);
    requestAnimationFrame(() => this._setSwipeOffset(target));

    const done = () => {
      this._els.slideCurrentEl.removeEventListener("transitionend", done);
      this._swipeAnimating = false;
      this._swipeDragX = 0;
      this._commitSwipeArrival(action);
      this._setSlideTransition(false);
      this._setSwipeOffset(0);
      this._handleAction(action);
    };

    this._els.slideCurrentEl.addEventListener("transitionend", done, { once: true });
    return true;
  }

  _handleAction(action, payload) {
    if (!action) return;

    if (action === "settings") {
      this._navigate(this._config.navigation_path);
      return;
    }

    if (action === "toggle-pause") {
      this._callRelay("toggle_pause");
      return;
    }

    if (action === "prev") {
      this._callService("input_select", "select_previous", {
        entity_id: this._config.picker_entity,
        cycle: true,
      });
      return;
    }

    if (action === "next") {
      this._callService("input_select", "select_next", {
        entity_id: this._config.picker_entity,
        cycle: true,
      });
      return;
    }

    if (action === "select-photo") {
      const option = String(payload?.value || "").trim();
      if (!option) return;
      this._callService("input_select", "select_option", {
        entity_id: this._config.picker_entity,
        option,
      });
    }
  }

  _bindEvents() {
    const root = this.shadowRoot;
    const stage = this._els.frameStage;

    const findActionEl = (evt) => {
      for (const node of evt.composedPath()) {
        if (node instanceof Element && node.dataset?.action) return node;
      }
      return null;
    };

    root.addEventListener(
      "touchstart",
      (evt) => {
        const zone = evt
          .composedPath()
          .find((node) => node instanceof Element && node.dataset?.swipeZone === "true");
        if (!zone) return;
        const touch = evt.changedTouches[0];
        this._startSwipe(touch.clientX, touch.clientY);
      },
      { passive: true }
    );

    root.addEventListener(
      "touchmove",
      (evt) => {
        if (!this._swipeTracking) return;
        const touch = evt.changedTouches[0];
        const consumed = this._moveSwipe(touch.clientX, touch.clientY);
        if (consumed && evt.cancelable) evt.preventDefault();
      },
      { passive: false }
    );

    root.addEventListener(
      "touchend",
      (evt) => {
        const actionEl = findActionEl(evt);
        if (actionEl) {
          const tag = actionEl.tagName?.toLowerCase();
          const nativeEl = tag === "select" || tag === "input" || tag === "textarea";
          if (!nativeEl && evt.cancelable) evt.preventDefault();
          this._touchActive = true;
          setTimeout(() => {
            this._touchActive = false;
          }, 400);
          this._handleAction(actionEl.dataset.action);
          this._swipeTracking = false;
          return;
        }

        if (!this._swipeTracking) return;
        const wasLocked = this._swipeLocked;
        if (this._finishSwipe()) {
          this._touchActive = true;
          setTimeout(() => {
            this._touchActive = false;
          }, 400);
        }
        if (wasLocked) return;
      },
      { passive: false }
    );

    stage.addEventListener(
      "touchcancel",
      () => {
        this._swipeTracking = false;
        this._swipeLocked = false;
        this._swipeDragX = 0;
        this._setSlideTransition(false);
        this._setSwipeOffset(0);
      },
      { passive: true }
    );

    root.addEventListener("click", (evt) => {
      if (this._touchActive) return;
      const actionEl = findActionEl(evt);
      if (!actionEl) return;
      this._handleAction(actionEl.dataset.action);
    });

    root.addEventListener("change", (evt) => {
      const target = evt.target;
      if (!(target instanceof HTMLSelectElement)) return;
      if (target.dataset.action !== "select-photo") return;
      this._handleAction("select-photo", { value: target.value });
    });
  }

  _styles() {
    const sizeRule = this._config.height
      ? `height: ${this._config.height};`
      : `aspect-ratio: ${this._config.aspect_ratio};`;

    return `
      :host {
        --pdc-radius: 18px;
        --pdc-radius-sm: 12px;
        --pdc-surface: var(--ha-card-background, var(--card-background-color, #101317));
        --pdc-border: color-mix(in srgb, var(--primary-text-color, #fff) 12%, transparent);
        --pdc-text: var(--primary-text-color, #f5f7fa);
        --pdc-muted: color-mix(in srgb, var(--secondary-text-color, #b6beca) 88%, white 12%);
        --pdc-accent: var(--primary-color, #66b3ff);
      }

      ha-card {
        overflow: hidden;
        background:
          radial-gradient(circle at top, rgba(115, 160, 255, 0.16), transparent 34%),
          linear-gradient(180deg, rgba(12, 15, 20, 0.98), rgba(19, 24, 31, 0.98));
      }

      .card-shell {
        padding: 14px;
        display: grid;
        gap: 12px;
      }

      .card-header {
        display: flex;
        align-items: start;
        justify-content: space-between;
        gap: 12px;
      }

      .header-copy {
        min-width: 0;
      }

      .header-kicker {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        color: var(--pdc-muted);
      }

      .header-title {
        margin-top: 4px;
        font-size: 22px;
        line-height: 1.1;
        font-weight: 600;
        color: var(--pdc-text);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .header-subtitle {
        margin-top: 4px;
        font-size: 12px;
        color: var(--pdc-muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .frame-stage {
        ${sizeRule}
        position: relative;
        width: 100%;
        overflow: hidden;
        border-radius: var(--pdc-radius);
        background: #0b0f14;
        box-shadow:
          inset 0 0 0 1px var(--pdc-border),
          0 16px 36px rgba(0, 0, 0, 0.26);
      }

      .frame-backdrop,
      .frame-scrim,
      .frame-empty,
      .slide-track,
      .slide-frame,
      .frame-image {
        position: absolute;
        inset: 0;
      }

      .slide-track {
        overflow: hidden;
      }

      .slide-frame {
        background: transparent;
        will-change: transform;
        backface-visibility: hidden;
      }

      .slide-prev,
      .slide-next {
        z-index: 1;
      }

      .slide-current {
        z-index: 2;
      }

      .frame-backdrop {
        z-index: 0;
        background-position: center;
        background-size: cover;
        filter: blur(26px) saturate(0.9) brightness(0.7);
        transform: scale(1.08);
        opacity: 0.92;
      }

      .frame-scrim {
        background:
          linear-gradient(180deg, rgba(4, 6, 9, 0.08), rgba(4, 6, 9, 0.24)),
          linear-gradient(90deg, rgba(4, 6, 9, 0.14), transparent 18%, transparent 82%, rgba(4, 6, 9, 0.14));
        pointer-events: none;
      }

      .frame-image {
        width: 100%;
        height: 100%;
        object-fit: contain;
      }

      .frame-empty {
        display: none;
        place-items: center;
        text-align: center;
        padding: 24px;
        color: var(--pdc-muted);
        font-size: 14px;
        z-index: 4;
      }

      .control-bar {
        display: grid;
        grid-template-columns: auto auto auto minmax(0, 1fr);
        gap: 10px;
        align-items: center;
      }

      .icon-btn {
        width: 44px;
        height: 44px;
        border: 0;
        border-radius: 999px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        color: var(--pdc-text);
        background: rgba(255, 255, 255, 0.08);
        box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08);
        cursor: pointer;
        transition: background 160ms ease, transform 160ms ease;
        --mdc-icon-size: 22px;
      }

      .icon-btn:hover {
        background: rgba(255, 255, 255, 0.14);
      }

      .icon-btn:active {
        transform: scale(0.97);
      }

      .settings-btn {
        flex-shrink: 0;
      }

      .picker-select {
        min-width: 0;
        width: 100%;
        height: 44px;
        border: 0;
        border-radius: 999px;
        padding: 0 16px;
        font: inherit;
        color: var(--pdc-text);
        background: rgba(255, 255, 255, 0.08);
        box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08);
        appearance: none;
        color-scheme: dark;
        cursor: pointer;
      }

      .picker-select option {
        color: var(--pdc-text);
        background: rgb(22, 27, 34);
      }

      .picker-select:disabled {
        opacity: 0.45;
        cursor: default;
      }

      @media (max-width: 480px) {
        .card-shell {
          padding: 12px;
        }

        .header-title {
          font-size: 19px;
        }

        .control-bar {
          grid-template-columns: repeat(3, 44px);
        }

        .picker-select {
          grid-column: 1 / -1;
        }
      }
    `;
  }

  _escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str || "";
    return div.innerHTML;
  }

  getCardSize() {
    return 6;
  }

  static getStubConfig() {
    return { ...PDC_DEFAULTS };
  }
}

customElements.define("photo-display-card", PhotoDisplayCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "photo-display-card",
  name: "Photo Display Card",
  description: "Fixed-size photo frame card with filter title, swipe navigation, and popup settings.",
  preview: true,
});
