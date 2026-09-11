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
    Date,
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
    "overrunLabel",
    "retryLabel",
    "collectorStateLabel",
  ]);

  assert.equal(functions.formatDuration(125), "2m 5s");
  assert.equal(functions.formatCount(null), "—");
  assert.equal(functions.formatCount(12), "12");
  assert.equal(functions.formatBytes(1536), "1.5 KiB");
  assert.equal(functions.statusValue("", "unknown"), "unknown");
  assert.equal(functions.collectionInventoryLabel(true), "fresh inventory");
  assert.equal(functions.collectionInventoryLabel(false), "cached inventory");
  assert.equal(functions.collectionInventoryLabel(null), "—");
  assert.equal(functions.collectionDurationLabel(1.25), "1.3s");
  assert.equal(functions.collectionDurationLabel(null), "—");
  assert.equal(functions.overrunLabel(0), "no");
  assert.equal(functions.overrunLabel(2.5), "2.5s");
  assert.equal(functions.retryLabel({}), "no");
  assert.equal(
    functions.retryLabel({ background_consecutive_failures: 3, background_backoff_seconds_remaining: 240 }),
    "in 4m 0s (3 failures so far)"
  );
  assert.equal(
    functions.retryLabel({ background_consecutive_failures: 1, background_backoff_seconds_remaining: 0 }),
    "now (1 failure so far)"
  );
  assert.equal(functions.collectorStateLabel({ collector_running: false }), "Stopped");
  assert.equal(functions.collectorStateLabel({ collector_running: true, collector_starting: true }), "Starting");
  assert.equal(functions.collectorStateLabel({ collector_running: true }), "Running");
});

test("dashboard timestamps render in local time with their age", () => {
  const { formatTimestamp } = loadFunctions(["formatTimestamp", "relativeTimeLabel"]);
  const now = Date.parse("2030-01-01T00:03:00Z");

  const rendered = formatTimestamp("2030-01-01T00:00:00Z", "never", now);
  assert.notEqual(rendered, "2030-01-01T00:00:00Z");
  assert.match(rendered, /\(3 min ago\)$/);
  assert.match(rendered, new RegExp(String(new Date("2030-01-01T00:00:00Z").getFullYear())));
  assert.match(formatTimestamp("2030-01-01T00:02:50Z", "never", now), /\(just now\)$/);
  assert.match(formatTimestamp("2030-01-01T00:20:00Z", "never", now), /\(in 17 min\)$/);
  assert.match(formatTimestamp("2029-12-30T00:03:00Z", "never", now), /\(2 d ago\)$/);
  assert.equal(formatTimestamp(null), "never");
  assert.equal(formatTimestamp(undefined, "not scheduled"), "not scheduled");
  assert.equal(formatTimestamp("not a date"), "not a date");
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

test("dashboard copy avoids engineering words", () => {
  const visibleTemplate = TEMPLATE_SOURCE
    .replace(/{{[\s\S]*?}}/g, " ")
    .replace(/{%[\s\S]*?%}/g, " ")
    .replace(/<[^>]+>/g, " ")
    .toLowerCase();
  for (const word of ["sidecar", "rollup", "scope", "overrun", "deferred", "backed off"]) {
    assert.ok(!visibleTemplate.includes(word), `${word} must not appear in dashboard copy`);
  }
  assert.ok(!SOURCE.includes('"deferred"'));
  assert.ok(!SOURCE.includes("backed off"));
  assert.match(TEMPLATE_SOURCE, /<dl class="status-list">/);
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
  assert.equal(requests[0][0], "/api/history/refresh");
  assert.equal(requests[0][1].method, "POST");
  assert.equal(requests[0][1].headers["Content-Type"], "application/json");
  assert.equal(requests[0][1].body, JSON.stringify({ mode: "fast" }));
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

test("cooldown reply counts down on the Full button only", async () => {
  const status = { textContent: "" };
  const fastButton = { disabled: false, textContent: "Quick refresh" };
  const fullButton = { disabled: false, textContent: "Full refresh" };
  const timers = [];
  const window = {
    setInterval(callback, delay) {
      timers.push({ callback, delay });
      return timers.length;
    },
    clearInterval() {},
  };
  const { runRefresh } = loadFunctions(["runRefresh", "startFullRefreshCooldown", "formatDuration"], {
    status,
    buttons: [fastButton, fullButton],
    fullButton,
    window,
    cooldownTimer: null,
    async fetch() {
      return {
        ok: false,
        status: 429,
        text: async () => JSON.stringify({
          ok: false,
          mode: "full",
          detail: "A full refresh ran recently. Try again in 2 min.",
          retry_after_seconds: 65,
        }),
      };
    },
    renderOverview() {
      assert.fail("a cooled-down refresh must not replace the dashboard overview");
    },
  });

  await runRefresh("full");

  assert.equal(status.textContent, "A full refresh ran recently. Try again in 2 min.");
  assert.equal(fastButton.disabled, false);
  assert.equal(fullButton.disabled, true);
  assert.equal(fullButton.textContent, "Full refresh (1m 5s)");
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 1000);

  timers[0].callback();
  assert.equal(fullButton.textContent, "Full refresh (1m 4s)");
  for (let tick = 0; tick < 64; tick += 1) timers[0].callback();
  assert.equal(fullButton.disabled, false);
  assert.equal(fullButton.textContent, "Full refresh");
});
