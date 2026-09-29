"use strict";

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

function loadFunctions(names, context = {}) {
  const sandbox = vm.createContext({ ...context });
  const source = names.map(functionSource).join("\n");
  vm.runInContext(`${source}\nthis.__loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.__loaded;
}

test("timeline rate lookup uses logarithmic reads of prepared samples", () => {
  const { nearestPreparedTimelineSampleIndex } = loadFunctions(["nearestPreparedTimelineSampleIndex"]);
  const source = Array.from({ length: 65_536 }, (_, index) => ({ timestampMs: index * 10, value: index }));
  let reads = 0;
  const samples = new Proxy(source, {
    get(target, property, receiver) {
      if (typeof property === "string" && /^\d+$/.test(property)) reads += 1;
      return Reflect.get(target, property, receiver);
    },
  });

  assert.equal(nearestPreparedTimelineSampleIndex(samples, 456_784), 45_678);
  assert.ok(reads <= 40, `binary lookup should not read ${reads} samples`);
  assert.equal(nearestPreparedTimelineSampleIndex(samples, -1), 0);

  const rateSource = functionSource("heatmapTimelineRateAt");
  assert.match(rateSource, /nearestPreparedTimelineSampleIndex\(/);
  assert.doesNotMatch(rateSource, /\.forEach\(/);
});

test("history heatmaps always request metric-only bounded playback", () => {
  const source = functionSource("heatmapHistoryScopeRequest");
  assert.match(source, /params\.set\("event_limit", "0"\)/);
  assert.match(source, /params\.set\("metric_limit", "24"\)/);
  assert.match(source, /params\.set\("window_hours", String\(boundedWindowHours\)\)/);
  assert.match(source, /8760/);
  assert.doesNotMatch(source, /metrics.*temperature_c.*bytes_read.*bytes_written/s);
});

test("attention score context provides constant-time view comparisons", () => {
  const entries = [
    { id: "a", temperature: 10, write: 10 },
    { id: "b", temperature: 20, write: 20 },
    { id: "c", temperature: 30, write: 30 },
    { id: "d", temperature: 40, write: 40 },
    { id: "missing", temperature: null, write: null },
  ];
  const heatmapSmartNumber = (entry, field) => field === "temperature_c" ? entry.temperature : entry.write;
  const { buildAttentionScoreContext, attentionPeerWriteMedian } = loadFunctions(
    ["buildAttentionScoreContext", "attentionPeerWriteMedian"],
    { heatmapSmartNumber, Map, Number },
  );

  const context = buildAttentionScoreContext(entries);
  assert.equal(context.temperatureAverage, 25);
  assert.equal(attentionPeerWriteMedian(entries[0], context), 30);
  assert.equal(attentionPeerWriteMedian(entries[2], context), 20);
  assert.equal(attentionPeerWriteMedian(entries[4], context), 30);

  const throwingEntries = new Proxy([], {
    get() { throw new Error("attention scoring rescanned the full entry list"); },
  });
  const { computeTemperatureDelta } = loadFunctions(["computeTemperatureDelta"], {
    heatmapMetricNumber: (entry) => entry.temperature,
    Number,
  });
  assert.equal(computeTemperatureDelta(entries[0], throwingEntries, context), -15);

  const buildSource = functionSource("buildHeatmapContext");
  assert.match(buildSource, /buildAttentionScoreContext\(entries\)/);
  assert.ok(
    buildSource.indexOf("buildAttentionScoreContext(entries)") < buildSource.indexOf("entries.forEach"),
    "attention context must be built once before per-entry scoring",
  );
});

// Load production functions, not stand-ins for nullable SMART conversion.
function loadMetricFunctions(context = {}) {
  const names = [
    "heatmapSmartNumber", "heatmapEntryOccupied", "heatmapSlotState",
    "heatmapMetricNumber", "buildAttentionScoreContext", "attentionPeerWriteMedian",
    "heatmapTemperatureAverage", "computeTemperatureDelta", "countRisk",
    "computeAttentionScore", "roundHeatmapValue", "heatmapReadWriteRatio",
    "formatReadWriteRatioValue", "computeAnnualizedBytes", "heatmapAnnualizedBytes",
    "heatmapPreparedTimelineSamples", "heatmapTimelineMetricSamples", "sampleTimestampMs",
    "formatHistoryMetricValue", "formatHistoryRateValue", "formatMetricBytes",
    "sortHistorySamplesAscending", "filterHistorySamplesToWindow", "historyWindowCutoffMs",
    "historyMetricCard", "formatHistoryWindowDelta", "formatHistoryAverage",
    "summarizeHistoryCounterChanges", "computeHistoryCounterAverage",
    "isHistoryCounterScaleDiscontinuity", "buildHistoryChartScale", "buildHistoryRateSamples",
    "buildHeatmapContext", "heatmapMetricDefinitions", "heatmapMetricDefinition",
    "normalizeHeatmapMetricSelection", "applyHeatmapToTile", "resetHeatmapTile",
    "heatmapIntensity", "normalizeHeatmapScaleSensitivity", "heatmapRgbForIntensity", "interpolateRgb",
  ];
  // Baseline replay must reach the actual defect, not fail on a missing new helper.
  if (APP_SOURCE.includes("function nullableMetricNumber(")) names.push("nullableMetricNumber");
  return loadFunctions(names, {
    ANNUALIZED_MIN_POWER_ON_HOURS: 720,
    HEATMAP_MIN_SCALE_SENSITIVITY: 0.5, HEATMAP_MAX_SCALE_SENSITIVITY: 2,
    HEATMAP_DEFAULT_SCALE_SENSITIVITY: 1, HEATMAP_DEFAULT_METRIC: "attention_score",
    heatmapSampleCache: new WeakMap(),
    heatmapTimelineActive: () => false,
    escapeHtml: (value) => String(value),
    ...context,
  });
}

function metricEntry(data = {}, key = "0") {
  return { key, slot: { state: "healthy", device_name: "sda", temperature_c: null }, smartEntry: { data: { available: true, ...data } } };
}

for (const missing of [null, undefined, "", " \t "]) {
  test(`missing SMART fields stay unknown: ${JSON.stringify(missing)}`, () => {
    const api = loadMetricFunctions();
    const entry = metricEntry({ temperature_c: missing, bytes_read: missing, power_on_hours: 8760 });
    assert.equal(api.heatmapSmartNumber(entry, "temperature_c", missing), null);
    assert.equal(api.heatmapSmartNumber(entry, "media_errors"), null);
    assert.equal(api.heatmapSmartNumber(entry, "temperature_c", 32), 32);
    assert.equal(api.heatmapReadWriteRatio(entry), null);
    assert.equal(api.heatmapAnnualizedBytes(entry, "bytes_read"), null);
    assert.equal(api.computeAnnualizedBytes(missing, 8760), null);
    assert.equal(api.formatReadWriteRatioValue(missing), "n/a");
    assert.equal(api.roundHeatmapValue(missing), "n/a");
  });

  test(`absent thresholds and endurance do not add risk: ${JSON.stringify(missing)}`, () => {
    const api = loadMetricFunctions();
    const entry = metricEntry({ temperature_c: 30, warning_temperature_c: missing, critical_temperature_c: missing,
      endurance_used_percent: missing, endurance_remaining_percent: missing });
    const score = api.computeAttentionScore(entry, [entry]);
    assert.equal(score.value, 0);
    assert.deepEqual(Array.from(score.reasons), []);
  });

  test(`absent generic error counters use uncorrected counts: ${JSON.stringify(missing)}`, () => {
    const api = loadMetricFunctions();
    const entry = metricEntry({ read_error_count: missing, write_error_count: missing,
      uncorrected_read_errors: 12, uncorrected_write_errors: 4 });
    const score = api.computeAttentionScore(entry, [entry]);
    assert.equal(score.value, 26);
    assert.deepEqual(Array.from(score.reasons), ["12 read errors", "4 write errors"]);
  });

  test(`history does not invent measurements for ${JSON.stringify(missing)}`, () => {
    const api = loadMetricFunctions();
    const samples = [{ observed_at: "2026-01-01T00:00:00Z", value: missing }];
    for (const metric of ["temperature_c", "power_on_hours", "bytes_read", "annualized_bytes_written"]) {
      assert.equal(api.formatHistoryMetricValue(metric, missing), "n/a");
      const card = api.historyMetricCard(metric, metric, { metrics: { [metric]: samples } }, null);
      assert.match(card, /history-metric-value">n\/a</);
      assert.match(card, /No stored samples yet/);
    }
    assert.equal(api.formatHistoryRateValue(missing), "n/a");
    assert.equal(api.sortHistorySamplesAscending(samples).length, 0);
    assert.equal(api.buildHistoryChartScale([samples]), null);
    const entry = { historyPayload: { metrics: { temperature_c: samples } } };
    assert.equal(api.heatmapPreparedTimelineSamples(entry, "temperature_c").length, 0);
  });
}

test("invalid scalar types cannot become measured zero or one", () => {
  const api = loadMetricFunctions();
  for (const value of [false, true, [], [0], {}, NaN, Infinity, "NaN", "Infinity", "not measured"]) {
    assert.equal(api.heatmapSmartNumber(metricEntry({ temperature_c: value }), "temperature_c"), null);
    assert.equal(api.formatHistoryMetricValue("temperature_c", value), "n/a");
  }
});

test("known zero and numeric strings preserve metric and counter precedence", () => {
  const api = loadMetricFunctions();
  for (const zero of [0, "0"]) {
    const entry = metricEntry({ temperature_c: zero, bytes_read: zero, bytes_written: zero,
      read_error_count: zero, write_error_count: zero, uncorrected_read_errors: 12, uncorrected_write_errors: 4 });
    assert.equal(api.heatmapSmartNumber(entry, "temperature_c", 32), 0);
    assert.equal(api.heatmapReadWriteRatio(entry), 0);
    assert.equal(api.computeAttentionScore(entry, [entry]).value, 0);
    assert.equal(api.formatHistoryMetricValue("temperature_c", zero), "0 C");
    assert.equal(api.formatHistoryMetricValue("power_on_hours", zero), "0 hr (0 d)");
    assert.equal(api.formatHistoryMetricValue("bytes_read", zero), "0 B");
    assert.equal(api.formatHistoryMetricValue("annualized_bytes_written", zero), "0 B/yr");
    assert.equal(api.formatHistoryRateValue(zero), "0 B/hr");
    assert.equal(api.computeAnnualizedBytes(zero, 8760), 0);
  }
  assert.equal(api.heatmapSmartNumber(metricEntry({ temperature_c: " 32 " }), "temperature_c"), 32);
});

test("reported thresholds, endurance and generic counters keep their risk weights", () => {
  const api = loadMetricFunctions();
  const cases = [
    [{ temperature_c: 30, warning_temperature_c: 30 }, 24, "at warning temp 30 C"],
    [{ temperature_c: 30, critical_temperature_c: 30 }, 40, "at critical temp 30 C"],
    [{ temperature_c: 0, critical_temperature_c: 0 }, 40, "at critical temp 0 C"],
    [{ endurance_used_percent: 90 }, 35, "90% endurance used"],
    [{ endurance_remaining_percent: 0 }, 35, "0% endurance remaining"],
    [{ read_error_count: 3, uncorrected_read_errors: 99 }, 8, "3 read errors"],
    [{ write_error_count: 10, uncorrected_write_errors: 99 }, 18, "10 write errors"],
  ];
  for (const [data, value, reason] of cases) {
    const entry = metricEntry(data);
    const score = api.computeAttentionScore(entry, [entry]);
    assert.equal(score.value, value);
    assert.deepEqual(Array.from(score.reasons), [reason]);
  }
});

test("missing temperatures stay out of view averages and measured counts", () => {
  const missing = metricEntry({ temperature_c: null });
  const zero = metricEntry({ temperature_c: 0 }, "1");
  const warm = metricEntry({ temperature_c: 30 }, "2");
  let entries = [missing];
  const api = loadMetricFunctions({
    state: { heatmap: { enabled: true, metric: "temperature_c", sensitivity: 1 } },
    currentHeatmapEntries: () => entries,
  });
  let context = api.buildHeatmapContext();
  assert.equal(context.records.get("0").value, null);
  assert.equal(context.valueCount, 0);
  assert.equal(api.heatmapTemperatureAverage(entries), null);
  entries = [missing, zero, warm];
  context = api.buildHeatmapContext();
  assert.equal(context.records.get("1").value, 0);
  assert.equal(context.valueCount, 2);
  assert.equal(api.heatmapTemperatureAverage(entries), 15);
  assert.equal(api.buildAttentionScoreContext(entries).temperatureAverage, 15);
  assert.equal(api.computeTemperatureDelta(missing, entries), null);
  assert.equal(api.computeTemperatureDelta(zero, entries), -15);
});

test("empty history cards display n/a and zero samples remain measured", () => {
  const api = loadMetricFunctions();
  for (const metric of ["temperature_c", "power_on_hours"]) {
    assert.match(api.historyMetricCard(metric, metric, { metrics: {} }, null), /history-metric-value">n\/a</);
    const samples = [{ observed_at: "2026-01-01T00:00:00Z", value: 0 }];
    assert.match(api.historyMetricCard(metric, metric, { metrics: { [metric]: samples } }, null), /1 stored sample/);
    assert.equal(api.sortHistorySamplesAscending(samples).length, 1);
    assert.equal(api.buildHistoryChartScale([samples]).minValue, 0);
    assert.equal(api.heatmapPreparedTimelineSamples({ historyPayload: { metrics: { [metric]: samples } } }, metric)[0].value, 0);
  }
});

test("unknown temperature renders a missing badge while zero renders an active badge", () => {
  const entries = [metricEntry({ temperature_c: null }), metricEntry({ temperature_c: 0 }, "1")];
  const api = loadMetricFunctions({
    state: { heatmap: { enabled: true, metric: "temperature_c", sensitivity: 1 } },
    currentHeatmapEntries: () => entries,
    document: { createElement: () => ({}) },
  });
  for (const entry of entries) {
    const classes = new Set();
    const tile = {
      children: [],
      classList: { contains: (name) => classes.has(name), add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)) },
      style: { removeProperty() {}, setProperty() {} },
      appendChild(child) { this.children.push(child); },
    };
    api.applyHeatmapToTile(tile, api.buildHeatmapContext(), entry.key);
    assert.equal(classes.has("heatmap-missing"), entry.key === "0");
    assert.equal(classes.has("heatmap-active"), entry.key === "1");
    assert.equal(tile.children[0].textContent, entry.key === "0" ? "--" : "0 C");
    assert.equal(tile.children[0].title, entry.key === "0" ? "Temperature: unknown" : "Temperature: 0 C");
  }
});

test("missing counter history cannot synthesize a starting zero", () => {
  const api = loadMetricFunctions();
  const samples = [
    { observed_at: "2026-01-01T00:00:00Z", value: null },
    { observed_at: "2026-01-01T01:00:00Z", value: 20 },
  ];
  assert.equal(api.summarizeHistoryCounterChanges("bytes_read", samples), null);
  assert.equal(api.buildHistoryRateSamples("bytes_read", samples).length, 0);
  samples[0].value = 0;
  assert.equal(api.summarizeHistoryCounterChanges("bytes_read", samples).totalDelta, 20);
  assert.equal(api.buildHistoryRateSamples("bytes_read", samples)[0].value, 20);
});
