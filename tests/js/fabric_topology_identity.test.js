"use strict";

// No reading is merged by key collision, synthesized, or attributed twice
// (#1504). Two rows that claim one PHY of one expander are both kept: the
// worst measured value stands and which port the PHY is is not read. The
// subjects a check expects come from the topology, not from the rows that
// arrived: a whole missing observation is not read, a repeated lane member
// never stands in for a missing one. Ranges use only counters that were
// read. One attribution of a PHY to its bay serves every surface. Every
// payload is synthetic, in the producer's shapes.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const T = H.loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;
const DIRECT = require("../fixtures/sas_fabric/core_direct_paths.json");

const fixture = (id) => structuredClone(FIXTURES.find((item) => item.id === id));
const model = (item) => T.buildTopologyModel(item.fabric, { ...item.options, width: 1150, enclosureHref: (id) => `view?enclosure=${id}` });
const checksOf = (built, id) => built.checks.filter((entry) => entry.id === id || entry.id.startsWith(`${id}:`));
const statuses = (built, id) => JSON.parse(JSON.stringify(checksOf(built, id).map((entry) => entry.status)));
const textOf = (value) => JSON.stringify(value);
const node = (fabric, id) => fabric.nodes.find((entry) => entry.id === id);
const row = (fabric, id, name) => node(fabric, id).raw.phys.find((entry) => entry.name === name);
const cardRow = (built, ref, pattern) => T.cardFor(built, ref).rows.find((entry) => pattern.test(entry.label));
const unread = (entry, ...keys) => {
  for (const key of keys) entry[key] = null;
  entry.counter_state = "not_read";
  entry.not_read = [...new Set([...(entry.not_read || []), ...keys])];
};
const X4 = "expander:controller:host1:expander-1:0";
const HOST6 = "expander:controller:host6:expander-6:0";
const HOST7 = "expander:controller:host7:expander-7:0";

// --- 1: duplicate PHY identity ----------------------------------------------

test("two rows claiming one PHY of one expander keep the measured fault and leave the PHY's port not read", () => {
  for (const order of ["as read", "reversed"]) {
    const item = fixture("wide-x4-sata");
    row(item.fabric, X4, "phy-1:0:0").invalid_dword_count = 101;
    row(item.fabric, X4, "phy-1:0:1").phy = 0;
    if (order === "reversed") node(item.fabric, X4).raw.phys.reverse();
    const built = model(item);
    const found = checksOf(built, "port-errors");
    assert.ok(statuses(built, "port-errors").includes("critical"), `${order}: ${textOf(found)}`);
    assert.match(textOf(found), /two rows|reported twice|more than one row/, order);
    const chip = built.nodes.find((entry) => entry.kind === "expander" && !entry.mergedInto && built.sas.expanders.has(entry.id) && entry.id === X4) || built.nodes.find((entry) => entry.id === X4);
    const errors = cardRow(built, { kind: "node", id: chip.id }, /^PHY errors$/);
    assert.equal(errors.warn, "critical", `${order}: ${textOf(errors)}`);
    assert.doesNotMatch(errors.value, /^none on/);
  }
  // Control: distinct identities: CRITICAL on Bay 00.
  const control = fixture("wide-x4-sata");
  row(control.fabric, X4, "phy-1:0:0").invalid_dword_count = 101;
  assert.match(textOf(checksOf(model(control), "port-errors")), /Bay 00/);
});

// --- 2: expected subjects from the topology ---------------------------------

test("a whole observation row missing is not read, as a row with missing counters is", () => {
  const cases = {
    "counter missing": (seven) => unread(seven, "invalid_dword_count"),
    "row missing": (seven, item) => { node(item.fabric, HOST7).raw.phys = node(item.fabric, HOST7).raw.phys.filter((entry) => entry !== seven); },
  };
  const control = fixture("dual-domain-linux");
  assert.ok(statuses(model(control), "cable-errors").includes("pass"));
  for (const [name, change] of Object.entries(cases)) {
    const item = fixture("dual-domain-linux");
    change(row(item.fabric, HOST7, "phy-7:0:28"), item);
    const built = model(item);
    const found = checksOf(built, "cable-errors");
    assert.ok(!statuses(built, "cable-errors").includes("pass"), `${name}: ${textOf(found)}`);
    assert.doesNotMatch(textOf(found), /checked at both of its ends/, name);
    const chip = built.nodes.find((entry) => entry.kind === "expander" && entry.rows?.length > 1);
    const via = cardRow(built, { kind: "node", id: chip.id }, /Also read via/);
    assert.doesNotMatch(via.value, /same counters/, `${name}: ${via.value}`);
  }
});

test("a repeated lane member never stands in for a missing one; lane count and members disagreeing is not read", () => {
  const repeated = fixture("fanout-2");
  const uplink = repeated.fabric.links.find((link) => link.kind === "controller-expander");
  const hba = node(repeated.fabric, uplink.source);
  uplink.reported_lanes.source_phys[7] = 6;
  hba.raw.phy_rows.find((entry) => entry.phy === 7).invalid_dword_count = 101;
  let built = model(repeated);
  assert.ok(!statuses(built, "cable-errors").includes("pass"), textOf(checksOf(built, "cable-errors")));
  let end = cardRow(built, { kind: "link", id: uplink.id }, /^Errors at host/);
  assert.doesNotMatch(end.value, /none on 8 PHYs/);
  const wide = fixture("fanout-2");
  const link = wide.fabric.links.find((entry) => entry.kind === "controller-expander");
  link.reported_lanes.lane_count = 7;
  built = model(wide);
  assert.ok(!statuses(built, "cable-errors").includes("pass"), textOf(checksOf(built, "cable-errors")));
  assert.match(textOf(checksOf(built, "cable-errors")), /7 lanes.*8 distinct|8 distinct.*7 lanes/);
  end = cardRow(built, { kind: "link", id: link.id }, /^(Membership|PHYs)$/);
  assert.ok(T.cardFor(built, { kind: "link", id: link.id }).rows.some((entry) => /not read/.test(entry.value) && /lane/i.test(entry.value)), textOf(T.cardFor(built, { kind: "link", id: link.id }).rows));
});

// --- 3: no synthesized value -------------------------------------------------

test("a range uses only counters that were read, and names the ones that were not", () => {
  const item = fixture("wide-x4-sata");
  for (const phy of [16, 17, 18, 19]) {
    const entry = node(item.fabric, X4).raw.phys.find((candidate) => candidate.phy === phy);
    entry.loss_of_dword_synchronization_count = 10;
    if (phy === 16) unread(entry, "invalid_dword_count");
    else entry.invalid_dword_count = 17;
  }
  const built = model(item);
  const text = textOf(built.checks) + textOf(built.nodes.map((entry) => T.cardFor(built, { kind: "node", id: entry.id })?.rows));
  assert.doesNotMatch(text, /0–17 invalid dword/);
  assert.match(text, /17 invalid dword/);
  assert.match(text, /invalid dword not read|1 not read/);
  // The transport note counts a PHY read in part as read in part.
  assert.doesNotMatch(built.transportNote.measured.join(" "), /\(1 return none\)/);
  assert.match(built.transportNote.measured.join(" "), /in part/);
});

test("the transport note counts every observation, as the chip does", () => {
  const item = fixture("dual-domain-linux");
  for (const name of ["phy-7:0:0", "phy-7:0:28"]) unread(row(item.fabric, HOST7, name), "invalid_dword_count");
  const line = model(item).transportNote.measured.join(" ");
  assert.doesNotMatch(line, /read on (\d+) of \1\b/, line);
});

// --- 4: one attribution ------------------------------------------------------

test("a PHY two bays claim has its owner not read on every surface", () => {
  const item = fixture("fanout-2");
  const own = item.fabric.traces.find((entry) => entry.id === "bay:0").metrics.sas_transport_ports[0];
  const other = item.fabric.traces.find((entry) => entry.id === "bay:1");
  for (const entry of [...other.metrics.sas_transport_ports, ...other.metrics.path_states]) entry.sas_transport_port_id = own.sas_transport_port_id;
  const start = model(item);
  const port = start.bays[0].ports[0];
  node(item.fabric, port.expanderId).raw.phys.find((entry) => entry.phy === port.phy.phy).invalid_dword_count = 101;
  const built = model(item);
  const chip = built.nodeById.get(port.expanderId);
  const chipErrors = cardRow(built, { kind: "node", id: chip.id }, /^PHY errors$/).value;
  assert.doesNotMatch(chipErrors, /\(Bay 0[01]\)/, chipErrors);
  assert.match(chipErrors, /owner not read/);
  const busErrors = cardRow(built, { kind: "link", id: chip.busId }, /^PHY errors$/).value;
  assert.doesNotMatch(busErrors, /^Bay 0[01] PHY/, busErrors);
  assert.match(busErrors, /owner not read/);
  for (const bay of built.bays.slice(0, 2)) assert.doesNotMatch(bay.extra.join(" "), /101 invalid dword/, `${bay.label}: ${bay.extra.join(" | ")}`);
});

// --- 5: counts match their labels --------------------------------------------

test("mapped, unmapped and empty SES slots are counted apart", () => {
  const item = fixture("empty-survey");
  for (const id of ["bay:0", "bay:1"]) item.fabric.traces.find((entry) => entry.id === id).metrics.path_states[0].state = "unmapped";
  node(item.fabric, "bay:3").metrics.present = false;
  const text = textOf(checksOf(model(item), "path-states"));
  assert.doesNotMatch(text, /[34] slots mapped/);
  assert.match(text, /1 slot mapped/);
  assert.match(text, /2 slots unmapped/);
  assert.match(text, /1 .*empty/);
});

test("placement past the cap says what was retained and what is missing, never no port row for a read one", () => {
  const data = structuredClone(DIRECT);
  const trace = data.fabric.traces.find((entry) => entry.id === "bay:0");
  const own = trace.metrics.mpr_devices.find((entry) => entry.member_device_name === "da3");
  trace.metrics.path_states = [{ controller: "mpr0", state: "active", device_name: "da3" }, ...Array.from({ length: 7 }, (_, index) => ({ controller: "mpr0", state: "active", device_name: `da${30 + index}` }))];
  trace.metrics.mpr_devices = [own];
  Object.assign(trace.metrics, { not_read: ["path_states"], not_read_reasons: { path_states: "path limit reached" }, not_read_counts: { path_states: 4 } });
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  const text = textOf(checksOf(built, "ports"));
  assert.match(text, /8 of 12 paths/);
  assert.doesNotMatch(text, /no port rows? for this disk/);
});

test("a bay whose occupancy is not read is said as a bay, never a disk", () => {
  const item = fixture("fanout-2");
  node(item.fabric, "bay:0").metrics.present = null;
  Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:0").metrics, { path_states: [], sas_transport_ports: [] });
  const text = textOf(checksOf(model(item), "port-errors"));
  assert.match(text, /1 bay with occupancy not read/);
  assert.doesNotMatch(text, /1 disk not read/);
});
