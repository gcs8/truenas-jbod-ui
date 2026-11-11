"use strict";

// #1161: main-page SMART summary reads (single bay, saved view, batch) carry
// the same native deadline as Storage Fabric. A read that misses it settles as
// unavailable with a Retry button and releases its bay, instead of keeping
// ownership with an endless Loading. qa/smart-browser-ownership.spec.js proves
// the browser behavior; this file pins the shared deadline and the settle rules.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const FABRIC_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/sas_fabric_view.js"), "utf8");

function functionSource(name) {
  const match = APP_SOURCE.match(new RegExp(`^  (?:async )?function ${name}\\([\\s\\S]*?^  }`, "m"));
  assert.ok(match, `function ${name} must exist`);
  return match[0];
}

function deadline(source) {
  const match = source.match(/const SMART_SUMMARY_REQUEST_TIMEOUT_MS = (\d+);/);
  assert.ok(match, "SMART_SUMMARY_REQUEST_TIMEOUT_MS must be declared");
  return Number(match[1]);
}

function timeoutError() {
  return new DOMException("signal timed out", "TimeoutError");
}

function load(names, bindings = {}) {
  const context = vm.createContext({
    AbortSignal, DOMException, Math, Number, Boolean, String, Date,
    SMART_SUMMARY_REQUEST_TIMEOUT_MS: deadline(APP_SOURCE),
    SMART_SUMMARY_CACHE_TTL_MS: 300000,
    state: { snapshotMode: false, smartSummaries: {}, smartSummaryGeneration: 1, hoveredSlot: null, selectedSlot: null, heatmap: { enabled: false } },
    refreshHoveredTooltip: () => {},
    renderDetail: () => {},
    refreshHeatmapTileOverlays: () => {},
    getSmartCacheKey: (slot) => `system|enc|${slot.slot}`,
    ...bindings,
  });
  const helpers = ["smartSummaryRequestSignal", "isSmartSummaryTimeout", "smartSummaryFailureMessage", "smartSummaryAgeMs", "isSmartEntryCurrent", "isSmartEntryInFlight"];
  vm.runInContext([...new Set([...helpers, ...names])].map(functionSource).join("\n"), context, { filename: "app.js" });
  return context;
}

test("main page and Storage Fabric share one SMART summary deadline", () => {
  assert.equal(deadline(APP_SOURCE), deadline(FABRIC_SOURCE));
  assert.ok(deadline(APP_SOURCE) > 0);
  assert.match(FABRIC_SOURCE, /signal: AbortSignal\.timeout\(SMART_SUMMARY_REQUEST_TIMEOUT_MS\)/);
  assert.doesNotMatch(FABRIC_SOURCE, /AbortSignal\.timeout\(\d/);
});

test("every main-page SMART summary read passes a fresh deadline signal", () => {
  for (const name of ["ensureSmartSummary", "ensureStorageViewSmartSummary", "requestSmartBatchForSlots"]) {
    assert.match(functionSource(name), /signal: smartSummaryRequestSignal\(\)/, name);
  }
  const context = load([]);
  const first = context.smartSummaryRequestSignal();
  const second = context.smartSummaryRequestSignal();
  assert.ok(first instanceof AbortSignal);
  assert.notEqual(first, second);
  assert.equal(first.aborted, false);
});

test("a single-bay read that misses its deadline settles unavailable and releases the bay", async () => {
  const requests = [];
  const context = load(["ensureSmartSummary"], {
    sendScopedRequest: async (url, options) => { requests.push({ url, options }); throw timeoutError(); },
  });
  const slot = { slot: 3 };
  await context.ensureSmartSummary(slot);
  assert.equal(requests.length, 1);
  assert.ok(requests[0].options.signal instanceof AbortSignal);
  const entry = context.state.smartSummaries["system|enc|3"];
  assert.equal(entry.loading, false);
  assert.equal(entry.refreshing, false);
  assert.equal(entry.timedOut, true);
  assert.equal(entry.data.available, false);
  assert.match(entry.data.message, /did not answer within 15 s/);
  assert.equal(context.isSmartEntryInFlight(entry), false);
  // Ordinary renders do not hammer a hung disk: the settled timeout follows
  // the normal SMART cache interval until Retry or expiry.
  await context.ensureSmartSummary(slot);
  assert.equal(requests.length, 1);
});

test("a timed-out refresh keeps the last reading; other failures are not marked timed out", async () => {
  let failure = timeoutError();
  const context = load(["ensureSmartSummary"], {
    sendScopedRequest: async () => { throw failure; },
  });
  const key = "system|enc|4";
  context.state.smartSummaries[key] = { loading: false, refreshing: false, data: { available: true, temperature_c: 40 }, requestedAt: 1 };
  await context.ensureSmartSummary({ slot: 4 });
  assert.equal(context.state.smartSummaries[key].data.temperature_c, 40);
  assert.equal(context.state.smartSummaries[key].timedOut, true);

  failure = new Error("Request failed with 500");
  context.state.smartSummaries[key].requestedAt = 1;
  await context.ensureSmartSummary({ slot: 4 });
  assert.equal(context.state.smartSummaries[key].timedOut, undefined);
});

test("a batch that misses its deadline settles its owned bays as timed out", () => {
  const context = load(["applySmartPrefetchPayload", "applySmartPrefetchError"]);
  const owned = { loading: true, refreshing: false, data: null, generation: 1 };
  const replaced = { loading: true, refreshing: false, data: null, generation: 1 };
  context.state.smartSummaries = { a: owned, b: { loading: true } };
  context.applySmartPrefetchError([{ slot: 0, cacheKey: "a", owner: owned }, { slot: 1, cacheKey: "b", owner: replaced }], timeoutError());
  assert.equal(context.state.smartSummaries.a.timedOut, true);
  assert.equal(context.state.smartSummaries.a.loading, false);
  assert.match(context.state.smartSummaries.a.data.message, /did not answer within 15 s/);
  // A newer owner is never overwritten.
  assert.deepEqual(context.state.smartSummaries.b, { loading: true });
});

test("Retry re-requests only a settled timeout, never an in-flight owner", () => {
  const calls = [];
  const context = load(["retryDetailSmartSummary"]);
  const settled = { loading: false, refreshing: false, timedOut: true, requestedAt: Date.now(), data: { available: false } };
  context.state.detailSmartRetry = { entry: settled, ensureSmart: () => calls.push("ensure") };
  context.retryDetailSmartSummary();
  assert.deepEqual(calls, ["ensure"]);
  assert.equal(settled.requestedAt, 0);
  assert.equal(context.isSmartEntryCurrent(settled), false);
  assert.equal(context.state.detailSmartRetry, null);

  const inFlight = { loading: true, refreshing: false, timedOut: true, requestedAt: 5 };
  context.state.detailSmartRetry = { entry: inFlight, ensureSmart: () => calls.push("again") };
  context.retryDetailSmartSummary();
  assert.deepEqual(calls, ["ensure"]);
  assert.equal(inFlight.requestedAt, 5);
});
