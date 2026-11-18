"use strict";

// Header select widths (#1356). On the Storage Fabric page the header's
// selector cards sit in a four-track grid. The System track was a fixed share
// of that grid (minmax(170px, 1.15fr)), so between the 1240px breakpoint and
// about 1800px it came out near 200px wide: after the card padding, the select
// padding and the dropdown arrow, a 14-character name no longer fit and was
// cut off with no way to read the rest. Growing the System track to the
// select's max-content fixed that but handed the whole spare row to System (a
// select's max-content is its LONGEST option), so the Enclosure select fell
// to its 170px minimum and an 11-character enclosure label was cut off.
//
// The page now measures the room each select card needs for its SELECTED
// label and publishes it as a custom property on the header row; the two
// select tracks grow to that room when the row has it and share a shortfall.
//
// The first tests run a small model of the CSS grid track sizing for that
// header, built from the declarations in style.css, at the desktop widths.
// The model understands only the track forms the header uses and fails closed
// on anything else. Its glyph width and arrow allowance are estimates; the
// rendered regression in qa/fixture-suites/header-select-width.js measures
// the real controls in a browser.
//
// The cascade tests resolve every stylesheet rule that matches each header
// select (selector matching, specificity, source order, !important, media at
// each width and the interactive states), so a later or more specific rule
// that narrows the select or turns the ellipsis off fails here.
//
// The remaining tests pin the room measurement and the title helper: both
// pages keep each header select's `title` set to the full selected label,
// including after the selection changes.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");
const TOPOLOGY_STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_topology.css"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");
const MAIN_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const FABRIC_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/sas_fabric.html"), "utf8");
const MAIN_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

const SUPPORTED_WIDTHS = [390, 1280, 1600, 1920, 2560, 3840];
const DESKTOP_WIDTHS = [1280, 1600, 1920];
const SCROLLBAR_PX = 15;
const ROOT_FONT_PX = 16;
const MONO_CHARACTER_EM = 0.62;
const SELECT_ARROW_PX = 24;
const SHORT_SYSTEM = "nas-lab-east-1";
const LONG_SYSTEM = "SYNTH-HOST-ABCDEFGHIJKLMNOPQRSTUVWXYZ-ABCDEFGHIJKLMNOPQRSTUV";
const SHORT_ENCLOSURE = "SYNTH-SH10";
const SHELF_ENCLOSURE = "SYNTH-SHELF";
const LONG_ENCLOSURE = "SYNTH-SHELF-ABCDEFGHIJKLMNOPQRSTUVWXYZ-ABCDEFGHIJKLMNOPQRSTU";

test("the label fixtures have the lengths the header contract names", () => {
  assert.equal(SHORT_SYSTEM.length, 14);
  assert.equal(SHORT_ENCLOSURE.length, 10);
  assert.equal(SHELF_ENCLOSURE.length, 11);
  assert.equal(LONG_SYSTEM.length, 60);
  assert.equal(LONG_ENCLOSURE.length, 60);
});

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "));
}

// Every style rule with its selector list, body, source order and the chain
// of enclosing at-rule preludes.
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
          selectors: splitArguments(open.prelude),
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

const RULES = parseRules(STYLE_SOURCE);
const FABRIC_RULES = [...RULES, ...parseRules(TOPOLOGY_STYLE_SOURCE, 1)];

// Media conditions this stylesheet uses, evaluated for a screen at `viewport`
// with no forced colours and no reduced-motion preference. Anything else
// fails closed.
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

function ruleApplies(rule, viewport) {
  return rule.atRules.every((prelude) => mediaMatches(prelude, viewport));
}

// Declarations of a rule body, in order, with !important split off.
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

function lastDeclaration(body, property) {
  let value = null;
  for (const declaration of declarations(body)) {
    if (declaration.property === property) value = declaration.value;
  }
  return value;
}

// --- Selector matching -----------------------------------------------------
// An element is { tag, id, classes, attributes, parent }. `states` holds the
// interactive pseudo-classes in force (hover, focus, focus-visible, active,
// disabled). Sibling combinators and structural pseudo-classes are outside
// this model: they fail closed when the rest of the selector could match.

const STATE_PSEUDO = new Set(["hover", "focus", "focus-visible", "focus-within", "active", "disabled", "enabled", "checked", "invalid", "visited", "link", "placeholder-shown"]);

function splitArguments(text) {
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

// Compound selector -> list of simple selectors.
function parseCompound(text) {
  const simple = [];
  let index = 0;
  const readName = () => {
    const match = /^-?[A-Za-z_][\w-]*/.exec(text.slice(index));
    assert.ok(match, `cannot read a name in selector part ${text}`);
    index += match[0].length;
    return match[0];
  };
  while (index < text.length) {
    const character = text[index];
    if (character === "*") {
      index += 1;
    } else if (character === ".") {
      index += 1;
      simple.push({ type: "class", name: readName() });
    } else if (character === "#") {
      index += 1;
      simple.push({ type: "id", name: readName() });
    } else if (character === "[") {
      const close = text.indexOf("]", index);
      assert.ok(close > index, `unterminated attribute selector in ${text}`);
      const inner = text.slice(index + 1, close).trim();
      index = close + 1;
      const match = /^([\w-]+)\s*(?:([~|^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+)))?(?:\s+i)?$/.exec(inner);
      assert.ok(match, `attribute selector [${inner}] is outside this model`);
      simple.push({ type: "attribute", name: match[1], operator: match[2] || null, value: match[3] ?? match[4] ?? match[5] ?? null });
    } else if (character === ":") {
      const pseudoElement = text[index + 1] === ":";
      index += pseudoElement ? 2 : 1;
      const name = readName().toLowerCase();
      let argument = null;
      if (text[index] === "(") {
        let depth = 0;
        const open = index;
        for (; index < text.length; index += 1) {
          if (text[index] === "(") depth += 1;
          if (text[index] === ")") {
            depth -= 1;
            if (depth === 0) break;
          }
        }
        argument = text.slice(open + 1, index);
        index += 1;
      }
      const legacyElement = ["before", "after", "first-line", "first-letter"].includes(name);
      simple.push({ type: pseudoElement || legacyElement ? "pseudo-element" : "pseudo", name, argument });
    } else if (/[A-Za-z]/.test(character)) {
      simple.push({ type: "tag", name: readName().toLowerCase() });
    } else {
      assert.fail(`selector part ${text} is outside this model`);
    }
  }
  return simple;
}

// Selector -> [{ compound, combinator }] from left to right; `combinator` is
// the one that joins the compound to the next (to its right).
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
    if (depth === 0 && !bracket && (character === ">" || character === "+" || character === "~")) {
      flush();
      tokens.push(character);
    } else if (depth === 0 && !bracket && /\s/.test(character)) {
      flush();
      tokens.push(" ");
    } else {
      current += character;
    }
  }
  flush();
  const parts = [];
  let combinator = null;
  for (const token of tokens) {
    if (token === " " || token === ">" || token === "+" || token === "~") {
      if (token !== " " || combinator === null) combinator = token;
      continue;
    }
    if (parts.length) parts[parts.length - 1].combinator = combinator || " ";
    parts.push({ compound: parseCompound(token), combinator: null });
    combinator = null;
  }
  return parts;
}

function attributeMatches(element, simple) {
  const attributes = element.attributes || {};
  if (!(simple.name in attributes)) return false;
  if (!simple.operator) return true;
  const actual = String(attributes[simple.name]);
  const wanted = simple.value;
  switch (simple.operator) {
    case "=": return actual === wanted;
    case "~=": return actual.split(/\s+/).includes(wanted);
    case "^=": return actual.startsWith(wanted);
    case "$=": return actual.endsWith(wanted);
    case "*=": return actual.includes(wanted);
    case "|=": return actual === wanted || actual.startsWith(`${wanted}-`);
    default: return assert.fail(`attribute operator ${simple.operator}`);
  }
}

// true, false, or "unknown" when the compound uses syntax outside the model.
function compoundMatches(element, compound, states) {
  let unknown = false;
  for (const simple of compound) {
    if (simple.type === "pseudo-element") return false;
    if (simple.type === "tag" && simple.name !== element.tag) return false;
    if (simple.type === "id" && simple.name !== element.id) return false;
    if (simple.type === "class" && !(element.classes || []).includes(simple.name)) return false;
    if (simple.type === "attribute" && !attributeMatches(element, simple)) return false;
    if (simple.type === "pseudo") {
      if (STATE_PSEUDO.has(simple.name)) {
        const active = simple.name === "enabled" ? !states.has("disabled") : states.has(simple.name);
        if (!active) return false;
      } else if (simple.name === "root") {
        if (element.tag !== "html") return false;
      } else if (simple.name === "not" || simple.name === "is" || simple.name === "where") {
        const results = splitArguments(simple.argument).map((inner) => selectorMatches(element, inner, states));
        if (results.includes("unknown")) unknown = true;
        else if (simple.name === "not" ? results.includes(true) : !results.includes(true)) return false;
      } else {
        unknown = true;
      }
    }
  }
  return unknown ? "unknown" : true;
}

function selectorMatches(element, selector, states) {
  const parts = parseSelector(selector);
  const match = (index, candidate) => {
    const result = compoundMatches(candidate, parts[index].compound, states);
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
    for (const simple of compound) {
      if (simple.type === "id") score[0] += 1;
      else if (simple.type === "class" || simple.type === "attribute") score[1] += 1;
      else if (simple.type === "tag" || simple.type === "pseudo-element") score[2] += 1;
      else if (simple.type === "pseudo") {
        if (simple.name === "where") continue;
        if (simple.name === "not" || simple.name === "is") {
          const inner = splitArguments(simple.argument).map(specificity)
            .reduce((best, next) => (compareSpecificity(next, best) > 0 ? next : best), [0, 0, 0]);
          score[0] += inner[0];
          score[1] += inner[1];
          score[2] += inner[2];
        } else {
          score[1] += 1;
        }
      }
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

// Longhands each shorthand in scope sets. A property outside this table that
// could change one of the checked values (`all`, for example) fails closed.
const SHORTHANDS = {
  overflow: (value) => {
    const [x, y = x] = value.split(/\s+/);
    return { "overflow-x": x, "overflow-y": y };
  },
  "white-space": (value) => {
    const collapse = { normal: "collapse", nowrap: "collapse", pre: "preserve", "pre-wrap": "preserve", "pre-line": "preserve-breaks", "break-spaces": "break-spaces" };
    const wrap = { normal: "wrap", nowrap: "nowrap", pre: "nowrap", "pre-wrap": "wrap", "pre-line": "wrap", "break-spaces": "wrap" };
    if (value in wrap) return { "white-space-collapse": collapse[value], "text-wrap-mode": wrap[value] };
    return { "white-space": value, "text-wrap-mode": value.includes("nowrap") ? "nowrap" : (value.includes("wrap") ? "wrap" : "unknown") };
  },
  "text-wrap": (value) => ({ "text-wrap-mode": value.split(/\s+/).includes("nowrap") ? "nowrap" : "wrap" }),
};
const CHECKED_LONGHANDS = ["width", "min-width", "max-width", "text-overflow", "overflow-x", "text-wrap-mode", "display"];
const BLOCKED_PROPERTIES = new Set(["all"]);

// The cascaded value of each checked longhand for `element`, or a fail-closed
// error. Inline styles do not exist on the header selects; the template test
// below pins that.
function cascade(element, rules, viewport, states) {
  const winners = {};
  for (const rule of rules) {
    if (!ruleApplies(rule, viewport)) continue;
    let best = null;
    for (const selector of rule.selectors) {
      const matched = selectorMatches(element, selector, states);
      if (matched === "unknown") {
        // Syntax outside the model on a selector whose known parts match:
        // only a problem if it sets a checked property.
        const touches = declarations(rule.body).some(({ property }) => (
          CHECKED_LONGHANDS.includes(property) || property in SHORTHANDS || BLOCKED_PROPERTIES.has(property)
        ));
        assert.ok(!touches, `selector "${selector}" may match #${element.id} but is outside this model`);
        continue;
      }
      if (matched !== true) continue;
      const score = specificity(selector);
      if (!best || compareSpecificity(score, best) > 0) best = score;
    }
    if (!best) continue;
    for (const { property, value, important } of declarations(rule.body)) {
      assert.ok(!BLOCKED_PROPERTIES.has(property), `#${element.id}: "${property}" is outside this model`);
      const longhands = SHORTHANDS[property] ? SHORTHANDS[property](value) : { [property]: value };
      for (const [longhand, longhandValue] of Object.entries(longhands)) {
        if (!CHECKED_LONGHANDS.includes(longhand)) continue;
        const candidate = { value: longhandValue, important, specificity: best, sheet: rule.sheet, order: rule.order, selector: rule.selectors.join(", ") };
        const current = winners[longhand];
        const wins = !current
          || (important !== current.important ? important
            : (compareSpecificity(best, current.specificity) !== 0 ? compareSpecificity(best, current.specificity) > 0
              : (rule.sheet !== current.sheet ? rule.sheet > current.sheet : rule.order >= current.order)));
        if (wins) winners[longhand] = candidate;
      }
    }
  }
  return winners;
}

function element(tag, { id = "", classes = [], attributes = {} } = {}, parent = null) {
  return { tag, id, classes, attributes, parent };
}

function fabricSelect(id) {
  const html = element("html", { attributes: { lang: "en" } });
  const body = element("body", {}, html);
  const shell = element("div", { classes: ["app-shell", "fabric-page-shell"] }, body);
  const header = element("header", { classes: ["topbar", "fabric-page-topbar"] }, shell);
  const meta = element("div", { classes: ["topbar-meta"] }, header);
  const card = element("div", { classes: ["meta-card", "meta-select-card"] }, meta);
  return element("select", { id, classes: ["meta-select"], attributes: { id, class: "meta-select" } }, card);
}

function mainSelect(id, cardClass) {
  const html = element("html", { attributes: { lang: "en" } });
  const body = element("body", {}, html);
  const shell = element("div", { classes: ["app-shell"] }, body);
  const header = element("header", { classes: ["topbar"] }, shell);
  const meta = element("div", { classes: ["topbar-meta"] }, header);
  const card = element("div", { classes: ["meta-card", "meta-select-card", cardClass] }, meta);
  return element("select", { id, classes: ["meta-select"], attributes: { id, class: "meta-select" } }, card);
}

const HEADER_SELECTS = [
  { page: "Storage Fabric", name: "System", element: fabricSelect("fabric-system-select"), rules: FABRIC_RULES },
  { page: "Storage Fabric", name: "Enclosure", element: fabricSelect("fabric-enclosure-select"), rules: FABRIC_RULES },
  { page: "main page", name: "System", element: mainSelect("system-select", "system-select-card"), rules: RULES },
  { page: "main page", name: "Enclosure", element: mainSelect("enclosure-select", "enclosure-select-card"), rules: RULES },
];

test("the cascade model's element chains match the templates", () => {
  for (const [template, pattern] of [
    [FABRIC_TEMPLATE, /<div class="app-shell fabric-page-shell">\s*<header class="topbar fabric-page-topbar">[\s\S]*?<div class="topbar-meta">\s*<div class="meta-card meta-select-card">\s*<label[^>]*>System<\/label>\s*<select class="meta-select" id="fabric-system-select">[\s\S]*?<div class="meta-card meta-select-card">\s*<label[^>]*>Enclosure<\/label>\s*<select class="meta-select" id="fabric-enclosure-select">/],
    [MAIN_TEMPLATE, /<div class="app-shell">\s*<header class="topbar">[\s\S]*?<div class="topbar-meta">\s*<div class="meta-card meta-select-card system-select-card">\s*<label[^>]*>System<\/label>\s*<select class="meta-select" id="system-select">[\s\S]*?<div class="meta-card meta-select-card enclosure-select-card">\s*<label[^>]*>Enclosure \/ View<\/label>\s*<select class="meta-select" id="enclosure-select">/],
  ]) {
    assert.match(template, pattern);
  }
  for (const id of ["fabric-system-select", "fabric-enclosure-select", "system-select", "enclosure-select"]) {
    const tag = new RegExp(`<select[^>]*id="${id}"[^>]*>`).exec(FABRIC_TEMPLATE + MAIN_TEMPLATE)?.[0];
    assert.ok(tag, `${id} must exist`);
    assert.doesNotMatch(tag, /style=/, `${id} must not carry an inline style the cascade model cannot see`);
  }
});

const SELECT_STATES = [[], ["hover"], ["focus"], ["focus", "focus-visible"], ["disabled"], ["hover", "disabled"]];

for (const select of HEADER_SELECTS) {
  for (const viewport of SUPPORTED_WIDTHS) {
    test(`${select.page} ${select.name} select fills its card and ends a long label in an ellipsis at ${viewport}px`, () => {
      for (const states of SELECT_STATES) {
        const winners = cascade(select.element, select.rules, viewport, new Set(states));
        const value = (property) => winners[property]?.value ?? null;
        const where = (property) => `${property} at ${viewport}px [${states.join(",") || "default"}] from ${winners[property]?.selector ?? "(initial)"}`;
        assert.equal(value("width"), "100%", where("width"));
        assert.ok(["0", "0px"].includes(value("min-width")), where("min-width"));
        assert.ok([null, "none"].includes(value("max-width")), where("max-width"));
        assert.equal(value("text-overflow"), "ellipsis", where("text-overflow"));
        assert.equal(value("overflow-x"), "hidden", where("overflow-x"));
        assert.equal(value("text-wrap-mode"), "nowrap", where("text-wrap-mode"));
        assert.ok([null, "block", "inline-block"].includes(value("display")), where("display"));
      }
    });
  }
}

// The model must see what the reviewer's two mutations do.
test("the cascade model catches a narrowed select and a later clip override", () => {
  const select = HEADER_SELECTS[1];
  const narrowed = parseRules(STYLE_SOURCE.replace(/(\.meta-select \{\s*width:) 100%;/, "$1 60px;"));
  assert.notEqual(narrowed.length, 0);
  assert.equal(cascade(select.element, narrowed, 1600, new Set()).width.value, "60px");
  const clipped = parseRules(`${STYLE_SOURCE}\n.meta-select { text-overflow: clip; }\n`);
  assert.equal(cascade(select.element, clipped, 1600, new Set())["text-overflow"].value, "clip");
  const wrapped = parseRules(`${STYLE_SOURCE}\n.topbar-meta select { white-space: normal; }\n`);
  assert.equal(cascade(select.element, wrapped, 1600, new Set())["text-wrap-mode"].value, "wrap");
  const media = parseRules(`${STYLE_SOURCE}\n@media (max-width: 1700px) { #fabric-enclosure-select { width: 50% !important; } }\n`);
  assert.equal(cascade(select.element, media, 1600, new Set()).width.value, "50%");
  assert.equal(cascade(select.element, media, 1920, new Set()).width.value, "100%");
});

// --- Grid track model ------------------------------------------------------

function cascaded(groups, property, viewport, rules = RULES) {
  for (let group = groups.length - 1; group >= 0; group -= 1) {
    let winner = null;
    for (const rule of rules) {
      if (!ruleApplies(rule, viewport)) continue;
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
    if (/\s/.test(character) && depth === 0) {
      if (current) parts.push(current);
      current = "";
    } else {
      current += character;
    }
  }
  if (current) parts.push(current);
  return parts;
}

// Track forms the header uses, with `var(--name, fallback)` resolved from
// `variables` (the custom properties the page script sets on the row).
// Anything else fails closed.
function parseTracks(value, label, variables = {}) {
  const resolved = value.replace(/var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*?))?\s*\)/g, (whole, name, fallback) => {
    if (name in variables) return variables[name];
    assert.ok(fallback !== undefined, `${label}: ${whole} has no fallback`);
    return fallback;
  });
  const tracks = [];
  for (const part of splitTopLevel(resolved)) {
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
    const maxPx = /^([\d.]+)px$/.exec(minmax[2]);
    if (maxFr) tracks.push({ min, flex: Number(maxFr[1]) });
    else if (maxPx) tracks.push({ min, limit: Math.max(min, Number(maxPx[1])) });
    else if (minmax[2] === "max-content" || minmax[2] === "auto") tracks.push({ min, content: true });
    else assert.fail(`${label}: track ${part} is outside this model`);
  }
  return tracks;
}

// CSS grid column sizing for a definite-width container (CSS Grid 1, 12.4 to
// 12.7): base sizes are the minimums; non-flexible tracks get growth limits
// (a fixed max, or the max-content of their item); "maximize tracks" shares
// the free space EQUALLY between the non-flexible tracks, freezing each at
// its growth limit; the space left over goes to the fr tracks ("find the size
// of an fr", with tracks below their minimum frozen).
function sizeTracks(tracks, space, gap, contentWidths = []) {
  const sizes = tracks.map((track) => track.min);
  const limits = tracks.map((track, index) => {
    if (track.flex) return track.min;
    if (track.content) return Math.max(track.min, contentWidths[index] ?? track.min);
    return track.limit;
  });
  let free = space - gap * (tracks.length - 1) - sizes.reduce((sum, size) => sum + size, 0);
  let growing = tracks.map((track, index) => index).filter((index) => limits[index] > sizes[index]);
  while (free > 1e-9 && growing.length) {
    const share = free / growing.length;
    const step = Math.min(share, ...growing.map((index) => limits[index] - sizes[index]));
    growing.forEach((index) => { sizes[index] += step; });
    free -= step * growing.length;
    growing = growing.filter((index) => limits[index] - sizes[index] > 1e-9);
  }
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

// The card width a Storage Fabric header select needs to show `label` in
// full, with the select's padding, border and arrow and the card's padding
// and border. This is what the page script publishes for the selected label.
function fabricCardRoom(label, viewport = 1600) {
  const cardPadding = horizontalPadding(cascaded([[".meta-card"], [".fabric-page-topbar .meta-card"]], "padding", viewport), "card");
  const selectPadding = horizontalPadding(cascaded([[".meta-select"]], "padding", viewport), "select");
  const border = 2;
  return Math.ceil(label.length * characterPx()) + selectPadding + border + SELECT_ARROW_PX + cardPadding + border;
}

// The Storage Fabric header row at `viewport`. `system` and `enclosure` are
// { options, selected } label lists. A `max-content` track takes the room of
// the LONGEST option (a select's intrinsic width); a `var()` track takes the
// room the page script publishes for the SELECTED label.
function fabricHeader(viewport, system, enclosure) {
  const shellMax = px(cascaded([[".app-shell"], [".fabric-page-shell"]], "max-width", viewport), "shell max-width");
  const shellPadding = horizontalPadding(cascaded([[".app-shell", ".fabric-page-shell"]], "padding", viewport), "shell");
  const topbarPadding = horizontalPadding(cascaded([[".topbar", ".fabric-page-topbar"]], "padding", viewport), "topbar");
  const topbarGap = px(cascaded([[".topbar", ".fabric-page-topbar"]], "gap", viewport), "topbar gap");
  const metaGap = px(cascaded([[".topbar-meta"], [".fabric-page-topbar .topbar-meta"]], "gap", viewport), "meta gap");
  const border = 2;

  const shell = Math.min(viewport - SCROLLBAR_PX, shellMax);
  const topbarInner = shell - shellPadding - topbarPadding - border;
  const outer = parseTracks(cascaded([[".topbar", ".fabric-page-topbar"]], "grid-template-columns", viewport), "fabric topbar");
  const metaColumn = outer.length === 1 ? topbarInner : sizeTracks(outer, topbarInner, topbarGap)[1];
  const variables = {
    "--fabric-system-card-room": `${fabricCardRoom(system.selected, viewport)}px`,
    "--fabric-enclosure-card-room": `${fabricCardRoom(enclosure.selected, viewport)}px`,
  };
  const template = cascaded([[".fabric-page-topbar .topbar-meta"]], "grid-template-columns", viewport);
  const meta = parseTracks(template, "fabric meta", variables);
  const longest = (labels) => Math.max(...labels.options.map((label) => fabricCardRoom(label, viewport)));
  const sizes = sizeTracks(meta, metaColumn, metaGap, [longest(system), longest(enclosure)]);
  return {
    metaColumn,
    meta,
    sizes,
    metaGap,
    systemNeed: fabricCardRoom(system.selected, viewport),
    enclosureNeed: fabricCardRoom(enclosure.selected, viewport),
  };
}

const SYSTEM_OPTIONS = [SHORT_SYSTEM, LONG_SYSTEM];
const ENCLOSURE_OPTIONS = [SHORT_ENCLOSURE, SHELF_ENCLOSURE, LONG_ENCLOSURE];

function assertInsideRow(layout, viewport) {
  const used = layout.sizes.reduce((sum, size) => sum + size, 0) + layout.metaGap * (layout.sizes.length - 1);
  assert.ok(used <= layout.metaColumn + 0.5, `at ${viewport}px the header cards use ${used}px of ${layout.metaColumn}px`);
  layout.meta.forEach((track, index) => {
    assert.ok(layout.sizes[index] >= track.min - 0.5, `at ${viewport}px card ${index + 1} must keep its ${track.min}px minimum`);
  });
}

for (const viewport of DESKTOP_WIDTHS) {
  for (const enclosureLabel of [SHORT_ENCLOSURE, SHELF_ENCLOSURE]) {
    test(`Storage Fabric shows a 14-character system and the ${enclosureLabel.length}-character enclosure in full at ${viewport}px`, () => {
      const layout = fabricHeader(
        viewport,
        { options: SYSTEM_OPTIONS, selected: SHORT_SYSTEM },
        { options: ENCLOSURE_OPTIONS, selected: enclosureLabel },
      );
      const [systemCard, enclosureCard] = layout.sizes;
      assert.ok(systemCard >= layout.systemNeed - 0.5, `at ${viewport}px the System card is ${systemCard.toFixed(1)}px; "${SHORT_SYSTEM}" needs ${layout.systemNeed}px`);
      assert.ok(enclosureCard >= layout.enclosureNeed - 0.5, `at ${viewport}px the Enclosure card is ${enclosureCard.toFixed(1)}px; "${enclosureLabel}" needs ${layout.enclosureNeed}px`);
      assertInsideRow(layout, viewport);
    });
  }

  test(`Storage Fabric keeps the enclosure label whole beside a long system name at ${viewport}px`, () => {
    const layout = fabricHeader(
      viewport,
      { options: SYSTEM_OPTIONS, selected: LONG_SYSTEM },
      { options: ENCLOSURE_OPTIONS, selected: SHELF_ENCLOSURE },
    );
    assert.ok(layout.sizes[1] >= layout.enclosureNeed - 0.5, `at ${viewport}px the Enclosure card is ${layout.sizes[1].toFixed(1)}px; "${SHELF_ENCLOSURE}" needs ${layout.enclosureNeed}px`);
    assert.ok(layout.sizes[0] > layout.sizes[1], `at ${viewport}px the long System name takes the spare room`);
    assertInsideRow(layout, viewport);
  });

  test(`Storage Fabric shares a shortfall between two long labels at ${viewport}px`, () => {
    const layout = fabricHeader(
      viewport,
      { options: SYSTEM_OPTIONS, selected: LONG_SYSTEM },
      { options: ENCLOSURE_OPTIONS, selected: LONG_ENCLOSURE },
    );
    const [systemCard, enclosureCard] = layout.sizes;
    assert.ok(Math.abs(systemCard - enclosureCard) <= 1, `at ${viewport}px System ${systemCard.toFixed(1)}px and Enclosure ${enclosureCard.toFixed(1)}px share the row`);
    assert.ok(systemCard < layout.systemNeed, "the long labels do not fit and end in an ellipsis");
    assertInsideRow(layout, viewport);
  });
}

for (const viewport of SUPPORTED_WIDTHS) {
  test(`Storage Fabric header cards stay inside the header with long labels at ${viewport}px`, () => {
    const layout = fabricHeader(
      viewport,
      { options: SYSTEM_OPTIONS, selected: LONG_SYSTEM },
      { options: ENCLOSURE_OPTIONS, selected: LONG_ENCLOSURE },
    );
    assertInsideRow(layout, viewport);
  });
}

test("without the measured room the header falls back to the fixed shares", () => {
  const template = cascaded([[".fabric-page-topbar .topbar-meta"]], "grid-template-columns", 1600);
  const tracks = parseTracks(template, "fabric meta");
  assert.deepEqual(tracks, parseTracks("minmax(170px, 1.15fr) minmax(170px, 1.15fr) minmax(135px, 0.85fr) minmax(135px, 0.85fr)", "main"));
});

// --- Page scripts ----------------------------------------------------------

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
  test(`${page}: a header select's title is the full selected label`, () => {
    const syncSelectTitle = vm.runInNewContext(`(${functionSource(source, "syncSelectTitle")})`);
    const select = fakeSelect([SHORT_SYSTEM, LONG_SYSTEM], 1);
    syncSelectTitle(select);
    assert.equal(select.title, LONG_SYSTEM);
    select.selectedIndex = 0;
    syncSelectTitle(select);
    assert.equal(select.title, SHORT_SYSTEM);
    select.selectedIndex = -1;
    syncSelectTitle(select);
    assert.equal(select.title, "");
    syncSelectTitle(null);
  });
}

// The position of `pattern` inside `body`, or -1.
function indexOfPattern(body, pattern) {
  const match = pattern.exec(body);
  return match ? match.index : -1;
}

function handlerSource(source, marker) {
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, `${marker} must exist`);
  return source.slice(start, start + 240);
}

test("Storage Fabric sets both header select titles and their room after the values are set, and on change", () => {
  const render = functionSource(FABRIC_SOURCE, "renderSelectors");
  const systemValue = indexOfPattern(render, /elements\.systemSelect\.value =/);
  const enclosureValue = indexOfPattern(render, /elements\.enclosureSelect\.value =/);
  const systemTitle = indexOfPattern(render, /syncSelectTitle\(elements\.systemSelect\)/);
  const enclosureTitle = indexOfPattern(render, /syncSelectTitle\(elements\.enclosureSelect\)/);
  const room = indexOfPattern(render, /syncHeaderSelectRoom\(\)/);
  assert.ok(systemValue >= 0 && systemTitle > systemValue, "System title after its value");
  assert.ok(enclosureValue >= 0 && enclosureTitle > enclosureValue, "Enclosure title after its value");
  assert.ok(room > enclosureValue && room > systemValue, "room measured after both values");
  for (const name of ["systemSelect", "enclosureSelect"]) {
    const handler = handlerSource(FABRIC_SOURCE, `elements.${name}.addEventListener("change"`);
    assert.match(handler, new RegExp(`syncSelectTitle\\(elements\\.${name}\\)`));
    assert.match(handler, /syncHeaderSelectRoom\(\)/);
  }
});

test("the main page sets both header select titles after the values are set, and on change", () => {
  const render = functionSource(MAIN_SOURCE, "renderSelectors");
  const systemValue = indexOfPattern(render, /systemSelect\.value = state\.selectedSystemId/);
  const systemTitle = indexOfPattern(render, /syncSelectTitle\(systemSelect\)/);
  const enclosureValue = indexOfPattern(render, /enclosureSelect\.selectedIndex = 0;/);
  const enclosureTitle = indexOfPattern(render, /syncSelectTitle\(enclosureSelect\)/);
  const enclosureStatic = indexOfPattern(render, /showSelectAsTextInSnapshot\(enclosureSelect/);
  assert.ok(systemValue >= 0 && systemTitle > systemValue, "System title after its value");
  assert.ok(enclosureValue >= 0 && enclosureTitle > enclosureValue, "Enclosure title after its value");
  assert.ok(enclosureTitle < enclosureStatic, "Enclosure title before the snapshot text copy");
  assert.match(handlerSource(MAIN_SOURCE, 'systemSelect.addEventListener("change"'), /syncSelectTitle\(systemSelect\)/);
  assert.match(handlerSource(MAIN_SOURCE, 'enclosureSelect.addEventListener("change"'), /syncSelectTitle\(enclosureSelect\)/);
});

// A fake DOM for the room measurement: a select's rendered width is its
// padding and border, an arrow, and 9.6px per character of its LONGEST option.
const FAKE_CHARACTER_PX = 9.6;
const FAKE_SELECT_CHROME_PX = 26 + 20;
const FAKE_CARD_CHROME_PX = 26;

function fakeRoomDom() {
  const appended = [];
  const removed = [];
  const makeElement = (tag) => {
    const node = {
      tag,
      children: [],
      attributes: {},
      style: { cssText: "" },
      className: "",
      textContent: "",
      tabIndex: 0,
      parent: null,
      append(child) {
        this.children.push(child);
        child.parent = this;
      },
      setAttribute(name, value) {
        this.attributes[name] = String(value);
      },
      remove() {
        removed.push(this);
        if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
        this.parent = null;
      },
      getBoundingClientRect() {
        if (this.tag !== "select" || !this.parent || this.zeroWidth) return { width: 0 };
        const longest = Math.max(0, ...this.children.map((child) => child.textContent.trim().length));
        return { width: FAKE_SELECT_CHROME_PX + longest * FAKE_CHARACTER_PX };
      },
    };
    return node;
  };
  const row = {
    properties: {},
    style: {
      setProperty(name, value) { row.properties[name] = value; },
      removeProperty(name) { delete row.properties[name]; },
    },
  };
  const makeCard = () => {
    const card = makeElement("div");
    const append = card.append.bind(card);
    card.append = (child) => {
      appended.push(child);
      append(child);
    };
    return card;
  };
  const makeSelect = (labels, selectedIndex, card) => ({
    className: "meta-select",
    options: labels.map((label) => ({ textContent: `\n  ${label}\n` })),
    selectedIndex,
    closest(selector) {
      if (selector === ".meta-card") return card;
      if (selector === ".topbar-meta") return row;
      return null;
    },
  });
  const systemCard = makeCard();
  const enclosureCard = makeCard();
  const elements = {
    systemSelect: makeSelect(SYSTEM_OPTIONS, 0, systemCard),
    enclosureSelect: makeSelect(ENCLOSURE_OPTIONS, 1, enclosureCard),
  };
  const context = {
    elements,
    document: { createElement: makeElement },
    getComputedStyle: () => ({ paddingLeft: "12px", paddingRight: "12px", borderLeftWidth: "1px", borderRightWidth: "1px" }),
    WeakMap,
    Math,
    Number,
  };
  vm.createContext(context);
  vm.runInContext(`
    const selectCardRoomCache = new WeakMap();
    ${functionSource(FABRIC_SOURCE, "selectCardRoom")}
    ${functionSource(FABRIC_SOURCE, "syncHeaderSelectRoom")}
    this.syncHeaderSelectRoom = syncHeaderSelectRoom;
  `, context);
  return { context, elements, row, appended, removed, systemCard, enclosureCard };
}

const expectedRoom = (label) => `${Math.ceil(FAKE_SELECT_CHROME_PX + label.length * FAKE_CHARACTER_PX + FAKE_CARD_CHROME_PX)}px`;

test("Storage Fabric publishes the room each header select needs for its SELECTED label", () => {
  const dom = fakeRoomDom();
  dom.context.syncHeaderSelectRoom();
  assert.equal(dom.row.properties["--fabric-system-card-room"], expectedRoom(SHORT_SYSTEM));
  assert.equal(dom.row.properties["--fabric-enclosure-card-room"], expectedRoom(SHELF_ENCLOSURE));
  // The measuring copy holds only the selected label, is hidden from layout,
  // input and assistive technology, and is gone afterwards.
  assert.equal(dom.appended.length, 2);
  for (const probe of dom.appended) {
    assert.equal(probe.tag, "select");
    assert.equal(probe.className, "meta-select");
    assert.equal(probe.children.length, 1);
    assert.match(probe.style.cssText, /position:\s*absolute/);
    assert.match(probe.style.cssText, /visibility:\s*hidden/);
    assert.match(probe.style.cssText, /width:\s*auto/);
    assert.match(probe.style.cssText, /max-width:\s*none/);
    assert.equal(probe.attributes["aria-hidden"], "true");
    assert.equal(probe.tabIndex, -1);
    assert.ok(dom.removed.includes(probe));
  }
  assert.equal(dom.systemCard.children.length, 0);
  assert.equal(dom.enclosureCard.children.length, 0);

  // A new selection is measured again; an unchanged one is not.
  dom.elements.systemSelect.selectedIndex = 1;
  dom.elements.enclosureSelect.selectedIndex = 2;
  dom.context.syncHeaderSelectRoom();
  assert.equal(dom.row.properties["--fabric-system-card-room"], expectedRoom(LONG_SYSTEM));
  assert.equal(dom.row.properties["--fabric-enclosure-card-room"], expectedRoom(LONG_ENCLOSURE));
  assert.equal(dom.appended.length, 4);
  dom.context.syncHeaderSelectRoom();
  assert.equal(dom.appended.length, 4, "an unchanged label reuses its measurement");
});

test("Storage Fabric drops the published room when a label cannot be measured", () => {
  const dom = fakeRoomDom();
  dom.context.syncHeaderSelectRoom();
  assert.ok(dom.row.properties["--fabric-system-card-room"]);
  dom.elements.systemSelect.options = [];
  dom.elements.systemSelect.selectedIndex = -1;
  const createElement = dom.context.document.createElement;
  dom.context.document.createElement = (tag) => Object.assign(createElement(tag), { zeroWidth: true });
  dom.elements.enclosureSelect.selectedIndex = 0;
  dom.context.syncHeaderSelectRoom();
  assert.equal(dom.row.properties["--fabric-system-card-room"], undefined);
  assert.equal(dom.row.properties["--fabric-enclosure-card-room"], undefined, "a hidden page measures 0 and falls back to the fixed shares");
});

test("the header grid reads the published room for both select tracks", () => {
  const template = cascaded([[".fabric-page-topbar .topbar-meta"]], "grid-template-columns", 1600);
  assert.match(template, /^minmax\(170px, var\(--fabric-system-card-room, 1\.15fr\)\) minmax\(170px, var\(--fabric-enclosure-card-room, 1\.15fr\)\) /);
});
