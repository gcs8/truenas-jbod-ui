"use strict";

// One role ledger per physical PHY (#1504): every read census record (by
// PHY number or by a PHY name that resolves to one row), every row's device
// type from one shared vocabulary, and every bay or cable claim, merged
// across every observation of the chip. Evidence of two roles is "role not
// read" in every check, card and pin; a record not read is unread evidence,
// never a role or a destination. A wide port is one disk port. Every payload
// is synthetic, in the producer's shapes.

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

// --- 1: census identity -------------------------------------------------------------

test("a disk the census names by its PHY name alone keeps its fault in the disk-port check", () => {
  const item = direct();
  record(item).phys = [];
  const built = model(item);
  assert.ok(statuses(built, "port-errors").includes("critical"), textOf(checksOf(built, "port-errors")));
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /host1: PHY 12, a disk port/);
});

test("a census record not read is unread evidence: no role, no destination, its fault said once", () => {
  const item = direct();
  record(item).state = "not_read";
  const built = model(item);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(built.checks.filter((entry) => entry.status === "critical")));
  assert.equal(critical(built, "port-errors").length, 0, textOf(critical(built, "port-errors")));
  assert.match(lines[0], /role not read: its transport census record was not read/, lines[0]);
  assert.match(pin(built, HOST1, 12), /destination not read/);
  // Whether it is a disk port is not read: the disk-port check does not pass.
  assert.ok(!statuses(built, "port-errors").includes("pass"), textOf(checksOf(built, "port-errors")));
  assert.match(textOf(checksOf(built, "port-errors")), /host1: PHY 12 \(role not read: its transport census record was not read\), so whether it is a disk port is not read/);
});

// --- 2: one role ledger per physical PHY -------------------------------------------

test("a PHY read records name as a disk and as a link has its role not read, and passes neither check", () => {
  const item = direct({ count: 0 });
  item.fabric.raw.sas_transport_ports.push({ ...record(item), id: "sas-port:second", remote_kind: "expander", remote_id: item.fabric.nodes.find((entry) => entry.kind === "expander").id, has_block: null, remote_sas_address: null });
  const built = model(item);
  for (const id of ["port-errors", "cable-errors"]) {
    assert.ok(!statuses(built, id).includes("pass"), `${id}: ${textOf(checksOf(built, id))}`);
    assert.match(textOf(checksOf(built, id)), /PHY 12[^"]*role not read/, id);
  }
  assert.doesNotMatch(textOf(checksOf(built, "port-errors")), /no errors on 12 disk port PHYs/);
  assert.match(pin(built, HOST1, 12), /role not read|destination not read/);
});

test("role evidence on one HBA's observation of a chip governs every observation of it", () => {
  const { item, host8 } = threeReaders();
  Object.assign(slow(host8.raw.phys.find((entry) => entry.phy === 28)), { invalid_dword_count: 101 });
  const census = item.fabric.raw.sas_transport_ports;
  census.push({ ...census.find((entry) => entry.remote_kind === "end_device"), id: "sas-port:extra-disk", owner_id: host8.id, phys: [28], phy_names: ["phy-not-a-row"], remote_kind: "end_device", state: "read" });
  const built = model(item);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /owner not read/);
  const link = built.links.find((entry) => entry.target === HOST6 && entry.lanes);
  const row = kv(built, { kind: "link", id: link.id }).find(([, label]) => /PHY 28$/.test(label));
  assert.doesNotMatch(row[2], /101 invalid dword/, row[2]);
});

test("a CORE row naming an expander by any of the parser's device types is a link, never a bay's disk", () => {
  for (const device of ["Edge Expander", "Fanout Expander", "Expander", "SMP Target", "SAS Initiator", "SATA Initiator"]) {
    const { built, expander } = coreRole(device);
    const lines = said(built, /101 invalid dword/);
    assert.equal(lines.length, 1, `${device}: ${textOf(lines)}`);
    assert.match(lines[0], /owner not read/, device);
    assert.doesNotMatch(cardRows(built, { kind: "node", id: expander.id }).find((entry) => entry.label === "PHY errors").value, /PHY 4 \(Bay 00\)/, device);
  }
});

test("a device type outside the vocabulary is role not read, never a bay's disk", () => {
  const { built } = coreRole("Frobnicator Device");
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /owner not read[^:]*: [^)]*Frobnicator Device/, lines[0]);
});

test("a cable's far PHY whose own row names an enclosure processor is not the cable's", () => {
  const data = core();
  const link = data.fabric.links[0];
  const row = data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32);
  Object.assign(row, { device: "SEP Target", invalid_dword_count: 101, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, counter_state: "measured" });
  const built = coreModel(data);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /owner not read: the cable from mpr0 claims it, but its row or the transport census calls it an enclosure processor/, lines[0]);
  const lane = kv(built, { kind: "link", id: link.id }).find(([, label]) => label === "Lane PHY 0 ↔ 32");
  assert.doesNotMatch(lane[2], /target PHY: 101/, lane[2]);
});

test("every device type mprutil prints, and every expander type the CORE builder reads, has a role", () => {
  const read = (file) => fs.readFileSync(path.join(__dirname, "..", "..", file), "utf8");
  const types = [...read("app/services/sas_fabric_parsers.py").match(/MPR_DEVICE_TYPES = frozenset\(\s*\{([^}]*)\}/)[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  const expanders = [...read("app/services/sas_fabric_core.py").match(/_MPR_EXPANDER_ATTACHED_DEVICES = frozenset\(\{([^}]*)\}/)[1].matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  assert.ok(types.length >= 10 && expanders.length >= 4, textOf([types, expanders]));
  for (const device of [...types, ...expanders]) assert.notEqual(T.deviceRole(device), "unknown", device);
  for (const device of expanders) assert.equal(T.deviceRole(device), "link", device);
  assert.equal(T.deviceRole("SEP Target"), "sep");
  assert.equal(T.deviceRole("Frobnicator Device"), "unknown");
});

// --- 3: destinations only from read records ------------------------------------------

test("an HBA card names no second cable for a PHY one cable lists", () => {
  const item = direct();
  item.fabric.links.find((link) => link.kind === "controller-expander").reported_lanes.source_phys[0] = 12;
  const card = textOf(cardRows(model(item), { kind: "node", id: HOST1 }));
  assert.doesNotMatch(card, /more than one cable's PHY list/, card);
  assert.match(card, /PHY 12: owner not read: the cable to [^;"]* claims it, but its row or the transport census names a disk on it/, card);
  const data = core();
  const controller = data.fabric.controllers.find((entry) => entry.id === data.fabric.links[0].source);
  controller.phy_rows.push({ ...controller.phy_rows.find((entry) => Number(entry.phy) === 0), max: "3.0" });
  const coreCard = textOf(cardRows(coreModel(data), { kind: "node", id: controller.id }));
  assert.doesNotMatch(coreCard, /more than one cable's PHY list/, coreCard);
  assert.match(coreCard, /PHY 0: owner not read: its number is on more than one row of its HBA/, coreCard);
});

test("lanes whose pairing is refused still go to the expander the HBA's own handle names", () => {
  const data = core();
  const link = data.fabric.links.find((entry) => entry.reported_lanes);
  Object.assign(link.reported_lanes.lanes[0], { target_phy: 0, negotiated_rate_gbps: 3 });
  const built = coreModel(data);
  const card = textOf(cardRows(built, { kind: "node", id: link.source }));
  assert.doesNotMatch(card, /not in this enclosure's payload/, card);
  for (const phy of [0, 1, 2, 3]) {
    assert.doesNotMatch(pin(built, link.source, phy), /outside this view/, `PHY ${phy}`);
    assert.match(pin(built, link.source, phy), /to 5000c50000000e01/, `PHY ${phy}`);
  }
});

test("two read records naming two present expanders give destination not read, in either order", () => {
  const outputs = [false, true].map((first) => {
    const item = direct({ kind: "expander" });
    const expanders = item.fabric.nodes.filter((entry) => entry.kind === "expander");
    const census = item.fabric.raw.sas_transport_ports;
    Object.assign(record(item), { remote_id: expanders[0].id });
    census[first ? "unshift" : "push"]({ ...record(item), id: "sas-port:relation-2", remote_id: expanders[1].id });
    const built = model(item);
    return [pin(built, HOST1, 12), textOf(cardRows(built, { kind: "node", id: HOST1 }))];
  });
  assert.deepEqual(outputs[0], outputs[1]);
  assert.match(outputs[0][0], /destination not read/);
  assert.doesNotMatch(outputs[0][0], /to Expander/);
});

test("a direct disk joined to no bay is never said to be outside the view", () => {
  const item = direct();
  Object.assign(record(item), { bay_identifier: 7, enclosure_identifier: item.fabric.selected_enclosure_id || null });
  const built = model(item);
  assert.doesNotMatch(pin(built, HOST1, 12), /in no bay of this view|outside/, pin(built, HOST1, 12));
  assert.doesNotMatch(textOf(cardRows(built, { kind: "node", id: HOST1 })), /in no bay of this view|outside this view/);
});

test("an HBA uplink in no cable with a fault is said once", () => {
  const item = direct({ kind: "expander" });
  const built = model(item);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /host1: PHY 12, attached to another expander/);
});

// --- 4: a wide port is one disk port ---------------------------------------------------

test("faults on two PHYs of one wide port are one port's, never both ports of the drive", () => {
  const item = direct({ mapped: true, phys: [12, 13] });
  phyRows(item.fabric, HOST1).find((entry) => entry.phy === 13).invalid_dword_count = 102;
  const built = model(item);
  const lines = critical(built, "port-errors");
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /^Bay 07 \(wdfresh\), one port: host1 PHY 12: 101 invalid dword; host1 PHY 13: 102 invalid dword/, lines[0]);
  assert.doesNotMatch(textOf(checksOf(built, "port-errors")), /both ports/);
});

test("a wide port whose PHYs a read record calls expander links does not pass placement", () => {
  const item = direct({ mapped: true, phys: [12, 13], count: 0 });
  item.fabric.raw.sas_transport_ports.push({ ...record(item), id: "sas-port:extra", remote_kind: "expander", remote_id: null, has_block: null, remote_sas_address: null, bay_identifier: null });
  const built = model(item);
  assert.ok(!statuses(built, "ports").includes("pass"), textOf(checksOf(built, "ports")));
  assert.ok(!statuses(built, "port-errors").includes("pass"), textOf(checksOf(built, "port-errors")));
});

// --- 5: mixed paths say only the ports in use ---------------------------------------------

test("a direct path and expander paths on two disk ports are never one port in use", () => {
  const data = structuredClone(DIRECT);
  const controller = data.fabric.controllers.find((entry) => entry.id === "controller:mpr0");
  controller.phy_rows = [{ phy: "2", device_handle: "0030", device: "SAS Target", speed: "12.0", min: "1.5", max: "12.0" }];
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  Object.assign(metrics.mpr_devices[0], { expander_ids: ["controller:mpr0"], expander_phy: 2, handle: null, parent: null });
  metrics.mpr_devices.push({ ...metrics.mpr_devices[1], member_device_name: "da31", handle: "0091" });
  metrics.path_states.push({ ...metrics.path_states[1], device_name: "da31" });
  for (const row of [...metrics.path_states, ...metrics.mpr_devices]) row.state = "active";
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  assert.deepEqual([...new Set(built.bays[0].ports.map((port) => port.sasAddress))].length, 2);
  assert.doesNotMatch(textOf(checksOf(built, "ports")), /one of its two ports is in use/);
});

test("beside a direct path, which disk ports the paths use is not read where an address is not", () => {
  const data = structuredClone(DIRECT);
  const controller = data.fabric.controllers.find((entry) => entry.id === "controller:mpr0");
  controller.phy_rows = [{ phy: "2", device_handle: "0030", device: "SAS Target", speed: "12.0", min: "1.5", max: "12.0" }];
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  Object.assign(metrics.mpr_devices[0], { expander_ids: ["controller:mpr0"], expander_phy: 2, handle: null, parent: null });
  const extra = { ...metrics.mpr_devices[1], member_device_name: "da31", handle: "0091" };
  delete extra.sas_address;
  metrics.mpr_devices.push(extra);
  const state = { ...metrics.path_states[1], device_name: "da31" };
  delete state.sas_address;
  metrics.path_states.push(state);
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  assert.ok(built.bays[0].ports.some((port) => !port.sasAddress), "one path's disk port address is not read");
  assert.ok(!statuses(built, "ports").includes("pass"), textOf(checksOf(built, "ports")));
  assert.match(textOf(checksOf(built, "ports")), /which of its disk ports its direct and expander paths use is not read/);
});

test("a role the second observation of a chip reads governs the PHY as much as the first's", () => {
  // CORE, two boards: mpr1 also observes mpr0's expander …0e01. Only
  // mpr1's row for Bay 00's PHY names an expander on it.
  const data = core();
  const fabric = data.fabric;
  const options = { inventory: { selected_system_id: fabric.system_id, selected_enclosure_id: fabric.selected_enclosure_id, layout_rows: [[0, 1, 2, 3], [4, 5, 6, 7]] } };
  const first = fabric.expanders.find((entry) => entry.id.endsWith("mpr0:5000c50000000e01"));
  const device = fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => entry.expander_ids.includes(first.id));
  Object.assign(first.phys.find((entry) => Number(entry.phy) === 6), { device: "SAS Target", dev_handle: device.handle, speed: "12.0" });
  const id = first.id.replace("mpr0", "mpr1");
  const second = { ...structuredClone(first), id };
  fabric.expanders.push(second);
  const original = fabric.nodes.find((entry) => entry.id === first.id);
  fabric.nodes.push({ ...structuredClone(original), id, controller_id: "controller:mpr1", raw: { ...structuredClone(original.raw), controller_id: "controller:mpr1" } });
  const before = T.buildTopologyModel(structuredClone(fabric), options);
  const port = before.bays[0].ports.find((entry) => entry.expanderId === first.id);
  assert.ok(port?.phy, "Bay 00 has a joined port on …0e01");
  assert.ok(before.nodes.some((entry) => entry.rows?.length === 2), "the two observations are one chip");
  second.phys.find((entry) => Number(entry.phy) === port.phy.phy).device = "Edge Expander";
  const built = T.buildTopologyModel(fabric, options);
  const errors = cardRows(built, { kind: "port", id: built.bays[0].ports.find((entry) => entry.expanderId === first.id).key }).find((entry) => entry.label === "PHY errors");
  assert.match(errors.value, /owner not read: Bay 00 claims it, but its row or the transport census calls it a link/, errors.value);
});

test("a pinned chip row names every role its observations read, a bay's and the census's", () => {
  const item = fixture("fanout-2");
  const port = model(item).bays[0].ports[0];
  const row = phyRows(item.fabric, port.expanderId).find((entry) => entry.phy === port.phy.phy);
  Object.assign(row, { negotiated_rate_gbps: null, negotiated_link_rate: null, speed: null });
  item.fabric.raw.sas_transport_ports.unshift({ id: "sas-port:extra-named", owner_id: port.expanderId, state: "read", phy_names: [row.name], phys: [],
    remote_kind: "expander", remote_id: null, remote_sas_address: null, has_block: null, bay_identifier: null, enclosure_identifier: null, target_basis: "reported" });
  const built = model(item);
  const chip = built.nodeById.get(port.expanderId)?.mergedInto || port.expanderId;
  const pinned = kv(built, { kind: "node", id: chip }).find(([, label]) => label === `PHY ${port.phy.phy}`);
  assert.ok(pinned, textOf(kv(built, { kind: "node", id: chip })));
  assert.match(pinned[2], /SMP Target/, pinned[2]);
  assert.match(pinned[2], /SAS Target|SATA Target/, pinned[2]);
});

test("a read expander record that names no present target gives destination not read", () => {
  const built = model(direct({ kind: "expander", count: 0 }));
  assert.match(pin(built, HOST1, 12), /^destination not read/, pin(built, HOST1, 12));
});

test("read records naming an expander and an attachment not read give destination not read", () => {
  const item = direct({ kind: "expander", count: 0 });
  Object.assign(record(item), { remote_id: item.fabric.nodes.find((entry) => entry.kind === "expander").id });
  item.fabric.raw.sas_transport_ports.push({ ...record(item), id: "sas-port:relation-2", remote_kind: "unknown", remote_id: null });
  const value = pin(model(item), HOST1, 12);
  assert.match(value, /^destination not read: the transport census lists it as expander and as unknown/, value);
});
