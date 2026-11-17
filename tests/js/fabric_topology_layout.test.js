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
    // Parents that share a child sit side by side over it: the farthest one
    // is half their row's width, less its own half, from the middle.
    const coParents = new Set(out.flatMap((link) => [...(model.incoming.get(link.target) || [])].filter((other) => other.points).map((other) => other.source)));
    const row = [...coParents].map((id) => model.nodeById.get(id)).filter(Boolean);
    const rowWidth = row.reduce((sum, other) => sum + other.w, 0) + 48 * Math.max(0, row.length - 1);
    result.push({ id: item.id, outside, offset: Math.abs(middle - (left + right) / 2), sole, sideBySide: (rowWidth - item.w) / 2 });
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
    "producer member health": T.buildTopologyModel(H.producerCases().linux_health.fabric, { width }),
  };
}

test("every parent sits over the span of its direct children, near its midpoint, and crossings are fewer than before", () => {
  const T = H.loadTopology();
  // Crossings measured the same way on the layout this pull request first drew.
  const before = { "two HBAs, level-2 cascade": 3, "four shelves, collapsed": 6, "four shelves, expanded": 8 };
  for (const width of [390, 1600, 1920, 2560, 3840]) {
    for (const [name, model] of Object.entries(denseFixtures(T, width))) {
      for (const parent of parentOffsets(model)) {
        assert.equal(parent.outside, 0, `${name} at ${width}: ${parent.id} is over its children`);
        // A parent whose children have no other parent sits at their midpoint
        // unless its neighbours' boxes are in the way; parents that share a
        // child sit side by side over it.
        assert.ok(parent.offset <= (parent.sole ? 40 : Math.max(200, parent.sideBySide + 24)), `${name} at ${width}: ${parent.id} is ${parent.offset} px from its children's midpoint`);
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
  // Expanding a shelf: the shelf the operator expanded stays in view (its
  // control keeps its place on screen: the page-level scroll test).
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
  // Fit, then Actual size: back to the offsets held before Fit.
  dom.box.scrollLeft = 640;
  const held = dom.box.scrollLeft;
  dom.handlers.get("click")({ target: { closest: (selector) => (selector === "[data-topology-fit]" ? dom.fit : null) } });
  T.render(fabric, { width: 1100, expanded });
  dom.replace();
  T.mount(dom.container);
  dom.handlers.get("click")({ target: { closest: (selector) => (selector === "[data-topology-fit]" ? dom.fit : null) } });
  html = T.render(fabric, { width: 1100, expanded });
  dom.replace();
  T.mount(dom.container);
  assert.equal(dom.box.scrollLeft, held, "Actual size returns to where the drawing was");
  // A drawing that fits its box says nothing about scrolling.
  assert.doesNotMatch(H.loadTopology({ document }).render(H.twoHbaCascade().fabric, { width: 4000 }), /data-topology-wide-notice/);
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

test("the deferred width measurement runs once per inner width while Topology is shown, and never after it is left or redrawn", () => {
  // A fake timer queue: nothing runs until the test says so.
  const queue = new Map();
  let next = 1;
  const sandboxWindow = {
    addEventListener() {}, removeEventListener() {},
    setTimeout(callback) { queue.set(next, callback); next += 1; return next - 1; },
    clearTimeout(id) { queue.delete(id); },
  };
  // A timer that runs leaves the queue, as in a browser.
  const take = () => {
    const [id, callback] = [...queue.entries()].pop();
    queue.delete(id);
    return callback;
  };
  const T = H.loadTopology({ document: { addEventListener() {}, removeEventListener() {} }, window: sandboxWindow });
  let requested = 0;
  // The page's panel: each redraw puts a new root and a new scroll box in it.
  let root = { querySelector: () => null, querySelectorAll: () => [] };
  let box = { scrollLeft: 0, scrollTop: 0, clientWidth: 400 };
  const container = {
    dataset: {},
    querySelector: (selector) => (selector === ".fabric-topology-scroll" ? box : selector === ".fabric-topology" ? root : null),
    addEventListener() {},
  };
  T.attach(container, null, null, { requestRender() { requested += 1; } });
  const { fabric } = H.twoHbaCascade();
  // The drawing is laid out for a 400 px box; the box put in place measures `inner`.
  const draw = (inner) => {
    box = { scrollLeft: 0, scrollTop: 0, clientWidth: 400 };
    T.render(fabric, {});
    root = { querySelector: () => null, querySelectorAll: () => [] };
    box = { scrollLeft: 0, scrollTop: 0, clientWidth: inner };
    T.mount(container);
  };
  const scheduled = [];
  let total = 0;
  const counting = sandboxWindow.setTimeout;
  sandboxWindow.setTimeout = (callback, delay) => { total += 1; return counting(callback, delay); };
  for (const inner of [373, 373, 372, 373]) {
    draw(inner);
    scheduled.push(total);
  }
  assert.deepEqual(scheduled, [1, 1, 2, 2], "373 and 372 are each measured once");
  assert.equal(queue.size, 1, "only the latest measurement is pending");
  // A measurement for a root the page has since replaced does nothing.
  draw(371);
  const stale = take();
  root = { querySelector: () => null, querySelectorAll: () => [] };
  stale();
  assert.equal(requested, 0, "a replaced root is not laid out again");
  // The one for the root in place lays it out once more.
  draw(370);
  take()();
  assert.equal(requested, 1);
  // Leaving Topology cancels what is pending; a new visit measures afresh.
  draw(369);
  assert.equal(queue.size, 1);
  const left = [...queue.values()].pop();
  T.release();
  assert.equal(queue.size, 0, "nothing pending after release");
  left();
  assert.equal(requested, 1, "a callback that ran anyway does nothing after release");
  draw(373);
  assert.equal(queue.size, 1, "a width measured on an earlier visit is measured again");
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
    assert.equal(overs.length, 2 * gaps.length, `${name}: two pieces from each crossing centre`);
    assert.doesNotMatch(group[1], /<circle/, `${name}: no dot`);
    for (const [, x1, y1, x2, y2] of gaps) assert.ok(Math.hypot(Number(x2) - Number(x1), Number(y2) - Number(y1)) > 0, `${name}: the gap at ${x1},${y1} has a length`);
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

// ---- layout cost ------------------------------------------------------------------

// `lines` links that are not bay bundles: host to each of `hbas` HBAs, and
// each HBA to its share of level-1 expanders.
function manyLines(lines, hbas) {
  const fabric = H.coreFabric();
  const names = H.range(0, hbas).map((index) => `mpr${index}`);
  for (const name of names) H.addController(fabric, name, { phys: Math.ceil(lines / hbas) * 4, linked: 0 });
  for (let index = 0; index < lines - hbas; index += 1) H.addExpander(fabric, names[index % hbas], 0x100000 + index, 1, `controller:${names[index % hbas]}`);
  return fabric;
}

test("the previous order is read through a rank map built once per tier", () => {
  const T = H.loadTopology();
  const fabric = manyLines(384, 1);
  const first = T.buildTopologyModel(fabric, { width: 1920 });
  let reads = 0;
  const counted = (values) => new Proxy(values, {
    get(target, key, receiver) {
      if (typeof key === "string" && /^\d+$/.test(key)) reads += 1;
      return Reflect.get(target, key, receiver);
    },
  });
  const previousOrder = Object.fromEntries([...first.tiers].map((tier) => [tier.id, counted([...tier.order].reverse())]));
  const items = [...first.tiers].reduce((sum, tier) => sum + tier.order.length, 0);
  const again = T.buildTopologyModel(fabric, { width: 1920, previousOrder });
  assert.ok(reads <= 2 * items, `${reads} reads of the previous order for ${items} items`);
  // The previous order is still the one drawn on a tie.
  const row = [...again.tiers].find((tier) => tier.id === "expanders:1");
  assert.equal(row.order.length, 383);
});

test("layout work is counted and bounded: above the threshold the polish steps are skipped, the page says so, and nothing is dropped", () => {
  const T = H.loadTopology();
  // Below the threshold: the whole search runs and no notice is shown.
  const small = T.buildTopologyModel(manyLines(256, 16), { width: 1920 });
  assert.ok(Number.isFinite(small.work.total), "the layout counts its work");
  assert.equal(small.simplified, false);
  assert.ok(small.work.candidates > 1, "the candidate-order search ran");
  assert.ok(![...small.notices].some((text) => /simplified/.test(text)));
  for (const hbas of [1, 16]) {
    const model = T.buildTopologyModel(manyLines(4096, hbas), { width: 1920 });
    const lines = [...model.links].filter((link) => !link.bundle);
    assert.equal(lines.length, 4096, `${hbas} HBAs: every line is in the model`);
    for (const link of lines) assert.ok(link.points && link.points.length >= 2 && link.points.every((point) => point.every(Number.isFinite)), `${link.id} is drawn`);
    assert.equal(model.nodes.length, 4097, `${hbas} HBAs: every device is drawn`);
    for (const item of model.nodes) assert.ok([item.x, item.y, item.w, item.h].every(Number.isFinite), item.id);
    assert.equal(model.simplified, true, `${hbas} HBAs: simplified`);
    assert.equal(model.work.candidates, 1, "one ordering pass");
    assert.deepEqual([...model.crossingMarks], [], "no crossing gaps");
    const notice = [...model.notices].find((text) => /simplified/.test(text));
    assert.ok(notice, "the page says the drawing was simplified");
    assert.match(notice, /4096 lines/);
    assert.match(notice, /crossing gaps/);
    assert.match(notice, /order/);
    assert.match(notice, /every device and every line is drawn/i);
    // The stated bound: 30 million counted steps at 4096 lines (one ordering
    // pass and one routing pass; the full polish would count about 257 million).
    assert.ok(model.work.total <= 30e6, `${hbas} HBAs: ${model.work.total} work units at 4096 lines`);
    const html = T.renderTopology(model, {});
    assert.equal((html.match(/<polyline class="topo-link /g) || []).length, 4096);
  }
});

// ---- crossings: the redrawn piece runs through the centre for every pattern ---

function dashOf(css, index) {
  const rule = new RegExp(`\\.topo-link\\.chain-dash-${index}\\s*\\{([^}]*)\\}`).exec(css);
  if (!rule) return [];
  return /stroke-dasharray:\s*([^;]+);/.exec(rule[1])[1].trim().split(/[\s,]+/).map(Number);
}

test("the piece redrawn across a crossing gap is continuous through the crossing centre for each of the eight chain patterns", () => {
  const T = H.loadTopology();
  const css = topologyCss();
  const gapWidth = Number(/\.topo-cross-gap\s*\{[^}]*stroke-width:\s*([\d.]+)/.exec(css)[1]);
  const model = T.buildTopologyModel(H.eightHbaSixteenShelves().fabric, { width: 1600 });
  assert.ok(model.crossingMarks.length > 0);
  for (let pattern = 0; pattern < 8; pattern += 1) {
    // Every crossing drawn as if its continuing line were chain `pattern`.
    for (const mark of model.crossingMarks) model.linkById.get(mark.over).chain = model.chains[pattern];
    const html = T.renderTopology(model, {});
    const group = /<g class="topo-crossings">([\s\S]*?)<\/g>/.exec(html)[1];
    const gaps = [...group.matchAll(/<line class="topo-cross-gap" x1="(-?\d+)" y1="(-?\d+)" x2="(-?\d+)" y2="(-?\d+)"/g)];
    const pieces = [...group.matchAll(/<line class="topo-link topo-cross-over([^"]*)" data-topo-cross="[^"]+" x1="(-?\d+)" y1="(-?\d+)" x2="(-?\d+)" y2="(-?\d+)"/g)]
      .map((item) => ({ classes: item[1], x1: Number(item[2]), y1: Number(item[3]), x2: Number(item[4]), y2: Number(item[5]) }));
    assert.equal(gaps.length, model.crossingMarks.length);
    const dash = dashOf(css, pattern);
    // A pattern starts with a dash: a piece painted from its first point.
    const firstDash = dash.length ? dash[0] : Infinity;
    assert.ok(firstDash > 0, `pattern ${pattern} starts with a dash`);
    for (const [, gx1, gy1, gx2] of gaps) {
      const cx = (Number(gx1) + Number(gx2)) / 2;
      const cy = Number(gy1);
      const here = pieces.filter((piece) => piece.x1 === cx && piece.x2 === cx && (piece.y1 === cy));
      assert.equal(here.length, 2, `pattern ${pattern}: two pieces start at the centre ${cx},${cy}`);
      for (const piece of here) {
        assert.match(piece.classes, new RegExp(`\\bchain-dash-${pattern}\\b`), `pattern ${pattern}`);
        assert.ok(Math.abs(piece.y2 - piece.y1) > gapWidth / 2, `pattern ${pattern}: the piece reaches past the gap`);
      }
      assert.ok(here.some((piece) => piece.y2 < cy) && here.some((piece) => piece.y2 > cy), `pattern ${pattern}: one piece up, one down`);
      // Painted on both sides of the centre: [cy - firstDash, cy + firstDash].
      assert.ok(Math.min(firstDash, 9) >= 2, `pattern ${pattern}: ${firstDash} px painted each side of the centre`);
    }
  }
});

// ---- bay bundles keep 3:1 against the drawing's surface -------------------------

test("every chain colour's bay bundle has at least 3:1 contrast against the scroll surface after its opacity", () => {
  const css = topologyCss();
  const vars = Object.fromEntries([...ROOT_CSS.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6});/g)].map((match) => [match[1], match[2]]));
  const resolve = (value) => vars[/var\(--([a-z0-9-]+)\)/.exec(value)[1]];
  const surface = resolve(/\.fabric-topology-scroll\s*\{[^}]*background:\s*([^;]+);/.exec(css)[1]);
  const bundle = /\.fabric-topology-svg \.topo-link\.is-bundle\s*\{([^}]*)\}/.exec(css)[1];
  const opacity = Number(/stroke-opacity:\s*([\d.]+)/.exec(bundle)?.[1] ?? 1);
  for (let colour = 0; colour < 4; colour += 1) {
    const stroke = new RegExp(`\\.topo-link\\.chain-colour-${colour}\\s*\\{[^}]*stroke:\\s*([^;]+);`).exec(css)[1];
    const ratio = contrast(blend(resolve(stroke), opacity, surface), surface);
    assert.ok(ratio >= 3, `chain colour ${colour} bundle at opacity ${opacity}: ${ratio.toFixed(2)}:1`);
  }
  // The legend's bundle swatch is drawn the same way.
  const swatch = /\.fabric-topology-swatch\.is-bundle\s*\{([^}]*)\}/.exec(css)[1];
  assert.equal(Number(/opacity:\s*([\d.]+)/.exec(swatch)?.[1] ?? 1), opacity);
});

// ---- more chains than looks ------------------------------------------------------

test("when more chains are drawn than there are looks, a notice names the chains that share one, and none is dropped", () => {
  const T = H.loadTopology();
  const fabric = H.coreFabric();
  for (let index = 0; index < 34; index += 1) {
    const hba = H.addController(fabric, `mpr${index}`);
    H.addExpander(fabric, `mpr${index}`, 0x9000 + index, 1, hba.id);
  }
  const model = T.buildTopologyModel(fabric, { width: 1920 });
  assert.equal(model.chains.length, 34);
  const looks = model.chains.map((_, index) => T.chainStyle(index)).map((look) => `${look.colour}/${look.dash}`);
  assert.equal(new Set(looks.slice(0, 32)).size, 32, "the first 32 chains differ in colour or pattern");
  const notice = [...model.notices].find((text) => /share/.test(text));
  assert.ok(notice, "a notice names the shared looks");
  assert.match(notice, /mpr0 and mpr32/);
  assert.match(notice, /mpr1 and mpr33/);
  const html = T.renderTopology(model, {});
  for (let index = 0; index < 34; index += 1) assert.ok(html.includes(`data-topology-legend="chain-${index}"`), `chain ${index} is in the legend`);
  // Thirty-two chains need no notice.
  const fewer = H.coreFabric();
  for (let index = 0; index < 32; index += 1) {
    const hba = H.addController(fewer, `mpr${index}`);
    H.addExpander(fewer, `mpr${index}`, 0x9000 + index, 1, hba.id);
  }
  assert.ok(![...T.buildTopologyModel(fewer, { width: 1920 }).notices].some((text) => /share/.test(text)));
});

test("the shared-look notice names every group of chains that share a look, however many there are", () => {
  const T = H.loadTopology();
  const chains = (count) => {
    const fabric = H.coreFabric();
    for (let index = 0; index < count; index += 1) {
      const hba = H.addController(fabric, `mpr${index}`);
      H.addExpander(fabric, `mpr${index}`, 0x9000 + index, 1, hba.id);
    }
    return [...T.buildTopologyModel(fabric, { width: 1920 }).notices].find((text) => /share/.test(text));
  };
  // 37 chains: five pairs share a look, and all five are named.
  const notice = chains(37);
  const named = /share a colour and line pattern: ([^.]*)\./.exec(notice)?.[1];
  assert.equal(named, "mpr0 and mpr32; mpr1 and mpr33; mpr2 and mpr34; mpr3 and mpr35; mpr4 and mpr36", notice);
  assert.doesNotMatch(notice, /more/);
  // 70 chains: every one of the 32 looks is shared, some by three chains.
  const many = chains(70);
  assert.doesNotMatch(many, /more/);
  for (let index = 0; index < 32; index += 1) assert.match(many, new RegExp(`\\bmpr${index}\\b`), `mpr${index}`);
  assert.match(many, /mpr5, mpr37 and mpr69/);
});

// ---- width: the scroll box's inner width -----------------------------------------

test("the width notice and the width-limited Fit use the scroll box's inner width", () => {
  const css = topologyCss();
  const border = Number(/\.fabric-topology-scroll\s*\{[^}]*border:\s*([\d.]+)px/.exec(css)[1]);
  const { fabric } = H.fourShelves();
  // Before the box exists: the panel's width less the box's two borders.
  const T = H.loadTopology({ document: { addEventListener() {}, removeEventListener() {} } });
  const dom = viewDom(0);
  T.attach(dom.container, null, null, {});
  dom.replace();
  const panelWidth = 390;
  let html = T.render(fabric, { width: panelWidth, height: 700 });
  assert.match(html, new RegExp(`wider than its box \\(${panelWidth - 2 * border} px\\)`));
  // With the box drawn, its own client width (here less a 15 px scroll bar).
  T.mount(dom.container);
  const box = dom.box;
  box.clientWidth = panelWidth - 2 * border - 15;
  html = T.render(fabric, { width: panelWidth, height: 700 });
  assert.match(html, new RegExp(`wider than its box \\(${box.clientWidth} px\\)`));
  assert.equal(T.drawnModel().box.width, box.clientWidth);
  const fitted = T.renderTopology(T.drawnModel(), { fit: true });
  const svg = /<svg class="fabric-topology-svg[^"]*"[^>]*width="(\d+)"/.exec(fitted);
  assert.ok(Number(svg[1]) <= box.clientWidth, `fitted to ${svg[1]} px in a ${box.clientWidth} px box`);
});

// ---- a narrow page does not scroll sideways ------------------------------------

test("at 390 px the legend and the notices wrap inside the panel", () => {
  const css = topologyCss();
  const rule = (selector) => new RegExp(`(?:^|\\n|,\\s*)${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*(?:,[^{]*)?\\{([^}]*)\\}`).exec(css)?.[1] || "";
  assert.match(rule(".fabric-topology"), /grid-template-columns:\s*minmax\(0,\s*1fr\)/, "the view's one column never grows past the panel");
  for (const selector of [".fabric-topology > *", ".fabric-topology-legend-item", ".fabric-topology .warning-item"]) {
    assert.match(rule(selector), /min-width:\s*0/, selector);
    assert.match(rule(selector), /overflow-wrap:\s*anywhere/, selector);
  }
  // The drawing alone scrolls sideways, inside its own box.
  assert.match(rule(".fabric-topology-scroll"), /overflow:\s*auto/);
  assert.match(rule(".fabric-topology-scroll"), /max-width:\s*100%/);
});

// ---- tier labels -----------------------------------------------------------------

test("an expander tier label never leaves its level number alone on a line", () => {
  const T = H.loadTopology();
  for (const fixture of [H.twoHbaCascade().fabric, H.threeLevelCascade().fabric]) {
    const model = T.buildTopologyModel(fixture);
    const rows = [...model.tiers].filter((tier) => tier.id.startsWith("expanders:"));
    assert.ok(rows.length >= 2);
    for (const tier of rows) {
      assert.ok(tier.labelLines.every((line) => !/^\d+$/.test(line.trim())), JSON.stringify(tier.labelLines));
      assert.match(tier.labelLines[tier.labelLines.length - 1], /level \d+$/);
    }
  }
});

// ---- the trace sentence stays in the window ------------------------------------

function cssRule(css, selector) {
  return new RegExp(`(?:^|\\n|\\{)\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`).exec(css)?.[1] || "";
}

function mediaBlock(css, width) {
  return new RegExp(`@media \\(max-width: ${width}px\\)\\s*\\{([\\s\\S]*?)\\n\\}`).exec(css)?.[1] || "";
}

test("the trace sentence sticks to the window's bottom in an opaque band whose reserved height a focused item is kept clear of", () => {
  const css = topologyCss();
  const band = cssRule(css, ".fabric-topology .fabric-topology-pin");
  assert.match(band, /position:\s*sticky;/);
  assert.match(band, /bottom:\s*0;/);
  // Opaque: the page's surface colour, a six-digit colour with no alpha.
  assert.match(band, /background:\s*var\(--surface\);/);
  assert.match(ROOT_CSS, /--surface:\s*#[0-9a-f]{6};/i);
  assert.match(band, /border-top:\s*1px solid var\(--panel-border\);/);
  assert.ok(Number(/z-index:\s*(\d+);/.exec(band)?.[1]) >= 1, "painted above the drawing");
  // Its height is reserved in lines, the same for the hint, a trace and a pin;
  // a longer sentence may grow it and is never clipped. The reserved height
  // itself is checked by the next test.
  assert.match(band, /min-height:\s*var\(--topology-trace-band\);/);
  assert.match(band, /line-height:\s*var\(--topology-trace-line\);/);
  assert.doesNotMatch(band, /(?:^|[\s;])(?:height|max-height|overflow|text-overflow|-webkit-line-clamp|line-clamp)\s*:/);
  const view = cssRule(css, ".fabric-topology");
  assert.match(view, /--topology-trace-line:\s*1\.25rem;/);
  const reserved = (text) => Number(/--topology-trace-lines:\s*(\d+);/.exec(text)?.[1]);
  assert.equal(reserved(view), 1, "one line on wide pages");
  assert.equal(reserved(cssRule(mediaBlock(css, 1240), ".fabric-topology")), 2, "two lines up to 1240 px, the page's one-column width");
  assert.equal(reserved(cssRule(mediaBlock(css, 640), ".fabric-topology")), 3, "three lines at phone width");
  assert.match(ROOT_CSS, /@media \(max-width: 1240px\)/, "an existing page breakpoint");
  assert.ok(css.indexOf("@media (max-width: 1240px)") < css.indexOf("@media (max-width: 640px)"), "the narrower rule comes last and wins");
  // The band's whole height: its lines, padding on both sides and its border.
  const padding = /padding:\s*(\d+)px 0;/.exec(band);
  assert.ok(padding, "vertical padding in px");
  const whole = 2 * Number(padding[1]) + 1;
  assert.match(view, new RegExp(`--topology-trace-band:\\s*calc\\(var\\(--topology-trace-lines\\) \\* var\\(--topology-trace-line\\) \\+ ${whole}px\\);`));
  // Every item that takes keyboard focus scrolls clear of the band.
  assert.match(cssRule(css, `.fabric-topology-svg [role="button"]`), /scroll-margin-bottom:\s*var\(--topology-trace-band\);/);
  const T = H.loadTopology();
  for (const [name, model] of Object.entries(fixtures(T, { width: 1100 }))) {
    const html = T.renderTopology(model, {});
    const focusable = [...html.matchAll(/<[a-z]+\b[^>]*\btabindex="0"[^>]*>/g)].map((match) => match[0]);
    assert.ok(focusable.length, name);
    assert.ok(focusable.every((tag) => tag.includes('role="button"')), `${name}: every focusable item is covered by the rule`);
  }
  // Only the band sticks; nothing above the drawing or in it is placed by this.
  assert.equal((css.match(/position:\s*sticky/g) || []).length, 1);
});

test("the trace sentence is one element after the drawing's scroll box, the same element for the hint, a hover and a pin", () => {
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
  const fabric = H.twoHbaCascade().fabric;
  const tag = (html) => {
    const found = [...html.matchAll(/<p [^>]*data-topology-pin[^>]*>/g)].map((match) => match[0]);
    assert.equal(found.length, 1, "one trace sentence");
    const at = html.indexOf(found[0]);
    assert.ok(at > html.indexOf('<div class="fabric-topology-scroll">'), "after the scroll box");
    assert.ok(at > html.lastIndexOf("</svg></div>"), "after the drawing, outside its box");
    assert.ok(at < html.indexOf("fabric-topology-not-shown"), "before the footer");
    return found[0];
  };
  const idle = tag(T.render(fabric, { width: 1100 }));
  const model = T.drawnModel();
  const link = [...model.links].find((item) => !item.bundle);
  const element = {
    dataset: { topoLink: link.id },
    closest(selector) {
      if (selector === ".fabric-topology") return root;
      return /\[data-topo-/.test(selector) ? this : null;
    },
  };
  handlers.get("mouseover")({ target: element });
  assert.match(pin.textContent, /^Tracing: /, "a hover writes into the same element");
  handlers.get("click")({ target: element });
  assert.ok(T.inspectorDetail(), "the line is pinned");
  const pinned = T.render(fabric, { width: 1100 });
  assert.equal(tag(pinned), idle, "the pinned redraw has the same element");
  assert.match(pinned, /data-topology-pin>Pinned: /);
});

// The page's stylesheet decides how a min-height is measured: under
// `box-sizing: border-box` it is the whole box, padding and border included,
// so the reserved height must be the whole band or the band still grows when
// its lines fill. Which box-sizing reaches the sentence is read from both
// stylesheets, in their load order, for the sentence's own element.
const PIN_ELEMENT = { tag: "p", classes: new Set(["fabric-topology-pin", "subtle"]), attributes: new Set(["data-topology-pin"]) };
const PIN_ANCESTOR_CLASSES = new Set(["fabric-topology", "fabric-map-panel", "fabric-view-layout", "fabric-view-panel", "panel", "app-shell", "fabric-page-shell"]);

function cssRules(css) {
  const rules = [];
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const match of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const prelude = match[1].trim();
    if (!prelude.startsWith("@")) rules.push({ selectors: prelude.split(",").map((part) => part.trim()), body: match[2] });
  }
  return rules;
}

function compound(part) {
  const tokens = part.match(/\*|[a-z][a-z0-9-]*|\.[\w-]+|#[\w-]+|\[[^\]]+\]|::?[\w-]+(?:\([^)]*\))?/gi) || [];
  return {
    tag: tokens.find((token) => /^(?:\*|[a-z])/i.test(token)) || null,
    classes: tokens.filter((token) => token.startsWith(".")).map((token) => token.slice(1)),
    ids: tokens.filter((token) => token.startsWith("#")),
    attributes: tokens.filter((token) => token.startsWith("[")).map((token) => token.slice(1, -1)),
    pseudo: tokens.filter((token) => token.startsWith(":")),
  };
}

// Whether a selector reaches the sentence's element, and its specificity.
// A selector this reader cannot decide fails the test rather than being skipped.
function reachesPin(selector) {
  const parts = selector.replace(/\s*>\s*/g, " ").split(/\s+/);
  assert.ok(!/[+~]/.test(selector), `sibling combinator in ${selector}`);
  const last = compound(parts[parts.length - 1]);
  const all = parts.map(compound);
  const specificity = [
    all.reduce((sum, item) => sum + item.ids.length, 0),
    all.reduce((sum, item) => sum + item.classes.length + item.attributes.length + item.pseudo.filter((token) => !token.startsWith("::")).length, 0),
    all.reduce((sum, item) => sum + (item.tag && item.tag !== "*" ? 1 : 0) + item.pseudo.filter((token) => token.startsWith("::")).length, 0),
  ];
  const own = (!last.tag || last.tag === "*" || last.tag === PIN_ELEMENT.tag)
    && !last.ids.length
    && last.classes.every((name) => PIN_ELEMENT.classes.has(name))
    && last.attributes.every((name) => PIN_ELEMENT.attributes.has(name));
  if (!own || last.pseudo.some((token) => token.startsWith("::"))) return null;
  assert.equal(last.pseudo.length, 0, `state-dependent selector ${selector}`);
  const ancestors = all.slice(0, -1).every((item) => !item.ids.length && !item.pseudo.length && item.attributes.length === 0
    && item.classes.every((name) => PIN_ANCESTOR_CLASSES.has(name)) && (!item.tag || ["*", "div", "body", "html", "main", "section"].includes(item.tag)));
  return ancestors ? specificity : null;
}

function declared(body, property) {
  // A property name is letters and hyphens, which need no escaping here.
  assert.match(property, /^[a-z-]+$/);
  return [...body.matchAll(new RegExp(`(?:^|[;{\\s])${property}\\s*:\\s*([^;]+);`, "g"))].map((match) => match[1].trim());
}

function pinBoxSizing(sheets) {
  let winner = { value: "content-box", specificity: [-1, -1, -1] };
  const more = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
  for (const css of sheets) {
    for (const rule of cssRules(css)) {
      for (const value of declared(rule.body, "box-sizing")) {
        for (const selector of rule.selectors) {
          const specificity = reachesPin(selector);
          if (specificity && more(specificity, winner.specificity) >= 0) winner = { value, specificity, selector };
        }
      }
    }
  }
  return winner;
}

function cssPixels(expression, properties) {
  let text = expression;
  for (let depth = 0; /var\(/.test(text); depth += 1) {
    assert.ok(depth < 10, `unresolved ${expression}`);
    text = text.replace(/var\((--[\w-]+)\)/g, (_, name) => {
      assert.ok(properties.has(name), `${name} is defined`);
      return `(${properties.get(name)})`;
    });
  }
  text = text.replace(/calc\(/g, "(").replace(/([\d.]+)rem\b/g, (_, value) => String(Number(value) * 16)).replace(/([\d.]+)px\b/g, "$1");
  assert.match(text, /^[\d.\s+*()-]+$/, `plain arithmetic: ${text}`);
  return Function(`"use strict"; return (${text});`)();
}

test("the trace band's reserved height is the whole band at every breakpoint, under the box sizing that reaches it", () => {
  const css = topologyCss();
  const sizing = pinBoxSizing([ROOT_CSS, css]);
  const band = cssRule(css, ".fabric-topology .fabric-topology-pin");
  assert.equal(cssRules(css).filter((rule) => rule.selectors.some((selector) => /fabric-topology-pin/.test(selector))).length, 1, "one rule sizes the band, at every width");
  const [minHeight] = declared(band, "min-height");
  assert.ok(minHeight, "the band reserves a height");
  const padding = declared(band, "padding")[0].split(/\s+/).map((value) => Number.parseFloat(value));
  const vertical = padding[0] + (padding.length > 2 ? padding[2] : padding[0]);
  const border = Number.parseFloat(declared(band, "border-top")[0]);
  assert.equal(declared(band, "border-bottom").length + declared(band, "border").length, 0, "only a top border");
  const view = cssRule(css, ".fabric-topology");
  const base = new Map([...view.matchAll(/(--topology-[\w-]+):\s*([^;]+);/g)].map((match) => [match[1], match[2].trim()]));
  const breakpoints = [["wide", base]];
  for (const match of css.matchAll(/@media \(max-width: (\d+)px\)/g)) {
    const inner = cssRule(mediaBlock(css, Number(match[1])), ".fabric-topology");
    const set = new Map([...inner.matchAll(/(--topology-[\w-]+):\s*([^;]+);/g)].map((item) => [item[1], item[2].trim()]));
    if (set.size) breakpoints.push([`${match[1]} px`, new Map([...base, ...set])]);
  }
  assert.ok(breakpoints.length >= 3, "a wide reservation and the narrower ones");
  const seen = [];
  for (const [name, properties] of breakpoints) {
    const lines = cssPixels("var(--topology-trace-lines)", properties);
    const line = cssPixels("var(--topology-trace-line)", properties);
    const reserved = cssPixels(minHeight, properties);
    const whole = lines * line + (sizing.value === "border-box" ? vertical + border : 0);
    assert.equal(reserved, whole, `${name}: ${lines} line(s) of ${line} px under ${sizing.value} (from ${sizing.selector || "the initial value"})`);
    // A focused item scrolls clear of the same whole band.
    assert.equal(cssPixels("var(--topology-trace-band)", properties), lines * line + vertical + border, `${name}: the band's whole height`);
    seen.push(reserved);
  }
  assert.equal(sizing.value, "border-box", "the page sizes every box by its border");
  assert.deepEqual(seen, [29, 49, 69], "29, 49 and 69 px bands");
});
