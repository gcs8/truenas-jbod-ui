"use strict";

// Storage Fabric summary counts and read completeness. A CORE count whose
// completeness the payload does not report is not a measurement: it never
// reads as a plain number (never "0 expanders" for lists nobody vouched
// for). A count reported complete stays a plain number, including 0, and a
// count reported partial or not read keeps its existing text. The fixture
// holds CORE builder output; only the completeness fields are changed here.

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
const COUNTED = ["expanders", "enclosures"];

function textOf(html) {
  return html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function loadPage(fabric, mode, slots) {
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
    window.renderTest = { renderMap, renderSummary, state, fabricCountText, fabricCountPhrase };
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
    api,
    map: textOf(read("fabric-map-panel").innerHTML),
    summary: Object.fromEntries(Object.entries(SUMMARY_IDS).map(([key, id]) => [key, read(id).textContent])),
  };
}

// The fixture case with its completeness fields rewritten:
// "absent" drops raw.controller_read; "counts-absent" drops only its counts;
// "domain-absent" drops the counted domains' entries; an object sets values.
function fabricWith(caseName, completeness) {
  const fabric = clone(CASES[caseName].fabric);
  const read = fabric.raw.controller_read;
  if (completeness === "absent") delete fabric.raw.controller_read;
  else if (completeness === "counts-absent") delete read.counts;
  else if (completeness === "domain-absent") COUNTED.forEach((domain) => delete read.counts[domain]);
  else if (completeness === "domain-null") COUNTED.forEach((domain) => { read.counts[domain] = null; });
  else Object.assign(read.counts, completeness);
  return fabric;
}

function render(caseName, completeness, mode = "lanes") {
  return loadPage(fabricWith(caseName, completeness), mode, CASES[caseName].slots);
}

test("fixture cases hold the counts these tests rely on", () => {
  for (const [name, expected] of [["complete", 0], ["healthy_lists", 1]]) {
    const fabric = CASES[name].fabric;
    assert.equal(fabric.platform, "core");
    assert.equal(fabric.controllers.length, 1, name);
    assert.equal(fabric.paths.length, 0, name);
    for (const domain of COUNTED) assert.equal(fabric[domain].length, expected, `${name} ${domain}`);
    assert.deepEqual(fabric.raw.controller_read.counts, {
      controllers: "complete", paths: "complete", expanders: "complete", enclosures: "complete",
    });
  }
});

for (const completeness of ["absent", "counts-absent", "domain-absent", "domain-null"]) {
  test(`unreported completeness (${completeness}): a zero expander and enclosure count says not reported`, () => {
    for (const mode of PAGE_MODES) {
      const { summary } = render("complete", completeness, mode);
      for (const domain of COUNTED) {
        assert.equal(summary[domain], "not reported", `${mode} ${domain}`);
      }
    }
  });

  test(`unreported completeness (${completeness}): a non-zero count is never a plain number`, () => {
    for (const mode of PAGE_MODES) {
      const { summary } = render("healthy_lists", completeness, mode);
      for (const domain of COUNTED) {
        assert.doesNotMatch(summary[domain], /^\d+$/, `${mode} ${domain}`);
        assert.equal(summary[domain], "1, completeness not reported", `${mode} ${domain}`);
      }
    }
  });
}

test("sibling counts on the same reader: controllers and paths without completeness are not plain numbers", () => {
  const { summary, map } = render("complete", "absent", "lanes");
  assert.equal(summary.controllers, "1, completeness not reported");
  assert.equal(summary.paths, "not reported");
  // The host lane meta uses the phrase form of the same reader.
  assert.match(map, /1 controller, completeness not reported \/ paths not reported \//);
  assert.doesNotMatch(map, /\b0 paths\b/);
});

test("sibling counts: the Disk Path host card does not show a plain controller count without completeness", () => {
  const reported = render("complete", {}, "disk");
  assert.match(reported.map, /Controllers 1\b/);
  const unreported = render("complete", "absent", "disk");
  assert.match(unreported.map, /Controllers 1, completeness not reported/);
});

test("reported complete: zero stays 0 and one stays 1 in all four page modes", () => {
  for (const mode of PAGE_MODES) {
    assert.deepEqual({ ...render("complete", {}, mode).summary }, { controllers: "1", paths: "0", expanders: "0", enclosures: "0" }, mode);
    assert.deepEqual({ ...render("healthy_lists", {}, mode).summary }, { controllers: "1", paths: "0", expanders: "1", enclosures: "1" }, mode);
  }
});

test("reported partial keeps its existing text in all four page modes", () => {
  for (const mode of PAGE_MODES) {
    const zero = render("complete", { expanders: "partial", enclosures: "partial" }, mode).summary;
    const one = render("healthy_lists", { expanders: "partial", enclosures: "partial" }, mode).summary;
    for (const domain of COUNTED) {
      assert.equal(zero[domain], "not read", `${mode} ${domain}`);
      assert.equal(one[domain], "at least 1", `${mode} ${domain}`);
    }
  }
});

test("reported not read keeps its existing text in all four page modes", () => {
  for (const mode of PAGE_MODES) {
    for (const caseName of ["complete", "healthy_lists"]) {
      const { summary } = render(caseName, { expanders: "not_read", enclosures: "not_read" }, mode);
      for (const domain of COUNTED) assert.equal(summary[domain], "not read", `${caseName} ${mode} ${domain}`);
    }
  }
});

test("reported completeness never shows the not-reported wording in the four page modes", () => {
  for (const mode of PAGE_MODES) {
    for (const caseName of ["complete", "healthy_lists", "unparsed_lists", "no_controller_bays"]) {
      const page = loadPage(clone(CASES[caseName].fabric), mode, CASES[caseName].slots);
      assert.doesNotMatch(Object.values(page.summary).join(" | "), /not reported/, `${caseName} ${mode}`);
      assert.doesNotMatch(page.map, /completeness not reported/, `${caseName} ${mode}`);
    }
  }
});

test("lanes host meta keeps its reported text", () => {
  assert.match(render("complete", {}, "lanes").map, /1 controller \/ 0 paths \//);
  assert.match(render("complete", { paths: "partial" }, "lanes").map, /1 controller \/ paths not read \//);
});

test("payloads outside the CORE controller-read contract keep their counts as before", () => {
  const fabric = fabricWith("complete", "absent");
  fabric.platform = "linux";
  const { api } = render("complete", {}, "lanes");
  assert.equal(api.fabricCountText(fabric, "expanders", 0), "0");
  assert.equal(api.fabricCountText(fabric, "enclosures", 2), "2");
  assert.equal(api.fabricCountPhrase(fabric, "paths", 0, "path"), "0 paths");
});
