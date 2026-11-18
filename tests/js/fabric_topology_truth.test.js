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

// The drawing alone, without the legend's swatches.
function drawing(html) {
  const found = /<div class="fabric-topology-scroll">[\s\S]*?<\/svg><\/div>/.exec(html);
  assert.ok(found, "a drawing is shown");
  return found[0];
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
  assert.match(core, /Bays 00–03 · 4 bays/);
  assert.deepEqual(cellsOf(core).get("bay:3").text, ["03"]);
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
  assert.doesNotMatch(drawing(T.renderTopology(T.buildTopologyModel(second), T.view())), /is-on-path/);
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
  assert.match(elements.get("fabric-map-panel").innerHTML, /data-topology-pin>Pinned: Bay 06/);
  assert.equal(T.inspectorDetail(), null);
  assert.deepEqual([...T.searchTopology(T.buildTopologyModel(fabric, { inventory: snapshot }), "SANITIZED-TOPO-6").matches].map((item) => item.id), ["bay:6"]);
  // Esc clears the pin and asks the page to clear its selection.
  dom.documentHandlers.find((item) => item.name === "keydown").handler({ key: "Escape", target: { closest: () => null } });
  assert.equal(cleared.length, 2);
  assert.equal(T.view().pin, null);
  assert.equal(api.state.selectedTraceId, null);
  // The visible sentence.
  assert.doesNotMatch(elements.get("fabric-map-panel").innerHTML, /data-topology-pin>Pinned:/);
});

// ---- the CORE builder's own kind name, and traces with no drawn record ------

test("a CORE payload that names its builder kind core_mpr is drawn as CORE", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  fabric.raw.fabric_kind = "core_mpr";
  const hba = H.addController(fabric, "mpr0");
  const exp = H.addExpander(fabric, "mpr0", 0xa1, 1, hba.id);
  H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", exp.id);
  const model = T.buildTopologyModel(fabric);
  assert.deepEqual([...model.tiers].map((tier) => tier.id), ["host", "controllers", "expanders:1", "enclosures", "bays"]);
  assert.ok(model.nodeById.has(exp.id));
  assert.doesNotMatch(T.renderTopology(model, {}), /not collected on this platform/);
  assert.deepEqual([...model.notices], []);
});

test("bay traces without a bay node are named, not silently left out", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", null);
  fabric.links.push(H.link(hba.id, enc.id, "controller-enclosure"));
  H.addBays(fabric, [0, 1, 2, 3], [enc.id]);
  // Two traces whose bay node the payload does not carry.
  fabric.nodes = fabric.nodes.filter((item) => !["bay:2", "bay:3"].includes(item.id));
  fabric.links = fabric.links.filter((item) => !["bay:2", "bay:3"].includes(item.target));
  const model = T.buildTopologyModel(fabric);
  assert.equal(model.counts.baysPlaced, 2);
  assert.match([...model.notices].join(" "), /2 bay traces without a bay node \(bay:2, bay:3\)/);
});

// ---- member health is never drawn as a transport or SES state ---------------

test("a bay state whose basis is member health is drawn and named as member health, whatever its word", () => {
  const T = H.loadTopology();
  const { fabric } = cases.linux_health;
  const html = T.renderTopology(T.buildTopologyModel(fabric), {});
  const cells = cellsOf(html);
  // The real builder: a faulted disk, an unmapped logical disk, an optimal disk.
  assert.deepEqual(cells.get("bay:0").marks, ["fault"]);
  assert.deepEqual(cells.get("bay:1").marks, ["unmapped"]);
  assert.deepEqual(cells.get("bay:2").marks, ["optimal"]);
  for (const id of ["bay:0", "bay:1", "bay:2"]) {
    assert.deepEqual(cells.get(id).tones, ["member"], `${id} is a member-health mark`);
    assert.match(cells.get(id).name, /member health (fault|unmapped|optimal)/, `${id} says member health`);
  }
  for (const tone of ["path-fault", "path-unmapped", "path-active", "path-mapped"]) {
    assert.ok(!html.includes(`data-topology-legend="${tone}"`), `no ${tone} legend for member health`);
  }
  assert.ok(html.includes('data-topology-legend="path-member"'));
  // Control: the CORE transport states keep their own marks.
  assert.deepEqual(cellsOf(T.renderTopology(T.buildTopologyModel(cases.core_cascade.fabric), {})).get("bay:2").tones, ["active", "fault"]);
});

// ---- state words cannot reach the prototype ---------------------------------

test("state, kind and controller words such as __proto__ or constructor cannot stop the drawing", () => {
  const T = H.loadTopology();
  for (const word of ["__proto__", "constructor", "toString", "hasOwnProperty", "valueOf"]) {
    const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
    for (const trace of fabric.traces) {
      for (const state of trace.metrics.path_states || []) {
        state.state = word;
        state.controller_label = word;
      }
    }
    for (const item of fabric.nodes) if (item.kind === "path") item.metrics = { ...item.metrics, path_type: word };
    const model = T.buildTopologyModel(fabric);
    const html = T.renderTopology(model, {});
    const cell = cellsOf(html).get("bay:0");
    assert.deepEqual(cell.tones, ["other", "other"], word);
    assert.equal(Object.getPrototypeOf({}).polluted, undefined);
    const collapsed = T.buildTopologyModel(fabric, { expanded: JSON.parse(`{"${word}": true}`) });
    assert.ok(T.renderTopology(collapsed, {}).includes("<svg"), word);
  }
  // Every tone a lookup can give has a drawn mark and a legend entry.
  const words = ["active", "online", "optimal", "passive", "standby", "fail", "failed", "fault", "missing", "unknown", "mapped", "unmapped", "present", "mixed", "anything", "__proto__", "constructor"];
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", null);
  fabric.links.push(H.link(hba.id, enc.id, "controller-enclosure"));
  H.addBays(fabric, H.range(0, words.length * 2), [enc.id]);
  for (const trace of fabric.traces) {
    const word = words[trace.slots[0] % words.length];
    trace.metrics.path_states = [{ controller: "mpr0", state: word, state_basis: trace.slots[0] < words.length ? "" : "member-health-not-transport" }];
  }
  const html = T.renderTopology(T.buildTopologyModel(fabric), {});
  const cells = cellsOf(html);
  assert.equal(cells.size, words.length * 2);
  for (const [id, cell] of cells) {
    assert.equal(cell.tones.length, 1, id);
    assert.ok(html.includes(`data-topology-legend="path-${cell.tones[0]}"`), `${id}: ${cell.tones[0]} is in the legend`);
  }
});

// ---- every input record is accounted for -------------------------------------

test("a duplicate bay trace, a trace of an unsupported kind and a duplicate bay link each get a notice", () => {
  const T = H.loadTopology();
  const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  const first = fabric.traces.find((trace) => trace.id === "bay:1");
  fabric.traces.push({ ...JSON.parse(JSON.stringify(first)), label: "a second trace for the same bay" });
  fabric.traces.push({ id: "trace:alien", kind: "alien", label: "alien", metrics: {} });
  const pathBay = fabric.links.find((item) => item.kind === "path-bay" && item.target === "bay:6");
  fabric.links.push({ ...pathBay, status: "fail" });
  const model = T.buildTopologyModel(fabric);
  const text = [...model.notices].join(" ");
  assert.match(text, /1 duplicate trace id \(bay:1\)/);
  assert.match(text, /1 trace of a kind this view does not draw \(alien: trace:alien\)/);
  assert.match(text, /1 duplicate link id \(path-bay:/);
  // The first trace wins: its own label and path states are drawn.
  assert.equal(model.bayById.get("bay:1").traceId, "bay:1");
  // A rejected duplicate bay link cannot come back through the path-state fallback.
  const bare = JSON.parse(JSON.stringify(fabric));
  bare.traces = bare.traces.filter((trace) => trace.id !== "bay:6");
  const states = T.buildTopologyModel(bare).bayById.get("bay:6").pathStates.map((state) => state.state);
  assert.equal(states.filter((state) => state === "fail").length, 0, JSON.stringify(states));
  assert.equal(states.length, bare.links.filter((item) => item.kind === "path-bay" && item.target === "bay:6").length - 1);
  // Control: the real producer's payload needs no notice.
  assert.deepEqual([...T.buildTopologyModel(cases.core_cascade.fabric).notices], []);
});

// ---- only the exact supported fabric kinds are recognised --------------------

test("an unknown or conflicting fabric kind gets its own notice and makes no claim about collection", () => {
  const T = H.loadTopology();
  const base = () => {
    const fabric = H.coreFabric();
    const hba = H.addController(fabric, "mpr0");
    const exp = H.addExpander(fabric, "mpr0", 0xa1, 1, hba.id);
    const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", exp.id);
    H.addBays(fabric, [0, 1], [enc.id]);
    return fabric;
  };
  const claims = /not collected on this platform|not read|read not reported|read complete|no HBAs reported/;
  for (const [platform, kind, words] of [
    ["unknown", "core_alien", /fabric kind "core_alien" is not one this view knows/],
    ["unknown", "alien", /fabric kind "alien" is not one this view knows/],
    ["core", "linux_ses", /fabric kind "linux_ses" does not match platform "core"/],
    ["scale", "core_mpr", /fabric kind "core_mpr" does not match platform "scale"/],
    [undefined, undefined, /names no fabric kind/],
  ]) {
    const fabric = base();
    if (platform === undefined) delete fabric.platform;
    else fabric.platform = platform;
    if (kind !== undefined) fabric.raw.fabric_kind = kind;
    const model = T.buildTopologyModel(fabric);
    const html = T.renderTopology(model, {});
    const notices = [...model.notices].join(" ");
    assert.match(notices, words, `${platform}/${kind}`);
    assert.match(notices, /makes no claim about what was or was not collected/);
    assert.doesNotMatch(html.replace(notices.replace(/&/g, "&amp;").replace(/"/g, "&quot;"), ""), claims, `${platform}/${kind}: no collection claim`);
    // Nothing is dropped: the expander and the bays are still drawn.
    assert.ok(html.includes("data-topo-node=\"expander:controller:mpr0:"), `${platform}/${kind}`);
    assert.equal(cellsOf(html).size, 2);
  }
  // The real CORE payload that names no kind keeps its CORE drawing.
  const core = T.buildTopologyModel(cases.core_cascade.fabric);
  assert.equal(core.core, true);
  assert.deepEqual([...core.notices], []);
  // A CORE builder kind is CORE even where the platform field is absent.
  const named = base();
  delete named.platform;
  named.raw.fabric_kind = "core_mpr";
  const namedModel = T.buildTopologyModel(named);
  assert.equal(namedModel.core, true);
  assert.deepEqual([...namedModel.notices], []);
  assert.deepEqual([...namedModel.tiers].map((tier) => tier.id), ["host", "controllers", "expanders:1", "enclosures", "bays"]);
  // Every storage kind the producers emit is a non-CORE drawing on a platform
  // that produces it (each pair is checked in full in the kind/platform test).
  for (const [kind, platform] of [["linux_ses", "scale"], ["linux_ses", "linux"], ["storage_scale", "scale"], ["storage_linux", "linux"], ["storage_quantastor", "quantastor"], ["storage_esxi", "esxi"], ["storage_bmc", "ipmi"], ["storage_generic", "other"]]) {
    const fabric = JSON.parse(JSON.stringify(cases.linux_virtual.fabric));
    fabric.raw.fabric_kind = kind;
    fabric.platform = platform;
    const model = T.buildTopologyModel(fabric);
    assert.equal(model.core, false, kind);
    assert.deepEqual([...model.notices], [], `${kind} on ${platform}`);
    assert.match(T.renderTopology(model, {}), /not collected on this platform/, kind);
  }
});

// ---- bay numbers are the producer's labels -----------------------------------

test("a profile that counts bays from 1 shows the producer's label in the cell, heading and search", () => {
  const T = H.loadTopology();
  const model = T.buildTopologyModel(cases.linux_health.fabric);
  const html = T.renderTopology(model, {});
  const cells = cellsOf(html);
  assert.deepEqual(cells.get("bay:0").text, ["01"]);
  assert.deepEqual(cells.get("bay:2").text, ["03"]);
  assert.match(cells.get("bay:0").name, /^Bay 01,/);
  assert.doesNotMatch(html, /Bays 0–/);
  assert.deepEqual([...T.searchTopology(model, "1").matches].map((item) => item.id), ["bay:0"]);
  assert.deepEqual([...T.searchTopology(model, "bay 3").matches].map((item) => item.id), ["bay:2"]);
  assert.equal(T.searchTopology(model, "0").matches.length, 0, "the internal index is not a bay number");
  // Control: a profile that counts from 0 keeps "00".
  assert.deepEqual(cellsOf(T.renderTopology(T.buildTopologyModel(cases.core_cascade.fabric), {})).get("bay:0").text, ["00"]);
});

// ---- wording in a logical-disk state, and on a source that is not an HBA ----

test("in a logical-disk state the legend, footer and search help say disk, and a source is not called an HBA", () => {
  const T = H.loadTopology({ document: { addEventListener() {}, removeEventListener() {} } });
  const model = T.buildTopologyModel(cases.linux_virtual.fabric);
  const html = T.renderTopology(model, {});
  const legend = /<div class="fabric-topology-legend"[^>]*>([\s\S]*?)<\/div>/.exec(html)[1];
  const footer = /<p class="fabric-topology-not-shown[^"]*">([^<]*)<\/p>/.exec(html)[1];
  assert.doesNotMatch(legend.replace(/<[^>]+>/g, " "), /\bbays?\b/i, legend);
  assert.doesNotMatch(footer, /\bbays?\b/i, footer);
  assert.doesNotMatch(legend, /\bHBA\b/, "the source card says it is not an HBA");
  const help = T.searchHelp(model);
  assert.doesNotMatch(`${help.label} ${help.placeholder}`, /\bbay\b/i);
  assert.match(help.label, /disk/);
  // The page's search row takes the help text.
  const label = { textContent: "Find a bay, disk or expander" };
  const input = { value: "", placeholder: "Bay number", labels: [label], addEventListener() {} };
  T.attach({ dataset: {}, querySelector: () => null, addEventListener() {} }, input, { textContent: "" }, {});
  T.render(cases.linux_virtual.fabric, {});
  assert.doesNotMatch(`${label.textContent} ${input.placeholder}`, /\bbay\b/i);
  // The dashed band explains both "not read" and "read, not reported".
  const core = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  delete core.raw.controller_read;
  core.nodes = core.nodes.filter((item) => item.kind !== "expander");
  const band = T.renderTopology(T.buildTopologyModel(core), {});
  const item = /data-topology-legend="mark-not-read"[^>]*>(?:<[^>]+>)*([^<]*)/.exec(band)[1];
  assert.match(item, /not read/);
  assert.match(item, /read, (?:but )?(?:its completeness )?not reported/);
  // Control: CORE bays keep bay wording and HBAs keep their name.
  const coreHtml = T.renderTopology(T.buildTopologyModel(cases.core_cascade.fabric), {});
  assert.match(coreHtml, /every line from this HBA down/);
  assert.match(T.searchHelp(T.buildTopologyModel(cases.core_cascade.fabric)).label, /bay/);
});

// ---- occupancy that is not a boolean stays unknown -------------------------

test("occupancy that is not a boolean is drawn unknown, never empty", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", null);
  fabric.links.push(H.link(hba.id, enc.id, "controller-enclosure"));
  const values = ["unknown", null, 0, 1, "false", "true", {}, []];
  H.addBays(fabric, H.range(0, values.length + 2), [enc.id]);
  values.forEach((value, index) => {
    fabric.nodes.find((item) => item.id === `bay:${index}`).metrics.present = value;
  });
  fabric.nodes.find((item) => item.id === `bay:${values.length}`).metrics.present = false;
  delete fabric.nodes.find((item) => item.id === `bay:${values.length + 1}`).metrics.present;
  const html = T.renderTopology(T.buildTopologyModel(fabric), {});
  const classes = new Map([...html.matchAll(/<g class="topo-bay (bay-[a-z]+)[^"]*"[^>]*data-topo-bay="([^"]+)"/g)].map((match) => [match[2], match[1]]));
  values.forEach((value, index) => assert.equal(classes.get(`bay:${index}`), "bay-unknown", JSON.stringify(value)));
  assert.equal(classes.get(`bay:${values.length}`), "bay-no", "control: false is empty");
  assert.equal(classes.get(`bay:${values.length + 1}`), "bay-unknown", "absent is unknown");
  const big = H.coreFabric();
  const hba2 = H.addController(big, "mpr0");
  const enc2 = H.addEnclosure(big, "mpr0", "0001", "SYNTH-ENC", null);
  big.links.push(H.link(hba2.id, enc2.id, "controller-enclosure"));
  H.addBays(big, H.range(0, 120), [enc2.id]);
  for (const item of big.nodes) if (item.kind === "bay") item.metrics.present = item.slot < 100 ? "unknown" : false;
  const summary = T.buildTopologyModel(big).groups[0].summary;
  assert.deepEqual([summary.withDisk, summary.empty, summary.unknown], [0, 20, 100]);
});

// ---- leaving Topology through the page's mode buttons ------------------------

function loadFullPage(T, sharedDocument, fabric, snapshot, customize = () => {}, page = null) {
  const fs = require("node:fs");
  const path = require("node:path");
  const vm = require("node:vm");
  const source = fs.readFileSync(path.join(H.ROOT, "app/static/sas_fabric_view.js"), "utf8");
  const elements = new Map();
  const buttons = ["lanes", "impact", "trace", "disk", "topology"].map((mode) => {
    const handlers = new Map();
    return {
      dataset: { fabricMode: mode },
      handlers,
      textContent: mode,
      classList: { toggle() {} },
      setAttribute() {},
      addEventListener(type, handler) { handlers.set(type, handler); },
    };
  });
  const element = (id) => {
    const item = {
      id, innerHTML: "", textContent: "", value: "", dataset: {}, className: "", hidden: false,
      clientWidth: 1200, scrollLeft: 0, scrollTop: 0, options: [],
      classList: { toggle() {}, add() {}, remove() {} },
      closest: () => null, querySelector: () => null, querySelectorAll: () => [],
      setAttribute() {}, removeAttribute() {}, appendChild() {},
      handlers: new Map(),
      addEventListener(type, handler) { item.handlers.set(type, handler); },
    };
    customize(id, item);
    return item;
  };
  Object.assign(sharedDocument, {
    activeElement: null,
    body: { classList: { toggle() {} } },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element(id));
      return elements.get(id);
    },
    querySelectorAll: (selector) => (selector === "[data-fabric-mode]" ? buttons : []),
    querySelector: () => null,
  });
  const sandbox = vm.createContext({
    URLSearchParams, URL, console, setTimeout, clearTimeout, AbortSignal, AbortController,
    HTMLInputElement: class {}, HTMLFormElement: class {}, HTMLSelectElement: class {}, HTMLDetailsElement: class {}, Element: class {},
    fetch: () => new Promise(() => {}),
    window: {
      location: { search: "?mode=topology", pathname: "/sas-fabric", href: "http://example.test/sas-fabric", origin: "http://example.test" },
      history: { replaceState() {} },
      requestAnimationFrame(callback) { callback(); },
      scrollX: 0,
      scrollY: 0,
      innerHeight: 900,
      SAS_FABRIC_BOOTSTRAP: { snapshot, fabric },
      SasFabricTopology: T,
    },
    document: sharedDocument,
  });
  page?.bind(sandbox.window);
  vm.runInContext(source.replace(/\}\)\(\);\s*$/, "  window.__page = { state, render };\n})();"), sandbox);
  const click = (mode) => buttons.find((button) => button.dataset.fabricMode === mode).handlers.get("click")();
  return { page: sandbox.window.__page, elements, click, sandbox };
}

test("leaving Topology through the mode buttons clears its pin, trace and search, also with no payload", () => {
  for (const variant of ["unavailable", "absent"]) {
    const dom = fakeDom();
    const T = H.loadTopology({ document: dom.document });
    const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
    const { page, elements, click } = loadFullPage(T, dom.document, fabric, cases.core_cascade.snapshot);
    const panel = elements.get("fabric-map-panel");
    const search = elements.get("fabric-topology-search");
    page.state.mode = "topology";
    page.render();
    const bundle = [...T.buildTopologyModel(fabric).links].find((item) => item.bundle);
    panel.handlers.get("click")({ target: dom.element({ topoLink: bundle.id }) });
    assert.ok(T.inspectorDetail(), `${variant}: the bundle is pinned`);
    search.value = "bay 3";
    search.handlers.get("input")();
    panel.handlers.get("click")({ target: dom.element({ topoLink: bundle.id }) });
    // The payload goes away, the operator leaves Topology and comes back.
    if (variant === "unavailable") page.state.fabric = { ...fabric, available: false };
    else page.state.fabric = null;
    click("lanes");
    click("topology");
    page.state.fabric = fabric;
    page.render();
    assert.equal(T.inspectorDetail(), null, `${variant}: no pin comes back`);
    assert.equal(T.view().pin, null, variant);
    assert.equal(T.view().query, "", variant);
    assert.equal(search.value, "", variant);
    assert.doesNotMatch(drawing(panel.innerHTML), /is-on-path|is-pinned/, variant);
  }
});

test("a pinned link's facts leave the inspector when the selected scope has no fabric to draw", () => {
  const dom = fakeDom();
  const T = H.loadTopology({ document: dom.document });
  const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  const { page, elements } = loadFullPage(T, dom.document, fabric, cases.core_cascade.snapshot);
  const panel = elements.get("fabric-map-panel");
  const title = elements.get("fabric-inspector-title");
  const body = elements.get("fabric-inspector-body");
  page.state.mode = "topology";
  page.render();
  const bundle = [...T.buildTopologyModel(fabric).links].find((item) => item.bundle);
  panel.handlers.get("click")({ target: dom.element({ topoLink: bundle.id }) });
  const pinned = T.inspectorDetail();
  assert.ok(pinned, "the bundle is pinned");
  assert.equal(body.innerHTML, pinned.html, "the inspector shows the pinned link");
  // The operator stays in Topology and selects a system whose fabric is not available.
  page.state.fabric = { ...fabric, system_id: "synthetic-other", available: false };
  page.render();
  assert.equal(T.inspectorDetail(), null, "the pin is gone with the scope it belonged to");
  assert.notEqual(title.textContent, pinned.title);
  assert.notEqual(body.innerHTML, pinned.html, "the other scope's link facts are not shown");
  assert.equal(T.view().pin, null);
  // Back on the first system nothing is pinned.
  page.state.fabric = fabric;
  page.render();
  assert.equal(T.inspectorDetail(), null, "no pin comes back");
  assert.doesNotMatch(panel.innerHTML, /is-pinned/);
});

test("the Path Focus and Disk Path strip is hidden in Topology and returns in the older modes", () => {
  const dom = fakeDom();
  const T = H.loadTopology({ document: dom.document });
  const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  const { page, elements, click } = loadFullPage(T, dom.document, fabric, cases.core_cascade.snapshot);
  const strip = elements.get("fabric-focus-strip");
  click("lanes");
  const before = strip.innerHTML;
  assert.equal(strip.hidden, false);
  assert.ok(before.length > 0, "the older modes fill the strip");
  click("topology");
  assert.equal(strip.hidden, true, "Topology hides the strip");
  for (const mode of ["lanes", "impact", "trace", "disk"]) {
    click(mode);
    assert.equal(strip.hidden, false, mode);
    assert.ok(strip.innerHTML.length > 0, `${mode} fills the strip`);
    click("topology");
    assert.equal(strip.hidden, true, `topology after ${mode}`);
  }
  void page;
});

// ---- a bay label without a supported number ------------------------------------

test("a bay label with no supported number is drawn as the producer wrote it, and no detail invents a number", () => {
  const T = H.loadTopology();
  // The real builder keeps a slot label of Arabic-Indic digits as "Bay ١".
  const { fabric, snapshot } = cases.ses_non_ascii_labels;
  const model = T.buildTopologyModel(fabric, { inventory: snapshot });
  const cells = cellsOf(T.renderTopology(model, {}));
  assert.equal(cells.size, 4);
  assert.deepEqual(cells.get("bay:0").text, ["Bay \u0661"]);
  assert.deepEqual(cells.get("bay:2").text, ["Bay \u0663"]);
  assert.deepEqual(cells.get("bay:3").text, ["04"], "control: an ASCII number is shown as the number");
  for (const bay of model.bays) {
    const shown = bay.number || bay.label;
    assert.ok(bay.w >= shown.length * 7 + 12 || shown.length * 7 + 12 <= 32, `${bay.bayId}: "${shown}" fits its ${bay.w} px cell`);
  }
  // Its label text finds it; a number finds only the bay that carries one.
  assert.deepEqual([...T.searchTopology(model, "\u0661").matches].map((item) => item.id), ["bay:0"]);
  assert.deepEqual([...T.searchTopology(model, "4").matches].filter((item) => item.kind === "bay").map((item) => item.id), ["bay:3"]);
  // A bundle of more than 48 such bays lists only the numbers the payload has.
  const many = JSON.parse(JSON.stringify(fabric));
  const template = many.nodes.find((item) => item.id === "bay:0");
  const linkTemplate = many.links.find((item) => item.kind === "ses-bay" && item.target === "bay:0");
  for (let index = 0; index < 60; index += 1) {
    const id = `bay:${100 + index}`;
    const label = index < 30 ? `Bay ${"\u0661".repeat(1 + (index % 3))}${index}` : `Bay ${10 + index}`;
    many.nodes.push({ ...JSON.parse(JSON.stringify(template)), id, slot: 100 + index, label });
    many.links.push({ ...JSON.parse(JSON.stringify(linkTemplate)), id: `ses-bay:${id}`, target: id });
  }
  const big = T.buildTopologyModel(many);
  const bundle = [...big.links].find((item) => item.bundle && item.count > 48);
  assert.ok(bundle, "one bundle carries the 64 bays");
  const detail = T.linkDetail(big, bundle.id).html.replace(/<[^>]+>/g, " ");
  const listed = /numbers ([0-9–, ]+)/.exec(detail);
  assert.ok(listed, detail);
  assert.equal(listed[1].trim(), "4, 40–69", "only the numbers the labels carry");
  assert.doesNotMatch(detail, /\b0, 0\b/);
  assert.match(detail, /33 bays? whose labels? carr(?:y|ies) no number/);
});

test("a bay group where only some labels carry a number says how many are numbered, not a range over all of them", () => {
  const T = H.loadTopology();
  const heading = (model) => [...model.groups].map((group) => group.lines[0].text);
  // The real builder's labels: three of Arabic-Indic digits, one "04".
  const { fabric, snapshot } = cases.ses_non_ascii_labels;
  const model = T.buildTopologyModel(fabric, { inventory: snapshot });
  assert.deepEqual(heading(model), ["4 bays, 1 numbered: 04"]);
  const html = T.renderTopology(model, {});
  assert.doesNotMatch(html, /Bays 04–04/);
  assert.match(html, /aria-label="4 bays, 1 numbered: 04"/);
  // Every bay numbered: the range, as before.
  const full = T.buildTopologyModel(cases.core_cascade.fabric, { inventory: cases.core_cascade.snapshot });
  for (const text of heading(full)) assert.match(text, /^Bays \d+–\d+ · \d+ bays$/, text);
});

// ---- titles stay distinct after every generated suffix ---------------------------

test("no two boxes with different ids show the same title, also where a generated title equals an existing alias", () => {
  const T = H.loadTopology();
  const visibleTitle = (item) => item.lines.slice(0, item.titleLines).join(" ");
  const distinct = (name, model) => {
    const seen = new Map();
    for (const item of model.nodes) {
      const title = `${item.tier}: ${visibleTitle(item)}`;
      assert.ok(!seen.has(title) || seen.get(title) === item.id, `${name}: ${item.id} and ${seen.get(title)} both read "${title}"`);
      seen.set(title, item.id);
    }
  };
  // Three boxes: two named X and one already named "X (<id of the first>)".
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const first = H.addEnclosure(fabric, "mpr0", "000a", "X", null);
  H.addEnclosure(fabric, "mpr0", "000b", "X", null);
  H.addEnclosure(fabric, "mpr0", "000c", `X (${first.id})`, null);
  for (const item of fabric.nodes.filter((node) => node.kind === "mpr-enclosure")) {
    delete item.controller_id;
    fabric.links.push(H.link(hba.id, item.id, "controller-enclosure"));
  }
  distinct("three boxes", T.buildTopologyModel(fabric));
  // The real cascade: both mpr0 enclosure views and the first mpr1 view
  // carry aliases, the last one equal to what the passes generate.
  const cascade = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  for (const item of cascade.nodes) {
    if (item.id.startsWith("mpr-enclosure:controller:mpr0:")) item.alias = "X";
    if (item.id === "mpr-enclosure:controller:mpr1:0002") item.alias = "X via mpr0 (mpr-enclosure:controller:mpr0:0002)";
  }
  distinct("real cascade with aliases", T.buildTopologyModel(cascade));
  // A fourth view already carries the number a generated title would get:
  // the payload's titles are kept as they are, the generated one takes the
  // next free number, and the order of the payload's nodes changes nothing.
  const numbered = JSON.parse(JSON.stringify(cascade));
  const real = "X via mpr0 (mpr-enclosure:controller:mpr0:0002) #2";
  numbered.nodes.find((item) => item.id === "mpr-enclosure:controller:mpr1:0003").alias = real;
  const titles = (fabric) => Object.fromEntries([...T.buildTopologyModel(fabric).nodes].filter((item) => item.tier === "enclosures").map((item) => [item.id, item.label]));
  const forward = titles(numbered);
  const reversed = JSON.parse(JSON.stringify(numbered));
  reversed.nodes.reverse();
  assert.deepEqual(titles(reversed), forward, "the same nodes in reversed order give the same titles");
  assert.equal(forward["mpr-enclosure:controller:mpr1:0003"], real, "a title from the payload is kept");
  assert.equal(forward["mpr-enclosure:controller:mpr1:0002"], "X via mpr0 (mpr-enclosure:controller:mpr0:0002)", "a title from the payload is kept");
  assert.equal(forward["mpr-enclosure:controller:mpr0:0002"], "X via mpr0 (mpr-enclosure:controller:mpr0:0002) #3", "the generated title takes the next free number");
  assert.ok(Object.values(forward).every((title) => !/#\d+ #\d+$/.test(title)), Object.values(forward).join(" | "));
  distinct("real numbered alias", T.buildTopologyModel(numbered));
  distinct("real numbered alias, reversed", T.buildTopologyModel(reversed));
  // Without the payload's numbered title, the reversed cascade numbers the same box too.
  const plain = JSON.parse(JSON.stringify(cascade));
  plain.nodes.reverse();
  assert.deepEqual(titles(plain), titles(cascade));
});

// Two pairs of boxes whose generated titles equal each other: each title is
// its name and its id, and one box's name and id read as the other's. The
// box with the lower id keeps the title and the other takes " #2", whatever
// the payload's order.
test("two generated titles that are equal get their numbers in identifier order, not payload order", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const boxes = [
    ["enc:2", "Shelf (enc:1)"], ["enc:3", "Shelf (enc:1)"], ["enc:1) (enc:2", "Shelf"], ["enc:4", "Shelf"],
    ["enc:6", "Tray (enc:5)"], ["enc:7", "Tray (enc:5)"], ["enc:5) (enc:6", "Tray"], ["enc:8", "Tray"],
  ];
  for (const [index, [id, label]] of boxes.entries()) {
    const item = H.addEnclosure(fabric, "mpr0", `00${index}`, label, null);
    delete item.controller_id;
    item.id = id;
    fabric.links.push(H.link(hba.id, id, "controller-enclosure"));
  }
  const visible = (payload) => Object.fromEntries([...T.buildTopologyModel(payload).nodes]
    .filter((item) => item.tier === "enclosures")
    .map((item) => [item.id, item.lines.slice(0, item.titleLines).join(" ")]));
  const forward = visible(fabric);
  assert.deepEqual(Object.keys(forward).sort(), boxes.map(([id]) => id).sort(), "every box is drawn");
  assert.equal(forward["enc:1) (enc:2"], "Shelf (enc:1) (enc:2)", "the lower id keeps the generated title");
  assert.equal(forward["enc:2"], "Shelf (enc:1) (enc:2) #2", "the higher id takes the number");
  assert.equal(forward["enc:5) (enc:6"], "Tray (enc:5) (enc:6)");
  assert.equal(forward["enc:6"], "Tray (enc:5) (enc:6) #2");
  assert.equal(new Set(Object.values(forward)).size, boxes.length, Object.values(forward).join(" | "));
  const reversed = JSON.parse(JSON.stringify(fabric));
  reversed.nodes.reverse();
  reversed.links.reverse();
  assert.deepEqual(visible(reversed), forward, "the same titles in reversed payload order");
});

// ---- a storage kind is accepted only on a platform that produces it ------------

test("a named storage kind is accepted only on a platform that produces it; every other pair makes no collection claim and draws every record", () => {
  const T = H.loadTopology();
  const producers = {
    "": ["core"],
    core_mpr: ["core", null],
    linux_ses: ["scale", "linux", null],
    storage_scale: ["scale", null],
    storage_linux: ["linux", null],
    storage_quantastor: ["quantastor", null],
    storage_esxi: ["esxi", null],
    storage_bmc: ["ipmi", null],
    storage_generic: ["something-else", "unknown", null],
  };
  const platforms = ["core", "scale", "linux", "quantastor", "esxi", "ipmi", "unknown", "something-else", null];
  const claims = /not collected on this platform|not read|read not reported|read complete|no HBAs reported/;
  for (const [kind, accepted] of Object.entries(producers)) {
    for (const platform of platforms) {
      // A storage payload from the real builder, plus four supplied
      // expander records a storage drawing would leave out.
      const fabric = JSON.parse(JSON.stringify(cases.linux_virtual.fabric));
      if (kind) fabric.raw.fabric_kind = kind;
      else delete fabric.raw.fabric_kind;
      if (platform === null) delete fabric.platform;
      else fabric.platform = platform;
      const source = fabric.nodes.find((item) => item.kind === "controller");
      for (let index = 0; index < 4; index += 1) {
        const id = `expander:synthetic:${index}`;
        fabric.nodes.push(H.node(id, "expander", { metrics: { sas_level: "1" } }));
        fabric.links.push(H.link(source.id, id, "controller-expander"));
      }
      const model = T.buildTopologyModel(fabric);
      const html = T.renderTopology(model, {});
      const name = `${kind || "no kind"} on ${platform ?? "no platform"}`;
      if (accepted.includes(platform)) {
        assert.notEqual(model.contract, "unknown", `${name} is accepted`);
        assert.deepEqual([...model.notices].filter((text) => /fabric kind/.test(text)), [], name);
        continue;
      }
      assert.equal(model.contract, "unknown", `${name} takes the unknown-contract path`);
      const notices = [...model.notices].join(" ");
      assert.match(notices, /makes no claim about what was or was not collected/, name);
      assert.doesNotMatch(html.replace(notices.replace(/&/g, "&amp;").replace(/"/g, "&quot;"), ""), claims, `${name}: no collection claim`);
      for (let index = 0; index < 4; index += 1) assert.ok(html.includes(`data-topo-node="expander:synthetic:${index}"`), `${name}: expander ${index} is drawn`);
      for (const item of fabric.nodes.filter((node) => node.kind === "path")) assert.ok(html.includes(`data-topo-node="${item.id}"`), `${name}: ${item.id} is drawn`);
      assert.equal(cellsOf(html).size, fabric.nodes.filter((node) => node.kind === "bay").length, `${name}: every bay is drawn`);
    }
  }
});

test("the unknown-contract notice names what it does not draw, and every other record is drawn", () => {
  const T = H.loadTopology();
  // The real builder's SES payload on a platform that does not produce it.
  const fabric = JSON.parse(JSON.stringify(cases.ses_non_ascii_labels.fabric));
  fabric.platform = "esxi";
  fabric.nodes.push(H.node("pool:synthetic", "pool"), H.node("vdev:synthetic", "vdev"));
  const model = T.buildTopologyModel(fabric);
  assert.equal(model.contract, "unknown");
  const notice = [...model.notices].find((text) => /does not match platform/.test(text));
  assert.equal(notice, "The fabric kind \"linux_ses\" does not match platform \"esxi\": it draws the records the payload carries "
    + "except pools, vdevs and backplane zones, which this view does not draw, and makes no claim about what was or was not collected.");
  const html = T.renderTopology(model, {});
  const hidden = new Set(["pool", "vdev", "backplane"]);
  assert.ok(fabric.nodes.some((item) => item.kind === "backplane"), "the payload has backplane zones");
  for (const item of fabric.nodes) {
    const drawn = html.includes(`data-topo-node="${item.id}"`) || html.includes(`data-topo-bay="${item.id}"`);
    assert.equal(drawn, !hidden.has(item.kind), `${item.kind} ${item.id}`);
  }
});

// ---- an all-digit query is a bay number and an identity --------------------------

test("an all-digit query finds bay numbers first, then serials, SAS addresses and node identities that contain it", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const exp = H.addExpander(fabric, "mpr0", 0xa1, 1, hba.id);
  fabric.nodes.find((item) => item.id === exp.id).label = "700012340000";
  fabric.nodes.find((item) => item.id === exp.id).raw_id = "700012340000";
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", exp.id);
  H.addBays(fabric, H.range(0, 8), [enc.id]);
  fabric.nodes.find((item) => item.id === "bay:2").metrics.serial = "800012340002";
  fabric.nodes.find((item) => item.id === "bay:5").metrics.sas_address = "300012345005";
  const model = T.buildTopologyModel(fabric);
  const ids = (query) => [...T.searchTopology(model, query).matches].map((item) => item.id);
  assert.deepEqual(ids("800012340002"), ["bay:2"], "a serial made of digits");
  assert.deepEqual(ids("300012345005"), ["bay:5"], "a SAS address made of digits");
  assert.deepEqual(ids("700012340000"), [exp.id], "an expander identity made of digits");
  // Bay 05 by its number first, then every identity that contains the digits.
  assert.equal(ids("5")[0], "bay:5");
  assert.equal(ids("5").filter((id) => id === "bay:5").length, 1, "listed once");
  assert.ok(ids("1234").includes("bay:2") && ids("1234").includes("bay:5") && ids("1234").includes(exp.id), ids("1234").join(" "));
  assert.deepEqual(ids("bay 5"), ["bay:5"], "\"bay N\" stays a bay number");
  // A logical-disk view has no bay numbers; an all-digit serial still finds its disk.
  const { fabric: virtual, snapshot } = cases.linux_virtual;
  const inventory = JSON.parse(JSON.stringify(snapshot));
  inventory.slots[1].serial = "900000000011";
  const logical = T.buildTopologyModel(virtual, { inventory });
  assert.deepEqual([...T.searchTopology(logical, "900000000011").matches].map((item) => item.id), ["bay:1"]);
});

test("an all-digit query finds an inventory logical unit id and a trace SAS address, and nothing for other digits", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", null);
  fabric.links.push(H.link(hba.id, enc.id, "controller-enclosure"));
  H.addBays(fabric, H.range(0, 8), [enc.id]);
  // The SAS address only the bay's trace carries.
  fabric.traces.find((trace) => trace.id === "bay:3").metrics.mpr_devices = [{ sas_address: "310012340003" }];
  // The logical unit id only the inventory carries.
  const inventory = {
    selected_system_id: fabric.system_id,
    selected_enclosure_id: fabric.selected_enclosure_id,
    slots: [{ slot: 7, logical_unit_id: "600012340007" }],
  };
  const model = T.buildTopologyModel(fabric, { inventory });
  const ids = (query) => [...T.searchTopology(model, query).matches].map((item) => item.id);
  assert.deepEqual(ids("600012340007"), ["bay:7"], "the inventory's logical unit id");
  assert.deepEqual(ids("600012349999"), [], "other digits find nothing");
  assert.deepEqual(ids("310012340003"), ["bay:3"], "the trace's SAS address");
  assert.deepEqual(ids("310012349999"), [], "other digits find nothing");
  // Without the inventory and the trace's device, the same digits find nothing.
  const bare = JSON.parse(JSON.stringify(fabric));
  bare.traces.find((trace) => trace.id === "bay:3").metrics.mpr_devices = [];
  const plain = T.buildTopologyModel(bare);
  assert.deepEqual([...T.searchTopology(plain, "600012340007").matches], []);
  assert.deepEqual([...T.searchTopology(plain, "310012340003").matches], []);
});

// ---- behaviour the tests did not hold -------------------------------------------

test("a value shown in the inspector is escaped", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hostile = "<img src=x onerror=alert(1)>&\"'";
  const hba = H.addController(fabric, "mpr0");
  const exp = H.addExpander(fabric, "mpr0", 0xa1, 1, hba.id, { reported_lanes: H.lanes([0, 1], [4, 5]) });
  fabric.nodes.find((item) => item.id === hba.id).label = hostile;
  const enc = H.addEnclosure(fabric, "mpr0", "0001", hostile, exp.id);
  H.addBays(fabric, H.range(0, 60), [enc.id]);
  for (const item of fabric.nodes.filter((node) => node.kind === "bay")) item.label = `${hostile} ${item.slot}`;
  const model = T.buildTopologyModel(fabric);
  for (const link of [...model.links].filter((item) => item.bundle || item.kind === "controller-expander")) {
    const { html } = T.linkDetail(model, link.id);
    assert.ok(!html.includes("<img"), `${link.id}: no raw markup`);
    assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;&amp;&quot;&#39;"), `${link.id}: the value is escaped`);
  }
});

test("an inventory of another enclosure gives no serials to this one", () => {
  const T = H.loadTopology();
  const { fabric, snapshot } = cases.core_cascade;
  assert.deepEqual([...T.searchTopology(T.buildTopologyModel(fabric, { inventory: snapshot }), "SANITIZED-TOPO-3").matches].map((item) => item.id), ["bay:3"]);
  const other = { ...snapshot, selected_enclosure_id: "another-enclosure" };
  assert.equal(T.searchTopology(T.buildTopologyModel(fabric, { inventory: other }), "SANITIZED-TOPO-3").matches.length, 0);
});

test("a bay number or level written with a sign or a decimal point is not read as a number", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const enc = H.addEnclosure(fabric, "mpr0", "0001", "SYNTH-ENC", null);
  fabric.links.push(H.link(hba.id, enc.id, "controller-enclosure"));
  H.addBays(fabric, H.range(0, 4), [enc.id]);
  const labels = ["Bay +1", "Bay 1.0", "Bay -1", "Bay 1e0"];
  labels.forEach((label, index) => { fabric.nodes.find((item) => item.id === `bay:${index}`).label = label; });
  for (const [index, level] of ["+1", "1.0", "-1"].entries()) {
    H.addExpander(fabric, "mpr0", 0xb0 + index, level, hba.id);
  }
  const inventory = { selected_system_id: fabric.system_id, selected_enclosure_id: fabric.selected_enclosure_id, slots: H.range(0, 4).map((slot) => ({ slot, slot_label: ["+1", "1.0", "-1", "1e0"][slot] })) };
  const model = T.buildTopologyModel(fabric, { inventory });
  for (const bay of model.bays) assert.equal(bay.number, "", `${bay.label} has no number`);
  assert.equal([...T.searchTopology(model, "1").matches].filter((item) => item.kind === "bay").length, 0);
  for (const item of [...model.nodes].filter((node) => node.kind === "expander")) {
    assert.equal(item.level, null, item.id);
    assert.ok(item.facts.includes("level not reported"), item.id);
  }
});

test("of two bay traces with one id the first is kept, and the notice says so", () => {
  const T = H.loadTopology();
  const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  const first = fabric.traces.find((trace) => trace.id === "bay:1");
  const second = JSON.parse(JSON.stringify(first));
  for (const state of second.metrics.path_states) state.state = "fail";
  fabric.traces.push(second);
  const model = T.buildTopologyModel(fabric);
  assert.deepEqual(model.bayById.get("bay:1").pathStates.map((state) => state.state), first.metrics.path_states.map((state) => state.state));
  assert.match([...model.notices].join(" "), /1 duplicate trace id \(bay:1\); the first trace of each id was kept/);
  // In the other order the other trace is the first one.
  const reversed = JSON.parse(JSON.stringify(fabric));
  const index = reversed.traces.findIndex((trace) => trace.id === "bay:1");
  const [kept] = reversed.traces.splice(index, 1);
  reversed.traces.push(kept);
  assert.deepEqual(T.buildTopologyModel(reversed).bayById.get("bay:1").pathStates.map((state) => state.state), second.metrics.path_states.map((state) => state.state));
});

// ---- the drawing's scroll position across the page's own redraws ----------------

// The page around the map panel, as the browser keeps it: the document is the
// content above the panel, the panel down to the scroll box's bottom border
// and the content below it, and never shorter than the window. Whenever that
// leaves a smaller scroll range the page's scroll position is held back to it;
// window.scrollTo is held back the same way. When the document grows again the
// browser's scroll anchoring takes the page back towards the position it was
// held back from, but not from the top, where nothing is anchored. Measured
// in the browser on a collapse: the page went from 506 to 45 px and, as the
// box grew, to 485 px at 2560x1440; from 190 to 0 px, where it stayed while
// the box grew, at 3840x2160. `anchoring: false` leaves only the clamp.
function fakePage({ viewport, above, below, anchoring = true }) {
  const page = { innerHeight: viewport, above, below, scrollX: 0, scrollY: 0, panelHeight: () => 0, scrolls: [], windows: [], reads: 0 };
  let wanted = 0;
  page.height = () => Math.max(page.innerHeight, page.above + page.panelHeight() + page.below);
  page.range = () => Math.max(0, page.height() - page.innerHeight);
  // The document's root element, as a script reads the page's length; every
  // read is counted.
  page.root = {
    get scrollHeight() { page.reads += 1; return page.height(); },
    get clientHeight() { page.reads += 1; return page.innerHeight; },
  };
  page.layout = () => {
    page.scrollY = Math.min(wanted, page.range());
    if (!anchoring || page.scrollY === 0) wanted = page.scrollY;
  };
  page.scrollTo = (x, y) => {
    const call = { to: Number(y) || 0, from: page.scrollY };
    wanted = Math.max(0, call.to);
    page.layout();
    page.scrolls.push({ ...call, accepted: page.scrollY });
  };
  // A script's window reads and scrolls this page.
  page.bind = (win) => {
    const read = (name) => ({ get: () => page[name], configurable: true, enumerable: true });
    Object.defineProperties(win, { scrollX: read("scrollX"), scrollY: read("scrollY"), innerHeight: read("innerHeight") });
    win.scrollTo = page.scrollTo;
    page.windows.push(win);
    return win;
  };
  return page;
}

// The page's map panel with a real-shaped scroll box: replacing the panel's
// HTML makes a new box at offset 0 that clamps its offsets to its drawing's
// current size (the drawing's attributes may be changed after it is put in
// place), and sits lower on the page for every notice above it, more so when
// a notice wraps onto more lines in a narrow panel, and for every row of
// legend entries: 15 px rows 6 px apart, entries packed by a width per
// character chosen to give the rows a browser measured for the 1,024-bay
// shelf (57 px drawn in full and 36 px as a summary in a 1,132 px box,
// 36 px both ways in a 1,452 px box).
// Like the page's box, which has only a maximum height, the box is as tall as
// its drawing up to `height`: growing the drawing after it is in place grows
// the box. With `page` (a fakePage), the page around the box scrolls too.
// `height` may be a function, for a window whose height changes.
function scrollingPanel(item, { inner = 1100, height: limit = 600, page = null } = {}) {
  let html = "";
  const pin = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pin : null), querySelectorAll: () => [] };
  const lines = (text) => Math.max(1, Math.ceil((text.length * 7) / Math.max(1, inner - 20)));
  const legendHeight = (markup) => {
    const legend = /<div class="fabric-topology-legend"[^>]*>([\s\S]*?)<\/div>/.exec(markup)?.[1] || "";
    const widths = legend.split("data-topology-legend=").slice(1).map((part) => 34 + 3.3 * part.replace(/^[^>]*>/, "").split(/<[^>]+>/).join("").length);
    let rows = widths.length ? 1 : 0;
    let used = 0;
    for (const width of widths) {
      if (used && used + 12 + width > inner) {
        rows += 1;
        used = 0;
      }
      used += (used ? 12 : 0) + width;
    }
    return rows ? 21 * rows - 6 : 0;
  };
  const box = (markup) => {
    const tag = /<svg class="fabric-topology-svg[^"]*"[^>]*>/.exec(markup)?.[0] || "";
    const attributes = Object.fromEntries([...tag.matchAll(/ (width|height|viewBox)="([^"]*)"/g)].map((match) => [match[1], match[2]]));
    const svg = {
      getAttribute: (name) => attributes[name] ?? null,
      setAttribute: (name, value) => {
        attributes[name] = String(value);
        page?.layout();
      },
    };
    const above = markup.slice(0, markup.indexOf("fabric-topology-scroll"));
    const notices = [...above.matchAll(/<div class="warning-item[^"]*">([^<]*)<\/div>/g)].map((match) => match[1]);
    const bar = /<div class="fabric-topology-bar">([\s\S]*?)<\/div>/.exec(above)?.[1].replace(/<[^>]+>/g, " ") || "";
    const top = 100 + legendHeight(above) + notices.reduce((sum, text) => sum + 26 + 18 * lines(text), 0) + 12 + 18 * lines(bar);
    let left = 0;
    let down = 0;
    const client = () => Math.min(typeof limit === "function" ? limit() : limit, Number(attributes.height) || 0);
    const extent = (name, size) => Math.max(0, (Number(attributes[name]) || 0) - size);
    // The box's part of the document: what sits above it in the panel, the
    // box itself and its two 1 px borders.
    if (page) page.panelHeight = () => top + client() + 2;
    const rect = () => {
      const y = page ? page.above + top - page.scrollY : top;
      return { left: 20, top: y, right: 20 + inner, bottom: y + client(), width: inner, height: client() };
    };
    return {
      clientWidth: inner,
      get clientHeight() { return client(); },
      svg,
      querySelector: (selector) => (selector === ".fabric-topology-svg" || selector === "svg" ? svg : null),
      // A drawing made smaller after an offset was set clamps it, as the browser does.
      get scrollLeft() { return Math.min(left, extent("width", inner)); },
      set scrollLeft(value) { left = Math.max(0, Math.min(extent("width", inner), Number(value) || 0)); },
      get scrollTop() { return Math.min(down, extent("height", client())); },
      set scrollTop(value) { down = Math.max(0, Math.min(extent("height", client()), Number(value) || 0)); },
      getBoundingClientRect: rect,
    };
  };
  item.clientWidth = inner + 2;
  item.box = null;
  Object.defineProperty(item, "innerHTML", {
    get: () => html,
    set(value) {
      html = value;
      item.box = value.includes("fabric-topology-scroll") ? box(value) : null;
      if (page && !item.box) page.panelHeight = () => 0;
      page?.layout();
    },
  });
  item.querySelector = (selector) => {
    if (selector === ".fabric-topology-scroll") return item.box;
    if (selector === ".fabric-topology") return item.box ? root : null;
    if (selector === "[data-topology-fit]") return { focus() {} };
    return null;
  };
}

// An element that answers closest() for the attributes it carries.
function pageTarget(attributes, root) {
  const element = {
    dataset: Object.fromEntries(Object.entries(attributes).map(([name, value]) => [name.replace(/^data-/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value])),
    getAttribute: (name) => attributes[name] ?? null,
    hasAttribute: (name) => name in attributes,
    closest(selector) {
      for (const part of selector.split(",").map((item) => item.trim())) {
        if (part === ".fabric-topology") return root;
        const match = /^\[([a-z-]+)(?:="([^"]*)")?\]$/.exec(part);
        if (match && match[1] in attributes && (match[2] === undefined || attributes[match[1]] === match[2])) return element;
      }
      return null;
    },
  };
  element.parentElement = element;
  return element;
}

// Where a point of the drawing is on the screen: the box's place on the page,
// the drawing's own origin (its viewBox) and both scroll offsets.
function screenPoint(panel, item) {
  const rect = panel.box.getBoundingClientRect();
  const [originX, originY] = String(panel.box.svg.getAttribute("viewBox") || "0 0").split(/\s+/).map(Number);
  return [rect.left + item.x - originX - panel.box.scrollLeft, rect.top + item.y - originY - panel.box.scrollTop];
}

function expandControls(panel) {
  const match = /<g class="topo-expand"[^>]*data-fabric-expand-slots="([^"]+)"[^>]*aria-expanded="(true|false)"[^>]*><rect x="(-?[\d.]+)" y="(-?[\d.]+)"/g;
  return [...panel.innerHTML.matchAll(match)].map((item) => ({ key: item[1], expanded: item[2], x: Number(item[3]), y: Number(item[4]) }));
}

// The whole page script on a panel of the given inner width and height,
// in Topology, with clicks sent through the panel's and the document's
// real handlers.
// With `size.page` (a fakePage), both scripts share that page as their window.
function topologyPage(fabric, size) {
  const dom = fakeDom();
  if (size.page) dom.document.documentElement = size.page.root;
  const T = H.loadTopology(size.page ? { document: dom.document, window: size.page.bind({}) } : { document: dom.document });
  const snapshot = { selected_system_id: fabric.system_id, selected_enclosure_id: fabric.selected_enclosure_id, slots: [] };
  let panel = null;
  const { page, elements, click: mode, sandbox } = loadFullPage(T, dom.document, fabric, snapshot, (id, item) => {
    if (id === "fabric-map-panel") {
      panel = item;
      scrollingPanel(item, size);
    }
  }, size.page || null);
  Object.assign(page.state, { fabricScopeReady: true, loading: false, selectedSystemId: fabric.system_id, selectedEnclosureId: fabric.selected_enclosure_id, mode: "topology", fabric });
  page.render();
  const documentClick = dom.documentHandlers.find((item) => item.name === "click").handler;
  const click = (attributes) => {
    const target = pageTarget(attributes, panel.querySelector(".fabric-topology"));
    panel.handlers.get("click")({ type: "click", target });
    documentClick({ type: "click", target });
  };
  return { T, page, elements, panel, click, mode, sandbox, snapshot };
}

test("a redraw from pinning, selecting, searching, Fit or Actual size keeps both scroll offsets, and an expanded or collapsed shelf keeps its control in place", () => {
  const dom = fakeDom();
  const T = H.loadTopology({ document: dom.document });
  const { fabric } = H.fourShelves();
  const snapshot = { selected_system_id: fabric.system_id, selected_enclosure_id: fabric.selected_enclosure_id, slots: [] };
  let panel = null;
  const { page, elements } = loadFullPage(T, dom.document, fabric, snapshot, (id, item) => {
    if (id === "fabric-map-panel") {
      panel = item;
      scrollingPanel(item);
    }
  });
  Object.assign(page.state, { fabricScopeReady: true, loading: false, selectedSystemId: fabric.system_id, selectedEnclosureId: fabric.selected_enclosure_id, mode: "topology", fabric });
  page.render();
  const root = panel.querySelector(".fabric-topology");
  const documentClick = dom.documentHandlers.find((item) => item.name === "click").handler;
  const click = (attributes) => {
    const target = pageTarget(attributes, root);
    panel.handlers.get("click")({ type: "click", target });
    documentClick({ type: "click", target });
  };
  const offsets = () => [panel.box.scrollLeft, panel.box.scrollTop];
  panel.box.scrollLeft = 700;
  panel.box.scrollTop = 150;
  assert.deepEqual(offsets(), [700, 150], "the drawing scrolls both ways");
  // Selecting a bay: the page selects it and redraws.
  click({ "data-topo-bay": "bay:10", "data-fabric-trace": "bay:10" });
  assert.equal(page.state.selectedTraceId, "bay:10");
  assert.deepEqual(offsets(), [700, 150], "selecting a bay");
  // Pinning a link: the inspector shows it and the page redraws.
  const link = [...T.drawnModel().links].find((item) => item.kind === "controller-expander");
  click({ "data-topo-link": link.id, "data-fabric-topology-link": link.id });
  assert.ok(T.inspectorDetail(), "the link is pinned");
  assert.deepEqual(offsets(), [700, 150], "pinning a link");
  // Searching clears the pin and redraws.
  const search = elements.get("fabric-topology-search");
  search.value = "bay 3";
  search.handlers.get("input")();
  assert.equal(T.inspectorDetail(), null);
  assert.deepEqual(offsets(), [700, 150], "searching");
  // Fit keeps the offsets the fitted drawing allows; Actual size returns to them.
  click({ "data-topology-fit": "fit" });
  assert.match(panel.innerHTML, /is-fitted/);
  click({ "data-topology-fit": "actual" });
  assert.doesNotMatch(panel.innerHTML, /is-fitted/);
  assert.deepEqual(offsets(), [700, 150], "Fit, then Actual size");
  // Expanding, then collapsing, a shelf: its control stays where it was on screen.
  const control = () => {
    const match = /<g class="topo-expand"[^>]*data-fabric-expand-slots="([^"]+)"[^>]*aria-expanded="(true|false)"[^>]*><rect x="(-?[\d.]+)" y="(-?[\d.]+)"/g;
    return [...panel.innerHTML.matchAll(match)].map((item) => ({ key: item[1], expanded: item[2], x: Number(item[3]), y: Number(item[4]) }));
  };
  const onScreen = (item) => screenPoint(panel, item);
  const [first] = control();
  assert.ok(first, "a collapsed shelf has a control");
  for (const step of ["expand", "collapse"]) {
    const before = control().find((item) => item.key === first.key);
    panel.box.scrollLeft = before.x - 400;
    panel.box.scrollTop = before.y - 300;
    const [left, top] = offsets();
    assert.ok(left > 0 && top > 0, `${step}: both offsets are not zero (${left}, ${top})`);
    assert.ok(before.x >= left && before.x + 160 <= left + 1100 && before.y >= top && before.y + 22 <= top + 600, `${step}: the control is in view`);
    const was = onScreen(before);
    click({ "data-fabric-expand-slots": first.key });
    const after = control().find((item) => item.key === first.key);
    assert.equal(after.expanded, step === "expand" ? "true" : "false");
    const now = onScreen(after);
    assert.ok(Math.abs(now[0] - was[0]) <= 1 && Math.abs(now[1] - was[1]) <= 1, `${step}: the control moved from ${was} to ${now}`);
  }
});

// One dual-pathed shelf of 1,024 bays behind two HBAs.
function bigShelf() {
  const fabric = H.coreFabric();
  const enclosures = [];
  for (const [side, name] of ["mpr0", "mpr1"].entries()) {
    const hba = H.addController(fabric, name);
    const exp = H.addExpander(fabric, name, 0xf100 + side, 1, hba.id, { reported_lanes: H.lanes([0, 1, 2, 3], [32, 33, 34, 35]) });
    enclosures.push(H.addEnclosure(fabric, name, "0x0001", `SYNTH-JBOD-${side ? "B" : "A"}`, exp.id).id);
  }
  H.addBays(fabric, H.range(0, 1024), enclosures, [{ controller: "mpr0", state: "active" }, { controller: "mpr1", state: "passive" }]);
  return fabric;
}

test("a visible expand control stays at its place on the screen also next to the drawing's left or top edge", () => {
  const cases = [
    // A 1,100 px box that clamps its offsets; the shelf's control starts near the left edge.
    { name: "1,024-bay shelf, 1,100 px", fabric: bigShelf(), size: { inner: 1100, height: 600 } },
    // The four-shelf system at 390 and at 3,840 px pages: the boxes' inner sizes there.
    { name: "four shelves, 390 px", fabric: H.fourShelves().fabric, size: { inner: 322, height: 658 } },
    { name: "four shelves, 3,840 px", fabric: H.fourShelves().fabric, size: { inner: 3372, height: 1684 } },
  ];
  const moved = [];
  const checked = new Map();
  for (const { name, fabric, size } of cases) {
    const keys = expandControls(topologyPage(fabric, size).panel).map((item) => item.key);
    assert.ok(keys.length, `${name}: a collapsed shelf has a control`);
    for (const key of keys) {
      // Each shelf from two starting views: the drawing's origin, where the
      // control is in view there, and the control at the box's top left corner.
      for (const start of ["origin", "corner"]) {
        const { panel, click } = topologyPage(fabric, size);
        const first = expandControls(panel).find((item) => item.key === key);
        if (start === "origin") {
          panel.box.scrollLeft = 0;
          panel.box.scrollTop = 0;
          if (first.x + 160 > size.inner || first.y + 22 > size.height) continue;
        } else {
          panel.box.scrollLeft = first.x - 4;
          panel.box.scrollTop = first.y - 4;
        }
        for (const step of ["expand", "collapse"]) {
          const before = expandControls(panel).find((item) => item.key === key);
          const was = screenPoint(panel, before);
          const rect = panel.box.getBoundingClientRect();
          assert.ok(was[0] >= rect.left && was[0] + 160 <= rect.left + size.inner && was[1] >= rect.top && was[1] + 22 <= rect.top + size.height,
            `${name}, ${key}, ${start}, ${step}: the control is in view at ${was}`);
          click({ "data-fabric-expand-slots": key });
          checked.set(name, (checked.get(name) || 0) + 1);
          const after = expandControls(panel).find((item) => item.key === key);
          assert.equal(after.expanded, step === "expand" ? "true" : "false");
          const now = screenPoint(panel, after);
          if (Math.abs(now[0] - was[0]) > 1 || Math.abs(now[1] - was[1]) > 1) moved.push(`${name}, ${key}, ${start}, ${step}: from ${was} to ${now}`);
        }
      }
    }
  }
  assert.deepEqual([...checked.keys()], cases.map((item) => item.name), "every case expands and collapses a visible control");
  assert.deepEqual(moved, [], "every control stays in place");
  // The room added before the drawing's left edge goes on the first redraw
  // whose view no longer needs it, and nothing on the screen moves then.
  const { panel, click } = topologyPage(bigShelf(), { inner: 1100, height: 600 });
  const [shelf] = expandControls(panel);
  panel.box.scrollLeft = 0;
  panel.box.scrollTop = 0;
  click({ "data-fabric-expand-slots": shelf.key });
  const origin = () => String(panel.box.svg.getAttribute("viewBox")).split(" ").slice(0, 2).map(Number);
  assert.ok(origin()[0] < 0, `room before the left edge: ${origin()}`);
  panel.box.scrollLeft += 400;
  const held = screenPoint(panel, expandControls(panel)[0]);
  click({ "data-topo-bay": "bay:1", "data-fabric-trace": "bay:1" });
  assert.deepEqual(origin(), [0, 0], "no room once the view does not need it");
  assert.deepEqual(screenPoint(panel, expandControls(panel)[0]), held, "the redraw moves nothing");
  click({ "data-fabric-expand-slots": shelf.key });
  assert.deepEqual(origin(), [0, 0]);
});

test("the trace line sits below the drawing, so hover text that wraps cannot move a lane label from under the pointer", () => {
  const dom = fakeDom();
  const T = H.loadTopology({ document: dom.document });
  T.attach(dom.container, dom.search, dom.status, {});
  const fabric = JSON.parse(JSON.stringify(cases.core_cascade.fabric));
  const link = [...T.buildTopologyModel(fabric, { width: 322 }).links].find((item) => !item.bundle);
  const unpinned = T.render(fabric, { width: 324 });
  dom.handlers.get("click")({ target: dom.element({ topoLink: link.id }) });
  assert.ok(T.inspectorDetail(), "the lane is pinned");
  const pinned = T.render(fabric, { width: 324 });
  for (const [name, html] of [["unpinned", unpinned], ["pinned", pinned]]) {
    assert.equal((html.match(/data-topology-pin/g) || []).length, 1, `${name}: one trace line`);
    const bar = /<div class="fabric-topology-bar">[\s\S]*?<\/div>/.exec(html)[0];
    assert.doesNotMatch(bar, /data-topology-pin/, `${name}: not in the bar above the drawing`);
    assert.ok(html.indexOf("data-topology-pin") > html.indexOf("fabric-topology-scroll"), `${name}: after the drawing's scroll box`);
  }
  assert.match(pinned, /data-topology-pin>Pinned: /);
});

// Collapsing a shelf drawn in full while its control is in view: the shorter
// drawing makes the box shorter until mount adds room after it, the legend
// above may lose a row, and on a tall page the page itself is held back while
// the shorter drawing is in place. The control stays where it was.
test("a collapsed shelf's control stays in place when the redraw changes the box's height, the legend above it or the page's scroll", () => {
  const cases = [
    // 1,600 x 900: the legend goes from 57 to 36 px and the box from 700 to
    // 681 px until the room is added; offset 236 must become 215.
    { name: "1,024 bays, 1600x900", fabric: bigShelf, size: { inner: 1132, height: 700 }, top: 236, legend: -21, wanted: 215 },
    // 1,920 x 900: the legend keeps its rows; the box still shrinks for a moment.
    { name: "1,024 bays, 1920x900", fabric: bigShelf, size: { inner: 1452, height: 700 }, top: 236, legend: 0, wanted: 236 },
    // 1,920 x 700: the collapsed drawing (681 px) is taller than the box (544 px).
    { name: "1,024 bays, 1920x700", fabric: bigShelf, size: { inner: 1452, height: 544 }, top: 314, legend: 0, wanted: 314 },
    // 1,600 x 1,080 with the page scrolled 592 px: while the 681 px drawing is
    // in place the page can only scroll 443 px, so the box sits 149 px lower
    // until the room is added.
    { name: "1,024 bays, 1600x1080, page scrolled", fabric: bigShelf, size: { inner: 1132, height: 840, page: fakePage({ above: 480, below: 150, viewport: 1080 }) }, scrollY: 592, top: 166, legend: -21, wanted: 145 },
    // The four-shelf system: its other shelves keep the legend's rows.
    { name: "four shelves, 1600x900", fabric: () => H.fourShelves().fabric, size: { inner: 1132, height: 700 }, top: 400, legend: 0, wanted: 400 },
  ];
  const moved = [];
  for (const { name, fabric, size, scrollY, top, legend, wanted } of cases) {
    const { panel, click } = topologyPage(fabric(), size);
    const [shelf] = expandControls(panel);
    panel.box.scrollLeft = Math.max(0, shelf.x - 200);
    panel.box.scrollTop = 0;
    click({ "data-fabric-expand-slots": shelf.key });
    const before = expandControls(panel).find((item) => item.key === shelf.key);
    assert.equal(before.expanded, "true", `${name}: drawn in full`);
    panel.box.scrollTop = top - Number(String(panel.box.svg.getAttribute("viewBox")).split(" ")[1]);
    if (size.page) size.page.scrollTo(0, scrollY);
    const rect = panel.box.getBoundingClientRect();
    const was = screenPoint(panel, before);
    assert.ok(was[0] >= rect.left && was[0] + 160 <= rect.right && was[1] >= rect.top && was[1] + 22 <= rect.bottom, `${name}: the control is in view at ${was}`);
    click({ "data-fabric-expand-slots": shelf.key });
    const after = expandControls(panel).find((item) => item.key === shelf.key);
    assert.equal(after.expanded, "false", `${name}: a summary again`);
    const now = screenPoint(panel, after);
    if (!size.page) assert.equal(panel.box.getBoundingClientRect().top - rect.top, legend, `${name}: the box moves with the legend's height`);
    const offset = panel.box.scrollTop + Number(String(panel.box.svg.getAttribute("viewBox")).split(" ")[1]);
    if (now[0] !== was[0] || now[1] !== was[1] || offset !== wanted) moved.push(`${name}: from ${was} to ${now}, offset ${offset} for ${wanted}`);
  }
  assert.deepEqual(moved, [], "every control stays in place");
});

// The redraw needs room before the drawing's top edge: the box moves up by
// the legend row the collapse removes, with the drawing's offset at 0.
test("a control kept in place above the drawing's top edge gets room there, and the room goes once no view needs it", () => {
  const { panel, click } = topologyPage(bigShelf(), { inner: 1132, height: 700 });
  const [shelf] = expandControls(panel);
  const origin = () => String(panel.box.svg.getAttribute("viewBox")).split(" ").slice(0, 2).map(Number);
  panel.box.scrollLeft = 0;
  panel.box.scrollTop = 0;
  click({ "data-fabric-expand-slots": shelf.key });
  panel.box.scrollTop = -origin()[1];
  const before = expandControls(panel)[0];
  const was = screenPoint(panel, before);
  assert.ok(was[1] >= panel.box.getBoundingClientRect().top, `the control is in view at ${was}`);
  click({ "data-fabric-expand-slots": shelf.key });
  assert.equal(origin()[1], -21, "21 px of room before the drawing's top edge");
  assert.deepEqual(screenPoint(panel, expandControls(panel)[0]), was, "collapsing moves nothing");
  // Drawn in full again: the view no longer needs that room, and the control stays.
  click({ "data-fabric-expand-slots": shelf.key });
  assert.equal(origin()[1], 0, "no room before the top edge once the view does not need it");
  assert.deepEqual(screenPoint(panel, expandControls(panel)[0]), was, "expanding again moves nothing");
});

// At 390 px a pin that added the traced-path entry made the legend a row
// taller and moved the drawing down under the pointer.
test("hover, focus, search and a pin never change the legend's entries, so the drawing does not move", () => {
  const T = H.loadTopology();
  for (const [name, fabric] of [["four shelves", H.fourShelves().fabric], ["1,024-bay shelf", bigShelf()], ["cascade", cases.core_cascade.fabric]]) {
    const model = T.buildTopologyModel(fabric, { width: 322 });
    const legend = (view) => /<div class="fabric-topology-legend"[^>]*>[\s\S]*?<\/div>/.exec(T.renderTopology(model, view))[0];
    const idle = legend({});
    const link = [...model.links].find((item) => !item.bundle);
    const bay = [...model.bays][0];
    assert.ok(link && bay, name);
    const query = bay.number === "" ? bay.label : `bay ${bay.number}`;
    assert.ok(T.searchTopology(model, query).matches.length, `${name}: "${query}" finds a bay`);
    const views = {
      "hover or focus on a line": { hover: { kind: "link", id: link.id } },
      "hover or focus on a bay": { hover: { kind: "bay", id: bay.bayId } },
      "a pinned line": { pin: { kind: "link", id: link.id } },
      "a pinned bay": { pin: { kind: "bay", id: bay.bayId } },
      "a search": { query },
    };
    for (const [what, view] of Object.entries(views)) {
      assert.match(drawing(T.renderTopology(model, view)), /is-on-path/, `${name}, ${what}: something is traced`);
      assert.equal(legend(view), idle, `${name}, ${what}: the same legend`);
    }
    assert.match(idle, /data-topology-legend="is-on-path"/, `${name}: the traced-path entry is listed before anything is traced`);
  }
  // Through the page at 390 px: pinning a line keeps the box where it was.
  const { panel, click } = topologyPage(cases.core_cascade.fabric, { inner: 322, height: 658 });
  const legendOf = () => /<div class="fabric-topology-legend"[^>]*>[\s\S]*?<\/div>/.exec(panel.innerHTML)[0];
  const before = legendOf();
  const top = panel.box.getBoundingClientRect().top;
  const [link] = [...H.loadTopology().buildTopologyModel(cases.core_cascade.fabric, { width: 322 }).links].filter((item) => !item.bundle);
  click({ "data-topo-link": link.id, "data-fabric-topology-link": link.id });
  assert.match(panel.innerHTML, /data-topology-pin>Pinned: /, "the line is pinned");
  assert.equal(legendOf(), before, "the pin leaves the legend as it was");
  assert.equal(panel.box.getBoundingClientRect().top, top, "and the drawing's box where it was");
});

// ---- the shelf control and the page's own scroll position ---------------------

// What a browser measured around the 1,024-bay shelf at eight window sizes:
// the box's inner width and height limit, its top on the page with the shelf
// as a summary (`summaryTop`) and drawn in full (`fullTop`), the page below
// it, and where the page was scrolled before an expand and before a collapse.
const MEASURED_WINDOWS = [
  { name: "1600x900", viewport: 900, inner: 1132, limit: 700, summaryTop: 689.625, fullTop: 710.625, below: 152.375, expandY: 625, collapseY: 611 },
  { name: "1920x900", viewport: 900, inner: 1452, limit: 700, summaryTop: 689.625, fullTop: 689.625, below: 152.375, expandY: 625, collapseY: 590 },
  { name: "1920x700", viewport: 700, inner: 1452, limit: 544, summaryTop: 689.625, fullTop: 689.625, below: 152.375, expandY: 688, collapseY: 612 },
  { name: "1600x1080", viewport: 1080, inner: 1132, limit: 840, summaryTop: 689.625, fullTop: 710.625, below: 152.375, expandY: 445, collapseY: 592 },
  { name: "1920x1080", viewport: 1080, inner: 1452, limit: 840, summaryTop: 689.625, fullTop: 689.625, below: 152.375, expandY: 445, collapseY: 571 },
  { name: "1600x1440", viewport: 1440, inner: 1132, limit: 1121, summaryTop: 689.625, fullTop: 710.625, below: 152.375, expandY: 85, collapseY: 546 },
  { name: "2560x1440", viewport: 1440, inner: 2092, limit: 1121, summaryTop: 668.625, fullTop: 689.625, below: 133.375, expandY: 45, collapseY: 506 },
  { name: "3840x2160", viewport: 2160, inner: 3372, limit: 1683, summaryTop: 668.625, fullTop: 668.625, below: 132.375, expandY: 0, collapseY: 190 },
];
// The four-shelf system at two of them; its legend keeps its rows.
const MEASURED_FOUR_SHELVES = [
  { name: "four shelves, 1600x900", viewport: 900, inner: 1132, limit: 700, summaryTop: 767.625, below: 152.375, expandY: 722, collapseY: 722 },
  { name: "four shelves, 1920x1080", viewport: 1080, inner: 1452, limit: 840, summaryTop: 746.625, below: 152.375, expandY: 661, collapseY: 661 },
];

// The whole page on one of those windows, the page's top placed where the
// browser had it. `trace` lists the page's scroll position at mount's entry
// and exit and every window.scrollTo with the step it came from.
function measuredPage(fabric, size, { anchoring = true } = {}) {
  const page = fakePage({ viewport: size.viewport, above: 0, below: size.below, anchoring });
  const view = topologyPage(fabric, { inner: size.inner, height: () => size.limit, page });
  page.above = size.summaryTop - view.panel.box.getBoundingClientRect().top;
  page.layout();
  const trace = [];
  const mount = view.T.mount;
  view.T.mount = (...args) => {
    trace.push(["mount entry", page.scrollY]);
    const from = page.scrolls.length;
    mount(...args);
    page.scrolls.slice(from).forEach((call) => trace.push(["mount scrollTo", call]));
    trace.push(["mount exit", page.scrollY]);
  };
  return { ...view, app: view.page, page, trace, mount };
}

// One expand or collapse from the start the browser measured: the control
// 24 px below the box's top or 24 px above its bottom, as far as the box
// allows, and the page where it was. Returns the control's move in the window.
function toggleFrom(view, size, key, start, y) {
  const { panel, page, click } = view;
  const before = expandControls(panel).find((item) => item.key === key);
  const origin = String(panel.box.svg.getAttribute("viewBox")).split(" ").map(Number);
  panel.box.scrollLeft = Math.max(0, before.x - origin[0] - 200);
  panel.box.scrollTop = start === "top" ? before.y - origin[1] - 24 : before.y - origin[1] + 22 + 24 - size.limit;
  page.scrollTo(0, y);
  page.scrolls.length = 0;
  view.trace.length = 0;
  const was = screenPoint(panel, before);
  const rect = panel.box.getBoundingClientRect();
  const visible = was[1] >= rect.top && was[1] + 22 <= rect.bottom && was[1] >= 0 && was[1] + 22 <= size.viewport;
  click({ "data-fabric-expand-slots": key });
  const now = screenPoint(panel, expandControls(panel).find((item) => item.key === key));
  return { visible, was, dx: now[0] - was[0], dy: now[1] - was[1], pageY: [y, page.scrollY] };
}

// Expand, then collapse, from each start: the moves that are not 0.
function shelfMoves(sizes, fabric, { anchoring = true, keys = null } = {}) {
  const moves = [];
  for (const size of sizes) {
    for (const start of ["top", "bottom"]) {
      const probe = measuredPage(fabric(), size, { anchoring });
      for (const key of keys || [expandControls(probe.panel)[0].key]) {
        const view = measuredPage(fabric(), size, { anchoring });
        for (const step of ["expand", "collapse"]) {
          const move = toggleFrom(view, size, key, start, step === "expand" ? size.expandY : size.collapseY);
          assert.ok(move.visible, `${size.name}, ${start}, ${step}: the control is in view before the click at ${move.was}`);
          if (move.dx || move.dy) moves.push(`${size.name}, ${key.slice(-6)}, ${start}, ${step}: ${move.dx}, ${move.dy}`);
        }
      }
    }
  }
  return moves;
}

test("the fake page is held back while the document is short and comes back as it grows, but not from the top", () => {
  const page = fakePage({ viewport: 1000, above: 500, below: 100 });
  let box = 1000;
  page.panelHeight = () => box;
  page.layout();
  page.scrollTo(0, 400);
  assert.equal(page.scrollY, 400);
  box = 600;
  page.layout();
  assert.equal(page.scrollY, 200, "held back to the shorter document");
  box = 1000;
  page.layout();
  assert.equal(page.scrollY, 400, "back as the document grows again");
  box = 300;
  page.layout();
  assert.equal(page.scrollY, 0);
  box = 1000;
  page.layout();
  assert.equal(page.scrollY, 0, "from the top, the page stays there");
  page.scrollTo(0, 900);
  assert.deepEqual(page.scrolls.at(-1), { to: 900, from: 0, accepted: 600 }, "scrollTo is held back the same way");
});

// The browser's page around the 1,024-bay shelf at the seven sizes where the
// control already kept its place, and the four-shelf system.
test("an expanded or collapsed shelf's control keeps its place in the window at seven measured window sizes and with four shelves", () => {
  assert.deepEqual(shelfMoves(MEASURED_WINDOWS.slice(0, 7), bigShelf), [], "every control stays in place");
  const four = measuredPage(H.fourShelves().fabric, MEASURED_FOUR_SHELVES[0]);
  const keys = expandControls(four.panel).map((item) => item.key);
  assert.equal(keys.length, 2, "two shelves can be expanded");
  assert.deepEqual(shelfMoves(MEASURED_FOUR_SHELVES, () => H.fourShelves().fabric, { keys }), [], "every four-shelf control stays in place");
  // The model's legend puts the box where the browser had it with the shelf drawn in full.
  for (const size of MEASURED_WINDOWS) {
    const view = measuredPage(bigShelf(), size);
    view.click({ "data-fabric-expand-slots": expandControls(view.panel)[0].key });
    assert.equal(view.panel.box.getBoundingClientRect().top + view.page.scrollY, size.fullTop, `${size.name}: the box's top with the shelf drawn in full`);
  }
});

// At 3840x2160 the page cannot scroll at all while the collapsed drawing is
// in place, so it drops to the top; the page's own redraw puts it back only
// after mount has aimed the control, or cannot when the box has not grown.
test("at 3840x2160 a collapsed shelf's control keeps its place in the window from the top and the bottom start", () => {
  assert.deepEqual(shelfMoves(MEASURED_WINDOWS.slice(7), bigShelf), [], "every control stays in place");
});

// Mount puts the page back before it aims; the page's redraw then puts it
// back again for every mode. Where mount's succeeded the second has nothing
// to do; where the page could not get back there either, it moves nothing.
test("mount's and the page's own page-scroll restorations do not fight", () => {
  for (const size of MEASURED_WINDOWS.slice(6)) {
    for (const start of ["top", "bottom"]) {
      const view = measuredPage(bigShelf(), size);
      const key = expandControls(view.panel)[0].key;
      toggleFrom(view, size, key, start, size.expandY);
      const move = toggleFrom(view, size, key, start, size.collapseY);
      const label = `${size.name}, ${start}`;
      assert.equal(move.dy, 0, label);
      const inMount = view.trace.filter(([step]) => step === "mount scrollTo").map(([, call]) => call);
      assert.equal(inMount.length, 1, `${label}: mount puts the page back once`);
      assert.equal(inMount[0].to, size.collapseY, label);
      const afterMount = view.page.scrolls.slice(view.page.scrolls.indexOf(view.page.scrolls.find((call) => call.to === inMount[0].to)) + 1);
      if (inMount[0].accepted === size.collapseY) assert.deepEqual(afterMount, [], `${label}: the page's own restoration has nothing left to do`);
      afterMount.forEach((call) => assert.equal(call.accepted, call.from, `${label}: the page's own restoration moves nothing`));
    }
  }
  // 3840x2160: mount's restoration succeeds, so the page makes no call of its own.
  const view = measuredPage(bigShelf(), MEASURED_WINDOWS[7]);
  const key = expandControls(view.panel)[0].key;
  toggleFrom(view, MEASURED_WINDOWS[7], key, "top", 0);
  toggleFrom(view, MEASURED_WINDOWS[7], key, "top", 190);
  assert.deepEqual(view.page.scrolls, [{ to: 190, from: 0, accepted: 190 }]);
});

// Any other caller of mount, one without a page position, gets what the
// drawing had before mount took one: the control aimed at the page as it
// is, and no page scroll. At 3840x2160 that is the move a browser measured.
test("mount without a page position aims at the page as it is and never scrolls the page", () => {
  const size = MEASURED_WINDOWS[7];
  const moves = [];
  for (const start of ["top", "bottom"]) {
    const view = measuredPage(bigShelf(), size);
    view.T.mount = (container) => {
      const from = view.page.scrolls.length;
      view.mount(container);
      assert.deepEqual(view.page.scrolls.slice(from), [], `${start}: no page scroll from mount`);
    };
    const key = expandControls(view.panel)[0].key;
    toggleFrom(view, size, key, start, size.expandY);
    moves.push(toggleFrom(view, size, key, start, size.collapseY).dy);
  }
  assert.deepEqual(moves, [176, 190]);
});

// No control to keep in place: mount never scrolls the page. Fit takes the
// offsets to what the fitted drawing allows and Actual size gives them back;
// pinning, selecting, searching and a refresh keep them, and the page stays.
test("a redraw with no shelf control to keep does not scroll the page, and Fit and Actual size still drop and restore the offsets", () => {
  const size = MEASURED_WINDOWS[4];
  const view = measuredPage(bigShelf(), size);
  const { panel, page, click, T, elements } = view;
  click({ "data-fabric-expand-slots": expandControls(panel)[0].key });
  panel.box.scrollLeft = 300;
  panel.box.scrollTop = 400;
  page.scrollTo(0, size.collapseY);
  page.scrolls.length = 0;
  view.trace.length = 0;
  // In drawing coordinates: room no view needs any more may go meanwhile.
  const offsets = () => {
    const origin = String(panel.box.svg.getAttribute("viewBox")).split(" ").map(Number);
    return [panel.box.scrollLeft + origin[0], panel.box.scrollTop + origin[1]];
  };
  const held = offsets();
  assert.ok(held[1] > 0 && page.scrollY > 0, `the drawing and the page are scrolled: ${held}, ${page.scrollY}`);
  const still = (what) => {
    assert.deepEqual(page.scrolls, [], `${what}: no page scroll`);
    assert.equal(page.scrollY, size.collapseY, `${what}: the page stays`);
    assert.deepEqual(offsets(), held, `${what}: the offsets stay`);
  };
  click({ "data-topo-bay": "bay:10", "data-fabric-trace": "bay:10" });
  still("selecting a bay");
  const link = [...T.drawnModel().links].find((item) => item.kind === "controller-expander");
  click({ "data-topo-link": link.id, "data-fabric-topology-link": link.id });
  assert.ok(T.inspectorDetail(), "the link is pinned");
  still("pinning a link");
  const search = elements.get("fabric-topology-search");
  search.value = "bay 3";
  search.handlers.get("input")();
  still("searching");
  view.app.render();
  still("a refresh");
  click({ "data-topology-fit": "fit" });
  assert.match(panel.innerHTML, /is-fitted/);
  // The fitted drawing is scaled to 78% of the window, the box's inner height
  // is that less its borders: what is left to scroll is at most those 2 px.
  const slack = [Number(panel.box.svg.getAttribute("width")) - panel.box.clientWidth, Number(panel.box.svg.getAttribute("height")) - panel.box.clientHeight];
  assert.ok(slack.every((value) => value <= 2), `the fitted drawing fits the box: ${slack}`);
  assert.deepEqual(offsets(), held.map((value, index) => Math.min(value, Math.max(0, slack[index]))), "Fit keeps only what the fitted drawing allows");
  click({ "data-topology-fit": "actual" });
  assert.deepEqual(offsets(), held, "Actual size gives them back");
  assert.deepEqual(view.trace.filter(([step]) => step === "mount scrollTo"), [], "mount scrolls the page for none of these");
});

// The page's first view of the drawing, the older modes, and the room the
// kept control needed after the drawing's bottom edge.
test("the first view, the older modes and a later view that needs no room leave the new page restoration alone", () => {
  // The page is already scrolled when Topology first draws: mount starts the
  // drawing at its top and leaves the page where it is.
  const page = fakePage({ viewport: 1080, above: 2000, below: 152.375 });
  page.scrollTo(0, 500);
  const view = topologyPage(bigShelf(), { inner: 1452, height: 840, page });
  assert.deepEqual(page.scrolls, [{ to: 500, from: 0, accepted: 500 }], "the first view scrolls nothing");
  assert.equal(page.scrollY, 500);
  assert.equal(view.panel.box.scrollTop, 0, "the drawing starts at its top");
  // The four older modes never call mount; Topology's own first view again does not scroll the page.
  const calls = [];
  const mount = view.T.mount;
  view.T.mount = (...args) => {
    calls.push(view.page.state.mode);
    mount(...args);
  };
  for (const mode of ["lanes", "impact", "trace", "disk"]) view.mode(mode);
  assert.deepEqual(calls, [], "no older mode calls mount");
  view.mode("topology");
  assert.deepEqual(calls, ["topology"]);
  assert.deepEqual(page.scrolls.slice(1).filter((call) => call.from !== call.accepted), [], "nothing moved the page");
  // After a kept control needed room after the drawing's bottom edge, the next
  // redraw whose view does not need it gives the room back where that moves
  // nothing: here once the page is at its top. Before that, with the page
  // 190 px down and less slack below it than the room, the room stays but
  // for that slack, and the page stays where it was.
  const size = MEASURED_WINDOWS[7];
  const big = measuredPage(bigShelf(), size);
  const key = expandControls(big.panel)[0].key;
  toggleFrom(big, size, key, "bottom", size.expandY);
  toggleFrom(big, size, key, "bottom", size.collapseY);
  const natural = big.T.drawnModel().height;
  const height = () => Number(big.panel.box.svg.getAttribute("height"));
  const room = height();
  assert.ok(room > natural, "room after the drawing's bottom edge");
  big.panel.box.scrollTop = 0;
  const slack = slackOf(big.page);
  assert.ok(slack < room - Math.ceil(natural), `less slack (${slack}) than room (${room - Math.ceil(natural)})`);
  big.click({ "data-topo-bay": "bay:1", "data-fabric-trace": "bay:1" });
  assert.equal(big.page.scrollY, size.collapseY, "the page stays");
  assert.equal(height(), room - slack, "the room stays but for the page's slack");
  big.page.scrollTo(0, 0);
  big.click({ "data-topo-bay": "bay:2", "data-fabric-trace": "bay:2" });
  assert.equal(big.page.scrollY, 0, "the page stays at its top");
  assert.equal(height(), Math.ceil(natural), "the room is given back");
  assert.deepEqual(big.trace.filter(([step]) => step === "mount scrollTo").length, 2, "the collapse's mount and the first selection's put the page back");
});


// ---- the redraw after a collapse ------------------------------------------------

// The 1,024-bay shelf expanded and collapsed again from one of the measured
// starts, its control kept in place; the drawing's offsets and the page are
// then left as the collapse left them.
function collapsedFrom(size, start) {
  const view = measuredPage(bigShelf(), size);
  const key = expandControls(view.panel)[0].key;
  toggleFrom(view, size, key, start, size.expandY);
  const collapse = toggleFrom(view, size, key, start, size.collapseY);
  assert.ok(collapse.visible && !collapse.dx && !collapse.dy, `${size.name}, ${start}: the collapse keeps the control at ${collapse.was}`);
  return { ...view, key };
}

// Where the control is in the window, the page's scroll position and the
// drawing's offsets in drawing coordinates.
function heldView(view) {
  const { panel, page, key } = view;
  const origin = String(panel.box.svg.getAttribute("viewBox")).split(" ").map(Number);
  return {
    control: screenPoint(panel, expandControls(panel).find((item) => item.key === key)),
    pageY: page.scrollY,
    offsets: [panel.box.scrollLeft + origin[0], panel.box.scrollTop + origin[1]],
  };
}

function pinLink(view) {
  const link = [...view.T.drawnModel().links].find((item) => item.kind === "controller-expander");
  view.click({ "data-topo-link": link.id, "data-fabric-topology-link": link.id });
}

// The page's refresh button, answered with the same inventory and fabric again.
async function refreshSame(view, fabric) {
  const data = JSON.stringify(fabric);
  const inventory = JSON.stringify(view.snapshot);
  view.sandbox.fetch = async (url) => ({ ok: true, status: 200, json: async () => JSON.parse(String(url).includes("/api/sas-fabric") ? data : inventory) });
  view.elements.get("fabric-refresh-button").handlers.get("click")();
  for (let turn = 0; turn < 20 && view.app.state.loading; turn += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(view.app.state.loading, false, "the refresh finished");
}

// The box's height and the drawing's, room included.
function sized(view) {
  return { box: view.panel.box.clientHeight, drawing: Number(view.panel.box.svg.getAttribute("height")) };
}

// What the page can lose below its scroll position before it is held back:
// everything at its top, where nothing can hold it back.
function slackOf(page) {
  return page.scrollY > 0 ? page.range() - page.scrollY : Infinity;
}

// After a collapse the box is for a moment only as tall as the collapsed
// drawing. A browser measured the next pin moving the control (top / bottom
// start) 19/0 px at 1600x900 and 1920x900, 159/126 at 1600x1080 and
// 1920x1080, 440/440 at 1600x1440 and 2560x1440 and 636/190 at 3840x2160,
// where the page also dropped to the top. A redraw that keeps the view moves
// neither the page nor the drawing, and the box gets shorter only by what
// the page can lose below its scroll position.
test("a pin, a selection, a search and a refresh after a collapse leave the shelf control and the page where they were", async () => {
  const moved = [];
  for (const size of MEASURED_WINDOWS) {
    for (const start of ["top", "bottom"]) {
      const view = collapsedFrom(size, start);
      const steps = {
        "pin": () => pinLink(view),
        "selection": () => view.click({ "data-topo-bay": "bay:1", "data-fabric-trace": "bay:1" }),
        "search": () => {
          const search = view.elements.get("fabric-topology-search");
          search.value = "bay 3";
          search.handlers.get("input")();
        },
        "refresh": () => refreshSame(view, view.app.state.fabric),
      };
      for (const [step, act] of Object.entries(steps)) {
        const before = heldView(view);
        const box = sized(view).box;
        const slack = slackOf(view.page);
        await act();
        const after = heldView(view);
        const lost = box - sized(view).box;
        if (JSON.stringify(after) !== JSON.stringify(before) || lost > slack) {
          moved.push(`${size.name}, ${start}, ${step}: control ${after.control[0] - before.control[0]}/${after.control[1] - before.control[1]}, page ${before.pageY}>${after.pageY}, offsets ${before.offsets}>${after.offsets}, box ${lost} px shorter for ${slack} px of slack`);
        }
      }
      assert.match(view.panel.innerHTML, /aria-expanded="false"/, `${size.name}, ${start}: still collapsed`);
    }
  }
  assert.deepEqual(moved, [], "nothing moves");
});

// Room after the drawing goes as far as the page lets it: all of it where
// the page's slack covers it, exactly the slack where that is smaller, and
// none for a view below the drawing's top edge, which needs the box at its
// height to stay reachable.
test("after a collapse the room after the drawing goes as far as the page's slack allows, and no further", () => {
  const results = {};
  const cases = [
    // The 3840x2160 page: its slack under 190 px is smaller than the room.
    ["part, 3840x2160 bottom", MEASURED_WINDOWS[7], "bottom"],
    // A page long enough below the panel: the slack covers the room.
    ["all, 1920x1080 bottom, long page", { ...MEASURED_WINDOWS[4], below: 3000 }, "bottom"],
    ["all, 1600x1440 bottom, long page", { ...MEASURED_WINDOWS[5], below: 3000 }, "bottom"],
    // The view lies 551 px below the drawing's top edge: nothing goes.
    ["none, 1920x1080 top, long page", { ...MEASURED_WINDOWS[4], below: 3000 }, "top"],
  ];
  for (const [name, size, start] of cases) {
    const view = collapsedFrom(size, start);
    const before = { held: heldView(view), ...sized(view), slack: slackOf(view.page) };
    const natural = Math.ceil(view.T.drawnModel().height);
    const origin = String(view.panel.box.svg.getAttribute("viewBox")).split(" ").map(Number);
    const room = before.drawing - Math.max(natural, before.held.offsets[1] + before.box) - -origin[1];
    pinLink(view);
    assert.deepEqual(heldView(view), before.held, `${name}: nothing moves`);
    const after = sized(view);
    results[name] = { released: before.drawing - after.drawing, room: before.drawing - natural - -origin[1], slack: Number.isFinite(before.slack) ? before.slack : "page at its top" };
    assert.equal(room, 0, `${name}: the drawing was sized exactly for the box before the pin`);
    assert.equal(before.box - after.box, results[name].released, `${name}: the box shrinks with the drawing`);
  }
  for (const [name, { released, room, slack }] of Object.entries(results)) {
    assert.ok(room > 0, `${name}: there is room after the drawing (${room})`);
    if (name.startsWith("part")) {
      assert.ok(slack > 0 && slack < room, `${name}: the slack (${slack}) is smaller than the room (${room})`);
      assert.equal(released, slack, `${name}: exactly the slack goes`);
    } else if (name.startsWith("all")) {
      assert.ok(slack >= room, `${name}: the slack (${slack}) covers the room (${room})`);
      assert.equal(released, room, `${name}: all of the room goes`);
    } else {
      assert.ok(slack > 0, `${name}: the page has slack (${slack})`);
      assert.equal(released, 0, `${name}: nothing goes`);
    }
  }
});

// The first view sizes the drawing for itself; a pin then keeps that size.
test("a pin after the first view leaves the box at the drawing's natural height, sized once and without reading the page's length", () => {
  for (const size of [MEASURED_WINDOWS[0], MEASURED_WINDOWS[4], MEASURED_WINDOWS[7]]) {
    const view = measuredPage(bigShelf(), size);
    const natural = Math.ceil(view.T.drawnModel().height);
    const first = sized(view);
    assert.deepEqual(first, { box: Math.min(natural, size.limit), drawing: natural }, `${size.name}: the first view`);
    const heights = [];
    const reads = view.page.reads;
    const mount = view.T.mount;
    view.T.mount = (...args) => {
      const own = view.panel.box.svg;
      let count = 0;
      const write = own.setAttribute;
      own.setAttribute = (name, value) => {
        if (name === "height") count += 1;
        write(name, value);
      };
      mount(...args);
      heights.push(count);
    };
    pinLink(view);
    assert.deepEqual(sized(view), first, `${size.name}: the pin keeps the box's height`);
    assert.deepEqual(heights, [1], `${size.name}: sized once`);
    assert.equal(view.page.reads, reads, `${size.name}: the page's length is not read`);
  }
});

// The loss a browser measured repeated on each further redraw: five pins in
// a row leave every number where the first left it.
test("five pins in a row after a collapse leave every number where the first pin left it", () => {
  const crept = [];
  for (const size of MEASURED_WINDOWS) {
    for (const start of ["top", "bottom"]) {
      const view = collapsedFrom(size, start);
      pinLink(view);
      const first = { ...heldView(view), ...sized(view) };
      for (let pin = 2; pin <= 5; pin += 1) {
        pinLink(view);
        const now = { ...heldView(view), ...sized(view) };
        if (JSON.stringify(now) !== JSON.stringify(first)) crept.push(`${size.name}, ${start}, pin ${pin}: ${JSON.stringify(first)} > ${JSON.stringify(now)}`);
      }
    }
  }
  assert.deepEqual(crept, [], "nothing creeps");
});

// The window got shorter between two redraws: the box's height limit fell
// from 840 to 700 px. The next redraw keeps the view and sizes the room for
// the box as it is now, not as it was at the last redraw.
test("a redraw after the window got shorter keeps the view and sizes the room for the box's new height", () => {
  const results = [];
  for (const start of ["top", "bottom"]) {
    const size = { ...MEASURED_WINDOWS[4] };
    const view = collapsedFrom(size, start);
    pinLink(view);
    assert.ok(sized(view).box > 700, `${start}: the box taller than its new limit before (${sized(view).box})`);
    size.limit = 700;
    view.page.innerHeight = 900;
    view.page.layout();
    const before = heldView(view);
    pinLink(view);
    assert.deepEqual(heldView(view), before, `${start}: nothing moves`);
    const origin = String(view.panel.box.svg.getAttribute("viewBox")).split(" ").map(Number);
    const natural = Math.ceil(view.T.drawnModel().height);
    const { box, drawing } = sized(view);
    results.push({ start, box, room: drawing - -origin[1] - natural, tallest: Math.max(natural, Math.ceil(before.offsets[1] + 700)) - natural });
  }
  for (const { start, box, room, tallest } of results) {
    assert.ok(box <= 700, `${start}: the box within its new limit (${box})`);
    assert.ok(room <= tallest, `${start}: no room for the box's old height (${room} > ${tallest})`);
  }
  // The bottom start's view is at the drawing's top, and the page has the
  // slack for the box to go back to the drawing's own height.
  assert.deepEqual(results.map(({ start, box }) => [start, box]), [["top", 700], ["bottom", 681]]);
});

// An expand or collapse whose control was not in view keeps the drawing's
// vertical offset (and its horizontal one, tested below):
// the page and the box's height stay as they would for a pin. At the 1,920 px
// widths the legend above the box keeps its rows, so the box stays put too.
test("a collapse whose control is out of view keeps the vertical offset, the page and the box's height", () => {
  const moved = [];
  for (const size of [MEASURED_WINDOWS[1], MEASURED_WINDOWS[2], MEASURED_WINDOWS[4]]) {
    const view = measuredPage(bigShelf(), size);
    const { panel, page } = view;
    const key = expandControls(panel)[0].key;
    view.click({ "data-fabric-expand-slots": key });
    const control = expandControls(panel).find((item) => item.key === key);
    // Scrolled down past the control, and past the collapsed drawing's height.
    panel.box.scrollTop = control.y + 1000;
    page.scrollTo(0, size.collapseY);
    const rect = panel.box.getBoundingClientRect();
    const at = screenPoint(panel, control);
    assert.ok(at[1] + 22 < rect.top, `${size.name}: the control is out of view above the box`);
    const before = { pageY: page.scrollY, top: heldView({ ...view, key }).offsets[1], boxTop: rect.top };
    const box = sized(view).box;
    view.click({ "data-fabric-expand-slots": key });
    assert.match(panel.innerHTML, /aria-expanded="false"/, `${size.name}: collapsed`);
    const after = { pageY: page.scrollY, top: heldView({ ...view, key }).offsets[1], boxTop: panel.box.getBoundingClientRect().top };
    if (JSON.stringify(after) !== JSON.stringify(before) || sized(view).box !== box) moved.push(`${size.name}: ${JSON.stringify(before)} > ${JSON.stringify(after)}, box ${box} > ${sized(view).box}`);
  }
  assert.deepEqual(moved, [], "nothing moves");
});

// A browser measured the kept left offset going from -208 to 0 at 1920x900,
// 1920x700 and 1920x1080 on a collapse whose control was out of view: the
// view began in the room before the drawing's left edge, and a host moved
// 208 px in the window. Such a redraw keeps the horizontal offset too,
// room before the left edge included, and the drawing does not move.
test("a toggle whose control is out of view keeps the horizontal offset, room before the left edge included", () => {
  const moved = [];
  for (const size of [MEASURED_WINDOWS[1], MEASURED_WINDOWS[2], MEASURED_WINDOWS[4]]) {
    for (const scrolled of [0, 150]) {
      const view = measuredPage(bigShelf(), size);
      const { panel, page } = view;
      const key = expandControls(panel)[0].key;
      toggleFrom(view, size, key, "top", size.expandY);
      const control = expandControls(panel).find((item) => item.key === key);
      panel.box.scrollLeft = scrolled;
      panel.box.scrollTop = control.y + 1000;
      page.scrollTo(0, size.collapseY);
      const label = `${size.name}, ${scrolled}`;
      assert.ok(screenPoint(panel, control)[1] + 22 < panel.box.getBoundingClientRect().top, `${label}: the control is out of view`);
      const host = () => screenPoint(panel, view.T.drawnModel().nodes.find((item) => item.kind === "host"));
      const held = () => ({ offsets: heldView({ ...view, key }).offsets, host: host(), pageY: page.scrollY });
      const before = held();
      assert.equal(before.offsets[0], scrolled - 208, `${label}: the view begins before the drawing's left edge`);
      view.click({ "data-fabric-expand-slots": key });
      assert.match(panel.innerHTML, /aria-expanded="false"/, `${label}: collapsed`);
      const after = held();
      if (JSON.stringify(after) !== JSON.stringify(before)) moved.push(`${label}: ${JSON.stringify(before)} > ${JSON.stringify(after)}`);
    }
  }
  assert.deepEqual(moved, [], "nothing moves sideways");
});

// Only a redraw whose kept view needs room after the drawing for the box's
// recorded height sizes twice; one with no room that needs none sizes once
// and reads nothing of the page.
test("a redraw sizes the room once, without reading the page's length, where its kept view needs no room after the drawing", () => {
  const measure = (view, act) => {
    const counts = [];
    const reads = view.page.reads;
    const mount = view.T.mount;
    view.T.mount = (...args) => {
      let heights = 0;
      const svg = view.panel.box.svg;
      const set = svg.setAttribute;
      svg.setAttribute = (name, value) => {
        if (name === "height") heights += 1;
        set(name, value);
      };
      mount(...args);
      counts.push(heights);
    };
    act();
    view.T.mount = mount;
    return { sizings: counts, pageReads: view.page.reads - reads };
  };
  const counts = {};
  // The shelf drawn in full and scrolled well inside it.
  const full = measuredPage(bigShelf(), MEASURED_WINDOWS[4]);
  full.click({ "data-fabric-expand-slots": expandControls(full.panel)[0].key });
  full.panel.box.scrollTop = 400;
  counts["inside the drawing"] = measure(full, () => pinLink(full));
  // 1920x700: the collapsed drawing is taller than the box, scrolled inside it.
  const low = measuredPage(bigShelf(), MEASURED_WINDOWS[2]);
  low.panel.box.scrollTop = 100;
  counts["inside a drawing taller than its box"] = measure(low, () => pinLink(low));
  // After a collapse from the bottom start, the page scrolled: room is kept or released.
  const bottom = collapsedFrom(MEASURED_WINDOWS[4], "bottom");
  counts["room at the drawing's top"] = measure(bottom, () => pinLink(bottom));
  // After a collapse from the top start: the view lies below the drawing's top.
  const top = collapsedFrom(MEASURED_WINDOWS[4], "top");
  counts["room below the drawing's top"] = measure(top, () => pinLink(top));
  assert.deepEqual(counts, {
    "inside the drawing": { sizings: [1], pageReads: 0 },
    "inside a drawing taller than its box": { sizings: [1], pageReads: 0 },
    "room at the drawing's top": { sizings: [2], pageReads: 2 },
    "room below the drawing's top": { sizings: [2], pageReads: 0 },
  });
});

// Any other caller of mount, one without a page position: the drawing's
// offsets and the box's height are kept, and mount never scrolls the page.
// The page's own restoration after the redraw puts the page back here.
test("a kept view after a collapse keeps its offsets and box height and never scrolls the page when mount has no page position", () => {
  const moves = [];
  for (const size of [MEASURED_WINDOWS[0], MEASURED_WINDOWS[4], MEASURED_WINDOWS[7]]) {
    for (const start of ["top", "bottom"]) {
      const view = collapsedFrom(size, start);
      let held = null;
      view.T.mount = (container) => {
        const from = view.page.scrolls.length;
        view.mount(container);
        assert.deepEqual(view.page.scrolls.slice(from), [], `${size.name}, ${start}: no page scroll from mount`);
        held = { offsets: heldView(view).offsets, box: sized(view).box };
      };
      const before = heldView(view);
      const box = sized(view).box;
      pinLink(view);
      assert.deepEqual(held, { offsets: before.offsets, box }, `${size.name}, ${start}: mount keeps the offsets and the box's height`);
      const after = heldView(view);
      moves.push(`${size.name}, ${start}: ${after.control[0] - before.control[0]}/${after.control[1] - before.control[1]}`);
    }
  }
  assert.deepEqual(moves, ["1600x900, top: 0/0", "1600x900, bottom: 0/0", "1920x1080, top: 0/0", "1920x1080, bottom: 0/0", "3840x2160, top: 0/0", "3840x2160, bottom: 0/0"]);
});


// The room first sized for the tallest box is never left behind: when
// putting the page back throws, mount still sizes the room exactly for the
// view it set, and the error is not swallowed.
test("the room first sized for the tallest box goes also when putting the page back throws", () => {
  const size = MEASURED_WINDOWS[7];
  const exact = (view) => {
    const { panel } = view;
    const origin = String(panel.box.svg.getAttribute("viewBox")).split(" ").map(Number);
    const top = panel.box.scrollTop + origin[1];
    const natural = Math.ceil(view.T.drawnModel().height);
    return { height: Number(panel.box.svg.getAttribute("height")), exact: Math.max(natural, Math.ceil(top + panel.box.clientHeight)) - origin[1], tallest: Math.ceil(top + size.viewport) - origin[1] };
  };
  const broken = (view) => {
    const [own] = view.page.windows;
    own.scrollTo = () => {
      throw new Error("synthetic scroll failure");
    };
    return () => {
      own.scrollTo = view.page.scrollTo;
    };
  };
  const results = {};
  // A collapse whose control is kept, and a pin after a collapse.
  const toggled = measuredPage(bigShelf(), size);
  const key = expandControls(toggled.panel)[0].key;
  toggleFrom(toggled, size, key, "top", size.expandY);
  const repair = broken(toggled);
  assert.throws(() => toggleFrom(toggled, size, key, "top", size.collapseY), /synthetic scroll failure/, "collapse: the error reaches the page");
  repair();
  results.collapse = exact(toggled);
  const pinned = collapsedFrom(size, "top");
  pinned.page.scrollTo(0, 100);
  const restore = broken(pinned);
  assert.throws(() => pinLink(pinned), /synthetic scroll failure/, "pin: the error reaches the page");
  restore();
  results.pin = exact(pinned);
  for (const [name, { height, exact: wanted, tallest }] of Object.entries(results)) {
    assert.ok(tallest > wanted, `${name}: the tallest box's room (${tallest}) is more than the exact room (${wanted})`);
    assert.equal(height, wanted, `${name}: the drawing is sized exactly for its view`);
  }
});

// A browser saw the box's own horizontal offset go from 100 to 0 px on a
// refresh with unchanged data at 1920x1080. The drawing was then kept 208 px
// right of its left edge by the expand that drew the shelf in full, and the
// box was scrolled 100 px into that room: the view lies 108 px before the
// drawing's left edge. The refresh sizes the room for that view, 108 px, so
// the box's offset into it is 0 while nothing on the screen moves.
test("a refresh with unchanged data keeps the drawing, its offsets and the page where they were, also scrolled sideways", async () => {
  for (const [size, left] of [[MEASURED_WINDOWS[4], 100], [MEASURED_WINDOWS[0], 100], [MEASURED_WINDOWS[4], 0]]) {
    const fabric = bigShelf();
    const view = measuredPage(fabric, size);
    const { panel, page } = view;
    view.click({ "data-fabric-expand-slots": expandControls(panel)[0].key });
    const origin = () => String(panel.box.svg.getAttribute("viewBox")).split(" ").map(Number).slice(0, 2);
    assert.deepEqual(origin(), [-208, 0], `${size.name}: room before the drawing's left edge after the expand`);
    panel.box.scrollLeft = left;
    panel.box.scrollTop = 200;
    page.scrollTo(0, 100);
    const label = `${size.name}, ${left}`;
    assert.deepEqual([panel.box.scrollLeft, panel.box.scrollTop, page.scrollY], [left, 200, 100], label);
    const bay = () => {
      const item = view.T.drawnModel().bays[5];
      return screenPoint(panel, { x: item.x, y: item.y });
    };
    const offsets = () => [panel.box.scrollLeft + origin()[0], panel.box.scrollTop + origin()[1]];
    const before = { bay: bay(), offsets: offsets(), pageY: page.scrollY };
    await refreshSame(view, fabric);
    assert.equal(bay()[0], before.bay[0], `${label}: a drawn bay keeps its x in the window`);
    assert.deepEqual({ bay: bay(), offsets: offsets(), pageY: page.scrollY }, before, `${label}: nothing moves`);
    assert.deepEqual([panel.box.scrollLeft, origin()[0]], left ? [0, left - 208] : [0, -208], `${label}: the room the view needs, and the box at its start`);
  }
});
