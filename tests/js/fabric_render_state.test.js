"use strict";

// Storage Fabric render-state contract: a render must not drop keyboard focus,
// scroll position, open panels, or an in-progress friendly-name draft, and the
// controls it emits must be real buttons that are not nested inside each other.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(name) {
  const start = FABRIC_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = FABRIC_SOURCE.indexOf("{", FABRIC_SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < FABRIC_SOURCE.length; index += 1) {
    const character = FABRIC_SOURCE[index];
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return FABRIC_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

// Synthetic CORE fabric: 2 HBAs, 4 paths, 8 expanders, 4 backplane zones, 40 bays.
function buildFabric(slotCount = 40) {
  const nodes = [];
  const links = [];
  const traces = [];
  const controllers = [];
  const paths = [];
  const slots = [];
  const slotIds = Array.from({ length: slotCount }, (_, index) => index);
  const zoneOf = (slot) => Math.floor(slot / Math.ceil(slotCount / 4));
  nodes.push({ id: "host", kind: "host", label: "nas.example.test", status: "online", metrics: {}, related_slots: slotIds, raw: {} });
  for (let c = 0; c < 2; c += 1) {
    const controllerId = `controller:mpr${c}`;
    nodes.push({ id: controllerId, kind: "controller", label: `mpr${c}`, status: "online", metrics: { firmware: "16.00.12.00" }, related_slots: slotIds, raw: {}, raw_id: `mpr${c}` });
    controllers.push({ id: controllerId, name: `mpr${c}`, related_slots: slotIds });
    for (const pathState of ["active", "passive"]) {
      const pathId = `path:mpr${c}:${pathState}`;
      const pathSlots = slotIds.filter((slot) => (pathState === "active" ? slot % 2 === c : slot % 2 !== c));
      nodes.push({ id: pathId, kind: "path", label: `mpr${c} ${pathState}`, status: pathState, controller_id: controllerId, related_slots: pathSlots, metrics: { state: pathState }, raw: {} });
      paths.push({ id: pathId, controller: `mpr${c}`, state: pathState, slots: pathSlots, count: pathSlots.length });
      traces.push({ id: pathId, kind: "path", label: `mpr${c} ${pathState}`, slots: pathSlots, node_ids: ["host", controllerId, pathId], link_ids: [], metrics: { state: pathState }, evidence: [] });
      links.push({ id: `link:${controllerId}:${pathId}`, kind: "controller-path", source: controllerId, target: pathId, status: pathState, related_slots: pathSlots });
    }
    for (let e = 0; e < 4; e += 1) {
      const zoneSlots = slotIds.filter((slot) => zoneOf(slot) === e);
      nodes.push({ id: `expander:${c}:${e}`, kind: "expander", label: `5000000000000${c}${e}`, status: "online", controller_id: controllerId, related_slots: zoneSlots, metrics: { linked_phys: 36, num_phys: 36 }, raw: {} });
      nodes.push({ id: `mpr-enclosure:${c}:${e}`, kind: "mpr-enclosure", label: `enc-${c}-${e}`, status: "online", controller_id: controllerId, related_slots: zoneSlots, metrics: {}, raw: {} });
    }
  }
  for (let z = 0; z < 4; z += 1) {
    nodes.push({ id: `backplane:${z}`, kind: "backplane", label: `Backplane Zone ${z + 1}`, status: "online", related_slots: slotIds.filter((slot) => zoneOf(slot) === z), metrics: {}, raw: {} });
  }
  for (const slot of slotIds) {
    const c = slot % 2;
    const z = zoneOf(slot);
    slots.push({ slot, present: true, physical_location_known: true, device_name: `da${slot}`, serial: `SANITIZED-${String(slot).padStart(4, "0")}`, model: "SYNTH-HDD", size_human: "18 TB", pool_name: "tank", vdev_name: `raidz2-${z}` });
    nodes.push({ id: `bay:${slot}`, kind: "bay", label: `Bay ${String(slot).padStart(2, "0")}`, status: "online", related_slots: [slot], metrics: {}, raw: {} });
    traces.push({
      id: `bay:${slot}`,
      kind: "bay",
      label: `Bay ${String(slot).padStart(2, "0")}`,
      slots: [slot],
      node_ids: ["host", `controller:mpr${c}`, `path:mpr${c}:active`, `expander:${c}:${z}`, `mpr-enclosure:${c}:${z}`, `backplane:${z}`, `bay:${slot}`],
      link_ids: [],
      metrics: { device_name: `da${slot}`, path_states: [{ controller: `mpr${c}`, state: "active", device_name: `da${slot}` }] },
      evidence: ["inventory snapshot"],
    });
  }
  return {
    available: true,
    platform: "core",
    system_id: "demo",
    system_label: "nas.example.test",
    selected_enclosure_id: "enc-1",
    selected_enclosure_label: "Demo JBOD",
    nodes, links, traces, controllers, paths,
    expanders: [], enclosures: [], aliases: [], warnings: [], sources: {},
    raw: { fabric_kind: "core_sas" },
    snapshot: { slots },
  };
}

function attributePairs(selector) {
  return Array.from(selector.matchAll(/\[([\w-]+)="((?:[^"\\]|\\.)*)"\]/g))
    .map((match) => [match[1], match[2].replace(/\\(.)/g, "$1")]);
}

// Minimal element: innerHTML is a string; querySelectorAll scans the markup for
// opening tags carrying every [attr="value"] pair in the selector.
function makeElement(id, focusLog) {
  const element = {
    id,
    _innerHTML: "",
    innerHTMLWrites: 0,
    textContent: "",
    value: "",
    disabled: false,
    href: "",
    scrollTop: 0,
    scrollLeft: 0,
    dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll(selector) {
      const pairs = attributePairs(selector);
      if (!pairs.length) return [];
      return Array.from(element._innerHTML.matchAll(/<[a-z]+\b[^>]*>/g))
        .map((match) => match[0])
        .filter((tag) => pairs.every(([name, value]) => tag.includes(`${name}="${value}"`)))
        .map((tag) => ({
          tag,
          focus() { focusLog.push(tag); },
          setSelectionRange(start, end) { focusLog.push(`selection:${start}:${end}`); },
        }));
    },
    setAttribute() {},
    addEventListener() {},
  };
  Object.defineProperty(element, "innerHTML", {
    get() { return element._innerHTML; },
    set(value) {
      element._innerHTML = value;
      element.innerHTMLWrites += 1;
      element.scrollTop = 0;
      element.scrollLeft = 0;
    },
  });
  return element;
}

function loadFabricPage({ baseURI = "http://nas.example.test/sas-fabric", backLink = null } = {}) {
  const fabric = buildFabric();
  const elements = new Map();
  const handlers = new Map();
  const focusLog = [];
  const fetchLog = [];
  const replaceStateLog = [];
  const pending = [];
  const counters = { renders: 0 };
  class HTMLInputElement {}
  class HTMLDetailsElement {}
  class Element {}
  const document = {
    baseURI,
    activeElement: null,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id, focusLog));
      return elements.get(id);
    },
    querySelectorAll(selector) {
      return selector === "[data-fabric-back-link]" && backLink ? [backLink] : [];
    },
    querySelector() { return null; },
    addEventListener(type, handler) {
      handlers.set(type, handler);
    },
  };
  const sandbox = vm.createContext({
    URLSearchParams, URL, TextEncoder, console, Date, Math, Number, String, Array, Object, Set, Map, WeakMap, JSON, Boolean, Error, Promise,
    encodeURIComponent,
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    setTimeout,
    HTMLInputElement, HTMLFormElement: class {}, HTMLSelectElement: class {}, HTMLDetailsElement, Element,
    __counters: counters,
    fetch(url) {
      fetchLog.push(String(url));
      return new Promise((resolve) => {
        pending.push(() => resolve({ ok: true, status: 200, json: async () => ({ available: true, smart_health_status: "PASSED", temperature_c: 31 }) }));
      });
    },
    window: {
      location: { search: "", pathname: new URL(baseURI).pathname, href: baseURI, origin: new URL(baseURI).origin },
      history: { replaceState(_state, _title, url) { replaceStateLog.push(url); } },
      requestAnimationFrame(callback) { callback(); },
      scrollX: 0,
      scrollY: 0,
      SAS_FABRIC_BOOTSTRAP: {
        snapshot: { slots: fabric.snapshot.slots, systems: [{ id: "demo", label: "nas.example.test" }], enclosures: [{ id: "enc-1", label: "Demo JBOD" }], selected_system_id: "demo", selected_enclosure_id: "enc-1" },
        fabric,
      },
    },
    document,
  });
  const source = FABRIC_SOURCE
    .replace("function render() {", "function render() { __counters.renders += 1;")
    .replace(/\}\)\(\);\s*$/, `
    window.__fabricTest = { state, elements, render, handleFabricActivation, refreshFabric, scopedUrl, syncLocation, applyFabric, nodeMap, traceMap, slotByNumber, fabricViewCopy, formatKind, renderTraceSummaryButton };
  })();`);
  vm.runInContext(source, sandbox);
  return {
    ...sandbox.window.__fabricTest,
    fabric, elements, handlers, focusLog, fetchLog, replaceStateLog, counters, document,
    HTMLInputElement, HTMLDetailsElement, Element,
    resolvePending() {
      pending.splice(0).forEach((resolve) => resolve());
      return new Promise((resolve) => setTimeout(resolve, 5));
    },
  };
}

function activationTarget(attribute, dataset, extra = {}) {
  const button = { dataset, ...extra };
  return { closest(selector) { return selector === `[${attribute}]` ? button : null; } };
}

test("activating a node re-focuses that node's card after the map is rebuilt", () => {
  const page = loadFabricPage();
  page.state.mode = "lanes";
  page.render();
  const mapPanel = page.elements.get("fabric-map-panel");
  page.document.activeElement = {
    getAttribute: (name) => (name === "data-fabric-node" ? "expander:0:1" : null),
    hasAttribute: (name) => name === "data-fabric-node",
    closest: (selector) => (selector.includes("#fabric-map-panel") ? mapPanel : null),
  };
  const writes = mapPanel.innerHTMLWrites;
  page.handleFabricActivation(activationTarget("data-fabric-node", { fabricNode: "expander:0:1" }));
  assert.equal(page.state.selectedNodeId, "expander:0:1");
  assert.ok(mapPanel.innerHTMLWrites > writes, "the lanes map is rebuilt on selection");
  assert.equal(page.focusLog.length, 1, "exactly one element receives focus after the render");
  assert.match(page.focusLog[0], /data-fabric-node="expander:0:1"/);
});

test("map and inspector scroll positions survive a render", () => {
  const page = loadFabricPage();
  page.state.mode = "lanes";
  page.render();
  const mapPanel = page.elements.get("fabric-map-panel");
  const inspector = page.elements.get("fabric-inspector-body");
  mapPanel.scrollTop = 240;
  inspector.scrollTop = 80;
  page.render();
  assert.equal(mapPanel.scrollTop, 240);
  assert.equal(inspector.scrollTop, 80);
});

test("an opened kernel-error panel stays open when the SMART summary arrives", async () => {
  const page = loadFabricPage();
  const controller = page.fabric.nodes.find((node) => node.id === "controller:mpr1");
  controller.metrics.kernel_diagnostics = {
    event_count: 3,
    error_count: 1,
    devices: ["da3"],
    recent_events: [
      { device: "da3", message: "synthetic error", severity: "error" },
      { device: "da3", message: "synthetic warning", severity: "warning" },
      { device: "da3", message: "synthetic info", severity: "info" },
    ],
  };
  page.state.mode = "disk";
  page.state.selectedTraceId = "bay:3";
  page.state.selectedDiskTraceId = "bay:3";
  page.render();
  const mapPanel = page.elements.get("fabric-map-panel");
  const keyMatch = mapPanel.innerHTML.match(/<details class="fabric-diagnostic-evidence[^>]*data-fabric-evidence-key="([^"]+)"/);
  assert.ok(keyMatch, "the evidence panel carries a key so its open state can be remembered");
  assert.doesNotMatch(mapPanel.innerHTML, /<details class="fabric-diagnostic-evidence[^>]*\sopen/);
  assert.equal(page.fetchLog.filter((url) => url.includes("/api/slots/3/smart")).length, 1, "selecting a bay starts one SMART fetch");

  const details = new page.HTMLDetailsElement();
  Object.assign(details, { open: true, dataset: { fabricEvidenceKey: keyMatch[1] }, matches: (selector) => selector === "[data-fabric-evidence-key]" });
  page.handlers.get("toggle")({ target: details });
  assert.ok(page.state.openEvidencePanels.has(keyMatch[1]));

  const renders = page.counters.renders;
  await page.resolvePending();
  assert.equal(page.counters.renders, renders + 1, "SMART arrival still re-renders when nothing is being edited");
  assert.match(mapPanel.innerHTML, /<details class="fabric-diagnostic-evidence[^>]*\sopen/);
});

test("friendly-name drafts survive a render and Escape cancels the editor", () => {
  const page = loadFabricPage();
  page.state.mode = "trace";
  page.render();
  page.handleFabricActivation(activationTarget("data-fabric-node", { fabricNode: "expander:0:1" }));
  page.handleFabricActivation(activationTarget("data-fabric-alias-edit", { fabricAliasEdit: "expander:0:1" }));
  assert.equal(page.state.aliasEditObjectId, "expander:0:1");
  const inspector = page.elements.get("fabric-inspector-body");
  assert.match(inspector.innerHTML, /data-fabric-alias-input[\s\S]*?value=""|value=""[\s\S]*?data-fabric-alias-input/);

  const input = new page.HTMLInputElement();
  Object.assign(input, { value: "Front shelf", dataset: { fabricAliasObject: "expander:0:1" }, matches: (selector) => selector === "[data-fabric-alias-input]" });
  page.handlers.get("input")({ target: input });
  page.render();
  assert.match(inspector.innerHTML, /value="Front shelf"/);

  const escapeTarget = new page.Element();
  escapeTarget.closest = (selector) => (selector === "[data-fabric-alias-form]" ? {} : null);
  let prevented = false;
  page.handlers.get("keydown")({ key: "Escape", target: escapeTarget, preventDefault() { prevented = true; } });
  assert.ok(prevented);
  assert.equal(page.state.aliasEditObjectId, null);
  assert.equal(page.state.aliasDraft, null);
  assert.doesNotMatch(inspector.innerHTML, /data-fabric-alias-input/);

  page.handleFabricActivation(activationTarget("data-fabric-alias-edit", { fabricAliasEdit: "expander:0:1" }));
  assert.equal(page.state.aliasDraft, null, "reopening the editor starts from the saved label, not the abandoned draft");
});

test("SMART arrival does not re-render while a friendly name is being edited", async () => {
  const page = loadFabricPage();
  page.state.mode = "disk";
  page.state.selectedTraceId = "bay:3";
  page.state.selectedDiskTraceId = "bay:3";
  page.render();
  assert.equal(page.fetchLog.filter((url) => url.includes("/api/slots/3/smart")).length, 1);
  page.handleFabricActivation(activationTarget("data-fabric-alias-edit", { fabricAliasEdit: "bay:3" }));
  const renders = page.counters.renders;
  await page.resolvePending();
  assert.equal(page.counters.renders, renders, "the editor is left alone until the user finishes");
});

test("slot overflow toggles and impact cards are siblings of their buttons, not nested controls", () => {
  const page = loadFabricPage();
  page.state.mode = "lanes";
  page.render();
  const lanes = page.elements.get("fabric-map-panel").innerHTML;
  assert.doesNotMatch(lanes, /role="button"/);
  assert.doesNotMatch(lanes, /tabindex="0"/);
  assert.match(lanes, /<\/button>\s*<button type="button" class="fabric-overflow-button" data-fabric-expand-slots="path:path:mpr0:active" aria-expanded="false">\+\d+<\/button>/);

  page.state.mode = "impact";
  page.state.selectedTraceId = "path:mpr0:active";
  page.render();
  const impact = page.elements.get("fabric-map-panel").innerHTML;
  assert.doesNotMatch(impact, /<article/);
  assert.match(impact, /<div class="fabric-impact-card[^"]*">\s*<button type="button" class="fabric-node-card fabric-impact-card-head" data-fabric-trace="path:mpr0:active">/);
});

test("the +N toggle expands the slot list in place without rebuilding the map", () => {
  const page = loadFabricPage();
  page.state.mode = "lanes";
  page.render();
  const mapPanel = page.elements.get("fabric-map-panel");
  const writes = mapPanel.innerHTMLWrites;
  const listElement = { textContent: "" };
  const cell = { dataset: { fabricSlotLimit: "16" }, querySelector: (selector) => (selector === "[data-fabric-slot-list]" ? listElement : null) };
  const attributes = {};
  const toggle = activationTarget("data-fabric-expand-slots", { fabricExpandSlots: "path:path:mpr0:active" }, {
    textContent: "",
    closest: (selector) => (selector === "[data-fabric-slot-cell]" ? cell : null),
    setAttribute(name, value) { attributes[name] = value; },
  });
  page.handleFabricActivation(toggle);
  assert.equal(mapPanel.innerHTMLWrites, writes, "no innerHTML rebuild for a list toggle");
  assert.ok(page.state.expandedSlotLists["path:path:mpr0:active"]);
  assert.equal(listElement.textContent.split(", ").length, 20, "all 20 active-path bays are listed once expanded");
  assert.equal(toggle.closest("[data-fabric-expand-slots]").textContent, "Show fewer");
  assert.equal(attributes["aria-expanded"], "true");
});

test("disk path cards show extra facts in a More details block instead of a hover-only tooltip", () => {
  const page = loadFabricPage();
  page.state.mode = "disk";
  page.state.selectedTraceId = "bay:3";
  page.render();
  const markup = page.elements.get("fabric-map-panel").innerHTML;
  assert.match(markup, /<\/(?:button|div)>\s*<details class="disk-path-card-details">\s*<summary>More details \(\d+\)<\/summary>/);
  const cardBodies = Array.from(markup.matchAll(/<button[^>]*class="disk-path-card [^"]*"[^>]*>([\s\S]*?)<\/button>/g)).map((match) => match[1]);
  assert.ok(cardBodies.length >= 4);
  assert.ok(cardBodies.every((body) => !body.includes("<details")), "the details block is never inside the card button");
  const titles = Array.from(markup.matchAll(/class="disk-path-card [^"]*"[^>]*title="([^"]*)"/g)).map((match) => match[1]);
  assert.ok(titles.length >= 4);
  assert.ok(titles.every((title) => title.split("\n").length <= 3), "tooltips are a short summary only");
});

test("visited related traces stay clickable", () => {
  const page = loadFabricPage();
  page.state.selectedTraceId = "bay:3";
  page.state.selectionTrail = [{ kind: "trace", id: "path:mpr1:active" }];
  const visited = page.renderTraceSummaryButton(page.fabric.traces.find((trace) => trace.id === "path:mpr1:active"));
  assert.doesNotMatch(visited, /disabled/);
  assert.match(visited, /data-fabric-trace="path:mpr1:active"/);
  assert.match(visited, /is-visited/);
  assert.match(visited, /\(visited\)/);
});

test("refresh fetches inventory and fabric together and the button does a cached refresh", () => {
  const page = loadFabricPage();
  page.fetchLog.length = 0;
  void page.refreshFabric(false);
  assert.deepEqual(
    page.fetchLog.map((url) => url.replace(/\?.*$/, "")),
    ["/api/inventory", "/api/sas-fabric"],
    "both requests start before either resolves",
  );
  assert.ok(page.fetchLog.every((url) => url.includes("force=false")));
  assert.match(FABRIC_SOURCE, /refreshButton\.addEventListener\("click", \(\) => \{\s*void refreshFabric\(false\);/);
  assert.match(functionSource("refreshFabric"), /Promise\.all\(\[/);
});

test("page and API URLs follow document.baseURI behind a path prefix", () => {
  const backLink = { href: "" };
  const page = loadFabricPage({ baseURI: "http://nas.example.test/jbod/sas-fabric", backLink });
  assert.equal(page.scopedUrl("/api/inventory"), "/jbod/api/inventory?system_id=demo&enclosure_id=enc-1");
  page.state.mode = "trace";
  page.syncLocation();
  assert.equal(page.replaceStateLog.at(-1), "/jbod/sas-fabric?system_id=demo&enclosure_id=enc-1&mode=trace");
  assert.equal(backLink.href, "/jbod/?system_id=demo&enclosure_id=enc-1");
});

test("node, trace, slot and copy lookups are cached per fabric object", () => {
  const page = loadFabricPage();
  assert.equal(page.nodeMap(page.fabric), page.nodeMap(page.fabric));
  assert.equal(page.traceMap(page.fabric), page.traceMap(page.fabric));
  assert.equal(page.fabricViewCopy(page.fabric), page.fabricViewCopy(page.fabric));
  assert.equal(page.slotByNumber(3), page.slotByNumber(3));
  assert.equal(page.slotByNumber(3).serial, "SANITIZED-0003");
  const nextFabric = { ...buildFabric(8), nodes: buildFabric(8).nodes.slice(0, 3) };
  page.applyFabric(nextFabric);
  assert.notEqual(page.nodeMap(nextFabric), page.nodeMap(page.fabric));
  assert.equal(page.nodeMap(nextFabric).size, 3);
});

test("kind labels come from a table and dead label branches are gone", () => {
  const page = loadFabricPage();
  assert.equal(page.formatKind("mpr-enclosure"), "MPR Enclosure");
  assert.equal(page.formatKind("ses-bay"), "SES Bay");
  assert.equal(page.formatKind("path-storage-enclosure"), "Path Storage Enclosure");
  assert.equal(page.formatKind("sas-widget"), "SAS Widget");
  assert.equal(page.formatKind(""), "Item");
  assert.match(FABRIC_SOURCE, /const KIND_LABELS = \{/);
  assert.doesNotMatch(functionSource("diskPathLabels"), /"Host" : storageFabric \? "Host"/);
  assert.doesNotMatch(functionSource("traceKindRank"), /backplane|storage-enclosure|mpr-enclosure/);
});
