"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../history_service/static/dashboard.js");
const SOURCE = fs.existsSync(SCRIPT_PATH) ? fs.readFileSync(SCRIPT_PATH, "utf8") : "";
const TEMPLATE_PATH = path.resolve(__dirname, "../../history_service/templates/dashboard.html");
const TEMPLATE_SOURCE = fs.existsSync(TEMPLATE_PATH) ? fs.readFileSync(TEMPLATE_PATH, "utf8") : "";

function functionSource(name) {
  const patterns = [`async function ${name}(`, `function ${name}(`];
  const start = patterns.reduce((found, pattern) => {
    const index = SOURCE.indexOf(pattern);
    return found === -1 || (index !== -1 && index < found) ? index : found;
  }, -1);
  assert.notEqual(start, -1, `function ${name} must exist`);
  const parametersEnd = SOURCE.indexOf(")", start);
  const bodyStart = SOURCE.indexOf("{", parametersEnd);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  for (let index = bodyStart; index < SOURCE.length; index += 1) {
    const character = SOURCE[index];
    const next = SOURCE[index + 1];
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
      if (depth === 0) return SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} must have a complete body`);
}

function loadFunctions(names, bindings = {}) {
  const context = vm.createContext({
    Boolean,
    Error,
    JSON,
    Math,
    Number,
    String,
    ...bindings,
  });
  vm.runInContext(
    `${names.map(functionSource).join("\n")}\nglobalThis.__tested = { ${names.join(", ")} };`,
    context,
    { filename: "history-dashboard.js" }
  );
  return context.__tested;
}

test("history dashboard JavaScript is an extracted static asset", () => {
  assert.ok(SOURCE, "history_service/static/dashboard.js must exist");
});

test("dashboard formatters preserve count, byte, duration, and status labels", () => {
  const functions = loadFunctions([
    "formatDuration",
    "formatTimestamp",
    "formatCount",
    "formatBytes",
    "statusValue",
    "collectionInventoryLabel",
    "collectionDurationLabel",
    "backoffLabel",
  ]);

  assert.equal(functions.formatDuration(125), "2m 5s");
  assert.equal(functions.formatCount(null), "-");
  assert.equal(functions.formatCount(undefined), "-");
  assert.equal(functions.formatCount(0), "0");
  assert.equal(functions.formatTimestamp(null), "never");
  assert.equal(functions.formatTimestamp("bad"), "not recorded");
  assert.equal(functions.formatTimestamp("", "not scheduled"), "not scheduled");
  assert.equal(functions.formatTimestamp("2026-09-09T12:00:00Z"), new Date("2026-09-09T12:00:00Z").toLocaleString());
  assert.equal(functions.collectionDurationLabel(null), "not recorded");
  assert.equal(functions.formatCount(12, true), "~12");
  assert.equal(functions.formatBytes(1536), "1.5 KiB");
  assert.equal(functions.statusValue("", "unknown"), "unknown");
  assert.equal(functions.collectionInventoryLabel(true), "fresh inventory");
  assert.equal(functions.collectionInventoryLabel(false), "cached inventory");
  assert.equal(functions.collectionInventoryLabel(null), "not recorded");
  assert.equal(functions.collectionDurationLabel(1.25), "1.3s");
  assert.equal(functions.backoffLabel(1.2), "2s remaining");
});

test("dashboard reads the script-safe JSON bootstrap block", () => {
  const payload = { collector: { collection_activity: "</script>" }, counts: { tracked_slots: 1 } };
  const { readInitialOverview } = loadFunctions(["readInitialOverview"], {
    document: {
      getElementById(id) {
        assert.equal(id, "history-dashboard-bootstrap");
        return { textContent: JSON.stringify(payload) };
      },
    },
  });

  assert.deepEqual(readInitialOverview(), payload);
});

test("dashboard omits collector locations while retaining approved status updates", () => {
  const publicSources = `${TEMPLATE_SOURCE}\n${SOURCE}`;
  for (const forbidden of [
    "status-source-base-url",
    "status-sqlite-path",
    ".source_base_url",
    ".sqlite_path",
  ]) {
    assert.ok(!publicSources.includes(forbidden), `${forbidden} must not remain in dashboard sources`);
  }
  assert.match(SOURCE, /collector\.last_inventory_at/);
  assert.match(SOURCE, /collector\.last_fast_metrics_at/);
  assert.match(SOURCE, /collector\.last_slow_metrics_at/);
  assert.match(SOURCE, /collector\.last_error/);
  assert.match(TEMPLATE_SOURCE, /status-last-inventory-at/);
  assert.match(TEMPLATE_SOURCE, /status-last-error/);
  assert.match(TEMPLATE_SOURCE, /db-size-value/);
});

test("dashboard does not repaint the server-rendered overview during bootstrap", () => {
  assert.match(SOURCE, /renderCollectorBanner\(initialCollectorStatus\)/);
  assert.doesNotMatch(SOURCE, /renderOverview\(initial(?:OverviewPayload|CollectorStatus)\)/);
});

// Refresh success/refusal and polling lifecycle are exercised through the
// complete asset in history_dashboard_polling.test.js.