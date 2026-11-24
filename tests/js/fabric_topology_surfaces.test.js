"use strict";

// Every surface reads the one attribution, including the inspectors a pin
// opens (#1504). A selected cable, bus, HBA or bay never shows a reading
// whose owner is not read, or is another's, as its own. A disk attached
// directly to an HBA is a disk-port subject, placed on its HBA. A cable's
// members are validated against its measured pairs at both ends, for every
// consumer. A value names the observation it came from. A count never
// changes the fact it reports. Every payload is synthetic, in the
// producer's shapes.

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
const textOf = (value) => JSON.stringify(value);
const node = (fabric, id) => fabric.nodes.find((entry) => entry.id === id);
const phyRows = (fabric, id) => node(fabric, id).raw.phys || node(fabric, id).raw.phy_rows;
const cardRows = (built, ref) => T.cardFor(built, ref)?.rows || [];
const unescape = (value) => value.replaceAll("&#39;", "'").replaceAll("&amp;", "&").replaceAll("&lt;", "<").replaceAll("&gt;", ">").replaceAll("&quot;", "\"");
// The rows of an inspector as rendered: [warn class, label, value].
const kv = (built, ref) => [...(T.detailFor(built, ref)?.html || "").matchAll(/<div class="kv-row([^"]*)"[^>]*><span[^>]*>([^<]*)<\/span><span[^>]*>([^<]*)<\/span>/g)]
  .map((match) => [match[1].trim(), unescape(match[2]), unescape(match[3])]);
const said = (built, pattern) => [...critical(built, "cable-errors"), ...critical(built, "port-errors")].filter((line) => pattern.test(line));
const X4 = "expander:controller:host1:expander-1:0";
const HOST1 = "controller:host1";
const unread = (entry, ...keys) => {
  for (const key of keys) entry[key] = null;
  entry.counter_state = "not_read";
  entry.not_read = [...new Set([...(entry.not_read || []), ...keys])];
};

// A disk attached directly to host1's PHY 12, in the builder's shapes: an
// HBA PHY row, a census end-device port it owns, and, when mapped, Bay 07's
// own path through that port.
function direct({ mapped = false, count = 101, missing = false, kind = "end_device" } = {}) {
  const item = fixture("wide-x4-sata");
  const rows = phyRows(item.fabric, HOST1);
  if (!missing) rows.push({ ...rows[0], name: "phy-1:12", phy: 12, not_read: [], counter_state: "measured", invalid_dword_count: count, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const id = "sas-port:port-1:12";
  item.fabric.raw.sas_transport_ports.push({ id, owner_id: HOST1, state: "read", phy_names: ["phy-1:12"], phys: [12], remote_kind: kind, remote_id: null,
    remote_sas_address: "5eee001e00000123", has_block: kind === "end_device" ? true : null, bay_identifier: mapped ? 7 : null, enclosure_identifier: null, target_basis: "reported" });
  item.fabric.raw.sas_transport_port_index[id] = { id, owner_id: HOST1, phy_names: ["phy-1:12"], state: "read" };
  if (mapped) {
    const bay = node(item.fabric, "bay:7");
    Object.assign(bay.metrics, { present: true, device_name: "wdfresh", sas_address: "5eee001e00000123", transport_protocol: "SAS", target_port_protocol: "SSP" });
    const row = { controller: "host1", state: "unknown", device_name: "wdfresh", transport_protocol: "SAS", attached_sas_address: "5aaa000000000101", phy_identifier: 12,
      target_port_protocol: "SSP", sas_address: "5eee001e00000123", member_device_name: "wdfresh", bay_identifier: 7, port: "port-1:12", expander_ids: [], sas_transport_port_id: id, source: "linux_sas_transport" };
    Object.assign(item.fabric.traces.find((entry) => entry.id === "bay:7").metrics, { sas_transport_ports: [row], path_states: [row] });
  }
  return item;
}

// --- 1: inspectors read the attribution ------------------------------------------

test("a selected cable never shows a disk row's fault as its own (disk-uplink duplicate)", () => {
  const item = fixture("wide-x4-sata");
  phyRows(item.fabric, X4).find((entry) => entry.name === "phy-1:0:0").invalid_dword_count = 101;
  phyRows(item.fabric, X4).find((entry) => entry.name === "phy-1:0:16").phy = 0;
  const built = model(item);
  const link = built.links.find((entry) => entry.target === X4 && entry.lanes);
  const rows = kv(built, { kind: "link", id: link.id });
  assert.ok(!rows.some(([, , value]) => /101 invalid dword/.test(value)), textOf(rows));
  assert.ok(rows.some(([cls, label, value]) => /PHY 0\b/.test(label) && /owner not read/.test(value) && !cls), textOf(rows));
});

test("two cables listing one HBA PHY: neither selected cable shows its fault", () => {
  const item = fixture("fanout-2");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  const leaf = item.fabric.nodes.filter((entry) => entry.kind === "expander")[1];
  item.fabric.links.push({ ...structuredClone(uplink), id: `controller-expander:${uplink.source}->${leaf.id}`, target: leaf.id });
  phyRows(item.fabric, uplink.source)[0].invalid_dword_count = 101;
  const built = model(item);
  for (const link of built.links.filter((entry) => entry.source === uplink.source && entry.lanes)) {
    const rows = kv(built, { kind: "link", id: link.id });
    assert.ok(!rows.some(([, , value]) => /101 invalid dword/.test(value)), `${link.id}: ${textOf(rows)}`);
  }
});

test("a bay's PHY in a cascade list: the selected cable, bus and bay agree its owner is not read", () => {
  const item = fixture("fanout-2");
  const port = model(item).bays[0].ports[0];
  const cascade = item.fabric.links.find((link) => link.kind === "expander-expander" && link.source === port.expanderId);
  cascade.reported_lanes.source_phys[0] = port.phy.phy;
  phyRows(item.fabric, port.expanderId).find((entry) => entry.phy === port.phy.phy).invalid_dword_count = 101;
  const built = model(item);
  const surfaces = [{ kind: "link", id: cascade.id }, { kind: "link", id: built.nodeById.get(port.expanderId).busId }, { kind: "bay", id: built.bays[0].bayId }];
  for (const ref of surfaces) {
    const rows = kv(built, ref);
    assert.ok(!rows.some(([cls, , value]) => /101 invalid dword/.test(value) || (cls && /owner not read/.test(value))), `${ref.kind} ${ref.id}: ${textOf(rows)}`);
  }
});

// --- 2: HBA-owned disk ports ------------------------------------------------------

test("a disk on an HBA PHY with a measured fault never lets the disk-port check pass", () => {
  const built = model(direct());
  assert.ok(statuses(built, "port-errors").includes("critical"), textOf(checksOf(built, "port-errors")));
  assert.equal(said(built, /101 invalid dword/).length, 1, textOf(built.checks));
  assert.match(said(built, /101 invalid dword/)[0], /host1: PHY 12/);
  // A quiet one is counted: 12 disk port PHYs, not 11.
  assert.match(textOf(checksOf(model(direct({ count: 0 })), "port-errors")), /no errors on 12 disk port PHYs/);
});

test("a mapped disk on an HBA PHY is placed on its HBA, and every surface reads its PHY", () => {
  const built = model(direct({ mapped: true }));
  const placement = textOf(checksOf(built, "ports"));
  assert.doesNotMatch(placement, /expander outside this backplane/);
  const bay = built.bayById.get("bay:7");
  const port = bay.ports[0];
  assert.equal(port.expanderId, HOST1);
  assert.equal(port.phy?.phy, 12);
  const portErrors = cardRows(built, { kind: "port", id: port.key }).find((entry) => entry.label === "PHY errors");
  assert.equal(portErrors.warn, "critical", textOf(portErrors));
  assert.match(textOf(critical(built, "port-errors")), /Bay 07/);
  const hba = cardRows(built, { kind: "node", id: HOST1 });
  assert.doesNotMatch(textOf(hba), /not in this enclosure's payload/);
  const detail = kv(built, { kind: "node", id: HOST1 }).find(([, label]) => label === "PHY 12");
  assert.doesNotMatch(detail[2], /outside this view/);
  assert.match(detail[2], /Bay 07/);
});

test("an HBA disk PHY the census lists without its row is not read and stays in every denominator", () => {
  const before = model(direct({ count: 0 }));
  const built = model(direct({ missing: true }));
  const total = (note) => Number(/read on \d+ of (\d+)/.exec(note.measured.join(" "))[1]);
  assert.equal(total(built.transportNote), total(before.transportNote));
  assert.ok(!statuses(built, "port-errors").includes("pass"), textOf(checksOf(built, "port-errors")));
  assert.match(textOf(cardRows(built, { kind: "node", id: HOST1 })), /1 PHY not read/);
});

test("an HBA PHY the census lists as an expander link, in no cable, is an uplink subject", () => {
  const item = direct({ kind: "expander" });
  unread(phyRows(item.fabric, HOST1).find((entry) => entry.phy === 12), "invalid_dword_count");
  const built = model(item);
  assert.ok(!statuses(built, "cable-errors").includes("pass"), textOf(checksOf(built, "cable-errors")));
});

test("a cable list naming a PHY the census calls a disk does not take its fault", () => {
  const item = direct();
  item.fabric.links.find((link) => link.kind === "controller-expander").reported_lanes.source_phys[0] = 12;
  const built = model(item);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(built.checks));
  assert.match(lines[0], /owner not read/);
});

test("an HBA does not call lanes outside the payload when the census names their expander", () => {
  const item = fixture("wide-x4-sata");
  const uplink = item.fabric.links.find((link) => link.kind === "controller-expander");
  uplink.reported_lanes = null;
  const built = model(item);
  assert.doesNotMatch(textOf(cardRows(built, { kind: "node", id: uplink.source })), /not in this enclosure's payload/);
  assert.ok(!kv(built, { kind: "node", id: uplink.source }).some(([, label, value]) => /^PHY [0-3]$/.test(label) && /outside this view/.test(value)));
});

// --- 3: membership against the measured pairs --------------------------------------

const cascade = () => structuredClone(H.producerCases().core_cascade);
const coreModel = (data) => T.buildTopologyModel(data.fabric, data.options || { inventory: data.snapshot });

test("a pair table that disagrees with the lists is membership not read, for every consumer", () => {
  for (const mode of ["pair only", "foreign far PHY", "slow repeat"]) {
    const data = cascade();
    const link = data.fabric.links.find((entry) => entry.reported_lanes);
    const lanes = link.reported_lanes;
    if (mode === "pair only") lanes.lanes[3].target_phy = lanes.lanes[2].target_phy;
    if (mode === "foreign far PHY") Object.assign(lanes.lanes[0], { target_phy: 0, negotiated_rate_gbps: 3 });
    if (mode === "slow repeat") {
      lanes.target_phys[3] = lanes.target_phys[2];
      Object.assign(lanes.lanes[3], { target_phy: lanes.target_phys[2], negotiated_rate_gbps: 3 });
    }
    const built = coreModel(data);
    const lanesCheck = checksOf(built, "lanes");
    assert.ok(!statuses(built, "lanes").includes("pass"), `${mode}: ${textOf(lanesCheck)}`);
    assert.ok(!statuses(built, "lanes").includes("critical"), `${mode}: ${textOf(lanesCheck)}`);
    assert.match(textOf(lanesCheck), /membership is not read/, mode);
    const hba = cardRows(built, { kind: "node", id: link.source }).find((entry) => entry.label === "In this view");
    assert.doesNotMatch(hba?.value || "", /below the maximum/, `${mode}: ${hba?.value}`);
    for (const ref of [{ kind: "link", id: link.id }, { kind: "node", id: link.source }]) {
      assert.ok(!kv(built, ref).some(([cls, label]) => cls && /PHY/.test(label)), `${mode} ${ref.id}: ${textOf(kv(built, ref))}`);
    }
  }
});

test("an overlong far list claims no PHY: its extra member's fault is not the cable's", () => {
  const data = cascade();
  const link = data.fabric.links.find((entry) => entry.reported_lanes);
  link.reported_lanes.target_phys.push(0);
  const row = data.fabric.expanders.find((entry) => entry.id === link.target).phys.find((entry) => Number(entry.phy) === 0);
  Object.assign(row, { invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0, counter_state: "measured" });
  const built = coreModel(data);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(built.checks));
  assert.doesNotMatch(lines[0], /→/, lines[0]);
  const chip = cardRows(built, { kind: "node", id: link.target }).find((entry) => entry.label === "PHY errors");
  assert.doesNotMatch(chip.value, /PHY 0 \(cable from/, chip.value);
});

// --- 4: provenance -----------------------------------------------------------------

test("a measured fault on a bay's PHY whose own row calls it an expander link is not the disk's", () => {
  const data = structuredClone(DIRECT);
  const expander = data.fabric.expanders[0];
  const device = data.fabric.traces.find((entry) => entry.id === "bay:0").metrics.mpr_devices.find((entry) => (entry.expander_ids || []).includes(expander.id));
  expander.phys.push({ ...expander.phys[0], phy: "4", device: "SMP Target", dev_handle: device.handle, counter_state: "measured",
    invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
  const chip = cardRows(built, { kind: "node", id: expander.id }).find((entry) => entry.label === "PHY errors");
  assert.doesNotMatch(chip.value, /PHY 4 \(Bay 00\)/, chip.value);
  assert.doesNotMatch(built.bays[0].extra.join(" "), /101 invalid dword/);
  const lines = said(built, /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(built.checks));
  assert.match(lines[0], /owner not read/);
});

test("each disputed PHY of a cable is named with its own claimants", () => {
  const item = fixture("fanout-2");
  const start = model(item);
  const port = start.bays[0].ports[0];
  const link = item.fabric.links.find((entry) => entry.kind === "expander-expander" && entry.source === port.expanderId);
  link.reported_lanes.source_phys.splice(0, 2, ...start.bays.slice(0, 2).map((bay) => bay.ports[0].phy.phy));
  const built = model(item);
  const end = cardRows(built, { kind: "link", id: link.id }).find((entry) => /^Errors at/.test(entry.label) && /owner not read/.test(entry.value));
  for (const bay of start.bays.slice(0, 2)) {
    const phy = bay.ports[0].phy.phy;
    assert.match(end.value, new RegExp(`PHY ${phy}: owner not read: ${bay.label} and`), end.value);
  }
});

test("only the observation that read a PHY in part is named as partial", () => {
  const item = fixture("dual-domain-linux");
  const seven = node(item.fabric, "expander:controller:host7:expander-7:0");
  const control = node(item.fabric, "controller:host7");
  const copies = JSON.parse(JSON.stringify([seven, control]).replaceAll("host7", "host8").replaceAll("-7:", "-8:").replaceAll("5aaa000000000701", "5aaa000000000801"));
  item.fabric.nodes.push(...copies);
  const link = item.fabric.links.find((entry) => entry.target === seven.id);
  item.fabric.links.push(JSON.parse(JSON.stringify(link).replaceAll("host7", "host8").replaceAll("-7:", "-8:")));
  unread(copies[0].raw.phys.find((entry) => entry.phy === 28), "invalid_dword_count");
  const built = model(item);
  const chip = built.nodes.find((entry) => entry.kind === "expander" && entry.rows?.length > 2);
  const via = cardRows(built, { kind: "node", id: chip.id }).find((entry) => /Also read via/.test(entry.label)).value;
  assert.match(via, /host8: counters not all read on PHY 28/, via);
  // host7 read PHY 28 in full, as host6 did: it is never named partial.
  assert.doesNotMatch(via, /host7: [^;]*counters not all read/, via);
});

test("a disagreement is said of the PHY whose observations disagree", () => {
  const item = fixture("dual-domain-linux");
  phyRows(item.fabric, "expander:controller:host6:expander-6:0").find((entry) => entry.phy === 29).invalid_dword_count = 101;
  for (const id of ["expander:controller:host6:expander-6:0", "expander:controller:host7:expander-7:0"]) phyRows(item.fabric, id).find((entry) => entry.phy === 28).invalid_dword_count = 1;
  const line = critical(model(item), "cable-errors")[0];
  assert.match(line, /PHY 29: 101 invalid dword; the 2 observations of this PHY disagree/, line);
  assert.doesNotMatch(line.slice(line.indexOf("PHY 28")), /disagree/, line);
});

test("one physical fault two cables' observations reach is said once", () => {
  const item = fixture("dual-domain-linux");
  const seven = phyRows(item.fabric, "expander:controller:host7:expander-7:0");
  const [twenty, twentyEight] = [seven.find((entry) => entry.phy === 20), seven.find((entry) => entry.phy === 28)];
  [twenty.phy, twentyEight.phy] = [28, 20];
  phyRows(item.fabric, "expander:controller:host6:expander-6:0").find((entry) => entry.phy === 28).invalid_dword_count = 101;
  const built = model(item);
  assert.equal(said(built, /101 invalid dword/).length, 1, textOf(built.checks.filter((entry) => entry.status === "critical")));
});

// --- 5: counts never change the fact ------------------------------------------------

test("a PASS counts cable ends and uplink PHYs apart", () => {
  const data = cascade();
  for (const expander of data.fabric.expanders) for (const row of expander.phys) Object.assign(row, { counter_state: "measured", invalid_dword_count: 0, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const link = data.fabric.links.find((entry) => entry.reported_lanes);
  const rows = data.fabric.expanders.find((entry) => entry.id === link.target).phys;
  rows.push({ ...rows.find((entry) => Number(entry.phy) === 32), phy: "91" });
  const found = checksOf(coreModel(data), "cable-errors");
  assert.doesNotMatch(textOf(found), /\b7 cable ends\b/, textOf(found));
  assert.match(textOf(found), /1 uplink PHY/, textOf(found));
});

test("a slot reported both mapped and unmapped is one disputed slot, whichever comes first", () => {
  const texts = [true, false].map((first) => {
    const item = fixture("empty-survey");
    const states = item.fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states;
    states[first ? "unshift" : "push"]({ ...states[0], state: "unmapped" });
    return textOf(checksOf(model(item), "path-states"));
  });
  assert.equal(texts[0], texts[1]);
  assert.match(texts[0], /3 slots mapped \(holding a disk\)/);
  assert.match(texts[0], /1 slot reported both mapped and unmapped/);
});

test("PHYs a census lists without rows are said in one order, whatever the census order", () => {
  const outputs = [false, true].map((reverse) => {
    const item = fixture("fanout-2");
    const chips = item.fabric.nodes.filter((entry) => entry.kind === "expander").slice(0, 2);
    let text = JSON.stringify(item);
    chips.forEach((chip, index) => { text = text.replaceAll(chip.id, index ? "expander:chip01" : "expander:chip1"); });
    const data = JSON.parse(text);
    const census = data.fabric.raw.sas_transport_ports;
    const proto = census.find((port) => port.remote_kind === "end_device");
    const owners = ["expander:chip1", "expander:chip01", "expander:chip1"];
    (reverse ? [...owners].reverse() : owners).forEach((owner_id, index) => census.push({ ...proto, id: `sas-port:extra${index}`, owner_id, phys: [91], phy_names: ["phy-unread-91"] }));
    return checksOf(model(data), "port-errors").flatMap((entry) => entry.lines).filter((line) => /PHY 91/.test(line));
  });
  assert.equal(outputs[0].length, 2, textOf(outputs[0]));
  assert.deepEqual(outputs[0], outputs[1]);
});

// --- each keyed guard observed ------------------------------------------------------

test("a lane row's far PHY whose number is on two rows shows no reading as the cable's", () => {
  const data = cascade();
  const link = data.fabric.links.find((entry) => entry.reported_lanes);
  const rows = data.fabric.expanders.find((entry) => entry.id === link.target).phys;
  const original = rows.find((entry) => Number(entry.phy) === link.reported_lanes.lanes[0].target_phy);
  rows.push({ ...original, counter_state: "measured", invalid_dword_count: 101, running_disparity_error_count: 0, loss_of_dword_synchronization_count: 0, phy_reset_problem_count: 0 });
  const built = coreModel(data);
  const lane = kv(built, { kind: "link", id: link.id }).find(([, label]) => label.startsWith(`Lane PHY ${link.reported_lanes.lanes[0].source_phy} ↔`));
  assert.doesNotMatch(lane[2], /101 invalid dword/, lane[2]);
  assert.match(lane[2], /owner not read: its number is on more than one row/, lane[2]);
});

test("an HBA PHY in no cable and no census still counts in the note and on the card", () => {
  const before = model(fixture("wide-x4-sata"));
  const item = fixture("wide-x4-sata");
  const rows = phyRows(item.fabric, HOST1);
  rows.push({ ...rows[0], name: "phy-1:15", phy: 15 });
  const built = model(item);
  const total = (one) => Number(/read on \d+ of (\d+)/.exec(one.transportNote.measured.join(" "))[1]);
  assert.equal(total(built), total(before) + 1);
  assert.match(cardRows(built, { kind: "node", id: HOST1 }).find((entry) => entry.label === "PHY errors").value, /none on 13 PHYs/);
});

test("a number two rows share, one claimed by a bay, has its owner not read", () => {
  const item = fixture("wide-x4-sata");
  const rows = phyRows(item.fabric, X4);
  rows.find((entry) => entry.name === "phy-1:0:0").invalid_dword_count = 101;
  rows.find((entry) => entry.name === "phy-1:0:10").phy = 0;
  const lines = said(model(item), /101 invalid dword/);
  assert.equal(lines.length, 1, textOf(lines));
  assert.match(lines[0], /owner not read: its number is on more than one row/);
  assert.doesNotMatch(lines[0], /^Bay 00/);
});

test("a direct port naming an HBA PHY number two rows share is matched to neither row", () => {
  const item = direct({ mapped: true });
  const rows = phyRows(item.fabric, HOST1);
  rows.push({ ...rows.find((entry) => entry.phy === 12), name: "phy-1:13", invalid_dword_count: 0 });
  const port = model(item).bayById.get("bay:7").ports[0];
  assert.equal(port.phy, null);
  assert.match(port.why, /more than one row of the HBA's PHY table/);
});

test("an observation is compared with the chip's own only where that reads one row per PHY", () => {
  const item = fixture("dual-domain-linux");
  const six = phyRows(item.fabric, "expander:controller:host6:expander-6:0");
  six.find((entry) => entry.phy === 29).phy = 28;
  const built = model(item);
  const chip = built.nodes.find((entry) => entry.kind === "expander" && entry.rows?.length > 1);
  const via = cardRows(built, { kind: "node", id: chip.id }).find((entry) => /Also read via/.test(entry.label)).value;
  assert.match(via, /host7: counters missing or on more than one row on PHY [^;]*28/, via);
});
