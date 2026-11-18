"use strict";

// Long system name in the page heading (#1381). The main page's heading puts
// the selected system name in the line above the page title
// (`#header-eyebrow`); the summary line below the title can name an
// enclosure. Both sat in an unnamed column of the header's flex row with
// nothing that lets a word break. A long name with no space in it was one
// unbreakable word, so it overflowed the header at a narrow width and set the
// column's minimum width at a wider one: at 390 px a 60-character name made
// the document 549 px wide and the page scrolled sideways.
//
// The heading column is now `.topbar-copy`, and its eyebrow and summary lines
// break a word anywhere when it does not fit (`overflow-wrap: anywhere`,
// which, unlike `break-word`, also drops the line's min-content width to one
// character), so the full name wraps inside the header at every width. The
// title keeps whole words and the column keeps its automatic minimum width,
// so the title's longest word stays the column's floor and is never squeezed
// out of it. The Storage Fabric page uses the same header column and class.
//
// These tests resolve the stylesheet cascade (selector matching, specificity,
// source order, !important, media at each width, inheritance) for the
// heading column and its three lines, built from the templates' own markup,
// so removing the class, the rule, or a later rule that turns it off fails
// here. The rendered check is in qa/fixture-suites/header-select-width.js
// (main page, 14- and 60-character names, 390x844 and wider). The UI is a
// desktop application and narrow widths are not a supported layout
// (CONTRIBUTING.md); they are in the width list because the stylesheet's
// narrow media rules apply there and the finding was reported at 390 px, not
// as a product capability.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");
const TOPOLOGY_STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_topology.css"), "utf8");
const MAIN_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const FABRIC_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/sas_fabric.html"), "utf8");

// Every width the stylesheet's media queries distinguish, from a phone to 4K.
const WIDTHS = [320, 390, 640, 641, 760, 761, 820, 821, 1024, 1240, 1241, 1280, 1600, 1920, 2560, 3840];
const LONG_SYSTEM = "SYNTH-HOST-ABCDEFGHIJKLMNOPQRSTUVWXYZ-ABCDEFGHIJKLMNOPQRSTUV";

// --- Stylesheet parsing ----------------------------------------------------

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "));
}

function splitTopLevel(text) {
  const parts = [];
  let depth = 0;
  let current = "";
  for (const character of text) {
    if (character === "(") depth += 1;
    if (character === ")") depth -= 1;
    if (character === "," && depth === 0) {
      parts.push(current.trim());
      current = "";
    } else {
      current += character;
    }
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

function parseRules(css, sheet = 0) {
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
      assert.ok(open, "a stylesheet must not close more blocks than it opens");
      if (!open.prelude.startsWith("@")) {
        rules.push({
          selectors: splitTopLevel(open.prelude),
          body: source.slice(open.bodyStart, index),
          order: rules.length,
          sheet,
          atRules: stack.map((frame) => frame.prelude),
        });
      }
      start = index + 1;
    } else if (character === ";" && !stack.length) {
      start = index + 1;
    }
  }
  assert.equal(stack.length, 0, "a stylesheet must close every block it opens");
  return rules;
}

// Media conditions for a screen at `viewport`, no forced colours, no
// reduced-motion preference. Anything else fails closed.
function mediaMatches(prelude, viewport) {
  const query = /^@media (.+)$/.exec(prelude);
  if (!query) {
    if (/^@keyframes /.test(prelude)) return false;
    return assert.fail(`at-rule ${prelude} is outside this model`);
  }
  return query[1].split(/\s*,\s*/).some((alternative) => alternative.split(/\s+and\s+/).every((part) => {
    if (part === "screen" || part === "all") return true;
    if (part === "print") return false;
    const feature = /^\(\s*([a-z-]+)\s*:\s*([^)]+?)\s*\)$/.exec(part);
    assert.ok(feature, `media condition ${part} is outside this model`);
    const [, name, value] = feature;
    if (name === "max-width") return viewport <= Number(/^(\d+)px$/.exec(value)?.[1] ?? NaN);
    if (name === "min-width") return viewport >= Number(/^(\d+)px$/.exec(value)?.[1] ?? NaN);
    if (name === "forced-colors") return value === "none";
    if (name === "prefers-reduced-motion") return value === "no-preference";
    return assert.fail(`media feature ${name} is outside this model`);
  }));
}

function declarations(body) {
  const list = [];
  for (const raw of body.split(";")) {
    const colon = raw.indexOf(":");
    if (colon < 0) continue;
    const property = raw.slice(0, colon).trim().toLowerCase();
    let value = raw.slice(colon + 1).trim();
    const important = /!\s*important$/i.test(value);
    if (important) value = value.replace(/!\s*important$/i, "").trim();
    if (property) list.push({ property, value, important });
  }
  return list;
}

// --- Selector matching -----------------------------------------------------
// An element is { tag, id, classes, parent }. Interactive pseudo-classes
// count as matching (the worst case: a hover rule that stops wrapping must
// fail). Pseudo-elements never match the element itself. Syntax outside the
// model returns "unknown", which fails closed when the rule sets a checked
// property.

const STATE_PSEUDO = new Set(["hover", "focus", "focus-visible", "focus-within", "active", "visited", "link", "target"]);

function parseCompound(text) {
  const simple = [];
  const pattern = /([.#]?)(-?[A-Za-z_][\w-]*)|\*|(::?)([A-Za-z-]+)(\((?:[^()]|\([^()]*\))*\))?|\[([^\]]*)\]/y;
  let index = 0;
  while (index < text.length) {
    pattern.lastIndex = index;
    const match = pattern.exec(text);
    if (!match) return null;
    index = pattern.lastIndex;
    if (match[0] === "*") continue;
    if (match[2] !== undefined) {
      simple.push({ type: match[1] === "." ? "class" : match[1] === "#" ? "id" : "tag", name: match[1] ? match[2] : match[2].toLowerCase() });
    } else if (match[4] !== undefined) {
      const name = match[4].toLowerCase();
      const element = match[3] === "::" || ["before", "after", "first-line", "first-letter"].includes(name);
      simple.push({ type: element ? "pseudo-element" : "pseudo", name, argument: match[5] || null });
    } else {
      simple.push({ type: "attribute", text: match[6] });
    }
  }
  return simple;
}

function parseSelector(selector) {
  const tokens = [];
  let current = "";
  let depth = 0;
  let bracket = false;
  const flush = () => {
    if (current) tokens.push(current);
    current = "";
  };
  for (const character of selector.trim()) {
    if (character === "(") depth += 1;
    if (character === ")") depth -= 1;
    if (character === "[") bracket = true;
    if (character === "]") bracket = false;
    if (depth === 0 && !bracket && (character === ">" || character === "+" || character === "~" || /\s/.test(character))) {
      flush();
      tokens.push(/\s/.test(character) ? " " : character);
    } else {
      current += character;
    }
  }
  flush();
  const parts = [];
  let combinator = null;
  for (const token of tokens) {
    if ([" ", ">", "+", "~"].includes(token)) {
      if (token !== " " || combinator === null) combinator = token;
      continue;
    }
    if (parts.length) parts[parts.length - 1].combinator = combinator || " ";
    parts.push({ compound: parseCompound(token), combinator: null });
    combinator = null;
  }
  return parts;
}

function compoundMatches(element, compound) {
  if (compound === null) return "unknown";
  let unknown = false;
  for (const simple of compound) {
    if (simple.type === "pseudo-element") return false;
    if (simple.type === "tag" && simple.name !== element.tag) return false;
    if (simple.type === "id" && simple.name !== element.id) return false;
    if (simple.type === "class" && !element.classes.includes(simple.name)) return false;
    if (simple.type === "attribute") unknown = true;
    if (simple.type === "pseudo") {
      if (simple.name === "root") {
        if (element.tag !== "html") return false;
      } else if (["not", "is", "where"].includes(simple.name) && simple.argument) {
        const results = splitTopLevel(simple.argument.slice(1, -1)).map((inner) => selectorMatches(element, inner));
        if (results.includes("unknown")) unknown = true;
        else if (simple.name === "not" ? results.includes(true) : !results.includes(true)) return false;
      } else if (!STATE_PSEUDO.has(simple.name)) {
        unknown = true;
      }
    }
  }
  return unknown ? "unknown" : true;
}

function selectorMatches(element, selector) {
  const parts = parseSelector(selector);
  const match = (index, candidate) => {
    const result = compoundMatches(candidate, parts[index].compound);
    if (result === false) return false;
    if (index === 0) return result;
    const combinator = parts[index - 1].combinator;
    if (combinator === "+" || combinator === "~") return "unknown";
    if (combinator === ">") {
      if (!candidate.parent) return false;
      const parent = match(index - 1, candidate.parent);
      return parent === true ? result : parent;
    }
    let sawUnknown = false;
    for (let ancestor = candidate.parent; ancestor; ancestor = ancestor.parent) {
      const outer = match(index - 1, ancestor);
      if (outer === true) return result;
      if (outer === "unknown") sawUnknown = true;
    }
    return sawUnknown ? "unknown" : false;
  };
  return match(parts.length - 1, element);
}

function specificity(selector) {
  const score = [0, 0, 0];
  for (const { compound } of parseSelector(selector)) {
    for (const simple of compound || []) {
      if (simple.type === "pseudo" && ["not", "is", "where"].includes(simple.name) && simple.argument) {
        if (simple.name === "where") continue;
        const inner = splitTopLevel(simple.argument.slice(1, -1)).map(specificity)
          .reduce((best, next) => (compareSpecificity(next, best) > 0 ? next : best), [0, 0, 0]);
        for (let index = 0; index < 3; index += 1) score[index] += inner[index];
        continue;
      }
      if (simple.type === "id") score[0] += 1;
      else if (simple.type === "class" || simple.type === "attribute" || simple.type === "pseudo") score[1] += 1;
      else score[2] += 1;
    }
  }
  return score;
}

function compareSpecificity(left, right) {
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] - right[index];
  }
  return 0;
}

// --- Cascade ---------------------------------------------------------------

// Longhands the heading contract reads. `word-wrap` is the legacy name of
// `overflow-wrap`; `white-space` and `text-wrap` set whether text wraps.
const SHORTHANDS = {
  "word-wrap": (value) => ({ "overflow-wrap": value }),
  "white-space": (value) => ({ "text-wrap-mode": /nowrap|^pre$/.test(value) ? "nowrap" : "wrap" }),
  "text-wrap": (value) => ({ "text-wrap-mode": value.split(/\s+/).includes("nowrap") ? "nowrap" : "wrap" }),
  flex: (value) => ({ flex: value }),
};
const CHECKED = ["min-width", "width", "max-width", "overflow-wrap", "word-break", "text-wrap-mode", "display"];
const INHERITED = new Set(["overflow-wrap", "word-break", "text-wrap-mode"]);
const BLOCKED = new Set(["all"]);

function ownValues(element, rules, viewport) {
  const winners = {};
  for (const rule of rules) {
    if (!rule.atRules.every((prelude) => mediaMatches(prelude, viewport))) continue;
    let best = null;
    for (const selector of rule.selectors) {
      const matched = selectorMatches(element, selector);
      if (matched === "unknown") {
        const touches = declarations(rule.body).some(({ property }) => (
          CHECKED.includes(property) || property in SHORTHANDS || BLOCKED.has(property)
        ));
        assert.ok(!touches, `selector "${selector}" may match ${describe(element)} but is outside this model`);
        continue;
      }
      if (matched !== true) continue;
      const score = specificity(selector);
      if (!best || compareSpecificity(score, best) > 0) best = score;
    }
    if (!best) continue;
    for (const { property, value, important } of declarations(rule.body)) {
      assert.ok(!BLOCKED.has(property), `${describe(element)}: "${property}" is outside this model`);
      const longhands = SHORTHANDS[property] ? SHORTHANDS[property](value) : { [property]: value };
      for (const [longhand, longhandValue] of Object.entries(longhands)) {
        if (!CHECKED.includes(longhand)) continue;
        const current = winners[longhand];
        const wins = !current
          || (important !== current.important ? important
            : (compareSpecificity(best, current.specificity) !== 0 ? compareSpecificity(best, current.specificity) > 0
              : (rule.sheet !== current.sheet ? rule.sheet > current.sheet : rule.order >= current.order)));
        if (wins) winners[longhand] = { value: longhandValue, important, specificity: best, sheet: rule.sheet, order: rule.order, selector: rule.selectors.join(", ") };
      }
    }
  }
  return winners;
}

// Computed value of `property`: own cascade, then `inherit` or the
// parent's value for an inherited property, else null (the initial value).
function computed(element, property, rules, viewport) {
  const own = ownValues(element, rules, viewport)[property];
  if (own && own.value !== "inherit" && own.value !== "unset") return own;
  if ((INHERITED.has(property) || own?.value === "inherit") && element.parent) return computed(element.parent, property, rules, viewport);
  return null;
}

function describe(element) {
  return `${element.tag}${element.id ? `#${element.id}` : ""}${element.classes.map((name) => `.${name}`).join("")}`;
}

// --- Markup ----------------------------------------------------------------

function classesOf(tag) {
  return (/\bclass="([^"]*)"/.exec(tag)?.[1] || "").split(/\s+/).filter(Boolean);
}

function node(tag, classes = [], id = "", parent = null) {
  return { tag, id, classes, parent };
}

// The heading column and its lines as the template writes them.
function headingModel(template, eyebrowId) {
  const header = /<div class="([^"]*)">\s*<header class="([^"]*)">\s*(<div\b[^>]*>)\s*<p class="([^"]*)" id="([^"]*)">[\s\S]*?<h1>[\s\S]*?<\/h1>\s*<p class="([^"]*)" id="([^"]*)">/.exec(template);
  assert.ok(header, "the header's first child is the heading column: eyebrow, h1, summary");
  assert.equal(header[5], eyebrowId);
  assert.doesNotMatch(header[3], /style=/, "the heading column must not carry an inline style the model cannot see");
  const html = node("html");
  const body = node("body", [], "", html);
  const shell = node("div", header[1].split(/\s+/), "", body);
  const topbar = node("header", header[2].split(/\s+/), "", shell);
  const column = node("div", classesOf(header[3]), "", topbar);
  return {
    topbar,
    column,
    lines: [
      node("p", header[4].split(/\s+/), header[5], column),
      node("h1", [], "", column),
      node("p", header[6].split(/\s+/), header[7], column),
    ],
  };
}

const RULES = parseRules(STYLE_SOURCE);
const FABRIC_RULES = [...RULES, ...parseRules(TOPOLOGY_STYLE_SOURCE, 1)];
const PAGES = [
  { page: "main page", template: MAIN_TEMPLATE, eyebrow: "header-eyebrow", rules: RULES },
  { page: "Storage Fabric", template: FABRIC_TEMPLATE, eyebrow: "fabric-page-eyebrow", rules: FABRIC_RULES },
];

test("the main page heading shows the selected system name above the page title", () => {
  assert.equal(LONG_SYSTEM.length, 60);
  assert.match(MAIN_TEMPLATE, /<p class="eyebrow" id="header-eyebrow">\s*\{\{ profile\.eyebrow if profile and profile\.eyebrow else \(snapshot\.selected_system_label or "TrueNAS JBOD Enclosure UI"\) \}\}/);
});

for (const { page, template, eyebrow } of PAGES) {
  test(`${page} heading column is the header's named .topbar-copy column`, () => {
    const model = headingModel(template, eyebrow);
    assert.ok(model.column.classes.includes("topbar-copy"), `${page}: heading column classes are [${model.column.classes}]`);
  });
}

function where(winner, property, viewport) {
  return `${property} at ${viewport}px from ${winner?.selector ?? "(initial)"}`;
}

for (const { page, template, eyebrow, rules } of PAGES) {
  for (const viewport of WIDTHS) {
    test(`${page} heading lets a long name wrap inside the header at ${viewport}px`, () => {
      const model = headingModel(template, eyebrow);
      // The column keeps its automatic minimum width (the title's longest
      // word) and no fixed width; the name lines must not set that minimum.
      const minWidth = computed(model.column, "min-width", rules, viewport);
      assert.ok([undefined, "auto"].includes(minWidth?.value), `heading column ${where(minWidth, "min-width", viewport)} is ${minWidth?.value}`);
      const width = computed(model.column, "width", rules, viewport);
      assert.ok([undefined, "auto", "100%"].includes(width?.value), `heading column ${where(width, "width", viewport)}`);
      const [eyebrowLine, title, summary] = model.lines;
      for (const line of [eyebrowLine, summary]) {
        const label = describe(line);
        // A word longer than the line breaks, and the line's min-content
        // width is one character, so a name never widens the column.
        const overflowWrap = computed(line, "overflow-wrap", rules, viewport);
        const wordBreak = computed(line, "word-break", rules, viewport);
        assert.ok(
          overflowWrap?.value === "anywhere" || wordBreak?.value === "break-all",
          `${label} ${where(overflowWrap, "overflow-wrap", viewport)} is ${overflowWrap?.value ?? "normal"}; ${where(wordBreak, "word-break", viewport)} is ${wordBreak?.value ?? "normal"}`,
        );
      }
      for (const line of [eyebrowLine, title, summary]) {
        const label = describe(line);
        const wrapMode = computed(line, "text-wrap-mode", rules, viewport);
        assert.notEqual(wrapMode?.value, "nowrap", `${label} ${where(wrapMode, "text-wrap-mode", viewport)}`);
        for (const property of ["width", "min-width"]) {
          const value = computed(line, property, rules, viewport);
          assert.ok([undefined, "auto", "0", "0px", "100%"].includes(value?.value), `${label} ${where(value, property, viewport)} is ${value?.value}`);
        }
        const display = computed(line, "display", rules, viewport);
        assert.notEqual(display?.value, "none", `${label} ${where(display, "display", viewport)}: the heading stays visible`);
      }
    });
  }
}

// The model must see the mutations that bring the bug back.
test("the heading cascade model catches a removed rule and later overrides", () => {
  const model = headingModel(MAIN_TEMPLATE, "header-eyebrow");
  if (!model.column.classes.includes("topbar-copy")) return assert.fail("the heading column has no .topbar-copy class");
  const [eyebrow] = model.lines;
  const nowrap = parseRules(`${STYLE_SOURCE}\n.topbar .eyebrow { white-space: nowrap; }\n`);
  assert.equal(computed(eyebrow, "text-wrap-mode", nowrap, 390).value, "nowrap");
  const normal = parseRules(`${STYLE_SOURCE}\n@media (max-width: 820px) { #header-eyebrow { overflow-wrap: normal; } }\n`);
  assert.equal(computed(eyebrow, "overflow-wrap", normal, 390).value, "normal");
  assert.equal(computed(eyebrow, "overflow-wrap", normal, 1600).value, "anywhere");
  const floor = parseRules(`${STYLE_SOURCE}\n.topbar > div { min-width: 600px !important; }\n`);
  assert.equal(computed(model.column, "min-width", floor, 390).value, "600px");
  const breakWord = parseRules(`${STYLE_SOURCE}\n.topbar .subtle { overflow-wrap: break-word; }\n`);
  assert.equal(computed(model.lines[2], "overflow-wrap", breakWord, 1600).value, "break-word");
  const removed = parseRules(STYLE_SOURCE.replace(/\.topbar-copy \.eyebrow,\s*\.topbar-copy \.subtle\s*\{[^}]*\}/, ""));
  assert.equal(removed.length, RULES.length - 1);
  assert.equal(computed(eyebrow, "overflow-wrap", removed, 390), null);
  assert.equal(computed(model.lines[2], "overflow-wrap", removed, 390), null);
});
