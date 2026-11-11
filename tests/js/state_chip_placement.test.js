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

test("a chip that only touches a part, under a pixel, still clears it (#1145)", () => {
  // Right-latched 2.5-inch rear bays at 1920 px under Endurance: "beside"
  // grazes the slot number by half a pixel; "middle" would sit on the
  // "100%" values (the wider badge) of some bays.
  const rearRight = (badgeRight) => ({
    width: 91,
    height: 134,
    parts: [[72.66, 8, 82.66, 18], [8, 10, 36.5, 32], [8, 88, 36.7, 103], [8, 109, 60.66, 124], [64.66, 8, 82.66, 126], [6, 48, badgeRight, 72]]
      .map(([left, top, right, bottom]) => ({ left, top, right, bottom })),
  });
  const choice = choose([rearRight(38.91), rearRight(31.69), rearRight(31.69)]);
  assert.equal(choice.spot, "beside");
  assert.equal(choice.hits, 0);
  // A real overlap still steers the chip away: a number pill 2.5px into
  // "beside" sends it to the clear middle of narrow-valued bays.
  const wideNumber = rearRight(31.69);
  wideNumber.parts[1] = { ...wideNumber.parts[1], right: 38.5 };
  assert.deepEqual(choose([wideNumber]), { spot: "middle", hits: 0, right: 31, top: 130, size: 24 });
});

// #1150 / #1151: when no named spot clears every bay, the chip goes to the
// first free place (from the top right) that does, shrinking from 24 px
// through 20 and 16 to a 12 px dot; it never lands on a part.
const clearOfEveryPart = (choice, bays) => bays.every((layout) => {
  const left = layout.width - choice.right - choice.size;
  const chip = { left, top: choice.top, right: left + choice.size, bottom: choice.top + choice.size };
  return left >= 0 && chip.bottom <= layout.height
    && layout.parts.every((part) => chip.right <= part.left || part.right <= chip.left || chip.bottom <= part.top || part.bottom <= chip.top);
});

test("a 3.5-inch right-latched bay at 390 px gets a free place off the number (#1150)", () => {
  // SCALE 24-bay front at 390 px: LED, number pill, temperature badge,
  // device and serial lines, full-height latch. "beside" sat on the number.
  const scale390 = bay(70, 134, [[52, 8, 10, 10], [8, 10, 28.5, 22], [6, 48, 32.9, 24], [8, 88, 28.7, 15], [8, 109, 32, 15], [44, 8, 18, 118]]);
  const choice = choose([scale390]);
  assert.equal(choice.spot, "free");
  assert.equal(choice.hits, 0);
  assert.ok(choice.size < 24, "no full chip fits; a smaller one does");
  assert.ok(clearOfEveryPart(choice, [scale390]), JSON.stringify(choice));
  // With the heat map off the gap under the number holds a full chip.
  const scaleOff = bay(70, 134, [[52, 8, 10, 10], [8, 10, 28.5, 22], [8, 88, 28.7, 15], [8, 109, 32, 15], [44, 8, 18, 118]]);
  const off = choose([scaleOff]);
  assert.deepEqual([off.spot, off.size], ["free", 24]);
  assert.ok(clearOfEveryPart(off, [scaleOff]), JSON.stringify(off));
});

test("dense sleds with a rotated hours value and empty bays keep the chip off the value (#1151)", () => {
  // 1x24 top-latched 2.5-inch sleds: a bay with "52,345" up the sled and
  // an empty bay with "--". The foot spot sat on the value's first digit.
  const hours = bay(41, 134, [[23, 8, 10, 10], [8, 30, 28.5, 22], [8, 8, 9.3, 18], [6, 58, 16.7, 51.4]]);
  const empty = bay(41, 134, [[23, 8, 10, 10], [8, 30, 28.5, 22], [8, 8, 9.3, 18], [6, 58.8, 16.7, 22.4]]);
  const choice = choose([hours, empty, hours]);
  assert.equal(choice.hits, 0);
  assert.ok(clearOfEveryPart(choice, [hours, empty]), JSON.stringify(choice));
});

test("a bay with no free place for even a dot keeps the fewest-overlap spot", () => {
  const full = bay(30, 30, [[0, 0, 30, 30]]);
  const choice = choose([full]);
  assert.ok(choice.hits > 0);
  assert.notEqual(choice.spot, "free");
});

test("M.2 cards put the chip beside the gold latch, or top-left when a short card is mounted", () => {
  // 2280 card on the Hyper M.2 board: hole, slot label, size tag, gold latch.
  const card2280 = bay(334, 86, [[5, 37, 12, 12], [33, 12, 36, 15], [77, 11, 111, 18], [319, 21, 10, 44], [33, 35, 270, 17], [33, 57, 270, 17]]);
  assert.deepEqual(choose([card2280]), { spot: "beside", hits: 0, right: 19, top: 69, size: 24 });
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
  // #925: selected, peer and Connections bays keep their chip too, so every bay
  // steers the spot and no bay is skipped.
  const place = functionSource("placeStateChips");
  assert.match(place, /grid\.querySelectorAll\("\.slot-tile\[data-slot\]"\)/);
  assert.doesNotMatch(place, /:not\(|selected|peer-highlight|fabric-highlight/);
});
