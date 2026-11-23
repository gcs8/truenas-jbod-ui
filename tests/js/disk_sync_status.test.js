"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../app/static/app.js"), "utf8");
const section = source.slice(source.indexOf("  function diskInventorySyncPlatformSupported("), source.indexOf("  // The server refused a bay-assignment"));

function harness(answers) {
  const statuses = [], requests = [], refreshes = [], waits = [];
  const state = { selectedSystemId: "system-a", snapshot: { sources: { ssh: { enabled: true } } }, diskInventorySync: { inFlight: false } };
  const context = vm.createContext({
    state, currentPlatform: () => "core", diskInventorySyncControls: null,
    setStatus: (text, kind = "info") => statuses.push({ text, kind }),
    refreshSnapshot: async (...args) => refreshes.push(args),
    window: {
      setInterval: () => 1, clearInterval: () => {},
      setTimeout: (fn, milliseconds) => { waits.push(milliseconds); fn(); },
    },
    fetchJson: async (url, options) => {
      requests.push({ url, options });
      assert.ok(answers.length, "status polling must stop at completion");
      const answer = answers.shift();
      if (answer instanceof Error) throw answer;
      return answer;
    },
  });
  vm.runInContext(`${section}\nthis.run = runDiskInventorySync;`, context);
  return { context, statuses, requests, refreshes, waits, state };
}

test("busy refusal is neutral and observes completion without another write", async () => {
  const busy = new Error("busy");
  busy.status = 409;
  busy.detail = { state: "WAITING", job_id: 17, message: "Waiting behind disk sync job 16 (running, 20%)." };
  const h = harness([busy, { state: "SUCCESS", job_id: 17, message: "Inventory re-read." }]);
  await h.context.run("full", "system-a");
  assert.ok(h.statuses.some(row => row.text.includes("Waiting behind disk sync job 16") && row.kind === "info"));
  assert.ok(!h.statuses.some(row => row.kind === "error"));
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[0].options.method, "POST");
  assert.equal(h.requests[1].options?.method || "GET", "GET");
  assert.equal(h.refreshes.length, 1);
  assert.equal(h.state.diskInventorySync.inFlight, false);
});

test("running response stays neutral until the real job fails with its error", async () => {
  const h = harness([
    { state: "RUNNING", job_id: 17, timed_out: true, message: "Disk sync job 17 is running." },
    { state: "FAILED", job_id: 17, error: "UNIQUE constraint failed: storage_disk.disk_identifier", message: "Disk sync job 17 failed." },
  ]);
  await h.context.run("full", "system-a");
  assert.equal(h.requests.length, 2);
  assert.equal(h.statuses.filter(row => row.kind === "error").length, 1);
  assert.match(h.statuses.at(-1).text, /UNIQUE constraint failed/);
  assert.equal(h.refreshes.length, 0);
});

test("unknown old status allows another full sync and no failure styling", async () => {
  const h = harness([
    { state: "UNKNOWN", job_id: 17, message: "Previous job state is unknown. You can start a new sync." },
    { state: "SUCCESS", job_id: 17, message: "Inventory re-read." },
  ]);
  await h.context.run("full", "system-a");
  assert.ok(h.statuses.some(row => row.text.includes("unknown") && row.kind === "info"));
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.equal(h.state.diskInventorySync.inFlight, false);
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].options.method, "GET");
  assert.equal(h.refreshes.length, 1);
});

test("multipath prerequisite is guidance and does not poll or refresh", async () => {
  const h = harness([{ state: "MULTIPATH_REQUIRED", message: "Run Sync multipath table before Full disk sync." }]);
  await h.context.run("full", "system-a");
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.equal(h.requests.length, 1);
  assert.equal(h.refreshes.length, 0);
});

test("status-read transport failure becomes unknown, not a failed sync", async () => {
  const h = harness([
    { state: "WAITING", job_id: 17, message: "Job 17 is queued." },
    new Error("temporary status timeout"),
    { state: "SUCCESS", job_id: 17, message: "Inventory re-read." },
  ]);
  await h.context.run("full", "system-a");
  assert.ok(h.statuses.some(row => row.text.includes("state is unknown") && row.kind === "info"));
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.equal(h.requests.filter(row => row.options.method === "POST").length, 1);
  assert.equal(h.requests.length, 3);
  assert.match(h.requests[2].url, /job_id=17$/);
  assert.equal(h.refreshes.length, 1);
});

test("unknown known-job observations remain bounded and never resubmit POST", async () => {
  const h = harness(Array.from({ length: 721 }, () => ({
    state: "UNKNOWN", job_id: 17, message: "State is unknown.",
  })));
  await h.context.run("full", "system-a");
  assert.equal(h.requests.length, 721);
  assert.equal(h.requests.filter(row => row.options.method === "POST").length, 1);
  assert.ok(h.requests.slice(1).every(row => row.options.method === "GET" && row.url.endsWith("job_id=17")));
  assert.ok(h.statuses.every(row => row.kind === "info"));
  assert.equal(h.state.diskInventorySync.inFlight, false);
});

test("unknown without a job id ends observation without a write replay", async () => {
  const h = harness([{ state: "UNKNOWN", message: "No job id is available." }]);
  await h.context.run("full", "system-a");
  assert.equal(h.requests.length, 1);
  assert.equal(h.statuses.at(-1).kind, "info");
});

test("multipath remains available while a full sync request owns its button", async () => {
  const h = harness([]);
  h.state.diskInventorySync = { full: {}, inFlight: true, mode: "full" };
  assert.equal(h.context.diskInventorySyncModeAvailability("multipath").available, true);
  assert.equal(h.context.diskInventorySyncModeAvailability("full").available, false);
});

test("a multipath completion cannot release an outstanding full request", async () => {
  let release;
  const first = new Promise(resolve => { release = resolve; });
  const h = harness([first, { state: "SUCCESS", message: "Multipath rebuilt." }]);
  const full = h.context.run("full", "system-a");
  await h.context.run("multipath", "system-a");
  assert.equal(h.state.diskInventorySync.inFlight, true);
  assert.equal(h.context.diskInventorySyncModeAvailability("full").available, false);
  release({ state: "SUCCESS", message: "Inventory re-read." });
  await full;
  assert.equal(h.state.diskInventorySync.inFlight, false);
});

test("full failure remains visible after a newer multipath success", async () => {
  let release;
  const fullAnswer = new Promise(resolve => { release = resolve; });
  const h = harness([fullAnswer, { state: "SUCCESS", message: "Multipath rebuilt." }]);
  const full = h.context.run("full", "system-a");
  await h.context.run("multipath", "system-a");
  release({ state: "FAILED", job_id: 17, message: "Disk sync failed.", error: "Synthetic full-job error." });
  await full;
  assert.equal(h.statuses.at(-1).kind, "error");
  assert.match(h.statuses.at(-1).text, /Synthetic full-job error/);
  assert.match(h.statuses.at(-1).text, /Multipath rebuilt/);
  assert.equal(h.requests.filter(row => row.options.method === "POST").length, 2);
});

test("a full failure wins visually even when multipath finishes afterward", async () => {
  let release;
  const multipathAnswer = new Promise(resolve => { release = resolve; });
  const h = harness([
    multipathAnswer,
    { state: "FAILED", job_id: 17, message: "Disk sync failed.", error: "Synthetic full-job error." },
  ]);
  const multipath = h.context.run("multipath", "system-a");
  await h.context.run("full", "system-a");
  release({ state: "SUCCESS", message: "Multipath rebuilt." });
  await multipath;
  assert.equal(h.statuses.at(-1).kind, "error");
  assert.match(h.statuses.at(-1).text, /Synthetic full-job error/);
  assert.match(h.statuses.at(-1).text, /Multipath rebuilt/);
  assert.match(h.statuses.at(-1).text, /^Full disk sync: Disk sync failed/);
});

test("a later successful full sync replaces its earlier failure, not a multipath failure", async () => {
  const h = harness([
    { state: "FAILED", job_id: 17, message: "Earlier full sync failed.", error: "Earlier full error." },
    { state: "FAILED", message: "Multipath failed.", error: "Multipath error." },
    { state: "SUCCESS", job_id: 18, message: "New full sync succeeded." },
    { state: "SUCCESS", message: "New multipath succeeded." },
  ]);
  await h.context.run("full", "system-a");
  await h.context.run("multipath", "system-a");
  await h.context.run("full", "system-a");
  assert.doesNotMatch(h.statuses.at(-1).text, /Earlier full/);
  assert.match(h.statuses.at(-1).text, /^Sync multipath table: Multipath failed/);
  assert.match(h.statuses.at(-1).text, /New full sync succeeded/);
  assert.equal(h.statuses.at(-1).kind, "error");
  await h.context.run("multipath", "system-a");
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.doesNotMatch(h.statuses.at(-1).text, /error|failed/);
});

test("unknown job observation permits a separately requested full sync and keeps observing the old job", async () => {
  const h = harness([
    { state: "UNKNOWN", job_id: 17, message: "State is unknown. You can start a new sync." },
    { state: "SUCCESS", job_id: 18, message: "New inventory re-read." },
    { state: "FAILED", job_id: 17, message: "Earlier job failed.", error: "Synthetic earlier-job error." },
  ]);
  let reached, resume;
  const observing = new Promise(resolve => { reached = resolve; });
  h.context.window.setTimeout = fn => { resume = fn; reached(); };
  const first = h.context.run("full", "system-a");
  await observing;
  assert.equal(h.context.diskInventorySyncModeAvailability("full").available, true);
  assert.equal(h.state.diskInventorySync.inFlight, false, "read-only observation never owns POST admission");
  assert.equal(h.requests.length, 1, "observation must not automatically submit another sync");
  await h.context.run("full", "system-a");
  assert.equal(h.requests.length, 2, "a separate explicit request reaches the backend");
  assert.equal(h.requests[1].options.method, "POST");
  resume();
  await first;
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].options.method, "GET");
  assert.match(h.requests[2].url, /job_id=17$/);
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.doesNotMatch(h.statuses.at(-1).text, /Synthetic earlier-job error|Earlier job failed/);
  assert.match(h.statuses.at(-1).text, /New inventory re-read/);
  assert.equal(h.state.diskInventorySync.statuses.length, 1);
});

test("an older refresh completion cannot republish success over a newer failure", async t => {
  for (const refreshFails of [false, true]) {
    await t.test(refreshFails ? "refresh unavailable" : "refresh succeeds", async () => {
      const h = harness([
        { state: "SUCCESS", job_id: 17, message: "Older inventory re-read." },
        { state: "FAILED", job_id: 18, message: "New job failed.", error: "New job error." },
      ]);
      let entered, finish;
      const refreshing = new Promise(resolve => { entered = resolve; });
      const refresh = new Promise((resolve, reject) => { finish = refreshFails ? reject : resolve; });
      h.context.refreshSnapshot = async () => { entered(); await refresh; };
      const older = h.context.run("full", "system-a");
      await refreshing;
      assert.equal(h.context.diskInventorySyncModeAvailability("full").available, true);
      await h.context.run("full", "system-a");
      assert.equal(h.statuses.at(-1).kind, "error");
      assert.match(h.statuses.at(-1).text, /New job error/);
      assert.equal(h.state.diskInventorySync.statuses.length, 1);
      const rendered = h.statuses.length;
      finish(refreshFails ? new Error("Synthetic refresh unavailable.") : undefined);
      await older;
      assert.equal(h.statuses.length, rendered, "a retired owner must not render again");
      assert.equal(h.statuses.at(-1).kind, "error");
      assert.match(h.statuses.at(-1).text, /New job error/);
      assert.doesNotMatch(h.statuses.at(-1).text, /Older inventory|refresh is unavailable/);
      assert.equal(h.state.diskInventorySync.statuses.length, 1);
      assert.equal(h.state.diskInventorySync.statuses[0].result.job_id, 18);
      assert.equal(h.state.diskInventorySync.observations.size, 0);
    });
  }
});

test("an older observed success cannot start a refresh after a newer failure", async () => {
  const h = harness([
    { state: "UNKNOWN", job_id: 17, message: "Old job state unknown." },
    { state: "FAILED", job_id: 18, message: "New job failed.", error: "New job error." },
    { state: "SUCCESS", job_id: 17, message: "Old job succeeded." },
  ]);
  let reached, resume;
  const observing = new Promise(resolve => { reached = resolve; });
  h.context.window.setTimeout = fn => { resume = fn; reached(); };
  const older = h.context.run("full", "system-a");
  await observing;
  await h.context.run("full", "system-a");
  resume();
  await older;
  assert.equal(h.refreshes.length, 0, "a superseded success must not start a new inventory refresh");
  assert.equal(h.statuses.at(-1).kind, "error");
  assert.match(h.statuses.at(-1).text, /New job error/);
});

test("the real inventory refresh cannot hide a superseding job failure", async t => {
  for (const refreshFails of [false, true]) {
    await t.test(refreshFails ? "refresh fails" : "refresh completes", async () => {
      const h = harness([
        { state: "SUCCESS", job_id: 17, message: "Old job succeeded." },
        { state: "FAILED", job_id: 18, message: "New job failed.", error: "New job error." },
      ]);
      let entered, finish;
      const refreshing = new Promise(resolve => { entered = resolve; });
      const inventory = new Promise((resolve, reject) => { finish = refreshFails ? reject : resolve; });
      const syncFetch = h.context.fetchJson;
      const applied = [];
      const noop = () => {};
      Object.assign(h.state, { latestRefreshToken: 0, refreshesInFlight: 0, sasFabric: {}, history: {} });
      Object.assign(h.context, {
        URLSearchParams, cancelAutoRefreshTimer: noop, beginUiPerfRun: () => null,
        refreshStatusMessage: () => "Refreshing inventory.", buildSelectionParams: () => new URLSearchParams(),
        applySnapshot: snapshot => applied.push(snapshot), invalidateHistoryCaches: noop, renderAll: noop,
        fetchStorageViewRuntime: noop, waitForNextPaint: async () => {}, renderUiPerfPanel: noop,
        renderStorageViewRuntimeStatus: noop, markHistoryCachesStale: noop, renderHistoryPanel: noop, renderHeatmapControls: noop,
        refreshHistoryStatus: async () => {}, completeUiPerfHistory: noop, setUiPerfSmartPending: noop,
        candidateSlotsForSmartPrefetch: () => [], currentSmartPrefetchScopeKey: noop,
        scheduleSmartPrefetch: noop, ensureHeatmapData: noop, maybeFinalizeUiPerfRun: noop, scheduleAutoRefresh: noop,
        fetchJson: async (url, options) => {
          if (url.startsWith("/api/inventory?")) { entered(); return inventory; }
          return syncFetch(url, options);
        },
      });
      vm.runInContext(source.slice(source.indexOf("  async function refreshSnapshot("), source.indexOf("  async function sendLedAction(")), h.context);
      const older = h.context.run("full", "system-a");
      await refreshing;
      await h.context.run("full", "system-a");
      assert.match(h.statuses.at(-1).text, /New job error/);
      finish(refreshFails ? new Error("Synthetic inventory read unavailable.") : {});
      await older;
      assert.equal(h.statuses.at(-1).kind, "error");
      assert.match(h.statuses.at(-1).text, /New job error/);
      assert.equal(applied.length, 0, "a superseded refresh must not publish its captured inventory either");
    });
  }
});

test("a superseded owner stays retired after newer outcomes leave the bounded list", async () => {
  const h = harness([
    { state: "SUCCESS", job_id: 17, message: "Older inventory re-read." },
    { state: "FAILED", job_id: 18, message: "New job failed.", error: "New job error." },
    ...Array.from({ length: 8 }, () => ({ state: "SUCCESS", message: "Other system multipath rebuilt." })),
  ]);
  let entered, finish;
  const refreshing = new Promise(resolve => { entered = resolve; });
  const refresh = new Promise(resolve => { finish = resolve; });
  h.context.refreshSnapshot = async () => {
    if (h.state.selectedSystemId === "system-a") { entered(); await refresh; }
  };
  const older = h.context.run("full", "system-a");
  await refreshing;
  await h.context.run("full", "system-a");
  for (let index = 0; index < 8; index += 1) {
    h.state.selectedSystemId = `other-system-${index}`;
    await h.context.run("multipath", h.state.selectedSystemId);
  }
  assert.equal(h.state.diskInventorySync.statuses.length, 8);
  assert.ok(h.state.diskInventorySync.statuses.every(row => row.systemId !== "system-a"));
  h.state.selectedSystemId = "system-a";
  finish();
  await older;
  assert.equal(h.state.diskInventorySync.statuses.length, 8);
  assert.ok(h.state.diskInventorySync.statuses.every(row => row.systemId !== "system-a"),
    "retired owners cannot return when the replacement is no longer in the list");
});

test("a later request wins even when its job id is lower", async () => {
  const h = harness([
    { state: "UNKNOWN", job_id: 18, message: "Earlier request state unknown." },
    { state: "FAILED", job_id: 17, message: "Later request failed.", error: "Later request error." },
    { state: "SUCCESS", job_id: 18, message: "Earlier request succeeded." },
  ]);
  let reached, resume;
  const observing = new Promise(resolve => { reached = resolve; });
  h.context.window.setTimeout = fn => { resume = fn; reached(); };
  const earlier = h.context.run("full", "system-a");
  await observing;
  await h.context.run("full", "system-a");
  resume();
  await earlier;
  assert.equal(h.refreshes.length, 0, "a superseded success must not start a new inventory refresh");
  assert.equal(h.statuses.at(-1).kind, "error");
  assert.doesNotMatch(h.statuses.at(-1).text, /Earlier request/);
  assert.match(h.statuses.at(-1).text, /Later request error/);
  assert.equal(h.state.diskInventorySync.statuses.length, 1);
});

test("job ids restarting after a middleware restart cannot hide a newer success", async () => {
  const h = harness([
    { state: "FAILED", job_id: 1700, message: "Job 1700 failed.", error: "Pre-restart error." },
    { state: "SUCCESS", job_id: 17, message: "Job 17 re-read inventory." },
  ]);
  await h.context.run("full", "system-a");
  assert.equal(h.statuses.at(-1).kind, "error");
  await h.context.run("full", "system-a");
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.doesNotMatch(h.statuses.at(-1).text, /Pre-restart error|Job 1700/);
  assert.match(h.statuses.at(-1).text, /Job 17 re-read inventory/);
  assert.equal(h.refreshes.length, 1, "the newer success must refresh the inventory");
  assert.equal(h.state.diskInventorySync.statuses.length, 1);
});

test("a pre-restart job finishing late cannot hide a newer success with a lower job id", async () => {
  const h = harness([
    { state: "UNKNOWN", job_id: 1700, message: "Job 1700 state unknown." },
    { state: "SUCCESS", job_id: 17, message: "Job 17 re-read inventory." },
    { state: "FAILED", job_id: 1700, message: "Job 1700 failed.", error: "Pre-restart error." },
  ]);
  let reached, resume;
  const observing = new Promise(resolve => { reached = resolve; });
  h.context.window.setTimeout = fn => { resume = fn; reached(); };
  const earlier = h.context.run("full", "system-a");
  await observing;
  await h.context.run("full", "system-a");
  resume();
  await earlier;
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.doesNotMatch(h.statuses.at(-1).text, /Pre-restart error/);
  assert.match(h.statuses.at(-1).text, /Job 17 re-read inventory/);
  assert.equal(h.state.diskInventorySync.statuses.length, 1);
});

test("a successful full sync replaces an earlier failure that has no job id", async () => {
  const postError = Object.assign(new Error("HTTP 500 upstream"), { status: 500 });
  const h = harness([postError, { state: "SUCCESS", job_id: 20, message: "Job 20 re-read inventory." }]);
  await h.context.run("full", "system-a");
  assert.equal(h.statuses.at(-1).kind, "error");
  await h.context.run("full", "system-a");
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.doesNotMatch(h.statuses.at(-1).text, /HTTP 500/);
  assert.match(h.statuses.at(-1).text, /Job 20 re-read inventory/);
  assert.equal(h.state.diskInventorySync.statuses.length, 1);
});

test("an older failure without a job id cannot hide a newer successful job", async () => {
  let reject;
  const postAnswer = new Promise((_resolve, fail) => { reject = fail; });
  const h = harness([postAnswer, { state: "SUCCESS", job_id: 20, message: "Job 20 re-read inventory." }]);
  const older = h.context.run("full", "system-a");
  await Promise.resolve();
  // Admission normally serializes POSTs; release it here to pin ordering when the old failure lands last.
  delete h.state.diskInventorySync.full;
  await h.context.run("full", "system-a");
  reject(Object.assign(new Error("HTTP 504 gateway timeout"), { status: 504 }));
  await older;
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.doesNotMatch(h.statuses.at(-1).text, /HTTP 504/);
  assert.equal(h.state.diskInventorySync.statuses.length, 1);
  assert.equal(h.state.diskInventorySync.statuses[0].result.job_id, 20);
});

test("an older job's success cannot hide a newer failure that has no job id", async () => {
  const h = harness([
    { state: "UNKNOWN", job_id: 17, message: "Old job state unknown." },
    Object.assign(new Error("HTTP 500 upstream"), { status: 500 }),
    { state: "SUCCESS", job_id: 17, message: "Old job succeeded." },
  ]);
  let reached, resume;
  const observing = new Promise(resolve => { reached = resolve; });
  h.context.window.setTimeout = fn => { resume = fn; reached(); };
  const older = h.context.run("full", "system-a");
  await observing;
  await h.context.run("full", "system-a");
  resume();
  await older;
  assert.equal(h.refreshes.length, 0, "a superseded success must not start a new inventory refresh");
  assert.equal(h.statuses.at(-1).kind, "error");
  assert.match(h.statuses.at(-1).text, /HTTP 500/);
  assert.doesNotMatch(h.statuses.at(-1).text, /Old job succeeded/);
  assert.equal(h.state.diskInventorySync.statuses.length, 1);
});

test("a newer failure or abort replaces an earlier same-mode terminal outcome", async t => {
  for (const terminal of ["FAILED", "ABORTED"]) {
    await t.test(terminal, async () => {
      const h = harness([
        { state: "SUCCESS", job_id: 17, message: "Old job succeeded." },
        { state: terminal, job_id: 18, message: "New job stopped.", error: "New terminal error." },
      ]);
      await h.context.run("full", "system-a");
      await h.context.run("full", "system-a");
      assert.equal(h.state.diskInventorySync.statuses.length, 1);
      assert.equal(h.statuses.at(-1).kind, "error");
      assert.doesNotMatch(h.statuses.at(-1).text, /Old job/);
      assert.match(h.statuses.at(-1).text, /New terminal error/);
    });
  }
});

test("known running observation defers admission decisions to the backend busy refusal", async () => {
  const busy = new Error("busy");
  busy.status = 409;
  busy.detail = { state: "RUNNING", job_id: 17, message: "Job 17 is still running." };
  const h = harness([
    { state: "RUNNING", job_id: 17, message: "Job 17 is running." },
    busy, busy,
    { state: "SUCCESS", job_id: 17, message: "Inventory re-read." },
  ]);
  let reached, resume;
  const observing = new Promise(resolve => { reached = resolve; });
  h.context.window.setTimeout = fn => { resume = fn; reached(); };
  const first = h.context.run("full", "system-a");
  await observing;
  assert.equal(h.context.diskInventorySyncModeAvailability("full").available, true);
  await h.context.run("full", "system-a");
  assert.equal(h.requests.length, 2, "busy response must reuse the existing observer, not start another loop");
  const watcher = h.state.diskInventorySync.observations.get("system-a/17");
  assert.ok(watcher, "a refused duplicate must retain the accepted job watcher");
  await h.context.run("full", "system-a");
  assert.equal(h.requests.length, 3, "a third request must not start a second GET loop");
  assert.equal(h.state.diskInventorySync.observations.get("system-a/17"), watcher);
  resume();
  await first;
  assert.equal(h.requests.length, 4);
  assert.equal(h.requests[3].options.method, "GET");
  assert.match(h.requests[3].url, /job_id=17$/);
  assert.equal(h.statuses.at(-1).kind, "info");
});

test("an older observer completion does not release a newer POST request", async () => {
  let releaseNew;
  const newAnswer = new Promise(resolve => { releaseNew = resolve; });
  const h = harness([
    { state: "UNKNOWN", job_id: 17, message: "Job state is unknown." },
    newAnswer,
    { state: "SUCCESS", job_id: 17, message: "Earlier job finished." },
  ]);
  let reached, resume;
  const observing = new Promise(resolve => { reached = resolve; });
  h.context.window.setTimeout = fn => { resume = fn; reached(); };
  const first = h.context.run("full", "system-a");
  await observing;
  assert.equal(h.context.diskInventorySyncModeAvailability("full").available, true);
  const second = h.context.run("full", "system-a");
  resume();
  await first;
  assert.equal(h.context.diskInventorySyncModeAvailability("full").available, false);
  releaseNew({ state: "SUCCESS", job_id: 18, message: "New job finished." });
  await second;
  assert.equal(h.context.diskInventorySyncModeAvailability("full").available, true);
});

test("a terminal failure on the final bounded observation is still displayed", async () => {
  const answers = Array.from({ length: 720 }, () => ({ state: "RUNNING", job_id: 17, message: "Job running." }));
  answers.push({ state: "FAILED", job_id: 17, message: "Job failed.", error: "Final observation error." });
  const h = harness(answers);
  await h.context.run("full", "system-a");
  assert.equal(h.requests.length, 721);
  assert.equal(h.statuses.at(-1).kind, "error");
  assert.match(h.statuses.at(-1).text, /Final observation error/);
});

test("recent independent outcome entries stay bounded", async () => {
  const h = harness(Array.from({ length: 12 }, (_, index) => ({ state: "SUCCESS", message: `Multipath result ${index}.` })));
  for (let index = 0; index < 12; index += 1) {
    h.state.selectedSystemId = `system-${index}`;
    await h.context.run("multipath", h.state.selectedSystemId);
  }
  assert.equal(h.state.diskInventorySync.statuses.length, 8);
  assert.match(h.statuses.at(-1).text, /Multipath result 11/);
  assert.equal(h.state.diskInventorySync.observations.size, 0);
});

test("completion on another selected system does not replace its inventory", async () => {
  const h = harness([{ state: "SUCCESS", message: "Inventory re-read." }]);
  h.state.selectedSystemId = "system-b";
  await h.context.run("full", "system-a");
  assert.equal(h.refreshes.length, 0);
});
