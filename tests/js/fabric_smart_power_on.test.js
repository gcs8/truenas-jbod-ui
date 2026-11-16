"use strict";

// #1201: Storage Fabric must not render an unknown SMART lifetime as "0 d".
// SmartSummaryView allows null power_on_days and power_on_hours on an
// available summary. Number(null) is 0, so a presence check that converts
// first turned "unknown" into zero days and also hid a known hours value.
// These tests run the real formatter, summary line and SMART card builder from
// app/static/sas_fabric_view.js in a Node VM and check the generated text and
// markup. They do not claim real-browser coverage.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(name) {
  const start = FABRIC_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = FABRIC_SOURCE.indexOf("{", FABRIC_SOURCE.indexOf(")", start));
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < FABRIC_SOURCE.length; index += 1) {
    const character = FABRIC_SOURCE[index];
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
      if (depth === 0) return FABRIC_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const NAMES = [
  "escapeHtml",
  "formatInteger",
  "formatSmartBytes",
  "formatSmartPowerOn",
  "smartSummaryLine",
  "renderSmartSummaryCard",
];

function load(summary) {
  const context = vm.createContext({
    state: { smartRequests: {} },
    smartSummaryForSlot: () => summary,
    smartCacheKey: () => "synthetic-key",
  });
  vm.runInContext(NAMES.map(functionSource).join("\n"), context, { filename: "sas_fabric_view.js" });
  return context;
}

function render(fields) {
  const summary = { available: true, smart_health_status: "PASSED", ...fields };
  const context = load(summary);
  return {
    power: context.formatSmartPowerOn(summary),
    line: context.smartSummaryLine(summary),
    card: context.renderSmartSummaryCard(0, {}),
  };
}

test("both lifetime fields null leaves power-on time out of the summary and card", () => {
  const result = render({ power_on_days: null, power_on_hours: null });
  assert.equal(result.power, "");
  assert.equal(result.line, "PASSED");
  assert.doesNotMatch(result.card, /Power on/);
  assert.match(result.card, /<em>Health<\/em>PASSED/);
});

test("null days falls back to the known hours value", () => {
  const result = render({ power_on_days: null, power_on_hours: 48 });
  assert.equal(result.power, "48 h");
  assert.equal(result.line, "PASSED / 48 h");
  assert.match(result.card, /<em>Power on<\/em>48 h/);
  assert.doesNotMatch(result.card, /0 d/);
});

test("absent, undefined, empty and blank lifetime fields are unknown, not zero", () => {
  for (const fields of [
    {},
    { power_on_days: undefined, power_on_hours: undefined },
    { power_on_days: "", power_on_hours: "" },
    { power_on_days: "  ", power_on_hours: null },
    { power_on_days: false, power_on_hours: null },
  ]) {
    const result = render(fields);
    assert.equal(result.power, "", JSON.stringify(fields));
    assert.equal(result.line, "PASSED", JSON.stringify(fields));
    assert.doesNotMatch(result.card, /Power on/, JSON.stringify(fields));
  }
});

test("empty days still falls back to known hours", () => {
  assert.equal(render({ power_on_days: "", power_on_hours: 48 }).power, "48 h");
  assert.equal(render({ power_on_hours: 48 }).power, "48 h");
});

test("a genuine reported zero is kept", () => {
  const days = render({ power_on_days: 0, power_on_hours: null });
  assert.equal(days.power, "0 d");
  assert.equal(days.line, "PASSED / 0 d");
  assert.match(days.card, /<em>Power on<\/em>0 d/);

  const hours = render({ power_on_days: null, power_on_hours: 0 });
  assert.equal(hours.power, "0 h");
  assert.match(hours.card, /<em>Power on<\/em>0 h/);
});

test("control: reported days are still preferred over hours", () => {
  const result = render({ power_on_days: 1234, power_on_hours: 29616 });
  assert.equal(result.power, `${(1234).toLocaleString()} d`);
  assert.equal(result.line, `PASSED / ${(1234).toLocaleString()} d`);
  assert.match(result.card, /<em>Power on<\/em>/);
});
