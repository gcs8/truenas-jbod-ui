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

const sandbox = vm.createContext({});
vm.runInContext(`${functionSource("chooseStateChipSpot")}\nthis.choose = chooseStateChipSpot;`, sandbox);
const choose = (bays) => JSON.parse(JSON.stringify(sandbox.choose(bays)));

// Part boxes are [left, top, width, height] inside the bay's padding box, as
// measured in Chromium on the shipped layouts (heat map off unless noted).
function bay(width, height, parts) {
  return {
    width,
    height,
    parts: parts.map(([left, top, partWidth, partHeight]) => ({
      left, top, right: left + partWidth, bottom: top + partHeight,
    })),
  };
}

const LAYOUTS = {
  rightLatch3_5: bay(288, 134, [[270, 8, 10, 10], [8, 10, 29, 22], [8, 88, 25, 15], [8, 109, 62, 15], [262, 8, 18, 118]]),
  topLoader: bay(74, 119, [[56, 8, 10, 10], [4, 5, 29, 22], [4, 68, 39, 15], [4, 89, 33, 15]]),
  dense2_5: bay(41, 134, [[23, 8, 10, 10], [8, 30, 29, 22], [8, 88, 25, 15], [8, 109, 25, 15], [8, 8, 9, 18]]),
  dense2_5Heatmap: bay(41, 134, [[23, 8, 10, 10], [8, 30, 29, 22], [8, 8, 9, 18], [6, 55, 29, 28]]),
  unifiHeatmap: bay(288, 100, [[267, 46, 9, 9], [14, 14, 29, 22], [14, 50, 25, 15], [14, 71, 240, 15], [6, 38, 276, 28]]),
  topLatch2_5: bay(112, 134, [[94, 8, 10, 10], [8, 30, 29, 22], [8, 88, 66, 15], [8, 109, 50, 15], [8, 8, 80, 18]]),
  compactRear: bay(101, 249, [[83, 8, 10, 10], [8, 10, 29, 22], [8, 183, 42, 15], [8, 204, 85, 15], [8, 223, 85, 18]]),
};

test("each shipped layout gets a state-chip spot that clears every part", () => {
  const expected = {
    rightLatch3_5: { spot: "beside", hits: 0, right: 30 },
    topLoader: { spot: "under", hits: 0, top: 22 },
    dense2_5: { spot: "middle", hits: 0 },
    dense2_5Heatmap: { spot: "foot", hits: 0 },
    unifiHeatmap: { spot: "corner", hits: 0 },
    topLatch2_5: { spot: "under", hits: 0, top: 30 },
    compactRear: { spot: "beside", hits: 0, right: 22 },
  };
  for (const [name, layout] of Object.entries(LAYOUTS)) {
    const choice = choose([layout]);
    for (const [key, value] of Object.entries(expected[name])) {
      assert.equal(choice[key], value, `${name} ${key}`);
    }
  }
});

test("one spot serves the whole face, so a bay that blocks the corner moves every chip", () => {
  const clearCorner = bay(288, 134, [[8, 10, 29, 22]]);
  assert.equal(choose([clearCorner]).spot, "corner");
  assert.equal(choose([clearCorner, LAYOUTS.rightLatch3_5]).spot, "beside");
});

test("a bay with no clear spot gets the spot with the fewest overlaps", () => {
  // A 30px bay with a label across its middle: every spot overlaps something.
  const crowded = bay(30, 60, [[0, 0, 30, 60]]);
  const choice = choose([crowded]);
  assert.ok(choice.hits > 0);
  assert.equal(choice.spot, "corner", "ties keep the earliest spot");
});

test("M.2 cards put the chip beside the gold latch, or top-left when a short card is mounted", () => {
  // 2280 card on the Hyper M.2 board: hole, slot label, size tag, gold latch.
  const card2280 = bay(334, 86, [[5, 37, 12, 12], [33, 12, 36, 15], [77, 11, 111, 18], [319, 21, 10, 44], [33, 35, 270, 17], [33, 57, 270, 17]]);
  assert.deepEqual(choose([card2280]), { spot: "beside", hits: 0, right: 19, top: 69 });
  // A 2230 card is 150px wide and its size tag runs to the edge, so no
  // right-hand spot is clear on both; above the screw hole is.
  const card2230 = bay(150, 86, [[5, 37, 12, 12], [33, 12, 36, 15], [77, 11, 73, 18], [135, 21, 10, 44], [33, 35, 90, 17], [33, 57, 90, 17]]);
  assert.equal(choose([card2280, card2230]).spot, "start");
  assert.equal(choose([card2280, card2230]).hits, 0);
});

test("selection passes, renders and heat-map refreshes all re-place the chips", () => {
  // finishGridRender runs refreshGridSelectionState, so renders are covered too.
  const renderGrid = APP_SOURCE.slice(APP_SOURCE.indexOf("  function renderGrid() {"), APP_SOURCE.indexOf("  function kvRow("));
  assert.match(renderGrid, /const finishGridRender = \(\) => \{[^}]*refreshGridSelectionState\(\);/s);
  assert.match(functionSource("refreshGridSelectionState"), /scheduleStateChipPlacement\(\)/);
  assert.match(functionSource("refreshHeatmapTileOverlays"), /scheduleStateChipPlacement\(\)/);
  assert.match(APP_SOURCE, /new ResizeObserver\(scheduleStateChipPlacement\)\.observe\(grid\)/);
  // Ring tiles draw no chip, so they must not steer where the others go.
  assert.match(functionSource("placeStateChips"), /:not\(\.selected, \.peer-highlight, \.fabric-highlight\)/);
});
