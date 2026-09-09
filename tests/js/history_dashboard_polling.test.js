"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../../history_service/static/dashboard.js"), "utf8");

async function flush() {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

function dashboard({ hidden = false, buttons = true, initial = { collector_running: true } } = {}) {
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const requests = [];
  const listeners = {};
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      textContent: id === "collector-state-value" ? "Running" : "",
      disabled: false, hidden: false, classList: { toggle() {} },
      addEventListener(event, callback) { this[event] = callback; },
      setAttribute(name, value) { this[name] = value; },
      removeAttribute(name) { delete this[name]; },
      replaceChildren(...children) { this.children = children; }, appendChild() {},
    });
    return elements.get(id);
  }
  const document = {
    hidden,
    getElementById(id) {
      if (id === "history-dashboard-bootstrap") return { textContent: JSON.stringify(initial) };
      if (!buttons && /history-refresh-(fast|full|status)/.test(id)) return null;
      return element(id);
    },
    querySelectorAll() { return []; },
    createElement() { return { textContent: "", children: [], setAttribute(name, value) { this[name] = value; }, appendChild(child) { this.children.push(child); } }; },
    addEventListener(event, callback) { listeners[event] = callback; },
  };
  const window = {
    setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, at: now + ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, at: now + ms, interval: ms }); return id; },
  };
  vm.runInNewContext(source, {
    document, window, AbortController, console,
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject }));
    },
  });
  return {
    requests, element, poll: window.__HISTORY_DASHBOARD_POLL,
    async advance(ms) {
      const end = now + ms;
      while (true) {
        const due = [...timers].filter(([, value]) => value.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at;
        timers.delete(due[0]);
        if (due[1].interval) timers.set(due[0], { ...due[1], at: now + due[1].interval });
        due[1].fn();
        await flush();
      }
      now = end;
      await flush();
    },
    async visibility(value) { document.hidden = value; listeners.visibilitychange?.(); await flush(); },
    async reply(index, payload, code = 200) {
      requests[index].resolve({ ok: code < 400, status: code, json: async () => payload, text: async () => JSON.stringify(payload) });
      await flush();
    },
    async click(mode) { element(`history-refresh-${mode}`).click(); await flush(); },
  };
}

test("presentation formats initial and polled times and does not invent startup", async () => {
  const stamp = "2026-09-09T12:00:00+00:00";
  const d = dashboard({ initial: { collector_running: true, last_inventory_at: stamp } });
  assert.equal(d.element("status-last-inventory-at").textContent, new Date(stamp).toLocaleString());
  assert.equal(d.element("collector-state-value").textContent, "Running");
  d.poll.pollCollectorStatus();
  await d.reply(0, { collector_running: false, last_inventory_at: "bad", last_fast_metrics_at: null, last_background_overrun_seconds: 0 });
  assert.equal(d.element("status-last-inventory-at").textContent, "not recorded");
  assert.equal(d.element("status-last-fast-metrics-at").textContent, "never");
  assert.equal(d.element("status-last-background-overrun").textContent, "no");
  assert.equal(d.element("collector-state-value").textContent, "Stopped");
});

test("missing counts stay distinct from zero with accessible explanations", async () => {
  const d = dashboard();
  d.poll.pollOverviewStatus();
  await d.reply(0, { collector: { collector_running: true }, counts: { event_count: 0 }, counts_exact: true,
    scopes: [{ system_label: "-", event_count: 0, last_seen_at: "2026-09-09T12:00:00Z" }] });
  assert.equal(d.element("tracked-slots-value").textContent, "-");
  assert.equal(d.element("tracked-slots-value")["aria-label"], "not counted yet");
  assert.equal(d.element("slot-events-value").textContent, "0");
  const cells = d.element("tracked-scopes-body").children[0].children;
  assert.equal(cells[0]["aria-label"], undefined, "a system label is not a missing count");
  assert.equal(cells[2].textContent, "-");
  assert.equal(cells[2]["aria-label"], "not counted yet");
  assert.equal(cells[3].textContent, "0");
  assert.equal(cells[5].textContent, new Date("2026-09-09T12:00:00Z").toLocaleString());
});

test("status template uses semantic terms and plain labels", () => {
  const template = fs.readFileSync(path.resolve(__dirname, "../../history_service/templates/dashboard.html"), "utf8");
  assert.match(template, /<dl/);
  assert.match(template, /<dt>Last shelf scan<\/dt>\s*<dd id="status-last-inventory-at"/);
  assert.match(template, />Quick refresh<\/button>/);
  assert.match(template, /Hourly summaries/);
  assert.doesNotMatch(template, />Tracked Scopes</);
});

const overview = (running, count = 7) => ({ collector: { collector_running: running }, counts: { tracked_slots: count }, counts_exact: true });

test("whole asset coalesces each read channel and waits for completion before scheduling", async () => {
  const d = dashboard();
  const first = d.poll.pollCollectorStatus();
  const second = d.poll.pollCollectorStatus();
  assert.equal(d.requests.length, 1);
  assert.ok(d.requests[0].options.signal);
  await d.advance(4000);
  assert.equal(d.requests.filter(r => r.url === "/healthz").length, 1);
  await d.reply(0, overview(false));
  await Promise.all([first, second]);
  await d.advance(1999);
  assert.equal(d.requests.length, 1);
  await d.advance(1);
  assert.equal(d.requests.length, 2);
});

test("older overview cannot replace newer health collector state but can update counts", async () => {
  const d = dashboard();
  d.poll.pollOverviewStatus();
  d.poll.pollOverviewStatus();
  d.poll.pollCollectorStatus();
  assert.equal(d.requests.length, 2);
  await d.reply(1, overview(false));
  await d.reply(0, overview(true, 9));
  assert.equal(d.element("collector-state-value").textContent, "Stopped");
  assert.equal(d.element("tracked-slots-value").textContent, "9");
});

test("older health cannot replace newer overview collector state", async () => {
  const d = dashboard();
  d.poll.pollCollectorStatus();
  d.poll.pollOverviewStatus();
  await d.reply(1, overview(false));
  await d.reply(0, overview(true));
  assert.equal(d.element("collector-state-value").textContent, "Stopped");
});

test("hidden startup does not poll; resume checks immediately and fences canceled responses", async () => {
  const d = dashboard({ hidden: true });
  await d.advance(30000);
  d.poll.pollCollectorStatus();
  assert.equal(d.requests.length, 0);
  await d.visibility(false);
  assert.equal(d.requests.length, 2);
  await d.visibility(true);
  assert.ok(d.requests.every(r => r.options.signal.aborted));
  let reads = 0;
  const late = { get collector() { reads += 1; return { collector_running: false }; } };
  await d.reply(0, late);
  await d.reply(1, late);
  assert.equal(reads, 0, "canceled payload must not even be consumed");
  await d.advance(30000);
  assert.equal(d.requests.length, 2);
  assert.match(d.element("history-collector-freshness").textContent, /paused|stale/i);
  await d.visibility(false);
  assert.equal(d.requests.length, 4);
});

test("hung fetch and hung body time out, label stale, and recover without late repaint", async () => {
  for (const hungBody of [false, true]) {
    const d = dashboard();
    const pending = d.poll.pollCollectorStatus();
    if (hungBody) d.requests[0].resolve({ ok: true, json: () => new Promise(() => {}) });
    await d.advance(15000);
    assert.ok(d.requests[0].options.signal?.aborted);
    await pending;
    assert.match(d.element("collector-state-value").textContent, /stale/i);
    assert.match(d.element("history-collector-freshness").textContent, /stale/i);
    const health = d.requests.map((r, i) => ({ ...r, i })).filter(r => r.url === "/healthz");
    assert.equal(health.length, 2);
    await d.reply(health[1].i, overview(false));
    await d.reply(0, overview(true));
    assert.equal(d.element("collector-state-value").textContent, "Stopped");
    assert.doesNotMatch(d.element("history-collector-freshness").textContent, /stale/i);
  }
});

test("overview failure remains visibly stale when health succeeds", async () => {
  const d = dashboard();
  d.poll.pollOverviewStatus();
  await d.reply(0, {}, 503);
  d.poll.pollCollectorStatus();
  await d.reply(1, overview(true));
  assert.match(d.element("history-overview-freshness").textContent, /stale/i);
  assert.equal(d.element("collector-state-value").textContent, "Running");
});

test("manual refresh cancels old reads, coalesces clicks, and renders confirmed success", async () => {
  const d = dashboard();
  d.poll.pollOverviewStatus();
  d.poll.pollCollectorStatus();
  await d.click("fast");
  await d.click("full");
  assert.equal(d.requests.filter(r => r.options.method === "POST").length, 1);
  assert.ok(d.requests[0].options.signal.aborted);
  assert.ok(d.requests[1].options.signal.aborted);
  const post = d.requests[2];
  assert.equal(post.options.body, JSON.stringify({ mode: "fast" }));
  assert.equal(post.options.headers["Content-Type"], "application/json");
  await d.reply(2, { ...overview(false, 42), ok: true, detail: "History fast refresh completed." });
  await d.reply(0, overview(true, 1));
  await d.reply(1, overview(true));
  assert.equal(d.element("tracked-slots-value").textContent, "42");
  assert.equal(d.element("collector-state-value").textContent, "Stopped");
  assert.equal(d.element("history-refresh-status").textContent, "History fast refresh completed.");
  assert.equal(d.element("history-refresh-fast").disabled, false);
});

test("refresh timeout is unknown outcome, never automatically retried or presented as failed", async () => {
  const d = dashboard();
  await d.click("full");
  await d.advance(125000);
  assert.ok(d.requests[0].options.signal.aborted);
  const message = d.element("history-refresh-status").textContent;
  assert.match(message, /outcome unknown/i);
  assert.match(message, /may still be running/i);
  assert.doesNotMatch(message, /refresh failed/i);
  assert.equal(d.element("history-refresh-full").disabled, true);
  await d.click("full");
  await d.reply(0, { ...overview(true), ok: true });
  assert.equal(d.requests.filter(r => r.options.method === "POST").length, 1);
  assert.equal(d.element("history-refresh-status").textContent, message);
});

test("explicit refresh refusal preserves detail and restores buttons", async () => {
  const d = dashboard();
  await d.click("full");
  await d.reply(0, { ok: false, detail: "History collection already running." }, 409);
  assert.equal(d.element("history-refresh-status").textContent, "Refresh failed: History collection already running.");
  assert.equal(d.element("history-refresh-full").disabled, false);
});

// Canonical HTTP 500 envelope from main.refresh_history's collection-failure
// handler, also asserted by test_history_refresh_endpoint_returns_json_error_on_collection_failure.
const failureEnvelope = require("../fixtures/history_refresh_failure.json");
const applicationFailure = mode => ({
  ...failureEnvelope,
  mode,
  detail: `History ${mode} refresh failed; see service logs.`,
  collector: {
    ...failureEnvelope.collector,
    last_error: `History ${mode} refresh failed; see service logs.`,
  },
});

for (const mode of ["fast", "full"]) {
  test(`confirmed ${mode} application failure restores both buttons without retrying`, async () => {
    const d = dashboard();
    await d.click(mode);
    await d.reply(0, applicationFailure(mode), 500);
    assert.equal(d.element("history-refresh-status").textContent,
      `Refresh failed: History ${mode} refresh failed; see service logs.`);
    assert.equal(d.element("history-refresh-fast").disabled, false);
    assert.equal(d.element("history-refresh-full").disabled, false);
    await d.advance(30000);
    assert.equal(d.requests.filter(r => r.options.method === "POST").length, 1);
    await d.click(mode);
    assert.equal(d.requests.filter(r => r.options.method === "POST").length, 2,
      "only an explicit new click may retry a confirmed failure");
  });
}

for (const [label, payload, code] of [
  ["generic JSON 500", { ok: false, detail: "Internal Server Error" }, 500],
  ["null JSON 500", null, 500],
  ["wrong mode", applicationFailure("fast"), 500],
  ["missing collector", { ...applicationFailure("full"), collector: undefined }, 500],
  ["malformed collector", { ...applicationFailure("full"), collector: { collector_running: "true" } }, 500],
  ["malformed collection state", { ...applicationFailure("full"), collector: { ...failureEnvelope.collector, collection_running: "false" } }, 500],
  ["unbound collector error", { ...applicationFailure("full"), collector: { ...failureEnvelope.collector, last_error: "Gateway failure" } }, 500],
  ["noncanonical detail", { ...applicationFailure("full"), detail: "Gateway failure" }, 500],
  ["missing failure flag", { ...applicationFailure("full"), ok: undefined }, 500],
  ["missing detail", { ...applicationFailure("full"), detail: undefined }, 500],
  ["gateway status", applicationFailure("full"), 504],
]) {
  test(`${label} is not proof of completed application failure`, async () => {
    const d = dashboard();
    await d.click("full");
    await d.reply(0, payload, code);
    assert.match(d.element("history-refresh-status").textContent, /outcome unknown/i);
    assert.equal(d.element("history-refresh-fast").disabled, true);
    assert.equal(d.element("history-refresh-full").disabled, true);
    await d.advance(30000);
    await d.click("full");
    assert.equal(d.requests.filter(r => r.options.method === "POST").length, 1);
  });
}

for (const malformedBody of [false, true]) {
  test(`${malformedBody ? "unreadable gateway body" : "network rejection"} keeps the write outcome unknown`, async () => {
    const d = dashboard();
    await d.click("full");
    if (malformedBody) {
      d.requests[0].resolve({ ok: false, status: 500, text: async () => "<html>Gateway error</html>" });
    } else {
      d.requests[0].reject(new TypeError("Failed to fetch"));
    }
    await flush();
    assert.match(d.element("history-refresh-status").textContent, /outcome unknown/i);
    assert.equal(d.element("history-refresh-fast").disabled, true);
    assert.equal(d.element("history-refresh-full").disabled, true);
    await d.advance(30000);
    await d.click("fast");
    assert.equal(d.requests.filter(r => r.options.method === "POST").length, 1);
  });
}

test("template labels initial values as snapshots without depending on JavaScript", () => {
  const template = fs.readFileSync(path.resolve(__dirname, "../../history_service/templates/dashboard.html"), "utf8");
  assert.match(template, /id="history-collector-freshness"[^>]*>Last checked: page load\. Collector status snapshot/);
  assert.match(template, /id="history-overview-freshness"[^>]*>Last checked: page load\. Overview snapshot/);
});

test("malformed health response is stale rather than an invented stopped collector", async () => {
  const d = dashboard();
  d.poll.pollCollectorStatus();
  await d.reply(0, {});
  assert.match(d.element("collector-state-value").textContent, /stale/i);
  assert.match(d.element("history-collector-freshness").textContent, /stale/i);
});

test("gateway timeout on a write is unknown rather than retry-safe failure", async () => {
  const d = dashboard();
  await d.click("full");
  await d.reply(0, { detail: "Gateway Timeout" }, 504);
  assert.match(d.element("history-refresh-status").textContent, /outcome unknown/i);
  assert.equal(d.element("history-refresh-full").disabled, true);
});

test("late read failure does not mark a newer collector observation stale", async () => {
  const d = dashboard();
  d.poll.pollOverviewStatus();
  d.poll.pollCollectorStatus();
  await d.reply(1, overview(false));
  await d.reply(0, {}, 503);
  assert.equal(d.element("collector-state-value").textContent, "Stopped");
  assert.doesNotMatch(d.element("history-collector-freshness").textContent, /stale/i);
  assert.match(d.element("history-overview-freshness").textContent, /stale/i);
});

test("hiding during a write pauses reads without pretending to cancel the server operation", async () => {
  const d = dashboard();
  await d.click("fast");
  await d.visibility(true);
  assert.equal(d.requests[0].options.signal.aborted, false);
  await d.reply(0, { ...overview(false, 42), ok: true });
  assert.equal(d.requests.length, 1);
  assert.match(d.element("collector-state-value").textContent, /stale/i);
  assert.equal(d.element("history-refresh-status").textContent, "History refresh completed.");
  await d.visibility(false);
  assert.equal(d.requests.length, 3);
});

test("token-policy DOM without action buttons still polls without issuing writes", async () => {
  const d = dashboard({ buttons: false });
  await d.advance(10000);
  assert.ok(d.requests.length > 0);
  assert.ok(d.requests.every(r => r.options.method !== "POST"));
});
