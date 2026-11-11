"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const root = path.resolve(__dirname, "../..");
const revision = process.env.FABRIC_LIFECYCLE_SOURCE_REVISION;
const source = revision
  ? execFileSync("git", ["show", `${revision}:app/static/sas_fabric_view.js`], { cwd: root, encoding: "utf8" })
  : fs.readFileSync(path.join(root, "app/static/sas_fabric_view.js"), "utf8");
const NOW = Date.parse("2026-11-11T12:00:00Z");

// Real production functions and lifecycle listeners. Only DOM, wall clock,
// timers and the network entry point are synthetic; no real sleeps or I/O.
function lifecycle({ ageSeconds = 180 } = {}) {
  let now = NOW;
  let serial = 0;
  let fired = 0;
  const timers = new Map();
  const listeners = new Map();
  const reads = [];
  const elements = new Map();
  class ClockDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const window = {
    location: { search: "" },
    SAS_FABRIC_BOOTSTRAP: {
      snapshot: { last_updated: new Date(NOW - ageSeconds * 1000).toISOString(), refresh_interval_seconds: 30 },
      fabric: { available: true, traces: [], snapshot_cache_state: "stale-hit", source_cache_state: "stale-hit" },
    },
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener(name, callback) { listeners.set(name, callback); },
  };
  const sandbox = vm.createContext({
    URLSearchParams, Date: ClockDate, window,
    document: {
      querySelectorAll: () => [],
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { textContent: "", dataset: {}, className: "" });
        return elements.get(id);
      },
    },
  });
  const functionsEnd = source.indexOf("  elements.modeButtons.forEach((button) => {", source.indexOf("  function render()"));
  const listenersStart = source.indexOf('  if (typeof window.addEventListener === "function")');
  const listenersEnd = source.indexOf("  ensureSelectionForMode(state.fabric);", listenersStart);
  assert.ok(functionsEnd > 0 && listenersStart > functionsEnd && listenersEnd > listenersStart);
  vm.runInContext(`${source.slice(0, functionsEnd)}
    refreshFabric = (...args) => window.recordRead(...args);
    ${source.slice(listenersStart, listenersEnd)}
    window.lifecycle = { state, renderStatus, scheduleStaleRefetch };
  })();`, sandbox);
  window.recordRead = (...args) => reads.push(args);
  const ui = window.lifecycle;
  return {
    ...ui, reads, timers,
    chip: elements.get("fabric-api-chip"), status: elements.get("fabric-status-text"),
    fired: () => fired,
    emit(name, persisted = true) { listeners.get(name)?.({ persisted }); },
    elapse(ms) { now += ms; }, // time spent frozen in bfcache, without callbacks
    tick(ms) {
      const end = now + ms;
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at;
        timers.delete(next[0]);
        fired += 1;
        assert.ok(fired < 20, "status updates must not become a timer loop");
        next[1].callback();
      }
      now = end;
    },
  };
}

test("a second young stale-hit ages out with one local timer at the evidence deadline", () => {
  const ui = lifecycle();
  ui.renderStatus();
  ui.tick(15_000);
  // The delayed response returns exactly the same evidence and re-renders.
  ui.renderStatus();
  assert.equal(ui.timers.size, 1);
  ui.tick(105_000);
  assert.match(ui.chip.textContent, /OK$/);
  ui.tick(1);
  assert.match(ui.chip.textContent, /STALE$/);
  assert.equal(ui.status.dataset.tone, "error");
  assert.equal(ui.fired(), 1);
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.reads.length, 0);
});

test("tolerated future skew schedules one check at the absolute age-out moment", () => {
  const ui = lifecycle({ ageSeconds: -30 });
  ui.renderStatus();
  ui.tick(330_000);
  assert.match(ui.chip.textContent, /OK$/);
  assert.equal(ui.fired(), 0);
  ui.tick(1);
  assert.match(ui.chip.textContent, /STALE$/);
  assert.equal(ui.fired(), 1);
  assert.equal(ui.timers.size, 0);
});

test("persisted pageshow immediately re-evaluates old evidence and restores one read", () => {
  const ui = lifecycle();
  ui.renderStatus();
  ui.scheduleStaleRefetch(ui.state.refreshRequestToken);
  ui.emit("pagehide");
  assert.equal(ui.timers.size, 0);
  ui.elapse(180_000);
  ui.emit("pageshow");
  assert.match(ui.chip.textContent, /STALE$/);
  assert.equal(ui.status.dataset.tone, "error");
  ui.tick(15_000);
  assert.equal(ui.reads.length, 1);
  assert.equal(ui.reads[0][0], false);
  assert.equal(ui.reads[0][1].revalidation, true);
  ui.tick(600_000);
  assert.equal(ui.reads.length, 1);
});

test("young persisted restore rearms the age check; ordinary pageshow does nothing", () => {
  const ui = lifecycle();
  ui.renderStatus();
  ui.emit("pagehide");
  ui.elapse(60_000);
  ui.emit("pageshow", false);
  assert.equal(ui.timers.size, 0);
  ui.emit("pageshow");
  assert.match(ui.chip.textContent, /OK$/);
  ui.tick(60_001);
  assert.match(ui.chip.textContent, /STALE$/);
  assert.equal(ui.reads.length, 1);
});

test("late refresh completion after pagehide cannot resurrect either timer", () => {
  const ui = lifecycle();
  ui.emit("pagehide");
  ui.renderStatus();
  ui.scheduleStaleRefetch(ui.state.refreshRequestToken);
  assert.equal(ui.timers.size, 0);
  ui.tick(600_000);
  assert.equal(ui.reads.length, 0);
});

for (const guard of ["loading", "aliasEditObjectId", "newer request"]) {
  test(`one delayed read respects ${guard} without changing request ownership`, () => {
    const ui = lifecycle();
    ui.scheduleStaleRefetch(ui.state.refreshRequestToken);
    if (guard === "newer request") ui.state.refreshRequestToken += 1;
    else ui.state[guard] = true;
    const token = ui.state.refreshRequestToken;
    ui.tick(600_000);
    assert.equal(ui.reads.length, 0);
    assert.equal(ui.state.refreshRequestToken, token);
    assert.equal(ui.timers.size, 0);
  });
}
