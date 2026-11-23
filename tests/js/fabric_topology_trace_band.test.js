"use strict";

// Storage Fabric Topology mode (#937, #1369): the trace band under the
// drawing keeps one height for any sentence its drawing can show. The band
// holds the visible sentence and one empty, hidden measurer. When a drawing
// is put in place, every distinct sentence that drawing can show is laid out
// once in the measurer at the band's width, the tallest is read, rounded up
// and held as the band's height, and the measurer is emptied again. Hover,
// focus and a pin that keeps the same sentences change only the visible text.
// The only sentences left out of the measure are exact duplicates and a
// trace that is the start of its own held-pin trace. There is no limit.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const H = require("./helpers/fabric_topology");

const SOURCE_PATH = path.join(H.ROOT, "app/static/sas_fabric_topology.js");
const CSS_PATH = path.join(H.ROOT, "app/static/sas_fabric_topology.css");
const BUILDER = "function traceSentences(model, view = {}) {";

function loadSource(source, context = {}) {
  const sandbox = vm.createContext({ window: {}, document: { addEventListener() {} }, ...context });
  vm.runInContext(source, sandbox);
  return sandbox.window.SasFabricTopology;
}

function unescape(text) {
  return text.replace(/&quot;/g, "\"").replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
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

function visibleOf(html) {
  return unescape(/data-topology-pin>([^<]*)</.exec(html)[1]);
}

function band(html) {
  const found = /<div class="fabric-topology-trace" data-topology-trace>([\s\S]*?)<\/div><p class="fabric-topology-not-shown/.exec(html);
  assert.ok(found, "the band is one element");
  return found[1];
}

// Glyph advances of a fake layout, in px: a wide W, a narrow i, 7 px for the
// rest. A sentence takes whole lines of 20.2 px at the measurer's width.
function advance(text) {
  let sum = 0;
  for (const glyph of text) sum += glyph === "W" ? 14 : glyph === "i" ? 3.5 : 7;
  return sum;
}

function lineLayout(text, width) {
  return Math.max(1, Math.ceil(advance(text) / width)) * 20.2;
}

const LAYOUT_KEYS = ["clientWidth", "clientHeight", "offsetWidth", "offsetHeight", "scrollWidth", "scrollHeight"];

// A page around the drawing. Each draw puts a new band in place, as the page
// does when it replaces the map's markup: the visible paragraph, the
// measurer and a fake layout that lays out what the measurer holds. Every
// layout read, computed-style read, measure and held-height write is counted.
function fakePage(T0 = null, settings = {}) {
  const env = { width: 340, font: "16px sans-serif", lineHeight: "20px", dpr: 1, layout: lineLayout, ...settings };
  const counts = { measures: 0, reads: 0, styleReads: 0, holds: 0, markupWrites: 0 };
  const log = [];
  const fills = [];
  const handlers = new Map();
  const docHandlers = new Map();
  const fontHandlers = new Map();
  const media = [];
  const observers = [];
  const read = (name) => {
    counts.reads += 1;
    log.push(`read ${name}`);
  };
  const countReads = (target, name) => {
    for (const key of LAYOUT_KEYS) Object.defineProperty(target, key, { get() { read(`${name}.${key}`); return env.width; } });
  };
  const element = (tag) => ({ tagName: tag.toUpperCase(), className: "", textContent: "" });
  const doc = {
    createElement: (tag) => element(tag),
    createDocumentFragment() {
      const fragment = { fragment: true, children: [] };
      fragment.appendChild = (child) => { fragment.children.push(child); return child; };
      fragment.append = (...nodes) => { fragment.children.push(...nodes); };
      return fragment;
    },
  };
  const rootStyle = new Map();
  const root = {
    style: { setProperty: (name, value) => rootStyle.set(name, value) },
    querySelector: (selector) => (selector === "[data-topology-trace]" ? current : current.querySelector(selector)),
    querySelectorAll: () => [],
  };
  let current = null;
  function newBand(visible) {
    const measurer = { ownerDocument: doc, children: [] };
    const add = (nodes) => {
      for (const node of nodes) {
        if (typeof node === "string") throw new Error("text, not an element, given to the measurer");
        measurer.children.push(...(node.fragment ? node.children : [node]));
      }
    };
    measurer.appendChild = (child) => { add([child]); return child; };
    measurer.append = (...nodes) => add(nodes);
    measurer.replaceChildren = (...nodes) => { measurer.children = []; add(nodes); };
    Object.defineProperty(measurer, "textContent", {
      get: () => measurer.children.map((child) => child.textContent).join(""),
      set(value) {
        assert.equal(value, "", "the measurer is only emptied through its text");
        measurer.children = [];
      },
    });
    Object.defineProperty(measurer, "innerHTML", { get: () => "", set() { counts.markupWrites += 1; } });
    measurer.getBoundingClientRect = () => {
      read("measurer.rect");
      const texts = measurer.children.map((child) => child.textContent);
      if (texts.length) {
        counts.measures += 1;
        fills.push({ texts, elements: measurer.children.map((child) => ({ tag: child.tagName, className: child.className })), width: env.width });
      }
      const height = texts.length ? Math.max(...texts.map((text) => env.layout(text, env.width))) : 0;
      return { width: env.width, height, left: 0, top: 0, right: env.width, bottom: height };
    };
    countReads(measurer, "measurer");
    const para = { textContent: visible, className: "fabric-topology-pin subtle", getBoundingClientRect() { read("pin.rect"); return { width: env.width, height: 20 }; } };
    countReads(para, "pin");
    const style = new Map();
    const item = {
      measurer,
      para,
      style: {
        setProperty(name, value) {
          style.set(name, value);
          if (name === "--topology-trace-held") {
            counts.holds += 1;
            log.push(`hold ${value}`);
          }
        },
        getPropertyValue: (name) => style.get(name) || "",
      },
      held: () => style.get("--topology-trace-held") || null,
      querySelector: (selector) => ({ "[data-topology-measure]": measurer, "[data-topology-pin]": para })[selector] || null,
      closest: (selector) => (selector === ".fabric-topology" ? root : null),
      getBoundingClientRect() {
        read("band.rect");
        return { width: env.width, height: 0, left: 0, top: 0, right: env.width, bottom: 0 };
      },
    };
    countReads(item, "band");
    return item;
  }
  current = newBand("");
  const container = {
    dataset: {},
    box: null,
    querySelector(selector) {
      if (selector === ".fabric-topology") return root;
      if (selector === ".fabric-topology-scroll") return container.box;
      return root.querySelector(selector);
    },
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  class FakeResizeObserver {
    constructor(callback) {
      this.callback = callback;
      this.targets = [];
      observers.push(this);
    }
    observe(target, options) { this.targets.push({ target, options }); }
    disconnect() { this.targets = []; }
  }
  const window = {
    get devicePixelRatio() { return env.dpr; },
    getComputedStyle(target) {
      counts.styleReads += 1;
      log.push("style");
      return { font: target === current.para ? env.font : "", lineHeight: target === current.para ? env.lineHeight : "" };
    },
    matchMedia(query) {
      const entry = { query, listeners: [] };
      entry.addEventListener = (type, listener) => { if (type === "change") entry.listeners.push(listener); };
      entry.removeEventListener = (type, listener) => { entry.listeners = entry.listeners.filter((item) => item !== listener); };
      media.push(entry);
      return entry;
    },
    ResizeObserver: FakeResizeObserver,
  };
  const document = {
    addEventListener: (name, handler) => docHandlers.set(name, handler),
    removeEventListener: (name) => docHandlers.delete(name),
    fonts: {
      addEventListener: (name, handler) => fontHandlers.set(name, handler),
      removeEventListener: (name) => fontHandlers.delete(name),
    },
  };
  const T = T0 ? T0({ window, document }) : loadSource(H.topologySource(), { window, document });
  T.attach(container, null, null, {});
  const page = {
    T,
    env,
    counts,
    log,
    fills,
    handlers,
    docHandlers,
    fontHandlers,
    media,
    observers,
    container,
    rootStyle,
    get band() { return current; },
    get pin() { return current.para; },
    // The page's redraw: new markup in place, then mount.
    draw(fabric, options = {}, placed = null) {
      const html = T.render(fabric, options);
      current = newBand(visibleOf(html));
      placed?.();
      T.mount(container);
      return html;
    },
    // The band's observer reports the band's size (the empty measurer, one
    // em by one line, keeps its own).
    resize(inline, block = 49) {
      for (const observer of observers) {
        for (const { target } of observer.targets) {
          if (target !== current.measurer) observer.callback([{ target, borderBoxSize: [{ inlineSize: inline, blockSize: block }] }]);
        }
      }
    },
    // A new font size or line height: the browser reports the empty
    // measurer's own new size, and only where it is observed.
    textResized() {
      for (const observer of observers) {
        for (const { target } of observer.targets) {
          if (target === current.measurer) observer.callback([{ target, borderBoxSize: [{ inlineSize: 17, blockSize: 21.25 }] }]);
        }
      }
    },
    element: (ref) => ({
      dataset: { [ref.key]: ref.id },
      closest(selector) {
        if (selector === ".fabric-topology") return root;
        return /\[data-topo-/.test(selector) ? this : null;
      },
    }),
    escape: () => docHandlers.get("keydown")?.({ key: "Escape", target: null }),
    lastFill() {
      assert.ok(fills.length, "the band was measured");
      return fills[fills.length - 1].texts;
    },
  };
  return page;
}

// A topology view whose sentence builder records every list it returns.
function recorded(calls) {
  const source = H.topologySource();
  assert.equal(source.split(BUILDER).length - 1, 1, "the sentence builder is one function");
  const wrapped = source.replace(BUILDER, "function traceSentences(model, view = {}) { const made = traceSentencesBuilt(model, view); calls.lists.push(made); return made; }\n  function traceSentencesBuilt(model, view = {}) {");
  return (context) => loadSource(wrapped, { ...context, calls });
}

// Every sentence the band shows in one drawing, through the real handlers:
// the sentence the drawing starts with; every drawn item hovered and focused
// with no pin and with a pin held (also the pin the drawing starts with);
// every item that can be pinned, pinned.
function producible(page, fabric, options) {
  const { handlers, element } = page;
  const html = page.draw(fabric, options);
  const sentences = new Set([visibleOf(html)]);
  const refs = drawnRefs(html);
  const pinnable = refs.filter((ref) => ref.kind !== "group");
  const see = () => sentences.add(page.pin.textContent);
  const visit = () => {
    for (const ref of refs) {
      handlers.get("mouseover")({ target: element(ref) });
      see();
      handlers.get("mouseout")({ target: element(ref), relatedTarget: null });
      see();
      handlers.get("focusin")({ target: element(ref) });
      see();
      handlers.get("focusout")({ target: element(ref), relatedTarget: null });
      see();
    }
  };
  visit();
  page.escape();
  see();
  visit();
  for (const ref of pinnable) {
    handlers.get("click")({ target: element(ref) });
    see();
  }
  // Force a pin instead of toggling off the sole item in a host-only drawing.
  page.escape();
  if (pinnable.length) handlers.get("click")({ target: element(pinnable[0]) });
  visit();
  sentences.delete("");
  return { html, sentences: [...sentences] };
}

// The sentences the measure needs: every distinct one that is not the start
// of another the drawing can show (sorted, a string's longer continuations
// follow it directly).
function needed(sentences) {
  const sorted = [...new Set(sentences)].sort();
  return sorted.filter((text, index) => !(index + 1 < sorted.length && sorted[index + 1].startsWith(text)));
}

// Every sentence the band shows is measured, or is the start of one that is;
// every measured sentence is one the band can show; each once, no limit.
function assertCovered(name, measured, sentences) {
  assert.equal(new Set(measured).size, measured.length, `${name}: no sentence twice`);
  const kept = new Set(measured);
  const sorted = [...kept].sort();
  const covered = (text) => {
    if (kept.has(text)) return true;
    let low = 0;
    let high = sorted.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (sorted[mid] < text) low = mid + 1;
      else high = mid;
    }
    return low < sorted.length && sorted[low].startsWith(text);
  };
  const missing = sentences.filter((text) => !covered(text));
  assert.deepEqual(missing.slice(0, 3), [], `${name}: ${missing.length} sentences the band can show are not measured`);
  const shown = new Set(sentences);
  for (const text of measured) assert.ok(shown.has(text), `${name}: a measured sentence the band can show: ${text}`);
  assert.equal(measured.length, needed(sentences).length, `${name}: each distinct sentence measured, no limit`);
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

function bayCount(html) {
  return drawnRefs(html).filter((ref) => ref.kind === "bay").length;
}

// Round 1's adversarial labels: bays 00-07 long runs of narrow letters, bay
// 08 fewer, wide ones. A sample by character count misses bay 08.
function adversarial() {
  const fabric = H.twoHbaCascade().fabric;
  const bay = (slot) => fabric.nodes.find((item) => item.id === `bay:${slot}`);
  for (let index = 0; index < 8; index += 1) bay(index).label = `Bay ${String(index).padStart(2, "0")} ${"i ".repeat(80 + (index % 2))}`;
  bay(8).label = `Bay 08 ${"W".repeat(140)}`;
  return fabric;
}

test("every sentence the band shows, through hover, focus, a pin and a held pin, is measured, from small drawings to 1,696 bays drawn in full", () => {
  const shelf = bigShelf();
  const sixteen = H.eightHbaSixteenShelves().fabric;
  const plan = H.loadTopology({ document: { addEventListener() {} } });
  const cases = [
    ["default system, 1,100 px", H.twoHbaCascade().fabric, { width: 1100 }, 48],
    ["default system, 322 px", H.twoHbaCascade().fabric, { width: 322 }, 48],
    ["eight-lane wide port", widePort(), { width: 1100 }, 48],
    ["four shelves, collapsed", H.fourShelves().fabric, { width: 1100 }, null],
    ["1,024-bay shelf, collapsed", shelf, { width: 1100 }, 0],
    // A bay inside the collapsed shelf, selected elsewhere on the page: its
    // pinned sentence is shown although the bay is not drawn.
    ["1,024-bay shelf, collapsed, a hidden bay selected", shelf, { width: 1100, selection: { kind: "node", id: "bay:999" } }, 0],
    ["1,024-bay shelf, drawn in full", shelf, { width: 1100, expanded: expandedAll(plan, shelf) }, 1024],
    ["sixteen shelves, 1,696 bays drawn in full", sixteen, { width: 1100, expanded: expandedAll(plan, sixteen) }, 1696],
  ];
  for (const [name, fabric, options, bays] of cases) {
    const calls = { lists: [] };
    const page = fakePage(recorded(calls));
    const { html, sentences } = producible(page, fabric, options);
    if (bays !== null) assert.equal(bayCount(html), bays, `${name}: bays drawn`);
    assert.equal(page.fills.length, 1, `${name}: one measure for the drawing`);
    const measured = page.lastFill();
    // What the measurer holds is the builder's whole list, in its order.
    assert.equal(calls.lists.length, 1, `${name}: one build`);
    assert.deepEqual(measured, [...calls.lists[0]], `${name}: the builder's list, not a part of it`);
    assertCovered(name, measured, sentences);
    assert.ok(measured.includes(longest(sentences)), `${name}: the longest sentence is measured`);
    if (options.selection) assert.ok(measured.includes(visibleOf(html)) && /^Pinned: Bay 999,/.test(visibleOf(html)), `${name}: the hidden pin's sentence is measured`);
    if (bays === 1696) assert.ok(measured.length > 2 * 1696, `${measured.length} sentences`);
  }
  const page = fakePage();
  page.draw(widePort(), { width: 1100 });
  const api = H.loadTopology();
  const model = api.buildTopologyModel(widePort());
  // The cascade's wide port (PHYs 24-31 down to 0-7), not the HBA's cable.
  const cables = model.links.filter((link) => link.lanes?.count === 8 && link.lanes.sourcePhys[0] === 24);
  assert.equal(cables.length, 2, "one level-1 to level-2 cable per HBA");
  for (const cable of cables) {
    // Full SAS detail moved to the one hover card, not the short trace band.
    const card = api.cardFor(model, { kind: "link", id: cable.id });
    assert.match(card.rows.find((row) => row.label === "PHYs").value, /^24–31 at .+ ↔ 0–7 at .+$/);
  }
});

test("the held height is the tallest laid-out sentence the drawing can show, rounded up: a sample by length would miss it", () => {
  for (const width of [322, 390]) {
    const page = fakePage(null, { width });
    const { sentences } = producible(page, adversarial(), { width: 1100 });
    const heights = sentences.map((text) => lineLayout(text, width));
    const tallest = Math.max(...heights);
    const winner = sentences[heights.indexOf(tallest)];
    // The tallest is bay 08's wide sentence, which is not among the eight longest.
    assert.match(winner, /Bay 08 W{140}/);
    const byLength = [...sentences].sort((a, b) => b.length - a.length);
    assert.ok(byLength.indexOf(winner) >= 8, `${width} px: bay 08 is ${byLength.indexOf(winner) + 1}th by length`);
    assert.equal(page.band.held(), `${Math.ceil(tallest)}px`, `${width} px: the held height is the tallest sentence's`);
    assert.equal(page.fills.length, 1);
    assert.equal(page.fills[0].width, width);
  }
});

test("the held height is rounded up, never down", () => {
  for (const [measured, held] of [[88.4, "89px"], [88.6, "89px"], [88, "88px"], [20.01, "21px"]]) {
    const page = fakePage(null, { layout: () => measured });
    page.draw(H.twoHbaCascade().fabric, { width: 1100 });
    assert.equal(page.band.held(), held, `${measured} px measured`);
  }
});

test("hover, focus, a pin without a redraw, a pointer leaving and Esc measure nothing, read no layout or style and write no height", () => {
  const calls = { lists: [] };
  const page = fakePage(recorded(calls));
  const html = page.draw(H.twoHbaCascade().fabric, { width: 1100 });
  assert.equal(page.counts.measures, 1);
  assert.equal(calls.lists.length, 1, "one build per drawing");
  const before = { ...page.counts };
  page.log.length = 0;
  const refs = drawnRefs(html);
  const { handlers, element } = page;
  for (const ref of refs) {
    handlers.get("mouseover")({ target: element(ref) });
    handlers.get("focusin")({ target: element(ref) });
    handlers.get("mouseout")({ target: element(ref), relatedTarget: null });
    handlers.get("focusout")({ target: element(ref), relatedTarget: null });
  }
  // Pins that do not redraw (a link pin asks the page to redraw; this page has no hook).
  for (const ref of refs.filter((item) => item.kind !== "group")) handlers.get("click")({ target: element(ref) });
  for (const ref of refs) handlers.get("mouseover")({ target: element(ref) });
  page.escape();
  assert.match(page.pin.textContent, /^Hover or focus/);
  assert.deepEqual(page.log, [], "nothing read or written but the visible text");
  assert.deepEqual({ ...page.counts }, before);
  assert.equal(calls.lists.length, 1, "no sentence list is built outside a drawing");
  assert.equal(page.band.measurer.children.length, 0);
});

test("the band is measured once per drawing, width, font and resolution, and not again for the same", () => {
  const page = fakePage();
  const fabric = H.twoHbaCascade().fabric;
  const html = page.draw(fabric, { width: 1100 });
  const step = (name, expected, action) => {
    const before = page.counts.measures;
    action();
    assert.equal(page.counts.measures - before, expected, `${name}: ${expected} measure(s)`);
    assert.ok(page.band.held(), `${name}: the band in place holds a height`);
    assert.equal(page.band.measurer.children.length, 0, `${name}: the measurer is empty`);
  };
  assert.equal(page.counts.measures, 1, "the first drawing");
  const first = page.band.held();
  const link = drawnRefs(html).find((ref) => ref.kind === "link");
  step("a link pin redraw", 0, () => {
    page.handlers.get("click")({ target: page.element(link) });
    page.draw(fabric, { width: 1100 });
  });
  assert.match(page.pin.textContent, /^Pinned: /);
  assert.equal(page.band.held(), first, "the same height, written on the new band without a measure");
  step("a refresh with the same labels", 0, () => page.draw(H.twoHbaCascade().fabric, { width: 1100 }));
  step("Fit", 0, () => {
    page.handlers.get("click")({ target: { closest: (selector) => (selector === "[data-topology-fit]" ? {} : null) } });
    page.draw(fabric, { width: 1100 });
  });
  step("Esc and its redraw", 0, () => {
    page.escape();
    page.draw(fabric, { width: 1100 });
  });
  step("the band's own block size only", 0, () => page.resize(340, 71));
  step("a new band width", 1, () => {
    page.env.width = 300;
    page.resize(300);
  });
  step("back to a width already measured", 0, () => {
    page.env.width = 340;
    page.resize(340);
  });
  assert.equal(page.band.held(), first);
  step("a redraw at that width", 0, () => page.draw(fabric, { width: 1100 }));
  step("a new font, the band keeping its size", 1, () => {
    page.env.font = "18px sans-serif";
    page.textResized();
  });
  step("a redraw with that font", 0, () => page.draw(fabric, { width: 1100 }));
  step("a new line height, seen at a redraw", 1, () => {
    page.env.lineHeight = "24px";
    page.draw(fabric, { width: 1100 });
  });
  assert.equal(typeof page.fontHandlers.get("loadingdone"), "function", "a font load is listened for");
  step("a web font loaded", 1, () => page.fontHandlers.get("loadingdone")());
  step("a new resolution", 1, () => {
    page.env.dpr = 2;
    for (const entry of [...page.media]) for (const listener of entry.listeners) listener({ matches: false });
  });
  step("a redraw at that resolution", 0, () => page.draw(fabric, { width: 1100 }));
  // A new drawing: other sentences.
  const shelves = H.fourShelves().fabric;
  const keys = expandedAll(page.T, shelves);
  step("another system", 1, () => page.draw(shelves, { width: 1100 }));
  step("the shelves expanded", 1, () => page.draw(shelves, { width: 1100, expanded: keys }));
  assert.ok(bayCount(page.T.renderTopology(page.T.drawnModel(), {})) > 0, "bays drawn");
  step("the same expansion redrawn", 0, () => page.draw(shelves, { width: 1100, expanded: keys }));
  step("collapsed again", 1, () => page.draw(shelves, { width: 1100 }));
});

test("a pin the drawing does not show adds its sentence and is measured with the drawing", () => {
  const shelf = bigShelf();
  const page = fakePage();
  page.draw(shelf, { width: 1100 });
  assert.equal(page.counts.measures, 1);
  assert.ok(!page.lastFill().some((text) => text.startsWith("Pinned: Bay 999,")));
  page.draw(shelf, { width: 1100, selection: { kind: "node", id: "bay:999" } });
  assert.equal(page.counts.measures, 2, "another sentence: measured again");
  assert.match(page.pin.textContent, /^Pinned: Bay 999,/);
  assert.ok(page.lastFill().includes(page.pin.textContent));
  page.draw(shelf, { width: 1100, selection: { kind: "node", id: "bay:999" } });
  assert.equal(page.counts.measures, 2, "the same pin redrawn: no measure");
});

test("the band holds one visible sentence and an empty measurer: no sentence is in its markup, at 1,696 bays drawn in full", () => {
  const sixteen = H.eightHbaSixteenShelves().fabric;
  const page = fakePage();
  const html = page.draw(sixteen, { width: 1100, expanded: expandedAll(page.T, sixteen) });
  assert.equal(bayCount(html), 1696);
  assert.equal((html.match(/data-topology-trace>/g) || []).length, 1, "one band");
  const inner = band(html);
  const visible = visibleOf(html);
  assert.equal(inner, `<p class="fabric-topology-pin subtle" data-topology-pin>${visible}</p><div class="fabric-topology-trace-measure" data-topology-measure aria-hidden="true"></div>`);
  assert.ok(inner.length - visible.length < 200, `band markup: ${inner.length} characters`);
  assert.doesNotMatch(html, /data-topology-sizer|fabric-topology-trace-sizer/);
  assert.ok(page.lastFill().length > 2 * 1696, "every sentence measured");
  assert.equal(page.band.measurer.children.length, 0, "the measurer is emptied after the measure");
  // Its elements are the visible sentence's own kind.
  assert.ok(page.fills[0].elements.every((item) => item.tag === "P" && item.className === "fabric-topology-pin subtle"));
  // A pin redraw changes only the visible text.
  const link = drawnRefs(html).find((ref) => ref.kind === "link");
  page.handlers.get("click")({ target: page.element(link) });
  const pinned = page.draw(sixteen, { width: 1100, expanded: expandedAll(page.T, sixteen) });
  const blank = (text) => text.replace(/data-topology-pin>[^<]*</, "data-topology-pin><");
  assert.equal(blank(band(pinned)), blank(inner));
});

test("the held height is in place before mount puts the view and the page back", () => {
  const page = fakePage();
  const box = {
    scrollLeftValue: 0,
    scrollTopValue: 0,
    querySelector: (selector) => (selector === ".fabric-topology-svg" ? { setAttribute: (name) => page.log.push(`room ${name}`) } : null),
    getBoundingClientRect: () => ({ left: 0, top: 0 }),
  };
  for (const key of ["clientWidth", "clientHeight"]) Object.defineProperty(box, key, { get() { page.log.push(`box ${key}`); return 1000; } });
  for (const key of ["scrollLeft", "scrollTop"]) {
    Object.defineProperty(box, key, { get() { return box[`${key}Value`]; }, set(value) { page.log.push(`scroll ${key}`); box[`${key}Value`] = value; } });
  }
  page.container.box = box;
  const fabric = H.twoHbaCascade().fabric;
  for (const name of ["the first drawing (a measure)", "a redraw (held from the measure)"]) {
    page.draw(fabric, { width: 1100 }, () => { page.log.length = 0; });
    const mountWork = page.log.findIndex((entry) => /^(room|scroll|box) /.test(entry));
    const hold = page.log.findIndex((entry) => entry.startsWith("hold "));
    assert.ok(mountWork >= 0, `${name}: mount sized the room: ${page.log.join(", ")}`);
    assert.ok(hold >= 0 && hold < mountWork, `${name}: held before mount's work: ${page.log.join(", ")}`);
  }
});

test("a band with no width yet is not measured and nothing is kept; the first width it gets is", () => {
  const page = fakePage(null, { width: 0 });
  const fabric = H.twoHbaCascade().fabric;
  page.draw(fabric, { width: 1100 });
  assert.equal(page.counts.measures, 0);
  assert.equal(page.band.held(), null);
  page.draw(fabric, { width: 1100 });
  assert.equal(page.counts.measures, 0, "a zero width is not kept as measured");
  page.env.width = 340;
  page.resize(340);
  assert.equal(page.counts.measures, 1);
  assert.ok(page.band.held());
  assert.equal(page.band.measurer.children.length, 0);
});

test("a name with quotes, markup, an ampersand and a backslash reaches the measurer only as text", () => {
  const name = "Bay 03 \"front\" 'left' <mark>x</mark> & rack\\shelf";
  const fabric = H.twoHbaCascade().fabric;
  fabric.nodes.find((item) => item.id === "bay:3").label = name;
  const page = fakePage();
  const html = page.draw(fabric, { width: 1100 });
  assert.doesNotMatch(band(html), /<mark|rack/, "no name in the band's markup");
  assert.equal(page.counts.markupWrites, 0, "no markup written into the measurer");
  const ref = { kind: "bay", id: "bay:3", key: "topoBay" };
  page.handlers.get("click")({ target: page.element(ref) });
  page.handlers.get("mouseout")({ target: page.element(ref), relatedTarget: null });
  const pinned = page.pin.textContent;
  page.handlers.get("focusin")({ target: page.element(ref) });
  const held = page.pin.textContent;
  assert.ok(held.includes(name) && pinned.includes(name));
  for (const sentence of [held, pinned]) assert.equal(page.lastFill().filter((text) => text === sentence).length, 1, `measured as text: ${sentence}`);
  assert.ok(page.fills[0].elements.every((item) => item.tag === "P"));
});

test("a host-only fabric with a long host label: the host's held-pin focus sentence is measured", () => {
  const fabric = H.coreFabric();
  fabric.nodes[0].label = `synthetic-host-${"segment-".repeat(30)}end`;
  const page = fakePage();
  const { sentences } = producible(page, fabric, { width: 1100 });
  const host = { kind: "node", id: "host", key: "topoNode" };
  // A click on the pinned item clears it (approved design), so start unpinned.
  page.escape();
  page.handlers.get("click")({ target: page.element(host) });
  assert.match(page.pin.textContent, /^Pinned: synthetic-host-/);
  page.handlers.get("focusin")({ target: page.element(host) });
  assert.match(page.pin.textContent, /^Tracing: synthetic-host-(?:segment-){30}end.*\. Pinned item kept; Esc clears\.$/);
  assert.ok(page.lastFill().includes(page.pin.textContent));
  assertCovered("host only", page.lastFill(), sentences);
});

test("the band's sentences are written once: a change to the wording changes the visible sentence and the measured ones alike", () => {
  const source = H.topologySource();
  for (const words of ["Tracing: ", "Pinned: ", " Pinned item kept; Esc clears.", ". Click it again, click the background or press Esc to clear."]) {
    assert.equal(source.split(words).length - 1, 1, `${JSON.stringify(words)} is written once`);
  }
  const changed = source.replace("Tracing: ", "Following: ").replace(" Pinned item kept; Esc clears.", " The pin stays; Esc clears it.");
  const page = fakePage((context) => loadSource(changed, context));
  const { sentences } = producible(page, H.twoHbaCascade().fabric, { width: 1100 });
  const measured = page.lastFill();
  assert.ok(measured.some((text) => /^Following: .* The pin stays; Esc clears it\.$/.test(text)), JSON.stringify(measured.slice(0, 5)));
  assert.ok(measured.every((text) => !text.includes("Pinned item kept")));
  assert.ok(sentences.some((text) => /^Following: .* The pin stays; Esc clears it\.$/.test(text)), "the visible sentence has the new words too");
  assert.ok(measured.includes(longest(sentences)));
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
  const marks = new Set(["[data-topo-node]", "[data-topo-link]", "[data-topo-cross]", "[data-topo-bay]", "[data-topo-group]", "[data-topo-port]"]);
  const root = new Proxy({}, {
    get(target, key) {
      if (key === "querySelector") return (selector) => { if (!["[data-topology-pin]", "[data-topology-tip]", "[data-topology-clear]"].includes(selector)) log.push(`query ${selector}`); return selector === "[data-topology-pin]" ? pin : null; };
      if (key === "querySelectorAll") return (selector) => { if (!marks.has(selector)) log.push(`query all ${selector}`); return []; };
      log.push(`root get ${String(key)}`);
      return undefined;
    },
  });
  const handlers = new Map();
  const docHandlers = new Map();
  const container = {
    dataset: {},
    querySelector: (selector) => { if (selector !== ".fabric-topology") log.push(`container query ${selector}`); return selector === ".fabric-topology" ? root : null; },
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  const T = H.loadTopology({ document: { addEventListener: (name, handler) => docHandlers.set(name, handler) } });
  T.attach(container, null, null, {});
  const html = T.render(H.twoHbaCascade().fabric, { width: 1100 });
  log.length = 0;
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

test("the measurer is an out-of-flow box on the band's one cell that lays sentences out as the visible one; the band holds the height over its floor", () => {
  const css = fs.readFileSync(CSS_PATH, "utf8");
  const bandRule = cssRule(css, ".fabric-topology .fabric-topology-trace");
  assert.match(bandRule, /display:\s*grid;/);
  assert.match(bandRule, /grid-template-columns:\s*minmax\(0, 1fr\);/);
  // The floor stays, and the held height is the measured text plus the band's
  // own padding and border.
  const minHeight = /min-height:\s*([^;]+);/.exec(bandRule)?.[1];
  assert.equal(minHeight, "max(var(--topology-trace-band), calc(var(--topology-trace-held, 0px) + 9px))");
  const padding = /padding:\s*(\d+)px 0;/.exec(bandRule);
  const border = /border-top:\s*(\d+)px /.exec(bandRule);
  assert.equal(2 * Number(padding[1]) + Number(border[1]), 9, "9 px: the band's padding and border");
  assert.match(cssRule(css, ".fabric-topology-trace > *,\n.fabric-topology-trace-measure > *"), /grid-area:\s*1 \/ 1;\s*min-width:\s*0;\s*margin:\s*0;/, "the measured sentences sit as the visible one does");
  const measure = cssRule(css, ".fabric-topology-trace-measure");
  for (const rule of [/position:\s*absolute;/, /grid-area:\s*1 \/ 1;/, /inset-inline:\s*0;/, /display:\s*grid;/, /grid-template-columns:\s*minmax\(0, 1fr\);/,
    /visibility:\s*hidden;/, /user-select:\s*none;/, /pointer-events:\s*none;/, /contain:\s*layout paint style;/]) {
    assert.match(measure, rule);
  }
  // No property of its own changes how text is laid out, and nothing keeps it from layout.
  for (const body of [measure, cssRule(css, ".fabric-topology-trace > *,\n.fabric-topology-trace-measure > *")]) {
    assert.doesNotMatch(body, /(?:^|[\s;])(?:font[\w-]*|line-height|letter-spacing|word-[\w-]+|white-space|overflow-wrap|text-[\w-]+|hyphens|tab-size)\s*:/, body);
    assert.doesNotMatch(body, /contain:\s*[^;]*\b(?:strict|size|content)\b|display:\s*none|content-visibility/, body);
  }
  assert.doesNotMatch(css, /fabric-topology-trace-sizer/);
  for (const rule of [bandRule, cssRule(css, ".fabric-topology .fabric-topology-pin")]) {
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
  const first = current;
  current = make();
  T.render(H.twoHbaCascade().fabric, { width: 1100 });
  T.mount(container);
  assert.equal(observers.length, 1, "one observer for the page");
  assert.deepEqual(observers[0].targets.map((item) => item.target), [current.trace]);
  observers[0].callback([{ target: current.trace, borderBoxSize: [{ blockSize: 69, inlineSize: 340 }] }]);
  assert.equal(current.style.get("--topology-trace-height"), "69px");
  assert.equal(first.style.get("--topology-trace-height"), "89px");
  const disconnects = observers[0].disconnects;
  T.mount(container);
  assert.equal(observers[0].disconnects, disconnects);
  observers[0].callback([{ target: current.trace }]);
  assert.equal(current.style.get("--topology-trace-height"), "69px");
  T.release();
  assert.deepEqual(observers[0].targets, [], "leaving the mode stops observing");
  const bare = make();
  const plain = { dataset: {}, querySelector: (selector) => (selector === "[data-topology-trace]" ? bare.trace : null), addEventListener() {} };
  const U = H.loadTopology({ document: { addEventListener() {} } });
  U.attach(plain, null, null, {});
  U.render(H.twoHbaCascade().fabric, { width: 1100 });
  U.mount(plain);
  assert.equal(bare.style.size, 0);
});

test("a redraw at a new band width holds that width's height before mount reads the box; a width already measured measures nothing", () => {
  const page = fakePage();
  const box = {
    scrollLeft: 0,
    scrollTop: 0,
    clientHeight: 600,
    querySelector: () => ({ setAttribute() {} }),
    getBoundingClientRect: () => ({ left: 0, top: 0 }),
  };
  const seen = [];
  Object.defineProperty(box, "clientWidth", { get() { seen.push(page.band.held()); return 1000; } });
  page.container.box = box;
  const fabric = H.twoHbaCascade().fabric;
  page.draw(fabric, { width: 1100 });
  const sentences = page.lastFill();
  const exact = (width) => `${Math.ceil(Math.max(...sentences.map((text) => lineLayout(text, width))))}px`;
  assert.notEqual(exact(170), exact(340), "the two widths need different heights");
  assert.equal(page.band.held(), exact(340));
  const redraw = (width, name) => {
    // The redraw itself changes the band's width; no observer report has come for the new band.
    page.env.width = width;
    const before = page.counts.measures;
    page.draw(fabric, { width: 1100 }, () => { seen.length = 0; });
    assert.ok(seen.length > 0, `${name}: mount read the box`);
    assert.equal(seen[0], exact(width), `${name}: mount's first box read sees the height for ${width} px`);
    return page.counts.measures - before;
  };
  assert.equal(redraw(170, "narrower"), 1, "a new width is measured, before mount");
  assert.equal(page.band.measurer.children.length, 0);
  assert.equal(redraw(170, "the same width again"), 0, "the same width: nothing measured");
  assert.equal(redraw(340, "back to the first width"), 0, "a width already measured: nothing measured");
  page.env.width = 340;
  const reads = page.counts.reads;
  page.draw(fabric, { width: 1100 }, () => { page.log.length = 0; });
  assert.equal(page.counts.reads - reads, 1, "one read of the band's width per redraw");
  // The new band holds the page's height before that read, so the page is
  // laid out as long as it was; the same width writes nothing more.
  assert.deepEqual(page.log.filter((entry) => /^(hold |read )/.test(entry)), [`hold ${exact(340)}`, "read band.rect"]);
});

test("a new font size or line height is seen while the band keeps its size: the empty measurer is one em by one line and observed", () => {
  const page = fakePage();
  page.draw(adversarial(), { width: 1100 });
  const texts = page.lastFill();
  const held = Number.parseFloat(page.band.held());
  const beforeIdle = lineLayout(page.pin.textContent, page.env.width);
  // A larger default font size: every sentence is taller, but the idle
  // sentence and the floor still fit the held height, so the band itself
  // does not change size and no band size is reported.
  page.env.font = "17px sans-serif";
  page.env.lineHeight = "21.25px";
  page.env.layout = (text, width) => lineLayout(text, width) * 17 / 16;
  const newIdle = page.env.layout(page.pin.textContent, page.env.width);
  assert.ok(beforeIdle < held && newIdle < held && 3 * 21.25 < held, "the band keeps its size");
  const expected = `${Math.ceil(Math.max(...texts.map((text) => page.env.layout(text, page.env.width))))}px`;
  assert.notEqual(expected, page.band.held());
  const measures = page.counts.measures;
  const rootBefore = new Map(page.rootStyle);
  page.textResized();
  assert.equal(page.counts.measures - measures, 1, "measured again for the new font");
  assert.equal(page.band.held(), expected, "the held height is the new font's tallest sentence, before any hover");
  assert.deepEqual(page.rootStyle, rootBefore, "the measurer's size is not the band's height");
  assert.equal(page.band.measurer.children.length, 0);
  page.textResized();
  assert.equal(page.counts.measures - measures, 1, "the same font again: nothing measured");
  // Hover, focus and a pin of the tallest sentence then change only the text.
  const after = { ...page.counts };
  const wide = { kind: "bay", id: "bay:8", key: "topoBay" };
  page.handlers.get("click")({ target: page.element(wide) });
  page.handlers.get("focusin")({ target: page.element(wide) });
  page.handlers.get("mouseover")({ target: page.element(wide) });
  assert.deepEqual({ ...page.counts }, after);
  assert.equal(page.band.held(), expected);
  // What makes the browser report it: the measurer, observed, is one em by
  // one line whenever it is empty, which is always outside the measure.
  assert.ok(page.observers.some((observer) => observer.targets.some((item) => item.target === page.band.measurer)), "the measurer is observed");
  const css = fs.readFileSync(CSS_PATH, "utf8");
  const empty = cssRule(css, ".fabric-topology-trace-measure:empty");
  assert.match(empty, /inline-size:\s*1em;/, "one em wide while empty: follows the font size");
  assert.match(empty, /block-size:\s*1lh;/, "one line high while empty: follows the line height");
});

test("the held height of a width kept for each of eight widths; a ninth puts out the oldest, which is measured again on its return", () => {
  const page = fakePage();
  const fabric = H.twoHbaCascade().fabric;
  page.draw(fabric, { width: 1100 });
  const at = (width) => {
    page.env.width = width;
    page.resize(width);
    return page.counts.measures;
  };
  const heights = new Map([[340, page.band.held()]]);
  for (let width = 341; width <= 348; width += 1) {
    at(width);
    heights.set(width, page.band.held());
  }
  assert.equal(page.counts.measures, 9, "nine widths, nine measures");
  assert.equal(at(348), 9, "the newest width is kept");
  assert.equal(at(342), 9, "a width in the middle is kept");
  assert.equal(at(341), 9, "the second oldest is kept");
  assert.equal(at(340), 10, "the oldest of nine widths was put out and is measured again");
  assert.equal(page.band.held(), heights.get(340));
  assert.equal(at(341), 11, "returning to 340 put out the next oldest, 341");
  assert.equal(at(348), 11, "the newest is still kept");
  assert.equal(page.band.held(), heights.get(348));
  assert.equal(page.band.measurer.children.length, 0);
});
