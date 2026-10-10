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
  const sandbox = vm.createContext({ ...context });
  const declarations = names.map(functionSource).join("\n");
  vm.runInContext(
    `${declarations}\nthis.__loaded = { ${names.join(", ")} };`,
    sandbox,
    { filename: "connections-clear-selection.behavior.js" },
  );
  return sandbox.__loaded;
}

function fabricFixture() {
  return {
    available: true,
    traces: [
      { id: "bay:0", kind: "bay", slots: [0], metrics: {} },
      { id: "path:synthetic-hba:fail", kind: "path", slots: [0], metrics: { state: "fail" } },
    ],
    nodes: [
      { id: "controller:synthetic-hba", kind: "controller", related_slots: [0] },
    ],
  };
}

test("an unselected grid opens Connections without auto-selecting a failed path", () => {
  const state = {
    selectedSlot: null,
    sasFabric: { data: fabricFixture(), selectionCleared: false },
  };
  const { defaultSasFabricTraceId } = loadFunctions([
    "sasFabricList",
    "sasFabricClassToken",
    "defaultSasFabricTraceId",
  ], { state });

  assert.equal(defaultSasFabricTraceId(), null);
  state.selectedSlot = 0;
  assert.equal(defaultSasFabricTraceId(), "bay:0");
  state.sasFabric.selectionCleared = true;
  assert.equal(defaultSasFabricTraceId(), null, "an explicit clear survives later resolution");
});

test("clearing a selected bay clears only selection state", () => {
  const fabric = fabricFixture();
  const state = {
    sasFabric: {
      data: fabric,
      selectedTraceId: "bay:0",
      selectedNodeId: null,
      selectionCleared: false,
    },
  };
  const { clearSasFabricBaySelection } = loadFunctions([
    "clearSasFabricBaySelection",
  ], {
    state,
    selectedSasFabricTrace: () => fabric.traces[0],
    defaultSasFabricTraceId: () => "path:synthetic-hba:fail",
  });

  clearSasFabricBaySelection();

  assert.equal(state.sasFabric.selectedTraceId, null);
  assert.equal(state.sasFabric.selectedNodeId, null);
  assert.equal(state.sasFabric.selectionCleared, true);
  assert.equal(fabric.traces[1].metrics.state, "fail", "fault state stays in the fabric payload");
});

test("clicking a selected path or node delegates to the common clear action", () => {
  const fabric = fabricFixture();
  const cleared = [];
  const renders = [];
  const state = {
    selectedSlot: null,
    history: { panelError: null },
    sasFabric: {
      data: fabric,
      selectedTraceId: "path:synthetic-hba:fail",
      selectedNodeId: null,
      selectionCleared: false,
    },
  };
  const context = {
    state,
    sasFabricTraceById: (id) => fabric.traces.find((trace) => trace.id === id) || null,
    sasFabricNodeById: (id) => fabric.nodes.find((node) => node.id === id) || null,
    sasFabricSortedSlots: (slots) => slots,
    confirmMappingDraftDiscard: () => true,
    clearSasFabricSelection() { cleared.push("clear"); return true; },
    renderAll() { renders.push("render"); },
  };
  const { selectSasFabricTrace, selectSasFabricNode } = loadFunctions([
    "selectSasFabricTrace",
    "selectSasFabricNode",
  ], context);

  assert.equal(selectSasFabricTrace("path:synthetic-hba:fail"), true);
  assert.deepEqual(cleared, ["clear"]);
  assert.deepEqual(renders, []);

  state.sasFabric.selectedTraceId = null;
  state.sasFabric.selectedNodeId = "controller:synthetic-hba";
  assert.equal(selectSasFabricNode("controller:synthetic-hba"), true);
  assert.deepEqual(cleared, ["clear", "clear"]);
  assert.deepEqual(renders, []);
});

test("clearing a selected fabric bay uses the grid clear path and its mapping prompt", () => {
  const state = {
    selectedSlot: 0,
    sasFabric: {
      selectedTraceId: "bay:0",
      selectedNodeId: null,
      selectionCleared: false,
    },
  };
  let gridClearCalls = 0;
  const { clearSasFabricSelection } = loadFunctions([
    "clearSasFabricSelection",
  ], {
    state,
    selectedSasFabricTrace: () => ({ id: "bay:0", kind: "bay", slots: [0] }),
    selectedSasFabricNode: () => null,
    clearSelectedSlot() {
      gridClearCalls += 1;
      return false;
    },
    renderAll() { assert.fail("a rejected grid clear must not render a cleared selection"); },
  });

  assert.equal(clearSasFabricSelection(), false);
  assert.equal(gridClearCalls, 1);
  assert.equal(state.sasFabric.selectedTraceId, "bay:0");
  assert.equal(state.sasFabric.selectionCleared, false);
});
