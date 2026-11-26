"use strict";

// What was not read never resolves a subject (#1504): an unread reply never
// relaxes an ambiguity or confirms a single device; one measured observation
// is said once, on the subject that owns it; a record with no location never
// joins disks across enclosures; a lane is judged only where both ends' own
// rows are read and name each other as the only ends of that link. Every
// payload is synthetic, in the producer's shapes; the CORE handle states
// are the ones the Python builder emits (tests/test_fabric_topology_fixture.py
// holds it to them).

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const T = H.loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;
const DIRECT = require("../fixtures/sas_fabric/core_direct_paths.json");

const fixture = (id) => structuredClone(FIXTURES.find((item) => item.id === id));
const model = (item) => T.buildTopologyModel(item.fabric, item.options || { inventory: item.snapshot });
const checksOf = (built, id) => built.checks.filter((entry) => entry.id === id || entry.id.startsWith(`${id}:`));
const statuses = (built, id) => checksOf(built, id).map((entry) => entry.status);
const critical = (built, id) => checksOf(built, id).filter((entry) => entry.status === "critical").flatMap((entry) => entry.lines);
const said = (built, pattern) => [...critical(built, "cable-errors"), ...critical(built, "port-errors")].filter((line) => pattern.test(line));
const textOf = (value) => JSON.stringify(value);
const node = (fabric, id) => fabric.nodes.find((entry) => entry.id === id);
const phyRows = (fabric, id) => node(fabric, id).raw.phys || node(fabric, id).raw.phy_rows;
const cardRows = (built, ref) => T.cardFor(built, ref)?.rows || [];
const unescape = (value) => value.replaceAll("&#39;", "'").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", "\"").replaceAll("&amp;", "&");
const kv = (built, ref) => [...(T.detailFor(built, ref)?.html || "").matchAll(/<div class="kv-row([^"]*)"[^>]*><span[^>]*>([^<]*)<\/span><span[^>]*>([^<]*)<\/span>/g)]
  .map((match) => [match[1].trim(), unescape(match[2]), unescape(match[3])]);
const pin = (built, id, phy) => kv(built, { kind: "node", id }).filter(([, label]) => label === `PHY ${phy}`).map(([, , value]) => value).join(" | ");
const said1 = (built, pattern) => {
  const lines = said(built, pattern);
  assert.equal(lines.length, 1, textOf(built.checks.filter((entry) => entry.status === "critical")));
  return lines[0];
};
const core = () => structuredClone(H.producerCases().core_cascade);
const coreModel = (data) => T.buildTopologyModel(data.fabric, data.options || { inventory: data.snapshot });
const MPR0 = "controller:mpr0";
const controllerOf = (data) => data.fabric.controllers.find((entry) => entry.id === MPR0);
const hbaRow = (data, phy) => controllerOf(data).phy_rows.find((entry) => Number(entry.phy) === phy);
const mprDevice = (data) => data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => entry.controller === "mpr0");
const hbaCard = (built) => textOf(cardRows(built, { kind: "node", id: MPR0 }));

// --- 1: an unread reply never resolves a handle ------------------------------------

test("a handle the builder could not resolve because a reply went unread is not read, never a direct disk", () => {
  for (const state of ["ambiguous", "not_read"]) {
    const data = core();
    Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "0020", device: "SAS Target", speed: "12.0" });
    Object.assign(mprDevice(data), { parent: "0001", expander_ids: [] });
    controllerOf(data).device_handle_states = { 4: state };
    controllerOf(data).mpr_lists = { expanders: "not_read", devices: "read", enclosures: "read" };
    const built = coreModel(data);
    const value = pin(built, MPR0, 4);
    assert.match(value, /^destination not read/, `${state}: ${value}`);
    assert.doesNotMatch(hbaCard(built), /attached directly/, state);
  }
});

test("which lists of an HBA were read is found by the HBA, whatever its Friendly Label", () => {
  const data = core();
  Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "009b", device: "Edge Expander", speed: "12.0" });
  delete data.fabric.raw.controller_read;
  controllerOf(data).mpr_lists = { expanders: "read", devices: "not_read", enclosures: "read" };
  for (const entry of [...data.fabric.nodes, ...data.fabric.controllers].filter((item) => item.id === MPR0)) entry.display_label = "Shelf HBA";
  const built = coreModel(data);
  assert.match(pin(built, MPR0, 4), /^destination not read: .*devices list was not read/);
  assert.doesNotMatch(hbaCard(built), /009b that is not in this enclosure's payload/);
});

test("an expander only the device list names is not outside this view", () => {
  const data = core();
  Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "009b", device: "Edge Expander", speed: "12.0" });
  controllerOf(data).device_handle_states = { 4: "unlisted_expander" };
  const built = coreModel(data);
  const value = pin(built, MPR0, 4);
  assert.match(value, /^destination not read: device handle 009b names an expander/, value);
  assert.doesNotMatch(hbaCard(built), /009b that is not in this enclosure's payload/);
});

test("a disk handle two devices of the unit carry claims no PHY, though only one of them is a bay", () => {
  for (const mode of ["control", "ambiguous"]) {
    const data = structuredClone(DIRECT);
    const expander = data.fabric.expanders[0];
    const port = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => (entry.expander_ids || []).includes(expander.id));
    expander.phys.push({ ...expander.phys[0], phy: "4", device: "SAS Target", dev_handle: port.handle, counter_state: "measured",
      invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
    if (mode === "ambiguous") data.fabric.controllers.find((entry) => entry.id === `controller:${port.controller}`).ambiguous_device_handles = [port.handle];
    const built = coreModel(data);
    const line = said1(built, /101 invalid dword/);
    if (mode === "control") {
      assert.match(line, /^Bay 00/, line);
      continue;
    }
    assert.match(line, new RegExp(`owner not read: Bay 00 claims it, but its device handle ${port.handle} names more than one device`), line);
    const key = built.bays[0].ports.find((item) => item.phy?.phy === 4).key;
    const errors = cardRows(built, { kind: "port", id: key }).find((entry) => entry.label === "PHY errors");
    assert.doesNotMatch(errors.value, /101/, errors.value);
  }
});

// --- 2: one measured observation is said once --------------------------------------

function samePhyPaths(order) {
  const data = structuredClone(DIRECT);
  const metrics = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics;
  for (const row of [...metrics.path_states, ...metrics.mpr_devices]) row.state = "active";
  const expander = data.fabric.expanders[1];
  expander.phys.push({ ...expander.phys[0], phy: "5", dev_handle: "0020", device: "SAS Target",
    invalid_dword_count: 101, phy_reset_problem_count: 0, loss_of_dword_synchronization_count: 0, running_disparity_error_count: 0, counter_state: "measured" });
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

test("a bay's hover never charges one PHY's counters to two alternative paths", () => {
  for (const order of ["first", "last"]) {
    const built = samePhyPaths(order);
    const rows = cardRows(built, { kind: "bay", id: built.bays[0].bayId }).filter((entry) => /da11|da41/.test(entry.label));
    assert.equal(rows.length, 2, textOf(rows));
    for (const row of rows) {
      assert.doesNotMatch(row.value, /101 invalid dword/, `${order}: ${textOf(row)}`);
      assert.notEqual(row.warn, "critical", `${order}: ${textOf(row)}`);
      assert.match(row.value, /not read/, `${order}: ${textOf(row)}`);
    }
    // Said once, on the bay.
    assert.match(said1(built, /101 invalid dword/), /^Bay 00/);
  }
});

// Two addressed bays on one expander; the second's address reported for both.
function sharedAddress() {
  const item = fixture("fanout-2");
  const [first, second] = model(item).bays.filter((bay) => bay.ports.some((port) => port.sasAddress && port.phy));
  const census = item.fabric.raw.sas_transport_ports;
  const own = census.find((entry) => entry.remote_kind === "end_device" && entry.owner_id === second.ports[0].expanderId && (entry.phys || []).includes(second.ports[0].phy.phy));
  own.remote_sas_address = first.ports[0].sasAddress;
  for (const row of Object.values(item.fabric.traces.find((entry) => entry.id === second.bayId).metrics).filter(Array.isArray).flat()) if (row && "sas_address" in row) row.sas_address = first.ports[0].sasAddress;
  phyRows(item.fabric, second.ports[0].expanderId).find((entry) => entry.phy === second.ports[0].phy.phy).invalid_dword_count = 101;
  return { item, second };
}

test("a path whose PHY's owner is not read names that PHY as not shown to be its own, on every surface", () => {
  const { item, second } = sharedAddress();
  const built = model(item);
  const port = built.bayById.get(second.bayId).ports[0];
  const path = cardRows(built, { kind: "port", id: port.key }).find((entry) => entry.label === "Path").value;
  assert.match(path, new RegExp(`PHY ${port.phy.phy} \\(not read as this path's`), path);
  const hover = cardRows(built, { kind: "bay", id: second.bayId }).filter((entry) => entry.label.startsWith("via"));
  assert.ok(hover.length && hover.every((entry) => entry.warn !== "critical" && !/101/.test(entry.value)), textOf(hover));
});

// --- 3: a record with no location joins nothing -------------------------------------

function directMapped() {
  const item = fixture("wide-x4-sata");
  const host = "controller:host1";
  const rows = phyRows(item.fabric, host);
  rows.push({ ...rows[0], name: "phy-1:12", phy: 12, not_read: [], counter_state: "measured", invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const id = "sas-port:port-1:12";
  const record = { id, owner_id: host, state: "read", phy_names: ["phy-1:12"], phys: [12], remote_kind: "end_device", remote_id: null,
    remote_sas_address: "5eee001e00000123", has_block: true, bay_identifier: 7, enclosure_identifier: "5fff0000000000f0", target_basis: "reported" };
  item.fabric.raw.sas_transport_ports.push(record);
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id: host, phy_names: ["phy-1:12"], state: "read" };
  Object.assign(node(item.fabric, "bay:7").metrics, { present: true, device_name: "wdfresh", sas_address: "5eee001e00000123", transport_protocol: "SAS", target_port_protocol: "SSP" });
  const row = { controller: "host1", state: "unknown", device_name: "wdfresh", transport_protocol: "SAS", attached_sas_address: "5aaa000000000101", phy_identifier: 12, target_port_protocol: "SSP",
    sas_address: "5eee001e00000123", member_device_name: "wdfresh", bay_identifier: 7, port: "port-1:12", expander_ids: [], sas_transport_port_id: id, source: "linux_sas_transport" };
  Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:7").metrics, { sas_transport_ports: [row], path_states: [row] });
  return { item, record };
}

test("a record with neither address nor enclosure never merges two disks of different enclosures", () => {
  for (const reverse of [false, true]) {
    const { item, record } = directMapped();
    const census = item.fabric.raw.sas_transport_ports;
    census.push({ ...record, id: "sas-port:other-enclosure", remote_sas_address: "5eee001e00000456", enclosure_identifier: "5fff0000000000aa" },
      { ...record, id: "sas-port:unlocated", remote_sas_address: null, enclosure_identifier: null });
    if (reverse) census.reverse();
    const built = model(item);
    const line = said1(built, /101 invalid dword/);
    assert.doesNotMatch(line, /^Bay 07/, `${reverse}: ${line}`);
    assert.doesNotMatch(pin(built, "controller:host1", 12), /^Bay 07/);
  }
});

test("a bay whose own records report two enclosures is placed in neither", () => {
  const { item } = directMapped();
  item.fabric.traces.find((entry) => entry.id === "bay:7").metrics.enclosure_id = "5fff0000000000aa";
  node(item.fabric, "bay:7").metrics.enclosure_id = "5fff0000000000f0";
  const line = said1(model(item), /101 invalid dword/);
  assert.doesNotMatch(line, /^Bay 07/, line);
});

// --- 4: a lane is judged only on both ends' own rows --------------------------------

function laneCase(mode) {
  const data = core();
  const link = data.fabric.links[0];
  const expander = data.fabric.expanders.find((entry) => entry.id === link.target);
  const far = expander.phys.find((entry) => Number(entry.phy) === 32);
  Object.assign(far, { invalid_dword_count: 101, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, counter_state: "measured" });
  if (mode === "no near rows") {
    controllerOf(data).phy_rows = [];
    far.dev_handle = "000a";
  }
  if (mode === "no pairs") {
    link.reported_lanes.lanes = [];
    far.dev_handle = "000a";
  }
  if (mode === "own handles agree, parent does not") {
    hbaRow(data, 0).controller_handle = "000b";
    far.dev_handle = "000b";
  }
  if (mode === "another expander claims the HBA PHY") {
    const other = data.fabric.expanders.find((entry) => entry.id !== expander.id && entry.controller_id === expander.controller_id);
    other.phys.push({ ...far, phy: "50", invalid_dword_count: 0 });
  }
  if (mode === "far rate not read") far.speed = null;
  if (mode === "both rates not read") {
    far.speed = null;
    hbaRow(data, 0).speed = null;
  }
  return { data, link };
}

test("a retained CORE lane list is not passed, and claims no member's fault, unless both ends' own rows name each other as the only ends of the link", () => {
  for (const mode of ["no near rows", "no pairs", "own handles agree, parent does not", "another expander claims the HBA PHY", "far rate not read", "both rates not read"]) {
    const { data, link } = laneCase(mode);
    const built = coreModel(data);
    assert.ok(!statuses(built, "lanes").includes("pass"), `${mode}: ${textOf(checksOf(built, "lanes"))}`);
    if (!/rate/.test(mode)) {
      assert.doesNotMatch(said1(built, /101 invalid dword/), /→/, mode);
      const lanes = cardRows(built, { kind: "link", id: built.links.find((entry) => entry.source === link.source && entry.target === link.target).id }).find((entry) => entry.label === "Lanes");
      assert.doesNotMatch(lanes.value, /at 12 Gb\/s/, `${mode}: ${lanes.value}`);
    }
  }
  // The control passes, and the cable owns its far fault.
  const { data } = laneCase("control");
  const built = coreModel(data);
  assert.ok(statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
  assert.match(said1(built, /101 invalid dword/), /→/);
});

// --- each rule observed on an input no other rule settles first -------------------

test("an HBA row's handle the builder found on two devices of the unit is not read, even with no row state", () => {
  const data = core();
  Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "0020", device: "SAS Target", speed: "12.0" });
  Object.assign(mprDevice(data), { parent: "0001", expander_ids: [] });
  controllerOf(data).ambiguous_device_handles = ["0020"];
  const value = pin(coreModel(data), MPR0, 4);
  assert.match(value, /^destination not read: device handle 0020 names more than one device/, value);
});

test("two records at one bay number, one enclosure not read, are not said to be two disks", () => {
  const { item, record } = directMapped();
  item.fabric.raw.sas_transport_ports.push({ ...record, id: "sas-port:same-number", remote_sas_address: "5eee001e00000aaa", enclosure_identifier: null });
  const line = said1(model(item), /101 invalid dword/);
  assert.doesNotMatch(line, /two disks|^Bay 07/, line);
  assert.match(line, /do not name one disk/, line);
});

test("a lane listed below its maximum is not judged slow while an end's own rate is not read", () => {
  const data = core();
  const link = data.fabric.links[0];
  const far = data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32);
  far.speed = null;
  hbaRow(data, 0).speed = "3.0";
  link.reported_lanes.lanes[0].negotiated_rate_gbps = 3;
  link.reported_lanes.negotiated_rate_gbps = null;
  const lanes = checksOf(coreModel(data), "lanes");
  assert.ok(!lanes.some((entry) => ["pass", "critical"].includes(entry.status)), textOf(lanes));
  assert.doesNotMatch(textOf(lanes), /below the maximum/);
});

test("a cable whose near end has no PHY table at all claims none of its far end's faults", () => {
  const { data } = laneCase("control");
  data.fabric.controllers = data.fabric.controllers.filter((entry) => entry.id !== MPR0);
  const built = coreModel(data);
  assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
  assert.doesNotMatch(said1(built, /101 invalid dword/), /→/);
});
