"use strict";

// Topology shows each item from its own evidence only: an owner the shared
// resolver rejected, a controller named by a Friendly Label, a pinned path
// whose rows moved, a cable end or pairing that was not read, a partial
// neighbour read and an inventory grid that spans enclosures are each shown
// as read, inferred or not read, never as another item's data (#1504).
// Every payload is a synthetic fixture.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");
const { loadTopology, producerCases } = H;

const T = loadTopology();
const FIXTURES = require("../fixtures/sas_fabric/topology_generality.json").fixtures;
const DIRECT = require("../fixtures/sas_fabric/core_direct_paths.json");

const fixture = (id) => structuredClone(FIXTURES.find((item) => item.id === id));
const model = (item) => T.buildTopologyModel(item.fabric, { ...item.options, width: 1150, enclosureHref: (id) => `view?enclosure=${id}` });
const checksOf = (built, id) => built.checks.filter((entry) => entry.id === id || entry.id.startsWith(`${id}:`));
const textOf = (value) => JSON.stringify(value);
// Values built inside the page's sandbox, as plain data of this realm.
const plain = (value) => JSON.parse(JSON.stringify(value));

function fakeDom() {
  const handlers = new Map();
  const documentHandlers = [];
  const pinText = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pinText : null), querySelectorAll: () => [] };
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === ".fabric-topology" ? root : null),
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  const search = { value: "", addEventListener: () => {} };
  const document = { addEventListener: (name, handler) => documentHandlers.push({ name, handler }) };
  const element = (dataset) => ({
    dataset,
    closest(selector) {
      if (selector === ".fabric-topology") return root;
      return /\[data-topo-/.test(selector) ? this : null;
    },
  });
  return { handlers, container, search, document, element };
}

test("a port never takes the owner of a transport index entry the resolver rejected", () => {
  for (const variant of ["owner only", "owner and state"]) {
    const item = fixture("cascade3-top");
    const trace = item.fabric.traces.find((entry) => entry.id === "bay:0");
    const own = trace.metrics.path_states[0];
    const entry = item.fabric.raw.sas_transport_port_index[own.sas_transport_port_id];
    const control = model(item);
    const controlPort = control.bayById.get("bay:0").ports[0];
    // Control: the entry as read names the bay's own expander and PHY.
    assert.match(textOf(T.cardFor(control, { kind: "port", id: controlPort.key }).rows), /Expander …a00ff \(5bbb0000000a00ff\) PHY 0/);
    const wrong = item.fabric.nodes.filter((node) => node.kind === "expander").at(-1);
    assert.notEqual(wrong.id, entry.owner_id);
    entry.owner_id = wrong.id;
    if (variant === "owner and state") entry.state = "not_read";
    const built = model(item);
    const bay = built.bayById.get("bay:0");
    const port = bay.ports[0];
    assert.equal(bay.pathStates[0].transportPhyCount, null, variant);
    assert.notEqual(port.expanderId, wrong.id, variant);
    const card = textOf(T.cardFor(built, { kind: "port", id: port.key }));
    assert.doesNotMatch(card, /Shelf C|5bbb0000000c00ff|Further down/, variant);
    assert.match(card, /not read/, variant);
    const path = T.tracePath(built, { kind: "port", id: port.key });
    assert.ok(![...path.nodes].some((id) => id === wrong.id || built.nodeById.get(id)?.mergedInto === wrong.id), variant);
  }
});

test("joins, traces and checks use the payload's controller id; a Friendly Label is display only", () => {
  const ports = (data) => {
    const built = T.buildTopologyModel(data.fabric, { inventory: data.snapshot });
    const all = [...built.portByKey.values()];
    return plain(built.bayById.get("bay:0").ports.map((port) => ({
      device: port.device, controller: port.controller, id: port.controllerId,
      traced: [...T.tracePath(built, { kind: "port", id: port.key }).nodes].filter((id) => id.startsWith("controller:")),
      // Tracing the port's own HBA reaches this port, and no port of the other HBA.
      fromHba: [...T.tracePath(built, { kind: "node", id: port.controllerId }).ports].map((key) => all.find((other) => other.key === key)?.device).sort(),
    })));
  };
  const own = { da3: "controller:mpr0", da11: "controller:mpr1" };
  const control = ports(structuredClone(DIRECT));
  const data = structuredClone(DIRECT);
  for (const node of data.fabric.nodes.filter((entry) => entry.kind === "controller")) node.alias = "IO";
  const aliased = ports(data);
  for (const rows of [control, aliased]) {
    assert.deepEqual(rows.map((row) => row.device).sort(), ["da11", "da3"]);
    for (const row of rows) {
      assert.equal(row.id, own[row.device], row.device);
      assert.deepEqual(row.traced, [own[row.device]], row.device);
      assert.deepEqual(row.fromHba, [row.device], row.device);
    }
  }
  // The label is still what the page shows.
  assert.deepEqual(aliased.map((row) => row.controller), ["IO", "IO"]);
  // A label equal to another controller's payload name is still only a label.
  const crossed = structuredClone(DIRECT);
  crossed.fabric.nodes.find((entry) => entry.id === "controller:mpr0").alias = "mpr1";
  for (const row of ports(crossed)) assert.equal(row.id, own[row.device], `crossed: ${row.device}`);
});

test("a pinned path stays the same path when its rows are reordered", () => {
  const dom = fakeDom();
  const api = loadTopology({ document: dom.document });
  const { fabric, snapshot } = structuredClone(DIRECT);
  api.attach(dom.container, dom.search, { textContent: "" });
  const options = { inventory: snapshot, selection: null };
  api.render(fabric, options);
  dom.handlers.get("click")({ target: dom.element({ topoPort: "bay:0#0" }) });
  const identify = () => {
    const html = api.inspectorDetail()?.html || "";
    return plain([/bay \d+ path via ([^<]+)/i.exec(html)?.[1], html.match(/HBA device<\/span><span class="kv-value">([^<]+)/)?.[1]]);
  };
  assert.deepEqual(identify(), ["mpr0 (da3)", "da3"]);
  fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states.reverse();
  api.render(fabric, options);
  // Still pinned, and still the same path; never its sibling.
  assert.ok(api.view().pin);
  assert.deepEqual(identify(), ["mpr0 (da3)", "da3"]);
  // A pin whose path is gone is cleared, not moved to another path.
  fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states = fabric.traces.find((entry) => entry.id === "bay:0").metrics.path_states.filter((row) => row.device_name !== "da3");
  api.render(fabric, options);
  assert.ok(!api.view().pin || !/da11/.test(identify().join(" ")));
});

test("lane rates are judged per lane only where the pairing is read, and never pass with an unread far end", () => {
  // Every pairing of these PHYs runs at its common maximum.
  const item = fixture("wide-x4-sata");
  let built = model(item);
  const cable = built.links.find((link) => link.lanes?.count === 4);
  const raw = item.fabric.links.find((link) => link.id === cable.id).reported_lanes;
  raw.negotiated_rate_gbps = 6;
  for (const [id, phys, max] of [[cable.source, cable.lanes.sourcePhys, 12], [cable.target, cable.lanes.targetPhys, 6]]) {
    const node = item.fabric.nodes.find((entry) => entry.id === id);
    for (const row of node.raw.phys || node.raw.phy_rows) {
      if (phys.includes(row.phy)) Object.assign(row, { speed: "6.0 Gbit", negotiated_link_rate: "6.0 Gbit", negotiated_rate_gbps: 6, maximum_rate_gbps: max, maximum_link_rate: `${max}.0 Gbit` });
    }
  }
  built = model(item);
  let lanes = checksOf(built, "lanes");
  assert.ok(!lanes.some((entry) => entry.status === "critical"), textOf(lanes));
  assert.doesNotMatch(textOf(lanes), /below its maximum/);
  assert.match(textOf(lanes.filter((entry) => entry.status === "not_read")), /which PHY pairs with which/);
  // Control: with the pairing read, a lane below both ends' maximum is critical.
  raw.lanes = cable.lanes.sourcePhys.map((phy, index) => ({ source_phy: phy, target_phy: cable.lanes.targetPhys[index], negotiated_rate_gbps: 6 }));
  for (const row of item.fabric.nodes.find((entry) => entry.id === cable.target).raw.phys) {
    if (cable.lanes.targetPhys.includes(row.phy)) Object.assign(row, { maximum_rate_gbps: 12, maximum_link_rate: "12.0 Gbit" });
  }
  lanes = checksOf(model(item), "lanes");
  assert.ok(lanes.some((entry) => entry.status === "critical"), textOf(lanes));
  // Without the port census the far ends are not read: no two-ended pass.
  const plain = fixture("wide-x4-sata");
  delete plain.fabric.raw.sas_transport_ports;
  const unread = model(plain);
  assert.match(textOf(checksOf(unread, "lanes")), /far end not read/);
  for (const id of ["lanes", "cable-errors"]) {
    const found = checksOf(unread, id);
    assert.ok(!found.some((entry) => entry.status === "pass"), `${id}: ${textOf(found)}`);
    // The check's title is its question; no line or note claims it.
    assert.doesNotMatch(textOf(found.map((entry) => [entry.lines, entry.notes])), /maximum of both its ends|checked at both of its ends/, id);
  }
  // Control: with the census both checks still pass on that fixture.
  const read = model(fixture("wide-x4-sata"));
  assert.equal(checksOf(read, "cable-errors")[0].status, "pass");
});

// A 12 Gb/s HBA cabled to a 6 Gb/s expander runs at 6: the most both ends
// support. Not critical, paired or not; unpaired, the comparison is not read.
test("an HBA faster than its expander, at the expander's maximum, is not a slow lane", () => {
  const slowExpander = (item, paired) => {
    const cable = model(item).links.find((link) => link.kind === "controller-expander" && link.lanes);
    const raw = item.fabric.links.find((link) => link.id === cable.id).reported_lanes;
    raw.negotiated_rate_gbps = 6;
    if (paired) raw.lanes = cable.lanes.sourcePhys.map((phy, index) => ({ source_phy: phy, target_phy: cable.lanes.targetPhys[index], negotiated_rate_gbps: 6 }));
    for (const [id, phys, max] of [[cable.source, cable.lanes.sourcePhys, 12], [cable.target, cable.lanes.targetPhys, 6]]) {
      const node = item.fabric.nodes.find((entry) => entry.id === id);
      for (const row of node.raw.phys || node.raw.phy_rows) {
        if (phys.includes(row.phy)) Object.assign(row, { speed: "6.0 Gbit", negotiated_link_rate: "6.0 Gbit", negotiated_rate_gbps: 6, maximum_rate_gbps: max, maximum_link_rate: `${max}.0 Gbit` });
      }
    }
    return { cable, built: model(item) };
  };
  for (const paired of [false, true]) {
    const { cable, built } = slowExpander(fixture("cascade3-top"), paired);
    const lanes = checksOf(built, "lanes");
    assert.ok(!lanes.some((entry) => entry.status === "critical"), `${paired}: ${textOf(lanes)}`);
    assert.doesNotMatch(textOf(lanes.map((entry) => entry.lines)), /below/, String(paired));
    assert.ok(!T.cardFor(built, { kind: "link", id: cable.id }).rows.some((row) => row.warn === true || /below/i.test(row.label)), String(paired));
    if (!paired) assert.match(textOf(lanes), /which PHY pairs with which is not read/);
  }
  // Control: paired, a lane below both ends' maximum is still critical.
  const item = fixture("cascade3-top");
  const { cable } = slowExpander(item, true);
  for (const row of item.fabric.nodes.find((entry) => entry.id === cable.target).raw.phys) {
    if (cable.lanes.targetPhys.includes(row.phy)) Object.assign(row, { maximum_rate_gbps: 12, maximum_link_rate: "12.0 Gbit" });
  }
  assert.ok(checksOf(model(item), "lanes").some((entry) => entry.status === "critical"));
});

// Independent HBA pairs serve different shelves: each shelf is held only
// against the HBAs its own evidence names, never every HBA of the host.
test("each enclosure's redundancy is judged against the HBAs that report it", () => {
  const withIds = () => {
    const { fabric } = H.eightHbaSixteenShelves();
    for (const node of fabric.nodes.filter((entry) => entry.kind === "mpr-enclosure")) {
      const shelf = Number(/SHELF-(\d+)/.exec(node.label)[1]);
      node.raw = { ...node.raw, logical_id: `5000c5000000${(0xf00 + shelf).toString(16)}` };
    }
    return fabric;
  };
  const redundancy = (fabric) => checksOf(T.buildTopologyModel(fabric, { width: 1600 }), "redundancy");
  const intact = redundancy(withIds());
  assert.equal(intact.length, 1, textOf(intact));
  assert.equal(intact[0].status, "pass", textOf(intact));
  assert.doesNotMatch(textOf(intact), /mpr[2-7].*SHELF-0-|no expander via/);
  // Control: shelf 0 still reports a path via mpr1 for its bays, but no
  // expander via mpr1 is cabled: that is a finding.
  const cut = withIds();
  const lost = cut.nodes.find((entry) => entry.kind === "expander" && entry.controller_id === "controller:mpr1" && cut.links.some((link) => link.source === entry.id && /SHELF-0-B/.test(cut.nodes.find((item) => item.id === link.target)?.label || "")));
  cut.nodes = cut.nodes.filter((entry) => entry.id !== lost.id && !(entry.kind === "mpr-enclosure" && /SHELF-0-B/.test(entry.label)));
  cut.links = cut.links.filter((link) => link.source !== lost.id && link.target !== lost.id);
  const flagged = redundancy(cut);
  assert.match(textOf(flagged.filter((entry) => entry.status === "warning")), /SHELF-0-A: no expander via mpr1\b/);
  assert.doesNotMatch(textOf(flagged), /mpr[2-7]/);
  // A shelf only one HBA reports, in a host with more: whether another HBA
  // should reach it is not read, not a finding.
  const single = withIds();
  for (const bay of single.nodes.filter((entry) => entry.kind === "bay")) {
    const trace = single.traces.find((entry) => entry.id === bay.id);
    if (trace?.metrics?.path_states?.some((state) => state.controller === "mpr1")) trace.metrics.path_states = trace.metrics.path_states.filter((state) => state.controller !== "mpr1");
  }
  single.links = single.links.filter((link) => !(link.kind === "path-bay" && /path:mpr1:/.test(link.source)));
  const one = single.nodes.find((entry) => entry.kind === "expander" && entry.controller_id === "controller:mpr1" && single.links.some((link) => link.source === entry.id && /SHELF-0-B/.test(single.nodes.find((item) => item.id === link.target)?.label || "")));
  single.nodes = single.nodes.filter((entry) => entry.id !== one.id && !(entry.kind === "mpr-enclosure" && /SHELF-0-B/.test(entry.label)));
  single.links = single.links.filter((link) => link.source !== one.id && link.target !== one.id);
  const lone = redundancy(single);
  assert.doesNotMatch(textOf(lone.filter((entry) => entry.status === "warning").map((entry) => entry.lines)), /SHELF-0-A/);
  assert.match(textOf(lone.filter((entry) => entry.status === "not_read")), /SHELF-0-A/);
});

test("only established paths and rates are counted as read", () => {
  const ports = checksOf(model(fixture("dual-domain-linux")), "ports");
  assert.match(textOf(ports), /a path via another HBA is not read for 8 disks/);
  assert.doesNotMatch(textOf(ports), /8 disks with a path via each HBA/);
  const item = fixture("wide-x4-sata");
  const phys = item.fabric.nodes.filter((node) => ["expander", "controller"].includes(node.kind)).flatMap((node) => node.raw.phys || node.raw.phy_rows);
  const both = phys.filter((row) => row.negotiated_rate_gbps !== null && row.maximum_rate_gbps !== null).length;
  assert.ok(both < phys.length);
  const line = model(item).transportNote.measured[1];
  assert.doesNotMatch(line, new RegExp(`^${phys.length} PHYs with link rate and maximum rate`));
  assert.match(line, new RegExp(`\\b${both} of ${phys.length} PHYs\\b`));
});

test("a neighbour stub read in part says so, for its counters and its disks", () => {
  const item = fixture("fanout-2");
  const control = model(item).nodes.find((entry) => entry.neighbour);
  assert.equal(control.neighbour.health, "no PHY errors");
  assert.ok(control.facts.some((fact) => /\b4\/4 disks\b/.test(fact)));
  const rows = item.fabric.nodes.find((entry) => entry.id === control.id).raw.phys;
  const phy = rows.find((row) => row.negotiated_rate_gbps !== null);
  phy.counter_state = "not_read";
  for (const key of Object.keys(phy).filter((name) => name.endsWith("_count"))) phy[key] = null;
  let built = model(item);
  let stub = built.nodes.find((entry) => entry.id === control.id);
  assert.notEqual(stub.neighbour.health, "no PHY errors");
  assert.match(stub.neighbour.health, /not read/);
  assert.match(textOf(T.cardFor(built, { kind: "node", id: stub.id }).rows), /not read/);
  const disks = fixture("fanout-2");
  const port = disks.fabric.raw.sas_transport_ports.find((entry) => entry.owner_id === control.id && entry.remote_kind === "end_device" && entry.has_block);
  Object.assign(port, { remote_kind: "not_read", target_basis: "not_read", has_block: null, remote_id: null, remote_sas_address: null, enclosure_identifier: null, bay_identifier: null });
  built = model(disks);
  stub = built.nodes.find((entry) => entry.id === control.id);
  assert.ok(!stub.facts.some((fact) => /\b3\/4 disks\b/.test(fact)), textOf(stub.facts));
  assert.match(textOf(stub.facts), /1 attachment not read/);
});

test("bays follow the inventory grid inside each enclosure, or the page says they cannot", () => {
  const { fabric } = producerCases().core_cascade;
  const build = (rows) => T.buildTopologyModel(fabric, { inventory: { selected_system_id: fabric.system_id, selected_enclosure_id: fabric.selected_enclosure_id, layout_rows: rows } });
  // Rows within one enclosure, and rows that cross both: each enclosure's
  // cells keep the grid's rows, order and columns, without a notice.
  for (const rows of [[[3, 2, 1, 0], [7, 6, 5, 4]], [[3, 7, 2, 6], [1, 5, 0, 4]]]) {
    const built = build(rows);
    assert.deepEqual(plain(built.notices.filter((notice) => /layout rows/i.test(notice))), [], textOf(rows));
    const groups = built.groups.filter((entry) => entry.enclosure);
    assert.equal(groups.length, 2);
    for (const group of groups) {
      const mine = new Set(group.bays.map((bay) => bay.slot));
      const expected = rows.map((row) => row.map((slot, column) => [slot, column]).filter(([slot]) => mine.has(slot))).filter((row) => row.length);
      assert.ok(group.rows, `${textOf(rows)}: ${group.id} ignores the grid`);
      assert.deepEqual(plain(group.rows.map((row) => row.map((cell) => [cell.bay.slot, cell.column]))), expected, textOf(rows));
    }
  }
  // A grid without one enclosure's bays: that enclosure is drawn in bay order, and the page says so.
  const partial = build([[3, 2, 1, 0]]);
  assert.ok(partial.notices.some((notice) => /layout rows do not place every bay of/.test(notice)), textOf(partial.notices));
});

test("an enclosure bundle's inspector offers its observed label targets", () => {
  const dom = fakeDom();
  const api = loadTopology({ document: dom.document });
  const { fabric } = producerCases().core_cascade;
  api.attach(dom.container, dom.search, { textContent: "" });
  api.render(fabric, {});
  const bundle = api.drawnModel().links.find((link) => link.bundle);
  dom.handlers.get("click")({ target: dom.element({ topoLink: bundle.id }) });
  const detail = api.inspectorDetail();
  const body = { innerHTML: "" };
  const nodes = new Map(fabric.nodes.map((node) => [node.id, node]));
  api.composeInspector(detail, { nodes, renderAliasRow: (label, target) => `<edit ${target.objectId}/>`, title: {}, body });
  assert.ok(nodes.has(bundle.source));
  assert.doesNotMatch(body.innerHTML, /No observed label target/);
  assert.match(body.innerHTML, new RegExp(`<edit ${bundle.source}/>`));
});
