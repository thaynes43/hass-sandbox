/** Exercise the real card through DOM events, relay JSON and sensor acknowledgements.
 * Timers are virtual: neither touch deduplication nor a 30s save timeout sleeps.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { ShimElement, ShimNode, shimDocument, customElements, fire } = require("./card_dom_shim");
const cardPath = process.argv[2];
const timers = new Map();
const notes = [];
let timerId = 0;
let now = 0;
let requestId = 0;
const sandbox = {
  HTMLElement: ShimElement, Element: ShimElement, Node: ShimNode,
  document: shimDocument, customElements, queueMicrotask,
  crypto: { randomUUID: () => `request-${++requestId}` },
  console: { log: (...args) => notes.push(args.join(" ")), warn: (...args) => notes.push(args.join(" ")) },
  setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, at: now + ms }); return id; },
  clearTimeout: (id) => timers.delete(id),
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(cardPath, "utf8"), sandbox, { filename: cardPath });

const clone = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
function advance(ms) {
  const finish = now + ms;
  for (;;) {
    const due = [...timers].filter(([, timer]) => timer.at <= finish).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    timers.delete(due[0]); now = due[1].at; due[1].fn();
  }
  now = finish;
}

function mount(filters = [{ name: "Family", selection: "all_photos", randomize: true }], relay) {
  const calls = [];
  const card = shimDocument.createElement("immich-fetcher-card");
  shimDocument.body.appendChild(card);
  card.setConfig({});
  const attrs = {
    status: "idle", filters: JSON.stringify(filters), num_photos: 12,
    update_interval_minutes: 20, favorite_people: '["Tom"]', people_cutoff: 8,
    show_favorite_people_only: true, location_aliases: '{"Home":{"city":"Raleigh"}}',
    people_available: '[{"name":"Tom"},{"name":"Ada"}]',
    albums_available: '[{"name":"Family"},{"name":"Trips"}]',
  };
  let revision = 0;
  const update = (extra = {}) => {
    Object.assign(attrs, extra);
    card.hass = {
      states: { "sensor.immich_fetcher_status": { state: "idle", last_updated: String(++revision), attributes: { ...attrs } } },
      callService(domain, service, data) {
        calls.push({ domain, service, command: data.command, payload: JSON.parse(data.payload) });
        return relay ? relay() : Promise.resolve();
      },
    };
  };
  update(); card._expandedFilterIdx = 0; card._render();
  const q = (selector) => { const el = card.shadowRoot.querySelector(selector); assert.ok(el, `missing ${selector}`); return el; };
  return { card, calls, update, q };
}

function input(el, value) {
  el.focus(); fire(el, "keydown", { key: "x", composed: true });
  el.value = value; fire(el, "input", { composed: true });
}
async function change(el, value) {
  el.value = value; fire(el, "change", { composed: false }); await flush();
}
function click(el) { fire(el, "click", { composed: true }); }
function dateField(q, endpoint, part, rangeIdx = 0) {
  return q(`[data-action="set-recurring-date"][data-endpoint="${endpoint}"][data-part="${part}"][data-range-idx="${rangeIdx}"]`);
}

const cases = {};
function scenario(name, run) { cases[name] = run; }

for (const [name, filter] of Object.entries({
  required_query: { selection: "search", search_query: " " },
  required_album: { selection: "album", album_name: "" },
  inverted_absolute_dates: { taken_after: "2025-05-06", taken_before: "2025-05-05" },
  invalid_absolute_day: { taken_after: "2025-02-29" },
  incomplete_recurring_start: { recurring_date_ranges: [{ start: "02-" }] },
  incomplete_recurring_end: { recurring_date_ranges: [{ start: "02-29", end: "03-" }] },
  missing_recurring_start: { recurring_date_ranges: [{ start: "", end: "03-01" }] },
  impossible_recurring_day: { recurring_date_ranges: [{ start: "04-31" }] },
  unknown_person: { people: ["Unavailable person"] },
  unknown_album: { selection: "album", album_name: "Unavailable album" },
  duplicate_filter_names: {},
})) {
  scenario(name, async () => {
    const filters = [{ name: "Family", selection: "all_photos", ...filter }];
    if (name === "duplicate_filter_names") filters.push(clone(filters[0]));
    const { card, calls, q } = mount(filters);
    card._markDirty();
    assert.equal(q('[data-action="save"]').disabled, true);
    assert.ok(card._filterErrors(card._filters[0]).length);
    card._saveConfig(); await flush();
    assert.equal(calls.length, 0); assert.equal(card._dirty, true);
    assert.ok(q('[data-filter-errors="0"]').textContent.length);
  });
}

scenario("search_with_person_and_leap_day", async () => {
  const { card, calls, q } = mount([{ name: "Birthday", selection: "search", search_query: "birthday cake", people: ["Tom"], recurring_date_ranges: [{ start: "02-29" }] }]);
  card._markDirty(); assert.equal(q('[data-action="save"]').disabled, false);
  click(q('[data-action="save"]')); await flush();
  assert.equal(calls.length, 1); assert.equal(calls[0].domain, "script");
  assert.equal(calls[0].service, "immich_fetcher_relay"); assert.equal(calls[0].command, "update_config");
  assert.deepEqual(calls[0].payload.filters[0].people, ["Tom"]);
  assert.equal(calls[0].payload.filters[0].search_query, "birthday cake");
  assert.deepEqual(calls[0].payload.filters[0].recurring_date_ranges, [{ start: "02-29", end: "02-29" }]);
});

scenario("month_days_and_month_change", async () => {
  const { card, q } = mount([{ name: "Birthday", selection: "all_photos", recurring_date_ranges: [{ start: "01-31" }] }]);
  await change(dateField(q, "start", "month"), "02");
  assert.equal(card._filters[0].recurring_date_ranges[0].start, "02-");
  const days = dateField(q, "start", "day").querySelectorAll("option").map((el) => el.value);
  assert.ok(days.includes("29")); assert.ok(!days.includes("30")); assert.ok(!days.includes("31"));
  await change(dateField(q, "start", "day"), "29");
  assert.equal(card._filters[0].recurring_date_ranges[0].start, "02-29");
  assert.equal(q('[data-action="save"]').disabled, false);
});

scenario("multiple_ranges_add_remove_and_wrap", async () => {
  const { card, calls, q } = mount();
  click(q('[data-action="add-date-range"]')); click(q('[data-action="add-date-range"]'));
  assert.equal(card._filters[0].recurring_date_ranges.length, 2); assert.equal(calls.length, 0);
  for (const [endpoint, part, value, idx] of [["start", "month", "12", 0], ["start", "day", "24", 0], ["end", "month", "01", 0], ["end", "day", "02", 0], ["start", "month", "02", 1], ["start", "day", "29", 1]]) {
    await change(dateField(q, endpoint, part, idx), value);
  }
  assert.match(card.shadowRoot.textContent, /continues through New Year/);
  click(q('[data-action="remove-date-range"][data-range-idx="0"]'));
  assert.deepEqual(clone(card._filters[0].recurring_date_ranges), [{ start: "02-29", end: "" }]);
  assert.equal(card._dirty, true); assert.equal(calls.length, 0);
});

scenario("roundtrip_preserves_config", async () => {
  const filters = [{ name: "Trips", selection: "album", album_name: "Trips", randomize: false, paused: true, people: ["Tom"], location: "Home", taken_after: "2020-01-01", taken_before: "2025-12-31", recurring_date_ranges: [{ start: "12-24", end: "01-02" }, { start: "02-29", end: "02-29" }], favorites_only: true, rating: 4 }];
  const { card, calls, update } = mount(filters); card._markDirty(); card._saveConfig();
  const sent = calls[0].payload; assert.deepEqual(sent.filters, filters);
  assert.equal(sent.num_photos, 12); assert.equal(sent.update_interval_minutes, 20);
  assert.deepEqual(sent.location_aliases, { Home: { city: "Raleigh" } });
  assert.deepEqual(sent.favorite_people, ["Tom"]); assert.equal(sent.people_cutoff, 8); assert.equal(sent.show_favorite_people_only, true);
  update({ filters: JSON.stringify(sent.filters), config_save_request_id: sent.request_id, config_save_status: "saved" });
  assert.equal(card._dirty, false); assert.equal(card._pendingSave, null);
  assert.deepEqual(clone(card._filters[0].recurring_date_ranges), filters[0].recurring_date_ranges);
  card._markDirty(); card._saveConfig(); assert.deepEqual(calls[1].payload.filters, filters);
});

scenario("legacy_timestamp_limits_roundtrip", async () => {
  const filters = [{ name: "Legacy", selection: "all_photos", taken_after: "2020-03-01T00:00:00-05:00", taken_before: "2021-03-01T05:00:00+00:00" }];
  const { card, calls, q } = mount(filters);
  assert.equal(q('[data-field="taken_after"]').value, filters[0].taken_after);
  assert.equal(q('[data-field="taken_after"]').type, "text");
  card._markDirty(); assert.equal(q('[data-action="save"]').disabled, false); card._saveConfig();
  assert.equal(calls[0].payload.filters[0].taken_after, filters[0].taken_after);
  assert.equal(calls[0].payload.filters[0].taken_before, filters[0].taken_before);
});

scenario("mixed_timestamp_and_date_same_day", async () => {
  const { card, calls, q } = mount([{ name: "Same day", selection: "all_photos", taken_after: "2020-03-01T18:00:00-05:00", taken_before: "2020-03-01" }]);
  card._markDirty(); assert.equal(q('[data-action="save"]').disabled, false); card._saveConfig();
  assert.equal(calls.length, 1); assert.equal(calls[0].payload.filters[0].taken_before, "2020-03-01");
});

scenario("stale_favorite_can_be_removed", async () => {
  const { card, calls, update, q } = mount();
  update({ favorite_people: '["Unavailable person"]', people_cutoff: 1 });
  card._showPeopleManager = true; card._render(); card._markDirty();
  assert.equal(q('[data-action="save"]').disabled, true);
  assert.match(q('[data-save-message]').textContent, /People/);
  click(q('[data-action="toggle-favorite-person"][data-person="Unavailable person"]'));
  assert.deepEqual(clone(card._favoritePeople), []);
  assert.equal(q('[data-action="save"]').disabled, false); card._saveConfig();
  assert.deepEqual(calls[0].payload.favorite_people, []);
});

scenario("favorite_beyond_cutoff_stays_removable", async () => {
  const { card, update, q } = mount(); update({ favorite_people: '["Ada"]', people_cutoff: 1 });
  card._showPeopleManager = true; card._render();
  click(q('[data-action="toggle-favorite-person"][data-person="Ada"]'));
  assert.deepEqual(clone(card._favoritePeople), []);
});

scenario("quoted_query_and_people_roundtrip", async () => {
  const person = `Tom "TJ" O'Haynes`;
  const query = `birthday "cake" and O'Haynes`;
  const { card, calls, update, q } = mount([{ name: "Quoted", selection: "search", search_query: query }]);
  update({ people_available: JSON.stringify([{ name: person }]), favorite_people: '[]', show_favorite_people_only: false });
  assert.equal(q('[data-field="search_query"]').value, query);
  const button = q('[data-action="quick-add-person"]'); assert.equal(button.dataset.person, person);
  click(button); card._saveConfig();
  assert.equal(calls[0].payload.filters[0].search_query, query);
  assert.deepEqual(calls[0].payload.filters[0].people, [person]);
});

scenario("opened_select_survives_sensor_update", async () => {
  const { card, update, q } = mount([{ name: "Birthday", recurring_date_ranges: [{ start: "02-29" }] }]);
  const select = dateField(q, "start", "month");
  fire(select, "pointerdown", { composed: true }); select.focus();
  fire(select, "touchstart", { composed: true }); update({ status: "fetching" });
  assert.ok(dateField(q, "start", "month") === select, "sensor update closed an opened dropdown");
  assert.ok(card.shadowRoot.activeElement === select);
  await change(select, "03"); select.blur(); await flush(); assert.equal(card._editing, false);
});

scenario("text_to_select_keeps_new_picker_edit", async () => {
  const { card, update, q } = mount([{ name: "Search", selection: "search", search_query: "Christmas", recurring_date_ranges: [{ start: "02-29" }] }]);
  input(q('[data-field="search_query"]'), "Christmas lights");
  const select = dateField(q, "start", "month"); fire(select, "pointerdown", { composed: true }); select.focus();
  await flush(); update({ status: "fetching" });
  assert.ok(dateField(q, "start", "month") === select, "old text blur released the new picker guard");
  assert.ok(card.shadowRoot.activeElement === select);
});

scenario("last_filter_name_renders_as_text", async () => {
  const { update, q } = mount();
  const filter = 'Holiday <img src=x onerror="bad()">';
  update({ last_fetch: "2026-10-01T12:00:00Z", last_fetch_filter: filter, last_fetch_count: 3 });
  const status = q('.status-detail');
  assert.equal(status.querySelector("img"), null);
  assert.ok(status.textContent.includes(filter), "status lost the literal filter name");
});

scenario("save_waits_for_matching_success", async () => {
  const { card, calls, update, q } = mount(); input(q('[data-field="name"]'), "New name");
  card._saveConfig(); await flush(); const request = calls[0].payload.request_id;
  assert.equal(card._dirty, true); assert.equal(calls.length, 1);
  update({ config_save_request_id: "other-client", config_save_status: "saved" });
  assert.equal(card._dirty, true); assert.equal(card._pendingSave.id, request);
  update({ config_save_request_id: request, config_save_status: "pending" }); assert.equal(card._dirty, true);
  update({ filters: JSON.stringify(calls[0].payload.filters), config_save_status: "saved" });
  assert.equal(card._dirty, false); assert.equal(card._pendingSave, null);
});

scenario("rejected_save_keeps_edits", async () => {
  const { card, calls, update, q } = mount(); input(q('[data-field="name"]'), "New name"); card._saveConfig();
  update({ config_save_request_id: calls[0].payload.request_id, config_save_status: "error", config_save_error: "Unknown album" });
  assert.equal(card._dirty, true); assert.equal(card._filters[0].name, "New name");
  assert.equal(card._pendingSave, null); assert.equal(card._saveError, true); assert.match(card._saveMessage, /Unknown album/);
});

scenario("relay_failure_keeps_edits", async () => {
  const { card, q } = mount(undefined, () => Promise.reject(new Error("network failure")));
  input(q('[data-field="name"]'), "New name"); card._saveConfig(); await flush();
  assert.equal(card._dirty, true); assert.equal(card._filters[0].name, "New name");
  assert.equal(card._pendingSave, null); assert.equal(card._saveError, true);
  assert.match(card._saveMessage, /Could not send/); assert.equal(notes.length, 1); notes.length = 0;
});

scenario("save_timeout_keeps_edits", async () => {
  const { card, q } = mount(); input(q('[data-field="name"]'), "New name"); card._saveConfig();
  advance(29999); assert.ok(card._pendingSave); advance(1);
  assert.equal(card._pendingSave, null); assert.equal(card._dirty, true);
  assert.equal(card._filters[0].name, "New name"); assert.match(card._saveMessage, /not been confirmed/);
});

scenario("pending_save_preserves_newer_edits", async () => {
  const { card, calls, update, q } = mount(); input(q('[data-field="name"]'), "Sent name"); card._saveConfig();
  const sent = calls[0].payload; input(q('[data-field="name"]'), "Newer name");
  update({ filters: JSON.stringify(sent.filters), config_save_request_id: sent.request_id, config_save_status: "saved" });
  assert.equal(card._dirty, true); assert.equal(card._filters[0].name, "Newer name");
  assert.equal(card._pendingSave, null); assert.match(card._saveMessage, /newer unsaved changes/);
});

for (const [name, selector, value] of [
  ["focused_text_survives_sensor_update", '[data-field="search_query"]', "Christmas lights"],
  ["focused_number_survives_sensor_update", '[data-field="search_pool_size"]', "15"],
  ["focused_select_survives_sensor_update", '[data-action="set-recurring-date"][data-endpoint="start"][data-part="month"]', "03"],
]) {
  scenario(name, async () => {
    const { card, update, q } = mount([{ name: "Search", selection: "search", search_query: "Christmas", recurring_date_ranges: [{ start: "02-29" }] }]);
    const field = q(selector);
    // The current pool control is a slider. Exercise the delegated guard with
    // a native numeric text-entry control as well, without replacing card code.
    if (name === "focused_number_survives_sensor_update") field.setAttribute("type", "number");
    input(field, value); update({ status: "fetching" });
    assert.ok(q(selector) === field, "sensor update replaced the editing control");
    assert.ok(card.shadowRoot.activeElement === field, "sensor update lost input focus");
    assert.equal(field.value, value);
    await change(field, value); field.blur(); await flush();
    assert.equal(card._editing, false); assert.ok(q(selector) !== field, "committed input froze rendering");
  });
}

scenario("focus_without_edit_does_not_freeze", async () => {
  const { update, q } = mount(); const field = q('[data-field="name"]'); field.focus(); update({ status: "fetching" });
  assert.ok(q('[data-field="name"]') !== field, "focus alone blocked rendering");
});

scenario("save_click_after_edit_dispatches", async () => {
  const { card, calls, q } = mount([{ name: "Search", selection: "search", search_query: "Christmas" }]);
  input(q('[data-field="search_query"]'), "Christmas lights");
  const save = q('[data-action="save"]'); fire(save, "pointerdown", { composed: true });
  save.focus(); await flush(); fire(save, "pointerup", { composed: true }); click(save); advance(0); await flush();
  assert.equal(calls.length, 1); assert.equal(calls[0].payload.filters[0].search_query, "Christmas lights");
  assert.ok(card._pendingSave);
});

scenario("touch_then_click_dispatches_once", async () => {
  const { calls, q } = mount(); const button = q('[data-action="refresh-now"]');
  fire(button, "touchstart", { composed: true }); fire(button, "touchend", { composed: true }); click(button); await flush();
  assert.equal(calls.length, 1); advance(400); click(q('[data-action="refresh-now"]')); assert.equal(calls.length, 2);
});

for (const event of ["touchmove", "scroll", "touchcancel"]) {
  scenario(`gesture_${event}_cancels_action`, async () => {
    const { calls, q } = mount(); const button = q('[data-action="refresh-now"]');
    fire(button, "touchstart", { composed: true }); fire(button, event, { composed: true }); fire(button, "touchend", { composed: true });
    await flush(); assert.equal(calls.length, 0);
  });
}

scenario("native_select_touch_is_not_prevented", async () => {
  const { q } = mount([{ name: "Birthday", recurring_date_ranges: [{ start: "02-29" }] }]);
  const select = dateField(q, "start", "month"); fire(select, "touchstart", { composed: true });
  const event = fire(select, "touchend", { composed: true }); assert.equal(event.defaultPrevented, false);
});

(async () => {
  const report = {};
  for (const [name, run] of Object.entries(cases)) {
    timers.clear(); notes.length = 0; shimDocument.body.innerHTML = "";
    try { await run(); assert.deepEqual(notes, []); report[name] = { passed: true }; }
    catch (error) { report[name] = { passed: false, detail: error.stack }; }
  }
  process.stdout.write(JSON.stringify(report) + "\n");
})().catch((error) => { process.stderr.write(error.stack); process.exitCode = 1; });
