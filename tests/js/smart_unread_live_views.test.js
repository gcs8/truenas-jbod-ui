"use strict";

// Live SMART views (disk detail, storage-view detail, the SMART tooltip, the
// heat map and its default Attention Score) read the summary's unread_fields:
// a field the SMART JSON reported in a form that could not be read says
// "not read". It is not shown as absent ("n/a"), not replaced by an inventory
// value, and an Attention Score built on unread inputs is not a low-risk zero.

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

// Detail rows: formatter name, the summary field it shows, and how it is called.
const SMART_ONLY = (smartEntry) => [smartEntry];
const WITH_SLOT = (smartEntry) => [{
  temperature_c: 41, last_smart_test_type: "Short offline", last_smart_test_status: "Completed",
  logical_block_size: 512, physical_block_size: 4096, logical_unit_id: "5000c500aa", sas_address: "5000c500bb",
  raw_status: { attached_sas_address: "5000c500cc" }, multipath: { lunid: "5000c500dd" },
}, smartEntry];
const DETAIL_ROWS = [
  ["formatTemperatureValue", "temperature_c", WITH_SLOT],
  ["formatSmartHealthStatusValue", "smart_health_status", SMART_ONLY],
  ["formatLastSmartTestValue", "last_test_type", WITH_SLOT],
  ["formatLastSmartTestValue", "last_test_status", WITH_SLOT],
  ["formatPowerOnValue", "power_on_hours", SMART_ONLY],
  ["formatPowerCycleValue", "power_cycle_count", SMART_ONLY],
  ["formatPowerOnResetsValue", "power_on_resets", SMART_ONLY],
  ["formatSectorSizeValue", "logical_block_size", WITH_SLOT],
  ["formatSectorSizeValue", "physical_block_size", WITH_SLOT],
  ["formatRotationValue", "rotation_rate_rpm", SMART_ONLY],
  ["formatFormFactorValue", "form_factor", SMART_ONLY],
  ["formatReadCacheValue", "read_cache_enabled", SMART_ONLY],
  ["formatWritebackCacheValue", "writeback_cache_enabled", SMART_ONLY],
  ["formatTransportValue", "transport_protocol", SMART_ONLY],
  ["formatLogicalUnitIdValue", "logical_unit_id", WITH_SLOT],
  ["formatSasAddressValue", "sas_address", WITH_SLOT],
  ["formatAttachedSasAddressValue", "attached_sas_address", WITH_SLOT],
  ["formatLinkRateValue", "negotiated_link_rate", SMART_ONLY],
  ["formatNamespaceNguidValue", "namespace_nguid", SMART_ONLY],
  ["formatNamespaceEui64Value", "namespace_eui64", WITH_SLOT],
  ["formatFirmwareValue", "firmware_version", SMART_ONLY],
  ["formatProtocolVersionValue", "protocol_version", SMART_ONLY],
  ["formatWarningTemperatureValue", "warning_temperature_c", SMART_ONLY],
  ["formatCriticalTemperatureValue", "critical_temperature_c", SMART_ONLY],
  ["formatEnduranceValue", "endurance_used_percent", SMART_ONLY],
  ["formatAvailableSpareValue", "available_spare_percent", SMART_ONLY],
  ["formatBytesReadValue", "bytes_read", SMART_ONLY],
  ["formatBytesWrittenValue", "bytes_written", SMART_ONLY],
  ["formatAnnualizedReadValue", "annualized_bytes_read", SMART_ONLY],
  ["formatAnnualizedWriteValue", "annualized_bytes_written", SMART_ONLY],
  ["formatEstimatedRemainingWriteValue", "estimated_remaining_bytes_written", SMART_ONLY],
  ["formatReadCommandsValue", "read_commands", SMART_ONLY],
  ["formatWriteCommandsValue", "write_commands", SMART_ONLY],
  ["formatMediaErrorsValue", "media_errors", SMART_ONLY],
  ["formatUnsafeShutdownsValue", "unsafe_shutdowns", SMART_ONLY],
  ["formatHardwareResetsValue", "hardware_resets", SMART_ONLY],
  ["formatInterfaceCrcErrorsValue", "interface_crc_errors", SMART_ONLY],
];

const HELPERS = [
  "smartFieldNotRead", "smartNotReadOr", "formatOptionalCount", "formatBooleanFlagValue", "formatCacheFlagValue", "formatHexIdentifier",
  "formatNamespaceIdentifierValue", "formatThresholdTemperatureValue", "formatMetricBytes", "formatBytesValue",
  "computeAnnualizedBytes", "nullableMetricNumber", "formatPredictiveErrorsValue", "appendSmartTooltipMetrics",
  "heatmapSmartNumber", "heatmapMetricNumber", "countRisk", "computeAttentionScore", "computeTemperatureDelta",
  "attentionPeerWriteMedian", "buildAttentionScoreContext", "roundHeatmapValue", "attentionScoreInputState",
  "heatmapMissingText", "heatmapTooltipLines",
];
const NAMES = [...new Set([...DETAIL_ROWS.map(([name]) => name), ...HELPERS])];
const sandbox = vm.createContext({
  ANNUALIZED_MIN_POWER_ON_HOURS: 720,
  heatmapEntryOccupied: () => true,
  heatmapSlotState: (entry) => entry.slotState || "healthy",
  heatmapTemperatureAverage: () => null,
  heatmapTimelineSampleValue: () => null,
});
vm.runInContext(`${NAMES.map(functionSource).join("\n")}\nthis.loaded = { ${NAMES.join(", ")} };`, sandbox);
const app = sandbox.loaded;
// Values made in the sandbox realm compared as plain data.
const plain = (value) => JSON.parse(JSON.stringify(value));

function entry(data, unread = []) {
  return { loading: false, data: { available: true, message: null, ...data, unread_fields: unread } };
}

test("every live detail row says not read for its unread field, with no inventory fallback", () => {
  for (const [name, field, args] of DETAIL_ROWS) {
    const shown = app[name](...args(entry({ firmware_version: field === "firmware_version" ? null : "FW01" }, [field])));
    assert.match(shown, /not read/, `${name} for ${field}`);
  }
});

test("absent, real and zero values keep their existing text", () => {
  for (const [name, field, args] of DETAIL_ROWS) {
    const absent = app[name](...args(entry({ firmware_version: "FW01" })));
    assert.doesNotMatch(absent, /not read/, `${name} absent`);
    // Another field's refusal does not change this row.
    const other = app[name](...args(entry({ firmware_version: "FW01" }, [field === "media_errors" ? "bytes_read" : "media_errors"])));
    assert.equal(other, absent, `${name} with another field unread`);
  }
  assert.equal(app.formatTemperatureValue({ temperature_c: 41 }, entry({})), "41 C");
  assert.equal(app.formatTemperatureValue({}, entry({ temperature_c: 30 }, ["temperature_c"])), "30 C");
  assert.equal(app.formatInterfaceCrcErrorsValue(entry({ interface_crc_errors: 0 })), "0");
  assert.equal(app.formatSmartHealthStatusValue(entry({ smart_health_status: "PASSED" })), "PASSED");
  assert.equal(app.formatAvailableSpareValue(entry({ available_spare_percent: 100 }, ["available_spare_threshold_percent"])),
    "100% (threshold not read)");
  assert.equal(app.formatSectorSizeValue({}, entry({ physical_block_size: 4096 }, ["logical_block_size"])),
    "Logical not read / Physical 4096 B");
  assert.equal(app.formatLastSmartTestValue({}, entry({ last_test_type: "Short offline", last_test_status: "Completed" },
    ["last_test_lifetime_hours"])), "Short offline: Completed (time not read)");
  assert.equal(app.formatLastSmartTestValue({ last_smart_test_lifetime_hours: 9 },
    entry({ last_test_type: "Short offline", last_test_status: "Completed", last_test_lifetime_hours: 7 })),
    "Short offline: Completed (@ 7h)");
});

test("the SMART tooltip names unread fields", () => {
  const lines = [];
  app.appendSmartTooltipMetrics(lines, { temperature_c: 41 }, entry({ firmware_version: "FW01" },
    ["smart_health_status", "temperature_c"]));
  assert.deepEqual(lines.slice(0, 2), ["Temp: not read", "SMART: not read"]);
});

const ALL_RISK_UNREAD = [
  "endurance_remaining_percent", "endurance_used_percent", "interface_crc_errors", "media_errors",
  "smart_health_status", "temperature_c", "unsafe_shutdowns",
];
const EMPTY_CONTEXT = { annualizedWrites: [], annualizedWriteIndexByEntry: new Map(), temperatureAverage: null };

function score(data, unread = [], extra = {}) {
  const scored = { entry: { slot: { temperature_c: 55 }, smartEntry: entry(data, unread), ...extra } };
  return app.computeAttentionScore(scored.entry, [scored.entry], EMPTY_CONTEXT);
}

test("an Attention Score with no SMART risk input read is not scored", () => {
  const result = score({ firmware_version: "FW01" }, ALL_RISK_UNREAD);
  assert.equal(result.value, null);
  assert.equal(result.missingText, "not scored: SMART inputs not read");
  // Every summary field unread: the summary is unavailable, still not a 10-point score.
  const unavailable = score({ available: false, message: "SMART summary fields were returned for this disk but could not be read." },
    ALL_RISK_UNREAD);
  assert.equal(unavailable.value, null);
});

test("read fault evidence keeps its score and says inputs were not read", () => {
  const failed = score({ smart_health_status: "FAILED", interface_crc_errors: 100 }, ["temperature_c"]);
  assert.equal(failed.value, 51);
  assert.deepEqual(plain(failed.reasons), ["SMART FAILED", "100 CRC errors", "SMART inputs not read"]);
  const fault = score({ firmware_version: "FW01" }, ALL_RISK_UNREAD, { slotState: "fault" });
  assert.equal(fault.value, 30);
  assert.deepEqual(plain(fault.reasons), ["slot fault state", "SMART inputs not read"]);
  // A partly read healthy disk scores 0 but says it is incomplete.
  const partial = score({ smart_health_status: "PASSED", interface_crc_errors: 0 }, ["temperature_c"]);
  assert.deepEqual(plain([partial.value, partial.reasons]), [0, ["SMART inputs not read"]]);
  // An unread temperature is not replaced by the inventory's 55 C.
  assert.ok(!partial.reasons.some((reason) => reason.includes("55")));
});

test("a healthy, fully read disk still scores 0 with no reason", () => {
  const healthy = score({ smart_health_status: "PASSED", temperature_c: 30, interface_crc_errors: 0, media_errors: 0 });
  assert.deepEqual(plain([healthy.value, healthy.reasons]), [0, []]);
  assert.equal(healthy.missingText, undefined);
  const failed = score({ smart_health_status: "FAILED", interface_crc_errors: 100, temperature_c: 30 });
  assert.deepEqual(plain([failed.value, failed.reasons]), [51, ["SMART FAILED", "100 CRC errors"]]);
});

// The score weighs both thresholds, so each one refused on its own makes a
// healthy 0 incomplete; read fault evidence is not involved.
for (const field of ["warning_temperature_c", "critical_temperature_c"]) {
  test(`a refused ${field} qualifies a healthy zero score`, () => {
    const refused = score({ smart_health_status: "PASSED", temperature_c: 30 }, [field]);
    assert.deepEqual(plain([refused.value, refused.reasons]), [0, ["SMART inputs not read"]]);
    assert.equal(refused.missingText, undefined);
    const read = score({ smart_health_status: "PASSED", temperature_c: 30, warning_temperature_c: 60, critical_temperature_c: 70 });
    assert.deepEqual(plain([read.value, read.reasons]), [0, []]);
  });
}

test("Available Spare says not read when only its threshold was refused and no spare reading exists", () => {
  assert.equal(app.formatAvailableSpareValue(entry({ firmware_version: "FW01" }, ["available_spare_threshold_percent"])), "not read");
  // Absent spare with nothing refused keeps n/a.
  assert.equal(app.formatAvailableSpareValue(entry({ firmware_version: "FW01" })), "n/a");
  assert.equal(app.formatAvailableSpareValue(entry({ firmware_version: "FW01" }, ["media_errors"])), "n/a");
});

test("the heat map says why a bay has no value", () => {
  const temperature = { id: "temperature_c", label: "Temperature", historyMetrics: ["temperature_c"], format: String };
  const attention = { id: "attention_score", label: "Attention Score", format: String };
  const unread = { smartEntry: entry({ firmware_version: "FW01" }, ["temperature_c"]) };
  assert.equal(app.heatmapMissingText(temperature, unread, null, {}), "not read");
  assert.equal(app.heatmapMissingText(temperature, { smartEntry: entry({}) }, null, {}), null);
  assert.equal(app.heatmapMissingText(temperature, unread, null, { timelineActive: true }), null);
  assert.equal(app.heatmapMissingText(attention, unread, { value: null, missingText: "not scored: SMART inputs not read" }, {}),
    "not scored: SMART inputs not read");
  const context = (metric, record) => ({ enabled: true, metric, records: new Map([["1", record]]) });
  assert.deepEqual(plain(app.heatmapTooltipLines("1", context(attention, { value: null, missingText: "not scored: SMART inputs not read" }))),
    ["Heat Map: Attention Score not scored: SMART inputs not read"]);
  assert.deepEqual(plain(app.heatmapTooltipLines("1", context(temperature, { value: null, missingText: "not read" }))),
    ["Heat Map: Temperature not read"]);
  assert.deepEqual(plain(app.heatmapTooltipLines("1", context(temperature, { value: null }))), ["Heat Map: Temperature unknown"]);
  // An unread field gets no inventory fallback in the heat map either.
  assert.equal(app.heatmapSmartNumber({ smartEntry: unread.smartEntry }, "temperature_c", 41), null);
  assert.equal(app.heatmapSmartNumber({ smartEntry: entry({}) }, "temperature_c", 41), 41);
});

test("the heat-map badge title uses the same reason", () => {
  const body = functionSource("applyHeatmapToTile");
  assert.match(body, /badge\.title = `\$\{metric\.label\}: \$\{record\?\.missingText \|\| "unknown"\}`/);
});

test("a copyable row offers Copy for an identifier, not for text standing in for one", () => {
  const sandbox = vm.createContext({});
  vm.runInContext(`${functionSource("escapeHtml")}\n${functionSource("kvRow")}\nthis.kvRow = kvRow;`, sandbox);
  const offersCopy = (value) => sandbox.kvRow("Namespace NGUID", value, true).includes("copy-button");
  assert.equal(offersCopy("synthetic-namespace-id"), true);
  assert.equal(offersCopy("SYNTHETIC-0"), true);
  // The row still shows the text; there is just nothing to copy.
  for (const standIn of ["not read", "Loading..."]) {
    assert.equal(offersCopy(standIn), false, standIn);
    assert.ok(sandbox.kvRow("Namespace NGUID", standIn, true).includes(`<div class="kv-value">${standIn}</div>`), standIn);
  }
  assert.equal(offersCopy(""), false);
  assert.equal(sandbox.kvRow("Serial", "SYNTHETIC-0", false).includes("copy-button"), false);
});
