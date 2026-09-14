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

const LAYOUT_FUNCTIONS = [
  "countLayoutSlots",
  "normalizeLayoutRows",
  "normalizeGeometryRowGroups",
  "normalizeDriveScaleCandidate",
  "inferDominantDriveScale",
  "inferChassisLayoutMode",
  "buildChassisGeometry",
  "splitRowIntoGroups",
  "rowGroupBreakpoints",
  "flatGroupedColumnTemplate",
  "rowGroupingMetrics",
  "groupColumnTemplate",
  "buildLayoutGridRows",
  "renderChassisRows",
];

const RENDER_FUNCTIONS = [
  "escapeHtml",
  "formatSlotLabel",
  "sasFabricList",
  "sasFabricSortedSlots",
  "renderSasFabricFlatBayChips",
  "sasFabricBayGridCell",
  "renderSasFabricBayChips",
];

function fakeElement(tagName) {
  const element = {
    tagName,
    className: "",
    children: [],
    dataset: {},
    style: { setProperty() {} },
    classList: {
      tokens: new Set(),
      add(...names) {
        names.forEach((name) => element.classList.tokens.add(name));
      },
      remove(...names) {
        names.forEach((name) => element.classList.tokens.delete(name));
      },
      contains(name) {
        return element.classList.tokens.has(name);
      },
    },
    setAttribute() {},
    appendChild(child) {
      element.children.push(child);
      return child;
    },
  };
  return element;
}

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  const source = names.map(functionSource).join("\n");
  vm.runInContext(`${source}\nthis.__loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.__loaded;
}

// ---------------------------------------------------------------------------
// Layout fixtures, all taken from app/services/profile_registry.py so the test
// speaks the repo's own geometry rather than an invented one.
// ---------------------------------------------------------------------------

function range(start, end) {
  return Array.from({ length: end - start }, (_, index) => start + index);
}

function sparseSlotLayout(rows, columns, { startingSlot = 0, excludedCells = [] } = {}) {
  const excluded = new Set(excludedCells.map(([row, column]) => `${row}:${column}`));
  const layout = Array.from({ length: rows }, () => Array.from({ length: columns }, () => null));
  let nextSlot = startingSlot;
  for (let rowIndex = rows - 1; rowIndex >= 0; rowIndex -= 1) {
    for (let columnIndex = 0; columnIndex < columns; columnIndex += 1) {
      if (excluded.has(`${rowIndex}:${columnIndex}`)) {
        continue;
      }
      layout[rowIndex][columnIndex] = nextSlot;
      nextSlot += 1;
    }
  }
  return layout;
}

function mergeSlotLayoutSections(...sections) {
  const rowCount = sections[0].length;
  return Array.from({ length: rowCount }, (_, rowIndex) =>
    sections.reduce((row, section) => row.concat(section[rowIndex]), []));
}

// supermicro-cse-946-top-60: the Archive CORE top view from the report.
const CSE_946 = {
  name: "CSE-946 60-bay top loader",
  profile: {
    profileId: "supermicro-cse-946-top-60",
    edgeLabel: "System front / latch edge",
    faceStyle: "top-loader",
    latchEdge: "bottom",
    baySize: "3.5",
    rowGroups: [6, 6, 3],
  },
  layoutRows: [range(45, 60), range(30, 45), range(15, 30), range(0, 15)],
};

// scale-ssg-6048r-front-24: a 24-bay front loader with column-major numbering.
const FRONT_24 = {
  name: "SSG-6048R 24-bay front loader",
  profile: {
    profileId: "scale-ssg-6048r-front-24",
    edgeLabel: "Front of chassis",
    faceStyle: "front-drive",
    latchEdge: "right",
    baySize: "3.5",
    rowGroups: [],
  },
  layoutRows: [
    [5, 11, 17, 23],
    [4, 10, 16, 22],
    [3, 9, 15, 21],
    [2, 8, 14, 20],
    [1, 7, 13, 19],
    [0, 6, 12, 18],
  ],
};

// A combined front+rear enclosure pair (LSI-F + LSI-R): the backend merges both
// members into one slot space and renders it through one profile whose layout
// is built from two sections, exactly like generic-front-106-8x14.
const COMBINED_PAIR = {
  name: "combined front+rear pair (merged sections, with gaps)",
  profile: {
    profileId: "generic-front-106-8x14",
    edgeLabel: "Front of chassis",
    faceStyle: "front-drive",
    latchEdge: "top",
    baySize: "3.5",
    rowGroups: [],
  },
  layoutRows: mergeSlotLayoutSections(
    sparseSlotLayout(8, 2, {
      startingSlot: 96,
      excludedCells: [[0, 0], [0, 1], [1, 0], [1, 1], [2, 0], [2, 1]],
    }),
    sparseSlotLayout(8, 12)
  ),
};

const LAYOUTS = [CSE_946, FRONT_24, COMBINED_PAIR];

function layoutContext(layout) {
  const slots = layout.layoutRows
    .flat()
    .filter((slotNumber) => Number.isInteger(slotNumber))
    .map((slotNumber) => ({ slot: slotNumber, state: "matched" }));
  return {
    state: { snapshot: { slots, layout_slot_count: slots.length }, selectedSlot: null },
    getSmartSummaryEntry: () => null,
    getSelectedProfile: () => ({ row_groups: layout.profile.rowGroups }),
    currentLayoutSlotCount: () => slots.length,
    activeLayoutRows: () => layout.layoutRows,
  };
}

function chassisPositions(loaded, grid, layout, geometry) {
  const positions = new Map();
  const columnsByRow = new Map();
  const appendTile = (container, slotValue) => {
    const rowIndex = grid.children.length;
    const columnIndex = columnsByRow.get(rowIndex) || 0;
    columnsByRow.set(rowIndex, columnIndex + 1);
    if (Number.isInteger(slotValue)) {
      positions.set(slotValue, `${rowIndex}:${columnIndex}`);
    }
  };
  loaded.renderChassisRows(layout.layoutRows, geometry, appendTile);
  return positions;
}

function fabricPositions(loaded, layout, geometry) {
  const positions = new Map();
  loaded.buildLayoutGridRows(layout.layoutRows, geometry).forEach((row, rowIndex) => {
    let columnIndex = 0;
    row.groups.forEach((group) => {
      group.forEach((slotValue) => {
        if (Number.isInteger(slotValue)) {
          positions.set(slotValue, `${rowIndex}:${columnIndex}`);
        }
        columnIndex += 1;
      });
    });
  });
  return positions;
}

test("the shared layout helper keeps the enclosure's row order and column groups", () => {
  const layout = CSE_946;
  const grid = fakeElement("div");
  const loaded = loadFunctions(LAYOUT_FUNCTIONS, {
    ...layoutContext(layout),
    document: { createElement: fakeElement },
    grid,
  });
  const geometry = loaded.buildChassisGeometry(layout.profile, layout.layoutRows);
  const rows = loaded.buildLayoutGridRows(layout.layoutRows, geometry);

  assert.equal(rows.length, 4);
  assert.equal(rows[0].slots[0], 45, "the row furthest from the latch edge comes first");
  assert.equal(rows[3].slots[0], 0, "bay 00 sits on the bottom row, at the latch edge");
  assert.equal(
    JSON.stringify(rows.map((row) => row.groups.map((group) => group.length))),
    JSON.stringify([[6, 6, 3], [6, 6, 3], [6, 6, 3], [6, 6, 3]]),
    "the 6 | 6 | 3 column grouping survives"
  );
  assert.equal(JSON.stringify(rows[0].breakpoints), JSON.stringify([6, 12]), "the group gaps are preserved");
  assert.equal(rows[0].flatGrouped, true, "top loaders keep their flat grouped row rendering");
});

for (const layout of LAYOUTS) {
  test(`every bay lands on the enclosure's (row, column) for ${layout.name}`, () => {
    const grid = fakeElement("div");
    const loaded = loadFunctions(LAYOUT_FUNCTIONS, {
      ...layoutContext(layout),
      document: { createElement: fakeElement },
      grid,
    });
    const geometry = loaded.buildChassisGeometry(layout.profile, layout.layoutRows);
    const chassis = chassisPositions(loaded, grid, layout, geometry);
    const fabric = fabricPositions(loaded, layout, geometry);

    assert.ok(chassis.size > 0, "the chassis renderer must place bays");
    assert.equal(
      JSON.stringify(Array.from(fabric.entries()).sort()),
      JSON.stringify(Array.from(chassis.entries()).sort()),
      "the fabric grid must place every bay exactly where the enclosure tile is"
    );
  });
}

function bayGridContext(layout, { impactedSlots = [], emptySlots = [], layoutRows = null } = {}) {
  const emptySet = new Set(emptySlots);
  const slots = layout.layoutRows
    .flat()
    .filter((slotNumber) => Number.isInteger(slotNumber))
    .map((slotNumber) => ({
      slot: slotNumber,
      state: emptySet.has(slotNumber) ? "empty" : "matched",
    }));
  const rows = layoutRows === null ? layout.layoutRows : layoutRows;
  return {
    state: { snapshot: { slots, layout_slot_count: slots.length }, selectedSlot: null },
    getSmartSummaryEntry: () => null,
    getSelectedProfile: () => ({ row_groups: layout.profile.rowGroups }),
    currentLayoutSlotCount: () => slots.length,
    activeLayoutRows: () => rows,
    buildViewProfile: () => layout.profile,
    sasFabricSelectedSlotSet: () => new Set(impactedSlots),
  };
}

test("impacted bays are lit inside the enclosure shape, everything else is a placeholder", () => {
  const layout = CSE_946;
  const loaded = loadFunctions([...LAYOUT_FUNCTIONS, ...RENDER_FUNCTIONS], {
    ...bayGridContext(layout, { impactedSlots: [0, 46], emptySlots: [1] }),
    document: { createElement: fakeElement },
    grid: fakeElement("div"),
  });
  const markup = loaded.renderSasFabricBayChips([0, 46], 72);

  assert.match(markup, /class="sas-fabric-bay-layout"/, "the grid is laid out, not wrapped");
  assert.equal(
    (markup.match(/class="sas-fabric-bay-row"/g) || []).length,
    4,
    "one row per enclosure row"
  );
  assert.match(
    markup,
    /<button type="button" class="sas-fabric-bay-chip is-selected" data-sas-fabric-slot="0">/,
    "an impacted bay stays a selectable chip"
  );
  assert.match(markup, /data-sas-fabric-slot="46"/);
  assert.doesNotMatch(markup, /data-sas-fabric-slot="2"/, "un-impacted bays are not selectable chips");
  assert.match(markup, /class="sas-fabric-bay-chip is-empty" title="01">01</, "an empty bay renders an empty placeholder");
  assert.match(markup, /class="sas-fabric-bay-chip is-outside" title="02">02</, "a populated bay renders a dim placeholder");
  assert.equal(
    (markup.match(/sas-fabric-bay-chip/g) || []).length,
    60,
    "every bay position of the active layout is drawn"
  );
  assert.doesNotMatch(markup, /sas-fabric-bay-overflow/, "a layout-shaped grid has no +N overflow");
  assert.match(markup, /SYSTEM FRONT \/ LATCH EDGE|System front \/ latch edge/, "the edge cue is explicit");
});

test("a layout with gaps draws the gaps, not a shifted row", () => {
  const layout = COMBINED_PAIR;
  const loaded = loadFunctions([...LAYOUT_FUNCTIONS, ...RENDER_FUNCTIONS], {
    ...bayGridContext(layout, { impactedSlots: [0] }),
    document: { createElement: fakeElement },
    grid: fakeElement("div"),
  });
  const markup = loaded.renderSasFabricBayChips([0], 72);
  assert.equal(
    (markup.match(/sas-fabric-bay-gap/g) || []).length,
    6,
    "the six excluded cells stay gaps"
  );
  assert.equal((markup.match(/sas-fabric-bay-chip/g) || []).length, 106);
});

test("with no layout rows the grid falls back to sorted chips and says so", () => {
  const layout = CSE_946;
  const loaded = loadFunctions([...LAYOUT_FUNCTIONS, ...RENDER_FUNCTIONS], {
    ...bayGridContext(layout, { impactedSlots: [3, 1], layoutRows: [] }),
    document: { createElement: fakeElement },
    grid: fakeElement("div"),
  });
  const markup = loaded.renderSasFabricBayChips([3, 1], 72);

  assert.doesNotMatch(markup, /sas-fabric-bay-layout"/);
  assert.match(markup, /data-sas-fabric-slot="1"[\s\S]*data-sas-fabric-slot="3"/, "sorted flat chips");
  assert.match(markup, /sas-fabric-bay-layout-note/, "the fallback is declared in the grid");
});

test("no mapped bays keeps the existing empty note", () => {
  const layout = CSE_946;
  const loaded = loadFunctions([...LAYOUT_FUNCTIONS, ...RENDER_FUNCTIONS], {
    ...bayGridContext(layout, {}),
    document: { createElement: fakeElement },
    grid: fakeElement("div"),
  });
  assert.equal(loaded.renderSasFabricBayChips([], 72), '<span class="sas-fabric-empty-note">No mapped bays</span>');
});

test("the chassis renderer and the fabric grid share one layout source", () => {
  assert.match(functionSource("renderChassisRows"), /buildLayoutGridRows\(/);
  assert.match(functionSource("renderSasFabricBayChips"), /buildLayoutGridRows\(/);
  assert.doesNotMatch(
    functionSource("renderSasFabricBayChips"),
    /sorted\.slice\(0, limit\)/,
    "the layout grid must not truncate the enclosure"
  );
});
