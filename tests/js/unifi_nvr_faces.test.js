"use strict";

// The UniFi NVR faces (#1068): per-model decoration (touchscreen, ports,
// vents, print, reset) is drawn around the bays, never as a bay. Only bays are
// buttons with data-slot; the decoration cannot be clicked, focused, counted,
// searched or exported, and slot indices keep their layout positions.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

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

const FUNCTIONS = [
  "escapeHtml",
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
  "unifiFaceVariantSpec",
  "buildUnifiFaceBand",
  "buildUnifiFaceDecor",
  "renderUnifiFaceRows",
  "renderChassisRows",
];

function fakeElement(tagName) {
  const attributes = new Map();
  const element = {
    tagName: String(tagName).toUpperCase(),
    className: "",
    innerHTML: "",
    children: [],
    dataset: {},
    style: { setProperty() {} },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      return attributes.has(name) ? attributes.get(name) : null;
    },
    hasAttribute(name) {
      return attributes.has(name);
    },
    appendChild(child) {
      element.children.push(child);
      return child;
    },
  };
  return element;
}

function loadRenderer(profile, layoutRows) {
  const slots = layoutRows.flat().filter(Number.isInteger).map((slot) => ({ slot, state: "matched" }));
  const sandbox = vm.createContext({
    document: { createElement: fakeElement },
    grid: fakeElement("div"),
    state: { snapshot: { slots, layout_slot_count: slots.length }, selectedSlot: null },
    getSmartSummaryEntry: () => null,
    getSelectedProfile: () => ({ row_groups: [] }),
    currentLayoutSlotCount: () => slots.length,
    activeLayoutRows: () => layoutRows,
  });
  vm.runInContext(`${FUNCTIONS.map(functionSource).join("\n")}\nthis.__loaded = { ${FUNCTIONS.join(", ")} };`, sandbox);
  const loaded = sandbox.__loaded;
  const target = fakeElement("div");
  // The shipped renderers append a <button class="slot-tile" data-slot> per bay.
  const appendTile = (container, slotValue) => {
    if (!Number.isInteger(slotValue)) {
      const gap = fakeElement("div");
      gap.className = "slot-gap";
      gap.setAttribute("aria-hidden", "true");
      container.appendChild(gap);
      return;
    }
    const tile = fakeElement("button");
    tile.className = "slot-tile state-healthy";
    tile.dataset.slot = String(slotValue);
    container.appendChild(tile);
  };
  const geometry = loaded.buildChassisGeometry(profile, layoutRows);
  loaded.renderChassisRows(layoutRows, geometry, appendTile, target);
  return { loaded, geometry, target };
}

function walk(node, visit) {
  visit(node);
  (node.children || []).forEach((child) => walk(child, visit));
}

function bayTiles(target) {
  const tiles = [];
  walk(target, (node) => {
    if (String(node.className).split(/\s+/).includes("slot-tile") && node.dataset?.slot !== undefined) {
      tiles.push(node);
    }
  });
  return tiles;
}

function decorNodes(target) {
  const nodes = [];
  walk(target, (node) => {
    if (/\bunifi-face-(?:decor|band)\b/.test(String(node.className))) {
      nodes.push(node);
    }
  });
  return nodes;
}

function rowCells(target) {
  return target.children
    .filter((node) => String(node.className).split(/\s+/).includes("slot-row"))
    .map((row) => row.children[0].children.map((cell) =>
      cell.dataset?.slot !== undefined ? Number(cell.dataset.slot) : String(cell.className).match(/unifi-face-(touchscreen|ports)/)?.[1] || cell.className));
}

const UNVR = {
  profile: { profileId: "ubiquiti-unvr-front-4", faceStyle: "unifi-drive", faceVariant: "unvr", baySize: "3.5", latchEdge: "bottom" },
  layoutRows: [[0, 1, 2, 3]],
};

const UNVR_PRO = {
  profile: { profileId: "ubiquiti-unvr-pro-front-7", faceStyle: "unifi-drive", faceVariant: "unvr-pro", baySize: "3.5", latchEdge: "bottom" },
  layoutRows: [[0, 1, 2, null], [3, 4, 5, 6]],
};

test("the UNVR Pro top row is touchscreen, bays 1-3, ports; the bottom row is bays 4-7", () => {
  const { target, geometry } = loadRenderer(UNVR_PRO.profile, UNVR_PRO.layoutRows);
  assert.equal(geometry.faceVariant, "unvr-pro");
  assert.deepEqual(rowCells(target), [
    ["touchscreen", 0, 1, 2, "ports"],
    [3, 4, 5, 6],
  ]);
  const [topRow, bottomRow] = target.children
    .filter((node) => String(node.className).split(/\s+/).includes("slot-row"))
    .map((row) => row.children[0]);
  // Five tracks on the top row: the decoration takes a share of one bay width
  // on each side, so bays 1-3 sit offset between the touchscreen and the ports.
  assert.equal(topRow.style.gridTemplateColumns.split(/\s+(?=minmax)/).length, 5);
  assert.equal(bottomRow.style.gridTemplateColumns.split(/\s+(?=minmax)/).length, 4);
  // The layout's empty cell is not drawn as a pill-shaped gap any more.
  walk(target, (node) => assert.notEqual(node.className, "slot-gap"));
});

test("UniFi decoration is never a bay: not a button, no data-slot, aria-hidden, not focusable", () => {
  for (const { profile, layoutRows } of [UNVR, UNVR_PRO]) {
    const { target } = loadRenderer(profile, layoutRows);
    const decor = decorNodes(target);
    // Top and bottom bands on both; the UNVR Pro adds its touchscreen and ports.
    assert.equal(decor.length, profile.faceVariant === "unvr-pro" ? 4 : 2, `${profile.faceVariant} draws its decoration`);
    for (const node of decor) {
      assert.equal(node.tagName, "DIV", node.className);
      assert.equal(node.dataset.slot, undefined, node.className);
      assert.ok(!String(node.className).split(/\s+/).includes("slot-tile"), node.className);
      assert.equal(node.getAttribute("aria-hidden"), "true", node.className);
      assert.equal(node.hasAttribute("tabindex"), false, node.className);
      assert.doesNotMatch(node.innerHTML, /<(?:button|a|input|select)\b|tabindex|data-slot/i, node.className);
    }
    // Bay count and slot indices are exactly the layout's.
    assert.deepEqual(
      bayTiles(target).map((tile) => Number(tile.dataset.slot)),
      layoutRows.flat().filter(Number.isInteger),
    );
  }
});

test("the UNVR face has a System LED bar, five top vents, NVR print and a RESET pinhole", () => {
  const { target } = loadRenderer(UNVR.profile, UNVR.layoutRows);
  assert.deepEqual(rowCells(target), [[0, 1, 2, 3]]);
  const [top] = target.children;
  const bottom = target.children[target.children.length - 1];
  assert.match(top.className, /unifi-face-band-top/);
  assert.match(top.innerHTML, /unifi-face-led-bar/);
  assert.equal((top.innerHTML.match(/class="unifi-face-vent"/g) || []).length, 5);
  assert.match(bottom.className, /unifi-face-band-bottom/);
  assert.match(bottom.innerHTML, />NVR</);
  assert.match(bottom.innerHTML, /unifi-face-reset/);
  assert.match(bottom.innerHTML, /RESET/);
});

test("the UNVR Pro face has five vents above and below, NVR Pro print, ports 1 and 2 and RESET", () => {
  const { target } = loadRenderer(UNVR_PRO.profile, UNVR_PRO.layoutRows);
  const top = target.children[0];
  const bottom = target.children[target.children.length - 1];
  assert.equal((top.innerHTML.match(/class="unifi-face-vent"/g) || []).length, 5);
  assert.equal((bottom.innerHTML.match(/class="unifi-face-vent"/g) || []).length, 5);
  assert.match(bottom.innerHTML, /RESET/);
  const decor = decorNodes(target);
  const screen = decor.find((node) => /unifi-face-touchscreen/.test(node.className));
  const ports = decor.find((node) => /unifi-face-ports/.test(node.className));
  assert.match(screen.innerHTML, /NVR Pro/);
  assert.equal(screen.getAttribute("title"), "Touchscreen (not a bay)");
  assert.match(ports.innerHTML, /unifi-face-port-rj45[\s\S]*>1<[\s\S]*unifi-face-port-sfp[\s\S]*>2</);
  assert.equal(ports.getAttribute("title"), "Network ports (not bays)");
});

test("a UniFi profile without a known variant keeps the plain face and its row groups", () => {
  for (const faceVariant of [undefined, null, "", "not-a-model"]) {
    const { target } = loadRenderer({ ...UNVR_PRO.profile, faceVariant }, UNVR_PRO.layoutRows);
    assert.equal(decorNodes(target).length, 0, String(faceVariant));
    // The shipped row-group path still draws the layout's empty cell as a gap.
    let gaps = 0;
    walk(target, (node) => { if (node.className === "slot-gap") gaps += 1; });
    assert.equal(gaps, 1);
  }
  // Other face styles never pick up UniFi decoration, even with a variant set.
  const { target } = loadRenderer({ ...UNVR.profile, faceStyle: "front-drive" }, UNVR.layoutRows);
  assert.equal(decorNodes(target).length, 0);
});

test("the view profile and chassis chrome carry the face variant", () => {
  assert.match(functionSource("buildViewProfile"), /faceVariant:/);
  assert.match(functionSource("renderViewChrome"), /dataset\.faceVariant/);
  assert.match(functionSource("getSelectedProfile"), /face_variant:/);
});

test("UniFi variant faces drop the grey side plates and the striped vent window", () => {
  assert.match(
    STYLE_SOURCE,
    /\.chassis-shell\[data-face-style="unifi-drive"\]\[data-face-variant\] \.service-zone \{\s*display: none;/,
  );
  assert.match(STYLE_SOURCE, /\.unifi-face-decor \{[^}]*pointer-events: none;/);
});
