"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(source, name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = source.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);

  const parametersEnd = source.indexOf(")", start);
  const bodyStart = source.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
    const next = source[index + 1];
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
      if (character === "\\") {
        index += 1;
      } else if (character === quote) {
        quote = null;
      }
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
    if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) {
        return source.slice(start, index + 1);
      }
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunction(source, name, context = {}) {
  const sandbox = vm.createContext({ ...context });
  vm.runInContext(`${functionSource(source, name)}\nthis.__loaded = ${name};`, sandbox, {
    filename: `${name}.behavior.js`,
  });
  return { fn: sandbox.__loaded, context: sandbox };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

test("overlapping manual, selection, and auto refreshes schedule exactly one next refresh", async () => {
  const requests = [deferred(), deferred(), deferred()];
  let requestIndex = 0;
  let scheduled = 0;
  const state = {
    snapshotMode: false,
    latestRefreshToken: 0,
    refreshesInFlight: 0,
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    storageViewsRuntimeLoading: false,
    sasFabric: { open: false },
    history: { configured: false },
    uiPerf: { currentRun: null },
  };
  const { fn: refreshSnapshot } = loadFunction(APP_SOURCE, "refreshSnapshot", {
    state,
    cancelAutoRefreshTimer() {},
    beginUiPerfRun() { return null; },
    refreshStatusMessage() { return "refreshing"; },
    setStatus() {},
    buildSelectionParams() { return new URLSearchParams(); },
    URLSearchParams,
    fetchJson() { return requests[requestIndex++].promise; },
    applySnapshot() {},
    invalidateHistoryCaches() {},
    renderAll() {},
    fetchStorageViewRuntime() { return Promise.resolve(); },
    fetchSasFabric() { return Promise.resolve(); },
    waitForNextPaint() { return Promise.resolve(); },
    uiPerfNow() { return 0; },
    archiveUiPerfRun() {},
    renderUiPerfPanel() {},
    refreshHistoryStatus() { return Promise.resolve(); },
    completeUiPerfHistory() {},
    setUiPerfSmartPending() {},
    candidateSlotsForSmartPrefetch() { return []; },
    currentSmartPrefetchScopeKey() { return "scope"; },
    scheduleSmartPrefetch() {},
    ensureHeatmapData() {},
    maybeFinalizeUiPerfRun() {},
    markHistoryCachesStale() {},
    renderHistoryPanel() {},
    renderHeatmapControls() {},
    renderStorageViewsRuntime() {},
    scheduleAutoRefresh() { scheduled += 1; },
  });

  const manual = refreshSnapshot(true, "manual-refresh");
  const selection = refreshSnapshot(false, "system-switch");
  const automatic = refreshSnapshot(false, "auto-refresh");

  requests[1].resolve({ selected_system_id: "system-a" });
  await selection;
  requests[2].resolve({ selected_system_id: "system-a" });
  await automatic;
  requests[0].resolve({ selected_system_id: "system-a" });
  await manual;

  assert.equal(state.refreshesInFlight, 0);
  assert.equal(scheduled, 1);
});

test("successful inventory refresh invalidates history before render and a failed refresh preserves it", async () => {
  const state = {
    snapshotMode: false,
    latestRefreshToken: 0,
    refreshesInFlight: 0,
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    storageViewsRuntimeLoading: false,
    sasFabric: { open: false },
    history: { configured: false },
    uiPerf: { currentRun: null },
  };
  const events = [];
  let shouldFail = false;
  const { fn: refreshSnapshot } = loadFunction(APP_SOURCE, "refreshSnapshot", {
    state,
    cancelAutoRefreshTimer() {},
    beginUiPerfRun() { return null; },
    refreshStatusMessage() { return "refreshing"; },
    setStatus() {},
    buildSelectionParams() { return new URLSearchParams(); },
    URLSearchParams,
    async fetchJson() {
      if (shouldFail) throw new Error("inventory unavailable");
      return { selected_system_id: "system-a" };
    },
    applySnapshot() { events.push("apply"); },
    invalidateHistoryCaches() { events.push("invalidate"); },
    renderAll() { events.push("render"); },
    fetchStorageViewRuntime() { return Promise.resolve(); },
    fetchSasFabric() { return Promise.resolve(); },
    waitForNextPaint() { return Promise.resolve(); },
    uiPerfNow() { return 0; },
    archiveUiPerfRun() {},
    renderUiPerfPanel() {},
    refreshHistoryStatus() { return Promise.resolve(); },
    completeUiPerfHistory() {},
    setUiPerfSmartPending() {},
    candidateSlotsForSmartPrefetch() { return []; },
    currentSmartPrefetchScopeKey() { return "scope"; },
    scheduleSmartPrefetch() {},
    ensureHeatmapData() {},
    maybeFinalizeUiPerfRun() {},
    markHistoryCachesStale(error) { events.push(`stale:${error.message || error}`); },
    renderHistoryPanel() {},
    renderHeatmapControls() {},
    renderStorageViewsRuntime() {},
    scheduleAutoRefresh() {},
  });

  await refreshSnapshot(true);
  assert.deepEqual(events.slice(0, 3), ["apply", "invalidate", "render"]);

  events.length = 0;
  shouldFail = true;
  await refreshSnapshot(true);
  assert.equal(events.includes("invalidate"), false);
  assert.deepEqual(events, ["stale:inventory unavailable"]);
});

test("mapping draft survives repeated renders for the same selected slot", () => {
  const state = {
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    selectedStorageViewRuntimeId: "",
    mappingFormScopeKey: null,
    snapshot: {},
  };
  const mappingForm = {
    serial: { value: "" },
    device_name: { value: "" },
    gptid: { value: "" },
    notes: { value: "" },
  };
  const { fn: mappingFormScopeKey } = loadFunction(APP_SOURCE, "mappingFormScopeKey", { state });
  const { fn: syncMappingFormForSlot } = loadFunction(APP_SOURCE, "syncMappingFormForSlot", {
    state,
    mappingForm,
    mappingFormScopeKey,
  });

  syncMappingFormForSlot({ slot: 7, serial: "server-old", device_name: "da7", gptid: "gpt-old", notes: "old" });
  mappingForm.serial.value = "operator draft";
  mappingForm.notes.value = "typed while SMART loaded";

  syncMappingFormForSlot({ slot: 7, serial: "server-new", device_name: "da7", gptid: "gpt-new", notes: "new" });

  assert.equal(mappingForm.serial.value, "operator draft");
  assert.equal(mappingForm.notes.value, "typed while SMART loaded");
});

test("mapping form initializes from the newly selected slot scope", () => {
  const state = {
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    selectedStorageViewRuntimeId: "",
    mappingFormScopeKey: null,
    snapshot: {},
  };
  const mappingForm = {
    serial: { value: "" },
    device_name: { value: "" },
    gptid: { value: "" },
    notes: { value: "" },
  };
  const { fn: mappingFormScopeKey } = loadFunction(APP_SOURCE, "mappingFormScopeKey", { state });
  const { fn: syncMappingFormForSlot } = loadFunction(APP_SOURCE, "syncMappingFormForSlot", {
    state,
    mappingForm,
    mappingFormScopeKey,
  });

  syncMappingFormForSlot({ slot: 7, serial: "slot-seven", device_name: "da7", gptid: "gpt-7", notes: "seven" });
  mappingForm.serial.value = "draft for seven";
  syncMappingFormForSlot({ slot: 8, serial: "slot-eight", device_name: "da8", gptid: "gpt-8", notes: "eight" });

  assert.equal(mappingForm.serial.value, "slot-eight");
  assert.equal(mappingForm.device_name.value, "da8");
  assert.equal(mappingForm.gptid.value, "gpt-8");
  assert.equal(mappingForm.notes.value, "eight");
});

test("successful mapping save renders the authoritative snapshot instead of the old draft", async () => {
  const state = { snapshotMode: false, selectedSlot: 7, mappingFormScopeKey: "system|enc||7" };
  let renderedScopeKey = "not-rendered";
  let appliedSnapshot = null;
  const events = [];
  class FakeFormData {
    get(name) {
      return name === "serial" ? "operator draft" : null;
    }
  }
  const { fn: saveMapping } = loadFunction(APP_SOURCE, "saveMapping", {
    state,
    FormData: FakeFormData,
    mappingForm: {},
    getSlotById() { return { slot: 7, slot_label: "07" }; },
    setStatus() {},
    async sendScopedRequest() {
      return { snapshot: { marker: "server-normalized" } };
    },
    applySnapshot(snapshot) { appliedSnapshot = snapshot; events.push("apply"); },
    invalidateHistoryCaches() { events.push("invalidate"); },
    renderAll() { renderedScopeKey = state.mappingFormScopeKey; events.push("render"); },
    scheduleSmartPrefetch() {},
  });

  await saveMapping({ preventDefault() {} });

  assert.deepEqual(appliedSnapshot, { marker: "server-normalized" });
  assert.equal(state.mappingFormScopeKey, null);
  assert.equal(renderedScopeKey, null);
  assert.deepEqual(events, ["apply", "invalidate", "render"]);
});

test("successful mapping clear invalidates history before render", async () => {
  const state = { snapshotMode: false, selectedSlot: 7, mappingFormScopeKey: "system|enc||7" };
  const events = [];
  const { fn: clearMapping } = loadFunction(APP_SOURCE, "clearMapping", {
    state,
    window: { confirm: () => true },
    getSlotById() { return { slot: 7, slot_label: "07" }; },
    setStatus() {},
    async sendScopedRequest() { return { snapshot: { marker: "cleared" } }; },
    applySnapshot() { events.push("apply"); },
    invalidateHistoryCaches() { events.push("invalidate"); },
    renderAll() { events.push("render"); },
    scheduleSmartPrefetch() {},
  });

  await clearMapping();

  assert.deepEqual(events, ["apply", "invalidate", "render"]);
});

test("mapping import invalidates the selected draft before rendering imported state", async () => {
  const state = { snapshotMode: false, mappingFormScopeKey: "system|enc||7" };
  let renderedScopeKey = "not-rendered";
  let appliedSnapshot = null;
  const events = [];
  const mappingImportFile = { value: "selected-file" };
  const { fn: importMappingsFromFile } = loadFunction(APP_SOURCE, "importMappingsFromFile", {
    state,
    mappingImportFile,
    setStatus() {},
    async sendScopedRequest() {
      return { snapshot: { marker: "imported" }, imported: 1 };
    },
    applySnapshot(snapshot) { appliedSnapshot = snapshot; events.push("apply"); },
    invalidateHistoryCaches() { events.push("invalidate"); },
    renderAll() { renderedScopeKey = state.mappingFormScopeKey; events.push("render"); },
    scheduleSmartPrefetch() {},
  });

  await importMappingsFromFile({
    name: "mappings.json",
    async text() { return "{}"; },
  });

  assert.deepEqual(appliedSnapshot, { marker: "imported" });
  assert.equal(state.mappingFormScopeKey, null);
  assert.equal(renderedScopeKey, null);
  assert.equal(mappingImportFile.value, "");
  assert.deepEqual(events, ["apply", "invalidate", "render"]);
});

test("stale history failure cannot clear the newer request state", async () => {
  const oldRequest = deferred();
  const newRequest = deferred();
  let activeTarget = { slot: { slot: 1 }, cacheKey: "slot-1", fetchUrl: "/history/1" };
  const state = {
    snapshotMode: false,
    history: {
      panelLoading: false,
      panelError: null,
      panelRequestToken: 0,
      panelFreshness: null,
      generation: 0,
      inFlight: {},
      slotCache: {},
    },
  };
  const { fn: loadHistoryForSelectedSlot } = loadFunction(APP_SOURCE, "loadHistoryForSelectedSlot", {
    state,
    getSelectedHistoryTarget() { return activeTarget; },
    isHistoryAvailable() { return true; },
    getLiveHistoryCacheEntry() { return null; },
    renderHistoryPanel() {},
    async fetchLiveHistoryPayload(target) {
      const payload = await (target.fetchUrl === "/history/1" ? oldRequest.promise : newRequest.promise);
      state.history.slotCache[target.cacheKey] = {
        payload,
        fetchedAt: 1,
        lastAccessedAt: 1,
        generation: state.history.generation,
      };
      return payload;
    },
  });

  const oldLoad = loadHistoryForSelectedSlot(true);
  activeTarget = { slot: { slot: 2 }, cacheKey: "slot-2", fetchUrl: "/history/2" };
  const newLoad = loadHistoryForSelectedSlot(true);

  oldRequest.reject(new Error("stale history failure"));
  await oldLoad;
  assert.equal(state.history.panelError, null);
  assert.equal(state.history.panelLoading, true);

  newRequest.resolve({ samples: ["new"] });
  await newLoad;
  assert.equal(state.history.panelError, null);
  assert.equal(state.history.panelLoading, false);
  assert.deepEqual(state.history.slotCache["slot-2"].payload, { samples: ["new"] });
});

test("cached history selection invalidates an older in-flight request", async () => {
  const oldRequest = deferred();
  let activeTarget = { slot: { slot: 1 }, cacheKey: "slot-1", fetchUrl: "/history/1" };
  const state = {
    snapshotMode: false,
    history: {
      panelLoading: false,
      panelError: null,
      panelRequestToken: 0,
      panelFreshness: null,
      generation: 0,
      inFlight: {},
      slotCache: {
        "slot-2": {
          payload: { samples: ["cached"] },
          fetchedAt: 1,
          lastAccessedAt: 1,
          generation: 0,
        },
      },
    },
  };
  const { fn: loadHistoryForSelectedSlot } = loadFunction(APP_SOURCE, "loadHistoryForSelectedSlot", {
    state,
    getSelectedHistoryTarget() { return activeTarget; },
    isHistoryAvailable() { return true; },
    getLiveHistoryCacheEntry(target) {
      const entry = state.history.slotCache[target];
      return entry ? { ...entry, freshness: "fresh" } : null;
    },
    renderHistoryPanel() {},
    fetchLiveHistoryPayload() { return oldRequest.promise; },
  });

  const oldLoad = loadHistoryForSelectedSlot(true);
  activeTarget = { slot: { slot: 2 }, cacheKey: "slot-2", fetchUrl: "/history/2" };
  await loadHistoryForSelectedSlot(false);
  oldRequest.reject(new Error("stale history failure"));
  await oldLoad;

  assert.equal(state.history.panelError, null);
  assert.equal(state.history.panelLoading, false);
});

test("dedicated fabric refresh ignores a response for an older selection", async () => {
  const requests = [];
  const state = {
    selectedSystemId: "system-a",
    selectedEnclosureId: null,
    loading: false,
    error: null,
    snapshot: {},
    fabric: null,
    refreshRequestToken: 0,
  };
  function scopedUrl(endpoint, { force = false } = {}) {
    return `${endpoint}?system_id=${state.selectedSystemId}&force=${force}`;
  }
  function fetchJson(url) {
    const request = deferred();
    requests.push({ url, ...request });
    return request.promise;
  }
  function requestFor(fragment) {
    return requests.find((request) => request.url.includes(fragment));
  }
  const { fn: refreshFabric } = loadFunction(FABRIC_SOURCE, "refreshFabric", {
    state,
    render() {},
    scopedUrl,
    fetchJson,
    applySnapshot(snapshot) {
      state.snapshot = snapshot;
      state.selectedSystemId = snapshot.selected_system_id || state.selectedSystemId;
    },
    applyFabric(fabric) { state.fabric = fabric; },
  });

  const oldRefresh = refreshFabric(false);
  state.selectedSystemId = "system-b";
  const newRefresh = refreshFabric(false);

  requestFor("/api/inventory?system_id=system-b").resolve({ selected_system_id: "system-b" });
  await flushPromises();
  requestFor("/api/sas-fabric?system_id=system-b").resolve({ system_id: "system-b", marker: "new" });
  await newRefresh;

  requestFor("/api/inventory?system_id=system-a").resolve({ selected_system_id: "system-a" });
  await flushPromises();
  const staleFabricRequest = requestFor("/api/sas-fabric?system_id=system-a");
  if (staleFabricRequest) {
    staleFabricRequest.resolve({ system_id: "system-a", marker: "stale" });
  }
  await oldRefresh;

  assert.equal(state.selectedSystemId, "system-b");
  assert.deepEqual(state.fabric, { system_id: "system-b", marker: "new" });
});

for (const [label, source] of [["main UI", APP_SOURCE], ["dedicated fabric", FABRIC_SOURCE]]) {
  test(`${label} fetchJson reports HTTP status for a non-JSON error body`, async () => {
    const { fn: fetchJson } = loadFunction(source, "fetchJson", {
      fetch: async () => ({
        ok: false,
        status: 502,
        async json() { throw new SyntaxError("Unexpected token <"); },
      }),
    });

    await assert.rejects(() => fetchJson("/api/example"), /Request failed with 502/);
  });
}
