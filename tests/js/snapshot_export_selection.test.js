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
    ["resolveInitialSnapshotSelection"],
    { Number, Set, URLSearchParams },
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
