"use strict";

// #1232: a request the outgoing page queued while the crawler moves to the
// next state (for example the history dashboard's periodic GET /healthz) is
// judged by the policy of the state that page belongs to, never by the next
// state's read allowlist. The crawler retires the outgoing document before it
// arms the next state. Nothing is widened: a read the outgoing state does not
// admit, a mutation, a foreign origin and a read for another target are still
// refused during the transition, and a finished state's follow-up window still
// closes when the next navigation starts.
// No browser, no network: a fake page fires the outgoing document's queued
// requests through the real guard while a navigation is in flight.

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const root = path.resolve(__dirname, "..", "..");
const req = (rel) => require(path.join(root, rel));
const { Crawler } = req("qa/ui-crawl/crawler");
const { RequestGuard } = req("qa/ui-crawl/guard");
const { loadInventory, expandStates } = req("qa/ui-crawl/inventory");

const ORIGINS = {
  ui: "https://ui.synthetic.invalid",
  history: "https://history.synthetic.invalid",
  admin: "https://admin.synthetic.invalid",
};
const HISTORY = { page: "history", ready: "history", label: "synthetic/history", url: `${ORIGINS.history}/` };
const ADMIN = { page: "admin", ready: "admin", label: "synthetic/admin", url: `${ORIGINS.admin}/` };
const HEALTH_POLL = { method: "GET", url: `${ORIGINS.history}/healthz` };

function tmpDir(label) {
  return fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), `uic1232-${label}-`));
}

function stateById(id) {
  const inventory = loadInventory();
  const state = expandStates(inventory).find((entry) => entry.id === id);
  assert.ok(state, `no state ${id}`);
  return state;
}

// A page with one live document at a time. `queued` maps a document URL to
// the requests that document schedules once it has loaded (its timers). They
// fire while the next navigation is in flight, before it commits: the window
// in which the outgoing document is still alive.
function transitionPage(guard, queued = {}) {
  const page = {
    current: "about:blank",
    pending: [],
    sent: [],
    gotos: [],
    url() {
      return this.current;
    },
    async goto(url) {
      this.gotos.push(url);
      const outgoing = this.current;
      for (const request of this.pending.splice(0)) {
        const verdict = guard.admit({ body: null, ...request });
        this.sent.push({ from: outgoing, during: url, method: request.method, url: request.url, verdict });
      }
      this.current = url;
      this.pending = (queued[url] || []).map((request) => ({ ...request }));
      return { status: () => 200, headers: () => ({}) };
    },
    async waitForFunction() {},
    async waitForTimeout() {},
    async waitForLoadState() {},
    async evaluate() {
      return [];
    },
    locator() {
      const self = {
        count: async () => 0,
        filter: () => self,
        first: () => ({ isEnabled: async () => false, isVisible: async () => false, evaluate: async () => ["temperature_c"] }),
      };
      return self;
    },
  };
  return page;
}

// A Crawler on the fake page. Every state is not-applicable after its page
// loads, so only navigation and request admission run (no steps, no capture).
function stubCrawler({ queued = {}, fixtureMode = true, outDir }) {
  const inventory = loadInventory();
  const guard = new RequestGuard({ origins: ORIGINS });
  const crawler = new Crawler({ context: null, cdpFactory: null, guard, inventory, outDir, widths: [1600], fixtureMode, log: () => {} });
  const page = transitionPage(guard, queued);
  crawler.page = page;
  crawler.cdp = { send: async () => ({}) };
  crawler.unmetRequirement = async () => "synthetic: page load only";
  // Which document was alive each time a state's policy was armed.
  const armed = [];
  const begin = guard.beginState.bind(guard);
  guard.beginState = (stateId, options) => {
    armed.push({ stateId, document: page.url() });
    return begin(stateId, options);
  };
  return { crawler, guard, page, armed };
}

function onlySent(page, from) {
  return page.sent.filter((entry) => entry.from === from);
}

test("#1232: a health poll the history page queued is judged by history.default, not by the next admin state", async () => {
  const outDir = tmpDir("poll");
  try {
    const { crawler, guard, page, armed } = stubCrawler({ queued: { [HISTORY.url]: [HEALTH_POLL] }, outDir });
    const first = await crawler.crawlState(HISTORY, stateById("history.default"));
    const second = await crawler.crawlState(ADMIN, stateById("admin.operations"));
    assert.equal(first.status, "not-applicable", JSON.stringify(first));
    assert.equal(second.status, "not-applicable", JSON.stringify(second));

    // Non-vacuous: the history document really sent its poll during the transition.
    const late = onlySent(page, HISTORY.url);
    assert.equal(late.length, 1, JSON.stringify(page.sent));
    assert.equal(late[0].url, HEALTH_POLL.url);
    assert.equal(late[0].verdict.allowed, true, `the outgoing history page's own poll was refused: ${late[0].verdict.reason}`);
    assert.deepEqual(guard.blocked(), [], "nothing was refused");

    // The outgoing document was gone before the admin policy was armed.
    const admin = armed.find((entry) => entry.stateId === "admin.operations");
    assert.ok(admin, JSON.stringify(armed));
    assert.notEqual(admin.document, HISTORY.url, "admin.operations was armed while the history page was still alive");
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1232: the next state's allowlist does not admit a read the outgoing page's own state refuses", async () => {
  const outDir = tmpDir("reverse");
  try {
    // The admin page sending the history poll is outside admin.operations,
    // also while that page is the outgoing one and history.default is next.
    const { crawler, guard, page } = stubCrawler({ queued: { [ADMIN.url]: [HEALTH_POLL] }, outDir });
    await crawler.crawlState(ADMIN, stateById("admin.operations"));
    await crawler.crawlState(HISTORY, stateById("history.default"));
    const late = onlySent(page, ADMIN.url);
    assert.equal(late.length, 1, JSON.stringify(page.sent));
    assert.equal(late[0].verdict.allowed, false, "the next state's allowlist admitted the outgoing admin page's read");
    assert.equal(late[0].verdict.reason, "read GET /healthz is not in the read allowlist of admin.operations");
    assert.equal(guard.blocked().length, 1);
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1232 control: admin.operations still refuses the history poll; no allowlist was widened", () => {
  const { readsFor } = req("qa/ui-crawl/inventory");
  const guard = new RequestGuard({ origins: ORIGINS });
  guard.beginState("admin.operations", { reads: readsFor(loadInventory(), stateById("admin.operations")) });
  const verdict = guard.admit({ ...HEALTH_POLL, body: null });
  assert.equal(verdict.allowed, false);
  assert.equal(verdict.reason, "read GET /healthz is not in the read allowlist of admin.operations");
  // Positive control: history.default admits it while it is current.
  guard.beginState("history.default", { reads: readsFor(loadInventory(), stateById("history.default")) });
  assert.equal(guard.admit({ ...HEALTH_POLL, body: null }).allowed, true);
});

test("#1232 control: an outgoing page's undeclared read, mutation and foreign-origin request are still refused during the transition", async () => {
  const outDir = tmpDir("refusals");
  try {
    const queued = {
      [HISTORY.url]: [
        { method: "POST", url: `${ORIGINS.history}/api/history/scopes/bundle`, body: "{}" },
        { method: "POST", url: `${ORIGINS.history}/api/history/refresh`, body: "{}" },
        { method: "GET", url: "https://foreign.synthetic.invalid/collect" },
      ],
    };
    const { crawler, guard, page } = stubCrawler({ queued, outDir });
    await crawler.crawlState(HISTORY, stateById("history.default"));
    await crawler.crawlState(ADMIN, stateById("admin.operations"));
    const late = onlySent(page, HISTORY.url);
    assert.equal(late.length, 3, JSON.stringify(page.sent));
    assert.equal(guard.blocked().length, 3);
    const byUrl = Object.fromEntries(late.map((entry) => [`${entry.method} ${entry.url}`, entry.verdict]));
    // A read neither history.default nor admin.operations declares.
    const bundle = byUrl[`POST ${ORIGINS.history}/api/history/scopes/bundle`];
    assert.equal(bundle.allowed, false);
    assert.match(bundle.reason, /^read POST \/api\/history\/scopes\/bundle is not in the read allowlist of /);
    const refresh = byUrl[`POST ${ORIGINS.history}/api/history/refresh`];
    assert.equal(refresh.allowed, false);
    assert.equal(refresh.reason, "forbidden route outside a declaring state");
    const foreign = byUrl["GET https://foreign.synthetic.invalid/collect"];
    assert.equal(foreign.allowed, false);
    assert.equal(foreign.reason, "foreign origin");
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1232: with live target binding, the outgoing page's read is bound to its own target and another target's stays refused", async () => {
  const outDir = tmpDir("bind");
  const enclosureUrl = (enc) => `${ORIGINS.ui}/?system_id=sys-a&enclosure_id=${enc}`;
  const inventoryRead = (enc, extra = "") => ({ method: "GET", url: `${ORIGINS.ui}/api/inventory?system_id=sys-a&enclosure_id=${enc}${extra}` });
  const targetA = { page: "main", ready: "main", system: "sys-a", label: "synthetic/enc-a", url: enclosureUrl("enc-a") };
  const targetB = { page: "main", ready: "main", system: "sys-a", label: "synthetic/enc-b", url: enclosureUrl("enc-b") };
  try {
    const queued = { [targetA.url]: [inventoryRead("enc-a"), inventoryRead("enc-b"), inventoryRead("enc-a", "&force=true")] };
    const { crawler, guard, page } = stubCrawler({ queued, fixtureMode: false, outDir });
    const state = stateById("main.default");
    await crawler.crawlState(targetA, state);
    // A finished state's follow-up window closes when the next navigation
    // starts; retiring the page does not extend it.
    guard.openFollowUp(state.id, [{ method: "GET", path: "/api/inventory", force: true, max: 1 }], { bind: crawler.bindFor(targetA), ms: 60_000 });
    await crawler.crawlState(targetB, state);
    const late = onlySent(page, targetA.url);
    assert.equal(late.length, 3, JSON.stringify(page.sent));
    const [own, other, forced] = late.map((entry) => entry.verdict);
    assert.equal(own.allowed, true, `the outgoing page's read of its own enclosure was refused: ${own.reason}`);
    assert.equal(other.allowed, false, "a read for another enclosure than the outgoing page's target was admitted");
    assert.equal(other.reason, "read GET /api/inventory outside the bound enclosure_id (other) of main.default");
    assert.equal(forced.allowed, false, "a forced read after the follow-up window closed was admitted");
    assert.equal(forced.reason, "appliance-load route outside a declaring state");
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1232: a page that cannot be retired fails the next state before its policy is armed or its page loads", async () => {
  const outDir = tmpDir("stuck");
  try {
    const { crawler, page, armed } = stubCrawler({ queued: { [HISTORY.url]: [HEALTH_POLL] }, outDir });
    await crawler.crawlState(HISTORY, stateById("history.default"));
    const goto = page.goto.bind(page);
    page.goto = async (url, options) => {
      if (url === "about:blank") throw new Error("synthetic navigation failure");
      return goto(url, options);
    };
    const record = await crawler.crawlState(ADMIN, stateById("admin.operations"));
    assert.equal(record.status, "error", JSON.stringify(record));
    assert.match(record.findings.map((finding) => finding.text).join("\n"), /could not retire the previous page: synthetic navigation failure/);
    assert.equal(armed.some((entry) => entry.stateId === "admin.operations"), false, "admin.operations was armed while the history page was still alive");
    assert.equal(page.gotos.includes(ADMIN.url), false, "the admin page was loaded anyway");
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

test("#1232: option expansion also retires the outgoing page before it arms its state", async () => {
  const outDir = tmpDir("expand");
  const MAIN = { page: "main", ready: "main", label: "synthetic/main", url: `${ORIGINS.ui}/` };
  try {
    const { crawler, guard, page, armed } = stubCrawler({ queued: { [HISTORY.url]: [HEALTH_POLL] }, outDir });
    crawler.newDriver = () => ({ runSteps: async () => {} });
    await crawler.crawlState(HISTORY, stateById("history.default"));
    const expanded = await crawler.expandOptions(MAIN, stateById("main.heatmap.metric"));
    assert.deepEqual(expanded.map((state) => state.id), ["main.heatmap.metric{temperature_c}"]);
    const late = onlySent(page, HISTORY.url);
    assert.equal(late.length, 1, JSON.stringify(page.sent));
    assert.equal(late[0].verdict.allowed, true, `the outgoing history page's own poll was refused: ${late[0].verdict.reason}`);
    assert.deepEqual(guard.blocked(), []);
    const main = armed.find((entry) => entry.stateId === "main.heatmap.metric");
    assert.notEqual(main.document, HISTORY.url, "main.heatmap.metric was armed while the history page was still alive");
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
});

const MAIN = { page: "main", label: "synthetic/main", url: `${ORIGINS.ui}/` };
const ESTIMATE = "/api/export/enclosure-snapshot/estimate";

async function exportCrawler(t) {
  const outDir = tmpDir("estimate");
  t.after(() => fs.rmSync(outDir, { recursive: true, force: true }));
  const { crawler, guard, page } = stubCrawler({ outDir });
  const events = new EventEmitter();
  const pending = new Set();
  const goto = page.goto.bind(page);
  function finish(request, failed = false) {
    if (pending.delete(request)) events.emit(failed ? "requestfailed" : "requestfinished", request);
  }
  Object.assign(page, {
    on: events.on.bind(events),
    setDefaultTimeout() {},
    goto(url) {
      // The browser drops every outstanding fetch when the document retires.
      for (const request of pending) finish(request, true);
      return goto(url);
    },
  });
  crawler.context = { newPage: async () => page };
  crawler.cdpFactory = async () => crawler.cdp;
  await crawler.openPage();
  crawler.unmetRequirement = async (state) => state.page === "main" ? null : "synthetic: page load only";
  crawler.newDriver = () => ({
    resetState() {}, downloads: [],
    async runSteps() {
      for (const request of requests) {
        events.emit("request", request);
        assert.equal(guard.admit({ method: request.method(), url: request.url(), body: "{}" }).allowed, true);
        pending.add(request);
      }
    },
  });
  crawler.runChecks = async () => ({ findings: [] });
  let captured = false;
  crawler.capture = async () => {
    captured = true;
    return { tiles: [] };
  };
  const requests = [0, 1].map(() => ({
    method: () => "POST", url: () => `${ORIGINS.ui}${ESTIMATE}`, resourceType: () => "fetch",
    timing: () => ({ responseEnd: 10 }), failure: () => ({ errorText: "net::ERR_ABORTED" }),
  }));
  return { crawler, page, requests, finish, captured: () => captured };
}

test("export scope waits past capture for a superseded estimate before the next state", async (t) => {
  const { crawler, requests, finish, captured } = await exportCrawler(t);
  const records = (async () => [
    await crawler.crawlState(MAIN, stateById("main.export.scope")),
    await crawler.crawlState(HISTORY, stateById("history.default")),
  ])();
  await new Promise(setImmediate);
  assert.ok(captured(), "the export capture is complete while both requests are still outstanding");
  finish(requests[1]);
  await new Promise(setImmediate);
  finish(requests[0]);
  const [first, second] = await records;
  assert.deepEqual(second.findings, [], "retirement aborted the outgoing estimate in the next state");
  assert.deepEqual(first.findings, []);
  assert.equal(first.status, "done");
});

test("an estimate aborted by the live export dialog remains blocking", async (t) => {
  const { crawler, requests, finish, captured } = await exportCrawler(t);
  const record = crawler.crawlState(MAIN, stateById("main.export.dialog"));
  await new Promise(setImmediate);
  assert.ok(captured());
  finish(requests[0], true);
  finish(requests[1]);
  const result = await record;
  assert.ok(result.findings.some(({ type, severity, text }) => type === "request-failed" && severity === "error"
    && text === `POST ${ESTIMATE} net::ERR_ABORTED`), "the live document's abort must be reported");
  assert.equal(result.status, "error");
});

test("an export estimate that never settles is a named blocking timeout", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { crawler, page, requests, finish, captured } = await exportCrawler(t);
  const record = crawler.crawlState(MAIN, stateById("main.export.dialog"));
  await new Promise(setImmediate);
  assert.ok(captured());
  finish(requests[1]);
  t.mock.timers.tick(29_999);
  await new Promise(setImmediate);
  assert.equal(page.url(), MAIN.url, "the 30 s deadline has not expired");
  t.mock.timers.tick(1);
  const result = await record;
  assert.ok(result.findings.some(({ type, severity, text }) => type === "estimate-settle-timeout" && severity === "error"
    && text.includes(ESTIMATE)), "the unsettled request must be reported as a timeout");
  assert.equal(result.status, "error");
  assert.equal(page.url(), "about:blank");
});
