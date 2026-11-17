"use strict";

// Storage Fabric Topology mode (#937): layout and interaction, checked on the
// layout model's own coordinates. The dense fixtures are the two the view is
// judged by: two HBAs each with a level-1 and a level-2 expander, and two HBAs
// with four dual-pathed shelves where one shelf cascades off another.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const H = require("./helpers/fabric_topology");
const G = require("./helpers/fabric_topology_geometry");

const CSS_PATH = path.join(H.ROOT, "app/static/sas_fabric_topology.css");
const ROOT_CSS = fs.readFileSync(path.join(H.ROOT, "app/static/style.css"), "utf8");

function topologyCss() {
  assert.ok(fs.existsSync(CSS_PATH), "the Topology rules live in app/static/sas_fabric_topology.css");
  return fs.readFileSync(CSS_PATH, "utf8");
}

function fixtures(T, options = {}) {
  const four = H.fourShelves().fabric;
  const collapsed = T.buildTopologyModel(four, options);
  const expanded = Object.fromEntries([...collapsed.groups].map((group) => [group.expandKey, true]));
  return {
    "two HBAs, level-2 cascade": T.buildTopologyModel(H.twoHbaCascade().fabric, options),
    "four shelves, collapsed": collapsed,
    "four shelves, expanded": T.buildTopologyModel(four, { ...options, expanded }),
  };
}

function centre(item) {
  return item.x + item.w / 2;
}

function itemOf(model, id) {
  return model.nodeById.get(id) || model.groupById.get(id);
}

// ---- width ----------------------------------------------------------------

test("the layout takes the available width and spreads the tiers across it", () => {
  const T = H.loadTopology();
  const { fabric } = H.twoHbaCascade();
  const wide = T.buildTopologyModel(fabric, { width: 3600 });
  assert.equal(wide.width, 3600);
  const items = [...wide.nodes, ...wide.groups];
  const left = Math.min(...items.map((item) => item.x));
  const right = Math.max(...items.map((item) => item.x + item.w));
  assert.ok(right - left >= 0.75 * (3600 - 200), `the drawing spans ${right - left} of 3600 px`);
  for (const item of items) assert.ok(item.x >= 0 && item.x + item.w <= wide.width, `${item.id} is inside the drawing`);
  // Content wider than the box keeps its size and scrolls inside the box.
  const narrow = T.buildTopologyModel(fabric, { width: 900 });
  assert.ok(narrow.width > 900);
  for (const item of [...narrow.nodes, ...narrow.groups]) assert.ok(item.x >= 0 && item.x + item.w <= narrow.width);
});

test("Fit scales the whole drawing into the box and says when its text drops under 12 px", () => {
  const T = H.loadTopology();
  const model = T.buildTopologyModel(H.fourShelves().fabric, { width: 1000, height: 600 });
  const fitted = T.renderTopology(model, { fit: true });
  const svg = /<svg class="fabric-topology-svg[^"]*"[^>]*width="(\d+)" height="(\d+)" viewBox="0 0 (\d+) (\d+)"/.exec(fitted);
  assert.ok(svg, "the SVG keeps its full view box");
  assert.ok(Number(svg[1]) <= 1000 && Number(svg[2]) <= 600, `fitted to ${svg[1]} x ${svg[2]}`);
  assert.equal(Number(svg[3]), Math.ceil(model.width));
  const scale = Number(svg[1]) / Math.ceil(model.width);
  const notice = /data-topology-fit-notice[^>]*>([^<]*)</.exec(fitted);
  assert.ok(notice, "a notice says the text is smaller than 12 px");
  assert.match(notice[1], new RegExp(`${(12 * scale).toFixed(1)} px`));
  assert.match(notice[1], /under 12 px/);
  assert.match(fitted, /data-topology-fit="actual"[^>]*aria-pressed="true"|aria-pressed="true"[^>]*data-topology-fit="actual"/);
  // At actual size the control offers Fit, and no notice is shown.
  const actual = T.renderTopology(model, {});
  assert.match(actual, /data-topology-fit="fit"/);
  assert.doesNotMatch(actual, /data-topology-fit-notice/);
  // A drawing that already fits is never enlarged and needs no notice.
  const small = T.buildTopologyModel(H.twoHbaCascade().fabric, { width: 4000, height: 3000 });
  assert.doesNotMatch(T.renderTopology(small, { fit: true }), /data-topology-fit-notice/);
});

test("only Topology widens the page; the other four modes keep the capped column", () => {
  const css = topologyCss();
  assert.match(css, /body\.fabric-topology-mode \.fabric-page-shell\s*\{[^}]*max-width:\s*none/);
  const source = fs.readFileSync(path.join(H.ROOT, "app/static/sas_fabric_view.js"), "utf8");
  assert.match(source, /classList\?\.toggle\("fabric-topology-mode", state\.mode === "topology"\)/);
  assert.doesNotMatch(ROOT_CSS, /fabric-topology/, "style.css carries no Topology rules");
});

// ---- routing --------------------------------------------------------------

for (const width of [0, 3600]) {
  test(`routes never pass behind other boxes, never share a track and keep labels clear (width ${width || "default"})`, () => {
    const T = H.loadTopology();
    for (const [name, model] of Object.entries(fixtures(T, width ? { width } : {}))) {
      assert.deepEqual(G.linesBehindBoxes(model), [], `${name}: lines behind boxes`);
      assert.deepEqual(G.sharedTracks(model), [], `${name}: parallel segments under 10 px apart`);
      assert.deepEqual(G.cornerTouches(model), [], `${name}: corners touching another link`);
      assert.deepEqual(G.labelProblems(model), [], `${name}: labels`);
    }
  });
}

// Every parent's offset from the midpoint of its direct children's span:
// { outside, offset, sole } per parent; sole is true when no child has
// another parent.
function parentOffsets(model) {
  const result = [];
  for (const item of [...model.nodes]) {
    const out = [...(model.outgoing.get(item.id) || [])].filter((link) => link.points);
    const children = out.map((link) => itemOf(model, link.target)).filter(Boolean);
    if (!children.length) continue;
    const left = Math.min(...children.map((child) => child.x));
    const right = Math.max(...children.map((child) => child.x + child.w));
    const middle = centre(item);
    const outside = middle < left ? left - middle : middle > right ? middle - right : 0;
    const sole = out.every((link) => [...(model.incoming.get(link.target) || [])].filter((other) => other.points).length === 1);
    result.push({ id: item.id, outside, offset: Math.abs(middle - (left + right) / 2), sole });
  }
  return result;
}

function denseFixtures(T, width) {
  const eight = H.eightHbaSixteenShelves().fabric;
  const eightExpanded = Object.fromEntries([...T.buildTopologyModel(eight).groups].map((group) => [group.expandKey, true]));
  return {
    ...fixtures(T, { width }),
    "eight HBAs, sixteen shelves, collapsed": T.buildTopologyModel(eight, { width }),
    "eight HBAs, sixteen shelves, expanded": T.buildTopologyModel(eight, { width, expanded: eightExpanded }),
    "three-level cascade": T.buildTopologyModel(H.threeLevelCascade().fabric, { width }),
    "producer logical disks": T.buildTopologyModel(H.producerCases().linux_virtual.fabric, { width }),
  };
}

test("every parent sits over the span of its direct children, near its midpoint, and crossings are fewer than before", () => {
  const T = H.loadTopology();
  // Crossings measured the same way on the layout this pull request first drew.
  const before = { "two HBAs, level-2 cascade": 3, "four shelves, collapsed": 6, "four shelves, expanded": 8 };
  for (const width of [390, 1600, 3840]) {
    for (const [name, model] of Object.entries(denseFixtures(T, width))) {
      for (const parent of parentOffsets(model)) {
        assert.equal(parent.outside, 0, `${name} at ${width}: ${parent.id} is over its children`);
        // A parent whose children have no other parent sits at their midpoint
        // unless its neighbours' boxes are in the way; parents that share a
        // child sit side by side over it.
        assert.ok(parent.offset <= (parent.sole ? 40 : 200), `${name} at ${width}: ${parent.id} is ${parent.offset} px from its children's midpoint`);
      }
      const host = model.nodeById.get("host");
      const hbas = [...model.nodes].filter((item) => item.kind === "controller");
      assert.ok(centre(host) >= Math.min(...hbas.map((item) => item.x)) && centre(host) <= Math.max(...hbas.map((item) => item.x + item.w)), `${name}: host over the HBAs`);
      if (before[name] !== undefined) {
        const count = G.crossings(model);
        assert.ok(count < before[name] || count === 0, `${name}: ${count} crossings, ${before[name]} before`);
      }
    }
  }
  // The default cascade's level-1 expander is centred between its enclosure
  // and its level-2 expander, not 926 px to one side at 3840.
  const wide = T.buildTopologyModel(H.twoHbaCascade().fabric, { width: 3840 });
  for (const parent of parentOffsets(wide).filter((item) => item.id.includes("0a10") || item.id.includes("0b10"))) {
    assert.ok(parent.offset <= 1, `${parent.id}: ${parent.offset} px`);
  }
});

test("each HBA's chain has its own line colour, named in the legend, and the link kinds differ in the drawing", () => {
  const T = H.loadTopology();
  const { fabric } = H.fourShelves();
  const model = T.buildTopologyModel(fabric);
  const html = T.renderTopology(model, {});
  const chainOf = (id) => {
    const escaped = id.replace(/>/g, "&gt;");
    const match = new RegExp(`<polyline class="topo-link [^"]*chain-(\\d+)[^"]*" data-topo-link="${escaped.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`).exec(html);
    return match ? Number(match[1]) : null;
  };
  const chains = new Map();
  for (const link of model.links) {
    const owner = link.chain;
    assert.ok(owner, `${link.id} belongs to an HBA chain`);
    if (!chains.has(owner)) chains.set(owner, chainOf(link.id));
    assert.equal(chainOf(link.id), chains.get(owner), `${link.id} is drawn in its chain's colour`);
  }
  assert.deepEqual([...chains.keys()].sort(), ["controller:mpr0", "controller:mpr1"]);
  assert.notEqual(chains.get("controller:mpr0"), chains.get("controller:mpr1"));
  for (const [owner, index] of chains) {
    const legend = new RegExp(`data-topology-legend="chain-${index}"[^>]*>(?:<[^>]+>)*[^<]*${owner.replace("controller:", "")}`);
    assert.match(html, legend, `${owner} is named in the legend`);
  }
  // Colours come from the page's custom properties, and the link kinds differ by width or dash.
  const css = topologyCss();
  const rule = (selector) => {
    const match = new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`).exec(css);
    assert.ok(match, `${selector} has a rule`);
    const width = /stroke-width:\s*([\d.]+)/.exec(match[1])?.[1] || "";
    const dash = /stroke-dasharray:\s*([^;]+);/.exec(match[1])?.[1].trim() || "none";
    return `${width}/${dash}`;
  };
  const kinds = [".topo-link.is-lanes-reported", ".topo-link.is-lanes-unreported", ".topo-link.is-plain-link", ".topo-link.is-bundle"].map(rule);
  assert.equal(new Set(kinds).size, 4, kinds.join(" "));
  for (const match of css.matchAll(/\.topo-link\.chain-\d+\s*\{[^}]*stroke:\s*([^;]+);/g)) assert.match(match[1], /^var\(--[a-z0-9-]+\)$/);
});

// ---- interaction ------------------------------------------------------------

function scrollDom() {
  const handlers = new Map();
  let box = { scrollLeft: 0, scrollTop: 0 };
  const root = { querySelector: () => null, querySelectorAll: () => [] };
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === ".fabric-topology-scroll" ? box : selector === ".fabric-topology" ? root : null),
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  return { handlers, container, replace() { box = { scrollLeft: 0, scrollTop: 0 }; return box; }, get box() { return box; } };
}

test("selecting or pinning keeps the drawing's scroll position across the page's re-render", () => {
  const dom = scrollDom();
  const T = H.loadTopology({ document: { addEventListener() {} } });
  T.attach(dom.container, null, null, {});
  const { fabric } = H.fourShelves();
  T.render(fabric, {});
  T.mount(dom.container);
  dom.box.scrollLeft = 1450;
  dom.box.scrollTop = 220;
  // The page selects a far-away bay and redraws the map: the old scroll box
  // is still in the page while Topology renders, then it is replaced.
  T.render(fabric, { selection: { kind: "trace", id: "bay:300" } });
  dom.replace();
  T.mount(dom.container);
  assert.equal(dom.box.scrollLeft, 1450);
  assert.equal(dom.box.scrollTop, 220);
  // A new scope starts at the origin.
  const other = JSON.parse(JSON.stringify(fabric));
  other.system_id = "another-system";
  T.render(other, {});
  dom.replace();
  T.mount(dom.container);
  assert.equal(dom.box.scrollLeft, 0);
});

test("what the inspector shows as selected is marked in the drawing", () => {
  const T = H.loadTopology({ document: { addEventListener() {} } });
  const { fabric } = H.twoHbaCascade();
  const html = T.render(fabric, { selection: { kind: "trace", id: "bay:30" } });
  assert.match(html, /<g class="topo-bay[^"]*is-pinned[^"]*"[^>]*data-topo-bay="bay:30"/);
  assert.equal((html.match(/is-pinned/g) || []).length, 1);
  const node = T.render(fabric, { selection: { kind: "node", id: "controller:mpr1" } });
  assert.match(node, /<g class="topo-node[^"]*is-pinned[^"]*"[^>]*data-topo-node="controller:mpr1"/);
  assert.doesNotMatch(T.render(fabric, { selection: null }), /is-pinned/);
});

// ---- text -----------------------------------------------------------------

test("no fact is cut off where a wider box or a second line fits", () => {
  const T = H.loadTopology();
  const cases = H.producerCases();
  const notRead = H.coreFabric();
  for (const name of ["mpr0", "mpr1"]) H.addController(notRead, name);
  H.addBays(notRead, H.range(0, 24), [], [{ controller: "mpr0", state: "active" }]);
  notRead.raw.controller_read.counts.expanders = "not_read";
  notRead.raw.controller_read.counts.enclosures = "not_read";
  const models = {
    ...fixtures(T),
    "producer cascade": T.buildTopologyModel(cases.core_cascade.fabric),
    "producer SES": T.buildTopologyModel(cases.scale_ses.fabric),
    "producer logical disks": T.buildTopologyModel(cases.linux_virtual.fabric),
    "not read": T.buildTopologyModel(notRead),
  };
  for (const [name, model] of Object.entries(models)) {
    const html = T.renderTopology(model, {});
    const texts = [...html.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map((match) => match[1]);
    assert.deepEqual(texts.filter((text) => text.includes("…")), [], `${name}: shortened text`);
    for (const item of model.nodes) {
      // Lines sit inside their box.
      for (const line of item.lines) assert.ok(line.length * 8 + 20 <= item.w + 1, `${name}: ${item.id} line "${line}" fits ${item.w}`);
    }
  }
  // HBA lines keep both the linked PHY count and the wide-port groups.
  const hba = models["two HBAs, level-2 cascade"].nodeById.get("controller:mpr0");
  assert.ok(hba.lines.some((line) => /4 of 8 PHYs linked/.test(line)));
  assert.ok(hba.lines.some((line) => /^PHYs 0–3/.test(line)));
});

test("text that must be shortened is reachable by keyboard focus as well as by hover", () => {
  const handlers = new Map();
  const pin = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pin : null), querySelectorAll: () => [] };
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === ".fabric-topology" ? root : null),
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  const T = H.loadTopology({ document: { addEventListener() {} } });
  T.attach(container, null, null, {});
  const fabric = H.coreFabric();
  const long = `SYNTH-${"X".repeat(70)}`;
  const hba = H.addController(fabric, "mpr0");
  fabric.nodes.find((item) => item.id === hba.id).label = long;
  const html = T.render(fabric, {});
  assert.match(html, /…/, "an unbreakable 76-character name is shortened in its box");
  assert.ok(html.includes(`aria-label="${long}`), "its accessible name is whole");
  const element = { dataset: { topoNode: hba.id }, closest(selector) { return selector === ".fabric-topology" ? root : this; } };
  handlers.get("focusin")({ target: element });
  assert.ok(pin.textContent.includes(long), "keyboard focus shows the whole text on the page");
  handlers.get("focusout")({ target: element, relatedTarget: null });
  handlers.get("mouseover")({ target: element });
  assert.ok(pin.textContent.includes(long), "hover shows it too");
});

// ---- path marks -----------------------------------------------------------

function luminance(hex) {
  const value = hex.replace("#", "");
  const channels = [0, 2, 4].map((index) => parseInt(value.slice(index, index + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

function blend(fill, opacity, under) {
  const rgb = (hex) => [0, 2, 4].map((index) => parseInt(hex.replace("#", "").slice(index, index + 2), 16));
  const [f, u] = [rgb(fill), rgb(under)];
  return `#${f.map((c, index) => Math.round(c * opacity + u[index] * (1 - opacity)).toString(16).padStart(2, "0")).join("")}`;
}

test("path marks are at least 8 px, differ by shape, and contrast with every cell colour", () => {
  const T = H.loadTopology();
  const html = T.renderTopology(T.buildTopologyModel(H.producerCases().core_cascade.fabric), {});
  const marks = [...html.matchAll(/<(circle|rect|path) ([^>]*)class="topo-path-mark path-([a-z]+)"/g)];
  assert.ok(marks.length >= 16);
  const shapes = new Map();
  for (const [, tag, attributes, tone] of marks) {
    let size;
    if (tag === "circle") size = 2 * Number(/r="([\d.]+)"/.exec(attributes)[1]);
    else if (tag === "rect") size = Math.min(Number(/width="([\d.]+)"/.exec(attributes)[1]), Number(/height="([\d.]+)"/.exec(attributes)[1]));
    else {
      const numbers = [...attributes.matchAll(/-?[\d.]+/g)].map((match) => Number(match[0]));
      const xs = numbers.filter((_, index) => index % 2 === 0);
      const ys = numbers.filter((_, index) => index % 2 === 1);
      size = Math.min(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
    }
    assert.ok(size >= 8, `${tone} mark is ${size} px`);
    shapes.set(tone, `${tag}:${attributes.replace(/-?[\d.]+/g, "")}`);
  }
  assert.equal(new Set(shapes.values()).size, shapes.size, "each state has its own shape");

  // Contrast, from the page's own custom properties.
  const css = topologyCss();
  const vars = Object.fromEntries([...ROOT_CSS.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6});/g)].map((match) => [match[1], match[2]]));
  const resolve = (value) => vars[/var\(--([a-z0-9-]+)\)/.exec(value)[1]];
  const ruleFor = (selector) => new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`).exec(css)?.[1] || "";
  const groupFill = resolve(/fill:\s*([^;]+);/.exec(ruleFor(".topo-group > rect"))[1]);
  const cells = {};
  for (const state of ["bay-yes", "bay-no", "bay-unknown", "state-fault"]) {
    const body = ruleFor(`.topo-bay.${state} > rect`);
    const fill = /fill:\s*([^;]+);/.exec(body)?.[1].trim();
    const opacity = Number(/fill-opacity:\s*([\d.]+)/.exec(body)?.[1] ?? 1);
    cells[state] = !fill || fill === "none" ? groupFill : blend(resolve(fill), opacity, groupFill);
  }
  for (const tone of ["active", "standby", "fault", "missing", "mapped", "unmapped", "unknown", "member", "other"]) {
    const body = ruleFor(`.topo-path-mark.path-${tone}`);
    const paint = /(?:^|\s)(?:fill|stroke):\s*(var\(--[a-z0-9-]+\))/.exec(body);
    assert.ok(paint, `path-${tone} is painted from a custom property`);
    const colour = resolve(paint[1]);
    for (const [state, cell] of Object.entries(cells)) {
      const ratio = contrast(colour, cell);
      assert.ok(ratio >= 3, `path-${tone} ${colour} on ${state} ${cell}: ${ratio.toFixed(2)}:1`);
    }
  }
});

// ---- narrow screens -------------------------------------------------------

test("at 390 px the search box stays inside its panel", () => {
  const css = topologyCss();
  const input = /\.fabric-topology-search\s*\{([^}]*)\}/.exec(css)[1];
  assert.match(input, /box-sizing:\s*border-box/);
  assert.match(input, /max-width:\s*100%/);
  assert.match(input, /min-width:\s*0/);
  assert.doesNotMatch(input, /(?:^|\s)width:\s*\d+px/);
  const row = /\.fabric-topology-search-row\s*\{([^}]*)\}/.exec(css)[1];
  assert.match(row, /flex-wrap:\s*wrap/);
  assert.match(row, /min-width:\s*0/);
  assert.match(row, /max-width:\s*100%/);
});

// ---- the synthetic inputs agree with themselves ---------------------------

test("every test builder gives each HBA a linked-PHY count equal to the lanes its links report", () => {
  const builders = { "two HBAs, level-2 cascade": H.twoHbaCascade().fabric, "four shelves": H.fourShelves().fabric };
  if (H.eightHbaSixteenShelves) builders["eight HBAs, sixteen shelves"] = H.eightHbaSixteenShelves().fabric;
  if (H.threeLevelCascade) builders["three-level cascade"] = H.threeLevelCascade().fabric;
  for (const [name, fabric] of Object.entries(builders)) {
    for (const hba of fabric.nodes.filter((item) => item.kind === "controller")) {
      const lanes = fabric.links
        .filter((item) => item.source === hba.id && item.kind === "controller-expander" && item.reported_lanes)
        .flatMap((item) => item.reported_lanes.source_phys);
      assert.equal(hba.metrics.linked_phy_count, lanes.length, `${name}: ${hba.id} linked PHYs`);
      assert.equal(new Set(lanes).size, lanes.length, `${name}: ${hba.id} uses each PHY once`);
      assert.ok(lanes.every((phy) => phy < hba.metrics.phy_count), `${name}: ${hba.id} PHYs within its count`);
    }
  }
});

// ---- first view ---------------------------------------------------------------

function viewDom(clientWidth) {
  const handlers = new Map();
  const fit = { focused: 0, focus() { this.focused += 1; } };
  let box = { scrollLeft: 0, scrollTop: 0, clientWidth };
  const root = { querySelector: () => null, querySelectorAll: () => [] };
  const container = {
    dataset: {},
    querySelector: (selector) => {
      if (selector === ".fabric-topology-scroll") return box;
      if (selector === ".fabric-topology") return root;
      if (selector === "[data-topology-fit]") return fit;
      return null;
    },
    addEventListener: (name, handler) => handlers.set(name, handler),
  };
  return {
    handlers, container, fit,
    replace() { box = { scrollLeft: 0, scrollTop: 0, clientWidth }; return box; },
    get box() { return box; },
  };
}

function visible(dom, model, ids) {
  for (const id of ids) {
    const item = model.nodeById.get(id) || model.groupById.get(id);
    if (item.x < dom.box.scrollLeft - 0.5 || item.x + item.w > dom.box.scrollLeft + dom.box.clientWidth + 0.5) return false;
  }
  return true;
}

test("the host and every HBA are in view when Topology opens, after a shelf is expanded and after Actual size", () => {
  const document = { addEventListener() {}, removeEventListener() {} };
  const T = H.loadTopology({ document });
  const dom = viewDom(1100);
  T.attach(dom.container, null, null, { requestRender() {} });
  const { fabric } = H.fourShelves();
  const top = (model) => [...model.nodes].filter((item) => item.kind === "host" || item.kind === "controller").map((item) => item.id);
  // Opening: the drawing is wider than its 1100 px box; host and HBAs fit in it.
  let html = T.render(fabric, { width: 1100 });
  const opened = T.drawnModel();
  assert.ok(opened.width > 1100);
  T.mount(dom.container);
  assert.ok(visible(dom, opened, top(opened)), `scrolled to ${dom.box.scrollLeft}`);
  // The page says the drawing is wider than its box, next to Fit.
  assert.match(html, /<div class="fabric-topology-bar">[\s\S]*data-topology-fit[\s\S]*data-topology-wide-notice[^>]*>[^<]*wider than its box/);
  // Expanding a shelf: the shelf the operator expanded stays in view, and so do the host and HBAs.
  const group = opened.groups.find((item) => item.expandable);
  dom.box.scrollLeft = Math.max(0, group.x - 200);
  T.render(fabric, { width: 1100 });
  const expanded = { [group.expandKey]: true };
  html = T.render(fabric, { width: 1100, expanded });
  dom.replace();
  T.mount(dom.container);
  const after = T.drawnModel();
  const shown = after.groupById.get(group.id);
  assert.ok(shown.x < dom.box.scrollLeft + dom.box.clientWidth && shown.x + shown.w > dom.box.scrollLeft, "the expanded shelf is in view");
  assert.ok(visible(dom, after, top(after)), `host and HBAs in view at ${dom.box.scrollLeft}`);
  // Fit, then Actual size: back to a view with the host and HBAs.
  dom.handlers.get("click")({ target: { closest: (selector) => (selector === "[data-topology-fit]" ? dom.fit : null) } });
  T.render(fabric, { width: 1100, expanded });
  dom.replace();
  T.mount(dom.container);
  dom.box.scrollLeft = 5000;
  dom.handlers.get("click")({ target: { closest: (selector) => (selector === "[data-topology-fit]" ? dom.fit : null) } });
  html = T.render(fabric, { width: 1100, expanded });
  dom.replace();
  T.mount(dom.container);
  assert.ok(visible(dom, T.drawnModel(), top(T.drawnModel())), `Actual size scrolled to ${dom.box.scrollLeft}`);
  // A drawing that fits its box says nothing about scrolling.
  assert.doesNotMatch(T.render(H.twoHbaCascade().fabric, { width: 4000 }), /data-topology-wide-notice/);
});

test("Fit keeps keyboard focus on the Fit control after the redraw", () => {
  const T = H.loadTopology({ document: { addEventListener() {}, removeEventListener() {} } });
  const dom = viewDom(900);
  let renders = 0;
  T.attach(dom.container, null, null, { requestRender() { renders += 1; } });
  const { fabric } = H.fourShelves();
  T.render(fabric, { width: 900 });
  T.mount(dom.container);
  dom.handlers.get("click")({ target: { closest: (selector) => (selector === "[data-topology-fit]" ? dom.fit : null) } });
  assert.equal(renders, 1);
  T.render(fabric, { width: 900 });
  T.mount(dom.container);
  assert.equal(dom.fit.focused, 1, "the new Fit control has focus");
  T.render(fabric, { width: 900 });
  T.mount(dom.container);
  assert.equal(dom.fit.focused, 1, "only after a Fit click");
});

test("leaving the view removes its document and window listeners; one Esc handler however often it was attached", () => {
  const counts = () => ({ keydown: listeners.document.filter((item) => item.type === "keydown").length, resize: listeners.window.filter((item) => item.type === "resize").length });
  const listeners = { document: [], window: [] };
  const target = (where) => ({
    addEventListener(type, handler) { listeners[where].push({ type, handler }); },
    removeEventListener(type, handler) { listeners[where] = listeners[where].filter((item) => item.type !== type || item.handler !== handler); },
  });
  const sandboxWindow = { ...target("window"), setTimeout, clearTimeout };
  const T = H.loadTopology({ document: target("document"), window: sandboxWindow });
  let cleared = 0;
  const { fabric } = H.twoHbaCascade();
  for (let index = 0; index < 5; index += 1) {
    const dom = viewDom(900);
    T.attach(dom.container, null, null, { clearSelection() { cleared += 1; }, requestRender() {} });
    T.render(fabric, {});
    T.mount(dom.container);
  }
  assert.deepEqual(counts(), { keydown: 1, resize: 1 });
  listeners.document.find((item) => item.type === "keydown").handler({ key: "Escape", target: { closest: () => null } });
  assert.equal(cleared, 1, "one Esc, one clear");
  T.release();
  assert.deepEqual(counts(), { keydown: 0, resize: 0 });
  T.render(fabric, {});
  assert.deepEqual(counts(), { keydown: 1, resize: 1 }, "entering Topology again listens again, once");
});

// ---- Esc, and the strip Topology does not use -------------------------------

test("Esc clears pin, trace and inspector, and they stay cleared while focus stays on the same item", () => {
  const documentHandlers = [];
  const T = H.loadTopology({ document: { addEventListener: (type, handler) => documentHandlers.push({ type, handler }), removeEventListener() {} } });
  const pin = { textContent: "" };
  const root = { querySelector: (selector) => (selector === "[data-topology-pin]" ? pin : null), querySelectorAll: () => [] };
  const handlers = new Map();
  const container = { dataset: {}, querySelector: (selector) => (selector === ".fabric-topology" ? root : null), addEventListener: (type, handler) => handlers.set(type, handler) };
  let cleared = 0;
  T.attach(container, null, null, { clearSelection() { cleared += 1; }, requestRender() {} });
  const { fabric } = H.twoHbaCascade();
  T.render(fabric, {});
  const element = (dataset) => ({ dataset, closest(selector) { return selector === ".fabric-topology" ? root : /\[data-topo-/.test(selector) ? this : null; } });
  const bay = element({ topoBay: "bay:30" });
  handlers.get("focusin")({ type: "focusin", target: bay });
  handlers.get("click")({ type: "click", target: bay });
  assert.match(pin.textContent, /^Tracing: Bay 30/);
  documentHandlers.find((item) => item.type === "keydown").handler({ key: "Escape", target: bay });
  assert.equal(cleared, 1);
  assert.equal(T.view().pin, null);
  assert.equal(T.view().hover, null);
  // The page redraws and puts focus back on the same bay: nothing returns.
  T.render(fabric, {});
  handlers.get("focusin")({ type: "focusin", target: element({ topoBay: "bay:30" }) });
  assert.equal(T.view().hover, null, "focus restored to the same item does not trace it again");
  assert.doesNotMatch(pin.textContent, /^Tracing/);
  // Focus moving to another item traces again; so does the pointer.
  handlers.get("focusin")({ type: "focusin", target: element({ topoBay: "bay:31" }) });
  assert.match(pin.textContent, /^Tracing: Bay 31/);
  documentHandlers.find((item) => item.type === "keydown").handler({ key: "Escape", target: bay });
  handlers.get("mouseover")({ type: "mouseover", target: element({ topoBay: "bay:31" }) });
  assert.match(pin.textContent, /^Tracing: Bay 31/);
});

// ---- crossings, bands, chains, cost, upward links ---------------------------

test("where two lines cross, the horizontal one has a gap the vertical runs through, and no dot", () => {
  const T = H.loadTopology();
  for (const [name, model] of Object.entries(denseFixtures(T, 1600))) {
    const html = T.renderTopology(model, {});
    const group = /<g class="topo-crossings">([\s\S]*?)<\/g>/.exec(html);
    assert.ok(group, name);
    const gaps = [...group[1].matchAll(/<line class="topo-cross-gap" x1="(-?\d+)" y1="(-?\d+)" x2="(-?\d+)" y2="(-?\d+)"/g)];
    const overs = [...group[1].matchAll(/<line class="topo-link topo-cross-over[^"]*" data-topo-cross="([^"]+)" x1="(-?\d+)" y1="(-?\d+)" x2="(-?\d+)" y2="(-?\d+)"/g)];
    assert.equal(gaps.length, G.crossings(model), `${name}: one gap per crossing`);
    assert.equal(overs.length, gaps.length, name);
    assert.doesNotMatch(group[1], /<circle/, `${name}: no dot`);
    for (const [, x1, y1, x2] of gaps) {
      const x = (Number(x1) + Number(x2)) / 2;
      const y = Number(y1);
      const crossing = G.linkSegments(model).filter((segment) => {
        const [lo, hi] = [Math.min(segment.a[0], segment.b[0]), Math.max(segment.a[0], segment.b[0])];
        const [top, bottom] = [Math.min(segment.a[1], segment.b[1]), Math.max(segment.a[1], segment.b[1])];
        return lo - 1 <= x && x <= hi + 1 && top - 1 <= y && y <= bottom + 1;
      });
      assert.ok(crossing.some((segment) => segment.horizontal) && crossing.some((segment) => segment.vertical), `${name}: the gap at ${x},${y} is on a crossing`);
    }
  }
});

test("no line is drawn over a not-collected band or its note, and bands are in the box census", () => {
  const T = H.loadTopology();
  const cases = H.producerCases();
  const notRead = H.coreFabric();
  for (const name of ["mpr0", "mpr1"]) H.addController(notRead, name);
  H.addBays(notRead, H.range(0, 24), [], [{ controller: "mpr0", state: "active" }]);
  notRead.raw.controller_read.counts.expanders = "not_read";
  for (const [name, fabric] of Object.entries({ "SCALE SES": cases.scale_ses.fabric, "logical disks": cases.linux_virtual.fabric, "member health": cases.linux_health.fabric, "not read": notRead })) {
    for (const width of [0, 390, 3840]) {
      const model = T.buildTopologyModel(fabric, { width });
      assert.ok(G.boxes(model).some((box) => box.kind === "band"), `${name}: the band is in the census`);
      assert.deepEqual(G.linesBehindBoxes(model), [], `${name} at ${width}`);
      for (const link of model.links) for (const [x] of link.points || []) assert.ok(x >= 168, `${name}: ${link.id} stays out of the tier label column`);
    }
  }
});

test("every HBA chain has its own colour and line pattern, named in the legend, none in the muted grey", () => {
  const T = H.loadTopology();
  const looks = Array.from({ length: 16 }, (_, index) => T.chainStyle(index));
  for (let i = 0; i < 16; i += 1) {
    for (let j = i + 1; j < 16; j += 1) assert.ok(looks[i].colour !== looks[j].colour || looks[i].dash !== looks[j].dash, `chains ${i} and ${j}`);
  }
  assert.equal(new Set(looks.slice(0, 8).map((look) => look.dash)).size, 8, "the first eight differ by pattern, not only colour");
  const model = T.buildTopologyModel(H.eightHbaSixteenShelves().fabric);
  const html = T.renderTopology(model, {});
  assert.equal(model.chains.length, 8);
  const css = topologyCss();
  for (const [index] of model.chains.entries()) {
    const { colour, dash } = looks[index];
    const lines = [...html.matchAll(new RegExp(`<polyline class="topo-link [^"]*\\bchain-${index}\\b[^"]*"`, "g"))].map((match) => match[0]);
    assert.ok(lines.length > 0, `chain ${index} is drawn`);
    for (const line of lines) assert.ok(line.includes(`chain-colour-${colour} chain-dash-${dash}`), line);
    assert.match(html, new RegExp(`data-topology-legend="chain-${index}"[^>]*><svg[^>]*><line class="[^"]*chain-colour-${colour} chain-dash-${dash}"`), `chain ${index} in the legend`);
    const rule = new RegExp(`\\.topo-link\\.chain-colour-${colour}\\s*\\{[^}]*stroke:\\s*var\\(--([a-z0-9-]+)\\)`).exec(css);
    assert.ok(rule && rule[1] !== "muted", `colour ${colour}`);
    if (dash) assert.match(css, new RegExp(`\\.topo-link\\.chain-dash-${dash}\\s*\\{[^}]*stroke-dasharray`), `pattern ${dash}`);
  }
});

test("track placement does at most quadratic work in the number of lines in a gap", () => {
  const T = H.loadTopology();
  const work = {};
  for (const count of [24, 48, 96, 192]) {
    const fabric = H.coreFabric();
    const hba = H.addController(fabric, "mpr0", { phys: count * 2, linked: 0 });
    for (let index = 0; index < count; index += 1) H.addExpander(fabric, "mpr0", 0xa000 + index, 1, hba.id);
    const model = T.buildTopologyModel(fabric);
    const perPass = model.work.trackCosts / model.work.routePasses;
    assert.ok(perPass <= 2 * count * count, `${count} lines: ${perPass} cost evaluations per pass`);
    work[count] = perPass;
  }
  for (const [small, large] of [[24, 48], [48, 96], [96, 192]]) {
    assert.ok(work[large] / work[small] <= 4.5, `${small} -> ${large}: x${(work[large] / work[small]).toFixed(2)}`);
  }
});

test("a link that points up the tiers is named and not drawn, and no coordinate is ever NaN", () => {
  const T = H.loadTopology();
  const { fabric, ids } = H.twoHbaCascade();
  fabric.links.push(H.link(ids.mpr0.level1.id, "host", "expander-host"));
  fabric.links.push(H.link(ids.mpr1.level2.id, ids.mpr1.hba.id, "expander-controller"));
  const model = T.buildTopologyModel(fabric, { width: 1600 });
  const text = [...model.notices].join(" ");
  assert.match(text, /2 links that points? up the tiers \(expander-host:/);
  assert.ok(!model.linkById.has(`expander-host:${ids.mpr0.level1.id}->host`));
  const all = { ...denseFixtures(T, 1600), upward: model };
  for (const [name, item] of Object.entries(all)) {
    const html = T.renderTopology(item, {});
    assert.doesNotMatch(html, /NaN/, name);
    for (const link of item.links) for (const point of link.points || []) assert.ok(point.every(Number.isFinite), `${name}: ${link.id}`);
    for (const node of [...item.nodes, ...item.groups]) assert.ok([node.x, node.y, node.w, node.h].every(Number.isFinite), `${name}: ${node.id}`);
  }
});

// ---- titles ---------------------------------------------------------------------

test("two boxes in one tier never show the same title unless their ids are the same", () => {
  const T = H.loadTopology();
  const visibleTitle = (item) => item.lines.slice(0, item.titleLines).join(" ");
  const check = (name, model) => {
    const seen = new Map();
    for (const item of model.nodes) {
      const key = `${item.tier}\u0000${visibleTitle(item)}`;
      assert.ok(!seen.has(key) || seen.get(key) === item.id, `${name}: ${item.id} and ${seen.get(key)} both read "${visibleTitle(item)}"`);
      seen.set(key, item.id);
    }
  };
  // The producer names both HBAs' enclosure views by the same logical id.
  check("producer cascade", T.buildTopologyModel(H.producerCases().core_cascade.fabric));
  // Two expanders whose long names differ only past the shortening point.
  const fabric = H.coreFabric();
  const hba = H.addController(fabric, "mpr0");
  const a = H.addExpander(fabric, "mpr0", 0xa1, 1, hba.id);
  const b = H.addExpander(fabric, "mpr0", 0xa2, 1, hba.id);
  fabric.nodes.find((item) => item.id === a.id).label = `SYNTH-${"X".repeat(60)}-A`;
  fabric.nodes.find((item) => item.id === b.id).label = `SYNTH-${"X".repeat(60)}-B`;
  const model = T.buildTopologyModel(fabric);
  check("long names", model);
  assert.ok(visibleTitle(model.nodeById.get(a.id)).replace(/\s+/g, "").endsWith("-A"), "more of the name is shown");
});

test("at 390 px the search label sits above an input at least 240 px wide", () => {
  const css = topologyCss();
  const media = /@media \(max-width: 640px\)\s*\{([\s\S]*?)\n\}/.exec(css);
  assert.ok(media, "a narrow-screen rule");
  const rule = (selector) => new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`).exec(media[1])?.[1] || "";
  assert.match(rule(".fabric-topology-search-row"), /flex-direction:\s*column/);
  assert.match(rule(".fabric-topology-search-row"), /flex:\s*1 1 100%/);
  const input = rule(".fabric-topology-search-row .fabric-topology-search");
  assert.match(input, /width:\s*100%/);
  assert.match(input, /min-width:\s*15rem/);
  // The header only wraps while the Topology search row is shown.
  assert.match(media[1], /\.panel-header:has\(> \.fabric-topology-search-row:not\(\[hidden\]\)\)\s*\{[^}]*flex-wrap:\s*wrap/);
});
