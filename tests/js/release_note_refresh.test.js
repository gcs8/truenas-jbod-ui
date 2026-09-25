"use strict";

// #452: the header release note is rendered once by the server. The page must
// poll /api/release-status while the note is still "checking" or "error", so a
// boot-time failure recovers without a reload, and never poll otherwise.

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");

function extract(startMarker, endMarker) {
  const start = APP_SOURCE.indexOf(startMarker);
  const end = APP_SOURCE.indexOf(endMarker, start);
  assert.ok(start !== -1 && end !== -1, `${startMarker} must exist`);
  return APP_SOURCE.slice(start, end);
}

const SOURCE = extract("  const RELEASE_NOTE_POLL_DELAYS_MS", "  function renderAppVersionNote()");

function harness({ initialClass, responses, snapshotMode = false }) {
  const classes = new Set(initialClass.split(" "));
  const note = {
    textContent: "Checking for updates...",
    get className() { return [...classes].join(" "); },
    set className(value) { classes.clear(); value.split(" ").forEach((name) => classes.add(name)); },
    classList: { contains: (name) => classes.has(name) },
  };
  const timers = [];
  const fetched = [];
  const context = {
    appVersionNote: note,
    state: { snapshotMode, appUpdated: false },
    window: { setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; } },
    fetchJson: async (url) => {
      fetched.push(url);
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    setTextIfChanged: (element, text) => { element.textContent = text; },
    String,
  };
  vm.createContext(context);
  vm.runInContext(`${SOURCE}\nthis.scheduleReleaseNoteRefresh = scheduleReleaseNoteRefresh;`, context);
  return { context, note, timers, fetched };
}

async function runNextTimer(timers) {
  const timer = timers.shift();
  assert.ok(timer, "a refresh must be scheduled");
  timer.callback();
  await new Promise((resolve) => setImmediate(resolve));
  return timer.delay;
}

test("a boot-time failure recovers without a reload", async () => {
  const { context, note, timers, fetched } = harness({
    initialClass: "meta-note version-note version-note-error",
    responses: [new Error("offline"), { status: "current", summary: "Up to date" }],
  });
  context.scheduleReleaseNoteRefresh();
  assert.equal(await runNextTimer(timers), 20000);
  assert.equal(await runNextTimer(timers), 75000);
  assert.deepEqual(fetched, ["/api/release-status", "/api/release-status"]);
  assert.equal(note.textContent, "Up to date");
  assert.equal(note.className, "meta-note version-note version-note-current");
  assert.equal(timers.length, 0, "stop polling once the check succeeded");
});

test("a note that is already settled is never polled", () => {
  for (const status of ["current", "update-available", "disabled", "unknown"]) {
    const { context, timers } = harness({ initialClass: `meta-note version-note version-note-${status}`, responses: [] });
    context.scheduleReleaseNoteRefresh();
    assert.equal(timers.length, 0, status);
  }
});

test("polling is bounded and skipped for offline snapshots", async () => {
  const stuck = harness({
    initialClass: "meta-note version-note version-note-checking",
    responses: [{ status: "checking", summary: "Checking for updates..." }, { status: "error", summary: "Could not check for updates" }, { status: "error", summary: "Could not check for updates" }],
  });
  stuck.context.scheduleReleaseNoteRefresh();
  const delays = [];
  while (stuck.timers.length) delays.push(await runNextTimer(stuck.timers));
  assert.deepEqual(delays, [20000, 75000, 320000]);
  assert.equal(stuck.note.textContent, "Could not check for updates");

  const snapshot = harness({ initialClass: "meta-note version-note version-note-checking", responses: [], snapshotMode: true });
  snapshot.context.scheduleReleaseNoteRefresh();
  assert.equal(snapshot.timers.length, 0);
});

test("startup schedules the release note refresh", () => {
  assert.match(APP_SOURCE, /\n  scheduleReleaseNoteRefresh\(\);\n/);
});
