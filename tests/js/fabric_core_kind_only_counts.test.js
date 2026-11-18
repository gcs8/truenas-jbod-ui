"use strict";

// The Storage Fabric page treats a payload as CORE by its platform or, when
// the platform is missing, by its fabric kind (`core_mpr`). A kind-only CORE
// payload without completeness fields must say its counts were not reported,
// so a reader that stops recognising the kind fails here. The fixture holds
// CORE builder output; only the platform and completeness fields change.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const CASES = JSON.parse(
  fs.readFileSync(path.join(ROOT, "tests/fixtures/sas_fabric/core_no_controller_fabric.json"), "utf8"),
);
const SUMMARY_IDS = {
  controllers: "fabric-summary-controllers",
  paths: "fabric-summary-paths",
  expanders: "fabric-summary-expanders",
  enclosures: "fabric-summary-enclosures",
};
const PAGE_MODES = ["lanes", "impact", "trace", "disk"];

function textOf(html) {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function renderPage(fabric, mode, slots) {
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots }, fabric } },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", dataset: {}, className: "", closest: () => null });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    window.renderTest = { renderMap, renderSummary, state };
  })();`, sandbox);
  const api = sandbox.window.renderTest;
  api.state.fabric = fabric;
  api.state.snapshot = { ...api.state.snapshot, slots };
  api.state.loading = false;
  api.state.error = null;
  api.state.mode = mode;
  api.renderMap();
  api.renderSummary();
  const read = (id) => elements.get(id) || { innerHTML: "", textContent: "" };
  return {
    map: textOf(read("fabric-map-panel").innerHTML),
    summary: Object.fromEntries(Object.entries(SUMMARY_IDS).map(([key, id]) => [key, read(id).textContent])),
  };
}

function kindOnly(caseName, { completeness = false } = {}) {
  const fabric = JSON.parse(JSON.stringify(CASES[caseName].fabric));
  delete fabric.platform;
  fabric.raw.fabric_kind = "core_mpr";
  if (!completeness) delete fabric.raw.controller_read;
  return fabric;
}

test("kind-only CORE payload without completeness: the summary says not reported in every page mode", () => {
  const fabric = kindOnly("complete");
  assert.equal(fabric.platform, undefined);
  assert.equal(fabric.raw.controller_read, undefined);
  for (const mode of PAGE_MODES) {
    const { summary } = renderPage(fabric, mode, CASES.complete.slots);
    assert.deepEqual({ ...summary }, {
      controllers: "1, completeness not reported",
      paths: "not reported",
      expanders: "not reported",
      enclosures: "not reported",
    }, mode);
  }
});

test("kind-only CORE payload without completeness: non-zero counts are never plain numbers", () => {
  const { summary, map } = renderPage(kindOnly("healthy_lists"), "lanes", CASES.healthy_lists.slots);
  assert.equal(summary.expanders, "1, completeness not reported");
  assert.equal(summary.enclosures, "1, completeness not reported");
  assert.match(map, /completeness not reported/);
});

test("kind-only CORE payload with completeness reported: zero stays 0", () => {
  const { summary } = renderPage(kindOnly("complete", { completeness: true }), "lanes", CASES.complete.slots);
  assert.deepEqual({ ...summary }, { controllers: "1", paths: "0", expanders: "0", enclosures: "0" });
});

test("control: a payload with neither the CORE platform nor the CORE kind keeps its plain counts", () => {
  const fabric = kindOnly("healthy_lists");
  fabric.raw.fabric_kind = "linux_ses";
  const { summary } = renderPage(fabric, "lanes", CASES.healthy_lists.slots);
  assert.deepEqual({ ...summary }, { controllers: "1", paths: "0", expanders: "1", enclosures: "1" });
});
