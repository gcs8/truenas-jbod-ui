"use strict";

// Removed unreachable branches and unused selectors stay removed, and the
// paths that survive them keep working.
// - An NVMe carrier saved view is drawn only by the dedicated board branch of
//   renderStorageViewGrid, which returns before the generic tile callback
//   exists, so that callback carries no NVMe arms.
// - .snapshot-banner-facts, .snapshot-banner-fact and .builder-panel-note have
//   no emitter in any template, script or Python source.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const STYLE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");
const ADMIN_STYLE = fs.readFileSync(path.join(ROOT, "admin_service/static/admin.css"), "utf8");

function functionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = APP_SOURCE.indexOf("{", APP_SOURCE.indexOf(")", start));
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

function fakeElement(tagName) {
  const attributes = new Map();
  const classes = new Set();
  const element = {
    tagName: String(tagName).toUpperCase(),
    className: "",
    innerHTML: "",
    children: [],
    dataset: {},
    style: {},
    classList: {
      add(name) {
        classes.add(name);
      },
      contains(name) {
        return classes.has(name) || String(element.className).split(/\s+/).includes(name);
      },
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      return attributes.has(name) ? attributes.get(name) : null;
    },
    appendChild(child) {
      element.children.push(child);
      return child;
    },
  };
  return element;
}

function walk(node, visit) {
  visit(node);
  (node.children || []).forEach((child) => walk(child, visit));
}

test("an NVMe carrier saved view is drawn by the dedicated board branch only", () => {
  const nvmeMarkupCalls = [];
  const view = {
    id: "synthetic-carrier",
    kind: "nvme_carrier",
    slot_layout: [[0], [1]],
    slots: [
      { slot_index: 0, slot_label: "M.2 1", state: "matched", slot_size: "2280" },
      { slot_index: 1, slot_label: "M.2 2", state: "empty", slot_size: null },
    ],
  };
  const sandbox = vm.createContext({
    document: { createElement: fakeElement },
    grid: fakeElement("div"),
    state: { selectedSlot: 1 },
    hideSlotTooltip() {},
    getSelectedPeerContext: () => ({ active: false, peerSlots: new Set() }),
    buildViewProfile: () => ({}),
    buildHeatmapContext: () => ({ enabled: false }),
    renderHeatmapControls() {},
    nvmeCarrierBoardLayout: () => ({ className: "synthetic-board", imageSrc: "board.svg", edgeNote: "Edge" }),
    nvmeCarrierSlotMetrics: () => ({ left: "1%", top: "2%", width: "3%", minHeight: "4%" }),
    storageViewRuntimeMatchesFilter: () => true,
    buildNvmeRuntimeTileMarkup(slot, selectedView) {
      nvmeMarkupCalls.push([slot.slot_index, selectedView.kind]);
      return `<span>nvme ${slot.slot_index}</span>`;
    },
    applyHeatmapToTile() {},
    bayName() {},
    buildStorageViewRuntimeTooltip: () => "",
    buildChassisGeometry() {
      throw new Error("the generic chassis path must not run for an NVMe carrier");
    },
    renderChassisRows() {
      throw new Error("the generic tile callback must not run for an NVMe carrier");
    },
  });
  vm.runInContext(`${functionSource("renderStorageViewGrid")}\nthis.__render = renderStorageViewGrid;`, sandbox);
  const target = fakeElement("div");
  sandbox.__render(view, target);

  assert.equal(target.children.length, 1);
  const board = target.children[0];
  assert.equal(board.className, "nvme-carrier-canvas synthetic-board");
  const tiles = [];
  walk(board, (node) => {
    if (node.dataset?.slot !== undefined) tiles.push(node);
  });
  assert.deepEqual(tiles.map((tile) => tile.dataset.slot), ["0", "1"]);
  for (const tile of tiles) {
    assert.match(tile.className, /\bstorage-view-slot-nvme\b/);
    assert.match(tile.className, /\bstorage-view-slot-nvme-absolute\b/);
  }
  assert.match(tiles[0].className, /\bstate-healthy\b/);
  assert.equal(tiles[1].classList.contains("selected"), true);
  assert.deepEqual(nvmeMarkupCalls, [[0, "nvme_carrier"], [1, "nvme_carrier"]]);
  assert.equal(tiles[1].innerHTML, "<span>nvme 1</span>");
});

test("the generic tile callback carries no NVMe arms after the dedicated branch returns", () => {
  const render = functionSource("renderStorageViewGrid");
  const dedicated = render.indexOf('if (selectedView.kind === "nvme_carrier") {');
  const callback = render.indexOf("const appendTile = ");
  assert.notEqual(dedicated, -1, "the dedicated NVMe branch must remain");
  assert.notEqual(callback, -1, "the generic tile callback must remain");
  const branch = render.slice(dedicated, callback);
  assert.match(branch, /buildNvmeRuntimeTileMarkup\(slot, selectedView\)/, "the dedicated branch keeps the NVMe renderer");
  assert.match(branch, /target\.appendChild\(board\);\s*return;\s*\}\s*const geometry = buildChassisGeometry\(profile, slotLayout\);\s*$/, "the dedicated branch returns before the callback");
  const generic = render.slice(callback);
  assert.doesNotMatch(generic, /nvme/i, "no NVMe arm after the dedicated branch has returned");
  assert.match(generic, /storage-view-slot-boot/, "the boot-device arm stays");
  assert.match(generic, /buildSatadomRuntimeTileMarkup\(slot, selectedView\)/, "the boot-device markup stays");
  assert.match(APP_SOURCE, /function buildNvmeRuntimeTileMarkup\(/, "the NVMe renderer stays");
});

function sourceFiles(directory, extensions, found = []) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === ".git" || entry.name === "public-demo") continue;
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) sourceFiles(full, extensions, found);
    else if (extensions.has(path.extname(entry.name))) found.push(full);
  }
  return found;
}

test("unused selectors stay removed and the live builder copy rule stays", () => {
  const tokens = ["snapshot-banner-facts", "snapshot-banner-fact", "builder-panel-note"];
  const emitters = [
    ...sourceFiles(path.join(ROOT, "app"), new Set([".html", ".js", ".py"])),
    ...sourceFiles(path.join(ROOT, "admin_service"), new Set([".html", ".js", ".py"])),
  ];
  assert.ok(emitters.length > 0);
  for (const token of tokens) {
    const exact = new RegExp(`(?<![A-Za-z0-9_-])${token}(?![A-Za-z0-9_-])`);
    for (const file of emitters) {
      assert.doesNotMatch(fs.readFileSync(file, "utf8"), exact, `${path.relative(ROOT, file)} must not emit ${token}`);
    }
    assert.doesNotMatch(STYLE, new RegExp(`\\.${token}(?![A-Za-z0-9_-])`), `${token} CSS must stay deleted`);
    assert.doesNotMatch(ADMIN_STYLE, new RegExp(`\\.${token}(?![A-Za-z0-9_-])`), `${token} CSS must stay deleted`);
  }
  assert.match(
    ADMIN_STYLE,
    /\n\.builder-panel-copy \.panel-copy \{\n {2}margin: 0;\n {2}max-width: 80ch;\n\}/,
    "the live builder copy rule keeps its declarations",
  );
  assert.match(STYLE, /\n\.snapshot-banner-copy \.subtle \{/, "neighbouring banner rules stay");
});
