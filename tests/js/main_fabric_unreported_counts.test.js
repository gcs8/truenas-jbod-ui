"use strict";

// The main page's Storage Fabric summary reads counts with the same rule as
// the dedicated Storage Fabric page: a CORE count whose completeness the
// payload does not report never reads as a plain number, a count reported
// complete stays a plain number (including 0), and payloads outside the CORE
// controller-read contract keep their counts. The fixture holds CORE builder
// output; only the completeness fields and the platform marker are changed.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const CASES = JSON.parse(
  fs.readFileSync(path.join(ROOT, "tests/fixtures/sas_fabric/core_no_controller_fabric.json"), "utf8"),
);
const DOMAINS = ["controllers", "paths", "expanders", "enclosures"];

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

// The main page's fabric count helpers: the source between the kind
// formatter and the payload classifier, run on their own.
function loadMainHelpers() {
  const start = APP_SOURCE.indexOf("  function formatSasFabricKind(");
  const end = APP_SOURCE.indexOf("  function isStorageFabricPayload(");
  assert.ok(start > 0 && end > start, "main page fabric count helpers must exist");
  const sandbox = vm.createContext({});
  vm.runInContext(`function sasFabricList(value) { return Array.isArray(value) ? value : []; }
${APP_SOURCE.slice(start, end)}
this.helpers = { sasFabricCountPhrase, sasFabricCountSummary, sasFabricNoControllerText };`, sandbox);
  return sandbox.helpers;
}

// The Storage Fabric page's reader, from the page script itself.
function loadFabricReader() {
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: {} },
    document: {
      getElementById: () => ({ innerHTML: "", textContent: "", dataset: {}, className: "", closest: () => null }),
      querySelectorAll: () => [],
    },
  });
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    window.readerTest = { fabricCountPhrase };
  })();`, sandbox);
  return sandbox.window.readerTest;
}

// "absent" drops raw.controller_read; "counts-absent" drops only its counts;
// "domain-absent" and "domain-null" clear every domain's entry.
function fabricWith(caseName, completeness, { kindOnly = false } = {}) {
  const fabric = clone(CASES[caseName].fabric);
  const read = fabric.raw.controller_read;
  if (completeness === "absent") delete fabric.raw.controller_read;
  else if (completeness === "counts-absent") delete read.counts;
  else if (completeness === "domain-absent") DOMAINS.forEach((domain) => delete read.counts[domain]);
  else if (completeness === "domain-null") DOMAINS.forEach((domain) => { read.counts[domain] = null; });
  if (kindOnly) {
    delete fabric.platform;
    fabric.raw.fabric_kind = "core_mpr";
  }
  return fabric;
}

const UNREPORTED = ["absent", "counts-absent", "domain-absent", "domain-null"];

for (const completeness of UNREPORTED) {
  test(`main summary, unreported completeness (${completeness}): counts are not plain numbers`, () => {
    const { sasFabricCountSummary } = loadMainHelpers();
    assert.equal(
      sasFabricCountSummary(fabricWith("complete", completeness)),
      "1 source, completeness not reported / paths not reported / expanders not reported / enclosures not reported / 1 trace",
    );
    assert.equal(
      sasFabricCountSummary(fabricWith("healthy_lists", completeness)),
      "1 source, completeness not reported / paths not reported / 1 expander, completeness not reported / 1 enclosure, completeness not reported / 1 trace",
    );
  });
}

test("main summary: a CORE payload identified only by its fabric kind follows the same rule", () => {
  const { sasFabricCountSummary } = loadMainHelpers();
  const fabric = fabricWith("complete", "absent", { kindOnly: true });
  assert.equal(fabric.platform, undefined);
  assert.equal(
    sasFabricCountSummary(fabric),
    "1 source, completeness not reported / paths not reported / expanders not reported / enclosures not reported / 1 trace",
  );
});

test("main summary: reported counts keep their text (complete zero stays 0, partial and not read unchanged)", () => {
  const { sasFabricCountSummary } = loadMainHelpers();
  assert.equal(sasFabricCountSummary(CASES.complete.fabric), "1 source / 0 paths / 0 expanders / 0 enclosures / 1 trace");
  assert.equal(sasFabricCountSummary(CASES.healthy_lists.fabric), "1 source / 0 paths / 1 expander / 1 enclosure / 1 trace");
  assert.equal(sasFabricCountSummary(CASES.no_controller_bays.fabric), "sources not read / paths not read / expanders not read / enclosures not read / 1 trace");
  assert.equal(sasFabricCountSummary(CASES.unreadable_second_unit.fabric), "at least 1 source / 0 paths / expanders not read / enclosures not read / 1 trace");
});

test("main summary: payloads outside the CORE contract keep their plain counts", () => {
  const { sasFabricCountSummary } = loadMainHelpers();
  for (const [platform, kind] of [["scale", "scale_ses"], ["linux", "linux_ses"], ["quantastor", "storage_quantastor"], [undefined, "linux_ses"]]) {
    const fabric = fabricWith("healthy_lists", "absent");
    if (platform === undefined) delete fabric.platform;
    else fabric.platform = platform;
    fabric.raw.fabric_kind = kind;
    assert.equal(sasFabricCountSummary(fabric), "1 source / 0 paths / 1 expander / 1 enclosure / 1 trace", `${platform} ${kind}`);
  }
});

test("main host card: a CORE read without controllers and without completeness does not claim none were reported", () => {
  const { sasFabricNoControllerText } = loadMainHelpers();
  const fabric = fabricWith("no_controller_bays", "absent");
  assert.equal(sasFabricNoControllerText(fabric, false).meta, "HBA controllers not reported");
  assert.doesNotMatch(sasFabricNoControllerText(fabric, false).message, /No controllers were reported/);
  const kindOnly = fabricWith("no_controller_bays", "absent", { kindOnly: true });
  assert.equal(sasFabricNoControllerText(kindOnly, false).meta, "HBA controllers not reported");
  // Reported states keep their text.
  assert.equal(sasFabricNoControllerText(CASES.no_controller_bays.fabric, false).meta, "HBA controllers not read");
  const reportedZero = fabricWith("no_controller_bays", {});
  reportedZero.raw.controller_read.counts.controllers = "complete";
  assert.equal(sasFabricNoControllerText(reportedZero, false).meta, "No HBA controllers reported");
  assert.equal(sasFabricNoControllerText(fabric, true).meta, "No storage sources reported");
});

test("main page and Storage Fabric page give the same phrase for every count state", () => {
  const { sasFabricCountPhrase } = loadMainHelpers();
  const { fabricCountPhrase } = loadFabricReader();
  const payloads = [
    ["complete", CASES.complete.fabric],
    ["not read", CASES.no_controller_bays.fabric],
    ["partial", CASES.unreadable_second_unit.fabric],
    ...UNREPORTED.map((completeness) => [completeness, fabricWith("complete", completeness)]),
    ["kind only", fabricWith("complete", "absent", { kindOnly: true })],
    ["linux", { ...fabricWith("complete", "absent"), platform: "linux" }],
    ["no platform, no kind", (() => { const fabric = fabricWith("complete", "absent"); delete fabric.platform; return fabric; })()],
  ];
  for (const [name, fabric] of payloads) {
    for (const domain of DOMAINS) {
      for (const count of [0, 1, 3]) {
        assert.equal(
          sasFabricCountPhrase(fabric, domain, count, "source"),
          fabricCountPhrase(fabric, domain, count, "source"),
          `${name} ${domain} ${count}`,
        );
      }
    }
  }
});

// Source of one top-level function, from its declaration to the matching
// closing brace (the bodies compared here hold no braces inside strings).
function functionSource(source, name) {
  const start = source.indexOf(`  function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const end = source.indexOf("\n  }\n", start);
  assert.ok(end > start, `function ${name} must have a complete body`);
  return source.slice(start, end + 4);
}

// The main page's own summary render, renderSasFabric, with the fabric
// panel open: the count helpers it uses are the page's own, and the summary
// element is the only element it is given, so the visible text is what the
// caller writes, not what a helper returns.
function renderMainSummary(fabric) {
  const start = APP_SOURCE.indexOf("  function formatSasFabricKind(");
  const end = APP_SOURCE.indexOf("  function isStorageFabricPayload(");
  assert.ok(start > 0 && end > start, "main page fabric count helpers must exist");
  const summary = { textContent: "Shows how disks connect to controllers." };
  const sandbox = vm.createContext({
    state: { snapshotMode: false, sasFabric: { open: true, loading: false, data: fabric, selectedNodeId: null, selectedTraceId: null } },
    sasFabricPanel: { classList: { toggle() {} } },
    sasFabricSummary: summary,
    sasFabricToggleButton: null,
    sasFabricRefreshButton: null,
    sasFabricShowAllButton: null,
    sasFabricStatus: null,
    sasFabricLanes: null,
    sasFabricTraceById: () => ({}),
    resolveSasFabricTraceId: () => null,
    renderSasFabricInspector: () => {},
  });
  vm.runInContext(`${functionSource(APP_SOURCE, "sasFabricList")}
${APP_SOURCE.slice(start, end)}
${functionSource(APP_SOURCE, "renderSasFabric")}
renderSasFabric();`, sandbox);
  return summary.textContent;
}

test("main page render: the visible summary of a CORE payload without reported completeness says not reported", () => {
  const unreported = "1 source, completeness not reported / paths not reported / expanders not reported / enclosures not reported / 1 trace";
  assert.equal(renderMainSummary(fabricWith("complete", "absent")), unreported);
  assert.equal(renderMainSummary(fabricWith("complete", "absent", { kindOnly: true })), unreported);
  assert.equal(
    renderMainSummary(fabricWith("healthy_lists", "counts-absent")),
    "1 source, completeness not reported / paths not reported / 1 expander, completeness not reported / 1 enclosure, completeness not reported / 1 trace",
  );
  // Control: counts reported complete stay plain numbers, zero included.
  assert.equal(renderMainSummary(CASES.complete.fabric), "1 source / 0 paths / 0 expanders / 0 enclosures / 1 trace");
});

test("the main page reads counts through a textual twin of the Storage Fabric page reader", () => {
  assert.equal(functionSource(APP_SOURCE, "fabricCountState"), functionSource(FABRIC_SOURCE, "fabricCountState"));
  assert.equal(
    functionSource(APP_SOURCE, "sasFabricCountPhrase").replace("function sasFabricCountPhrase(", "function fabricCountPhrase("),
    functionSource(FABRIC_SOURCE, "fabricCountPhrase"),
  );
});

// A single trace or bay takes the singular noun; other counts keep the
// plural, and the not-read and partial wording of the other domains stays.
function loadMainWording() {
  const sandbox = vm.createContext({});
  vm.runInContext(`${["sasFabricList", "fabricCountState", "sasFabricCountPhrase", "sasFabricCountSummary", "sasFabricNodeMeta"].map((name) => functionSource(APP_SOURCE, name)).join("\n")}
this.helpers = { sasFabricCountSummary, sasFabricNodeMeta };`, sandbox);
  return sandbox.helpers;
}

test("main summary: one trace reads 1 trace; zero and two keep the plural", () => {
  const { sasFabricCountSummary } = loadMainWording();
  const withTraces = (count) => ({ ...clone(CASES.complete.fabric), traces: Array.from({ length: count }, (_, index) => ({ id: `bay:${index}` })) });
  assert.equal(sasFabricCountSummary(withTraces(1)), "1 source / 0 paths / 0 expanders / 0 enclosures / 1 trace");
  assert.equal(sasFabricCountSummary(withTraces(2)), "1 source / 0 paths / 0 expanders / 0 enclosures / 2 traces");
  assert.equal(sasFabricCountSummary(withTraces(0)), "1 source / 0 paths / 0 expanders / 0 enclosures / 0 traces");
  assert.equal(
    sasFabricCountSummary({ controllers: [], paths: [], expanders: [], enclosures: [], traces: [{ id: "bay:0" }] }),
    "0 sources / 0 paths / 0 expanders / 0 enclosures / 1 trace",
  );
});

test("main node meta: one bay reads 1 bay; two keep the plural; no count adds nothing", () => {
  const { sasFabricNodeMeta } = loadMainWording();
  assert.equal(sasFabricNodeMeta({}, { count: 1 }), "1 bay");
  assert.equal(sasFabricNodeMeta({}, { count: 2 }), "2 bays");
  assert.equal(sasFabricNodeMeta({}, { count: 24, pcie_slot: "Slot 3" }), "Slot 3 / 24 bays");
  assert.equal(sasFabricNodeMeta({}, {}), "");
});
