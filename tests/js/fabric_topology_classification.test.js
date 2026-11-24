"use strict";

// One classification of each PHY, from read evidence, before any check
// (#1504): a disk port, a cable member, or not classified; a PHY with both
// kinds of evidence has its role not read and passes neither check. An
// invalid cable list is no evidence. A lane's rate and counters are its own
// observation's. A destination is stated only where read. Duplicate rows are
// resolved the same in any order. A reader is named only for what is true of
// it. Every payload is synthetic, in the producer's shapes.

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

// --- 1: one classification ---------------------------------------------------------

test("an over-long cable list never takes a direct disk's PHY from the disk-port check", () => {
  for (const mapped of [false, true]) {
    const item = direct({ mapped });
    item.fabric.links.find((link) => link.kind === "controller-expander").reported_lanes.source_phys.push(12);
    const built = model(item);
    assert.ok(statuses(built, "port-errors").includes("critical"), `mapped=${mapped}: ${textOf(checksOf(built, "port-errors"))}`);
    const lines = said(built, /101 invalid dword/);
    assert.equal(lines.length, 1, textOf(lines));
    assert.match(lines[0], mapped ? /^Bay 07/ : /host1: PHY 12/);
    const pin = kv(built, { kind: "node", id: HOST1 }).find(([, label]) => label === "PHY 12");
    assert.doesNotMatch(pin[2], /to Expander/, pin[2]);
  }
});

test("an HBA PHY the census calls an expander link, with no row, is an uplink not read", () => {
  const built = model(direct({ kind: "expander", missing: true }));
  assert.ok(!statuses(built, "cable-errors").includes("pass"), textOf(checksOf(built, "cable-errors")));
  assert.match(textOf(checksOf(built, "cable-errors")), /1 of 1 uplink PHY not read/);
});

test("an HBA fault on a PHY of unknown role is said once, never as a disk port's", () => {
  const item = fixture("wide-x4-sata");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  const rows = phyRows(item.fabric, uplink.source);
  rows[0].invalid_dword_count = 101;
  rows[1].phy = rows[0].phy;
  uplink.reported_lanes = null;
  for (const port of item.fabric.raw.sas_transport_ports.filter((entry) => entry.owner_id === uplink.source)) Object.assign(port, { state: "not_read", phy_names: [], phys: [] });
  const built = model(item);
  assert.equal(said(built, /101 invalid dword/).length, 1, textOf(built.checks.filter((entry) => entry.status === "critical")));
  assert.equal(critical(built, "port-errors").length, 0, textOf(critical(built, "port-errors")));
});

// --- 2: per-lane values are the lane's own observation's -----------------------------

test("a pinned cable shows its own observation's rate, whatever another HBA reads", () => {
  for (const count of [0, 101]) {
    const { item, host8 } = threeReaders();
    Object.assign(slow(host8.raw.phys.find((entry) => entry.phy === 28)), { invalid_dword_count: count });
    const built = model(item);
    const link = built.links.find((entry) => entry.target === HOST6 && entry.lanes);
    const row = kv(built, { kind: "link", id: link.id }).find(([, label]) => /PHY 28$/.test(label));
    assert.match(row[2], /^12 Gb\/s/, `count=${count}: ${row[2]}`);
  }
});

test("a pinned bus shows each port's own rate, whatever another HBA reads", () => {
  for (const count of [0, 101]) {
    const { item, host8 } = threeReaders();
    Object.assign(slow(host8.raw.phys.find((entry) => entry.phy === 0)), { invalid_dword_count: count });
    const built = model(item);
    const chip = built.nodes.find((entry) => entry.kind === "expander" && entry.rows?.length === 3);
    const port = built.bays[0].ports.find((entry) => entry.phy?.phy === 0);
    const row = kv(built, { kind: "link", id: chip.busId }).find(([, label]) => label === "Bay 00 · PHY 0");
    assert.match(row[2], /12 Gb\/s/, `count=${count}: ${row[2]}`);
    assert.doesNotMatch(row[2], /\b3 Gb\/s/, row[2]);
    assert.match(textOf(cardRows(built, { kind: "port", id: port.key })), /12 Gb\/s/);
  }
});

// --- 3: destinations only where read ----------------------------------------------------

test("an HBA PHY whose census entry is not read has its destination not read", () => {
  const item = fixture("wide-x4-sata");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  uplink.reported_lanes = null;
  for (const port of item.fabric.raw.sas_transport_ports.filter((entry) => entry.owner_id === uplink.source)) Object.assign(port, { state: "not_read", phy_names: [], phys: [] });
  const built = model(item);
  const card = textOf(cardRows(built, { kind: "node", id: uplink.source }));
  assert.doesNotMatch(card, /not in this enclosure's payload/, card);
  const pins = kv(built, { kind: "node", id: uplink.source }).filter(([, label]) => /^PHY [0-3]$/.test(label));
  for (const [, label, value] of pins) assert.doesNotMatch(value, /outside this view|to Expander/, `${label}: ${value}`);
  assert.ok(pins.some(([, , value]) => /destination not read/.test(value)), textOf(pins));
});

test("a PHY two census records name differently has its destination not read, in either order", () => {
  const outputs = [false, true].map((first) => {
    const item = direct();
    const census = item.fabric.raw.sas_transport_ports;
    const disk = census.find((entry) => entry.owner_id === HOST1 && entry.phys.includes(12));
    const other = { ...disk, id: "sas-port:relation2", remote_kind: "expander", remote_id: item.fabric.nodes.find((entry) => entry.kind === "expander").id, remote_sas_address: null, bay_identifier: null, has_block: null };
    census[first ? "unshift" : "push"](other);
    const built = model(item);
    return [textOf(cardRows(built, { kind: "node", id: HOST1 })), kv(built, { kind: "node", id: HOST1 }).find(([, label]) => label === "PHY 12")[2]];
  });
  assert.deepEqual(outputs[0], outputs[1]);
  assert.doesNotMatch(outputs[0][1], /to Expander|a disk attached directly/, outputs[0][1]);
  // The check says the same in either order, and does not call it a disk port.
  const lines = [false, true].map((first) => {
    const item = direct();
    const census = item.fabric.raw.sas_transport_ports;
    const disk = census.find((entry) => entry.owner_id === HOST1 && entry.phys.includes(12));
    census[first ? "unshift" : "push"]({ ...disk, id: "sas-port:relation2", remote_kind: "expander", remote_id: item.fabric.nodes.find((entry) => entry.kind === "expander").id, remote_sas_address: null, bay_identifier: null, has_block: null });
    return said(model(item), /101 invalid dword/);
  });
  assert.deepEqual(lines[0], lines[1]);
  assert.equal(lines[0].length, 1);
  assert.match(lines[0][0], /role not read/, lines[0][0]);
});

test("a disk on a wide HBA port is its bay's on every PHY of the port", () => {
  const built = model(direct({ mapped: true, phys: [12, 13] }));
  for (const phy of [12, 13]) {
    const pin = kv(built, { kind: "node", id: HOST1 }).find(([, label]) => label === `PHY ${phy}`);
    assert.match(pin[2], /Bay 07/, pin[2]);
  }
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1);
  assert.match(lines[0], /^Bay 07/, lines[0]);
  assert.doesNotMatch(textOf(built.transportNote), /on its expander PHY/);
  assert.doesNotMatch(textOf(checksOf(built, "port-errors")), /since the expander last reset/);
});

// --- 4: duplicate rows, any order ------------------------------------------------------

test("an HBA PHY on two rows gives its lane no maximum, in either order", () => {
  const outputs = [false, true].map((first) => {
    const data = structuredClone(H.producerCases().core_cascade);
    const link = data.fabric.links[0];
    const controller = data.fabric.controllers.find((entry) => entry.id === link.source);
    const row = controller.phy_rows.find((entry) => Number(entry.phy) === 0);
    controller.phy_rows[first ? "unshift" : "push"]({ ...row, max: "3.0", speed: "3.0" });
    link.reported_lanes.lanes[0].negotiated_rate_gbps = 3;
    const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
    return { lanes: checksOf(built, "lanes"), pin: kv(built, { kind: "link", id: link.id }).filter(([, label]) => label.startsWith("Lane PHY 0")) };
  });
  assert.deepEqual(outputs[0], outputs[1]);
  assert.ok(!outputs[0].lanes.some((entry) => ["pass", "critical"].includes(entry.status)), textOf(outputs[0].lanes));
});

test("a pinned lane whose membership is not read states no maximum", () => {
  const data = structuredClone(H.producerCases().core_cascade);
  const link = data.fabric.links.find((entry) => entry.reported_lanes);
  Object.assign(link.reported_lanes.lanes[0], { target_phy: 0, negotiated_rate_gbps: 3 });
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  const row = kv(built, { kind: "link", id: link.id }).find(([, label]) => label.startsWith("Lane PHY 0"));
  assert.doesNotMatch(row[2], /both ends/, row[2]);
});

// --- 5: provenance ----------------------------------------------------------------------

test("a reader that has one fully read row is never named as missing or duplicated", () => {
  const { item, host8 } = threeReaders();
  host8.raw.phys.find((entry) => entry.phy === 29).phy = 28;
  const built = model(item);
  const chip = built.nodes.find((entry) => entry.kind === "expander" && entry.rows?.length === 3);
  const via = cardRows(built, { kind: "node", id: chip.id }).find((entry) => /Also read via/.test(entry.label)).value;
  assert.match(via, /host8: counters missing or on more than one row on PHY [^;]*28/, via);
  assert.doesNotMatch(via, /host7: [^;]*missing/, via);
});

test("a bay's PHY whose own row names an enclosure processor is not the disk's", () => {
  const data = structuredClone(DIRECT);
  const expander = data.fabric.expanders[0];
  const device = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => (entry.expander_ids || []).includes(expander.id));
  expander.phys.push({ ...expander.phys[0], phy: "4", device: "SEP Target", dev_handle: device.handle, counter_state: "measured",
    invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /owner not read/);
  assert.doesNotMatch(cardRows(built, { kind: "node", id: expander.id }).find((entry) => entry.label === "PHY errors").value, /PHY 4 \(Bay 00\)/);
  assert.ok(!kv(built, { kind: "bay", id: "bay:0" }).some(([cls, , value]) => cls && /101/.test(value)));
});

test("placement names the expander-routed paths as such beside a direct one", () => {
  const data = structuredClone(DIRECT);
  const controller = data.fabric.controllers.find((entry) => entry.id === "controller:mpr0");
  controller.phy_rows = [{ phy: "2", device_handle: "0030", device: "SAS Target", speed: "12.0", min: "1.5", max: "12.0" }];
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  Object.assign(metrics.mpr_devices[0], { expander_ids: ["controller:mpr0"], expander_phy: 2, handle: null, parent: null });
  metrics.mpr_devices.push({ ...metrics.mpr_devices[1], member_device_name: "da31", handle: "0091" });
  metrics.path_states.push({ ...metrics.path_states[1], device_name: "da31" });
  const text = textOf(checksOf(T.buildTopologyModel(data.fabric, { inventory: data.snapshot }), "ports"));
  assert.doesNotMatch(text, /every path runs through/, text);
});

test("a bus names a fault on its spare PHY by the bay of another board that claims it", () => {
  const data = structuredClone(H.producerCases().core_cascade);
  const fabric = data.fabric;
  const options = { inventory: { selected_system_id: fabric.system_id, selected_enclosure_id: fabric.selected_enclosure_id, layout_rows: [[0, 1, 2, 3], [4, 5, 6, 7]] } };
  const device = fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices[0];
  const expander = fabric.expanders.find((entry) => entry.id.endsWith("0e02"));
  Object.assign(device, { parent: expander.dev_handle, expander_ids: [expander.id], handle: "009a" });
  Object.assign(expander.phys.find((entry) => Number(entry.phy) === 6), { device: "SAS Target", dev_handle: "009a", speed: "12.0",
    invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0, counter_state: "measured" });
  const built = T.buildTopologyModel(fabric, options);
  const bus = cardRows(built, { kind: "link", id: built.nodeById.get(expander.id).busId }).find((entry) => entry.label === "PHY errors");
  assert.match(bus.value, /Bay 00 PHY 6: 101 invalid dword/, bus.value);
});

// --- each classification guard observed ----------------------------------------------

test("a bay's PHY a read census record calls an expander link is owner not read, from the census alone", () => {
  const item = fixture("fanout-2");
  const port = model(item).bays[0].ports[0];
  item.fabric.raw.sas_transport_ports.push({ id: "sas-port:extra-link", owner_id: port.expanderId, state: "read", phy_names: ["phy-not-a-row"], phys: [port.phy.phy],
    remote_kind: "expander", remote_id: null, remote_sas_address: null, has_block: null, bay_identifier: null, enclosure_identifier: null, target_basis: "reported" });
  const built = model(item);
  const errors = cardRows(built, { kind: "port", id: built.bays[0].ports[0].key }).find((entry) => entry.label === "PHY errors");
  assert.match(errors.value, /owner not read: Bay 00 claims it, but its row or the transport census calls it a link/, errors.value);
});

test("a cable's PHY a read census record names a disk on is owner not read, from the census alone", () => {
  const item = direct();
  const record = item.fabric.raw.sas_transport_ports.find((entry) => entry.owner_id === HOST1 && entry.phys.includes(12));
  record.phy_names = ["phy-not-a-row"];
  item.fabric.links.find((link) => link.kind === "controller-expander").reported_lanes.source_phys[0] = 12;
  const built = model(item);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /owner not read: the cable to [^)]* claims it, but its row or the transport census names a disk on it/);
  // The census names a disk: the disk-port check says it, and does not pass.
  assert.equal(critical(built, "port-errors").filter((line) => /101 invalid dword/.test(line)).length, 1, textOf(checksOf(built, "port-errors")));
});

test("a bay's PHY a read census record names as an expander link by PHY name only is owner not read", () => {
  const item = fixture("fanout-2");
  const port = model(item).bays[0].ports[0];
  const row = phyRows(item.fabric, port.expanderId).find((entry) => entry.phy === port.phy.phy);
  item.fabric.raw.sas_transport_ports.push({ id: "sas-port:extra-named", owner_id: port.expanderId, state: "read", phy_names: [row.name], phys: [],
    remote_kind: "expander", remote_id: null, remote_sas_address: null, has_block: null, bay_identifier: null, enclosure_identifier: null, target_basis: "reported" });
  const built = model(item);
  const errors = cardRows(built, { kind: "port", id: built.bays[0].ports[0].key }).find((entry) => entry.label === "PHY errors");
  assert.match(errors.value, /owner not read: Bay 00 claims it, but its row or the transport census calls it a link/, errors.value);
  // The same record not read names no role: Bay 00 keeps its PHY.
  item.fabric.raw.sas_transport_ports.at(-1).state = "not_read";
  const kept = model(item);
  assert.doesNotMatch(cardRows(kept, { kind: "port", id: kept.bays[0].ports[0].key }).find((entry) => entry.label === "PHY errors").value, /owner not read/);
});

test("a pinned lane shows its far observation's counters, never another observation's", () => {
  // host6's cable with its lanes paired PHY by PHY to the shared chip;
  // host7's observation of far PHY 28 reads 101.
  const item = fixture("dual-domain-linux");
  const link = item.fabric.links.find((entry) => entry.kind === "controller-expander" && entry.target === HOST6);
  const near = link.reported_lanes.source_phys;
  const far = near.map((_, index) => 28 + index);
  Object.assign(link.reported_lanes, { target_phys: far, lanes: near.map((phy, index) => ({ source_phy: phy, target_phy: far[index], negotiated_rate_gbps: 12 })) });
  phyRows(item.fabric, "expander:controller:host7:expander-7:0").find((entry) => entry.phy === 28).invalid_dword_count = 101;
  const built = model(item);
  const lane = kv(built, { kind: "link", id: link.id }).find(([, label]) => label === `Lane PHY ${near[0]} ↔ 28`);
  assert.ok(lane, textOf(kv(built, { kind: "link", id: link.id })));
  assert.doesNotMatch(lane[2].split("; via")[0], /101 invalid dword/, lane[2]);
  assert.match(lane[2], /via host7 this PHY reads 101 invalid dword/, lane[2]);
});

test("a census record that is not read names no destination, whatever PHYs it lists", () => {
  const item = fixture("wide-x4-sata");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  uplink.reported_lanes = null;
  for (const port of item.fabric.raw.sas_transport_ports.filter((entry) => entry.owner_id === uplink.source)) port.state = "not_read";
  const pins = kv(model(item), { kind: "node", id: uplink.source }).filter(([, label]) => /^PHY [0-3]$/.test(label));
  for (const [, label, value] of pins) assert.match(value, /destination not read/, `${label}: ${value}`);
});

test("an HBA card's lanes in this view are the ones its cables own, never an over-long list's", () => {
  const item = direct();
  item.fabric.links.find((link) => link.kind === "controller-expander").reported_lanes.source_phys.push(12);
  const lanes = cardRows(model(item), { kind: "node", id: HOST1 }).find((entry) => entry.label === "In this view");
  assert.doesNotMatch(lanes.value.split(":")[0], /\b12\b/, lanes.value);
});
