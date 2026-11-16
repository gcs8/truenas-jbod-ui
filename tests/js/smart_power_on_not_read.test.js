"use strict";

// Disk detail, storage-view detail and the SMART tooltip (the live page and
// the saved HTML copy share this renderer): a power-on time the SMART JSON
// reported but could not be read says "not read". It is not shown as absent
// ("n/a"), as unavailable, or as zero.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(name) {
  const start = APP_SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
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
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const notApplicable = () => "n/a";
const STUBS = Object.fromEntries([
  "formatTemperatureValue", "formatSmartHealthStatusValue", "formatEnduranceValue",
  "formatAvailableSpareValue", "formatBytesReadValue", "formatBytesWrittenValue",
  "formatAnnualizedReadValue", "formatAnnualizedWriteValue", "formatEstimatedRemainingWriteValue",
  "formatMediaErrorsValue", "formatPredictiveErrorsValue", "formatTransportValue",
  "formatProtocolVersionValue", "formatFirmwareValue", "formatLinkRateValue",
  "formatAttachedSasAddressValue",
].map((name) => [name, notApplicable]));

const NAMES = ["escapeHtml", "kvRow", "kvRowIfMeaningful", "formatPowerOnValue", "appendSmartTooltipMetrics"];
const sandbox = vm.createContext({ ...STUBS });
vm.runInContext(`${NAMES.map(functionSource).join("\n")}\nthis.loaded = { ${NAMES.join(", ")} };`, sandbox);
const { kvRow, kvRowIfMeaningful, formatPowerOnValue, appendSmartTooltipMetrics } = sandbox.loaded;

function entry(data) {
  return { loading: false, data: { available: true, message: null, power_on_hours: null, power_on_days: null, unread_fields: [], ...data } };
}

function tooltip(smartEntry) {
  const lines = [];
  appendSmartTooltipMetrics(lines, {}, smartEntry);
  return lines;
}

const UNREAD = entry({ temperature_c: 30, unread_fields: ["power_on_days", "power_on_hours"] });

test("unread power-on time says not read in the detail rows and the tooltip", () => {
  assert.equal(formatPowerOnValue(UNREAD), "not read");
  assert.match(kvRowIfMeaningful("Power On", formatPowerOnValue(UNREAD)), /<div class="kv-value">not read<\/div>/);
  assert.match(kvRow("Power On", formatPowerOnValue(UNREAD)), /<div class="kv-value">not read<\/div>/);
  assert.deepEqual(tooltip(UNREAD), ["Power On: not read"]);
  // Nothing readable at all: the field is still "not read", not "Unavailable".
  const nothingRead = entry({
    available: false,
    message: "SMART summary fields were returned for this disk but could not be read.",
    unread_fields: ["power_on_days", "power_on_hours"],
  });
  assert.equal(formatPowerOnValue(nothingRead), "not read");
});

test("absent, unavailable, zero and real readings keep their existing text", () => {
  assert.equal(formatPowerOnValue(entry({ temperature_c: 30 })), "n/a");
  assert.equal(kvRowIfMeaningful("Power On", formatPowerOnValue(entry({ temperature_c: 30 }))), "");
  assert.deepEqual(tooltip(entry({ temperature_c: 30 })), []);
  assert.equal(formatPowerOnValue(entry({ available: false, message: "No SMART summary fields were returned for this disk." })), "Unavailable");
  assert.equal(formatPowerOnValue(entry({ power_on_hours: 0, power_on_days: 0 })), "0 hr (0 d)");
  assert.equal(formatPowerOnValue(entry({ power_on_hours: 48, power_on_days: 2, unread_fields: ["power_on_hours"] })), "48 hr (2 d)");
  assert.equal(formatPowerOnValue(entry({ unread_fields: ["temperature_c"] })), "n/a");
  // A summary saved before this field existed has no list at all.
  assert.equal(formatPowerOnValue({ loading: false, data: { available: true, message: null, power_on_hours: null } }), "n/a");
  assert.equal(formatPowerOnValue({ loading: true, data: null }), "Loading...");
});
