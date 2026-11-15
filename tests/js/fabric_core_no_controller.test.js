"use strict";

// CORE Storage Fabric with no readable controller output: the bays, pools and
// vdevs the inventory already knows are still drawn, with a plain notice that
// controller data could not be read. The fixture is CORE builder output; the
// Python test in tests/test_core_fabric_legacy_controller.py keeps it current.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const FIXTURE = JSON.parse(
  fs.readFileSync(path.join(ROOT, "tests/fixtures/sas_fabric/core_no_controller_fabric.json"), "utf8"),
);
const NO_CONTROLLER_NOTICE = "No CORE SAS controller/path data is available yet. Check SSH and mprutil permissions.";
const UNAVAILABLE_MAP = "No Storage Fabric map is available for this platform yet.";

const LANES_NOTICE = "Controller data could not be read. The bays below come from the inventory.";
const NO_LANES = "No controller lanes are available yet.";

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
}

function loadPage(fabric, mode = "lanes") {
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots: [] }, fabric } },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", dataset: {}, className: "" });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    window.renderTest = { renderMap, renderStatus, renderWarnings, metricRows, state };
  })();`, sandbox);
  const api = sandbox.window.renderTest;
  if (mode === null) return api;
  api.state.fabric = fabric;
  api.state.loading = false;
  api.state.error = null;
  api.state.mode = mode;
  api.renderMap();
  api.renderStatus();
  api.renderWarnings();
  const read = (id) => elements.get(id) || { innerHTML: "", textContent: "", dataset: {} };
  return {
    map: read("fabric-map-panel").innerHTML,
    chip: read("fabric-api-chip").textContent,
    status: read("fabric-status-text").textContent,
    warnings: read("fabric-warning-list").innerHTML,
  };
}

test("fixture is the no-controller CORE payload", () => {
  assert.equal(FIXTURE.platform, "core");
  assert.equal(FIXTURE.available, true);
  assert.deepEqual(FIXTURE.controllers, []);
  assert.deepEqual(FIXTURE.warnings, [NO_CONTROLLER_NOTICE]);
  const kinds = new Set(FIXTURE.nodes.map((node) => node.kind));
  for (const kind of ["bay", "pool", "vdev"]) assert.ok(kinds.has(kind), kind);
  assert.ok(!kinds.has("controller"));
});

test("default lanes view draws the known bays with a plain controller notice", () => {
  const page = loadPage(FIXTURE, "lanes");
  assert.doesNotMatch(page.map, new RegExp(escapeRegExp(UNAVAILABLE_MAP)));
  assert.doesNotMatch(page.map, new RegExp(escapeRegExp(NO_LANES)));
  assert.match(page.map, new RegExp(escapeRegExp(LANES_NOTICE)));
  for (const node of FIXTURE.nodes.filter((item) => item.kind === "bay")) {
    assert.match(page.map, new RegExp(`data-fabric-trace="${escapeRegExp(node.id)}"`), node.id);
  }
  assert.equal(page.chip, "STORAGE PARTIAL");
  assert.match(page.status, new RegExp(`partial evidence\\. ${escapeRegExp(NO_CONTROLLER_NOTICE)}`));
  assert.match(page.warnings, new RegExp(escapeRegExp(NO_CONTROLLER_NOTICE)));
});

test("trace view draws the bay, pool and vdev the inventory knows", () => {
  const page = loadPage(FIXTURE, "trace");
  for (const node of FIXTURE.nodes.filter((item) => ["bay", "pool", "vdev"].includes(item.kind))) {
    assert.match(page.map, new RegExp(`data-fabric-node="${escapeRegExp(node.id)}"`), node.id);
  }
});

test("control: lanes without bays and other platforms keep the old empty text", () => {
  const noBays = { ...FIXTURE, nodes: FIXTURE.nodes.filter((node) => node.kind !== "bay") };
  assert.match(loadPage(noBays, "lanes").map, new RegExp(escapeRegExp(NO_LANES)));
  assert.match(loadPage({ ...FIXTURE, platform: "scale" }, "lanes").map, new RegExp(escapeRegExp(NO_LANES)));
});

test("unknown PHY counts add no inspector row; reported counts do", () => {
  const { metricRows } = loadPage(FIXTURE, null);
  const labels = { linked_phys: "Linked PHYs" };
  const unknown = metricRows({ phy_count: null, linked_phy_count: null, linked_phys: null }, labels);
  assert.doesNotMatch(unknown, /phy count|Linked PHYs/);
  const reported = metricRows({ phy_count: 2, linked_phy_count: 1, linked_phys: 1 }, labels);
  assert.match(reported, /phy count/);
  assert.match(reported, /linked phy count/);
  assert.match(reported, /Linked PHYs/);
});

test("control: an unavailable payload still hides the map", () => {
  const page = loadPage({ ...FIXTURE, available: false });
  assert.match(page.map, new RegExp(escapeRegExp(UNAVAILABLE_MAP)));
  assert.equal(page.chip, "STORAGE OFF");
});
