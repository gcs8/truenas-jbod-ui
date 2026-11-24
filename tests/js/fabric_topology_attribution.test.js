"use strict";

// One owner per measured PHY observation, and every verdict names the
// measurement that set it (#1504). A PHY that a disk, a cable or two cables
// claim, or whose number is on two rows, has its owner not read on every
// surface: no cable takes a counter that may be a disk's, no two cables
// take one HBA PHY, and a fault whose owner is not read is still said once.
// Both ends of a cable are held to its lane count. Counts are over distinct
// subjects. Every payload is synthetic, in the producer's shapes.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const T = H.loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;

const fixture = (id) => structuredClone(FIXTURES.find((item) => item.id === id));
const model = (item) => T.buildTopologyModel(item.fabric, { ...item.options, width: 1150 });
const checksOf = (built, id) => built.checks.filter((entry) => entry.id === id || entry.id.startsWith(`${id}:`));
const statuses = (built, id) => JSON.parse(JSON.stringify(checksOf(built, id).map((entry) => entry.status)));
const textOf = (value) => JSON.stringify(value);
const node = (fabric, id) => fabric.nodes.find((entry) => entry.id === id);
const phyRow = (fabric, id, name) => (node(fabric, id).raw.phys || node(fabric, id).raw.phy_rows).find((entry) => entry.name === name);
const cardRows = (built, ref) => T.cardFor(built, ref)?.rows || [];
const unread = (entry, ...keys) => {
  for (const key of keys) entry[key] = null;
  entry.counter_state = "not_read";
  entry.not_read = [...new Set([...(entry.not_read || []), ...keys])];
};
const X4 = "expander:controller:host1:expander-1:0";
const HOST6 = "expander:controller:host6:expander-6:0";
const critical = (built, id) => checksOf(built, id).filter((entry) => entry.status === "critical");

// --- 1: attribution -----------------------------------------------------------

test("P1: a disk row's fault is never charged to a cable whose inferred member shares its number", () => {
  const item = fixture("wide-x4-sata");
  phyRow(item.fabric, X4, "phy-1:0:0").invalid_dword_count = 101;
  phyRow(item.fabric, X4, "phy-1:0:16").phy = 0;
  const built = model(item);
  const cable = checksOf(built, "cable-errors");
  assert.doesNotMatch(textOf(cable.filter((entry) => entry.status !== "not_read")), /101 invalid dword/, textOf(cable));
  assert.ok(!statuses(built, "cable-errors").includes("pass"), textOf(cable));
  // The 101 is still said, once, with its owner not read.
  const said = [...critical(built, "cable-errors"), ...critical(built, "port-errors")];
  assert.equal(said.flatMap((entry) => entry.lines).filter((line) => /101 invalid dword/.test(line)).length, 1, textOf(said));
  assert.match(textOf(said), /owner not read/);
  const chip = cardRows(built, { kind: "node", id: X4 }).find((entry) => entry.label === "PHY errors");
  assert.doesNotMatch(chip.value, /PHY 0 \(cable from/, chip.value);
  assert.match(chip.value, /PHY 0 \(owner not read/);
  // Control: distinct numbers: Bay 00, and the cable passes.
  const control = fixture("wide-x4-sata");
  phyRow(control.fabric, X4, "phy-1:0:0").invalid_dword_count = 101;
  assert.match(textOf(critical(model(control), "port-errors")), /Bay 00/);
});

test("two cables listing one HBA PHY: its fault is said once, owner not read, on neither cable", () => {
  const item = fixture("fanout-2");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  const leaf = item.fabric.nodes.filter((entry) => entry.kind === "expander")[1];
  item.fabric.links.push({ ...structuredClone(uplink), id: `controller-expander:${uplink.source}->${leaf.id}`, target: leaf.id });
  node(item.fabric, uplink.source).raw.phy_rows[0].invalid_dword_count = 101;
  const built = model(item);
  const lines = critical(built, "cable-errors").flatMap((entry) => entry.lines).filter((line) => /101 invalid dword/.test(line));
  assert.equal(lines.length, 1, textOf(checksOf(built, "cable-errors")));
  assert.match(lines[0], /host2: PHY 0 \(owner not read/);
  for (const link of built.links.filter((entry) => entry.source === uplink.source && entry.lanes)) {
    const end = cardRows(built, { kind: "link", id: link.id }).find((entry) => /^Errors at host2/.test(entry.label));
    assert.doesNotMatch(end.value, /101/, `${link.id}: ${end.value}`);
    assert.match(end.value, /owner not read/, `${link.id}: ${end.value}`);
  }
});

test("a bay's PHY in a cascade's PHY list has its owner not read on the bay, chip and cable alike", () => {
  const item = fixture("fanout-2");
  const port = model(item).bays[0].ports[0];
  const cascade = item.fabric.links.find((link) => link.kind === "expander-expander" && link.source === port.expanderId);
  cascade.reported_lanes.source_phys[0] = port.phy.phy;
  node(item.fabric, port.expanderId).raw.phys.find((entry) => entry.phy === port.phy.phy).invalid_dword_count = 101;
  const built = model(item);
  assert.doesNotMatch(built.bays[0].extra.join(" "), /101 invalid dword/, built.bays[0].extra.join(" | "));
  const chip = cardRows(built, { kind: "node", id: port.expanderId }).find((entry) => entry.label === "PHY errors");
  assert.match(chip.value, new RegExp(`PHY ${port.phy.phy} \\(owner not read`), chip.value);
  assert.doesNotMatch(textOf(checksOf(built, "cable-errors").filter((entry) => entry.status !== "not_read")), /101/);
  const said = [...critical(built, "cable-errors"), ...critical(built, "port-errors")].flatMap((entry) => entry.lines).filter((line) => /101 invalid dword/.test(line));
  assert.equal(said.length, 1, textOf(said));
});

test("an HBA fault on a number two rows share is said, never lost", () => {
  const item = fixture("wide-x4-sata");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  const hba = node(item.fabric, uplink.source);
  hba.raw.phy_rows[0].invalid_dword_count = 101;
  hba.raw.phy_rows[1].phy = hba.raw.phy_rows[0].phy;
  uplink.reported_lanes = null;
  const built = model(item);
  const said = critical(built, "cable-errors").flatMap((entry) => entry.lines).filter((line) => /101 invalid dword/.test(line));
  assert.equal(said.length, 1, textOf(checksOf(built, "cable-errors")));
  assert.match(said[0], /owner not read: its number is on more than one row/);
  const note = built.transportNote.measured.join(" ");
  assert.doesNotMatch(note, /in part on 2/, note);
  assert.match(note, /1 number on more than one row/, note);
  // The disputed number is not also counted as read, and the census's PHY 1
  // (its row renumbered to 0) is missing: read + disputed + missing = all.
  const [, readCount, total] = /read on (\d+) of (\d+)/.exec(note).map(Number);
  assert.match(note, /\(1 return none\)/, note);
  assert.equal(readCount + 2, total, note);
});

// --- 2: a verdict names the measurement that set it ----------------------------

test("CRITICAL names the critical PHY first, whatever its number", () => {
  // The warning on a lower PHY number than the critical one.
  const item = fixture("wide-x4-sata");
  node(item.fabric, X4).raw.phys.find((entry) => entry.phy === 16).invalid_dword_count = 1;
  const seventeen = node(item.fabric, X4).raw.phys.find((entry) => entry.phy === 17);
  unread(seventeen, "invalid_dword_count");
  seventeen.loss_of_dword_synchronization_count = 10;
  const line = critical(model(item), "cable-errors").flatMap((entry) => entry.lines)[0];
  assert.match(line, /PHY 17: 10 loss of dword sync/, line);
  assert.ok(line.indexOf("PHY 17") < line.indexOf("PHY 16"), line);
});

test("CRITICAL names the critical PHY and its count first, beside a partial read", () => {
  for (const mode of ["one critical", "critical and warning"]) {
    const item = fixture("wide-x4-sata");
    const sixteen = node(item.fabric, X4).raw.phys.find((entry) => entry.phy === 16);
    unread(sixteen, "invalid_dword_count");
    sixteen.loss_of_dword_synchronization_count = 10;
    if (mode !== "one critical") node(item.fabric, X4).raw.phys.find((entry) => entry.phy === 17).invalid_dword_count = 1;
    const built = model(item);
    const lines = critical(built, "cable-errors").flatMap((entry) => entry.lines);
    assert.ok(lines.length, `${mode}: ${textOf(checksOf(built, "cable-errors"))}`);
    assert.match(lines[0], /PHY 16: 10 loss of dword sync/, `${mode}: ${lines[0]}`);
    if (mode !== "one critical") assert.ok(lines[0].indexOf("PHY 16") < lines[0].indexOf("PHY 17"), lines[0]);
  }
});

// --- 3: both ends' membership ----------------------------------------------------

test("a repeated far-end member is membership not read for lanes and cables", () => {
  for (const mode of ["far list only", "far list and its lane"]) {
    const data = structuredClone(H.producerCases().core_cascade);
    const link = data.fabric.links.find((entry) => entry.reported_lanes);
    const lanes = link.reported_lanes;
    lanes.target_phys[3] = lanes.target_phys[2];
    if (mode !== "far list only") lanes.lanes[3].target_phy = lanes.lanes[2].target_phy;
    const built = T.buildTopologyModel(data.fabric, data.options || { inventory: data.snapshot });
    const found = checksOf(built, "lanes");
    assert.ok(!statuses(built, "lanes").includes("pass"), `${mode}: ${textOf(found)}`);
    assert.match(textOf(found), /3 distinct PHYs listed at .*far|far.*3 distinct/, mode);
    // A lane below both ends' maximum is not judged while its far member is not read.
    const slow = structuredClone(data);
    const lanesRow = slow.fabric.links.find((entry) => entry.reported_lanes).reported_lanes.lanes[0];
    for (const key of Object.keys(lanesRow).filter((name) => /rate/.test(name) && typeof lanesRow[name] === "number")) lanesRow[key] = 3;
    const slowBuilt = T.buildTopologyModel(slow.fabric, slow.options || { inventory: slow.snapshot });
    assert.ok(!statuses(slowBuilt, "lanes").some((status) => ["critical", "warning"].includes(status) && checksOf(slowBuilt, "lanes").some((entry) => /below the maximum/.test(entry.lines.join(" ")))), textOf(checksOf(slowBuilt, "lanes")));
    // The unlisted uplink PHY is an uplink PHY, never a seventh cable end.
    assert.doesNotMatch(textOf(checksOf(built, "cable-errors")), /\b\d+ of 7 cable ends\b/);
  }
});

// --- 4: equality needs both readings complete -------------------------------------

test("'same counters' needs every observation of the PHY fully read, in either order", () => {
  for (const host of ["host6", "host7"]) {
    const item = fixture("dual-domain-linux");
    const id = host === "host6" ? HOST6 : "expander:controller:host7:expander-7:0";
    unread(phyRow(item.fabric, id, `phy-${host.slice(4)}:0:28`), "invalid_dword_count");
    const built = model(item);
    const chip = built.nodes.find((entry) => entry.kind === "expander" && entry.rows?.length > 1);
    const via = cardRows(built, { kind: "node", id: chip.id }).find((entry) => /Also read via/.test(entry.label));
    assert.doesNotMatch(via.value, /same counters/, `${host}: ${via.value}`);
    assert.match(via.value, /PHY 28/, `${host}: ${via.value}`);
  }
});

// --- 5: counts over distinct subjects -----------------------------------------------

test("SES slots are counted by distinct bay, not by state row", () => {
  const item = fixture("empty-survey");
  const states = item.fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states;
  states.push(structuredClone(states[0]));
  const text = textOf(checksOf(model(item), "path-states"));
  assert.match(text, /4 slots mapped/);
  assert.doesNotMatch(text, /5 slots/);
});

test("the transport note's denominator is the expected PHYs, so a missing row is missing", () => {
  const item = fixture("wide-x4-sata");
  const before = model(item).transportNote.measured.join(" ");
  const disk = item.fabric.raw.sas_transport_ports.find((port) => port.owner_id === X4 && port.remote_kind === "end_device");
  node(item.fabric, X4).raw.phys = node(item.fabric, X4).raw.phys.filter((entry) => !disk.phys.includes(entry.phy));
  const built = model(item);
  const note = built.transportNote.measured.join(" ");
  const total = (text) => Number(/read on \d+ of (\d+)/.exec(text)[1]);
  assert.equal(total(note), total(before), note);
  const chip = cardRows(built, { kind: "node", id: X4 }).find((entry) => entry.label === "PHY errors");
  assert.match(chip.value, /1 PHY not read/, chip.value);
  const bus = cardRows(built, { kind: "link", id: built.nodeById.get(X4).busId }).find((entry) => entry.label === "PHY errors");
  assert.match(bus.value, /1 disk port PHY not read/, bus.value);
});

// --- an unmarked subject is not read, never a pass ---------------------------

test("a disk port on a PHY its own expander row calls an expander link has its owner not read, and said", () => {
  // CORE: a bay port joined by handle to a PHY whose row says SMP Target.
  // A bay claims it and its row calls it a link: the owner is not read.
  const data = structuredClone(require("../fixtures/sas_fabric/core_direct_paths.json"));
  const expander = data.fabric.expanders[0];
  const device = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => (entry.expander_ids || []).includes(expander.id));
  expander.phys.push({ ...expander.phys[0], phy: "4", device: "SMP Target", dev_handle: device.handle, counter_state: "measured",
    invalid_dword_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  assert.equal(built.bays[0].ports.find((port) => port.expanderId === expander.id).phy.phy, 4);
  const found = checksOf(built, "port-errors");
  assert.ok(!statuses(built, "port-errors").includes("pass"), textOf(found));
  assert.match(textOf(found), /PHY 4 \(owner not read: Bay 00 claims it, but its row or the transport census calls it a link/);
});

test("a CORE disk attached directly to an HBA whose counters are not collected is not judged, and said", () => {
  // The port names a PHY of its HBA's own table; this platform does not read
  // HBA counters, so no check judges it, and the disk-port check says so.
  const data = structuredClone(require("../fixtures/sas_fabric/core_direct_paths.json"));
  const controller = data.fabric.controllers.find((entry) => entry.id === "controller:mpr0");
  controller.phy_rows = [{ phy: "2", device_handle: "0030", device: "SAS Target", speed: "12.0", min: "1.5", max: "12.0" }];
  const device = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices[0];
  Object.assign(device, { expander_ids: ["controller:mpr0"], expander_phy: 2, handle: null, parent: null });
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  const port = built.bays[0].ports.find((item) => item.expanderId === "controller:mpr0");
  assert.equal(port.phy?.phy, 2);
  const found = checksOf(built, "port-errors");
  assert.ok(!statuses(built, "port-errors").includes("pass"), textOf(found));
  assert.match(textOf(found), /1 disk port PHY not judged from read evidence: Bay 00: PHY 2 of mpr0/);
  // Its one other path, through an expander, is not said to carry every path.
  assert.doesNotMatch(textOf(checksOf(built, "ports")), /runs through/);
});
