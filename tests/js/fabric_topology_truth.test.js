"use strict";

// Storage Fabric Topology mode (#937): what the drawing says must be what the
// producer reported. These tests draw payloads the real Python builder made
// (tests/fixtures/sas_fabric/topology_fabric.json, rebuilt and compared by
// tests/test_fabric_topology_fixture.py) and check the rendered SVG, not only
// the layout model. Every identifier is synthetic.

const assert = require("node:assert/strict");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const cases = H.producerCases();

function cellsOf(html) {
  // One entry per rendered bay cell: its id, its text and its path marks.
  const cells = new Map();
  for (const match of html.matchAll(/<g class="topo-bay[^"]*"[^>]*data-topo-bay="([^"]+)"[^>]*aria-label="([^"]*)"[^>]*>([\s\S]*?)<\/g>/g)) {
    const body = match[3];
    cells.set(match[1], {
      name: match[2],
      marks: [...body.matchAll(/data-path-state="([^"]*)"/g)].map((item) => item[1]),
      tones: [...body.matchAll(/class="topo-path-mark path-([a-z]+)"/g)].map((item) => item[1]),
      text: [...body.matchAll(/<text class="topo-bay-number"[^>]*>([^<]*)<\/text>/g)].map((item) => item[1]),
      more: [...body.matchAll(/<text class="topo-path-more"[^>]*>([^<]*)<\/text>/g)].map((item) => item[1]),
    });
  }
  return cells;
}

function points(html, linkId) {
  const escaped = linkId.replace(/>/g, "&gt;").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`<polyline class="topo-link[^"]*" data-topo-link="${escaped}" points="([^"]+)"`).exec(html);
  assert.ok(match, `${linkId} is drawn`);
  return match[1].split(" ").map((pair) => pair.split(",").map(Number));
}

// ---- decimal strings from the real producer --------------------------

test("levels and PHY counts the producer sends as decimal strings draw the cascade top to bottom", () => {
  const T = H.loadTopology();
  const { fabric } = cases.core_cascade;
  const model = T.buildTopologyModel(fabric);
  const tierIds = [...model.tiers].map((tier) => tier.id);
  assert.deepEqual(tierIds.filter((id) => id.startsWith("expanders")), ["expanders:1", "expanders:2"]);
  const expanders = [...model.nodes].filter((item) => item.kind === "expander");
  const level = (value) => expanders.filter((item) => item.level === value);
  assert.equal(level(1).length, 2);
  assert.equal(level(2).length, 2);
  for (const lower of level(2)) for (const upper of level(1)) assert.ok(lower.y > upper.y, "level 2 is drawn below level 1");
  const html = T.renderTopology(model, {});
  assert.match(html, />level 1<\/text><text[^>]*>8 of 36 PHYs linked</);
  assert.match(html, />level 2<\/text><text[^>]*>4 of 36 PHYs linked</);
  assert.doesNotMatch(html, /level not reported/);

  // Strict reader: anything but plain decimal digits stays unknown.
  for (const bad of ["1.5", "-1", " 1", "1e1", "0x2", "", "12345678901", null, true, [], {}]) {
    const copy = JSON.parse(JSON.stringify(fabric));
    for (const item of copy.nodes) if (item.kind === "expander") item.metrics = { ...item.metrics, sas_level: bad, num_phys: bad };
    for (const item of copy.nodes) if (item.kind === "expander") item.raw = { ...item.raw, sas_level: bad };
    const badModel = T.buildTopologyModel(copy);
    assert.ok([...badModel.nodes].filter((item) => item.kind === "expander").every((item) => item.level === null), JSON.stringify(bad));
    assert.match(T.renderTopology(badModel, {}), /level not reported/);
  }
});

// ---- a logical inventory is not a bay ---------------------------------

test("disks without a physical location are never called bays or numbered as bays", () => {
  const T = H.loadTopology();
  const { fabric } = cases.linux_virtual;
  const model = T.buildTopologyModel(fabric);
  const html = T.renderTopology(model, {});
  const tier = [...model.tiers].find((item) => item.id === "bays");
  assert.doesNotMatch(tier.label, /bay/i);
  assert.doesNotMatch(tier.note, /bay/i);
  const cells = cellsOf(html);
  assert.equal(cells.size, 2);
  for (const [id, cell] of cells) {
    assert.doesNotMatch(cell.name, /\bbay\b/i, id);
    assert.match(cell.name, /^Disk \d, physical location unavailable/);
    assert.deepEqual(cell.text.map((text) => /^Disk \d$/.test(text)), [true]);
  }
  assert.doesNotMatch(html, /Bays \d/);
  assert.doesNotMatch(html, />\s*0\s*</, "no inventory ordinal is drawn as a bay number");
  const group = /<g class="topo-group[^"]*"[^>]*aria-label="([^"]*)"/.exec(html);
  assert.match(group[1], /^Disks, physical location unavailable · 2 disks/);
  // Search: a number is a bay number, so it finds no disk here; the label does.
  assert.equal(T.searchTopology(model, "0").matches.length, 0);
  assert.equal(T.searchTopology(model, "bay 1").matches.length, 0);
  assert.deepEqual([...T.searchTopology(model, "Disk 2").matches].map((item) => item.label), ["Disk 2"]);
  // Control: the CORE producer's bays have a location and keep bay wording.
  const core = T.renderTopology(T.buildTopologyModel(cases.core_cascade.fabric), {});
  assert.match(core, /Bays 0–3 · 4 bays/);
  assert.deepEqual(cellsOf(core).get("bay:3").text, ["3"]);
});

// ---- canonical path states, two marks per dual-path bay --------------

test("the producer's canonical path states each get their own mark, two independent marks per bay", () => {
  const T = H.loadTopology();
  const model = T.buildTopologyModel(cases.core_cascade.fabric);
  const html = T.renderTopology(model, {});
  const cells = cellsOf(html);
  assert.deepEqual(cells.get("bay:2").marks, ["active", "fail"]);
  assert.deepEqual(cells.get("bay:2").tones, ["active", "fault"]);
  assert.deepEqual(cells.get("bay:5").marks, ["active", "missing"]);
  assert.deepEqual(cells.get("bay:5").tones, ["active", "missing"]);
  assert.deepEqual(cells.get("bay:0").tones, ["active", "standby"]);
  for (const tone of ["path-active", "path-standby", "path-fault", "path-missing"]) {
    assert.ok(html.includes(`data-topology-legend="${tone}"`), `${tone} is in the legend`);
  }
  assert.doesNotMatch(html, /path-other/, "no canonical state is unrecognised");
  // Every state the producers emit (sas_fabric.py normalize_path_state, the
  // Linux SES builder, the platform route builders) maps to a named mark.
  const tones = Object.fromEntries(["active", "passive", "fail", "missing", "unknown", "mapped", "unmapped", "present", "mixed"]
    .map((state) => [state, T.pathTone(state)]));
  assert.deepEqual(tones, {
    active: "active", passive: "standby", fail: "fault", missing: "missing", unknown: "unknown",
    mapped: "mapped", unmapped: "unmapped", present: "member", mixed: "member",
  });
});

// ---- missing evidence is unknown; malformed payloads say so ----------

test("missing completeness or cache evidence is unknown, never a complete read or ok", () => {
  const T = H.loadTopology();
  const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  delete fabric.raw.controller_read;
  delete fabric.snapshot_cache_state;
  delete fabric.source_cache_state;
  const model = T.buildTopologyModel(fabric);
  const parts = Object.fromEntries([...model.parts].map((part) => [part.name, part.state]));
  assert.equal(parts.cache, "not reported");
  for (const name of ["controllers", "expanders", "enclosures"]) assert.equal(parts[name], "not reported", name);
  // Without expander nodes the tier says the read is not reported, not "none".
  fabric.nodes = fabric.nodes.filter((item) => item.kind !== "expander");
  const bare = T.buildTopologyModel(fabric);
  const tier = [...bare.tiers].find((item) => item.id === "expanders");
  assert.equal(tier.state, "not_read");
  assert.doesNotMatch(tier.note, /complete/);
  const html = T.renderTopology(bare, {});
  assert.doesNotMatch(html, /\(read complete\)/);
  assert.doesNotMatch(html, />[^<]*: ok</);
});

test("wrong collection types and null members give a payload-not-readable notice, never an exception", () => {
  const T = H.loadTopology();
  const variants = {
    "wrong types": { ...H.coreFabric(), nodes: {}, links: "wrong", traces: 2 },
    "null node": { ...H.coreFabric(), nodes: [null, ...H.coreFabric().nodes] },
    "null link": { ...H.coreFabric(), links: [null] },
    "null trace": { ...H.coreFabric(), traces: [null, 7] },
    "null payload": null,
  };
  for (const [name, fabric] of Object.entries(variants)) {
    const model = T.buildTopologyModel(fabric);
    const html = T.renderTopology(model, {});
    const notice = [...model.notices].find((text) => text.startsWith("Payload not readable"));
    assert.ok(notice, `${name}: ${JSON.stringify([...model.notices])}`);
    assert.ok(html.includes(notice.replace(/&/g, "&amp;")), name);
    // A tier with nothing drawn is never a measured zero when node entries
    // could not be read; a skipped link or trace entry says so in the notice.
    if (!["wrong types", "null node", "null payload"].includes(name)) continue;
    for (const tier of model.tiers) {
      if (tier.id === "host" && name === "null node") continue;
      assert.notEqual(tier.state, "none", `${name}: ${tier.id} is not an empty healthy tier`);
      assert.notEqual(tier.state, "drawn", `${name}: ${tier.id} has nothing readable to draw`);
    }
  }
  const wrong = T.buildTopologyModel(variants["wrong types"]);
  const notice = [...wrong.notices].find((text) => text.startsWith("Payload not readable"));
  for (const field of ["nodes", "links", "traces"]) assert.match(notice, new RegExp(field));
  assert.match([...T.buildTopologyModel(variants["null node"]).notices].join(" "), /1 node entry/);
  assert.match([...T.buildTopologyModel(variants["null trace"]).notices].join(" "), /2 trace entries/);
});

// ---- search finds what its label promises -----------------------------

test("search finds a disk by serial from the inventory and by the SAS address the fabric carries", () => {
  const T = H.loadTopology();
  const { fabric, snapshot } = cases.core_cascade;
  const model = T.buildTopologyModel(fabric, { inventory: snapshot });
  assert.deepEqual([...T.searchTopology(model, "SANITIZED-TOPO-3").matches].map((item) => item.id), ["bay:3"]);
  // The second path's disk address, from the bay trace's mpr_devices.
  const address = H.sasAddress(0xd000 + 2 * 0x10 + 1);
  assert.deepEqual([...T.searchTopology(model, address).matches].map((item) => item.id), ["bay:2"]);
  assert.deepEqual([...T.searchTopology(model, "da13").matches].map((item) => item.id), ["bay:5"], "member disk name");
  assert.deepEqual([...T.searchTopology(model, "multipath/disk6").matches].map((item) => item.id), ["bay:6"]);
  // An inventory for another system or enclosure is not used.
  const other = { ...snapshot, selected_system_id: "another-system" };
  assert.equal(T.searchTopology(T.buildTopologyModel(fabric, { inventory: other }), "SANITIZED-TOPO-3").matches.length, 0);
  const ses = T.buildTopologyModel(cases.scale_ses.fabric, { inventory: cases.scale_ses.snapshot });
  assert.deepEqual([...T.searchTopology(ses, H.sasAddress(0xc001)).matches].map((item) => item.id), ["bay:1"]);
  assert.deepEqual([...T.searchTopology(ses, "SANITIZED-SES-2").matches].map((item) => item.id), ["bay:2"]);
});

// ---- one pin and search per scope; bundle facts ------------------

function fakeDom() {
  const handlers = new Map();
  const searchHandlers = new Map();
  const documentHandlers = [];
  const pinText = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pinText : null), querySelectorAll: () => [] };
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === ".fabric-topology" ? root : null),
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  const search = { value: "", addEventListener: (name, handler) => searchHandlers.set(name, handler) };
  const status = { textContent: "" };
  const document = { addEventListener: (name, handler) => documentHandlers.push({ name, handler }) };
  // closest() answers like the DOM: the drawing's root, the item itself for
  // the selectors that name it, nothing for anything else.
  const element = (dataset) => ({
    dataset,
    closest(selector) {
      if (selector === ".fabric-topology") return root;
      return /\[data-topo-/.test(selector) ? this : null;
    },
  });
  return { handlers, searchHandlers, documentHandlers, pinText, container, search, status, document, element };
}

test("pin and search belong to one system and enclosure, and a typed search clears an older pin", () => {
  const dom = fakeDom();
  const T = H.loadTopology({ document: dom.document });
  const calls = [];
  T.attach(dom.container, dom.search, dom.status, { clearSelection: () => calls.push("clear"), requestRender: () => calls.push("render") });
  const first = cases.core_cascade.fabric;
  T.render(first, {});
  dom.handlers.get("click")({ target: dom.element({ topoBay: "bay:0" }) });
  assert.match(dom.pinText.textContent, /^Pinned: Bay 00/);
  // Typing a search clears the pin: the newest action wins.
  dom.search.value = "bay 3";
  dom.searchHandlers.get("input")();
  assert.doesNotMatch(dom.pinText.textContent, /^Pinned/);
  assert.match(dom.status.textContent, /^1 match: Bay 03/);
  // The same payload drawn again keeps the search; its status is recomputed.
  T.render(first, {});
  assert.equal(dom.search.value, "bay 3");
  // A different system clears pin, hover and search, and the status line.
  dom.handlers.get("click")({ target: dom.element({ topoBay: "bay:1" }) });
  const second = JSON.parse(JSON.stringify(first));
  second.system_id = "another-system";
  T.render(second, {});
  assert.equal(dom.search.value, "");
  assert.equal(dom.status.textContent, "");
  assert.doesNotMatch(T.renderTopology(T.buildTopologyModel(second), T.view()), /is-on-path/);
  // Leaving the mode clears them too.
  dom.search.value = "bay 4";
  dom.searchHandlers.get("input")();
  T.release();
  assert.equal(dom.search.value, "");
  assert.equal(dom.status.textContent, "");
  assert.equal(T.view().query, "");
  // The payload changing under an active search refreshes the status line.
  T.render(first, {});
  dom.search.value = "bay 7";
  dom.searchHandlers.get("input")();
  const fewer = JSON.parse(JSON.stringify(first));
  fewer.nodes = fewer.nodes.filter((item) => item.id !== "bay:7");
  T.render(fewer, {});
  assert.match(dom.status.textContent, /^No match/);
});

test("a pinned bay bundle fills the inspector with its source enclosure, bay count and bays", () => {
  const dom = fakeDom();
  const T = H.loadTopology({ document: dom.document });
  const calls = [];
  T.attach(dom.container, dom.search, dom.status, { clearSelection: () => calls.push("clear"), requestRender: () => calls.push("render") });
  const model = T.buildTopologyModel(cases.core_cascade.fabric);
  const bundle = [...model.links].find((item) => item.bundle && item.source === "mpr-enclosure:controller:mpr1:0003");
  T.render(cases.core_cascade.fabric, {});
  dom.handlers.get("click")({ target: dom.element({ topoLink: bundle.id }) });
  assert.ok(calls.includes("render"), "the page redraws its inspector");
  const detail = T.inspectorDetail();
  assert.ok(detail, "the inspector gets the bundle's facts");
  assert.match(detail.title, /bay bundle/i);
  assert.match(detail.html, /Source enclosure/);
  assert.ok(detail.html.includes(H.sasAddress(0xf02)), "the source enclosure is named");
  assert.match(detail.html, /4 bays/);
  assert.match(detail.html, /Bay 04, Bay 05, Bay 06, Bay 07/);
  // A physical link reports its lanes; a node or bay pin leaves the inspector to the page.
  const cascade = [...model.links].find((item) => item.kind === "expander-expander");
  dom.handlers.get("click")({ target: dom.element({ topoLink: cascade.id }) });
  assert.match(T.inspectorDetail().html, /x4 · 12 Gb\/s/);
  dom.handlers.get("click")({ target: dom.element({ topoBay: "bay:1" }) });
  assert.equal(T.inspectorDetail(), null);
});

// ---- every input record is accounted for ------------------------------

test("unsupported kinds, links with a missing end and duplicate ids are counted and named on the page", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const exp = H.addExpander(fabric, "mpr0", 0xa1, 1, hba.id);
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", exp.id);
  H.addBays(fabric, [0, 1, 2], [enc.id]);
  fabric.nodes.push(H.node("gadget:1", "new-device-kind"));
  fabric.links.push(H.link(hba.id, "expander:nowhere", "controller-expander"));
  fabric.nodes.push({ ...hba, label: "a second row for the same id" });
  fabric.nodes.push({ ...fabric.nodes.find((item) => item.id === "bay:1") });
  const model = T.buildTopologyModel(fabric);
  const text = [...model.notices].join(" ");
  assert.match(text, /1 node of a kind this view does not draw \(new-device-kind: gadget:1\)/);
  assert.match(text, /1 link with an end the payload does not carry \(controller-expander:controller:mpr0->expander:nowhere\)/);
  assert.match(text, /2 duplicate node ids \(controller:mpr0, bay:1\)/);
  assert.match(text, /4 bay rows in the payload, 3 bays drawn/);
  assert.ok(T.renderTopology(model, {}).includes("new-device-kind: gadget:1"));
  // Control: the real producer's payload needs no such notice.
  assert.deepEqual([...T.buildTopologyModel(cases.core_cascade.fabric).notices], []);
});

// ---- Path marks beyond what a cell can draw --------------------------------

test("a bay with more paths than its cell can draw shows the count of the rest, on the cell and in the legend", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", null);
  fabric.links.push(H.link(hba.id, enc.id, "controller-enclosure"));
  const states = ["active", "passive", "fail", "missing", "active", "passive"].map((state, index) => ({ controller: `mpr${index}`, state }));
  H.addBays(fabric, [0], [enc.id], states);
  H.addBays(fabric, [1], [enc.id], states.slice(0, 2));
  const html = T.renderTopology(T.buildTopologyModel(fabric), {});
  const cells = cellsOf(html);
  const busy = cells.get("bay:0");
  assert.equal(busy.marks.length + Number(busy.more[0].replace("+", "")), 6, "every path is either drawn or counted");
  assert.ok(busy.marks.length >= 2);
  assert.match(busy.name, /mpr5 passive/, "the name lists every path");
  assert.ok(html.includes('data-topology-legend="path-more"'));
  assert.deepEqual(cells.get("bay:1").more, [], "no count when every path is drawn");
});

// ---- a path node is captioned from what the payload says it is --------

test("path nodes are captioned from the fabric kind and path type, SES only where the payload says SES", () => {
  const T = H.loadTopology();
  const ses = T.buildTopologyModel(cases.scale_ses.fabric);
  const sesPath = [...ses.nodes].find((item) => item.kind === "path");
  assert.match(sesPath.facts.join(" "), /^SES device · mapped/);
  const block = T.buildTopologyModel(cases.linux_virtual.fabric);
  const paths = [...block.nodes].filter((item) => item.kind === "path");
  assert.equal(paths.length, 2);
  for (const item of paths) {
    assert.doesNotMatch(item.facts.join(" "), /SES/);
    assert.match(item.facts.join(" "), /^block device · member health healthy/);
  }
  assert.equal([...block.tiers].find((tier) => tier.id === "devices").label, "Storage paths");
  assert.equal([...ses.tiers].find((tier) => tier.id === "devices").label, "SES devices");
});

// ---- Rendered output, not only the model -----------------------------------

test("the rendered SVG draws every expanded bay, both path marks and the level-2 route from its level-1 parent", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const exp = H.addExpander(fabric, "mpr0", 0xa1, 1, hba.id);
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", exp.id);
  H.addBays(fabric, Array.from({ length: 1024 }, (_, index) => index), [enc.id], [{ controller: "mpr0", state: "active" }]);
  const collapsed = T.buildTopologyModel(fabric);
  const expanded = T.buildTopologyModel(fabric, { expanded: { [collapsed.groups[0].expandKey]: true } });
  const cells = cellsOf(T.renderTopology(expanded, {}));
  assert.equal(cells.size, 1024);
  for (let slot = 0; slot < 1024; slot += 1) assert.ok(cells.has(`bay:${slot}`), `bay ${slot} is drawn`);

  const real = T.buildTopologyModel(cases.core_cascade.fabric);
  const html = T.renderTopology(real, {});
  const dual = cellsOf(html).get("bay:4");
  assert.deepEqual(dual.marks, ["active", "passive"]);
  for (const cascade of [...real.links].filter((item) => item.kind === "expander-expander")) {
    const parent = real.nodeById.get(cascade.source);
    const [x, y] = points(html, cascade.id)[0];
    assert.ok(x > parent.x && x < parent.x + parent.w, `${cascade.id} starts under its level-1 parent`);
    assert.equal(y, Math.round(parent.y + parent.h));
    const hbaNode = real.nodeById.get(real.incoming.get(cascade.source)[0].source);
    assert.ok(y > hbaNode.y + hbaNode.h, "not at the HBA");
  }
});

// ---- the page hands Topology its selection, inventory and inspector --------

function loadPage(T, fabric, snapshot) {
  const fs = require("node:fs");
  const path = require("node:path");
  const vm = require("node:vm");
  const source = fs.readFileSync(path.join(H.ROOT, "app/static/sas_fabric_view.js"), "utf8");
  const elements = new Map();
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "?mode=topology" }, SAS_FABRIC_BOOTSTRAP: { snapshot, fabric }, SasFabricTopology: T },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", dataset: {}, className: "", hidden: true, closest: () => null });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
  });
  const end = source.indexOf("  elements.modeButtons.forEach((button) => {", source.indexOf("  function render()"));
  assert.ok(end > 0);
  vm.runInContext(`${source.slice(0, end)}
    window.renderTest = { renderMap, renderInspector, ensureSelectionForMode, setSelectionRef, state };
  })();`, sandbox);
  return { api: sandbox.window.renderTest, elements };
}

test("the page's inspector shows a pinned bundle's facts, and Topology selects nothing on its own", () => {
  const dom = fakeDom();
  const T = H.loadTopology({ document: dom.document });
  const { fabric, snapshot } = cases.core_cascade;
  const { api, elements } = loadPage(T, fabric, snapshot);
  api.state.fabric = fabric;
  api.ensureSelectionForMode(fabric);
  assert.equal(api.state.selectedTraceId, null, "no automatic selection in Topology");
  assert.equal(api.state.selectedNodeId, null);
  const cleared = [];
  const clearSelection = () => {
    cleared.push(true);
    api.state.selectedTraceId = null;
    api.state.selectedNodeId = null;
  };
  T.attach(dom.container, dom.search, dom.status, { clearSelection, requestRender: () => api.renderMap() });
  api.renderMap();
  const model = T.buildTopologyModel(fabric);
  const bundle = [...model.links].find((item) => item.bundle);
  dom.handlers.get("click")({ target: dom.element({ topoLink: bundle.id }) });
  assert.match(elements.get("fabric-inspector-title").textContent, /bay bundle/i);
  assert.match(elements.get("fabric-inspector-body").innerHTML, /Bays it carries/);
  // A bay the page selects is the drawing's pin; the inventory reaches the search.
  assert.ok(api.setSelectionRef({ kind: "trace", id: "bay:6" }));
  api.renderMap();
  assert.match(elements.get("fabric-map-panel").innerHTML, /Pinned: Bay 06/);
  assert.equal(T.inspectorDetail(), null);
  assert.deepEqual([...T.searchTopology(T.buildTopologyModel(fabric, { inventory: snapshot }), "SANITIZED-TOPO-6").matches].map((item) => item.id), ["bay:6"]);
  // Esc clears the pin and asks the page to clear its selection.
  dom.documentHandlers.find((item) => item.name === "keydown").handler({ key: "Escape", target: { closest: () => null } });
  assert.equal(cleared.length, 2);
  assert.equal(T.view().pin, null);
  assert.equal(api.state.selectedTraceId, null);
  assert.doesNotMatch(elements.get("fabric-map-panel").innerHTML, /Pinned:/);
});
