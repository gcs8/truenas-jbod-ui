"use strict";

// A saved bay assignment whose disk was not found is not a disk until the bay
// itself was read as occupied. The slot shapes below are what the inventory
// produces (tests/test_ses_unknown_occupancy_consumers.py pins them): an
// unread bay has present=false and presence_known=false.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const APP_SOURCE = fs.readFileSync(path.resolve(__dirname, "../../app/static/app.js"), "utf8");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const bodyStart = source.indexOf("{", start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

function load(names, prelude = "") {
  // Every consumer asks the one occupancy reader; load it beside them.
  if (APP_SOURCE.includes("function slotOccupancy(") && !names.includes("slotOccupancy")) {
    names = ["slotOccupancy", ...names];
  }
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(`${prelude}\n${names.map((name) => functionSource(APP_SOURCE, name)).join("\n")}\nthis.loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.loaded;
}

const UNREAD_WITH_SAVED_ASSIGNMENT = {
  slot: 0, state: "unmapped", present: false, presence_known: false,
  mapping_source: "unknown", serial: "SANITIZED-MAPPED",
};
const OBSERVED_UNMATCHED_DISK = { slot: 1, state: "unmapped", present: true, mapping_source: "unknown" };

test("an unread bay with a saved assignment is not counted as a disk", () => {
  const { summarizeMappingHealth } = load(["summarizeMappingHealth"]);
  const result = summarizeMappingHealth([UNREAD_WITH_SAVED_ASSIGNMENT], 1);
  assert.deepEqual(
    { populated: result.populated, unmatched: result.unmatched, unverified: result.unverified, unknown: result.unknown },
    { populated: 0, unmatched: 0, unverified: 1, unknown: 0 },
  );
  assert.equal(result.headline, "1 saved bay assignment cannot be verified right now.");
  assert.equal(result.tone, "unknown");
  assert.equal(
    summarizeMappingHealth([UNREAD_WITH_SAVED_ASSIGNMENT, { ...UNREAD_WITH_SAVED_ASSIGNMENT, slot: 2 }], 2).headline,
    "2 saved bay assignments cannot be verified right now.",
  );
});

test("an observed disk that matches no assignment is still reported", () => {
  const { summarizeMappingHealth } = load(["summarizeMappingHealth"]);
  const result = summarizeMappingHealth([OBSERVED_UNMATCHED_DISK, UNREAD_WITH_SAVED_ASSIGNMENT], 2);
  assert.equal(result.unmatched, 1);
  assert.equal(result.unverified, 1);
  assert.equal(result.populated, 1);
  assert.equal(result.headline, "1 disk is not matched to a bay. Click it to assign one.");
  assert.equal(result.tone, "warning");
  assert.equal(summarizeMappingHealth([{ slot: 0, state: "healthy", present: true }], 1).tone, "ok");
  assert.equal(summarizeMappingHealth([{ slot: 0, state: "unknown" }], 1).tone, "unknown");
});

test("a storage-view saved assignment with no live disk is not counted as a disk", () => {
  const { mappingHealthScope, summarizeMappingHealth } = load(["mappingHealthScope", "summarizeMappingHealth"]);
  const scope = mappingHealthScope({}, {
    label: "Saved view",
    slots: [
      { slot_index: 0, state: "unmapped", occupied: false, source: "manual_mapping" },
      { slot_index: 1, state: "healthy", occupied: true, source: "snapshot_slot" },
    ],
    slot_layout: [[0, 1]],
  });
  const result = summarizeMappingHealth(scope.slots, scope.layoutSlotCount);
  assert.equal(result.unverified, 1);
  assert.equal(result.unmatched, 0);
  assert.equal(result.populated, 1);
});

test("the heatmap does not treat an unread bay as holding a disk", () => {
  const { heatmapEntryOccupied } = load(["heatmapEntryOccupied"]);
  assert.equal(heatmapEntryOccupied({ slot: UNREAD_WITH_SAVED_ASSIGNMENT }), false);
  assert.equal(heatmapEntryOccupied({ slot: OBSERVED_UNMATCHED_DISK }), true);
  assert.equal(heatmapEntryOccupied({ slot: { state: "healthy", serial: "SANITIZED-A", present: true } }), true);
});

// A saved chassis view backed by another enclosure than the page snapshot
// draws its tiles from the runtime slot, not from a live bay.
const UNREAD_RUNTIME_SLOT = { slot_index: 0, slot_label: "00", occupied: false, presence_known: false, state: "unknown" };
const EMPTY_RUNTIME_SLOT = { slot_index: 1, slot_label: "01", occupied: false, state: "empty" };

test("a storage-view tile for an unread bay does not say Empty", () => {
  const { storageViewRuntimeShortLabel, storageViewRuntimeSecondaryLabel } = load([
    "storageViewRuntimeShortLabel", "storageViewRuntimeSecondaryLabel",
  ]);
  assert.equal(storageViewRuntimeShortLabel(UNREAD_RUNTIME_SLOT), "Unknown");
  assert.equal(storageViewRuntimeSecondaryLabel(UNREAD_RUNTIME_SLOT), "Whether this slot holds a disk is not known.");
  assert.equal(storageViewRuntimeShortLabel(EMPTY_RUNTIME_SLOT), "Empty");
  assert.equal(
    storageViewRuntimeSecondaryLabel(EMPTY_RUNTIME_SLOT),
    "No live disk is currently landing in this storage-view slot.",
  );
  assert.equal(storageViewRuntimeShortLabel({ ...EMPTY_RUNTIME_SLOT, occupied: true, serial: "SANITIZED-A" }), "SANITIZED-A");
});

test("the fabric note for an unread storage-view bay does not call it empty", () => {
  for (const [slot, expected] of [
    [UNREAD_RUNTIME_SLOT, "Whether this slot holds a disk is not known."],
    [EMPTY_RUNTIME_SLOT, "This slot is empty."],
  ]) {
    const { sasFabricViewSlotNote } = load(["sasFabricViewSlotNote"], `
      const state = { selectedSlot: ${slot.slot_index} };
      function getSelectedStorageViewRuntime() { return { template_id: "ses-auto" }; }
      function sasFabricBayForViewSlot() { return null; }
      function getSelectedStorageViewRuntimeSlot() { return ${JSON.stringify(slot)}; }
    `);
    assert.equal(sasFabricViewSlotNote(), expected);
  }
});

// One reader decides occupancy for every page consumer below.
test("the occupancy reader says present, empty or unknown", () => {
  const { slotOccupancy } = load(["slotOccupancy"]);
  assert.equal(slotOccupancy({ present: true }), "present");
  assert.equal(slotOccupancy({ present: false }), "empty");
  assert.equal(slotOccupancy({ present: false, presence_known: false }), "unknown");
  assert.equal(slotOccupancy({ occupied: true }), "present");
  assert.equal(slotOccupancy({ occupied: false }), "empty");
  assert.equal(slotOccupancy({ occupied: false, presence_known: false }), "unknown");
  assert.equal(slotOccupancy({ state: "healthy" }), "unknown");
  assert.equal(slotOccupancy(null), "unknown");
});

// The real CORE `sesutil show` row `Slot 00 - - - Identify` gives this slot
// (tests/test_ses_unknown_occupancy_consumers.py pins it): a locate light on a
// bay nobody read.
const UNREAD_IDENTIFY = { slot: 0, state: "identify", present: false, presence_known: false, mapping_source: "unknown" };
const UNREAD_FAULT = { ...UNREAD_IDENTIFY, state: "fault" };

test("a locate or fault light on an unread bay is not counted as a disk", () => {
  const { summarizeMappingHealth } = load(["summarizeMappingHealth"]);
  for (const slot of [UNREAD_IDENTIFY, UNREAD_FAULT]) {
    const result = summarizeMappingHealth([slot], 1);
    assert.deepEqual(
      { matched: result.matched, populated: result.populated, unknown: result.unknown, empty: result.empty },
      { matched: 0, populated: 0, unknown: 1, empty: 0 },
      slot.state,
    );
    assert.equal(result.headline, "1 bay has an unknown state.");
    assert.equal(result.tone, "unknown");
  }
  // Controls: a locate light on a bay read as occupied is a disk; one on a
  // bay read as empty is an empty bay.
  const lit = summarizeMappingHealth([{ ...UNREAD_IDENTIFY, present: true, presence_known: undefined }], 1);
  assert.equal(lit.matched, 1);
  assert.equal(lit.headline, "The only disk is in a known bay.");
  const litEmpty = summarizeMappingHealth([{ ...UNREAD_IDENTIFY, presence_known: undefined }], 1);
  assert.equal(litEmpty.empty, 1);
  assert.equal(litEmpty.headline, "No disks are reported in this view.");
});

test("the storage-view mapping scope keeps unread occupancy", () => {
  const { mappingHealthScope, summarizeMappingHealth } = load(["mappingHealthScope", "summarizeMappingHealth"]);
  const scope = mappingHealthScope({}, {
    label: "Saved view",
    slots: [
      { slot_index: 0, state: "identify", occupied: false, presence_known: false, source: "snapshot_slot" },
      { slot_index: 1, occupied: false, presence_known: false, source: "placeholder" },
      { slot_index: 2, state: "empty", occupied: false, source: "placeholder" },
      { slot_index: 3, state: "healthy", occupied: true, source: "snapshot_slot" },
    ],
    slot_layout: [[0, 1, 2, 3]],
  });
  assert.equal(scope.slots[0].presence_known, false);
  assert.equal(scope.slots[1].state, "unknown");
  const result = summarizeMappingHealth(scope.slots, scope.layoutSlotCount);
  assert.deepEqual(
    { matched: result.matched, empty: result.empty, unknown: result.unknown, populated: result.populated },
    { matched: 1, empty: 1, unknown: 2, populated: 1 },
  );
});

test("storage-view tiles for unread bays do not say Empty on any template", () => {
  const fns = load([
    "storageViewRuntimeTilePrimary", "storageViewRuntimeTileSummary", "storageViewRuntimeTertiaryLabel",
    "buildStorageViewTooltipLines", "slotPrimaryLabel",
  ], `
    function escapeHtml(value) { return String(value); }
    function persistentIdLabel() { return "Persistent ID"; }
    function formatSlotHealth(value) { return value; }
    function isPlaceholderHintLabel() { return false; }
    function getLiveBackedStorageViewSlot() { return null; }
    function appendSmartTooltipMetrics() {}
    function formatLogicalUnitIdValue() { return "n/a"; }
    function appendHeatmapTooltipLines(lines) { return lines; }
  `);
  for (const kind of ["nvme_carrier", "boot_devices", "ses_enclosure"]) {
    assert.equal(fns.storageViewRuntimeTilePrimary(UNREAD_RUNTIME_SLOT, { kind }), "Unknown", kind);
    assert.equal(fns.storageViewRuntimeTileSummary(UNREAD_RUNTIME_SLOT, { kind }), "Whether this slot holds a disk is not known.", kind);
    assert.equal(fns.storageViewRuntimeTilePrimary(EMPTY_RUNTIME_SLOT, { kind }), "Empty", kind);
  }
  assert.equal(fns.storageViewRuntimeTileSummary(EMPTY_RUNTIME_SLOT, { kind: "nvme_carrier" }), "No live disk matched yet.");
  assert.equal(fns.storageViewRuntimeTertiaryLabel({ ...UNREAD_RUNTIME_SLOT, slot_size: "2280" }), "2280");
  const tooltip = (slot) => fns.buildStorageViewTooltipLines(slot, { label: "View" }, null)[1];
  assert.equal(tooltip(UNREAD_RUNTIME_SLOT), "Whether this slot holds a disk is not known.");
  assert.equal(tooltip(EMPTY_RUNTIME_SLOT), "No live disk matched");
  assert.equal(tooltip({ ...EMPTY_RUNTIME_SLOT, occupied: true, serial: "SANITIZED-A" }), "SANITIZED-A");
  assert.equal(fns.slotPrimaryLabel({ state: "other", present: true }), "PRESENT");
  assert.equal(fns.slotPrimaryLabel({ state: "other", present: false, presence_known: false }), "UNKNOWN");
});

test("the embedded boot tile does not call an unread slot empty", () => {
  const { buildEmbeddedBootMediaRuntimeTileMarkup } = load(["buildEmbeddedBootMediaRuntimeTileMarkup"], `
    function escapeHtml(value) { return String(value); }
    function stateLabel() { return "state"; }
  `);
  assert.match(buildEmbeddedBootMediaRuntimeTileMarkup(UNREAD_RUNTIME_SLOT), /Whether this slot holds a disk is not known\./);
  assert.match(
    buildEmbeddedBootMediaRuntimeTileMarkup(EMPTY_RUNTIME_SLOT),
    /No live boot device is currently landing in this storage-view slot\./,
  );
});

test("missing runtime rows are drawn unknown, not empty", () => {
  // Every fallback the page builds for a layout bay with no runtime row.
  const fallbacks = APP_SOURCE.match(/occupied: false,\n\s+presence_known: false,\n\s+state: "unknown",/g) || [];
  assert.equal(fallbacks.length, 3);
  assert.doesNotMatch(APP_SOURCE, /occupied: false,\n\s+state: "empty",/);
});

test("the restore preview never calls an unread bay empty", () => {
  const { mappingImportPreviewMessage } = load(["mappingImportPreviewMessage"]);
  const warning = { enclosure_id: "enc-a", slot: 0, field: "serial", backup: "SANITIZED-BACKUP", current: null };
  const unread = mappingImportPreviewMessage({ disk_warnings: [{ ...warning, occupancy: "unknown" }] });
  assert.doesNotMatch(unread, /bay now empty|now holds a different disk/);
  assert.match(unread, /The current disk cannot be checked \(1\): enc-a slot 0 \[backup Serial SANITIZED-BACKUP\]/);
  assert.match(unread, /Confirming restores the backup's assignments to these bays anyway\./);
  // Control: a bay read as empty keeps its wording.
  const empty = mappingImportPreviewMessage({ disk_warnings: [warning] });
  assert.match(empty, /This bay now holds a different disk \(1\): enc-a slot 0 \[backup Serial SANITIZED-BACKUP, bay now empty\]/);
  assert.doesNotMatch(empty, /cannot be checked/);
});

// metricRows has a default parameter with braces; take the body from ") {".
function bodySource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const bodyStart = source.indexOf(") {", start) + 2;
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

test("Storage Fabric renders an unread bay as unknown, not no", () => {
  const FABRIC_SOURCE = fs.readFileSync(path.resolve(__dirname, "../../app/static/sas_fabric_view.js"), "utf8");
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(`
    function escapeHtml(value) { return String(value); }
    function list(value) { return Array.isArray(value) ? value : []; }
    function diagnosticSummary(value) { return value; }
    ${["formatValue", "kvRow", "metricRows"].map((name) => bodySource(FABRIC_SOURCE, name)).join("\n")}
    this.metricRows = metricRows;
  `, sandbox);
  // The metric values the server builds (tests/test_ses_unknown_occupancy_consumers.py).
  assert.match(sandbox.metricRows({ present: "unknown" }), /<span>present<\/span>\s*<strong>unknown<\/strong>/);
  assert.match(sandbox.metricRows({ present: false }), /<span>present<\/span>\s*<strong>no<\/strong>/);
});
