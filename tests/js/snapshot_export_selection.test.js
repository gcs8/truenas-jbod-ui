"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
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
