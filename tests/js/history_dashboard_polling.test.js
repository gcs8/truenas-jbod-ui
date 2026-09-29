"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { spawnSync } = require("node:child_process");
const source = fs.readFileSync(path.resolve(__dirname, "../../history_service/static/dashboard.js"), "utf8");

async function flush() {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

// Render the complete production template without importing service startup or
// opening a database. The DOM adapter below supports only this asset's APIs;
// these are offline DOM behavior tests, not browser layout or live-service QA.
function templateDocument(initial, buttons) {
  const rendered = spawnSync(process.env.PYTHON || "python", ["-c", `
import json, sys
from html.parser import HTMLParser
from jinja2 import Environment, FileSystemLoader
from app.script_json import register_script_json_filters
payload = json.load(sys.stdin)
env = Environment(loader=FileSystemLoader("history_service/templates"), autoescape=True)
register_script_json_filters(env)
html = env.get_template("dashboard.html").render(
    app_name="Synthetic history", app_version="test", status=payload["initial"],
    status_json=json.dumps(payload["initial"]), direct_refresh_enabled=payload["buttons"],
    refresh={}, counts={}, scopes=[], collector_state_label="Running",
    collector_banner_text="", url_for=lambda name, path: "/static/" + path,
)
class DOM(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = {"tag": "document", "attrs": {}, "children": []}
        self.stack = [self.root]
    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self.stack[-1]["children"].append(node)
        if tag not in {"meta", "link", "br", "hr", "img", "input"}:
            self.stack.append(node)
    def handle_endtag(self, tag):
        assert self.stack[-1]["tag"] == tag, (tag, self.stack[-1]["tag"])
        self.stack.pop()
    def handle_data(self, text):
        self.stack[-1]["children"].append(text)
dom = DOM()
dom.feed(html)
assert len(dom.stack) == 1
print(json.dumps(dom.root))
`], {
    cwd: path.resolve(__dirname, "../.."),
    input: JSON.stringify({ initial, buttons }), encoding: "utf8", timeout: 10000,
  });
  assert.equal(rendered.status, 0, rendered.stderr);
  const elements = new Map();
  const times = [];
  function node(record) {
    if (typeof record === "string") return { textContent: record };
    const attrs = { ...record.attrs };
    const classes = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
    const value = {
      children: record.children.map(node),
      disabled: Object.hasOwn(attrs, "disabled"), hidden: Object.hasOwn(attrs, "hidden"),
      get textContent() { return this.children.map(child => child.textContent).join(""); },
      set textContent(text) { this.children = [{ textContent: String(text) }]; },
      set innerHTML(html) { this.textContent = html; },
      getAttribute(name) { return Object.hasOwn(attrs, name) ? attrs[name] : null; },
      setAttribute(name, text) { attrs[name] = String(text); },
      removeAttribute(name) { delete attrs[name]; },
      addEventListener(event, callback) { this[event] = callback; },
      replaceChildren(...children) { this.children = children; },
      appendChild(child) { this.children.push(child); },
      classList: {
        toggle(name, force) { if (force) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      },
    };
    if (attrs.id) {
      assert.ok(!elements.has(attrs.id), `duplicate template id ${attrs.id}`);
      elements.set(attrs.id, value);
    }
    if (record.tag === "time" && attrs.datetime) times.push(value);
    return value;
  }
  node(JSON.parse(rendered.stdout));
  return { elements, times, createElement: () => node({ attrs: {}, children: [] }) };
}

function dashboard({ hidden = false, buttons = true, initial = { collector_running: true }, template = false } = {}) {
  let now = 0;
  let nextTimer = 0;
  const timers = new Map();
  const requests = [];
  const listeners = {};
  const dom = template ? templateDocument(initial, buttons) : null;
  const elements = dom?.elements || new Map();
  function element(id) {
    if (dom) {
      assert.ok(elements.has(id), `missing template hook ${id}`);
      return elements.get(id);
    }
    if (!elements.has(id)) elements.set(id, {
      textContent: id === "collector-state-value" ? "Running" : "",
      disabled: false, hidden: false,
      classList: {
        names: new Set(),
        toggle(name, force) { if (force) this.names.add(name); else this.names.delete(name); },
        contains(name) { return this.names.has(name); },
      },
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
      if (dom) return elements.get(id) || null;
      if (id === "history-dashboard-bootstrap") return { textContent: JSON.stringify(initial) };
      if (!buttons && /history-refresh-(fast|full|status)/.test(id)) return null;
      return element(id);
    },
    querySelectorAll() { return dom?.times || []; },
    createElement() { return dom ? dom.createElement() : { textContent: "", children: [], setAttribute(name, value) { this[name] = value; }, appendChild(child) { this.children.push(child); } }; },
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

// The standalone npm job is Node-only. It still runs every behavior assertion
// against the asset DOM. PYTHON selects the stronger complete-template fixture
// using the project's installed Jinja environment; failures never fall back.
const recoveryDashboard = options => dashboard({ ...options, template: Boolean(process.env.PYTHON) });
const recoveryFixture = process.env.PYTHON ? "complete template" : "asset DOM";

const quarantineA = "2026-09-09T12:00:00+00:00";
const quarantineB = "2026-09-10T13:00:00+00:00";
const recoveryWarning = "yes, earlier history was quarantined and this database started empty";
const recoveryCollector = (required, stamp) => ({
  collector_running: true, history_recovery_required: required, history_quarantined_at: stamp,
});
const recoveryPayload = (collector, channel) => channel === "health"
  ? { collector, status: collector.history_recovery_required ? "degraded" : "ok" }
  : { collector, ok: true, counts: { tracked_slots: 7 } };

async function sendRecovery(d, channel, collector, code = 200) {
  const index = d.requests.length;
  if (channel === "health") d.poll.pollCollectorStatus();
  else if (channel === "overview") d.poll.pollOverviewStatus();
  else await d.click("fast");
  assert.equal(d.requests[index].url, channel === "health" ? "/healthz" : `/api/history/${channel}`);
  await d.reply(index, recoveryPayload(collector, channel), code);
  // Failed refresh resumes reads too. Fail those checks before the next
  // explicit check. On success leave reads pending: assertions must prove
  // the refresh response itself updated the DOM, not a follow-up GET.
  if (channel === "refresh" && code !== 200) {
    for (let i = index + 1; i < d.requests.length; i += 1) {
      await d.reply(i, recoveryPayload(collector, "overview"), code);
    }
  }
}

function assertRecovery(d, required, stamp, warning = required !== false) {
  assert.equal(d.element("status-history-recovery-required").textContent,
    required === true ? recoveryWarning : required === false ? "no" : "unknown");
  assert.equal(d.element("status-history-recovery-required").classList.contains("status-error"), warning);
  assert.equal(d.element("status-history-quarantined-at").textContent, stamp);
}

for (const channel of ["health", "overview", "refresh"]) {
  test(`recovery ${recoveryFixture}: ${channel} updates both transitions and quarantine time`, async () => {
    const d = recoveryDashboard({ initial: recoveryCollector(false, null) });
    assertRecovery(d, false, "never");
    await sendRecovery(d, channel, recoveryCollector(true, quarantineA));
    assertRecovery(d, true, new Date(quarantineA).toLocaleString());
    assert.equal(d.element("history-collector-freshness").textContent, "Collector status checked successfully.");
    await sendRecovery(d, channel, recoveryCollector(true, quarantineB));
    assertRecovery(d, true, new Date(quarantineB).toLocaleString());
    await sendRecovery(d, channel, recoveryCollector(false, null));
    assertRecovery(d, false, "never");
  });

  test(`recovery ${recoveryFixture}: ${channel} does not coerce unknown evidence to no or never`, async () => {
    for (const prior of [false, true]) {
      const d = recoveryDashboard({ initial: recoveryCollector(prior, prior ? quarantineA : null) });
      for (const unknown of [undefined, null, "false", "true", 0, 1, {}, []]) {
        await sendRecovery(d, channel, { collector_running: true, history_recovery_required: unknown });
        assertRecovery(d, undefined, "not recorded");
      }
      for (const stamp of [undefined, "", "bad", 0, false, {}, []]) {
        await sendRecovery(d, channel, recoveryCollector(false, stamp));
        assertRecovery(d, false, "not recorded");
      }
      await sendRecovery(d, channel, recoveryCollector(true, null));
      assertRecovery(d, true, "not recorded");
      await sendRecovery(d, channel, recoveryCollector(false, quarantineB));
      assertRecovery(d, false, new Date(quarantineB).toLocaleString());
      await sendRecovery(d, channel, recoveryCollector(false, null));
      assertRecovery(d, false, "never");
    }
  });
}

test(`recovery ${recoveryFixture}: missing bootstrap fields become unknown, not a fresh negative`, () => {
  const d = recoveryDashboard();
  assertRecovery(d, undefined, "not recorded");
  assert.doesNotMatch(d.element("history-collector-freshness").textContent, /checked successfully/);
});

for (const older of ["health", "overview"]) {
  test(`recovery ${recoveryFixture}: older ${older} cannot repaint newer state or warning`, async () => {
    for (const required of [true, false]) {
      const d = recoveryDashboard({ initial: recoveryCollector(!required, quarantineA) });
      const polls = older === "health"
        ? [d.poll.pollCollectorStatus, d.poll.pollOverviewStatus]
        : [d.poll.pollOverviewStatus, d.poll.pollCollectorStatus];
      polls.forEach(poll => poll());
      await d.reply(1, recoveryPayload(recoveryCollector(required, quarantineB), "overview"));
      assertRecovery(d, required, new Date(quarantineB).toLocaleString());
      await d.reply(0, recoveryPayload(recoveryCollector(!required, quarantineA), "overview"));
      assertRecovery(d, required, new Date(quarantineB).toLocaleString());
    }
  });
}

test(`recovery ${recoveryFixture}: failed checks retain values but mark them stale until a known response`, async () => {
  for (const required of [true, false]) {
    const d = recoveryDashboard({ initial: recoveryCollector(required, required ? quarantineA : null) });
    const label = required ? new Date(quarantineA).toLocaleString() : "never";
    for (const channel of ["health", "overview", "refresh"]) {
      await sendRecovery(d, channel, recoveryCollector(!required, quarantineB), 503);
      assertRecovery(d, required, label);
      assert.match(d.element("history-collector-freshness").textContent, /stale/i);
    }
    await sendRecovery(d, "health", recoveryCollector(!required, quarantineB));
    assertRecovery(d, !required, new Date(quarantineB).toLocaleString());
  }
});

test(`recovery ${recoveryFixture}: manual refresh fences canceled reads in both directions`, async () => {
  for (const required of [true, false]) {
    const d = recoveryDashboard({ initial: recoveryCollector(!required, quarantineA) });
    d.poll.pollCollectorStatus();
    d.poll.pollOverviewStatus();
    await d.click("fast");
    await d.reply(2, recoveryPayload(recoveryCollector(required, quarantineB), "refresh"));
    assertRecovery(d, required, new Date(quarantineB).toLocaleString());
    await d.reply(0, recoveryPayload(recoveryCollector(!required, quarantineA), "health"));
    await d.reply(1, recoveryPayload(recoveryCollector(!required, quarantineA), "overview"));
    assertRecovery(d, required, new Date(quarantineB).toLocaleString());
  }
});

test(`recovery ${recoveryFixture}: hidden refresh completion cannot replace the snapshot`, async () => {
  const d = recoveryDashboard({ initial: recoveryCollector(true, quarantineA) });
  await d.click("fast");
  await d.visibility(true);
  await d.reply(0, recoveryPayload(recoveryCollector(false, null), "refresh"));
  assertRecovery(d, true, new Date(quarantineA).toLocaleString());
  assert.match(d.element("history-collector-freshness").textContent, /stale/i);
  await d.visibility(false);
  await d.reply(2, recoveryPayload(recoveryCollector(false, null), "overview"));
  assertRecovery(d, false, "never");
});

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

test("overview polling refreshes free space and backup footprint labels (#597)", async () => {
  const d = dashboard();
  d.poll.pollOverviewStatus();
  await d.reply(0, {
    ...overview(true, 3),
    database: { size_bytes: 40960, reclaimable_label: "2.0 KiB (25%)", backup_footprint_label: "3.0 KiB in 2 copies" },
  });
  assert.equal(d.element("db-reclaimable").textContent, "Free inside the file: 2.0 KiB (25%)");
  assert.equal(d.element("status-backup-footprint").textContent, "3.0 KiB in 2 copies");
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
