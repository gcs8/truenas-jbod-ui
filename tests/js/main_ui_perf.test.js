"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const STYLES = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = APP_SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    const next = APP_SOURCE[index + 1];
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (blockComment) {
      if (character === "*" && next === "/") {
        blockComment = false;
        index += 1;
      }
      continue;
    }
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "/" && next === "/") {
      lineComment = true;
      index += 1;
      continue;
    }
    if (character === "/" && next === "*") {
      blockComment = true;
      index += 1;
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

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ Number, Boolean, Array, Map, Promise, Date, ...context });
  const source = names.map(functionSource).join("\n");
  vm.runInContext(`${source}\nthis.__loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.__loaded;
}

function countingArray(items, counter) {
  return new Proxy(items, {
    get(target, property, receiver) {
      if (typeof property === "string" && /^\d+$/.test(property)) counter.reads += 1;
      return Reflect.get(target, property, receiver);
    },
  });
}

test("bay lookups index the slot list once instead of scanning it per call", () => {
  const counter = { reads: 0 };
  const slots = Array.from({ length: 200 }, (_, index) => ({ slot: index, device_name: `sd${index}` }));
  const state = { snapshot: { slots: countingArray(slots, counter) } };
  const { getSlotById } = loadFunctions(["getSlotById"], { state });

  for (let index = 0; index < 200; index += 1) {
    assert.equal(getSlotById(index)?.device_name, `sd${index}`);
  }
  assert.ok(counter.reads <= 400, `indexed lookups should not read ${counter.reads} slots`);
  assert.equal(getSlotById(999), null);

  state.snapshot.slots = [{ slot: 5, device_name: "replaced" }];
  assert.equal(getSlotById(5)?.device_name, "replaced");
  assert.equal(getSlotById(6), null);
});

test("storage-view slot lookups index the view once per slot list", () => {
  const counter = { reads: 0 };
  const viewSlots = Array.from({ length: 120 }, (_, index) => ({ slot_index: index, slot_label: `Slot ${index + 1}` }));
  const view = { id: "chassis", slots: countingArray(viewSlots, counter) };
  const state = {};
  const { getSelectedStorageViewRuntimeSlot } = loadFunctions(
    ["getSelectedStorageViewRuntimeSlot", "storageViewRuntimeSlotByIndex"],
    { state, getSelectedStorageViewRuntime: () => view },
  );

  for (let index = 0; index < 120; index += 1) {
    assert.equal(getSelectedStorageViewRuntimeSlot(index)?.slot_label, `Slot ${index + 1}`);
  }
  assert.ok(counter.reads <= 240, `indexed lookups should not read ${counter.reads} view slots`);
  assert.equal(getSelectedStorageViewRuntimeSlot("7")?.slot_index, 7);
  assert.equal(getSelectedStorageViewRuntimeSlot(null), null);
  assert.equal(getSelectedStorageViewRuntimeSlot(500), null);
});

test("selection-state pass resolves the storage view once per pass, not per tile", () => {
  let viewLookups = 0;
  const view = { id: "chassis", slots: [] };
  const makeTile = (slot) => ({
    dataset: { slot },
    classList: { toggle() {}, remove() {} },
    setAttribute() {},
  });
  const tiles = Array.from({ length: 60 }, (_, index) => makeTile(String(index)));
  const { refreshGridSelectionState } = loadFunctions(
    ["refreshGridSelectionState", "fabricSlotNumberForGridTile"],
    {
      state: { selectedSlot: null },
      grid: { querySelectorAll() { return tiles; } },
      getSelectedPeerContext() { return { active: false, peerSlots: new Set() }; },
      sasFabricSelectedSlotSet() { return new Set([1]); },
      getSelectedStorageViewRuntime() { viewLookups += 1; return view; },
      storageViewRuntimeSlotByIndex(candidateView, slotIndex) {
        assert.equal(candidateView, view);
        return { slot_index: slotIndex };
      },
      getLiveBackedStorageViewSlot(candidateView, slot) { return { slot: slot.slot_index }; },
    },
  );

  refreshGridSelectionState();
  assert.equal(viewLookups, 1);
});

test("hover fetches defer to a queued batch prefetch that already covers the bay", async () => {
  const calls = { ensure: 0, tooltip: 0 };
  const slot = { slot: 4, device_name: "sde", present: true };
  const state = {
    snapshotMode: false,
    smartSummaries: {},
    smartSummaryGeneration: 3,
    smartPrefetchTimerId: 17,
    smartPrefetchRunning: false,
    hoveredSlot: 4,
  };
  const { ensureSmartSummaryOnInteraction } = loadFunctions(
    ["ensureSmartSummaryOnInteraction", "smartPrefetchPending"],
    {
      state,
      getSmartCacheKey: (candidate) => `key-${candidate.slot}`,
      isSmartEntryCurrent: (entry) => Boolean(entry?.current),
      isSmartEntryInFlight: (entry) => Boolean(entry?.loading),
      candidateSlotsForSmartPrefetch: () => [slot],
      ensureSmartSummary: async () => { calls.ensure += 1; },
      refreshHoveredTooltip: () => { calls.tooltip += 1; },
    },
  );

  await ensureSmartSummaryOnInteraction(slot);
  assert.equal(calls.ensure, 0, "a covered bay must not start its own request");
  assert.equal(state.smartSummaries["key-4"].loading, true);
  assert.equal(state.smartSummaries["key-4"].generation, 3);
  assert.equal(calls.tooltip, 1);

  await ensureSmartSummaryOnInteraction(slot);
  assert.equal(calls.ensure, 0, "a bay already marked loading stays with the batch");

  state.smartPrefetchTimerId = null;
  state.smartPrefetchRunning = false;
  await ensureSmartSummaryOnInteraction({ slot: 9, device_name: "sdj", present: true });
  assert.equal(calls.ensure, 1, "with no batch pending the bay fetches on its own");

  state.smartPrefetchRunning = true;
  await ensureSmartSummaryOnInteraction({ slot: 12, device_name: "sdm", present: true });
  assert.equal(calls.ensure, 2, "a bay the batch does not cover still fetches on its own");

  state.smartSummaries["key-4"] = { current: true, data: { temperature_c: 30 } };
  await ensureSmartSummaryOnInteraction(slot);
  assert.equal(calls.ensure, 3, "a current entry goes through the normal path");
});

function fakeGrid() {
  const listeners = {};
  const grid = {
    addEventListener(type, handler) { listeners[type] = handler; },
    contains() { return true; },
  };
  return { grid, listeners };
}

function fakeTile(slot) {
  const tile = {
    dataset: { slot },
    closest() { return tile; },
    contains() { return false; },
  };
  return tile;
}

test("one delegated handler set serves saved-view tiles and live tiles", () => {
  const { grid, listeners } = fakeGrid();
  const calls = [];
  const view = { id: "chassis", slots: [{ slot_index: 3, slot_label: "Slot 4" }] };
  let selectedView = view;
  const state = { hoveredSlot: null, selectedSlot: null };
  const { bindDelegatedGridInteractions } = loadFunctions(
    [
      "bindDelegatedGridInteractions",
      "delegatedGridTile",
      "delegatedGridSlot",
      "requestDelegatedSlotSmartSummary",
      "endDelegatedTileHover",
      "storageViewRuntimeSlotByIndex",
    ],
    {
      state,
      grid,
      getSelectedStorageViewRuntime: () => selectedView,
      getSlotById: (slotNumber) => (slotNumber === 8 ? { slot: 8, device_name: "sdi" } : null),
      formatSlotLabel: (slotNumber) => String(slotNumber).padStart(2, "0"),
      ensureStorageViewSmartSummary: async (candidateView, slot) => { calls.push(["view-smart", candidateView.id, slot.slot_index]); },
      ensureSmartSummaryOnInteraction: async (slot) => { calls.push(["live-smart", slot.slot]); },
      refreshHoveredTooltip: (tile) => { calls.push(["tooltip", tile.dataset.slot]); },
      positionSlotTooltip: () => {},
      positionSlotTooltipFromElement: () => {},
      hideSlotTooltip: () => { calls.push(["hide"]); },
      selectSlot: (slotNumber) => { state.selectedSlot = slotNumber; calls.push(["select", slotNumber]); },
      clearSelectedSlot: () => { state.selectedSlot = null; calls.push(["clear"]); },
    },
  );

  bindDelegatedGridInteractions();
  assert.deepEqual(Object.keys(listeners).sort(), ["click", "focusin", "focusout", "mousemove", "mouseout", "mouseover"]);

  const viewTile = fakeTile("3");
  listeners.mouseover({ target: viewTile, relatedTarget: null, clientX: 1, clientY: 1 });
  assert.equal(state.hoveredSlot, 3);
  assert.deepEqual(calls.at(-1), ["view-smart", "chassis", 3]);
  listeners.click({ target: viewTile });
  assert.deepEqual(calls.at(-1), ["select", 3]);
  listeners.click({ target: viewTile });
  assert.deepEqual(calls.at(-1), ["clear"]);
  listeners.mouseout({ target: viewTile, relatedTarget: null });
  assert.equal(state.hoveredSlot, null);

  selectedView = null;
  const liveTile = fakeTile("8");
  listeners.focusin({ target: liveTile });
  assert.equal(state.hoveredSlot, 8);
  assert.deepEqual(calls.at(-1), ["live-smart", 8]);
  listeners.focusout({ target: liveTile, relatedTarget: null });
  assert.equal(state.hoveredSlot, null);

  const unknownTile = fakeTile("42");
  listeners.mouseover({ target: unknownTile, relatedTarget: null, clientX: 1, clientY: 1 });
  assert.equal(state.hoveredSlot, 42, "bays the snapshot does not list still get a tooltip");
  assert.deepEqual(calls.at(-1), ["live-smart", 42]);

  listeners.mouseover({ target: { closest() { return null; } }, relatedTarget: null });
  assert.equal(state.hoveredSlot, 42, "events outside a tile leave the hover state alone");
});

test("tiles carry no listeners of their own", () => {
  for (const name of ["renderStorageViewGrid", "renderLiveNvmeCarrierGrid", "renderGrid"]) {
    assert.doesNotMatch(functionSource(name), /addEventListener\(/, `${name} must rely on the delegated grid handlers`);
  }
  assert.equal(APP_SOURCE.includes("function bindStorageViewTileInteractions("), false);
  assert.equal(APP_SOURCE.includes("function delegatedLiveSlot("), false);
});

test("stylesheet no longer carries rules for removed markup", () => {
  for (const selector of [".system-setup-dialog", ".field input.is-readonly", ".history-chart-grid"]) {
    assert.equal(STYLES.includes(selector), false, `${selector} has no markup left to style`);
  }
});
