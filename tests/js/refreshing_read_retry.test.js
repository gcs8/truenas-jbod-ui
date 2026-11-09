"use strict";

// #1144: while the app's own background inventory refresh runs, a read with no
// cached view to fall back on answers a retryable 503 ("The inventory is still
// refreshing", with Retry-After). The page's read callers retry it a bounded
// number of times with the wait shown, instead of ending in "Live estimate
// unavailable" or a failed load. Forced refreshes, writes and identify lights
// are never retried.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = APP_SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = APP_SOURCE.indexOf(")", start);
  const bodyStart = APP_SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    const character = APP_SOURCE[index];
    const next = APP_SOURCE[index + 1];
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "/" && next === "/") {
      lineComment = true;
      index += 1;
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

const RETRY_HELPERS = ["busyReadRetryDelayMs", "busyReadRetryMessage", "retryBusyRead"];

function constantValue(name) {
  const match = APP_SOURCE.match(new RegExp(`const ${name} = (\\d+);`));
  assert.ok(match, `constant ${name} must exist`);
  return Number(match[1]);
}

function load(names, context = {}) {
  const waits = [];
  const sandbox = vm.createContext({
    Math,
    Number,
    Boolean,
    Symbol,
    URLSearchParams,
    BUSY_READ_RETRY_LIMIT: constantValue("BUSY_READ_RETRY_LIMIT"),
    BUSY_READ_RETRY_MAX_DELAY_MS: constantValue("BUSY_READ_RETRY_MAX_DELAY_MS"),
    // Waits resolve at once; the delays asked for are recorded.
    waitMs: async (delayMs) => { waits.push(delayMs); },
    ...context,
  });
  vm.runInContext(`${[...RETRY_HELPERS, ...names].map(functionSource).join("\n")}
this.__loaded = { ${[...RETRY_HELPERS, ...names].join(", ")} };`, sandbox);
  return { ...sandbox.__loaded, waits, sandbox };
}

function busy(retryAfterSeconds = 2) {
  const error = new Error("The inventory is still refreshing. Try again in a moment.");
  error.status = 503;
  error.retryAfterSeconds = retryAfterSeconds;
  return error;
}

function httpResponse(status, payload, retryAfter = null) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (name.toLowerCase() === "retry-after" ? retryAfter : null) },
    json: async () => payload,
  };
}

test("a retryable 503 is retried after its Retry-After, a bounded number of times", async () => {
  const limit = constantValue("BUSY_READ_RETRY_LIMIT");
  const { retryBusyRead, waits } = load([]);
  let reads = 0;
  const retries = [];
  await assert.rejects(
    retryBusyRead(async () => { reads += 1; throw busy(2); }, { onRetry: (attempt) => retries.push(attempt) }),
    /still refreshing/,
  );
  assert.equal(reads, limit + 1);
  assert.deepEqual(retries, Array.from({ length: limit }, (_, index) => index + 1));
  assert.deepEqual(waits, Array.from({ length: limit }, () => 2000));

  reads = 0;
  const value = await retryBusyRead(async () => {
    reads += 1;
    if (reads < 3) throw busy(1);
    return "loaded";
  });
  assert.equal(value, "loaded");
  assert.equal(reads, 3);
});

test("only a 503 with Retry-After is retried, and the wait is clamped", async () => {
  const { retryBusyRead, busyReadRetryDelayMs } = load([]);
  for (const error of [
    Object.assign(new Error("server fault"), { status: 500, retryAfterSeconds: 2 }),
    Object.assign(new Error("unavailable"), { status: 503, retryAfterSeconds: null }),
    Object.assign(new Error("conflict"), { status: 409 }),
    new Error("Failed to fetch"),
  ]) {
    let reads = 0;
    await assert.rejects(retryBusyRead(async () => { reads += 1; throw error; }));
    assert.equal(reads, 1, error.message);
  }
  assert.equal(busyReadRetryDelayMs(busy(0)), 1000);
  assert.equal(busyReadRetryDelayMs(busy(600)), constantValue("BUSY_READ_RETRY_MAX_DELAY_MS"));
});

test("a superseded read stops retrying", async () => {
  const { retryBusyRead } = load([]);
  let current = true;
  let reads = 0;
  await assert.rejects(retryBusyRead(async () => {
    reads += 1;
    current = false;
    throw busy();
  }, { isCurrent: () => current }));
  assert.equal(reads, 1);
});

function exportEstimateState() {
  return {
    snapshotMode: false,
    export: { estimate: { loading: false, error: null, data: null, retryNote: null, requestToken: 0 } },
  };
}

function loadEstimate(state, fetch, dialog = { open: true }) {
  const shown = [];
  const loaded = load(["refreshSnapshotExportEstimate"], {
    state,
    fetch,
    exportSnapshotDialog: dialog,
    buildScopedUrl: (url) => url,
    snapshotExportRequestPayload: () => ({}),
    snapshotExportEstimateBasisKey: () => "basis",
    updateSnapshotExportEstimateSelectionFromCache: () => false,
    syncSnapshotExportDialog: () => shown.push({
      loading: state.export.estimate.loading,
      retryNote: state.export.estimate.retryNote,
      error: state.export.estimate.error,
    }),
    retryAfterSecondsFrom: (response) => {
      const value = response.headers.get("Retry-After");
      return value === null ? null : Number(value);
    },
  });
  return { ...loaded, shown };
}

test("the export estimate retries 'still refreshing' with the wait shown, then shows the estimate", async () => {
  const state = exportEstimateState();
  const responses = [
    httpResponse(503, { detail: "The inventory is still refreshing. Try again in a moment." }, "2"),
    httpResponse(200, { html_size_label: "2.0 MiB", inventory_note: "The inventory refresh is taking longer than usual." }),
  ];
  let requests = 0;
  const { refreshSnapshotExportEstimate, shown, waits } = loadEstimate(state, async () => responses[requests++]);

  await refreshSnapshotExportEstimate();

  assert.equal(requests, 2);
  assert.deepEqual(waits, [2000]);
  // While it waited, the dialog said why and when it would try again.
  const waiting = shown.find((entry) => entry.loading && entry.retryNote);
  assert.ok(waiting, JSON.stringify(shown));
  assert.match(waiting.retryNote, /still refreshing.*again in 2 s.*retry 1 of 3/);
  assert.equal(state.export.estimate.error, null);
  assert.equal(state.export.estimate.data.html_size_label, "2.0 MiB");
  assert.equal(state.export.estimate.retryNote, null);
  assert.equal(state.export.estimate.loading, false);
});

test("the export estimate gives up after its bounded retries and says why", async () => {
  const state = exportEstimateState();
  let requests = 0;
  const { refreshSnapshotExportEstimate } = loadEstimate(state, async () => {
    requests += 1;
    return httpResponse(503, { detail: "The inventory is still refreshing. Try again in a moment." }, "2");
  });

  await refreshSnapshotExportEstimate();

  assert.equal(requests, constantValue("BUSY_READ_RETRY_LIMIT") + 1);
  assert.equal(state.export.estimate.data, null);
  assert.match(state.export.estimate.error, /^Live estimate unavailable: The inventory is still refreshing/);
  assert.equal(state.export.estimate.loading, false);
});

test("the export estimate does not retry an error that waiting will not clear", async () => {
  const state = exportEstimateState();
  let requests = 0;
  const { refreshSnapshotExportEstimate } = loadEstimate(state, async () => {
    requests += 1;
    return httpResponse(500, { detail: "synthetic fault" });
  });
  await refreshSnapshotExportEstimate();
  assert.equal(requests, 1);
  assert.match(state.export.estimate.error, /synthetic fault/);
});

test("closing the export dialog stops the estimate's retries", async () => {
  const state = exportEstimateState();
  const dialog = { open: true };
  let requests = 0;
  const { refreshSnapshotExportEstimate } = loadEstimate(state, async () => {
    requests += 1;
    dialog.open = false;
    return httpResponse(503, { detail: "The inventory is still refreshing. Try again in a moment." }, "2");
  }, dialog);
  await refreshSnapshotExportEstimate();
  assert.equal(requests, 1);
});

test("the estimate shows the note when it was made from an older cached view", () => {
  const element = { innerHTML: "" };
  const state = exportEstimateState();
  state.export.estimate.data = {
    html_size_label: "2.0 MiB",
    zip_size_label: "1.0 MiB",
    html_within_limit: true,
    zip_within_limit: true,
    selected_within_limit: true,
    selected_allowed: true,
    selected_packaging: "auto",
    auto_packaging: "html",
    inventory_note: "The inventory refresh is taking longer than usual, so this view shows the inventory read at <then>.",
  };
  const { renderSnapshotExportEstimate } = load([
    "escapeHtml",
    "estimatePackagingLabel",
    "estimateCurrentPackagingLabel",
    "estimateCurrentPackagingMeta",
    "buildEstimateAdvice",
    "renderSnapshotExportEstimate",
  ], { state, exportSnapshotEstimate: element, String });
  renderSnapshotExportEstimate();
  assert.match(element.innerHTML, /taking longer than usual, so this view shows the inventory read at &lt;then&gt;\./);
});

function refreshContext(state, fetchJson, statuses) {
  return {
    state,
    fetchJson,
    cancelAutoRefreshTimer() {},
    beginUiPerfRun() { return null; },
    refreshStatusMessage: () => "Loading...",
    setStatus(message) { statuses.push(message); },
    buildSelectionParams() { return new URLSearchParams(); },
    applySnapshot(snapshot) { state.applied = snapshot; },
    invalidateHistoryCaches() {},
    renderAll() {},
    fetchStorageViewRuntime() { return Promise.resolve(); },
    fetchSasFabric() { return Promise.resolve(); },
    waitForNextPaint() { return Promise.resolve(); },
    uiPerfNow() { return 0; },
    archiveUiPerfRun() {},
    renderUiPerfPanel() {},
    refreshHistoryStatus() { return Promise.resolve(); },
    completeUiPerfHistory() {},
    setUiPerfSmartPending() {},
    candidateSlotsForSmartPrefetch() { return []; },
    currentSmartPrefetchScopeKey() { return "scope"; },
    scheduleSmartPrefetch() {},
    ensureHeatmapData() {},
    maybeFinalizeUiPerfRun() {},
    markHistoryCachesStale() {},
    renderHistoryPanel() {},
    renderHeatmapControls() {},
    renderStorageViewRuntimeStatus() {},
    scheduleAutoRefresh() {},
  };
}

function refreshState() {
  return {
    snapshotMode: false,
    mappingFormDirty: false,
    latestRefreshToken: 0,
    refreshesInFlight: 0,
    selectedSystemId: "system-a",
    selectedEnclosureId: "enclosure-b",
    storageViewsRuntimeLoading: false,
    sasFabric: { open: false },
    history: { configured: false },
    uiPerf: { currentRun: null },
  };
}

test("an enclosure switch answered 'still refreshing' is retried with the wait shown", async () => {
  const state = refreshState();
  const statuses = [];
  const urls = [];
  const { refreshSnapshot } = load(["refreshSnapshot"], refreshContext(state, async (url) => {
    urls.push(url);
    if (urls.length === 1) throw busy(2);
    return { selected_system_id: "system-a", selected_enclosure_id: "enclosure-b", slots: [] };
  }, statuses));

  await refreshSnapshot(false, "enclosure-switch");

  assert.equal(urls.length, 2);
  assert.ok(urls.every((url) => url.includes("force=false")));
  assert.equal(state.applied.selected_enclosure_id, "enclosure-b");
  assert.ok(statuses.some((message) => /^Loading\.\.\. The inventory is still refreshing\. Trying again in 2 s/.test(message)), statuses);
  assert.equal(statuses.at(-1), "Up to date.");
});

test("a forced Refresh is never sent twice", async () => {
  const state = refreshState();
  const statuses = [];
  let requests = 0;
  const { refreshSnapshot } = load(["refreshSnapshot"], refreshContext(state, async () => {
    requests += 1;
    throw busy(2);
  }, statuses));

  await refreshSnapshot(true, "manual-refresh");

  // The server already joins the running refresh for a forced read (#1139).
  assert.equal(requests, 1);
  assert.match(statuses.at(-1), /^Refresh failed: The inventory is still refreshing/);
});

test("a background auto-refresh is not retried; its timer tries again", async () => {
  const state = refreshState();
  let requests = 0;
  const { refreshSnapshot } = load(["refreshSnapshot"], refreshContext(state, async () => {
    requests += 1;
    throw busy(2);
  }, []));
  await refreshSnapshot(false, "auto-refresh");
  assert.equal(requests, 1);
});

function storageViewContext(state, fetchJson, statuses) {
  return {
    state,
    fetchJson,
    currentUiScopeKey: () => "scope",
    renderSelectors() {},
    renderAll() {},
    renderStorageViewRuntimeStatus() {},
    buildSelectionParams() { return new URLSearchParams(); },
    applyStorageViewRuntime(payload) { state.storageViewsRuntime = payload; },
    setStatus(message) { statuses.push(message); },
  };
}

test("a storage-view load answered 'still refreshing' is retried; a forced one is not", async () => {
  for (const force of [false, true]) {
    const state = { snapshotMode: false, selectedSystemId: "system-a", storageViewsRuntimeRequestToken: 0 };
    const statuses = [];
    let requests = 0;
    const { fetchStorageViewRuntime } = load(["fetchStorageViewRuntime"], storageViewContext(state, async () => {
      requests += 1;
      if (requests === 1) throw busy(2);
      return { system_id: "system-a", views: [] };
    }, statuses));

    await fetchStorageViewRuntime(force, false);

    if (force) {
      assert.equal(requests, 1);
      assert.match(state.storageViewsRuntimeError, /still refreshing/);
    } else {
      assert.equal(requests, 2);
      assert.equal(state.storageViewsRuntimeError, null);
      assert.deepEqual(state.storageViewsRuntime, { system_id: "system-a", views: [] });
      assert.ok(statuses.some((message) => /^Loading storage views\.\.\. The inventory is still refreshing/.test(message)), statuses);
    }
  }
});

test("a SMART batch answered 'still refreshing' is retried while its prefetch run is current", async () => {
  let requests = 0;
  let current = true;
  const { requestSmartBatchForSlots } = load(["requestSmartBatchForSlots"], {
    SMART_BATCH_REQUEST_MAX_CONCURRENCY: 4,
    JSON,
    sendScopedRequest: async (url, options) => {
      requests += 1;
      assert.equal(url, "/api/slots/smart-batch");
      assert.deepEqual(JSON.parse(options.body).slots, [3, 4]);
      if (requests === 1) throw busy(1);
      return { summaries: [] };
    },
  });
  const slots = [{ slot: 3 }, { slot: 4 }];
  assert.deepEqual(await requestSmartBatchForSlots(slots, () => current), { summaries: [] });
  assert.equal(requests, 2);

  requests = 0;
  current = true;
  const replaced = requestSmartBatchForSlots(slots, () => current);
  current = false;
  await assert.rejects(replaced, /still refreshing/);
  assert.equal(requests, 1);
});
