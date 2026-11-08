"use strict";

// #1103: heat-map temperature colours come from absolute bands (the drive's
// own SMART thresholds, else documented defaults), not from the observed
// min-max of the current view, and every tile text line stays readable on
// every heat colour.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const STYLE_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");
const TEMPLATE_SOURCE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

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

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  vm.runInContext(`${names.map(functionSource).join("\n")}\nthis.__loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.__loaded;
}

const BAND_FUNCTIONS = [
  "nullableMetricNumber",
  "heatmapTemperatureBandDefinitions",
  "heatmapTemperatureDefaultProfiles",
  "heatmapTemperatureProfileId",
  "heatmapTemperatureThresholdValue",
  "resolveHeatmapTemperatureThresholds",
  "heatmapTemperatureBand",
  "heatmapRelativeLuminance",
  "heatmapContrastRatio",
  "heatmapSurfaceRgb",
  "heatmapReadableTextRgb",
  "heatmapBadgeRgb",
  "heatmapRgbForIntensity",
  "interpolateRgb",
];

function bandApi() {
  return loadFunctions(BAND_FUNCTIONS);
}

function bandOf(api, value, data = {}, slot = {}) {
  return api.heatmapTemperatureBand(value, api.resolveHeatmapTemperatureThresholds(data, slot))?.id ?? null;
}

test("documented HDD defaults place the reported healthy readings in calm bands", () => {
  const api = bandApi();
  const thresholds = api.resolveHeatmapTemperatureThresholds({}, {});
  assert.equal(thresholds.source, "default");
  assert.equal(thresholds.profileId, "hdd");
  assert.deepEqual(
    { cool: thresholds.cool, warm: thresholds.warm, warning: thresholds.warning, critical: thresholds.critical },
    { cool: 30, warm: 45, warning: 50, critical: 60 },
  );
  // The issue: 48 C and 49 C on a 4-bay, 35 C on a 24-bay all looked red.
  assert.equal(bandOf(api, 48), "warm");
  assert.equal(bandOf(api, 49), "warm");
  assert.equal(bandOf(api, 35), "normal");
  assert.equal(bandOf(api, 36), "normal");
  assert.equal(bandOf(api, 29), "cool");
  assert.equal(bandOf(api, 30), "normal");
  assert.equal(bandOf(api, 44), "normal");
  assert.equal(bandOf(api, 45), "warm");
  assert.equal(bandOf(api, 50), "warning");
  assert.equal(bandOf(api, 59), "warning");
  assert.equal(bandOf(api, 60), "critical");
});

test("solid-state drives get the higher documented default ranges", () => {
  const api = bandApi();
  for (const [data, slot] of [
    [{ transport_protocol: "NVMe" }, {}],
    [{ rotation_rate_rpm: 0 }, {}],
    [{}, { transport_protocol: "nvme" }],
    [{}, { device_name: "nvme3n1" }],
  ]) {
    const thresholds = api.resolveHeatmapTemperatureThresholds(data, slot);
    assert.equal(thresholds.profileId, "solid_state", JSON.stringify({ data, slot }));
    assert.equal(thresholds.source, "default");
    assert.deepEqual(
      [thresholds.cool, thresholds.warm, thresholds.warning, thresholds.critical],
      [30, 60, 70, 80],
    );
  }
  assert.equal(bandOf(api, 55, { transport_protocol: "NVMe" }), "normal");
  assert.equal(bandOf(api, 65, { transport_protocol: "NVMe" }), "warm");
  // A spinning disk keeps HDD defaults even when other fields are present.
  assert.equal(api.resolveHeatmapTemperatureThresholds({ rotation_rate_rpm: 7200 }, { device_name: "da4" }).profileId, "hdd");
});

test("drive-reported warning and critical thresholds replace the defaults", () => {
  const api = bandApi();
  const data = { warning_temperature_c: 70, critical_temperature_c: 80, transport_protocol: "NVMe" };
  const thresholds = api.resolveHeatmapTemperatureThresholds(data, {});
  assert.equal(thresholds.source, "drive");
  assert.equal(thresholds.warning, 70);
  assert.equal(thresholds.critical, 80);
  assert.ok(thresholds.warm < thresholds.warning);
  assert.equal(bandOf(api, 69, data), "warm");
  assert.equal(bandOf(api, 70, data), "warning");
  assert.equal(bandOf(api, 80, data), "critical");

  const hdd = { warning_temperature_c: 55, critical_temperature_c: 65 };
  assert.equal(bandOf(api, 60, hdd), "warning", "a drive rated to 65 C is not critical at 60 C");
  assert.equal(bandOf(api, 65, hdd), "critical");
  // Numeric strings from older cached payloads are still measurements.
  assert.equal(api.resolveHeatmapTemperatureThresholds({ warning_temperature_c: "55", critical_temperature_c: "65" }, {}).source, "drive");
});

test("a single reported threshold keeps the default spacing for the other", () => {
  const api = bandApi();
  const criticalOnly = api.resolveHeatmapTemperatureThresholds({ critical_temperature_c: 65 }, {});
  assert.equal(criticalOnly.source, "drive_partial");
  assert.equal(criticalOnly.critical, 65);
  assert.equal(criticalOnly.warning, 55);
  assert.equal(criticalOnly.warm, 50);

  const warningOnly = api.resolveHeatmapTemperatureThresholds({ warning_temperature_c: 58 }, {});
  assert.equal(warningOnly.source, "drive_partial");
  assert.equal(warningOnly.warning, 58);
  assert.equal(warningOnly.critical, 68);
  assert.equal(warningOnly.warm, 53);

  // A warning above the critical value is inconsistent: keep the critical claim.
  const inverted = api.resolveHeatmapTemperatureThresholds({ warning_temperature_c: 90, critical_temperature_c: 70 }, {});
  assert.equal(inverted.critical, 70);
  assert.ok(inverted.warning < inverted.critical);
  assert.equal(inverted.source, "drive_partial");
});

test("absent, unreported or implausible thresholds fall back to defaults", () => {
  const api = bandApi();
  for (const value of [null, undefined, "", " ", 0, -5, 255, 1000, NaN, Infinity, true, false, [], {}, "hot"]) {
    const thresholds = api.resolveHeatmapTemperatureThresholds(
      { warning_temperature_c: value, critical_temperature_c: value },
      {},
    );
    assert.equal(thresholds.source, "default", `threshold ${String(value)} must be ignored`);
    assert.equal(thresholds.critical, 60);
    assert.equal(api.heatmapTemperatureThresholdValue(value), null);
  }
  assert.equal(api.resolveHeatmapTemperatureThresholds(null, null).source, "default");
});

test("bands stay ordered when drive thresholds sit below the default cool edge", () => {
  const api = bandApi();
  const thresholds = api.resolveHeatmapTemperatureThresholds({ warning_temperature_c: 28, critical_temperature_c: 32 }, {});
  assert.ok(thresholds.cool <= thresholds.warm);
  assert.ok(thresholds.warm <= thresholds.warning);
  assert.ok(thresholds.warning <= thresholds.critical);
  assert.equal(bandOf(api, 27, { warning_temperature_c: 28, critical_temperature_c: 32 }) === "critical", false);
  assert.equal(bandOf(api, 32, { warning_temperature_c: 28, critical_temperature_c: 32 }), "critical");
});

test("single values, equal values and out-of-range readings map without a view range", () => {
  const api = bandApi();
  // The band depends only on the value and its own thresholds, so a view of
  // one disk, or of identical disks, colours each reading as it would anywhere.
  assert.equal(bandOf(api, 38), "normal");
  assert.deepEqual([40, 40, 40].map((value) => bandOf(api, value)), ["normal", "normal", "normal"]);
  assert.equal(bandOf(api, -10), "cool");
  assert.equal(bandOf(api, 0), "cool");
  assert.equal(bandOf(api, 140), "critical");
  assert.equal(bandOf(api, null), null);
  assert.equal(bandOf(api, Number.NaN), null);
  // The tile shows a rounded value; the band follows the displayed number.
  assert.equal(bandOf(api, 49.4), "warm");
  assert.equal(bandOf(api, 49.6), "warning");
});

test("every band has a distinct colour from cool to critical", () => {
  const api = bandApi();
  const bands = Array.from(api.heatmapTemperatureBandDefinitions());
  assert.deepEqual(bands.map((band) => band.id), ["cool", "normal", "warm", "warning", "critical"]);
  assert.equal(new Set(bands.map((band) => Array.from(band.rgb).join(","))).size, bands.length);
  for (const band of bands) assert.ok(band.label);
});

function cssColorRgb(value) {
  const hex = /^#([0-9a-f]{6})$/i.exec(value.trim());
  if (hex) return [0, 2, 4].map((offset) => parseInt(hex[1].slice(offset, offset + 2), 16));
  const rgba = /^rgba?\(([^)]+)\)$/i.exec(value.trim());
  assert.ok(rgba, `unsupported colour ${value}`);
  return rgba[1].split(",").map((part) => Number(part.trim()));
}

function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(STYLE_SOURCE);
  assert.ok(match, `${selector} rule must exist`);
  return match[1];
}

test("every tile text line meets WCAG AA on every band and relative colour", () => {
  const api = bandApi();
  const surfaces = [];
  for (const band of api.heatmapTemperatureBandDefinitions()) {
    surfaces.push([band.id, api.heatmapSurfaceRgb(band.rgb, band.alpha)]);
  }
  for (let step = 0; step <= 20; step += 1) {
    const intensity = step / 20;
    surfaces.push([`relative ${intensity}`, api.heatmapSurfaceRgb(api.heatmapRgbForIntensity(intensity), 0.5 + (intensity * 0.25))]);
  }
  const badgeRule = cssRule(".slot-heatmap-value");
  assert.match(badgeRule, /background:\s*rgb\(var\(--heatmap-badge-rgb/, "the value badge is an opaque computed chip");
  const badgeText = cssColorRgb(/color:\s*([^;]+);/.exec(badgeRule)[1]);
  for (const [name, surface] of surfaces) {
    const text = api.heatmapReadableTextRgb(surface);
    const ratio = api.heatmapContrastRatio(text, surface);
    assert.ok(ratio >= 4.5, `${name}: tile text contrast ${ratio.toFixed(2)} must be at least 4.5`);
    const chip = api.heatmapBadgeRgb(surface);
    const badgeRatio = api.heatmapContrastRatio(badgeText, chip);
    assert.ok(badgeRatio >= 4.5, `${name}: value badge contrast ${badgeRatio.toFixed(2)} must be at least 4.5`);
  }
});

test("missing-value tiles keep readable text and badge colours", () => {
  const api = bandApi();
  const missing = Array.from(STYLE_SOURCE.matchAll(/\n\.slot-tile\.heatmap-missing \{([^}]*)\}/g))
    .find((match) => match[1].includes("--heatmap-surface-rgb"));
  assert.ok(missing, "missing tiles declare their surface colours");
  const variable = (name) => cssColorRgb(`rgb(${new RegExp(`--${name}:\\s*([^;]+);`).exec(missing[1])[1]})`);
  assert.ok(api.heatmapContrastRatio(variable("heatmap-text-rgb"), variable("heatmap-surface-rgb")) >= 4.5);
  assert.ok(api.heatmapContrastRatio([244, 247, 251], variable("heatmap-badge-rgb")) >= 4.5);
});

test("contrast helpers follow the WCAG relative luminance formula", () => {
  const api = bandApi();
  assert.equal(api.heatmapContrastRatio([0, 0, 0], [255, 255, 255]).toFixed(2), "21.00");
  assert.equal(api.heatmapContrastRatio([255, 255, 255], [255, 255, 255]), 1);
  assert.equal(api.heatmapRelativeLuminance([255, 255, 255]), 1);
  assert.deepEqual(Array.from(api.heatmapReadableTextRgb([250, 250, 250])), Array.from(api.heatmapReadableTextRgb([255, 200, 0])));
  assert.notDeepEqual(Array.from(api.heatmapReadableTextRgb([10, 10, 10])), Array.from(api.heatmapReadableTextRgb([250, 250, 250])));
});

test("heat-map text lines use the computed text colour on an opaque surface chip", () => {
  const textRule = /\n\.slot-tile:is\(\.heatmap-active, \.heatmap-missing\) :is\(\.slot-device, [^{]*\{([^}]*)\}/.exec(STYLE_SOURCE);
  assert.ok(textRule, "heat-map text line rule must exist");
  assert.doesNotMatch(textRule[1], /opacity:\s*0\.45/, "faded text lines were the reported contrast defect");
  assert.match(textRule[1], /color:\s*rgb\(var\(--heatmap-text-rgb/);
  assert.match(textRule[1], /background(?:-color)?:\s*rgb\(var\(--heatmap-surface-rgb/);
  // Custom properties are set as `--heatmap-${name}`.
  const applySource = functionSource("applyHeatmapToTile");
  for (const name of ["text-rgb", "surface-rgb", "badge-rgb"]) {
    assert.match(applySource, new RegExp(`"${name}"`));
    assert.match(functionSource("resetHeatmapTile"), new RegExp(`"${name}"`));
  }
});

test("temperature colours default to bands and keep relative min-max as an explicit option", () => {
  assert.match(TEMPLATE_SOURCE, /id="heatmap-colour-select"/);
  assert.match(TEMPLATE_SOURCE, /<option value="bands" selected>/);
  assert.match(TEMPLATE_SOURCE, /<option value="relative">/);
  assert.match(TEMPLATE_SOURCE, /id="heatmap-legend-bands"/);
  assert.match(TEMPLATE_SOURCE, /id="heatmap-legend-source"/);
  assert.match(APP_SOURCE, /temperatureScale:\s*"bands"/);
});

// Load the production context builder with the band helpers, as the
// existing heat-map suite does, so the default render path is exercised.
function contextApi(state, entries) {
  return loadFunctions([
    ...BAND_FUNCTIONS,
    "heatmapSmartNumber", "heatmapEntryOccupied", "heatmapSlotState", "heatmapMetricNumber",
    "buildAttentionScoreContext", "attentionPeerWriteMedian", "heatmapTemperatureAverage",
    "computeTemperatureDelta", "countRisk", "computeAttentionScore", "roundHeatmapValue",
    "heatmapReadWriteRatio", "formatReadWriteRatioValue", "computeAnnualizedBytes", "heatmapAnnualizedBytes",
    "heatmapAnnualizedRead", "heatmapAnnualizedWrite", "heatmapHistoryRate",
    "buildHeatmapContext", "heatmapMetricDefinitions", "heatmapMetricDefinition",
    "normalizeHeatmapMetricSelection", "heatmapIntensity", "normalizeHeatmapScaleSensitivity",
    "heatmapTemperatureBandsActive", "normalizeHeatmapTemperatureScale", "summarizeHeatmapTemperatureBands",
    "heatmapTemperatureThresholdKey",
  ], {
    HEATMAP_MIN_SCALE_SENSITIVITY: 0.5, HEATMAP_MAX_SCALE_SENSITIVITY: 2,
    HEATMAP_DEFAULT_SCALE_SENSITIVITY: 1, HEATMAP_DEFAULT_METRIC: "attention_score",
    heatmapTimelineActive: () => false,
    formatMetricBytes: (value) => `${value} B`,
    formatHistoryRateValue: (value) => `${value} B/hr`,
    state,
    currentHeatmapEntries: () => entries,
  });
}

function entry(key, temperature, data = {}) {
  return {
    key,
    slot: { state: "healthy", device_name: `sd${key}`, temperature_c: temperature },
    smartEntry: { data: { available: true, temperature_c: temperature, ...data } },
  };
}

test("the default temperature context colours 48 C and 49 C in the same band", () => {
  const entries = [entry("0", 48), entry("1", 49), entry("2", 60), entry("3", 60, { warning_temperature_c: 55, critical_temperature_c: 65 })];
  const state = { heatmap: { enabled: true, metric: "temperature_c", sensitivity: 1, temperatureScale: "bands" } };
  const api = contextApi(state, entries);
  const context = api.buildHeatmapContext();
  assert.equal(context.temperatureBands, true);
  assert.equal(context.records.get("0").band.id, "warm");
  assert.equal(context.records.get("1").band.id, "warm");
  assert.equal(context.records.get("2").band.id, "critical");
  assert.equal(context.records.get("3").band.id, "warning");
  assert.equal(context.records.get("3").thresholds.source, "drive");
  assert.equal(context.bandSummary.total, 4);
  assert.equal(context.bandSummary.distinctCount, 2);
  assert.equal(context.bandSummary.primaryCount, 3);
  assert.equal(context.bandSummary.primary.profileId, "hdd");

  state.heatmap.temperatureScale = "relative";
  const relative = api.buildHeatmapContext();
  assert.equal(relative.temperatureBands, false);
  assert.equal(relative.records.get("0").band, undefined);
  assert.equal(relative.min, 48);
  assert.equal(relative.max, 60);

  state.heatmap.temperatureScale = "unexpected";
  assert.equal(api.buildHeatmapContext().temperatureBands, true, "unknown scale values fall back to bands");
});

test("temperature vs view average uses a fixed symmetric range, not the observed spread", () => {
  const entries = [entry("0", 48), entry("1", 49)];
  const state = { heatmap: { enabled: true, metric: "temperature_delta_c", sensitivity: 1, temperatureScale: "bands" } };
  const api = contextApi(state, entries);
  const context = api.buildHeatmapContext();
  assert.equal(context.min, -10);
  assert.equal(context.max, 10);
  const low = api.heatmapIntensity(context.records.get("0"), context);
  const high = api.heatmapIntensity(context.records.get("1"), context);
  assert.ok(Math.abs(high - low) <= 0.06, `a 1 C spread must stay near neutral, got ${low} and ${high}`);
});
