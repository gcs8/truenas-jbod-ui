"use strict";

// #1201: Storage Fabric must not render an unknown SMART lifetime as "0 d".
// SmartSummaryView allows null power_on_days and power_on_hours on an
// available summary. Number(null) is 0, so a presence check that converts
// first turned "unknown" into zero days and also hid a known hours value.
// These tests run the real formatter, summary line and SMART card builder from
// app/static/sas_fabric_view.js in a Node VM and check the generated text and
// markup. They do not claim real-browser coverage.

// Storage Fabric SMART power-on time: an absent value is omitted, a value the
// SMART JSON reported but could not be read says "not read", and neither is
// shown as a measured zero. The summaries below have the shape the SMART
// summary API serialises: every field present, unknown fields null.

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

const { formatInteger, formatSmartPowerOn, formatSmartPowerOnFact, smartSummaryLine } = new Function(
  [
    functionSource("formatInteger"),
    functionSource("formatSmartPowerOn"),
    functionSource("smartPowerOnNotRead"),
    functionSource("smartFieldNotRead"),
    functionSource("formatSmartPowerOnFact"),
    functionSource("smartSummaryLine"),
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
  const card = functionSource("renderSmartSummaryCard");
  assert.match(card, /formatSmartPowerOnFact\(summary\)/);
  assert.doesNotMatch(card, /formatSmartPowerOn\(summary\)/);
  assert.match(FABRIC_SOURCE, /\["Power on", formatSmartPowerOnFact\(smartSummary\)\]/);
});

// The cases from #1201, run through the real SMART card builder in a Node VM.

const NAMES = [
  "escapeHtml",
  "formatInteger",
  "formatSmartBytes",
  "formatSmartPowerOn",
  "smartPowerOnNotRead",
  "formatSmartPowerOnFact",
  "smartFieldNotRead",
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

// A negative power-on time is not a reading. The server sends no hours or days
// and names both not read (the Python tests in test_smart_json_shapes.py check
// the QuantaStor summary and the stored-summary rebuild send exactly this).
test("a negative power-on time sent as not read shows not read and no number", () => {
  const refused = render({ power_on_days: null, power_on_hours: null, unread_fields: ["power_on_days", "power_on_hours"] });
  assert.equal(refused.power, "");
  assert.equal(refused.line, "PASSED / power on not read");
  assert.match(refused.card, /<em>Power on<\/em>not read/);
  assert.doesNotMatch(`${refused.line} ${refused.card}`, /-\d/);
  // A raw negative value is still never printed as a lifetime.
  const raw = render({ power_on_days: -1, power_on_hours: -1 });
  assert.equal(raw.power, "");
  assert.doesNotMatch(`${raw.line} ${raw.card}`, /-1 [dh]/);
  // Controls: zero and a real value stay readings.
  assert.match(render({ power_on_days: 0, power_on_hours: 0 }).card, /<em>Power on<\/em>0 d/);
  assert.equal(render({ power_on_days: 10, power_on_hours: 240 }).line, "PASSED / 10 d");
});
