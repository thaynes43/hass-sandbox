/**
 * Countdown Config Card
 *
 * Configuration popup for managing countdowns — create, edit, delete,
 * generate AI background images, and adjust text overlay styling.
 *
 * Reads:  sensor.countdown_status
 * Writes: script.countdown_relay
 *
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App,
 *            Android/UniFi wall display.
 */

const CDC_DEFAULTS = {
  status_entity: "sensor.countdown_status",
  relay_script: "countdown_relay",
};

// ---------------------------------------------------------------------------
// HTML escaping utility
// ---------------------------------------------------------------------------

function cdcEscapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Card class
// ---------------------------------------------------------------------------

class CountdownConfigCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...CDC_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._touchActive = false;

    // Edit/create state
    this._editingId = null;         // ID of countdown being edited (null for new)
    this._isCreatingNew = false;    // True when adding a new countdown
    this._generatingIds = new Set(); // IDs with image generation in progress
    this._generatingAcked = new Set(); // IDs where sensor has confirmed generating=true
    this._confirmDeleteId = null;   // ID awaiting delete confirmation
    this._pendingEdits = {};        // Buffered form values during editing
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...CDC_DEFAULTS, ...config };
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;

    // Sync _generatingIds with sensor state.  Two-phase approach:
    // 1. When sensor shows generating=true, mark the id as "acknowledged"
    // 2. Only clear the id once it was acknowledged AND generating is now false
    // This prevents clearing the id before the sensor has caught up.
    if (this._generatingIds.size > 0) {
      const countdowns = this._getCountdowns();
      for (const id of [...this._generatingIds]) {
        const countdown = countdowns.find((c) => c.id === id);
        if (countdown && countdown.generating) {
          this._generatingAcked.add(id);
        }
        if (countdown && this._generatingAcked.has(id) && !countdown.generating) {
          this._generatingIds.delete(id);
          this._generatingAcked.delete(id);
        }
      }
    }

    const snap = this._snapshot();
    if (!firstSet && snap === this._lastSnapshot) return;
    this._lastSnapshot = snap;

    // Focus guard — skip re-render when any form input has focus
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
      this._bindEvents();
      this._update();
      return;
    }

    this._update();
  }

  getCardSize() {
    return 8;
  }

  static getStubConfig() {
    return { ...CDC_DEFAULTS };
  }

  // ---------------------------------------------------------------------------
  // Snapshot — re-render only when relevant state changes
  // ---------------------------------------------------------------------------

  _snapshot() {
    if (!this._hass) return null;
    const s = this._hass.states[this._config.status_entity];
    if (!s) return null;
    const a = s.attributes || {};
    return JSON.stringify({
      countdowns: (a.countdowns || []).map((c) => ({
        id: c.id,
        title: c.title,
        text: c.countdown_text,
        img: c.image_url,
        gen: c.generating,
        style: c.text_style,
      })),
      editingId: this._editingId,
      confirmDeleteId: this._confirmDeleteId,
      generatingIds: Array.from(this._generatingIds).sort().join(","),
    });
  }

  // ---------------------------------------------------------------------------
  // Relay call
  // ---------------------------------------------------------------------------

  _callRelay(command, data) {
    if (!this._hass) return;
    this._hass
      .callService("script", this._config.relay_script, {
        command,
        payload: JSON.stringify(data || {}),
      })
      .catch((err) => {
        console.warn("countdown-config-card: relay failed", command, err);
      });
  }

  // ---------------------------------------------------------------------------
  // Data helpers
  // ---------------------------------------------------------------------------

  _getCountdowns() {
    if (!this._hass) return [];
    const s = this._hass.states[this._config.status_entity];
    return s?.attributes?.countdowns || [];
  }

  _getCountdownById(id) {
    return this._getCountdowns().find((c) => c.id === id) || null;
  }

  _badgeClass(countdown) {
    const text = countdown.countdown_text || "";
    if (!text) return "cdc-badge-expired";
    if (text === "NOW") return "cdc-badge-now";
    return "cdc-badge-active";
  }

  _badgeLabel(countdown) {
    const text = countdown.countdown_text || "";
    if (!text) return "EXPIRED";
    if (text === "NOW") return "NOW";
    return "ACTIVE";
  }

  // ---------------------------------------------------------------------------
  // Preview helpers
  // ---------------------------------------------------------------------------

  _previewCountdownText(targetDatetime) {
    if (!targetDatetime) return "15D 5H 23M";
    try {
      const target = new Date(targetDatetime);
      const now = new Date();
      const diff = target - now;
      if (diff <= 0) {
        return diff > -86400000 ? "NOW" : "EXPIRED";
      }
      const d = Math.floor(diff / 86400000);
      const h = Math.floor((diff % 86400000) / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      return `${d}D ${h}H ${m}M`;
    } catch (_) {
      return "15D 5H 23M";
    }
  }

  _updatePreview() {
    const preview = this.shadowRoot?.querySelector(".cdc-preview");
    if (!preview) return;
    const pe = this._pendingEdits;
    const style = pe.text_style || {};

    const bgEl = preview.querySelector(".cdc-preview-bg");
    const titleEl = preview.querySelector(".cdc-preview-title");
    const subEl = preview.querySelector(".cdc-preview-subtitle");
    const countdownEl = preview.querySelector(".cdc-preview-countdown");

    // Background image — use current countdown image if editing existing
    if (bgEl && this._editingId && this._editingId !== "__new__") {
      const countdown = this._getCountdownById(this._editingId);
      const rawUrl = countdown?.image_url || "";
      const imgUrl = rawUrl ? rawUrl + (countdown?.image_version ? "?v=" + countdown.image_version : "") : "";
      if (imgUrl && bgEl.src !== imgUrl) {
        bgEl.src = imgUrl;
        bgEl.style.display = "";
      } else if (!imgUrl) {
        bgEl.style.display = "none";
      }
    } else if (bgEl) {
      bgEl.style.display = "none";
    }

    if (titleEl) {
      titleEl.textContent = pe.title || "Title";
      titleEl.style.fontSize = (style.title_size || 24) + "px";
    }
    if (subEl) subEl.textContent = pe.subtitle || "";
    if (countdownEl) {
      countdownEl.textContent = this._previewCountdownText(pe.target_datetime);
      countdownEl.style.fontSize = (style.font_size || 50) + "px";
      countdownEl.style.color = style.color || "#ffd24a";
      countdownEl.style.top = (style.position_y ?? 70) + "%";
      countdownEl.style.transform = "translateY(-50%)";
      if (style.text_shadow !== false) {
        countdownEl.style.textShadow =
          "-3px -3px 6px rgba(0,0,0,0.9), 3px -3px 6px rgba(0,0,0,0.9), " +
          "-3px 3px 6px rgba(0,0,0,0.9), 3px 3px 6px rgba(0,0,0,0.9), " +
          "0 0 10px rgba(255,210,74,0.9), 0 0 22px rgba(255,160,40,0.7), " +
          "0 0 32px rgba(255,120,20,0.5)";
      } else {
        countdownEl.style.textShadow = "none";
      }
    }

    // Update display values for sliders
    const sizeValEl = this.shadowRoot?.querySelector(".cdc-style-font-val");
    if (sizeValEl) sizeValEl.textContent = style.font_size || 50;
    const titleValEl = this.shadowRoot?.querySelector(".cdc-style-title-val");
    if (titleValEl) titleValEl.textContent = style.title_size || 24;
    const posValEl = this.shadowRoot?.querySelector(".cdc-style-pos-val");
    if (posValEl) posValEl.textContent = (style.position_y ?? 70) + "%";
  }

  // ---------------------------------------------------------------------------
  // Edit / create actions
  // ---------------------------------------------------------------------------

  _startEdit(id) {
    const countdown = this._getCountdownById(id);
    if (!countdown) return;
    this._editingId = id;
    this._isCreatingNew = false;
    this._pendingEdits = {
      title: countdown.title || "",
      subtitle: countdown.subtitle || "",
      target_datetime: countdown.target_datetime || "",
      image_prompt: countdown.image_prompt || "",
      text_style: {
        ...(countdown.text_style || {
          font_size: 50,
          color: "#ffd24a",
          position_y: 70,
          text_shadow: true,
        }),
      },
    };
    this._update();
  }

  _startNew() {
    this._editingId = "__new__";
    this._isCreatingNew = true;
    this._pendingEdits = {
      title: "",
      subtitle: "",
      target_datetime: "",
      image_prompt: "",
      text_style: { font_size: 50, title_size: 24, color: "#ffd24a", position_y: 70, text_shadow: true },
    };
    this._update();
  }

  _doSave(opts) {
    const pe = this._pendingEdits;
    if (!pe.title || !pe.target_datetime) return;

    const data = {
      title: pe.title,
      subtitle: pe.subtitle || "",
      target_datetime: pe.target_datetime,
      image_prompt: pe.image_prompt || "",
      text_style: pe.text_style,
    };

    // When auto_generate is set, the server will generate the image
    // immediately after creating the countdown (avoids needing the ID
    // on the client side for new countdowns).
    if (opts?.auto_generate && pe.image_prompt) {
      data.auto_generate = true;
    }

    if (!this._isCreatingNew && this._editingId !== "__new__") {
      data.id = this._editingId;
    }

    this._callRelay("save_countdown", data);
    this._editingId = null;
    this._isCreatingNew = false;
    this._pendingEdits = {};
    this._waitingForNewId = false;
    // Don't call _update() — sensor change will trigger re-render
  }

  _doDelete(id) {
    this._callRelay("delete_countdown", { id });
    this._confirmDeleteId = null;
    // Don't call _update() — sensor change will trigger re-render
  }

  _cancelEdit() {
    this._editingId = null;
    this._isCreatingNew = false;
    this._pendingEdits = {};
    this._update();
  }

  _doGenerate(id) {
    const targetId = id || this._editingId;
    const prompt = this._pendingEdits?.image_prompt;
    if (!prompt) return;

    // For new countdowns: save first with auto_generate flag so the server
    // generates the image immediately after creating the countdown.
    if (this._isCreatingNew) {
      this._updateGenButton(true);
      this._doSave({ auto_generate: true });
      return;
    }

    this._generatingIds.add(targetId);
    // Surgically update the button — do NOT call _update() here because
    // it replaces the edit form innerHTML, overwriting this DOM patch.
    this._updateGenButton(true);

    this._callRelay("generate_image", { id: targetId, prompt });

    // Auto-clear generating state after 120s timeout
    setTimeout(() => {
      if (this._generatingIds.has(targetId)) {
        this._generatingIds.delete(targetId);
        this._updateGenButton(false);
        this._update();
      }
    }, 120000);
  }

  _updateGenButton(isGenerating) {
    const btn = this.shadowRoot?.querySelector(".cdc-gen-btn");
    const status = this.shadowRoot?.querySelector(".cdc-gen-status");
    if (btn) {
      btn.disabled = isGenerating;
    }
    if (status) {
      status.style.visibility = isGenerating ? "visible" : "hidden";
    }
  }

  // ---------------------------------------------------------------------------
  // Action dispatch
  // ---------------------------------------------------------------------------

  _dispatchAction(el) {
    const action = el.dataset.action;
    const id = el.dataset.id;

    switch (action) {
      case "add-new":
        this._startNew();
        break;
      case "edit":
        this._startEdit(id);
        break;
      case "delete":
        this._confirmDeleteId = id;
        this._update();
        break;
      case "confirm-delete":
        this._doDelete(id);
        break;
      case "cancel-delete":
        this._confirmDeleteId = null;
        this._update();
        break;
      case "generate":
        this._doGenerate(id);
        break;
      case "save":
        this._doSave();
        break;
      case "cancel-edit":
        this._cancelEdit();
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------------------
  // Build DOM once
  // ---------------------------------------------------------------------------

  _buildDom() {
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="cdc-card">
        <div class="cdc-header">
          <h2>Countdowns</h2>
          <button class="cdc-add-btn" data-action="add-new">+ Add</button>
        </div>
        <div class="cdc-list"></div>
        <div class="cdc-edit-container"></div>
        <div class="cdc-global-settings"></div>
      </div>
    `;

    this._els = {
      list: this.shadowRoot.querySelector(".cdc-list"),
      editContainer: this.shadowRoot.querySelector(".cdc-edit-container"),
      globalSettings: this.shadowRoot.querySelector(".cdc-global-settings"),
    };

    this._domBuilt = true;
  }

  // ---------------------------------------------------------------------------
  // Update — render list and edit form into existing DOM slots
  // ---------------------------------------------------------------------------

  _update() {
    if (!this._els) return;

    const countdowns = this._getCountdowns();

    // Render the list
    this._els.list.innerHTML = this._renderList(countdowns);

    // Render the edit form (or clear it)
    if (this._editingId !== null) {
      this._els.editContainer.innerHTML = this._renderEditForm();
      this._attachFormListeners();
      this._updatePreview();
    } else {
      this._els.editContainer.innerHTML = "";
    }

    // Render global settings (rotation interval)
    this._els.globalSettings.innerHTML = this._renderGlobalSettings();
    this._attachGlobalSettingsListeners();
  }

  // ---------------------------------------------------------------------------
  // List rendering
  // ---------------------------------------------------------------------------

  _renderList(countdowns) {
    if (!countdowns.length && this._editingId === "__new__") {
      return ""; // Nothing to show in the list when creating first item
    }

    if (!countdowns.length) {
      return `<div class="cdc-empty">No countdowns yet. Tap "+ Add" to create one.</div>`;
    }

    return countdowns
      .map((c) => this._renderListItem(c))
      .join("");
  }

  _renderListItem(countdown) {
    const id = cdcEscapeHtml(String(countdown.id || ""));
    const title = cdcEscapeHtml(countdown.title || "Untitled");
    const subtitle = cdcEscapeHtml(countdown.subtitle || "");
    const countdownText = cdcEscapeHtml(countdown.countdown_text || "");
    const rawImgUrl = countdown.image_url || "";
    const imgUrl = cdcEscapeHtml(
      rawImgUrl ? rawImgUrl + (countdown.image_version ? "?v=" + countdown.image_version : "") : ""
    );
    const badgeClass = this._badgeClass(countdown);
    const badgeLabel = this._badgeLabel(countdown);
    const isGenerating = this._generatingIds.has(countdown.id);

    // Delete confirmation row replaces the item row
    if (this._confirmDeleteId === id) {
      return `
        <div class="cdc-delete-confirm">
          <span>Delete "${title}"?</span>
          <div class="cdc-confirm-btns">
            <button class="cdc-confirm-yes" data-action="confirm-delete" data-id="${id}">Delete</button>
            <button class="cdc-confirm-no" data-action="cancel-delete" data-id="${id}">Cancel</button>
          </div>
        </div>
      `;
    }

    const thumbHtml = imgUrl
      ? `<img class="cdc-thumb" src="${imgUrl}" alt="${title}" />`
      : `<div class="cdc-thumb cdc-thumb-placeholder"></div>`;

    const genSpinner = isGenerating
      ? `<div class="cdc-gen-spinner" title="Generating image…"></div>`
      : "";

    const subText = subtitle
      ? `${subtitle}${countdownText ? " · " + countdownText : ""}`
      : countdownText;

    return `
      <div class="cdc-item">
        <div class="cdc-thumb-wrap">
          ${thumbHtml}
          ${genSpinner}
        </div>
        <div class="cdc-item-info">
          <div class="cdc-item-title">${title}</div>
          ${subText ? `<div class="cdc-item-sub">${cdcEscapeHtml(subText)}</div>` : ""}
        </div>
        <span class="cdc-item-badge ${badgeClass}">${badgeLabel}</span>
        <div class="cdc-item-actions">
          <button class="cdc-icon-btn" data-action="edit" data-id="${id}" title="Edit">&#9998;</button>
          <button class="cdc-icon-btn" data-action="delete" data-id="${id}" title="Delete">&#128465;</button>
        </div>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Edit form rendering
  // ---------------------------------------------------------------------------

  _renderEditForm() {
    const pe = this._pendingEdits;
    const style = pe.text_style || {};
    const isNew = this._isCreatingNew;
    const heading = isNew ? "New Countdown" : "Edit Countdown";
    const isGenerating = !isNew && this._generatingIds.has(this._editingId);

    const titleVal = cdcEscapeHtml(pe.title || "");
    const subtitleVal = cdcEscapeHtml(pe.subtitle || "");
    const promptVal = cdcEscapeHtml(pe.image_prompt || "");

    // target_datetime: format for datetime-local input (YYYY-MM-DDTHH:MM)
    let dtVal = "";
    if (pe.target_datetime) {
      try {
        const d = new Date(pe.target_datetime);
        if (!isNaN(d)) {
          // Format as local datetime-local value
          const pad = (n) => String(n).padStart(2, "0");
          dtVal =
            d.getFullYear() +
            "-" +
            pad(d.getMonth() + 1) +
            "-" +
            pad(d.getDate()) +
            "T" +
            pad(d.getHours()) +
            ":" +
            pad(d.getMinutes());
        }
      } catch (_) {
        dtVal = pe.target_datetime;
      }
    }

    const fontSize = style.font_size ?? 50;
    const titleSize = style.title_size ?? 24;
    const color = style.color || "#ffd24a";
    const posY = style.position_y ?? 70;
    const shadow = style.text_shadow !== false;

    // Current image for preview bg
    let bgStyle = "";
    if (!isNew && this._editingId) {
      const countdown = this._getCountdownById(this._editingId);
      if (countdown?.image_url) {
        const vUrl = countdown.image_url + (countdown.image_version ? "?v=" + countdown.image_version : "");
        bgStyle = ` style="display:block;" src="${cdcEscapeHtml(vUrl)}"`;
      }
    }

    // genBtnContent removed — button text is static, status shown via .cdc-gen-status

    return `
      <div class="cdc-edit">
        <h3>${cdcEscapeHtml(heading)}</h3>

        <div class="cdc-field">
          <label>Title</label>
          <input type="text" data-field="title" value="${titleVal}" placeholder="e.g. Summer Vacation" />
        </div>

        <div class="cdc-field">
          <label>Subtitle</label>
          <input type="text" data-field="subtitle" value="${subtitleVal}" placeholder="Optional subtitle" />
        </div>

        <div class="cdc-field">
          <label>Target Date &amp; Time</label>
          <input type="datetime-local" data-field="target_datetime" value="${dtVal}" />
        </div>

        <div class="cdc-field">
          <label>Image Prompt</label>
          <textarea data-field="image_prompt" placeholder="Describe the background image…">${promptVal}</textarea>
        </div>

        <div class="cdc-field">
          <label>Text Style</label>
          <div class="cdc-style-row">
            <div class="cdc-style-item">
              <label>Countdown Size</label>
              <input type="range" min="20" max="80" value="${fontSize}" data-field="style.font_size" />
              <span class="cdc-style-font-val">${fontSize}</span>
            </div>
            <div class="cdc-style-item">
              <label>Title Size</label>
              <input type="range" min="10" max="40" value="${titleSize}" data-field="style.title_size" />
              <span class="cdc-style-title-val">${titleSize}</span>
            </div>
            <div class="cdc-style-item">
              <label>Color</label>
              <input type="color" value="${color}" data-field="style.color" />
            </div>
            <div class="cdc-style-item">
              <label>Position Y</label>
              <input type="range" min="5" max="90" value="${posY}" data-field="style.position_y" />
              <span class="cdc-style-pos-val">${posY}%</span>
            </div>
            <div class="cdc-style-item">
              <label>Shadow</label>
              <input type="checkbox" data-field="style.text_shadow" ${shadow ? "checked" : ""} />
            </div>
          </div>
        </div>

        <div class="cdc-preview">
          <img class="cdc-preview-bg"${bgStyle ? bgStyle : ' style="display:none;" src=""'} alt="" />
          <div class="cdc-preview-overlay">
            <div class="cdc-preview-title" style="font-size:${titleSize}px;">${cdcEscapeHtml(pe.title || "Title")}</div>
            <div class="cdc-preview-subtitle">${cdcEscapeHtml(pe.subtitle || "")}</div>
            <div class="cdc-preview-countdown" style="font-size:${fontSize}px; color:${color}; top:${posY}%; transform:translateY(-50%);">
              ${cdcEscapeHtml(this._previewCountdownText(pe.target_datetime))}
            </div>
          </div>
        </div>

        <div class="cdc-gen-row">
          <button class="cdc-gen-btn" data-action="generate" data-id="${cdcEscapeHtml(this._editingId || "")}" ${isGenerating ? "disabled" : ""}>
            &#9654; Generate Image
          </button>
          <span class="cdc-gen-status" style="visibility:${isGenerating ? "visible" : "hidden"};">Please wait\u2026</span>
          ${isNew ? `<span class="cdc-gen-note">Will save and generate automatically</span>` : ""}
        </div>

        <div class="cdc-form-actions">
          <button class="cdc-cancel-btn" data-action="cancel-edit">Cancel</button>
          <button class="cdc-save-btn" data-action="save">Save</button>
        </div>
      </div>
    `;
  }

  // ---------------------------------------------------------------------------
  // Global settings (rotation interval)
  // ---------------------------------------------------------------------------

  _renderGlobalSettings() {
    const s = this._hass?.states[this._config.status_entity];
    const interval = s?.attributes?.rotation_interval_s ?? 15;
    return `
      <div class="cdc-global">
        <div class="cdc-global-field">
          <label>Slide interval: ${interval}s</label>
          <input type="range" min="1" max="120" step="1" value="${interval}" class="cdc-rotation-slider" />
        </div>
      </div>
    `;
  }

  _attachGlobalSettingsListeners() {
    const slider = this._els.globalSettings?.querySelector(".cdc-rotation-slider");
    if (!slider) return;
    slider.addEventListener("input", (e) => {
      const val = parseInt(e.target.value);
      if (isNaN(val) || val < 1) return;
      const label = this._els.globalSettings.querySelector(".cdc-global-field label");
      if (label) label.textContent = `Slide interval: ${val}s`;
    });
    slider.addEventListener("change", (e) => {
      const val = parseInt(e.target.value);
      if (isNaN(val) || val < 1) return;
      this._callRelay("set_rotation_interval", { seconds: val });
    });
  }

  // ---------------------------------------------------------------------------
  // Form input listeners — attached after each edit form render
  // ---------------------------------------------------------------------------

  _attachFormListeners() {
    const editContainer = this._els?.editContainer;
    if (!editContainer) return;

    editContainer.querySelectorAll("[data-field]").forEach((input) => {
      const handler = (e) => {
        const field = e.target.dataset.field;
        if (!this._pendingEdits.text_style) {
          this._pendingEdits.text_style = {};
        }

        if (field.startsWith("style.")) {
          const key = field.split(".")[1];
          let val;
          if (e.target.type === "checkbox") {
            val = e.target.checked;
          } else if (e.target.type === "range" || e.target.type === "number") {
            val = parseInt(e.target.value, 10);
          } else {
            val = e.target.value;
          }
          this._pendingEdits.text_style[key] = val;
        } else {
          this._pendingEdits[field] = e.target.value;
        }

        // Update preview without re-rendering the full card
        this._updatePreview();
      };
      // Listen to both input and change — datetime-local pickers fire change
      // but not input on some browsers when using the picker UI.
      input.addEventListener("input", handler);
      input.addEventListener("change", handler);
    });
  }

  // ---------------------------------------------------------------------------
  // Touch / click deduplication
  // ---------------------------------------------------------------------------

  _bindEvents() {
    const root = this.shadowRoot;

    const findActionTarget = (evt) => {
      for (const node of evt.composedPath()) {
        if (node instanceof Element && node.dataset?.action) return node;
      }
      return null;
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
        const el = findActionTarget(e);
        if (touchCancelled || !el) {
          this._touchActive = false;
          return;
        }

        // CRITICAL: never preventDefault on native form elements — Android
        // webviews will not open keyboards, dropdowns, or date pickers.
        const tag = el.tagName?.toLowerCase();
        const nativeEl =
          tag === "input" || tag === "select" || tag === "textarea";
        if (!nativeEl && e.cancelable) e.preventDefault();

        this._touchActive = true;
        this._dispatchAction(el);
        setTimeout(() => {
          this._touchActive = false;
        }, 400);
      },
      { passive: false }
    );

    root.addEventListener("click", (e) => {
      if (this._touchActive) return;
      const el = findActionTarget(e);
      if (el) this._dispatchAction(el);
    });
  }

  // ---------------------------------------------------------------------------
  // Styles
  // ---------------------------------------------------------------------------

  _styles() {
    return `
      :host {
        display: block;
        touch-action: pan-y;
      }

      .cdc-card {
        background: var(--card-background-color, #1c1c1c);
        border-radius: 12px;
        padding: 16px;
        color: var(--primary-text-color, #fff);
        font-family: var(--ha-card-font-family, inherit);
      }

      .cdc-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 16px;
      }

      .cdc-header h2 {
        margin: 0;
        font-size: 20px;
        font-weight: 700;
      }

      .cdc-add-btn {
        background: var(--primary-color, #03a9f4);
        color: #fff;
        border: none;
        border-radius: 8px;
        padding: 8px 16px;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
      }

      .cdc-add-btn:active {
        filter: brightness(0.88);
      }

      .cdc-list {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .cdc-empty {
        text-align: center;
        color: var(--secondary-text-color, #888);
        font-size: 14px;
        padding: 24px 8px;
        font-style: italic;
      }

      .cdc-item {
        display: flex;
        gap: 12px;
        align-items: center;
        background: rgba(255,255,255,0.05);
        border-radius: 8px;
        padding: 10px;
        cursor: default;
      }

      .cdc-thumb-wrap {
        position: relative;
        width: 80px;
        height: 45px;
        flex-shrink: 0;
      }

      .cdc-thumb {
        width: 80px;
        height: 45px;
        border-radius: 6px;
        object-fit: cover;
        display: block;
      }

      .cdc-thumb-placeholder {
        background: linear-gradient(135deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
        border-radius: 6px;
        border: 1px solid rgba(255,255,255,0.08);
      }

      .cdc-gen-spinner {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(0,0,0,0.55);
        border-radius: 6px;
      }

      .cdc-gen-spinner::after {
        content: "";
        width: 18px;
        height: 18px;
        border: 2px solid rgba(255,255,255,0.3);
        border-top-color: #fff;
        border-radius: 50%;
        animation: cdc-spin 0.8s linear infinite;
      }

      .cdc-item-info {
        flex: 1;
        min-width: 0;
      }

      .cdc-item-title {
        font-weight: 700;
        font-size: 14px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .cdc-item-sub {
        font-size: 12px;
        color: var(--secondary-text-color, #888);
        margin-top: 2px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .cdc-item-badge {
        font-size: 11px;
        font-weight: 700;
        padding: 3px 8px;
        border-radius: 10px;
        text-transform: uppercase;
        flex-shrink: 0;
      }

      .cdc-badge-active { background: #4caf50; color: #fff; }
      .cdc-badge-now { background: #ff9800; color: #fff; animation: cdc-pulse 1.5s infinite; }
      .cdc-badge-expired { background: #666; color: #aaa; }

      @keyframes cdc-pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.6; }
      }

      @keyframes cdc-spin {
        to { transform: rotate(360deg); }
      }

      .cdc-item-actions {
        display: flex;
        gap: 6px;
        flex-shrink: 0;
      }

      .cdc-icon-btn {
        background: none;
        border: 1px solid rgba(255,255,255,0.2);
        color: var(--primary-text-color, #fff);
        border-radius: 6px;
        width: 32px;
        height: 32px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 16px;
        flex-shrink: 0;
      }

      .cdc-icon-btn:hover { background: rgba(255,255,255,0.1); }
      .cdc-icon-btn:active { background: rgba(255,255,255,0.18); }

      .cdc-delete-confirm {
        background: rgba(244,67,54,0.15);
        border-radius: 8px;
        padding: 12px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }

      .cdc-delete-confirm span {
        color: #f44336;
        font-weight: 600;
        font-size: 14px;
        flex: 1;
        min-width: 0;
      }

      .cdc-confirm-btns {
        display: flex;
        gap: 8px;
        flex-shrink: 0;
      }

      .cdc-confirm-yes {
        background: #f44336;
        color: #fff;
        border: none;
        border-radius: 6px;
        padding: 6px 14px;
        cursor: pointer;
        font-weight: 600;
        font-size: 13px;
      }

      .cdc-confirm-no {
        background: rgba(255,255,255,0.1);
        color: #fff;
        border: none;
        border-radius: 6px;
        padding: 6px 14px;
        cursor: pointer;
        font-size: 13px;
      }

      /* ---- Edit form ---- */

      .cdc-edit {
        background: rgba(255,255,255,0.05);
        border-radius: 8px;
        padding: 16px;
        margin-top: 12px;
      }

      .cdc-edit h3 {
        margin: 0 0 12px;
        font-size: 16px;
        font-weight: 700;
      }

      .cdc-field {
        margin-bottom: 12px;
      }

      .cdc-field > label {
        display: block;
        font-size: 12px;
        font-weight: 600;
        color: var(--secondary-text-color, #888);
        margin-bottom: 4px;
        text-transform: uppercase;
        letter-spacing: 1px;
      }

      .cdc-field input[type="text"],
      .cdc-field input[type="datetime-local"],
      .cdc-field textarea {
        width: 100%;
        box-sizing: border-box;
        background: rgba(255,255,255,0.08);
        border: 1px solid rgba(255,255,255,0.15);
        border-radius: 6px;
        color: var(--primary-text-color, #fff);
        padding: 8px 10px;
        font-size: 14px;
        font-family: inherit;
      }

      .cdc-field textarea {
        min-height: 150px;
        resize: vertical;
      }

      .cdc-field input[type="text"]:focus,
      .cdc-field input[type="datetime-local"]:focus,
      .cdc-field textarea:focus {
        outline: none;
        border-color: var(--primary-color, #03a9f4);
      }

      /* ---- Style controls ---- */

      .cdc-style-row {
        display: flex;
        gap: 16px;
        align-items: flex-start;
        flex-wrap: wrap;
      }

      .cdc-style-item {
        display: flex;
        flex-direction: column;
        gap: 4px;
        align-items: flex-start;
      }

      .cdc-style-item > label {
        font-size: 11px;
        color: var(--secondary-text-color, #888);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        font-weight: 600;
      }

      .cdc-style-item input[type="range"] {
        width: 100px;
        cursor: pointer;
      }

      .cdc-style-item input[type="color"] {
        width: 40px;
        height: 30px;
        border: none;
        background: none;
        cursor: pointer;
        padding: 0;
      }

      .cdc-style-item input[type="checkbox"] {
        width: 18px;
        height: 18px;
        cursor: pointer;
        margin: 4px 0 0;
      }

      .cdc-style-font-val,
      .cdc-style-pos-val {
        font-size: 12px;
        color: var(--secondary-text-color, #888);
      }

      /* ---- Preview ---- */

      .cdc-preview {
        position: relative;
        width: 100%;
        max-width: 480px;
        aspect-ratio: 16 / 9;
        overflow: hidden;
        border-radius: 8px;
        margin: 12px auto;
        background: #111;
      }

      .cdc-preview-bg {
        width: 100%;
        height: 100%;
        object-fit: cover;
        position: absolute;
        inset: 0;
      }

      .cdc-preview-overlay {
        position: absolute;
        inset: 0;
        display: flex;
        flex-direction: column;
        align-items: center;
        pointer-events: none;
      }

      .cdc-preview-title {
        font-size: 16px;
        font-weight: 800;
        color: #fff;
        text-transform: uppercase;
        letter-spacing: 2px;
        text-shadow: 2px 2px 8px rgba(0,0,0,0.8);
        text-align: center;
        margin-top: 12px;
        padding: 0 8px;
      }

      .cdc-preview-subtitle {
        font-size: 12px;
        font-weight: 600;
        color: rgba(255,255,255,0.85);
        text-shadow: 1px 1px 4px rgba(0,0,0,0.8);
        margin-top: 2px;
        text-align: center;
      }

      .cdc-preview-countdown {
        font-weight: 1000;
        text-transform: uppercase;
        letter-spacing: 4px;
        position: absolute;
        width: 100%;
        text-align: center;
      }

      /* ---- Generate button ---- */

      .cdc-gen-row {
        display: flex;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
        margin-bottom: 4px;
      }

      .cdc-gen-status {
        font-size: 14px;
        font-weight: 600;
        color: #ff9800;
        margin-left: 12px;
      }

      .cdc-gen-note {
        font-size: 12px;
        color: var(--secondary-text-color, #888);
        font-style: italic;
      }

      .cdc-gen-btn {
        background: var(--primary-color, #03a9f4);
        color: #fff;
        border: none;
        border-radius: 8px;
        padding: 10px 20px;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .cdc-gen-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .cdc-gen-btn:not(:disabled):active {
        filter: brightness(0.88);
      }

      .cdc-gen-btn .spinner {
        width: 16px;
        height: 16px;
        border: 2px solid rgba(255,255,255,0.3);
        border-top-color: #fff;
        border-radius: 50%;
        animation: cdc-spin 0.8s linear infinite;
        display: inline-block;
        flex-shrink: 0;
      }

      /* ---- Form action buttons ---- */

      /* ---- Global settings ---- */

      .cdc-global {
        margin-top: 16px;
        padding-top: 16px;
        border-top: 1px solid rgba(255,255,255,0.1);
      }

      .cdc-global-field {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .cdc-global-field label {
        font-size: 12px;
        font-weight: 600;
        color: var(--secondary-text-color, #888);
        text-transform: uppercase;
        letter-spacing: 1px;
      }

      .cdc-global-field input[type="range"] {
        width: 100%;
      }

      .cdc-form-actions {
        display: flex;
        gap: 8px;
        margin-top: 16px;
        justify-content: flex-end;
      }

      .cdc-save-btn {
        background: #4caf50;
        color: #fff;
        border: none;
        border-radius: 8px;
        padding: 10px 24px;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
      }

      .cdc-save-btn:active {
        filter: brightness(0.88);
      }

      .cdc-cancel-btn {
        background: rgba(255,255,255,0.1);
        color: #fff;
        border: none;
        border-radius: 8px;
        padding: 10px 24px;
        font-size: 14px;
        cursor: pointer;
      }

      .cdc-cancel-btn:active {
        background: rgba(255,255,255,0.18);
      }
    `;
  }
}

customElements.define("countdown-config-card", CountdownConfigCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "countdown-config-card",
  name: "Countdown Config Card",
  description: "Configuration popup for managing countdowns",
});
