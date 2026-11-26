"use strict";

// Nothing is resolved from part of its evidence (#1504). A device handle,
// the disk on a PHY, a cable's lanes and a lane's rate are each decided
// once, from every record of the payload that could name them: a reply not
// read, an identifier that names more than one thing, a row the lane list
// leaves out, or a row from another controller unit leaves the subject not
// read on every surface, never resolved by the records that happen to
// agree. Every payload is synthetic, in the producer's shapes.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const T = H.loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;

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
const linkOf = (built, source, target) => built.links.flatMap((entry) => [entry, ...(entry.stubCables || [])]).find((entry) => entry.source === source && entry.target === target && entry.lanes);
const lanePin = (built, link, lane) => kv(built, { kind: "link", id: link.id }).find(([, label]) => label === `Lane PHY ${lane}`) || ["", "", ""];
const lanesCard = (built, link) => cardRows(built, { kind: "link", id: link.id }).find((entry) => entry.label === "Lanes")?.value || "";

// --- 1: which lists were read is decided before any device is named -----------

// HBA PHY 4's handle is the one a bay's disk carries, attached directly;
// the builder that saved the payload kept no per-PHY handle state.
function directHandle() {
  const data = core();
  Object.assign(hbaRow(data, 4), { controller_handle: "0001", device_handle: "0020", device: "SAS Target", speed: "12.0" });
  Object.assign(mprDevice(data), { parent: "0001", expander_ids: [] });
  delete controllerOf(data).device_handle_states;
  return data;
}

test("a list of an HBA that was not read leaves its handles not read, though a bay's disk carries one", () => {
  const coverage = {
    "the HBA's own list states": (data) => { controllerOf(data).mpr_lists = { expanders: "not_read", devices: "read", enclosures: "read" }; },
    "the payload's list states": (data) => { delete controllerOf(data).mpr_lists; data.fabric.raw.controller_read.lists.mpr0.expanders = "not_read"; },
    "the two sources disagree": (data) => { controllerOf(data).mpr_lists = { expanders: "read", devices: "read", enclosures: "read" }; data.fabric.raw.controller_read.lists.mpr0.expanders = "not_read"; },
  };
  for (const [name, unread] of Object.entries(coverage)) {
    const data = directHandle();
    unread(data);
    const built = coreModel(data);
    const value = pin(built, MPR0, 4);
    assert.match(value, /^destination not read: .*expanders list was not read/, `${name}: ${value}`);
    assert.doesNotMatch(hbaCard(built), /attached directly/, name);
  }
  // Every list read: the disk is named.
  const built = coreModel(directHandle());
  assert.match(pin(built, MPR0, 4), /^to the disk in Bay 00 \(da0\), attached directly/);
});

test("an HBA that reports no list states names no device by handle and calls none absent", () => {
  for (const handle of ["0020", "009b"]) {
    const data = directHandle();
    hbaRow(data, 4).device_handle = handle;
    delete controllerOf(data).mpr_lists;
    delete data.fabric.raw.controller_read.lists;
    const value = pin(coreModel(data), MPR0, 4);
    assert.match(value, /^destination not read: the payload does not say which lists of mpr0 were read/, `${handle}: ${value}`);
    assert.doesNotMatch(value, /outside this view|attached directly/, handle);
  }
});

// --- 2: an identifier that names more than one thing joins nothing -------------

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

test("a disk port address the payload places in two enclosures bridges no records into one disk", () => {
  for (const order of ["as read", "reversed"]) {
    const { item, record } = directMapped();
    const census = item.fabric.raw.sas_transport_ports;
    // A second address on the PHY, at slot 7 of another enclosure, and the
    // same address at slot 7 of this one: it names no one place.
    census.push({ ...record, id: "sas-port:foreign", remote_sas_address: "5eee001e00000456", enclosure_identifier: "5fff0000000000aa" },
      { ...record, id: "sas-port:bridge", remote_sas_address: "5eee001e00000456" });
    if (order === "reversed") census.reverse();
    const built = model(item);
    assert.ok(built.ambiguousAddresses.has("5eee001e00000456"), order);
    const line = said1(built, /101 invalid dword/);
    assert.doesNotMatch(line, /^Bay 07/, `${order}: ${line}`);
    assert.match(line, /owner not read: Bay 07 claims it, but a record on it names disk port address 5eee001e00000456, which is reported for more than one slot/, `${order}: ${line}`);
    assert.doesNotMatch(pin(built, "controller:host1", 12), /^Bay 07/, order);
  }
  // The address at one place only: the fault is the bay's.
  const { item } = directMapped();
  assert.match(said1(model(item), /101 invalid dword/), /^Bay 07/);
});

test("on a PHY no bay claims, an address in two enclosures joins no records into one disk either", () => {
  for (const order of ["as read", "reversed"]) {
    const { item, record } = directMapped();
    // No bay's path is joined to the PHY.
    Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:7").metrics, { sas_transport_ports: [], path_states: [] });
    const census = item.fabric.raw.sas_transport_ports;
    census.push({ ...record, id: "sas-port:foreign", remote_sas_address: "5eee001e00000456", enclosure_identifier: "5fff0000000000aa" },
      { ...record, id: "sas-port:bridge", remote_sas_address: "5eee001e00000456" });
    if (order === "reversed") census.reverse();
    const value = pin(model(item), "controller:host1", 12);
    assert.match(value, /^role not read: the transport census names two disks on it/, `${order}: ${value}`);
  }
});

// --- 3: a lane list is a cable's only where it is every row naming both ends ---

function cable(mode) {
  const data = core();
  const link = data.fabric.links[0];
  const hba = controllerOf(data);
  const expander = data.fabric.expanders.find((entry) => entry.id === link.target);
  const far = expander.phys.find((entry) => Number(entry.phy) === 32);
  Object.assign(far, { invalid_dword_count: 101, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, counter_state: "measured" });
  const near = hbaRow(data, 0);
  if (mode === "an HBA row the list leaves out" || mode === "a row at each end the list leaves out") Object.assign(hbaRow(data, 4), { ...near, phy: "4" });
  if (mode === "an expander row the list leaves out" || mode === "a row at each end the list leaves out") {
    Object.assign(expander.phys.find((entry) => Number(entry.phy) === 4), { ...far, phy: "4", remote_phy: "4", invalid_dword_count: 0 });
  }
  if (mode === "no PHY table at either end") {
    data.fabric.controllers = [];
    data.fabric.expanders = [];
  }
  return { data, link: { source: link.source, target: link.target }, hba };
}

test("a retained lane list that is not every row by which the two ends name each other is no cable's membership", () => {
  for (const mode of ["an HBA row the list leaves out", "an expander row the list leaves out", "a row at each end the list leaves out"]) {
    const { data, link } = cable(mode);
    const built = coreModel(data);
    assert.ok(!statuses(built, "lanes").includes("pass"), `${mode}: ${textOf(checksOf(built, "lanes"))}`);
    assert.match(textOf(checksOf(built, "lanes")), /which the lane list leaves out|the lane list names PHY/, mode);
    const drawn = linkOf(built, link.source, link.target);
    assert.match(lanesCard(built, drawn), /^4 reported; which PHYs the cable uses is not read/, `${mode}: ${lanesCard(built, drawn)}`);
    assert.doesNotMatch(said1(built, /101 invalid dword/), /→/, mode);
  }
  const { data, link } = cable("control");
  const built = coreModel(data);
  assert.ok(statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
  assert.match(lanesCard(built, linkOf(built, link.source, link.target)), /^4: 4 at 12 Gb\/s$/);
});

test("with no PHY table at either end a retained lane list is shown as reported, never as verified membership", () => {
  const { data, link } = cable("no PHY table at either end");
  const built = coreModel(data);
  const drawn = linkOf(built, link.source, link.target);
  assert.match(lanesCard(built, drawn), /^4 reported; which PHYs the cable uses is not read/, lanesCard(built, drawn));
  assert.ok(!statuses(built, "lanes").includes("pass"));
});

// The two cascades, each from one unit; their handles repeat across units.
function cascades(mode) {
  const data = core();
  const links = data.fabric.links.filter((entry) => entry.kind === "expander-expander");
  if (mode === "crossed") {
    [links[0].target, links[1].target] = [links[1].target, links[0].target];
    for (const entry of links) entry.id = `${entry.kind}:${entry.source}->${entry.target}`;
  }
  const child = data.fabric.expanders.find((entry) => entry.id.endsWith("0e12"));
  Object.assign(child.phys.find((entry) => Number(entry.phy) === 0), { counter_state: "measured", invalid_dword_count: 102, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  if (mode === "child handle ambiguous") controllerOf(data).ambiguous_device_handles = [data.fabric.expanders.find((entry) => entry.id.endsWith("0e02")).dev_handle];
  return data;
}

test("a cascade's two ends must be rows of one controller unit, by handles that unit holds unique", () => {
  const own = coreModel(cascades("own"));
  assert.ok(statuses(own, "lanes").includes("pass"), textOf(checksOf(own, "lanes")));
  assert.match(said1(own, /102 invalid dword/), /0e11 → .*0e12/);
  const crossed = coreModel(cascades("crossed"));
  assert.ok(!statuses(crossed, "lanes").includes("pass"), textOf(checksOf(crossed, "lanes")));
  assert.match(textOf(checksOf(crossed, "lanes")), /another controller unit/);
  assert.doesNotMatch(said1(crossed, /102 invalid dword/), /0e01 → /);
  const ambiguous = coreModel(cascades("child handle ambiguous"));
  assert.ok(!statuses(ambiguous, "lanes").includes("pass"), textOf(checksOf(ambiguous, "lanes")));
  assert.match(textOf(checksOf(ambiguous, "lanes")), /device handle 000a names more than one device of mpr0/);
});

// --- 4: a lane's rate is shown only where both ends' own rows read it ----------

function unreadSpeed(mode) {
  const data = core();
  const link = data.fabric.links[0];
  const far = data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 32);
  far.speed = null;
  hbaRow(data, 0).speed = mode === "near slow, far not read" ? "3.0" : null;
  link.reported_lanes.lanes[0].negotiated_rate_gbps = mode === "near slow, far not read" ? 3 : 12;
  link.reported_lanes.negotiated_rate_gbps = null;
  return { data, link: { source: link.source, target: link.target } };
}

test("a lane whose end rows do not read its speed shows no measured rate on any surface", () => {
  for (const mode of ["neither end read", "near slow, far not read"]) {
    const { data, link } = unreadSpeed(mode);
    const built = coreModel(data);
    const drawn = linkOf(built, link.source, link.target);
    const [style, , value] = lanePin(built, drawn, "0 ↔ 32");
    assert.match(value, /^rate not read/, `${mode}: ${value}`);
    assert.doesNotMatch(style, /topo-warn/, `${mode}: ${style}`);
    assert.doesNotMatch(drawn.labelLines[0], /12 Gb\/s$|3–12 Gb\/s/, `${mode}: ${drawn.labelLines[0]}`);
    assert.match(lanesCard(built, drawn), /3 at 12 Gb\/s, 1 at a rate not read/, `${mode}: ${lanesCard(built, drawn)}`);
    assert.doesNotMatch(textOf(cardRows(built, { kind: "link", id: drawn.id })), /below both ends/i, mode);
  }
});

// --- DUP1: a lane's maximum is no row's where its PHY number is on two rows ----

function explicitPairs(duplicate) {
  const item = fixture("wide-x4-sata");
  const link = item.fabric.links.find((entry) => entry.kind === "controller-expander" && entry.reported_lanes?.source_phys?.includes(0));
  Object.assign(link.reported_lanes, { basis: "reciprocal_reported_phy_rows", target_basis: "reported", target_phys: [16, 17, 18, 19],
    lanes: [0, 1, 2, 3].map((phy) => ({ source_phy: phy, target_phy: phy + 16, negotiated_rate_gbps: 12 })) });
  const rows = phyRows(item.fabric, link.source);
  if (duplicate) {
    const twin = { ...rows.find((entry) => entry.phy === 0), name: "phy-1:20", maximum_rate_gbps: 3, maximum_link_rate: "3.0 Gbit" };
    if (duplicate === "slower first") rows.unshift(twin);
    else rows.push(twin);
  }
  return { item, link: { source: link.source, target: link.target } };
}

test("a lane whose HBA PHY number is on two rows has no maximum, whichever row comes first", () => {
  for (const duplicate of ["slower first", "slower last"]) {
    const { item, link } = explicitPairs(duplicate);
    const built = model(item);
    const [, , value] = lanePin(built, linkOf(built, link.source, link.target), "0 ↔ 16");
    assert.match(value, /\(a maximum not read\)/, `${duplicate}: ${value}`);
    assert.doesNotMatch(value, /both ends/, `${duplicate}: ${value}`);
  }
  const { item, link } = explicitPairs("");
  const built = model(item);
  assert.match(lanePin(built, linkOf(built, link.source, link.target), "0 ↔ 16")[2], /^12 Gb\/s \(both ends 12\)/);
});
