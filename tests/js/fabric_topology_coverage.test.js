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
  const copyRow = (copy, ownerId, phy, where) => {
    let done = false;
    for (const rows of tables(copy, ownerId)) {
      const row = rows.find((entry) => Number(entry.phy) === phy);
      if (!row) continue;
      const twin = { ...row, name: `${row.name || "phy"}-copy`, counter_state: "measured", not_read: [], invalid_dword_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 };
      if (where === "first") rows.unshift(twin);
      else rows.push(twin);
      done = true;
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
      out.push({ name: `${label} on ${observed.id}, critical injected`, checks, tokens, faulty, injected: { phy }, countersOnly: true, change: (copy) => inject(copy, observed.id, phy) });
      // A second row for this PHY, a clean copy, put first or last: the
      // two orders must say the same.
      out.push({ name: `${label} on ${observed.id}, row duplicated`, checks, tokens, faulty, orders: true,
        change: (copy) => copyRow(copy, observed.id, phy, "first"), twin: (copy) => copyRow(copy, observed.id, phy, "last") });
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
    // A PHY of that end not in the list, appended: a disk port's where the
    // census names one there. The list is not read; the PHY keeps its role.
    for (const [side, key, end] of [["near", "source_phys", link.source], ["far", "target_phys", link.target]]) {
      const listed = link.reported_lanes?.[key] || [];
      if (!listed.length) continue;
      const rows = phyRows(nodes.get(end)).concat(...(fabric.expanders || []).filter((entry) => entry.id === end).map((entry) => entry.phys || []));
      const census = (fabric.raw?.sas_transport_ports || []).filter((port) => port.owner_id === end && port.remote_kind === "end_device").flatMap((port) => port.phys || []);
      const spare = [...census, ...rows.map((entry) => Number(entry.phy))].find((phy) => Number.isInteger(phy) && !listed.includes(phy));
      if (spare === undefined) continue;
      const append = (copy) => { lanesOf(copy).reported_lanes[key].push(spare); return true; };
      out.push({ name: `cable ${index} ${side} list with PHY ${spare} appended`, checks: ["lanes", "cable-errors"], tokens: [link.source, link.target], change: append });
      // A counter is injected only where the platform reads that owner's counters.
      const counted = nodes.get(end)?.kind !== "controller" || phyRows(nodes.get(end)).some((entry) => entry.counter_state);
      if (counted) out.push({ name: `cable ${index} ${side} list with PHY ${spare} appended, critical on it`, checks: ["cable-errors"], tokens: [end, `PHY ${spare}`], faulty: { owner: end, phy: spare }, injected: { phy: spare },
        change: (copy) => append(copy) && inject(copy, end, spare) });
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
  // An identifier on two rows identifies neither: a disk PHY's name given
  // to another row of its table, a CORE disk's device handle given to
  // another row of its expander, a disk port's SAS address reported for
  // another bay. A device type flipped to another entry of the table that
  // cannot be that subject: the subject's checks cannot pass on it.
  const census = fabric.raw?.sas_transport_ports || [];
  const ownerRows = (copy, ownerId) => tables(copy, ownerId)[0] || [];
  census.forEach((port, index) => {
    const disk = port.remote_kind === "end_device" && port.has_block;
    const uplink = port.remote_kind === "expander" && nodes.get(port.owner_id)?.kind === "controller";
    if (!disk && !uplink) return;
    // Only where something claims the PHY does a flip contradict it: a bay
    // joined to it, or a cable listing it. Unclaimed, the flip is new
    // evidence of another role, and the PHY is that role's subject.
    const claimed = (port.phys || []).some((phy) => (disk
      ? built.bays.some((bay) => bay.ports.some((item) => item.expanderId === port.owner_id && (item.phy?.phy === phy)))
      : built.links.some((link) => link.source === port.owner_id && link.lanes?.sourcePhys.includes(phy))));
    const checks = !claimed ? [] : disk ? ["port-errors"] : ["lanes", "cable-errors"];
    for (const phy of port.phys || []) {
      const row = phyRows(nodes.get(port.owner_id)).find((entry) => Number(entry.phy) === phy);
      if (row?.name && phyRows(nodes.get(port.owner_id)).some((entry) => Number(entry.phy) !== phy)) {
        out.push({ name: `census ${index} PHY ${phy} name on two rows`, checks: disk ? checks : [], tokens: [port.owner_id, `PHY ${phy}`], change: (copy) => {
          const other = ownerRows(copy, port.owner_id).find((entry) => Number(entry.phy) !== phy);
          other.name = row.name;
          return true;
        } });
      }
    }
    out.push({ name: `census ${index} kind flipped`, checks, tokens: [port.owner_id, ...(port.phys || []).map((phy) => `PHY ${phy}`)], change: (copy) => {
      copy.fabric.raw.sas_transport_ports[index].remote_kind = disk ? "expander" : "end_device";
      return true;
    } });
  });
  // CORE rows: each claimed row's device type flipped to the table's other
  // kinds it cannot be; each bay's handle given to another row.
  const FLIPS = {
    bay: ["SMP Target", "SAS Initiator", "SEP Target", "No Device", "Unknown Device"],
    "controller-expander:near": ["SAS Target", "SEP Target", "No Device", "Unknown Device"],
    "controller-expander:far": ["Edge Expander", "SAS Target", "SEP Target", "No Device", "Unknown Device"],
    "expander-expander:near": ["SAS Initiator", "SAS Target", "SEP Target", "No Device", "Unknown Device"],
    "expander-expander:far": ["SAS Initiator", "SAS Target", "SEP Target", "No Device", "Unknown Device"],
  };
  const coreRows = (copy, ownerId) => [...(copy.fabric.expanders || []).filter((entry) => entry.id === ownerId).map((entry) => entry.phys), ...(copy.fabric.controllers || []).filter((entry) => entry.id === ownerId).map((entry) => entry.phy_rows)].filter(Array.isArray).flat();
  const flip = (label, checks, ownerId, phy, devices) => {
    // Only a row that names its device type (CORE): Linux rows take theirs
    // from the census, flipped above.
    if (!coreRows({ fabric }, ownerId).some((entry) => Number(entry.phy) === phy && entry.device)) return;
    for (const device of devices) {
      out.push({ name: `${label} type flipped to ${device}`, checks, tokens: [ownerId, `PHY ${phy}`], change: (copy) => {
        const rows = coreRows(copy, ownerId).filter((entry) => Number(entry.phy) === phy && entry.device);
        for (const entry of rows) entry.device = device;
        return rows.length > 0;
      } });
    }
  };
  for (const link of fabric.links.filter((entry) => entry.reported_lanes && FLIPS[`${entry.kind}:near`])) {
    for (const phy of link.reported_lanes.source_phys || []) flip(`${link.id} near PHY ${phy}`, ["lanes", "cable-errors"], link.source, phy, FLIPS[`${link.kind}:near`]);
    for (const phy of link.reported_lanes.target_phys || []) flip(`${link.id} far PHY ${phy}`, ["lanes", "cable-errors"], link.target, phy, FLIPS[`${link.kind}:far`]);
  }
  for (const trace of fabric.traces.filter((entry) => entry.kind === "bay")) {
    (trace.metrics?.mpr_devices || []).forEach((device, index) => {
      const expander = (fabric.expanders || []).find((entry) => (device.expander_ids || []).includes(entry.id));
      const row = expander?.phys.find((entry) => device.handle && entry.dev_handle === device.handle);
      if (!row) return;
      flip(`${trace.id} port ${index} PHY ${row.phy}`, ["port-errors"], expander.id, Number(row.phy), FLIPS.bay);
      out.push({ name: `${trace.id} port ${index} handle on two rows`, checks: ["port-errors"], tokens: [trace.id], change: (copy) => {
        const other = copy.fabric.expanders.find((entry) => entry.id === expander.id).phys.find((entry) => entry.dev_handle !== device.handle);
        other.dev_handle = device.handle;
        return true;
      } });
    });
  }
  const addressed = fabric.traces.filter((entry) => entry.kind === "bay" && Object.values(entry.metrics || {}).filter(Array.isArray).flat().some((row) => row?.sas_address));
  addressed.forEach((trace, index) => {
    const from = addressed[(index + 1) % addressed.length];
    if (from === trace) return;
    const address = Object.values(from.metrics).filter(Array.isArray).flat().find((row) => row?.sas_address).sas_address;
    out.push({ name: `${trace.id} disk port address also ${from.id}'s`, checks: ["port-errors"], tokens: [trace.id], change: (copy) => {
      const metrics = copy.fabric.traces.find((entry) => entry.id === trace.id).metrics;
      for (const row of Object.values(metrics).filter(Array.isArray).flat()) if (row && typeof row === "object" && row.sas_address) row.sas_address = address;
      return true;
    } });
  });
  // A field blanked on one of two records of one subject: a second census
  // record of the same disk missing its slot, its enclosure or its address,
  // or the bay's own record missing the address its trace still reports.
  // What is missing is not read, never a second subject: the checks and
  // flags say the same.
  census.forEach((port, index) => {
    if (port.remote_kind !== "end_device" || !port.has_block) return;
    for (const field of ["bay_identifier", "enclosure_identifier", "remote_sas_address"]) {
      if (port[field] === null || port[field] === undefined) continue;
      // Without its address a record is the same disk only at a census slot
      // read whole: its bay number and its enclosure.
      if (field === "remote_sas_address" && (port.bay_identifier === null || port.bay_identifier === undefined || !port.enclosure_identifier)) continue;
      out.push({ name: `census ${index} second record without its ${field}`, same: true, checks: [], tokens: [], change: (copy) => {
        copy.fabric.raw.sas_transport_ports.push({ ...copy.fabric.raw.sas_transport_ports[index], id: `${port.id}-copy`, [field]: null });
        return true;
      } });
    }
  });
  // A second record of a claimed disk PHY that reads neither address nor
  // enclosure identifies no disk: the PHY's owner is not read, never the
  // record's bay number.
  census.forEach((port, index) => {
    if (port.remote_kind !== "end_device" || !port.has_block || port.bay_identifier === null || port.bay_identifier === undefined) return;
    if (!(port.phys || []).some((phy) => built.bays.some((bay) => bay.ports.some((item) => item.expanderId === port.owner_id && item.phy?.phy === phy)))) return;
    out.push({ name: `census ${index} unlocated second record`, checks: ["port-errors"], tokens: [port.owner_id, ...(port.phys || []).map((phy) => `PHY ${phy}`)], change: (copy) => {
      copy.fabric.raw.sas_transport_ports.push({ ...copy.fabric.raw.sas_transport_ports[index], id: `${port.id}-unlocated`, remote_sas_address: null, enclosure_identifier: null });
      return true;
    } });
  });
  // A bay whose own trace places it in another enclosure than its node:
  // the bay is placed in neither, and its disk port is not judged.
  for (const bay of fabric.nodes.filter((entry) => entry.kind === "bay" && /^[0-9a-f]{16}$/i.test(String(entry.metrics?.enclosure_id || "")))) {
    if (!built.bayById.get(bay.id)?.ports.some((port) => port.phy)) continue;
    out.push({ name: `${bay.id} trace in another enclosure`, checks: ["port-errors"], tokens: [bay.id], change: (copy) => {
      const trace = copy.fabric.traces.find((entry) => entry.id === bay.id);
      if (!trace) return false;
      trace.metrics.enclosure_id = `${bay.metrics.enclosure_id.slice(0, -2)}${bay.metrics.enclosure_id.endsWith("aa") ? "bb" : "aa"}`;
      return true;
    } });
  }
  for (const bay of fabric.nodes.filter((entry) => entry.kind === "bay" && entry.metrics?.sas_address)) {
    const trace = fabric.traces.find((entry) => entry.id === bay.id);
    if (!(trace?.metrics?.path_states || []).some((row) => row?.sas_address === bay.metrics.sas_address)) continue;
    out.push({ name: `${bay.id} node without the address its trace reports`, same: true, checks: [], tokens: [], change: (copy) => {
      copy.fabric.nodes.find((entry) => entry.id === bay.id).metrics.sas_address = null;
      return true;
    } });
  }
  // Reciprocal endpoint fields changed on one side of a CORE cable: the far
  // row's handle or remote PHY, the near HBA row's own handle, an expander
  // near row's handle or remote PHY. The lanes check cannot pass on it.
  for (const link of fabric.links.filter((entry) => entry.reported_lanes?.lanes?.length && (fabric.expanders || []).some((item) => item.id === entry.target))) {
    const [lane] = link.reported_lanes.lanes;
    const hba = (fabric.controllers || []).some((item) => item.id === link.source);
    const rowOf = (copy, id, phy) => coreRows(copy, id).find((entry) => Number(entry.phy) === phy);
    const sides = [["far", link.target, lane.target_phy, "dev_handle", "0ffe"], ["far", link.target, lane.target_phy, "remote_phy", String(lane.source_phy + 100)],
      hba ? ["near", link.source, lane.source_phy, "controller_handle", "0ffe"] : ["near", link.source, lane.source_phy, "dev_handle", "0ffe"],
      ...(hba ? [] : [["near", link.source, lane.source_phy, "remote_phy", String(lane.target_phy + 100)]])];
    for (const [side, id, phy, field, value] of sides) {
      if (!rowOf({ fabric }, id, phy)?.[field]) continue;
      out.push({ name: `${link.id} ${side} PHY ${phy} ${field} changed`, checks: ["lanes"], tokens: [link.source, link.target, `PHY ${phy}`], change: (copy) => {
        rowOf(copy, id, phy)[field] = value;
        return true;
      } });
    }
    // Evidence not read at one end never lets a retained list pass: the far
    // row's rate or the near row's rate unread, the near table or the far
    // row not read, the pairs not reported; nor do ends that name each other
    // but not as the far chip's parent, or rows another expander of the unit
    // shares.
    const farRows = (copy) => copy.fabric.expanders.find((entry) => entry.id === link.target).phys;
    const unread = [
      ["far rate not read", (copy) => { const row = rowOf(copy, link.target, lane.target_phy); if (!row?.speed) return false; row.speed = null; return true; }],
      ["near rate not read", (copy) => { const row = rowOf(copy, link.source, lane.source_phy); if (!row?.speed) return false; row.speed = null; return true; }],
      ["far row not read", (copy) => { const rows = farRows(copy); const at = rows.findIndex((entry) => Number(entry.phy) === lane.target_phy); if (at < 0) return false; rows.splice(at, 1); return true; }],
      ["pairs not reported", (copy) => { copy.fabric.links.find((entry) => entry.id === link.id).reported_lanes.lanes = []; return true; }],
      ["far parent not the near end", (copy) => { const far = copy.fabric.expanders.find((entry) => entry.id === link.target); far.parent = "0ffe"; return true; }],
      ["far row shared with another expander", (copy) => {
        const far = copy.fabric.expanders.find((entry) => entry.id === link.target);
        const other = copy.fabric.expanders.find((entry) => entry.id !== far.id && entry.controller_id === far.controller_id);
        const row = rowOf(copy, link.target, lane.target_phy);
        if (!other || !row) return false;
        other.phys.push({ ...row, phy: "90", invalid_dword_count: 0, counter_state: "measured", not_read: [] });
        return true;
      }],
    ];
    if (hba) unread.push(["near table not read", (copy) => { copy.fabric.controllers.find((entry) => entry.id === link.source).phy_rows = []; return true; }]);
    for (const [label, change] of unread) out.push({ name: `${link.id} ${label}`, checks: ["lanes"], tokens: [link.source, link.target], change });
    // The near HBA row's handle found to name the enclosure processor, a
    // disk, more than one device of the unit, or not read because a reply
    // was not.
    if (hba) {
      for (const state of ["sep", "end_device", "ambiguous", "mismatch", "not_read", "unlisted_expander"]) {
        out.push({ name: `${link.id} near handle found ${state}`, checks: ["lanes"], tokens: [link.source, link.target], change: (copy) => {
          const controller = copy.fabric.controllers.find((entry) => entry.id === link.source);
          controller.device_handle_states = { ...controller.device_handle_states, [String(lane.source_phy)]: state };
          return true;
        } });
      }
    }
  }
  // The whole evidence of each subject, not part of it (#1504): a lane
  // list shorter than the rows by which its two ends name each other, or
  // whose ends are rows of two controller units, is no cable's membership;
  // a speed blanked on either end leaves that lane's rate not read on every
  // surface.
  const unusedPhy = (rows) => {
    const used = new Set(rows.map((entry) => Number(entry.phy)));
    let phy = 80;
    while (used.has(phy)) phy += 1;
    return phy;
  };
  for (const link of fabric.links.filter((entry) => entry.reported_lanes?.lanes?.length && (fabric.expanders || []).some((item) => item.id === entry.target))) {
    const [lane] = link.reported_lanes.lanes;
    const rowOf = (copy, id, phy) => coreRows(copy, id).find((entry) => Number(entry.phy) === phy);
    const tableOf = (copy, id) => (copy.fabric.controllers || []).find((entry) => entry.id === id)?.phy_rows || (copy.fabric.expanders || []).find((entry) => entry.id === id)?.phys;
    const lengthen = (copy, id, phy, extra = {}) => {
      const rows = tableOf(copy, id);
      const row = rowOf(copy, id, phy);
      if (!rows || !row) return false;
      rows.push({ ...row, phy: String(unusedPhy(rows)), ...extra, ...(row.counter_state ? { counter_state: "measured", not_read: [], invalid_dword_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 } : {}) });
      return true;
    };
    const longer = {
      "near table longer than the list": (copy) => lengthen(copy, link.source, lane.source_phy),
      "far table longer than the list": (copy) => lengthen(copy, link.target, lane.target_phy, { remote_phy: String(unusedPhy(tableOf(copy, link.source) || [])) }),
    };
    for (const [label, change] of Object.entries(longer)) {
      out.push({ name: `${link.id} ${label}`, checks: ["lanes"], tokens: [link.source, link.target], change,
        expect: (after, why) => assert.match(textOf(checksOf(after, "lanes")), /which the lane list leaves out/, `${why}: the longer table is not said`) });
    }
    // The far chip a row of another controller unit: the same handles there
    // name another device.
    const far = (fabric.expanders || []).find((item) => item.id === link.target);
    const other = (fabric.controllers || []).find((item) => item.id !== far.controller_id);
    if (other) {
      out.push({ name: `${link.id} far end of another controller unit`, checks: ["lanes"], tokens: [link.source, link.target], change: (copy) => {
        copy.fabric.expanders.find((item) => item.id === link.target).controller_id = other.id;
        return true;
      }, expect: (after, why) => assert.match(textOf(checksOf(after, "lanes")), /another controller unit/, `${why}: the other unit is not said`) });
    }
    for (const [side, id, phy] of [["near", link.source, lane.source_phy], ["far", link.target, lane.target_phy]]) {
      out.push({ name: `${link.id} ${side} speed blanked`, checks: ["lanes"], tokens: [link.source, link.target], change: (copy) => {
        const row = rowOf(copy, id, phy);
        if (!row?.speed) return false;
        row.speed = null;
        return true;
      }, expect: (after, why) => {
        const drawn = after.links.flatMap((entry) => [entry, ...(entry.stubCables || [])]).find((entry) => entry.source === link.source && entry.target === link.target && entry.lanes);
        const value = (T.detailFor(after, { kind: "link", id: drawn.id })?.html || "").match(new RegExp(`Lane PHY ${lane.source_phy} ↔ ${lane.target_phy}</span><span[^>]*>([^<]*)`))?.[1] || "";
        assert.match(value, /^rate not read/, `${why}: the lane's rate is shown though one end's row reads none: ${value}`);
      } });
    }
  }
  // A list without pairs (sysfs reports a port on the parent only): a speed
  // blanked on one PHY it lists leaves the list's rate not read.
  for (const link of fabric.links.filter((entry) => LANE_KINDS.has(entry.kind) && entry.reported_lanes?.source_phys?.length && !entry.reported_lanes?.lanes?.length)) {
    const phy = link.reported_lanes.source_phys[0];
    const rowsOf = (copy) => phyRows(copy.fabric.nodes.find((entry) => entry.id === link.source)).filter((entry) => Number(entry.phy) === phy && entry.negotiated_rate_gbps !== null && entry.negotiated_rate_gbps !== undefined);
    if (!rowsOf({ fabric }).length) continue;
    out.push({ name: `${link.id} near PHY ${phy} speed blanked`, checks: ["lanes"], tokens: [link.source, link.target], change: (copy) => {
      for (const entry of rowsOf(copy)) Object.assign(entry, { negotiated_rate_gbps: null, negotiated_link_rate: null, speed: null });
      return true;
    } });
  }
  // One disk port address in two enclosures, joined to a claimed disk PHY's
  // own record through its slot: the address identifies no disk, so it
  // bridges nothing, and the PHY's fault is no bay's.
  census.forEach((port, index) => {
    if (port.remote_kind !== "end_device" || !port.has_block || !port.remote_sas_address || port.bay_identifier === null || port.bay_identifier === undefined || !port.enclosure_identifier) return;
    const claimed = (port.phys || []).filter((phy) => built.bays.some((bay) => bay.ports.some((item) => item.expanderId === port.owner_id && item.phy?.phy === phy)));
    if (!claimed.length) return;
    const counted = nodes.get(port.owner_id)?.kind !== "controller" || phyRows(nodes.get(port.owner_id)).some((entry) => entry.counter_state);
    const bridged = `${port.remote_sas_address.slice(0, -4)}f00d`;
    const elsewhere = `${port.enclosure_identifier.slice(0, -2)}${port.enclosure_identifier.endsWith("aa") ? "bb" : "aa"}`;
    const bridge = (copy) => {
      copy.fabric.raw.sas_transport_ports.push({ ...port, id: `${port.id}-foreign`, remote_sas_address: bridged, enclosure_identifier: elsewhere },
        { ...port, id: `${port.id}-bridge`, remote_sas_address: bridged });
      return true;
    };
    out.push({ name: `census ${index} address in two enclosures bridging its slot`, checks: ["port-errors"], tokens: [port.owner_id, ...claimed.map((phy) => `PHY ${phy}`)], change: bridge });
    if (counted) {
      out.push({ name: `census ${index} address in two enclosures bridging its slot, critical on it`, checks: ["port-errors"], tokens: [port.owner_id, `PHY ${claimed[0]}`],
        faulty: { owner: port.owner_id, phy: claimed[0] }, injected: { phy: claimed[0] }, change: (copy) => bridge(copy) && inject(copy, port.owner_id, claimed[0]),
        expect: (after, why) => {
          // A fabric that builds no check list says the fault on the PHY's own card (below).
          if (!CHECK_IDS(after).length) return;
          const lines = after.checks.filter((entry) => entry.status === "critical").flatMap((entry) => entry.lines).filter((line) => /10 loss of dword sync/.test(line) && namesPhy(line, claimed[0]));
          assert.ok(lines.length && lines.every((line) => !/^Bay \d+/.test(line) && /owner not read/.test(line)), `${why}: the fault is charged to a bay through an address that names no one place: ${textOf(lines)}`);
        } });
    }
  });
  // An HBA list not read beside the records that would name its handles
  // (the builder that saved the payload kept no per-PHY state): no handle of
  // that HBA names anything (coverageFirst, below, holds every surface).
  for (const controller of (fabric.controllers || []).filter((item) => (item.phy_rows || []).some((row) => row.device_handle && row.device_handle !== "0000"))) {
    for (const list of ["expanders", "devices", "enclosures"]) {
      out.push({ name: `${controller.id} ${list} list not read, no handle states`, checks: ["lanes"], tokens: [controller.id], change: (copy) => {
        const entry = copy.fabric.controllers.find((item) => item.id === controller.id);
        delete entry.device_handle_states;
        entry.mpr_lists = { expanders: "read", devices: "read", enclosures: "read", [list]: "not_read" };
        return true;
      } });
    }
    out.push({ name: `${controller.id} no list states at all`, checks: ["lanes"], tokens: [controller.id], change: (copy) => {
      const entry = copy.fabric.controllers.find((item) => item.id === controller.id);
      delete entry.device_handle_states;
      delete entry.mpr_lists;
      if (copy.fabric.raw?.controller_read?.lists) delete copy.fabric.raw.controller_read.lists[entry.name];
      return true;
    } });
  }
  // A CORE bay path's handle given to another device of its HBA: the
  // enclosure processor, or an expander.
  for (const trace of fabric.traces.filter((entry) => entry.kind === "bay")) {
    (trace.metrics?.mpr_devices || []).forEach((device, index) => {
      const controller = (fabric.controllers || []).find((entry) => entry.id === `controller:${device.controller}`);
      if (!controller || !device.handle || !(device.expander_ids || []).length) return;
      const enclosure = fabric.nodes.find((entry) => entry.kind === "mpr-enclosure" && entry.controller_id === controller.id);
      if (enclosure) out.push({ name: `${trace.id} port ${index} handle also the enclosure processor's`, checks: ["port-errors"], tokens: [trace.id], change: (copy) => {
        copy.fabric.nodes.find((entry) => entry.id === enclosure.id).raw.sep_handle = device.handle;
        return true;
      } });
      out.push({ name: `${trace.id} port ${index} handle on two devices of the unit`, checks: ["port-errors"], tokens: [trace.id], change: (copy) => {
        const entry = copy.fabric.controllers.find((item) => item.id === controller.id);
        entry.ambiguous_device_handles = [...(entry.ambiguous_device_handles || []), device.handle];
        return true;
      } });
      const expander = (fabric.expanders || []).find((entry) => !(device.expander_ids || []).includes(entry.id) && entry.id.startsWith(`expander:${controller.id}:`));
      if (expander) out.push({ name: `${trace.id} port ${index} handle also an expander's`, checks: ["port-errors"], tokens: [trace.id], change: (copy) => {
        copy.fabric.expanders.find((entry) => entry.id === expander.id).dev_handle = device.handle;
        return true;
      } });
    });
  }
  // Every list of records, reversed: whatever is said over a set of records
  // is said the same in any order.
  const reverseAll = (lists) => {
    let done = false;
    for (const items of lists) if (Array.isArray(items) && items.length > 1) { items.reverse(); done = true; }
    // Nothing to reverse is evidence already absent, counted as such.
    return done || true;
  };
  out.push({ name: "census records reversed", permuted: true, checks: [], tokens: [], change: (copy) => reverseAll([copy.fabric.raw?.sas_transport_ports]) });
  out.push({ name: "trace record lists reversed", permuted: true, checks: [], tokens: [], change: (copy) => reverseAll(copy.fabric.traces.flatMap((entry) => Object.values(entry.metrics || {}).filter(Array.isArray))) });
  out.push({ name: "PHY tables reversed", permuted: true, checks: [], tokens: [], change: (copy) => reverseAll([...copy.fabric.nodes.flatMap((entry) => [entry.raw?.phys, entry.raw?.phy_rows]), ...(copy.fabric.expanders || []).map((entry) => entry.phys), ...(copy.fabric.controllers || []).map((entry) => entry.phy_rows)]) });
  out.push({ name: "links reversed", permuted: true, checks: [], tokens: [], change: (copy) => reverseAll([copy.fabric.links]) });
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

// Every pinned cable, bus and bay agrees with the check on who owns an
// injected fault: only the owner the check names shows it as critical, and
// no surface does where the check says the owner is not read. A chip's own
// inspector is the chip's, and always may.
// Entities are decoded with "&amp;" last, so no text is decoded twice.
const unescapeHtml = (value) => value.replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", "\"").replaceAll("&amp;", "&");
const warnRows = (built, ref) => [...(T.detailFor(built, ref)?.html || "").matchAll(/<div class="kv-row topo-warn"><span[^>]*>([^<]*)<\/span><span[^>]*>([^<]*)<\/span>/g)]
  .map((match) => unescapeHtml(match[2])).filter((value) => /10 loss of dword sync/.test(value));
function surfacesAgree(built, line, why) {
  const disputed = /owner not read/.test(line);
  for (const link of built.links.filter((entry) => entry.bus || (entry.lanes && entry.kind !== "expander-group"))) {
    const shown = warnRows(built, { kind: "link", id: link.id });
    if (!shown.length) continue;
    const title = T.cardFor(built, { kind: "link", id: link.id })?.title || "";
    const allowed = !disputed && (link.bus ? /^Bay \d+/.test(line) : line.startsWith(`${title}: at `));
    assert.ok(allowed, `${why}: the pinned ${link.bus ? "bus" : "cable"} ${title} shows the fault the check says is ${disputed ? "owned by no one read" : "another's"}: ${line} | ${shown}`);
  }
  for (const bay of built.bays) {
    // The bay's hover is a surface too: each path row is one observation.
    const hover = (T.cardFor(built, { kind: "bay", id: bay.bayId })?.rows || []).filter((row) => row.warn === "critical" && /10 loss of dword sync/.test(row.value));
    assert.ok(hover.length <= 1 && (!hover.length || (!disputed && line.startsWith(bay.label))), `${why}: the hover of ${bay.label} shows ${hover.length} paths with a fault the check gives ${disputed ? "no one" : "another"}: ${line} | ${JSON.stringify(hover)}`);
    const shown = warnRows(built, { kind: "bay", id: bay.bayId });
    if (!shown.length) continue;
    assert.ok(!disputed && line.startsWith(bay.label), `${why}: the pinned ${bay.label} shows a fault the check does not give it: ${line} | ${shown}`);
  }
}

// Every rate any surface displays, bound to its surface and row: the
// cards and inspectors of every cable, bus, chip, HBA, disk port, bay and
// enclosure, and the drawn link labels. A rate is a negotiated or maximum
// rate in Gb/s, an end's maximum "(max N)", a lane's "(both ends N)" and a
// PHY's range "(PHY a–b)". Counters are not rates: they may change.
const RATE_TOKEN = /\b\d+(?:\.\d+)? Gb\/s|\(max \d+(?:\.\d+)?\)|\(both ends \d+(?:\.\d+)?\)|\(PHY \d+(?:\.\d+)?–\d+(?:\.\d+)?\)|PHY max \d+(?:\.\d+)?/g;
const COUNTER_TOKEN = /\b\d+(?:–\d+)? (?:invalid dword|running disparity|loss of dword sync|PHY reset problem)\b/g;
// The chip a name in a line names: every node's label, SAS address and
// address tail, by the chip it belongs to (its address, else its id). The
// longest name ending nearest the PHY is the one said.
// A name two chips share (a short address tail) names neither.
// A chip's key: its drawn node's SAS address, else the payload's, else its id.
function chipKey(built, fabric, id) {
  const drawn = built.nodeById.get(id) || built.nodes.find((entry) => (entry.rows || []).some((row) => row.id === id));
  return drawn?.address || built.nodeById.get(drawn?.mergedInto)?.address || (fabric?.nodes || []).find((entry) => entry.id === id)?.raw?.sas_address || drawn?.id || id;
}
function chipNames(built, fabric) {
  const keys = new Map();
  // Every chip of the payload, drawn or grouped into a stub, and every node drawn.
  const chips = (fabric?.nodes || []).filter((entry) => ["expander", "controller"].includes(entry.kind)).map((entry) => ({ id: entry.id, label: built.nodeById.get(entry.id)?.label, address: built.nodeById.get(entry.id)?.address || entry.raw?.sas_address || "", rawId: entry.raw_id }));
  for (const entry of [...built.nodes, ...chips]) {
    const key = chipKey(built, fabric, entry.id);
    const tails = entry.address ? [4, 5, 6, 7, 8].map((size) => `…${entry.address.slice(-size)}`) : [];
    for (const name of new Set([entry.label, entry.address, entry.rawId, ...tails].filter((value) => typeof value === "string" && value.length > 2))) keys.set(name, new Set([...(keys.get(name) || []), key]));
  }
  return [...keys].filter(([, found]) => found.size === 1).map(([name, found]) => [name, [...found][0]]).sort((left, right) => right[0].length - left[0].length);
}
function ownerBefore(names, line, at, after) {
  // The text a PHY is said of: back to the nearest list or clause break,
  // past any closed parenthetical (about something else), and a following
  // "at <name>".
  let prefix = line.slice(0, at);
  for (let previous = ""; previous !== prefix;) [previous, prefix] = [prefix, prefix.replace(/\([^()]*\)/g, "")];
  const window = prefix.slice(Math.max(...[";", "|", "(", ",", "·"].map((mark) => prefix.lastIndexOf(mark))) + 1);
  let best = null;
  for (const [name, key] of names) {
    const index = window.lastIndexOf(name);
    if (index >= 0 && (!best || index + name.length > best.end)) best = { end: index + name.length, key };
  }
  if (best) return best.key;
  const following = /^ at ([^:;,(|·]+)/.exec(after)?.[1]?.trim() || "";
  return names.find(([name]) => following === name || following.endsWith(name))?.[1] || "";
}
// Every rate and every counter any surface displays, each bound to its
// surface and row: cards and inspectors, drawn link labels, and every
// wiring check's lines and notes. A counter is bound to the PHY and the
// chip it is said of.
function surfaceTokens(built, fabric) {
  const names = chipNames(built, fabric);
  const refs = [...built.links.map((link) => ({ kind: "link", id: link.id })),
    ...built.nodes.map((entry) => ({ kind: "node", id: entry.id })),
    ...built.bays.map((bay) => ({ kind: "bay", id: bay.bayId })),
    ...built.bays.flatMap((bay) => bay.ports.map((port) => ({ kind: "port", id: port.key })))];
  const rates = {};
  const counters = {};
  // A disk port's surface (its card, its bay's rows) is about its own PHYs:
  // a counter there with no PHY named is said of those.
  const portPhys = (ports) => ports.flatMap((port) => (port.phy ? [port.phy.phy] : port.phys || [])).join(", ");
  const own = new Map([...built.bays.map((bay) => [`bay:${bay.bayId}`, portPhys(bay.ports)]), ...built.bays.flatMap((bay) => bay.ports.map((port) => [`port:${port.key}`, portPhys([port])]))]);
  const add = (key, value) => {
    const text = String(value ?? "");
    const found = text.match(RATE_TOKEN);
    if (found) rates[key] = [...(rates[key] || []), ...found];
    // A counter is bound to its surface and to the PHY it is said of (the
    // nearest "PHY n" before it in its line): a row's label can change with
    // the tier it is in (Warning, Critical) as a fault joins it.
    const surface = key.replace(/ (pin|card|lines|notes|drawn)( .*)?$/, "");
    const label = key.match(/ (?:pin|card) (.*)$/)?.[1] || "";
    for (const line of `${label ? `${label}: ` : ""}${text}`.split(" | ")) {
      let ref = own.get(surface) || "";
      let owner = "";
      // A lane's row names both of its ends ("PHY 0 ↔ 32").
      for (const match of line.matchAll(/(?<!\()PHY (\d+(?:–\d+)?(?:, \d+(?:–\d+)?)*)(?: ↔ (\d+(?:–\d+)?(?:, \d+(?:–\d+)?)*))?|(\b\d+(?:–\d+)? (?:invalid dword|running disparity|loss of dword sync|PHY reset problem)\b)/g)) {
        if (match[1]) {
          ref = [match[1], match[2]].filter(Boolean).join(", ");
          // A PHY with no chip of its own named continues the line's list.
          owner = ownerBefore(names, line, match.index, line.slice(match.index + match[0].length)) || owner;
        } else counters[surface] = [...(counters[surface] || []), `${ref}::${owner}::${match[3]}`];
      }
    }
  };
  for (const ref of refs) {
    const html = T.detailFor(built, ref)?.html || "";
    for (const match of html.matchAll(/<div class="kv-row[^"]*"[^>]*><span[^>]*>([^<]*)<\/span><span[^>]*>([^<]*)<\/span>/g)) add(`${ref.kind}:${ref.id} pin ${match[1]}`, match[2]);
    for (const row of T.cardFor(built, ref)?.rows || []) add(`${ref.kind}:${ref.id} card ${row.label}`, row.value);
  }
  for (const link of built.links) add(`link:${link.id} drawn`, (link.labelLines || []).join(" | "));
  // A check splits by tier (cable-errors, cable-errors:warning): its lines
  // and notes are one surface, whichever tier a line is in.
  for (const entry of built.checks) {
    add(`check:${entry.id.split(":")[0]} lines`, entry.lines.join(" | "));
    add(`check:${entry.id.split(":")[0]} notes`, (entry.notes || []).join(" | "));
  }
  for (const key of Object.keys(rates)) rates[key].sort();
  return { rates, counters, built };
}

// A counters-only injection on one PHY changes no rate anywhere, and no
// counter value but that PHY's: every counter any surface says is bound to
// the PHY and the chip it is said of, and only that PHY's may come or go.
// What comes is the injected count, or a range whose both ends are counts
// some row of the payload measured, spanning the injected one. On a check's
// lines and notes the chip is named; on a card, pin or label with no chip
// named, the surface is the injected chip's own (its node, a cable at it, a
// bay or port on it). A count said of no PHY never changes.
const covers = (ref, phy) => ref.split(", ").some((part) => {
  const [low, high = low] = part.split("–").map(Number);
  return phy >= low && phy <= high;
});
// The counts each chip measured, by PHY: every observation of the chip, by
// the same key the names use. A range is bound to the chip and PHYs it is
// said of, never to a count some other PHY read.
function measuredCounts(built, fabric, key) {
  const counts = new Map();
  const add = (id, rows) => {
    for (const row of Array.isArray(rows) ? rows : []) {
      if (!Number.isSafeInteger(row?.[key])) continue;
      const at = `${chipKey(built, fabric, id)}|${Number(row.phy)}`;
      counts.set(at, new Set([...(counts.get(at) || []), row[key]]));
    }
  };
  for (const entry of fabric.nodes) add(entry.id, entry.raw?.phys || entry.raw?.phy_rows);
  for (const entry of fabric.expanders || []) add(entry.id, entry.phys);
  for (const entry of fabric.controllers || []) add(entry.id, entry.phy_rows);
  return (chip, ref) => new Set(ref.split(", ").flatMap((part) => {
    const [low, high = low] = part.split("–").map(Number);
    return [...counts].filter(([at]) => at.startsWith(`${chip}|`) && Number(at.split("|").pop()) >= low && Number(at.split("|").pop()) <= high).flatMap(([, values]) => [...values]);
  }));
}
function injectedChip(before, base, owner) {
  return chipKey(before, base.fabric, owner);
}
function surfaceOfChip(built, fabric, surface, chip) {
  const keyOf = (id) => chipKey(built, fabric, id);
  const [kind, ...rest] = surface.split(":");
  const id = rest.join(":");
  if (kind === "node") return keyOf(id) === chip || (built.nodeById.get(id)?.rows || []).some((row) => keyOf(row.id) === chip || row.id === chip);
  if (kind === "link") {
    const link = built.links.flatMap((entry) => [entry, ...(entry.stubCables || [])]).find((entry) => entry.id === id);
    return Boolean(link) && [link.source, link.target].some((end) => keyOf(end) === chip);
  }
  const ports = kind === "bay" ? built.bayById.get(id)?.ports || [] : kind === "port" ? [built.portByKey.get(id)].filter(Boolean) : [];
  return ports.some((port) => keyOf(port.expanderId) === chip);
}
function assertLocal(before, after, base, copy, deletion, why) {
  assert.deepEqual(after.rates, before.rates, `${why}: a counter change moved a displayed rate`);
  const phy = deletion.faulty.phy;
  const chip = injectedChip(before.built, base, deletion.faulty.owner);
  const measured = measuredCounts(after.built, copy.fabric, "loss_of_dword_synchronization_count");
  const bag = (list) => list.reduce((map, token) => map.set(token, (map.get(token) || 0) + 1), new Map());
  for (const key of new Set([...Object.keys(before.counters), ...Object.keys(after.counters)])) {
    const was = bag(before.counters[key] || []);
    const now = bag(after.counters[key] || []);
    for (const [pair, count] of now) {
      if (count <= (was.get(pair) || 0)) continue;
      const [ref, owner, token] = pair.split("::");
      const range = /^(\d+)–(\d+) loss of dword sync$/.exec(token);
      const read = range ? measured(owner || chip, ref) : new Set();
      const value = token === "10 loss of dword sync" || (range && read.has(Number(range[1])) && read.has(Number(range[2])) && Number(range[1]) <= 10 && Number(range[2]) >= 10);
      const whose = owner ? owner === chip : !key.startsWith("check:") && surfaceOfChip(after.built, copy.fabric, key, chip);
      assert.ok(ref && covers(ref, phy) && value && whose, `${why}: ${key} gained ${token} said of ${ref ? `PHY ${ref}` : "no PHY"}${owner ? ` of ${owner}` : ""}, not ${chip}'s`);
    }
    for (const [pair, count] of was) {
      if (count <= (now.get(pair) || 0)) continue;
      const [ref, , token] = pair.split("::");
      assert.ok(ref && covers(ref, phy), `${why}: ${key} lost ${token} said of ${ref ? `PHY ${ref}` : "no PHY"}, not PHY ${phy}'s`);
    }
  }
}

// Two invariants of the whole-evidence rule, held on every built model.
// A lane's rate is shown (pin, the cable's per-rate count, the drawn label)
// only where both ends' own one rows read that rate; a list without pairs
// only where every PHY it lists reads it.
const LANE_KINDS = new Set(["controller-expander", "expander-expander"]);
function laneRatesBacked(built, why) {
  const table = (id) => built.sas.controllers.get(id) || built.sas.expanders.get(id);
  const reads = (id, phy, rate) => table(id)?.byPhy.get(phy)?.rate === rate;
  for (const link of built.links.flatMap((entry) => [entry, ...(entry.stubCables || [])]).filter((entry) => entry.lanes && LANE_KINDS.has(entry.kind))) {
    const lanes = link.lanes.rows;
    const backed = (rate) => (lanes.length ? lanes.filter((lane) => reads(link.source, lane.source, rate) && reads(link.target, lane.target, rate)).length
      : [[link.source, link.lanes.sourcePhys], [link.target, link.lanes.targetPhys]].filter(([, phys]) => phys.length).every(([id, phys]) => phys.every((phy) => reads(id, phy, rate))) ? link.lanes.count : 0);
    const html = unescapeHtml(T.detailFor(built, { kind: "link", id: link.id })?.html || "");
    for (const match of html.matchAll(/Lane PHY (\d+) ↔ (\d+)<\/span><span[^>]*>(\d+(?:\.\d+)?) Gb\/s/g)) {
      assert.ok(reads(link.source, Number(match[1]), Number(match[3])) && reads(link.target, Number(match[2]), Number(match[3])), `${why}: ${link.id} lane ${match[1]} ↔ ${match[2]} shows ${match[3]} Gb/s, which its ends' rows do not both read`);
    }
    const card = (T.cardFor(built, { kind: "link", id: link.id })?.rows || []).find((row) => row.label === "Lanes")?.value || "";
    for (const match of card.matchAll(/(\d+) at (\d+(?:\.\d+)?) Gb\/s/g)) assert.ok(backed(Number(match[2])) >= Number(match[1]), `${why}: ${link.id} card says ${match[0]}, ${backed(Number(match[2]))} lanes read it at both ends`);
    const label = /(\d+(?:\.\d+)?)(?:–(\d+(?:\.\d+)?))? Gb\/s$/.exec(link.labelLines?.[0] || "");
    if (label && !label[2]) assert.equal(backed(Number(label[1])), link.lanes.count, `${why}: ${link.id} is drawn at ${label[0]}, which not every lane's ends read`);
  }
}
// An HBA whose lists are not all read, or that reports no list states,
// names no device by any handle and calls none absent, on any surface.
function coverageFirst(built, why) {
  if (built.transport) return;
  for (const [id, record] of built.sas.controllers) {
    if (record.lists && ["expanders", "devices", "enclosures"].every((key) => record.lists[key] === "read")) continue;
    if (!built.nodeById.has(id)) continue;
    const pins = [...(T.detailFor(built, { kind: "node", id })?.html || "").matchAll(/<div class="kv-row[^"]*"[^>]*><span[^>]*>(PHY \d+)<\/span><span[^>]*>([^<]*)<\/span>/g)].map((match) => [match[1], unescapeHtml(match[2])]);
    for (const [label, value] of pins) {
      const row = record.byPhy.get(Number(label.slice(4)));
      if (!row?.handle) continue;
      assert.doesNotMatch(value, /attached directly|outside this view|^to |^cable to |^a disk the HBA's device list names|^the enclosure processor|^a device the HBA's device list names/, `${why}: ${id} ${label} names what its handle carries though its lists are not all read: ${value}`);
    }
    assert.doesNotMatch(textOf(T.cardFor(built, { kind: "node", id })?.rows || []), /attached directly|outside this view/, `${why}: ${id}'s card names a handle's device though its lists are not all read`);
  }
}

// What the checks and flags say, as sets: line order is presentation.
function canonicalChecks(built) {
  const checks = {};
  for (const entry of built.checks) checks[entry.id] = { status: entry.status, lines: [...entry.lines].sort(), notes: [...(entry.notes || [])].sort() };
  const flags = [...built.flags].map(([key, items]) => [key, items.map((item) => `${item.tier}:${item.text}`).sort()]).sort();
  return { checks, flags };
}

function runProperty(name, base, build) {
  const before = build(structuredClone(base));
  namesItsMeasurement(before, `${name}: baseline`);
  laneRatesBacked(before, `${name}: baseline`);
  coverageFirst(before, `${name}: baseline`);
  const ids = CHECK_IDS(before);
  const critical = new Set(ids.filter((id) => tierOfCheck(before, id).includes("critical")));
  const deletions = subjectDeletions(base.fabric, before);
  let rates = null;
  let compared = 0;
  const baselineRates = () => (rates ??= surfaceTokens(before, base.fabric));
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
    laneRatesBacked(after, why);
    coverageFirst(after, why);
    if (deletion.expect) deletion.expect(after, why);
    if (deletion.permuted) {
      assert.deepEqual(canonicalChecks(after), canonicalChecks(before), `${why}: the same records in another order say something else`);
      continue;
    }
    if (deletion.same) {
      assert.deepEqual(canonicalChecks(after), canonicalChecks(before), `${why}: a field missing from one record of a subject made it say something else`);
      continue;
    }
    if (deletion.orders) {
      const other = structuredClone(base);
      deletion.twin(other);
      assert.equal(textOf(build(other).checks), textOf(after.checks), `${why}: the two orders of the rows say different things`);
    }
    namesItsMeasurement(after, why);
    // Locality, on every counters-only injection, whatever the fabric: a
    // counter on one observation changes no rate on any surface.
    if (deletion.countersOnly) {
      assertLocal(baselineRates(), surfaceTokens(after, copy.fabric), base, copy, deletion, why);
      compared += 1;
    }
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
      surfacesAgree(after, lines[0], why);
      // Locality: a counter on one observation changes no displayed rate.
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
  return { proposed: deletions.length, applied, absent, compared, injected: deletions.filter((item) => item.countersOnly).length };
}

function directDisk() {
  const item = fixture("wide-x4-sata");
  const host = item.fabric.nodes.find((entry) => entry.id === "controller:host1");
  host.raw.phy_rows.push({ ...host.raw.phy_rows[0], name: "phy-1:12", phy: 12, not_read: [], counter_state: "measured", invalid_dword_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const id = "sas-port:port-1:12";
  item.fabric.raw.sas_transport_ports.push({ id, owner_id: host.id, state: "read", phy_names: ["phy-1:12"], phys: [12], remote_kind: "end_device", remote_id: null,
    remote_sas_address: "5eee001e00000123", has_block: true, bay_identifier: 7, enclosure_identifier: null, target_basis: "reported" });
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id: host.id, phy_names: ["phy-1:12"], state: "read" };
  Object.assign(item.fabric.nodes.find((entry) => entry.id === "bay:7").metrics, { present: true, device_name: "wdfresh", sas_address: "5eee001e00000123", transport_protocol: "SAS", target_port_protocol: "SSP" });
  const row = { controller: "host1", state: "unknown", device_name: "wdfresh", transport_protocol: "SAS", attached_sas_address: "5aaa000000000101", phy_identifier: 12, target_port_protocol: "SSP",
    sas_address: "5eee001e00000123", member_device_name: "wdfresh", bay_identifier: 7, port: "port-1:12", expander_ids: [], sas_transport_port_id: id, source: "linux_sas_transport" };
  Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:7").metrics, { sas_transport_ports: [row], path_states: [row] });
  return item;
}

test("property: on every synthetic fabric and the CORE fabric, every deleted subject is not read, named or counted, and nothing vanishes", (t) => {
  const report = {};
  for (const base of FIXTURES) report[base.id] = runProperty(base.id, base, (item) => model(item));
  report.core_cascade = runProperty("core_cascade", core(), (data) => coreModel(data));
  // A disk attached directly to an HBA PHY, mapped to a bay.
  report["wide-x4-sata+direct"] = runProperty("wide-x4-sata+direct", directDisk(), (item) => model(item));
  t.diagnostic(`applied/proposed per fabric (already absent): ${Object.entries(report).map(([id, { applied, proposed, absent }]) => `${id} ${applied}/${proposed}${absent ? ` (${absent})` : ""}`).join(", ")}`);
  // Locality is compared on every counters-only injection, no-check-list fabrics included.
  const sums = Object.values(report).reduce((total, entry) => [total[0] + entry.compared, total[1] + entry.injected], [0, 0]);
  t.diagnostic(`locality compared on ${sums[0]} of ${sums[1]} counters-only injections`);
  assert.equal(sums[0], sums[1]);
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
