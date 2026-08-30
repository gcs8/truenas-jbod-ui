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
