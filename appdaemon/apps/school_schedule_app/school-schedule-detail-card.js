/**
 * School Schedule Detail Card
 *
 * The "what do those icons mean" view behind the compact school schedule
 * card: the six-day rotation laid out as a matrix (periods down, Day 1..6
 * across) with the same icon per class, teacher and room in each cell, the
 * current and next rotation day highlighted, and a legend.
 *
 * Opened from school-schedule-card via navigation_path:
 *   - wall-display: inside a bubble-card pop-up (#school-schedule-popup)
 *   - unifi-connect: as its own subview page (/unifi-connect/school-schedule)
 *
 * Reads sensor.school_schedule (published by school_schedule_app):
 *   cycle  — { "<day number>": [ { course, short, icon, period, teacher, room, hidden? } ] }
 *            every block of the day; hidden: "true" marks lunch/advisory, which the
 *            compact card leaves off but this view shows (muted)
 *   days   — { "YYYY-MM-DD": { day, classes: [ { period, start, end, ... } ] } }  (times)
 *   dates  — { "YYYY-MM-DD": <day number> }                                   (today / next)
 *   school, cycle_length, last_updated, sources
 *
 * Read-only: no relay script, no commands.
 *
 * Platforms: Desktop (Chrome/Firefox/Edge), iOS Companion App, Android/UniFi wall display.
 */

const SSD_DEFAULTS = {
  status_entity: "sensor.school_schedule",
  title: "School Schedule",
  show_legend: true,
  show_teacher: true,
  show_room: true,
};

const SSD_WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function ssdDateKey(d) {
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

/** "08:20" -> "8:20", "13:20" -> "1:20" (12-hour, no suffix — it is a school day); unparseable text is returned unchanged. */
function ssdShortTime(t) {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(t || ""));
  if (!m) return String(t || "");
  const h = Number(m[1]) % 12 || 12;
  return `${h}:${m[2]}`;
}

/**
 * Merge the per-day period sequences into one ordered list of period codes,
 * preserving each day's relative order (days can skip periods).
 */
function ssdMergePeriodOrder(sequences) {
  const order = [];
  for (const seq of sequences) {
    let anchor = -1; // index in `order` of the previous code from this sequence
    for (const code of seq) {
      const idx = order.indexOf(code);
      if (idx >= 0) {
        anchor = idx;
        continue;
      }
      order.splice(anchor + 1, 0, code);
      anchor += 1;
    }
  }
  return order;
}

class SchoolScheduleDetailCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...SSD_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
  }

  // ---------------------------------------------------------------------------
  // HA card lifecycle
  // ---------------------------------------------------------------------------

  setConfig(config) {
    this._config = { ...SSD_DEFAULTS, ...(config || {}) };
    this._lastSnapshot = null;
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const stateObj = hass?.states?.[this._config.status_entity];
    const snapshot = JSON.stringify([
      stateObj?.state ?? null,
      stateObj?.attributes ?? null,
      ssdDateKey(new Date()),
    ]);
    if (snapshot === this._lastSnapshot) return;
    this._lastSnapshot = snapshot;
    this._render();
  }

  getCardSize() {
    return 8;
  }

  static getStubConfig() {
    return { status_entity: "sensor.school_schedule" };
  }

  // ---------------------------------------------------------------------------
  // Data shaping
  // ---------------------------------------------------------------------------

  _model() {
    const stateObj = this._hass?.states?.[this._config.status_entity];
    const attrs = stateObj?.attributes || null;
    if (!attrs) return null;

    const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
    const cycle = isObj(attrs.cycle) ? attrs.cycle : {};
    const days = isObj(attrs.days) ? attrs.days : {};
    const dates = isObj(attrs.dates) ? attrs.dates : {};
    const cycleLength = Math.max(1, Number(attrs.cycle_length) || 6);

    const dayNumbers = [];
    for (let n = 1; n <= cycleLength; n++) dayNumbers.push(n);

    // Period rows, merged across days in the app's chronological order.
    const sequences = dayNumbers.map((n) =>
      (Array.isArray(cycle[String(n)]) ? cycle[String(n)] : [])
        .map((c) => String(c.period || ""))
        .filter(Boolean)
    );
    const periods = ssdMergePeriodOrder(sequences);

    // Cells: period -> day number -> class
    const cells = {};
    let anyClass = false;
    for (const n of dayNumbers) {
      const list = Array.isArray(cycle[String(n)]) ? cycle[String(n)] : [];
      for (const cls of list) {
        const code = String(cls.period || "");
        if (!code) continue;
        cells[code] = cells[code] || {};
        cells[code][n] = cls;
        anyClass = true;
      }
    }

    // Times per period from the dated schedule (first occurrence wins).
    const times = {};
    for (const key of Object.keys(days).sort()) {
      const list = Array.isArray(days[key]?.classes) ? days[key].classes : [];
      for (const cls of list) {
        const code = String(cls.period || "");
        if (code && !times[code] && cls.start) {
          times[code] = `${ssdShortTime(cls.start)}–${ssdShortTime(cls.end || "")}`.replace(/–$/, "");
        }
      }
    }

    // Today / next school day (same rule as the compact card).
    const now = new Date();
    const todayKey = ssdDateKey(now);
    const todayNum = dates[todayKey] ?? days[todayKey]?.day ?? null;
    let nextNum = null;
    let nextLabel = "";
    for (let i = 1; i <= 14; i++) {
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i);
      const key = ssdDateKey(d);
      const num = dates[key] ?? days[key]?.day ?? null;
      if (num !== null && num !== undefined) {
        nextNum = num;
        nextLabel = i === 1 ? "Tomorrow" : SSD_WEEKDAYS[d.getDay()];
        break;
      }
    }

    // Legend: unique icon + label pairs in first-seen order.
    const legend = [];
    const seen = new Set();
    for (const n of dayNumbers) {
      const list = Array.isArray(cycle[String(n)]) ? cycle[String(n)] : [];
      for (const cls of list) {
        const icon = String(cls.icon || "mdi:school");
        const short = String(cls.short || cls.course || "");
        const k = `${icon}|${short}`;
        if (seen.has(k)) continue;
        seen.add(k);
        legend.push({ icon, short, course: String(cls.course || "") });
      }
    }

    return {
      school: String(attrs.school || ""),
      cycleLength,
      dayNumbers,
      periods,
      cells,
      times,
      todayNum,
      nextNum,
      nextLabel,
      legend,
      anyClass,
      lastUpdated: String(attrs.last_updated || ""),
      sources: isObj(attrs.sources) ? attrs.sources : {},
    };
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------

  _render() {
    const model = this._model();
    const esc = (s) => this._escapeHtml(s);

    let body;
    if (!model) {
      body = `<p class="empty-msg">Schedule sensor not available.</p>`;
    } else if (!model.anyClass) {
      body = `<p class="empty-msg">No class schedule published yet. It refreshes every morning at 5:00 AM.</p>`;
    } else {
      body = this._renderMatrix(model) + (this._config.show_legend ? this._renderLegend(model) : "");
    }

    const subtitle = model?.school
      ? `${esc(model.school)} · ${model.cycleLength}-day rotation`
      : "";

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="card-header">
          <div class="header-row">
            <ha-icon icon="mdi:calendar-clock" class="header-icon"></ha-icon>
            <span class="header-title">${esc(this._config.title)}</span>
            <span class="header-subtitle">${subtitle}</span>
          </div>
        </div>
        <div class="content">
          ${body}
        </div>
        ${model ? this._renderFooter(model) : ""}
      </ha-card>
    `;
  }

  _renderMatrix(model) {
    const esc = (s) => this._escapeHtml(s);
    const cols = model.dayNumbers
      .map((n) => {
        const isToday = model.todayNum === n;
        const isNext = !isToday && model.nextNum === n;
        const chip = isToday
          ? `<span class="chip chip-today">Today</span>`
          : isNext
            ? `<span class="chip chip-next">${esc(model.nextLabel)}</span>`
            : "";
        return `<th class="day-head ${isToday ? "is-today" : ""} ${isNext ? "is-next" : ""}">
          <span class="day-name">Day ${n}</span>${chip}
        </th>`;
      })
      .join("");

    const rows = model.periods
      .map((code, i) => {
        const time = model.times[code] || "";
        const tds = model.dayNumbers
          .map((n) => {
            const cls = model.cells[code]?.[n];
            const isToday = model.todayNum === n;
            const isNext = !isToday && model.nextNum === n;
            const colClass = `${isToday ? "is-today" : ""} ${isNext ? "is-next" : ""}`;
            if (!cls) return `<td class="cell cell-empty ${colClass}"><span class="dash">–</span></td>`;
            const muted = cls.hidden === "true" || cls.hidden === true ? "cell-muted" : "";
            const icon = esc(String(cls.icon || "mdi:school"));
            const short = esc(String(cls.short || cls.course || ""));
            const course = String(cls.course || "");
            const meta = [
              this._config.show_teacher ? String(cls.teacher || "") : "",
              this._config.show_room ? String(cls.room || "") : "",
            ]
              .filter(Boolean)
              .join(" · ");
            return `<td class="cell ${colClass} ${muted}" title="${esc(course)}">
              <div class="cell-main">
                <span class="cell-icon"><ha-icon icon="${icon}"></ha-icon></span>
                <span class="cell-name">${short}</span>
              </div>
              ${meta ? `<div class="cell-meta">${esc(meta)}</div>` : ""}
            </td>`;
          })
          .join("");
        return `<tr>
          <th class="period-head">
            <span class="period-num">${i + 1}</span>
            ${time ? `<span class="period-time">${esc(time)}</span>` : `<span class="period-code">${esc(code)}</span>`}
          </th>
          ${tds}
        </tr>`;
      })
      .join("");

    return `
      <div class="matrix-wrap">
        <table class="matrix">
          <thead><tr><th class="corner">Period</th>${cols}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  _renderLegend(model) {
    const esc = (s) => this._escapeHtml(s);
    if (!model.legend.length) return "";
    const items = model.legend
      .map((item) => {
        const detail = item.course && item.course !== item.short ? `<span class="legend-course">${esc(item.course)}</span>` : "";
        return `<div class="legend-item">
          <span class="legend-icon"><ha-icon icon="${esc(item.icon)}"></ha-icon></span>
          <span class="legend-name">${esc(item.short)}</span>${detail}
        </div>`;
      })
      .join("");
    return `<div class="legend"><span class="legend-title">Icons</span>${items}</div>`;
  }

  _renderFooter(model) {
    const esc = (s) => this._escapeHtml(s);
    const parts = [];
    if (model.lastUpdated) {
      const d = new Date(model.lastUpdated);
      if (!Number.isNaN(d.getTime())) {
        parts.push(`Updated ${d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`);
      }
    }
    const src = (name, label) => {
      const s = model.sources?.[name]?.status;
      if (!s) return "";
      return `${label} ${s === "ok" ? "✓" : "✗"}`;
    };
    for (const p of [src("day_cycle", "Calendar"), src("powerschool", "PowerSchool")]) if (p) parts.push(p);
    if (!parts.length) return "";
    return `<div class="footer">${parts.map(esc).join(" · ")}</div>`;
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
  // Styles (shares the school-lunch-detail-card visual language)
  // ---------------------------------------------------------------------------

  _styles() {
    return `
      :host {
        --ssd-radius: 12px;
        --ssd-radius-sm: 8px;
        --ssd-spacing: 16px;
        --ssd-spacing-sm: 8px;
        --ssd-surface: var(--card-background-color, var(--ha-card-background, #fff));
        --ssd-surface-variant: var(--secondary-background-color, #f5f5f5);
        --ssd-on-surface: var(--primary-text-color, #212121);
        --ssd-on-surface-secondary: var(--secondary-text-color, #757575);
        --ssd-primary: var(--primary-color, #03a9f4);
        --ssd-primary-light: color-mix(in srgb, var(--ssd-primary) 12%, transparent);
        --ssd-border: var(--divider-color, #e0e0e0);
        --ssd-today: color-mix(in srgb, var(--ssd-primary) 14%, transparent);
        --ssd-next: color-mix(in srgb, var(--ssd-primary) 6%, transparent);
        font-size: 13px;
        color: var(--ssd-on-surface);
      }

      ha-card { overflow: hidden; }

      /* ── Header ── */
      .card-header { padding: var(--ssd-spacing) var(--ssd-spacing) 0; }

      .header-row {
        display: flex;
        align-items: baseline;
        gap: var(--ssd-spacing-sm);
        flex-wrap: wrap;
        padding-bottom: 12px;
        border-bottom: 2px solid var(--ssd-border);
      }

      .header-icon {
        color: var(--ssd-primary);
        --mdc-icon-size: 24px;
        align-self: center;
      }

      .header-title {
        font-size: 18px;
        font-weight: 500;
        color: var(--ssd-on-surface);
      }

      .header-subtitle {
        font-size: 13px;
        color: var(--ssd-on-surface-secondary);
        margin-left: auto;
      }

      /* ── Content ── */
      .content {
        padding: var(--ssd-spacing);
        overflow: auto;
        -webkit-overflow-scrolling: touch;
      }

      .empty-msg {
        color: var(--ssd-on-surface-secondary);
        margin: 0;
      }

      /* ── Matrix ── */
      .matrix-wrap { overflow-x: auto; }

      .matrix {
        width: 100%;
        border-collapse: separate;
        border-spacing: 4px;
        table-layout: fixed;
      }

      .matrix th, .matrix td {
        text-align: left;
        vertical-align: top;
        border-radius: var(--ssd-radius-sm);
      }

      .corner {
        width: 96px;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--ssd-on-surface-secondary);
        padding: 8px 10px;
      }

      .day-head {
        padding: 8px 12px;
        background: var(--ssd-surface-variant);
        white-space: nowrap;
      }

      .day-head.is-today { background: var(--ssd-today); box-shadow: inset 0 0 0 1.5px var(--ssd-primary); }
      .day-head.is-next { background: var(--ssd-next); }

      .day-name {
        font-size: 15px;
        font-weight: 600;
        margin-right: 8px;
      }

      .chip {
        display: inline-block;
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        padding: 2px 8px;
        border-radius: 999px;
        vertical-align: 2px;
      }

      .chip-today { background: var(--ssd-primary); color: #fff; }
      .chip-next { background: var(--ssd-primary-light); color: var(--ssd-primary); }

      .period-head {
        padding: 10px;
        background: var(--ssd-surface-variant);
        white-space: nowrap;
      }

      .period-num {
        display: inline-block;
        min-width: 22px;
        font-size: 15px;
        font-weight: 700;
        color: var(--ssd-primary);
      }

      .period-time, .period-code {
        display: block;
        font-size: 11px;
        color: var(--ssd-on-surface-secondary);
        margin-top: 2px;
      }

      .cell {
        padding: 10px 12px;
        background: var(--ssd-surface-variant);
      }

      .cell.is-today { background: var(--ssd-today); }
      .cell.is-next { background: var(--ssd-next); }

      .cell-empty { color: var(--ssd-on-surface-secondary); }
      .dash { opacity: 0.5; }

      /* lunch / advisory: present for the full picture, visually secondary */
      .cell-muted .cell-icon { background: transparent; box-shadow: inset 0 0 0 1px var(--ssd-border); color: var(--ssd-on-surface-secondary); }
      .cell-muted .cell-name { font-weight: 500; color: var(--ssd-on-surface-secondary); }

      .cell-main {
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: 0;
      }

      .cell-icon {
        flex: 0 0 auto;
        width: 32px;
        height: 32px;
        border-radius: 8px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background: color-mix(in srgb, var(--ssd-on-surface) 8%, transparent);
        color: var(--ssd-on-surface);
        --mdc-icon-size: 22px;
      }

      .cell-name {
        font-size: 14px;
        font-weight: 600;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .cell-meta {
        margin-top: 6px;
        font-size: 11px;
        color: var(--ssd-on-surface-secondary);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      /* ── Legend ── */
      .legend {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px 18px;
        margin-top: 14px;
        padding-top: 12px;
        border-top: 1px solid var(--ssd-border);
      }

      .legend-title {
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--ssd-on-surface-secondary);
        margin-right: 4px;
      }

      .legend-item {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        white-space: nowrap;
      }

      .legend-icon {
        width: 26px;
        height: 26px;
        border-radius: 7px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background: color-mix(in srgb, var(--ssd-on-surface) 8%, transparent);
        --mdc-icon-size: 18px;
      }

      .legend-name { font-weight: 600; }

      .legend-course {
        font-size: 11px;
        color: var(--ssd-on-surface-secondary);
      }

      /* ── Footer ── */
      .footer {
        padding: 0 var(--ssd-spacing) 12px;
        font-size: 11px;
        color: var(--ssd-on-surface-secondary);
      }
    `;
  }
}

if (!customElements.get("school-schedule-detail-card")) {
  customElements.define("school-schedule-detail-card", SchoolScheduleDetailCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "school-schedule-detail-card",
  name: "School Schedule Detail Card",
  description: "Six-day rotation matrix with class icons, teachers, rooms and a legend.",
});
