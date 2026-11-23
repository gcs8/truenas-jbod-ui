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

const RETRY_HELPERS = ["busyReadRetryDelayMs", "busyReadRetryMessage", "snapshotExportRetryDelayMs", "retryBusyRead", "smartSummaryRequestSignal", "isSmartSummaryTimeout", "smartSummaryFailureMessage"];

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
    // #1161: SMART summary reads carry a native deadline.
    AbortSignal,
    SMART_SUMMARY_REQUEST_TIMEOUT_MS: constantValue("SMART_SUMMARY_REQUEST_TIMEOUT_MS"),
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

test("the export estimate pauses after its bounded retries and offers a neutral retry", async () => {
  const state = exportEstimateState();
  let requests = 0;
  const { refreshSnapshotExportEstimate } = loadEstimate(state, async () => {
    requests += 1;
    return httpResponse(503, { detail: "The inventory is still refreshing. Try again in a moment." }, "2");
  });

  await refreshSnapshotExportEstimate();

  assert.equal(requests, constantValue("BUSY_READ_RETRY_LIMIT") + 1);
  assert.equal(state.export.estimate.data, null);
  assert.equal(state.export.estimate.error, null);
  assert.match(state.export.estimate.retryNote, /still refreshing/);
  assert.ok(state.export.estimate.retryAtMs > Date.now());
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

function exportDialogHarness(responses) {
  const state = exportEstimateState();
  state.snapshot = {};
  Object.assign(state.export, { running: false, packaging: "zip", redactSensitive: true });
  const clock = { now: 100_000 };
  const requests = [], statuses = [], downloads = [];
  const estimateElement = { innerHTML: "" };
  const confirm = { disabled: false, textContent: "Download copy", addEventListener(_event, handler) { this.click = handler; } };
  const dialog = { open: true, close() { this.open = false; } };
  const h = load([
    "refreshSnapshotExportEstimate", "exportEnclosureSnapshot", "syncSnapshotExportDialog",
    "renderSnapshotExportEstimate", "escapeHtml", "closeSnapshotExportDialog",
  ], {
    state, Date: { now: () => clock.now },
    exportSnapshotDialog: dialog, exportSnapshotConfirm: confirm,
    exportSnapshotEstimate: estimateElement, exportSnapshotNote: { textContent: "" },
    exportSnapshotWindowHint: null, exportRedactToggle: null, exportPackagingSelect: null,
    exportAllowOversizeToggle: null,
    renderSnapshotExportScopeControls() {}, getSelectedEnclosureOption: () => ({ label: "Synthetic shelf" }),
    selectedExportEnclosureIds: () => ["enclosure-a"], selectedExportStorageViewIds: () => [],
    currentHistoryWindowHours: () => 24, formatHistoryWindowDescription: () => "24 hours",
    snapshotExportSelectionDescription: () => "No bay selected.", isHistoryAvailable: () => false,
    buildScopedUrl: url => url, currentUiScopeKey: () => "scope",
    snapshotExportRequestPayload: () => ({ packaging: state.export.packaging, redact_sensitive: state.export.redactSensitive }),
    snapshotExportEstimateBasisKey: () => state.export.packaging,
    updateSnapshotExportEstimateSelectionFromCache: () => false,
    retryAfterSecondsFrom: response => response.headers.get("Retry-After") === null ? null : Number(response.headers.get("Retry-After")),
    fetch: async (url, options) => {
      requests.push({ url, options });
      assert.ok(responses.length, "only an explicit retry may send another request");
      return responses.shift();
    },
    setStatus: (text, kind = "info") => statuses.push({ text, kind }),
    window: { URL: { createObjectURL: () => "blob:synthetic", revokeObjectURL() {} } },
    document: { body: { appendChild() {} }, createElement: () => ({ click() { downloads.push(this.download); }, remove() {} }) },
    resolveDownloadFilename: () => "snapshot.zip", formatScopeLabel: () => "synthetic",
    formatArtifactSize: () => "1 MiB",
  });
  const start = APP_SOURCE.indexOf("  if (exportSnapshotConfirm) {", APP_SOURCE.indexOf('exportSnapshotCancel.addEventListener'));
  const end = APP_SOURCE.indexOf("  if (exportRedactToggle)", start);
  vm.runInContext(APP_SOURCE.slice(start, end), h.sandbox);
  return { ...h, state, clock, requests, statuses, downloads, confirm, dialog, estimateElement };
}

test("the estimate retry action keeps the dialog settings and waits the full Retry-After", async () => {
  const h = exportDialogHarness(Array.from({ length: 4 }, () => httpResponse(503, { detail: "Still refreshing." }, "12")));
  await h.refreshSnapshotExportEstimate();
  assert.equal(h.confirm.textContent, "Retry estimate");
  assert.equal(h.confirm.disabled, false);
  assert.doesNotMatch(h.estimateElement.innerHTML, /unavailable|warning|error/i);
  assert.match(h.estimateElement.innerHTML, /still refreshing/i);
  assert.deepEqual(h.waits, [12000, 12000, 12000]);
  assert.equal(h.requests.length, 4);
  // A manual retry is another estimate, never a download or a reopened dialog.
  let release;
  h.sandbox.waitMs = delay => new Promise(resolve => { h.waits.push(delay); release = resolve; });
  h.confirm.click();
  assert.equal(h.confirm.disabled, true);
  assert.equal(h.requests.length, 4, "the manual retry must wait for Retry-After");
  h.dialog.open = false;
  release();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.requests.length, 4, "closing the dialog cancels a waiting manual retry");
  assert.equal(h.state.export.packaging, "zip");
  assert.equal(h.state.export.redactSensitive, true);
  assert.equal(h.downloads.length, 0);
});

test("an explicit estimate retry succeeds without restarting the export", async () => {
  const h = exportDialogHarness(Array.from({ length: 4 }, () => httpResponse(503, { detail: "Still refreshing." }, "2")));
  await h.refreshSnapshotExportEstimate();
  h.sandbox.renderSnapshotExportEstimate = () => {};
  h.sandbox.fetch = async (url, options) => {
    h.requests.push({ url, options });
    return httpResponse(200, { selected_allowed: true, html_size_label: "1 MiB" });
  };
  await h.refreshSnapshotExportEstimate();
  assert.equal(h.requests.length, 5);
  assert.ok(h.requests.every(request => request.url.endsWith("/estimate")));
  assert.ok(h.requests.every(request => request.options.body === h.requests[0].options.body));
  assert.equal(h.state.export.estimate.error, null);
  assert.equal(h.state.export.estimate.data.html_size_label, "1 MiB");
  assert.equal(h.state.export.estimate.retryNote, null);
  assert.equal(h.confirm.textContent, "Download copy");
  assert.equal(h.dialog.open, true);
  assert.equal(h.downloads.length, 0);
  assert.deepEqual(h.waits, [2000, 2000, 2000, 2000]);
});

test("a busy download offers a neutral retry and preserves the selected export", async () => {
  const success = { ...httpResponse(200, {}), blob: async () => ({}) };
  const h = exportDialogHarness([httpResponse(503, { detail: "Still refreshing." }, "12"), success]);
  await h.exportEnclosureSnapshot();
  assert.equal(h.requests.length, 1, "a busy download waits for the operator's retry");
  assert.equal(h.state.export.running, false);
  assert.equal(h.dialog.open, true);
  assert.equal(h.confirm.textContent, "Retry download");
  assert.equal(h.confirm.disabled, false);
  assert.ok(h.statuses.every(row => row.kind === "info"));
  assert.match(h.statuses.at(-1).text, /still refreshing/i);
  let release;
  h.sandbox.waitMs = delay => new Promise(resolve => { h.waits.push(delay); release = resolve; });
  h.confirm.click();
  assert.equal(h.state.export.running, true);
  assert.equal(h.confirm.disabled, true);
  assert.deepEqual(h.waits, [12000]);
  assert.equal(h.requests.length, 1, "Retry-After is respected before re-asking");
  release();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[0].options.body, h.requests[1].options.body);
  assert.deepEqual(h.downloads, ["snapshot.zip"]);
  assert.equal(h.dialog.open, false);
  assert.equal(h.confirm.textContent, "Download copy");
});

test("closing the dialog during a busy download retry does not send another request", async () => {
  const h = exportDialogHarness([httpResponse(503, { detail: "Still refreshing." }, "2")]);
  await h.exportEnclosureSnapshot();
  let release;
  h.sandbox.waitMs = () => new Promise(resolve => { release = resolve; });
  const retry = h.exportEnclosureSnapshot();
  assert.equal(h.requests.length, 1);
  h.dialog.open = false;
  release();
  await retry;
  assert.equal(h.requests.length, 1);
  assert.equal(h.state.export.running, false);
  assert.equal(h.downloads.length, 0);
});

test("a waiting download retry cannot follow a different scope or changed settings", async () => {
  for (const change of ["scope", "settings"]) {
    const h = exportDialogHarness([httpResponse(503, { detail: "Still refreshing." }, "2")]);
    await h.exportEnclosureSnapshot();
    let release;
    h.sandbox.waitMs = () => new Promise(resolve => { release = resolve; });
    const retry = h.exportEnclosureSnapshot();
    if (change === "scope") h.sandbox.currentUiScopeKey = () => "another-scope";
    else h.state.export.packaging = "html";
    release();
    await retry;
    assert.equal(h.requests.length, 1, change);
    assert.equal(h.state.export.running, false);
    assert.equal(h.downloads.length, 0);
  }
});

test("a non-busy download error remains an error without a retry label", async () => {
  const h = exportDialogHarness([httpResponse(500, { detail: "Synthetic fault." })]);
  await h.exportEnclosureSnapshot();
  assert.equal(h.requests.length, 1);
  assert.equal(h.confirm.textContent, "Download copy");
  assert.equal(h.statuses.at(-1).kind, "error");
  assert.match(h.statuses.at(-1).text, /Synthetic fault/);
  assert.equal(h.waits.length, 0);
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

// Storage-view SMART (#1144 follow-up). A storage-view slot cannot be matched
// to a disk until the inventory view exists, so with no cached view its SMART
// read answers the retryable 503. The caller used to store that answer as the
// slot's SMART data for the full SMART cache lifetime, so the panel kept
// saying "still refreshing" long after the refresh had finished.

const STORAGE_VIEW = { id: "synthetic-view", backing_enclosure_id: null };
const STORAGE_VIEW_SLOT = { slot_index: 2, serial: "SYNTH-0002" };
const SMART_CACHE_KEY = "system-a|storage-view|synthetic-view||2||synth-0002";

function storageViewSmartHarness(fetchJson, { clock = { now: 1_000_000 } } = {}) {
  const state = {
    snapshotMode: false,
    smartSummaries: {},
    smartSummaryGeneration: 1,
    selectedStorageViewRuntimeId: STORAGE_VIEW.id,
    selectedSlot: STORAGE_VIEW_SLOT.slot_index,
    hoveredSlot: null,
    heatmap: { enabled: false },
  };
  const rendered = [];
  const loaded = load([
    "smartSummaryAgeMs",
    "isSmartEntryCurrent",
    "isSmartEntryInFlight",
    "ensureStorageViewSmartSummary",
  ], {
    state,
    fetchJson,
    encodeURIComponent,
    Date: { now: () => clock.now },
    SMART_SUMMARY_CACHE_TTL_MS: 300_000,
    getStorageViewSmartCacheKey: () => SMART_CACHE_KEY,
    buildSelectionParams: () => new URLSearchParams("system_id=system-a"),
    renderDetail() {
      // The detail panel asks for SMART on every render, as the page does.
      rendered.push({ ...state.smartSummaries[SMART_CACHE_KEY] });
      void loaded.ensureStorageViewSmartSummary(STORAGE_VIEW, STORAGE_VIEW_SLOT);
    },
    refreshHoveredTooltip() {},
    getLiveBackedStorageViewSlot: () => null,
    refreshGridTileAriaLabel() {},
    slotAccessibleName: () => "",
    buildStorageViewRuntimeTooltip: () => "",
    refreshHeatmapTileOverlays() {},
  });
  return { ...loaded, state, rendered, clock };
}

test("storage-view SMART answered 'still refreshing' is retried with the wait shown", async () => {
  const urls = [];
  const harness = storageViewSmartHarness(async (url) => {
    urls.push(url);
    if (urls.length === 1) throw busy(2);
    return { available: true, temperature_c: 31 };
  });

  await harness.ensureStorageViewSmartSummary(STORAGE_VIEW, STORAGE_VIEW_SLOT);

  assert.equal(urls.length, 2);
  assert.ok(urls.every((url) => url === "/api/storage-views/synthetic-view/slots/2/smart?system_id=system-a"), urls);
  assert.deepEqual(harness.waits, [2000]);
  // While it waited, the panel showed the slot as loading and said why.
  const waiting = harness.rendered.find((entry) => entry.loading && entry.retryNote);
  assert.ok(waiting, JSON.stringify(harness.rendered));
  assert.match(waiting.retryNote, /still refreshing.*again in 2 s.*retry 1 of 3/);
  const entry = harness.state.smartSummaries[SMART_CACHE_KEY];
  assert.deepEqual({ ...entry.data }, { available: true, temperature_c: 31 });
  assert.equal(entry.loading, false);
  assert.equal(harness.isSmartEntryCurrent(entry), true);
});

test("storage-view SMART that stays 'still refreshing' is not kept as the slot's data", async () => {
  let requests = 0;
  const harness = storageViewSmartHarness(async () => {
    requests += 1;
    if (requests <= constantValue("BUSY_READ_RETRY_LIMIT") + 1) throw busy(2);
    return { available: true, temperature_c: 33 };
  });

  await harness.ensureStorageViewSmartSummary(STORAGE_VIEW, STORAGE_VIEW_SLOT);

  // The same bounded policy as the page's other reads, then it gives up...
  assert.equal(requests, constantValue("BUSY_READ_RETRY_LIMIT") + 1);
  const failed = harness.state.smartSummaries[SMART_CACHE_KEY];
  assert.equal(failed.loading, false);
  assert.match(failed.data.message, /still refreshing/);
  // ...but the answer is not current SMART data, and the re-render that
  // shows it does not start another round at once.
  assert.equal(harness.isSmartEntryCurrent(failed), false);
  assert.equal(requests, constantValue("BUSY_READ_RETRY_LIMIT") + 1);

  // After the server's Retry-After, the next look at the slot reads again.
  harness.clock.now += 2000;
  await harness.ensureStorageViewSmartSummary(STORAGE_VIEW, STORAGE_VIEW_SLOT);
  assert.equal(requests, constantValue("BUSY_READ_RETRY_LIMIT") + 2);
  const entry = harness.state.smartSummaries[SMART_CACHE_KEY];
  assert.deepEqual({ ...entry.data }, { available: true, temperature_c: 33 });
  assert.equal(harness.isSmartEntryCurrent(entry), true);
});

test("storage-view SMART stops retrying once another request owns the slot", async () => {
  let requests = 0;
  let harness;
  harness = storageViewSmartHarness(async () => {
    requests += 1;
    // A scope change drops in-flight SMART entries.
    delete harness.state.smartSummaries[SMART_CACHE_KEY];
    throw busy(2);
  });
  await harness.ensureStorageViewSmartSummary(STORAGE_VIEW, STORAGE_VIEW_SLOT);
  assert.equal(requests, 1);
  assert.equal(harness.state.smartSummaries[SMART_CACHE_KEY], undefined);
});

test("storage-view SMART still keeps an error that waiting will not clear", async () => {
  let requests = 0;
  const harness = storageViewSmartHarness(async () => {
    requests += 1;
    throw Object.assign(new Error("synthetic fault"), { status: 400 });
  });
  await harness.ensureStorageViewSmartSummary(STORAGE_VIEW, STORAGE_VIEW_SLOT);
  assert.equal(requests, 1);
  const entry = harness.state.smartSummaries[SMART_CACHE_KEY];
  assert.match(entry.data.message, /synthetic fault/);
  assert.equal(harness.isSmartEntryCurrent(entry), true);
});

test("the SMART note and tooltip say a storage-view SMART read is waiting to retry", () => {
  const { buildSmartNoteText, appendSmartTooltipMetrics } = load(["buildSmartNoteText", "appendSmartTooltipMetrics"], {
    Number, String,
  });
  const retryNote = "The inventory is still refreshing. Trying again in 2 s (retry 1 of 3).";
  assert.equal(buildSmartNoteText({ led_supported: true }, { loading: true, data: null, retryNote }), retryNote);
  // A settled entry never repeats an old wait.
  assert.equal(buildSmartNoteText({ led_supported: true }, { loading: false, data: { available: true }, retryNote }), "");
  const lines = [];
  appendSmartTooltipMetrics(lines, {}, { loading: true, retryNote });
  assert.deepEqual(lines, ["SMART: loading...", retryNote]);
});
