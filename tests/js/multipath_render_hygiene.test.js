"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const SOURCES = [
  ["live asset", fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8")],
  ["checked public demo", fs.readFileSync(path.join(ROOT, "public-demo/index.html"), "utf8")],
];

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
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadRenderer(source) {
  const sandbox = vm.createContext({});
  const script = [
    functionSource(source, "escapeHtml"),
    functionSource(source, "sasFabricClassToken"),
    functionSource(source, "renderMultipathPills"),
    "this.__renderMultipathPills = renderMultipathPills;",
  ].join("\n");
  vm.runInContext(script, sandbox, { filename: "multipath-render-hygiene.behavior.js" });
  return sandbox.__renderMultipathPills;
}

for (const [label, source] of SOURCES) {
  test(`${label} bounds the multipath state class token and escapes its label`, () => {
    const renderMultipathPills = loadRenderer(source);
    const html = renderMultipathPills([
      {
        device_name: "path-a",
        state: 'active" data-unsafe="yes',
        controller_label: "Controller A",
      },
    ]);

    assert.doesNotMatch(html, /\sdata-unsafe=/);
    assert.match(html, /class="topology-pill path-state-[a-z0-9_-]+"/);
    assert.match(html, /<small>ACTIVE&quot; DATA-UNSAFE=&quot;YES \/ Controller A<\/small>/);
  });
}

const LIVE_SOURCE = SOURCES[0][1];

function loadLiveSlotTileRenderer(source) {
  const sandbox = vm.createContext({});
  const script = [
    "function isPlaceholderHintLabel() { return false; }",
    functionSource(source, "escapeHtml"),
    functionSource(source, "stateLabel"),
    functionSource(source, "slotPrimaryLabel"),
    functionSource(source, "buildLiveSlotTileMarkup"),
    "this.__buildLiveSlotTileMarkup = buildLiveSlotTileMarkup;",
  ].join("\n");
  vm.runInContext(script, sandbox, { filename: "live-slot-tile-hygiene.behavior.js" });
  return sandbox.__buildLiveSlotTileMarkup;
}

function loadTopologyRenderer(source) {
  const target = { innerHTML: "", querySelectorAll: () => [] };
  const sandbox = vm.createContext({ __topologyTarget: target });
  const script = [
    "const topologyContext = __topologyTarget;",
    "const state = { snapshot: { slots: [] } };",
    "function currentPlatform() { return 'truenas'; }",
    "function selectSlot() {}",
    functionSource(source, "escapeHtml"),
    functionSource(source, "sasFabricClassToken"),
    functionSource(source, "hasStoragePeerGroup"),
    functionSource(source, "sameStoragePeerGroup"),
    functionSource(source, "renderTopologyContext"),
    [
      "this.__renderTopologyContext = (slots, slot) => {",
      "  state.snapshot.slots = slots;",
      "  renderTopologyContext(slot);",
      "  return topologyContext.innerHTML;",
      "};",
    ].join("\n"),
  ].join("\n");
  vm.runInContext(script, sandbox, { filename: "topology-render-hygiene.behavior.js" });
  return sandbox.__renderTopologyContext;
}

test("live asset escapes the slot label in the live slot tile", () => {
  const buildLiveSlotTileMarkup = loadLiveSlotTileRenderer(LIVE_SOURCE);
  const html = buildLiveSlotTileMarkup({
    slot_label: '01"><img src=x onerror="boom">',
    state: "present",
    present: true,
    device_name: "sda",
    pool_name: "tank",
  });

  assert.doesNotMatch(html, /<img/);
  assert.match(html, /<span class="slot-number">01&quot;&gt;&lt;img src=x onerror=&quot;boom&quot;&gt;<\/span>/);
});

test("live asset bounds the topology pill state token and escapes its slot label", () => {
  const renderTopologyContext = loadTopologyRenderer(LIVE_SOURCE);
  const selected = {
    slot: 1,
    pool_name: "tank",
    vdev_name: "mirror-0",
    vdev_class: "data",
    device_name: "sda",
    state: "present",
    slot_label: "01",
    topology_label: "tank > mirror-0",
  };
  const peer = {
    slot: 2,
    pool_name: "tank",
    vdev_name: "mirror-0",
    vdev_class: "data",
    device_name: "sdb",
    state: 'present" data-unsafe="yes',
    slot_label: '02"><img src=x onerror="boom">',
  };
  const html = renderTopologyContext([selected, peer], selected);

  assert.doesNotMatch(html, /\sdata-unsafe=/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /class="topology-pill state-present-data-unsafe-yes"/);
  assert.match(html, /<span>02&quot;&gt;&lt;img src=x onerror=&quot;boom&quot;&gt;<\/span>/);
});
