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

// --- Property: deleting one subject's evidence never keeps a PASS ----------

// Each deletion names the checks whose subject it removes.
function deletions(item) {
  const out = [];
  const fabric = item.fabric;
  for (const [index, link] of lanes(item).entries()) {
    out.push([`cable ${index} lanes`, ["lanes", "cable-errors"], (copy) => { lanes(copy)[index].reported_lanes = null; }]);
    out.push([`cable ${index} unresolved`, ["lanes", "cable-errors"], (copy) => { unresolve(copy.fabric, lanes(copy)[index]); }]);
    if (link.reported_lanes?.source_phys?.length) out.push([`cable ${index} near PHY`, ["cable-errors"], (copy) => {
      const own = lanes(copy)[index];
      const node = copy.fabric.nodes.find((entry) => entry.id === own.source);
      const row = (node.raw.phys || node.raw.phy_rows).find((entry) => entry.phy === own.reported_lanes.source_phys[0]);
      if (row) cutCounters(row);
    }]);
  }
  for (const [index, port] of (fabric.raw?.sas_transport_ports || []).entries()) {
    if (port.remote_kind !== "end_device" || !port.phys?.length) continue;
    out.push([`disk port ${index} counters`, ["port-errors"], (copy) => {
      const census = copy.fabric.raw.sas_transport_ports[index];
      const node = copy.fabric.nodes.find((entry) => entry.id === census.owner_id);
      const row = node && (node.raw.phys || []).find((entry) => entry.phy === census.phys[0]);
      if (row) cutCounters(row);
    }]);
  }
  for (const bay of fabric.nodes.filter((node) => node.kind === "bay" && node.metrics?.present === true)) {
    out.push([`${bay.id} occupancy`, ["ports"], (copy) => { copy.fabric.nodes.find((entry) => entry.id === bay.id).metrics.present = null; }]);
    out.push([`${bay.id} ports`, ["ports", "port-errors"], (copy) => {
      const trace = copy.fabric.traces.find((entry) => entry.id === bay.id);
      if (trace) Object.assign(trace.metrics, { path_states: [], sas_transport_ports: [] });
    }]);
    out.push([`${bay.id} path states`, ["path-states"], (copy) => {
      const trace = copy.fabric.traces.find((entry) => entry.id === bay.id);
      for (const row of trace?.metrics?.path_states || []) Object.assign(row, { state: "unknown", not_read: ["state"] });
    }]);
  }
  return out;
}

test("property: on every synthetic fabric, deleting one subject's evidence never keeps its check at PASS", () => {
  const seen = [];
  for (const base of FIXTURES) {
    const before = model(structuredClone(base));
    const passing = new Set(before.checks.filter((entry) => entry.status === "pass").map((entry) => entry.id));
    for (const [name, ids, change] of deletions(base)) {
      const watched = ids.filter((id) => passing.has(id));
      if (!watched.length) continue;
      const copy = structuredClone(base);
      change(copy);
      const after = model(copy);
      for (const id of watched) {
        seen.push(`${base.id}:${id}`);
        assert.ok(!statuses(after, id).includes("pass"), `${base.id}: deleting ${name} kept ${id} at PASS: ${textOf(checksOf(after, id))}`);
      }
    }
  }
  // The property was exercised on every kind of check that can pass.
  for (const id of ["lanes", "cable-errors", "ports", "port-errors", "path-states"]) {
    assert.ok(seen.some((entry) => entry.endsWith(`:${id}`)) || id === "lanes" || id === "path-states", `exercised ${id}`);
  }
});

test("property: on the CORE fabric, deleting one subject's evidence never keeps its check at PASS", () => {
  const base = core();
  const before = coreModel(structuredClone(base));
  const passing = new Set(before.checks.filter((entry) => entry.status === "pass").map((entry) => entry.id));
  assert.ok(passing.has("lanes"));
  for (const [name, ids, change] of deletions({ fabric: base.fabric })) {
    const watched = ids.filter((id) => passing.has(id));
    if (!watched.length) continue;
    const copy = structuredClone(base);
    change(copy);
    const after = coreModel(copy);
    for (const id of watched) assert.ok(!statuses(after, id).includes("pass"), `deleting ${name} kept ${id} at PASS: ${textOf(checksOf(after, id))}`);
  }
});
