"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "../..");
const APP_SOURCE = fs.readFileSync(path.join(ROOT, "app/static/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "app/templates/index.html"), "utf8");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const bodyStart = source.indexOf("{", start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated function ${name}`);
}

function loadFunctions(names, context = {}) {
  const sandbox = { ...context };
  vm.createContext(sandbox);
  vm.runInContext(`${names.map((name) => functionSource(APP_SOURCE, name)).join("\n")}\nthis.loaded = { ${names.join(", ")} };`, sandbox);
  return sandbox.loaded;
}

function chip() {
  return { className: "status-chip", textContent: "", title: "" };
}

function renderStatusWith(sources) {
  const apiStatusChip = chip();
  const sshStatusChip = chip();
  const historyStatusChip = chip();
  const { renderStatus } = loadFunctions(["renderStatus", "snapshotSshStatus"], {
    state: {
      snapshotMode: false,
      snapshot: { sources, last_updated: "2026-09-01T12:00:00Z" },
      history: { configured: false },
    },
    snapshotStatusChip: chip(),
    apiStatusChip,
    sshStatusChip,
    historyStatusChip,
    lastUpdated: { textContent: "", title: "" },
    formatTimestamp: (value) => String(value),
    renderTimezoneLabel() {},
    renderSnapshotBanner() {},
    syncLocation() {},
  });
  renderStatus();
  return { apiStatusChip, sshStatusChip };
}

test("status chips use plain labels and carry the source message as their tooltip", () => {
  const { apiStatusChip, sshStatusChip } = renderStatusWith({
    api: { ok: false, message: "TrueNAS API is unreachable at https://nas.example.test." },
    ssh: { enabled: true, ok: true, message: "SSH probe completed." },
  });
  assert.equal(apiStatusChip.textContent, "TrueNAS API: error");
  assert.equal(apiStatusChip.className, "status-chip error");
  assert.equal(apiStatusChip.title, "TrueNAS API is unreachable at https://nas.example.test.");
  assert.equal(sshStatusChip.textContent, "SSH: OK");
  assert.equal(sshStatusChip.className, "status-chip ok");
  assert.equal(sshStatusChip.title, "SSH probe completed.");
});

test("SSH that was never turned on is shown neutral, not as a degraded state", () => {
  const { apiStatusChip, sshStatusChip } = renderStatusWith({
    api: { ok: true, message: "Connected." },
    ssh: { enabled: false, ok: true, message: "SSH is not configured for this system." },
  });
  assert.equal(apiStatusChip.textContent, "TrueNAS API: OK");
  assert.equal(sshStatusChip.textContent, "SSH: off");
  assert.equal(sshStatusChip.className, "status-chip");
  assert.equal(sshStatusChip.title, "SSH is not configured for this system.");
  assert.doesNotMatch(TEMPLATE, />(API|HIST)<\/div>/);
});

test("cache countdown chips only render when the UI Timing panel is enabled", () => {
  function fakeStrip() {
    const hidden = [];
    return {
      hidden,
      childElementCount: 0,
      innerHTML: "",
      classList: { toggle(name, force) { hidden.push([name, force]); } },
      querySelectorAll() { return []; },
      querySelector() { return null; },
    };
  }
  const definitions = () => [{ key: "snapshot", label: "Snapshot", ttlSeconds: 30, startMs: 0, title: "" }];

  const hiddenStrip = fakeStrip();
  loadFunctions(["renderCacheTimingChips"], {
    cacheTimingChips: hiddenStrip,
    state: { snapshotMode: false, uiPerf: { enabled: false } },
    cacheTimingDefinitions: definitions,
  }).renderCacheTimingChips();
  assert.deepEqual(hiddenStrip.hidden, [["hidden", true]]);
  assert.equal(hiddenStrip.innerHTML, "");

  const shownStrip = fakeStrip();
  loadFunctions(["renderCacheTimingChips"], {
    cacheTimingChips: shownStrip,
    state: { snapshotMode: false, uiPerf: { enabled: true } },
    cacheTimingDefinitions: definitions,
    escapeHtml: (value) => String(value),
    cacheCountdownParts: () => ({ label: "30s", expired: false, progress: 1 }),
    setTextIfChanged() {},
    setBarWidthIfChanged() {},
  }).renderCacheTimingChips();
  assert.deepEqual(shownStrip.hidden, [["hidden", false]]);
  assert.match(shownStrip.innerHTML, /data-cache-timing-key="snapshot"/);
});

test("header, bay status, and status-line copy avoids developer vocabulary", () => {
  const banned = /enrichment|calibrat|evidence|payload|gmultipath|middleware|sanitized|rollup|first-pass|artifact/i;
  for (const name of [
    "buildViewProfile",
    "mappingHealthSourceNote",
    "summarizeMappingHealth",
    "renderStatus",
    "heatmapMetricContextText",
    "refreshStatusMessage",
    "diskInventorySyncModeSpec",
    "buildEstimateAdvice",
  ]) {
    assert.doesNotMatch(functionSource(APP_SOURCE, name), banned, `${name} still uses developer vocabulary`);
  }
  assert.match(TEMPLATE, /<summary>Summary<\/summary>/);
  assert.match(TEMPLATE, /<h2>Bay assignment<\/h2>/);
  assert.doesNotMatch(TEMPLATE, /Calibration Mapping|Inventory evidence counters|Prefill From Slot|Evidence source and snapshot freshness/);
});
