"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SCRIPT_PATH = path.resolve(__dirname, "../../history_service/static/dashboard.js");
const SOURCE = fs.existsSync(SCRIPT_PATH) ? fs.readFileSync(SCRIPT_PATH, "utf8") : "";

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
    "formatCount",
    "formatBytes",
    "statusValue",
    "collectionInventoryLabel",
    "collectionDurationLabel",
    "backoffLabel",
  ]);

  assert.equal(functions.formatDuration(125), "2m 5s");
  assert.equal(functions.formatCount(null), "deferred");
  assert.equal(functions.formatCount(12, true), "~12");
  assert.equal(functions.formatBytes(1536), "1.5 KiB");
  assert.equal(functions.statusValue("", "unknown"), "unknown");
  assert.equal(functions.collectionInventoryLabel(true), "forced");
  assert.equal(functions.collectionInventoryLabel(false), "cached");
  assert.equal(functions.collectionInventoryLabel(null), "not recorded");
  assert.equal(functions.collectionDurationLabel(1.25), "1.3s");
  assert.equal(functions.backoffLabel(1.2), "2s remaining");
});

test("dashboard reads the script-safe JSON bootstrap block", () => {
  const payload = { collector: { source_base_url: "</script>" }, counts: { tracked_slots: 1 } };
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

test("dashboard does not repaint the server-rendered overview during bootstrap", () => {
  assert.match(SOURCE, /renderCollectorBanner\(initialCollectorStatus\)/);
  assert.doesNotMatch(SOURCE, /renderOverview\(initial(?:OverviewPayload|CollectorStatus)\)/);
});

test("refresh preserves success payload rendering and button state", async () => {
  const status = { textContent: "" };
  const buttons = [{ disabled: false }, { disabled: false }];
  const rendered = [];
  const requests = [];
  const payload = { ok: true, detail: "History fast refresh completed.", counts: {} };
  const { runRefresh } = loadFunctions(["runRefresh"], {
    status,
    buttons,
    encodeURIComponent,
    async fetch(url, options) {
      requests.push([url, options]);
      return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
    },
    renderOverview(value) {
      rendered.push(value);
    },
  });

  await runRefresh("fast");

  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], "/api/history/refresh?mode=fast");
  assert.equal(requests[0][1].method, "POST");
  assert.equal(status.textContent, "History fast refresh completed.");
  assert.deepEqual(rendered, [payload]);
  assert.deepEqual(buttons.map((button) => button.disabled), [false, false]);
});

test("refresh preserves structured HTTP error detail and restores buttons", async () => {
  const status = { textContent: "" };
  const buttons = [{ disabled: false }, { disabled: false }];
  const { runRefresh } = loadFunctions(["runRefresh"], {
    status,
    buttons,
    encodeURIComponent,
    async fetch() {
      return {
        ok: false,
        status: 409,
        text: async () => JSON.stringify({ ok: false, detail: "History collection already running." }),
      };
    },
    renderOverview() {
      assert.fail("failed refreshes must not replace the dashboard overview");
    },
  });

  await runRefresh("full");

  assert.equal(status.textContent, "Refresh failed: History collection already running.");
  assert.deepEqual(buttons.map((button) => button.disabled), [false, false]);
});