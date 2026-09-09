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

function dashboard({ hidden = false, buttons = true } = {}) {
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
      replaceChildren() {}, appendChild() {},
    });
    return elements.get(id);
  }
  const document = {
    hidden,
    getElementById(id) {
      if (id === "history-dashboard-bootstrap") return { textContent: '{"collector_running":true}' };
      if (!buttons && /history-refresh-(fast|full|status)/.test(id)) return null;
      return element(id);
    },
    createElement() { return { textContent: "", appendChild() {} }; },
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

test("template labels initial values as snapshots without depending on JavaScript", () => {
  const template = fs.readFileSync(path.resolve(__dirname, "../../history_service/templates/dashboard.html"), "utf8");
  assert.match(template, /id="history-collector-freshness"[^>]*>Collector status snapshot at page load/);
  assert.match(template, /id="history-overview-freshness"[^>]*>Overview snapshot at page load/);
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
