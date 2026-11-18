"use strict";

// Main page Connections map (renderSasFabricMap in app.js) on a CORE read
// with no or partial controller data: the bays the inventory knows are drawn
// in an explained inventory group, as on the Storage Fabric page, and never
// inside a controller lane. Synthetic CORE fixture only.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const CASES = JSON.parse(
  fs.readFileSync(path.join(ROOT, "tests/fixtures/sas_fabric/core_no_controller_fabric.json"), "utf8"),
);

function textOf(html) {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function appFunctionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = APP_SOURCE.indexOf("{", APP_SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

// The shipping map, lane and flat bay-chip renderers; layout, selection and
// node-card helpers are stubbed (no enclosure layout, nothing selected).
const SHIPPING = [
  "sasFabricList",
  "sasFabricSortedSlots",
  "sasFabricNodeMap",
  "isStorageFabricPayload",
  "fabricCountState",
  "sasFabricCountPhrase",
  "sasFabricNoControllerText",
  "sasFabricLaneListEmptyText",
  "renderSasFabricCompactNodes",
  "renderSasFabricFlatBayChips",
  "renderSasFabricBayChips",
  "renderSasFabricLane",
  "renderSasFabricMap",
];
// Helpers the fix adds; absent on main, so the map is loaded without them.
const OPTIONAL = ["sasFabricInventoryBaySlots", "sasFabricCountsIncomplete", "sasFabricCountsUnvouched", "renderSasFabricInventoryGroup"];

function loadMap(slots) {
  const sandbox = vm.createContext({
    state: { sasFabric: { data: null, expandedSlotLists: {} }, snapshot: { slots }, selectedSlot: null },
    escapeHtml: (value) => String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
    formatSlotLabel: (slot) => String(slot).padStart(2, "0"),
    sasFabricDisplayLabel: (item) => item?.display_label || item?.label || null,
    sasFabricSelectionTouchesNode: () => false,
    sasFabricSelectionTouchesSlots: () => false,
    sasFabricSelectedSlotSet: () => new Set(),
    sasFabricViewSlotForBay: () => null,
    sasFabricListExpanded: () => false,
    renderSasFabricExpandToggle: () => "",
    renderSasFabricNodeButton: (node, { label = null, meta = null } = {}) => `<button data-sas-fabric-node="${node.id}">${label || node.label || node.id} ${meta || ""}</button>`,
    sasFabricNodeMeta: () => "",
    renderSasFabricPathButton: (item) => `<button data-sas-fabric-trace="${item.id}"></button>`,
    formatSasFabricKind: String,
    getSelectedStorageViewRuntime: () => null,
    buildViewProfile: () => null,
    activeLayoutRows: () => [],
    buildChassisGeometry: () => ({}),
    buildLayoutGridRows: () => [],
  });
  const names = [...SHIPPING, ...OPTIONAL.filter((name) => APP_SOURCE.includes(`function ${name}(`))];
  vm.runInContext(`${names.map(appFunctionSource).join("\n")}\nthis.renderMap = renderSasFabricMap;`, sandbox);
  return sandbox.renderMap;
}

function render(fabric, slots) {
  const html = loadMap(slots)(fabric);
  return { html, text: textOf(html), chips: [...html.matchAll(/data-sas-fabric-slot="(\d+)"/g)].map((match) => Number(match[1])) };
}

function inventorySlots(fabric) {
  return fabric.nodes.filter((node) => node.kind === "bay").flatMap((node) => node.related_slots).sort((a, b) => a - b);
}

test("no controller data: the inventory bays are drawn in an explained group", () => {
  const { fabric, slots } = CASES.no_controller_bays;
  const page = render(fabric, slots);
  assert.deepEqual(page.chips, inventorySlots(fabric), page.text);
  assert.match(page.text, /Controller data could not be read\. Check the SSH setup/, page.text);
  assert.match(page.text, /Not attached to a controller\. Controller data could not be read, so these come from the inventory\./, page.text);
  assert.doesNotMatch(page.text, /No mapped bays/, page.text);
});

test("partial controller read: unattached inventory bays are drawn beside the lane, not in it", () => {
  const { fabric, slots } = CASES.unreadable_second_unit;
  const page = render(fabric, slots);
  assert.deepEqual(page.chips, inventorySlots(fabric), page.text);
  assert.match(page.text, /Not attached to a read controller\. Some controller data was not read, so these come from the inventory\./, page.text);
  // The lane itself still says it has no mapped bays: attachment is not inferred.
  const lane = page.html.split('<section class="sas-fabric-lane')[1];
  assert.match(textOf(lane), /No mapped bays/);
  assert.doesNotMatch(lane, /data-sas-fabric-slot=/);
});

test("partial controller read: a claimed bay stays in its lane and is drawn once", () => {
  // The read controller claims bay 0; the inventory knows bays 0 and 1.
  const fabric = structuredClone(CASES.unreadable_second_unit.fabric);
  fabric.nodes.push(structuredClone(CASES.no_controller_mixed.fabric.nodes.find((node) => node.id === "bay:1")));
  fabric.controllers[0].related_slots = [0];
  fabric.nodes.find((node) => node.id === "controller:mpr0").related_slots = [0];
  const slots = [structuredClone(CASES.unreadable_second_unit.slots[0]), structuredClone(CASES.no_controller_mixed.slots[1])];
  const page = render(fabric, slots);
  assert.deepEqual(inventorySlots(fabric), [0, 1]);
  assert.deepEqual(page.chips, [0, 1], page.text);
  const sections = page.html.split('<section class="sas-fabric-lane').slice(1);
  assert.equal(sections.length, 2, page.text);
  const chipsOf = (html) => [...html.matchAll(/data-sas-fabric-slot="(\d+)"/g)].map((match) => Number(match[1]));
  assert.doesNotMatch(sections[0], /sas-fabric-inventory-lane/);
  assert.deepEqual(chipsOf(sections[0]), [0], textOf(sections[0]));
  assert.match(sections[1], /sas-fabric-inventory-lane/);
  assert.deepEqual(chipsOf(sections[1]), [1], textOf(sections[1]));
  assert.match(textOf(sections[1]), /Not attached to a read controller\. Some controller data was not read/);
});

test("controller read whose completeness was not reported keeps unattached bays drawn", () => {
  const { controller_read: _ignored, ...raw } = CASES.unreadable_second_unit.fabric.raw;
  const fabric = { ...CASES.unreadable_second_unit.fabric, raw };
  const page = render(fabric, CASES.unreadable_second_unit.slots);
  assert.deepEqual(page.chips, inventorySlots(fabric), page.text);
  assert.match(page.text, /Whether the controller data was read whole was not reported, so these come from the inventory\./, page.text);
});

test("control: a whole read draws no inventory group and its lane is unchanged", () => {
  const { fabric, slots } = CASES.complete;
  const page = render(fabric, slots);
  assert.doesNotMatch(page.text, /come from the inventory|Not attached/, page.text);
  assert.equal((page.html.match(/<section class="sas-fabric-lane/g) || []).length, 1);
});

test("control: no bays in the inventory and storage payloads keep the plain notice", () => {
  const noBays = { ...CASES.no_controller_bays.fabric, nodes: CASES.no_controller_bays.fabric.nodes.filter((node) => node.kind !== "bay") };
  const bare = render(noBays, []);
  assert.deepEqual(bare.chips, []);
  assert.doesNotMatch(bare.text, /come from the inventory/);
  const storage = render({ ...CASES.no_controller_bays.fabric, platform: "scale" }, CASES.no_controller_bays.slots);
  assert.deepEqual(storage.chips, []);
  assert.match(storage.text, /No storage source rows are available yet\./);
});
