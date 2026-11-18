"use strict";

// Storage Fabric page, inventory rows without a physical location: the host
// line and the inventory-only groups call a row a disk only when the
// inventory confirms a disk is present in it. A row whose occupancy is
// unknown or confirmed empty is an inventory entry, not a disk. Physical
// bays stay bays whatever their occupancy. Synthetic CORE fixture only.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const CASES = JSON.parse(
  fs.readFileSync(path.join(ROOT, "tests/fixtures/sas_fabric/core_no_controller_fabric.json"), "utf8"),
);

function textOf(html) {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function renderLanes(fabric, slots) {
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots }, fabric } },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", dataset: {}, className: "", closest: () => null });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    window.renderTest = { renderMap, state };
  })();`, sandbox);
  const api = sandbox.window.renderTest;
  api.state.fabric = fabric;
  api.state.snapshot = { ...api.state.snapshot, slots };
  api.state.loading = false;
  api.state.error = null;
  api.state.mode = "lanes";
  api.renderMap();
  return textOf(elements.get("fabric-map-panel").innerHTML);
}

// Occupancy of the second row as SlotView serializes it.
const OCCUPANCY = {
  present: { present: true, state: "healthy" },
  unknown: { present: false, presence_known: false, state: "unknown", device_name: null },
  empty: { present: false, state: "empty", device_name: null },
  // Nobody read the bay, yet the row still carries a positive flag: only
  // `presence_known: false` keeps it from being counted as a disk.
  unread_flagged_present: { present: true, presence_known: false, state: "unknown" },
};

// Two inventory rows, the first one holding a disk. `located` marks both
// rows as physical bays; otherwise neither has a physical location.
function inventoryCase(name, occupancy, located = false) {
  const fabric = structuredClone(CASES[name].fabric);
  const slots = structuredClone(CASES[name].slots);
  for (const slot of slots) slot.physical_location_known = located;
  for (const item of [...fabric.nodes, ...fabric.traces]) {
    if (item.kind !== "bay") continue;
    item.metrics = { ...item.metrics, physical_location_known: located };
  }
  Object.assign(slots[1], OCCUPANCY[occupancy]);
  return { fabric, slots };
}

// The partial-read case carries one bay; add a second unclaimed one.
function partialReadCase(occupancy, located = false) {
  const base = CASES.unreadable_second_unit;
  const mixed = CASES.no_controller_mixed;
  const fabric = structuredClone(base.fabric);
  fabric.nodes.push(structuredClone(mixed.fabric.nodes.find((node) => node.id === "bay:1")));
  fabric.traces.push(structuredClone(mixed.fabric.traces.find((trace) => trace.id === "bay:1")));
  const slots = [structuredClone(base.slots[0]), structuredClone(mixed.slots[1])];
  for (const slot of slots) slot.physical_location_known = located;
  for (const item of [...fabric.nodes, ...fabric.traces]) {
    if (item.kind === "bay") item.metrics = { ...item.metrics, physical_location_known: located };
  }
  Object.assign(slots[1], OCCUPANCY[occupancy]);
  return { fabric, slots };
}

for (const occupancy of ["unknown", "empty", "unread_flagged_present"]) {
  test(`host line does not call an unlocated row with ${occupancy} occupancy a disk`, () => {
    const { fabric, slots } = inventoryCase("no_controller_mixed", occupancy);
    const map = renderLanes(fabric, slots);
    assert.doesNotMatch(map, /\b2 disks\b/, map);
    assert.match(map, /paths not read \/ 2 inventory entries, 1 confirmed disk\b/, map);
  });

  test(`no-controller inventory group with ${occupancy} occupancy says entries, not disks`, () => {
    const { fabric, slots } = inventoryCase("no_controller_mixed", occupancy);
    const map = renderLanes(fabric, slots);
    assert.match(map, /Controller data could not be read\. The entries below come from the inventory\. Not every entry is confirmed to hold a disk\./, map);
    assert.match(map, /Impacted Entries/, map);
    assert.doesNotMatch(map, /The disks below|Impacted Disks|\bbays?\b/i, map);
  });

  test(`unattached inventory group with ${occupancy} occupancy says entries, not disks`, () => {
    const { fabric, slots } = partialReadCase(occupancy);
    const map = renderLanes(fabric, slots);
    assert.match(map, /Some controller data was not read, so these entries come from the inventory\. Not every entry is confirmed to hold a disk\./, map);
    assert.doesNotMatch(map, /these disks come from the inventory/, map);
  });
}

test("control: unlocated rows that all hold a disk stay disks", () => {
  const { fabric, slots } = inventoryCase("no_controller_mixed", "present");
  const map = renderLanes(fabric, slots);
  assert.match(map, /paths not read \/ 2 disks\b/, map);
  assert.match(map, /The disks below come from the inventory\./, map);
  assert.match(map, /Impacted Disks/, map);
  assert.doesNotMatch(map, /entries|entry/, map);
  const partial = renderLanes(...Object.values(partialReadCase("present")));
  assert.match(partial, /so these disks come from the inventory\./, partial);
});

// `count` unlocated inventory rows with no controller data; only the first
// holds a confirmed disk unless `allPresent`.
function manyRowsCase(count, allPresent = false) {
  const { fabric, slots } = inventoryCase("no_controller_mixed", allPresent ? "present" : "unknown");
  const node = fabric.nodes.find((item) => item.id === "bay:1");
  const trace = fabric.traces.find((item) => item.id === "bay:1");
  for (let slot = 2; slot < count; slot += 1) {
    fabric.nodes.push({ ...structuredClone(node), id: `bay:${slot}`, slot, related_slots: [slot] });
    fabric.traces.push({ ...structuredClone(trace), id: `bay:${slot}`, slots: [slot] });
    slots.push({ ...structuredClone(slots[1]), slot, slot_label: String(slot).padStart(2, "0") });
  }
  return { fabric, slots };
}

test("inventory group overflow does not call unknown occupants disks (120/121 boundary)", () => {
  const atLimit = renderLanes(...Object.values(manyRowsCase(120)));
  assert.match(atLimit, /120 inventory entries, 1 confirmed disk\b/, atLimit);
  assert.doesNotMatch(atLimit, /\+\d+ (?:disks?|entr(?:y|ies)|bays?)\b/, atLimit);
  const over = renderLanes(...Object.values(manyRowsCase(121)));
  assert.match(over, /121 inventory entries, 1 confirmed disk\b/, over);
  assert.match(over, /Impacted Entries/, over);
  assert.match(over, /\+1 entry\b/, over);
  assert.doesNotMatch(over, /\+1 disks?\b/, over);
});

test("control: inventory group overflow of confirmed disks still says disk", () => {
  const over = renderLanes(...Object.values(manyRowsCase(121, true)));
  assert.match(over, /121 disks\b/, over);
  assert.match(over, /\+1 disk\b/, over);
  assert.doesNotMatch(over, /entries|entry/, over);
});

for (const occupancy of ["unknown", "empty", "present"]) {
  test(`control: physical bays are counted as bays with ${occupancy} occupancy`, () => {
    const { fabric, slots } = inventoryCase("no_controller_mixed", occupancy, true);
    const map = renderLanes(fabric, slots);
    assert.match(map, /paths not read \/ 2 bays\b/, map);
    assert.match(map, /The bays below come from the inventory\./, map);
    assert.match(map, /Impacted Bays/, map);
    assert.doesNotMatch(map, /entries|entry/, map);
  });
}
