"use strict";

// These bytes are rebuilt and compared with the actual CORE producer by
// tests.test_core_fabric_direct_paths. No multipath geom is present.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { loadTopology } = require("./helpers/fabric_topology");

const ROOT = path.resolve(__dirname, "../..");
const payload = JSON.parse(fs.readFileSync(path.join(ROOT, "tests/fixtures/sas_fabric/core_direct_paths.json"), "utf8"));
const source = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function page(mode) {
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots: payload.slots }, fabric: payload.fabric } },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", dataset: {}, className: "", closest: () => null });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  const end = source.indexOf("  elements.modeButtons.forEach((button) => {", source.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${source.slice(0, end)}
    window.directPathTest = { renderMap, state, branchSeedsForDiskTrace, branchMprDevice };
  })();`, sandbox);
  const api = sandbox.window.directPathTest;
  api.state.mode = mode;
  api.state.selectedTraceId = "bay:0";
  api.state.selectedDiskTraceId = "bay:0";
  api.renderMap();
  return { api, html: elements.get("fabric-map-panel").innerHTML };
}

for (const mode of ["disk", "impact", "lanes"]) {
  test(`direct-device ports remain visible in ${mode} mode`, () => {
    const { html } = page(mode);
    assert.match(html, /mpr0/);
    assert.match(html, /mpr1/);
    assert.match(html, /standby/i);
    assert.match(html, /active/i);
    assert.match(html, /data-fabric-trace="bay:0"/);
  });
}

test("Disk Path joins both branch seeds to their own controller context", () => {
  assert.equal(payload.slots[0].multipath, null);
  const { api } = page("disk");
  const trace = payload.fabric.traces.find((item) => item.id === "bay:0");
  const seeds = api.branchSeedsForDiskTrace(trace, payload.fabric);
  assert.equal(seeds.length, 2);
  for (const seed of seeds) {
    const device = api.branchMprDevice(seed, trace);
    assert.equal(device.controller, seed.controller);
    assert.equal(device.member_device_name, seed.device_name);
    assert.equal(device.speed, "12.0");
    assert.ok(device.expander_ids.length);
  }
});

test("Topology retains the direct disk's two observed controller paths", () => {
  const topology = loadTopology();
  const model = topology.buildTopologyModel(payload.fabric, { snapshot: { slots: payload.slots } });
  const bay = model.bayById.get("bay:0");
  assert.deepEqual(Array.from(bay.pathStates, (state) => [state.controller, state.state]), [
    ["mpr0", "standby"], ["mpr1", "active"],
  ]);
  const trace = topology.tracePath(model, { kind: "bay", id: "bay:0" });
  for (const name of ["mpr0", "mpr1"]) {
    assert.ok(trace.nodes.has(`controller:${name}`), name);
  }
});
