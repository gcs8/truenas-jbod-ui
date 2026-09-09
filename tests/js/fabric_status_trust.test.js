"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(name) {
  const start = FABRIC_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = FABRIC_SOURCE.indexOf(")", start);
  const bodyStart = FABRIC_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < FABRIC_SOURCE.length; index += 1) {
    const character = FABRIC_SOURCE[index];
    const next = FABRIC_SOURCE[index + 1];
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
      if (depth === 0) return FABRIC_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadTrustStatus() {
  const sandbox = vm.createContext({ Array, Object, Set, String });
  const source = [
    functionSource("isSasFabricEnrichmentWarning"),
    functionSource("fabricTrustStatus"),
  ].join("\n");
  vm.runInContext(`${source}\nthis.__loaded = fabricTrustStatus;`, sandbox);
  return sandbox.__loaded;
}

test("stale and trusted-fallback cache states cannot report Storage Fabric OK", () => {
  const fabricTrustStatus = loadTrustStatus();
  for (const [field, cacheState] of [
    ["snapshot_cache_state", "stale-hit"],
    ["snapshot_cache_state", "trusted-fallback"],
    ["source_cache_state", "stale-hit"],
  ]) {
    const status = fabricTrustStatus({
      available: true,
      traces: [{ id: "trace-a" }],
      [field]: cacheState,
    }, "STORAGE");
    assert.equal(status.suffix, "STALE");
    assert.equal(status.chipTone, "partial");
    assert.equal(status.statusTone, "error");
    assert.match(status.message, /retained/i);
  }
});

test("an enabled failed source reports partial Storage Fabric evidence", () => {
  const fabricTrustStatus = loadTrustStatus();
  const status = fabricTrustStatus({
    available: true,
    traces: [{ id: "trace-a" }, { id: "trace-b" }],
    snapshot_cache_state: "hit",
    source_cache_state: "hit",
    sources: {
      api: { enabled: true, ok: true },
      ssh: { enabled: true, ok: false },
      bmc: { enabled: false, ok: false },
    },
  }, "STORAGE");

  assert.equal(status.suffix, "PARTIAL");
  assert.equal(status.chipTone, "partial");
  assert.equal(status.statusTone, "error");
  assert.match(status.message, /2 traces/i);
  assert.match(status.message, /ssh/i);
  assert.doesNotMatch(status.message, /bmc/i);
});

test("a non-informational warning reports partial Storage Fabric evidence", () => {
  const fabricTrustStatus = loadTrustStatus();
  const status = fabricTrustStatus({
    available: true,
    traces: [{ id: "trace-a" }],
    snapshot_cache_state: "miss",
    source_cache_state: "hit",
    warnings: ["SSH connection failed before inventory commands could run."],
  }, "STORAGE");

  assert.equal(status.suffix, "PARTIAL");
  assert.match(status.message, /SSH connection failed/);
});

test("ordinary evidence-scope notes do not turn a healthy map partial", () => {
  const fabricTrustStatus = loadTrustStatus();
  const status = fabricTrustStatus({
    available: true,
    traces: [{ id: "trace-a" }],
    snapshot_cache_state: "miss",
    source_cache_state: "hit",
    sources: { api: { enabled: true, ok: true } },
    warnings: ["Storage Fabric is built from Linux SES slot evidence."],
  }, "STORAGE");

  assert.equal(status.suffix, "OK");
  assert.equal(status.chipTone, "ok");
  assert.equal(status.statusTone, "info");
});

function loadDiskRenderers({ metrics = {}, slot = null, label = "Disk 1" } = {}) {
  const trace = { id: "bay:0", kind: "bay", label, slots: [0], node_ids: [], link_ids: [], metrics };
  const diskNode = { id: "bay:0", kind: "bay", label, related_slots: [0], metrics };
  trace.node_ids = [diskNode.id];
  const fabric = { available: true, traces: [trace], nodes: [diskNode], links: [], aliases: {} };
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: {
      snapshot: { slots: slot ? [{ slot: 0, ...slot }] : [] }, fabric,
    } },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "" });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  // Load all production renderer functions, but not DOM event wiring or startup I/O.
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    state.selectedTraceId = "bay:0";
    state.selectedDiskTraceId = "bay:0";
    window.renderTest = { renderInspector, renderDiskPathMode, renderFocusStrip, renderTraceSummaryButton, renderBayChips, renderDiskPathBayChip, state };
  })();`, sandbox);
  return { ...sandbox.window.renderTest, fabric, trace, elements };
}

function visibleMarkup(markup) {
  return markup.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

for (const [name, payload] of [
  ["trace provenance without snapshot", { metrics: { physical_location_known: false } }],
  ["slot provenance", { slot: { physical_location_known: false } }],
  ["trace denial overrides physical slot", { metrics: { physical_location_known: false }, slot: { physical_location_known: true } }],
  ["slot denial overrides physical trace", { metrics: { physical_location_known: true }, slot: { physical_location_known: false } }],
  ["virtual slot provenance", { slot: { raw_status: { virtual_enclosure: true } } }],
  ["missing provenance", {}],
]) {
  test(`disk inspector and siblings do not assert physical bays: ${name}`, () => {
    const ui = loadDiskRenderers(payload);
    ui.renderInspector(ui.fabric);
    assert.equal(ui.elements.get("fabric-inspector-title").textContent, "Selected Disk 1");
    const inspector = ui.elements.get("fabric-inspector-body").innerHTML;
    assert.match(inspector, /Selected Disk<\/span>/);
    assert.match(inspector, /<strong>Disk 1<\/strong>/);
    assert.doesNotMatch(visibleMarkup(inspector), /\bbays?\b|\bbackplane\b/i);
    assert.match(inspector, /data-fabric-alias-edit="bay:0"/);
    const diskPath = ui.renderDiskPathMode(ui.fabric);
    assert.match(diskPath, /<strong>Disk 1<\/strong>/);
    assert.doesNotMatch(visibleMarkup(diskPath), /\bBays?\b|\bBackplane\b/);
    ui.renderFocusStrip(ui.fabric);
    assert.doesNotMatch(visibleMarkup(ui.elements.get("fabric-focus-strip").innerHTML), /\bBay\b/);
    assert.doesNotMatch(visibleMarkup(ui.renderTraceSummaryButton(ui.trace)), /\bBay\b/);
    assert.doesNotMatch(ui.renderBayChips([0]), /title="Bay /);
    assert.doesNotMatch(ui.renderDiskPathBayChip(0, 0, new Set([0])), /title="Bay /);
    ui.state.selectedTraceId = null;
    ui.state.selectedNodeId = "bay:0";
    ui.renderInspector(ui.fabric);
    assert.equal(ui.elements.get("fabric-inspector-title").textContent, "Selected Disk");
    assert.doesNotMatch(visibleMarkup(ui.elements.get("fabric-inspector-body").innerHTML), /\bbays?\b/i);
  });
}

test("known physical disk keeps bay labels and alias identity", () => {
  const ui = loadDiskRenderers({ slot: { physical_location_known: true }, label: "Bay 00" });
  ui.renderInspector(ui.fabric);
  assert.equal(ui.elements.get("fabric-inspector-title").textContent, "Selected Bay 00");
  assert.match(ui.elements.get("fabric-inspector-body").innerHTML, /Selected Bay<\/span>/);
  assert.match(ui.renderDiskPathMode(ui.fabric), /<strong>Bay 00<\/strong>/);
  assert.match(ui.renderDiskPathMode(ui.fabric), /Backplane Zone/);
  assert.match(ui.elements.get("fabric-inspector-body").innerHTML, /data-fabric-alias-edit="bay:0"/);
});

test("renderStatus delegates available payload wording to fabricTrustStatus", () => {
  assert.match(functionSource("renderStatus"), /fabricTrustStatus\(fabric, copy\.statusBase\)/);
});
