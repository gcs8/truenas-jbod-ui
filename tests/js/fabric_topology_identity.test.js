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
  // Its distinct members, and the repeat, are said.
  assert.match(textOf(checksOf(built, "cable-errors")), /8 lanes reported, 7 distinct PHYs listed at host2 \(1 repeated\)/);
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

test("a disk target its expander reports, matched to no bay and in no census, is a disk port subject", () => {
  // CORE has no transport census: the expander's own PHY table names the
  // disk target. Its counters not read make the disk-port check not read.
  const data = structuredClone(DIRECT);
  const expander = data.fabric.expanders[0];
  expander.phys.push({ ...expander.phys[0], phy: "4", device: "SAS Target", dev_handle: "0030", counter_state: "not_read" });
  const found = checksOf(T.buildTopologyModel(data.fabric, { inventory: data.snapshot }), "port-errors");
  assert.match(textOf(found), /\b1 of 1 disk port PHY not read\b/, textOf(found));
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

test("a fault line names the counters its PHY did not read", () => {
  const item = fixture("dual-domain-linux");
  const seven = row(item.fabric, HOST7, "phy-7:0:0");
  seven.loss_of_dword_synchronization_count = 10;
  unread(seven, "invalid_dword_count");
  const critical = checksOf(model(item), "port-errors").filter((entry) => entry.status === "critical");
  assert.match(textOf(critical), /10 loss of dword sync, invalid dword not read/);
});

test("a disk port the census lists with no row in its expander's table is said so", () => {
  const item = fixture("fanout-2");
  const disk = item.fabric.raw.sas_transport_ports.find((port) => port.remote_kind === "end_device" && port.has_block && port.owner_id.endsWith("expander-2:1"));
  const leaf = node(item.fabric, disk.owner_id);
  leaf.raw.phys = leaf.raw.phys.filter((entry) => entry.phy !== disk.phys[0]);
  const found = checksOf(model(item), "port-errors");
  assert.match(textOf(found), new RegExp(`PHY ${disk.phys[0]}, a disk port the transport census lists, has no row`));
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
  // Placement names both claimants on each bay's line.
  const placement = checksOf(built, "ports").flatMap((entry) => entry.lines).filter((line) => /^Bay 0[01]:/.test(line));
  assert.equal(placement.length, 2, textOf(placement));
  for (const line of placement) assert.match(line, /owner not read: Bay 00 and Bay 01 all claim it/, line);
  for (const bay of built.bays.slice(0, 2)) {
    assert.doesNotMatch(bay.extra.join(" "), /101 invalid dword/, `${bay.label}: ${bay.extra.join(" | ")}`);
    const portErrors = cardRow(built, { kind: "port", id: bay.ports[0].key }, /^PHY errors$/);
    assert.match(portErrors.value, /owner not read/, `${bay.label}: ${portErrors.value}`);
  }
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
  // Every path of a reason is counted under it: seven without a port row.
  assert.match(text, /not read for 7 \(da30/);
  // da3's own row names its expander: only its PHY is not read.
  assert.match(text, /the expander or HBA PHY of 1 disk port is not read/);
});

test("a bay whose occupancy is not read is said as a bay, never a disk", () => {
  const item = fixture("fanout-2");
  node(item.fabric, "bay:0").metrics.present = null;
  Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:0").metrics, { path_states: [], sas_transport_ports: [] });
  const text = textOf(checksOf(model(item), "port-errors"));
  assert.match(text, /1 bay with occupancy not read/);
  assert.doesNotMatch(text, /1 disk not read/);
});

// --- 1: every keyed map keeps every claimant ---------------------------------

test("a port naming a PHY number two rows share is matched to neither row", () => {
  const data = structuredClone(DIRECT);
  const expander = data.fabric.expanders[0];
  const device = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => (entry.expander_ids || []).includes(expander.id));
  Object.assign(device, { expander_phy: 4, handle: null });
  expander.phys.push({ ...expander.phys[0], phy: "4", device: "SAS Target", dev_handle: null, counter_state: "measured", invalid_dword_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 },
    { ...expander.phys[0], phy: "4", device: "SAS Target", dev_handle: null, counter_state: "measured", invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const port = T.buildTopologyModel(data.fabric, { inventory: data.snapshot }).bays[0].ports.find((entry) => entry.expanderId === expander.id);
  assert.equal(port.phy, null);
  assert.match(port.why, /more than one row/);
});

test("a PHY two census ports claim keeps both roles, whatever their order", () => {
  for (const order of ["disk first", "expander first"]) {
    // A disk of Leaf 1, in no bay of this view, also listed as an expander link.
    const item = fixture("fanout-2");
    const census = item.fabric.raw.sas_transport_ports;
    const disk = census.find((port) => port.remote_kind === "end_device" && port.has_block && port.owner_id.endsWith("expander-2:1"));
    const uplink = { ...disk, id: `${disk.id}-uplink`, remote_kind: "expander", remote_id: null, has_block: null, bay_identifier: null };
    if (order === "disk first") census.push(uplink);
    else census.splice(census.indexOf(disk), 0, uplink);
    node(item.fabric, disk.owner_id).raw.phys.find((entry) => entry.phy === disk.phys[0]).invalid_dword_count = 101;
    const built = model(item);
    const found = checksOf(built, "port-errors");
    assert.ok(statuses(built, "port-errors").includes("critical"), `${order}: ${textOf(found)}`);
    const critical = textOf(found.filter((entry) => entry.status === "critical"));
    assert.match(critical, /End device/, order);
    assert.match(critical, /SMP Target/, order);
  }
});

test("a bay's PHY the census also lists as an expander link has its owner not read", () => {
  const item = fixture("fanout-2");
  const port = model(item).bays[0].ports[0];
  const census = item.fabric.raw.sas_transport_ports;
  const disk = census.find((entry) => entry.owner_id === port.expanderId && (entry.phys || []).includes(port.phy.phy));
  census.push({ ...disk, id: `${disk.id}-uplink`, remote_kind: "expander", remote_id: null, has_block: null, bay_identifier: null });
  const built = model(item);
  const portErrors = cardRow(built, { kind: "port", id: built.bays[0].ports[0].key }, /^PHY errors$/);
  assert.match(portErrors.value, /owner not read: Bay 00 claims it, but its row or the transport census calls it a link/);
  assert.ok(!statuses(built, "ports").includes("pass"), textOf(checksOf(built, "ports")));
});

test("a cable PHY number two rows share gives its lane no maximum", () => {
  const data = structuredClone(H.producerCases().core_cascade);
  const options = data.options || { inventory: data.snapshot };
  const link = T.buildTopologyModel(data.fabric, options).links.find((entry) => entry.lanes?.rows.length);
  const lane = link.lanes.rows[0];
  const expander = data.fabric.expanders.find((entry) => entry.id === link.target);
  const original = expander.phys.find((entry) => Number(entry.phy) === lane.target);
  expander.phys.push({ ...original, max: "6.0" });
  const found = checksOf(T.buildTopologyModel(data.fabric, options), "lanes");
  assert.ok(!found.some((entry) => entry.status === "pass"), textOf(found));
  assert.match(textOf(found), /maximum not read for 1 lane/);
});

test("an HBA PHY two cables list is drawn as neither cable's", () => {
  const item = fixture("fanout-2");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  const leaf = item.fabric.nodes.filter((entry) => entry.kind === "expander")[1];
  item.fabric.links.push({ ...structuredClone(uplink), id: `controller-expander:${uplink.source}->${leaf.id}`, target: leaf.id });
  const built = model(item);
  const html = (T.detailFor(built, { kind: "node", id: uplink.source })?.html || "").replaceAll("&#39;", "'");
  assert.match(html, /PHY 0<\/span><span class="kv-value">owner not read: the cable to [^<]* and the cable to [^<]* all claim it/);
  const lanes = T.cardFor(built, { kind: "node", id: uplink.source }).rows.find((entry) => entry.label === "In this view");
  assert.match(lanes.value, /in more than one cable's PHY list/);
});
