"use strict";

// Coverage-first wiring checks (#1504). Every check first names what it is
// expected to judge: each cable, including HBA uplinks and a cable the
// payload could not resolve; each expander PHY, including a merged
// observation's; each occupied or unknown bay, including one outside any
// board; each path, including one without an owner; each port. Each subject
// is read and fine, read and faulty, or not read. A check passes only when
// every subject is read and fine. Joins, pins and traces use payload ids.
// Every payload is synthetic.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const T = H.loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;
const DIRECT = require("../fixtures/sas_fabric/core_direct_paths.json");

const fixture = (id) => structuredClone(FIXTURES.find((item) => item.id === id));
const model = (item) => T.buildTopologyModel(item.fabric, { ...item.options, width: 1150, enclosureHref: (id) => `view?enclosure=${id}` });
const direct = () => structuredClone(DIRECT);
const directModel = (data) => T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
const core = () => structuredClone(H.producerCases().core_cascade);
const coreModel = (data) => T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
const checksOf = (built, id) => built.checks.filter((entry) => entry.id === id || entry.id.startsWith(`${id}:`));
const statuses = (built, id) => JSON.parse(JSON.stringify(checksOf(built, id).map((entry) => entry.status)));
const textOf = (value) => JSON.stringify(value);
const plain = (value) => JSON.parse(JSON.stringify(value));
const notPass = (built, id, why) => {
  const found = checksOf(built, id);
  assert.ok(found.length, `${why}: ${id} is reported`);
  assert.ok(!found.some((entry) => entry.status === "pass"), `${why}: ${id} must not pass: ${textOf(found)}`);
  return found;
};
const cutCounters = (row) => {
  row.counter_state = "not_read";
  for (const key of Object.keys(row).filter((name) => name.endsWith("_count"))) row[key] = null;
};
const lanes = (item) => item.fabric.links.filter((link) => ["controller-expander", "expander-expander"].includes(link.kind));
// The producer's shape for a parent it could not resolve: no cable, parent unknown.
const unresolve = (fabric, link) => {
  fabric.links = fabric.links.filter((item) => item !== link);
  const child = fabric.nodes.find((node) => node.id === link.target);
  Object.assign(child.raw, { parent_link_state: "unknown", parent_link_reason: "not_read", parent_id: null, parent_port: null, source_phys: [] });
};

// --- W2: a port's route is its own expander only -------------------------

test("W2: a port's trace reaches only its own expander, never another on its HBA", () => {
  const data = direct();
  const trace = data.fabric.traces.find((entry) => entry.id === "bay:0");
  const own = trace.metrics.mpr_devices.find((row) => row.member_device_name === "da3").expander_ids[0];
  const ownNode = data.fabric.nodes.find((node) => node.id === own);
  // A second expander in the same controller-local enclosure, cabled from the same HBA.
  const decoy = structuredClone(ownNode);
  decoy.id = "expander:controller:mpr0:5000c50000000dff";
  decoy.label = "5000c50000000dff";
  decoy.raw_id = "5000c50000000dff";
  decoy.raw = { ...decoy.raw, sas_address: "5000c50000000dff", dev_handle: "000a" };
  data.fabric.nodes.push(decoy);
  for (const link of data.fabric.links.filter((item) => item.source === own || item.target === own)) {
    const copy = structuredClone(link);
    copy.source = copy.source === own ? decoy.id : copy.source;
    copy.target = copy.target === own ? decoy.id : copy.target;
    copy.id = `${copy.kind}:${copy.source}->${copy.target}`;
    data.fabric.links.push(copy);
  }
  for (const entry of data.fabric.expanders || []) if (entry.id === own) data.fabric.expanders.push({ ...structuredClone(entry), id: decoy.id, sas_address: "5000c50000000dff", dev_handle: "000a" });
  const trace3 = (built) => {
    const port = built.bayById.get("bay:0").ports.find((item) => item.device === "da3");
    const path = T.tracePath(built, { kind: "port", id: port.key });
    return { port, expanders: [...path.nodes].filter((id) => id.startsWith("expander:")), links: [...path.links].filter((id) => id.includes(decoy.id)) };
  };
  const found = trace3(directModel(data));
  assert.equal(found.port.expanderId, own);
  assert.deepEqual(plain(found.expanders), [own]);
  assert.deepEqual(plain(found.links), []);
  // A port with no expander of its own highlights no expander at all.
  trace.metrics.mpr_devices = [];
  const blind = trace3(directModel(data));
  assert.equal(blind.port.expanderId, "");
  assert.deepEqual(plain(blind.expanders), []);
});

// --- W3: a pin follows only a provably identical path ---------------------

function fakeDom() {
  const handlers = new Map();
  const pinText = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pinText : null), querySelectorAll: () => [] };
  const container = { dataset: {}, querySelector: (selector) => (selector === ".fabric-topology" ? root : null), addEventListener: (name, handler) => handlers.set(name, handler) };
  const element = (dataset) => ({ dataset, closest(selector) { return selector === ".fabric-topology" ? root : /\[data-topo-/.test(selector) ? this : null; } });
  return { handlers, container, search: { value: "", addEventListener: () => {} }, document: { addEventListener: () => {} }, element };
}

test("W3: a pinned path without a distinct identity never switches to its twin", () => {
  const dom = fakeDom();
  const api = H.loadTopology({ document: dom.document });
  const { fabric, snapshot } = direct();
  const trace = fabric.traces.find((entry) => entry.id === "bay:0");
  trace.metrics.path_states = trace.metrics.mpr_devices.map((row, index) => ({ controller: "mpr0", state: index ? "active" : "standby", sas_address: row.sas_address }));
  trace.metrics.mpr_devices = [];
  api.attach(dom.container, dom.search, { textContent: "" });
  const options = { inventory: snapshot, selection: null };
  api.render(fabric, options);
  dom.handlers.get("click")({ target: dom.element({ topoPort: "bay:0#0" }) });
  const address = () => api.inspectorDetail()?.html.match(/Port SAS address<\/span><span class="kv-value">([^<]+)/)?.[1];
  const before = address();
  assert.ok(before);
  trace.metrics.path_states.reverse();
  api.render(fabric, options);
  // The same path, or no pin: never the other path's address.
  assert.ok(!api.view().pin || address() === before, `${before} -> ${address()}`);
});

// --- W4: labels never join identities --------------------------------------

test("W4a: a path's controller is its payload id, whatever Friendly Label any node carries", () => {
  for (const second of [true, false]) {
    const item = fixture("pair-standby-silent");
    const a = item.fabric.nodes.find((node) => node.kind === "controller");
    a.alias = "Friendly A";
    if (second) {
      const b = structuredClone(a);
      Object.assign(b, { id: "controller:node:node-bbbb", label: "node-b", alias: "node-a" });
      item.fabric.nodes.push(b);
    }
    const built = model(item);
    const state = built.bays[0].pathStates[0];
    assert.equal(state.controllerId, a.id, `second=${second}: ${textOf(state)}`);
  }
});

test("W4b: a cable's near-end rows are the near end's, even when both ends share a label", () => {
  const item = fixture("wide-x4-sata");
  const cable = model(item).links.find((link) => link.kind === "controller-expander" && link.lanes?.count === 4);
  for (const id of [cable.source, cable.target]) item.fabric.nodes.find((node) => node.id === id).alias = "IO";
  const built = model(item);
  const rows = T.cardFor(built, { kind: "link", id: cable.id }).rows;
  const perLane = rows.find((row) => row.label === "Per lane");
  assert.ok(perLane, textOf(rows));
  const named = (perLane.value.match(/PHYs? ([0-9–, ]+):/g) || []).join(" ");
  for (const phy of cable.lanes.targetPhys) assert.doesNotMatch(named, new RegExp(`\\b${phy}\\b`), `far PHY ${phy} in the near row: ${perLane.value}`);
});

// --- W5: the inspector uses the check's mutual-capability rule -------------

test("W5: the cable inspector never marks a lane slow that runs at both ends' common maximum", () => {
  const item = fixture("wide-x4-sata");
  let built = model(item);
  const cable = built.links.find((link) => link.lanes?.count === 4);
  item.fabric.links.find((link) => link.id === cable.id).reported_lanes.negotiated_rate_gbps = 6;
  for (const [id, phys, max] of [[cable.source, cable.lanes.sourcePhys, 12], [cable.target, cable.lanes.targetPhys, 6]]) {
    const node = item.fabric.nodes.find((entry) => entry.id === id);
    for (const row of node.raw.phys || node.raw.phy_rows) if (phys.includes(row.phy)) Object.assign(row, { speed: "6.0 Gbit", negotiated_link_rate: "6.0 Gbit", negotiated_rate_gbps: 6, maximum_rate_gbps: max, maximum_link_rate: `${max}.0 Gbit` });
  }
  built = model(item);
  const html = T.detailFor(built, { kind: "link", id: cable.id }).html;
  const rows = html.match(/<div class="kv-row[^"]*"[^>]*>(?:(?!<\/div>).)*PHY [0-3]\b(?:(?!<\/div>).)*<\/div>/g) || [];
  assert.ok(rows.length, html.slice(0, 400));
  for (const row of rows) assert.doesNotMatch(row, /topo-warn|is-critical|is-warning/, row);
});

// --- W6: every cable, both ends, every uplink PHY ---------------------------

test("W6: cable error counters pass only when every cable end the platform reads was read", () => {
  const cases = {
    "uplink lanes not reported": (item) => { lanes(item).find((link) => link.kind === "controller-expander").reported_lanes = null; },
    "uplink source membership empty": (item) => { lanes(item).find((link) => link.kind === "controller-expander").reported_lanes.source_phys = []; },
    "HBA rows of one cable missing": (item) => {
      const uplink = lanes(item).find((link) => link.kind === "controller-expander");
      const hba = item.fabric.nodes.find((node) => node.id === uplink.source);
      hba.raw.phy_rows = hba.raw.phy_rows.filter((row) => !uplink.reported_lanes.source_phys.includes(row.phy));
    },
  };
  for (const [name, change] of Object.entries(cases)) {
    for (const id of ["fanout-2", "wide-x4-sata"]) {
      const item = fixture(id);
      assert.deepEqual(statuses(model(item), "cable-errors"), ["pass"], `${id} control`);
      change(item);
      const found = notPass(model(item), "cable-errors", `${id}: ${name}`);
      assert.doesNotMatch(textOf(found), /checked at both of its ends/, `${id}: ${name}`);
    }
  }
  // A cascade the payload could not resolve: its uplink PHYs' counters are not read.
  const data = core();
  const cut = data.fabric.links.find((link) => link.kind === "expander-expander");
  unresolve(data.fabric, cut);
  for (const entry of data.fabric.expanders) for (const row of entry.phys) {
    if (entry.id === cut.source && cut.reported_lanes.source_phys.includes(Number(row.phy))) { row.device = "SMP Target"; cutCounters(row); }
  }
  notPass(coreModel(data), "cable-errors", "unresolved cascade");
});

// --- W7: reach, cascades and lanes count unresolved parents -----------------

test("W7: an expander whose parent cable was not resolved is not reached, cascaded or cabled", () => {
  // Redundancy: the second HBA's uplink is not resolved.
  const dual = fixture("dual-domain-linux");
  assert.deepEqual(statuses(model(dual), "redundancy"), ["pass"]);
  const uplinks = lanes(dual).filter((link) => link.kind === "controller-expander");
  unresolve(dual.fabric, uplinks[uplinks.length - 1]);
  notPass(model(dual), "redundancy", "unresolved HBA uplink");
  // Cascades: both children of the head expander have no resolved parent.
  const fan = fixture("fanout-2");
  for (const link of lanes(fan).filter((item) => item.kind === "expander-expander")) unresolve(fan.fabric, link);
  const built = model(fan);
  notPass(built, "cascades", "unresolved cascades");
  notPass(built, "cable-errors", "unresolved cascades");
  // Lanes: a withheld cascade on a CORE fabric whose other cables pass.
  const data = core();
  assert.deepEqual(statuses(coreModel(data), "lanes"), ["pass"]);
  unresolve(data.fabric, data.fabric.links.find((link) => link.kind === "expander-expander"));
  notPass(coreModel(data), "lanes", "withheld cascade");
  // A partial expander read leaves every hardware check partial.
  const partial = core();
  partial.fabric.raw.controller_read.counts.expanders = "partial";
  for (const id of ["lanes", "redundancy", "cascades"]) notPass(coreModel(partial), id, "expanders partial");
  // A cascade end without an HBA id is not read, never a crossed side.
  for (const both of [false, true]) {
    const crossed = core();
    const cascade = crossed.fabric.links.find((link) => link.kind === "expander-expander");
    for (const node of crossed.fabric.nodes.filter((entry) => (both ? [cascade.source, cascade.target] : [cascade.target]).includes(entry.id))) node.controller_id = null;
    const found = notPass(coreModel(crossed), "cascades", `controller id cleared, both=${both}`);
    assert.doesNotMatch(textOf(found.filter((entry) => entry.status === "warning")), /joins an expander via/, `both=${both}`);
  }
  // A neighbour whose enclosure is not read is not a confirmed other backplane.
  const five = model(fixture("fanout-5"));
  assert.ok(five.nodes.flatMap((entry) => [entry, ...(entry.stubGroup || [])]).some((entry) => entry.neighbour && !entry.neighbour.known));
  notPass(five, "cascades", "unknown neighbour enclosure");
});

// --- W8: every path, with or without an owner -------------------------------

test("W8: path states count every path, its owner read or not", () => {
  const failed = direct();
  Object.assign(failed.fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states[0], { controller: null, state: "fail", not_read: ["controller"] });
  assert.ok(statuses(directModel(failed), "path-states").includes("critical"), textOf(checksOf(directModel(failed), "path-states")));
  const limited = direct();
  const bay = limited.fabric.traces.find((entry) => entry.id === "bay:0");
  bay.metrics.not_read = ["path_states"];
  bay.metrics.not_read_reasons = { path_states: "path limit" };
  notPass(directModel(limited), "path-states", "bay with paths past the limit");
  const query = direct();
  query.fabric.raw.controller_read.counts.paths = "partial";
  notPass(directModel(query), "path-states", "partial path count");
  const occupied = direct();
  occupied.fabric.nodes.find((node) => node.id === "bay:1").metrics.present = true;
  notPass(directModel(occupied), "path-states", "occupied bay without a path state");
  // A state marked not read is not shown as read on its port either.
  for (const [id, state] of [["wide-x4-sata", "active"]]) {
    const item = fixture(id);
    const trace = item.fabric.traces.find((entry) => entry.id === "bay:0");
    for (const row of [...trace.metrics.path_states, ...trace.metrics.sas_transport_ports]) Object.assign(row, { state, not_read: ["state"] });
    const built = model(item);
    const port = built.bayById.get("bay:0").ports[0];
    const shown = T.cardFor(built, { kind: "port", id: port.key }).rows.find((row) => row.label === "State").value;
    assert.doesNotMatch(shown, new RegExp(`^${state}$`), shown);
  }
  const marked = direct();
  marked.fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states[0].not_read = ["state"];
  const built = directModel(marked);
  const port = built.bayById.get("bay:0").ports.find((item) => item.device === "da3");
  assert.doesNotMatch(T.cardFor(built, { kind: "port", id: port.key }).rows.find((row) => row.label === "State").value, /^standby$/);
});

// --- W9: every occupied or unknown bay, wherever it is ----------------------

test("W9: port placement passes only when every bay that may hold a disk is placed from read evidence", () => {
  const unknown = fixture("fanout-2");
  for (const node of unknown.fabric.nodes.filter((entry) => entry.kind === "bay")) node.metrics.present = null;
  notPass(model(unknown), "ports", "unknown occupancy");
  const extra = fixture("fanout-2");
  extra.fabric.nodes.push({ ...structuredClone(extra.fabric.nodes.find((node) => node.kind === "bay")), id: "bay:99", slot: 99, label: "Bay 99", metrics: { present: true, device_name: "synthetic-extra" }, raw: {} });
  const outside = model(extra);
  notPass(outside, "ports", "occupied bay outside any board");
  notPass(outside, "port-errors", "occupied bay outside any board");
  // A SATA port whose transport entry the resolver rejected is not placed.
  const sata = fixture("wide-x4-sata");
  const sataBay = model(sata).bays.find((bay) => bay.occupancy === "yes" && bay.protocol === "SATA");
  const ref = sata.fabric.traces.find((entry) => entry.id === sataBay.bayId).metrics.sas_transport_ports[0];
  sata.fabric.raw.sas_transport_port_index[ref.sas_transport_port_id].state = "not_read";
  notPass(model(sata), "ports", "rejected SATA port");
  // A port on another enclosure's expander is not on its own backplane.
  const away = fixture("fanout-2");
  const trace = away.fabric.traces.find((entry) => entry.id === "bay:0");
  const dest = away.fabric.raw.sas_transport_ports.find((port) => port.owner_id?.endsWith("expander-2:1") && port.has_block);
  const { id, owner_id, phy_names, state } = dest;
  away.fabric.raw.sas_transport_port_index[id] = { id, owner_id, phy_names, state };
  for (const row of [...trace.metrics.path_states, ...trace.metrics.sas_transport_ports]) Object.assign(row, { sas_transport_port_id: id, expander_ids: [owner_id], sas_address: dest.remote_sas_address });
  notPass(model(away), "ports", "port on a neighbour's expander");
  // An HBA whose path the payload names without its controller: not "no path via".
  const owner = direct();
  Object.assign(owner.fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states.find((row) => row.device_name === "da11"), { controller: null, not_read: ["controller", "target"] });
  const built = directModel(owner);
  assert.doesNotMatch(textOf(checksOf(built, "ports").filter((entry) => entry.status === "warning")), /no path via/);
  // A Linux disk whose block device was not joined is not read, not "no path".
  const zero = fixture("fanout-2");
  Object.assign(zero.fabric.traces.find((entry) => entry.id === "bay:0").metrics, { path_states: [], sas_transport_ports: [] });
  const zeroBuilt = model(zero);
  assert.doesNotMatch(textOf(checksOf(zeroBuilt, "ports").filter((entry) => entry.status === "warning")), /no path is reported/);
  notPass(zeroBuilt, "ports", "unjoined Linux disk");
});

// --- W10: every disk port PHY, every observation of it ----------------------

test("W10: disk port counters cover every observation, port and expected path", () => {
  // The second HBA's observation of the same chip measures a fault.
  const twin = fixture("dual-domain-linux");
  const rows = twin.fabric.nodes.filter((node) => node.kind === "expander");
  const port = model(twin).bays.find((bay) => bay.ports.some((item) => item.phy)).ports.find((item) => item.phy);
  const phy = port.phy.phy;
  const other = rows.find((node) => node.id !== port.expanderId && (node.raw.phys || []).some((row) => row.phy === phy) && node.raw.sas_address === rows.find((entry) => entry.id === port.expanderId).raw.sas_address);
  assert.ok(other, "the chip is observed via both HBAs");
  other.raw.phys.find((row) => row.phy === phy).invalid_dword_count = 101;
  const built = model(twin);
  const found = checksOf(built, "port-errors");
  assert.ok(found.some((entry) => entry.status === "critical"), textOf(found));
  assert.match(textOf(found.filter((entry) => entry.status === "critical").map((entry) => entry.lines)), /observations of this PHY disagree/);
  // A PHY only another HBA's observation reports is still judged.
  const only = fixture("dual-domain-linux");
  const onlyModel = model(only);
  const joined = onlyModel.bays.flatMap((bay) => bay.ports).find((item) => item.phy);
  const canonical = only.fabric.nodes.find((node) => node.id === joined.expanderId);
  const twinRow = only.fabric.nodes.find((node) => node.kind === "expander" && node.id !== canonical.id && node.raw.sas_address === canonical.raw.sas_address);
  const twinPhy = twinRow.raw.phys.find((row) => row.phy === joined.phy.phy);
  // Point the bay's own transport entry at the other observation, then drop the PHY from the first.
  const index = only.fabric.raw.sas_transport_port_index;
  const ref = only.fabric.traces.find((entry) => entry.id === joined.bayId).metrics.sas_transport_ports[0].sas_transport_port_id;
  Object.assign(index[ref], { owner_id: twinRow.id, phy_names: [twinPhy.name] });
  canonical.raw.phys = canonical.raw.phys.filter((row) => row.phy !== joined.phy.phy);
  twinPhy.invalid_dword_count = 101;
  const lone = model(only);
  assert.equal(lone.bayById.get(joined.bayId).ports.find((item) => item.phy)?.expanderId, twinRow.id);
  assert.ok(statuses(lone, "port-errors").includes("critical"), textOf(checksOf(lone, "port-errors")));
  // An occupied bay with no port: its PHY is not read.
  const zero = fixture("fanout-2");
  Object.assign(zero.fabric.traces.find((entry) => entry.id === "bay:0").metrics, { path_states: [], sas_transport_ports: [] });
  notPass(model(zero), "port-errors", "occupied bay without a port");
  // A known disk PHY no bay maps, with counters not read.
  const spare = fixture("fanout-2");
  const leaf = spare.fabric.nodes.filter((node) => node.kind === "expander")[1];
  const attached = spare.fabric.raw.sas_transport_ports.find((port) => port.owner_id === leaf.id && port.has_block);
  cutCounters(leaf.raw.phys.find((row) => row.phy === attached.phys[0]));
  notPass(model(spare), "port-errors", "unread known disk PHY");
  // Paths the collector did not join are not checked: never all disk ports.
  notPass(model(fixture("dual-domain-linux")), "port-errors", "unjoined second paths");
});

test("a subject no judgement reaches is not read, never a pass", () => {
  // A disk port the payload places on a PHY a cable uses: contradictory, so
  // the disk-port check does not judge it, and must not pass without it.
  const item = fixture("wide-x4-sata");
  const before = model(item);
  assert.deepEqual(statuses(before, "port-errors"), ["pass"]);
  const cable = before.links.find((link) => link.kind === "controller-expander" && link.lanes?.targetPhys.length);
  const far = item.fabric.nodes.find((node) => node.id === cable.target);
  const phy = far.raw.phys.find((row) => row.phy === cable.lanes.targetPhys[0]);
  const bay = before.bays.find((entry) => entry.occupancy === "yes" && entry.ports.some((port) => port.expanderId === cable.target));
  const trace = item.fabric.traces.find((entry) => entry.id === bay.bayId);
  const id = trace.metrics.sas_transport_ports[0].sas_transport_port_id;
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id: far.id, phy_names: [phy.name], state: "read" };
  const built = model(item);
  assert.equal(built.bayById.get(bay.bayId).ports[0].phy?.phy, phy.phy);
  // A bay and a cable both claim the PHY: its owner is not read, and said.
  const found = notPass(built, "port-errors", "disk port on a cable PHY");
  assert.match(textOf(found), /owner not read: .* all claim it/);
});

// --- W11: neighbour health from the same coverage set -----------------------

test("W11: a neighbour's attached PHY without a rate still counts for its health", () => {
  const item = fixture("fanout-2");
  const leaf = item.fabric.nodes.filter((node) => node.kind === "expander")[1];
  const attached = item.fabric.raw.sas_transport_ports.find((port) => port.owner_id === leaf.id && port.has_block);
  const row = leaf.raw.phys.find((entry) => entry.phy === attached.phys[0]);
  cutCounters(row);
  Object.assign(row, { speed: null, negotiated_rate_gbps: null, negotiated_link_rate: null });
  const stub = model(item).nodeById.get(leaf.id);
  assert.notEqual(stub.neighbour.health, "no PHY errors");
  assert.match(stub.neighbour.health, /not read/);
});

// --- Property: deleting any one subject's evidence ------------------------
//
// Every subject, one at a time: each PHY at each end of each cable on each
// observation of its chip, each disk port PHY on each observation, each
// cable's lanes and its parent link, each path state, each bay's occupancy
// and its ports. After the deletion no check may vanish, no check may become
// PASS, the subject's own checks may not pass, and they must name the lost
// subject or count what was read; a known fault elsewhere must stay.

const CHECK_IDS = (built) => [...new Set(built.checks.map((entry) => entry.id.split(":")[0]))];
const tierOfCheck = (built, id) => statuses(built, id).filter((status) => status !== "not_read" && status !== "pass");

function subjectDeletions(fabric, built) {
  const out = [];
  const nodes = new Map(fabric.nodes.map((node) => [node.id, node]));
  const COUNTERS = ["invalid_dword_count", "running_disparity_error_count", "loss_of_dword_synchronization_count", "phy_reset_problem_count"];
  // Every table a PHY row of this owner can sit in: the node's own, and the
  // payload's expander list (CORE projects that one; its numbers are text).
  const tables = (copy, ownerId) => {
    const owner = copy.fabric.nodes.find((entry) => entry.id === ownerId);
    return [owner?.raw?.phys, owner?.raw?.phy_rows, ...(copy.fabric.expanders || []).filter((entry) => entry.id === ownerId).map((entry) => entry.phys)].filter(Array.isArray);
  };
  const phyRows = (node) => node?.raw?.phys || node?.raw?.phy_rows || [];
  const hasPhy = (ownerId, phy) => {
    const lists = [phyRows(nodes.get(ownerId)), ...(fabric.expanders || []).filter((entry) => entry.id === ownerId).map((entry) => entry.phys || [])];
    return lists.some((rows) => rows.some((entry) => Number(entry.phy) === phy));
  };
  // Every observation of the chip a node belongs to: nodes with its SAS address.
  const observations = (id) => {
    const node = nodes.get(id);
    const address = node?.raw?.sas_address;
    return node?.kind === "expander" && address ? fabric.nodes.filter((entry) => entry.kind === "expander" && entry.raw?.sas_address === address) : node ? [node] : [];
  };
  const cut = (copy, ownerId, phy) => {
    let done = false;
    for (const rows of tables(copy, ownerId)) {
      for (const row of rows.filter((entry) => Number(entry.phy) === phy)) {
        cutCounters(row);
        row.not_read = [...new Set([...(row.not_read || []), ...COUNTERS])];
        done = true;
      }
    }
    return done;
  };
  const drop = (copy, ownerId, phy) => {
    let done = false;
    for (const rows of tables(copy, ownerId)) {
      for (let index = rows.length - 1; index >= 0; index -= 1) if (Number(rows[index].phy) === phy) { rows.splice(index, 1); done = true; }
    }
    return done;
  };
  const inject = (copy, ownerId, phy) => {
    let done = false;
    for (const rows of tables(copy, ownerId)) {
      for (const row of rows.filter((entry) => Number(entry.phy) === phy)) {
        Object.assign(row, { counter_state: "measured", counter_reason: null, not_read: [], invalid_dword_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 10, phy_reset_problem_count: 0 });
        done = true;
      }
    }
    return done;
  };
  // Another row of the same table is given this PHY's number.
  const duplicate = (copy, ownerId, phy) => {
    let done = false;
    for (const rows of tables(copy, ownerId)) {
      const other = rows.find((entry) => Number(entry.phy) !== phy);
      if (!other) continue;
      other.phy = typeof other.phy === "string" ? String(phy) : phy;
      done = true;
    }
    return done;
  };
  // Each of a PHY's evidences, on each observation of its chip.
  const phySubject = (label, checks, tokens, ownerId, phy) => {
    for (const observed of observations(ownerId)) {
      if (!hasPhy(observed.id, phy)) continue;
      // An HBA's own counters are subjects only where the platform reads them.
      if (observed.kind === "controller" && !phyRows(observed).some((entry) => entry.counter_state)) continue;
      const faulty = { owner: observed.id, phy };
      out.push({ name: `${label} on ${observed.id}`, checks, tokens, faulty, change: (copy) => cut(copy, observed.id, phy) });
      out.push({ name: `${label} on ${observed.id}, row deleted`, checks, tokens, faulty, change: (copy) => drop(copy, observed.id, phy) });
      // The opposite of a deletion: a critical count measured on this one
      // observation. Some check must say it, naming this PHY and the count.
      out.push({ name: `${label} on ${observed.id}, critical injected`, checks, tokens, faulty, injected: { phy }, change: (copy) => inject(copy, observed.id, phy) });
      // Only where the table has another row to give this PHY's number.
      if (phyRows(observed).some((entry) => Number(entry.phy) !== phy)) out.push({ name: `${label} on ${observed.id}, number duplicated`, checks, tokens, faulty, change: (copy) => duplicate(copy, observed.id, phy) });
    }
  };
  const cables = fabric.links.filter((link) => ["controller-expander", "expander-expander"].includes(link.kind));
  cables.forEach((link, index) => {
    const lanesOf = (copy) => copy.fabric.links.filter((entry) => ["controller-expander", "expander-expander"].includes(entry.kind))[index];
    out.push({ name: `cable ${index} lanes`, checks: ["lanes", "cable-errors"], tokens: [link.source, link.target], change: (copy) => { lanesOf(copy).reported_lanes = null; return true; } });
    out.push({ name: `cable ${index} parent`, checks: ["lanes", "cable-errors"], tokens: [link.source, link.target], change: (copy) => { unresolve(copy.fabric, lanesOf(copy)); return true; } });
    const near = link.reported_lanes?.source_phys || [];
    if (near.length > 1) {
      out.push({ name: `cable ${index} repeated member`, checks: ["lanes", "cable-errors"], tokens: [link.source, link.target], change: (copy) => {
        const lanes = lanesOf(copy).reported_lanes;
        lanes.source_phys[lanes.source_phys.length - 1] = lanes.source_phys[0];
        return true;
      } });
    }
    const far = link.reported_lanes?.target_phys || [];
    if (far.length > 1) {
      out.push({ name: `cable ${index} repeated far member`, checks: ["lanes", "cable-errors"], tokens: [link.source, link.target], change: (copy) => {
        const lanes = lanesOf(copy).reported_lanes;
        lanes.target_phys[lanes.target_phys.length - 1] = lanes.target_phys[0];
        const pairs = lanes.lanes || [];
        if (pairs.length) pairs[pairs.length - 1].target_phy = pairs[0].target_phy;
        return true;
      } });
    }
    for (const phy of near) phySubject(`cable ${index} near PHY ${phy}`, ["cable-errors"], [link.source, `PHY ${phy}`], link.source, phy);
    // The far end's members as the renderer derives them (sysfs reports a
    // port on the parent only), else as reported.
    // The cables as the checks saw them, before neighbours were grouped.
    const drawn = built.links.flatMap((entry) => entry.stubCables || [entry]).find((entry) => entry.source === link.source && entry.target === link.target && entry.lanes);
    for (const phy of drawn?.lanes.targetPhys || link.reported_lanes?.target_phys || []) phySubject(`cable ${index} far PHY ${phy}`, ["cable-errors"], [link.target, `PHY ${phy}`], link.target, phy);
  });
  for (const port of fabric.raw?.sas_transport_ports || []) {
    if (port.remote_kind !== "end_device" || !port.has_block) continue;
    for (const phy of port.phys || []) phySubject(`disk PHY ${phy}`, ["port-errors"], [port.owner_id, `PHY ${phy}`], port.owner_id, phy);
  }
  for (const trace of fabric.traces.filter((entry) => entry.kind === "bay")) {
    (trace.metrics?.path_states || []).forEach((state, index) => {
      out.push({ name: `${trace.id} path ${index}`, checks: ["path-states"], tokens: [trace.id], change: (copy) => {
        Object.assign(copy.fabric.traces.find((entry) => entry.id === trace.id).metrics.path_states[index], { state: "unknown", not_read: ["state"] });
        return true;
      } });
    });
  }
  for (const bay of fabric.nodes.filter((node) => node.kind === "bay" && node.metrics?.present === true)) {
    out.push({ name: `${bay.id} occupancy`, checks: ["ports"], tokens: [bay.id], change: (copy) => { copy.fabric.nodes.find((entry) => entry.id === bay.id).metrics.present = null; return true; } });
    out.push({ name: `${bay.id} ports`, checks: ["ports", "port-errors", "path-states"], tokens: [bay.id], change: (copy) => {
      const trace = copy.fabric.traces.find((entry) => entry.id === bay.id);
      if (!trace) return false;
      Object.assign(trace.metrics, { path_states: [], sas_transport_ports: [] });
      return true;
    } });
  }
  return out;
}

// Whether a check's entries name the lost subject, or count what was read.
function accounted(before, after, id, tokens) {
  const lines = checksOf(after, id).filter((entry) => entry.status !== "pass").flatMap((entry) => entry.lines);
  const text = textOf(lines);
  if (lines.some((line) => /\b\d+\b/.test(line) && /\bnot read\b/.test(line))) return true;
  // A node is named by its label or by the short form of its SAS address.
  const tail = (token) => {
    const address = before.nodeById.get(token)?.address || before.nodeById.get(context(token))?.address || "";
    return address ? address.slice(-4) : "";
  };
  const context = (token) => before.nodeById.get(token)?.mergedInto || token;
  const labels = tokens.flatMap((token) => [before.nodeById.get(token)?.label, before.bayById.get(token)?.label, tail(token), /^PHY \d+$/.test(token) ? token : ""]).filter(Boolean);
  // Counted (a partial count, or a number of subjects not read) or named.
  return /Partial: \d+/.test(text) || /\b\d+ [a-zA-Z ]+? not read\b/.test(text) || labels.some((label) => text.includes(label));
}

// Every CRITICAL or WARNING line of a counter check names a measured count
// that reaches its tier; every CRITICAL lane line names its rate.
const LIMITS = { "invalid dword": [1, 100], "running disparity": [1, 100], "loss of dword sync": [1, 10], "PHY reset problem": [1, 10] };
function namesItsMeasurement(built, why) {
  for (const entry of built.checks) {
    const id = entry.id.split(":")[0];
    if (!["critical", "warning"].includes(entry.status)) continue;
    for (const line of entry.lines) {
      if (id === "lanes" && entry.status === "critical") {
        assert.match(line, /at \d+(\.\d+)? Gb\/s \(both ends \d+(\.\d+)?\)/, `${why}: ${id} critical without its rate: ${line}`);
        continue;
      }
      if (!["cable-errors", "port-errors"].includes(id)) continue;
      const counts = [...line.matchAll(/(\d+)(?:–(\d+))? (invalid dword|running disparity|loss of dword sync|PHY reset problem)/g)]
        .map((match) => [Number(match[2] || match[1]), LIMITS[match[3]]]);
      const level = entry.status === "critical" ? 1 : 0;
      assert.ok(counts.some(([count, limit]) => count >= limit[level]), `${why}: ${id} ${entry.status} line names no count of that tier: ${line}`);
    }
  }
}
const namesPhy = (line, phy) => [...line.matchAll(/PHY (\d+)(?:–(\d+))?/g)].some((match) => phy >= Number(match[1]) && phy <= Number(match[2] || match[1]));

function runProperty(name, base, build) {
  const before = build(structuredClone(base));
  namesItsMeasurement(before, `${name}: baseline`);
  const ids = CHECK_IDS(before);
  const critical = new Set(ids.filter((id) => tierOfCheck(before, id).includes("critical")));
  const deletions = subjectDeletions(base.fabric, before);
  let applied = 0;
  let absent = 0;
  for (const deletion of deletions) {
    const copy = structuredClone(base);
    if (deletion.change(copy) === false) continue;
    // The evidence was already absent (e.g. counters already not read).
    if (JSON.stringify(copy) === JSON.stringify(base)) {
      absent += 1;
      continue;
    }
    applied += 1;
    const after = build(copy);
    const why = `${name}: deleting ${deletion.name}`;
    namesItsMeasurement(after, why);
    if (deletion.injected && !ids.length) {
      // A fabric that builds no check list: the PHY's own card still says it.
      const owner = deletion.faulty.owner;
      const node = after.nodeById.get(owner) || after.nodes.find((entry) => entry.rows?.some((row) => row.id === owner) || entry.stubGroup?.some((member) => member.id === owner));
      const rows = node ? T.cardFor(after, { kind: "node", id: node.id })?.rows || [] : [];
      assert.ok(rows.some((row) => row.warn === "critical"), `${why}: no check list, and the card of ${owner} does not say the critical: ${textOf(rows)}`);
      continue;
    }
    if (deletion.injected) {
      // Said once as critical, with this PHY and its count, in a counter check.
      const lines = after.checks.filter((entry) => ["cable-errors", "port-errors"].includes(entry.id.split(":")[0]) && entry.status === "critical").flatMap((entry) => entry.lines)
        .filter((line) => /10 loss of dword sync/.test(line) && namesPhy(line, deletion.injected.phy));
      assert.ok(lines.length >= 1, `${why}: the injected critical is not said with PHY ${deletion.injected.phy}: ${textOf(after.checks.filter((entry) => ["cable-errors", "port-errors"].includes(entry.id.split(":")[0])))}`);
      continue;
    }
    for (const id of ids) {
      assert.ok(checksOf(after, id).length, `${why} made ${id} vanish`);
      if (!statuses(before, id).includes("pass")) assert.ok(!statuses(after, id).includes("pass"), `${why} made ${id} pass: ${textOf(checksOf(after, id))}`);
    }
    for (const id of deletion.checks.filter((check) => ids.includes(check))) {
      assert.ok(!statuses(after, id).includes("pass"), `${why} kept ${id} at PASS: ${textOf(checksOf(after, id))}`);
      // Where the check judged its subjects, it names or counts the lost one.
      if (statuses(before, id).includes("pass") && !tierOfCheck(after, id).length) assert.ok(accounted(before, after, id, deletion.tokens), `${why}: ${id} does not name or count the lost subject: ${textOf(checksOf(after, id))}`);
    }
    // A known critical fault stays unless the deleted evidence was the
    // fault, or removed the subject that carried it.
    // Only for the checks whose subjects it removes, never every check.
    const removesSubject = /(ports|lanes|parent|member)$/.test(deletion.name);
    const owner = deletion.faulty && base.fabric.nodes.find((node) => node.id === deletion.faulty.owner);
    const faultRow = owner && (owner.raw.phys || owner.raw.phy_rows || []).find((entry) => entry.phy === deletion.faulty.phy);
    const wasFault = faultRow && ["invalid_dword_count", "running_disparity_error_count", "loss_of_dword_synchronization_count", "phy_reset_problem_count"].some((key) => faultRow[key] > 0);
    if (!removesSubject && !wasFault) {
      for (const id of critical) assert.ok(tierOfCheck(after, id).includes("critical"), `${why} hid the known critical fault of ${id}: ${textOf(checksOf(after, id))}`);
    } else {
      for (const id of [...critical].filter((check) => !deletion.checks.includes(check))) assert.ok(tierOfCheck(after, id).includes("critical"), `${why} hid the unrelated critical fault of ${id}: ${textOf(checksOf(after, id))}`);
    }
  }
  return { proposed: deletions.length, applied, absent };
}

test("property: on every synthetic fabric and the CORE fabric, every deleted subject is not read, named or counted, and nothing vanishes", (t) => {
  const report = {};
  for (const base of FIXTURES) report[base.id] = runProperty(base.id, base, (item) => model(item));
  report.core_cascade = runProperty("core_cascade", core(), (data) => coreModel(data));
  t.diagnostic(`applied/proposed per fabric (already absent): ${Object.entries(report).map(([id, { applied, proposed, absent }]) => `${id} ${applied}/${proposed}${absent ? ` (${absent})` : ""}`).join(", ")}`);
  // Every subject proposed was deleted, or its evidence was already absent.
  for (const [id, { applied, proposed, absent }] of Object.entries(report)) {
    assert.equal(applied + absent, proposed, `${id}: ${applied} applied and ${absent} already absent of ${proposed}`);
  }
  assert.ok(Object.values(report).reduce((sum, item) => sum + item.applied, 0) > 500);
});

test("the disk-port PHY count is by subject type, whatever an expander id contains", () => {
  const item = fixture("fanout-2");
  const chip = item.fabric.nodes.find((node) => node.kind === "expander");
  const phy = chip.raw.phys.find((entry) => entry.phy === 0);
  phy.invalid_dword_count = null;
  phy.counter_state = "not_read";
  const strange = JSON.parse(JSON.stringify(item).replaceAll(chip.id, `${chip.id}#A`));
  for (const data of [item, strange]) {
    const found = checksOf(model(data), "port-errors");
    assert.ok(!found.some((entry) => entry.status === "pass"), textOf(found));
    assert.match(textOf(found), /\b1 of \d+ disk port PHYs not read\b/);
  }
});
