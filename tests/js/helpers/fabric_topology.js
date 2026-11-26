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
const TOPOLOGY_PATHS = ["sas_fabric_topology_layout.js", "sas_fabric_topology_ui.js", "sas_fabric_topology_evidence.js", "sas_fabric_topology.js"].map((name) => path.join(ROOT, "app/static", name));
function topologySource() { return TOPOLOGY_PATHS.map((file) => fs.readFileSync(file, "utf8")).join("\n"); }
const FIXTURE_PATH = path.join(ROOT, "tests/fixtures/sas_fabric/topology_fabric.json");

function loadTopology(context = {}) {
  const sandbox = vm.createContext({ window: {}, ...context });
  // Each script by its own path, as the page loads them, so an exception
  // names the script it came from.
  for (const file of [path.join(ROOT, "app/static/sas_fabric_transport.js"), ...TOPOLOGY_PATHS]) vm.runInContext(fs.readFileSync(file, "utf8"), sandbox, { filename: file });
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

// PHY counts agree with the lanes the builder gives the HBA's links.
function addController(fabric, name, { phys = 8, linked = 4 } = {}) {
  const item = node(`controller:${name}`, "controller", { label: name, raw: { board: "SYNTH-HBA" }, metrics: { phy_count: phys, linked_phy_count: linked } });
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


const range = (start, end) => Array.from({ length: end - start }, (_, index) => start + index);
const DUAL = [{ controller: "mpr0", state: "active" }, { controller: "mpr1", state: "passive" }];

// The default two-HBA system: each HBA has a level-1 expander feeding a front
// enclosure and a level-2 expander feeding a rear one; 48 dual-pathed bays.
function twoHbaCascade() {
  const fabric = coreFabric();
  const ids = {};
  for (const [index, name] of ["mpr0", "mpr1"].entries()) {
    const hba = addController(fabric, name);
    const level1 = addExpander(fabric, name, 0xa10 + index * 0x100, 1, hba.id, { reported_lanes: lanes([0, 1, 2, 3], [32, 33, 34, 35]) });
    const level2 = addExpander(fabric, name, 0xa20 + index * 0x100, 2, level1.id, { reported_lanes: lanes([28, 29, 30, 31], [0, 1, 2, 3]) });
    const front = addEnclosure(fabric, name, "0x0001", `SYNTH-FRONT-${name.toUpperCase()}`, level1.id);
    const rear = addEnclosure(fabric, name, "0x0002", `SYNTH-REAR-${name.toUpperCase()}`, level2.id);
    ids[name] = { hba, level1, level2, front, rear };
  }
  addBays(fabric, range(0, 24), [ids.mpr0.front.id, ids.mpr1.front.id], DUAL);
  addBays(fabric, range(24, 48), [ids.mpr0.rear.id, ids.mpr1.rear.id], DUAL);
  return { fabric, ids };
}

// Two HBAs and four dual-pathed shelves of 60, 84, 102 and 106 bays; shelf D
// hangs off shelf C's expanders (a level-2 cascade).
function fourShelves() {
  const fabric = coreFabric();
  const shelves = [["A", 60], ["B", 84], ["C", 102], ["D", 106]];
  const expanders = { mpr0: {}, mpr1: {} };
  // Shelves A, B and C each take four lanes of each HBA: 12 of 16 PHYs.
  for (const name of ["mpr0", "mpr1"]) addController(fabric, name, { phys: 16, linked: 12 });
  let start = 0;
  shelves.forEach(([shelf, count], index) => {
    const enclosures = [];
    for (const [side, name] of ["mpr0", "mpr1"].entries()) {
      const level = shelf === "D" ? 2 : 1;
      const parent = shelf === "D" ? expanders[name].C.id : `controller:${name}`;
      const extra = shelf === "D"
        ? { reported_lanes: lanes([32, 33, 34, 35], [0, 1, 2, 3]) }
        : { reported_lanes: lanes([0, 1, 2, 3].map((phy) => phy + index * 4), [36, 37, 38, 39]) };
      const exp = addExpander(fabric, name, 0xd000 + side * 0x100 + (index + 1) * 0x10 + level, level, parent, extra);
      expanders[name][shelf] = exp;
      enclosures.push(addEnclosure(fabric, name, `0x000${index + 1}`, `SYNTH-SHELF-${shelf}-${side ? "IOM-B" : "IOM-A"}`, exp.id).id);
    }
    addBays(fabric, range(start, start + count), enclosures, DUAL);
    start += count;
  });
  return { fabric, expanders };
}

// Eight HBAs in four pairs; each pair serves four dual-pathed 106-bay shelves,
// one level-1 expander per HBA and shelf, four lanes each: 16 of 16 PHYs.
function eightHbaSixteenShelves() {
  const fabric = coreFabric();
  const names = range(0, 8).map((index) => `mpr${index}`);
  for (const name of names) addController(fabric, name, { phys: 16, linked: 16 });
  let start = 0;
  for (let shelf = 0; shelf < 16; shelf += 1) {
    const pair = Math.floor(shelf / 4);
    const enclosures = [];
    for (const side of [0, 1]) {
      const name = names[pair * 2 + side];
      const phys = [0, 1, 2, 3].map((phy) => phy + (shelf % 4) * 4);
      const exp = addExpander(fabric, name, 0xe000 + shelf * 0x10 + pair * 2 + side, 1, `controller:${name}`, { reported_lanes: lanes(phys, [32, 33, 34, 35]) });
      enclosures.push(addEnclosure(fabric, name, `s${shelf}`, `SYNTH-SHELF-${shelf}-${side ? "B" : "A"}`, exp.id).id);
    }
    addBays(fabric, range(start, start + 106), enclosures, [{ controller: names[pair * 2], state: "active" }, { controller: names[pair * 2 + 1], state: "passive" }]);
    start += 106;
  }
  return { fabric };
}

// Two HBAs, three dual-pathed 24-bay shelves; each side cascades through
// expander levels 1, 2 and 3, one shelf per level.
function threeLevelCascade() {
  const fabric = coreFabric();
  const enclosures = [[], [], []];
  for (const [side, name] of ["mpr0", "mpr1"].entries()) {
    const hba = addController(fabric, name);
    let parent = hba.id;
    for (let level = 1; level <= 3; level += 1) {
      const upstream = level === 1 ? [0, 1, 2, 3] : [28, 29, 30, 31];
      const exp = addExpander(fabric, name, 0xe000 + (level - 1) * 0x10 + side, level, parent, { reported_lanes: lanes(upstream, level === 1 ? [32, 33, 34, 35] : [0, 1, 2, 3]) });
      enclosures[level - 1].push(addEnclosure(fabric, name, `s${level - 1}`, `SYNTH-L${level}-${side ? "B" : "A"}`, exp.id).id);
      parent = exp.id;
    }
  }
  enclosures.forEach((ids, index) => addBays(fabric, range(index * 24, index * 24 + 24), ids, DUAL));
  return { fabric };
}

module.exports = {
  eightHbaSixteenShelves,
  threeLevelCascade,
  range,
  twoHbaCascade,
  fourShelves,
  ROOT,
  loadTopology,
  topologySource,
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
