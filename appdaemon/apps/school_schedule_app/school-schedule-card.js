/**
 * School Schedule Card
 *
 * Compact at-a-glance card for the wall-display dashboard. Shows which day
 * of the school's six-day rotation today and the next school day are, with
 * one icon per class on each of those days.
 *
 * Designed to sit directly under the school lunch card on a 1920x1080
 * UniFi Connect display, so its height is fixed (two 40px rows + padding,
 * ~112px) and never grows with content: icons shrink instead of wrapping.
 *
 * Reads sensor.school_schedule (published by school_schedule_app):
 *   dates  — { "YYYY-MM-DD": <day number> } for every school day scraped
 *   days   — { "YYYY-MM-DD": { day, classes: [ { course, short, icon, ... } ], note } }
 *            per-date classes from PowerSchool (term- and holiday-aware)
 *   cycle  — { "<day number>": [ { course, short, icon, period, ... } ] } fallback
 *   Classes carrying hidden: "true" (lunch, advisory) are left off this card;
 *   the detail card shows them.
 *   closures — { "YYYY-MM-DD": "<label>" } optional no-school notes
 *   today / next — precomputed fallbacks when `dates` is missing
 *
 * Tapping the card navigates to config.navigation_path when one is set.
 *
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App, Android/UniFi wall display.
 */

const SSC_DEFAULTS = {
  status_entity: "sensor.school_schedule",
  navigation_path: "",
  today_label: "Today",
  tomorrow_label: "Tomorrow",
  max_lookahead_days: 14,
};

const SSC_WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const SSC_WEEKDAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** Local-time YYYY-MM-DD key for a Date. */
function sscDateKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** Parse a YYYY-MM-DD key into a local-time Date (midnight). */
function sscParseKey(key) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(key || ""));
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

/** "Wed 9/3" style short date. */
function sscShortDate(d) {
  return `${SSC_WEEKDAYS_SHORT[d.getDay()]} ${d.getMonth() + 1}/${d.getDate()}`;
}

class SchoolScheduleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...SSC_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._domBuilt = false;
    this._touchActive = false;
    this._els = null;
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...SSC_DEFAULTS, ...(config || {}) };
    this._lastSnapshot = null;
    if (this._domBuilt) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._domBuilt) this._buildDom();

    // Re-render only when the sensor payload or the calendar day changes.
    const stateObj = hass?.states?.[this._config.status_entity];
    const snapshot = JSON.stringify([
      stateObj?.state ?? null,
      stateObj?.attributes ?? null,
      sscDateKey(new Date()),
    ]);
    if (snapshot === this._lastSnapshot) return;
    this._lastSnapshot = snapshot;
    this._update();
  }

  getCardSize() {
    return 2;
  }

  static getStubConfig() {
    return { status_entity: "sensor.school_schedule" };
  }

  // ---------------------------------------------------------------------------
  // Data
  // ---------------------------------------------------------------------------

  _attrs() {
    const stateObj = this._hass?.states?.[this._config.status_entity];
    if (!stateObj) return null;
    return stateObj.attributes || {};
  }

  /**
   * Resolve the two rows to display: today and the next school day.
   *
   * Sources, in order of preference for a given date key:
   *   day number — attrs.dates[key] (calendar), else attrs.days[key].day
   *   classes    — attrs.days[key].classes (PowerSchool, per date, term-aware),
   *                else attrs.cycle[dayNumber] (per rotation day)
   *
   * Returns { available, rows: [ { kicker, dateLabel, dayNumber, classes, note } ] }
   */
  _resolveRows() {
    const attrs = this._attrs();
    if (!attrs) return { available: false, rows: [] };

    const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
    const dates = isObj(attrs.dates) ? attrs.dates : null;
    const days = isObj(attrs.days) ? attrs.days : null;
    const cycle = isObj(attrs.cycle) ? attrs.cycle : {};
    const closures = isObj(attrs.closures) ? attrs.closures : {};

    const now = new Date();
    const todayKey = sscDateKey(now);
    const rows = [];

    // The app flags lunch/advisory blocks with hidden: "true" (a string —
    // AppDaemon drops boolean attribute values); the compact card skips them.
    const shown = (list) => list.filter((c) => c && c.hidden !== "true" && c.hidden !== true);

    const cycleClasses = (dayNumber) => {
      if (dayNumber === null || dayNumber === undefined) return [];
      const list = cycle[String(dayNumber)];
      return Array.isArray(list) ? shown(list) : [];
    };

    const dayInfo = (key) => {
      const perDate = days ? days[key] : null;
      let num = dates ? dates[key] : undefined;
      if (num === undefined || num === null) num = perDate?.day ?? null;
      const perDateClasses = Array.isArray(perDate?.classes) ? shown(perDate.classes) : [];
      const classes = perDateClasses.length > 0 ? perDateClasses : cycleClasses(num);
      return {
        num,
        classes,
        isSchoolDay: num !== null || classes.length > 0,
        note: perDate?.note || closures[key] || "",
      };
    };

    if (dates || days) {
      // Today
      const today = dayInfo(todayKey);
      rows.push({
        kicker: this._config.today_label,
        dateLabel: sscShortDate(now),
        dayNumber: today.num,
        classes: today.classes,
        note: today.isSchoolDay ? today.note : today.note || "No school",
      });

      // Next school day (first future date with a day number or classes)
      let nextKey = null;
      let nextDate = null;
      let next = null;
      const lookahead = Math.max(1, Number(this._config.max_lookahead_days) || 14);
      for (let i = 1; i <= lookahead; i++) {
        const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i);
        const key = sscDateKey(d);
        const info = dayInfo(key);
        if (info.isSchoolDay) {
          nextKey = key;
          nextDate = d;
          next = info;
          break;
        }
      }
      if (nextKey) {
        const isTomorrow =
          sscDateKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)) === nextKey;
        rows.push({
          kicker: isTomorrow ? this._config.tomorrow_label : SSC_WEEKDAYS[nextDate.getDay()],
          dateLabel: sscShortDate(nextDate),
          dayNumber: next.num,
          classes: next.classes,
          note: next.note,
        });
      } else {
        rows.push({
          kicker: this._config.tomorrow_label,
          dateLabel: "",
          dayNumber: null,
          classes: [],
          note: "No school days scheduled",
        });
      }
      return { available: true, rows };
    }

    // Fallback: precomputed today/next from the app
    const fallback = (entry, kicker) => {
      const d = sscParseKey(entry?.date);
      const hasDay = entry?.day !== null && entry?.day !== undefined;
      return {
        kicker,
        dateLabel: d ? sscShortDate(d) : "",
        dayNumber: hasDay ? entry.day : null,
        classes: Array.isArray(entry?.classes) ? shown(entry.classes) : cycleClasses(entry?.day),
        note: hasDay ? "" : entry?.note || "No school",
      };
    };
    if (attrs.today || attrs.next) {
      rows.push(fallback(attrs.today, this._config.today_label));
      rows.push(fallback(attrs.next, this._config.tomorrow_label));
      return { available: true, rows };
    }
    return { available: false, rows: [] };
  }

  // ---------------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------------

  _buildDom() {
    const clickable = Boolean(this._config.navigation_path);
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card ${clickable ? 'data-action="open"' : ""} class="${clickable ? "clickable" : ""}">
        <div class="card-shell"></div>
      </ha-card>
    `;
    this._els = { shell: this.shadowRoot.querySelector(".card-shell") };
    this._bindEvents();
    this._domBuilt = true;
  }

  _update() {
    if (!this._els) return;
    const { available, rows } = this._resolveRows();

    if (!available) {
      this._els.shell.innerHTML = `
        <div class="empty">
          <ha-icon icon="mdi:school-outline"></ha-icon>
          <span>Schedule unavailable</span>
        </div>`;
      return;
    }

    this._els.shell.innerHTML = rows.map((row) => this._renderRow(row)).join("");
  }

  _renderRow(row) {
    const hasDay = row.dayNumber !== null && row.dayNumber !== undefined;
    const badge = hasDay
      ? `<div class="badge"><span class="badge-caption">Day</span><span class="badge-num">${this._escapeHtml(String(row.dayNumber))}</span></div>`
      : `<div class="badge badge-off"><span class="badge-caption">Day</span><span class="badge-num">–</span></div>`;

    let body;
    if (hasDay && row.classes.length > 0) {
      // Fixed-height card: shrink icons as the class count grows so the row
      // never wraps (fits 9 classes in a ~440px column). A calendar note on a
      // school day (early release, delay) adds an amber alert chip at the end.
      const n = row.classes.length + (row.note ? 1 : 0);
      const size = n <= 6 ? 28 : n === 7 ? 24 : n === 8 ? 20 : 18;
      const icons = row.classes
        .map((cls) => {
          const icon = this._escapeHtml(String(cls.icon || "mdi:book-open-variant"));
          const label = this._escapeHtml(String(cls.short || cls.course || ""));
          return `<div class="class-icon" title="${label}"><ha-icon icon="${icon}"></ha-icon></div>`;
        })
        .join("");
      const noteChip = row.note
        ? `<div class="class-icon note-chip" title="${this._escapeHtml(row.note)}"><ha-icon icon="mdi:clock-alert-outline"></ha-icon></div>`
        : "";
      body = `<div class="icons" style="--ssc-icon-size:${size}px">${icons}${noteChip}</div>`;
    } else if (hasDay) {
      body = `<div class="note">${this._escapeHtml(row.note || "No classes listed")}</div>`;
    } else {
      body = `<div class="note">${this._escapeHtml(row.note || "No school")}</div>`;
    }

    return `
      <div class="row ${hasDay ? "" : "row-off"}">
        <div class="label">
          <div class="kicker">${this._escapeHtml(row.kicker)}</div>
          <div class="date">${this._escapeHtml(row.dateLabel)}</div>
        </div>
        ${badge}
        ${body}
      </div>`;
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
      if (el.dataset.action === "open" && this._config.navigation_path) {
        this._navigate(this._config.navigation_path);
      }
    };

    let touchCancelled = false;
    ["touchcancel", "touchmove", "scroll"].forEach((evtName) => {
      root.addEventListener(evtName, () => { touchCancelled = true; }, { passive: true });
    });
    root.addEventListener("touchstart", () => { touchCancelled = false; }, { passive: true });

    root.addEventListener(
      "touchend",
      (e) => {
        const el = findActionEl(e);
        if (touchCancelled || !el) {
          this._touchActive = false;
          return;
        }
        // Never preventDefault on native form elements (Android keyboards).
        const tag = el.tagName?.toLowerCase();
        const nativeEl = tag === "input" || tag === "select" || tag === "textarea";
        if (!nativeEl && e.cancelable) e.preventDefault();

        this._touchActive = true;
        dispatchAction(el);
        setTimeout(() => { this._touchActive = false; }, 400);
      },
      { passive: false }
    );

    root.addEventListener("click", (e) => {
      if (this._touchActive) return;
      const el = findActionEl(e);
      if (el) dispatchAction(el);
    });
  }

  _navigate(path) {
    if (!path) return;
    if (path.startsWith("#")) {
      window.location.hash = path;
      return;
    }
    window.history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { bubbles: true, composed: true }));
  }

  _escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ---------------------------------------------------------------------------
  // Styles
  // ---------------------------------------------------------------------------

  _styles() {
    return `
      :host {
        --ssc-radius-sm: 12px;
        --ssc-border: color-mix(in srgb, var(--primary-text-color, #fff) 12%, transparent);
        --ssc-text: var(--primary-text-color, #f5f7fa);
        --ssc-muted: color-mix(in srgb, var(--secondary-text-color, #b6beca) 88%, white 12%);
        --ssc-accent: var(--primary-color, #66b3ff);
        --ssc-row-h: 40px;
        display: block;
      }

      ha-card {
        overflow: hidden;
        background:
          radial-gradient(circle at top, rgba(115, 160, 255, 0.16), transparent 34%),
          linear-gradient(180deg, rgba(12, 15, 20, 0.98), rgba(19, 24, 31, 0.98));
        transition: filter 120ms ease;
      }

      ha-card.clickable { cursor: pointer; }
      ha-card.clickable:active { filter: brightness(0.92); }

      .card-shell {
        padding: 12px 14px;
        display: grid;
        gap: 8px;
      }

      .row {
        height: var(--ssc-row-h);
        display: grid;
        grid-template-columns: 82px 44px minmax(0, 1fr);
        align-items: center;
        gap: 10px;
        border-radius: var(--ssc-radius-sm);
        background: rgba(255, 255, 255, 0.04);
        box-shadow: inset 0 0 0 1px var(--ssc-border);
        padding: 0 10px 0 12px;
        box-sizing: border-box;
      }

      .label { min-width: 0; line-height: 1.15; }

      .kicker {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--ssc-accent);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .date {
        margin-top: 2px;
        font-size: 11px;
        font-weight: 500;
        color: var(--ssc-muted);
        white-space: nowrap;
      }

      .badge {
        width: 44px;
        height: 32px;
        border-radius: 9px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        line-height: 1;
        background: color-mix(in srgb, var(--ssc-accent) 22%, transparent);
        box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--ssc-accent) 45%, transparent);
        color: var(--ssc-text);
      }

      .badge-caption {
        font-size: 8px;
        font-weight: 700;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: var(--ssc-accent);
      }

      .badge-num {
        margin-top: 1px;
        font-size: 17px;
        font-weight: 700;
      }

      .badge-off {
        background: rgba(255, 255, 255, 0.05);
        box-shadow: inset 0 0 0 1px var(--ssc-border);
      }

      .badge-off .badge-caption { color: var(--ssc-muted); }
      .badge-off .badge-num { color: var(--ssc-muted); }

      .row-off .kicker { color: var(--ssc-muted); }

      .icons {
        --ssc-icon-size: 28px;
        display: flex;
        align-items: center;
        justify-content: flex-start;
        gap: 4px;
        min-width: 0;
        overflow: hidden;
      }

      .class-icon {
        flex: 1 1 0;
        min-width: 0;
        max-width: calc(var(--ssc-icon-size) + 8px);
        height: calc(var(--ssc-icon-size) + 6px);
        border-radius: 8px;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(255, 255, 255, 0.05);
        color: var(--ssc-text);
        --mdc-icon-size: var(--ssc-icon-size);
      }

      .note-chip {
        color: #ffb454;
        background: rgba(255, 180, 84, 0.14);
        box-shadow: inset 0 0 0 1px rgba(255, 180, 84, 0.35);
      }

      .note {
        font-size: 13px;
        font-weight: 500;
        color: var(--ssc-muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .empty {
        display: flex;
        align-items: center;
        gap: 10px;
        height: calc(var(--ssc-row-h) * 2 + 8px);
        color: var(--ssc-muted);
        font-size: 13px;
        --mdc-icon-size: 22px;
      }
    `;
  }
}

if (!customElements.get("school-schedule-card")) {
  customElements.define("school-schedule-card", SchoolScheduleCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "school-schedule-card",
  name: "School Schedule Card",
  description: "Six-day rotation: today's and the next school day's day number with class icons.",
});
