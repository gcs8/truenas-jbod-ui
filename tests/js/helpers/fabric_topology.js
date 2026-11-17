"use strict";

// Shared loaders for the Storage Fabric Topology tests (#937). The model is
// loaded the way tests/js/fabric_topology_model.test.js loads it; the
// real-producer payloads come from tests/fixtures/sas_fabric/topology_fabric.json,
// which tests/test_fabric_topology_fixture.py rebuilds through the Python
// builder and compares. Every identifier is synthetic; SAS addresses are built
// at run time in the sanitized 5000c500... form.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../../..");
const TOPOLOGY_PATH = path.join(ROOT, "app/static/sas_fabric_topology.js");
const FIXTURE_PATH = path.join(ROOT, "tests/fixtures/sas_fabric/topology_fabric.json");

function loadTopology(context = {}) {
  const sandbox = vm.createContext({ window: {}, ...context });
  vm.runInContext(fs.readFileSync(TOPOLOGY_PATH, "utf8"), sandbox);
  return sandbox.window.SasFabricTopology;
}

function producerCases() {
  return JSON.parse(fs.readFileSync(FIXTURE_PATH, "utf8"));
}

function sasAddress(tag) {
  return `5000c500${tag.toString(16).padStart(8, "0")}`;
}

function node(id, kind, extra = {}) {
  return { id, kind, label: id, related_slots: [], metrics: {}, raw: {}, evidence: [], ...extra };
}

function link(source, target, kind, extra = {}) {
  const suffix = extra.slot === undefined || extra.slot === null ? "" : `:${extra.slot}`;
  return { id: `${kind}:${source}->${target}${suffix}`, source, target, kind, related_slots: [], evidence: [], reported_lanes: null, ...extra };
}

function lanes(sourcePhys, targetPhys, rate = 12) {
  return {
    basis: "reciprocal_reported_phy_rows",
    lane_count: sourcePhys.length,
    source_phys: sourcePhys,
    target_phys: targetPhys,
    lanes: sourcePhys.map((phy, index) => ({ source_phy: phy, target_phy: targetPhys[index], negotiated_rate_gbps: rate })),
    negotiated_rate_gbps: rate,
    evidence: [],
  };
}

function coreFabric() {
  return {
    available: true,
    platform: "core",
    system_id: "synthetic-core",
    selected_enclosure_id: "synthetic-enclosure",
    snapshot_cache_state: "hit",
    source_cache_state: "hit",
    nodes: [node("host", "host", { label: "synthetic-host" })],
    links: [],
    traces: [],
    sources: { ssh: { enabled: true, ok: true } },
    raw: {
      controller_read: {
        counts: { controllers: "complete", paths: "complete", expanders: "complete", enclosures: "complete" },
        unread_phy_tables: 0,
      },
    },
  };
}

function addController(fabric, name) {
  const item = node(`controller:${name}`, "controller", { label: name, raw: { board: "SYNTH-HBA" }, metrics: { phy_count: 8, linked_phy_count: 4 } });
  fabric.nodes.push(item);
  fabric.links.push(link("host", item.id, "host-controller"));
  return item;
}

function addExpander(fabric, controllerName, tag, level, parentId, extra = {}) {
  const address = sasAddress(tag);
  const item = node(`expander:controller:${controllerName}:${address}`, "expander", {
    label: address,
    raw_id: address,
    controller_id: `controller:${controllerName}`,
    metrics: { sas_level: String(level), num_phys: "36", linked_phys: 12 },
    raw: { parent_link_state: parentId ? "resolved" : "unknown", sas_level: String(level) },
  });
  fabric.nodes.push(item);
  if (parentId) {
    fabric.links.push(link(parentId, item.id, parentId.startsWith("controller:") ? "controller-expander" : "expander-expander", extra));
  }
  return item;
}

function addEnclosure(fabric, controllerName, handle, label, parentId) {
  const item = node(`mpr-enclosure:controller:${controllerName}:${handle}`, "mpr-enclosure", { label, controller_id: `controller:${controllerName}` });
  fabric.nodes.push(item);
  if (parentId) fabric.links.push(link(parentId, item.id, "expander-enclosure"));
  return item;
}

// Bays behind one or more enclosures; `paths` gives each bay's path states.
function addBays(fabric, slots, enclosureIds, paths = []) {
  for (const slot of slots) {
    const bayId = `bay:${slot}`;
    fabric.nodes.push(node(bayId, "bay", {
      label: `Bay ${String(slot).padStart(2, "0")}`,
      slot,
      status: "healthy",
      related_slots: [slot],
      metrics: { present: true, device_name: `da${slot}` },
    }));
    for (const enclosureId of enclosureIds) {
      fabric.links.push(link(enclosureId, bayId, "mpr-enclosure-bay", { slot, related_slots: [slot] }));
    }
    fabric.traces.push({
      id: bayId,
      kind: "bay",
      label: `Bay ${slot}`,
      node_ids: ["host", bayId],
      link_ids: [],
      slots: [slot],
      metrics: { device_name: `da${slot}`, path_states: paths.map(({ controller, state }) => ({ controller, state })), mpr_devices: [] },
      evidence: [],
    });
  }
}

module.exports = {
  ROOT,
  loadTopology,
  producerCases,
  sasAddress,
  node,
  link,
  lanes,
  coreFabric,
  addController,
  addExpander,
  addEnclosure,
  addBays,
};
