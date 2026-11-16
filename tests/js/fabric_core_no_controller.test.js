"use strict";

// CORE Storage Fabric when controller commands return nothing or only part of
// what was asked: the bays, pools and vdevs the inventory already knows are
// still drawn, a count whose read did not happen says "not read" (never 0),
// and the status is never OK. The fixture holds CORE builder output; the
// Python test in tests/test_core_fabric_legacy_controller.py keeps it current.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const CASES = JSON.parse(
  fs.readFileSync(path.join(ROOT, "tests/fixtures/sas_fabric/core_no_controller_fabric.json"), "utf8"),
);
const FIXTURE = CASES.no_controller_bays.fabric;
const NO_CONTROLLER_NOTICE = "No CORE SAS controller/path data is available yet. Check SSH and mprutil permissions.";
const UNREADABLE_UNIT_NOTICE = "CORE controller data could not be read for mpr1, so controller, expander and enclosure counts may be incomplete.";
const UNAVAILABLE_MAP = "No Storage Fabric map is available for this platform yet.";

const BAYS_NOTICE = "Controller data could not be read. The bays below come from the inventory.";
const DISKS_NOTICE = "Controller data could not be read. The disks below come from the inventory.";
const NO_LANES = "No controller lanes are available yet.";
const SUMMARY_IDS = {
  controllers: "fabric-summary-controllers",
  paths: "fabric-summary-paths",
  expanders: "fabric-summary-expanders",
  enclosures: "fabric-summary-enclosures",
};

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
}

function textOf(html) {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function loadPage(fabric, mode = "lanes", slots = []) {
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
    window.renderTest = { renderMap, renderStatus, renderWarnings, renderSummary, metricRows, state, phyFact: typeof phyFact === "function" ? phyFact : undefined };
  })();`, sandbox);
  const api = sandbox.window.renderTest;
  if (mode === null) return api;
  api.state.fabric = fabric;
  api.state.snapshot = { ...api.state.snapshot, slots };
  api.state.loading = false;
  api.state.error = null;
  api.state.mode = mode;
  api.renderMap();
  api.renderStatus();
  api.renderWarnings();
  api.renderSummary();
  const read = (id) => elements.get(id) || { innerHTML: "", textContent: "", dataset: {} };
  return {
    map: read("fabric-map-panel").innerHTML,
    chip: read("fabric-api-chip").textContent,
    status: read("fabric-status-text").textContent,
    warnings: read("fabric-warning-list").innerHTML,
    summary: Object.fromEntries(Object.entries(SUMMARY_IDS).map(([key, id]) => [key, read(id).textContent])),
  };
}

function loadCase(name, mode = "lanes") {
  return loadPage(CASES[name].fabric, mode, CASES[name].slots);
}

function appFunctionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = APP_SOURCE.indexOf("{", APP_SOURCE.indexOf(")", start));
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

function loadAppCountHelpers() {
  const sandbox = vm.createContext({});
  const names = ["sasFabricList", "sasFabricCountPhrase", "sasFabricCountSummary", "sasFabricNoControllerText"];
  vm.runInContext(`${names.map(appFunctionSource).join("\n")}\nthis.helpers = { sasFabricCountSummary, sasFabricNoControllerText };`, sandbox);
  return sandbox.helpers;
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
  const page = loadCase("no_controller_bays");
  assert.doesNotMatch(page.map, new RegExp(escapeRegExp(UNAVAILABLE_MAP)));
  assert.doesNotMatch(page.map, new RegExp(escapeRegExp(NO_LANES)));
  assert.match(page.map, new RegExp(escapeRegExp(BAYS_NOTICE)));
  assert.match(page.map, />Impacted Bays</);
  for (const node of FIXTURE.nodes.filter((item) => item.kind === "bay")) {
    assert.match(page.map, new RegExp(`data-fabric-trace="${escapeRegExp(node.id)}"`), node.id);
  }
  assert.equal(page.chip, "STORAGE PARTIAL");
  assert.match(page.status, new RegExp(`partial evidence\\. ${escapeRegExp(NO_CONTROLLER_NOTICE)}`));
  assert.match(page.warnings, new RegExp(escapeRegExp(NO_CONTROLLER_NOTICE)));
});

test("unread controller counts say not read in the summary, host line and disk facts", () => {
  const lanes = loadCase("no_controller_bays");
  assert.deepEqual(lanes.summary, { controllers: "not read", paths: "not read", expanders: "not read", enclosures: "not read" });
  const host = textOf(lanes.map);
  assert.match(host, /controllers not read \/ paths not read \/ 1 bay/);
  assert.doesNotMatch(host, /\b0 controllers\b|\b0 paths\b/);

  const disk = textOf(loadCase("no_controller_bays", "disk").map);
  assert.match(disk, /Controllers not read/);
  assert.doesNotMatch(disk, /Controllers 0\b/);
});

test("a readable unit beside an unreadable one is partial and names what was not read", () => {
  const page = loadCase("unreadable_second_unit");
  assert.equal(page.chip, "STORAGE PARTIAL");
  assert.match(page.status, new RegExp(escapeRegExp(UNREADABLE_UNIT_NOTICE)));
  assert.match(page.warnings, new RegExp(escapeRegExp(UNREADABLE_UNIT_NOTICE)));
  assert.deepEqual(page.summary, { controllers: "at least 1", paths: "0", expanders: "not read", enclosures: "not read" });
  assert.match(textOf(page.map), /at least 1 controller \/ 0 paths \/ 1 bay/);
  assert.match(page.map, /data-fabric-node="controller:mpr0"/);
  assert.match(textOf(loadCase("unreadable_second_unit", "disk").map), /Controllers at least 1/);
});

test("control: a whole read keeps its counts, including known zeroes, and stays OK", () => {
  const page = loadCase("complete");
  assert.equal(page.chip, "STORAGE OK");
  assert.deepEqual(page.summary, { controllers: "1", paths: "0", expanders: "0", enclosures: "0" });
  assert.match(textOf(page.map), /1 controller \/ 0 paths \/ 1 bay/);
  assert.match(textOf(loadCase("complete", "disk").map), /Controllers 1/);
});

test("control: a payload without read facts keeps its counts as before", () => {
  const { controller_read: _ignored, ...raw } = CASES.complete.fabric.raw;
  const page = loadPage({ ...CASES.complete.fabric, raw }, "lanes", CASES.complete.slots);
  assert.equal(page.chip, "STORAGE OK");
  assert.deepEqual(page.summary, { controllers: "1", paths: "0", expanders: "0", enclosures: "0" });
});

test("a count marked not read keeps the status partial even without a warning", () => {
  const page = loadPage({ ...CASES.unreadable_second_unit.fabric, warnings: [] }, "lanes", CASES.unreadable_second_unit.slots);
  assert.equal(page.chip, "STORAGE PARTIAL");
  assert.match(page.status, /Some controller data was not read\./);
});

test("no-controller lanes word unmapped inventory as disks, not bays", () => {
  for (const name of ["no_controller_virtual", "no_controller_mixed"]) {
    const page = loadCase(name);
    assert.match(page.map, new RegExp(escapeRegExp(DISKS_NOTICE)), name);
    assert.match(page.map, />Impacted Disks</, name);
    assert.doesNotMatch(page.map, /The bays below|Impacted Bays/, name);
  }
});

test("trace view draws the bay, pool and vdev the inventory knows", () => {
  const page = loadCase("no_controller_bays", "trace");
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

test("disk-path PHY fact: an unread table adds no fact; a whole table keeps its zero", () => {
  const { phyFact } = loadPage(FIXTURE, null);
  assert.equal(phyFact(null, "2"), null);
  assert.equal(phyFact(undefined, undefined), null);
  assert.equal(phyFact(0, "1"), "0/1");
  assert.equal(phyFact(1, "2"), "1/2");
  assert.equal(phyFact(1, null), "1");
});

test("control: an unavailable payload still hides the map", () => {
  const page = loadPage({ ...FIXTURE, available: false });
  assert.match(page.map, new RegExp(escapeRegExp(UNAVAILABLE_MAP)));
  assert.equal(page.chip, "STORAGE OFF");
});

test("main page topology summary uses the same not-read rule", () => {
  const { sasFabricCountSummary: summary, sasFabricNoControllerText: noController } = loadAppCountHelpers();
  assert.equal(summary(CASES.no_controller_bays.fabric), "sources not read / paths not read / expanders not read / enclosures not read / 1 traces");
  assert.equal(summary(CASES.unreadable_second_unit.fabric), "at least 1 source / 0 paths / expanders not read / enclosures not read / 1 traces");
  assert.equal(summary(CASES.complete.fabric), "1 source / 0 paths / 0 expanders / 0 enclosures / 1 traces");
  assert.deepEqual({ ...noController(CASES.no_controller_bays.fabric, false) }, {
    meta: "HBA controllers not read",
    message: "Controller data could not be read. Check the SSH setup for this system in the admin console.",
  });
  const { controller_read: _ignored, ...raw } = CASES.no_controller_bays.fabric.raw;
  assert.equal(noController({ ...CASES.no_controller_bays.fabric, raw }, false).meta, "No HBA controllers reported");
  assert.equal(noController(CASES.no_controller_bays.fabric, true).meta, "No storage sources reported");
});
