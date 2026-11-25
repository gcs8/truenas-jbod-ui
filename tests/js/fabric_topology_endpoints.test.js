"use strict";

// Identifiers and endpoints (#1504). A PHY name, device handle or SAS
// address that names more than one row or device identifies nothing: a
// subject reached through it is not read, never the first match and never
// absent. Two read records naming different targets leave the destination
// not read for every consumer. A disk port is its own SAS address, however
// many paths reach it. A cable's end must be a device type the shared table
// says can end that cable. Every payload is synthetic, in the producer's
// shapes.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const T = H.loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;
const DIRECT = require("../fixtures/sas_fabric/core_direct_paths.json");

const fixture = (id) => structuredClone(FIXTURES.find((item) => item.id === id));
const model = (item) => T.buildTopologyModel(item.fabric, item.options || { inventory: item.snapshot });
const checksOf = (built, id) => built.checks.filter((entry) => entry.id === id || entry.id.startsWith(`${id}:`));
const statuses = (built, id) => JSON.parse(JSON.stringify(checksOf(built, id).map((entry) => entry.status)));
const critical = (built, id) => checksOf(built, id).filter((entry) => entry.status === "critical").flatMap((entry) => entry.lines);
const said = (built, pattern) => [...critical(built, "cable-errors"), ...critical(built, "port-errors")].filter((line) => pattern.test(line));
const textOf = (value) => JSON.stringify(value);
const node = (fabric, id) => fabric.nodes.find((entry) => entry.id === id);
const phyRows = (fabric, id) => node(fabric, id).raw.phys || node(fabric, id).raw.phy_rows;
const cardRows = (built, ref) => T.cardFor(built, ref)?.rows || [];
// Entities are decoded with "&amp;" last, so no text is decoded twice.
const unescape = (value) => value.replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", "\"").replaceAll("&amp;", "&");
const kv = (built, ref) => [...(T.detailFor(built, ref)?.html || "").matchAll(/<div class="kv-row([^"]*)"[^>]*><span[^>]*>([^<]*)<\/span><span[^>]*>([^<]*)<\/span>/g)]
  .map((match) => [match[1].trim(), unescape(match[2]), unescape(match[3])]);
const HOST1 = "controller:host1";
const HOST6 = "expander:controller:host6:expander-6:0";
const unread = (entry, ...keys) => {
  for (const key of keys) entry[key] = null;
  entry.counter_state = "not_read";
  entry.not_read = [...new Set([...(entry.not_read || []), ...keys])];
};

// A disk attached directly to host1, on PHY 12 (or a wide port on 12 and 13).
function direct({ mapped = false, count = 101, missing = false, kind = "end_device", phys = [12] } = {}) {
  const item = fixture("wide-x4-sata");
  const rows = phyRows(item.fabric, HOST1);
  if (!missing) {
    for (const phy of phys) rows.push({ ...rows[0], name: `phy-1:${phy}`, phy, not_read: [], counter_state: "measured", invalid_dword_count: phy === phys[0] ? count : 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  }
  const id = "sas-port:port-1:12";
  const names = phys.map((phy) => `phy-1:${phy}`);
  item.fabric.raw.sas_transport_ports.push({ id, owner_id: HOST1, state: "read", phy_names: names, phys, remote_kind: kind, remote_id: null,
    remote_sas_address: "5eee001e00000123", has_block: kind === "end_device" ? true : null, bay_identifier: mapped ? 7 : null, enclosure_identifier: null, target_basis: "reported" });
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id: HOST1, phy_names: names, state: "read" };
  if (mapped) {
    Object.assign(node(item.fabric, "bay:7").metrics, { present: true, device_name: "wdfresh", sas_address: "5eee001e00000123", transport_protocol: "SAS", target_port_protocol: "SSP" });
    const row = { controller: "host1", state: "unknown", device_name: "wdfresh", transport_protocol: "SAS", attached_sas_address: "5aaa000000000101", phy_identifier: phys[0],
      target_port_protocol: "SSP", sas_address: "5eee001e00000123", member_device_name: "wdfresh", bay_identifier: 7, port: "port-1:12", expander_ids: [], sas_transport_port_id: id, source: "linux_sas_transport" };
    Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:7").metrics, { sas_transport_ports: [row], path_states: [row] });
  }
  return item;
}

// Three HBAs observing one chip: host6, host7 and a copy of host7 as host8.
function threeReaders() {
  const item = fixture("dual-domain-linux");
  const seven = node(item.fabric, "expander:controller:host7:expander-7:0");
  const control = node(item.fabric, "controller:host7");
  const copies = JSON.parse(JSON.stringify([seven, control]).replaceAll("host7", "host8").replaceAll("-7:", "-8:").replaceAll("5aaa000000000701", "5aaa000000000801"));
  item.fabric.nodes.push(...copies);
  const link = item.fabric.links.find((entry) => entry.target === seven.id);
  item.fabric.links.push(JSON.parse(JSON.stringify(link).replaceAll("host7", "host8").replaceAll("-7:", "-8:")));
  return { item, host8: copies[0] };
}
const slow = (entry) => Object.assign(entry, { negotiated_rate_gbps: 3, speed: "3.0 Gbit", negotiated_link_rate: "3.0 Gbit" });

const fs = require("node:fs");
const path = require("node:path");
const record = (item) => item.fabric.raw.sas_transport_ports.find((entry) => entry.owner_id === HOST1 && entry.phy_names.includes("phy-1:12"));
const pin = (built, id, phy) => kv(built, { kind: "node", id }).find(([, label]) => label === `PHY ${phy}`)?.[2] || "";
const core = () => structuredClone(H.producerCases().core_cascade);
const coreModel = (data) => T.buildTopologyModel(data.fabric, data.options || { inventory: data.snapshot });
// The CORE bay-port PHY of the R7 reproductions, its own row given a device type.
function coreRole(device, count = 101) {
  const data = structuredClone(DIRECT);
  const expander = data.fabric.expanders[0];
  const port = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => (entry.expander_ids || []).includes(expander.id));
  expander.phys.push({ ...expander.phys[0], phy: "4", device, dev_handle: port.handle, counter_state: "measured",
    invalid_dword_count: count, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  return { data, expander, built: T.buildTopologyModel(data.fabric, { inventory: data.snapshot }) };
}

const said1 = (built, pattern) => {
  const lines = said(built, pattern);
  assert.equal(lines.length, 1, textOf(built.checks.filter((entry) => entry.status === "critical")));
  return lines[0];
};
const expanders = (item) => item.fabric.nodes.filter((entry) => entry.kind === "expander");

// --- 1: identity uniqueness ----------------------------------------------------------

test("a PHY name on two rows identifies no PHY: its fault is said once and no counter check passes", () => {
  const control = direct();
  record(control).phys = [];
  assert.ok(statuses(model(control), "port-errors").includes("critical"), "the unique name is a disk port");
  const item = direct();
  record(item).phys = [];
  const rows = phyRows(item.fabric, HOST1);
  rows.push({ ...rows.find((entry) => entry.phy === 12), phy: 13, invalid_dword_count: 0 });
  const built = model(item);
  for (const id of ["port-errors", "cable-errors"]) assert.ok(!statuses(built, id).includes("pass"), `${id}: ${textOf(checksOf(built, id))}`);
  const line = said1(built, /101 invalid dword/);
  assert.match(line, /host1: PHY 12 \(role not read: its PHY name is on more than one row/, line);
  assert.match(textOf(checksOf(built, "port-errors")), /names phy-1:12, which is on more than one row of host1's PHY table/);
});

test("a device handle two expanders of one HBA share names no destination", () => {
  for (const mode of ["duplicate", "unread"]) {
    const data = core();
    const link = data.fabric.links.find((entry) => entry.reported_lanes);
    Object.assign(link.reported_lanes.lanes[0], { target_phy: 0, negotiated_rate_gbps: 3 });
    const controller = data.fabric.controllers.find((entry) => entry.id === link.source);
    const own = data.fabric.expanders.filter((entry) => entry.id.includes(":mpr0:"));
    if (mode === "duplicate") own[1].dev_handle = own[0].dev_handle;
    else delete controller.phy_rows.find((entry) => Number(entry.phy) === 0).device_handle;
    const built = coreModel(data);
    const value = pin(built, controller.id, 0);
    assert.match(value, /^destination not read/, `${mode}: ${value}`);
    assert.doesNotMatch(value, /outside this view/, mode);
    if (mode === "duplicate") assert.match(value, /device handle 0009 names more than one expander/, value);
    assert.doesNotMatch(textOf(cardRows(built, { kind: "node", id: controller.id })), /device handle 0009[^"]*not in this enclosure's payload/, mode);
  }
});

test("an HBA row whose own type cannot be a cable names no expander by its handle", () => {
  const data = core();
  const controller = data.fabric.controllers.find((entry) => entry.id === data.fabric.links[0].source);
  controller.phy_rows.find((entry) => Number(entry.phy) === 0).device = "SAS Target";
  data.fabric.links[0].reported_lanes.lanes[0].target_phy = 0;
  const value = pin(coreModel(data), controller.id, 0);
  assert.match(value, /^destination not read: its row says SAS Target, but its device handle names 5000c50000000e01/, value);
});

test("a disk port SAS address two bays report is not read in the disk-port check", () => {
  const item = fixture("fanout-2");
  const before = model(item);
  const [first, second] = before.bays.filter((bay) => bay.ports.some((port) => port.sasAddress));
  const address = first.ports.find((port) => port.sasAddress).sasAddress;
  for (const row of Object.values(item.fabric.traces.find((entry) => entry.id === second.bayId).metrics).filter(Array.isArray).flat()) {
    if (row && typeof row === "object" && "sas_address" in row) row.sas_address = address;
  }
  const built = model(item);
  assert.ok(!statuses(built, "port-errors").includes("pass"), textOf(checksOf(built, "port-errors")));
  assert.match(textOf(checksOf(built, "port-errors")), new RegExp(`${second.label}: its disk port address ${address} is also reported for ${first.label}`));
  // Counted as not read, not only said.
  assert.match(textOf(checksOf(built, "port-errors")), /\b\d+ of \d+ disk port PHYs? not read/);
});

test("a bay's PHY a read census record says is attached to something not read is not the bay's", () => {
  const item = fixture("fanout-2");
  const port = model(item).bays[0].ports[0];
  item.fabric.raw.sas_transport_ports.push({ id: "sas-port:extra-unknown", owner_id: port.expanderId, state: "read", phy_names: ["phy-not-a-row"], phys: [port.phy.phy],
    remote_kind: "unknown", remote_id: null, remote_sas_address: null, has_block: null, bay_identifier: null, enclosure_identifier: null, target_basis: "reported" });
  const built = model(item);
  const errors = cardRows(built, { kind: "port", id: built.bays[0].ports[0].key }).find((entry) => entry.label === "PHY errors");
  assert.match(errors.value, /owner not read: Bay 00 claims it, but the transport census does not say what it is attached to/, errors.value);
});

test("two faulty PHYs of one wide port whose address is not read are still one port", () => {
  const item = direct({ mapped: true, phys: [12, 13] });
  phyRows(item.fabric, HOST1).find((entry) => entry.phy === 13).invalid_dword_count = 102;
  for (const row of Object.values(item.fabric.traces.find((entry) => entry.id === "bay:7").metrics).filter(Array.isArray).flat()) delete row.sas_address;
  delete node(item.fabric, "bay:7").metrics.sas_address;
  const built = model(item);
  assert.equal(built.bayById.get("bay:7").ports[0].sasAddress, "");
  assert.match(critical(built, "port-errors")[0], /^Bay 07 \(wdfresh\), one port:/);
});

// --- 2: destination conflicts propagate ----------------------------------------------

function twoTargets(mode) {
  const item = direct({ kind: "expander" });
  const [one, two] = expanders(item);
  Object.assign(record(item), { remote_id: one.id, remote_sas_address: one.raw.sas_address });
  const second = { ...record(item), id: "sas-port:relation-2", remote_id: two.id, remote_sas_address: two.raw.sas_address };
  if (mode === "absent") Object.assign(second, { remote_id: "expander:controller:host1:expander-1:99", remote_sas_address: "5ddd0000000000ee" });
  if (mode === "same") Object.assign(second, { remote_id: one.id, remote_sas_address: one.raw.sas_address });
  item.fabric.raw.sas_transport_ports.push(second);
  if (mode === "covered") item.fabric.links.find((link) => link.kind === "controller-expander").reported_lanes.source_phys[0] = 12;
  return item;
}

test("two read records naming two targets leave a cable-listed PHY's destination not read everywhere", () => {
  const item = twoTargets("covered");
  const built = model(item);
  const link = built.links.find((entry) => entry.source === HOST1 && entry.lanes?.sourcePhys.includes(12));
  const value = pin(built, HOST1, 12);
  assert.match(value, /destination not read/, value);
  assert.doesNotMatch(value, /^to Expander/, value);
  const line = said1(built, /101 invalid dword/);
  assert.doesNotMatch(line, /→/, line);
  assert.match(line, /destination not read/, line);
  assert.ok(!checksOf(built, "lanes").some((entry) => entry.status === "pass"), textOf(checksOf(built, "lanes")));
  assert.match(textOf(checksOf(built, "lanes")), /PHY 12 at host1: the transport census names [^"]*, so which PHYs the cable uses is not read/);
  const lanes = kv(built, { kind: "link", id: link.id }).find(([, label]) => /host1 PHY 12$/.test(label));
  assert.ok(lanes && !/101 invalid dword/.test(lanes[2]), textOf(lanes));
});

test("a read record naming an expander not in the payload is a second destination", () => {
  const value = pin(model(twoTargets("absent")), HOST1, 12);
  assert.match(value, /^destination not read: the transport census names Expander …[0-9a-f]{4} and a device not in this payload \(5ddd0000000000ee\)/, value);
  assert.match(pin(model(twoTargets("same")), HOST1, 12), /^to Expander …[0-9a-f]{4}; which cable it is in is not read/);
});

// --- 3: a disk port is its own SAS address -------------------------------------------

function sameAddress(distinct) {
  const data = structuredClone(DIRECT);
  const controller = data.fabric.controllers.find((entry) => entry.id === "controller:mpr0");
  controller.phy_rows = [{ phy: "2", device_handle: "0030", device: "SAS Target", speed: "12.0", min: "1.5", max: "12.0" }];
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  Object.assign(metrics.mpr_devices[0], { expander_ids: ["controller:mpr0"], expander_phy: 2, handle: null, parent: null });
  metrics.mpr_devices.push({ ...metrics.mpr_devices[1], member_device_name: "da31", handle: "0091" });
  metrics.path_states.push({ ...metrics.path_states[1], device_name: "da31" });
  for (const row of [...metrics.path_states, ...metrics.mpr_devices]) row.state = "active";
  if (distinct) metrics.mpr_devices[2].sas_address = "5000c50000000d02";
  const expander = data.fabric.expanders[1];
  ["0020", "0091"].forEach((handle, index) => expander.phys.push({ ...expander.phys[0], phy: String(index + 5), dev_handle: handle, device: "SAS Target",
    invalid_dword_count: index + 101, phy_reset_problem_count: 0, loss_of_dword_synchronization_count: 0, running_disparity_error_count: 0, counter_state: "measured" }));
  return T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
}

test("two paths that name one disk port address are faults on one port, each on its own PHY", () => {
  const built = sameAddress(false);
  const lines = critical(built, "port-errors");
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /one port: [^;]*PHY 5: 101 invalid dword; [^;]*PHY 6: 102 invalid dword/, lines[0]);
  assert.doesNotMatch(textOf(checksOf(built, "port-errors")), /both ports/);
  assert.match(critical(sameAddress(true), "port-errors")[0], /both ports/);
});

test("an unpaired cable's PHY rows are not styled slow past a member whose role is not read", () => {
  const item = direct();
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  uplink.reported_lanes.source_phys[0] = 12;
  const near = phyRows(item.fabric, HOST1).find((entry) => entry.phy === uplink.reported_lanes.source_phys[1]);
  Object.assign(near, { negotiated_rate_gbps: 3, negotiated_link_rate: "3.0 Gbit" });
  const built = model(item);
  const link = built.links.find((entry) => entry.source === HOST1 && entry.lanes?.sourcePhys.includes(12));
  const rows = kv(built, { kind: "link", id: link.id }).filter(([, label]) => /PHY \d+$/.test(label));
  assert.ok(rows.length, textOf(kv(built, { kind: "link", id: link.id })));
  assert.ok(!rows.some(([cls, , value]) => cls === "topo-warn" && /\b3 Gb\/s/.test(value)), textOf(rows));
});

// --- 4: endpoint plausibility from the shared table -----------------------------------

test("an HBA cable's far PHY that names no initiator is not the cable's, for every non-initiator type", () => {
  for (const device of ["Edge Expander", "No Device", "SEP Target", "SAS Target", "Unknown Device", "Frobnicator Device"]) {
    const data = core();
    const link = data.fabric.links[0];
    Object.assign(data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32),
      { device, invalid_dword_count: 101, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, counter_state: "measured" });
    const built = coreModel(data);
    const line = said1(built, /101 invalid dword/);
    assert.doesNotMatch(line, /→/, `${device}: ${line}`);
    assert.match(line, /(owner|role) not read/, `${device}: ${line}`);
    // The PHY's own reason, not only that its cable's members are not read.
    if (device === "Edge Expander") assert.match(line, /names an expander, which cannot be this end of that cable/, line);
    const lane = kv(built, { kind: "link", id: link.id }).find(([, label]) => label === "Lane PHY 0 ↔ 32");
    assert.doesNotMatch(lane[2], /target PHY: 101/, `${device}: ${lane[2]}`);
    assert.ok(!statuses(built, "lanes").includes("pass"), `${device}: ${textOf(checksOf(built, "lanes"))}`);
    // A slow lane on that cable is not judged past the endpoint not read.
    const slow = core();
    Object.assign(slow.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32), { device });
    slow.fabric.links[0].reported_lanes.lanes[1].negotiated_rate_gbps = 3;
    const slowBuilt = coreModel(slow);
    assert.ok(!statuses(slowBuilt, "lanes").includes("critical"), `${device}: ${textOf(checksOf(slowBuilt, "lanes"))}`);
    assert.doesNotMatch(cardRows(slowBuilt, { kind: "node", id: link.source }).find((entry) => entry.label === "In this view")?.value || "", /below the maximum/, device);
  }
  const data = core();
  const link = data.fabric.links[0];
  Object.assign(data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32),
    { invalid_dword_count: 101, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, counter_state: "measured" });
  assert.match(said1(coreModel(data), /101 invalid dword/), /^mpr0 → 5000c50000000e01: at 5000c50000000e01 PHY 32: 101/);
});

test("a bay's PHY whose row says nothing is attached is not the disk's", () => {
  const { built, expander } = coreRole("No Device");
  assert.match(said1(built, /101 invalid dword/), /owner not read: Bay 00 claims it, but its row or the transport census says nothing is attached/);
  assert.doesNotMatch(cardRows(built, { kind: "node", id: expander.id }).find((entry) => entry.label === "PHY errors").value, /PHY 4 \(Bay 00\)/);
});

test("the table says which device types end which cable, as the CORE builder reads them", () => {
  const read = (file) => fs.readFileSync(path.join(__dirname, "..", "..", file), "utf8");
  const set = (file, name) => [...read(file).match(new RegExp(`${name} = frozenset\\(\\s*\\{([^}]*)\\}`))[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  const types = set("app/services/sas_fabric_parsers.py", "MPR_DEVICE_TYPES");
  const expander = set("app/services/sas_fabric_core.py", "_MPR_EXPANDER_ATTACHED_DEVICES");
  const initiator = set("app/services/sas_fabric_core.py", "_MPR_INITIATOR_ATTACHED_DEVICES");
  for (const device of new Set([...types, ...expander, ...initiator])) {
    // The child end of a cable from an HBA, and of a cable from an expander.
    assert.equal(T.cableEndAccepts("controller-expander", "far", device), initiator.includes(device), `HBA cable far end: ${device}`);
    assert.equal(T.cableEndAccepts("expander-expander", "far", device), expander.includes(device), `cascade far end: ${device}`);
  }
});

// --- follow-up: an unread census record never silences a fault -----------------------

test("a fault on an expander PHY only a census record not read lists is said once", () => {
  const item = fixture("wide-x4-sata");
  const chip = expanders(item)[0];
  const row = { ...chip.raw.phys[0], phy: 91, name: "phy-1:0:91", invalid_dword_count: 101, counter_state: "measured", not_read: [] };
  chip.raw.phys.push(row);
  item.fabric.raw.sas_transport_ports.push({ ...item.fabric.raw.sas_transport_ports.find((entry) => entry.owner_id === chip.id), id: "sas-port:extra", phys: [91], phy_names: [row.name], state: "not_read" });
  const line = said1(model(item), /101 invalid dword/);
  assert.match(line, /PHY 91 \(role not read: its transport census record was not read\)/, line);
});

test("an over-long far list claims none of its members, even one whose type can end the cable", () => {
  const data = core();
  const link = data.fabric.links.find((entry) => entry.reported_lanes);
  const rows = data.fabric.expanders.find((entry) => entry.id === link.target).phys;
  const extra = rows.find((entry) => !link.reported_lanes.target_phys.includes(Number(entry.phy)) && entry.device === "No Device");
  link.reported_lanes.target_phys.push(Number(extra.phy));
  Object.assign(extra, { device: "SAS Initiator", invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0, counter_state: "measured" });
  const line = said1(coreModel(data), /101 invalid dword/);
  assert.doesNotMatch(line, /→/, line);
});

test("a read expander record that names no target at all gives destination not read", () => {
  const item = direct({ kind: "expander", count: 0 });
  Object.assign(record(item), { remote_id: null, remote_sas_address: null });
  const value = pin(model(item), HOST1, 12);
  assert.match(value, /^destination not read( ·|$)/, value);
});
