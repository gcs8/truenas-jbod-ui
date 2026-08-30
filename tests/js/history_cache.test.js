"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(source, name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = source.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = source.indexOf(")", start);
  const bodyStart = source.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
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
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names, context) {
  const sandbox = vm.createContext({ ...context });
  const source = names.map((name) => functionSource(APP_SOURCE, name)).join("\n");
  vm.runInContext(`${source}\nthis.__loaded = { ${names.join(", ")} };`, sandbox, {
    filename: "history-cache.behavior.js",
  });
  return sandbox.__loaded;
}

function makeState({ snapshotMode = false } = {}) {
  return {
    snapshotMode,
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-a",
    snapshot: {
      selected_system_id: "system-a",
      selected_enclosure_id: "enclosure-a",
    },
    history: {
      generation: 3,
      slotCache: {},
      inFlight: {},
    },
    heatmap: {
      histories: { 1: { metrics: {} } },
      scopeKey: "old-scope",
      pendingScopeKey: null,
      error: null,
      playbackIndex: null,
      timelineCacheKey: "old-timeline",
      timelineCache: [1],
      requestToken: 0,
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("history cache key includes scope, kind, window, and generation", () => {
  const state = makeState();
  let windowHours = 24;
  const { getHistoryCacheKey } = loadFunctions(["getHistoryCacheKey"], {
    state,
    currentHistoryWindowHours: () => windowHours,
  });
  const slot = { slot: 7, enclosure_id: "enclosure-a", serial: "SERIAL-A", device_name: "da7" };
  const baseline = getHistoryCacheKey(slot);

  assert.equal(baseline, getHistoryCacheKey(slot));
  state.snapshot.selected_system_id = "system-b";
  assert.notEqual(getHistoryCacheKey(slot), baseline);
  state.snapshot.selected_system_id = "system-a";
  assert.notEqual(getHistoryCacheKey({ ...slot, enclosure_id: "enclosure-b" }), baseline);
  assert.notEqual(getHistoryCacheKey({ ...slot, slot: 8 }), baseline);
  assert.notEqual(getHistoryCacheKey({ ...slot, serial: "SERIAL-B" }), baseline);
  assert.notEqual(getHistoryCacheKey(slot, { kind: "metrics-only" }), baseline);
  windowHours = 48;
  assert.notEqual(getHistoryCacheKey(slot), baseline);
  windowHours = 24;
  state.history.generation += 1;
  assert.notEqual(getHistoryCacheKey(slot), baseline);
});

test("live history cache expires into a stale revalidation candidate", () => {
  const state = makeState();
  let now = 10_000;
  const helpers = loadFunctions(["storeLiveHistoryPayload", "getLiveHistoryCacheEntry"], {
    state,
    Date: { now: () => now },
    HISTORY_PAYLOAD_CACHE_TTL_MS: 1_000,
    HISTORY_PAYLOAD_CACHE_MAX_ENTRIES: 64,
  });

  helpers.storeLiveHistoryPayload("key", { marker: "cached" });
  now = 10_999;
  assert.equal(helpers.getLiveHistoryCacheEntry("key").freshness, "fresh");
  now = 11_001;
  const stale = helpers.getLiveHistoryCacheEntry("key");
  assert.equal(stale.freshness, "stale");
  assert.equal(stale.payload.marker, "cached");
});

test("live history cache evicts the least recently used entry at 64", () => {
  const state = makeState();
  let now = 1;
  const helpers = loadFunctions(["storeLiveHistoryPayload", "getLiveHistoryCacheEntry"], {
    state,
    Date: { now: () => now++ },
    HISTORY_PAYLOAD_CACHE_TTL_MS: 10_000,
    HISTORY_PAYLOAD_CACHE_MAX_ENTRIES: 64,
  });

  for (let index = 0; index < 64; index += 1) {
    helpers.storeLiveHistoryPayload(`key-${index}`, { index });
  }
  helpers.getLiveHistoryCacheEntry("key-0");
  helpers.storeLiveHistoryPayload("key-64", { index: 64 });

  assert.ok(state.history.slotCache["key-0"]);
  assert.equal(state.history.slotCache["key-1"], undefined);
  assert.ok(state.history.slotCache["key-64"]);
  assert.equal(Object.keys(state.history.slotCache).length, 64);
});

test("offline preloaded history remains generation-free and is never expired or evicted", () => {
  const state = makeState({ snapshotMode: true });
  state.history.slotCache["system-a|enclosure-a|7"] = { marker: "offline" };
  const helpers = loadFunctions(["getHistoryCacheKey", "getCachedHistoryPayload"], {
    state,
    currentHistoryWindowHours: () => 24,
    getLiveHistoryCacheEntry() { throw new Error("offline cache must not use live eviction"); },
  });
  const slot = { slot: 7, enclosure_id: "enclosure-a" };

  assert.equal(helpers.getHistoryCacheKey(slot, { includeWindow: false }), "system-a|enclosure-a|7");
  assert.deepEqual(
    helpers.getCachedHistoryPayload({ slot, cacheKey: helpers.getHistoryCacheKey(slot) }),
    { marker: "offline" },
  );
});

test("hard invalidation advances generation and clears all live history consumers", () => {
  const state = makeState();
  state.history.slotCache = { key: { payload: { marker: "old" } } };
  state.history.inFlight = { key: Promise.resolve() };
  state.history.statusGenerationMarker = "established-marker";
  let heatmapResets = 0;
  const { invalidateHistoryCaches } = loadFunctions(["invalidateHistoryCaches"], {
    state,
    resetHeatmapHistoryCache() { heatmapResets += 1; },
  });

  invalidateHistoryCaches();

  assert.equal(state.history.generation, 4);
  assert.equal(Object.keys(state.history.slotCache).length, 0);
  assert.equal(Object.keys(state.history.inFlight).length, 0);
  assert.equal(state.history.statusGenerationMarker, "established-marker");
  assert.equal(heatmapResets, 1);
});

test("concurrent history fetches for one key and generation share one request", async () => {
  const state = makeState();
  const request = deferred();
  let fetchCount = 0;
  const { fetchLiveHistoryPayload } = loadFunctions(["historyPayloadUnavailableDetail", "fetchLiveHistoryPayload"], {
    state,
    fetchJson() {
      fetchCount += 1;
      return request.promise;
    },
    storeLiveHistoryPayload(cacheKey, payload) {
      state.history.slotCache[cacheKey] = { payload, generation: state.history.generation };
    },
  });
  const target = { cacheKey: "slot|generation-3", fetchUrl: "/history/7" };

  const first = fetchLiveHistoryPayload(target);
  const second = fetchLiveHistoryPayload(target);
  assert.equal(fetchCount, 1);
  request.resolve({ marker: "shared" });
  const [firstPayload, secondPayload] = await Promise.all([first, second]);

  assert.equal(firstPayload.marker, "shared");
  assert.equal(secondPayload.marker, "shared");
  assert.equal(state.history.slotCache[target.cacheKey].payload.marker, "shared");
  assert.equal(Object.keys(state.history.inFlight).length, 0);
});

test("HTTP-200 unavailable slot history is rejected without replacing retained data", async () => {
  const state = makeState();
  let stored = false;
  const { fetchLiveHistoryPayload } = loadFunctions(
    ["fetchLiveHistoryPayload"],
    {
      state,
      async fetchJson() {
        return { available: false, detail: "history backend busy", metrics: {}, events: [] };
      },
      historyPayloadUnavailableDetail(payload) {
        return payload.available === false ? payload.detail : null;
      },
      storeLiveHistoryPayload() { stored = true; },
    },
  );

  await assert.rejects(
    fetchLiveHistoryPayload({ cacheKey: "slot|generation-3", fetchUrl: "/history/7" }),
    /history backend busy/,
  );
  assert.equal(stored, false);
});

test("a new generation fetch does not wait for or store an obsolete response", async () => {
  const state = makeState();
  const requests = [deferred(), deferred()];
  let fetchCount = 0;
  const helpers = loadFunctions(["historyPayloadUnavailableDetail", "fetchLiveHistoryPayload", "invalidateHistoryCaches"], {
    state,
    fetchJson() {
      const request = requests[fetchCount];
      fetchCount += 1;
      return request.promise;
    },
    storeLiveHistoryPayload(cacheKey, payload) {
      state.history.slotCache[cacheKey] = { payload, generation: state.history.generation };
    },
    resetHeatmapHistoryCache() {},
  });

  const oldLoad = helpers.fetchLiveHistoryPayload({
    cacheKey: "slot|generation-3",
    fetchUrl: "/history/7",
  });
  helpers.invalidateHistoryCaches();
  const newLoad = helpers.fetchLiveHistoryPayload({
    cacheKey: "slot|generation-4",
    fetchUrl: "/history/7",
  });
  assert.equal(fetchCount, 2);

  requests[1].resolve({ marker: "new" });
  await newLoad;
  requests[0].resolve({ marker: "old" });
  await oldLoad;

  assert.equal(state.history.slotCache["slot|generation-3"], undefined);
  assert.equal(state.history.slotCache["slot|generation-4"].payload.marker, "new");
});

test("fresh cached history renders without a network request", async () => {
  const state = makeState();
  state.history.panelRequestToken = 0;
  state.history.panelLoading = false;
  state.history.panelError = null;
  const target = { slot: { slot: 7 }, cacheKey: "slot|generation-3", fetchUrl: "/history/7" };
  let fetchCount = 0;
  let renderCount = 0;
  const { loadHistoryForSelectedSlot } = loadFunctions(["loadHistoryForSelectedSlot"], {
    state,
    getSelectedHistoryTarget: () => target,
    isHistoryAvailable: () => true,
    getLiveHistoryCacheEntry: () => ({
      payload: { marker: "fresh" },
      fetchedAt: 100,
      freshness: "fresh",
    }),
    renderHistoryPanel() { renderCount += 1; },
    fetchLiveHistoryPayload() { fetchCount += 1; },
  });

  await loadHistoryForSelectedSlot(false);

  assert.equal(fetchCount, 0);
  assert.equal(renderCount, 1);
  assert.equal(state.history.panelFreshness.state, "fresh");
  assert.equal(state.history.panelFreshness.fetchedAt, 100);
});

test("expired history stays visible and becomes stale after same-generation revalidation fails", async () => {
  const state = makeState();
  state.history.panelRequestToken = 0;
  state.history.panelLoading = false;
  state.history.panelError = null;
  const target = { slot: { slot: 7 }, cacheKey: "slot|generation-3", fetchUrl: "/history/7" };
  const staleEntry = {
    payload: { marker: "stale" },
    fetchedAt: 100,
    freshness: "stale",
  };
  const renderStates = [];
  const { loadHistoryForSelectedSlot } = loadFunctions(["loadHistoryForSelectedSlot"], {
    state,
    getSelectedHistoryTarget: () => target,
    isHistoryAvailable: () => true,
    getLiveHistoryCacheEntry: () => staleEntry,
    renderHistoryPanel() {
      renderStates.push({
        loading: state.history.panelLoading,
        error: state.history.panelError,
        freshness: state.history.panelFreshness?.state,
      });
    },
    async fetchLiveHistoryPayload() { throw new Error("backend busy"); },
  });

  await loadHistoryForSelectedSlot(false);

  assert.deepEqual(renderStates[0], { loading: true, error: null, freshness: "refreshing" });
  assert.equal(state.history.panelLoading, false);
  assert.equal(state.history.panelError, "backend busy");
  assert.equal(state.history.panelFreshness.state, "stale");
  assert.equal(state.history.panelFreshness.fetchedAt, 100);
});

test("history freshness copy distinguishes fresh, refreshing, stale, and unavailable states", () => {
  const { historyFreshnessNote } = loadFunctions(["historyFreshnessNote"], {
    formatTimestamp: (value) => String(value),
  });

  assert.match(historyFreshnessNote({ state: "fresh", fetchedAt: 100 }), /fresh/i);
  assert.match(historyFreshnessNote({ state: "refreshing", fetchedAt: 100 }), /Refreshing cached history/);
  assert.match(
    historyFreshnessNote({ state: "stale", fetchedAt: 100, error: "backend busy" }),
    /Stale cached history.*refresh failed.*backend busy/i,
  );
  assert.match(historyFreshnessNote({ state: "unavailable" }), /unavailable/i);
});

test("changed collector generation invalidates once while unchanged status does not", () => {
  const state = makeState();
  state.history.statusGenerationMarker = null;
  let invalidations = 0;
  const { updateHistoryStatusGeneration } = loadFunctions(["updateHistoryStatusGeneration"], {
    state,
    invalidateHistoryCaches() { invalidations += 1; },
  });
  const initial = {
    available: true,
    collector: { last_success_at: "2030-01-01T00:00:00Z", last_completed_at: "2030-01-01T00:00:01Z" },
  };

  assert.equal(updateHistoryStatusGeneration(initial), false);
  assert.equal(updateHistoryStatusGeneration(initial), false);
  assert.equal(invalidations, 0);
  assert.equal(updateHistoryStatusGeneration({
    available: true,
    collector: { last_success_at: "2030-01-01T00:01:00Z", last_completed_at: "2030-01-01T00:01:01Z" },
  }), true);
  assert.equal(invalidations, 1);
});

test("hard invalidation preserves the collector marker so a later success change still invalidates", () => {
  const state = makeState();
  state.history.statusGenerationMarker = null;
  state.history.panelRequestToken = 0;
  const helpers = loadFunctions(["invalidateHistoryCaches", "updateHistoryStatusGeneration"], {
    state,
    resetHeatmapHistoryCache() {},
  });
  const initial = {
    available: true,
    collector: { last_success_at: "2030-01-01T00:00:00Z", last_completed_at: "2030-01-01T00:00:01Z" },
  };
  const newer = {
    available: true,
    collector: { last_success_at: "2030-01-01T00:01:00Z", last_completed_at: "2030-01-01T00:01:01Z" },
  };

  assert.equal(helpers.updateHistoryStatusGeneration(initial), false);
  helpers.invalidateHistoryCaches();
  assert.equal(helpers.updateHistoryStatusGeneration(newer), true);
  assert.equal(state.history.generation, 5);
});

test("heatmap history revalidates after TTL and replaces derived caches", async () => {
  const state = makeState();
  state.heatmap.loading = false;
  state.heatmap.pendingScopeKey = null;
  state.heatmap.requestToken = 0;
  state.heatmap.fetchedAt = 10_000;
  state.heatmap.generation = state.history.generation;
  state.heatmap.freshness = "fresh";
  let now = 10_999;
  let fetchCount = 0;
  const { refreshHeatmapHistoryIfNeeded } = loadFunctions(["historyPayloadUnavailableDetail", "refreshHeatmapHistoryIfNeeded"], {
    state,
    Date: { now: () => now },
    HISTORY_PAYLOAD_CACHE_TTL_MS: 1_000,
    heatmapHistoryScopeRequest: () => ({ url: "/history/scope", scopeKey: "old-scope" }),
    async fetchJson() {
      fetchCount += 1;
      return { histories: { 2: { marker: "new" } } };
    },
    renderHeatmapControls() {},
    renderGrid() {},
  });

  await refreshHeatmapHistoryIfNeeded(false);
  assert.equal(fetchCount, 0);
  now = 11_001;
  await refreshHeatmapHistoryIfNeeded(false);

  assert.equal(fetchCount, 1);
  assert.equal(state.heatmap.histories[2].marker, "new");
  assert.equal(state.heatmap.fetchedAt, 11_001);
  assert.equal(state.heatmap.timelineCacheKey, null);
  assert.deepEqual(Array.from(state.heatmap.timelineCache), []);
  assert.equal(state.heatmap.freshness, "fresh");
});

test("concurrent forced heatmap refreshes share the pending scope request", async () => {
  const state = makeState();
  state.heatmap.loading = false;
  state.heatmap.pendingScopeKey = null;
  state.heatmap.requestToken = 0;
  const request = deferred();
  let fetchCount = 0;
  const { refreshHeatmapHistoryIfNeeded } = loadFunctions(["historyPayloadUnavailableDetail", "refreshHeatmapHistoryIfNeeded"], {
    state,
    Date: { now: () => 5_000 },
    HISTORY_PAYLOAD_CACHE_TTL_MS: 1_000,
    heatmapHistoryScopeRequest: () => ({ url: "/history/scope", scopeKey: "old-scope" }),
    fetchJson() {
      fetchCount += 1;
      return request.promise;
    },
    renderHeatmapControls() {},
    renderGrid() {},
  });

  const first = refreshHeatmapHistoryIfNeeded(true);
  const second = refreshHeatmapHistoryIfNeeded(true);
  assert.equal(fetchCount, 1);
  request.resolve({ histories: { 1: { marker: "shared" } } });
  await Promise.all([first, second]);

  assert.equal(state.heatmap.histories[1].marker, "shared");
});

test("failed heatmap revalidation keeps only same-generation data and labels it stale", async () => {
  const state = makeState();
  state.heatmap.loading = false;
  state.heatmap.pendingScopeKey = null;
  state.heatmap.requestToken = 0;
  state.heatmap.fetchedAt = 1;
  state.heatmap.generation = state.history.generation;
  state.heatmap.freshness = "fresh";
  const oldHistories = state.heatmap.histories;
  const { refreshHeatmapHistoryIfNeeded } = loadFunctions(["historyPayloadUnavailableDetail", "refreshHeatmapHistoryIfNeeded"], {
    state,
    Date: { now: () => 5_000 },
    HISTORY_PAYLOAD_CACHE_TTL_MS: 1_000,
    heatmapHistoryScopeRequest: () => ({ url: "/history/scope", scopeKey: "old-scope" }),
    async fetchJson() {
      return {
        histories: {
          1: { available: false, detail: "history backend busy", metrics: {}, events: [] },
        },
      };
    },
    historyPayloadUnavailableDetail(payload) {
      const unavailable = Object.values(payload.histories || {}).find((entry) => entry?.available === false);
      return unavailable?.detail || null;
    },
    renderHeatmapControls() {},
    renderGrid() {},
  });

  await refreshHeatmapHistoryIfNeeded(false);

  assert.equal(state.heatmap.histories, oldHistories);
  assert.equal(state.heatmap.freshness, "stale");
  assert.equal(state.heatmap.error, "history backend busy");
});

test("failed-inventory staleness forces revalidation instead of relabeling a fresh cache hit", async () => {
  const state = makeState();
  state.history.panelRequestToken = 0;
  state.history.panelLoading = false;
  state.history.panelError = null;
  state.history.panelFreshness = { state: "stale", fetchedAt: 100, error: "inventory unavailable" };
  const target = { slot: { slot: 7 }, cacheKey: "slot|generation-3", fetchUrl: "/history/7" };
  let fetchCount = 0;
  const { loadHistoryForSelectedSlot } = loadFunctions(["loadHistoryForSelectedSlot"], {
    state,
    getSelectedHistoryTarget: () => target,
    isHistoryAvailable: () => true,
    getLiveHistoryCacheEntry: () => ({
      payload: { marker: "retained" },
      fetchedAt: 100,
      freshness: "fresh",
    }),
    renderHistoryPanel() {},
    async fetchLiveHistoryPayload() {
      fetchCount += 1;
      throw new Error("history backend busy");
    },
  });

  await loadHistoryForSelectedSlot(false);

  assert.equal(fetchCount, 1);
  assert.equal(state.history.panelFreshness.state, "stale");
  assert.equal(state.history.panelFreshness.error, "history backend busy");
});

test("failed inventory refresh marks retained panel and heatmap history stale without invalidation", () => {
  const state = makeState();
  state.history.panelFreshness = { state: "fresh", fetchedAt: 100 };
  state.heatmap.generation = state.history.generation;
  state.heatmap.freshness = "fresh";
  const { markHistoryCachesStale } = loadFunctions(["markHistoryCachesStale"], { state });

  markHistoryCachesStale("inventory unavailable");

  assert.equal(state.history.generation, 3);
  assert.equal(state.history.panelFreshness.state, "stale");
  assert.equal(state.history.panelFreshness.fetchedAt, 100);
  assert.equal(state.history.panelFreshness.error, "inventory unavailable");
  assert.equal(state.heatmap.freshness, "stale");
  assert.equal(state.heatmap.error, "inventory unavailable");
});
