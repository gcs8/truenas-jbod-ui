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
  assert.equal(summarizeMappingHealth([{ slot: 0, state: "healthy" }], 1).tone, "ok");
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
