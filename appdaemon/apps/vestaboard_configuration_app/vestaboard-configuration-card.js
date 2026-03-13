/**
 * Vestaboard Configuration Card
 *
 * Custom Lovelace card for managing Vestaboard display: frame editor,
 * library management (Messages & Art), Vestaboard+ store, and queue status.
 * Reads from sensor.vestaboard_configuration_status and calls the relay script.
 */

/* ── Character / color lookup tables ────────────────────────────────── */

const CODE_TO_CHAR = {
  0: " ", 1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F", 7: "G",
  8: "H", 9: "I", 10: "J", 11: "K", 12: "L", 13: "M", 14: "N", 15: "O",
  16: "P", 17: "Q", 18: "R", 19: "S", 20: "T", 21: "U", 22: "V", 23: "W",
  24: "X", 25: "Y", 26: "Z", 27: "1", 28: "2", 29: "3", 30: "4", 31: "5",
  32: "6", 33: "7", 34: "8", 35: "9", 36: "0",
  37: "!", 38: "@", 39: "#", 40: "$", 41: "(", 42: ")", 44: "-",
  46: "+", 47: "&", 48: "=", 49: ";", 50: ":", 52: "'", 53: '"',
  54: "%", 55: ",", 56: ".", 59: "/", 60: "?",
};

const CHAR_TO_CODE = {};
for (const [code, ch] of Object.entries(CODE_TO_CHAR)) {
  if (ch !== " ") CHAR_TO_CODE[ch] = Number(code);
}
CHAR_TO_CODE[" "] = 0;

const COLOR_MAP = {
  63: "#e74c3c", 64: "#e67e22", 65: "#f1c40f", 66: "#2ecc71",
  67: "#3498db", 68: "#9b59b6", 69: "#ecf0f1", 70: "#2d3436",
};

const COLOR_NAMES = {
  63: "Red", 64: "Orange", 65: "Yellow", 66: "Green",
  67: "Blue", 68: "Violet", 69: "White", 70: "Black",
};

/* Palette: all character codes in display order */
const PALETTE_CODES = [
  0,
  ...Array.from({ length: 26 }, (_, i) => i + 1),
  36, ...Array.from({ length: 9 }, (_, i) => i + 27),
  37, 38, 39, 40, 41, 42, 44, 46, 47, 48, 49, 50, 52, 53, 54, 55, 56, 59, 60,
  63, 64, 65, 66, 67, 68, 69, 70,
];

const VBC_ROWS = 6;
const VBC_COLS = 22;

/* ── Utility functions ──────────────────────────────────────────────── */

function vbcRelativeTime(isoStr) {
  if (!isoStr) return "\u2014";
  const date = new Date(isoStr);
  if (isNaN(date.getTime())) return isoStr;
  const now = Date.now();
  const diffMs = now - date.getTime();
  const absDiff = Math.abs(diffMs);
  const future = diffMs < 0;
  const seconds = Math.floor(absDiff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (seconds < 60) return future ? `in ${seconds}s` : `${seconds}s ago`;
  if (minutes < 60) return future ? `in ${minutes}m` : `${minutes}m ago`;
  if (hours < 24) return future ? `in ${hours}h ${minutes % 60}m` : `${hours}h ${minutes % 60}m ago`;
  return date.toLocaleString();
}

function vbcCountdown(isoOrTimestamp) {
  if (!isoOrTimestamp) return null;
  let targetMs;
  if (typeof isoOrTimestamp === "number") {
    // Unix timestamp in seconds
    targetMs = isoOrTimestamp * 1000;
  } else {
    const date = new Date(isoOrTimestamp);
    if (isNaN(date.getTime())) return null;
    targetMs = date.getTime();
  }
  const remaining = targetMs - Date.now();
  if (remaining <= 0) return "expired";
  const s = Math.floor(remaining / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function vbcEmptyGrid() {
  return Array.from({ length: VBC_ROWS }, () => Array(VBC_COLS).fill(0));
}

function vbcCloneGrid(grid) {
  if (!grid) return vbcEmptyGrid();
  return grid.map((row) => [...row]);
}

/**
 * Parse a grid value that may be a JSON string (to avoid HA stripping zeros
 * from nested integer arrays) or already a parsed array.
 */
function vbcParseGrid(value) {
  if (typeof value === "string") {
    try { return JSON.parse(value); } catch (_) { return null; }
  }
  return value;
}

function vbcNormalizeGrid(grid) {
  const normalized = vbcEmptyGrid();
  grid = vbcParseGrid(grid);
  if (!Array.isArray(grid)) return normalized;

  for (let r = 0; r < VBC_ROWS; r++) {
    const row = grid[r];
    if (!Array.isArray(row)) continue;
    for (let c = 0; c < VBC_COLS; c++) {
      const code = Number(row[c]);
      normalized[r][c] = Number.isFinite(code) ? code : 0;
    }
  }

  return normalized;
}

/* ── Main card class ────────────────────────────────────────────────── */

class VestaboardConfigurationCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._lastSnapshot = null;
    this._delegatedBound = false;

    // Tab state
    this._activeTab = "editor";

    // Editor state
    this._editorGrid = vbcEmptyGrid();
    this._selectedPaletteCode = 0;
    this._editorMode = "paint";
    this._textInput = "";
    this._borderColor = null;
    this._isPainting = false;
    this._editorCreator = "";
    this._editorName = "";
    this._editorRating = 0;
    this._editorTtlMinutes = 30;
    this._editorShouldExpire = true;
    this._editingFrameId = null;

    // Library state
    this._libraryPage = 0;
    this._librarySort = "newest";
    this._libraryFilterCreator = "All";
    this._libraryFilterRating = 0;
    this._librarySubTab = "messages"; // "messages" or "art"

    // Automation / Store state (pending edits keyed by automation_id)
    this._automationEdits = {};
    this._artSubject = "";
    this._expandedAutoId = null; // which product card config is expanded
    this._storeSavedFeedback = {}; // {autoId: timestamp} for "Saved!" flash

    // Queue
    this._queueExpanded = false;

    // Countdown timer
    this._countdownInterval = null;

    // Delete confirmation
    this._confirmDeleteId = null;

    // AI art result (stored locally until pushed/saved)
    this._artResultFrame = null;
    this._artGenerating = false;
  }

  /* ── HA lifecycle ───────────────────────────────────────────────── */

  setConfig(config) {
    if (!config || !config.status_entity || !config.relay_script) {
      throw new Error("Requires status_entity and relay_script");
    }
    this._config = config;
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;
    if (firstSet) {
      this._lastSnapshot = this._sensorSnapshot();
      this._loadArtResult();
      this._render();
      this._startCountdown();
      return;
    }
    const snap = this._sensorSnapshot();
    if (snap === this._lastSnapshot) return;
    this._lastSnapshot = snap;
    // Clear optimistic automation edits when backend data refreshes
    this._automationEdits = {};
    this._loadArtResult();
    this._render();
  }

  connectedCallback() {
    this._startCountdown();
  }

  disconnectedCallback() {
    this._stopCountdown();
  }

  getCardSize() {
    return 12;
  }

  /* ── State snapshot ─────────────────────────────────────────────── */

  _sensorSnapshot() {
    const s = this._hass?.states?.[this._config.status_entity];
    if (!s) return null;
    const a = s.attributes || {};
    return `${s.last_updated}|${a.status}|${a.current_source}|${a.current_ttl_expires}|${a.generated_art_frame ? "art" : ""}`;
  }

  _sensorState() {
    if (!this._hass) return null;
    const s = this._hass.states[this._config.status_entity];
    if (!s || s.state === "unavailable" || s.state === "unknown") return null;
    return s;
  }

  _sensorAttr(attr, fallback) {
    const s = this._sensorState();
    if (!s || !s.attributes) return fallback;
    const v = s.attributes[attr];
    return v !== undefined ? v : fallback;
  }

  _parseJsonAttr(attr, fallback) {
    const raw = this._sensorAttr(attr, null);
    if (raw === null || raw === undefined) return fallback;
    if (Array.isArray(raw) || typeof raw === "object") return raw;
    try {
      return JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  _loadArtResult() {
    const artFrame = this._sensorAttr("generated_art_frame", null);
    if (artFrame && Array.isArray(artFrame) && artFrame.length === VBC_ROWS) {
      this._artResultFrame = artFrame;
      this._artGenerating = false;
    }
  }

  /* ── Relay ──────────────────────────────────────────────────────── */

  _callRelay(command, data) {
    if (!this._hass) return;
    this._hass
      .callService("script", this._config.relay_script, {
        command,
        payload: JSON.stringify(data || {}),
      })
      .catch((err) => {
        console.warn("vestaboard-config-card: relay failed", command, err);
      });
  }

  /* ── Countdown timer ────────────────────────────────────────────── */

  _startCountdown() {
    if (this._countdownInterval) return;
    this._countdownInterval = setInterval(() => {
      this._updateCountdowns();
    }, 1000);
  }

  _stopCountdown() {
    if (this._countdownInterval) {
      clearInterval(this._countdownInterval);
      this._countdownInterval = null;
    }
  }

  _updateCountdowns() {
    const root = this.shadowRoot;
    if (!root) return;

    // Update TTL countdown in board preview
    const ttlEl = root.querySelector(".ttl-countdown");
    if (ttlEl) {
      const expires = this._sensorAttr("current_ttl_expires", null);
      ttlEl.textContent = expires ? `TTL: ${vbcCountdown(expires) || "expired"}` : "";
    }

    // Update queue countdowns
    const queueItems = root.querySelectorAll("[data-queue-expiry]");
    queueItems.forEach((el) => {
      const expiry = el.getAttribute("data-queue-expiry");
      el.textContent = expiry ? (vbcCountdown(expiry) || "expired") : "no expiry";
    });

    // Update upcoming automation countdowns
    const upcomingItems = root.querySelectorAll("[data-upcoming-fire]");
    upcomingItems.forEach((el) => {
      const fireTime = parseFloat(el.getAttribute("data-upcoming-fire"));
      if (!isNaN(fireTime)) {
        el.textContent = vbcCountdown(fireTime) || "now";
      }
    });
  }

  /* ── Escape HTML ────────────────────────────────────────────────── */

  _esc(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  /* ── Grid rendering (shared) ────────────────────────────────────── */

  _renderGridHTML(grid, cellSize, gap, radius, interactive, idPrefix) {
    if (!grid || !grid.length) return '<div class="grid-empty">No frame data</div>';

    // Editor grid: full-width responsive (1fr columns, aspect-ratio cells)
    // Preview/mini grids: fixed pixel cells, inline-grid for centering
    const isEditor = interactive || idPrefix === "editor";
    const normalizedGrid = vbcNormalizeGrid(grid);

    const colTemplate = isEditor
      ? `repeat(${VBC_COLS},1fr)`
      : `repeat(${VBC_COLS},${cellSize}px)`;
    const rowTemplate = isEditor
      ? ""
      : `grid-template-rows:repeat(${VBC_ROWS},${cellSize}px);`;
    const cellSizeStyle = isEditor
      ? ""
      : `width:${cellSize}px;height:${cellSize}px;`;
    const gridClass = isEditor ? "vb-grid vb-grid-editor" : "vb-grid";

    const cells = [];
    for (let r = 0; r < VBC_ROWS; r++) {
      for (let c = 0; c < VBC_COLS; c++) {
        const code = normalizedGrid[r][c] || 0;
        const isColor = COLOR_MAP[code];
        const bg = isColor || (code === 0 ? "#000" : "rgba(0,0,0,0.22)");
        const ch = (isColor || code === 0) ? "" : (CODE_TO_CHAR[code] || "");
        const interactiveAttr = interactive
          ? ` data-action="grid-cell" data-row="${r}" data-col="${c}"`
          : "";
        const id = idPrefix ? ` id="${idPrefix}-${r}-${c}"` : "";
        cells.push(
          `<div class="vb-cell"${id}${interactiveAttr} style="${cellSizeStyle}border-radius:${radius}px;background:${bg};box-shadow:inset 0 0 0 1px rgba(255,255,255,0.08);">${this._esc(ch)}</div>`
        );
      }
    }

    return `<div class="${gridClass}" style="grid-template-columns:${colTemplate};${rowTemplate}gap:${gap}px;">${cells.join("")}</div>`;
  }

  /* ── Text mode helpers ──────────────────────────────────────────── */

  _textToGrid(text, borderCode) {
    const grid = vbcEmptyGrid();
    const hasBorder = borderCode !== null && borderCode !== undefined;

    // Apply border
    if (hasBorder) {
      for (let c = 0; c < VBC_COLS; c++) {
        grid[0][c] = borderCode;
        grid[5][c] = borderCode;
      }
      for (let r = 0; r < VBC_ROWS; r++) {
        grid[r][0] = borderCode;
        grid[r][21] = borderCode;
      }
    }

    // Wrap text into inner area
    const upper = text.toUpperCase();
    const innerWidth = hasBorder ? 20 : 22;
    const innerRows = hasBorder ? 4 : 6;
    const startRow = hasBorder ? 1 : 0;
    const startCol = hasBorder ? 1 : 0;

    const lines = this._wrapText(upper, innerRows, innerWidth);
    const centered = this._centerLines(lines, innerRows, innerWidth);

    for (let r = 0; r < innerRows; r++) {
      const line = centered[r] || "";
      for (let c = 0; c < innerWidth; c++) {
        const ch = line[c] || " ";
        grid[startRow + r][startCol + c] = CHAR_TO_CODE[ch] !== undefined ? CHAR_TO_CODE[ch] : 0;
      }
    }

    return grid;
  }

  _wrapText(text, maxLines, width) {
    if (!text) return [];
    const words = text.split(" ");
    const lines = [];
    let current = "";

    for (const word of words) {
      if (lines.length >= maxLines) break;
      if (!word) continue;

      if (word.length > width) {
        if (current) {
          lines.push(current);
          current = "";
          if (lines.length >= maxLines) break;
        }
        for (let i = 0; i < word.length; i += width) {
          if (lines.length >= maxLines) break;
          lines.push(word.slice(i, i + width));
        }
        continue;
      }

      const candidate = current ? `${current} ${word}` : word;
      if (candidate.length <= width) {
        current = candidate;
      } else {
        if (current) lines.push(current);
        current = word;
      }
    }

    if (current && lines.length < maxLines) lines.push(current);
    return lines.slice(0, maxLines);
  }

  _centerLines(lines, totalLines, width) {
    const out = Array(totalLines).fill("");
    const n = Math.min(lines.length, totalLines);
    const top = n < totalLines ? Math.floor((totalLines - n) / 2) : 0;
    for (let i = 0; i < n; i++) out[top + i] = lines[i];

    return out.map((line) => {
      const clipped = line.slice(0, width);
      const pad = width - clipped.length;
      const left = Math.floor(pad / 2);
      const right = pad - left;
      return `${" ".repeat(left)}${clipped}${" ".repeat(right)}`;
    });
  }

  /* ── Library data helpers ───────────────────────────────────────── */

  _getLibraryFrames(category) {
    const raw = this._parseJsonAttr("library", []);
    let frames = Array.isArray(raw) ? raw : [];

    // Filter by category (sub-tab)
    if (category) {
      frames = frames.filter((f) => (f.category || "message") === category);
    }

    // Filter by creator
    if (this._libraryFilterCreator !== "All") {
      frames = frames.filter((f) => f.creator === this._libraryFilterCreator);
    }
    if (this._libraryFilterRating > 0) {
      frames = frames.filter((f) => (f.rating || 0) >= this._libraryFilterRating);
    }

    // Sort
    switch (this._librarySort) {
      case "oldest":
        frames.sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
        break;
      case "highest_rated":
        frames.sort((a, b) => (b.rating || 0) - (a.rating || 0));
        break;
      case "by_creator":
        frames.sort((a, b) => (a.creator || "").localeCompare(b.creator || ""));
        break;
      case "newest":
      default:
        frames.sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
        break;
    }

    return frames;
  }

  /* ── Full render ────────────────────────────────────────────────── */

  _render() {
    // Focus guard: skip render while user is typing in a text field.
    // Checkboxes are excluded so that toggling one immediately
    // shows/hides dependent inputs without waiting for the next hass update.
    const active = this.shadowRoot?.activeElement;
    if (active && (
      (active.tagName === "INPUT" && active.type !== "checkbox") ||
      active.tagName === "TEXTAREA" ||
      active.tagName === "SELECT"
    )) {
      return;
    }

    const currentFrame = this._sensorAttr("current_frame", null);
    const currentSource = this._sensorAttr("current_source", "\u2014");
    const currentTtlExpires = this._sensorAttr("current_ttl_expires", null);
    const status = this._sensorAttr("status", "unknown");
    const creators = this._sensorAttr("creators", []);
    const cellSize = this._config.cell_size || 18;
    const gap = this._config.gap || 2;
    const radius = this._config.radius || 3;

    const statusColor = status === "ok"
      ? "var(--vbc-success, var(--success-color, #4caf50))"
      : "var(--vbc-error, var(--error-color, #f44336))";

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="card-header">
          <div class="header-row">
            <ha-icon icon="mdi:view-dashboard-variant" class="header-icon"></ha-icon>
            <span class="header-title">Vestaboard Configuration</span>
            <span class="status-dot" style="background:${statusColor};" title="${this._esc(status)}"></span>
          </div>
        </div>
        <div class="card-content">

          <!-- Board Preview -->
          <div class="board-preview">
            ${this._renderGridHTML(currentFrame, cellSize, gap, radius, false, null)}
            <div class="preview-info">
              <span class="source-label">Source: ${this._esc(currentSource)}</span>
              <span class="ttl-countdown">${currentTtlExpires ? `TTL: ${vbcCountdown(currentTtlExpires) || "expired"}` : ""}</span>
            </div>
          </div>

          <!-- Tab bar -->
          <div class="tab-bar">
            <button class="tab-btn ${this._activeTab === "editor" ? "active" : ""}" data-action="tab" data-tab="editor">Editor</button>
            <button class="tab-btn ${this._activeTab === "library" ? "active" : ""}" data-action="tab" data-tab="library">Library</button>
            <button class="tab-btn ${this._activeTab === "store" ? "active" : ""}" data-action="tab" data-tab="store">Vestaboard+</button>
          </div>

          <!-- Tab content -->
          <div class="tab-content">
            ${this._activeTab === "editor" ? this._renderEditorTab(creators) : ""}
            ${this._activeTab === "library" ? this._renderLibraryTab(creators) : ""}
            ${this._activeTab === "store" ? this._renderStorePage() : ""}
          </div>

          <!-- Queue Status -->
          ${this._renderQueueSection()}
        </div>
      </ha-card>
    `;

    this._ensureDelegatedListeners();
  }

  /* ── Editor tab ─────────────────────────────────────────────────── */

  _renderEditorTab(creators) {
    const cellSize = this._config.editor_cell_size || this._config.cell_size || 18;
    const gap = this._config.gap || 2;
    const radius = this._config.radius || 3;

    // In text mode, compute the preview grid from text input
    let displayGrid = this._editorGrid;
    if (this._editorMode === "text") {
      displayGrid = this._textToGrid(this._textInput, this._borderColor);
    }

    const modeToggle = `
      <div class="mode-toggle">
        <button class="mode-btn ${this._editorMode === "paint" ? "active" : ""}" data-action="set-mode" data-mode="paint">Paint</button>
        <button class="mode-btn ${this._editorMode === "text" ? "active" : ""}" data-action="set-mode" data-mode="text">Text</button>
      </div>
    `;

    let modeContent;
    if (this._editorMode === "paint") {
      modeContent = this._renderPaintMode();
    } else {
      modeContent = this._renderTextMode();
    }

    const creatorOptions = (creators || [])
      .map((c) => `<option value="${this._esc(c)}" ${c === this._editorCreator ? "selected" : ""}>${this._esc(c)}</option>`)
      .join("");

    const stars = Array.from({ length: 5 }, (_, i) => {
      const filled = i < this._editorRating;
      return `<span class="star ${filled ? "filled" : ""}" data-action="set-rating" data-rating="${i + 1}">${filled ? "\u2605" : "\u2606"}</span>`;
    }).join("");

    return `
      <div class="editor-section">
        ${modeToggle}

        <div class="editor-grid-wrap">
          ${this._renderGridHTML(displayGrid, cellSize, gap, radius, this._editorMode === "paint", "editor")}
        </div>

        ${modeContent}

        <!-- Save section -->
        <div class="save-section">
          <div class="save-row">
            <label class="field-label">Creator</label>
            <select class="vbc-select" data-action="set-creator">${creatorOptions}</select>
          </div>
          <div class="save-row">
            <label class="field-label">Name</label>
            <input type="text" class="vbc-input" data-action="set-name" value="${this._esc(this._editorName)}" placeholder="Frame name" maxlength="100">
          </div>
          <div class="save-row">
            <label class="field-label">Rating</label>
            <div class="star-rating">${stars}</div>
          </div>
          <div class="save-row ttl-row">
            <div class="ttl-field">
              <label class="field-label">TTL</label>
              <input type="number" class="vbc-input vbc-input-sm" data-action="set-ttl-minutes" value="${this._editorTtlMinutes}" min="1" max="1440">
              <span class="ttl-unit">min</span>
            </div>
            <label class="checkbox-label">
              <input type="checkbox" data-action="toggle-should-expire" ${this._editorShouldExpire ? "checked" : ""}>
              Should Expire
            </label>
          </div>
          <div class="save-actions">
            <button class="vbc-btn vbc-btn-secondary" data-action="save-to-library">${this._editingFrameId ? "Update in Library" : "Save to Library"}</button>
            <button class="vbc-btn vbc-btn-primary" data-action="push-to-board">Push to Board</button>
          </div>
          ${this._editingFrameId ? `<button class="vbc-btn vbc-btn-text" data-action="clear-edit">Cancel Edit</button>` : ""}
        </div>
      </div>
    `;
  }

  _renderPaintMode() {
    const paletteCells = PALETTE_CODES.map((code) => {
      const isColor = COLOR_MAP[code];
      const bg = isColor || "rgba(0,0,0,0.22)";
      const ch = isColor ? "" : (CODE_TO_CHAR[code] || "");
      const selected = code === this._selectedPaletteCode;
      const label = isColor ? COLOR_NAMES[code] : (ch === " " ? "Space" : ch);
      return `<div class="palette-cell ${selected ? "selected" : ""}" data-action="select-palette" data-code="${code}" style="background:${bg};" title="${label}">${this._esc(ch)}</div>`;
    }).join("");

    return `
      <div class="palette-section">
        <div class="palette-strip">${paletteCells}</div>
        ${COLOR_MAP[this._selectedPaletteCode] ? `<button class="vbc-btn vbc-btn-secondary vbc-btn-sm" data-action="apply-border">Apply Border</button>` : ""}
        <button class="vbc-btn vbc-btn-text vbc-btn-sm" data-action="clear-grid">Clear Grid</button>
      </div>
    `;
  }

  _renderTextMode() {
    const borderOptions = [
      `<option value="" ${this._borderColor === null ? "selected" : ""}>None</option>`,
      ...Object.entries(COLOR_NAMES).map(
        ([code, name]) =>
          `<option value="${code}" ${this._borderColor === Number(code) ? "selected" : ""}>${name}</option>`
      ),
    ].join("");

    return `
      <div class="text-mode-section">
        <textarea class="vbc-textarea" data-action="set-text" maxlength="80" placeholder="Type your message (max 80 chars)...">${this._esc(this._textInput)}</textarea>
        <div class="text-mode-row">
          <label class="field-label">Border</label>
          <select class="vbc-select" data-action="set-border">${borderOptions}</select>
        </div>
        <div class="char-count">${this._textInput.length}/80</div>
      </div>
    `;
  }

  /* ── Library tab (Messages / Art sub-tabs) ────────────────────── */

  _renderLibraryTab(creators) {
    const category = this._librarySubTab === "art" ? "art" : "message";
    const frames = this._getLibraryFrames(category);
    const perPage = 12;
    const totalPages = Math.max(1, Math.ceil(frames.length / perPage));
    const page = Math.min(this._libraryPage, totalPages - 1);
    const pageFrames = frames.slice(page * perPage, (page + 1) * perPage);

    const sortOptions = [
      ["newest", "Newest"], ["oldest", "Oldest"],
      ["highest_rated", "Highest Rated"], ["by_creator", "By Creator"],
    ]
      .map(([v, l]) => `<option value="${v}" ${this._librarySort === v ? "selected" : ""}>${l}</option>`)
      .join("");

    const creatorOptions = [
      `<option value="All" ${this._libraryFilterCreator === "All" ? "selected" : ""}>All Creators</option>`,
      ...(creators || []).map(
        (c) => `<option value="${this._esc(c)}" ${this._libraryFilterCreator === c ? "selected" : ""}>${this._esc(c)}</option>`
      ),
    ].join("");

    const ratingOptions = Array.from({ length: 6 }, (_, i) =>
      `<option value="${i}" ${this._libraryFilterRating === i ? "selected" : ""}>${i === 0 ? "Any Rating" : "\u2605".repeat(i) + "+"}</option>`
    ).join("");

    const frameCards = pageFrames.map((f) => this._renderLibraryFrame(f)).join("");

    return `
      <div class="library-section">
        <!-- Sub-tabs for Messages / Art -->
        <div class="sub-tab-bar">
          <button class="sub-tab-btn ${this._librarySubTab === "messages" ? "active" : ""}" data-action="lib-subtab" data-subtab="messages">Messages</button>
          <button class="sub-tab-btn ${this._librarySubTab === "art" ? "active" : ""}" data-action="lib-subtab" data-subtab="art">Art</button>
        </div>

        <div class="library-controls">
          <select class="vbc-select vbc-select-sm" data-action="lib-sort">${sortOptions}</select>
          <select class="vbc-select vbc-select-sm" data-action="lib-filter-creator">${creatorOptions}</select>
          <select class="vbc-select vbc-select-sm" data-action="lib-filter-rating">${ratingOptions}</select>
        </div>

        <div class="library-grid">
          ${frameCards || `<div class="library-empty">No ${category === "art" ? "art" : "messages"} in library</div>`}
        </div>

        ${totalPages > 1 ? `
        <div class="pagination">
          <button class="vbc-btn vbc-btn-text vbc-btn-sm" data-action="lib-prev" ${page <= 0 ? "disabled" : ""}>Prev</button>
          <span class="page-info">${page + 1} / ${totalPages}</span>
          <button class="vbc-btn vbc-btn-text vbc-btn-sm" data-action="lib-next" ${page >= totalPages - 1 ? "disabled" : ""}>Next</button>
        </div>` : ""}
      </div>
    `;
  }

  _renderLibraryFrame(frame) {
    const grid = frame.characters;
    const miniCellSize = 3;
    const miniGap = 1;
    const miniRadius = 1;
    const stars = "\u2605".repeat(frame.rating || 0) + "\u2606".repeat(5 - (frame.rating || 0));
    const isConfirming = this._confirmDeleteId === frame.frame_id;

    return `
      <div class="lib-frame-card">
        <div class="lib-frame-preview">
          ${this._renderGridHTML(grid, miniCellSize, miniGap, miniRadius, false, null)}
        </div>
        <div class="lib-frame-info">
          <span class="lib-frame-name" title="${this._esc(frame.name || "")}">${this._esc(frame.name || "Untitled")}</span>
          <span class="lib-frame-meta">${this._esc(frame.creator || "\u2014")} &middot; ${vbcRelativeTime(frame.created_at)}</span>
          <span class="lib-frame-stars">${stars}</span>
        </div>
        <div class="lib-frame-actions">
          <button class="vbc-btn vbc-btn-text vbc-btn-xs" data-action="lib-view" data-frame-id="${this._esc(frame.frame_id)}" title="View">View</button>
          <button class="vbc-btn vbc-btn-text vbc-btn-xs" data-action="lib-edit" data-frame-id="${this._esc(frame.frame_id)}" title="Edit">Edit</button>
          <button class="vbc-btn vbc-btn-text vbc-btn-xs" data-action="lib-push" data-frame-id="${this._esc(frame.frame_id)}" title="Push">Push</button>
          ${isConfirming
            ? `<button class="vbc-btn vbc-btn-danger vbc-btn-xs" data-action="lib-delete-confirm" data-frame-id="${this._esc(frame.frame_id)}">Confirm</button>
               <button class="vbc-btn vbc-btn-text vbc-btn-xs" data-action="lib-delete-cancel">Cancel</button>`
            : `<button class="vbc-btn vbc-btn-text vbc-btn-xs" data-action="lib-delete" data-frame-id="${this._esc(frame.frame_id)}" title="Delete">Delete</button>`
          }
        </div>
      </div>
    `;
  }

  /* ── Vestaboard+ Store tab ─────────────────────────────────────── */

  _renderStorePage() {
    const automations = this._sensorAttr("automations", []);
    const autoList = Array.isArray(automations) ? automations : [];

    const productCards = autoList.map((auto) => this._renderProductCard(auto)).join("");

    // AI Art Generator section
    const artResultHTML = this._artResultFrame ? `
      <div class="ai-art-result">
        <div class="ai-art-preview">
          ${this._renderGridHTML(this._artResultFrame, 5, 1, 2, false, null)}
        </div>
        <div class="ai-art-result-actions">
          <button class="vbc-btn vbc-btn-secondary vbc-btn-sm" data-action="art-push">Push to Board</button>
          <button class="vbc-btn vbc-btn-secondary vbc-btn-sm" data-action="art-edit">Edit in Editor</button>
          <button class="vbc-btn vbc-btn-text vbc-btn-sm" data-action="art-save-library">
            <span class="star filled">\u2605</span> Save to Library
          </button>
        </div>
      </div>
    ` : "";

    const generatingHTML = this._artGenerating
      ? '<div class="ai-art-generating">Generating... please wait</div>'
      : "";

    return `
      <div class="store-section">
        <div class="store-header">
          <ha-icon icon="mdi:store" style="--mdc-icon-size:22px;color:var(--vbc-primary);"></ha-icon>
          <span class="store-title">Vestaboard+ Store</span>
        </div>
        <p class="store-subtitle">Install automations to keep your board fresh with content.</p>

        <div class="store-grid">
          ${productCards || '<div class="store-empty">No automations available</div>'}
        </div>

        <!-- AI Art Generator -->
        <div class="ai-art-section">
          <div class="section-header-inline">
            <ha-icon icon="mdi:palette" style="--mdc-icon-size:18px;color:var(--vbc-primary);"></ha-icon>
            <span>AI Art Generator</span>
          </div>
          <div class="ai-art-controls">
            <input type="text" class="vbc-input" data-action="set-art-subject" value="${this._esc(this._artSubject)}" placeholder="Describe your art subject...">
            <button class="vbc-btn vbc-btn-primary vbc-btn-sm" data-action="generate-art" ${this._artGenerating ? "disabled" : ""}>Generate</button>
          </div>
          ${generatingHTML}
          ${artResultHTML}
        </div>
      </div>
    `;
  }

  _renderProductCard(auto) {
    const edits = this._automationEdits[auto.id] || {};
    const enabled = edits.enabled !== undefined ? edits.enabled : auto.enabled;
    const isExpanded = this._expandedAutoId === auto.id;
    const dotColor = enabled ? "var(--vbc-success, #4caf50)" : "var(--vbc-muted, #9e9e9e)";
    const hasSavedFeedback = this._storeSavedFeedback[auto.id] && (Date.now() - this._storeSavedFeedback[auto.id] < 2000);

    // Preview grid
    const previewGrid = auto.preview_frame || null;
    const previewHTML = previewGrid
      ? `<div class="product-preview">${this._renderGridHTML(previewGrid, 4, 1, 1, false, null)}</div>`
      : `<div class="product-preview product-preview-empty"><ha-icon icon="mdi:image-off-outline" style="--mdc-icon-size:32px;color:var(--vbc-muted);"></ha-icon></div>`;

    // Config section
    let configHTML = "";
    if (isExpanded) {
      configHTML = this._renderAutoConfig(auto);
    }

    return `
      <div class="product-card">
        ${previewHTML}
        <div class="product-body">
          <div class="product-info">
            <div class="product-name-row">
              <span class="auto-dot" style="background:${dotColor};"></span>
              <span class="product-name">${this._esc(auto.name || auto.id)}</span>
            </div>
            <span class="product-desc">${this._esc(this._autoDescription(auto.id))}</span>
          </div>
          <div class="product-actions">
            <button class="vbc-btn ${enabled ? "vbc-btn-installed" : "vbc-btn-primary"} vbc-btn-sm" data-action="store-toggle" data-auto-id="${this._esc(auto.id)}">
              ${enabled ? "Installed" : "Install"}
            </button>
            <button class="vbc-btn vbc-btn-text vbc-btn-sm" data-action="store-expand" data-auto-id="${this._esc(auto.id)}">
              ${isExpanded ? "Hide Config" : "Configure"}
            </button>
          </div>
          ${configHTML}
          ${hasSavedFeedback ? '<div class="saved-flash">Saved!</div>' : ""}
        </div>
      </div>
    `;
  }

  _autoDescription(autoId) {
    const descriptions = {
      calendar_clock: "Shows date, time, and upcoming calendar events on your board.",
      messages_from_library: "Randomly displays starred messages from your library.",
      random_message: "Randomly displays starred messages from your library.",
      art_from_library: "Randomly displays starred art from your library.",
      random_art: "Randomly displays starred art from your library.",
      art_generated_by_ai: "Uses AI to create unique pixel art for your board.",
      ai_art_generator: "Uses AI to create unique pixel art for your board.",
      calendar_summary: "Shows upcoming calendar events as a summary on your board.",
    };
    return descriptions[autoId] || "A Vestaboard automation.";
  }

  _renderAutoConfig(auto) {
    const schema = auto.config_schema || {};
    const config = auto.config || {};
    const edits = this._automationEdits[auto.id] || {};
    const schemaKeys = Object.keys(schema).filter((k) => k !== "enabled");

    if (schemaKeys.length === 0) {
      return '<div class="auto-config-section"><span class="auto-config-empty">No configurable options</span></div>';
    }

    const fields = schemaKeys.map((key) => {
      const field = schema[key];
      const currentValue = edits[key] !== undefined ? edits[key] : (config[key] !== undefined ? config[key] : field.default);
      const label = field.label || key;

      if (field.type === "bool") {
        return `
          <div class="config-field">
            <label class="checkbox-label config-checkbox">
              <input type="checkbox" data-action="store-config-field" data-auto-id="${this._esc(auto.id)}" data-field="${this._esc(key)}" data-field-type="bool" ${currentValue ? "checked" : ""}>
              ${this._esc(label)}
            </label>
          </div>
        `;
      }

      // number / int
      const min = field.min !== undefined ? `min="${field.min}"` : "";
      const max = field.max !== undefined ? `max="${field.max}"` : "";
      return `
        <div class="config-field">
          <label class="config-label">${this._esc(label)}</label>
          <input type="number" class="vbc-input vbc-input-sm" data-action="store-config-field" data-auto-id="${this._esc(auto.id)}" data-field="${this._esc(key)}" data-field-type="number" value="${currentValue !== null && currentValue !== undefined ? currentValue : ""}" ${min} ${max}>
        </div>
      `;
    }).join("");

    const dirty = Object.keys(edits).filter((k) => k !== "enabled").length > 0;

    return `
      <div class="auto-config-section">
        ${fields}
        ${dirty ? `<button class="vbc-btn vbc-btn-primary vbc-btn-sm" data-action="store-save-config" data-auto-id="${this._esc(auto.id)}">Save Config</button>` : ""}
      </div>
    `;
  }

  /* ── Queue section ──────────────────────────────────────────────── */

  _renderQueueSection() {
    const queue = this._sensorAttr("queue", []);
    const fallback = this._sensorAttr("fallback_source", null);
    const currentSource = this._sensorAttr("current_source", "\u2014");
    const currentTtlExpires = this._sensorAttr("current_ttl_expires", null);
    const chevron = this._queueExpanded ? "mdi:chevron-up" : "mdi:chevron-down";
    const queueList = Array.isArray(queue) ? queue : [];

    // Upcoming automations
    const automations = this._sensorAttr("automations", []);
    const upcomingAutos = (Array.isArray(automations) ? automations : [])
      .filter((a) => a.enabled && a.next_fire_time && a.next_fire_time > (Date.now() / 1000));

    let content = "";
    if (this._queueExpanded) {
      const queueItems = queueList.map((item) => `
        <div class="queue-item">
          <span class="queue-source">${this._esc(item.source || "\u2014")}</span>
          <span class="queue-countdown" data-queue-expiry="${this._esc(item.expires_at || "")}">${item.expires_at ? (vbcCountdown(item.expires_at) || "expired") : "no expiry"}</span>
        </div>
      `).join("");

      const upcomingItems = upcomingAutos.map((a) => `
        <div class="queue-item upcoming-item">
          <span class="queue-source">${this._esc(a.name || a.id)}</span>
          <span class="queue-countdown upcoming-countdown" data-upcoming-fire="${a.next_fire_time}">${vbcCountdown(a.next_fire_time) || "now"}</span>
        </div>
      `).join("");

      content = `
        <div class="queue-body">
          <div class="queue-current">
            <span class="queue-label">Current:</span>
            <span class="queue-value">${this._esc(currentSource)}</span>
            ${currentTtlExpires ? `<span class="queue-countdown ttl-countdown">TTL: ${vbcCountdown(currentTtlExpires) || "expired"}</span>` : ""}
          </div>
          ${queueList.length > 0 ? `
          <div class="queue-pending">
            <span class="queue-label">Pending (${queueList.length}):</span>
            ${queueItems}
          </div>` : ""}
          ${upcomingAutos.length > 0 ? `
          <div class="queue-upcoming">
            <span class="queue-label">Upcoming:</span>
            ${upcomingItems}
          </div>` : ""}
          <div class="queue-fallback">
            <span class="queue-label">Fallback:</span>
            <span class="queue-value">${this._esc(fallback || "none")}</span>
          </div>
        </div>
      `;
    }

    const totalCount = queueList.length + upcomingAutos.length;

    return `
      <div class="queue-section">
        <button class="queue-header" data-action="toggle-queue">
          <span>Queue Status (${totalCount})</span>
          <ha-icon icon="${chevron}" style="--mdc-icon-size:18px;"></ha-icon>
        </button>
        ${content}
      </div>
    `;
  }

  /* ── Delegated event handling ───────────────────────────────────── */

  _ensureDelegatedListeners() {
    if (this._delegatedBound) return;
    this._delegatedBound = true;

    const root = this.shadowRoot;
    let touchActive = false;
    let touchCancelled = false;
    let touchStartTarget = null;

    const findTarget = (e) => {
      for (const el of e.composedPath()) {
        if (el instanceof Element && el.dataset?.action) return el;
      }
      return null;
    };

    ["touchcancel", "scroll"].forEach((evt) => {
      root.addEventListener(evt, () => { touchCancelled = true; }, { passive: true });
    });

    root.addEventListener("touchstart", (e) => {
      touchActive = true;
      touchCancelled = false;
      touchStartTarget = findTarget(e);
    }, { passive: true });

    root.addEventListener("touchend", (e) => {
      this._isPainting = false;
      const touch = e.changedTouches?.[0];
      const pointTarget = touch ? root.elementFromPoint(touch.clientX, touch.clientY) : null;
      const el = (pointTarget instanceof Element && pointTarget.dataset?.action)
        ? pointTarget
        : findTarget(e) || touchStartTarget;
      if (touchCancelled || !el) { touchActive = false; return; }

      if (this._editorMode === "paint" && el.dataset.action === "grid-cell") {
        this._paintCell(el);
      } else {
        this._dispatchAction(el);
      }
      touchStartTarget = null;
      setTimeout(() => { touchActive = false; }, 400);
    });

    root.addEventListener("touchmove", (e) => {
      if (this._isPainting && this._editorMode === "paint") {
        const touch = e.touches[0];
        if (touch) {
          const target = root.elementFromPoint(touch.clientX, touch.clientY);
          if (target && target.dataset?.action === "grid-cell") {
            this._paintCell(target);
            return;
          }
        }
      }
      touchCancelled = true;
    }, { passive: true });

    // Desktop mouse paint drag
    root.addEventListener("mousedown", (e) => {
      if (this._editorMode === "paint") {
        const el = findTarget(e);
        if (el && el.dataset.action === "grid-cell") {
          this._isPainting = true;
          this._paintCell(el);
        }
      }
    });

    root.addEventListener("mousemove", (e) => {
      if (this._isPainting && this._editorMode === "paint") {
        const el = root.elementFromPoint(e.clientX, e.clientY);
        if (el && el.dataset?.action === "grid-cell") {
          this._paintCell(el);
        }
      }
    });

    root.addEventListener("mouseup", () => {
      this._isPainting = false;
    });

    root.addEventListener("mouseleave", () => {
      this._isPainting = false;
    });

    // Desktop click
    root.addEventListener("click", (e) => {
      if (touchActive) return;
      const el = findTarget(e);
      if (el) this._dispatchAction(el);
    });

    // Input events for text fields
    root.addEventListener("input", (e) => {
      const el = e.target;
      if (!el || !el.dataset?.action) return;
      this._handleInput(el);
    });

    // Change events for selects and checkboxes
    root.addEventListener("change", (e) => {
      const el = e.target;
      if (!el || !el.dataset?.action) return;
      this._handleChange(el);
    });
  }

  _paintCell(el) {
    const r = parseInt(el.dataset.row, 10);
    const c = parseInt(el.dataset.col, 10);
    if (isNaN(r) || isNaN(c)) return;
    if (this._editorGrid[r][c] === this._selectedPaletteCode) return;
    this._editorGrid[r][c] = this._selectedPaletteCode;

    // Update cell visually without full re-render
    const isColor = COLOR_MAP[this._selectedPaletteCode];
    const bg = isColor || "rgba(0,0,0,0.22)";
    const ch = isColor ? "" : (CODE_TO_CHAR[this._selectedPaletteCode] || "");
    el.style.background = bg;
    el.textContent = ch;
  }

  _dispatchAction(el) {
    const action = el.dataset.action;

    switch (action) {
      case "tab":
        this._activeTab = el.dataset.tab;
        this._render();
        break;

      case "set-mode":
        this._editorMode = el.dataset.mode;
        if (this._editorMode === "text") {
          this._editorGrid = this._textToGrid(this._textInput, this._borderColor);
        }
        this._render();
        break;

      case "grid-cell":
        // Handled by paint in mousedown/touchstart
        break;

      case "select-palette":
        this._selectedPaletteCode = parseInt(el.dataset.code, 10);
        this._render();
        break;

      case "apply-border": {
        const code = this._selectedPaletteCode;
        if (COLOR_MAP[code]) {
          for (let c = 0; c < VBC_COLS; c++) {
            this._editorGrid[0][c] = code;
            this._editorGrid[5][c] = code;
          }
          for (let r = 0; r < VBC_ROWS; r++) {
            this._editorGrid[r][0] = code;
            this._editorGrid[r][21] = code;
          }
          this._render();
        }
        break;
      }

      case "clear-grid":
        this._editorGrid = vbcEmptyGrid();
        this._render();
        break;

      case "set-rating": {
        const rating = parseInt(el.dataset.rating, 10);
        this._editorRating = this._editorRating === rating ? 0 : rating;
        this._render();
        break;
      }

      case "save-to-library":
        this._saveToLibrary();
        break;

      case "push-to-board":
        this._pushToBoard();
        break;

      case "clear-edit":
        this._editingFrameId = null;
        this._editorGrid = vbcEmptyGrid();
        this._editorName = "";
        this._editorRating = 0;
        this._render();
        break;

      // Library sub-tabs
      case "lib-subtab":
        this._librarySubTab = el.dataset.subtab;
        this._libraryPage = 0;
        this._render();
        break;

      case "lib-prev":
        if (this._libraryPage > 0) {
          this._libraryPage--;
          this._render();
        }
        break;

      case "lib-next": {
        const category = this._librarySubTab === "art" ? "art" : "message";
        const frames = this._getLibraryFrames(category);
        const maxPage = Math.max(0, Math.ceil(frames.length / 12) - 1);
        if (this._libraryPage < maxPage) {
          this._libraryPage++;
          this._render();
        }
        break;
      }

      case "lib-view":
        this._viewLibraryFrame(el.dataset.frameId);
        break;

      case "lib-edit":
        this._editLibraryFrame(el.dataset.frameId);
        break;

      case "lib-push":
        this._pushLibraryFrame(el.dataset.frameId);
        break;

      case "lib-delete":
        this._confirmDeleteId = el.dataset.frameId;
        this._render();
        break;

      case "lib-delete-confirm":
        this._callRelay("delete_frame", { frame_id: el.dataset.frameId });
        this._confirmDeleteId = null;
        break;

      case "lib-delete-cancel":
        this._confirmDeleteId = null;
        this._render();
        break;

      case "toggle-queue":
        this._queueExpanded = !this._queueExpanded;
        this._render();
        break;

      // Store actions
      case "store-toggle": {
        const autoId = el.dataset.autoId;
        const automations = this._sensorAttr("automations", []);
        const auto = (Array.isArray(automations) ? automations : []).find((a) => a.id === autoId);
        const currentEnabled = auto ? auto.enabled : false;
        const newEnabled = !currentEnabled;
        // Optimistic local state
        if (!this._automationEdits[autoId]) this._automationEdits[autoId] = {};
        this._automationEdits[autoId].enabled = newEnabled;
        this._callRelay("toggle_automation", {
          automation_id: autoId,
          enabled: newEnabled,
        });
        this._render();
        break;
      }

      case "store-expand": {
        const autoId = el.dataset.autoId;
        this._expandedAutoId = this._expandedAutoId === autoId ? null : autoId;
        this._render();
        break;
      }

      case "store-save-config":
        this._saveStoreConfig(el.dataset.autoId);
        break;

      case "generate-art":
        if (this._artSubject.trim()) {
          this._artGenerating = true;
          this._artResultFrame = null;
          this._callRelay("generate_art", { subject: this._artSubject.trim() });
          this._render();
        }
        break;

      case "art-push":
        if (this._artResultFrame) {
          this._callRelay("push_frame", { frame: this._artResultFrame });
        }
        break;

      case "art-edit":
        if (this._artResultFrame) {
          this._editorGrid = vbcCloneGrid(this._artResultFrame);
          this._editorMode = "paint";
          this._activeTab = "editor";
          this._render();
        }
        break;

      case "art-save-library":
        if (this._artResultFrame) {
          this._callRelay("save_art_to_library", {
            frame: this._artResultFrame,
            name: this._artSubject || "AI Art",
          });
        }
        break;

      default:
        break;
    }
  }

  _handleInput(el) {
    const action = el.dataset.action;

    switch (action) {
      case "set-text":
        this._textInput = el.value.slice(0, 80);
        if (this._editorMode === "text") {
          this._editorGrid = this._textToGrid(this._textInput, this._borderColor);
          const countEl = this.shadowRoot.querySelector(".char-count");
          if (countEl) countEl.textContent = `${this._textInput.length}/80`;
          this._updateEditorGridVisual();
        }
        break;

      case "set-name":
        this._editorName = el.value;
        break;

      case "set-art-subject":
        this._artSubject = el.value;
        break;

      case "set-ttl-minutes":
        this._editorTtlMinutes = parseInt(el.value, 10) || 30;
        break;

      case "store-config-field": {
        const autoId = el.dataset.autoId;
        const field = el.dataset.field;
        const fieldType = el.dataset.fieldType;
        if (!this._automationEdits[autoId]) this._automationEdits[autoId] = {};
        if (fieldType === "number") {
          this._automationEdits[autoId][field] = parseInt(el.value, 10) || 0;
        }
        // Show save button if not already visible
        const saveBtn = this.shadowRoot.querySelector(`[data-action="store-save-config"][data-auto-id="${autoId}"]`);
        if (!saveBtn) this._render();
        break;
      }

      default:
        break;
    }
  }

  _handleChange(el) {
    const action = el.dataset.action;

    switch (action) {
      case "set-creator":
        this._editorCreator = el.value;
        break;

      case "set-border": {
        const val = el.value;
        this._borderColor = val ? parseInt(val, 10) : null;
        if (this._editorMode === "text") {
          this._editorGrid = this._textToGrid(this._textInput, this._borderColor);
          this._render();
        }
        break;
      }

      case "toggle-should-expire":
        this._editorShouldExpire = el.checked;
        this._render();
        break;

      case "lib-sort":
        this._librarySort = el.value;
        this._libraryPage = 0;
        this._render();
        break;

      case "lib-filter-creator":
        this._libraryFilterCreator = el.value;
        this._libraryPage = 0;
        this._render();
        break;

      case "lib-filter-rating":
        this._libraryFilterRating = parseInt(el.value, 10) || 0;
        this._libraryPage = 0;
        this._render();
        break;

      case "auto-toggle": {
        const autoId = el.dataset.autoId;
        // Bug 6 fix: optimistic local state update
        if (!this._automationEdits[autoId]) this._automationEdits[autoId] = {};
        this._automationEdits[autoId].enabled = el.checked;
        this._callRelay("toggle_automation", {
          automation_id: autoId,
          enabled: el.checked,
        });
        this._render();
        break;
      }

      case "store-config-field": {
        const autoId = el.dataset.autoId;
        const field = el.dataset.field;
        const fieldType = el.dataset.fieldType;
        if (!this._automationEdits[autoId]) this._automationEdits[autoId] = {};
        if (fieldType === "bool") {
          this._automationEdits[autoId][field] = el.checked;
        }
        // Show save button
        const saveBtn = this.shadowRoot.querySelector(`[data-action="store-save-config"][data-auto-id="${autoId}"]`);
        if (!saveBtn) this._render();
        break;
      }

      default:
        break;
    }
  }

  _updateEditorGridVisual() {
    const grid = this._editorGrid;
    const root = this.shadowRoot;
    for (let r = 0; r < VBC_ROWS; r++) {
      for (let c = 0; c < VBC_COLS; c++) {
        const el = root.querySelector(`#editor-${r}-${c}`);
        if (!el) continue;
        const code = grid[r][c] || 0;
        const isColor = COLOR_MAP[code];
        el.style.background = isColor || "rgba(0,0,0,0.22)";
        el.textContent = isColor ? "" : (CODE_TO_CHAR[code] || "");
      }
    }
  }

  /* ── Action methods ─────────────────────────────────────────────── */

  _saveToLibrary() {
    const grid = this._editorMode === "text"
      ? this._textToGrid(this._textInput, this._borderColor)
      : this._editorGrid;

    if (this._editingFrameId) {
      this._callRelay("update_frame", {
        frame_id: this._editingFrameId,
        frame: grid,
        name: this._editorName,
        creator: this._editorCreator,
        rating: this._editorRating,
      });
      this._editingFrameId = null;
    } else {
      this._callRelay("save_frame", {
        frame: grid,
        name: this._editorName,
        creator: this._editorCreator,
        rating: this._editorRating,
      });
    }
  }

  _pushToBoard() {
    const grid = this._editorMode === "text"
      ? this._textToGrid(this._textInput, this._borderColor)
      : this._editorGrid;

    const data = {
      frame: grid,
      ttl_minutes: this._editorTtlMinutes,
      should_expire: this._editorShouldExpire,
    };
    this._callRelay("push_frame", data);
  }

  _viewLibraryFrame(frameId) {
    const frames = this._parseJsonAttr("library", []);
    const frame = (Array.isArray(frames) ? frames : []).find((f) => f.frame_id === frameId);
    if (!frame || !frame.characters) return;

    this._editorGrid = vbcCloneGrid(frame.characters);
    this._editorMode = "paint";
    this._activeTab = "editor";
    this._render();
  }

  _editLibraryFrame(frameId) {
    const frames = this._parseJsonAttr("library", []);
    const frame = (Array.isArray(frames) ? frames : []).find((f) => f.frame_id === frameId);
    if (!frame) return;

    this._editingFrameId = frameId;
    this._editorGrid = vbcCloneGrid(frame.characters);
    this._editorMode = "paint";
    this._editorName = frame.name || "";
    this._editorCreator = frame.creator || "";
    this._editorRating = frame.rating || 0;
    this._activeTab = "editor";
    this._render();
  }

  _pushLibraryFrame(frameId) {
    const data = {
      frame_id: frameId,
      ttl_minutes: this._editorTtlMinutes,
      should_expire: this._editorShouldExpire,
    };
    this._callRelay("push_library_frame", data);
  }

  _saveStoreConfig(autoId) {
    const edits = this._automationEdits[autoId];
    if (!edits) return;

    // Separate enabled from other config fields
    const { enabled, ...configFields } = edits;

    if (Object.keys(configFields).length > 0) {
      this._callRelay("set_automation_config", {
        automation_id: autoId,
        config: configFields,
      });
    }

    // Show saved feedback
    this._storeSavedFeedback[autoId] = Date.now();
    delete this._automationEdits[autoId];
    this._render();

    // Clear saved feedback after 2s
    setTimeout(() => {
      if (this._storeSavedFeedback[autoId]) {
        delete this._storeSavedFeedback[autoId];
        this._render();
      }
    }, 2000);
  }

  /* ── Styles ─────────────────────────────────────────────────────── */

  _styles() {
    return `
      :host {
        --vbc-radius: 12px;
        --vbc-radius-sm: 8px;
        --vbc-spacing: 16px;
        --vbc-spacing-sm: 8px;
        --vbc-surface: var(--card-background-color, var(--ha-card-background, #fff));
        --vbc-surface-variant: var(--secondary-background-color, #f5f5f5);
        --vbc-on-surface: var(--primary-text-color, #212121);
        --vbc-on-surface-secondary: var(--secondary-text-color, #757575);
        --vbc-primary: var(--primary-color, #03a9f4);
        --vbc-primary-light: color-mix(in srgb, var(--vbc-primary) 15%, transparent);
        --vbc-border: var(--divider-color, #e0e0e0);
        --vbc-success: var(--success-color, #4caf50);
        --vbc-error: var(--error-color, #f44336);
        --vbc-warning: var(--warning-color, #ff9800);
        --vbc-muted: #9e9e9e;
      }

      :host {
        display: block;
        touch-action: pan-y;
      }

      ha-card {
        overflow: hidden;
        touch-action: pan-y;
      }

      .card-header { padding: var(--vbc-spacing) var(--vbc-spacing) 0; }

      .header-row {
        display: flex;
        align-items: center;
        gap: var(--vbc-spacing-sm);
      }

      .header-icon {
        color: var(--vbc-primary);
        --mdc-icon-size: 24px;
      }

      .header-title {
        font-size: 18px;
        font-weight: 500;
        color: var(--vbc-on-surface);
        flex: 1;
      }

      .status-dot {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        flex-shrink: 0;
      }

      .card-content {
        padding: var(--vbc-spacing);
        display: flex;
        flex-direction: column;
        gap: var(--vbc-spacing);
        touch-action: pan-y;
      }

      /* Board Preview */
      .board-preview {
        background: var(--vbc-surface-variant);
        border-radius: var(--vbc-radius-sm);
        padding: 12px;
        text-align: center;
        overflow: hidden;
      }

      .vb-grid {
        display: inline-grid;
        justify-content: center;
        align-content: start;
        grid-auto-flow: row;
      }
      .vb-grid.vb-grid-editor {
        display: grid;
        width: 100%;
      }
      .vb-grid-editor .vb-cell {
        aspect-ratio: 1;
        touch-action: manipulation;
      }

      .vb-cell {
        box-sizing: border-box;
        display: flex;
        align-items: center;
        justify-content: center;
        color: rgba(255,255,255,0.95);
        font-weight: 800;
        font-size: clamp(6px, 1.8vw, 18px);
        line-height: 1;
        letter-spacing: 0.2px;
        user-select: none;
        -webkit-font-smoothing: antialiased;
        overflow: hidden;
      }

      .grid-empty {
        color: var(--vbc-on-surface-secondary);
        font-size: 13px;
        padding: 12px;
        text-align: center;
      }

      .preview-info {
        display: flex;
        justify-content: space-between;
        width: 100%;
        margin-top: 8px;
        font-size: 12px;
        color: var(--vbc-on-surface-secondary);
      }

      .ttl-countdown {
        font-weight: 500;
        color: var(--vbc-warning);
      }

      /* Tab bar */
      .tab-bar {
        display: flex;
        border-radius: var(--vbc-radius-sm);
        overflow: hidden;
        border: 1px solid var(--vbc-border);
      }

      .tab-btn {
        flex: 1;
        padding: 10px 8px;
        min-height: 44px;
        border: none;
        background: var(--vbc-surface);
        color: var(--vbc-on-surface-secondary);
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        font-family: inherit;
        transition: all 150ms;
      }

      .tab-btn + .tab-btn {
        border-left: 1px solid var(--vbc-border);
      }

      .tab-btn.active {
        background: var(--vbc-primary);
        color: #fff;
      }

      .tab-btn:hover:not(.active) {
        background: var(--vbc-surface-variant);
      }

      /* Sub-tab bar (Library Messages/Art) */
      .sub-tab-bar {
        display: flex;
        border-radius: 6px;
        overflow: hidden;
        border: 1px solid var(--vbc-border);
      }

      .sub-tab-btn {
        flex: 1;
        padding: 8px;
        min-height: 44px;
        border: none;
        background: var(--vbc-surface);
        color: var(--vbc-on-surface-secondary);
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        font-family: inherit;
        transition: all 150ms;
      }

      .sub-tab-btn + .sub-tab-btn {
        border-left: 1px solid var(--vbc-border);
      }

      .sub-tab-btn.active {
        background: var(--vbc-primary-light);
        color: var(--vbc-primary);
      }

      /* Editor section */
      .editor-section {
        display: flex;
        flex-direction: column;
        gap: 12px;
        touch-action: pan-y;
      }

      .mode-toggle {
        display: flex;
        border-radius: var(--vbc-radius-sm);
        overflow: hidden;
        border: 1px solid var(--vbc-border);
      }

      .mode-btn {
        flex: 1;
        padding: 8px;
        min-height: 44px;
        border: none;
        background: var(--vbc-surface);
        color: var(--vbc-on-surface-secondary);
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        font-family: inherit;
        transition: all 150ms;
      }

      .mode-btn + .mode-btn {
        border-left: 1px solid var(--vbc-border);
      }

      .mode-btn.active {
        background: var(--vbc-primary-light);
        color: var(--vbc-primary);
      }

      .editor-grid-wrap {
        display: flex;
        justify-content: center;
        padding: 8px;
        background: var(--vbc-surface-variant);
        border-radius: var(--vbc-radius-sm);
        touch-action: pan-y;
      }

      .editor-grid-wrap .vb-cell {
        cursor: crosshair;
      }

      .editor-grid-wrap .vb-cell:hover {
        outline: 2px solid var(--vbc-primary);
        outline-offset: -1px;
        z-index: 1;
      }

      /* Palette */
      .palette-section {
        display: flex;
        flex-direction: column;
        gap: 8px;
        touch-action: pan-y;
      }

      .palette-strip {
        display: flex;
        flex-wrap: wrap;
        gap: 3px;
        padding: 4px;
        background: var(--vbc-surface-variant);
        border-radius: var(--vbc-radius-sm);
      }

      .palette-cell {
        width: 24px;
        height: 24px;
        border-radius: 3px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 12px;
        font-weight: 800;
        color: rgba(255,255,255,0.95);
        cursor: pointer;
        user-select: none;
        transition: outline 100ms;
      }

      .palette-cell.selected {
        outline: 2px solid var(--vbc-primary);
        outline-offset: 1px;
      }

      .palette-cell:hover:not(.selected) {
        outline: 1px solid var(--vbc-on-surface-secondary);
        outline-offset: 1px;
      }

      /* Text mode */
      .text-mode-section {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      .text-mode-row {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .char-count {
        font-size: 11px;
        color: var(--vbc-on-surface-secondary);
        text-align: right;
      }

      /* Save section */
      .save-section {
        display: flex;
        flex-direction: column;
        gap: 10px;
        padding: 12px;
        border: 1px solid var(--vbc-border);
        border-radius: var(--vbc-radius-sm);
      }

      .save-row {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .save-row .field-label {
        width: 60px;
        flex-shrink: 0;
        font-size: 12px;
        font-weight: 500;
        color: var(--vbc-on-surface-secondary);
      }

      .ttl-row {
        flex-wrap: wrap;
        gap: 12px;
      }

      .ttl-field {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .ttl-unit {
        font-size: 12px;
        color: var(--vbc-on-surface-secondary);
      }

      .save-actions {
        display: flex;
        gap: 8px;
      }

      .star-rating {
        display: flex;
        gap: 2px;
      }

      .star {
        font-size: 20px;
        cursor: pointer;
        color: var(--vbc-muted);
        user-select: none;
        transition: color 100ms;
      }

      .star.filled {
        color: var(--vbc-warning);
      }

      .star:hover {
        color: var(--vbc-warning);
      }

      .checkbox-label {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        color: var(--vbc-on-surface);
        cursor: pointer;
        min-height: 44px;
      }

      .checkbox-label input[type="checkbox"] {
        width: 20px;
        height: 20px;
        cursor: pointer;
      }

      /* Form controls */
      .vbc-input, .vbc-select, .vbc-textarea {
        background: var(--vbc-surface);
        border: 1px solid var(--vbc-border);
        border-radius: 6px;
        padding: 8px 10px;
        font-size: 13px;
        font-family: inherit;
        color: var(--vbc-on-surface);
        outline: none;
        transition: border-color 150ms;
        min-height: 44px;
        box-sizing: border-box;
      }

      .vbc-input:focus, .vbc-select:focus, .vbc-textarea:focus {
        border-color: var(--vbc-primary);
      }

      .vbc-input { flex: 1; }
      .vbc-input-sm { width: 70px; flex: none; min-height: 36px; }

      .vbc-select { flex: 1; cursor: pointer; }
      .vbc-select-sm { flex: none; font-size: 12px; padding: 6px 8px; min-height: 36px; }

      .vbc-textarea {
        width: 100%;
        min-height: 60px;
        resize: vertical;
        box-sizing: border-box;
      }

      /* Buttons */
      .vbc-btn {
        border: none;
        border-radius: 6px;
        padding: 8px 16px;
        min-height: 44px;
        font-size: 13px;
        font-weight: 500;
        font-family: inherit;
        cursor: pointer;
        transition: all 150ms;
        white-space: nowrap;
      }

      .vbc-btn-primary {
        background: var(--vbc-primary);
        color: #fff;
      }
      .vbc-btn-primary:hover { opacity: 0.85; }

      .vbc-btn-secondary {
        background: var(--vbc-surface-variant);
        color: var(--vbc-on-surface);
        border: 1px solid var(--vbc-border);
      }
      .vbc-btn-secondary:hover {
        background: var(--vbc-primary-light);
      }

      .vbc-btn-text {
        background: none;
        color: var(--vbc-primary);
        padding: 4px 8px;
        min-height: 44px;
      }
      .vbc-btn-text:hover {
        background: var(--vbc-primary-light);
      }

      .vbc-btn-danger {
        background: var(--vbc-error);
        color: #fff;
      }
      .vbc-btn-danger:hover { opacity: 0.85; }

      .vbc-btn-installed {
        background: var(--vbc-success);
        color: #fff;
      }
      .vbc-btn-installed:hover { opacity: 0.85; }

      .vbc-btn-sm { padding: 6px 12px; font-size: 12px; min-height: 36px; }
      .vbc-btn-xs { padding: 4px 8px; font-size: 11px; min-height: 32px; }

      .vbc-btn:disabled { opacity: 0.4; cursor: default; }

      /* Library */
      .library-section {
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .library-controls {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
      }

      .library-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
        gap: 10px;
      }

      .library-empty {
        grid-column: 1 / -1;
        text-align: center;
        color: var(--vbc-on-surface-secondary);
        font-size: 13px;
        padding: 24px;
      }

      .lib-frame-card {
        border: 1px solid var(--vbc-border);
        border-radius: var(--vbc-radius-sm);
        overflow: hidden;
        background: var(--vbc-surface);
      }

      .lib-frame-preview {
        display: flex;
        justify-content: center;
        padding: 8px;
        background: var(--vbc-surface-variant);
      }

      .lib-frame-info {
        padding: 8px;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .lib-frame-name {
        font-size: 12px;
        font-weight: 500;
        color: var(--vbc-on-surface);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .lib-frame-meta {
        font-size: 10px;
        color: var(--vbc-on-surface-secondary);
      }

      .lib-frame-stars {
        font-size: 11px;
        color: var(--vbc-warning);
      }

      .lib-frame-actions {
        display: flex;
        gap: 2px;
        padding: 4px 8px 8px;
        flex-wrap: wrap;
      }

      .pagination {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
      }

      .page-info {
        font-size: 12px;
        color: var(--vbc-on-surface-secondary);
      }

      /* Store (Vestaboard+) */
      .store-section {
        display: flex;
        flex-direction: column;
        gap: 16px;
      }

      .store-header {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .store-title {
        font-size: 16px;
        font-weight: 600;
        color: var(--vbc-on-surface);
      }

      .store-subtitle {
        margin: 0;
        font-size: 13px;
        color: var(--vbc-on-surface-secondary);
      }

      .store-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
        gap: 12px;
      }

      .store-empty {
        grid-column: 1 / -1;
        text-align: center;
        color: var(--vbc-on-surface-secondary);
        font-size: 13px;
        padding: 24px;
      }

      .product-card {
        border: 1px solid var(--vbc-border);
        border-radius: var(--vbc-radius-sm);
        overflow: hidden;
        background: var(--vbc-surface);
        display: flex;
        flex-direction: column;
      }

      .product-preview {
        display: flex;
        justify-content: center;
        padding: 12px;
        background: var(--vbc-surface-variant);
        min-height: 40px;
      }

      .product-preview-empty {
        align-items: center;
      }

      .product-body {
        padding: 12px;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .product-info {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .product-name-row {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .auto-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
      }

      .product-name {
        font-size: 14px;
        font-weight: 600;
        color: var(--vbc-on-surface);
      }

      .product-desc {
        font-size: 12px;
        color: var(--vbc-on-surface-secondary);
        line-height: 1.4;
      }

      .product-actions {
        display: flex;
        gap: 8px;
        align-items: center;
      }

      /* Automation config in store */
      .auto-config-section {
        padding: 12px;
        background: var(--vbc-surface-variant);
        border-radius: 6px;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .auto-config-empty {
        font-size: 12px;
        color: var(--vbc-on-surface-secondary);
        font-style: italic;
      }

      .config-field {
        display: flex;
        align-items: center;
        gap: 8px;
      }

      .config-label {
        font-size: 12px;
        font-weight: 500;
        color: var(--vbc-on-surface-secondary);
        min-width: 120px;
        flex-shrink: 0;
      }

      .config-checkbox {
        min-height: 44px;
      }

      .saved-flash {
        font-size: 12px;
        font-weight: 500;
        color: var(--vbc-success);
        padding: 4px 0;
        animation: fadeIn 200ms ease-in;
      }

      @keyframes fadeIn {
        from { opacity: 0; }
        to { opacity: 1; }
      }

      /* AI Art */
      .ai-art-section {
        border: 1px solid var(--vbc-border);
        border-radius: var(--vbc-radius-sm);
        padding: 12px;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }

      .section-header-inline {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 14px;
        font-weight: 500;
        color: var(--vbc-on-surface);
      }

      .ai-art-controls {
        display: flex;
        gap: 8px;
      }

      .ai-art-generating {
        font-size: 12px;
        color: var(--vbc-primary);
        font-style: italic;
        padding: 4px 0;
      }

      .ai-art-result {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding-top: 8px;
        border-top: 1px solid var(--vbc-border);
      }

      .ai-art-preview {
        display: flex;
        justify-content: center;
        padding: 8px;
        background: var(--vbc-surface-variant);
        border-radius: var(--vbc-radius-sm);
      }

      .ai-art-result-actions {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
      }

      /* Queue */
      .queue-section {
        border: 1px solid var(--vbc-border);
        border-radius: var(--vbc-radius-sm);
        overflow: hidden;
      }

      .queue-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        width: 100%;
        padding: 10px 12px;
        min-height: 44px;
        border: none;
        background: var(--vbc-surface-variant);
        color: var(--vbc-on-surface);
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        font-family: inherit;
      }

      .queue-header:hover {
        background: var(--vbc-primary-light);
      }

      .queue-body {
        padding: 12px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        font-size: 12px;
      }

      .queue-current, .queue-pending, .queue-fallback, .queue-upcoming {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .queue-label {
        font-weight: 500;
        color: var(--vbc-on-surface-secondary);
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.3px;
      }

      .queue-value {
        color: var(--vbc-on-surface);
      }

      .queue-item {
        display: flex;
        justify-content: space-between;
        padding: 4px 0;
        min-height: 32px;
        align-items: center;
        border-bottom: 1px solid var(--vbc-border);
      }

      .queue-item:last-child { border-bottom: none; }

      .queue-source { color: var(--vbc-on-surface); }

      .queue-countdown {
        font-weight: 500;
        color: var(--vbc-warning);
      }

      .upcoming-item {
        border-left: 3px solid var(--vbc-primary);
        padding-left: 8px;
      }

      .upcoming-countdown {
        color: var(--vbc-primary);
      }

      /* Responsive: single column on small screens */
      @media (max-width: 640px) {
        .store-grid {
          grid-template-columns: 1fr;
        }
      }
    `;
  }
}

customElements.define("vestaboard-configuration-card", VestaboardConfigurationCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "vestaboard-configuration-card",
  name: "Vestaboard Configuration",
  description: "Manage Vestaboard display: frame editor, library, automations, and queue.",
  preview: false,
});
