/**
 * Immich Fetcher Config Card
 *
 * Custom Lovelace card for managing Immich photo fetcher filter profiles.
 * Reads from sensor.immich_fetcher_status and calls the relay script for config updates.
 */

const SENSOR_ENTITY = "sensor.immich_fetcher_status";

const SELECTION_TYPES = [
  { value: "all_photos", label: "All Photos" },
  { value: "search", label: "Search" },
  { value: "album", label: "Album" },
];

function defaultFilter() {
  return {
    name: "New Filter",
    selection: "all_photos",
    randomize: true,
    paused: false,
    search_query: "",
    search_pool_size: 250,
    album_name: "",
    people: [],
    location: "",
    taken_after: "",
    taken_before: "",
    favorites_only: false,
    rating: null,
  };
}

function relativeTime(isoStr) {
  if (!isoStr) return "—";
  const date = new Date(isoStr);
  if (isNaN(date.getTime())) return isoStr;
  const now = Date.now();
  const diffMs = now - date.getTime();
  const absDiff = Math.abs(diffMs);
  const future = diffMs < 0;
  const minutes = Math.floor(absDiff / 60000);
  const hours = Math.floor(minutes / 60);
  if (minutes < 1) return future ? "now" : "just now";
  if (minutes < 60) return future ? `in ${minutes} min` : `${minutes} min ago`;
  if (hours < 24) return future ? `in ${hours}h ${minutes % 60}m` : `${hours}h ${minutes % 60}m ago`;
  return date.toLocaleString();
}

class ImmichFetcherCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._filters = [];
    this._locationAliases = {};
    this._expandedFilterIdx = -1;
    this._numPhotos = 10;
    this._updateIntervalMinutes = 60;

    this._dirty = false;
    this._showAliasManager = false;
    this._showPeopleManager = false;
    this._newAliasName = "";
    this._newAliasCity = "";
    this._newAliasState = "";
    this._newAliasCountry = "";
    this._peopleInputValues = {};
    this._favoritePeople = [];
    this._peopleCutoff = 5;
    this._showFavoritePeopleOnly = false;
    this._lastSensorSnapshot = null;
  }

  setConfig(config) {
    this._config = config;
  }

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;
    if (firstSet) {
      this._loadFromSensor();
      this._lastSensorSnapshot = this._sensorSnapshot();
      this._render();
      return;
    }
    const snapshot = this._sensorSnapshot();
    if (snapshot === this._lastSensorSnapshot) return;
    this._lastSensorSnapshot = snapshot;
    if (!this._dirty) {
      this._loadFromSensor();
    }
    this._render();
  }

  _sensorSnapshot() {
    const s = this._hass?.states?.[SENSOR_ENTITY];
    if (!s) return null;
    const a = s.attributes || {};
    return `${s.last_updated || s.last_changed}|${a.active_filter_index}|${a.empty_filters}|${a.status}`;
  }

  _sensorState() {
    if (!this._hass) return null;
    const s = this._hass.states[SENSOR_ENTITY];
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
    } catch (e) {
      return fallback;
    }
  }

  _loadFromSensor() {
    const rawFilters = this._sensorAttr("filters", null);
    if (rawFilters) {
      try {
        const parsed = typeof rawFilters === "string" ? JSON.parse(rawFilters) : rawFilters;
        if (Array.isArray(parsed)) {
          this._filters = parsed.map((f) => ({ ...defaultFilter(), ...f }));
        }
      } catch (e) {
        console.warn("immich-fetcher-card: failed to parse filters", e);
      }
    }
    if (this._filters.length === 0) {
      this._filters = [defaultFilter()];
    }

    const rawAliases = this._sensorAttr("location_aliases", null);
    if (rawAliases) {
      try {
        this._locationAliases =
          typeof rawAliases === "string" ? JSON.parse(rawAliases) : rawAliases;
      } catch (e) {
        this._locationAliases = {};
      }
    }

    // download_quality kept in config but not exposed in UI (always "preview")

    const rawFavPeople = this._sensorAttr("favorite_people", null);
    if (rawFavPeople) {
      try {
        this._favoritePeople =
          typeof rawFavPeople === "string" ? JSON.parse(rawFavPeople) : rawFavPeople;
        if (!Array.isArray(this._favoritePeople)) this._favoritePeople = [];
      } catch (e) {
        this._favoritePeople = [];
      }
    }

    const sensorState = this._sensorState();
    if (sensorState && sensorState.attributes) {
      if (sensorState.attributes.num_photos !== undefined) {
        this._numPhotos = Number(sensorState.attributes.num_photos) || 10;
      }
      if (sensorState.attributes.update_interval_minutes !== undefined) {
        this._updateIntervalMinutes =
          Number(sensorState.attributes.update_interval_minutes) || 60;
      }
      if (sensorState.attributes.people_cutoff !== undefined) {
        this._peopleCutoff = Number(sensorState.attributes.people_cutoff) || 5;
      }
      if (sensorState.attributes.show_favorite_people_only !== undefined) {
        this._showFavoritePeopleOnly = !!sensorState.attributes.show_favorite_people_only;
      }
    }
    this._dirty = false;
  }

  _callRelay(command, data) {
    if (!this._hass) return;
    this._hass
      .callService("script", "immich_fetcher_relay", {
        command,
        payload: JSON.stringify(data || {}),
      })
      .catch((err) => {
        console.warn("immich-fetcher-card: relay failed", command, err);
      });
  }

  _saveConfig() {
    const payload = {
      filters: this._filters.map((f) => {
        const out = { ...f };
        if (out.people && out.people.length === 0) delete out.people;
        if (!out.paused) delete out.paused;
        if (!out.location) delete out.location;
        if (!out.taken_after) delete out.taken_after;
        if (!out.taken_before) delete out.taken_before;
        if (!out.favorites_only) delete out.favorites_only;
        if (out.rating === null || out.rating === undefined) delete out.rating;
        if (out.selection !== "search") {
          delete out.search_query;
          delete out.search_pool_size;
        }
        if (out.selection !== "album") {
          delete out.album_name;
        }
        return out;
      }),
      location_aliases: this._locationAliases,
      num_photos: this._numPhotos,
      update_interval_minutes: this._updateIntervalMinutes,
      download_quality: "preview",
      favorite_people: this._favoritePeople,
      people_cutoff: this._peopleCutoff,
      show_favorite_people_only: this._showFavoritePeopleOnly,
    };
    this._callRelay("update_config", payload);
    this._dirty = false;
    this._render();
  }

  _refreshNow() {
    this._callRelay("refresh_now");
  }

  _syncFilter(idx) {
    const name = this._filters[idx]?.name || "";
    this._callRelay("sync_filter", { filter_index: idx, filter_name: name });
  }

  _markDirty() {
    this._dirty = true;
    const active = this.shadowRoot.activeElement;
    if (
      active &&
      active.tagName === "INPUT" &&
      (active.type === "text" || active.type === "date")
    ) {
      const saveBtn = this.shadowRoot.querySelector('[data-action="save"]');
      if (saveBtn) saveBtn.disabled = false;
      return;
    }
    this._render();
  }

  // ── Render ────────────────────────────────────────────────────────

  _render() {
    const sensor = this._sensorState();
    const status = this._sensorAttr("status", "unknown");
    const lastFetch = this._sensorAttr("last_fetch", "");
    const lastFetchCount = this._sensorAttr("last_fetch_count", "");
    const lastFetchFilter = this._sensorAttr("last_fetch_filter", "");
    const nextFetch = this._sensorAttr("next_fetch", "");
    const activeIdx = this._sensorAttr("active_filter_index", 0);
    const emptyFilters = new Set(this._parseJsonAttr("empty_filters", []));
    const peopleAvailable = this._parseJsonAttr("people_available", []);
    const albumsAvailable = this._parseJsonAttr("albums_available", []);

    const statusColor =
      status === "fetching"
        ? "var(--warning-color, #ff9800)"
        : status === "error"
        ? "var(--error-color, #f44336)"
        : "var(--success-color, #4caf50)";

    const statusIcon =
      status === "fetching"
        ? "mdi:cloud-sync"
        : status === "error"
        ? "mdi:alert-circle"
        : "mdi:check-circle";

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="card-header">
          <div class="header-row">
            <ha-icon icon="mdi:image-multiple" class="header-icon"></ha-icon>
            <span class="header-title">Immich Fetcher</span>
          </div>
        </div>
        <div class="card-content">
          ${this._renderStatusBar(sensor, status, statusColor, statusIcon, lastFetch, lastFetchCount, lastFetchFilter, nextFetch)}
          ${this._renderFilterList(activeIdx, emptyFilters, peopleAvailable, albumsAvailable)}
          ${this._renderGlobalSettings()}
          ${this._renderPeopleManager(peopleAvailable)}
          ${this._renderAliasManager()}
          ${this._renderActions()}
        </div>
      </ha-card>
    `;
    this._ensureDelegatedListeners();
    this._attachFormListeners();
  }

  // ── Status Bar ────────────────────────────────────────────────────

  _renderStatusBar(sensor, status, statusColor, statusIcon, lastFetch, lastFetchCount, lastFetchFilter, nextFetch) {
    if (!sensor) {
      return `
        <div class="status-bar status-unavailable">
          <ha-icon icon="mdi:connection" class="status-icon"></ha-icon>
          <span class="status-text">Sensor unavailable — the fetcher app may not be running yet.</span>
        </div>`;
    }

    const lastInfo = lastFetch
      ? `${relativeTime(lastFetch)}${lastFetchFilter ? ` using "${lastFetchFilter}"` : ""}${lastFetchCount ? ` (${lastFetchCount} photos)` : ""}`
      : "never";

    return `
      <div class="status-bar">
        <div class="status-row">
          <ha-icon icon="${statusIcon}" style="color:${statusColor};--mdc-icon-size:18px;" class="status-icon"></ha-icon>
          <span class="status-label">${status.charAt(0).toUpperCase() + status.slice(1)}</span>
          <button class="btn-icon refresh-btn" data-action="refresh-now" title="Refresh Now">
            <ha-icon icon="mdi:refresh"></ha-icon>
          </button>
        </div>
        <div class="status-detail">
          <span>Last: ${lastInfo}</span>
          ${nextFetch ? `<span>Next: ${relativeTime(nextFetch)}</span>` : ""}
        </div>
      </div>`;
  }

  // ── Filter List ───────────────────────────────────────────────────

  _renderFilterList(activeIdx, emptyFilters, peopleAvailable, albumsAvailable) {
    const items = this._filters
      .map((f, i) => {
        const isActive = i === activeIdx;
        const isEmpty = emptyFilters.has(f.name);
        const isPaused = !!f.paused;
        const isExpanded = i === this._expandedFilterIdx;
        const badge = SELECTION_TYPES.find((t) => t.value === f.selection)?.label || f.selection;
        let statusIcon = "";
        if (isActive) {
          statusIcon = '<ha-icon icon="mdi:play-circle" class="active-indicator" title="Currently displaying"></ha-icon>';
        } else if (isPaused) {
          statusIcon = '<ha-icon icon="mdi:pause-circle" class="paused-indicator" title="Paused — fetcher skips this filter"></ha-icon>';
        } else if (isEmpty) {
          statusIcon = '<ha-icon icon="mdi:alert-circle" class="empty-indicator" title="Filter returned no images"></ha-icon>';
        }
        return `
          <div class="filter-item ${isExpanded ? "expanded" : ""} ${isActive ? "active" : ""} ${isPaused ? "paused" : ""} ${isEmpty && !isActive && !isPaused ? "empty" : ""}">
            <div class="filter-header">
              <div class="filter-header-left" data-action="toggle-filter" data-idx="${i}">
                ${statusIcon}
                <span class="filter-name">${this._escapeHtml(f.name)}</span>
                <span class="filter-badge">${badge}</span>
                ${f.randomize ? '<span class="filter-badge badge-random">Random</span>' : ""}
                ${isPaused ? '<span class="filter-badge badge-paused">Paused</span>' : ""}
              </div>
              <div class="filter-header-right">
                <button class="btn-icon sync-filter-btn" data-action="sync-filter" data-idx="${i}" title="Fetch with this filter now">
                  <ha-icon icon="mdi:sync"></ha-icon>
                </button>
                <button class="btn-icon" data-action="move-filter-up" data-idx="${i}" title="Move up" ${i === 0 ? "disabled" : ""}>
                  <ha-icon icon="mdi:chevron-up"></ha-icon>
                </button>
                <button class="btn-icon" data-action="move-filter-down" data-idx="${i}" title="Move down" ${i === this._filters.length - 1 ? "disabled" : ""}>
                  <ha-icon icon="mdi:chevron-down"></ha-icon>
                </button>
                <button class="btn-icon pause-filter-btn ${isPaused ? "resumable" : ""}" data-action="toggle-pause" data-idx="${i}" title="${isPaused ? "Resume filter" : "Pause filter (fetcher will skip it)"}">
                  <ha-icon icon="${isPaused ? "mdi:play" : "mdi:pause"}"></ha-icon>
                </button>
                <button class="btn-icon btn-danger" data-action="remove-filter" data-idx="${i}" title="Remove filter">
                  <ha-icon icon="mdi:delete"></ha-icon>
                </button>
                <ha-icon icon="${isExpanded ? "mdi:chevron-up" : "mdi:chevron-down"}" class="expand-chevron" data-action="toggle-filter" data-idx="${i}"></ha-icon>
              </div>
            </div>
            ${isExpanded ? this._renderFilterEditor(f, i, peopleAvailable, albumsAvailable) : ""}
          </div>`;
      })
      .join("");

    return `
      <div class="section">
        <div class="section-header">
          <span class="section-title">Filters</span>
          <button class="btn-text" data-action="add-filter">
            <ha-icon icon="mdi:plus"></ha-icon> Add Filter
          </button>
        </div>
        <div class="filter-list">${items}</div>
      </div>`;
  }

  // ── Filter Editor ─────────────────────────────────────────────────

  _renderFilterEditor(filter, idx, peopleAvailable, albumsAvailable) {
    const isSearch = filter.selection === "search";
    const isAlbum = filter.selection === "album";
    const isAllPhotos = filter.selection === "all_photos";

    const aliasNames = Object.keys(this._locationAliases);
    const locationOptions = aliasNames
      .map((a) => `<option value="${this._escapeHtml(a)}">`)
      .join("");

    const albumOptions = (Array.isArray(albumsAvailable) ? albumsAvailable : [])
      .map((a) => `<option value="${this._escapeHtml(a.name)}">${this._escapeHtml(a.name)} (${a.asset_count || 0})</option>`)
      .join("");

    const currentPeople = Array.isArray(filter.people) ? filter.people : [];
    const peopleTags = currentPeople
      .map(
        (p, pi) =>
          `<span class="tag">${this._escapeHtml(p)}<button class="tag-remove" data-action="remove-person" data-filter-idx="${idx}" data-person-idx="${pi}">&times;</button></span>`
      )
      .join("");

    const peopleDatalist = (Array.isArray(peopleAvailable) ? peopleAvailable : [])
      .map((p) => {
        const name = typeof p === "string" ? p : p.name;
        const count = typeof p === "object" ? p.asset_count : "";
        return `<option value="${this._escapeHtml(name)}">${this._escapeHtml(name)}${count ? ` (${count})` : ""}</option>`;
      })
      .join("");

    return `
      <div class="filter-editor">
        <div class="field-group">
          <label>Name</label>
          <input type="text" class="input" value="${this._escapeHtml(filter.name)}" data-action="set-filter-field" data-filter-idx="${idx}" data-field="name" />
        </div>

        <div class="field-group">
          <label>Selection Type</label>
          <div class="selection-row">
            ${SELECTION_TYPES.map(
              (t) =>
                `<button class="selection-btn ${filter.selection === t.value ? "selected" : ""}" data-action="set-selection" data-filter-idx="${idx}" data-value="${t.value}">${t.label}</button>`
            ).join("")}
          </div>
        </div>

        <div class="field-group">
          <label>
            <input type="checkbox" ${filter.randomize ? "checked" : ""} ${isAllPhotos ? "disabled" : ""} data-action="set-filter-field" data-filter-idx="${idx}" data-field="randomize" />
            Randomize ${isAllPhotos ? "(always on for All Photos)" : ""}
          </label>
        </div>

        ${isSearch ? `
        <div class="field-group">
          <label>Search Query <span class="required">*</span></label>
          <input type="text" class="input" placeholder="e.g. Birthday party, Beach sunset" value="${this._escapeHtml(filter.search_query || "")}" data-action="set-filter-field" data-filter-idx="${idx}" data-field="search_query" />
        </div>
        ${filter.randomize ? `
        <div class="field-group">
          <label>Search Pool Size: ${filter.search_pool_size || 250}</label>
          <input type="range" min="50" max="1000" step="50" value="${filter.search_pool_size || 250}" data-action="set-filter-field" data-filter-idx="${idx}" data-field="search_pool_size" />
        </div>` : ""}` : ""}

        ${isAlbum ? `
        <div class="field-group">
          <label>Album <span class="required">*</span></label>
          <select class="input" data-action="set-filter-field" data-filter-idx="${idx}" data-field="album_name">
            <option value="">Select an album...</option>
            ${albumOptions}
          </select>
          <input type="text" class="input" placeholder="Or type album name" value="${this._escapeHtml(filter.album_name || "")}" data-action="set-filter-field" data-filter-idx="${idx}" data-field="album_name" />
        </div>` : ""}

        <div class="subsection-title">Filter Criteria</div>

        <div class="field-group">
          <label>People</label>
          <div class="tags-container">${peopleTags}</div>
          ${this._renderQuickAddPeople(idx, currentPeople, peopleAvailable)}
          <div class="people-input-row">
            <input type="text" class="input people-input" list="people-datalist-${idx}" placeholder="Add person..." data-filter-idx="${idx}" />
            <datalist id="people-datalist-${idx}">${peopleDatalist}</datalist>
            <button class="btn-text btn-sm" data-action="add-person" data-filter-idx="${idx}">Add</button>
          </div>
        </div>

        <div class="field-group">
          <label>Location</label>
          <input type="text" class="input" list="location-datalist-${idx}" placeholder="Alias or Immich city name" value="${this._escapeHtml(filter.location || "")}" data-action="set-filter-field" data-filter-idx="${idx}" data-field="location" />
          <datalist id="location-datalist-${idx}">${locationOptions}</datalist>
        </div>

        <div class="field-row">
          <div class="field-group half">
            <label>Taken After</label>
            <input type="date" class="input" value="${filter.taken_after || ""}" data-action="set-filter-field" data-filter-idx="${idx}" data-field="taken_after" />
          </div>
          <div class="field-group half">
            <label>Taken Before</label>
            <input type="date" class="input" value="${filter.taken_before || ""}" data-action="set-filter-field" data-filter-idx="${idx}" data-field="taken_before" />
          </div>
        </div>

        <div class="field-group">
          <label>
            <input type="checkbox" ${filter.favorites_only ? "checked" : ""} data-action="set-filter-field" data-filter-idx="${idx}" data-field="favorites_only" />
            Favorited photos only
          </label>
        </div>

      </div>`;
  }

  _renderQuickAddPeople(filterIdx, currentPeople, peopleAvailable) {
    const people = Array.isArray(peopleAvailable) ? peopleAvailable : [];
    if (people.length === 0) return "";

    const favSet = new Set(this._favoritePeople);
    const currentSet = new Set(currentPeople);

    let candidates = people.filter((p) => {
      const name = typeof p === "string" ? p : p.name;
      return !currentSet.has(name);
    });

    if (this._showFavoritePeopleOnly) {
      candidates = candidates.filter((p) => {
        const name = typeof p === "string" ? p : p.name;
        return favSet.has(name);
      });
    }

    candidates = candidates.slice(0, this._peopleCutoff);
    if (candidates.length === 0) return "";

    const chips = candidates
      .map((p) => {
        const name = typeof p === "string" ? p : p.name;
        const isFav = favSet.has(name);
        return `<button class="quick-add-chip ${isFav ? "fav" : ""}" data-action="quick-add-person" data-filter-idx="${filterIdx}" data-person="${this._escapeHtml(name)}" title="Add ${this._escapeHtml(name)}">
          ${isFav ? '<ha-icon icon="mdi:star" class="chip-star"></ha-icon>' : ""}${this._escapeHtml(name)}
        </button>`;
      })
      .join("");

    return `<div class="quick-add-row">${chips}</div>`;
  }

  // ── Global Settings ───────────────────────────────────────────────

  _renderGlobalSettings() {
    return `
      <div class="section">
        <div class="section-header">
          <span class="section-title">Settings</span>
        </div>
        <div class="settings-grid">
          <div class="field-group">
            <label>Photos per batch: ${this._numPhotos}</label>
            <input type="range" min="1" max="50" step="1" value="${this._numPhotos}" data-action="set-global" data-field="num_photos" />
          </div>
          <div class="field-group">
            <label>Refresh interval: ${this._formatInterval(this._updateIntervalMinutes)}</label>
            <input type="range" min="5" max="30" step="5" value="${this._updateIntervalMinutes}" data-action="set-global" data-field="update_interval_minutes" />
          </div>
        </div>
      </div>`;
  }

  _formatInterval(minutes) {
    if (minutes < 60) return `${minutes} min`;
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return m ? `${h}h ${m}m` : `${h}h`;
  }

  // ── People Manager ─────────────────────────────────────────────────

  _renderPeopleManager(peopleAvailable) {
    const allPeople = Array.isArray(peopleAvailable) ? peopleAvailable : [];
    const favSet = new Set(this._favoritePeople);
    const visiblePeople = allPeople.slice(0, this._peopleCutoff);

    const peopleRows = visiblePeople
      .map((p, idx) => {
        const name = typeof p === "string" ? p : p.name;
        const isFav = favSet.has(name);
        return `<div class="people-row">
          <span class="people-rank">#${idx + 1}</span>
          <button class="btn-icon people-star ${isFav ? "starred" : ""}" data-action="toggle-favorite-person" data-person="${this._escapeHtml(name)}" title="${isFav ? "Remove from favorites" : "Add to favorites"}">
            <ha-icon icon="${isFav ? "mdi:star" : "mdi:star-outline"}"></ha-icon>
          </button>
          <span class="people-name">${this._escapeHtml(name)}</span>
        </div>`;
      })
      .join("");

    return `
      <div class="section">
        <div class="section-header">
          <span class="section-title">People</span>
          <button class="btn-text" data-action="toggle-people-manager">
            <ha-icon icon="${this._showPeopleManager ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
            ${this._showPeopleManager ? "Hide" : "Manage"}
          </button>
        </div>
        ${this._showPeopleManager ? `
        <div class="people-manager">
          <div class="people-settings">
            <div class="field-group">
              <label>Show top ${this._peopleCutoff} people (of ${allPeople.length})</label>
              <input type="range" min="1" max="99" step="1" value="${this._peopleCutoff}" data-action="set-global" data-field="people_cutoff" />
            </div>
            <div class="field-group">
              <label>
                <input type="checkbox" ${this._showFavoritePeopleOnly ? "checked" : ""} data-action="set-global-bool" data-field="show_favorite_people_only" />
                Only show favorites in filter quick-add
              </label>
            </div>
          </div>
          ${visiblePeople.length ? `
          <div class="people-list">
            ${peopleRows}
          </div>` : '<div class="empty-message">No people available from Immich. People appear here once the fetcher refreshes its cache from the Immich API.</div>'}
        </div>` : `
        <div class="people-summary">
          ${allPeople.length} people available${favSet.size ? `, ${favSet.size} starred` : ""}
        </div>`}
      </div>`;
  }

  // ── Location Alias Manager ────────────────────────────────────────

  _renderAliasManager() {
    const aliases = Object.entries(this._locationAliases);

    const aliasRows = aliases
      .map(
        ([name, val]) =>
          `<div class="alias-row">
            <span class="alias-name">${this._escapeHtml(name)}</span>
            <span class="alias-detail">${[val.city, val.state, val.country].filter(Boolean).join(", ")}</span>
            <button class="btn-icon btn-danger btn-sm" data-action="remove-alias" data-alias="${this._escapeHtml(name)}" title="Remove">
              <ha-icon icon="mdi:delete"></ha-icon>
            </button>
          </div>`
      )
      .join("");

    return `
      <div class="section">
        <div class="section-header">
          <span class="section-title">Location Aliases</span>
          <button class="btn-text" data-action="toggle-alias-manager">
            <ha-icon icon="${this._showAliasManager ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
            ${this._showAliasManager ? "Hide" : "Manage"}
          </button>
        </div>
        ${this._showAliasManager ? `
        <div class="alias-list">
          ${aliases.length ? aliasRows : '<div class="empty-message">No location aliases configured.</div>'}
          <div class="alias-add-form">
            <input type="text" class="input" placeholder="Display name" data-alias-field="name" value="${this._escapeHtml(this._newAliasName)}" />
            <input type="text" class="input" placeholder="City" data-alias-field="city" value="${this._escapeHtml(this._newAliasCity)}" />
            <input type="text" class="input" placeholder="State" data-alias-field="state" value="${this._escapeHtml(this._newAliasState)}" />
            <input type="text" class="input" placeholder="Country" data-alias-field="country" value="${this._escapeHtml(this._newAliasCountry)}" />
            <button class="btn-text" data-action="add-alias">
              <ha-icon icon="mdi:plus"></ha-icon> Add Alias
            </button>
          </div>
        </div>` : ""}
      </div>`;
  }

  // ── Actions ───────────────────────────────────────────────────────

  _renderActions() {
    return `
      <div class="actions-row">
        <button class="btn-primary" data-action="save" ${!this._dirty ? "disabled" : ""}>
          <ha-icon icon="mdi:content-save"></ha-icon> Save Configuration
        </button>
        <button class="btn-secondary" data-action="reload" title="Reload from sensor">
          <ha-icon icon="mdi:reload"></ha-icon> Discard Changes
        </button>
      </div>`;
  }

  // ── Event Handling ────────────────────────────────────────────────
  //
  // Uses the same touchstart/touchend + mousedown/click pattern as HA's
  // own actionHandler directive (home-assistant/frontend). Event
  // delegation at the shadow root via composedPath() means listeners
  // survive innerHTML rebuilds and work reliably on touch devices
  // (including HA companion app WebViews) because we never depend on
  // click events bubbling *out* of <ha-icon> shadow DOM.

  _ensureDelegatedListeners() {
    if (this._delegatedBound) return;
    this._delegatedBound = true;

    const root = this.shadowRoot;
    let touchActive = false;
    let touchCancelled = false;
    let moveCount = 0;

    ["touchcancel", "touchmove", "scroll"].forEach((evt) => {
      root.addEventListener(evt, () => { touchCancelled = true; moveCount++; }, { passive: true });
    });

    root.addEventListener("touchstart", () => {
      touchActive = true;
      touchCancelled = false;
      moveCount = 0;
    }, { passive: true });

    root.addEventListener("touchend", (e) => {
      const el = this._findActionTarget(e);
      const action = el?.dataset?.action || "none";
      if (touchCancelled || !el) {
        touchActive = false;
        return;
      }
      const tag = el.tagName?.toLowerCase();
      const nativeEl = tag === "input" || tag === "select" || tag === "textarea";
      if (!nativeEl && e.cancelable) e.preventDefault();
      this._dispatchAction(action, el);
      setTimeout(() => { touchActive = false; }, 400);
    });

    root.addEventListener("click", (e) => {
      const el = this._findActionTarget(e);
      const action = el?.dataset?.action || "none";
      if (touchActive) return;
      if (el) this._dispatchAction(action, el);
    });
  }

  _findActionTarget(e) {
    for (const node of e.composedPath()) {
      if (node === this.shadowRoot || node === this) break;
      if (node.dataset?.action) return node;
    }
    return null;
  }

  _dispatchAction(action, el) {
    const root = this.shadowRoot;
    const idx = () => parseInt(el.dataset.idx);
    const filterIdx = () => parseInt(el.dataset.filterIdx);

    switch (action) {
      case "refresh-now":
        this._refreshNow();
        break;

      case "sync-filter":
        this._syncFilter(idx());
        break;

      case "toggle-filter": {
        const i = idx();
        this._expandedFilterIdx = this._expandedFilterIdx === i ? -1 : i;
        this._render();
        break;
      }

      case "add-filter":
        this._filters.push(defaultFilter());
        this._expandedFilterIdx = this._filters.length - 1;
        this._markDirty();
        break;

      case "remove-filter": {
        const i = idx();
        if (this._filters.length <= 1) break;
        this._filters.splice(i, 1);
        if (this._expandedFilterIdx >= this._filters.length) this._expandedFilterIdx = -1;
        if (this._expandedFilterIdx === i) this._expandedFilterIdx = -1;
        this._markDirty();
        break;
      }

      case "toggle-pause": {
        const i = idx();
        this._filters[i].paused = !this._filters[i].paused;
        this._markDirty();
        break;
      }

      case "move-filter-up": {
        const i = idx();
        if (i === 0) break;
        [this._filters[i - 1], this._filters[i]] = [this._filters[i], this._filters[i - 1]];
        if (this._expandedFilterIdx === i) this._expandedFilterIdx = i - 1;
        else if (this._expandedFilterIdx === i - 1) this._expandedFilterIdx = i;
        this._markDirty();
        break;
      }

      case "move-filter-down": {
        const i = idx();
        if (i >= this._filters.length - 1) break;
        [this._filters[i], this._filters[i + 1]] = [this._filters[i + 1], this._filters[i]];
        if (this._expandedFilterIdx === i) this._expandedFilterIdx = i + 1;
        else if (this._expandedFilterIdx === i + 1) this._expandedFilterIdx = i;
        this._markDirty();
        break;
      }

      case "set-selection": {
        const i = filterIdx();
        const value = el.dataset.value;
        this._filters[i].selection = value;
        if (value === "all_photos") this._filters[i].randomize = true;
        this._markDirty();
        break;
      }

      case "add-person": {
        const i = filterIdx();
        const input = root.querySelector(`.people-input[data-filter-idx="${i}"]`);
        if (!input) break;
        const name = input.value.trim();
        if (!name) break;
        if (!this._filters[i].people) this._filters[i].people = [];
        if (!this._filters[i].people.includes(name)) {
          this._filters[i].people.push(name);
          this._markDirty();
        }
        break;
      }

      case "remove-person": {
        const fIdx = filterIdx();
        const pIdx = parseInt(el.dataset.personIdx);
        if (this._filters[fIdx].people) {
          this._filters[fIdx].people.splice(pIdx, 1);
          this._markDirty();
        }
        break;
      }

      case "toggle-people-manager":
        this._showPeopleManager = !this._showPeopleManager;
        this._render();
        break;

      case "toggle-favorite-person": {
        const person = el.dataset.person;
        const pi = this._favoritePeople.indexOf(person);
        if (pi >= 0) this._favoritePeople.splice(pi, 1);
        else this._favoritePeople.push(person);
        this._markDirty();
        break;
      }

      case "quick-add-person": {
        const i = filterIdx();
        const person = el.dataset.person;
        if (!this._filters[i].people) this._filters[i].people = [];
        if (!this._filters[i].people.includes(person)) {
          this._filters[i].people.push(person);
          this._markDirty();
        }
        break;
      }

      case "toggle-alias-manager":
        this._showAliasManager = !this._showAliasManager;
        this._render();
        break;

      case "add-alias": {
        const nameInput = root.querySelector('[data-alias-field="name"]');
        const cityInput = root.querySelector('[data-alias-field="city"]');
        const stateInput = root.querySelector('[data-alias-field="state"]');
        const countryInput = root.querySelector('[data-alias-field="country"]');
        const name = (nameInput?.value || "").trim();
        if (!name) break;
        const alias = {};
        const city = (cityInput?.value || "").trim();
        const state = (stateInput?.value || "").trim();
        const country = (countryInput?.value || "").trim();
        if (city) alias.city = city;
        if (state) alias.state = state;
        if (country) alias.country = country;
        if (Object.keys(alias).length === 0) break;
        this._locationAliases[name] = alias;
        this._newAliasName = "";
        this._newAliasCity = "";
        this._newAliasState = "";
        this._newAliasCountry = "";
        this._markDirty();
        break;
      }

      case "remove-alias":
        delete this._locationAliases[el.dataset.alias];
        this._markDirty();
        break;

      case "save":
        this._saveConfig();
        break;

      case "reload":
        this._loadFromSensor();
        this._expandedFilterIdx = -1;
        this._render();
        break;
    }
  }

  /**
   * Bind form-specific listeners that need per-element attachment (input/change
   * events on form controls). Called on every render since innerHTML destroys
   * the previous elements.
   */
  _attachFormListeners() {
    const root = this.shadowRoot;

    root.querySelectorAll('[data-action="set-filter-field"]').forEach((el) => {
      const idx = parseInt(el.dataset.filterIdx);
      const field = el.dataset.field;
      const handler = () => {
        if (field === "randomize" || field === "favorites_only") {
          this._filters[idx][field] = el.checked;
        } else if (field === "search_pool_size") {
          this._filters[idx][field] = parseInt(el.value) || 250;
        } else {
          this._filters[idx][field] = el.value;
        }
        this._markDirty();
      };
      if (el.type === "checkbox") {
        el.addEventListener("change", handler);
      } else if (el.type === "range") {
        el.addEventListener("input", handler);
      } else {
        el.addEventListener("input", handler);
      }
    });

    root.querySelectorAll('[data-action="set-global"]').forEach((el) => {
      const field = el.dataset.field;
      const handler = () => {
        if (field === "num_photos") {
          this._numPhotos = parseInt(el.value) || 10;
        } else if (field === "update_interval_minutes") {
          this._updateIntervalMinutes = parseInt(el.value) || 60;
        } else if (field === "people_cutoff") {
          this._peopleCutoff = parseInt(el.value) || 5;
        }
        this._markDirty();
      };
      if (el.type === "range") {
        el.addEventListener("input", handler);
      } else {
        el.addEventListener("change", handler);
      }
    });

    root.querySelectorAll('[data-action="set-global-bool"]').forEach((el) => {
      el.addEventListener("change", () => {
        const field = el.dataset.field;
        if (field === "show_favorite_people_only") {
          this._showFavoritePeopleOnly = el.checked;
        }
        this._markDirty();
      });
    });

    root.querySelectorAll("[data-alias-field]").forEach((el) => {
      el.addEventListener("input", () => {
        const field = el.dataset.aliasField;
        if (field === "name") this._newAliasName = el.value;
        if (field === "city") this._newAliasCity = el.value;
        if (field === "state") this._newAliasState = el.value;
        if (field === "country") this._newAliasCountry = el.value;
      });
    });

    root.querySelectorAll(".people-input").forEach((input) => {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          const idx = parseInt(input.dataset.filterIdx);
          const name = input.value.trim();
          if (!name) return;
          if (!this._filters[idx].people) this._filters[idx].people = [];
          if (!this._filters[idx].people.includes(name)) {
            this._filters[idx].people.push(name);
            this._markDirty();
          }
        }
      });
    });

    root.querySelectorAll('select[data-field="album_name"]').forEach((sel) => {
      sel.addEventListener("change", () => {
        const idx = parseInt(sel.dataset.filterIdx);
        if (sel.value) {
          this._filters[idx].album_name = sel.value;
          this._markDirty();
        }
      });
    });
  }

  // ── Styles ────────────────────────────────────────────────────────

  _styles() {
    return `
      :host {
        --ifc-radius: 12px;
        --ifc-radius-sm: 8px;
        --ifc-spacing: 16px;
        --ifc-spacing-sm: 8px;
        --ifc-spacing-xs: 4px;
        --ifc-surface: var(--card-background-color, var(--ha-card-background, #fff));
        --ifc-surface-variant: var(--secondary-background-color, #f5f5f5);
        --ifc-on-surface: var(--primary-text-color, #212121);
        --ifc-on-surface-secondary: var(--secondary-text-color, #757575);
        --ifc-primary: var(--primary-color, #03a9f4);
        --ifc-primary-light: color-mix(in srgb, var(--ifc-primary) 15%, transparent);
        --ifc-border: var(--divider-color, #e0e0e0);
        --ifc-error: var(--error-color, #f44336);
        --ifc-success: var(--success-color, #4caf50);
        --ifc-warning: var(--warning-color, #ff9800);
      }

      ha-card {
        overflow: hidden;
      }

      .card-header {
        padding: var(--ifc-spacing) var(--ifc-spacing) 0;
      }

      .header-row {
        display: flex;
        align-items: center;
        gap: var(--ifc-spacing-sm);
      }

      .header-icon {
        color: var(--ifc-primary);
        --mdc-icon-size: 24px;
      }

      .header-title {
        font-size: 18px;
        font-weight: 500;
        color: var(--ifc-on-surface);
      }

      .card-content {
        padding: var(--ifc-spacing);
        display: flex;
        flex-direction: column;
        gap: var(--ifc-spacing);
      }

      /* Status Bar */
      .status-bar {
        background: var(--ifc-surface-variant);
        border-radius: var(--ifc-radius-sm);
        padding: 12px;
      }

      .status-unavailable {
        display: flex;
        align-items: center;
        gap: var(--ifc-spacing-sm);
        color: var(--ifc-on-surface-secondary);
      }

      .status-unavailable .status-icon {
        --mdc-icon-size: 20px;
        color: var(--ifc-warning);
      }

      .status-row {
        display: flex;
        align-items: center;
        gap: var(--ifc-spacing-sm);
      }

      .status-icon {
        --mdc-icon-size: 18px;
        flex-shrink: 0;
      }

      .status-label {
        font-weight: 500;
        font-size: 14px;
        flex: 1;
      }

      .status-detail {
        display: flex;
        flex-direction: column;
        gap: 2px;
        margin-top: 6px;
        font-size: 12px;
        color: var(--ifc-on-surface-secondary);
      }

      /* Sections */
      .section {
        border: 1px solid var(--ifc-border);
        border-radius: var(--ifc-radius);
        overflow: hidden;
      }

      .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 12px var(--ifc-spacing);
        background: var(--ifc-surface-variant);
        border-bottom: 1px solid var(--ifc-border);
      }

      .section-title {
        font-weight: 500;
        font-size: 14px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--ifc-on-surface-secondary);
      }

      .subsection-title {
        font-weight: 500;
        font-size: 13px;
        color: var(--ifc-on-surface-secondary);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-top: 12px;
        margin-bottom: 4px;
        padding-bottom: 4px;
        border-bottom: 1px solid var(--ifc-border);
      }

      /* Filter list */
      .filter-list {
        display: flex;
        flex-direction: column;
      }

      .filter-item {
        border-bottom: 1px solid var(--ifc-border);
      }

      .filter-item:last-child {
        border-bottom: none;
      }

      .filter-item.active .filter-header {
        border-left: 3px solid var(--ifc-primary);
      }

      .filter-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px var(--ifc-spacing);
        user-select: none;
        transition: background-color 150ms;
        border-left: 3px solid transparent;
      }

      .filter-header:hover {
        background: var(--ifc-surface-variant);
      }

      .filter-header-left {
        display: flex;
        align-items: center;
        gap: var(--ifc-spacing-sm);
        flex: 1;
        min-width: 0;
        cursor: pointer;
      }

      .active-indicator {
        --mdc-icon-size: 16px;
        color: var(--ifc-primary);
        flex-shrink: 0;
      }

      .empty-indicator {
        --mdc-icon-size: 16px;
        color: var(--warning-color, #ff9800);
        flex-shrink: 0;
      }

      .paused-indicator {
        --mdc-icon-size: 16px;
        color: var(--ifc-on-surface-secondary);
        flex-shrink: 0;
      }

      .filter-item.empty .filter-name {
        opacity: 0.6;
      }

      .filter-item.paused .filter-name {
        opacity: 0.5;
      }

      .filter-item.paused .filter-badge:not(.badge-paused) {
        opacity: 0.5;
      }

      .filter-name {
        font-weight: 500;
        font-size: 14px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }

      .filter-badge {
        font-size: 11px;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--ifc-primary-light);
        color: var(--ifc-primary);
        font-weight: 500;
        white-space: nowrap;
        flex-shrink: 0;
      }

      .badge-random {
        background: color-mix(in srgb, var(--ifc-success) 15%, transparent);
        color: var(--ifc-success);
      }

      .badge-paused {
        background: color-mix(in srgb, var(--ifc-on-surface-secondary) 15%, transparent);
        color: var(--ifc-on-surface-secondary);
      }

      .filter-header-right {
        display: flex;
        align-items: center;
        gap: 2px;
        flex-shrink: 0;
      }

      .expand-chevron {
        --mdc-icon-size: 20px;
        color: var(--ifc-on-surface-secondary);
        margin-left: 4px;
        cursor: pointer;
        padding: 4px;
      }

      /* Filter editor */
      .filter-editor {
        padding: var(--ifc-spacing);
        background: var(--ifc-surface);
        border-top: 1px solid var(--ifc-border);
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      /* Fields */
      .field-group {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }

      .field-group label {
        font-size: 12px;
        font-weight: 500;
        color: var(--ifc-on-surface-secondary);
        display: flex;
        align-items: center;
        gap: 4px;
      }

      .field-group label input[type="checkbox"] {
        margin: 0;
        accent-color: var(--ifc-primary);
      }

      .field-row {
        display: flex;
        gap: var(--ifc-spacing-sm);
      }

      .field-group.half {
        flex: 1;
      }

      .required {
        color: var(--ifc-error);
      }

      .input, select.input {
        width: 100%;
        padding: 8px 12px;
        border: 1px solid var(--ifc-border);
        border-radius: var(--ifc-radius-sm);
        background: var(--ifc-surface);
        color: var(--ifc-on-surface);
        font-size: 14px;
        font-family: inherit;
        outline: none;
        transition: border-color 150ms;
        box-sizing: border-box;
      }

      .input:focus, select.input:focus {
        border-color: var(--ifc-primary);
      }

      input[type="range"] {
        width: 100%;
        accent-color: var(--ifc-primary);
      }

      /* Selection buttons */
      .selection-row {
        display: flex;
        gap: 4px;
      }

      .selection-btn {
        flex: 1;
        padding: 8px 4px;
        border: 1px solid var(--ifc-border);
        border-radius: var(--ifc-radius-sm);
        background: var(--ifc-surface);
        color: var(--ifc-on-surface);
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        transition: all 150ms;
        font-family: inherit;
      }

      .selection-btn:hover {
        background: var(--ifc-surface-variant);
      }

      .selection-btn.selected {
        background: var(--ifc-primary);
        color: #fff;
        border-color: var(--ifc-primary);
      }

      /* Tags (people) */
      .tags-container {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
      }

      .tag {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 8px;
        border-radius: 12px;
        background: var(--ifc-primary-light);
        color: var(--ifc-primary);
        font-size: 12px;
        font-weight: 500;
      }

      .tag-remove {
        background: none;
        border: none;
        color: inherit;
        cursor: pointer;
        font-size: 14px;
        line-height: 1;
        padding: 0 2px;
        opacity: 0.7;
      }

      .tag-remove:hover {
        opacity: 1;
      }

      .people-input-row {
        display: flex;
        gap: 4px;
        align-items: center;
      }

      .people-input-row .input {
        flex: 1;
      }

      /* Settings grid */
      .settings-grid {
        padding: var(--ifc-spacing);
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      /* Alias manager */
      .alias-list {
        padding: var(--ifc-spacing);
        display: flex;
        flex-direction: column;
        gap: var(--ifc-spacing-sm);
      }

      .alias-row {
        display: flex;
        align-items: center;
        gap: var(--ifc-spacing-sm);
        padding: 6px 8px;
        background: var(--ifc-surface-variant);
        border-radius: var(--ifc-radius-sm);
      }

      .alias-name {
        font-weight: 500;
        font-size: 13px;
        min-width: 100px;
      }

      .alias-detail {
        flex: 1;
        font-size: 12px;
        color: var(--ifc-on-surface-secondary);
      }

      .alias-add-form {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px;
        margin-top: 8px;
        padding-top: 8px;
        border-top: 1px solid var(--ifc-border);
      }

      .alias-add-form .btn-text {
        grid-column: 1 / -1;
        justify-self: start;
      }

      .empty-message {
        font-size: 13px;
        color: var(--ifc-on-surface-secondary);
        font-style: italic;
        padding: 4px 0;
      }

      /* People Manager */
      .people-manager {
        padding: var(--ifc-spacing);
        display: flex;
        flex-direction: column;
        gap: 12px;
      }

      .people-summary {
        padding: 4px var(--ifc-spacing);
        font-size: 12px;
        color: var(--ifc-on-surface-secondary);
      }

      .people-list {
        max-height: 240px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }

      .people-row {
        display: flex;
        align-items: center;
        gap: var(--ifc-spacing-sm);
        padding: 6px 8px;
        background: var(--ifc-surface-variant);
        border-radius: var(--ifc-radius-sm);
      }

      .people-star {
        background: none;
        border: none;
        cursor: pointer;
        padding: 2px;
        border-radius: 50%;
        color: var(--ifc-on-surface-secondary);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        transition: all 150ms;
        --mdc-icon-size: 20px;
      }

      .people-star:hover {
        color: var(--ifc-warning, #ff9800);
      }

      .people-star.starred {
        color: var(--ifc-warning, #ff9800);
      }

      .people-name {
        flex: 1;
        font-size: 13px;
        font-weight: 500;
      }

      .people-rank {
        font-size: 11px;
        color: var(--ifc-on-surface-secondary);
        min-width: 28px;
        text-align: right;
        font-variant-numeric: tabular-nums;
      }

      .people-settings {
        padding-top: 8px;
        border-top: 1px solid var(--ifc-border);
        display: flex;
        flex-direction: column;
        gap: 8px;
      }

      /* Quick-add people chips */
      .quick-add-row {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
        margin-bottom: 4px;
      }

      .quick-add-chip {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        padding: 3px 10px;
        border-radius: 12px;
        border: 1px dashed var(--ifc-border);
        background: var(--ifc-surface);
        color: var(--ifc-on-surface-secondary);
        cursor: pointer;
        font-size: 12px;
        font-weight: 500;
        font-family: inherit;
        transition: all 150ms;
      }

      .quick-add-chip:hover {
        border-color: var(--ifc-primary);
        color: var(--ifc-primary);
        background: var(--ifc-primary-light);
      }

      .quick-add-chip.fav {
        border-color: color-mix(in srgb, var(--ifc-warning, #ff9800) 40%, transparent);
      }

      .quick-add-chip .chip-star {
        --mdc-icon-size: 12px;
        color: var(--ifc-warning, #ff9800);
      }

      /* Make ha-icon pass-through inside interactive elements so touch
         events land on the button/link, not the icon's shadow DOM. */
      [data-action] ha-icon,
      .btn-icon ha-icon,
      .btn-text ha-icon,
      .quick-add-chip ha-icon {
        pointer-events: none;
      }

      /* Buttons */
      .btn-icon {
        background: none;
        border: none;
        cursor: pointer;
        padding: 4px;
        border-radius: 50%;
        color: var(--ifc-on-surface-secondary);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        transition: all 150ms;
        --mdc-icon-size: 18px;
        -webkit-tap-highlight-color: transparent;
        touch-action: manipulation;
      }

      .btn-icon:hover {
        background: var(--ifc-surface-variant);
        color: var(--ifc-on-surface);
      }

      .btn-icon:disabled {
        opacity: 0.3;
        cursor: default;
      }

      .btn-icon:disabled:hover {
        background: none;
      }

      .btn-icon.btn-danger:hover {
        color: var(--ifc-error);
      }

      .btn-icon.btn-sm {
        --mdc-icon-size: 16px;
        padding: 2px;
      }

      .btn-text {
        background: none;
        border: none;
        cursor: pointer;
        color: var(--ifc-primary);
        font-size: 13px;
        font-weight: 500;
        padding: 4px 8px;
        border-radius: var(--ifc-radius-sm);
        display: inline-flex;
        align-items: center;
        gap: 4px;
        transition: background-color 150ms;
        font-family: inherit;
        --mdc-icon-size: 16px;
        -webkit-tap-highlight-color: transparent;
        touch-action: manipulation;
      }

      .btn-text:hover {
        background: var(--ifc-primary-light);
      }

      .btn-text.btn-sm {
        font-size: 12px;
        padding: 2px 6px;
      }

      .refresh-btn {
        --mdc-icon-size: 20px;
        margin-left: auto;
      }

      .sync-filter-btn {
        --mdc-icon-size: 16px;
        color: var(--ifc-primary);
        opacity: 0.7;
        transition: opacity 0.15s;
      }
      .sync-filter-btn:hover {
        opacity: 1;
      }

      /* Resume state stands out so paused filters are easy to re-enable */
      .pause-filter-btn.resumable {
        color: var(--ifc-primary);
      }

      .actions-row {
        display: flex;
        gap: var(--ifc-spacing-sm);
        justify-content: flex-end;
        flex-wrap: wrap;
      }

      .btn-primary, .btn-secondary {
        padding: 10px 20px;
        border: none;
        border-radius: var(--ifc-radius-sm);
        cursor: pointer;
        font-size: 14px;
        font-weight: 500;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        transition: all 150ms;
        font-family: inherit;
        --mdc-icon-size: 18px;
      }

      .btn-primary {
        background: var(--ifc-primary);
        color: #fff;
      }

      .btn-primary:hover:not(:disabled) {
        filter: brightness(1.1);
      }

      .btn-primary:disabled {
        opacity: 0.5;
        cursor: default;
      }

      .btn-secondary {
        background: var(--ifc-surface-variant);
        color: var(--ifc-on-surface);
        border: 1px solid var(--ifc-border);
      }

      .btn-secondary:hover {
        background: var(--ifc-border);
      }
    `;
  }

  // ── Utilities ─────────────────────────────────────────────────────

  _escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ── HA Card Boilerplate ───────────────────────────────────────────

  getCardSize() {
    return 6;
  }

  static getConfigElement() {
    return document.createElement("immich-fetcher-card-editor");
  }

  static getStubConfig() {
    return {};
  }
}

class ImmichFetcherCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    this._config = config;
    this._render();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <div style="padding: 16px;">
        <p style="color: var(--secondary-text-color, #757575); font-size: 14px;">
          This card requires no configuration. It reads from
          <code>sensor.immich_fetcher_status</code> automatically.
        </p>
      </div>
    `;
  }

  get _configValue() {
    return this._config || {};
  }

  configChanged(newConfig) {
    const event = new Event("config-changed", { bubbles: true, composed: true });
    event.detail = { config: newConfig };
    this.dispatchEvent(event);
  }
}

customElements.define("immich-fetcher-card", ImmichFetcherCard);
customElements.define("immich-fetcher-card-editor", ImmichFetcherCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "immich-fetcher-card",
  name: "Immich Fetcher Config",
  description: "Manage Immich photo fetcher filter profiles, view status, and configure fetch settings.",
  preview: true,
});
