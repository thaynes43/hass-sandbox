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
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App, Android/UniFi wall display.
 */

const SLC_DEFAULTS = {
  status_entity: "sensor.school_lunch_menu",
  selection_entity: "input_text.school_lunch_selected_schools",
  navigation_path: "#school-lunch-popup",
};

// Items that appear every day — shown as "Includes:" rather than options
const SLC_INCLUDES = ["chilled fruit", "milk choice", "milk"];

/**
 * Parse a day's items into numbered options and an includes line.
 *
 * - Items whose name (lowercased) matches SLC_INCLUDES → includes list
 * - Remaining items: first item = Option 1, items starting with "OR "
 *   (case-insensitive) get the prefix stripped and become subsequent options
 * - Returns { options: string[], includes: string[] }
 */
function parseLunchItems(items) {
  const options = [];
  const includes = [];

  for (const item of items) {
    const name = String(item.name || "").trim();
    if (!name) continue;
    const lower = name.toLowerCase();

    if (SLC_INCLUDES.includes(lower)) {
      includes.push(name);
      continue;
    }

    // Strip leading "OR " / "or " prefix
    const orMatch = name.match(/^or\s+/i);
    if (orMatch) {
      options.push(name.slice(orMatch[0].length));
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

    this._render();
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
    return JSON.stringify({
      status: status
        ? {
            s: status.state,
            schools: status.attributes?.schools,
            last_updated: status.attributes?.last_updated,
          }
        : null,
      selection: selection ? { s: selection.state } : null,
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
   * Returns { targetDay, targetMonth, targetYear, headerLabel }
   */
  _targetDate() {
    const now = new Date();
    const dow = now.getDay(); // 0=Sun, 1=Mon, … 6=Sat
    const hour = now.getHours();

    // Advance to next calendar day first (our "tomorrow")
    const tomorrow = new Date(now);
    tomorrow.setDate(now.getDate() + 1);

    // Cases that need Monday instead of tomorrow:
    // - current day is Sunday (0)
    // - current day is Saturday (6)
    // - current day is Friday (5) and hour >= 15
    const showMonday =
      dow === 0 || dow === 6 || (dow === 5 && hour >= 15);

    let target;
    let headerLabel;

    if (showMonday) {
      // Find the coming Monday
      const daysUntilMonday = (8 - now.getDay()) % 7 || 7;
      target = new Date(now);
      target.setDate(now.getDate() + daysUntilMonday);
      headerLabel = "Monday's Lunch";
    } else {
      target = tomorrow;
      headerLabel = "Tomorrow's Lunch";
    }

    return {
      targetDay: target.getDate(),
      targetMonth: target.getMonth() + 1, // 1-indexed to match sensor
      targetYear: target.getFullYear(),
      headerLabel,
    };
  }

  /**
   * Return entree items for a given school and target date.
   * Filters: category contains "Entree" (case-insensitive) AND is_ancillary === false.
   */
  _getDayInfo(school, targetDay, targetMonth, targetYear) {
    // The sensor stores the current month's menu. Verify the school's
    // month/year matches what we are looking for.
    if (school.month !== targetMonth || school.year !== targetYear) {
      return null; // wrong month — no data for this date
    }

    const dayData = (school.days || []).find((d) => d.day === targetDay);
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
  // Render
  // ---------------------------------------------------------------------------

  _render() {
    const schools = this._sensorAttr(this._config.status_entity, "schools", []);
    const selectedNames = this._selectedSchools();
    const { targetDay, targetMonth, targetYear, headerLabel } = this._targetDate();

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

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card data-action="open">
        <div class="card-shell">
          <div class="card-header">
            <div class="header-copy">
              <div class="header-kicker">School Lunch</div>
              <div class="header-title">${this._escapeHtml(headerLabel)}</div>
            </div>
            <ha-icon class="header-icon" icon="mdi:silverware-fork-knife"></ha-icon>
          </div>
          <div class="content-stage">
            ${bodyHtml}
          </div>
        </div>
      </ha-card>
    `;

    this._ensureDelegatedListeners();
  }

  // ---------------------------------------------------------------------------
  // Touch / click deduplication (delegated, single listener)
  // ---------------------------------------------------------------------------

  _ensureDelegatedListeners() {
    // Guard: only bind once per shadowRoot lifetime. Since we replace innerHTML
    // on each render, we track the binding on the instance, not the DOM node.
    if (this._delegatedBound) return;
    this._delegatedBound = true;

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
