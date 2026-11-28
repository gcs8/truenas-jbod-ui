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
  for (const partial of [false, true]) for (const order of ["as read", "reversed"]) {
    const { item, record } = directMapped();
    const census = item.fabric.raw.sas_transport_ports;
    // A second address on the PHY, at slot 7 of another enclosure, and the
    // same address at slot 7 of this one: it names no one place.
    census.push({ ...record, id: "sas-port:foreign", remote_sas_address: "5eee001e00000456", enclosure_identifier: "5fff0000000000aa" },
      { ...record, id: "sas-port:bridge", remote_sas_address: "5eee001e00000456" });
    if (partial) census.find((row) => row.id === "sas-port:foreign").bay_identifier = null;
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

for (const mode of ["parent handle on another expander", "child handle on a processor", "devices unread"]) {
  test(`cascade membership: ${mode}`, () => {
    const data = core();
    const link = data.fabric.links.find((entry) => entry.kind === "expander-expander");
    const [parent, child] = [link.source, link.target].map((id) => data.fabric.expanders.find((entry) => entry.id === id));
    const other = data.fabric.links.find((entry) => entry.kind === link.kind && entry.id !== link.id);
    const control = coreModel(data);
    const otherCard = lanesCard(control, linkOf(control, other.source, other.target));
    if (mode.startsWith("parent")) data.fabric.expanders.push({ ...parent, id: `${parent.id}-other`, sas_address: parent.sas_address.replace(/.$/, "b"), phys: [] });
    if (mode.startsWith("child")) data.fabric.nodes.find((entry) => entry.kind === "mpr-enclosure" && entry.controller_id === child.controller_id).raw.sep_handle = child.dev_handle;
    if (mode.endsWith("unread")) data.fabric.raw.controller_read.lists.mpr0.devices = "not_read";
    Object.assign(child.phys[0], { counter_state: "measured", invalid_dword_count: 102, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0 });
    const built = coreModel(data);
    assert.match(lanesCard(built, linkOf(built, link.source, link.target)), /which PHYs the cable uses is not read/);
    assert.doesNotMatch(said1(built, /102 invalid dword/), /→/);
    assert.equal(lanesCard(built, linkOf(built, other.source, other.target)), otherCard);
  });
}

for (const mode of ["shorter list", "shorter port", "longer port"]) {
  test(`Linux cable membership: ${mode}`, () => {
    const item = fixture("wide-x4-sata");
    const link = item.fabric.links.find((entry) => entry.kind === "controller-expander");
    const port = item.fabric.raw.sas_transport_ports.find((entry) => entry.remote_id === link.target && entry.owner_id === link.source);
    if (mode === "shorter list") { link.reported_lanes.source_phys.pop(); link.reported_lanes.lane_count--; }
    if (mode === "shorter port") { port.phys.pop(); port.phy_names.pop(); }
    if (mode === "longer port") { port.phys.push(13); port.phy_names.push("phy-1:13"); phyRows(item.fabric, link.source).push({ ...phyRows(item.fabric, link.source)[0], phy: 13, name: "phy-1:13" }); }
    Object.assign(phyRows(item.fabric, link.source).find((row) => row.phy === 3), { counter_state: "measured", invalid_dword_count: 103 });
    const built = model(item);
    const drawn = linkOf(built, link.source, link.target);
    assert.match(lanesCard(built, drawn), /which PHYs the cable uses is not read/);
    assert.match(textOf(drawn.labelLines), /membership not read/);
    assert.doesNotMatch(said1(built, /103 invalid dword/), /→/);
  });
}

for (const mixed of [false, true]) {
  test(`the drawn rate is the rate of its lanes: ${mixed ? "mixed" : "uniform"}`, () => {
    const data = core();
    const link = data.fabric.links[0];
    if (mixed) {
      link.reported_lanes.lanes[0].negotiated_rate_gbps = 3;
      hbaRow(data, 0).speed = "3.0";
      data.fabric.expanders.find((row) => row.id === link.target).phys.find((row) => Number(row.phy) === 32).speed = "3.0";
    } else link.reported_lanes.negotiated_rate_gbps = 3;
    const built = coreModel(data);
    assert.equal(linkOf(built, link.source, link.target).labelLines[0], mixed ? "x4 · 3–12 Gb/s" : "x4 · 12 Gb/s");
  });
}

for (const first of [false, true]) {
  test(`duplicate HBA coverage: unread ${first ? "first" : "last"}`, () => {
    const data = directHandle();
    delete data.fabric.raw.controller_read.lists;
    const hba = controllerOf(data);
    const twin = structuredClone(hba);
    twin.mpr_lists = { ...hba.mpr_lists, expanders: "not_read" };
    data.fabric.controllers[first ? "unshift" : "push"](twin);
    assert.match(pin(coreModel(data), MPR0, 4), /^destination not read/);
  });
  test(`duplicate expander observations: slow ${first ? "first" : "last"}`, () => {
    const data = core();
    const link = data.fabric.links[0];
    const twin = structuredClone(data.fabric.expanders.find((row) => row.id === link.target));
    twin.phys.find((row) => Number(row.phy) === 32).speed = "3.0";
    data.fabric.expanders[first ? "unshift" : "push"](twin);
    const built = coreModel(data);
    const drawn = linkOf(built, link.source, link.target);
    assert.match(lanesCard(built, drawn), /3 at 12 Gb\/s, 1 at a rate not read/);
    assert.doesNotMatch(drawn.labelLines[0], /12 Gb\/s$/);
  });
}

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

// --- A Linux cable's PHY list is its parent port's, by every source that reads it ---

// The first HBA cable of the fabric, with a measured fault on its last PHY.
function parentPort() {
  const item = fixture("wide-x4-sata");
  const link = item.fabric.links.find((entry) => entry.kind === "controller-expander");
  const census = item.fabric.raw.sas_transport_ports;
  const port = census.find((entry) => entry.remote_id === link.target && entry.owner_id === link.source);
  const rows = phyRows(item.fabric, link.source);
  Object.assign(rows.find((row) => row.phy === 3), { counter_state: "measured", invalid_dword_count: 103 });
  return { item, link, census, port, rows, child: node(item.fabric, link.target) };
}

// One source of the port's membership missing an identifier, or disagreeing
// with the others, each time with every other source untouched.
const PARENT_PORT_SOURCES = {
  "the child names no parent port": ({ child }) => { delete child.raw.parent_port; },
  "the child names no parent port, and the census port is shorter": ({ child, port }) => { delete child.raw.parent_port; port.phys.pop(); port.phy_names.pop(); },
  "the port's PHY numbers are fewer than its PHY names": ({ port }) => { port.phys.pop(); },
  "the port's PHY names are fewer than its PHY numbers": ({ port }) => { port.phy_names.pop(); },
  "the port lists no PHY names": ({ port }) => { delete port.phy_names; },
  "the port lists no PHY numbers": ({ port }) => { delete port.phys; },
  "a PHY name of the port is on no row of the parent": ({ port }) => { port.phy_names[3] += "9"; },
  "a listed PHY's row carries another name": ({ rows }) => { rows.find((row) => row.phy === 3).name += "9"; },
  "a listed PHY's row carries no name": ({ rows }) => { delete rows.find((row) => row.phy === 3).name; },
  "the child reports no PHYs for its parent port": ({ child }) => { delete child.raw.source_phys; },
  "the child reports another PHY count for its parent port": ({ child }) => { child.raw.reported_num_phys -= 1; },
  "the child reports another parent": ({ child }) => { child.raw.parent_id += "-other"; },
  "the child's parent is not resolved": ({ child }) => { child.raw.parent_link_state = "unknown"; },
  "the census names the port of these two ends otherwise": ({ port }) => { port.id += "9"; },
  "a second census record names these two ends": ({ census, port }) => { census.push({ ...port, id: `${port.id}9`, phys: port.phys.slice(0, -1), phy_names: port.phy_names.slice(0, -1) }); },
  "a second census record names these two ends, with PHYs of its own": ({ census, port }) => { census.push({ ...port, id: `${port.id}9`, phys: [20], phy_names: [] }); },
  "the census has no record of the port": ({ census, port }) => { census.splice(census.indexOf(port), 1); },
  "the port census is reported with no records": ({ item }) => { item.fabric.raw.sas_transport_ports = []; },
  "the port's census record was not read": ({ port }) => { port.state = "not_read"; },
  "the port's census record names another owner": ({ port }) => { port.owner_id += "-other"; },
  "no port census, and the child names no parent port": ({ item, child }) => { delete item.fabric.raw.sas_transport_ports; delete child.raw.parent_port; },
};

for (const [mode, change] of Object.entries(PARENT_PORT_SOURCES)) {
  test(`Linux parent port: ${mode}`, () => {
    const found = parentPort();
    change(found);
    const built = model(found.item);
    const drawn = linkOf(built, found.link.source, found.link.target);
    const card = lanesCard(built, drawn);
    // No verified width, and no rate beside it.
    assert.match(card, /^4 reported; which PHYs the cable uses is not read \(/, `${mode}: ${card}`);
    assert.doesNotMatch(card, /Gb\/s/, `${mode}: ${card}`);
    assert.match(textOf(drawn.labelLines), /membership not read/, mode);
    assert.ok(!statuses(built, "lanes").includes("pass"), `${mode}: ${textOf(checksOf(built, "lanes"))}`);
    // The measured fault is said once, and is not this cable's.
    assert.doesNotMatch(said1(built, /103 invalid dword/), /→/, mode);
  });
}

// The list's own SAS address for an end is one more identifier of the cable.
for (const end of ["source_sas_address", "attached_sas_address"]) {
  test(`a Linux lane list that gives another device's SAS address as its ${end} is no cable's membership`, () => {
    const found = parentPort();
    const other = found.census.find((entry) => entry.remote_kind === "expander" && entry.remote_id !== found.link.target).remote_sas_address;
    found.link.reported_lanes[end] = other;
    const built = model(found.item);
    const drawn = linkOf(built, found.link.source, found.link.target);
    assert.match(lanesCard(built, drawn), /^4 reported; which PHYs the cable uses is not read \(.*the lane list gives another SAS address than /);
    assert.match(textOf(drawn.labelLines), /membership not read/);
    assert.doesNotMatch(said1(built, /103 invalid dword/), /→/);
  });
}

test("a CORE lane list that gives another expander's SAS address for its far end is no cable's membership", () => {
  const data = core();
  const [link, other] = data.fabric.links.filter((entry) => entry.kind === "expander-expander");
  link.reported_lanes.attached_sas_address = other.reported_lanes.attached_sas_address;
  const built = coreModel(data);
  assert.match(lanesCard(built, linkOf(built, link.source, link.target)), /^4 reported; which PHYs the cable uses is not read \(.*the lane list gives another SAS address than /);
  assert.equal(lanesCard(built, linkOf(built, other.source, other.target)), "4: 4 at 12 Gb/s");
  assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
});

test("a Linux cable every source agrees on is read; with no port census it is judged on what its child reports", () => {
  const found = parentPort();
  const built = model(found.item);
  assert.equal(lanesCard(built, linkOf(built, found.link.source, found.link.target)), "4 · 12 Gb/s");
  assert.match(said1(built, /103 invalid dword/), /→/);
  // An older payload carries no port census: the child's own report of its
  // parent port is the read source, and the far end stays not read.
  const older = parentPort();
  delete older.item.fabric.raw.sas_transport_ports;
  const plain = model(older.item);
  const drawn = linkOf(plain, older.link.source, older.link.target);
  assert.equal(lanesCard(plain, drawn), "4 · 12 Gb/s");
  assert.match(textOf(drawn.labelLines), /far end not read/);
  assert.doesNotMatch(textOf(drawn.labelLines), /membership not read/);
});

// --- More than one record of a Linux cable's child, or of a link -----------------

// A second record of the same id, changed, before and after the first: the
// two orders say the same, and neither resolves the cable.
function bothOrders(build, pattern) {
  const cards = [false, true].map((first) => {
    const { built, link } = build(first);
    const card = lanesCard(built, linkOf(built, link.source, link.target));
    assert.match(card, pattern, `${first ? "first" : "last"}: ${card}`);
    assert.doesNotMatch(card, /Gb\/s/, card);
    assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
    return card;
  });
  assert.equal(cards[0], cards[1]);
}
const beside = (records, record, twin, first) => records.splice(records.indexOf(record) + (first ? 0 : 1), 0, twin);

for (const [what, change] of Object.entries({
  "names no parent port": (twin) => { delete twin.raw.parent_port; },
  "reports fewer PHYs for its parent port": (twin) => { twin.raw.source_phys.pop(); },
  "reports another parent": (twin) => { twin.raw.parent_id += "-other"; },
})) {
  test(`two records of a Linux cable's child, one of which ${what}, leave its PHYs not read in either order`, () => {
    bothOrders((first) => {
      const found = parentPort();
      const twin = structuredClone(found.child);
      change(twin);
      beside(found.item.fabric.nodes, found.child, twin, first);
      const built = model(found.item);
      assert.doesNotMatch(said1(built, /103 invalid dword/), /→/);
      return { built, link: found.link };
    }, /^4 reported; which PHYs the cable uses is not read \(.*the records of .* disagree about the parent port it is on/);
  });
}

test("two records of a link whose lane lists differ leave its PHYs not read in either order", () => {
  // Linux: one record lists one PHY fewer.
  bothOrders((first) => {
    const found = parentPort();
    const twin = structuredClone(found.link);
    twin.reported_lanes.source_phys.pop();
    twin.reported_lanes.lane_count -= 1;
    beside(found.item.fabric.links, found.link, twin, first);
    const built = model(found.item);
    assert.doesNotMatch(said1(built, /103 invalid dword/), /→/);
    return { built, link: found.link };
  }, /^\d reported; which PHYs the cable uses is not read \(.*more than one record of this link, and their lane lists differ/);
  // CORE: one record lists one pair fewer.
  bothOrders((first) => {
    const data = core();
    const [link] = data.fabric.links;
    const twin = structuredClone(link);
    for (const key of ["lanes", "source_phys", "target_phys"]) twin.reported_lanes[key].pop();
    twin.reported_lanes.lane_count -= 1;
    beside(data.fabric.links, link, twin, first);
    return { built: coreModel(data), link };
  }, /^\d reported; which PHYs the cable uses is not read \(.*more than one record of this link, and their lane lists differ/);
});

test("a second record of a Linux cable's child, or of a link, that differs in nothing changes nothing", () => {
  const single = parentPort();
  const expected = model(single.item);
  for (const pick of ["child", "link"]) for (const first of [false, true]) {
    const found = parentPort();
    beside(pick === "child" ? found.item.fabric.nodes : found.item.fabric.links, found[pick], structuredClone(found[pick]), first);
    const built = model(found.item);
    assert.equal(lanesCard(built, linkOf(built, found.link.source, found.link.target)), "4 · 12 Gb/s", `${pick} ${first}`);
    assert.equal(said1(built, /103 invalid dword/), said1(expected, /103 invalid dword/));
  }
});

// --- A claim is decided over every record that could make it ---------------------

// The fabric's two cables from host1, their children and census ports, with
// a measured fault on PHY `phy` of host1.
function twoCables(phy = 3) {
  const item = fixture("wide-x4-sata");
  const [first, second] = item.fabric.links.filter((entry) => entry.kind === "controller-expander");
  const census = item.fabric.raw.sas_transport_ports;
  Object.assign(phyRows(item.fabric, first.source).find((row) => row.phy === phy), { counter_state: "measured", invalid_dword_count: 103 });
  return { item, first, second, census, children: [node(item.fabric, first.target), node(item.fabric, second.target)] };
}
const unowned = (built, pattern = /103 invalid dword/) => {
  const line = said1(built, pattern);
  assert.match(line, /owner not read/, line);
  assert.doesNotMatch(line.split(":")[0], /→/, line);
};
const withheld = (built, link, why) => {
  const drawn = linkOf(built, link.source, link.target);
  const card = lanesCard(built, drawn);
  assert.match(card, /^\d+ reported; which PHYs the cable uses is not read \(/, `${why}: ${card}`);
  assert.doesNotMatch(card, /Gb\/s/, `${why}: ${card}`);
  // Neither does the drawn label give a rate for a cable whose PHYs are not read.
  assert.doesNotMatch(textOf(drawn.labelLines), /Gb\/s/, `${why}: ${textOf(drawn.labelLines)}`);
  assert.match(textOf(drawn.labelLines), /membership not read/, why);
  return card;
};

test("a second record of another link that lists a cable's PHY leaves that PHY no cable's, in either order", () => {
  // The second cable's list moved one PHY down, onto the first cable's last.
  const shifted = (link) => { const twin = structuredClone(link); Object.assign(twin.reported_lanes, { source_phys: [3, ...link.reported_lanes.source_phys], lane_count: link.reported_lanes.lane_count + 1 }); return twin; };
  // Its only record: the fault's owner is not read.
  const alone = twoCables();
  alone.second.reported_lanes = shifted(alone.second).reported_lanes;
  unowned(model(alone.item));
  for (const first of [false, true]) {
    const found = twoCables();
    beside(found.item.fabric.links, found.second, shifted(found.second), first);
    const built = model(found.item);
    unowned(built);
    assert.match(withheld(built, found.first, `first ${first}`), /also lists it/);
    assert.ok(!names(pin(built, found.first.source, 3)), pin(built, found.first.source, 3));
  }
  // A list that is not one PHY per lane still names the PHYs it lists.
  const invalid = twoCables();
  const twin = shifted(invalid.second);
  twin.reported_lanes.lane_count -= 2;
  invalid.item.fabric.links.push(twin);
  unowned(model(invalid.item));
  // Control: neither record lists it.
  assert.match(said1(model(twoCables().item), /103 invalid dword/), /→/);
});

test("a second record of a link that swaps one member for another cable's PHY leaves that PHY no cable's", () => {
  // The fault is on the second cable's first PHY; the first cable's other
  // record lists it in place of its own last PHY: the same width.
  const swapped = (link) => { const twin = structuredClone(link); twin.reported_lanes.source_phys[3] = 4; return twin; };
  const control = twoCables(4);
  assert.match(said1(model(control.item), /103 invalid dword/), /→/);
  for (const first of [false, true]) {
    const found = twoCables(4);
    beside(found.item.fabric.links, found.first, swapped(found.first), first);
    const built = model(found.item);
    unowned(built);
    withheld(built, found.second, `first ${first}`);
    withheld(built, found.first, `first ${first}`);
  }
});

test("a bay's PHY that another record of a cable lists is not the bay's", () => {
  for (const first of [false, true]) {
    const { item } = directMapped();
    const link = item.fabric.links.filter((entry) => entry.kind === "controller-expander")[1];
    const twin = structuredClone(link);
    Object.assign(twin.reported_lanes, { source_phys: [...link.reported_lanes.source_phys, 12], lane_count: link.reported_lanes.lane_count + 1 });
    beside(item.fabric.links, link, twin, first);
    const line = said1(model(item), /101 invalid dword/);
    assert.doesNotMatch(line, /^Bay 07/, line);
    assert.match(line, /owner not read/, line);
  }
});

test("a link record whose far end is not in the payload still claims the PHYs it lists", () => {
  const ghost = (link, phys) => ({ ...structuredClone(link), id: `${link.id}-gone`, target: `${link.target}-gone`, reported_lanes: { ...structuredClone(link.reported_lanes), source_phys: phys, lane_count: phys.length } });
  // Against a cable: the cable's PHYs are not read, and the fault is no cable's.
  const found = twoCables();
  found.item.fabric.links.push(ghost(found.first, [3]));
  const built = model(found.item);
  unowned(built);
  assert.match(withheld(built, found.first, "cable"), /a device this view does not draw also lists it/);
  // Against a bay: the bay's PHY is not the bay's.
  const { item } = directMapped();
  item.fabric.links.push(ghost(item.fabric.links.find((entry) => entry.kind === "controller-expander"), [12]));
  const line = said1(model(item), /101 invalid dword/);
  assert.doesNotMatch(line, /^Bay 07/, line);
  assert.match(line, /owner not read: .*a lane list of a link this view does not draw/, line);
  // Against nothing: a PHY only such a list names is said as it was.
  const spare = () => {
    const made = twoCables();
    const rows = phyRows(made.item.fabric, made.first.source);
    rows.push({ ...rows[0], name: `${rows[0].name}9`, phy: 19 });
    return made;
  };
  const listed = spare();
  listed.item.fabric.links.push(ghost(listed.first, [19]));
  assert.equal(pin(model(listed.item), listed.first.source, 19), pin(model(spare().item), listed.first.source, 19));
});

test("a cable whose PHY a bay's path also claims shows no width as its own", () => {
  // A bay's path names, by number, the far PHY of the HBA cable's first lane.
  const data = core();
  const far = data.fabric.expanders.find((entry) => entry.id === data.fabric.links[0].target);
  Object.assign(mprDevice(data), { expander_ids: [far.id], parent: far.dev_handle, expander_phy: 32 });
  const built = coreModel(data);
  const card = lanesCard(built, hbaCable(data, built));
  assert.match(card, /^4 reported; which PHYs the cable uses is not read \(.*Bay 00.* also claims it, as a disk port/, card);
  assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
});

test("a second link record between the same two ends, under another id, that lists other PHYs leaves both not read", () => {
  for (const first of [false, true]) {
    const data = core();
    const [link] = data.fabric.links;
    const twin = structuredClone(link);
    twin.id += "-b";
    for (const [key, phy] of [["source_phys", 4], ["target_phys", 36]]) twin.reported_lanes[key][3] = phy;
    Object.assign(twin.reported_lanes.lanes[3], { source_phy: 4, target_phy: 36 });
    beside(data.fabric.links, link, twin, first);
    const built = coreModel(data);
    for (const entry of built.links.filter((item) => item.source === link.source && item.target === link.target && item.lanes)) {
      assert.match(lanesCard(built, entry), /^4 reported; which PHYs the cable uses is not read/, `${entry.id}, first ${first}`);
    }
    assert.ok(!names(pin(built, MPR0, 0)), pin(built, MPR0, 0));
  }
});

test("another census record of the parent that lists a cable's PHY, read or not, leaves the cable not read", () => {
  for (const state of ["not_read", "read"]) {
    const found = twoCables();
    const port = found.census.find((entry) => entry.remote_id === found.first.target && entry.owner_id === found.first.source);
    found.census.push({ ...port, id: `${port.id}9`, state, phys: [3], phy_names: [], remote_kind: "not_read", remote_id: null, remote_sas_address: null, target_basis: "not_read" });
    const built = model(found.item);
    withheld(built, found.first, state);
    unowned(built);
  }
});

test("two cables whose lists give the same PHYs show no width or rate as their own", () => {
  // The first cable's PHYs, and the far-end PHYs the view infers for it, at 3 Gb/s.
  const slowDown = (found) => {
    const far = linkOf(model(twoCables().item), found.first.source, found.first.target).lanes.targetPhys;
    for (const [id, phys] of [[found.first.source, found.first.reported_lanes.source_phys], [found.first.target, far]]) {
      for (const row of phyRows(found.item.fabric, id).filter((entry) => phys.includes(entry.phy))) Object.assign(row, { speed: "3.0 Gbit", negotiated_link_rate: "3.0 Gbit", negotiated_rate_gbps: 3 });
    }
  };
  for (const slow of [false, true]) {
    const found = twoCables();
    Object.assign(found.second.reported_lanes, { source_phys: [...found.first.reported_lanes.source_phys], lane_count: 4 });
    if (slow) slowDown(found);
    const built = model(found.item);
    assert.match(withheld(built, found.first, `slow ${slow}`), /also lists it/);
    withheld(built, found.second, `slow ${slow}`);
    assert.match(textOf(cardRows(built, { kind: "link", id: linkOf(built, found.first.source, found.first.target).id })), /rate not this cable's/);
    unowned(built);
  }
  // Control: an uncontested cable at 3 Gb/s shows it and owns its fault.
  const control = twoCables();
  slowDown(control);
  const built = model(control.item);
  assert.equal(lanesCard(built, linkOf(built, control.first.source, control.first.target)), "4 · 3 Gb/s");
  assert.match(said1(built, /103 invalid dword/), /→/);
});

test("a second child that reports a cable's parent port, or its PHYs, leaves the cable not read", () => {
  const claims = {
    "the same port, PHYs and count": ([a, b]) => Object.assign(b.raw, { parent_port: a.raw.parent_port, source_phys: [...a.raw.source_phys], reported_num_phys: a.raw.reported_num_phys }),
    "the same port name only": ([a, b]) => { b.raw.parent_port = a.raw.parent_port; },
    "one of its PHYs under its own port": ([a, b]) => { b.raw.source_phys = [a.raw.source_phys[3], ...b.raw.source_phys]; },
  };
  for (const [what, change] of Object.entries(claims)) for (const census of [true, false]) {
    const found = twoCables();
    change(found.children);
    if (!census) delete found.item.fabric.raw.sas_transport_ports;
    const built = model(found.item);
    unowned(built);
    withheld(built, found.first, `${what}, census ${census}`);
    assert.ok(!names(pin(built, found.first.source, 3)), `${what}: ${pin(built, found.first.source, 3)}`);
  }
});

// The CORE cascade with 102 on the first PHY of mpr0's downstream expander.
function cascade102() {
  const data = core();
  const link = data.fabric.links.find((entry) => entry.kind === "expander-expander" && entry.source.includes(":mpr0:"));
  const child = data.fabric.expanders.find((entry) => entry.id === link.target);
  Object.assign(child.phys.find((row) => Number(row.phy) === 0), { counter_state: "measured", invalid_dword_count: 102, phy_reset_problem_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0 });
  return { data, link, child };
}

test("a handle the HBA itself carries is not one expander's, for the cascade as for the uplink", () => {
  const control = cascade102();
  assert.match(said1(coreModel(control.data), /102 invalid dword/), /→/);
  for (const rows of ["one PHY", "every PHY"]) {
    const found = cascade102();
    const own = controllerOf(found.data).phy_rows.filter((row) => row.controller_handle !== "0000");
    for (const row of rows === "one PHY" ? own.slice(-1) : own) row.controller_handle = found.child.dev_handle;
    const built = coreModel(found.data);
    const card = lanesCard(built, linkOf(built, found.link.source, found.link.target));
    assert.match(card, /^4 reported; which PHYs the cable uses is not read \(.*names more than one device of mpr0/, `${rows}: ${card}`);
    unowned(built, /102 invalid dword/);
    assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
  }
});

test("an HBA row whose device handle is the HBA's own names no device", () => {
  const data = directHandle();
  hbaRow(data, 4).device_handle = hbaRow(data, 4).controller_handle;
  const value = pin(coreModel(data), MPR0, 4);
  assert.match(value, /^destination not read: device handle 0001 is one mpr0 gives as its own/, value);
});

test("a lane list's SAS address for an end is that end's by every record of it, or the cable is not read", () => {
  const far = (data) => data.fabric.expanders.find((entry) => entry.id === data.fabric.links[0].target);
  const other = (data) => data.fabric.expanders.find((entry) => entry.controller_id === MPR0 && entry.id !== data.fabric.links[0].target).sas_address;
  const refused = (data, why) => {
    const built = coreModel(data);
    assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/, why);
    assert.ok(!statuses(built, "lanes").includes("pass"), `${why}: ${textOf(checksOf(built, "lanes"))}`);
  };
  for (const listed of ["another chip's address", "its target's address"]) for (const first of [false, true]) {
    // A second record of the target gives it another chip's address; the
    // list names that chip, or the target as its first record has it.
    const data = twinned(first, "expanders", far, (twin, whole) => { twin.sas_address = other(whole); });
    if (listed === "another chip's address") data.fabric.links[0].reported_lanes.attached_sas_address = other(data);
    refused(data, `${listed}, first ${first}`);
  }
  // A second record gives the target an address no other device has; the
  // list still names it as its first record does.
  for (const first of [false, true]) refused(twinned(first, "expanders", far, (twin) => { twin.sas_address = `${twin.sas_address.slice(0, -1)}f`; }), `an address of its own, first ${first}`);
  // The target's node carries no address, its one record another chip's,
  // and the list none at all.
  const borrowed = core();
  far(borrowed).sas_address = other(borrowed);
  Object.assign(node(borrowed.fabric, far(borrowed).id), { raw_id: "far" }).raw.sas_address = null;
  borrowed.fabric.links[0].reported_lanes.attached_sas_address = null;
  refused(borrowed, "another chip's address on its only record");
  // The target's node gives a second address in its own detail.
  const detail = core();
  node(detail.fabric, far(detail).id).raw.sas_address = `${far(detail).sas_address.slice(0, -1)}f`;
  refused(detail, "the node's own detail gives another address");
  // One record of the target, with another chip's address the list also
  // gives: the target's own node still has its own.
  const moved = core();
  far(moved).sas_address = other(moved);
  moved.fabric.links[0].reported_lanes.attached_sas_address = other(moved);
  refused(moved, "record and list give another chip's address");
  // The target's own address not read (on its record or its node), and the
  // list names another chip's.
  const unread = core();
  far(unread).sas_address = null;
  Object.assign(node(unread.fabric, far(unread).id), { raw_id: "far" }).raw.sas_address = null;
  unread.fabric.links[0].reported_lanes.attached_sas_address = other(unread);
  refused(unread, "target address not read");
  // Linux: two records of the child, one with another expander's address.
  for (const first of [false, true]) {
    const found = twoCables();
    const twin = structuredClone(found.children[0]);
    twin.raw.sas_address = found.children[1].raw.sas_address;
    beside(found.item.fabric.nodes, found.children[0], twin, first);
    const built = model(found.item);
    withheld(built, found.first, `linux, first ${first}`);
    unowned(built);
  }
});

test("a Linux cable read from its child's report alone says that is its basis", () => {
  const older = parentPort();
  delete older.item.fabric.raw.sas_transport_ports;
  const plain = model(older.item);
  const rows = cardRows(plain, { kind: "link", id: linkOf(plain, older.link.source, older.link.target).id });
  assert.equal(rows.find((entry) => entry.label === "Lanes").value, "4 · 12 Gb/s");
  assert.match(rows.find((entry) => entry.label === "Basis")?.value || "", /^the parent port as its child reports it; the payload has no port census$/);
  const found = parentPort();
  const built = model(found.item);
  assert.ok(!cardRows(built, { kind: "link", id: linkOf(built, found.link.source, found.link.target).id }).some((entry) => entry.label === "Basis"));
});

// --- Records of one endpoint are joined toward not read, in either order -------

// The CORE cascade with a second record of one endpoint, changed, before
// or after the first.
function twinned(first, list, pick, change) {
  const data = core();
  const twin = structuredClone(pick(data));
  change(twin, data);
  data.fabric[list][first ? "unshift" : "push"](twin);
  return data;
}
const hbaCable = (data, built) => linkOf(built, data.fabric.links[0].source, data.fabric.links[0].target);
const names = (value) => /^to |^cable to |attached directly|outside this view/.test(value);

for (const first of [false, true]) {
  const order = first ? "first" : "last";
  // What the builder found a handle names, kept beside one record only: a
  // marker (the first four) survives, and a kind the other record does not
  // give is a disagreement.
  for (const state of ["ambiguous", "mismatch", "not_read", "unlisted_expander", "end_device"]) {
    test(`duplicate HBA records: the one that says ${state} of a handle ${order}`, () => {
      const data = twinned(first, "controllers", controllerOf, (twin) => { twin.device_handle_states = { ...twin.device_handle_states, 0: state }; });
      const built = coreModel(data);
      const value = pin(built, MPR0, 0);
      assert.match(value, /not read/, value);
      assert.ok(!names(value), value);
      assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/);
      assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
    });
  }
  test(`duplicate expander records: one gives a second expander the far end's handle, ${order}`, () => {
    const far = (data) => data.fabric.expanders.find((entry) => entry.id === data.fabric.links[0].target);
    const child = (data) => data.fabric.expanders.find((entry) => entry.controller_id === MPR0 && entry.id !== data.fabric.links[0].target);
    const data = twinned(first, "expanders", child, (twin, whole) => { twin.dev_handle = far(whole).dev_handle; });
    const built = coreModel(data);
    const value = pin(built, MPR0, 0);
    assert.match(value, /not read/, value);
    assert.ok(!names(value), value);
    assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/);
  });
  test(`duplicate expander records: a row only one of them reads names the HBA, ${order}`, () => {
    const far = (data) => data.fabric.expanders.find((entry) => entry.id === data.fabric.links[0].target);
    const data = twinned(first, "expanders", far, (twin) => {
      const lane = twin.phys.find((row) => Number(row.phy) === 32);
      Object.assign(twin.phys.find((row) => Number(row.phy) === 4), { dev_handle: lane.dev_handle, device: lane.device, speed: lane.speed, remote_phy: "4" });
    });
    const built = coreModel(data);
    assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/);
    assert.ok(!statuses(built, "lanes").includes("pass"), textOf(checksOf(built, "lanes")));
  });
  test(`duplicate expander records: one places another unit's expander in this unit, ${order}`, () => {
    // The other unit's first expander is given a handle this unit does not
    // use, so no handle collides; its rows still name an HBA handle and PHYs
    // as this unit's far end does (those repeat across units).
    const rehandled = () => {
      const data = core();
      const other = data.fabric.expanders.find((entry) => entry.controller_id !== MPR0 && entry.parent === data.fabric.expanders[0].parent);
      const [from, to] = [other.dev_handle, `${other.dev_handle.slice(0, -2)}f${other.dev_handle.slice(-1)}`];
      for (const record of [...data.fabric.expanders, ...data.fabric.controllers].filter((entry) => (entry.controller_id || entry.id) === other.controller_id)) {
        for (const key of ["dev_handle", "parent"]) if (record[key] === from) record[key] = to;
        for (const row of record.phys || record.phy_rows) for (const key of ["dev_handle", "device_handle"]) if (row[key] === from) row[key] = to;
      }
      return { data, other };
    };
    const control = rehandled();
    assert.equal(lanesCard(coreModel(control.data), hbaCable(control.data, coreModel(control.data))), "4: 4 at 12 Gb/s");
    const { data, other } = rehandled();
    const twin = { ...structuredClone(other), controller_id: MPR0 };
    data.fabric.expanders[first ? "unshift" : "push"](twin);
    const built = coreModel(data);
    const card = lanesCard(built, hbaCable(data, built));
    assert.match(card, /^4 reported; which PHYs the cable uses is not read .*rows of the unit name PHY \d+ of mpr0/, card);
  });
  // Already held before records were joined row by row; kept beside the
  // cases above so each part of the join is held by an assertion.
  test(`duplicate expander records: one gives the far end another handle, ${order}`, () => {
    const data = twinned(first, "expanders", (whole) => whole.fabric.expanders.find((entry) => entry.id === whole.fabric.links[0].target), (twin) => { twin.dev_handle = `${twin.dev_handle.slice(0, -1)}f`; });
    const built = coreModel(data);
    assert.ok(!names(pin(built, MPR0, 0)), pin(built, MPR0, 0));
    assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/);
  });
  test(`duplicate expander records: one has no row for a lane's PHY, ${order}`, () => {
    const data = twinned(first, "expanders", (whole) => whole.fabric.expanders.find((entry) => entry.id === whole.fabric.links[0].target), (twin) => { twin.phys = twin.phys.filter((row) => Number(row.phy) !== 32); });
    const built = coreModel(data);
    assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/);
  });
  test(`duplicate HBA records: one lists a handle among the unit's ambiguous handles, ${order}`, () => {
    const data = twinned(first, "controllers", controllerOf, (twin) => { twin.ambiguous_device_handles = [twin.phy_rows[0].device_handle]; });
    const built = coreModel(data);
    assert.ok(!names(pin(built, MPR0, 0)), pin(built, MPR0, 0));
    assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/);
  });
  test(`duplicate expander records that differ in nothing keep a counter one row reads, ${order}`, () => {
    // A row that names the counters it did not read has the rest.
    const far = (data) => data.fabric.expanders.find((entry) => entry.id === data.fabric.links[0].target);
    const partial = (record) => Object.assign(record.phys.find((row) => Number(row.phy) === 32), { counter_state: "partial", not_read: ["phy_reset_problem_count"], invalid_dword_count: 104, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: null });
    const single = core();
    partial(far(single));
    const data = core();
    partial(far(data));
    data.fabric.expanders[first ? "unshift" : "push"](structuredClone(far(data)));
    assert.match(said1(coreModel(single), /104 invalid dword/), /→/);
    assert.equal(said1(coreModel(data), /104 invalid dword/), said1(coreModel(single), /104 invalid dword/));
  });
}

test("duplicate HBA records keep a handle marker both carry, however the PHY number is written", () => {
  const padded = () => {
    const data = core();
    const hba = controllerOf(data);
    for (const row of hba.phy_rows) row.phy = row.phy.padStart(2, "0");
    hba.device_handle_states = { ...hba.device_handle_states, "00": "ambiguous" };
    return data;
  };
  const single = coreModel(padded());
  assert.match(pin(single, MPR0, 0), /not read/);
  const data = padded();
  data.fabric.controllers.push(structuredClone(controllerOf(data)));
  const built = coreModel(data);
  assert.equal(pin(built, MPR0, 0), pin(single, MPR0, 0));
  assert.match(lanesCard(built, hbaCable(data, built)), /^4 reported; which PHYs the cable uses is not read/);
});

test("a CORE node's conflicting address disowns the cascade in either record order", () => {
  for (const first of [true, false]) {
    const { data, link, child } = cascade102();
    const parent = node(data.fabric, link.source);
    const other = structuredClone(parent);
    other.raw.sas_address = child.sas_address;
    beside(data.fabric.nodes, parent, other, first);
    const built = coreModel(data);
    unowned(built, /102 invalid dword/);
    assert.match(lanesCard(built, linkOf(built, link.source, link.target)), /not read/);
    assert.ok(!statuses(built, "lanes").includes("pass"));
  }
});

test("renaming a Linux cable leaves its card and the lanes check unchanged", () => {
  const item = fixture("dual-domain-linux");
  const link = item.fabric.links.find((entry) => entry.reported_lanes);
  const before = model(item);
  const card = cardRows(before, { kind: "link", id: link.id });
  assert.equal(lanesCard(before, link), "8 · 12 Gb/s");
  link.id = "custom-cable";
  const built = model(item);
  assert.deepEqual(cardRows(built, { kind: "link", id: link.id }), card);
  assert.deepEqual(checksOf(built, "lanes"), checksOf(before, "lanes"));
});

test("competing Linux cable records with different PHY lists stay unverified in either order", () => {
  for (const first of [false, true]) {
    const item = fixture("dual-domain-linux");
    const link = item.fabric.links.find((entry) => entry.reported_lanes);
    link.id = "custom-cable";
    const rival = structuredClone(link);
    rival.id = "other-cable";
    rival.reported_lanes.source_phys.pop();
    rival.reported_lanes.lane_count--;
    beside(item.fabric.links, link, rival, first);
    const built = model(item);
    for (const cable of [link, rival]) {
      const card = lanesCard(built, cable);
      assert.ok(card.startsWith(`${cable.reported_lanes.lane_count} reported; which PHYs the cable uses is not read`), card);
      assert.doesNotMatch(card, /Gb\/s/);
    }
    assert.match(textOf(checksOf(built, "lanes")), /their lane lists differ/);
  }
});

test("a second Linux link ID claiming identical PHYs counts even when its record is not drawn", () => {
  for (const duplicateId of [false, true]) for (const first of [true, false]) {
    const item = fixture("dual-domain-linux");
    const [link, other] = item.fabric.links.filter((entry) => entry.reported_lanes);
    const rival = structuredClone(link);
    rival.id = duplicateId ? other.id : "other-cable";
    beside(item.fabric.links, duplicateId ? other : link, rival, first);
    const built = model(item);
    const reason = /which PHYs the cable uses is not read .*a second link ID between these ends also claims it/;
    assert.match(lanesCard(built, link), reason);
    if (!duplicateId || first) assert.match(lanesCard(built, rival), reason);
    assert.match(textOf(checksOf(built, "lanes")), /a second link ID between these ends also claims it/);
  }
});

test("two link IDs cannot verify two cables on the same four PHY pairs", () => {
  for (const sameId of [true, false]) for (const first of [true, false]) {
    const data = core();
    const link = data.fabric.links.find((row) => row.kind === "controller-expander");
    const other = structuredClone(link);
    if (!sameId) other.id += "-another";
    beside(data.fabric.links, link, other, first);
    const built = coreModel(data);
    if (sameId) assert.match(textOf(checksOf(built, "lanes")), /4 cables, each x4/);
    else {
      const svg = T.renderTopology(built, {});
      for (const cable of [link, other]) {
        assert.match(lanesCard(built, cable), /not read/);
        const description = [...svg.matchAll(/<g class="topo-link-label[^>]*data-topo-link="([^"]*)" aria-label="([^"]*)"/g)].find((match) => unescape(match[1]) === cable.id)?.[2];
        assert.match(description, /membership and rate not read/);
        assert.doesNotMatch(description, /4 lanes, 12 Gb\/s/);
      }
      assert.ok(!statuses(built, "lanes").includes("pass"));
    }
  }
});

test("the grouped fan-out card claims only its members' verified rates", () => {
  for (const order of ["control", "first", "last"]) {
    const data = fixture("fanout-5");
    const [a, b] = data.fabric.links.filter((row) => row.kind === "expander-expander" && row.reported_lanes);
    if (order !== "control") {
      const other = structuredClone(b);
      other.reported_lanes.source_phys = [...a.reported_lanes.source_phys];
      beside(data.fabric.links, b, other, order === "first");
    }
    const built = model(data);
    const group = built.links.find((row) => row.stubCables);
    assert.equal(group.labelLines.some((line) => line.includes("width not read")), order !== "control");
    const rows = cardRows(built, { kind: "link", id: group.id });
    for (const cable of group.stubCables.slice(0, 2)) {
      const row = rows.find((entry) => entry.label === `to ${cable.targetName}`);
      if (order === "control") assert.match(row.value, /x4 · 12 Gb\/s/);
      else {
        assert.match(lanesCard(built, cable), /not read/);
        assert.match(row.value, /not read/);
        assert.doesNotMatch(row.value, /Gb\/s/);
      }
    }
  }
});

test("only established widths can warn about a narrower cable on the same backplane", () => {
  for (const verified of [true, false]) {
    const data = fixture("dual-domain-linux");
    const [link] = data.fabric.links.filter((entry) => entry.reported_lanes);
    link.reported_lanes.source_phys.pop();
    link.reported_lanes.lane_count--;
    if (verified) {
      const child = node(data.fabric, link.target).raw;
      child.source_phys.pop();
      child.reported_num_phys--;
      const port = data.fabric.raw.sas_transport_ports.find((entry) => entry.remote_id === link.target && entry.owner_id === link.source);
      port.phys.pop();
      port.phy_names.pop();
    }
    const compare = (built) => checksOf(built, "lanes").flatMap((check) => check.lines).some((line) => line.includes("another cable into the same backplane"));
    assert.equal(compare(model(data)), verified, "disputed narrower member");
    const [other] = data.fabric.links.filter((entry) => entry.reported_lanes && entry !== link);
    other.reported_lanes.source_phys.push(40);
    other.reported_lanes.lane_count++;
    assert.equal(compare(model(data)), false, "disputed wider reference");
  }
});

test("duplicate records that differ in nothing say what one record says", () => {
  const single = coreModel(core());
  for (const [list, pick] of [["controllers", controllerOf], ["expanders", (data) => data.fabric.expanders[0]]]) for (const first of [false, true]) {
    const data = twinned(first, list, pick, () => {});
    const built = coreModel(data);
    assert.equal(pin(built, MPR0, 0), pin(single, MPR0, 0));
    assert.match(pin(built, MPR0, 0), /^to /);
    assert.equal(lanesCard(built, hbaCable(data, built)), "4: 4 at 12 Gb/s");
    assert.equal(textOf(built.checks), textOf(single.checks));
  }
});
