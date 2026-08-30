"use strict";

const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = process.env.APP_SOURCE_REV
  ? execFileSync("git", ["show", `${process.env.APP_SOURCE_REV}:app/static/app.js`], {
      cwd: ROOT,
      encoding: "utf8",
    })
  : fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
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

function loadFunction(name, context) {
  const sandbox = vm.createContext({ ...context });
  vm.runInContext(`${functionSource(APP_SOURCE, name)}\nthis.__loaded = ${name};`, sandbox, {
    filename: `${name}.behavior.js`,
  });
  return sandbox.__loaded;
}

test("slot selection updates targeted domains without rebuilding the grid", () => {
  const calls = [];
  const state = {
    selectedSlot: null,
    history: { panelError: "stale" },
  };
  const selectSlot = loadFunction("selectSlot", {
    state,
    syncSasFabricTraceToSlot(slot) { calls.push(`sync:${slot}`); },
    refreshGridSelectionState() { calls.push("grid-state"); },
    renderSasFabric() { calls.push("fabric"); },
    renderDetail() { calls.push("detail"); },
    renderAll() { calls.push("full-render"); },
  });

  selectSlot(7);

  assert.equal(state.selectedSlot, 7);
  assert.equal(state.history.panelError, null);
  assert.deepEqual(calls, ["sync:7", "grid-state", "fabric", "detail"]);
});

test("clearing selection updates targeted domains without rebuilding the grid", () => {
  const calls = [];
  const state = {
    selectedSlot: 7,
    history: { panelError: "stale" },
  };
  const clearSelectedSlot = loadFunction("clearSelectedSlot", {
    state,
    clearSasFabricBaySelection() { calls.push("clear-fabric-selection"); },
    refreshGridSelectionState() { calls.push("grid-state"); },
    renderSasFabric() { calls.push("fabric"); },
    renderDetail() { calls.push("detail"); },
    renderAll() { calls.push("full-render"); },
  });

  clearSelectedSlot();

  assert.equal(state.selectedSlot, null);
  assert.equal(state.history.panelError, null);
  assert.deepEqual(calls, ["clear-fabric-selection", "grid-state", "fabric", "detail"]);
});

test("saved chassis fabric highlights use the backing live slot identity", () => {
  const makeTile = (slot) => {
    const classes = new Set();
    return {
      classes,
      tile: {
        dataset: { slot },
        disabled: false,
        classList: {
          contains(name) { return classes.has(name); },
          remove(name) { classes.delete(name); },
          toggle(name, force) {
            if (force) classes.add(name);
            else classes.delete(name);
          },
        },
        setAttribute() {},
      },
    };
  };
  const backed = makeTile("0");
  const unbacked = makeTile("1");
  const refreshGridSelectionState = loadFunction("refreshGridSelectionState", {
    state: { selectedSlot: null },
    grid: { querySelectorAll() { return [backed.tile, unbacked.tile]; } },
    getSelectedPeerContext() { return { active: false, peerSlots: new Set() }; },
    sasFabricSelectedSlotSet() { return new Set([42]); },
    fabricSlotNumberForGridTile(tile) { return tile.dataset.slot === "0" ? 42 : null; },
  });

  refreshGridSelectionState();

  assert.equal(backed.classes.has("fabric-highlight"), true);
  assert.equal(backed.classes.has("fabric-dimmed"), false);
  assert.equal(unbacked.classes.has("fabric-highlight"), false);
  assert.equal(unbacked.classes.has("fabric-dimmed"), false);
});

test("grid rebuild moves focus to search when no slot remains visible", () => {
  let searchFocused = false;
  const restoreGridFocus = loadFunction("restoreGridFocus", {
    grid: { querySelectorAll() { return []; } },
    visibleGridTiles() { return []; },
    searchBox: {
      disabled: false,
      isConnected: true,
      focus() { searchFocused = true; },
    },
  });

  restoreGridFocus("7");

  assert.equal(searchFocused, true);
});

test("search filtering toggles existing tiles without rebuilding the grid", () => {
  const makeTile = (slot) => {
    const classes = new Set();
    return {
      dataset: { slot: String(slot) },
      classList: {
        contains(name) { return classes.has(name); },
        toggle(name, force) {
          if (force) classes.add(name);
          else classes.delete(name);
        },
      },
      classes,
    };
  };
  const first = makeTile(1);
  const second = makeTile(2);
  const state = { search: "archive" };
  const refreshGridFilterState = loadFunction("refreshGridFilterState", {
    grid: { querySelectorAll() { return [first, second]; } },
    gridTileMatchesFilter(tile) { return tile.dataset.slot === "2"; },
  });

  refreshGridFilterState();

  assert.equal(first.classes.has("filtered-out"), true);
  assert.equal(second.classes.has("filtered-out"), false);
  const inputHandler = APP_SOURCE.slice(
    APP_SOURCE.indexOf('searchBox.addEventListener("input"'),
    APP_SOURCE.indexOf('refreshButton.addEventListener("click"'),
  );
  assert.match(inputHandler, /refreshGridFilterState\(\)/);
  assert.doesNotMatch(inputHandler, /renderGrid\(\)/);
  assert.equal(state.search, "archive");
});

test("live slot search compares normalized text case-insensitively", () => {
  const passesFilter = loadFunction("passesFilter", {
    state: { search: "m06000000" },
  });

  assert.equal(passesFilter({ search_text: "Bay 001 M06000000" }), true);
  assert.equal(passesFilter({ search_text: "Bay 002 M06000001" }), false);
});

test("SMART completion refreshes heatmap overlays without rebuilding tiles", () => {
  for (const name of ["ensureSmartSummary", "ensureStorageViewSmartSummary"]) {
    const source = functionSource(APP_SOURCE, name);
    assert.match(source, /refreshGridTileAriaLabel\(/, `${name} must update the existing tile label`);
    assert.match(source, /refreshHeatmapTileOverlays\(\)/, `${name} must refresh overlays in place`);
    assert.doesNotMatch(source, /renderGrid\(\)/, `${name} must preserve tile identity`);
  }
});
