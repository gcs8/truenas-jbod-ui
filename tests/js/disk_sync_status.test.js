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
  const h = harness([{ state: "UNKNOWN", job_id: 17, message: "Previous job state is unknown. You can start a new sync." }]);
  await h.context.run("full", "system-a");
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.equal(h.state.diskInventorySync.inFlight, false);
  assert.equal(h.requests.length, 1);
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
  ]);
  await h.context.run("full", "system-a");
  assert.match(h.statuses.at(-1).text, /state is unknown/);
  assert.equal(h.statuses.at(-1).kind, "info");
  assert.equal(h.requests.filter(row => row.options.method === "POST").length, 1);
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

test("completion on another selected system does not replace its inventory", async () => {
  const h = harness([{ state: "SUCCESS", message: "Inventory re-read." }]);
  h.state.selectedSystemId = "system-b";
  await h.context.run("full", "system-a");
  assert.equal(h.refreshes.length, 0);
});
