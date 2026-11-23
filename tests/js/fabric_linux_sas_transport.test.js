"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../..");
function topology() {
  const context = vm.createContext({ window: {} });
  vm.runInContext(fs.readFileSync(path.join(root, "app/static/sas_fabric_transport.js"), "utf8"), context);
  vm.runInContext(require("./helpers/fabric_topology").topologySource(), context);
  return context.window.SasFabricTopology;
}
function transport() {
  const context = vm.createContext({ window: {} });
  vm.runInContext(fs.readFileSync(path.join(root, "app/static/sas_fabric_transport.js"), "utf8"), context);
  return context.window.SasFabricTransport;
}
function syntheticFabric(available = true) {
  const node = (id, kind, metrics = {}) => ({ id, kind, label: id, metrics, raw: {}, related_slots: [] });
  const link = (source, target, kind, extra = {}) => ({ id: `${kind}:${source}->${target}`, source, target, kind, ...extra });
  return {
    available: true, platform: "scale", raw: { fabric_kind: "linux_ses", sas_transport_available: available },
    nodes: [node("host", "host"), node("controller:host2", "controller"),
      node("expander:front", "expander", { sas_level: 1 }), node("expander:rear", "expander", { sas_level: 2 }),
      node("ses:sg9", "ses-enclosure")],
    links: [link("host", "controller:host2", "host-controller"),
      link("controller:host2", "expander:front", "controller-expander"),
      link("expander:front", "expander:rear", "expander-expander", {
        reported_lanes: { basis: "sysfs_parent_port_membership", lane_count: 2,
          source_phys: [2, 3], target_phys: [], lanes: [], negotiated_rate_gbps: 12 },
      }), link("expander:rear", "ses:sg9", "expander-ses-enclosure")], traces: [],
  };
}

function renderLanes(fabric) {
  const source = fs.readFileSync(path.join(root, "app/static/sas_fabric_view.js"), "utf8");
  const extract = (name) => {
    const start = source.indexOf(`  function ${name}(`);
    const end = source.indexOf("\n  function ", start + 10);
    assert.ok(start >= 0 && end > start);
    return source.slice(start, end);
  };
  const context = vm.createContext({
    fabricViewCopy: () => ({ laneStages: { controller: "HBA", paths: "Paths", expanders: "Expanders", enclosures: "Enclosures", bays: "Bays" } }),
    nodeMap: (value) => new Map(value.nodes.map((node) => [node.id, node])), list: (value) => Array.isArray(value) ? value : [],
    displayLabel: (node) => node?.label || "", sortFabricNodesForLane: (value) => value,
    sortedSlots: (value) => value || [], selectionTouchesNode: () => false, selectionTouchesSlots: () => false,
    escapeHtml: String, renderNodeButton: () => "", nodeMeta: () => "", renderPathButton: () => "",
    renderNodeGrid: (nodes) => nodes.map((node) => node.id).join(" "), laneListEmptyText: () => "empty",
    aggregateDiskNoun: () => "bay", renderBayChips: (slots) => `bays:${slots.join(",")}`, fabricCountPhrase: () => "", hostDiskSummary: () => "",
    inventoryBaySlots: (value) => value.nodes.filter((node) => node.kind === "bay").map((node) => node.slot),
    renderLanesWithoutController: () => "", fabricCountsUnvouched: () => false,
  });
  vm.runInContext(extract("renderUnattachedInventory") + "\n" + extract("renderLanesMode") + "\nthis.render = renderLanesMode;", context);
  return context.render(fabric);
}

test("Linux transport records are drawn rather than classified as uncollected", () => {
  const model = topology().buildTopologyModel(syntheticFabric());
  // Transport uses the approved renderer's SAS evidence contract.
  assert.equal(model.contract, "core");
  assert.equal(model.nodes.filter((node) => node.kind === "expander").length, 2);
  assert.ok(model.tiers.some((tier) => tier.label === "Expanders, level 2"));
  assert.ok(!model.tiers.some((tier) => tier.state === "not_collected"));
  const cascade = model.links.find((link) => link.kind === "expander-expander");
  assert.equal(cascade.lanes.count, 2);
  assert.equal(cascade.lanes.rate, 12);
  assert.equal(model.links.length, 4);
  assert.ok(!model.notices.some((notice) => notice.startsWith("Not drawn:")));
});

test("no-SES SAS transport edges connect the reported bay", () => {
  const fabric = syntheticFabric();
  fabric.nodes = fabric.nodes.filter((node) => node.kind !== "ses-enclosure");
  fabric.nodes.push({ id: "bay:0", kind: "bay", label: "Bay 00", slot: 0, related_slots: [0], metrics: {}, raw: {} });
  fabric.links = fabric.links.filter((link) => link.kind !== "expander-ses-enclosure");
  fabric.links.push({ id: "transport-bay:rear", kind: "transport-bay", source: "expander:rear", target: "bay:0", slot: 0, related_slots: [0] });
  const model = topology().buildTopologyModel(fabric);
  assert.ok(model.links.some((link) => link.bundle && link.source === "expander:rear"));
  assert.ok(!model.notices.some((notice) => notice.startsWith("Not drawn:")));
});

test("shared observed SES membership appears in each matching controller lane", () => {
  const source = fs.readFileSync(path.join(root, "app/static/sas_fabric_view.js"), "utf8");
  const start = source.indexOf("  function renderLanesMode(");
  const end = source.indexOf("\n  function ", start + 10);
  const fabric = syntheticFabric();
  fabric.nodes.push({ id: "controller:host3", kind: "controller", label: "host3" });
  fabric.nodes.push({ id: "controller:host4", kind: "controller", label: "host4" });
  const ses = fabric.nodes.find((node) => node.kind === "ses-enclosure");
  ses.controller_id = null;
  ses.raw.controller_ids = ["controller:host2", "controller:host3"];
  const context = vm.createContext({
    fabricViewCopy: () => ({ laneStages: { controller: "HBA", paths: "Paths", expanders: "Expanders", enclosures: "Enclosures", bays: "Bays" } }),
    nodeMap: (value) => new Map(value.nodes.map((node) => [node.id, node])), list: (value) => Array.isArray(value) ? value : [],
    displayLabel: (node) => node?.label || "", sortFabricNodesForLane: (value) => value,
    sortedSlots: (value) => value || [], selectionTouchesNode: () => false, selectionTouchesSlots: () => false,
    escapeHtml: String, renderNodeButton: () => "", nodeMeta: () => "", renderPathButton: () => "",
    renderNodeGrid: (nodes) => nodes.map((node) => node.id).join(" "), laneListEmptyText: () => "empty",
    aggregateDiskNoun: () => "bay", renderBayChips: () => "", fabricCountPhrase: () => "", hostDiskSummary: () => "",
    renderUnattachedInventory: () => "", renderLanesWithoutController: () => "",
  });
  vm.runInContext(source.slice(start, end) + "\nthis.render = renderLanesMode;", context);
  const html = context.render(fabric);
  assert.equal((html.match(/ses:sg9/g) || []).length, 2);
});

test("unjoined SES enclosures remain in an unaffiliated Storage Lanes group", () => {
  const fabric = syntheticFabric();
  const ses = fabric.nodes.find((node) => node.kind === "ses-enclosure");
  ses.controller_id = null;
  ses.raw.controller_ids = [];
  fabric.nodes.push({ id: "bay:0", kind: "bay", label: "Bay 00", slot: 0, related_slots: [0], metrics: {}, raw: {} });
  const html = renderLanes(fabric);
  assert.equal((html.match(/ses:sg9/g) || []).length, 1);
  assert.ok(html.includes("Controller attachment not read"));
  assert.ok(html.includes("bays:0"));
  assert.ok(!html.includes("controller:linux-ses"));
});

test("parent-side lane sets show the remote PHYs as unknown", () => {
  const fabric = syntheticFabric();
  const model = topology().buildTopologyModel(fabric);
  const cascade = model.links.find((link) => link.kind === "expander-expander");
  assert.equal(cascade.lanes.sourcePhys.join(","), "2,3");
  assert.equal(cascade.lanes.targetPhys.length, 0);
  assert.ok(cascade.labelLines.some((line) => line.includes("far end not read")));
});

test("SES-only payloads retain the existing not-collected explanation", () => {
  const model = topology().buildTopologyModel(syntheticFabric(false));
  assert.equal(model.contract, "storage");
  assert.ok(model.tiers.some((tier) => tier.state === "not_collected"));
});

function referencedTransportFabric() {
  const fabric = syntheticFabric();
  const port = { controller: "host2", state: "unknown", path_id: "path:host2:sysfs", member_device_name: "sdz", sas_transport_port_id: "sas-port:port-2:1:2" };
  fabric.raw.sas_transport_port_index = { "sas-port:port-2:1:2": { id: "sas-port:port-2:1:2", owner_id: "expander:rear", phy_names: ["phy-2:1:7"], state: "read" } };
  const rear = fabric.nodes.find((node) => node.id === "expander:rear");
  rear.controller_id = "controller:host2";
  rear.raw.phys = [{ name: "phy-2:1:7", phy: 7, negotiated_rate_gbps: 12, counter_state: "measured", invalid_dword_count: 9, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 }];
  fabric.nodes.push({ id: "bay:0", kind: "bay", label: "Bay 00", slot: 0, related_slots: [0], raw: {}, metrics: { present: true, device_name: "sdz" } });
  fabric.traces.push({ id: "bay:0", kind: "bay", label: "Bay 00", slots: [0], node_ids: ["host", "controller:host2", "expander:rear", "ses:sg9", "bay:0"], link_ids: [], metrics: { device_name: "sdz", path_states: [port] } });
  fabric.controllers = [{ id: "controller:host2", name: "host2" }];
  fabric.paths = [{ id: "path:host2:sysfs", controller: "host2", state: "unknown", count: 1, slots: [0], sas_transport_port_ids: [port.sas_transport_port_id] }];
  return fabric;
}

function transportRenderers(fabric) {
  const source = fs.readFileSync(path.join(root, "app/static/sas_fabric_view.js"), "utf8");
  const elements = new Map();
  const context = vm.createContext({ URLSearchParams,
    window: { location: { search: "" }, SasFabricTopology: topology(), SasFabricTransport: transport(), SAS_FABRIC_BOOTSTRAP: { snapshot: { slots: [] }, fabric } },
    document: { getElementById(id) { if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "" }); return elements.get(id); }, querySelectorAll: () => [] },
  });
  const end = source.indexOf("  elements.modeButtons.forEach((button) => {", source.indexOf("  function render()"));
  vm.runInContext(source.slice(0, end) + "\nwindow.testApi = { branchSeedsForDiskTrace, renderDiskPathBranch, renderLanesMode };\n})();", context);
  return context.window.testApi;
}

test("referenced transport PHY rows resolve in topology, Disk Path and Storage Lanes", () => {
  const fabric = referencedTransportFabric();
  const api = topology();
  const resolve = transport().resolveTransportPort;
  assert.equal(typeof resolve, "function");
  const resolved = resolve(fabric, fabric.traces[0].metrics.path_states[0]);
  assert.equal(resolved.phys[0].invalid_dword_count, 9);
  const model = api.buildTopologyModel(fabric);
  // The approved model keeps resolved PHY detail on the independently pinnable port.
  assert.equal(model.bays[0].ports.length, 1);
  assert.equal(model.bays[0].ports[0].phy.counters.invalid_dword_count, 9);
  const ui = transportRenderers(fabric);
  const trace = fabric.traces[0];
  const seed = ui.branchSeedsForDiskTrace(trace, fabric)[0];
  const branch = ui.renderDiskPathBranch(seed, trace, fabric, 0);
  assert.ok(branch.includes("invalid dword 9"));
  assert.ok(ui.renderLanesMode(fabric).includes("1 reported PHY rows"));
});

test("dangling or contradictory transport PHY references remain not read", () => {
  const fabric = referencedTransportFabric();
  const api = topology();
  const resolve = transport().resolveTransportPort;
  assert.equal(typeof resolve, "function");
  const metric = fabric.traces[0].metrics.path_states[0];
  delete fabric.raw.sas_transport_port_index[metric.sas_transport_port_id];
  assert.equal(resolve(fabric, metric).state, "not_read");
  const changed = referencedTransportFabric();
  changed.raw.sas_transport_port_index[metric.sas_transport_port_id].phy_names.push("phy-missing");
  assert.equal(resolve(changed, metric).state, "not_read");
  assert.equal(resolve(changed, metric).phys.length, 0);
});

test("transport copy does not claim that reported expander detail is unexposed", () => {
  const source = fs.readFileSync(path.join(root, "app/static/sas_fabric_view.js"), "utf8");
  const start = source.indexOf("  function buildFabricViewCopy(");
  const end = source.indexOf("\n  function ", start + 10);
  assert.ok(start >= 0 && end > start);
  const context = vm.createContext({ fabricKind: (fabric) => fabric.raw.fabric_kind, modeCopy: {} });
  vm.runInContext(source.slice(start, end) + "\nthis.copy = buildFabricViewCopy;", context);
  const copy = context.copy(syntheticFabric(), "TrueNAS SCALE");
  assert.equal(copy.summary.controllers[0], "HBAs");
  assert.equal(copy.summary.expanders[0], "Expanders");
  assert.ok(!JSON.stringify(copy).includes("not exposed"));
  assert.equal(context.copy(syntheticFabric(false), "TrueNAS SCALE").summary.expanders[1], "not exposed");
});
