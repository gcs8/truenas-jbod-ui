"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

const MIN_REM = 0.75;
const MIN_PX = 12;
const SECONDARY_TEXT_SELECTOR = /\.(fabric-|disk-path-|slot-)/;
const GLYPH_PSEUDO_ELEMENT = /::(before|after)/;

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "));
}

function parseRules(css) {
  const source = stripComments(css);
  const rules = [];
  const stack = [];
  let start = 0;
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === "{") {
      stack.push({ selector: source.slice(start, index).trim(), bodyStart: index + 1 });
      start = index + 1;
    } else if (character === "}") {
      const open = stack.pop();
      assert.ok(open, "style.css must not close more blocks than it opens");
      rules.push({
        selector: open.selector.replace(/\s+/g, " "),
        body: source.slice(open.bodyStart, index),
        line: source.slice(0, open.bodyStart).split("\n").length,
      });
      start = index + 1;
    }
  }
  assert.equal(stack.length, 0, "style.css must close every block it opens");
  return rules;
}

function declarations(body, property) {
  const pattern = new RegExp(`(?:^|[;\\s])${property}\\s*:\\s*([^;]+);`, "g");
  const values = [];
  let match;
  while ((match = pattern.exec(body))) {
    values.push(match[1].trim());
  }
  return values;
}

function fontSizeTooSmall(value) {
  const rem = /^([\d.]+)rem$/.exec(value);
  if (rem) {
    return Number(rem[1]) < MIN_REM;
  }
  const px = /^([\d.]+)px$/.exec(value);
  if (px) {
    return Number(px[1]) < MIN_PX;
  }
  return false;
}

function relativeLuminance(hex) {
  const value = parseInt(hex.slice(1), 16);
  const [red, green, blue] = [value >> 16, (value >> 8) & 255, value & 255].map((channel) => {
    const scaled = channel / 255;
    return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(foreground, background) {
  const [lighter, darker] = [relativeLuminance(foreground), relativeLuminance(background)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

const RULES = parseRules(STYLE_SOURCE);

test("bay, fabric and disk-path text never drops below 12px", () => {
  const violations = [];
  for (const rule of RULES) {
    if (rule.selector.startsWith("@")) {
      continue;
    }
    if (!SECONDARY_TEXT_SELECTOR.test(rule.selector) || GLYPH_PSEUDO_ELEMENT.test(rule.selector)) {
      continue;
    }
    for (const value of declarations(rule.body, "font-size")) {
      if (fontSizeTooSmall(value)) {
        violations.push(`${rule.selector} (line ${rule.line}): font-size ${value}`);
      }
    }
  }
  assert.deepEqual(violations, [], `text below the ${MIN_REM}rem floor:\n${violations.join("\n")}`);
});

test("diagnostic chips are not forced to uppercase", () => {
  const uppercased = RULES.filter(
    (rule) =>
      rule.selector.includes(".fabric-diagnostic-") &&
      declarations(rule.body, "text-transform").some((value) => value === "uppercase"),
  ).map((rule) => `${rule.selector} (line ${rule.line})`);
  assert.deepEqual(uppercased, []);
});

test("the print stylesheet keeps bay colours and flattens the dark theme", () => {
  const printRules = RULES.filter((rule) => rule.selector === "@media print");
  assert.equal(printRules.length, 1, "style.css must carry exactly one @media print block");
  const printBody = printRules[0].body;
  assert.match(printBody, /print-color-adjust:\s*exact/);
  assert.match(printBody, /-webkit-print-color-adjust:\s*exact/);
  assert.match(printBody, /\.slot-tile[^{]*\{[^}]*print-color-adjust:\s*exact/);
  assert.match(printBody, /--text:\s*#000/);
  assert.match(printBody, /--bg:\s*#fff/);
  assert.match(printBody, /\.toolbar[^{]*\{[^}]*display:\s*none/);
  assert.match(printBody, /\.slot-tooltip[^{]*\{[^}]*display:\s*none/);
});

test("the empty NVMe bay chip text reaches AA contrast across its face gradient", () => {
  const rule = RULES.find(
    (candidate) => candidate.selector === '.chassis-shell[data-face-style="unifi-drive"] .slot-tile.state-empty',
  );
  assert.ok(rule, "the light NVMe empty-bay rule must exist");
  const [color] = declarations(rule.body, "color");
  assert.match(color, /^#[0-9a-f]{6}$/i, `chip colour must be a six-digit hex, got ${color}`);
  for (const faceStop of ["#d8dce1", "#bcc3cb", "#aeb6bf"]) {
    const ratio = contrastRatio(color, faceStop);
    assert.ok(ratio >= 4.5, `${color} on ${faceStop} is ${ratio.toFixed(2)}:1, below 4.5:1`);
  }
});
