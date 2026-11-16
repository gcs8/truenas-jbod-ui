"use strict";

// Storage Fabric SMART power-on time: an absent value is omitted, a value the
// SMART JSON reported but could not be read says "not read", and neither is
// shown as a measured zero. The summaries below have the shape the SMART
// summary API serialises: every field present, unknown fields null.

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

const { formatInteger, formatSmartPowerOn, formatSmartPowerOnFact, smartSummaryLine } = new Function(
  [
    functionSource(FABRIC_SOURCE, "formatInteger"),
    functionSource(FABRIC_SOURCE, "formatSmartPowerOn"),
    functionSource(FABRIC_SOURCE, "smartPowerOnNotRead"),
    functionSource(FABRIC_SOURCE, "formatSmartPowerOnFact"),
    functionSource(FABRIC_SOURCE, "smartSummaryLine"),
    "return { formatInteger, formatSmartPowerOn, formatSmartPowerOnFact, smartSummaryLine };",
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
    unread_fields: [],
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

test("a reported but unreadable power-on time says not read", () => {
  const unread = summary({ smart_health_status: "PASSED", temperature_c: 30, unread_fields: ["power_on_days", "power_on_hours"] });
  assert.equal(formatSmartPowerOn(unread), "");
  assert.equal(formatSmartPowerOnFact(unread), "not read");
  assert.equal(smartSummaryLine(unread), "PASSED / 30 C / power on not read");
  // Absent stays omitted; zero and a reading stay readings.
  assert.equal(formatSmartPowerOnFact(summary({ temperature_c: 30 })), "");
  assert.equal(smartSummaryLine(summary({ temperature_c: 30 })), "30 C");
  assert.equal(formatSmartPowerOnFact(summary({ power_on_hours: 0, power_on_days: 0 })), "0 d");
  // A value another source filled wins over the unread marker.
  assert.equal(formatSmartPowerOnFact(summary({ power_on_hours: 48, power_on_days: 2, unread_fields: ["power_on_hours"] })), "2 d");
  // Other unread fields do not make power-on time unread.
  assert.equal(formatSmartPowerOnFact(summary({ unread_fields: ["temperature_c"] })), "");
  // A summary with nothing readable keeps its own message.
  assert.equal(
    smartSummaryLine({ available: false, message: "SMART summary fields were returned for this disk but could not be read.", unread_fields: ["power_on_hours"] }),
    "SMART summary fields were returned for this disk but could not be read.",
  );
});

test("the SMART card and the disk path facts use the power-on fact", () => {
  const card = functionSource(FABRIC_SOURCE, "renderSmartSummaryCard");
  assert.match(card, /formatSmartPowerOnFact\(summary\)/);
  assert.doesNotMatch(card, /formatSmartPowerOn\(summary\)/);
  assert.match(FABRIC_SOURCE, /\["Power on", formatSmartPowerOnFact\(smartSummary\)\]/);
});
