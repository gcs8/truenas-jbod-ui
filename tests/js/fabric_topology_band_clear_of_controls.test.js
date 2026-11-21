"use strict";

// Topology at phone width (#1378): the sticky trace band must never lie over
// the controls above the drawing (Fit, Actual size, the notices beside and
// under them) when the page opens, at any width and for any fixture.
//
// A sticky box is moved only inside its containing block: with `bottom: 0`
// it rises to stay at the window's bottom edge, but never above the top of
// its containing block (CSS Positioned Layout 3, sticky positioning). So
// what the band can cover is exactly what precedes it inside that block.
// When that block was the whole Topology view, the band could rise over the
// parts, the legend, the notices and the Fit bar; at 390x844 it did, on the
// page's first view. The band's containing block must hold the drawing's box
// and nothing above it, so the band stays in its own place until the drawing
// reaches it, and then can lie only over the drawing (accepted sticky
// behaviour, and focused items scroll clear of it).
//
// No browser is used: the markup is the production renderer's and the rules
// are the shipping stylesheet's. A browser lane checks the pixels.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const H = require("./helpers/fabric_topology");

const CSS = fs.readFileSync(path.join(H.ROOT, "app/static/sas_fabric_topology.css"), "utf8");
const VOID = new Set(["br", "img", "input", "hr", "meta", "link", "col", "wbr", "source"]);

function parse(html) {
  const root = { tag: "#root", classes: [], attributes: "", children: [], parent: null };
  let current = root;
  for (const match of html.matchAll(/<(\/?)([a-zA-Z][\w-]*)((?:[^>"]|"[^"]*")*)>/g)) {
    const [, closing, rawTag, attributes] = match;
    const tag = rawTag.toLowerCase();
    if (closing) {
      let open = current;
      while (open !== root && open.tag !== tag) open = open.parent;
      if (open !== root) current = open.parent;
      continue;
    }
    const classes = (/\bclass="([^"]*)"/.exec(attributes)?.[1] || "").split(/\s+/).filter(Boolean);
    const element = { tag, classes, attributes, children: [], parent: current };
    current.children.push(element);
    const selfClosing = attributes.trim().endsWith("/");
    if (!selfClosing && !VOID.has(tag)) current = element;
  }
  return root;
}

function find(node, predicate) {
  if (predicate(node)) return node;
  for (const child of node.children) {
    const found = find(child, predicate);
    if (found) return found;
  }
  return null;
}

function contains(ancestor, node) {
  for (let at = node; at; at = at.parent) if (at === ancestor) return true;
  return false;
}

function label(node) {
  return `${node.tag}${node.classes.length ? `.${node.classes.join(".")}` : ""}`;
}

function cssRule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?:^|\\n|\\{|,\\s*)\\s*${escaped}\\s*(?:,[^{]*)?\\{([^}]*)\\}`).exec(css.replace(/\/\*[\s\S]*?\*\//g, ""))?.[1] || "";
}

// Every state the page can open in: the fixtures drawn at a phone's width and
// a desktop width, with and without Fit, with a pin, with notices.
function views() {
  const T = H.loadTopology();
  const four = H.fourShelves().fabric;
  const out = [];
  for (const width of [390, 1241, 1920]) {
    const options = { width, height: 640 };
    const collapsed = T.buildTopologyModel(four, options);
    const expanded = Object.fromEntries([...collapsed.groups].map((group) => [group.expandKey, true]));
    const models = {
      "two HBAs, level-2 cascade": T.buildTopologyModel(H.twoHbaCascade().fabric, options),
      "three-level cascade": T.buildTopologyModel(H.threeLevelCascade().fabric, options),
      "four shelves, collapsed": collapsed,
      "four shelves, expanded": T.buildTopologyModel(four, { ...options, expanded }),
    };
    for (const [name, model] of Object.entries(models)) {
      const item = [...model.nodes][0];
      for (const [state, view] of [["idle", {}], ["Fit", { fit: true }], ["pinned", { pin: { kind: "node", id: item.id } }]]) {
        out.push({ name: `${name}, ${width} px, ${state}`, html: T.renderTopology(model, view) });
      }
    }
  }
  return out;
}

test("the trace band's sticky containing block holds the drawing and nothing above it: Fit and the notices are never under it (#1378)", () => {
  const all = views();
  assert.ok(all.some((view) => /data-topology-wide-notice/.test(view.html)), "a view with the wide-drawing notice beside Fit is checked");
  assert.ok(all.some((view) => /data-topology-fit-notice/.test(view.html)), "a view with the Fit notice is checked");
  for (const { name, html } of all) {
    const tree = parse(html);
    const view = find(tree, (node) => node.classes.includes("fabric-topology"));
    const band = find(tree, (node) => /\bdata-topology-trace\b/.test(node.attributes));
    const fit = find(tree, (node) => /\bdata-topology-fit\b/.test(node.attributes));
    const drawing = find(tree, (node) => node.classes.includes("fabric-topology-scroll"));
    assert.ok(view && band && fit && drawing, `${name}: the view, band, Fit and drawing are rendered`);
    const block = band.parent;
    // Control: the band and the drawing share the block, and the band follows the drawing.
    assert.ok(contains(block, drawing), `${name}: the drawing is in the band's containing block`);
    const siblings = block.children;
    const before = siblings.slice(0, siblings.indexOf(band));
    assert.deepEqual(before.map(label), ["div.fabric-topology-scroll"], `${name}: only the drawing's box can be under the band`);
    assert.ok(!contains(block, fit), `${name}: Fit is outside the band's containing block`);
    for (const selector of ["fabric-topology-bar", "fabric-topology-parts", "fabric-topology-legend", "fabric-topology-fit-notice", "warning-item"]) {
      const above = find(view, (node) => node.classes.includes(selector) && !contains(block, node));
      const inside = find(block, (node) => node.classes.includes(selector));
      assert.equal(inside, null, `${name}: .${selector} is not in the band's containing block`);
      if (selector === "fabric-topology-bar") assert.ok(above, `${name}: the Fit bar is drawn, above the drawing`);
    }
  }
});

test("the band is still sticky to the window's bottom edge, and its block keeps the view's spacing and width rules", () => {
  const band = cssRule(CSS, ".fabric-topology .fabric-topology-trace");
  assert.match(band, /position:\s*sticky;/);
  assert.match(band, /bottom:\s*0;/);
  const view = cssRule(CSS, ".fabric-topology");
  const stage = cssRule(CSS, ".fabric-topology-stage");
  for (const property of ["display", "grid-template-columns", "gap"]) {
    const value = (body) => new RegExp(`(?:^|[\\s;])${property}:\\s*([^;]+);`).exec(body)?.[1];
    assert.ok(value(stage), `the drawing's block sets ${property}`);
    assert.equal(value(stage), value(view), `the drawing's block keeps the view's ${property}, so nothing moves`);
  }
  for (const selector of [".fabric-topology > *", ".fabric-topology-stage > *"]) {
    assert.match(cssRule(CSS, selector), /min-width:\s*0/, `${selector}: never wider than the panel`);
    assert.match(cssRule(CSS, selector), /overflow-wrap:\s*anywhere/, `${selector}: long ids wrap`);
  }
});
