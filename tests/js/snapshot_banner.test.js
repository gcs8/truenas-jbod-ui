"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

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
    if (lineComment) {
      lineComment = character !== "\n";
      continue;
    }
    if (quote) {
      if (character === "\\") index += 1;
      else if (character === quote) quote = null;
      continue;
    }
    if (character === "/" && APP_SOURCE[index + 1] === "/") {
      lineComment = true;
      continue;
    }
    if (character === "'" || character === '"' || character === "`") {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunction(name, context = {}) {
  const sandbox = vm.createContext({ ...context });
  vm.runInContext(`${functionSource(name)}\nthis.__loaded = ${name};`, sandbox, {
    filename: `${name}.behavior.js`,
  });
  return sandbox.__loaded;
}

function element() {
  const classes = new Set();
  return {
    disabled: false,
    checked: false,
    value: "",
    textContent: "",
    title: "",
    classList: {
      toggle(name, force) {
        if (force === undefined ? !classes.has(name) : force) classes.add(name);
        else classes.delete(name);
      },
      contains(name) {
        return classes.has(name);
      },
    },
  };
}

function refreshHarness(snapshotMode) {
  const controls = {
    refreshButton: element(),
    autoRefreshToggle: element(),
    refreshIntervalSelect: element(),
    autoRefreshField: element(),
    refreshIntervalField: element(),
    inventoryEvidenceDisclosure: element(),
  };
  const renderRefreshControls = loadFunction("renderRefreshControls", {
    ...controls,
    state: { snapshotMode, autoRefresh: true, refreshIntervalSeconds: 30 },
    renderTimingSurfaces() {},
    ensureTimingTick() {},
  });
  renderRefreshControls();
  return controls;
}

test("a saved copy hides the refresh controls and the inventory evidence counters", () => {
  const controls = refreshHarness(true);

  for (const name of ["refreshButton", "autoRefreshField", "refreshIntervalField", "inventoryEvidenceDisclosure"]) {
    assert.equal(controls[name].classList.contains("hidden"), true, `${name} must be hidden in a saved copy`);
  }
  assert.equal(controls.refreshButton.disabled, true);
  assert.equal(controls.autoRefreshToggle.disabled, true);
});

test("the live UI keeps the refresh controls and evidence counters visible", () => {
  const controls = refreshHarness(false);

  for (const name of ["refreshButton", "autoRefreshField", "refreshIntervalField", "inventoryEvidenceDisclosure"]) {
    assert.equal(controls[name].classList.contains("hidden"), false, `${name} must stay visible live`);
  }
  assert.equal(controls.refreshButton.disabled, false);
});

test("renderRefreshControls tolerates a template without the optional wrappers", () => {
  const renderRefreshControls = loadFunction("renderRefreshControls", {
    refreshButton: element(),
    autoRefreshToggle: element(),
    refreshIntervalSelect: element(),
    autoRefreshField: null,
    refreshIntervalField: null,
    inventoryEvidenceDisclosure: null,
    state: { snapshotMode: true, autoRefresh: true, refreshIntervalSeconds: 30 },
    renderTimingSurfaces() {},
    ensureTimingTick() {},
  });

  assert.doesNotThrow(() => renderRefreshControls());
});

test("the snapshot banner explains the capture time in plain words", () => {
  const snapshotGeneratedValue = element();
  const snapshotGeneratedNote = element();
  const renderSnapshotBanner = loadFunction("renderSnapshotBanner", {
    snapshotGeneratedValue,
    snapshotGeneratedNote,
    state: {
      snapshotMode: true,
      snapshotExportMeta: { generated_at: "2026-09-01T10:00:00+00:00" },
      snapshot: {},
    },
    formatTimestamp: (value) => `formatted:${value}`,
    getBrowserTimeZone: () => "Europe/Madrid",
  });

  renderSnapshotBanner();

  assert.equal(snapshotGeneratedValue.textContent, "formatted:2026-09-01T10:00:00+00:00");
  assert.equal(snapshotGeneratedNote.textContent, "Times shown in your local time zone (Europe/Madrid)");
  assert.doesNotMatch(snapshotGeneratedNote.textContent, /viewer|artifact/i);
});

test("the template leads with what the copy is and keeps provenance in a collapsed block", () => {
  const start = TEMPLATE.indexOf('<section class="panel snapshot-banner">');
  const end = TEMPLATE.indexOf("</section>", TEMPLATE.indexOf('<details class="summary-disclosure snapshot-banner-about">', start));
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const banner = TEMPLATE.slice(start, end);

  assert.match(banner, /Demo data/);
  assert.match(banner, /Offline copy/);
  assert.match(banner, /About this copy/);
  assert.doesNotMatch(banner, /Frozen|Explorable|Artifact|artifact|Sanitized|Downsampling|re-rendered/);
  assert.ok(banner.indexOf('id="snapshot-app-version"') > banner.indexOf("snapshot-banner-about"));
  assert.doesNotMatch(banner, /<details[^>]*\sopen[\s>]/);
});
