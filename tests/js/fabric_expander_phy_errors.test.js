"use strict";

// CORE expander PHY error counters (#941): a counter the app could not read
// must render as unknown or not available, never as zero.

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
  const bodyStart = FABRIC_SOURCE.indexOf("{", FABRIC_SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < FABRIC_SOURCE.length; index += 1) {
    const character = FABRIC_SOURCE[index];
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
      if (depth === 0) return FABRIC_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const context = vm.createContext({});
vm.runInContext(`${functionSource("expanderPhyErrorText")}; this.expanderPhyErrorText = expanderPhyErrorText;`, context);
const { expanderPhyErrorText } = context;

test("measured expander PHY counters are listed by name", () => {
  assert.equal(
    expanderPhyErrorText({
      counter_state: "measured",
      invalid_dword_count: 9,
      running_disparity_error_count: 0,
      loss_of_dword_synchronization_count: 2,
      phy_reset_problem_count: 4294967295,
    }),
    "invalid dword 9, disparity 0, loss of sync 2, reset problem saturated",
  );
});

test("refused or unreadable counters never render as zero", () => {
  assert.equal(
    expanderPhyErrorText({ counter_state: "permission_denied", invalid_dword_count: null }),
    "Not available (permission not granted)",
  );
  for (const counterState of ["unknown", "no_route", "unavailable", "not_read"]) {
    const text = expanderPhyErrorText({ counter_state: counterState, invalid_dword_count: null });
    assert.equal(text, "Unknown");
    assert.doesNotMatch(text, /\b0\b/);
  }
});

test("rows without counter data add no row", () => {
  assert.equal(expanderPhyErrorText(null), null);
  assert.equal(expanderPhyErrorText({ phy: "3", device: "End Device" }), null);
});

// Rendered cards: load the production renderers (not the DOM wiring or
// startup I/O) and render a CORE disk branch and the expander inspector.
const EXPANDER_ID = "expander:mpr0:5000c50000000001";

function coreFabric({ phys, metrics }) {
  const expander = {
    id: EXPANDER_ID, kind: "expander", label: "5000c50000000001", status: "online",
    controller_id: "controller:mpr0", related_slots: [0, 1], raw_id: "5000c50000000001",
    metrics: { linked_phys: 2, num_phys: 4, ...metrics },
    raw: { sas_address: "5000c50000000001", dev_handle: "0009", phys },
  };
  const nodes = [
    { id: "host", kind: "host", label: "core.example.test", status: "online", metrics: {}, related_slots: [0, 1], raw: {} },
    { id: "controller:mpr0", kind: "controller", label: "mpr0", status: "online", metrics: {}, related_slots: [0, 1], raw: {} },
    expander,
  ];
  const traces = [0, 1].map((slot) => ({
    id: `bay:${slot}`, kind: "bay", label: `Bay 0${slot}`, slots: [slot],
    node_ids: ["host", "controller:mpr0", EXPANDER_ID], link_ids: [],
    metrics: { mpr_devices: [{ controller: "mpr0", state: "active", handle: slot ? "000b" : "000a", expander_ids: [EXPANDER_ID] }] },
  }));
  for (const trace of traces) nodes.push({ id: trace.id, kind: "bay", label: trace.label, status: "online", related_slots: trace.slots, metrics: {}, raw: {} });
  return { available: true, platform: "core", nodes, traces, links: [], controllers: [{ id: "controller:mpr0", name: "mpr0" }], aliases: {} };
}

function loadRenderers(fabric) {
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots: [] }, fabric } },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "" });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    window.renderTest = { renderDiskPathBranch, renderInspector, state };
  })();`, sandbox);
  const api = sandbox.window.renderTest;
  const expanderNode = fabric.nodes.find((node) => node.id === EXPANDER_ID);
  return {
    branch(slot) {
      const trace = fabric.traces[slot];
      const seed = { controller: "mpr0", state: "active", mprDevice: trace.metrics.mpr_devices[0], expanderNode };
      return api.renderDiskPathBranch(seed, trace, fabric, slot);
    },
    inspector() {
      api.state.selectedTraceId = null;
      api.state.selectedNodeId = EXPANDER_ID;
      api.renderInspector(fabric);
      return elements.get("fabric-inspector-body").innerHTML;
    },
  };
}

function facts(markup) {
  // Every rendered fact as "label: value", visible or under "More details".
  return [...markup.matchAll(/<span><em>([^<]*)<\/em>([^<]*)<\/span>/g)].map(([, label, value]) => `${label}: ${value}`);
}

function inspectorRows(markup) {
  return [...markup.matchAll(/<span>([^<]*)<\/span>\s*<strong>([^<]*)<\/strong>/g)].map(([, label, value]) => `${label}: ${value}`);
}

const MEASURED = (phy, handle, counts) => ({
  phy: String(phy), dev_handle: handle, device: "End Device", counter_state: "measured", counter_reason: null,
  invalid_dword_count: counts[0], running_disparity_error_count: counts[1],
  loss_of_dword_synchronization_count: counts[2], phy_reset_problem_count: counts[3],
});
const UNKNOWN = (phy, handle, reason) => ({
  phy: String(phy), dev_handle: handle, device: "End Device", counter_state: "unknown", counter_reason: reason,
  invalid_dword_count: null, running_disparity_error_count: null,
  loss_of_dword_synchronization_count: null, phy_reset_problem_count: null,
});

test("rendered cards show partial counters: measured PHYs by name, unread PHYs unknown", () => {
  const ui = loadRenderers(coreFabric({
    phys: [MEASURED(0, "000a", [9, 0, 2, 0]), UNKNOWN(1, "000b", "not_read")],
    metrics: { phy_error_counters: "1 PHYs read, 1 with errors, 3 unknown" },
  }));
  const measured = facts(ui.branch(0));
  assert.ok(measured.includes("PHY errors: invalid dword 9, disparity 0, loss of sync 2, reset problem 0"), measured.join("\n"));
  assert.ok(measured.includes("PHY errors: 1 PHYs read, 1 with errors, 3 unknown"), "expander card summary");
  const unread = facts(ui.branch(1));
  assert.ok(unread.includes("PHY errors: Unknown"), unread.join("\n"));
  assert.ok(inspectorRows(ui.inspector()).includes("PHY error counters: 1 PHYs read, 1 with errors, 3 unknown"));
});

test("rendered cards keep a measured zero apart from an unknown counter", () => {
  const ui = loadRenderers(coreFabric({
    phys: [MEASURED(0, "000a", [0, 0, 0, 0]), UNKNOWN(1, "000b", "no_response")],
    metrics: { phy_error_counters: "1 PHYs read, 0 with errors, 3 unknown" },
  }));
  assert.ok(facts(ui.branch(0)).includes("PHY errors: invalid dword 0, disparity 0, loss of sync 0, reset problem 0"));
  const unknown = facts(ui.branch(1)).filter((fact) => fact.startsWith("PHY errors: ") && !fact.includes("PHYs read"));
  assert.deepEqual(unknown, ["PHY errors: Unknown"]);
});

test("rendered cards say permission was not granted instead of showing zeros", () => {
  const message = "Expander counters not available (permission not granted).";
  const denied = { ...UNKNOWN(0, "000a", null), counter_state: "permission_denied" };
  const ui = loadRenderers(coreFabric({ phys: [denied], metrics: { phy_error_counters: message } }));
  const shown = facts(ui.branch(0)).filter((fact) => fact.startsWith("PHY errors: "));
  assert.deepEqual(shown, ["PHY errors: Not available (permission not granted)", `PHY errors: ${message}`]);
  assert.ok(inspectorRows(ui.inspector()).includes(`PHY error counters: ${message}`));
});

test("an older snapshot without counter fields renders no counter rows", () => {
  const ui = loadRenderers(coreFabric({
    phys: [{ phy: "0", dev_handle: "000a", device: "End Device" }, { phy: "1", dev_handle: "000b", device: "End Device" }],
    metrics: {},
  }));
  for (const slot of [0, 1]) {
    const markup = ui.branch(slot);
    assert.match(markup, /Expander device/, "the branch still renders the expander PHY");
    assert.equal(facts(markup).filter((fact) => fact.startsWith("PHY errors")).length, 0);
    assert.doesNotMatch(markup, /Unknown|undefined|null|NaN/);
  }
  assert.doesNotMatch(ui.inspector(), /PHY error counters/);
});
