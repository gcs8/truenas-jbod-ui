"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../app/static/app.js"), "utf8");
const descriptor = "SANITIZED-DESCRIPTOR 4T";
const unknown = {
  slot: 0, slot_index: 0, slot_label: "00", present: true, occupied: true,
  state: "unknown", size_bytes: null, size_human: null,
  raw_status: { reported_size: descriptor },
};

function functionSource(name) {
  const match = source.match(new RegExp(`  function ${name}\\([^]*?\\n  }`));
  assert.ok(match, `${name} exists`);
  return match[0];
}

function load(names, overrides = {}) {
  const code = names.map(functionSource).join("\n");
  const sandbox = {};
  // Unrelated panel effects and SMART formatters do not affect the capacity rows.
  for (const [, name] of code.matchAll(/\b([a-z]\w*)\(/g)) sandbox[name] = () => "";
  Object.assign(sandbox, {
    escapeHtml: (value) => String(value ?? "").replaceAll("<", "&lt;"),
    stateLabel: (slot) => slot.state,
    slotOccupancy: (slot) => slot.occupied || slot.present ? "present" : "empty",
    ...overrides,
  });
  vm.createContext(sandbox);
  vm.runInContext(`${code}\nthis.api = { ${names.join(", ")} };`, sandbox);
  return sandbox.api;
}

function detailHarness(storageSlot = null, snapshotMode = false) {
  const node = () => ({ classList: { add() {}, remove() {}, toggle() {} },
    innerHTML: "", querySelector() { return null; } });
  const grid = node();
  const view = storageSlot ? { id: "synthetic-view", label: "Synthetic view" } : null;
  const names = ["renderLiveSlotDetail", "renderDetail", "kvRow", "kvRowIfMeaningful", "isEmptyBay"];
  const api = load(names, {
    state: { snapshotMode, selectedSlot: 0, history: { panelOpen: false } },
    detailEmpty: node(), detailContent: node(), detailSecondary: node(), detailLedControls: node(),
    detailSlotTitle: node(), detailStatePill: node(), detailKvGrid: grid, ledButtons: [],
    mappingEmpty: node(), mappingForm: node(),
    getSelectedStorageViewRuntime: () => view,
    getSelectedStorageViewRuntimeSlot: () => storageSlot,
    getLiveBackedStorageViewSlot: () => null,
    getSlotById: () => unknown,
    currentPlatform: () => "core",
  });
  return { api, grid };
}

function row(html, label) {
  return html.match(new RegExp(`<div class="kv-label">${label}</div>\\s*<div class="kv-value"[^>]*>([^<]*)`))?.[1];
}

for (const snapshotMode of [false, true]) {
  test(`Slot Details explicitly reports unread size, offline=${snapshotMode}`, () => {
    const { api, grid } = detailHarness(null, snapshotMode);
    api.renderLiveSlotDetail(unknown);
    assert.equal(row(grid.innerHTML, "Size"), "not read yet");
    assert.equal(row(grid.innerHTML, "Enclosure descriptor"), descriptor);
  });

  test(`storage-view detail explicitly reports unread capacity, offline=${snapshotMode}`, () => {
    const { api, grid } = detailHarness(unknown, snapshotMode);
    api.renderDetail();
    assert.equal(row(grid.innerHTML, "Capacity"), "not read yet");
    assert.doesNotMatch(grid.innerHTML, /SANITIZED-DESCRIPTOR/);
  });
}

test("a real capacity is preserved in both detail paths", () => {
  const known = { ...unknown, size_bytes: 4_000_000_000_000, size_human: "3.6 TiB (4.0 TB)" };
  const live = detailHarness();
  live.api.renderLiveSlotDetail(known);
  assert.equal(row(live.grid.innerHTML, "Size"), known.size_human);
  const storage = detailHarness(known);
  storage.api.renderDetail();
  assert.equal(row(storage.grid.innerHTML, "Capacity"), known.size_human);
});

test("main bay tile and heat-map tooltip never use the enclosure descriptor as a size", () => {
  const api = load(["buildLiveSlotTileMarkup", "liveSlotSecondaryLabel", "heatmapTooltipLines"], {
    slotPrimaryLabel: () => "Disk present",
  });
  assert.doesNotMatch(api.buildLiveSlotTileMarkup(unknown), /SANITIZED-DESCRIPTOR|4T/);
  const tooltip = api.heatmapTooltipLines("0", {
    definition: { label: "Temperature", format: () => "not read" },
    values: new Map([["0", { value: null }]]),
  });
  assert.doesNotMatch(tooltip.join(" "), /SANITIZED-DESCRIPTOR|4T/);
});

test("embedded boot-media cards show unread capacity instead of omitting it", () => {
  const api = load(["buildEmbeddedBootMediaRuntimeTileMarkup"]);
  assert.match(api.buildEmbeddedBootMediaRuntimeTileMarkup(unknown), /not read yet/);
  assert.doesNotMatch(api.buildEmbeddedBootMediaRuntimeTileMarkup(unknown), /SANITIZED-DESCRIPTOR/);
});
