"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");
const STYLES = fs.readFileSync(path.join(ROOT, "app/static/style.css"), "utf8");

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
  let lineComment = false;
  let blockComment = false;
  for (let index = bodyStart; index < source.length; index += 1) {
    const character = source[index];
    const next = source[index + 1];
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (blockComment) {
      if (character === "*" && next === "/") {
        blockComment = false;
        index += 1;
      }
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
    if (character === "/" && next === "*") {
      blockComment = true;
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
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

const FUNCTION_NAMES = [
  "diskInventorySyncPlatformSupported",
  "diskInventorySyncModeSpec",
  "diskInventorySyncModeAvailability",
  "renderDiskInventorySyncControls",
  "disarmDiskInventorySync",
  "armDiskInventorySync",
  "handleDiskInventorySyncClick",
];

function loadFunctions(context) {
  const sandbox = vm.createContext(context);
  const source = FUNCTION_NAMES.map((name) => functionSource(APP_SOURCE, name)).join("\n");
  const exports = FUNCTION_NAMES.map((name) => `this.__${name} = ${name};`).join("\n");
  vm.runInContext(`${source}\n${exports}`, sandbox, { filename: "disk-inventory-sync.behavior.js" });
  return Object.fromEntries(FUNCTION_NAMES.map((name) => [name, sandbox[`__${name}`]]));
}

function classList(initial = []) {
  const values = new Set(initial);
  return {
    values,
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    toggle(name, force) {
      if (force === undefined ? !values.has(name) : force) values.add(name);
      else values.delete(name);
    },
    contains(name) { return values.has(name); },
  };
}

function button(mode) {
  return { dataset: { diskInventorySyncMode: mode }, classList: classList([]), disabled: false, title: "", textContent: "" };
}

function harness({ platform = "core", sshEnabled = true, snapshotMode = false } = {}) {
  const timers = [];
  const cleared = [];
  const statuses = [];
  const runs = [];
  const state = {
    snapshotMode,
    snapshot: { sources: { ssh: { enabled: sshEnabled, ok: true } } },
    diskInventorySync: { armedMode: null, armTimerId: null, inFlight: false },
  };
  const buttons = { multipath: button("multipath"), full: button("full") };
  const controls = { classList: classList(["hidden"]) };
  const hint = { classList: classList(["hidden"]), textContent: "" };
  const fns = loadFunctions({
    state,
    currentPlatform: () => platform,
    diskInventorySyncControls: controls,
    diskInventorySyncHint: hint,
    diskInventorySyncButtons: [buttons.multipath, buttons.full],
    setStatus: (message, tone = "info") => statuses.push({ message, tone }),
    runDiskInventorySync: (mode) => { runs.push(mode); return Promise.resolve(); },
    window: {
      setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
      clearTimeout(id) { cleared.push(id); },
    },
  });
  return { fns, state, buttons, controls, hint, timers, cleared, statuses, runs };
}

test("controls stay hidden in snapshot mode and on platforms without the TrueNAS middleware", () => {
  const snapshot = harness({ snapshotMode: true });
  snapshot.fns.renderDiskInventorySyncControls();
  assert.equal(snapshot.controls.classList.contains("hidden"), true);

  const linux = harness({ platform: "linux" });
  linux.fns.renderDiskInventorySyncControls();
  assert.equal(linux.controls.classList.contains("hidden"), true);

  const core = harness({ platform: "core" });
  core.fns.renderDiskInventorySyncControls();
  assert.equal(core.controls.classList.contains("hidden"), false);
  assert.equal(core.buttons.multipath.classList.contains("hidden"), false);
  assert.equal(core.buttons.full.classList.contains("hidden"), false);
  assert.equal(core.buttons.multipath.textContent, "Sync multipath table");
  assert.equal(core.buttons.full.textContent, "Full disk sync");

  const scale = harness({ platform: "scale" });
  scale.fns.renderDiskInventorySyncControls();
  assert.equal(scale.controls.classList.contains("hidden"), false);
  assert.equal(scale.buttons.multipath.classList.contains("hidden"), true, "multipath is CORE only");
  assert.equal(scale.buttons.full.disabled, false);
});

test("a snapshot click never arms or runs anything", () => {
  const h = harness({ snapshotMode: true });
  h.fns.handleDiskInventorySyncClick("full");
  assert.equal(h.state.diskInventorySync.armedMode, null);
  assert.deepEqual(h.runs, []);
  assert.equal(h.statuses.at(-1).tone, "error");
});

test("first click arms with the explanation, second click runs exactly once and disarms", () => {
  const h = harness({ platform: "core" });
  h.fns.renderDiskInventorySyncControls();

  h.fns.handleDiskInventorySyncClick("multipath");
  assert.equal(h.state.diskInventorySync.armedMode, "multipath");
  assert.equal(h.buttons.multipath.textContent, "Confirm sync");
  assert.equal(h.buttons.multipath.dataset.armed, "true");
  assert.equal(h.buttons.full.textContent, "Full disk sync");
  assert.equal(h.hint.classList.contains("hidden"), false);
  assert.match(h.hint.textContent, /disk\.multipath_sync/);
  assert.match(h.hint.textContent, /Pools and data are not touched/);
  assert.equal(h.timers.length, 1);
  assert.equal(h.timers[0].ms, 6000);
  assert.deepEqual(h.runs, []);

  h.fns.handleDiskInventorySyncClick("multipath");
  assert.deepEqual(h.runs, ["multipath"]);
  assert.equal(h.state.diskInventorySync.armedMode, null);
  assert.equal(h.buttons.multipath.textContent, "Sync multipath table");
  assert.equal(h.buttons.multipath.dataset.armed, "false");
  assert.equal(h.hint.classList.contains("hidden"), true);
  assert.deepEqual(h.cleared, [1], "the arm timer is cleared when the sync runs");
});

test("arming the other mode re-arms instead of running", () => {
  const h = harness({ platform: "core" });
  h.fns.handleDiskInventorySyncClick("multipath");
  h.fns.handleDiskInventorySyncClick("full");
  assert.equal(h.state.diskInventorySync.armedMode, "full");
  assert.deepEqual(h.runs, []);
  assert.match(h.hint.textContent, /disk\.sync_all/);
  assert.equal(h.buttons.multipath.textContent, "Sync multipath table");
  assert.equal(h.buttons.full.textContent, "Confirm sync");
});

test("the arm window expiring or an outside disarm resets without running", () => {
  const h = harness({ platform: "core" });
  h.fns.handleDiskInventorySyncClick("full");
  assert.equal(h.state.diskInventorySync.armedMode, "full");
  h.timers[0].fn();
  assert.equal(h.state.diskInventorySync.armedMode, null);
  assert.equal(h.state.diskInventorySync.armTimerId, null);
  assert.equal(h.buttons.full.textContent, "Full disk sync");
  assert.deepEqual(h.runs, []);

  h.fns.handleDiskInventorySyncClick("full");
  h.fns.disarmDiskInventorySync();
  assert.equal(h.state.diskInventorySync.armedMode, null);
  assert.deepEqual(h.runs, []);
  assert.equal(h.hint.classList.contains("hidden"), true);
});

test("buttons are disabled with a title reason for SSH off, in-flight sync, and unsupported mode", () => {
  const sshOff = harness({ platform: "core", sshEnabled: false });
  sshOff.fns.renderDiskInventorySyncControls();
  assert.equal(sshOff.buttons.full.disabled, true);
  assert.match(sshOff.buttons.full.title, /SSH is disabled for this system/);
  sshOff.fns.handleDiskInventorySyncClick("full");
  assert.equal(sshOff.state.diskInventorySync.armedMode, null);
  assert.deepEqual(sshOff.runs, []);
  assert.match(sshOff.statuses.at(-1).message, /SSH is disabled/);

  const busy = harness({ platform: "core" });
  busy.state.diskInventorySync.inFlight = true;
  busy.fns.renderDiskInventorySyncControls();
  assert.equal(busy.buttons.multipath.disabled, true);
  assert.match(busy.buttons.multipath.title, /already running/);

  const scale = harness({ platform: "scale" });
  const availability = scale.fns.diskInventorySyncModeAvailability("multipath");
  assert.equal(availability.available, false);
  assert.match(availability.reason, /only available on TrueNAS CORE/);
  scale.fns.renderDiskInventorySyncControls();
  assert.equal(scale.buttons.full.disabled, false);
  assert.match(scale.buttons.full.title, /disk\.sync_all/);
});

test("template places the action group in the enclosure header beside the view toggles", () => {
  const actionsStart = TEMPLATE.indexOf('<div class="panel-header-actions">');
  const groupStart = TEMPLATE.indexOf('id="disk-inventory-sync-controls"');
  const fabricLink = TEMPLATE.indexOf('id="sas-fabric-view-link"');
  const heatmapControls = TEMPLATE.indexOf('id="heatmap-controls"');
  const legend = TEMPLATE.indexOf('<div class="legend">');
  assert.notEqual(actionsStart, -1);
  assert.ok(groupStart > actionsStart, "group must be inside the header actions");
  assert.ok(groupStart > fabricLink, "group sits right of the Storage Fabric toggle");
  assert.ok(groupStart < heatmapControls, "group precedes the heat map controls");
  assert.ok(groupStart < legend, "group precedes the legend");
  assert.ok(TEMPLATE.indexOf('id="detail-led-controls"') > legend, "group is not in the Slot Details panel");
  assert.match(TEMPLATE, /id="disk-inventory-sync-controls"[^>]*class="disk-inventory-sync-controls hidden"/);
  assert.match(TEMPLATE, /role="group" aria-label="TrueNAS disk inventory"/);
  assert.match(TEMPLATE, /data-disk-inventory-sync-mode="multipath"[^>]*>\s*Sync multipath table/);
  assert.match(TEMPLATE, /data-disk-inventory-sync-mode="full"[^>]*>\s*Full disk sync/);
  assert.match(TEMPLATE, /id="disk-inventory-sync-hint"/);
  assert.match(STYLES, /\.disk-inventory-sync-controls\s*\{[^}]*flex-wrap:\s*wrap/);
  assert.match(STYLES, /\.disk-inventory-sync-hint\s*\{[^}]*flex-basis:\s*100%/);
});

test("app wires the buttons, the outside-click disarm, and the guarded route", () => {
  assert.match(APP_SOURCE, /querySelectorAll\("\[data-disk-inventory-sync-mode\]"\)/);
  assert.match(APP_SOURCE, /handleDiskInventorySyncClick\(button\.dataset\.diskInventorySyncMode\)/);
  assert.match(APP_SOURCE, /diskInventorySyncControls\.contains\(event\.target\)/);
  assert.match(APP_SOURCE, /\/api\/systems\/\$\{encodeURIComponent\(systemId\)\}\/disk-inventory-sync/);
  assert.match(APP_SOURCE, /JSON\.stringify\(\{ mode, confirm: true \}\)/);
  const renderAll = functionSource(APP_SOURCE, "renderAll");
  assert.match(renderAll, /renderDiskInventorySyncControls\(\)/);
});
