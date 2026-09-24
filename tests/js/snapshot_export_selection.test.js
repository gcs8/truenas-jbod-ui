"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(name) {
  let start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  if (APP_SOURCE.slice(Math.max(0, start - 6), start) === "async ") {
    start -= 6;
  }
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
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

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nthis.__loaded = { ${names.join(", ")} };`,
    sandbox,
  );
  return sandbox.__loaded;
}

test("snapshot export payload identifies the active storage view", () => {
  const state = {
    selectedSlot: 1,
    selectedStorageViewRuntimeId: "boot-doms",
    history: { panelOpen: true, ioChartMode: "total" },
    export: {
      redactSensitive: false,
      packaging: "auto",
      allowOversize: false,
      includeLiveEnclosures: false,
      includeStorageViews: true,
    },
  };
  const { snapshotExportRequestPayload } = loadFunctions(
    ["snapshotExportRequestPayload"],
    {
      Boolean,
      currentHistoryWindowHours: () => 24,
      isHistoryAvailable: () => true,
      selectedExportEnclosureIds: () => [],
      selectedExportStorageViewIds: () => ["boot-doms"],
      state,
    },
  );

  const payload = snapshotExportRequestPayload();

  assert.equal(payload.selected_slot, 1);
  assert.equal(payload.selected_storage_view_id, "boot-doms");
});

test("snapshot bootstrap preserves a valid view and clears an unresolved view slot", () => {
  const { resolveInitialSnapshotSelection } = loadFunctions(
    ["resolveInitialSnapshotSelection", "isMainUiStorageViewRuntimeOption"],
    { Boolean, Number, Set, URLSearchParams },
  );
  const validBootstrap = {
    initialSelectedSlot: 1,
    initialSelectedStorageViewId: "boot-doms",
    storageViewsRuntime: { views: [{ id: "boot-doms" }] },
  };

  assert.deepEqual(
    { ...resolveInitialSnapshotSelection(validBootstrap, true) },
    { selectedSlot: 1, storageViewId: "boot-doms" },
  );
  assert.deepEqual(
    {
      ...resolveInitialSnapshotSelection(
        { ...validBootstrap, storageViewsRuntime: { views: [] } },
        true,
      ),
    },
    { selectedSlot: null, storageViewId: "" },
  );
  assert.deepEqual(
    {
      ...resolveInitialSnapshotSelection(
        { initialSelectedSlot: 0, storageViewsRuntime: { views: [] } },
        true,
      ),
    },
    { selectedSlot: 0, storageViewId: "" },
  );
});

test("snapshot bootstrap drops a hidden or disabled view together with its slot", () => {
  const { resolveInitialSnapshotSelection } = loadFunctions(
    ["resolveInitialSnapshotSelection", "isMainUiStorageViewRuntimeOption"],
    { Boolean, Number, Set, URLSearchParams },
  );

  assert.deepEqual(
    {
      ...resolveInitialSnapshotSelection(
        {
          initialSelectedSlot: 41,
          initialSelectedStorageViewId: "maint-view",
          storageViewsRuntime: {
            views: [{ id: "maint-view", render: { show_in_main_ui: false } }],
          },
        },
        true,
      ),
    },
    { selectedSlot: null, storageViewId: "" },
  );
  assert.deepEqual(
    {
      ...resolveInitialSnapshotSelection(
        {
          initialSelectedSlot: 41,
          initialSelectedStorageViewId: "off-view",
          storageViewsRuntime: { views: [{ id: "off-view", enabled: false }] },
        },
        true,
      ),
    },
    { selectedSlot: null, storageViewId: "" },
  );
});

test("dropping a storage view runtime selection clears its slot index", () => {
  const state = {
    selectedSlot: 41,
    selectedStorageViewRuntimeId: "maint-view",
    storageViewsRuntime: {
      views: [{ id: "maint-view", enabled: false }, { id: "boot-doms" }],
    },
  };
  const { ensureStorageViewRuntimeSelection } = loadFunctions(
    [
      "storageViewRuntimeViews",
      "isMainUiStorageViewRuntimeOption",
      "getMainUiStorageViewRuntimeOptions",
      "getStorageViewRuntimeById",
      "dropStorageViewRuntimeSelection",
      "ensureStorageViewRuntimeSelection",
    ],
    { Array, Boolean, Number, state },
  );

  assert.equal(ensureStorageViewRuntimeSelection(false), null);
  assert.equal(state.selectedStorageViewRuntimeId, "");
  assert.equal(state.selectedSlot, null);
});

test("keeping a visible storage view runtime selection preserves its slot index", () => {
  const state = {
    selectedSlot: 3,
    selectedStorageViewRuntimeId: "boot-doms",
    storageViewsRuntime: { views: [{ id: "boot-doms" }] },
  };
  const { ensureStorageViewRuntimeSelection } = loadFunctions(
    [
      "storageViewRuntimeViews",
      "isMainUiStorageViewRuntimeOption",
      "getMainUiStorageViewRuntimeOptions",
      "getStorageViewRuntimeById",
      "dropStorageViewRuntimeSelection",
      "ensureStorageViewRuntimeSelection",
    ],
    { Array, Boolean, Number, state },
  );

  assert.equal(ensureStorageViewRuntimeSelection(false)?.id, "boot-doms");
  assert.equal(state.selectedStorageViewRuntimeId, "boot-doms");
  assert.equal(state.selectedSlot, 3);
});

test("a live enclosure bay selection survives an empty storage view runtime", () => {
  const state = {
    selectedSlot: 12,
    selectedStorageViewRuntimeId: "",
    storageViewsRuntime: { views: [] },
  };
  const { ensureStorageViewRuntimeSelection } = loadFunctions(
    [
      "storageViewRuntimeViews",
      "isMainUiStorageViewRuntimeOption",
      "getMainUiStorageViewRuntimeOptions",
      "getStorageViewRuntimeById",
      "dropStorageViewRuntimeSelection",
      "ensureStorageViewRuntimeSelection",
    ],
    { Array, Boolean, Number, state },
  );

  assert.equal(ensureStorageViewRuntimeSelection(false), null);
  assert.equal(state.selectedSlot, 12);
});

function exportEstimateState() {
  return {
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    selectedSlot: 1,
    selectedStorageViewRuntimeId: "",
    snapshotExportSourceGeneration: 0,
    snapshot: { slots: [], selected_system_id: "system-a", selected_enclosure_id: "enclosure-a" },
    layoutRows: [],
    smartSummaries: {},
    export: {
      redactSensitive: false,
      packaging: "auto",
      allowOversize: false,
      includeLiveEnclosures: false,
      selectedEnclosureIds: [],
      includeStorageViews: false,
      selectedStorageViewIds: [],
      estimate: {
        loading: false,
        error: null,
        data: null,
        requestToken: 0,
      },
    },
    history: { panelOpen: false, ioChartMode: "total" },
  };
}

function exportEstimateContext(state, overrides = {}) {
  return {
    Boolean,
    Number,
    buildScopedUrl: (url) => url,
    currentHistoryWindowHours: () => 24,
    exportSnapshotDialog: null,
    noteServerAppVersion: () => {},
    syncWritePolicyFromSnapshot: () => {},
    getSelectedStorageViewRuntimeSlot: () => null,
    getSlotById: () => null,
    isHistoryAvailable: () => false,
    normalizePersistedPackaging: (value) => value,
    pruneSmartSummaryCache: () => {},
    rememberReusableSnapshot: () => {},
    selectedExportEnclosureIds: () => [],
    selectedExportStorageViewIds: () => [],
    state,
    syncSnapshotExportDialog: () => {},
    ...overrides,
  };
}

function loadExportEstimateFunctions(state, overrides = {}) {
  return loadFunctions(
    [
      "snapshotExportRequestPayload",
      "snapshotExportEstimateBasisKey",
      "estimatePackagingSizeDetails",
      "estimateEffectivePackagingForSelection",
      "updateSnapshotExportEstimateSelectionFromCache",
      "invalidateSnapshotExportEstimate",
      "invalidateSnapshotExportEstimateIfBasisChanged",
      "advanceSnapshotExportSourceGeneration",
      "applySnapshot",
      "refreshSnapshotExportEstimate",
    ],
    exportEstimateContext(state, overrides),
  );
}

test("snapshot export estimate basis includes selected source identity and generation but not packaging", () => {
  const state = exportEstimateState();
  const { snapshotExportRequestPayload, snapshotExportEstimateBasisKey } = loadFunctions(
    ["snapshotExportRequestPayload", "snapshotExportEstimateBasisKey"],
    exportEstimateContext(state),
  );

  const initialKey = snapshotExportEstimateBasisKey();
  state.export.packaging = "zip";
  state.export.allowOversize = true;
  assert.equal(snapshotExportEstimateBasisKey(), initialKey);

  state.selectedSystemId = "system-b";
  assert.notEqual(snapshotExportEstimateBasisKey(), initialKey);
  state.selectedSystemId = "system-a";
  state.selectedEnclosureId = "enclosure-b";
  assert.notEqual(snapshotExportEstimateBasisKey(), initialKey);
  state.selectedEnclosureId = "enclosure-a";
  state.snapshotExportSourceGeneration += 1;
  assert.notEqual(snapshotExportEstimateBasisKey(), initialKey);
});

test("closing before a snapshot refresh requires a new estimate when the dialog reopens", async () => {
  const state = exportEstimateState();
  state.selectedSlot = null;
  const exportSnapshotDialog = { open: false };
  let estimateRequests = 0;
  const loaded = loadExportEstimateFunctions(state, {
    exportSnapshotDialog,
    fetch: async () => {
      estimateRequests += 1;
      return {
        ok: true,
        json: async () => ({ html_size_label: "7.0 MiB", selected_packaging: "auto" }),
      };
    },
  });
  state.export.estimate.data = {
    _estimate_basis_key: loaded.snapshotExportEstimateBasisKey(),
    auto_packaging: "html",
    html_size_label: "6.0 MiB",
    html_within_limit: true,
  };

  loaded.applySnapshot({
    slots: [],
    selected_system_id: "system-a",
    selected_enclosure_id: "enclosure-a",
  });
  await loaded.refreshSnapshotExportEstimate();
  assert.equal(estimateRequests, 0);

  exportSnapshotDialog.open = true;
  await loaded.refreshSnapshotExportEstimate();

  assert.equal(estimateRequests, 1);
  assert.equal(state.export.estimate.data.html_size_label, "7.0 MiB");
});

test("closing before a scope switch requires a new estimate when the dialog reopens", async () => {
  const state = exportEstimateState();
  const exportSnapshotDialog = { open: false };
  let estimateRequests = 0;
  const loaded = loadExportEstimateFunctions(state, {
    exportSnapshotDialog,
    fetch: async () => {
      estimateRequests += 1;
      return {
        ok: true,
        json: async () => ({ html_size_label: "8.0 MiB", selected_packaging: "auto" }),
      };
    },
  });
  const previousBasisKey = loaded.snapshotExportEstimateBasisKey();
  state.export.estimate.data = {
    _estimate_basis_key: previousBasisKey,
    auto_packaging: "html",
    html_size_label: "6.0 MiB",
    html_within_limit: true,
  };

  state.selectedSystemId = "system-b";
  state.selectedEnclosureId = "enclosure-b";
  loaded.invalidateSnapshotExportEstimateIfBasisChanged(previousBasisKey);
  await loaded.refreshSnapshotExportEstimate();
  assert.equal(estimateRequests, 0);

  exportSnapshotDialog.open = true;
  await loaded.refreshSnapshotExportEstimate();

  assert.equal(estimateRequests, 1);
  assert.equal(state.export.estimate.data.html_size_label, "8.0 MiB");
});

test("a late estimate response from a replaced snapshot is ignored before reading its payload", async () => {
  const state = exportEstimateState();
  const exportSnapshotDialog = { open: true };
  let resolveResponse;
  let payloadReads = 0;
  const responsePromise = new Promise((resolve) => {
    resolveResponse = resolve;
  });
  const loaded = loadExportEstimateFunctions(state, {
    exportSnapshotDialog,
    fetch: () => responsePromise,
  });

  const pendingEstimate = loaded.refreshSnapshotExportEstimate();
  const requestToken = state.export.estimate.requestToken;
  loaded.applySnapshot({
    slots: [],
    selected_system_id: "system-b",
    selected_enclosure_id: "enclosure-b",
  });
  resolveResponse({
    ok: true,
    json: async () => {
      payloadReads += 1;
      return { html_size_label: "stale" };
    },
  });
  await pendingEstimate;

  assert.ok(state.export.estimate.requestToken > requestToken);
  assert.equal(payloadReads, 0);
  assert.equal(state.export.estimate.data, null);
  assert.equal(state.export.estimate.loading, false);
});
