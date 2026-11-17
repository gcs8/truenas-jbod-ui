"use strict";

// Storage Fabric Topology mode (#937, #1361, #1369): the trace band under the
// drawing keeps one height for any sentence its drawing can show. The band is
// a one-cell grid: the visible sentence and a few hidden sizer sentences (the
// longest the drawing can produce) share the cell, so the band takes the
// tallest height and reflows with the width by itself.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const H = require("./helpers/fabric_topology");

const SOURCE_PATH = path.join(H.ROOT, "app/static/sas_fabric_topology.js");
const CSS_PATH = path.join(H.ROOT, "app/static/sas_fabric_topology.css");
// The most sizers a drawing may carry, whatever its size.
const SIZER_LIMIT = 8;

function loadSource(source, context = {}) {
  const sandbox = vm.createContext({ window: {}, document: { addEventListener() {} }, ...context });
  vm.runInContext(source, sandbox);
  return sandbox.window.SasFabricTopology;
}

function unescape(text) {
  return text.replace(/&quot;/g, "\"").replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}

function sizersOf(html) {
  return [...html.matchAll(/data-topology-sizer="([^"]*)"/g)].map((match) => unescape(match[1]));
}

// The drawn items a pointer or keyboard can reach, from the markup.
function drawnRefs(html) {
  const kinds = { link: "topoLink", node: "topoNode", bay: "topoBay", group: "topoGroup" };
  const seen = new Map();
  for (const match of html.matchAll(/data-topo-(link|node|bay|group)="([^"]*)"/g)) {
    const ref = { kind: match[1], id: unescape(match[2]), key: kinds[match[1]] };
    seen.set(`${ref.kind}\u0000${ref.id}`, ref);
  }
  return [...seen.values()];
}

// The page around the drawing, as far as hover, focus and a pin reach it.
function page(T) {
  const handlers = new Map();
  const pin = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pin : null), querySelectorAll: () => [] };
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === ".fabric-topology" ? root : null),
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  T.attach(container, null, null, {});
  const element = (ref) => ({
    dataset: { [ref.key]: ref.id },
    closest(selector) {
      if (selector === ".fabric-topology") return root;
      return /\[data-topo-/.test(selector) ? this : null;
    },
  });
  return { handlers, pin, element };
}

// Every sentence the band can show in one drawing: the hint, each drawn item
// hovered or focused with and without a pin held, and each pinnable item pinned.
function producible(T, fabric, options) {
  const { handlers, pin, element } = page(T);
  const html = T.render(fabric, options);
  const sentences = new Set();
  const refs = drawnRefs(html);
  const pinnable = refs.filter((ref) => ref.kind !== "group");
  // The hint, as the drawing starts.
  sentences.add(/data-topology-pin>([^<]*)</.exec(html)[1]);
  for (const ref of refs) {
    handlers.get("mouseover")({ target: element(ref) });
    sentences.add(pin.textContent);
  }
  for (const ref of pinnable) {
    handlers.get("click")({ target: element(ref) });
    handlers.get("mouseout")({ target: element(ref), relatedTarget: null });
    sentences.add(pin.textContent);
  }
  // A pin held: hover and focus each item.
  if (pinnable.length) handlers.get("click")({ target: element(pinnable[0]) });
  for (const ref of refs) {
    handlers.get("focusin")({ target: element(ref) });
    sentences.add(pin.textContent);
  }
  sentences.delete("");
  return { html, sentences: [...sentences] };
}

function longest(sentences) {
  return sentences.reduce((best, text) => (text.length > best.length ? text : best), "");
}

// The default two-HBA system with an eight-lane wide port on each level-1 and level-2 link.
function widePort() {
  const fabric = H.coreFabric();
  const fronts = [];
  const rears = [];
  for (const [index, name] of ["mpr0", "mpr1"].entries()) {
    const hba = H.addController(fabric, name, { phys: 16, linked: 8 });
    const level1 = H.addExpander(fabric, name, 0xa10 + index * 0x100, 1, hba.id, { reported_lanes: H.lanes(H.range(0, 8), H.range(32, 40)) });
    const level2 = H.addExpander(fabric, name, 0xa20 + index * 0x100, 2, level1.id, { reported_lanes: H.lanes(H.range(24, 32), H.range(0, 8)) });
    fronts.push(H.addEnclosure(fabric, name, "0x0001", `SYNTH-FRONT-${name.toUpperCase()}`, level1.id).id);
    rears.push(H.addEnclosure(fabric, name, "0x0002", `SYNTH-REAR-${name.toUpperCase()}`, level2.id).id);
  }
  const dual = [{ controller: "mpr0", state: "active" }, { controller: "mpr1", state: "passive" }];
  H.addBays(fabric, H.range(0, 24), fronts, dual);
  H.addBays(fabric, H.range(24, 48), rears, dual);
  return fabric;
}

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

function expandedAll(T, fabric) {
  return Object.fromEntries([...T.buildTopologyModel(fabric, { width: 1100 }).groups].map((group) => [group.expandKey, true]));
}

test("the band's sizers hold the longest sentence the drawing can show, in its held-pin form, and only sentences it can show", () => {
  const cases = [
    ["default system, 1,100 px", H.twoHbaCascade().fabric, { width: 1100 }],
    ["default system, 322 px", H.twoHbaCascade().fabric, { width: 322 }],
    ["eight-lane wide port", widePort(), { width: 1100 }],
    ["four shelves, collapsed", H.fourShelves().fabric, { width: 1100 }],
  ];
  for (const [name, fabric, options] of cases) {
    const T = H.loadTopology({ document: { addEventListener() {} } });
    const { html, sentences } = producible(T, fabric, options);
    const sizers = sizersOf(html);
    const top = longest(sentences);
    assert.match(top, /^Tracing: .*\. Pinned item kept; Esc clears\.$/, `${name}: the longest form is a trace with a pin held`);
    assert.ok(sizers.includes(top), `${name}: the longest sentence (${top.length} characters) is a sizer: ${JSON.stringify(sizers)}`);
    assert.ok(sizers.length >= 2 && sizers.length <= SIZER_LIMIT, `${name}: a few sizers, not one and not more than ${SIZER_LIMIT}: ${sizers.length}`);
    assert.equal(new Set(sizers).size, sizers.length, `${name}: no sentence twice`);
    for (const sizer of sizers) assert.ok(sentences.includes(sizer), `${name}: a sizer the band can show: ${sizer}`);
    // The longest sentences: one left out that is longer than one kept is a
    // shorter form (no pin held, or the pin alone) of an item whose held-pin
    // trace, the same words and more, is kept.
    const shortestKept = Math.min(...sizers.map((text) => text.length));
    const longer = sentences.filter((text) => text.length > shortestKept && !sizers.includes(text));
    for (const text of longer) {
      const label = /^(?:Tracing|Pinned): (.*?)\.(?: Esc clears\.)?$/.exec(text)?.[1];
      assert.ok(label && sizers.includes(`Tracing: ${label}. Pinned item kept; Esc clears.`), `${name}: left out, and no longer form kept: ${text}`);
    }
  }
  const T = H.loadTopology({ document: { addEventListener() {} } });
  const wide = sizersOf(T.render(widePort(), { width: 1100 }));
  assert.ok(wide.some((text) => text.includes("PHYs 24, 25, 26, 27, 28, 29, 30, 31 to PHYs 0, 1, 2, 3, 4, 5, 6, 7. Pinned item kept; Esc clears.")),
    "the wide port's whole PHY list is in a sizer");
});

test("a 1,024-bay shelf carries no more sizers than a small drawing, and its collapsed bays are not among them", () => {
  const fabric = bigShelf();
  const T = H.loadTopology({ document: { addEventListener() {} } });
  const collapsed = producible(T, fabric, { width: 1100 });
  const expanded = producible(H.loadTopology({ document: { addEventListener() {} } }), fabric, { width: 1100, expanded: expandedAll(T, fabric) });
  assert.ok(drawnRefs(expanded.html).filter((ref) => ref.kind === "bay").length >= 1024, "every bay is drawn when the shelf is expanded");
  assert.equal(drawnRefs(collapsed.html).filter((ref) => ref.kind === "bay").length, 0, "no bay is drawn while the shelf is a summary");
  for (const [name, drawing] of [["collapsed", collapsed], ["expanded", expanded]]) {
    const sizers = sizersOf(drawing.html);
    assert.ok(sizers.length >= 2 && sizers.length <= SIZER_LIMIT, `${name}: ${sizers.length} sizers`);
    assert.equal((drawing.html.match(/data-topology-sizer=/g) || []).length, sizers.length, `${name}: one element per sizer`);
    for (const sizer of sizers) assert.ok(drawing.sentences.includes(sizer), `${name}: a sentence this drawing can show: ${sizer}`);
    assert.ok(sizers.includes(longest(drawing.sentences)), `${name}: the longest is kept`);
  }
});

test("the band's sentences are written once: a change to the wording changes the visible sentence and every sizer alike", () => {
  const source = fs.readFileSync(SOURCE_PATH, "utf8");
  // Each piece of wording is in the source once, so there is no second copy to drift.
  for (const words of ["Tracing: ", "Pinned: ", " Pinned item kept; Esc clears.", ". Esc clears."]) {
    assert.equal(source.split(words).length - 1, 1, `${JSON.stringify(words)} is written once`);
  }
  const changed = source.replace("Tracing: ", "Following: ").replace(" Pinned item kept; Esc clears.", " The pin stays; Esc clears it.");
  const T = loadSource(changed);
  const fabric = H.twoHbaCascade().fabric;
  const { html, sentences } = producible(T, fabric, { width: 1100 });
  const sizers = sizersOf(html);
  assert.ok(sizers.length, "sizers are drawn");
  assert.ok(sizers.some((text) => /^Following: .* The pin stays; Esc clears it\.$/.test(text)), JSON.stringify(sizers));
  assert.ok(sizers.every((text) => !text.includes("Pinned item kept")), "no sizer keeps the old words");
  assert.ok(sentences.some((text) => /^Following: .* The pin stays; Esc clears it\.$/.test(text)), "the visible sentence has the new words too");
  assert.ok(sizers.includes(longest(sentences)));
});

function band(html) {
  const found = /<div class="fabric-topology-trace" data-topology-trace>([\s\S]*?)<\/div>/.exec(html);
  assert.ok(found, "the band is one element");
  return found[1];
}

test("the band holds the visible sentence first and then only hidden, empty sizer elements; a pin redraw changes only the visible text", () => {
  const T = H.loadTopology({ document: { addEventListener() {} } });
  const { handlers, pin, element } = page(T);
  const fabric = H.twoHbaCascade().fabric;
  const idle = T.render(fabric, { width: 1100 });
  assert.equal((idle.match(/data-topology-trace>/g) || []).length, 1, "one band");
  const inner = band(idle);
  const visible = /^<p class="fabric-topology-pin subtle" data-topology-pin>[^<]*<\/p>/.exec(inner);
  assert.ok(visible, "the visible sentence comes first, as before");
  const rest = inner.slice(visible[0].length);
  const sizers = [...rest.matchAll(/<span ([^>]*)><\/span>/g)];
  assert.equal(sizers.map((match) => match[0]).join(""), rest, "nothing but sizers after it");
  assert.ok(sizers.length >= 2);
  for (const [, attributes] of sizers) {
    assert.match(attributes, /^class="fabric-topology-trace-sizer subtle" aria-hidden="true" data-topology-sizer="[^"]*"$/, attributes);
  }
  assert.doesNotMatch(rest, /tabindex|role=|data-topology-pin/, "no sizer is focusable, a control or the live sentence");
  // A link pin redraws the drawing: the band differs only in the visible
  // text, also for the link with the longest sentence, whose pinned form is
  // longer than most sizers but shorter than its own held-pin trace.
  let link = null;
  let most = -1;
  for (const ref of drawnRefs(idle).filter((item) => item.kind === "link")) {
    handlers.get("mouseover")({ target: element(ref) });
    if (pin.textContent.length > most) [link, most] = [ref, pin.textContent.length];
  }
  handlers.get("click")({ target: element(link) });
  const pinned = T.render(fabric, { width: 1100 });
  const after = band(pinned);
  assert.match(after, /data-topology-pin>Pinned: /);
  const blank = (text) => text.replace(/data-topology-pin>[^<]*</, "data-topology-pin><");
  assert.equal(blank(after), blank(inner), "the same sizers and the same visible element");
});

test("hover, focus, a pin and Esc write only the visible sentence's text and read no layout", () => {
  const log = [];
  const pin = new Proxy({ textContent: "" }, {
    get(target, key) {
      if (key !== "textContent") log.push(`pin get ${String(key)}`);
      return target[key];
    },
    set(target, key, value) {
      if (key !== "textContent") log.push(`pin set ${String(key)}`);
      target[key] = value;
      return true;
    },
  });
  const marks = new Set(["[data-topo-node]", "[data-topo-link]", "[data-topo-cross]", "[data-topo-bay]", "[data-topo-group]"]);
  const root = new Proxy({}, {
    get(target, key) {
      if (key === "querySelector") return (selector) => { if (selector !== "[data-topology-pin]") log.push(`query ${selector}`); return selector === "[data-topology-pin]" ? pin : null; };
      if (key === "querySelectorAll") return (selector) => { if (!marks.has(selector)) log.push(`query all ${selector}`); return []; };
      log.push(`root get ${String(key)}`);
      return undefined;
    },
  });
  const handlers = new Map();
  const docHandlers = new Map();
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === ".fabric-topology" ? root : null),
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  const T = H.loadTopology({ document: { addEventListener: (name, handler) => docHandlers.set(name, handler) } });
  T.attach(container, null, null, {});
  const html = T.render(H.twoHbaCascade().fabric, { width: 1100 });
  const element = (ref) => ({
    dataset: { [ref.key]: ref.id },
    closest(selector) {
      if (selector === ".fabric-topology") return root;
      return /\[data-topo-/.test(selector) ? this : null;
    },
  });
  const refs = drawnRefs(html);
  const bay = refs.find((ref) => ref.kind === "bay");
  const link = refs.find((ref) => ref.kind === "link");
  handlers.get("mouseover")({ target: element(link) });
  handlers.get("focusin")({ target: element(bay) });
  handlers.get("click")({ target: element(bay) });
  handlers.get("mouseout")({ target: element(bay), relatedTarget: null });
  handlers.get("mouseover")({ target: element(link) });
  docHandlers.get("keydown")({ key: "Escape", target: element(link) });
  assert.match(pin.textContent, /^Hover or focus/);
  assert.deepEqual(log, [], "only the visible text is written; nothing else in the band is read or changed");
});

function cssRule(css, selector) {
  return new RegExp(`(?:^|\\n|\\})\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`).exec(css)?.[1] || "";
}

test("the band is one grid cell its sentences share; sizers are invisible, unselectable and drawn from their attribute; nothing is clipped", () => {
  const css = fs.readFileSync(CSS_PATH, "utf8");
  const bandRule = cssRule(css, ".fabric-topology .fabric-topology-trace");
  assert.match(bandRule, /display:\s*grid;/);
  assert.match(bandRule, /grid-template-columns:\s*minmax\(0, 1fr\);/);
  assert.match(bandRule, /min-height:\s*var\(--topology-trace-band\);/, "the reserved heights stay as the floor");
  assert.match(cssRule(css, ".fabric-topology-trace > *"), /grid-area:\s*1 \/ 1;/, "every sentence in the one cell");
  const sizer = cssRule(css, ".fabric-topology-trace-sizer");
  assert.match(sizer, /visibility:\s*hidden;/);
  assert.match(sizer, /user-select:\s*none;/);
  assert.match(sizer, /pointer-events:\s*none;/);
  assert.match(cssRule(css, ".fabric-topology-trace-sizer::before"), /content:\s*attr\(data-topology-sizer\);/, "its text is generated, not in the document");
  for (const rule of [bandRule, cssRule(css, ".fabric-topology-trace > *"), sizer, cssRule(css, ".fabric-topology .fabric-topology-pin")]) {
    assert.doesNotMatch(rule, /(?:^|[\s;])(?:height|max-height|overflow|text-overflow|white-space|-webkit-line-clamp|line-clamp)\s*:/, rule);
  }
});

test("a focused item keeps clear of the band at its measured height, with the reserved height until it is measured", () => {
  const css = fs.readFileSync(CSS_PATH, "utf8");
  assert.match(cssRule(css, ".fabric-topology-svg [role=\"button\"]"), /scroll-margin-bottom:\s*var\(--topology-trace-height, var\(--topology-trace-band\)\);/);
  const observers = [];
  class FakeResizeObserver {
    constructor(callback) {
      this.callback = callback;
      this.targets = [];
      this.disconnects = 0;
      observers.push(this);
    }
    observe(target, options) { this.targets.push({ target, options }); }
    disconnect() { this.disconnects += 1; this.targets = []; }
  }
  const make = () => {
    const style = new Map();
    const root = { style: { setProperty: (name, value) => style.set(name, value) } };
    const trace = { closest: (selector) => (selector === ".fabric-topology" ? root : null) };
    return { root, trace, style };
  };
  let current = make();
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === "[data-topology-trace]" ? current.trace : null),
    addEventListener() {},
  };
  const T = H.loadTopology({ document: { addEventListener() {} }, window: { ResizeObserver: FakeResizeObserver } });
  T.attach(container, null, null, {});
  T.render(H.twoHbaCascade().fabric, { width: 1100 });
  T.mount(container);
  assert.equal(observers.length, 1);
  assert.equal(observers[0].targets.length, 1);
  assert.equal(observers[0].targets[0].target, current.trace, "the band is observed");
  assert.deepEqual({ ...observers[0].targets[0].options }, { box: "border-box" });
  observers[0].callback([{ target: current.trace, borderBoxSize: [{ blockSize: 88.4, inlineSize: 340 }] }]);
  assert.equal(current.style.get("--topology-trace-height"), "89px", "the band's whole height, rounded up, on the topology root");
  // A redraw puts a new band in place: it is observed instead of the old one.
  const first = current;
  current = make();
  T.render(H.twoHbaCascade().fabric, { width: 1100 });
  T.mount(container);
  assert.equal(observers.length, 1, "one observer for the page");
  assert.deepEqual(observers[0].targets.map((item) => item.target), [current.trace]);
  observers[0].callback([{ target: current.trace, borderBoxSize: [{ blockSize: 69, inlineSize: 340 }] }]);
  assert.equal(current.style.get("--topology-trace-height"), "69px");
  assert.equal(first.style.get("--topology-trace-height"), "89px");
  // The same band again: nothing to change.
  const disconnects = observers[0].disconnects;
  T.mount(container);
  assert.equal(observers[0].disconnects, disconnects);
  // An entry without a size leaves the reserved height in force.
  observers[0].callback([{ target: current.trace }]);
  assert.equal(current.style.get("--topology-trace-height"), "69px");
  T.release();
  assert.deepEqual(observers[0].targets, [], "leaving the mode stops observing");
  // A browser without ResizeObserver: the reserved height is the margin.
  const bare = make();
  const plain = { dataset: {}, querySelector: (selector) => (selector === "[data-topology-trace]" ? bare.trace : null), addEventListener() {} };
  const U = H.loadTopology({ document: { addEventListener() {} } });
  U.attach(plain, null, null, {});
  U.render(H.twoHbaCascade().fabric, { width: 1100 });
  U.mount(plain);
  assert.equal(bare.style.size, 0);
});
