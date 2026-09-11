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


const SMART_PREFETCH_FUNCTIONS = [
  "ensureSmartSummaryOnInteraction",
  "smartPrefetchPending",
  "scheduleSmartPrefetch",
  "currentSmartPrefetchScopeKey",
  "runSmartPrefetch",
  "shouldUseSingleSmartPrefetchRequest",
  "applySmartPrefetchPayload",
  "applySmartPrefetchError",
  "candidateSlotsForSmartPrefetch",
  "isSmartEntryCurrent",
  "isSmartEntryInFlight",
  "smartSummaryAgeMs",
  "getSmartSummaryEntry",
  "getSmartCacheKey",
];

function flushMicrotasks() {
  return new Promise((resolve) => setImmediate(resolve));
}

// Loads the real candidate filter, cache-entry predicates and prefetch runner so
// the queued-batch handover is exercised end to end instead of through stubs.
function loadSmartPrefetchHarness(options = {}) {
  const slots = options.slots || [
    { slot: 1, present: true, device_name: "sdb" },
    { slot: 2, present: true, device_name: "sdc" },
  ];
  const state = {
    snapshotMode: false,
    smartSummaries: {},
    smartSummaryGeneration: 1,
    smartPrefetchTimerId: null,
    smartPrefetchRunning: false,
    smartPrefetchToken: 0,
    smartPrefetchScopeKey: null,
    selectedSystemId: "sysA",
    selectedEnclosureId: "encA",
    hoveredSlot: null,
    selectedSlot: null,
    heatmap: { enabled: false },
    snapshot: { slots, selected_system_id: "sysA", selected_enclosure_id: "encA" },
  };
  const timers = [];
  const calls = { batches: [], ensure: 0, tooltip: 0, complete: 0, failures: [] };
  const window = {
    setTimeout(callback) {
      timers.push(callback);
      return timers.length;
    },
    clearTimeout() {},
  };
  const loaded = loadFunctions(SMART_PREFETCH_FUNCTIONS, {
    state,
    window,
    Math,
    Set,
    Object,
    String,
    console,
    SMART_SUMMARY_CACHE_TTL_MS: 30000,
    SMART_PREFETCH_STALE_MS: 15000,
    SMART_PREFETCH_DELAY_MS: 120,
    SMART_PREFETCH_STRATEGY: options.strategy || "single",
    SMART_PREFETCH_SINGLE_THRESHOLD: 128,
    SMART_PREFETCH_CHUNK_SIZE: 24,
    SMART_PREFETCH_BATCH_CONCURRENCY: 2,
    getSlotById: (slotNumber) => slots.find((slot) => slot.slot === slotNumber) || null,
    getPreloadedSmartSummariesForEnclosureId: () => ({}),
    currentLiveEnclosureId: () => "encA",
    updateSmartPrefetchViews: () => {},
    logSmartPrefetchFailure: (message, error) => { calls.failures.push(String(error && error.message)); },
    completeUiPerfSmart: () => { calls.complete += 1; },
    refreshHoveredTooltip: () => { calls.tooltip += 1; },
    ensureSmartSummary: async () => { calls.ensure += 1; },
    requestSmartBatchForSlots: async (batch) => {
      calls.batches.push(batch.map((slot) => slot.slot));
      if (options.failBatch) {
        throw new Error("smart-batch unavailable");
      }
      return { summaries: batch.map((slot) => ({ slot: slot.slot, summary: { available: true, temperature_c: 31 } })) };
    },
  });
  return { ...loaded, state, slots, timers, calls };
}

test("a bay hovered during the queued window stays eligible for that batch", async () => {
  const harness = loadSmartPrefetchHarness();
  const { state, slots, timers, calls } = harness;

  harness.scheduleSmartPrefetch();
  assert.equal(timers.length, 1, "the prefetch must be queued behind a timer");
  assert.equal(harness.smartPrefetchPending(), true);

  state.hoveredSlot = 1;
  await harness.ensureSmartSummaryOnInteraction(slots[0]);
  assert.equal(calls.ensure, 0, "a covered bay must not race the batch with its own request");
  assert.equal(calls.tooltip, 1, "the tooltip must still be told the bay is loading");

  const candidates = harness.candidateSlotsForSmartPrefetch().map((slot) => slot.slot);
  assert.deepEqual(candidates, [1, 2], "the hovered bay must remain a candidate for the queued batch");

  timers[0]();
  await flushMicrotasks();
  await flushMicrotasks();

  assert.deepEqual(calls.batches, [[1, 2]], "the queued batch must request the hovered bay");
  const entry = state.smartSummaries[harness.getSmartCacheKey(slots[0])];
  assert.equal(entry.loading, false, "the hovered bay must not stay loading after the batch resolves");
  assert.deepEqual(entry.data, { available: true, temperature_c: 31 });
  assert.equal(harness.candidateSlotsForSmartPrefetch().length, 0, "a resolved bay is no longer a candidate");
});

test("a failed queued batch clears the hovered bay instead of leaving it loading", async () => {
  const harness = loadSmartPrefetchHarness({ failBatch: true });
  const { state, slots, timers, calls } = harness;

  harness.scheduleSmartPrefetch();
  state.hoveredSlot = 1;
  await harness.ensureSmartSummaryOnInteraction(slots[0]);
  assert.ok(
    harness.candidateSlotsForSmartPrefetch().some((slot) => slot.slot === 1),
    "the hovered bay must be owned by the queued batch",
  );

  timers[0]();
  await flushMicrotasks();
  await flushMicrotasks();

  assert.deepEqual(calls.batches, [[1, 2]]);
  const entry = state.smartSummaries[harness.getSmartCacheKey(slots[0])];
  assert.equal(entry.loading, false, "a failed batch must not leave the bay loading forever");
  assert.equal(entry.refreshing, false);
  assert.equal(entry.data.available, false);
  assert.equal(entry.data.message, "smart-batch unavailable");
});

test("a cancelled prefetch run leaves the hovered bay eligible for the next run", async () => {
  const harness = loadSmartPrefetchHarness();
  const { state, slots, timers, calls } = harness;

  harness.scheduleSmartPrefetch();
  state.hoveredSlot = 1;
  await harness.ensureSmartSummaryOnInteraction(slots[0]);

  // A scope change supersedes the queued run before its timer fires.
  state.smartPrefetchToken += 1;
  timers[0]();
  await flushMicrotasks();
  assert.deepEqual(calls.batches, [], "the superseded run must not send a request");

  const candidates = harness.candidateSlotsForSmartPrefetch().map((slot) => slot.slot);
  assert.deepEqual(candidates, [1, 2], "the hovered bay must survive a cancelled run as a candidate");
});
