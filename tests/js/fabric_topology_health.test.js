"use strict";

// One health reading per physical PHY, one source for every surface (#1504).
// Each counter is judged on its own threshold; a PHY observation's tier is
// its worst read counter, and a counter that was read is never dropped
// because another was not. Where observations of one PHY via different HBAs
// disagree, the worst by severity stands and the disagreement is said. Chip
// cards, buses, cable ends, stubs, groups and checks all read the same
// reading. Every payload is synthetic: changes to the shipping producer's
// output, in the shapes it emits.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const T = H.loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;
const DIRECT = require("../fixtures/sas_fabric/core_direct_paths.json");

const fixture = (id) => structuredClone(FIXTURES.find((item) => item.id === id));
const model = (item) => T.buildTopologyModel(item.fabric, { ...item.options, width: 1150, enclosureHref: (id) => `view?enclosure=${id}` });
const direct = () => structuredClone(DIRECT);
const directModel = (data, options = {}) => T.buildTopologyModel(data.fabric, { inventory: data.snapshot, ...options });
const checksOf = (built, id) => built.checks.filter((entry) => entry.id === id || entry.id.startsWith(`${id}:`));
const statuses = (built, id) => JSON.parse(JSON.stringify(checksOf(built, id).map((entry) => entry.status)));
const textOf = (value) => JSON.stringify(value);
const plain = (value) => JSON.parse(JSON.stringify(value));
const row = (fabric, nodeId, phy) => (fabric.nodes.find((node) => node.id === nodeId).raw.phys || []).find((entry) => entry.phy === phy);
const HOST6 = "expander:controller:host6:expander-6:0";
const HOST7 = "expander:controller:host7:expander-7:0";
// The producer's shape for a PHY whose named counters were not read.
const unreadCounter = (entry, ...keys) => {
  for (const key of keys) entry[key] = null;
  entry.counter_state = "not_read";
  entry.not_read = [...new Set([...(entry.not_read || []), ...keys])];
};
const dual = (change) => {
  const item = fixture("dual-domain-linux");
  for (const phy of [0, 28]) change(row(item.fabric, HOST6, phy), row(item.fabric, HOST7, phy), phy);
  return { item, built: model(item) };
};
const sharedChip = (built) => built.nodes.find((entry) => entry.kind === "expander" && entry.rows?.length > 1);
const rowValue = (rows, pattern) => rows.find((entry) => pattern.test(entry.label));

// --- A: ranked by severity, never by total -------------------------------

test("A: a critical observation is never hidden by a larger warning total", () => {
  const { built } = dual((six, seven) => { six.invalid_dword_count = 99; seven.loss_of_dword_synchronization_count = 10; });
  for (const id of ["port-errors", "cable-errors"]) {
    const found = checksOf(built, id);
    assert.ok(statuses(built, id).includes("critical"), `${id}: ${textOf(found)}`);
    assert.match(textOf(found.filter((entry) => entry.status === "critical")), /10 loss of dword sync/, id);
    assert.match(textOf(found.filter((entry) => entry.status === "critical")), /disagree/, id);
  }
  // Control: the critical reading alone.
  const { built: alone } = dual((six, seven) => { seven.loss_of_dword_synchronization_count = 10; });
  for (const id of ["port-errors", "cable-errors"]) assert.ok(statuses(alone, id).includes("critical"), id);
});

// --- B: a read counter survives an unread sibling counter ------------------

test("B: a critical counter that was read stands beside a counter that was not", () => {
  const { built } = dual((six, seven) => { seven.loss_of_dword_synchronization_count = 10; unreadCounter(seven, "invalid_dword_count"); });
  for (const id of ["port-errors", "cable-errors"]) {
    const found = statuses(built, id);
    assert.ok(found.includes("critical"), `${id}: ${textOf(checksOf(built, id))}`);
    assert.ok(found.includes("not_read"), `${id}: the unread counter is still said: ${textOf(checksOf(built, id))}`);
  }
  // The faulty PHY is also counted as not read: a fault and a gap, both.
  assert.match(textOf(checksOf(built, "port-errors")), /counters not read for 1 disk port PHY\b/);
  assert.match(textOf(checksOf(built, "cable-errors")), /counters not read for 1 PHY at Expander/);
  // Control: the same unread counter beside a zero: not read only.
  const { built: zero } = dual((six, seven) => { unreadCounter(seven, "invalid_dword_count"); });
  for (const id of ["port-errors", "cable-errors"]) assert.deepEqual(statuses(zero, id).filter((status) => status !== "not_read"), [], id);
});

// --- C: every surface reads the same reading --------------------------------

test("C: the chip card, its bus, the cable end and the port card agree with the checks", () => {
  for (const mode of ["second HBA only", "only the second HBA has the PHY"]) {
    const copy = fixture("dual-domain-linux");
    for (const phy of [0, 28]) row(copy.fabric, HOST7, phy).loss_of_dword_synchronization_count = 10;
    if (mode !== "second HBA only") {
      const owner = copy.fabric.nodes.find((node) => node.id === HOST6);
      owner.raw.phys = owner.raw.phys.filter((entry) => entry.phy !== 0);
    }
    const use = model(copy);
    assert.ok(statuses(use, "port-errors").includes("critical"), `${mode}: ${textOf(checksOf(use, "port-errors"))}`);
    const chip = sharedChip(use);
    const card = T.cardFor(use, { kind: "node", id: chip.id }).rows;
    const errors = rowValue(card, /^PHY errors$/);
    assert.equal(errors.warn, "critical", `${mode}: ${textOf(errors)}`);
    assert.doesNotMatch(errors.value, /^none on/, mode);
    const via = rowValue(card, /Also read via/);
    if (via) assert.doesNotMatch(via.value, /same counters/, mode);
    if (chip.busId) {
      const bus = rowValue(T.cardFor(use, { kind: "link", id: chip.busId }).rows, /^PHY errors$/);
      assert.equal(bus.warn, "critical", `${mode}: bus ${textOf(bus)}`);
    }
    if (mode === "second HBA only") {
      const port = use.bays.flatMap((bay) => bay.ports).find((entry) => entry.phy?.phy === 0);
      const portErrors = rowValue(T.cardFor(use, { kind: "port", id: port.key }).rows, /^PHY errors$/);
      assert.equal(portErrors.warn, "critical", `${mode}: port ${textOf(portErrors)}`);
      const cable = use.links.find((link) => link.lanes && link.lanes.targetPhys.includes(28));
      const end = T.cardFor(use, { kind: "link", id: cable.id }).rows.find((entry) => /^Errors at Expander/.test(entry.label));
      assert.equal(end.warn, "critical", `cable end ${textOf(end)}`);
    }
  }
});

// --- D: a PHY's role is the union of what its observations say ---------------

test("D: a PHY one HBA's census names as a disk is a disk port, whichever HBA is first", () => {
  for (const [count, expected] of [[null, "not_read"], [101, "critical"]]) {
    const item = fixture("dual-domain-linux");
    const seven = row(item.fabric, HOST7, 18);
    const six = row(item.fabric, HOST6, 18);
    assert.ok(six && seven, "both observations report PHY 18");
    item.fabric.raw.sas_transport_ports.push({
      id: "sas-port:port-7:0:18", owner_id: HOST7, state: "read", phy_names: [seven.name], phys: [18],
      remote_kind: "end_device", remote_id: null, remote_sas_address: "5eee001a000000aa", has_block: true, bay_identifier: 99, enclosure_identifier: "5ddd0000000000f0", target_basis: "reported",
    });
    if (count === null) unreadCounter(seven, "invalid_dword_count");
    else seven.invalid_dword_count = count;
    const built = model(item);
    const found = checksOf(built, "port-errors");
    assert.ok(statuses(built, "port-errors").includes(expected), textOf(found));
    assert.doesNotMatch(textOf(found), /nothing reported/);
    assert.match(textOf(found), count === null ? /counters not read for 1 disk port PHY\b/ : /PHY 18, a disk port .*mapped to no bay/);
  }
});

// --- E: a group carries the worst of everything it hides ---------------------

test("E: a grouped neighbour carries the tier of a descendant it hides", () => {
  const item = fixture("fanout-5");
  const chips = item.fabric.nodes.filter((node) => node.kind === "expander");
  const [, parent, deep] = chips;
  const link = item.fabric.links.find((entry) => entry.kind === "expander-expander" && entry.target === deep.id);
  link.source = parent.id;
  link.id = `${link.kind}:${parent.id}->${deep.id}`;
  deep.raw.parent_id = parent.id;
  row(item.fabric, deep.id, 0).invalid_dword_count = 101;
  const built = model(item);
  assert.ok(statuses(built, "port-errors").includes("critical"));
  const group = built.nodes.find((entry) => entry.stubGroup);
  assert.ok(group, "the neighbours are grouped");
  assert.equal(group.groupTier, "critical");
  assert.doesNotMatch(group.facts.join(" "), /no findings/);
});

// --- F: a capped set is one gap of its true size ----------------------------

test("F: paths past the limit are a counted gap, never a ninth path or a missing HBA", () => {
  const data = direct();
  const trace = data.fabric.traces.find((entry) => entry.id === "bay:0");
  trace.metrics.path_states = Array.from({ length: 8 }, (_, index) => ({ controller: "mpr0", state: "active", device_name: `da${30 + index}` }));
  trace.metrics.mpr_devices = [];
  Object.assign(trace.metrics, { not_read: ["path_states"], not_read_reasons: { path_states: "path limit reached" }, not_read_counts: { path_states: 4 } });
  const built = directModel(data);
  const states = textOf(checksOf(built, "path-states"));
  assert.ok(!statuses(built, "path-states").includes("pass"), states);
  assert.match(states, /4 paths? (were )?not read/);
  assert.doesNotMatch(states, /of 9\b/);
  assert.doesNotMatch(textOf(checksOf(built, "ports")), /no path via mpr1/);
});

// --- G: no manufactured paths -----------------------------------------------

test("G: paths, PHYs and unresolved joins are counted apart", () => {
  const data = direct();
  const trace = data.fabric.traces.find((entry) => entry.id === "bay:0");
  Object.assign(trace.metrics.path_states.find((entry) => entry.device_name === "da11"), { controller: null, not_read: ["controller", "target"] });
  trace.metrics.mpr_devices = [];
  const found = textOf(checksOf(directModel(data), "port-errors"));
  assert.match(found, /\b2 disk paths\b/);
  assert.doesNotMatch(found, /\b3 disk paths\b/);
  // A Linux bay without its block join: the 16 PHYs read are 16, not "16 of 17".
  const linux = fixture("fanout-2");
  Object.assign(linux.fabric.traces.find((entry) => entry.id === "bay:0").metrics, { path_states: [], sas_transport_ports: [] });
  const lines = textOf(checksOf(model(linux), "port-errors"));
  assert.doesNotMatch(lines, /of 17\b/);
});

// --- H: a cascade passes only on resolved, distinct identities --------------

test("H: unresolved parents and one backplane id twice never pass the cascade check", () => {
  const item = fixture("dual-domain-linux");
  for (const link of item.fabric.links.filter((entry) => entry.kind === "controller-expander")) {
    item.fabric.links = item.fabric.links.filter((entry) => entry !== link);
    Object.assign(item.fabric.nodes.find((node) => node.id === link.target).raw, { parent_link_state: "unknown", parent_link_reason: "not_read", parent_id: null, parent_port: null, source_phys: [] });
  }
  const orphans = model(item);
  assert.ok(!statuses(orphans, "cascades").includes("pass"), textOf(checksOf(orphans, "cascades")));
  assert.doesNotMatch(textOf(checksOf(orphans, "cascades")), /every expander here is cabled to an HBA/);
  const same = fixture("cascade3-top");
  const [first, second] = model(same).nodes.filter((entry) => entry.neighbour);
  for (const port of same.fabric.raw.sas_transport_ports) if (port.owner_id === second.id && port.remote_kind === "end_device") port.enclosure_identifier = first.neighbour.enclosureId;
  const built = model(same);
  const ids = built.nodes.filter((entry) => entry.neighbour).map((entry) => entry.neighbour.enclosureId);
  assert.equal(new Set(ids).size, 1, "both neighbours resolve to one enclosure");
  assert.ok(!statuses(built, "cascades").includes("pass"), textOf(checksOf(built, "cascades")));
});

// --- I: a port with no board highlights only itself -------------------------

test("I: a port trace off any board reaches only its own route, never its siblings", () => {
  const data = direct();
  data.fabric.nodes = data.fabric.nodes.filter((node) => !["mpr-enclosure", "enclosure"].includes(node.kind));
  const ids = new Set(data.fabric.nodes.map((node) => node.id));
  data.fabric.links = data.fabric.links.filter((link) => ids.has(link.source) && ids.has(link.target));
  const built = directModel(data);
  const bay = built.bayById.get("bay:0");
  assert.ok(bay.ports.length > 1);
  for (const port of bay.ports) {
    const path = T.tracePath(built, { kind: "port", id: port.key });
    assert.deepEqual(plain([...path.ports]), [port.key], port.device);
    const others = bay.ports.filter((other) => other.controllerId !== port.controllerId).map((other) => other.controllerId);
    for (const id of others) assert.ok(!path.nodes.has(id), `${port.device} reaches ${id}`);
  }
});

// --- J: unresolved controller ids never join by label ------------------------

test("J: a path whose controller id is not in the graph never joins another id's row by its label", () => {
  const data = direct();
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  const state = metrics.path_states[0];
  const device = metrics.mpr_devices[1];
  Object.assign(state, { controller: "mpr8", controller_label: "mpr9", device_name: "da3" });
  Object.assign(device, { controller: "mpr9", member_device_name: null, expander_ids: [], enclosure_id: null, physical_enclosure_id: null });
  metrics.mpr_devices = [device];
  metrics.path_states = [state];
  const port = directModel(data).bays[0].ports.find((entry) => entry.device === "da3");
  assert.equal(port.sasAddress, "");
  assert.equal(port.handle, "");
});

function fakeDom() {
  const handlers = new Map();
  const pinText = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pinText : null), querySelectorAll: () => [] };
  const container = { dataset: {}, querySelector: (selector) => (selector === ".fabric-topology" ? root : null), addEventListener: (name, handler) => handlers.set(name, handler) };
  const element = (dataset) => ({ dataset, closest(selector) { return selector === ".fabric-topology" ? root : /\[data-topo-/.test(selector) ? this : null; } });
  return { handlers, container, search: { value: "", addEventListener: () => {} }, document: { addEventListener: () => {} }, element };
}

test("J: a pin does not follow a path whose unresolved controller id changed", () => {
  const dom = fakeDom();
  const api = H.loadTopology({ document: dom.document });
  const data = direct();
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  metrics.mpr_devices = [];
  metrics.path_states = [Object.assign(metrics.path_states[0], { controller: "mpr8", controller_label: "IO", device_name: "da3" })];
  api.attach(dom.container, dom.search, { textContent: "" });
  const options = { inventory: data.snapshot, selection: null };
  api.render(data.fabric, options);
  dom.handlers.get("click")({ target: dom.element({ topoPort: "bay:0#0" }) });
  assert.ok(api.view().pin);
  metrics.path_states[0].controller = "mpr9";
  api.render(data.fabric, options);
  assert.equal(api.view().pin, null);
});

// --- K: bad and partly unread is both ---------------------------------------

test("K: a cable end with a fault and an unread PHY is both, and never checked at both ends", () => {
  for (const mode of ["one end", "two observations"]) {
    const item = fixture("dual-domain-linux");
    if (mode === "one end") {
      row(item.fabric, HOST6, 28).invalid_dword_count = 101;
      unreadCounter(row(item.fabric, HOST6, 29), "invalid_dword_count");
    } else {
      for (const phy of [0, 28]) {
        row(item.fabric, HOST6, phy).loss_of_dword_synchronization_count = 10;
        unreadCounter(row(item.fabric, HOST7, phy), "invalid_dword_count");
      }
    }
    const built = model(item);
    const found = checksOf(built, "cable-errors");
    assert.ok(statuses(built, "cable-errors").includes("critical"), `${mode}: ${textOf(found)}`);
    assert.ok(statuses(built, "cable-errors").includes("not_read"), `${mode}: ${textOf(found)}`);
    assert.doesNotMatch(textOf(found), /checked at both of its ends/, mode);
    // The cable card says the same of the end: the fault, and the PHY not read.
    const cable = built.links.find((link) => link.lanes && link.lanes.targetPhys.includes(28));
    const end = T.cardFor(built, { kind: "link", id: cable.id }).rows.find((entry) => /^Errors at Expander/.test(entry.label));
    assert.equal(end.warn, "critical", `${mode}: ${textOf(end)}`);
    assert.match(end.value, /1 PHY not read/, `${mode}: ${textOf(end)}`);
  }
});

// --- L: an SES slot mapping is not a path-state read -------------------------

test("L: slot mappings are said as mappings, never a passing path-state check", () => {
  const built = model(fixture("empty-survey"));
  const found = checksOf(built, "path-states");
  assert.ok(!statuses(built, "path-states").includes("pass"), textOf(found));
  assert.match(textOf(found), /mapped/);
});

// --- Boundary cases ----------------------------------------------------------

test("boundary: a cable's expected width is its lane count, not its shorter member list", () => {
  for (const fault of [false, true]) {
    const item = fixture("fanout-2");
    const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
    const hba = item.fabric.nodes.find((node) => node.id === uplink.source);
    const phy = uplink.reported_lanes.source_phys.pop();
    assert.equal(uplink.reported_lanes.lane_count, uplink.reported_lanes.source_phys.length + 1);
    if (fault) hba.raw.phy_rows.find((entry) => entry.phy === phy).invalid_dword_count = 101;
    const built = model(item);
    assert.ok(!statuses(built, "cable-errors").includes("pass"), `fault=${fault}: ${textOf(checksOf(built, "cable-errors"))}`);
  }
});

test("boundary: two bays claiming one PHY leave its owner not read, and its fault is still said", () => {
  const item = fixture("fanout-2");
  const own = item.fabric.traces.find((entry) => entry.id === "bay:0").metrics.sas_transport_ports[0];
  const other = item.fabric.traces.find((entry) => entry.id === "bay:1");
  for (const entry of [...other.metrics.sas_transport_ports, ...other.metrics.path_states]) entry.sas_transport_port_id = own.sas_transport_port_id;
  const start = model(item);
  const port = start.bays[0].ports[0];
  row(item.fabric, port.expanderId, port.phy.phy).invalid_dword_count = 101;
  const built = model(item);
  assert.ok(!statuses(built, "ports").includes("pass"), textOf(checksOf(built, "ports")));
  const errors = checksOf(built, "port-errors");
  assert.ok(statuses(built, "port-errors").includes("critical"), textOf(errors));
  const critical = textOf(errors.filter((entry) => entry.status === "critical"));
  assert.doesNotMatch(critical, /^\["Bay 01/);
  assert.match(critical, /which (bay|disk) .*not read|claimed by/);
});

test("boundary: a bay that may hold a disk is a subject of the disk-port and path-state checks", () => {
  const item = fixture("fanout-2");
  item.fabric.nodes.find((node) => node.id === "bay:0").metrics.present = null;
  Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:0").metrics, { path_states: [], sas_transport_ports: [] });
  assert.ok(!statuses(model(item), "port-errors").includes("pass"), textOf(checksOf(model(item), "port-errors")));
  const data = direct();
  data.fabric.nodes.find((node) => node.id === "bay:1").metrics.present = null;
  assert.ok(!statuses(directModel(data), "path-states").includes("pass"), textOf(checksOf(directModel(data), "path-states")));
});

test("boundary: a neighbour with no census rows of its own has bays not read, never 0 disks", () => {
  const item = fixture("fanout-2");
  const leaf = item.fabric.nodes.filter((node) => node.kind === "expander")[1];
  item.fabric.raw.sas_transport_ports = item.fabric.raw.sas_transport_ports.filter((port) => port.owner_id !== leaf.id);
  const stub = model(item).nodeById.get(leaf.id);
  assert.equal(stub.neighbour.disks, null);
  assert.doesNotMatch(stub.facts.join(" "), /\b0 disks\b/);
  assert.match(stub.facts.join(" "), /disks not read/);
});

// --- F9: per-port address oracle ---------------------------------------------

test("F9: each port card shows its own path's address, never a sibling path's", () => {
  const cases = [["core_direct_paths", direct(), directModel], ...FIXTURES.map((entry) => [entry.id, structuredClone(entry), model])];
  let checked = 0;
  for (const [name, data, build] of cases) {
    const built = build(data);
    for (const bay of built.bays) {
      const trace = data.fabric.traces.find((entry) => entry.id === bay.bayId);
      for (const port of bay.ports) {
        const value = T.cardFor(built, { kind: "port", id: port.key }).rows.find((entry) => entry.label === "Port SAS address")?.value || "";
        if (!/^[0-9a-f]{16}$/.test(value)) continue;
        // The port's own rows: its path state and the MPR row naming its member.
        const rows = [...(trace?.metrics?.path_states || []), ...(trace?.metrics?.sas_transport_ports || []), ...(trace?.metrics?.mpr_devices || [])]
          .filter((entry) => (entry.device_name || entry.member_device_name || "") === port.device);
        const own = new Set(rows.map((entry) => String(entry.sas_address || "").toLowerCase().replace(/^0x/, "")).filter(Boolean));
        assert.ok(own.has(value), `${name} ${port.key} (${port.device}): ${value} is not its own address (${[...own]})`);
        checked += 1;
      }
    }
  }
  assert.ok(checked >= 2);
});
