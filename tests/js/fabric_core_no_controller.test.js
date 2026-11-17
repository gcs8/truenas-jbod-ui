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
const UNATTACHED_BAYS_NOTICE = "Not attached to a read controller. Some controller data was not read, so these bays come from the inventory.";
const PHY_TABLE_NOTICE = "CORE PHY tables could not be read whole for 1 controller, so their linked PHY counts were not read.";
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

test("a CORE payload without read facts says its counts were not reported, not 0", () => {
  const { controller_read: _ignored, ...raw } = CASES.complete.fabric.raw;
  const page = loadPage({ ...CASES.complete.fabric, raw }, "lanes", CASES.complete.slots);
  // Counts nobody vouched for do not leave the page OK, and the known bay stays drawn.
  assert.equal(page.chip, "STORAGE PARTIAL");
  assert.match(page.status, /Completeness of some controller data was not reported\./);
  assert.match(page.map, /data-fabric-trace="bay:0"/);
  assert.deepEqual(page.summary, {
    controllers: "1, completeness not reported",
    paths: "not reported",
    expanders: "not reported",
    enclosures: "not reported",
  });
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

test("list replies that were not read say not read in the summary and the lane, never no objects", () => {
  const page = loadCase("unparsed_lists");
  assert.equal(page.chip, "STORAGE PARTIAL");
  assert.equal(page.summary.expanders, "not read");
  assert.equal(page.summary.enclosures, "not read");
  const lanes = textOf(page.map);
  assert.equal((lanes.match(/Not read/g) || []).length, 2, lanes);
  assert.doesNotMatch(lanes, /No objects reported/);
  // Control: lists that were read and printed empty are measured zeroes.
  const whole = textOf(loadCase("complete").map);
  assert.equal((whole.match(/No objects reported/g) || []).length, 2, whole);
  assert.doesNotMatch(whole, /Not read/);
});

test("a partial read still draws inventory bays no controller lane claims, with their pool and vdev", () => {
  const page = loadCase("unreadable_second_unit");
  assert.match(page.map, new RegExp(escapeRegExp(UNATTACHED_BAYS_NOTICE)));
  assert.match(page.map, /data-fabric-trace="bay:0"[^>]*title="da0 \/ tank \/ mirror-0"/);
  assert.match(page.map, /data-fabric-node="controller:mpr0"/);
  // Control: a whole read keeps its lanes as before, with no extra group.
  const whole = loadCase("complete");
  assert.doesNotMatch(whole.map, /Not attached to a read controller/);
  assert.doesNotMatch(whole.map, /data-fabric-trace="bay:0"/);
});

test("a rejected PHY table is not read, never absent or zero, and the view is partial", () => {
  const page = loadCase("phy_table_rejected");
  assert.equal(page.chip, "STORAGE PARTIAL");
  assert.match(page.status, new RegExp(escapeRegExp(PHY_TABLE_NOTICE)));
  const { metricRows, phyFact } = loadPage(FIXTURE, null);
  const [controller] = CASES.phy_table_rejected.fabric.nodes.filter((node) => node.kind === "controller");
  const rows = textOf(metricRows(controller.metrics));
  assert.match(rows, /phy count not read/);
  assert.match(rows, /linked phy count not read/);
  assert.equal(phyFact(null, null, true), "not read");
  assert.match(textOf(metricRows({ slots: "1", device_count: null, not_read: ["device_count"] })), /device count not read/);
  // The status also stays partial from the read facts alone.
  const quiet = loadPage({ ...CASES.phy_table_rejected.fabric, warnings: [] }, "lanes", CASES.phy_table_rejected.slots);
  assert.equal(quiet.chip, "STORAGE PARTIAL");
});

// Complete, successful replies in shapes mprutil prints: each shows at least
// what main showed, with no warning and no lower status. An expander whose
// enclosure handle is blank is read but not drawn in the selected enclosure's
// scope, as on main.
const HEALTHY_SUMMARIES = {
  healthy_lists: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
  blank_expander_parent: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
  blank_expander_enclosure: { controllers: "1", paths: "0", expanders: "0", enclosures: "1" },
  blank_expander_handles: { controllers: "1", paths: "0", expanders: "0", enclosures: "1" },
  blank_sep_unknown_enclosure: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
  show_all_only: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
  show_all_second_unit: { controllers: "2", paths: "0", expanders: "2", enclosures: "2" },
  discovery_first: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
  discovery_interior: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
  discovery_last: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
  discovery_every: { controllers: "1", paths: "0", expanders: "1", enclosures: "1" },
};

test("complete replies as mprutil prints them stay OK with their counts", () => {
  for (const [name, summary] of Object.entries(HEALTHY_SUMMARIES)) {
    const page = loadCase(name);
    assert.equal(page.chip, "STORAGE OK", name);
    assert.equal(page.warnings, "", name);
    assert.doesNotMatch(page.status, /partial evidence/, name);
    assert.deepEqual(page.summary, summary, name);
    assert.doesNotMatch(textOf(page.map), /Not read|not read/, name);
  }
});

test("control: an unparseable or empty reply still says not read and is partial", () => {
  for (const name of ["show_all_unparseable", "show_all_empty"]) {
    const page = loadCase(name);
    assert.equal(page.chip, "STORAGE PARTIAL", name);
    assert.deepEqual(page.summary, { controllers: "not read", paths: "not read", expanders: "not read", enclosures: "not read" }, name);
  }
  const lists = loadCase("unparsed_lists");
  assert.equal(lists.chip, "STORAGE PARTIAL");
  assert.equal(lists.summary.expanders, "not read");
});

test("disk view PHY fact of a controller whose table was not read whole says not read", () => {
  const disk = textOf(loadCase("phy_table_rejected_on_path", "disk").map);
  assert.match(disk, /PHY not read/);
  // Control: the same controller with a table read whole shows its counts.
  const fabric = structuredClone(CASES.phy_table_rejected_on_path.fabric);
  const controller = fabric.nodes.find((node) => node.id === "controller:mpr0");
  delete controller.metrics.not_read;
  Object.assign(controller.metrics, { phy_count: 2, linked_phy_count: 1 });
  const whole = textOf(loadPage(fabric, "disk", CASES.phy_table_rejected_on_path.slots).map);
  assert.match(whole, /PHY 1\/2/);
  assert.doesNotMatch(whole, /PHY not read/);
});

test("main page controller lane draws its list state, not the old default", () => {
  const sandbox = vm.createContext({
    state: {},
    escapeHtml: String,
    isStorageFabricPayload: () => false,
    sasFabricDisplayLabel: (item) => item?.label || null,
    sasFabricSortedSlots: (slots) => [...(slots || [])].sort((left, right) => left - right),
    sasFabricSelectionTouchesNode: () => false,
    sasFabricSelectionTouchesSlots: () => false,
    renderSasFabricNodeButton: (node) => `[${node.id}]`,
    sasFabricNodeMeta: () => "",
    renderSasFabricPathButton: () => "",
    renderSasFabricBayChips: () => "",
    sasFabricListExpanded: () => false,
    renderSasFabricExpandToggle: () => "",
    formatSasFabricKind: String,
  });
  const names = ["sasFabricList", "renderSasFabricCompactNodes", "sasFabricLaneListEmptyText", "renderSasFabricLane"];
  vm.runInContext(`${names.map(appFunctionSource).join("\n")}\nthis.lane = renderSasFabricLane;`, sandbox);
  const drawLane = (fabric) => {
    const nodeMap = new Map(fabric.nodes.map((node) => [node.id, node]));
    return textOf(sandbox.lane(fabric.controllers[0], fabric, nodeMap));
  };
  const unread = drawLane(CASES.unparsed_lists.fabric);
  assert.equal((unread.match(/Not read/g) || []).length, 2, unread);
  assert.doesNotMatch(unread, /No objects reported/);
  const whole = drawLane(CASES.complete.fabric);
  assert.equal((whole.match(/No objects reported/g) || []).length, 2, whole);
});

test("main page inspector and lanes follow the same not-read facts", () => {
  const sandbox = vm.createContext({ kvRow: (label, value) => `${label}: ${value};`, formatSasFabricValue: String });
  vm.runInContext(`${["sasFabricList", "sasFabricMetricRows"].map(appFunctionSource).join("\n")}\nthis.rows = sasFabricMetricRows;`, sandbox);
  assert.equal(sandbox.rows({ phy_count: null, not_read: ["phy_count"] }), "phy count: not read;");
  assert.equal(sandbox.rows({ phy_count: null }), "");
  vm.runInContext(`${appFunctionSource("sasFabricLaneListEmptyText")}\nthis.emptyText = sasFabricLaneListEmptyText;`, sandbox);
  const { emptyText } = sandbox;
  assert.equal(emptyText(CASES.unparsed_lists.fabric, "mpr0", "expanders"), "Not read");
  assert.equal(emptyText(CASES.complete.fabric, "mpr0", "expanders"), "No objects reported");
  assert.equal(emptyText({ raw: {} }, "mpr0", "expanders"), "No objects reported");
});
