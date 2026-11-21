"use strict";

// Disk Path fault evidence at 390 px (#1133): the kernel event-sample table
// is wider than a phone (its own minimum width), so it must scroll inside its
// own box and never widen the page. The box already scrolls; what widened
// the page was every grid item above it. A grid item's automatic minimum
// width is its min-content width, and an implicit `auto` column takes that
// minimum too, so the table's minimum width climbed from item to item until
// the evidence panel overflowed the Disk Path view.
//
// The panel markup is rendered by the production renderer; the rules are read
// from the shipping stylesheet as they apply at 390 px. The model below is
// the CSS Grid rule itself: walking up from the scroll box, a grid item whose
// minimum width is `auto` takes the table's width as its minimum while that
// width is still carried, and a grid column whose minimum is zero (as in
// `minmax(0, 1fr)`) stops carrying it, since its min-content size is then
// zero. A browser check at 390 px is in qa/header-and-disk-path-layout.spec.js.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const STYLE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");
const PHONE = 390;

// ---- the production markup -----------------------------------------------------

function loadRenderers(fabric) {
  const sandbox = vm.createContext({
    URLSearchParams,
    window: { location: { search: "" }, SAS_FABRIC_BOOTSTRAP: { snapshot: { slots: [] }, fabric } },
    document: {
      getElementById: () => ({ innerHTML: "", textContent: "" }),
      querySelectorAll: () => [],
    },
  });
  const end = FABRIC_SOURCE.indexOf("  elements.modeButtons.forEach((button) => {", FABRIC_SOURCE.indexOf("  function render()"));
  assert.ok(end > 0, "the renderers end where the page wiring starts");
  vm.runInContext(`${FABRIC_SOURCE.slice(0, end)}
    window.renderTest = { renderDiskPathMode, state };
  })();`, sandbox);
  return sandbox.window.renderTest;
}

// A CORE bay on one path leg with kernel fault evidence on that leg: enough
// rows that the event sample table is shown.
function faultFabric() {
  const rows = Array.from({ length: 6 }, (_, index) => ({
    event_id: `event:${index}`,
    order: index + 1,
    severity: index % 2 ? "warning" : "error",
    impact: index % 2 ? "warning" : "error",
    type: "scsi_command_failed",
    finding: "Command failed: CDB READ(10) on the synthetic target, sense key MEDIUM ERROR",
    scope: "mpr0 target 4 da4",
    code: "0x31120303",
    confidence: "standard",
  }));
  const diagnostics = {
    event_count: rows.length,
    devices: ["da4"],
    targets: [4],
    operator_summary: `${rows.length} kernel events on this path leg`,
    event_table: { rows, sample_count: rows.length },
  };
  const nodes = [
    { id: "host", kind: "host", label: "synthetic-core", status: "online", metrics: {}, related_slots: [4], raw: {} },
    { id: "controller:mpr0", kind: "controller", label: "mpr0", status: "online", metrics: {}, related_slots: [4], raw: {} },
    { id: "bay:4", kind: "bay", label: "Bay 04", status: "online", metrics: {}, related_slots: [4], raw: {} },
  ];
  const trace = {
    id: "bay:4", kind: "bay", label: "Bay 04", slots: [4], node_ids: ["host", "controller:mpr0", "bay:4"], link_ids: [],
    metrics: {
      device_name: "da4",
      physical_location_known: true,
      path_states: [{ controller: "mpr0", state: "active", device_name: "da4" }],
      mpr_devices: [{ controller: "mpr0", state: "active", member_device_name: "da4", handle: "000e", diagnostics }],
    },
  };
  return { available: true, platform: "core", nodes, traces: [trace], links: [], controllers: [{ id: "controller:mpr0", name: "mpr0" }], aliases: {} };
}

// The Disk Path view with the fault evidence panel and its event sample open.
function renderOpenEvidence() {
  const fabric = faultFabric();
  const api = loadRenderers(fabric);
  api.state.mode = "disk";
  api.state.selectedTraceId = "bay:4";
  const first = api.renderDiskPathMode(fabric);
  const evidenceKey = /data-fabric-evidence-key="([^"]+)"/.exec(first)?.[1];
  const tableKey = /data-fabric-diagnostic-table-key="([^"]+)"/.exec(first)?.[1];
  assert.ok(evidenceKey && tableKey, "the Disk Path view shows the fault evidence panel and its event sample");
  api.state.openEvidencePanels.add(evidenceKey.replace(/&quot;/g, "\"").replace(/&amp;/g, "&"));
  for (const key of Object.keys(api.state.diagnosticTables)) api.state.diagnosticTables[key].open = true;
  const html = api.renderDiskPathMode(fabric);
  assert.match(html, /<table class="fabric-diagnostic-table">/);
  return html;
}

// Elements and their parents, from markup the renderer writes (well formed,
// no raw text in attributes).
const VOID = new Set(["br", "img", "input", "hr", "meta", "link", "col", "wbr", "source"]);

function parse(html) {
  const root = { tag: "#root", classes: [], children: [], parent: null };
  let current = root;
  for (const match of html.matchAll(/<(\/?)([a-zA-Z][\w-]*)([^>]*)>/g)) {
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
    if (!VOID.has(tag) && !attributes.trim().endsWith("/")) current = element;
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

// ---- the stylesheet as it applies at 390 px -------------------------------------

function applies(media, width) {
  if (!media) return true;
  const max = /max-width:\s*(\d+)px/.exec(media);
  const min = /min-width:\s*(\d+)px/.exec(media);
  if (!max && !min) return false;
  return (!max || width <= Number(max[1])) && (!min || width >= Number(min[1]));
}

function rules(css) {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const found = [];
  let index = 0;
  const stack = [];
  let prelude = "";
  for (; index < text.length; index += 1) {
    const character = text[index];
    if (character === "{") {
      const head = prelude.trim();
      prelude = "";
      if (head.startsWith("@")) {
        stack.push({ media: head, body: null });
        continue;
      }
      const close = text.indexOf("}", index);
      const media = stack.filter((item) => item.media).map((item) => item.media).join(" and ");
      found.push({ selectors: head.split(",").map((part) => part.trim()), body: text.slice(index + 1, close), media });
      index = close;
    } else if (character === "}") {
      stack.pop();
      prelude = "";
    } else {
      prelude += character;
    }
  }
  return found;
}

const RULES = rules(STYLE).filter((rule) => applies(rule.media, PHONE));

// Only compound class selectors with no combinator: the rules on this panel's
// own elements. A declaration from a later rule wins, as in the cascade for
// rules of equal or lower specificity; the panel's rules are single or
// descendant class selectors, so later wins among the matched ones.
function matches(selector, element, ancestors) {
  const parts = selector.split(/\s+(?![^[]*\])/).filter((part) => part && part !== ">");
  const compound = (part, node) => {
    if (!node || node.tag === "#root") return false;
    const tokens = part.match(/\.[\w-]+|^[a-z][\w-]*|\*|:[\w-]+(?:\([^)]*\))?|::?[\w-]+/gi) || [];
    if (tokens.some((token) => token.startsWith(":"))) return false;
    return tokens.every((token) => (token === "*" ? true : token.startsWith(".") ? node.classes.includes(token.slice(1)) : node.tag === token.toLowerCase()));
  };
  if (!compound(parts[parts.length - 1], element)) return false;
  let at = 0;
  for (let index = parts.length - 2; index >= 0; index -= 1) {
    while (at < ancestors.length && !compound(parts[index], ancestors[at])) at += 1;
    if (at >= ancestors.length) return false;
    at += 1;
  }
  return true;
}

function computed(element) {
  const ancestors = [];
  for (let node = element.parent; node && node.tag !== "#root"; node = node.parent) ancestors.push(node);
  const style = {};
  for (const rule of RULES) {
    if (!rule.selectors.some((selector) => matches(selector, element, ancestors))) continue;
    for (const declaration of rule.body.split(";")) {
      const colon = declaration.indexOf(":");
      if (colon < 0) continue;
      style[declaration.slice(0, colon).trim()] = declaration.slice(colon + 1).trim();
    }
  }
  return style;
}

function isGrid(style) {
  return /^(?:inline-)?grid$/.test(style.display || "");
}

function zeroMinimumColumns(style) {
  const columns = style["grid-template-columns"] || "";
  if (!columns) return false;
  const tracks = columns.match(/minmax\([^)]*\)|repeat\([^)]*\)\)?|[^\s]+/g) || [];
  return tracks.every((track) => /^minmax\(\s*0(?:px)?\s*,/.test(track));
}

function scrolls(style) {
  return /auto|scroll|hidden/.test(style["overflow-x"] || style.overflow || "");
}

// A <details> lays its content out in a slot of its own (the details content
// box): with the details as a grid, that slot is the grid item, block level,
// with an automatic minimum width.
function layoutParent(element) {
  const parent = element.parent;
  if (parent?.tag === "details" && element.tag !== "summary") {
    return { slot: true, parent };
  }
  return { slot: false, parent };
}

// Walks from the table's scroll box up to the Disk Path view and returns each
// grid item that takes the table's width as its minimum.
function carriedMinimums(html) {
  const tree = parse(html);
  const table = find(tree, (node) => node.classes.includes("fabric-diagnostic-table"));
  const view = find(tree, (node) => node.classes.includes("disk-path-view"));
  assert.ok(table && view, "the table and the Disk Path view are rendered");
  const box = table.parent;
  const boxStyle = computed(box);
  assert.ok(box.classes.includes("fabric-diagnostic-table-wrap"), "the table sits in its own box");
  assert.ok(scrolls(boxStyle), "the table's own box scrolls sideways");
  const tableMinimum = Number(/(\d+)px/.exec(computed(table)["min-width"] || "")?.[1] || 0);
  assert.ok(tableMinimum > PHONE, `the table is wider than a phone (${tableMinimum} px): the case #1133 needs`);
  const failures = [];
  const chain = [];
  let carried = true;
  let element = box;
  while (element && element !== view) {
    const { slot, parent } = layoutParent(element);
    const parentStyle = computed(parent);
    const name = (node) => `${node.tag}.${node.classes.join(".")}`;
    if (isGrid(parentStyle)) {
      // The grid item: the element itself, or its parent's content slot.
      const itemStyle = slot ? {} : computed(element);
      const itemName = slot ? `${name(parent)} content` : name(element);
      const automatic = !itemStyle["min-width"] || itemStyle["min-width"] === "auto";
      if (carried && automatic && !scrolls(itemStyle)) failures.push(`${itemName} in ${name(parent)}`);
      chain.push(`${itemName} in ${name(parent)} (${parentStyle["grid-template-columns"] || "auto"})`);
      if (zeroMinimumColumns(parentStyle)) carried = false;
    } else if (/flex/.test(parentStyle.display || "")) {
      failures.push(`${name(element)} is a flex item; the model covers grid only`);
    }
    element = parent;
  }
  assert.equal(element, view, "the scroll box is inside the Disk Path view");
  return { failures, chain, carried };
}

test("at 390 px the open fault evidence table scrolls inside its own box: no grid item above it takes the table's width (#1133)", () => {
  const html = renderOpenEvidence();
  const { failures, chain, carried } = carriedMinimums(html);
  assert.deepEqual(failures, [], `grid items that would widen the page to the table's minimum width:\n${chain.join("\n")}`);
  assert.equal(carried, false, `a zero-minimum column stops the table's width below the Disk Path view:\n${chain.join("\n")}`);
});

test("control: the model sees the page widen when the panel's columns keep an automatic minimum", () => {
  const html = renderOpenEvidence();
  const saved = RULES.length;
  // The rules as on a page whose evidence grids have implicit auto columns
  // and whose items keep their automatic minimum.
  RULES.push({ selectors: [".disk-path-evidence-stack", ".fabric-diagnostic-evidence", ".fabric-diagnostic-evidence-body", ".fabric-diagnostic-raw"], body: "grid-template-columns: none; min-width: auto", media: "" });
  try {
    const { failures } = carriedMinimums(html);
    assert.ok(failures.length > 0, "the widening is found");
  } finally {
    RULES.length = saved;
  }
});

// Once the table scrolls inside its box, that box is a scrollable region: it
// takes keyboard focus, so the table can be scrolled without a pointer, and
// it is named, as the history table's box is (#1116).
test("the table's scroll box is a named region that takes keyboard focus and shows it", () => {
  const html = renderOpenEvidence();
  const box = find(parse(html), (node) => node.classes.includes("fabric-diagnostic-table-wrap"));
  assert.match(box.attributes, /\brole="region"/);
  assert.match(box.attributes, /\btabindex="0"/);
  assert.match(box.attributes, /\baria-label="Recent event sample"/);
  const focus = rules(STYLE).find((rule) => rule.selectors.some((selector) => /\.fabric-diagnostic-table-wrap:focus-visible$/.test(selector)));
  assert.ok(focus, "a focus ring rule for the box");
  assert.match(focus.body, /outline:\s*2px solid var\(--selected\)/);
});
