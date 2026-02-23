class VestaboardPreviewCard extends HTMLElement {
  static getStubConfig() {
    return {
      title: "Preview (6 \u00D7 22)",
      message_entity: "input_text.vestaboard_custom_message",
      border_entity: "input_select.vestaboard_border_tile",
      cell_size: 18,
      gap: 2,
      radius: 3,
    };
  }

  setConfig(config) {
    if (!config || !config.message_entity || !config.border_entity) {
      throw new Error("Requires message_entity and border_entity");
    }
    this._config = {
      ...VestaboardPreviewCard.getStubConfig(),
      ...config,
    };

    if (!this._root) {
      this._root = this.attachShadow({ mode: "open" });
      this._root.innerHTML = `
        <style>
          :host { display: block; }
          .wrap { padding: 6px 0; }
          .title { font-weight: 600; margin: 0 0 6px 0; }
          .grid {
            display: grid;
            grid-template-columns: repeat(22, var(--vb-cell));
            gap: var(--vb-gap);
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
          }
          .cell {
            width: var(--vb-cell);
            height: var(--vb-cell);
            border-radius: var(--vb-radius);
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            user-select: none;
            -webkit-font-smoothing: antialiased;
          }
          .char {
            font-size: calc(var(--vb-cell) * 0.65);
            font-weight: 800;
            line-height: 1;
            letter-spacing: 0.2px;
          }
        </style>
        <div class="wrap">
          <div class="title"></div>
          <div class="grid"></div>
        </div>
      `;
      this._titleEl = this._root.querySelector(".title");
      this._gridEl = this._root.querySelector(".grid");
    }

    this._root.host.style.setProperty("--vb-cell", `${this._config.cell_size}px`);
    this._root.host.style.setProperty("--vb-gap", `${this._config.gap}px`);
    this._root.host.style.setProperty("--vb-radius", `${this._config.radius}px`);
    this._titleEl.textContent = this._config.title || "";
  }

  set hass(hass) {
    if (!this._config) return;

    const messageState = hass.states[this._config.message_entity]?.state ?? "";
    const borderState = hass.states[this._config.border_entity]?.state ?? "None";

    const { cells, borderColor } = this._buildCells(
      String(messageState),
      String(borderState),
    );

    const bg = this._config.background ?? "rgba(0,0,0,0.22)";
    const fg = this._config.foreground ?? "rgba(255,255,255,0.95)";

    while (this._gridEl.firstChild) this._gridEl.removeChild(this._gridEl.firstChild);
    for (const cell of cells) {
      const el = document.createElement("div");
      el.className = "cell";
      el.style.background = cell.isBorder && borderColor ? borderColor : bg;
      el.style.color = fg;

      if (cell.char && cell.char !== " ") {
        const span = document.createElement("span");
        span.className = "char";
        span.textContent = cell.char;
        el.appendChild(span);
      }

      this._gridEl.appendChild(el);
    }
  }

  getCardSize() {
    return 5;
  }

  _normalizeBorder(raw) {
    const s = String(raw ?? "").trim();
    if (!s) return "None";
    if (s.toLowerCase() === "none") return "None";

    // Some frontends/options can include extra text; keep first token.
    const token = s.split(/\s+/)[0];

    // Remove emoji variation selector (VS16) if present.
    return token.replace(/\uFE0F/g, "");
  }

  _borderColor(borderStateRaw) {
    const borderState = this._normalizeBorder(borderStateRaw);
    const map = {
      "\u{1F7E5}": "#e74c3c", // red square
      "\u{1F7E7}": "#e67e22", // orange square
      "\u{1F7E8}": "#f1c40f", // yellow square
      "\u{1F7E9}": "#2ecc71", // green square
      "\u{1F7E6}": "#3498db", // blue square
      "\u{1F7EA}": "#9b59b6", // purple square
      "\u{2B1C}": "#ecf0f1", // white large square
      "\u{2B1B}": "#2d3436", // black large square
    };
    return map[borderState] ?? null;
  }

  _normalizeMessage(raw) {
    const cleaned = raw.replace(/\r?\n/g, " ").trim().replace(/\s+/g, " ");
    return cleaned.slice(0, 80).toUpperCase();
  }

  _wrapToWidth(text, maxLines, width) {
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

  _buildCells(messageRaw, borderState) {
    const borderColor = this._borderColor(borderState);
    const hasBorder = !!borderColor;

    const text = this._normalizeMessage(messageRaw);
    const wrapped = this._wrapToWidth(text, 4, 20);
    const centered = this._centerLines(wrapped, 4, 20);

    const cells = [];
    for (let r = 0; r < 6; r++) {
      for (let c = 0; c < 22; c++) {
        const isBorder = hasBorder && (r === 0 || r === 5 || c === 0 || c === 21);
        const char =
          isBorder
            ? " "
            : centered[r - 1]?.[c - 1] ?? " ";
        cells.push({ isBorder, char });
      }
    }

    return { cells, borderColor };
  }
}

customElements.define("vestaboard-preview-card", VestaboardPreviewCard);
