"use strict";

// System dropdown width (#1356). On the Storage Fabric page the header's
// selector cards sit in a four-track grid. The System track was a fixed share
// of that grid (minmax(170px, 1.15fr)), so between the 1240px breakpoint and
// about 1800px it came out near 200px wide: after the card padding, the select
// padding and the dropdown arrow, a 14-character name no longer fit and was
// cut off with no way to read the rest.
//
// The first tests run a small model of the CSS grid track sizing for that
// header, built from the declarations in style.css, at every supported width.
// The model understands only the track forms the header uses and fails closed
// on anything else. It measures the room the select gets, not real glyphs: the
// character width below is a conservative monospace estimate, and the arrow
// allowance is an estimate of the native control. It cannot see fonts, zoom,
// or the main page's flex header; the browser screenshots cover those.
//
// The other tests pin the fallback for names that do not fit: the select ends
// in an ellipsis, and both pages keep the select's `title` set to the full
// selected name, including after the selection changes.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const MAIN_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

const SUPPORTED_WIDTHS = [390, 1280, 1600, 1920, 2560, 3840];
const SCROLLBAR_PX = 15;
const ROOT_FONT_PX = 16;
const MONO_CHARACTER_EM = 0.62;
const SELECT_ARROW_PX = 24;
const SHORT_NAME = "nas-lab-east-1";
const LONG_NAME = "storage-lab-east-primary-array-with-a-very-long-synthetic-name";

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "));
}

// Every rule with its selector list, body, source order and the max-width of
// the enclosing @media block (Infinity at top level, null for other at-rules).
function parseRules(css) {
  const source = stripComments(css);
  const rules = [];
  const stack = [];
  let start = 0;
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === "{") {
      stack.push({ prelude: source.slice(start, index).trim().replace(/\s+/g, " "), bodyStart: index + 1 });
      start = index + 1;
    } else if (character === "}") {
      const open = stack.pop();
      assert.ok(open, "style.css must not close more blocks than it opens");
      if (!open.prelude.startsWith("@")) {
        const outer = stack.length ? stack[stack.length - 1].prelude : null;
        let maxWidth = Infinity;
        if (outer !== null) {
          const media = /^@media \(max-width: (\d+)px\)$/.exec(outer);
          maxWidth = media ? Number(media[1]) : null;
        }
        rules.push({
          selectors: open.prelude.split(",").map((selector) => selector.trim()),
          body: source.slice(open.bodyStart, index),
          order: rules.length,
          maxWidth,
        });
      }
      start = index + 1;
    }
  }
  assert.equal(stack.length, 0, "style.css must close every block it opens");
  return rules;
}

const RULES = parseRules(STYLE_SOURCE);

function lastDeclaration(body, property) {
  const pattern = new RegExp(`(?:^|[;{\\s])${property}\\s*:\\s*([^;]+);`, "g");
  let value = null;
  let match;
  while ((match = pattern.exec(body))) value = match[1].trim();
  return value;
}

// The cascaded value at a viewport width. `groups` lists selectors from lowest
// to highest specificity; inside a group the later rule wins.
function cascaded(groups, property, viewport) {
  for (let group = groups.length - 1; group >= 0; group -= 1) {
    let winner = null;
    for (const rule of RULES) {
      if (rule.maxWidth === null || viewport > rule.maxWidth) continue;
      if (!rule.selectors.some((selector) => groups[group].includes(selector))) continue;
      const value = lastDeclaration(rule.body, property);
      if (value !== null) winner = value;
    }
    if (winner !== null) return winner;
  }
  return null;
}

function px(value, label) {
  const match = /^(-?[\d.]+)px$/.exec(value || "");
  assert.ok(match, `${label} must be a px length for this model, got ${value}`);
  return Number(match[1]);
}

// Horizontal padding (left + right) from a padding shorthand.
function horizontalPadding(value, label) {
  const parts = String(value || "").split(/\s+/);
  assert.ok(parts.length >= 1 && parts.length <= 4, `${label} padding must be a shorthand, got ${value}`);
  const right = parts.length === 1 ? parts[0] : parts[1];
  const left = parts.length === 4 ? parts[3] : right;
  return px(right, `${label} padding`) + px(left, `${label} padding`);
}

function splitTopLevel(value) {
  const parts = [];
  let depth = 0;
  let current = "";
  for (const character of value.trim()) {
    if (character === "(") depth += 1;
    if (character === ")") depth -= 1;
    if (character === " " && depth === 0) {
      if (current) parts.push(current);
      current = "";
    } else {
      current += character;
    }
  }
  if (current) parts.push(current);
  return parts;
}

// Track forms the header uses. Anything else fails closed.
function parseTracks(value, label) {
  const tracks = [];
  for (const part of splitTopLevel(value)) {
    const repeat = /^repeat\((\d+),\s*(.+)\)$/.exec(part);
    if (repeat) {
      for (let count = 0; count < Number(repeat[1]); count += 1) tracks.push(...parseTracks(repeat[2], label));
      continue;
    }
    const fr = /^([\d.]+)fr$/.exec(part);
    if (fr) {
      tracks.push({ min: 0, flex: Number(fr[1]) });
      continue;
    }
    const minmax = /^minmax\(\s*([\d.]+)(?:px)?\s*,\s*([^)]+?)\s*\)$/.exec(part);
    assert.ok(minmax, `${label}: track ${part} is outside this model`);
    const min = Number(minmax[1]);
    const maxFr = /^([\d.]+)fr$/.exec(minmax[2]);
    if (maxFr) tracks.push({ min, flex: Number(maxFr[1]) });
    else if (minmax[2] === "max-content" || minmax[2] === "auto") tracks.push({ min, content: true });
    else assert.fail(`${label}: track ${part} is outside this model`);
  }
  return tracks;
}

// CSS grid column sizing for definite-width containers: base sizes, then
// content-sized tracks grow toward their max-content, then the remaining
// space is shared by the fr tracks ("find the size of an fr").
function sizeTracks(tracks, space, gap, contentWidths = []) {
  const sizes = tracks.map((track) => track.min);
  let free = space - gap * (tracks.length - 1) - sizes.reduce((sum, size) => sum + size, 0);
  tracks.forEach((track, index) => {
    if (!track.content || free <= 0) return;
    const want = Math.max(track.min, contentWidths[index] ?? track.min);
    const grow = Math.min(free, want - sizes[index]);
    sizes[index] += grow;
    free -= grow;
  });
  const flexible = new Set(tracks.map((track, index) => (track.flex ? index : -1)).filter((index) => index >= 0));
  for (;;) {
    const fixed = tracks.reduce((sum, track, index) => sum + (flexible.has(index) ? 0 : sizes[index]), 0);
    const factors = [...flexible].reduce((sum, index) => sum + tracks[index].flex, 0);
    if (!factors) break;
    const fr = Math.max(0, space - gap * (tracks.length - 1) - fixed) / Math.max(1, factors);
    const frozen = [...flexible].filter((index) => tracks[index].flex * fr < tracks[index].min);
    if (!frozen.length) {
      flexible.forEach((index) => { sizes[index] = tracks[index].flex * fr; });
      break;
    }
    frozen.forEach((index) => flexible.delete(index));
  }
  return sizes;
}

function characterPx() {
  const font = cascaded([[".meta-select"]], "font-family", 3840);
  assert.equal(font, "var(--mono)", "the model's character width assumes the monospace select font");
  return MONO_CHARACTER_EM * ROOT_FONT_PX;
}

// Room the System select's text gets on the Storage Fabric page, and the card
// width it would need to show `name` in full.
function fabricSystemSelect(viewport, name) {
  const shellMax = px(cascaded([[".app-shell"], [".fabric-page-shell"]], "max-width", viewport), "shell max-width");
  const shellPadding = horizontalPadding(cascaded([[".app-shell", ".fabric-page-shell"]], "padding", viewport), "shell");
  const topbarPadding = horizontalPadding(cascaded([[".topbar", ".fabric-page-topbar"]], "padding", viewport), "topbar");
  const topbarGap = px(cascaded([[".topbar", ".fabric-page-topbar"]], "gap", viewport), "topbar gap");
  const metaGap = px(cascaded([[".topbar-meta"], [".fabric-page-topbar .topbar-meta"]], "gap", viewport), "meta gap");
  const cardPadding = horizontalPadding(cascaded([[".meta-card"], [".fabric-page-topbar .meta-card"]], "padding", viewport), "card");
  const selectPadding = horizontalPadding(cascaded([[".meta-select"]], "padding", viewport), "select");
  const border = 2;

  const shell = Math.min(viewport - SCROLLBAR_PX, shellMax);
  const topbarInner = shell - shellPadding - topbarPadding - border;
  const outer = parseTracks(cascaded([[".topbar", ".fabric-page-topbar"]], "grid-template-columns", viewport), "fabric topbar");
  const metaColumn = outer.length === 1 ? topbarInner : sizeTracks(outer, topbarInner, topbarGap)[1];
  const meta = parseTracks(cascaded([[".fabric-page-topbar .topbar-meta"]], "grid-template-columns", viewport), "fabric meta");

  const selectChrome = selectPadding + border + SELECT_ARROW_PX;
  const cardChrome = cardPadding + border;
  const needed = Math.ceil(name.length * characterPx()) + selectChrome + cardChrome;
  const sizes = sizeTracks(meta, metaColumn, metaGap, [needed]);
  return { metaColumn, meta, sizes, metaGap, needed, track: sizes[0] };
}

for (const viewport of SUPPORTED_WIDTHS) {
  test(`Storage Fabric System select shows a 14-character name in full at ${viewport}px`, () => {
    const layout = fabricSystemSelect(viewport, SHORT_NAME);
    assert.ok(
      layout.track >= layout.needed,
      `at ${viewport}px the System card is ${layout.track.toFixed(1)}px wide; `
        + `"${SHORT_NAME}" needs ${layout.needed}px with the arrow`,
    );
  });

  test(`Storage Fabric System select with a long name stays inside the header at ${viewport}px`, () => {
    const layout = fabricSystemSelect(viewport, LONG_NAME);
    const used = layout.sizes.reduce((sum, size) => sum + size, 0) + layout.metaGap * (layout.sizes.length - 1);
    assert.ok(used <= layout.metaColumn + 0.5, `at ${viewport}px the header cards use ${used}px of ${layout.metaColumn}px`);
    layout.meta.forEach((track, index) => {
      assert.ok(layout.sizes[index] >= track.min - 0.5, `at ${viewport}px card ${index + 1} must keep its ${track.min}px minimum`);
    });
  });
}

test("the header selects end a name that does not fit in an ellipsis", () => {
  const rule = RULES.find((candidate) => candidate.maxWidth === Infinity && candidate.selectors.includes(".meta-select"));
  assert.ok(rule, ".meta-select must have a top-level rule");
  assert.equal(lastDeclaration(rule.body, "text-overflow"), "ellipsis");
  assert.equal(lastDeclaration(rule.body, "white-space"), "nowrap");
  assert.equal(lastDeclaration(rule.body, "overflow"), "hidden");
});

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = source.indexOf("{", source.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
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
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  return assert.fail(`function ${name} must have a complete body`);
}

function fakeSelect(labels, selectedIndex) {
  const select = {
    options: labels.map((label) => ({ textContent: `\n  ${label}\n` })),
    selectedIndex,
    title: "stale",
  };
  return select;
}

for (const [page, source] of [["Storage Fabric", FABRIC_SOURCE], ["main page", MAIN_SOURCE]]) {
  test(`${page}: the System select's title is the full selected name`, () => {
    const syncSelectTitle = vm.runInNewContext(`(${functionSource(source, "syncSelectTitle")})`);
    const select = fakeSelect([SHORT_NAME, LONG_NAME], 1);
    syncSelectTitle(select);
    assert.equal(select.title, LONG_NAME);
    select.selectedIndex = 0;
    syncSelectTitle(select);
    assert.equal(select.title, SHORT_NAME);
    select.selectedIndex = -1;
    syncSelectTitle(select);
    assert.equal(select.title, "");
    syncSelectTitle(null);
  });
}

test("Storage Fabric sets the System title on render and on change", () => {
  assert.match(functionSource(FABRIC_SOURCE, "renderSelectors"), /syncSelectTitle\(elements\.systemSelect\)/);
  const change = FABRIC_SOURCE.indexOf('elements.systemSelect.addEventListener("change"');
  assert.notEqual(change, -1);
  assert.match(FABRIC_SOURCE.slice(change, change + 200), /syncSelectTitle\(elements\.systemSelect\)/);
});

test("the main page sets the System title on render and on change", () => {
  assert.match(functionSource(MAIN_SOURCE, "renderSelectors"), /syncSelectTitle\(systemSelect\)/);
  const change = MAIN_SOURCE.indexOf('systemSelect.addEventListener("change"');
  assert.notEqual(change, -1);
  assert.match(MAIN_SOURCE.slice(change, change + 200), /syncSelectTitle\(systemSelect\)/);
});
