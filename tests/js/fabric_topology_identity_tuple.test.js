"use strict";

// A subject is its whole identity across the payload (#1504): which reply
// names an enclosure processor does not make it a disk or absent; a device
// handle, a disk's place and a cable's two ends must each agree in every
// record before a surface names them; a record missing a field is not read
// for that field, never a second subject. Every payload is synthetic, in
// the producer's shapes; the CORE handle states are the ones the Python
// builder emits (tests/test_fabric_topology_fixture.py holds it to them).

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
const MPR0 = "controller:mpr0";
const hbaRow = (data, phy) => data.fabric.controllers.find((entry) => entry.id === MPR0).phy_rows.find((entry) => Number(entry.phy) === phy);
const mprDevice = (data) => data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => entry.controller === "mpr0");
const hbaCard = (built) => textOf(cardRows(built, { kind: "node", id: MPR0 }));

// --- 1: the enclosure processor, whichever reply names it ---------------------------

test("an HBA row's handle the enclosure processor carries is the enclosure processor, never a disk or absent", () => {
  for (const mode of ["builder state", "enclosure list only"]) {
    const data = core();
    Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "0008", device: "SEP Target", speed: "12.0" });
    if (mode === "builder state") data.fabric.controllers.find((entry) => entry.id === MPR0).device_handle_states = { 4: "sep" };
    const built = coreModel(data);
    const value = pin(built, MPR0, 4);
    assert.match(value, /enclosure processor/, `${mode}: ${value}`);
    assert.doesNotMatch(value, /disk|outside this view/, `${mode}: ${value}`);
    assert.doesNotMatch(hbaCard(built), /a disk the HBA|0008 that is not in this enclosure's payload/, mode);
  }
});

test("a handle nothing carries is outside this view only where every list of its HBA was read", () => {
  const data = core();
  Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "009b", device: "SAS Target", speed: "12.0" });
  assert.match(pin(coreModel(data), MPR0, 4), /^outside this view/);
  data.fabric.raw.controller_read.lists.mpr0.devices = "not_read";
  const value = pin(coreModel(data), MPR0, 4);
  assert.match(value, /^destination not read: mpr0's devices list was not read/, value);
  assert.doesNotMatch(hbaCard(coreModel(data)), /009b that is not in this enclosure's payload/);
});

// --- 2: an expander handle that names a second device -------------------------------

test("an expander handle the enclosure processor also carries names neither, and the cable's lanes are not passed", () => {
  const data = core();
  data.fabric.nodes.find((entry) => entry.id === "mpr-enclosure:controller:mpr0:0002").raw.sep_handle = "0009";
  const built = coreModel(data);
  assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
  for (const phy of [0, 1, 2, 3]) {
    const value = pin(built, MPR0, phy);
    assert.match(value, /device handle 0009 names more than one device of mpr0: destination not read/, `PHY ${phy}: ${value}`);
    assert.doesNotMatch(value, /^to 5000c50000000e01/, `PHY ${phy}`);
  }
});

// --- 3: a disk's handle proves neither direct attachment nor the row's type ---------

test("a disk handle whose path runs through an expander, or whose row names another type, is no direct disk", () => {
  for (const mode of ["behind", "type clash", "builder mismatch"]) {
    const data = core();
    Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "0020", device: mode === "type clash" ? "Edge Expander" : "SAS Target", speed: "12.0" });
    if (mode === "type clash") Object.assign(mprDevice(data), { parent: "0001", expander_ids: [] });
    if (mode === "builder mismatch") {
      data.fabric.controllers.find((entry) => entry.id === MPR0).device_handle_states = { 4: "mismatch" };
      mprDevice(data).handle = "00ee";
    }
    const built = coreModel(data);
    const value = pin(built, MPR0, 4);
    assert.match(value, /^destination not read/, `${mode}: ${value}`);
    assert.doesNotMatch(value, /attached directly|a disk the HBA/, `${mode}: ${value}`);
    assert.doesNotMatch(hbaCard(built), /attached directly/, mode);
  }
  // The control: the disk's own parent is this HBA and the row says a disk.
  const data = core();
  Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "0020", device: "SAS Target", speed: "12.0" });
  Object.assign(mprDevice(data), { parent: "0001", expander_ids: [] });
  assert.match(pin(coreModel(data), MPR0, 4), /^to the disk in Bay 00 \(da0\), attached directly/);
});

// --- 4: a disk's place, from every record that reports it ---------------------------

// Two addressed bays on one expander, the second's PHY faulty.
function twoBays() {
  const item = fixture("fanout-2");
  const [first, second] = model(item).bays.filter((bay) => bay.ports.some((port) => port.sasAddress && port.phy));
  const census = item.fabric.raw.sas_transport_ports;
  const own = (bay) => census.find((entry) => entry.remote_kind === "end_device" && entry.owner_id === bay.ports[0].expanderId && (entry.phys || []).includes(bay.ports[0].phy.phy));
  const port = second.ports[0];
  phyRows(item.fabric, port.expanderId).find((entry) => entry.phy === port.phy.phy).invalid_dword_count = 101;
  return { item, first, second, census, own, trace: (bay) => item.fabric.traces.find((entry) => entry.id === bay.bayId) };
}

test("an address another bay's inventory still reports leaves the fault unassigned, though that bay's transport end is not read", () => {
  const { item, first, second, census, own, trace } = twoBays();
  const address = first.ports[0].sasAddress;
  // The first bay's end is not read: no census record, no joined port; its
  // trace still reports the address.
  census.splice(census.indexOf(own(first)), 1);
  const metrics = trace(first).metrics;
  metrics.path_states = metrics.path_states.map((row) => ({ ...row, controller: null, path_id: null, sas_transport_port_id: undefined }));
  metrics.sas_transport_ports = [];
  // The second bay reports the same address, in its census record and trace.
  own(second).remote_sas_address = address;
  for (const row of Object.values(trace(second).metrics).filter(Array.isArray).flat()) if (row && "sas_address" in row) row.sas_address = address;
  const built = model(item);
  const line = said1(built, /101 invalid dword/);
  assert.doesNotMatch(line, new RegExp(`^${second.label}`), line);
  assert.match(line, /owner not read/, line);
  assert.doesNotMatch(pin(built, second.ports[0].expanderId, second.ports[0].phy.phy), new RegExp(`^${second.label}`));
});

test("a disk the transport places in another enclosure than its bay's is no bay's", () => {
  const { item, second, own } = twoBays();
  Object.assign(own(second), { enclosure_identifier: "5fff0000000000aa", bay_identifier: 0 });
  const line = said1(model(item), /101 invalid dword/);
  assert.doesNotMatch(line, new RegExp(`^${second.label}`), line);
  assert.match(line, /owner not read/, line);
});

// A disk attached directly to host1 PHY 12, mapped to Bay 07.
function directMapped() {
  const item = fixture("wide-x4-sata");
  const host = "controller:host1";
  const rows = phyRows(item.fabric, host);
  rows.push({ ...rows[0], name: "phy-1:12", phy: 12, not_read: [], counter_state: "measured", invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const id = "sas-port:port-1:12";
  const record = { id, owner_id: host, state: "read", phy_names: ["phy-1:12"], phys: [12], remote_kind: "end_device", remote_id: null,
    remote_sas_address: "5eee001e00000123", has_block: true, bay_identifier: 7, enclosure_identifier: null, target_basis: "reported" };
  item.fabric.raw.sas_transport_ports.push(record);
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id: host, phy_names: ["phy-1:12"], state: "read" };
  Object.assign(node(item.fabric, "bay:7").metrics, { present: true, device_name: "wdfresh", sas_address: "5eee001e00000123", transport_protocol: "SAS", target_port_protocol: "SSP" });
  const row = { controller: "host1", state: "unknown", device_name: "wdfresh", transport_protocol: "SAS", attached_sas_address: "5aaa000000000101", phy_identifier: 12, target_port_protocol: "SSP",
    sas_address: "5eee001e00000123", member_device_name: "wdfresh", bay_identifier: 7, port: "port-1:12", expander_ids: [], sas_transport_port_id: id, source: "linux_sas_transport" };
  Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:7").metrics, { sas_transport_ports: [row], path_states: [row] });
  return { item, record };
}

test("a PHY's census record naming another disk than the bay's own path is no bay's", () => {
  for (const bay of [0, 7]) {
    const { item, record } = directMapped();
    Object.assign(record, { remote_sas_address: "5eee001e00000aaa", bay_identifier: bay });
    const built = model(item);
    assert.doesNotMatch(pin(built, "controller:host1", 12), /^Bay 07/, `bay ${bay}`);
    const line = said1(built, /101 invalid dword/);
    assert.doesNotMatch(line, /^Bay 07/, line);
    // A transport bay number is not comparable with a bay of this view:
    // the records disagree, which is not a count of disks.
    assert.doesNotMatch(line, /two disks/, line);
    assert.match(line, /do not name one disk/, line);
  }
});

// --- 5: a disputed owner reaches every disk-port readout ----------------------------

test("a disk port whose PHY's owner is not read shows neither the PHY's rate nor its counters as its own", () => {
  const { item, first, second, own, trace } = twoBays();
  const address = first.ports[0].sasAddress;
  own(second).remote_sas_address = address;
  for (const row of Object.values(trace(second).metrics).filter(Array.isArray).flat()) if (row && "sas_address" in row) row.sas_address = address;
  Object.assign(phyRows(item.fabric, second.ports[0].expanderId).find((entry) => entry.phy === second.ports[0].phy.phy), { negotiated_rate_gbps: 3, negotiated_link_rate: "3.0 Gbit" });
  const built = model(item);
  const rows = cardRows(built, { kind: "port", id: built.bayById.get(second.bayId).ports[0].key });
  const rate = rows.find((entry) => entry.label === "Rate");
  assert.doesNotMatch(rate.value, /\b3 Gb\/s/, textOf(rate));
  assert.match(rate.value, /not read/, textOf(rate));
});

// The CORE paths fixture with two mpr1 paths of Bay 00 on one expander PHY
// that name two disk port addresses.
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

test("a PHY two paths of one bay name with two port addresses is said on the bay, never as either port's own", () => {
  for (const order of ["first", "last"]) {
    const built = samePhyPaths(order);
    const ports = built.bays[0].ports.filter((port) => port.phy?.phy === 5);
    assert.equal(ports.length, 2, `${order}: ${textOf(built.bays[0].ports.map((port) => [port.device, port.phy?.phy]))}`);
    for (const port of ports) {
      const errors = cardRows(built, { kind: "port", id: port.key }).find((entry) => entry.label === "PHY errors");
      assert.doesNotMatch(errors.value, /101 invalid dword/, `${order} ${port.device}: ${errors.value}`);
      assert.notEqual(errors.warn, "critical", `${order} ${port.device}`);
      assert.ok(!(built.flags.get(`port:${port.key}`) || []).length, `${order} ${port.device}: ${textOf(built.flags.get(`port:${port.key}`))}`);
    }
    assert.match(said1(built, /101 invalid dword/), /^Bay 00/);
  }
});

// --- 6: a retained lane list never overrides the two ends' own rows -----------------

test("a retained CORE lane list is not passed where the two ends do not name each other or report different rates", () => {
  for (const mode of ["far handle", "far remote PHY", "near own handle", "far rate"]) {
    const data = core();
    const link = data.fabric.links[0];
    const far = data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32);
    Object.assign(far, { invalid_dword_count: 101, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, counter_state: "measured" });
    if (mode === "far handle") far.dev_handle = "000a";
    if (mode === "far remote PHY") far.remote_phy = "2";
    if (mode === "near own handle") hbaRow(data, 0).controller_handle = "000b";
    if (mode === "far rate") far.speed = "3.0";
    const built = coreModel(data);
    assert.ok(!statuses(built, "lanes").includes("pass"), `${mode}: ${textOf(checksOf(built, "lanes"))}`);
    const lane = kv(built, { kind: "link", id: link.id }).find(([, label]) => label === "Lane PHY 0 ↔ 32")?.[2] || "";
    assert.doesNotMatch(lane, /^12 Gb\/s \(both ends 12\)/, `${mode}: ${lane}`);
    if (mode !== "far rate") assert.doesNotMatch(said1(built, /101 invalid dword/), /→/, mode);
  }
});

test("a cable whose members are not read shows its lane list as reported, not as verified wiring", () => {
  const data = core();
  const link = data.fabric.links[0];
  hbaRow(data, 0).device_handle = "000a";
  const built = coreModel(data);
  const rows = cardRows(built, { kind: "link", id: built.links.find((entry) => entry.source === link.source && entry.target === link.target).id });
  const lanes = rows.find((entry) => entry.label === "Lanes").value;
  const phys = rows.find((entry) => entry.label === "PHYs").value;
  assert.doesNotMatch(lanes, /at 12 Gb\/s/, lanes);
  assert.match(lanes, /reported/, lanes);
  assert.match(`${lanes} ${phys}`, /membership not read|members are not read/, `${lanes} | ${phys}`);
});

// --- 7: a missing field is not read, never a second subject -------------------------

test("a second record of the same disk without its slot is the same disk", () => {
  const { item, record } = directMapped();
  item.fabric.raw.sas_transport_ports.push({ ...record, id: "sas-port:same-disk-unlocated", bay_identifier: null });
  const built = model(item);
  assert.match(pin(built, "controller:host1", 12), /^Bay 07/);
  const line = said1(built, /101 invalid dword/);
  assert.match(line, /^Bay 07/, line);
  assert.doesNotMatch(line, /two disks/, line);
});

test("three disks named on one PHY are not said to be two", () => {
  const { item, record } = directMapped();
  item.fabric.raw.sas_transport_ports.push({ ...record, id: "sas-port:other-0", remote_sas_address: "5eee001e00000aaa", bay_identifier: 0 },
    { ...record, id: "sas-port:other-2", remote_sas_address: "5eee001e00000bbb", bay_identifier: 2 });
  const line = said1(model(item), /101 invalid dword/);
  assert.doesNotMatch(line, /^Bay 07|two disks/, line);
  assert.match(line, /owner not read/, line);
});

function coreModel(data) {
  return T.buildTopologyModel(data.fabric, data.options || { inventory: data.snapshot });
}

// --- each rule observed on an input no other rule settles first -------------------

test("two transport records placing one address at two bay numbers leave its fault unassigned, whatever the enclosure", () => {
  const { item, second, own } = twoBays();
  // Another record of the second disk's address at another transport bay,
  // no enclosure read on either: the census alone places it twice.
  const record = own(second);
  record.enclosure_identifier = null;
  item.fabric.raw.sas_transport_ports.push({ ...record, id: "sas-port:elsewhere", owner_id: "expander:controller:host9:expander-9:0", phys: [3], phy_names: ["phy-9:0:3"], bay_identifier: 17 });
  const line = said1(model(item), /101 invalid dword/);
  assert.doesNotMatch(line, new RegExp(`^${second.label}`), line);
  assert.match(line, /owner not read/, line);
});

test("an HBA PHY on two rows gives its lane no maximum, even where every rate agrees", () => {
  for (const first of [false, true]) {
    const data = core();
    const link = data.fabric.links[0];
    const controller = data.fabric.controllers.find((entry) => entry.id === link.source);
    const row = controller.phy_rows.find((entry) => Number(entry.phy) === 0);
    controller.phy_rows[first ? "unshift" : "push"]({ ...row, max: "3.0" });
    const lanes = checksOf(coreModel(data), "lanes");
    assert.match(textOf(lanes), /rate or an end's maximum not read for 1 lane/, `${first}: ${textOf(lanes)}`);
  }
});

test("a lane below both ends' maxima is not judged slow while its cable's ends do not name each other", () => {
  const data = core();
  const link = data.fabric.links[0];
  const far = data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32);
  Object.assign(far, { dev_handle: "000a", speed: "3.0" });
  hbaRow(data, 0).speed = "3.0";
  link.reported_lanes.lanes[0].negotiated_rate_gbps = 3;
  link.reported_lanes.negotiated_rate_gbps = null;
  const lanes = checksOf(coreModel(data), "lanes");
  assert.ok(!lanes.some((entry) => ["pass", "critical"].includes(entry.status)), textOf(lanes));
  assert.doesNotMatch(textOf(lanes), /below the maximum/, textOf(lanes));
});

test("a neighbour's health includes a fault on its PHY that a bay of this view owns", () => {
  const item = fixture("fanout-2");
  const trace = item.fabric.traces.find((entry) => entry.id === "bay:0");
  const dest = item.fabric.raw.sas_transport_ports.find((port) => port.owner_id?.endsWith("expander-2:1") && port.has_block);
  // No other place reports the address: the bay owns the PHY.
  dest.enclosure_identifier = null;
  const { id, owner_id, phy_names, state } = dest;
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id, phy_names, state };
  for (const entry of [...trace.metrics.path_states, ...trace.metrics.sas_transport_ports]) Object.assign(entry, { sas_transport_port_id: id, expander_ids: [owner_id], sas_address: dest.remote_sas_address });
  phyRows(item.fabric, owner_id).find((entry) => entry.phy === dest.phys[0]).invalid_dword_count = 101;
  const built = model(item);
  assert.match(said1(built, /101 invalid dword/), /^Bay 00/);
  const stub = built.nodeById.get(owner_id);
  assert.equal(stub.neighbour.tier, "critical", textOf(stub.neighbour));
});

test("a CORE disk path's handle another device of its HBA also carries does not say which disk is on the PHY", () => {
  for (const mode of ["control", "enclosure processor", "expander"]) {
    const data = structuredClone(DIRECT);
    const expander = data.fabric.expanders[0];
    const port = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => (entry.expander_ids || []).includes(expander.id));
    expander.phys.push({ ...expander.phys[0], phy: "4", device: "SAS Target", dev_handle: port.handle, counter_state: "measured",
      invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
    const controller = `controller:${port.controller}`;
    if (mode === "enclosure processor") data.fabric.nodes.find((entry) => entry.kind === "mpr-enclosure" && entry.controller_id === controller).raw.sep_handle = port.handle;
    if (mode === "expander") data.fabric.expanders.push({ ...structuredClone(expander), id: `expander:${controller}:5000c50000000eff`, sas_address: "5000c50000000eff", dev_handle: port.handle, phys: [] });
    const line = said1(coreModel(data), /101 invalid dword/);
    if (mode === "control") assert.match(line, /^Bay 00/, line);
    else assert.match(line, new RegExp(`owner not read: Bay 00 claims it, but its device handle ${port.handle} names more than one device`), `${mode}: ${line}`);
  }
});

test("a port on a neighbour's expander is not placed on this backplane, whatever its address says", () => {
  const item = fixture("fanout-2");
  const trace = item.fabric.traces.find((entry) => entry.id === "bay:0");
  const dest = item.fabric.raw.sas_transport_ports.find((port) => port.owner_id?.endsWith("expander-2:1") && port.has_block);
  // No other place reports the address, so only the landing is in question.
  dest.enclosure_identifier = null;
  const { id, owner_id, phy_names, state } = dest;
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id, phy_names, state };
  for (const row of [...trace.metrics.path_states, ...trace.metrics.sas_transport_ports]) Object.assign(row, { sas_transport_port_id: id, expander_ids: [owner_id], sas_address: dest.remote_sas_address });
  const ports = checksOf(model(item), "ports");
  assert.ok(!ports.some((entry) => entry.status === "pass"), textOf(ports));
  assert.match(textOf(ports), /lands on/, textOf(ports));
});
