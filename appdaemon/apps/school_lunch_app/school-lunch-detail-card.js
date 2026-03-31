/**
 * School Lunch Detail Card
 *
 * Multi-tab popup card showing weekly and monthly school lunch menus.
 * Tabs:
 *   - "This Week": Mon-Fri grid, one row per selected school, tomorrow highlighted
 *   - Per-school tabs: full month calendar (Mon-Fri), prev/next month navigation
 *   - "Settings": school selection checkboxes, last-updated timestamp
 *
 * Reads state from:
 *   - sensor.school_lunch_menu (schools data)
 *   - input_text.school_lunch_selected_schools (JSON array of selected school names)
 *
 * Sends commands via relay script:
 *   - select_schools: { schools: [...] }
 *   - fetch_month: { school: "...", menu_id: "..." }
 *
 * Mandatory patterns:
 *   - Shadow DOM
 *   - Touch/click deduplication (400ms flag)
 *   - NEVER preventDefault() on input/select/textarea touchend (Android checkboxes)
 *   - Focus guard: skip re-render when shadowRoot.activeElement is set
 */

const SLD_DEFAULTS = {
  status_entity: "sensor.school_lunch_menu",
  selection_entity: "input_text.school_lunch_selected_schools",
  relay_script: "school_lunch_relay",
};

// ── Utility helpers ─────────────────────────────────────────────────────────

/**
 * Escape HTML to prevent XSS when injecting strings into innerHTML.
 */
function sldEscapeHtml(str) {
  if (!str) return "";
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/**
 * Return true if the given date is a weekend (Sat=6 or Sun=0).
 */
function isWeekend(date) {
  const d = date.getDay();
  return d === 0 || d === 6;
}

/**
 * Determine the "target day" for the "This Week" highlight.
 *
 * ``cutoffStr`` is ``"HH:MM:SS"`` from sensor attribute ``show_tomorrow_after``.
 * Before cutoff → show today (or Monday on weekends).
 * After cutoff  → show tomorrow (or Monday on Fri evening / weekends).
 *
 * Returns a Date (time set to midnight local).
 */
function getTargetDay(cutoffStr) {
  const now = new Date();
  const day = now.getDay(); // 0=Sun,1=Mon,...,6=Sat

  const [cutH, cutM, cutS] = String(cutoffStr || "15:00:00").split(":").map(Number);
  const cutoff = new Date(now);
  cutoff.setHours(cutH || 15, cutM || 0, cutS || 0, 0);
  const pastCutoff = now >= cutoff;

  let target = new Date(now);
  target.setHours(0, 0, 0, 0);

  // Weekend → Monday
  if (day === 0) {
    target.setDate(target.getDate() + 1);
    return target;
  }
  if (day === 6) {
    target.setDate(target.getDate() + 2);
    return target;
  }

  if (pastCutoff) {
    // After cutoff: tomorrow (or Monday if Friday)
    target.setDate(target.getDate() + (day === 5 ? 3 : 1));
  }
  // Before cutoff: today (target already set to today at midnight)
  return target;
}

/**
 * Split a day's items into options and includes using the ``role`` field
 * set by the AppDaemon app. The app classifies items automatically:
 * items appearing on 75%+ of menu days → "includes", everything else → "option".
 *
 * Returns { options: string[], includes: string[] }
 */
function parseLunchItems(items) {
  if (!Array.isArray(items)) return { options: [], includes: [] };
  const options = [];
  const includes = [];
  for (const it of items) {
    const name = String(it.name || "").trim();
    if (!name) continue;
    if (it.role === "includes") {
      includes.push(name);
    } else {
      options.push(name);
    }
  }
  return { options, includes };
}

/**
 * Render parsed lunch items as HTML with numbered options + includes.
 * cssPrefix controls class names (e.g. "menu" for weekly, "cal" for monthly).
 */
function renderLunchHtml(parsed, cssPrefix = "menu") {
  const { options, includes } = parsed;
  if (options.length === 0 && includes.length === 0) {
    return `<span class="${cssPrefix}-no-menu">No menu</span>`;
  }
  let html = "";
  if (options.length > 0) {
    html += options
      .map(
        (opt, i) =>
          `<div class="${cssPrefix}-option"><span class="${cssPrefix}-option-num">Option ${i + 1}:</span> ${sldEscapeHtml(opt)}</div>`
      )
      .join("");
  }
  if (includes.length > 0) {
    html += `<div class="${cssPrefix}-includes">Includes: ${sldEscapeHtml(includes.join(" & "))}</div>`;
  }
  return html;
}

/**
 * Given a month (1-indexed) and year, return an array of week rows.
 * Each row is an array of 5 elements (Mon-Fri): null for days outside the month,
 * or the day number for days inside it.
 *
 * Only Mon-Fri columns are included; weekends are skipped.
 */
function buildMonthGrid(month, year) {
  // month is 1-indexed; new Date uses 0-indexed
  const firstDay = new Date(year, month - 1, 1);
  const lastDay = new Date(year, month, 0); // last day of month

  const rows = [];
  // Advance to Monday of the first week that contains a weekday in this month
  // Start iteration from the Monday on or before the first of the month
  const start = new Date(firstDay);
  // getDay: 0=Sun,1=Mon,2=Tue,3=Wed,4=Thu,5=Fri,6=Sat
  const firstDow = start.getDay();
  // Move back to Monday of that week
  const daysBackToMonday = firstDow === 0 ? 6 : firstDow - 1;
  start.setDate(start.getDate() - daysBackToMonday);

  // Build rows until we've covered the full month
  let cursor = new Date(start);
  while (cursor <= lastDay) {
    const row = [];
    for (let col = 0; col < 5; col++) {
      // col 0=Mon, 1=Tue, ..., 4=Fri
      const cellMonth = cursor.getMonth() + 1; // 1-indexed
      const cellYear = cursor.getFullYear();
      if (cellMonth === month && cellYear === year) {
        row.push(cursor.getDate());
      } else {
        row.push(null); // outside this month
      }
      cursor.setDate(cursor.getDate() + 1);
    }
    // Skip weekends automatically (cursor jumps from Fri to Mon)
    // Sat=skip, Sun=skip
    while (isWeekend(cursor)) {
      cursor.setDate(cursor.getDate() + 1);
    }
    // Only add the row if it overlaps the month at all
    if (row.some((d) => d !== null)) {
      rows.push(row);
    }
  }
  return rows;
}

/**
 * Given a month (1-indexed) and year, return the Monday date of the
 * current week (or null if today is a weekend).
 */
function currentWeekMonday() {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const dow = today.getDay(); // 0=Sun,6=Sat
  if (dow === 0 || dow === 6) return null; // weekend — no "current" weekday
  const daysBack = dow - 1; // Mon=1 → 0, Tue=2 → 1, ...
  const monday = new Date(today);
  monday.setDate(today.getDate() - daysBack);
  return monday;
}

/**
 * Get the Mon-Fri dates of the current (or next) business week.
 * If today is a weekend, return next week's Mon-Fri.
 */
function getWeekDates(cutoffStr) {
  const target = getTargetDay(cutoffStr);
  // target is always a weekday (Mon-Fri)
  // Find Monday of target's week
  const dow = target.getDay(); // 1=Mon,...,5=Fri
  const monday = new Date(target);
  monday.setDate(target.getDate() - (dow - 1));
  const dates = [];
  for (let i = 0; i < 5; i++) {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    dates.push(d);
  }
  return dates;
}

/**
 * Format a Date as "Mon 3" (abbreviated weekday + day number).
 */
function formatWeekDayHeader(date) {
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return `${days[date.getDay()]} ${date.getDate()}`;
}

/**
 * Month name for display.
 */
function monthName(month1indexed) {
  const names = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  return names[month1indexed - 1] || String(month1indexed);
}

// ── Card class ──────────────────────────────────────────────────────────────

class SchoolLunchDetailCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { ...SLD_DEFAULTS };
    this._hass = null;
    this._lastSnapshot = null;
    this._delegatedBound = false;
    // Active tab: "week" | school name | "settings"
    this._activeTab = "week";
  }

  // ── HA Card interface ──────────────────────────────────────────────

  setConfig(config) {
    this._config = { ...SLD_DEFAULTS, ...config };
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

  getCardSize() {
    return 6;
  }

  // ── State helpers ──────────────────────────────────────────────────

  _snapshot() {
    if (!this._hass) return null;
    const ss = this._hass.states[this._config.status_entity];
    const sel = this._hass.states[this._config.selection_entity];
    const statusPart = ss
      ? `${ss.state}|${JSON.stringify(ss.attributes)}`
      : "?";
    const selPart = sel ? sel.state : "?";
    // Include the target date so the card re-renders on day and cutoff transitions
    const cutoff = ss?.attributes?.show_tomorrow_after || "15:00:00";
    const target = getTargetDay(cutoff);
    const datePart = `${target.getFullYear()}-${target.getMonth() + 1}-${target.getDate()}`;
    return `${statusPart}~${selPart}~${this._activeTab}~${datePart}`;
  }

  _schools() {
    const ss = this._hass?.states?.[this._config.status_entity];
    return ss?.attributes?.schools || [];
  }

  _selectedSchools() {
    const sel = this._hass?.states?.[this._config.selection_entity];
    if (!sel?.state) return [];
    try {
      return JSON.parse(sel.state);
    } catch {
      return [];
    }
  }

  _lastUpdated() {
    const ss = this._hass?.states?.[this._config.status_entity];
    return ss?.attributes?.last_updated || null;
  }

  // ── Relay ──────────────────────────────────────────────────────────

  _callRelay(command, data) {
    if (!this._hass) return;
    this._hass
      .callService("script", this._config.relay_script, {
        command,
        payload: JSON.stringify(data || {}),
      })
      .catch((err) => {
        console.warn("school-lunch-detail-card: relay failed", command, err);
      });
  }

  // ── Render ─────────────────────────────────────────────────────────

  _render() {
    // Focus guard: skip re-render if any input/textarea has focus.
    // This prevents checkboxes and any text inputs losing state mid-interaction.
    const active = this.shadowRoot?.activeElement;
    if (active) {
      const tag = active.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT"
      ) {
        return;
      }
    }

    const schools = this._schools();
    const selectedSchools = this._selectedSchools();
    const statusState = this._hass?.states?.[this._config.status_entity];
    const cutoffStr = statusState?.attributes?.show_tomorrow_after || "15:00:00";

    // Build tab bar
    const tabs = [
      { id: "week", label: "This Week" },
      ...schools.map((s) => ({ id: s.name, label: s.name })),
      { id: "settings", label: "Settings" },
    ];

    // Validate active tab still exists (e.g. schools changed)
    const validTabIds = new Set(tabs.map((t) => t.id));
    if (!validTabIds.has(this._activeTab)) {
      this._activeTab = "week";
    }

    const tabBarHtml = tabs
      .map(
        (t) => `
      <button
        class="tab-btn ${t.id === this._activeTab ? "active" : ""}"
        data-action="switch-tab"
        data-tab="${sldEscapeHtml(t.id)}"
      >${sldEscapeHtml(t.label)}</button>`
      )
      .join("");

    let contentHtml = "";
    if (this._activeTab === "week") {
      contentHtml = this._renderWeekTab(schools, selectedSchools, cutoffStr);
    } else if (this._activeTab === "settings") {
      contentHtml = this._renderSettingsTab(schools, selectedSchools);
    } else {
      // Per-school month calendar
      const school = schools.find((s) => s.name === this._activeTab);
      contentHtml = school
        ? this._renderMonthTab(school)
        : `<p class="empty-msg">School data not available.</p>`;
    }

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="card-header">
          <div class="header-row">
            <ha-icon icon="mdi:silverware-fork-knife" class="header-icon"></ha-icon>
            <span class="header-title">School Lunch Menu</span>
          </div>
        </div>
        <div class="tab-bar" role="tablist">
          ${tabBarHtml}
        </div>
        <div class="tab-content">
          ${contentHtml}
        </div>
      </ha-card>
    `;

    this._ensureDelegatedListeners();
  }

  // ── Tab: This Week ─────────────────────────────────────────────────

  _renderWeekTab(schools, selectedSchools, cutoffStr) {
    const weekDates = getWeekDates(cutoffStr);
    const targetDay = getTargetDay(cutoffStr);
    const targetDayNum = targetDay.getDate();
    const targetMonth = targetDay.getMonth(); // 0-indexed
    const targetYear = targetDay.getFullYear();

    // Header row: Mon-Fri with date
    const headerCols = weekDates
      .map((d) => {
        const isTarget =
          d.getDate() === targetDayNum &&
          d.getMonth() === targetMonth &&
          d.getFullYear() === targetYear;
        return `<th class="week-day-header ${isTarget ? "highlight-col" : ""}">${formatWeekDayHeader(d)}</th>`;
      })
      .join("");

    // One row per selected school
    const selectedSet = new Set(selectedSchools);
    const filteredSchools = schools.filter((s) => selectedSet.has(s.name));

    if (filteredSchools.length === 0) {
      return `
        <div class="week-grid-wrap">
          <p class="empty-msg">No schools selected. Go to Settings to select schools.</p>
        </div>`;
    }

    const rows = filteredSchools
      .map((school) => {
        const cells = weekDates
          .map((d) => {
            const isTarget =
              d.getDate() === targetDayNum &&
              d.getMonth() === targetMonth &&
              d.getFullYear() === targetYear;

            // Match per-day month/year so cross-month weeks work
            const cellMonth = d.getMonth() + 1; // 1-indexed
            const cellYear = d.getFullYear();
            const dayData = school.days?.find(
              (day) => day.day === d.getDate() && day.month === cellMonth && day.year === cellYear
            );
            let cellContent = "";

            if (dayData) {
              if (dayData.notice) {
                cellContent = `<span class="day-notice">${sldEscapeHtml(dayData.notice)}</span>`;
                const parsed = parseLunchItems(dayData.items);
                if (parsed.options.length > 0) {
                  cellContent += renderLunchHtml(parsed, "menu");
                }
              } else {
                cellContent = renderLunchHtml(parseLunchItems(dayData.items), "menu");
              }
            } else {
              cellContent = `<span class="no-menu">—</span>`;
            }

            return `<td class="week-cell ${isTarget ? "highlight-col" : ""}">${cellContent}</td>`;
          })
          .join("");

        return `
          <tr>
            <td class="school-label">${sldEscapeHtml(school.name)}</td>
            ${cells}
          </tr>`;
      })
      .join("");

    return `
      <div class="week-grid-wrap">
        <div class="table-scroll">
          <table class="week-table">
            <thead>
              <tr>
                <th class="school-col-header"></th>
                ${headerCols}
              </tr>
            </thead>
            <tbody>
              ${rows}
            </tbody>
          </table>
        </div>
      </div>`;
  }

  // ── Tab: Month calendar (per school) ──────────────────────────────

  _renderMonthTab(school) {
    const { name, month, year, days, prev_month_id, next_month_id } = school;
    const grid = buildMonthGrid(month, year);

    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const cwMonday = currentWeekMonday();

    // Day headers Mon-Fri
    const dayHeaders = ["Mon", "Tue", "Wed", "Thu", "Fri"]
      .map((d) => `<th class="cal-day-header">${d}</th>`)
      .join("");

    // Build calendar rows
    const calRows = grid
      .map((row) => {
        // Determine if this row is the "current week"
        let isCurrentWeek = false;
        if (cwMonday) {
          // The row's Monday corresponds to the first non-null cell in the row,
          // adjusted back to Monday. We check by computing the Monday of this row.
          // Since grid rows are Mon-Fri, col 0 is Monday.
          // If col 0 is null, use the first non-null cell's Monday.
          const firstDayNum = row[0] ?? row.find((d) => d !== null);
          if (firstDayNum !== null && firstDayNum !== undefined) {
            // col 0 = Monday of this row in the target month
            // We need to find the actual Monday date
            const colOffset = row.indexOf(firstDayNum);
            // day number for the Monday of this row
            const mondayDayNum = firstDayNum - colOffset;
            if (mondayDayNum >= 1) {
              const rowMonday = new Date(year, month - 1, mondayDayNum);
              isCurrentWeek =
                rowMonday.getTime() === cwMonday.getTime() &&
                rowMonday.getMonth() + 1 === month &&
                rowMonday.getFullYear() === year;
            }
          }
        }

        const cells = row
          .map((dayNum) => {
            if (dayNum === null) {
              return `<td class="cal-cell empty"></td>`;
            }
            const dayData = days?.find(
              (d) => d.day === dayNum && d.month === month && d.year === year
            );
            let itemsHtml = "";
            if (dayData) {
              if (dayData.notice) {
                itemsHtml = `<span class="cal-notice">${sldEscapeHtml(dayData.notice)}</span>`;
                const parsed = parseLunchItems(dayData.items);
                if (parsed.options.length > 0) {
                  itemsHtml += renderLunchHtml(parsed, "cal");
                }
              } else {
                itemsHtml = renderLunchHtml(parseLunchItems(dayData.items), "cal");
              }
            }

            // Highlight today
            const isToday =
              dayNum === today.getDate() &&
              month === today.getMonth() + 1 &&
              year === today.getFullYear();

            return `
              <td class="cal-cell ${isToday ? "today" : ""}">
                <div class="cal-day-num ${isToday ? "today-num" : ""}">${dayNum}</div>
                ${itemsHtml}
              </td>`;
          })
          .join("");

        return `<tr class="${isCurrentWeek ? "current-week-row" : ""}">${cells}</tr>`;
      })
      .join("");

    // Navigation arrows — only show if IDs are available
    const prevBtn = prev_month_id
      ? `<button class="nav-arrow" data-action="prev-month" data-school="${sldEscapeHtml(name)}" data-menu-id="${sldEscapeHtml(prev_month_id)}" title="Previous month">&#8249;</button>`
      : `<button class="nav-arrow" disabled title="No previous month">&#8249;</button>`;
    const nextBtn = next_month_id
      ? `<button class="nav-arrow" data-action="next-month" data-school="${sldEscapeHtml(name)}" data-menu-id="${sldEscapeHtml(next_month_id)}" title="Next month">&#8250;</button>`
      : `<button class="nav-arrow" disabled title="No next month">&#8250;</button>`;

    return `
      <div class="month-view">
        <div class="month-nav">
          ${prevBtn}
          <span class="month-title">${monthName(month)} ${year}</span>
          ${nextBtn}
        </div>
        <div class="table-scroll">
          <table class="cal-table">
            <thead>
              <tr>${dayHeaders}</tr>
            </thead>
            <tbody>
              ${calRows}
            </tbody>
          </table>
        </div>
      </div>`;
  }

  // ── Tab: Settings ──────────────────────────────────────────────────

  _renderSettingsTab(schools, selectedSchools) {
    const selectedSet = new Set(selectedSchools);
    const numSelected = selectedSet.size;
    const lastUpdated = this._lastUpdated();

    const checkboxRows = schools
      .map((school) => {
        const checked = selectedSet.has(school.name);
        // Disable the last remaining checked school so min-1 is enforced
        const isLast = checked && numSelected === 1;
        const disabledAttr = isLast ? "disabled" : "";
        const title = isLast ? "At least one school must be selected" : "";
        return `
          <label class="school-checkbox-label" title="${title}">
            <input
              type="checkbox"
              class="school-checkbox"
              data-action="toggle-school"
              data-school="${sldEscapeHtml(school.name)}"
              ${checked ? "checked" : ""}
              ${disabledAttr}
            />
            <span class="school-checkbox-text">${sldEscapeHtml(school.name)}</span>
            ${isLast ? '<span class="min-one-hint">(min 1)</span>' : ""}
          </label>`;
      })
      .join("");

    const lastUpdatedHtml = lastUpdated
      ? `<div class="last-updated">Last updated: ${sldEscapeHtml(lastUpdated)}</div>`
      : "";

    return `
      <div class="settings-view">
        <div class="settings-section">
          <div class="settings-section-title">Schools</div>
          <div class="school-checkboxes">
            ${checkboxRows || '<span class="empty-msg">No schools configured.</span>'}
          </div>
        </div>
        ${lastUpdatedHtml}
      </div>`;
  }

  // ── Delegated event handling ───────────────────────────────────────

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

      if (action === "switch-tab") {
        const tab = el.dataset.tab;
        if (tab && tab !== this._activeTab) {
          this._activeTab = tab;
          // Invalidate snapshot so re-render fires
          this._lastSnapshot = null;
          this._render();
        }
      } else if (action === "prev-month" || action === "next-month") {
        const school = el.dataset.school;
        const menuId = el.dataset.menuId;
        if (school && menuId) {
          this._callRelay("fetch_month", { school, menu_id: menuId });
        }
      } else if (action === "toggle-school") {
        // Handled via "change" event listener (below) — do nothing here
        // to avoid double-fire. The change event fires reliably on checkboxes.
      }
    };

    // Cancel touch on scroll/move
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

      // CRITICAL: NEVER preventDefault on native form elements.
      // Android webviews will not open keyboards, dropdowns, or toggle checkboxes.
      const tag = el.tagName?.toLowerCase();
      const nativeEl =
        tag === "input" || tag === "select" || tag === "textarea";
      if (!nativeEl && e.cancelable) e.preventDefault();

      dispatchAction(el);
      setTimeout(() => { touchActive = false; }, 400);
    });

    // Desktop click — skip if touch just handled it
    root.addEventListener("click", (e) => {
      if (touchActive) return;
      const el = findTarget(e);
      if (el) dispatchAction(el);
    });

    // Handle checkbox changes separately — the "change" event fires reliably
    // on all platforms after the checkbox state has already toggled.
    root.addEventListener("change", (e) => {
      const el = e.target;
      if (el?.dataset?.action === "toggle-school") {
        this._onSchoolCheckboxChange();
      }
    });
  }

  /**
   * Read all checkbox states from the DOM and send a select_schools command.
   * Called on every checkbox "change" event.
   */
  _onSchoolCheckboxChange() {
    const checkboxes = this.shadowRoot?.querySelectorAll(
      '[data-action="toggle-school"]'
    );
    if (!checkboxes) return;

    const selected = [];
    checkboxes.forEach((cb) => {
      if (cb.checked) selected.push(cb.dataset.school);
    });

    if (selected.length === 0) {
      // Enforce minimum 1: this should not normally happen (last checkbox is
      // disabled), but guard defensively.
      console.warn("school-lunch-detail-card: at least 1 school must be selected");
      return;
    }

    this._callRelay("select_schools", { schools: selected });
  }

  // ── Styles ─────────────────────────────────────────────────────────

  _styles() {
    return `
      :host {
        --sld-radius: 12px;
        --sld-radius-sm: 8px;
        --sld-spacing: 16px;
        --sld-spacing-sm: 8px;
        --sld-surface: var(--card-background-color, var(--ha-card-background, #fff));
        --sld-surface-variant: var(--secondary-background-color, #f5f5f5);
        --sld-on-surface: var(--primary-text-color, #212121);
        --sld-on-surface-secondary: var(--secondary-text-color, #757575);
        --sld-primary: var(--primary-color, #03a9f4);
        --sld-primary-light: color-mix(in srgb, var(--sld-primary) 12%, transparent);
        --sld-border: var(--divider-color, #e0e0e0);
        --sld-highlight: color-mix(in srgb, var(--sld-primary) 10%, transparent);
        --sld-current-week: color-mix(in srgb, var(--sld-primary) 8%, transparent);
        --sld-today-bg: var(--sld-primary);
        --sld-today-text: #fff;
        font-size: 13px;
        color: var(--sld-on-surface);
      }

      ha-card {
        overflow: hidden;
      }

      /* ── Header ── */
      .card-header {
        padding: var(--sld-spacing) var(--sld-spacing) 0;
      }

      .header-row {
        display: flex;
        align-items: center;
        gap: var(--sld-spacing-sm);
      }

      .header-icon {
        color: var(--sld-primary);
        --mdc-icon-size: 24px;
      }

      .header-title {
        font-size: 18px;
        font-weight: 500;
        color: var(--sld-on-surface);
      }

      /* ── Tab bar ── */
      .tab-bar {
        display: flex;
        overflow-x: auto;
        border-bottom: 2px solid var(--sld-border);
        padding: 0 var(--sld-spacing);
        scrollbar-width: none;
        -webkit-overflow-scrolling: touch;
        gap: 2px;
      }

      .tab-bar::-webkit-scrollbar {
        display: none;
      }

      .tab-btn {
        flex-shrink: 0;
        background: none;
        border: none;
        border-bottom: 3px solid transparent;
        margin-bottom: -2px;
        padding: 10px 14px;
        cursor: pointer;
        font: inherit;
        font-size: 13px;
        font-weight: 500;
        color: var(--sld-on-surface-secondary);
        white-space: nowrap;
        transition: color 150ms, border-color 150ms;
      }

      .tab-btn:hover {
        color: var(--sld-on-surface);
      }

      .tab-btn.active {
        color: var(--sld-primary);
        border-bottom-color: var(--sld-primary);
      }

      /* ── Tab content ── */
      .tab-content {
        padding: var(--sld-spacing);
        overflow: auto;
        -webkit-overflow-scrolling: touch;
      }

      /* ── Empty / error messages ── */
      .empty-msg {
        color: var(--sld-on-surface-secondary);
        font-style: italic;
        padding: var(--sld-spacing) 0;
        text-align: center;
      }

      /* ── Scrollable table wrapper ── */
      .table-scroll {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
      }

      /* ── Week view table ── */
      .week-grid-wrap {
        width: 100%;
      }

      .week-table {
        width: 100%;
        border-collapse: collapse;
        table-layout: auto;
      }

      .week-table th,
      .week-table td {
        border: 1px solid var(--sld-border);
        padding: 6px 8px;
        vertical-align: top;
        text-align: left;
      }

      .week-day-header {
        font-weight: 600;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--sld-on-surface-secondary);
        background: var(--sld-surface-variant);
        white-space: nowrap;
      }

      .week-day-header.highlight-col {
        color: var(--sld-primary);
        background: var(--sld-highlight);
      }

      .school-col-header {
        background: var(--sld-surface-variant);
        min-width: 80px;
      }

      .school-label {
        font-weight: 600;
        font-size: 12px;
        color: var(--sld-on-surface);
        white-space: nowrap;
        background: var(--sld-surface-variant);
        vertical-align: middle;
      }

      .week-cell {
        font-size: 12px;
        min-width: 90px;
      }

      .week-cell.highlight-col {
        background: var(--sld-highlight);
      }

      .menu-option {
        font-size: 13px;
        line-height: 1.3;
        margin: 2px 0;
      }

      .menu-option-num {
        font-weight: 600;
        color: var(--sld-primary);
      }

      .menu-includes {
        font-size: 11px;
        font-style: italic;
        color: var(--sld-on-surface-secondary);
        margin-top: 3px;
      }

      .menu-no-menu {
        font-size: 12px;
        color: var(--sld-on-surface-secondary);
        font-style: italic;
      }

      .no-menu {
        color: var(--sld-on-surface-secondary);
        font-style: italic;
        font-size: 11px;
      }

      /* ── Month calendar ── */
      .month-view {
        width: 100%;
      }

      .month-nav {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: var(--sld-spacing-sm);
        gap: var(--sld-spacing-sm);
      }

      .month-title {
        font-size: 15px;
        font-weight: 600;
        color: var(--sld-on-surface);
        flex: 1;
        text-align: center;
      }

      .nav-arrow {
        background: none;
        border: 1px solid var(--sld-border);
        border-radius: var(--sld-radius-sm);
        cursor: pointer;
        padding: 4px 12px;
        font-size: 20px;
        color: var(--sld-on-surface);
        line-height: 1;
        transition: background 150ms;
      }

      .nav-arrow:hover:not(:disabled) {
        background: var(--sld-surface-variant);
      }

      .nav-arrow:disabled {
        opacity: 0.35;
        cursor: default;
      }

      .cal-table {
        width: 100%;
        border-collapse: collapse;
        table-layout: fixed;
      }

      .cal-table th,
      .cal-table td {
        border: 1px solid var(--sld-border);
        vertical-align: top;
        padding: 4px 6px;
      }

      .cal-day-header {
        font-weight: 600;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        color: var(--sld-on-surface-secondary);
        background: var(--sld-surface-variant);
        text-align: center;
        padding: 6px;
      }

      .cal-cell {
        font-size: 12px;
        min-height: 50px;
        min-width: 60px;
      }

      .cal-cell.empty {
        background: var(--sld-surface-variant);
        opacity: 0.4;
      }

      .cal-cell.today {
        background: var(--sld-primary-light);
      }

      .cal-day-num {
        font-size: 11px;
        font-weight: 700;
        color: var(--sld-on-surface-secondary);
        margin-bottom: 2px;
      }

      .cal-day-num.today-num {
        color: var(--sld-primary);
      }

      .cal-option {
        font-size: 11px;
        line-height: 1.3;
        margin: 1px 0;
      }

      .cal-option-num {
        font-weight: 600;
        color: var(--sld-primary);
      }

      .cal-includes {
        font-size: 10px;
        font-style: italic;
        color: var(--sld-on-surface-secondary);
        margin-top: 2px;
      }

      .cal-no-menu {
        font-size: 11px;
        color: var(--sld-on-surface-secondary);
        font-style: italic;
      }

      .day-notice {
        display: block;
        font-size: 11px;
        font-weight: 600;
        color: var(--warning-color, #ff9800);
        margin-bottom: 2px;
      }

      .cal-notice {
        display: block;
        font-size: 10px;
        font-weight: 600;
        color: var(--warning-color, #ff9800);
        margin-bottom: 2px;
      }

      /* Highlight the current week row */
      .current-week-row td {
        background: var(--sld-current-week);
      }

      /* Don't override empty cell highlight */
      .current-week-row td.empty {
        background: var(--sld-surface-variant);
        opacity: 0.4;
      }

      /* ── Settings tab ── */
      .settings-view {
        display: flex;
        flex-direction: column;
        gap: var(--sld-spacing);
      }

      .settings-section {
        border: 1px solid var(--sld-border);
        border-radius: var(--sld-radius);
        overflow: hidden;
      }

      .settings-section-title {
        font-weight: 500;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--sld-on-surface-secondary);
        background: var(--sld-surface-variant);
        padding: 10px var(--sld-spacing);
        border-bottom: 1px solid var(--sld-border);
      }

      .school-checkboxes {
        padding: var(--sld-spacing);
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .school-checkbox-label {
        display: flex;
        align-items: center;
        gap: 10px;
        cursor: pointer;
        font-size: 14px;
        color: var(--sld-on-surface);
        user-select: none;
      }

      .school-checkbox {
        width: 18px;
        height: 18px;
        accent-color: var(--sld-primary);
        flex-shrink: 0;
        cursor: pointer;
      }

      .school-checkbox:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .school-checkbox-text {
        flex: 1;
      }

      .min-one-hint {
        font-size: 11px;
        color: var(--sld-on-surface-secondary);
        font-style: italic;
      }

      .last-updated {
        font-size: 12px;
        color: var(--sld-on-surface-secondary);
        text-align: right;
        padding: var(--sld-spacing-sm) 0;
      }
    `;
  }
}

// ── Registration ────────────────────────────────────────────────────────────

customElements.define("school-lunch-detail-card", SchoolLunchDetailCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "school-lunch-detail-card",
  name: "School Lunch Detail Card",
  description:
    "Full-detail school lunch menu: weekly view, monthly calendars per school, and settings.",
  preview: true,
});
