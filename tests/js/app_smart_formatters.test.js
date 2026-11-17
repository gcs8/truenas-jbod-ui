"use strict";

// The main page's pure SMART value formatters live in app_smart_formatters.js
// (#1345). The module loads on its own (browser global or CommonJS), app.js
// takes the same names from it, and the live and saved pages load it first.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const MODULE_PATH = path.join(ROOT, "app/static/app_smart_formatters.js");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const BASE_TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/base.html"), "utf8");

const NAMES = [
  "ANNUALIZED_MIN_POWER_ON_HOURS",
  "smartFieldNotRead", "smartNotReadOr", "formatTemperatureValue", "formatLastSmartTestValue", "formatPowerOnValue",
  "formatPowerCycleValue", "formatPowerOnResetsValue", "formatSectorSizeValue", "formatRotationValue",
  "formatFormFactorValue", "formatBooleanFlagValue", "formatCacheFlagValue", "formatReadCacheValue",
  "formatWritebackCacheValue", "formatTrimSupportedValue", "formatHexIdentifier", "formatTransportValue",
  "formatLogicalUnitIdValue", "formatSasAddressValue",
  "formatMetricBytes", "formatOptionalCount", "formatEnduranceValue", "formatAvailableSpareValue", "formatBytesValue",
  "formatBytesWrittenValue", "formatBytesReadValue", "computeAnnualizedBytes", "formatAnnualizedReadValue",
  "formatAnnualizedWriteValue", "formatEstimatedRemainingWriteValue", "formatReadCommandsValue",
  "formatWriteCommandsValue", "formatReadErrorCountValue", "formatWriteErrorCountValue", "readErrorCountLabel",
  "writeErrorCountLabel", "formatMediaErrorsValue", "formatPredictiveErrorsValue", "formatNonMediumErrorsValue",
  "formatUnsafeShutdownsValue", "formatHardwareResetsValue", "formatInterfaceCrcErrorsValue",
  "formatSmartHealthStatusValue",
  "nullableMetricNumber",
];

function entry(data, unread = []) {
  return { loading: false, data: { available: true, message: null, ...data, unread_fields: unread } };
}

test("the module loads under CommonJS on its own and exports exactly the formatter names", () => {
  delete require.cache[MODULE_PATH];
  const api = require(MODULE_PATH);
  assert.deepEqual(Object.keys(api).sort(), [...NAMES].sort());
  assert.equal(api.ANNUALIZED_MIN_POWER_ON_HOURS, 720);
  for (const name of NAMES.slice(1)) assert.equal(typeof api[name], "function", name);
});

test("in a browser realm the module sets one global and touches nothing else", () => {
  const sandbox = vm.createContext({});
  const before = Object.keys(sandbox).sort();
  vm.runInContext(fs.readFileSync(MODULE_PATH, "utf8"), sandbox);
  assert.deepEqual(Object.keys(sandbox).sort(), [...before, "AppSmartFormatters"].sort());
  assert.deepEqual(Object.keys(sandbox.AppSmartFormatters).sort(), [...NAMES].sort());
});

test("the formatters keep their unread, absent, zero and loading text", () => {
  const api = require(MODULE_PATH);
  assert.equal(api.formatPowerOnValue(entry({ power_on_hours: 48, power_on_days: 2 })), "48 hr (2 d)");
  assert.equal(api.formatPowerOnValue(entry({ power_on_hours: null }, ["power_on_hours"])), "not read");
  assert.equal(api.formatPowerOnValue({ loading: true, data: null }), "Loading...");
  assert.equal(api.formatInterfaceCrcErrorsValue(entry({ interface_crc_errors: 0 })), "0");
  assert.equal(api.formatSmartHealthStatusValue(entry({}, ["smart_health_status"])), "not read");
  assert.equal(api.formatSmartHealthStatusValue(entry({})), "n/a");
  assert.equal(api.computeAnnualizedBytes(null, 8760), null);
  assert.equal(api.computeAnnualizedBytes(0, 8760), 0);
  assert.equal(api.nullableMetricNumber(""), null);
  assert.equal(api.nullableMetricNumber("0"), 0);
  assert.equal(api.nullableMetricNumber(undefined), null);
});

test("app.js defines none of the moved names and takes every one from the module", () => {
  for (const name of NAMES.slice(1)) {
    assert.equal(APP_SOURCE.includes(`function ${name}(`), false, `app.js must not define ${name}`);
  }
  assert.equal(/\bconst ANNUALIZED_MIN_POWER_ON_HOURS\s*=/.test(APP_SOURCE), false);
  const binding = APP_SOURCE.match(/const \{([^}]*)\} = window\.AppSmartFormatters;/);
  assert.ok(binding, "app.js binds the formatter names from window.AppSmartFormatters");
  assert.deepEqual(binding[1].split(",").map((name) => name.trim()).filter(Boolean).sort(), [...NAMES].sort());
  // The binding runs before any other statement of the page script.
  assert.ok(APP_SOURCE.indexOf("window.AppSmartFormatters") < APP_SOURCE.indexOf("const bootstrap ="));
});

test("the main page loads the module before app.js, both deferred", () => {
  const moduleTag = "<script src=\"{{ url_for('static', path='app_smart_formatters.js') }}?v={{ app_version }}\" defer></script>";
  const appTag = "<script src=\"{{ url_for('static', path='app.js') }}?v={{ app_version }}\" defer></script>";
  assert.equal(BASE_TEMPLATE.split(moduleTag).length - 1, 1);
  assert.equal(BASE_TEMPLATE.split(appTag).length - 1, 1);
  assert.ok(BASE_TEMPLATE.indexOf(moduleTag) < BASE_TEMPLATE.indexOf(appTag));
});
