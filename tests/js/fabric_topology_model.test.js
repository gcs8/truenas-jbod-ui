"use strict";

// Storage Fabric "Topology" mode (#937): the layout model draws the SAS fabric
// from the host down to the bays using only the /api/sas-fabric payload. It
// never invents a node, a link, a lane count, a PHY number or a rate; a tier
// the payload does not carry says "not read" or "not collected"; and a large
// shelf collapses into a summary block instead of dropping bays. Every
// identifier below is synthetic.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const TOPOLOGY_PATH = path.join(ROOT, "app/static/sas_fabric_topology.js");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const NOT_COLLECTED = "HBA and expander hops are not collected on this platform.";

function loadTopology() {
  assert.ok(fs.existsSync(TOPOLOGY_PATH), "app/static/sas_fabric_topology.js must exist");
  const sandbox = vm.createContext({ window: {} });
  vm.runInContext(fs.readFileSync(TOPOLOGY_PATH, "utf8"), sandbox);
  const api = sandbox.window.SasFabricTopology;
  assert.ok(api, "the file must publish window.SasFabricTopology");
  return api;
}

// ---- synthetic payload builders -------------------------------------------

function node(id, kind, extra = {}) {
  return { id, kind, label: id, related_slots: [], metrics: {}, raw: {}, evidence: [], ...extra };
}

function link(source, target, kind, extra = {}) {
  const suffix = extra.slot === undefined || extra.slot === null ? "" : `:${extra.slot}`;
  return { id: `${kind}:${source}->${target}${suffix}`, source, target, kind, related_slots: [], evidence: [], reported_lanes: null, ...extra };
}

function lanes(sourcePhys, targetPhys, rate = 12) {
  return {
    basis: "both_endpoints",
    lane_count: sourcePhys.length,
    source_phys: sourcePhys,
    target_phys: targetPhys,
    lanes: sourcePhys.map((phy, index) => ({ source_phy: phy, target_phy: targetPhys[index], negotiated_rate_gbps: rate })),
    negotiated_rate_gbps: rate,
    evidence: [],
  };
}

function controller(name) {
  return node(`controller:${name}`, "controller", { label: name, raw: { board: "SYNTH-HBA" }, metrics: { phy_count: 8, linked_phy_count: 4 } });
}

function expander(controllerName, address, level, extra = {}) {
  return node(`expander:controller:${controllerName}:${address}`, "expander", {
    label: address,
    raw_id: address,
    controller_id: `controller:${controllerName}`,
    metrics: { sas_level: level, num_phys: 36, linked_phys: 12 },
    raw: { parent_link_state: "resolved", sas_level: level },
    ...extra,
  });
}

function enclosure(controllerName, handle) {
  return node(`mpr-enclosure:controller:${controllerName}:${handle}`, "mpr-enclosure", {
    label: `SYNTH-ENC-${handle}`,
    controller_id: `controller:${controllerName}`,
  });
}

// Bays hang off one or more enclosures; `paths` gives each bay's path states.
function addBays(fabric, slots, enclosureIds, paths = []) {
  for (const slot of slots) {
    const bayId = `bay:${slot}`;
    fabric.nodes.push(node(bayId, "bay", {
      label: `Bay ${String(slot).padStart(2, "0")}`,
      slot,
      status: "healthy",
      related_slots: [slot],
      metrics: { present: true, device_name: `da${slot}`, serial: `SYN-SER-${slot}` },
    }));
    for (const enclosureId of enclosureIds) {
      const kind = enclosureId.startsWith("ses:") ? "ses-bay" : "mpr-enclosure-bay";
      fabric.links.push(link(enclosureId, bayId, kind, { slot, related_slots: [slot] }));
    }
    for (const { controller: name, state } of paths) {
      fabric.links.push(link(`path:${name}:${state}`, bayId, "path-bay", { slot, status: state, related_slots: [slot] }));
    }
    fabric.traces.push({
      id: bayId,
      kind: "bay",
      label: `Bay ${slot}`,
      node_ids: ["host", bayId],
      link_ids: [],
      slots: [slot],
      metrics: { device_name: `da${slot}`, path_states: paths.map(({ controller: name, state }) => ({ controller: name, state, device_name: `da${slot}` })) },
      evidence: [],
    });
  }
}

function coreFabric() {
  return {
    available: true,
    platform: "core",
    snapshot_cache_state: "hit",
    source_cache_state: "hit",
    nodes: [node("host", "host", { label: "synthetic-host" })],
    links: [],
    traces: [],
    controllers: [],
    expanders: [],
    enclosures: [],
    paths: [],
    warnings: [],
    sources: { ssh: { enabled: true, ok: true } },
    raw: {
      controller_read: {
        counts: { controllers: "complete", paths: "complete", expanders: "complete", enclosures: "complete" },
        unread_phy_tables: 0,
      },
    },
  };
}

function addController(fabric, name) {
  const item = controller(name);
  fabric.nodes.push(item);
  fabric.links.push(link("host", item.id, "host-controller"));
  return item;
}

function oneHbaOneExpander({ reported = null, bayCount = 4 } = {}) {
  const fabric = coreFabric();
  const hba = addController(fabric, "mpr0");
  const exp = expander("mpr0", "5000aa0000000001", 1);
  const enc = enclosure("mpr0", "0x0001");
  fabric.nodes.push(exp, enc);
  fabric.links.push(link(hba.id, exp.id, "controller-expander", { reported_lanes: reported }));
  fabric.links.push(link(exp.id, enc.id, "expander-enclosure"));
  // Logical multipath grouping nodes the CORE payload also carries.
  fabric.nodes.push(node("path:mpr0:active", "path", { controller_id: hba.id, status: "active" }));
  fabric.links.push(link(hba.id, "path:mpr0:active", "controller-path", { status: "active" }));
  fabric.links.push(link("path:mpr0:active", exp.id, "path-expander", { status: "active" }));
  addBays(fabric, Array.from({ length: bayCount }, (_, index) => index), [enc.id], [{ controller: "mpr0", state: "active" }]);
  return { fabric, hba, exp, enc };
}

// Two HBAs, every bay dual-pathed, and a level-2 expander behind each level-1
// expander. Front bays 0-3 sit behind the level-1 expanders, rear bays 4-7
// behind the level-2 expanders.
function dualPathCascade() {
  const fabric = coreFabric();
  const ids = {};
  for (const [name, prefix] of [["mpr0", "a"], ["mpr1", "b"]]) {
    const hba = addController(fabric, name);
    const level1 = expander(name, `5000${prefix}00000000010`, 1);
    const level2 = expander(name, `5000${prefix}00000000020`, 2);
    const front = enclosure(name, "0x0001");
    const rear = enclosure(name, "0x0002");
    fabric.nodes.push(level1, level2, front, rear);
    fabric.links.push(link(hba.id, level1.id, "controller-expander", { reported_lanes: lanes([0, 1, 2, 3], [32, 33, 34, 35]) }));
    fabric.links.push(link(level1.id, level2.id, "expander-expander", { reported_lanes: lanes([28, 29, 30, 31], [0, 1, 2, 3]) }));
    fabric.links.push(link(level1.id, front.id, "expander-enclosure"));
    fabric.links.push(link(level2.id, rear.id, "expander-enclosure"));
    ids[name] = { hba, level1, level2, front, rear };
  }
  const paths = [{ controller: "mpr0", state: "active" }, { controller: "mpr1", state: "passive" }];
  addBays(fabric, [0, 1, 2, 3], [ids.mpr0.front.id, ids.mpr1.front.id], paths);
  addBays(fabric, [4, 5, 6, 7], [ids.mpr0.rear.id, ids.mpr1.rear.id], paths);
  return { fabric, ids };
}

function linuxSesFabric() {
  const fabric = {
    available: true,
    platform: "scale",
    snapshot_cache_state: "hit",
    source_cache_state: "hit",
    nodes: [
      node("host", "host", { label: "synthetic-linux" }),
      node("controller:linux-ses", "controller", { label: "Linux SES", raw: { fabric_kind: "linux_ses" } }),
      node("path:linux-ses:sg9", "path", { label: "/dev/sg9 mapped", status: "mapped", controller_id: "controller:linux-ses" }),
      node("ses:sg9", "ses-enclosure", { label: "SYNTH-SHELF", controller_id: "controller:linux-ses" }),
    ],
    links: [
      link("host", "controller:linux-ses", "host-controller"),
      link("controller:linux-ses", "path:linux-ses:sg9", "controller-path", { status: "mapped" }),
      link("path:linux-ses:sg9", "ses:sg9", "path-ses-enclosure", { status: "mapped" }),
    ],
    traces: [],
    controllers: [],
    expanders: [],
    enclosures: [],
    paths: [],
    warnings: [],
    sources: {},
    raw: { fabric_domain: "storage_fabric", fabric_kind: "linux_ses" },
  };
  addBays(fabric, [1, 2, 3], ["ses:sg9"], [{ controller: "linux-ses", state: "mapped" }]);
  // Linux SES bays have no path-bay link; drop the ones addBays made.
  fabric.links = fabric.links.filter((item) => item.kind !== "path-bay");
  return fabric;
}

const drawnLinks = (model) => [...model.links].filter((item) => !item.bundle);
const bundles = (model) => [...model.links].filter((item) => item.bundle);
const ids = (items) => [...items].map((item) => item.id).sort();
const sorted = (values) => [...values].sort();

// ---- tests -----------------------------------------------------------------

test("one HBA and one expander: host, HBA, expander, enclosure and bays, one line per payload link", () => {
  const T = loadTopology();
  const { fabric, hba, exp, enc } = oneHbaOneExpander();
  const model = T.buildTopologyModel(fabric);
  assert.deepEqual([...model.tiers].map((tier) => tier.id), ["host", "controllers", "expanders:1", "enclosures", "bays"]);
  assert.deepEqual(ids(model.nodes), sorted(["host", hba.id, exp.id, enc.id]));
  assert.deepEqual(ids(drawnLinks(model)), sorted([
    `host-controller:host->${hba.id}`,
    `controller-expander:${hba.id}->${exp.id}`,
    `expander-enclosure:${exp.id}->${enc.id}`,
  ]));
  // The logical multipath grouping is not a physical hop and is not drawn.
  assert.ok(!model.nodeById.has("path:mpr0:active"));
  const payloadLinkIds = new Set(fabric.links.map((item) => item.id));
  for (const item of drawnLinks(model)) assert.ok(payloadLinkIds.has(item.id), `${item.id} must come from the payload`);
  const [bundle] = bundles(model);
  assert.equal(bundle.source, enc.id);
  assert.equal(bundle.count, 4);
  for (const memberId of bundle.memberLinkIds) assert.ok(payloadLinkIds.has(memberId));
  assert.equal(model.counts.bayNodes, 4);
  assert.equal(model.counts.baysPlaced, 4);
  const hbaLink = model.linkById.get(`controller-expander:${hba.id}->${exp.id}`);
  assert.equal(hbaLink.lanesState, "not_reported");
  assert.deepEqual([...hbaLink.labelLines], ["lanes not reported"]);
});

test("two HBAs, dual path, level-2 cascade: the level-2 expander hangs off its level-1 parent", () => {
  const T = loadTopology();
  const { fabric, ids: parts } = dualPathCascade();
  const model = T.buildTopologyModel(fabric);
  assert.deepEqual([...model.tiers].map((tier) => tier.id), ["host", "controllers", "expanders:1", "expanders:2", "enclosures", "bays"]);
  for (const name of ["mpr0", "mpr1"]) {
    const { hba, level1, level2 } = parts[name];
    const into = drawnLinks(model).filter((item) => item.target === level2.id);
    assert.equal(into.length, 1, `${name} level-2 expander has exactly one parent line`);
    assert.equal(into[0].source, level1.id);
    assert.ok(!drawnLinks(model).some((item) => item.source === hba.id && item.target === level2.id));
    assert.ok(model.nodeById.get(level2.id).y > model.nodeById.get(level1.id).y, "level 2 is drawn below level 1");
  }
  // Each dual-pathed bay keeps both path states, not a merged one.
  const group = model.groupByBay.get("bay:5");
  assert.deepEqual(sorted(group.enclosureIds), sorted([parts.mpr0.rear.id, parts.mpr1.rear.id]));
  const cell = [...group.cells].find((item) => item.bayId === "bay:5");
  assert.deepEqual([...cell.pathStates].map((item) => `${item.controller}:${item.state}`), ["mpr0:active", "mpr1:passive"]);
  assert.equal(model.counts.baysPlaced, 8);
});

test("a link with reported lanes is labelled with lane count, PHYs at both ends and rate; a null one is drawn as not reported", () => {
  const T = loadTopology();
  const reported = oneHbaOneExpander({ reported: lanes([0, 1, 2, 3], [32, 33, 34, 35]) });
  const model = T.buildTopologyModel(reported.fabric);
  const item = model.linkById.get(`controller-expander:${reported.hba.id}->${reported.exp.id}`);
  assert.equal(item.lanesState, "reported");
  assert.deepEqual({ ...item.lanes, sourcePhys: [...item.lanes.sourcePhys], targetPhys: [...item.lanes.targetPhys] },
    { count: 4, sourcePhys: [0, 1, 2, 3], targetPhys: [32, 33, 34, 35], rate: 12 });
  assert.deepEqual([...item.labelLines], ["x4 · 12 Gb/s", "PHY 0–3 ↔ 32–35"]);
  // The HBA groups its PHYs by the wide port the payload reports.
  const hba = model.nodeById.get(reported.hba.id);
  assert.deepEqual([...hba.phyGroups].map((group) => `${[...group.phys].join(",")}>${group.target}`), [`0,1,2,3>${reported.exp.id}`]);

  // A reported link without a common rate never gets one invented.
  const noRate = lanes([4, 5], [8, 9], null);
  const rateless = oneHbaOneExpander({ reported: noRate });
  const rateModel = T.buildTopologyModel(rateless.fabric);
  const rateLink = rateModel.linkById.get(`controller-expander:${rateless.hba.id}->${rateless.exp.id}`);
  assert.deepEqual([...rateLink.labelLines], ["x2 · rate not reported", "PHY 4–5 ↔ 8–9"]);
  assert.equal(rateLink.lanes.rate, null);

  const plain = oneHbaOneExpander();
  const plainModel = T.buildTopologyModel(plain.fabric);
  const plainLink = plainModel.linkById.get(`controller-expander:${plain.hba.id}->${plain.exp.id}`);
  assert.equal(plainLink.lanes, null);
  assert.equal(plainLink.lanesState, "not_reported");
  assert.deepEqual([...plainModel.nodeById.get(plain.hba.id).phyGroups], []);
  const html = T.renderTopology(plainModel, {});
  assert.match(html, /is-lanes-unreported/);
  assert.match(html, /lanes not reported/);
});

test("Linux SES payload: draws what it has, states the hops are not collected, invents nothing", () => {
  const T = loadTopology();
  const fabric = linuxSesFabric();
  const model = T.buildTopologyModel(fabric);
  const payloadNodeIds = new Set(fabric.nodes.map((item) => item.id));
  for (const item of model.nodes) assert.ok(payloadNodeIds.has(item.id), `${item.id} must come from the payload`);
  assert.ok(![...model.nodes].some((item) => item.kind === "expander"));
  const payloadLinkIds = new Set(fabric.links.map((item) => item.id));
  for (const item of drawnLinks(model)) assert.ok(payloadLinkIds.has(item.id), `${item.id} must come from the payload`);
  for (const item of bundles(model)) for (const memberId of item.memberLinkIds) assert.ok(payloadLinkIds.has(memberId));
  const missing = [...model.tiers].find((tier) => tier.id === "not-collected");
  assert.ok(missing, "a not-collected tier is drawn");
  assert.equal(missing.state, "not_collected");
  assert.equal(missing.note, NOT_COLLECTED);
  assert.equal([...model.tiers].find((tier) => tier.id === "controllers").label, "Source");
  assert.equal(model.counts.baysPlaced, 3);
  assert.ok(T.renderTopology(model, {}).includes(NOT_COLLECTED));
  // A platform the view does not know gets the same statement.
  const other = linuxSesFabric();
  other.raw = { fabric_domain: "storage_fabric", fabric_kind: "storage_example" };
  const otherModel = T.buildTopologyModel(other);
  assert.equal([...otherModel.tiers].find((tier) => tier.id === "not-collected").note, NOT_COLLECTED);
});

test("a 1024-bay expander keeps every bay: a summary block that expands, and the page says so", () => {
  const T = loadTopology();
  const { fabric, enc } = oneHbaOneExpander({ bayCount: 1024 });
  const model = T.buildTopologyModel(fabric);
  assert.equal(model.counts.bayNodes, 1024);
  assert.equal(model.counts.baysPlaced, 1024);
  const [group] = model.groups;
  assert.equal(group.collapsed, true);
  assert.equal(group.bayIds.length, 1024);
  assert.equal(group.summary.bays, 1024);
  assert.equal(group.summary.withDisk, 1024);
  assert.deepEqual([...group.enclosureIds], [enc.id]);
  assert.ok([...model.notices].some((text) => text.includes("1024 bays") && text.includes("summary")), "the collapse is announced");
  const html = T.renderTopology(model, {});
  assert.match(html, /data-fabric-expand-slots="topology:/);
  assert.match(html, /1024 bays/);

  const expanded = T.buildTopologyModel(fabric, { expanded: { [group.expandKey]: true } });
  assert.equal(expanded.groups[0].collapsed, false);
  assert.equal(expanded.groups[0].cells.length, 1024);
  assert.equal(new Set([...expanded.groups[0].cells].map((cell) => cell.bayId)).size, 1024);
});

test("a stale or unavailable part is marked: not read is never an empty healthy tier", () => {
  const T = loadTopology();
  const { fabric } = oneHbaOneExpander();
  // Expanders and enclosures were not read: no expander nodes, no enclosure nodes.
  fabric.nodes = fabric.nodes.filter((item) => !["expander", "mpr-enclosure"].includes(item.kind));
  fabric.links = fabric.links.filter((item) => !["controller-expander", "expander-enclosure", "mpr-enclosure-bay", "path-expander"].includes(item.kind));
  fabric.raw.controller_read.counts.expanders = "not_read";
  fabric.raw.controller_read.counts.enclosures = "not_read";
  fabric.snapshot_cache_state = "stale-hit";
  fabric.sources = { ssh: { enabled: true, ok: false, message: "synthetic failure" }, api: { enabled: false, ok: false } };
  const model = T.buildTopologyModel(fabric);
  const tier = (id) => [...model.tiers].find((item) => item.id === id);
  assert.equal(tier("expanders").state, "not_read");
  assert.match(tier("expanders").note, /not read/);
  assert.equal(tier("enclosures").state, "not_read");
  // Bays the inventory knows are still drawn, in a group that says it has no enclosure link.
  assert.equal(model.counts.baysPlaced, 4);
  assert.deepEqual([...model.groups[0].enclosureIds], []);
  assert.match(T.renderTopology(model, {}), /No enclosure link · 4 bays/);
  const parts = Object.fromEntries([...model.parts].map((part) => [part.name, part.state]));
  assert.equal(parts.cache, "stale");
  assert.equal(parts.ssh, "failed");
  assert.equal(parts.api, "disabled");
  assert.equal(parts.expanders, "not read");

  // Partial reads are a lower bound.
  const partial = oneHbaOneExpander();
  partial.fabric.raw.controller_read.counts.expanders = "partial";
  assert.equal([...T.buildTopologyModel(partial.fabric).tiers].find((item) => item.id === "expanders:1").state, "partial");

  // Control: a complete read with no expanders is a measured zero, not "not read".
  const direct = oneHbaOneExpander();
  direct.fabric.nodes = direct.fabric.nodes.filter((item) => item.kind !== "expander");
  direct.fabric.links = direct.fabric.links.filter((item) => !item.kind.includes("expander"));
  assert.equal([...T.buildTopologyModel(direct.fabric).tiers].find((item) => item.id === "expanders").state, "none");

  // No controllers and no controller read: the HBA tier is not read.
  const bare = oneHbaOneExpander();
  bare.fabric.nodes = bare.fabric.nodes.filter((item) => !["controller", "expander", "mpr-enclosure", "path"].includes(item.kind));
  bare.fabric.links = bare.fabric.links.filter((item) => item.kind === "path-bay");
  bare.fabric.raw.controller_read.counts = { controllers: "not_read", paths: "not_read", expanders: "not_read", enclosures: "not_read" };
  assert.equal([...T.buildTopologyModel(bare.fabric).tiers].find((item) => item.id === "controllers").state, "not_read");
});

test("an expander whose parent link the payload does not carry gets no invented line", () => {
  const T = loadTopology();
  const { fabric, exp } = oneHbaOneExpander();
  fabric.links = fabric.links.filter((item) => item.kind !== "controller-expander");
  fabric.nodes.find((item) => item.id === exp.id).raw.parent_link_state = "unknown";
  const model = T.buildTopologyModel(fabric);
  assert.ok(!drawnLinks(model).some((item) => item.target === exp.id));
  assert.equal(model.nodeById.get(exp.id).parentUnknown, true);
  assert.match(T.renderTopology(model, {}), /parent link not reported/);
});

test("path trace from a bay goes up to the host through both paths and no further", () => {
  const T = loadTopology();
  const { fabric, ids: parts } = dualPathCascade();
  const model = T.buildTopologyModel(fabric);
  const trace = T.tracePath(model, { kind: "bay", id: "bay:5" });
  const expected = ["host", ...["mpr0", "mpr1"].flatMap((name) => [parts[name].hba.id, parts[name].level1.id, parts[name].level2.id, parts[name].rear.id])];
  assert.deepEqual(sorted(trace.nodes), sorted(expected));
  assert.deepEqual(sorted(trace.bays), ["bay:5"]);
  assert.ok(!trace.nodes.has(parts.mpr0.front.id));
  assert.ok(trace.links.has(`expander-expander:${parts.mpr1.level1.id}->${parts.mpr1.level2.id}`));
  assert.ok(!trace.links.has(`expander-enclosure:${parts.mpr0.level1.id}->${parts.mpr0.front.id}`));
});

test("path trace from a link goes down to the bays behind it and up to the host", () => {
  const T = loadTopology();
  const { fabric, ids: parts } = dualPathCascade();
  const model = T.buildTopologyModel(fabric);
  const top = T.tracePath(model, { kind: "link", id: `controller-expander:${parts.mpr0.hba.id}->${parts.mpr0.level1.id}` });
  assert.deepEqual(sorted(top.bays), ["bay:0", "bay:1", "bay:2", "bay:3", "bay:4", "bay:5", "bay:6", "bay:7"]);
  assert.ok(top.nodes.has("host") && top.nodes.has(parts.mpr0.hba.id) && top.nodes.has(parts.mpr0.rear.id));
  assert.ok(!top.nodes.has(parts.mpr1.hba.id) && !top.nodes.has(parts.mpr1.level1.id));

  const cascade = T.tracePath(model, { kind: "link", id: `expander-expander:${parts.mpr0.level1.id}->${parts.mpr0.level2.id}` });
  assert.deepEqual(sorted(cascade.bays), ["bay:4", "bay:5", "bay:6", "bay:7"]);
  assert.ok(cascade.nodes.has(parts.mpr0.level1.id) && cascade.nodes.has(parts.mpr0.hba.id) && cascade.nodes.has("host"));
  assert.ok(!cascade.nodes.has(parts.mpr0.front.id));
});

test("search finds a bay, a disk identifier or an expander and returns its path", () => {
  const T = loadTopology();
  const { fabric, ids: parts } = dualPathCascade();
  const model = T.buildTopologyModel(fabric);
  assert.deepEqual([...T.searchTopology(model, "bay 5").matches].map((item) => item.id), ["bay:5"]);
  assert.deepEqual([...T.searchTopology(model, "5").matches].map((item) => item.id), ["bay:5"], "a number is a bay number, not a substring");
  assert.deepEqual([...T.searchTopology(model, "SYN-SER-6").matches].map((item) => item.id), ["bay:6"]);
  assert.deepEqual([...T.searchTopology(model, "da7").matches].map((item) => item.id), ["bay:7"]);
  const found = T.searchTopology(model, "5000b00000000020");
  assert.deepEqual([...found.matches].map((item) => item.id), [parts.mpr1.level2.id]);
  assert.ok(found.path.nodes.has(parts.mpr1.hba.id));
  assert.equal(T.searchTopology(model, "no-such-thing").matches.length, 0);
  // A match inside a collapsed shelf says so.
  const big = oneHbaOneExpander({ bayCount: 300 });
  const hit = T.searchTopology(T.buildTopologyModel(big.fabric), "Bay 250").matches[0];
  assert.equal(hit.id, "bay:250");
  assert.equal(hit.collapsed, true);
});

test("the renderer escapes payload text, names every control and explains every line style it uses", () => {
  const T = loadTopology();
  const { fabric, ids: parts } = dualPathCascade();
  fabric.nodes.find((item) => item.id === parts.mpr0.hba.id).label = "<img src=x onerror=alert(1)>";
  fabric.links.find((item) => item.kind === "expander-expander").reported_lanes = null;
  const model = T.buildTopologyModel(fabric);
  const html = T.renderTopology(model, { pin: { kind: "bay", id: "bay:5" } });
  assert.ok(!html.includes("<img"), "payload text is escaped");
  assert.match(html, /<svg[^>]*role="group"/);
  assert.match(html, /data-fabric-node="host"/);
  assert.match(html, /data-fabric-trace="bay:5"/);
  assert.match(html, /data-fabric-topology-link="controller-expander:/);
  for (const role of html.match(/<g [^>]*role="button"[^>]*>/g)) assert.match(role, /aria-label="[^"]+"/);
  for (const style of ["is-lanes-reported", "is-lanes-unreported", "is-bundle"]) {
    assert.ok(html.includes(style), `${style} is drawn`);
    assert.ok(html.includes(`data-topology-legend="${style}"`), `${style} is in the legend`);
  }
  assert.match(html, /is-on-path/);
  assert.match(html, /Pools and vdevs are not drawn here/);
});

test("focusable boxes never overlap, so every node, link and bay stays reachable", () => {
  const T = loadTopology();
  const big = oneHbaOneExpander({ bayCount: 300 }).fabric;
  const collapsed = T.buildTopologyModel(big);
  const models = {
    dual: T.buildTopologyModel(dualPathCascade().fabric),
    linux: T.buildTopologyModel(linuxSesFabric()),
    expanded: T.buildTopologyModel(big, { expanded: { [collapsed.groups[0].expandKey]: true } }),
  };
  for (const [name, model] of Object.entries(models)) {
    const boxes = [
      ...[...model.nodes].map((item) => ({ id: item.id, x: item.x, y: item.y, w: item.w, h: item.h })),
      ...[...model.links].filter((item) => item.labelBox).map((item) => ({ id: item.id, ...item.labelBox })),
      ...[...model.groups].flatMap((group) => [...group.cells].map((cell) => ({ id: cell.bayId, x: cell.x, y: cell.y, w: 30, h: 30 }))),
    ];
    assert.ok(boxes.length > 5);
    for (let left = 0; left < boxes.length; left += 1) {
      for (let right = left + 1; right < boxes.length; right += 1) {
        const a = boxes[left];
        const b = boxes[right];
        const width = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
        const height = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
        assert.ok(width <= 0 || height <= 0, `${name}: ${a.id} overlaps ${b.id}`);
      }
    }
    for (const box of boxes) assert.ok(box.x >= 0 && box.x + box.w <= model.width && box.y >= 0 && box.y + box.h <= model.height, `${name}: ${box.id} is inside the drawing`);
  }
});

test("the Storage Fabric page registers Topology as a fifth mode and hands it the payload", () => {
  const T = loadTopology();
  const { fabric } = dualPathCascade();
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "?mode=topology" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots: [] }, fabric }, SasFabricTopology: T },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", dataset: {}, className: "", hidden: true, closest: () => null });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    window.renderTest = { renderMap, renderModeChrome, state };
  })();`, sandbox);
  const api = sandbox.window.renderTest;
  assert.equal(api.state.mode, "topology");
  api.state.fabric = fabric;
  api.renderModeChrome();
  api.renderMap();
  assert.equal(elements.get("fabric-map-title").textContent, "Topology");
  const map = elements.get("fabric-map-panel").innerHTML;
  assert.match(map, /class="fabric-topology"/);
  assert.match(map, /data-fabric-topology-link=/);
  assert.equal(elements.get("fabric-topology-search-row").hidden, false);
  // The other four modes keep their own rendering.
  api.state.mode = "lanes";
  api.renderModeChrome();
  api.renderMap();
  assert.doesNotMatch(elements.get("fabric-map-panel").innerHTML, /class="fabric-topology"/);
  assert.equal(elements.get("fabric-topology-search-row").hidden, true);
});
