"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

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
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunction(name, context = {}) {
  const sandbox = vm.createContext({ ...context });
  vm.runInContext(`${functionSource(APP_SOURCE, name)}\nthis.__loaded = ${name};`, sandbox, {
    filename: `${name}.behavior.js`,
  });
  return sandbox.__loaded;
}

function visibleClassList(hidden = false) {
  return { contains: (name) => name === "hidden" && hidden };
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

test("auto refresh pause reason covers hidden documents and active mapping drafts", () => {
  const state = { mappingFormDirty: true };
  const mappingForm = { classList: visibleClassList(false) };
  const document = { visibilityState: "hidden" };
  const mappingEditorHasUnsavedChanges = loadFunction("mappingEditorHasUnsavedChanges", { state, mappingForm });
  const autoRefreshPauseReason = loadFunction("autoRefreshPauseReason", {
    state,
    mappingForm,
    document,
    mappingEditorHasUnsavedChanges,
  });

  assert.equal(autoRefreshPauseReason(), "hidden");
  document.visibilityState = "visible";
  assert.equal(autoRefreshPauseReason(), "mapping");
  mappingForm.classList = visibleClassList(true);
  assert.equal(autoRefreshPauseReason(), null);
});

test("paused scheduling creates no timers and visible resume creates exactly one", () => {
  let pauseReason = "hidden";
  let timerSets = 0;
  let timerClears = 0;
  const state = {
    snapshotMode: false,
    autoRefresh: true,
    refreshIntervalSeconds: 15,
    refreshesInFlight: 0,
    timerId: null,
    timerScheduledAt: 0,
    timerDueAt: 0,
    timerDelayMs: 0,
  };
  const scheduleAutoRefresh = loadFunction("scheduleAutoRefresh", {
    state,
    autoRefreshPauseReason: () => pauseReason,
    cancelAutoRefreshTimer() {
      if (state.timerId) timerClears += 1;
      state.timerId = null;
      state.timerScheduledAt = 0;
      state.timerDueAt = 0;
      state.timerDelayMs = 0;
    },
    renderTimingSurfaces() {},
    ensureTimingTick() {},
    Date: { now: () => 1000 },
    window: {
      setTimeout() {
        timerSets += 1;
        return timerSets;
      },
    },
    async refreshSnapshot() {},
  });

  scheduleAutoRefresh();
  scheduleAutoRefresh();
  assert.equal(timerSets, 0);
  pauseReason = null;
  scheduleAutoRefresh();
  assert.equal(timerSets, 1);
  assert.equal(timerClears, 0);
  assert.equal(state.timerDelayMs, 15000);
});

test("visibility return restarts a full interval without an immediate refresh", () => {
  const events = [];
  const document = { visibilityState: "hidden" };
  const handleAutoRefreshVisibilityChange = loadFunction("handleAutoRefreshVisibilityChange", {
    document,
    cancelAutoRefreshTimer() { events.push("cancel"); },
    scheduleAutoRefresh() { events.push("schedule"); },
    renderTimingSurfaces() { events.push("render"); },
  });

  handleAutoRefreshVisibilityChange();
  assert.deepEqual(events, ["cancel", "render"]);
  document.visibilityState = "visible";
  handleAutoRefreshVisibilityChange();
  assert.deepEqual(events, ["cancel", "render", "schedule"]);
});

test("dirty mapping manual refresh requires confirmation and cancel performs no request", async () => {
  const events = [];
  const requestManualRefresh = loadFunction("requestManualRefresh", {
    mappingEditorHasUnsavedChanges: () => true,
    window: { confirm: () => false },
    setStatus(message) { events.push(`status:${message}`); },
    async refreshSnapshot() { events.push("refresh"); },
  });

  assert.equal(await requestManualRefresh(), false);
  assert.equal(events.includes("refresh"), false);
  assert.match(events[0], /canceled/i);
});

test("clean or confirmed dirty mapping manual refresh remains available", async () => {
  let dirty = false;
  let confirms = 0;
  let refreshes = 0;
  const requestManualRefresh = loadFunction("requestManualRefresh", {
    mappingEditorHasUnsavedChanges: () => dirty,
    window: { confirm: () => { confirms += 1; return true; } },
    setStatus() {},
    async refreshSnapshot(force, reason) {
      assert.equal(force, true);
      assert.equal(reason, "manual-refresh");
      refreshes += 1;
    },
  });

  assert.equal(await requestManualRefresh(), true);
  assert.equal(confirms, 0);
  dirty = true;
  assert.equal(await requestManualRefresh(), true);
  assert.equal(confirms, 1);
  assert.equal(refreshes, 2);
});

test("mapping input and change events mark the calibration editor dirty", () => {
  assert.match(APP_SOURCE, /mappingForm\.addEventListener\("input", markMappingFormDirty\)/);
  assert.match(APP_SOURCE, /mappingForm\.addEventListener\("change", markMappingFormDirty\)/);
});

test("resetting a replaced mapping form clears its dirty state", () => {
  const state = {
    mappingFormDirty: true,
    mappingFormScopeKey: "system|enclosure||1",
    refreshesInFlight: 0,
  };
  const mappingForm = {
    serial: { value: "draft" },
    device_name: { value: "draft" },
    gptid: { value: "draft" },
    notes: { value: "draft" },
  };
  let schedules = 0;
  const resetMappingForm = loadFunction("resetMappingForm", {
    state,
    mappingForm,
    scheduleAutoRefresh() { schedules += 1; },
  });

  resetMappingForm();
  assert.equal(state.mappingFormDirty, false);
  assert.equal(state.mappingFormScopeKey, null);
  assert.equal(schedules, 1);
});

test("synchronizing a saved mapping restarts polling after dirty state clears", () => {
  const state = {
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    selectedStorageViewRuntimeId: "",
    mappingFormScopeKey: null,
    mappingFormDirty: true,
    refreshesInFlight: 0,
    snapshot: {},
  };
  const mappingForm = {
    serial: { value: "draft" },
    device_name: { value: "draft" },
    gptid: { value: "draft" },
    notes: { value: "draft" },
  };
  let schedules = 0;
  const mappingFormScopeKey = loadFunction("mappingFormScopeKey", { state });
  const syncMappingFormForSlot = loadFunction("syncMappingFormForSlot", {
    state,
    mappingForm,
    mappingFormScopeKey,
    scheduleAutoRefresh() { schedules += 1; },
  });

  syncMappingFormForSlot({ slot: 1, serial: "saved", device_name: "da1", gptid: "disk-1", notes: "saved" });

  assert.equal(state.mappingFormDirty, false);
  assert.equal(mappingForm.serial.value, "saved");
  assert.equal(schedules, 1);
});

test("a draft started during an automatic refresh blocks its response render", async () => {
  const request = deferred();
  const events = [];
  const state = {
    snapshotMode: false,
    mappingFormDirty: false,
    latestRefreshToken: 0,
    refreshesInFlight: 0,
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    storageViewsRuntimeLoading: false,
    sasFabric: { open: false },
    history: { configured: false },
    uiPerf: { currentRun: null },
  };
  const refreshSnapshot = loadFunction("refreshSnapshot", {
    state,
    cancelAutoRefreshTimer() {},
    beginUiPerfRun() { return null; },
    refreshStatusMessage() { return "refreshing"; },
    setStatus(message) { events.push(`status:${message}`); },
    buildSelectionParams() { return new URLSearchParams(); },
    URLSearchParams,
    fetchJson() { return request.promise; },
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
    markHistoryCachesStale() {},
    renderHistoryPanel() {},
    renderHeatmapControls() {},
    renderStorageViewsRuntime() {},
    scheduleAutoRefresh() { events.push("schedule"); },
  });

  const refresh = refreshSnapshot(false, "auto-refresh");
  state.mappingFormDirty = true;
  request.resolve({ selected_system_id: "system-a", slots: [] });
  await refresh;

  assert.equal(events.includes("apply"), false);
  assert.equal(events.includes("render"), false);
  assert.equal(events.some((event) => /deferred.*calibration/i.test(event)), true);
  assert.equal(events.filter((event) => event === "schedule").length, 1);
});

test("discard confirmation keeps or releases a dirty calibration draft explicitly", () => {
  const state = {
    mappingFormDirty: true,
    mappingFormScopeKey: "system-a|enclosure-a||1",
    refreshesInFlight: 0,
  };
  const mappingForm = { classList: visibleClassList(false) };
  const statuses = [];
  let schedules = 0;
  let timingRenders = 0;
  let confirmed = false;
  const mappingEditorHasUnsavedChanges = loadFunction("mappingEditorHasUnsavedChanges", { state, mappingForm });
  const confirmMappingDraftDiscard = loadFunction("confirmMappingDraftDiscard", {
    state,
    mappingEditorHasUnsavedChanges,
    window: { confirm: () => confirmed },
    setStatus(message) { statuses.push(message); },
    scheduleAutoRefresh() { schedules += 1; },
    renderTimingSurfaces() { timingRenders += 1; },
  });

  assert.equal(confirmMappingDraftDiscard(), false);
  assert.equal(state.mappingFormDirty, true);
  assert.equal(state.mappingFormScopeKey, "system-a|enclosure-a||1");
  assert.equal(schedules, 0);
  assert.match(statuses.at(-1), /canceled.*edits were kept/i);

  confirmed = true;
  assert.equal(confirmMappingDraftDiscard(), true);
  assert.equal(state.mappingFormDirty, false);
  assert.equal(state.mappingFormScopeKey, null);
  assert.equal(schedules, 1);
  assert.equal(timingRenders, 1);
});

test("canceled dirty slot navigation leaves selection and rendered detail untouched", () => {
  const state = { selectedSlot: 1, history: { panelError: "kept" } };
  const events = [];
  const selectSlot = loadFunction("selectSlot", {
    state,
    confirmMappingDraftDiscard: () => false,
    syncSasFabricTraceToSlot() { events.push("fabric"); },
    refreshGridSelectionState() { events.push("grid"); },
    renderSasFabric() { events.push("render-fabric"); },
    renderDetail() { events.push("detail"); },
  });

  assert.equal(selectSlot(2), false);
  assert.equal(state.selectedSlot, 1);
  assert.equal(state.history.panelError, "kept");
  assert.deepEqual(events, []);
});

test("canceled dirty clear keeps the selected calibration slot", () => {
  const state = { selectedSlot: 1, history: { panelError: "kept" } };
  const events = [];
  const clearSelectedSlot = loadFunction("clearSelectedSlot", {
    state,
    confirmMappingDraftDiscard: () => false,
    clearSasFabricBaySelection() { events.push("fabric"); },
    refreshGridSelectionState() { events.push("grid"); },
    renderSasFabric() { events.push("render-fabric"); },
    renderDetail() { events.push("detail"); },
  });

  assert.equal(clearSelectedSlot(), false);
  assert.equal(state.selectedSlot, 1);
  assert.equal(state.history.panelError, "kept");
  assert.deepEqual(events, []);
});

test("canceled dirty fabric bay navigation does not change trace or slot", () => {
  const state = {
    selectedSlot: 1,
    history: { panelError: "kept" },
    sasFabric: { selectedTraceId: "bay:1", selectedNodeId: "node-a" },
  };
  let renders = 0;
  const selectSasFabricTrace = loadFunction("selectSasFabricTrace", {
    state,
    sasFabricTraceById: () => ({ id: "bay:2", kind: "bay", slots: [2] }),
    sasFabricSortedSlots: (slots) => slots,
    confirmMappingDraftDiscard: () => false,
    renderAll() { renders += 1; },
  });

  assert.equal(selectSasFabricTrace("bay:2"), false);
  assert.equal(state.selectedSlot, 1);
  assert.equal(state.sasFabric.selectedTraceId, "bay:1");
  assert.equal(state.sasFabric.selectedNodeId, "node-a");
  assert.equal(renders, 0);
});

test("fabric slot controls do not synchronize trace state before dirty navigation is approved", () => {
  const events = [];
  const selectSasFabricSlot = loadFunction("selectSasFabricSlot", {
    Number,
    syncSasFabricTraceToSlot(slot) { events.push(`sync:${slot}`); },
    selectSlot(slot) {
      events.push(`select:${slot}`);
      return false;
    },
  });

  assert.equal(selectSasFabricSlot(2), false);
  assert.deepEqual(events, ["select:2"]);
});

test("system, enclosure, and saved-view navigation guard dirty calibration drafts", () => {
  for (const [marker, length] of [
    ['systemSelect.addEventListener("change"', 500],
    ['enclosureSelect.addEventListener("change"', 500],
  ]) {
    const start = APP_SOURCE.indexOf(marker);
    assert.notEqual(start, -1, `${marker} must exist`);
    assert.match(
      APP_SOURCE.slice(start, start + length),
      /confirmMappingDraftDiscard\(\)/,
      `${marker} must guard dirty mapping navigation`
    );
  }

  const cardHandlerStart = APP_SOURCE.indexOf('storageViewList.addEventListener("click"');
  assert.notEqual(cardHandlerStart, -1, "saved-view card handler must exist");
  assert.match(
    APP_SOURCE.slice(cardHandlerStart, cardHandlerStart + 500),
    /selectStorageViewRuntimeFromCard\(nextViewId\)/,
    "saved-view cards must use the guarded scope transition"
  );
  const guardedHelperStart = APP_SOURCE.indexOf("function selectStorageViewRuntimeFromCard(");
  assert.notEqual(guardedHelperStart, -1, "saved-view guarded transition must exist");
  assert.match(
    APP_SOURCE.slice(guardedHelperStart, guardedHelperStart + 500),
    /confirmMappingDraftDiscard\(\)/,
    "saved-view guarded transition must protect dirty mapping navigation"
  );
});
