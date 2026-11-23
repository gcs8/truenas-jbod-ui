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

function page(mode, data = payload) {
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots: data.slots }, fabric: data.fabric } },
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
    window.directPathTest = { renderMap, state, branchSeedsForDiskTrace, branchMprDevice, renderDiskPathBranch, diskPathBranchEvidence };
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

test("an explicitly unread controller never borrows a bay HBA, path or fault", () => {
  const data = structuredClone(payload);
  const trace = data.fabric.traces.find((item) => item.id === "bay:0");
  const member = trace.metrics.path_states.find((row) => row.device_name === "da11");
  member.controller = null;
  member.not_read = ["controller", "target"];
  trace.metrics.mpr_devices = trace.metrics.mpr_devices.filter((row) => row.member_device_name !== "da11");
  const { api } = page("disk", data);
  const seed = api.branchSeedsForDiskTrace(trace, data.fabric).find((row) => row.device_name === "da11");
  const rendered = api.renderDiskPathBranch(seed, trace, data.fabric, 0);
  const hba = rendered.split('disk-path-kind">HBA')[1].split('disk-path-card-cell')[0];
  assert.match(hba, /not read/);
  assert.doesNotMatch(hba, /mpr0|controller:mpr|firmware|16\.00/);
  assert.doesNotMatch(rendered.match(/<small>([^<]*)<\/small>/)[1], /mpr0|controller active/);
  assert.doesNotMatch(api.diskPathBranchEvidence(seed, trace, data.fabric), /mpr0|controller active da11/);
});

test("direct ports with zero MPR rows never refill an expander from enclosure scope", () => {
  const data = structuredClone(payload);
  const trace = data.fabric.traces.find((item) => item.id === "bay:0");
  trace.metrics.mpr_devices = [];
  const { api } = page("disk", data);
  for (const seed of api.branchSeedsForDiskTrace(trace, data.fabric)) {
    const rendered = api.renderDiskPathBranch(seed, trace, data.fabric, 0);
    assert.match(rendered, /Speed<\/em>not read/);
    for (const node of data.fabric.nodes.filter((row) => row.kind === "expander")) {
      assert.ok(!rendered.includes(node.id), node.id);
    }
  }
});

test("a direct port with no matched expander never refills from the bay", () => {
  const data = structuredClone(payload);
  const trace = data.fabric.traces.find((item) => item.id === "bay:0");
  trace.metrics.mpr_devices.forEach((row) => { row.expander_ids = []; });
  const { api } = page("disk", data);
  for (const seed of api.branchSeedsForDiskTrace(trace, data.fabric)) {
    const rendered = api.renderDiskPathBranch(seed, trace, data.fabric, 0);
    for (const node of data.fabric.nodes.filter((row) => row.kind === "expander")) {
      assert.ok(!rendered.includes(node.id), node.id);
    }
  }
});

for (const mode of ["disk", "impact"]) {
  test(`${mode} never invents a backplane zone`, () => {
    assert.doesNotMatch(page(mode).html, /Backplane Zone/);
  });
}

test("a direct seed without a graph path uses only its own header fields", () => {
  const { data, trace } = fourPathPayload();
  const { api } = page("disk", data);
  const seed = api.branchSeedsForDiskTrace(trace, data.fabric).find((row) => row.device_name === "da12");
  const rendered = api.renderDiskPathBranch(seed, trace, data.fabric, 0);
  assert.match(rendered.match(/<small>([^<]*)<\/small>/)[1], /^mpr1 standby \/ da12$/);
  const hba = rendered.split('disk-path-kind">HBA')[1].split('disk-path-card-cell')[0];
  assert.doesNotMatch(hba, /16\.00|data-fabric-node|data-fabric-trace/);
  assert.equal(api.branchMprDevice(seed, trace), null);
});

test("a direct seed without a controller says controller not read in its header", () => {
  const { data, trace } = fourPathPayload();
  const own = trace.metrics.path_states.find((row) => row.device_name === "da12");
  own.controller = null;
  own.not_read = ["controller", "target"];
  const { api } = page("disk", data);
  const seed = api.branchSeedsForDiskTrace(trace, data.fabric).find((row) => row.device_name === "da12");
  const rendered = api.renderDiskPathBranch(seed, trace, data.fabric, 0);
  assert.match(rendered.match(/<small>([^<]*)<\/small>/)[1], /^controller not read standby \/ da12$/);
  assert.doesNotMatch(rendered.match(/<small>([^<]*)<\/small>/)[1], /mpr[01]/);
});

// Four direct paths, two per controller, with MPR rows for only one path on
// each controller: the producer rows asserted by
// test_four_direct_paths_with_partial_mpr_rows_keep_their_own_rows.
function fourPathPayload() {
  const data = JSON.parse(JSON.stringify(payload));
  const trace = data.fabric.traces.find((item) => item.id === "bay:0");
  trace.metrics.path_states = [
    { controller: "mpr0", state: "standby", device_name: "da3" },
    { controller: "mpr0", state: "standby", device_name: "da4" },
    { controller: "mpr1", state: "active", device_name: "da11" },
    { controller: "mpr1", state: "standby", device_name: "da12" },
  ];
  assert.deepEqual(trace.metrics.mpr_devices.map((row) => row.member_device_name), ["da3", "da11"]);
  return { data, trace };
}

test("Disk Path never draws a sibling port's device or SAS address on a path without its own MPR row", () => {
  const { data, trace } = fourPathPayload();
  const addresses = Object.fromEntries(trace.metrics.mpr_devices.map((row) => [row.member_device_name, row.sas_address]));
  const { api, html } = page("disk", data);
  const seeds = api.branchSeedsForDiskTrace(trace, data.fabric);
  assert.deepEqual(Array.from(seeds, (seed) => seed.device_name), ["da3", "da4", "da11", "da12"]);
  const branches = Object.fromEntries(seeds.map((seed) => [seed.device_name, {
    device: api.branchMprDevice(seed, trace),
    html: api.renderDiskPathBranch(seed, trace, data.fabric, 0),
  }]));
  for (const [own, sibling] of [["da3", "da4"], ["da11", "da12"]]) {
    // Control: the member with its own row keeps its port detail.
    assert.equal(branches[own].device.member_device_name, own);
    assert.match(branches[own].html, new RegExp(addresses[own]));
    // The sibling keeps its own name; its port detail is not read.
    const rendered = branches[sibling].html;
    const header = rendered.match(/<small>([^<]*)<\/small>/)[1];
    assert.match(header, new RegExp(`/ ${sibling}$`));
    assert.doesNotMatch(header, new RegExp(`\\b${own}\\b`));
    assert.equal(branches[sibling].device, null);
    assert.match(branches[own].html.match(/<small>([^<]*)<\/small>/)[1], new RegExp(`/ ${own}$`));
    for (const address of Object.values(addresses)) {
      assert.doesNotMatch(rendered, new RegExp(address));
    }
    // Nor the sibling port's expander or MPR enclosure: not read for this path.
    for (const row of trace.metrics.mpr_devices) {
      // The physical enclosure is bay-level evidence shared by all paths.
      // Only a controller-local enclosure must be withheld from an unread port.
      if (!row.enclosure_id.startsWith("enclosure:")) assert.ok(!rendered.includes(row.enclosure_id), row.enclosure_id);
      for (const expanderId of row.expander_ids) {
        assert.match(branches[row.member_device_name].html, new RegExp(expanderId.split(":").pop()));
        assert.doesNotMatch(rendered, new RegExp(expanderId.split(":").pop()));
      }
    }
    assert.match(rendered, /not read/);
  }
  // The full Disk Path view renders all four branches.
  for (const name of ["da3", "da4", "da11", "da12"]) {
    assert.match(html, new RegExp(`\\b${name}\\b`));
  }
});

// Codex review of #1504: a partial reply with only the second path's row on a
// controller. Each Topology port takes only the row that names its own member;
// a path without one stays not read, never its sibling's device or address.
test("Topology ports take only their own member's MPR row; a path without one stays not read", () => {
  for (const order of ["as given", "rows reversed"]) {
    const { data, trace } = fourPathPayload();
    for (const row of trace.metrics.mpr_devices) row.member_device_name = { da3: "da4", da11: "da12" }[row.member_device_name];
    if (order === "rows reversed") trace.metrics.mpr_devices.reverse();
    const rows = Object.fromEntries(trace.metrics.mpr_devices.map((row) => [row.member_device_name, row]));
    const T = loadTopology();
    const bay = T.buildTopologyModel(data.fabric).bays.find((item) => item.bayId === "bay:0");
    const ports = Object.fromEntries(bay.ports.map((port) => [port.device, port]));
    assert.deepEqual(Object.keys(ports).sort(), ["da11", "da12", "da3", "da4"], order);
    for (const [own, sibling] of [["da4", "da3"], ["da12", "da11"]]) {
      // Control: the member with its own row keeps its port detail.
      assert.equal(ports[own].sasAddress, rows[own].sas_address.toLowerCase().replace(/^0x/, ""), `${order}: ${own}`);
      assert.ok(ports[own].expanderId, `${order}: ${own} keeps its expander`);
      // The sibling keeps its own name and path state; its port detail is not read.
      assert.equal(ports[sibling].sasAddress, "", `${order}: ${sibling} borrows no address`);
      assert.equal(ports[sibling].expanderId, "", `${order}: ${sibling} borrows no expander`);
      assert.equal(ports[sibling].phy, null, `${order}: ${sibling} borrows no PHY`);
      assert.equal(ports[sibling].handle, "", `${order}: ${sibling} borrows no handle`);
    }
  }
});
