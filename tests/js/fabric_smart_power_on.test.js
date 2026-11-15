"use strict";

// Storage Fabric SMART power-on time: an unknown value is omitted, never shown
// as a measured zero. The summaries below have the shape the SMART summary
// API serialises: every field present, unknown fields null.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const ROOT = path.resolve(__dirname, "../..");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const bodyStart = source.indexOf("{", source.indexOf(")", start));
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
    if (character === "{") depth += 1;
    else if (character === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const { formatInteger, formatSmartPowerOn, smartSummaryLine } = new Function(
  [
    functionSource(FABRIC_SOURCE, "formatInteger"),
    functionSource(FABRIC_SOURCE, "formatSmartPowerOn"),
    functionSource(FABRIC_SOURCE, "smartSummaryLine"),
    "return { formatInteger, formatSmartPowerOn, smartSummaryLine };",
  ].join("\n"),
)();

function summary(fields) {
  return {
    available: true,
    message: null,
    smart_health_status: null,
    temperature_c: null,
    power_on_hours: null,
    power_on_days: null,
    ...fields,
  };
}

test("unknown power-on time is omitted, not shown as 0 d", () => {
  // Parser output for a rejected (boolean) or missing hours field.
  const unknown = summary({ temperature_c: 30 });
  assert.equal(formatSmartPowerOn(unknown), "");
  assert.equal(smartSummaryLine(unknown), "30 C");
  // Hours without days: the null days field must not win as a zero.
  assert.equal(formatSmartPowerOn(summary({ power_on_hours: 5 })), "5 h");
});

test("non-number power-on values are not readings", () => {
  for (const value of [null, undefined, "", true, false, [], "12", Number.NaN, -1, 1.5]) {
    const line = smartSummaryLine(summary({ temperature_c: 30, power_on_hours: value, power_on_days: value }));
    assert.equal(line, "30 C", `value ${String(value)}`);
  }
});

test("a measured zero and real hours stay visible", () => {
  assert.equal(formatSmartPowerOn(summary({ power_on_hours: 0, power_on_days: 0 })), "0 d");
  assert.equal(smartSummaryLine(summary({ temperature_c: 30, power_on_hours: 48, power_on_days: 2 })), "30 C / 2 d");
  assert.equal(smartSummaryLine({ available: false, message: "SMART JSON parsing failed." }), "SMART JSON parsing failed.");
});

test("formatInteger does not turn null or booleans into 0", () => {
  for (const value of [null, undefined, "", true, false]) {
    assert.equal(formatInteger(value), "", `value ${String(value)}`);
  }
  assert.equal(formatInteger(0), "0");
});
