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

test("a parent sits over its children and crossings are fewer than before", () => {
  const T = H.loadTopology();
  // Crossings measured the same way on the layout this pull request first drew.
  const before = { "two HBAs, level-2 cascade": 3, "four shelves, collapsed": 6, "four shelves, expanded": 8 };
  for (const [name, model] of Object.entries(fixtures(T))) {
    for (const item of [...model.nodes]) {
      const children = [...(model.outgoing.get(item.id) || [])].map((link) => itemOf(model, link.target)).filter(Boolean);
      if (!children.length) continue;
      const left = Math.min(...children.map((child) => child.x));
      const right = Math.max(...children.map((child) => child.x + child.w));
      assert.ok(centre(item) >= left && centre(item) <= right, `${name}: ${item.id} sits over its children`);
    }
    // The host is centred over its HBAs.
    const host = model.nodeById.get("host");
    const hbas = [...model.nodes].filter((item) => item.kind === "controller");
    assert.ok(centre(host) >= Math.min(...hbas.map(centre)) && centre(host) <= Math.max(...hbas.map(centre)), `${name}: host over the HBAs`);
    // An enclosure fed from level 1 and one fed from level 2 each sit under their own expander.
    for (const link of [...model.links].filter((item) => item.kind === "expander-enclosure")) {
      const parent = itemOf(model, link.source);
      const child = itemOf(model, link.target);
      assert.ok(Math.abs(centre(parent) - centre(child)) <= (parent.w + child.w) / 2, `${name}: ${child.id} is under ${parent.id}`);
    }
    const count = G.crossings(model);
    assert.ok(count < before[name] || count === 0, `${name}: ${count} crossings, ${before[name]} before`);
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
