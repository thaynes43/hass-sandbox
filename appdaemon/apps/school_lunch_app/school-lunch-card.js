/**
 * School Lunch Card
 *
 * At-a-glance card for the wall-display dashboard. Shows tomorrow's (or
 * Monday's) lunch entrees for the selected schools.
 *
 * Reads:
 *   sensor.school_lunch_menu          — schools[], last_updated
 *   input_text.school_lunch_selected_schools — JSON array of selected school names
 *
 * Tapping the card navigates to config.navigation_path (default "#school-lunch-popup").
 *
 * Optional `height` (px): pins the card to a fixed height so a long menu can
 * never push the dashboard's bottom button row off the wall display; the menu
 * area then scrolls by touch, with a fade + chevron hint while more is below.
 *
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App, Android/UniFi wall display.
 */

const SLC_DEFAULTS = {
  status_entity: "sensor.school_lunch_menu",
  selection_entity: "input_text.school_lunch_selected_schools",
  navigation_path: "#school-lunch-popup",
};

/**
 * Split a day's items into options and includes using the ``role`` field
 * set by the AppDaemon app. The app classifies items automatically:
 * items appearing on 75%+ of menu days → "includes", everything else → "option".
 *
 * Returns { options: string[], includes: string[] }
 */
function parseLunchItems(items) {
  const options = [];
  const includes = [];
  for (const item of items) {
    const name = String(item.name || "").trim();
    if (!name) continue;
    if (item.role === "includes") {
      includes.push(name);
    } else {
      options.push(name);
    }
  }
  return { options, includes };
}

class SchoolLunchCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...SLC_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._touchActive = false;
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...SLC_DEFAULTS, ...config };
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;

    const snap = this._snapshot();
    if (!firstSet && snap === this._lastSnapshot) return;
    this._lastSnapshot = snap;

    // Focus guard: skip re-render when an input has focus (no inputs in this card,
    // but included for consistency with the project pattern).
    const active = this.shadowRoot?.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
      return;
    }

    if (!this._domBuilt) {
      this._buildDom();
      this._update();
      return;
    }

    this._update();
  }

  getCardSize() {
    return 3;
  }

  static getStubConfig() {
    return { ...SLC_DEFAULTS };
  }

  // ---------------------------------------------------------------------------
  // Snapshot — only re-render when relevant state changes
  // ---------------------------------------------------------------------------

  _snapshot() {
    if (!this._hass) return null;
    const status = this._hass.states[this._config.status_entity];
    const selection = this._hass.states[this._config.selection_entity];
    // Include the target date so the card re-renders on date/cutoff transitions
    const { targetDay, targetMonth, targetYear, headerLabel } = this._targetDate();
    return JSON.stringify({
      status: status
        ? {
            s: status.state,
            schools: status.attributes?.schools,
            last_updated: status.attributes?.last_updated,
          }
        : null,
      selection: selection ? { s: selection.state } : null,
      target: { targetDay, targetMonth, targetYear, headerLabel },
    });
  }

  // ---------------------------------------------------------------------------
  // Data helpers
  // ---------------------------------------------------------------------------

  /** Return the sensor attribute value, or fallback if missing. */
  _sensorAttr(entityId, attr, fallback) {
    const s = this._hass?.states?.[entityId];
    const v = s?.attributes?.[attr];
    return v !== undefined ? v : fallback;
  }

  /**
   * Determine the "target day" number for display.
   *
   * Rules:
   *   - Friday after 15:00, Saturday, Sunday → show Monday (next week)
   *   - Otherwise → show tomorrow
   *
   * The sensor attribute ``show_tomorrow_after`` (HH:MM:SS) controls when
   * we flip from showing today's lunch to tomorrow's. Before that time we
   * show today (or Monday on weekends). After that time we show tomorrow
   * (or Monday on Fri evening / weekends).
   *
   * Returns { targetDay, targetMonth, targetYear, headerLabel }
   */
  _targetDate() {
    const now = new Date();
    const dow = now.getDay(); // 0=Sun, 1=Mon, … 6=Sat

    // Parse show_tomorrow_after from sensor (default 15:00:00)
    const cutoffStr = String(
      this._sensorAttr(this._config.status_entity, "show_tomorrow_after", "15:00:00") || "15:00:00"
    );
    const [cutH, cutM, cutS] = cutoffStr.split(":").map(Number);
    const cutoff = new Date(now);
    cutoff.setHours(cutH || 15, cutM || 0, cutS || 0, 0);
    const pastCutoff = now >= cutoff;

    // Weekend → always show Monday
    if (dow === 0 || dow === 6) {
      const daysUntilMonday = dow === 0 ? 1 : 2;
      const monday = new Date(now);
      monday.setDate(now.getDate() + daysUntilMonday);
      return {
        targetDay: monday.getDate(),
        targetMonth: monday.getMonth() + 1,
        targetYear: monday.getFullYear(),
        headerLabel: "Monday's Lunch",
      };
    }

    if (pastCutoff) {
      // After cutoff: show tomorrow (or Monday if Friday)
      if (dow === 5) {
        const monday = new Date(now);
        monday.setDate(now.getDate() + 3);
        return {
          targetDay: monday.getDate(),
          targetMonth: monday.getMonth() + 1,
          targetYear: monday.getFullYear(),
          headerLabel: "Monday's Lunch",
        };
      }
      const tomorrow = new Date(now);
      tomorrow.setDate(now.getDate() + 1);
      return {
        targetDay: tomorrow.getDate(),
        targetMonth: tomorrow.getMonth() + 1,
        targetYear: tomorrow.getFullYear(),
        headerLabel: "Tomorrow's Lunch",
      };
    }

    // Before cutoff: show today
    return {
      targetDay: now.getDate(),
      targetMonth: now.getMonth() + 1,
      targetYear: now.getFullYear(),
      headerLabel: "Today's Lunch",
    };
  }

  /**
   * Return entree items for a given school and target date.
   * Filters: category contains "Entree" (case-insensitive) AND is_ancillary === false.
   */
  _getDayInfo(school, targetDay, targetMonth, targetYear) {
    // Each day carries its own month/year so cross-month lookups work
    // (e.g. last week of March showing April days).
    const dayData = (school.days || []).find(
      (d) => d.day === targetDay && d.month === targetMonth && d.year === targetYear
    );
    if (!dayData) return null;

    return {
      ...parseLunchItems(dayData.items || []),
      notice: dayData.notice || "",
    };
  }

  /** Render numbered options + includes line. */
  _renderOptions(options, includes) {
    let html = `<div class="options-list">`;
    options.forEach((opt, i) => {
      html += `<div class="option-row">
        <span class="option-num">Option ${i + 1}:</span>
        <span class="option-text">${this._escapeHtml(opt)}</span>
      </div>`;
    });
    html += `</div>`;
    if (includes.length > 0) {
      html += `<div class="includes-line">Includes: ${this._escapeHtml(includes.join(" & "))}</div>`;
    }
    return html;
  }

  /** Parse the selected-schools JSON from input_text. Returns an array of names. */
  _selectedSchools() {
    const s = this._hass?.states?.[this._config.selection_entity];
    const raw = s?.state || "[]";
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (_err) {
      return [];
    }
  }

  /** Escape a string for safe HTML insertion. */
  _escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str || "";
    return div.innerHTML;
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
      <ha-card data-action="open">
        <div class="card-shell">
          <div class="card-header">
            <div class="header-copy">
              <div class="header-kicker">School Lunch</div>
              <div class="header-title"></div>
            </div>
            <ha-icon class="header-icon" icon="mdi:silverware-fork-knife"></ha-icon>
          </div>
          <div class="stage-wrap">
            <div class="content-stage"></div>
            <div class="scroll-fade" aria-hidden="true"><ha-icon icon="mdi:chevron-down"></ha-icon></div>
          </div>
        </div>
      </ha-card>
    `;

    const root = this.shadowRoot;
    this._els = {
      card: root.querySelector("ha-card"),
      headerTitle: root.querySelector(".header-title"),
      contentStage: root.querySelector(".content-stage"),
      scrollFade: root.querySelector(".scroll-fade"),
    };

    // Fixed height (wall display): the card stops growing with the menu and
    // the menu area scrolls instead. Native touch scrolling is left alone —
    // the tap handler already ignores a gesture that moved.
    const height = Number(this._config.height);
    if (Number.isFinite(height) && height > 0) {
      this._els.card.classList.add("fixed-height");
      this._els.card.style.setProperty("--slc-height", `${Math.round(height)}px`);
      this._els.contentStage.addEventListener(
        "scroll",
        () => this._updateScrollFade(),
        { passive: true }
      );
    }

    this._bindEvents();
    this._domBuilt = true;
  }

  /** Show the bottom fade + chevron only while more menu is hidden below. */
  _updateScrollFade() {
    const stage = this._els?.contentStage;
    const fade = this._els?.scrollFade;
    if (!stage || !fade) return;
    const more = stage.scrollHeight - stage.scrollTop - stage.clientHeight > 4;
    fade.classList.toggle("visible", more);
  }

  _update() {
    if (!this._els) return;

    const schools = this._sensorAttr(this._config.status_entity, "schools", []);
    const selectedNames = this._selectedSchools();
    const { targetDay, targetMonth, targetYear, headerLabel } = this._targetDate();

    this._els.headerTitle.textContent = headerLabel;

    // Filter to only selected schools (preserve order from sensor)
    const selectedSchools = (schools || []).filter((s) =>
      selectedNames.includes(s.name)
    );

    // Build the per-school blocks
    const schoolBlocks = selectedSchools.map((school) => {
      const dayInfo = this._getDayInfo(school, targetDay, targetMonth, targetYear);

      if (dayInfo === null) {
        return `
          <div class="school-block">
            <div class="school-name">${this._escapeHtml(school.name)}</div>
            <div class="no-menu">No menu available</div>
          </div>`;
      }

      // Notice day (e.g. "EARLY RELEASE GRAB AND GO..." or "NO SCHOOL...")
      if (dayInfo.notice) {
        let bodyHtml = `<div class="day-notice">${this._escapeHtml(dayInfo.notice)}</div>`;
        if (dayInfo.options.length > 0) {
          bodyHtml += this._renderOptions(dayInfo.options, dayInfo.includes);
        }
        return `
          <div class="school-block">
            <div class="school-name">${this._escapeHtml(school.name)}</div>
            ${bodyHtml}
          </div>`;
      }

      if (dayInfo.options.length === 0) {
        return `
          <div class="school-block">
            <div class="school-name">${this._escapeHtml(school.name)}</div>
            <div class="no-menu">No menu listed</div>
          </div>`;
      }

      return `
        <div class="school-block">
          <div class="school-name">${this._escapeHtml(school.name)}</div>
          ${this._renderOptions(dayInfo.options, dayInfo.includes)}
        </div>`;
    });

    const bodyHtml =
      schoolBlocks.length > 0
        ? schoolBlocks.join("")
        : `<div class="no-menu">No schools selected</div>`;

    this._els.contentStage.innerHTML = bodyHtml;
    requestAnimationFrame(() => this._updateScrollFade());
  }

  // ---------------------------------------------------------------------------
  // Touch / click deduplication (delegated, single listener, bound once)
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

    // Cancel on scroll/move
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

        // CRITICAL: never preventDefault on native form elements — Android
        // webviews will not open keyboards/dropdowns.
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

    // Desktop click — gate on touchActive to prevent double-fire on touch devices
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
        --slc-radius: 18px;
        --slc-radius-sm: 12px;
        --slc-border: color-mix(in srgb, var(--primary-text-color, #fff) 12%, transparent);
        --slc-text: var(--primary-text-color, #f5f7fa);
        --slc-muted: color-mix(in srgb, var(--secondary-text-color, #b6beca) 88%, white 12%);
        --slc-accent: var(--primary-color, #66b3ff);
        display: block;
      }

      ha-card {
        cursor: pointer;
        overflow: hidden;
        background:
          radial-gradient(circle at top, rgba(115, 160, 255, 0.16), transparent 34%),
          linear-gradient(180deg, rgba(12, 15, 20, 0.98), rgba(19, 24, 31, 0.98));
        transition: filter 120ms ease;
      }

      ha-card:active {
        filter: brightness(0.92);
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
        color: var(--slc-muted);
      }

      .header-title {
        margin-top: 4px;
        font-size: 20px;
        line-height: 1.1;
        font-weight: 600;
        color: var(--slc-text);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .header-icon {
        --mdc-icon-size: 22px;
        color: var(--slc-accent);
        flex-shrink: 0;
        opacity: 0.7;
        margin-top: 2px;
      }

      .content-stage {
        border-radius: var(--slc-radius-sm);
        background: rgba(255, 255, 255, 0.04);
        box-shadow: inset 0 0 0 1px var(--slc-border);
        padding: 12px 14px;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .school-block {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .school-name {
        font-size: 13px;
        font-weight: 700;
        color: var(--slc-accent);
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }

      .options-list {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .option-row {
        font-size: 13px;
        color: var(--slc-text);
        line-height: 1.4;
      }

      .option-num {
        font-weight: 600;
        color: var(--slc-accent);
        margin-right: 4px;
      }

      .includes-line {
        margin-top: 4px;
        font-size: 11px;
        font-style: italic;
        color: var(--slc-muted);
      }

      .no-menu {
        font-size: 12px;
        color: var(--slc-muted);
        font-style: italic;
      }

      .day-notice {
        font-size: 12px;
        font-weight: 600;
        color: var(--warning-color, #ff9800);
        margin-bottom: 4px;
      }

      /* Subtle divider between schools when more than one */
      .school-block + .school-block {
        padding-top: 8px;
        border-top: 1px solid var(--slc-border);
      }

      /* ── Fixed height + touch scrolling (config: height) ── */
      .stage-wrap {
        position: relative;
        min-height: 0;
        display: flex;
        flex-direction: column;
      }

      ha-card.fixed-height {
        height: var(--slc-height);
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
      }

      ha-card.fixed-height .card-shell {
        flex: 1;
        min-height: 0;
        grid-template-rows: auto minmax(0, 1fr);
      }

      ha-card.fixed-height .content-stage {
        flex: 1;
        min-height: 0;
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
        overscroll-behavior: contain;
        touch-action: pan-y;
        scrollbar-width: none;
      }

      ha-card.fixed-height .content-stage::-webkit-scrollbar {
        display: none;
      }

      .scroll-fade {
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        height: 44px;
        border-radius: 0 0 var(--slc-radius-sm) var(--slc-radius-sm);
        background: linear-gradient(180deg, rgba(19, 24, 31, 0), rgba(19, 24, 31, 0.97) 70%);
        display: flex;
        align-items: flex-end;
        justify-content: center;
        padding-bottom: 2px;
        color: var(--slc-accent);
        --mdc-icon-size: 22px;
        pointer-events: none;
        opacity: 0;
        transition: opacity 150ms ease;
      }

      .scroll-fade.visible {
        opacity: 1;
      }
    `;
  }
}

customElements.define("school-lunch-card", SchoolLunchCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "school-lunch-card",
  name: "School Lunch Card",
  description: "At-a-glance school lunch menu for tomorrow",
  preview: true,
});
