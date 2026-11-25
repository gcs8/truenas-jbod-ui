"use strict";

// Identity before scope, presence before absence, one conflict state for
// every consumer, order independence and one finding per measurement
// (#1504). The CORE and transport identity states some tests set are what
// the Python builder emits (tests/test_fabric_topology_fixture.py holds the
// builder to them). Every payload is synthetic, in the producer's shapes.

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
// The CORE paths fixture with Bay 00's direct path on mpr0 and two mpr1
// paths through …0e01; extra rows on that expander's PHYs 5 and 6.
function samePhyPaths(order) {
  const data = structuredClone(DIRECT);
  const controller = data.fabric.controllers.find((entry) => entry.id === "controller:mpr0");
  controller.phy_rows = [{ phy: "2", device_handle: "0030", device: "SAS Target", speed: "12.0", min: "1.5", max: "12.0" }];
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  Object.assign(metrics.mpr_devices[0], { expander_ids: ["controller:mpr0"], expander_phy: 2, handle: null, parent: null });
  metrics.mpr_devices.push({ ...metrics.mpr_devices[1], member_device_name: "da31", handle: "0091" });
  metrics.path_states.push({ ...metrics.path_states[1], device_name: "da31" });
  for (const row of [...metrics.path_states, ...metrics.mpr_devices]) row.state = "active";
  const expander = data.fabric.expanders[1];
  ["0020", "0091"].forEach((handle, index) => expander.phys.push({ ...expander.phys[0], phy: String(index + 5), dev_handle: handle, device: "SAS Target",
    invalid_dword_count: index + 101, phy_reset_problem_count: 0, loss_of_dword_synchronization_count: 0, running_disparity_error_count: 0, counter_state: "measured" }));
  // da41 on da11's handle (PHY 5), naming another disk port address.
  const extra = { ...metrics.mpr_devices[1], member_device_name: "da41", sas_address: "5000c50000000d02" };
  const state = { ...metrics.path_states[1], device_name: "da41" };
  if (order === "first") {
    metrics.mpr_devices.unshift(extra);
    metrics.path_states.unshift(state);
  } else {
    metrics.mpr_devices.push(extra);
    metrics.path_states.push(state);
  }
  return T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
}
const cableLane = (built, link, from, to) => kv(built, { kind: "link", id: link.id }).find(([, label]) => label === `Lane PHY ${from} ↔ ${to}`);

// --- 1: identity before scope ------------------------------------------------------

test("a disk port address two bays report assigns neither bay a fault", () => {
  const item = fixture("fanout-2");
  const [first, second] = model(item).bays.filter((bay) => bay.ports.some((port) => port.sasAddress));
  const address = first.ports[0].sasAddress;
  for (const row of Object.values(item.fabric.traces.find((entry) => entry.id === second.bayId).metrics).filter(Array.isArray).flat()) if (row && "sas_address" in row) row.sas_address = address;
  const port = second.ports[0];
  phyRows(item.fabric, port.expanderId).find((entry) => entry.phy === port.phy.phy).invalid_dword_count = 101;
  const built = model(item);
  const line = said1(built, /101 invalid dword/);
  assert.doesNotMatch(line, new RegExp(`^${second.label}`), line);
  assert.match(line, /owner not read: [^)]*disk port address/, line);
  const card = cardRows(built, { kind: "port", id: built.bayById.get(second.bayId).ports[0].key }).find((entry) => entry.label === "PHY errors");
  assert.notEqual(card?.warn, "critical", textOf(card));
  assert.doesNotMatch(pin(built, port.expanderId, port.phy.phy), new RegExp(`^${second.label}`));
});

test("a disk port address a census record outside this view also reports is ambiguous here", () => {
  for (const mode of ["other slot", "builder state"]) {
    const item = fixture("fanout-2");
    const port = model(item).bays[0].ports[0];
    const census = item.fabric.raw.sas_transport_ports;
    const own = census.find((entry) => entry.owner_id === port.expanderId && (entry.phys || []).includes(port.phy.phy));
    if (mode === "other slot") census.push({ ...own, id: "sas-port:elsewhere", owner_id: "expander:controller:host9:expander-9:0", phys: [3], phy_names: ["phy-9:0:3"], bay_identifier: 17, enclosure_identifier: "5fff0000000000aa" });
    else own.remote_address_state = "ambiguous";
    phyRows(item.fabric, port.expanderId).find((entry) => entry.phy === port.phy.phy).invalid_dword_count = 101;
    const line = said1(model(item), /101 invalid dword/);
    assert.doesNotMatch(line, /^Bay 00/, `${mode}: ${line}`);
    assert.match(line, /owner not read/, `${mode}: ${line}`);
  }
});

test("a CORE handle the builder found on two expanders of the unit names no destination", () => {
  const data = core();
  data.fabric.links = data.fabric.links.filter((link) => !(link.kind === "controller-expander" && link.source === "controller:mpr0"));
  data.fabric.controllers.find((entry) => entry.id === "controller:mpr0").device_handle_states = { 0: "ambiguous", 1: "ambiguous", 2: "ambiguous", 3: "ambiguous" };
  const built = coreModel(data);
  for (const phy of [0, 1, 2, 3]) assert.match(pin(built, "controller:mpr0", phy), /^destination not read: device handle 0009 names more than one device of mpr0/, `PHY ${phy}`);
  assert.doesNotMatch(textOf(cardRows(built, { kind: "node", id: "controller:mpr0" })), /to 5000c50000000e01/);
});

// --- 2: presence before absence ----------------------------------------------------

test("an HBA row's handle a bay's disk carries is that disk, never outside the payload", () => {
  for (const mode of ["bay", "builder state"]) {
    const data = core();
    const controller = data.fabric.controllers.find((entry) => entry.id === "controller:mpr0");
    Object.assign(controller.phy_rows.find((entry) => Number(entry.phy) === 4), { device_handle: "0020", device: "SAS Target", speed: "12.0", min: "3.0", max: "12.0" });
    const device = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => entry.controller === "mpr0");
    if (mode === "bay") Object.assign(device, { handle: "0020", parent: "0001", expander_ids: [] });
    else {
      controller.device_handle_states = { 4: "end_device" };
      device.handle = "00ee";
    }
    const built = coreModel(data);
    const value = pin(built, "controller:mpr0", 4);
    assert.doesNotMatch(value, /outside this view/, `${mode}: ${value}`);
    assert.match(value, mode === "bay" ? /^to the disk in Bay 00 \(da0\)/ : /^a disk the HBA's device list names \(handle 0020\)/, `${mode}: ${value}`);
    assert.doesNotMatch(textOf(cardRows(built, { kind: "node", id: "controller:mpr0" })), /0020 that is not in this enclosure's payload/, mode);
  }
});

// --- 3: one conflict state for every consumer --------------------------------------

test("a record whose id and SAS address name two expanders names no destination", () => {
  const item = twoTargets("covered");
  const records = item.fabric.raw.sas_transport_ports.filter((entry) => entry.owner_id === HOST1 && entry.phys.includes(12));
  records[1].remote_id = records[0].remote_id;
  const built = model(item);
  assert.match(pin(built, HOST1, 12), /destination not read/);
  assert.doesNotMatch(said1(built, /101 invalid dword/), /→/);
});

test("a record whose id names something other than an expander names no destination", () => {
  const item = twoTargets("same");
  for (const entry of item.fabric.raw.sas_transport_ports.filter((port) => port.owner_id === HOST1 && port.phys.includes(12))) entry.remote_id = HOST1;
  const value = pin(model(item), HOST1, 12);
  assert.match(value, /^destination not read: the transport census names [^·]*host1, which is not an expander/, value);
});

test("two read disk records naming two disks on one PHY assign it to no bay", () => {
  const item = direct({ mapped: true });
  item.fabric.raw.sas_transport_ports.push({ ...record(item), id: "sas-port:other-disk", remote_sas_address: "5eee001e00000aaa", bay_identifier: 0 });
  const built = model(item);
  assert.doesNotMatch(pin(built, HOST1, 12), /^Bay 07/);
  const line = said1(built, /101 invalid dword/);
  assert.doesNotMatch(line, /^Bay 07/, line);
  assert.match(line, /the transport census names two disks on it/, line);
});

test("a disputed member supplies no rate to its cable's per-lane readout or drawn label", () => {
  const item = twoTargets("covered");
  Object.assign(phyRows(item.fabric, HOST1).find((entry) => entry.phy === 12), { negotiated_rate_gbps: 3, negotiated_link_rate: "3.0 Gbit", speed: "3.0 Gbit" });
  const built = model(item);
  const link = built.links.find((entry) => entry.source === HOST1 && entry.lanes?.sourcePhys.includes(12));
  const perLane = cardRows(built, { kind: "link", id: link.id }).filter((entry) => entry.label === "Per lane").map((entry) => entry.value).join(" | ");
  assert.doesNotMatch(perLane, /\b3 Gb\/s/, perLane);
  assert.match(perLane, /PHY [^|]*12[^|]*: rate not this cable's/, perLane);
  assert.match((link.labelLines || []).join(" "), /membership not read/, textOf(link.labelLines));
});

test("a retained CORE lane list never overrides its HBA row's handle or the far row's type", () => {
  for (const mode of ["other handle", "visible twin handle", "no far type", "blank far type"]) {
    const data = core();
    const link = data.fabric.links[0];
    const controller = data.fabric.controllers.find((entry) => entry.id === link.source);
    const far = data.fabric.expanders.find((entry) => entry.id === link.target);
    const row = far.phys.find((entry) => Number(entry.phy) === 32);
    Object.assign(row, { invalid_dword_count: 101, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, counter_state: "measured" });
    if (mode === "other handle") controller.phy_rows.find((entry) => Number(entry.phy) === 0).device_handle = "000a";
    if (mode === "visible twin handle") data.fabric.expanders[1].dev_handle = far.dev_handle;
    if (mode === "no far type") delete row.device;
    if (mode === "blank far type") row.device = "";
    const built = coreModel(data);
    assert.ok(!statuses(built, "lanes").includes("pass"), `${mode}: ${textOf(checksOf(built, "lanes"))}`);
    const line = said1(built, /101 invalid dword/);
    assert.doesNotMatch(line, /→/, `${mode}: ${line}`);
    assert.doesNotMatch(cableLane(built, link, 0, 32)[2], /both ends 12|target PHY: 101/, `${mode}: ${cableLane(built, link, 0, 32)[2]}`);
    if (mode === "other handle") assert.doesNotMatch(pin(built, link.source, 0), /^to 5000c50000000e01/, mode);
  }
});

// --- 4: order independence ---------------------------------------------------------

test("which ports of a drive carry faults is the same in any path order", () => {
  const outputs = ["first", "last"].map((order) => critical(samePhyPaths(order), "port-errors"));
  assert.deepEqual(outputs[0], outputs[1]);
  assert.doesNotMatch(outputs[0].join(" "), /both ports/);
  assert.match(outputs[0].join(" "), /which disk port each path uses is not read/);
});

// --- 5: one fault, one finding -----------------------------------------------------

test("an expander fault whose role is not read is one finding, however it is reached", () => {
  for (const mode of ["name only", "unknown kind"]) {
    const item = fixture("wide-x4-sata");
    const chip = expanders(item)[0];
    const row = { ...chip.raw.phys[0], phy: 91, name: "phy-1:0:91", invalid_dword_count: 101, counter_state: "measured", not_read: [] };
    chip.raw.phys.push(row);
    const own = { ...item.fabric.raw.sas_transport_ports.find((entry) => entry.owner_id === chip.id && entry.remote_kind === "end_device"), id: "sas-port:extra", phys: [91], phy_names: [row.name], state: "read" };
    item.fabric.raw.sas_transport_ports.push(own);
    if (mode === "name only") {
      chip.raw.phys.push({ ...row, phy: 92, invalid_dword_count: 0 });
      own.phys = [];
    } else own.remote_kind = "unknown";
    const built = model(item);
    said1(built, /101 invalid dword/);
    // Whether it is a disk port is not read: the disk-port check does not pass.
    assert.ok(!statuses(built, "port-errors").includes("pass"), `${mode}: ${textOf(checksOf(built, "port-errors"))}`);
  }
});

test("an empty alias beside an HBA's PHY table hides no shared name", () => {
  const item = direct();
  record(item).phys = [];
  const rows = phyRows(item.fabric, HOST1);
  rows.push({ ...rows.find((entry) => entry.phy === 12), phy: 13, invalid_dword_count: 0 });
  node(item.fabric, HOST1).raw.phys = [];
  const built = model(item);
  said1(built, /101 invalid dword/);
  for (const id of ["port-errors", "cable-errors"]) assert.ok(!statuses(built, id).includes("pass"), `${id}: ${textOf(checksOf(built, id))}`);
});

test("a disk record naming one unique and one shared PHY name gives neither PHY a disk role", () => {
  const item = direct();
  const rows = phyRows(item.fabric, HOST1);
  rows.push({ ...rows.find((entry) => entry.phy === 12), name: "phy-1:13", phy: 13, invalid_dword_count: 0 }, { ...rows.find((entry) => entry.phy === 12), name: "phy-1:13", phy: 14, invalid_dword_count: 0 });
  record(item).phy_names.push("phy-1:13");
  record(item).phys = [];
  const line = said1(model(item), /101 invalid dword/);
  assert.match(line, /host1: PHY 12/, line);
});

test("a near PHY is not styled below the maximum while the far list is not read", () => {
  const item = fixture("wide-x4-sata");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander" && link.reported_lanes?.lane_count === 4);
  // Its four inferred far members, and one more PHY no census record names.
  const inferred = model(fixture("wide-x4-sata")).links.find((entry) => entry.source === uplink.source && entry.target === uplink.target).lanes.targetPhys;
  const listed = new Set(item.fabric.raw.sas_transport_ports.filter((port) => port.owner_id === uplink.target).flatMap((port) => port.phys || []));
  const spare = phyRows(item.fabric, uplink.target).map((entry) => entry.phy).find((phy) => !inferred.includes(phy) && !listed.has(phy));
  const far = [...inferred, spare];
  Object.assign(uplink.reported_lanes, { target_phys: far, target_basis: "reported" });
  const slowPhy = uplink.reported_lanes.source_phys[1];
  Object.assign(phyRows(item.fabric, uplink.source).find((entry) => entry.phy === slowPhy), { negotiated_rate_gbps: 3, negotiated_link_rate: "3.0 Gbit" });
  const built = model(item);
  const link = built.links.find((entry) => entry.source === uplink.source && entry.target === uplink.target);
  assert.ok(link?.lanes?.targetPhys.length === 5, textOf(link?.lanes));
  const rows = kv(built, { kind: "link", id: link.id }).filter(([, label]) => new RegExp(`PHY ${slowPhy}$`).test(label));
  assert.ok(rows.length, textOf(kv(built, { kind: "link", id: link.id })));
  assert.ok(!rows.some(([cls]) => cls === "topo-warn"), textOf(rows));
  assert.ok(!cardRows(built, { kind: "link", id: link.id }).some((entry) => entry.label === "Per lane" && entry.warn), textOf(cardRows(built, { kind: "link", id: link.id })));
});
